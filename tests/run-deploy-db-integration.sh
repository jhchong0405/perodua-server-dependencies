#!/usr/bin/env bash
# Destructive only inside a fresh, disposable test container; never run on a DB host.
set -Eeuo pipefail

if [[ ${DEPLOY_DB_TEST_ISOLATED:-} != 1 || ! -f /.dockerenv || $EUID != 0 ]]; then
    printf '%s\n' 'Run this harness only in its disposable Docker test image.' >&2
    exit 2
fi

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY_SCRIPT="$SOURCE_DIR/scripts/deploy-db.sh"
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
python3 "$SOURCE_DIR/tests/check_app_preflight.py" "$SOURCE_DIR/scripts" "$TEST_DIR" preflight_fixture app_preflight "$TEST_DIR/app.password"
pass "deploy-app.sh preflight on a DB_MODE=empty database: EMPTY, SETUP_UNMARKED, persisted SETUP_PENDING marker, post-init assertions, READY stamp, restored-database test unchanged"

python3 "$SOURCE_DIR/tests/check-https-download.py" "$SOURCE_DIR/scripts" "$TEST_DIR"
assert_sql https_restored 'SELECT count(*) FROM public.res_users' 2
assert_absent https_refused
pass 'real HTTPS Basic-auth download prompts securely; wrong cloud password prevents restore'

write_empty_conf password_prompt app_password_prompt
python3 "$SOURCE_DIR/tests/check-password-prompt.py" "$SOURCE_DIR/scripts" "$TEST_DIR/password_prompt.conf"
assert_sql password_prompt 'SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()' app_password_prompt
pass 'interactive password entry asks again after a short password or two different entries, never echoes, then deploys'

# ── uninstall.sh --role db ─────────────────────────────────────────────────────
UNINSTALL="$SOURCE_DIR/scripts/uninstall.sh"
HBA_FILE=$(admin_sql -d postgres -c 'SHOW hba_file')
uninstall_ok() {
    local log=$1; shift
    bash "$UNINSTALL" --role db "$@" > "$TEST_DIR/$log.log" 2>&1 || { cat "$TEST_DIR/$log.log" >&2; die "uninstall should succeed: $log"; }
}
uninstall_fails() {
    local log=$1; shift
    if bash "$UNINSTALL" --role db "$@" > "$TEST_DIR/$log.log" 2>&1; then die "uninstall should have refused: $log"; fi
}
write_empty_conf uninst_a app_uninst_a
sed -i 's/^DB_LISTEN_IP=.*/DB_LISTEN_IP=127.0.0.4/' "$TEST_DIR/uninst_a.conf"
deploy_ok uninst_a
uninstall_fails uninst-wrong --config "$TEST_DIR/uninst_a.conf" --confirm uninst_b
runuser -u postgres -- psql -X -q -d uninst_a -c 'SELECT pg_sleep(60)' > /dev/null 2>&1 &
sleeper=$!
for _ in $(seq 1 100); do
    [[ $(admin_sql -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'uninst_a'") == 0 ]] || break
    sleep 0.1
done
uninstall_fails uninst-busy --config "$TEST_DIR/uninst_a.conf" --confirm uninst_a
kill "$sleeper" 2>/dev/null || true
wait "$sleeper" 2>/dev/null || true
grep -q 'connection(s) are open' "$TEST_DIR/uninst-busy.log" || die 'open connections were not reported'
assert_sql postgres "SELECT count(*) FROM pg_database WHERE datname = 'uninst_a'" 1
until [[ $(admin_sql -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'uninst_a'") == 0 ]]; do sleep 0.1; done
uninstall_ok uninst-a --config "$TEST_DIR/uninst_a.conf" --confirm uninst_a
assert_absent uninst_a
assert_sql postgres "SELECT count(*) FROM pg_roles WHERE rolname = 'app_uninst_a'" 0
if grep -q 'perodua-db-deploy uninst_a' "$HBA_FILE"; then die 'uninstall left the access rules'; fi
assert_sql postgres 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL' 0
[[ ",$(admin_sql -d postgres -c 'SHOW listen_addresses')," != *,127.0.0.4,* ]] || die 'uninstall left the listen address'
pg_isready -h 127.0.0.2 -p 5432 > /dev/null || die 'another deployment lost its listen address'
[[ ! -e /var/lib/perodua-db-deploy/16-main/uninst_a ]] || die 'uninstall left the deployment records'
[[ -f $TEST_DIR/uninst_a.conf ]] || die 'uninstall deleted a hand-written configuration'
assert_sql password_prompt 'SELECT current_database()' password_prompt
grep -q 'perodua-db-deploy password_prompt' "$HBA_FILE" || die 'uninstall touched another deployment'
printf '%s' 'Uninstall Fixture #2026' > "$TEST_DIR/new.password"
chmod 600 "$TEST_DIR/new.password"
deploy_ok uninst_a "$TEST_DIR/new.password"
pass 'uninstall removes a deployment (database, role and its password, access rules, listen address, records) only after an exact confirmation and with no open connection; a fresh deploy with a new password then succeeds'

write_empty_conf uninst_b app_uninst_a
sed -i 's/^DB_LISTEN_IP=.*/DB_LISTEN_IP=127.0.0.2/' "$TEST_DIR/uninst_b.conf"
sed -i '1i # Created by deploy-db.sh from your answers. All settings: deploy.conf.example.' "$TEST_DIR/uninst_b.conf"
deploy_ok uninst_b "$TEST_DIR/new.password"
uninstall_ok uninst-b --config "$TEST_DIR/uninst_b.conf" --confirm uninst_b
assert_absent uninst_b
assert_sql uninst_a 'SELECT current_user' postgres
assert_sql postgres "SELECT count(*) FROM pg_roles WHERE rolname = 'app_uninst_a'" 1
grep -q 'keep login role app_uninst_a' "$TEST_DIR/uninst-b.log" || die 'the shared role was not reported as kept'
grep -q 'keep listening on 127.0.0.2' "$TEST_DIR/uninst-b.log" || die 'the shared listen address was not reported as kept'
[[ ",$(admin_sql -d postgres -c 'SHOW listen_addresses')," == *,127.0.0.2,* ]] || die 'a listen address another deployment uses was removed'
[[ ! -e $TEST_DIR/uninst_b.conf ]] || die 'the deploy.conf written by the guided setup was not deleted'
pass 'uninstall keeps the login role and listen address another deployment uses, and deletes a guided deploy.conf'

admin_sql -d postgres -c 'CREATE DATABASE uninst_manual'
uninstall_fails uninst-manual --database uninst_manual --confirm uninst_manual
admin_sql -d postgres -c 'DROP DATABASE uninst_a'
admin_sql -d postgres -c 'CREATE DATABASE uninst_a OWNER app_uninst_a'
uninstall_fails uninst-replaced --config "$TEST_DIR/uninst_a.conf" --confirm uninst_a
for log in uninst-manual uninst-replaced; do
    grep -q 'was not created by deploy-db.sh on this server, or was replaced since' "$TEST_DIR/$log.log" || die "$log refused for the wrong reason"
done
assert_sql postgres "SELECT count(*) FROM pg_database WHERE datname IN ('uninst_manual', 'uninst_a')" 2
pass 'uninstall refuses a database it did not create, or one replaced since, and changes nothing'

# ── service.sh ────────────────────────────────────────────────────────────────
SERVICE="$SOURCE_DIR/scripts/service.sh"
shellcheck "$SERVICE"
write_empty_conf reset_fixture app_reset
deploy_ok reset_fixture
python3 "$SOURCE_DIR/tests/check_reset_database.py" "$SOURCE_DIR/scripts" "$TEST_DIR" reset_fixture app_reset "$TEST_DIR/app.password" \
    > "$TEST_DIR/reset-database.log" 2>&1 || { cat "$TEST_DIR/reset-database.log" >&2; die 'reset_database.py check failed'; }
pass 'reset (reset_database.py) empties an Odoo-shaped database that a one-shot drop cannot, changes nothing on a failed login, keeps the database, its owner and access, and leaves it EMPTY for deploy-app.sh --init-db'

service_ok() {
    local log=$1; shift
    bash "$SERVICE" --role db "$@" > "$TEST_DIR/$log.log" 2>&1 || { cat "$TEST_DIR/$log.log" >&2; die "service.sh $* failed"; }
}
databases=$(admin_sql -d postgres -c 'SELECT string_agg(datname, $$,$$ ORDER BY datname) FROM pg_database')
service_ok service-status status
grep -q '^16  *main  *5432  *online ' "$TEST_DIR/service-status.log" || die 'status does not show the cluster online'
runuser -u postgres -- psql -X -At -d postgres -c 'SELECT pg_sleep(60)' > "$TEST_DIR/service-client.log" 2>&1 &
client=$!
for _ in $(seq 50); do
    [[ $(admin_sql -d postgres -c "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep(60)%'") == 1 ]] && break
    sleep 0.1
done
service_ok service-stop stop
grep -q 'Closing 1 open connection(s)' "$TEST_DIR/service-stop.log" || die 'stop did not report the open connection'
if pg_ctlcluster 16 main status > /dev/null 2>&1; then die 'stop left PostgreSQL running'; fi
if wait "$client"; then die 'the open connection survived the stop'; fi
service_ok service-status-down status
grep -q '^16  *main  *5432  *down ' "$TEST_DIR/service-status-down.log" || die 'status does not show the cluster down'
service_ok service-stop-again stop
grep -q 'already stopped' "$TEST_DIR/service-stop-again.log" || die 'a second stop did not say the cluster is already stopped'
service_ok service-start start
service_ok service-restart restart
[[ $(admin_sql -d postgres -c 'SELECT string_agg(datname, $$,$$ ORDER BY datname) FROM pg_database') == "$databases" ]] \
    || die 'the databases changed across stop, start and restart'
pass 'service.sh --role db: status, stop (closing and reporting an open connection), a repeated stop, start and restart; the databases stay'

# ── one server: PostgreSQL on Docker's address starts after Docker ─────────────
# A stub Docker reports 127.0.0.6 as a Docker network address of this server;
# the container has no real Docker bridge. systemd is not PID 1 here, so this
# checks the setting and its removal, not an actual boot.
mkdir -p "$TEST_DIR/stub-docker"
printf '%s\n' '#!/bin/sh' 'case "$*" in' '  "network ls -q") echo bridge ;;' '  "network inspect "*) echo "127.0.0.6 " ;;' \
    '  *) exit 1 ;;' 'esac' > "$TEST_DIR/stub-docker/docker"
chmod 755 "$TEST_DIR/stub-docker/docker"
AFTER_DOCKER=/etc/systemd/system/postgresql@16-main.service.d/perodua-after-docker.conf
write_empty_conf one_server app_one_server
sed -i 's/^DB_LISTEN_IP=.*/DB_LISTEN_IP=127.0.0.6/' "$TEST_DIR/one_server.conf"
PATH="$TEST_DIR/stub-docker:$PATH" deploy_ok one_server
grep -qxF '# Listen address: 127.0.0.6' "$AFTER_DOCKER" && grep -qxF 'After=docker.service' "$AFTER_DOCKER" \
    || die 'listening on a Docker address did not order PostgreSQL after Docker'
grep -q 'PostgreSQL starts after Docker at boot' "$TEST_DIR/one_server.log" || die 'the start-after-Docker setting was not reported'
grep -q 'Next, on this server: sudo bash deploy-app.sh --init-db' "$TEST_DIR/one_server.log" || die 'one server: the next step names the wrong server'
PATH="$TEST_DIR/stub-docker:$PATH" deploy_ok one_server
[[ $(grep -c 'After=docker.service' "$AFTER_DOCKER") == 1 ]] || die 'a rerun duplicated the start-after-Docker setting'
bash "$SOURCE_DIR/scripts/uninstall.sh" --role db --config "$TEST_DIR/one_server.conf" --confirm one_server \
    > "$TEST_DIR/uninst-one-server.log" 2>&1 || { cat "$TEST_DIR/uninst-one-server.log" >&2; die 'uninstall of the one-server deployment failed'; }
grep -q 'no longer start PostgreSQL after Docker at boot' "$TEST_DIR/uninst-one-server.log" || die 'uninstall did not list the start-after-Docker setting'
[[ ! -e $AFTER_DOCKER && ! -d ${AFTER_DOCKER%/*} ]] || die 'uninstall left the start-after-Docker setting'
[[ ",$(admin_sql -d postgres -c 'SHOW listen_addresses')," != *,127.0.0.6,* ]] || die 'uninstall left the Docker listen address'
pass 'one server: a Docker listen address makes PostgreSQL start after Docker (kept once on rerun); uninstall removes that setting with the listen address'

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

# Last: this uninstalls PostgreSQL itself.
python3 "$SOURCE_DIR/tests/check-uninstall-purge.py" "$SOURCE_DIR/scripts" "$TEST_DIR/password_prompt.conf" password_prompt
pass 'uninstall --purge: a wrong answer changes nothing; PURGE removes the deployment, PostgreSQL 16 and its data'

printf '\nALL %s INTEGRATION CHECKS PASSED\nLogs: %s\n' "$PASS_COUNT" "$TEST_DIR"
