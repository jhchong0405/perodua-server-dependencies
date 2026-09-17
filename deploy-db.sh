#!/usr/bin/env bash
# Native PostgreSQL 16 bootstrap. Restore only trusted pg_dump -Fc archives.
set +x +v
set -Eeuo pipefail
umask 077
export LC_ALL=C
unset PGPASSWORD PGPASSFILE PGHOST PGHOSTADDR PGPORT PGUSER PGDATABASE PGOPTIONS \
    PGSERVICE PGSERVICEFILE PGSSLMODE || true
unset DB_PASSWORD VERIFIER confirmation escaped || true

usage() {
    cat <<'EOF'
Usage: sudo bash deploy-db.sh [--config FILE] [--password-file FILE]
       bash deploy-db.sh --config FILE --check-config

Default configuration: deploy.conf next to this script.
Passwords are prompted on the terminal. --password-file accepts a root-owned
regular file with no group/other permissions (for controlled automation).
Requires Ubuntu 24.04, PostgreSQL 16 already installed, python3, curl and CA
certificates. Local BACKUP_FILE deployments do not require curl.
Only PostgreSQL custom-format archives (pg_dump -Fc) are accepted.
Existing databases are never overwritten, dropped or reimported.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG="$SCRIPT_DIR/deploy.conf"
PASSWORD_FILE=''
CHECK_ONLY=0
while (($#)); do
    case "$1" in
        --config|--password-file)
            (($# >= 2)) || die "Missing value for $1"
            if [[ $1 == --config ]]; then CONFIG=$2; else PASSWORD_FILE=$2; fi
            shift 2 ;;
        --check-config) CHECK_ONLY=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die "Unknown argument: $1" ;;
    esac
done
command -v python3 >/dev/null || die 'Install python3 (standard library only; no pip packages).'
[[ -f $CONFIG && ! -L $CONFIG ]] || die "Configuration is not a regular, non-symlink file: $CONFIG"
(( (8#$(stat -c %a "$CONFIG") & 8#022) == 0 )) || die 'Configuration must not be group/world writable.'
CONFIG=$(realpath -- "$CONFIG")

DB_NAME=perodua
DB_USER=odoo
DB_LC_COLLATE=C
DB_LC_CTYPE=C.UTF-8
PG_CLUSTER=main
PG_PORT=5432
BACKUP_FILE=''
BACKUP_URL=''
BACKUP_SHA256=''
DOWNLOAD_USER=''
DB_LISTEN_IP=127.0.0.1
APP_CIDR=''
MIN_FREE_MB=1024
EXPECTED_TABLES=public.ir_module_module,public.res_users

# Deliberately not `source`: configuration is data, never executable shell.
declare -A seen=()
trim() { local v=$1; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"; REPLY=$v; }
while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    trim "$line"; line=$REPLY
    [[ -z $line || $line == \#* ]] && continue
    [[ $line == *=* ]] || die 'Configuration lines must be KEY=value.'
    trim "${line%%=*}"; key=$REPLY
    trim "${line#*=}"; value=$REPLY
    case "$key" in
        DB_NAME|DB_USER|DB_LC_COLLATE|DB_LC_CTYPE|PG_CLUSTER|PG_PORT|BACKUP_FILE|BACKUP_URL|BACKUP_SHA256|DOWNLOAD_USER|DB_LISTEN_IP|APP_CIDR|MIN_FREE_MB|EXPECTED_TABLES) ;;
        *) die "Unknown configuration key: $key" ;;
    esac
    [[ ! ${seen[$key]+yes} ]] || die "Duplicate configuration key: $key"
    seen[$key]=1
    if [[ $value == \"*\" || $value == \'*\' ]]; then value=${value:1:${#value}-2}; fi
    printf -v "$key" '%s' "$value"
done < "$CONFIG"

[[ $DB_NAME =~ ^[a-z][a-z0-9_]{0,39}$ && $DB_NAME != postgres && $DB_NAME != template[01] ]] || die 'Use a non-system DB_NAME: lowercase letters, digits, underscore, up to 40 characters.'
[[ $DB_USER =~ ^[a-z][a-z0-9_]{0,39}$ && $DB_USER != postgres && $DB_USER != pg_* ]] || die 'Use a non-system DB_USER: lowercase letters, digits, underscore, up to 40 characters.'
[[ $PG_CLUSTER =~ ^[a-z][a-z0-9_]{0,29}$ ]] || die 'Invalid PG_CLUSTER.'
for locale in "$DB_LC_COLLATE" "$DB_LC_CTYPE"; do
    [[ $locale =~ ^[A-Za-z0-9_.@-]+$ ]] || die 'Invalid database locale; use the source database locale names.'
done
[[ $PG_PORT =~ ^[1-9][0-9]{0,4}$ ]] || die 'Invalid PG_PORT.'
((PG_PORT <= 65535)) || die 'Invalid PG_PORT.'
[[ $MIN_FREE_MB =~ ^[1-9][0-9]{0,8}$ ]] || die 'MIN_FREE_MB must be a positive integer.'
[[ $BACKUP_SHA256 =~ ^[[:xdigit:]]{64}$ ]] || die 'Set BACKUP_SHA256 to the trusted 64-digit SHA-256 of the archive.'
BACKUP_SHA256=${BACKUP_SHA256,,}
if [[ -n $BACKUP_FILE && -n $BACKUP_URL || -z $BACKUP_FILE && -z $BACKUP_URL ]]; then
    die 'Set exactly one of BACKUP_FILE and BACKUP_URL.'
fi
[[ $DOWNLOAD_USER != *:* && $DOWNLOAD_USER != *$'\n'* && $DOWNLOAD_USER != *$'\r'* ]] || die 'Invalid DOWNLOAD_USER.'
IFS=, read -r -a TABLES <<< "$EXPECTED_TABLES"
[[ -n $EXPECTED_TABLES && $EXPECTED_TABLES != *, ]] || die 'EXPECTED_TABLES must not be empty.'
for table in "${TABLES[@]}"; do
    [[ $table =~ ^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$ ]] || die "Invalid schema.table in EXPECTED_TABLES: $table"
done
python3 - "$DB_LISTEN_IP" "$APP_CIDR" "$BACKUP_URL" <<'PY'
import ipaddress, sys, urllib.parse
try:
    address = ipaddress.IPv4Address(sys.argv[1])
    if address.is_unspecified or address.is_multicast:
        raise ValueError('DB_LISTEN_IP must be a specific local IPv4 address, not 0.0.0.0 or multicast')
    if sys.argv[2]:
        network = ipaddress.IPv4Network(sys.argv[2], strict=True)
        if network.prefixlen == 0 or network.is_multicast:
            raise ValueError('APP_CIDR must be a restricted IPv4 CIDR, not 0.0.0.0/0')
        if address.is_loopback:
            raise ValueError('Set DB_LISTEN_IP to the database server internal IP when APP_CIDR is set')
    if sys.argv[3]:
        u = urllib.parse.urlsplit(sys.argv[3])
        if u.scheme != 'https' or not u.hostname or u.username is not None or u.fragment or any(ord(c) < 33 for c in sys.argv[3]):
            raise ValueError('BACKUP_URL must be a direct HTTPS URL without embedded credentials or whitespace')
except ValueError as exc:
    sys.exit(f'ERROR: {exc}')
PY
if [[ -n $BACKUP_FILE ]]; then
    [[ $BACKUP_FILE == /* ]] || BACKUP_FILE="$(dirname -- "$CONFIG")/$BACKUP_FILE"
    [[ -f $BACKUP_FILE ]] || die 'BACKUP_FILE does not exist or is not a regular file.'
    BACKUP_FILE=$(realpath -- "$BACKUP_FILE")
fi
if ((CHECK_ONLY)); then printf 'Configuration valid. No services, files or databases changed.\n'; exit 0; fi

[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'
# shellcheck source=/dev/null
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die 'This script supports Ubuntu 24.04.'
python3 - "$DB_LISTEN_IP" <<'PY'
import socket, sys
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((sys.argv[1], 0))
except OSError as exc:
    sys.exit(f'ERROR: DB_LISTEN_IP is not bindable on this server: {exc}')
PY
for tool in runuser flock pg_ctlcluster pg_conftool sha256sum; do
    command -v "$tool" >/dev/null || die "Missing tool: $tool. Install PostgreSQL 16 and required OS tools first."
done
[[ -z $BACKUP_URL ]] || command -v curl >/dev/null || die 'Install curl and ca-certificates for HTTPS downloads.'
PG_BIN=/usr/lib/postgresql/16/bin
[[ -x $PG_BIN/pg_restore && -x $PG_BIN/psql ]] || die 'Install postgresql-16 and postgresql-client-16 first.'
CLUSTER_CONF="/etc/postgresql/16/$PG_CLUSTER"
[[ -f $CLUSTER_CONF/postgresql.conf ]] || die 'The requested PostgreSQL 16 cluster does not exist.'
[[ $(pg_conftool 16 "$PG_CLUSTER" show port | awk '{print $NF}') == "$PG_PORT" ]] || die 'PG_PORT does not match the installed cluster; configure the cluster port first.'

STATE_BASE=/var/lib/perodua-db-deploy
for dir in "$STATE_BASE" "$STATE_BASE/16-$PG_CLUSTER" "$STATE_BASE/16-$PG_CLUSTER/$DB_NAME"; do
    [[ ! -L $dir ]] || die "Refusing symlink state directory: $dir"
    if [[ -e $dir ]]; then
        [[ -d $dir && $(stat -c %u "$dir") == 0 ]] || die 'State directories must be root-owned directories.'
        (( (8#$(stat -c %a "$dir") & 8#077) == 0 )) || die 'State directories must have mode 0700.'
    else
        mkdir -m 700 -- "$dir"
    fi
done
STATE_DIR="$STATE_BASE/16-$PG_CLUSTER/$DB_NAME"
# Cluster-wide: account and PostgreSQL configuration changes are shared.
exec 9> "$STATE_BASE/16-$PG_CLUSTER/deploy.lock"
flock -n 9 || die 'Another deployment is running for this PostgreSQL cluster.'
LOG_FILE="$STATE_DIR/$(date -u +%Y%m%dT%H%M%S)-$$.log"
exec 7>&1 8>&2
exec > >(tee -a "$LOG_FILE" 9>&-) 2>&1
LOG_PID=$!
TMP_DIR=$(mktemp -d /var/tmp/perodua-db-deploy.XXXXXXXX)
STAGING=''
PHASE=preflight
NETWORK_DIRTY=0
HBA_BACKUP=''
PGCONFIG_BACKUP=''
cleanup() {
    local rc=$?
    unset DB_PASSWORD VERIFIER
    [[ -z ${TMP_DIR:-} ]] || rm -rf -- "$TMP_DIR"
    if ((rc)); then
        if ((NETWORK_DIRTY)); then
            printf 'Restoring configuration snapshots after failed network setup.\n' >&2
            [[ -z $HBA_BACKUP ]] || cp -p -- "$HBA_BACKUP" "$HBA"
            [[ -z $PGCONFIG_BACKUP ]] || cp -p -- "$PGCONFIG_BACKUP" "$CLUSTER_CONF/postgresql.conf"
            if [[ -n $PGCONFIG_BACKUP ]]; then
                pg_ctlcluster 16 "$PG_CLUSTER" restart || printf 'Could not restart restored configuration; administrator action required.\n' >&2
            else
                pg_ctlcluster 16 "$PG_CLUSTER" reload || printf 'Could not reload restored configuration; administrator action required.\n' >&2
            fi
        fi
        printf 'FAILED during %s. Target databases were not overwritten. Log: %s\n' "$PHASE" "$LOG_FILE" >&2
        [[ -z $STAGING ]] || printf 'Staging database (if created) retained for inspection: %s\n' "$STAGING" >&2
    fi
    flock -u 9 || true
    # Flush the log before returning, without leaving tee holding the lock or
    # allowing an immediate rerun to race the final log writes.
    exec 1>&7 2>&8 7>&- 8>&-
    wait "$LOG_PID" || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'printf "Command failed at script line %s (phase: %s).\n" "$LINENO" "$PHASE" >&2' ERR

admin() { runuser -u postgres -- "$PG_BIN/psql" -X -w -h /var/run/postgresql -p "$PG_PORT" -d postgres -v ON_ERROR_STOP=1 -At "$@"; }
admin_db() { local db=$1; shift; runuser -u postgres -- "$PG_BIN/psql" -X -w -h /var/run/postgresql -p "$PG_PORT" -d "$db" -v ON_ERROR_STOP=1 -At "$@"; }
has_systemd() { [[ $(cat /proc/1/comm) == systemd ]]; }
start_cluster() {
    if has_systemd; then
        systemctl enable "postgresql@16-$PG_CLUSTER.service"
        systemctl start "postgresql@16-$PG_CLUSTER.service"
    else
        # Allows real PostgreSQL verification in a disposable Linux container.
        pg_ctlcluster 16 "$PG_CLUSTER" status >/dev/null 2>&1 || pg_ctlcluster 16 "$PG_CLUSTER" start
        log 'systemd is not PID 1: cluster started with pg_ctlcluster; boot enablement is not verified.'
    fi
    local attempt=0
    until admin -c 'SELECT 1' >/dev/null 2>&1; do
        ((attempt+=1))
        ((attempt < 30)) || die 'PostgreSQL did not become ready.'
        sleep 1
    done
}
restart_cluster() {
    if has_systemd; then systemctl restart "postgresql@16-$PG_CLUSTER.service"; else pg_ctlcluster 16 "$PG_CLUSTER" restart; fi
    start_cluster
}
start_cluster
[[ $(admin -c 'SHOW server_version_num') == 16???? ]] || die 'Connected server is not PostgreSQL 16.'
[[ $(admin -c 'SHOW config_file') == "$CLUSTER_CONF/postgresql.conf" ]] || die 'Connected to an unexpected cluster.'

TARGET_OID=$(admin -c "SELECT oid FROM pg_database WHERE datname='$DB_NAME'")
RESTORED=0
if [[ -n $TARGET_OID ]]; then
    # An OID plus checksum and owner prevents interpreting an unrelated database
    # as a previous deployment merely because its name matches.
    identity="$BACKUP_SHA256|$TARGET_OID|$DB_USER"
    if [[ -f $STATE_DIR/success && $(cat "$STATE_DIR/success") == "$identity" ]]; then
        RESTORED=1
    elif [[ -f $STATE_DIR/pending && $(cat "$STATE_DIR/pending") == "$identity" ]]; then
        RESTORED=1
        log 'Recovering a previously validated rename interrupted before completion marking.'
    else
        die "Database $DB_NAME already exists and does not match this deployment. Nothing will be overwritten."
    fi
    [[ $(admin -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='$DB_NAME'") == "$DB_USER" ]] || die 'Existing database ownership changed.'
    [[ $(admin -c "SELECT datcollate || '|' || datctype FROM pg_database WHERE datname='$DB_NAME'") == "$DB_LC_COLLATE|$DB_LC_CTYPE" ]] || die 'Configured locales differ from the existing database; changing locales requires a separate migration.'
fi

PHASE=credentials
if [[ -n $PASSWORD_FILE ]]; then
    [[ -f $PASSWORD_FILE && ! -L $PASSWORD_FILE && $(stat -c %u "$PASSWORD_FILE") == 0 ]] || die 'Password file must be a root-owned regular file, not a symlink.'
    (( (8#$(stat -c %a "$PASSWORD_FILE") & 8#077) == 0 )) || die 'Password file must have no group/other permissions (use chmod 600).'
    DB_PASSWORD=$(cat -- "$PASSWORD_FILE")
else
    [[ -r /dev/tty ]] || die 'No terminal available; use --password-file with a protected file.'
    IFS= read -r -s -p "Database password for $DB_USER: " DB_PASSWORD </dev/tty
    printf '\n' >/dev/tty
    IFS= read -r -s -p 'Confirm database password: ' confirmation </dev/tty
    printf '\n' >/dev/tty
    [[ $DB_PASSWORD == "$confirmation" ]] || die 'Passwords do not match.'
    unset confirmation
fi
[[ ${#DB_PASSWORD} -ge 12 && ${#DB_PASSWORD} -le 1024 && $DB_PASSWORD != *[!\ -\~]* ]] || die 'Use a 12-1024 character printable ASCII password (spaces and punctuation are supported).'
escaped=${DB_PASSWORD//\\/\\\\}; escaped=${escaped//:/\\:}
printf '127.0.0.1:%s:*:%s:%s\n' "$PG_PORT" "$DB_USER" "$escaped" > "$TMP_DIR/pgpass"
unset escaped
app() { local db=$1; shift; PGPASSFILE="$TMP_DIR/pgpass" "$PG_BIN/psql" -X -w -h 127.0.0.1 -p "$PG_PORT" -U "$DB_USER" -d "$db" -v ON_ERROR_STOP=1 -At "$@"; }

if (( ! RESTORED )); then
    PHASE=download
    free_mb=$(df -Pm "$TMP_DIR" | awk 'NR==2 {print $4}')
    ((free_mb >= MIN_FREE_MB)) || die 'Insufficient free space for backup download.'
    ARCHIVE="$TMP_DIR/database.dump"
    if [[ -n $BACKUP_FILE ]]; then
        cp -- "$BACKUP_FILE" "$ARCHIVE"
    else
        curl_args=(--fail --silent --show-error --proto '=https' --tlsv1.2 --connect-timeout 30 --output "$ARCHIVE" --write-out '%{http_code}')
        if [[ -n $DOWNLOAD_USER ]]; then
            [[ -r /dev/tty ]] || die 'Authenticated download requires a terminal; use BACKUP_FILE for unattended deployment.'
            curl_args+=(--user "$DOWNLOAD_USER")
            log 'Enter the cloud download password when curl prompts.'
        fi
        code=$(curl --disable "${curl_args[@]}" --url "$BACKUP_URL")
        [[ $code == 200 ]] || die 'Download must return HTTP 200; use a direct URL (redirects are not followed).'
    fi
    [[ $(sha256sum "$ARCHIVE" | cut -d ' ' -f 1) == "$BACKUP_SHA256" ]] || die 'Backup SHA-256 mismatch.'
    [[ $(head -c 5 "$ARCHIVE") == PGDMP ]] || die 'Use a PostgreSQL custom-format dump (pg_dump -Fc), not SQL, ZIP or a Docker volume.'
    "$PG_BIN/pg_restore" --list "$ARCHIVE" > "$TMP_DIR/toc"
    data_dir=$(admin -c 'SHOW data_directory')
    free_mb=$(df -Pm "$data_dir" | awk 'NR==2 {print $4}')
    ((free_mb >= MIN_FREE_MB)) || die 'Insufficient free space on the PostgreSQL data filesystem.'
    STAGING="${DB_NAME}__restore_$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
fi

PHASE=role
role_state=$(admin -c "SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) FROM pg_roles WHERE rolname='$DB_USER'")
if [[ -z $role_state ]]; then
    (( ! RESTORED )) || die 'Previously deployed database role is missing.'
    # Generate the SCRAM verifier locally. Plaintext is never SQL, argv or env.
    # shellcheck disable=SC2016 # Python f-string dollars are literal, not shell variables.
    VERIFIER=$(printf '%s' "$DB_PASSWORD" | python3 -c '
import sys, os, hashlib, hmac, base64
p=sys.stdin.buffer.read(); salt=os.urandom(16); rounds=4096
salted=hashlib.pbkdf2_hmac("sha256",p,salt,rounds)
stored=hashlib.sha256(hmac.digest(salted,b"Client Key","sha256")).digest()
server=hmac.digest(salted,b"Server Key","sha256")
b64=lambda b:base64.b64encode(b).decode()
print(f"SCRAM-SHA-256${rounds}:{b64(salt)}${b64(stored)}:{b64(server)}")')
    printf "SET log_statement='none'; SET log_min_duration_statement=-1; CREATE ROLE \"%s\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '%s';\n" "$DB_USER" "$VERIFIER" | admin >/dev/null
    unset VERIFIER
elif [[ $role_state != t ]]; then
    die 'Existing app role has elevated privileges, memberships or NOLOGIN. Use a dedicated non-privileged login role.'
fi
unset DB_PASSWORD

if (( ! RESTORED )); then
    printf "CREATE DATABASE \"%s\" OWNER \"%s\" TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE '%s' LC_CTYPE '%s';\nREVOKE ALL ON DATABASE \"%s\" FROM PUBLIC;\n" "$STAGING" "$DB_USER" "$DB_LC_COLLATE" "$DB_LC_CTYPE" "$STAGING" | admin >/dev/null
fi

PHASE=network
# Prepend a database-specific block: a later broad HBA rule must never bypass it.
# Leave local Unix socket access and all other databases' rules untouched.
HBA=$(admin -c 'SHOW hba_file')
[[ $HBA == "$CLUSTER_CONF/pg_hba.conf" && ! -L $HBA ]] || die 'Custom/symlink pg_hba.conf is not supported; configure access manually first.'
[[ $(admin -c 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') == 0 ]] || die 'Existing HBA configuration has errors; repair it first.'
HBA_BACKUP="$STATE_DIR/pg_hba.conf.$(date -u +%Y%m%dT%H%M%S).$$.bak"
cp -p -- "$HBA" "$HBA_BACKUP"
NETWORK_DIRTY=1
python3 - "$HBA" "$DB_NAME" "$DB_USER" "$APP_CIDR" "$STAGING" <<'PY'
import os, stat, sys, tempfile
path, db, user, cidr, stage = sys.argv[1:]
begin=f'# BEGIN perodua-db-deploy {db}'
end=f'# END perodua-db-deploy {db}'
with open(path) as f: old=f.read()
lines=old.splitlines(); remaining=[]; inside=False
if lines.count(begin)!=lines.count(end) or lines.count(begin)>1:
    sys.exit('ERROR: malformed managed HBA block; inspect configuration manually')
for line in lines:
    if line==begin: inside=True; continue
    if line==end:
        if not inside: sys.exit('ERROR: malformed managed HBA block order')
        inside=False; continue
    if not inside: remaining.append(line)
block=[begin]
for name in filter(None, [db, stage]):
    block.append(f'host "{name}" "{user}" 127.0.0.1/32 scram-sha-256')
    if cidr and name==db: block.append(f'host "{name}" "{user}" {cidr} scram-sha-256')
    block += [f'host "{name}" all 0.0.0.0/0 reject',f'host "{name}" all ::/0 reject']
text='\n'.join(block+[end]+remaining)+'\n'
s=os.stat(path); fd,tmp=tempfile.mkstemp(dir=os.path.dirname(path))
try:
    os.fchmod(fd,stat.S_IMODE(s.st_mode)); os.fchown(fd,s.st_uid,s.st_gid)
    with os.fdopen(fd,'w') as f: f.write(text)
    os.replace(tmp,path)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PY
if [[ $(admin -c 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') != 0 ]]; then
    die 'HBA validation failed; inspect the log and saved configuration before reloading.'
fi
admin -c 'SELECT pg_reload_conf()' >/dev/null

# Preserve existing listeners and add only the requested address and loopback.
current_listen=$(admin -c 'SHOW listen_addresses')
new_listen=$(python3 - "$current_listen" "$DB_LISTEN_IP" <<'PY'
import sys
items=[s.strip() for s in sys.argv[1].split(',') if s.strip()]
if '*' not in items:
    for ip in ['127.0.0.1',sys.argv[2]]:
        if ip=='127.0.0.1' and 'localhost' in items: continue
        if ip not in items: items.append(ip)
print(','.join(items))
PY
)
if [[ $new_listen != "$current_listen" ]]; then
    PGCONFIG_BACKUP="$STATE_DIR/postgresql.conf.$(date -u +%Y%m%dT%H%M%S).$$.bak"
    cp -p -- "$CLUSTER_CONF/postgresql.conf" "$PGCONFIG_BACKUP"
    pg_conftool 16 "$PG_CLUSTER" set listen_addresses "$new_listen"
    # pg_file_settings reports "setting could not be applied" for a valid
    # postmaster setting while the old process is running. Parse startup config
    # using postgres -C instead, including postgresql.auto.conf precedence.
    config_listen=$(runuser -u postgres -- "$PG_BIN/postgres" \
        -D "$(admin -c 'SHOW data_directory')" \
        -c "config_file=$CLUSTER_CONF/postgresql.conf" -C listen_addresses)
    [[ $config_listen == "$new_listen" ]] || die 'listen_addresses is overridden by another configuration file.'
    restart_cluster
    [[ $(admin -c 'SHOW listen_addresses') == "$new_listen" ]] || die 'listen_addresses was overridden by another setting; inspect PostgreSQL configuration.'
fi
"$PG_BIN/pg_isready" -h "$DB_LISTEN_IP" -p "$PG_PORT" >/dev/null || die 'PostgreSQL is not accepting connections at DB_LISTEN_IP.'
NETWORK_DIRTY=0

verify_db() {
    local db=$1 table relation
    [[ $(app "$db" -c 'SELECT current_user') == "$DB_USER" ]] || die 'Application password authentication failed.'
    for table in "${TABLES[@]}"; do
        relation="\"${table%%.*}\".\"${table#*.}\""
        [[ $(app "$db" -c "SELECT to_regclass('$relation') IS NOT NULL") == t ]] || die "Expected table missing: $table"
        app "$db" -c "SELECT 1 FROM $relation LIMIT 1" >/dev/null
    done
}

if (( ! RESTORED )); then
    PHASE=restore
    # Authenticate BEFORE import, including when reusing an existing role.
    [[ $(app "$STAGING" -c 'SELECT current_user') == "$DB_USER" ]] || die 'Application password authentication failed.'
    log "Restoring into staging database $STAGING."
    # Run as the app role; trusted extensions (e.g. unaccent, pg_trgm) can be
    # created by its database owner. Privileged extensions fail closed and must
    # be handled in a reviewed preparation step; never elevate the app role.
    # Root opens stdin so the postgres OS user cannot read private credentials.
    runuser -u postgres -- "$PG_BIN/pg_restore" -h /var/run/postgresql -p "$PG_PORT" \
        --dbname="$STAGING" --role="$DB_USER" --no-owner --no-acl \
        --no-tablespaces --no-publications --no-subscriptions \
        --exit-on-error --single-transaction < "$ARCHIVE"
    PHASE=verification
    verify_db "$STAGING"
    app "$STAGING" -c 'ANALYZE' >/dev/null
    stage_oid=$(admin -c "SELECT oid FROM pg_database WHERE datname='$STAGING'")
    # Write identity before rename to recover the rename/marker crash window.
    printf '%s|%s|%s\n' "$BACKUP_SHA256" "$stage_oid" "$DB_USER" > "$STATE_DIR/pending.tmp"
    mv -- "$STATE_DIR/pending.tmp" "$STATE_DIR/pending"
    PHASE=publish
    # ALTER DATABASE refuses if a conflicting target appeared. Never terminate
    # sessions, use --clean, or drop a database to force publication.
    admin -c "ALTER DATABASE \"$STAGING\" RENAME TO \"$DB_NAME\"" >/dev/null
    TARGET_OID=$stage_oid
    STAGING=''
else
    log 'Matching database already deployed; skipping download and restore.'
fi

PHASE=final-verification
verify_db "$DB_NAME"
[[ $(admin -c "SELECT oid FROM pg_database WHERE datname='$DB_NAME'") == "$TARGET_OID" ]] || die 'Database identity changed during verification.'
printf '%s|%s|%s\n' "$BACKUP_SHA256" "$TARGET_OID" "$DB_USER" > "$STATE_DIR/success.tmp"
mv -- "$STATE_DIR/success.tmp" "$STATE_DIR/success"
printf 'DB_HOST=%s\nDB_PORT=%s\nDB_NAME=%s\nDB_USER=%s\n' "$DB_LISTEN_IP" "$PG_PORT" "$DB_NAME" "$DB_USER" > "$STATE_DIR/connection.txt"
log 'SUCCESS: database restored/verified and PostgreSQL is running.'
cat "$STATE_DIR/connection.txt"
printf 'Log: %s\nConnection summary: %s/connection.txt\n' "$LOG_FILE" "$STATE_DIR"
if [[ -n $APP_CIDR ]]; then
    printf 'HBA permits %s. Allow TCP %s from that source in the host/cloud firewall, then test from App Server.\n' "$APP_CIDR" "$PG_PORT"
else
    printf 'Database is restricted to local TCP access. Set DB_LISTEN_IP and APP_CIDR, then rerun to enable App Server access.\n'
fi
printf 'This verifies PostgreSQL only; Odoo startup and filestore restoration run on App Server.\n'
