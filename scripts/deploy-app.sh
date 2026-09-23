#!/usr/bin/env bash
# App-only Client Stable UIUX deployment. Database service stays on its own host.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077

RELEASE=client-stable-uiux-v1.0.0
REVISION=ec7c22386204371975ad79931f57c8091c07935d
# Same content digests as the GHCR release; the private registry now serves them.
ODOO_IMAGE=perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-v1.0.0@sha256:c16053940627c1c939a742327c2426b83355bf388a5fd2796894cb21770870da
WEB_IMAGE=perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-web-v1.0.0@sha256:6f7f7bc3700c6506ab43a9505940538893d75ef4a396e0cc8077f37106dcbdff
REGISTRY=${ODOO_IMAGE%%/*}
# --init-db: a fresh UAT database, the release graph without the client
# demonstration dataset. Before anything is written, uat_guard.py checks the
# pinned image: nothing Odoo could install may depend on EXCLUDED_MODULE or
# refer to it; after installation the database itself is checked as well.
INIT_MODULES=perodua_client_stable,perodua_gateway,perodua_forecast_workbook,perodua_supplier_execution,perodua_uiux_api
EXCLUDED_MODULE=perodua_demo_client
# A restored release database (or one seeded by earlier versions of this
# script) is still held to exactly the module set it was always checked for.
RESTORED_MODULES=perodua_client_stable,perodua_demo_client,perodua_gateway,perodua_forecast_workbook,perodua_supplier_execution,perodua_uiux_api
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
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
--init-db initializes a NEW or EMPTY database as a fresh UAT system: the release
modules without the client demonstration dataset (perodua_demo_client), without
Odoo demo data, and with UAT-only administrators whadmin, admin1 and admin2, all
with the fixed password "perodua". Prepare the empty database on the DB server
with deploy-db.sh DB_MODE=empty. It never reinitializes or upgrades an
initialized database.
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
    prompt HTTP_PORT 'Web port that browsers open on this server'
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
for helper in uat_guard.py uat_admins.py; do
    [[ -f $SCRIPT_DIR/$helper && ! -L $SCRIPT_DIR/$helper ]] || fail "$helper is missing next to deploy-app.sh; run it from the complete release directory"
done
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
                fail "The pinned image was not found in the registry $REGISTRY. Check the release with the publisher"
            fi
            fail "Pull from the registry $REGISTRY failed due to network, registry, or Docker error. Check connectivity and available disk space"
        fi
        ((NON_INTERACTIVE == 0)) || fail "Registry authentication is required. Login with docker login $REGISTRY first, then retry"
        ((attempts < 3)) || fail 'Registry authentication failed after three attempts'
        [[ -t 0 ]] || fail 'Registry authentication requires a terminal'
        printf 'The registry %s requires authentication. Use the deployment account issued for it.\n' "$REGISTRY"
        read -r -p 'Registry username (blank cancels): ' username || fail 'Login cancelled'
        [[ -n $username ]] || fail 'Login cancelled'
        [[ $username =~ ^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$ ]] || fail 'Invalid registry username'
        read -r -s -p 'Registry password (hidden; blank cancels): ' token || fail 'Login cancelled'
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
        if ! printf '%s' "$token" | docker login "$REGISTRY" --username "$username" --password-stdin > "$TEMP_DIR/login.log" 2>&1; then
            printf 'Login failed. Check the username, password and the connection to %s.\n' "$REGISTRY" >&2
        fi
        unset token
    done
    printf 'Pulled pinned image: %s\n' "${image%@*}"
}
pull_image "$ODOO_IMAGE"
pull_image "$WEB_IMAGE"

cat > "$TEMP_DIR/preflight.py" <<'PY'
# Modes: check | mark-pending | verify-fresh | stamp. States printed by check:
#   MISSING / MISSING_NO_CREATEDB / EMPTY     nothing initialized yet
#   SETUP_PENDING                             a fresh UAT initialization whose
#                                             administrators are not set up yet
#   SETUP_UNMARKED                            the same, but the run stopped before
#                                             it could even record that
#   READY <attachments>                       initialized and stamped
# perodua.uat_init records the fresh path: 'pending' after modules install,
# 'complete' once stamped. Databases without it are restored/legacy databases
# and are held to RESTORED_MODULES exactly as before.
import ast, hashlib, os, pathlib, sys
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
      RESTORED_MODULES: "$RESTORED_MODULES"
      EXCLUDED_MODULE: "$EXCLUDED_MODULE"
    command: ["odoo", "-c", "/etc/odoo/odoo.conf", "-d", "$DB_NAME", "--proxy-mode", "--workers=0"]
    secrets: [db_password]
    volumes:
      - filestore:/var/lib/odoo
      - ./preflight.py:/opt/deploy/preflight.py:ro
      - ./uat_guard.py:/opt/deploy/uat_guard.py:ro
      - ./uat_admins.py:/opt/deploy/uat_admins.py:ro
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
# The UAT helpers ship next to this script and run with the image's Python.
for helper in uat_guard.py uat_admins.py; do install -m 0444 -- "$SCRIPT_DIR/$helper" "$TEMP_DIR/$helper"; done
install -d -m 0700 "$DEPLOY_DIR/secrets"
for file in compose.yml preflight.py uat_guard.py uat_admins.py; do mv -f -- "$TEMP_DIR/$file" "$DEPLOY_DIR/$file"; done
mv -f -- "$TEMP_DIR/db_password" "$DEPLOY_DIR/secrets/db_password"
mv -f -- "$TEMP_DIR/identity" "$DEPLOY_DIR/.deployment-identity"
{
    for key in DB_HOST DB_PORT DB_NAME DB_USER PROJECT_NAME HTTP_PORT BIND_IP STARTUP_TIMEOUT INIT_TIMEOUT; do printf '%s=%s\n' "$key" "${!key}"; done
    printf 'DB_PASSWORD_FILE=%s/secrets/db_password\n' "$DEPLOY_DIR"
} > "$DEPLOY_DIR/app.env"
compose() { docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" "$@"; }
compose config --quiet
preflight() { compose run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py "$1"; }
uat_helper() { compose run --rm --no-deps -T odoo python3 /opt/deploy/uat_guard.py "$@"; }
uat_setup() {
    # Reached only from a fresh initialization or its interrupted resumption,
    # never from an ordinary redeploy, so UAT passwords are not reset later.
    printf 'Setting up the UAT administrators whadmin, admin1 and admin2 through the Odoo ORM...\n'
    # `odoo shell` runs as a non-odoo command so that its options follow the
    # subcommand; the entrypoint would put them before it. The variables are
    # expanded inside the container from its own environment, and the password
    # travels as PGPASSWORD (Odoo's environment name for --db_password), never
    # on a command line that ps on the host would show.
    # shellcheck disable=SC2016
    timeout --foreground "$INIT_TIMEOUT" docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" run --rm --no-deps -T odoo \
        bash -c 'PGPASSWORD="$DB_PASSWORD" exec odoo shell -c /etc/odoo/odoo.conf -d "$DB_NAME" --db_host="$DB_HOST" --db_port="$DB_PORT" --db_user="$DB_USER" < /opt/deploy/uat_admins.py' \
        > "$DEPLOY_DIR/uat-setup.log" 2>&1 || fail "UAT administrator setup failed; see $DEPLOY_DIR/uat-setup.log. Rerun with --init-db to retry it; modules are not reinstalled"
    grep -F 'UAT admins: ready:' "$DEPLOY_DIR/uat-setup.log" || fail "UAT administrator setup did not confirm completion; see $DEPLOY_DIR/uat-setup.log"
    state=$(preflight verify-fresh)
    [[ $state == VERIFIED\ * ]] || fail 'Fresh UAT database failed its post-initialization checks'
}
state=$(preflight check)
FRESH=0
case $state in
    MISSING|EMPTY|MISSING_NO_CREATEDB)
        ((INIT_DB)) || fail 'Database is missing or empty. Restore a matching database, or create an empty one with deploy-db.sh DB_MODE=empty and rerun with --init-db'
        [[ $state != MISSING_NO_CREATEDB ]] || fail "Database $DB_NAME does not exist and $DB_USER may not create databases (by design). On the DB server run deploy-db.sh with DB_MODE=empty to create it, then rerun with --init-db"
        printf 'Initializing NEW UAT database %s without %s and without Odoo demo data.\n' "$DB_NAME" "$EXCLUDED_MODULE"
        printf 'Checking the pinned image before anything is written to the database...\n'
        if ! uat_helper guard --modules "$INIT_MODULES" --exclude "$EXCLUDED_MODULE" --odoo-config /etc/odoo/odoo.conf --json \
                > "$DEPLOY_DIR/uat-guard.json" 2> "$DEPLOY_DIR/uat-guard.log"; then
            cat "$DEPLOY_DIR/uat-guard.log" >&2
            fail "Refused before any database change: the pinned image cannot keep $EXCLUDED_MODULE out safely. Report: $DEPLOY_DIR/uat-guard.json"
        fi
        cat "$DEPLOY_DIR/uat-guard.log"
        demo_flag=$(uat_helper demo-flag --odoo-config /etc/odoo/odoo.conf 2>> "$DEPLOY_DIR/uat-guard.log") \
            || fail "Cannot determine how this image's Odoo disables demo data; see $DEPLOY_DIR/uat-guard.log"
        [[ $demo_flag =~ ^--[a-z-]+=[A-Za-z0-9]+$ ]] || fail 'Unexpected demo-data option reported by the image'
        printf 'Odoo demo data disabled with %s, verified against this image. This can take several minutes.\n' "$demo_flag"
        printf 'Initialization log: %s/initialization.log (follow with tail -f in another terminal).\n' "$DEPLOY_DIR"
        # Exact module graph, unlike the image default's empty-database branch,
        # which may install every baked module. Never pass -u on an existing DB.
        STARTED=1
        timeout --foreground "$INIT_TIMEOUT" docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" run --rm --no-deps -T odoo \
            odoo -c /etc/odoo/odoo.conf -d "$DB_NAME" -i "$INIT_MODULES" "$demo_flag" --stop-after-init \
            > "$DEPLOY_DIR/initialization.log" 2>&1 || fail 'Initialization failed or timed out; inspect initialization.log. Rerun with --init-db: it finishes the setup if every module was installed, and otherwise stops with the recovery steps in docs/DEPLOYMENT.md'
        [[ $(preflight mark-pending) == PENDING ]] || fail 'Initialized database failed the fresh UAT checks'
        uat_setup
        FRESH=1 ;;
    SETUP_PENDING|SETUP_UNMARKED)
        ((INIT_DB)) || fail 'A fresh UAT initialization installed its modules but stopped before its administrators were set up. Rerun with --init-db to finish it; modules are not reinstalled'
        printf 'Finishing the interrupted UAT setup of %s; modules are not reinstalled.\n' "$DB_NAME"
        STARTED=1
        if [[ $state == SETUP_UNMARKED ]]; then
            [[ $(preflight mark-pending) == PENDING ]] || fail 'Initialized database failed the fresh UAT checks'
        fi
        uat_setup
        FRESH=1 ;;
    READY\ *) printf 'Existing initialized database accepted; initialization and module upgrades are skipped.\n' ;;
    *) fail 'Unexpected database preflight response' ;;
esac
if ((FRESH)); then
    # A fresh database is stamped READY only after the sign-in checks below pass.
    # Until then it stays SETUP_PENDING, so a rerun with --init-db checks it all
    # again instead of accepting a system nobody could sign in to.
    attachments=${state#VERIFIED }
else
    [[ $state == READY\ * ]] || fail 'Database initialization did not reach READY'
    attachments=${state#READY }
fi
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
compose exec -T odoo python3 - "$REVISION" "$STARTUP_TIMEOUT" "$FRESH" <<'PY'
import http.cookiejar, json, sys, time, urllib.request, urllib.error
revision=sys.argv[1]
fresh=sys.argv[3] == '1'
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
def verify_uat():
    # Fresh UAT only: every administrator signs in through the workbench's own
    # login, the session reports that user, and one of them loads business data.
    for login in ('whadmin', 'admin1', 'admin2'):
        opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def call(path, payload=None):
            body=None if payload is None else json.dumps(payload).encode()
            request=urllib.request.Request('http://web' + path, data=body, headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        if login == 'whadmin':
            # The endpoint must really check the password, or the rest proves nothing.
            try:
                call('/uiux/api/session/login', {'login': login, 'password': 'not-the-uat-password'})
                raise ValueError('a wrong password was accepted for ' + login)
            except urllib.error.HTTPError as e:
                if e.code != 401: raise
        try:
            signed=call('/uiux/api/session/login', {'login': login, 'password': 'perodua'})
        except urllib.error.HTTPError as e:
            raise ValueError(login + ' could not sign in (HTTP ' + str(e.code) + ')')
        if signed.get('code') != 0 or ((signed.get('data') or {}).get('user') or {}).get('username') != login:
            raise ValueError('signing in as ' + login + ' returned another user')
        me=call('/uiux/api/session/me')
        if me.get('code') != 0 or (me.get('data') or {}).get('username') != login:
            raise ValueError('session after signing in is not ' + login)
        if 'admin' not in me['data'].get('roles', []):
            raise ValueError(login + ' does not have the workbench administrator role')
        if login == 'whadmin':
            if not call('/uiux/api/menu').get('data'):
                raise ValueError('workbench menu is empty for whadmin')
            if call('/uiux/api/dashboard/summary').get('code') != 0:
                raise ValueError('dashboard summary failed for whadmin')
        call('/uiux/api/session/logout', {})
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
if fresh:
    try:
        verify_uat()
    except Exception as error:
        print('UAT verification failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
    print('Verified UAT sign-in for whadmin, admin1 and admin2, and the workbench menu and dashboard for whadmin.')
PY
if ((FRESH)); then
    state=$(preflight stamp)
    [[ $state == READY\ * ]] || fail 'Fresh UAT database could not be stamped READY'
fi
printf '\nDeployment verified: %s (%s)\n' "$RELEASE" "$REVISION"
printf 'HTTP URL: http://%s:%s/app/\n' "${BIND_IP/0.0.0.0/<APP_SERVER_IP>}" "$HTTP_PORT"
printf 'Compose: %s/compose.yml\nFilestore volume: %s_filestore\n' "$DEPLOY_DIR" "$PROJECT_NAME"
if ((FRESH)); then
    printf 'UAT administrators (UAT only, fixed password): whadmin, admin1, admin2 / perodua\n'
fi
printf 'HTTP only. Configure a trusted HTTPS entry point before exposing real credentials.\n'
printf 'Browser login, business journeys and real-server firewall checks remain part of deployment acceptance.\n'
