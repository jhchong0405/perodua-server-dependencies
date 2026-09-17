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
}.items():
    path = directory / (name + '.conf')
    path.write_text(base + '\n' + payload + '\n')
    path.chmod(0o600)
PY
for name in dollar_injection backtick_injection extra_command unknown_key; do
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

python3 "$SOURCE_DIR/tests/check-https-download.py" "$SOURCE_DIR" "$TEST_DIR"
assert_sql https_restored 'SELECT count(*) FROM public.res_users' 2
assert_absent https_refused
pass 'real HTTPS Basic-auth download prompts securely; wrong cloud password prevents restore'

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
