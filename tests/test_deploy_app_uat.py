"""deploy-app.sh orchestration of the fresh UAT path, against a scripted fake Docker.

Run on Linux/WSL: python3 -m unittest discover -s tests -v
The fake answers each container command the way the real one would report,
so these tests pin the ORDER and the ABSENCE of steps: what runs before the
database is touched, what never runs on an initialized database, and that no
module upgrade (-u) is ever issued. The real guard, ORM script and Odoo are
exercised by the acceptance run, not here.
"""

import http.server
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse

from test_deploy_app import service_environment


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SCRIPT = SCRIPTS / 'deploy-app.sh'
FRESH_MODULES = ('perodua_client_stable,perodua_gateway,perodua_forecast_workbook,'
                 'perodua_supplier_execution,perodua_uiux_api')
SUPPORT_LINE = re.compile(r'^PUBLIC_ROOT_SUPPORTED=[01]$', re.M)
MAIL_LINE = re.compile(r'^MAIL_SUPPORTED=[01]$', re.M)
BASE_URL = 'https://stgissrp.perodua.com.my/dev'


def release_copy(folder, supported):
    """deploy-app.sh and its helpers in another folder, as a release whose web
    image does (1) or does not (0) serve the page under PUBLIC_ROOT."""
    folder.mkdir()
    text = SCRIPT.read_text(encoding='utf-8')
    assert len(SUPPORT_LINE.findall(text)) == 1, 'deploy-app.sh must set PUBLIC_ROOT_SUPPORTED once'
    (folder / 'deploy-app.sh').write_text(SUPPORT_LINE.sub(f'PUBLIC_ROOT_SUPPORTED={int(supported)}', text),
                                          encoding='utf-8')
    for helper in ('uat_guard.py', 'uat_admins.py'):
        shutil.copy(SCRIPTS / helper, folder / helper)
    return folder / 'deploy-app.sh'


# The tests pin these fixture values in a copy of deploy-app.sh, so that they
# do not depend on the grey release that this kit version pins.
DEMO_PINS = {
    'DEMO_RELEASE': 'demo-uiux-grey-v0.99.0',
    'DEMO_REVISION': '9' * 40,
    'DEMO_ODOO_IMAGE': 'perodua-deploy.novutal.com/perodua-odoo:demo-uiux-grey-v0.99.0@sha256:' + 'd' * 64,
    'DEMO_WEB_IMAGE': 'perodua-deploy.novutal.com/perodua-odoo:demo-uiux-grey-web-v0.99.0@sha256:' + 'e' * 64,
}
# The demo-uiux block of the kit versions before the first pin of a grey release.
PLACEHOLDER_PINS = {
    'DEMO_RELEASE': 'demo-uiux-grey-vX.Y.Z',
    'DEMO_REVISION': 'PLACEHOLDER-the-perodua-odoo-commit-of-the-first-grey-release',
    'DEMO_ODOO_IMAGE': 'perodua-deploy.novutal.com/perodua-odoo:demo-uiux-grey-vX.Y.Z@sha256:PLACEHOLDER-digest-of-the-first-pin',
    'DEMO_WEB_IMAGE': 'perodua-deploy.novutal.com/perodua-odoo:demo-uiux-grey-web-vX.Y.Z@sha256:PLACEHOLDER-digest-of-the-first-pin',
}
DEMO_MODULES = re.search(r'^DEMO_INIT_MODULES=(\S+)$', SCRIPT.read_text(encoding='utf-8'), re.M).group(1)


def demo_release_copy(folder, pins=DEMO_PINS):
    """deploy-app.sh and its helpers in another folder, as a kit version whose
    demo-uiux block pins ``pins`` (the fixture release DEMO_PINS)."""
    folder.mkdir()
    text = SCRIPT.read_text(encoding='utf-8')
    for key, value in pins.items():
        line = re.compile(rf'^{key}=\S+$', re.M)
        assert len(line.findall(text)) == 1, f'deploy-app.sh must set {key} once'
        text = line.sub(f'{key}={value}', text)
    (folder / 'deploy-app.sh').write_text(text, encoding='utf-8')
    for helper in ('uat_guard.py', 'uat_admins.py', 'uat_demo.py'):
        shutil.copy(SCRIPTS / helper, folder / helper)
    return folder / 'deploy-app.sh'

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
# FAKE_VOLUMES: the project's volumes that an uninstall left; FAKE_LEFTOVER:
# the attachments volume existed before this run.
if args[:2] == ['volume', 'ls']: reply(os.environ.get('FAKE_VOLUMES', ''))
if args[:2] == ['network', 'ls']: reply()
if args[:2] == ['volume', 'inspect']: reply(code=0 if os.environ.get('FAKE_LEFTOVER') else 1)
if args[:2] == ['volume', 'rm']: reply()
if args and args[0] == 'compose':
    if 'config' in args: reply()
    # FAKE_REPLACE_RUNNING: the container ids that ps of a compose project reports.
    if 'ps' in args: reply(os.environ.get('FAKE_REPLACE_RUNNING', ''))
    if 'stop' in args: reply()
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
            if mode == 'public-urls':
                code = int(os.environ.get('FAKE_PUBLIC_URLS_EXIT', '0'))
                reply('' if code else 'PUBLIC_URLS_SET', code, 'Database preflight: fixture' if code else '')
            reply({'mark-pending': 'PENDING', 'verify-fresh': 'VERIFIED 3',
                   'stamp': 'READY 3', 'stamp-upgrade': 'READY 3'}[mode])
        if cmd[:2] == ['python3', '/opt/deploy/uat_guard.py']:
            if cmd[2] == 'guard':
                code = int(os.environ.get('FAKE_GUARD_EXIT', '0'))
                reply(json.dumps({'verdict': 'accepted' if code == 0 else 'refused'}), code,
                      'UAT guard: ' + ('accepted' if code == 0 else 'REFUSED: fixture'))
            if cmd[2] == 'demo-flag':
                reply(os.environ.get('FAKE_DEMO_FLAG', '--without-demo=True'))
        if cmd[:1] == ['odoo']: reply('fake odoo init', int(os.environ.get('FAKE_INIT_EXIT', '0')))
        if cmd[:2] == ['bash', '-c'] and '/var/lib/odoo/filestore' in cmd[2]:
            reply(os.environ.get('FAKE_LEFTOVER_FILES', '0'))
        if cmd[:2] == ['bash', '-c'] and 'uat_demo.py' in cmd[2]:
            if 'PERODUA_UAT_DEMO_STEP=apply' in args:
                # The apply step: the plan on its standard input. It gives the
                # passwords (FAKE_DEMO_APPLIED, the database) and commits; with
                # FAKE_DEMO_APPLY_EXIT it then stops without its answer.
                rows = [line for line in sys.stdin.read().splitlines() if line.startswith('SEEDED_ACCOUNT\t')]
                with open(os.environ['FAKE_DEMO_APPLIED'], 'a', encoding='utf-8') as applied:
                    applied.writelines(row + '\n' for row in rows)
                code = int(os.environ.get('FAKE_DEMO_APPLY_EXIT', '0'))
                ready = 'UAT demo: ready: %d seeded accounts have a new random password' % len(rows)
                reply('' if code else ready, code, 'odoo shell log line')
            # The plan step: one line per new password, then its count.
            code = int(os.environ.get('FAKE_DEMO_EXIT', '0'))
            accounts = [line.split(':') for line in os.environ.get('FAKE_DEMO_ACCOUNTS', '').split()]
            lines = ['SEEDED_ACCOUNT\t%s\t%s\t%s' % (login, password, kind) for login, password, kind in accounts]
            planned = 'UAT demo: planned: %d' % int(os.environ.get('FAKE_DEMO_COUNT', len(lines)))
            reply('\n'.join(lines + ([] if code else [planned])), code, 'odoo shell log line')
        if cmd[:2] == ['bash', '-c'] and 'odoo shell' in cmd[2]:
            code = int(os.environ.get('FAKE_ADMINS_EXIT', '0'))
            reply('UAT admins: ready: whadmin, admin1, admin2' if code == 0 else 'boom', code)
        if cmd[:2] == ['python3', '-c']: reply()
        if cmd[:1] == ['perodua-mail-test']:
            reply('fake perodua-mail-test: one test email to ' + cmd[-1], int(os.environ.get('FAKE_MAIL_TEST_EXIT', '0')))
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
        self.values = {
            'DB_HOST': '192.0.2.20', 'DB_PORT': '5432', 'DB_NAME': 'perodua', 'DB_USER': 'odoo',
            'DB_PASSWORD_FILE': str(password), 'PROJECT_NAME': 'perodua-uat-fixture',
            'HTTP_PORT': '18110', 'BIND_IP': '127.0.0.1', 'STARTUP_TIMEOUT': '600',
            'INIT_TIMEOUT': '600'}
        self.configure()
        self.script = SCRIPT
        self.applied = base / 'demo-applied.tsv'
        self.env = dict(os.environ, HOME=str(base), DOCKER_CONFIG=str(base / 'docker-config'),
                        PATH=str(base / 'bin') + os.pathsep + os.environ['PATH'],
                        FAKE_DOCKER_LOG=str(self.log), FAKE_DEMO_APPLIED=str(self.applied))

    def configure(self, **updates):
        self.config.write_text(''.join(f'{k}={v}\n' for k, v in dict(self.values, **updates).items()),
                               encoding='utf-8')

    def serve_under_dev(self):
        """A release that supports PUBLIC_ROOT, configured for /dev."""
        self.script = release_copy(Path(self.temp.name) / 'release', supported=True)
        self.configure(PUBLIC_ROOT='/dev', ENVIRONMENT_LABEL='DEV', PUBLIC_BASE_URL=BASE_URL)

    def run_script(self, check, *extra, **fake):
        self.env['FAKE_CHECK'] = check
        self.env.update({f'FAKE_{k.upper()}': str(v) for k, v in fake.items()})
        return subprocess.run(['bash', str(self.script), '--config', str(self.config),
                               '--dir', str(self.deploy_dir), '--non-interactive', *extra],
                              env=self.env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def public_urls_arguments(self):
        """The arguments of each preflight public-urls call."""
        found = []
        for args in self.calls():
            if args[:1] == ['compose'] and 'run' in args:
                cmd = args[args.index('odoo', args.index('run')) + 1:]
                if cmd[:3] == ['python3', '/opt/deploy/preflight.py', 'public-urls']:
                    found.append(cmd[3:])
        return found

    def verification_arguments(self):
        """What the HTTP verification in the Odoo container is given, after python3 -."""
        found = []
        for args in self.calls():
            if args[:1] == ['compose'] and 'exec' in args:
                found.append(args[args.index('-', args.index('python3')) + 1:])
        return found

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
                elif cmd[:2] == ['bash', '-c'] and '/var/lib/odoo/filestore' in cmd[2]:
                    steps.append('leftover check')
                elif cmd[:2] == ['bash', '-c'] and 'uat_demo.py' in cmd[2]:
                    steps.append('odoo shell < uat_demo.py ' + ('apply' if 'PERODUA_UAT_DEMO_STEP=apply' in args else 'plan'))
                elif cmd[:2] == ['bash', '-c']:
                    steps.append('odoo shell < uat_admins.py' if 'uat_admins.py' in cmd[2] else 'bash')
                elif cmd[:2] == ['python3', '-c']:
                    steps.append('filestore check')
            elif 'stop' in args:
                steps.append('stop ' + ' '.join(args[args.index('stop') + 1:]))
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
        self.assertEqual(self.container_steps(), ['preflight check', 'stop web odoo', 'preflight public-urls', 'up', 'http verify fresh=0'])
        self.assert_never_upgrades_or_touches_admins()

    def test_init_db_does_not_reinitialize_a_ready_database(self):
        result = self.run_script('READY 2', '--init-db')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('initialization and module upgrades are skipped', result.stdout)
        self.assertEqual(self.container_steps(),
                         ['preflight check', 'filestore check', 'stop web odoo', 'preflight public-urls', 'up', 'http verify fresh=0'])
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
            'stop web odoo',
            'preflight public-urls',
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

    # ── attachments an earlier deployment of the project left ──────────────
    FRESH_STEPS = ['uat_guard guard', 'uat_guard demo-flag']

    def volume_removals(self):
        return [args for args in self.calls() if args[:2] == ['volume', 'rm']]

    def test_leftover_attachments_stop_a_new_system_without_a_terminal(self):
        result = self.run_script('EMPTY', '--init-db', leftover='1', leftover_files='3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('still holds 3 files from an earlier deployment', result.stdout)
        self.assertIn('sudo docker volume rm perodua-uat-fixture_filestore', result.stdout)
        self.assertIn('The database was not changed', result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check', 'leftover check'])
        self.assertEqual(self.volume_removals(), [])

    def test_a_leftover_volume_without_files_is_used_as_is(self):
        result = self.run_script('EMPTY', '--init-db', leftover='1', leftover_files='0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps()[:4], ['preflight check', 'leftover check', *self.FRESH_STEPS])
        self.assertEqual(self.volume_removals(), [])

    def test_leftover_attachments_are_deleted_only_after_a_yes(self):
        for answer, deleted in ((b'y', True), (b'', False)):
            with self.subTest(answer=answer):
                self.log.unlink(missing_ok=True)
                code, output = self.run_on_terminal(answer, 'EMPTY', leftover='1', leftover_files='3')
                self.assertIn('Delete perodua-uat-fixture_filestore and continue? [y/N]: ', output)
                if deleted:
                    self.assertEqual(code, 0, output)
                    self.assertIn('Deleted perodua-uat-fixture_filestore.', output)
                    self.assertEqual(self.volume_removals(), [['volume', 'rm', 'perodua-uat-fixture_filestore']])
                    self.assertEqual(self.container_steps()[:4], ['preflight check', 'leftover check', *self.FRESH_STEPS])
                else:
                    self.assertNotEqual(code, 0)
                    self.assertIn('Kept perodua-uat-fixture_filestore', output)
                    self.assertEqual(self.volume_removals(), [])
                    self.assertEqual(self.container_steps(), ['preflight check', 'leftover check'])

    def test_a_volume_an_uninstall_kept_is_used_again_with_the_same_database(self):
        # A new deployment directory; the project's attachments volume is still there.
        result = self.run_script('READY 2', volumes='perodua-uat-fixture_filestore', leftover='1')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Using the attachments volume perodua-uat-fixture_filestore that an earlier deployment', result.stdout)
        self.assertEqual(self.container_steps(), ['preflight check', 'filestore check', 'stop web odoo', 'preflight public-urls', 'up', 'http verify fresh=0'])
        self.assertEqual(self.volume_removals(), [])

    def test_other_resources_of_the_project_still_stop_a_new_directory(self):
        result = self.run_script('READY 2', volumes='perodua-uat-fixture_data')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PROJECT_NAME already owns Docker resources', result.stdout)
        self.assertEqual(self.container_steps(), [])

    def run_on_terminal(self, answer, check, **fake):
        """deploy-app.sh on a pseudo-terminal, answering the one question it asks."""
        import pty, select, time
        self.env['FAKE_CHECK'] = check
        self.env.update({f'FAKE_{k.upper()}': str(v) for k, v in fake.items()})
        pid, master = pty.fork()
        if pid == 0:
            os.execvpe('bash', ['bash', str(self.script), '--config', str(self.config),
                                '--dir', str(self.deploy_dir), '--init-db'], self.env)
        output, answered, deadline = b'', False, time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b''
                    output += chunk
                if not answered and b'and continue? [y/N]: ' in output:
                    os.write(master, answer + b'\n')
                    answered = True
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    while select.select([master], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output += chunk
                    return os.waitstatus_to_exitcode(status), output.decode(errors='replace')
            self.fail('deploy-app.sh did not finish: ' + output.decode(errors='replace'))
        finally:
            os.close(master)

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
            'filestore check', 'stop web odoo', 'preflight public-urls', 'up', 'http verify fresh=1', 'preflight stamp'])
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
            'preflight verify-fresh', 'filestore check', 'stop web odoo', 'preflight public-urls', 'up', 'http verify fresh=1', 'preflight stamp'])
        self.assertFalse(any(a == '-i' for args in self.calls() for a in args))

    def test_failed_sign_in_verification_is_not_stamped(self):
        result = self.run_script('EMPTY', '--init-db', http_exit=1)
        self.assertNotEqual(result.returncode, 0)
        steps = self.container_steps()
        self.assertEqual(steps[-1], 'http verify fresh=1')
        self.assertNotIn('preflight stamp', steps)

    # ── addresses recorded in the database, the page under a path ──────────
    PATHS = (('READY 0', ()), ('READY 2', ('--init-db',)), ('EMPTY', ('--init-db',)),
             ('SETUP_PENDING', ('--init-db',)), ('SETUP_UNMARKED', ('--init-db',)))

    def test_public_addresses_are_recorded_before_the_containers_start_on_every_path(self):
        self.serve_under_dev()
        for check, extra in self.PATHS:
            with self.subTest(check=check, extra=extra):
                self.log.unlink(missing_ok=True)
                result = self.run_script(check, *extra)
                self.assertEqual(result.returncode, 0, result.stdout)
                steps = self.container_steps()
                self.assertEqual(steps.count('preflight public-urls'), 1, steps)
                # Once the database is initialized, and right before the containers are recreated.
                self.assertEqual(steps.index('preflight public-urls'), steps.index('up') - 1, steps)
                # With the running App stopped while it is written.
                self.assertEqual(steps.index('stop web odoo'), steps.index('preflight public-urls') - 1, steps)
                if 'preflight verify-fresh' in steps:
                    self.assertLess(steps.index('preflight verify-fresh'), steps.index('preflight public-urls'))
                self.assertEqual(self.public_urls_arguments(), [[BASE_URL]])
                # The HTTP verification is given the path, the image profile
                # of the line, then whether the database is new.
                fresh = '0' if check.startswith('READY') else '1'
                self.assertEqual(self.verification_arguments()[0][2:], ['/dev', 'client-stable-uiux', fresh])

    def test_without_a_public_base_url_only_the_report_address_is_recorded(self):
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        # An empty argument: preflight writes report.url and leaves web.base.url alone.
        self.assertEqual(self.public_urls_arguments(), [['']])
        self.assertEqual(self.verification_arguments()[0][2:], ['', 'client-stable-uiux', '0'])

    def test_a_failure_to_record_the_addresses_stops_before_the_containers_start(self):
        for check, extra in (('READY 0', ()), ('EMPTY', ('--init-db',))):
            with self.subTest(check=check):
                self.log.unlink(missing_ok=True)
                result = self.run_script(check, *extra, public_urls_exit=1)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Could not record the report and public addresses', result.stdout)
                steps = self.container_steps()
                self.assertEqual(steps[-1], 'preflight public-urls')
                self.assertNotIn('up', steps)
                self.assertNotIn('preflight stamp', steps)

    def test_the_final_message_names_the_addresses_under_the_path(self):
        self.serve_under_dev()
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('HTTP URL: http://127.0.0.1:18110/dev/app/ (this server only)', result.stdout)
        self.assertIn('then open http://localhost:18110/dev/app/', result.stdout)
        self.assertIn(f'Public URL (through the front proxy or F5): {BASE_URL}/app/', result.stdout)
        self.log.unlink()
        self.configure(BIND_IP='10.1.2.3', PUBLIC_ROOT='/uat', PUBLIC_BASE_URL='')
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('HTTP URL: http://10.1.2.3:18110/uat/app/\n', result.stdout)
        self.assertNotIn('Public URL', result.stdout)

    def test_outgoing_email_adds_no_step(self):
        # The mail settings reach Odoo through compose.yml, which every run
        # recreates the containers with: no preflight mode and no step is new.
        # A release without the mail code says nothing about outgoing email.
        self.script = release_copy(Path(self.temp.name) / 'no-mail', supported=True)
        text = self.script.read_text(encoding='utf-8')
        self.assertEqual(len(MAIL_LINE.findall(text)), 1)
        self.script.write_text(MAIL_LINE.sub('MAIL_SUPPORTED=0', text), encoding='utf-8')
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        steps = self.container_steps()
        self.assertNotIn('Outgoing email', result.stdout)
        self.script = SCRIPT  # the pinned release has the mail code
        smtp_password = Path(self.temp.name) / 'smtp-password'
        smtp_password.write_text('fake-app-password\n', encoding='utf-8')
        self.configure(SMTP_HOST='smtp.example.com', SMTP_USER='sender@example.com', SMTP_PASSWORD_FILE=str(smtp_password),
                       SMTP_FROM='sender@example.com', MAIL_REDIRECT_TO='inbox@example.com')
        self.log.unlink()
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), steps)
        self.assertIn('Outgoing email: smtp.example.com:587 starttls, sign-in as sender@example.com, from sender@example.com,'
                      ' every email to the test address inbox@example.com.\n', result.stdout)
        self.assertIn(f'Send a test email with: sudo bash deploy-app.sh --send-test-mail ADDRESS --dir {self.deploy_dir}\n',
                      result.stdout)
        for args in self.calls():
            self.assertFalse(any('fake-app-password' in arg for arg in args), args)
        self.assertNotIn('fake-app-password', result.stdout)
        self.configure()
        self.log.unlink()
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), steps)
        self.assertIn(f'Outgoing email: off. Configure it with: sudo bash deploy-app.sh --mail --dir {self.deploy_dir}\n',
                      result.stdout)

    # ── generated deployment ────────────────────────────────────────────────
    def test_helpers_are_installed_read_only_and_mounted(self):
        self.run_script('READY 0')
        for helper in ('uat_guard.py', 'uat_admins.py'):
            path = self.deploy_dir / helper
            self.assertEqual(path.read_bytes(), (SCRIPTS / helper).read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        compose = (self.deploy_dir / 'compose.yml').read_text()
        self.assertIn('./uat_guard.py:/opt/deploy/uat_guard.py:ro', compose)
        self.assertIn('./uat_admins.py:/opt/deploy/uat_admins.py:ro', compose)
        self.assertIn('EXCLUDED_MODULE: "perodua_demo_client"', compose)
        self.assertIn(f'INIT_MODULES: "{FRESH_MODULES}"', compose)
        self.assertIn('RESTORED_MODULES: "perodua_client_stable,perodua_demo_client,', compose)

    def test_the_web_health_check_asks_for_the_internal_server(self):
        # From v1.0.4 only Host web reaches /app/ at the root path; 127.0.0.1
        # is a public host name there and gets the page under PUBLIC_ROOT.
        self.serve_under_dev()
        self.run_script('READY 0')
        compose = (self.deploy_dir / 'compose.yml').read_text()
        self.assertIn('test: ["CMD", "wget", "-q", "-O", "/dev/null", "--header", "Host: web", '
                      '"http://127.0.0.1/app/version.json"]', compose)

    def test_missing_helper_fails_before_docker(self):
        partial = Path(self.temp.name) / 'partial-release'
        partial.mkdir()
        (partial / 'deploy-app.sh').write_bytes(SCRIPT.read_bytes())
        (partial / 'uat_guard.py').write_bytes((SCRIPTS / 'uat_guard.py').read_bytes())
        self.env['FAKE_CHECK'] = 'READY 0'
        result = subprocess.run(['bash', str(partial / 'deploy-app.sh'), '--config', str(self.config),
                                 '--dir', str(self.deploy_dir), '--non-interactive'],
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uat_admins.py is missing', result.stdout)
        self.assertEqual(self.calls(), [])

    # ── the demo-uiux line ──────────────────────────────────────────────────
    # login:password:account, as uat_demo.py lists them. Obvious fakes.
    DEMO_ACCOUNTS = ('planner:FixturePw2345abcdEFGH:internal', 'portal.cust-001:FixturePw6789wxyzABCD:portal')

    def demo(self, **updates):
        """A kit version whose demo-uiux block pins the fixture release, and an
        app.env of that line."""
        folder = Path(self.temp.name) / 'demo-release'
        if not folder.exists():
            demo_release_copy(folder)
        self.script = folder / 'deploy-app.sh'
        self.configure(PERODUA_LINE='demo-uiux', **updates)

    def run_commands(self):
        """The commands of the one-off containers, after the service name."""
        return [args[args.index('odoo', args.index('run')) + 1:] for args in self.calls()
                if args[:1] == ['compose'] and 'run' in args]

    def demo_calls(self):
        return [args for args in self.calls() if args[:1] == ['compose'] and any('uat_demo.py' in a for a in args)]

    def seeded_rows(self):
        text = (self.deploy_dir / 'seeded-accounts.txt').read_text()
        return [line.split('\t') for line in text.splitlines() if not line.startswith('#')]

    def applied_rows(self):
        """login:password:account of each password that the fake apply step gave, in order."""
        text = self.applied.read_text() if self.applied.exists() else ''
        return [':'.join(line.split('\t')[1:]) for line in text.splitlines()]

    def assert_passwords_kept_secret(self, result, accounts=DEMO_ACCOUNTS, kept_in=('seeded-accounts.txt',)):
        """The passwords of the seeded accounts are only in the files kept_in of the directory."""
        # run_script gives the standard error in result.stdout.
        for password in (account.split(':')[1] for account in accounts):
            self.assertNotIn(password, result.stdout)
            for args in self.calls():
                self.assertFalse(any(password in arg for arg in args), args)
            for path in self.deploy_dir.rglob('*'):
                if path.is_file() and path.name not in kept_in:
                    self.assertNotIn(password, path.read_text(errors='replace'), path)

    def pending_rows(self):
        text = (self.deploy_dir / 'seeded-accounts.pending').read_text()
        return [':'.join(line.split('\t')[1:]) for line in text.splitlines() if not line.startswith('#')]

    def test_the_demo_init_list_is_the_one_of_the_entrypoint_and_the_hub(self):
        # docker/entrypoint.sh, DEFAULT_INIT_MODULES of the demo-uiux profile, and perodua_sso_hub.
        self.assertEqual(DEMO_MODULES.split(','), [
            'perodua_demo_ui', 'perodua_portal', 'perodua_portal_workspaces', 'perodua_agent_portal',
            'perodua_demo_client', 'perodua_gateway', 'perodua_forecast_workbook', 'perodua_demo_profile',
            'perodua_supplier_execution', 'perodua_uiux_api', 'perodua_sso_hub'])
        self.assertNotIn('perodua_client_stable', DEMO_MODULES)

    def test_the_real_deploy_app_has_one_plain_release_line_per_block(self):
        # service.sh reset reads these with sed; each block has one line of each.
        text = SCRIPT.read_text(encoding='utf-8')
        for block in ("", "DEMO_"):
            for name in ("RELEASE", "REVISION"):
                with self.subTest(line=block + name):
                    self.assertEqual(len(re.findall(rf"^{block}{name}=\S+$", text, re.M)), 1)

    def test_the_real_demo_block_pins_a_grey_release(self):
        text = SCRIPT.read_text(encoding='utf-8')
        pins = {key: re.search(rf'^{key}=(\S+)$', text, re.M).group(1) for key in DEMO_PINS}
        version = re.fullmatch(r'demo-uiux-grey-(v\d+\.\d+\.\d+)', pins['DEMO_RELEASE']).group(1)
        self.assertRegex(pins['DEMO_REVISION'], r'^[0-9a-f]{40}$')
        for key, tag in (('DEMO_ODOO_IMAGE', 'demo-uiux-grey-'), ('DEMO_WEB_IMAGE', 'demo-uiux-grey-web-')):
            with self.subTest(image=key):
                self.assertRegex(pins[key], rf'^perodua-deploy\.novutal\.com/perodua-odoo:{tag}{re.escape(version)}'
                                            r'@sha256:[0-9a-f]{64}$')
        self.assertNotEqual(pins['DEMO_ODOO_IMAGE'].split('@')[1], pins['DEMO_WEB_IMAGE'].split('@')[1])
        # The settings of the line pass the pin check, and --check-config runs no Docker.
        self.configure(PERODUA_LINE='demo-uiux')
        result = self.run_script('EMPTY', '--check-config')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f'Configuration valid for {pins["DEMO_RELEASE"]}. Nothing was changed.', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_real_demo_block_upgrades_from_the_earlier_grey_releases(self):
        text = SCRIPT.read_text(encoding='utf-8')
        release = re.search(r'^DEMO_RELEASE=(\S+)$', text, re.M).group(1)
        sources = re.search(r'^DEMO_MODULE_UPGRADE_FROM=(\S*)$', text, re.M).group(1).split(',')
        accepted = re.search(r'^DEMO_OLD_RELEASES_ACCEPTED=(\S*)$', text, re.M).group(1).split(',')
        # From the second pin on: the fingerprints and the names of the earlier grey releases.
        for fingerprint in sources:
            self.assertRegex(fingerprint, r'^[0-9a-f]{32}$')
        for name in accepted:
            self.assertRegex(name, r'^demo-uiux-grey-v\d+\.\d+\.\d+$')
            self.assertNotEqual(name, release)

    def test_the_demo_line_stops_while_its_block_holds_placeholders(self):
        self.script = demo_release_copy(Path(self.temp.name) / 'placeholders', PLACEHOLDER_PINS)
        self.configure(PERODUA_LINE='demo-uiux')
        for extra in ((), ('--init-db',), ('--check-config',)):
            with self.subTest(extra=extra):
                result = self.run_script('EMPTY', *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('This scripts folder pins no release of the demo-uiux line yet', result.stdout)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.deploy_dir.exists())

    def test_an_unknown_line_stops_before_docker(self):
        result = self.run_script('READY 0', '--line', 'demo')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown line demo: the line is client-stable-uiux or demo-uiux', result.stdout)
        self.assertEqual(self.calls(), [])
        self.configure(PERODUA_LINE='demo-uiux')
        result = self.run_script('READY 0', '--line', 'client-stable-uiux')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--line client-stable-uiux and PERODUA_LINE=demo-uiux in', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_fresh_demo_database_gets_the_demo_seed_and_random_passwords(self):
        self.demo()
        result = self.run_script('EMPTY', '--init-db', demo_accounts=' '.join(self.DEMO_ACCOUNTS))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), [
            'preflight check', 'uat_guard guard', 'uat_guard demo-flag',
            f'odoo -c /etc/odoo/odoo.conf -d perodua -i {DEMO_MODULES} --without-demo=True --stop-after-init',
            'preflight mark-pending', 'odoo shell < uat_admins.py', 'odoo shell < uat_demo.py plan',
            'odoo shell < uat_demo.py apply', 'preflight verify-fresh', 'filestore check', 'stop web odoo',
            'preflight public-urls', 'up', 'http verify fresh=1', 'preflight stamp'])
        self.assertIn('Initializing NEW UAT database perodua of the demo-uiux line, with its demo seed', result.stdout)
        # The guard keeps the client menu profile out, with the reviews of this line.
        [guard] = [cmd for cmd in self.run_commands() if cmd[:3] == ['python3', '/opt/deploy/uat_guard.py', 'guard']]
        self.assertEqual(guard[guard.index('--exclude') + 1], 'perodua_client_stable')
        self.assertEqual(guard[guard.index('--line') + 1], 'demo-uiux')
        # uat_demo.py runs twice in the install job, the plan and the apply
        # step, without a Docker log. The apply step gets the plan on file
        # descriptor 3, the standard input of the container.
        plan, apply = self.demo_calls()
        for demo, step in ((plan, 'plan'), (apply, 'apply')):
            self.assertIn('PERODUA_UAT_DEMO=install', demo)
            self.assertIn('PERODUA_UAT_DEMO_STEP=' + step, demo)
            self.assertTrue(any(arg.endswith('/no-log.yml') for arg in demo), demo)
            self.assertTrue(demo[-1].startswith('exec 3<&0; '), demo)
        self.assertEqual(self.applied_rows(), list(self.DEMO_ACCOUNTS))
        self.assertFalse((self.deploy_dir / 'seeded-accounts.pending').exists())
        self.assertEqual(self.verification_arguments()[0][2:], ['', 'demo-uiux', '1'])
        # Only root may read the list of the new passwords, and nothing else shows them.
        accounts = self.deploy_dir / 'seeded-accounts.txt'
        self.assertEqual(accounts.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.seeded_rows(), [account.split(':') for account in self.DEMO_ACCOUNTS])
        self.assert_passwords_kept_secret(result)
        self.assertIn(f'Seeded accounts: 2 got a new random password. They are listed in {self.deploy_dir}/seeded-accounts.txt',
                      result.stdout)
        self.assertIn('odoo shell log line', (self.deploy_dir / 'uat-demo.log').read_text())
        self.assertIn('UAT administrators (UAT only, fixed password): whadmin, admin1, admin2 / perodua', result.stdout)
        self.assertIn('Portal mode: off', result.stdout)
        # The directory names its line.
        self.assertIn('line=demo-uiux\n', (self.deploy_dir / '.deployment-identity').read_text())
        self.assertTrue((self.deploy_dir / 'app.env').read_text().startswith('PERODUA_LINE=demo-uiux\n'))
        helper = self.deploy_dir / 'uat_demo.py'
        self.assertEqual(helper.read_bytes(), (SCRIPTS / 'uat_demo.py').read_bytes())
        self.assertEqual(helper.stat().st_mode & 0o777, 0o444)

    def test_a_failed_demo_setup_is_not_stamped_and_keeps_no_password(self):
        self.demo()
        result = self.run_script('EMPTY', '--init-db', demo_accounts=' '.join(self.DEMO_ACCOUNTS), demo_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'The UAT setup of the demo-uiux line failed; see {self.deploy_dir}/uat-demo.log', result.stdout)
        steps = self.container_steps()
        self.assertEqual(steps[-1], 'odoo shell < uat_demo.py plan')
        self.assertFalse((self.deploy_dir / 'seeded-accounts.txt').exists())
        # The plan step changes nothing, so a failed plan keeps no record.
        self.assertFalse((self.deploy_dir / 'seeded-accounts.pending').exists())
        self.assertEqual(self.applied_rows(), [])
        self.assert_passwords_kept_secret(result)
        # A count that differs from the list is refused as well; the rerun
        # finishes the interrupted setup.
        self.log.unlink()
        result = self.run_script('SETUP_PENDING', '--init-db', demo_exit=0, demo_count=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reported 3 new passwords and listed 2', result.stdout)
        self.assertFalse((self.deploy_dir / 'seeded-accounts.txt').exists())
        self.assertFalse((self.deploy_dir / 'seeded-accounts.pending').exists())
        self.log.unlink()
        result = self.run_script('SETUP_PENDING', '--init-db', demo_count=2)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps()[:4], ['preflight check', 'odoo shell < uat_admins.py',
                                                      'odoo shell < uat_demo.py plan', 'odoo shell < uat_demo.py apply'])
        self.assertEqual(len(self.seeded_rows()), 2)

    def test_after_a_module_upgrade_an_account_with_the_seed_password_gets_a_new_one(self):
        self.demo()
        self.assertEqual(self.run_script('EMPTY', '--init-db', demo_accounts=self.DEMO_ACCOUNTS[0]).returncode, 0)
        added = 'persona.new:FixturePw8888zzzzYYYY:portal'
        self.log.unlink()
        result = self.run_script('UPGRADE_PENDING 3', demo_accounts=added)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps(), [
            'preflight check', 'odoo shell < uat_demo.py plan', 'odoo shell < uat_demo.py apply', 'filestore check',
            'stop web odoo', 'preflight public-urls', 'up', 'http verify fresh=0', 'preflight stamp-upgrade'])
        for demo in self.demo_calls():
            self.assertIn('PERODUA_UAT_DEMO=upgrade', demo)
        self.assertEqual(self.seeded_rows(), [self.DEMO_ACCOUNTS[0].split(':'), added.split(':')])
        self.assertIn(f'Seeded accounts: 1 more, each with a new random password, in {self.deploy_dir}/seeded-accounts.txt',
                      result.stdout)
        self.assertIn('README.md, "Upgrade the demo-uiux line", lists it', result.stdout)
        self.assert_passwords_kept_secret(result, (self.DEMO_ACCOUNTS[0], added))
        # None left: the list stays as it is.
        before = (self.deploy_dir / 'seeded-accounts.txt').read_text()
        self.log.unlink()
        result = self.run_script('UPGRADE_PENDING 3', demo_accounts='')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.deploy_dir / 'seeded-accounts.txt').read_text(), before)
        self.assertNotIn('Seeded accounts:', result.stdout)
        self.assertNotIn('odoo shell < uat_demo.py apply', self.container_steps())

    def test_passwords_given_before_a_stop_are_listed_by_the_rerun(self):
        # The apply step of an upgrade commits a new password, then the run
        # stops before seeded-accounts.txt lists it (a stop right after the
        # commit: no answer, exit 137). Its rerun chooses only the accounts
        # that still have the seed password, so it cannot choose this one
        # again: the record seeded-accounts.pending must give it.
        self.demo()
        self.assertEqual(self.run_script('EMPTY', '--init-db', demo_accounts=self.DEMO_ACCOUNTS[0]).returncode, 0)
        before = (self.deploy_dir / 'seeded-accounts.txt').read_text()
        added = 'persona.new:FixturePw8888zzzzYYYY:portal'
        pending = self.deploy_dir / 'seeded-accounts.pending'
        self.log.unlink()
        self.applied.unlink()
        result = self.run_script('UPGRADE_PENDING 3', demo_accounts=added, demo_apply_exit=137)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.container_steps(), ['preflight check', 'odoo shell < uat_demo.py plan',
                                                  'odoo shell < uat_demo.py apply'])
        self.assertEqual(self.applied_rows(), [added])
        self.assertIn(f'The new passwords stay in {pending} (root only) until a run lists them. Run deploy-app.sh again',
                      result.stdout)
        # The record stays, root only, after the cleanup; nothing else has the password.
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pending_rows(), [added])
        self.assertEqual((self.deploy_dir / 'seeded-accounts.txt').read_text(), before)
        self.assert_passwords_kept_secret(result, (added,), kept_in=('seeded-accounts.pending',))
        # The rerun gives the same password again first, lists it and removes
        # the record; then its own plan finds no account.
        self.log.unlink()
        self.applied.unlink()
        result = self.run_script('UPGRADE_PENDING 3', demo_accounts='', demo_apply_exit=0)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('An earlier run gave seeded accounts new passwords and stopped before it listed them in '
                      f'{self.deploy_dir}/seeded-accounts.txt', result.stdout)
        self.assertEqual(self.container_steps()[:3], ['preflight check', 'odoo shell < uat_demo.py apply',
                                                      'odoo shell < uat_demo.py plan'])
        self.assertEqual(self.container_steps().count('odoo shell < uat_demo.py apply'), 1)
        self.assertEqual(self.applied_rows(), [added])
        self.assertEqual(self.seeded_rows(), [self.DEMO_ACCOUNTS[0].split(':'), added.split(':')])
        self.assertFalse(pending.exists())
        self.assertIn(f'Seeded accounts: 1 more, each with a new random password, in {self.deploy_dir}/seeded-accounts.txt',
                      result.stdout)
        self.assert_passwords_kept_secret(result, (self.DEMO_ACCOUNTS[0], added))
        # A stop after the list, before the record went: the rerun lists no row twice.
        pending.write_text('# record\nSEEDED_ACCOUNT\t' + added.replace(':', '\t') + '\n')
        pending.chmod(0o600)
        self.log.unlink()
        result = self.run_script('UPGRADE_PENDING 3', demo_accounts='')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.seeded_rows(), [self.DEMO_ACCOUNTS[0].split(':'), added.split(':')])
        self.assertFalse(pending.exists())

    def test_a_rerun_of_init_db_replaces_the_record_of_a_stopped_run(self):
        # A stop right after the commit of the install job keeps its record.
        # The rerun of --init-db gives every account a new password again, so
        # its plan replaces the record, and the list has only the new ones.
        self.demo()
        pending = self.deploy_dir / 'seeded-accounts.pending'
        result = self.run_script('EMPTY', '--init-db', demo_accounts=' '.join(self.DEMO_ACCOUNTS), demo_apply_exit=137)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'The new passwords stay in {pending} (root only) until a run lists them. Rerun with --init-db',
                      result.stdout)
        self.assertEqual(self.pending_rows(), list(self.DEMO_ACCOUNTS))
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.deploy_dir / 'seeded-accounts.txt').exists())
        self.assert_passwords_kept_secret(result, kept_in=('seeded-accounts.pending',))
        renewed = ('planner:FixturePw3456bcdeFGHJ:internal', 'portal.cust-001:FixturePw7892wxyzBCDE:portal')
        self.log.unlink()
        self.applied.unlink()
        result = self.run_script('SETUP_PENDING', '--init-db', demo_accounts=' '.join(renewed), demo_apply_exit=0)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.container_steps()[:5], [
            'preflight check', 'odoo shell < uat_admins.py', 'odoo shell < uat_demo.py plan',
            'odoo shell < uat_demo.py apply', 'preflight verify-fresh'])
        self.assertEqual(self.applied_rows(), list(renewed))
        self.assertEqual(self.seeded_rows(), [account.split(':') for account in renewed])
        self.assertFalse(pending.exists())
        self.assert_passwords_kept_secret(result, self.DEMO_ACCOUNTS + renewed)

    def test_the_demo_line_compose_file(self):
        self.demo()
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        compose = (self.deploy_dir / 'compose.yml').read_text()
        odoo = service_environment(compose, 'odoo')
        self.assertEqual((odoo['PERODUA_IMAGE_PROFILE'], odoo['INIT_MODULES'], odoo['RESTORED_MODULES'], odoo['EXCLUDED_MODULE']),
                         ('demo-uiux', DEMO_MODULES, DEMO_MODULES, 'perodua_client_stable'))
        # The personas, mail off (the image may carry settings of its own), and
        # no PERODUA_DEMO_SEED: the image profile gives the sample data.
        self.assertEqual((odoo['PERODUA_DEMO_PERSONAS'], odoo['PERODUA_MAIL']), ('1', 'off'))
        self.assertNotIn('PERODUA_MAIL_REDIRECT_TO', odoo)
        self.assertNotIn('IMAGE_MAIL', (self.deploy_dir / 'app.env').read_text())
        self.assertIn('Outgoing email: off. Configure it with: sudo bash deploy-app.sh --mail', result.stdout)
        self.assertNotIn('PERODUA_DEMO_SEED', odoo)
        self.assertIn(f'    image: {DEMO_PINS["DEMO_ODOO_IMAGE"]}\n', compose)
        self.assertIn(f'    image: {DEMO_PINS["DEMO_WEB_IMAGE"]}\n', compose)
        self.assertIn('      - ./uat_demo.py:/opt/deploy/uat_demo.py:ro\n', compose)
        self.assertIn(f'Deployment verified: {DEMO_PINS["DEMO_RELEASE"]} ({DEMO_PINS["DEMO_REVISION"]})', result.stdout)
        # With an SMTP server, the mail settings of app.env apply as on the client line.
        self.demo(SMTP_HOST='10.10.0.25', SMTP_ENCRYPTION='none', SMTP_FROM='no-reply@perodua.com.my',
                  MAIL_REDIRECT_TO='off')
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        odoo = service_environment((self.deploy_dir / 'compose.yml').read_text(), 'odoo')
        self.assertNotIn('PERODUA_MAIL', odoo)
        self.assertEqual(odoo['PERODUA_SMTP_HOST'], '10.10.0.25')
        self.assertNotIn('IMAGE_MAIL', (self.deploy_dir / 'app.env').read_text())

    # ── IMAGE_MAIL=on: the mail settings inside the Odoo image ──────────────
    def test_image_mail_uses_the_mail_settings_of_the_image(self):
        for redirect, text in (('inbox@example.com', 'every email to the test address inbox@example.com'),
                               ('off', 'every email to its real recipients')):
            with self.subTest(redirect=redirect):
                self.demo(IMAGE_MAIL='on', MAIL_REDIRECT_TO=redirect)
                result = self.run_script('READY 0')
                self.assertEqual(result.returncode, 0, result.stdout)
                compose = (self.deploy_dir / 'compose.yml').read_text()
                odoo = service_environment(compose, 'odoo')
                # No PERODUA_MAIL off: the image's settings and password, with the redirect of app.env.
                self.assertEqual((odoo['PERODUA_MAIL'], odoo['PERODUA_MAIL_REDIRECT_TO']), ('on', redirect))
                self.assertNotIn('PERODUA_MAIL: "off"', compose)
                self.assertEqual([key for key in odoo if key.startswith('PERODUA_SMTP_')], [])
                self.assertNotIn('smtp_password', compose)
                app_env = (self.deploy_dir / 'app.env').read_text().splitlines()
                self.assertIn('IMAGE_MAIL=on', app_env)
                self.assertIn('MAIL_REDIRECT_TO=' + redirect, app_env)
                self.assertFalse([line for line in app_env if line.startswith('SMTP_')], app_env)
                self.assertIn(f'Outgoing email: the mail settings inside the Odoo image (IMAGE_MAIL=on), {text}.\n',
                              result.stdout)
                # --check-config accepts the settings, and runs no Docker.
                self.log.unlink(missing_ok=True)
                result = self.run_script('READY 0', '--check-config')
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.calls(), [])

    def test_image_mail_refusals_before_docker(self):
        two_sources = 'name two sources of the mail settings: IMAGE_MAIL=on uses the settings inside the Odoo image'
        cases = (
            ({'IMAGE_MAIL': 'on'}, 'MAIL_REDIRECT_TO is required with IMAGE_MAIL=on'),
            ({'IMAGE_MAIL': 'on', 'MAIL_REDIRECT_TO': 'inbox'}, 'MAIL_REDIRECT_TO must be one email address'),
            ({'IMAGE_MAIL': 'yes', 'MAIL_REDIRECT_TO': 'off'}, 'IMAGE_MAIL must be on or empty'),
            ({'IMAGE_MAIL': 'on', 'MAIL_REDIRECT_TO': 'off', 'SMTP_HOST': 'smtp.example.com',
              'SMTP_FROM': 'no-reply@example.com'}, 'IMAGE_MAIL=on and SMTP_HOST ' + two_sources),
            ({'IMAGE_MAIL': 'on', 'MAIL_REDIRECT_TO': 'off', 'SMTP_FROM': 'no-reply@example.com'},
             'IMAGE_MAIL=on and SMTP_FROM ' + two_sources),
        )
        for updates, message in cases:
            for extra in ((), ('--check-config',)):
                with self.subTest(updates=updates, extra=extra):
                    self.demo(**updates)
                    result = self.run_script('EMPTY', *extra)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stdout)
                    self.assertEqual(self.calls(), [])
                    self.assertFalse(self.deploy_dir.exists())
        # The client-stable-uiux line: its image carries no mail settings.
        self.script = SCRIPT
        self.configure(IMAGE_MAIL='on', MAIL_REDIRECT_TO='off')
        for extra in ((), ('--check-config',)):
            with self.subTest(line='client-stable-uiux', extra=extra):
                result = self.run_script('EMPTY', *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('IMAGE_MAIL=on applies only to the demo-uiux line', result.stdout)
                self.assertEqual(self.calls(), [])

    def test_the_mail_option_is_refused_with_image_mail(self):
        # --mail asks the SMTP questions: on a terminal, it stops before the first one.
        self.demo(IMAGE_MAIL='on', MAIL_REDIRECT_TO='off')
        pid, master = pty.fork()
        if pid == 0:
            os.execvpe('bash', ['bash', str(self.script), '--config', str(self.config), '--dir', str(self.deploy_dir),
                                '--mail'], self.env)
        output = b''
        while True:
            ready, _, _ = select.select([master], [], [], 20)
            if not ready:
                os.kill(pid, 9)
                break
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        _, status = os.waitpid(pid, 0)
        os.close(master)
        text = output.decode(errors='replace')
        self.assertNotEqual(os.waitstatus_to_exitcode(status), 0, text)
        self.assertIn('--mail asks the SMTP questions, and', text)
        self.assertIn('has IMAGE_MAIL=on, the mail settings inside the Odoo image', text)
        self.assertNotIn('SMTP server, host name or IP address', text)
        self.assertEqual(self.calls(), [])

    def test_send_test_mail_with_image_mail(self):
        self.demo(IMAGE_MAIL='on', MAIL_REDIRECT_TO='inbox@example.com')
        self.assertEqual(self.run_script('READY 0').returncode, 0)

        def send(address='you@example.com'):
            self.log.unlink(missing_ok=True)
            return subprocess.run(['bash', str(self.script), '--dir', str(self.deploy_dir), '--send-test-mail', address],
                                  env=self.env, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=60)
        result = send()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Sending one test email to you@example.com: the mail settings inside the Odoo image '
                      '(IMAGE_MAIL=on), every email to the test address inbox@example.com...', result.stdout)
        self.assertEqual(self.run_commands(), [['perodua-mail-test', 'you@example.com']])
        self.assertIn('fake perodua-mail-test: one test email to you@example.com', result.stdout)
        self.assertIn('MAIL_REDIRECT_TO sends it to the test address inbox@example.com, with [TEST for you@example.com]',
                      result.stdout)
        self.env['FAKE_MAIL_TEST_EXIT'] = '1'
        result = send()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('The test email was not sent, for the reason above. The mail settings are those inside the Odoo'
                      ' image: read the mail-test lines above. The App names them when it starts: sudo docker compose'
                      f' --project-name perodua-uat-fixture --file {self.deploy_dir}/compose.yml logs odoo | grep -F'
                      " \\[entrypoint\\]\\ mail.", result.stdout)
        self.assertNotIn('mail line above', result.stdout)
        del self.env['FAKE_MAIL_TEST_EXIT']
        # To the real recipients.
        self.demo(IMAGE_MAIL='on', MAIL_REDIRECT_TO='off')
        self.assertEqual(self.run_script('READY 0').returncode, 0)
        result = send()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Test email sent to you@example.com. Check that it arrives.', result.stdout)

    def test_the_demo_line_has_new_names_and_the_addresses_of_app_env(self):
        # The switch keeps the old directory, project and database, so the
        # line has new default names. The page path, the port and the issuer
        # stay those of app.env (on grey: /dev, 8110, https://stgiss.perodua.com.my/dev),
        # and the sign-in hand-over needs no warning for them.
        self.demo()
        issuer = 'https://stgiss.perodua.com.my/dev'
        values = {k: v for k, v in self.values.items() if k not in ('PROJECT_NAME', 'DB_NAME', 'HTTP_PORT')}
        values.update(PERODUA_LINE='demo-uiux', PUBLIC_ROOT='/dev', ENVIRONMENT_LABEL='DEV', PUBLIC_BASE_URL=issuer)
        self.config.write_text(''.join(f'{k}={v}\n' for k, v in values.items()), encoding='utf-8')
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        app_env = (self.deploy_dir / 'app.env').read_text().splitlines()
        for line in ('PROJECT_NAME=perodua-demo-uiux', 'DB_NAME=perodua_demo', 'HTTP_PORT=8110', 'PUBLIC_ROOT=/dev',
                     'PUBLIC_BASE_URL=' + issuer):
            self.assertIn(line, app_env)
        identity = (self.deploy_dir / '.deployment-identity').read_text().splitlines()
        self.assertIn('project=perodua-demo-uiux', identity)
        self.assertIn('database=perodua_demo', identity)
        compose = (self.deploy_dir / 'compose.yml').read_text()
        self.assertIn('      - "127.0.0.1:8110:80"\n', compose)
        self.assertIn('      PUBLIC_ROOT: "/dev"\n', compose)
        self.assertEqual(service_environment(compose, 'odoo')['DB_NAME'], 'perodua_demo')
        self.assertEqual({args[2] for args in self.calls() if args[:2] == ['compose', '--project-name']}, {'perodua-demo-uiux'})
        self.assertEqual(self.public_urls_arguments(), [[issuer]])
        self.assertNotIn('sign-in hand-over', result.stdout)
        self.assertIn(f'Public URL (through the front proxy or F5): {issuer}/app/', result.stdout)

    def test_the_client_line_writes_no_line_and_no_demo_setting(self):
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        compose = (self.deploy_dir / 'compose.yml').read_text()
        for text in ('PERODUA_DEMO_PERSONAS', 'PERODUA_MAIL:', 'uat_demo.py', 'PERODUA_LINE'):
            self.assertNotIn(text, compose)
        self.assertIn('      PERODUA_IMAGE_PROFILE: client-stable-uiux\n', compose)
        self.assertNotIn('line=', (self.deploy_dir / '.deployment-identity').read_text())
        self.assertNotIn('PERODUA_LINE', (self.deploy_dir / 'app.env').read_text())
        self.assertNotIn('IMAGE_MAIL', (self.deploy_dir / 'app.env').read_text())
        for name in ('uat_demo.py', 'seeded-accounts.txt', 'rollback.sh'):
            self.assertFalse((self.deploy_dir / name).exists(), name)

    def test_a_directory_keeps_the_line_of_its_first_deployment(self):
        self.assertEqual(self.run_script('READY 0').returncode, 0)
        identity = (self.deploy_dir / '.deployment-identity').read_text()
        self.demo()
        self.log.unlink()
        for extra in ((), ('--line', 'demo-uiux')):
            with self.subTest(extra=extra):
                result = self.run_script('READY 0', *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'{self.deploy_dir} runs the client-stable-uiux line, and a directory keeps the line of its'
                              ' first deployment', result.stdout)
                self.assertIn(f'with --replace {self.deploy_dir}', result.stdout)
                self.assertEqual(self.calls(), [])
                self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), identity)
        # And the other way: a directory of the demo-uiux line stays on it.
        self.deploy_dir = Path(self.temp.name) / 'demo-app'
        self.assertEqual(self.run_script('READY 0').returncode, 0)
        self.configure()
        self.log.unlink()
        result = self.run_script('READY 0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'{self.deploy_dir} runs the demo-uiux line', result.stdout)
        self.assertEqual(self.calls(), [])
        # A run that reads only its own app.env keeps its line.
        result = subprocess.run(['bash', str(self.script), '--dir', str(self.deploy_dir), '--non-interactive'],
                                env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f'Deployment verified: {DEMO_PINS["DEMO_RELEASE"]}', result.stdout)

    # ── --replace ───────────────────────────────────────────────────────────
    def replaced(self, name='old-app', **identity):
        """Another deployment directory as deploy-app.sh leaves it: the client
        line, on another project and database."""
        old = Path(self.temp.name) / name
        old.mkdir()
        values = dict(release='client-stable-uiux-v1.2.2', revision='e' * 40, project='perodua-client-uiux',
                      host='192.0.2.20', port='5432', database='perodua_client', user='odoo')
        values.update(identity)
        (old / '.deployment-identity').write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        (old / 'compose.yml').write_text('# the compose file of the old deployment\n')
        return old

    def app_steps(self):
        """(new or old, action) of the calls that stop, start or list an App."""
        return [('old' if args[2] == 'perodua-client-uiux' else 'new', args[5]) for args in self.calls()
                if args[:2] == ['compose', '--project-name'] and args[5] in ('ps', 'stop', 'up')]

    def test_replace_stops_the_other_app_right_before_this_one_starts(self):
        old = self.replaced()
        self.demo()
        result = self.run_script('EMPTY', '--init-db', '--replace', str(old), demo_accounts=self.DEMO_ACCOUNTS[0],
                                 replace_running='0123abcd')
        self.assertEqual(result.returncode, 0, result.stdout)
        # The new database is set up first; the old App stops only right before the new one starts.
        self.assertEqual(self.app_steps(), [('new', 'stop'), ('old', 'ps'), ('old', 'stop'), ('new', 'up')])
        rollback = self.deploy_dir / 'rollback.sh'
        self.assertEqual(rollback.stat().st_mode & 0o777, 0o700)
        text = rollback.read_text()
        self.assertIn(f'OLD_DIR={old} OLD_PROJECT=perodua-client-uiux OLD_RELEASE=client-stable-uiux-v1.2.2\n', text)
        self.assertIn(f'NEW_DIR={self.deploy_dir} NEW_PROJECT=perodua-uat-fixture NEW_RELEASE={DEMO_PINS["DEMO_RELEASE"]}\n',
                      text)
        self.assertEqual(subprocess.run(['bash', '-n', str(rollback)]).returncode, 0)
        self.assertIn(f'The way back: sudo bash {rollback}', result.stdout)
        self.assertIn(f'This deployment takes the place of client-stable-uiux-v1.2.2 in {old}. Its App is stopped, not removed',
                      result.stdout)
        # Nothing of the old directory changed.
        self.assertEqual(sorted(path.name for path in old.iterdir()), ['.deploy.lock', '.deployment-identity', 'compose.yml'])
        self.assertEqual((old / 'compose.yml').read_text(), '# the compose file of the old deployment\n')

    def test_replace_starts_the_other_app_again_when_this_one_fails(self):
        old = self.replaced()
        self.demo()
        result = self.run_script('READY 0', '--replace', str(old), replace_running='0123abcd', http_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.app_steps(), [('new', 'stop'), ('old', 'ps'), ('old', 'stop'), ('new', 'up'),
                                            ('new', 'stop'), ('old', 'up')])
        self.assertIn(f'Starting the App of client-stable-uiux-v1.2.2 in {old} again', result.stdout)
        self.assertIn('runs again, with its own database and attachments', result.stdout)
        # An App that was not running before stays stopped.
        self.log.unlink()
        result = self.run_script('READY 0', '--replace', str(old), replace_running='', http_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.app_steps()[-1], ('new', 'stop'))
        self.assertIn('was not running before this run, and stays stopped', result.stdout)

    def test_replace_refuses_what_it_cannot_keep_before_docker(self):
        self.demo()
        missing = Path(self.temp.name) / 'nothing'
        missing.mkdir()
        cases = (
            (str(self.replaced('same-project', project='perodua-uat-fixture')), (),
             '--replace: ' + str(Path(self.temp.name) / 'same-project') + ' uses the Compose project perodua-uat-fixture as well'),
            (str(self.replaced('same-database', database='perodua')), (), 'keeps database perodua of'),
            (str(missing), (), '--replace needs a deployment'),
            ('relative/old-app', (), '--replace must be an absolute directory path'),
            (str(self.replaced('with-check')), ('--check-config',), 'not with --upgrade, --check-config'),
        )
        for target, extra, message in cases:
            with self.subTest(message=message):
                self.log.unlink(missing_ok=True)
                result = self.run_script('READY 0', '--replace', target, *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.calls(), [])


REVISION = re.search(r'^REVISION=([0-9a-f]{40})$', SCRIPT.read_text(encoding='utf-8'), re.M).group(1)
PUBLIC_HOST = 'perodua-public-check'


class FakeWeb(http.server.BaseHTTPRequestHandler):
    """The web container, reached as an HTTP proxy so that the verification
    keeps its own URLs (http://web/...). It answers from server.routes by
    (side, path) and picks the side as its nginx does by server_name: Host web
    is the internal server, every other host name (the public check's, and
    127.0.0.1 or localhost as well) the public one."""

    def route(self):
        url = urllib.parse.urlsplit(self.path)
        self.server.targets.add(url.netloc)
        host = (self.headers.get('Host') or '').rpartition(':')[0] or self.headers.get('Host')
        side = 'internal' if host == 'web' else 'public'
        return side, url.path + ('?' + url.query if url.query else '')

    def do_GET(self):
        side, path = self.route()
        self.server.requests.append((side, path))
        status, headers, body = self.server.routes.get((side, path), (404, {}, b'<html>404</html>'))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # the sign-ins of a fresh database: not served here
        side, path = self.route()
        self.server.requests.append((side, 'POST ' + path))
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


def web_routes(root):
    """What a web container serving the page under root answers; everything else is 404."""
    def page(prefix):
        return (f'<!doctype html><html><head><script type="module" crossorigin '
                f'src="{prefix}/app/assets/index-abc123.js"></script></head></html>').encode()
    version = json.dumps({'bundle': 'index-abc123.js', 'source_revision': REVISION}).encode()
    api = json.dumps({'code': 0, 'data': {'source_revision': REVISION, 'image_profile': 'client-stable-uiux'}}).encode()
    routes = {
        ('internal', '/app/version.json'): (200, {}, version),
        ('internal', '/uiux/api/version'): (200, {}, api),
        ('internal', '/app/'): (200, {}, page('')),
        ('internal', '/uiux/api/session/me'): (401, {}, json.dumps({'code': 401, 'data': None}).encode()),
        ('public', root + '/app/version.json'): (200, {}, version),
        ('public', root + '/uiux/api/version'): (200, {}, api),
        ('public', root + '/app/'): (200, {}, page(root)),
    }
    if root:
        routes[('public', root + '/')] = (302, {'Location': root + '/app/'}, b'')
    return routes


class HttpVerificationTests(unittest.TestCase):
    """The HTTP verification deploy-app.sh runs in the Odoo container, run here
    unchanged: its requests to http://web go through a proxy setting to a fake
    web container on 127.0.0.1, which sees the host names they carry."""

    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeWeb)
        self.server.requests = []
        self.server.targets = set()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        text = SCRIPT.read_text(encoding='utf-8')
        start = 'compose exec -T odoo python3 - "$REVISION" "$STARTUP_TIMEOUT" "$PUBLIC_ROOT" "$PROFILE" "$FRESH" <<\'PY\'\n'
        self.assertIn(start, text)
        self.source = text.split(start, 1)[1].split('\nPY\n', 1)[0]
        self.assertIn("'http://web'", self.source)

    def verify(self, root, fresh='0', profile='client-stable-uiux', **changes):
        self.server.routes = web_routes(root)
        self.server.routes.update({('public', path): answer for path, answer in changes.items()})
        env = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower()}
        env['http_proxy'] = f'http://127.0.0.1:{self.server.server_address[1]}'
        # STARTUP_TIMEOUT 0: the first failure is final. fresh '0': no sign-ins.
        result = subprocess.run(['python3', '-', REVISION, '0', root, profile, fresh], input=self.source, env=env,
                                text=True, capture_output=True, timeout=60)
        # Every request went to the Compose service web.
        self.assertEqual(self.server.targets, {'web'})
        return result

    def assert_fails(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('HTTP verification failed: ' + message, result.stderr)

    def test_a_page_under_its_path_passes(self):
        result = self.verify('/dev')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified the public page under /dev/app/, and that other public paths are closed.', result.stdout)
        public = [path for side, path in self.server.requests if side == 'public']
        self.assertEqual(public, ['/dev/app/version.json', '/dev/uiux/api/version', '/dev/app/', '/dev/',
                                  '/dev/my', '/app/'])
        self.assertFalse([path for _, path in self.server.requests if path.startswith('POST ')])

    def test_a_fresh_database_goes_on_to_the_sign_ins(self):
        # The last argument, after the path, says the database is new.
        for root in ('/dev', ''):
            with self.subTest(root=root):
                self.server.requests.clear()
                result = self.verify(root, fresh='1')
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('UAT verification failed', result.stderr)
                self.assertIn(('internal', 'POST /uiux/api/session/login'), self.server.requests)

    def test_the_backend_reports_the_image_profile_of_the_line(self):
        # The fake backend reports client-stable-uiux: right for that line only.
        self.assertEqual(self.verify('/dev').returncode, 0)
        self.assert_fails(self.verify('/dev', profile='demo-uiux'), 'backend profile mismatch')

    def test_a_page_at_the_root_passes_as_on_earlier_releases(self):
        result = self.verify('')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified the public page at /app/.', result.stdout)
        public = [path for side, path in self.server.requests if side == 'public']
        self.assertEqual(public, ['/app/version.json', '/uiux/api/version', '/app/'])

    def test_an_absolute_redirect_is_read_not_followed(self):
        # Following it would look up the check's host name, which does not exist.
        result = self.verify('/dev', **{'/dev/': (302, {'Location': f'http://{PUBLIC_HOST}/dev/app/'}, b'')})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_open_paths_outside_the_page_fail(self):
        for path, answer in (('/dev/my', (200, {}, b'<html>portal</html>')),
                             ('/dev/my', (302, {'Location': '/dev/app/'}, b'')),
                             ('/app/', (200, {}, b'<html>the page without its path</html>'))):
            with self.subTest(path=path, status=answer[0]):
                result = self.verify('/dev', **{path: answer})
                self.assert_fails(result, f'public {path} returned HTTP {answer[0]} instead of 404')

    def test_a_page_that_is_not_under_its_path_fails(self):
        cases = (
            ({'/dev/app/': (200, {}, b'<html><script src="/app/assets/index-abc123.js"></script></html>')},
             'public /dev/app/ does not load its files from /dev/app/assets/'),
            ({'/dev/app/': (200, {}, b'<html><script src="/dev/app/assets/i.js"></script>'
                                     b'<a href="/__PERODUA_PUBLIC_ROOT__/app/">x</a></html>')},
             'public /dev/app/ still contains a build placeholder'),
            ({'/dev/app/version.json': (200, {}, json.dumps({'source_revision': '0' * 40}).encode())},
             'public /dev/app/version.json names another source revision'),
            ({'/dev/app/version.json': (404, {}, b'<html>404</html>')},
             'public /dev/app/version.json returned HTTP 404'),
            ({'/dev/uiux/api/version': (200, {}, b'<html>not the API</html>')},
             'public /dev/uiux/api/version did not return JSON'),
            ({'/dev/': (302, {'Location': '/app/'}, b'')},
             'public /dev/ does not redirect to /dev/app/'),
        )
        for changes, message in cases:
            with self.subTest(message=message):
                self.assert_fails(self.verify('/dev', **changes), message)


class UatAdminScriptTests(unittest.TestCase):
    """Static properties of the ORM script; its behaviour is proven on a real
    Odoo database by the acceptance run."""

    source = (SCRIPTS / 'uat_admins.py').read_text(encoding='utf-8')

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
