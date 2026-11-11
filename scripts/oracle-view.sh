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

RELEASE=oracle-view-grey-v1.0.6
# Published to the private registry only: every account that can pull it can read the secrets in it.
IMAGE=perodua-deploy.novutal.com/oracle-view:grey-v1.0.6@sha256:317f94236da7c3233b8047c0c5c22066e5c0fdb6e6e27dd506982d2b66050e4a
REGISTRY=${IMAGE%%/*}
REPOSITORY=${IMAGE%%[:@]*}
DIGEST=${IMAGE##*@}
PINNED=$REPOSITORY@$DIGEST   # finds the pinned image by its digest, whatever its tag (install --no-pull)
STREAM_TAG=$REPOSITORY:${DIGEST/:/-}   # the tag the Mac streams the pin under: one for each digest, never moved
PLATFORM=linux/amd64      # the platform of the image and of the App server, for the pull on the Mac
IMAGE_UID=10001           # the image's unprivileged user: it reads the users file
PROJECT=oracle-view       # the Compose project, its service and the container name
VOLUME=oracle-view_oracle-view-state   # mapping edits, SQL audit, login device key
PORT=8797                 # on 127.0.0.1 only; the kit route publishes it
ROUTE='stgiss.perodua.com.my   /dev/oracle-view/   8797   strip,http'
ROUTES_FILE=/etc/perodua-https/routes.conf
DEPLOY_DIR=/opt/perodua-oracle-view
FIRST_USERS=(perodua:admin pm:editor)
# The RP side is fixed by rp_targets.json in the image: host, database and role. The
# database is the one of the grey App (owner decision of 2026-11-07). The SQL of role,
# role --check and drop-role reads it as the psql variable ovb_db.
RP_DATABASE=perodua_oracle2
# The database of the releases before grey-v1.0.2 (ovb_old_db): role and drop-role remove
# the rights of the role there.
OLD_RP_DATABASE=perodua_oracle
RP_ROLE=oracle_view_rp
# The schema of the staging tables of Copy to RP (copy design, section 4.3) in RP_DATABASE:
# the role owns it. drop-role drops it with its tables and their data.
STAGE_SCHEMA=ovb_stage
APP_IP=192.168.23.13      # the App server, as the DB server sees it
PG_PORT=5432
SETTINGS=(OVB_MEM_LIMIT OVB_PIDS_LIMIT OVB_CPUS OVB_RP_SSLMODE)
OVB_MEM_LIMIT=1g OVB_PIDS_LIMIT=256 OVB_CPUS=1.5 OVB_RP_SSLMODE=require
# Optional, in seconds: without a value the container gets no such variable, and the
# default of the image applies (300 s). The image reads both (5-3600).
OPTIONAL=(OVB_LIVE_TIMEOUT OVB_SQL_TIMEOUT)
OVB_LIVE_TIMEOUT='' OVB_SQL_TIMEOUT=''
COMMAND='' CONFIG='' NON_INTERACTIVE=0 NO_PULL=0 CONFIRM='' PURGE=0 VERIFIER_FILE='' CHECK_ONLY=0 DROP_STAGING=0
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
       sudo bash oracle-view.sh install [--config PATH] [--non-interactive] [--no-pull] [--dir PATH]
       sudo bash oracle-view.sh status [--non-interactive] [--dir PATH]
       sudo bash oracle-view.sh uninstall [--purge] [--confirm oracle-view] [--non-interactive] [--dir PATH]
Usage, on the DB server:
       sudo bash oracle-view.sh role --verifier-file FILE [--app-ip ADDRESS] [--pg-port PORT] [--non-interactive]
       sudo bash oracle-view.sh role --check [--app-ip ADDRESS] [--pg-port PORT] [--non-interactive]
       sudo bash oracle-view.sh drop-role [--confirm oracle_view_rp] [--drop-staging] [--pg-port PORT] [--non-interactive]

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
--non-interactive  Or KIT_NON_INTERACTIVE=1 in the environment (sudo env
           KIT_NON_INTERACTIVE=1 bash ...), for every command: nothing is asked
           and the terminal is never read. uninstall and drop-role then need
           --confirm, and install stops when the registry wants a login (run
           docker login first, or use --no-pull). KIT_NON_INTERACTIVE=0 or no
           value: ask on a terminal; another value is a usage error.
--no-pull  Or OVB_NO_PULL=1 in the environment (sudo env OVB_NO_PULL=1 bash ...),
           or KIT_NO_PULL=1, the variable of every kit script (0 or no value:
           the pull as before; another value is a usage error). When both
           variables have a value, the values must be the same.
           install never pulls, never logs in to the registry and never asks:
           the pinned image must already be on this server, streamed from the
           Mac (bash oracle-view-stream.sh SSH_HOST there). install finds it by
           its digest reference and checks that its registry digest, and on the
           containerd image store its ID, is the pinned digest; then it starts
           the image once, with no network. When it is missing, or does not
           start, install stops before any change and prints the command for
           the Mac. On the classic image store an image from docker load has no
           registry digest: install stops there too.
status     The image, whether the pinned image is on this server, the settings,
           the users (no password hashes), the state volume and the health of the
           container.
uninstall  Removes the container and the files install made in --dir. The state
           volume (mapping edits, SQL audit), the users file and the image stay;
           --purge removes them too. Type the project name to confirm, or pass it
           with --confirm.
role       Creates or updates the role oracle_view_rp on the database $RP_DATABASE
           with the SCRAM-SHA-256 verifier in --verifier-file (the image build
           wrote it; never the password), and its grants: read and write on the
           tables, read-only by default, and the schema $STAGE_SCHEMA for the
           staging tables of Copy to RP (the role owns it and has USAGE and
           CREATE there). It makes the role a member of the owner of
           $RP_DATABASE WITH INHERIT TRUE, SET FALSE (owner decision of
           2026-11-09): every right of that owner (DDL, DROP and TRUNCATE on the
           Odoo tables, CREATE in public), but no SET ROLE to it. A membership
           is for the whole server: the role also gets the rights of that owner
           on its other databases (CONNECT, ALTER DATABASE, DROP DATABASE;
           pg_hba.conf decides the logins) and the memberships of that owner. It
           stops before any change when the owner of $RP_DATABASE or of
           $OLD_RP_DATABASE is a superuser, or when oracle_view_rp owns
           $RP_DATABASE, $OLD_RP_DATABASE, another database or a tablespace. It
           ends the sessions of the role in $OLD_RP_DATABASE (the database of the
           earlier releases) and removes its rights there. The objects of the role
           there go to the owner of that database and stay. Meanwhile the role has
           NOLOGIN; it gets its LOGIN back, also when the command fails. Then it
           checks the rights (also that the role has no right of its own on
           another database), names the objects of the role outside $STAGE_SCHEMA, the other
           databases of the owner and the other roles of which the role is a
           member (INFO lines), lists the tables in $STAGE_SCHEMA with their
           sizes, shows SHOW ssl and whether dblink or postgres_fdw is
           installed, and checks pg_hba.conf for
           the App server (--app-ip, default 192.168.23.13). It never edits
           pg_hba.conf: it prints the line to add.
           --check: the checks only, after a pg_hba.conf change.
drop-role  Ends the sessions of oracle_view_rp and drops the role and its grants
           in $RP_DATABASE and $OLD_RP_DATABASE. It also drops the schema
           $STAGE_SCHEMA with its staging tables and their data: when
           $STAGE_SCHEMA holds tables, it lists them and stops unless
           --drop-staging is given. The other objects of the role in
           $RP_DATABASE and $OLD_RP_DATABASE (what the SQL console made, a table
           in public for example) stay: it gives them to the owner of that
           database and names them. It stops before any change when
           oracle_view_rp owns one of these databases: a different owner is
           needed first. It also stops before any change when the owner of one
           of them is a superuser, or when oracle_view_rp owns another database
           or a tablespace. When it stops, the role keeps its LOGIN. Rows that
           the SQL console changed stay changed. Type the role name to confirm,
           or pass it with --confirm.
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
confirm() {   # $1: the word to type
    if [[ -z $CONFIRM ]]; then
        read_terminal CONFIRM "Type $1 to confirm: " \
            "A non-interactive run, or a run without a terminal, asks nothing: pass --confirm $1. Nothing was changed." \
            || die 'Cancelled'
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
            OVB_MEM_LIMIT|OVB_PIDS_LIMIT|OVB_CPUS|OVB_RP_SSLMODE|OVB_LIVE_TIMEOUT|OVB_SQL_TIMEOUT)
                printf -v "$key" '%s' "${line#*=}" ;;
            *) die "$file: unknown key $key" ;;
        esac
    done < "$file"
}

problem() {   # $1 key, $2 value: prints why the value cannot be used, nothing when it can
    local value=$2 memory='^([1-9][0-9]{0,5})([mg])$' count='^[1-9][0-9]{0,4}$'
    local cpus='^[0-9]{1,2}(\.[0-9]{1,2})?$' zero='^0+(\.0+)?$' seconds='^[1-9][0-9]{0,3}$'
    case $1 in
        OVB_MEM_LIMIT)
            if [[ ! $value =~ $memory ]]; then echo 'a memory size such as 1g or 768m'
            elif [[ ${BASH_REMATCH[2]} == m ]] && ((10#${BASH_REMATCH[1]} < 256)); then echo 'at least 256m'
            fi ;;
        OVB_PIDS_LIMIT) [[ $value =~ $count ]] && ((10#$value >= 32 && 10#$value <= 32768)) || echo 'a number of processes, 32-32768' ;;
        OVB_CPUS) [[ $value =~ $cpus && ! $value =~ $zero ]] || echo 'a number of CPUs such as 1.5 (0.01-99.99)' ;;
        OVB_RP_SSLMODE) [[ $value == require || $value == prefer ]] || echo 'require, or prefer when the DB server has ssl off (clear text)' ;;
        OVB_LIVE_TIMEOUT|OVB_SQL_TIMEOUT)
            [[ -z $value ]] || { [[ $value =~ $seconds ]] && ((10#$value >= 5 && 10#$value <= 3600)); } \
                || echo 'a number of seconds, 5-3600, or no value for the default of the image (300)' ;;
    esac
}

check_settings() {
    local key reason
    for key in "${SETTINGS[@]}" "${OPTIONAL[@]}"; do
        reason=$(problem "$key" "${!key}")
        [[ -z $reason ]] || die "$key=${!key} cannot be used: it must be $reason"
    done
}

# ---------------------------------------------------------------- image
check_engine() {   # the health check of compose.yml uses start_interval: Docker Engine API 1.44 (Docker 25) or later
    local api
    api=$(docker version --format '{{.Server.APIVersion}}' 2> /dev/null) || api=''
    [[ $api =~ ^([0-9]+)\.([0-9]+)$ ]] || die "The Docker engine does not answer (docker version): start it with sudo systemctl enable --now docker. Nothing was changed."
    if ((10#${BASH_REMATCH[1]} < 1 || (10#${BASH_REMATCH[1]} == 1 && 10#${BASH_REMATCH[2]} < 44))); then
        die "The Docker engine has API $api; the health check of the tool (start_interval) needs API 1.44 (Docker 25) or later. Update Docker first (sudo apt-get install --only-upgrade docker-ce docker-ce-cli containerd.io docker-compose-plugin; the containers of this server restart). Nothing was changed."
    fi
}
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
        read_terminal username 'Registry username (blank cancels): ' \
            "The registry login needs a terminal (or run docker login $REGISTRY first)" || die 'Cancelled'
        [[ -n $username ]] || die 'Cancelled'
        [[ $username =~ $name ]] || die 'Invalid registry username'
        read_terminal -s -k token 'Registry password (hidden; blank cancels): ' \
            "The registry login needs a terminal (or run docker login $REGISTRY first)" || die 'Cancelled'
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
    docker run --rm --pull never --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --user "$IMAGE_UID:$IMAGE_UID" --entrypoint python "$IMAGE" -c ''
}
check_pinned_image() {   # --no-pull, before any change: no pull, no login and no question
    local store found=0 out
    store=$(image_store)
    [[ -n $store ]] || die 'The Docker engine does not answer docker info. Nothing was changed.'
    pinned_here "$store" || found=$?
    case $found in
        0) out=$(starts 2>&1) \
               || die "The pinned image on this server does not start: $(printf '%s\n' "$out" | tail -n 1). Remove it (sudo docker image rm $PINNED), stream the pinned image again, then run install again. Nothing was changed."
           printf 'Pinned image on this server (%s image store), not pulled: %s\n' "$store" "$PINNED"
           return 0 ;;
        2) die "The image that $PINNED finds on this server does not have the pinned digest as its registry digest (and, on the containerd image store, as its ID). Remove it (sudo docker image rm $PINNED), stream the pinned image again, then run install again. Nothing was changed." ;;
    esac
    [[ $store == containerd ]] || die "The pinned image is not on this server, and --no-pull does not pull it: $PINNED. The Docker engine of this server uses the classic image store: there an image from docker load has no registry digest, so install cannot check it against the pinned digest and does not use it. Switch the engine to the containerd image store first (README.md, Oracle Field Browser, Install from a streamed image), then stream the image. Nothing was changed."
    # docker save of a digest reference writes no image name, and docker load then keeps a
    # dangling image that the digest reference does not find; a release tag moves to a later
    # pin: the Mac saves the stream tag, one for each digest.
    die "$(printf '%s\n' "The pinned image is not on this server, and --no-pull does not pull it: $PINNED" \
        "Stream it from the Mac (it has the registry login and the containerd image store). There, SSH_HOST is the ssh name of this server (host name $(uname -n)):" \
        "  bash scripts/oracle-view-stream.sh SSH_HOST" \
        'or with Docker alone:' \
        "  docker pull --platform $PLATFORM $IMAGE && docker tag $PINNED $STREAM_TAG && docker save $STREAM_TAG | ssh SSH_HOST 'sudo -n docker load'" \
        'Then run this install again. Nothing was changed.')"
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
        line=$(printf '%s\n' "$password" | docker run --rm -i --pull never --network none --read-only --cap-drop ALL \
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
    local image=$1 key optional=''
    for key in "${OPTIONAL[@]}"; do   # only the time-outs with a value: else the default of the image applies
        [[ -z ${!key} ]] || optional+="      $key: \"${!key}\""$'\n'
    done
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
${optional}    mem_limit: "$OVB_MEM_LIMIT"
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
        for key in "${OPTIONAL[@]}"; do [[ -z ${!key} ]] || printf '%s=%s\n' "$key" "${!key}"; done
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

route_listed() { grep -qsE '^[^#]*/dev/oracle-view/' "$ROUTES_FILE"; }   # a commented example is not a route

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
    check_engine
    claim_project
    ((NO_PULL == 0)) || check_pinned_image
    install -d -m 0700 "$DEPLOY_DIR"
    is_ours || printf 'DIR=%s\n' "$DEPLOY_DIR" > "$DEPLOY_DIR/$IDENTITY"
    make_stage
    if [[ -n $(ss -ltnH "sport = :$PORT") ]] && ! owns_port; then
        die "Port $PORT is already used by another program on this server"
    fi
    ((NO_PULL)) || pull_image
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

timeout_text() { if [[ -n $1 ]]; then printf '%s s' "$1"; else printf '300 s (the default of the image)'; fi; }

cmd_status() {
    local image users store found=0
    open_deployment
    image=$(sed -n 's/^    image: //p' "$DEPLOY_DIR/compose.yml")
    if [[ $image == "$IMAGE" ]]; then
        printf 'Release:  %s (the pinned image)\n' "$RELEASE"
    else
        printf 'Release:  not the pinned %s: run install to change to it\n' "$RELEASE"
    fi
    printf 'Image:    %s\n' "$image"
    store=$(image_store)
    if [[ -z $store ]]; then
        printf 'Local:    not checked (docker info does not answer)\n'
    else
        pinned_here "$store" || found=$?
        case $found in
            0) printf 'Local:    the pinned image is on this server (%s image store)\n' "$store" ;;
            1) printf 'Local:    the pinned image is not on this server: install pulls it, install --no-pull needs it streamed first\n' ;;
            *) printf 'Local:    the image that %s finds here does not have the pinned digest\n' "$PINNED" ;;
        esac
    fi
    printf 'Listens:  127.0.0.1:%s (this server only)\n' "$PORT"
    printf 'Settings: memory %s, %s processes, %s CPUs, TLS to the RP database %s (%s/oracle-view.env)\n' \
        "$OVB_MEM_LIMIT" "$OVB_PIDS_LIMIT" "$OVB_CPUS" "$OVB_RP_SSLMODE" "$DEPLOY_DIR"
    printf 'Time-outs: live Oracle calls %s, SQL console %s\n' "$(timeout_text "$OVB_LIVE_TIMEOUT")" "$(timeout_text "$OVB_SQL_TIMEOUT")"
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

join_list() { local list; list=$(printf '%s, ' "$@"); printf '%s' "${list%, }"; }

cmd_uninstall() {
    local id images
    local -a removed=() kept=()
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
        removed=("the $PROJECT container" "the files of install")
        if docker volume inspect "$VOLUME" > /dev/null 2>&1; then
            if docker volume rm "$VOLUME" > /dev/null; then
                removed+=("the state volume $VOLUME")
            else
                kept+=("the state volume $VOLUME")
            fi
        fi
        images=$(docker image ls --quiet "$REPOSITORY" | sort -u) || { kept+=("the images of $REPOSITORY (docker image ls failed)"); images=''; }
        for id in $images; do
            if docker image rm --force "$id" > /dev/null; then
                removed+=("the image $id")
            else
                kept+=("the image $id")
            fi
        done
        if [[ -e $DEPLOY_DIR/users || -L $DEPLOY_DIR/users ]]; then
            if rm -f -- "$DEPLOY_DIR/users"; then removed+=("the users file"); else kept+=("$DEPLOY_DIR/users"); fi
        fi
        if ((${#kept[@]})); then
            # The identity stays, so that uninstall --purge can be run again.
            printf 'Removed: %s.\n' "$(join_list "${removed[@]}")"
            printf 'NOT removed: %s (see above).\n' "$(join_list "${kept[@]}")" >&2
            die "uninstall --purge did not finish. Fix the cause, then run again: sudo bash oracle-view.sh uninstall --purge --confirm $PROJECT --dir $DEPLOY_DIR"
        fi
        rm -f -- "$DEPLOY_DIR/${IDENTITY:?}"
        if rmdir -- "$DEPLOY_DIR" 2> /dev/null; then
            removed+=("$DEPLOY_DIR")
            printf 'Removed: %s.\n' "$(join_list "${removed[@]}")"
        else
            printf 'Removed: %s.\n%s stays: it holds files install did not create.\n' "$(join_list "${removed[@]}")" "$DEPLOY_DIR"
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
pg_sql() {   # a SQL script on stdin, with the database names as psql variables, then more options
    pg postgres -q -v "ovb_db=$RP_DATABASE" -v "ovb_old_db=$OLD_RP_DATABASE" "$@" -f -
}
# role and drop-role give the role NOLOGIN while they clean up (no session of the role can
# make an object between REASSIGN OWNED BY and DROP OWNED BY, which would drop it). When
# their SQL stops, the role gets back the LOGIN that it had before the run.
LOGIN_BEFORE=none
login_before() {   # true, false, or none (no role)
    LOGIN_BEFORE=$(pg_value postgres "SELECT coalesce((SELECT rolcanlogin::text FROM pg_roles WHERE rolname = '$RP_ROLE'), 'none')") \
        || die "Reading the role $RP_ROLE failed (above). Nothing was changed."
}
login_back() {
    [[ $LOGIN_BEFORE == true ]] || return 0
    cat <<'SQL' | pg_sql || printf 'WARNING: %s may have NOLOGIN now. As postgres: ALTER ROLE %s LOGIN;\n' "$RP_ROLE" "$RP_ROLE" >&2
-- oracle-view.sh: login-back
SELECT count(*) > 0 AS ovb_back FROM pg_roles WHERE rolname = 'oracle_view_rp' AND NOT rolcanlogin \gset
\if :ovb_back
ALTER ROLE oracle_view_rp LOGIN;
\echo oracle_view_rp has its LOGIN again, as before this run.
\endif
SQL
}
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

# Before any change, role and drop-role read the owners of ovb_db and ovb_old_db and the
# shared objects of oracle_view_rp; the psql variables ovb_old (the commands clean
# ovb_old_db) come first. The objects of the role go to the owner of each database
# (REASSIGN_SQL): never to a superuser, which would run them (a SECURITY DEFINER function,
# for example), and never to the role itself. REASSIGN OWNED BY also gives each database
# and tablespace of the role (pg_shdepend lists them with dbid 0) to that owner, and names
# none of them. So each of these stops the command before any change.
OWNERS_SQL=$(cat <<'SQL'
SELECT coalesce(max(r.rolname::text) FILTER (WHERE d.datname = :'ovb_db'), '') AS ovb_owner,
       coalesce(bool_or(r.rolsuper) FILTER (WHERE d.datname = :'ovb_db'), false) AS ovb_owner_super,
       coalesce(max(r.rolname::text) FILTER (WHERE d.datname = :'ovb_old_db'), '') AS ovb_old_owner,
       :'ovb_old'::boolean AND coalesce(bool_or(r.rolsuper) FILTER (WHERE d.datname = :'ovb_old_db'), false)
         AS ovb_old_owner_super,
       coalesce(bool_or(r.rolname = 'oracle_view_rp'), false) AS ovb_owner_self,
       coalesce(string_agg(d.datname, ' and ' ORDER BY d.datname) FILTER (WHERE r.rolname = 'oracle_view_rp'), '')
         AS ovb_owned
  FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
 WHERE d.datname IN (:'ovb_db', :'ovb_old_db') \gset
SELECT count(*) > 0 AS ovb_rp_shared,
       coalesce(string_agg(o.type || ' ' || o.identity, ', ' ORDER BY o.type, o.identity), '') AS ovb_rp_shared_list
  FROM pg_shdepend s CROSS JOIN LATERAL pg_identify_object(s.classid, s.objid, 0) o
 WHERE s.dbid = 0 AND s.deptype = 'o' AND s.refclassid = 'pg_authid'::regclass
   AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp') \gset
SQL
)

# The objects of oracle_view_rp in the current database (what the SQL console made with the
# rights of the owner of the database, for example a table in public) go to the owner of
# that database and stay; psql names them. Each DROP OWNED BY oracle_view_rp of role and
# drop-role comes right after it, in the same transaction, so it drops none of them (owner
# decision of 2026-11-09). No session of the role can make an object between the two: the
# role has NOLOGIN, and its sessions there have ended. pg_shdepend lists each object of the
# role (an index or a TOAST table belongs to its table); REASSIGN OWNED BY does not move a
# default privilege or a user mapping, and DROP OWNED BY removes them. The owner and the
# shared objects of the role are read again here (OWNERS_SQL read them before any change):
# when the role owns the database, when its owner is a superuser, or when the role owns a
# database or a tablespace, this stops, and its transaction changes nothing.
REASSIGN_SQL=$(cat <<'SQL'
SELECT count(*) AS ovb_kept, count(*) > 0 AS ovb_kept_any, current_database() AS ovb_kept_db,
       (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) AS ovb_kept_owner,
       (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()) = 'oracle_view_rp'
         AS ovb_kept_self,
       (SELECT r.rolsuper FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
         WHERE d.datname = current_database()) AS ovb_kept_super,
       EXISTS (SELECT 1 FROM pg_shdepend x
                WHERE x.dbid = 0 AND x.deptype = 'o' AND x.refclassid = 'pg_authid'::regclass
                  AND x.refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')) AS ovb_kept_shared,
       coalesce(string_agg('    ' || o.type || ' ' || o.identity, E'\n' ORDER BY o.type, o.identity), '') AS ovb_kept_list
  FROM pg_shdepend s CROSS JOIN LATERAL pg_identify_object(s.classid, s.objid, s.objsubid) o
 WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
   AND s.deptype = 'o' AND s.refclassid = 'pg_authid'::regclass
   AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')
   AND s.classid NOT IN ('pg_default_acl'::regclass, 'pg_user_mapping'::regclass) \gset
\if :ovb_kept_self
\echo oracle_view_rp owns :ovb_kept_db: its objects there cannot go to another owner. A different owner is needed first.
DO $$ BEGIN RAISE EXCEPTION 'oracle_view_rp owns the database: give it a different owner first'; END $$;
\elif :ovb_kept_super
\echo The owner of :ovb_kept_db is :ovb_kept_owner, a superuser: the objects of oracle_view_rp there do not go to it.
\echo Nothing was changed in :ovb_kept_db.
DO $$ BEGIN RAISE EXCEPTION 'the owner of the database is a superuser: give it an owner that is not a superuser first'; END $$;
\elif :ovb_kept_shared
\echo oracle_view_rp owns a database or a tablespace: REASSIGN OWNED BY would give it to :ovb_kept_owner.
\echo Nothing was changed in :ovb_kept_db.
DO $$ BEGIN RAISE EXCEPTION 'oracle_view_rp owns a database or a tablespace: give it a different owner first'; END $$;
\endif
SELECT format('REASSIGN OWNED BY oracle_view_rp TO %I', :'ovb_kept_owner') \gexec
\if :ovb_kept_any
\echo :ovb_kept objects of oracle_view_rp stay in :ovb_kept_db, now owned by :ovb_kept_owner, the owner of the database:
\echo :ovb_kept_list
\endif
SQL
)

role_sql() {   # after a \set ovb_verifier line: create or update the role and its grants
    cat <<'SQL'
-- oracle-view.sh: role
\set QUIET on
-- The statements that hold the verifier stay out of the server log (also the sampled
-- statements and transactions, and when one fails), out of pg_stat_statements and out
-- of the error text of psql. Without pg_stat_statements, its SET only makes a placeholder.
\set VERBOSITY terse
\set SHOW_CONTEXT never
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET pg_stat_statements.track_utility = off;
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_ro') AS ovb_ro,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_rp') AS ovb_rp,
       coalesce((SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'), false) AS ovb_rp_login,
       :'ovb_old_db' <> :'ovb_db'
         AND EXISTS (SELECT 1 FROM pg_database WHERE datname = :'ovb_old_db'
                       AND datallowconn AND datconnlimit <> -2) AS ovb_old \gset
-- The role gets the rights of the owner of ovb_db through a membership in that owner
-- (owner decision of 2026-11-09). The rights of a superuser owner would be every right
-- on the server. A role cannot be a member of itself, and the objects of the role in
-- ovb_old_db go to the owner of that database (REASSIGN_SQL): never to a superuser, never
-- to the role itself, and with no database or tablespace of the role. Then the run stops
-- here, before any change.
SQL
    printf '%s\n' "$OWNERS_SQL"
    cat <<'SQL'
\if :ovb_owner_super
\echo The owner of :ovb_db is :ovb_owner, a superuser. oracle_view_rp gets the rights of the owner of the database
\echo (owner decision of 2026-11-09): the rights of a superuser are every right on the server. Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'the owner of the RP database is a superuser: give the database an owner that is not a superuser (the role of the App), then run role again'; END $$;
\elif :ovb_old_owner_super
\echo The owner of :ovb_old_db is :ovb_old_owner, a superuser. role gives the objects of oracle_view_rp there to the owner
\echo of that database (REASSIGN OWNED BY), and a superuser would run them (a SECURITY DEFINER function, for example).
\echo Give :ovb_old_db an owner that is not a superuser (the role of the App), or drop it. Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'the owner of the old database is a superuser: give it an owner that is not a superuser, then run role again'; END $$;
\elif :ovb_owner_self
\echo oracle_view_rp owns :ovb_owned. A different owner is needed first (the role of the App, not a superuser): a role
\echo cannot be a member of itself, and the objects of oracle_view_rp go to the owner of the database. Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'oracle_view_rp owns the RP database or the old database: give it a different owner first, then run role again'; END $$;
\elif :ovb_rp_shared
\echo oracle_view_rp owns :ovb_rp_shared_list. REASSIGN OWNED BY gives each database and tablespace of the
\echo role to the owner of the database, without a word. Give each one a different owner first (ALTER DATABASE or ALTER
\echo TABLESPACE, as postgres). Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'oracle_view_rp owns a database or a tablespace: give it a different owner first, then run role again'; END $$;
\endif
\if :ovb_ro
\echo The read-only role oracle_view_ro of an earlier bundle: its sessions end and it is dropped
ALTER ROLE oracle_view_ro NOLOGIN;
SELECT count(pg_terminate_backend(pid)) AS ended_sessions FROM pg_stat_activity WHERE usename = 'oracle_view_ro' \gset
\endif
-- The releases before grey-v1.0.2 gave the role its rights in ovb_old_db. The image does
-- not read that database any more: the sessions of the role there end, and DROP OWNED BY
-- removes its rights there. DROP OWNED BY also revokes the rights of the role on every
-- database (CONNECT, TEMPORARY: a database is a shared object), and deploy-db.sh revokes
-- CONNECT from PUBLIC. So ovb_db gets them again in the same transaction: a stop between
-- the two changes nothing, and the tool keeps its way in. When the owner of ovb_db owns
-- ovb_old_db too, the role has the rights of that owner there through its membership: line
-- 17 of the rights check names such a database, and the role can make objects there. They
-- go to the owner of ovb_old_db before DROP OWNED BY (REASSIGN_SQL): the cleanup drops none
-- of them. A table that a session of the role committed between REASSIGN OWNED BY and DROP
-- OWNED BY would be dropped (READ COMMITTED): so the role first gets NOLOGIN (no new
-- session), and its sessions there end, each one waited for (up to 5 s), so that no
-- temporary table of an ending session is an object of the role either. After the COMMIT
-- they end again (a session that logged in right before the NOLOGIN), and the role gets its
-- LOGIN back when it had it; when the run stops, oracle-view.sh gives it back. No old
-- database, or one that does not accept connections (ALLOW_CONNECTIONS false, or a database
-- half dropped): nothing to do here, and check 12 still lists a right that stays there.
\if :ovb_old
\if :ovb_rp
\echo The rights of oracle_view_rp in :ovb_old_db, the database of the earlier releases: its sessions there end and the rights are removed
ALTER ROLE oracle_view_rp NOLOGIN;
SELECT count(pg_terminate_backend(pid, 5000)) AS ended_sessions FROM pg_stat_activity
 WHERE usename = 'oracle_view_rp' AND datname = :'ovb_old_db' \gset
\endif
\connect :ovb_old_db
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET pg_stat_statements.track_utility = off;
BEGIN;
\if :ovb_ro
DROP OWNED BY oracle_view_ro;
\endif
\if :ovb_rp
SQL
    printf '%s\n' "$REASSIGN_SQL"
    cat <<'SQL'
DROP OWNED BY oracle_view_rp;
GRANT CONNECT, TEMPORARY ON DATABASE :"ovb_db" TO oracle_view_rp;
\endif
COMMIT;
\if :ovb_rp
SELECT count(pg_terminate_backend(pid)) AS ended_sessions FROM pg_stat_activity
 WHERE usename = 'oracle_view_rp' AND datname = :'ovb_old_db' \gset
\if :ovb_rp_login
ALTER ROLE oracle_view_rp LOGIN;
\endif
\endif
\endif
\if :ovb_ro
\connect :ovb_db
DROP OWNED BY oracle_view_ro;
\endif
\connect postgres
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = panic;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;
SET pg_stat_statements.track_utility = off;
\if :ovb_ro
DROP ROLE oracle_view_ro;
\endif
\if :ovb_rp
ALTER ROLE oracle_view_rp LOGIN PASSWORD :'ovb_verifier' CONNECTION LIMIT 10;
\else
CREATE ROLE oracle_view_rp LOGIN PASSWORD :'ovb_verifier' CONNECTION LIMIT 10;
\endif
\unset ovb_verifier
\set VERBOSITY default
\set SHOW_CONTEXT errors
-- Owner decision of 2026-11-09 (it replaces the one of 2026-11-06): the SQL console has
-- every right of the owner of this test database, read-only by default. The role is a
-- member of that owner WITH INHERIT TRUE, SET FALSE: it has the rights of the owner (DDL,
-- DROP and TRUNCATE on the Odoo tables, CREATE in public and in new schemas), but it
-- cannot SET ROLE to the owner, so what it makes is its own. No superuser and no other
-- role attribute. A membership is for the whole server: the role has the same rights on
-- the other databases of that owner (line 18 of the rights check names them), and it
-- gets the memberships of that owner too (line 19).
ALTER ROLE oracle_view_rp NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE oracle_view_rp SET default_transaction_read_only = on;
ALTER ROLE oracle_view_rp SET statement_timeout = '30s';
ALTER ROLE oracle_view_rp SET idle_in_transaction_session_timeout = '60s';
-- The temporary files of one session (the file import of the SQL console: a sort, a RETURNING or a
-- set-returning function can spill to the temporary disk of the server). temp_file_limit is a
-- superuser setting: a session of the role cannot change it (owner choice of 2026-11-09: 8 GB).
ALTER ROLE oracle_view_rp SET temp_file_limit = '8GB';
GRANT CONNECT, TEMPORARY ON DATABASE :"ovb_db" TO oracle_view_rp;
-- The owner is read again here: an owner that became a superuser after the check at the
-- start gets no member (check 11 then fails). A later run grants with the same grantor,
-- so it sets SET FALSE again; client_min_messages: no NOTICE when nothing changes.
SET client_min_messages = warning;
SELECT format('GRANT %I TO oracle_view_rp WITH INHERIT TRUE, SET FALSE', r.rolname)
  FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
 WHERE d.datname = :'ovb_db' AND NOT r.rolsuper AND r.rolname <> 'oracle_view_rp' \gexec
RESET client_min_messages;
\connect :ovb_db
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
-- Copy to RP (copy design, section 4.3): the staging tables go to a schema of their own,
-- owned by the role, apart from what the SQL console makes in public, so that drop-role
-- can drop them with the schema. PUBLIC has no right there. A schema ovb_stage of another
-- owner stays as it is (no grant there), and check 13 of the rights fails.
-- client_min_messages: no NOTICE on later runs.
SET client_min_messages = warning;
CREATE SCHEMA IF NOT EXISTS ovb_stage AUTHORIZATION oracle_view_rp;
RESET client_min_messages;
SELECT nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp') AS ovb_stage_own
  FROM pg_namespace WHERE nspname = 'ovb_stage' \gset
\if :ovb_stage_own
REVOKE ALL ON SCHEMA ovb_stage FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA ovb_stage TO oracle_view_rp;
\else
\echo The schema ovb_stage of :ovb_db belongs to another role: it stays as it is, without a grant to oracle_view_rp
\endif
SQL
}

# One line per right, "OK|what" or "FAIL|what", in the database RP_DATABASE, then lines
# 17 to 19, "INFO|what": they inform and never fail. A name in "what" (an object, a
# database or a role that the SQL console can make) may hold a line end or another control
# character: it shows as \uXXXX, so that it cannot add a line or change the count.
# has_table_privilege with a list is true when ANY of the rights is held: each right is checked alone.
ROLE_CHECK_SQL=$(cat <<'SQL'
SELECT CASE WHEN n IN (17, 18, 19) THEN 'INFO' WHEN ok THEN 'OK' ELSE 'FAIL' END || '|' || shown FROM (
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
  SELECT 5, has_database_privilege('oracle_view_rp', current_database(), 'CONNECT'), 'CONNECT on ' || current_database()
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
  -- Checks 9 to 11: the rights of the owner of the database (owner decision of 2026-11-09).
  SELECT 9, count(*) = 0, 'TRUNCATE on every table of public (' || count(*) || ' without)'
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
     AND NOT has_table_privilege('oracle_view_rp', c.oid, 'TRUNCATE')
  UNION ALL
  SELECT 10, has_schema_privilege('oracle_view_rp', 'public', 'CREATE'), 'CREATE in public'
  UNION ALL
  -- A member of the owner, never of a superuser owner: INHERIT (the rights of the owner),
  -- no SET (no SET ROLE to the owner) and no ADMIN (no grant of the owner to others). Each
  -- grantor of the membership has a row in pg_auth_members: each option counts for all.
  -- pg_has_role also follows the paths through other roles: no SET ROLE to the owner and
  -- no ADMIN on it through another role either.
  SELECT 11, coalesce(bool_or(m.inherit_option) AND NOT bool_or(m.set_option) AND NOT bool_or(m.admin_option), false)
             AND NOT o.rolsuper AND o.rolname <> 'oracle_view_rp'
             AND NOT pg_has_role('oracle_view_rp', o.oid, 'SET')
             AND NOT pg_has_role('oracle_view_rp', o.oid, 'MEMBER WITH ADMIN OPTION'),
         'member of ' || o.rolname || ', the owner of ' || current_database()
         || ', with INHERIT TRUE, SET FALSE, ADMIN FALSE'
         || CASE WHEN o.rolsuper THEN ' (' || o.rolname || ' is a superuser)'
                 WHEN o.rolname = 'oracle_view_rp' THEN ' (oracle_view_rp owns the database)'
                 WHEN count(m.member) = 0 THEN ' (it is not a member)'
                 WHEN NOT (bool_or(m.inherit_option) AND NOT bool_or(m.set_option) AND NOT bool_or(m.admin_option))
                 THEN ' (it has INHERIT ' || upper(bool_or(m.inherit_option)::text) || ', SET '
                      || upper(bool_or(m.set_option)::text) || ', ADMIN ' || upper(bool_or(m.admin_option)::text) || ')'
                 WHEN pg_has_role('oracle_view_rp', o.oid, 'SET')
                 THEN ' (it can SET ROLE to ' || o.rolname || ' through another role)'
                 WHEN pg_has_role('oracle_view_rp', o.oid, 'MEMBER WITH ADMIN OPTION')
                 THEN ' (it has ADMIN on ' || o.rolname || ' through another role)'
                 ELSE '' END
    FROM pg_database d JOIN pg_roles o ON o.oid = d.datdba
    LEFT JOIN pg_auth_members m ON m.roleid = d.datdba
                               AND m.member = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')
   WHERE d.datname = current_database()
   GROUP BY o.oid, o.rolname, o.rolsuper
  UNION ALL
  -- A right in another database (a grant, a default privilege or an object there) or on it.
  SELECT 12, count(*) = 0, 'no right on a database other than ' || current_database()
         || coalesce(' (it has rights on: ' || string_agg(d.datname, ', ' ORDER BY d.datname) || ')', '')
    FROM pg_database d
   WHERE d.datname <> current_database()
     AND (EXISTS (SELECT 1 FROM pg_shdepend s
                   WHERE s.dbid = d.oid AND s.refclassid = 'pg_authid'::regclass
                     AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp'))
          OR EXISTS (SELECT 1 FROM aclexplode(d.datacl) a
                      WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')))
  UNION ALL
  -- The schema of the staging tables of Copy to RP: the role owns it. Another schema of the
  -- role (the SQL console can make one) is in line 17.
  SELECT 13, coalesce(bool_or(n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')), false),
         'owns the schema ovb_stage'
         || CASE WHEN count(*) = 0 THEN ' (there is no schema ovb_stage)'
                 WHEN bool_or(n.nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')) THEN ''
                 ELSE ' (the owner of ovb_stage is ' || pg_get_userbyid(min(n.nspowner)) || ')' END
    FROM pg_namespace n WHERE n.nspname = 'ovb_stage'
  UNION ALL
  SELECT 14, coalesce((SELECT has_schema_privilege('oracle_view_rp', oid, 'USAGE')
                              AND has_schema_privilege('oracle_view_rp', oid, 'CREATE')
                         FROM pg_namespace WHERE nspname = 'ovb_stage'), false),
         'USAGE and CREATE on the schema ovb_stage'
  UNION ALL
  SELECT 15, coalesce((SELECT NOT EXISTS (SELECT 1 FROM aclexplode(nspacl) a WHERE a.grantee = 0)
                         FROM pg_namespace WHERE nspname = 'ovb_stage'), false),
         'no right of PUBLIC on the schema ovb_stage'
  UNION ALL
  -- The limit of the temporary files of one session (owner choice of 2026-11-09: 8 GB).
  SELECT 16, coalesce('temp_file_limit=8GB' = ANY (rolconfig), false),
         'temporary files of a session up to 8GB (temp_file_limit=8GB)'
    FROM pg_roles WHERE rolname = 'oracle_view_rp'
  UNION ALL
  -- INFO: every object the role owns in this database (pg_shdepend: also functions, types
  -- and schemas), but the schema ovb_stage and what is in it: what the SQL console made
  -- with the rights of the owner, for example a table in public, and the temporary tables
  -- of open sessions. An index or a TOAST table has no entry: it belongs to its table. A
  -- default privilege and a user mapping are no object of the role (DROP OWNED BY removes
  -- them; REASSIGN OWNED BY of drop-role does not move them).
  SELECT 17, NULL, 'owns ' || count(*) || ' objects outside the schema ovb_stage'
         || coalesce(' (' || array_to_string((array_agg(o.type || ' ' || o.identity ORDER BY o.type, o.identity))[1:5], ', ')
                     || CASE WHEN count(*) > 5 THEN ', ' || count(*) - 5 || ' more' ELSE '' END || ')', '')
    FROM pg_shdepend s CROSS JOIN LATERAL pg_identify_object(s.classid, s.objid, s.objsubid) o
   WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
     AND s.deptype = 'o' AND s.refclassid = 'pg_authid'::regclass
     AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = 'oracle_view_rp')
     AND s.classid NOT IN ('pg_default_acl'::regclass, 'pg_user_mapping'::regclass)
     AND o.schema IS DISTINCT FROM 'ovb_stage'
     AND NOT (s.classid = 'pg_namespace'::regclass AND o.identity = 'ovb_stage')
  UNION ALL
  -- INFO: a membership is for the whole server. The role has the rights of the owner of
  -- this database on each other database of that owner too (CONNECT, ALTER DATABASE, DROP
  -- DATABASE); pg_hba.conf decides where it can log in.
  SELECT 18, NULL, 'other databases of ' || o.rolname || ', with its rights through the membership: '
         || coalesce(string_agg(x.datname, ', ' ORDER BY x.datname), 'none')
    FROM pg_database d JOIN pg_roles o ON o.oid = d.datdba
    LEFT JOIN pg_database x ON x.datdba = d.datdba AND x.oid <> d.oid
   WHERE d.datname = current_database()
   GROUP BY o.rolname
  UNION ALL
  -- INFO: the other roles of which the role is a member, directly or through the owner (it
  -- also gets the memberships of the owner). pg_database_owner: the owner of this database
  -- is its member.
  SELECT 19, NULL, 'other roles of which it is a member (also through ' || o.rolname || '): '
         || coalesce((SELECT string_agg(x.rolname, ', ' ORDER BY x.rolname) FROM pg_roles x
                       WHERE x.rolname NOT IN ('oracle_view_rp', 'pg_database_owner') AND x.oid <> o.oid
                         AND pg_has_role('oracle_view_rp', x.oid, 'MEMBER')), 'none')
    FROM pg_database d JOIN pg_roles o ON o.oid = d.datdba
   WHERE d.datname = current_database()
) checks CROSS JOIN LATERAL (
  SELECT string_agg(CASE WHEN ch ~ '[[:cntrl:]]' THEN '\u' || lpad(to_hex(ascii(ch)), 4, '0') ELSE ch END, ''
                    ORDER BY i) AS shown
    FROM unnest(string_to_array(what, NULL)) WITH ORDINALITY AS c (ch, i)
) escaped ORDER BY n
SQL
)

ROLE_CHECKS=19   # the lines of ROLE_CHECK_SQL
role_checks() {   # prints each right and each INFO line; a missing or an extra right stops the command
    local out result what failed=0 elsewhere=0 stage=0 member=0
    out=$(pg_value "$RP_DATABASE" "$ROLE_CHECK_SQL") || die 'The check of the rights failed (above)'
    [[ $(grep -c . <<< "$out") == "$ROLE_CHECKS" ]] || die "The check of the rights gave an unexpected answer: $out"
    while IFS='|' read -r result what; do
        printf '%-4s %s\n' "$result" "$what"
        [[ $result == OK || $result == INFO ]] && continue
        failed=1
        [[ $what != 'no right on a database other than '* ]] || elsewhere=1
        [[ $what != 'member of '* ]] || member=1
        case $what in "owns the schema $STAGE_SCHEMA"*|*" on the schema $STAGE_SCHEMA") stage=1 ;; esac
    done <<< "$out"
    if ((member)); then
        printf '\n%s gets the rights of the owner of %s through a membership in that owner (owner decision of\n' \
            "$RP_ROLE" "$RP_DATABASE" >&2
        printf '2026-11-09): INHERIT TRUE, SET FALSE, ADMIN FALSE, and the owner is not a superuser. role --verifier-file FILE\n' >&2
        printf 'grants it, and sets SET FALSE on its own grant again. It changes neither ADMIN nor the grant of another\n' >&2
        printf 'grantor (pg_auth_members): revoke such a grant or option by hand, then run role again.\n' >&2
    fi
    if ((stage)); then
        printf '\nThe schema %s of %s holds the staging tables of Copy to RP: %s must own it, with USAGE and CREATE there,\n' \
            "$STAGE_SCHEMA" "$RP_DATABASE" "$RP_ROLE" >&2
        printf 'and PUBLIC must have no right there. role --verifier-file FILE makes it so, except for a schema %s\n' \
            "$STAGE_SCHEMA" >&2
        printf 'of another owner: find out who made it before you change its owner or drop it, then run role again.\n' >&2
    fi
    if ((elsewhere)); then
        printf '\nIn each database of the FAIL line, remove the rights of %s. Run this in that database:\n' "$RP_ROLE" >&2
        printf '    BEGIN; DROP OWNED BY %s; GRANT CONNECT, TEMPORARY ON DATABASE %s TO %s; COMMIT;\n' \
            "$RP_ROLE" "$RP_DATABASE" "$RP_ROLE" >&2
        printf 'DROP OWNED BY alone also removes CONNECT on %s (a database is a shared object): then the tool cannot connect.\n' \
            "$RP_DATABASE" >&2
        printf 'DROP OWNED BY also drops the objects of %s there (a table that the SQL console made, for example). To keep\n' \
            "$RP_ROLE" >&2
        printf 'them, run first in that database: REASSIGN OWNED BY %s TO OWNER; (OWNER: the owner of that database, see \\l).\n' \
            "$RP_ROLE" >&2
        printf 'Then run role again.\n' >&2
    fi
    ((failed == 0)) || die "The role $RP_ROLE does not have the rights above. Nothing in pg_hba.conf was checked."
}

# The staging tables in STAGE_SCHEMA of RP_DATABASE, the tables that drop-role drops (as
# drop_sql counts them): every table and materialized view there when the role owns the
# schema, else those of the role there. "count|total size", then one line for each table.
STAGE_FROM="FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_roles r ON r.rolname = '$RP_ROLE'
 WHERE n.nspname = '$STAGE_SCHEMA' AND c.relkind IN ('r', 'p', 'm') AND (n.nspowner = r.oid OR c.relowner = r.oid)"
# The objects outside STAGE_SCHEMA that depend on an object in it (a view or a foreign key
# in public on a staging table, for example), when the role owns the schema: DROP SCHEMA
# ... CASCADE of drop-role would drop them too, so drop-role stops while there are any.
# An object of a kind that this list does not place counts as outside.
STAGE_OUTSIDE_WITH="WITH s AS (SELECT n.oid FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
                WHERE n.nspname = '$STAGE_SCHEMA' AND r.rolname = '$RP_ROLE'),
     inside (classid, objid) AS (
         SELECT 'pg_namespace'::regclass::oid, s.oid FROM s
         UNION ALL SELECT 'pg_class'::regclass::oid, c.oid FROM pg_class c JOIN s ON c.relnamespace = s.oid
         UNION ALL SELECT 'pg_type'::regclass::oid, t.oid FROM pg_type t JOIN s ON t.typnamespace = s.oid
         UNION ALL SELECT 'pg_proc'::regclass::oid, p.oid FROM pg_proc p JOIN s ON p.pronamespace = s.oid),
     dependent AS (
         SELECT CASE d.classid
                  WHEN 'pg_class'::regclass THEN (SELECT relnamespace FROM pg_class WHERE oid = d.objid)
                  WHEN 'pg_type'::regclass THEN (SELECT typnamespace FROM pg_type WHERE oid = d.objid)
                  WHEN 'pg_proc'::regclass THEN (SELECT pronamespace FROM pg_proc WHERE oid = d.objid)
                  WHEN 'pg_operator'::regclass THEN (SELECT oprnamespace FROM pg_operator WHERE oid = d.objid)
                  WHEN 'pg_constraint'::regclass THEN (SELECT connamespace FROM pg_constraint WHERE oid = d.objid)
                  WHEN 'pg_statistic_ext'::regclass THEN (SELECT stxnamespace FROM pg_statistic_ext WHERE oid = d.objid)
                  WHEN 'pg_default_acl'::regclass THEN (SELECT defaclnamespace FROM pg_default_acl WHERE oid = d.objid)
                  WHEN 'pg_rewrite'::regclass THEN (SELECT c.relnamespace FROM pg_rewrite w JOIN pg_class c
                                                      ON c.oid = w.ev_class WHERE w.oid = d.objid)
                  WHEN 'pg_trigger'::regclass THEN (SELECT c.relnamespace FROM pg_trigger g JOIN pg_class c
                                                      ON c.oid = g.tgrelid WHERE g.oid = d.objid)
                  WHEN 'pg_attrdef'::regclass THEN (SELECT c.relnamespace FROM pg_attrdef a JOIN pg_class c
                                                      ON c.oid = a.adrelid WHERE a.oid = d.objid)
                  WHEN 'pg_policy'::regclass THEN (SELECT c.relnamespace FROM pg_policy p JOIN pg_class c
                                                     ON c.oid = p.polrelid WHERE p.oid = d.objid)
                END AS nsp,
                CASE WHEN d.classid = 'pg_rewrite'::regclass THEN 'pg_class'::regclass::oid ELSE d.classid END AS oclass,
                CASE WHEN d.classid = 'pg_rewrite'::regclass   -- the rule of a view: name the view
                     THEN (SELECT ev_class FROM pg_rewrite WHERE oid = d.objid) ELSE d.objid END AS oobj
           FROM pg_depend d JOIN inside i ON i.classid = d.refclassid AND i.objid = d.refobjid
          WHERE d.deptype <> 'i'),
     outside AS (SELECT DISTINCT o.type || ' ' || o.identity AS obj
                   FROM dependent, pg_identify_object(oclass, oobj, 0) o
                  WHERE nsp IS DISTINCT FROM (SELECT oid FROM s))"
STAGE_TOTAL_SQL="SELECT count(*) || '|' || pg_size_pretty(coalesce(sum(pg_total_relation_size(c.oid)), 0))
  $STAGE_FROM"
STAGE_LIST_SQL="SELECT format('    %-40s %12s  %s', c.relname, pg_size_pretty(pg_total_relation_size(c.oid)),
              pg_get_userbyid(c.relowner))
  $STAGE_FROM
 ORDER BY pg_total_relation_size(c.oid) DESC, c.relname"
STAGE_TABLES=0 STAGE_SIZE=''
stage_tables() {   # lists the staging tables with their sizes; sets STAGE_TABLES and STAGE_SIZE
    local total
    total=$(pg_value "$RP_DATABASE" "$STAGE_TOTAL_SQL") || die "The list of the tables in $STAGE_SCHEMA failed (above)"
    [[ $total =~ ^[0-9]+\|.+$ ]] || die "The list of the tables in $STAGE_SCHEMA gave an unexpected answer: $total"
    STAGE_TABLES=${total%%|*} STAGE_SIZE=${total#*|}
    printf '\n== Staging tables of Copy to RP: the schema %s of %s\n' "$STAGE_SCHEMA" "$RP_DATABASE"
    if ((STAGE_TABLES == 0)); then
        printf 'none: %s holds no table\n' "$STAGE_SCHEMA"
        return 0
    fi
    printf '%s tables, %s on disk:\n' "$STAGE_TABLES" "$STAGE_SIZE"
    printf '    %-40s %12s  %s\n' table size owner
    pg_value "$RP_DATABASE" "$STAGE_LIST_SQL" || die "The list of the tables in $STAGE_SCHEMA failed (above)"
}

stage_outside() {   # stops when objects outside STAGE_SCHEMA depend on it (DROP SCHEMA ... CASCADE drops them)
    local found
    found=$(pg_value "$RP_DATABASE" "$STAGE_OUTSIDE_WITH SELECT obj FROM outside ORDER BY obj") \
        || die "The check of the objects that depend on $STAGE_SCHEMA failed (above)"
    [[ -n $found ]] || return 0
    printf '\nThese objects outside the schema %s depend on an object in it. drop-role drops the schema with CASCADE,\n' \
        "$STAGE_SCHEMA" >&2
    printf 'so it would drop them too, with their data:\n' >&2
    printf '    %s\n' "${found//$'\n'/$'\n    '}" >&2
    printf 'Remove each dependency first (for example DROP VIEW, or ALTER TABLE ... DROP CONSTRAINT), or copy the object.\n' >&2
    die "Objects outside the schema $STAGE_SCHEMA depend on it (above). Nothing was changed."
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

hba_sql() {   # with the psql variables ovb_app, ovb_db and ovb_old_db: does a pg_hba.conf line let the App server in? It changes nothing.
    cat <<'SQL'
-- oracle-view.sh: hba-check
-- pg_hba.conf takes the first rule that matches (type, database, user, address), in the
-- order of rule_number (include files count where they are included). The type depends
-- on the transport: with ssl on, the tool connects with TLS (sslmode require or prefer),
-- so host and hostssl lines match and hostnossl lines do not; with ssl off, host and
-- hostnossl lines match (the tool then needs sslmode prefer). The tool has no Kerberos:
-- a hostgssenc line never matches, a hostnogssenc line does. md5 is OK: for a role with
-- a SCRAM-SHA-256 verifier, PostgreSQL then uses SCRAM.
-- Each field of a line (database, user, address) matches the App server, does not match
-- it, or is unsure: this check cannot read it (database @file, a regular expression /...,
-- sameuser, samerole, samegroup; user +group, @file, /...; address samehost, samenet or a
-- host name). An IPv6 address never matches the IPv4 address of the App server, and is
-- never unsure. A line is "check by hand" when its type can match, each of the three
-- fields matches or is unsure, and at least one is unsure. Such a line above the first
-- matching line gives RESULT: CHECK BY HAND, and so does a line with an error that stops
-- PostgreSQL from loading the file (its type is empty). A line that PostgreSQL loads with
-- a warning (a hostssl line with ssl off) never matches.
\pset footer off
\echo == pg_hba.conf and TLS of this server
SELECT current_setting('hba_file') AS hba_file, current_setting('listen_addresses') AS listen_addresses,
       current_setting('port') AS port, current_setting('password_encryption') AS password_encryption,
       current_setting('ssl') AS ssl;
-- One row per host line that PostgreSQL loads: for each field, _ok (it matches the App
-- server) and _unsure (this check cannot read it).
CREATE TEMPORARY VIEW ovb_hba_fields AS
  SELECT rule_number, file_name, line_number, type, database, user_name, address, netmask, auth_method,
         (type = 'host' OR type = 'hostnogssenc'
          OR (type = 'hostssl' AND current_setting('ssl') = 'on')
          OR (type = 'hostnossl' AND current_setting('ssl') <> 'on')) AS type_ok,
         ('all' = ANY (database) OR :'ovb_db' = ANY (database)) AS database_ok,
         EXISTS (SELECT 1 FROM unnest(database) d
                  WHERE d ~ '^[@/]' OR d IN ('sameuser', 'samerole', 'samegroup')) AS database_unsure,
         ('all' = ANY (user_name) OR 'oracle_view_rp' = ANY (user_name)) AS user_ok,
         EXISTS (SELECT 1 FROM unnest(user_name) u WHERE u ~ '^[+@/]') AS user_unsure,
         -- CASE: the casts run only on IPv4 lines (an IPv6 mask with an IPv4 address is an error).
         (address = 'all'
          OR CASE WHEN address ~ '^[0-9.]+$' AND netmask ~ '^[0-9.]+$'
                  THEN (:'ovb_app'::inet & netmask::inet) = (address::inet & netmask::inet)
                  ELSE false END) AS address_ok,
         -- samehost, samenet or a host name; an IPv4 or IPv6 address is never unsure.
         (address IN ('samehost', 'samenet') OR (address !~ '^([0-9.]+|all)$' AND address !~ ':')) AS address_unsure
    FROM pg_hba_file_rules
   WHERE error IS NULL AND type <> 'local';
CREATE TEMPORARY VIEW ovb_hba_rules AS
  SELECT *,
         coalesce(type_ok AND database_ok AND user_ok AND address_ok, false) AS matches,
         coalesce(type_ok AND (database_ok OR database_unsure) AND (user_ok OR user_unsure)
                  AND (address_ok OR address_unsure) AND NOT (database_ok AND user_ok AND address_ok), false) AS unsure
    FROM ovb_hba_fields;
\echo == The lines that can match the App server :ovb_app, database :ovb_db, user oracle_view_rp
SELECT rule_number, line_number, type, database, user_name, address, netmask, auth_method,
       CASE WHEN matches THEN 'matches' ELSE 'check by hand' END AS result, file_name
  FROM ovb_hba_rules WHERE matches OR unsure ORDER BY rule_number;
SELECT coalesce((SELECT auth_method FROM ovb_hba_rules WHERE matches ORDER BY rule_number LIMIT 1), 'none')
         AS first_auth_method,
       EXISTS (SELECT 1 FROM ovb_hba_rules
                WHERE unsure
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
\echo RESULT: CHECK BY HAND - pg_hba.conf has lines with errors (the list "pg_hba.conf lines with errors"). PostgreSQL does not load such a file, so this check cannot tell which lines it uses.
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
-- A line for the role on the database of the earlier releases gives access without a reason.
\echo == pg_hba.conf lines for oracle_view_rp on :ovb_old_db, the database of the earlier releases (expected: none)
SELECT rule_number, line_number, type, database, user_name, address, netmask, auth_method, file_name
  FROM pg_hba_file_rules
 WHERE error IS NULL AND :'ovb_old_db' <> :'ovb_db' AND :'ovb_old_db' = ANY (database)
   AND 'oracle_view_rp' = ANY (user_name)
 ORDER BY rule_number;
SELECT count(*) > 0 AS ovb_old_lines
  FROM pg_hba_file_rules
 WHERE error IS NULL AND :'ovb_old_db' <> :'ovb_db' AND :'ovb_old_db' = ANY (database)
   AND 'oracle_view_rp' = ANY (user_name) \gset
\if :ovb_old_lines
\echo NOTE: the tool does not read :ovb_old_db any more: a line of the list above lets oracle_view_rp in there without a reason.
\echo Change its database to :ovb_db, or delete it (back up the file first), reload PostgreSQL (no restart) and check again.
\endif
SQL
}

hba_check() {   # returns 0 when the App server gets in, 3 when the line is missing, 4 for a check by hand
    local out hba again reload block status=0 where='above the first line of the list above, or above the first host line'
    local line="host    $RP_DATABASE    $RP_ROLE    $APP_IP/32    scram-sha-256" marker="# BEGIN perodua-db-deploy $RP_DATABASE"
    out=$(hba_sql | pg_sql -v "ovb_app=$APP_IP") || die 'The pg_hba.conf check failed (above)'
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
  1. Correct the lines of the list "pg_hba.conf lines with errors", in the files that it names.
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
-- their sessions, removes their grants and default privileges in ovb_db and in
-- ovb_old_db, the database of the earlier releases, when it exists and accepts
-- connections (DROP OWNED BY: a grant there cannot make DROP ROLE fail), and drops the
-- roles. The rows that the SQL console changed stay changed. In ovb_db it first drops the
-- schema ovb_stage of Copy to RP with its staging tables and their data; with tables there
-- and ovb_drop_staging off (a table made after the check of the script), it stops, gives
-- the role its LOGIN back and drops nothing. It does the same when an object outside
-- ovb_stage depends on an object in it (DROP SCHEMA ... CASCADE would drop that object).
-- Then, in ovb_db and in ovb_old_db, it gives every other object of the role (what the SQL
-- console made with the rights of the owner of the database, for example a table in
-- public) to the owner of that database, names it, and drops none of it (owner decision of
-- 2026-11-09). The changes in each database are one transaction: a stop there changes
-- nothing there, and oracle-view.sh gives the role its LOGIN back.
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_rp') AS ovb_rp,
       coalesce((SELECT rolcanlogin FROM pg_roles WHERE rolname = 'oracle_view_rp'), false) AS ovb_rp_login,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oracle_view_ro') AS ovb_ro,
       :'ovb_old_db' <> :'ovb_db'
         AND EXISTS (SELECT 1 FROM pg_database WHERE datname = :'ovb_old_db'
                       AND datallowconn AND datconnlimit <> -2) AS ovb_old \gset
\if :ovb_rp
-- The objects of the role go to the owner of the database (REASSIGN_SQL): never to a
-- superuser, which would run them, never to the role itself (DROP OWNED BY would then drop
-- them), and with no database or tablespace of the role. The role can own ovb_db or
-- ovb_old_db when a superuser gives it, or after a REASSIGN OWNED BY of the SQL console
-- from the owner to the role (PostgreSQL 16 checks the inherited rights there, not SET
-- ROLE). Then the run stops here, before any change.
SQL
    printf '%s\n' "$OWNERS_SQL"
    cat <<'SQL'
\if :ovb_owner_super
\echo The owner of :ovb_db is :ovb_owner, a superuser. drop-role gives the objects of oracle_view_rp there to the owner of
\echo the database (REASSIGN OWNED BY), and a superuser would run them (a SECURITY DEFINER function, for example). Give
\echo :ovb_db an owner that is not a superuser (the role of the App) first. Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'the owner of the RP database is a superuser: give it an owner that is not a superuser, then run drop-role again'; END $$;
\elif :ovb_old_owner_super
\echo The owner of :ovb_old_db is :ovb_old_owner, a superuser. drop-role gives the objects of oracle_view_rp there to the
\echo owner of that database (REASSIGN OWNED BY), and a superuser would run them (a SECURITY DEFINER function, for example).
\echo Give :ovb_old_db an owner that is not a superuser (the role of the App), or drop it. Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'the owner of the old database is a superuser: give it an owner that is not a superuser, then run drop-role again'; END $$;
\elif :ovb_owner_self
\echo oracle_view_rp owns :ovb_owned. A different owner is needed first (the role of the App): drop-role gives the
\echo objects of oracle_view_rp to the owner of the database and never drops them. Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'oracle_view_rp owns the RP database or the old database: give it a different owner first, then run drop-role again'; END $$;
\elif :ovb_rp_shared
\echo oracle_view_rp owns :ovb_rp_shared_list. REASSIGN OWNED BY gives each database and tablespace of the
\echo role to the owner of the database, without a word. Give each one a different owner first (ALTER DATABASE or ALTER
\echo TABLESPACE, as postgres). Nothing was changed.
DO $$ BEGIN RAISE EXCEPTION 'oracle_view_rp owns a database or a tablespace: give it a different owner first, then run drop-role again'; END $$;
\endif
ALTER ROLE oracle_view_rp NOLOGIN;
-- Each session ends before the next step (up to 5 s for each): a temporary table of a
-- session that is still ending would be an object of the role for REASSIGN OWNED BY below.
SELECT count(pg_terminate_backend(pid, 5000)) AS ended_sessions FROM pg_stat_activity WHERE usename = 'oracle_view_rp' \gset
\connect :ovb_db
BEGIN;
-- The tables that go: every table of ovb_stage when the role owns the schema, else the
-- tables of the role there (DROP OWNED BY). A schema ovb_stage of another owner stays.
SELECT count(c.oid) AS ovb_stage_tables,
       pg_size_pretty(coalesce(sum(pg_total_relation_size(c.oid)), 0)) AS ovb_stage_size,
       count(c.oid) > 0 AND NOT :'ovb_drop_staging'::boolean AS ovb_stage_kept,
       coalesce(bool_or(n.nspowner = r.oid), false) AS ovb_stage,
       coalesce(bool_or(n.nspowner <> r.oid), false) AS ovb_stage_other
  FROM pg_namespace n JOIN pg_roles r ON r.rolname = 'oracle_view_rp'
  LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'm')
                      AND (n.nspowner = r.oid OR c.relowner = r.oid)
 WHERE n.nspname = 'ovb_stage' \gset
\if :ovb_stage_kept
\connect postgres
\if :ovb_rp_login
ALTER ROLE oracle_view_rp LOGIN;
\endif
\echo ovb_stage holds :ovb_stage_tables staging tables now. Nothing was dropped.
DO $$ BEGIN RAISE EXCEPTION 'ovb_stage holds staging tables: run drop-role again, with --drop-staging to drop them'; END $$;
\endif
\if :ovb_stage
SQL
    # $1: the WITH clause of the objects outside ovb_stage that depend on it (STAGE_OUTSIDE_WITH).
    printf '%s\n' "$1 SELECT count(*) > 0 AS ovb_stage_outside, count(*) AS ovb_stage_outside_count," \
        "       coalesce(string_agg(obj, '; ' ORDER BY obj), '') AS ovb_stage_outside_list FROM outside \\gset"
    cat <<'SQL'
\if :ovb_stage_outside
\connect postgres
\if :ovb_rp_login
ALTER ROLE oracle_view_rp LOGIN;
\endif
\echo :ovb_stage_outside_count objects outside ovb_stage depend on it: :ovb_stage_outside_list
\echo Nothing was dropped.
DO $$ BEGIN RAISE EXCEPTION 'objects outside ovb_stage depend on it: DROP SCHEMA ovb_stage CASCADE would drop them'; END $$;
\endif
DROP SCHEMA ovb_stage CASCADE;
\echo schema ovb_stage dropped, with its :ovb_stage_tables staging tables and their data (:ovb_stage_size)
\elif :ovb_stage_other
-- The staging tables of the role there go, as before the REASSIGN OWNED BY below: the
-- materialized views first (one can read a table), then the tables, each kind in one
-- statement, so that a foreign key between two of them cannot stop the drop. A partition
-- goes with its table: IF EXISTS, and no NOTICE for it. An object of another role that
-- depends on one of them stops the transaction, which then drops nothing.
SET client_min_messages = warning;
SELECT 'DROP MATERIALIZED VIEW IF EXISTS ' || string_agg(format('ovb_stage.%I', c.relname), ', ' ORDER BY c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_roles r ON r.oid = c.relowner
 WHERE n.nspname = 'ovb_stage' AND r.rolname = 'oracle_view_rp' AND c.relkind = 'm' HAVING count(*) > 0 \gexec
SELECT 'DROP TABLE IF EXISTS ' || string_agg(format('ovb_stage.%I', c.relname), ', ' ORDER BY c.relname)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_roles r ON r.oid = c.relowner
 WHERE n.nspname = 'ovb_stage' AND r.rolname = 'oracle_view_rp' AND c.relkind IN ('r', 'p') HAVING count(*) > 0 \gexec
RESET client_min_messages;
\echo schema ovb_stage of another owner stays: :ovb_stage_tables staging tables of oracle_view_rp in it dropped (:ovb_stage_size)
\endif
SQL
    # Every other object of the role, in ovb_db and in ovb_old_db, goes to the owner of that
    # database and stays; DROP OWNED BY then removes the grants and default privileges.
    printf '%s\n' "$REASSIGN_SQL"
    cat <<'SQL'
DROP OWNED BY oracle_view_rp;
COMMIT;
\if :ovb_old
\connect :ovb_old_db
BEGIN;
SQL
    printf '%s\n' "$REASSIGN_SQL"
    cat <<'SQL'
DROP OWNED BY oracle_view_rp;
COMMIT;
\endif
\connect postgres
DROP ROLE oracle_view_rp;
\echo role oracle_view_rp dropped
\else
\echo role oracle_view_rp does not exist: nothing to do
\endif
\if :ovb_ro
ALTER ROLE oracle_view_ro NOLOGIN;
SELECT count(pg_terminate_backend(pid)) AS ended_sessions FROM pg_stat_activity WHERE usename = 'oracle_view_ro' \gset
\connect :ovb_db
DROP OWNED BY oracle_view_ro;
\if :ovb_old
\connect :ovb_old_db
DROP OWNED BY oracle_view_ro;
\endif
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
        login_before
        # The verifier goes to psql on stdin only, never on a command line.
        if ! { printf "\\set ovb_verifier '%s'\n" "$VERIFIER"; role_sql; } | pg_sql; then
            login_back
            die "Creating or updating the role $RP_ROLE failed (above)"
        fi
        VERIFIER=''
        printf 'The role %s has the verifier of the file and its grants\n' "$RP_ROLE"
    fi
    printf '\n== Rights of %s\n' "$RP_ROLE"
    role_checks
    stage_tables
    ((STAGE_TABLES == 0)) || printf 'To free the disk, drop the staging tables that nobody needs (README.md, Oracle Field Browser).\n'
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
    local hba staging=off
    db_lock
    find_psql
    db_ready
    printf 'This ends the sessions of %s, removes its rights in %s and %s and drops it (also oracle_view_ro of an earlier bundle).\n' \
        "$RP_ROLE" "$RP_DATABASE" "$OLD_RP_DATABASE"
    printf 'It also drops the schema %s of %s: the staging tables of Copy to RP and their data are dropped.\n' \
        "$STAGE_SCHEMA" "$RP_DATABASE"
    printf 'The objects that the role made outside %s (a table in public, for example) stay: it gives them to the owner\n' \
        "$STAGE_SCHEMA"
    printf 'of their database and names them. Rows that the SQL console changed stay changed.\n'
    if [[ $(pg_value postgres "SELECT count(*) FROM pg_roles WHERE rolname = '$RP_ROLE'") == 1 ]]; then
        stage_tables
        stage_outside
        if ((STAGE_TABLES > 0 && DROP_STAGING == 0)); then
            printf '\ndrop-role drops these %s tables and their data (%s). To keep a table, copy it first, for example:\n' \
                "$STAGE_TABLES" "$STAGE_SIZE" >&2
            printf "    sudo -u postgres pg_dump -p %s -d %s -n %s -Fc -f /var/tmp/ovb_stage.dump\n" \
                "$PG_PORT" "$RP_DATABASE" "$STAGE_SCHEMA" >&2
            die "The schema $STAGE_SCHEMA holds $STAGE_TABLES staging tables (above). To drop them with the role, run drop-role again with --drop-staging. Nothing was changed."
        fi
    fi
    confirm "$RP_ROLE"
    ((DROP_STAGING == 0)) || staging=on
    login_before
    if ! drop_sql "$STAGE_OUTSIDE_WITH" | pg_sql -v "ovb_drop_staging=$staging"; then
        login_back
        die "Dropping the role $RP_ROLE failed (above)"
    fi
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
        --non-interactive) NON_INTERACTIVE=1; shift ;;   # an option of every command
        --no-pull) NO_PULL=1; GIVEN+=("$1"); shift ;;
        --purge) PURGE=1; GIVEN+=("$1"); shift ;;
        --check) CHECK_ONLY=1; GIVEN+=("$1"); shift ;;
        --drop-staging) DROP_STAGING=1; GIVEN+=("$1"); shift ;;
        *) usage_error "Unknown argument: $1" ;;
    esac
done
# --non-interactive, or KIT_NON_INTERACTIVE=1 in the environment: no question at
# all (README.md, "Run the kit without a person"). A confirmation comes from
# --confirm; without it the command stops before any change.
case ${KIT_NON_INTERACTIVE:-} in
    ''|0) ;;
    1) NON_INTERACTIVE=1 ;;
    *) usage_error 'KIT_NON_INTERACTIVE must be 1 (never ask) or 0 (ask on a terminal).' ;;
esac
((NON_INTERACTIVE == 0)) || export KIT_NON_INTERACTIVE=1
# KIT_NO_PULL=1, the variable of every kit script (README.md, "Install from streamed images"),
# or OVB_NO_PULL=1 below: install --no-pull for a run that cannot add an option.
case ${KIT_NO_PULL:-} in
    ''|0|1) ;;
    *) usage_error 'KIT_NO_PULL must be 1 (never pull: use the image streamed from the Mac) or 0 (pull as before).' ;;
esac
allowed() {   # $@: the options of COMMAND
    local option
    for option in "${GIVEN[@]}"; do
        [[ " $* " == *" $option "* ]] || usage_error "$option does not belong to $COMMAND"
    done
}
case $COMMAND in
    install)
        allowed --config --no-pull --dir
        case ${OVB_NO_PULL:-} in   # --no-pull for a run that cannot add an option
            ''|0) ;;
            1) NO_PULL=1 ;;
            *) usage_error "OVB_NO_PULL must be 1 or 0: $OVB_NO_PULL" ;;
        esac
        [[ -z ${KIT_NO_PULL:-} || -z ${OVB_NO_PULL:-} || $KIT_NO_PULL == "$OVB_NO_PULL" ]] \
            || usage_error "KIT_NO_PULL=$KIT_NO_PULL and OVB_NO_PULL=$OVB_NO_PULL do not agree: give one of them, or the same value to both"
        [[ ${KIT_NO_PULL:-} != 1 ]] || NO_PULL=1 ;;
    status) allowed --dir ;;
    uninstall) allowed --purge --confirm --dir ;;
    role)
        allowed --verifier-file --check --app-ip --pg-port
        [[ -n $VERIFIER_FILE || $CHECK_ONLY == 1 ]] || usage_error 'role needs --verifier-file FILE (or --check)'
        [[ -z $VERIFIER_FILE || $CHECK_ONLY == 0 ]] || usage_error '--verifier-file and --check do not go together' ;;
    drop-role) allowed --confirm --drop-staging --pg-port ;;
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
