# Modes: check | mark-pending | verify-fresh | stamp | public-urls [BASE_URL].
# States printed by check:
#   MISSING / MISSING_NO_CREATEDB / EMPTY     nothing initialized yet
#   SETUP_PENDING                             a fresh UAT initialization whose
#                                             administrators are not set up yet
#   SETUP_UNMARKED                            the same, but the run stopped before
#                                             it could even record that
#   READY <attachments>                       initialized and stamped
# perodua.uat_init records the fresh path: 'pending' after modules install,
# 'complete' once stamped. Databases without it are restored/legacy databases
# and are held to RESTORED_MODULES exactly as before.
# public-urls, on any initialized database: report.url, and with BASE_URL a
# frozen web.base.url. Prints PUBLIC_URLS_SET.
#
# A module upgrade (deploy-app.sh --upgrade to other modules), in this order:
#   upgrade-check FROM   check, with the module upgrade's refusals when the
#                        stored fingerprint is FROM: prints RETIRED lines,
#                        RESTORE_LOCKS, the MODULES to upgrade, then
#                        MODULE_UPGRADE <attachments>. With the modules of
#                        this release: RESTORE_LOCKS, then READY <attachments>.
#                        RESTORE_LOCKS <relations> <other objects> <margin>
#                        <max_locks_per_transaction> <max_connections>
#                        <max_prepared_transactions>: the locks restore.txt
#                        step 3 can need, and the lock table of the DB server.
#                        When they do not fit, the line comes before the
#                        refusal (also of mark-upgrading)
#   retired-export       the rows the upgrade deletes, as CSV in a tar.gz
#   mark-upgrading FROM OLD NEW
#                        saves the cron flags and the last mail id, and sets
#                        perodua.image_modhash to 'upgrading:FROM'. Neither the
#                        old release's check nor this one's accepts that.
#   verify-upgrade       after -u: puts the cron flags back (a mock intake pull
#                        stays off), holds every outgoing mail (the mail -u
#                        queued and the mail that waited before it), checks
#                        the result and sets 'upgraded:<fingerprint>'. Prints
#                        UPGRADE_VERIFIED <queued by -u> <waited before>
#   check --accept-pending
#                        prints UPGRADE_PENDING <attachments> for that state;
#                        without the option check refuses it
#   stamp-upgrade        after the deployment: the fingerprint, then READY
#   release-mail         queues the held mail again:
#                        RELEASED <queued by -u> <waited before>
import ast, csv, hashlib, io, json, os, pathlib, re, sys, tarfile, time
import psycopg2
mode = sys.argv[1]
init_modules = os.environ['INIT_MODULES'].split(',')
restored_modules = os.environ['RESTORED_MODULES'].split(',')
excluded = os.environ['EXCLUDED_MODULE']
PROFILE = 'client-stable-uiux'
params = dict(host=os.environ['DB_HOST'], port=os.environ['DB_PORT'], user=os.environ['DB_USER'],
              password=pathlib.Path('/run/secrets/db_password').read_text(), connect_timeout=10)
def fail(message):
    print('Database preflight: ' + message, file=sys.stderr)
    sys.exit(1)
def connect(db):
    try:
        return psycopg2.connect(dbname=db, **params)
    except psycopg2.Error as exc:
        category = 'authentication / access / network failure'
        detail = str(exc).lower()
        if exc.pgcode == '28P01' or 'password authentication failed' in detail:
            category = 'incorrect username or password: use the DB_USER and the password set with deploy-db.sh'
        elif 'no pg_hba.conf entry' in detail:
            category = ('the DB server does not accept connections from this App server: on the DB server, APP_CIDR in'
                        ' deploy.conf must contain the App server address that the DB server sees; correct it and run deploy-db.sh again')
        elif 'connection refused' in detail:
            category = ('connection refused: nothing listens at DB_HOST port DB_PORT; DB_HOST is the address deploy-db.sh'
                        ' printed (DB_LISTEN_IP on the DB server), and PostgreSQL must be running there')
        elif 'timeout' in detail:
            category = ('connection timed out: a firewall between the servers, or on the DB server (ufw, a cloud security group),'
                        ' blocks TCP DB_PORT from this App server')
        elif 'could not translate host name' in detail: category = 'hostname could not be resolved'
        elif exc.pgcode == '42501': category = 'insufficient database permissions'
        fail('cannot connect to ' + db + ' (' + category + '). Check DB Server and pg_hba.conf.')
db = os.environ['DB_NAME']
with connect('postgres') as cn:
    with cn.cursor() as cur:
        cur.execute('SHOW server_version_num')
        if not 160000 <= int(cur.fetchone()[0]) < 170000:
            fail('this release requires PostgreSQL 16')
        cur.execute('SELECT datdba = (SELECT oid FROM pg_roles WHERE rolname=current_user) FROM pg_database WHERE datname=%s', (db,))
        row = cur.fetchone()
        if row is None:
            cur.execute('SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user')
            can_create = cur.fetchone()[0]
            print('MISSING' if can_create else 'MISSING_NO_CREATEDB')
            sys.exit(0)
        if not row[0]: fail('application account must own the target database')
def put(cur, key, value):
    cur.execute('INSERT INTO ir_config_parameter (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', (key, value))
def image_versions():
    # Module versions as Odoo records them on install (adapt_version).
    versions = {}
    for manifest in pathlib.Path('/opt/perodua-addons').glob('perodua_*/__manifest__.py'):
        version = str(ast.literal_eval(manifest.read_text()).get('version', '1.0'))
        versions[manifest.parent.name] = version if version.startswith('19.0.') and version != '19.0' else '19.0.' + version
    return versions

# ── Module upgrade ───────────────────────────────────────────────────────────
UPGRADING, UPGRADED = 'upgrading:', 'upgraded:'
KIT_PARAM = 'perodua.kit_upgrade'
HOLD_REASON = 'Held by deploy-app.sh --upgrade (module upgrade): release it with deploy-app.sh --release-queued-mail'
# Gone from the new release: perodua_ui 1.79.0 uninstalls the first three in
# the same -u. perodua_demo_ui retires the bridge, which may stay recorded.
RETIRED_MODULES = ('perodua_hw_sim', 'perodua_supplier_transport_ext', 'perodua_warehouse_ext')
ALLOWED_ABSENT = ('perodua_demo_client_ui',)
NEVER_INSTALLED = ('perodua_demo_client', 'perodua_reporting', 'perodua_e2e')
# The data the upgrade deletes with them (plan WP5, D3): tables, columns with a
# value other than their default, and the attachments of the retired models.
RETIRED_TABLES = ('perodua_transporter_rate', 'perodua_transporter_process', 'perodua_supplier_process',
                  'perodua_trip_volume_wizard', 'perodua_supplier_delivery_report_wizard',
                  'perodua_po_invoice_report_wizard', 'perodua_import_receipt_wizard',
                  'perodua_overflow_move_wizard', 'perodua_warehouse_dispatch_wizard_line',
                  'perodua_warehouse_dispatch_wizard', 'perodua_warehouse_putaway_wizard',
                  'perodua_warehouse_stock_count_wizard', 'perodua_demo_control',
                  'perodua_outbound_route_stop', 'perodua_outbound_route', 'perodua_driver_checkin')
RETIRED_MODELS = tuple(t.replace('_', '.') for t in RETIRED_TABLES) + ('perodua.hw.sim',)
RETIRED_COLUMNS = (
    ('stock_location', 'perodua_is_overflow', 'perodua_is_overflow IS TRUE'),
    ('stock_picking', 'perodua_wcs_pick_instruction_id',
     "perodua_wcs_pick_instruction_id IS NOT NULL AND perodua_wcs_pick_instruction_id <> ''"),
    ('stock_picking', 'perodua_dispatch_state',
     "perodua_dispatch_state IS NOT NULL AND perodua_dispatch_state <> 'none'"),
    ('stock_picking', 'perodua_outbound_route_id', 'perodua_outbound_route_id IS NOT NULL'),
)
# Reference records with a unique code that the upgrade creates again when
# their XML ID is missing. The new release adopts a user's record with the
# same code, or keeps the record away; it cannot when the XML ID still exists
# but its record was deleted, and another record holds the code.
REFERENCE_RECORDS = (
    ('perodua_master_data', 'holiday_type_public', 'perodua_holiday_type', 'PUBLIC'),
    ('perodua_master_data', 'holiday_type_state', 'perodua_holiday_type', 'STATE'),
    ('perodua_master_data', 'holiday_type_shutdown', 'perodua_holiday_type', 'SHUTDOWN'),
    ('perodua_master_data', 'holiday_type_company', 'perodua_holiday_type', 'COMPANY'),
    ('perodua_master_data', 'order_cycle_daily', 'perodua_order_cycle', 'DAILY'),
    ('perodua_master_data', 'order_cycle_weekly', 'perodua_order_cycle', 'WEEKLY'),
    ('perodua_master_data', 'order_cycle_bimonthly', 'perodua_order_cycle', 'BIMONTHLY'),
    ('perodua_master_data', 'order_cycle_monthly', 'perodua_order_cycle', 'MONTHLY'),
    ('perodua_orders_ext', 'customer_category_service', 'perodua_customer_category', 'CAT-SERVICE'),
    ('perodua_orders_ext', 'customer_category_body_paint', 'perodua_customer_category', 'CAT-BP'),
)
HOST_CODES = ('rp', 'sp', 'cp')
# The scheduled pulls that read a mock feed while their system resolves to
# mock (perodua_integration.mode.<SYSTEM>, else perodua_integration.mode, else
# mock). verify-upgrade never switches one of them on in that case.
MOCK_INTAKE_CRONS = {
    'perodua_integration.cron_consume_promise_feed': 'PROMISE',
    'perodua_orders_ext.cron_pull_pss_orders': 'PSS',
    'perodua_orders_ext.cron_pull_psos_orders': 'PSOS',
    'perodua_orders_ext.cron_pull_pcircle_orders': 'PCircle',
}

def table_exists(cur, table):
    cur.execute('SELECT to_regclass(%s)', ('public.' + table,))
    return cur.fetchone()[0] is not None
def column_exists(cur, table, column):
    cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, column))
    return cur.fetchone()[0] > 0
def version_key(version):
    return tuple(int(part) for part in re.findall(r'[0-9]+', version or ''))
def params_like(cur, *patterns):
    cur.execute('SELECT key, value FROM ir_config_parameter WHERE ' + ' OR '.join(['key LIKE %s'] * len(patterns)), patterns)
    return dict(cur.fetchall())
def kit_record(cur):
    cur.execute('SELECT value FROM ir_config_parameter WHERE key = %s', (KIT_PARAM,))
    row = cur.fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        fail(KIT_PARAM + ' is not readable')
def in_list(column, values):  # "column IN (%s, ...)" and its values; never empty
    return column + ' IN (' + ', '.join(['%s'] * len(values)) + ')', tuple(values)
def held_counts(record):
    # The held mail as two numbers: the mail -u queued (an id above the last
    # one that mark-upgrading saved) and the mail that waited before it.
    queued = sum(1 for mail in record['held_mail'] if mail > record['mail_max_id'])
    return queued, len(record['held_mail']) - queued
def system_mode(params, system):
    # perodua.connector.mixin._resolve_mode, from the parameters alone.
    for key in ('perodua_integration.mode.' + system, 'perodua_integration.mode'):
        if params.get(key) in ('mock', 'live'):
            return params[key]
    return 'mock'
def retired_counts(cur):
    counts = []
    for table in RETIRED_TABLES:
        if table_exists(cur, table):
            cur.execute('SELECT count(*) FROM "%s"' % table)
            counts.append(('table', table, cur.fetchone()[0]))
    for table, column, condition in RETIRED_COLUMNS:
        if column_exists(cur, table, column):
            cur.execute('SELECT count(*) FROM "%s" WHERE %s' % (table, condition))
            counts.append(('column', table + '.' + column, cur.fetchone()[0]))
    where, values = in_list('res_model', RETIRED_MODELS)
    cur.execute('SELECT res_model, count(*) FROM ir_attachment WHERE ' + where + ' GROUP BY res_model ORDER BY res_model', values)
    counts += [('attachments', model, count) for model, count in cur.fetchall()]
    return counts
def upgrade_modules(installed, image):
    return sorted(name for name in installed if name.startswith('perodua_') and name in image)
def module_upgrade_problems(cur, modules, installed, uat, excluded_rows):
    # Every reason the new release's -u must not run on this database.
    problems = []
    if uat != 'complete':
        problems.append('perodua.uat_init is %s, not complete: only a UAT database that deploy-app.sh --init-db set up takes a module upgrade, not a restored one' % (uat or 'not set'))
    for name in NEVER_INSTALLED:
        if name in installed:
            problems.append(name + ' is installed')
    if excluded_rows:
        problems.append('%d records of %s are in ir_model_data' % (excluded_rows, NEVER_INSTALLED[0]))
    image = image_versions()
    for name in sorted(installed):
        if not name.startswith('perodua_'):
            continue
        if name not in image:
            if name not in RETIRED_MODULES + ALLOWED_ABSENT:
                problems.append(name + ' is installed but is not a module of this release')
        elif version_key(image[name]) < version_key(modules[name][2]):
            problems.append('%s would go down from version %s to %s' % (name, modules[name][2], image[name]))
    hosts = params_like(cur, 'perodua_client_stable.hosts').get('perodua_client_stable.hosts')
    if hosts:
        try:
            workspaces = json.loads(hosts)['workspaces']
            codes = {code for listed in workspaces.values() for code in listed}
        except (ValueError, KeyError, TypeError, AttributeError):
            problems.append('perodua_client_stable.hosts is not a host table')
        else:
            unknown = sorted(str(code) for code in codes if code not in HOST_CODES)
            if unknown:
                problems.append('perodua_client_stable.hosts names the workspace codes %s; this release knows only %s'
                                % (', '.join(unknown), ', '.join(HOST_CODES)))
    seed = params_like(cur, 'perodua_demo.seed_mode', 'perodua_demo.seeded', '%.sample_data')
    samples = {key: value for key, value in seed.items() if key.endswith('.sample_data')}
    mode_value = seed.get('perodua_demo.seed_mode')
    if mode_value is None and not samples:
        problems.append('neither perodua_demo.seed_mode nor a <module>.sample_data parameter is set, so nothing keeps the sample data of the new release out')
    if mode_value is not None and mode_value != 'none':
        problems.append('perodua_demo.seed_mode is %s, not none' % mode_value)
    loaded = sorted(key for key, value in samples.items() if value != 'none')
    if loaded:
        problems.append('sample data is on for: ' + ', '.join(loaded))
    if seed.get('perodua_demo.seeded'):
        problems.append('perodua_demo.seeded is set: a demonstration database')
    for module, name, table, code in REFERENCE_RECORDS:
        if not table_exists(cur, table):
            continue
        cur.execute('SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s', (module, name))
        row = cur.fetchone()
        if row is None:
            continue
        cur.execute('SELECT count(*) FROM "%s" WHERE id = %%s' % table, (row[0],))
        if cur.fetchone()[0]:
            continue
        cur.execute('SELECT id FROM "%s" WHERE code = %%s ORDER BY id' % table, (code,))
        other = cur.fetchone()
        if other:
            problems.append('%s.%s names a deleted record, and record %s of %s has its code %s: the upgrade would create it again and stop on the unique code'
                            % (module, name, other[0], table, code))
    return problems
# The way back, restore.txt step 3, restores the backup in one transaction. Up
# to its commit it keeps a lock on each relation that it creates or reads and,
# as PostgreSQL 16.15 records dependencies, on each object that a new object
# depends on. RESTORE_LOCKS counts these kinds of objects, more rather than
# less. It is measured and tested on the databases that docs/DEPLOYMENT.md
# lists ("The lock table of the way back"); RESTORE_LOCK_MARGIN covers the
# small rest:
#   - each relation from OID 16384 outside temporary schemas, and each
#     materialized view once more for the new heap of its REFRESH (three
#     times with its TOAST table and index);
#   - each object from OID 16384 outside temporary schemas in pg_constraint,
#     pg_type (an array type only when an object depends on it), pg_proc,
#     pg_operator, pg_opclass, pg_opfamily, pg_collation, pg_conversion,
#     pg_ts_config, pg_ts_dict, pg_ts_parser, pg_ts_template,
#     pg_statistic_ext, pg_namespace, pg_extension, pg_language, pg_am,
#     pg_cast, pg_transform, pg_foreign_data_wrapper, pg_foreign_server,
#     pg_event_trigger, pg_publication, pg_largeobject_metadata, pg_rewrite
#     and pg_policy;
#   - each trigger from OID 16384 that is not internal (its clones on
#     partitions, constraint triggers and DEPENDS ON EXTENSION included),
#     each internal trigger that another object depends on or with a comment,
#     and each internal trigger of a partitioned table or of a partition. A
#     foreign key of a partition that is the same as a foreign key of its
#     table becomes one with it in the restore: PostgreSQL drops the triggers
#     of the copy on the referenced table (and, when that table is
#     partitioned, its constraints and triggers on the partitions) and keeps a
#     lock on each. The copy has as many triggers on the partition as it loses
#     on a referenced table that is not partitioned. The other internal
#     triggers, those of foreign keys between tables that are not
#     partitioned, are not locked: an Odoo database has about 13000 of them,
#     and no partitioned table;
#   - each column with a default of an INHERITS child (not a partition), and
#     each inherited column of such a child whose parent column has a
#     default. pg_dump creates the child, which then gets a copy of each
#     default of its parent, and sets each inherited default apart (DEFAULT
#     NULL when the child has none): PostgreSQL deletes the copy and keeps a
#     lock on it. The 5 children of ir_actions in an Odoo database count 5,
#     although their copies are not locked: pg_dump sets the serial default
#     of ir_actions after it creates the children;
#   - each object below OID 16384 that such an object depends on (such as the
#     schema public and plpgsql), and each system relation (CREATE COLLATION
#     and views can keep a lock on one);
#   - each role and tablespace from OID 16384, two types for each
#     materialized view (the row type of the new heap and its array type),
#     and the transaction ID.
# Measured with PostgreSQL 16.15 before the commit (the locks in the lock
# table): an Odoo 19 database of this project (888 tables, 5246 relations)
# held 10677, and this count gives 11141 (413 of them system relations); a
# partitioned table with 32000 triggers held 32022, and the count gives 96434;
# 1000 copies of foreign keys on 20 partitions, to a table with 30
# partitions, held 94861, and the count gives 103374; 200 INHERITS children
# of a table with 40 defaults held 8804, and the count gives 9219.
RESTORE_LOCKS = """WITH relation AS (
        SELECT c.relkind, c.reltoastrelid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.oid >= 16384 AND n.nspname !~ '^pg_(toast_)?temp_'),
     object (catalog, oid, ns) AS (
        SELECT 'pg_constraint'::regclass, oid, connamespace FROM pg_constraint
        UNION ALL SELECT 'pg_type'::regclass, oid, typnamespace FROM pg_type t
                   WHERE typcategory <> 'A' OR EXISTS (SELECT FROM pg_depend WHERE refclassid = 'pg_type'::regclass AND refobjid = t.oid)
        UNION ALL SELECT 'pg_proc'::regclass, oid, pronamespace FROM pg_proc
        UNION ALL SELECT 'pg_operator'::regclass, oid, oprnamespace FROM pg_operator
        UNION ALL SELECT 'pg_opclass'::regclass, oid, opcnamespace FROM pg_opclass
        UNION ALL SELECT 'pg_opfamily'::regclass, oid, opfnamespace FROM pg_opfamily
        UNION ALL SELECT 'pg_collation'::regclass, oid, collnamespace FROM pg_collation
        UNION ALL SELECT 'pg_conversion'::regclass, oid, connamespace FROM pg_conversion
        UNION ALL SELECT 'pg_ts_config'::regclass, oid, cfgnamespace FROM pg_ts_config
        UNION ALL SELECT 'pg_ts_dict'::regclass, oid, dictnamespace FROM pg_ts_dict
        UNION ALL SELECT 'pg_ts_parser'::regclass, oid, prsnamespace FROM pg_ts_parser
        UNION ALL SELECT 'pg_ts_template'::regclass, oid, tmplnamespace FROM pg_ts_template
        UNION ALL SELECT 'pg_statistic_ext'::regclass, oid, stxnamespace FROM pg_statistic_ext
        UNION ALL SELECT 'pg_namespace'::regclass, oid, oid FROM pg_namespace
        UNION ALL SELECT 'pg_extension'::regclass, oid, NULL FROM pg_extension
        UNION ALL SELECT 'pg_language'::regclass, oid, NULL FROM pg_language
        UNION ALL SELECT 'pg_am'::regclass, oid, NULL FROM pg_am
        UNION ALL SELECT 'pg_cast'::regclass, oid, NULL FROM pg_cast
        UNION ALL SELECT 'pg_transform'::regclass, oid, NULL FROM pg_transform
        UNION ALL SELECT 'pg_foreign_data_wrapper'::regclass, oid, NULL FROM pg_foreign_data_wrapper
        UNION ALL SELECT 'pg_foreign_server'::regclass, oid, NULL FROM pg_foreign_server
        UNION ALL SELECT 'pg_event_trigger'::regclass, oid, NULL FROM pg_event_trigger
        UNION ALL SELECT 'pg_publication'::regclass, oid, NULL FROM pg_publication
        UNION ALL SELECT 'pg_largeobject_metadata'::regclass, oid, NULL FROM pg_largeobject_metadata
        UNION ALL SELECT 'pg_rewrite'::regclass, oid, NULL FROM pg_rewrite
        UNION ALL SELECT 'pg_policy'::regclass, oid, NULL FROM pg_policy
        UNION ALL SELECT 'pg_trigger'::regclass, oid, NULL FROM pg_trigger t
                   WHERE NOT tgisinternal OR EXISTS (SELECT FROM pg_depend WHERE refclassid = 'pg_trigger'::regclass AND refobjid = t.oid)
                      OR EXISTS (SELECT FROM pg_description WHERE classoid = 'pg_trigger'::regclass AND objoid = t.oid)
                      OR EXISTS (SELECT FROM pg_class WHERE oid = t.tgrelid AND (relkind = 'p' OR relispartition))),
     inherited_default AS (
        SELECT a.attrelid, a.attnum FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.oid >= 16384 AND n.nspname !~ '^pg_(toast_)?temp_' AND NOT c.relispartition AND a.attnum > 0 AND NOT a.attisdropped
           AND EXISTS (SELECT FROM pg_inherits WHERE inhrelid = c.oid)
           AND (EXISTS (SELECT FROM pg_attrdef WHERE adrelid = a.attrelid AND adnum = a.attnum)
                OR a.attinhcount > 0 AND EXISTS (SELECT FROM pg_inherits i JOIN pg_attribute p ON p.attrelid = i.inhparent AND p.attname = a.attname
                                                  JOIN pg_attrdef d ON d.adrelid = p.attrelid AND d.adnum = p.attnum
                                                  WHERE i.inhrelid = c.oid))),
     other AS (
        SELECT o.catalog, o.oid FROM object o LEFT JOIN pg_namespace n ON n.oid = o.ns
         WHERE o.oid >= 16384 AND coalesce(n.nspname !~ '^pg_(toast_)?temp_', true)
        UNION SELECT refclassid, refobjid FROM pg_depend WHERE objid >= 16384 AND refobjid < 16384
        UNION SELECT 'pg_class'::regclass, oid FROM pg_class WHERE oid < 16384
        UNION SELECT 'pg_namespace'::regclass, oid FROM pg_namespace WHERE nspname = 'public'
        UNION SELECT 'pg_authid'::regclass, oid FROM pg_roles WHERE oid >= 16384
        UNION SELECT 'pg_tablespace'::regclass, oid FROM pg_tablespace WHERE oid >= 16384)
SELECT (SELECT count(*) + count(*) FILTER (WHERE relkind = 'm') + 2 * count(*) FILTER (WHERE relkind = 'm' AND reltoastrelid <> 0)
          FROM relation),
       (SELECT count(*) FROM other) + (SELECT count(*) FROM inherited_default) + 2 * (SELECT count(*) FROM relation WHERE relkind = 'm') + 1,
       current_setting('max_locks_per_transaction')::int, current_setting('max_connections')::int,
       current_setting('max_prepared_transactions')::int"""
# Room for the locks that other sessions on the DB server hold while step 3
# runs (autovacuum, other databases, monitoring; the App is stopped): about 15
# sessions at the default of 64. It also covers the objects that the install
# script of an extension makes and drops in step 3, which no catalog shows
# afterwards (measured: cube 14, seg 14, intarray 12, hstore 4, pg_trgm and
# unaccent 0).
RESTORE_LOCK_MARGIN = 1000
# The max_locks_per_transaction of the DB servers of this project, which
# deploy-db.sh sets: with 100 connections, room for 12800 locks. A database
# that needs more gets its own value.
RESTORE_LOCK_SETTING = 128
def restore_locks(cur):
    cur.execute(RESTORE_LOCKS)
    return tuple(int(value) for value in cur.fetchone())
def restore_locks_line(locks):
    relations, others, per_transaction, connections, prepared = locks
    return 'RESTORE_LOCKS %d %d %d %d %d %d' % (relations, others, RESTORE_LOCK_MARGIN, per_transaction, connections, prepared)
def restore_locks_problem(locks):
    # The size of the lock table as the PostgreSQL documentation gives it.
    # PostgreSQL 16 also counts its background processes, and a full table
    # can take free shared memory, so a restore can pass with less room (half
    # in a test), but nothing promises that.
    relations, others, per_transaction, connections, prepared = locks
    need = relations + others + RESTORE_LOCK_MARGIN
    room = per_transaction * (connections + prepared)
    if need <= room:
        return None
    # The value to set: 128, or what the need asks, and never less than the
    # value the DB server has.
    return ('the way back can fail. restore.txt step 3 restores the backup of database %s in one transaction, which can lock'
            ' up to %d objects (%d tables, indexes, sequences, views and TOAST tables, and %d constraints, types, system tables'
            ' and other objects). With a margin of %d for the other sessions on the DB server, it can need %d locks, and the DB'
            ' server has room for %d: max_locks_per_transaction %d x (max_connections %d + max_prepared_transactions %d). With'
            ' too few, step 3 can stop with "out of shared memory", leave the database empty and the App down. Set'
            ' max_locks_per_transaction to %d on the DB server and restart PostgreSQL there, with the App stopped (README.md,'
            ' "The lock table of the DB server", gives the steps). Then run --upgrade again'
            % (db, relations + others, relations, others, RESTORE_LOCK_MARGIN, need, room, per_transaction, connections,
               prepared, max(per_transaction, RESTORE_LOCK_SETTING, -(-need // (connections + prepared)))))
def refuse(problems, code=1):
    for problem in problems:
        print('Database preflight: module upgrade refused: ' + problem, file=sys.stderr)
    sys.exit(code)
def retired_export(cur):
    # Each retired table, the rows of each retired column with a value, and the
    # retired attachments (their files are in filestore.tar.gz), as CSV.
    buffer = io.BytesIO()
    def add(archive, name, header, rows):
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(header)
        writer.writerows(rows)
        data = text.getvalue().encode()
        info = tarfile.TarInfo('retired-data/' + name)
        info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
        archive.addfile(info, io.BytesIO(data))
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for table in RETIRED_TABLES:
            if table_exists(cur, table):
                cur.execute('SELECT * FROM "%s" ORDER BY id' % table)
                add(archive, 'tables/' + table + '.csv', [d[0] for d in cur.description], cur.fetchall())
        for table, column, condition in RETIRED_COLUMNS:
            if column_exists(cur, table, column):
                cur.execute('SELECT id, "%s" FROM "%s" WHERE %s ORDER BY id' % (column, table, condition))
                add(archive, 'columns/' + table + '.' + column + '.csv', ['id', column], cur.fetchall())
        where, values = in_list('res_model', RETIRED_MODELS)
        cur.execute('SELECT id, name, res_model, res_field, res_id, store_fname, checksum, mimetype, file_size FROM ir_attachment WHERE '
                    + where + ' ORDER BY id', values)
        add(archive, 'attachments.csv', [d[0] for d in cur.description], cur.fetchall())
    sys.stdout.buffer.write(buffer.getvalue())
def mark_upgrading(cn, cur, installed, fingerprint):
    # Before -u: the cron flags, the last mail and the modules, then the mark.
    old_fingerprint, old_release, new_release = sys.argv[2:5]
    cur.execute('SELECT id, active FROM ir_cron ORDER BY id')
    crons = {str(cron): bool(active) for cron, active in cur.fetchall()}
    mail = 0
    if table_exists(cur, 'mail_mail'):
        cur.execute('SELECT max(id) FROM mail_mail')
        mail = cur.fetchone()[0] or 0
    cur.execute("SELECT count(*) FROM res_users WHERE login = 'agent'")
    record = {'from_fingerprint': old_fingerprint, 'to_fingerprint': fingerprint, 'from_release': old_release,
              'to_release': new_release, 'marked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'modules': upgrade_modules(installed, image_versions()), 'crons': crons, 'mail_max_id': mail,
              'agent_login': cur.fetchone()[0] > 0, 'held_mail': [], 'released_mail': []}
    put(cur, KIT_PARAM, json.dumps(record, sort_keys=True))
    put(cur, 'perodua.image_modhash', UPGRADING + old_fingerprint)
    cn.commit()
    print('MARKED')
    sys.exit(0)
def verify_upgrade(cn, cur, modules, stored, fingerprint):
    record = kit_record(cur)
    if not record or stored.get('perodua.image_modhash') != UPGRADING + record.get('from_fingerprint', ''):
        fail('the mark of this module upgrade is missing or damaged')
    if record['to_fingerprint'] != fingerprint:
        fail('the module upgrade was marked for other modules than this release has')
    # First what keeps the running App safe, whatever the checks below find.
    # The cron flags as they were before -u. A cron the upgrade created keeps
    # its flag. A mock intake pull never goes on while its system is mock.
    params = params_like(cur, 'perodua_integration.mode%')
    cur.execute("SELECT res_id, module, name FROM ir_model_data WHERE model = 'ir.cron'")
    xmlids = {res_id: module + '.' + name for res_id, module, name in cur.fetchall()}
    cur.execute('SELECT id, active FROM ir_cron ORDER BY id')
    restored, kept_off, new = 0, [], []
    for cron, active in cur.fetchall():
        before = record['crons'].get(str(cron))
        target = bool(active) if before is None else before
        system = MOCK_INTAKE_CRONS.get(xmlids.get(cron))
        if system and system_mode(params, system) == 'mock' and target:
            target = False
            kept_off.append(xmlids[cron])
        if before is None:
            new.append('%s (%s)' % (xmlids.get(cron, 'id %d' % cron), 'on' if target else 'off'))
        if target != bool(active):
            cur.execute('UPDATE ir_cron SET active = %s WHERE id = %s', (target, cron))
            restored += target == before
    # Every mail of the queue (state outgoing) waits for the owner's review:
    # the mail the run queued, and the mail that waited before it, which the
    # run may have changed (a migration gives an older release mail its
    # address). A row in any other state stays as it is, so a second run finds
    # the held rows as they are and counts each of them once.
    held = []
    if table_exists(cur, 'mail_mail'):
        cur.execute("SELECT id FROM mail_mail WHERE state = 'outgoing' ORDER BY id")
        held = [row[0] for row in cur.fetchall()]
        if held:
            where, values = in_list('id', held)
            cur.execute("UPDATE mail_mail SET state = 'exception', failure_reason = %s WHERE state = 'outgoing' AND " + where,
                        (HOLD_REASON,) + values)
    record['held_mail'] = sorted(set(record['held_mail']) | set(held))
    held_queued, held_before = held_counts(record)
    put(cur, KIT_PARAM, json.dumps(record, sort_keys=True))
    cn.commit()
    print('Cron jobs: %d flags put back as before the upgrade.' % restored)
    for xmlid in kept_off:
        print('Cron jobs: %s stays off: it reads the mock feed while its system resolves to mock.' % xmlid)
    if new:
        print('Cron jobs new with this release: ' + ', '.join(new) + '.')
    print('Mail held (state exception) until deploy-app.sh --release-queued-mail: %d that the upgrade queued,'
          ' %d that waited in the queue before it.' % (held_queued, held_before))
    # Then the result itself (plan section 3, go/no-go).
    installed = {name for name, (state, _, _) in modules.items() if state == 'installed'}
    problems = ['%s is %s' % (name, state) for name, (state, _, _) in sorted(modules.items())
                if state in ('to install', 'to upgrade', 'to remove')]
    problems += [name + ' is still installed' for name in RETIRED_MODULES + NEVER_INSTALLED if name in installed]
    image = image_versions()
    for name in sorted(installed):
        if name.startswith('perodua_') and image.get(name) != modules[name][2]:
            problems.append('%s is at version %s, the release at %s' % (name, modules[name][2], image.get(name, 'none (not in the release)')))
    problems += [name + ' is no longer installed' for name in record['modules'] if name not in installed]
    seed = params_like(cur, 'perodua_demo.seeded', 'perodua.uat_init', 'perodua.runtime_profile')
    if seed.get('perodua_demo.seeded'):
        problems.append('perodua_demo.seeded is set: sample data was loaded')
    if seed.get('perodua.uat_init') != 'complete' or seed.get('perodua.runtime_profile') != PROFILE:
        problems.append('the release stamps perodua.uat_init and perodua.runtime_profile changed')
    cur.execute("SELECT count(*) FROM res_users WHERE login = 'agent'")
    if cur.fetchone()[0] and not record['agent_login']:
        problems.append('the sample user agent was created')
    if problems:
        for problem in problems:
            print('Database preflight: the upgraded database is not right: ' + problem, file=sys.stderr)
        sys.exit(1)
    put(cur, 'perodua.image_modhash', UPGRADED + fingerprint)
    cn.commit()
    print('UPGRADE_VERIFIED %d %d' % (held_queued, held_before))
    sys.exit(0)
def release_mail(cn, cur):
    # Only the rows verify-upgrade held and that are still held: a row the
    # owner deleted, cancelled or sent again since stays as it is. Two
    # updates, so that each number is the count of the rows it changed.
    record = kit_record(cur)
    held = (record or {}).get('held_mail') or []
    released = [0, 0]
    if held:
        where, values = in_list('id', held)
        for index, side in enumerate(('id > %s', 'id <= %s')):
            cur.execute("UPDATE mail_mail SET state = 'outgoing', failure_reason = NULL WHERE state = 'exception' AND failure_reason = %s AND "
                        + where + ' AND ' + side, (HOLD_REASON,) + values + (record['mail_max_id'],))
            released[index] = cur.rowcount
        record['released_mail'] = sorted(set(record.get('released_mail', [])) | set(held))
        record['held_mail'] = []
        put(cur, KIT_PARAM, json.dumps(record, sort_keys=True))
        cn.commit()
    print('RELEASED %d %d' % tuple(released))
    sys.exit(0)
with connect(db) as cn:
    with cn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')")
        if not cur.fetchone()[0]:
            if mode not in ('check', 'upgrade-check'): fail('database is still empty; cannot ' + mode)
            print('EMPTY')
            sys.exit(0)
        cur.execute("SELECT to_regclass('public.ir_module_module'), to_regclass('public.ir_config_parameter')")
        if not all(cur.fetchone()): fail('target is neither empty nor an initialized Odoo database')
        cur.execute('SELECT name, state, demo, latest_version FROM ir_module_module')
        modules = {name: (state, demo, version) for name, state, demo, version in cur.fetchall()}
        installed = {name for name, (state, _, _) in modules.items() if state == 'installed'}
        names = sorted(p for p in pathlib.Path('/opt/perodua-addons').glob('perodua_*') if p.is_dir())
        fingerprint = hashlib.md5(b''.join((p / '__manifest__.py').read_bytes() for p in names)).hexdigest()
        cur.execute("SELECT key,value FROM ir_config_parameter WHERE key IN ('perodua.runtime_profile','perodua.image_modhash','perodua.uat_init')")
        stored = dict(cur.fetchall())
        uat = stored.get('perodua.uat_init')
        modhash = stored.get('perodua.image_modhash') or ''
        if modhash.startswith(UPGRADING):
            # A module upgrade marked this database: whether -u ran, ran part
            # of the way or ran fully, only verify-upgrade may continue.
            if mode == 'verify-upgrade':
                verify_upgrade(cn, cur, modules, stored, fingerprint)
            fail('database is marked by a module upgrade that did not finish (perodua.image_modhash ' + modhash + '): no release can run on it.'
                 ' Restore the backup that deploy-app.sh --upgrade made before it: restore.txt in its backup folder')
        if mode == 'verify-upgrade':
            fail('verify-upgrade applies only to a database that a module upgrade marked')
        if any(state in ('to install', 'to upgrade', 'to remove') for state, _, _ in modules.values()):
            # Odoo commits each module as it installs it, so a killed -i
            # (timeout, Ctrl-C, lost session) leaves the rest 'to install'.
            if 'perodua.runtime_profile' not in stored:
                fail('database contains module changes left by an interrupted installation. If an earlier --init-db stopped part-way, recreate the empty database on the DB server; see docs/DEPLOYMENT.md')
            fail('database contains pending module changes; finish them separately')
        if mode == 'public-urls':
            # PDF reports load their styles from this Odoo itself, not through
            # the front proxy. A frozen base URL is not replaced by the address
            # an administrator signs in through. Written before the containers
            # are recreated, so Odoo starts with these values.
            base_url = sys.argv[2] if len(sys.argv) > 2 else ''
            label = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
            url = re.fullmatch(r'https?://(' + label + r'(?:\.' + label + r')*)(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9-]{0,30})?', base_url)
            if base_url and not (url and len(url.group(1)) <= 253):
                fail('invalid public base URL')
            put(cur, 'report.url', 'http://127.0.0.1:8069')
            if base_url:
                put(cur, 'web.base.url', base_url)
                put(cur, 'web.base.url.freeze', 'True')
            cn.commit()
            print('PUBLIC_URLS_SET')
            sys.exit(0)
        if mode == 'retired-export':
            retired_export(cur)
            sys.exit(0)
        def excluded_records():
            cur.execute('SELECT count(*) FROM ir_model_data WHERE module=%s', (excluded,))
            return cur.fetchone()[0]
        def fresh_problem():
            # The post-initialization assertions: whatever the guard concluded
            # beforehand, the database itself must show the module absent, and
            # the modules must be the ones this image installs.
            missing = sorted(set(init_modules) - installed)
            if missing: return 'fresh initialization did not install: ' + ', '.join(missing)
            if excluded in installed: return excluded + ' is installed; a fresh UAT database must not contain it'
            if excluded_records(): return 'records belonging to ' + excluded + ' were loaded'
            demo = sorted(name for name, (state, loaded, _) in modules.items() if loaded and state == 'installed')
            if demo: return 'Odoo demo data was loaded for: ' + ', '.join(demo[:10])
            image = image_versions()
            other = sorted(name for name in installed if name.startswith('perodua_') and image.get(name) != modules[name][2])
            if other: return 'modules were installed by a different release image: ' + ', '.join(other[:10])
            return None
        def fresh_checks():
            problem = fresh_problem()
            if problem: fail(problem)
        if mode == 'mark-pending':
            if uat == 'complete': fail('database already completed its UAT initialization')
            if 'perodua.runtime_profile' in stored: fail('database carries a release stamp; it is not a fresh initialization')
            fresh_checks()
            put(cur, 'perodua.uat_init', 'pending')
            # Commit before exiting: leaving the connection block through
            # SystemExit makes psycopg2 roll the marker back.
            cn.commit()
            print('PENDING')
            sys.exit(0)
        checking = mode in ('check', 'upgrade-check')
        if uat is None and 'perodua.runtime_profile' not in stored:
            # Initialized but neither marked nor stamped: an --init-db that
            # stopped between installing the modules and recording that, or
            # not a release database at all. Only the former may be finished.
            if not checking: fail(mode + ' applies only to a fresh initialization that is awaiting its UAT setup')
            problem = fresh_problem()
            if problem:
                fail('database is initialized but has no release stamp and is not a complete fresh UAT initialization (' + problem + '). If an earlier --init-db failed part-way, recreate the empty database on the DB server; see docs/DEPLOYMENT.md')
            print('SETUP_UNMARKED')
            sys.exit(0)
        if uat == 'pending':
            fresh_checks()
            if checking:
                print('SETUP_PENDING')
                sys.exit(0)
            if mode == 'verify-fresh':
                cur.execute("SELECT count(*) FROM ir_attachment WHERE store_fname IS NOT NULL")
                print('VERIFIED ' + str(cur.fetchone()[0]))
                sys.exit(0)
            if mode == 'stamp':
                stored.update({'perodua.runtime_profile': PROFILE, 'perodua.image_modhash': fingerprint})
                for key in ('perodua.runtime_profile', 'perodua.image_modhash'): put(cur, key, stored[key])
                put(cur, 'perodua.uat_init', 'complete')
                uat = 'complete'
        elif not checking and mode not in ('mark-upgrading', 'stamp-upgrade', 'release-mail'):
            fail(mode + ' applies only to a fresh initialization that is awaiting its UAT setup')
        required = init_modules if uat == 'complete' else restored_modules
        missing = sorted(set(required) - installed)
        if missing: fail('required modules are not installed: ' + ', '.join(missing))
        if uat == 'complete' and (excluded in installed or excluded_records()):
            fail(excluded + ' was installed into this UAT database after its initialization')
        if stored.get('perodua.runtime_profile') != PROFILE:
            fail('runtime profile does not match client-stable-uiux; restore the correct database')
        cur.execute("SELECT count(*) FROM ir_attachment WHERE store_fname IS NOT NULL")
        attachments = cur.fetchone()[0]
        modhash = stored.get('perodua.image_modhash') or ''  # a fresh stamp above has just set it
        upgrade_from = sys.argv[2] if mode in ('upgrade-check', 'mark-upgrading') and len(sys.argv) > 2 else ''
        if modhash != fingerprint and upgrade_from and modhash == upgrade_from:
            # The modules of the release the module upgrade starts from. The
            # mark checks again what upgrade-check checked.
            problems = module_upgrade_problems(cur, modules, installed, uat, excluded_records())
            # From the mark on, the backup is the only way back: it must fit.
            locks = restore_locks(cur)
            lock_problem = restore_locks_problem(locks)
            if lock_problem:
                # deploy-app.sh reads the numbers for the steps it prints.
                print(restore_locks_line(locks))
                problems.append(lock_problem)
            if problems:
                # mark-upgrading: exit 3 says that nothing was written, so
                # deploy-app.sh starts the old App again. Every other failure
                # exits 1, which may come after the commit of the mark.
                refuse(problems, 3 if mode == 'mark-upgrading' else 1)
            if mode == 'mark-upgrading':
                mark_upgrading(cn, cur, installed, fingerprint)
            for kind, name, count in retired_counts(cur):
                print('RETIRED %s %s %d' % (kind, name, count))
            print(restore_locks_line(locks))
            print('MODULES ' + ','.join(upgrade_modules(installed, image_versions())))
            print('MODULE_UPGRADE ' + str(attachments))
            sys.exit(0)
        if modhash == UPGRADED + fingerprint:
            # Upgraded and verified; the deployment of this release finishes it.
            if mode == 'stamp-upgrade':
                put(cur, 'perodua.image_modhash', fingerprint)
                cn.commit()
                modhash = fingerprint
            elif mode == 'upgrade-check' or (mode == 'check' and '--accept-pending' in sys.argv[2:]):
                print('UPGRADE_PENDING ' + str(attachments))
                sys.exit(0)
            else:
                fail('the module upgrade of this database to this release is checked but not finished: run deploy-app.sh --dir DIR from the'
                     ' scripts folder of the new release, without --upgrade (it runs no module upgrade again)')
        elif mode in ('mark-upgrading', 'stamp-upgrade'):
            fail(mode + ' does not apply to this database (perodua.image_modhash ' + modhash + ')')
        if modhash != fingerprint:
            if mode == 'upgrade-check':
                fail('database module fingerprint does not match this release, and is not the fingerprint this release upgrades modules from')
            fail('database module fingerprint does not match this release; upgrades require a separate plan')
        if mode == 'release-mail':
            release_mail(cn, cur)
        if mode == 'upgrade-check':
            print(restore_locks_line(restore_locks(cur)))
        print('READY ' + str(attachments))
