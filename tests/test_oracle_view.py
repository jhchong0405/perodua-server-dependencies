"""oracle-view.sh against fakes, and its role SQL on a real throwaway PostgreSQL 16.

The script requires root; run these in the test image (tests/Dockerfile) or skip
them elsewhere. On the App side, a fake Docker CLI answers only the commands the
script uses, fails on anything else, and records each call with its standard
input, its DOCKER_CONFIG and whether the script's directory lock was held. Its
`docker run` of make_user.py prints a users line made from the password it
reads, so a test can match the line with initial-login.txt. Fake ss and curl
answer from the same state; mv and tar run the real programs unless a test asks
one to fail. On the DB side, fake runuser, id and psql record each psql call
with its standard input and answer from a state file. RoleOnPostgresTest runs
the role SQL on a throwaway PostgreSQL 16 cluster (the test image has it) on its
own port and socket. Every password, key and verifier here is made up.
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pty
import pwd
import re
import select
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oracle-view.sh"
TEXT = SCRIPT.read_text()
IMAGE = re.search(r"^IMAGE=(\S+)$", TEXT, re.M).group(1)
RELEASE = re.search(r"^RELEASE=(\S+)$", TEXT, re.M).group(1)
REGISTRY = "perodua-deploy.novutal.com"
REPOSITORY = REGISTRY + "/oracle-view"
OLD_IMAGE = REPOSITORY + ":grey-v0.9.0@sha256:" + "0" * 64
PROJECT = "oracle-view"
VOLUME = "oracle-view_oracle-view-state"
UP = ["up", "--detach", "--wait", "--wait-timeout", "180", "--remove-orphans", "--pull", "never"]
OWNER = ["ps", "--all", "--filter", f"label=com.docker.compose.project={PROJECT}",
         "--format", '{{.Label "com.docker.compose.project.working_dir"}}']
MAKE_USER = "/opt/oracle-view/app/deploy/make_user.py"
ENGINE_API = ("version", "--format", "{{.Server.APIVersion}}")
ROUTES = Path("/etc/perodua-https/routes.conf")
ROUTE = "stgiss.perodua.com.my   /dev/oracle-view/   8797   strip,http"
PUBLISHED = ["users", "initial-login.txt", "oracle-view.env", "compose.yml"]
GENERATION = ["compose.yml", "oracle-view.env"]
REGISTRY_ACCOUNT = ["deploy-user", "made-up-registry-pass"]
HBA_FILE = "/etc/postgresql/16/main/pg_hba.conf"
TEST_PASSWORD = "made-up-rp-password-for-tests"


def users_line(name, role, password):
    """The users line that the fake make_user.py prints for this password."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(f"{name}:{password}".encode()).digest()).decode().rstrip("=")
    return f"{name}:{role}:scrypt:ln=15:r=8:p=1:{digest[:16]}:{digest[16:]}"


def scram_verifier(password, salt=b"made-up-salt-16b", iterations=4096):
    """PostgreSQL's SCRAM-SHA-256 verifier, as deploy/rp_secret.py of the image build makes it."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data):
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


VERIFIER = scram_verifier(TEST_PASSWORD)
STORED_KEY = VERIFIER.split("$")[2].split(":")[0]


FAKE_DOCKER = r'''#!/usr/bin/env python3
import base64, fcntl, hashlib, json, os, sys
IMAGE = ''' + repr(IMAGE) + r'''
REPOSITORY = ''' + repr(REPOSITORY) + r'''
VOLUME = ''' + repr(VOLUME) + r'''
OWNER = ''' + repr(OWNER) + r'''
UP = ''' + repr(UP) + r'''
MAKE_USER = ''' + repr(MAKE_USER) + r'''
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    state = json.load(f)
def lock_state():  # what another oracle-view.sh started now would find
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
def image_id(ref):
    return "id" + hashlib.sha256(ref.encode()).hexdigest()[:10]
entry = {"args": args, "lock": lock_state(), "docker_config": os.environ.get("DOCKER_CONFIG", "")}
out, err, status = "", "", 0
compose = len(args) > 5 and args[:3] == ["compose", "--project-name", "oracle-view"] and args[3] == "--file"
action = args[5:] if compose else None
if args == ["compose", "version"]:
    out = "Docker Compose version v2.40.0\n"
elif args == ["version", "--format", "{{.Server.APIVersion}}"]:
    if state["api_version"]:
        out = state["api_version"] + "\n"
    else:
        err, status = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n", 1
elif args == OWNER:
    out = state["owner"] + "\n" if state["owner"] else ""
elif args[:4] == ["ps", "--all", "--quiet", "--filter"]:
    out = "c0ffee\n" if state["owner"] else ""
elif args == ["rm", "--force", "c0ffee"]:
    state.update(owner="", listening=False)
elif args[:2] == ["network", "rm"]:
    pass
elif args == ["image", "inspect", IMAGE]:
    status = 0 if IMAGE in state["images"] else 1
elif args == ["pull", IMAGE]:
    if state["registry_account"] and state["logged_in"] != state["registry_account"]:
        err, status = "Error response from daemon: unauthorized: authentication required\n", 1
    elif state["pull_error"]:
        err, status = state["pull_error"] + "\n", 1
    else:
        state["images"].append(IMAGE)
elif args == ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]:
    out = "unix:///var/run/docker.sock\n"
elif args[:3] == ["login", "perodua-deploy.novutal.com", "--username"] and args[4:] == ["--password-stdin"]:
    entry["stdin"] = sys.stdin.read()
    if [args[3], entry["stdin"]] == state["registry_account"]:
        state["logged_in"] = [args[3], entry["stdin"]]
        out = "Login Succeeded\n"
    else:
        err, status = "Error response from daemon: unauthorized: incorrect username or password\n", 1
elif args[:2] == ["run", "--rm"] and MAKE_USER in args:
    entry["stdin"] = sys.stdin.read()
    password = entry["stdin"][:-1] if entry["stdin"].endswith("\n") else entry["stdin"]
    name, role = args[-1], args[args.index("--role") + 1]
    digest = base64.urlsafe_b64encode(hashlib.sha256(f"{name}:{password}".encode()).digest()).decode().rstrip("=")
    status = state["make_user_status"]
    out = "" if status else f"{name}:{role}:scrypt:ln=15:r=8:p=1:{digest[:16]}:{digest[16:]}\n"
elif compose and action == ["config", "--quiet"]:
    entry["compose_file"] = open(args[4]).read()
    status = state["config_status"]
elif compose and action == ["ps", "--quiet", "oracle-view"]:
    out = "c0ffee\n" if state["listening"] else ""
elif compose and action == ["ps", "--all"]:
    out = "NAME          STATUS\noracle-view   Up 2 minutes (healthy)\n"
elif compose and action == ["logs", "--tail", "40", "oracle-view"]:
    pass
elif compose and action == ["down", "--remove-orphans"]:
    state.update(listening=False, owner="")
elif compose and action in (UP, UP + ["--force-recreate"]):
    directory = os.path.dirname(args[4])
    entry["compose_file"] = open(args[4]).read()
    entry["users"] = open(os.path.join(directory, "users")).read()  # what the new container reads
    status = state["up_status"].pop(0) if state["up_status"] else 0
    if status == 0:
        state.update(listening=True, owner=directory, volume=True)
elif args == ["volume", "inspect", VOLUME]:
    status = 0 if state["volume"] else 1
elif args == ["volume", "rm", VOLUME]:
    if state["volume_rm_error"]:
        err, status = state["volume_rm_error"] + "\n", 1
    else:
        state["volume"] = False
elif args == ["image", "ls", "--quiet", REPOSITORY]:
    out = "".join(image_id(ref) + "\n" for ref in state["images"])
elif args[:3] == ["image", "rm", "--force"] and len(args) == 4:
    if state["image_rm_error"]:
        err, status = state["image_rm_error"] + "\n", 1
    else:
        state["images"] = [ref for ref in state["images"] if image_id(ref) != args[3]]
else:
    sys.exit(f"fake docker: unsupported command {args}")
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
with open(os.environ["FAKE_DOCKER_STATE"], "w") as f:
    json.dump(state, f)
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(status)
'''
# ss -ltnH "sport = :8797"
FAKE_SS = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    state = json.load(f)
if (state["listening"] or state["foreign_listener"]) and sys.argv[-1] == "sport = :8797":
    print("LISTEN 0 4096 127.0.0.1:8797 0.0.0.0:*")
'''
# curl -fsS --max-time 5 --noproxy * http://127.0.0.1:8797/healthz
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    state = json.load(f)
if sys.argv[-1] == "http://127.0.0.1:8797/healthz" and state["listening"] and state["healthz"] == "ok":
    print("ok")
    sys.exit(0)
sys.exit(7)
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


def lock_path(name):  # where oracle-view.sh keeps a lock
    base = "/run/lock" if os.path.isdir("/run/lock") else "/run"
    return f"{base}/oracle-view-{name}.lock"


def dir_lock(directory):
    return lock_path(hashlib.sha256(os.path.realpath(directory).encode()).hexdigest()[:16])


def needs_root_on_linux(cls):
    return unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir(),
                               "needs root on Linux (the test image)")(cls)


@needs_root_on_linux
class OracleViewAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = Path(os.path.realpath(self.tmp)) / "deploy"
        self.lock = dir_lock(self.dir)
        for path in (self.lock, lock_path(f"project-{PROJECT}")):
            self.addCleanup(Path(path).unlink, missing_ok=True)
        self.log = self.tmp / "docker.log"
        self.state = self.tmp / "state.json"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("ss", FAKE_SS), ("curl", FAKE_CURL), ("mv", FAKE_MV),
                           ("tar", FAKE_TAR)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_DOCKER_STATE=str(self.state),
                        FAKE_DOCKER_LOG=str(self.log), FAKE_LOCK=self.lock, FAKE_MV_ONCE=str(self.tmp / "mv-failed"),
                        REAL_MV=shutil.which("mv"), REAL_TAR=shutil.which("tar"))
        self.env.pop("DOCKER_CONFIG", None)
        self.config = self.tmp / "oracle-view.conf"
        self.set_state()

    def set_state(self, **changes):
        state = json.loads(self.state.read_text()) if self.state.exists() else {
            "images": [], "registry_account": None, "logged_in": None, "pull_error": "", "listening": False,
            "foreign_listener": False, "owner": "", "make_user_status": 0, "healthz": "ok", "volume": False,
            "api_version": "1.51", "volume_rm_error": "", "image_rm_error": ""}
        state.update({"up_status": [], "config_status": 0})
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def write_config(self, **values):
        self.config.write_text("".join(f"{key}={value}\n" for key, value in values.items()))

    def run_script(self, *args, **env):
        self.log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(SCRIPT), *args, "--dir", str(self.dir)], env=dict(self.env, **env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def install(self, *args, **env):
        if self.config.exists():
            args = ("--config", str(self.config)) + args
        return self.run_script("install", "--non-interactive", *args, **env)

    def installed(self, *args):
        result = self.install(*args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def ups(self):
        return [call for call in self.calls() if call["args"][5:5 + len(UP)] == UP]

    def make_user_runs(self):
        return [call for call in self.calls() if call["args"][:2] == ["run", "--rm"]]

    def assertNothingChanged(self):  # Docker was asked, at most, its versions and who owns the project
        self.assertLessEqual({tuple(call["args"]) for call in self.calls()},
                             {("compose", "version"), ENGINE_API, tuple(OWNER)})

    def published(self):
        return {name: (self.dir / name).read_bytes() for name in PUBLISHED if (self.dir / name).exists()}

    def generation(self):
        return {name: (self.dir / name).read_bytes() for name in GENERATION}

    def record_last_good(self):  # the deployed files become the last good generation
        with tarfile.open(self.dir / ".last-good.tar", "w") as archive:
            for name in GENERATION:
                archive.add(self.dir / name, arcname=name)

    def edit(self, name, old, new):  # change a deployed file the way an administrator would
        path = self.dir / name
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, new))

    def passwords(self):
        text = (self.dir / "initial-login.txt").read_text()
        return dict(re.findall(r"^User name: (\S+) \(role \S+, made [^)]+\)\nPassword:  (\S+)$", text, re.M))

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def converse(self, answers, *args):
        """Run on a pseudo-terminal, answer each prompt in turn, return (exit code, output)."""
        pid, master = pty.fork()
        if pid == 0:
            os.execvpe("bash", ["bash", str(SCRIPT), *args], self.env)
        output, cursor, status = b"", 0, None
        deadline = time.monotonic() + 60

        def read_more(wait=0.05):
            nonlocal output
            if not select.select([master], [], [], wait)[0]:
                return True
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return False
            output += chunk
            return bool(chunk)

        try:
            for prompt, answer in answers:
                while prompt not in output[cursor:]:
                    if time.monotonic() > deadline or not read_more():
                        self.fail(f"Expected prompt {prompt!r}; received {output.decode(errors='replace')}")
                cursor = output.index(prompt, cursor) + len(prompt)
                os.write(master, answer + b"\n")
            while status is None and time.monotonic() < deadline:
                alive = read_more()
                done, result = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = result
                elif not alive:
                    time.sleep(0.05)
            while read_more(0.2) and select.select([master], [], [], 0.2)[0]:
                pass
        finally:
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)
        self.assertIsNotNone(status, "Script did not finish: " + output.decode(errors="replace"))
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")

    # ------------------------------------------------------------------ the image
    def test_the_pin_is_one_digest_of_the_private_registry(self):
        self.assertEqual(len(re.findall(r"^IMAGE=", TEXT, re.M)), 1)
        self.assertRegex(IMAGE, r"^perodua-deploy\.novutal\.com/oracle-view:grey-v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")
        self.assertEqual(RELEASE, "oracle-view-" + IMAGE.split("@")[0].split(":")[1])
        self.installed()
        calls = self.calls()
        self.assertEqual([call["args"] for call in calls if call["args"][:1] == ["pull"]], [["pull", IMAGE]])
        compose = (self.dir / "compose.yml").read_text()
        self.assertIn(f"    image: {IMAGE}\n", compose)
        self.assertIn("    pull_policy: never\n", compose)
        self.assertEqual([up["args"][5:5 + len(UP)] for up in self.ups()], [UP])  # up never pulls
        for run in self.make_user_runs():
            self.assertIn(IMAGE, run["args"])
        self.installed()  # the image is on the server now: no second pull
        self.assertNotIn("pull", [call["args"][0] for call in self.calls()])

    def test_registry_login_without_a_terminal(self):
        self.set_state(registry_account=REGISTRY_ACCOUNT)
        for args, message in ((["--non-interactive"], "The registry needs a login: run docker login perodua-deploy.novutal.com first"),
                              ([], "The registry login needs a terminal (or run docker login perodua-deploy.novutal.com first)")):
            with self.subTest(args=args):
                result = self.run_script("install", *args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertNotIn("login", [call["args"][0] for call in self.calls()])
                self.assertEqual(self.make_user_runs(), [])
                self.assertEqual(self.ups(), [])
                for name in PUBLISHED:
                    self.assertFalse((self.dir / name).exists(), name)

    def test_registry_login_on_a_terminal_goes_through_stdin_to_a_throwaway_config(self):
        self.set_state(registry_account=REGISTRY_ACCOUNT)
        code, output = self.converse([(b"Registry username (blank cancels): ", REGISTRY_ACCOUNT[0].encode()),
                                      (b"Registry password (hidden; blank cancels): ", REGISTRY_ACCOUNT[1].encode())],
                                     "install", "--dir", str(self.dir))
        self.assertEqual(code, 0, output)
        self.assertIn(f"Installed: {RELEASE}", output)
        self.assertNotIn(REGISTRY_ACCOUNT[1], output)  # no echo
        calls = self.calls()
        logins = [call for call in calls if call["args"][:1] == ["login"]]
        self.assertEqual([call["args"] for call in logins],
                         [["login", REGISTRY, "--username", REGISTRY_ACCOUNT[0], "--password-stdin"]])
        self.assertEqual(logins[0]["stdin"], REGISTRY_ACCOUNT[1])
        config = logins[0]["docker_config"]
        self.assertTrue(config.startswith("/"), config)
        self.assertFalse(Path(config).exists())  # the login is not kept on the server
        self.assertEqual([call["args"] for call in calls if call["args"][:1] == ["pull"]], [["pull", IMAGE]] * 2)
        for call in calls:
            self.assertNotIn(REGISTRY_ACCOUNT[1], " ".join(call["args"]))

    # ------------------------------------------------------------------ the container
    def test_install_runs_the_container_locked_down(self):
        self.installed()
        compose = (self.dir / "compose.yml").read_text()
        for line in ("    container_name: oracle-view\n", "    restart: unless-stopped\n    read_only: true\n",
                     "      - /tmp:size=64m,mode=1777\n", "      - oracle-view-state:/var/lib/oracle-view\n",
                     "      - type: bind\n        source: ./users\n        target: /etc/oracle-view/users\n"
                     "        read_only: true\n        bind:\n          create_host_path: false\n",
                     '      - "127.0.0.1:8797:8797"\n', '      OVB_RP_SSLMODE: "require"\n', '    mem_limit: "1g"\n',
                     '    memswap_limit: "1g"\n', "    pids_limit: 256\n", "    cpus: 1.5\n", "    cap_drop: [ALL]\n",
                     '    security_opt: ["no-new-privileges:true"]\n',
                     '      test: ["CMD", "python", "/opt/oracle-view/app/deploy/healthcheck.py"]\n',
                     "\nvolumes:\n  oracle-view-state:\n"):
            self.assertIn(line, compose)
        self.assertEqual(compose.count("8797:8797"), 1)
        self.assertNotIn("privileged", compose)
        self.assertNotIn("0.0.0.0", compose)
        self.assertEqual(self.mode(self.dir), 0o700)
        self.assertEqual(self.mode(self.dir / "compose.yml"), 0o600)
        self.assertEqual(self.mode(self.dir / "oracle-view.env"), 0o600)
        self.assertEqual([line for line in (self.dir / "oracle-view.env").read_text().splitlines() if not line.startswith("#")],
                         ["OVB_MEM_LIMIT=1g", "OVB_PIDS_LIMIT=256", "OVB_CPUS=1.5", "OVB_RP_SSLMODE=require"])
        self.assertEqual((self.dir / ".oracle-view").read_text(), f"DIR={self.dir}\n")
        with tarfile.open(self.dir / ".last-good.tar") as archive:
            self.assertEqual(sorted(archive.getnames()), sorted(GENERATION))
        self.assertFalse((self.dir / ".stage").exists())
        for call in self.calls():
            if call["args"][:1] == ["pull"] or call["args"][5:5 + len(UP)] == UP:
                self.assertEqual(call["lock"], "held", call["args"])

    def test_settings_are_checked_before_docker_is_used(self):
        cases = [("OVB_MEM_LIMIT", "1 g"), ("OVB_MEM_LIMIT", "128m"), ("OVB_MEM_LIMIT", "1G"),
                 ("OVB_MEM_LIMIT", "$(touch /tmp/oracle-view-test)"), ("OVB_PIDS_LIMIT", "16"),
                 ("OVB_PIDS_LIMIT", "40000"), ("OVB_CPUS", "0"), ("OVB_CPUS", "0.00"), ("OVB_CPUS", "1,5"),
                 ("OVB_CPUS", "100"), ("OVB_RP_SSLMODE", "disable"), ("OVB_RP_SSLMODE", "verify-full")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.write_config(**{key: value})
                result = self.install()
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{key}={value} cannot be used", result.stderr)
                self.assertNothingChanged()
                self.assertFalse(self.dir.exists())
        self.write_config(OVB_EXTRA="1")
        result = self.install()
        self.assertIn("unknown key OVB_EXTRA", result.stderr)
        self.assertNothingChanged()
        self.assertFalse(Path("/tmp/oracle-view-test").exists())

    def test_config_reads_over_the_saved_settings(self):
        self.write_config(OVB_RP_SSLMODE="prefer", OVB_MEM_LIMIT="768m")
        self.installed()
        compose = (self.dir / "compose.yml").read_text()
        self.assertIn('      OVB_RP_SSLMODE: "prefer"\n', compose)
        self.assertIn('    mem_limit: "768m"\n    memswap_limit: "768m"\n', compose)
        self.config.unlink()
        self.installed()  # the saved settings stay
        self.assertIn("OVB_RP_SSLMODE=prefer\n", (self.dir / "oracle-view.env").read_text())
        self.write_config(OVB_CPUS="2")
        self.installed()
        settings = (self.dir / "oracle-view.env").read_text()
        for line in ("OVB_MEM_LIMIT=768m\n", "OVB_PIDS_LIMIT=256\n", "OVB_CPUS=2\n", "OVB_RP_SSLMODE=prefer\n"):
            self.assertIn(line, settings)
        # The settings file of the manual install (deploy/grey) is a valid --config, comments and all.
        self.config.write_text("# Settings of /opt/oracle-view/compose.yml\r\nOVB_MEM_LIMIT=1g\r\nOVB_PIDS_LIMIT=256\r\n"
                               "OVB_CPUS=1.5\r\n# TLS to the RP database\r\nOVB_RP_SSLMODE=require\r\n")
        self.installed()
        self.assertIn('      OVB_RP_SSLMODE: "require"\n', (self.dir / "compose.yml").read_text())

    # ------------------------------------------------------------------ users
    def test_the_first_users_get_passwords_only_in_the_root_file(self):
        result = self.installed()
        users = (self.dir / "users").read_text().splitlines()
        self.assertEqual([line.split(":")[:2] for line in users], [["perodua", "admin"], ["pm", "editor"]])
        info = os.stat(self.dir / "users")
        self.assertEqual((info.st_uid, info.st_gid, self.mode(self.dir / "users")), (10001, 10001, 0o400))
        initial = self.dir / "initial-login.txt"
        self.assertEqual((os.stat(initial).st_uid, self.mode(initial)), (0, 0o600))
        passwords = self.passwords()
        self.assertEqual(sorted(passwords), ["perodua", "pm"])
        self.assertEqual(users, [users_line("perodua", "admin", passwords["perodua"]),
                                 users_line("pm", "editor", passwords["pm"])])
        self.assertNotEqual(passwords["perodua"], passwords["pm"])
        runs = self.make_user_runs()
        self.assertEqual([run["stdin"] for run in runs], [passwords["perodua"] + "\n", passwords["pm"] + "\n"])
        for run in runs:
            options = run["args"][:run["args"].index(IMAGE)]
            for flag in (["--network", "none"], ["--read-only"], ["--cap-drop", "ALL"],
                         ["--security-opt", "no-new-privileges:true"], ["--user", "10001:10001"]):
                self.assertIn(" ".join(flag), " ".join(options))
        for password in passwords.values():
            self.assertGreaterEqual(len(password), 20)
            self.assertNotIn(password, result.stdout + result.stderr)
            for call in self.calls():
                self.assertNotIn(password, " ".join(call["args"]))
        self.assertIn("New users: perodua (admin), pm (editor).", result.stdout)
        self.assertIn(f"sudo rm {self.dir}/initial-login.txt", result.stdout)
        up = self.ups()[-1]
        self.assertEqual(up["args"][5:], UP + ["--force-recreate"])
        self.assertEqual(up["users"], (self.dir / "users").read_text())
        # A second run keeps the users and their passwords, and the container.
        before, users_before = initial.read_text(), (self.dir / "users").read_text()
        result = self.installed()
        self.assertEqual(self.make_user_runs(), [])
        self.assertEqual((initial.read_text(), (self.dir / "users").read_text()), (before, users_before))
        self.assertEqual(self.ups()[-1]["args"][5:], UP)
        self.assertIn("Kept user perodua\nKept user pm\n", result.stdout)
        self.assertIn("still holds first passwords", result.stdout)

    def test_a_changed_users_file_makes_the_container_again(self):
        self.installed()
        (self.dir / "initial-login.txt").unlink()  # the passwords are kept elsewhere now
        perodua = (self.dir / "users").read_text().splitlines()[0]
        viewer = "viewer1:viewer:scrypt:ln=15:r=8:p=1:bWFkZS11cA:bWFkZS11cC1oYXNo"
        # The administrator adds a viewer, without the last line end, and removes pm for a new password.
        (self.dir / "users").write_text(f"{perodua}\n{viewer}")
        result = self.installed()
        self.assertEqual([run["args"][-1] for run in self.make_user_runs()], ["pm"])
        passwords = self.passwords()
        self.assertEqual(sorted(passwords), ["pm"])
        self.assertEqual((self.dir / "users").read_text(),
                         f"{perodua}\n{viewer}\n{users_line('pm', 'editor', passwords['pm'])}\n")
        self.assertEqual(self.ups()[-1]["args"][5:], UP + ["--force-recreate"])
        self.assertIn("New users: pm (editor).", result.stdout)
        # An edit alone, with no new user, also needs a new container.
        with open(self.dir / "users", "a") as users:
            users.write("viewer2:viewer:scrypt:ln=15:r=8:p=1:bWFkZS11cA:bWFkZS11cC1oYXNoMg\n")
        self.installed()
        self.assertEqual(self.make_user_runs(), [])
        self.assertEqual(self.ups()[-1]["args"][5:], UP + ["--force-recreate"])
        self.assertIn("viewer2:viewer", self.ups()[-1]["users"])

    def test_a_first_install_that_stops_before_publishing_leaves_no_password(self):
        for state, message in (({"make_user_status": 1}, "make_user.py of the image failed for perodua. Nothing was changed."),
                               ({"config_status": 1}, "The generated compose file is not valid. Nothing was changed.")):
            with self.subTest(state=state):
                self.set_state(**state)
                result = self.install()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.published(), {})
                self.assertEqual(self.ups(), [])
                self.assertFalse((self.dir / ".stage").exists())
                self.set_state(make_user_status=0)
        self.installed()  # a plain retry makes the users and uses them
        self.assertEqual(sorted(self.passwords()), ["perodua", "pm"])

    # ------------------------------------------------------------------ the staged install and its way back
    def test_a_failed_publication_puts_the_previous_files_back(self):
        self.installed()
        perodua = (self.dir / "users").read_text().splitlines()[0]
        (self.dir / "users").write_text(perodua + "\n")  # pm is made again in the next install
        before = self.published()
        self.write_config(OVB_CPUS="2")
        result = self.install(FAKE_MV_FAIL="compose.yml")  # the users, the passwords and the settings moved, then this failed
        self.assertEqual(result.returncode, 1)
        self.assertIn("Publishing the new files failed. Nothing was changed", result.stderr)
        self.assertEqual(self.published(), before)
        self.assertEqual(self.ups(), [])

    def test_a_release_that_does_not_start_is_switched_back_to_the_last_good_one(self):
        self.installed()
        self.edit("compose.yml", IMAGE, OLD_IMAGE)  # as if an older release had run
        self.record_last_good()
        last_good = self.generation()
        self.write_config(OVB_CPUS="2")
        self.set_state(up_status=[1, 0])
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the last good release runs again with its settings", result.stderr)
        self.assertEqual(self.generation(), last_good)
        self.assertIn("OVB_CPUS=2\n", (self.dir / "oracle-view.env.failed").read_text())
        ups = self.ups()
        self.assertEqual(len(ups), 2)
        self.assertIn(f"image: {IMAGE}\n", ups[0]["compose_file"])
        self.assertIn(f"image: {OLD_IMAGE}\n", ups[1]["compose_file"])
        self.assertEqual(ups[1]["args"][5:], UP + ["--force-recreate"])
        # When the last good one does not start either, the message says so.
        self.set_state(up_status=[1, 1])
        self.edit("oracle-view.env", "OVB_PIDS_LIMIT=256", "OVB_PIDS_LIMIT=300")
        result = self.run_script("install")
        self.assertIn("the last good one did not start either", result.stderr)
        self.assertNotIn("runs again", result.stderr)

    def test_a_first_start_that_fails_keeps_the_users_for_the_retry(self):
        self.set_state(up_status=[1])
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("The tool did not become healthy", result.stderr)
        passwords = self.passwords()
        self.assertEqual(sorted(passwords), ["perodua", "pm"])
        self.assertFalse((self.dir / ".last-good.tar").exists())
        self.assertFalse((self.dir / ".users.sha256").exists())
        self.installed()  # the retry makes no new password, and a new container
        self.assertEqual(self.make_user_runs(), [])
        self.assertEqual(self.passwords(), passwords)
        self.assertEqual(self.ups()[-1]["args"][5:], UP + ["--force-recreate"])
        self.assertEqual((self.dir / ".users.sha256").read_text(),
                         hashlib.sha256((self.dir / "users").read_bytes()).hexdigest() + "\n")

    def test_a_failed_snapshot_keeps_the_previous_one(self):
        self.installed()
        snapshot = (self.dir / ".last-good.tar").read_bytes()
        self.write_config(OVB_CPUS="2")
        result = self.install(FAKE_TAR_FAIL="1")
        self.assertIn("recording it as the last good one failed", result.stderr)
        self.assertEqual((self.dir / ".last-good.tar").read_bytes(), snapshot)
        self.assertFalse((self.dir / ".last-good.tar.new").exists())

    # ------------------------------------------------------------------ checks around the start
    def test_healthz_on_the_server_is_checked(self):
        self.set_state(healthz="error")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("http://127.0.0.1:8797/healthz does not answer ok on this server", result.stderr)
        self.assertIn("does not answer ok", self.run_script("status").stdout)

    def test_a_port_used_by_another_program_stops_before_the_pull(self):
        self.set_state(foreign_listener=True)
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Port 8797 is already used by another program", result.stderr)
        self.assertFalse(any(call["args"][:1] in (["pull"], ["run"]) for call in self.calls()))
        self.assertEqual(self.ups(), [])

    def test_the_manual_install_must_go_first(self):
        self.set_state(owner="/opt/oracle-view")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already runs from /opt/oracle-view", result.stderr)
        self.assertIn("uninstall-app.sh, without --purge", result.stderr)
        self.assertNothingChanged()

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

    def test_status_shows_users_without_their_hashes(self):
        self.installed()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in (f"Release:  {RELEASE} (the pinned image)\n", f"Image:    {IMAGE}\n",
                     "Listens:  127.0.0.1:8797 (this server only)\n",
                     "Users:    perodua (admin), pm (editor)", f"State:    volume {VOLUME}\n",
                     "Health:   http://127.0.0.1:8797/healthz answers ok\n", "still holds first passwords"):
            self.assertIn(line, result.stdout)
        for line in (self.dir / "users").read_text().splitlines():
            self.assertNotIn(line.split(":")[-1], result.stdout)
        for password in self.passwords().values():
            self.assertNotIn(password, result.stdout)

    def test_an_old_docker_engine_stops_the_install_before_any_change(self):
        for api, message in (("1.43", "The Docker engine has API 1.43; the health check of the tool (start_interval) "
                                      "needs API 1.44 (Docker 25) or later. Update Docker first"),
                             ("1.9", "The Docker engine has API 1.9;"),
                             ("", "The Docker engine does not answer (docker version)")):
            with self.subTest(api=api):
                self.set_state(api_version=api)
                result = self.install()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertIn("Nothing was changed.", result.stderr)
                self.assertNothingChanged()
                self.assertFalse(self.dir.exists())
        for api in ("1.44", "1.100", "2.0"):
            with self.subTest(api=api):
                self.set_state(api_version=api)
                self.installed()
                args = [call["args"] for call in self.calls()]
                self.assertEqual(args.count(list(ENGINE_API)), 1)
                if ["pull", IMAGE] in args:  # the first install: the check comes before the pull
                    self.assertLess(args.index(list(ENGINE_API)), args.index(["pull", IMAGE]))

    def test_a_commented_route_is_not_a_route(self):
        saved = ROUTES.read_bytes() if ROUTES.exists() else None
        made = not ROUTES.parent.exists()

        def restore():
            if saved is None:
                ROUTES.unlink(missing_ok=True)
            else:
                ROUTES.write_bytes(saved)
            if made:
                ROUTES.parent.rmdir()
        self.addCleanup(restore)
        ROUTES.parent.mkdir(exist_ok=True)
        # The table of the kit, where the route of the tool is a comment only.
        example = (SCRIPT.parent / "https-routes.conf.example").read_text()
        self.assertIn("# stgiss.perodua.com.my         /dev/oracle-view/   8797   strip,http\n", example)
        ROUTES.write_text(example)
        result = self.installed()
        self.assertIn(f"Next: publish it with https.sh (README.md, Oracle Field Browser). Add this line to {ROUTES}:\n"
                      f"  {ROUTE}\n", result.stdout)
        result = self.run_script("uninstall", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("The route /dev/oracle-view/ stays", result.stdout)
        ROUTES.write_text(example + ROUTE + "\n")
        result = self.installed()
        self.assertNotIn("Next: publish it", result.stdout)
        result = self.run_script("uninstall", "--confirm", PROJECT)
        self.assertIn(f"The route /dev/oracle-view/ stays in {ROUTES} and now has no service", result.stdout)

    # ------------------------------------------------------------------ uninstall
    def test_uninstall_keeps_the_state_the_users_and_the_image_unless_purged(self):
        self.installed()
        users = (self.dir / "users").read_text()
        for args in ([], ["--confirm", "wrong"]):
            with self.subTest(args=args):
                result = self.run_script("uninstall", *args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Nothing was changed", result.stderr)
                self.assertNothingChanged()
                self.assertTrue((self.dir / "compose.yml").exists())
        result = self.run_script("uninstall", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".oracle-view", "users"])
        args = [call["args"] for call in self.calls()]
        self.assertIn(["compose", "--project-name", PROJECT, "--file", str(self.dir / "compose.yml"),
                       "down", "--remove-orphans"], args)
        self.assertFalse(any(a[:2] in (["volume", "rm"], ["image", "rm"]) for a in args))
        self.assertIn(f"Kept: the state volume {VOLUME} (mapping edits, SQL audit, login device key), "
                      f"{self.dir}/users (the users and their password hashes) and the image", result.stdout)
        self.assertEqual(json.loads(self.state.read_text())["volume"], True)
        # A later install uses the users again: no new password.
        self.installed()
        self.assertEqual(self.make_user_runs(), [])
        self.assertEqual((self.dir / "users").read_text(), users)
        self.assertFalse((self.dir / "initial-login.txt").exists())
        # --purge removes the state volume, the users and the images; files of others stay.
        (self.dir / "notes.txt").write_text("the administrator's")
        result = self.run_script("uninstall", "--purge", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["notes.txt"])
        args = [call["args"] for call in self.calls()]
        self.assertIn(["volume", "rm", VOLUME], args)
        self.assertTrue(any(a[:3] == ["image", "rm", "--force"] for a in args))
        state = json.loads(self.state.read_text())
        self.assertEqual((state["volume"], state["images"]), (False, []))
        self.assertIn(f"Removed: the oracle-view container, the files of install, the state volume {VOLUME}, the image ",
                      result.stdout)
        self.assertIn(f", the users file.\n{self.dir} stays: it holds files install did not create", result.stdout)
        (self.dir / "notes.txt").unlink()
        self.installed()
        self.assertEqual(self.run_script("uninstall", "--purge", "--confirm", PROJECT).returncode, 0)
        self.assertFalse(self.dir.exists())

    def test_a_purge_that_fails_says_what_it_removed_and_what_stays(self):
        self.installed()
        self.set_state(volume_rm_error="Error response from daemon: remove oracle-view_oracle-view-state: volume is in use")
        result = self.run_script("uninstall", "--purge", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stdout, r"\nRemoved: the oracle-view container, the files of install, the image id[0-9a-f]+, "
                                        r"the users file\.\n")
        self.assertIn(f"NOT removed: the state volume {VOLUME} (see above).\n", result.stderr)
        self.assertIn("uninstall --purge did not finish. Fix the cause, then run again: sudo bash oracle-view.sh "
                      f"uninstall --purge --confirm {PROJECT} --dir {self.dir}", result.stderr)
        self.assertNotIn("Removed: the oracle-view container, the files of install, the state volume", result.stdout)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".oracle-view"])  # kept for the next run
        state = json.loads(self.state.read_text())
        self.assertEqual((state["volume"], state["images"]), (True, []))
        # An image that cannot be removed is named too; the next run removes what is left.
        self.set_state(volume_rm_error="")
        self.installed()
        self.set_state(image_rm_error="Error response from daemon: conflict: unable to remove repository reference")
        result = self.run_script("uninstall", "--purge", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Removed: the oracle-view container, the files of install, the state volume {VOLUME}, "
                      "the users file.\n", result.stdout)
        self.assertRegex(result.stderr, r"NOT removed: the image id[0-9a-f]+ \(see above\)\.\n")
        self.assertTrue((self.dir / ".oracle-view").exists())
        self.set_state(image_rm_error="")
        result = self.run_script("uninstall", "--purge", "--confirm", PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\nRemoved: the oracle-view container, the files of install, the image id[0-9a-f]+, "
                                        + re.escape(f"{self.dir}.") + "\n")
        self.assertFalse(self.dir.exists())
        self.assertEqual(json.loads(self.state.read_text())["images"], [])

    def test_a_held_lock_stops_every_change(self):
        self.installed()
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for args in (["install"], ["uninstall", "--confirm", PROJECT]):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertIn("Another oracle-view.sh command", result.stderr)
                self.assertNothingChanged()
        self.assertTrue((self.dir / "compose.yml").exists())


FAKE_RUNUSER = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:3] != ["-u", "postgres", "--"]:
    sys.exit(f"fake runuser: unexpected {args}")
os.environ["FAKE_RUNUSER"] = "postgres"
os.execvp(args[3], args[3:])
'''
FAKE_ID = r'''#!/bin/sh
if [ "$*" = "-u postgres" ]; then echo 999; exit 0; fi
exec /usr/bin/id "$@"
'''
FAKE_PSQL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_PG_STATE"]) as f:
    state = json.load(f)
entry = {"args": args, "user": os.environ.get("FAKE_RUNUSER", "")}
command = args[args.index("-c") + 1] if "-c" in args else None
out, err, status = "", "", 0
if command is None:
    entry["stdin"] = script = sys.stdin.read()
    if "-- oracle-view.sh: role\n" in script:
        status = state["role_status"]
        state["role_exists"] = state["role_exists"] or status == 0
    elif "-- oracle-view.sh: hba-check\n" in script:
        out = state["hba_output"]
    elif "-- oracle-view.sh: drop-role\n" in script:
        state["role_exists"] = False
        out = "role oracle_view_rp dropped\n"
    else:
        sys.exit("fake psql: unexpected script")
elif command == "SELECT 1":
    if state["running"]:
        out = "1\n"
    else:
        err, status = 'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed\n', 2
elif command.startswith("SELECT count(*) FROM pg_database WHERE datname = 'perodua_oracle'"):
    out = "1\n" if state["database"] else "0\n"
elif command.startswith("SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"):
    out = "1\n" if state["role_exists"] else "0\n"
elif command == "SHOW ssl":
    out = state["ssl"] + "\n"
elif command == "SHOW hba_file":
    out = state["hba_file"] + "\n"
elif "FROM pg_extension" in command:
    out = state["extensions"] + "\n"
elif "the password is a SCRAM-SHA-256 verifier" in command:
    out = "".join(line + "\n" for line in state["rights"])
else:
    sys.exit(f"fake psql: unexpected command {command}")
with open(os.environ["FAKE_PG_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
with open(os.environ["FAKE_PG_STATE"], "w") as f:
    json.dump(state, f)
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(status)
'''
RIGHTS_OK = ["OK|the password is a SCRAM-SHA-256 verifier", "OK|LOGIN, CONNECTION LIMIT 10",
             "OK|no SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS",
             "OK|read-only by default (default_transaction_read_only=on)", "OK|CONNECT on perodua_oracle",
             "OK|SELECT on every table and view of public (0 without)",
             "OK|INSERT, UPDATE and DELETE on every table of public (0 without)",
             "OK|USAGE and UPDATE on every sequence of public (0 without)", "OK|no TRUNCATE",
             "OK|no CREATE in public", "OK|owns no table, view or sequence (DDL on the Odoo tables fails)"]
HBA_LINE = "host    perodua_oracle    oracle_view_rp    {}/32    scram-sha-256"
BLOCK = "# BEGIN perodua-db-deploy perodua_oracle"  # deploy-db.sh: the first line of its block for the database
DEPLOY_DB = SCRIPT.with_name("deploy-db.sh")
SETTINGS_OF_THE_VERIFIER = ("SET log_statement = 'none';", "SET log_min_duration_statement = -1;",
                            "SET log_min_error_statement = panic;", "SET log_min_duration_sample = -1;",
                            "SET log_transaction_sample_rate = 0;", "SET pg_stat_statements.track_utility = off;")


@needs_root_on_linux
class OracleViewRoleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(Path(lock_path("role")).unlink, missing_ok=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("runuser", FAKE_RUNUSER), ("id", FAKE_ID), ("psql", FAKE_PSQL)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.state = self.tmp / "pg-state.json"
        self.log = self.tmp / "psql.log"
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_PG_STATE=str(self.state),
                        FAKE_PG_LOG=str(self.log))
        self.set_state(running=True, database=True, role_exists=False, role_status=0, ssl="on", extensions="",
                       rights=RIGHTS_OK, hba_output="RESULT: OK - the first matching line uses scram-sha-256.\n",
                       hba_file=HBA_FILE)
        self.verifier = self.tmp / "rp_grey.verifier"
        self.verifier.write_text(VERIFIER + "\n")

    def set_state(self, **changes):
        state = json.loads(self.state.read_text()) if self.state.exists() else {}
        state.update(changes)
        self.state.write_text(json.dumps(state))

    def run_script(self, *args):
        self.log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def role(self, *args):
        return self.run_script("role", "--verifier-file", str(self.verifier), *args)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def scripts(self, kind=None):  # the SQL scripts sent on stdin, of one kind
        return [call for call in self.calls() if "stdin" in call
                and (kind is None or f"-- oracle-view.sh: {kind}\n" in call["stdin"])]

    def test_the_verifier_file_is_checked_before_postgres_changes(self):
        password = "xQ3vT9made-up-password-for-tests-only0Zk-_A"  # the form of rp_grey.password
        cases = {"a password": (password + "\n", "never takes the password itself"),
                 "an md5 hash": ("md5" + "0" * 32 + "\n", "does not hold a SCRAM-SHA-256 verifier"),
                 "a short key": (VERIFIER[:-6] + "=\n", "does not hold a SCRAM-SHA-256 verifier"),
                 "a quote": (VERIFIER.replace("4096:", "4096:'", 1) + "\n", "does not hold a SCRAM-SHA-256 verifier"),
                 "two lines": (VERIFIER + "\n" + VERIFIER + "\n", "must hold one line"),
                 "the role.sql of the bundle": (f"CREATE ROLE oracle_view_rp PASSWORD '{VERIFIER}';\n",
                                                "does not hold a SCRAM-SHA-256 verifier"),
                 "an empty file": ("", "does not hold a SCRAM-SHA-256 verifier")}
        for name, (text, message) in cases.items():
            with self.subTest(name=name):
                self.verifier.write_text(text)
                result = self.role()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.scripts(), [])
                if text.strip():
                    self.assertNotIn(text.strip().splitlines()[0], result.stdout + result.stderr)
        result = self.run_script("role", "--verifier-file", str(self.tmp / "missing"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a readable file", result.stderr)
        self.assertEqual(self.scripts(), [])

    def test_the_role_sql_gets_the_verifier_on_standard_input_only(self):
        self.verifier.write_bytes(VERIFIER.encode() + b"\r\n")  # saved on Windows
        result = self.role()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        (call,) = self.scripts("role")
        self.assertEqual(call["user"], "postgres")
        args = call["args"]
        for pair in (["-h", "/var/run/postgresql"], ["-p", "5432"], ["-d", "postgres"], ["-v", "ON_ERROR_STOP=1"],
                     ["-f", "-"]):
            self.assertIn(" ".join(pair), " ".join(args))
        sql = call["stdin"]
        self.assertTrue(sql.startswith(f"\\set ovb_verifier '{VERIFIER}'\n"), sql[:80])
        self.assertEqual(sql.count(STORED_KEY), 1)
        for each in self.calls():
            self.assertNotIn(STORED_KEY, " ".join(each["args"]))
        self.assertNotIn(STORED_KEY, result.stdout + result.stderr)
        # Out of the server log and the error text, before each statement with the verifier:
        # at the start of the session, and again after each \connect.
        start = sql[:sql.index("\\if :ovb_old\n")]
        for setting in SETTINGS_OF_THE_VERIFIER:
            self.assertIn(setting, start)
        for statement in ("CREATE ROLE oracle_view_rp LOGIN PASSWORD :'ovb_verifier' CONNECTION LIMIT 10;",
                          "ALTER ROLE oracle_view_rp LOGIN PASSWORD :'ovb_verifier' CONNECTION LIMIT 10;",
                          "DROP ROLE oracle_view_ro;"):  # the session after \connect postgres, while the verifier is set
            at = sql.index(statement)
            since_connect = sql[sql.rfind("\\connect", 0, at) + 1:at]
            for setting in SETTINGS_OF_THE_VERIFIER:
                self.assertIn(setting, since_connect)
            for setting in ("\\set VERBOSITY terse\n", "\\set SHOW_CONTEXT never\n"):
                self.assertLess(sql.index(setting), at)
            self.assertLess(at, sql.index("\\unset ovb_verifier\n"))
        # The rights of the owner decision of 2026-11-06.
        for line in ("ALTER ROLE oracle_view_rp NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;",
                     "ALTER ROLE oracle_view_rp SET default_transaction_read_only = on;",
                     "ALTER ROLE oracle_view_rp SET statement_timeout = '30s';",
                     "ALTER ROLE oracle_view_rp SET idle_in_transaction_session_timeout = '60s';",
                     "GRANT CONNECT, TEMPORARY ON DATABASE perodua_oracle TO oracle_view_rp;",
                     "\\connect perodua_oracle\nGRANT USAGE ON SCHEMA public TO oracle_view_rp;",
                     "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO oracle_view_rp;",
                     "GRANT SELECT, USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public TO oracle_view_rp;",
                     "IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oracle_view_rp",
                     "IN SCHEMA public GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO oracle_view_rp",
                     "DROP ROLE oracle_view_ro;"):
            self.assertIn(line, sql)
        statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        for word in ("TRUNCATE", "ALTER SYSTEM", "pg_reload_conf", "OWNER TO", "GRANT CREATE", "GRANT ALL"):
            self.assertNotIn(word, statements)
        self.assertIsNone(re.search(r"(?<!NO)SUPERUSER", statements))
        self.assertIn("OK   the password is a SCRAM-SHA-256 verifier\n", result.stdout)
        self.assertIn("The role is ready", result.stdout)
        self.assertIn(f"Delete your copy of the verifier file: rm {self.verifier}", result.stdout)

    def test_a_missing_right_stops_before_the_pg_hba_check(self):
        for rights, message in ((RIGHTS_OK[:8] + ["FAIL|no TRUNCATE"] + RIGHTS_OK[9:], "does not have the rights above"),
                                (RIGHTS_OK[4:], "unexpected answer")):
            with self.subTest(message=message):
                self.set_state(rights=rights)
                result = self.role()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.scripts("hba-check"), [])

    def test_the_pg_hba_check_prints_the_line_and_never_edits_the_file(self):
        self.set_state(role_exists=True, hba_output="RESULT: NOT OK - no matching line.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.scripts("role"), [])
        (call,) = self.scripts("hba-check")
        self.assertIn("-v ovb_app=192.168.23.13", " ".join(call["args"]))
        self.assertIn("(:'ovb_app'::inet & netmask::inet)", call["stdin"])
        self.assertIn("'perodua_oracle' = ANY (database)", call["stdin"])
        self.assertIn("'oracle_view_rp' = ANY (user_name)", call["stdin"])
        for text in ("This command never edits pg_hba.conf",
                     f"  1. Back up the file:  sudo cp -p {HBA_FILE} {HBA_FILE}.before-oracle-view\n",
                     "       " + HBA_LINE.format("192.168.23.13") + "\n",
                     "  3. Reload (no restart):  sudo -u postgres psql -X -p 5432 -d postgres -c 'SELECT pg_reload_conf()'\n",
                     "  4. Check again:  sudo bash oracle-view.sh role --check\n",
                     f"Way back: sudo cp -p {HBA_FILE}.before-oracle-view {HBA_FILE}"):
            self.assertIn(text, result.stdout)
        self.assertNotIn("The role is ready", result.stdout)
        for each in self.calls():  # the script reloads nothing and changes no file of PostgreSQL
            for word in ("pg_reload_conf", "ALTER SYSTEM", "COPY ", "pg_write", "lo_export"):
                self.assertNotIn(word, " ".join(each["args"]) + each.get("stdin", ""))
        self.set_state(hba_output="RESULT: CHECK BY HAND - a line above the first matching line has a rule.\n")
        result = self.run_script("role", "--check", "--app-ip", "10.20.30.40", "--pg-port", "5433")
        self.assertEqual(result.returncode, 4)
        self.assertIn('Read the lines that the list marks "check by hand"', result.stdout)
        self.assertIn("    " + HBA_LINE.format("10.20.30.40") + "\n", result.stdout)
        self.assertIn("sudo bash oracle-view.sh role --check --app-ip 10.20.30.40 --pg-port 5433\n", result.stdout)
        (call,) = self.scripts("hba-check")
        self.assertIn("-p 5433", " ".join(call["args"]))
        self.assertIn("-v ovb_app=10.20.30.40", " ".join(call["args"]))
        self.set_state(hba_output="RESULT: OK - the first matching line uses scram-sha-256.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("The role is ready", result.stdout)
        self.set_state(hba_output="")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("gave no result", result.stderr)

    def test_the_block_of_deploy_db_gets_the_line_above_it(self):
        hba = self.tmp / "pg_hba.conf"
        hba.write_text(f"# PostgreSQL Client Authentication\n\n{BLOCK}\nhost \"perodua_oracle\" all 0.0.0.0/0 reject\n"
                       "# END perodua-db-deploy perodua_oracle\nlocal all postgres peer\n")
        self.set_state(role_exists=True, hba_file=str(hba), hba_output="RESULT: NOT OK - no matching line.\n")
        note = (f"NOTE: {hba} holds the block of deploy-db.sh for perodua_oracle (line 3). Each later\n"
                "deploy-db.sh run writes that block again at the top of the file, above the line of oracle_view_rp,\n"
                "and deletes the line when it is inside the block. After each deploy-db.sh run, run:\n"
                "    sudo bash oracle-view.sh role --check\n"
                f"and move the line of oracle_view_rp back above the line \"{BLOCK}\".\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn(f'  2. Add this line above line 3, "{BLOCK}": a line inside or below that block of deploy-db.sh '
                      "does not work:\n       " + HBA_LINE.format("192.168.23.13") + "\n", result.stdout)
        self.assertIn(note, result.stdout)
        self.set_state(hba_output="RESULT: CHECK BY HAND - a line above the first matching line has a rule.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 4)
        self.assertIn(f'add this line above them, and above line 3, "{BLOCK}"', result.stdout)
        self.assertIn(note, result.stdout)
        self.set_state(hba_output="RESULT: OK - the first matching line uses scram-sha-256.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(note, result.stdout)  # also when the line is above the block now
        self.assertIn("The role is ready", result.stdout)
        # The block of another database, or none: no note.
        for text in (hba.read_text().replace("perodua_oracle", "perodua_oracle2"), "local all postgres peer\n"):
            with self.subTest(text=text):
                hba.write_text(text)
                result = self.run_script("role", "--check")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("NOTE:", result.stdout)

    def test_a_pg_hba_conf_with_errors_needs_a_check_by_hand(self):
        self.set_state(role_exists=True, hba_output="RESULT: CHECK BY HAND - pg_hba.conf has lines with errors (the last "
                                                    "list). PostgreSQL does not load such a file.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 4)
        self.assertIn("PostgreSQL does not load a pg_hba.conf with errors, so this check cannot tell which lines it uses.\n"
                      "  1. Correct the lines of the last list, in the files that it names.\n", result.stdout)
        self.assertNotIn('Read the lines that the list marks "check by hand"', result.stdout)
        self.assertNotIn("The role is ready", result.stdout)

    def test_role_check_needs_the_role(self):
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist yet: run role --verifier-file FILE first", result.stderr)
        self.assertEqual(self.scripts(), [])

    def test_tls_off_and_extensions_that_write_are_reported_not_changed(self):
        self.set_state(ssl="off", extensions="dblink, postgres_fdw")
        result = self.role()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ssl = off\nWARNING: ssl is not on.", result.stdout)
        self.assertIn("OVB_RP_SSLMODE=prefer", result.stdout)
        self.assertIn("perodua_oracle has: dblink, postgres_fdw\n", result.stdout)
        self.assertIn("This command changes no right", result.stdout)
        for each in self.calls():
            self.assertNotIn("REVOKE", " ".join(each["args"]) + each.get("stdin", ""))
        self.set_state(ssl="on", extensions="")
        result = self.role()
        self.assertNotIn("WARNING", result.stdout)
        self.assertIn("none: perodua_oracle has no dblink and no postgres_fdw\n", result.stdout)

    def test_postgres_and_the_database_are_checked_first(self):
        for state, message in (({"running": False}, "PostgreSQL does not answer on port 5432"),
                               ({"running": True, "database": False}, "There is no database perodua_oracle")):
            with self.subTest(state=state):
                self.set_state(**state)
                for args in (["role", "--verifier-file", str(self.verifier)], ["drop-role", "--confirm", "oracle_view_rp"]):
                    result = self.run_script(*args)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(self.scripts(), [])

    def test_usage_errors(self):
        for args in (["role"], ["role", "--check", "--verifier-file", str(self.verifier)],
                     ["role", "--check", "--dir", "/opt/x"], ["role", "--check", "--app-ip", "192.168.23"],
                     ["role", "--check", "--app-ip", "host.example"], ["role", "--check", "--pg-port", "70000"],
                     ["drop-role", "--verifier-file", str(self.verifier)], ["install", "--check"],
                     ["status", "--purge"], ["frobnicate"], []):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.calls(), [])

    def test_drop_role_needs_the_role_name(self):
        for args in ([], ["--confirm", "oracle-view"]):
            with self.subTest(args=args):
                result = self.run_script("drop-role", *args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Nothing was changed", result.stderr)
                self.assertEqual(self.scripts(), [])
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stderr)
        (call,) = self.scripts("drop-role")
        self.assertEqual(call["user"], "postgres")
        sql = call["stdin"]
        steps = ["ALTER ROLE oracle_view_rp NOLOGIN;", "WHERE usename = 'oracle_view_rp'", "\\connect perodua_oracle",
                 "DROP OWNED BY oracle_view_rp;", "\\connect postgres", "DROP ROLE oracle_view_rp;"]
        self.assertEqual([sql.index(step) for step in steps], sorted(sql.index(step) for step in steps))
        self.assertIn("pg_terminate_backend", sql)
        self.assertIn(f"stays in {HBA_FILE}: remove it by hand", result.stdout)
        self.assertIn("SELECT pg_reload_conf()", result.stdout)

    def test_a_held_lock_stops_role_and_drop_role(self):
        fd = os.open(lock_path("role"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for args in (["role", "--verifier-file", str(self.verifier)], ["drop-role", "--confirm", "oracle_view_rp"]):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Another oracle-view.sh role or drop-role command is running", result.stderr)
                self.assertEqual(self.calls(), [])


PG_BIN = Path("/usr/lib/postgresql/16/bin")


def has_postgres_user():
    try:
        pwd.getpwnam("postgres")
        return True
    except KeyError:
        return False


def deploy_db_hba_writer():
    """The Python program with which deploy-db.sh writes its block at the top of pg_hba.conf."""
    found = re.search(r'python3 - "\$HBA" "\$DB_NAME" "\$DB_USER" "\$APP_CIDR" "\$STAGING" <<\'PY\'\n(.*?)\nPY\n',
                      DEPLOY_DB.read_text(), re.S)
    return found.group(1) if found else None


class PostgresCluster:
    """A throwaway PostgreSQL 16 cluster for one test class, on its own port, with its server log."""
    PORT = None
    SERVER_OPTIONS = ""
    APP = "192.0.2.13"  # a documentation address: no line of the cluster lets it in at first

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.chown(cls.tmp, "postgres", "postgres")
        cls.data = cls.tmp / "data"
        cls.server_log = cls.tmp / "server.log"
        os.makedirs("/var/run/postgresql", exist_ok=True)
        shutil.chown("/var/run/postgresql", "postgres", "postgres")
        cls.as_postgres([str(PG_BIN / "initdb"), "-D", str(cls.data), "-U", "postgres", "--auth-local=peer",
                         "--auth-host=scram-sha-256", "-E", "UTF8", "--locale=C"])
        cls.hba = cls.data / "pg_hba.conf"
        cls.hba_lines = ["local all postgres peer", "local all all peer", "host all all ::1/128 scram-sha-256",
                         "host all all 127.0.0.1/32 scram-sha-256"]
        cls.write_hba(cls.hba_lines)
        cls.as_postgres([str(PG_BIN / "pg_ctl"), "-D", str(cls.data), "-l", str(cls.server_log), "-w", "-o",
                         f"-p {cls.PORT} -k /var/run/postgresql -c listen_addresses=127.0.0.1 -c log_statement=all "
                         + cls.SERVER_OPTIONS, "start"])
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.addClassCleanup(cls.as_postgres, [str(PG_BIN / "pg_ctl"), "-D", str(cls.data), "-m", "immediate", "stop"])
        cls.sql("postgres", "CREATE ROLE odoo LOGIN")
        cls.sql("postgres", "CREATE DATABASE perodua_oracle OWNER odoo")
        cls.sql("postgres", "CREATE ROLE oracle_view_ro LOGIN")
        cls.sql("perodua_oracle", "SET ROLE odoo; CREATE TABLE res_partner (id serial PRIMARY KEY, name varchar);"
                " INSERT INTO res_partner (name) VALUES ('made-up partner');"
                " CREATE VIEW partner_names AS SELECT name FROM res_partner")
        cls.sql("perodua_oracle", "GRANT SELECT ON res_partner TO oracle_view_ro")

    @classmethod
    def as_postgres(cls, command):
        subprocess.run(["runuser", "-u", "postgres", "--", *command], cwd=cls.tmp, check=True, timeout=120,
                       stdout=subprocess.DEVNULL)

    @classmethod
    def write_hba(cls, lines):
        cls.hba.write_text("".join(line + "\n" for line in lines))

    @classmethod
    def sql(cls, database, query):
        return subprocess.run(["runuser", "-u", "postgres", "--", str(PG_BIN / "psql"), "-X", "-At", "-h",
                               "/var/run/postgresql", "-p", cls.PORT, "-d", database, "-v", "ON_ERROR_STOP=1",
                               "-c", query], cwd=cls.tmp, check=True, capture_output=True, text=True,
                              timeout=60).stdout.strip()

    def reload(self, ssl=None):
        """Reload the configuration, and wait until a new session has it."""
        if ssl is not None:
            self.sql("postgres", f"ALTER SYSTEM SET ssl = {'on' if ssl else 'off'}")
        before = self.sql("postgres", "SELECT pg_conf_load_time()")
        self.sql("postgres", "SELECT pg_reload_conf()")
        deadline = time.monotonic() + 10
        while self.sql("postgres", "SELECT pg_conf_load_time()") == before and time.monotonic() < deadline:
            time.sleep(0.1)

    def tool(self, query):  # as the tool signs in: over TCP with the password of the image
        return subprocess.run([str(PG_BIN / "psql"), "-X", "-At", "-c", query,
                               f"host=127.0.0.1 port={self.PORT} dbname=perodua_oracle user=oracle_view_rp sslmode=prefer"],
                              env=dict(os.environ, PGPASSWORD=TEST_PASSWORD), capture_output=True, text=True,
                              timeout=60)

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args, "--pg-port", self.PORT], cwd=self.tmp,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def make_role(self):
        verifier = self.tmp / "rp_grey.verifier"
        verifier.write_text(VERIFIER + "\n")
        return self.run_script("role", "--verifier-file", str(verifier), "--app-ip", self.APP)

    def check(self, lines, **reload):
        """role --check with these pg_hba.conf lines after the two local lines."""
        self.write_hba(self.hba_lines[:2] + lines)
        self.reload(**reload)
        return self.run_script("role", "--check", "--app-ip", self.APP)


REAL_POSTGRES = unittest.skipUnless(
    hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir() and (PG_BIN / "initdb").exists()
    and shutil.which("runuser") and has_postgres_user(), "needs root and PostgreSQL 16 on Linux (the test image)")


@REAL_POSTGRES
class RoleOnPostgresTest(PostgresCluster, unittest.TestCase):
    """The role SQL, the rights check and the pg_hba.conf check on a real throwaway cluster."""
    PORT = "55432"
    # Each statement is also logged by the sampling of statements and of transactions.
    SERVER_OPTIONS = "-c log_min_duration_sample=0 -c log_statement_sample_rate=1 -c log_transaction_sample_rate=1"

    def test_role_rights_pg_hba_check_and_drop_on_a_real_server(self):  # the first test of the class (by name)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'"), "1")
        self.addCleanup(self.reload)
        self.addCleanup(self.write_hba, self.hba_lines)
        verifier = self.tmp / "rp_grey.verifier"
        result = self.make_role()
        output = result.stdout + result.stderr
        # The ::1 line above is IPv6: it cannot match the App server, so this is NOT OK, not CHECK BY HAND.
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("RESULT: NOT OK", result.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 11, result.stdout)
        self.assertEqual(self.sql("postgres", "SELECT rolpassword FROM pg_authid WHERE rolname = 'oracle_view_rp'"), VERIFIER)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'"), "0")
        log = self.server_log.read_text(errors="replace")
        self.assertIn("SET log_statement = 'none'", log)  # the log records statements
        for setting, value in (("log_min_duration_sample", "0"), ("log_statement_sample_rate", "1"),
                               ("log_transaction_sample_rate", "1")):  # the server also logs by sampling
            self.assertEqual(self.sql("postgres", f"SHOW {setting}"), value)
        self.assertNotIn(STORED_KEY, log)
        self.assertNotIn(STORED_KEY, output)
        # Each right is checked alone: without DELETE on one table, the line for INSERT, UPDATE and DELETE fails.
        self.sql("perodua_oracle", "REVOKE DELETE ON res_partner FROM oracle_view_rp")
        missing = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn("FAIL INSERT, UPDATE and DELETE on every table of public (1 without)\n", missing.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", missing.stdout, re.M)), 10, missing.stdout)
        self.assertIn("Nothing in pg_hba.conf was checked", missing.stderr)
        self.assertNotIn("RESULT:", missing.stdout)
        self.sql("perodua_oracle", "GRANT DELETE ON res_partner TO oracle_view_rp")
        # A table that Odoo makes later gets the rights too (default privileges).
        self.sql("perodua_oracle", "SET ROLE odoo; CREATE TABLE later_table (id int)")
        self.assertEqual(self.tool("SHOW default_transaction_read_only").stdout.strip(), "on")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        self.assertEqual(self.tool("SELECT count(*) FROM partner_names").stdout.strip(), "1")
        failed = self.tool("UPDATE res_partner SET name = name")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("read-only transaction", failed.stderr)
        written = self.tool("BEGIN READ WRITE; UPDATE res_partner SET name = name; INSERT INTO later_table VALUES (1);"
                            " SELECT nextval('res_partner_id_seq'); ROLLBACK")
        self.assertEqual(written.returncode, 0, written.stderr)
        for query in ("TRUNCATE res_partner", "ALTER TABLE res_partner ADD COLUMN x int", "CREATE TABLE mine (id int)"):
            with self.subTest(query=query):
                self.assertNotEqual(self.tool(f"BEGIN READ WRITE; {query}; ROLLBACK").returncode, 0)
        # The printed line, at the top of the host lines, lets the App server in.
        line = HBA_LINE.format(self.APP)
        self.assertIn(line, result.stdout)
        self.write_hba(self.hba_lines[:2] + [line] + self.hba_lines[2:])
        self.sql("postgres", "SELECT pg_reload_conf()")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: OK", result.stdout)
        # A samehost line above it is a line that the check cannot read.
        self.write_hba(self.hba_lines[:2] + ["host all all samehost scram-sha-256", line] + self.hba_lines[2:])
        self.sql("postgres", "SELECT pg_reload_conf()")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.write_hba(self.hba_lines[:2] + [line] + self.hba_lines[2:])
        self.sql("postgres", "SELECT pg_reload_conf()")
        # A second run sets the same verifier and grants.
        result = self.run_script("role", "--verifier-file", str(verifier), "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.tool("SELECT count(*) FROM later_table").returncode, 0)
        # drop-role takes the role and its rights away.
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "0")
        self.assertEqual(self.sql("perodua_oracle", "SELECT count(*) FROM res_partner"), "1")

    def test_the_pg_hba_check_reads_each_kind_of_rule(self):
        self.write_hba(self.hba_lines)
        self.reload()
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.addCleanup(self.reload, ssl=False)
        self.addCleanup(self.write_hba, self.hba_lines)
        rest = self.hba_lines[2:]

        def rp(kind="host", database="perodua_oracle", user="oracle_view_rp", address=f"{self.APP}/32",
               method="scram-sha-256"):
            return f"{kind} {database} {user} {address} {method}"
        ok, md5 = "RESULT: OK - the first matching line uses scram-sha-256.", "RESULT: OK - the first matching line uses md5."
        cases = [  # (name, lines, ssl, exit status, text of the output)
            ("hostnossl, ssl off", [rp("hostnossl")], False, 0, ok),
            ("hostssl, ssl off", [rp("hostssl")], False, 3, "| hostssl record cannot match because SSL is disabled | "
                                                             "loaded, never matches"),
            ("hostssl, ssl on", [rp("hostssl")], True, 0, ok),
            ("hostnossl, ssl on", [rp("hostnossl")], True, 3, "RESULT: NOT OK"),
            ("md5", [rp(method="md5")], False, 0, md5),
            ("an all address", [rp(address="all")], False, 0, ok),
            ("a reject first", [rp(method="reject"), rp()], False, 3, "first matching line uses: reject"),
            ("password first", [rp(method="password"), rp()], False, 3, "RESULT: NOT OK"),
            ("samenet above", ["host all all samenet scram-sha-256", rp()], False, 4, "RESULT: CHECK BY HAND - a line above"),
            ("a database regular expression above", [rp(database="/^perodua"), rp()], False, 4,
             "RESULT: CHECK BY HAND - a line above"),
            ("a user regular expression above", [rp(user="/^oracle_view"), rp()], False, 4,
             "RESULT: CHECK BY HAND - a line above"),
            ("samegroup above", [rp(database="samegroup"), rp()], False, 4, "RESULT: CHECK BY HAND - a line above"),
            ("a line with an error", ["host all all 10.0.0.0/33 scram-sha-256", rp()], False, 4,
             "RESULT: CHECK BY HAND - pg_hba.conf has lines with errors"),
        ]
        (self.data / "server.crt").exists() or subprocess.run(
            ["openssl", "req", "-new", "-x509", "-days", "1", "-nodes", "-subj", "/CN=localhost",
             "-keyout", str(self.data / "server.key"), "-out", str(self.data / "server.crt")],
            check=True, capture_output=True, timeout=60)
        for name in ("server.key", "server.crt"):
            shutil.chown(self.data / name, "postgres", "postgres")
            (self.data / name).chmod(0o600)
        for name, lines, ssl, status, text in cases:
            with self.subTest(name=name):
                result = self.check(lines + rest, ssl=ssl)
                self.assertEqual(self.sql("postgres", "SHOW ssl"), "on" if ssl else "off")
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertIn(text, result.stdout)
        # md5 lets the role in: its password is a SCRAM-SHA-256 verifier, so PostgreSQL uses SCRAM.
        self.write_hba(self.hba_lines[:2] + ["host perodua_oracle oracle_view_rp 127.0.0.1/32 md5"])
        self.reload(ssl=False)
        self.assertEqual(self.tool("SELECT 1").stdout.strip(), "1")
        # The rules are taken in the order of PostgreSQL, also through an include file: line 10 of the
        # included file comes before line 4 of pg_hba.conf.
        included = self.data / "oracle-view-include.conf"
        included.write_text("# made-up include file\n" * 9 + rp(method="reject") + "\n")
        shutil.chown(included, "postgres", "postgres")
        result = self.check(["include oracle-view-include.conf", rp()] + rest)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("first matching line uses: reject", result.stdout)
        result = self.check([rp(), "include oracle-view-include.conf"] + rest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_block_of_a_later_deploy_db_run_goes_above_the_line(self):
        writer = deploy_db_hba_writer()
        self.assertIsNotNone(writer, "the pg_hba.conf writer of deploy-db.sh")

        def deploy_db():  # as deploy-db.sh for perodua_oracle with the App role odoo writes pg_hba.conf
            subprocess.run(["python3", "-", str(self.hba), "perodua_oracle", "odoo", "198.51.100.0/24", ""],
                           input=writer, text=True, check=True, timeout=60)
            self.reload()
        self.write_hba(self.hba_lines)
        self.reload()
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.addCleanup(self.reload)
        self.addCleanup(self.write_hba, self.hba_lines)
        line = HBA_LINE.format(self.APP)
        self.write_hba(self.hba_lines[:2] + [line] + self.hba_lines[2:])
        self.reload()
        self.assertEqual(self.run_script("role", "--check", "--app-ip", self.APP).returncode, 0)
        deploy_db()
        lines = self.hba.read_text().splitlines()
        self.assertEqual(lines[0], BLOCK)
        self.assertLess(lines.index('host "perodua_oracle" all 0.0.0.0/0 reject'), lines.index(line))
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)  # the reject of the block comes first
        self.assertIn("first matching line uses: reject", result.stdout)
        self.assertIn(f'  2. Add this line above line 1, "{BLOCK}"', result.stdout)
        self.assertIn("After each deploy-db.sh run, run:\n"
                      f"    sudo bash oracle-view.sh role --check --app-ip {self.APP} --pg-port {self.PORT}\n", result.stdout)
        # Above the block it lets the App server in, and the note stays.
        self.hba.write_text(line + "\n" + "".join(text + "\n" for text in lines if text != line))
        self.reload()
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"NOTE: {self.hba} holds the block of deploy-db.sh for perodua_oracle (line 2).", result.stdout)
        # The next deploy-db.sh run puts its block above the line again.
        deploy_db()
        self.assertEqual(self.run_script("role", "--check", "--app-ip", self.APP).returncode, 3)


@REAL_POSTGRES
class StatementStatsOnPostgresTest(PostgresCluster, unittest.TestCase):
    """With pg_stat_statements loaded, the statements with the verifier stay out of it."""
    PORT = "55433"
    SERVER_OPTIONS = "-c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track_utility=on"
    WITH_THE_VERIFIER = "SELECT count(*) FROM pg_stat_statements WHERE query ~ 'ROLE oracle_view_rp LOGIN PASSWORD'"

    def test_the_verifier_stays_out_of_pg_stat_statements(self):
        self.assertEqual(self.make_role().returncode, 3)
        self.sql("postgres", "CREATE EXTENSION pg_stat_statements")
        self.addCleanup(self.sql, "postgres", "DROP EXTENSION pg_stat_statements")
        # It records utility statements: the CREATE ROLE of the cluster setup is there.
        self.assertNotEqual(self.sql("postgres", "SELECT count(*) FROM pg_stat_statements "
                                                 "WHERE query LIKE 'CREATE ROLE odoo%'"), "0")
        self.assertEqual(self.sql("postgres", self.WITH_THE_VERIFIER), "0")
        self.assertEqual(self.sql("postgres", f"SELECT count(*) FROM pg_stat_statements WHERE strpos(query, '{STORED_KEY}') > 0"), "0")
        # A second run (ALTER ROLE) too.
        self.assertEqual(self.make_role().returncode, 3)
        self.assertEqual(self.sql("postgres", self.WITH_THE_VERIFIER), "0")


README = (SCRIPT.parents[1] / "README.md").read_text()
SECTION = README.split("\n## Oracle Field Browser\n", 1)[1].split("\n## ", 1)[0]


def words(text):  # the text with each run of white space as one space, as Markdown shows it
    return " ".join(text.split())


class OracleViewDocsTest(unittest.TestCase):
    """The README and the usage text name the pinned release and the steps that the script expects."""

    def test_the_readme_and_the_usage_name_the_pinned_release(self):
        tag = IMAGE.split("@")[0]
        self.assertEqual(set(re.findall(r"oracle-view:(grey-v[0-9][0-9.]*[0-9])", README)), {tag.split(":")[1]})
        self.assertIn(f"`{tag}`", SECTION)
        self.assertIn(f"Wait for `Installed: {RELEASE}`.", SECTION)
        usage = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(usage.returncode, 0, usage.stderr)
        self.assertIn(f"install    Pulls the pinned image ({RELEASE}) from the private registry", usage.stdout)

    def test_the_intro_names_the_tool(self):
        intro = README.split("\n## ", 1)[0]
        self.assertIn("**Oracle Field Browser** (separate)", intro)
        self.assertIn("See [Oracle Field Browser](#oracle-field-browser).", words(intro))
        self.assertEqual(README.count("\n## Oracle Field Browser\n"), 1)  # the anchor of the link

    def test_the_move_from_the_manual_install_deletes_its_folder_after_the_last_use(self):
        steps = SECTION.split("\n### From the manual install\n", 1)[1].split("\n### ", 1)[0]
        order = ["uninstall-app.sh", "install --config /opt/oracle-view/.env", "sudo cp /opt/oracle-view/users",
                 "sudo rm -rf /opt/oracle-view\n", "Put the route back"]
        for text in order:
            self.assertIn(text, steps)
        at = [steps.index(text) for text in order]
        self.assertEqual(at, sorted(at))
        self.assertEqual(steps.count("/opt/oracle-view"), steps[:at[3]].count("/opt/oracle-view") + 1)
        self.assertIn("The state volume `oracle-view_oracle-view-state` (the mapping edits and the SQL audit) is a Docker "
                      "volume, not in that folder: it stays.", words(steps))

    def test_the_readme_and_the_script_know_the_block_of_deploy_db(self):
        deploy_db = DEPLOY_DB.read_text()
        self.assertIn("begin=f'# BEGIN perodua-db-deploy {db}'", deploy_db)
        self.assertIn("block += [f'host \"{name}\" all 0.0.0.0/0 reject',f'host \"{name}\" all ::/0 reject']", deploy_db)
        self.assertIn("text='\\n'.join(block+[end]+remaining)+'\\n'", deploy_db)  # at the top, on every run
        self.assertIn('marker="# BEGIN perodua-db-deploy $RP_DATABASE"', TEXT)
        text = words(SECTION)
        for sentence in ("Each `deploy-db.sh` run writes its block for the database at the top of `pg_hba.conf`, from "
                         "`# BEGIN perodua-db-deploy perodua_oracle` to `# END perodua-db-deploy perodua_oracle`.",
                         'ends with `host "perodua_oracle" all 0.0.0.0/0 reject`.',
                         "after every `deploy-db.sh` run, run `sudo bash oracle-view.sh role --check` and move the line "
                         "back above the block."):
            self.assertIn(sentence, text)


if __name__ == "__main__":
    unittest.main()
