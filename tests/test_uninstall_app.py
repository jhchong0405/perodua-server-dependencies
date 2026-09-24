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

deploy-app.sh holds DEPLOY_DIR/.deploy.lock while it runs. With each call the
fake, and a fake rm, record whether a deploy-app.sh started at that moment
would have found the lock held; after every run, each change must have found
it held. The fake rm also records what was left in the directory when the lock
file was removed: nothing else may be left.
"""

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import time
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
# The last two are the checks deploy-app.sh makes before it takes its lock.
READS = (["ps"], ["inspect"], ["network", "ls"], ["volume", "ls"], ["compose", "version"], ["info"])
# The start of the fake Docker CLI and of the fake rm.
FAKE_START = r'''#!/usr/bin/env python3
import fcntl, json, os, sys

def lock_state():  # what a deploy-app.sh started now would find
    try:
        fd = os.open(os.environ['FAKE_DOCKER_LOCK'], os.O_RDONLY)
    except FileNotFoundError:
        return 'missing'
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return 'free'
    except BlockingIOError:
        return 'held'
    finally:
        os.close(fd)
'''
FAKE_DOCKER = FAKE_START + r'''
TEMPLATE = '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}'
PROJECT_FILTER = 'label=com.docker.compose.project='
args, out, error = sys.argv[1:], [], None
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps({'args': args, 'lock': lock_state()}) + '\n')
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
elif args in (['compose', 'version'], ['info']):
    pass
elif args == ['info', '--format', '{{.OSType}}/{{.Architecture}}']:
    out = ['linux/x86_64']
else:
    sys.exit(f'fake docker: unsupported command {args}')
with open(os.environ['FAKE_DOCKER_STATE'], 'w') as f:
    json.dump(state, f)
sys.stdout.write(''.join(line + '\n' for line in out))
sys.exit(error)
'''.replace("UNREACHABLE", repr(UNREACHABLE))
FAKE_RM = FAKE_START + r'''
lock = os.environ['FAKE_DOCKER_LOCK']
directory = os.path.dirname(lock)
# What is left in the directory when the lock file, or the directory, goes.
left = sorted(os.listdir(directory)) if lock in sys.argv or directory in sys.argv else None
with open(os.environ['FAKE_RM_LOG'], 'a') as log:
    log.write(json.dumps({'args': ['rm', *sys.argv[1:]], 'lock': lock_state(), 'left': left}) + '\n')
os.execv('/usr/bin/rm', ['rm', *sys.argv[1:]])
'''
# Another uninstall deletes the lock file between this one's open and its lock,
# and a deployment may create a new one.
FAKE_FLOCK = r'''#!/usr/bin/env python3
import os, sys
lock = os.environ['FAKE_DOCKER_LOCK']
os.unlink(lock)
if os.environ['FAKE_FLOCK'] == 'replaced':
    open(lock, 'w').close()
os.execv('/usr/bin/flock', ['flock', *sys.argv[1:]])
'''


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
        self.stubs = base / "bin"
        self.stubs.mkdir()
        for name, source in (("docker", FAKE_DOCKER), ("rm", FAKE_RM)):
            self.stub(name, source)
        self.removals = base / "rm-calls.jsonl"
        self.app = base / "app"
        self.env = dict(os.environ, PATH=f"{self.stubs}{os.pathsep}{os.environ['PATH']}",
                        FAKE_DOCKER_LOG=str(self.calls), FAKE_DOCKER_STATE=str(self.state),
                        FAKE_DOCKER_LOCK=str(self.app / ".deploy.lock"), FAKE_RM_LOG=str(self.removals))
        self.app.mkdir()
        (self.app / ".deployment-identity").write_text(
            f"release=client-stable-uiux-v1.0.0\nrevision=fixture\nproject={PROJECT}\n"
            "host=192.0.2.20\nport=5432\ndatabase=perodua\nuser=odoo\n")
        (self.app / "compose.yml").write_text(
            f"services:\n  odoo:\n    image: {ODOO}\n  web:\n    image: {WEB}\n")
        (self.app / "secrets").mkdir()
        (self.app / "secrets" / "db_password").write_text("fixture-password")
        # deploy-app.sh also leaves its settings and its lock file, unlocked.
        (self.app / "app.env").write_text(
            f"DB_HOST=192.0.2.20\nDB_PORT=5432\nDB_NAME=perodua\nDB_USER=odoo\nPROJECT_NAME={PROJECT}\n")
        (self.app / ".deploy.lock").touch()

    def stub(self, name, source):
        (self.stubs / name).write_text(source)
        (self.stubs / name).chmod(0o755)

    def set_state(self, **changes):
        state = {"containers": dict(CONTAINERS), "volumes": dict(VOLUMES), "networks": dict(NETWORKS),
                 "fail": [], "fail_after_down": []}
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def run_script(self, *args, stdin=subprocess.DEVNULL):
        result = subprocess.run(["bash", str(SCRIPT), "--role", "app", "--dir", str(self.app), *args],
                                env=self.env, stdin=stdin, text=True, capture_output=True,
                                timeout=30, start_new_session=True)
        self.check_lock()
        return result

    @staticmethod
    def log(path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def docker_calls(self):
        return [entry["args"] for entry in self.log(self.calls)]

    def changes(self):
        return [call for call in self.docker_calls() if not any(call[:len(read)] == read for read in READS)]

    def check_lock(self):
        # A deployment started during any change must have been refused: every
        # changing Docker call and every removal found the lock held, and the
        # lock file was removed on its own, once nothing else was left.
        changes = [entry for entry in self.log(self.calls)
                   if not any(entry["args"][:len(read)] == read for read in READS)]
        removals = self.log(self.removals)
        self.assertEqual([entry["args"] for entry in changes + removals if entry["lock"] != "held"], [],
                         "changed while a deployment could start")
        listings = [entry["left"] for entry in removals if entry["left"] is not None]
        self.assertEqual(listings, [[".deploy.lock"]] * len(listings),
                         "left in the directory when the lock file was removed")
        self.assertTrue(listings or self.app.exists(), "the directory was removed without removing the lock file last")

    def hold_lock(self):
        # Another process takes the lock the way deploy-app.sh does, and keeps it.
        holder = subprocess.Popen(["bash", "-c", 'exec 9>"$1" && flock -n 9 && echo held && exec sleep infinity',
                                   "deploy-app", str(self.app / ".deploy.lock")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline(), "held\n")

    def confirm_on_terminal(self, answer, while_waiting):
        # Runs the uninstall on a pseudo-terminal without --confirm, calls
        # while_waiting() at the confirmation prompt, then types the answer.
        pid, terminal = pty.fork()
        if pid == 0:
            try:
                os.execvpe("bash", ["bash", str(SCRIPT), "--role", "app", "--dir", str(self.app)], self.env)
            finally:
                os._exit(127)
        prompt, output, status = f"Type {PROJECT} to confirm: ".encode(), b"", None
        deadline = time.monotonic() + 30

        def read():  # False once the script has exited and its output is read
            nonlocal output
            self.assertLess(time.monotonic(), deadline, "timed out: " + output.decode(errors="replace"))
            if select.select([terminal], [], [], 0.1)[0]:
                try:
                    chunk = os.read(terminal, 65536)
                except OSError:  # the terminal closed
                    return False
                output += chunk
                return bool(chunk)
            return True

        try:
            while prompt not in output:
                self.assertTrue(read(), "no confirmation prompt: " + output.decode(errors="replace"))
            while_waiting()
            os.write(terminal, answer + b"\n")
            while read():
                pass
            status = os.waitpid(pid, 0)[1]
        finally:
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(terminal)
        self.check_lock()
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")

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
        (self.app / ".deploy.lock").unlink()
        result = self.run_script("--confirm", PROJECT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("was not created by deploy-app.sh", result.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertTrue(self.app.exists())
        self.assertFalse((self.app / ".deploy.lock").exists())  # no lock file left in such a directory

    def test_while_a_deployment_runs_nothing_is_listed_or_changed(self):
        self.hold_lock()
        for args in (("--confirm", PROJECT), ("--confirm", PROJECT, "--purge")):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("A deployment is running in this directory. Nothing was changed.", result.stderr)
                self.assertEqual(self.docker_calls(), [])
                self.assertEqual(self.left_volumes(), set(VOLUMES))
                self.assertTrue((self.app / "secrets" / "db_password").exists())

    def test_a_lock_on_a_lock_file_deleted_before_it_was_taken_is_refused(self):
        # The lock then succeeds on the old file, which no deployment checks.
        self.stub("flock", FAKE_FLOCK)
        for change in ("replaced", "removed"):
            with self.subTest(lock_file=change):
                self.env["FAKE_FLOCK"] = change
                result = self.run_script("--confirm", PROJECT)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("was removed or replaced while this uninstall was starting. Nothing was changed.",
                              result.stderr)
                self.assertEqual(self.docker_calls(), [])
                self.assertTrue((self.app / "secrets" / "db_password").exists())

    def test_a_deployment_started_while_the_uninstall_waits_for_confirmation_is_refused(self):
        deployments = []

        def deploy():
            deployments.append(subprocess.run(
                ["bash", str(SCRIPTS / "deploy-app.sh"), "--dir", str(self.app), "--non-interactive"],
                env=self.env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30))
            self.assertEqual(self.changes(), [])

        code, output = self.confirm_on_terminal(PROJECT.encode(), deploy)
        self.assertEqual(deployments[0].returncode, 1)
        self.assertIn("Error: Another deployment is running in this directory", deployments[0].stderr)
        self.assertEqual(code, 0, output)
        self.assertIn(f'SUCCESS: the App deployment "{PROJECT}" was removed.', output)
        self.assertEqual(self.changes(), [
            self.down(),
            ["volume", "rm", "anon-reused"],
            ["volume", "rm", "anon-new"],
        ])
        self.assertFalse(self.app.exists())


if __name__ == "__main__":
    unittest.main()
