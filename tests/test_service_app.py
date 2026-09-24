"""service.sh --role app against a fake Docker CLI and a fake deploy-app.sh.

The script requires root; run these in the test image (tests/Dockerfile) or
skip them elsewhere. The fake Docker keeps the containers and volumes in a JSON
file, answers only the commands the script uses and fails on anything else. Each
call records whether a deploy-app.sh started at that moment would have found the
deployment lock (.deploy.lock) held. The fake deploy-app.sh sits next to a copy
of service.sh and records its arguments and the lock the same way.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PROJECT = "perodua-fixture"
FILESTORE = f"{PROJECT}_filestore"
RELEASE = "client-stable-uiux-v9.9.9"
REVISION = "0123456789abcdef0123456789abcdef01234567"
WIPE = ["run", "--rm", "--no-deps", "-T", "odoo", "python3", "-"]
CHECK = ["run", "--rm", "--no-deps", "-T", "odoo", "python3", "/opt/deploy/preflight.py", "check"]
LOCK_STATE = r'''#!/usr/bin/env python3
import fcntl, os
def lock_state():  # what a deploy-app.sh started now would find
    try:
        fd = os.open(os.environ['FAKE_LOCK'], os.O_RDONLY)
    except FileNotFoundError:
        return 'missing'
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return 'free'
    except BlockingIOError:
        return 'held'
    finally:
        os.close(fd)
if __name__ == '__main__':
    print(lock_state())
'''
FAKE_DOCKER = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lockstate import lock_state
TEMPLATE = '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}'
args, out, status = sys.argv[1:], [], 0
entry = {'args': args, 'lock': lock_state()}
with open(os.environ['FAKE_DOCKER_STATE']) as f:
    state = json.load(f)
containers, volumes = state['containers'], state['volumes']
compose = args[:3] == ['compose', '--project-name', state['project']] and args[3] == '--file'
action = args[5:] if compose else None
if compose and action == ['stop']:
    pass
elif compose and action == ['run', '--rm', '--no-deps', '-T', 'odoo', 'python3', '/opt/deploy/preflight.py', 'check']:
    entry['stdin'] = sys.stdin.read()  # compose run passes stdin to the container
    out = [state['preflight']] if state['preflight'] else []
    status = 0 if state['preflight'] else 1
elif compose and action == ['up', '--detach', '--no-recreate', '--wait', '--wait-timeout', '600']:
    status = state['up_status']
elif compose and action[:2] == ['ps', '--all']:
    out = ['SERVICE   STATUS   PORTS']
elif compose and action == ['run', '--rm', '--no-deps', '-T', 'odoo', 'python3', '-']:
    entry['stdin'] = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
    status = state['wipe_status']
elif compose and action == ['down', '--volumes', '--remove-orphans']:
    for name in [m for project, mounts in containers.values() if project == state['project'] for m in mounts]:
        if volumes.get(name) in ('named', 'anonymous'):  # "reused" ones stay, as Docker does
            del volumes[name]
    containers.clear()
elif args == ['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + state['project']]:
    out = list(containers)
elif args[:3] == ['inspect', '--format', TEMPLATE] and len(args) > 3:
    for container in args[3:]:
        out += containers[container][1] + ['']
elif args == ['volume', 'ls', '-q', '--filter', 'label=com.docker.volume.anonymous']:
    out = [v for v, kind in volumes.items() if kind != 'named']
elif args[:2] == ['volume', 'rm'] and len(args) == 3:
    if args[2] in volumes:
        del volumes[args[2]]
    else:
        sys.stderr.write(f'Error response from daemon: get {args[2]}: no such volume\n')
        status = 1
else:
    sys.exit(f'fake docker: unsupported command {args}')
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(entry) + '\n')
with open(os.environ['FAKE_DOCKER_STATE'], 'w') as f:
    json.dump(state, f)
sys.stdout.write(''.join(line + '\n' for line in out))
sys.exit(status)
'''
FAKE_DEPLOY_APP = f'''#!/usr/bin/env bash
# Fixture: the release lines service.sh reads, and a record of the call.
RELEASE={RELEASE}
REVISION={REVISION}
''' + r'''printf '%s %s\n' "$(python3 "$FAKE_LOCKSTATE")" "$*" >> "$FAKE_DEPLOY_LOG"
exit "${FAKE_DEPLOY_STATUS:-0}"
'''


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir(),
                     "needs root on Linux (the test image)")
class ServiceAppTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="perodua-service-test-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.bundle = base / "bundle"
        self.bundle.mkdir()
        for name in ("service.sh", "reset_database.py"):
            shutil.copy(SCRIPTS / name, self.bundle / name)
        (self.bundle / "deploy-app.sh").write_text(FAKE_DEPLOY_APP)
        stubs = base / "bin"
        stubs.mkdir()
        for name, source in (("docker", FAKE_DOCKER), ("lockstate.py", LOCK_STATE)):
            (stubs / name).write_text(source)
            (stubs / name).chmod(0o755)
        self.app = base / "app"
        self.app.mkdir()
        self.identity = (f"release=client-stable-uiux-v1.0.2\nrevision={'f' * 40}\nproject={PROJECT}\n"
                         "host=192.0.2.20\nport=5432\ndatabase=perodua\nuser=odoo\n")
        (self.app / ".deployment-identity").write_text(self.identity)
        (self.app / "compose.yml").write_text("services: {}\n")
        (self.app / "app.env").write_text(f"DB_HOST=192.0.2.20\nPROJECT_NAME={PROJECT}\n")
        (self.app / ".deploy.lock").touch()
        self.calls, self.state, self.deploys = base / "docker.jsonl", base / "state.json", base / "deploy.log"
        self.set_state()
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                        FAKE_LOCK=str(self.app / ".deploy.lock"), FAKE_LOCKSTATE=str(stubs / "lockstate.py"),
                        FAKE_DOCKER_LOG=str(self.calls), FAKE_DOCKER_STATE=str(self.state),
                        FAKE_DEPLOY_LOG=str(self.deploys))

    def set_state(self, **changes):
        # odoo-1 was recreated by a redeploy and reuses its anonymous volume,
        # which "down --volumes" keeps; web-1 has one that it deletes itself.
        state = {"project": PROJECT, "preflight": "READY 12", "up_status": 0, "wipe_status": 0,
                 "containers": {"odoo-1": [PROJECT, [FILESTORE, "anon-reused"]], "web-1": [PROJECT, ["anon-new"]]},
                 "volumes": {FILESTORE: "named", "anon-reused": "reused", "anon-new": "anonymous",
                             "anon-other": "anonymous"}}
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def run_script(self, *args, stdin=None, **env):
        return subprocess.run(["bash", str(self.bundle / "service.sh"), "--role", "app", "--dir", str(self.app), *args],
                              env=dict(self.env, **env), input=stdin,
                              stdin=subprocess.DEVNULL if stdin is None else None, text=True, capture_output=True,
                              timeout=30, start_new_session=True)

    def docker_log(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def docker_calls(self):  # compose calls without the project and file options
        return [entry["args"][5:] if entry["args"][0] == "compose" else entry["args"] for entry in self.docker_log()]

    def deploy_calls(self):
        return self.deploys.read_text().splitlines() if self.deploys.exists() else []

    def hold_lock(self):
        # Another process takes the lock the way deploy-app.sh does, and keeps it.
        holder = subprocess.Popen(["bash", "-c", 'exec 9>"$1" && flock -n 9 && echo held && exec sleep infinity',
                                   "deploy-app", str(self.app / ".deploy.lock")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline(), "held\n")

    def assert_nothing_changed(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.deploy_calls(), [])
        self.assertEqual((self.app / ".deployment-identity").read_text(), self.identity)

    def test_stop_start_restart_hold_the_lock_and_status_does_not_need_it(self):
        up = ["up", "--detach", "--no-recreate", "--wait", "--wait-timeout", "600"]
        for action, expected in (("stop", [["stop"]]), ("start", [CHECK, up]), ("restart", [CHECK, ["stop"], up])):
            with self.subTest(action=action):
                self.calls.unlink(missing_ok=True)
                result = self.run_script(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.docker_calls(), expected)
                self.assertEqual({entry["lock"] for entry in self.docker_log()}, {"held"})
        self.calls.unlink()
        self.hold_lock()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_calls(), [["ps", "--all", "--format", r"table {{.Service}}\t{{.Status}}\t{{.Ports}}"]])

    def test_a_running_deployment_stops_every_change(self):
        self.hold_lock()
        for args in (["stop"], ["start"], ["restart"], ["reset", "--confirm", "perodua"]):
            with self.subTest(action=args[0]):
                self.assert_nothing_changed(self.run_script(*args), "A deployment or uninstall is running")

    def test_start_and_restart_need_a_database_that_is_set_up(self):
        # On an empty or unfinished database, Odoo would set itself up at start.
        for preflight, message in (("EMPTY", "The database is not set up (EMPTY)"),
                                   ("SETUP_PENDING", "The database is not set up (SETUP_PENDING)"),
                                   ("", "the database check failed for the reason above")):
            for action in ("start", "restart"):
                with self.subTest(preflight=preflight, action=action):
                    self.calls.unlink(missing_ok=True)
                    self.set_state(preflight=preflight)
                    result = self.run_script(action)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(self.docker_calls(), [CHECK])

    def test_the_database_check_leaves_stdin_alone(self):
        # Seen on a real server: run from a script on stdin, the check's
        # "compose run" read the rest of that script.
        result = self.run_script("start", stdin="echo the rest of a script\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        check = next(entry for entry in self.docker_log() if entry["args"][5:] == CHECK)
        self.assertEqual(check["stdin"], "")

    def test_an_unhealthy_start_names_the_log_command(self):
        self.set_state(up_status=1)
        result = self.run_script("start")
        self.assertEqual(result.returncode, 1)
        self.assertIn("The App did not become healthy", result.stderr)
        self.assertIn("logs --tail 100", result.stderr)

    def test_reset_empties_the_database_then_initializes_the_release_next_to_it(self):
        result = self.run_script("reset", "--confirm", "perodua")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"deploy-app.sh --init-db: {RELEASE} (now client-stable-uiux-v1.0.2)", result.stdout)
        calls = self.docker_calls()
        self.assertEqual(calls[:6], [
            ["ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
            ["inspect", "--format", '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}',
             "odoo-1", "web-1"],
            ["volume", "ls", "-q", "--filter", "label=com.docker.volume.anonymous"],
            ["stop"],
            WIPE,
            ["down", "--volumes", "--remove-orphans"],
        ])
        # The App's two anonymous volumes are deleted by name ("down" already
        # took anon-new); another container's stays.
        self.assertEqual(sorted(calls[6:]), [["volume", "rm", "anon-new"], ["volume", "rm", "anon-reused"]])
        self.assertEqual(json.loads(self.state.read_text())["volumes"], {"anon-other": "anonymous"})
        wipe = next(entry for entry in self.docker_log() if "stdin" in entry)
        self.assertEqual(wipe["stdin"], hashlib.sha256((SCRIPTS / "reset_database.py").read_bytes()).hexdigest())
        self.assertEqual({entry["lock"] for entry in self.docker_log()}, {"held"})
        # deploy-app.sh runs last, with the lock free for it to take, and finds
        # the directory bound to its own release.
        self.assertEqual(self.deploy_calls(), [f"free --dir {self.app} --init-db"])
        self.assertEqual((self.app / ".deployment-identity").read_text(),
                         self.identity.replace("client-stable-uiux-v1.0.2", RELEASE).replace("f" * 40, REVISION))

    def test_reset_changes_nothing_without_a_matching_confirmation(self):
        self.assert_nothing_changed(self.run_script("reset", "--confirm", "other"), "--confirm must be exactly perodua")
        self.assert_nothing_changed(self.run_script("reset"), "No terminal to confirm on: pass --confirm perodua")
        (self.app / "app.env").unlink()
        self.assert_nothing_changed(self.run_script("reset", "--confirm", "perodua"), "app.env is missing")

    def test_a_failed_wipe_stops_before_the_containers_and_attachments_go(self):
        for status, message in ((3, "Nothing was deleted. The App is stopped"), (1, "only partly emptied")):
            with self.subTest(status=status):
                self.calls.unlink(missing_ok=True)
                self.set_state(wipe_status=status)
                result = self.run_script("reset", "--confirm", "perodua")
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.docker_calls()[-2:], [["stop"], WIPE])
                self.assertIn(FILESTORE, json.loads(self.state.read_text())["volumes"])
                self.assertEqual(self.deploy_calls(), [])

    def test_a_failed_initialization_says_to_run_the_reset_again(self):
        result = self.run_script("reset", "--confirm", "perodua", FAKE_DEPLOY_STATUS="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy-app.sh did not finish", result.stderr)
        self.assertEqual(len(self.deploy_calls()), 1)

    def test_reset_is_refused_on_the_database_server(self):
        result = subprocess.run(["bash", str(self.bundle / "service.sh"), "--role", "db", "reset"],
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("reset runs on the App server", result.stderr)


if __name__ == "__main__":
    unittest.main()
