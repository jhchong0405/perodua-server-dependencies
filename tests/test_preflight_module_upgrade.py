"""The module-upgrade modes of deploy-app.sh's preflight.py, run against a database.

The exact preflight.py text that deploy-app.sh writes runs as a script, with
the image's addon tree (manifests only) in a temporary folder. By default the
database is SQLite behind a small stand-in for psycopg2, which answers the
PostgreSQL catalogue queries the script makes (server version, owner, pg_class,
to_regclass, information_schema.columns) and passes everything else to SQLite;
the script's SQL keeps to what both understand. With

    PREFLIGHT_TEST_PG='HOST PORT USER PASSWORD DATABASE'

the same tests run against that PostgreSQL 16 database with the real psycopg2;
USER must own DATABASE, whose public schema each test drops and creates again.

The preflight.py of v1.0.5 to v1.0.8 (tests/fixtures, identical in the four
kit versions 4198af8 to 8867545) runs against the same database to show that
the old release refuses it from the mark on.
"""

import hashlib
import io
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


TESTS = Path(__file__).resolve().parent
SCRIPT = (TESTS.parent / 'scripts' / 'deploy-app.sh').read_text(encoding='utf-8')
NEW = SCRIPT.split('cat > "$TEMP_DIR/preflight.py" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0] + '\n'
OLD = (TESTS / 'fixtures' / 'preflight-v1.0.5-v1.0.8.py').read_text(encoding='utf-8')
INIT = ['perodua_client_stable', 'perodua_gateway', 'perodua_forecast_workbook', 'perodua_supplier_execution',
        'perodua_uiux_api']
EXCLUDED = 'perodua_demo_client'
RESTORED = INIT[:1] + [EXCLUDED] + INIT[1:]
HOLD = 'Held by deploy-app.sh --upgrade (module upgrade): release it with deploy-app.sh --release-queued-mail'
PG = os.environ.get('PREFLIGHT_TEST_PG', '').split()

FAKE_PSYCOPG2 = r'''
import os, re, sqlite3

class Error(Exception):
    pgcode = None

def connect(dbname=None, **kwargs):
    return Connection()

class Connection:
    def __init__(self):
        self.db = sqlite3.connect(os.environ['FAKE_PG_DB'])
    def cursor(self):
        return Cursor(self.db)
    def commit(self):
        self.db.commit()
    def rollback(self):
        self.db.rollback()
    def __enter__(self):
        return self
    def __exit__(self, kind, value, traceback):
        (self.db.commit if kind is None else self.db.rollback)()
        return False

class Cursor:
    def __init__(self, db):
        self.db, self.cur, self.rows, self.description, self.rowcount = db, db.cursor(), None, None, -1
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def tables(self):
        return {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    def answer(self, rows):
        self.rows, self.description = list(rows), [('answer',)]
    def execute(self, sql, params=()):
        text, params = ' '.join(sql.split()), tuple(params or ())
        if text == 'SHOW server_version_num':
            return self.answer([('160004',)])
        if text.startswith('SELECT datdba'):
            return self.answer([(True,)])
        if 'FROM pg_class' in text:
            return self.answer([(len(self.tables()),)])
        if text.startswith('SELECT to_regclass('):
            names = re.findall(r"to_regclass\('public\.(\w+)'\)", text) or [p.split('.', 1)[1] for p in params]
            return self.answer([tuple(name if name in self.tables() else None for name in names)])
        if 'information_schema.columns' in text:
            table, column = params
            columns = {row[1] for row in self.db.execute('PRAGMA table_info("%s")' % table)}
            return self.answer([(int(column in columns),)])
        self.rows = None
        self.cur.execute(sql.replace('%s', '?'), params)
        self.description, self.rowcount = self.cur.description, self.cur.rowcount
    def fetchone(self):
        if self.rows is not None:
            return self.rows.pop(0) if self.rows else None
        return self.cur.fetchone()
    def fetchall(self):
        if self.rows is not None:
            rows, self.rows = self.rows, []
            return rows
        return self.cur.fetchall()
'''

SCHEMA = '''
CREATE TABLE ir_module_module (id {pk}, name varchar NOT NULL UNIQUE, state varchar, demo boolean, latest_version varchar);
CREATE TABLE ir_config_parameter (id {pk}, key varchar NOT NULL UNIQUE, value text);
CREATE TABLE ir_model_data (id {pk}, module varchar, name varchar, model varchar, res_id integer, noupdate boolean);
CREATE TABLE ir_attachment (id {pk}, name varchar, res_model varchar, res_field varchar, res_id integer,
                            store_fname varchar, checksum varchar, mimetype varchar, file_size integer);
CREATE TABLE ir_cron (id {pk}, cron_name varchar, active boolean);
CREATE TABLE mail_mail (id {pk}, state varchar, failure_reason text, email_to text);
CREATE TABLE res_users (id {pk}, login varchar);
CREATE TABLE perodua_holiday_type (id {pk}, code varchar UNIQUE, name varchar);
CREATE TABLE perodua_order_cycle (id {pk}, code varchar UNIQUE, name varchar);
CREATE TABLE perodua_customer_category (id {pk}, code varchar UNIQUE, name varchar);
CREATE TABLE perodua_transporter_rate (id {pk}, name varchar, rate numeric);
CREATE TABLE perodua_outbound_route (id {pk}, name varchar);
CREATE TABLE stock_location (id {pk}, name varchar, perodua_is_overflow boolean);
CREATE TABLE stock_picking (id {pk}, name varchar, perodua_wcs_pick_instruction_id varchar,
                            perodua_dispatch_state varchar, perodua_outbound_route_id integer);
'''


def fingerprint(addons):
    return hashlib.md5(b''.join((p / '__manifest__.py').read_bytes()
                                for p in sorted(q for q in addons.glob('perodua_*') if q.is_dir()))).hexdigest()


class PreflightModuleUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='perodua-preflight-')
        self.addCleanup(temp.cleanup)
        self.base = base = Path(temp.name)
        (base / 'db_password').write_text(PG[3] if PG else 'fixture-password')
        # The new release's addons, and the old release's (its fingerprint is
        # the one the module upgrade starts from).
        self.image = self.addons(base / 'new', {**{m: '19.0.2.0.0' for m in INIT}, 'perodua_ui': '19.0.1.79.0',
                                                EXCLUDED: '19.0.1.0.0', 'perodua_demo': '19.0.1.42.0'})
        self.old_image = self.addons(base / 'old', {**{m: '19.0.1.0.0' for m in INIT}, 'perodua_ui': '19.0.1.78.0',
                                                    EXCLUDED: '19.0.1.0.0', 'perodua_demo': '19.0.1.40.0',
                                                    'perodua_hw_sim': '19.0.1.0.0'})
        self.new_fp, self.from_fp = fingerprint(self.image), fingerprint(self.old_image)
        self.scripts = {}
        for name, text, addons in (('new', NEW, self.image), ('old', OLD, self.old_image)):
            path = base / f'preflight-{name}.py'
            path.write_text(text.replace('/run/secrets/db_password', str(base / 'db_password'))
                            .replace('/opt/perodua-addons', str(addons)))
            self.scripts[name] = path
        self.env = dict(os.environ, DB_HOST=PG[0] if PG else '127.0.0.1', DB_PORT=PG[1] if PG else '5432',
                        DB_USER=PG[2] if PG else 'odoo', DB_NAME=PG[4] if PG else 'perodua',
                        INIT_MODULES=','.join(INIT), RESTORED_MODULES=','.join(RESTORED), EXCLUDED_MODULE=EXCLUDED,
                        PYTHONDONTWRITEBYTECODE='1')
        if PG:
            import psycopg2
            self.connection = psycopg2.connect(host=PG[0], port=PG[1], user=PG[2], password=PG[3], dbname=PG[4])
            self.addCleanup(self.connection.close)
            self.sql('DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public')
            for statement in SCHEMA.format(pk='serial PRIMARY KEY').split(';'):
                if statement.strip():
                    self.sql(statement)
        else:
            import sqlite3
            (base / 'fake').mkdir()
            (base / 'fake' / 'psycopg2.py').write_text(FAKE_PSYCOPG2)
            self.env.update(FAKE_PG_DB=str(base / 'db.sqlite'), PYTHONPATH=str(base / 'fake'))
            self.connection = sqlite3.connect(base / 'db.sqlite')
            self.addCleanup(self.connection.close)
            self.connection.executescript(SCHEMA.format(pk='integer PRIMARY KEY'))
        self.ready_database()

    @staticmethod
    def addons(folder, versions):
        for name, version in versions.items():
            (folder / name).mkdir(parents=True)
            (folder / name / '__manifest__.py').write_text(repr({'name': name, 'version': version}) + '\n')
        return folder

    # ── the database ────────────────────────────────────────────────────────
    def sql(self, statement, params=()):
        cursor = self.connection.cursor()
        cursor.execute(statement if PG else statement.replace('%s', '?'), params)
        rows = cursor.fetchall() if cursor.description else None
        self.connection.commit()
        return rows

    def param(self, key, value):
        self.sql('DELETE FROM ir_config_parameter WHERE key = %s', (key,))
        if value is not None:
            self.sql('INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)', (key, value))

    def params(self):
        return dict(self.sql('SELECT key, value FROM ir_config_parameter'))

    def module(self, name, state='installed', version='19.0.1.0.0'):
        self.sql('DELETE FROM ir_module_module WHERE name = %s', (name,))
        self.sql('INSERT INTO ir_module_module (name, state, demo, latest_version) VALUES (%s, %s, %s, %s)',
                 (name, state, False, version))

    def ready_database(self):
        """A UAT database of v1.0.6: --init-db, stamped, no sample data."""
        for name in INIT:
            self.module(name)
        self.module('perodua_ui', version='19.0.1.78.0')
        self.module('perodua_demo', version='19.0.1.40.0')
        self.module('perodua_hw_sim')  # retired: not in the new release
        self.module('base', version='19.0.1.3')
        for key, value in (('perodua.uat_init', 'complete'), ('perodua.runtime_profile', 'client-stable-uiux'),
                           ('perodua.image_modhash', self.from_fp), ('perodua_demo.seed_mode', 'none'),
                           ('perodua_master_sync.sample_data', 'none')):
            self.param(key, value)
        self.sql("INSERT INTO ir_attachment (name, store_fname) VALUES ('a.pdf', 'ab/abcdef')")
        self.sql("INSERT INTO res_users (login) VALUES ('whadmin')")

    def after_module_upgrade(self):
        """What a successful -u leaves: the release's versions, retired gone."""
        for name in INIT:
            self.module(name, version='19.0.2.0.0')
        self.module('perodua_ui', version='19.0.1.79.0')
        self.module('perodua_demo', version='19.0.1.42.0')
        self.module('perodua_hw_sim', state='uninstalled')

    # ── running preflight.py ────────────────────────────────────────────────
    def run_preflight(self, *args, script='new', ok=True):
        result = subprocess.run([sys.executable, str(self.scripts[script]), *args], env=self.env,
                                capture_output=True, timeout=60)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return result.stdout if args[0] == 'retired-export' else result.stdout.decode().strip()
        self.assertNotEqual(result.returncode, 0, result.stdout.decode())
        return result.stderr.decode().strip()

    def process(self, *args, script='new'):
        """The finished run itself: its exit code, standard output and error."""
        return subprocess.run([sys.executable, str(self.scripts[script]), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def check(self, *args, **kwargs):
        return self.run_preflight('upgrade-check', self.from_fp, *args, **kwargs)

    def mark(self):
        self.assertEqual(self.run_preflight('mark-upgrading', self.from_fp, 'client-stable-uiux-v1.0.6',
                                            'client-stable-uiux-v1.2.0'), 'MARKED')

    def kit(self):
        return json.loads(self.params()['perodua.kit_upgrade'])

    # ── upgrade-check ───────────────────────────────────────────────────────
    def test_the_same_modules_need_no_module_upgrade(self):
        self.param('perodua.image_modhash', self.new_fp)
        self.assertEqual(self.check(), 'READY 1')
        self.assertEqual(self.run_preflight('check'), 'READY 1')

    def test_the_old_modules_get_a_module_upgrade_with_its_report(self):
        self.module('perodua_demo_client_ui')  # retired by perodua_demo_ui, not in the release: allowed
        self.sql("INSERT INTO perodua_transporter_rate (name, rate) VALUES ('KL', 10), ('JB', 12)")
        self.sql("INSERT INTO stock_location (name, perodua_is_overflow) VALUES ('A', %s), ('B', %s), ('C', NULL)",
                 (True, False))
        self.sql("INSERT INTO perodua_outbound_route (id, name) VALUES (5, 'North')")
        self.sql("INSERT INTO stock_picking (id, name, perodua_wcs_pick_instruction_id, perodua_dispatch_state, perodua_outbound_route_id) "
                 "VALUES (7, 'OUT/7', '', 'packing', 5), (8, 'OUT/8', 'W-1', 'none', NULL), (9, 'OUT/9', NULL, NULL, NULL)")
        self.sql("INSERT INTO ir_attachment (name, res_model, res_id) VALUES ('rate.pdf', 'perodua.transporter.rate', 1)")
        lines = self.check().splitlines()
        self.assertEqual(lines, [
            'RETIRED table perodua_transporter_rate 2',
            'RETIRED table perodua_outbound_route 1',
            'RETIRED column stock_location.perodua_is_overflow 1',
            'RETIRED column stock_picking.perodua_wcs_pick_instruction_id 1',
            'RETIRED column stock_picking.perodua_dispatch_state 1',
            'RETIRED column stock_picking.perodua_outbound_route_id 1',
            'RETIRED attachments perodua.transporter.rate 1',
            'MODULES ' + ','.join(sorted(INIT + ['perodua_demo', 'perodua_ui'])),
            'MODULE_UPGRADE 1'])
        # Only reads: nothing was written.
        self.assertEqual(self.params()['perodua.image_modhash'], self.from_fp)

    def test_each_refusal(self):
        cases = (
            ('a restored database', lambda: (self.param('perodua.uat_init', None), self.module(EXCLUDED)),
             ['perodua.uat_init is not set, not complete', 'perodua_demo_client is installed']),
            ('perodua_reporting', lambda: self.module('perodua_reporting'), ['perodua_reporting is installed']),
            ('perodua_e2e', lambda: self.module('perodua_e2e'), ['perodua_e2e is installed']),
            ('a version that goes down', lambda: self.module('perodua_ui', version='19.0.1.80.0'),
             ['perodua_ui would go down from version 19.0.1.80.0 to 19.0.1.79.0']),
            ('an unknown module', lambda: self.module('perodua_custom'),
             ['perodua_custom is installed but is not a module of this release']),
            ('a host code', lambda: self.param('perodua_client_stable.hosts', json.dumps(
                {'signin': 'stgiss.perodua.com.my', 'workspaces': {'stgissrp.perodua.com.my': ['rp', 'ops']}})),
             ['perodua_client_stable.hosts names the workspace codes ops; this release knows only rp, sp, cp']),
            ('a host table that is not one', lambda: self.param('perodua_client_stable.hosts', '{"signin": 1}'),
             ['perodua_client_stable.hosts is not a host table']),
            ('no seed parameters', lambda: (self.param('perodua_demo.seed_mode', None),
                                            self.param('perodua_master_sync.sample_data', None)),
             ['neither perodua_demo.seed_mode nor a <module>.sample_data parameter is set']),
            ('a seed mode', lambda: self.param('perodua_demo.seed_mode', 'full'),
             ['perodua_demo.seed_mode is full, not none']),
            ('sample data on', lambda: self.param('perodua_integration.sample_data', 'loaded'),
             ['sample data is on for: perodua_integration.sample_data']),
            ('seeded', lambda: self.param('perodua_demo.seeded', '1'),
             ['perodua_demo.seeded is set: a demonstration database']),
            ('a reference code', lambda: (
                self.sql("INSERT INTO ir_model_data (module, name, model, res_id) VALUES "
                         "('perodua_master_data', 'holiday_type_public', 'perodua.holiday.type', 99)"),
                self.sql("INSERT INTO perodua_holiday_type (id, code, name) VALUES (4, 'PUBLIC', 'Public holiday')")),
             ['perodua_master_data.holiday_type_public names a deleted record, and record 4 of perodua_holiday_type has its code PUBLIC']),
        )
        for name, change, messages in cases:
            with self.subTest(name):
                self.setUp()  # a fresh database and folder for each case
                change()
                error = self.check(ok=False)
                for message in messages:
                    self.assertIn('Database preflight: module upgrade refused: ' + message, error)
                self.assertEqual(self.params().get('perodua.image_modhash'), self.from_fp)

    def test_what_the_new_release_resolves_itself_is_not_refused(self):
        # A user's record that holds a shipped code, without the XML ID: the
        # release adopts it. A record the XML ID still names: nothing to do.
        self.sql("INSERT INTO perodua_customer_category (id, code, name) VALUES (3, 'CAT-SERVICE', 'Service')")
        self.sql("INSERT INTO perodua_order_cycle (id, code, name) VALUES (6, 'DAILY', 'Daily')")
        self.sql("INSERT INTO ir_model_data (module, name, model, res_id) VALUES "
                 "('perodua_master_data', 'order_cycle_daily', 'perodua.order.cycle', 6)")
        self.param('perodua_client_stable.hosts', json.dumps(
            {'signin': 'stgiss.perodua.com.my', 'workspaces': {'stgiss.perodua.com.my': [], 'stgissrp.perodua.com.my': ['rp'],
                                                              'stgisssp.perodua.com.my': ['sp'], 'stgisscp.perodua.com.my': ['cp']}}))
        self.param('perodua_demo.seed_mode', None)  # one sample_data parameter is enough
        self.assertTrue(self.check().endswith('MODULE_UPGRADE 1'))

    def test_another_fingerprint_is_refused(self):
        self.param('perodua.image_modhash', 'f' * 32)
        error = self.check(ok=False)
        self.assertIn('is not the fingerprint this release upgrades modules from', error)
        self.assertIn('upgrades require a separate plan', self.run_preflight('check', ok=False))

    # ── the mark ────────────────────────────────────────────────────────────
    def test_from_the_mark_on_neither_release_accepts_the_database(self):
        self.assertEqual(self.run_preflight('check', script='old'), 'READY 1')  # v1.0.6 before
        self.sql("INSERT INTO ir_cron (id, active) VALUES (1, %s), (2, %s)", (True, False))
        self.sql("INSERT INTO mail_mail (id, state) VALUES (40, 'sent')")
        self.mark()
        self.assertEqual(self.params()['perodua.image_modhash'], 'upgrading:' + self.from_fp)
        record = self.kit()
        self.assertEqual((record['crons'], record['mail_max_id'], record['agent_login']), ({'1': True, '2': False}, 40, False))
        self.assertEqual(record['modules'], sorted(INIT + ['perodua_demo', 'perodua_ui']))
        self.assertEqual((record['from_release'], record['to_release'], record['to_fingerprint']),
                         ('client-stable-uiux-v1.0.6', 'client-stable-uiux-v1.2.0', self.new_fp))
        # service.sh and deploy-app.sh of v1.0.5 to v1.0.8 run this check.
        self.assertIn('database module fingerprint does not match this release', self.run_preflight('check', script='old', ok=False))
        for args in (('check',), ('check', '--accept-pending'), ('upgrade-check', self.from_fp), ('public-urls', ''),
                     ('stamp-upgrade',), ('release-mail',),
                     ('mark-upgrading', self.from_fp, 'client-stable-uiux-v1.0.6', 'client-stable-uiux-v1.2.0')):
            with self.subTest(args=args):
                self.assertIn('marked by a module upgrade that did not finish', self.run_preflight(*args, ok=False))
        # a partly run -u leaves modules to upgrade or remove: the same answer
        self.module('perodua_hw_sim', state='to remove')
        self.assertIn('marked by a module upgrade that did not finish', self.run_preflight('check', ok=False))
        self.assertIn('database contains pending module changes', self.run_preflight('check', script='old', ok=False))

    def test_a_refused_mark_exits_3_and_writes_nothing(self):
        # What changed while the old App ran: the refusal exits 3 before any
        # write, so deploy-app.sh starts the old App again. Other failures
        # exit 1 (they may come after the commit).
        self.param('perodua_demo.seed_mode', 'full')
        result = subprocess.run([sys.executable, str(self.scripts['new']), 'mark-upgrading', self.from_fp,
                                 'client-stable-uiux-v1.0.6', 'client-stable-uiux-v1.2.0'],
                                env=self.env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr.decode())
        self.assertIn('Database preflight: module upgrade refused: perodua_demo.seed_mode is full, not none',
                      result.stderr.decode())
        self.assertEqual(result.stdout, b'')
        params = self.params()
        self.assertEqual(params['perodua.image_modhash'], self.from_fp)
        self.assertNotIn('perodua.kit_upgrade', params)
        self.assertEqual(self.run_preflight('check', script='old'), 'READY 1')  # the old release still runs on it
        # upgrade-check keeps exit 1 for the same refusal
        result = subprocess.run([sys.executable, str(self.scripts['new']), 'upgrade-check', self.from_fp],
                                env=self.env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr.decode())
        # a failure that is not a refusal: exit 1
        self.param('perodua_demo.seed_mode', 'none')
        self.param('perodua.image_modhash', self.new_fp)
        result = subprocess.run([sys.executable, str(self.scripts['new']), 'mark-upgrading', self.from_fp,
                                 'client-stable-uiux-v1.0.6', 'client-stable-uiux-v1.2.0'],
                                env=self.env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr.decode())
        self.assertIn('mark-upgrading does not apply', result.stderr.decode())

    def test_mark_needs_the_old_modules(self):
        self.param('perodua.image_modhash', self.new_fp)
        self.assertIn('mark-upgrading does not apply', self.run_preflight(
            'mark-upgrading', self.from_fp, 'client-stable-uiux-v1.0.6', 'client-stable-uiux-v1.2.0', ok=False))
        self.assertIn('verify-upgrade applies only to a database that a module upgrade marked',
                      self.run_preflight('verify-upgrade', ok=False))

    # ── verify-upgrade ──────────────────────────────────────────────────────
    def cron(self, cron_id, active, xmlid=None):
        self.sql('INSERT INTO ir_cron (id, active) VALUES (%s, %s)', (cron_id, active))
        if xmlid:
            module, name = xmlid.split('.')
            self.sql("INSERT INTO ir_model_data (module, name, model, res_id) VALUES (%s, %s, 'ir.cron', %s)",
                     (module, name, cron_id))

    def actives(self):
        return {cron: bool(active) for cron, active in self.sql('SELECT id, active FROM ir_cron ORDER BY id')}

    def test_the_cron_rule(self):
        self.cron(1, True, 'mail.ir_cron_mail_scheduler_action')
        self.cron(2, False, 'perodua_rp.cron_nightly_report')
        self.cron(3, True, 'perodua_integration.cron_consume_promise_feed')  # mock, on before
        self.cron(4, True, 'perodua_orders_ext.cron_pull_pss_orders')       # live, on before
        self.cron(5, False, 'perodua_orders_ext.cron_pull_psos_orders')
        self.param('perodua_integration.mode', 'live')
        self.param('perodua_integration.mode.PROMISE', 'mock')
        self.param('perodua_integration.mode.PCircle', 'mock')
        self.mark()
        # What -u did: data files switched flags on and off, and made new crons.
        self.sql('UPDATE ir_cron SET active = %s WHERE id IN (1, 4)', (False,))
        self.sql('UPDATE ir_cron SET active = %s WHERE id IN (2, 3, 5)', (True,))
        self.cron(6, True, 'perodua_rp.cron_perodua_spdio_release_email_sync')  # new
        self.cron(7, True, 'perodua_orders_ext.cron_pull_pcircle_orders')        # new, mock
        self.after_module_upgrade()
        output = self.run_preflight('verify-upgrade')
        self.assertEqual(self.actives(), {1: True, 2: False, 3: False, 4: True, 5: False, 6: True, 7: False})
        self.assertIn('Cron jobs: 4 flags put back as before the upgrade.', output)
        self.assertIn('perodua_integration.cron_consume_promise_feed stays off: it reads the mock feed', output)
        self.assertIn('perodua_orders_ext.cron_pull_pcircle_orders stays off', output)
        self.assertNotIn('cron_pull_pss_orders stays off', output)  # its system is live
        self.assertIn('Cron jobs new with this release: perodua_rp.cron_perodua_spdio_release_email_sync (on), '
                      'perodua_orders_ext.cron_pull_pcircle_orders (off).', output)
        self.assertTrue(output.endswith('UPGRADE_VERIFIED 0 0'), output)

    def test_without_a_mode_every_system_is_mock(self):
        for cron_id, xmlid in enumerate(('perodua_integration.cron_consume_promise_feed', 'perodua_orders_ext.cron_pull_pss_orders',
                                          'perodua_orders_ext.cron_pull_psos_orders', 'perodua_orders_ext.cron_pull_pcircle_orders'), 1):
            self.cron(cron_id, True, xmlid)
        self.mark()
        self.after_module_upgrade()
        self.run_preflight('verify-upgrade')
        self.assertEqual(self.actives(), {1: False, 2: False, 3: False, 4: False})

    def mail(self):
        return {mail: (state, reason, to) for mail, state, reason, to in
                self.sql('SELECT id, state, failure_reason, email_to FROM mail_mail ORDER BY id')}

    def test_the_mail_hold_and_its_release(self):
        # The queue before the upgrade: mail 1 and 2 wait (state outgoing),
        # mail 2 is a release mail without an address. The others are done.
        self.sql("INSERT INTO mail_mail (id, state, failure_reason, email_to) VALUES "
                 "(1, 'outgoing', NULL, 'buyer@example.com'), (2, 'outgoing', NULL, NULL), "
                 "(3, 'sent', NULL, 'a@example.com'), (4, 'exception', 'SMTP: connection refused', 'b@example.com'), "
                 "(5, 'cancel', NULL, 'c@example.com')")
        self.mark()
        self.assertEqual(self.kit()['mail_max_id'], 5)
        # What -u did: a migration gave the waiting release mail the address
        # of its supplier, and the run made mail in every state.
        self.sql("UPDATE mail_mail SET email_to = 'supplier@example.com' WHERE id = 2")
        self.sql("INSERT INTO mail_mail (id, state, failure_reason, email_to) VALUES "
                 "(6, 'outgoing', NULL, 'd@example.com'), (7, 'outgoing', NULL, 'e@example.com'), "
                 "(8, 'exception', 'Missing recipient', NULL), (9, 'sent', NULL, 'f@example.com'), "
                 "(10, 'cancel', NULL, 'g@example.com')")
        self.after_module_upgrade()
        output = self.run_preflight('verify-upgrade')
        # Every outgoing mail is held: the two that -u queued, the older one
        # and the older one that -u gave an address. No other row changed.
        self.assertIn('Mail held (state exception) until deploy-app.sh --release-queued-mail: '
                      '2 that the upgrade queued, 2 that waited in the queue before it.', output)
        self.assertTrue(output.endswith('UPGRADE_VERIFIED 2 2'), output)
        untouched = {3: ('sent', None, 'a@example.com'), 4: ('exception', 'SMTP: connection refused', 'b@example.com'),
                     5: ('cancel', None, 'c@example.com'), 8: ('exception', 'Missing recipient', None),
                     9: ('sent', None, 'f@example.com'), 10: ('cancel', None, 'g@example.com')}
        self.assertEqual(self.mail(), {**untouched,
                                       1: ('exception', HOLD, 'buyer@example.com'), 2: ('exception', HOLD, 'supplier@example.com'),
                                       6: ('exception', HOLD, 'd@example.com'), 7: ('exception', HOLD, 'e@example.com')})
        self.assertEqual(self.sql("SELECT count(*) FROM mail_mail WHERE state = 'outgoing'"), [(0,)])
        self.assertEqual(self.params()['perodua.image_modhash'], 'upgraded:' + self.new_fp)
        self.assertEqual((self.kit()['held_mail'], self.kit()['mail_max_id']), ([1, 2, 6, 7], 5))
        # Upgraded and checked: only deploy-app.sh's own check accepts it.
        self.assertIn('checked but not finished', self.run_preflight('check', ok=False))
        self.assertIn('module fingerprint does not match', self.run_preflight('check', script='old', ok=False))
        self.assertEqual(self.run_preflight('check', '--accept-pending'), 'UPGRADE_PENDING 1')
        self.assertEqual(self.check(), 'UPGRADE_PENDING 1')
        self.assertIn('checked but not finished', self.run_preflight('release-mail', ok=False))
        self.assertEqual(self.run_preflight('public-urls', 'https://stgiss.perodua.com.my/dev'), 'PUBLIC_URLS_SET')
        self.assertEqual(self.run_preflight('stamp-upgrade'), 'READY 1')
        self.assertEqual(self.params()['perodua.image_modhash'], self.new_fp)
        self.assertEqual(self.run_preflight('check'), 'READY 1')
        # Until the release nothing changes the held rows.
        self.assertEqual(self.sql("SELECT count(*) FROM mail_mail WHERE state = 'outgoing'"), [(0,)])
        # A held mail the owner cancelled or deleted stays as it is, and so
        # does a row with the same reason that this upgrade did not hold.
        self.sql("UPDATE mail_mail SET state = 'cancel' WHERE id = 7")
        self.sql('DELETE FROM mail_mail WHERE id = 1')
        self.sql("INSERT INTO mail_mail (id, state, failure_reason, email_to) VALUES (11, 'exception', %s, 'h@example.com')", (HOLD,))
        self.assertEqual(self.run_preflight('release-mail'), 'RELEASED 1 1')
        self.assertEqual(self.mail(), {**untouched,
                                       2: ('outgoing', None, 'supplier@example.com'), 6: ('outgoing', None, 'd@example.com'),
                                       7: ('cancel', HOLD, 'e@example.com'), 11: ('exception', HOLD, 'h@example.com')})
        self.assertEqual((self.kit()['held_mail'], self.kit()['released_mail']), ([], [1, 2, 6, 7]))
        after = self.mail()
        self.assertEqual(self.run_preflight('release-mail'), 'RELEASED 0 0')
        self.assertEqual(self.mail(), after)

    def test_only_older_mail_waits_in_the_queue(self):
        # -u queued nothing: the mail that waited before it is held all the same.
        self.sql("INSERT INTO mail_mail (id, state) VALUES (1, 'sent'), (2, 'outgoing'), (3, 'outgoing')")
        self.mark()
        self.after_module_upgrade()
        output = self.run_preflight('verify-upgrade')
        self.assertIn('Mail held (state exception) until deploy-app.sh --release-queued-mail: '
                      '0 that the upgrade queued, 2 that waited in the queue before it.', output)
        self.assertTrue(output.endswith('UPGRADE_VERIFIED 0 2'), output)
        self.assertEqual(self.mail(), {1: ('sent', None, None), 2: ('exception', HOLD, None), 3: ('exception', HOLD, None)})
        self.assertEqual(self.run_preflight('stamp-upgrade'), 'READY 1')
        self.assertEqual(self.run_preflight('release-mail'), 'RELEASED 0 2')
        self.assertEqual(self.mail(), {1: ('sent', None, None), 2: ('outgoing', None, None), 3: ('outgoing', None, None)})

    def test_a_second_verify_upgrade_holds_and_counts_each_mail_once(self):
        self.sql("INSERT INTO mail_mail (id, state, failure_reason) VALUES (1, 'outgoing', NULL), (2, 'exception', 'SMTP: timeout')")
        self.mark()
        self.sql("INSERT INTO mail_mail (id, state) VALUES (3, 'outgoing'), (4, 'outgoing'), (5, 'sent')")
        self.after_module_upgrade()
        line = ('Mail held (state exception) until deploy-app.sh --release-queued-mail: '
                '2 that the upgrade queued, 1 that waited in the queue before it.')
        # The first run holds the mail, then refuses the result: the mark stays.
        self.module('perodua_ui', state='to upgrade', version='19.0.1.79.0')
        first = self.process('verify-upgrade')
        self.assertEqual(first.returncode, 1, first.stdout)
        self.assertIn('the upgraded database is not right: perodua_ui is to upgrade', first.stderr)
        self.assertIn(line, first.stdout)
        self.assertNotIn('UPGRADE_VERIFIED', first.stdout)
        held = {1: ('exception', HOLD, None), 2: ('exception', 'SMTP: timeout', None), 3: ('exception', HOLD, None),
                4: ('exception', HOLD, None), 5: ('sent', None, None)}
        self.assertEqual(self.mail(), held)
        self.assertEqual(self.kit()['held_mail'], [1, 3, 4])
        self.assertEqual(self.params()['perodua.image_modhash'], 'upgrading:' + self.from_fp)
        # The second run, on the same database: the same rows, the same counts.
        self.module('perodua_ui', version='19.0.1.79.0')
        output = self.run_preflight('verify-upgrade')
        self.assertIn(line, output)
        self.assertTrue(output.endswith('UPGRADE_VERIFIED 2 1'), output)
        self.assertEqual(self.mail(), held)
        self.assertEqual(self.kit()['held_mail'], [1, 3, 4])
        # A third run is refused: the database is no longer marked. Nothing changes.
        self.assertIn('verify-upgrade applies only to a database that a module upgrade marked',
                      self.run_preflight('verify-upgrade', ok=False))
        self.assertEqual(self.mail(), held)
        self.assertEqual(self.kit()['held_mail'], [1, 3, 4])
        self.assertEqual(self.run_preflight('stamp-upgrade'), 'READY 1')
        self.assertEqual(self.run_preflight('release-mail'), 'RELEASED 2 1')
        self.assertEqual(self.mail(), {1: ('outgoing', None, None), 2: ('exception', 'SMTP: timeout', None),
                                       3: ('outgoing', None, None), 4: ('outgoing', None, None), 5: ('sent', None, None)})

    def test_a_mail_queued_between_two_runs_is_held_and_counted_once(self):
        # Nothing runs between two verify-upgrade runs. If a mail does enter
        # the queue, the next run holds it too and counts the others once.
        self.sql("INSERT INTO mail_mail (id, state) VALUES (1, 'outgoing')")
        self.mark()
        self.sql("INSERT INTO mail_mail (id, state) VALUES (2, 'outgoing')")
        self.after_module_upgrade()
        self.module('perodua_ui', state='to upgrade', version='19.0.1.79.0')
        self.assertEqual(self.process('verify-upgrade').returncode, 1)
        self.assertEqual(self.kit()['held_mail'], [1, 2])
        self.sql("INSERT INTO mail_mail (id, state) VALUES (3, 'outgoing')")
        self.module('perodua_ui', version='19.0.1.79.0')
        self.assertTrue(self.run_preflight('verify-upgrade').endswith('UPGRADE_VERIFIED 2 1'))
        self.assertEqual(self.kit()['held_mail'], [1, 2, 3])
        self.assertEqual(self.mail(), {mail: ('exception', HOLD, None) for mail in (1, 2, 3)})

    def test_a_wrong_result_is_refused_and_keeps_the_mark(self):
        self.cron(1, False, 'perodua_integration.cron_consume_promise_feed')
        self.mark()
        self.sql("INSERT INTO mail_mail (id, state) VALUES (1, 'outgoing')")
        self.sql('UPDATE ir_cron SET active = %s', (True,))
        self.after_module_upgrade()
        self.module('perodua_ui', state='to upgrade', version='19.0.1.79.0')
        self.module('perodua_gateway', version='19.0.1.0.0')
        self.module('perodua_hw_sim')
        self.param('perodua_demo.seeded', '1')
        self.sql("INSERT INTO res_users (login) VALUES ('agent')")
        error = self.run_preflight('verify-upgrade', ok=False)
        for message in ('perodua_ui is to upgrade', 'perodua_hw_sim is still installed',
                        'perodua_gateway is at version 19.0.1.0.0, the release at 19.0.2.0.0',
                        'perodua_demo.seeded is set', 'the sample user agent was created'):
            self.assertIn('the upgraded database is not right: ' + message, error)
        self.assertEqual(self.params()['perodua.image_modhash'], 'upgrading:' + self.from_fp)
        # What keeps a running App safe was written all the same.
        self.assertEqual(self.actives(), {1: False})
        self.assertEqual(self.sql('SELECT state FROM mail_mail'), [('exception',)])

    # ── the retired data ────────────────────────────────────────────────────
    def test_retired_export(self):
        self.sql("INSERT INTO perodua_transporter_rate (id, name, rate) VALUES (1, 'KL', 10)")
        self.sql("INSERT INTO perodua_outbound_route (id, name) VALUES (5, 'North')")
        self.sql("INSERT INTO stock_picking (id, name, perodua_dispatch_state, perodua_outbound_route_id) "
                 "VALUES (7, 'OUT/7', 'packing', 5), (8, 'OUT/8', 'none', NULL)")
        self.sql("INSERT INTO ir_attachment (id, name, res_model, res_id, store_fname, checksum) "
                 "VALUES (9, 'rate.pdf', 'perodua.transporter.rate', 1, 'cd/cdef01', 'abc')")
        data = self.run_preflight('retired-export')
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            files = {m.name: list(csv.reader(io.StringIO(archive.extractfile(m).read().decode())))
                     for m in archive.getmembers()}
        self.assertEqual(files['retired-data/tables/perodua_transporter_rate.csv'][0], ['id', 'name', 'rate'])
        self.assertEqual(files['retired-data/tables/perodua_transporter_rate.csv'][1][:2], ['1', 'KL'])
        self.assertEqual(files['retired-data/tables/perodua_outbound_route.csv'], [['id', 'name'], ['5', 'North']])
        # the picking and route pairs
        self.assertEqual(files['retired-data/columns/stock_picking.perodua_outbound_route_id.csv'],
                         [['id', 'perodua_outbound_route_id'], ['7', '5']])
        self.assertEqual(files['retired-data/columns/stock_picking.perodua_dispatch_state.csv'],
                         [['id', 'perodua_dispatch_state'], ['7', 'packing']])
        self.assertEqual(files['retired-data/attachments.csv'][1][:6], ['9', 'rate.pdf', 'perodua.transporter.rate', '', '1', 'cd/cdef01'])


if __name__ == '__main__':
    unittest.main()
