"""deploy-app.sh orchestration of the fresh UAT path, against a scripted fake Docker.

Run on Linux/WSL: python3 -m unittest discover -s tests -v
The fake answers each container command the way the real one would report,
so these tests pin the ORDER and the ABSENCE of steps: what runs before the
database is touched, what never runs on an initialized database, and that no
module upgrade (-u) is ever issued. The real guard, ORM script and Odoo are
exercised by the acceptance run, not here.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy-app.sh'
FRESH_MODULES = ('perodua_client_stable,perodua_gateway,perodua_forecast_workbook,'
                 'perodua_supplier_execution,perodua_uiux_api')

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\n')
def reply(text='', code=0, err=''):
    if text: print(text)
    if err: print(err, file=sys.stderr)
    sys.exit(code)
if args[:2] == ['compose', 'version']: reply('Docker Compose version v2.39.4')
if args and args[0] == 'info': reply('linux/x86_64' if '--format' in args else '')
if args and args[0] in ('ps', 'pull'): reply()
if len(args) > 1 and args[0] in ('volume', 'network') and args[1] == 'ls': reply()
if args and args[0] == 'compose':
    if 'config' in args: reply()
    if 'up' in args: reply()
    if 'exec' in args:
        sys.stdin.read()
        code = int(os.environ.get('FAKE_HTTP_EXIT', '0'))
        reply('fake HTTP verification ' + ('passed' if code == 0 else 'failed'), code)
    if 'run' in args:
        cmd = args[args.index('odoo', args.index('run')) + 1:]
        if cmd[:2] == ['python3', '/opt/deploy/preflight.py']:
            mode = cmd[2]
            if mode == 'check': reply(os.environ['FAKE_CHECK'])
            reply({'mark-pending': 'PENDING', 'verify-fresh': 'VERIFIED 3',
                   'stamp': 'READY 3'}[mode])
        if cmd[:2] == ['python3', '/opt/deploy/uat_guard.py']:
            if cmd[2] == 'guard':
                code = int(os.environ.get('FAKE_GUARD_EXIT', '0'))
                reply(json.dumps({'verdict': 'accepted' if code == 0 else 'refused'}), code,
                      'UAT guard: ' + ('accepted' if code == 0 else 'REFUSED: fixture'))
            if cmd[2] == 'demo-flag':
                reply(os.environ.get('FAKE_DEMO_FLAG', '--without-demo=True'))
        if cmd[:1] == ['odoo']: reply('fake odoo init', int(os.environ.get('FAKE_INIT_EXIT', '0')))
        if cmd[:2] == ['bash', '-c'] and 'odoo shell' in cmd[2]:
            code = int(os.environ.get('FAKE_ADMINS_EXIT', '0'))
            reply('UAT admins: ready: whadmin, admin1, admin2' if code == 0 else 'boom', code)
        if cmd[:2] == ['python3', '-c']: reply()
print('Fake Docker rejected unexpected command: ' + ' '.join(args), file=sys.stderr)
sys.exit(93)
'''


class UatOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='perodua-uat-orchestration-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        (base / 'bin').mkdir()
        fake = base / 'bin' / 'docker'
        fake.write_text(FAKE_DOCKER, encoding='utf-8')
        fake.chmod(0o755)
        self.log = base / 'docker-calls.jsonl'
        self.deploy_dir = base / 'app'
        password = base / 'db-password'
        password.write_text('fixture-password\n', encoding='utf-8')
        password.chmod(0o600)
        self.config = base / 'app.env'
        self.config.write_text(''.join(f'{k}={v}\n' for k, v in {
            'DB_HOST': '192.0.2.20', 'DB_PORT': '5432', 'DB_NAME': 'perodua', 'DB_USER': 'odoo',
            'DB_PASSWORD_FILE': str(password), 'PROJECT_NAME': 'perodua-uat-fixture',
            'HTTP_PORT': '18110', 'BIND_IP': '127.0.0.1', 'STARTUP_TIMEOUT': '600',
            'INIT_TIMEOUT': '600'}.items()), encoding='utf-8')
        self.env = dict(os.environ, HOME=str(base), DOCKER_CONFIG=str(base / 'docker-config'),
                        PATH=str(base / 'bin') + os.pathsep + os.environ['PATH'],
                        FAKE_DOCKER_LOG=str(self.log))

    def run_script(self, check, *extra, **fake):
        self.env['FAKE_CHECK'] = check
        self.env.update({f'FAKE_{k.upper()}': str(v) for k, v in fake.items()})
        return subprocess.run(['bash', str(SCRIPT), '--config', str(self.config),
                               '--dir', str(self.deploy_dir), '--non-interactive', *extra],
                              env=self.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def container_steps(self):
        """Condensed container commands, in order."""
        steps = []
        for args in self.calls():
            if args[:1] != ['compose']:
                continue
            if 'run' in args:
                cmd = args[args.index('odoo', args.index('run')) + 1:]
                if cmd[:2] == ['python3', '/opt/deploy/preflight.py']:
                    steps.append('preflight ' + cmd[2])
                elif cmd[:2] == ['python3', '/opt/deploy/uat_guard.py']:
                    steps.append('uat_guard ' + cmd[2])
                elif cmd[:1] == ['odoo']:
                    steps.append('odoo ' + ' '.join(cmd[1:]))
                elif cmd[:2] == ['bash', '-c']:
                    steps.append('odoo shell < uat_admins.py' if 'uat_admins.py' in cmd[2] else 'bash')
                elif cmd[:2] == ['python3', '-c']:
                    steps.append('filestore check')
            elif 'up' in args:
                steps.append('up')
            elif 'exec' in args:
                steps.append('http verify fresh=' + args[-1])
        return steps

    def assert_never_upgrades_or_touches_admins(self):
        for args in self.calls():
            self.assertNotIn('-u', args)
            self.assertNotIn('--update', args)
            self.assertFalse(any('odoo shell' in a for a in args), args)
            self.assertFalse(any(a == '-i' for a in args), args)

    # ── initialized databases ───────────────────────────────────────────────
    def test_ready_database_is_used_as_is(self):
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check', 'up', 'http verify fresh=0'])
        self.assert_never_upgrades_or_touches_admins()

    def test_init_db_does_not_reinitialize_a_ready_database(self):
        result = self.run_script('READY 2', '--init-db')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('initialization and module upgrades are skipped', result.stdout)
        self.assertEqual(self.container_steps(),
                         ['preflight check', 'filestore check', 'up', 'http verify fresh=0'])
        self.assert_never_upgrades_or_touches_admins()
        self.assertNotIn('whadmin', result.stdout)

    # ── nothing initialized yet ─────────────────────────────────────────────
    def test_empty_database_requires_init_db(self):
        result = self.run_script('EMPTY')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DB_MODE=empty', result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check'])

    def test_missing_database_without_createdb_points_to_the_db_server(self):
        result = self.run_script('MISSING_NO_CREATEDB', '--init-db')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('may not create databases', result.stdout)
        self.assertIn('run deploy-db.sh with DB_MODE=empty', result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check'])

    def test_fresh_initialization_runs_every_step_in_order(self):
        result = self.run_script('EMPTY', '--init-db')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), [
            'preflight check',
            'uat_guard guard',
            'uat_guard demo-flag',
            f'odoo -c /etc/odoo/odoo.conf -d perodua -i {FRESH_MODULES} --without-demo=True --stop-after-init',
            'preflight mark-pending',
            'odoo shell < uat_admins.py',
            'preflight verify-fresh',
            'filestore check',
            'up',
            'http verify fresh=1',
            'preflight stamp',
        ])
        self.assertNotIn('perodua_demo_client', FRESH_MODULES)
        self.assertIn('whadmin, admin1, admin2 / perodua', result.stdout)
        for args in self.calls():
            self.assertNotIn('-u', args)
            # The database password never appears on a command line.
            self.assertFalse(any('--db_password' in a for a in args), args)

    def test_guard_refusal_stops_before_any_database_write(self):
        for code in (3, 2):  # refused, or could not be verified
            with self.subTest(exit_code=code):
                self.log.unlink(missing_ok=True)
                result = self.run_script('EMPTY', '--init-db', guard_exit=code)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Refused before any database change', result.stdout)
                self.assertEqual(self.container_steps(), ['preflight check', 'uat_guard guard'])
                self.assert_never_upgrades_or_touches_admins()

    def test_unexpected_demo_option_stops_before_initialization(self):
        result = self.run_script('EMPTY', '--init-db', demo_flag='--without-demo=all;touch /x')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unexpected demo-data option', result.stdout)
        self.assertNotIn('odoo -c', ' '.join(self.container_steps()))

    def test_failed_administrator_setup_is_not_stamped(self):
        result = self.run_script('EMPTY', '--init-db', admins_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UAT administrator setup failed', result.stdout)
        steps = self.container_steps()
        self.assertEqual(steps[-1], 'odoo shell < uat_admins.py')
        self.assertNotIn('preflight stamp', steps)
        self.assertNotIn('up', steps)

    # ── an interrupted fresh initialization ─────────────────────────────────
    def test_setup_pending_requires_init_db(self):
        result = self.run_script('SETUP_PENDING')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Rerun with --init-db to finish it', result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check'])

    def test_setup_pending_finishes_without_reinstalling(self):
        result = self.run_script('SETUP_PENDING', '--init-db')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), [
            'preflight check', 'odoo shell < uat_admins.py', 'preflight verify-fresh',
            'filestore check', 'up', 'http verify fresh=1', 'preflight stamp'])
        self.assertFalse(any(a == '-i' for args in self.calls() for a in args))

    def test_unmarked_initialization_is_adopted_only_with_init_db(self):
        # `odoo -i` finished but the run stopped before recording it.
        result = self.run_script('SETUP_UNMARKED')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Rerun with --init-db to finish it', result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check'])
        self.log.unlink()
        result = self.run_script('SETUP_UNMARKED', '--init-db')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), [
            'preflight check', 'preflight mark-pending', 'odoo shell < uat_admins.py',
            'preflight verify-fresh', 'filestore check', 'up', 'http verify fresh=1', 'preflight stamp'])
        self.assertFalse(any(a == '-i' for args in self.calls() for a in args))

    def test_failed_sign_in_verification_is_not_stamped(self):
        result = self.run_script('EMPTY', '--init-db', http_exit=1)
        self.assertNotEqual(result.returncode, 0)
        steps = self.container_steps()
        self.assertEqual(steps[-1], 'http verify fresh=1')
        self.assertNotIn('preflight stamp', steps)

    # ── generated deployment ────────────────────────────────────────────────
    def test_helpers_are_installed_read_only_and_mounted(self):
        self.run_script('READY 0')
        for helper in ('uat_guard.py', 'uat_admins.py'):
            path = self.deploy_dir / helper
            self.assertEqual(path.read_bytes(), (ROOT / helper).read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        compose = (self.deploy_dir / 'compose.yml').read_text()
        self.assertIn('./uat_guard.py:/opt/deploy/uat_guard.py:ro', compose)
        self.assertIn('./uat_admins.py:/opt/deploy/uat_admins.py:ro', compose)
        self.assertIn('EXCLUDED_MODULE: "perodua_demo_client"', compose)
        self.assertIn(f'INIT_MODULES: "{FRESH_MODULES}"', compose)
        self.assertIn('RESTORED_MODULES: "perodua_client_stable,perodua_demo_client,', compose)

    def test_missing_helper_fails_before_docker(self):
        partial = Path(self.temp.name) / 'partial-release'
        partial.mkdir()
        (partial / 'deploy-app.sh').write_bytes(SCRIPT.read_bytes())
        (partial / 'uat_guard.py').write_bytes((ROOT / 'uat_guard.py').read_bytes())
        self.env['FAKE_CHECK'] = 'READY 0'
        result = subprocess.run(['bash', str(partial / 'deploy-app.sh'), '--config', str(self.config),
                                 '--dir', str(self.deploy_dir), '--non-interactive'],
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uat_admins.py is missing', result.stdout)
        self.assertEqual(self.calls(), [])


class UatAdminScriptTests(unittest.TestCase):
    """Static properties of the ORM script; its behaviour is proven on a real
    Odoo database by the acceptance run."""

    source = (ROOT / 'uat_admins.py').read_text(encoding='utf-8')

    def test_compiles(self):
        compile(self.source, 'uat_admins.py', 'exec')

    def test_uses_the_orm_for_passwords_never_sql(self):
        self.assertIn("user.write({'password': PASSWORD})", self.source)
        for forbidden in ('cr.execute', 'UPDATE res_users', 'crypt_context', 'passlib'):
            self.assertNotIn(forbidden, self.source)

    def test_keeps_the_seeded_logins_and_archives_the_native_administrator(self):
        self.assertIn("Users.search([('login', '=', login)])", self.source)
        self.assertIn("admin.write({'active': False})", self.source)
        self.assertNotIn("admin.write({'login'", self.source)
        self.assertIn('the technical superuser was touched', self.source)
        self.assertIn('other active administrator logins remain', self.source)
        self.assertIn("env.ref('base.group_erp_manager')", self.source)

    def test_copies_groups_from_the_administrator_instead_of_listing_them(self):
        self.assertIn('groups, effective = admin.group_ids, admin.all_group_ids', self.source)
        self.assertIn("'group_ids': [Command.set(groups.ids)]", self.source)
        self.assertIn('user.all_group_ids != effective', self.source)
        self.assertIn("env.ref('base.group_system')", self.source)
        self.assertNotIn("env.ref('base.group_user')", self.source)

    def test_is_idempotent_and_commits_only_after_the_checks(self):
        self.assertIn('if not user:', self.source)
        self.assertLess(self.source.index('_check_credentials('), self.source.index('env.cr.commit()'))
        self.assertEqual(self.source.count('env.cr.commit()'), 1)

    def test_fixed_uat_logins_and_password(self):
        self.assertIn("PASSWORD = 'perodua'", self.source)
        self.assertIn("LOGINS = (('whadmin', 'WH Admin'), ('admin1', 'Admin 1'), ('admin2', 'Admin 2'))",
                      self.source)


if __name__ == '__main__':
    unittest.main()
