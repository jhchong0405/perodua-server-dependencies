#!/usr/bin/env bash
# App-only Client Stable UIUX deployment. Database service stays on its own host.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077

RELEASE=client-stable-uiux-v1.2.0
REVISION=09d8c712f27c29facaad53c0a34de7df78e0b3f2
# Built by CI from REVISION and published to GHCR, then copied to the private
# registry, registry to registry, with no new build: the digests are the ones
# CI built. The Odoo image loads no sample data; both images name REVISION.
ODOO_IMAGE=perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-v1.2.0@sha256:e1d08ad8d8f5bf04131d361965380eadd3242fc1c7c08df49703bba3783a2e23
WEB_IMAGE=perodua-deploy.novutal.com/perodua-odoo:client-stable-uiux-web-v1.2.0@sha256:89c1a0f941c148b5f7cf50b871dcaed51a4e92fb6761a4d81b8b0e97a17f1518
REGISTRY=${ODOO_IMAGE%%/*}
# Whether the pinned web image serves the page under PUBLIC_ROOT and shows
# ENVIRONMENT_LABEL: yes from v1.0.4. The scripts folder of v1.0.3 has 0 here.
PUBLIC_ROOT_SUPPORTED=1
# Whether the pinned release signs the module host names (stgissrp, stgisssp,
# stgisscp) in through the sign-in host with a one-time ticket: yes from
# v1.0.5. Odoo switches it on only when PUBLIC_BASE_URL is https:// on the
# sign-in host, so without that a run warns. The scripts folder of v1.0.4 has 0
# here.
HANDOVER_SUPPORTED=1
HANDOVER_HOST=stgiss.perodua.com.my
# --init-db: a fresh UAT database, the release graph without the client
# demonstration dataset. Before anything is written, uat_guard.py checks the
# pinned image: nothing Odoo could install may depend on EXCLUDED_MODULE or
# refer to it; after installation the database itself is checked as well.
INIT_MODULES=perodua_client_stable,perodua_gateway,perodua_forecast_workbook,perodua_supplier_execution,perodua_uiux_api
EXCLUDED_MODULE=perodua_demo_client
# A restored release database (or one seeded by earlier versions of this
# script) is still held to exactly the module set it was always checked for.
RESTORED_MODULES=perodua_client_stable,perodua_demo_client,perodua_gateway,perodua_forecast_workbook,perodua_supplier_execution,perodua_uiux_api
# --upgrade to a release whose Odoo modules differ from the database's runs a
# module upgrade (-u) only on a database stamped with this module fingerprint
# (v1.0.3 to v1.0.8 have the same modules), and only in a deployment directory
# of one of these releases. Any other fingerprint is refused before anything
# changes. While the pinned image has this fingerprint itself, every --upgrade
# keeps the modules and runs no -u.
MODULE_UPGRADE_FROM=3b62a97697d2974ef37328de7f3d034d
OLD_RELEASES_ACCEPTED=client-stable-uiux-v1.0.5,client-stable-uiux-v1.0.6,client-stable-uiux-v1.0.7,client-stable-uiux-v1.0.8
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEPLOY_DIR=/opt/perodua-app
CONFIG='' NON_INTERACTIVE=0 INIT_DB=0 CHECK_ONLY=0 TEMP_DIR='' AUTH_DIR='' STARTED=0
# --upgrade: the release the directory runs before, the backup of its data,
# whether its App was running, and how far the upgrade got (see cleanup).
UPGRADE=0 BACKUP_BASE='' BACKUP_DIR='' OLD_RELEASE='' OLD_REVISION='' WAS_RUNNING=0 UPGRADE_STATE=''
# A module upgrade: the modules it upgrades, Odoo's demo-data option, the
# retired rows it deletes (and their lines in the report), the one-off
# container and its process, whether a database upgraded earlier only waits
# for its deployment, the held mail (the mail the upgrade queued, and the mail
# that waited in the queue before it), and whether the containers of the old
# release are removed.
MODULE_UPGRADE=0 UPGRADE_MODULES='' DEMO_FLAG='' RETIRED_TOTAL=0 RETIRED_LINES=() DROP_RETIRED=0 CONFIRM=''
UPGRADE_CONTAINER='' UPGRADE_PID='' PENDING_ONLY=0 PENDING_STAMP=0 HELD_MAIL='' HELD_MAIL_BEFORE='' RELEASE_MAIL=0 DB_MARKED=0 OLD_REMOVED=0
DB_HOST='' DB_PORT=5432 DB_NAME=perodua DB_USER=odoo DB_PASSWORD_FILE=''
PROJECT_NAME=perodua-client-uiux HTTP_PORT=8110 BIND_IP=0.0.0.0
STARTUP_TIMEOUT=600 INIT_TIMEOUT=3600
PUBLIC_ROOT='' ENVIRONMENT_LABEL='' PUBLIC_BASE_URL=''
# The settings the configuration file gives a value, and whether that file is an
# app.env written by hand in the deployment directory before its first run.
declare -A SET=()
PREPARED=0

usage() {
    cat <<'HELP'
Usage: bash deploy-app.sh [--config PATH] [--dir PATH] [--non-interactive] [--init-db]
       bash deploy-app.sh [--config PATH] [--dir PATH] --check-config
       bash deploy-app.sh --upgrade [--dir PATH] [--backup-dir PATH] [--non-interactive]
                          [--confirm DATABASE] [--drop-retired-data]
       bash deploy-app.sh --release-queued-mail [--dir PATH]
Deploy the pinned Client Stable UIUX v1.2.0 Odoo + Web images using Docker Compose.
Requires a reachable external PostgreSQL 16 server; does not install or configure it.
Default: use an already initialized, matching Client Stable UIUX database.
--init-db initializes a NEW or EMPTY database as a fresh UAT system without
business data: the release modules (the image loads no sample data), without the
client demonstration dataset (perodua_demo_client), without Odoo demo data, and
with UAT-only administrators whadmin, admin1 and admin2, all with the fixed
password "perodua". Prepare the empty database on the DB server
with deploy-db.sh DB_MODE=empty. It never reinitializes or upgrades an
initialized database.
--dir defaults to /opt/perodua-app; existing configuration is reused there.
Before the first deployment it may hold an app.env written by hand, such as
only PUBLIC_ROOT and the other settings of the page: on a terminal, the run
asks for the database settings that a configuration without DB_HOST leaves out.
Until a first deployment has used its database, a rerun may change the
database settings and asks for the password again.
--config accepts literal KEY=VALUE lines (see app.env.example), never shell code.
--check-config reads --config, or app.env in --dir, checks every setting the
way a deployment does and stops before Docker is used; nothing is changed.
--upgrade moves a deployment of another release to this one and keeps its data.
The directory must run the same project and database (app.env). It stops the
App, saves the database (pg_dump -Fc) and the attachments in --dir/backups/ (or
--backup-dir), then deploys this release. If anything fails before that, the
directory and its App stay as they were. With the same Odoo modules no module
upgrade (-u) runs. With other modules it runs one only from the modules of
v1.0.3 to v1.0.8 and a directory of v1.0.5 to v1.0.8: after the backup it
removes the old App's containers (from then on the backup is the only way
back), upgrades the modules once, checks the result, holds every mail of the
mail queue (the mail the upgrade queued and the mail that waited before it)
and puts the cron flags back. It asks to type the database name, or takes
--confirm DATABASE. --drop-retired-data agrees to delete the data of retired
modules; it is saved as CSV in the backup folder first.
--release-queued-mail queues the held mail again, after its review.
HTTP only: default 0.0.0.0:8110. Odoo ports are private to the Compose network.
Optional keys, read only from --config or app.env: PUBLIC_ROOT (such as /dev,
v1.0.4 or later) serves the page under http://HOST:PORT/dev/app/;
ENVIRONMENT_LABEL is the label on the sign-in page (v1.0.4 or later);
PUBLIC_BASE_URL (such as https://HOST/dev) is the address browsers use.
HELP
}
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
cleanup() {
    local status=$?
    trap - EXIT
    # Whatever happens now, the cleanup runs to its end: a second signal, or a
    # message the hung-up terminal of a lost SSH session can no longer take,
    # does not stop it. The commands it starts ignore those signals as well.
    trap '' HUP INT TERM
    set +e
    if (( status != 0 && STARTED )); then
        printf 'Deployment did not pass verification. Existing database and volumes were retained.\n' >&2
        printf 'Logs: docker compose --project-name %q --file %q logs --tail 100\n' "$PROJECT_NAME" "$DEPLOY_DIR/compose.yml" >&2
    fi
    # Before the temporary Docker configuration goes: it may start the old App again.
    if (( status != 0 && UPGRADE )) && [[ -n $OLD_RELEASE ]]; then upgrade_stopped; fi
    [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
    [[ -z $AUTH_DIR ]] || rm -rf -- "$AUTH_DIR"
    exit "$status"
}
stop_module_upgrade() {  # the one-off -u container must not run on after this script
    if [[ -n $UPGRADE_PID ]]; then
        kill "$UPGRADE_PID" 2>/dev/null
        wait "$UPGRADE_PID" 2>/dev/null
        UPGRADE_PID=''
    fi
    [[ -z $UPGRADE_CONTAINER ]] || docker rm -f "$UPGRADE_CONTAINER" >/dev/null 2>&1
}
upgrade_stopped() {  # what a failed --upgrade leaves, and the way back
    local restore="$BACKUP_DIR/restore.txt"
    [[ -n $BACKUP_DIR ]] || restore="restore.txt in the backup folder of the earlier --upgrade (in $DEPLOY_DIR/backups or its --backup-dir)"

    case $UPGRADE_STATE in
        '')
            if ((DB_MARKED)); then
                printf 'Nothing was changed by this run. The database carries the mark of an earlier module upgrade that stopped, so %s cannot run on it: restore the backup of that upgrade (restore.txt in its backup folder, in %s/backups or its --backup-dir).\n' "$OLD_RELEASE" "$DEPLOY_DIR" >&2
            else
                printf 'Nothing was changed: %s still runs %s.\n' "$DEPLOY_DIR" "$OLD_RELEASE" >&2
            fi ;;
        marked|migrating|migrated)
            # The database may carry the mark (a mark that was refused went
            # back to 'backed-up'): the old release must not run on it, and
            # the backup is the only way back.
            stop_module_upgrade
            # A stop between the mark and the removal: remove them now.
            ((OLD_REMOVED)) || current rm --stop --force odoo web >&2
            printf 'The module upgrade from %s to %s stopped ' "$OLD_RELEASE" "$RELEASE" >&2
            case $UPGRADE_STATE in
                marked)
                    printf 'after the backup, before the module upgrade (-u) started. The database holds the data of the backup. The mark of this upgrade was written, or its result is not known (above).\n' >&2
                    printf 'The containers of %s are removed, and its App is not started again: %s cannot run on a database that carries the mark (its database check refuses it), and this run cannot tell that the mark is absent.\n' "$OLD_RELEASE" "$OLD_RELEASE" >&2 ;;
                migrating|migrated)
                    if [[ $UPGRADE_STATE == migrating ]]; then
                        printf 'while the module upgrade (-u) ran. The database is partly upgraded. Log: %s/module-upgrade.log\n' "$BACKUP_DIR" >&2
                    else
                        printf 'after the module upgrade (-u): the check of its result failed (above). Log: %s/module-upgrade.log\n' "$BACKUP_DIR" >&2
                    fi
                    printf 'The containers of %s were removed when the upgrade started, and its App is not started again: %s cannot run on this database any more (its database check refuses the mark of this upgrade).\n' "$OLD_RELEASE" "$OLD_RELEASE" >&2 ;;
            esac
            printf 'The only way back is the backup: follow %s/restore.txt, all of its steps. Then %s runs again with the data of the backup, and --upgrade can run again once the cause is solved. restore.txt:\n' "$BACKUP_DIR" "$OLD_RELEASE" >&2
            cat -- "$BACKUP_DIR/restore.txt" >&2 ;;
        stopped|backed-up)
            # An incomplete backup is no use to anyone; a complete one stays.
            if [[ $UPGRADE_STATE == stopped && -n $BACKUP_DIR ]]; then rm -rf -- "$BACKUP_DIR"; BACKUP_DIR=''; fi
            printf 'The upgrade stopped before the switch: %s still runs %s, with its own files and containers.\n' "$DEPLOY_DIR" "$OLD_RELEASE" >&2
            if ((WAS_RUNNING)); then
                printf 'Starting the App of %s again...\n' "$OLD_RELEASE" >&2
                if docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" \
                        up --detach --no-recreate --wait --wait-timeout "$STARTUP_TIMEOUT" >&2; then
                    printf 'The App of %s runs again.\n' "$OLD_RELEASE" >&2
                else
                    printf 'The App of %s did not start again. Start it with: sudo bash service.sh --role app --dir %q start\n' "$OLD_RELEASE" "$DEPLOY_DIR" >&2
                fi
            else
                printf 'Its App was not running before the upgrade, and stays stopped.\n' >&2
            fi
            [[ -z $BACKUP_DIR ]] || printf 'The backup in %s is complete and kept.\n' "$BACKUP_DIR" >&2 ;;
        switched)
            if ((MODULE_UPGRADE)); then
                printf 'The module upgrade from %s to %s stopped after the switch. Now:\n' "$OLD_RELEASE" "$RELEASE" >&2
                printf '  - %s names %s: its identity, compose.yml and app.env are those of %s.\n' "$DEPLOY_DIR" "$RELEASE" "$RELEASE" >&2
                printf '  - The modules of database %s are upgraded and checked. Its database check reports UPGRADE_PENDING until a deployment of %s finishes.\n' "$DB_NAME" "$RELEASE" >&2
                if ((STARTED)); then
                    printf '  - Its containers were created from the %s images. They may be running, but did not pass the checks above.\n' "$RELEASE" >&2
                else
                    printf '  - The App is stopped.\n' >&2
                fi
                printf 'To finish the upgrade: solve the problem above, then run sudo bash deploy-app.sh --dir %q from this scripts folder, without --upgrade. It runs no module upgrade again.\n' "$DEPLOY_DIR" >&2
                printf '%s cannot run on this database. The only way back is the backup: %s. Use it only before users write data with %s (the point of no return): all data written after the backup is lost.\n' "$OLD_RELEASE" "$restore" "$RELEASE" >&2
                return
            fi
            printf 'The upgrade from %s to %s stopped after the switch. Now:\n' "$OLD_RELEASE" "$RELEASE" >&2
            printf '  - %s names %s: its identity, compose.yml and app.env are those of %s.\n' "$DEPLOY_DIR" "$RELEASE" "$RELEASE" >&2
            if ((STARTED)); then
                printf '  - Its containers were recreated from the %s images. They may be running, but did not pass the checks above.\n' "$RELEASE" >&2
                printf '  - The database holds the data of the backup and whatever the App wrote since it started.\n' >&2
            else
                printf '  - The App is stopped. The containers of %s were not removed or changed.\n' "$OLD_RELEASE" >&2
                printf '  - The database holds the data of the backup. Only report.url and web.base.url may have been written again, with the values from app.env.\n' >&2
            fi
            printf 'To finish the upgrade: solve the problem above, then run sudo bash deploy-app.sh --dir %q from this scripts folder, without --upgrade.\n' "$DEPLOY_DIR" >&2
            printf 'To go back to %s: put back its identity and settings, then deploy it without --upgrade:\n' "$OLD_RELEASE" >&2
            go_back_steps >&2
            printf 'To also put the data back as it was before the upgrade: follow %s/restore.txt instead (its step 5 is the above).\n' "$BACKUP_DIR" >&2 ;;
    esac
}
trap cleanup EXIT
# A lost SSH session (SIGHUP) fails the run as Ctrl-C does. Without its own trap,
# bash would run the EXIT trap with the status of the last command, often 0,
# and a stopped upgrade would neither start the old App again nor say so.
trap 'exit 129' HUP
trap 'exit 130' INT TERM
# Only the main shell reports. A command that fails inside $( ) or <( ) is either
# checked where its result is used or leaves the deployment going.
trap '[[ $BASHPID != "$$" ]] || printf "Deployment failed at line %s.\n" "$LINENO" >&2' ERR
while (($#)); do
    case $1 in
        --config|--dir|--backup-dir|--confirm)
            (($# >= 2)) || fail "$1 requires a value"
            case $1 in
                --config) CONFIG=$2 ;;
                --dir) DEPLOY_DIR=$2 ;;
                --backup-dir) BACKUP_BASE=$2 ;;
                --confirm) CONFIRM=$2; [[ -n $CONFIRM ]] || fail '--confirm requires the database name' ;;
            esac
            shift 2 ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --init-db) INIT_DB=1; shift ;;
        --check-config) CHECK_ONLY=1; shift ;;
        --upgrade) UPGRADE=1; shift ;;
        --drop-retired-data) DROP_RETIRED=1; shift ;;
        --release-queued-mail) RELEASE_MAIL=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
[[ $DEPLOY_DIR == /* && $DEPLOY_DIR != / && $DEPLOY_DIR != *$'\n'* ]] || fail '--dir must be an absolute directory path, not /'
[[ ! -L $DEPLOY_DIR ]] || fail 'Deployment directory must not be a symbolic link'
if ((UPGRADE)); then
    ((INIT_DB == 0 && CHECK_ONLY == 0)) || fail '--upgrade cannot be combined with --init-db or --check-config'
    [[ -f $DEPLOY_DIR/.deployment-identity && ! -L $DEPLOY_DIR/.deployment-identity ]] \
        || fail "--upgrade moves an existing deployment to this release, and $DEPLOY_DIR has none (no .deployment-identity). Deploy it with deploy-app.sh without --upgrade"
    [[ -n $CONFIG || -f $DEPLOY_DIR/app.env ]] \
        || fail "--upgrade reads the settings of the deployment from $DEPLOY_DIR/app.env, which is missing; pass them with --config"
    [[ -z $BACKUP_BASE || ( $BACKUP_BASE == /* && $BACKUP_BASE != / && $BACKUP_BASE != *$'\n'* ) ]] \
        || fail '--backup-dir must be an absolute directory path, not /'
else
    [[ -z $BACKUP_BASE ]] || fail '--backup-dir applies only to --upgrade'
    [[ -z $CONFIRM ]] || fail '--confirm applies only to --upgrade'
    ((DROP_RETIRED == 0)) || fail '--drop-retired-data applies only to --upgrade'
fi
if ((RELEASE_MAIL)); then
    ((UPGRADE == 0 && INIT_DB == 0 && CHECK_ONLY == 0)) \
        || fail '--release-queued-mail runs on its own, after an upgrade finished: not with --upgrade, --init-db or --check-config'
    [[ -f $DEPLOY_DIR/.deployment-identity && ! -L $DEPLOY_DIR/.deployment-identity ]] \
        || fail "--release-queued-mail needs a deployment, and $DEPLOY_DIR has none (no .deployment-identity)"
fi
if [[ -z $CONFIG && -f $DEPLOY_DIR/app.env ]]; then
    CONFIG=$DEPLOY_DIR/app.env
    # Before the first deployment, app.env can only be the operator's: every
    # run of this script writes it together with .deployment-identity.
    if [[ ! -e $DEPLOY_DIR/.deployment-identity && ! -L $DEPLOY_DIR/app.env ]]; then PREPARED=1; fi
fi
if [[ -n $CONFIG ]]; then
    [[ -r $CONFIG ]] || fail 'Configuration file is not readable'
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        [[ $line =~ ^([A-Z_]+)=(.*)$ ]] || fail 'Configuration must contain literal KEY=VALUE lines'
        key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
        case $key in
            DB_HOST|DB_PORT|DB_NAME|DB_USER|DB_PASSWORD_FILE|PROJECT_NAME|HTTP_PORT|BIND_IP|STARTUP_TIMEOUT|INIT_TIMEOUT|PUBLIC_ROOT|ENVIRONMENT_LABEL|PUBLIC_BASE_URL)
                printf -v "$key" '%s' "$value"
                [[ -z $value ]] || SET[$key]=1 ;;
            *) fail "Unknown configuration key: $key" ;;
        esac
    done < "$CONFIG"
fi
# --check-config never asks: there must be settings to check.
((CHECK_ONLY == 0)) || [[ -n $CONFIG ]] || fail "--check-config needs --config or $DEPLOY_DIR/app.env"
prompt() {
    local name=$1 label=$2 answer
    if ((NON_INTERACTIVE)); then [[ -n ${!name} ]] || fail "$name is required in --config"; return; fi
    [[ -t 0 ]] || fail 'Interactive deployment requires a terminal; use --config and --non-interactive'
    read -r -p "$label [${!name}]: " answer || fail 'Input cancelled'
    [[ -z $answer ]] || printf -v "$name" '%s' "$answer"
    [[ -n ${!name} ]] || fail "$name is required"
}
db_host_problem() {  # why DB_HOST cannot be used, if it cannot
    case $DB_HOST in
        '')
            printf 'DB_HOST is empty: it is the IP address of the database server, which deploy-db.sh prints at the end.' ;;
        localhost|127.*|::1|0.0.0.0)
            printf '%s would be the App container itself, not the database. With the database on this server, use the DB_HOST that deploy-db.sh printed (Docker'"'"'s address of this server, usually 172.17.0.1).' "$DB_HOST" ;;
        *) [[ $DB_HOST =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]] || printf 'DB_HOST must be an IP address or hostname (no URL or shell syntax).' ;;
    esac
}
local_database() {  # the settings deploy-db.sh recorded, when it set up exactly one database on this server
    local records=() line
    mapfile -t records < <(find /var/lib/perodua-db-deploy -mindepth 3 -maxdepth 3 -name connection.txt 2>/dev/null)
    ((${#records[@]} == 1)) || return 1
    while IFS= read -r line || [[ -n $line ]]; do
        # A value the configuration file gives stays.
        if [[ $line =~ ^(DB_HOST|DB_PORT|DB_NAME|DB_USER)=([A-Za-z0-9_.:-]+)$ && -z ${SET[${BASH_REMATCH[1]}]:-} ]]; then
            printf -v "${BASH_REMATCH[1]}" '%s' "${BASH_REMATCH[2]}"
        fi
    done < "${records[0]}"
}
server_addresses() {  # this server's own IPv4 addresses, without loopback and Docker's
    local gateways=() address
    if command -v docker >/dev/null; then
        # shellcheck disable=SC2046 # one argument per network ID
        read -r -d '' -a gateways < <(docker network inspect $(docker network ls -q 2>/dev/null) \
            --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' 2>/dev/null) || true
    fi
    for address in $(hostname -I 2>/dev/null); do
        [[ $address == *.* && $address != 127.* && $address != 169.254.* ]] || continue
        [[ " ${gateways[*]} " != *" $address "* ]] || continue
        printf '%s\n' "$address"
    done
}
is_private_ipv4() { [[ $1 =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]; }
public_addresses() {
    local address
    for address in $(server_addresses); do is_private_ipv4 "$address" || printf '%s\n' "$address"; done
}
choose_bind_ip() {
    local private=() public=() options answer default number=1 address
    for address in $(server_addresses); do
        if is_private_ipv4 "$address"; then private+=("$address"); else public+=("$address"); fi
    done
    options=(127.0.0.1 "${private[@]}" 0.0.0.0)
    printf '\nWho may open the web page?\n'
    printf '  1) Only this server (127.0.0.1): open it from your computer through an SSH tunnel\n'
    for address in "${private[@]}"; do
        number=$((number + 1))
        printf '  %s) Computers on the private network, through %s\n' "$number" "$address"
    done
    printf '  %s) Every computer that can reach this server (all addresses)\n' "${#options[@]}"
    default=1
    ((${#private[@]} == 0)) || default=2
    while :; do
        read -r -p "Choose [$default]: " answer || fail 'Input cancelled'
        answer=${answer:-$default}
        if [[ ! $answer =~ ^[1-9][0-9]?$ ]] || ((answer > ${#options[@]})); then
            printf 'Enter a number from 1 to %s.\n' "${#options[@]}"
            continue
        fi
        BIND_IP=${options[answer - 1]}
        [[ $BIND_IP == 0.0.0.0 && ${#public[@]} -gt 0 ]] || break
        printf '%s is a public internet address: anyone on the internet could open the page and sign in with the UAT passwords in the README, unless a cloud firewall (security group) blocks port %s. Docker opens published ports past ufw.\n' "${public[*]}" "$HTTP_PORT"
        read -r -p 'Open it on every address anyway? [y/N]: ' answer || fail 'Input cancelled'
        [[ ${answer:-n} != [Yy]* ]] || break
    done
}
ask_database() {  # the database questions of a first interactive run
    if ((NON_INTERACTIVE == 0)) && local_database; then
        printf 'deploy-db.sh set up the database on this server: its settings are the defaults below.\n'
    fi
    while :; do
        prompt DB_HOST 'Database server IP / hostname'
        problem=$(db_host_problem)
        [[ -n $problem ]] || break
        ((NON_INTERACTIVE == 0)) || fail "$problem"
        printf '%s\n' "$problem"
        DB_HOST=''
    done
    prompt DB_PORT 'Database port'
    prompt DB_NAME 'Application database name'
    prompt DB_USER 'Database username'
}
# Supplying a complete configuration avoids repeated prompts on subsequent runs.
if [[ -z $CONFIG ]]; then
    ask_database
    prompt HTTP_PORT 'Web port that browsers open on this server'
    ((NON_INTERACTIVE)) || choose_bind_ip
elif [[ -z $DB_HOST ]]; then
    # A configuration without the database settings, such as an app.env written
    # by hand with only PUBLIC_ROOT and the other settings of the page: a run on
    # a terminal asks the database questions, with the file's values as defaults.
    if ((CHECK_ONLY || NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        fail "$CONFIG has no DB_HOST. Add DB_HOST=<IP address of the database server> (deploy-db.sh prints it at the end), or run sudo bash deploy-app.sh on a terminal without --check-config and --non-interactive: it asks for the database settings that the file leaves out"
    fi
    printf '%s has no database settings: answer the questions below. They are saved in %s/app.env.\n' "$CONFIG" "$DEPLOY_DIR"
    ask_database
fi
problem=$(db_host_problem)
[[ -z $problem ]] || fail "$problem"
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
# The page under a path (/dev, /uat) behind a front proxy or F5 that keeps the
# path; the web container adds it to its own links. Empty: the page is at /app/.
[[ $PUBLIC_ROOT =~ ^(/[a-z0-9][a-z0-9-]{0,30})?$ ]] \
    || fail 'PUBLIC_ROOT must be empty or one path segment such as /dev or /uat: a / and 1-31 lowercase letters, digits or -, not starting with -, and no / at the end'
label_pattern='^[A-Za-z0-9 ._()-]{1,40}$'
[[ -z $ENVIRONMENT_LABEL || $ENVIRONMENT_LABEL =~ $label_pattern ]] \
    || fail 'ENVIRONMENT_LABEL must be empty or 1-40 letters, digits, spaces and . _ ( ) -'
# PUBLIC_BASE_URL: the host is dot-separated DNS labels (or an IPv4 address);
# it becomes the frozen web.base.url, which only Odoo's settings can change.
url_pattern='^https?://([A-Za-z0-9.-]+)(:[0-9]{1,5})?(/[a-z0-9][a-z0-9-]{0,30})?$'
host_pattern='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
if [[ -n $PUBLIC_BASE_URL ]]; then
    [[ $PUBLIC_BASE_URL =~ $url_pattern ]] \
        || fail 'PUBLIC_BASE_URL must be empty or http(s)://HOST[:PORT][/PATH] without a / at the end, such as https://stgissrp.perodua.com.my/dev'
    url_host=${BASH_REMATCH[1]} url_port=${BASH_REMATCH[2]#:} url_path=${BASH_REMATCH[3]}
    [[ ${#url_host} -le 253 && $url_host =~ $host_pattern ]] \
        || fail 'PUBLIC_BASE_URL has an invalid host name: labels of letters, digits and - separated by single dots, each starting and ending with a letter or digit'
    [[ -z $url_port ]] || ((10#$url_port >= 1 && 10#$url_port <= 65535)) || fail 'PUBLIC_BASE_URL has an invalid port'
    # The page is served at PUBLIC_ROOT and nowhere else, so the address has
    # exactly that path: none at all when PUBLIC_ROOT is empty.
    if [[ -n $PUBLIC_ROOT ]]; then
        [[ $url_path == "$PUBLIC_ROOT" ]] \
            || fail "PUBLIC_BASE_URL must end with PUBLIC_ROOT ($PUBLIC_ROOT), such as https://HOST$PUBLIC_ROOT"
    else
        [[ -z $url_path ]] \
            || fail "PUBLIC_BASE_URL has the path $url_path but PUBLIC_ROOT is empty: set PUBLIC_ROOT=$url_path, or remove the path"
    fi
fi
if ((PUBLIC_ROOT_SUPPORTED == 0)); then
    [[ -z $PUBLIC_ROOT ]] \
        || fail "PUBLIC_ROOT needs Client Stable UIUX v1.0.4 or later, and this scripts folder deploys $RELEASE. Use the scripts folder of v1.0.4 or later, or leave PUBLIC_ROOT empty"
    [[ -z $ENVIRONMENT_LABEL ]] \
        || printf 'Warning: ENVIRONMENT_LABEL has no effect on %s; the sign-in page shows it from v1.0.4.\n' "$RELEASE" >&2
fi
# Without a frozen base URL, Odoo records the address of every administrator
# sign-in as web.base.url. Behind the web container that is http:// and has no
# path (the container removes PUBLIC_ROOT before Odoo), so links Odoo builds
# would miss the path.
if [[ -n $PUBLIC_ROOT && -z $PUBLIC_BASE_URL ]]; then
    printf 'Warning: PUBLIC_ROOT is set without PUBLIC_BASE_URL. Odoo will record http://HOST without %s as its base URL when an administrator signs in; set PUBLIC_BASE_URL, such as https://HOST%s.\n' "$PUBLIC_ROOT" "$PUBLIC_ROOT" >&2
fi
# Odoo hands a sign-in on to the module host names only when the frozen
# web.base.url is https:// on the sign-in host, with no port other than 443.
# Otherwise every host name keeps its own password sign-in, as before v1.0.5;
# the menu of each name is the same either way.
if ((HANDOVER_SUPPORTED)); then
    handover_off="so the sign-in hand-over of $RELEASE stays off: each host name keeps its own password sign-in. Set PUBLIC_BASE_URL=https://$HANDOVER_HOST$PUBLIC_ROOT"
    if [[ -z $PUBLIC_BASE_URL ]]; then
        printf 'Warning: PUBLIC_BASE_URL is empty, %s.\n' "$handover_off" >&2
    elif [[ $PUBLIC_BASE_URL != https://* ]]; then
        printf 'Warning: PUBLIC_BASE_URL does not start with https://, %s.\n' "$handover_off" >&2
    elif [[ ${url_host,,} != "$HANDOVER_HOST" ]]; then
        printf 'Warning: PUBLIC_BASE_URL names %s, not the sign-in host %s. Unless the Odoo system parameter perodua_client_stable.hosts names %s as the sign-in host, %s.\n' \
            "$url_host" "$HANDOVER_HOST" "$url_host" "$handover_off" >&2
    elif [[ -n $url_port ]] && ((10#$url_port != 443)); then
        printf 'Warning: PUBLIC_BASE_URL has the port %s, not 443, %s.\n' "$url_port" "$handover_off" >&2
    fi
fi
[[ -z $DB_PASSWORD_FILE || ( $DB_PASSWORD_FILE == /* && -r $DB_PASSWORD_FILE && -f $DB_PASSWORD_FILE ) ]] || fail 'DB_PASSWORD_FILE must be an absolute path to a readable file'
if ((NON_INTERACTIVE)) && [[ -z $DB_PASSWORD_FILE && ! -r $DEPLOY_DIR/secrets/db_password ]]; then
    fail 'DB_PASSWORD_FILE is required for the first non-interactive deployment'
fi
for helper in uat_guard.py uat_admins.py; do
    [[ -f $SCRIPT_DIR/$helper && ! -L $SCRIPT_DIR/$helper ]] || fail "$helper is missing next to deploy-app.sh; run it from the complete release directory"
done
# service.sh reset runs this before it deletes anything, so that settings this
# release refuses stop the reset while the data is still there. The directory
# identity is not compared: the reset binds the directory to this release.
password_problem() {  # why $1 cannot be the database password, if it cannot
    [[ -n $1 && $1 != *$'\n'* && $1 != *$'\r'* ]] || printf 'Database password must be nonempty and contain no line breaks'
}
if ((CHECK_ONLY)); then
    # A real run reads the password only after the checks above. Read it here
    # too, from the same file, so that a reset does not delete the data and
    # then refuse the password.
    password_source=$DB_PASSWORD_FILE
    [[ -n $password_source || ! -r $DEPLOY_DIR/secrets/db_password ]] || password_source=$DEPLOY_DIR/secrets/db_password
    if [[ -n $password_source ]]; then
        password=$(<"$password_source")
        problem=$(password_problem "$password")
        unset password
        [[ -z $problem ]] || fail "$problem ($password_source)"
    fi
    printf 'Configuration valid for %s. Nothing was changed.\n' "$RELEASE"
    exit 0
fi
command -v docker >/dev/null || fail 'Docker is missing. First run install-dependencies.sh --role app on Ubuntu 24.04'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required'
docker info >/dev/null 2>&1 || fail 'Cannot reach Docker. Start the engine and check your account permissions'
engine=$(docker info --format '{{.OSType}}/{{.Architecture}}')
[[ $engine == linux/x86_64 || $engine == linux/amd64 ]] || fail 'The Docker engine must run Linux amd64 containers'
mkdir -p -- "$DEPLOY_DIR"
DEPLOY_DIR=$(cd -- "$DEPLOY_DIR" && pwd -P)
[[ $DEPLOY_DIR != / && $DEPLOY_DIR != *$'\n'* ]] || fail 'Unsupported directory path'
# A first deployment that has not used its database yet: it stopped before (a
# wrong DB_HOST or password, a firewall, a DB server that refused this server).
# Until then its database settings, password and release may change; its
# project may not, because its Docker network and volume carry that name.
UNVERIFIED=$DEPLOY_DIR/.deployment-unverified
unverified() { [[ -f $UNVERIFIED && ! -L $UNVERIFIED ]]; }
if [[ ! -e $DEPLOY_DIR/.deployment-identity ]]; then
    others=(! -name .deploy.lock)
    ((PREPARED == 0)) || others+=(! -name app.env)
    [[ -z $(find "$DEPLOY_DIR" -mindepth 1 -maxdepth 1 "${others[@]}" -print -quit) ]] \
        || fail 'Use an empty deployment directory. Before the first deployment it may hold only app.env, as the settings that a run without --config reads; keep password files elsewhere'
fi
chmod 0700 "$DEPLOY_DIR"
exec 9>"$DEPLOY_DIR/.deploy.lock"
flock -n 9 || fail 'Another deployment is running in this directory'
TEMP_DIR=$(mktemp -d "$DEPLOY_DIR/.deploy.XXXXXXXX")
printf '%s\n' "release=$RELEASE" "revision=$REVISION" "project=$PROJECT_NAME" "host=$DB_HOST" "port=$DB_PORT" "database=$DB_NAME" "user=$DB_USER" > "$TEMP_DIR/identity"
same_but_release() {  # whether the identity $1 differs from this run's in the release only
    [[ $(grep -Ev '^(release|revision)=' "$1") == "$(grep -Ev '^(release|revision)=' "$TEMP_DIR/identity")" ]]
}
check_upgrade() {
    local release revision
    release=$(sed -n 's/^release=//p' "$DEPLOY_DIR/.deployment-identity")
    revision=$(sed -n 's/^revision=//p' "$DEPLOY_DIR/.deployment-identity")
    [[ $release =~ ^[a-z0-9.-]+$ && $revision =~ ^[0-9a-f]{40}$ ]] || fail "Unreadable $DEPLOY_DIR/.deployment-identity"
    OLD_RELEASE=$release OLD_REVISION=$revision
    ! unverified \
        || fail "The first deployment in $DEPLOY_DIR stopped before it used its database, so it has no data to keep. Finish it with deploy-app.sh without --upgrade"
    same_but_release "$DEPLOY_DIR/.deployment-identity" \
        || fail "--upgrade keeps the project, database server, port, database and user of the deployment, and $CONFIG names others than $DEPLOY_DIR/.deployment-identity: $(diff <(grep -Ev '^(release|revision)=' "$DEPLOY_DIR/.deployment-identity") <(grep -Ev '^(release|revision)=' "$TEMP_DIR/identity") | sed -n 's/^> //p' | paste -sd ' ' -)"
    ! cmp -s "$TEMP_DIR/identity" "$DEPLOY_DIR/.deployment-identity" \
        || fail "$DEPLOY_DIR already runs $RELEASE. Run deploy-app.sh without --upgrade"
    printf 'Upgrade of %s from %s (%s) to %s (%s). The database %s and the attachments are kept.\n' \
        "$DEPLOY_DIR" "$OLD_RELEASE" "$OLD_REVISION" "$RELEASE" "$REVISION" "$DB_NAME"
}
if [[ -e $DEPLOY_DIR/.deployment-identity ]]; then
    if ((UPGRADE)); then
        check_upgrade
    elif ! cmp -s "$TEMP_DIR/identity" "$DEPLOY_DIR/.deployment-identity"; then
        if unverified && [[ $(grep -x 'project=.*' "$TEMP_DIR/identity") == "$(grep -x 'project=.*' "$DEPLOY_DIR/.deployment-identity")" ]]; then
            printf 'The earlier first run in %s stopped before it used its database, so its settings may still change: using the ones of this run.\n' "$DEPLOY_DIR"
        elif same_but_release "$DEPLOY_DIR/.deployment-identity"; then
            fail "$DEPLOY_DIR runs $(sed -n 's/^release=//p' "$DEPLOY_DIR/.deployment-identity"). To move it to $RELEASE and keep its data, run deploy-app.sh --upgrade --dir $DEPLOY_DIR (see docs/DEPLOYMENT.md); otherwise use a separate deployment directory"
        else
            fail 'Directory belongs to a different database, project, or release. Use a separate deployment directory'
        fi
    fi
elif [[ -e $DEPLOY_DIR/compose.yml || -e $DEPLOY_DIR/secrets ]] || { [[ -e $DEPLOY_DIR/app.env ]] && ((PREPARED == 0)); }; then
    fail 'Unmanaged deployment files already exist in this directory; use an empty directory'
fi
if ((RELEASE_MAIL)); then
    # The mail a module upgrade held (verify-upgrade), queued again after the
    # owner reviewed it. Only the rows that upgrade held (the mail it queued
    # and the mail that waited in the queue before it), and only once the
    # deployment of this release finished (the database is stamped READY).
    ! unverified || fail "The first deployment in $DEPLOY_DIR stopped before it used its database: there is no held mail"
    printf 'Queuing the mail that the module upgrade held in database %s again...\n' "$DB_NAME"
    released=$(docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" \
        run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py release-mail </dev/null) \
        || fail 'The held mail was not released, for the reason above. Nothing was changed'
    [[ $released =~ ^RELEASED\ ([0-9]+)\ ([0-9]+)$ ]] || fail "Unexpected answer from the database check: $released"
    printf '%s held mail(s) are queued again (outgoing): %s that the module upgrade queued, %s that waited in the queue before the upgrade. Odoo sends them with its mail queue.\n' \
        "$((10#${BASH_REMATCH[1]} + 10#${BASH_REMATCH[2]}))" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    exit 0
fi
containers=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME")
for container in $containers; do
    owner=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")
    [[ $owner == "$DEPLOY_DIR" ]] || fail 'PROJECT_NAME is already used from a different directory'
done
FILESTORE_VOLUME=${PROJECT_NAME}_filestore ADOPTED=0
if [[ ! -e $DEPLOY_DIR/.deployment-identity ]]; then
    for kind in volume network; do
        existing=$(docker "$kind" ls -q --filter "label=com.docker.compose.project=$PROJECT_NAME")
        # An App uninstall keeps the attachments volume. It is used again below
        # with the same database, or offered for deletion for a new empty one.
        if [[ $kind == volume && $existing == "$FILESTORE_VOLUME" ]]; then ADOPTED=1; continue; fi
        [[ -z $existing ]] || fail 'PROJECT_NAME already owns Docker resources; choose a new project name'
    done
fi
# Whether the volume, and so perhaps attachments, existed before this run.
LEFTOVER=0
if docker volume inspect "$FILESTORE_VOLUME" >/dev/null 2>&1; then LEFTOVER=1; fi
if unverified && [[ -z $DB_PASSWORD_FILE || $DB_PASSWORD_FILE -ef $DEPLOY_DIR/secrets/db_password ]] \
        && ((NON_INTERACTIVE == 0)) && [[ -t 0 && -r $DEPLOY_DIR/secrets/db_password ]]; then
    # The password the earlier first run saved may be the reason it stopped.
    read -r -s -p 'Database password (hidden; Enter keeps the one entered before): ' password || fail 'Input cancelled'
    printf '\n'
    [[ -n $password ]] || password=$(<"$DEPLOY_DIR/secrets/db_password")
elif [[ -n $DB_PASSWORD_FILE ]]; then
    password=$(<"$DB_PASSWORD_FILE")
elif [[ -r $DEPLOY_DIR/secrets/db_password ]]; then
    password=$(<"$DEPLOY_DIR/secrets/db_password")
else
    ((NON_INTERACTIVE == 0)) || fail 'DB_PASSWORD_FILE is required for the first non-interactive deployment'
    [[ -t 0 ]] || fail 'Password input requires a terminal'
    read -r -s -p 'Database password (hidden): ' password || fail 'Input cancelled'
    printf '\n'
fi
problem=$(password_problem "$password")
[[ -z $problem ]] || fail "$problem"
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
if [[ -n $CONFIG && $BIND_IP == 0.0.0.0 ]]; then  # asked on the terminal otherwise
    exposed=$(public_addresses | paste -sd ' ' -)
    if [[ -n $exposed ]]; then
        printf 'Warning: BIND_IP=0.0.0.0 opens the web page on every address of this server, including the public %s. Unless a cloud firewall (security group) blocks port %s, anyone on the internet can sign in with the UAT passwords. Docker opens published ports past ufw.\n' "$exposed" "$HTTP_PORT" >&2
    fi
fi
pull_image "$ODOO_IMAGE"
pull_image "$WEB_IMAGE"

cat > "$TEMP_DIR/preflight.py" <<'PY'
# Modes: check | mark-pending | verify-fresh | stamp | public-urls [BASE_URL].
# States printed by check:
#   MISSING / MISSING_NO_CREATEDB / EMPTY     nothing initialized yet
#   SETUP_PENDING                             a fresh UAT initialization whose
#                                             administrators are not set up yet
#   SETUP_UNMARKED                            the same, but the run stopped before
#                                             it could even record that
#   READY <attachments>                       initialized and stamped
# perodua.uat_init records the fresh path: 'pending' after modules install,
# 'complete' once stamped. Databases without it are restored/legacy databases
# and are held to RESTORED_MODULES exactly as before.
# public-urls, on any initialized database: report.url, and with BASE_URL a
# frozen web.base.url. Prints PUBLIC_URLS_SET.
#
# A module upgrade (deploy-app.sh --upgrade to other modules), in this order:
#   upgrade-check FROM   check, with the module upgrade's refusals when the
#                        stored fingerprint is FROM: prints RETIRED lines, the
#                        MODULES to upgrade, then MODULE_UPGRADE <attachments>
#   retired-export       the rows the upgrade deletes, as CSV in a tar.gz
#   mark-upgrading FROM OLD NEW
#                        saves the cron flags and the last mail id, and sets
#                        perodua.image_modhash to 'upgrading:FROM'. Neither the
#                        old release's check nor this one's accepts that.
#   verify-upgrade       after -u: puts the cron flags back (a mock intake pull
#                        stays off), holds every outgoing mail (the mail -u
#                        queued and the mail that waited before it), checks
#                        the result and sets 'upgraded:<fingerprint>'. Prints
#                        UPGRADE_VERIFIED <queued by -u> <waited before>
#   check --accept-pending
#                        prints UPGRADE_PENDING <attachments> for that state;
#                        without the option check refuses it
#   stamp-upgrade        after the deployment: the fingerprint, then READY
#   release-mail         queues the held mail again:
#                        RELEASED <queued by -u> <waited before>
import ast, csv, hashlib, io, json, os, pathlib, re, sys, tarfile, time
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
            category = 'incorrect username or password: use the DB_USER and the password set with deploy-db.sh'
        elif 'no pg_hba.conf entry' in detail:
            category = ('the DB server does not accept connections from this App server: on the DB server, APP_CIDR in'
                        ' deploy.conf must contain the App server address that the DB server sees; correct it and run deploy-db.sh again')
        elif 'connection refused' in detail:
            category = ('connection refused: nothing listens at DB_HOST port DB_PORT; DB_HOST is the address deploy-db.sh'
                        ' printed (DB_LISTEN_IP on the DB server), and PostgreSQL must be running there')
        elif 'timeout' in detail:
            category = ('connection timed out: a firewall between the servers, or on the DB server (ufw, a cloud security group),'
                        ' blocks TCP DB_PORT from this App server')
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

# ── Module upgrade ───────────────────────────────────────────────────────────
UPGRADING, UPGRADED = 'upgrading:', 'upgraded:'
KIT_PARAM = 'perodua.kit_upgrade'
HOLD_REASON = 'Held by deploy-app.sh --upgrade (module upgrade): release it with deploy-app.sh --release-queued-mail'
# Gone from the new release: perodua_ui 1.79.0 uninstalls the first three in
# the same -u. perodua_demo_ui retires the bridge, which may stay recorded.
RETIRED_MODULES = ('perodua_hw_sim', 'perodua_supplier_transport_ext', 'perodua_warehouse_ext')
ALLOWED_ABSENT = ('perodua_demo_client_ui',)
NEVER_INSTALLED = ('perodua_demo_client', 'perodua_reporting', 'perodua_e2e')
# The data the upgrade deletes with them (plan WP5, D3): tables, columns with a
# value other than their default, and the attachments of the retired models.
RETIRED_TABLES = ('perodua_transporter_rate', 'perodua_transporter_process', 'perodua_supplier_process',
                  'perodua_trip_volume_wizard', 'perodua_supplier_delivery_report_wizard',
                  'perodua_po_invoice_report_wizard', 'perodua_import_receipt_wizard',
                  'perodua_overflow_move_wizard', 'perodua_warehouse_dispatch_wizard_line',
                  'perodua_warehouse_dispatch_wizard', 'perodua_warehouse_putaway_wizard',
                  'perodua_warehouse_stock_count_wizard', 'perodua_demo_control',
                  'perodua_outbound_route_stop', 'perodua_outbound_route', 'perodua_driver_checkin')
RETIRED_MODELS = tuple(t.replace('_', '.') for t in RETIRED_TABLES) + ('perodua.hw.sim',)
RETIRED_COLUMNS = (
    ('stock_location', 'perodua_is_overflow', 'perodua_is_overflow IS TRUE'),
    ('stock_picking', 'perodua_wcs_pick_instruction_id',
     "perodua_wcs_pick_instruction_id IS NOT NULL AND perodua_wcs_pick_instruction_id <> ''"),
    ('stock_picking', 'perodua_dispatch_state',
     "perodua_dispatch_state IS NOT NULL AND perodua_dispatch_state <> 'none'"),
    ('stock_picking', 'perodua_outbound_route_id', 'perodua_outbound_route_id IS NOT NULL'),
)
# Reference records with a unique code that the upgrade creates again when
# their XML ID is missing. The new release adopts a user's record with the
# same code, or keeps the record away; it cannot when the XML ID still exists
# but its record was deleted, and another record holds the code.
REFERENCE_RECORDS = (
    ('perodua_master_data', 'holiday_type_public', 'perodua_holiday_type', 'PUBLIC'),
    ('perodua_master_data', 'holiday_type_state', 'perodua_holiday_type', 'STATE'),
    ('perodua_master_data', 'holiday_type_shutdown', 'perodua_holiday_type', 'SHUTDOWN'),
    ('perodua_master_data', 'holiday_type_company', 'perodua_holiday_type', 'COMPANY'),
    ('perodua_master_data', 'order_cycle_daily', 'perodua_order_cycle', 'DAILY'),
    ('perodua_master_data', 'order_cycle_weekly', 'perodua_order_cycle', 'WEEKLY'),
    ('perodua_master_data', 'order_cycle_bimonthly', 'perodua_order_cycle', 'BIMONTHLY'),
    ('perodua_master_data', 'order_cycle_monthly', 'perodua_order_cycle', 'MONTHLY'),
    ('perodua_orders_ext', 'customer_category_service', 'perodua_customer_category', 'CAT-SERVICE'),
    ('perodua_orders_ext', 'customer_category_body_paint', 'perodua_customer_category', 'CAT-BP'),
)
HOST_CODES = ('rp', 'sp', 'cp')
# The scheduled pulls that read a mock feed while their system resolves to
# mock (perodua_integration.mode.<SYSTEM>, else perodua_integration.mode, else
# mock). verify-upgrade never switches one of them on in that case.
MOCK_INTAKE_CRONS = {
    'perodua_integration.cron_consume_promise_feed': 'PROMISE',
    'perodua_orders_ext.cron_pull_pss_orders': 'PSS',
    'perodua_orders_ext.cron_pull_psos_orders': 'PSOS',
    'perodua_orders_ext.cron_pull_pcircle_orders': 'PCircle',
}

def table_exists(cur, table):
    cur.execute('SELECT to_regclass(%s)', ('public.' + table,))
    return cur.fetchone()[0] is not None
def column_exists(cur, table, column):
    cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                (table, column))
    return cur.fetchone()[0] > 0
def version_key(version):
    return tuple(int(part) for part in re.findall(r'[0-9]+', version or ''))
def params_like(cur, *patterns):
    cur.execute('SELECT key, value FROM ir_config_parameter WHERE ' + ' OR '.join(['key LIKE %s'] * len(patterns)), patterns)
    return dict(cur.fetchall())
def kit_record(cur):
    cur.execute('SELECT value FROM ir_config_parameter WHERE key = %s', (KIT_PARAM,))
    row = cur.fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        fail(KIT_PARAM + ' is not readable')
def in_list(column, values):  # "column IN (%s, ...)" and its values; never empty
    return column + ' IN (' + ', '.join(['%s'] * len(values)) + ')', tuple(values)
def held_counts(record):
    # The held mail as two numbers: the mail -u queued (an id above the last
    # one that mark-upgrading saved) and the mail that waited before it.
    queued = sum(1 for mail in record['held_mail'] if mail > record['mail_max_id'])
    return queued, len(record['held_mail']) - queued
def system_mode(params, system):
    # perodua.connector.mixin._resolve_mode, from the parameters alone.
    for key in ('perodua_integration.mode.' + system, 'perodua_integration.mode'):
        if params.get(key) in ('mock', 'live'):
            return params[key]
    return 'mock'
def retired_counts(cur):
    counts = []
    for table in RETIRED_TABLES:
        if table_exists(cur, table):
            cur.execute('SELECT count(*) FROM "%s"' % table)
            counts.append(('table', table, cur.fetchone()[0]))
    for table, column, condition in RETIRED_COLUMNS:
        if column_exists(cur, table, column):
            cur.execute('SELECT count(*) FROM "%s" WHERE %s' % (table, condition))
            counts.append(('column', table + '.' + column, cur.fetchone()[0]))
    where, values = in_list('res_model', RETIRED_MODELS)
    cur.execute('SELECT res_model, count(*) FROM ir_attachment WHERE ' + where + ' GROUP BY res_model ORDER BY res_model', values)
    counts += [('attachments', model, count) for model, count in cur.fetchall()]
    return counts
def upgrade_modules(installed, image):
    return sorted(name for name in installed if name.startswith('perodua_') and name in image)
def module_upgrade_problems(cur, modules, installed, uat, excluded_rows):
    # Every reason the new release's -u must not run on this database.
    problems = []
    if uat != 'complete':
        problems.append('perodua.uat_init is %s, not complete: only a UAT database that deploy-app.sh --init-db set up takes a module upgrade, not a restored one' % (uat or 'not set'))
    for name in NEVER_INSTALLED:
        if name in installed:
            problems.append(name + ' is installed')
    if excluded_rows:
        problems.append('%d records of %s are in ir_model_data' % (excluded_rows, NEVER_INSTALLED[0]))
    image = image_versions()
    for name in sorted(installed):
        if not name.startswith('perodua_'):
            continue
        if name not in image:
            if name not in RETIRED_MODULES + ALLOWED_ABSENT:
                problems.append(name + ' is installed but is not a module of this release')
        elif version_key(image[name]) < version_key(modules[name][2]):
            problems.append('%s would go down from version %s to %s' % (name, modules[name][2], image[name]))
    hosts = params_like(cur, 'perodua_client_stable.hosts').get('perodua_client_stable.hosts')
    if hosts:
        try:
            workspaces = json.loads(hosts)['workspaces']
            codes = {code for listed in workspaces.values() for code in listed}
        except (ValueError, KeyError, TypeError, AttributeError):
            problems.append('perodua_client_stable.hosts is not a host table')
        else:
            unknown = sorted(str(code) for code in codes if code not in HOST_CODES)
            if unknown:
                problems.append('perodua_client_stable.hosts names the workspace codes %s; this release knows only %s'
                                % (', '.join(unknown), ', '.join(HOST_CODES)))
    seed = params_like(cur, 'perodua_demo.seed_mode', 'perodua_demo.seeded', '%.sample_data')
    samples = {key: value for key, value in seed.items() if key.endswith('.sample_data')}
    mode_value = seed.get('perodua_demo.seed_mode')
    if mode_value is None and not samples:
        problems.append('neither perodua_demo.seed_mode nor a <module>.sample_data parameter is set, so nothing keeps the sample data of the new release out')
    if mode_value is not None and mode_value != 'none':
        problems.append('perodua_demo.seed_mode is %s, not none' % mode_value)
    loaded = sorted(key for key, value in samples.items() if value != 'none')
    if loaded:
        problems.append('sample data is on for: ' + ', '.join(loaded))
    if seed.get('perodua_demo.seeded'):
        problems.append('perodua_demo.seeded is set: a demonstration database')
    for module, name, table, code in REFERENCE_RECORDS:
        if not table_exists(cur, table):
            continue
        cur.execute('SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s', (module, name))
        row = cur.fetchone()
        if row is None:
            continue
        cur.execute('SELECT count(*) FROM "%s" WHERE id = %%s' % table, (row[0],))
        if cur.fetchone()[0]:
            continue
        cur.execute('SELECT id FROM "%s" WHERE code = %%s ORDER BY id' % table, (code,))
        other = cur.fetchone()
        if other:
            problems.append('%s.%s names a deleted record, and record %s of %s has its code %s: the upgrade would create it again and stop on the unique code'
                            % (module, name, other[0], table, code))
    return problems
def refuse(problems, code=1):
    for problem in problems:
        print('Database preflight: module upgrade refused: ' + problem, file=sys.stderr)
    sys.exit(code)
def retired_export(cur):
    # Each retired table, the rows of each retired column with a value, and the
    # retired attachments (their files are in filestore.tar.gz), as CSV.
    buffer = io.BytesIO()
    def add(archive, name, header, rows):
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(header)
        writer.writerows(rows)
        data = text.getvalue().encode()
        info = tarfile.TarInfo('retired-data/' + name)
        info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
        archive.addfile(info, io.BytesIO(data))
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for table in RETIRED_TABLES:
            if table_exists(cur, table):
                cur.execute('SELECT * FROM "%s" ORDER BY id' % table)
                add(archive, 'tables/' + table + '.csv', [d[0] for d in cur.description], cur.fetchall())
        for table, column, condition in RETIRED_COLUMNS:
            if column_exists(cur, table, column):
                cur.execute('SELECT id, "%s" FROM "%s" WHERE %s ORDER BY id' % (column, table, condition))
                add(archive, 'columns/' + table + '.' + column + '.csv', ['id', column], cur.fetchall())
        where, values = in_list('res_model', RETIRED_MODELS)
        cur.execute('SELECT id, name, res_model, res_field, res_id, store_fname, checksum, mimetype, file_size FROM ir_attachment WHERE '
                    + where + ' ORDER BY id', values)
        add(archive, 'attachments.csv', [d[0] for d in cur.description], cur.fetchall())
    sys.stdout.buffer.write(buffer.getvalue())
def mark_upgrading(cn, cur, installed, fingerprint):
    # Before -u: the cron flags, the last mail and the modules, then the mark.
    old_fingerprint, old_release, new_release = sys.argv[2:5]
    cur.execute('SELECT id, active FROM ir_cron ORDER BY id')
    crons = {str(cron): bool(active) for cron, active in cur.fetchall()}
    mail = 0
    if table_exists(cur, 'mail_mail'):
        cur.execute('SELECT max(id) FROM mail_mail')
        mail = cur.fetchone()[0] or 0
    cur.execute("SELECT count(*) FROM res_users WHERE login = 'agent'")
    record = {'from_fingerprint': old_fingerprint, 'to_fingerprint': fingerprint, 'from_release': old_release,
              'to_release': new_release, 'marked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'modules': upgrade_modules(installed, image_versions()), 'crons': crons, 'mail_max_id': mail,
              'agent_login': cur.fetchone()[0] > 0, 'held_mail': [], 'released_mail': []}
    put(cur, KIT_PARAM, json.dumps(record, sort_keys=True))
    put(cur, 'perodua.image_modhash', UPGRADING + old_fingerprint)
    cn.commit()
    print('MARKED')
    sys.exit(0)
def verify_upgrade(cn, cur, modules, stored, fingerprint):
    record = kit_record(cur)
    if not record or stored.get('perodua.image_modhash') != UPGRADING + record.get('from_fingerprint', ''):
        fail('the mark of this module upgrade is missing or damaged')
    if record['to_fingerprint'] != fingerprint:
        fail('the module upgrade was marked for other modules than this release has')
    # First what keeps the running App safe, whatever the checks below find.
    # The cron flags as they were before -u. A cron the upgrade created keeps
    # its flag. A mock intake pull never goes on while its system is mock.
    params = params_like(cur, 'perodua_integration.mode%')
    cur.execute("SELECT res_id, module, name FROM ir_model_data WHERE model = 'ir.cron'")
    xmlids = {res_id: module + '.' + name for res_id, module, name in cur.fetchall()}
    cur.execute('SELECT id, active FROM ir_cron ORDER BY id')
    restored, kept_off, new = 0, [], []
    for cron, active in cur.fetchall():
        before = record['crons'].get(str(cron))
        target = bool(active) if before is None else before
        system = MOCK_INTAKE_CRONS.get(xmlids.get(cron))
        if system and system_mode(params, system) == 'mock' and target:
            target = False
            kept_off.append(xmlids[cron])
        if before is None:
            new.append('%s (%s)' % (xmlids.get(cron, 'id %d' % cron), 'on' if target else 'off'))
        if target != bool(active):
            cur.execute('UPDATE ir_cron SET active = %s WHERE id = %s', (target, cron))
            restored += target == before
    # Every mail of the queue (state outgoing) waits for the owner's review:
    # the mail the run queued, and the mail that waited before it, which the
    # run may have changed (a migration gives an older release mail its
    # address). A row in any other state stays as it is, so a second run finds
    # the held rows as they are and counts each of them once.
    held = []
    if table_exists(cur, 'mail_mail'):
        cur.execute("SELECT id FROM mail_mail WHERE state = 'outgoing' ORDER BY id")
        held = [row[0] for row in cur.fetchall()]
        if held:
            where, values = in_list('id', held)
            cur.execute("UPDATE mail_mail SET state = 'exception', failure_reason = %s WHERE state = 'outgoing' AND " + where,
                        (HOLD_REASON,) + values)
    record['held_mail'] = sorted(set(record['held_mail']) | set(held))
    held_queued, held_before = held_counts(record)
    put(cur, KIT_PARAM, json.dumps(record, sort_keys=True))
    cn.commit()
    print('Cron jobs: %d flags put back as before the upgrade.' % restored)
    for xmlid in kept_off:
        print('Cron jobs: %s stays off: it reads the mock feed while its system resolves to mock.' % xmlid)
    if new:
        print('Cron jobs new with this release: ' + ', '.join(new) + '.')
    print('Mail held (state exception) until deploy-app.sh --release-queued-mail: %d that the upgrade queued,'
          ' %d that waited in the queue before it.' % (held_queued, held_before))
    # Then the result itself (plan section 3, go/no-go).
    installed = {name for name, (state, _, _) in modules.items() if state == 'installed'}
    problems = ['%s is %s' % (name, state) for name, (state, _, _) in sorted(modules.items())
                if state in ('to install', 'to upgrade', 'to remove')]
    problems += [name + ' is still installed' for name in RETIRED_MODULES + NEVER_INSTALLED if name in installed]
    image = image_versions()
    for name in sorted(installed):
        if name.startswith('perodua_') and image.get(name) != modules[name][2]:
            problems.append('%s is at version %s, the release at %s' % (name, modules[name][2], image.get(name, 'none (not in the release)')))
    problems += [name + ' is no longer installed' for name in record['modules'] if name not in installed]
    seed = params_like(cur, 'perodua_demo.seeded', 'perodua.uat_init', 'perodua.runtime_profile')
    if seed.get('perodua_demo.seeded'):
        problems.append('perodua_demo.seeded is set: sample data was loaded')
    if seed.get('perodua.uat_init') != 'complete' or seed.get('perodua.runtime_profile') != PROFILE:
        problems.append('the release stamps perodua.uat_init and perodua.runtime_profile changed')
    cur.execute("SELECT count(*) FROM res_users WHERE login = 'agent'")
    if cur.fetchone()[0] and not record['agent_login']:
        problems.append('the sample user agent was created')
    if problems:
        for problem in problems:
            print('Database preflight: the upgraded database is not right: ' + problem, file=sys.stderr)
        sys.exit(1)
    put(cur, 'perodua.image_modhash', UPGRADED + fingerprint)
    cn.commit()
    print('UPGRADE_VERIFIED %d %d' % (held_queued, held_before))
    sys.exit(0)
def release_mail(cn, cur):
    # Only the rows verify-upgrade held and that are still held: a row the
    # owner deleted, cancelled or sent again since stays as it is. Two
    # updates, so that each number is the count of the rows it changed.
    record = kit_record(cur)
    held = (record or {}).get('held_mail') or []
    released = [0, 0]
    if held:
        where, values = in_list('id', held)
        for index, side in enumerate(('id > %s', 'id <= %s')):
            cur.execute("UPDATE mail_mail SET state = 'outgoing', failure_reason = NULL WHERE state = 'exception' AND failure_reason = %s AND "
                        + where + ' AND ' + side, (HOLD_REASON,) + values + (record['mail_max_id'],))
            released[index] = cur.rowcount
        record['released_mail'] = sorted(set(record.get('released_mail', [])) | set(held))
        record['held_mail'] = []
        put(cur, KIT_PARAM, json.dumps(record, sort_keys=True))
        cn.commit()
    print('RELEASED %d %d' % tuple(released))
    sys.exit(0)
with connect(db) as cn:
    with cn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S')")
        if not cur.fetchone()[0]:
            if mode not in ('check', 'upgrade-check'): fail('database is still empty; cannot ' + mode)
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
        modhash = stored.get('perodua.image_modhash') or ''
        if modhash.startswith(UPGRADING):
            # A module upgrade marked this database: whether -u ran, ran part
            # of the way or ran fully, only verify-upgrade may continue.
            if mode == 'verify-upgrade':
                verify_upgrade(cn, cur, modules, stored, fingerprint)
            fail('database is marked by a module upgrade that did not finish (perodua.image_modhash ' + modhash + '): no release can run on it.'
                 ' Restore the backup that deploy-app.sh --upgrade made before it: restore.txt in its backup folder')
        if mode == 'verify-upgrade':
            fail('verify-upgrade applies only to a database that a module upgrade marked')
        if any(state in ('to install', 'to upgrade', 'to remove') for state, _, _ in modules.values()):
            # Odoo commits each module as it installs it, so a killed -i
            # (timeout, Ctrl-C, lost session) leaves the rest 'to install'.
            if 'perodua.runtime_profile' not in stored:
                fail('database contains module changes left by an interrupted installation. If an earlier --init-db stopped part-way, recreate the empty database on the DB server; see docs/DEPLOYMENT.md')
            fail('database contains pending module changes; finish them separately')
        if mode == 'public-urls':
            # PDF reports load their styles from this Odoo itself, not through
            # the front proxy. A frozen base URL is not replaced by the address
            # an administrator signs in through. Written before the containers
            # are recreated, so Odoo starts with these values.
            base_url = sys.argv[2] if len(sys.argv) > 2 else ''
            label = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
            url = re.fullmatch(r'https?://(' + label + r'(?:\.' + label + r')*)(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9-]{0,30})?', base_url)
            if base_url and not (url and len(url.group(1)) <= 253):
                fail('invalid public base URL')
            put(cur, 'report.url', 'http://127.0.0.1:8069')
            if base_url:
                put(cur, 'web.base.url', base_url)
                put(cur, 'web.base.url.freeze', 'True')
            cn.commit()
            print('PUBLIC_URLS_SET')
            sys.exit(0)
        if mode == 'retired-export':
            retired_export(cur)
            sys.exit(0)
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
        checking = mode in ('check', 'upgrade-check')
        if uat is None and 'perodua.runtime_profile' not in stored:
            # Initialized but neither marked nor stamped: an --init-db that
            # stopped between installing the modules and recording that, or
            # not a release database at all. Only the former may be finished.
            if not checking: fail(mode + ' applies only to a fresh initialization that is awaiting its UAT setup')
            problem = fresh_problem()
            if problem:
                fail('database is initialized but has no release stamp and is not a complete fresh UAT initialization (' + problem + '). If an earlier --init-db failed part-way, recreate the empty database on the DB server; see docs/DEPLOYMENT.md')
            print('SETUP_UNMARKED')
            sys.exit(0)
        if uat == 'pending':
            fresh_checks()
            if checking:
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
        elif not checking and mode not in ('mark-upgrading', 'stamp-upgrade', 'release-mail'):
            fail(mode + ' applies only to a fresh initialization that is awaiting its UAT setup')
        required = init_modules if uat == 'complete' else restored_modules
        missing = sorted(set(required) - installed)
        if missing: fail('required modules are not installed: ' + ', '.join(missing))
        if uat == 'complete' and (excluded in installed or excluded_records()):
            fail(excluded + ' was installed into this UAT database after its initialization')
        if stored.get('perodua.runtime_profile') != PROFILE:
            fail('runtime profile does not match client-stable-uiux; restore the correct database')
        cur.execute("SELECT count(*) FROM ir_attachment WHERE store_fname IS NOT NULL")
        attachments = cur.fetchone()[0]
        modhash = stored.get('perodua.image_modhash') or ''  # a fresh stamp above has just set it
        upgrade_from = sys.argv[2] if mode in ('upgrade-check', 'mark-upgrading') and len(sys.argv) > 2 else ''
        if modhash != fingerprint and upgrade_from and modhash == upgrade_from:
            # The modules of the release the module upgrade starts from. The
            # mark checks again what upgrade-check checked.
            problems = module_upgrade_problems(cur, modules, installed, uat, excluded_records())
            if problems:
                # mark-upgrading: exit 3 says that nothing was written, so
                # deploy-app.sh starts the old App again. Every other failure
                # exits 1, which may come after the commit of the mark.
                refuse(problems, 3 if mode == 'mark-upgrading' else 1)
            if mode == 'mark-upgrading':
                mark_upgrading(cn, cur, installed, fingerprint)
            for kind, name, count in retired_counts(cur):
                print('RETIRED %s %s %d' % (kind, name, count))
            print('MODULES ' + ','.join(upgrade_modules(installed, image_versions())))
            print('MODULE_UPGRADE ' + str(attachments))
            sys.exit(0)
        if modhash == UPGRADED + fingerprint:
            # Upgraded and verified; the deployment of this release finishes it.
            if mode == 'stamp-upgrade':
                put(cur, 'perodua.image_modhash', fingerprint)
                cn.commit()
                modhash = fingerprint
            elif mode == 'upgrade-check' or (mode == 'check' and '--accept-pending' in sys.argv[2:]):
                print('UPGRADE_PENDING ' + str(attachments))
                sys.exit(0)
            else:
                fail('the module upgrade of this database to this release is checked but not finished: run deploy-app.sh --dir DIR from the'
                     ' scripts folder of the new release, without --upgrade (it runs no module upgrade again)')
        elif mode in ('mark-upgrading', 'stamp-upgrade'):
            fail(mode + ' does not apply to this database (perodua.image_modhash ' + modhash + ')')
        if modhash != fingerprint:
            if mode == 'upgrade-check':
                fail('database module fingerprint does not match this release, and is not the fingerprint this release upgrades modules from')
            fail('database module fingerprint does not match this release; upgrades require a separate plan')
        if mode == 'release-mail':
            release_mail(cn, cur)
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
      PUBLIC_ROOT: "$PUBLIC_ROOT"
      ENVIRONMENT_LABEL: "$ENVIRONMENT_LABEL"
    depends_on:
      odoo:
        condition: service_healthy
    ports:
      - "$BIND_IP:$HTTP_PORT:80"
    healthcheck:
      # Host: web selects the web container's internal server (v1.0.4), which
      # serves /app/ at the root path whatever PUBLIC_ROOT is. Earlier web
      # images answer every host name the same way.
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "--header", "Host: web", "http://127.0.0.1/app/version.json"]
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
# --upgrade: up to here nothing in the directory has changed. The new release
# checks the database and saves it from a staged copy of the deployment in
# TEMP_DIR: the same project, so the same attachments volume and network. The
# directory's files and containers stay those of the old release until the
# switch below.
staged() { docker compose --project-name "$PROJECT_NAME" --file "$TEMP_DIR/compose.yml" "$@"; }
current() { docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" "$@"; }
upgrade_check_database() {
    local state report
    install -d -m 0700 "$TEMP_DIR/secrets"
    cp -p -- "$TEMP_DIR/db_password" "$TEMP_DIR/secrets/db_password"
    staged config --quiet
    printf 'Checking database %s for %s before anything is changed...\n' "$DB_NAME" "$RELEASE"
    if ! report=$(staged run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py upgrade-check "$MODULE_UPGRADE_FROM" \
            </dev/null 2> "$TEMP_DIR/check.log"); then
        cat "$TEMP_DIR/check.log" >&2
        if grep -q 'module fingerprint does not match' "$TEMP_DIR/check.log"; then
            fail "$RELEASE has other Odoo modules than the release that set up database $DB_NAME. It upgrades modules (-u) only from the modules of v1.0.3 to v1.0.8 (fingerprint $MODULE_UPGRADE_FROM)"
        fi
        if grep -q 'marked by a module upgrade that did not finish' "$TEMP_DIR/check.log"; then
            DB_MARKED=1
            fail "An earlier module upgrade of database $DB_NAME stopped part-way. Restore its backup first (above)"
        fi
        if grep -q 'module upgrade refused' "$TEMP_DIR/check.log"; then
            fail "$RELEASE has other Odoo modules than database $DB_NAME, and the module upgrade (-u) is refused for the reasons above"
        fi
        fail 'The database check above failed'
    fi
    state=${report##*$'\n'}
    case $state in
        READY\ *)
            printf 'Database %s fits %s: the same Odoo modules, %s attachments.\n' "$DB_NAME" "$RELEASE" "${state#READY }"
            return ;;
        MODULE_UPGRADE\ *|UPGRADE_PENDING\ *) ;;
        *) fail "--upgrade needs an initialized database, and the check of $DB_NAME reports $state" ;;
    esac
    [[ ,$OLD_RELEASES_ACCEPTED, == *,"$OLD_RELEASE",* ]] \
        || fail "$RELEASE upgrades the Odoo modules only of a deployment of ${OLD_RELEASES_ACCEPTED//,/, }, and $DEPLOY_DIR runs $OLD_RELEASE. Upgrade it to one of those first"
    MODULE_UPGRADE=1
    if [[ $state == UPGRADE_PENDING\ * ]]; then
        # An earlier --upgrade upgraded and checked the modules, then stopped
        # before its switch. Its backup holds the data from before.
        PENDING_ONLY=1
        printf 'An earlier --upgrade upgraded the Odoo modules of database %s to %s and checked them, then stopped before its switch. This run makes the switch and deploys %s, without a new backup or module upgrade. The backup of that run (in %s/backups, or its --backup-dir) holds the data from before.\n' \
            "$DB_NAME" "$RELEASE" "$RELEASE" "$DEPLOY_DIR"
        return
    fi
    UPGRADE_MODULES=$(sed -n 's/^MODULES //p' <<< "$report")
    [[ $UPGRADE_MODULES =~ ^perodua_[a-z0-9_]+(,perodua_[a-z0-9_]+)*$ ]] || fail "Unexpected module list from the database check: $UPGRADE_MODULES"
    retired_counts "$report"
    printf 'Database %s has the modules of %s (fingerprint %s). %s has other modules: this upgrade runs a module upgrade (-u) of %s modules. %s attachments.\n' \
        "$DB_NAME" "$OLD_RELEASE" "$MODULE_UPGRADE_FROM" "$RELEASE" "$(tr ',' '\n' <<< "$UPGRADE_MODULES" | wc -l | tr -d ' ')" "${state#MODULE_UPGRADE }"
    if ((RETIRED_TOTAL == 0)); then
        printf 'Retired data: none. The tables, columns and attachments of the retired modules hold nothing that the upgrade deletes.\n'
        return
    fi
    printf 'Retired data that the module upgrade deletes (the modules that owned it are gone from %s):\n' "$RELEASE"
    printf '%s\n' "${RETIRED_LINES[@]}"
    ((DROP_RETIRED)) \
        || fail "The module upgrade deletes the retired data above. Once its owner agreed, run --upgrade again with --drop-retired-data: the rows are saved as CSV in the backup folder (retired-data.tar.gz) before the upgrade"
    printf 'With --drop-retired-data: the rows above are saved as CSV in the backup folder, then deleted by the upgrade.\n'
}
retired_counts() {  # the RETIRED lines of an upgrade-check report: their total, and the lines that are not 0
    local kind name count
    RETIRED_TOTAL=0 RETIRED_LINES=()
    while read -r kind name count; do
        [[ $count =~ ^[0-9]+$ ]] || fail "Unexpected retired-data count from the database check: $kind $name $count"
        ((count == 0)) || RETIRED_LINES+=("$(printf '  %-11s %s: %s' "$kind" "$name" "$count")")
        RETIRED_TOTAL=$((RETIRED_TOTAL + count))
    done < <(sed -n 's/^RETIRED //p' <<< "$1")
}
upgrade_recheck_database() {
    # The first check ran while the old App ran, and users could write until
    # the App stopped for the backup. Now nothing writes: check again, so that
    # no retired row is deleted without --drop-retired-data and its CSV copy.
    # A refusal here starts the old App again (state backed-up).
    local report state modules
    printf 'Checking database %s again, now that the App is stopped...\n' "$DB_NAME"
    if ! report=$(staged run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py upgrade-check "$MODULE_UPGRADE_FROM" \
            </dev/null 2> "$TEMP_DIR/check.log"); then
        cat "$TEMP_DIR/check.log" >&2
        if grep -q 'module upgrade refused' "$TEMP_DIR/check.log"; then
            fail "Database $DB_NAME changed while the App ran: the module upgrade (-u) is now refused for the reasons above"
        fi
        fail 'The second database check failed (above)'
    fi
    state=${report##*$'\n'}
    [[ $state == MODULE_UPGRADE\ * ]] || fail "The second check of database $DB_NAME reports $state, not the module upgrade of the first check"
    modules=$(sed -n 's/^MODULES //p' <<< "$report")
    [[ $modules == "$UPGRADE_MODULES" ]] \
        || fail "The modules to upgrade changed while the App ran: $UPGRADE_MODULES at the first check, $modules now. Run --upgrade again"
    retired_counts "$report"
    if ((RETIRED_TOTAL == 0)); then
        printf 'Database %s: the same modules to upgrade, and still no retired data.\n' "$DB_NAME"
        return
    fi
    printf 'Retired data that the module upgrade deletes, counted with the App stopped:\n'
    printf '%s\n' "${RETIRED_LINES[@]}"
    ((DROP_RETIRED)) \
        || fail "Users wrote data of the retired modules after the first check, while the App ran (above). The module upgrade deletes it. Once its owner agreed, run --upgrade again with --drop-retired-data: the rows are saved as CSV in the backup folder (retired-data.tar.gz) before the upgrade"
}
module_upgrade_guard() {  # before anything changes: the modules -u touches never need perodua_demo_client
    printf 'Checking that the module upgrade keeps %s out of database %s...\n' "$EXCLUDED_MODULE" "$DB_NAME"
    if ! staged run --rm --no-deps -T odoo python3 /opt/deploy/uat_guard.py guard --modules "$UPGRADE_MODULES" \
            --exclude "$EXCLUDED_MODULE" --odoo-config /etc/odoo/odoo.conf --json \
            </dev/null > "$TEMP_DIR/uat-guard.json" 2> "$TEMP_DIR/uat-guard.log"; then
        cat "$TEMP_DIR/uat-guard.log" >&2
        fail "Refused before any change: the modules of $RELEASE cannot keep $EXCLUDED_MODULE out of database $DB_NAME safely (above)"
    fi
    cat "$TEMP_DIR/uat-guard.log"
    DEMO_FLAG=$(staged run --rm --no-deps -T odoo python3 /opt/deploy/uat_guard.py demo-flag --odoo-config /etc/odoo/odoo.conf \
        </dev/null 2>> "$TEMP_DIR/uat-guard.log") \
        || fail "Cannot determine how the Odoo of $RELEASE disables demo data (above)"
    [[ $DEMO_FLAG =~ ^--[a-z-]+=[A-Za-z0-9]+$ ]] || fail 'Unexpected demo-data option reported by the image'
}
module_upgrade_confirm() {
    local answer
    cat <<PLAN
The module upgrade of database $DB_NAME on $DB_HOST, from $OLD_RELEASE to $RELEASE:
  1. Stop the App, then save the database and the attachments (as every --upgrade does).
     Check the database again, now that nothing writes to it.
  2. Mark the database for the upgrade and remove the containers of $OLD_RELEASE.
     From then on $OLD_RELEASE cannot run on this database, and the only way
     back is to restore the backup (restore.txt).
  3. Upgrade the modules once (-u, no -i, cron jobs off). Then hold every mail
     of the mail queue: the mail the upgrade queues, and the mail that waits
     in the queue before it. Release it after review with --release-queued-mail.
  4. Check the result, put the cron flags back, deploy $RELEASE.
Point of no return: once users write data with $RELEASE, going back to the
backup loses that data. After that point, fix forward.
PLAN
    if [[ -n $CONFIRM ]]; then
        [[ $CONFIRM == "$DB_NAME" ]] || fail "--confirm must be exactly $DB_NAME, the database of $DEPLOY_DIR"
        return
    fi
    if ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        fail "A module upgrade needs a confirmation: run on a terminal, or pass --confirm $DB_NAME"
    fi
    read -r -p "Type $DB_NAME to start the module upgrade: " answer || fail 'Input cancelled'
    [[ $answer == "$DB_NAME" ]] || fail 'The confirmation did not match'
}
export_retired_data() {  # the owner agreed (--drop-retired-data): keep a copy next to the backup
    printf 'Saving the retired data to %s/retired-data.tar.gz...\n' "$BACKUP_DIR"
    staged --file "$TEMP_DIR/no-log.yml" run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py retired-export \
        </dev/null > "$BACKUP_DIR/retired-data.tar.gz" || fail 'The export of the retired data failed (above)'
    tar -tzf "$BACKUP_DIR/retired-data.tar.gz" > "$TEMP_DIR/retired.list" \
        || fail "The export $BACKUP_DIR/retired-data.tar.gz cannot be read back"
    printf 'Saved: %s\n' "$(paste -sd ' ' - < "$TEMP_DIR/retired.list")"
}
module_upgrade() {  # after the backup: the mark, -u, and the check of its result
    local state status=0
    # The containers of the old release are stopped. From the mark on they
    # must never start again, so they go right after it. mark-upgrading
    # checks the database once more and refuses with exit 3 before it writes
    # anything: then the old App starts again (backed-up). Any other failure
    # may come after the mark was written, so it counts as marked.
    UPGRADE_STATE=marked
    state=$(staged run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py mark-upgrading \
        "$MODULE_UPGRADE_FROM" "$OLD_RELEASE" "$RELEASE" </dev/null) || status=$?
    if ((status == 3)); then
        UPGRADE_STATE=backed-up
        fail 'The mark of the module upgrade was refused for the reasons above. Nothing was written to the database'
    fi
    printf 'Removing the containers of %s (its compose.yml and images stay): from here on it does not start again on this database.\n' "$OLD_RELEASE"
    current rm --stop --force odoo web
    OLD_REMOVED=1
    ((status == 0)) || fail 'Could not mark the database for the module upgrade (above)'
    [[ $state == MARKED ]] || fail "Unexpected answer to the mark of the module upgrade: $state"
    UPGRADE_CONTAINER=$PROJECT_NAME-module-upgrade
    docker rm -f "$UPGRADE_CONTAINER" >/dev/null 2>&1 || true
    UPGRADE_STATE=migrating
    printf 'Upgrading the Odoo modules of %s (-u, at most INIT_TIMEOUT=%s seconds). Log: %s/module-upgrade.log (follow with tail -f in another terminal).\n' \
        "$DB_NAME" "$INIT_TIMEOUT" "$BACKUP_DIR"
    # One run of Odoo, through bash -c so that the image's entrypoint runs it
    # as it is (no init flow) and the password stays off the command line. In
    # the background, so that a lost session stops it at once (cleanup).
    # shellcheck disable=SC2016
    timeout "$INIT_TIMEOUT" docker compose --project-name "$PROJECT_NAME" --file "$TEMP_DIR/compose.yml" \
        run --rm --no-deps -T --name "$UPGRADE_CONTAINER" \
        -e PERODUA_UPGRADE_MODULES="$UPGRADE_MODULES" -e PERODUA_DEMO_FLAG="$DEMO_FLAG" odoo bash -c \
        'PGPASSWORD="$DB_PASSWORD" exec odoo -c /etc/odoo/odoo.conf -d "$DB_NAME" --db_host="$DB_HOST" --db_port="$DB_PORT" --db_user="$DB_USER" -u "$PERODUA_UPGRADE_MODULES" "$PERODUA_DEMO_FLAG" --max-cron-threads=0 --stop-after-init --log-handler=odoo.modules.migration:INFO' \
        </dev/null > "$BACKUP_DIR/module-upgrade.log" 2>&1 &
    UPGRADE_PID=$!
    wait "$UPGRADE_PID" || status=$?
    UPGRADE_PID=''
    ((status != 124)) || fail "The module upgrade did not finish within INIT_TIMEOUT=$INIT_TIMEOUT seconds; see $BACKUP_DIR/module-upgrade.log"
    ((status == 0)) || fail "The module upgrade (-u) failed; see $BACKUP_DIR/module-upgrade.log"
    UPGRADE_STATE=migrated
    # From the stop for the backup until the deployment below starts this
    # release, the only Odoo process on the database is the run of -u above,
    # which has no cron threads and stops after the upgrade; the containers
    # of the old release are removed. So no mail queue runs before
    # verify-upgrade holds the queue.
    printf 'Module upgrade finished. Checking the result, putting the cron flags back and holding every mail of the mail queue...\n'
    if ! state=$(staged run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py verify-upgrade </dev/null 2> "$TEMP_DIR/verify.log"); then
        cat "$TEMP_DIR/verify.log" >&2
        fail 'The upgraded database failed its checks (above)'
    fi
    [[ ${state##*$'\n'} =~ ^UPGRADE_VERIFIED\ ([0-9]+)\ ([0-9]+)$ ]] || fail "Unexpected answer from the check of the upgraded database: $state"
    HELD_MAIL=${BASH_REMATCH[1]} HELD_MAIL_BEFORE=${BASH_REMATCH[2]}
    [[ $state != *$'\n'* ]] || printf '%s\n' "${state%$'\n'*}"
    printf 'The upgraded database passed its checks.\n'
}
upgrade_space() {  # the backup must fit, before the App stops
    local sizes database attachments base free
    # pg_database_size (indexes included, nothing compressed) and the
    # attachments as stored: more than the dump and the archive will take.
    # shellcheck disable=SC2016
    sizes=$(staged run --rm --no-deps -T odoo bash -c \
        'PGPASSWORD="$DB_PASSWORD" psql -X -A -t -q --no-password --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$DB_NAME" -c "SELECT pg_database_size(current_database()) / 1024" && if [ -d "/var/lib/odoo/filestore/$DB_NAME" ]; then du -sk "/var/lib/odoo/filestore/$DB_NAME" | cut -f1; else echo 0; fi' \
        </dev/null) || fail "Cannot read the size of database $DB_NAME and of its attachments (above)"
    [[ $sizes =~ ^([0-9]+)$'\n'([0-9]+)$ ]] || fail "Cannot read the size of database $DB_NAME and of its attachments: $sizes"
    database=${BASH_REMATCH[1]} attachments=${BASH_REMATCH[2]}
    base=${BACKUP_BASE:-$DEPLOY_DIR/backups}
    while [[ ! -d $base ]]; do base=${base%/*}; base=${base:-/}; done
    free=$(df -Pk -- "$base" | awk 'NR == 2 { print $4 }')
    [[ $free =~ ^[0-9]+$ ]] || fail "Cannot read the free space of $base"
    ((free >= database + attachments)) || fail "Not enough room for the backup on the disk of $base: $((free / 1024)) MB free, and the backup may need up to $(((database + attachments) / 1024)) MB (database $DB_NAME $((database / 1024)) MB, attachments $((attachments / 1024)) MB; the dump is usually smaller than the database). Free space there, or give --backup-dir on a disk with more room"
}
upgrade_backup() {
    local folder running bytes files
    upgrade_space
    # pg_dump and tar send the backup to standard output, which Docker's default
    # log (json-file) would also keep, at about four times its size, until the
    # container is removed. These two runs have no container log.
    printf 'services:\n  odoo:\n    logging:\n      driver: none\n' > "$TEMP_DIR/no-log.yml"
    # The App is stopped first, so that the database and the attachments are
    # saved as they are when the switch starts.
    running=$(current ps --quiet --status running) || fail 'Cannot read the state of the App containers'
    [[ -z $running ]] || WAS_RUNNING=1
    folder=${BACKUP_BASE:-$DEPLOY_DIR/backups}/$(date -u +%Y%m%dT%H%M%SZ)-$OLD_RELEASE
    if ! { mkdir -p -- "${folder%/*}" && mkdir -- "$folder"; }; then fail "Cannot create the backup folder $folder"; fi
    BACKUP_DIR=$folder
    UPGRADE_STATE=stopped
    printf 'Stopping the App of %s for the backup...\n' "$OLD_RELEASE"
    current stop web odoo
    printf 'Saving database %s to %s/database.dump...\n' "$DB_NAME" "$BACKUP_DIR"
    # pg_dump of the image (PostgreSQL 18) reaches the DB server as the App
    # does. The password comes from the container's own environment, never a
    # command line.
    # shellcheck disable=SC2016
    staged --file "$TEMP_DIR/no-log.yml" run --rm --no-deps -T odoo bash -c \
        'PGPASSWORD="$DB_PASSWORD" exec pg_dump --format=custom --no-password --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --dbname="$DB_NAME"' \
        </dev/null > "$BACKUP_DIR/database.dump" || fail "pg_dump of database $DB_NAME failed (above)"
    if ! { staged run --rm --no-deps -T odoo pg_restore --list < "$BACKUP_DIR/database.dump" > "$TEMP_DIR/database.list" \
            && grep -Eq ' TABLE DATA public ir_module_module( |$)' "$TEMP_DIR/database.list"; }; then
        fail "The backup $BACKUP_DIR/database.dump cannot be read back, or holds no Odoo database"
    fi
    printf 'Saving the attachments of %s to %s/filestore.tar.gz...\n' "$DB_NAME" "$BACKUP_DIR"
    # The layout README.md restores ("From a backup"): filestore/DB_NAME/...
    # shellcheck disable=SC2016
    staged --file "$TEMP_DIR/no-log.yml" run --rm --no-deps -T odoo bash -c \
        'cd /var/lib/odoo && if [ -d "filestore/$DB_NAME" ]; then exec tar -czf - "filestore/$DB_NAME"; fi; exec tar -czf - --files-from=/dev/null' \
        </dev/null > "$BACKUP_DIR/filestore.tar.gz" || fail "The archive of the attachments volume ${PROJECT_NAME}_filestore failed (above)"
    files=$(tar -tzf "$BACKUP_DIR/filestore.tar.gz" | awk '!/\/$/ { n++ } END { print n + 0 }') \
        || fail "The backup $BACKUP_DIR/filestore.tar.gz cannot be read back"
    cp -p -- "$DEPLOY_DIR/.deployment-identity" "$BACKUP_DIR/deployment-identity"
    [[ ! -f $DEPLOY_DIR/app.env ]] || cp -p -- "$DEPLOY_DIR/app.env" "$BACKUP_DIR/app.env"
    restore_hint > "$BACKUP_DIR/restore.txt"
    UPGRADE_STATE=backed-up
    bytes=$(wc -c < "$BACKUP_DIR/database.dump")
    printf 'Backup complete in %s: database.dump (%s bytes), filestore.tar.gz (%s files).\n' "$BACKUP_DIR" "$bytes" "$files"
    cat "$BACKUP_DIR/restore.txt"
}
restore_hint() {  # how to put the backup back, saved next to it as restore.txt
    local compose dump archive
    compose=$(printf 'sudo docker compose --project-name %q --file %q' "$PROJECT_NAME" "$DEPLOY_DIR/compose.yml")
    dump=$(printf '%q' "$BACKUP_DIR/database.dump")
    archive=$(printf '%q' "$BACKUP_DIR/filestore.tar.gz:/restore.tar.gz:ro")
    cat <<HINT
Backup of $DEPLOY_DIR before the upgrade from $OLD_RELEASE to $RELEASE:
  database.dump        database $DB_NAME on $DB_HOST:$DB_PORT (pg_dump -Fc, PostgreSQL 18)
  filestore.tar.gz     its attachments, filestore/$DB_NAME/..., from the volume ${PROJECT_NAME}_filestore
  deployment-identity  the directory's identity before the upgrade; app.env its settings

To put this data back, on this App server. Restore database.dump with the
pg_restore and psql of the Odoo image as below: the pg_restore 16 of the DB
server cannot read it, and the SQL of pg_restore 18 starts with the one line
PostgreSQL 16 does not know (SET transaction_timeout), which step 3 leaves out.
1. Stop the App:
   sudo bash service.sh --role app --dir $(printf '%q' "$DEPLOY_DIR") stop
2. Empty the database, from a scripts folder (reset_database.py is there):
   $compose run --rm --no-deps -T odoo python3 - < reset_database.py
3. Restore the database, in one transaction:
   $compose run --rm --no-deps -T odoo bash -c 'set -o pipefail; pg_restore --no-owner --no-acl --file=- | sed "0,/^SET transaction_timeout = 0;/{//d}" | PGPASSWORD="\$DB_PASSWORD" psql -X -q --output=/dev/null --no-password -v ON_ERROR_STOP=1 --single-transaction --host="\$DB_HOST" --port="\$DB_PORT" --username="\$DB_USER" --dbname="\$DB_NAME"' < $dump
4. Put the attachments back, and end every sign-in (the sessions):
   $compose run --rm --no-deps --user 0 --entrypoint bash -v $archive odoo -ec 'rm -rf "/var/lib/odoo/filestore/\$DB_NAME" /var/lib/odoo/sessions; tar --no-same-owner -xzf /restore.tar.gz -C /var/lib/odoo; mkdir -p "/var/lib/odoo/filestore/\$DB_NAME"; chown -R odoo:odoo /var/lib/odoo/filestore'
   Every user must sign in again. The database is as it was at the backup:
   passwords changed and sign-outs made since then are undone, so a user who
   changed a password for safety after the backup must change it again.
5. Deploy $OLD_RELEASE again, with the identity and settings the directory had
   before the upgrade (a scripts folder of $OLD_RELEASE needs no --upgrade):
$(go_back_steps)
HINT
    # A module upgrade: check-upgrade-backup.sh reads this function without it.
    ((${MODULE_UPGRADE:-0})) || return 0
    cat <<HINT

This upgrade runs a module upgrade (-u). From the mark of this upgrade on (the
containers of $OLD_RELEASE are removed right after it),
$OLD_RELEASE cannot run on database $DB_NAME: steps 1 to 5 are then the only
way back, and each of them is needed. Point of no return:
once users write data with $RELEASE, these steps lose that data; after that
point, fix forward instead. Also in this folder: module-upgrade.log (the log
of -u), uat-guard.json (the check of the modules) and, when there was retired
data, retired-data.tar.gz (the retired rows as CSV, before the upgrade).
HINT
}
go_back_steps() {  # back to OLD_RELEASE, from any of its scripts folders
    local dir backup
    dir=$(printf '%q' "$DEPLOY_DIR") backup=$(printf '%q' "$BACKUP_DIR")
    printf '   sudo cp -p %s/deployment-identity %s/.deployment-identity\n' "$backup" "$dir"
    [[ ! -f $BACKUP_DIR/app.env ]] || printf '   sudo cp -p %s/app.env %s/app.env\n' "$backup" "$dir"
    printf '   Then, from a scripts folder of %s: sudo bash deploy-app.sh --dir %s\n' "$OLD_RELEASE" "$dir"
}
if ((UPGRADE)); then
    upgrade_check_database
    if ((PENDING_ONLY == 0)); then
        if ((MODULE_UPGRADE)); then
            module_upgrade_guard
            module_upgrade_confirm
        fi
        upgrade_backup
        if ((MODULE_UPGRADE)); then
            upgrade_recheck_database  # RETIRED_TOTAL: the count with the App stopped
            ((RETIRED_TOTAL == 0)) || export_retired_data
            cp -p -- "$TEMP_DIR/uat-guard.json" "$BACKUP_DIR/uat-guard.json"
            module_upgrade
        fi
    fi
    UPGRADE_STATE=switched
fi
install -d -m 0700 "$DEPLOY_DIR/secrets"
for file in compose.yml preflight.py uat_guard.py uat_admins.py; do mv -f -- "$TEMP_DIR/$file" "$DEPLOY_DIR/$file"; done
mv -f -- "$TEMP_DIR/db_password" "$DEPLOY_DIR/secrets/db_password"
# A first deployment stays unverified until it uses its database (bind_directory).
if [[ ! -e $DEPLOY_DIR/.deployment-identity ]]; then
    : > "$TEMP_DIR/unverified"
    mv -f -- "$TEMP_DIR/unverified" "$UNVERIFIED"  # a rename replaces a symlink there, never writes through it
fi
mv -f -- "$TEMP_DIR/identity" "$DEPLOY_DIR/.deployment-identity"
{
    for key in DB_HOST DB_PORT DB_NAME DB_USER PROJECT_NAME HTTP_PORT BIND_IP STARTUP_TIMEOUT INIT_TIMEOUT; do printf '%s=%s\n' "$key" "${!key}"; done
    # Only when set: empty is their default, and an app.env without them stays
    # readable by the scripts of v1.0.3 and earlier, which refuse unknown keys.
    for key in PUBLIC_ROOT ENVIRONMENT_LABEL PUBLIC_BASE_URL; do
        [[ -z ${!key} ]] || printf '%s=%s\n' "$key" "${!key}"
    done
    printf 'DB_PASSWORD_FILE=%s/secrets/db_password\n' "$DEPLOY_DIR"
} > "$DEPLOY_DIR/app.env"
compose() { docker compose --project-name "$PROJECT_NAME" --file "$DEPLOY_DIR/compose.yml" "$@"; }
compose config --quiet
preflight() { compose run --rm --no-deps -T odoo python3 /opt/deploy/preflight.py "$@"; }
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
clear_leftover_attachments() {
    # Files an earlier deployment of this project left in the volume belong to
    # that deployment's database, not to a new empty one.
    local files answer
    files=$(compose run --rm --no-deps -T odoo bash -c 'find /var/lib/odoo/filestore -type f 2>/dev/null | wc -l' </dev/null) \
        || fail "Cannot read the attachments volume $FILESTORE_VOLUME"
    [[ $files =~ ^[0-9]+$ ]] || fail "Cannot read the attachments volume $FILESTORE_VOLUME"
    ((files > 0)) || return 0
    printf 'The attachments volume %s still holds %s files from an earlier deployment of this project. They belong to its database, so a new UAT system cannot use them.\n' "$FILESTORE_VOLUME" "$files"
    if ((NON_INTERACTIVE)) || [[ ! -t 0 ]]; then
        fail "Delete the volume (sudo docker volume rm $FILESTORE_VOLUME) or set another PROJECT_NAME, then rerun. The database was not changed"
    fi
    read -r -p "Delete $FILESTORE_VOLUME and continue? [y/N]: " answer || fail 'Input cancelled'
    [[ ${answer:-n} == [Yy]* ]] \
        || fail "Kept $FILESTORE_VOLUME. Delete it (sudo docker volume rm $FILESTORE_VOLUME) or set another PROJECT_NAME, then rerun. The database was not changed"
    docker volume rm "$FILESTORE_VOLUME" >/dev/null || fail "Could not delete $FILESTORE_VOLUME"
    printf 'Deleted %s.\n' "$FILESTORE_VOLUME"
}
bind_directory() {  # from here on, this directory belongs to this database
    rm -f -- "$UNVERIFIED"
}
# --accept-pending: a database that a module upgrade upgraded and checked
# reports UPGRADE_PENDING, and this deployment finishes it. Every other caller
# of check (service.sh, of any release) is refused it.
if ! state=$(preflight check --accept-pending); then
    unverified || fail 'The database check above failed. Fix the cause and run deploy-app.sh again'
    fail "The database check above failed, before this deployment used the database. This App server's addresses: $(server_addresses | paste -sd ' ' -). Correct DB_HOST, DB_PORT, DB_NAME or DB_USER in $DEPLOY_DIR/app.env, or the settings on the DB server (the App server address it accepts, its firewall), then run deploy-app.sh again: on a terminal it asks for the database password again"
fi
FRESH=0
case $state in
    MISSING|EMPTY|MISSING_NO_CREATEDB)
        ((INIT_DB)) || fail 'Database is missing or empty. Restore a matching database, or create an empty one with deploy-db.sh DB_MODE=empty and rerun with --init-db'
        [[ $state != MISSING_NO_CREATEDB ]] || fail "Database $DB_NAME does not exist and $DB_USER may not create databases (by design). On the DB server run deploy-db.sh with DB_MODE=empty to create it, then rerun with --init-db"
        bind_directory
        ((LEFTOVER == 0)) || clear_leftover_attachments
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
        bind_directory
        STARTED=1
        if [[ $state == SETUP_UNMARKED ]]; then
            [[ $(preflight mark-pending) == PENDING ]] || fail 'Initialized database failed the fresh UAT checks'
        fi
        uat_setup
        FRESH=1 ;;
    READY\ *)
        printf 'Existing initialized database accepted; initialization and module upgrades are skipped.\n'
        bind_directory
        if ((ADOPTED)); then
            printf 'Using the attachments volume %s that an earlier deployment of this project left; it is checked against the database below.\n' "$FILESTORE_VOLUME"
        fi ;;
    UPGRADE_PENDING\ *)
        # Upgraded and checked by --upgrade (this run or an earlier one); the
        # stamp follows the checks below. No module upgrade runs here.
        printf 'The Odoo modules of database %s are upgraded to %s and checked: deploying it, with no module upgrade.\n' "$DB_NAME" "$RELEASE"
        bind_directory
        PENDING_STAMP=1 ;;
    *) fail 'Unexpected database preflight response' ;;
esac
if ((FRESH)); then
    # A fresh database is stamped READY only after the sign-in checks below pass.
    # Until then it stays SETUP_PENDING, so a rerun with --init-db checks it all
    # again instead of accepting a system nobody could sign in to.
    attachments=${state#VERIFIED }
elif ((PENDING_STAMP)); then
    attachments=${state#UPGRADE_PENDING }
else
    [[ $state == READY\ * ]] || fail 'Database initialization did not reach READY'
    attachments=${state#READY }
fi
if ((attachments > 0)); then
    printf 'Database has %s file attachments. Checking its paired filestore volume.\n' "$attachments"
    # Exit 3: attachments are missing; it then lists the database folders in the volume.
    status=0
    compose run --rm --no-deps -T odoo python3 -c '
import os, pathlib, psycopg2, sys
p=pathlib.Path("/run/secrets/db_password").read_text()
db=os.environ["DB_NAME"]
c=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=p,dbname=db,connect_timeout=10)
with c.cursor() as q:
 q.execute("SELECT store_fname FROM ir_attachment WHERE store_fname IS NOT NULL")
 names=[name for (name,) in q.fetchall()]
base=pathlib.Path("/var/lib/odoo/filestore")
missing=sum(not (base/db/name).is_file() for name in names)
if missing:
 print("%d of the %d attachments of database %s are not in the volume." % (missing, len(names), db), file=sys.stderr)
 folders=[]
 try:
  for d in sorted(base.iterdir()):
   if d.is_dir():
    try:
     n=sum(1 for f in d.rglob("*") if f.is_file())
     folders.append("%s (%d file%s)" % (d.name, n, "" if n == 1 else "s"))
    except OSError:
     folders.append("%s (unreadable)" % d.name)
 except OSError:
  pass
 print("Database folders in the volume: %s." % (", ".join(folders) or "none"), file=sys.stderr)
 sys.exit(3)
' || status=$?
    ((status != 3)) || fail "Filestore incomplete: the attachments volume $FILESTORE_VOLUME does not hold every attachment of database $DB_NAME (above). An App uninstall keeps the volume for the same database; this database's attachments must come from where the database came from (see From a backup in the README). Restore them into the volume, then run deploy-app.sh again"
    ((status == 0)) || fail "Could not check the attachments volume $FILESTORE_VOLUME against database $DB_NAME (above)"
fi
# Every run, for a new and an initialized database alike, before the containers
# are recreated: report.url, and PUBLIC_BASE_URL as the frozen web.base.url.
# A running Odoo keeps these parameters cached and does not see this direct
# write, so an administrator sign-in it handles before the recreation below
# (even one that reached it before the web container stopped) would still
# overwrite web.base.url, which then stays frozen. Both are stopped first; the
# write runs in its own one-off container, and the recreation below starts
# them again anyway.
compose stop web odoo
[[ $(preflight public-urls "$PUBLIC_BASE_URL") == PUBLIC_URLS_SET ]] \
    || fail 'Could not record the report and public addresses in the database. The App is stopped: fix the cause and run deploy-app.sh again'
STARTED=1
compose up -d --force-recreate --wait --wait-timeout "$STARTUP_TIMEOUT"
compose exec -T odoo python3 - "$REVISION" "$STARTUP_TIMEOUT" "$PUBLIC_ROOT" "$FRESH" <<'PY'
import http.cookiejar, json, sys, time, urllib.parse, urllib.request, urllib.error
revision=sys.argv[1]
public_root=sys.argv[3]
fresh=sys.argv[4] == '1'
def fetch(path):
    with urllib.request.urlopen('http://web' + path, timeout=20) as response:
        return json.load(response)
class KeepRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # report a redirect instead of following it
public_opener=urllib.request.build_opener(KeepRedirects)
def public(path):
    # What a browser behind the front proxy or F5 reaches: the web container's
    # public server, for any host name but web, the internal one that the
    # checks above and the health check use (127.0.0.1 and localhost are
    # public too: a browser on this server or through an SSH tunnel).
    request=urllib.request.Request('http://web' + path, headers={'Host': 'perodua-public-check'})
    try:
        with public_opener.open(request, timeout=20) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()
def public_json(path):
    status, _, body = public(path)
    if status != 200:
        raise ValueError('public ' + path + ' returned HTTP ' + str(status))
    try:
        return json.loads(body)
    except ValueError:
        raise ValueError('public ' + path + ' did not return JSON')
def verify_public():
    if public_json(public_root + '/app/version.json').get('source_revision') != revision:
        raise ValueError('public ' + public_root + '/app/version.json names another source revision')
    if public_json(public_root + '/uiux/api/version').get('code') != 0:
        raise ValueError('public ' + public_root + '/uiux/api/version did not answer')
    status, _, body = public(public_root + '/app/')
    if status != 200 or (public_root + '/app/assets/').encode() not in body:
        raise ValueError('public ' + public_root + '/app/ does not load its files from ' + public_root + '/app/assets/')
    if b'__PERODUA_' in body:
        raise ValueError('public ' + public_root + '/app/ still contains a build placeholder')
    if not public_root:
        return
    status, headers, _ = public(public_root + '/')
    location=urllib.parse.urlsplit(headers.get('Location') or '').path
    if status not in (301, 302, 303, 307, 308) or location != public_root + '/app/':
        raise ValueError('public ' + public_root + '/ does not redirect to ' + public_root + '/app/')
    # Only the page's own paths are open: Odoo's pages, and the page without
    # its path, are closed.
    for path in (public_root + '/my', '/app/'):
        status, _, _ = public(path)
        if status != 404:
            raise ValueError('public ' + path + ' returned HTTP ' + str(status) + ' instead of 404')
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
    verify_public()
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
if public_root:
    print('Verified the public page under ' + public_root + '/app/, and that other public paths are closed.')
else:
    print('Verified the public page at /app/.')
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
if ((PENDING_STAMP)); then
    # From here on the database check reports READY: the upgrade is done.
    state=$(preflight stamp-upgrade)
    [[ $state == READY\ * ]] || fail 'The upgraded database could not be stamped READY. Run deploy-app.sh again'
fi
printf '\nDeployment verified: %s (%s)\n' "$RELEASE" "$REVISION"
if ((UPGRADE)) && [[ -n $BACKUP_DIR ]]; then
    printf 'Upgraded from %s. The data as it was before: %s (restore.txt explains how to put it back).\n' "$OLD_RELEASE" "$BACKUP_DIR"
fi
if ((PENDING_STAMP)); then
    printf 'The Odoo modules were upgraded (-u). %s cannot run on this database any more: the backup is the only way back, and only until users write data (the point of no return).\n' \
        "${OLD_RELEASE:-The release before}"
    [[ -z $HELD_MAIL ]] || printf 'Held mail: %s that the module upgrade queued, %s that waited in the queue before the upgrade.\n' \
        "$HELD_MAIL" "$HELD_MAIL_BEFORE"
    # The public host names do not open Odoo's own pages when PUBLIC_ROOT is
    # set, so the message also names the list that needs no web page.
    printf 'Review the held mail: README.md, "Upgrade an App server to %s", step 13 lists it with psql on the DB server (in Odoo: Settings > Technical > Emails, status Delivery Failed). Then send it with: sudo bash deploy-app.sh --release-queued-mail --dir %q\n' \
        "${RELEASE##*-}" "$DEPLOY_DIR"
fi
if [[ $BIND_IP == 127.0.0.1 ]]; then
    printf 'HTTP URL: http://127.0.0.1:%s%s/app/ (this server only). From your computer: ssh -N -L %s:127.0.0.1:%s <user>@<APP_SERVER_IP>, then open http://localhost:%s%s/app/\n' \
        "$HTTP_PORT" "$PUBLIC_ROOT" "$HTTP_PORT" "$HTTP_PORT" "$HTTP_PORT" "$PUBLIC_ROOT"
else
    printf 'HTTP URL: http://%s:%s%s/app/\n' "${BIND_IP/0.0.0.0/<APP_SERVER_IP>}" "$HTTP_PORT" "$PUBLIC_ROOT"
fi
[[ -z $PUBLIC_BASE_URL ]] || printf 'Public URL (through the front proxy or F5): %s/app/\n' "$PUBLIC_BASE_URL"
printf 'Compose: %s/compose.yml\nFilestore volume: %s_filestore\n' "$DEPLOY_DIR" "$PROJECT_NAME"
if ((FRESH)); then
    printf 'UAT administrators (UAT only, fixed password): whadmin, admin1, admin2 / perodua\n'
fi
printf 'HTTP only. Configure a trusted HTTPS entry point before exposing real credentials.\n'
printf 'Browser login, business journeys and real-server firewall checks remain part of deployment acceptance.\n'
