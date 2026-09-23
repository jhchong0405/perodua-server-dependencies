"""deploy-db.sh guided setup: answers on a terminal create deploy.conf.

The real script runs on a pseudo-terminal with --check-config, so nothing is
deployed and PostgreSQL is not needed (pg_conftool is a stub). The question
about this server's internal IP needs one real non-loopback IPv4 address.
"""

import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def local_ipv4():
    try:
        addresses = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                                   timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return next((a for a in addresses if "." in a and not a.startswith(("127.", "169.254."))), None)


LOCAL = local_ipv4()


@unittest.skipUnless(LOCAL, "needs a non-loopback IPv4 address on this host")
class GuidedSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="perodua-guided-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.script = base / "deploy-db.sh"
        shutil.copy(ROOT / "deploy-db.sh", self.script)
        self.config = base / "deploy.conf"
        stubs = base / "bin"
        stubs.mkdir()
        self.pg_conftool = stubs / "pg_conftool"
        self.pg_conftool.write_text('#!/bin/sh\necho "port = 5433"\n')
        self.pg_conftool.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}")

    def converse(self, answers, *args):
        """Run on a PTY, answer each prompt in turn, return (exit code, output)."""
        pid, master = pty.fork()
        if pid == 0:
            os.execvpe("bash", ["bash", str(self.script), "--check-config", *args], self.env)
        output, cursor, status = b"", 0, None
        deadline = time.monotonic() + 30

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

    def settings(self):
        return [line for line in self.config.read_text().splitlines() if not line.startswith("#")]

    def test_answers_create_a_valid_fresh_uat_configuration(self):
        code, output = self.converse([
            (b"Choose [1]: ", b""),
            (b"which the App server connects to", b"192.0.2.10"),  # not an address of this host
            (b"which the App server connects to", b""),  # the offered default
            (b"only it may connect): ", b"not-an-address"),
            (b"only it may connect): ", b"10.0.0.10"),
            (b"and continue? [Y/n]: ", b""),
        ])
        self.assertEqual(code, 0, output)
        self.assertIn(LOCAL, output)
        self.assertIn("192.0.2.10 is not an address of this server", output)
        self.assertIn("Enter the IPv4 address of the App server", output)
        self.assertIn("Configuration valid", output)
        self.assertEqual(self.settings(), [
            "DB_MODE=empty", "DB_NAME=perodua", "DB_USER=odoo", "PG_PORT=5433",
            f"DB_LISTEN_IP={LOCAL}", "APP_CIDR=10.0.0.10/32",
        ])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        # A later run reads the saved answers and asks nothing, even without a terminal.
        again = subprocess.run(["bash", str(self.script), "--check-config"], env=self.env, text=True,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                               start_new_session=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Configuration valid", again.stdout)
        self.assertNotIn("Choose", again.stdout + again.stderr)

    def test_a_network_can_be_given_instead_of_one_app_address(self):
        code, output = self.converse([
            (b"Choose [1]: ", b"1"),
            (b"which the App server connects to", LOCAL.encode()),
            (b"only it may connect): ", b"10.0.0.0/24"),
            (b"and continue? [Y/n]: ", b"y"),
        ])
        self.assertEqual(code, 0, output)
        self.assertIn("APP_CIDR=10.0.0.0/24", self.settings())

    def test_choosing_a_restore_saves_nothing_and_explains(self):
        code, output = self.converse([(b"Choose [1]: ", b"2")])
        self.assertNotEqual(code, 0)
        self.assertIn("deploy.conf.example", output)
        self.assertFalse(self.config.exists())

    def test_declining_the_summary_saves_nothing(self):
        code, output = self.converse([
            (b"Choose [1]: ", b""),
            (b"which the App server connects to", b""),
            (b"only it may connect): ", b"10.0.0.10"),
            (b"and continue? [Y/n]: ", b"n"),
        ])
        self.assertNotEqual(code, 0)
        self.assertIn("Nothing was saved or changed", output)
        self.assertFalse(self.config.exists())

    def test_without_a_terminal_it_explains_instead_of_asking(self):
        result = subprocess.run(["bash", str(self.script), "--check-config"], env=self.env, text=True,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                start_new_session=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deploy.conf.example", result.stderr)
        self.assertFalse(self.config.exists())

    def test_an_explicit_config_is_never_guided(self):
        code, output = self.converse([], "--config", str(self.config))
        self.assertNotEqual(code, 0)
        self.assertIn("Configuration is not a regular", output)
        self.assertNotIn("Choose", output)
        self.assertFalse(self.config.exists())

    def test_missing_postgresql_points_to_the_installer(self):
        self.pg_conftool.write_text("#!/bin/sh\nexit 1\n")
        code, output = self.converse([])
        self.assertNotEqual(code, 0)
        self.assertIn("install-dependencies.sh --role db", output)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
