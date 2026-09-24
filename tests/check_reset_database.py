"""Run service.sh reset's reset_database.py against real PostgreSQL 16.

Invoked by run-deploy-db-integration.sh inside its disposable container, on a
database that deploy-db.sh created with DB_MODE=empty. The application role
first fills it the way Odoo does: every table has create_uid and write_uid
foreign keys to res_users, ir_act_window and ir_act_server INHERIT ir_actions,
and there are an api schema with views, a free sequence and a function. The
checks: dropping it all in one transaction runs out of lock space on a server
with default settings (why the script works in batches); a failed login changes
nothing (exit 3); the script leaves the database EMPTY for deploy-app.sh
--init-db, with public as a new database has it, and keeps the database, its
owner and who may connect; and a second run on the empty database passes.

usage: check_reset_database.py SCRIPTS_DIR TEST_DIR DB_NAME DB_USER PASSWORD_FILE
"""
import os
from pathlib import Path
import subprocess
import sys

import psycopg2

scripts_dir, test_dir, db_name, db_user, password_file = sys.argv[1:]
password = Path(password_file).read_text().rstrip('\n')
Path('/run/secrets').mkdir(parents=True, exist_ok=True)
Path('/run/secrets/db_password').write_text(password)  # no newline, as deploy-app.sh writes it
wrong = Path(test_dir) / 'reset-wrong.password'
wrong.write_text('not-the-password')
script = (Path(scripts_dir) / 'deploy-app.sh').read_text()
preflight = Path(test_dir) / 'reset-preflight.py'
preflight.write_text(script.split('cat > "$TEMP_DIR/preflight.py" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0] + '\n')
# As in the container: compose.yml sets DB_PASSWORD_FILE and the image's
# entrypoint takes it out again, so the script reads /run/secrets/db_password.
ENV = {key: value for key, value in os.environ.items() if key != 'DB_PASSWORD_FILE'}
ENV.update(DB_HOST='127.0.0.1', DB_PORT='5432', DB_USER=db_user, DB_NAME=db_name,
           INIT_MODULES='perodua_client_stable', RESTORED_MODULES='perodua_client_stable',
           EXCLUDED_MODULE='perodua_demo_client')
MODELS = 900


def die(message):
    sys.exit(f'FAIL: reset_database: {message}')


def connect():
    return psycopg2.connect(host='127.0.0.1', port=5432, user=db_user, password=password, dbname=db_name)


def sql(statement):
    connection = connect()
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(statement)
            return cursor.fetchall() if cursor.description else None
    finally:
        connection.close()


def reset(**env):
    return subprocess.run([sys.executable, str(Path(scripts_dir) / 'reset_database.py')],
                          env=dict(ENV, **env), text=True, capture_output=True, timeout=600)


def relations():
    return sql("SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace")[0][0]


# What Odoo leaves: the inheriting tables first, so that they share a batch.
sql('''CREATE TABLE res_users (id serial PRIMARY KEY, login varchar NOT NULL,
                               create_uid int REFERENCES res_users, write_uid int REFERENCES res_users);
       INSERT INTO res_users (login) VALUES ('admin');
       CREATE TABLE ir_actions (id serial PRIMARY KEY, name varchar,
                                create_uid int REFERENCES res_users, write_uid int REFERENCES res_users);
       CREATE TABLE ir_act_window (res_model varchar, PRIMARY KEY (id)) INHERITS (ir_actions);
       CREATE TABLE ir_act_server (code text, PRIMARY KEY (id)) INHERITS (ir_actions);
       INSERT INTO ir_act_window (name, res_model) VALUES ('Orders', 'sale.order');
       CREATE SEQUENCE ir_sequence_001;
       CREATE FUNCTION fixture_users() RETURNS bigint LANGUAGE sql AS 'SELECT count(*) FROM res_users';
       CREATE SCHEMA api;
       CREATE VIEW api.users_v1 AS SELECT id, login FROM public.res_users;
       CREATE VIEW api.version_v1 AS SELECT 1 AS version;''')
for first in range(0, MODELS, 100):  # creating takes locks too
    sql(';'.join(f'''CREATE TABLE model_{n} (id serial PRIMARY KEY, name text,
                     create_uid int REFERENCES res_users ON DELETE SET NULL,
                     write_uid int REFERENCES res_users ON DELETE SET NULL)''' for n in range(first, first + 100)))
database = sql('SELECT oid, datdba, datacl::text FROM pg_database WHERE datname = current_database()')
filled = relations()

# Control: in one transaction, the drop needs more locks than the server holds.
connection = connect()
try:
    with connection.cursor() as cursor:
        cursor.execute('DROP SCHEMA public CASCADE')
    die('a one-shot DROP SCHEMA public CASCADE fit in the lock table; the fixture no longer shows why the reset batches')
except psycopg2.Error as error:
    if error.pgcode != '53200':  # out of shared memory
        die(f'the one-shot drop failed for another reason: {error}')
finally:
    connection.rollback()
    connection.close()

result = reset(DB_PASSWORD_FILE=str(wrong))
if result.returncode != 3 or 'Cannot connect to the database' not in result.stderr:
    die(f'a failed login must exit 3 with its reason, got {result.returncode}: {result.stderr.strip()}')
if relations() != filled:
    die('a failed login changed the database')

result = reset()
if result.returncode:
    die(f'reset failed ({result.returncode}): {result.stderr.strip()}')
if relations():
    die('relations are left in public')
if sql("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'") != [('public',)]:
    die('schemas other than public are left')
if sql("SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace") != [(0,)]:
    die('functions are left in public')
schema = sql("SELECT pg_get_userbyid(nspowner), nspacl::text, obj_description(oid, 'pg_namespace') "
             "FROM pg_namespace WHERE nspname = 'public'")
if schema != [('pg_database_owner', '{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}',
               'standard public schema')]:
    die(f'public is not as a new database has it: {schema}')
if sql('SELECT oid, datdba, datacl::text FROM pg_database WHERE datname = current_database()') != database:
    die('the database itself, its owner or who may connect changed')
sql('CREATE TABLE public.after_reset (id int); DROP TABLE public.after_reset')

result = reset()
if result.returncode:
    die(f'a second reset of the empty database failed: {result.stderr.strip()}')
state = subprocess.run([sys.executable, str(preflight), 'check'], env=ENV, text=True, capture_output=True, timeout=60)
if state.stdout.strip() != 'EMPTY':
    die(f'deploy-app.sh preflight does not see an empty database: {state.stdout.strip()} {state.stderr.strip()}')
print(f'reset_database: {filled} relations dropped in batches; the database is EMPTY and kept')
