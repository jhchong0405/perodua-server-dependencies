"""deploy-app.sh --upgrade against a scripted fake Docker.

Run on Linux/WSL: python3 -m unittest discover -s tests -v
The deployment directory is set up as an earlier release left it. The fake
Docker answers the container commands of the upgrade and records, for every
call, which compose file it used and which release the directory named at that
moment. So these tests pin what is refused before anything changes, the ORDER
of the steps (the backup before the switch), what a failure leaves, and that no
module upgrade (-u) is ever issued. pg_dump, pg_restore and tar themselves are
exercised against real PostgreSQL 16 by the acceptance check, not here.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SCRIPT = SCRIPTS / 'deploy-app.sh'
TEXT = SCRIPT.read_text(encoding='utf-8')
RELEASE = re.search(r'^RELEASE=(\S+)$', TEXT, re.M).group(1)
REVISION = re.search(r'^REVISION=([0-9a-f]{40})$', TEXT, re.M).group(1)
ODOO_IMAGE = re.search(r'^ODOO_IMAGE=(\S+)$', TEXT, re.M).group(1)
OLD_RELEASE = 'client-stable-uiux-v1.0.3'
OLD_REVISION = 'a' * 40
DUMP = b'PGDMP fixture archive\n'
FINGERPRINT = ('Database preflight: database module fingerprint does not match this release, '
               'and is not the fingerprint this release upgrades modules from')

FAKE_DOCKER = r'''#!/usr/bin/env python3
import io, json, os, sys, tarfile
args = sys.argv[1:]
deploy_dir = os.environ['FAKE_DEPLOY_DIR']
def named_release():
    try:
        with open(os.path.join(deploy_dir, '.deployment-identity')) as f:
            return next(line[8:].strip() for line in f if line.startswith('release='))
    except (OSError, StopIteration):
        return None
entry = {'args': args, 'release': named_release()}
# compose --project-name P --file F [--file OVERRIDE ...] ACTION: the overrides as they read
overrides = []
if args[:1] == ['compose']:
    rest = args[5:]
    while rest[:1] == ['--file']:
        with open(rest[1]) as f:
            overrides.append(f.read())
        rest = rest[2:]
    entry['overrides'] = overrides
with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(entry) + '\n')
def reply(text='', code=0, err=''):
    if text: print(text)
    if err: print(err, file=sys.stderr)
    sys.exit(code)
if args[:2] == ['compose', 'version']: reply('Docker Compose version v2.39.4')
if args and args[0] == 'info': reply('linux/x86_64' if '--format' in args else '')
if args and args[0] in ('ps', 'pull'): reply()
if args[:2] in (['volume', 'ls'], ['network', 'ls']): reply()
if args[:2] == ['volume', 'inspect']: reply()
if args[:2] == ['rm', '-f']: reply()
STAGED = args[:1] == ['compose'] and args[4] != os.path.join(deploy_dir, 'compose.yml')
def deployed_preflight_is_new():
    # The directory's preflight.py: the fixture of an earlier release, or the
    # one this release wrote at the switch.
    with open(os.path.join(deploy_dir, 'preflight.py')) as f:
        return 'UPGRADE_PENDING' in f.read()
def module_db(cmd):
    # FAKE_DB: the database of a module upgrade, as a JSON file. Its modhash
    # is 'from' (the old modules), 'upgrading' (marked), 'upgraded' (checked
    # after -u) or 'new' (stamped). Each preflight answers as the real one.
    mode = cmd[2]
    with open(os.environ['FAKE_DB']) as f:
        db = json.load(f)
    def save():
        with open(os.environ['FAKE_DB'], 'w') as f:
            json.dump(db, f)
    state = db['modhash']
    if mode in ('check', 'upgrade-check') and not (STAGED or deployed_preflight_is_new()):
        # The preflight.py of v1.0.5 to v1.0.8: READY for its own modules only.
        if state == 'from': reply('READY 3')
        reply(code=1, err='Database preflight: database module fingerprint does not match this release; upgrades require a separate plan')
    if state == 'upgrading' and mode != 'verify-upgrade':
        reply(code=1, err='Database preflight: database is marked by a module upgrade that did not finish (perodua.image_modhash upgrading:fixture): no release can run on it.')
    if mode == 'upgrade-check':
        if os.environ.get('FAKE_REFUSE'):
            reply(code=1, err='\n'.join('Database preflight: module upgrade refused: ' + r for r in os.environ['FAKE_REFUSE'].split('|')))
        if state == 'from':
            if os.environ.get('FAKE_OTHER_FINGERPRINT'):
                reply(code=1, err='Database preflight: database module fingerprint does not match this release, and is not the fingerprint this release upgrades modules from')
            retired = os.environ.get('FAKE_RETIRED', 'table perodua_transporter_rate 0|column stock_picking.perodua_dispatch_state 0')
            reply(''.join('RETIRED ' + r + '\n' for r in retired.split('|')) + 'MODULES perodua_client_stable,perodua_ui\nMODULE_UPGRADE 3')
        reply('UPGRADE_PENDING 3' if state == 'upgraded' else 'READY 3')
    if mode == 'check':
        if state == 'upgraded':
            if '--accept-pending' in cmd: reply('UPGRADE_PENDING 3')
            reply(code=1, err='Database preflight: the module upgrade of this database to this release is checked but not finished')
        reply('READY 3') if state == 'new' else reply(code=1, err='Database preflight: database module fingerprint does not match this release; upgrades require a separate plan')
    if mode == 'mark-upgrading':
        if os.environ.get('FAKE_MARK_EXIT'): reply(code=1, err='Database preflight: fixture')
        db['modhash'] = 'upgrading'; save(); reply('MARKED')
    if mode == 'verify-upgrade':
        if os.environ.get('FAKE_VERIFY_EXIT'): reply(code=1, err='Database preflight: the upgraded database is not right: perodua_ui is to upgrade')
        db['modhash'] = 'upgraded'; save()
        reply('Cron jobs: 5 flags put back as before the upgrade.\n'
              'Cron jobs: perodua_orders_ext.cron_pull_pss_orders stays off: it reads the mock feed while its system resolves to mock.\n'
              'Mail: 2 messages the upgrade queued are held (state exception), until deploy-app.sh --release-queued-mail.\n'
              'UPGRADE_VERIFIED 2')
    if mode == 'stamp-upgrade':
        if state != 'upgraded': reply(code=1, err='Database preflight: stamp-upgrade does not apply')
        db['modhash'] = 'new'; save(); reply('READY 3')
    if mode == 'release-mail':
        reply('RELEASED 2') if state == 'new' else reply(code=1, err='Database preflight: fixture')
    if mode == 'retired-export':
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            data = b'id,perodua_dispatch_state\n7,packing\n'
            info = tarfile.TarInfo('retired-data/columns/stock_picking.perodua_dispatch_state.csv')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        sys.stdout.buffer.write(buffer.getvalue())
        sys.exit(int(os.environ.get('FAKE_EXPORT_EXIT', '0')))
if args and args[0] == 'compose':
    action = args[5 + 2 * len(overrides):]
    if action[:1] == ['config']: reply()
    if action == ['ps', '--quiet', '--status', 'running']:
        reply('0123456789ab' if os.environ.get('FAKE_RUNNING') == '1' else '')
    if action[:1] == ['stop']: reply()
    if action == ['rm', '--stop', '--force', 'odoo', 'web']: reply()
    if action[:1] == ['up']: reply(code=int(os.environ.get('FAKE_UP_EXIT', '0')))
    if action[:1] == ['exec']:
        sys.stdin.read()
        reply(code=int(os.environ.get('FAKE_HTTP_EXIT', '0')))
    if action[:1] == ['run']:
        cmd = action[action.index('odoo') + 1:]
        if cmd[:2] == ['python3', '/opt/deploy/preflight.py'] and os.environ.get('FAKE_DB') and cmd[2] != 'public-urls':
            module_db(cmd)
        if cmd[:2] == ['python3', '/opt/deploy/uat_guard.py']:
            if cmd[2] == 'guard':
                code = int(os.environ.get('FAKE_GUARD_EXIT', '0'))
                reply(json.dumps({'verdict': 'refused' if code else 'accepted'}), code,
                      'UAT guard: REFUSED: fixture' if code else 'UAT guard: accepted: fixture')
            if cmd[2] == 'demo-flag': reply('--without-demo=True')
        if cmd[:2] == ['bash', '-c'] and ' -u ' in cmd[2]:
            if os.environ.get('FAKE_U_HANG'):  # a long -u: say so, then take a while
                import time
                open(os.environ['FAKE_U_HANG'], 'w').close()
                time.sleep(float(os.environ.get('FAKE_U_SECONDS', '30')))
                open(os.environ['FAKE_U_HANG'] + '.finished', 'w').close()
            reply('fake odoo -u', int(os.environ.get('FAKE_U_EXIT', '0')))
        if cmd[:2] == ['python3', '/opt/deploy/preflight.py']:
            if cmd[2] in ('check', 'upgrade-check'):
                state = os.environ.get('FAKE_CHECK', '')
                reply(state, 0 if state else 1, '' if state else os.environ.get('FAKE_CHECK_ERR', 'Database preflight: fixture'))
            if cmd[2] == 'public-urls':
                code = int(os.environ.get('FAKE_PUBLIC_URLS_EXIT', '0'))
                reply('' if code else 'PUBLIC_URLS_SET', code)
        if cmd[:2] == ['python3', '-c']: reply()
        if cmd[:2] == ['bash', '-c'] and 'pg_database_size' in cmd[2]:
            reply(os.environ.get('FAKE_DB_KB', '2048') + '\n' + os.environ.get('FAKE_FS_KB', '1024'))
        if cmd[:2] == ['bash', '-c'] and 'pg_dump' in cmd[2]:
            code = int(os.environ.get('FAKE_DUMP_EXIT', '0'))
            if code: reply(code=code, err='pg_dump: error: fixture')
            if os.environ.get('FAKE_DUMP_HANG'):  # a long dump: say so, then take a while
                import time
                sys.stdout.buffer.write(b'PGDMP partial')
                sys.stdout.flush()
                open(os.environ['FAKE_DUMP_HANG'], 'w').close()
                time.sleep(float(os.environ.get('FAKE_DUMP_SECONDS', '30')))
            sys.stdout.buffer.write(b'PGDMP fixture archive\n')
            sys.exit(0)
        if cmd == ['pg_restore', '--list']:
            data = sys.stdin.buffer.read()
            if not data.startswith(b'PGDMP'): reply(code=1, err='pg_restore: error: input file does not appear to be a valid archive')
            table = os.environ.get('FAKE_LIST_TABLE', 'ir_module_module')
            reply(';\n; Archive created at 2026-10-06 07:15:00 UTC\n;\n'
                  '5020; 0 16990 TABLE DATA public ir_config_parameter odoo\n'
                  '5021; 0 17000 TABLE DATA public ' + table + ' odoo')
        if cmd[:2] == ['bash', '-c'] and 'tar -czf' in cmd[2]:
            code = int(os.environ.get('FAKE_TAR_EXIT', '0'))
            if code: reply(code=code, err='tar: fixture')
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                for name in ('filestore/perodua/ab/abcdef', 'filestore/perodua/cd/cdef01', 'filestore/perodua/ef/ef0123'):
                    info = tarfile.TarInfo(name)
                    info.size = 3
                    archive.addfile(info, io.BytesIO(b'one'))
            sys.stdout.buffer.write(buffer.getvalue())
            sys.exit(0)
print('Fake Docker rejected unexpected command: ' + ' '.join(args), file=sys.stderr)
sys.exit(93)
'''


class UpgradeHarness:
    """The directory of an earlier release, the fake Docker and its record."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='perodua-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        (self.base / 'bin').mkdir()
        fake = self.base / 'bin' / 'docker'
        fake.write_text(FAKE_DOCKER, encoding='utf-8')
        fake.chmod(0o755)
        self.log = self.base / 'docker-calls.jsonl'
        self.deploy_dir = self.base / 'app'
        self.values = {
            'DB_HOST': '192.0.2.20', 'DB_PORT': '5432', 'DB_NAME': 'perodua', 'DB_USER': 'odoo',
            'PROJECT_NAME': 'perodua-upgrade-fixture', 'HTTP_PORT': '18110', 'BIND_IP': '127.0.0.1',
            'STARTUP_TIMEOUT': '600', 'INIT_TIMEOUT': '600'}
        self.deployed(OLD_RELEASE, OLD_REVISION)
        self.env = dict(os.environ, HOME=str(self.base), DOCKER_CONFIG=str(self.base / 'docker-config'),
                        PATH=str(self.base / 'bin') + os.pathsep + os.environ['PATH'],
                        FAKE_DOCKER_LOG=str(self.log), FAKE_DEPLOY_DIR=str(self.deploy_dir),
                        FAKE_CHECK='READY 3', FAKE_RUNNING='1')

    def deployed(self, release, revision, unverified=False):
        """A directory as deploy-app.sh of release left it after a verified run."""
        d = self.deploy_dir
        (d / 'secrets').mkdir(parents=True, mode=0o700, exist_ok=True)
        (d / 'secrets' / 'db_password').write_text('fixture-password')
        (d / 'secrets' / 'db_password').chmod(0o444)
        self.identity = (f'release={release}\nrevision={revision}\nproject={self.values["PROJECT_NAME"]}\n'
                         f'host={self.values["DB_HOST"]}\nport={self.values["DB_PORT"]}\n'
                         f'database={self.values["DB_NAME"]}\nuser={self.values["DB_USER"]}\n')
        (d / '.deployment-identity').write_text(self.identity)
        (d / 'compose.yml').write_text(f'# the compose file of {release}\n')
        (d / 'preflight.py').write_text(f'# the preflight of {release}\n')
        (d / '.deploy.lock').touch()
        if unverified:
            (d / '.deployment-unverified').touch()
        self.write_app_env()

    def write_app_env(self, **updates):
        values = dict(self.values, **updates)
        values['DB_PASSWORD_FILE'] = f'{self.deploy_dir}/secrets/db_password'
        self.app_env = ''.join(f'{k}={v}\n' for k, v in values.items())
        (self.deploy_dir / 'app.env').write_text(self.app_env)

    def run_script(self, *extra, **fake):
        self.env.update({f'FAKE_{k.upper()}': str(v) for k, v in fake.items()})
        return subprocess.run(['bash', str(SCRIPT), '--upgrade', '--dir', str(self.deploy_dir), '--non-interactive', *extra],
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def steps(self):
        """Condensed Docker steps, in order, with the release the directory named
        at the time. 'staged' marks the new release's compose file in the
        temporary folder, not the directory's own."""
        steps = []
        for entry in self.calls():
            args = entry['args']
            if args[:1] == ['pull']:
                steps.append(('pull', entry['release']))
                continue
            if args[:2] != ['compose', '--project-name'] or args[5:6] == ['config']:
                continue
            staged = '' if args[4] == f'{self.deploy_dir}/compose.yml' else 'staged '
            action = args[5 + 2 * len(entry['overrides']):]
            if action[:1] == ['run']:
                cmd = action[action.index('odoo') + 1:]
                if cmd[:2] == ['python3', '/opt/deploy/preflight.py']:
                    step = 'preflight ' + cmd[2] + (' --accept-pending' if '--accept-pending' in cmd else '')
                elif cmd[:2] == ['python3', '-c']:
                    step = 'filestore check'
                elif cmd[:2] == ['bash', '-c'] and 'pg_database_size' in cmd[2]:
                    step = 'backup size'
                elif cmd[:2] == ['bash', '-c'] and 'pg_dump' in cmd[2]:
                    step = 'pg_dump'
                elif cmd[:2] == ['bash', '-c'] and 'tar -czf' in cmd[2]:
                    step = 'filestore archive'
                elif cmd[:2] == ['bash', '-c'] and ' -u ' in cmd[2]:
                    step = 'odoo -u'
                elif cmd[:2] == ['python3', '/opt/deploy/uat_guard.py']:
                    step = 'uat_guard ' + cmd[2]
                else:
                    step = ' '.join(cmd)
            elif action[:1] == ['up']:
                step = 'up' if '--force-recreate' in action else 'restart'
            elif action[:1] == ['ps']:
                step = 'running?'
            elif action == ['rm', '--stop', '--force', 'odoo', 'web']:
                step = 'remove the containers'
            elif action[:1] == ['exec']:
                step = 'http verify'
            else:
                step = ' '.join(action)
            steps.append((staged + step, entry['release']))
        return steps

    def step_names(self):
        return [name for name, _ in self.steps()]

    def backups(self, root=None):
        root = root or self.deploy_dir / 'backups'
        return sorted(root.iterdir()) if root.is_dir() else []

    def assert_nothing_changed(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout)
        self.assertIn(f'Nothing was changed: {self.deploy_dir} still runs {OLD_RELEASE}.', result.stdout)
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
        self.assertEqual((self.deploy_dir / 'compose.yml').read_text(), f'# the compose file of {OLD_RELEASE}\n')
        self.assertEqual((self.deploy_dir / 'app.env').read_text(), self.app_env)
        self.assertNotIn('stop web odoo', self.step_names())
        self.assertEqual(self.backups(), [])

    def assert_never_upgrades_modules(self):
        for entry in self.calls():
            self.assertNotIn('-u', entry['args'])
            self.assertNotIn('--update', entry['args'])
            self.assertFalse(any(a == '-i' for a in entry['args']), entry['args'])



class UpgradeTests(UpgradeHarness, unittest.TestCase):
    # ── refused before anything changes ─────────────────────────────────────
    def test_another_project_database_server_or_user_is_refused(self):
        for key, value in (('PROJECT_NAME', 'perodua-other'), ('DB_NAME', 'perodua_uat'), ('DB_HOST', '192.0.2.99'),
                           ('DB_PORT', '5433'), ('DB_USER', 'odoo_uat')):
            with self.subTest(key=key):
                self.log.unlink(missing_ok=True)
                self.write_app_env(**{key: value})
                result = self.run_script()
                self.assert_nothing_changed(result, '--upgrade keeps the project, database server, port, database and user')
                self.assertIn(f'={value}', result.stdout)
                self.assertEqual(self.step_names(), [])  # neither a pull nor a container
        self.write_app_env()

    def test_a_module_fingerprint_mismatch_is_refused_before_the_app_stops(self):
        result = self.run_script(check='', check_err=FINGERPRINT)
        self.assert_nothing_changed(result, 'It upgrades modules (-u) only from the modules of v1.0.3 to v1.0.8')
        self.assertIn(FINGERPRINT, result.stdout)
        # The new release's image checks the database; the App keeps running.
        self.assertEqual(self.step_names(), ['pull', 'pull', 'staged preflight upgrade-check'])
        self.assert_never_upgrades_modules()

    def test_a_database_that_is_not_ready_is_refused(self):
        for check, err in (('SETUP_PENDING', ''), ('', 'Database preflight: cannot connect to perodua (fixture)')):
            with self.subTest(check=check):
                self.log.unlink(missing_ok=True)
                result = self.run_script(check=check, check_err=err)
                message = f'the check of perodua reports {check}' if check else 'The database check above failed'
                self.assert_nothing_changed(result, message)
                self.assertEqual(self.step_names(), ['pull', 'pull', 'staged preflight upgrade-check'])

    def test_the_release_the_directory_already_runs_is_refused(self):
        self.deployed(RELEASE, REVISION)
        result = subprocess.run(['bash', str(SCRIPT), '--upgrade', '--dir', str(self.deploy_dir), '--non-interactive'],
                                env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'already runs {RELEASE}. Run deploy-app.sh without --upgrade', result.stdout)
        self.assertEqual(self.step_names(), [])

    def test_a_first_deployment_that_never_used_its_database_is_refused(self):
        self.deployed(OLD_RELEASE, OLD_REVISION, unverified=True)
        result = self.run_script()
        self.assert_nothing_changed(result, 'so it has no data to keep')
        self.assertEqual(self.step_names(), [])

    def test_upgrade_needs_a_deployment_and_its_own_options(self):
        empty = self.base / 'empty'
        empty.mkdir()
        cases = ((['--upgrade', '--dir', str(empty)], 'has none (no .deployment-identity)'),
                 (['--upgrade', '--dir', str(self.deploy_dir), '--init-db'], 'cannot be combined with --init-db'),
                 (['--upgrade', '--dir', str(self.deploy_dir), '--check-config'], 'cannot be combined with --init-db or --check-config'),
                 (['--upgrade', '--dir', str(self.deploy_dir), '--backup-dir', 'relative'], '--backup-dir must be an absolute'),
                 (['--dir', str(self.deploy_dir), '--backup-dir', str(self.base)], '--backup-dir applies only to --upgrade'))
        for args, message in cases:
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(SCRIPT), *args, '--non-interactive'], env=self.env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.calls(), [])
        (self.deploy_dir / 'app.env').unlink()
        result = self.run_script()
        self.assertIn('reads the settings of the deployment from', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_plain_run_in_a_directory_of_another_release_points_to_upgrade(self):
        result = subprocess.run(['bash', str(SCRIPT), '--dir', str(self.deploy_dir), '--non-interactive'],
                                env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'runs {OLD_RELEASE}. To move it to {RELEASE} and keep its data, run deploy-app.sh --upgrade', result.stdout)
        self.assertEqual(self.step_names(), [])
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)

    # ── a backup that fails ─────────────────────────────────────────────────
    BACKUP_STEPS = ['pull', 'pull', 'staged preflight upgrade-check', 'staged backup size', 'running?', 'stop web odoo',
                    'staged pg_dump', 'staged pg_restore --list', 'staged filestore archive']

    def test_a_failed_backup_stops_before_the_switch_and_starts_the_old_app_again(self):
        for fake, last, message in (({'dump_exit': 1}, 'staged pg_dump', 'pg_dump of database perodua failed'),
                                    ({'list_table': 'res_partner'}, 'staged pg_restore --list', 'cannot be read back, or holds no Odoo database'),
                                    ({'tar_exit': 2}, 'staged filestore archive', 'The archive of the attachments volume')):
            with self.subTest(fake=fake):
                self.log.unlink(missing_ok=True)
                for key in ('FAKE_DUMP_EXIT', 'FAKE_LIST_TABLE', 'FAKE_TAR_EXIT'):
                    self.env.pop(key, None)
                result = self.run_script(**fake)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assertIn(f'The upgrade stopped before the switch: {self.deploy_dir} still runs {OLD_RELEASE}', result.stdout)
                self.assertIn(f'The App of {OLD_RELEASE} runs again.', result.stdout)
                steps = self.step_names()
                self.assertEqual(steps[:-1], self.BACKUP_STEPS[:self.BACKUP_STEPS.index(last) + 1])
                # The old App, from its own compose file, without recreating it.
                self.assertEqual(steps[-1], 'restart')
                restart = [e['args'] for e in self.calls() if e['args'][5:6] == ['up']][0]
                self.assertEqual(restart[3:], ['--file', f'{self.deploy_dir}/compose.yml', 'up', '--detach',
                                               '--no-recreate', '--wait', '--wait-timeout', '600'])
                self.assertEqual({release for _, release in self.steps()}, {OLD_RELEASE})
                self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
                self.assertEqual((self.deploy_dir / 'compose.yml').read_text(), f'# the compose file of {OLD_RELEASE}\n')
                # The incomplete backup is not left behind.
                self.assertEqual(self.backups(), [])

    def test_an_app_that_was_not_running_stays_stopped_after_a_failed_backup(self):
        result = self.run_script(running='0', dump_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Its App was not running before the upgrade, and stays stopped.', result.stdout)
        self.assertNotIn('restart', self.step_names())

    def test_an_old_app_that_does_not_start_again_is_named(self):
        result = self.run_script(dump_exit=1, up_exit=1)
        self.assertIn(f'did not start again. Start it with: sudo bash service.sh --role app --dir {self.deploy_dir} start',
                      result.stdout)

    def test_a_lost_ssh_session_during_the_backup_starts_the_old_app_again(self):
        # SIGHUP while pg_dump runs: to the whole process group (the terminal
        # hangs up, the dump dies with it), or to the script only (sudo passes it
        # on; the dump still ends). With stderr that takes nothing more (the
        # terminal is gone), the old App must still be started again. The
        # address of the sign-in host: a run that warns about the hand-over
        # would stop at that warning, on /dev/full, before the backup starts.
        self.write_app_env(PUBLIC_BASE_URL='https://stgiss.perodua.com.my')
        for to_group, stderr in ((True, subprocess.STDOUT), (False, subprocess.STDOUT), (True, 'full')):
            with self.subTest(to_group=to_group, stderr=stderr):
                self.log.unlink(missing_ok=True)
                shutil.rmtree(self.deploy_dir / 'backups', ignore_errors=True)
                marker = self.base / 'dumping'
                marker.unlink(missing_ok=True)
                env = dict(self.env, FAKE_DUMP_HANG=str(marker), FAKE_DUMP_SECONDS='30' if to_group else '1')
                if stderr == 'full':
                    stderr = open('/dev/full', 'wb')
                    self.addCleanup(stderr.close)
                process = subprocess.Popen(['bash', str(SCRIPT), '--upgrade', '--dir', str(self.deploy_dir), '--non-interactive'],
                                           env=env, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
                for _ in range(300):
                    if marker.exists() or process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertTrue(marker.exists(), 'pg_dump never started')
                if to_group:
                    os.killpg(process.pid, signal.SIGHUP)
                else:
                    os.kill(process.pid, signal.SIGHUP)
                output = process.communicate(timeout=60)[0].decode()
                self.assertEqual(process.returncode, 129, output)
                steps = self.step_names()
                self.assertEqual(steps[:-1], self.BACKUP_STEPS[:self.BACKUP_STEPS.index('staged pg_dump') + 1])
                self.assertEqual(steps[-1], 'restart')  # the old App, from its own compose file
                self.assertEqual(self.backups(), [])  # the incomplete backup is not left behind
                self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
                if stderr == subprocess.STDOUT:
                    self.assertIn(f'The upgrade stopped before the switch: {self.deploy_dir} still runs {OLD_RELEASE}', output)
                    self.assertIn(f'The App of {OLD_RELEASE} runs again.', output)

    # ── the upgrade ─────────────────────────────────────────────────────────
    def test_the_backup_comes_before_the_switch(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        steps = self.steps()
        self.assertEqual([name for name, _ in steps], self.BACKUP_STEPS + [
            'preflight check --accept-pending', 'filestore check', 'stop web odoo', 'preflight public-urls', 'up', 'http verify'])
        switch = [name for name, _ in steps].index('preflight check --accept-pending')
        # Up to the switch, the directory named the old release and its own
        # compose file was used only to see and stop its App.
        self.assertEqual({release for _, release in steps[:switch]}, {OLD_RELEASE})
        self.assertEqual({release for _, release in steps[switch:]}, {RELEASE})
        self.assertEqual([name for name, _ in steps[:switch] if not name.startswith(('staged', 'pull'))],
                         ['running?', 'stop web odoo'])
        self.assert_never_upgrades_modules()
        for entry in self.calls():
            self.assertFalse(any('fixture-password' in a for a in entry['args']), entry['args'])
        self.assertIn(f'Deployment verified: {RELEASE} ({REVISION})', result.stdout)
        self.assertIn(f'Upgraded from {OLD_RELEASE}. The data as it was before: {self.backups()[0]}', result.stdout)

    def test_the_identity_names_the_new_release_and_nothing_else_changes(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        identity = (self.deploy_dir / '.deployment-identity').read_text()
        self.assertEqual(identity, self.identity.replace(f'release={OLD_RELEASE}', f'release={RELEASE}')
                                                .replace(f'revision={OLD_REVISION}', f'revision={REVISION}'))
        self.assertEqual((self.deploy_dir / 'app.env').read_text(), self.app_env)
        compose = (self.deploy_dir / 'compose.yml').read_text()
        self.assertIn(f'image: {ODOO_IMAGE}', compose)
        self.assertIn('  filestore:', compose)  # the same attachments volume
        self.assertEqual((self.deploy_dir / 'secrets' / 'db_password').read_text(), 'fixture-password')

    def test_the_backup_holds_the_database_the_attachments_and_the_way_back(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        [backup] = self.backups()
        self.assertRegex(backup.name, rf'^\d{{8}}T\d{{6}}Z-{re.escape(OLD_RELEASE)}$')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / 'database.dump').read_bytes(), DUMP)
        with tarfile.open(backup / 'filestore.tar.gz') as archive:
            self.assertEqual(len([m for m in archive.getmembers() if m.isfile()]), 3)
        self.assertEqual((backup / 'deployment-identity').read_text(), self.identity)
        self.assertEqual((backup / 'app.env').read_text(), self.app_env)
        self.assertIn(f'Backup complete in {backup}: database.dump ({len(DUMP)} bytes), filestore.tar.gz (3 files).',
                      result.stdout)
        hint = (backup / 'restore.txt').read_text()
        self.assertIn(hint, result.stdout)  # printed as well
        for line in ('sudo bash service.sh --role app --dir ' + str(self.deploy_dir) + ' stop',
                     'odoo python3 - < reset_database.py',
                     'pg_restore --no-owner --no-acl --file=-',
                     'psql -X -q --output=/dev/null --no-password -v ON_ERROR_STOP=1 --single-transaction',
                     f'< {backup}/database.dump',
                     f'-v {backup}/filestore.tar.gz:/restore.tar.gz:ro odoo',
                     # sessions ended after the backup must not come back with its data
                     'rm -rf "/var/lib/odoo/filestore/$DB_NAME" /var/lib/odoo/sessions;',
                     'Every user must sign in again.',
                     # the way back works from any scripts folder of the old release
                     f'sudo cp -p {backup}/deployment-identity {self.deploy_dir}/.deployment-identity',
                     f'sudo cp -p {backup}/app.env {self.deploy_dir}/app.env',
                     f'Then, from a scripts folder of {OLD_RELEASE}: sudo bash deploy-app.sh --dir {self.deploy_dir}'):
            self.assertIn(line, hint)
        self.assertNotIn('deploy-app.sh --upgrade', hint)
        self.assertNotIn('fixture-password', hint)

    def test_the_backup_runs_keep_no_container_log(self):
        # pg_dump and tar stream the backup to standard output; Docker's json-file
        # log would keep another copy of it, about four times as large.
        self.assertEqual(self.run_script().returncode, 0)
        for entry in self.calls():
            args = entry['args']
            if args[:1] != ['compose']:
                continue
            action = args[5 + 2 * len(entry['overrides']):]
            streams = action[:1] == ['run'] and any('pg_dump' in a or 'tar -czf' in a for a in action)
            self.assertEqual(entry['overrides'], ['services:\n  odoo:\n    logging:\n      driver: none\n'] if streams else [],
                             action)
        self.assertNotIn('logging', (self.deploy_dir / 'compose.yml').read_text())  # the App keeps its logs

    def test_a_backup_that_does_not_fit_is_refused_before_the_app_stops(self):
        result = self.run_script(db_kb=str(2 ** 50), fs_kb='1024')
        self.assert_nothing_changed(result, 'Not enough room for the backup on the disk of')
        self.assertIn('give --backup-dir on a disk with more room', result.stdout)
        self.assertEqual(self.step_names(), ['pull', 'pull', 'staged preflight upgrade-check', 'staged backup size'])
        # a size it cannot read stops it as well
        self.log.unlink()
        result = self.run_script(db_kb='', fs_kb='1024')
        self.assert_nothing_changed(result, 'Cannot read the size of database perodua and of its attachments')

    def test_a_backup_folder_elsewhere(self):
        elsewhere = self.base / 'backups' / 'perodua'
        result = self.run_script('--backup-dir', str(elsewhere))
        self.assertEqual(result.returncode, 0, result.stdout)
        [backup] = self.backups(elsewhere)
        self.assertEqual((backup / 'database.dump').read_bytes(), DUMP)
        self.assertEqual(self.backups(), [])

    # ── a failure after the switch ──────────────────────────────────────────
    def test_a_failure_after_the_switch_says_where_the_server_is_and_the_way_back(self):
        for fake, state in (({'public_urls_exit': 1}, 'The App is stopped.'),
                            ({'http_exit': 1}, f'Its containers were recreated from the {RELEASE} images.')):
            with self.subTest(fake=fake):
                self.deployed(OLD_RELEASE, OLD_REVISION)
                shutil.rmtree(self.deploy_dir / 'backups', ignore_errors=True)
                self.log.unlink(missing_ok=True)
                for key in ('FAKE_PUBLIC_URLS_EXIT', 'FAKE_HTTP_EXIT'):
                    self.env.pop(key, None)
                result = self.run_script(**fake)
                self.assertNotEqual(result.returncode, 0)
                backup = self.backups()[-1]
                self.assertIn(f'The upgrade from {OLD_RELEASE} to {RELEASE} stopped after the switch. Now:', result.stdout)
                self.assertIn(f'{self.deploy_dir} names {RELEASE}', result.stdout)
                self.assertIn(state, result.stdout)
                self.assertIn(f'run sudo bash deploy-app.sh --dir {self.deploy_dir} from this scripts folder, without --upgrade',
                              result.stdout)
                self.assertIn(f'To go back to {OLD_RELEASE}: put back its identity and settings, then deploy it without --upgrade:\n'
                              f'   sudo cp -p {backup}/deployment-identity {self.deploy_dir}/.deployment-identity\n'
                              f'   sudo cp -p {backup}/app.env {self.deploy_dir}/app.env\n'
                              f'   Then, from a scripts folder of {OLD_RELEASE}: sudo bash deploy-app.sh --dir {self.deploy_dir}\n',
                              result.stdout)
                self.assertIn(f'follow {backup}/restore.txt instead', result.stdout)
                self.assertTrue((backup / 'database.dump').is_file())
                self.assertIn(f'release={RELEASE}\n', (self.deploy_dir / '.deployment-identity').read_text())
                self.assert_never_upgrades_modules()

    def test_the_way_back_works_from_a_scripts_folder_without_upgrade(self):
        # As v1.0.3: its deploy-app.sh has no --upgrade and refuses a directory
        # whose identity names another release.
        result = self.run_script(public_urls_exit=1)
        self.assertNotEqual(result.returncode, 0)
        # restore.txt (printed after the backup) and the failure message give the same copies
        copies = re.findall(r'^   sudo (cp -p \S+ \S+)$', result.stdout, re.M)
        self.assertEqual(len(copies), 4, result.stdout)
        self.assertEqual(copies[:2], copies[2:])
        copies = copies[2:]
        old = self.base / 'old'
        old.mkdir()
        (old / 'deploy-app.sh').write_text(re.sub(r'^REVISION=\S+$', f'REVISION={OLD_REVISION}',
                                                  re.sub(r'^RELEASE=\S+$', f'RELEASE={OLD_RELEASE}', TEXT, flags=re.M),
                                                  flags=re.M))
        for helper in ('uat_guard.py', 'uat_admins.py'):
            (old / helper).write_bytes((SCRIPTS / helper).read_bytes())
        plain = ['bash', str(old / 'deploy-app.sh'), '--dir', str(self.deploy_dir), '--non-interactive']
        self.env.pop('FAKE_PUBLIC_URLS_EXIT')
        refused = subprocess.run(plain, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(refused.returncode, 0)  # before the copies
        for copy in copies:
            subprocess.run(copy.split(), check=True)
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
        self.assertEqual((self.deploy_dir / 'app.env').read_text(), self.app_env)
        result = subprocess.run(plain, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f'Deployment verified: {OLD_RELEASE} ({OLD_REVISION})', result.stdout)
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)

    def test_going_back_is_an_upgrade_to_the_old_release(self):
        # After the switch, the directory names this release; a scripts folder
        # of another release with the same modules moves it there again.
        self.assertEqual(self.run_script().returncode, 0)
        newer = 'client-stable-uiux-v9.9.9'
        copy = self.base / 'newer'
        copy.mkdir()
        (copy / 'deploy-app.sh').write_text(re.sub(r'^RELEASE=\S+$', f'RELEASE={newer}', TEXT, flags=re.M))
        for helper in ('uat_guard.py', 'uat_admins.py'):
            (copy / helper).write_bytes((SCRIPTS / helper).read_bytes())
        self.log.unlink()
        result = subprocess.run(['bash', str(copy / 'deploy-app.sh'), '--upgrade', '--dir', str(self.deploy_dir),
                                 '--non-interactive'], env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f'release={newer}\n', (self.deploy_dir / '.deployment-identity').read_text())
        self.assertEqual(len(self.backups()), 2)
        self.assertTrue(self.backups()[-1].name.endswith('-' + RELEASE))


if __name__ == '__main__':
    unittest.main()
