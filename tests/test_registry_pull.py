"""Registry regressions using a strict fake Docker executable, never a daemon.

Success in these tests means reaching the deliberately blocked DB preflight.
The tests prove cache selection, pull outcomes, retries and diagnostic handling;
they do not substitute for an actual registry/download deployment test.
"""

import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time
import unittest

import test_deploy_app as safety


IMAGE_SETTINGS = dict(re.findall(
    r"^(ODOO_IMAGE|WEB_IMAGE)=(.+)$", safety.SCRIPT.read_text(), re.MULTILINE,
))
ODOO_IMAGE = IMAGE_SETTINGS["ODOO_IMAGE"]
WEB_IMAGE = IMAGE_SETTINGS["WEB_IMAGE"]


class RegistryPullTests(unittest.TestCase):
    setUp = safety.DeploymentSafetyTests.setUp
    write_config = safety.DeploymentSafetyTests.write_config
    command = safety.DeploymentSafetyTests.command
    run_script = safety.DeploymentSafetyTests.run_script
    docker_calls = safety.DeploymentSafetyTests.docker_calls
    terminal_exchange = safety.DeploymentSafetyTests.terminal_exchange
    assert_original_auth_unchanged = safety.DeploymentSafetyTests.assert_original_auth_unchanged

    @staticmethod
    def fake_docker_source():
        return r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
state_path = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
spec = json.loads(pathlib.Path(os.environ['REGISTRY_FIXTURE']).read_text())
state = json.loads(state_path.read_text()) if state_path.exists() else {'pulls': {}, 'authenticated': False}
entry = {'args': args, 'config': os.environ.get('DOCKER_CONFIG'),
         'host': os.environ.get('DOCKER_HOST'), 'context': os.environ.get('DOCKER_CONTEXT')}
with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(entry) + '\n')
def respond(result):
    print(result.get('text', ''), file=sys.stderr, flush=True)
    if result.get('hold_until'):
        deadline = time.monotonic() + 8
        while not pathlib.Path(result['hold_until']).exists():
            if time.monotonic() >= deadline:
                print('Fixture timed out waiting for test observer', file=sys.stderr); sys.exit(95)
            time.sleep(0.01)
    sys.exit(result.get('code', 0))
if args[:2] == ['compose', 'version']:
    print('Docker Compose version v2.39.4'); sys.exit(0)
if args and args[0] == 'info':
    if '--format' in args: print('linux/x86_64')
    sys.exit(0)
if args and args[0] == 'ps': sys.exit(0)
if len(args) > 1 and args[0] in ('volume', 'network') and args[1] == 'ls': sys.exit(0)
if args[:2] == ['context', 'inspect']:
    print('unix:///var/run/docker.sock'); sys.exit(0)
if args[:2] == ['image', 'inspect']:
    if args[2] in spec.get('cached', []):
        print('[{"Id":"sha256:fixture-local-image"}]'); sys.exit(0)
    print('Error response from daemon: No such image: ' + args[2], file=sys.stderr); sys.exit(1)
if args and args[0] == 'pull':
    image = args[-1]
    count = state['pulls'].get(image, 0)
    state['pulls'][image] = count + 1
    state_path.write_text(json.dumps(state))
    if spec.get('requires_auth') and not state['authenticated']:
        respond({'code': 1, 'text': 'unauthorized: authentication required'})
    options = spec.get('pulls', {}).get(image, [{'code': 0, 'text': 'Status: Downloaded newer image for ' + image}])
    respond(options[min(count, len(options) - 1)])
if args and args[0] == 'login':
    token = sys.stdin.read()
    if '--password-stdin' not in args: sys.exit(94)
    with open(str(state_path) + '.tokens', 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(token) + '\n')
    if spec.get('echo_login_token'):
        print('Diagnostic echo of supplied credential: ' + token, flush=True)
    if token == 'fixture-valid-token':
        state['authenticated'] = True
        state_path.write_text(json.dumps(state))
        print('Login Succeeded'); sys.exit(0)
    print('unauthorized: fixture token rejected', file=sys.stderr); sys.exit(1)
if args and args[0] == 'compose' and 'config' in args: sys.exit(0)
if args and args[0] == 'compose' and 'run' in args:
    print('Fixture stopped before any database or container operation', file=sys.stderr)
    sys.exit(92)
print('Fake Docker rejected unexpected command: ' + ' '.join(args), file=sys.stderr)
sys.exit(93)
'''

    def configure(self, **spec):
        path = self.base / "registry-fixture.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        self.env["REGISTRY_FIXTURE"] = str(path)
        # Isolate host settings too; this contract is for default local Ubuntu.
        self.env.pop("DOCKER_HOST", None)
        self.env.pop("DOCKER_CONTEXT", None)

    def calls_of(self, operation):
        return [call for call in self.docker_calls() if call["args"][0] == operation]

    def log_files(self):
        root = Path(str(self.deploy_dir) + ".logs")
        return sorted(root.glob("run-*/*.log"))

    def log_text(self):
        return "\n".join(path.read_text() for path in self.log_files())

    def sleep_calls(self):
        if not self.sleep_log.exists():
            return []
        return [json.loads(line) for line in self.sleep_log.read_text().splitlines()]

    def assert_reached_db_barrier(self, result):
        self.assertNotEqual(result.returncode, 0, "Fake must never deploy")
        self.assertIn("Fixture stopped before any database or container operation", result.stdout)
        self.assertNotIn("Fake Docker rejected unexpected command", result.stdout)

    def assert_pull_count(self, image, count):
        self.assertEqual(sum(call["args"][-1] == image for call in self.calls_of("pull")), count)

    def assert_no_login(self):
        self.assertEqual(self.calls_of("login"), [])

    def assert_exit_reported(self, output, code):
        self.assertRegex(output, rf"(?i)(exit(?:\s+code)?|status|code)[^\d\n]*{code}\b")

    def test_exact_pinned_cache_skips_both_pulls_and_login(self):
        self.configure(cached=[ODOO_IMAGE, WEB_IMAGE])
        result = self.run_script("--non-interactive")
        self.assert_reached_db_barrier(result)
        self.assertEqual(self.calls_of("pull"), [])
        self.assert_no_login()
        inspections = [call["args"][2] for call in self.calls_of("image")]
        self.assertEqual(inspections, [ODOO_IMAGE, WEB_IMAGE])
        self.assertTrue(all("@sha256:" in image for image in inspections))

    def test_cached_backend_only_pulls_web(self):
        self.configure(cached=[ODOO_IMAGE])
        result = self.run_script("--non-interactive")
        self.assert_reached_db_barrier(result)
        self.assert_pull_count(ODOO_IMAGE, 0)
        self.assert_pull_count(WEB_IMAGE, 1)
        self.assert_no_login()

    def test_same_tag_or_wrong_digest_does_not_count_as_pinned_cache(self):
        self.configure(cached=[ODOO_IMAGE.split("@")[0], ODOO_IMAGE.split("@")[0] + "@sha256:" + "0" * 64,
                               WEB_IMAGE.split("@")[0]])
        result = self.run_script("--non-interactive")
        self.assert_reached_db_barrier(result)
        self.assert_pull_count(ODOO_IMAGE, 1)
        self.assert_pull_count(WEB_IMAGE, 1)

    def test_backend_success_web_failure_preserves_separate_logs_and_exit(self):
        error = "failed to register layer: fixture-unpack-failure"
        self.configure(pulls={WEB_IMAGE: [{"code": 42, "text": error}]})
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assert_pull_count(ODOO_IMAGE, 1)
        self.assert_pull_count(WEB_IMAGE, 1)
        self.assertIn(error, result.stdout)
        self.assertIn("Web", result.stdout)
        self.assertIn(WEB_IMAGE.split("@")[0], result.stdout)
        self.assert_exit_reported(result.stdout, 42)
        logs = self.log_files()
        self.assertGreaterEqual(len(logs), 2)
        backend_logs = [path for path in logs if "Status: Downloaded newer image for " + ODOO_IMAGE in path.read_text()]
        web_logs = [path for path in logs if error in path.read_text()]
        self.assertEqual(len(backend_logs), 1)
        self.assertEqual(len(web_logs), 1)
        self.assertNotEqual(backend_logs[0], web_logs[0])
        self.assertIn(str(logs[0].parent), result.stdout)
        self.assertFalse([path for path in self.deploy_dir.glob(".deploy.*") if path.is_dir()])
        self.assertEqual(self.sleep_calls(), [])

    def test_transient_failure_recovers_after_one_retry(self):
        self.configure(pulls={ODOO_IMAGE: [
            {"code": 17, "text": "read tcp: connection reset by peer"},
            {"code": 0, "text": "Status: Downloaded newer image for " + ODOO_IMAGE},
        ]})
        result = self.run_script("--non-interactive")
        self.assert_reached_db_barrier(result)
        self.assert_pull_count(ODOO_IMAGE, 2)
        self.assert_pull_count(WEB_IMAGE, 1)
        self.assertEqual(self.sleep_calls(), [["3"]])
        self.assertIn("connection reset by peer", self.log_text())
        self.assertGreaterEqual(len(self.log_files()), 3)
        self.assert_no_login()

    def test_progress_is_visible_and_logged_before_docker_pull_finishes(self):
        release = self.base / "release-fixture-pull"
        marker = "fixture live layer download progress"
        self.configure(pulls={ODOO_IMAGE: [{
            "code": 0, "text": marker, "hold_until": str(release),
        }]})
        process = subprocess.Popen(
            self.command("--non-interactive"), env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        output = b""
        try:
            deadline = time.monotonic() + 5
            while marker.encode() not in output and time.monotonic() < deadline:
                readable, _, _ = select.select([process.stdout], [], [], 0.05)
                if readable:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output += chunk
            self.assertIn(marker, output.decode(errors="replace"))
            self.assertIsNone(process.poll(), "The fixture must still be downloading")
            self.assertIn(marker, self.log_text(), "Progress should be logged immediately")
            release.write_text("continue", encoding="utf-8")
            remainder, _ = process.communicate(timeout=10)
            result = subprocess.CompletedProcess(
                process.args, process.returncode, (output + remainder).decode(errors="replace"),
            )
            self.assert_reached_db_barrier(result)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)

    def test_transient_failure_stops_after_three_attempts(self):
        error = "unexpected status from HEAD request: 503 Service Unavailable"
        self.configure(pulls={ODOO_IMAGE: [{"code": 27, "text": error}]})
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assert_pull_count(ODOO_IMAGE, 3)
        self.assert_pull_count(WEB_IMAGE, 0)
        self.assertEqual(self.sleep_calls(), [["3"], ["3"]])
        self.assertEqual(sum(error in path.read_text() for path in self.log_files()), 3)
        self.assert_exit_reported(result.stdout, 27)
        self.assert_no_login()

    def test_permanent_errors_neither_retry_nor_request_registry_login(self):
        errors = [
            "failed to register layer: no space left on device",
            "tls: failed to verify certificate: x509: certificate signed by unknown authority",
            "manifest unknown: manifest unknown",
            "open /var/lib/docker/tmp/download401: permission denied",
            "failed to register layer sha256:aa401bb403cc: operation not permitted",
        ]
        for error in errors:
            with self.subTest(error=error):
                self.configure(pulls={ODOO_IMAGE: [{"code": 31, "text": error}]})
                before = len(self.calls_of("pull"))
                result = self.run_script("--non-interactive")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.calls_of("pull")) - before, 1)
                self.assertIn(error, result.stdout)
                self.assert_exit_reported(result.stdout, 31)
                self.assertNotIn("GHCR authentication is required", result.stdout)
                self.assertEqual(self.sleep_calls(), [])
                self.assert_no_login()

    def test_http_auth_status_reports_authentication_without_network_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                error = f"unexpected status from HEAD request: {status} Forbidden"
                self.configure(pulls={ODOO_IMAGE: [{"code": 1, "text": error}]})
                before = len(self.calls_of("pull"))
                result = self.run_script("--non-interactive")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("GHCR authentication", result.stdout)
                self.assertEqual(len(self.calls_of("pull")) - before, 1)
                self.assertEqual(self.sleep_calls(), [])
                self.assert_no_login()

    def test_docker_exit_code_wins_over_successful_tee_and_success_text(self):
        self.configure(pulls={ODOO_IMAGE: [{
            "code": 47, "text": "Status: Downloaded newer image for " + ODOO_IMAGE + "\nfixture post-download failure",
        }]})
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assert_exit_reported(result.stdout, 47)
        self.assert_pull_count(WEB_IMAGE, 0)
        self.assertNotIn("Fixture stopped before", result.stdout)

    def test_zero_exit_with_error_words_is_not_reclassified_as_failure(self):
        self.configure(pulls={ODOO_IMAGE: [{"code": 0, "text": "fixture note: previous timeout / 401 is resolved"}]})
        result = self.run_script("--non-interactive")
        self.assert_reached_db_barrier(result)
        self.assert_pull_count(ODOO_IMAGE, 1)
        self.assertEqual(self.sleep_calls(), [])
        self.assert_no_login()

    def test_logging_failure_cannot_be_reported_as_successful_pull(self):
        self.configure()
        fake_tee = self.bin / "tee"
        fake_tee.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "sys.stdout.write(sys.stdin.read())\n"
            "print('fixture tee failure: no space left on device', file=sys.stderr)\n"
            "sys.exit(73)\n", encoding="utf-8",
        )
        fake_tee.chmod(0o755)
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assert_pull_count(ODOO_IMAGE, 1)
        self.assert_pull_count(WEB_IMAGE, 0)
        self.assertNotIn("Fixture stopped before", result.stdout)
        self.assertRegex(result.stdout.lower(), "log|tee")

    def test_failed_pull_logs_do_not_block_next_run(self):
        self.configure(pulls={ODOO_IMAGE: [{"code": 31, "text": "fixture pull failure"}]})
        failed = self.run_script("--non-interactive")
        self.assertNotEqual(failed.returncode, 0)
        old_logs = self.log_files()
        self.assertTrue(old_logs)
        self.configure()
        result = self.run_script("--non-interactive")
        self.assert_reached_db_barrier(result)
        self.assertNotIn("Use an empty deployment directory", result.stdout)
        self.assertTrue(all(path.exists() for path in old_logs))
        self.assertGreater(len({path.parent for path in self.log_files()}), 1)

    def test_hidden_login_redacts_echo_and_removes_temporary_credentials(self):
        self.configure(requires_auth=True, echo_login_token=True)
        prompts = [
            (b"GitHub username", b"fixture-user", True),
            (b"GHCR token", b"fixture-invalid-token", False),
            (b"GitHub username", b"fixture-user", True),
            (b"GHCR token", b"fixture-valid-token", False),
        ]
        code, output = self.terminal_exchange(prompts)
        self.assertNotEqual(code, 0)
        self.assertIn("Login failed", output)
        self.assertIn("Login Succeeded", output)
        self.assertIn("Fixture stopped before", output)
        self.assertTrue(self.log_files())
        for secret in ("fixture-invalid-token", "fixture-valid-token"):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, self.log_text())
        logins = self.calls_of("login")
        self.assertEqual(len(logins), 2)
        for call in logins:
            self.assertIn("--password-stdin", call["args"])
            self.assertNotEqual(call["config"], str(self.auth))
            self.assertFalse(Path(call["config"]).exists())
            self.assertEqual(call["host"], "unix:///var/run/docker.sock")
            self.assertIsNone(call["context"])
        for call in self.calls_of("pull")[-2:]:
            self.assertEqual(call["config"], logins[-1]["config"])
        self.assert_original_auth_unchanged()
        self.assertFalse([path for path in self.deploy_dir.glob(".deploy.*") if path.is_dir()])

    def test_pull_diagnostics_redact_registry_tokens_and_authorization(self):
        secrets = ["ghp_" + "x" * 36, "fixture-bearer-credential", "fixture-query-credential"]
        error = ("pull failed; token=" + secrets[0] + "\n"
                 "Authorization: Bearer " + secrets[1] + "\n"
                 "https://ghcr.io/v2/path?token=" + secrets[2])
        self.configure(pulls={ODOO_IMAGE: [{"code": 31, "text": error}]})
        result = self.run_script("--non-interactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.log_files())
        for secret in secrets:
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, self.log_text())
        self.assertIn("pull failed", result.stdout)


if __name__ == "__main__":
    unittest.main()
