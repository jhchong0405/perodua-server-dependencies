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
    if args and args[0] == 'compose' and 'run' in args:
        # FAKE_PREFLIGHT_STATE: the database check answers with this state,
        # and the fixture stops at the next Docker command instead.
        if os.environ.get('FAKE_PREFLIGHT_STATE') and args[-2:] == ['/opt/deploy/preflight.py', 'check']:
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


if __name__ == "__main__":
    unittest.main()
