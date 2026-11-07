#!/usr/bin/env bash
# Oracle Field Browser: the web tool that shows the Oracle EBS objects, the RP
# database and the RP-to-Oracle mapping, with a SQL console. On the App server,
# install runs it from a pinned image with Docker Compose and the other App
# commands operate it. On the DB server, role makes the RP role that the tool
# signs in with. The image holds the Oracle settings and the RP role password
# (owner decision of 2026-11-06): this kit holds no secret of the tool.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077
unset PGPASSWORD PGPASSFILE PGHOST PGHOSTADDR PGPORT PGUSER PGDATABASE PGOPTIONS \
    PGSERVICE PGSERVICEFILE PGSSLMODE || true

RELEASE=oracle-view-grey-v1.0.1
# Published to the private registry only: every account that can pull it can read the secrets in it.
IMAGE=perodua-deploy.novutal.com/oracle-view:grey-v1.0.1@sha256:8e82b47fd7050919f9f7fbd29642e20e64dda0ffa86ac45e811795249251e980
REGISTRY=${IMAGE%%/*}
REPOSITORY=${IMAGE%%[:@]*}
IMAGE_UID=10001           # the image's unprivileged user: it reads the users file
PROJECT=oracle-view       # the Compose project, its service and the container name
VOLUME=oracle-view_oracle-view-state   # mapping edits, SQL audit, login device key
PORT=8797                 # on 127.0.0.1 only; the kit route publishes it
ROUTE='stgiss.perodua.com.my   /dev/oracle-view/   8797   strip,http'
ROUTES_FILE=/etc/perodua-https/routes.conf
DEPLOY_DIR=/opt/perodua-oracle-view
FIRST_USERS=(perodua:admin pm:editor)
# The RP side is fixed by rp_targets.json in the image: host, database and role.
RP_DATABASE=perodua_oracle
RP_ROLE=oracle_view_rp
APP_IP=192.168.23.13      # the App server, as the DB server sees it
PG_PORT=5432
SETTINGS=(OVB_MEM_LIMIT OVB_PIDS_LIMIT OVB_CPUS OVB_RP_SSLMODE)
OVB_MEM_LIMIT=1g OVB_PIDS_LIMIT=256 OVB_CPUS=1.5 OVB_RP_SSLMODE=require
COMMAND='' CONFIG='' NON_INTERACTIVE=0 CONFIRM='' PURGE=0 VERIFIER_FILE='' CHECK_ONLY=0
TEMP_DIR='' STAGE='' STAGED=0 AUTH_DIR='' VERIFIER=''
GIVEN=()                  # the options on the command line, checked against the command
NEW_USERS=()              # "name (role)" of the users this install made
# In the deployment directory:
PUBLISHED=(users initial-login.txt oracle-view.env compose.yml)   # replaced whole, in this order
GENERATION=(compose.yml oracle-view.env)   # what the last good snapshot holds, to switch back
IDENTITY=.oracle-view     # DIR=, written by the first install
SNAPSHOT=.last-good.tar   # the generation that last started
STAMP=.users.sha256       # the users file the container read at its last start
INITIAL='initial-login.txt'   # the first passwords, root only, never printed

usage() {
    cat <<HELP
Usage, on the App server:
       sudo bash oracle-view.sh install [--config PATH] [--non-interactive] [--dir PATH]
       sudo bash oracle-view.sh status [--dir PATH]
       sudo bash oracle-view.sh uninstall [--purge] [--confirm oracle-view] [--dir PATH]
Usage, on the DB server:
       sudo bash oracle-view.sh role --verifier-file FILE [--app-ip ADDRESS] [--pg-port PORT]
       sudo bash oracle-view.sh role --check [--app-ip ADDRESS] [--pg-port PORT]
       sudo bash oracle-view.sh drop-role [--confirm oracle_view_rp] [--pg-port PORT]

install    Pulls the pinned image ($RELEASE) from the private registry
           and runs it from --dir (default /opt/perodua-oracle-view, a new or empty
           directory the first time) with Docker Compose, on 127.0.0.1:8797 only.
           It makes the users perodua (admin) and pm (editor) when the users file
           does not hold them, each with a random password that goes once to
           DIR/initial-login.txt (root only) and is never printed. Later runs keep
           the users, the settings and the state volume, and make the container
           again when the users file changed. If the new release does not become
           healthy, the last one that did runs again with its settings.
--config   Literal KEY=VALUE lines (see oracle-view.env.example), never shell code,
           over the saved settings. Without settings the defaults apply.
status     The image, the settings, the users (no password hashes), the state
           volume and the health of the container.
uninstall  Removes the container and the files install made in --dir. The state
           volume (mapping edits, SQL audit), the users file and the image stay;
           --purge removes them too. Type the project name to confirm, or pass it
           with --confirm.
role       Creates or updates the role oracle_view_rp on the database perodua_oracle
           with the SCRAM-SHA-256 verifier in --verifier-file (the image build
           wrote it; never the password), and its grants: read and write on the
           tables, read-only by default, no DDL on the Odoo tables. Then it checks
           the rights, shows SHOW ssl and whether dblink or postgres_fdw is
           installed, and checks pg_hba.conf for the App server (--app-ip, default
           192.168.23.13). It never edits pg_hba.conf: it prints the line to add.
           --check: the checks only, after a pg_hba.conf change.
drop-role  Ends the sessions of oracle_view_rp and drops the role and its grants.
           Rows that the SQL console changed stay changed. Type the role name to
           confirm, or pass it with --confirm.
Exit codes: 0 done; 1 error; 2 usage; role: 3 pg_hba.conf needs the printed line,
4 pg_hba.conf needs a check by hand (a line the check cannot read, or an error).
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s (see: bash oracle-view.sh --help)\n' "$*" >&2; exit 2; }
cleanup() {
    [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
    [[ -z $AUTH_DIR ]] || rm -rf -- "$AUTH_DIR"
    ((STAGED == 0)) || rm -rf -- "${STAGE:?}"
}
trap cleanup EXIT
compose() { docker compose --project-name "$PROJECT" --file "$DEPLOY_DIR/compose.yml" "$@"; }
up() { compose up --detach --wait --wait-timeout 180 --remove-orphans --pull never "$@"; }

lock_file() {   # $1: a name; the lock lives outside the deployment directory
    local base=/run/lock
    [[ -d $base ]] || base=/run
    printf '%s/oracle-view-%s.lock' "$base" "$1"
}
lock() {   # taken before any deployment state is read
    exec 9>"$(lock_file "$(printf '%s' "$DEPLOY_DIR" | sha256sum | cut -c1-16)")"
    flock -n 9 || die 'Another oracle-view.sh command is changing this deployment'
}
claim_project() {   # one directory for the Compose project, and one command at a time on it
    local owners others
    exec 8>"$(lock_file "project-$PROJECT")"
    flock -n 8 || die "Another oracle-view.sh command is changing the Compose project $PROJECT"
    owners=$(docker ps --all --filter "label=com.docker.compose.project=$PROJECT" \
        --format '{{.Label "com.docker.compose.project.working_dir"}}')
    others=$(printf '%s\n' "$owners" | sort -u | grep -vxF -e "$DEPLOY_DIR" -e '' || true)
    [[ -z $others ]] || die "The Compose project $PROJECT already runs from $others. The manual install of the oracle-view repository (deploy/grey) runs from /opt/oracle-view: remove it with its uninstall-app.sh, without --purge, so that the state volume stays. Then run install again."
}
confirm() {   # $1: the word to type
    if [[ -z $CONFIRM ]]; then
        [[ -t 0 ]] || die "No terminal to confirm on: pass --confirm $1. Nothing was changed."
        read -r -p "Type $1 to confirm: " CONFIRM || die 'Cancelled'
    fi
    [[ $CONFIRM == "$1" ]] || die 'Not confirmed. Nothing was changed.'
}

is_ours() { [[ -f $DEPLOY_DIR/$IDENTITY && $(sed -n 's/^DIR=//p' "$DEPLOY_DIR/$IDENTITY" | tail -n 1) == "$DEPLOY_DIR" ]]; }
open_deployment() {   # an installed deployment of this script, with its settings
    if ! is_ours || [[ ! -f $DEPLOY_DIR/compose.yml || ! -f $DEPLOY_DIR/oracle-view.env ]]; then
        die "No oracle-view.sh deployment in $DEPLOY_DIR. Run: sudo bash oracle-view.sh install"
    fi
    read_settings "$DEPLOY_DIR/oracle-view.env"
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
            OVB_MEM_LIMIT|OVB_PIDS_LIMIT|OVB_CPUS|OVB_RP_SSLMODE) printf -v "$key" '%s' "${line#*=}" ;;
            *) die "$file: unknown key $key" ;;
        esac
    done < "$file"
}

problem() {   # $1 key, $2 value: prints why the value cannot be used, nothing when it can
    local value=$2 memory='^([1-9][0-9]{0,5})([mg])$' count='^[1-9][0-9]{0,4}$'
    local cpus='^[0-9]{1,2}(\.[0-9]{1,2})?$' zero='^0+(\.0+)?$'
    case $1 in
        OVB_MEM_LIMIT)
            if [[ ! $value =~ $memory ]]; then echo 'a memory size such as 1g or 768m'
            elif [[ ${BASH_REMATCH[2]} == m ]] && ((10#${BASH_REMATCH[1]} < 256)); then echo 'at least 256m'
            fi ;;
        OVB_PIDS_LIMIT) [[ $value =~ $count ]] && ((10#$value >= 32 && 10#$value <= 32768)) || echo 'a number of processes, 32-32768' ;;
        OVB_CPUS) [[ $value =~ $cpus && ! $value =~ $zero ]] || echo 'a number of CPUs such as 1.5 (0.01-99.99)' ;;
        OVB_RP_SSLMODE) [[ $value == require || $value == prefer ]] || echo 'require, or prefer when the DB server has ssl off (clear text)' ;;
    esac
}

check_settings() {
    local key reason
    for key in "${SETTINGS[@]}"; do
        reason=$(problem "$key" "${!key}")
        [[ -z $reason ]] || die "$key=${!key} cannot be used: it must be $reason"
    done
}

# ---------------------------------------------------------------- image
auth_error() { grep -Eiq 'unauthorized|authentication required|denied|forbidden|403|401|no basic auth' "$1"; }
pull_image() {   # asks for the registry account only when the registry wants one
    local username token attempts=0 endpoint name='^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$'
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
        [[ -t 0 ]] || die "The registry login needs a terminal (or run docker login $REGISTRY first)"
        printf 'The registry %s requires a login. Use the deployment account issued for it.\n' "$REGISTRY"
        read -r -p 'Registry username (blank cancels): ' username || die 'Cancelled'
        [[ -n $username ]] || die 'Cancelled'
        [[ $username =~ $name ]] || die 'Invalid registry username'
        IFS= read -r -s -p 'Registry password (hidden; blank cancels): ' token || die 'Cancelled'
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

# ---------------------------------------------------------------- generations
# A generation (the users file, the first passwords, oracle-view.env, compose.yml)
# is written to $STAGE, on the deployment's filesystem, checked there, and
# published by renames.

make_stage() {
    rm -rf -- "${STAGE:?}"
    install -d -m 0700 "$STAGE"
    STAGED=1
}

new_password() { head -c 18 /dev/urandom | base64 | tr -d '\n=' | tr '+/' '-_'; }

stage_users() {   # the users file, with each missing first user made by make_user.py of the image
    local entry name role password line form users=$STAGE/users initial=$STAGE/$INITIAL
    if [[ -e $DEPLOY_DIR/users || -L $DEPLOY_DIR/users ]]; then
        [[ -f $DEPLOY_DIR/users && ! -L $DEPLOY_DIR/users ]] || die "$DEPLOY_DIR/users must be a regular file"
        cp -- "$DEPLOY_DIR/users" "$users"
        [[ ! -s $users || -z $(tail -c 1 -- "$users") ]] || printf '\n' >> "$users"   # a last line without its line end
    else
        : > "$users"
    fi
    for entry in "${FIRST_USERS[@]}"; do
        name=${entry%%:*} role=${entry#*:}
        if grep -q "^$name:" "$users"; then
            printf 'Kept user %s\n' "$name"
            continue
        fi
        password=$(new_password)
        ((${#password} >= 20)) || die 'Could not make a random password'
        # The password reaches make_user.py on stdin only: printf is a shell builtin, so no command line holds it.
        line=$(printf '%s\n' "$password" | docker run --rm -i --network none --read-only --cap-drop ALL \
            --security-opt no-new-privileges:true --user "$IMAGE_UID:$IMAGE_UID" --entrypoint python "$IMAGE" \
            /opt/oracle-view/app/deploy/make_user.py --role "$role" --password-stdin "$name") \
            || die "make_user.py of the image failed for $name. Nothing was changed."
        form="^$name:$role:scrypt:ln=[0-9]+:r=[0-9]+:p=[0-9]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+\$"
        [[ $line =~ $form ]] \
            || die "make_user.py of the image did not print a users line for $name. Nothing was changed."
        printf '%s\n' "$line" >> "$users"
        if [[ ! -f $initial ]]; then
            if [[ -f $DEPLOY_DIR/$INITIAL ]]; then
                cp -- "$DEPLOY_DIR/$INITIAL" "$initial"
            else
                printf 'Oracle Field Browser, first logins (made by oracle-view.sh install)\nURL: https://stgiss.perodua.com.my/dev/oracle-view/\nKeep each password in a password manager, then delete this file.\n\n' > "$initial"
            fi
        fi
        printf 'User name: %s (role %s, made %s)\nPassword:  %s\n\n' "$name" "$role" "$(date -u +%Y-%m-%dT%H:%MZ)" \
            "$password" >> "$initial"
        unset password
        NEW_USERS+=("$name ($role)")
    done
    chown "$IMAGE_UID:$IMAGE_UID" "$users"
    chmod 0400 "$users"
    if [[ -f $initial ]]; then
        chown root:root "$initial"
        chmod 0600 "$initial"
    fi
}

stage_files() {   # $1 image: compose.yml and oracle-view.env of the new generation, checked by Compose in $STAGE
    local image=$1 key
    cat > "$STAGE/compose.yml" <<YAML
# Written by oracle-view.sh install from oracle-view.env: edit that file, then run install.
services:
  $PROJECT:
    image: $image
    pull_policy: never
    container_name: $PROJECT
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
    volumes:
      - oracle-view-state:/var/lib/oracle-view
      # The server reads the users file at its start only. A missing file is an error, never a new folder.
      - type: bind
        source: ./users
        target: /etc/oracle-view/users
        read_only: true
        bind:
          create_host_path: false
    ports:
      - "127.0.0.1:$PORT:8797"
    environment:
      OVB_RP_SSLMODE: "$OVB_RP_SSLMODE"
    mem_limit: "$OVB_MEM_LIMIT"
    memswap_limit: "$OVB_MEM_LIMIT"
    pids_limit: $OVB_PIDS_LIMIT
    cpus: $OVB_CPUS
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    healthcheck:
      test: ["CMD", "python", "/opt/oracle-view/app/deploy/healthcheck.py"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 90s
      start_interval: 2s
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "5"}
volumes:
  oracle-view-state:
YAML
    {
        printf '# Settings of oracle-view.sh (literal KEY=VALUE). After a change: sudo bash oracle-view.sh install\n'
        for key in "${SETTINGS[@]}"; do printf '%s=%s\n' "$key" "${!key}"; done
    } > "$STAGE/oracle-view.env"
    docker compose --project-name "$PROJECT" --file "$STAGE/compose.yml" config --quiet \
        || die 'The generated compose file is not valid. Nothing was changed.'
}

publish_stage() {   # renames on one filesystem, so every file is replaced whole; compose.yml last
    local file
    for file in "${PUBLISHED[@]}"; do
        [[ ! -f $STAGE/$file ]] || mv -f -- "$STAGE/$file" "$DEPLOY_DIR/$file" || return 1
    done
}

keep_published() {   # copies of the files publish_stage will replace, to undo it
    local file
    install -d -m 0700 "$STAGE/previous"
    for file in "${PUBLISHED[@]}"; do
        [[ ! -f $DEPLOY_DIR/$file ]] || cp -p -- "$DEPLOY_DIR/$file" "$STAGE/previous/$file"
    done
}

undo_publication() {   # every published file as it was before publish_stage, or gone if it was not there
    local file
    for file in "${PUBLISHED[@]}"; do
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
    rm -f -- "$DEPLOY_DIR/oracle-view.env.failed"
}

unpack_snapshot() {   # the last good generation into a fresh $STAGE
    rm -rf -- "${STAGE:?}" && install -d -m 0700 "$STAGE" && tar -xpf "$DEPLOY_DIR/$SNAPSHOT" -C "$STAGE"
}

deploy() {   # publish the staged generation and start it; a release that does not start is switched back
    local -a recreate=()
    local users_sum
    users_sum=$(sha256sum -- "$STAGE/users" | cut -d' ' -f1)
    # The server reads the users file at its start only: a changed file needs a new container.
    if ((${#NEW_USERS[@]})) || [[ $users_sum != "$(cat -- "$DEPLOY_DIR/$STAMP" 2> /dev/null || true)" ]]; then
        recreate=(--force-recreate)
    fi
    keep_published
    if ! publish_stage; then
        undo_publication || die "Publishing the new files failed and could not be undone: check $DEPLOY_DIR"
        die 'Publishing the new files failed. Nothing was changed, and the tool still runs what it ran.'
    fi
    printf 'Starting %s...\n' "$PROJECT"
    if up "${recreate[@]}"; then
        printf '%s\n' "$users_sum" > "$DEPLOY_DIR/$STAMP"
        save_snapshot
        return 0
    fi
    compose logs --tail 40 "$PROJECT" >&2 || true
    switch_back
}

switch_back() {   # after a failed start: the last good generation runs again, if it is another one
    [[ -f $DEPLOY_DIR/$SNAPSHOT ]] || die 'The tool did not become healthy (log above)'
    unpack_snapshot || die "Could not read the last good release in $DEPLOY_DIR/$SNAPSHOT"
    ! same_generation || die 'The tool did not become healthy (log above)'
    cp -p -- "$DEPLOY_DIR/oracle-view.env" "$DEPLOY_DIR/oracle-view.env.failed" || die 'Could not keep the settings that failed'
    publish_stage || die "Could not put the last good release back; it is in $DEPLOY_DIR/$SNAPSHOT"
    up --force-recreate \
        || die "The new release did not become healthy, and the last good one did not start either (it is in $DEPLOY_DIR/$SNAPSHOT); see: sudo docker logs $PROJECT"
    die 'The new release did not become healthy; the last good release runs again with its settings. The settings that failed are in oracle-view.env.failed (log above).'
}

owns_port() {   # the port is the one this deployment's running container publishes
    [[ -f $DEPLOY_DIR/compose.yml && -n $(compose ps --quiet "$PROJECT" 2> /dev/null) ]]
}

healthz() {   # /healthz through the published port, from this server
    [[ $(curl -fsS --max-time 5 --noproxy '*' "http://127.0.0.1:$PORT/healthz" 2> /dev/null || true) == ok ]]
}

route_listed() { grep -qsF '/dev/oracle-view/' "$ROUTES_FILE"; }

# ---------------------------------------------------------------- App commands
cmd_install() {
    local list
    lock
    if ! is_ours && [[ -e $DEPLOY_DIR ]] && [[ ! -d $DEPLOY_DIR || -n $(ls -A -- "$DEPLOY_DIR") ]]; then
        die "$DEPLOY_DIR is not empty and not a deployment of this script: use a new or empty directory"
    fi
    if is_ours && [[ -f $DEPLOY_DIR/oracle-view.env ]]; then read_settings "$DEPLOY_DIR/oracle-view.env"; fi
    [[ -z $CONFIG ]] || read_settings "$CONFIG"
    check_settings
    claim_project
    install -d -m 0700 "$DEPLOY_DIR"
    is_ours || printf 'DIR=%s\n' "$DEPLOY_DIR" > "$DEPLOY_DIR/$IDENTITY"
    make_stage
    if [[ -n $(ss -ltnH "sport = :$PORT") ]] && ! owns_port; then
        die "Port $PORT is already used by another program on this server"
    fi
    pull_image
    stage_users
    stage_files "$IMAGE"
    deploy
    if command -v curl > /dev/null; then
        healthz || die "The container is healthy, but http://127.0.0.1:$PORT/healthz does not answer ok on this server: see sudo bash oracle-view.sh status"
    else
        printf 'NOTE curl is not installed: the check of http://127.0.0.1:%s/healthz on this server was not done\n' "$PORT"
    fi
    printf '\nInstalled: %s, healthy on http://127.0.0.1:%s/ (this server only)\n' "$RELEASE" "$PORT"
    printf 'Settings: %s/oracle-view.env\nUsers:    %s/users\n' "$DEPLOY_DIR" "$DEPLOY_DIR"
    if ((${#NEW_USERS[@]})); then
        list=$(printf '%s, ' "${NEW_USERS[@]}")
        printf '\nNew users: %s. Their first passwords are in %s (root only), never printed:\n' \
            "${list%, }" "$DEPLOY_DIR/$INITIAL"
        printf '  sudo cat %s\n' "$DEPLOY_DIR/$INITIAL"
        printf 'Keep them in a password manager and give the pm password to the PM in a safe way. Then delete the file:\n'
        printf '  sudo rm %s\n' "$DEPLOY_DIR/$INITIAL"
    elif [[ -f $DEPLOY_DIR/$INITIAL ]]; then
        printf '\n%s still holds first passwords: keep them, then delete it: sudo rm %s\n' \
            "$DEPLOY_DIR/$INITIAL" "$DEPLOY_DIR/$INITIAL"
    fi
    if ! route_listed; then
        printf '\nNext: publish it with https.sh (README.md, Oracle Field Browser). Add this line to %s:\n  %s\n' \
            "$ROUTES_FILE" "$ROUTE"
        printf 'then run: sudo bash https.sh apply\n'
        printf 'The http option serves the login on port 80 in clear text for the WAF: let only the WAF addresses reach port 80.\n'
    fi
}

cmd_status() {
    local image users
    open_deployment
    image=$(sed -n 's/^    image: //p' "$DEPLOY_DIR/compose.yml")
    if [[ $image == "$IMAGE" ]]; then
        printf 'Release:  %s (the pinned image)\n' "$RELEASE"
    else
        printf 'Release:  not the pinned %s: run install to change to it\n' "$RELEASE"
    fi
    printf 'Image:    %s\n' "$image"
    printf 'Listens:  127.0.0.1:%s (this server only)\n' "$PORT"
    printf 'Settings: memory %s, %s processes, %s CPUs, TLS to the RP database %s (%s/oracle-view.env)\n' \
        "$OVB_MEM_LIMIT" "$OVB_PIDS_LIMIT" "$OVB_CPUS" "$OVB_RP_SSLMODE" "$DEPLOY_DIR"
    # Only the name and the role: a line is name:role:<hash>, an old line name:<hash> is an admin.
    users=$(awk -F: 'NF { printf "%s%s (%s)", sep, $1, (($2 ~ /^(viewer|editor|admin)$/) ? $2 : "admin"); sep = ", " }' \
        "$DEPLOY_DIR/users" 2> /dev/null || true)
    printf 'Users:    %s (%s/users)\n' "${users:-none}" "$DEPLOY_DIR"
    if docker volume inspect "$VOLUME" > /dev/null 2>&1; then
        printf 'State:    volume %s\n' "$VOLUME"
    else
        printf 'State:    volume %s is missing\n' "$VOLUME"
    fi
    if ! command -v curl > /dev/null; then
        printf 'Health:   not checked (curl is not installed)\n'
    elif healthz; then
        printf 'Health:   http://127.0.0.1:%s/healthz answers ok\n' "$PORT"
    else
        printf 'Health:   http://127.0.0.1:%s/healthz does not answer ok (see: sudo docker logs %s)\n' "$PORT" "$PROJECT"
    fi
    [[ ! -f $DEPLOY_DIR/$INITIAL ]] \
        || printf 'NOTE      %s still holds first passwords: keep them, then delete it\n' "$DEPLOY_DIR/$INITIAL"
    printf '\n'
    compose ps --all
}

cmd_uninstall() {
    local id
    lock
    is_ours || die "$DEPLOY_DIR is not a deployment of this script (no $IDENTITY). Nothing was changed."
    if ((PURGE)); then
        printf 'This removes the %s container, the files install made in %s, the users file, the state volume %s (mapping edits, SQL audit) and the images of %s.\n' \
            "$PROJECT" "$DEPLOY_DIR" "$VOLUME" "$REPOSITORY"
    else
        printf 'This removes the %s container and the files install made in %s. The state volume %s, the users file and the image stay.\n' \
            "$PROJECT" "$DEPLOY_DIR" "$VOLUME"
    fi
    confirm "$PROJECT"
    claim_project
    if [[ -f $DEPLOY_DIR/compose.yml ]]; then
        compose down --remove-orphans
    else
        docker ps --all --quiet --filter "label=com.docker.compose.project=$PROJECT" | xargs -r docker rm --force > /dev/null
        docker network rm "${PROJECT}_default" > /dev/null 2>&1 || true
    fi
    # The first passwords never stay behind.
    if [[ -f $DEPLOY_DIR/$INITIAL ]]; then
        shred -u -- "$DEPLOY_DIR/$INITIAL" 2> /dev/null || rm -f -- "$DEPLOY_DIR/$INITIAL"
    fi
    rm -rf -- "${STAGE:?}"
    rm -f -- "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/oracle-view.env" "$DEPLOY_DIR/oracle-view.env.failed" \
        "$DEPLOY_DIR/$SNAPSHOT" "$DEPLOY_DIR/$SNAPSHOT.new" "$DEPLOY_DIR/${STAMP:?}"
    if ((PURGE)); then
        if docker volume inspect "$VOLUME" > /dev/null 2>&1; then
            docker volume rm "$VOLUME" > /dev/null || printf 'WARNING: the volume %s could not be removed (see above)\n' "$VOLUME" >&2
        fi
        for id in $(docker image ls --quiet "$REPOSITORY" | sort -u); do
            docker image rm --force "$id" > /dev/null || printf 'WARNING: the image %s could not be removed\n' "$id" >&2
        done
        rm -f -- "$DEPLOY_DIR/users" "$DEPLOY_DIR/${IDENTITY:?}"
        if rmdir -- "$DEPLOY_DIR" 2> /dev/null; then
            printf 'Removed %s, its state volume, its users, its images and %s.\n' "$PROJECT" "$DEPLOY_DIR"
        else
            printf 'Removed %s, its state volume, its users and its images. %s stays: it holds files install did not create.\n' \
                "$PROJECT" "$DEPLOY_DIR"
        fi
    else
        printf 'Removed the %s container and the files of install in %s.\n' "$PROJECT" "$DEPLOY_DIR"
        printf 'Kept: the state volume %s (mapping edits, SQL audit, login device key), %s/users (the users and their password hashes) and the image %s.\n' \
            "$VOLUME" "$DEPLOY_DIR" "${IMAGE%@*}"
        printf 'A later install uses them again; uninstall --purge removes them.\n'
    fi
    if route_listed; then
        printf 'The route /dev/oracle-view/ stays in %s and now has no service: remove its line, then run: sudo bash https.sh apply\n' \
            "$ROUTES_FILE"
    fi
}

# ---------------------------------------------------------------- DB server
PSQL=''
db_lock() {
    exec 9>"$(lock_file role)"
    flock -n 9 || die 'Another oracle-view.sh role or drop-role command is running'
}
find_psql() {
    PSQL=$(command -v psql || true)
    [[ -n $PSQL || ! -x /usr/lib/postgresql/16/bin/psql ]] || PSQL=/usr/lib/postgresql/16/bin/psql
    [[ -n $PSQL ]] || die 'psql is not installed on this server: run role and drop-role on the DB server'
    id -u postgres > /dev/null 2>&1 || die 'There is no postgres user on this server: run role and drop-role on the DB server'
}
pg() {   # $1 database, then psql options: as postgres over the local socket, stop at the first error
    local database=$1
    shift
    runuser -u postgres -- "$PSQL" -X -w -h /var/run/postgresql -p "$PG_PORT" -d "$database" -v ON_ERROR_STOP=1 "$@"
}
pg_value() { pg "$1" -At -c "$2"; }
db_ready() {
    local answer
    answer=$(pg_value postgres 'SELECT 1' 2>&1) || true
    [[ $answer == 1 ]] || die "PostgreSQL does not answer on port $PG_PORT of this server (socket /var/run/postgresql): $answer"
    [[ $(pg_value postgres "SELECT count(*) FROM pg_database WHERE datname = '$RP_DATABASE'") == 1 ]] \
        || die "There is no database $RP_DATABASE on this server: the image reads that database (its rp_targets.json)"
}

# PostgreSQL's form of a SCRAM-SHA-256 password: SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>.
VERIFIER_FORM='^SCRAM-SHA-256\$[1-9][0-9]{0,8}:[A-Za-z0-9+/]{4,}={0,2}\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=$'
read_verifier() {   # $1 file: one line, a SCRAM-SHA-256 verifier; its value is never printed
    local file=$1 text
    [[ -f $file && -r $file ]] || die "--verifier-file: $file is not a readable file"
    (($(wc -c < "$file") <= 1024)) || die "$file is too large for a verifier: give the verifier file of the image build"
    text=$(< "$file")
    text=${text%$'\r'}
    [[ $text != *$'\n'* ]] || die "$file must hold one line, the verifier: give the verifier file of the image build"
    [[ $text =~ $VERIFIER_FORM ]] \
        || die "$file does not hold a SCRAM-SHA-256 verifier (SCRAM-SHA-256\$<iterations>:<salt>\$<StoredKey>:<ServerKey>). Give the verifier file of the image build; this command never takes the password itself."
    VERIFIER=$text
}

role_sql() {   # after a \set ovb_verifier line: create or update the role and its grants
    cat <<'SQL'
-- oracle-view.sh: role
\set QUIET on
-- The statements that hold the verifier stay out of the server log, also when one
-- fails, and out of the error text of psql.
\set VERBOSITY terse
\set SHOW_CONTEXT never
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_ro') AS ovb_old \gset
\if :ovb_old
\echo The read-only role oracle_view_ro of an earlier bundle: its sessions end and it is dropped
ALTER ROLE oracle_view_ro NOLOGIN;
SELECT count(pg_terminate_backend(pid)) AS ended_sessions FROM pg_stat_activity WHERE usename = 'oracle_view_ro' \gset
\connect perodua_oracle
DROP OWNED BY oracle_view_ro;
\connect postgres
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
DROP ROLE oracle_view_ro;
\endif
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_rp') AS ovb_new \gset
\if :ovb_new
CREATE ROLE oracle_view_rp LOGIN PASSWORD :'ovb_verifier' CONNECTION LIMIT 10;
\else
ALTER ROLE oracle_view_rp LOGIN PASSWORD :'ovb_verifier' CONNECTION LIMIT 10;
\endif
\unset ovb_verifier
\set VERBOSITY default
\set SHOW_CONTEXT errors
-- Owner decision of 2026-11-06: the SQL console has the maximum rights on this test
-- database, read-only by default. No superuser, no ownership of a table (Odoo owns
-- them, so DDL on them fails), no TRUNCATE, no CREATE in public.
ALTER ROLE oracle_view_rp NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE oracle_view_rp SET default_transaction_read_only = on;
ALTER ROLE oracle_view_rp SET statement_timeout = '30s';
ALTER ROLE oracle_view_rp SET idle_in_transaction_session_timeout = '60s';
GRANT CONNECT, TEMPORARY ON DATABASE perodua_oracle TO oracle_view_rp;
\connect perodua_oracle
GRANT USAGE ON SCHEMA public TO oracle_view_rp;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO oracle_view_rp;
GRANT SELECT, USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public TO oracle_view_rp;
-- New tables and sequences: default privileges for each role that owns objects in
-- public, and for the owner of the database (Odoo creates its tables as that role).
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO oracle_view_rp', owner)
  FROM (SELECT DISTINCT pg_get_userbyid(c.relowner) AS owner
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        UNION
        SELECT pg_get_userbyid(d.datdba) FROM pg_database d WHERE d.datname = current_database()) owners
 WHERE owner <> 'oracle_view_rp' \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO oracle_view_rp', owner)
  FROM (SELECT DISTINCT pg_get_userbyid(c.relowner) AS owner
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        UNION
        SELECT pg_get_userbyid(d.datdba) FROM pg_database d WHERE d.datname = current_database()) owners
 WHERE owner <> 'oracle_view_rp' \gexec
SQL
}

# One line per right, "OK|what" or "FAIL|what", in the database perodua_oracle.
# has_table_privilege with a list is true when ANY of the rights is held: each right is checked alone.
ROLE_CHECK_SQL=$(cat <<'SQL'
SELECT CASE WHEN ok THEN 'OK' ELSE 'FAIL' END || '|' || what FROM (
  SELECT 1 AS n, left(rolpassword, 14) = 'SCRAM-SHA-256$' AS ok, 'the password is a SCRAM-SHA-256 verifier' AS what
    FROM pg_authid WHERE rolname = 'oracle_view_rp'
  UNION ALL
  SELECT 2, rolcanlogin AND rolconnlimit = 10, 'LOGIN, CONNECTION LIMIT 10'
    FROM pg_roles WHERE rolname = 'oracle_view_rp'
  UNION ALL
  SELECT 3, NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls),
         'no SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS'
    FROM pg_roles WHERE rolname = 'oracle_view_rp'
  UNION ALL
  SELECT 4, coalesce('default_transaction_read_only=on' = ANY (rolconfig), false),
         'read-only by default (default_transaction_read_only=on)'
    FROM pg_roles WHERE rolname = 'oracle_view_rp'
  UNION ALL
  SELECT 5, has_database_privilege('oracle_view_rp', current_database(), 'CONNECT'), 'CONNECT on perodua_oracle'
  UNION ALL
  SELECT 6, count(*) = 0, 'SELECT on every table and view of public (' || count(*) || ' without)'
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
     AND NOT has_table_privilege('oracle_view_rp', c.oid, 'SELECT')
  UNION ALL
  SELECT 7, count(*) = 0, 'INSERT, UPDATE and DELETE on every table of public (' || count(*) || ' without)'
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
     AND NOT (has_table_privilege('oracle_view_rp', c.oid, 'INSERT')
              AND has_table_privilege('oracle_view_rp', c.oid, 'UPDATE')
              AND has_table_privilege('oracle_view_rp', c.oid, 'DELETE'))
  UNION ALL
  SELECT 8, count(*) = 0, 'USAGE and UPDATE on every sequence of public (' || count(*) || ' without)'
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind = 'S'
     AND NOT (has_sequence_privilege('oracle_view_rp', c.oid, 'USAGE')
              AND has_sequence_privilege('oracle_view_rp', c.oid, 'UPDATE'))
  UNION ALL
  SELECT 9, NOT coalesce(bool_or(has_table_privilege('oracle_view_rp', c.oid, 'TRUNCATE')), false), 'no TRUNCATE'
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  UNION ALL
  SELECT 10, NOT has_schema_privilege('oracle_view_rp', 'public', 'CREATE'), 'no CREATE in public'
  UNION ALL
  SELECT 11, count(*) = 0, 'owns no table, view or sequence (DDL on the Odoo tables fails)'
    FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')
) checks ORDER BY n
SQL
)

role_checks() {   # prints each right; a missing or an extra right stops the command
    local out result what failed=0
    out=$(pg_value "$RP_DATABASE" "$ROLE_CHECK_SQL") || die 'The check of the rights failed (above)'
    [[ $(grep -c . <<< "$out") == 11 ]] || die "The check of the rights gave an unexpected answer: $out"
    while IFS='|' read -r result what; do
        printf '%-4s %s\n' "$result" "$what"
        [[ $result == OK ]] || failed=1
    done <<< "$out"
    ((failed == 0)) || die "The role $RP_ROLE does not have the rights above. Nothing in pg_hba.conf was checked."
}

tls_note() {
    local ssl
    ssl=$(pg_value postgres 'SHOW ssl')
    printf '\n== TLS: SHOW ssl\nssl = %s\n' "$ssl"
    [[ $ssl != on ]] || return 0
    cat <<TEXT
WARNING: ssl is not on. The tool connects with OVB_RP_SSLMODE=require by default and then cannot connect.
Choose one (README.md, Oracle Field Browser):
  a. turn TLS on here: a server certificate and key in the data folder, ALTER SYSTEM SET ssl = on,
     then SELECT pg_reload_conf() (no restart);
  b. or keep clear text between the servers: on the App server add OVB_RP_SSLMODE=prefer to
     oracle-view.env of the deployment, then run: sudo bash oracle-view.sh install
TEXT
}

extensions_note() {
    local found
    found=$(pg_value "$RP_DATABASE" "SELECT coalesce(string_agg(extname, ', ' ORDER BY extname), '') FROM pg_extension WHERE extname IN ('dblink', 'postgres_fdw')")
    printf '\n== Extensions that can write in a Read run (dblink, postgres_fdw)\n'
    if [[ -n $found ]]; then
        printf '%s has: %s\n' "$RP_DATABASE" "$found"
        printf 'A Read run of the SQL console can write through them. This command changes no right:\n'
        printf 'to stop it (REVOKE EXECUTE on their functions) is an owner decision.\n'
    else
        printf 'none: %s has no dblink and no postgres_fdw\n' "$RP_DATABASE"
    fi
}

hba_sql() {   # with the psql variable ovb_app: does a pg_hba.conf line let the App server in? It changes nothing.
    cat <<'SQL'
-- oracle-view.sh: hba-check
-- pg_hba.conf takes the first rule that matches (type, database, user, address), in the
-- order of rule_number (include files count where they are included). The type depends
-- on the transport: with ssl on, the tool connects with TLS (sslmode require or prefer),
-- so host and hostssl lines match and hostnossl lines do not; with ssl off, host and
-- hostnossl lines match (the tool then needs sslmode prefer). The tool has no Kerberos:
-- a hostgssenc line never matches, a hostnogssenc line does. An IPv6 line never matches
-- the IPv4 address of the App server. A line that this check cannot read (samehost,
-- samenet, a host name, +group, @file, a regular expression /..., sameuser, samerole,
-- samegroup) above the first matching line gives RESULT: CHECK BY HAND, and so does a
-- line with an error that stops PostgreSQL from loading the file (its type is empty). A
-- line that PostgreSQL loads with a warning (a hostssl line with ssl off) never matches.
-- md5 is OK: for a role with a SCRAM-SHA-256 verifier, PostgreSQL then uses SCRAM.
\pset footer off
\echo == pg_hba.conf and TLS of this server
SELECT current_setting('hba_file') AS hba_file, current_setting('listen_addresses') AS listen_addresses,
       current_setting('port') AS port, current_setting('password_encryption') AS password_encryption,
       current_setting('ssl') AS ssl;
CREATE TEMPORARY VIEW ovb_hba_rules AS
  SELECT rule_number, file_name, line_number, type, database, user_name, address, netmask, auth_method,
         (type = 'host' OR type = 'hostnogssenc'
          OR (type = 'hostssl' AND current_setting('ssl') = 'on')
          OR (type = 'hostnossl' AND current_setting('ssl') <> 'on'))
         AND ('all' = ANY (database) OR 'perodua_oracle' = ANY (database))
         AND ('all' = ANY (user_name) OR 'oracle_view_rp' = ANY (user_name))
         -- CASE: the casts run only on IPv4 lines (an IPv6 mask with an IPv4 address is an error).
         AND (address = 'all'
              OR CASE WHEN address ~ '^[0-9.]+$' AND netmask ~ '^[0-9.]+$'
                      THEN (:'ovb_app'::inet & netmask::inet) = (address::inet & netmask::inet)
                      ELSE false END) AS matches,
         type IN ('host', 'hostssl', 'hostnossl', 'hostnogssenc')
         AND (address IN ('samehost', 'samenet') OR (address !~ '^([0-9.]+|all)$' AND address !~ ':')
              OR EXISTS (SELECT 1 FROM unnest(user_name) u WHERE u ~ '^[+@/]')
              OR EXISTS (SELECT 1 FROM unnest(database) d
                          WHERE d ~ '^[@/]' OR d IN ('sameuser', 'samerole', 'samegroup'))) AS unsure
    FROM pg_hba_file_rules
   WHERE error IS NULL AND type <> 'local';
\echo == The lines that can match the App server, database perodua_oracle, user oracle_view_rp
SELECT rule_number, line_number, type, database, user_name, address, netmask, auth_method,
       CASE WHEN matches THEN 'matches' ELSE 'check by hand' END AS result, file_name
  FROM ovb_hba_rules WHERE matches OR unsure ORDER BY rule_number;
SELECT coalesce((SELECT auth_method FROM ovb_hba_rules WHERE matches ORDER BY rule_number LIMIT 1), 'none')
         AS first_auth_method,
       EXISTS (SELECT 1 FROM ovb_hba_rules
                WHERE unsure AND NOT matches
                  AND rule_number < coalesce((SELECT min(rule_number) FROM ovb_hba_rules WHERE matches), 2147483647))
         AS unsure_before,
       EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL AND type IS NULL) AS hba_errors \gset ovb_
SELECT :'ovb_first_auth_method' IN ('scram-sha-256', 'md5') AS ovb_hba_ok,
       :'ovb_first_auth_method' = 'md5' AS ovb_hba_md5 \gset
\echo first matching line uses: :ovb_first_auth_method
\if :ovb_hba_md5
\echo NOTE: md5 also works for this role (its password is a SCRAM verifier, so PostgreSQL uses SCRAM).
\endif
\if :ovb_hba_errors
\echo RESULT: CHECK BY HAND - pg_hba.conf has lines with errors (the last list). PostgreSQL does not load such a file, so this check cannot tell which lines it uses.
\elif :ovb_unsure_before
\echo RESULT: CHECK BY HAND - a line above the first matching line has a rule that this check cannot read.
\elif :ovb_hba_ok
\echo RESULT: OK - the first matching line uses :ovb_first_auth_method.
\else
\echo RESULT: NOT OK - no matching line, or the first matching line does not use scram-sha-256 or md5.
\endif
\echo == pg_hba.conf lines with errors (expected: none)
SELECT file_name, line_number, error,
       CASE WHEN type IS NULL THEN 'the file does not load' ELSE 'loaded, never matches' END AS effect
  FROM pg_hba_file_rules WHERE error IS NOT NULL;
SQL
}

hba_check() {   # returns 0 when the App server gets in, 3 when the line is missing, 4 for a check by hand
    local out hba again reload block status=0 where='above the first line of the list above, or above the first host line'
    local line="host    $RP_DATABASE    $RP_ROLE    $APP_IP/32    scram-sha-256" marker="# BEGIN perodua-db-deploy $RP_DATABASE"
    out=$(hba_sql | pg postgres -q -v "ovb_app=$APP_IP" -f -) || die 'The pg_hba.conf check failed (above)'
    printf '%s\n' "$out"
    hba=$(pg_value postgres 'SHOW hba_file')
    again='sudo bash oracle-view.sh role --check'
    [[ $APP_IP == 192.168.23.13 ]] || again+=" --app-ip $APP_IP"
    [[ $PG_PORT == 5432 ]] || again+=" --pg-port $PG_PORT"
    reload="sudo -u postgres psql -X -p $PG_PORT -d postgres -c 'SELECT pg_reload_conf()'"
    # Each deploy-db.sh run writes its block for the database at the top of pg_hba.conf. The
    # block lets in only the App role and ends with lines that reject every other role.
    block=$(grep -m 1 -nxF -- "$marker" "$hba" 2> /dev/null | cut -d: -f1) || block=''
    [[ -z $block ]] || where="above line $block, \"$marker\": a line inside or below that block of deploy-db.sh does not work"
    if grep -q 'RESULT: CHECK BY HAND - pg_hba.conf has lines with errors' <<< "$out"; then
        cat <<TEXT

PostgreSQL does not load a pg_hba.conf with errors, so this check cannot tell which lines it uses.
  1. Correct the lines of the last list, in the files that it names.
  2. Reload (no restart):  $reload
  3. Check again:          $again
TEXT
        status=4
    elif grep -q 'RESULT: CHECK BY HAND' <<< "$out"; then
        [[ -z $block ]] || where="above them, and $where"
        [[ -n $block ]] || where='above them'
        cat <<TEXT

Read the lines that the list marks "check by hand" in $hba. When none of them matches the
App server ($APP_IP, database $RP_DATABASE, user $RP_ROLE), the first "matches" line decides.
To be sure, add this line $where:
    $line
  1. Back up the file first:  sudo cp -p $hba $hba.before-oracle-view
  2. Reload (no restart):     $reload
  3. Check again:             $again
TEXT
        status=4
    elif grep -q 'RESULT: NOT OK' <<< "$out"; then
        cat <<TEXT

This command never edits pg_hba.conf. To let the App server ($APP_IP) in:
  1. Back up the file:  sudo cp -p $hba $hba.before-oracle-view
  2. Add this line $where:
       $line
  3. Reload (no restart):  $reload
  4. Check again:  $again
Way back: sudo cp -p $hba.before-oracle-view $hba, then the reload of step 3.
TEXT
        status=3
    else
        grep -q 'RESULT: OK' <<< "$out" || die 'The pg_hba.conf check gave no result (above)'
    fi
    if [[ -n $block ]]; then
        cat <<TEXT

NOTE: $hba holds the block of deploy-db.sh for $RP_DATABASE (line $block). Each later
deploy-db.sh run writes that block again at the top of the file, above the line of $RP_ROLE,
and deletes the line when it is inside the block. After each deploy-db.sh run, run:
    $again
and move the line of $RP_ROLE back above the line "$marker".
TEXT
    fi
    return "$status"
}

drop_sql() {
    cat <<'SQL'
-- oracle-view.sh: drop-role
-- Stops new logins of oracle_view_rp (and of oracle_view_ro of an earlier bundle), ends
-- their sessions, removes their grants and default privileges in perodua_oracle
-- (DROP OWNED BY) and drops the roles. The rows that the SQL console changed stay changed.
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_rp') AS ovb_rp,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_ro') AS ovb_ro \gset
\if :ovb_rp
ALTER ROLE oracle_view_rp NOLOGIN;
SELECT count(pg_terminate_backend(pid)) AS ended_sessions FROM pg_stat_activity WHERE usename = 'oracle_view_rp' \gset
\connect perodua_oracle
DROP OWNED BY oracle_view_rp;
\connect postgres
DROP ROLE oracle_view_rp;
\echo role oracle_view_rp dropped
\else
\echo role oracle_view_rp does not exist: nothing to do
\endif
\if :ovb_ro
ALTER ROLE oracle_view_ro NOLOGIN;
SELECT count(pg_terminate_backend(pid)) AS ended_sessions FROM pg_stat_activity WHERE usename = 'oracle_view_ro' \gset
\connect perodua_oracle
DROP OWNED BY oracle_view_ro;
\connect postgres
DROP ROLE oracle_view_ro;
\echo role oracle_view_ro of an earlier bundle dropped
\endif
SQL
}

cmd_role() {
    local status=0
    db_lock
    find_psql
    db_ready
    if ((CHECK_ONLY)); then
        [[ $(pg_value postgres "SELECT count(*) FROM pg_roles WHERE rolname = '$RP_ROLE'") == 1 ]] \
            || die "The role $RP_ROLE does not exist yet: run role --verifier-file FILE first"
    else
        read_verifier "$VERIFIER_FILE"
        printf '== The role %s on %s\n' "$RP_ROLE" "$RP_DATABASE"
        # The verifier goes to psql on stdin only, never on a command line.
        { printf "\\set ovb_verifier '%s'\n" "$VERIFIER"; role_sql; } | pg postgres -q -f - \
            || die "Creating or updating the role $RP_ROLE failed (above)"
        VERIFIER=''
        printf 'The role %s has the verifier of the file and its grants\n' "$RP_ROLE"
    fi
    printf '\n== Rights of %s\n' "$RP_ROLE"
    role_checks
    tls_note
    extensions_note
    printf '\n== pg_hba.conf check\n'
    hba_check || status=$?
    ((status != 0)) || printf '\nThe role is ready. Next: the App server (README.md, Oracle Field Browser).\n'
    # role --check needs no verifier: the copy on this server is not needed any more.
    ((CHECK_ONLY)) || printf 'Delete your copy of the verifier file: rm %s\n' "$VERIFIER_FILE"
    exit "$status"
}

cmd_drop_role() {
    local hba
    db_lock
    find_psql
    db_ready
    printf 'This ends the sessions of %s, removes its rights in %s and drops it (also oracle_view_ro of an earlier bundle).\n' \
        "$RP_ROLE" "$RP_DATABASE"
    printf 'Rows that the SQL console changed stay changed.\n'
    confirm "$RP_ROLE"
    drop_sql | pg postgres -q -f - || die "Dropping the role $RP_ROLE failed (above)"
    hba=$(pg_value postgres 'SHOW hba_file')
    printf 'A pg_hba.conf line that you added for %s stays in %s: remove it by hand (or put back %s.before-oracle-view),\n' \
        "$RP_ROLE" "$hba" "$hba"
    printf "then reload: sudo -u postgres psql -X -p %s -d postgres -c 'SELECT pg_reload_conf()'\n" "$PG_PORT"
}

# ---------------------------------------------------------------- main
COMMAND=${1:-}
[[ -n $COMMAND ]] || { usage >&2; exit 2; }
shift
case $COMMAND in -h|--help|help) usage; exit 0 ;; esac
while (($#)); do
    case $1 in
        --config|--confirm|--dir|--verifier-file|--app-ip|--pg-port)
            (($# >= 2)) || usage_error "Missing value for $1"
            case $1 in
                --config) CONFIG=$2 ;;
                --confirm) CONFIRM=$2 ;;
                --dir) DEPLOY_DIR=$2 ;;
                --verifier-file) VERIFIER_FILE=$2 ;;
                --app-ip) APP_IP=$2 ;;
                --pg-port) PG_PORT=$2 ;;
            esac
            GIVEN+=("$1")
            shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; GIVEN+=("$1"); shift ;;
        --purge) PURGE=1; GIVEN+=("$1"); shift ;;
        --check) CHECK_ONLY=1; GIVEN+=("$1"); shift ;;
        *) usage_error "Unknown argument: $1" ;;
    esac
done
allowed() {   # $@: the options of COMMAND
    local option
    for option in "${GIVEN[@]}"; do
        [[ " $* " == *" $option "* ]] || usage_error "$option does not belong to $COMMAND"
    done
}
case $COMMAND in
    install) allowed --config --non-interactive --dir ;;
    status) allowed --dir ;;
    uninstall) allowed --purge --confirm --dir ;;
    role)
        allowed --verifier-file --check --app-ip --pg-port
        [[ -n $VERIFIER_FILE || $CHECK_ONLY == 1 ]] || usage_error 'role needs --verifier-file FILE (or --check)'
        [[ -z $VERIFIER_FILE || $CHECK_ONLY == 0 ]] || usage_error '--verifier-file and --check do not go together' ;;
    drop-role) allowed --confirm --pg-port ;;
    *) usage >&2; exit 2 ;;
esac
octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
ipv4="^($octet\\.){3}$octet\$"
[[ $APP_IP =~ $ipv4 ]] || usage_error "--app-ip must be an IPv4 address: $APP_IP"
if [[ ! $PG_PORT =~ ^[1-9][0-9]{0,4}$ ]] || ((10#$PG_PORT > 65535)); then
    usage_error "--pg-port must be a port number, 1-65535: $PG_PORT"
fi
[[ $DEPLOY_DIR == /* ]] || usage_error '--dir must be an absolute path'
[[ ! -L ${DEPLOY_DIR%/} ]] || usage_error '--dir must not be a symbolic link'
DEPLOY_DIR=$(realpath -m -- "$DEPLOY_DIR")
[[ $DEPLOY_DIR != / ]] || usage_error '--dir cannot be /'
# The identity file is line-based, and the paths go into messages and Docker arguments.
[[ $DEPLOY_DIR =~ ^/[A-Za-z0-9._/-]+$ ]] || usage_error '--dir may hold only letters, digits, ., _, - and /'
STAGE=$DEPLOY_DIR/.stage
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'

case $COMMAND in
    role) cmd_role ;;
    drop-role) cmd_drop_role ;;
    *)
        if ! command -v docker > /dev/null || ! docker compose version > /dev/null 2>&1; then
            die 'Docker with the Compose plugin is required: sudo bash install-dependencies.sh --role app'
        fi
        TEMP_DIR=$(mktemp -d)
        case $COMMAND in
            install) cmd_install ;;
            status) cmd_status ;;
            uninstall) cmd_uninstall ;;
        esac ;;
esac
