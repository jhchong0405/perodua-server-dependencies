#!/usr/bin/env bash
# --no-pull of deploy-app.sh, iss-api.sh and rp-adapter.sh, and stream-image.sh, on real
# Docker engines. Run it on the Mac (Docker Desktop or OrbStack on the containerd image
# store), from the kit repository:
#
#   bash tests/check-no-pull-engines.sh
#
# The servers are two throwaway docker:29-dind engines (linux/amd64): one on the
# containerd image store, one on the classic store. A throwaway registry:2 gives three
# tiny test images real digest pins (localhost:5055/kit-no-pull-check:a, :b and :c; a
# root file system with the busybox of the dind image), and two index pins that BuildKit
# on this computer builds from the same root file system with provenance: i (linux/amd64
# and its attestation manifest, the form of the kit pins of iss-api.sh, rp-adapter.sh and
# the Odoo images) and m (linux/amd64, linux/arm64 and their attestations). The classic
# engine shares the network of the registry, so a pull there reaches it under the same
# name. Copies of the kit scripts pin these images: iss-api.sh a, rp-adapter.sh b,
# deploy-app.sh a and b (the demo-uiux line b and a), and in a second copy iss-api.sh i.
# stream-image.sh runs on this computer with an ssh that runs each command in the engine
# of the server (docker exec, in /root); the --no-pull runs run in the engines. The test
# images have no Odoo, API or adapter: a run that passes the image check fails later, and
# a check reads only its image step. It prints PASS or FAIL for each check and exits 1
# when one fails. Every Docker resource is named kit-no-pull-check-* and removed at the
# end, with its volumes.
# Needs: docker:29-dind and registry:2 on this computer or from Docker Hub, docker buildx
# on this computer (it builds the index pins; its build cache keeps their small layers),
# and apk downloads in the engines (bash, coreutils, util-linux, iproute2, findutils,
# diffutils, curl, rsync).
set -Eeuo pipefail
export LC_ALL=C
KIT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DIND=${DIND_IMAGE:-docker:29-dind} REGISTRY_IMAGE=${REGISTRY_IMAGE:-registry:2}
NAME=kit-no-pull-check
REG=$NAME-registry CTD=$NAME-ctd CLASSIC=$NAME-classic
PORT=5055 REPO=localhost:5055/kit-no-pull-check
FACTS='{{.Id}}{{range .RepoDigests}} {{.}}{{end}}'
LAB=$(mktemp -d "${TMPDIR:-/tmp}/kit-no-pull-check.XXXXXX")
PASSED=0 FAILED=0 PIN_A='' PIN_B='' PIN_C='' PIN_I='' PIN_M=''

cleanup() {
    local ref
    docker rm -f -v "$CTD" "$CLASSIC" "$REG" > /dev/null 2>&1 || true
    for ref in $(docker image ls --format '{{.Repository}}:{{.Tag}}' "$REPO" 2> /dev/null); do
        docker image rm "$ref" > /dev/null 2>&1 || true
    done
    for ref in "$PIN_A" "$PIN_B" "$PIN_C" "$PIN_I" "$PIN_M"; do
        [[ -z $ref ]] || docker image rm "${ref%:*@*}@${ref##*@}" > /dev/null 2>&1 || true
    done
    rm -rf -- "$LAB"
}
trap cleanup EXIT
stop() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
pass() { PASSED=$((PASSED + 1)); printf 'PASS %s\n' "$*"; }
fail() { FAILED=$((FAILED + 1)); printf 'FAIL %s\n' "$*"; }
has() { [[ $1 == *"$2"* ]]; }   # has TEXT PART
lacks() { [[ $1 != *"$2"* ]]; }
absent() { ! docker exec "$1" test -e "$2"; }   # absent ENGINE PATH
facts() { docker exec "$1" docker image inspect --format "$FACTS" "$2" 2> /dev/null || true; }   # facts ENGINE REFERENCE
pinned() { printf '%s@%s' "${1%:*@*}" "${1##*@}"; }
stream_tag() { local digest=${1##*@}; printf '%s:%s' "${1%:*@*}" "${digest/:/-}"; }
run() {   # run ENGINE COMMAND...: OUT and CODE of a command in the engine, never stops this script
    local engine=$1
    shift
    CODE=0
    OUT=$(docker exec -w /root "$engine" "$@" 2>&1 < /dev/null) || CODE=$?
}
helper() {   # stream-image.sh of the kit copy, with the ssh of the check; OUT and CODE
    CODE=0
    OUT=$(PATH="$LAB/bin:$PATH" TMPDIR="$LAB/tmp" /bin/bash "$LAB/kit/scripts/stream-image.sh" "$@" 2>&1 < /dev/null) || CODE=$?
}
pulls_since() {   # ENGINE SECONDS: the image pulls of the engine since then
    docker exec "$1" docker events --since "$2" --until "$(docker exec "$1" date +%s)" --filter type=image \
        --format '{{.Action}}' | grep -c '^pull$' || true
}
wait_engine() {
    local tries=90
    while ((tries-- > 0)); do
        docker exec "$1" docker info > /dev/null 2>&1 && return 0
        sleep 2
    done
    docker logs --tail 30 "$1" >&2 || true
    stop "The engine $1 did not start"
}

# ---------------------------------------------------------------- the lab
for name in "$REG" "$CTD" "$CLASSIC"; do
    ! docker inspect "$name" > /dev/null 2>&1 || stop "$name already exists; remove it first"
done
[[ $(docker info --format '{{json .DriverStatus}}') == *io.containerd.snapshotter.v1* ]] \
    || stop 'Docker on this computer must use the containerd image store'
mkdir -p "$LAB/bin" "$LAB/tmp" "$LAB/kit" "$LAB/lab"
printf '{"features":{"containerd-snapshotter":true}}\n' > "$LAB/daemon-ctd.json"
printf '{"features":{"containerd-snapshotter":false}}\n' > "$LAB/daemon-classic.json"
docker run -d --name "$REG" -e "REGISTRY_HTTP_ADDR=0.0.0.0:$PORT" -p "127.0.0.1:$PORT:$PORT" "$REGISTRY_IMAGE" > /dev/null
docker run -d --privileged --platform linux/amd64 --name "$CTD" \
    --mount "type=bind,source=$LAB/daemon-ctd.json,target=/etc/docker/daemon.json,readonly" "$DIND" > /dev/null
docker run -d --privileged --platform linux/amd64 --name "$CLASSIC" --network "container:$REG" \
    --mount "type=bind,source=$LAB/daemon-classic.json,target=/etc/docker/daemon.json,readonly" "$DIND" > /dev/null
wait_engine "$CTD"
wait_engine "$CLASSIC"
for engine in "$CTD" "$CLASSIC"; do
    docker exec "$engine" apk add --no-cache bash coreutils util-linux iproute2 findutils diffutils curl rsync > /dev/null
    # sudo -n of the server: the engines run as root.
    docker exec "$engine" sh -c 'printf "%s\n" "#!/bin/sh" "[ \"\$1\" = -n ] && shift" "exec \"\$@\"" > /usr/local/bin/sudo && chmod 0755 /usr/local/bin/sudo'
done
if [[ $(docker exec "$CTD" docker info --format "{{json .DriverStatus}}") == *io.containerd.snapshotter.v1* && $(docker exec "$CLASSIC" docker info --format "{{json .DriverStatus}}") != *io.containerd.snapshotter.v1* ]]; then
    pass 'the engines: one containerd image store, one classic store'
else
    fail 'the engines: one containerd image store, one classic store'
fi

# The test images: busybox and its musl from the dind image (linux/amd64), and a word that
# gives each image its own digest. The registry gives the digest pin.
for word in a b c; do
    docker exec "$CTD" sh -c "rm -rf /tmp/rootfs && mkdir -p /tmp/rootfs/bin /tmp/rootfs/lib /tmp/rootfs/etc \
        && cp /bin/busybox /tmp/rootfs/bin/ && ln -s busybox /tmp/rootfs/bin/sh && cp -a /lib/*musl* /tmp/rootfs/lib/ \
        && echo $word > /tmp/rootfs/etc/kit-no-pull-check && tar -C /tmp/rootfs -cf - ." \
        | docker import --platform linux/amd64 - "$REPO:$word" > /dev/null
    docker push -q "$REPO:$word" > /dev/null
    pin=$REPO:$word@$(docker image inspect --format '{{.Id}}' "$REPO:$word")
    docker run --rm --pull never --network none --platform linux/amd64 --entrypoint sh "$REPO:$word" -c : \
        || stop "The test image $REPO:$word does not start on this computer"
    docker image rm "$REPO:$word" > /dev/null
    docker pull -q --platform linux/amd64 "$pin" > /dev/null || stop "The registry does not give $pin"
    docker image rm "$(pinned "$pin")" > /dev/null   # the helper pulls it
    case $word in a) PIN_A=$pin ;; b) PIN_B=$pin ;; c) PIN_C=$pin ;; esac
done
# The index pins: built and pushed by BuildKit on this computer (docker driver, containerd
# image store), then removed here, so that the helper pulls them. Their entries come from
# the registry.
mkdir -p "$LAB/index"
docker exec "$CTD" sh -c "rm -rf /tmp/rootfs && mkdir -p /tmp/rootfs/bin /tmp/rootfs/lib /tmp/rootfs/etc \
    && cp /bin/busybox /tmp/rootfs/bin/ && ln -s busybox /tmp/rootfs/bin/sh && cp -a /lib/*musl* /tmp/rootfs/lib/ \
    && echo i > /tmp/rootfs/etc/kit-no-pull-check && tar -C /tmp/rootfs -cf - ." > "$LAB/index/rootfs.tar"
printf 'FROM scratch\nADD rootfs.tar /\n' > "$LAB/index/Dockerfile"
ENTRY_PLATFORMS='{{range .Manifest.Manifests}}{{with .Platform}}{{.OS}}/{{.Architecture}}{{end}} {{index .Annotations "vnd.docker.reference.type"}};{{end}}'
for word in i m; do
    platforms=linux/amd64
    [[ $word == i ]] || platforms=linux/amd64,linux/arm64
    docker buildx build -q --platform "$platforms" --provenance=mode=min -t "$REPO:$word" --push "$LAB/index" > /dev/null \
        || stop "docker buildx build --push of the index test image $REPO:$word failed"
    pin=$REPO:$word@sha256:$(docker buildx imagetools inspect --raw "$REPO:$word" | shasum -a 256 | cut -d' ' -f1)
    entries=$(docker buildx imagetools inspect --format "$ENTRY_PLATFORMS" "$pin")
    case $word in
        i) [[ $entries == 'linux/amd64 ;unknown/unknown attestation-manifest;' ]] || stop "The index $pin holds: $entries"
           PIN_I=$pin ;;
        m) [[ $entries == 'linux/amd64 ;linux/arm64 ;unknown/unknown attestation-manifest;unknown/unknown attestation-manifest;' ]] \
               || stop "The index $pin holds: $entries"
           PIN_M=$pin ;;
    esac
    docker image rm "$REPO:$word" > /dev/null 2>&1 || true
    docker image rm "$(pinned "$pin")" > /dev/null 2>&1 || true   # the helper pulls it
done
printf 'Pins: %s %s %s %s %s\n' "$PIN_A" "$PIN_B" "$PIN_C" "$PIN_I" "$PIN_M"

# The kit with the test pins, on this computer and in the engines.
cp -R "$KIT/scripts" "$LAB/kit/"
repin() {   # FILE KEY=VALUE...: the one line ^KEY= of FILE gets VALUE (a comment after it stays)
    local file=$1 pair
    shift
    for pair in "$@"; do
        [[ $(grep -c "^${pair%%=*}=" "$file") == 1 ]] || stop "$file must hold one ${pair%%=*}= line"
        sed "s#^${pair%%=*}=[^ ]*#$pair#" "$file" > "$file.new" && mv "$file.new" "$file"
    done
}
repin "$LAB/kit/scripts/iss-api.sh" "IMAGE=$PIN_A"
repin "$LAB/kit/scripts/rp-adapter.sh" "IMAGE=$PIN_B"
repin "$LAB/kit/scripts/deploy-app.sh" "ODOO_IMAGE=$PIN_A" "WEB_IMAGE=$PIN_B" "DEMO_ODOO_IMAGE=$PIN_B" "DEMO_WEB_IMAGE=$PIN_A"
mkdir -p "$LAB/kit-index"
cp -R "$LAB/kit/scripts" "$LAB/kit-index/"
repin "$LAB/kit-index/scripts/iss-api.sh" "IMAGE=$PIN_I"
printf 'made-up-password\n' > "$LAB/lab/db-password"
printf 'DB_HOST=db.example.internal\nDB_SERVICE=ORCLPDB1\nDB_USER=APIUSR\nDB_PASSWORD_FILE=/lab/db-password\n' > "$LAB/lab/iss.conf"
printf 'DB_HOST=10.1.119.26\nDB_SERVICE=ENTPP2\nDB_USER=RP_GATEWAY\nDB_PASSWORD_FILE=/lab/db-password\n' > "$LAB/lab/rp.conf"
printf 'DB_HOST=192.0.2.20\nDB_PASSWORD_FILE=/lab/db-password\nBIND_IP=127.0.0.1\nHTTP_PORT=18110\nPROJECT_NAME=kit-no-pull-check\n' \
    > "$LAB/lab/app.env"
for engine in "$CTD" "$CLASSIC"; do
    docker exec "$engine" mkdir -p /kit /kit-index
    docker cp "$LAB/kit/scripts" "$engine:/kit/" > /dev/null
    docker cp "$LAB/kit-index/scripts" "$engine:/kit-index/" > /dev/null
    docker cp "$LAB/lab" "$engine:/" > /dev/null
done
# ssh OPTIONS [-n] HOST COMMAND... of the helper: COMMAND in the engine of HOST, in /root. With
# LAB_BREAK_AFTER=BYTES, the data of an rsync stops after BYTES: a link that breaks.
cat > "$LAB/bin/ssh" <<SSH
#!/bin/bash
if [ "\$1" = -G ]; then host=\${!#}; printf 'user root\nhostname %s\nport 22\n' "\${host#*@}"; exit 0; fi
for want in -T -a -x -o BatchMode=yes -o ConnectTimeout=20 -o ClearAllForwardings=yes -o PermitLocalCommand=no \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=4; do
    [ "\$1" = "\$want" ] || { echo "lab ssh: unexpected option \$1 (want \$want)" >&2; exit 255; }
    shift
done
n=0
if [ "\$1" = -n ]; then n=1; shift; fi
host=\${1#*@}
shift
case \$host in
    lab-ctd) engine=$CTD ;;
    lab-classic) engine=$CLASSIC ;;
    *) echo "ssh: Could not resolve hostname \$host" >&2; exit 255 ;;
esac
if [ -n "\${LAB_BREAK_AFTER:-}" ] && [ "\${1:-}" = rsync ]; then
    head -c "\$LAB_BREAK_AFTER" | docker exec -i -w /root "\$engine" sh -c "\$*"
    exit 255
fi
if [ \$n = 1 ]; then exec docker exec -w /root "\$engine" sh -c "\$*" < /dev/null; fi
exec docker exec -i -w /root "\$engine" sh -c "\$*"
SSH
chmod 0755 "$LAB/bin/ssh"

# ---------------------------------------------------------------- containerd store: missing
run "$CTD" bash /kit/scripts/iss-api.sh install --no-pull --non-interactive --config /lab/iss.conf --dir /tmp/iss
if ((CODE == 1)) && has "$OUT" "--no-pull does not pull it: $(pinned "$PIN_A")" && has "$OUT" "bash scripts/stream-image.sh SSH_HOST iss-api.sh" && has "$OUT" "docker save $(stream_tag "$PIN_A")" && absent "$CTD" /tmp/iss; then
    pass 'iss-api.sh install --no-pull, image missing: exit 1, the stream command, no directory'
else
    fail 'iss-api.sh install --no-pull, image missing: exit 1, the stream command, no directory'
fi
run "$CTD" bash /kit/scripts/rp-adapter.sh install --no-pull --non-interactive --config /lab/rp.conf --dir /tmp/rp
if ((CODE == 3)) && has "$OUT" "--no-pull does not pull it: $(pinned "$PIN_B")" && absent "$CTD" /tmp/rp; then
    pass 'rp-adapter.sh install --no-pull, image missing: exit 3, the stream command, no directory'
else
    fail 'rp-adapter.sh install --no-pull, image missing: exit 3, the stream command, no directory'
fi
run "$CTD" bash /kit/scripts/deploy-app.sh --no-pull --non-interactive --config /lab/app.env --dir /tmp/app
if ((CODE == 1)) && has "$OUT" "does not pull it: $(pinned "$PIN_A")" && has "$OUT" "does not pull it: $(pinned "$PIN_B")" && has "$OUT" "deploy-app.sh --line client-stable-uiux" && absent "$CTD" /tmp/app; then
    pass 'deploy-app.sh --no-pull, both images missing: exit 1, both named, no directory'
else
    fail 'deploy-app.sh --no-pull, both images missing: exit 1, both named, no directory'
fi

# ---------------------------------------------------------------- the helper, then the image step
helper --dry-run lab-ctd deploy-app.sh iss-api.sh rp-adapter.sh
if ((CODE == 0)) && has "$OUT" "docker pull --platform linux/amd64 $PIN_A" && [[ -z $(facts "$CTD" "$(pinned "$PIN_A")") ]]; then
    pass 'stream-image.sh --dry-run: the steps, nothing loaded'
else
    fail 'stream-image.sh --dry-run: the steps, nothing loaded'
fi
helper lab-ctd deploy-app.sh iss-api.sh rp-adapter.sh
if ((CODE == 0)) && [[ $(grep -c "^Streaming " <<< "$OUT") == 2 ]] && has "$OUT" "Next, on lab-ctd: sudo bash iss-api.sh install --no-pull --non-interactive"; then
    pass 'stream-image.sh: pulls a and b on this computer and streams them once each'
else
    fail 'stream-image.sh: pulls a and b on this computer and streams them once each'
fi
for pin in "$PIN_A" "$PIN_B"; do
    if [[ $(facts "$CTD" "$(pinned "$pin")") == "${pin##*@}"*" $(pinned "$pin")"* ]]; then
        pass "on the containerd engine: $(pinned "$pin") has the pinned digest as ID and registry digest"
    else
        fail "on the containerd engine: $(pinned "$pin") has the pinned digest as ID and registry digest"
    fi
done
since=$(docker exec "$CTD" date +%s)
run "$CTD" bash /kit/scripts/iss-api.sh install --no-pull --non-interactive --config /lab/iss.conf --dir /tmp/iss
if has "$OUT" "Pinned image on this server (containerd image store), not pulled: $(pinned "$PIN_A")"; then
    pass 'iss-api.sh install --no-pull: the image is checked, started and used, not pulled'
else
    fail 'iss-api.sh install --no-pull: the image is checked, started and used, not pulled'
fi
run "$CTD" bash /kit/scripts/iss-api.sh status --dir /tmp/iss
if has "$OUT" 'Local:    the pinned image is on this server (containerd image store)'; then
    pass 'iss-api.sh status: Local: the pinned image is on this server'
else
    fail 'iss-api.sh status: Local: the pinned image is on this server'
fi
run "$CTD" bash /kit/scripts/rp-adapter.sh install --no-pull --non-interactive --config /lab/rp.conf --dir /tmp/rp
if has "$OUT" "Pinned image on this server (containerd image store), not pulled: $(pinned "$PIN_B")"; then
    pass 'rp-adapter.sh install --no-pull: the image is checked, started and used, not pulled'
else
    fail 'rp-adapter.sh install --no-pull: the image is checked, started and used, not pulled'
fi
run "$CTD" bash /kit/scripts/deploy-app.sh --no-pull --non-interactive --config /lab/app.env --dir /tmp/app
if has "$OUT" "not pulled: $(pinned "$PIN_A")" && has "$OUT" "not pulled: $(pinned "$PIN_B")" && lacks "$OUT" "pull access denied" && lacks "$OUT" "Pulling"; then
    pass 'deploy-app.sh --no-pull: both images checked and started, not pulled; Compose ran with --pull never'
else
    fail 'deploy-app.sh --no-pull: both images checked and started, not pulled; Compose ran with --pull never'
fi
if [[ $(pulls_since "$CTD" "$since") == 0 ]]; then
    pass 'no image pull on the containerd engine during the --no-pull runs'
else
    fail 'no image pull on the containerd engine during the --no-pull runs'
fi
run "$CTD" bash /kit/scripts/deploy-app.sh --no-pull --non-interactive --config /lab/app.env --dir /tmp/app-demo --line demo-uiux
if has "$OUT" "not pulled: $(pinned "$PIN_B")" && has "$OUT" "not pulled: $(pinned "$PIN_A")"; then
    pass 'deploy-app.sh --no-pull --line demo-uiux: the images of that line'
else
    fail 'deploy-app.sh --no-pull --line demo-uiux: the images of that line'
fi

# A stream tag that names another image on this computer stops the helper before the server.
docker image rm "$(stream_tag "$PIN_A")" > /dev/null 2>&1 || true
docker tag "$(pinned "$PIN_B")" "$(stream_tag "$PIN_A")" || true
helper lab-ctd iss-api.sh
if ((CODE == 1)) && has "$OUT" "names another image"; then
    pass 'stream-image.sh: a stream tag of another image on this computer stops it'
else
    fail 'stream-image.sh: a stream tag of another image on this computer stops it'
fi
docker image rm "$(stream_tag "$PIN_A")" > /dev/null 2>&1 || true
helper --dry-run lab-ctd iss-api.sh deploy-app.sh --line demo-uiux
if ((CODE == 0)) && [[ $(grep -c "is already on lab-ctd (containerd image store)" <<< "$OUT") == 2 ]]; then
    pass 'stream-image.sh: an image on the server gets nothing'
else
    fail 'stream-image.sh: an image on the server gets nothing'
fi

# ---------------------------------------------------------------- the classic store
run "$CLASSIC" bash /kit/scripts/iss-api.sh install --no-pull --non-interactive --config /lab/iss.conf --dir /tmp/iss
if ((CODE == 1)) && has "$OUT" "uses the classic image store" && absent "$CLASSIC" /tmp/iss; then
    pass 'classic store, image missing: iss-api.sh stops with the classic message, no directory'
else
    fail 'classic store, image missing: iss-api.sh stops with the classic message, no directory'
fi
before=$(docker exec "$CLASSIC" docker image ls -q | wc -l)
helper lab-classic iss-api.sh
if ((CODE == 1)) && has "$OUT" "uses the classic image store" && [[ $(docker exec "$CLASSIC" docker image ls -q | wc -l) == "$before" ]]; then
    pass 'stream-image.sh to the classic store: refused, nothing loaded'
else
    fail 'stream-image.sh to the classic store: refused, nothing loaded'
fi
docker exec "$CLASSIC" docker pull -q "$PIN_A" > /dev/null || true
run "$CLASSIC" bash /kit/scripts/iss-api.sh install --no-pull --non-interactive --config /lab/iss.conf --dir /tmp/iss
if has "$OUT" "Pinned image on this server (classic image store), not pulled: $(pinned "$PIN_A")"; then
    pass 'classic store, image that a pull brought: iss-api.sh uses it'
else
    fail 'classic store, image that a pull brought: iss-api.sh uses it'
fi
helper lab-classic iss-api.sh
if ((CODE == 0)) && has "$OUT" "is already on lab-classic (classic image store)"; then
    pass 'stream-image.sh to the classic store with the pulled image: nothing to stream'
else
    fail 'stream-image.sh to the classic store with the pulled image: nothing to stream'
fi

# ---------------------------------------------------------------- --file: a break, then the resume
LAB_BREAK_AFTER=150000 helper --file lab-ctd --image "$PIN_C"
if ((CODE == 1)) && has "$OUT" "The copy to lab-ctd broke" && [[ -n $(ls "$LAB/tmp/stream-image-archives") ]]; then
    pass 'stream-image.sh --file, the link breaks: exit 1, the archive stays here'
else
    fail 'stream-image.sh --file, the link breaks: exit 1, the archive stays here'
fi
if [[ -n $(docker exec "$CTD" ls /root/stream-image) ]]; then
    pass 'stream-image.sh --file, the link breaks: the part stays on the server (rsync --partial)'
else
    fail 'stream-image.sh --file, the link breaks: the part stays on the server (rsync --partial)'
fi
helper --file lab-ctd --image "$PIN_C"
if ((CODE == 0)) && has "$OUT" "Resuming from the kept archive" && has "$OUT" "Same SHA-256 on lab-ctd" && [[ $(facts "$CTD" "$(pinned "$PIN_C")") == "${PIN_C##*@}"* ]]; then
    pass 'stream-image.sh --file, the next run: resumes from the kept archive, the same SHA-256, the check'
else
    fail 'stream-image.sh --file, the next run: resumes from the kept archive, the same SHA-256, the check'
fi
if [[ -z $(ls "$LAB/tmp/stream-image-archives") && -z $(docker exec "$CTD" ls /root/stream-image) ]]; then
    pass 'stream-image.sh --file: no archive left on either side'
else
    fail 'stream-image.sh --file: no archive left on either side'
fi
# busybox sh runs no applet by its bare name: cat goes through busybox.
run "$CTD" docker run --rm --pull never --network none --entrypoint sh "$PIN_C" -c 'busybox cat /etc/kit-no-pull-check'
if ((CODE == 0)) && [[ $OUT == c ]]; then
    pass 'the image of --file starts on the server'
else
    fail 'the image of --file starts on the server'
fi

# ---------------------------------------------------------------- index pins
# docker save without --platform writes the index itself: the server finds the pinned digest.
helper lab-ctd --image "$PIN_I"
if ((CODE == 0)) && has "$OUT" "The pinned image is an index of one linux/amd64 image and its attestation manifests: $(pinned "$PIN_I")" \
        && [[ $(facts "$CTD" "$(pinned "$PIN_I")") == "${PIN_I##*@} $(pinned "$PIN_I")"* ]]; then
    pass 'stream-image.sh, an index of linux/amd64 and its attestation: the pinned digest as ID and registry digest on the server'
else
    fail 'stream-image.sh, an index of linux/amd64 and its attestation: the pinned digest as ID and registry digest on the server'
fi
run "$CTD" docker image inspect --format '{{json .Descriptor}}' "$(pinned "$PIN_I")"
if ((CODE == 0)) && has "$OUT" '"mediaType":"application/vnd.oci.image.index.v1+json"'; then
    pass 'on the server the image of the index pin is the index'
else
    fail 'on the server the image of the index pin is the index'
fi
run "$CTD" docker run --rm --pull never --network none --entrypoint sh "$PIN_I" -c 'busybox cat /etc/kit-no-pull-check; busybox uname -m'
if ((CODE == 0)) && [[ $OUT == $'i\nx86_64' ]]; then
    pass 'the index pin starts on the server with --pull never (its linux/amd64 image)'
else
    fail 'the index pin starts on the server with --pull never (its linux/amd64 image)'
fi
since=$(docker exec "$CTD" date +%s)
run "$CTD" bash /kit-index/scripts/iss-api.sh install --no-pull --non-interactive --config /lab/iss.conf --dir /tmp/iss-index
if has "$OUT" "Pinned image on this server (containerd image store), not pulled: $(pinned "$PIN_I")" \
        && [[ $(pulls_since "$CTD" "$since") == 0 ]]; then
    pass 'iss-api.sh install --no-pull with an index pin: checked, started and used, not pulled'
else
    fail 'iss-api.sh install --no-pull with an index pin: checked, started and used, not pulled'
fi
helper lab-ctd --image "$PIN_M"
if ((CODE == 1)) && has "$OUT" "The pinned index $(pinned "$PIN_M") holds: linux/amd64, linux/arm64," \
        && [[ -z $(facts "$CTD" "$(pinned "$PIN_M")") ]]; then
    pass 'stream-image.sh, an index with another platform: refused after the pull, nothing loaded'
else
    fail 'stream-image.sh, an index with another platform: refused after the pull, nothing loaded'
fi

printf '%s passed, %s failed\n' "$PASSED" "$FAILED"
((FAILED == 0))
