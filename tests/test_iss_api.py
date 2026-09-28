"""iss-api.sh against a fake Docker CLI, a fake ss, and mv and tar that can fail.

The script requires root; run these in the test image (tests/Dockerfile) or skip
them elsewhere. The fake Docker answers only the commands the script uses, fails
on anything else, and records each call with its standard input and whether the
script's directory lock (in /run/lock) was held at that moment. It models the
Compose project's working directory, which a successful `up` sets to the
deployment directory. The fake ss reports only the port the container publishes
as listening, after a successful `up` and until `stop` or `down`. mv and tar run
the real programs unless FAKE_MV_FAIL names a destination file or FAKE_TAR_FAIL
asks the next archive creation to fail. Nothing is deployed.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "iss-api.sh"
IMAGE = re.search(r"^IMAGE=(\S+)$", SCRIPT.read_text(), re.M).group(1)
OLD_IMAGE = "perodua-deploy.novutal.com/iss-oracle-api:v0.9.0@sha256:" + "0" * 64
PROJECT = "perodua-iss-api"
UP = ["up", "--detach", "--wait", "--wait-timeout", "120", "--remove-orphans", "--pull", "never"]
CHECK = ["exec", "-T", "api", "python"]
OWNER = ["ps", "--all", "--filter", f"label=com.docker.compose.project={PROJECT}",
         "--format", '{{.Label "com.docker.compose.project.working_dir"}}']
GENERATION = ["compose.yml", "iss-api.env", "secrets/db_password"]
# characters that a shell, Compose interpolation or an env file would change
PASSWORD = 'pa$$ #"wo\'rd\\'
FAKE_DOCKER = r'''#!/usr/bin/env python3
import fcntl, json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    state = json.load(f)
def lock_state():  # what another iss-api.sh started now would find
    try:
        fd = os.open(os.environ["FAKE_LOCK"], os.O_RDONLY)
    except FileNotFoundError:
        return "missing"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return "free"
    except BlockingIOError:
        return "held"
    finally:
        os.close(fd)
entry, out, status = {"args": args, "lock": lock_state()}, "", 0
compose = len(args) > 5 and args[:3] == ["compose", "--project-name", state["project"]] and args[3] == "--file"
action = args[5:] if compose else None
if args == ["compose", "version"]:
    out = "Docker Compose version v5.0.0\n"
elif args == ''' + repr(OWNER) + r''':
    out = state["owner"] + "\n" if state["owner"] else ""
elif args[:4] == ["ps", "--all", "--quiet", "--filter"]:
    pass
elif args[:2] == ["image", "inspect"]:
    status = 0 if state["image_present"] else 1
elif args[:1] == ["pull"]:
    state["image_present"] = True
elif compose and action == ["config", "--quiet"]:
    entry["compose_file"] = open(args[4]).read()
    status = state["config_status"]
elif compose and action == ["ps", "--quiet", "api"]:
    out = "c0ffee\n" if state["listening"] else ""
elif compose and action in (["logs", "--tail", "40", "api"], ["restart", "api"]):
    pass
elif compose and action in (["stop"], ["down", "--remove-orphans"], ["down", "--remove-orphans", "--rmi", "all"]):
    state["listening"] = False
    if action[0] == "down":
        state["owner"] = ""
elif compose and action in (''' + repr(UP) + r''', ''' + repr(UP) + r''' + ["--force-recreate"]):
    entry["compose_file"] = open(args[4]).read()
    entry["password"] = open(os.path.join(os.path.dirname(args[4]), "secrets", "db_password")).read()
    status = state["up_status"].pop(0) if state["up_status"] else 0
    if status == 0:  # the container now publishes the port in the compose file
        state["listening"] = True
        state["port"] = int(re.search(r':(\d+):8000"', entry["compose_file"]).group(1))
        state["owner"] = os.path.dirname(args[4])
elif compose and action[:4] == ''' + repr(CHECK) + r''':
    entry["stdin"] = sys.stdin.read()
    out = "OK   fake check\n"
else:
    sys.exit(f"fake docker: unsupported command {args}")
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
with open(os.environ["FAKE_DOCKER_STATE"], "w") as f:
    json.dump(state, f)
sys.stdout.write(out)
sys.exit(status)
'''
# ss -ltnH "sport = :PORT"
FAKE_SS = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    state = json.load(f)
if state["listening"] and sys.argv[-1] == f"sport = :{state['port']}":
    print(f"LISTEN 0 4096 127.0.0.1:{state['port']} 0.0.0.0:*")
'''
FAKE_MV = r'''#!/usr/bin/env python3
import os, sys
fail, once = os.environ.get("FAKE_MV_FAIL"), os.environ["FAKE_MV_ONCE"]
if fail and sys.argv[-1].endswith("/" + fail) and not os.path.exists(once):  # fails the first such move only
    open(once, "w").close()
    sys.exit("mv: fake failure")
os.execv(os.environ["REAL_MV"], ["mv"] + sys.argv[1:])
'''
FAKE_TAR = r'''#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_TAR_FAIL") and "-cf" in sys.argv:
    open(sys.argv[sys.argv.index("-cf") + 1], "w").write("partial")
    sys.exit("tar: fake failure")
os.execv(os.environ["REAL_TAR"], ["tar"] + sys.argv[1:])
'''


def lock_path(directory):  # where iss-api.sh keeps the lock for a deployment directory
    base = "/run/lock" if os.path.isdir("/run/lock") else "/run"
    return f"{base}/iss-api-{hashlib.sha256(os.path.realpath(directory).encode()).hexdigest()[:16]}.lock"


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir(),
                     "needs root on Linux (the test image)")
class IssApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = Path(os.path.realpath(self.tmp)) / "deploy"
        self.lock = lock_path(self.dir)
        project_lock = Path(lock_path(self.dir)).with_name(f"iss-api-project-{PROJECT}.lock")
        for path in (Path(self.lock), project_lock):
            self.addCleanup(path.unlink, missing_ok=True)
        self.log = self.tmp / "docker.log"
        self.state = self.tmp / "state.json"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("ss", FAKE_SS), ("mv", FAKE_MV), ("tar", FAKE_TAR)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_DOCKER_STATE=str(self.state),
                        FAKE_DOCKER_LOG=str(self.log), FAKE_LOCK=self.lock, FAKE_MV_ONCE=str(self.tmp / "mv-failed"),
                        REAL_MV=shutil.which("mv"), REAL_TAR=shutil.which("tar"))
        self.password = self.tmp / "db-password"
        self.password.write_bytes(PASSWORD.encode() + b"\r\n")  # saved on Windows
        self.config = self.tmp / "iss-api.conf"
        self.set_state()
        self.write_config()

    def set_state(self, **changes):
        state = json.loads(self.state.read_text()) if self.state.exists() else {
            "project": PROJECT, "image_present": False, "listening": False, "port": 8000, "owner": ""}
        state.update({"up_status": [], "config_status": 0})
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def write_config(self, **changes):
        values = {"DB_HOST": "db.example.internal", "DB_SERVICE": "ORCLPDB1", "DB_USER": "API$USR",
                  "DB_PASSWORD_FILE": str(self.password)}
        values.update(changes)
        self.config.write_text("".join(f"{key}={value}\n" for key, value in values.items()))

    def edit(self, name, old, new):  # change a deployed file the way an administrator would
        path = self.dir / name
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, new))

    def record_last_good(self):  # the deployed files become the last good generation
        with tarfile.open(self.dir / ".last-good.tar", "w") as archive:
            for name in GENERATION:
                archive.add(self.dir / name, arcname=name)

    def generation(self):
        return {name: (self.dir / name).read_bytes() for name in GENERATION}

    def run_script(self, *args, **env):
        self.log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(SCRIPT), *args, "--dir", str(self.dir)], env=dict(self.env, **env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def install(self, **env):
        return self.run_script("install", "--config", str(self.config), "--non-interactive", **env)

    def installed(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def ups(self):
        return [call for call in self.calls() if call["args"][5:5 + len(UP)] == UP]

    def assertNothingChanged(self):  # Docker was asked, at most, who owns the project
        self.assertLessEqual({tuple(call["args"]) for call in self.calls()}, {("compose", "version"), tuple(OWNER)})

    def test_install_keeps_the_password_out_of_arguments_and_the_compose_file(self):
        result = self.installed()
        calls = self.calls()
        self.assertIn(["pull", IMAGE], [call["args"] for call in calls])
        for arg in (arg for call in calls for arg in call["args"]):
            self.assertNotIn(PASSWORD, arg)
        compose = (self.dir / "compose.yml").read_text()
        self.assertNotIn(PASSWORD, compose)
        self.assertIn(f"image: {IMAGE}\n", compose)
        self.assertIn('DB1_USER: "API$$USR"', compose)  # $$ is a literal $ to Compose
        self.assertIn('- "127.0.0.1:8000:8000"', compose)
        self.assertIn("secrets: [db_password]", compose)
        secret = self.dir / "secrets" / "db_password"
        self.assertEqual(secret.read_text(), PASSWORD)
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o700)
        settings = (self.dir / "iss-api.env").read_text()
        self.assertEqual(stat.S_IMODE((self.dir / "iss-api.env").stat().st_mode), 0o600)
        self.assertIn("DB_USER=API$USR\n", settings)
        self.assertNotIn("DB_PASSWORD_FILE", settings)
        self.assertEqual((self.dir / ".iss-api").read_text(), f"PROJECT_NAME={PROJECT}\nDIR={self.dir}\n")
        with tarfile.open(self.dir / ".last-good.tar") as archive:
            self.assertEqual(sorted(archive.getnames()), sorted(GENERATION))
            self.assertEqual(archive.extractfile("iss-api.env").read().decode(), settings)
        self.assertFalse((self.dir / ".stage").exists())
        # The check gets the new key on standard input; only its SHA-256 is kept; it is shown once.
        key = [call for call in calls if call["args"][5:9] == CHECK][-1]["stdin"]
        self.assertRegex(key, r"^[A-Za-z0-9_-]{43}$")
        for arg in (arg for call in calls for arg in call["args"]):
            self.assertNotIn(key, arg)
        self.assertIn(f"API_KEY_HASH={hashlib.sha256(key.encode()).hexdigest()}\n", settings)
        self.assertEqual(result.stdout.count(key), 1)
        self.assertIn("Deployment verified: iss-oracle-api-v1.0.0, with a query using the new key", result.stdout)
        for call in calls:
            if call["args"][:1] == ["pull"] or call["args"][5:5 + len(UP)] == UP:
                self.assertEqual(call["lock"], "held", call["args"])

    def test_settings_are_checked_before_docker_is_used(self):
        cases = [({"DB_HOST": "$(touch /tmp/iss-api-test)"}, "DB_HOST="),
                 ({"DB_HOST": "127.0.0.1"}, "the container itself"),
                 ({"DB_USER": "api user"}, "DB_USER="),
                 ({"HTTP_PORT": "70000"}, "HTTP_PORT="),
                 ({"BIND_IP": "everyone"}, "BIND_IP="),
                 ({"API_KEY_HASH": "not-a-hash"}, "API_KEY_HASH=")]
        for changes, message in cases:
            with self.subTest(changes=changes):
                self.write_config(**changes)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertNothingChanged()
                self.assertFalse(self.dir.exists())
        self.write_config(EXTRA="1")
        self.assertIn("unknown key EXTRA", self.install().stderr)
        self.assertFalse(Path("/tmp/iss-api-test").exists())

    def test_an_invalid_compose_file_changes_nothing_and_shows_no_key(self):
        self.installed()
        before = self.generation()
        self.password.write_text("another password")
        self.write_config(HTTP_PORT="8001")
        self.set_state(image_present=True, config_status=1)
        result = self.install()
        self.assertIn("not valid. Nothing was changed", result.stderr)
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.ups(), [])
        result = self.run_script("gen-key")  # a key that was never published is not shown
        self.assertIn("not valid. Nothing was changed", result.stderr)
        self.assertNotIn("New API key", result.stdout)
        self.assertEqual(self.generation(), before)

    def test_rerunning_an_unattended_install_keeps_the_key_and_the_container(self):
        self.installed()
        settings = (self.dir / "iss-api.env").read_text()
        result = self.installed()  # the same --config, which has no API_KEY_HASH
        self.assertEqual((self.dir / "iss-api.env").read_text(), settings)
        self.assertNotIn("New API key", result.stdout)
        self.assertIn("Deployment verified: iss-oracle-api-v1.0.0\n", result.stdout)
        self.assertEqual([call for call in self.calls() if call["args"][5:9] == CHECK][-1]["stdin"], "")
        self.assertEqual(self.ups()[-1]["args"][5:], UP)  # nothing changed: no new container

    def test_a_new_password_reaches_a_new_container(self):
        self.installed()
        self.password.write_text("the rotated password")
        self.installed()
        up = self.ups()[-1]
        self.assertEqual(up["args"][5:], UP + ["--force-recreate"])
        self.assertEqual(up["password"], "the rotated password")
        with tarfile.open(self.dir / ".last-good.tar") as archive:
            self.assertEqual(archive.extractfile("secrets/db_password").read(), b"the rotated password")

    def test_a_failed_first_start_still_shows_the_key_it_saved(self):
        self.set_state(up_status=[1])
        result = self.install()
        self.assertIn("did not become healthy", result.stderr)
        key = re.search(r"^  ([A-Za-z0-9_-]{43})$", result.stdout, re.M).group(1)
        self.assertIn(hashlib.sha256(key.encode()).hexdigest(), (self.dir / "iss-api.env").read_text())

    def test_a_release_that_does_not_start_is_switched_back_to_the_last_good_one(self):
        self.installed()
        self.edit("compose.yml", IMAGE, OLD_IMAGE)  # as if an older release had run
        self.record_last_good()
        last_good = self.generation()
        self.password.write_text("a new password")
        self.write_config(HTTP_PORT="8001")
        self.set_state(up_status=[1, 0])
        result = self.install()
        self.assertIn("the last good release runs again with its settings", result.stderr)
        self.assertEqual(self.generation(), last_good)
        self.assertIn("HTTP_PORT=8001\n", (self.dir / "iss-api.env.failed").read_text())
        ups = self.ups()
        self.assertEqual(len(ups), 2)
        self.assertIn(IMAGE, ups[0]["compose_file"])
        self.assertEqual(ups[0]["password"], "a new password")
        self.assertIn(OLD_IMAGE, ups[1]["compose_file"])
        self.assertEqual(ups[1]["args"][5:], UP + ["--force-recreate"])
        self.assertEqual(ups[1]["password"], PASSWORD)

    def test_an_edited_setting_that_fails_is_replaced_by_the_running_one(self):
        self.installed()
        self.edit("iss-api.env", "HTTP_PORT=8000", "HTTP_PORT=8001")  # edit, then install, as the README says
        self.set_state(up_status=[1, 0])
        result = self.run_script("install")
        self.assertIn("runs again with its settings", result.stderr)
        self.assertIn("HTTP_PORT=8000\n", (self.dir / "iss-api.env").read_text())
        self.assertIn('"127.0.0.1:8000:8000"', (self.dir / "compose.yml").read_text())
        self.assertIn("HTTP_PORT=8001\n", (self.dir / "iss-api.env.failed").read_text())

    def test_a_password_change_that_fails_is_switched_back(self):
        self.installed()
        self.password.write_text("a password that fails")
        self.set_state(up_status=[1, 0])
        result = self.install()  # compose.yml stays the same: only the password differs
        self.assertIn("runs again with its settings", result.stderr)
        self.assertEqual((self.dir / "secrets" / "db_password").read_text(), PASSWORD)

    def test_a_switch_back_that_fails_says_so(self):
        self.installed()
        self.edit("iss-api.env", "WORKERS=2", "WORKERS=3")
        self.set_state(up_status=[1, 1])
        result = self.run_script("install")
        self.assertIn("the last good one did not start either", result.stderr)
        self.assertNotIn("runs again", result.stderr)

    def test_a_failed_publication_puts_the_previous_files_back(self):
        self.installed()
        before = self.generation()
        self.password.write_text("a new password")
        self.write_config(HTTP_PORT="8001")
        result = self.install(FAKE_MV_FAIL="compose.yml")  # the password and settings moved, then this failed
        self.assertIn("Publishing the new files failed. Nothing was changed", result.stderr)
        self.assertEqual(self.generation(), before)
        self.assertEqual(self.ups(), [])

    def test_a_first_install_whose_publication_fails_leaves_no_key_behind(self):
        result = self.install(FAKE_MV_FAIL="compose.yml")
        self.assertIn("Publishing the new files failed. Nothing was changed", result.stderr)
        self.assertNotIn("New API key", result.stdout)
        for name in GENERATION:
            self.assertFalse((self.dir / name).exists(), name)
        result = self.installed()  # a plain retry issues, uses and shows a key that works
        key = [call for call in self.calls() if call["args"][5:9] == CHECK][-1]["stdin"]
        self.assertRegex(key, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(result.stdout.count(key), 1)
        self.assertIn(hashlib.sha256(key.encode()).hexdigest(), (self.dir / "iss-api.env").read_text())

    def test_a_failed_snapshot_keeps_the_previous_one(self):
        self.installed()
        snapshot = (self.dir / ".last-good.tar").read_bytes()
        self.write_config(HTTP_PORT="8001")
        result = self.install(FAKE_TAR_FAIL="1")
        self.assertIn("recording it as the last good one failed", result.stderr)
        self.assertEqual((self.dir / ".last-good.tar").read_bytes(), snapshot)
        self.assertFalse((self.dir / ".last-good.tar.new").exists())

    def test_gen_key_rotates_only_the_key(self):
        self.installed()
        self.edit("compose.yml", IMAGE, OLD_IMAGE)
        self.record_last_good()
        before = (self.dir / "iss-api.env").read_text()
        result = self.run_script("gen-key")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(OLD_IMAGE, (self.dir / "compose.yml").read_text())
        self.assertFalse(any(call["args"][:1] == ["pull"] for call in self.calls()))
        key = [call for call in self.calls() if call["args"][5:9] == CHECK][-1]["stdin"]
        after = (self.dir / "iss-api.env").read_text()
        self.assertIn(hashlib.sha256(key.encode()).hexdigest(), after)
        self.assertEqual([line for line in after.splitlines() if not line.startswith("API_KEY_HASH=")],
                         [line for line in before.splitlines() if not line.startswith("API_KEY_HASH=")])
        self.assertEqual(result.stdout.count(key), 1)
        # changes that install has not applied, or settings that fail the checks: nothing happens
        for old, new in (("BIND_IP=127.0.0.1", "BIND_IP=0.0.0.0"), ("DB_USER=API$USR", 'DB_USER=bad"user')):
            with self.subTest(new=new):
                self.edit("iss-api.env", old, new)
                generation = self.generation()
                result = self.run_script("gen-key")
                self.assertIn("changes that install has not applied", result.stderr)
                self.assertNothingChanged()
                self.assertEqual(self.generation(), generation)
                self.edit("iss-api.env", new, old)

    def test_project_name_cannot_change_or_be_shared(self):
        self.installed()
        self.edit("iss-api.env", f"PROJECT_NAME={PROJECT}", "PROJECT_NAME=another")
        for args in (["install"], ["stop"], ["gen-key"]):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertIn("PROJECT_NAME cannot change", result.stderr)
                self.assertNothingChanged()
        self.edit("iss-api.env", "PROJECT_NAME=another", f"PROJECT_NAME={PROJECT}")
        self.set_state(owner="/opt/another-deployment")  # the project already runs from elsewhere
        for args in (["install"], ["stop"], ["uninstall", "--confirm", PROJECT]):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertIn("already runs from /opt/another-deployment", result.stderr)
                self.assertNothingChanged()
        self.assertTrue((self.dir / "compose.yml").exists())

    def test_only_its_own_directory_is_touched(self):
        self.dir.mkdir()
        (self.dir / "keep.txt").write_text("not ours")
        result = self.install()
        self.assertIn("not empty and not a deployment of this script", result.stderr)
        self.assertNothingChanged()
        result = self.run_script("uninstall", "--confirm", PROJECT)
        self.assertIn("not a deployment of this script", result.stderr)
        self.assertNothingChanged()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["keep.txt"])

    def test_uninstall_needs_the_project_name_and_removes_only_its_files(self):
        self.installed()
        for args in ([], ["--confirm", "wrong"]):
            with self.subTest(args=args):
                result = self.run_script("uninstall", *args)
                self.assertIn("Nothing was changed", result.stderr)
                self.assertNothingChanged()
                self.assertTrue((self.dir / "compose.yml").exists())
        (self.dir / "notes.txt").write_text("the administrator's")
        (self.dir / "secrets" / "old-password").write_text("the administrator's too")
        result = self.run_script("uninstall", "--purge", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(str(p.relative_to(self.dir)) for p in self.dir.rglob("*")),
                         ["notes.txt", "secrets", "secrets/old-password"])
        down = [call for call in self.calls() if "down" in call["args"]]
        self.assertEqual([call["args"][5:] for call in down], [["down", "--remove-orphans", "--rmi", "all"]])
        self.assertEqual(down[0]["lock"], "held")
        shutil.rmtree(self.dir)
        self.installed()
        self.assertEqual(self.run_script("uninstall", "--confirm", PROJECT).returncode, 0)
        self.assertFalse(self.dir.exists())

    def test_a_held_lock_stops_every_change(self):
        self.installed()
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for args in (["install"], ["start"], ["stop"], ["restart"], ["gen-key"], ["uninstall", "--confirm", PROJECT]):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertIn("Another iss-api.sh command", result.stderr)
                self.assertNothingChanged()
        self.assertTrue((self.dir / "compose.yml").exists())


if __name__ == "__main__":
    unittest.main()
