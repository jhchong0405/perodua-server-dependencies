"""No kit script waits for a person in a non-interactive run (owner rule 10d).

Agents run the kit over ssh, sometimes with ssh -t: stdin is then a
pseudo-terminal, and a script cannot tell the agent from a person. The flag
--non-interactive or KIT_NON_INTERACTIVE=1 in the environment says it: with
either, no script reads the terminal, and a value that no option or setting
gives stops the run before any change, with a message that names the option.

Static checks (any host): no script uses /dev/tty; every read of a person's
answer is in one helper per script, read_terminal, which refuses in a
non-interactive run and when stdin is not a terminal; every other read takes
its input from a file, a string or a pipe; every script honours the flag and
the variable; and no external command can ask (curl --user, openssl without
-passin, docker login without --password-stdin, apt-get without -y and
--force-confold, sudo without -n, ssh without BatchMode, a terminal for a
container, input() or getpass in Python, Read-Host in PowerShell).

Pseudo-terminal checks: each script runs on a new pseudo-terminal (stdin,
stdout, stderr and the controlling terminal, as ssh -t gives) and nothing is
typed. With the variable or the flag it must end within a few seconds without
a question: each question then takes its option, or the run stops naming it,
and nothing is changed. Without them, a person on the terminal is still asked
(one sample per script). They use the fakes of the kit's other tests and the
same platform gates: root on Linux for service.sh, iss-api.sh, rp-adapter.sh
and https.sh, root on Ubuntu 24.04 for uninstall.sh. The tests remove
KIT_NON_INTERACTIVE from their own environment first.

oracle-view.sh and oracle-view-stream.sh are not checked here yet: another
branch changes them (README.md, "Run the kit without a person").
"""

import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

import test_deploy_app as app_tests
import test_deploy_app_uat as uat_tests
import test_deploy_db_guided as guided_tests
import test_https as https_tests
import test_iss_api as iss_tests
import test_rp_adapter as rp_tests
import test_service_app as service_tests
import test_uninstall_app as uninstall_tests


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
# Changed by another branch at the same time; checked after it merges.
NOT_YET = {"oracle-view.sh", "oracle-view-stream.sh"}
KIT = sorted(path for path in SCRIPTS.glob("*.sh") if path.name not in NOT_YET)
LINUX = Path("/proc/self/fd").is_dir()
ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
# What a question to a person looks like in the output of any kit script.
QUESTIONS = ("[y/N]", "[Y/n]", "to confirm: ", "(hidden", "Choose [", "? [1]: ", "Enter pass phrase",
             "Pass phrase of", "password for user", "(12 or more characters): ", "Registry username")
NO_WAIT = 20  # seconds: a run that waits for an answer is still running then
CTRL_C = b"\x03"

HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
COMMAND_START = r"(?:^|[;&|({!]|\$\(|\bthen\b|\bdo\b|\belse\b|\bwhile\b|\buntil\b|\bif\b)\s*(?:[A-Za-z_]\w*=\S*\s+)*"
READ = re.compile(COMMAND_START + r"(read|mapfile|readarray)\s+-")


def without_variable(env):
    env = dict(env)
    env.pop("KIT_NON_INTERACTIVE", None)
    return env


def non_interactive(env, value="1"):
    return dict(without_variable(env), KIT_NON_INTERACTIVE=value)


def on_terminal(argv, env, answers=(), timeout=NO_WAIT):
    """argv on a new pseudo-terminal, as ssh -t gives it: stdin, stdout, stderr
    and the controlling terminal. Nothing is typed but the answers: (prompt,
    answer) pairs, each typed once its prompt is shown. Returns the exit code,
    or None when the run still waited after timeout seconds, and the output."""
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], [str(arg) for arg in argv], env)
        finally:
            os._exit(127)
    output, cursor, pending, status = b"", 0, list(answers), None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.05)[0]:
                try:
                    output += os.read(terminal, 65536)
                except OSError:  # the terminal closed with the script
                    pass
            if pending and pending[0][0] in output[cursor:]:
                prompt, answer = pending.pop(0)
                cursor = output.index(prompt, cursor) + len(prompt)
                os.write(terminal, answer if answer == CTRL_C else answer + b"\n")
            done, result = os.waitpid(pid, os.WNOHANG)
            if done:
                status = result
                while select.select([terminal], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(terminal, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                break
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)
    text = output.decode(errors="replace").replace("\r\n", "\n")
    return (None if status is None else os.waitstatus_to_exitcode(status)), text


class NoWait:
    """Assertions for a run on a terminal that nobody answers."""

    def assert_ended_without_a_question(self, code, output, *questions):
        self.assertIsNotNone(code, "waited for an answer on the terminal:\n" + output)
        for question in QUESTIONS + questions:
            self.assertNotIn(question, output)

    def assert_refused(self, code, output, *named):
        """Ended without a question and with an error that names each of named."""
        self.assert_ended_without_a_question(code, output)
        self.assertNotEqual(code, 0, output)
        for text in named:
            self.assertIn(text, output)


# ───────────────────────────────────────────────────────────── static checks
def code_text(raw):
    """The shell code of a script, line for line: heredoc bodies and comments
    blanked, the text in quotes replaced by Q (its line breaks kept). The code
    of a $( ) inside double quotes stays code."""
    lines, term = [], None
    for line in raw.split("\n"):
        if term is not None:
            lines.append("")
            if line.strip() == term:
                term = None
            continue
        match = HEREDOC.search(line)
        if match:
            term = match.group(2)
        lines.append(line)
    text, out, i = "\n".join(lines), [], 0
    modes, depths = ["code"], []  # code, "double" quotes, or "sub": the code of a $( ) in double quotes
    while i < len(text):
        c, mode = text[i], modes[-1]
        if mode == "double":
            if c == "\\":
                out.append("\n" * text.count("\n", i, i + 2))
                i += 2
            elif c == '"':
                modes.pop()
                i += 1
            elif text.startswith("$(", i):
                modes.append("sub")
                depths.append(0)
                out.append("$(")
                i += 2
            else:
                out.append("\n" if c == "\n" else "")
                i += 1
        elif c == "\\":
            out.append(text[i:i + 2])
            i += 2
        elif c == "'" or text.startswith("$'", i):
            end = i + (2 if c == "$" else 1)
            while end < len(text) and text[end] != "'":
                end += 2 if c == "$" and text[end] == "\\" else 1
            out.append("Q" + "\n" * text.count("\n", i, end))
            i = end + 1
        elif c == '"':
            modes.append("double")
            out.append("Q")
            i += 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        else:
            if mode == "sub" and c == "(":
                depths[-1] += 1
            elif mode == "sub" and c == ")":
                if depths[-1] == 0:
                    modes.pop()
                    depths.pop()
                else:
                    depths[-1] -= 1
            out.append(c)
            i += 1
    return "".join(out)


def logical_lines(code):
    """(first line number, line) with the backslash-newline continuations joined."""
    joined, buffer, start = [], "", None
    for number, line in enumerate(code.split("\n"), 1):
        start = start or number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        joined.append((start, buffer + line))
        buffer, start = "", None
    return joined


def function_lines(code, name):
    """The line numbers of the body of the shell function name, or an empty range."""
    lines = code.split("\n")
    for number, line in enumerate(lines, 1):
        if re.match(rf"^{name}\(\)\s*\{{", line):
            end = next(n for n in range(number + 1, len(lines) + 1) if lines[n - 1] == "}")
            return range(number, end + 1)
    return range(0)


def matching_done(lines, number):
    """The done line of the while or until loop that starts at line number."""
    indent = len(lines[number - 1]) - len(lines[number - 1].lstrip())
    for n in range(number + 1, len(lines) + 1):
        line = lines[n - 1]
        if line.strip().startswith("done") and len(line) - len(line.lstrip()) == indent:
            return line
    return ""


def terminal_reads(path):
    """The reads that may take a person's answer, outside read_terminal: a read
    with -p or -s, and every read whose input is not a file, a string or a pipe."""
    code = code_text(path.read_text())
    lines, helper, found = code.split("\n"), function_lines(code, "read_terminal"), []
    for number, line in enumerate(lines, 1):
        if number in helper or not READ.search(line):
            continue
        command = line[READ.search(line).start():]
        if re.search(r"\bread\b[^;|&<]*\s-[A-Za-z]*[ps]\b", command):
            found.append((number, line.strip()))
        elif re.search(r"<<<|<\s*<\(|<\s*(Q|\$|/|\w)", command) or re.search(r"\|\s*while\b", line):
            continue
        elif re.search(r"\b(while|until)\b", line) and re.search(r"^\s*done\s*<", matching_done(lines, number)):
            continue
        else:
            found.append((number, line.strip()))
    return found


class StaticChecks(unittest.TestCase):
    def test_the_scripts_are_found(self):
        self.assertGreaterEqual({path.name for path in KIT}, {
            "deploy-app.sh", "deploy-db.sh", "https.sh", "install-dependencies.sh", "iss-api.sh",
            "rp-adapter.sh", "service.sh", "uninstall.sh"})

    def test_no_script_uses_dev_tty(self):
        # Not as a test ([[ -r /dev/tty ]] tests only the file mode), not to
        # read (an agent under ssh -t that gives stdin from /dev/null would
        # still be asked there), not to write (without a terminal it fails).
        for path in KIT:
            with self.subTest(script=path.name):
                code = code_text(path.read_text())
                self.assertEqual([n for n, line in enumerate(code.split("\n"), 1) if "/dev/tty" in line], [])

    def test_every_answer_of_a_person_is_read_in_read_terminal_only(self):
        for path in KIT:
            with self.subTest(script=path.name):
                self.assertEqual(terminal_reads(path), [])

    def test_read_terminal_refuses_without_a_terminal_and_in_a_non_interactive_run(self):
        helpers = {}
        for path in KIT:
            code = code_text(path.read_text())
            calls = [line for line in code.split("\n") if re.search(r"\bread_terminal\b", line)]
            body = function_lines(code, "read_terminal")
            if not body:
                self.assertEqual(calls, [], f"{path.name} calls read_terminal without defining it")
                continue
            text = "\n".join(code.split("\n")[body.start - 1:body.stop - 1])
            with self.subTest(script=path.name):
                self.assertRegex(text, r"\(\(NON_INTERACTIVE == 0\)\) && \[\[ -t 0 \]\] \|\| (die|fail) Q")
                self.assertIn("read -r", text)
                self.assertNotIn("/dev/", text)
            helpers[path.name] = re.sub(r"\b(die|fail)\b", "STOP", text)
        self.assertGreaterEqual(len(helpers), 7)
        self.assertEqual(len(set(helpers.values())), 1, "one helper, the same in every script")

    def test_every_script_honours_the_flag_and_the_variable(self):
        for path in KIT:
            raw = path.read_text()
            with self.subTest(script=path.name):
                self.assertRegex(raw, r"--non-interactive\)")  # an option of its own parser
                self.assertIn("case ${KIT_NON_INTERACTIVE:-} in", raw)
                self.assertRegex(raw, r"''\|0(\|1)?\)")
                self.assertIn("KIT_NON_INTERACTIVE must be 1", raw)
                if "NON_INTERACTIVE=1 ;;" in raw:
                    # A script that starts another kit script passes the mode on.
                    self.assertIn("((NON_INTERACTIVE == 0)) || export KIT_NON_INTERACTIVE=1", raw)

    def test_no_external_command_can_ask(self):
        for path in KIT:
            raw, code = path.read_text(), code_text(path.read_text())
            raw_lines = raw.split("\n")
            with self.subTest(script=path.name):
                for number, line in logical_lines(code):
                    where = f"{path.name}:{number}: {line.strip()}"
                    # curl would ask for the password of --user on /dev/tty.
                    if re.search(r"\bcurl(_args)?\b", line):
                        self.assertNotRegex(line, r"(?<![\w-])(--user|-u)(?![\w-])", where)
                    # openssl asks for the pass phrase of an encrypted key on /dev/tty.
                    if re.search(r"\bopenssl\s+(pkey|rsa|ec|pkcs8|pkcs12)\b", line):
                        self.assertIn("-passin", line, where)
                    if re.search(r"\bdocker\s+login\b", line):
                        self.assertIn("--password-stdin", line, where)
                    if re.search(COMMAND_START + r"sudo\b", line):
                        self.assertRegex(line, r"\bsudo\s+-n\b", where)
                    if re.search(COMMAND_START + r"ssh\b", line):
                        self.assertIn("BatchMode=yes", line, where)
                    # A container with a terminal could ask through it.
                    self.assertNotRegex(line, r"(?<![\w-])(-it|-ti|--tty|--interactive)(?![\w-])", where)
                    if re.search(r"\bdocker\s+(run|exec)\b", line):
                        self.assertNotRegex(line, r"(?<![\w-])-[it](?![\w-])", where)
                    if re.search(r"(?:^|[\s;&|(])(compose|current|staged)\b.*\b(run|exec)\b", line):
                        self.assertRegex(line, r"(?<![\w-])-T(?![\w-])", where)
                    match = re.search(COMMAND_START + r"apt-get\s+(install|upgrade|full-upgrade|dist-upgrade|purge|remove)\b", line)
                    if match:
                        self.assertRegex(line, r"(?<![\w-])-y(?![\w-])", where)
                        self.assertIn("DEBIAN_FRONTEND=noninteractive", raw, where)
                        if match.group(1) != "purge" and match.group(1) != "remove":
                            source = "\n".join(raw_lines[number - 1:number + 2])
                            self.assertRegex(source, r"--force-confold|\$\{keep\[@\]\}", where)
                            self.assertIn("--force-confdef -o Dpkg::Options::=--force-confold", raw, where)
                # Python run by the script never asks either.
                self.assertNotRegex(raw, r"\binput\s*\(|\bgetpass\b")

    def test_the_other_kit_files_never_ask(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            with self.subTest(script=path.name):
                self.assertNotRegex(path.read_text(), r"\binput\s*\(|\bgetpass\b")
        for path in sorted(SCRIPTS.glob("*.ps1")):
            with self.subTest(script=path.name):
                self.assertNotRegex(path.read_text(), r"(?i)Read-Host|Get-Credential|ReadKey|\$Host\.UI")

    @unittest.skipUnless(LINUX, "needs Linux (bash 4 or later)")
    def test_the_help_names_the_flag_and_the_variable(self):
        for path in KIT:
            with self.subTest(script=path.name):
                result = subprocess.run(["bash", str(path), "--help"], capture_output=True, text=True, timeout=30,
                                        stdin=subprocess.DEVNULL, env=without_variable(os.environ))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--non-interactive", result.stdout)
                self.assertIn("KIT_NON_INTERACTIVE=1", result.stdout)


# ───────────────────────────── the variable and the options, checked at once
@unittest.skipUnless(LINUX, "needs Linux (GNU tools and bash 4 or later)")
class CheckedBeforeAnyChange(NoWait, unittest.TestCase):
    """Refusals that come before the root check: they need no fakes."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="perodua-nowait-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def run_on_terminal(self, script, *args, value="1"):
        return on_terminal(["bash", SCRIPTS / script, *args], non_interactive(os.environ, value))

    def test_an_unknown_value_of_the_variable_stops_every_script_at_once(self):
        for script, args in (("deploy-db.sh", ["--check-config"]), ("service.sh", ["--role", "app", "status"]),
                             ("uninstall.sh", ["--role", "app"]), ("deploy-app.sh", ["--dir", self.tmp / "app"]),
                             ("iss-api.sh", ["status"]), ("rp-adapter.sh", ["status"]), ("https.sh", ["status"]),
                             ("install-dependencies.sh", ["--role", "app"])):
            for value in ("yes", "true", "2"):
                with self.subTest(script=script, value=value):
                    code, output = self.run_on_terminal(script, *args, value=value)
                    self.assert_refused(code, output, "KIT_NON_INTERACTIVE must be 1")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_uninstall_options_are_checked_before_anything_else(self):
        for args, message in (
                (["--role", "db", "--purge", "--confirm", "perodua"], "--purge needs --confirm-purge PURGE"),
                (["--role", "db", "--purge", "--confirm-purge", "purge"], "--confirm-purge must be exactly PURGE"),
                (["--role", "db", "--confirm-purge", "PURGE"], "--confirm-purge belongs to --role db --purge"),
                (["--role", "app", "--purge", "--confirm-purge", "PURGE"], "--confirm-purge belongs to --role db --purge"),
                (["--role", "db", "--delete-old-containers", "perodua-odoo"], "belong to --role app"),
                (["--role", "app", "--delete-old-volumes"], "give that too"),
                (["--role", "app", "--delete-old-containers", "Not A Project"], "needs a Compose project name")):
            with self.subTest(args=args):
                code, output = self.run_on_terminal("uninstall.sh", *args)
                self.assert_refused(code, output, message, "Nothing was changed")

    def test_iss_api_check_key_is_refused_at_once(self):
        key = self.tmp / "key"
        key.write_text("k3y\n")
        for args, message in ((["check", "--key"], "pass --key-file FILE"),
                              (["check", "--key", "--key-file", key], "not both"),
                              (["status", "--key-file", key], "--key and --key-file belong to check"),
                              (["check", "--key-file", self.tmp / "missing"], "--key-file must be a readable regular file")):
            with self.subTest(args=args):
                code, output = self.run_on_terminal("iss-api.sh", *args, "--dir", self.tmp / "iss")
                self.assert_refused(code, output, message)
        self.assertFalse((self.tmp / "iss").exists())

    def test_https_key_pass_file_needs_the_key(self):
        secret = self.tmp / "pass"
        secret.write_text("secret\n")
        code, output = self.run_on_terminal("https.sh", "csr", "--key-pass-file", secret, "--dir", self.tmp / "https")
        self.assert_refused(code, output, "--key-pass-file is the pass phrase of --key FILE")
        self.assertFalse((self.tmp / "https").exists())

    def test_deploy_app_delete_old_attachments_applies_only_to_init_db(self):
        code, output = self.run_on_terminal("deploy-app.sh", "--dir", self.tmp / "app",
                                            "--delete-old-attachments", "perodua-client-uiux_filestore")
        self.assert_refused(code, output, "--delete-old-attachments applies only to --init-db")
        self.assertFalse((self.tmp / "app").exists())


# ───────────────────────────────────────────────────────────────── deploy-db.sh
@unittest.skipUnless(LINUX and guided_tests.LOCAL, "needs Linux and a non-loopback IPv4 address on this host")
class DeployDbNoWait(NoWait, unittest.TestCase):
    """The guided setup and the passwords of deploy-db.sh, with the fakes of
    test_deploy_db_guided.py: pg_conftool and Docker are stubs."""

    setUp = guided_tests.GuidedSetupTests.setUp
    settings = guided_tests.GuidedSetupTests.settings

    def deploy_db(self, *args, env=None):
        return on_terminal(["bash", self.script, *args], non_interactive(self.env) if env is None else env)

    def write_config(self, **values):
        settings = {"DB_MODE": "empty", "DB_NAME": "perodua", "DB_USER": "odoo", "DB_LISTEN_IP": "127.0.0.1"}
        settings.update(values)
        self.config.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        self.config.chmod(0o600)

    def test_without_deploy_conf_a_non_interactive_run_names_the_options(self):
        for env, extra in ((non_interactive(self.env), []), (without_variable(self.env), ["--non-interactive"])):
            with self.subTest(flag=extra):
                code, output = self.deploy_db("--check-config", *extra, env=env)
                self.assert_refused(code, output, "--app-ip ADDRESS --listen-ip ADDRESS", "--app-here",
                                    "deploy.conf.example")
                self.assertNotIn("set up?", output)
                self.assertFalse(self.config.exists())

    def test_the_options_answer_the_guided_setup(self):
        # With the variable, and for a person on a terminal who gives them: nothing is asked.
        for env in (non_interactive(self.env), without_variable(self.env)):
            with self.subTest(variable="KIT_NON_INTERACTIVE" in env):
                self.config.unlink(missing_ok=True)
                code, output = self.deploy_db("--app-ip", "10.0.0.10", "--listen-ip", guided_tests.LOCAL,
                                              "--check-config", env=env)
                self.assert_ended_without_a_question(code, output, "set up?", "Where does the App run?")
                self.assertEqual(code, 0, output)
                self.assertIn("Configuration valid", output)
                self.assertEqual(self.settings(), [
                    "DB_MODE=empty", "DB_NAME=perodua", "DB_USER=odoo", "PG_PORT=5433",
                    f"DB_LISTEN_IP={guided_tests.LOCAL}", "APP_CIDR=10.0.0.10/32"])
                self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_app_here_listens_on_docker_and_accepts_its_networks(self):
        self.env["FAKE_GATEWAY"] = guided_tests.LOCAL  # stands in for Docker's bridge address
        code, output = self.deploy_db("--app-here", "--check-config")
        self.assert_ended_without_a_question(code, output)
        self.assertEqual(code, 0, output)
        self.assertIn("APP_CIDR=172.16.0.0/12,192.168.0.0/16", self.settings())
        self.assertIn(f"DB_LISTEN_IP={guided_tests.LOCAL}", self.settings())

    def test_an_answer_that_cannot_be_used_stops_without_saving(self):
        local = guided_tests.LOCAL
        for args, message in (
                (["--app-ip", "10.0.0.10"], "--app-ip needs --listen-ip"),
                (["--app-ip", "10.0.0.10", "--listen-ip", "192.0.2.10"], "192.0.2.10 is not an address of this server"),
                (["--app-ip", local, "--listen-ip", local], "pass --app-here instead"),
                (["--app-ip", "127.0.0.1", "--listen-ip", local], "pass --app-here instead"),
                (["--app-ip", "not-an-address", "--listen-ip", local], "--app-ip must be the IPv4 address"),
                (["--app-here"], "Docker is not running on this server yet"),
                (["--app-here", "--listen-ip", local], "belong to --app-ip"),
                (["--app-here", "--app-ip", "10.0.0.10"], "not both")):
            with self.subTest(args=args):
                code, output = self.deploy_db(*args, "--check-config")
                self.assert_refused(code, output, message)
                self.assertFalse(self.config.exists())

    def test_the_options_never_replace_a_deploy_conf(self):
        self.write_config()
        before = self.config.read_text()
        code, output = self.deploy_db("--app-ip", "10.0.0.10", "--listen-ip", guided_tests.LOCAL, "--check-config")
        self.assert_refused(code, output, "Edit it instead. Nothing was changed.")
        self.assertEqual(self.config.read_text(), before)

    def test_the_database_password_comes_only_from_password_file(self):
        self.write_config()
        code, output = self.deploy_db("--config", self.config)
        self.assert_refused(code, output, "pass --password-file FILE", "Nothing was changed")
        self.assertNotIn("Database password for", output)

    def test_an_authenticated_download_needs_cloud_password_file(self):
        self.write_config(DB_MODE="restore", BACKUP_URL="https://backup.example.invalid/database.dump",
                          BACKUP_SHA256="0" * 64, DOWNLOAD_USER="cloud", DB_LISTEN_IP="127.0.0.1")
        password = self.config.with_name("db-password")
        password.write_text("Fixture password 2026\n")
        password.chmod(0o600)
        code, output = self.deploy_db("--config", self.config, "--password-file", password)
        self.assert_refused(code, output, "pass --cloud-password-file FILE", "Nothing was changed")
        # Without DOWNLOAD_USER, the option has nothing to give the password to.
        self.write_config()
        code, output = self.deploy_db("--config", self.config, "--cloud-password-file", password, "--check-config")
        self.assert_refused(code, output, "--cloud-password-file is the password of DOWNLOAD_USER")

    def test_a_person_on_a_terminal_is_still_asked(self):
        code, output = on_terminal(["bash", self.script, "--check-config"], without_variable(self.env),
                                   answers=[(guided_tests.SETUP, CTRL_C)])
        self.assertIsNotNone(code, output)
        self.assertIn(guided_tests.SETUP.decode(), output)
        self.assertFalse(self.config.exists())


# ─────────────────────────────────────────────────────────────────── service.sh
@unittest.skipUnless(ROOT and LINUX, "needs root on Linux (the test image)")
class ServiceNoWait(NoWait, unittest.TestCase):
    """service.sh reset with the fakes of test_service_app.py: a fake Docker and
    a fake deploy-app.sh that records its arguments."""

    setUp = service_tests.ServiceAppTests.setUp
    set_state = service_tests.ServiceAppTests.set_state
    docker_log = service_tests.ServiceAppTests.docker_log
    docker_calls = service_tests.ServiceAppTests.docker_calls
    deploy_calls = service_tests.ServiceAppTests.deploy_calls

    def reset(self, *args, env=None):
        return on_terminal(["bash", self.bundle / "service.sh", "--role", "app", "--dir", self.app, "reset", *args],
                           non_interactive(self.env) if env is None else env)

    def test_a_reset_without_confirm_asks_nothing_and_deletes_nothing(self):
        for env, extra in ((non_interactive(self.env), []), (without_variable(self.env), ["--non-interactive"])):
            with self.subTest(flag=extra):
                self.deploys.unlink(missing_ok=True)
                code, output = self.reset(*extra, env=env)
                self.assert_refused(code, output, "pass --confirm perodua", "Nothing was changed")
                self.assertEqual(self.docker_calls(), [])
                # The settings check before the plan ran without questions too.
                self.assertEqual(self.deploy_calls(), [f"held --dir {self.app} --init-db --non-interactive --check-config"])

    def test_a_reset_with_confirm_runs_deploy_app_without_questions(self):
        for env, extra in ((non_interactive(self.env), []), (without_variable(self.env), ["--non-interactive"])):
            with self.subTest(flag=extra):
                self.set_state()
                self.deploys.unlink(missing_ok=True)
                code, output = self.reset("--confirm", "perodua", *extra, env=env)
                self.assert_ended_without_a_question(code, output)
                self.assertEqual(code, 0, output)
                self.assertEqual(self.deploy_calls(), [
                    f"held --dir {self.app} --init-db --non-interactive --check-config",
                    f"free --dir {self.app} --init-db --non-interactive"])

    def test_a_person_is_still_asked_to_confirm(self):
        code, output = on_terminal(["bash", self.bundle / "service.sh", "--role", "app", "--dir", self.app, "reset"],
                                   without_variable(self.env), answers=[(b"Type perodua to confirm: ", b"wrong")])
        self.assertIn("Type perodua to confirm: ", output)
        self.assertIn("The confirmation did not match. Nothing was changed.", output)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.docker_calls(), [])
        # A person's reset passes no --non-interactive on.
        self.assertEqual(self.deploy_calls(), [f"held --dir {self.app} --init-db --check-config"])


# ───────────────────────────────────────────────────────────────── uninstall.sh
@unittest.skipUnless(ROOT and uninstall_tests.ubuntu_2404(), "needs root on Ubuntu 24.04 (the test image)")
class UninstallNoWait(NoWait, unittest.TestCase):
    """uninstall.sh with the fake Docker of test_uninstall_app.py."""

    setUp = uninstall_tests.UninstallAppTests.setUp
    stub = uninstall_tests.UninstallAppTests.stub
    set_state = uninstall_tests.UninstallAppTests.set_state
    log = staticmethod(uninstall_tests.UninstallAppTests.log)
    docker_calls = uninstall_tests.UninstallAppTests.docker_calls
    changes = uninstall_tests.UninstallAppTests.changes
    check_lock = uninstall_tests.UninstallAppTests.check_lock
    left = uninstall_tests.UninstallAppTests.left
    left_volumes = uninstall_tests.UninstallAppTests.left_volumes
    down = uninstall_tests.UninstallAppTests.down
    with_old_deployment = uninstall_tests.UninstallAppTests.with_old_deployment
    old_down = uninstall_tests.UninstallAppTests.old_down

    def uninstall(self, *args, env=None, role="app", answers=()):
        directory = [] if role == "db" else ["--dir", self.app]
        code, output = on_terminal(["bash", uninstall_tests.SCRIPT, "--role", role, *directory, *args],
                                   non_interactive(self.env) if env is None else env, answers=answers)
        self.check_lock()
        return code, output

    def test_without_confirm_nothing_changes(self):
        code, output = self.uninstall()
        self.assert_refused(code, output, f"pass --confirm {uninstall_tests.PROJECT}. Nothing was changed.")
        self.assertEqual(self.changes(), [])
        self.assertTrue(self.app.exists())

    def test_other_containers_stay_unless_named(self):
        self.with_old_deployment()
        code, output = self.uninstall("--confirm", uninstall_tests.PROJECT)
        self.assert_ended_without_a_question(code, output)
        self.assertEqual(code, 0, output)
        self.assertIn("Not deleted: --delete-old-containers does not name them. To delete them: "
                      f"sudo docker compose --project-name {uninstall_tests.OLD_PROJECT} down\n"
                      f"Or run this again with --delete-old-containers {uninstall_tests.OLD_PROJECT}.\n", output)
        self.assertNotIn(self.old_down(), self.changes())
        self.assertEqual(sorted(self.left("containers")), ["old-id-1", "old-id-2", "other-id-1"])

    def test_named_containers_go_without_a_question_and_their_volumes_only_when_named(self):
        old_volumes = set(uninstall_tests.OLD_VOLUMES)
        for extra, env, volumes_deleted in (
                ([], None, False),
                (["--delete-old-volumes"], None, True),
                ([], "person", False)):  # a person who gives the option is not asked either
            with self.subTest(extra=extra, env=env):
                self.setUp()
                self.with_old_deployment()
                code, output = self.uninstall(
                    "--confirm", uninstall_tests.PROJECT, "--delete-old-containers", uninstall_tests.OLD_PROJECT,
                    *extra, env=without_variable(self.env) if env else None)
                self.assert_ended_without_a_question(code, output)
                self.assertEqual(code, 0, output)
                self.assertIn(self.old_down(), self.changes())
                self.assertEqual([c for c in self.left("containers") if c.startswith("old-")], [])
                if volumes_deleted:
                    self.assertEqual(old_volumes & self.left_volumes(), set())
                else:
                    self.assertEqual(old_volumes & self.left_volumes(), old_volumes)
                    self.assertIn("Kept the volumes they used (", output)
                    self.assertIn("--delete-old-volumes deletes them.", output)

    def test_containers_without_a_project_are_named_with_a_dash(self):
        self.with_old_deployment(**uninstall_tests.MANUAL)
        code, output = self.uninstall("--confirm", uninstall_tests.PROJECT, "--delete-old-containers", "-")
        self.assert_ended_without_a_question(code, output)
        self.assertEqual(code, 0, output)
        self.assertIn(["rm", "-f", "manual-id-1", "manual-id-2"], self.changes())
        self.assertNotIn(self.old_down(), self.changes())

    def test_a_named_project_that_is_not_there_is_reported(self):
        code, output = self.uninstall("--confirm", uninstall_tests.PROJECT, "--delete-old-containers", "no-such-app")
        self.assertEqual(code, 0, output)
        self.assertIn('No other container of no-such-app has "perodua" in its name or image', output)

    def test_the_database_role_needs_confirm_before_postgresql_starts(self):
        code, output = self.uninstall("--database", "perodua", role="db")
        self.assert_refused(code, output, "pass --confirm perodua", "Nothing was changed")
        self.assertNotIn("PostgreSQL 16 is not installed", output)

    def test_a_person_is_still_asked(self):
        self.with_old_deployment()
        code, output = self.uninstall("--confirm", uninstall_tests.PROJECT, env=without_variable(self.env),
                                      answers=[(b"[y/N]: ", b"n")])
        self.assertEqual(code, 0, output)
        self.assertIn("Is this an old deployment? Delete these containers [y/N]: ", output)
        self.assertIn("Kept them.", output)


# ──────────────────────────────────────────────────────────────── deploy-app.sh
@unittest.skipUnless(LINUX, "needs Linux (GNU tools and bash 4 or later)")
class DeployAppNoWait(NoWait, unittest.TestCase):
    """deploy-app.sh with the fake Docker of test_deploy_app.py."""

    setUp = app_tests.DeploymentSafetyTests.setUp
    fake_docker_source = staticmethod(app_tests.DeploymentSafetyTests.fake_docker_source)
    write_config = app_tests.DeploymentSafetyTests.write_config
    command = app_tests.DeploymentSafetyTests.command
    docker_calls = app_tests.DeploymentSafetyTests.docker_calls

    def test_without_settings_a_non_interactive_run_names_the_setting(self):
        empty = self.base / "empty-app"
        code, output = on_terminal(["bash", app_tests.SCRIPT, "--dir", empty], non_interactive(self.env))
        self.assert_refused(code, output, "DB_HOST is required in --config")
        self.assertNotIn("Database server IP / hostname", output)
        self.assertEqual(self.docker_calls(), [])

    def test_the_mail_questions_are_refused_at_once(self):
        code, output = on_terminal(self.command("--mail"), non_interactive(self.env))
        self.assert_refused(code, output, "--mail asks the outgoing email questions")
        self.assertNotIn("SMTP server", output)
        self.assertEqual(self.docker_calls(), [])

    def test_a_registry_login_is_never_asked(self):
        self.env["FAKE_DOCKER_MODE"] = "auth"  # every pull needs a login
        code, output = on_terminal(self.command(), non_interactive(self.env))
        self.assert_refused(code, output, "Registry authentication is required. Login with docker login")
        self.assertNotIn("Registry username", output)

    def test_a_person_is_still_asked_for_the_database_settings(self):
        empty = self.base / "empty-app"
        code, output = on_terminal(["bash", app_tests.SCRIPT, "--dir", empty], without_variable(self.env),
                                   answers=[(b"Database server IP / hostname", CTRL_C)])
        self.assertIsNotNone(code, output)
        self.assertIn("Database server IP / hostname", output)


@unittest.skipUnless(LINUX, "needs Linux (GNU tools and bash 4 or later)")
class DeployAppAttachmentsNoWait(NoWait, unittest.TestCase):
    """The delete of the attachments volume an earlier deployment left, with the
    scripted fake Docker of test_deploy_app_uat.py."""

    VOLUME = "perodua-uat-fixture_filestore"
    setUp = uat_tests.UatOrchestrationTests.setUp
    configure = uat_tests.UatOrchestrationTests.configure
    calls = uat_tests.UatOrchestrationTests.calls
    container_steps = uat_tests.UatOrchestrationTests.container_steps
    volume_removals = uat_tests.UatOrchestrationTests.volume_removals
    FRESH_STEPS = uat_tests.UatOrchestrationTests.FRESH_STEPS

    def deploy(self, *extra, variable=True):
        """A new UAT database (EMPTY), and the attachments volume holds 3 files of an earlier deployment."""
        env = dict(self.env, FAKE_CHECK="EMPTY", FAKE_LEFTOVER="1", FAKE_LEFTOVER_FILES="3")
        return on_terminal(["bash", self.script, "--config", self.config, "--dir", self.deploy_dir, "--init-db", *extra],
                           non_interactive(env) if variable else without_variable(env), timeout=60)

    def test_leftover_attachments_stop_a_non_interactive_run_and_name_the_option(self):
        code, output = self.deploy()
        self.assert_refused(code, output, f"--delete-old-attachments {self.VOLUME}", "The database was not changed")
        self.assertEqual(self.volume_removals(), [])
        self.assertEqual(self.container_steps(), ["preflight check", "leftover check"])

    def test_the_option_deletes_the_volume_without_a_question(self):
        # With the variable, and for a person on a terminal who gives the option.
        for number, variable in enumerate((True, False)):
            with self.subTest(variable=variable):
                self.log.unlink(missing_ok=True)
                self.deploy_dir = Path(self.temp.name) / f"app-{number}"
                code, output = self.deploy("--delete-old-attachments", self.VOLUME, variable=variable)
                self.assert_ended_without_a_question(code, output)
                self.assertEqual(code, 0, output)
                self.assertIn(f"Deleted {self.VOLUME}.", output)
                self.assertEqual(self.volume_removals(), [["volume", "rm", self.VOLUME]])
                self.assertEqual(self.container_steps()[:4], ["preflight check", "leftover check", *self.FRESH_STEPS])

    def test_the_option_names_only_the_volume_of_this_project(self):
        code, output = self.deploy("--delete-old-attachments", "other_filestore")
        self.assert_refused(code, output, f"--delete-old-attachments must name {self.VOLUME}")
        self.assertEqual(self.calls(), [])


# ──────────────────────────────────────────────────────────────────── iss-api.sh
@unittest.skipUnless(ROOT and LINUX, "needs root on Linux (the test image)")
class IssApiNoWait(NoWait, unittest.TestCase):
    """iss-api.sh with the fake Docker and ss of test_iss_api.py."""

    setUp = iss_tests.IssApiTest.setUp
    set_state = iss_tests.IssApiTest.set_state
    write_config = iss_tests.IssApiTest.write_config
    run_script = iss_tests.IssApiTest.run_script
    install = iss_tests.IssApiTest.install
    installed = iss_tests.IssApiTest.installed
    calls = iss_tests.IssApiTest.calls

    def iss_api(self, *args, env=None, answers=()):
        return on_terminal(["bash", iss_tests.SCRIPT, *args, "--dir", self.dir],
                           non_interactive(self.env) if env is None else env, answers=answers)

    def test_check_takes_the_key_from_key_file(self):
        self.installed()
        key = self.tmp / "api-key"
        key.write_text("k3y-from-a-file\r\n")  # saved on Windows
        self.log.unlink(missing_ok=True)
        code, output = self.iss_api("check", "--key-file", key)
        self.assert_ended_without_a_question(code, output)
        self.assertEqual(code, 0, output)
        checks = [call for call in self.calls() if call["args"][5:9] == iss_tests.CHECK]
        self.assertEqual([call["stdin"] for call in checks], ["k3y-from-a-file"])
        for call in self.calls():
            self.assertNotIn("k3y-from-a-file", " ".join(call["args"]))

    def test_uninstall_without_confirm_asks_nothing(self):
        self.installed()
        code, output = self.iss_api("uninstall")
        self.assert_refused(code, output, f"pass --confirm {iss_tests.PROJECT}. Nothing was changed.")
        self.assertTrue((self.dir / "compose.yml").exists())

    def test_a_first_install_without_settings_asks_nothing(self):
        code, output = self.iss_api("install")
        self.assert_refused(code, output, "No settings yet")
        self.assertNotIn("Oracle database server", output)

    def test_non_interactive_is_an_option_of_every_command(self):
        self.installed()
        result = self.run_script("check", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_person_is_still_asked_to_confirm(self):
        self.installed()
        prompt = f"Type the project name ({iss_tests.PROJECT}) to confirm: "
        code, output = self.iss_api("uninstall", env=without_variable(self.env), answers=[(prompt.encode(), b"wrong")])
        self.assertIn(prompt, output)
        self.assertIn("Not confirmed. Nothing was changed.", output)
        self.assertNotEqual(code, 0)


# ───────────────────────────────────────────────────────────────── rp-adapter.sh
@unittest.skipUnless(ROOT and LINUX, "needs root on Linux (a disposable test container)")
class RpAdapterNoWait(NoWait, unittest.TestCase):
    """rp-adapter.sh with the fake Docker of test_rp_adapter.py."""

    setUp = rp_tests.RpAdapterTest.setUp
    set_state = rp_tests.RpAdapterTest.set_state
    config = rp_tests.RpAdapterTest.config
    run_script = rp_tests.RpAdapterTest.run_script
    calls = rp_tests.RpAdapterTest.calls
    install = rp_tests.RpAdapterTest.install

    def rp_adapter(self, *args, env=None, answers=()):
        return on_terminal(["bash", rp_tests.SCRIPT, *args, "--dir", self.dir],
                           non_interactive(self.env) if env is None else env, answers=answers)

    def test_uninstall_without_confirm_asks_nothing(self):
        self.install()
        code, output = self.rp_adapter("uninstall")
        self.assert_refused(code, output, f"pass --confirm {self.dir}. Nothing was changed.")
        self.assertTrue((self.dir / "rp-adapter.env").exists())

    def test_a_first_install_without_settings_asks_nothing(self):
        code, output = self.rp_adapter("install")
        self.assert_refused(code, output, "No settings yet")
        self.assertNotIn("Oracle database server", output)

    def test_a_registry_login_is_never_asked(self):
        self.set_state(pull_error="unauthorized: authentication required")
        code, output = self.rp_adapter("install", "--config", self.config())
        self.assert_refused(code, output, "run docker login")
        self.assertNotIn("Registry username", output)

    def test_non_interactive_is_an_option_of_every_command(self):
        self.install()
        result = self.run_script("status", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_person_is_still_asked_to_confirm(self):
        self.install()
        prompt = f"Type the directory ({self.dir}) to confirm: "
        code, output = self.rp_adapter("uninstall", env=without_variable(self.env), answers=[(prompt.encode(), b"wrong")])
        self.assertIn(prompt, output)
        self.assertIn("Not confirmed. Nothing was changed.", output)
        self.assertNotEqual(code, 0)


# ───────────────────────────────────────────────────────────────────── https.sh
@https_tests.needs_root
class HttpsNoWait(NoWait, https_tests.Base):
    """https.sh with the fakes of test_https.py (https_tests.Base has no tests)."""

    PASS = "Fixture pass phrase 2026"

    def setUp(self):
        super().setUp()
        self.encrypted = self.tmp / "encrypted.key"
        https_tests.openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-aes256",
                            "-pass", f"pass:{self.PASS}", "-out", self.encrypted)
        self.expected = https_tests.openssl("pkey", "-in", self.encrypted, "-passin", f"pass:{self.PASS}", "-pubout")

    def https(self, *args, env=None, answers=()):
        return on_terminal(["bash", https_tests.SCRIPT, *args, "--dir", self.dir, "--nginx-conf", self.conf,
                            "--unit-dir", self.units, "--lib-dir", self.lib],
                           non_interactive(self.env) if env is None else env, answers=answers, timeout=60)

    def written_key(self):
        """The public key of key.pem, read without a pass phrase: nginx needs it unencrypted."""
        return https_tests.openssl("pkey", "-in", self.dir / "key.pem", "-passin", "pass:", "-pubout")

    def test_an_encrypted_key_needs_key_pass_file(self):
        code, output = self.https("csr", "--key", self.encrypted)
        self.assert_refused(code, output, "--key-pass-file FILE")
        self.assertNotIn("Enter pass phrase", output)  # openssl's own question
        self.assertNotIn("key.pem.tmp", self.files())
        self.assertFalse((self.dir / "key.pem").exists())

    def test_key_pass_file_gives_the_pass_phrase(self):
        secret = self.tmp / "pass"
        secret.write_text(self.PASS + "\n")
        code, output = self.https("csr", "--key", self.encrypted, "--key-pass-file", secret)
        self.assert_ended_without_a_question(code, output)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.written_key(), self.expected)
        self.assertNotIn(self.PASS, output)

    def test_another_pass_phrase_is_refused(self):
        secret = self.tmp / "pass"
        secret.write_text("not the pass phrase\n")
        code, output = self.https("csr", "--key", self.encrypted, "--key-pass-file", secret)
        self.assert_refused(code, output, "holds another pass phrase")
        self.assertNotIn("key.pem.tmp", self.files())

    def test_uninstall_without_confirm_asks_nothing(self):
        code, output = self.https("uninstall")
        self.assert_refused(code, output, "pass --confirm yes. Nothing was changed.")

    def test_a_person_types_the_pass_phrase_on_the_terminal(self):
        code, output = self.https("csr", "--key", self.encrypted, env=without_variable(self.env),
                                  answers=[(b"Pass phrase of ", self.PASS.encode())])
        self.assertEqual(code, 0, output)
        self.assertIn(f"Pass phrase of {self.encrypted} (hidden): ", output)
        self.assertNotIn(self.PASS, output)  # not echoed
        self.assertNotIn("Enter pass phrase", output)  # asked by the script, never by openssl
        self.assertEqual(self.written_key(), self.expected)


if __name__ == "__main__":
    unittest.main()
