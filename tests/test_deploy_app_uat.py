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
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SCRIPT = SCRIPTS / 'deploy-app.sh'
FRESH_MODULES = ('perodua_client_stable,perodua_gateway,perodua_forecast_workbook,'
                 'perodua_supplier_execution,perodua_uiux_api')
SUPPORT_LINE = re.compile(r'^PUBLIC_ROOT_SUPPORTED=[01]$', re.M)
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
                   'stamp': 'READY 3'}[mode])
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
        self.values = {
            'DB_HOST': '192.0.2.20', 'DB_PORT': '5432', 'DB_NAME': 'perodua', 'DB_USER': 'odoo',
            'DB_PASSWORD_FILE': str(password), 'PROJECT_NAME': 'perodua-uat-fixture',
            'HTTP_PORT': '18110', 'BIND_IP': '127.0.0.1', 'STARTUP_TIMEOUT': '600',
            'INIT_TIMEOUT': '600'}
        self.configure()
        self.script = SCRIPT
        self.env = dict(os.environ, HOME=str(base), DOCKER_CONFIG=str(base / 'docker-config'),
                        PATH=str(base / 'bin') + os.pathsep + os.environ['PATH'],
                        FAKE_DOCKER_LOG=str(self.log))

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
                # The HTTP verification is given the path, then whether the database is new.
                fresh = '0' if check.startswith('READY') else '1'
                self.assertEqual(self.verification_arguments()[0][2:], ['/dev', fresh])

    def test_without_a_public_base_url_only_the_report_address_is_recorded(self):
        result = self.run_script('READY 0')
        self.assertEqual(result.returncode, 0, result.stdout)
        # An empty argument: preflight writes report.url and leaves web.base.url alone.
        self.assertEqual(self.public_urls_arguments(), [['']])
        self.assertEqual(self.verification_arguments()[0][2:], ['', '0'])

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
        start = 'compose exec -T odoo python3 - "$REVISION" "$STARTUP_TIMEOUT" "$PUBLIC_ROOT" "$FRESH" <<\'PY\'\n'
        self.assertIn(start, text)
        self.source = text.split(start, 1)[1].split('\nPY\n', 1)[0]
        self.assertIn("'http://web'", self.source)

    def verify(self, root, fresh='0', **changes):
        self.server.routes = web_routes(root)
        self.server.routes.update({('public', path): answer for path, answer in changes.items()})
        env = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower()}
        env['http_proxy'] = f'http://127.0.0.1:{self.server.server_address[1]}'
        # STARTUP_TIMEOUT 0: the first failure is final. fresh '0': no sign-ins.
        result = subprocess.run(['python3', '-', REVISION, '0', root, fresh], input=self.source, env=env,
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
