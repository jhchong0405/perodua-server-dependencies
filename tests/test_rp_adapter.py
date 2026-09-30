"""rp-adapter.sh against a fake Docker CLI.

The script requires root; run these in a disposable Linux container as root (for
example the test image, or any Ubuntu with python3 and util-linux) or skip them
elsewhere. The fake Docker answers only the commands the script uses and fails on
anything else. It records each call; for `docker run` it also records what the
container would see in its mounted settings and secret files, and, when the
adapter is told to write to /output, creates a run folder there the way the
adapter does. Nothing is pulled or run.
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
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rp-adapter.sh"
IMAGE = re.search(r"^IMAGE=(\S+)$", SCRIPT.read_text(), re.M).group(1)
# characters that a shell or a KEY=VALUE file would change
PASSWORD = 'pa$$ #"wo\'rd\\ `x`'
API_KEY = "k3y-for-the-iss-api"
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_STATE"]) as f:
    state = json.load(f)
entry, out, err, status = {"args": args}, "", "", 0
image = state["image"]
if args == ["image", "inspect", image]:
    status = 0 if state["image_present"] else 1
elif args == ["pull", image]:
    if state["pull_error"]:
        err, status = state["pull_error"] + "\n", 1
    else:
        state["image_present"] = True
elif args == ["image", "rm", image]:
    state["image_present"] = False
elif args[:2] == ["run", "--rm"] and image in args:
    at = args.index(image)
    entry["adapter"] = args[at + 1:]
    mounts = {}
    for i, arg in enumerate(args[:at]):
        if arg == "--mount":
            spec = dict(part.split("=", 1) for part in args[i + 1].split(",") if "=" in part)
            mounts[spec["dst"]] = spec["src"]
    entry["mounts"] = mounts
    for dst, key in (("/config/adapter.conf", "conf"), ("/run/secrets/db_password", "password"),
                     ("/run/secrets/api_key", "api_key")):
        if dst in mounts:
            entry[key] = open(mounts[dst]).read()
    adapter = entry["adapter"]
    if "--out" in adapter:  # the adapter writes a timestamped run folder there
        target = adapter[adapter.index("--out") + 1]
        assert target.startswith("/output/"), target
        base = os.path.join(mounts["/output"], target[len("/output/"):], "20260930T010203Z")
        run, n = base, 1
        while os.path.exists(run):  # the adapter's naming: -2, -3 ... within one second
            n += 1
            run = f"{base}-{n}"
        os.makedirs(run)
        open(os.path.join(run, "manifest.json"), "w").write("{}")
    status = state["run_status"].pop(0) if state["run_status"] else 0
    out = "fake adapter output\n"
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


FAKE_MV = r'''#!/usr/bin/env python3
# mv and cp: the real one, unless FAKE_MV_FAIL names the destination (and
# FAKE_MV_TOOL, default mv, this tool): then the first such call fails or, with
# FAKE_MV_KILL, kills the script half-way.
import os, shutil, signal, sys
tool = os.path.basename(sys.argv[0])
fail, once = os.environ.get("FAKE_MV_FAIL"), os.environ.get("FAKE_MV_ONCE", "")
if (fail and tool == os.environ.get("FAKE_MV_TOOL", "mv")
        and sys.argv[-1].endswith("/" + fail) and not os.path.exists(once)):
    open(once, "w").close()  # only the first such call fails
    if os.environ.get("FAKE_MV_KILL"):
        os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(tool + ": fake failure")
os.execv(shutil.which(tool, path="/usr/bin:/bin"), [tool] + sys.argv[1:])
'''


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir(),
                     "needs root on Linux (a disposable test container)")
class RpAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = Path(os.path.realpath(self.tmp)) / "deploy"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        for tool in ("mv", "cp"):  # the real ones, unless FAKE_MV_FAIL names the destination
            (bin_dir / tool).write_text(FAKE_MV)
            (bin_dir / tool).chmod(0o755)
        self.state = self.tmp / "state.json"
        self.log = self.tmp / "docker.log"
        self.set_state(image=IMAGE, image_present=False, pull_error="", run_status=[])
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                        FAKE_DOCKER_STATE=str(self.state), FAKE_DOCKER_LOG=str(self.log))
        self.password_file = self.tmp / "db-password"
        self.password_file.write_text(PASSWORD + "\n")
        self.key_file = self.tmp / "api-key"
        self.key_file.write_text(API_KEY)

    def set_state(self, **values):
        state = json.loads(self.state.read_text()) if self.state.exists() else {}
        state.update(values)
        self.state.write_text(json.dumps(state))

    def config(self, **overrides):
        values = {"DB_HOST": "10.1.119.26", "DB_PORT": "1521", "DB_SERVICE": "ENTPP2",
                  "DB_USER": "RP_GATEWAY", "DB_PASSWORD_FILE": str(self.password_file),
                  "CALL_TIMEOUT": "120", "API_URL": "", "DOCKER_NETWORK": "host"}
        values.update(overrides)
        path = self.tmp / "settings.env"
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))
        return path

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args, "--dir", str(self.dir)],
                              env=self.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def runs(self):
        return [c for c in self.calls() if "adapter" in c]

    def install(self, **overrides):
        result = self.run_script("install", "--config", str(self.config(**overrides)),
                                 "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.log.unlink(missing_ok=True)
        return result

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    # ------------------------------------------------------------------ install
    def test_install_writes_settings_and_secrets_then_checks_the_database(self):
        result = self.run_script("install", "--config", str(self.config(
            API_URL="http://127.0.0.1:8000", API_KEY_FILE=str(self.key_file))), "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Set up and verified", result.stdout)
        settings = (self.dir / "rp-adapter.env").read_text()
        self.assertIn("DB_HOST=10.1.119.26\n", settings)
        self.assertNotIn("PASSWORD", settings)
        self.assertNotIn("API_KEY_FILE", settings)
        self.assertEqual(self.mode(self.dir / "rp-adapter.env"), 0o600)
        self.assertEqual(self.mode(self.dir), 0o700)
        self.assertEqual(self.mode(self.dir / "secrets"), 0o700)
        for name, value in (("db_password", PASSWORD), ("api_key", API_KEY)):
            self.assertEqual((self.dir / "secrets" / name).read_text(), value)
            self.assertEqual(self.mode(self.dir / "secrets" / name), 0o444)
        output = os.stat(self.dir / "output")
        self.assertEqual((output.st_uid, output.st_gid, self.mode(self.dir / "output")),
                         (10001, 10001, 0o700))
        conf = (self.dir / "adapter.conf").read_text()
        self.assertIn("ISS_DB_PASSWORD_FILE=/run/secrets/db_password\n", conf)
        self.assertIn("ISS_API_BASE_URL=http://127.0.0.1:8000\n", conf)
        self.assertNotIn(PASSWORD, conf)

        calls = self.calls()
        self.assertEqual([c["args"][:2] for c in calls[:2]], [["image", "inspect"], ["pull", IMAGE]])
        runs = self.runs()
        self.assertEqual([r["adapter"] for r in runs], [
            ["db", "health", "--config", "/config/adapter.conf"],
            ["health", "--scope", "connection", "--config", "/config/adapter.conf"]])
        first = runs[0]
        self.assertEqual(first["password"], PASSWORD)
        self.assertEqual(first["api_key"], API_KEY)
        for flag in ("--read-only", "--cap-drop", "--security-opt"):
            self.assertIn(flag, first["args"])
        self.assertIn("10001:10001", first["args"])
        self.assertEqual(first["args"][first["args"].index("--network") + 1], "host")
        for call in calls:  # the secrets travel only as mounted files
            self.assertNotIn(PASSWORD, " ".join(call["args"]))
            self.assertNotIn(API_KEY, " ".join(call["args"]))

    def test_rerun_keeps_the_password_and_a_new_file_replaces_it(self):
        self.install()
        result = self.run_script("install")  # no terminal, no --config: reuses everything
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dir / "secrets" / "db_password").read_text(), PASSWORD)
        self.assertEqual([c["args"][:2] for c in self.calls()][0], ["image", "inspect"])
        self.assertNotIn(["pull", IMAGE], [c["args"] for c in self.calls()])  # already there
        self.password_file.write_text("new-password\r\n")
        self.install()
        self.assertEqual((self.dir / "secrets" / "db_password").read_text(), "new-password")

    def test_first_install_needs_the_password_file_without_a_terminal(self):
        result = self.run_script("install", "--config", str(self.config(DB_PASSWORD_FILE=None)),
                                 "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("needs DB_PASSWORD_FILE", result.stderr)
        self.assertEqual(self.runs(), [])

    def test_invalid_settings_are_refused_before_docker(self):
        cases = {"DB_HOST": "db host", "DB_PORT": "70000", "DB_SERVICE": "ENT PP2",
                 "DB_USER": "1user", "CALL_TIMEOUT": "0", "API_URL": "ftp://x",
                 "DOCKER_NETWORK": "a b"}
        for key, value in cases.items():
            with self.subTest(key=key):
                result = self.run_script("install", "--config", str(self.config(**{key: value})),
                                         "--non-interactive")
                self.assertEqual(result.returncode, 3)
                self.assertIn(f"{key}={value} cannot be used", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_loopback_database_only_with_the_host_network(self):
        self.install(DB_HOST="127.0.0.1")
        result = self.run_script("install", "--config", str(self.config(
            DB_HOST="127.0.0.1", DOCKER_NETWORK="bridge")), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("the container itself", result.stderr)

    def test_unknown_setting_is_refused(self):
        path = self.config()
        path.write_text(path.read_text() + "DB_PASSWORD=plain\n")
        result = self.run_script("install", "--config", str(path), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unknown key DB_PASSWORD", result.stderr)

    def test_a_directory_with_other_files_is_refused(self):
        self.dir.mkdir()
        (self.dir / "something").write_text("x")
        result = self.run_script("install", "--config", str(self.config()), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not empty", result.stderr)

    def test_database_failure_at_install_keeps_the_settings(self):
        self.set_state(run_status=[2])
        result = self.run_script("install", "--config", str(self.config()), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("cannot use the database", result.stderr)
        self.assertTrue((self.dir / "rp-adapter.env").exists())

    def test_registry_login_needed_without_a_terminal(self):
        self.set_state(pull_error="unauthorized: authentication required")
        result = self.run_script("install", "--config", str(self.config()), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("docker login perodua-deploy.novutal.com", result.stderr)

    # ------------------------------------------------------------------ using it
    def test_health_passes_options_and_exit_code(self):
        self.install()
        self.set_state(run_status=[2])
        result = self.run_script("health", "--deep", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "fake adapter output\n")  # stdout stays the adapter's
        self.assertEqual(self.runs()[0]["adapter"],
                         ["db", "health", "--deep", "--json", "--config", "/config/adapter.conf"])

    def test_survey_and_export_write_under_output(self):
        self.install()
        result = self.run_script("survey", "--exact", "--rows", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        adapter = self.runs()[0]["adapter"]
        self.assertEqual(adapter[:5], ["db", "survey", "--exact", "--rows", "5"])
        self.assertEqual((adapter[5], adapter[7:]), ("--out", ["--config", "/config/adapter.conf"]))
        run = re.fullmatch(r"/output/survey/(\d{8}T\d{6}Z-p\d+)", adapter[6]).group(1)
        folder = self.dir / "output" / "survey" / run
        # the folder the adapter made inside this run's folder, with the manifest
        self.assertEqual([p.name for p in folder.iterdir()], ["20260930T010203Z"])
        self.assertIn(f"Results on this server: {folder}/20260930T010203Z\n", result.stderr)
        self.assertTrue((folder / "20260930T010203Z" / "manifest.json").is_file())
        self.assertEqual((os.stat(folder).st_uid, self.mode(folder)), (10001, 0o700))
        result = self.run_script("export", "--all", "--format", "jsonl")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.runs()[-1]["adapter"][:5], ["db", "export", "--all", "--format", "jsonl"])
        self.assertIn("Results on this server: " + str(self.dir / "output" / "export"), result.stderr)

    def test_export_needs_datasets(self):
        self.install()
        result = self.run_script("export")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--all", result.stderr)

    def test_options_the_script_sets_are_refused(self):
        self.install()
        for args in (("survey", "--out", "/tmp/x"), ("export", "--all", "--out=/tmp"),
                     ("run", "db", "health", "--config", "/etc/other")):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 3)
                self.assertIn("is set by rp-adapter.sh", result.stderr)
        self.assertEqual(self.runs(), [])

    def test_run_passes_any_adapter_command(self):
        self.install()
        result = self.run_script("run", "db", "columns", "ISS_CUSTOMERS_V")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.runs()[0]["adapter"],
                         ["db", "columns", "ISS_CUSTOMERS_V", "--config", "/config/adapter.conf"])

    def test_api_health_needs_the_api(self):
        self.install()
        result = self.run_script("api-health")
        self.assertEqual(result.returncode, 3)
        self.assertIn("No API_URL", result.stderr)

    def test_commands_need_a_setup(self):
        result = self.run_script("health")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Run: sudo bash rp-adapter.sh install", result.stderr)

    def test_status_shows_no_secret(self):
        self.install()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RP_GATEWAY@10.1.119.26:1521/ENTPP2", result.stdout)
        self.assertNotIn(PASSWORD, result.stdout)

    def test_every_run_is_locked_down(self):
        self.install(API_URL="http://127.0.0.1:8000", API_KEY_FILE=str(self.key_file))
        self.run_script("health")
        args = self.runs()[0]["args"]
        at = args.index(IMAGE)
        options = args[:at]

        def value(flag):
            return options[options.index(flag) + 1]

        self.assertEqual((value("--network"), value("--tmpfs"), value("--cap-drop"),
                          value("--security-opt"), value("--user")),
                         ("host", "/tmp", "ALL", "no-new-privileges:true", "10001:10001"))
        self.assertIn("--read-only", options)
        mounts = [options[i + 1] for i, a in enumerate(options) if a == "--mount"]
        self.assertEqual(sorted(mounts), sorted([
            f"type=bind,src={self.dir}/adapter.conf,dst=/config/adapter.conf,readonly",
            f"type=bind,src={self.dir}/secrets/db_password,dst=/run/secrets/db_password,readonly",
            f"type=bind,src={self.dir}/secrets/api_key,dst=/run/secrets/api_key,readonly",
            f"type=bind,src={self.dir}/output,dst=/output"]))
        self.assertNotIn("-e", options)
        self.assertNotIn("--env", options)
        self.assertNotIn("--privileged", options)

    def lock_path(self):
        base = "/run/lock" if os.path.isdir("/run/lock") else "/run"
        digest = hashlib.sha256(str(self.dir).encode()).hexdigest()[:16]
        return f"{base}/rp-adapter-{digest}.lock"

    def test_runs_and_changes_exclude_each_other(self):
        self.install()
        path = self.lock_path()
        with open(path, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # an install or uninstall in progress
            result = self.run_script("health")
            self.assertEqual(result.returncode, 3)
            self.assertIn("install or uninstall is changing", result.stderr)
            fcntl.flock(held, fcntl.LOCK_UN)
            fcntl.flock(held, fcntl.LOCK_SH)  # a survey still running
            result = self.run_script("health")
            self.assertEqual(result.returncode, 0, result.stderr)  # runs may overlap
            result = self.run_script("uninstall", "--confirm", str(self.dir))
            self.assertEqual(result.returncode, 3)
            self.assertIn("Another rp-adapter.sh command", result.stderr)
        self.assertTrue((self.dir / "rp-adapter.env").exists())

    def test_a_failed_install_changes_nothing(self):
        self.install()
        conf = (self.dir / "adapter.conf").read_text()
        self.password_file.write_text("new-password")
        result = self.run_script("install", "--config", str(self.config(
            DB_USER="OTHER_USER", API_URL="http://127.0.0.1:8000",
            API_KEY_FILE=str(self.tmp / "missing-key"))), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("API_KEY_FILE must be", result.stderr)
        self.assertEqual((self.dir / "secrets" / "db_password").read_text(), PASSWORD)
        self.assertEqual((self.dir / "adapter.conf").read_text(), conf)
        self.assertIn("DB_USER=RP_GATEWAY", (self.dir / "rp-adapter.env").read_text())
        leftovers = [p.name for p in self.dir.rglob("*") if p.name.endswith(".new")]
        self.assertEqual(leftovers, [])

    def test_api_key_rotation_and_removal(self):
        self.install(API_URL="http://127.0.0.1:8000", API_KEY_FILE=str(self.key_file))
        self.key_file.write_text("rotated-key")
        self.install(API_URL="http://127.0.0.1:8000", API_KEY_FILE=str(self.key_file))
        self.assertEqual((self.dir / "secrets" / "api_key").read_text(), "rotated-key")
        self.install(API_URL="")
        self.assertFalse((self.dir / "secrets" / "api_key").exists())
        self.assertNotIn("ISS_API", (self.dir / "adapter.conf").read_text())

    def test_api_url_checks(self):
        for url in ("http://127.0.0.1:70000", "http://bad host", "http://h:0"):
            with self.subTest(url=url):
                result = self.run_script("install", "--config", str(self.config(API_URL=url)),
                                         "--non-interactive")
                self.assertEqual(result.returncode, 3)
                self.assertIn("API_URL=", result.stderr)
        result = self.run_script("install", "--config", str(self.config(
            API_URL="http://127.0.0.1:8000", DOCKER_NETWORK="rpnet")), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("the container itself", result.stderr)

    def test_directory_names_that_mounts_cannot_carry(self):
        result = subprocess.run(["bash", str(SCRIPT), "health", "--dir", str(self.tmp / "a,b")],
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3)
        self.assertIn("--dir may hold only", result.stderr)

    def test_an_unmarked_output_folder_is_not_adopted(self):
        (self.dir / "output").mkdir(parents=True)
        result = self.run_script("install", "--config", str(self.config()), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not empty", result.stderr)

    def test_each_run_reports_its_own_folder(self):
        self.install()
        survey = self.dir / "output" / "survey"
        other = survey / "20990101T000000Z-p1"  # another run writing at the same time
        other.mkdir(parents=True)
        result = self.run_script("survey")
        reported = re.findall(r"Results on this server: (\S+)", result.stderr)
        self.assertEqual(len(reported), 1)
        self.assertEqual(Path(reported[0]).parent.parent, survey)
        self.assertNotEqual(Path(reported[0]).parent, other)
        self.assertTrue((Path(reported[0]) / "manifest.json").is_file())

    def test_status_shows_the_newest_run(self):
        self.install()
        survey = self.dir / "output" / "survey"
        for name in ("20260930T010203Z-p9", "20260930T010203Z-p10", "20260929T235959Z-p99"):
            (survey / name / "20260930T010203Z").mkdir(parents=True)
        (survey / "20260930T020000Z-p5").mkdir()  # a run with no results (yet)
        status = self.run_script("status").stdout
        # -p10 after -p9: version order; the folder with the manifest, not the run's
        self.assertIn(f"Latest survey: {survey}/20260930T010203Z-p10/20260930T010203Z\n", status)

    def test_a_first_install_that_fails_can_be_retried_or_removed(self):
        self.set_state(pull_error="Error response from daemon: network unreachable")
        result = self.run_script("install", "--config", str(self.config()), "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Pulling from perodua-deploy.novutal.com failed", result.stderr)
        leftovers = [p.name for p in self.dir.rglob("*") if p.name.endswith((".new", ".old"))]
        self.assertEqual(leftovers, [])  # nothing staged stays behind
        self.set_state(pull_error="")
        self.install()  # the same directory is accepted again
        self.set_state(pull_error="Error response from daemon: network unreachable",
                       image_present=False)
        shutil.rmtree(self.dir)
        self.run_script("install", "--config", str(self.config()), "--non-interactive")
        result = self.run_script("uninstall", "--confirm", str(self.dir))
        self.assertEqual(result.returncode, 0, result.stderr)  # and can be removed
        self.assertFalse(self.dir.exists())

    def test_a_move_that_fails_puts_the_previous_setup_back(self):
        self.install()
        before = {name: (self.dir / name).read_text()
                  for name in ("adapter.conf", "rp-adapter.env", "secrets/db_password")}
        self.password_file.write_text("new-password")
        self.env["FAKE_MV_FAIL"] = "adapter.conf"
        self.env["FAKE_MV_ONCE"] = str(self.tmp / "mv-failed-once")
        result = self.run_script("install", "--config", str(self.config(DB_USER="OTHER_USER")),
                                 "--non-interactive")
        self.assertEqual(result.returncode, 3)
        self.assertIn("the previous one was put back", result.stderr)
        after = {name: (self.dir / name).read_text() for name in before}
        self.assertEqual(after, before)
        leftovers = [p.name for p in self.dir.rglob("*") if p.name.endswith((".new", ".old"))]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.runs(), [])

    def kill_install_at(self, destination, tool="mv"):
        """An install of a new user and password, killed at the first `tool` to `destination`."""
        self.password_file.write_text("new-password")
        self.env.update(FAKE_MV_FAIL=destination, FAKE_MV_TOOL=tool, FAKE_MV_KILL="1",
                        FAKE_MV_ONCE=str(self.tmp / f"killed-at-{tool}-{destination.replace('/', '_')}"))
        result = self.run_script("install", "--config", str(self.config(DB_USER="OTHER_USER")),
                                 "--non-interactive")
        self.assertEqual(result.returncode, -9)
        for name in ("FAKE_MV_FAIL", "FAKE_MV_TOOL", "FAKE_MV_KILL", "FAKE_MV_ONCE"):
            del self.env[name]

    def assert_previous_setup_back(self, before):
        result = self.run_script("install", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the previous setup is back", result.stderr)
        after = {name: (self.dir / name).read_text() for name in before}
        self.assertEqual(after, before)
        leftovers = [p.name for p in self.dir.rglob("*")
                     if p.name.endswith((".new", ".old")) or p.name == ".publishing"]
        self.assertEqual(leftovers, [])

    def current_setup(self):
        return {name: (self.dir / name).read_text()
                for name in ("adapter.conf", "rp-adapter.env", "secrets/db_password")}

    def test_an_install_killed_half_way_is_refused_then_undone(self):
        self.install()
        before = self.current_setup()
        self.kill_install_at("adapter.conf")  # after the new password, before the user
        self.assertEqual((self.dir / "secrets/db_password").read_text(), "new-password")
        result = self.run_script("health")  # the mixed files are not used
        self.assertEqual(result.returncode, 3)
        self.assertIn("An install was stopped while it replaced the setup", result.stderr)
        self.assertEqual(self.runs(), [])
        self.assert_previous_setup_back(before)

    def test_what_a_killed_install_staged_is_not_published_later(self):
        self.install()
        before = self.current_setup()
        self.kill_install_at("secrets/db_password")  # the new password is still only staged
        self.assertTrue((self.dir / "secrets/.db_password.new").exists())
        self.assert_previous_setup_back(before)  # not the old user with the new password

    def test_putting_back_can_itself_be_stopped_and_run_again(self):
        self.install()
        before = self.current_setup()
        self.kill_install_at("adapter.conf")
        # the recovering install is killed too, after the password is back
        self.env.update(FAKE_MV_FAIL="adapter.conf", FAKE_MV_TOOL="cp", FAKE_MV_KILL="1",
                        FAKE_MV_ONCE=str(self.tmp / "killed-putting-back"))
        self.assertEqual(self.run_script("install", "--non-interactive").returncode, -9)
        for name in ("FAKE_MV_FAIL", "FAKE_MV_TOOL", "FAKE_MV_KILL", "FAKE_MV_ONCE"):
            del self.env[name]
        self.assert_previous_setup_back(before)

    def test_a_local_failure_is_unknown_to_monitoring(self):
        self.install()
        (self.dir / "output" / "survey").write_text("")  # a file where a folder must go
        result = self.run_script("survey")
        self.assertEqual(result.returncode, 3)  # not 1: that would read as WARNING
        self.assertIn("rp-adapter.sh failed at line", result.stderr)
        self.assertEqual(self.runs(), [])

    def test_usage_errors_are_unknown_to_monitoring(self):
        for args in ((), ("frobnicate",)):
            with self.subTest(args=args):
                result = subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 3)

    # ------------------------------------------------------------------ uninstall
    def test_uninstall_keeps_results_unless_purged(self):
        self.install()
        self.run_script("survey")
        result = self.run_script("uninstall", "--confirm", "/wrong")
        self.assertEqual(result.returncode, 3)
        self.assertTrue((self.dir / "rp-adapter.env").exists())
        result = self.run_script("uninstall", "--confirm", str(self.dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["output"])
        self.install()
        result = self.run_script("uninstall", "--purge", "--confirm", str(self.dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.dir.exists())
        self.assertIn(["image", "rm", IMAGE], [c["args"] for c in self.calls()])


if __name__ == "__main__":
    unittest.main()
