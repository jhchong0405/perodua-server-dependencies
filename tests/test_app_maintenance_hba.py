"""Exercise deploy-db.sh's HBA writer on temporary files; no PostgreSQL changes."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SOURCE = (SCRIPTS / 'deploy-db.sh').read_text()
MARKER = 'python3 - "$HBA" "$DB_NAME" "$DB_USER" "$APP_CIDR" "$STAGING" <<\'PY\'\n'
WRITER = SOURCE.split(MARKER, 1)[1].split('\nPY\n', 1)[0]
EXISTING = (
    '# Existing administrator rules must stay unchanged.\n'
    'local all postgres peer\n'
    'host all all 127.0.0.1/32 scram-sha-256\n'
    'host "postgres" "monitor" 10.233.20.4/32 scram-sha-256\n'
    'host "other_app" "other_user" 10.233.20.5/32 scram-sha-256\n'
)


class MaintenanceDatabaseHbaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='perodua-hba-test-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'pg_hba.conf'
        self.path.write_text(EXISTING)
        self.path.chmod(0o640)

    def write_hba(self, cidr):
        subprocess.run(
            [sys.executable, '-', str(self.path), 'perodua_app_script',
             'odoo', cidr, ''],
            input=WRITER, text=True, check=True, capture_output=True,
        )
        return self.path.read_text()

    def test_remote_app_gets_postgres_rule_for_only_its_own_identity_and_source(self):
        result = self.write_hba('10.233.10.10/32')
        generated = result.removesuffix(EXISTING).splitlines()
        maintenance = [line for line in generated if line.startswith('host "postgres" ')]
        self.assertEqual(maintenance, [
            'host "postgres" "odoo" 10.233.10.10/32 scram-sha-256'
        ])
        self.assertTrue(result.endswith(EXISTING))
        self.assertIn('host "perodua_app_script" "odoo" 10.233.10.10/32 scram-sha-256', generated)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_same_configuration_is_idempotent(self):
        first = self.write_hba('10.233.10.10/32')
        self.assertEqual(first, self.write_hba('10.233.10.10/32'))
        self.assertEqual(first.count('host "postgres" "odoo" '), 1)

    def test_changing_source_replaces_the_prior_maintenance_rule(self):
        self.write_hba('10.233.10.10/32')
        result = self.write_hba('10.233.10.11/32')
        self.assertNotIn('10.233.10.10/32', result)
        self.assertIn('host "postgres" "odoo" 10.233.10.11/32 scram-sha-256', result)
        self.assertTrue(result.endswith(EXISTING))

    def test_local_only_deployment_has_no_remote_maintenance_exception(self):
        self.assertNotIn('host "postgres" "odoo" ', self.write_hba(''))
        self.write_hba('10.233.10.10/32')
        result = self.write_hba('')
        self.assertNotIn('host "postgres" "odoo" ', result)
        self.assertTrue(result.endswith(EXISTING))


if __name__ == '__main__':
    unittest.main()
