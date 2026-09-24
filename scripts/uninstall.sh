#!/usr/bin/env bash
# Undo deploy-db.sh (--role db) or deploy-app.sh (--role app) on this server.
# Nothing is removed before the plan has been shown and confirmed.
set -Eeuo pipefail
umask 077
export LC_ALL=C

usage() {
    cat <<'EOF'
Usage: sudo bash uninstall.sh --role db  [--config FILE | --database NAME] [--confirm NAME] [--purge]
       sudo bash uninstall.sh --role app [--dir PATH] [--confirm PROJECT] [--purge]

--role db removes what deploy-db.sh created for one database: the database and
any staging copy left by a failed run, the login role (unless another database
or deployment still uses it), its access rules, the listen address it added
(unless another deployment uses it), its deployment records, and a deploy.conf
written by the guided setup. The database is taken from deploy.conf next to this
script, --config, --database, or the only deployment recorded on this server.
PostgreSQL stays installed. --purge also uninstalls PostgreSQL 16 and deletes
every database on this server; it needs a terminal.

--role app stops and removes the App containers, their anonymous volumes and
the network, and deletes the deployment directory (default /opt/perodua-app),
including its copy of the database password. The attachments volume and the
images are kept; --purge removes them too.

The plan is shown first. Type the database or project name to confirm, or pass
it with --confirm for unattended use.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROLE='' CONFIG='' DATABASE='' DEPLOY_DIR=/opt/perodua-app CONFIRM='' PURGE=0
while (($#)); do
    case $1 in
        --role|--config|--database|--dir|--confirm)
            (($# >= 2)) || die "Missing value for $1"
            case $1 in
                --role) ROLE=$2 ;;
                --config) CONFIG=$2 ;;
                --database) DATABASE=$2 ;;
                --dir) DEPLOY_DIR=$2 ;;
                --confirm) CONFIRM=$2 ;;
            esac
            shift 2 ;;
        --purge) PURGE=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die "Unknown argument: $1" ;;
    esac
done
[[ $ROLE == db || $ROLE == app ]] || { usage >&2; die 'Choose --role db or --role app.'; }
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'
# shellcheck source=/dev/null
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die 'This script supports Ubuntu 24.04.'

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
confirm_purge() {
    local answer
    has_terminal || die '--purge needs a terminal. Nothing was changed.'
    IFS= read -r -p 'Type PURGE to uninstall PostgreSQL 16 and delete every database on this server: ' answer </dev/tty \
        || die 'Cancelled. Nothing was changed.'
    [[ $answer == PURGE ]] || die 'The confirmation did not match. Nothing was changed.'
}

# ── App server ────────────────────────────────────────────────────────────────
uninstall_app() {
    local project images=() image label listed ids=() mounted anonymous=() anon error volume
    [[ $DEPLOY_DIR == /* && $DEPLOY_DIR != / ]] || die '--dir must be an absolute directory path, not /.'
    [[ -d $DEPLOY_DIR && ! -L $DEPLOY_DIR ]] || die "No App deployment at $DEPLOY_DIR. Nothing was changed."
    [[ -f $DEPLOY_DIR/.deployment-identity ]] \
        || die "$DEPLOY_DIR was not created by deploy-app.sh (no .deployment-identity). Nothing was changed."
    project=$(sed -n 's/^project=//p' "$DEPLOY_DIR/.deployment-identity")
    [[ $project =~ ^[a-z][a-z0-9_-]{0,49}$ ]] || die 'Unreadable project name in .deployment-identity. Nothing was changed.'
    command -v docker >/dev/null || die 'Docker is not installed. Nothing was changed.'
    if [[ -f $DEPLOY_DIR/compose.yml ]]; then
        mapfile -t images < <(sed -n 's/^    image: //p' "$DEPLOY_DIR/compose.yml" | awk '!seen[$0]++')
    fi
    label="label=com.docker.compose.project=$project"
    listed=$(docker ps -aq --filter "$label")
    [[ -z $listed ]] || mapfile -t ids <<< "$listed"
    # Docker creates an anonymous volume (label com.docker.volume.anonymous) for
    # each folder that the image declares as a VOLUME and compose.yml does not
    # name, such as /mnt/extra-addons. It keeps them when it removes the
    # containers, and after a redeploy even with "down --volumes", because Compose
    # hands them to the new container by name. So the ones these containers mount
    # are listed now and deleted by name afterwards. Named volumes never match.
    if ((${#ids[@]})); then
        mounted=$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' "${ids[@]}") \
            || die 'Could not read the volumes of the App containers. Nothing was changed.'
        listed=$(docker volume ls -q --filter label=com.docker.volume.anonymous) \
            || die 'Could not list the anonymous volumes. Nothing was changed.'
        mapfile -t anonymous < <(sed '/^$/d' <<< "$mounted" | grep -Fx -f <(printf '%s\n' "$listed") | awk '!seen[$0]++')
    fi
    volume=${project}_filestore

    printf 'Uninstall the App deployment "%s" from %s:\n' "$project" "$DEPLOY_DIR"
    printf '  - stop and remove its %s container(s) and its network\n' "${#ids[@]}"
    ((${#anonymous[@]} == 0)) \
        || printf '  - delete the %s anonymous volume(s) Docker created for them (such as /mnt/extra-addons)\n' "${#anonymous[@]}"
    printf '  - delete %s (configuration, the copy of the database password, logs)\n' "$DEPLOY_DIR"
    if ((PURGE)); then
        printf '  - delete the attachments volume %s\n' "$volume"
        for image in "${images[@]}"; do printf '  - delete the image %s\n' "${image%@*}"; done
        printf 'The attachments are deleted and cannot be recovered.\n'
    else
        printf 'Kept: the attachments volume %s and the downloaded images (--purge removes them).\n' "$volume"
    fi
    printf 'The database on the DB server is not touched.\n'
    confirm "$project"

    if [[ -f $DEPLOY_DIR/compose.yml ]]; then
        local down=(down --remove-orphans)
        ((PURGE == 0)) || down+=(--volumes)
        docker compose --project-name "$project" --file "$DEPLOY_DIR/compose.yml" "${down[@]}"
    else
        # No compose file left: remove what carries the project's label.
        mapfile -t ids < <(docker ps -aq --filter "$label")
        ((${#ids[@]} == 0)) || docker rm -f "${ids[@]}" >/dev/null
        mapfile -t ids < <(docker network ls -q --filter "$label")
        ((${#ids[@]} == 0)) || docker network rm "${ids[@]}" >/dev/null
        if ((PURGE)); then
            mapfile -t ids < <(docker volume ls -q --filter "$label")
            ((${#ids[@]} == 0)) || docker volume rm "${ids[@]}" >/dev/null
        fi
    fi
    for anon in "${anonymous[@]}"; do
        if error=$(docker volume rm "$anon" 2>&1 >/dev/null); then
            printf 'Deleted anonymous volume %s\n' "$anon"
        elif [[ $error != *'no such volume'* ]]; then  # else "down --volumes" deleted it
            printf 'Kept anonymous volume %s: %s\n' "$anon" "$error"
        fi
    done
    rm -rf -- "$DEPLOY_DIR"
    if ((PURGE)); then
        for image in "${images[@]}"; do
            if docker image rm "$image" >/dev/null 2>&1; then
                printf 'Deleted image %s\n' "${image%@*}"
            else
                printf 'Kept image %s: another container still uses it.\n' "${image%@*}"
            fi
        done
    fi
    printf 'SUCCESS: the App deployment "%s" was removed.\n' "$project"
}

# ── DB server ─────────────────────────────────────────────────────────────────
PG_BIN=/usr/lib/postgresql/16/bin
STATE_BASE=/var/lib/perodua-db-deploy
PG_PACKAGES=(postgresql-16 postgresql-client-16 postgresql-contrib postgresql-common postgresql-client-common)
PG_CLUSTER=main PG_PORT=5432 PURGE_PACKAGES=''
admin() { runuser -u postgres -- "$PG_BIN/psql" -X -w -h /var/run/postgresql -p "$PG_PORT" -d postgres -v ON_ERROR_STOP=1 -At "$@"; }
has_systemd() { [[ $(cat /proc/1/comm) == systemd ]]; }
start_cluster() {
    local attempt=0
    if ! pg_ctlcluster 16 "$PG_CLUSTER" status >/dev/null 2>&1; then
        if has_systemd; then systemctl start "postgresql@16-$PG_CLUSTER.service"; else pg_ctlcluster 16 "$PG_CLUSTER" start; fi
    fi
    until admin -c 'SELECT 1' >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        ((attempt < 30)) || die 'PostgreSQL did not become ready. Nothing was changed.'
        sleep 1
    done
}
restart_cluster() {
    if has_systemd; then systemctl restart "postgresql@16-$PG_CLUSTER.service"; else pg_ctlcluster 16 "$PG_CLUSTER" restart; fi
    start_cluster
}
conf_value() {  # KEY FILE: read a literal KEY=value; configuration is never sourced
    local line
    line=$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$2" | tail -n 1) || return 0
    line=${line#*=}
    line=${line#"${line%%[![:space:]]*}"}
    line=${line%"${line##*[![:space:]]}"}
    if [[ $line == \"*\" || $line == \'*\' ]]; then line=${line:1:${#line}-2}; fi
    printf '%s' "$line"
}
sql_list() { local item out=''; for item in "$@" ''; do out+="'$item',"; done; printf '%s' "${out%,}"; }

purge_plan() {  # $@: databases already being dropped
    local package others=''
    PURGE_PACKAGES=$(apt-get -s purge "${PG_PACKAGES[@]}" 2>/dev/null | awk '/^Purg /{print $2}' | sort | tr '\n' ' ')
    for package in $PURGE_PACKAGES; do
        [[ " ${PG_PACKAGES[*]} " == *" $package "* ]] \
            || die "Purging would also remove $package (another PostgreSQL version or a package that needs it). Nothing was changed."
    done
    if [[ -z $PURGE_PACKAGES ]]; then
        printf 'PostgreSQL 16 is not installed; there is nothing to purge.\n'
        return
    fi
    if others=$(admin -c "SELECT coalesce(string_agg(datname, ', ' ORDER BY datname), 'none') FROM pg_database WHERE datname NOT IN ('postgres', 'template0', 'template1', $(sql_list "$@"))" 2>/dev/null); then
        :
    else
        others='could not be listed (PostgreSQL is not running); every database is deleted'
    fi
    printf 'Then uninstall PostgreSQL 16 (%s) and delete its data, configuration and logs.\n' "${PURGE_PACKAGES% }"
    printf 'Other databases deleted with it: %s\n' "$others"
}
purge_execute() {
    [[ -n $PURGE_PACKAGES ]] || return 0
    if has_systemd; then systemctl stop postgresql 2>/dev/null || true; else pg_ctlcluster 16 "$PG_CLUSTER" stop 2>/dev/null || true; fi
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -q "${PG_PACKAGES[@]}" > /var/tmp/uninstall-postgresql.log 2>&1 \
        || die 'apt-get purge failed; see /var/tmp/uninstall-postgresql.log.'
    rm -rf /var/lib/postgresql /etc/postgresql /etc/postgresql-common /var/log/postgresql /run/postgresql "$STATE_BASE"
    rm -f /var/tmp/uninstall-postgresql.log
    printf 'PostgreSQL 16 was uninstalled. Libraries it pulled in stay; "sudo apt autoremove" lists them.\n'
}

uninstall_db() {
    local conf='' states=() state marker='' marker_oid='' oid='' owner staging=() targets=() connections
    local db_name db_user='' listen_ip='' role_action='' other listen_action='' current_listen new_listen='' guided conf_action=''
    if [[ -n $CONFIG ]]; then
        [[ -f $CONFIG ]] || die "Configuration not found: $CONFIG"
        conf=$CONFIG
    elif [[ -z $DATABASE && -f $SCRIPT_DIR/deploy.conf ]]; then
        conf=$SCRIPT_DIR/deploy.conf
    fi
    if [[ -n $conf ]]; then
        db_name=$(conf_value DB_NAME "$conf")
        PG_CLUSTER=$(conf_value PG_CLUSTER "$conf")
        listen_ip=$(conf_value DB_LISTEN_IP "$conf")
    elif [[ -n $DATABASE ]]; then
        db_name=$DATABASE
    else
        mapfile -t states < <(find "$STATE_BASE" -mindepth 3 -maxdepth 3 \( -name success -o -name pending \) -printf '%h\n' 2>/dev/null | sort -u)
        if ((${#states[@]} == 1)); then
            db_name=${states[0]##*/}
            PG_CLUSTER=${states[0]%/*}
            PG_CLUSTER=${PG_CLUSTER##*/16-}
        elif ((${#states[@]} > 1)); then
            die "Several deployments are recorded; choose one with --database: $(printf '%s ' "${states[@]##*/}")"
        elif ((PURGE)); then
            printf 'No database deployment is recorded on this server.\n'
            purge_plan
            [[ -z $PURGE_PACKAGES ]] || { confirm_purge; purge_execute; }
            exit 0
        else
            die 'No database deployment is recorded on this server. Nothing was changed.'
        fi
    fi
    db_name=${db_name:-perodua} PG_CLUSTER=${PG_CLUSTER:-main}
    [[ $db_name =~ ^[a-z][a-z0-9_]{0,39}$ && $db_name != postgres && $db_name != template[01] ]] || die 'Invalid database name.'
    [[ $PG_CLUSTER =~ ^[a-z][a-z0-9_]{0,29}$ ]] || die 'Invalid PG_CLUSTER.'
    [[ -x $PG_BIN/psql ]] || die 'PostgreSQL 16 is not installed, so there is no database to remove.'
    PG_PORT=$(pg_conftool 16 "$PG_CLUSTER" show port 2>/dev/null | awk '{print $NF}') || true
    [[ $PG_PORT =~ ^[1-9][0-9]{0,4}$ ]] || die "PostgreSQL 16 cluster $PG_CLUSTER does not exist."
    start_cluster

    state=$STATE_BASE/16-$PG_CLUSTER/$db_name
    for marker in "$state/success" "$state/pending" ''; do [[ -z $marker || ! -f $marker ]] || break; done
    if [[ -n $marker ]]; then
        marker=$(<"$marker")
        marker_oid=$(cut -d '|' -f 2 <<< "$marker")
        db_user=$(cut -d '|' -f 3 <<< "$marker")
        [[ $db_user =~ ^[a-z][a-z0-9_]{0,39}$ && $marker_oid =~ ^[0-9]+$ ]] || die "Unreadable deployment record in $state. Nothing was changed."
        [[ ! -f $state/connection.txt ]] || listen_ip=$(conf_value DB_HOST "$state/connection.txt")
    fi
    oid=$(admin -c "SELECT oid FROM pg_database WHERE datname='$db_name'")
    if [[ -n $oid ]]; then
        owner=$(admin -c "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE oid=$oid")
        [[ -n $marker && $oid == "$marker_oid" && $owner == "$db_user" ]] \
            || die "Database $db_name was not created by deploy-db.sh on this server, or was replaced since. Nothing was changed."
        targets+=("$db_name")
    elif [[ -z $marker ]]; then
        die "No deployment of $db_name is recorded on this server. Nothing was changed."
    fi
    mapfile -t staging < <(admin -c "SELECT datname FROM pg_database WHERE (datname LIKE '${db_name}\_\_restore\_%' OR datname LIKE '${db_name}\_\_empty\_%') AND pg_get_userbyid(datdba) = '$db_user' ORDER BY 1")
    targets+=("${staging[@]}")
    if ((${#targets[@]})); then
        connections=$(admin -c "SELECT count(*) FROM pg_stat_activity WHERE datname IN ($(sql_list "${targets[@]}"))")
        ((connections == 0)) || die "$connections connection(s) are open to $db_name. Stop the App first (on the App server: sudo bash uninstall.sh --role app), then run this again. Nothing was changed."
    fi

    # The role goes only if no other database or recorded deployment uses it.
    if [[ -n $(admin -c "SELECT 1 FROM pg_roles WHERE rolname='$db_user'") ]]; then
        other=$(admin -c "SELECT string_agg(datname, ', ') FROM pg_database WHERE pg_get_userbyid(datdba) = '$db_user' AND datname NOT IN ($(sql_list "${targets[@]}"))")
        if [[ -z $other ]]; then
            other=$(grep -lx "[^|]*|[0-9]*|$db_user" "$STATE_BASE"/16-*/*/success "$STATE_BASE"/16-*/*/pending 2>/dev/null | grep -v "^$state/" | head -n 1 || true)
            [[ -z $other ]] || { other=${other%/*}; other="the deployment of ${other##*/}"; }
        fi
        role_action=${other:+keep}
        role_action=${role_action:-drop}
    fi
    # The listen address goes unless another deployment uses it. 127.0.0.1 stays:
    # deploy-db.sh adds it for every deployment's local checks.
    current_listen=$(admin -c 'SHOW listen_addresses')
    current_listen=${current_listen// /}
    if [[ -n $listen_ip && $listen_ip != 127.0.0.1 && ",$current_listen," == *",$listen_ip,"* ]]; then
        local summaries=()
        mapfile -t summaries < <(find "$STATE_BASE" -mindepth 3 -maxdepth 3 -name connection.txt ! -path "$state/*" 2>/dev/null)
        if ((${#summaries[@]})) && grep -qxF "DB_HOST=$listen_ip" "${summaries[@]}"; then
            listen_action=keep
        else
            listen_action=remove
            new_listen=$(tr ',' '\n' <<< "$current_listen" | { grep -vxF "$listen_ip" || true; } | paste -sd ',' -)
        fi
    fi
    guided=${CONFIG:-$SCRIPT_DIR/deploy.conf}
    if [[ -f $guided && $(conf_value DB_NAME "$guided") == "$db_name" ]]; then
        if head -n 1 "$guided" | grep -q '^# Created by deploy-db.sh from your answers'; then conf_action=delete; else conf_action=keep; fi
    fi

    printf 'Uninstall the database deployment "%s" (PostgreSQL 16, cluster %s):\n' "$db_name" "$PG_CLUSTER"
    ((${#targets[@]} == 0)) || printf '  - drop database %s\n' "${targets[@]}"
    case $role_action in
        drop) printf '  - drop login role %s, and with it its password\n' "$db_user" ;;
        keep) printf '  - keep login role %s: %s still uses it\n' "$db_user" "$other" ;;
    esac
    printf '  - remove its access rules from pg_hba.conf\n'
    case $listen_action in
        remove) printf '  - stop listening on %s (PostgreSQL restarts briefly)\n' "$listen_ip" ;;
        keep) printf '  - keep listening on %s: another deployment uses it\n' "$listen_ip" ;;
    esac
    printf '  - delete the deployment records in %s\n' "$state"
    case $conf_action in
        delete) printf '  - delete %s (written by the guided setup)\n' "$guided" ;;
        keep) printf '  - keep %s (written by hand)\n' "$guided" ;;
    esac
    ((${#targets[@]} == 0)) || printf 'All data in these databases is deleted and cannot be recovered.\n'
    if ((PURGE)); then purge_plan "${targets[@]}"; else printf 'PostgreSQL 16 stays installed.\n'; fi
    confirm "$db_name"
    ((PURGE == 0)) || [[ -z $PURGE_PACKAGES ]] || confirm_purge

    local target hba backup
    for target in "${targets[@]}"; do
        admin -c "DROP DATABASE \"$target\"" >/dev/null
        printf 'Dropped database %s\n' "$target"
    done
    if [[ $role_action == drop ]]; then
        if admin -c "DROP ROLE \"$db_user\"" >/dev/null 2>&1; then
            printf 'Dropped login role %s\n' "$db_user"
        else
            printf 'Kept login role %s: it still has objects or privileges elsewhere.\n' "$db_user"
        fi
    fi
    hba=$(admin -c 'SHOW hba_file')
    backup=$(mktemp /var/tmp/pg_hba.conf.uninstall.XXXXXX)
    cp -p -- "$hba" "$backup"
    python3 - "$hba" "$db_name" <<'PY'
import os, stat, sys, tempfile
path, db = sys.argv[1:]
begin, end = f'# BEGIN perodua-db-deploy {db}', f'# END perodua-db-deploy {db}'
lines = open(path).read().splitlines()
if lines.count(begin) != lines.count(end) or lines.count(begin) > 1:
    sys.exit('ERROR: malformed managed access-rule block; inspect pg_hba.conf manually')
kept, inside = [], False
for line in lines:
    if line == begin:
        inside = True
    elif line == end:
        inside = False
    elif not inside:
        kept.append(line)
st = os.stat(path)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
try:
    os.fchmod(fd, stat.S_IMODE(st.st_mode))
    os.fchown(fd, st.st_uid, st.st_gid)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(kept) + '\n')
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
    if [[ $(admin -c 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL') != 0 ]]; then
        cp -p -- "$backup" "$hba"
        die "The access rules would be invalid; the previous pg_hba.conf was restored (copy: $backup)."
    fi
    admin -c 'SELECT pg_reload_conf()' >/dev/null
    rm -f -- "$backup"
    printf 'Removed the access rules for %s\n' "$db_name"
    if [[ $listen_action == remove ]]; then
        pg_conftool 16 "$PG_CLUSTER" set listen_addresses "${new_listen:-localhost}"
        restart_cluster
        printf 'PostgreSQL now listens on %s\n' "$(admin -c 'SHOW listen_addresses')"
    fi
    rm -rf -- "$state"
    [[ $conf_action != delete ]] || rm -f -- "$guided"
    printf 'SUCCESS: the database deployment "%s" was removed.\n' "$db_name"
    ((PURGE == 0)) || purge_execute
}

if [[ $ROLE == app ]]; then uninstall_app; else uninstall_db; fi
