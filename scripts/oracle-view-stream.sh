#!/usr/bin/env bash
# Puts the pinned image of oracle-view.sh on an App server from this computer (the Mac,
# which has the registry login), for: sudo bash oracle-view.sh install --no-pull. The
# transport is stream-image.sh next to this script, which owns it for every kit script:
# this command is bash scripts/stream-image.sh [--dry-run] SSH_HOST oracle-view.sh. It
# never asks anything. It runs with the bash of macOS (3.2).
set +x
set -Eeuo pipefail
export LC_ALL=C

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DRY_RUN=0 HOST=''

usage() {
    cat <<'HELP'
Usage, on the Mac, in the kit repository:
       bash scripts/oracle-view-stream.sh [--dry-run] SSH_HOST

Streams the pinned image of oracle-view.sh to the App server SSH_HOST (docker save
here, sudo -n docker load there) and checks it there as install --no-pull does.
Then run on the server:
       sudo bash oracle-view.sh install --no-pull --non-interactive
SSH_HOST   The ssh name of the App server (a Host of ~/.ssh/config, or USER@HOST).
           ssh must log in with a key and no question (batch mode), and the user
           must run docker with sudo -n (no password).
--dry-run  The checks here and on the server, and the steps it would run. It
           pulls, tags and loads nothing.
It runs bash scripts/stream-image.sh [--dry-run] SSH_HOST oracle-view.sh: the
checks, the stream tag, the lock for each server and the messages are those of
stream-image.sh (bash scripts/stream-image.sh --help; README.md, "Install from
streamed images").
Exit codes: 0 done; 1 error; 2 usage.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s (see: bash oracle-view-stream.sh --help)\n' "$*" >&2; exit 2; }

while (($#)); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -*) usage_error "Unknown option: $1" ;;
        *) [[ -z $HOST ]] || usage_error "Give one SSH_HOST only: $1"; HOST=$1; shift ;;
    esac
done
[[ -n $HOST ]] || usage_error 'Missing SSH_HOST'
[[ -f $HERE/stream-image.sh ]] || die "stream-image.sh must be next to this script, in $HERE"
if ((DRY_RUN)); then
    exec "$BASH" "$HERE/stream-image.sh" --dry-run "$HOST" oracle-view.sh
fi
exec "$BASH" "$HERE/stream-image.sh" "$HOST" oracle-view.sh
