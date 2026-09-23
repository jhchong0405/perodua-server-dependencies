#!/usr/bin/env bash
# Destructive only inside a fresh, disposable test container; never run on a DB host.
set -Eeuo pipefail

if [[ ${DEPLOY_DB_TEST_ISOLATED:-} != 1 || ! -f /.dockerenv || $EUID != 0 ]]; then
    printf '%s\n' 'Run this harness only in its disposable Docker test image.' >&2
    exit 2
fi

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY_SCRIPT="$SOURCE_DIR/deploy-db.sh"
TEST_DIR=$(mktemp -d /tmp/deploy-db-integration.XXXXXX)
chmod 755 "$TEST_DIR"
PASS_COUNT=0

printf 'UTC: %s\n' "$(date -u +%FT%TZ)"
printf 'Environment: Ubuntu 24.04; %s; systemd is not PID 1\n' "$(psql --version)"
printf 'Script SHA-256: %s\n' "$(sha256sum "$DEPLOY_SCRIPT" | awk '{print $1}')"
printf 'Command: bash /src/tests/run-deploy-db-integration.sh (isolated Docker, read-only source, network none)\n\n'

die() { printf 'FAIL: %s\nLogs: %s\n' "$*" "$TEST_DIR" >&2; exit 1; }
pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf 'PASS %02d: %s\n' "$PASS_COUNT" "$*"; }
admin_sql() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -At "$@"; }
assert_sql() {
    local database=$1 query=$2 expected=$3 actual
    actual=$(admin_sql -d "$database" -c "$query")
    [[ $actual == "$expected" ]] || die "SQL assertion on $database: expected [$expected], received [$actual]"
}
hash_file() { sha256sum "$1" | awk '{print $1}'; }
write_conf() {
    local name=$1 user=$2 backup=${3:-"$TEST_DIR/fixture.dump"} digest=${4:-}
    [[ -n $digest ]] || digest=$(hash_file "$backup")
    cat > "$TEST_DIR/$name.conf" <<EOF
DB_NAME=$name
DB_USER=$user
PG_CLUSTER=main
PG_PORT=5432
BACKUP_FILE=$backup
BACKUP_SHA256=$digest
DB_LISTEN_IP=127.0.0.1
APP_CIDR=
MIN_FREE_MB=1
EXPECTED_TABLES=public.ir_module_module,public.res_users
EOF
    chmod 600 "$TEST_DIR/$name.conf"
}
write_empty_conf() {
    local name=$1 user=$2
    cat > "$TEST_DIR/$name.conf" <<EOF
DB_MODE=empty
DB_NAME=$name
DB_USER=$user
PG_CLUSTER=main
PG_PORT=5432
BACKUP_FILE=
BACKUP_URL=
BACKUP_SHA256=
DOWNLOAD_USER=
DB_LISTEN_IP=127.0.0.1
APP_CIDR=
MIN_FREE_MB=1
EOF
    chmod 600 "$TEST_DIR/$name.conf"
}
deploy_ok() {
    local name=$1 password_file=${2:-"$TEST_DIR/app.password"}
    if ! bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/$name.conf" --password-file "$password_file" > "$TEST_DIR/$name.log" 2>&1; then
        cat "$TEST_DIR/$name.log" >&2
        die "deployment should succeed: $name"
    fi
}
deploy_fails() {
    local name=$1 password_file=${2:-"$TEST_DIR/app.password"}
    if bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/$name.conf" --password-file "$password_file" > "$TEST_DIR/$name.log" 2>&1; then
        die "deployment should refuse: $name"
    fi
}
assert_absent() {
    assert_sql postgres "SELECT count(*) FROM pg_database WHERE datname = '$1'" 0
}

bash -n "$DEPLOY_SCRIPT"
shellcheck "$DEPLOY_SCRIPT"
pass 'bash syntax and ShellCheck'

pg_ctlcluster 16 main start
[[ $(admin_sql -d postgres -c 'SHOW server_version_num') == 16* ]] || die 'PostgreSQL 16 required'
assert_sql postgres "SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')" 0

python3 - "$TEST_DIR" <<'PY'
import pathlib
import sys
directory = pathlib.Path(sys.argv[1])
for name, value in {
    'app.password': 'Fixture:pass\'"\\with $punctuation',
    'wrong.password': 'different-password',
}.items():
    path = directory / name
    path.write_text(value + '\n')
    path.chmod(0o600)
PY

admin_sql -d postgres <<'SQL'
CREATE ROLE source_owner LOGIN;
CREATE DATABASE source_fixture OWNER source_owner TEMPLATE template0;
SQL
admin_sql -d source_fixture <<'SQL'
SET ROLE source_owner;
CREATE EXTENSION unaccent;
CREATE EXTENSION pg_trgm;
CREATE TABLE public.ir_module_module (id serial PRIMARY KEY, name text NOT NULL, state text NOT NULL);
INSERT INTO public.ir_module_module (name, state) VALUES ('base', 'installed'), ('perodua_fixture', 'installed');
CREATE TABLE public.res_users (id serial PRIMARY KEY, login text UNIQUE NOT NULL, active boolean NOT NULL DEFAULT true);
INSERT INTO public.res_users (login) VALUES ('fixture-admin'), ('fixture-operator');
CREATE TABLE public.business_records (id serial PRIMARY KEY, reference text NOT NULL, amount numeric(16,2) NOT NULL);
INSERT INTO public.business_records (reference, amount) VALUES ('ORDER-001', 123.45), ('ORDER-002', 500.00);
CREATE VIEW public.active_fixture_users AS SELECT login FROM public.res_users WHERE active;
CREATE FUNCTION public.fixture_total() RETURNS numeric LANGUAGE SQL AS 'SELECT sum(amount) FROM public.business_records';
SQL
runuser -u postgres -- pg_dump -Fc -d source_fixture > "$TEST_DIR/fixture.dump"

write_conf config_only app_check
before_files=$(find /var/lib -maxdepth 1 -name perodua-db-deploy | wc -l)
bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/config_only.conf" --check-config > "$TEST_DIR/check-config.log" 2>&1
assert_absent config_only
[[ $(find /var/lib -maxdepth 1 -name perodua-db-deploy | wc -l) == "$before_files" ]] || die '--check-config wrote deployment state'
pass 'check-config validates without creating a database or deployment state'

pg_ctlcluster 16 main stop
write_conf restored app_restored
deploy_ok restored
assert_sql restored 'SELECT reference || chr(58) || amount FROM public.business_records ORDER BY id' $'ORDER-001:123.45\nORDER-002:500.00'
assert_sql restored 'SELECT fixture_total()' 623.45
assert_sql restored 'SELECT count(*) FROM public.active_fixture_users' 2
assert_sql restored "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('unaccent', 'pg_trgm')" 'pg_trgm,unaccent'
assert_sql postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='restored'" app_restored
assert_sql postgres "SELECT datcollate || ',' || datctype FROM pg_database WHERE datname='restored'" 'C,C.UTF-8'
assert_sql postgres "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname='app_restored'" f
assert_sql restored "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','S','v') AND pg_get_userbyid(c.relowner) <> 'app_restored'" 0
assert_sql restored "SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname='fixture_total'" app_restored
PGPASSWORD=$(cat "$TEST_DIR/app.password") psql -X -h 127.0.0.1 -U app_restored -d restored -v ON_ERROR_STOP=1 -At -c "INSERT INTO public.business_records (reference,amount) VALUES ('AFTER-DEPLOY', 77.00)" > "$TEST_DIR/app-login.log"
pass 'stopped service starts; real dump restores extensions, schema, data, ownership, and least-privilege app login'

restored_oid=$(admin_sql -d postgres -c "SELECT oid FROM pg_database WHERE datname='restored'")
deploy_ok restored
assert_sql restored "SELECT count(*) FROM public.business_records WHERE reference='AFTER-DEPLOY'" 1
assert_sql postgres "SELECT oid FROM pg_database WHERE datname='restored'" "$restored_oid"
pass 'same-backup rerun preserves database identity and post-deployment writes'

mv /var/lib/perodua-db-deploy/16-main/restored/success /var/lib/perodua-db-deploy/16-main/restored/pending
deploy_ok restored
[[ -f /var/lib/perodua-db-deploy/16-main/restored/success ]] || die 'interrupted publication did not recreate success marker'
assert_sql restored "SELECT count(*) FROM public.business_records WHERE reference='AFTER-DEPLOY'" 1
assert_sql postgres "SELECT oid FROM pg_database WHERE datname='restored'" "$restored_oid"
pass 'interrupted publication recovers pending identity marker without reimporting or changing data'

deploy_fails restored "$TEST_DIR/wrong.password"
assert_sql restored "SELECT count(*) FROM public.business_records WHERE reference='AFTER-DEPLOY'" 1
pass 'rerun rejects wrong application password without changing data'

cp "$TEST_DIR/restored.conf" "$TEST_DIR/locale_mismatch.conf"
printf '\nDB_LC_COLLATE=C.UTF-8\nDB_LC_CTYPE=C.UTF-8\n' >> "$TEST_DIR/locale_mismatch.conf"
deploy_fails locale_mismatch
assert_sql restored "SELECT count(*) FROM public.business_records WHERE reference='AFTER-DEPLOY'" 1
assert_sql postgres "SELECT datcollate || ',' || datctype FROM pg_database WHERE datname='restored'" 'C,C.UTF-8'
pass 'rerun with mismatched locale configuration refused without changing database'

admin_sql -d postgres -c 'CREATE DATABASE unrelated'
admin_sql -d unrelated -c "CREATE TABLE keep_me(value text); INSERT INTO keep_me VALUES ('untouched')"
write_conf unrelated app_unrelated
deploy_fails unrelated
assert_sql unrelated 'SELECT value FROM keep_me' untouched
pass 'existing unrelated database refused and retained unchanged'

write_conf bad_checksum app_bad_checksum "$TEST_DIR/fixture.dump" "$(printf '0%.0s' {1..64})"
deploy_fails bad_checksum
assert_absent bad_checksum
pass 'incorrect SHA-256 rejected without target database'

printf '%s\n' 'not a PostgreSQL archive' > "$TEST_DIR/malformed.dump"
write_conf malformed app_malformed "$TEST_DIR/malformed.dump"
deploy_fails malformed
assert_absent malformed
pass 'malformed archive rejected without target database'

python3 - "$TEST_DIR" <<'PY'
import pathlib
import sys
import zlib
directory = pathlib.Path(sys.argv[1])
archive = bytearray((directory / 'fixture.dump').read_bytes())
for offset in range(len(archive) - 2):
    if archive[offset:offset + 2] not in (b'\x78\x9c', b'\x78\xda', b'\x78\x01'):
        continue
    inflater = zlib.decompressobj()
    try:
        inflater.decompress(archive[offset:])
    except zlib.error:
        continue
    if inflater.eof:
        # Damage the Adler checksum of a real payload, leaving the TOC valid.
        stream_end = len(archive) - len(inflater.unused_data)
        archive[stream_end - 1] ^= 0xff
        break
else:
    raise SystemExit('fixture did not contain a compressed payload')
(directory / 'corrupted.dump').write_bytes(archive)
PY
pg_restore --list "$TEST_DIR/corrupted.dump" > "$TEST_DIR/corrupted-list.log"
write_conf interrupted_restore app_interrupted "$TEST_DIR/corrupted.dump"
deploy_fails interrupted_restore
assert_absent interrupted_restore
assert_sql postgres "SELECT count(*) FROM pg_database WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname='app_interrupted')" 1
write_conf interrupted_restore app_interrupted
deploy_ok interrupted_restore
assert_sql interrupted_restore 'SELECT count(*) FROM public.business_records' 2
pass 'corrupted payload restore failure retains staging for inspection, never publishes target, and retry succeeds'

admin_sql -d postgres -c 'CREATE DATABASE incomplete_fixture OWNER source_owner TEMPLATE template0'
admin_sql -d incomplete_fixture -c 'SET ROLE source_owner; CREATE TABLE restored_probe(value text); INSERT INTO restored_probe VALUES ('"'partial fixture'"')'
runuser -u postgres -- pg_dump -Fc -d incomplete_fixture > "$TEST_DIR/incomplete.dump"
write_conf retry_restore app_retry "$TEST_DIR/incomplete.dump"
deploy_fails retry_restore
assert_absent retry_restore
write_conf retry_restore app_retry
deploy_ok retry_restore
assert_sql retry_restore 'SELECT count(*) FROM public.res_users' 2
pass 'failed required-table verification never publishes target and a corrected-backup retry succeeds'

python3 - "$TEST_DIR" <<'PY'
import pathlib
import sys
directory = pathlib.Path(sys.argv[1])
base = (directory / 'config_only.conf').read_text()
for name, payload in {
    'dollar_injection': 'DB_NAME=$(touch /tmp/deploy-db-test-injected)',
    'backtick_injection': 'DB_NAME=`touch /tmp/deploy-db-test-injected`',
    'extra_command': 'touch /tmp/deploy-db-test-injected',
    'unknown_key': 'SHELLOPTS=xtrace',
    'mode_injection': 'DB_MODE=$(touch /tmp/deploy-db-test-injected)',
    'unknown_mode': 'DB_MODE=snapshot',
}.items():
    path = directory / (name + '.conf')
    path.write_text(base + '\n' + payload + '\n')
    path.chmod(0o600)
PY
for name in dollar_injection backtick_injection extra_command unknown_key mode_injection unknown_mode; do
    if bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/$name.conf" --check-config > "$TEST_DIR/$name.log" 2>&1; then
        die "unsafe config accepted: $name"
    fi
done
[[ ! -e /tmp/deploy-db-test-injected ]] || die 'configuration executed code'
pass 'config rejects shell substitution, commands, and unknown keys without executing them'

write_conf weak_secret app_weak_secret
cp "$TEST_DIR/app.password" "$TEST_DIR/readable.password"
chmod 644 "$TEST_DIR/readable.password"
deploy_fails weak_secret "$TEST_DIR/readable.password"
assert_absent weak_secret
pass 'world-readable password file rejected'

write_conf unavailable_address app_unavailable
sed -i 's/^DB_LISTEN_IP=.*/DB_LISTEN_IP=192.0.2.53/' "$TEST_DIR/unavailable_address.conf"
deploy_fails unavailable_address
assert_absent unavailable_address
pass 'unassigned listen address refused before target publication'

write_conf all user
deploy_ok all
assert_sql postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='all'" user
PGPASSWORD=$(cat "$TEST_DIR/app.password") psql -X -h 127.0.0.1 -U user -d all -v ON_ERROR_STOP=1 -At -c 'SELECT current_user' > "$TEST_DIR/literal-role-login.log"
[[ $(cat "$TEST_DIR/literal-role-login.log") == user ]] || die 'reserved-word application role cannot authenticate'
admin_sql -d postgres -c 'GRANT CONNECT ON DATABASE "all" TO app_restored'
if PGPASSWORD=$(cat "$TEST_DIR/app.password") psql -X -h 127.0.0.1 -U app_restored -d all -v ON_ERROR_STOP=1 -At -c 'SELECT 1' > "$TEST_DIR/denied-other-role.log" 2>&1; then
    die 'target HBA permitted a different login role'
fi
PGPASSWORD=$(cat "$TEST_DIR/app.password") psql -X -h 127.0.0.1 -U app_restored -d source_fixture -v ON_ERROR_STOP=1 -At -c 'SELECT 1' > "$TEST_DIR/unrelated-login.log"
[[ $(cat "$TEST_DIR/unrelated-login.log") == 1 ]] || die 'literal all database name restricted an unrelated database'
pass 'SQL/HBA keyword names are literal; target rejects other role and unrelated DB login still works'

admin_sql -d postgres -c 'CREATE ROLE app_privileged LOGIN SUPERUSER'
write_conf privileged_role app_privileged
deploy_fails privileged_role
assert_absent privileged_role
assert_sql postgres "SELECT rolsuper FROM pg_roles WHERE rolname='app_privileged'" t
admin_sql -d postgres -c 'CREATE ROLE membership_parent; CREATE ROLE app_membership LOGIN; GRANT membership_parent TO app_membership'
write_conf membership_role app_membership
deploy_fails membership_role
assert_absent membership_role
assert_sql postgres "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='app_membership')" 1
pass 'existing privileged roles and role memberships rejected without modifying their privileges'

write_conf added_listener app_added_listener
sed -i 's/^DB_LISTEN_IP=.*/DB_LISTEN_IP=127.0.0.2/' "$TEST_DIR/added_listener.conf"
deploy_ok added_listener
pg_isready -h 127.0.0.2 -p 5432 > "$TEST_DIR/added-listener.log"
assert_sql added_listener 'SELECT count(*) FROM public.res_users' 2
pass 'adding a real listen address parses startup settings and restarts PostgreSQL successfully'

current_listener=$(admin_sql -d postgres -c 'SHOW listen_addresses')
admin_sql -d postgres -c "ALTER SYSTEM SET listen_addresses = '$current_listener'"
config_before=$(hash_file /etc/postgresql/16/main/postgresql.conf)
hba_before=$(hash_file /etc/postgresql/16/main/pg_hba.conf)
write_conf overridden_listener app_overridden
sed -i 's/^DB_LISTEN_IP=.*/DB_LISTEN_IP=127.0.0.3/' "$TEST_DIR/overridden_listener.conf"
deploy_fails overridden_listener
assert_absent overridden_listener
[[ $(hash_file /etc/postgresql/16/main/postgresql.conf) == "$config_before" ]] || die 'failed network setup did not restore postgresql.conf'
[[ $(hash_file /etc/postgresql/16/main/pg_hba.conf) == "$hba_before" ]] || die 'failed network setup did not restore pg_hba.conf'
admin_sql -d postgres -c 'ALTER SYSTEM RESET listen_addresses'
pass 'auto.conf listener override refused with original PostgreSQL and HBA configuration restored'

# ── DB_MODE=empty ────────────────────────────────────────────────────────────
STATE_ROOT=/var/lib/perodua-db-deploy/16-main
write_empty_conf empty_check app_empty_check
bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/empty_check.conf" --check-config > "$TEST_DIR/empty-check-config.log" 2>&1 || die 'empty mode without a backup should validate'
printf 'EXPECTED_TABLES=\n' >> "$TEST_DIR/empty_check.conf"
bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/empty_check.conf" --check-config >> "$TEST_DIR/empty-check-config.log" 2>&1 || die 'empty mode must not require EXPECTED_TABLES'
assert_absent empty_check
for leftover in BACKUP_FILE=database.dump BACKUP_URL=https://backup.invalid/database.dump "BACKUP_SHA256=$(hash_file "$TEST_DIR/fixture.dump")" DOWNLOAD_USER=cloud; do
    write_empty_conf empty_leftover app_empty_leftover
    sed -i "s|^${leftover%%=*}=.*|$leftover|" "$TEST_DIR/empty_leftover.conf"
    if bash "$DEPLOY_SCRIPT" --config "$TEST_DIR/empty_leftover.conf" --check-config > "$TEST_DIR/empty-leftover.log" 2>&1; then
        die "empty mode accepted a backup setting: ${leftover%%=*}"
    fi
    grep -q 'DB_MODE=empty restores no backup' "$TEST_DIR/empty-leftover.log" || die "empty mode refused ${leftover%%=*} for the wrong reason"
done
assert_absent empty_leftover
pass 'empty mode validates without backup, checksum or EXPECTED_TABLES; leftover backup settings are refused'

write_empty_conf uat app_uat
deploy_ok uat
uat_oid=$(admin_sql -d postgres -c "SELECT oid FROM pg_database WHERE datname='uat'")
assert_sql postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='uat'" app_uat
assert_sql postgres "SELECT pg_encoding_to_char(encoding) || ',' || datcollate || ',' || datctype FROM pg_database WHERE datname='uat'" 'UTF8,C,C.UTF-8'
assert_sql postgres "SELECT concat_ws(',', rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls) FROM pg_roles WHERE rolname='app_uat'" 't,f,f,f,f,f'
assert_sql postgres "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='app_uat')" 0
assert_sql postgres "SELECT has_database_privilege('public', 'uat', 'CONNECT')" f
assert_sql uat "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'" 0
assert_sql postgres "SELECT count(*) FROM pg_database WHERE datname LIKE 'uat\_\_%'" 0
PGPASSWORD=$(cat "$TEST_DIR/app.password") psql -X -h 127.0.0.1 -U app_uat -d uat -v ON_ERROR_STOP=1 -At -c 'SELECT current_user' > "$TEST_DIR/uat-login.log"
[[ $(cat "$TEST_DIR/uat-login.log") == app_uat ]] || die 'empty-mode application TCP/SCRAM login failed'
[[ $(cat "$STATE_ROOT/uat/success") == "empty|$uat_oid|app_uat" ]] || die 'empty-mode identity marker missing or wrong'
if grep -q 'Restoring into staging' "$TEST_DIR/uat.log"; then die 'empty mode ran a restore'; fi
pass 'empty mode creates a UTF8 database with the configured locales, owned by a least-privileged login role, closed to PUBLIC, without a backup'

# Simulate the App server initializing Odoo, then rerun the DB deployment.
PGPASSWORD=$(cat "$TEST_DIR/app.password") psql -X -q -h 127.0.0.1 -U app_uat -d uat -v ON_ERROR_STOP=1 -c "
CREATE TABLE public.ir_module_module (id serial PRIMARY KEY, name text NOT NULL, state text NOT NULL);
INSERT INTO public.ir_module_module (name, state) VALUES ('base', 'installed'), ('perodua_client_stable', 'installed');
CREATE TABLE public.uat_probe (value text NOT NULL);
INSERT INTO public.uat_probe VALUES ('written by the App after the first run');"
deploy_ok uat
assert_sql postgres "SELECT oid FROM pg_database WHERE datname='uat'" "$uat_oid"
assert_sql uat 'SELECT value FROM public.uat_probe' 'written by the App after the first run'
assert_sql uat 'SELECT count(*) FROM public.ir_module_module' 2
grep -q 'Matching empty-mode database already deployed' "$TEST_DIR/uat.log" || die 'empty-mode rerun did not recognize its own deployment'
pass 'empty-mode rerun after App initialization keeps the database OID, tables and data'

mv "$STATE_ROOT/uat/success" "$STATE_ROOT/uat/pending"
deploy_ok uat
[[ -f $STATE_ROOT/uat/success ]] || die 'empty-mode interrupted publication did not recreate the success marker'
assert_sql postgres "SELECT oid FROM pg_database WHERE datname='uat'" "$uat_oid"
assert_sql uat 'SELECT count(*) FROM public.uat_probe' 1
pass 'empty mode recovers an interrupted publication from its pending marker without recreating the database'

deploy_fails uat "$TEST_DIR/wrong.password"
cp "$TEST_DIR/uat.conf" "$TEST_DIR/uat_locale.conf"
printf 'DB_LC_COLLATE=C.UTF-8\nDB_LC_CTYPE=C.UTF-8\n' >> "$TEST_DIR/uat_locale.conf"
deploy_fails uat_locale
assert_sql postgres "SELECT oid || ',' || datcollate FROM pg_database WHERE datname='uat'" "$uat_oid,C"
assert_sql uat 'SELECT count(*) FROM public.uat_probe' 1
pass 'empty-mode reruns with a wrong password or different locales are refused without changing the database'

write_empty_conf owner_changed app_owner_changed
deploy_ok owner_changed
admin_sql -d postgres -c 'CREATE ROLE intruder' -c 'ALTER DATABASE owner_changed OWNER TO intruder'
deploy_fails owner_changed
assert_sql postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='owner_changed'" intruder
pass 'empty-mode rerun refuses a database whose owner changed, leaving the owner untouched'

admin_sql -d postgres -c 'CREATE DATABASE empty_unrelated'
admin_sql -d empty_unrelated -c "CREATE TABLE keep_me(value text); INSERT INTO keep_me VALUES ('untouched')"
write_empty_conf empty_unrelated app_empty_unrelated
deploy_fails empty_unrelated
assert_sql empty_unrelated 'SELECT value FROM keep_me' untouched
admin_sql -d postgres -c 'CREATE ROLE app_manual LOGIN' -c "CREATE DATABASE empty_manual OWNER app_manual TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C.UTF-8'"
manual_oid=$(admin_sql -d postgres -c "SELECT oid FROM pg_database WHERE datname='empty_manual'")
write_empty_conf empty_manual app_manual
deploy_fails empty_manual
assert_sql postgres "SELECT oid FROM pg_database WHERE datname='empty_manual'" "$manual_oid"
pass 'empty mode never takes over a same-name database it did not create, even with matching owner and locales'

write_empty_conf restored app_restored
deploy_fails restored
assert_sql restored "SELECT count(*) FROM public.business_records WHERE reference='AFTER-DEPLOY'" 1
cp "$TEST_DIR/uat.conf" "$TEST_DIR/uat_as_restore.conf"
python3 - "$TEST_DIR/uat_as_restore.conf" "$(hash_file "$TEST_DIR/fixture.dump")" "$TEST_DIR/fixture.dump" <<'PY'
import pathlib
import sys
path, digest, archive = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text().replace('DB_MODE=empty', 'DB_MODE=restore')
text = text.replace('BACKUP_FILE=\n', f'BACKUP_FILE={archive}\n').replace('BACKUP_SHA256=\n', f'BACKUP_SHA256={digest}\n')
path.write_text(text)
PY
deploy_fails uat_as_restore
assert_sql postgres "SELECT oid FROM pg_database WHERE datname='uat'" "$uat_oid"
assert_sql uat 'SELECT count(*) FROM public.uat_probe' 1
pass 'restore and empty deployment identities never match: cross-mode reruns are refused without changes'

admin_sql -d postgres -c 'CREATE ROLE app_empty_createdb LOGIN CREATEDB'
write_empty_conf empty_createdb app_empty_createdb
deploy_fails empty_createdb
assert_absent empty_createdb
assert_sql postgres "SELECT rolcreatedb FROM pg_roles WHERE rolname='app_empty_createdb'" t
pass 'empty mode refuses an existing CREATEDB role instead of using or altering it'

admin_sql -d postgres -c "CREATE ROLE app_empty_retry LOGIN PASSWORD 'different-password'"
write_empty_conf empty_retry app_empty_retry
deploy_fails empty_retry
assert_absent empty_retry
assert_sql postgres "SELECT count(*) FROM pg_database WHERE datname LIKE 'empty\_retry\_\_empty\_%'" 1
deploy_ok empty_retry "$TEST_DIR/wrong.password"
assert_sql postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='empty_retry'" app_empty_retry
pass 'failed empty-mode verification never publishes the target, keeps staging for inspection, and a retry succeeds'

write_empty_conf preflight_fixture app_preflight
deploy_ok preflight_fixture
python3 "$SOURCE_DIR/tests/check_app_preflight.py" "$SOURCE_DIR" "$TEST_DIR" preflight_fixture app_preflight "$TEST_DIR/app.password"
pass "deploy-app.sh preflight on a DB_MODE=empty database: EMPTY, SETUP_UNMARKED, persisted SETUP_PENDING marker, post-init assertions, READY stamp, restored-database test unchanged"

python3 "$SOURCE_DIR/tests/check-https-download.py" "$SOURCE_DIR" "$TEST_DIR"
assert_sql https_restored 'SELECT count(*) FROM public.res_users' 2
assert_absent https_refused
pass 'real HTTPS Basic-auth download prompts securely; wrong cloud password prevents restore'

write_empty_conf password_prompt app_password_prompt
python3 "$SOURCE_DIR/tests/check-password-prompt.py" "$SOURCE_DIR" "$TEST_DIR/password_prompt.conf"
assert_sql password_prompt 'SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()' app_password_prompt
pass 'interactive password entry asks again after a short password or two different entries, never echoes, then deploys'

python3 - "$TEST_DIR" <<'PY'
import pathlib
import sys
directory = pathlib.Path(sys.argv[1])
secret = (directory / 'app.password').read_text().strip()
logs = list(directory.glob('*.log')) + list(pathlib.Path('/var/lib/perodua-db-deploy').rglob('*.log'))
for log in logs:
    if secret in log.read_text(errors='replace'):
        raise SystemExit(f'password leaked into {log.name}')
PY
pass 'deployment output does not contain the application password'

printf '\nALL %s INTEGRATION CHECKS PASSED\nLogs: %s\n' "$PASS_COUNT" "$TEST_DIR"
