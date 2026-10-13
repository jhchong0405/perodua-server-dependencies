"""Deployment command safety checks. Docker is always replaced by a local fake.

Run on Linux/WSL: python3 -m unittest discover -s tests -v
These are process-level failure/interactive tests, not a registry or Odoo acceptance test.
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
import termios
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "deploy-app.sh"
SUPPORT_LINE = re.compile(r"^PUBLIC_ROOT_SUPPORTED=[01]$", re.M)
HANDOVER_LINE = re.compile(r"^HANDOVER_SUPPORTED=[01]$", re.M)
MAIL_LINE = re.compile(r"^MAIL_SUPPORTED=[01]$", re.M)
# An obvious fake: no test ever holds a real SMTP password.
SMTP_PASSWORD = "fake-app-password"


def release_copy(folder, supported):
    """deploy-app.sh and its helpers in another folder, as a release whose web
    image does (1) or does not (0) serve the page under PUBLIC_ROOT."""
    folder.mkdir()
    text = SCRIPT.read_text(encoding="utf-8")
    assert len(SUPPORT_LINE.findall(text)) == 1, "deploy-app.sh must set PUBLIC_ROOT_SUPPORTED once"
    (folder / "deploy-app.sh").write_text(SUPPORT_LINE.sub(f"PUBLIC_ROOT_SUPPORTED={int(supported)}", text),
                                          encoding="utf-8")
    for helper in ("uat_guard.py", "uat_admins.py"):
        shutil.copy(SCRIPTS / helper, folder / helper)
    return folder / "deploy-app.sh"


def service_environment(compose, service):
    """The environment of one service in the generated compose.yml, as a dict."""
    lines = compose.splitlines()
    start = lines.index(f"  {service}:")
    environment, inside = {}, False
    for line in lines[start + 1:]:
        indent = len(line) - len(line.lstrip(" "))
        if line.strip() and indent <= 2:
            break  # the next service, or the next top-level key
        if line == "    environment:":
            inside = True
        elif inside and indent >= 6:
            key, _, value = line.strip().partition(": ")
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            environment[key] = value
        else:
            inside = False
    return environment


class DeploymentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="perodua-script-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.auth = self.base / "original-docker-config"
        self.auth.mkdir()
        self.auth_file = self.auth / "config.json"
        self.auth_original = b'{"auths":{},"fixture":"preserve-this-config"}\n'
        self.auth_file.write_bytes(self.auth_original)
        self.calls = self.base / "docker-calls.jsonl"
        self.deploy_dir = self.base / "app"
        self.password = self.base / "db-password"
        self.password.write_text("fixture-password\n", encoding="utf-8")
        self.password.chmod(0o600)
        self.config = self.base / "app.env"
        self.values = {
            "DB_HOST": "192.0.2.20",
            "DB_PORT": "5432",
            "DB_NAME": "perodua",
            "DB_USER": "odoo",
            "DB_PASSWORD_FILE": str(self.password),
            "PROJECT_NAME": "perodua-fixture",
            "HTTP_PORT": "18110",
            "BIND_IP": "127.0.0.1",
            "STARTUP_TIMEOUT": "600",
            "INIT_TIMEOUT": "3600",
        }
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home),
            "DOCKER_CONFIG": str(self.auth),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "FAKE_DOCKER_LOG": str(self.calls),
            "FAKE_DOCKER_STATE": str(self.base / "fake-state"),
        })
        # No test may fall through to the real Docker CLI. Unknown commands fail.
        self.fake = self.bin / "docker"
        self.fake.write_text(self.fake_docker_source(), encoding="utf-8")
        self.fake.chmod(0o755)
        # This server's addresses, for the "who may open the web page" choices:
        # one private and one public.
        hostname = self.bin / "hostname"
        hostname.write_text('#!/bin/sh\n[ "$1" = -I ] && echo "10.1.2.3 203.0.113.7 "\n', encoding="utf-8")
        hostname.chmod(0o755)
        self.write_config()

    @staticmethod
    def fake_docker_source():
        return r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
entry = {'args': args, 'config': os.environ.get('DOCKER_CONFIG')}
with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(entry) + '\n')
if os.environ.get('FAKE_DOCKER_MODE') in ('auth', 'network'):
    if args[:2] == ['compose', 'version']:
        print('Docker Compose version v2.39.4'); sys.exit(0)
    if args and args[0] == 'info':
        if '--format' in args: print('linux/x86_64')
        sys.exit(0)
    if args and args[0] == 'ps': sys.exit(0)
    if len(args) > 1 and args[0] in ('volume', 'network') and args[1] == 'ls': sys.exit(0)
    if args[:2] == ['context', 'inspect']:
        print('unix:///var/run/docker.sock'); sys.exit(0)
    if args and args[0] == 'pull':
        if os.environ.get('FAKE_DOCKER_MODE') == 'network':
            print('dial tcp: network is unreachable', file=sys.stderr); sys.exit(1)
        if state.exists():
            print('Fixture image available'); sys.exit(0)
        print('unauthorized: authentication required', file=sys.stderr); sys.exit(1)
    if args and args[0] == 'login':
        token = sys.stdin.read()
        with open(str(state) + '.tokens', 'a', encoding='utf-8') as received:
            received.write(json.dumps(token) + '\n')
        if '--password-stdin' not in args: sys.exit(94)
        if token == 'fixture-valid-token':
            state.write_text('authenticated')
            print('Login Succeeded'); sys.exit(0)
        print('unauthorized: fixture token rejected', file=sys.stderr); sys.exit(1)
    if args and args[0] == 'compose' and 'config' in args: sys.exit(0)
    if args and args[0] == 'compose' and 'perodua-mail-test' in args:
        print('fake perodua-mail-test: one test email to ' + args[-1])
        sys.exit(int(os.environ.get('FAKE_MAIL_TEST_EXIT', '0')))
    if args and args[0] == 'compose' and 'run' in args:
        # FAKE_PREFLIGHT_STATE: the database check answers with this state,
        # and the fixture stops at the next Docker command instead.
        if os.environ.get('FAKE_PREFLIGHT_STATE') and args[-3:] == ['/opt/deploy/preflight.py', 'check', '--accept-pending']:
            print(os.environ['FAKE_PREFLIGHT_STATE']); sys.exit(0)
        print('Fixture stopped before any database or container operation', file=sys.stderr)
        sys.exit(92)
print('Fake Docker rejected unexpected command: ' + ' '.join(args), file=sys.stderr)
sys.exit(93)
'''

    def write_config(self, **updates):
        values = dict(self.values)
        values.update(updates)
        self.config.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        self.config.chmod(0o600)

    def command(self, *extra, script=SCRIPT):
        return ["bash", str(script), "--config", str(self.config),
                "--dir", str(self.deploy_dir), *extra]

    def run_script(self, *extra, script=SCRIPT):
        return subprocess.run(self.command(*extra, script=script), env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=20)

    def run_saved(self, script):
        """A later run that reads only the app.env the first run saved."""
        return subprocess.run(["bash", str(script), "--dir", str(self.deploy_dir), "--non-interactive"],
                              env=self.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=20)

    def pulls_succeed(self):
        """Fake Docker answers up to the first database check, then stops."""
        self.env["FAKE_DOCKER_MODE"] = "auth"
        (self.base / "fake-state").write_text("authenticated")

    def docker_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def terminal_exchange(self, responses, command=None):
        """Drive real Bash reads on a PTY and check token prompts disable echo."""
        pid, master = pty.fork()
        if pid == 0:
            os.execvpe("bash", command or self.command(), self.env)
        output = b""
        cursor = 0
        deadline = time.monotonic() + 20
        status = None

        def read_more():
            nonlocal output
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                output += chunk
            return True

        try:
            for prompt, response, echo_enabled in responses:
                while prompt not in output[cursor:]:
                    if time.monotonic() >= deadline or not read_more():
                        self.fail("Expected terminal prompt %r; received %s" %
                                  (prompt, output.decode(errors="replace")))
                cursor = output.index(prompt, cursor) + len(prompt)
                current_echo = bool(termios.tcgetattr(master)[3] & termios.ECHO)
                self.assertEqual(current_echo, echo_enabled,
                                 "Unexpected terminal echo state at " + repr(prompt))
                os.write(master, response + b"\n")
            while time.monotonic() < deadline:
                read_more()
                exited, status_value = os.waitpid(pid, os.WNOHANG)
                if exited:
                    status = status_value
                    while read_more():
                        if not select.select([master], [], [], 0)[0]:
                            break
                    return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
            self.fail("Deployment did not finish after terminal input: " + output.decode(errors="replace"))
        finally:
            if status is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except ProcessLookupError:
                    pass
            os.close(master)

    def assert_original_auth_unchanged(self):
        self.assertEqual(self.auth_file.read_bytes(), self.auth_original)

    def assert_early_failure(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.docker_calls(), [], result.stdout)
        self.assertNotIn("fixture-password", result.stdout)
        self.assert_original_auth_unchanged()

    def test_scripts_have_valid_bash_syntax(self):
        for path in (SCRIPT, SCRIPTS / "install-dependencies.sh"):
            with self.subTest(script=path.name):
                result = subprocess.run(["bash", "-n", str(path)], text=True,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_error_traps_report_only_failures_the_main_shell_meets(self):
        # A command that fails inside $( ) or <( ) is checked where its result is
        # used, or the script goes on; it must not print "failed at line" (an App
        # deployment printed "Deployment failed at line 124" and went on).
        # Real failures, also those inside functions and $( ), are still reported.
        for path, prelude in ((SCRIPT, ""), (SCRIPTS / "deploy-db.sh", "PHASE=check\n"),
                              (SCRIPTS / "install-dependencies.sh", "")):
            trap = next(line for line in path.read_text().splitlines()
                        if line.startswith("trap ") and line.endswith(" ERR"))
            head = "set -Eeuo pipefail\n" + prelude + trap + "\n"
            with self.subTest(script=path.name):
                quiet = subprocess.run(
                    ["bash", "-c", head + "f() { local a=(); mapfile -t a < <(find /nonexistent 2>/dev/null); }\n"
                     "f\nfor x in $(false); do :; done\nwait\necho reached\n"],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual((quiet.stdout, quiet.stderr), ("reached\n", ""))
                for body in ("false\n", "g() { false; }\ng\n", "x=$(false)\n"):
                    loud = subprocess.run(["bash", "-c", head + body + "echo reached\n"],
                                          capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(loud.returncode, 0, body)
                    self.assertEqual(loud.stdout, "", body)
                    self.assertEqual(len(re.findall(r"failed at (?:script )?line \d+", loud.stderr)), 1, body)

    def test_deployment_script_has_unix_line_endings(self):
        body = SCRIPT.read_bytes()
        self.assertTrue(body.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r", body)

    def test_help_needs_no_docker(self):
        result = subprocess.run(["bash", str(SCRIPT), "--help"], env=self.env,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config", result.stdout)
        self.assertEqual(self.docker_calls(), [])
        # The help names the release the script pins.
        version = re.search(r"^RELEASE=client-stable-uiux-v(\d+\.\d+\.\d+)$", SCRIPT.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(version)
        self.assertIn(f"Deploy the pinned Client Stable UIUX v{version.group(1)} Odoo + Web images", result.stdout)

    def test_unknown_option_and_missing_option_values_fail_early(self):
        for args in (("--unknown",), ("--dir",), ("--config",)):
            with self.subTest(args=args):
                result = subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                                        text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=10)
                self.assert_early_failure(result)

    def test_unknown_configuration_key_is_rejected(self):
        self.write_config(UNRECOGNIZED="value")
        self.assert_early_failure(self.run_script("--non-interactive"))

    def test_invalid_connection_parameters_fail_before_docker(self):
        invalid = (("DB_HOST", ""), ("DB_PORT", "not-a-port"),
                   ("DB_PORT", "65536"), ("HTTP_PORT", "0"),
                   ("DB_NAME", ""), ("DB_USER", ""))
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.write_config(**{key: value})
                self.assert_early_failure(self.run_script("--non-interactive"))

    def test_configuration_shell_expressions_cannot_execute(self):
        marker = self.base / "configuration-was-executed"
        expressions = (f"$(touch {marker})", f"`touch {marker}`",
                       f"valid; touch {marker}")
        for value in expressions:
            with self.subTest(value=value):
                self.write_config(DB_HOST=value)
                result = self.run_script("--non-interactive")
                self.assert_early_failure(result)
                self.assertFalse(marker.exists(), "Configuration executed a shell command")

    def test_configuration_file_cannot_run_a_standalone_command(self):
        marker = self.base / "configuration-command-was-executed"
        with self.config.open("a", encoding="utf-8") as config:
            config.write(f"touch {marker}\n")
        self.assert_early_failure(self.run_script("--non-interactive"))
        self.assertFalse(marker.exists())

    def test_missing_password_file_fails_without_disclosing_password(self):
        self.password.unlink()
        self.assert_early_failure(self.run_script("--non-interactive"))

    def test_noninteractive_auth_failure_does_not_prompt_or_change_auth(self):
        self.env["FAKE_DOCKER_MODE"] = "auth"
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Registry authentication", result.stdout)
        self.assertIn("docker login perodua-deploy.novutal.com", result.stdout)
        self.assertNotIn("Registry username", result.stdout)
        self.assertFalse(any(call['args'][0] == 'login' for call in self.docker_calls()))
        self.assert_original_auth_unchanged()

    def test_network_failure_is_not_presented_as_a_token_problem(self):
        self.env["FAKE_DOCKER_MODE"] = "network"
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network", result.stdout.lower())
        self.assertNotIn("authentication is required", result.stdout)
        self.assertFalse(any(call['args'][0] == 'login' for call in self.docker_calls()))
        self.assert_original_auth_unchanged()

    def test_hidden_token_retries_and_uses_temporary_docker_auth(self):
        self.env["FAKE_DOCKER_MODE"] = "auth"
        prompts = [
            (b"Registry username", b"fixture-user", True),
            (b"Registry password", b"fixture-invalid-token", False),
            (b"Registry username", b"fixture-user", True),
            (b"Registry password", b"fixture-valid-token", False),
        ]
        code, output = self.terminal_exchange(prompts)
        self.assertNotEqual(code, 0, "Fake Docker must stop before database operations")
        self.assertIn("Login failed", output)
        self.assertIn("Fixture stopped before", output)
        self.assertNotIn("fixture-invalid-token", output)
        self.assertNotIn("fixture-valid-token", output)
        logins = [call for call in self.docker_calls() if call['args'][0] == 'login']
        self.assertEqual(len(logins), 2)
        for login in logins:
            self.assertIn("--password-stdin", login['args'])
            self.assertEqual(login['args'][1], "perodua-deploy.novutal.com")
            self.assertNotEqual(login['config'], str(self.auth))
            self.assertFalse(Path(login['config']).exists(), "Temporary credentials survived exit")
            self.assertNotIn("fixture-valid-token", " ".join(login['args']))
        received = Path(str(self.base / 'fake-state') + '.tokens')
        self.assertEqual([json.loads(line) for line in received.read_text().splitlines()],
                         ["fixture-invalid-token", "fixture-valid-token"])
        self.assert_original_auth_unchanged()

    def interactive_run(self, host_answers, choices, port=b""):
        """The first interactive run: database questions, web port, who may open the page."""
        self.env["FAKE_DOCKER_MODE"] = "auth"
        (self.base / "fake-state").write_text("authenticated")  # pulls succeed without a login
        host = [(b"Database server IP / hostname []: ", answer, True) for answer in host_answers]
        return self.terminal_exchange(host + [
            (b"Database port [5432]: ", b"", True),
            (b"Application database name [perodua]: ", b"", True),
            (b"Database username [odoo]: ", b"", True),
            (b"Web port that browsers open on this server [8110]: ", port, True),
            *choices,
            (b"Database password (hidden): ", b"fixture-password", False),
        ], command=["bash", str(SCRIPT), "--dir", str(self.deploy_dir)])

    def test_first_interactive_run_asks_for_the_web_port_and_saves_it(self):
        code, output = self.interactive_run([b"192.0.2.20"], [(b"Choose [2]: ", b"", True)], port=b"18111")
        self.assertNotEqual(code, 0, "Fake Docker must stop before database operations")
        self.assertIn("Fixture stopped before", output)
        # Nothing claims a failure before the fixture stops it, whether or not
        # deploy-db.sh ever ran on the test host.
        self.assertNotIn("failed at line", output.split("Fixture stopped before")[0])
        self.assertNotIn("fixture-password", output)
        saved = (self.deploy_dir / "app.env").read_text()
        for line in ("DB_HOST=192.0.2.20", "DB_PORT=5432", "DB_NAME=perodua", "DB_USER=odoo", "HTTP_PORT=18111",
                     "BIND_IP=10.1.2.3"):
            self.assertIn(line + "\n", saved)
        # The private network is the default; the public address is not offered on its own.
        self.assertIn("2) Computers on the private network, through 10.1.2.3", output)
        self.assertNotIn("through 203.0.113.7", output)
        self.assertIn('"10.1.2.3:18111:80"', (self.deploy_dir / "compose.yml").read_text())

    def test_this_server_only_binds_to_loopback(self):
        code, output = self.interactive_run([b"192.0.2.20"], [(b"Choose [2]: ", b"1", True)])
        self.assertIn("Fixture stopped before", output)
        self.assertIn('"127.0.0.1:8110:80"', (self.deploy_dir / "compose.yml").read_text())

    def test_every_address_on_a_public_server_needs_a_confirmation(self):
        code, output = self.interactive_run([b"192.0.2.20"], [
            (b"Choose [2]: ", b"9", True),
            (b"Choose [2]: ", b"3", True),
            (b"Open it on every address anyway? [y/N]: ", b"", True),
            (b"Choose [2]: ", b"3", True),
            (b"Open it on every address anyway? [y/N]: ", b"y", True),
        ])
        self.assertIn("Fixture stopped before", output)
        self.assertIn("Enter a number from 1 to 3.", output)
        self.assertIn("203.0.113.7 is a public internet address", output)
        self.assertIn("Docker opens published ports past ufw", output)
        self.assertIn('"0.0.0.0:8110:80"', (self.deploy_dir / "compose.yml").read_text())

    def test_a_loopback_database_host_is_explained_and_asked_again(self):
        code, output = self.interactive_run([b"127.0.0.1", b"192.0.2.20"], [(b"Choose [2]: ", b"", True)])
        self.assertIn("Fixture stopped before", output)
        self.assertIn("127.0.0.1 would be the App container itself", output)
        self.assertIn("DB_HOST=192.0.2.20\n", (self.deploy_dir / "app.env").read_text())

    def test_a_loopback_database_host_in_the_configuration_fails_before_docker(self):
        for value in ("localhost", "127.0.0.1"):
            with self.subTest(value=value):
                self.write_config(DB_HOST=value)
                result = self.run_script("--non-interactive")
                self.assert_early_failure(result)
                self.assertIn("would be the App container itself", result.stdout)

    def test_every_address_from_a_configuration_warns_on_a_public_server(self):
        self.env["FAKE_DOCKER_MODE"] = "auth"
        (self.base / "fake-state").write_text("authenticated")
        self.write_config(BIND_IP="0.0.0.0")
        result = self.run_script("--non-interactive")
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertIn("Warning: BIND_IP=0.0.0.0 opens the web page on every address of this server, "
                      "including the public 203.0.113.7", result.stdout)

    def test_blank_hidden_token_cancels_without_login(self):
        self.env["FAKE_DOCKER_MODE"] = "auth"
        code, output = self.terminal_exchange([
            (b"Registry username", b"fixture-user", True),
            (b"Registry password", b"", False),
        ])
        self.assertNotEqual(code, 0)
        self.assertIn("Login cancelled", output)
        self.assertFalse(any(call['args'][0] == 'login' for call in self.docker_calls()))
        self.assert_original_auth_unchanged()

    # ── the page under a path: PUBLIC_ROOT, ENVIRONMENT_LABEL, PUBLIC_BASE_URL ──
    PUBLIC = {"PUBLIC_ROOT": "/dev", "ENVIRONMENT_LABEL": "DEV (Client Stable) 1.0-a_b",
              "PUBLIC_BASE_URL": "https://stgissrp.perodua.com.my/dev"}

    def test_public_settings_are_saved_passed_to_the_web_container_and_read_back(self):
        script = release_copy(self.base / "release", supported=True)
        self.pulls_succeed()
        self.write_config(**self.PUBLIC)
        result = self.run_script("--non-interactive", script=script)
        self.assertIn("Fixture stopped before", result.stdout)  # it went on to the database check
        saved = (self.deploy_dir / "app.env").read_text()
        for key, value in self.PUBLIC.items():
            self.assertIn(f"{key}={value}\n", saved)
        compose = (self.deploy_dir / "compose.yml").read_text()
        web = service_environment(compose, "web")
        self.assertEqual(web["PUBLIC_ROOT"], "/dev")
        self.assertEqual(web["ENVIRONMENT_LABEL"], "DEV (Client Stable) 1.0-a_b")
        self.assertEqual(web["ODOO_UPSTREAM"], "odoo:8069")
        self.assertNotIn("PUBLIC_ROOT", service_environment(compose, "odoo"))
        # A later run without --config reads app.env and saves the same settings.
        result = self.run_saved(script)
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertEqual((self.deploy_dir / "app.env").read_text(), saved)
        self.assertEqual((self.deploy_dir / "compose.yml").read_text(), compose)

    # The keys the scripts of v1.0.3 and earlier (26c70b7) accept in app.env.
    EARLIER_KEYS = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD_FILE", "PROJECT_NAME",
                    "HTTP_PORT", "BIND_IP", "STARTUP_TIMEOUT", "INIT_TIMEOUT"}

    def test_a_configuration_without_public_settings_does_not_save_them(self):
        # Empty is their default. Left out, app.env stays readable by earlier
        # scripts, which refuse unknown keys (a reset from an older folder).
        for given in ({}, {key: "" for key in self.PUBLIC}):
            with self.subTest(given=given):
                self.calls.unlink(missing_ok=True)
                self.pulls_succeed()
                self.write_config(**given)
                result = self.run_script("--non-interactive")
                self.assertIn("Fixture stopped before", result.stdout)
                saved = (self.deploy_dir / "app.env").read_text()
                keys = {line.partition("=")[0] for line in saved.splitlines()}
                self.assertEqual(keys, self.EARLIER_KEYS)
                web = service_environment((self.deploy_dir / "compose.yml").read_text(), "web")
                self.assertEqual((web["PUBLIC_ROOT"], web["ENVIRONMENT_LABEL"]), ("", ""))
                result = self.run_saved(SCRIPT)
                self.assertIn("Fixture stopped before", result.stdout)
                self.assertEqual((self.deploy_dir / "app.env").read_text(), saved)

    def test_clearing_a_public_setting_removes_it_from_app_env(self):
        script = release_copy(self.base / "release", supported=True)
        self.pulls_succeed()
        self.write_config(**self.PUBLIC)
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive", script=script).stdout)
        self.write_config(PUBLIC_ROOT="/dev", ENVIRONMENT_LABEL="", PUBLIC_BASE_URL="https://stgissrp.perodua.com.my/dev")
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive", script=script).stdout)
        saved = (self.deploy_dir / "app.env").read_text()
        self.assertNotIn("ENVIRONMENT_LABEL", saved)
        self.assertIn("\nPUBLIC_ROOT=/dev\n", saved)

    def test_invalid_public_settings_fail_before_docker(self):
        script = release_copy(self.base / "release", supported=True)
        marker = self.base / "public-setting-was-executed"
        invalid = {
            "PUBLIC_ROOT": ("dev", "/", "/dev/", "/Dev", "/-dev", "/dev/uat", "/d_v", "/" + "a" * 32,
                            "/dev app", " /dev", f"/$(touch {marker})"),
            "ENVIRONMENT_LABEL": ("x" * 41, "UAT;id", 'UAT"', "UAT$HOME", "UAT\\1", "UAT/1", "UAT#1",
                                  "UATé", f"`touch {marker}`"),
            "PUBLIC_BASE_URL": ("stgissrp.perodua.com.my/dev", "https://stgissrp.perodua.com.my/dev/",
                                "ftp://stgissrp.perodua.com.my/dev", "https://stgissrp.perodua.com.my/dev?a=1",
                                "https://user@stgissrp.perodua.com.my/dev", "https://host:0/dev",
                                "https://host:65536/dev", "https://host/dev/app", "https://host/Dev",
                                "https:///dev", "https://host/dev#x", f"https://host/$(touch {marker})",
                                # Not host names: it would become the frozen web.base.url.
                                "https://-/dev", "https://../dev", "https://a..b/dev", "https://.-./dev",
                                "https://-a.b/dev", "https://a-.b/dev", "https://a.b./dev", "https://.a.b/dev",
                                f"https://{'a' * 64}.example/dev", "https://" + ".".join(["a" * 63] * 4) + "/dev"),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.write_config(**{key: value})
                    result = self.run_script("--non-interactive", script=script)
                    self.assert_early_failure(result)
                    self.assertIn(key, result.stdout)
        self.assertFalse(marker.exists(), "A public setting executed a shell command")

    def test_the_public_base_url_must_end_with_the_public_root(self):
        script = release_copy(self.base / "release", supported=True)
        # "https://dev" ends with the characters "/dev", but has no path.
        for url in ("https://stgissrp.perodua.com.my/uat", "https://stgissrp.perodua.com.my",
                    "https://dev", "https://stgissrp.perodua.com.my/xdev", "https://stgissrp.perodua.com.my/devx"):
            with self.subTest(url=url):
                self.write_config(PUBLIC_ROOT="/dev", PUBLIC_BASE_URL=url)
                result = self.run_script("--non-interactive", script=script)
                self.assert_early_failure(result)
                self.assertIn("PUBLIC_BASE_URL must end with PUBLIC_ROOT (/dev)", result.stdout)

    def test_a_base_url_path_needs_the_same_public_root(self):
        # The web container serves the page at the root then: /dev/app/ would
        # be a 404, and a front proxy that removed /dev would leave the page
        # loading /app/assets/ outside /dev.
        script = release_copy(self.base / "release", supported=True)
        self.write_config(PUBLIC_ROOT="", PUBLIC_BASE_URL="https://stgissrp.perodua.com.my/dev")
        result = self.run_script("--non-interactive", script=script)
        self.assert_early_failure(result)
        self.assertIn("PUBLIC_BASE_URL has the path /dev but PUBLIC_ROOT is empty", result.stdout)

    def test_valid_public_settings_are_accepted(self):
        script = release_copy(self.base / "release", supported=True)
        self.pulls_succeed()
        accepted = (
            {"PUBLIC_ROOT": "/uat", "PUBLIC_BASE_URL": "https://stgissrp.perodua.com.my:8443/uat"},
            {"PUBLIC_ROOT": "/" + "a" * 31, "ENVIRONMENT_LABEL": "x" * 40},
            {"PUBLIC_ROOT": "/0-9", "PUBLIC_BASE_URL": "http://10.1.2.3/0-9"},
            {"PUBLIC_ROOT": "", "PUBLIC_BASE_URL": "https://stgissrp.perodua.com.my"},
            {"PUBLIC_ROOT": "", "PUBLIC_BASE_URL": "http://10.1.2.3:8110", "ENVIRONMENT_LABEL": "UAT"},
            {"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://a-b.c1.Example-2.my/dev"},
            {"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": f"https://{'a' * 63}.{'b' * 63}/dev"},
            {"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "http://intranet/dev"},
        )
        for settings in accepted:
            with self.subTest(**settings):
                self.write_config(**settings)
                result = self.run_script("--non-interactive", script=script)
                self.assertIn("Fixture stopped before", result.stdout)
                saved = (self.deploy_dir / "app.env").read_text()
                for key, value in settings.items():
                    if value:
                        self.assertIn(f"\n{key}={value}\n", saved)
                    else:
                        self.assertNotIn(f"\n{key}=", saved)

    def test_a_path_without_a_base_url_is_warned_about(self):
        # Odoo would record the sign-in address, http:// and without the path.
        script = release_copy(self.base / "release", supported=True)
        self.pulls_succeed()
        warning = "Warning: PUBLIC_ROOT is set without PUBLIC_BASE_URL."
        for settings, warned in (({"PUBLIC_ROOT": "/dev"}, True),
                                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://stgissrp.perodua.com.my/dev"}, False),
                                 ({}, False)):
            with self.subTest(**settings):
                self.write_config(**settings)
                result = self.run_script("--non-interactive", script=script)
                self.assertIn("Fixture stopped before", result.stdout)
                self.assertEqual(warning in result.stdout, warned, result.stdout)

    # ── the sign-in hand-over (v1.0.5): PUBLIC_BASE_URL on the sign-in host ──
    def handover_copy(self, supported):
        """deploy-app.sh and its helpers as a release whose Odoo does (1) or
        does not (0) hand a sign-in on from stgiss to the module host names."""
        script = release_copy(self.base / f"handover-{int(supported)}", supported=True)
        text = script.read_text(encoding="utf-8")
        self.assertEqual(len(HANDOVER_LINE.findall(text)), 1, "deploy-app.sh must set HANDOVER_SUPPORTED once")
        script.write_text(HANDOVER_LINE.sub(f"HANDOVER_SUPPORTED={int(supported)}", text), encoding="utf-8")
        return script

    def test_a_base_url_that_keeps_the_handover_off_is_warned_about(self):
        script = self.handover_copy(True)
        cases = (({}, "Warning: PUBLIC_BASE_URL is empty, so the sign-in hand-over of"),
                 ({"PUBLIC_ROOT": "/dev"}, "Warning: PUBLIC_BASE_URL is empty, so the sign-in hand-over of"),
                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "http://stgiss.perodua.com.my/dev"},
                  "Warning: PUBLIC_BASE_URL does not start with https://, so the sign-in hand-over of"),
                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://stgissrp.perodua.com.my/dev"},
                  "Warning: PUBLIC_BASE_URL names stgissrp.perodua.com.my, not the sign-in host stgiss.perodua.com.my."),
                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://stgiss.perodua.com.my:8443/dev"},
                  "Warning: PUBLIC_BASE_URL has the port 8443, not 443, so the sign-in hand-over of"),
                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://stgiss.perodua.com.my:443/dev"}, None),
                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://STGISS.perodua.com.my/dev"}, None),
                 ({"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://stgiss.perodua.com.my/dev"}, None))
        for settings, warning in cases:
            with self.subTest(**settings):
                self.write_config(**settings)
                # A warning only: the settings are valid, and nothing is changed.
                result = self.run_script("--check-config", script=script)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("Configuration valid", result.stdout)
                self.assertEqual(self.docker_calls(), [])
                if warning:
                    self.assertIn(warning, result.stdout)
                    root = settings.get("PUBLIC_ROOT", "")
                    self.assertIn(f"each host name keeps its own password sign-in. Set PUBLIC_BASE_URL="
                                  f"https://stgiss.perodua.com.my{root}.\n", result.stdout)
                else:
                    self.assertNotIn("hand-over", result.stdout)

    def test_a_deployment_warns_about_the_handover_too(self):
        script = self.handover_copy(True)
        self.pulls_succeed()
        self.write_config(PUBLIC_ROOT="/dev", PUBLIC_BASE_URL="http://stgiss.perodua.com.my/dev")
        result = self.run_script("--non-interactive", script=script)
        self.assertIn("Warning: PUBLIC_BASE_URL does not start with https://", result.stdout)
        self.assertIn("Fixture stopped before", result.stdout)

    def test_a_release_without_the_handover_does_not_warn(self):
        script = self.handover_copy(False)
        for settings in ({}, {"PUBLIC_ROOT": "/dev", "PUBLIC_BASE_URL": "https://stgissrp.perodua.com.my/dev"}):
            with self.subTest(**settings):
                self.write_config(**settings)
                result = self.run_script("--check-config", script=script)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("hand-over", result.stdout)

    def test_the_pinned_release_has_the_handover_from_v1_0_5(self):
        # Moving the pin to v1.0.5 or later must switch HANDOVER_SUPPORTED on.
        text = SCRIPT.read_text(encoding="utf-8")
        version = re.search(r"^RELEASE=client-stable-uiux-v(\d+)\.(\d+)\.(\d+)$", text, re.M)
        self.assertIsNotNone(version)
        supported = tuple(int(part) for part in version.groups()) >= (1, 0, 5)
        self.assertEqual(HANDOVER_LINE.findall(text), [f"HANDOVER_SUPPORTED={int(supported)}"])

    # ── --check-config: what service.sh reset runs before it deletes anything ──
    def test_check_config_checks_the_settings_without_docker(self):
        result = self.run_script("--check-config")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Configuration valid for client-stable-uiux-v", result.stdout)
        self.assertIn("Nothing was changed.", result.stdout)
        self.assertEqual(self.docker_calls(), [])
        self.assertFalse(self.deploy_dir.exists())

    def test_check_config_reads_app_env_in_the_directory(self):
        self.deploy_dir.mkdir()
        (self.deploy_dir / "app.env").write_text(self.config.read_text())
        result = subprocess.run(["bash", str(SCRIPT), "--dir", str(self.deploy_dir), "--init-db", "--check-config"],
                                env=self.env, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(sorted(p.name for p in self.deploy_dir.iterdir()), ["app.env"])
        # Without app.env or --config there is nothing to check, and it never asks.
        (self.deploy_dir / "app.env").unlink()
        result = subprocess.run(["bash", str(SCRIPT), "--dir", str(self.deploy_dir), "--check-config"],
                                env=self.env, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--check-config needs --config or", result.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_check_config_refuses_what_a_deployment_would_refuse(self):
        unsupported = release_copy(self.base / "release", supported=False)
        for settings, script, message in (
                ({"PUBLIC_ROOT": "/dev"}, unsupported, "PUBLIC_ROOT needs Client Stable UIUX v1.0.4 or later"),
                ({"PUBLIC_ROOT": "/Dev"}, SCRIPT, "PUBLIC_ROOT must be empty or one path segment"),
                ({"HTTP_PORT": "99999"}, SCRIPT, "HTTP_PORT exceeds 65535"),
                ({"FUTURE_KEY": "x"}, SCRIPT, "Unknown configuration key: FUTURE_KEY")):
            with self.subTest(**settings):
                self.write_config(**settings)
                result = self.run_script("--check-config", script=script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assert_early_failure(result)

    def test_check_config_refuses_a_password_file_a_deployment_would_refuse(self):
        # service.sh reset deletes the data after this check; the deployment it
        # then runs reads the same file and must not be the first to refuse it.
        for content in ("", "\n", "first\nsecond\n", "pass\rword"):
            with self.subTest(content=content):
                self.password.write_text(content, encoding="utf-8", newline="")
                result = self.run_script("--check-config")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Database password must be nonempty and contain no line breaks", result.stdout)
                self.assertNotIn("Configuration valid", result.stdout)
                self.assertEqual(self.docker_calls(), [])
        # The directory's own secret is read when app.env names no file.
        self.password.write_text("fixture-password\n", encoding="utf-8")
        self.deploy_dir.mkdir()
        (self.deploy_dir / "secrets").mkdir()
        (self.deploy_dir / "secrets" / "db_password").write_text("", encoding="utf-8")
        self.write_config(DB_PASSWORD_FILE="")
        result = self.run_script("--check-config")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("secrets/db_password", result.stdout)

    def test_a_release_without_public_root_support_refuses_it_before_docker(self):
        script = release_copy(self.base / "release", supported=False)
        self.write_config(PUBLIC_ROOT="/dev", PUBLIC_BASE_URL="https://stgissrp.perodua.com.my/dev")
        result = self.run_script("--non-interactive", script=script)
        self.assert_early_failure(result)
        self.assertIn("PUBLIC_ROOT needs Client Stable UIUX v1.0.4 or later", result.stdout)
        # The label alone is only a warning there; the base URL works on any release.
        self.pulls_succeed()
        self.write_config(ENVIRONMENT_LABEL="UAT", PUBLIC_BASE_URL="https://stgissrp.perodua.com.my")
        result = self.run_script("--non-interactive", script=script)
        self.assertIn("Warning: ENVIRONMENT_LABEL has no effect", result.stdout)
        self.assertIn("Fixture stopped before", result.stdout)

    def test_the_pinned_release_supports_public_root_from_v1_0_4(self):
        # Moving the pin to v1.0.4 or later must switch PUBLIC_ROOT_SUPPORTED on.
        version = re.search(r"^RELEASE=client-stable-uiux-v(\d+)\.(\d+)\.(\d+)$",
                            SCRIPT.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(version)
        supported = tuple(int(part) for part in version.groups()) >= (1, 0, 4)
        self.pulls_succeed()
        self.write_config(PUBLIC_ROOT="/dev")
        result = self.run_script("--non-interactive")
        if supported:
            self.assertIn("Fixture stopped before", result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PUBLIC_ROOT needs Client Stable UIUX v1.0.4 or later", result.stdout)
            self.assertEqual(self.docker_calls(), [])

    # ── an app.env written by hand, and a first run that stopped early ──
    # What an operator wrote into /opt/perodua-app/app.env before the first run:
    # the settings of the page only, no database settings.
    HAND_WRITTEN = ("HTTP_PORT=8110\nBIND_IP=127.0.0.1\nPUBLIC_ROOT=/dev\nENVIRONMENT_LABEL=DEV environment\n"
                    "PUBLIC_BASE_URL=https://stgissrp.perodua.com.my/dev\n")

    def hand_written_app_env(self):
        self.deploy_dir.mkdir()
        (self.deploy_dir / "app.env").write_text(self.HAND_WRITTEN, encoding="utf-8")

    def saved_command(self, *extra):
        return ["bash", str(SCRIPT), "--dir", str(self.deploy_dir), *extra]

    def run_saved_command(self, *extra, stdin=subprocess.DEVNULL):
        return subprocess.run(self.saved_command(*extra), env=self.env, text=True, stdin=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)

    def test_a_hand_written_app_env_gets_the_database_questions(self):
        self.hand_written_app_env()
        self.pulls_succeed()
        code, output = self.terminal_exchange([
            (b"Database server IP / hostname []: ", b"192.0.2.20", True),
            (b"Database port [5432]: ", b"", True),
            (b"Application database name [perodua]: ", b"", True),
            (b"Database username [odoo]: ", b"", True),
            (b"Database password (hidden): ", b"fixture-password", False),
        ], command=self.saved_command("--init-db"))
        self.assertNotEqual(code, 0, "Fake Docker must stop before database operations")
        self.assertIn("app.env has no database settings: answer the questions below.", output)
        # Not asked: the file gives the web port and who may open the page.
        self.assertNotIn("Web port that browsers open", output)
        self.assertNotIn("Who may open the web page?", output)
        self.assertIn("Fixture stopped before", output)
        self.assertNotIn("fixture-password", output)
        saved = (self.deploy_dir / "app.env").read_text()
        for line in ("DB_HOST=192.0.2.20", "DB_PORT=5432", "DB_NAME=perodua", "DB_USER=odoo", "HTTP_PORT=8110",
                     "BIND_IP=127.0.0.1", "PUBLIC_ROOT=/dev", "ENVIRONMENT_LABEL=DEV environment",
                     "PUBLIC_BASE_URL=https://stgissrp.perodua.com.my/dev"):
            self.assertIn(line + "\n", saved)
        self.assertEqual((self.deploy_dir / "secrets" / "db_password").read_text(), "fixture-password")
        self.assertIn('"127.0.0.1:8110:80"', (self.deploy_dir / "compose.yml").read_text())
        # It stopped at the database check: nothing has used the database yet.
        self.assertTrue((self.deploy_dir / ".deployment-unverified").exists())
        self.assertIn("before this deployment used the database", output)
        self.assertIn("This App server's addresses: 10.1.2.3 203.0.113.7", output)

    def test_a_missing_db_host_is_named_where_nothing_can_ask(self):
        self.hand_written_app_env()
        for extra in (("--check-config",), ("--non-interactive",), ()):
            with self.subTest(extra=extra):
                result = self.run_saved_command(*extra)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("app.env has no DB_HOST. Add DB_HOST=<IP address of the database server>", result.stdout)
                self.assertNotIn("must be an IP address or hostname", result.stdout)
                self.assertEqual(self.docker_calls(), [])
                self.assertEqual(sorted(p.name for p in self.deploy_dir.iterdir()), ["app.env"])
                self.assertEqual((self.deploy_dir / "app.env").read_text(), self.HAND_WRITTEN)

    def test_a_complete_hand_written_app_env_deploys_without_questions(self):
        self.deploy_dir.mkdir()
        (self.deploy_dir / "app.env").write_text(self.config.read_text())
        self.pulls_succeed()
        result = self.run_saved_command("--non-interactive")
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertNotIn("Use an empty deployment directory", result.stdout)
        self.assertIn("DB_HOST=192.0.2.20\n", (self.deploy_dir / "app.env").read_text())

    def test_an_app_env_next_to_another_configuration_is_still_refused(self):
        # Only the file the run reads may be there; with --config it is not.
        self.hand_written_app_env()
        self.pulls_succeed()
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Use an empty deployment directory", result.stdout)
        self.assertEqual((self.deploy_dir / "app.env").read_text(), self.HAND_WRITTEN)

    def test_an_unverified_first_run_may_change_its_database_settings(self):
        self.pulls_succeed()
        result = self.run_script("--non-interactive")
        self.assertIn("Fixture stopped before", result.stdout)
        identity = self.deploy_dir / ".deployment-identity"
        self.assertIn("host=192.0.2.20\n", identity.read_text())
        # The DB server was another one: the corrected setting is taken.
        self.write_config(DB_HOST="192.0.2.21", DB_NAME="perodua_dev")
        result = self.run_script("--non-interactive")
        self.assertIn("stopped before it used its database, so its settings may still change", result.stdout)
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertIn("host=192.0.2.21\n", identity.read_text())
        self.assertIn("database=perodua_dev\n", identity.read_text())
        self.assertIn("DB_HOST=192.0.2.21\n", (self.deploy_dir / "app.env").read_text())
        # Its Docker network and volume carry the project name: that one stays.
        self.write_config(DB_HOST="192.0.2.21", DB_NAME="perodua_dev", PROJECT_NAME="perodua-other")
        result = self.run_script("--non-interactive")
        self.assertIn("Directory belongs to a different database, project, or release", result.stdout)

    def test_a_marker_that_is_not_a_regular_file_is_neither_written_through_nor_trusted(self):
        outside = self.base / "outside-file"
        outside.write_text("keep")
        self.pulls_succeed()
        # In an empty directory it is not ignored: the run stops, and the target stays.
        self.deploy_dir.mkdir()
        (self.deploy_dir / ".deployment-unverified").symlink_to(outside)
        result = self.run_script("--non-interactive")
        self.assertIn("Use an empty deployment directory", result.stdout)
        self.assertEqual(outside.read_text(), "keep")
        (self.deploy_dir / ".deployment-unverified").unlink()
        # Next to an identity, a symlink does not make the directory unverified.
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive").stdout)
        marker = self.deploy_dir / ".deployment-unverified"
        self.assertTrue(marker.is_file() and not marker.is_symlink())
        marker.unlink()
        marker.symlink_to(outside)
        self.write_config(DB_HOST="192.0.2.21")
        result = self.run_script("--non-interactive")
        self.assertIn("Directory belongs to a different database, project, or release", result.stdout)
        self.assertEqual(outside.read_text(), "keep")

    def test_a_deployment_that_used_its_database_keeps_its_settings(self):
        self.pulls_succeed()
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive").stdout)
        marker = self.deploy_dir / ".deployment-unverified"
        # A missing database is not used yet: without --init-db it stops, still unverified.
        self.env["FAKE_PREFLIGHT_STATE"] = "EMPTY"
        result = self.run_script("--non-interactive")
        self.assertIn("Database is missing or empty", result.stdout)
        self.assertTrue(marker.exists())
        # An initialized database is used from here on (the fixture stops after the check).
        self.env["FAKE_PREFLIGHT_STATE"] = "READY 0"
        result = self.run_script("--non-interactive")
        self.assertIn("Existing initialized database accepted", result.stdout)
        self.assertFalse(marker.exists())
        del self.env["FAKE_PREFLIGHT_STATE"]
        self.write_config(DB_HOST="192.0.2.21")
        result = self.run_script("--non-interactive")
        self.assertIn("Directory belongs to a different database, project, or release", result.stdout)
        self.assertIn("host=192.0.2.20\n", (self.deploy_dir / ".deployment-identity").read_text())
        # A failed check of a bound deployment says so, without the first-run advice.
        result = self.run_saved(SCRIPT)
        self.assertIn("The database check above failed. Fix the cause", result.stdout)
        self.assertNotIn("before this deployment used the database", result.stdout)

    def test_an_unverified_rerun_asks_for_the_password_again(self):
        code, output = self.interactive_run([b"192.0.2.20"], [(b"Choose [2]: ", b"", True)])
        self.assertIn("Fixture stopped before", output)
        secret = self.deploy_dir / "secrets" / "db_password"
        self.assertEqual(secret.read_text(), "fixture-password")
        prompt = b"Database password (hidden; Enter keeps the one entered before): "
        for answer, kept in ((b"corrected-password", "corrected-password"), (b"", "corrected-password")):
            with self.subTest(answer=answer):
                code, output = self.terminal_exchange([(prompt, answer, False)], command=self.saved_command())
                self.assertIn("Fixture stopped before", output)
                self.assertNotIn("corrected-password", output)
                self.assertEqual(secret.read_text(), kept)
        # Without a terminal the saved password is used, as before.
        result = self.run_saved(SCRIPT)
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertNotIn("Database password", result.stdout)
        self.assertEqual(secret.read_text(), "corrected-password")


    # ── outgoing email (v1.2.2): the SMTP settings, --mail and --send-test-mail ──
    MAIL = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_ENCRYPTION": "starttls",
            "SMTP_USER": "sender@example.com", "SMTP_FROM": "sender@example.com",
            "SMTP_FROM_NAME": "Perodua SPD (test)", "MAIL_REDIRECT_TO": "inbox@example.com"}

    def mail_copy(self, supported=True):
        """deploy-app.sh and its helpers as a release whose Odoo image does (1)
        or does not (0) send outgoing email."""
        script = release_copy(self.base / f"mail-{int(supported)}", supported=True)
        text = script.read_text(encoding="utf-8")
        self.assertEqual(len(MAIL_LINE.findall(text)), 1, "deploy-app.sh must set MAIL_SUPPORTED once")
        script.write_text(MAIL_LINE.sub(f"MAIL_SUPPORTED={int(supported)}", text), encoding="utf-8")
        return script

    def smtp_password_file(self, content=SMTP_PASSWORD + "\n"):
        path = self.base / "smtp-password"
        path.write_text(content, encoding="utf-8", newline="")
        path.chmod(0o600)
        return path

    def write_mail_config(self, **updates):
        values = dict(self.MAIL, SMTP_PASSWORD_FILE=str(self.smtp_password_file()))
        values.update(updates)
        self.write_config(**values)

    def assert_smtp_password_kept_secret(self, output=""):
        for call in self.docker_calls():
            for arg in call["args"]:
                self.assertNotIn(SMTP_PASSWORD, arg)
        self.assertNotIn(SMTP_PASSWORD, output)
        for name in ("compose.yml", "app.env", ".deployment-identity"):
            if (self.deploy_dir / name).exists():
                self.assertNotIn(SMTP_PASSWORD, (self.deploy_dir / name).read_text(), name)

    def mode(self, path):
        return path.stat().st_mode & 0o777

    def test_the_pinned_release_sends_mail_from_v1_2_2(self):
        # Moving the pin to v1.2.2 or later must switch MAIL_SUPPORTED on; v1.2.1 has no mail code.
        text = SCRIPT.read_text(encoding="utf-8")
        version = re.search(r"^RELEASE=client-stable-uiux-v(\d+)\.(\d+)\.(\d+)$", text, re.M)
        self.assertIsNotNone(version)
        supported = tuple(int(part) for part in version.groups()) >= (1, 2, 2)
        self.assertEqual(MAIL_LINE.findall(text), [f"MAIL_SUPPORTED={int(supported)}"])

    def test_a_release_without_mail_refuses_the_mail_settings_before_docker(self):
        script = self.mail_copy(supported=False)
        password = self.smtp_password_file()
        for key, value in dict(self.MAIL, SMTP_PASSWORD_FILE=str(password)).items():
            for extra in (("--non-interactive",), ("--check-config",)):
                with self.subTest(key=key, extra=extra):
                    self.write_config(**{key: value})
                    result = self.run_script(*extra, script=script)
                    self.assert_early_failure(result)
                    self.assertIn(f"{key} is set, but outgoing email needs client-stable-uiux v1.2.2 or later", result.stdout)
        self.write_config()
        for extra in (("--mail",), ("--send-test-mail", "inbox@example.com")):
            with self.subTest(extra=extra):
                result = self.run_script(*extra, script=script)
                self.assert_early_failure(result)
                self.assertIn("Outgoing email needs client-stable-uiux v1.2.2 or later", result.stdout)

    def test_mail_settings_are_saved_and_given_to_the_odoo_container(self):
        script = self.mail_copy()
        self.pulls_succeed()
        self.write_mail_config()
        result = self.run_script("--non-interactive", script=script)
        self.assertIn("Fixture stopped before", result.stdout)
        saved = (self.deploy_dir / "app.env").read_text()
        for key, value in self.MAIL.items():
            self.assertIn(f"\n{key}={value}\n", saved)
        self.assertIn(f"\nSMTP_PASSWORD_FILE={self.deploy_dir}/secrets/smtp_password\n", saved)
        compose = (self.deploy_dir / "compose.yml").read_text()
        odoo = service_environment(compose, "odoo")
        self.assertEqual({key: value for key, value in odoo.items() if "MAIL" in key or "SMTP" in key}, {
            "PERODUA_SMTP_HOST": "smtp.example.com", "PERODUA_SMTP_PORT": "587",
            "PERODUA_SMTP_ENCRYPTION": "starttls", "PERODUA_SMTP_USER": "sender@example.com",
            "PERODUA_SMTP_PASSWORD_FILE": "/run/secrets/smtp_password", "PERODUA_SMTP_FROM": "sender@example.com",
            "PERODUA_SMTP_FROM_NAME": "Perodua SPD (test)", "PERODUA_MAIL_REDIRECT_TO": "inbox@example.com"})
        self.assertEqual(odoo["DB_PASSWORD_FILE"], "/run/secrets/db_password")
        self.assertFalse(any("SMTP" in key or "MAIL" in key for key in service_environment(compose, "web")))
        self.assertIn("    secrets: [db_password, smtp_password]\n", compose)
        self.assertIn("secrets:\n  db_password:\n    file: ./secrets/db_password\n"
                      "  smtp_password:\n    file: ./secrets/smtp_password\nvolumes:\n", compose)
        # The secret rules of the database password (and of iss-api.sh).
        secret = self.deploy_dir / "secrets" / "smtp_password"
        self.assertEqual(secret.read_text(), SMTP_PASSWORD)
        self.assertEqual(self.mode(secret), 0o444)
        self.assertEqual(self.mode(secret.parent), 0o700)
        self.assertEqual(self.mode(self.deploy_dir / "app.env"), 0o600)
        self.assertNotIn("smtp", (self.deploy_dir / ".deployment-identity").read_text().lower())
        self.assert_smtp_password_kept_secret(result.stdout)
        # A later run without --config reads app.env, and the saved secret, and writes the same files.
        result = self.run_saved(script)
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertEqual((self.deploy_dir / "app.env").read_text(), saved)
        self.assertEqual((self.deploy_dir / "compose.yml").read_text(), compose)
        self.assertEqual(secret.read_text(), SMTP_PASSWORD)
        self.assert_smtp_password_kept_secret(result.stdout)

    def test_the_encryption_gives_the_default_port_and_a_relay_needs_no_sign_in(self):
        script = self.mail_copy()
        self.pulls_succeed()
        for encryption, port in (("", "587"), ("starttls", "587"), ("ssl", "465"), ("none", "25")):
            with self.subTest(encryption=encryption):
                self.write_config(SMTP_HOST="10.10.0.25", SMTP_ENCRYPTION=encryption,
                                  SMTP_FROM="no-reply@perodua.com.my", MAIL_REDIRECT_TO="off")
                result = self.run_script("--non-interactive", script=script)
                self.assertIn("Fixture stopped before", result.stdout)
                saved = (self.deploy_dir / "app.env").read_text()
                self.assertIn(f"\nSMTP_ENCRYPTION={encryption or 'starttls'}\nSMTP_FROM=", saved)
                self.assertIn(f"\nSMTP_PORT={port}\n", saved)
                self.assertNotIn("SMTP_USER", saved)
                self.assertNotIn("SMTP_PASSWORD_FILE", saved)
                self.assertNotIn("SMTP_FROM_NAME", saved)
                compose = (self.deploy_dir / "compose.yml").read_text()
                odoo = service_environment(compose, "odoo")
                self.assertEqual(odoo["PERODUA_SMTP_PORT"], port)
                self.assertEqual(odoo["PERODUA_MAIL_REDIRECT_TO"], "off")
                for key in ("PERODUA_SMTP_USER", "PERODUA_SMTP_PASSWORD_FILE", "PERODUA_SMTP_FROM_NAME"):
                    self.assertNotIn(key, odoo)
                self.assertIn("    secrets: [db_password]\n", compose)
                self.assertNotIn("smtp_password", compose)
                self.assertFalse((self.deploy_dir / "secrets" / "smtp_password").exists())

    def test_without_mail_settings_app_env_and_compose_stay_as_before(self):
        # Byte for byte what the release without mail writes: the scripts of
        # v1.2.1 and earlier read this app.env, and nothing recreates a
        # container for a compose.yml that did not change.
        self.pulls_succeed()
        written = []
        for script in (SCRIPT, self.mail_copy()):
            with self.subTest(script=script):
                shutil.rmtree(self.deploy_dir, ignore_errors=True)
                self.write_config()
                result = self.run_script("--non-interactive", script=script)
                self.assertIn("Fixture stopped before", result.stdout)
                saved = (self.deploy_dir / "app.env").read_text()
                self.assertEqual({line.partition("=")[0] for line in saved.splitlines()}, self.EARLIER_KEYS)
                compose = (self.deploy_dir / "compose.yml").read_text()
                self.assertNotIn("SMTP", compose)
                self.assertNotIn("smtp", compose)
                self.assertNotIn("MAIL", compose)
                self.assertIn("    secrets: [db_password]\n", compose)
                self.assertFalse((self.deploy_dir / "secrets" / "smtp_password").exists())
                written.append((saved, compose))
        self.assertEqual(written[0], written[1])

    def test_clearing_the_mail_settings_removes_them_and_the_saved_password(self):
        script = self.mail_copy()
        self.pulls_succeed()
        self.write_mail_config()
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive", script=script).stdout)
        self.assertTrue((self.deploy_dir / "secrets" / "smtp_password").exists())
        self.write_config()
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive", script=script).stdout)
        self.assertNotIn("SMTP", (self.deploy_dir / "app.env").read_text())
        self.assertNotIn("smtp", (self.deploy_dir / "compose.yml").read_text().lower())
        self.assertFalse((self.deploy_dir / "secrets" / "smtp_password").exists())
        self.assertTrue((self.deploy_dir / "secrets" / "db_password").exists())

    def test_invalid_mail_settings_fail_before_docker(self):
        script = self.mail_copy()
        marker = self.base / "mail-setting-was-executed"
        relative = "smtp-password"
        cases = [
            *[({"SMTP_HOST": value}, "SMTP_HOST must be a host name or IPv4 address")
              for value in ("a b", "-x.com", "x..com", '"x"', "$x", "x.com.", "a" * 64 + ".com",
                            ".".join(["a" * 50] * 5), f"$(touch {marker})", "smtp.example.com:587")],
            *[({"SMTP_PORT": value}, "SMTP_PORT must be a port number, 1-65535")
              for value in ("0", "65536", "x", "25 ", "-1", "0587")],
            *[({"SMTP_ENCRYPTION": value}, "SMTP_ENCRYPTION must be starttls, ssl or none")
              for value in ("tls", "STARTTLS", "ssl_strict", "starttls_strict")],
            *[({"SMTP_USER": value}, "SMTP_USER must be empty (no sign-in) or at most 254 ASCII characters")
              for value in ("a b", 'a"b', "a\\b", "a$b", "a\tb", "a" * 255, f"`touch {marker}`",
                            "josé@example.com", "a b")],
            *[({"SMTP_FROM": value}, "SMTP_FROM must be a plain email address")
              for value in ("a", "a@", "@b.c", "a b@c.d", "a@b", '"a"@b.c', "Perodua <a@b.c>", "a@b.c,d@e.f",
                            f"$(touch {marker})@b.c")],
            *[({"MAIL_REDIRECT_TO": value}, "MAIL_REDIRECT_TO must be one email address")
              for value in ("a", "a@b", "OFF", "Off", "a@b.c d@e.f", "a@b.c,d@e.f", '"a"@b.c')],
            *[({"SMTP_FROM_NAME": value}, "SMTP_FROM_NAME must be empty (the author's name) or 1-64 letters")
              for value in (" lead", 'a"b', "a$b", "a" * 65, "a\\b", "a;b", "a<b>", f"$(touch {marker})")],
            ({"SMTP_FROM": ""}, "SMTP_FROM is required with SMTP_HOST"),
            ({"MAIL_REDIRECT_TO": ""}, "MAIL_REDIRECT_TO is required with SMTP_HOST"),
            ({"SMTP_ENCRYPTION": "none"}, "SMTP_USER cannot sign in with SMTP_ENCRYPTION=none"),
            ({"SMTP_PASSWORD_FILE": ""}, "SMTP_USER needs SMTP_PASSWORD_FILE"),
            ({"SMTP_PASSWORD_FILE": relative}, "SMTP_PASSWORD_FILE must be an absolute path to a readable file"),
            ({"SMTP_PASSWORD_FILE": str(self.base / "missing")}, "SMTP_PASSWORD_FILE must be an absolute path"),
            ({"SMTP_PASSWORD_FILE": str(self.base)}, "SMTP_PASSWORD_FILE must be an absolute path"),
            ({"SMTP_USER": ""}, "SMTP_PASSWORD_FILE applies only with SMTP_USER"),
        ]
        # Without SMTP_HOST, each other mail setting on its own.
        alone = [(key, value) for key, value in self.MAIL.items() if key != "SMTP_HOST"]
        alone.append(("SMTP_PASSWORD_FILE", str(self.smtp_password_file())))
        cases += [({"SMTP_HOST": "", key: value}, f"{key} applies only with SMTP_HOST") for key, value in alone]
        for updates, message in cases:
            with self.subTest(**updates):
                if updates.get("SMTP_HOST") == "":
                    self.write_config(**updates)
                else:
                    self.write_mail_config(**updates)
                result = self.run_script("--non-interactive", script=script)
                self.assert_early_failure(result)
                self.assertIn(message, result.stdout)
                self.assertNotIn(SMTP_PASSWORD, result.stdout)
        self.assertFalse(marker.exists(), "A mail setting executed a shell command")

    # The table of values that perodua-odoo's entrypoint and this script both
    # accept or refuse (the contract of 2026-10-13).
    HOSTS_ACCEPTED = ("smtp.gmail.com", "relay.perodua.local", "10.10.0.25")
    HOSTS_REFUSED = ("", "a b", "-x.com", "x..com", '"x"', "$x", ".".join(["a" * 63] * 3) + "." + "a" * 62)
    ADDRESSES_ACCEPTED = ("myaicode3@gmail.com", "no-reply@perodua.com.my")
    ADDRESSES_REFUSED = ("a", "a@", "@b.c", "a b@c.d", "a@b", '"a"@b.c', "a@b.c\n")
    NAMES_ACCEPTED = ("Perodua SPD (test)", "Perodua SPD")
    NAMES_REFUSED = (" lead", 'a"b', "a$b", "a" * 65)

    def mail_problem(self, key, value):
        """What mail_problem in deploy-app.sh prints for one value: empty when it is accepted."""
        lines = SCRIPT.read_text(encoding="utf-8").splitlines()
        start = lines.index(next(line for line in lines if line.startswith("mail_problem() {")))
        end = lines.index("}", start)
        pattern = next(line for line in lines if line.startswith("host_pattern="))
        source = "\n".join(["set -Eeuo pipefail", "export LC_ALL=C", pattern, *lines[start:end + 1],
                            'mail_problem "$1" "$2"'])
        result = subprocess.run(["bash", "-c", source, "mail-problem", key, value], text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_the_validation_table_both_repos_share(self):
        self.assertEqual(len(self.HOSTS_REFUSED[-1]), 254)
        self.assertEqual(len(self.NAMES_REFUSED[-1]), 65)
        table = [("SMTP_HOST", self.HOSTS_ACCEPTED, self.HOSTS_REFUSED),
                 ("SMTP_FROM", self.ADDRESSES_ACCEPTED, self.ADDRESSES_REFUSED),
                 ("MAIL_REDIRECT_TO", self.ADDRESSES_ACCEPTED + ("off",), self.ADDRESSES_REFUSED),
                 ("SMTP_FROM_NAME", self.NAMES_ACCEPTED + ("",), self.NAMES_REFUSED)]
        for key, accepted, refused in table:
            for value in accepted:
                with self.subTest(key=key, accepted=value):
                    self.assertEqual(self.mail_problem(key, value), "")
            for value in refused:
                with self.subTest(key=key, refused=value):
                    self.assertTrue(self.mail_problem(key, value).startswith(key + " must be"))
        # The same values through a configuration file, which holds one line per
        # setting (an empty host is no outgoing email there, and a line break
        # ends the value).
        script = self.mail_copy()
        for key, accepted, refused in table:
            for value in accepted:
                with self.subTest(key=key, config_accepted=value):
                    self.write_mail_config(**{key: value})
                    result = self.run_script("--check-config", script=script)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertIn("Configuration valid", result.stdout)
            for value in refused:
                if value == "" or "\n" in value:
                    continue
                with self.subTest(key=key, config_refused=value):
                    self.write_mail_config(**{key: value})
                    result = self.run_script("--check-config", script=script)
                    self.assert_early_failure(result)
                    self.assertIn(key + " must be", result.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_the_sign_in_user_is_ascii(self):
        # Python's SMTP sign-in sends the user as ASCII, and compose.yml must
        # stay valid UTF-8: a byte from 0x80 up stops the run before any file
        # is written.
        for value in ("sender@example.com", "perodua-spd", "DOMAIN/perodua.spd", "a" * 254, ""):
            with self.subTest(accepted=value):
                self.assertEqual(self.mail_problem("SMTP_USER", value), "")
        for value in ("josé@example.com", "a b", "user\udcffname", "a" * 255):
            with self.subTest(refused=value):
                self.assertTrue(self.mail_problem("SMTP_USER", value).startswith("SMTP_USER must be"))
        script = self.mail_copy()
        self.pulls_succeed()
        self.write_mail_config()
        text = self.config.read_bytes()
        self.assertIn(b"\nSMTP_USER=sender@example.com\n", text)
        self.config.write_bytes(text.replace(b"\nSMTP_USER=sender@example.com\n", b"\nSMTP_USER=user\xffname\n"))
        for extra in (("--non-interactive",), ("--check-config",)):
            with self.subTest(extra=extra):
                result = self.run_script(*extra, script=script)
                self.assert_early_failure(result)
                self.assertIn("SMTP_USER must be empty (no sign-in) or at most 254 ASCII characters", result.stdout)
                self.assertFalse(self.deploy_dir.exists())

    def test_check_config_refuses_an_smtp_password_file_a_deployment_would_refuse(self):
        # service.sh reset deletes the data after this check; the deployment it
        # then runs reads the same file and must not be the first to refuse it.
        script = self.mail_copy()
        for content in ("", "\n", "first\nsecond\n", "pass\rword"):
            with self.subTest(content=content):
                self.write_config(**self.MAIL, SMTP_PASSWORD_FILE=str(self.smtp_password_file(content)))
                result = self.run_script("--check-config", script=script)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("SMTP password must be nonempty and contain no line breaks", result.stdout)
                self.assertNotIn("Configuration valid", result.stdout)
                self.assertEqual(self.docker_calls(), [])
        self.write_mail_config()
        result = self.run_script("--check-config", script=script)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(SMTP_PASSWORD, result.stdout)
        # The directory's own secret is read when app.env names no file.
        self.deploy_dir.mkdir()
        (self.deploy_dir / "secrets").mkdir()
        (self.deploy_dir / "secrets" / "smtp_password").write_text("", encoding="utf-8")
        self.write_mail_config(SMTP_PASSWORD_FILE="")
        result = self.run_script("--check-config", script=script)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("SMTP password must be nonempty and contain no line breaks", result.stdout)
        self.assertIn("secrets/smtp_password", result.stdout)

    def test_app_env_is_readable_by_root_only_even_when_written_by_hand(self):
        self.deploy_dir.mkdir()
        app_env = self.deploy_dir / "app.env"
        app_env.write_text(self.config.read_text())
        app_env.chmod(0o644)
        self.pulls_succeed()
        result = self.run_saved_command("--non-interactive")
        self.assertIn("Fixture stopped before", result.stdout)
        self.assertEqual(self.mode(app_env), 0o600)

    def mail_first_run(self, script, mail_answers):
        """A first interactive run of a release with mail: the database
        questions, the web port, who may open the page, then the mail questions."""
        self.pulls_succeed()
        return self.terminal_exchange([
            (b"Database server IP / hostname []: ", b"192.0.2.20", True),
            (b"Database port [5432]: ", b"", True),
            (b"Application database name [perodua]: ", b"", True),
            (b"Database username [odoo]: ", b"", True),
            (b"Web port that browsers open on this server [8110]: ", b"", True),
            (b"Choose [2]: ", b"", True),
            *mail_answers,
            (b"Database password (hidden): ", b"fixture-password", False),
        ], command=["bash", str(script), "--dir", str(self.deploy_dir)])

    def test_a_first_run_asks_the_mail_questions_in_order(self):
        script = self.mail_copy()
        code, output = self.mail_first_run(script, [
            (b"Configure outgoing email now? [y/N]: ", b"y", True),
            (b"SMTP server, host name or IP address (- for none) []: ", b"smtp.example.com", True),
            (b"Choose [1]: ", b"", True),
            (b"SMTP port [587]: ", b"", True),
            (b"SMTP sign-in user (Enter for no sign-in) []: ", b"sender@example.com", True),
            (b"SMTP password (hidden): ", SMTP_PASSWORD.encode(), False),
            (b"Sender address of every email [sender@example.com]: ", b"", True),
            (b"Sender name of every email (Enter for the author's name) []: ", b"Perodua SPD (test)", True),
            (b"Send every email to one test address? [Y/n]: ", b"", True),
            (b"Test address that gets every email []: ", b"inbox@example.com", True),
        ])
        self.assertIn("Fixture stopped before", output)
        self.assertIn("1) STARTTLS, port 587", output)
        self.assertIn("3) None, port 25: an internal relay only, without sign-in", output)
        self.assertIn('Outgoing email: smtp.example.com:587 starttls, sign-in as sender@example.com, from '
                      'sender@example.com as "Perodua SPD (test)", every email to the test address inbox@example.com.',
                      output)
        saved = (self.deploy_dir / "app.env").read_text()
        for key, value in self.MAIL.items():
            self.assertIn(f"\n{key}={value}\n", saved)
        self.assertEqual((self.deploy_dir / "secrets" / "smtp_password").read_text(), SMTP_PASSWORD)
        self.assertEqual(self.mode(self.deploy_dir / "secrets" / "smtp_password"), 0o444)
        self.assertEqual(service_environment((self.deploy_dir / "compose.yml").read_text(), "odoo")["PERODUA_SMTP_USER"],
                         "sender@example.com")
        self.assert_smtp_password_kept_secret(output)

    def test_a_first_run_may_leave_mail_off(self):
        code, output = self.mail_first_run(self.mail_copy(), [
            (b"Configure outgoing email now? [y/N]: ", b"", True)])
        self.assertIn("Fixture stopped before", output)
        self.assertNotIn("SMTP server", output)
        self.assertNotIn("SMTP", (self.deploy_dir / "app.env").read_text())
        self.assertNotIn("smtp", (self.deploy_dir / "compose.yml").read_text().lower())
        # A release without mail does not ask.
        shutil.rmtree(self.deploy_dir)
        code, output = self.interactive_run([b"192.0.2.20"], [(b"Choose [2]: ", b"", True)])
        self.assertIn("Fixture stopped before", output)
        self.assertNotIn("outgoing email", output.lower())

    def test_real_mail_needs_the_typed_confirmation_and_wrong_answers_are_asked_again(self):
        code, output = self.mail_first_run(self.mail_copy(), [
            (b"Configure outgoing email now? [y/N]: ", b"y", True),
            (b"SMTP server, host name or IP address (- for none) []: ", b"bad host", True),
            (b"SMTP server, host name or IP address (- for none) []: ", b"relay.perodua.local", True),
            (b"Choose [1]: ", b"4", True),
            (b"Choose [1]: ", b"3", True),
            (b"SMTP port [25]: ", b"70000", True),
            (b"SMTP port [25]: ", b"", True),
            (b"Sender address of every email []: ", b"", True),
            (b"Sender address of every email []: ", b"a@b", True),
            (b"Sender address of every email []: ", b"no-reply@perodua.com.my", True),
            (b"Sender name of every email (Enter for the author's name) []: ", b"", True),
            (b"Send every email to one test address? [Y/n]: ", b"y", True),
            (b"Test address that gets every email []: ", b"off", True),
            (b"Send every email to one test address? [Y/n]: ", b"n", True),
            (b"Type SEND REAL MAIL to confirm: ", b"send real mail", True),
            (b"Send every email to one test address? [Y/n]: ", b"n", True),
            (b"Type SEND REAL MAIL to confirm: ", b"SEND REAL MAIL", True),
        ])
        self.assertIn("Fixture stopped before", output)
        self.assertIn("SMTP_HOST must be a host name or IPv4 address", output)
        self.assertIn("Enter a number from 1 to 3.", output)
        self.assertIn("SMTP_PORT must be a port number, 1-65535. Try again.", output)
        self.assertIn("An answer is required.", output)
        self.assertIn("SMTP_FROM must be a plain email address", output)
        self.assertIn("off is not an address. To send every email to its real recipients, answer n.", output)
        self.assertIn("The confirmation did not match.", output)
        self.assertIn("No sign-in: without encryption, a password would cross the network as plain text.", output)
        self.assertNotIn("SMTP sign-in user", output)
        self.assertNotIn("SMTP password (hidden", output)
        saved = (self.deploy_dir / "app.env").read_text()
        for line in ("SMTP_HOST=relay.perodua.local", "SMTP_PORT=25", "SMTP_ENCRYPTION=none",
                     "SMTP_FROM=no-reply@perodua.com.my", "MAIL_REDIRECT_TO=off"):
            self.assertIn(f"\n{line}\n", saved)
        for key in ("SMTP_USER", "SMTP_PASSWORD_FILE", "SMTP_FROM_NAME"):
            self.assertNotIn(key, saved)
        odoo = service_environment((self.deploy_dir / "compose.yml").read_text(), "odoo")
        self.assertEqual(odoo["PERODUA_MAIL_REDIRECT_TO"], "off")
        self.assertFalse((self.deploy_dir / "secrets" / "smtp_password").exists())

    def mail_deployment(self, script):
        """A first deployment from a configuration with mail, as --mail later finds it."""
        self.pulls_succeed()
        self.write_mail_config()
        self.assertIn("Fixture stopped before", self.run_script("--non-interactive", script=script).stdout)

    def test_the_mail_option_asks_again_with_the_saved_answers(self):
        script = self.mail_copy()
        self.mail_deployment(script)
        secret = self.deploy_dir / "secrets" / "smtp_password"
        command = ["bash", str(script), "--dir", str(self.deploy_dir), "--mail"]
        # The first run stopped before it used its database: it asks for that password again too.
        database = (b"Database password (hidden; Enter keeps the one entered before): ", b"", False)
        code, output = self.terminal_exchange([
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"", True),
            (b"Choose [1]: ", b"2", True),
            (b"SMTP port [465]: ", b"", True),
            (b"SMTP sign-in user (- for no sign-in) [sender@example.com]: ", b"", True),
            (b"SMTP password (hidden; Enter keeps the saved one): ", b"", False),
            (b"Sender address of every email [sender@example.com]: ", b"", True),
            (b"Sender name of every email (- for the author's name) [Perodua SPD (test)]: ", b"-", True),
            (b"Send every email to one test address? [Y/n]: ", b"", True),
            (b"Test address that gets every email [inbox@example.com]: ", b"other@example.com", True),
            database,
        ], command=command)
        self.assertIn("Fixture stopped before", output)
        saved = (self.deploy_dir / "app.env").read_text()
        for line in ("SMTP_ENCRYPTION=ssl", "SMTP_PORT=465", "MAIL_REDIRECT_TO=other@example.com",
                     f"SMTP_PASSWORD_FILE={self.deploy_dir}/secrets/smtp_password"):
            self.assertIn(f"\n{line}\n", saved)
        self.assertNotIn("SMTP_FROM_NAME", saved)
        self.assertEqual(secret.read_text(), SMTP_PASSWORD)
        odoo = service_environment((self.deploy_dir / "compose.yml").read_text(), "odoo")
        self.assertEqual((odoo["PERODUA_SMTP_ENCRYPTION"], odoo["PERODUA_SMTP_PORT"]), ("ssl", "465"))
        self.assertNotIn("PERODUA_SMTP_FROM_NAME", odoo)
        self.assert_smtp_password_kept_secret(output)
        # A new password replaces the saved one; the same encryption keeps the saved port.
        code, output = self.terminal_exchange([
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"", True),
            (b"Choose [2]: ", b"", True),
            (b"SMTP port [465]: ", b"2465", True),
            (b"SMTP sign-in user (- for no sign-in) [sender@example.com]: ", b"", True),
            (b"SMTP password (hidden; Enter keeps the saved one): ", b"fake-app-password-2", False),
            (b"Sender address of every email [sender@example.com]: ", b"", True),
            (b"Sender name of every email (Enter for the author's name) []: ", b"", True),
            (b"Send every email to one test address? [Y/n]: ", b"", True),
            (b"Test address that gets every email [other@example.com]: ", b"", True),
            database,
        ], command=command)
        self.assertIn("Fixture stopped before", output)
        self.assertNotIn("fake-app-password-2", output)
        self.assertEqual(secret.read_text(), "fake-app-password-2")
        self.assertIn("\nSMTP_PORT=2465\n", (self.deploy_dir / "app.env").read_text())
        for call in self.docker_calls():
            self.assertFalse(any("fake-app-password-2" in arg for arg in call["args"]))

    def assert_no_sign_in_saved(self, output):
        self.assertIn("Fixture stopped before", output)
        self.assertNotIn("applies only with SMTP_USER", output)
        saved = (self.deploy_dir / "app.env").read_text()
        self.assertNotIn("SMTP_USER", saved)
        self.assertNotIn("SMTP_PASSWORD_FILE", saved)
        compose = (self.deploy_dir / "compose.yml").read_text()
        self.assertIn("    secrets: [db_password]\n", compose)
        self.assertNotIn("smtp_password", compose)
        self.assertNotIn("PERODUA_SMTP_USER", service_environment(compose, "odoo"))
        self.assertFalse((self.deploy_dir / "secrets" / "smtp_password").exists())
        self.assertTrue((self.deploy_dir / "secrets" / "db_password").exists())

    def test_the_mail_option_can_remove_the_sign_in(self):
        script = self.mail_copy()
        self.mail_deployment(script)
        self.assertTrue((self.deploy_dir / "secrets" / "smtp_password").exists())
        code, output = self.terminal_exchange([
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"", True),
            (b"Choose [1]: ", b"", True),
            (b"SMTP port [587]: ", b"", True),
            (b"SMTP sign-in user (- for no sign-in) [sender@example.com]: ", b"-", True),
            (b"Sender address of every email [sender@example.com]: ", b"", True),
            (b"Sender name of every email (- for the author's name) [Perodua SPD (test)]: ", b"", True),
            (b"Send every email to one test address? [Y/n]: ", b"", True),
            (b"Test address that gets every email [inbox@example.com]: ", b"", True),
            (b"Database password (hidden; Enter keeps the one entered before): ", b"", False),
        ], command=["bash", str(script), "--dir", str(self.deploy_dir), "--mail"])
        self.assertNotIn("SMTP password (hidden", output)
        self.assertIn('Outgoing email: smtp.example.com:587 starttls, no sign-in, from sender@example.com as '
                      '"Perodua SPD (test)", every email to the test address inbox@example.com.', output)
        self.assert_no_sign_in_saved(output)

    def test_the_mail_option_can_move_to_a_relay_without_encryption(self):
        script = self.mail_copy()
        self.mail_deployment(script)
        self.assertTrue((self.deploy_dir / "secrets" / "smtp_password").exists())
        code, output = self.terminal_exchange([
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"relay.perodua.local", True),
            (b"Choose [1]: ", b"3", True),
            (b"SMTP port [25]: ", b"", True),
            (b"Sender address of every email [sender@example.com]: ", b"", True),
            (b"Sender name of every email (- for the author's name) [Perodua SPD (test)]: ", b"", True),
            (b"Send every email to one test address? [Y/n]: ", b"", True),
            (b"Test address that gets every email [inbox@example.com]: ", b"", True),
            (b"Database password (hidden; Enter keeps the one entered before): ", b"", False),
        ], command=["bash", str(script), "--dir", str(self.deploy_dir), "--mail"])
        self.assertNotIn("SMTP sign-in user", output)
        self.assertIn("Outgoing email: relay.perodua.local:25 none, no sign-in, from sender@example.com", output)
        self.assert_no_sign_in_saved(output)
        odoo = service_environment((self.deploy_dir / "compose.yml").read_text(), "odoo")
        self.assertEqual((odoo["PERODUA_SMTP_HOST"], odoo["PERODUA_SMTP_ENCRYPTION"], odoo["PERODUA_SMTP_PORT"]),
                         ("relay.perodua.local", "none", "25"))

    def test_a_typed_smtp_password_keeps_its_spaces(self):
        # A password file keeps leading and trailing spaces; each password
        # question must keep them too.
        script = self.mail_copy()
        secret = self.deploy_dir / "secrets" / "smtp_password"
        self.pulls_succeed()
        # A configuration with a user and no SMTP_PASSWORD_FILE, on a terminal.
        self.write_config(**self.MAIL)
        code, output = self.terminal_exchange([
            (b"SMTP password (hidden): ", b" fake-app-password ", False),
        ], command=self.command(script=script))
        self.assertIn("Fixture stopped before", output)
        self.assertEqual(secret.read_text(), " fake-app-password ")
        mail = ["bash", str(script), "--dir", str(self.deploy_dir), "--mail"]
        asked = [
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"", True),
            (b"Choose [1]: ", b"", True),
            (b"SMTP port [587]: ", b"", True),
        ]
        answered = [
            (b"Sender address of every email [sender@example.com]: ", b"", True),
            (b"Sender name of every email (- for the author's name) [Perodua SPD (test)]: ", b"", True),
            (b"Send every email to one test address? [Y/n]: ", b"", True),
            (b"Test address that gets every email [inbox@example.com]: ", b"", True),
            (b"Database password (hidden; Enter keeps the one entered before): ", b"", False),
        ]
        # --mail with a saved password.
        code, output = self.terminal_exchange([
            *asked,
            (b"SMTP sign-in user (- for no sign-in) [sender@example.com]: ", b"", True),
            (b"SMTP password (hidden; Enter keeps the saved one): ", b"  fake-app-password-2\t ", False),
            *answered,
        ], command=mail)
        self.assertIn("Fixture stopped before", output)
        self.assertEqual(secret.read_text(), "  fake-app-password-2\t ")
        # --mail without a saved password: the sign-in removed, then added again.
        code, output = self.terminal_exchange([
            *asked,
            (b"SMTP sign-in user (- for no sign-in) [sender@example.com]: ", b"-", True),
            *answered,
        ], command=mail)
        self.assertIn("Fixture stopped before", output)
        self.assertFalse(secret.exists())
        code, output = self.terminal_exchange([
            *asked,
            (b"SMTP sign-in user (Enter for no sign-in) []: ", b"sender@example.com", True),
            (b"SMTP password (hidden): ", b"fake-app-password-3 ", False),
            *answered,
        ], command=mail)
        self.assertIn("Fixture stopped before", output)
        self.assertEqual(secret.read_text(), "fake-app-password-3 ")
        for call in self.docker_calls():
            self.assertFalse(any(SMTP_PASSWORD in arg for arg in call["args"]))

    def test_the_mail_option_can_switch_mail_off(self):
        script = self.mail_copy()
        self.mail_deployment(script)
        code, output = self.terminal_exchange([
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"-", True),
            (b"Switch outgoing email off? [y/N]: ", b"", True),
            (b"SMTP server, host name or IP address (- for none) [smtp.example.com]: ", b"-", True),
            (b"Switch outgoing email off? [y/N]: ", b"y", True),
            (b"Database password (hidden; Enter keeps the one entered before): ", b"", False),
        ], command=["bash", str(script), "--dir", str(self.deploy_dir), "--mail"])
        self.assertIn("Outgoing email is off.", output)
        self.assertIn("Fixture stopped before", output)
        saved = (self.deploy_dir / "app.env").read_text()
        self.assertEqual({line.partition("=")[0] for line in saved.splitlines()}, self.EARLIER_KEYS)
        self.assertNotIn("smtp", (self.deploy_dir / "compose.yml").read_text().lower())
        self.assertFalse((self.deploy_dir / "secrets" / "smtp_password").exists())

    def test_the_mail_option_needs_a_terminal_and_a_plain_run(self):
        script = self.mail_copy()
        self.write_config()
        for extra, message in ((("--mail",), "--mail asks questions and requires a terminal"),
                               (("--mail", "--non-interactive"), "--mail asks the outgoing email questions"),
                               (("--mail", "--check-config"), "--mail asks the outgoing email questions"),
                               (("--mail", "--send-test-mail", "inbox@example.com"), "--mail asks the outgoing email questions"),
                               (("--mail", "--upgrade"), "--upgrade moves an existing deployment")):
            with self.subTest(extra=extra):
                result = subprocess.run(self.command(*extra, script=script), env=self.env, text=True,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, timeout=20)
                self.assert_early_failure(result)
                self.assertIn(message, result.stdout)

    def bound_mail_deployment(self, script, **updates):
        """A deployment with mail that has used its database (READY), as
        --send-test-mail needs it."""
        self.pulls_succeed()
        if updates.pop("mail", True):
            self.write_mail_config(**updates)
        else:
            self.write_config(**updates)
        self.env["FAKE_PREFLIGHT_STATE"] = "READY 0"
        result = self.run_script("--non-interactive", script=script)
        self.assertIn("Existing initialized database accepted", result.stdout)
        del self.env["FAKE_PREFLIGHT_STATE"]
        self.assertFalse((self.deploy_dir / ".deployment-unverified").exists())
        self.calls.unlink()

    def send_test_mail(self, script, *extra, address="you@example.com"):
        return subprocess.run(["bash", str(script), "--dir", str(self.deploy_dir), "--send-test-mail", address, *extra],
                              env=self.env, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=20)

    def test_send_test_mail_runs_only_the_image_helper(self):
        script = self.mail_copy()
        self.bound_mail_deployment(script)
        result = self.send_test_mail(script)
        self.assertEqual(result.returncode, 0, result.stdout)
        compose_calls = [call["args"] for call in self.docker_calls() if call["args"][:1] == ["compose"]]
        self.assertEqual(compose_calls[-1], [
            "compose", "--project-name", "perodua-fixture", "--file", str(self.deploy_dir / "compose.yml"),
            "run", "--rm", "--no-deps", "-T", "odoo", "perodua-mail-test", "you@example.com"])
        self.assertEqual([args for args in compose_calls if "run" in args], compose_calls[-1:])
        self.assertFalse(any(call["args"][:1] == ["pull"] for call in self.docker_calls()))
        self.assertIn("fake perodua-mail-test: one test email to you@example.com", result.stdout)
        self.assertIn("MAIL_REDIRECT_TO sends it to the test address inbox@example.com, with [TEST for you@example.com]"
                      " before its subject", result.stdout)
        self.assert_smtp_password_kept_secret(result.stdout)
        # A failure of the helper is reported.
        self.env["FAKE_MAIL_TEST_EXIT"] = "1"
        result = self.send_test_mail(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The test email was not sent, for the reason above", result.stdout)

    def test_send_test_mail_to_the_real_recipient(self):
        script = self.mail_copy()
        self.bound_mail_deployment(script, MAIL_REDIRECT_TO="off")
        result = self.send_test_mail(script)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Test email sent to you@example.com. Check that it arrives.", result.stdout)

    def test_send_test_mail_runs_on_its_own_on_a_deployment_with_mail(self):
        script = self.mail_copy()
        for address in ("you", "a@b", "you@example.com,other@example.com", '"you"@example.com'):
            with self.subTest(address=address):
                self.write_config()
                self.deploy_dir.mkdir(exist_ok=True)
                (self.deploy_dir / ".deployment-identity").write_text("release=x\n")
                result = self.send_test_mail(script, "--config", str(self.config), address=address)
                self.assert_early_failure(result)
                self.assertIn("The address of --send-test-mail must be one email address", result.stdout)
        shutil.rmtree(self.deploy_dir)
        result = self.send_test_mail(script)
        self.assert_early_failure(result)
        self.assertIn("--send-test-mail needs a deployment", result.stdout)
        self.bound_mail_deployment(script)
        for extra in (("--init-db",), ("--check-config",), ("--release-queued-mail",), ("--upgrade",)):
            with self.subTest(extra=extra):
                result = self.send_test_mail(script, *extra)
                self.assert_early_failure(result)
                self.assertIn("--send-test-mail runs on its own", result.stdout)
        # Without outgoing email it says how to configure it, and runs nothing.
        shutil.rmtree(self.deploy_dir)
        self.bound_mail_deployment(script, mail=False)
        result = self.send_test_mail(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Outgoing email is off in", result.stdout)
        self.assertIn("deploy-app.sh --mail --dir", result.stdout)
        self.assertFalse(any("run" in call["args"] for call in self.docker_calls()))
        # A first deployment that never used its database has nothing to send with.
        shutil.rmtree(self.deploy_dir)
        self.mail_deployment(script)
        self.calls.unlink()
        result = self.send_test_mail(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped before it used its database", result.stdout)
        self.assertFalse(any("run" in call["args"] for call in self.docker_calls()))


if __name__ == "__main__":
    unittest.main()
