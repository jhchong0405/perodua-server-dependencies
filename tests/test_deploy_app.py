"""Deployment command safety checks. Docker is always replaced by a local fake.

Run on Linux/WSL: python3 -m unittest discover -s tests -v
These are process-level failure/interactive tests, not a registry or Odoo acceptance test.
"""

import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import tempfile
import termios
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "deploy-app.sh"


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

    def command(self, *extra):
        return ["bash", str(SCRIPT), "--config", str(self.config),
                "--dir", str(self.deploy_dir), *extra]

    def run_script(self, *extra):
        return subprocess.run(self.command(*extra), env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=20)

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


if __name__ == "__main__":
    unittest.main()
