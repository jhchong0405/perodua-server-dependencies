#!/usr/bin/env bash
# RP adapter: the RP integration gateway's command line, run from a pinned image.
# It reads the Perodua Oracle EBS database directly and read-only (health check,
# survey of the objects the integrations use, exports), and can also check the
# ISS-Oracle API. install sets it up on this server; the other commands use it.
# Every use is one short-lived container; nothing keeps running.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077

RELEASE=rp-adapter-v0.1.0
# Published to the private registry only. The image holds no settings or secrets;
# python-oracledb runs in thin mode, so the server needs no Oracle client.
IMAGE=perodua-deploy.novutal.com/rp-adapter:v0.1.0@sha256:6c5fc88ac8f39d1075191eb7f20b8c057eebcc57a77fa89123c79edc93513cf1
REGISTRY=${IMAGE%%/*}
IMAGE_UID=10001           # the image's unprivileged user
DEPLOY_DIR=/opt/perodua-rp-adapter
SETTINGS=(DB_HOST DB_PORT DB_SERVICE DB_USER CALL_TIMEOUT API_URL DOCKER_NETWORK)
DB_HOST='' DB_PORT=1521 DB_SERVICE='' DB_USER='' CALL_TIMEOUT=120 API_URL='' DOCKER_NETWORK=host
# Read from --config and used by name through read_secret's indirection.
# shellcheck disable=SC2034
DB_PASSWORD_FILE='' API_KEY_FILE=''
COMMAND='' CONFIG='' NON_INTERACTIVE=0 CONFIRM='' PURGE=0
TEMP_DIR='' AUTH_DIR=''
ARGS=()                   # passed on to the adapter
IDENTITY=.rp-adapter      # DIR=, written by the first install; uninstall needs it

usage() {
    cat <<'HELP'
Usage: sudo bash rp-adapter.sh install [--config PATH] [--non-interactive] [--dir PATH]
       sudo bash rp-adapter.sh health [--deep] [--json] [--dir PATH]
       sudo bash rp-adapter.sh survey [--exact] [--rows N] [--datasets A,B] [--dir PATH]
       sudo bash rp-adapter.sh export (--all | DATASET...) [--format csv|jsonl] [--max-rows N] [--dir PATH]
       sudo bash rp-adapter.sh api-health [--deep] [--json] [--dir PATH]
       sudo bash rp-adapter.sh run ADAPTER-COMMAND... [--dir PATH]
       sudo bash rp-adapter.sh status [--dir PATH]
       sudo bash rp-adapter.sh uninstall [--purge] [--confirm DIR] [--dir PATH]

install     Pulls the pinned image (rp-adapter-v0.1.0) from the private registry
            and sets up --dir (default /opt/perodua-rp-adapter, a new or empty
            directory the first time). The first run asks for the Oracle database
            (host, port, service name, user, password) and, optionally, the
            ISS-Oracle API's address and key. It ends with the database health
            check. Later runs reuse rp-adapter.env and the secrets; run install
            again after editing rp-adapter.env.
--config    Literal KEY=VALUE lines (see rp-adapter.env.example), never shell code.
            The first install with --non-interactive needs DB_PASSWORD_FILE there
            (and API_KEY_FILE when API_URL is set).
health      The database health check: connection, sign-in, every object the
            datasets read, every dataset. Exit 0 OK, 1 WARNING, 2 CRITICAL,
            3 UNKNOWN (settings), for cron and monitoring. --json for tools.
survey      The first look at the database: for every object its columns, Oracle
            types, row counts and sample rows; for every dataset a sample and
            profile. The report folder is under DIR/output/survey/.
export      Writes datasets to CSV or JSON Lines with a manifest (row counts,
            sha256, column types) under DIR/output/export/.
api-health  The ISS-Oracle API's health check (needs API_URL).
run         Any other adapter command, for example: run db objects,
            run db columns ISS_CUSTOMERS_V, run db size --exact,
            run db sample customers --rows 5, run db datasets, run endpoints.
status      The settings (no secrets), the image and the output folders.
uninstall   Removes the settings and the secrets install created in --dir; the
            output folder stays unless --purge, which also removes the image.
            Type the directory to confirm, or pass it with --confirm.
Every command runs the adapter read-only: it runs only its own reviewed
queries, in read-only transactions, never SQL typed at run time. Needs Docker
(install-dependencies.sh --role app) and a network path from this server to the
database port.
Exit codes: the adapter's (health: 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN; others:
0 done, 1 partly failed, 2 failed, 3 settings or usage). This script's own errors
(no setup, bad settings, a busy lock, a local step that failed) exit 3; other
codes come from Docker itself.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 3; }   # 3: UNKNOWN to a monitoring system
# Any other local step that fails (a full disk, a directory that cannot be made)
# is this script's error too, not the adapter's WARNING or CRITICAL.
trap '[[ $BASHPID != "$$" ]] || { printf "ERROR: rp-adapter.sh failed at line %s.\n" "$LINENO" >&2; exit 3; }' ERR
cleanup() {
    [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
    [[ -z $AUTH_DIR ]] || rm -rf -- "$AUTH_DIR"
    ((${#STAGED[@]} == 0)) || rm -f -- "${STAGED[@]}"   # a generation that was not published
}
trap cleanup EXIT

lock() {   # $1 shared: runs may overlap; exclusive: install and uninstall change files
    local base=/run/lock
    [[ -d $base ]] || base=/run
    exec 9>"$base/rp-adapter-$(printf '%s' "$DEPLOY_DIR" | sha256sum | cut -c1-16).lock"
    if [[ $1 == shared ]]; then
        flock -n -s 9 || die 'install or uninstall is changing this setup right now: try again when it is done'
    else
        flock -n 9 || die 'Another rp-adapter.sh command is using this setup: try again when it is done'
    fi
}
is_ours() { [[ -f $DEPLOY_DIR/$IDENTITY ]] && [[ $(sed -n 's/^DIR=//p' "$DEPLOY_DIR/$IDENTITY" | tail -n 1) == "$DEPLOY_DIR" ]]; }
OUTPUT_MARK=.rp-adapter-output   # in output/: proves that uninstall left this folder
only_results_left() {   # the directory holds nothing but the output/ an earlier setup left
    local entry
    for entry in "$DEPLOY_DIR"/* "$DEPLOY_DIR"/.[!.]* "$DEPLOY_DIR"/..?*; do
        [[ -e $entry || -L $entry ]] || continue
        [[ ${entry##*/} == output ]] || return 1
    done
    [[ -d $DEPLOY_DIR/output && ! -L $DEPLOY_DIR/output && -f $DEPLOY_DIR/output/$OUTPUT_MARK ]]
}
open_deployment() {
    if ! is_ours || [[ ! -f $DEPLOY_DIR/rp-adapter.env || ! -f $DEPLOY_DIR/adapter.conf ]]; then
        die "No rp-adapter.sh setup in $DEPLOY_DIR. Run: sudo bash rp-adapter.sh install"
    fi
    [[ ! -e $DEPLOY_DIR/$PUBLISHING ]] \
        || die 'An install was stopped while it replaced the setup. Run install again: it first puts the previous setup back.'
    read_settings "$DEPLOY_DIR/rp-adapter.env"
    check_settings
    [[ -f $DEPLOY_DIR/secrets/db_password ]] || die "The database password is missing: run install again"
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
            DB_HOST|DB_PORT|DB_SERVICE|DB_USER|CALL_TIMEOUT|API_URL|DOCKER_NETWORK|DB_PASSWORD_FILE|API_KEY_FILE)
                printf -v "$key" '%s' "${line#*=}" ;;
            *) die "$file: unknown key $key" ;;
        esac
    done < "$file"
}

valid_port() { [[ $1 =~ ^[1-9][0-9]{0,4}$ ]] && ((10#$1 <= 65535)); }

problem() {   # $1 key, $2 value: prints why the value cannot be used, nothing when it can
    local value=$2
    local host='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$' loop='^(127\.|localhost$|localhost\.)'
    local service='^[A-Za-z0-9][A-Za-z0-9_.#$-]{0,127}$'
    local user='^[A-Za-z][A-Za-z0-9_#$]{0,127}$' number='^[1-9][0-9]{0,4}$'
    local url='^https?://([^/:]+)(:([0-9]+))?/?$'
    local network='^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$'
    case $1 in
        DB_HOST)
            if [[ ! $value =~ $host ]]; then echo 'a host name or an IPv4 address'
            elif [[ $DOCKER_NETWORK != host && ${value,,} =~ $loop ]]; then
                echo "the database server's address (with DOCKER_NETWORK=$DOCKER_NETWORK, $value is the container itself)"
            fi ;;
        API_URL)
            [[ -n $value ]] || return 0
            if [[ ! $value =~ $url ]]; then echo 'empty, or http(s)://host[:port] of the ISS-Oracle API'; return 0; fi
            local api_host=${BASH_REMATCH[1]} api_port=${BASH_REMATCH[3]}
            if [[ ! $api_host =~ $host ]] || { [[ -n $api_port ]] && ! valid_port "$api_port"; }; then
                echo 'empty, or http(s)://host[:port] of the ISS-Oracle API (port 1-65535)'
            elif [[ $DOCKER_NETWORK != host && ${api_host,,} =~ $loop ]]; then
                echo "the API server's address (with DOCKER_NETWORK=$DOCKER_NETWORK, $api_host is the container itself)"
            fi ;;
        DB_PORT) valid_port "$value" || echo 'a port number, 1-65535' ;;
        DB_SERVICE) [[ $value =~ $service ]] || echo 'an Oracle service name' ;;
        DB_USER) [[ $value =~ $user ]] || echo 'an Oracle user name' ;;
        CALL_TIMEOUT) [[ $value =~ $number ]] && ((10#$value <= 86400)) || echo 'seconds, 1-86400' ;;
        DOCKER_NETWORK) [[ $value =~ $network ]] || echo "host, or the name of a Docker network" ;;
    esac
}

check_settings() {
    local key reason
    for key in "${SETTINGS[@]}"; do
        reason=$(problem "$key" "${!key}")
        [[ -z $reason ]] || die "$key=${!key} cannot be used: it must be $reason"
    done
}

ask() {   # $1 key, $2 question, $3 "optional" to accept a blank answer
    local answer reason
    while :; do
        read -r -p "$2${!1:+ [${!1}]}: " answer || die 'Cancelled'
        answer=${answer:-${!1}}
        reason=$(problem "$1" "$answer")
        if [[ -z $reason ]] && [[ -n $answer || ${3:-} == optional ]]; then break; fi
        printf 'That cannot be used: it must be %s. Try again.\n' "${reason:-given}" >&2
    done
    printf -v "$1" '%s' "$answer"
}

ask_settings() {
    printf 'RP adapter setup. The answers are saved in %s/rp-adapter.env.\n' "$DEPLOY_DIR"
    ask DB_HOST 'Oracle database server (host name or IP)'
    ask DB_PORT 'Oracle listener port'
    ask DB_SERVICE 'Oracle service name'
    ask DB_USER 'Database user (an account with SELECT grants only is safest)'
    ask API_URL 'ISS-Oracle API address, e.g. http://127.0.0.1:8000 (blank: not used)' optional
}

# ---------------------------------------------------------------- a new generation
# install stages every file it changes as FILE.new (secrets as secrets/.NAME.new)
# and replaces the files only once all of them are ready: a failure before that
# leaves the setup as it was.
STAGED=()                 # staged files, removed by cleanup unless published

stage_secret() {   # $1 file name in secrets/, $2 *_FILE setting, $3 prompt; nothing when it stays
    local name=$1 source=${!2} prompt=$3 value=''
    if [[ -n $source ]]; then
        [[ $source == /* && -f $source && -r $source ]] || die "$2 must be an absolute path to a readable file"
        value=$(<"$source")
        value=${value%$'\r'}   # a file saved on Windows
    elif [[ -f $DEPLOY_DIR/secrets/$name ]]; then
        return 0
    elif ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        die "The first install without a terminal needs $2 in --config"
    else
        while [[ -z $value ]]; do
            IFS= read -r -s -p "$prompt (hidden): " value || die 'Cancelled'
            printf '\n'
        done
    fi
    [[ -n $value && $value != *$'\n'* ]] || die "$prompt must be one non-empty line"
    # 0444 so the image's unprivileged user can read the mounted file; the
    # secrets directory itself is 0700, so on the host only root reaches it.
    STAGED+=("$DEPLOY_DIR/secrets/.$name.new")
    printf '%s' "$value" > "$DEPLOY_DIR/secrets/.$name.new"
    unset value
    chmod 0444 "$DEPLOY_DIR/secrets/.$name.new"
}

stage_file() {   # $1 path, $2 mode: stdin becomes $1.new (call it with a redirection,
    # never at the end of a pipe: a pipe would run it in a subshell and the
    # cleanup would not learn about the file)
    STAGED+=("$1.new")
    cat > "$1.new" && chmod "$2" "$1.new"
}

settings_lines() {
    local key
    for key in "${SETTINGS[@]}"; do printf '%s=%s\n' "$key" "${!key}"; done
}
adapter_conf_lines() {   # what the adapter reads inside the container: paths there, no secrets
    printf 'ISS_DB_HOST=%s\nISS_DB_PORT=%s\nISS_DB_SERVICE=%s\nISS_DB_USER=%s\n' \
        "$DB_HOST" "$DB_PORT" "$DB_SERVICE" "$DB_USER"
    printf 'ISS_DB_PASSWORD_FILE=/run/secrets/db_password\nISS_DB_CALL_TIMEOUT=%s\n' "$CALL_TIMEOUT"
    if [[ -n $API_URL ]]; then
        printf 'ISS_API_BASE_URL=%s\nISS_API_KEY_FILE=/run/secrets/api_key\n' "${API_URL%/}"
    fi
}
stage_settings() {   # made in full first: a process substitution's failure goes unnoticed
    local lines
    lines=$(settings_lines) || die 'Could not write the settings. Nothing was changed.'
    stage_file "$DEPLOY_DIR/rp-adapter.env" 0600 <<< "$lines"
    lines=$(adapter_conf_lines) || die 'Could not write the settings. Nothing was changed.'
    stage_file "$DEPLOY_DIR/adapter.conf" 0444 <<< "$lines"
}

# The files one generation consists of, and whether this install replaces them.
GENERATION=(secrets/db_password secrets/api_key adapter.conf rp-adapter.env)
staged_name() {   # $1 generation file: its staged name
    case $1 in secrets/*) printf 'secrets/.%s.new' "${1#secrets/}" ;; *) printf '%s.new' "$1" ;; esac
}

# Present while publish replaces the files. An install that was killed half-way
# leaves it: the other commands then refuse the mixed files, and the next install
# puts the previous ones (kept as .old) back before it does anything else.
PUBLISHING=.publishing

put_back() {   # the previous generation from its .old copies; fails if any step did
    local file failed=0
    # Copies, not moves: the .old files stay until the marker is gone, so a
    # put_back that is itself stopped can simply run again.
    for file in "${GENERATION[@]}"; do
        if [[ -f $DEPLOY_DIR/$file.old ]]; then
            cp -p -- "$DEPLOY_DIR/$file.old" "$DEPLOY_DIR/$file" || failed=1
        else
            rm -f -- "$DEPLOY_DIR/$file" || failed=1   # it did not exist before
        fi
    done
    ((failed == 0)) || die "Putting the previous setup back failed: check $DEPLOY_DIR (copies end in .old), then run install again"
    rm -f -- "$DEPLOY_DIR/$PUBLISHING"
    for file in "${GENERATION[@]}"; do rm -f -- "$DEPLOY_DIR/$file.old"; done
}
discard_staged() {   # what a stopped install staged: never part of the next one
    local file
    for file in "${GENERATION[@]}"; do rm -f -- "$DEPLOY_DIR/$(staged_name "$file")"; done
}

publish() {   # every staged file into place; on any failure, the previous files come back
    local file failed=0
    for file in "${GENERATION[@]}"; do   # keep the previous generation to undo
        rm -f -- "$DEPLOY_DIR/$file.old"
        [[ ! -f $DEPLOY_DIR/$file ]] || cp -p -- "$DEPLOY_DIR/$file" "$DEPLOY_DIR/$file.old" \
            || die 'Could not keep a copy of the current setup. Nothing was changed.'
    done
    # Only now, with every copy complete: a .old copy exists exactly when the file did.
    : > "$DEPLOY_DIR/$PUBLISHING" || die 'Could not write to the setup. Nothing was changed.'
    for file in "${GENERATION[@]}"; do
        if [[ -f $DEPLOY_DIR/$(staged_name "$file") ]]; then
            mv -f -- "$DEPLOY_DIR/$(staged_name "$file")" "$DEPLOY_DIR/$file" || { failed=1; break; }
        elif [[ $file == secrets/api_key && -z $API_URL ]]; then
            rm -f -- "$DEPLOY_DIR/$file" || { failed=1; break; }
        fi
    done
    if ((failed)); then
        put_back
        die 'Replacing the setup failed; the previous one was put back. Run install again.'
    fi
    rm -f -- "$DEPLOY_DIR/$PUBLISHING"   # the new generation is complete
    for file in "${GENERATION[@]}"; do rm -f -- "$DEPLOY_DIR/$file.old"; done
    STAGED=()
}

# ---------------------------------------------------------------- image
auth_error() { grep -Eiq 'unauthorized|authentication required|denied|forbidden|403|401|no basic auth' "$1"; }
pull_image() {   # asks for the registry account only when the registry wants one
    local username token attempts=0 endpoint name='^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$'
    if docker image inspect "$IMAGE" > /dev/null 2>&1; then
        return 0
    fi
    printf 'Pulling pinned image: %s...\n' "${IMAGE%@*}" >&2
    while ! docker pull "$IMAGE" > "$TEMP_DIR/pull.log" 2>&1; do
        if ! auth_error "$TEMP_DIR/pull.log"; then
            grep -Eiq 'manifest unknown|not found|manifest invalid' "$TEMP_DIR/pull.log" \
                && die "The pinned image was not found in the registry $REGISTRY"
            die "Pulling from $REGISTRY failed (network, registry or Docker error): $(tail -n 1 "$TEMP_DIR/pull.log")"
        fi
        ((NON_INTERACTIVE == 0)) || die "The registry needs a login: run docker login $REGISTRY first"
        ((attempts < 3)) || die 'The registry login failed three times'
        [[ -t 0 ]] || die "The registry login needs a terminal (or run docker login $REGISTRY first)"
        printf 'The registry %s requires a login. Use the deployment account issued for it.\n' "$REGISTRY" >&2
        read -r -p 'Registry username (blank cancels): ' username || die 'Cancelled'
        [[ -n $username ]] || die 'Cancelled'
        [[ $username =~ $name ]] || die 'Invalid registry username'
        IFS= read -r -s -p 'Registry password (hidden; blank cancels): ' token || die 'Cancelled'
        printf '\n' >&2
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
    printf 'Pulled pinned image: %s\n' "${IMAGE%@*}" >&2
}

# ---------------------------------------------------------------- running the adapter
adapter() {   # runs the pinned image once, with the settings, secrets and output mounted
    local -a mounts=(
        --mount "type=bind,src=$DEPLOY_DIR/adapter.conf,dst=/config/adapter.conf,readonly"
        --mount "type=bind,src=$DEPLOY_DIR/secrets/db_password,dst=/run/secrets/db_password,readonly"
        --mount "type=bind,src=$DEPLOY_DIR/output,dst=/output")
    [[ ! -f $DEPLOY_DIR/secrets/api_key ]] \
        || mounts+=(--mount "type=bind,src=$DEPLOY_DIR/secrets/api_key,dst=/run/secrets/api_key,readonly")
    docker run --rm --network "$DOCKER_NETWORK" --read-only --tmpfs /tmp --cap-drop ALL \
        --security-opt no-new-privileges:true --user "$IMAGE_UID:$IMAGE_UID" \
        --label com.perodua.rp-adapter=1 "${mounts[@]}" "$IMAGE" "$@" --config /config/adapter.conf
}

own_arguments() {   # $@: options this script sets itself and the adapter must not get twice
    local arg own
    for arg in "${ARGS[@]}"; do
        for own in "$@"; do
            [[ $arg != "$own" && $arg != "$own="* ]] || die "$own is set by rp-adapter.sh; do not pass it"
        done
    done
}

folders() {   # $1 directory, $2 levels below it (1 or 2): the folders there, sorted
    local entry
    local -a found
    if (($2 == 1)); then found=("$1"/*/); else found=("$1"/*/*/); fi
    for entry in "${found[@]}"; do
        if [[ -d $entry ]]; then printf '%s\n' "${entry%/}"; fi
    done | sort -V
}
latest() {   # $1 folder under output/: the newest run's results (where manifest.json is)
    # output/<folder>/<run>/<results>: this script names each run by UTC time and
    # process (20260930T010203Z-p4711), the adapter its results folder inside it,
    # so a version sort of the paths puts the newest last.
    folders "$DEPLOY_DIR/output/$1" 2 | tail -n 1
}

run_adapter() {   # $1 output folder or '', then the adapter arguments; exits with its status
    local folder=$1 status=0 run results
    shift
    pull_image
    if [[ -n $folder ]]; then
        # A folder of its own for this run: runs may overlap, and each must find
        # its own results (the adapter writes one timestamped folder inside it).
        run=$DEPLOY_DIR/output/$folder/$(date -u +%Y%m%dT%H%M%SZ)-p$$
        install -d -m 0700 -o "$IMAGE_UID" -g "$IMAGE_UID" "$DEPLOY_DIR/output/$folder" "$run"
        adapter "$@" --out "/output/$folder/${run##*/}" || status=$?
        results=$(folders "$run" 1 | tail -n 1)
        printf 'Results on this server: %s\n' "${results:-$run (empty)}" >&2
    else
        adapter "$@" || status=$?
    fi
    exit "$status"
}

# ---------------------------------------------------------------- commands
cmd_install() {
    local status=0
    lock exclusive
    # A new or empty directory, one set up by this script, or one that uninstall
    # left with only its results (output/, with its mark) in it.
    if ! is_ours && [[ -e $DEPLOY_DIR ]] && { [[ ! -d $DEPLOY_DIR ]] || ! only_results_left; }; then
        die "$DEPLOY_DIR is not empty and not a setup of this script: use a new or empty directory"
    fi
    if is_ours; then
        # publish moves whatever is staged, so nothing an earlier install left may stay
        discard_staged
        if [[ -e $DEPLOY_DIR/$PUBLISHING ]]; then
            put_back
            printf 'An earlier install was stopped while it replaced the setup; the previous setup is back.\n' >&2
        fi
    fi
    if [[ -n $CONFIG ]]; then
        read_settings "$CONFIG"
    elif [[ -f $DEPLOY_DIR/rp-adapter.env ]]; then
        read_settings "$DEPLOY_DIR/rp-adapter.env"
    elif ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        die 'No settings yet: run it on a terminal to answer the setup questions, or pass --config'
    else
        ask_settings
    fi
    check_settings
    install -d -m 0700 "$DEPLOY_DIR" "$DEPLOY_DIR/secrets"
    # Claimed before anything else is written, so an install that fails later
    # (a registry login, a pull) can be run again or uninstalled.
    is_ours || printf 'DIR=%s\n' "$DEPLOY_DIR" > "$DEPLOY_DIR/$IDENTITY"
    # Everything new is staged first and replaces the setup in one go below.
    stage_secret db_password DB_PASSWORD_FILE 'Database password'
    [[ -z $API_URL ]] || stage_secret api_key API_KEY_FILE 'ISS-Oracle API key'
    stage_settings
    pull_image
    install -d -m 0700 -o "$IMAGE_UID" -g "$IMAGE_UID" "$DEPLOY_DIR/output"
    [[ -f $DEPLOY_DIR/output/$OUTPUT_MARK ]] || printf 'Results of rp-adapter.sh in %s\n' "$DEPLOY_DIR" \
        > "$DEPLOY_DIR/output/$OUTPUT_MARK"
    publish
    printf 'Checking the database from the container (%s)\n' "$RELEASE"
    adapter db health || status=$?
    case $status in
        0) printf '\nSet up and verified: %s\n' "$RELEASE" ;;
        1) printf '\nSet up: %s. The database answers, but some objects or datasets failed (above):\n' "$RELEASE"
           printf 'usually a missing grant. Fix it, then: sudo bash rp-adapter.sh health\n' ;;
        *) die "The adapter is set up but cannot use the database (above). Fix the cause, then run install again: the settings are kept in $DEPLOY_DIR/rp-adapter.env" ;;
    esac
    if [[ -n $API_URL ]]; then
        printf '\nChecking the ISS-Oracle API at %s\n' "$API_URL"
        adapter health --scope connection \
            || printf 'The API check failed (above); the database side is usable. Fix it, then: sudo bash rp-adapter.sh api-health\n'
    fi
    printf 'Settings: %s/rp-adapter.env\nResults:  %s/output/\n' "$DEPLOY_DIR" "$DEPLOY_DIR"
    printf 'Next:     sudo bash rp-adapter.sh survey\n'
}

cmd_status() {
    local folder
    open_deployment
    printf 'Release:  %s\nImage:    %s (%s)\n' "$RELEASE" "${IMAGE%@*}" \
        "$(docker image inspect "$IMAGE" > /dev/null 2>&1 && echo 'on this server' || echo 'not pulled yet')"
    printf 'Database: %s@%s:%s/%s (call timeout %ss, network %s)\n' "$DB_USER" "$DB_HOST" "$DB_PORT" \
        "$DB_SERVICE" "$CALL_TIMEOUT" "$DOCKER_NETWORK"
    printf 'API:      %s\n' "${API_URL:-not used}"
    printf 'Secrets:  database password%s\n' "$([[ -f $DEPLOY_DIR/secrets/api_key ]] && echo ', API key')"
    printf 'Settings: %s/rp-adapter.env\n' "$DEPLOY_DIR"
    for folder in survey export; do
        printf 'Latest %-7s %s\n' "$folder:" "$(latest "$folder" || true)"
    done
}

cmd_uninstall() {
    lock exclusive
    is_ours || die "$DEPLOY_DIR is not a setup of this script (no $IDENTITY). Nothing was changed."
    printf 'This removes the settings and the secrets in %s%s.\n' "$DEPLOY_DIR" \
        "$( ((PURGE)) && printf ', the output folder with its reports and exports, and the image' \
            || printf '; the output folder stays')"
    if [[ -z $CONFIRM ]]; then
        [[ -t 0 ]] || die "No terminal to confirm on: pass --confirm $DEPLOY_DIR. Nothing was changed."
        read -r -p "Type the directory ($DEPLOY_DIR) to confirm: " CONFIRM || die 'Cancelled'
    fi
    [[ $CONFIRM == "$DEPLOY_DIR" ]] || die 'Not confirmed. Nothing was changed.'
    # exactly the files install creates, with the staged and kept copies a crash
    # in the middle of an install could leave
    local file
    for file in "${GENERATION[@]}"; do
        rm -f -- "${DEPLOY_DIR:?}/$file" "$DEPLOY_DIR/$file.old" "$DEPLOY_DIR/$(staged_name "$file")"
    done
    rm -f -- "$DEPLOY_DIR/${PUBLISHING:?}" "$DEPLOY_DIR/${IDENTITY:?}"
    rmdir -- "$DEPLOY_DIR/secrets" 2> /dev/null || true
    if ((PURGE)); then
        rm -rf -- "${DEPLOY_DIR:?}/output"
        docker image rm "$IMAGE" > /dev/null 2>&1 || true
    fi
    if rmdir -- "$DEPLOY_DIR" 2> /dev/null; then
        printf 'Removed %s.\n' "$DEPLOY_DIR"
    elif [[ -d $DEPLOY_DIR/output ]]; then
        printf 'Removed the settings and secrets. The results stay in %s/output.\n' "$DEPLOY_DIR"
    else
        printf 'Removed the settings and secrets. %s stays: it holds files install did not create.\n' "$DEPLOY_DIR"
    fi
}

COMMAND=${1:-}
[[ -n $COMMAND ]] || { usage >&2; exit 3; }
shift
case $COMMAND in -h|--help|help) usage; exit 0 ;; esac
while (($#)); do
    case $1 in
        --dir)
            (($# >= 2)) || die "Missing value for $1"
            DEPLOY_DIR=$2; shift 2 ;;
        --config|--confirm)
            [[ $COMMAND == install || $COMMAND == uninstall ]] || { ARGS+=("$1"); shift; continue; }
            (($# >= 2)) || die "Missing value for $1"
            case $1 in --config) CONFIG=$2 ;; --confirm) CONFIRM=$2 ;; esac
            shift 2 ;;
        --non-interactive) [[ $COMMAND == install ]] || die '--non-interactive belongs to install'; NON_INTERACTIVE=1; shift ;;
        --purge) [[ $COMMAND == uninstall ]] || die '--purge belongs to uninstall'; PURGE=1; shift ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
[[ -z $CONFIG || $COMMAND == install ]] || die '--config belongs to install'
[[ -z $CONFIRM || $COMMAND == uninstall ]] || die '--confirm belongs to uninstall'
case $COMMAND in
    install|status|uninstall) ((${#ARGS[@]} == 0)) || die "Unknown argument for $COMMAND: ${ARGS[0]} (see: bash rp-adapter.sh --help)" ;;
esac
[[ $DEPLOY_DIR == /* ]] || die '--dir must be an absolute path'
[[ ! -L ${DEPLOY_DIR%/} ]] || die '--dir must not be a symbolic link'
DEPLOY_DIR=$(realpath -m -- "$DEPLOY_DIR")
[[ $DEPLOY_DIR != / ]] || die '--dir cannot be /'
# Docker mount options and the identity file are comma- and line-separated.
[[ $DEPLOY_DIR =~ ^/[A-Za-z0-9._/-]+$ ]] || die '--dir may hold only letters, digits, ., _, - and /'
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'
command -v docker > /dev/null || die 'Docker is required: sudo bash install-dependencies.sh --role app'
TEMP_DIR=$(mktemp -d)

case $COMMAND in
    install|uninstall) ;;
    *) lock shared ;;   # runs may overlap each other, never an install or uninstall
esac
case $COMMAND in
    install) cmd_install ;;
    status) cmd_status ;;
    uninstall) cmd_uninstall ;;
    health) open_deployment; own_arguments --config; run_adapter '' db health "${ARGS[@]}" ;;
    survey) open_deployment; own_arguments --config --out; run_adapter survey db survey "${ARGS[@]}" ;;
    export)
        open_deployment; own_arguments --config --out
        ((${#ARGS[@]})) || die 'Name the datasets to export, or --all (list them: sudo bash rp-adapter.sh run db datasets)'
        run_adapter export db export "${ARGS[@]}" ;;
    api-health)
        open_deployment; own_arguments --config
        [[ -n $API_URL ]] || die 'No API_URL in rp-adapter.env: the ISS-Oracle API is not set up for this adapter'
        run_adapter '' health "${ARGS[@]}" ;;
    run)
        open_deployment; own_arguments --config
        ((${#ARGS[@]})) || die 'Give the adapter command, for example: run db objects'
        run_adapter '' "${ARGS[@]}" ;;
    *) usage >&2; exit 3 ;;
esac
