"""uninstall.sh --role app against a fake Docker CLI.

The script requires root on Ubuntu 24.04, like the deployment scripts; run
these in the test image (tests/Dockerfile) or skip them elsewhere.

The fake keeps its containers, volumes and networks in a JSON file, answers only
the commands and filters the script uses, and fails on anything else. Anonymous
volumes come in two kinds: "anonymous", created by Docker with the container
that mounts it, which "down --volumes" deletes; and "reused", handed by Compose
to a container it recreated at a redeploy, which "down --volumes" keeps (as seen
on Docker 29 with Compose v5). A volume that an existing container mounts is in
use and cannot be removed.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "uninstall.sh"
ODOO = "perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-v1.0.0@sha256:" + "c" * 64
WEB = "perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-web-v1.0.0@sha256:" + "6" * 64
PROJECT = "perodua-fixture"
FILESTORE = f"{PROJECT}_filestore"
# fixture-id-1 was recreated by a redeploy and reuses its anonymous volume;
# fixture-id-2 was created once. other-app is another Compose project, and
# anon-dangling belongs to no container.
CONTAINERS = {
    "fixture-id-1": [PROJECT, [FILESTORE, "anon-reused"]],
    "fixture-id-2": [PROJECT, ["anon-new"]],
    "other-id-1": ["other-app", ["other-app_data", "anon-other"]],
}
VOLUMES = {
    FILESTORE: f"named:{PROJECT}", "anon-reused": "reused", "anon-new": "anonymous",
    "anon-dangling": "anonymous", "other-app_data": "named:other-app", "anon-other": "anonymous",
}
NETWORKS = {"fixture-network": PROJECT, "other-network": "other-app"}
UNREACHABLE = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
READS = (["ps"], ["inspect"], ["network", "ls"], ["volume", "ls"])
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
TEMPLATE = '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}'
PROJECT_FILTER = 'label=com.docker.compose.project='
args, out, error = sys.argv[1:], [], None
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
with open(os.environ['FAKE_DOCKER_STATE']) as f:
    state = json.load(f)
containers, volumes, networks = state['containers'], state['volumes'], state['networks']

def filtered(prefix, length):  # the --filter value of "PREFIX... --filter VALUE"
    if args[:len(prefix)] != prefix or len(args) != length or args[-2] != '--filter':
        return None
    return args[-1]

def project_of(value):
    if not (value or '').startswith(PROJECT_FILTER):
        sys.exit(f'fake docker: unsupported filter {value!r}')
    return value[len(PROJECT_FILTER):]

def remove_containers(ids, with_volumes):  # with_volumes skips volumes still in use, like Docker
    for project, mounts in [containers.pop(container) for container in ids]:
        for name in mounts:
            in_use = any(name in other for _, other in containers.values())
            if with_volumes and not in_use and volumes.get(name) in ('named:' + project, 'anonymous'):
                del volumes[name]
    state['fail'] += state.pop('fail_after_down', [])

if any(' '.join(args).startswith(command) for command in state['fail']):
    error = UNREACHABLE
elif filtered(['ps', '-aq'], 4) is not None:
    project = project_of(args[-1])
    out = [c for c, (p, _) in containers.items() if p == project]
elif args[:1] == ['inspect']:
    if args[1:3] != ['--format', TEMPLATE] or len(args) < 4:
        sys.exit(f'fake docker: unsupported inspect {args}')
    for container in args[3:]:
        out += containers[container][1] + ['']
elif filtered(['network', 'ls', '-q'], 5) is not None:
    project = project_of(args[-1])
    out = [n for n, p in networks.items() if p == project]
elif filtered(['volume', 'ls', '-q'], 5) == 'label=com.docker.volume.anonymous':
    out = [v for v, kind in volumes.items() if not kind.startswith('named:')]
elif filtered(['volume', 'ls', '-q'], 5) is not None:
    project = project_of(args[-1])
    out = [v for v, kind in volumes.items() if kind == 'named:' + project]
elif args[:2] == ['volume', 'rm'] and len(args) > 2:
    for name in args[2:]:
        users = [c for c, (_, mounts) in containers.items() if name in mounts]
        if name not in volumes:
            error = f'Error response from daemon: get {name}: no such volume'
        elif users:
            error = f'Error response from daemon: remove {name}: volume is in use - [{", ".join(users)}]'
        else:
            del volumes[name]
elif args[:2] == ['network', 'rm'] and len(args) > 2:
    for name in args[2:]:
        del networks[name]
elif args[:2] == ['rm', '-f'] and len(args) > 2:
    remove_containers(args[2:], False)
elif args[:2] == ['compose', '--project-name'] and args[3] == '--file' and args[5:7] == ['down', '--remove-orphans'] \
        and args[7:] in ([], ['--volumes']):
    remove_containers([c for c, (p, _) in containers.items() if p == args[2]], args[7:] == ['--volumes'])
    for name in [n for n, p in networks.items() if p == args[2]]:
        del networks[name]
elif args[:2] == ['image', 'rm'] and len(args) == 3:
    pass
else:
    sys.exit(f'fake docker: unsupported command {args}')
with open(os.environ['FAKE_DOCKER_STATE'], 'w') as f:
    json.dump(state, f)
sys.stdout.write(''.join(line + '\n' for line in out))
sys.exit(error)
'''.replace("UNREACHABLE", repr(UNREACHABLE))


def ubuntu_2404():
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return False
    return "ID=ubuntu" in text and 'VERSION_ID="24.04"' in text


@unittest.skipUnless(os.geteuid() == 0 and ubuntu_2404(), "needs root on Ubuntu 24.04 (the test image)")
class UninstallAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="perodua-uninstall-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.calls = base / "docker-calls.jsonl"
        self.state = base / "docker-state.json"
        self.set_state()
        stubs = base / "bin"
        stubs.mkdir()
        docker = stubs / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                        FAKE_DOCKER_LOG=str(self.calls), FAKE_DOCKER_STATE=str(self.state))
        self.app = base / "app"
        self.app.mkdir()
        (self.app / ".deployment-identity").write_text(
            f"release=client-stable-uiux-v1.0.0\nrevision=fixture\nproject={PROJECT}\n"
            "host=192.0.2.20\nport=5432\ndatabase=perodua\nuser=odoo\n")
        (self.app / "compose.yml").write_text(
            f"services:\n  odoo:\n    image: {ODOO}\n  web:\n    image: {WEB}\n")
        (self.app / "secrets").mkdir()
        (self.app / "secrets" / "db_password").write_text("fixture-password")

    def set_state(self, **changes):
        state = {"containers": dict(CONTAINERS), "volumes": dict(VOLUMES), "networks": dict(NETWORKS),
                 "fail": [], "fail_after_down": []}
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def run_script(self, *args, stdin=subprocess.DEVNULL):
        return subprocess.run(["bash", str(SCRIPT), "--role", "app", "--dir", str(self.app), *args],
                              env=self.env, stdin=stdin, text=True, capture_output=True,
                              timeout=30, start_new_session=True)

    def docker_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def changes(self):
        return [call for call in self.docker_calls() if not any(call[:len(read)] == read for read in READS)]

    def left(self, kind):
        return json.loads(self.state.read_text())[kind]

    def left_volumes(self):
        return set(self.left("volumes"))

    def down(self, *extra):
        return ["compose", "--project-name", PROJECT, "--file", str(self.app / "compose.yml"),
                "down", "--remove-orphans", *extra]

    def test_default_removes_containers_their_anonymous_volumes_network_and_directory_but_keeps_data_and_images(self):
        result = self.run_script("--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stop and remove its 2 container(s)", result.stdout)
        self.assertIn("delete the 2 anonymous volume(s)", result.stdout)
        self.assertIn(f"Kept: the attachments volume {FILESTORE}", result.stdout)
        self.assertEqual(self.changes(), [
            self.down(),
            ["volume", "rm", "anon-reused"],
            ["volume", "rm", "anon-new"],
        ])
        self.assertIn("Deleted anonymous volume anon-reused", result.stdout)
        self.assertEqual(self.left_volumes(), {FILESTORE, "anon-dangling", "other-app_data", "anon-other"})
        self.assertEqual(list(self.left("containers")), ["other-id-1"])
        self.assertFalse(self.app.exists())

    def test_purge_also_removes_the_attachments_volume_and_the_images(self):
        result = self.run_script("--confirm", PROJECT, "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"delete the attachments volume {FILESTORE}", result.stdout)
        # "down --volumes" deletes anon-new itself; anon-reused only goes by name.
        self.assertEqual(self.changes(), [
            self.down("--volumes"),
            ["volume", "rm", "anon-reused"],
            ["volume", "rm", "anon-new"],
            ["image", "rm", ODOO],
            ["image", "rm", WEB],
        ])
        self.assertIn("Deleted anonymous volume anon-reused", result.stdout)
        self.assertNotIn("anon-new", result.stdout)
        self.assertNotIn("Kept", result.stdout)
        self.assertEqual(self.left_volumes(), {"anon-dangling", "other-app_data", "anon-other"})
        self.assertFalse(self.app.exists())

    def test_an_anonymous_volume_another_container_uses_is_kept_with_dockers_reason(self):
        self.set_state(containers=dict(CONTAINERS, **{"backup-id": [None, [FILESTORE, "anon-reused"]]}))
        result = self.run_script("--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kept anonymous volume anon-reused: Error response from daemon: "
                      "remove anon-reused: volume is in use - [backup-id]", result.stdout)
        self.assertIn("Deleted anonymous volume anon-new", result.stdout)
        self.assertIn("anon-reused", self.left_volumes())
        self.assertFalse(self.app.exists())

    def test_purge_keeps_an_anonymous_volume_another_container_uses(self):
        # "down --volumes" leaves a volume in use, so the explicit removal reports it.
        self.set_state(containers=dict(CONTAINERS, **{"backup-id": [None, ["anon-new"]]}))
        result = self.run_script("--confirm", PROJECT, "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kept anonymous volume anon-new: Error response from daemon: "
                      "remove anon-new: volume is in use - [backup-id]", result.stdout)
        self.assertIn("Deleted anonymous volume anon-reused", result.stdout)
        self.assertEqual(self.left_volumes(), {"anon-new", "anon-dangling", "other-app_data", "anon-other"})
        self.assertFalse(self.app.exists())

    def test_a_volume_docker_cannot_delete_is_named_with_the_error_and_the_uninstall_finishes(self):
        self.set_state(fail_after_down=["volume rm"])
        result = self.run_script("--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Kept anonymous volume anon-reused: {UNREACHABLE}", result.stdout)
        self.assertIn(f"Kept anonymous volume anon-new: {UNREACHABLE}", result.stdout)
        self.assertLessEqual({"anon-reused", "anon-new"}, self.left_volumes())
        self.assertFalse(self.app.exists())

    def test_without_a_compose_file_the_labelled_resources_and_anonymous_volumes_are_removed(self):
        (self.app / "compose.yml").unlink()
        result = self.run_script("--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes(), [
            ["rm", "-f", "fixture-id-1", "fixture-id-2"],
            ["network", "rm", "fixture-network"],
            ["volume", "rm", "anon-reused"],
            ["volume", "rm", "anon-new"],
        ])
        self.assertNotIn(["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
                         self.docker_calls())
        self.assertEqual(self.left_volumes(), {FILESTORE, "anon-dangling", "other-app_data", "anon-other"})
        self.assertEqual(self.left("networks"), {"other-network": "other-app"})
        self.assertEqual(list(self.left("containers")), ["other-id-1"])
        self.assertFalse(self.app.exists())

    def test_purge_without_a_compose_file_removes_the_project_volumes_only(self):
        (self.app / "compose.yml").unlink()
        result = self.run_script("--confirm", PROJECT, "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes(), [
            ["rm", "-f", "fixture-id-1", "fixture-id-2"],
            ["network", "rm", "fixture-network"],
            ["volume", "rm", FILESTORE],
            ["volume", "rm", "anon-reused"],
            ["volume", "rm", "anon-new"],
        ])
        self.assertEqual(self.left_volumes(), {"anon-dangling", "other-app_data", "anon-other"})
        self.assertFalse(self.app.exists())

    def test_containers_without_anonymous_volumes_lose_no_volume(self):
        # Docker lists the newest container first, here one without volumes, so
        # the inspect output starts with a blank line.
        self.set_state(containers={"fixture-id-2": [PROJECT, []], "fixture-id-1": [PROJECT, [FILESTORE]]},
                       volumes={FILESTORE: f"named:{PROJECT}", "other-app_data": "named:other-app"})
        result = self.run_script("--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("anonymous volume", result.stdout)
        self.assertEqual(self.changes(), [self.down()])
        self.assertEqual(self.left_volumes(), {FILESTORE, "other-app_data"})

    def test_without_containers_no_volume_is_looked_up(self):
        self.set_state(containers={"other-id-1": CONTAINERS["other-id-1"]})
        result = self.run_script("--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("anonymous volume", result.stdout)
        self.assertEqual(self.docker_calls(), [
            ["ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
            self.down(),
        ])
        self.assertEqual(self.left_volumes(), set(VOLUMES))
        self.assertFalse(self.app.exists())

    def test_if_the_volumes_cannot_be_listed_nothing_changes(self):
        for command, message in (("inspect", "Could not read the volumes of the App containers"),
                                 ("volume ls", "Could not list the anonymous volumes")):
            with self.subTest(command=command):
                self.calls.unlink(missing_ok=True)
                self.set_state(fail=[command])
                result = self.run_script("--confirm", PROJECT)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{message}. Nothing was changed.", result.stderr)
                self.assertEqual(self.changes(), [])
                self.assertEqual(self.left_volumes(), set(VOLUMES))
                self.assertTrue((self.app / "secrets" / "db_password").exists())

    def test_a_wrong_confirmation_changes_nothing(self):
        result = self.run_script("--confirm", "another-project")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertIn("delete the 2 anonymous volume(s)", result.stdout)
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.left_volumes(), set(VOLUMES))
        self.assertTrue((self.app / "secrets" / "db_password").exists())

    def test_without_a_terminal_or_confirm_nothing_changes(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"pass --confirm {PROJECT}", result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertTrue(self.app.exists())

    def test_a_directory_not_created_by_deploy_app_is_refused(self):
        (self.app / ".deployment-identity").unlink()
        result = self.run_script("--confirm", PROJECT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("was not created by deploy-app.sh", result.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertTrue(self.app.exists())


if __name__ == "__main__":
    unittest.main()
