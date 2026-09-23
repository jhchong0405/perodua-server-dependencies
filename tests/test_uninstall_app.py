"""uninstall.sh --role app against a fake Docker CLI.

The script requires root on Ubuntu 24.04, like the deployment scripts; run
these in the test image (tests/Dockerfile) or skip them elsewhere.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "uninstall.sh"
ODOO = "perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-v1.0.0@sha256:" + "c" * 64
WEB = "perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-web-v1.0.0@sha256:" + "6" * 64


def ubuntu_2404():
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return False
    return "ID=ubuntu" in text and 'VERSION_ID="24.04"' in text


@unittest.skipUnless(os.geteuid() == 0 and ubuntu_2404(), "needs root on Ubuntu 24.04 (the test image)")
class UninstallAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="perodua-uninstall-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.calls = base / "docker-calls.jsonl"
        stubs = base / "bin"
        stubs.mkdir()
        docker = stubs / "docker"
        docker.write_text(r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[:2] == ['ps', '-aq'] or args[:3] in (['network', 'ls', '-q'], ['volume', 'ls', '-q']):
    print('fixture-id-1\nfixture-id-2' if args[0] == 'ps' else 'fixture-id-3')
sys.exit(0)
''')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                        FAKE_DOCKER_LOG=str(self.calls))
        self.app = base / "app"
        self.app.mkdir()
        (self.app / ".deployment-identity").write_text(
            "release=client-stable-uiux-v1.0.0\nrevision=fixture\nproject=perodua-fixture\n"
            "host=192.0.2.20\nport=5432\ndatabase=perodua\nuser=odoo\n")
        (self.app / "compose.yml").write_text(
            f"services:\n  odoo:\n    image: {ODOO}\n  web:\n    image: {WEB}\n")
        (self.app / "secrets").mkdir()
        (self.app / "secrets" / "db_password").write_text("fixture-password")

    def run_script(self, *args, stdin=subprocess.DEVNULL):
        return subprocess.run(["bash", str(SCRIPT), "--role", "app", "--dir", str(self.app), *args],
                              env=self.env, stdin=stdin, text=True, capture_output=True,
                              timeout=30, start_new_session=True)

    def docker_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def changes(self):
        return [call for call in self.docker_calls() if call[:2] not in (["ps", "-aq"],)]

    def test_default_removes_containers_network_and_directory_but_keeps_data_and_images(self):
        result = self.run_script("--confirm", "perodua-fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kept: the attachments volume perodua-fixture_filestore", result.stdout)
        self.assertEqual(self.changes(), [[
            "compose", "--project-name", "perodua-fixture", "--file", str(self.app / "compose.yml"),
            "down", "--remove-orphans",
        ]])
        self.assertFalse(self.app.exists())

    def test_purge_also_removes_the_attachments_volume_and_the_images(self):
        result = self.run_script("--confirm", "perodua-fixture", "--purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("delete the attachments volume perodua-fixture_filestore", result.stdout)
        self.assertEqual(self.changes(), [
            ["compose", "--project-name", "perodua-fixture", "--file", str(self.app / "compose.yml"),
             "down", "--remove-orphans", "--volumes"],
            ["image", "rm", ODOO],
            ["image", "rm", WEB],
        ])
        self.assertFalse(self.app.exists())

    def test_without_a_compose_file_the_labelled_resources_are_removed(self):
        (self.app / "compose.yml").unlink()
        result = self.run_script("--confirm", "perodua-fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        label = "label=com.docker.compose.project=perodua-fixture"
        self.assertIn(["rm", "-f", "fixture-id-1", "fixture-id-2"], self.docker_calls())
        self.assertIn(["network", "rm", "fixture-id-3"], self.docker_calls())
        self.assertNotIn(["volume", "ls", "-q", "--filter", label], self.docker_calls())
        self.assertFalse(self.app.exists())

    def test_a_wrong_confirmation_changes_nothing(self):
        result = self.run_script("--confirm", "another-project")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertTrue((self.app / "secrets" / "db_password").exists())

    def test_without_a_terminal_or_confirm_nothing_changes(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pass --confirm perodua-fixture", result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertTrue(self.app.exists())

    def test_a_directory_not_created_by_deploy_app_is_refused(self):
        (self.app / ".deployment-identity").unlink()
        result = self.run_script("--confirm", "perodua-fixture")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("was not created by deploy-app.sh", result.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertTrue(self.app.exists())


if __name__ == "__main__":
    unittest.main()
