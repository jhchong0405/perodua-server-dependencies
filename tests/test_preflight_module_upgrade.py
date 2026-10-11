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

The lock check of the way back (RESTORE_LOCKS): with SQLite the lock table of
the server comes from FAKE_PG_LOCK_TABLE, so both answers are tested. On
PostgreSQL, with pg_dump, pg_restore and psql on the PATH, a dump of the
database is restored in one transaction as restore.txt step 3 does it, and
pg_locks before its commit must agree with the count.
"""

import hashlib
import io
import csv
import json
import os
from pathlib import Path
import re
import shutil
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
RESTORE_LOCKS_SQL = re.search(r'RESTORE_LOCKS = """(.*?)"""', NEW, re.S).group(1)
# The same count of the objects other than relations, per catalog.
CATALOG_LOCKS_SQL = (RESTORE_LOCKS_SQL.split('\nSELECT (SELECT count(*)', 1)[0]
                     + '\nSELECT catalog::regclass::text, count(*) FROM other GROUP BY 1')

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
        if 'max_locks_per_transaction' in text:
            # restore_locks: the tables, FAKE_PG_OBJECTS other objects and the
            # lock table of FAKE_PG_LOCK_TABLE (per transaction, connections, prepared)
            room = [int(value) for value in os.environ.get('FAKE_PG_LOCK_TABLE', '64 100 0').split()]
            return self.answer([(len(self.tables()), int(os.environ.get('FAKE_PG_OBJECTS', '20')), *room)])
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

# For the lock test on PostgreSQL: each kind of object that a restore creates
# and can lock, as the owner of the database can create it (pg_trgm and
# unaccent are trusted extensions), one transaction at a time.
RESTORE_FIXTURE = [
    """CREATE EXTENSION IF NOT EXISTS pg_trgm;
       CREATE EXTENSION IF NOT EXISTS unaccent;
       CREATE TYPE fixture_state AS ENUM ('draft', 'done');
       CREATE DOMAIN fixture_code AS varchar(8) CHECK (VALUE <> '');
       CREATE TYPE fixture_pair AS (a int, b text);
       CREATE TYPE fixture_span AS RANGE (subtype = int4);
       CREATE COLLATION fixture_c FROM "C";
       COMMENT ON COLLATION fixture_c IS 'byte order';
       CREATE TEXT SEARCH DICTIONARY fixture_dict (TEMPLATE = simple);
       CREATE TEXT SEARCH CONFIGURATION fixture_ts (COPY = simple);
       ALTER TEXT SEARCH CONFIGURATION fixture_ts ALTER MAPPING FOR asciiword WITH unaccent, fixture_dict;
       CREATE CONVERSION fixture_conv FOR 'LATIN1' TO 'UTF8' FROM iso8859_1_to_utf8;
       COMMENT ON CONVERSION fixture_conv IS 'latin1'""",
    """CREATE FUNCTION fixture_trim() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN NEW.name := trim(NEW.name); RETURN NEW; END';
       CREATE FUNCTION fixture_pair_eq(fixture_pair, fixture_pair) RETURNS boolean LANGUAGE sql IMMUTABLE AS 'SELECT $1.a = $2.a';
       CREATE OPERATOR === (LEFTARG = fixture_pair, RIGHTARG = fixture_pair, FUNCTION = fixture_pair_eq);
       COMMENT ON OPERATOR === (fixture_pair, fixture_pair) IS 'the same a';
       CREATE FUNCTION fixture_pair_text(fixture_pair) RETURNS text LANGUAGE sql IMMUTABLE AS 'SELECT $1.b';
       CREATE CAST (fixture_pair AS text) WITH FUNCTION fixture_pair_text(fixture_pair);
       COMMENT ON CAST (fixture_pair AS text) IS 'b';
       CREATE FUNCTION fixture_states(fixture_state[]) RETURNS int LANGUAGE sql IMMUTABLE AS 'SELECT cardinality($1)'""",
    """CREATE TABLE fixture_order (id serial PRIMARY KEY, name text NOT NULL COLLATE fixture_c, code fixture_code UNIQUE,
                                  state fixture_state, states fixture_state[] DEFAULT '{}', pair fixture_pair,
                                  pairs fixture_pair[], codes fixture_code[], spans fixture_span[], body text,
                                  user_id int REFERENCES res_users ON DELETE SET NULL,
                                  double int GENERATED ALWAYS AS (id * 2) STORED, CHECK (id > 0));
       COMMENT ON CONSTRAINT fixture_order_user_id_fkey ON fixture_order IS 'who ordered';
       CREATE INDEX fixture_order_name_trgm ON fixture_order USING gin (name gin_trgm_ops);
       CREATE INDEX fixture_order_body ON fixture_order USING gin (to_tsvector('fixture_ts'::regconfig, body));
       CREATE TRIGGER fixture_order_trim BEFORE INSERT ON fixture_order FOR EACH ROW EXECUTE FUNCTION fixture_trim();
       COMMENT ON TRIGGER fixture_order_trim ON fixture_order IS 'trims the name';
       CREATE STATISTICS fixture_order_stats ON id, user_id FROM fixture_order;
       COMMENT ON STATISTICS fixture_order_stats IS 'together';
       INSERT INTO fixture_order (name, code, state, states, pairs, codes, spans, body)
            VALUES (' first ', 'A1', 'draft', '{draft}', ARRAY[(1, 'x')::fixture_pair], '{A1}', '{"[1,5)"}', 'a long note')""",
    """CREATE FUNCTION fixture_done() RETURNS bigint LANGUAGE sql STABLE
           BEGIN ATOMIC SELECT count(*) FROM fixture_order WHERE state = 'done'::fixture_state; END""",
    """CREATE TABLE fixture_line (id serial PRIMARY KEY, order_id int NOT NULL REFERENCES fixture_order ON DELETE CASCADE,
                                 history fixture_order[], note text);
       CREATE TABLE fixture_line_extra (extra text) INHERITS (fixture_line);
       CREATE TABLE fixture_typed OF fixture_pair;
       CREATE TABLE fixture_event (id int, at date NOT NULL, note text) PARTITION BY RANGE (at);
       CREATE TABLE fixture_event_2026 PARTITION OF fixture_event FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
       CREATE TABLE fixture_event_2027 PARTITION OF fixture_event FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
       CREATE INDEX fixture_event_at ON fixture_event (at);
       CREATE UNLOGGED TABLE fixture_cache (key text PRIMARY KEY, value jsonb);
       CREATE SEQUENCE fixture_number""",
    # Triggers that the restore locks without a comment: those of a partitioned
    # table (the clone on each partition depends on its trigger), one that
    # depends on an extension and a constraint trigger; and a foreign key of a
    # partitioned table.
    """CREATE FUNCTION fixture_keep() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';
       ALTER TABLE fixture_event ADD COLUMN user_id int REFERENCES res_users ON DELETE SET NULL;
       """ + ';'.join(f'CREATE TRIGGER fixture_event_{n} BEFORE UPDATE ON fixture_event FOR EACH ROW EXECUTE FUNCTION fixture_keep()'
                     for n in range(20)) + """;
       ALTER TRIGGER fixture_event_0 ON fixture_event DEPENDS ON EXTENSION pg_trgm;
       CREATE CONSTRAINT TRIGGER fixture_line_check AFTER INSERT ON fixture_line DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW EXECUTE FUNCTION fixture_keep()""",
    """CREATE TABLE fixture_log (id int, note text);
       CREATE RULE fixture_log_notify AS ON INSERT TO fixture_log DO ALSO NOTIFY fixture_log;
       COMMENT ON RULE fixture_log_notify ON fixture_log IS 'tells listeners';
       ALTER TABLE fixture_log ENABLE ROW LEVEL SECURITY;
       CREATE POLICY fixture_log_all ON fixture_log USING (true);
       COMMENT ON POLICY fixture_log_all ON fixture_log IS 'everyone';
       CREATE PUBLICATION fixture_pub FOR TABLE fixture_log;
       COMMENT ON PUBLICATION fixture_pub IS 'the log'""",
    """CREATE VIEW fixture_open AS SELECT id, name, states FROM fixture_order WHERE state = 'draft';
       CREATE MATERIALIZED VIEW fixture_totals AS SELECT state, count(*) AS n, string_agg(body, ',') AS bodies
                                                    FROM fixture_order GROUP BY state;
       CREATE INDEX fixture_totals_state ON fixture_totals (state);
       CREATE MATERIALIZED VIEW fixture_ids AS SELECT id, user_id FROM fixture_order;
       CREATE MATERIALIZED VIEW fixture_later AS SELECT id FROM fixture_order WITH NO DATA;
       CREATE SCHEMA fixture_api;
       CREATE VIEW fixture_api.orders_v1 AS SELECT id, name FROM public.fixture_order""",
]


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
    def locks(self, output):
        """The numbers of the RESTORE_LOCKS line of an upgrade-check."""
        [line] = [line for line in output.splitlines() if line.startswith('RESTORE_LOCKS ')]
        self.assertRegex(line, r'^RESTORE_LOCKS \d+ \d+ 1000 \d+ \d+ \d+$')
        return [int(value) for value in line.split()[1:]]

    def test_the_same_modules_need_no_module_upgrade(self):
        self.param('perodua.image_modhash', self.new_fp)
        output = self.check()
        self.assertEqual(output.splitlines()[1:], ['READY 1'])
        self.locks(output)
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
        self.locks(lines.pop(7))  # the way back, before the modules
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

    # ── the lock table and the way back ─────────────────────────────────────
    @unittest.skipIf(PG, 'the lock table of a real server cannot be set by a test')
    def test_the_way_back_must_fit_the_lock_table(self):
        relations = len(self.sql("SELECT name FROM sqlite_master WHERE type = 'table'"))
        # Room: 11 x (100 + 0) = 1100 locks. Need: the relations, the other
        # objects and the margin of 1000.
        self.env.update(FAKE_PG_LOCK_TABLE='11 100 0', FAKE_PG_OBJECTS=str(100 - relations))
        lines = self.check().splitlines()
        self.assertEqual(self.locks('\n'.join(lines)), [relations, 100 - relations, 1000, 11, 100, 0])
        self.assertEqual(lines[-1], 'MODULE_UPGRADE 1')
        self.env['FAKE_PG_LOCK_TABLE'] = '11 90 10'  # a prepared transaction has room too
        self.assertTrue(self.check().endswith('MODULE_UPGRADE 1'))
        # One lock more than the room: refused before anything is written.
        # The numbers come first on standard output, for the steps that
        # deploy-app.sh prints.
        self.env.update(FAKE_PG_LOCK_TABLE='11 100 0', FAKE_PG_OBJECTS=str(101 - relations))
        result = self.process('upgrade-check', self.from_fp)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, f'RESTORE_LOCKS {relations} {101 - relations} 1000 11 100 0\n')
        self.assertIn('Database preflight: module upgrade refused: the way back can fail. restore.txt step 3 '
                      'restores the backup of database perodua in one transaction, which can lock up to 101 objects '
                      f'({relations} tables, indexes, sequences, views and TOAST tables, and {101 - relations} constraints, '
                      'types, system tables and other objects). With a margin of 1000 for the other sessions on the DB '
                      'server, it can need 1101 locks, and the DB server has room for 1100: max_locks_per_transaction 11 x '
                      '(max_connections 100 + max_prepared_transactions 0). With too few, step 3 can stop with "out of '
                      'shared memory", leave the database empty and the App down. Set max_locks_per_transaction to 128 on '
                      'the DB server and restart PostgreSQL there, with the App stopped (README.md, "The lock table of the '
                      'DB server", gives the steps). Then run --upgrade again', result.stderr)
        self.assertNotIn('would fail', result.stderr)
        # 128, the value of the DB servers of this project, or more when the
        # database needs more: 1101 locks over 5 connections is 221 each.
        self.env['FAKE_PG_LOCK_TABLE'] = '11 5 0'
        self.assertIn('Set max_locks_per_transaction to 221 on the DB server', self.check(ok=False))
        # A DB server at 256 (deploy-db.sh keeps a higher value): enough room
        # for 100 connections; with 4, 1101 locks need 276 each, never less
        # than 256.
        self.env['FAKE_PG_LOCK_TABLE'] = '256 100 0'
        self.assertTrue(self.check().endswith('MODULE_UPGRADE 1'))
        self.env['FAKE_PG_LOCK_TABLE'] = '256 4 0'
        self.assertIn('room for 1024: max_locks_per_transaction 256 x (max_connections 4 + max_prepared_transactions 0). '
                      'With too few, step 3 can stop with "out of shared memory", leave the database empty and the App down. '
                      'Set max_locks_per_transaction to 276 on the DB server', self.check(ok=False))
        self.env['FAKE_PG_LOCK_TABLE'] = '11 100 0'
        result = self.process('mark-upgrading', self.from_fp, 'client-stable-uiux-v1.0.6', 'client-stable-uiux-v1.2.0')
        self.assertEqual(result.returncode, 3, result.stderr)  # deploy-app.sh starts the old App again
        self.assertIn('the way back can fail', result.stderr)
        self.assertEqual(self.params()['perodua.image_modhash'], self.from_fp)
        self.assertNotIn('perodua.kit_upgrade', self.params())
        # The same modules: no module upgrade and no refusal. The line is
        # there; deploy-app.sh warns that restore.txt step 3 can fail.
        self.param('perodua.image_modhash', self.new_fp)
        self.assertEqual(self.check().splitlines(), [f'RESTORE_LOCKS {relations} {101 - relations} 1000 11 100 0', 'READY 1'])

    @unittest.skipUnless(PG and all(shutil.which(tool) for tool in ('pg_dump', 'pg_restore', 'psql')),
                         'needs PREFLIGHT_TEST_PG, and pg_dump, pg_restore and psql on the PATH')
    def test_the_lock_count_is_what_a_restore_locks(self):
        self.addCleanup(lambda: self.sql('DROP PUBLICATION IF EXISTS fixture_pub; DROP SCHEMA IF EXISTS fixture_api CASCADE'))
        for transaction in RESTORE_FIXTURE:
            self.sql(transaction)
        # Odoo models, each with TOAST, a sequence and foreign keys. A few per
        # transaction, as a server with a small lock table has room for.
        for first in range(0, 150, 10):
            self.sql(';'.join(f"""CREATE TABLE model_{n} (id serial PRIMARY KEY, name text,
                                  create_uid int REFERENCES res_users ON DELETE SET NULL,
                                  write_uid int REFERENCES res_users ON DELETE SET NULL)""" for n in range(first, first + 10)))
        [count] = self.sql(RESTORE_LOCKS_SQL)
        relations, others, per_transaction, connections, prepared = count
        counted = dict(self.sql(CATALOG_LOCKS_SQL))
        [[materialized_views]] = self.sql("SELECT count(*) FROM pg_class WHERE relkind = 'm'")
        need, room = relations + others + 1000, per_transaction * (connections + prepared)
        result = self.process('upgrade-check', self.from_fp)
        self.assertEqual(self.locks(result.stdout), [relations, others, 1000, per_transaction, connections, prepared])
        if need <= room:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn(f'it can need {need} locks, and the DB server has room for {room}', result.stderr)
        # restore.txt steps 2 and 3: the database is emptied, a few objects per
        # transaction as reset_database.py does it, then a dump is restored in
        # one transaction. Before its commit, the transaction reads its own
        # locks in the lock table of the server: not those in the 16 fast-path
        # slots of the session, and not the lock of this query on pg_locks.
        env = dict(os.environ, PGPASSWORD=PG[3])
        server = ['--host', PG[0], '--port', PG[1], '--username', PG[2], '--no-password']
        dump = self.base / 'database.dump'
        subprocess.run(['pg_dump', '--format=custom', *server, '--dbname', PG[4], '--file', str(dump)], env=env, check=True)
        script = subprocess.run(['pg_restore', '--no-owner', '--no-acl', '--file=-', str(dump)], env=env, check=True,
                                capture_output=True, text=True).stdout.replace('\nSET transaction_timeout = 0;\n', '\n', 1)
        for first in range(0, 150, 10):
            self.sql('DROP TABLE ' + ', '.join(f'model_{n}' for n in range(first, first + 10)))
        for statement in ('DROP PUBLICATION fixture_pub', 'DROP SCHEMA fixture_api CASCADE', 'DROP TABLE fixture_event',
                          'DROP MATERIALIZED VIEW fixture_totals, fixture_ids, fixture_later',
                          'DROP TABLE fixture_log, fixture_cache, fixture_typed, fixture_line CASCADE',
                          'DROP SCHEMA public CASCADE; CREATE SCHEMA public'):
            self.sql(statement)
        probe = ("\\o\nSELECT count(DISTINCT relation) FILTER (WHERE locktype = 'relation' AND relation >= 16384),"
                 " count(DISTINCT (locktype, database, relation, classid, objid, objsubid, virtualxid, transactionid::text))"
                 " FILTER (WHERE NOT fastpath AND relation IS DISTINCT FROM 'pg_locks'::regclass)"
                 " FROM pg_locks WHERE pid = pg_backend_pid();\n"
                 "SELECT classid::regclass::text, count(DISTINCT objid) FROM pg_locks"
                 " WHERE pid = pg_backend_pid() AND locktype = 'object' GROUP BY 1;\n")
        restore = subprocess.run(['psql', '-X', '-q', '-A', '-t', '-F', ' ', '-v', 'ON_ERROR_STOP=1', '--single-transaction', *server,
                                  '--dbname', PG[4]], input='\\o /dev/null\n' + script + probe, env=env,
                                 capture_output=True, text=True, timeout=600)
        if restore.returncode:
            # A restore that does not fit the lock table: the check refused it.
            self.assertIn('out of shared memory', restore.stderr)
            self.assertGreater(need, room)
            return
        first, *catalogs = restore.stdout.splitlines()
        locked_relations, locked = (int(value) for value in first.split())
        held = {catalog: int(value) for catalog, value in (line.split() for line in catalogs)}
        # Each relation of the dump, and the new heaps of the REFRESH of the
        # materialized views (no longer in pg_class), are in the count.
        self.assertLessEqual(locked_relations, relations)
        # The count is an upper bound of what the restore holds in the lock
        # table: collations, text search objects, user array types and the
        # types of the new heaps of REFRESH included.
        self.assertGreaterEqual(relations + others, locked, f'count {relations} + {others}, locked {locked}')
        # And per catalog: the triggers above included. The two types of the
        # new heap of each REFRESH are counted apart. pg_trgm and unaccent
        # make and drop no object of their own in the restore.
        self.assertGreaterEqual(held.get('pg_trigger', 0), 20)
        for catalog, value in held.items():
            room = counted.get(catalog, 0) + (2 * materialized_views if catalog == 'pg_type' else 0)
            self.assertLessEqual(value, room, f'{catalog}: counted {room}, locked {value}')
        self.assertEqual(self.sql(RESTORE_LOCKS_SQL), [count])  # the restored database counts the same

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
