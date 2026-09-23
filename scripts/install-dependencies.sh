#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

usage() {
    printf 'Usage: sudo bash %s --role app|db\n' "${0##*/}"
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
    usage
    exit 0
fi
[[ $# -eq 2 && $1 == --role ]] || { usage >&2; exit 1; }
role=$2
[[ $role == app || $role == db ]] || fail 'Choose --role app or --role db.'
[[ $EUID -eq 0 ]] || fail 'Run with sudo or as root.'
[[ -r /etc/os-release ]] || fail 'Cannot identify the operating system.'
# shellcheck source=/dev/null
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail 'Ubuntu 24.04 is required.'
[[ $(dpkg --print-architecture) == amd64 ]] || fail 'This installer supports amd64.'

trap 'printf "Installation failed at line %s.\n" "$LINENO" >&2' ERR
export DEBIAN_FRONTEND=noninteractive

update_packages() {
    apt-get update -o APT::Update::Error-Mode=any
}

install_packages() {
    apt-get install -y --no-install-recommends --no-upgrade --no-remove "$@"
}

if [[ $role == app ]]; then
    conflicts=()
    for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
        if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) == 'install ok installed' ]]; then
            conflicts+=("$package")
        fi
    done
    [[ ${#conflicts[@]} -eq 0 ]] || fail "Resolve conflicting packages first: ${conflicts[*]}. Nothing was removed."

    update_packages
    install_packages ca-certificates curl
    # awk must read to the end: exiting early makes apt-cache die of SIGPIPE, which
    # pipefail turns into a failure once Docker's repository is already configured.
    candidate=$(apt-cache policy docker-ce | awk '/Candidate:/ && !seen {print $2; seen=1}')
    if [[ -z $candidate || $candidate == '(none)' ]]; then
        # Preserve an administrator's existing source file rather than replacing it.
        [[ ! -e /etc/apt/sources.list.d/docker.sources && ! -e /etc/apt/sources.list.d/docker.list ]] || fail 'Docker source exists but has no package candidate. Check its configuration.'
        key_file=/etc/apt/keyrings/docker.asc
        [[ ! -e $key_file ]] || fail 'Docker key already exists without a usable repository. Check the APT configuration.'
        temp_key=$(mktemp)
        trap 'rm -f -- "$temp_key"' EXIT
        curl --fail --silent --show-error --location --retry 3 \
            https://download.docker.com/linux/ubuntu/gpg --output "$temp_key"
        install -d -m 0755 /etc/apt/keyrings
        install -m 0644 "$temp_key" "$key_file"
        printf '%s\n' \
            'Types: deb' \
            'URIs: https://download.docker.com/linux/ubuntu' \
            'Suites: noble' \
            'Components: stable' \
            'Architectures: amd64' \
            'Signed-By: /etc/apt/keyrings/docker.asc' \
            > /etc/apt/sources.list.d/docker.sources
        update_packages
    fi

    install_packages docker-ce docker-buildx-plugin docker-compose-plugin
    packages=(ca-certificates curl docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
    docker --version
    docker compose version
    docker buildx version
else
    update_packages
    # Native restore uses the client tools, trusted extensions, Python standard
    # library, HTTPS downloads, and source-database locales. No pip environment.
    # nano is for editing deploy.conf, the next step in the README.
    packages=(postgresql-16 postgresql-client-16 postgresql-contrib python3 curl ca-certificates locales nano)
    install_packages "${packages[@]}"
    /usr/lib/postgresql/16/bin/postgres --version
fi

for package in "${packages[@]}"; do
    [[ $(dpkg-query -W -f='${Status}' "$package") == 'install ok installed' ]] || fail "Package not fully installed: $package"
done
dpkg-query -W -f='${binary:Package}\t${Version}\n' "${packages[@]}"
printf '\nDependency installation complete (%s).\n' "$role"
