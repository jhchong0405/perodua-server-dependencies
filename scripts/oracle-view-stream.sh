#!/usr/bin/env bash
# Puts the pinned image of oracle-view.sh on an App server from this computer (the Mac,
# which has the registry login), for: sudo bash oracle-view.sh install --no-pull. The
# server needs no registry login, and nobody types a password on it. It never asks
# anything: ssh runs in batch mode and sudo with -n. It runs with the bash of macOS (3.2).
set +x
set -Eeuo pipefail
export LC_ALL=C

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DRY_RUN=0 HOST='' LOCK='' SERVER=''
CONTAINERD='["driver-type","io.containerd.snapshotter.v1"]'   # in docker info: the containerd image store
FACTS='{{.Id}}{{range .RepoDigests}} {{.}}{{end}}'              # what oracle-view.sh install --no-pull checks
# ssh never asks, never makes a terminal, forwards nothing and runs no local command; a dead link ends in about a minute.
SSH=(ssh -T -a -x -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes -o PermitLocalCommand=no
     -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

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
login of this computer). It saves the image under its stream tag,
REPOSITORY:sha256-HEX (HEX: the digits of the pinned digest), which no other image
takes: an archive of a digest reference has no image name, and a release tag can
move to a later image. It stops when the stream tag names another image here. The
engine of this computer must use the containerd image store; on the server, a
classic image store gets nothing either. The pin must be one image manifest (one
platform), not an index. A server that has the pinned image gets nothing. One run
at a time for each server (the host name and port of ssh -G).
Exit codes: 0 done; 1 error; 2 usage.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s (see: bash oracle-view-stream.sh --help)\n' "$*" >&2; exit 2; }
cleanup() { [[ -z $LOCK ]] || rm -rf -- "$LOCK"; }
trap cleanup EXIT

server_of_host() {   # the host name and port that ssh uses for SSH_HOST, so that two names of one server share a lock
    local config name port
    config=$(ssh -G "$HOST" 2> /dev/null) || config=''
    name=$(printf '%s\n' "$config" | sed -n 's/^hostname //p' | head -n 1)
    port=$(printf '%s\n' "$config" | sed -n 's/^port //p' | head -n 1)
    [[ -n $name ]] || name=${HOST#*@}
    SERVER=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]'):${port:-22}
}
lock() {   # a directory (mkdir is atomic on macOS and Linux) that holds the number of the process
    local dir pid
    dir=${TMPDIR:-/tmp}
    dir=${dir%/}/oracle-view-stream-$(printf '%s' "$SERVER" | tr -c 'A-Za-z0-9._-' _).lock
    if ! mkdir -m 0700 "$dir" 2> /dev/null; then
        [[ -d $dir && ! -L $dir ]] || die "The lock $dir is not a directory of oracle-view-stream.sh: remove it, then run again"
        pid=$(cat -- "$dir/pid" 2> /dev/null || true)
        if [[ ! $pid =~ ^[0-9]+$ ]] || kill -0 "$pid" 2> /dev/null; then
            die "Another oracle-view-stream.sh run (process ${pid:-unknown}) streams to the server $SERVER. If there is none, remove $dir"
        fi
        rm -rf -- "$dir"   # left by a run that stopped
        mkdir -m 0700 "$dir" 2> /dev/null || die "Another oracle-view-stream.sh run streams to the server $SERVER"
    fi
    LOCK=$dir
    printf '%s\n' "$$" > "$dir/pid"
}

remote() { "${SSH[@]}" -n "$HOST" "$1"; }   # a check; $1: a command line for the shell of the server
remote_load() { "${SSH[@]}" "$HOST" 'sudo -n docker load'; }   # the archive comes on the standard input
server_has_pin() {   # $1 store of the server; the check of install --no-pull: found by the digest reference, with the
    local facts      # pinned digest as registry digest, and on the containerd image store as ID
    facts=$(remote "sudo -n docker image inspect --format '$FACTS' $PINNED" 2> /dev/null) || return 1
    [[ " $facts " == *" $PINNED "* ]] || return 1
    [[ $1 != containerd || ${facts%% *} == "$DIGEST" ]]
}
one_manifest() {   # docker save needs all the content of the image: of an index, a pull for one platform brings a part
    local kind
    kind=$(docker image inspect --format '{{.Descriptor.MediaType}}' "$PINNED" 2> /dev/null) || kind=''
    [[ $kind == application/vnd.oci.image.manifest.v1+json || $kind == application/vnd.docker.distribution.manifest.v2+json ]] \
        || die "The pinned image on this computer is ${kind:-of an unknown media type}, not one image manifest: docker save cannot save an index of which this computer has only the $PLATFORM part. This script streams a single-platform image only. Nothing was streamed."
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
name='^[A-Za-z0-9][A-Za-z0-9._@-]*$'
[[ $HOST =~ $name ]] || usage_error "SSH_HOST may hold only letters, digits, ., _, - and @, and starts with a letter or digit: $HOST"

# The pin stays in one place: oracle-view.sh next to this script.
[[ -f $HERE/oracle-view.sh ]] || die "oracle-view.sh must be next to this script, in $HERE"
IMAGE=$(sed -n 's/^IMAGE=\([^ ]*\).*/\1/p' "$HERE/oracle-view.sh")
PLATFORM=$(sed -n 's/^PLATFORM=\([^ ]*\).*/\1/p' "$HERE/oracle-view.sh")
form='^[a-z0-9.-]+/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$'
[[ $IMAGE =~ $form ]] || die 'oracle-view.sh must hold one IMAGE=REPOSITORY:TAG@sha256:DIGEST line'
form='^linux/[a-z0-9]+$'
[[ $PLATFORM =~ $form ]] || die 'oracle-view.sh must hold one PLATFORM=linux/ARCH line'
REGISTRY=${IMAGE%%/*} REPOSITORY=${IMAGE%%[:@]*} DIGEST=${IMAGE##*@}
PINNED=$REPOSITORY@$DIGEST
STREAM_TAG=$REPOSITORY:${DIGEST/:/-}   # one tag for each digest: a later pin never moves it

server_of_host
lock
command -v docker > /dev/null || die 'Docker is needed on this computer'
store=$(docker info --format '{{json .DriverStatus}}' 2> /dev/null) || die 'Docker on this computer does not answer docker info: start it first'
[[ $store == *"$CONTAINERD"* ]] || die 'Docker on this computer does not use the containerd image store: its docker save does not keep the manifest of the registry, so the server cannot check the pinned digest. Docker Desktop: Settings, General, Use containerd for pulling and storing images.'
here=0
id=$(docker image inspect --format '{{.Id}}' "$PINNED" 2> /dev/null) || id=''
[[ $id != "$DIGEST" ]] || here=1
((here == 0)) || one_manifest
tagged=$(docker image inspect --format '{{.Id}}' "$STREAM_TAG" 2> /dev/null) || tagged=''
[[ -z $tagged || $tagged == "$DIGEST" ]] \
    || die "On this computer the stream tag $STREAM_TAG names another image (ID $tagged), not the pinned digest. Remove that tag (docker image rm $STREAM_TAG), then run this script again. Nothing was changed."

out=$(remote "sudo -n docker version --format '{{.Server.APIVersion}}'" 2>&1) \
    || die "ssh $HOST (batch mode) with sudo -n docker failed: $(printf '%s\n' "$out" | tail -n 1). ssh must log in with a key and no question, and the user must run docker with sudo -n."
store=$(remote "sudo -n docker info --format '{{json .DriverStatus}}'" 2>&1) \
    || die "docker info failed on $HOST: $(printf '%s\n' "$store" | tail -n 1). Nothing was changed."
if [[ $store == *"$CONTAINERD"* ]]; then store=containerd; else store=classic; fi
if server_has_pin "$store"; then
    printf 'The pinned image is already on %s (%s image store): %s. Nothing to stream.\n' "$HOST" "$store" "$PINNED"
    next_step
    exit 0
fi
[[ $store == containerd ]] || die "The Docker engine of $HOST uses the classic image store: there an image from docker load has no registry digest, and install --no-pull does not use it. Switch that engine to the containerd image store first (README.md, Oracle Field Browser, Install from a streamed image). Nothing was changed."

if ((DRY_RUN)); then
    if ((here)); then
        printf 'On this computer: %s\n' "$PINNED"
    else
        printf 'Not on this computer: %s\n' "$PINNED"
    fi
    printf 'Not on %s. Dry run: nothing was changed. The steps:\n' "$HOST"
    ((here)) || printf '  docker pull --platform %s %s\n' "$PLATFORM" "$IMAGE"
    [[ -n $tagged ]] || printf '  docker tag %s %s\n' "$PINNED" "$STREAM_TAG"
    printf "  docker save %s | ssh %s 'sudo -n docker load'\n" "$STREAM_TAG" "$HOST"
    printf '  the check of %s on %s\n' "$PINNED" "$HOST"
    exit 0
fi

if ((here == 0)); then
    printf 'Pulling the pinned image on this computer: %s\n' "$IMAGE"
    docker pull --platform "$PLATFORM" "$IMAGE" \
        || die "The pull failed on this computer (above). When the registry wants a login, run docker login $REGISTRY on this computer first."
    id=$(docker image inspect --format '{{.Id}}' "$PINNED" 2> /dev/null) || id=''
    [[ $id == "$DIGEST" ]] || die "After the pull, $PINNED on this computer does not have the pinned digest as its ID (ID: ${id:-none}). Nothing was streamed."
    one_manifest
fi
if [[ -z $tagged ]]; then
    docker tag "$PINNED" "$STREAM_TAG" || die "docker tag $PINNED $STREAM_TAG failed on this computer (above). Nothing was streamed."
fi
printf 'Streaming %s to %s...\n' "$STREAM_TAG" "$HOST"
if ! out=$(docker save "$STREAM_TAG" | remote_load 2>&1); then
    printf '%s\n' "$out" >&2
    die "The stream to $HOST failed (above)"
fi
printf '%s\n' "$out"
[[ $out != *'Error unpacking'* ]] \
    || die "docker load on $HOST could not unpack the image (above). Remove it there (sudo docker image rm $STREAM_TAG), then run this script again."
server_has_pin containerd || die "After docker load, $HOST does not find $PINNED with the pinned digest"
printf 'On %s: %s, ID %s\n' "$HOST" "$PINNED" "$DIGEST"
next_step
