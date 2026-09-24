"""Empty the App's database for service.sh reset. Runs in the Odoo image.

Drops the tables, views, sequences and functions in the public schema and in
the schemas the application account created (such as api), then creates public
again the way a new database has it. The database itself, its owner, password
and access rules stay, so deploy-app.sh --init-db finds it EMPTY.

Exit status 3: nothing was deleted. Any other failure may leave the database
partly emptied; running the reset again finishes it.
"""
import os
import pathlib
import sys

import psycopg2

# PostgreSQL locks every object a drop reaches until the transaction ends.
# Dropping an Odoo database in one go, or only res_users with the
# create_uid/write_uid foreign keys every table has on it, needs more locks
# than a server with default settings has room for ("out of shared memory").
# So the foreign keys go first and then the tables, a few hundred per
# transaction. IF EXISTS: a table can go with an earlier one in its batch, such
# as ir_act_window, which Odoo creates as a child (INHERITS) of ir_actions.
FOREIGN_KEYS = """SELECT format('ALTER TABLE IF EXISTS %%s DROP CONSTRAINT IF EXISTS %%I', conrelid::regclass, conname)
    FROM pg_constraint WHERE contype = 'f' AND conparentid = 0 AND connamespace = ANY(%s::oid[]) LIMIT 200"""
TABLES = """SELECT format('DROP TABLE IF EXISTS %%s CASCADE', oid::regclass)
    FROM pg_class WHERE relkind IN ('r', 'p', 'f') AND NOT relispartition AND relnamespace = ANY(%s::oid[]) LIMIT 100"""


def main():
    try:
        # The image's entrypoint takes DB_PASSWORD_FILE out of the environment;
        # the secret stays where compose.yml mounts it, as preflight.py reads it.
        password = pathlib.Path(os.environ.get("DB_PASSWORD_FILE", "/run/secrets/db_password")).read_text()
        connection = psycopg2.connect(
            host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], user=os.environ["DB_USER"],
            password=password, dbname=os.environ["DB_NAME"], connect_timeout=10)
    except (KeyError, OSError, psycopg2.Error) as error:
        print("Cannot connect to the database: " + (str(error).strip() or repr(error)), file=sys.stderr)
        return 3
    changed = False
    try:
        with connection.cursor() as cursor:
            # A session that still uses a table makes the reset fail instead of wait.
            cursor.execute("SET lock_timeout = '1min'")
            cursor.execute("SELECT array_agg(oid) FROM pg_namespace WHERE nspname = 'public'"
                           " OR nspowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)")
            schemas = cursor.fetchone()[0] or []
            for query in (FOREIGN_KEYS, TABLES):
                while True:
                    cursor.execute(query, (schemas,))
                    statements = [statement for (statement,) in cursor.fetchall()]
                    if not statements:
                        break
                    for statement in statements:
                        cursor.execute(statement)
                    connection.commit()
                    changed = True
            # What is left (views, sequences, functions) is small enough for one go.
            cursor.execute("SELECT format('DROP SCHEMA %%I CASCADE', nspname) FROM pg_namespace"
                           " WHERE oid = ANY(%s::oid[])", (schemas,))
            for (statement,) in cursor.fetchall():
                cursor.execute(statement)
            # As PostgreSQL 16 creates it in a new database.
            cursor.execute("CREATE SCHEMA public AUTHORIZATION pg_database_owner")
            cursor.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
            cursor.execute("COMMENT ON SCHEMA public IS 'standard public schema'")
        connection.commit()
    except psycopg2.Error as error:
        print("Could not empty the database: " + str(error).strip(), file=sys.stderr)
        return 1 if changed else 3
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
