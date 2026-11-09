#!/usr/bin/env bash
# Puts the pinned image of oracle-view.sh on an App server from this computer (the Mac,
# which has the registry login), for: sudo bash oracle-view.sh install --no-pull. The
# server needs no registry login, and nobody types a password on it. It never asks
# anything: ssh runs in batch mode and sudo with -n. It runs with the bash of macOS (3.2).
set +x
set -Eeuo pipefail
export LC_ALL=C

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DRY_RUN=0 HOST='' LOCK=''
CONTAINERD='["driver-type","io.containerd.snapshotter.v1"]'   # in docker info: the containerd image store
FACTS='{{.Id}}{{range .RepoDigests}} {{.}}{{end}}'              # what oracle-view.sh install --no-pull checks

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
When the pinned image is not on this computer, it pulls it first (the registry
login of this computer). It points the tag of the pin here at the pinned digest
and saves that tag: an archive of a digest reference has no image name, and the
server would keep a dangling image that install --no-pull does not find. Both
engines must use the containerd image store. A server that has the pinned image
gets nothing. One run at a time for each SSH_HOST.
Exit codes: 0 done; 1 error; 2 usage.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s (see: bash oracle-view-stream.sh --help)\n' "$*" >&2; exit 2; }
cleanup() { [[ -z $LOCK ]] || rm -f "$LOCK"; }
trap cleanup EXIT

lock() {   # macOS has no flock: the lock is a symbolic link to the number of the process
    local file pid
    file=${TMPDIR:-/tmp}
    file=${file%/}/oracle-view-stream-$(printf '%s' "$HOST" | tr -c 'A-Za-z0-9._-' _).lock
    if ! ln -s "$$" "$file" 2> /dev/null; then
        pid=$(readlink "$file" 2> /dev/null || true)
        if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2> /dev/null; then
            die "Another oracle-view-stream.sh run (process $pid) streams to $HOST. If there is none, remove $file"
        fi
        rm -f "$file"   # left by a run that stopped
        ln -s "$$" "$file" 2> /dev/null || die "Another oracle-view-stream.sh run streams to $HOST"
    fi
    LOCK=$file
}

remote() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$1"; }   # $1: a command line for the shell of the server
server_has_pin() {   # the check of install --no-pull: found by the digest reference, with the pinned digest as ID and registry digest
    local facts
    facts=$(remote "sudo -n docker image inspect --format '$FACTS' $PINNED" 2> /dev/null) || return 1
    [[ " $facts " == *" $PINNED "* && ${facts%% *} == "$DIGEST" ]]
}
next_step() { printf 'Next, on %s: sudo bash oracle-view.sh install --no-pull --non-interactive\n' "$HOST"; }

while (($#)); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -*) usage_error "Unknown option: $1" ;;
        *) [[ -z $HOST ]] || usage_error "Give one SSH_HOST only: $1"; HOST=$1; shift ;;
    esac
done
[[ -n $HOST ]] || usage_error 'Missing SSH_HOST'
name='^[A-Za-z0-9_][A-Za-z0-9._@-]*$'
[[ $HOST =~ $name ]] || usage_error "SSH_HOST may hold only letters, digits, ., _, - and @, and starts with a letter or digit: $HOST"

# The pin stays in one place: oracle-view.sh next to this script.
[[ -f $HERE/oracle-view.sh ]] || die "oracle-view.sh must be next to this script, in $HERE"
IMAGE=$(sed -n 's/^IMAGE=\([^ ]*\).*/\1/p' "$HERE/oracle-view.sh")
PLATFORM=$(sed -n 's/^PLATFORM=\([^ ]*\).*/\1/p' "$HERE/oracle-view.sh")
form='^[a-z0-9.-]+/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$'
[[ $IMAGE =~ $form ]] || die 'oracle-view.sh must hold one IMAGE=REPOSITORY:TAG@sha256:DIGEST line'
form='^linux/[a-z0-9]+$'
[[ $PLATFORM =~ $form ]] || die 'oracle-view.sh must hold one PLATFORM=linux/ARCH line'
REGISTRY=${IMAGE%%/*} REPOSITORY=${IMAGE%%[:@]*} DIGEST=${IMAGE##*@} TAGGED=${IMAGE%@*}
PINNED=$REPOSITORY@$DIGEST

lock
command -v docker > /dev/null || die 'Docker is needed on this computer'
store=$(docker info --format '{{json .DriverStatus}}' 2> /dev/null) || die 'Docker on this computer does not answer docker info: start it first'
[[ $store == *"$CONTAINERD"* ]] || die 'Docker on this computer does not use the containerd image store: its docker save does not keep the manifest of the registry, so the server cannot check the pinned digest. Docker Desktop: Settings, General, Use containerd for pulling and storing images.'
here=0
id=$(docker image inspect --format '{{.Id}}' "$PINNED" 2> /dev/null) || id=''
[[ $id != "$DIGEST" ]] || here=1

out=$(remote "sudo -n docker version --format '{{.Server.APIVersion}}'" 2>&1) \
    || die "ssh $HOST (batch mode) with sudo -n docker failed: $(printf '%s\n' "$out" | tail -n 1). ssh must log in with a key and no question, and the user must run docker with sudo -n."
store=$(remote "sudo -n docker info --format '{{json .DriverStatus}}'" 2> /dev/null) || die "docker info failed on $HOST"
[[ $store == *"$CONTAINERD"* ]] || die "The Docker engine of $HOST uses the classic image store: there an image from docker load has no registry digest, and install --no-pull does not use it. Switch that engine to the containerd image store first (README.md, Oracle Field Browser, Install from a streamed image). Nothing was changed."
if server_has_pin; then
    printf 'The pinned image is already on %s: %s. Nothing to stream.\n' "$HOST" "$PINNED"
    next_step
    exit 0
fi

if ((DRY_RUN)); then
    if ((here)); then
        printf 'On this computer: %s\n' "$PINNED"
    else
        printf 'Not on this computer: %s\n' "$PINNED"
    fi
    printf 'Not on %s. Dry run: nothing was changed. The steps:\n' "$HOST"
    ((here)) || printf '  docker pull --platform %s %s\n' "$PLATFORM" "$IMAGE"
    printf '  docker tag %s %s\n' "$PINNED" "$TAGGED"
    printf "  docker save %s | ssh %s 'sudo -n docker load'\n" "$TAGGED" "$HOST"
    printf '  the check of %s on %s\n' "$PINNED" "$HOST"
    exit 0
fi

if ((here == 0)); then
    printf 'Pulling the pinned image on this computer: %s\n' "$TAGGED"
    docker pull --platform "$PLATFORM" "$IMAGE" \
        || die "The pull failed on this computer (above). When the registry wants a login, run docker login $REGISTRY on this computer first."
    id=$(docker image inspect --format '{{.Id}}' "$PINNED" 2> /dev/null) || id=''
    [[ $id == "$DIGEST" ]] || die "After the pull, $PINNED on this computer does not have the pinned digest as its ID"
fi
docker tag "$PINNED" "$TAGGED"
printf 'Streaming %s to %s...\n' "$TAGGED" "$HOST"
docker save "$TAGGED" | remote 'sudo -n docker load' || die "The stream to $HOST failed (above)"
server_has_pin || die "After docker load, $HOST does not find $PINNED with the pinned digest"
printf 'On %s: %s, ID %s\n' "$HOST" "$PINNED" "$DIGEST"
next_step
