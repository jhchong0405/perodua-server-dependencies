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
import ast, hashlib, os, pathlib, re, sys
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
with connect(db) as cn:
    with cn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')")
        if not cur.fetchone()[0]:
            if mode != 'check': fail('database is still empty; cannot ' + mode)
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
        if uat is None and 'perodua.runtime_profile' not in stored:
            # Initialized but neither marked nor stamped: an --init-db that
            # stopped between installing the modules and recording that, or
            # not a release database at all. Only the former may be finished.
            if mode != 'check': fail(mode + ' applies only to a fresh initialization that is awaiting its UAT setup')
            problem = fresh_problem()
            if problem:
                fail('database is initialized but has no release stamp and is not a complete fresh UAT initialization (' + problem + '). If an earlier --init-db failed part-way, recreate the empty database on the DB server; see docs/DEPLOYMENT.md')
            print('SETUP_UNMARKED')
            sys.exit(0)
        if uat == 'pending':
            fresh_checks()
            if mode == 'check':
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
        elif mode != 'check':
            fail(mode + ' applies only to a fresh initialization that is awaiting its UAT setup')
        required = init_modules if uat == 'complete' else restored_modules
        missing = sorted(set(required) - installed)
        if missing: fail('required modules are not installed: ' + ', '.join(missing))
        if uat == 'complete' and (excluded in installed or excluded_records()):
            fail(excluded + ' was installed into this UAT database after its initialization')
        if stored.get('perodua.runtime_profile') != PROFILE:
            fail('runtime profile does not match client-stable-uiux; restore the correct database')
        if stored.get('perodua.image_modhash') != fingerprint:
            fail('database module fingerprint does not match this release; upgrades require a separate plan')
        cur.execute("SELECT count(*) FROM ir_attachment WHERE store_fname IS NOT NULL")
        attachments = cur.fetchone()[0]
        print('READY ' + str(attachments))
