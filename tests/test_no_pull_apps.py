"""--no-pull of deploy-app.sh, iss-api.sh and rp-adapter.sh, and the Mac helper stream-image.sh.

NoPullCodeTest reads the scripts (any host). NoPullOptionTest runs the refusals that
come before Docker (Linux). The App tests run the scripts as root on Linux (the test
image) against the fake Docker CLI of each script's own tests (test_iss_api,
test_rp_adapter, test_deploy_app_uat), behind a fake that answers what --no-pull adds:
docker info (the image store), docker image inspect of the digest reference, and the
start of a pinned image (docker run --rm --pull never --network none --entrypoint sh
IMAGE -c :). In a no-pull test it refuses docker pull, docker login and docker
context. It records each call with its DOCKER_CONFIG and whether the deployment
directory existed at that moment. StreamImageTest runs stream-image.sh (no root, with
/bin/bash: bash 3.2 on the Mac) against fakes of docker (the Mac), ssh (the server),
rsync and shasum. Every digest, password and host here is made up, except the pins
that the scripts hold.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import test_deploy_app_uat as uat_tests
import test_iss_api as iss_tests
import test_rp_adapter as rp_tests
from test_non_interactive import QUESTIONS, code_text, logical_lines, on_terminal, without_variable


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ISS = SCRIPTS / "iss-api.sh"
RP = SCRIPTS / "rp-adapter.sh"
APP = SCRIPTS / "deploy-app.sh"
OV = SCRIPTS / "oracle-view.sh"
STREAM = SCRIPTS / "stream-image.sh"
LINUX = Path("/proc/self/fd").is_dir()
ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
FACTS = "{{.Id}}{{range .RepoDigests}} {{.}}{{end}}"
STORE_FORMAT = "{{json .DriverStatus}}"
INFO = ["info", "--format", STORE_FORMAT]
DRIVER_STATUS = {"containerd": '[["driver-type","io.containerd.snapshotter.v1"]]',
                 "classic": '[["Backing Filesystem","extfs"],["Supports d_type","true"]]'}
CONFIG_ID = "sha256:" + "c" * 64  # the ID of a pulled image on the classic image store: the digest of its config
OTHER = "sha256:" + "e" * 64
HOST_NAME = os.uname().nodename


def pin(path, key):
    return re.search(rf"^{key}=(\S+)", path.read_text(), re.M).group(1)


def parts(image):
    """REPOSITORY, DIGEST, the digest reference and the stream tag of a pin, as the scripts make them."""
    name, digest = image.split("@")
    repository = name[:name.rindex(":")]  # the tag follows the last ':'; a registry port comes before a '/'
    return repository, digest, f"{repository}@{digest}", f"{repository}:{digest.replace(':', '-')}"


def pinned(image):
    return parts(image)[2]


def facts(image):  # what docker image inspect --format FACTS prints for a pin streamed or pulled to containerd
    return f"{parts(image)[1]} {pinned(image)}"


def start(image, user=None):
    return (["run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges:true"] + (["--user", user] if user else [])
            + ["--entrypoint", "sh", image, "-c", ":"])


def stream_command(image):
    _, _, reference, tag = parts(image)
    return (f"docker pull --platform linux/amd64 {image} && docker tag {reference} {tag} && "
            f"docker save {tag} | ssh SSH_HOST 'sudo -n docker load'")


def missing_message(image, source, prefix="ERROR: ", end="."):
    return (f"{prefix}The pinned image is not on this server, and --no-pull does not pull it: {pinned(image)}\n"
            "Stream it from the Mac (it has the registry login and the containerd image store). There, SSH_HOST is "
            f"the ssh name of this server (host name {HOST_NAME}):\n"
            f"  bash scripts/stream-image.sh SSH_HOST {source}\n"
            "or with Docker alone:\n"
            f"  {stream_command(image)}\n"
            f"Then run this command again. Nothing was changed{end}\n")


def snapshot(directory):
    """Every file under directory with its bytes and mode, to show that a run changed nothing."""
    if not directory.exists():
        return None
    found = {}
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        found[str(path.relative_to(directory))] = (path.read_bytes() if path.is_file() and not path.is_symlink() else b"",
                                                    info.st_mode)
    return found


def shell_function(text, name):
    """A function of a shell script, from its name to the closing brace at the start of a line, without comment lines."""
    body = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.M | re.S).group(0)
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def function_body(text, name):
    """The line numbers of the shell function name, from its first line to its closing brace."""
    lines = text.split("\n")
    start = next(n for n, line in enumerate(lines, 1) if line.startswith(f"{name}() {{"))
    end = next(n for n in range(start + 1, len(lines) + 1) if lines[n - 1] == "}")
    return range(start, end + 1)


def raw_logical_line(text, number):
    """The command that starts at line number, with its backslash-newline continuations joined."""
    lines, joined = text.split("\n"), ""
    while lines[number - 1].endswith("\\"):
        joined += lines[number - 1][:-1] + " "
        number += 1
    return joined + lines[number - 1]


# ─────────────────────────────────────────────────────────────── the fake Docker of the server
# In front of the fake Docker of a script's own tests (INNER_DOCKER): it answers what --no-pull adds.
NO_PULL_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
FACTS = ''' + repr(FACTS) + r'''
INFO = ''' + repr(INFO) + r'''
DRIVER_STATUS = ''' + repr(DRIVER_STATUS) + r'''
with open(os.environ["NO_PULL_STATE"]) as f:
    state = json.load(f)
entry = {"args": args, "docker_config": os.environ.get("DOCKER_CONFIG", ""),
         "dir_exists": os.path.exists(os.environ["NO_PULL_DIR"])}
with open(os.environ["NO_PULL_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
def reply(out="", err="", status=0):
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(status)
def reference(image):  # the digest reference of a pin
    name, digest = image.split("@")
    return name[:name.rindex(":")] + "@" + digest
if args == INFO:
    if state["store"] in DRIVER_STATUS:
        reply(DRIVER_STATUS[state["store"]] + "\n")
    reply(err="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n", status=1)
if args[:4] == ["image", "inspect", "--format", FACTS] and len(args) == 5:
    if state["images"].get(args[4]):
        reply(state["images"][args[4]] + "\n")
    reply(err=f"Error response from daemon: No such image: {args[4]}\n", status=1)
if (args[:4] == ["run", "--rm", "--pull", "never"] and args[-2:] == ["-c", ":"] and len(args) > 5
        and args[-5:-3] == ["--entrypoint", "sh"]):
    image = args[-3]
    if not state["images"].get(reference(image)):
        reply(err=f"docker: Error response from daemon: No such image: {image}\n", status=125)
    if state["start_errors"].get(reference(image)):
        reply(err=state["start_errors"][reference(image)] + "\n", status=125)
    reply()
if args == ["compose", "version"] and state["compose_version"]:
    reply(state["compose_version"] + "\n")
if args[:1] == ["compose"] and "release-mail" in args:
    reply("RELEASED 1 2\n")
if args[:1] in (["pull"], ["login"], ["context"]) and state["forbid_pull"]:
    reply(err="fake docker: no pull, login or context in a no-pull run\n", status=97)
os.execv(os.environ["INNER_DOCKER"], [os.environ["INNER_DOCKER"]] + args)
'''


def needs_root_on_linux(cls):
    return unittest.skipUnless(ROOT and LINUX, "needs root on Linux (the test image)")(cls)


class NoPullFake:
    """The fake Docker of the server in front of the fake of a script's own tests."""

    def install_fake(self, inner, directory):
        self.bin = self.tmp / "bin"
        self.bin.mkdir(exist_ok=True)
        (self.tmp / "inner").mkdir(exist_ok=True)
        inner_docker = self.tmp / "inner" / "docker"
        inner_docker.write_text(inner)
        inner_docker.chmod(0o755)
        (self.bin / "docker").write_text(NO_PULL_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.no_pull_state = self.tmp / "no-pull-state.json"
        self.no_pull_log = self.tmp / "no-pull.log"
        self.no_pull_state.write_text(json.dumps({"store": "containerd", "images": {}, "start_errors": {},
                                                  "compose_version": "", "forbid_pull": True}))
        return {"INNER_DOCKER": str(inner_docker), "NO_PULL_STATE": str(self.no_pull_state),
                "NO_PULL_LOG": str(self.no_pull_log), "NO_PULL_DIR": str(directory)}

    def server(self, **changes):
        state = json.loads(self.no_pull_state.read_text())
        state.update(changes)
        self.no_pull_state.write_text(json.dumps(state))

    def on_server(self, *images, **changes):  # these pins are on the server, as a stream or a pull brings them
        self.server(images={pinned(image): facts(image) for image in images}, **changes)

    def docker_calls(self):
        return [json.loads(line) for line in self.no_pull_log.read_text().splitlines()] if self.no_pull_log.exists() else []

    def args(self):
        return [call["args"] for call in self.docker_calls()]

    def pulls_and_logins(self):
        return [args for args in self.args() if args[:1] in (["pull"], ["login"], ["context"])]

    def assertChecksBeforeAnyChange(self, images, user=None):
        """docker info, the check of each digest reference and each start, all before the directory existed."""
        calls = self.docker_calls()
        args = [call["args"] for call in calls]
        checks = [INFO] + [["image", "inspect", "--format", FACTS, pinned(image)] for image in images] \
            + [start(image, user) for image in images]
        for check in checks:
            self.assertIn(check, args)
            self.assertFalse(calls[args.index(check)]["dir_exists"], check)
        self.assertEqual(args.count(INFO), 1)
        last = max(args.index(check) for check in checks)
        first_change = next((n for n, call in enumerate(calls) if call["dir_exists"]), len(calls))
        self.assertLess(last, first_change)
        for call in calls:
            self.assertEqual(call["docker_config"], self.env.get("DOCKER_CONFIG", ""))  # no throwaway login config


# ─────────────────────────────────────────────────────────────── static checks (any host)
class NoPullCodeTest(unittest.TestCase):
    """The no-pull step of each script, read from the script itself."""

    CHECKS = {ISS: ("image_store", "pinned_here", "starts", "check_pinned_image", "local_line"),
              RP: ("image_store", "pinned_here", "starts", "check_pinned_image", "get_image", "local_line"),
              APP: ("pin_parts", "image_store", "pinned_here", "starts", "check_compose_pull", "check_pinned_images")}

    def test_the_no_pull_step_has_no_read_no_pull_and_no_login(self):
        for path, names in self.CHECKS.items():
            text = path.read_text()
            for name in names:
                with self.subTest(script=path.name, function=name):
                    code = shell_function(text, name)
                    self.assertIsNone(re.search(r"\bread\b(?!-only)", code))  # docker run --read-only is no read
                    # A command, not the text of the message for the Mac: at the start of a line or after ; & | ( or $(.
                    self.assertIsNone(re.search(r"(^|[;&|(]|\$\()\s*docker\s+(pull|login)\b", code, re.M))
                    for word in ("/dev/tty", "-t 0", "DOCKER_CONFIG", "AUTH_DIR", "read_terminal"):
                        self.assertNotIn(word, code)
                    if name != "get_image":
                        self.assertNotIn("pull_image", code)

    def test_pull_image_runs_only_without_no_pull(self):
        iss, rp, app = ISS.read_text(), RP.read_text(), APP.read_text()
        self.assertEqual(re.findall(r"^.*\bpull_image\b.*$", iss, re.M),
                         ["pull_image() {   # asks for the registry account only when the registry wants one",
                          "    ((NO_PULL)) || pull_image"])
        self.assertEqual(re.findall(r"^.*\bpull_image\b.*$", rp, re.M),
                         ["pull_image() {   # asks for the registry account only when the registry wants one",
                          "    if ((NO_PULL)); then check_pinned_image; else pull_image; fi",
                          "    ((NO_PULL)) || pull_image"])
        self.assertIn("    get_image\n", shell_function(rp, "run_adapter"))
        self.assertIn('if ((NO_PULL == 0)); then  # --no-pull checked both images before the first change\n'
                      '    pull_image "$ODOO_IMAGE"\n    pull_image "$WEB_IMAGE"\nfi\n', app)
        self.assertEqual(len(re.findall(r"^\s*pull_image ", app, re.M)), 2)

    def test_the_check_comes_before_the_first_change(self):
        install = shell_function(ISS.read_text(), "cmd_install")
        self.assertLess(install.index("    claim_project\n    ((NO_PULL == 0)) || check_pinned_image\n"),
                        install.index('    install -d -m 0700 "$DEPLOY_DIR"\n'))
        install = shell_function(RP.read_text(), "cmd_install")
        check = install.index("    ((NO_PULL == 0)) || check_pinned_image\n")
        for change in ("        discard_staged\n", "            put_back\n", '    install -d -m 0700 "$DEPLOY_DIR" "$DEPLOY_DIR/secrets"\n'):
            self.assertLess(check, install.index(change), change)
        app = APP.read_text()
        check = app.index("if ((NO_PULL)); then\n    check_compose_pull\n    ((RELEASE_MAIL)) || [[ -n $TEST_MAIL ]] || check_pinned_images\nfi\n")
        for change in ('mkdir -p -- "$DEPLOY_DIR"\n', 'exec 9>"$DEPLOY_DIR/.deploy.lock"\n', 'TEMP_DIR=$(mktemp -d "$DEPLOY_DIR/.deploy.XXXXXXXX")\n'):
            self.assertLess(check, app.index(change), change)
        # The start of each image comes after the check of its digest, and before the line that says it is used.
        for path, name in ((ISS, "check_pinned_image"), (RP, "check_pinned_image"), (APP, "check_pinned_images")):
            code = shell_function(path.read_text(), name)
            self.assertLess(code.index("pinned_here "), code.index("out=$(starts"))
            self.assertLess(code.index("out=$(starts"), code.index("not pulled"))

    def test_docker_pull_and_login_are_only_in_pull_image(self):
        # The text of the messages is no command: code_text makes it Q.
        for path in (ISS, RP, APP):
            code = code_text(path.read_text())
            body = function_body(code, "pull_image")
            with self.subTest(script=path.name):
                found = [number for number, line in enumerate(code.split("\n"), 1)
                         if re.search(r"\bdocker\s+(pull|login)\b", line)]
                self.assertEqual(len(found), 2)
                self.assertTrue(all(number in body for number in found), found)

    def test_no_container_start_can_pull_in_a_no_pull_run(self):
        # deploy-app.sh: every Compose run and up (and docker run) of the script itself; the text of rollback.sh
        # and restore.txt (heredocs) and of the messages (quotes) is no command of this run.
        raw = APP.read_text()
        found = 0
        for number, line in logical_lines(code_text(raw)):
            where = f"deploy-app.sh:{number}: {line.strip()}"
            if re.search(r"(?:^|[\s;&|({!]|\$\()(?:docker\s+compose|compose|staged|current)\b.*?\s(run|up|create|pull)\b", line):
                found += 1
                self.assertRegex(raw_logical_line(raw, number), r'\s(run|up) "\$\{PULL_NEVER\[@\]\}" ', where)
            if re.search(r"\bdocker\s+(run|create)\b", line):
                found += 1
                self.assertIn("docker run --rm --pull never --network none ", raw_logical_line(raw, number), where)
        self.assertEqual(found, 25)  # 24 Compose calls and the start of an image
        self.assertIn("if ((NO_PULL)); then NON_INTERACTIVE=1 PULL_NEVER=(--pull never); fi\n", raw)
        # iss-api.sh: up and the start never pull; rp-adapter.sh: the adapter and the start never pull.
        for path, expected in ((ISS, ["up() { compose up --detach --wait --wait-timeout 120 --remove-orphans --pull never \"$@\"; }",
                                      "    docker run --rm --pull never --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \\"]),
                               (RP, ["    docker run --rm --pull never --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \\",
                                     "    docker run --rm --pull never --network \"$DOCKER_NETWORK\" --read-only --tmpfs /tmp --cap-drop ALL \\"])):
            text = path.read_text()
            starts = [line for line in text.splitlines()
                      if re.search(r"(^|[;&|(]|\$\()\s*docker\s+(run|create)\b|\bcompose\s+(up|run|create)\b", line)
                      and not line.lstrip().startswith("#")]
            with self.subTest(script=path.name):
                self.assertEqual(sorted(starts), sorted(expected))

    def test_the_option_and_the_variable(self):
        for path in (ISS, RP, APP):
            text = path.read_text()
            with self.subTest(script=path.name):
                self.assertRegex(text, r"\n\s+--no-pull\) ")
                self.assertIn("case ${KIT_NO_PULL:-} in\n    ''|0) ;;\n", text)
                self.assertIn("KIT_NO_PULL must be 1 (never pull: use the image", text)
                # No-pull is non-interactive, and the mode is decided before the export of KIT_NON_INTERACTIVE.
                self.assertRegex(text, r"NON_INTERACTIVE=1")
                self.assertLess(text.index("case ${KIT_NO_PULL:-} in"), text.index("case ${KIT_NON_INTERACTIVE:-} in"))
                self.assertLess(text.index("case ${KIT_NON_INTERACTIVE:-} in"),
                                text.index("((NON_INTERACTIVE == 0)) || export KIT_NON_INTERACTIVE=1"))
        self.assertIn("((NO_PULL == 0)) || NON_INTERACTIVE=1\n", ISS.read_text())
        self.assertIn("((NO_PULL == 0)) || NON_INTERACTIVE=1\n", RP.read_text())
        self.assertIn("    1) [[ $COMMAND != install ]] || NO_PULL=1 ;;\n", ISS.read_text())
        self.assertIn("    1) case $COMMAND in status|uninstall) ;; *) NO_PULL=1 ;; esac ;;\n", RP.read_text())
        self.assertIn("NO_PULL=$NO_PULL_OPTION\ncase ${KIT_NO_PULL:-} in\n    ''|0) ;;\n    1) NO_PULL=1 ;;\n", APP.read_text())

    def test_the_pins_are_not_changed_and_each_script_names_its_platform(self):
        # The pins are an owner decision (Q10 of the rebuild plan of .13): this work changes none of them.
        self.assertEqual(pin(ISS, "IMAGE"), "perodua-deploy.novutal.com/iss-oracle-api:v1.0.0@sha256:"
                         "5b8a32cc339217a6b6a9b35dd15b5d88253dedb74104389eeb69a3251e3c7843")
        self.assertEqual(pin(RP, "IMAGE"), "perodua-deploy.novutal.com/rp-adapter:v0.1.0@sha256:"
                         "6c5fc88ac8f39d1075191eb7f20b8c057eebcc57a77fa89123c79edc93513cf1")
        self.assertEqual([pin(APP, key).split("@")[1][7:15] for key in
                          ("ODOO_IMAGE", "WEB_IMAGE", "DEMO_ODOO_IMAGE", "DEMO_WEB_IMAGE")],
                         ["772500ec", "477ec3df", "74286eb8", "0cd02045"])
        for path in (ISS, RP, APP):
            text = path.read_text()
            with self.subTest(script=path.name):
                self.assertEqual(re.findall(r"^PLATFORM=(\S+)", text, re.M), ["linux/amd64"])
        for path in (ISS, RP):
            text = path.read_text()
            with self.subTest(script=path.name):
                self.assertIn("\nREPOSITORY=${IMAGE%:*@*}  ", text)
                self.assertIn("\nDIGEST=${IMAGE##*@}\nPINNED=$REPOSITORY@$DIGEST ", text)
                self.assertIn("\nSTREAM_TAG=$REPOSITORY:${DIGEST/:/-} ", text)
        self.assertIn("    REPOSITORY=${1%:*@*} DIGEST=${1##*@}\n    PINNED=$REPOSITORY@$DIGEST STREAM_TAG=$REPOSITORY:${DIGEST/:/-}\n",
                      APP.read_text())

    def test_the_usage_names_the_option(self):
        for path, line in ((ISS, "install [--config PATH] [--non-interactive] [--no-pull] [--dir PATH]"),
                           (RP, "install [--config PATH] [--non-interactive] [--no-pull] [--dir PATH]"),
                           (APP, "[--non-interactive] [--no-pull] [--init-db]")):
            text = path.read_text()
            with self.subTest(script=path.name):
                self.assertIn(line, text)
                self.assertIn("KIT_NO_PULL=1 in the environment (sudo env KIT_NO_PULL=1 bash", text)
                self.assertIn("stream-image.sh SSH_HOST", text)


# ─────────────────────────────────────────────────── the option and the variable, before Docker (Linux)
@unittest.skipUnless(LINUX, "needs Linux (GNU tools and bash 4 or later)")
class NoPullOptionTest(unittest.TestCase):
    """Refusals that come before the root check and before any Docker call."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="perodua-no-pull-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.env = without_variable(os.environ)
        self.env.pop("KIT_NO_PULL", None)

    def run_script(self, path, *args, **env):
        return subprocess.run(["bash", str(path), *map(str, args)], env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def test_an_unknown_value_of_the_variable_stops_at_once(self):
        for path, args, code in ((ISS, ["install", "--dir", self.tmp / "iss"], 1),
                                 (RP, ["install", "--dir", self.tmp / "rp"], 3),
                                 (APP, ["--dir", self.tmp / "app"], 1)):
            for value in ("yes", "true", "2", " 1"):
                with self.subTest(script=path.name, value=value):
                    result = self.run_script(path, *args, KIT_NO_PULL=value)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertIn("KIT_NO_PULL must be 1 (never pull: use the image", result.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_the_option_belongs_to_the_command_that_deploys(self):
        for path, args, code, message in (
                (ISS, ["status", "--no-pull"], 1, "--no-pull belongs to install"),
                (ISS, ["start", "--no-pull"], 1, "--no-pull belongs to install"),
                (RP, ["health", "--no-pull"], 3, "--no-pull belongs to install (KIT_NO_PULL=1 covers the other commands)"),
                (RP, ["status", "--no-pull"], 3, "--no-pull belongs to install"),
                (APP, ["--check-config", "--no-pull"], 1, "--no-pull belongs to a deployment: not with --check-config, "
                                                          "--release-queued-mail or --send-test-mail"),
                (APP, ["--release-queued-mail", "--no-pull"], 1, "--no-pull belongs to a deployment"),
                (APP, ["--send-test-mail", "test@example.com", "--no-pull"], 1, "--no-pull belongs to a deployment"),
                (APP, ["--mail", "--no-pull"], 1, "--mail asks the outgoing email questions, and --no-pull (or "
                                                   "KIT_NO_PULL=1) never asks.")):
            with self.subTest(script=path.name, args=args):
                result = self.run_script(path, *args, "--dir", self.tmp / "dir")
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(message, result.stderr)
        result = self.run_script(APP, "--mail", "--dir", self.tmp / "dir", KIT_NO_PULL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--mail asks the outgoing email questions, and --no-pull (or KIT_NO_PULL=1) never asks.", result.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_check_config_takes_the_variable(self):
        # service.sh reset runs deploy-app.sh --check-config: the variable of its environment reaches it.
        password = self.tmp / "db-password"
        password.write_text("fixture-password\n")
        config = self.tmp / "app.env"
        config.write_text(f"DB_HOST=192.0.2.20\nDB_PASSWORD_FILE={password}\nBIND_IP=127.0.0.1\n")
        result = self.run_script(APP, "--config", config, "--dir", self.tmp / "app", "--check-config", KIT_NO_PULL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Configuration valid", result.stdout)
        self.assertFalse((self.tmp / "app").exists())


# ─────────────────────────────────────────────────────────────── iss-api.sh
@needs_root_on_linux
class IssApiNoPullTest(NoPullFake, unittest.TestCase):
    IMAGE = iss_tests.IMAGE

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = Path(os.path.realpath(self.tmp)) / "deploy"
        self.lock = iss_tests.lock_path(self.dir)
        for path in (Path(self.lock), Path(self.lock).with_name(f"iss-api-project-{iss_tests.PROJECT}.lock")):
            self.addCleanup(path.unlink, missing_ok=True)
        extra = self.install_fake(iss_tests.FAKE_DOCKER, self.dir)
        for name, text in (("ss", iss_tests.FAKE_SS), ("mv", iss_tests.FAKE_MV), ("tar", iss_tests.FAKE_TAR)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.inner_state = self.tmp / "inner-state.json"
        self.inner_state.write_text(json.dumps({"project": iss_tests.PROJECT, "image_present": False, "listening": False,
                                                "port": 8000, "owner": "", "up_status": [], "config_status": 0}))
        self.env = dict(without_variable(os.environ), PATH=f"{self.bin}:{os.environ['PATH']}",
                        FAKE_DOCKER_STATE=str(self.inner_state), FAKE_DOCKER_LOG=str(self.tmp / "inner.log"),
                        FAKE_LOCK=self.lock, FAKE_MV_ONCE=str(self.tmp / "mv-failed"), REAL_MV=shutil.which("mv"),
                        REAL_TAR=shutil.which("tar"), **extra)
        for name in ("DOCKER_CONFIG", "KIT_NO_PULL"):
            self.env.pop(name, None)
        password = self.tmp / "db-password"
        password.write_text("made-up-oracle-password\n")
        self.config = self.tmp / "iss-api.conf"
        self.config.write_text(f"DB_HOST=db.example.internal\nDB_SERVICE=ORCLPDB1\nDB_USER=API$USR\nDB_PASSWORD_FILE={password}\n")

    def run_script(self, *args, **env):
        self.no_pull_log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(ISS), *args, "--dir", str(self.dir)], env=dict(self.env, **env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def install(self, *args, **env):
        return self.run_script("install", "--config", str(self.config), "--non-interactive", *args, **env)

    def test_no_pull_installs_with_the_image_on_the_server_and_never_pulls_or_logs_in(self):
        self.on_server(self.IMAGE)
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(f"Pinned image on this server (containerd image store), not pulled: {pinned(self.IMAGE)}\n", result.stdout)
        self.assertIn("Deployment verified: iss-oracle-api-v1.0.0", result.stdout)
        self.assertEqual(self.pulls_and_logins(), [])
        self.assertNotIn(["image", "inspect", self.IMAGE], self.args())  # the first step of a pull
        self.assertChecksBeforeAnyChange([self.IMAGE])
        ups = [args[args.index("up") + 1:] for args in self.args() if args[:1] == ["compose"] and "up" in args]
        self.assertTrue(ups)
        for words in ups:
            self.assertIn(["--pull", "never"], [words[n:n + 2] for n in range(len(words))], words)
        self.assertIn("Local:    the pinned image is on this server (containerd image store)\n",
                      self.run_script("status").stdout)
        # The variable does the same.
        result = self.install(KIT_NO_PULL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not pulled", result.stdout)
        self.assertEqual(self.pulls_and_logins(), [])

    def test_no_pull_without_the_image_stops_before_any_change(self):
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(missing_message(self.IMAGE, "iss-api.sh"), result.stderr)
        self.assertFalse(self.dir.exists())
        self.assertLessEqual({tuple(args) for args in self.args()},
                             {("compose", "version"), tuple(iss_tests.OWNER), tuple(INFO),
                              ("image", "inspect", "--format", FACTS, pinned(self.IMAGE))})
        # An installed release stays as it was.
        self.on_server(self.IMAGE)
        self.assertEqual(self.install("--no-pull").returncode, 0)
        before = snapshot(self.dir)
        self.server(images={})
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn(missing_message(self.IMAGE, "iss-api.sh"), result.stderr)
        self.assertEqual(snapshot(self.dir), before)
        self.assertEqual(self.pulls_and_logins(), [])
        self.assertIn("Local:    the pinned image is not on this server: install pulls it, install --no-pull needs it "
                      "streamed first\n", self.run_script("status").stdout)

    def test_no_pull_refuses_another_digest_and_an_image_that_does_not_start(self):
        reference, digest = pinned(self.IMAGE), parts(self.IMAGE)[1]
        for found in (f"{OTHER} {reference}", f"{digest} {parts(self.IMAGE)[0]}@{OTHER}", digest):
            with self.subTest(facts=found):
                self.server(images={reference: found})
                result = self.install("--no-pull")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"ERROR: The image that {reference} finds on this server does not have the pinned digest "
                              "as its registry digest (and, on the containerd image store, as its ID). Remove it (sudo "
                              f"docker image rm {reference}), stream the pinned image again, then run this command again. "
                              "Nothing was changed.\n", result.stderr)
                self.assertFalse(self.dir.exists())
        error = "docker: Error response from daemon: failed to extract layer sha256:0123: unexpected EOF"
        self.on_server(self.IMAGE, start_errors={reference: error})
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: The pinned image on this server does not start: {error}. Remove it (sudo docker image rm "
                      f"{reference}), stream the pinned image again, then run this command again. Nothing was changed.\n",
                      result.stderr)
        self.assertIn(start(self.IMAGE), self.args())
        self.assertFalse(self.dir.exists())

    def test_no_pull_on_the_classic_image_store(self):
        reference = pinned(self.IMAGE)
        self.server(store="classic")
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: The pinned image is not on this server, and --no-pull does not pull it: {reference}. The "
                      "Docker engine of this server uses the classic image store: there an image from docker load has no "
                      "registry digest, so install cannot check it against the pinned digest and does not use it. Switch "
                      "the engine to the containerd image store first (README.md, \"Install from streamed images\"), then "
                      "stream the image. Nothing was changed.\n", result.stderr)
        self.assertNotIn("docker save", result.stderr)
        self.assertFalse(self.dir.exists())
        # A pull brought it before: its registry digest is the pin, its ID the digest of its config.
        self.server(images={reference: f"{CONFIG_ID} {reference}"})
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Pinned image on this server (classic image store), not pulled: {reference}\n", result.stdout)
        self.server(store="")
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: The Docker engine does not answer docker info. Nothing was changed.\n", result.stderr)
        self.assertIn("Local:    not checked (docker info does not answer)\n", self.run_script("status").stdout)

    def test_without_no_pull_the_image_step_is_unchanged(self):
        self.server(forbid_pull=False)
        for env in ({}, {"KIT_NO_PULL": "0"}, {"KIT_NO_PULL": ""}):
            with self.subTest(env=env):
                result = self.install(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(INFO, self.args())
                self.assertNotIn("not pulled", result.stdout)
                self.assertIn(["image", "inspect", self.IMAGE], self.args())
        result = self.run_script("status", KIT_NO_PULL="1")  # the variable is for install: status takes it
        self.assertNotIn("KIT_NO_PULL", result.stderr)
        self.assertIn("Local:    the pinned image is not on this server", result.stdout)

    def test_no_pull_never_asks_on_a_terminal(self):
        # Without --non-interactive and without settings: no question, the run names --config.
        env = dict(self.env)
        code, output = on_terminal(["bash", ISS, "install", "--no-pull", "--dir", self.dir], env)
        self.assertIsNotNone(code, output)
        self.assertNotEqual(code, 0, output)
        self.assertIn("No settings yet", output)
        for question in QUESTIONS + ("Oracle database server",):
            self.assertNotIn(question, output)
        self.on_server(self.IMAGE)
        code, output = on_terminal(["bash", ISS, "install", "--config", self.config, "--dir", self.dir], dict(env, KIT_NO_PULL="1"))
        self.assertEqual(code, 0, output)
        for question in QUESTIONS:
            self.assertNotIn(question, output)
        self.assertEqual(self.pulls_and_logins(), [])


# ─────────────────────────────────────────────────────────────── rp-adapter.sh
@needs_root_on_linux
class RpAdapterNoPullTest(NoPullFake, unittest.TestCase):
    IMAGE = rp_tests.IMAGE
    USER = "10001:10001"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = Path(os.path.realpath(self.tmp)) / "deploy"
        extra = self.install_fake(rp_tests.FAKE_DOCKER, self.dir)
        for tool in ("mv", "cp"):
            (self.bin / tool).write_text(rp_tests.FAKE_MV)
            (self.bin / tool).chmod(0o755)
        self.inner_state = self.tmp / "inner-state.json"
        self.inner_state.write_text(json.dumps({"image": self.IMAGE, "image_present": False, "pull_error": "",
                                                "run_status": []}))
        self.env = dict(without_variable(os.environ), PATH=f"{self.bin}:{os.environ['PATH']}",
                        FAKE_DOCKER_STATE=str(self.inner_state), FAKE_DOCKER_LOG=str(self.tmp / "inner.log"), **extra)
        for name in ("DOCKER_CONFIG", "KIT_NO_PULL"):
            self.env.pop(name, None)
        password = self.tmp / "db-password"
        password.write_text("made-up-oracle-password\n")
        self.config = self.tmp / "settings.env"
        self.config.write_text("DB_HOST=10.1.119.26\nDB_PORT=1521\nDB_SERVICE=ENTPP2\nDB_USER=RP_GATEWAY\n"
                               f"DB_PASSWORD_FILE={password}\nCALL_TIMEOUT=120\nAPI_URL=\nDOCKER_NETWORK=host\n")

    def run_script(self, *args, **env):
        self.no_pull_log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(RP), *args, "--dir", str(self.dir)], env=dict(self.env, **env),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def install(self, *args, **env):
        return self.run_script("install", "--config", str(self.config), "--non-interactive", *args, **env)

    def adapter_runs(self):
        return [args for args in self.args() if args[:2] == ["run", "--rm"] and args != start(self.IMAGE, self.USER)]

    def test_no_pull_installs_with_the_image_on_the_server_and_never_pulls_or_logs_in(self):
        self.on_server(self.IMAGE)
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(f"Pinned image on this server (containerd image store), not pulled: {pinned(self.IMAGE)}\n",
                      result.stderr)  # the standard output of a command is the adapter's
        self.assertIn("Set up and verified", result.stdout)
        self.assertEqual(self.pulls_and_logins(), [])
        self.assertChecksBeforeAnyChange([self.IMAGE], self.USER)
        runs = self.adapter_runs()
        self.assertTrue(runs)
        for args in runs:
            self.assertEqual(args[:4], ["run", "--rm", "--pull", "never"])
        self.assertIn("Local:    the pinned image is on this server (containerd image store)\n",
                      self.run_script("status").stdout)

    def test_no_pull_without_the_image_stops_before_any_change(self):
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 3)
        self.assertIn(missing_message(self.IMAGE, "rp-adapter.sh"), result.stderr)
        self.assertFalse(self.dir.exists())
        self.assertEqual(self.pulls_and_logins(), [])
        # A setup stays as it was, also the files of an install that was stopped while it replaced them: they are
        # put back only after the check.
        self.on_server(self.IMAGE)
        self.assertEqual(self.install("--no-pull").returncode, 0)
        (self.dir / ".publishing").write_text("")
        (self.dir / "rp-adapter.env.old").write_text("DB_HOST=10.0.0.1\n")
        before = snapshot(self.dir)
        self.server(images={})
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 3)
        self.assertIn(missing_message(self.IMAGE, "rp-adapter.sh"), result.stderr)
        self.assertEqual(snapshot(self.dir), before)

    def test_the_variable_covers_the_commands_that_run_the_adapter(self):
        self.on_server(self.IMAGE)
        self.assertEqual(self.install("--no-pull").returncode, 0)
        output = snapshot(self.dir / "output")
        self.server(images={})
        for command in (["health"], ["survey"], ["export", "--all"], ["run", "db", "objects"]):
            with self.subTest(command=command):
                result = self.run_script(*command, KIT_NO_PULL="1")
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(missing_message(self.IMAGE, "rp-adapter.sh"), result.stderr)
                self.assertEqual(self.adapter_runs(), [])
                self.assertEqual(snapshot(self.dir / "output"), output)  # no run folder
                self.assertEqual(self.pulls_and_logins(), [])
        self.on_server(self.IMAGE)
        result = self.run_script("survey", KIT_NO_PULL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not pulled", result.stderr)
        self.assertEqual(result.stdout, "fake adapter output\n")
        self.assertEqual([args[:4] for args in self.adapter_runs()], [["run", "--rm", "--pull", "never"]])
        # Without the variable, a command pulls a missing image as before.
        self.server(forbid_pull=False, images={})
        self.assertEqual(self.run_script("health").returncode, 0)
        self.assertIn(["pull", self.IMAGE], self.args())
        self.assertNotIn(INFO, self.args())

    def test_no_pull_on_the_classic_image_store_and_another_digest(self):
        reference = pinned(self.IMAGE)
        self.server(store="classic")
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 3)
        self.assertIn("The Docker engine of this server uses the classic image store: there an image from docker load "
                      "has no registry digest, so this script cannot check it against the pinned digest", result.stderr)
        self.server(images={reference: f"{CONFIG_ID} {reference}"})
        self.assertEqual(self.install("--no-pull").returncode, 0)
        self.server(store="containerd", images={reference: f"{OTHER} {reference}"})
        result = self.install("--no-pull")
        self.assertEqual(result.returncode, 3)
        self.assertIn(f"ERROR: The image that {reference} finds on this server does not have the pinned digest", result.stderr)
        self.assertIn(f"Local:    the image that {reference} finds here does not have the pinned digest\n",
                      self.run_script("status").stdout)


# ─────────────────────────────────────────────────────────────── deploy-app.sh
@unittest.skipUnless(LINUX, "needs Linux (GNU tools and bash 4 or later)")
class DeployAppNoPullTest(NoPullFake, unittest.TestCase):
    LINES = {"client-stable-uiux": (pin(APP, "ODOO_IMAGE"), pin(APP, "WEB_IMAGE")),
             "demo-uiux": (uat_tests.DEMO_PINS["DEMO_ODOO_IMAGE"], uat_tests.DEMO_PINS["DEMO_WEB_IMAGE"])}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="perodua-no-pull-app-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.dir = self.tmp / "app"
        extra = self.install_fake(uat_tests.FAKE_DOCKER, self.dir)
        password = self.tmp / "db-password"
        password.write_text("fixture-password\n")
        password.chmod(0o600)
        self.config = self.tmp / "app.env"
        self.config.write_text(f"DB_HOST=192.0.2.20\nDB_PORT=5432\nDB_NAME=perodua\nDB_USER=odoo\nDB_PASSWORD_FILE={password}\n"
                               "PROJECT_NAME=perodua-uat-fixture\nHTTP_PORT=18110\nBIND_IP=127.0.0.1\n"
                               "STARTUP_TIMEOUT=600\nINIT_TIMEOUT=600\n")
        self.env = dict(without_variable(os.environ), HOME=str(self.tmp), DOCKER_CONFIG=str(self.tmp / "docker-config"),
                        PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_DOCKER_LOG=str(self.tmp / "inner.jsonl"),
                        FAKE_DEMO_APPLIED=str(self.tmp / "demo-applied.tsv"), FAKE_CHECK="READY 0", **extra)
        self.env.pop("KIT_NO_PULL", None)
        self.script = APP

    def line(self, name):
        if name == "demo-uiux" and self.script == APP:
            self.script = uat_tests.demo_release_copy(self.tmp / "demo-release")
        return ["--line", name]

    def deploy(self, *args, **env):
        self.no_pull_log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(self.script), "--config", str(self.config), "--dir", str(self.dir),
                               "--non-interactive", *args], env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def container_starts(self):
        """Each Compose run or up: the words after the subcommand."""
        found = []
        for args in self.args():
            if args[:1] == ["compose"]:
                for word in ("run", "up"):
                    if word in args[5:]:
                        found.append(args[args.index(word, 5) + 1:])
                        break
        return found

    def test_no_pull_deploys_both_images_of_each_line_without_a_pull(self):
        for name, images in self.LINES.items():
            with self.subTest(line=name):
                shutil.rmtree(self.dir, ignore_errors=True)
                line = self.line(name)
                self.on_server(*images)
                result = self.deploy("--no-pull", *line)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertIn("".join(f"Pinned image on this server (containerd image store), not pulled: {pinned(image)}\n"
                                      for image in images), result.stdout)
                self.assertEqual(self.pulls_and_logins(), [])
                self.assertChecksBeforeAnyChange(images)
                self.assertIn(["compose", "version"], self.args())
                starts = self.container_starts()
                self.assertGreaterEqual(len(starts), 3)  # the checks of the database, and up
                for words in starts:
                    self.assertEqual(words[:2], ["--pull", "never"], words)

    def test_no_pull_without_an_image_stops_before_any_change(self):
        odoo, web = self.LINES["client-stable-uiux"]
        for present, gone in (([web], [odoo]), ([odoo], [web]), ([], [odoo, web])):
            with self.subTest(missing=[pinned(image) for image in gone]):
                self.on_server(*present)
                result = self.deploy("--no-pull")
                self.assertEqual(result.returncode, 1)
                expected = "".join(f"The pinned image is not on this server, and --no-pull does not pull it: {pinned(image)}\n"
                                   for image in gone)
                expected = "Error: " + expected + (
                    f"Stream {'it' if len(gone) == 1 else 'them'} from the Mac (it has the registry login and the "
                    f"containerd image store). There, SSH_HOST is the ssh name of this server (host name {HOST_NAME}):\n"
                    "  bash scripts/stream-image.sh SSH_HOST deploy-app.sh --line client-stable-uiux\n"
                    "or with Docker alone:\n" + "".join(f"  {stream_command(image)}\n" for image in gone)
                    + "Then run this command again. Nothing was changed\n")
                self.assertIn(expected, result.stderr)
                self.assertFalse(self.dir.exists())
                self.assertEqual(self.container_starts(), [])
                self.assertEqual(self.pulls_and_logins(), [])
        # A deployment stays as it was.
        self.on_server(odoo, web)
        self.assertEqual(self.deploy("--no-pull").returncode, 0)
        before = snapshot(self.dir)
        self.on_server(odoo)
        result = self.deploy("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(snapshot(self.dir), before)
        self.assertEqual(self.container_starts(), [])
        # The demo-uiux line names its own images and its line.
        line = self.line("demo-uiux")
        self.server(images={})
        shutil.rmtree(self.dir)
        result = self.deploy("--no-pull", *line)
        self.assertEqual(result.returncode, 1)
        self.assertIn("  bash scripts/stream-image.sh SSH_HOST deploy-app.sh --line demo-uiux\n", result.stderr)
        for image in self.LINES["demo-uiux"]:
            self.assertIn(f"  {stream_command(image)}\n", result.stderr)

    def test_no_pull_refuses_another_digest_a_start_that_fails_and_the_classic_store(self):
        odoo, web = self.LINES["client-stable-uiux"]
        self.on_server(odoo)
        self.server(images={pinned(odoo): facts(odoo), pinned(web): f"{OTHER} {pinned(web)}"})
        result = self.deploy("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Error: The image that {pinned(web)} finds on this server does not have the pinned digest as its "
                      "registry digest (and, on the containerd image store, as its ID). Remove it (sudo docker image rm "
                      f"{pinned(web)}), stream the pinned image again, then run this command again. Nothing was changed\n",
                      result.stderr)
        self.assertFalse(self.dir.exists())
        error = "docker: Error response from daemon: failed to extract layer sha256:0123: unexpected EOF"
        self.on_server(odoo, web, start_errors={pinned(odoo): error})
        result = self.deploy("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"Error: The pinned image {pinned(odoo)} on this server does not start: {error}. Remove it", result.stderr)
        self.assertFalse(self.dir.exists())
        self.server(store="classic", images={}, start_errors={})
        result = self.deploy("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn("The Docker engine of this server uses the classic image store: there an image from docker load has "
                      "no registry digest, so deploy-app.sh cannot check it against the pinned digest", result.stderr)
        self.assertNotIn("docker save", result.stderr)
        self.server(images={pinned(image): f"{CONFIG_ID} {pinned(image)}" for image in (odoo, web)})
        result = self.deploy("--no-pull")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Pinned image on this server (classic image store), not pulled: {pinned(web)}\n", result.stdout)
        self.server(store="")
        shutil.rmtree(self.dir)
        result = self.deploy("--no-pull")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error: The Docker engine does not answer docker info. Nothing was changed\n", result.stderr)
        self.assertFalse(self.dir.exists())

    def test_no_pull_needs_compose_2_32_2(self):
        odoo, web = self.LINES["client-stable-uiux"]
        self.on_server(odoo, web)
        for version in ("Docker Compose version v2.32.1", "Docker Compose version v2.8.0", "Docker Compose version 1.29.2"):
            with self.subTest(version=version):
                self.server(compose_version=version)
                result = self.deploy("--no-pull")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Error: --no-pull starts every container with --pull never, and docker compose run has that "
                              f"option from Docker Compose 2.32.2 on. This server answers: {version}. Update the Compose "
                              "plugin first", result.stderr)
                self.assertFalse(self.dir.exists())
                self.assertNotIn(INFO, self.args())
        for version in ("Docker Compose version v2.32.2", "Docker Compose version 2.40.3", "Docker Compose version v5.0.0"):
            with self.subTest(version=version):
                shutil.rmtree(self.dir, ignore_errors=True)
                self.server(compose_version=version)
                self.assertEqual(self.deploy("--no-pull").returncode, 0)

    def test_the_variable_reaches_the_mail_runs_without_an_image_check(self):
        odoo, web = self.LINES["client-stable-uiux"]
        self.on_server(odoo, web)
        self.assertEqual(self.deploy("--no-pull").returncode, 0)
        self.server(images={})  # not checked by these runs
        self.no_pull_log.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(self.script), "--dir", str(self.dir), "--release-queued-mail"],
                                env=dict(self.env, KIT_NO_PULL="1"), stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 held mail(s) are queued again", result.stdout)
        self.assertNotIn(INFO, self.args())
        self.assertEqual([words[:2] for words in self.container_starts()], [["--pull", "never"]])
        self.assertEqual(self.pulls_and_logins(), [])

    def test_without_no_pull_the_run_is_unchanged(self):
        self.server(forbid_pull=False)
        odoo, web = self.LINES["client-stable-uiux"]
        for env in ({}, {"KIT_NO_PULL": "0"}):
            with self.subTest(env=env):
                shutil.rmtree(self.dir, ignore_errors=True)
                result = self.deploy(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.pulls_and_logins(), [["pull", odoo], ["pull", web]])
                self.assertNotIn(INFO, self.args())
                for words in self.container_starts():
                    self.assertNotIn("--pull", words)

    def test_no_pull_never_asks_on_a_terminal(self):
        for args, env in ((["--no-pull"], {}), ([], {"KIT_NO_PULL": "1"})):
            with self.subTest(args=args, env=env):
                code, output = on_terminal(["bash", APP, "--dir", self.dir, *args], dict(self.env, **env))
                self.assertIsNotNone(code, output)
                self.assertNotEqual(code, 0, output)
                self.assertIn("DB_HOST is required in --config", output)
                for question in QUESTIONS + ("Database server IP",):
                    self.assertNotIn(question, output)
                self.assertFalse(self.dir.exists())


# ─────────────────────────────────────────────────────────────── stream-image.sh on the Mac
STREAM_TEXT = STREAM.read_text()
STREAM_BASH = "/bin/bash"  # on the Mac, the bash of macOS (3.2), as the owner runs the helper there
SSH_OPTIONS = ["-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ClearAllForwardings=yes",
               "-o", "PermitLocalCommand=no", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
SERVER_VERSION = "sudo -n docker version --format '{{.Server.APIVersion}}'"
SERVER_INFO = "sudo -n docker info --format '{{json .DriverStatus}}'"
SERVER_INSPECT = f"sudo -n docker image inspect --format '{FACTS}' "
SERVER_LOAD = "sudo -n docker load"
SERVER_MKDIR = "mkdir -p -m 0700 stream-image && chmod 0700 stream-image"
SERVER_TOOLS = "command -v rsync && command -v sha256sum"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
# docker buildx imagetools inspect --format of the helper: one line for each entry of an index.
ENTRIES = ('{{range .Manifest.Manifests}}{{.Digest}} {{.MediaType}} {{with .Platform}}{{.OS}}/{{.Architecture}}'
           '{{with .Variant}}/{{.}}{{end}}{{else}}-{{end}} {{with .Annotations}}'
           '{{or (index . "vnd.docker.reference.type") "-"}}{{else}}-{{end}}{{"\\n"}}{{end}}')
AMD64 = f"sha256:{'a' * 64} {MANIFEST} linux/amd64 -"  # the entries of an index, as ENTRIES prints them
ARM64 = f"sha256:{'b' * 64} {MANIFEST} linux/arm64 -"
ATTESTATION = f"sha256:{'d' * 64} {MANIFEST} unknown/unknown attestation-manifest"
TERMINAL = "typed on the terminal\n"  # the standard input of the helper: no check on the server may read it
# The pins of the kit scripts, and the rows 1-3 of the image table of the rebuild plan of .13 (section 3.1).
KIT_PINS = {"iss-api.sh": [pin(ISS, "IMAGE")], "rp-adapter.sh": [pin(RP, "IMAGE")], "oracle-view.sh": [pin(OV, "IMAGE")],
            "client-stable-uiux": [pin(APP, "ODOO_IMAGE"), pin(APP, "WEB_IMAGE")],
            "demo-uiux": [pin(APP, "DEMO_ODOO_IMAGE"), pin(APP, "DEMO_WEB_IMAGE")]}
REBUILD = ["perodua-deploy.novutal.com/perodua-odoo:demo-uiux-grey-v0.60.0-oracle2@sha256:"
           "cc0bc93d79c8e74902196a955198605267eb2570625d1436d8658baf92892244",
           "perodua-deploy.novutal.com/perodua-odoo:demo-uiux-grey-web-v0.60.0-oracle2@sha256:"
           "468b48c3cb74c1208dd45e02bd3db917347148b031b7442689cbd1ec1d1ac855",
           "perodua-deploy.novutal.com/rp-adapter:v0.8.0-oracle2@sha256:"
           "9df3d38f91cf9ea110c152343b969f94997bef5a649e95a6a3b267a4b1f378fa"]
ALL_PINS = sorted({image for images in KIT_PINS.values() for image in images} | set(REBUILD))
# The pins that are an OCI index of two entries, the linux/amd64 image and its BuildKit attestation manifest
# (docker buildx imagetools inspect --raw, 2026-11-10); the other pins are one image manifest each.
INDEX_PINS = [KIT_PINS["iss-api.sh"][0], KIT_PINS["rp-adapter.sh"][0], KIT_PINS["client-stable-uiux"][0],
              KIT_PINS["demo-uiux"][0]]


def archive(image):  # what the fake docker save writes for the stream tag of a pin
    tag = parts(image)[3]
    return f"archive of {tag} -> {parts(image)[1]}\n" + "layer data\n" * 400


def archive_name(image):
    return re.sub(r"[^A-Za-z0-9._-]", "_", parts(image)[3]) + ".tar"


FAKE_MAC_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
MANIFEST = ''' + repr(MANIFEST) + r'''
INDEX = ''' + repr(INDEX) + r'''
ENTRIES = ''' + repr(ENTRIES) + r'''
with open(os.environ["FAKE_STREAM_STATE"]) as f:
    state = json.load(f)
mac = state["mac"]
entry = {"tool": "docker", "args": args}
out, err, status = "", "", 0
def reference(image):
    name, digest = image.split("@")
    return name[:name.rindex(":")] + "@" + digest
def archive(tag):
    return f"archive of {tag} -> {mac['tags'][tag]}\n" + "layer data\n" * 400
def missing(ref):
    return "", f"Error response from daemon: No such image: {ref}\n", 1
if args == ["info", "--format", "{{json .DriverStatus}}"]:
    if mac["store"] == "containerd":
        out = '[["driver-type","io.containerd.snapshotter.v1"]]\n'
    elif mac["store"] == "classic":
        out = '[["Backing Filesystem","extfs"]]\n'
    else:
        err, status = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n", 1
elif args[:3] == ["image", "inspect", "--format"] and len(args) == 5 and args[3] == "{{.Id}}":
    if args[4] in mac["images"]:
        out = mac["images"][args[4]]["id"] + "\n"
    elif args[4] in mac["tags"]:
        out = mac["tags"][args[4]] + "\n"
    else:
        out, err, status = missing(args[4])
elif args[:3] == ["image", "inspect", "--format"] and len(args) == 5 and args[3] == "{{json .Descriptor}}":
    if args[4] in mac["images"]:
        image = mac["images"][args[4]]
        out = json.dumps({"mediaType": image["kind"], "digest": image["id"], "size": 1234}) + "\n"
    else:
        out, err, status = missing(args[4])
elif args[:2] == ["pull", "--platform"] and len(args) == 4:
    if mac["logged_in"]:
        kind = INDEX if reference(args[3]) in mac["registry"] else mac["pull_kind"]
        mac["images"][reference(args[3])] = {"id": mac["pull_ids"].get(args[3], args[3].split("@")[1]), "kind": kind}
    else:
        err, status = "Error response from daemon: unauthorized: authentication required\n", 1
elif args[:3] == ["buildx", "imagetools", "inspect"] and (len(args) == 5 and args[3] == "--raw"
                                                          or len(args) == 6 and args[3:5] == ["--format", ENTRIES]):
    # The index in the registry: its bytes (FAKE_SHASUM gives them the digest they name) or its entries.
    index = mac["registry"].get(args[-1])
    if not mac["logged_in"]:
        err, status = "ERROR: failed to authorize: failed to fetch oauth token: 401 Unauthorized\n", 1
    elif index is None:
        err, status = f"ERROR: {args[-1]}: not found\n", 1
    elif args[3] == "--raw":
        out = f"raw index {index['digest']}\n"
    else:
        out = "".join(line + "\n" for line in index["entries"])
elif args[:1] == ["tag"] and len(args) == 3:
    if mac["tag_error"]:
        err, status = mac["tag_error"] + "\n", 1
    else:
        mac["tags"][args[2]] = mac["images"][args[1]]["id"]
elif args[:1] == ["save"] and len(args) in (2, 4):
    if mac["save_error"]:
        err, status = mac["save_error"] + "\n", 1
    elif len(args) == 2:
        out = archive(args[1])
    elif args[1] == "-o":
        with open(args[2], "w") as f:
            f.write(archive(args[3]))
    else:
        sys.exit(f"fake docker: unsupported command {args}")
else:
    sys.exit(f"fake docker: unsupported command {args}")
with open(os.environ["FAKE_STREAM_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
with open(os.environ["FAKE_STREAM_STATE"] + f".{os.getpid()}", "w") as f:  # docker save and ssh load run at once
    json.dump(state, f)
os.replace(os.environ["FAKE_STREAM_STATE"] + f".{os.getpid()}", os.environ["FAKE_STREAM_STATE"])
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(status)
'''
# ssh to the server: the options of the helper only, and the commands of the server answered from its state. Without
# -n it reads the standard input, as ssh does; ssh -G prints the host name of the ssh configuration. docker load keeps
# the name of the archive, so the digest reference finds the image on the containerd image store. The home folder of
# the ssh user is a folder of the test.
FAKE_SSH = r'''#!/usr/bin/env python3
import hashlib, json, os, re, sys
OPTIONS = ''' + repr(SSH_OPTIONS) + r'''
VERSION, INFO, INSPECT, LOAD, MKDIR, TOOLS = ''' + repr((SERVER_VERSION, SERVER_INFO, SERVER_INSPECT, SERVER_LOAD,
                                                        SERVER_MKDIR, SERVER_TOOLS)) + r'''
args = sys.argv[1:]
with open(os.environ["FAKE_STREAM_STATE"]) as f:
    state = json.load(f)
server = state["server"]
home = server["home"]
if args[:1] == ["-G"]:
    host = args[-1]
    if args not in (["-G", host], ["-G", "-o", "BatchMode=yes", host]):
        sys.exit(f"fake ssh: unexpected arguments {args}")
    print(f"user me\nhostname {server['names'].get(host, host.split('@')[-1])}\nport 22\nbatchmode yes")
    sys.exit(0)
rest = args[len(OPTIONS):]
if args[:len(OPTIONS)] != OPTIONS or len(rest) not in (2, 3) or (len(rest) == 3 and rest[0] != "-n"):
    sys.exit(f"fake ssh: unexpected arguments {args}")
host, command = rest[-2:]
entry = {"tool": "ssh", "args": args, "host": host, "command": command, "n": rest[0] == "-n"}
if not entry["n"]:
    entry["stdin"] = sys.stdin.read()
out, err, status = "", "", 0
def load(text):
    first = text.split("\n", 1)[0]
    match = re.fullmatch(r"archive of (\S+) -> (\S+)", first)
    if not match:
        return "", "Error unpacking image: invalid tar header\n", 1
    tag, image_id = match.groups()
    repository, digest = tag[:tag.rindex(":")], tag[tag.rindex(":") + 1:].replace("-", ":", 1)
    if image_id == digest and server["store"] == "containerd" and not server["dangling"]:
        server["images"][f"{repository}@{digest}"] = f"{image_id} {repository}@{digest}"
    return server["load_output"] or f"Loaded image: {tag}\n", "", 0
if not server["reachable"]:
    err, status = f"ssh: connect to host {host} port 22: Connection refused\n", 255
elif not server["sudo"] and command.startswith("sudo "):
    err, status = "sudo: a password is required\n", 1
elif command == VERSION:
    out = "1.52\n"
elif command == INFO:
    if server["info_error"]:
        err, status = server["info_error"] + "\n", 1
    else:
        out = ('[["driver-type","io.containerd.snapshotter.v1"]]' if server["store"] == "containerd"
               else '[["Backing Filesystem","extfs"]]') + "\n"
elif command.startswith(INSPECT):
    ref = command[len(INSPECT):]
    if server["images"].get(ref):
        out = server["images"][ref] + "\n"
    else:
        err, status = f"Error response from daemon: No such image: {ref}\n", 1
elif command == LOAD:
    if entry.get("stdin", "") == "":
        err, status = "requested load from stdin, but stdin is empty\n", 1
    elif server["load_error"]:
        err, status = server["load_error"] + "\n", 1
    else:
        out, err, status = load(entry["stdin"])
elif command == MKDIR:
    os.makedirs(os.path.join(home, "stream-image"), mode=0o700, exist_ok=True)
    os.chmod(os.path.join(home, "stream-image"), 0o700)
elif command == TOOLS:
    if server["tools"]:
        out = "/usr/bin/rsync\n/usr/bin/sha256sum\n"
    else:
        status = 1
elif command.startswith("sha256sum stream-image/"):
    path = os.path.join(home, command.split(" ", 1)[1])
    if os.path.isfile(path):
        out = hashlib.sha256(open(path, "rb").read()).hexdigest() + "  " + command.split(" ", 1)[1] + "\n"
    else:
        err, status = f"sha256sum: {command.split(' ', 1)[1]}: No such file or directory\n", 1
elif command.startswith("sudo -n docker load -i stream-image/"):
    path = os.path.join(home, command.rsplit(" ", 1)[1])
    if server["load_error"]:
        err, status = server["load_error"] + "\n", 1
    else:
        out, err, status = load(open(path).read())
elif command.startswith("rm -f stream-image/"):
    path = os.path.join(home, command.rsplit(" ", 1)[1])
    if os.path.exists(path):
        os.remove(path)
else:
    sys.exit(f"fake ssh: unsupported command {command!r}")
with open(os.environ["FAKE_STREAM_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
with open(os.environ["FAKE_STREAM_STATE"] + f".{os.getpid()}", "w") as f:  # docker save and ssh load run at once
    json.dump(state, f)
os.replace(os.environ["FAKE_STREAM_STATE"] + f".{os.getpid()}", os.environ["FAKE_STREAM_STATE"])
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(status)
'''
# rsync --partial -e "ssh OPTIONS" ARCHIVE HOST:stream-image/NAME: a copy into the home folder of the server. A break
# keeps the part that came over, as --partial does.
FAKE_RSYNC = r'''#!/usr/bin/env python3
import json, os, sys
SSH = ''' + repr(" ".join(["ssh"] + SSH_OPTIONS)) + r'''
args = sys.argv[1:]
with open(os.environ["FAKE_STREAM_STATE"]) as f:
    state = json.load(f)
server = state["server"]
if len(args) != 5 or args[:3] != ["--partial", "-e", SSH] or ":" not in args[4]:
    sys.exit(f"fake rsync: unexpected arguments {args}")
source, (host, path) = args[3], args[4].split(":", 1)
target = os.path.join(server["home"], path)
entry = {"tool": "rsync", "args": args, "host": host,
         "part_before": os.path.getsize(target) if os.path.exists(target) else None}
data = open(source, "rb").read()
status, err = 0, ""
if not os.path.isdir(os.path.dirname(target)):
    err, status = "rsync: mkstemp failed: No such file or directory (2)\n", 23
elif server["rsync_breaks"] > 0:
    server["rsync_breaks"] -= 1
    open(target, "wb").write(data[:len(data) // 2])
    err, status = ("rsync: connection unexpectedly closed (1024 bytes received so far) [sender]\n"
                   "rsync error: error in rsync protocol data stream (code 12)\n"), 12
else:
    open(target, "wb").write(data + (b"x" if server["rsync_corrupt"] else b""))
with open(os.environ["FAKE_STREAM_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
with open(os.environ["FAKE_STREAM_STATE"], "w") as f:
    json.dump(state, f)
sys.stderr.write(err)
sys.exit(status)
'''
# shasum -a 256 FILE, or the standard input: the raw index of the fake buildx gets the digest that it names.
FAKE_SHASUM = r'''#!/usr/bin/env python3
import hashlib, json, os, re, sys
args = sys.argv[1:]
if len(args) not in (2, 3) or args[:2] != ["-a", "256"]:
    sys.exit(f"fake shasum: unexpected arguments {args}")
with open(os.environ["FAKE_STREAM_LOG"], "a") as log:
    log.write(json.dumps({"tool": "shasum", "args": args}) + "\n")
if len(args) == 3:
    print(hashlib.sha256(open(args[2], "rb").read()).hexdigest() + "  " + args[2])
else:
    data = sys.stdin.read()
    named = re.fullmatch(r"raw index sha256:([0-9a-f]{64})\n", data)
    print((named.group(1) if named else hashlib.sha256(data.encode()).hexdigest()) + "  -")
'''


class StreamImageTest(unittest.TestCase):
    """stream-image.sh, run on the Mac, against fakes of docker, ssh, rsync and shasum."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.log = self.tmp / "calls.log"
        self.state = self.tmp / "state.json"
        self.home = self.tmp / "server-home"
        self.home.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, text in (("docker", FAKE_MAC_DOCKER), ("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC), ("shasum", FAKE_SHASUM)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = dict(without_variable(os.environ), PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=str(self.tmp),
                        FAKE_STREAM_STATE=str(self.state), FAKE_STREAM_LOG=str(self.log))
        self.archives = self.tmp / "stream-image-archives"
        self.state.write_text(json.dumps({
            "mac": {"store": "containerd", "logged_in": True, "pull_ids": {}, "pull_kind": MANIFEST, "tag_error": "",
                    "save_error": "", "tags": {},
                    "images": {pinned(image): {"id": parts(image)[1], "kind": INDEX if image in INDEX_PINS else MANIFEST}
                               for image in ALL_PINS},
                    "registry": {pinned(image): {"digest": parts(image)[1], "entries": [AMD64, ATTESTATION]}
                                 for image in INDEX_PINS}},
            "server": {"reachable": True, "sudo": True, "store": "containerd", "images": {}, "info_error": "",
                       "load_error": "", "load_output": "", "dangling": False, "names": {"grey-alias": "grey"},
                       "home": str(self.home), "tools": True, "rsync_breaks": 0, "rsync_corrupt": False}}))

    def set_state(self, mac=None, server=None):
        state = json.loads(self.state.read_text())
        state["mac"].update(mac or {})
        state["server"].update(server or {})
        self.state.write_text(json.dumps(state))

    def get_state(self):
        return json.loads(self.state.read_text())

    def stream(self, *args, **env):
        self.log.unlink(missing_ok=True)
        return subprocess.run([STREAM_BASH, str(STREAM), *args], env=dict(self.env, **env), input=TERMINAL,
                              capture_output=True, text=True, timeout=60)

    def calls(self, tool):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [call for call in map(json.loads, lines) if call["tool"] == tool]

    def docker(self):
        return [call["args"] for call in self.calls("docker")]

    def server(self):
        return [call["command"] for call in self.calls("ssh")]

    def saves(self):
        return [args[-1] for args in self.docker() if args[:1] == ["save"]]

    def locks(self):
        return sorted(path.name for path in self.tmp.iterdir() if path.name.endswith(".lock"))

    def assertNothingStreamed(self):
        self.assertEqual(self.saves(), [])
        self.assertEqual([command for command in self.server() if "docker load" in command], [])
        self.assertEqual(self.calls("rsync"), [])

    def test_each_source_streams_the_pins_that_its_script_holds(self):
        for args, images, steps in (
                (["iss-api.sh"], KIT_PINS["iss-api.sh"], ["sudo bash iss-api.sh install --no-pull --non-interactive"]),
                (["rp-adapter.sh"], KIT_PINS["rp-adapter.sh"], ["sudo bash rp-adapter.sh install --no-pull --non-interactive"]),
                (["oracle-view.sh"], KIT_PINS["oracle-view.sh"], ["sudo bash oracle-view.sh install --no-pull --non-interactive"]),
                (["deploy-app.sh"], KIT_PINS["client-stable-uiux"],
                 ["sudo bash deploy-app.sh --no-pull --non-interactive --line client-stable-uiux (with the --config and "
                  "--dir of the deployment)"]),
                (["deploy-app.sh", "--line", "demo-uiux"], KIT_PINS["demo-uiux"],
                 ["sudo bash deploy-app.sh --no-pull --non-interactive --line demo-uiux (with the --config and --dir of "
                  "the deployment)"]),
                (["deploy-app.sh", "--line", "demo-uiux", "iss-api.sh", "rp-adapter.sh", "iss-api.sh"],
                 KIT_PINS["demo-uiux"] + KIT_PINS["iss-api.sh"] + KIT_PINS["rp-adapter.sh"],
                 ["sudo bash deploy-app.sh --no-pull --non-interactive --line demo-uiux (with the --config and --dir of "
                  "the deployment)", "sudo bash iss-api.sh install --no-pull --non-interactive",
                  "sudo bash rp-adapter.sh install --no-pull --non-interactive",
                  "sudo bash iss-api.sh install --no-pull --non-interactive"])):
            with self.subTest(args=args):
                self.set_state(mac={"tags": {}}, server={"images": {}})
                result = self.stream("grey", *args)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(self.saves(), [parts(image)[3] for image in images])  # each pin once, in order
                server = self.get_state()["server"]["images"]
                self.assertEqual(server, {pinned(image): facts(image) for image in images})
                for image in images:
                    self.assertIn(f"Streaming {parts(image)[3]} to grey...\nLoaded image: {parts(image)[3]}\n"
                                  f"On grey: {pinned(image)}, ID {parts(image)[1]}\n", result.stdout)
                self.assertTrue(result.stdout.endswith("".join(f"Next, on grey: {step}\n" for step in steps)), result.stdout)
                loads = [call for call in self.calls("ssh") if call["command"] == SERVER_LOAD]
                self.assertEqual([call["stdin"] for call in loads], [archive(image) for image in images])
                self.assertEqual(self.locks(), [])

    def test_the_order_of_the_checks_and_the_stream(self):
        image = KIT_PINS["iss-api.sh"][0]  # an index: the linux/amd64 image and its attestation manifest
        _, digest, reference, tag = parts(image)
        result = self.stream("grey", "iss-api.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker(), [["info", "--format", STORE_FORMAT],
                                         ["image", "inspect", "--format", "{{.Id}}", reference],
                                         ["image", "inspect", "--format", "{{json .Descriptor}}", reference],
                                         ["buildx", "imagetools", "inspect", "--raw", reference],
                                         ["buildx", "imagetools", "inspect", "--format", ENTRIES, reference],
                                         ["image", "inspect", "--format", "{{.Id}}", tag],
                                         ["tag", reference, tag], ["save", tag]])
        self.assertEqual([call["args"] for call in self.calls("shasum")], [["-a", "256"]])
        self.assertIn(f"The pinned image is an index of one linux/amd64 image and its attestation manifests: {reference}\n",
                      result.stdout)
        self.assertEqual(self.server(), [SERVER_VERSION, SERVER_INFO, SERVER_INSPECT + reference, SERVER_LOAD,
                                         SERVER_INSPECT + reference])
        ssh = self.calls("ssh")
        self.assertEqual([call["n"] for call in ssh], [True, True, True, False, True])
        self.assertEqual([call.get("stdin") for call in ssh], [None, None, None, archive(image), None])
        # The server has it now: a second run streams nothing.
        result = self.stream("grey", "iss-api.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"The pinned image is already on grey (containerd image store): {reference}. Nothing to stream.\n"
                      "Next, on grey: sudo bash iss-api.sh install --no-pull --non-interactive\n", result.stdout)
        self.assertNothingStreamed()
        # A stream tag that names the pinned digest already is used as it is: no docker tag.
        self.set_state(server={"images": {}})
        self.assertEqual(self.stream("grey", "iss-api.sh").returncode, 0)
        self.assertNotIn(["tag", reference, tag], self.docker())
        self.assertEqual(self.saves(), [tag])

    def test_image_sources_of_the_rebuild_plan(self):
        # Rows 1-3 of the image table of the rebuild plan: no kit script pins them.
        args = [word for image in REBUILD for word in ("--image", image)]
        self.set_state(mac={"images": {}})
        result = self.stream("grey", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in self.docker() if call[:1] == ["pull"]],
                         [["pull", "--platform", "linux/amd64", image] for image in REBUILD])
        self.assertEqual(self.saves(), [parts(image)[3] for image in REBUILD])
        self.assertNotIn("Next, on grey", result.stdout)
        # --platform applies to every --image.
        self.set_state(mac={"images": {}, "tags": {}}, server={"images": {}})
        self.assertEqual(self.stream("--platform", "linux/arm64", "grey", *args[:2]).returncode, 0)
        self.assertEqual([call for call in self.docker() if call[:1] == ["pull"]],
                         [["pull", "--platform", "linux/arm64", REBUILD[0]]])
        # A pull that brings another ID, and a pull without the registry login of the Mac, reach no server.
        self.set_state(mac={"images": {}, "tags": {}, "pull_ids": {REBUILD[2]: OTHER}}, server={"images": {}})
        result = self.stream("grey", "--image", REBUILD[2])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: After the pull, {pinned(REBUILD[2])} on this computer does not have the pinned digest as "
                      f"its ID (ID: {OTHER}). Nothing was streamed.\n", result.stderr)
        self.assertNothingStreamed()
        self.set_state(mac={"images": {}, "pull_ids": {}, "logged_in": False})
        result = self.stream("grey", "--image", REBUILD[2])
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: The pull failed on this computer (above). When the registry wants a login, run docker "
                      "login perodua-deploy.novutal.com on this computer first.\n", result.stderr)
        self.assertNothingStreamed()

    def test_usage(self):
        good = REBUILD[0]
        for args, message in (([], "Missing SSH_HOST"), (["grey"], "Missing SOURCE"),
                              (["grey", "deploy"], "A SOURCE is deploy-app.sh, iss-api.sh, rp-adapter.sh, oracle-view.sh "
                                                   "or --image PIN: deploy"),
                              (["grey", "scripts/iss-api.sh"], "A SOURCE is"),
                              (["grey", "--line", "demo-uiux", "deploy-app.sh"], "--line belongs to the deploy-app.sh before it, once"),
                              (["grey", "iss-api.sh", "--line", "demo-uiux"], "--line belongs to the deploy-app.sh before it"),
                              (["grey", "deploy-app.sh", "--line", "demo-uiux", "--line", "demo-uiux"], "--line belongs"),
                              (["grey", "deploy-app.sh", "--line", "client-stable"], "--line must be client-stable-uiux or demo-uiux"),
                              (["grey", "deploy-app.sh", "--line"], "Missing value for --line"),
                              (["grey", "--image", good.split("@")[0]], "--image must be a pin of the form"),
                              (["grey", "--image", good.replace(":demo-uiux-grey-v0.60.0-oracle2", "")], "--image must be a pin"),
                              (["grey", "--image", good[:-1]], "--image must be a pin"),
                              (["grey", "--image", good.upper()], "--image must be a pin"),
                              (["grey", "--image", good + ";id"], "--image must be a pin"),
                              (["grey", "iss-api.sh", "--platform", "linux/arm64"], "--platform belongs to --image"),
                              (["grey", "--image", good, "--platform", "arm64"], "--platform must be linux/ARCH"),
                              (["grey", "--image", good, "--platform", "linux/amd64", "--platform", "linux/arm64"],
                               "Give --platform once"),
                              (["-oProxyCommand=id", "iss-api.sh"], "Unknown option: -oProxyCommand=id"),
                              (["grey;id", "iss-api.sh"], "SSH_HOST may hold only letters, digits, ., _, - and @"),
                              ([".grey", "iss-api.sh"], "SSH_HOST may hold only")):
            with self.subTest(args=args):
                result = self.stream(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"ERROR: {message}", result.stderr)
                self.assertEqual(self.docker() + self.server(), [])
        result = self.stream("grey", "iss-api.sh", KIT_NON_INTERACTIVE="yes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("KIT_NON_INTERACTIVE must be 1", result.stderr)
        for value in ("1", "0", ""):
            self.assertEqual(self.stream("--dry-run", "--non-interactive", "grey", "iss-api.sh",
                                         KIT_NON_INTERACTIVE=value).returncode, 0)
        result = self.stream("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("bash scripts/stream-image.sh [--dry-run] [--file] SSH_HOST SOURCE...\n", result.stdout)
        self.assertEqual(self.locks(), [])

    def test_dry_run_changes_nothing(self):
        image = KIT_PINS["rp-adapter.sh"][0]
        _, _, reference, tag = parts(image)
        self.set_state(mac={"images": {}})
        result = self.stream("--dry-run", "grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"Not on this computer: {reference}\n"
                                        "Not on grey. Dry run: nothing was changed. The steps:\n"
                                        f"  docker pull --platform linux/amd64 {image}\n"
                                        f"  docker tag {reference} {tag}\n"
                                        f"  docker save {tag} | ssh grey 'sudo -n docker load'\n"
                                        f"  the check of {reference} on grey\n")
        self.assertEqual(self.server(), [SERVER_VERSION, SERVER_INFO, SERVER_INSPECT + reference])
        result = self.stream("--dry-run", "--file", "grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"  rsync --partial ARCHIVE grey:stream-image/{archive_name(image)}, then sha256sum there\n", result.stdout)
        self.assertIn(f"  ssh grey 'sudo -n docker load -i stream-image/{archive_name(image)}'\n", result.stdout)
        self.assertEqual(self.server(), [SERVER_VERSION, SERVER_INFO, SERVER_INSPECT + reference, SERVER_TOOLS])
        self.assertNothingStreamed()
        self.assertNotIn(["pull", "--platform", "linux/amd64", image], self.docker())
        self.assertFalse(self.archives.exists())
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.locks(), [])

    def test_file_mode_copies_checks_loads_and_removes_the_archives(self):
        image = KIT_PINS["oracle-view.sh"][0]
        _, digest, reference, tag = parts(image)
        result = self.stream("--file", "grey", "oracle-view.sh")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        name = archive_name(image)
        self.assertEqual([args for args in self.docker() if args[:1] == ["save"]],
                         [["save", "-o", f"{self.archives}/{name}.part", tag]])
        self.assertEqual(self.server(), [SERVER_VERSION, SERVER_INFO, SERVER_INSPECT + reference, SERVER_TOOLS, SERVER_MKDIR,
                                         f"sha256sum stream-image/{name}", f"sudo -n docker load -i stream-image/{name}",
                                         SERVER_INSPECT + reference, f"rm -f stream-image/{name}"])
        self.assertEqual(len(self.calls("rsync")), 1)
        self.assertEqual(self.calls("rsync")[0]["args"][3:], [f"{self.archives}/{name}", f"grey:stream-image/{name}"])
        self.assertTrue(all(call["n"] for call in self.calls("ssh")))  # nothing on the standard input
        sum_line = hashlib.sha256(archive(image).encode()).hexdigest()
        self.assertIn(f"Same SHA-256 on grey: {sum_line}\nLoaded image: {tag}\nOn grey: {reference}, ID {digest}\n",
                      result.stdout)
        self.assertEqual(self.get_state()["server"]["images"], {reference: facts(image)})
        self.assertEqual(list(self.archives.iterdir()), [])
        self.assertEqual(oct(self.archives.stat().st_mode & 0o777), "0o700")
        self.assertEqual(list((self.home / "stream-image").iterdir()), [])
        self.assertEqual(oct((self.home / "stream-image").stat().st_mode & 0o777), "0o700")

    def test_file_mode_resumes_after_a_break(self):
        image = KIT_PINS["client-stable-uiux"][0]
        name = archive_name(image)
        self.set_state(server={"rsync_breaks": 1})
        result = self.stream("--file", "grey", "--image", image)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: The copy to grey broke (above). Run this script again: it resumes from the archive kept "
                      f"here ({self.archives}/{name}) and from the part on the server.\n", result.stderr)
        self.assertEqual(sorted(path.name for path in self.archives.iterdir()), [name, name + ".sha256"])
        part = (self.home / "stream-image" / name).stat().st_size
        self.assertEqual(part, len(archive(image)) // 2)
        self.assertEqual(self.get_state()["server"]["images"], {})
        # The next run saves nothing again and copies over the part.
        result = self.stream("--file", "grey", "--image", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Resuming from the kept archive {self.archives}/{name}\n", result.stdout)
        self.assertEqual(self.saves(), [])
        self.assertEqual([call["part_before"] for call in self.calls("rsync")], [part])
        self.assertEqual(self.get_state()["server"]["images"], {pinned(image): facts(image)})
        self.assertEqual(list(self.archives.iterdir()), [])
        self.assertEqual(list((self.home / "stream-image").iterdir()), [])
        # An archive whose SHA-256 is not the recorded one is saved again.
        self.set_state(server={"images": {}, "rsync_breaks": 1})
        self.assertEqual(self.stream("--file", "grey", "--image", image).returncode, 1)
        (self.archives / name).write_text("damaged")
        self.set_state(server={"rsync_breaks": 0})
        result = self.stream("--file", "grey", "--image", image)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Resuming", result.stdout)
        self.assertEqual(len(self.saves()), 1)

    def test_file_mode_refuses_another_checksum_and_a_server_without_the_tools(self):
        image = KIT_PINS["iss-api.sh"][0]
        name = archive_name(image)
        self.set_state(server={"rsync_corrupt": True})
        result = self.stream("--file", "grey", "iss-api.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("has another SHA-256", result.stderr)
        self.assertIn("it was removed there. Run this script again: it copies the archive kept here again.\n", result.stderr)
        self.assertFalse((self.home / "stream-image" / name).exists())
        self.assertTrue((self.archives / name).exists())
        self.assertNotIn(f"sudo -n docker load -i stream-image/{name}", self.server())
        self.set_state(server={"rsync_corrupt": False})
        self.assertEqual(self.stream("--file", "grey", "iss-api.sh").returncode, 0)
        self.assertEqual(self.saves(), [])
        self.set_state(server={"images": {}, "tools": False})
        result = self.stream("--file", "grey", "iss-api.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: --file needs rsync and sha256sum on grey, and it has not both. Install them there, or stream "
                      "without --file. Nothing was changed.\n", result.stderr)
        self.assertNothingStreamed()

    def test_the_server_store_and_a_stream_tag_of_another_image(self):
        image = KIT_PINS["iss-api.sh"][0]
        _, _, reference, tag = parts(image)
        self.set_state(server={"store": "classic"})
        result = self.stream("grey", "iss-api.sh", "rp-adapter.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR: The Docker engine of grey uses the classic image store: there an image from docker load has "
                      f"no registry digest, and the --no-pull runs do not use it ({reference} is not there). Switch that "
                      "engine to the containerd image store first (README.md, \"Install from streamed images\"). Nothing "
                      "was changed.\n", result.stderr)
        self.assertNothingStreamed()
        # A pin that a pull brought on the classic store: the --no-pull runs use it, so nothing is streamed.
        self.set_state(server={"images": {reference: f"{CONFIG_ID} {reference}"}})
        result = self.stream("grey", "iss-api.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"The pinned image is already on grey (classic image store): {reference}. Nothing to stream.\n",
                      result.stdout)
        # A stream tag that names another image here stops before the server, also in a dry run.
        self.set_state(mac={"tags": {tag: OTHER}}, server={"store": "containerd", "images": {}})
        for args in (["grey", "iss-api.sh"], ["--dry-run", "grey", "iss-api.sh"]):
            with self.subTest(args=args):
                result = self.stream(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"ERROR: On this computer the stream tag {tag} names another image (ID {OTHER}), not the "
                              f"pinned digest. Remove that tag (docker image rm {tag}), then run this script again. "
                              "Nothing was changed.\n", result.stderr)
                self.assertEqual(self.server(), [])
        # A classic store on the Mac stops before ssh.
        self.set_state(mac={"store": "classic", "tags": {}})
        result = self.stream("grey", "iss-api.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Docker on this computer does not use the containerd image store", result.stderr)
        self.assertEqual(self.server(), [])

    def test_an_index_of_one_platform_and_its_attestations(self):
        # docker save without --platform writes the index itself, so the server finds the pinned digest (lab of
        # 2026-11-10). The helper reads the entries from the registry and accepts one image of the platform and
        # attestation manifests only: the pull for that platform brings no other image, and docker save needs all.
        image = KIT_PINS["rp-adapter.sh"][0]
        _, digest, reference, tag = parts(image)
        index = {"digest": digest, "entries": [AMD64, ATTESTATION]}
        result = self.stream("grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([args for args in self.docker() if args[:1] == ["save"]], [["save", tag]])  # no --platform
        self.assertEqual(self.get_state()["server"]["images"], {reference: f"{digest} {reference}"})
        self.assertIn(f"On grey: {reference}, ID {digest}\n", result.stdout)
        refused = (f"This script streams an index only when its one image is linux/amd64 and each other entry is an "
                   "attestation manifest (unknown/unknown): docker save writes the whole index, and the pull of this "
                   "computer brings that image and its attestations only. Nothing was streamed.\n")
        other_type = f"sha256:{'a' * 64} {INDEX} linux/amd64 -"
        for entries, held in (([AMD64, ARM64, ATTESTATION, ATTESTATION.replace("d" * 64, "f" * 64)],
                               "linux/amd64, linux/arm64, unknown/unknown attestation-manifest, unknown/unknown "
                               "attestation-manifest"),
                              ([ARM64, ATTESTATION], "linux/arm64, unknown/unknown attestation-manifest"),
                              ([AMD64, AMD64.replace("linux/amd64", "linux/amd64/v3")], "linux/amd64, linux/amd64/v3"),
                              ([AMD64, ATTESTATION.replace("attestation-manifest", "-")], "linux/amd64, unknown/unknown"),
                              ([other_type, ATTESTATION], "linux/amd64, unknown/unknown attestation-manifest"),
                              ([], "")):
            with self.subTest(entries=entries):
                self.set_state(mac={"registry": {reference: dict(index, entries=entries)}, "tags": {}},
                               server={"images": {}})
                for args in (["grey", "rp-adapter.sh"], ["--dry-run", "grey", "rp-adapter.sh"]):
                    result = self.stream(*args)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"ERROR: The pinned index {reference} holds: {held}. {refused}", result.stderr)
                    self.assertEqual(self.server(), [])  # checked before the server
                    self.assertNothingStreamed()
        # The bytes of the index in the registry must have the pinned digest.
        self.set_state(mac={"registry": {reference: dict(index, digest=OTHER)}})
        result = self.stream("grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: The registry gives another index for {reference} (its digest is {OTHER}). Nothing was "
                      "streamed.\n", result.stderr)
        self.assertNotIn(["buildx", "imagetools", "inspect", "--format", ENTRIES, reference], self.docker())
        self.assertEqual(self.server(), [])
        # The entries come from the registry, with the login of this computer, also in a dry run.
        self.set_state(mac={"registry": {reference: index}, "logged_in": False})
        for args in (["grey", "rp-adapter.sh"], ["--dry-run", "grey", "rp-adapter.sh"]):
            with self.subTest(args=args):
                result = self.stream(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("ERROR: failed to authorize: failed to fetch oauth token: 401 Unauthorized\nERROR: docker "
                              f"buildx imagetools inspect --raw {reference} failed on this computer (above): the entries "
                              "of an index come from the registry. When the registry wants a login, run docker login "
                              "perodua-deploy.novutal.com on this computer first. Nothing was streamed.\n", result.stderr)
                self.assertEqual(self.server(), [])
        # Another media type is refused.
        self.set_state(mac={"logged_in": True,
                            "images": {reference: {"id": digest, "kind": "application/vnd.docker.container.image.v1+json"}}})
        result = self.stream("grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: The pinned image {reference} on this computer is application/vnd.docker.container.image.v1"
                      "+json: this script streams one image manifest, or an index of one linux/amd64 image and its "
                      "attestation manifests. Nothing was streamed.\n", result.stderr)
        self.assertNotIn(["buildx", "imagetools", "inspect", "--raw", reference], self.docker())
        # An index that the Mac pulls now is checked after the pull, before the tag; any variant of the platform counts.
        self.set_state(mac={"images": {}, "tags": {}})
        result = self.stream("grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[:2] for args in self.docker() if args[0] in ("pull", "buildx", "tag", "save")],
                         [["pull", "--platform"], ["buildx", "imagetools"], ["buildx", "imagetools"], ["tag", reference],
                          ["save", tag]])
        arm = REBUILD[2]
        self.set_state(mac={"images": {}, "tags": {}, "registry": {pinned(arm): {
            "digest": parts(arm)[1], "entries": [ARM64.replace("linux/arm64", "linux/arm64/v8"), ATTESTATION]}}},
            server={"images": {}})
        result = self.stream("--platform", "linux/arm64", "grey", "--image", arm)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"The pinned image is an index of one linux/arm64 image and its attestation manifests: {pinned(arm)}\n",
                      result.stdout)
        self.assertEqual(self.saves(), [parts(arm)[3]])

    def test_errors_on_the_server_stop_it(self):
        image = KIT_PINS["rp-adapter.sh"][0]
        _, _, reference, tag = parts(image)
        for server, message in (({"sudo": False}, "sudo: a password is required"),
                                ({"sudo": True, "reachable": False}, "ssh: connect to host grey port 22: Connection refused")):
            with self.subTest(server=server):
                self.set_state(server=server)
                result = self.stream("grey", "rp-adapter.sh")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"ERROR: ssh grey (batch mode) with sudo -n docker failed: {message}. ssh must log in with a "
                              "key and no question, and the user must run docker with sudo -n.\n", result.stderr)
        self.set_state(server={"reachable": True})
        unpacking = f"Error unpacking image {tag}: failed to extract layer sha256:0123: unexpected EOF\nLoaded image: {tag}\n"
        self.set_state(server={"load_output": unpacking})
        result = self.stream("grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: docker load on grey could not unpack the image (above). Remove it there (sudo docker image "
                      f"rm {tag}), then run this script again.\n", result.stderr)
        self.assertNotIn("Next, on grey", result.stdout)
        self.set_state(server={"load_output": "", "images": {}, "dangling": True})
        result = self.stream("grey", "rp-adapter.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"ERROR: After docker load, grey does not find {reference} with the pinned digest\n", result.stderr)

    def test_one_run_at_a_time_for_each_server(self):
        lock = self.tmp / "stream-image-grey_22.lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{os.getpid()}\n")  # a running process holds it
        for host in ("grey", "grey-alias", "admin@grey", "GREY"):  # one server, however ssh names it
            with self.subTest(host=host):
                result = self.stream(host, "iss-api.sh")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"ERROR: Another stream-image.sh run (process {os.getpid()}) streams to the server grey:22. "
                              f"If there is none, remove {lock}\n", result.stderr)
                self.assertEqual(self.docker() + self.server(), [])
        self.assertEqual(self.stream("--dry-run", "deploy@grey2", "iss-api.sh").returncode, 0)
        self.assertEqual(self.locks(), [lock.name])
        ended = subprocess.Popen(["true"])
        ended.wait()
        (lock / "pid").write_text(f"{ended.pid}\n")  # the lock of a run that stopped is taken over
        self.assertEqual(self.stream("grey", "iss-api.sh").returncode, 0)
        self.assertEqual(self.locks(), [])

    def test_the_helper_never_asks_and_reads_the_pins_from_the_scripts(self):
        code = re.sub(r"cat <<'HELP'\n.*?\nHELP\n", "", STREAM_TEXT, flags=re.S)
        code = "\n".join(re.sub(r"\s{2,}# .*$", "", line) for line in code.splitlines()
                         if not line.lstrip().startswith("#"))  # without comments
        self.assertIsNone(re.search(r"\bread\b", code))
        ssh = [line.strip() for line in code.splitlines()
               if re.search(r"\bssh\b", line) and not re.search(r"\b(printf|die|usage_error)\b", line)]
        self.assertEqual(ssh, ["SSH=(ssh -T -a -x -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes "
                               "-o PermitLocalCommand=no", 'config=$(ssh -G -o BatchMode=yes "$HOST" 2> /dev/null) || config=\'\''])
        self.assertIn('remote() { "${SSH[@]}" -n "$HOST" "$1"; }', code)
        self.assertIn('remote_load() { "${SSH[@]}" "$HOST" \'sudo -n docker load\'; }', code)
        self.assertEqual(code.count('"${SSH[@]}"'), 2)
        self.assertIn('rsync --partial -e "${SSH[*]}" "$archive" "$HOST:$REMOTE_DIR/$name"', code)
        allowed = ("sudo -n docker ", "mkdir -p -m 0700 $REMOTE_DIR && chmod 0700 $REMOTE_DIR", "sha256sum $REMOTE_DIR/",
                   "rm -f $REMOTE_DIR/", "command -v rsync && command -v sha256sum")
        remote = re.findall(r"\bremote ([\"'])(.*?)\1", code)
        self.assertEqual(len(remote), 9)
        for _, command in remote:
            self.assertTrue(command.startswith(allowed), command)
        self.assertEqual(re.findall(r'docker save (?:-o "\$archive\.part" )?"\$(\w+)"', code), ["STREAM_TAG", "STREAM_TAG"])
        self.assertEqual(len(re.findall(r"(^|[;&|(]|\$\()\s*docker save\b", code, re.M)), 2)
        self.assertNotRegex(code, r"docker save[^\n]*--platform")  # it would save the manifest of the platform, not the pin
        # An index: its bytes from the registry, checked against the pinned digest, and its entries.
        self.assertIn('sum=$(docker buildx imagetools inspect --raw "$PINNED" | shasum -a 256)', code)
        self.assertIn('[[ sha256:${sum%% *} == "$DIGEST" ]]', code)
        self.assertIn('entries=$(docker buildx imagetools inspect --format "$ENTRIES" "$PINNED")', code)
        self.assertIn(f"\nENTRIES='{ENTRIES}'\n", STREAM_TEXT)
        # The pins stay in the kit scripts.
        self.assertNotRegex(STREAM_TEXT, r"sha256:[0-9a-f]{64}|grey-v\d|v\d+\.\d+\.\d+")
        self.assertIn('values=$(sed -n "s/^$2=\\\\([^ ]*\\\\).*/\\\\1/p" "$HERE/$1")', STREAM_TEXT)


if __name__ == "__main__":
    unittest.main()
