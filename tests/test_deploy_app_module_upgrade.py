"""deploy-app.sh --upgrade with a module upgrade (-u), against the fake Docker of
test_deploy_app_upgrade.py.

The fake keeps the database of the upgrade as a JSON file (FAKE_DB): its module
fingerprint is 'from' (the modules of v1.0.3 to v1.0.8), 'upgrading' (marked
before -u), 'upgraded' (checked after -u) or 'new' (stamped). Each preflight
mode answers as the real one does for that state; the real modes run against a
database in test_preflight_module_upgrade.py. The directory's preflight.py is
the fixture of the old release until the switch: as the real one of v1.0.5 to
v1.0.8, it accepts only the 'from' state. These tests pin the order of the
steps, every refusal before the App stops, the second check with the App
stopped and the refused mark (both start the old App again), that nothing
starts the old App once the database may carry the mark, the mail hold, and
the reruns.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tarfile
import time
import unittest

from test_deploy_app_upgrade import RELEASE, REVISION, SCRIPT, SCRIPTS, UpgradeHarness


TEXT = SCRIPT.read_text(encoding='utf-8')
README = (SCRIPTS.parent / 'README.md').read_text(encoding='utf-8')
MODULE_UPGRADE_FROM = re.search(r'^MODULE_UPGRADE_FROM=([0-9a-f]{32})$', TEXT, re.M).group(1)
ACCEPTED = re.search(r'^OLD_RELEASES_ACCEPTED=(\S+)$', TEXT, re.M).group(1).split(',')
OLD = 'client-stable-uiux-v1.0.6'
OLD_REVISION = 'b' * 40
CONTAINER = 'perodua-upgrade-fixture-module-upgrade'
# service.sh of v1.0.5, v1.0.6, v1.0.7 and v1.0.8 (kit 4198af8 to 8867545) is
# this file, byte for byte. If it changes, keep a copy of that one for the test
# below: it is what the old scripts folder runs after the mark.
OLD_SERVICE_SHA256 = 'b3c2533b5f2f7b78c6e2b2c036ce215dc8e306f56b96058ef8a4ddb13f1ce26c'

BACKUP = ['staged backup size', 'running?', 'stop web odoo', 'staged pg_dump', 'staged pg_restore --list',
          'staged filestore archive']
CHECKS = ['pull', 'pull', 'staged preflight upgrade-check', 'staged uat_guard guard', 'staged uat_guard demo-flag']
RECHECK = ['staged preflight upgrade-check']  # the second check, with the App stopped
UPGRADE = ['staged preflight mark-upgrading', 'remove the containers', f'rm -f {CONTAINER}', 'staged odoo -u',
           'staged preflight verify-upgrade']
DEPLOY = ['preflight check --accept-pending', 'filestore check', 'stop web odoo', 'preflight public-urls', 'up',
          'http verify', 'preflight stamp-upgrade']


class ModuleUpgradeFixture(UpgradeHarness):
    """A directory of v1.0.6 on a database with the modules of v1.0.3 to v1.0.8."""
    def setUp(self):
        super().setUp()
        self.deployed(OLD, OLD_REVISION)
        self.db = self.base / 'database.json'
        self.set_db('from')
        self.env['FAKE_DB'] = str(self.db)

    def set_db(self, state):
        self.db.write_text(json.dumps({'modhash': state}))

    def db_state(self):
        return json.loads(self.db.read_text())['modhash']

    def module_steps(self):
        """Steps with the container removal of docker rm -f, in order."""
        steps, compose = [], iter(self.steps())
        for entry in self.calls():
            args = entry['args']
            if args[:2] == ['rm', '-f']:
                steps.append(('rm -f ' + args[2], entry['release']))
            elif args[:1] == ['pull'] or (args[:2] == ['compose', '--project-name'] and args[5:6] != ['config']):
                steps.append(next(compose))
        return steps

    def names(self):
        return [name for name, _ in self.module_steps()]

    def upgrade(self, *extra, **fake):
        return self.run_script('--confirm', 'perodua', *extra, **fake)

    def assert_refused_before_the_stop(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout)
        self.assertIn(f'Nothing was changed: {self.deploy_dir} still runs {OLD}.', result.stdout)
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
        self.assertEqual((self.deploy_dir / 'compose.yml').read_text(), f'# the compose file of {OLD}\n')
        self.assertEqual((self.deploy_dir / 'app.env').read_text(), self.app_env)
        self.assertNotIn('stop web odoo', self.names())
        self.assertEqual(self.backups(), [])
        self.assertNotIn('remove the containers', self.names())
        self.assertNotIn('staged odoo -u', self.names())
        self.assertEqual(self.db_state(), 'from')

    def assert_no_old_app_after_the_mark(self, result):
        """Once the database may carry the mark: the containers removed, no
        start of the old App, the -u container removed, and restore.txt as the
        way back."""
        self.assertNotEqual(result.returncode, 0, result.stdout)
        names = self.names()
        self.assertEqual(names.count('remove the containers'), 1)
        remove = names.index('remove the containers')
        self.assertEqual(names[remove - 1], 'staged preflight mark-upgrading')
        self.assertNotIn('restart', names)
        self.assertNotIn('up', names)
        self.assertEqual(self.module_steps()[remove][1], OLD)  # the directory's own compose file
        self.assertIn(f'The containers of {OLD} ', result.stdout)
        self.assertIn('and its App is not started again', result.stdout)
        self.assertIn('The only way back is the backup', result.stdout)
        [backup] = self.backups()
        self.assertIn(f'follow {backup}/restore.txt, all of its steps', result.stdout)
        self.assertIn((backup / 'restore.txt').read_text(), result.stdout)
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
        self.assertNotIn('Starting the App of', result.stdout)
        return backup


class ModuleUpgradeTests(ModuleUpgradeFixture, unittest.TestCase):
    # ── the order of the steps ──────────────────────────────────────────────
    def test_the_steps_in_order(self):
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        steps = self.module_steps()
        self.assertEqual([name for name, _ in steps], CHECKS + BACKUP + RECHECK + UPGRADE + DEPLOY)
        switch = [name for name, _ in steps].index('preflight check --accept-pending')
        self.assertEqual({release for _, release in steps[:switch]}, {OLD})
        self.assertEqual({release for _, release in steps[switch:]}, {RELEASE})
        self.assertEqual(self.db_state(), 'new')
        self.assertIn(f'Deployment verified: {RELEASE} ({REVISION})', result.stdout)
        self.assertIn(f'from {OLD} to {RELEASE}', result.stdout)
        self.assertIn('Point of no return', result.stdout)
        for entry in self.calls():
            self.assertFalse(any('fixture-password' in a for a in entry['args']), entry['args'])

    def test_the_module_upgrade_is_one_run_of_odoo_without_init_or_cron(self):
        self.assertEqual(self.upgrade().returncode, 0)
        [run] = [e['args'] for e in self.calls() if any(' -u ' in a for a in e['args'])]
        self.assertEqual(run[3:5], ['--file', run[4]])
        self.assertNotEqual(run[4], f'{self.deploy_dir}/compose.yml')  # the new release's compose file
        action = run[5:]
        self.assertEqual(action[:6], ['run', '--rm', '--no-deps', '-T', '--name', CONTAINER])
        self.assertIn('PERODUA_UPGRADE_MODULES=perodua_client_stable,perodua_ui', action)
        self.assertIn('PERODUA_DEMO_FLAG=--without-demo=True', action)
        odoo = action.index('odoo')
        self.assertEqual(action[odoo + 1:odoo + 3], ['bash', '-c'])
        command = action[odoo + 3]
        self.assertTrue(command.startswith('PGPASSWORD="$DB_PASSWORD" exec odoo -c /etc/odoo/odoo.conf -d "$DB_NAME"'), command)
        for part in ('-u "$PERODUA_UPGRADE_MODULES" "$PERODUA_DEMO_FLAG"', '--max-cron-threads=0', '--stop-after-init',
                     '--log-handler=odoo.modules.migration:INFO'):
            self.assertIn(part, command)
        self.assertNotIn(' -i ', command)
        self.assertNotIn('-i', action)
        [backup] = self.backups()
        self.assertEqual((backup / 'module-upgrade.log').read_text(), 'fake odoo -u\n')
        self.assertTrue((backup / 'uat-guard.json').is_file())

    def test_the_cron_flags_and_the_mail_hold_come_before_the_app_starts(self):
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        names = self.names()
        self.assertLess(names.index('staged preflight verify-upgrade'), names.index('up'))
        self.assertIn('Cron jobs: 5 flags put back as before the upgrade.', result.stdout)
        self.assertIn('cron_pull_pss_orders stays off: it reads the mock feed', result.stdout)
        # Two numbers: the mail -u queued, and the mail that waited before it.
        self.assertIn('Mail held (state exception) until deploy-app.sh --release-queued-mail: '
                      '2 that the upgrade queued, 1 that waited in the queue before it.', result.stdout)
        self.assertIn('Held mail: 2 that the module upgrade queued, 1 that waited in the queue before the upgrade.', result.stdout)
        self.assertIn(f'sudo bash deploy-app.sh --release-queued-mail --dir {self.deploy_dir}', result.stdout)

    def test_no_mail_queue_runs_between_the_stop_of_the_app_and_the_mail_hold(self):
        # From the stop for the backup to verify-upgrade (the hold): only
        # one-off runs, the removal of the old containers, and one run of
        # Odoo, which has no cron thread and stops after the upgrade. So no
        # mail queue runs on the database before the hold.
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        names = self.names()
        stop, hold = names.index('stop web odoo'), names.index('staged preflight verify-upgrade')
        self.assertEqual(names[stop + 1:hold], BACKUP[3:] + RECHECK + UPGRADE[:-1])
        self.assertEqual(names[hold + 1:names.index('up')], DEPLOY[:DEPLOY.index('up')])
        for step in ('up', 'restart'):
            self.assertNotIn(step, names[:hold])
        [run] = [e['args'] for e in self.calls() if any(' -u ' in a for a in e['args'])]
        command = run[run.index('odoo') + 3]
        self.assertIn('--max-cron-threads=0', command)
        self.assertIn('--stop-after-init', command)

    def test_the_final_message_names_the_list_of_the_held_mail_in_the_readme(self):
        # With PUBLIC_ROOT the public host names do not open Odoo's own pages,
        # so the message names the README step that lists the mail with psql.
        self.write_app_env(PUBLIC_ROOT='/dev', PUBLIC_BASE_URL='https://stgiss.perodua.com.my/dev')
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        version = RELEASE.rsplit('-', 1)[1]
        heading = f'Upgrade an App server to {version}'
        self.assertIn(f'Review the held mail: README.md, "{heading}", step 13 lists it with psql on the DB server '
                      '(in Odoo: Settings > Technical > Emails, status Delivery Failed). Then send it with: '
                      f'sudo bash deploy-app.sh --release-queued-mail --dir {self.deploy_dir}', result.stdout)
        # The README has that section and that step, and its list selects the
        # rows by the state and the reason that the hold writes.
        self.assertEqual(README.count(f'\n## {heading}\n'), 1)
        section = README.split(f'\n## {heading}\n', 1)[1].split('\n## ', 1)[0]
        self.assertEqual(section.count('\n13. The held mail.'), 1)
        step = section.split('\n13. The held mail.', 1)[1].split('\n**When the upgrade stops.**', 1)[0]
        reason = re.search(r"^HOLD_REASON = '([^']+)'$", TEXT, re.M).group(1)
        prefix = re.search(r"m\.state = 'exception' AND m\.failure_reason LIKE '([^%']+)%'", step).group(1)
        self.assertTrue(reason.startswith(prefix), (reason, prefix))
        self.assertIn('sudo bash deploy-app.sh --release-queued-mail --dir /opt/perodua-app', step)
        # The lines that the README quotes are the lines of the script.
        for quoted in ('Held mail: N that the module upgrade queued, M that waited in the queue before the upgrade.',
                       f'Review the held mail: README.md, "{heading}", step 13 lists it with psql on the DB server '
                       '(in Odoo: Settings > Technical > Emails, status Delivery Failed). Then send it with: '
                       'sudo bash deploy-app.sh --release-queued-mail --dir /opt/perodua-app'):
            self.assertIn(quoted, section)

    def test_an_answer_without_the_two_mail_counts_is_refused(self):
        # verify-upgrade must say how many mails of each kind it holds.
        result = self.upgrade(verified='UPGRADE_VERIFIED 2')
        self.assert_no_old_app_after_the_mark(result)
        self.assertIn('Unexpected answer from the check of the upgraded database', result.stdout)
        self.assertNotIn('Held mail:', result.stdout)

    def test_restore_txt_names_the_module_upgrade_and_the_point_of_no_return(self):
        self.assertEqual(self.upgrade().returncode, 0)
        [backup] = self.backups()
        hint = (backup / 'restore.txt').read_text()
        self.assertIn('This upgrade runs a module upgrade (-u).', hint)
        self.assertIn(f'{OLD} cannot run on database perodua', hint)
        self.assertIn('Point of no return', hint)
        self.assertIn(f'Then, from a scripts folder of {OLD}: sudo bash deploy-app.sh --dir {self.deploy_dir}', hint)

    def test_every_accepted_old_release(self):
        self.assertEqual(ACCEPTED, [f'client-stable-uiux-v1.0.{n}' for n in (5, 6, 7, 8)])
        for old in ACCEPTED:
            if old == RELEASE:
                continue  # the directory already runs it
            with self.subTest(old=old):
                self.deployed(old, OLD_REVISION)
                self.set_db('from')
                self.log.unlink(missing_ok=True)
                result = self.upgrade()
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f'release={RELEASE}\n', (self.deploy_dir / '.deployment-identity').read_text())

    # ── refused before the App stops ────────────────────────────────────────
    def test_another_old_release_is_refused(self):
        self.deployed('client-stable-uiux-v1.0.4', OLD_REVISION)
        result = self.upgrade()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'{RELEASE} upgrades the Odoo modules only of a deployment of {", ".join(ACCEPTED)}, '
                      f'and {self.deploy_dir} runs client-stable-uiux-v1.0.4', result.stdout)
        self.assertNotIn('stop web odoo', self.names())
        self.assertEqual(self.backups(), [])

    def test_another_fingerprint_is_refused(self):
        result = self.upgrade(other_fingerprint=1)
        self.assert_refused_before_the_stop(
            result, f'It upgrades modules (-u) only from the modules of v1.0.3 to v1.0.8 (fingerprint {MODULE_UPGRADE_FROM})')
        self.assertEqual(self.names(), ['pull', 'pull', 'staged preflight upgrade-check'])

    def test_each_refusal_of_the_database_check(self):
        # The texts of module_upgrade_problems in preflight.py.
        for reason in ('perodua.uat_init is not set, not complete: only a UAT database that deploy-app.sh --init-db set up takes a module upgrade, not a restored one',
                       'perodua_demo_client is installed',
                       '12 records of perodua_demo_client are in ir_model_data',
                       'perodua_ui would go down from version 19.0.1.79.0 to 19.0.1.78.0',
                       'perodua_custom is installed but is not a module of this release',
                       'perodua_client_stable.hosts names the workspace codes xx; this release knows only rp, sp, cp',
                       'neither perodua_demo.seed_mode nor a <module>.sample_data parameter is set, so nothing keeps the sample data of the new release out',
                       'perodua_master_data.holiday_type_public names a deleted record, and record 4 of perodua_holiday_type has its code PUBLIC'):
            with self.subTest(reason=reason):
                self.log.unlink(missing_ok=True)
                result = self.upgrade(refuse=reason + '|perodua_e2e is installed')
                self.assert_refused_before_the_stop(result, 'the module upgrade (-u) is refused for the reasons above')
                self.assertIn('module upgrade refused: ' + reason, result.stdout)
                self.assertIn('module upgrade refused: perodua_e2e is installed', result.stdout)
        self.env.pop('FAKE_REFUSE')

    def test_retired_data_needs_drop_retired_data(self):
        retired = 'table perodua_transporter_rate 2|column stock_picking.perodua_dispatch_state 1|attachments perodua.transporter.rate 3'
        result = self.upgrade(retired=retired)
        self.assert_refused_before_the_stop(result, 'run --upgrade again with --drop-retired-data')
        for line in ('table       perodua_transporter_rate: 2', 'column      stock_picking.perodua_dispatch_state: 1',
                     'attachments perodua.transporter.rate: 3'):
            self.assertIn(line, result.stdout)
        self.assertEqual(self.names(), ['pull', 'pull', 'staged preflight upgrade-check'])
        # with the owner's agreement: the rows are saved before the containers go
        self.log.unlink()
        result = self.upgrade('--drop-retired-data')
        self.assertEqual(result.returncode, 0, result.stdout)
        names = self.names()
        self.assertEqual(names[names.index('staged filestore archive') + 1:names.index('staged preflight mark-upgrading')],
                         RECHECK + ['staged preflight retired-export'])
        [backup] = self.backups()
        with tarfile.open(backup / 'retired-data.tar.gz') as archive:
            self.assertEqual(archive.getnames(), ['retired-data/columns/stock_picking.perodua_dispatch_state.csv'])

    # ── the second check, with the App stopped ──────────────────────────────
    def assert_old_app_started_again_after_the_recheck(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout)
        names = self.names()
        self.assertEqual(names[names.index('staged filestore archive') + 1:], RECHECK + ['restart'])
        self.assertIn(f'The upgrade stopped before the switch: {self.deploy_dir} still runs {OLD}', result.stdout)
        self.assertIn(f'The App of {OLD} runs again.', result.stdout)
        [backup] = self.backups()
        self.assertIn(f'The backup in {backup} is complete and kept.', result.stdout)
        self.assertFalse((backup / 'retired-data.tar.gz').exists())
        self.assertNotIn('The only way back is the backup', result.stdout)
        self.assertEqual((self.deploy_dir / '.deployment-identity').read_text(), self.identity)
        self.assertEqual(self.db_state(), 'from')

    def test_retired_data_written_after_the_first_check_is_refused_with_the_app_stopped(self):
        # The first check (App running) finds none; a user records a driver
        # check-in before the App stops for the backup.
        result = self.upgrade(retired_again='table perodua_driver_checkin 2|column stock_picking.perodua_dispatch_state 0')
        self.assertIn('Retired data: none.', result.stdout)
        self.assert_old_app_started_again_after_the_recheck(
            result, 'Users wrote data of the retired modules after the first check, while the App ran (above).')
        self.assertIn('Retired data that the module upgrade deletes, counted with the App stopped:', result.stdout)
        self.assertIn('table       perodua_driver_checkin: 2', result.stdout)
        self.assertIn('run --upgrade again with --drop-retired-data', result.stdout)
        self.assertNotIn('staged preflight retired-export', self.names())

    def test_retired_data_written_after_the_first_check_is_saved_with_drop_retired_data(self):
        result = self.upgrade('--drop-retired-data', retired_again='table perodua_driver_checkin 2')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Retired data: none.', result.stdout)
        names = self.names()
        self.assertEqual(names[names.index('staged filestore archive') + 1:names.index('staged preflight mark-upgrading')],
                         RECHECK + ['staged preflight retired-export'])
        [backup] = self.backups()
        self.assertTrue((backup / 'retired-data.tar.gz').is_file())
        self.assertEqual(self.db_state(), 'new')

    def test_a_refusal_of_the_second_check_starts_the_old_app_again(self):
        result = self.upgrade(refuse_again='perodua_demo.seed_mode is full, not none')
        self.assert_old_app_started_again_after_the_recheck(
            result, 'Database perodua changed while the App ran: the module upgrade (-u) is now refused for the reasons above')
        self.assertIn('module upgrade refused: perodua_demo.seed_mode is full, not none', result.stdout)

    def test_other_modules_at_the_second_check_start_the_old_app_again(self):
        result = self.upgrade(modules_again='perodua_client_stable,perodua_rp,perodua_ui')
        self.assert_old_app_started_again_after_the_recheck(
            result, 'The modules to upgrade changed while the App ran: perodua_client_stable,perodua_ui at the first check, '
                    'perodua_client_stable,perodua_rp,perodua_ui now')

    def test_a_failed_export_of_the_retired_data_starts_the_old_app_again(self):
        result = self.upgrade('--drop-retired-data', retired='table perodua_transporter_rate 2', export_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('The export of the retired data failed', result.stdout)
        self.assertIn(f'The App of {OLD} runs again.', result.stdout)
        self.assertEqual(self.names()[-1], 'restart')
        self.assertNotIn('remove the containers', self.names())
        self.assertEqual(self.db_state(), 'from')

    def test_the_guard_refuses_before_the_app_stops(self):
        result = self.upgrade(guard_exit=3)
        self.assert_refused_before_the_stop(result, f'Refused before any change: the modules of {RELEASE} cannot keep perodua_demo_client out')
        self.assertIn('UAT guard: REFUSED: fixture', result.stdout)
        self.assertEqual(self.names(), ['pull', 'pull', 'staged preflight upgrade-check', 'staged uat_guard guard'])

    def test_a_module_upgrade_needs_a_confirmation(self):
        result = self.run_script()  # --non-interactive, no --confirm
        self.assert_refused_before_the_stop(result, 'A module upgrade needs a confirmation: run on a terminal, or pass --confirm perodua')
        self.assertIn('Point of no return', result.stdout)  # the plan is shown first
        self.log.unlink()
        result = self.run_script('--confirm', 'perodua_other')
        self.assert_refused_before_the_stop(result, '--confirm must be exactly perodua')

    def test_the_new_options_belong_to_upgrade(self):
        for args, message in ((['--confirm', 'perodua'], '--confirm applies only to --upgrade'),
                              (['--drop-retired-data'], '--drop-retired-data applies only to --upgrade'),
                              (['--release-queued-mail', '--upgrade'], '--release-queued-mail runs on its own')):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(SCRIPT), '--dir', str(self.deploy_dir), '--non-interactive', *args],
                                        env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.calls(), [])

    # ── the mark ─────────────────────────────────────────────────────────────
    def test_a_refused_mark_starts_the_old_app_again(self):
        # mark-upgrading checks the database again and refuses (exit 3)
        # before it writes: the old containers are still there, and start.
        result = self.upgrade(mark_exit=3)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('module upgrade refused: perodua_demo.seed_mode is full, not none', result.stdout)
        self.assertIn('The mark of the module upgrade was refused for the reasons above. Nothing was written to the database',
                      result.stdout)
        names = self.names()
        self.assertEqual(names[names.index('staged filestore archive') + 1:],
                         RECHECK + ['staged preflight mark-upgrading', 'restart'])
        self.assertIn(f'The App of {OLD} runs again.', result.stdout)
        [backup] = self.backups()
        self.assertIn(f'The backup in {backup} is complete and kept.', result.stdout)
        self.assertNotIn('The only way back is the backup', result.stdout)
        self.assertNotIn('restore.txt:', result.stdout)
        self.assertEqual(self.db_state(), 'from')

    # ── once the database may carry the mark: never the old App again ───────
    def test_a_failed_mark_removes_the_old_containers_and_points_to_the_backup(self):
        # Exit 1 may come after the commit of the mark (a lost connection):
        # the run cannot tell, so it treats the database as marked.
        for fake, state in (({'mark_exit': 1}, 'from'), ({'mark_lost': 1}, 'upgrading')):
            with self.subTest(fake=fake):
                self.set_db('from')
                self.log.unlink(missing_ok=True)
                shutil.rmtree(self.deploy_dir / 'backups', ignore_errors=True)
                for key in ('FAKE_MARK_EXIT', 'FAKE_MARK_LOST'):
                    self.env.pop(key, None)
                result = self.upgrade(**fake)
                backup = self.assert_no_old_app_after_the_mark(result)
                names = self.names()
                self.assertEqual(names[names.index('staged filestore archive') + 1:],
                                 RECHECK + ['staged preflight mark-upgrading', 'remove the containers'])
                self.assertIn('after the backup, before the module upgrade (-u) started', result.stdout)
                self.assertIn('The mark of this upgrade was written, or its result is not known (above).', result.stdout)
                self.assertIn(f'The containers of {OLD} are removed, and its App is not started again', result.stdout)
                self.assertTrue((backup / 'database.dump').is_file())
                self.assertEqual(self.db_state(), state)

    def test_a_failed_module_upgrade_does_not_start_the_old_app(self):
        result = self.upgrade(u_exit=1)
        backup = self.assert_no_old_app_after_the_mark(result)
        self.assertIn(f'The module upgrade (-u) failed; see {backup}/module-upgrade.log', result.stdout)
        self.assertIn('while the module upgrade (-u) ran. The database is partly upgraded.', result.stdout)
        self.assertEqual(self.names()[-2:], ['staged odoo -u', f'rm -f {CONTAINER}'])
        self.assertEqual(self.db_state(), 'upgrading')

    def test_a_module_upgrade_that_times_out_does_not_start_the_old_app(self):
        self.write_app_env(INIT_TIMEOUT='2')
        marker = self.base / 'upgrading'
        started = time.monotonic()
        result = self.upgrade(u_hang=marker, u_seconds=30)
        self.assertLess(time.monotonic() - started, 25)
        self.assert_no_old_app_after_the_mark(result)
        self.assertIn('The module upgrade did not finish within INIT_TIMEOUT=2 seconds', result.stdout)
        self.assertEqual(self.names()[-2:], ['staged odoo -u', f'rm -f {CONTAINER}'])
        time.sleep(1)
        self.assertFalse(Path(str(marker) + '.finished').exists(), 'the -u run went on after the timeout')

    def test_a_lost_ssh_session_during_the_module_upgrade_does_not_start_the_old_app(self):
        # The sign-in host as PUBLIC_BASE_URL: no hand-over warning on stderr.
        self.write_app_env(PUBLIC_BASE_URL='https://stgiss.perodua.com.my')
        for to_group in (True, False):
            with self.subTest(to_group=to_group):
                self.deployed(OLD, OLD_REVISION)
                self.write_app_env(PUBLIC_BASE_URL='https://stgiss.perodua.com.my')
                self.set_db('from')
                self.log.unlink(missing_ok=True)
                shutil.rmtree(self.deploy_dir / 'backups', ignore_errors=True)
                marker = self.base / f'upgrading-{to_group}'
                env = dict(self.env, FAKE_U_HANG=str(marker), FAKE_U_SECONDS='30')
                process = subprocess.Popen(['bash', str(SCRIPT), '--upgrade', '--dir', str(self.deploy_dir), '--non-interactive',
                                            '--confirm', 'perodua'],
                                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                for _ in range(300):
                    if marker.exists() or process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertTrue(marker.exists(), 'the module upgrade never started')
                started = time.monotonic()
                if to_group:
                    os.killpg(process.pid, signal.SIGHUP)
                else:
                    os.kill(process.pid, signal.SIGHUP)
                output = process.communicate(timeout=60)[0].decode()
                self.assertLess(time.monotonic() - started, 20, 'the script waited for the -u run')
                self.assertEqual(process.returncode, 129, output)
                result = subprocess.CompletedProcess(process.args, process.returncode, output)
                self.assert_no_old_app_after_the_mark(result)
                self.assertEqual(self.names()[-2:], ['staged odoo -u', f'rm -f {CONTAINER}'])
                time.sleep(1)
                self.assertFalse(Path(str(marker) + '.finished').exists(), 'the -u run went on after the lost session')

    def test_a_failed_check_of_the_result_does_not_start_the_old_app(self):
        result = self.upgrade(verify_exit=1)
        self.assert_no_old_app_after_the_mark(result)
        self.assertIn('the upgraded database is not right: perodua_ui is to upgrade', result.stdout)
        self.assertIn('after the module upgrade (-u): the check of its result failed', result.stdout)
        self.assertEqual(self.names()[-2:], ['staged preflight verify-upgrade', f'rm -f {CONTAINER}'])

    def test_a_rerun_after_a_stop_before_the_switch_is_refused_until_the_restore(self):
        self.assertNotEqual(self.upgrade(u_exit=1).returncode, 0)
        self.log.unlink()
        result = self.upgrade()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('An earlier module upgrade of database perodua stopped part-way. Restore its backup first', result.stdout)
        self.assertIn('Nothing was changed by this run. The database carries the mark of an earlier module upgrade', result.stdout)
        self.assertEqual(self.names(), ['pull', 'pull', 'staged preflight upgrade-check'])
        self.assertEqual(len(self.backups()), 1)

    # ── after the switch: a rerun finishes without -u ───────────────────────
    def test_a_failure_after_the_switch_and_the_rerun_that_finishes_it(self):
        result = self.upgrade(http_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'The module upgrade from {OLD} to {RELEASE} stopped after the switch. Now:', result.stdout)
        self.assertIn('Its database check reports UPGRADE_PENDING', result.stdout)
        self.assertIn(f'run sudo bash deploy-app.sh --dir {self.deploy_dir} from this scripts folder, without --upgrade. '
                      'It runs no module upgrade again.', result.stdout)
        self.assertIn(f'{OLD} cannot run on this database. The only way back is the backup', result.stdout)
        self.assertNotIn('put back its identity and settings, then deploy it without --upgrade', result.stdout)
        self.assertEqual(self.db_state(), 'upgraded')
        self.env.pop('FAKE_HTTP_EXIT')
        self.log.unlink()
        result = subprocess.run(['bash', str(SCRIPT), '--dir', str(self.deploy_dir), '--non-interactive'], env=self.env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.names(), ['pull', 'pull'] + DEPLOY)
        self.assertIn('are upgraded to %s and checked: deploying it, with no module upgrade' % RELEASE, result.stdout)
        self.assertEqual(self.db_state(), 'new')
        # The run that held the mail printed the counts; this one names the list.
        self.assertNotIn('Held mail:', result.stdout)
        self.assertIn('Review the held mail: README.md, "Upgrade an App server to', result.stdout)

    def test_a_rerun_of_upgrade_from_pending_switches_without_backup_or_module_upgrade(self):
        # An earlier run upgraded and checked the database, then stopped before
        # its switch: the directory still names the old release.
        self.set_db('upgraded')
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.names(), ['pull', 'pull', 'staged preflight upgrade-check'] + DEPLOY)
        self.assertIn('This run makes the switch and deploys', result.stdout)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.db_state(), 'new')
        self.assertIn(f'release={RELEASE}\n', (self.deploy_dir / '.deployment-identity').read_text())

    # ── the held mail ───────────────────────────────────────────────────────
    def test_release_queued_mail(self):
        self.assertEqual(self.upgrade().returncode, 0)
        self.log.unlink()
        command = ['bash', str(SCRIPT), '--release-queued-mail', '--dir', str(self.deploy_dir), '--non-interactive']
        result = subprocess.run(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('3 held mail(s) are queued again (outgoing): 2 that the module upgrade queued, '
                      '1 that waited in the queue before the upgrade. Odoo sends them with its mail queue.', result.stdout)
        self.assertEqual(self.module_steps(), [('preflight release-mail', RELEASE)])  # no pull, no restart
        # An answer without the two counts is not taken as a count.
        self.log.unlink()
        result = subprocess.run(command, env=dict(self.env, FAKE_RELEASED='RELEASED 3'), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unexpected answer from the database check: RELEASED 3', result.stdout)
        self.assertNotIn('queued again', result.stdout)
        # Only from the scripts folder of the release the directory runs.
        self.deployed(OLD, OLD_REVISION)
        self.log.unlink()
        result = subprocess.run(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'To move it to {RELEASE} and keep its data, run deploy-app.sh --upgrade', result.stdout)
        self.assertEqual(self.module_steps(), [])


@unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0 and Path('/proc/self/fd').is_dir(),
                     'service.sh needs root on Linux (the test image)')
class OldServiceShTests(ModuleUpgradeFixture, unittest.TestCase):
    """service.sh of v1.0.5 to v1.0.8 never starts the App after the mark."""

    def service(self, action):
        return subprocess.run(['bash', str(SCRIPTS / 'service.sh'), '--role', 'app', '--dir', str(self.deploy_dir), action],
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)

    def assert_not_started(self):
        for action in ('start', 'restart'):
            with self.subTest(action=action):
                self.log.unlink(missing_ok=True)
                result = self.service(action)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('The App was not started: the database check failed for the reason above.', result.stdout)
                self.assertNotIn('up', self.names())
                self.assertNotIn('restart', self.names())
                self.assertNotIn('Run the reset again', result.stdout)

    def test_service_sh_is_the_one_of_the_old_releases(self):
        import hashlib
        self.assertEqual(hashlib.sha256((SCRIPTS / 'service.sh').read_bytes()).hexdigest(), OLD_SERVICE_SHA256)

    def test_old_service_sh_refuses_a_marked_database(self):
        # Before the switch: the directory's own preflight.py is the old one.
        self.assertNotEqual(self.upgrade(u_exit=1).returncode, 0)
        self.assertEqual(self.db_state(), 'upgrading')
        self.assert_not_started()

    def test_old_service_sh_refuses_an_upgraded_database_before_its_deployment(self):
        # After the switch the directory holds the new preflight.py; it refuses
        # the pending state to every caller but deploy-app.sh.
        self.assertNotEqual(self.upgrade(http_exit=1).returncode, 0)
        self.assertEqual(self.db_state(), 'upgraded')
        self.env.pop('FAKE_HTTP_EXIT')
        self.assert_not_started()


if __name__ == '__main__':
    unittest.main()
