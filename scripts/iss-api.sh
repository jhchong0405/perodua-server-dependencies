#!/usr/bin/env bash
# ISS-Oracle API: the FastAPI service in front of the Perodua Oracle EBS database,
# run from a pinned image with Docker Compose. install deploys or upgrades it; the
# other commands operate it.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077

RELEASE=iss-oracle-api-v1.0.0
# Published to the private registry only. python-oracledb runs in thin mode, so
# the server needs no Oracle client. The image holds no settings or secrets.
IMAGE=perodua-deploy.novutal.com/iss-oracle-api:v1.0.0@sha256:5b8a32cc339217a6b6a9b35dd15b5d88253dedb74104389eeb69a3251e3c7843
REGISTRY=${IMAGE%%/*}
REPOSITORY=${IMAGE%:*@*}  # the name without the tag and the digest (a registry port stays)
DIGEST=${IMAGE##*@}
PINNED=$REPOSITORY@$DIGEST   # finds the pinned image by its digest, whatever its tag (install --no-pull)
STREAM_TAG=$REPOSITORY:${DIGEST/:/-}   # the tag the Mac streams the pin under: one for each digest, never moved
PLATFORM=linux/amd64      # the platform of the image and of the App server, for the pull on the Mac
DEPLOY_DIR=/opt/perodua-iss-api
SETTINGS=(DB_HOST DB_PORT DB_SERVICE DB_USER API_KEY_HASH PROJECT_NAME BIND_IP HTTP_PORT WORKERS)
DB_HOST='' DB_PORT=1521 DB_SERVICE='' DB_USER='' DB_PASSWORD_FILE='' API_KEY_HASH=''
PROJECT_NAME=perodua-iss-api BIND_IP=127.0.0.1 HTTP_PORT=8000 WORKERS=2
COMMAND='' CONFIG='' NON_INTERACTIVE=0 NO_PULL=0 CONFIRM='' PURGE=0 ASK_KEY=0 KEY_FILE='' FOLLOW=0
TEMP_DIR='' STAGE='' STAGED=0 AUTH_DIR='' CANDIDATE_KEY='' NEW_KEY=''
# In the deployment directory, next to the generation it runs (GENERATION):
GENERATION=(compose.yml iss-api.env secrets/db_password)
IDENTITY=.iss-api         # PROJECT_NAME and DIR, written by the first install; uninstall needs it
SNAPSHOT=.last-good.tar   # the generation that last started, to switch back to

usage() {
    cat <<'HELP'
Usage: sudo bash iss-api.sh install [--config PATH] [--non-interactive] [--no-pull] [--dir PATH]
       sudo bash iss-api.sh status|start|stop|restart [--dir PATH]
       sudo bash iss-api.sh logs [--follow] [--dir PATH]
       sudo bash iss-api.sh check [--key | --key-file FILE] [--dir PATH]
       sudo bash iss-api.sh gen-key [--dir PATH]
       sudo bash iss-api.sh uninstall [--purge] [--confirm PROJECT] [--dir PATH]

install    Pulls the pinned image (iss-oracle-api-v1.0.0) from the private registry
           and runs it from --dir (default /opt/perodua-iss-api, a new or empty
           directory the first time) with Docker Compose. The first run asks for the
           Oracle database (host, port, service name, user, password) and who may
           call the API, and prints a new API key once. Later runs reuse iss-api.env
           and secrets/db_password there; run install again after editing
           iss-api.env to apply the change. It ends with a check: database reachable
           from the container, request log writable, requests without the key
           refused and, when it issued a new key, a query with that key answered.
           If a release does not become healthy, the last one that did runs again
           with its settings, and the settings that failed are kept in
           iss-api.env.failed.
--config   Literal KEY=VALUE lines (see iss-api.env.example), never shell code.
           The first install with --non-interactive needs DB_PASSWORD_FILE there.
           Without API_KEY_HASH the deployed key stays.
--no-pull  Or KIT_NO_PULL=1 in the environment (sudo env KIT_NO_PULL=1 bash ...).
           install never pulls, never logs in to the registry and never asks
           (as --non-interactive): the pinned image must already be on this
           server, streamed from the Mac (bash scripts/stream-image.sh SSH_HOST
           iss-api.sh there). install finds it by its digest reference and
           checks that its registry digest, and on the containerd image store
           its ID, is the pinned digest; then it starts the image once, with no
           network. When it is missing, or does not start, install stops before
           any change and prints the command for the Mac. On the classic image
           store an image from docker load has no registry digest: install
           stops there too (README.md, "Install from streamed images").
status, start, stop and restart keep the settings. A stopped API stays stopped
           after a reboot until it is started again. status also shows whether
           the pinned image is on this server (Local:).
logs       The container's log; --follow keeps reading until it is stopped
           (not in a non-interactive run).
check      Runs the check again; --key asks for an API key to test a query with it,
           --key-file FILE takes it from the first line of a file instead.
gen-key    Issues a new API key for the release that runs; the old key stops
           working at once. It refuses while iss-api.env holds changes that
           install has not applied.
uninstall  Removes the container and the files install created in --dir, with the
           settings and the database password. --purge also removes the image.
           Type the project name to confirm, or pass it with --confirm.
--non-interactive, or KIT_NON_INTERACTIVE=1 in the environment: no command
           asks (README.md, "Run the kit without a person"). install needs
           --config or iss-api.env, uninstall --confirm, check --key-file.
Each deployment directory needs its own PROJECT_NAME. Needs Docker with the
Compose plugin (install-dependencies.sh --role app) and a network path from this
server to the database port. The database needs the API_REQUEST_LOG table and
read grants (README.md, ISS-Oracle API).
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# The only place that reads an answer from a person: stdin, and only when it is
# a terminal and the run is not non-interactive. Never /dev/tty, which an agent
# under ssh -t still has. $3 says how to give the answer without a person.
read_terminal() {  # [-s hidden] [-k keep spaces] VAR PROMPT HOW
    local read_options=() read_ifs=$' \t\n'
    while [[ $1 == -[sk] ]]; do
        if [[ $1 == -s ]]; then read_options+=(-s); else read_ifs=''; fi
        shift
    done
    { ((NON_INTERACTIVE == 0)) && [[ -t 0 ]]; } || die "$3"
    IFS=$read_ifs read -r "${read_options[@]}" -p "$2" "$1"
}
show_key() {
    [[ -n $NEW_KEY ]] || return 0
    printf '\nNew API key (shown only this once; store it now and give it only to the calling backend):\n  %s\n' "$NEW_KEY"
    printf 'The caller sends it in the X-API-Key header.\n'
    NEW_KEY=''
}
cleanup() {
    [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
    [[ -z $AUTH_DIR ]] || rm -rf -- "$AUTH_DIR"
    ((STAGED == 0)) || rm -rf -- "${STAGE:?}"
    show_key   # a published key is in use even after a later failure: never lose it
}
trap cleanup EXIT
compose() { docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" "$@"; }
up() { compose up --detach --wait --wait-timeout 120 --remove-orphans --pull never "$@"; }

lock_file() {   # $1: a name; the lock lives outside the deployment directory
    local base=/run/lock
    [[ -d $base ]] || base=/run
    printf '%s/iss-api-%s.lock' "$base" "$1"
}
lock() {   # taken before any deployment state is read
    exec 9>"$(lock_file "$(printf '%s' "$DEPLOY_DIR" | sha256sum | cut -c1-16)")"
    flock -n 9 || die 'Another iss-api.sh command is changing this deployment'
}
claim_project() {   # one directory per Compose project, and one command at a time on it
    local owners others
    exec 8>"$(lock_file "project-$PROJECT_NAME")"
    flock -n 8 || die "Another iss-api.sh command is changing the Compose project $PROJECT_NAME"
    owners=$(docker ps --all --filter "label=com.docker.compose.project=$PROJECT_NAME" \
        --format '{{.Label "com.docker.compose.project.working_dir"}}')
    others=$(printf '%s\n' "$owners" | sort -u | grep -vxF -e "$DEPLOY_DIR" -e '' || true)
    [[ -z $others ]] || die "The Compose project $PROJECT_NAME already runs from $others: set another PROJECT_NAME"
}

identity() {   # $1: PROJECT_NAME or DIR, as the first install recorded it
    [[ -f $DEPLOY_DIR/$IDENTITY ]] || return 0
    sed -n "s/^$1=//p" "$DEPLOY_DIR/$IDENTITY" | tail -n 1
}
is_ours() { [[ -f $DEPLOY_DIR/$IDENTITY && $(identity DIR) == "$DEPLOY_DIR" ]]; }
same_project() {
    [[ $PROJECT_NAME == "$(identity PROJECT_NAME)" ]] \
        || die "PROJECT_NAME cannot change from $(identity PROJECT_NAME) to $PROJECT_NAME: set it back, or uninstall first"
}
open_deployment() {   # an installed deployment of this script, with its settings
    if ! is_ours || [[ ! -f $DEPLOY_DIR/compose.yml || ! -f $DEPLOY_DIR/iss-api.env ]]; then
        die "No iss-api.sh deployment in $DEPLOY_DIR. Run: sudo bash iss-api.sh install"
    fi
    read_settings "$DEPLOY_DIR/iss-api.env"
    same_project
}

# ---------------------------------------------------------------- settings
read_settings() {   # literal KEY=VALUE lines, never shell code; unknown keys are refused
    local file=$1 line key
    [[ -f $file && -r $file ]] || die "Cannot read $file"
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        [[ $line == *=* ]] || die "$file: not a KEY=VALUE line: $line"
        key=${line%%=*}
        case $key in
            DB_HOST|DB_PORT|DB_SERVICE|DB_USER|DB_PASSWORD_FILE|API_KEY_HASH|PROJECT_NAME|BIND_IP|HTTP_PORT|WORKERS)
                printf -v "$key" '%s' "${line#*=}" ;;
            *) die "$file: unknown key $key" ;;
        esac
    done < "$file"
}

problem() {   # $1 key, $2 value: prints why the value cannot be used, nothing when it can
    local value=$2
    local host='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$' loop='^(127\.|localhost$|localhost\.)'
    local port='^[1-9][0-9]{0,4}$' service='^[A-Za-z0-9][A-Za-z0-9_.#$-]{0,127}$'
    local user='^[A-Za-z][A-Za-z0-9_#$]{0,127}$' project='^[a-z0-9][a-z0-9_-]{0,62}$'
    local octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])' hash='^([0-9a-fA-F]{64})?$'
    local ipv4="^($octet\\.){3}$octet\$"
    case $1 in
        DB_HOST)
            if [[ ! $value =~ $host ]]; then echo 'a host name or an IPv4 address'
            elif [[ ${value,,} =~ $loop ]]; then echo "the database server's address ($value is the container itself)"
            fi ;;
        DB_PORT|HTTP_PORT) [[ $value =~ $port ]] && ((10#$value <= 65535)) || echo 'a port number, 1-65535' ;;
        DB_SERVICE) [[ $value =~ $service ]] || echo 'an Oracle service name' ;;
        DB_USER) [[ $value =~ $user ]] || echo 'an Oracle user name' ;;
        API_KEY_HASH) [[ $value =~ $hash ]] || echo 'the SHA-256 of the key: 64 hexadecimal digits' ;;
        PROJECT_NAME) [[ $value =~ $project ]] || echo 'lower-case letters, digits, - and _' ;;
        BIND_IP) [[ $value =~ $ipv4 ]] || echo 'an IPv4 address of this server, 127.0.0.1 or 0.0.0.0' ;;
        WORKERS) [[ $value =~ $port ]] && ((10#$value <= 32)) || echo 'a number of worker processes, 1-32' ;;
    esac
}

check_settings() {
    local key reason
    for key in "${SETTINGS[@]}"; do
        reason=$(problem "$key" "${!key}")
        [[ -z $reason ]] || die "$key=${!key} cannot be used: it must be $reason"
    done
}

ask() {   # $1 key, $2 question: asks again until the answer can be used
    local answer='' reason
    while :; do
        read_terminal answer "$2${!1:+ [${!1}]}: " 'No settings yet: run it on a terminal to answer the setup questions, or pass --config' \
            || die 'Cancelled'
        answer=${answer:-${!1}}
        reason=$(problem "$1" "$answer")
        [[ -n $answer && -z $reason ]] && break
        printf 'That cannot be used: it must be %s. Try again.\n' "${reason:-given}" >&2
    done
    printf -v "$1" '%s' "$answer"
}

ask_settings() {
    local choice=''
    printf 'ISS-Oracle API setup. The answers are saved in %s/iss-api.env.\n' "$DEPLOY_DIR"
    ask DB_HOST 'Oracle database server (host name or IP)'
    ask DB_PORT 'Oracle listener port'
    ask DB_SERVICE 'Oracle service name'
    ask DB_USER 'Database user'
    printf 'Who may call the API?\n'
    printf '  1) Only this server (127.0.0.1): a reverse proxy or an SSH tunnel on this server\n'
    printf '  2) Every computer that can reach this server (0.0.0.0). Put a firewall in front first:\n'
    printf '     your cloud provider'"'"'s, because ufw does not filter ports that Docker publishes\n'
    while :; do
        read_terminal choice 'Choice [1]: ' 'BIND_IP in --config sets who may call the API' || die 'Cancelled'
        case ${choice:-1} in
            1) BIND_IP=127.0.0.1; break ;;
            2) BIND_IP=0.0.0.0; break ;;
        esac
        printf 'Enter 1 or 2.\n' >&2
    done
    ask HTTP_PORT 'API port on this server'
}

PASSWORD_WITHOUT_QUESTION='A first install without a terminal, or a non-interactive one, needs DB_PASSWORD_FILE in --config'
password_ready() {   # before any change: the password that read_password will need can be had without a question
    if [[ -n $DB_PASSWORD_FILE ]]; then
        [[ $DB_PASSWORD_FILE == /* && -f $DB_PASSWORD_FILE && -r $DB_PASSWORD_FILE ]] \
            || die 'DB_PASSWORD_FILE must be an absolute path to a readable file. Nothing was changed.'
    elif [[ ! -f $DEPLOY_DIR/secrets/db_password ]] && { ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; }; then
        die "$PASSWORD_WITHOUT_QUESTION. Nothing was changed."
    fi
}
read_password() {   # into the staged secrets/db_password, unless the deployed password stays
    local password=''
    if [[ -n $DB_PASSWORD_FILE ]]; then
        [[ $DB_PASSWORD_FILE == /* && -f $DB_PASSWORD_FILE && -r $DB_PASSWORD_FILE ]] \
            || die 'DB_PASSWORD_FILE must be an absolute path to a readable file'
        password=$(<"$DB_PASSWORD_FILE")
        password=${password%$'\r'}   # a file saved on Windows
    elif [[ -f $DEPLOY_DIR/secrets/db_password ]]; then
        return 0
    elif ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        die "$PASSWORD_WITHOUT_QUESTION"
    else
        while [[ -z $password ]]; do
            read_terminal -s -k password 'Database password (hidden): ' "$PASSWORD_WITHOUT_QUESTION" \
                || die 'Cancelled'
            printf '\n'
        done
    fi
    [[ -n $password && $password != *$'\n'* ]] || die 'The database password must be one non-empty line'
    printf '%s' "$password" > "$STAGE/secrets/db_password"
    unset password
    # The secrets directory is 0700. 0444 lets the image's unprivileged user read
    # the bind-mounted file; Docker Compose file secrets do not remap modes.
    chmod 0444 "$STAGE/secrets/db_password"
}

new_key() {   # the service keeps only the SHA-256; the key is shown once it is published
    CANDIDATE_KEY=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')
    API_KEY_HASH=$(printf '%s' "$CANDIDATE_KEY" | sha256sum | cut -d' ' -f1)
}

# ---------------------------------------------------------------- image
auth_error() { grep -Eiq 'unauthorized|authentication required|denied|forbidden|403|401|no basic auth' "$1"; }
pull_image() {   # asks for the registry account only when the registry wants one
    local username='' token='' attempts=0 endpoint name='^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$'
    if docker image inspect "$IMAGE" > /dev/null 2>&1; then
        printf 'Pinned image already on this server: %s\n' "${IMAGE%@*}"
        return 0
    fi
    printf 'Pulling pinned image: %s...\n' "${IMAGE%@*}"
    while ! docker pull "$IMAGE" > "$TEMP_DIR/pull.log" 2>&1; do
        if ! auth_error "$TEMP_DIR/pull.log"; then
            grep -Eiq 'manifest unknown|not found|manifest invalid' "$TEMP_DIR/pull.log" \
                && die "The pinned image was not found in the registry $REGISTRY"
            die "Pulling from $REGISTRY failed (network, registry or Docker error): $(tail -n 1 "$TEMP_DIR/pull.log")"
        fi
        ((NON_INTERACTIVE == 0)) || die "The registry needs a login: run docker login $REGISTRY first"
        ((attempts < 3)) || die 'The registry login failed three times'
        [[ -t 0 ]] || die 'The registry login needs a terminal'
        printf 'The registry %s requires a login. Use the deployment account issued for it.\n' "$REGISTRY"
        read_terminal username 'Registry username (blank cancels): ' 'The registry login needs a terminal' || die 'Cancelled'
        [[ -n $username ]] || die 'Cancelled'
        [[ $username =~ $name ]] || die 'Invalid registry username'
        read_terminal -s -k token 'Registry password (hidden; blank cancels): ' 'The registry login needs a terminal' || die 'Cancelled'
        printf '\n'
        [[ -n $token ]] || die 'Cancelled'
        if [[ -z $AUTH_DIR ]]; then
            # Log in to a throwaway Docker config, so the password is not kept on
            # this server. Resolve the engine first: a fresh config has no context.
            endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}')
            [[ -n ${DOCKER_HOST:-} ]] || export DOCKER_HOST=$endpoint
            AUTH_DIR=$(mktemp -d)
            export DOCKER_CONFIG=$AUTH_DIR
            unset DOCKER_CONTEXT
            printf '{}\n' > "$AUTH_DIR/config.json"
        fi
        attempts=$((attempts + 1))
        printf '%s' "$token" | docker login "$REGISTRY" --username "$username" --password-stdin > "$TEMP_DIR/login.log" 2>&1 \
            || printf 'Login failed. Check the username, the password and the connection to %s.\n' "$REGISTRY" >&2
        unset token
    done
    printf 'Pulled pinned image: %s\n' "${IMAGE%@*}"
}

# install --no-pull uses only an image that is on this server. On the containerd image
# store the ID of an image is the digest of its manifest (or of its index), checked against
# the content, and docker load keeps it; on the classic store a loaded image has no registry
# digest.
image_store() {   # containerd, classic, or nothing when docker info does not answer
    local status
    status=$(docker info --format '{{json .DriverStatus}}' 2> /dev/null) || return 0
    if [[ $status == *'["driver-type","io.containerd.snapshotter.v1"]'* ]]; then echo containerd; else echo classic; fi
}
pinned_here() {   # $1 store; 0: the pinned image is here, 1: no image has its digest, 2: one does, but not as pinned
    local facts
    facts=$(docker image inspect --format '{{.Id}}{{range .RepoDigests}} {{.}}{{end}}' "$PINNED" 2> /dev/null) || return 1
    [[ " $facts " == *" $PINNED "* ]] || return 2
    [[ $1 != containerd || ${facts%% *} == "$DIGEST" ]] || return 2
}
starts() {   # the pinned image starts a process, with no pull and no network: its layers are complete
    # sh: the command of the service in compose.yml runs it too.
    docker run --rm --pull never --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --entrypoint sh "$IMAGE" -c :
}
check_pinned_image() {   # --no-pull, before any change: no pull, no login and no question
    local store found=0 out
    store=$(image_store)
    [[ -n $store ]] || die 'The Docker engine does not answer docker info. Nothing was changed.'
    pinned_here "$store" || found=$?
    case $found in
        0) out=$(starts 2>&1) \
               || die "The pinned image on this server does not start: $(printf '%s\n' "$out" | tail -n 1). Remove it (sudo docker image rm $PINNED), stream the pinned image again, then run this command again. Nothing was changed."
           printf 'Pinned image on this server (%s image store), not pulled: %s\n' "$store" "$PINNED"
           return 0 ;;
        2) die "The image that $PINNED finds on this server does not have the pinned digest as its registry digest (and, on the containerd image store, as its ID). Remove it (sudo docker image rm $PINNED), stream the pinned image again, then run this command again. Nothing was changed." ;;
    esac
    [[ $store == containerd ]] || die "The pinned image is not on this server, and --no-pull does not pull it: $PINNED. The Docker engine of this server uses the classic image store: there an image from docker load has no registry digest, so install cannot check it against the pinned digest and does not use it. Switch the engine to the containerd image store first (README.md, \"Install from streamed images\"), then stream the image. Nothing was changed."
    # docker save of a digest reference writes no image name, and docker load then keeps a
    # dangling image that the digest reference does not find; a release tag moves to a later
    # pin: the Mac saves the stream tag, one for each digest.
    die "$(printf '%s\n' "The pinned image is not on this server, and --no-pull does not pull it: $PINNED" \
        "Stream it from the Mac (it has the registry login and the containerd image store). There, SSH_HOST is the ssh name of this server (host name $(uname -n)):" \
        "  bash scripts/stream-image.sh SSH_HOST iss-api.sh" \
        'or with Docker alone:' \
        "  docker pull --platform $PLATFORM $IMAGE && docker tag $PINNED $STREAM_TAG && docker save $STREAM_TAG | ssh SSH_HOST 'sudo -n docker load'" \
        'Then run this command again. Nothing was changed.')"
}
local_line() {   # status: the check of install --no-pull, as one line
    local store found=0
    store=$(image_store)
    if [[ -z $store ]]; then
        printf 'Local:    not checked (docker info does not answer)\n'
        return 0
    fi
    pinned_here "$store" || found=$?
    case $found in
        0) printf 'Local:    the pinned image is on this server (%s image store)\n' "$store" ;;
        1) printf 'Local:    the pinned image is not on this server: install pulls it, install --no-pull needs it streamed first\n' ;;
        *) printf 'Local:    the image that %s finds here does not have the pinned digest\n' "$PINNED" ;;
    esac
}

# ---------------------------------------------------------------- generations
# A generation (compose.yml, iss-api.env, secrets/db_password) is written to
# $STAGE, on the deployment's filesystem, checked there, and published by renames.

make_stage() {
    rm -rf -- "${STAGE:?}"
    install -d -m 0700 "$STAGE/secrets"
    STAGED=1
}

quoted() { printf '"%s"' "${1//\$/\$\$}"; }   # validated values hold no quotes; $ is literal to Compose

stage_files() {   # $1 image: the generation in $STAGE, checked by Compose there
    local image=$1 forwarded=127.0.0.1 key
    [[ -f $STAGE/secrets/db_password ]] || cp -p -- "$DEPLOY_DIR/secrets/db_password" "$STAGE/secrets/db_password"
    # With 127.0.0.1 only a local proxy can connect, so its X-Forwarded-For is
    # trusted and the request log records the real caller.
    [[ $BIND_IP != 127.0.0.1 ]] || forwarded='*'
    cat > "$STAGE/compose.yml" <<YAML
services:
  api:
    image: $image
    environment:
      DB1_HOST: $(quoted "$DB_HOST")
      DB1_PORT: "$DB_PORT"
      DB1_SERVICE: $(quoted "$DB_SERVICE")
      DB1_USER: $(quoted "$DB_USER")
      API_KEY_HASH: "${API_KEY_HASH,,}"
      WORKERS: "$WORKERS"
      FORWARDED_ALLOW_IPS: "$forwarded"
    # The application reads DB1_PASSWORD from its environment. It is set from the
    # secret when the process starts, so the compose file and docker inspect never show it.
    command: ["sh", "-c", "DB1_PASSWORD=\$\$(cat /run/secrets/db_password) && export DB1_PASSWORD && exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers \"\$\$WORKERS\""]
    secrets: [db_password]
    ports:
      - "$BIND_IP:$HTTP_PORT:8000"
    init: true
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    # A TCP check: every HTTP request, even GET /, writes a row to API_REQUEST_LOG.
    healthcheck:
      test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
      start_interval: 2s
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "5"}
    restart: unless-stopped
secrets:
  db_password:
    file: ./secrets/db_password
YAML
    for key in "${SETTINGS[@]}"; do printf '%s=%s\n' "$key" "${!key}"; done > "$STAGE/iss-api.env"
    docker compose --project-name "$PROJECT_NAME" --file "$STAGE/compose.yml" config --quiet \
        || die 'The generated compose file is not valid. Nothing was changed.'
}

publish_stage() {   # renames on one filesystem, so every file is replaced whole
    install -d -m 0700 "$DEPLOY_DIR/secrets" \
        && mv -f -- "$STAGE/secrets/db_password" "$DEPLOY_DIR/secrets/db_password" \
        && mv -f -- "$STAGE/iss-api.env" "$DEPLOY_DIR/iss-api.env" \
        && mv -f -- "$STAGE/compose.yml" "$DEPLOY_DIR/compose.yml"
}

keep_published() {   # copies of the files publish_stage will replace, to undo it
    local file
    install -d -m 0700 "$STAGE/previous/secrets"
    for file in "${GENERATION[@]}"; do
        [[ ! -f $DEPLOY_DIR/$file ]] || cp -p -- "$DEPLOY_DIR/$file" "$STAGE/previous/$file"
    done
}

undo_publication() {   # every generation file as it was before publish_stage, or gone if it was not there
    local file
    for file in "${GENERATION[@]}"; do
        if [[ -f $STAGE/previous/$file ]]; then
            mv -f -- "$STAGE/previous/$file" "$DEPLOY_DIR/$file" || return 1
        else
            rm -f -- "$DEPLOY_DIR/$file" || return 1
        fi
    done
}

same_generation() {   # $STAGE holds the generation that is deployed
    local file
    for file in "${GENERATION[@]}"; do
        cmp -s -- "$STAGE/$file" "$DEPLOY_DIR/$file" || return 1
    done
}

save_snapshot() {   # replaced whole: a failed save keeps the previous snapshot
    if ! tar -cf "$DEPLOY_DIR/$SNAPSHOT.new" -C "$DEPLOY_DIR" "${GENERATION[@]}" \
        || ! mv -f -- "$DEPLOY_DIR/$SNAPSHOT.new" "$DEPLOY_DIR/$SNAPSHOT"; then
        rm -f -- "$DEPLOY_DIR/$SNAPSHOT.new"
        die 'The release runs, but recording it as the last good one failed; the previous record is kept'
    fi
    rm -f -- "$DEPLOY_DIR/iss-api.env.failed"
}

unpack_snapshot() {   # the last good generation into a fresh $STAGE
    rm -rf -- "${STAGE:?}" && install -d -m 0700 "$STAGE" && tar -xpf "$DEPLOY_DIR/$SNAPSHOT" -C "$STAGE"
}

deploy() {   # $1 image: stage, publish and start; a release that does not start is switched back
    local -a recreate=()
    stage_files "$1"
    # A new password reaches the process only through a new container.
    if [[ ! -f $DEPLOY_DIR/secrets/db_password ]] || ! cmp -s -- "$STAGE/secrets/db_password" "$DEPLOY_DIR/secrets/db_password"; then
        recreate=(--force-recreate)
    fi
    keep_published
    if ! publish_stage; then
        if ! undo_publication; then
            NEW_KEY=$CANDIDATE_KEY   # its SHA-256 may be in the settings now
            die "Publishing the new files failed and could not be undone: check $DEPLOY_DIR"
        fi
        die 'Publishing the new files failed. Nothing was changed, and the API still runs what it ran.'
    fi
    NEW_KEY=$CANDIDATE_KEY   # published: from now on a new key is in use and must be shown
    printf 'Starting %s...\n' "$PROJECT_NAME"
    if up "${recreate[@]}"; then
        save_snapshot
        return 0
    fi
    compose logs --tail 40 api >&2 || true
    switch_back
}

switch_back() {   # after a failed start: the last good generation runs again, if it is another one
    [[ -f $DEPLOY_DIR/$SNAPSHOT ]] || die 'The API did not become healthy (log above)'
    unpack_snapshot || die "Could not read the last good release in $DEPLOY_DIR/$SNAPSHOT"
    ! same_generation || die 'The API did not become healthy (log above)'
    cp -p -- "$DEPLOY_DIR/iss-api.env" "$DEPLOY_DIR/iss-api.env.failed" || die 'Could not keep the settings that failed'
    publish_stage || die "Could not put the last good release back; it is in $DEPLOY_DIR/$SNAPSHOT"
    NEW_KEY=''   # the restored settings hold the key that worked before
    up --force-recreate \
        || die "The new release did not become healthy, and the last good one did not start either (it is in $DEPLOY_DIR/$SNAPSHOT); see: sudo bash iss-api.sh logs"
    die 'The new release did not become healthy; the last good release runs again with its settings. The settings that failed are in iss-api.env.failed (log above).'
}

owns_port() {   # the port is the one this deployment's running container publishes
    [[ -f $DEPLOY_DIR/compose.yml ]] && grep -qF -- ":$HTTP_PORT:8000\"" "$DEPLOY_DIR/compose.yml" \
        && [[ -n $(compose ps --quiet api 2> /dev/null) ]]
}

# ---------------------------------------------------------------- check
SMOKE_PY=$(cat <<'PY'
import socket, sys, time, urllib.error, urllib.request
host, port, key = sys.argv[1], int(sys.argv[2]), sys.stdin.read().strip()
try:
    socket.create_connection((host, port), 5).close()
except OSError as error:
    print(f"FAIL database {host}:{port} is not reachable from the container ({error}):"
          " open the firewall, or correct DB_HOST and DB_PORT")
    sys.exit(1)
print(f"OK   database {host}:{port} is reachable from the container")
http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
tests = [("/", "", 200), ("/customers", "", 401)]
if key:
    tests.append(("/accounting/account_type/", key, 200))
failed = 0
for path, k, want in tests:
    request = urllib.request.Request("http://127.0.0.1:8000" + path, headers={"X-API-Key": k} if k else {})
    start = time.monotonic()
    try:
        with http.open(request, timeout=60) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    except Exception as error:
        code = type(error).__name__
    ms = int((time.monotonic() - start) * 1000)
    mark = "OK  " if code == want else "FAIL"
    failed += code != want
    print(f"{mark} GET {path}{' with the API key' if k else ''} -> {code} (expected {want}, {ms} ms)")
if not key:
    print("     A query with the API key was not tested: sudo bash iss-api.sh check --key (or --key-file FILE)")
if failed:
    print("Hint: 500 means the API cannot sign in to the database, or API_REQUEST_LOG or a grant is"
          " missing; the ORA- error is in: sudo bash iss-api.sh logs. 401 with a key means a wrong key.")
sys.exit(1 if failed else 0)
PY
)

smoke_test() {   # $1: API key for a query with it (optional); it travels on stdin, never in argv
    if [[ -z $(ss -ltnH "sport = :$HTTP_PORT") ]]; then
        printf 'FAIL nothing listens on port %s of this server\n' "$HTTP_PORT"
        return 1
    fi
    printf 'OK   port %s:%s is open on this server\n' "$BIND_IP" "$HTTP_PORT"
    printf '%s' "${1:-}" | compose exec -T api python -c "$SMOKE_PY" "$DB_HOST" "$DB_PORT"
}

# ---------------------------------------------------------------- commands
cmd_install() {
    lock
    if ! is_ours && [[ -e $DEPLOY_DIR ]] && [[ ! -d $DEPLOY_DIR || -n $(ls -A -- "$DEPLOY_DIR") ]]; then
        die "$DEPLOY_DIR is not empty and not a deployment of this script: use a new or empty directory"
    fi
    if [[ -n $CONFIG ]]; then
        read_settings "$CONFIG"
        # Without a key in --config the deployed key stays: rerunning an unattended
        # install must not lock the calling system out.
        if [[ -z $API_KEY_HASH && -f $DEPLOY_DIR/iss-api.env ]]; then
            API_KEY_HASH=$(sed -n 's/^API_KEY_HASH=//p' "$DEPLOY_DIR/iss-api.env" | tail -n 1)
        fi
    elif [[ -f $DEPLOY_DIR/iss-api.env ]]; then
        read_settings "$DEPLOY_DIR/iss-api.env"
    elif ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        die 'No settings yet: run it on a terminal to answer the setup questions, or pass --config'
    else
        ask_settings
    fi
    if is_ours; then same_project; fi
    check_settings
    password_ready
    claim_project
    ((NO_PULL == 0)) || check_pinned_image
    install -d -m 0700 "$DEPLOY_DIR"
    is_ours || printf 'PROJECT_NAME=%s\nDIR=%s\n' "$PROJECT_NAME" "$DEPLOY_DIR" > "$DEPLOY_DIR/$IDENTITY"
    make_stage
    read_password
    if [[ -n $(ss -ltnH "sport = :$HTTP_PORT") ]] && ! owns_port; then
        die "Port $HTTP_PORT is already used by another program: set HTTP_PORT in the settings"
    fi
    ((NO_PULL)) || pull_image
    [[ -n $API_KEY_HASH ]] || new_key
    deploy "$IMAGE"
    printf 'Checking the deployment\n'
    smoke_test "$NEW_KEY" || die 'The API is running but the check failed: fix the cause above, then run: sudo bash iss-api.sh check'
    printf '\nDeployment verified: %s%s\n' "$RELEASE" "${NEW_KEY:+, with a query using the new key}"
    if [[ $BIND_IP == 127.0.0.1 ]]; then
        printf 'API URL: http://127.0.0.1:%s/ (this server only). From your computer: ssh -N -L %s:127.0.0.1:%s <user>@<SERVER_IP>, then open http://localhost:%s/docs\n' \
            "$HTTP_PORT" "$HTTP_PORT" "$HTTP_PORT" "$HTTP_PORT"
    else
        printf 'API URL: http://%s:%s/\n' "${BIND_IP/0.0.0.0/<SERVER_IP>}" "$HTTP_PORT"
    fi
    printf 'Settings: %s/iss-api.env\nCompose: %s/compose.yml\n' "$DEPLOY_DIR" "$DEPLOY_DIR"
    printf 'HTTP only. Put a trusted HTTPS entry point in front before callers send the API key over a network.\n'
    show_key
}

cmd_status() {
    open_deployment
    printf 'Release:  %s\nListens:  %s:%s\nDatabase: %s:%s/%s as %s\nSettings: %s/iss-api.env\n' \
        "$(sed -n 's/^    image: //p' "$DEPLOY_DIR/compose.yml")" "$BIND_IP" "$HTTP_PORT" \
        "$DB_HOST" "$DB_PORT" "$DB_SERVICE" "$DB_USER" "$DEPLOY_DIR"
    local_line
    printf '\n'
    compose ps --all
}

cmd_gen_key() {
    local image
    lock
    open_deployment
    claim_project
    [[ -f $DEPLOY_DIR/$SNAPSHOT ]] || die 'No release has started from this directory yet: run install first'
    tar -xOf "$DEPLOY_DIR/$SNAPSHOT" iss-api.env | cmp -s - "$DEPLOY_DIR/iss-api.env" \
        || die 'iss-api.env holds changes that install has not applied: run install first, or undo them'
    check_settings
    image=$(sed -n 's/^    image: //p' "$DEPLOY_DIR/compose.yml")
    [[ -n $image ]] || die "No image found in $DEPLOY_DIR/compose.yml"
    make_stage
    new_key
    deploy "$image"   # the image that runs now: a new key changes nothing else
    smoke_test "$NEW_KEY" || die 'The new key is in use, but the check failed (see above)'
    show_key
    printf 'The previous API key no longer works: give the new one to the calling system.\n'
}

cmd_uninstall() {
    local -a down=(down --remove-orphans)
    lock
    is_ours || die "$DEPLOY_DIR is not a deployment of this script (no $IDENTITY). Nothing was changed."
    PROJECT_NAME=$(identity PROJECT_NAME)
    printf 'This removes the %s container and the files install created in %s, with the settings and the database password%s.\n' \
        "$PROJECT_NAME" "$DEPLOY_DIR" "$( ((PURGE)) && printf ', and the image')"
    if [[ -z $CONFIRM ]]; then
        read_terminal CONFIRM "Type the project name ($PROJECT_NAME) to confirm: " \
            "A non-interactive run, or a run without a terminal, asks nothing: pass --confirm $PROJECT_NAME. Nothing was changed." || die 'Cancelled'
    fi
    [[ $CONFIRM == "$PROJECT_NAME" ]] || die 'Not confirmed. Nothing was changed.'
    claim_project
    ((PURGE == 0)) || down+=(--rmi all)
    if [[ -f $DEPLOY_DIR/compose.yml ]]; then
        compose "${down[@]}"
    else
        docker ps --all --quiet --filter "label=com.docker.compose.project=$PROJECT_NAME" | xargs -r docker rm --force > /dev/null
        docker network rm "${PROJECT_NAME}_default" > /dev/null 2>&1 || true
    fi
    rm -rf -- "${STAGE:?}"
    rm -f -- "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/iss-api.env" "$DEPLOY_DIR/iss-api.env.failed" \
        "$DEPLOY_DIR/secrets/db_password" "$DEPLOY_DIR/$SNAPSHOT" "$DEPLOY_DIR/$SNAPSHOT.new" "$DEPLOY_DIR/${IDENTITY:?}"
    rmdir -- "$DEPLOY_DIR/secrets" 2> /dev/null || true
    if rmdir -- "$DEPLOY_DIR" 2> /dev/null; then
        printf 'Removed %s and %s.\n' "$PROJECT_NAME" "$DEPLOY_DIR"
    else
        printf 'Removed %s. %s stays: it holds files install did not create.\n' "$PROJECT_NAME" "$DEPLOY_DIR"
    fi
}

COMMAND=${1:-}
[[ -n $COMMAND ]] || { usage >&2; exit 2; }
shift
case $COMMAND in -h|--help|help) usage; exit 0 ;; esac
while (($#)); do
    case $1 in
        --config|--confirm|--dir)
            (($# >= 2)) || die "Missing value for $1"
            case $1 in --config) CONFIG=$2 ;; --confirm) CONFIRM=$2 ;; --dir) DEPLOY_DIR=$2 ;; esac
            shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --no-pull) NO_PULL=1; shift ;;
        --purge) PURGE=1; shift ;;
        --key) ASK_KEY=1; shift ;;
        --key-file)
            (($# >= 2)) || die "Missing value for $1"
            KEY_FILE=$2; shift 2 ;;
        --follow) FOLLOW=1; shift ;;
        *) die "Unknown argument: $1 (see: bash iss-api.sh --help)" ;;
    esac
done
[[ -z $CONFIG ]] || [[ $COMMAND == install ]] || die '--config belongs to install'
[[ -z $CONFIRM && $PURGE == 0 ]] || [[ $COMMAND == uninstall ]] || die '--confirm and --purge belong to uninstall'
[[ $ASK_KEY == 0 && -z $KEY_FILE ]] || [[ $COMMAND == check ]] || die '--key and --key-file belong to check'
[[ $ASK_KEY == 0 || -z $KEY_FILE ]] || die 'Use --key or --key-file, not both'
[[ $NO_PULL == 0 || $COMMAND == install ]] || die '--no-pull belongs to install'
# --no-pull, or KIT_NO_PULL=1 in the environment (README.md, "Install from streamed
# images"): install uses only the pinned image on this server, streamed from the Mac. It
# never pulls, never logs in to the registry and never asks: no-pull is non-interactive.
case ${KIT_NO_PULL:-} in
    ''|0) ;;
    1) [[ $COMMAND != install ]] || NO_PULL=1 ;;
    *) die 'KIT_NO_PULL must be 1 (never pull: use the image streamed from the Mac) or 0 (pull as before).' ;;
esac
((NO_PULL == 0)) || NON_INTERACTIVE=1
# --non-interactive, or KIT_NON_INTERACTIVE=1 in the environment: no question
# at all (README.md, "Run the kit without a person"). Each answer comes from an
# option or a setting; a missing one stops the run before it changes anything.
case ${KIT_NON_INTERACTIVE:-} in
    ''|0) ;;
    1) NON_INTERACTIVE=1 ;;
    *) die 'KIT_NON_INTERACTIVE must be 1 (never ask) or 0 (ask on a terminal).' ;;
esac
((NON_INTERACTIVE == 0)) || export KIT_NON_INTERACTIVE=1
((ASK_KEY == 0 || NON_INTERACTIVE == 0)) || die 'check --key asks for the key, and a non-interactive run never asks: pass --key-file FILE'
# Read as a regular file only: never a terminal, never a pipe that may not end.
[[ -z $KEY_FILE ]] || [[ -f $KEY_FILE && -r $KEY_FILE ]] || die "--key-file must be a readable regular file: $KEY_FILE"
[[ $FOLLOW == 0 || $COMMAND == logs ]] || die '--follow belongs to logs'
# logs --follow reads until it is stopped: a run without a person must end by itself.
((FOLLOW == 0 || NON_INTERACTIVE == 0)) || die 'logs --follow runs until it is stopped, and a non-interactive run must end: use logs without --follow'
[[ $DEPLOY_DIR == /* ]] || die '--dir must be an absolute path'
[[ ! -L ${DEPLOY_DIR%/} ]] || die '--dir must not be a symbolic link'
DEPLOY_DIR=$(realpath -m -- "$DEPLOY_DIR")
[[ $DEPLOY_DIR != / ]] || die '--dir cannot be /'
STAGE=$DEPLOY_DIR/.stage
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'
protected_file() {  # $1 a secret file, $2 its option: root-owned, not a symlink, no group/other permissions
    [[ -f $1 && ! -L $1 && $(stat -c %u "$1") == 0 ]] || die "$2 must be a root-owned regular file, not a symlink: $1"
    (( (8#$(stat -c %a "$1") & 8#077) == 0 )) || die "$2 must have no group/other permissions (use chmod 600): $1"
}
[[ -z $KEY_FILE ]] || protected_file "$KEY_FILE" --key-file
if ! command -v docker > /dev/null || ! docker compose version > /dev/null 2>&1; then
    die 'Docker with the Compose plugin is required: sudo bash install-dependencies.sh --role app'
fi
TEMP_DIR=$(mktemp -d)

case $COMMAND in
    install) cmd_install ;;
    status) cmd_status ;;
    start) lock; open_deployment; claim_project; up; printf 'Started %s.\n' "$PROJECT_NAME" ;;
    stop) lock; open_deployment; claim_project; compose stop; printf 'Stopped %s. It stays stopped after a reboot until: sudo bash iss-api.sh start\n' "$PROJECT_NAME" ;;
    restart) lock; open_deployment; claim_project; compose restart api; up; printf 'Restarted %s.\n' "$PROJECT_NAME" ;;
    logs)
        open_deployment
        if ((FOLLOW)); then compose logs --tail 100 --follow api; else compose logs --tail 200 api; fi ;;
    check)
        open_deployment
        key=''
        if ((ASK_KEY)); then
            read_terminal -s key 'API key (hidden): ' '--key needs a terminal to ask for the key: pass --key-file FILE instead' || die 'Cancelled'
            printf '\n'
        elif [[ -n $KEY_FILE ]]; then
            key=$(head -n 1 -- "$KEY_FILE")
            key=${key%$'\r'}   # a file saved on Windows
            [[ -n $key ]] || die "--key-file $KEY_FILE holds no key on its first line"
        fi
        smoke_test "$key" ;;
    gen-key) cmd_gen_key ;;
    uninstall) cmd_uninstall ;;
    *) usage >&2; exit 2 ;;
esac
