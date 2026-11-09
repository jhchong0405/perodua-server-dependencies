#!/usr/bin/env bash
# Puts pinned images on a server from this computer (the Mac, which has the registry
# login), for the --no-pull runs of deploy-app.sh, iss-api.sh, rp-adapter.sh and
# oracle-view.sh, and for an image that no kit script pins. The server needs no registry
# login, and nobody types a password on it. It never asks anything: ssh runs in batch
# mode and sudo with -n. It runs with the bash of macOS (3.2).
set +x
set -Eeuo pipefail
export LC_ALL=C

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DRY_RUN=0 FILE_MODE=0 HOST='' LOCK='' SERVER='' PLATFORM_OPTION='' SERVER_STORE=''
CONTAINERD='["driver-type","io.containerd.snapshotter.v1"]'   # in docker info: the containerd image store
FACTS='{{.Id}}{{range .RepoDigests}} {{.}}{{end}}'              # what the --no-pull runs check
PIN_FORM='^[a-z0-9.-]+(:[0-9]{1,5})?/[a-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$'   # the pin form of the kit
PLATFORM_FORM='^linux/[a-z0-9]+$'
MANIFEST_TYPES=' application/vnd.oci.image.manifest.v1+json application/vnd.docker.distribution.manifest.v2+json '
INDEX_TYPES=' application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json '
# docker buildx imagetools inspect --format: one line for each entry of an index: its digest, its media type, its
# platform (OS/ARCH[/VARIANT], - when it has none) and its vnd.docker.reference.type annotation (- when it has none).
ENTRIES='{{range .Manifest.Manifests}}{{.Digest}} {{.MediaType}} {{with .Platform}}{{.OS}}/{{.Architecture}}{{with .Variant}}/{{.}}{{end}}{{else}}-{{end}} {{with .Annotations}}{{or (index . "vnd.docker.reference.type") "-"}}{{else}}-{{end}}{{"\n"}}{{end}}'
KIT_SOURCES=' deploy-app.sh iss-api.sh rp-adapter.sh oracle-view.sh '
LINES=' client-stable-uiux demo-uiux '
# ssh never asks, never makes a terminal, forwards nothing and runs no local command; a dead link ends in about a minute.
SSH=(ssh -T -a -x -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes -o PermitLocalCommand=no
     -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
REMOTE_DIR=stream-image   # --file: the folder (0700) in the home folder of the ssh user on the server
ARCHIVE_DIR=''            # --file: the folder (0700) of the archives on this computer
# The sources, in the order of the command line: a kit script (with the line of deploy-app.sh) or an --image.
SOURCE_NAMES=() SOURCE_LINES=() SOURCE_IMAGES=()
# The images, one for each pin: the pin and its platform.
IMAGES=() PLATFORMS=()
# For each image: on this computer (1/0), the ID that its stream tag names here, and on the server (1/0).
MAC_HAS=() MAC_TAGGED=() SERVER_HAS=()
REPOSITORY='' DIGEST='' PINNED='' STREAM_TAG='' REGISTRY=''

usage() {
    cat <<'HELP'
Usage, on the Mac, in the kit repository:
       bash scripts/stream-image.sh [--dry-run] [--file] SSH_HOST SOURCE...

Streams pinned images to the server SSH_HOST (docker save here, sudo -n docker
load there) and checks each one there as the --no-pull runs of the kit do.
Then run the kit script on the server with --no-pull (README.md, "Install from
streamed images").
SSH_HOST   The ssh name of the server (a Host of ~/.ssh/config, or USER@HOST).
           ssh must log in with a key and no question (batch mode), and the user
           must run docker with sudo -n (no password).
SOURCE     A kit script next to this one, whose pins it reads (the pin stays in
           one place):
             deploy-app.sh [--line client-stable-uiux|demo-uiux]
                        the Odoo image and the web image of the line (default:
                        the default line of deploy-app.sh)
             iss-api.sh, rp-adapter.sh, oracle-view.sh
                        the image of the script
           or an image that no kit script pins:
             --image REPOSITORY:TAG@sha256:HEX
                        in the pin form of the kit; give it once for each image.
                        --platform linux/ARCH (default linux/amd64) applies to
                        every --image.
--file     For a big image over a link that can break: docker save to an
           archive on this computer (kept under the name of its stream tag until
           the check on the server passes), shasum -a 256, rsync --partial to the
           folder ~/stream-image (0700) of the ssh user on the server, sha256sum
           there must be the same, sudo -n docker load -i, the check, then the
           archive goes on both sides. A run after a break resumes from the kept
           archive. The server needs rsync and sha256sum.
--dry-run  The checks here and on the server, and the steps it would run. It
           pulls, tags, saves, copies and loads nothing.
--non-interactive, or KIT_NON_INTERACTIVE=1 in the environment: accepted as in
           every kit script (README.md, "Run the kit without a person"). This
           script never asks either way.
When a pinned image is not on this computer, it pulls it first (the registry
login of this computer). It saves each image under its stream tag,
REPOSITORY:sha256-HEX (HEX: the digits of the pinned digest), which no other
image takes: an archive of a digest reference has no image name, and a release
tag can move to a later image. It stops when a stream tag names another image
here. The engine of this computer must use the containerd image store; on the
server, a classic image store gets nothing either. Each pin is one image
manifest, or an index whose one image is the platform (linux/amd64) and whose
other entries are attestation manifests (platform unknown/unknown), as BuildKit
pushes it. For an index it reads the entries from the registry (docker buildx
imagetools inspect, also in a dry run) and checks that the index there has the
pinned digest. docker save has no --platform, so the archive holds the index
itself and the server finds the pinned digest. An image that the server has
already gets nothing. One run at a time for each server (the host name and port
of ssh -G).
Exit codes: 0 done; 1 error; 2 usage.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage_error() { printf 'ERROR: %s (see: bash stream-image.sh --help)\n' "$*" >&2; exit 2; }
cleanup() { [[ -z $LOCK ]] || rm -rf -- "$LOCK"; }
trap cleanup EXIT

server_of_host() {   # the host name and port that ssh uses for SSH_HOST, so that two names of one server share a lock
    local config name port
    config=$(ssh -G -o BatchMode=yes "$HOST" 2> /dev/null) || config=''
    name=$(printf '%s\n' "$config" | sed -n 's/^hostname //p' | head -n 1)
    port=$(printf '%s\n' "$config" | sed -n 's/^port //p' | head -n 1)
    [[ -n $name ]] || name=${HOST#*@}
    SERVER=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]'):${port:-22}
}
lock() {   # a directory (mkdir is atomic on macOS and Linux) that holds the number of the process
    local dir pid
    dir=${TMPDIR:-/tmp}
    dir=${dir%/}/stream-image-$(printf '%s' "$SERVER" | tr -c 'A-Za-z0-9._-' _).lock
    if ! mkdir -m 0700 "$dir" 2> /dev/null; then
        [[ -d $dir && ! -L $dir ]] || die "The lock $dir is not a directory of stream-image.sh: remove it, then run again"
        pid=$(cat -- "$dir/pid" 2> /dev/null || true)
        if [[ ! $pid =~ ^[0-9]+$ ]] || kill -0 "$pid" 2> /dev/null; then
            die "Another stream-image.sh run (process ${pid:-unknown}) streams to the server $SERVER. If there is none, remove $dir"
        fi
        rm -rf -- "$dir"   # left by a run that stopped
        mkdir -m 0700 "$dir" 2> /dev/null || die "Another stream-image.sh run streams to the server $SERVER"
    fi
    LOCK=$dir
    printf '%s\n' "$$" > "$dir/pid"
}

remote() { "${SSH[@]}" -n "$HOST" "$1"; }   # a command that reads nothing; $1: a command line for the shell of the server
remote_load() { "${SSH[@]}" "$HOST" 'sudo -n docker load'; }   # the archive comes on the standard input

pin_value() {   # $1 a kit script next to this one, $2 a name: the value of its one line NAME=VALUE (a comment may follow)
    local values
    values=$(sed -n "s/^$2=\\([^ ]*\\).*/\\1/p" "$HERE/$1")
    [[ -n $values && $values != *$'\n'* ]] || die "$1 must hold one $2= line"
    printf '%s\n' "$values"
}
pin_parts() {   # $1 a pin: REGISTRY, REPOSITORY (a registry port stays), DIGEST, PINNED and STREAM_TAG
    REGISTRY=${1%%/*} REPOSITORY=${1%:*@*} DIGEST=${1##*@}
    PINNED=$REPOSITORY@$DIGEST
    STREAM_TAG=$REPOSITORY:${DIGEST/:/-}   # one tag for each digest: a later pin never moves it
}
add_image() {   # $1 pin, $2 platform, $3 the source that names it; a pin that is there already is not added again
    local i=0
    [[ $1 =~ $PIN_FORM ]] || die "$3: $1 is not a pin of the form REPOSITORY:TAG@sha256:HEX"
    [[ $2 =~ $PLATFORM_FORM ]] || die "$3: the platform $2 is not of the form linux/ARCH"
    while ((i < ${#IMAGES[@]})); do
        [[ ${IMAGES[i]} != "$1" ]] || return 0
        i=$((i + 1))
    done
    IMAGES+=("$1") PLATFORMS+=("$2")
}
read_pins() {   # the images of every source, from the kit scripts next to this one
    local i=0 name line platform default prefix odoo web image
    while ((i < ${#SOURCE_NAMES[@]})); do
        name=${SOURCE_NAMES[i]}
        case $name in
            --image) add_image "${SOURCE_IMAGES[i]}" "${PLATFORM_OPTION:-linux/amd64}" '--image' ;;
            deploy-app.sh)
                [[ -f $HERE/$name ]] || die "$name must be next to this script, in $HERE"
                platform=$(pin_value "$name" PLATFORM)
                line=${SOURCE_LINES[i]}
                if [[ -z $line ]]; then
                    # shellcheck disable=SC2016
                    default=$(sed -n 's/^LINE=\${PERODUA_LINE:-\([a-z-]*\)}$/\1/p' "$HERE/deploy-app.sh")
                    [[ $LINES == *" $default "* ]] || die "deploy-app.sh must hold one line LINE=\${PERODUA_LINE:-LINE}"
                    line=$default
                    SOURCE_LINES[i]=$line
                fi
                prefix=''
                [[ $line != demo-uiux ]] || prefix=DEMO_
                odoo=$(pin_value "$name" "${prefix}ODOO_IMAGE")
                web=$(pin_value "$name" "${prefix}WEB_IMAGE")
                add_image "$odoo" "$platform" "deploy-app.sh --line $line"
                add_image "$web" "$platform" "deploy-app.sh --line $line" ;;
            *)
                [[ -f $HERE/$name ]] || die "$name must be next to this script, in $HERE"
                platform=$(pin_value "$name" PLATFORM)
                image=$(pin_value "$name" IMAGE)
                add_image "$image" "$platform" "$name" ;;
        esac
        i=$((i + 1))
    done
}

streamable() {   # $1 platform: docker save writes the pin itself, with all its content, so the server can check its digest
    # docker save has no --platform: with it, the archive holds the manifest of that platform instead of the pinned
    # index, and on the server the digest reference finds no image (lab of 2026-11-10). Without it, docker save
    # needs every entry of an index on this computer; a pull for one platform brings that image and its attestations.
    local descriptor kind='' form='"mediaType":"([^"]+)"' sum entries verdict
    descriptor=$(docker image inspect --format '{{json .Descriptor}}' "$PINNED" 2> /dev/null) || descriptor=''
    [[ ! $descriptor =~ $form ]] || kind=${BASH_REMATCH[1]}
    [[ -z $kind || $MANIFEST_TYPES != *" $kind "* ]] || return 0
    [[ -n $kind && $INDEX_TYPES == *" $kind "* ]] \
        || die "The pinned image $PINNED on this computer is ${kind:-of an unknown media type}: this script streams one image manifest, or an index of one $1 image and its attestation manifests. Nothing was streamed."
    # The entries of the index, from the registry with the login of this computer; the index there must have the pin.
    sum=$(docker buildx imagetools inspect --raw "$PINNED" | shasum -a 256) \
        || die "docker buildx imagetools inspect --raw $PINNED failed on this computer (above): the entries of an index come from the registry. When the registry wants a login, run docker login $REGISTRY on this computer first. Nothing was streamed."
    [[ sha256:${sum%% *} == "$DIGEST" ]] \
        || die "The registry gives another index for $PINNED (its digest is sha256:${sum%% *}). Nothing was streamed."
    entries=$(docker buildx imagetools inspect --format "$ENTRIES" "$PINNED") \
        || die "docker buildx imagetools inspect $PINNED failed on this computer (above). Nothing was streamed."
    # The one image of the platform (any variant of it) and attestation manifests of BuildKit, nothing else.
    verdict=$(printf '%s\n' "$entries" | awk -v platform="$1" -v manifests="$MANIFEST_TYPES" '
        NF == 0 { next }
        NF == 4 && ($3 == platform || index($3, platform "/") == 1) && index(manifests, " " $2 " ") { image++; next }
        NF == 4 && $3 == "unknown/unknown" && $4 == "attestation-manifest" { attestation++; next }
        { other++ }
        END { printf "%d %d", image, other }')
    [[ $verdict == '1 0' ]] \
        || die "The pinned index $PINNED holds: $(printf '%s\n' "$entries" | awk 'NF { printf "%s%s", sep, ($4 == "-" ? $3 : $3 " " $4); sep = ", " }'). This script streams an index only when its one image is $1 and each other entry is an attestation manifest (unknown/unknown): docker save writes the whole index, and the pull of this computer brings that image and its attestations only. Nothing was streamed."
    printf 'The pinned image is an index of one %s image and its attestation manifests: %s\n' "$1" "$PINNED"
}
server_has_pin() {   # the check of the --no-pull runs: found by the digest reference, with the pinned digest as
    local facts      # registry digest, and on the containerd image store as ID
    facts=$(remote "sudo -n docker image inspect --format '$FACTS' $PINNED" 2> /dev/null) || return 1
    [[ " $facts " == *" $PINNED "* ]] || return 1
    [[ $SERVER_STORE != containerd || ${facts%% *} == "$DIGEST" ]]
}
archive_name() { printf '%s.tar' "$(printf '%s' "$STREAM_TAG" | tr -c 'A-Za-z0-9._-' _)"; }
archive_dir() {   # --file: the folder of the archives on this computer, 0700 and of this user
    ARCHIVE_DIR=${TMPDIR:-/tmp}
    ARCHIVE_DIR=${ARCHIVE_DIR%/}/stream-image-archives
    [[ -e $ARCHIVE_DIR || -L $ARCHIVE_DIR ]] || mkdir -m 0700 "$ARCHIVE_DIR" 2> /dev/null || true
    [[ -d $ARCHIVE_DIR && ! -L $ARCHIVE_DIR && -O $ARCHIVE_DIR ]] \
        || die "$ARCHIVE_DIR is not a folder of this user: remove it, then run again"
    chmod 0700 "$ARCHIVE_DIR"
}
local_sum() { shasum -a 256 "$1" | sed 's/ .*//'; }

check_load() {   # $1 the output of docker load on the server
    printf '%s\n' "$1"
    [[ $1 != *'Error unpacking'* ]] \
        || die "docker load on $HOST could not unpack the image (above). Remove it there (sudo docker image rm $STREAM_TAG), then run this script again."
    server_has_pin || die "After docker load, $HOST does not find $PINNED with the pinned digest"
    printf 'On %s: %s, ID %s\n' "$HOST" "$PINNED" "$DIGEST"
}
stream_pipe() {   # docker save here | sudo -n docker load there
    local out
    printf 'Streaming %s to %s...\n' "$STREAM_TAG" "$HOST"
    if ! out=$(docker save "$STREAM_TAG" | remote_load 2>&1); then
        printf '%s\n' "$out" >&2
        die "The stream to $HOST failed (above)"
    fi
    check_load "$out"
}
stream_file() {   # --file: an archive here, rsync --partial, the same checksum there, docker load -i, then no archive
    local name archive sum kept='' there out
    name=$(archive_name)
    archive=$ARCHIVE_DIR/$name
    if [[ -f $archive && -f $archive.sha256 ]]; then
        kept=$(cat -- "$archive.sha256")
        sum=$(local_sum "$archive")
        if [[ $sum == "$kept" ]]; then
            printf 'Resuming from the kept archive %s\n' "$archive"
        else
            kept=''
        fi
    fi
    if [[ -z $kept ]]; then
        rm -f -- "$archive" "$archive.sha256" "$archive.part"
        printf 'Saving %s to %s...\n' "$STREAM_TAG" "$archive"
        docker save -o "$archive.part" "$STREAM_TAG" || die "docker save $STREAM_TAG failed on this computer (above). Nothing was copied."
        sum=$(local_sum "$archive.part")
        mv -f -- "$archive.part" "$archive"
        printf '%s\n' "$sum" > "$archive.sha256"
    fi
    remote "mkdir -p -m 0700 $REMOTE_DIR && chmod 0700 $REMOTE_DIR" \
        || die "Could not make the folder ~/$REMOTE_DIR on $HOST (above). The archive stays here: $archive"
    printf 'Copying %s to %s:%s/%s (rsync --partial)...\n' "$archive" "$HOST" "$REMOTE_DIR" "$name"
    rsync --partial -e "${SSH[*]}" "$archive" "$HOST:$REMOTE_DIR/$name" \
        || die "The copy to $HOST broke (above). Run this script again: it resumes from the archive kept here ($archive) and from the part on the server."
    there=$(remote "sha256sum $REMOTE_DIR/$name") || die "sha256sum of $REMOTE_DIR/$name failed on $HOST (above)"
    if [[ ${there%% *} != "$sum" ]]; then
        remote "rm -f $REMOTE_DIR/$name" || true
        die "On $HOST the archive $REMOTE_DIR/$name has another SHA-256 (${there%% *}) than here ($sum): it was removed there. Run this script again: it copies the archive kept here again."
    fi
    printf 'Same SHA-256 on %s: %s\n' "$HOST" "$sum"
    if ! out=$(remote "sudo -n docker load -i $REMOTE_DIR/$name" 2>&1); then
        printf '%s\n' "$out" >&2
        die "docker load -i on $HOST failed (above). The archives stay: run this script again."
    fi
    check_load "$out"
    remote "rm -f $REMOTE_DIR/$name" || printf 'NOTE The archive %s/%s stays on %s: remove it there.\n' "$REMOTE_DIR" "$name" "$HOST" >&2
    rm -f -- "$archive" "$archive.sha256"
}
next_steps() {   # the --no-pull runs of the kit scripts among the sources
    local i=0
    while ((i < ${#SOURCE_NAMES[@]})); do
        case ${SOURCE_NAMES[i]} in
            deploy-app.sh)
                printf 'Next, on %s: sudo bash deploy-app.sh --no-pull --non-interactive --line %s (with the --config and --dir of the deployment)\n' \
                    "$HOST" "${SOURCE_LINES[i]}" ;;
            --image) ;;
            *) printf 'Next, on %s: sudo bash %s install --no-pull --non-interactive\n' "$HOST" "${SOURCE_NAMES[i]}" ;;
        esac
        i=$((i + 1))
    done
}

while (($#)); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --file) FILE_MODE=1; shift ;;
        --non-interactive) shift ;;   # this script never asks
        --line|--image|--platform)
            (($# >= 2)) || usage_error "Missing value for $1"
            case $1 in
                --line)
                    n=${#SOURCE_NAMES[@]}
                    { ((n > 0)) && [[ ${SOURCE_NAMES[n - 1]} == deploy-app.sh && -z ${SOURCE_LINES[n - 1]} ]]; } \
                        || usage_error '--line belongs to the deploy-app.sh before it, once'
                    [[ $LINES == *" $2 "* ]] || usage_error "--line must be client-stable-uiux or demo-uiux: $2"
                    SOURCE_LINES[n - 1]=$2 ;;
                --image) SOURCE_NAMES+=(--image) SOURCE_LINES+=('') SOURCE_IMAGES+=("$2") ;;
                --platform)
                    [[ -z $PLATFORM_OPTION ]] || usage_error 'Give --platform once'
                    PLATFORM_OPTION=$2 ;;
            esac
            shift 2 ;;
        -*) usage_error "Unknown option: $1" ;;
        *)
            if [[ -z $HOST ]]; then
                HOST=$1
            else
                [[ $KIT_SOURCES == *" $1 "* ]] \
                    || usage_error "A SOURCE is deploy-app.sh, iss-api.sh, rp-adapter.sh, oracle-view.sh or --image PIN: $1"
                SOURCE_NAMES+=("$1") SOURCE_LINES+=('') SOURCE_IMAGES+=('')
            fi
            shift ;;
    esac
done
case ${KIT_NON_INTERACTIVE:-} in   # read as in every kit script; this script never asks either way
    ''|0|1) ;;
    *) usage_error 'KIT_NON_INTERACTIVE must be 1 (never ask) or 0 (ask on a terminal).' ;;
esac
[[ -n $HOST ]] || usage_error 'Missing SSH_HOST'
name='^[A-Za-z0-9][A-Za-z0-9._@-]*$'
[[ $HOST =~ $name ]] || usage_error "SSH_HOST may hold only letters, digits, ., _, - and @, and starts with a letter or digit: $HOST"
((${#SOURCE_NAMES[@]})) || usage_error 'Missing SOURCE: name a kit script or give --image PIN'
if [[ -n $PLATFORM_OPTION ]]; then
    [[ " ${SOURCE_NAMES[*]} " == *' --image '* ]] || usage_error '--platform belongs to --image'
    [[ $PLATFORM_OPTION =~ $PLATFORM_FORM ]] || usage_error "--platform must be linux/ARCH: $PLATFORM_OPTION"
fi
i=0
while ((i < ${#SOURCE_IMAGES[@]})); do
    [[ ${SOURCE_NAMES[i]} != --image || ${SOURCE_IMAGES[i]} =~ $PIN_FORM ]] \
        || usage_error "--image must be a pin of the form REPOSITORY:TAG@sha256:HEX (64 digits): ${SOURCE_IMAGES[i]}"
    i=$((i + 1))
done

# The pins stay in one place: the kit scripts next to this one.
read_pins

server_of_host
lock
command -v docker > /dev/null || die 'Docker is needed on this computer'
store=$(docker info --format '{{json .DriverStatus}}' 2> /dev/null) || die 'Docker on this computer does not answer docker info: start it first'
[[ $store == *"$CONTAINERD"* ]] || die 'Docker on this computer does not use the containerd image store: its docker save does not keep the manifest of the registry, so the server cannot check the pinned digest. Docker Desktop: Settings, General, Use containerd for pulling and storing images.'
((FILE_MODE == 0)) || { command -v rsync > /dev/null && command -v shasum > /dev/null; } \
    || die '--file needs rsync and shasum on this computer'

# This computer, for every image before the server: the pin, its kind and its stream tag.
i=0
while ((i < ${#IMAGES[@]})); do
    pin_parts "${IMAGES[i]}"
    here=0
    id=$(docker image inspect --format '{{.Id}}' "$PINNED" 2> /dev/null) || id=''
    [[ $id != "$DIGEST" ]] || here=1
    ((here == 0)) || streamable "${PLATFORMS[i]}"
    tagged=$(docker image inspect --format '{{.Id}}' "$STREAM_TAG" 2> /dev/null) || tagged=''
    [[ -z $tagged || $tagged == "$DIGEST" ]] \
        || die "On this computer the stream tag $STREAM_TAG names another image (ID $tagged), not the pinned digest. Remove that tag (docker image rm $STREAM_TAG), then run this script again. Nothing was changed."
    MAC_HAS+=("$here") MAC_TAGGED+=("$tagged")
    i=$((i + 1))
done

# The server: ssh and sudo -n docker, its image store, and the images it has.
out=$(remote "sudo -n docker version --format '{{.Server.APIVersion}}'" 2>&1) \
    || die "ssh $HOST (batch mode) with sudo -n docker failed: $(printf '%s\n' "$out" | tail -n 1). ssh must log in with a key and no question, and the user must run docker with sudo -n."
store=$(remote "sudo -n docker info --format '{{json .DriverStatus}}'" 2>&1) \
    || die "docker info failed on $HOST: $(printf '%s\n' "$store" | tail -n 1). Nothing was changed."
if [[ $store == *"$CONTAINERD"* ]]; then SERVER_STORE=containerd; else SERVER_STORE=classic; fi
missing=0
i=0
while ((i < ${#IMAGES[@]})); do
    pin_parts "${IMAGES[i]}"
    if server_has_pin; then
        SERVER_HAS+=(1)
        printf 'The pinned image is already on %s (%s image store): %s. Nothing to stream.\n' "$HOST" "$SERVER_STORE" "$PINNED"
    else
        SERVER_HAS+=(0)
        missing=$((missing + 1))
        [[ $SERVER_STORE == containerd ]] || die "The Docker engine of $HOST uses the classic image store: there an image from docker load has no registry digest, and the --no-pull runs do not use it ($PINNED is not there). Switch that engine to the containerd image store first (README.md, \"Install from streamed images\"). Nothing was changed."
    fi
    i=$((i + 1))
done
if ((missing && FILE_MODE)); then
    out=$(remote 'command -v rsync && command -v sha256sum' 2>&1) \
        || die "--file needs rsync and sha256sum on $HOST, and it has not both. Install them there, or stream without --file. Nothing was changed."
fi

if ((DRY_RUN)); then
    i=0
    while ((i < ${#IMAGES[@]})); do
        pin_parts "${IMAGES[i]}"
        if ((SERVER_HAS[i] == 0)); then
            if ((MAC_HAS[i])); then printf 'On this computer: %s\n' "$PINNED"; else printf 'Not on this computer: %s\n' "$PINNED"; fi
            printf 'Not on %s. Dry run: nothing was changed. The steps:\n' "$HOST"
            ((MAC_HAS[i])) || printf '  docker pull --platform %s %s\n' "${PLATFORMS[i]}" "${IMAGES[i]}"
            [[ -n ${MAC_TAGGED[i]} ]] || printf '  docker tag %s %s\n' "$PINNED" "$STREAM_TAG"
            if ((FILE_MODE)); then
                printf '  docker save -o ARCHIVE %s, shasum -a 256 ARCHIVE (ARCHIVE: %s/%s, kept until the check passes)\n' \
                    "$STREAM_TAG" "${TMPDIR:-/tmp}" "stream-image-archives/$(archive_name)"
                printf '  rsync --partial ARCHIVE %s:%s/%s, then sha256sum there\n' "$HOST" "$REMOTE_DIR" "$(archive_name)"
                printf "  ssh %s 'sudo -n docker load -i %s/%s'\n" "$HOST" "$REMOTE_DIR" "$(archive_name)"
            else
                printf "  docker save %s | ssh %s 'sudo -n docker load'\n" "$STREAM_TAG" "$HOST"
            fi
            printf '  the check of %s on %s\n' "$PINNED" "$HOST"
        fi
        i=$((i + 1))
    done
    exit 0
fi

((FILE_MODE == 0 || missing == 0)) || archive_dir
i=0
while ((i < ${#IMAGES[@]})); do
    pin_parts "${IMAGES[i]}"
    if ((SERVER_HAS[i] == 0)); then
        if ((MAC_HAS[i] == 0)); then
            printf 'Pulling the pinned image on this computer: %s\n' "${IMAGES[i]}"
            docker pull --platform "${PLATFORMS[i]}" "${IMAGES[i]}" \
                || die "The pull failed on this computer (above). When the registry wants a login, run docker login $REGISTRY on this computer first."
            id=$(docker image inspect --format '{{.Id}}' "$PINNED" 2> /dev/null) || id=''
            [[ $id == "$DIGEST" ]] || die "After the pull, $PINNED on this computer does not have the pinned digest as its ID (ID: ${id:-none}). Nothing was streamed."
            streamable "${PLATFORMS[i]}"
        fi
        if [[ -z ${MAC_TAGGED[i]} ]]; then
            docker tag "$PINNED" "$STREAM_TAG" || die "docker tag $PINNED $STREAM_TAG failed on this computer (above). Nothing was streamed."
        fi
        if ((FILE_MODE)); then stream_file; else stream_pipe; fi
    fi
    i=$((i + 1))
done
next_steps
