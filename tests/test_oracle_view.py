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
                 ("OVB_CPUS", "100"), ("OVB_RP_SSLMODE", "disable"), ("OVB_RP_SSLMODE", "verify-full"),
                 ("OVB_LIVE_TIMEOUT", "4"), ("OVB_LIVE_TIMEOUT", "3601"), ("OVB_LIVE_TIMEOUT", "0"),
                 ("OVB_LIVE_TIMEOUT", "0120"), ("OVB_LIVE_TIMEOUT", "1_20"), ("OVB_LIVE_TIMEOUT", "120s"),
                 ("OVB_SQL_TIMEOUT", "4"), ("OVB_SQL_TIMEOUT", "3601"), ("OVB_SQL_TIMEOUT", "-5"),
                 ("OVB_SQL_TIMEOUT", "5.5"), ("OVB_SQL_TIMEOUT", " 60"), ("OVB_SQL_TIMEOUT", "10000")]
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

    def test_the_time_outs_are_optional_settings(self):
        # Not set: no variable for the container, so the image default (300 s) applies. The environment of
        # the command is not a setting.
        result = self.install(OVB_LIVE_TIMEOUT="120", OVB_SQL_TIMEOUT="60")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("TIMEOUT", (self.dir / "compose.yml").read_text())
        self.assertNotIn("TIMEOUT", (self.dir / "oracle-view.env").read_text())
        default = "300 s (the default of the image)"
        self.assertIn(f"Time-outs: live Oracle calls {default}, SQL console {default}\n", self.run_script("status").stdout)
        # The ends of the range, as strings of the environment of the container, after OVB_RP_SSLMODE.
        self.write_config(OVB_LIVE_TIMEOUT="5", OVB_SQL_TIMEOUT="3600")
        self.installed()
        self.assertIn('      OVB_RP_SSLMODE: "require"\n      OVB_LIVE_TIMEOUT: "5"\n      OVB_SQL_TIMEOUT: "3600"\n'
                      '    mem_limit: "1g"\n', (self.dir / "compose.yml").read_text())
        self.assertEqual([line for line in (self.dir / "oracle-view.env").read_text().splitlines() if not line.startswith("#")],
                         ["OVB_MEM_LIMIT=1g", "OVB_PIDS_LIMIT=256", "OVB_CPUS=1.5", "OVB_RP_SSLMODE=require",
                          "OVB_LIVE_TIMEOUT=5", "OVB_SQL_TIMEOUT=3600"])
        self.assertIn("Time-outs: live Oracle calls 5 s, SQL console 3600 s\n", self.run_script("status").stdout)
        self.config.unlink()
        self.installed()  # the saved values stay
        self.assertIn('      OVB_LIVE_TIMEOUT: "5"\n      OVB_SQL_TIMEOUT: "3600"\n', (self.dir / "compose.yml").read_text())
        # A key with no value removes the saved value.
        self.write_config(OVB_SQL_TIMEOUT="")
        self.installed()
        compose = (self.dir / "compose.yml").read_text()
        self.assertIn('      OVB_RP_SSLMODE: "require"\n      OVB_LIVE_TIMEOUT: "5"\n    mem_limit: "1g"\n', compose)
        self.assertNotIn("OVB_SQL_TIMEOUT", compose + (self.dir / "oracle-view.env").read_text())
        self.assertIn(f"Time-outs: live Oracle calls 5 s, SQL console {default}\n", self.run_script("status").stdout)

    def test_the_example_settings_are_a_valid_config(self):
        text = SCRIPT.with_name("oracle-view.env.example").read_text()
        for line in ("\n# OVB_LIVE_TIMEOUT=300\n", "\n# OVB_SQL_TIMEOUT=300\n", "in seconds: 5-3600"):
            self.assertIn(line, text)
        self.config.write_text(text)
        self.installed()
        self.assertNotIn("TIMEOUT", (self.dir / "compose.yml").read_text())
        # The defaults that it names are values in the range.
        self.config.write_text(text.replace("# OVB_LIVE_TIMEOUT=", "OVB_LIVE_TIMEOUT=").replace("# OVB_SQL_TIMEOUT=",
                                                                                              "OVB_SQL_TIMEOUT="))
        self.installed()
        self.assertIn('      OVB_LIVE_TIMEOUT: "300"\n      OVB_SQL_TIMEOUT: "300"\n', (self.dir / "compose.yml").read_text())

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
elif command == "SELECT count(*) FROM pg_database WHERE datname = 'perodua_oracle2'":
    out = "1\n" if state["database"] else "0\n"
elif command.startswith("SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"):
    out = "1\n" if state["role_exists"] else "0\n"
elif command == "SHOW ssl":
    out = state["ssl"] + "\n"
elif command == "SHOW hba_file":
    out = state["hba_file"] + "\n"
elif "FROM pg_extension" in command:
    out = state["extensions"] + "\n"
elif command.startswith("SELECT count(*) || '|' || pg_size_pretty") and "n.nspname = 'ovb_stage'" in command:
    out = state["stage_total"] + "\n"
elif command.startswith("SELECT format('    %-40s %12s  %s'") and "n.nspname = 'ovb_stage'" in command:
    out = "".join(line + "\n" for line in state["stage_list"])
elif command.startswith("WITH s AS (") and command.endswith("SELECT obj FROM outside ORDER BY obj"):
    out = "".join(line + "\n" for line in state["stage_outside"])
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
             "OK|read-only by default (default_transaction_read_only=on)", "OK|CONNECT on perodua_oracle2",
             "OK|SELECT on every table and view of public (0 without)",
             "OK|INSERT, UPDATE and DELETE on every table of public (0 without)",
             "OK|USAGE and UPDATE on every sequence of public (0 without)",
             # The rights of the owner of the database (owner decision of 2026-11-09).
             "OK|TRUNCATE on every table of public (0 without)", "OK|CREATE in public",
             "OK|member of odoo, the owner of perodua_oracle2, with INHERIT TRUE, SET FALSE, ADMIN FALSE",
             "OK|no right on a database other than perodua_oracle2",
             # The schema of the staging tables of Copy to RP (copy design, section 4.3).
             "OK|owns the schema ovb_stage", "OK|USAGE and CREATE on the schema ovb_stage",
             "OK|no right of PUBLIC on the schema ovb_stage",
             # Information only: never a stop.
             "INFO|owns 0 objects outside the schema ovb_stage",
             "INFO|other databases of odoo, with its rights through the membership: none",
             "INFO|other roles of which it is a member (also through odoo): none"]
MEMBER_HINT = ("oracle_view_rp gets the rights of the owner of perodua_oracle2 through a membership in that owner "
               "(owner decision of\n2026-11-09): INHERIT TRUE, SET FALSE, ADMIN FALSE, and the owner is not a superuser.")
# The SQL of the script that gives the objects of oracle_view_rp in a database to the owner of that database, before
# each DROP OWNED BY oracle_view_rp of role and drop-role (owner decision of 2026-11-09).
REASSIGN_SQL = re.search(r"^REASSIGN_SQL=\$\(cat <<'SQL'\n(.*?)\nSQL\n\)$", TEXT, re.M | re.S).group(1)
STAGE_SQL = ("CREATE SCHEMA IF NOT EXISTS ovb_stage AUTHORIZATION oracle_view_rp;\n", "REVOKE ALL ON SCHEMA ovb_stage FROM PUBLIC;\n",
             "GRANT USAGE, CREATE ON SCHEMA ovb_stage TO oracle_view_rp;\n")
# Two staging tables, as the list of the script shows them.
STAGE_LIST = ["    ovb_copy_20261107_parts                       1160 kB  oracle_view_rp",
              "    ovb_copy_20261106_prices                   8192 bytes  oracle_view_rp"]
RP_DB = "perodua_oracle2"  # the database of the grey App (owner decision of 2026-11-07) and of the image
OLD_DB = "perodua_oracle"  # the database of the releases before grey-v1.0.2: role and drop-role clean it
# role and drop-role clean the old database only when it accepts connections (not ALLOW_CONNECTIONS false,
# not half dropped): else each run stops at its \connect there.
OLD_DATABASE_CHECK = (":'ovb_old_db' <> :'ovb_db'\n         AND EXISTS (SELECT 1 FROM pg_database WHERE datname = :'ovb_old_db'\n"
                      "                       AND datallowconn AND datconnlimit <> -2) AS ovb_old \\gset\n")
# The hint for a right in another database: DROP OWNED BY there also revokes CONNECT on perodua_oracle2.
SAFE_FORM = ("BEGIN; DROP OWNED BY oracle_view_rp; GRANT CONNECT, TEMPORARY ON DATABASE perodua_oracle2 TO oracle_view_rp;"
             " COMMIT;")
SAFE_FORM_HINT = ("remove the rights of oracle_view_rp. Run this in that database:\n    " + SAFE_FORM + "\n"
                  "DROP OWNED BY alone also removes CONNECT on perodua_oracle2 (a database is a shared object): "
                  "then the tool cannot connect.\n"
                  # DROP OWNED BY would drop what the role made there (owner decision of 2026-11-09: it stays).
                  "DROP OWNED BY also drops the objects of oracle_view_rp there (a table that the SQL console made, for "
                  "example). To keep\nthem, run first in that database: REASSIGN OWNED BY oracle_view_rp TO OWNER; "
                  "(OWNER: the owner of that database, see \\l).\nThen run role again.\n")


def in_order(text, steps):
    """The position of each step in text, each one searched after the one before; -1 when it is not there."""
    at, found = 0, []
    for step in steps:
        at = text.find(step, at)
        found.append(at)
        if at < 0:
            break
        at += len(step)
    return found
HBA_LINE = "host    perodua_oracle2    oracle_view_rp    {}/32    scram-sha-256"
BLOCK = "# BEGIN perodua-db-deploy perodua_oracle2"  # deploy-db.sh: the first line of its block for the database
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
                       hba_file=HBA_FILE, stage_total="0|0 bytes", stage_list=[], stage_outside=[])
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
                     ["-v", "ovb_db=perodua_oracle2"], ["-v", "ovb_old_db=perodua_oracle"], ["-f", "-"]):
            self.assertIn(" ".join(pair), " ".join(args))
        sql = call["stdin"]
        self.assertTrue(sql.startswith(f"\\set ovb_verifier '{VERIFIER}'\n"), sql[:80])
        self.assertEqual(sql.count(STORED_KEY), 1)
        for each in self.calls():
            self.assertNotIn(STORED_KEY, " ".join(each["args"]))
        self.assertNotIn(STORED_KEY, result.stdout + result.stderr)
        # Out of the server log and the error text, before each statement with the verifier:
        # at the start of the session, and again after each \connect.
        start = sql[:sql.index("\\if :ovb_ro\n")]
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
        # The owners, read before any change: a superuser owner of ovb_db stops the run, and so does the role as the
        # owner of ovb_db or ovb_old_db (its objects there go to the owner of the database, never to itself).
        guard = ["SELECT r.rolname AS ovb_owner, r.rolsuper AS ovb_owner_super\n  FROM pg_database d JOIN pg_roles r "
                 "ON r.oid = d.datdba WHERE d.datname = :'ovb_db' \\gset\n",
                 "AS ovb_owned\n  FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba\n WHERE d.datname IN (:'ovb_db', "
                 ":'ovb_old_db') AND r.rolname = 'oracle_view_rp' \\gset\n", "\\if :ovb_owner_super\n", "RAISE EXCEPTION",
                 "\\elif :ovb_owner_self\n", "A different owner is needed first", "RAISE EXCEPTION", "\\endif\n"]
        self.assertNotIn(-1, in_order(start, guard), start)
        self.assertLess(sql.index(guard[0]), sql.index("ALTER ROLE oracle_view_ro NOLOGIN;"))
        # The rights of the owner decision of 2026-11-09: the grants of 2026-11-06, and the membership in the owner
        # of ovb_db, read again (never a superuser, never the role), with INHERIT and without SET.
        membership = ("SELECT format('GRANT %I TO oracle_view_rp WITH INHERIT TRUE, SET FALSE', r.rolname)\n"
                      "  FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba\n"
                      " WHERE d.datname = :'ovb_db' AND NOT r.rolsuper AND r.rolname <> 'oracle_view_rp' \\gexec\n")
        self.assertNotIn(-1, in_order(sql, ['GRANT CONNECT, TEMPORARY ON DATABASE :"ovb_db" TO oracle_view_rp;\n',
                                            "SET client_min_messages = warning;\n", membership,
                                            "RESET client_min_messages;\n", "\\connect :ovb_db\n"]))
        for line in ("ALTER ROLE oracle_view_rp NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;",
                     "ALTER ROLE oracle_view_rp SET default_transaction_read_only = on;",
                     "ALTER ROLE oracle_view_rp SET statement_timeout = '30s';",
                     "ALTER ROLE oracle_view_rp SET idle_in_transaction_session_timeout = '60s';",
                     'GRANT CONNECT, TEMPORARY ON DATABASE :"ovb_db" TO oracle_view_rp;',
                     "\\connect :ovb_db\nGRANT USAGE ON SCHEMA public TO oracle_view_rp;",
                     "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO oracle_view_rp;",
                     "GRANT SELECT, USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public TO oracle_view_rp;",
                     "IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oracle_view_rp",
                     "IN SCHEMA public GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO oracle_view_rp",
                     "DROP ROLE oracle_view_ro;"):
            self.assertIn(line, sql)
        statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        # The rights of the owner come with the membership only: no other grant of them, no SET and no ADMIN.
        for word in ("TRUNCATE", "ALTER SYSTEM", "pg_reload_conf", "OWNER TO", "GRANT CREATE", "GRANT ALL", "SET TRUE",
                     "ADMIN"):
            self.assertNotIn(word, statements)
        self.assertEqual(statements.count("WITH INHERIT TRUE"), 1)
        self.assertIsNone(re.search(r"(?<!NO)SUPERUSER", statements))
        # Copy to RP: the schema ovb_stage of the role in ovb_db, after the grants there; CREATE only there.
        self.assertNotIn(-1, in_order(sql, ["\\connect :ovb_db\nGRANT USAGE ON SCHEMA public TO oracle_view_rp;", *STAGE_SQL]))
        self.assertEqual(sql.rfind("\\connect"), sql.rfind("\\connect :ovb_db\n"))  # the schema goes to ovb_db
        self.assertEqual(re.findall(r"^.*\bCREATE\b.*ON SCHEMA.*$", statements, re.M), [STAGE_SQL[2].strip()])
        self.assertEqual(re.findall(r"^.*\bREVOKE\b.*$", statements, re.M), [STAGE_SQL[1].strip()])
        # The old database: its sessions of the role end, and in ONE transaction DROP OWNED BY removes the
        # rights of the role (also on every database) and ovb_db gets CONNECT and TEMPORARY again.
        old = sql[sql.index("\\if :ovb_old\n"):]
        old = old[:old.index("\\connect postgres\n")]
        steps = ["\\if :ovb_rp\n", "pg_terminate_backend(pid, 5000)",
                 "WHERE usename = 'oracle_view_rp' AND datname = :'ovb_old_db'", "\\endif\n",
                 "\\connect :ovb_old_db\n", "BEGIN;\n", "DROP OWNED BY oracle_view_ro;\n", "\\if :ovb_rp\n",
                 # What the role made there (it has the rights of odoo there when odoo owns both databases) goes to the
                 # owner of that database, named, before DROP OWNED BY: it stays.
                 REASSIGN_SQL + "\n",
                 "DROP OWNED BY oracle_view_rp;\n", 'GRANT CONNECT, TEMPORARY ON DATABASE :"ovb_db" TO oracle_view_rp;\n',
                 "\\endif\n", "COMMIT;\n",
                 # After the COMMIT, the sessions that opened before it end too.
                 "\\if :ovb_rp\n", "WHERE usename = 'oracle_view_rp' AND datname = :'ovb_old_db'", "\\endif\n",
                 "\\endif\n"]
        self.assertNotIn(-1, in_order(old, steps), old)
        transaction = old[old.index("BEGIN;\n"):old.index("COMMIT;\n")]
        self.assertNotIn("\\connect", transaction)
        self.assertIn(OLD_DATABASE_CHECK, sql)  # no old database, the same one, or one without connections: nothing to clean
        # oracle_view_ro of an earlier bundle: cleaned in both databases, then dropped.
        self.assertNotIn(-1, in_order(sql, ["ALTER ROLE oracle_view_ro NOLOGIN;", "usename = 'oracle_view_ro'",
                                            "\\connect :ovb_old_db\n", "DROP OWNED BY oracle_view_ro;\n",
                                            "\\connect :ovb_db\nDROP OWNED BY oracle_view_ro;\n",
                                            "\\connect postgres\n", "DROP ROLE oracle_view_ro;\n"]))
        self.assertIn("OK   the password is a SCRAM-SHA-256 verifier\n", result.stdout)
        self.assertIn("== Staging tables of Copy to RP: the schema ovb_stage of perodua_oracle2\n"
                      "none: ovb_stage holds no table\n", result.stdout)
        self.assertIn("The role is ready", result.stdout)
        self.assertIn(f"Delete your copy of the verifier file: rm {self.verifier}", result.stdout)

    def test_a_missing_right_stops_before_the_pg_hba_check(self):
        elsewhere = "FAIL|no right on a database other than perodua_oracle2 (it has rights on: perodua_oracle)"
        owner = "FAIL|owns the schema ovb_stage (the owner of ovb_stage is odoo)"
        member = "FAIL|member of odoo, the owner of perodua_oracle2, with INHERIT TRUE, SET FALSE, ADMIN FALSE"
        stage_hint = ("The schema ovb_stage of perodua_oracle2 holds the staging tables of Copy to RP: oracle_view_rp "
                      "must own it, with USAGE and CREATE there,\n")
        for rights, message in ((RIGHTS_OK[:8] + ["FAIL|TRUNCATE on every table of public (3 without)"] + RIGHTS_OK[9:],
                                 "does not have the rights above"),
                                (RIGHTS_OK[:9] + ["FAIL|CREATE in public"] + RIGHTS_OK[10:], "does not have the rights above"),
                                (RIGHTS_OK[:10] + [member + " (it is not a member)"] + RIGHTS_OK[11:], MEMBER_HINT),
                                (RIGHTS_OK[:10] + [member + " (it has INHERIT TRUE, SET TRUE, ADMIN FALSE)"] + RIGHTS_OK[11:],
                                 MEMBER_HINT),
                                (RIGHTS_OK[:10] + [member + " (odoo is a superuser)"] + RIGHTS_OK[11:], MEMBER_HINT),
                                (RIGHTS_OK[:11] + [elsewhere] + RIGHTS_OK[12:], SAFE_FORM_HINT),
                                (RIGHTS_OK[:12] + [owner] + RIGHTS_OK[13:], stage_hint),
                                (RIGHTS_OK[:13] + ["FAIL|USAGE and CREATE on the schema ovb_stage"] + RIGHTS_OK[14:],
                                 stage_hint),
                                (RIGHTS_OK[:14] + ["FAIL|no right of PUBLIC on the schema ovb_stage"] + RIGHTS_OK[15:],
                                 stage_hint),
                                (RIGHTS_OK[:12], "unexpected answer"),  # the 12 checks of the releases before ovb_stage
                                (RIGHTS_OK[:15], "unexpected answer"),  # the 15 checks of the releases before 2026-11-09
                                (RIGHTS_OK[:16], "unexpected answer"), (RIGHTS_OK[:17], "unexpected answer"),
                                (RIGHTS_OK[4:], "unexpected answer")):
            with self.subTest(rights=rights):
                self.set_state(rights=rights)
                result = self.role()
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.scripts("hba-check"), [])
                if message is not stage_hint:  # the hint of ovb_stage only for a FAIL line of ovb_stage
                    self.assertNotIn("staging tables", result.stderr)
                if message is not MEMBER_HINT:  # the hint of the membership only for its FAIL line
                    self.assertNotIn("through a membership", result.stderr)
        # An INFO line never stops the command, also with objects of the role outside ovb_stage.
        info = ["INFO|owns 2 objects outside the schema ovb_stage (schema made_up_more, table public.mine)",
                "INFO|other databases of odoo, with its rights through the membership: perodua_oracle",
                "INFO|other roles of which it is a member (also through odoo): pg_read_all_data"]
        self.set_state(rights=RIGHTS_OK[:15] + info)
        result = self.role()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INFO owns 2 objects outside the schema ovb_stage (schema made_up_more, table public.mine)\n"
                      "INFO other databases of odoo, with its rights through the membership: perodua_oracle\n"
                      "INFO other roles of which it is a member (also through odoo): pg_read_all_data\n", result.stdout)
        self.assertIn("The role is ready", result.stdout)
        # Membership through another role: SET ROLE or ADMIN to the owner that way fails too.
        for has in ("it can SET ROLE to odoo through another role", "it has ADMIN on odoo through another role"):
            with self.subTest(has=has):
                self.set_state(rights=RIGHTS_OK[:10] + [f"{member} ({has})"] + RIGHTS_OK[11:])
                result = self.role()
                self.assertEqual(result.returncode, 1)
                self.assertIn(MEMBER_HINT, result.stderr)
                self.assertEqual(self.scripts("hba-check"), [])
        # The check itself: the options of each direct grant, and the paths through other roles (pg_has_role).
        (query,) = [call["args"][call["args"].index("-c") + 1] for call in self.calls() if "-c" in call["args"]
                    and "the password is a SCRAM-SHA-256 verifier" in call["args"][call["args"].index("-c") + 1]]
        for text in ("AND NOT pg_has_role('oracle_view_rp', o.oid, 'SET')",
                     "AND NOT pg_has_role('oracle_view_rp', o.oid, 'MEMBER WITH ADMIN OPTION')",
                     "coalesce(bool_or(m.inherit_option) AND NOT bool_or(m.set_option) AND NOT bool_or(m.admin_option), "
                     "false)", "LEFT JOIN pg_auth_members m ON m.roleid = d.datdba",
                     "AND pg_has_role('oracle_view_rp', x.oid, 'MEMBER')",
                     "SELECT CASE WHEN n IN (16, 17, 18) THEN 'INFO' WHEN ok THEN 'OK' ELSE 'FAIL' END"):
            self.assertIn(text, query)

    def test_the_objects_of_the_role_go_to_the_owner_of_the_database(self):
        # Owner decision of 2026-11-09: what the role made (with the rights of the owner) is data. Before each DROP
        # OWNED BY oracle_view_rp, its objects go to the owner of that database, never to the role itself, and psql
        # names them.
        steps = ["  FROM pg_shdepend s CROSS JOIN LATERAL pg_identify_object(s.classid, s.objid, s.objsubid) o\n",
                 "AND s.deptype = 'o' AND s.refclassid = 'pg_authid'::regclass\n",
                 "AND s.classid NOT IN ('pg_default_acl'::regclass, 'pg_user_mapping'::regclass) \\gset\n",
                 "\\if :ovb_kept_self\n", "A different owner is needed first.\n", "RAISE EXCEPTION", "\\endif\n",
                 "SELECT format('REASSIGN OWNED BY oracle_view_rp TO %I', :'ovb_kept_owner') \\gexec\n",
                 "\\if :ovb_kept_any\n\\echo :ovb_kept objects of oracle_view_rp stay in :ovb_kept_db, now owned by "
                 ":ovb_kept_owner, the owner of the database:\n\\echo :ovb_kept_list\n\\endif"]
        self.assertNotIn(-1, in_order(REASSIGN_SQL, steps), REASSIGN_SQL)
        self.assertIn("(SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) AS "
                      "ovb_kept_owner", REASSIGN_SQL)
        self.assertNotIn("DROP", REASSIGN_SQL)
        # role: in the old database, inside its transaction; drop-role: in both databases. Each DROP OWNED BY
        # oracle_view_rp comes right after it.
        self.assertEqual(self.role().returncode, 0)
        (role,) = self.scripts("role")
        self.set_state(role_exists=True)
        self.assertEqual(self.run_script("drop-role", "--confirm", "oracle_view_rp").returncode, 0)
        (drop,) = self.scripts("drop-role")
        for sql, count in ((role["stdin"], 1), (drop["stdin"], 2)):
            self.assertEqual(sql.count(REASSIGN_SQL + "\nDROP OWNED BY oracle_view_rp;\n"), count)
            self.assertEqual(sql.count("\nDROP OWNED BY oracle_view_rp;\n"), count)

    def test_role_lists_the_staging_tables_with_their_sizes(self):
        self.set_state(role_exists=True, stage_total="2|1168 kB", stage_list=STAGE_LIST)
        for args in (["--check"], ["--verifier-file", str(self.verifier)]):
            with self.subTest(args=args):
                result = self.run_script("role", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("== Staging tables of Copy to RP: the schema ovb_stage of perodua_oracle2\n"
                              "2 tables, 1168 kB on disk:\n"
                              f"    {'table':<40} {'size':>12}  owner\n"
                              + "".join(line + "\n" for line in STAGE_LIST)
                              + "To free the disk, drop the staging tables that nobody needs (README.md, Oracle Field "
                              "Browser).\n", result.stdout)
                # After the rights, before TLS and the pg_hba.conf check: the list changes nothing.
                self.assertNotIn(-1, in_order(result.stdout, ["== Rights of oracle_view_rp", "== Staging tables",
                                                              "== TLS: SHOW ssl", "== pg_hba.conf check"]))
                self.assertEqual(self.scripts("drop-role"), [])
        self.set_state(stage_total="no answer")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("The list of the tables in ovb_stage gave an unexpected answer", result.stderr)
        self.assertEqual(self.scripts("hba-check"), [])

    def test_the_pg_hba_check_prints_the_line_and_never_edits_the_file(self):
        self.set_state(role_exists=True, hba_output="RESULT: NOT OK - no matching line.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.scripts("role"), [])
        (call,) = self.scripts("hba-check")
        self.assertIn("-v ovb_app=192.168.23.13", " ".join(call["args"]))
        self.assertIn("(:'ovb_app'::inet & netmask::inet)", call["stdin"])
        self.assertIn(":'ovb_db' = ANY (database)", call["stdin"])
        self.assertIn("-v ovb_db=perodua_oracle2", " ".join(call["args"]))
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
        block = (f"{BLOCK}\nhost \"perodua_oracle2\" all 0.0.0.0/0 reject\n"
                 "# END perodua-db-deploy perodua_oracle2\n")
        hba.write_text(f"# PostgreSQL Client Authentication\n\n{block}local all postgres peer\n")
        self.set_state(role_exists=True, hba_file=str(hba), hba_output="RESULT: NOT OK - no matching line.\n")
        note = (f"NOTE: {hba} holds the block of deploy-db.sh for perodua_oracle2 (line 3). Each later\n"
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
        # The block of another database (also of the database of the earlier releases), or none: no note.
        other = block.replace("perodua_oracle2", "perodua_oracle")
        for text in (f"{other}local all postgres peer\n", block.replace("perodua_oracle2", "perodua_oracle22"),
                     "local all postgres peer\n"):
            with self.subTest(text=text):
                hba.write_text(text)
                result = self.run_script("role", "--check")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("NOTE:", result.stdout)

    def test_a_pg_hba_conf_with_errors_needs_a_check_by_hand(self):
        self.set_state(role_exists=True, hba_output="RESULT: CHECK BY HAND - pg_hba.conf has lines with errors (the list "
                                                    "\"pg_hba.conf lines with errors\"). PostgreSQL does not load such "
                                                    "a file.\n")
        result = self.run_script("role", "--check")
        self.assertEqual(result.returncode, 4)
        self.assertIn("PostgreSQL does not load a pg_hba.conf with errors, so this check cannot tell which lines it uses.\n"
                      "  1. Correct the lines of the list \"pg_hba.conf lines with errors\", in the files that it names.\n", result.stdout)
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
        self.assertIn("perodua_oracle2 has: dblink, postgres_fdw\n", result.stdout)
        self.assertIn("This command changes no right", result.stdout)
        for each in self.calls():  # the only REVOKE is the one of PUBLIC on the schema ovb_stage
            sent = (" ".join(each["args"]) + each.get("stdin", "")).replace(STAGE_SQL[1], "")
            self.assertNotIn("REVOKE", sent)
        self.set_state(ssl="on", extensions="")
        result = self.role()
        self.assertNotIn("WARNING", result.stdout)
        self.assertIn("none: perodua_oracle2 has no dblink and no postgres_fdw\n", result.stdout)

    def test_postgres_and_the_database_are_checked_first(self):
        for state, message in (({"running": False}, "PostgreSQL does not answer on port 5432"),
                               ({"running": True, "database": False}, "There is no database perodua_oracle2 on this server")):
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
                     ["role", "--check", "--drop-staging"], ["uninstall", "--drop-staging"],
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
        self.assertIn("-v ovb_db=perodua_oracle2", " ".join(call["args"]))
        self.assertIn("-v ovb_old_db=perodua_oracle", " ".join(call["args"]))
        # Both databases before DROP ROLE, so that a grant in the old one cannot make it fail.
        for role in ("oracle_view_rp", "oracle_view_ro"):
            steps = [f"ALTER ROLE {role} NOLOGIN;", f"WHERE usename = '{role}'", "\\connect :ovb_db\n",
                     f"DROP OWNED BY {role};\n", "\\if :ovb_old\n\\connect :ovb_old_db\n", f"DROP OWNED BY {role};\n",
                     "\\endif\n\\connect postgres\n", f"DROP ROLE {role};"]
            self.assertNotIn(-1, in_order(sql, steps), role)
        self.assertIn(OLD_DATABASE_CHECK, sql)
        self.assertIn("removes its rights in perodua_oracle2 and perodua_oracle and drops it", result.stdout)
        self.assertIn("pg_terminate_backend", sql)
        self.assertIn(f"stays in {HBA_FILE}: remove it by hand", result.stdout)
        self.assertIn("SELECT pg_reload_conf()", result.stdout)
        # The schema ovb_stage of Copy to RP: dropped in ovb_db before DROP OWNED BY, and only with the choice of
        # the script (ovb_drop_staging) when it holds tables; else the role gets its LOGIN back and nothing goes.
        self.assertIn("-v ovb_drop_staging=off", " ".join(call["args"]))
        self.assertIn("It also drops the schema ovb_stage of perodua_oracle2: the staging tables of Copy to RP and "
                      "their data are dropped.\n", result.stdout)
        self.assertIn("The objects that the role made outside ovb_stage (a table in public, for example) stay: it gives "
                      "them to the owner\nof their database and names them.", result.stdout)
        steps = ["coalesce((SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'), false) AS ovb_rp_login",
                 # The objects of the role go to the owner of the database, never to the role itself: the role as the
                 # owner of ovb_db or ovb_old_db stops the run before any change.
                 "\\if :ovb_rp\n", "AS ovb_rp_owned\n  FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba\n WHERE "
                 "d.datname IN (:'ovb_db', :'ovb_old_db') AND r.rolname = 'oracle_view_rp' \\gset\n\\if :ovb_rp_owner\n",
                 "A different owner is needed first", "RAISE EXCEPTION", "\\endif\n",
                 "ALTER ROLE oracle_view_rp NOLOGIN;",
                 # Each session has ended (its temporary tables too) before the objects of the role are counted.
                 "count(pg_terminate_backend(pid, 5000))", "WHERE usename = 'oracle_view_rp'", "\\connect :ovb_db\n",
                 "count(c.oid) > 0 AND NOT :'ovb_drop_staging'::boolean AS ovb_stage_kept",
                 "\\if :ovb_stage_kept\n\\connect postgres\n\\if :ovb_rp_login\nALTER ROLE oracle_view_rp LOGIN;\n\\endif\n",
                 "RAISE EXCEPTION 'ovb_stage holds staging tables", "\\endif\n\\if :ovb_stage\nWITH s AS (",
                 "WHERE nsp IS DISTINCT FROM (SELECT oid FROM s)) SELECT count(*) > 0 AS ovb_stage_outside,",
                 "AS ovb_stage_outside_list FROM outside \\gset\n",
                 "\\if :ovb_stage_outside\n\\connect postgres\n\\if :ovb_rp_login\nALTER ROLE oracle_view_rp LOGIN;\n"
                 "\\endif\n", "RAISE EXCEPTION 'objects outside ovb_stage depend on it",
                 "\\endif\nDROP SCHEMA ovb_stage CASCADE;\n",
                 "\\echo schema ovb_stage dropped, with its :ovb_stage_tables staging tables and their data",
                 # A schema ovb_stage of another owner: only the staging tables of the role there go.
                 "\\elif :ovb_stage_other\n", "SELECT format('DROP %s IF EXISTS ovb_stage.%I'",
                 "WHERE n.nspname = 'ovb_stage' AND r.rolname = 'oracle_view_rp' AND c.relkind IN ('r', 'p', 'm') \\gexec\n",
                 "\\echo schema ovb_stage of another owner stays:", "\\endif\n",
                 # Every other object of the role goes to the owner of the database, named, and stays: in ovb_db, then
                 # in ovb_old_db, each time right before DROP OWNED BY.
                 REASSIGN_SQL + "\nDROP OWNED BY oracle_view_rp;\n",
                 "\\if :ovb_old\n\\connect :ovb_old_db\n" + REASSIGN_SQL + "\nDROP OWNED BY oracle_view_rp;\n",
                 "DROP ROLE oracle_view_rp;"]
        self.assertNotIn(-1, in_order(sql, steps), sql)
        self.assertEqual(sql.count("\nDROP SCHEMA"), 1)
        statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        self.assertEqual(statements.count("REASSIGN OWNED BY"), 2)  # in ovb_db and in ovb_old_db
        # Materialized views hold data too: they count with the tables, as in the list of the script.
        self.assertIn("LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'm')", sql)
        self.assertNotIn("== Staging tables", result.stdout)  # no role: no list

    def test_drop_role_stops_when_the_staging_tables_hold_data(self):
        self.set_state(role_exists=True, stage_total="2|1168 kB", stage_list=STAGE_LIST)
        listed = ("== Staging tables of Copy to RP: the schema ovb_stage of perodua_oracle2\n2 tables, 1168 kB on disk:\n"
                  f"    {'table':<40} {'size':>12}  owner\n" + "".join(line + "\n" for line in STAGE_LIST))
        for args in (["--confirm", "oracle_view_rp"], []):  # before the confirmation
            with self.subTest(args=args):
                result = self.run_script("drop-role", *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(listed, result.stdout)
                self.assertIn("drop-role drops these 2 tables and their data (1168 kB). To keep a table, copy it first",
                              result.stderr)
                self.assertIn("    sudo -u postgres pg_dump -p 5432 -d perodua_oracle2 -n ovb_stage -Fc -f "
                              "/var/tmp/ovb_stage.dump\n", result.stderr)
                self.assertIn("The schema ovb_stage holds 2 staging tables (above). To drop them with the role, run "
                              "drop-role again with --drop-staging. Nothing was changed.", result.stderr)
                self.assertNotIn("Type oracle_view_rp", result.stdout + result.stderr)
                self.assertEqual(self.scripts(), [])
        result = self.run_script("drop-role", "--drop-staging")  # the choice is not the confirmation
        self.assertEqual(result.returncode, 1)
        self.assertIn("No terminal to confirm on: pass --confirm oracle_view_rp. Nothing was changed.", result.stderr)
        self.assertEqual(self.scripts(), [])
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging", "--pg-port", "5433")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(listed, result.stdout)
        (call,) = self.scripts("drop-role")
        self.assertIn("-v ovb_drop_staging=on", " ".join(call["args"]))
        self.assertIn("-p 5433", " ".join(call["args"]))
        # Without tables, the schema goes without the choice: no data goes with it.
        self.set_state(role_exists=True, stage_total="0|0 bytes", stage_list=[])
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("none: ovb_stage holds no table\n", result.stdout)
        (call,) = self.scripts("drop-role")
        self.assertIn("-v ovb_drop_staging=off", " ".join(call["args"]))

    def test_drop_role_stops_when_objects_outside_depend_on_the_staging_schema(self):
        outside = ["table constraint copy_fk on public.t_ref", "view public.v_copy"]
        self.set_state(role_exists=True, stage_total="2|1168 kB", stage_list=STAGE_LIST, stage_outside=outside)
        for args in (["--confirm", "oracle_view_rp", "--drop-staging"], ["--confirm", "oracle_view_rp"], []):
            with self.subTest(args=args):
                result = self.run_script("drop-role", *args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("These objects outside the schema ovb_stage depend on an object in it. drop-role drops "
                              "the schema with CASCADE,\nso it would drop them too, with their data:\n"
                              "    table constraint copy_fk on public.t_ref\n    view public.v_copy\n", result.stderr)
                self.assertIn("Objects outside the schema ovb_stage depend on it (above). Nothing was changed.",
                              result.stderr)
                self.assertNotIn("Type oracle_view_rp", result.stdout + result.stderr)
                self.assertEqual(self.scripts(), [])
        (query,) = [call["args"][call["args"].index("-c") + 1] for call in self.calls()
                    if "-c" in call["args"] and call["args"][call["args"].index("-c") + 1].startswith("WITH s AS (")]
        self.assertIn("WHERE n.nspname = 'ovb_stage' AND r.rolname = 'oracle_view_rp'", query)
        self.assertIn("WHERE d.deptype <> 'i'", query)
        self.set_state(stage_outside=[])
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.scripts("drop-role")), 1)

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
        # The RP database and the database of the earlier releases, as deploy-db.sh makes them (no PUBLIC rights).
        for database in (RP_DB, OLD_DB):
            cls.sql("postgres", f"CREATE DATABASE {database} OWNER odoo")
            cls.sql("postgres", f"REVOKE ALL ON DATABASE {database} FROM PUBLIC")
        cls.sql(RP_DB, "SET ROLE odoo; CREATE TABLE res_partner (id serial PRIMARY KEY, name varchar);"
                " INSERT INTO res_partner (name) VALUES ('made-up partner');"
                " CREATE VIEW partner_names AS SELECT name FROM res_partner")
        cls.sql(OLD_DB, "SET ROLE odoo; CREATE TABLE old_partner (id serial PRIMARY KEY, name varchar);"
                " INSERT INTO old_partner (name) VALUES ('made-up old partner')")
        cls.make_old_ro()

    @classmethod
    def make_old_ro(cls):  # the read-only role of an earlier bundle, with rights in both databases
        cls.drop_old_ro()
        cls.sql("postgres", "CREATE ROLE oracle_view_ro LOGIN")
        for database, table in ((RP_DB, "res_partner"), (OLD_DB, "old_partner")):
            cls.sql("postgres", f"GRANT CONNECT ON DATABASE {database} TO oracle_view_ro")
            cls.sql(database, f"GRANT SELECT ON {table} TO oracle_view_ro")

    @classmethod
    def drop_old_ro(cls):  # oracle_view_ro and its rights, when it exists: each test can run alone
        if cls.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'") == "1":
            for database in cls.sql("postgres", "SELECT datname FROM pg_database WHERE NOT datistemplate").split():
                cls.sql(database, "DROP OWNED BY oracle_view_ro")
            cls.sql("postgres", "DROP ROLE oracle_view_ro")

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

    def tool(self, query, database=RP_DB):  # as the tool signs in: over TCP with the password of the image
        return subprocess.run([str(PG_BIN / "psql"), "-X", "-At", "-c", query,
                               f"host=127.0.0.1 port={self.PORT} dbname={database} user=oracle_view_rp sslmode=prefer"],
                              env=dict(os.environ, PGPASSWORD=TEST_PASSWORD), capture_output=True, text=True,
                              timeout=60)

    def run_script(self, *args, path=None):
        env = dict(os.environ, PATH=f"{path}:{os.environ['PATH']}") if path else None
        return subprocess.run(["bash", str(SCRIPT), *args, "--pg-port", self.PORT], cwd=self.tmp, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def make_role(self, path=None):
        verifier = self.tmp / "rp_grey.verifier"
        verifier.write_text(VERIFIER + "\n")
        return self.run_script("role", "--verifier-file", str(verifier), "--app-ip", self.APP, path=path)

    def rights_in(self, database, role="oracle_view_rp"):
        """What gives the role a right in or on the database: grants on it, and grants, default privileges or
        objects in it."""
        found = self.sql(database, f"""
            SELECT (SELECT count(*) FROM pg_database d, aclexplode(d.datacl) a
                     WHERE d.datname = current_database() AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '{role}'))
                   || ' ' || (SELECT count(*) FROM pg_shdepend s
                               WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                                 AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = '{role}'))""")
        on, inside = found.split()
        return int(on), int(inside)

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

    def test_role_rights_pg_hba_check_and_drop_on_a_real_server(self):
        self.make_old_ro()  # an earlier test of the class (by name) can have dropped it
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'"), "1")
        self.addCleanup(self.reload)
        self.addCleanup(self.write_hba, self.hba_lines)
        verifier = self.tmp / "rp_grey.verifier"
        result = self.make_role()
        output = result.stdout + result.stderr
        # The ::1 line above is IPv6: it cannot match the App server, so this is NOT OK, not CHECK BY HAND.
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("RESULT: NOT OK", result.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
        self.assertIn("OK   no right on a database other than perodua_oracle2\n", result.stdout)
        # The two INFO lines: the role owns nothing outside ovb_stage, and odoo also owns perodua_oracle.
        self.assertIn("INFO owns 0 objects outside the schema ovb_stage\n"
                      "INFO other databases of odoo, with its rights through the membership: perodua_oracle\n", result.stdout)
        self.assertEqual(self.sql("postgres", "SELECT rolpassword FROM pg_authid WHERE rolname = 'oracle_view_rp'"), VERIFIER)
        # oracle_view_ro had rights in both databases: both are cleaned, so it could be dropped.
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'"), "0")
        log = self.server_log.read_text(errors="replace")
        self.assertIn("SET log_statement = 'none'", log)  # the log records statements
        for setting, value in (("log_min_duration_sample", "0"), ("log_statement_sample_rate", "1"),
                               ("log_transaction_sample_rate", "1")):  # the server also logs by sampling
            self.assertEqual(self.sql("postgres", f"SHOW {setting}"), value)
        self.assertNotIn(STORED_KEY, log)
        self.assertNotIn(STORED_KEY, output)
        # Each right is checked alone: without DELETE on one table, the line for INSERT, UPDATE and DELETE fails.
        self.sql(RP_DB, "REVOKE DELETE ON res_partner FROM oracle_view_rp")
        missing = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn("FAIL INSERT, UPDATE and DELETE on every table of public (1 without)\n", missing.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", missing.stdout, re.M)), 14, missing.stdout)
        self.assertIn("Nothing in pg_hba.conf was checked", missing.stderr)
        self.assertNotIn("RESULT:", missing.stdout)
        self.sql(RP_DB, "GRANT DELETE ON res_partner TO oracle_view_rp")
        # A table that Odoo makes later gets the rights too (default privileges).
        self.sql(RP_DB, "SET ROLE odoo; CREATE TABLE later_table (id int)")
        self.assertEqual(self.tool("SHOW default_transaction_read_only").stdout.strip(), "on")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        self.assertEqual(self.tool("SELECT count(*) FROM partner_names").stdout.strip(), "1")
        failed = self.tool("UPDATE res_partner SET name = name")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("read-only transaction", failed.stderr)
        written = self.tool("BEGIN READ WRITE; UPDATE res_partner SET name = name; INSERT INTO later_table VALUES (1);"
                            " SELECT nextval('res_partner_id_seq'); ROLLBACK")
        self.assertEqual(written.returncode, 0, written.stderr)
        # The rights of odoo, the owner of the database (owner decision of 2026-11-09), but no SET ROLE to it.
        for query in ("TRUNCATE res_partner", "ALTER TABLE res_partner ADD COLUMN x int", "CREATE TABLE mine (id int)"):
            with self.subTest(query=query):
                done = self.tool(f"BEGIN READ WRITE; {query}; ROLLBACK")
                self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        denied = self.tool("SET ROLE odoo")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("permission denied to set role", denied.stderr)
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
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM res_partner"), "1")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ovb_stage'"), "0")

    def with_the_line(self):  # the printed pg_hba.conf line at the top of the host lines
        self.addCleanup(self.reload)
        self.addCleanup(self.write_hba, self.hba_lines)
        self.write_hba(self.hba_lines[:2] + [HBA_LINE.format(self.APP)] + self.hba_lines[2:])
        self.reload()

    def old_release_rights(self):
        """The rights that the releases before grey-v1.0.2 gave the role, in the old database only."""
        self.sql("postgres", "GRANT CONNECT, TEMPORARY ON DATABASE perodua_oracle TO oracle_view_rp")
        self.sql(OLD_DB, "GRANT USAGE ON SCHEMA public TO oracle_view_rp;"
                         " GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO oracle_view_rp;"
                         " GRANT SELECT, USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public TO oracle_view_rp;"
                         " ALTER DEFAULT PRIVILEGES FOR ROLE odoo IN SCHEMA public GRANT SELECT ON TABLES TO oracle_view_rp")
        self.assertEqual(self.rights_in(OLD_DB), (2, 4))  # CONNECT, TEMPORARY; the schema, table, sequence, default

    def test_the_old_database_loses_the_rights_and_the_new_one_keeps_connect(self):
        # The role as the releases before grey-v1.0.2 left it: rights in perodua_oracle only, and a session there.
        self.assertEqual(self.make_role().returncode, 3)  # the verifier, never in a statement of this test
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.sql(RP_DB, "DROP OWNED BY oracle_view_rp")  # also its rights on every database
        self.old_release_rights()
        self.assertEqual(self.rights_in(RP_DB), (0, 0))
        self.assertEqual(self.tool("SELECT count(*) FROM old_partner", OLD_DB).stdout.strip(), "1")
        session = subprocess.Popen([str(PG_BIN / "psql"), "-X", "-At", "-c", "SELECT pg_sleep(120)",
                                    f"host=127.0.0.1 port={self.PORT} dbname={OLD_DB} user=oracle_view_rp"],
                                   env=dict(os.environ, PGPASSWORD=TEST_PASSWORD), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(session.kill)
        deadline = time.monotonic() + 30
        while self.sql("postgres", "SELECT count(*) FROM pg_stat_activity WHERE usename = 'oracle_view_rp' "
                                   "AND datname = 'perodua_oracle'") != "1":
            self.assertLess(time.monotonic(), deadline, "the session of the old release did not start")
            time.sleep(0.1)
        self.with_the_line()
        result = self.make_role()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("RESULT: OK", result.stdout)
        self.assertIn("The rights of oracle_view_rp in perodua_oracle, the database of the earlier releases", result.stdout)
        self.assertIn("OK   no right on a database other than perodua_oracle2\n", result.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
        # The session there ended; no grant, default privilege or object of the role is left there.
        deadline = time.monotonic() + 10
        while session.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIsNotNone(session.poll(), "the session of oracle_view_rp in perodua_oracle still runs")
        self.assertNotEqual(session.returncode, 0)
        self.assertIn("terminating connection due to administrator command", session.stderr.read())
        self.assertEqual(self.rights_in(OLD_DB), (0, 0))
        # odoo owns perodua_oracle too: the role has the rights of odoo there through its membership (owner decision
        # of 2026-11-09), and the INFO line names that database. No right of the role itself lets it in there.
        self.assertIn("INFO other databases of odoo, with its rights through the membership: perodua_oracle\n",
                      result.stdout)
        self.assertEqual(self.tool("SELECT count(*) FROM old_partner", OLD_DB).stdout.strip(), "1")
        self.sql("postgres", "REVOKE odoo FROM oracle_view_rp")
        self.assertEqual(self.sql(OLD_DB, "SELECT has_table_privilege('oracle_view_rp', 'old_partner', 'SELECT')"), "f")
        denied = self.tool("SELECT 1", OLD_DB)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("permission denied for database", denied.stderr)
        # perodua_oracle2 has CONNECT and TEMPORARY of the role itself (also without the membership), and the tool
        # reads it.
        self.assertEqual(self.sql("postgres", "SELECT has_database_privilege('oracle_view_rp', 'perodua_oracle2', 'CONNECT')"
                                              " AND has_database_privilege('oracle_view_rp', 'perodua_oracle2', 'TEMPORARY')"), "t")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        self.sql("postgres", "GRANT odoo TO oracle_view_rp WITH INHERIT TRUE, SET FALSE")
        # A right that another change gives the role later, in another database, stops role --check.
        self.sql("postgres", "CREATE TABLE made_up_elsewhere (id int); GRANT SELECT ON made_up_elsewhere TO oracle_view_rp")
        self.addCleanup(self.sql, "postgres", "DROP TABLE IF EXISTS made_up_elsewhere")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL no right on a database other than perodua_oracle2 (it has rights on: postgres)\n", result.stdout)
        self.assertIn(SAFE_FORM_HINT, result.stderr)
        self.assertNotIn("RESULT:", result.stdout)
        self.sql("postgres", "DROP TABLE made_up_elsewhere")
        self.assertEqual(self.run_script("role", "--check", "--app-ip", self.APP).returncode, 0)

    def stop_after(self, text):
        """A psql in front of the real one: it adds an error right after the first text in a SQL script."""
        return self.add_after(text, "SELECT 1/0;\n")

    def add_after(self, text, added):
        """A psql in front of the real one: it adds the lines right after the first text in a SQL script."""
        wrapper = self.tmp / ("add-" + hashlib.sha256((text + added).encode()).hexdigest()[:8])
        wrapper.mkdir(exist_ok=True)
        (wrapper / "psql").write_text(f"""#!/usr/bin/env python3
import os, subprocess, sys
real = {str(PG_BIN / "psql")!r}
if "-f" in sys.argv:
    script = sys.stdin.read().replace({text!r}, {text + added!r}, 1)
    sys.exit(subprocess.run([real, *sys.argv[1:]], input=script, text=True).returncode)
os.execv(real, [real, *sys.argv[1:]])
""")
        (wrapper / "psql").chmod(0o755)
        shutil.chown(wrapper, "postgres", "postgres")
        return wrapper

    def test_the_old_database_is_cleaned_in_one_transaction(self):
        # A stop after DROP OWNED BY in the old database must not leave the role without CONNECT on
        # perodua_oracle2: DROP OWNED BY also revokes it there.
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        # Without the membership in odoo, the owner of perodua_oracle2: CONNECT there is then the grant of the role
        # itself. Both stops come before the grant of the membership; the last run grants it again.
        self.sql("postgres", "REVOKE odoo FROM oracle_view_rp")
        self.old_release_rights()
        result = self.make_role(path=self.stop_after("DROP OWNED BY oracle_view_rp;\n"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("division by zero", result.stdout + result.stderr)
        # Nothing of the transaction stays: the role keeps CONNECT on perodua_oracle2, and its old rights too.
        self.assertEqual(self.sql("postgres", "SELECT has_database_privilege('oracle_view_rp', 'perodua_oracle2', 'CONNECT')"), "t")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        self.assertEqual(self.rights_in(OLD_DB), (2, 4))
        # A stop right after the transaction: it removed the old rights and gave CONNECT on perodua_oracle2
        # again, so the tool keeps its way in before the later grants of the run.
        result = self.make_role(path=self.stop_after("COMMIT;\n"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("division by zero", result.stdout + result.stderr)
        self.assertEqual(self.rights_in(OLD_DB), (0, 0))
        self.assertEqual(self.sql("postgres", "SELECT has_database_privilege('oracle_view_rp', 'perodua_oracle2', 'CONNECT')"), "t")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        # The next run without a stop ends as usual.
        self.old_release_rights()
        self.assertEqual(self.make_role().returncode, 3)
        self.assertEqual(self.rights_in(OLD_DB), (0, 0))
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_auth_members WHERE member = 'oracle_view_rp'::regrole"
                                              " AND roleid = 'odoo'::regrole"), "1")

    def test_the_old_database_session_that_opens_before_the_commit_ends(self):
        # A container of an earlier release can open a session in the old database while the transaction
        # runs. The sessions there end again after the COMMIT, and after it no new one can start with a right
        # of the role (the membership in odoo, the owner of both databases, is taken away for that check).
        self.drop_old_ro()
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.old_release_rights()
        started, log, starter = self.tmp / "session-started", self.tmp / "session.log", self.tmp / "start-session.sh"
        psql = PG_BIN / "psql"
        starter.write_text(f"""#!/bin/sh
PGPASSWORD='{TEST_PASSWORD}' {psql} -X -At -c 'SELECT pg_sleep(120)' \\
    'host=127.0.0.1 port={self.PORT} dbname={OLD_DB} user=oracle_view_rp' < /dev/null > {log} 2>&1 9>&- &
i=0
until [ "$({psql} -X -At -h /var/run/postgresql -p {self.PORT} -d postgres -c "SELECT count(*) FROM pg_stat_activity
           WHERE usename = 'oracle_view_rp' AND datname = '{OLD_DB}'")" = 1 ]; do
    i=$((i + 1)); [ $i -lt 300 ] || exit 1; sleep 0.1
done
touch {started}
""")
        starter.chmod(0o755)
        self.addCleanup(self.sql, "postgres", "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                                              " WHERE usename = 'oracle_view_rp'")
        result = self.make_role(path=self.add_after("BEGIN;\n", f"\\! {starter}\n"))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertTrue(started.exists(), "the session in perodua_oracle did not start before the COMMIT")
        deadline = time.monotonic() + 10
        while "terminating connection" not in log.read_text() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_stat_activity WHERE usename = 'oracle_view_rp'"
                                              f" AND datname = '{OLD_DB}'"), "0",
                         "a session of oracle_view_rp that opened before the COMMIT still runs in perodua_oracle")
        self.assertIn("terminating connection due to administrator command", log.read_text())
        self.assertEqual(self.rights_in(OLD_DB), (0, 0))
        self.sql("postgres", "REVOKE odoo FROM oracle_view_rp")
        denied = self.tool("SELECT 1", OLD_DB)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("permission denied for database", denied.stderr)
        self.sql("postgres", "GRANT odoo TO oracle_view_rp WITH INHERIT TRUE, SET FALSE")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")

    def test_the_old_database_without_connections_is_skipped(self):
        # A database that refuses connections must not stop role and drop-role at their \connect there on
        # each run. Check 12 still finds the rights that stay in it (pg_shdepend).
        self.drop_old_ro()
        for refuse, accept in (("ALTER DATABASE perodua_oracle ALLOW_CONNECTIONS false",
                                "ALTER DATABASE perodua_oracle ALLOW_CONNECTIONS true"),
                               # PostgreSQL 16 marks a database that DROP DATABASE did not finish with -2.
                               ("UPDATE pg_database SET datconnlimit = -2 WHERE datname = 'perodua_oracle'",
                                "UPDATE pg_database SET datconnlimit = -1 WHERE datname = 'perodua_oracle'")):
            with self.subTest(refuse=refuse):
                try:
                    self.assertEqual(self.make_role().returncode, 3)
                    self.old_release_rights()
                    self.sql("postgres", "REVOKE CONNECT, TEMPORARY ON DATABASE perodua_oracle FROM oracle_view_rp")
                    self.assertEqual(self.rights_in(OLD_DB), (0, 4))  # in it only: no grant on the database
                    self.sql("postgres", refuse)
                    result = self.make_role()
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn("The rights of oracle_view_rp in perodua_oracle", result.stdout)
                    self.assertIn("FAIL no right on a database other than perodua_oracle2 (it has rights on: "
                                  "perodua_oracle)\n", result.stdout)
                    self.assertNotIn("RESULT:", result.stdout)
                    self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
                    # Without rights there, role and drop-role skip it and succeed.
                    self.sql("postgres", accept)
                    self.assertEqual(self.make_role().returncode, 3)
                    self.assertEqual(self.rights_in(OLD_DB), (0, 0))
                    self.sql("postgres", refuse)
                    result = self.make_role()
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
                    result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("role oracle_view_rp dropped", result.stdout)
                finally:
                    self.sql("postgres", accept)
                    self.run_script("drop-role", "--confirm", "oracle_view_rp")

    def test_the_rights_check_finds_a_grant_on_another_database(self):
        # A grant ON a database is only in the ACL of that database (aclexplode): pg_shdepend records it
        # for the shared object, not as a dependency in that database.
        self.drop_old_ro()
        self.with_the_line()
        self.assertEqual(self.make_role().returncode, 0)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.sql("postgres", "CREATE DATABASE made_up_third OWNER odoo")
        self.addCleanup(self.sql, "postgres", "DROP DATABASE IF EXISTS made_up_third")
        self.sql("postgres", "REVOKE ALL ON DATABASE made_up_third FROM PUBLIC")
        self.sql("postgres", "GRANT CONNECT ON DATABASE made_up_third TO oracle_view_rp")
        self.assertEqual(self.rights_in("made_up_third"), (1, 0))
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL no right on a database other than perodua_oracle2 (it has rights on: made_up_third)\n",
                      result.stdout)
        self.assertNotIn("RESULT:", result.stdout)
        # The printed statement, run in that database, removes the grant and keeps CONNECT on perodua_oracle2.
        self.assertIn(SAFE_FORM_HINT, result.stderr)
        self.sql("made_up_third", SAFE_FORM)
        self.assertEqual(self.rights_in("made_up_third"), (0, 0))
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK   no right on a database other than perodua_oracle2\n", result.stdout)

    def test_the_old_database_may_be_missing(self):
        self.drop_old_ro()
        self.sql("postgres", "ALTER DATABASE perodua_oracle RENAME TO made_up_away")
        self.addCleanup(self.sql, "postgres", "ALTER DATABASE made_up_away RENAME TO perodua_oracle")
        self.sql("postgres", "CREATE ROLE oracle_view_ro LOGIN")  # an earlier bundle, with rights in perodua_oracle2
        self.addCleanup(self.drop_old_ro)
        self.sql("postgres", "GRANT CONNECT ON DATABASE perodua_oracle2 TO oracle_view_ro")
        self.sql(RP_DB, "GRANT SELECT ON res_partner TO oracle_view_ro")
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.with_the_line()
        result = self.make_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("The rights of oracle_view_rp in perodua_oracle", result.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'"), "0")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "0")

    def test_the_drop_cleans_both_databases(self):
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.old_release_rights()  # rights in perodua_oracle that an earlier change left
        self.make_old_ro()
        self.addCleanup(self.drop_old_ro)
        self.assertNotEqual(self.rights_in(RP_DB), (0, 0))
        self.assertNotEqual(self.rights_in(RP_DB, "oracle_view_ro"), (0, 0))
        self.assertNotEqual(self.rights_in(OLD_DB, "oracle_view_ro"), (0, 0))
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("role oracle_view_rp dropped", result.stdout)
        self.assertIn("role oracle_view_ro of an earlier bundle dropped", result.stdout)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname IN ('oracle_view_rp', 'oracle_view_ro')"), "0")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM res_partner"), "1")
        self.assertEqual(self.sql(OLD_DB, "SELECT count(*) FROM old_partner"), "1")

    def membership(self):
        """Each grant of odoo to oracle_view_rp (pg_auth_members): "grantor inherit set admin"."""
        return self.sql("postgres", "SELECT coalesce(string_agg(pg_get_userbyid(grantor) || ' ' || inherit_option::text"
                                    " || ' ' || set_option::text || ' ' || admin_option::text, ', '), 'none')"
                                    " FROM pg_auth_members WHERE roleid = 'odoo'::regrole"
                                    " AND member = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')")

    def test_the_role_has_the_rights_of_the_owner_of_the_database(self):
        # Owner decision of 2026-11-09: oracle_view_rp is a member of odoo, the owner of perodua_oracle2, with INHERIT
        # and without SET and ADMIN. The tool makes tables of its own in public and changes the Odoo tables, but it
        # cannot SET ROLE to odoo.
        self.drop_old_ro()
        self.with_the_line()
        result = self.make_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.assertIn("OK   member of odoo, the owner of perodua_oracle2, with INHERIT TRUE, SET FALSE, ADMIN FALSE\n",
                      result.stdout)
        self.assertEqual(self.membership(), "postgres true false false")
        # A second run keeps the one grant, and prints no NOTICE for it.
        result = self.make_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.membership(), "postgres true false false")
        self.assertNotIn("already been granted", result.stdout + result.stderr)
        self.addCleanup(self.sql, RP_DB, "DROP TABLE IF EXISTS public.console_made")
        made = self.tool("BEGIN READ WRITE; CREATE TABLE public.console_made (id int); INSERT INTO console_made VALUES (1);"
                         " COMMIT")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.sql(RP_DB, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'console_made'"),
                         "oracle_view_rp")
        changed = self.tool("BEGIN READ WRITE; ALTER TABLE res_partner ADD COLUMN made_up int; TRUNCATE res_partner;"
                            " ROLLBACK")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        for query in ("SET ROLE odoo", "ALTER TABLE console_made OWNER TO odoo"):
            with self.subTest(query=query):
                denied = self.tool(f"BEGIN READ WRITE; {query}; ROLLBACK")
                self.assertNotEqual(denied.returncode, 0)
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INFO owns 1 objects outside the schema ovb_stage (table public.console_made)\n", result.stdout)
        # role --check fails without the membership, with SET or with ADMIN.
        self.sql("postgres", "REVOKE odoo FROM oracle_view_rp")
        member = "member of odoo, the owner of perodua_oracle2, with INHERIT TRUE, SET FALSE, ADMIN FALSE"
        for grant, has in (("", "it is not a member"), ("GRANT odoo TO oracle_view_rp WITH INHERIT TRUE, SET TRUE",
                                                         "it has INHERIT TRUE, SET TRUE, ADMIN FALSE")):
            with self.subTest(grant=grant):
                if grant:
                    self.sql("postgres", grant)
                result = self.run_script("role", "--check", "--app-ip", self.APP)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"FAIL {member} ({has})\n", result.stdout)
                self.assertIn(MEMBER_HINT, result.stderr)
                self.assertNotIn("RESULT:", result.stdout)
        self.assertEqual(self.tool("SET ROLE odoo").returncode, 0)  # what SET TRUE allows
        # role sets SET FALSE again on its own grant (the same grantor).
        self.assertEqual(self.make_role().returncode, 0)
        self.assertEqual(self.membership(), "postgres true false false")
        # It does not change ADMIN: role --check names it until it is revoked by hand.
        self.sql("postgres", "GRANT odoo TO oracle_view_rp WITH ADMIN TRUE")
        self.assertEqual(self.make_role().returncode, 1)
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"FAIL {member} (it has INHERIT TRUE, SET FALSE, ADMIN TRUE)\n", result.stdout)
        self.sql("postgres", "REVOKE ADMIN OPTION FOR odoo FROM oracle_view_rp")
        self.assertEqual(self.run_script("role", "--check", "--app-ip", self.APP).returncode, 0)
        # A path through another role: the direct grant is right, but the bridge gives SET ROLE odoo, or ADMIN on
        # odoo. The INFO line names the bridge.
        self.sql("postgres", "CREATE ROLE made_up_bridge NOLOGIN")
        self.addCleanup(self.sql, "postgres", "DROP ROLE IF EXISTS made_up_bridge")
        for grants, has in ((("GRANT odoo TO made_up_bridge WITH INHERIT TRUE, SET TRUE",
                              "GRANT made_up_bridge TO oracle_view_rp WITH INHERIT TRUE, SET TRUE"),
                             "it can SET ROLE to odoo through another role"),
                            (("GRANT odoo TO made_up_bridge WITH ADMIN TRUE, INHERIT TRUE, SET FALSE",
                              "GRANT made_up_bridge TO oracle_view_rp WITH INHERIT TRUE, SET FALSE"),
                             "it has ADMIN on odoo through another role")):
            with self.subTest(has=has):
                for grant in grants:
                    self.sql("postgres", grant)
                self.assertEqual(self.membership(), "postgres true false false")
                result = self.run_script("role", "--check", "--app-ip", self.APP)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"FAIL {member} ({has})\n", result.stdout)
                self.assertIn("INFO other roles of which it is a member (also through odoo): made_up_bridge\n",
                              result.stdout)
                self.assertIn(MEMBER_HINT, result.stderr)
                self.assertEqual(self.tool("SET ROLE odoo").returncode == 0, "SET ROLE" in has)
                self.sql("postgres", "REVOKE made_up_bridge FROM oracle_view_rp; REVOKE odoo FROM made_up_bridge")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INFO other roles of which it is a member (also through odoo): none\n", result.stdout)
        # drop-role drops nothing that the role made outside ovb_stage: it gives each object to odoo, the owner of
        # the database, names it, and drops the role.
        self.addCleanup(self.sql, RP_DB, "DROP SCHEMA IF EXISTS console_schema CASCADE")
        made = self.tool("BEGIN READ WRITE; CREATE SCHEMA console_schema; CREATE TABLE console_schema.notes"
                         " (id serial, note text); INSERT INTO console_schema.notes (note) VALUES ('made-up note'); COMMIT")
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4 objects of oracle_view_rp stay in perodua_oracle2, now owned by odoo, the owner of the database:\n"
                      "    schema console_schema\n    sequence console_schema.notes_id_seq\n"
                      "    table console_schema.notes\n    table public.console_made\nrole oracle_view_rp dropped\n",
                      result.stdout)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "0")
        self.assertEqual(self.sql(RP_DB, "SELECT string_agg(c.relname || ' ' || pg_get_userbyid(c.relowner), ', ' ORDER BY"
                                         " c.relname) FROM pg_class c WHERE c.relname IN ('console_made', 'notes',"
                                         " 'notes_id_seq')"), "console_made odoo, notes odoo, notes_id_seq odoo")
        self.assertEqual(self.sql(RP_DB, "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'console_schema'"),
                         "odoo")
        self.assertEqual(self.sql(RP_DB, "SELECT (SELECT count(*) FROM public.console_made) || ' '"
                                         " || (SELECT note FROM console_schema.notes)"), "1 made-up note")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ovb_stage'"), "0")

    def test_role_and_drop_role_stop_for_a_superuser_owner_or_the_role_as_the_owner(self):
        # The role gets the rights of the owner of perodua_oracle2: of a superuser, that is every right on the server,
        # and a role cannot be a member of itself. Its objects in perodua_oracle2 and perodua_oracle go to the owner
        # of that database, never to the role itself (else DROP OWNED BY would drop them). role and drop-role stop
        # before any change. A superuser makes the owner here; a REASSIGN OWNED BY odoo TO oracle_view_rp of the SQL
        # console can do the same.
        self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging")  # each test can run alone
        self.make_old_ro()
        self.addCleanup(self.drop_old_ro)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        # The first cleanups.
        self.addCleanup(self.sql, "postgres", "ALTER DATABASE perodua_oracle OWNER TO odoo")
        self.addCleanup(self.sql, "postgres", "ALTER DATABASE perodua_oracle2 OWNER TO odoo")
        self.sql("postgres", "ALTER DATABASE perodua_oracle2 OWNER TO postgres")
        result = self.make_role()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("The owner of perodua_oracle2 is postgres, a superuser.", result.stdout)
        self.assertIn("Nothing was changed.", result.stdout)
        self.assertIn("Creating or updating the role oracle_view_rp failed (above)", result.stderr)
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "0")
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_ro'"), "1")
        # The role itself as the owner (only a superuser can give it the database).
        self.sql("postgres", "ALTER DATABASE perodua_oracle2 OWNER TO odoo")
        self.assertEqual(self.make_role().returncode, 3)
        self.sql("postgres", "ALTER ROLE oracle_view_rp CONNECTION LIMIT 3")
        self.sql("postgres", "ALTER DATABASE perodua_oracle2 OWNER TO oracle_view_rp")
        result = self.make_role()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("oracle_view_rp owns perodua_oracle2. A different owner is needed first (the role of the App, not a "
                      "superuser): a role\ncannot be a member of itself, and the objects of oracle_view_rp go to the owner "
                      "of the database. Nothing was changed.\n", result.stdout)
        self.assertEqual(self.sql("postgres", "SELECT rolconnlimit FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "3")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL member of oracle_view_rp, the owner of perodua_oracle2, with INHERIT TRUE, SET FALSE, ADMIN "
                      "FALSE (oracle_view_rp owns the database)\n", result.stdout)
        # drop-role stops before any change too: the role keeps its LOGIN, ovb_stage and the Odoo table stay.
        self.addCleanup(self.sql, RP_DB, "DROP TABLE IF EXISTS public.console_kept")
        self.sql(RP_DB, "CREATE TABLE public.console_kept (id int); ALTER TABLE public.console_kept OWNER TO oracle_view_rp")
        for owned in ("perodua_oracle2", "perodua_oracle"):
            with self.subTest(owned=owned):
                self.sql("postgres", "ALTER DATABASE perodua_oracle2 OWNER TO "
                         + ("oracle_view_rp" if owned == "perodua_oracle2" else "odoo"))
                self.sql("postgres", "ALTER DATABASE perodua_oracle OWNER TO "
                         + ("oracle_view_rp" if owned == "perodua_oracle" else "odoo"))
                if owned == "perodua_oracle":  # role stops for the old database too
                    result = self.make_role()
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("oracle_view_rp owns perodua_oracle. A different owner is needed first", result.stdout)
                result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"oracle_view_rp owns {owned}. A different owner is needed first (the role of the App): "
                              "drop-role gives the\nobjects of oracle_view_rp to the owner of the database and never drops "
                              "them. Nothing was changed.\n", result.stdout)
                self.assertIn("Dropping the role oracle_view_rp failed (above)", result.stderr)
                self.assertEqual(self.sql("postgres", "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'"),
                                 "t")
                self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ovb_stage'"), "1")
                self.assertEqual(self.sql(RP_DB, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = "
                                                 "'console_kept'"), "oracle_view_rp")
        # With the owner of before, drop-role gives the table to it and drops the role.
        self.sql("postgres", "ALTER DATABASE perodua_oracle OWNER TO odoo")
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("    table public.console_kept\n", result.stdout)
        self.assertEqual(self.sql(RP_DB, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'console_kept'"),
                         "odoo")

    def test_the_objects_of_the_role_in_the_old_database_stay(self):
        # odoo owns perodua_oracle too: through its membership in odoo the role can make a table there. role and
        # drop-role give such a table to odoo before their DROP OWNED BY there, name it, and drop none of it.
        self.drop_old_ro()
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.addCleanup(self.sql, OLD_DB, "DROP TABLE IF EXISTS public.console_old, public.console_old_late")
        made = self.tool("BEGIN READ WRITE; CREATE TABLE console_old (id int); INSERT INTO console_old VALUES (1); COMMIT",
                         OLD_DB)
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.make_role()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("The rights of oracle_view_rp in perodua_oracle, the database of the earlier releases", result.stdout)
        self.assertIn("1 objects of oracle_view_rp stay in perodua_oracle, now owned by odoo, the owner of the database:\n"
                      "    table public.console_old\n", result.stdout)
        self.assertEqual(self.sql(OLD_DB, "SELECT pg_get_userbyid(relowner) || ' ' || (SELECT count(*) FROM console_old)"
                                          " FROM pg_class WHERE relname = 'console_old'"), "odoo 1")
        self.assertEqual(self.rights_in(OLD_DB), (0, 0))
        # A third run has nothing to give: no line for it.
        result = self.make_role()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("objects of oracle_view_rp stay in", result.stdout)
        # drop-role gives the objects of the role there to odoo too.
        made = self.tool("BEGIN READ WRITE; CREATE TABLE console_old_late (id int); COMMIT", OLD_DB)
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 objects of oracle_view_rp stay in perodua_oracle, now owned by odoo, the owner of the database:\n"
                      "    table public.console_old_late\nrole oracle_view_rp dropped\n", result.stdout)
        self.assertEqual(self.sql(OLD_DB, "SELECT string_agg(relname || ' ' || pg_get_userbyid(relowner), ', ' ORDER BY"
                                          " relname) FROM pg_class WHERE relname LIKE 'console_old%'"),
                         "console_old odoo, console_old_late odoo")
        self.assertEqual(self.sql(OLD_DB, "SELECT count(*) FROM console_old"), "1")

    def stage_tables(self):
        return self.sql(RP_DB, "SELECT coalesce(string_agg(relname, ' ' ORDER BY relname), '') FROM pg_class"
                               " WHERE relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'ovb_stage')"
                               " AND relkind = 'r'")

    def test_the_staging_schema_of_copy_to_rp(self):
        # Copy to RP (copy design, section 4.3): the role owns the schema ovb_stage and makes its staging tables
        # there. With the rights of the owner of the database (owner decision of 2026-11-09) it can also make
        # tables in public and new schemas: those are no staging tables, and only an INFO line counts them.
        self.drop_old_ro()
        self.with_the_line()
        result = self.make_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
        for line in ("INFO owns 0 objects outside the schema ovb_stage\n",
                     "OK   owns the schema ovb_stage\n", "OK   USAGE and CREATE on the schema ovb_stage\n",
                     "OK   no right of PUBLIC on the schema ovb_stage\n", "OK   CREATE in public\n",
                     "OK   TRUNCATE on every table of public (0 without)\n",
                     "== Staging tables of Copy to RP: the schema ovb_stage of perodua_oracle2\nnone: ovb_stage holds no table\n"):
            self.assertIn(line, result.stdout)
        self.assertEqual(self.sql(RP_DB, "SELECT pg_get_userbyid(nspowner) || ' ' || nspacl::text FROM pg_namespace"
                                         " WHERE nspname = 'ovb_stage'"), "oracle_view_rp {oracle_view_rp=UC/oracle_view_rp}")
        self.assertEqual(self.sql(RP_DB, "SELECT has_schema_privilege('odoo', 'ovb_stage', 'USAGE')"), "f")
        # A table made after the check of drop-role: the drop stops, the role gets its LOGIN back, nothing goes.
        late = self.add_after("\\connect :ovb_db\n", "CREATE TABLE ovb_stage.made_late (id int);\n")
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", path=late)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("none: ovb_stage holds no table\n", result.stdout)
        self.assertIn("ovb_stage holds 1 staging tables now. Nothing was dropped.", result.stdout)
        self.assertIn("run drop-role again, with --drop-staging to drop them", result.stderr)
        self.assertEqual(self.sql("postgres", "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "t")
        self.assertEqual(self.stage_tables(), "made_late")
        self.assertEqual(self.tool("SELECT count(*) FROM res_partner").stdout.strip(), "1")
        self.sql(RP_DB, "DROP TABLE ovb_stage.made_late")
        # The tool makes a staging table in a READ WRITE transaction. It can also write outside ovb_stage, with the
        # rights of odoo (rolled back here).
        made = self.tool("BEGIN READ WRITE; CREATE TABLE ovb_stage.copy_parts AS SELECT g AS id, repeat('x', 200) AS name"
                         " FROM generate_series(1, 2000) g; CREATE INDEX ON ovb_stage.copy_parts (id); COMMIT")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.tool("SELECT count(*) FROM ovb_stage.copy_parts").stdout.strip(), "2000")
        for query in ("CREATE TABLE public.mine (id int)", "CREATE TABLE mine (id int)", "TRUNCATE res_partner",
                      "CREATE SCHEMA made_up_more", "ALTER TABLE res_partner ADD COLUMN x int"):
            with self.subTest(query=query):
                done = self.tool(f"BEGIN READ WRITE; {query}; ROLLBACK")
                self.assertEqual(done.returncode, 0, done.stderr)
        with self.assertRaises(subprocess.CalledProcessError):  # PUBLIC, and so Odoo, has no right there
            self.sql(RP_DB, "SET ROLE odoo; SELECT count(*) FROM ovb_stage.copy_parts")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
        self.assertRegex(result.stdout, r"\n1 tables, [0-9]+ kB on disk:\n    table +size  owner\n"
                                        r"    copy_parts +[0-9]+ kB  oracle_view_rp\nTo free the disk")
        # An object of the role outside ovb_stage is information: role --check names it and does not stop.
        self.sql(RP_DB, "CREATE FUNCTION public.made_up_f() RETURNS int LANGUAGE sql AS 'SELECT 1';"
                        " ALTER FUNCTION public.made_up_f() OWNER TO oracle_view_rp")
        self.addCleanup(self.sql, RP_DB, "DROP FUNCTION IF EXISTS public.made_up_f()")
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("INFO owns 1 objects outside the schema ovb_stage (function public.made_up_f())\n", result.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 15, result.stdout)
        self.assertIn("RESULT: OK", result.stdout)
        self.sql(RP_DB, "DROP FUNCTION public.made_up_f()")
        # drop-role stops before any change while ovb_stage holds tables, and names the choice.
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"    copy_parts +[0-9]+ kB  oracle_view_rp\n")
        self.assertIn("run drop-role again with --drop-staging. Nothing was changed.", result.stderr)
        self.assertEqual(self.stage_tables(), "copy_parts")
        self.assertEqual(self.tool("SELECT count(*) FROM ovb_stage.copy_parts").stdout.strip(), "2000")
        # With --drop-staging: the schema, its tables and the role go; the rows of the Odoo tables stay.
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"schema ovb_stage dropped, with its 1 staging tables and their data \([0-9]+ kB\)\n")
        self.assertIn("role oracle_view_rp dropped", result.stdout)
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ovb_stage'"), "0")
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "0")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM res_partner"), "1")

    def test_drop_role_keeps_what_depends_on_the_staging_schema(self):
        # A materialized view of the role in ovb_stage holds data: it counts as a staging table. An object
        # outside ovb_stage that depends on a staging object stops drop-role, also with --drop-staging, because
        # DROP SCHEMA ovb_stage CASCADE would drop it too.
        self.drop_old_ro()
        self.with_the_line()
        self.assertEqual(self.make_role().returncode, 0)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
        made = self.tool("BEGIN READ WRITE; CREATE MATERIALIZED VIEW ovb_stage.parts_snap AS SELECT g AS id"
                         " FROM generate_series(1, 500) g; COMMIT")
        self.assertEqual(made.returncode, 0, made.stderr)
        result = self.run_script("role", "--check", "--app-ip", self.APP)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"\n1 tables, [0-9]+ kB on disk:\n    table +size  owner\n"
                                        r"    parts_snap +[0-9]+ kB  oracle_view_rp\n")
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("The schema ovb_stage holds 1 staging tables (above).", result.stderr)
        self.assertEqual(self.tool("SELECT count(*) FROM ovb_stage.parts_snap").stdout.strip(), "500")
        # A view and a foreign key in public on a staging table: drop-role stops before the confirmation.
        made = self.tool("BEGIN READ WRITE; CREATE TABLE ovb_stage.copy_keys (id int PRIMARY KEY);"
                         " INSERT INTO ovb_stage.copy_keys SELECT generate_series(1, 3); COMMIT")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.sql(RP_DB, "CREATE VIEW public.v_copy AS SELECT id FROM ovb_stage.copy_keys;"
                        " CREATE TABLE public.t_ref (id int CONSTRAINT copy_fk REFERENCES ovb_stage.copy_keys (id));"
                        " INSERT INTO public.t_ref VALUES (1)")
        self.addCleanup(self.sql, RP_DB, "DROP VIEW IF EXISTS public.v_copy; DROP TABLE IF EXISTS public.t_ref")
        for args in (["--drop-staging"], []):
            with self.subTest(args=args):
                result = self.run_script("drop-role", "--confirm", "oracle_view_rp", *args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("so it would drop them too, with their data:\n"
                              "    table constraint copy_fk on public.t_ref\n    view public.v_copy\n", result.stderr)
                self.assertIn("Objects outside the schema ovb_stage depend on it (above). Nothing was changed.",
                              result.stderr)
        self.assertEqual(self.sql("postgres", "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "t")
        # A dependency made after the check of the script: the SQL stops, gives LOGIN back and drops nothing.
        self.sql(RP_DB, "DROP VIEW public.v_copy")
        self.sql(RP_DB, "ALTER TABLE public.t_ref DROP CONSTRAINT copy_fk")
        late = self.add_after("\\connect :ovb_db\n", "CREATE VIEW public.v_late AS SELECT id FROM ovb_stage.copy_keys;\n")
        self.addCleanup(self.sql, RP_DB, "DROP VIEW IF EXISTS public.v_late")
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging", path=late)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("1 objects outside ovb_stage depend on it: view public.v_late\nNothing was dropped.\n", result.stdout)
        self.assertIn("DROP SCHEMA ovb_stage CASCADE would drop them", result.stderr)
        self.assertEqual(self.sql("postgres", "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "t")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM public.v_late"), "3")
        self.assertEqual(self.stage_tables(), "copy_keys")
        self.assertEqual(self.tool("SELECT count(*) FROM ovb_stage.parts_snap").stdout.strip(), "500")
        # Without the dependency, --drop-staging drops the schema with the table and the materialized view.
        self.sql(RP_DB, "DROP VIEW public.v_late")
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"schema ovb_stage dropped, with its 2 staging tables and their data \([0-9]+ kB\)\n")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ovb_stage'"), "0")
        self.assertEqual(self.sql(RP_DB, "SELECT count(*) FROM public.t_ref"), "1")

    def test_a_staging_schema_of_another_owner_stays(self):
        self.drop_old_ro()
        self.sql(RP_DB, "CREATE SCHEMA ovb_stage AUTHORIZATION odoo; SET ROLE odoo; CREATE TABLE ovb_stage.odoo_table (id int)")
        self.addCleanup(self.sql, RP_DB, "DROP SCHEMA IF EXISTS ovb_stage CASCADE")
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        result = self.make_role()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("The schema ovb_stage of perodua_oracle2 belongs to another role: it stays as it is", result.stdout)
        self.assertIn("FAIL owns the schema ovb_stage (the owner of ovb_stage is odoo)\n", result.stdout)
        # No grant there, but odoo owns that schema: the role has its USAGE and CREATE through the membership.
        self.assertIn("OK   USAGE and CREATE on the schema ovb_stage\n", result.stdout)
        self.assertEqual(len(re.findall(r"^OK   ", result.stdout, re.M)), 14, result.stdout)
        self.assertIn("find out who made it before you change its owner or drop it", result.stderr)
        self.assertNotIn("RESULT:", result.stdout)
        self.assertEqual(self.sql(RP_DB, "SELECT pg_get_userbyid(nspowner) || ' ' || coalesce(nspacl::text, 'default')"
                                         " FROM pg_namespace WHERE nspname = 'ovb_stage'"), "odoo default")
        # With the rights of odoo, the owner of that schema, the tool makes a staging table there.
        made = self.tool("BEGIN READ WRITE; CREATE TABLE ovb_stage.rp_copy (id int); COMMIT")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.stage_tables(), "odoo_table rp_copy")
        # drop-role lists the table of the role there and stops without --drop-staging; with it, only that table
        # goes. The schema and the table of odoo stay: they are not of the role.
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"    rp_copy +[0-9]+ (bytes|kB)  oracle_view_rp\n")
        self.assertIn("The schema ovb_stage holds 1 staging tables (above).", result.stderr)
        result = self.run_script("drop-role", "--confirm", "oracle_view_rp", "--drop-staging")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("schema ovb_stage of another owner stays: 1 staging tables of oracle_view_rp in it dropped", result.stdout)
        self.assertNotIn("objects of oracle_view_rp stay in", result.stdout)
        self.assertEqual(self.stage_tables(), "odoo_table")
        self.assertEqual(self.sql("postgres", "SELECT count(*) FROM pg_roles WHERE rolname = 'oracle_view_rp'"), "0")

    def test_the_pg_hba_check_reads_each_kind_of_rule(self):
        self.write_hba(self.hba_lines)
        self.reload()
        self.assertEqual(self.make_role().returncode, 3)
        self.addCleanup(self.run_script, "drop-role", "--confirm", "oracle_view_rp")
        self.addCleanup(self.reload, ssl=False)
        self.addCleanup(self.write_hba, self.hba_lines)
        rest = self.hba_lines[2:]

        def rp(kind="host", database=RP_DB, user="oracle_view_rp", address=f"{self.APP}/32",
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
            # A field that cannot match makes a line with an unsure field a line that never matches.
            ("+group on IPv6 above", ["host all +admins ::1/128 scram-sha-256", rp()], False, 0, ok),
            ("+group on the database replication above", ["host replication +repl 10.0.0.0/8 scram-sha-256", rp()],
             False, 0, ok),
            ("+group on another address above", ["host all +dba 10.1.2.3/32 scram-sha-256", rp()], False, 0, ok),
            ("+group on another database above", [rp(database=OLD_DB, user="+dba"), rp()], False, 0, ok),
            ("samenet on another database above", [rp(database="made_up_other", address="samenet"), rp()], False, 0, ok),
            ("a host name for another user above", [rp(user="odoo", address="app.example"), rp()], False, 0, ok),
            ("a hostgssenc line with +group above", [rp(kind="hostgssenc", user="+dba"), rp()], False, 0, ok),
            ("hostssl with samenet above, ssl off", ["hostssl all all samenet scram-sha-256", rp()], False, 0, ok),
            # When the other fields match, an unsure field still needs a check by hand.
            ("+group on the address of the App server above", [rp(user="+dba"), rp()], False, 4,
             "RESULT: CHECK BY HAND - a line above"),
            ("a host name above", [rp(address="app.example"), rp()], False, 4, "RESULT: CHECK BY HAND - a line above"),
            ("sameuser above", [rp(database="sameuser"), rp()], False, 4, "RESULT: CHECK BY HAND - a line above"),
            ("hostnossl with samehost above, ssl off", ["hostnossl all all samehost scram-sha-256", rp()], False, 4,
             "RESULT: CHECK BY HAND - a line above"),
            # Only above the first matching line.
            ("samenet below", [rp(), "host all all samenet scram-sha-256"], False, 0, ok),
            # A line for the role on the database of the earlier releases: a NOTE, the result stays.
            ("a line for the old database", [rp(database=OLD_DB), rp()], False, 0,
             "NOTE: the tool does not read perodua_oracle any more: a line of the list above lets oracle_view_rp in "
             "there without a reason."),
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
                self.assertEqual("NOTE: the tool does not read" in result.stdout, name == "a line for the old database")
        # md5 lets the role in: its password is a SCRAM-SHA-256 verifier, so PostgreSQL uses SCRAM.
        self.write_hba(self.hba_lines[:2] + ["host perodua_oracle2 oracle_view_rp 127.0.0.1/32 md5"])
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

        def deploy_db():  # as deploy-db.sh for perodua_oracle2 with the App role odoo writes pg_hba.conf
            subprocess.run(["python3", "-", str(self.hba), RP_DB, "odoo", "198.51.100.0/24", ""],
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
        self.assertLess(lines.index('host "perodua_oracle2" all 0.0.0.0/0 reject'), lines.index(line))
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
        self.assertIn(f"NOTE: {self.hba} holds the block of deploy-db.sh for perodua_oracle2 (line 2).", result.stdout)
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

    def test_the_pin_is_the_release_of_2026_11_09(self):
        # grey-v1.0.4: linux/amd64, label org.opencontainers.image.version=grey-v1.0.4, revision 00df86f.
        self.assertEqual(IMAGE, REPOSITORY + ":grey-v1.0.4@sha256:"
                         "cf9a9170fb5642883b32386583501093f17ad77fefaaf592462849c0727818f1")
        self.assertEqual(RELEASE, "oracle-view-grey-v1.0.4")
        for old in ("grey-v1.0.1", "8e82b47f", "a5b48111", "ed104afa"):
            self.assertNotIn(old, TEXT)
            self.assertNotIn(old, README)

    def test_one_line_names_the_rp_database(self):
        # The database of the grey App (owner decision of 2026-11-07): the SQL of the script reads RP_DATABASE.
        self.assertEqual([line for line in TEXT.splitlines() if "perodua_oracle" in line],
                         ["RP_DATABASE=perodua_oracle2", "OLD_RP_DATABASE=perodua_oracle"])
        text = words(SECTION)
        for sentence in ("| Role `oracle_view_rp` on the database `perodua_oracle2`:",
                         "`CONNECT` and `TEMPORARY` on `perodua_oracle2`,",
                         "host perodua_oracle2 oracle_view_rp 192.168.23.13/32 scram-sha-256",
                         "`RP database view: grey (192.168.23.12:5432/perodua_oracle2)`"):
            self.assertIn(sentence, text)

    def test_the_readme_describes_the_cleanup_of_the_old_database(self):
        text = words(SECTION)
        for sentence in ("the command ends the sessions of the role there and removes all its rights there (`DROP OWNED BY`).",
                         "So `perodua_oracle2` gets them again in the same transaction: a stop between the two changes "
                         "nothing, and the tool keeps its way in.",
                         "After the `COMMIT`, the command ends the sessions of the role there again: a container of an "
                         "earlier release can open a session before the `COMMIT`, and after it no new session can start, "
                         "because `PUBLIC` has no `CONNECT` there.",
                         "When the server has no `perodua_oracle`, or when `perodua_oracle` does not accept connections "
                         "(`ALLOW_CONNECTIONS false`, or a database half dropped), the command skips this step.",
                         SAFE_FORM,
                         "`DROP OWNED BY` alone also removes `CONNECT` on `perodua_oracle2` (a database is a shared "
                         "object): then the tool cannot connect until the next `role` run.",
                         "Such a database stops the command (exit status 1) before the `pg_hba.conf` check",
                         "removes its rights in `perodua_oracle2` and in `perodua_oracle` (when it exists, so that an old "
                         "grant cannot make the drop fail)"):
            self.assertIn(sentence, text)
        usage = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60).stdout
        self.assertIn("in perodua_oracle2 and perodua_oracle.", words(usage))
        self.assertIn("It ends the sessions of the role in perodua_oracle (the database of the earlier releases) and "
                      "removes its rights there.", words(usage))

    def test_the_readme_and_the_usage_describe_the_rights_of_the_owner(self):
        # Owner decision of 2026-11-09: it replaces the one of 2026-11-06 for the rights of the role.
        text = words(SECTION)
        for sentence in ("The role is a member of the owner of `perodua_oracle2` (`odoo` on grey), granted "
                         "`WITH INHERIT TRUE, SET FALSE`.",
                         "stops before any change when the owner is a superuser",
                         "or when `oracle_view_rp` owns `perodua_oracle2` or `perodua_oracle`: a different owner is "
                         "needed first",
                         # The membership is for the whole server: information, never a FAIL.
                         "A membership is for the whole server. On each other database of the same owner, the role gets "
                         "the rights of the owner too: `CONNECT` (`pg_hba.conf` still decides where it can log in), "
                         "`ALTER DATABASE` (for example `ALLOW_CONNECTIONS false`) from a session in `perodua_oracle2`, "
                         "and `DROP DATABASE` from a direct client, outside a transaction. The role also gets the "
                         "memberships of the owner.",
                         "The membership in the owner (`odoo`) is for the whole server.",
                         # What the role made stays: role (old database) and drop-role give it to the owner.
                         "before `DROP OWNED BY`, the command gives each object of the role there to the owner of "
                         "`perodua_oracle` (`REASSIGN OWNED BY`) and names it.",
                         "`drop-role` never drops what the role made outside `ovb_stage`",
                         "It stops before any change when `oracle_view_rp` owns `perodua_oracle2` or `perodua_oracle`: "
                         "its objects there would have no other owner to go to. A different owner is needed first."):
            self.assertIn(sentence, text)
        for old in ("no `CREATE` in `public`", "so DDL on them fails", "owns one schema, ovb_stage", "owns nothing outside"):
            self.assertNotIn(old, text)
        usage = words(subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60).stdout)
        for sentence in ("It makes the role a member of the owner of perodua_oracle2 WITH INHERIT TRUE, SET FALSE (owner "
                         "decision of 2026-11-09)", "A membership is for the whole server",
                         "It stops before any change when oracle_view_rp owns one of these databases: a different owner "
                         "is needed first."):
            self.assertIn(sentence, usage)
        for old in ("no CREATE in public", "no DDL on the Odoo tables", "owns nothing"):
            self.assertNotIn(old, usage)

    def test_the_readme_upgrade_names_the_change_of_the_database(self):
        upgrade = words(SECTION.split("\n### Upgrade\n", 1)[1].split("\n### ", 1)[0])
        for sentence in ("A release that changes the database of the image (as `grey-v1.0.2` did, to `perodua_oracle2`) "
                         "needs two steps before its `install`:",
                         "1. On the DB server, run `sudo bash oracle-view.sh role --verifier-file ~/rp_grey.verifier` "
                         "of the new release",
                         "2. Add the new `pg_hba.conf` line that `role` prints, then reload",
                         "To go back to a release before grey-v1.0.2, give the role its old rights on its old database "
                         "`perodua_oracle` again"):
            self.assertIn(sentence, upgrade)
        self.assertLess(upgrade.index("role --verifier-file"), upgrade.index("`pg_hba.conf` line"))

    def test_the_readme_names_lines_that_are_not_a_check_by_hand(self):
        text = words(SECTION)
        for sentence in ('A line with a field that cannot match is not "check by hand", for example '
                         "`host all +admins ::1/128 scram-sha-256` (IPv6: it never matches the IPv4 address of the App "
                         "server), `host replication +repl 10.0.0.0/8 scram-sha-256` (the database `replication`) and "
                         "`host all +dba 10.1.2.3/32 scram-sha-256` (another address).",
                         "and each of its other fields (type, database, user, address) can match",
                         "The list `pg_hba.conf lines for oracle_view_rp on perodua_oracle` is empty"):
            self.assertIn(sentence, text)

    def test_the_readme_names_the_time_outs(self):
        text = words(SECTION)
        for sentence in ("| `OVB_LIVE_TIMEOUT` | none: the image default, `300` | Seconds (5-3600) for each round trip of a "
                         "live Oracle call of the Oracle view",
                         "| `OVB_SQL_TIMEOUT` | none: the image default, `300` | Seconds (5-3600) for one statement of the "
                         "SQL console |",
                         "Without a value, `install` gives the container no such variable, and the default of the image "
                         "applies. A key with no value (`OVB_LIVE_TIMEOUT=`) in `--config` removes a saved value."):
            self.assertIn(sentence, text)

    def test_the_readme_names_the_verifier_file_of_the_image_build(self):
        # deploy/build-image.sh of oracle-view 9cd7a93: RP_VERIFIER=$SECRETS/rp_grey.verifier
        self.assertIn("writes the SCRAM-SHA-256 verifier of the role password to `secrets/rp_grey.verifier` in its "
                      "output folder.", words(SECTION))
        self.assertIn("scp OUTPUT_FOLDER/secrets/rp_grey.verifier YOU@DB_SERVER:\n", SECTION)
        self.assertIn("sudo bash oracle-view.sh role --verifier-file ~/rp_grey.verifier\n", SECTION)

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
                         "`# BEGIN perodua-db-deploy perodua_oracle2` to `# END perodua-db-deploy perodua_oracle2`.",
                         'ends with `host "perodua_oracle2" all 0.0.0.0/0 reject`.',
                         "after every `deploy-db.sh` run, run `sudo bash oracle-view.sh role --check` and move the line "
                         "back above the block."):
            self.assertIn(sentence, text)


if __name__ == "__main__":
    unittest.main()
