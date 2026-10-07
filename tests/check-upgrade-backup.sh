#!/usr/bin/env bash
# deploy-app.sh --upgrade's backup and the restore steps of its restore.txt,
# against real PostgreSQL 16 and the pg_dump, pg_restore and psql of an Odoo
# image (PostgreSQL 18 client tools, as in the release images):
#
#   bash tests/check-upgrade-backup.sh IMAGE
#
# IMAGE is a local Odoo image, such as the pinned release image; nothing is
# pulled except postgres:16 if it is missing. The commands are taken from
# deploy-app.sh itself. A database shaped as Odoo leaves it (900 tables with
# foreign keys to res_users, inheriting tables, an api schema, a sequence, a
# function, trusted extensions) and an attachments volume are saved, damaged,
# emptied with reset_database.py and restored; the result must equal the
# original. The DB server's pg_restore 16 must refuse the archive, as
# restore.txt says. Every Docker resource is named perodua-upgrade-check-* and
# removed at the end.
set -Eeuo pipefail
(($# == 1)) || { printf 'Usage: bash %s IMAGE\n' "${0##*/}" >&2; exit 2; }
IMAGE=$1
KIT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
NET=perodua-upgrade-check-net PG=perodua-upgrade-check-pg VOL=perodua-upgrade-check-fs
WORK=$(mktemp -d)
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
cleanup() {
    docker rm -f "$PG" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT
docker image inspect "$IMAGE" >/dev/null || fail "$IMAGE is not a local image"
for name in "$PG" "$VOL" "$NET"; do
    ! docker inspect "$name" >/dev/null 2>&1 || fail "$name already exists; remove it first"
done
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_PASSWORD=check-superuser postgres:16 >/dev/null
# The image's entrypoint restarts the server once after initdb.
for _ in 1 2; do
    until docker exec "$PG" pg_isready -q -U postgres 2>/dev/null; do sleep 1; done
    sleep 2
done
# As deploy-db.sh DB_MODE=empty leaves it: a least-privileged owner.
docker exec "$PG" psql -X -q -U postgres -v ON_ERROR_STOP=1 \
    -c "CREATE ROLE odoo LOGIN PASSWORD 'check-password' NOSUPERUSER NOCREATEDB NOCREATEROLE" \
    -c "CREATE DATABASE perodua OWNER odoo ENCODING 'UTF8' TEMPLATE template0" \
    -c "REVOKE CONNECT ON DATABASE perodua FROM PUBLIC"
printf 'check-password' > "$WORK/db_password"
chmod 0444 "$WORK/db_password"
# What the App container has: compose.yml's settings, and DB_PASSWORD that the
# image's entrypoint reads from the secret.
ENV=(-e DB_HOST="$PG" -e DB_PORT=5432 -e DB_USER=odoo -e DB_NAME=perodua -e DB_PASSWORD=check-password)
app() { docker run --rm -i --network "$NET" "${ENV[@]}" --entrypoint '' "$@"; }
sql() { docker exec -i -e PGPASSWORD=check-password "$PG" psql -X -q -At -h 127.0.0.1 -U odoo -d perodua -v ON_ERROR_STOP=1 "$@"; }

sql >/dev/null <<'SQL'
CREATE EXTENSION pg_trgm;
CREATE EXTENSION unaccent;
CREATE TABLE res_users (id serial PRIMARY KEY, login varchar NOT NULL,
                        create_uid int REFERENCES res_users, write_uid int REFERENCES res_users);
INSERT INTO res_users (login) VALUES ('admin'), ('whadmin');
CREATE TABLE ir_actions (id serial PRIMARY KEY, name varchar,
                         create_uid int REFERENCES res_users, write_uid int REFERENCES res_users);
CREATE TABLE ir_act_window (res_model varchar, PRIMARY KEY (id)) INHERITS (ir_actions);
INSERT INTO ir_act_window (name, res_model) VALUES ('Orders', 'sale.order');
CREATE TABLE ir_module_module (id serial PRIMARY KEY, name varchar UNIQUE, state varchar);
INSERT INTO ir_module_module (name, state) VALUES ('perodua_client_stable', 'installed'), ('perodua_uiux_api', 'installed');
CREATE TABLE ir_attachment (id serial PRIMARY KEY, name varchar, store_fname varchar);
INSERT INTO ir_attachment (name, store_fname) VALUES ('a.pdf', 'ab/abcdef'), ('b.png', 'cd/cdef01');
CREATE INDEX ir_attachment_name_trgm ON ir_attachment USING gin (name gin_trgm_ops);
CREATE SEQUENCE ir_sequence_001;
SELECT setval('ir_sequence_001', 41);
CREATE FUNCTION fixture_users() RETURNS bigint LANGUAGE sql AS 'SELECT count(*) FROM res_users';
CREATE SCHEMA api;
CREATE VIEW api.users_v1 AS SELECT id, login FROM public.res_users;
SQL
for first in $(seq 0 100 800); do
    for n in $(seq "$first" $((first + 99))); do
        printf 'CREATE TABLE model_%s (id serial PRIMARY KEY, name text, create_uid int REFERENCES res_users ON DELETE SET NULL, write_uid int REFERENCES res_users ON DELETE SET NULL);\n' "$n"
    done | sql
done
sql -c "INSERT INTO model_7 (name, create_uid) SELECT 'row ' || g, 1 FROM generate_series(1, 5000) g"
snapshot() {
    sql -c "SELECT (SELECT count(*) FROM pg_class WHERE relnamespace IN ('public'::regnamespace, 'api'::regnamespace)),
                   (SELECT count(*) FROM model_7), (SELECT string_agg(login, ',' ORDER BY id) FROM res_users),
                   (SELECT last_value FROM ir_sequence_001), (SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension),
                   (SELECT fixture_users()), (SELECT count(*) FROM api.users_v1), (SELECT count(*) FROM ir_act_window)"
}
before=$(snapshot)
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL:/var/lib/odoo" --user 0 --entrypoint bash "$IMAGE" -ec '
    mkdir -p /var/lib/odoo/filestore/perodua/ab /var/lib/odoo/filestore/perodua/cd /var/lib/odoo/sessions
    printf one > /var/lib/odoo/filestore/perodua/ab/abcdef
    printf two > /var/lib/odoo/filestore/perodua/cd/cdef01
    printf session > /var/lib/odoo/sessions/s1
    chown -R odoo:odoo /var/lib/odoo' 2>/dev/null
attachments() {
    docker run --rm -v "$VOL:/var/lib/odoo" --entrypoint bash "$IMAGE" -c \
        'cd /var/lib/odoo && find filestore $([ -d sessions ] && echo sessions) -type f -printf "%p %u " -exec cat {} \; -printf "\n" | sort' 2>/dev/null
}
files_before=$(attachments)

# The backup, with the command lines of deploy-app.sh.
python3 - "$KIT/scripts/deploy-app.sh" "$WORK" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text()
runs = re.findall(r"staged (--file \"\$TEMP_DIR/no-log\.yml\" )?run --rm --no-deps -T odoo bash -c \\\n\s+'([^']*)'", text)
scripts = [script for _, script in runs]
assert len(runs) == 3 and 'pg_database_size' in scripts[0] and 'pg_dump' in scripts[1] and 'tar -czf' in scripts[2], scripts
# the dump and the archive go to standard output: no container log may keep a copy
assert [bool(no_log) for no_log, _ in runs] == [False, True, True], runs
assert "printf 'services:\\n  odoo:\\n    logging:\\n      driver: none\\n' > \"$TEMP_DIR/no-log.yml\"" in text
# a module upgrade's export of the retired data streams to the backup folder as well
assert 'staged --file "$TEMP_DIR/no-log.yml" run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py retired-export' in text
pathlib.Path(sys.argv[2], 'size.sh').write_text(scripts[0])
pathlib.Path(sys.argv[2], 'dump.sh').write_text(scripts[1])
pathlib.Path(sys.argv[2], 'archive.sh').write_text(scripts[2])
PY
# The free-space check: the database size and the attachments in KB, one per line.
sizes=$(docker run --rm -i --network "$NET" "${ENV[@]}" -v "$VOL:/var/lib/odoo" --entrypoint '' "$IMAGE" \
    bash -c "$(<"$WORK/size.sh")" </dev/null 2>/dev/null) || fail 'the size check failed'
if ! [[ $sizes =~ ^[0-9]+$'\n'[0-9]+$ ]] || (( ${sizes%$'\n'*} <= 1000 || ${sizes#*$'\n'} == 0 )); then
    fail "the size check printed: $sizes"
fi
printf 'size check: database %s KB, attachments %s KB\n' "${sizes%$'\n'*}" "${sizes#*$'\n'}"
app "$IMAGE" bash -c "$(<"$WORK/dump.sh")" </dev/null > "$WORK/database.dump" 2>/dev/null || fail 'pg_dump failed'
app "$IMAGE" pg_restore --list < "$WORK/database.dump" > "$WORK/database.list" 2>/dev/null || fail 'pg_restore --list cannot read the dump from stdin'
grep -Eq ' TABLE DATA public ir_module_module( |$)' "$WORK/database.list" || fail 'the dump lists no ir_module_module data'
docker run --rm -i --network "$NET" "${ENV[@]}" -v "$VOL:/var/lib/odoo" --entrypoint '' "$IMAGE" \
    bash -c "$(<"$WORK/archive.sh")" </dev/null > "$WORK/filestore.tar.gz" 2>/dev/null || fail 'the attachments archive failed'
tar -tzf "$WORK/filestore.tar.gz" > "$WORK/archive.list" || fail 'the attachments archive cannot be read'
[[ $(grep -v '/$' "$WORK/archive.list" | sort | paste -sd ' ' -) == 'filestore/perodua/ab/abcdef filestore/perodua/cd/cdef01' ]] \
    || fail "the archive holds other files than the two attachments: $(paste -sd ' ' - < "$WORK/archive.list")"
printf 'backup: %s bytes of pg_dump -Fc, %s\n' "$(wc -c < "$WORK/database.dump" | tr -d ' ')" "$(paste -sd ' ' - < "$WORK/archive.list")"
# What restore.txt says about the DB server's tools.
if docker exec -i "$PG" pg_restore --list < "$WORK/database.dump" >/dev/null 2>"$WORK/pg16.err"; then
    fail 'pg_restore 16 read the archive; restore.txt says it cannot'
fi
printf 'pg_restore 16 refuses it: %s\n' "$(head -n 1 "$WORK/pg16.err")"

# Damage the data and the attachments, then follow restore.txt.
sql -c "DELETE FROM model_7 WHERE id > 10; DROP TABLE model_500; UPDATE res_users SET login = 'changed' WHERE id = 2;
        SELECT setval('ir_sequence_001', 99)" >/dev/null
docker run --rm -v "$VOL:/var/lib/odoo" --user 0 --entrypoint bash "$IMAGE" -ec \
    'rm /var/lib/odoo/filestore/perodua/ab/abcdef; printf stray > /var/lib/odoo/filestore/perodua/stray' 2>/dev/null
[[ $(snapshot) != "$before" && $(attachments) != "$files_before" ]] || fail 'the damage did not change anything'
(
    cd "$KIT/scripts"
    # restore_hint of deploy-app.sh, with the variables it reads.
    # shellcheck disable=SC2034
    DEPLOY_DIR=/opt/perodua-app PROJECT_NAME=perodua-client-uiux BACKUP_DIR=$WORK RELEASE=client-stable-uiux-v1.0.5 \
        OLD_RELEASE=client-stable-uiux-v1.0.4 DB_NAME=perodua DB_HOST=192.0.2.20 DB_PORT=5432
    eval "$(sed -n '/^restore_hint() {/,/^}/p; /^go_back_steps() {/,/^}/p' deploy-app.sh)"
    restore_hint
) > "$WORK/restore.txt"
python3 - "$WORK" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1], 'restore.txt').read_text()
pathlib.Path(sys.argv[1], 'restore-database.sh').write_text(re.search(r"-T odoo bash -c '([^']*)' <", text).group(1))
pathlib.Path(sys.argv[1], 'restore-files.sh').write_text(re.search(r"odoo -ec '([^']*)'", text).group(1))
PY
# Step 2: reset_database.py, as service.sh reset runs it.
docker run --rm -i --network "$NET" "${ENV[@]}" -e DB_PASSWORD_FILE=/run/secrets/db_password \
    -v "$WORK/db_password:/run/secrets/db_password:ro" --entrypoint '' "$IMAGE" python3 - \
    < "$KIT/scripts/reset_database.py" 2>/dev/null || fail 'reset_database.py failed'
[[ $(sql -c "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace") == 0 ]] \
    || fail 'reset_database.py left relations in public'
# Step 3 and 4.
app "$IMAGE" bash -c "$(<"$WORK/restore-database.sh")" < "$WORK/database.dump" 2>"$WORK/restore.err" \
    || fail "the database restore of restore.txt failed: $(grep -v '^perl: \|^\s\|^\tLANG\|are supported' "$WORK/restore.err" | head -n 5)"
docker run --rm -i "${ENV[@]}" -v "$VOL:/var/lib/odoo" -v "$WORK/filestore.tar.gz:/restore.tar.gz:ro" --user 0 \
    --entrypoint bash "$IMAGE" -ec "$(<"$WORK/restore-files.sh")" 2>/dev/null || fail 'the attachments restore of restore.txt failed'
after=$(snapshot)
[[ $after == "$before" ]] || fail "the restored database differs: before $before, after $after"
# The attachments as they were, and no session: every sign-in has ended.
files_after=$(attachments)
[[ $files_after == "$(grep -v '^sessions/' <<< "$files_before")" ]] || fail "the restored attachments differ: $files_after"
docker run --rm -v "$VOL:/var/lib/odoo" --entrypoint bash "$IMAGE" -c '! test -e /var/lib/odoo/sessions' 2>/dev/null \
    || fail 'the restore left the sessions of the Odoo session store'
grep -q 'Every user must sign in again' "$WORK/restore.txt" || fail 'restore.txt does not say that every user must sign in again'
printf 'restored: %s (relations, rows, logins, sequence, extensions, function, view, inherited rows)\n' "$after"
printf 'restored attachments, owner and content: %s; sessions removed (before: %s)\n' \
    "$(paste -sd ';' - <<< "$files_after")" "$(grep '^sessions/' <<< "$files_before" | paste -sd ';' -)"
printf 'OK: the --upgrade backup restores to the data it was taken from\n'
