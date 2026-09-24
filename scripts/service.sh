#!/usr/bin/env bash
# Stop, start or restart the App or the database server, or reset the App to a
# fresh UAT system.
set -Eeuo pipefail
umask 077
export LC_ALL=C

usage() {
    cat <<'EOF'
Usage: sudo bash service.sh --role app [--dir PATH] stop|start|restart|status
       sudo bash service.sh --role app [--dir PATH] reset [--confirm DATABASE]
       sudo bash service.sh --role db  [--cluster NAME] stop|start|restart|status

stop, start, restart and status keep all data and settings. Stop the App before
the database, and start the database before the App.

--role app  the containers deploy-app.sh created (--dir, default
            /opt/perodua-app). start and restart go ahead only if
            deploy-app.sh's database check reports READY. A stopped App stays
            stopped after a reboot until it is started again.
--role db   PostgreSQL 16 (--cluster, default main). It starts again with the
            server.

reset, on the App server, deletes all data in the App's database and all
attachments, then runs deploy-app.sh --init-db for a fresh UAT system of the
release next to this script. The database, its login and password, the web
port and the other settings stay; the UAT administrators whadmin, admin1 and
admin2 get the password "perodua" again. The plan is shown first: type the
database name to confirm, or pass it with --confirm for unattended use.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROLE='' ACTION='' DEPLOY_DIR=/opt/perodua-app CLUSTER='' CONFIRM=''
while (($#)); do
    case $1 in
        --role|--dir|--cluster|--confirm)
            (($# >= 2)) || die "Missing value for $1"
            case $1 in
                --role) ROLE=$2 ;;
                --dir) DEPLOY_DIR=$2 ;;
                --cluster) CLUSTER=$2 ;;
                --confirm) CONFIRM=$2 ;;
            esac
            shift 2 ;;
        stop|start|restart|status|reset)
            [[ -z $ACTION ]] || die 'Give one action.'
            ACTION=$1
            shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die "Unknown argument: $1" ;;
    esac
done
[[ $ROLE == app || $ROLE == db ]] || { usage >&2; die 'Choose --role app or --role db.'; }
[[ -n $ACTION ]] || { usage >&2; die 'Choose stop, start, restart, status or reset.'; }
[[ $ACTION != reset || $ROLE == app ]] || die 'reset runs on the App server (--role app). The database server needs no step.'
[[ -z $CONFIRM || $ACTION == reset ]] || die '--confirm only applies to reset.'
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'

has_terminal() { { : </dev/tty; } 2>/dev/null; }
confirm() {
    local expected=$1 answer
    if [[ -n $CONFIRM ]]; then
        [[ $CONFIRM == "$expected" ]] || die "--confirm must be exactly $expected. Nothing was changed."
        return
    fi
    has_terminal || die "No terminal to confirm on: pass --confirm $expected. Nothing was changed."
    IFS= read -r -p "Type $expected to confirm: " answer </dev/tty || die 'Cancelled. Nothing was changed.'
    [[ $answer == "$expected" ]] || die 'The confirmation did not match. Nothing was changed.'
}

# ── App server ────────────────────────────────────────────────────────────────
compose() { docker compose --project-name "$PROJECT" --file "$DEPLOY_DIR/compose.yml" "$@"; }

lock_deployment() {
    # The lock deploy-app.sh and uninstall.sh hold while they run.
    exec 9>>"$DEPLOY_DIR/.deploy.lock"
    flock -n 9 || die 'A deployment or uninstall is running in this directory. Nothing was changed.'
    [[ /proc/self/fd/9 -ef $DEPLOY_DIR/.deploy.lock ]] \
        || die "$DEPLOY_DIR was removed or replaced in the meantime. Nothing was changed."
}

app_stop() {
    compose stop
    printf 'App stopped. Start it again with: sudo bash service.sh --role app start\n'
}

app_check() {
    # deploy-app.sh's own database check. On an empty or unfinished database,
    # Odoo would set itself up at start, without the UAT setup.
    local state
    state=$(compose run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py check) \
        || die 'The database check failed (see above); the App was not started. Is the database server running?'
    [[ $state == READY\ * ]] \
        || die "The database is not set up ($state); the App was not started. Run the reset again, or deploy-app.sh --init-db."
}

app_start() {
    compose up --detach --no-recreate --wait --wait-timeout 600 \
        || die "The App did not become healthy. Logs: sudo docker compose --project-name $PROJECT --file $DEPLOY_DIR/compose.yml logs --tail 100"
    printf 'App started.\n'
}

app_reset() {
    local helper release revision current status=0 listed ids=() mounted anonymous=() anon error
    for helper in deploy-app.sh reset_database.py; do
        [[ -f $SCRIPT_DIR/$helper ]] || die "$helper is missing next to service.sh; run it from the complete release directory."
    done
    [[ -f $DEPLOY_DIR/app.env ]] || die "$DEPLOY_DIR/app.env is missing, so deploy-app.sh would ask for the settings again. Nothing was changed."
    release=$(sed -n 's/^RELEASE=//p' "$SCRIPT_DIR/deploy-app.sh")
    revision=$(sed -n 's/^REVISION=//p' "$SCRIPT_DIR/deploy-app.sh")
    [[ $release =~ ^[a-z0-9.-]+$ && $revision =~ ^[0-9a-f]{40}$ ]] || die 'Cannot read the release of deploy-app.sh.'
    current=$(sed -n 's/^release=//p' "$DEPLOY_DIR/.deployment-identity")

    printf 'Reset the App deployment "%s" in %s:\n' "$PROJECT" "$DEPLOY_DIR"
    printf '  - stop the App and delete all data in database %s on %s (the database, its login and password stay)\n' "$DB_NAME" "$DB_HOST"
    printf '  - remove the App containers and delete the attachments volume %s_filestore\n' "$PROJECT"
    printf '  - set up a fresh UAT system with deploy-app.sh --init-db: %s' "$release"
    if [[ $current == "$release" ]]; then printf '\n'; else printf ' (now %s)\n' "$current"; fi
    printf 'The data and attachments cannot be recovered. whadmin, admin1 and admin2 get the password "perodua" again.\n'
    confirm "$DB_NAME"

    # As uninstall.sh does: Docker keeps the containers' anonymous volumes (such
    # as /mnt/extra-addons), after a redeploy even with "down --volumes", so the
    # ones they mount are listed first and deleted by name afterwards.
    listed=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
    [[ -z $listed ]] || mapfile -t ids <<< "$listed"
    if ((${#ids[@]})); then
        mounted=$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' "${ids[@]}") \
            || die 'Could not read the volumes of the App containers. Nothing was changed.'
        listed=$(docker volume ls -q --filter label=com.docker.volume.anonymous) \
            || die 'Could not list the anonymous volumes. Nothing was changed.'
        mapfile -t anonymous < <(sed '/^$/d' <<< "$mounted" | grep -Fx -f <(printf '%s\n' "$listed") | awk '!seen[$0]++')
    fi

    compose stop
    compose run --rm --no-deps -T odoo python3 - < "$SCRIPT_DIR/reset_database.py" || status=$?
    ((status != 3)) || die 'Nothing was deleted. The App is stopped; start it again with: sudo bash service.sh --role app start'
    ((status == 0)) || die 'The database is only partly emptied. Solve the problem shown above, then run the reset again.'
    printf 'Database %s is empty.\n' "$DB_NAME"
    compose down --volumes --remove-orphans
    for anon in "${anonymous[@]}"; do
        if ! error=$(docker volume rm "$anon" 2>&1 >/dev/null) && [[ $error != *'no such volume'* ]]; then
            printf 'Kept anonymous volume %s: %s\n' "$anon" "$error"
        fi
    done

    # The directory is bound to the release it runs; the empty database may take another.
    sed -i -e "s/^release=.*/release=$release/" -e "s/^revision=.*/revision=$revision/" "$DEPLOY_DIR/.deployment-identity"
    exec 9>&-  # deploy-app.sh takes the lock itself
    bash "$SCRIPT_DIR/deploy-app.sh" --dir "$DEPLOY_DIR" --init-db \
        || die 'deploy-app.sh did not finish. Solve the problem shown above, then run the reset again.'
}

run_app() {
    [[ $DEPLOY_DIR == /* && $DEPLOY_DIR != / ]] || die '--dir must be an absolute directory path, not /.'
    [[ -f $DEPLOY_DIR/.deployment-identity && -f $DEPLOY_DIR/compose.yml ]] \
        || die "No App deployment at $DEPLOY_DIR (deploy it with deploy-app.sh, or pass --dir)."
    PROJECT=$(sed -n 's/^project=//p' "$DEPLOY_DIR/.deployment-identity")
    DB_NAME=$(sed -n 's/^database=//p' "$DEPLOY_DIR/.deployment-identity")
    DB_HOST=$(sed -n 's/^host=//p' "$DEPLOY_DIR/.deployment-identity")
    [[ $PROJECT =~ ^[a-z][a-z0-9_-]{0,49}$ && -n $DB_NAME ]] || die "Unreadable $DEPLOY_DIR/.deployment-identity."
    command -v docker >/dev/null || die 'Docker is not installed.'
    [[ $ACTION == status ]] || lock_deployment
    case $ACTION in
        stop) app_stop ;;
        start) app_check; app_start ;;
        restart) app_check; app_stop; app_start ;;
        status) compose ps --all --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}' ;;
        reset) app_reset ;;
    esac
}

# ── DB server ─────────────────────────────────────────────────────────────────
PG_BIN=/usr/lib/postgresql/16/bin
conf_value() {  # KEY FILE, as uninstall.sh reads deploy.conf: never sourced
    local line
    line=$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$2" | tail -n 1) || return 0
    line=${line#*=}
    line=${line#"${line%%[![:space:]]*}"}
    line=${line%"${line##*[![:space:]]}"}
    if [[ $line == \"*\" || $line == \'*\' ]]; then line=${line:1:${#line}-2}; fi
    printf '%s' "$line"
}
online() { pg_ctlcluster 16 "$CLUSTER" status >/dev/null 2>&1; }
connections() {
    runuser -u postgres -- "$PG_BIN/psql" -X -w -At -h /var/run/postgresql -p "$PORT" -d postgres \
        -c "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()"
}

db_stop() {
    local open
    if ! online; then
        printf 'PostgreSQL 16 (%s) is already stopped.\n' "$CLUSTER"
        return
    fi
    open=$(connections) || open=0
    ((open == 0)) || printf 'Closing %s open connection(s). An App that is still running shows errors until the database is back.\n' "$open"
    pg_ctlcluster 16 "$CLUSTER" stop
    printf 'PostgreSQL 16 (%s) stopped. Start it again with: sudo bash service.sh --role db start\n' "$CLUSTER"
}

db_start() {
    local attempt=0
    online || pg_ctlcluster 16 "$CLUSTER" start
    until "$PG_BIN/pg_isready" -q -h /var/run/postgresql -p "$PORT"; do
        ((++attempt < 30)) || die "PostgreSQL 16 ($CLUSTER) did not become ready; see /var/log/postgresql/postgresql-16-$CLUSTER.log"
        sleep 1
    done
    printf 'PostgreSQL 16 (%s) is running.\n' "$CLUSTER"
}

run_db() {
    # The cluster deploy.conf next to this script names, as for uninstall.sh.
    if [[ -z $CLUSTER && -f $SCRIPT_DIR/deploy.conf ]]; then
        CLUSTER=$(conf_value PG_CLUSTER "$SCRIPT_DIR/deploy.conf")
    fi
    CLUSTER=${CLUSTER:-main}
    [[ $CLUSTER =~ ^[a-z][a-z0-9_]{0,29}$ ]] || die 'Invalid cluster name.'
    [[ -x $PG_BIN/postgres ]] || die 'PostgreSQL 16 is not installed on this server.'
    PORT=$(pg_conftool 16 "$CLUSTER" show port 2>/dev/null | awk '{print $NF}') || true
    [[ $PORT =~ ^[1-9][0-9]{0,4}$ ]] || die "PostgreSQL 16 cluster $CLUSTER does not exist."
    case $ACTION in
        stop) db_stop ;;
        start) db_start ;;
        restart) db_stop; db_start ;;
        status)
            pg_lsclusters 16 "$CLUSTER"
            if online; then printf 'Open connections: %s\n' "$(connections)"; fi ;;
    esac
}

if [[ $ROLE == app ]]; then run_app; else run_db; fi
