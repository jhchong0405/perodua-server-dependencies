"""Run deploy-app.sh's generated preflight against real PostgreSQL 16.

Invoked by run-deploy-db-integration.sh inside its disposable container, on a
database that deploy-db.sh created with DB_MODE=empty. It executes the exact
preflight.py text that deploy-app.sh writes, over TCP as the application role,
through the fresh UAT state machine and the unchanged restored-database path.

usage: check_app_preflight.py SOURCE_DIR TEST_DIR DB_NAME DB_USER PASSWORD_FILE
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import psycopg2

source_dir, test_dir, db_name, db_user, password_file = sys.argv[1:]
script = (Path(source_dir) / 'deploy-app.sh').read_text()
body = script.split('cat > "$TEMP_DIR/preflight.py" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
preflight = Path(test_dir) / 'preflight.py'
preflight.write_text(body + '\n')

INIT = ['perodua_client_stable', 'perodua_gateway', 'perodua_forecast_workbook',
        'perodua_supplier_execution', 'perodua_uiux_api']
EXCLUDED = 'perodua_demo_client'
RESTORED = INIT[:1] + [EXCLUDED] + INIT[1:]
VERSION = '19.0.1.0.0'

password = Path(password_file).read_text().rstrip('\n')
Path('/run/secrets').mkdir(parents=True, exist_ok=True)
Path('/run/secrets/db_password').write_text(password)  # no newline, as deploy-app.sh writes it
# The image's addon tree as preflight reads it: manifests with versions.
addons = Path('/opt/perodua-addons')
for name in INIT + [EXCLUDED]:
    (addons / name).mkdir(parents=True, exist_ok=True)
    (addons / name / '__manifest__.py').write_text(f"{{'name': '{name}', 'version': '{VERSION}'}}\n")
FINGERPRINT = hashlib.md5(b''.join(
    (path / '__manifest__.py').read_bytes()
    for path in sorted(p for p in addons.glob('perodua_*') if p.is_dir()))).hexdigest()

ENV = dict(os.environ, DB_HOST='127.0.0.1', DB_PORT='5432', DB_USER=db_user, DB_NAME=db_name,
           INIT_MODULES=','.join(INIT), RESTORED_MODULES=','.join(RESTORED), EXCLUDED_MODULE=EXCLUDED)
PASSES = 0


def die(message):
    sys.exit(f'FAIL: preflight: {message}')


def run(mode, expect_ok=True):
    result = subprocess.run([sys.executable, str(preflight), mode], env=ENV, text=True,
                            capture_output=True, timeout=60)
    if expect_ok and result.returncode:
        die(f'{mode} failed unexpectedly: {result.stderr.strip()}')
    if not expect_ok and not result.returncode:
        die(f'{mode} should have failed but printed {result.stdout.strip()!r}')
    return result.stdout.strip() if expect_ok else result.stderr.strip()


def sql(statement, params=None):
    with psycopg2.connect(host='127.0.0.1', port=5432, user=db_user, password=password,
                          dbname=db_name) as cn, cn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall() if cur.description else None


def check(condition, message):
    global PASSES
    if not condition:
        die(message)
    PASSES += 1


# 1. Nothing initialized yet.
check(run('check') == 'EMPTY', 'a new empty-mode database must report EMPTY')
check('still empty' in run('mark-pending', expect_ok=False), 'mark-pending must refuse an empty database')

# 2. What `odoo -i` leaves behind on a fresh UAT database.
sql('''CREATE TABLE ir_module_module (id serial PRIMARY KEY, name varchar UNIQUE NOT NULL,
                                      state varchar NOT NULL, demo boolean DEFAULT false,
                                      latest_version varchar);
       CREATE TABLE ir_config_parameter (id serial PRIMARY KEY, key varchar UNIQUE NOT NULL, value text NOT NULL);
       CREATE TABLE ir_model_data (id serial PRIMARY KEY, module varchar NOT NULL, name varchar NOT NULL);
       CREATE TABLE ir_attachment (id serial PRIMARY KEY, store_fname varchar);''')
sql("INSERT INTO ir_module_module (name, state) VALUES ('base', 'installed')")
sql('INSERT INTO ir_module_module (name, state, latest_version) SELECT unnest(%s::text[]), %s, %s',
    (INIT, 'installed', VERSION))
sql('INSERT INTO ir_module_module (name, state) VALUES (%s, %s)', (EXCLUDED, 'uninstalled'))
sql("INSERT INTO ir_attachment (store_fname) VALUES ('ab/abcdef'), (NULL)")

# 3. A run that installed the modules but died before recording them can be
#    finished; one that did not leave a complete fresh install cannot.
check(run('check') == 'SETUP_UNMARKED', 'an unrecorded fresh initialization must report SETUP_UNMARKED')
sql("UPDATE ir_module_module SET demo=true WHERE name='perodua_gateway'")
check('recreate the empty database' in run('check', expect_ok=False),
      'an unrecorded database that is not a clean fresh install must be refused with the recovery step')
sql('UPDATE ir_module_module SET demo=false')
# Odoo commits module by module, so a killed `odoo -i` leaves the rest 'to install'.
sql("UPDATE ir_module_module SET state='to install' WHERE name='perodua_uiux_api'")
check('recreate the empty database' in run('check', expect_ok=False),
      'modules left to install by a killed initialization must be refused with the recovery step')
sql("UPDATE ir_module_module SET state='installed' WHERE name='perodua_uiux_api'")

# 4. The regression that motivated this file: the marker must be persisted.
check(run('mark-pending') == 'PENDING', 'mark-pending must accept a fresh initialization')
check(sql("SELECT value FROM ir_config_parameter WHERE key='perodua.uat_init'") == [('pending',)],
      'mark-pending did not commit its marker')
check(run('check') == 'SETUP_PENDING', 'a marked database must report SETUP_PENDING')
check(run('verify-fresh') == 'VERIFIED 1', 'verify-fresh must accept a clean fresh database')

# 5. Post-initialization assertions: the module, its records, demo data, and
#    modules that this image did not install are all refused.
sql('UPDATE ir_module_module SET state=%s WHERE name=%s', ('installed', EXCLUDED))
check(EXCLUDED in run('verify-fresh', expect_ok=False), 'an installed excluded module must be refused')
sql('UPDATE ir_module_module SET state=%s WHERE name=%s', ('uninstalled', EXCLUDED))
sql('INSERT INTO ir_model_data (module, name) VALUES (%s, %s)', (EXCLUDED, 'partner_hq'))
check('records belonging to' in run('verify-fresh', expect_ok=False),
      'records loaded under the excluded module must be refused')
sql('DELETE FROM ir_model_data')
sql("UPDATE ir_module_module SET demo=true WHERE name='perodua_gateway'")
check('demo data' in run('verify-fresh', expect_ok=False), 'loaded demo data must be refused')
sql('UPDATE ir_module_module SET demo=false')
sql("UPDATE ir_module_module SET latest_version='19.0.0.9.0' WHERE name='perodua_uiux_api'")
check('different release image' in run('stamp', expect_ok=False),
      'modules installed by another release must not be stamped')
sql('UPDATE ir_module_module SET latest_version=%s WHERE name=%s', (VERSION, 'perodua_uiux_api'))

# 6. Stamping turns it READY for good, held to the fresh module set.
check(run('stamp') == 'READY 1', 'stamp must report READY with the attachment count')
stored = dict(sql('SELECT key, value FROM ir_config_parameter'))
check(stored.get('perodua.uat_init') == 'complete' and stored.get('perodua.runtime_profile') == 'client-stable-uiux'
      and stored.get('perodua.image_modhash') == FINGERPRINT, f'stamp did not persist its identity: {stored}')
check(run('check') == 'READY 1', 'a stamped UAT database must report READY')
check('awaiting its UAT setup' in run('stamp', expect_ok=False), 'a second stamp must be refused')
check('already completed' in run('mark-pending', expect_ok=False), 'a stamped database must not be marked again')
sql('INSERT INTO ir_model_data (module, name) VALUES (%s, %s)', (EXCLUDED, 'partner_hq'))
check('after its initialization' in run('check', expect_ok=False),
      'a UAT database that later gained records of the excluded module must be refused')
sql('DELETE FROM ir_model_data')
sql("UPDATE ir_module_module SET state='to upgrade' WHERE name='perodua_gateway'")
message = run('check', expect_ok=False)
check('pending module changes' in message and 'recreate' not in message,
      'pending module operations on a stamped database must be refused without the fresh-install recovery step')
sql("UPDATE ir_module_module SET state='installed' WHERE name='perodua_gateway'")

# 7. A restored database keeps exactly its previous test: the excluded module is required.
sql("DELETE FROM ir_config_parameter WHERE key='perodua.uat_init'")
check(EXCLUDED in run('check', expect_ok=False), 'a restored database without the dataset module must be refused')
sql('UPDATE ir_module_module SET state=%s WHERE name=%s', ('installed', EXCLUDED))
check(run('check') == 'READY 1', 'a restored database with every release module must be READY')
check('awaiting its UAT setup' in run('verify-fresh', expect_ok=False), 'verify-fresh must refuse a restored database')
check('carries a release stamp' in run('mark-pending', expect_ok=False), 'a restored database must never be marked fresh')

print(f'app preflight: {PASSES} checks passed against PostgreSQL')
