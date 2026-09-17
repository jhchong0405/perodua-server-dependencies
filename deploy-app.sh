#!/usr/bin/env bash
# App-only Client Stable UIUX deployment. Database service stays on its own host.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077

RELEASE=client-stable-uiux-v1.0.0
REVISION=ec7c22386204371975ad79931f57c8091c07935d
ODOO_IMAGE=ghcr.io/jhchong0405/perodua-odoo:client-stable-uiux-v1.0.0@sha256:c16053940627c1c939a742327c2426b83355bf388a5fd2796894cb21770870da
WEB_IMAGE=ghcr.io/jhchong0405/perodua-odoo:client-stable-uiux-web-v1.0.0@sha256:6f7f7bc3700c6506ab43a9505940538893d75ef4a396e0cc8077f37106dcbdff
INIT_MODULES=perodua_client_stable,perodua_demo_client,perodua_gateway,perodua_forecast_workbook,perodua_supplier_execution,perodua_uiux_api
DEPLOY_DIR=/opt/perodua-app
CONFIG='' NON_INTERACTIVE=0 INIT_DB=0 TEMP_DIR='' AUTH_DIR='' STARTED=0
DB_HOST='' DB_PORT=5432 DB_NAME=perodua DB_USER=odoo DB_PASSWORD_FILE=''
PROJECT_NAME=perodua-client-uiux HTTP_PORT=8110 BIND_IP=0.0.0.0
STARTUP_TIMEOUT=600 INIT_TIMEOUT=3600

usage() {
    cat <<'HELP'
Usage: bash deploy-app.sh [--config PATH] [--dir PATH] [--non-interactive] [--init-db]
Deploy the pinned Client Stable UIUX v1.0.0 Odoo + Web images using Docker Compose.
Requires a reachable external PostgreSQL 16 server; does not install or configure it.
Default: use an already initialized, matching Client Stable UIUX database.
--init-db permits creation/initialization of a NEW or EMPTY database and seeds the
release's client demonstration dataset. It never upgrades an existing database.
--dir defaults to /opt/perodua-app; existing configuration is reused there.
--config accepts literal KEY=VALUE lines (see app.env.example), never shell code.
HTTP only: default 0.0.0.0:8110. Odoo ports are private to the Compose network.
HELP
}
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
cleanup() {
    local status=$?
    trap - EXIT
    [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
    [[ -z $AUTH_DIR ]] || rm -rf -- "$AUTH_DIR"
    if (( status != 0 && STARTED )); then
        printf 'Deployment did not pass verification. Existing database and volumes were retained.\n' >&2
        printf 'Logs: docker compose --project-name %q --file %q logs --tail 100\n' "$PROJECT_NAME" "$DEPLOY_DIR/compose.yml" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
trap 'printf "Deployment failed at line %s.\n" "$LINENO" >&2' ERR
while (($#)); do
    case $1 in
        --config|--dir)
            (($# >= 2)) || fail "$1 requires a path"
            if [[ $1 == --config ]]; then CONFIG=$2; else DEPLOY_DIR=$2; fi
            shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --init-db) INIT_DB=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
[[ $DEPLOY_DIR == /* && $DEPLOY_DIR != / && $DEPLOY_DIR != *$'\n'* ]] || fail '--dir must be an absolute directory path, not /'
[[ ! -L $DEPLOY_DIR ]] || fail 'Deployment directory must not be a symbolic link'
if [[ -z $CONFIG && -f $DEPLOY_DIR/app.env ]]; then CONFIG=$DEPLOY_DIR/app.env; fi
if [[ -n $CONFIG ]]; then
    [[ -r $CONFIG ]] || fail 'Configuration file is not readable'
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        [[ $line =~ ^([A-Z_]+)=(.*)$ ]] || fail 'Configuration must contain literal KEY=VALUE lines'
        key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
        case $key in
            DB_HOST|DB_PORT|DB_NAME|DB_USER|DB_PASSWORD_FILE|PROJECT_NAME|HTTP_PORT|BIND_IP|STARTUP_TIMEOUT|INIT_TIMEOUT)
                printf -v "$key" '%s' "$value" ;;
            *) fail "Unknown configuration key: $key" ;;
        esac
    done < "$CONFIG"
fi
prompt() {
    local name=$1 label=$2 answer
    if ((NON_INTERACTIVE)); then [[ -n ${!name} ]] || fail "$name is required in --config"; return; fi
    [[ -t 0 ]] || fail 'Interactive deployment requires a terminal; use --config and --non-interactive'
    read -r -p "$label [${!name}]: " answer || fail 'Input cancelled'
    [[ -z $answer ]] || printf -v "$name" '%s' "$answer"
    [[ -n ${!name} ]] || fail "$name is required"
}
# Supplying a complete configuration avoids repeated prompts on subsequent runs.
if [[ -z $CONFIG ]]; then
    prompt DB_HOST 'Database server IP / hostname'
    prompt DB_PORT 'Database port'
    prompt DB_NAME 'Application database name'
    prompt DB_USER 'Database username'
fi
[[ $DB_HOST =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]] || fail 'DB_HOST must be an IP address or hostname (no URL or shell syntax)'
for key in DB_NAME DB_USER; do
    [[ ${!key} =~ ^[A-Za-z_][A-Za-z0-9_-]{0,62}$ ]] || fail "$key must be a simple database identifier"
done
[[ $DB_NAME != postgres && $DB_NAME != template0 && $DB_NAME != template1 ]] || fail 'DB_NAME must be an application database'
[[ $PROJECT_NAME =~ ^[a-z][a-z0-9_-]{0,49}$ ]] || fail 'PROJECT_NAME must be a lowercase Compose project name (max 50 characters)'
for key in DB_PORT HTTP_PORT STARTUP_TIMEOUT INIT_TIMEOUT; do
    [[ ${!key} =~ ^[1-9][0-9]{0,4}$ ]] || fail "$key must be a positive integer"
    ((${!key} <= 65535)) || fail "$key exceeds 65535"
done
[[ $BIND_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || fail 'BIND_IP must be an IPv4 address'
IFS=. read -r -a octets <<< "$BIND_IP"
for octet in "${octets[@]}"; do ((10#$octet <= 255)) || fail 'Invalid BIND_IP'; done
[[ -z $DB_PASSWORD_FILE || ( $DB_PASSWORD_FILE == /* && -r $DB_PASSWORD_FILE && -f $DB_PASSWORD_FILE ) ]] || fail 'DB_PASSWORD_FILE must be an absolute path to a readable file'
if ((NON_INTERACTIVE)) && [[ -z $DB_PASSWORD_FILE && ! -r $DEPLOY_DIR/secrets/db_password ]]; then
    fail 'DB_PASSWORD_FILE is required for the first non-interactive deployment'
fi
command -v docker >/dev/null || fail 'Docker is missing. First run install-dependencies.sh --role app on Ubuntu 24.04'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required'
docker info >/dev/null 2>&1 || fail 'Cannot reach Docker. Start the engine and check your account permissions'
engine=$(docker info --format '{{.OSType}}/{{.Architecture}}')
[[ $engine == linux/x86_64 || $engine == linux/amd64 ]] || fail 'The Docker engine must run Linux amd64 containers'
mkdir -p -- "$DEPLOY_DIR"
DEPLOY_DIR=$(cd -- "$DEPLOY_DIR" && pwd -P)
[[ $DEPLOY_DIR != / && $DEPLOY_DIR != *$'\n'* ]] || fail 'Unsupported directory path'
if [[ ! -e $DEPLOY_DIR/.deployment-identity ]]; then
    [[ -z $(find "$DEPLOY_DIR" -mindepth 1 -maxdepth 1 ! -name .deploy.lock -print -quit) ]] || fail 'Use an empty deployment directory; keep your initial config and password files elsewhere'
fi
chmod 0700 "$DEPLOY_DIR"
exec 9>"$DEPLOY_DIR/.deploy.lock"
flock -n 9 || fail 'Another deployment is running in this directory'
TEMP_DIR=$(mktemp -d "$DEPLOY_DIR/.deploy.XXXXXXXX")
printf '%s\n' "release=$RELEASE" "revision=$REVISION" "project=$PROJECT_NAME" "host=$DB_HOST" "port=$DB_PORT" "database=$DB_NAME" "user=$DB_USER" > "$TEMP_DIR/identity"
if [[ -e $DEPLOY_DIR/.deployment-identity ]]; then
    cmp -s "$TEMP_DIR/identity" "$DEPLOY_DIR/.deployment-identity" || fail 'Directory belongs to a different database, project, or release. Use a separate deployment directory'
elif [[ -e $DEPLOY_DIR/compose.yml || -e $DEPLOY_DIR/secrets || -e $DEPLOY_DIR/app.env ]]; then
    fail 'Unmanaged deployment files already exist in this directory; use an empty directory'
fi
containers=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME")
for container in $containers; do
    owner=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")
    [[ $owner == "$DEPLOY_DIR" ]] || fail 'PROJECT_NAME is already used from a different directory'
done
if [[ ! -e $DEPLOY_DIR/.deployment-identity ]]; then
    for kind in volume network; do
        existing=$(docker "$kind" ls -q --filter "label=com.docker.compose.project=$PROJECT_NAME")
        [[ -z $existing ]] || fail 'PROJECT_NAME already owns Docker resources; choose a new project name'
    done
fi
if [[ -n $DB_PASSWORD_FILE ]]; then
    password=$(<"$DB_PASSWORD_FILE")
elif [[ -r $DEPLOY_DIR/secrets/db_password ]]; then
    password=$(<"$DEPLOY_DIR/secrets/db_password")
else
    ((NON_INTERACTIVE == 0)) || fail 'DB_PASSWORD_FILE is required for the first non-interactive deployment'
    [[ -t 0 ]] || fail 'Password input requires a terminal'
    read -r -s -p 'Database password (hidden): ' password || fail 'Input cancelled'
    printf '\n'
fi
[[ -n $password && $password != *$'\n'* && $password != *$'\r'* ]] || fail 'Database password must be nonempty and contain no line breaks'
printf '%s' "$password" > "$TEMP_DIR/db_password"
unset password
# The enclosing secrets directory is 0700. 0444 lets the unprivileged image UID
# read its bind-mounted secret; Docker Compose file secrets do not remap modes.
chmod 0444 "$TEMP_DIR/db_password"

auth_error() { grep -Eiq 'unauthorized|authentication required|denied|forbidden|403|401|no basic auth' "$1"; }
pull_image() {
    local image=$1 username token attempts=0 context_endpoint
    printf 'Pulling pinned image: %s (the first download can take several minutes)...\n' "${image%@*}"
    while ! docker pull "$image" > "$TEMP_DIR/pull.log" 2>&1; do
        if ! auth_error "$TEMP_DIR/pull.log"; then
            if grep -Eiq 'manifest unknown|not found|manifest invalid' "$TEMP_DIR/pull.log"; then
                fail 'The pinned image was not found in GHCR. Check the release with the publisher'
            fi
            fail 'GHCR pull failed due to network, registry, or Docker error. Check connectivity and available disk space'
        fi
        ((NON_INTERACTIVE == 0)) || fail 'GHCR authentication is required. Login with docker login ghcr.io first, then retry'
        ((attempts < 3)) || fail 'GHCR authentication failed after three attempts'
        [[ -t 0 ]] || fail 'GHCR authentication requires a terminal'
        printf 'GHCR requires authentication. Token needs read:packages and access to this package.\n'
        read -r -p 'GitHub username (blank cancels): ' username || fail 'Login cancelled'
        [[ -n $username ]] || fail 'Login cancelled'
        [[ $username =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]] || fail 'Invalid GitHub username'
        read -r -s -p 'GHCR token (hidden; blank cancels): ' token || fail 'Login cancelled'
        printf '\n'
        [[ -n $token ]] || fail 'Login cancelled'
        if [[ -z $AUTH_DIR ]]; then
            # Resolve the existing engine before changing Docker's config. A fresh
            # config has no credential helper, so docker login cannot overwrite it.
            context_endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}')
            [[ -n ${DOCKER_HOST:-} ]] || export DOCKER_HOST=$context_endpoint
            AUTH_DIR=$(mktemp -d)
            export DOCKER_CONFIG=$AUTH_DIR
            unset DOCKER_CONTEXT
            printf '{}\n' > "$AUTH_DIR/config.json"
        fi
        attempts=$((attempts + 1))
        if ! printf '%s' "$token" | docker login ghcr.io --username "$username" --password-stdin > "$TEMP_DIR/login.log" 2>&1; then
            printf 'Login failed. Check the token, username and GHCR connection.\n' >&2
        fi
        unset token
    done
    printf 'Pulled pinned image: %s\n' "${image%@*}"
}
pull_image "$ODOO_IMAGE"
pull_image "$WEB_IMAGE"

cat > "$TEMP_DIR/preflight.py" <<'PY'
import hashlib, os, pathlib, sys
import psycopg2
mode = sys.argv[1]
expected = os.environ['INIT_MODULES'].split(',')
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
            category = 'incorrect username or password'
        elif 'no pg_hba.conf entry' in detail: category = 'DB Server pg_hba.conf does not allow this connection'
        elif 'connection refused' in detail: category = 'connection refused; verify IP, port and listening service'
        elif 'timeout' in detail: category = 'connection timed out; verify routing and firewall'
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
with connect(db) as cn:
    with cn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')")
        if not cur.fetchone()[0]:
            print('EMPTY')
            sys.exit(0)
        cur.execute("SELECT to_regclass('public.ir_module_module'), to_regclass('public.ir_config_parameter')")
        if not all(cur.fetchone()): fail('target is neither empty nor an initialized Odoo database')
        cur.execute('SELECT name FROM ir_module_module WHERE name=ANY(%s) AND state=%s', (expected, 'installed'))
        missing = sorted(set(expected) - {row[0] for row in cur.fetchall()})
        if missing: fail('required modules are not installed: ' + ', '.join(missing))
        cur.execute("SELECT name FROM ir_module_module WHERE state IN ('to install','to upgrade','to remove') LIMIT 1")
        if cur.fetchone(): fail('database contains pending module changes; finish them separately')
        names = sorted(p for p in pathlib.Path('/opt/perodua-addons').glob('perodua_*') if p.is_dir())
        fingerprint = hashlib.md5(b''.join((p / '__manifest__.py').read_bytes() for p in names)).hexdigest()
        cur.execute("SELECT key,value FROM ir_config_parameter WHERE key IN ('perodua.runtime_profile','perodua.image_modhash')")
        stored = dict(cur.fetchall())
        if mode == 'stamp':
            for key, value in [('perodua.runtime_profile','client-stable-uiux'),('perodua.image_modhash',fingerprint)]:
                cur.execute('INSERT INTO ir_config_parameter (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', (key,value))
        elif stored.get('perodua.runtime_profile') != 'client-stable-uiux':
            fail('runtime profile does not match client-stable-uiux; restore the correct database')
        elif stored.get('perodua.image_modhash') != fingerprint:
            fail('database module fingerprint does not match this release; upgrades require a separate plan')
        cur.execute("SELECT count(*) FROM ir_attachment WHERE store_fname IS NOT NULL")
        attachments = cur.fetchone()[0]
        print('READY ' + str(attachments))
PY
cat > "$TEMP_DIR/compose.yml" <<YAML
services:
  odoo:
    image: $ODOO_IMAGE
    environment:
      DB_HOST: "$DB_HOST"
      DB_PORT: "$DB_PORT"
      DB_USER: "$DB_USER"
      DB_PASSWORD_FILE: /run/secrets/db_password
      DB_NAME: "$DB_NAME"
      PERODUA_DB: "$DB_NAME"
      PERODUA_IMAGE_PROFILE: client-stable-uiux
      INIT_MODULES: "$INIT_MODULES"
    command: ["odoo", "-c", "/etc/odoo/odoo.conf", "-d", "$DB_NAME", "--proxy-mode", "--workers=0"]
    secrets: [db_password]
    volumes:
      - filestore:/var/lib/odoo
      - ./preflight.py:/opt/deploy/preflight.py:ro
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8069/web/health', timeout=5)"]
      interval: 10s
      timeout: 8s
      retries: 6
      start_period: 30s
    restart: unless-stopped
  web:
    image: $WEB_IMAGE
    environment:
      ODOO_UPSTREAM: odoo:8069
      ODOO_WS_UPSTREAM: odoo:8069
    depends_on:
      odoo:
        condition: service_healthy
    ports:
      - "$BIND_IP:$HTTP_PORT:80"
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/app/version.json"]
      interval: 5s
      timeout: 5s
      retries: 6
    restart: unless-stopped
secrets:
  db_password:
    file: ./secrets/db_password
volumes:
  filestore:
YAML
# Generated helper is read-only, uses the release image's Python and psycopg2.
chmod 0444 "$TEMP_DIR/preflight.py"
install -d -m 0700 "$DEPLOY_DIR/secrets"
for file in compose.yml preflight.py; do mv -f -- "$TEMP_DIR/$file" "$DEPLOY_DIR/$file"; done
mv -f -- "$TEMP_DIR/db_password" "$DEPLOY_DIR/secrets/db_password"
mv -f -- "$TEMP_DIR/identity" "$DEPLOY_DIR/.deployment-identity"
{
    for key in DB_HOST DB_PORT DB_NAME DB_USER PROJECT_NAME HTTP_PORT BIND_IP STARTUP_TIMEOUT INIT_TIMEOUT; do printf '%s=%s\n' "$key" "${!key}"; done
    printf 'DB_PASSWORD_FILE=%s/secrets/db_password\n' "$DEPLOY_DIR"
} > "$DEPLOY_DIR/app.env"
compose() { docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" "$@"; }
compose config --quiet
preflight() { compose run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py "$1"; }
state=$(preflight check)
case $state in
    MISSING|EMPTY|MISSING_NO_CREATEDB)
        ((INIT_DB)) || fail 'Database is missing or empty. Restore a matching database or explicitly use --init-db to seed a new client dataset'
        [[ $state != MISSING_NO_CREATEDB ]] || fail 'Database account needs CREATEDB permission to initialize a missing database'
        printf 'Initializing NEW database %s with the release client demonstration dataset. This can take several minutes.\n' "$DB_NAME"
        printf 'Initialization log: %s/initialization.log (follow with tail -f in another terminal).\n' "$DEPLOY_DIR"
        # Exact module graph, unlike the image default's empty-database branch,
        # which may install every baked module. Never pass -u on an existing DB.
        STARTED=1
        timeout --foreground "$INIT_TIMEOUT" docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" run --rm --no-deps -T odoo \
            odoo -c /etc/odoo/odoo.conf -d "$DB_NAME" -i "$INIT_MODULES" --without-demo=True --stop-after-init \
            > "$DEPLOY_DIR/initialization.log" 2>&1 || fail 'Initialization failed or timed out. Inspect initialization.log; do not delete data or retry a partial initialization blindly'
        state=$(preflight stamp)
        ;;
    READY\ *) printf 'Existing initialized database accepted; initialization and module upgrades are skipped.\n' ;;
    *) fail 'Unexpected database preflight response' ;;
esac
[[ $state == READY\ * ]] || fail 'Database initialization did not reach READY'
attachments=${state#READY }
if ((attachments > 0)); then
    printf 'Database has %s file attachments. Checking its paired filestore volume.\n' "$attachments"
    compose run --rm --no-deps -T odoo python3 -c '
import os, pathlib, psycopg2, sys
p=pathlib.Path("/run/secrets/db_password").read_text()
c=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=p,dbname=os.environ["DB_NAME"],connect_timeout=10)
with c.cursor() as q:
 q.execute("SELECT store_fname FROM ir_attachment WHERE store_fname IS NOT NULL")
 root=pathlib.Path("/var/lib/odoo/filestore")/os.environ["DB_NAME"]
 missing=sum(not (root/name).is_file() for (name,) in q.fetchall())
 if missing: print("Filestore incomplete: restore the matching attachments to the project filestore volume before retrying.",file=sys.stderr); sys.exit(1)
'
fi
STARTED=1
compose up -d --force-recreate --wait --wait-timeout "$STARTUP_TIMEOUT"
compose exec -T odoo python3 - "$REVISION" "$STARTUP_TIMEOUT" <<'PY'
import json, sys, time, urllib.request, urllib.error
revision=sys.argv[1]
def fetch(path):
    with urllib.request.urlopen('http://web' + path, timeout=20) as response:
        return json.load(response)
def verify():
    frontend=fetch('/app/version.json')
    backend=fetch('/uiux/api/version')['data']
    if frontend.get('source_revision') != revision or backend.get('source_revision') != revision:
        raise ValueError('frontend/backend source revision mismatch')
    if backend.get('image_profile') != 'client-stable-uiux':
        raise ValueError('backend profile mismatch')
    with urllib.request.urlopen('http://web/app/',timeout=20) as r:
        if b'<html' not in r.read().lower(): raise ValueError('frontend did not serve HTML')
    try:
        fetch('/uiux/api/session/me')
        raise ValueError('anonymous session unexpectedly authenticated')
    except urllib.error.HTTPError as e:
        if e.code != 401: raise
        envelope=json.load(e)
        if envelope.get('code') != 401 or envelope.get('data') is not None:
            raise ValueError('session endpoint did not return its JSON contract')
deadline=time.monotonic()+int(sys.argv[2])
while True:
    try:
        verify()
        break
    except Exception as error:
        if time.monotonic() >= deadline:
            print('HTTP verification failed: ' + str(error), file=sys.stderr)
            sys.exit(1)
        time.sleep(3)
print('Verified frontend HTML, paired build revisions, API profile and anonymous session endpoint.')
PY
printf '\nDeployment verified: %s (%s)\n' "$RELEASE" "$REVISION"
printf 'HTTP URL: http://%s:%s/app/\n' "${BIND_IP/0.0.0.0/<APP_SERVER_IP>}" "$HTTP_PORT"
printf 'Compose: %s/compose.yml\nFilestore volume: %s_filestore\n' "$DEPLOY_DIR" "$PROJECT_NAME"
printf 'HTTP only. Configure a trusted HTTPS entry point before exposing real credentials.\n'
printf 'Browser login, business journeys and real-server firewall checks remain part of deployment acceptance.\n'
