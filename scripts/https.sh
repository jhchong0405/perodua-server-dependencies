#!/usr/bin/env bash
# HTTPS on this server: nginx terminates TLS for the host names in a routes table
# and forwards each path to a local port. The table drives everything: the
# certificate request (csr), the certificate check (install-cert) and the nginx
# configuration (apply). The certificate comes from an issuer (install-cert), or
# from Let's Encrypt for the same request (letsencrypt), renewed by a timer.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077
PATH=$PATH:/usr/local/sbin:/usr/sbin:/sbin   # cron, su and some sudo setups leave these out; nginx lives there

STATE_DIR=/etc/perodua-https
NGINX_CONF=/etc/nginx/conf.d/perodua-https.conf
DEFAULT_NGINX_CONF=$NGINX_CONF
API_RATE=10r/s API_BURST=20
SCRIPT_PATH=$(realpath -- "${BASH_SOURCE[0]}")
SCRIPT_DIR=${SCRIPT_PATH%/*}
# The subject GICT asks for in its certificate requests; the CN is the first host of the table.
DEFAULT_SUBJECT='/C=MY/ST=Selangor/L=Rawang/O=Perusahaan Otomobil Kedua Sdn Bhd/OU=GICT'
COMMAND='' ROUTES='' ROUTES_FILE='' SUBJECT=$DEFAULT_SUBJECT NEW_KEY=0 KEY_FILE='' CONFIRM='' PURGE=0 TEMP_DIR='' RELOADED=0
ARGS=() HOSTS=() R_HOST=() R_PATH=() R_PORT=() R_STRIP=() R_HTTP=() R_API=() SAVED=()
# In STATE_DIR: routes.conf (the table), key.pem (the private key nginx uses),
# key.new.pem (a new key waiting for its certificate), request.csr, fullchain.pem,
# and letsencrypt/ (see LE_DIR).
STATE_FILES=(key.pem key.new.pem request.csr fullchain.pem routes.conf)

# Let's Encrypt: lego (v5.5.2, pinned by digest) runs in Docker and answers the
# DNS-01 challenge through acme-dns: the DNS administrator points
# _acme-challenge.HOST once (CNAME) at an acme-dns account, and lego writes the
# challenge there. lego sees request.csr only, never the private key.
LEGO_IMAGE=goacme/lego@sha256:1944e8c36055beec47c7de6f15202b41128be75eea0ffa257f0c14d93c5155fd
# letsencrypt --manual: acme.sh v3.1.6, whose manual DNS mode prints every TXT record at once.
ACMESH_IMAGE=neilpang/acme.sh@sha256:34d0c9a75e0f5222b9bab885cbb3c0c01ae4414f4bebbc16bdbc2298140970ef
# The public DNS servers the manual records are checked with before Let's Encrypt is asked.
PUBLIC_RESOLVERS=8.8.8.8:53,1.1.1.1:53
DEFAULT_SERVER=https://acme-v02.api.letsencrypt.org/directory
DEFAULT_ACME_DNS=https://acmedns.novutal.com
RENEW_DAYS=30
SYSTEM_ROOTS=/etc/ssl/certs/ca-certificates.crt
# The renewal timer runs a copy of this script, which stays when the downloaded
# scripts folder moves; one Let's Encrypt setup per server, as one nginx file.
UNIT=perodua-https-renew
UNIT_DIR=/etc/systemd/system DEFAULT_UNIT_DIR=/etc/systemd/system
LIB_DIR=/usr/local/lib/perodua-https DEFAULT_LIB_DIR=/usr/local/lib/perodua-https
# In LE_DIR ($STATE_DIR/letsencrypt, 0700): settings (the options of the run that
# installed the certificate, for later runs and the timer), acme-dns.json (an acme-dns
# account for each host name, with its password: 0600), lego/ (lego's ACME account
# and the certificates it got), extra-root.pem (--extra-root) and installed (the
# fingerprint of the certificate from Let's Encrypt that was installed last), and
# acme-ca.pem (--acme-ca). Every file lego reads or writes is under LE_DIR, so
# the lego container mounts nothing but LE_DIR and request.csr.
# For a local end-to-end test against Pebble (the ACME test server), a local
# acme-dns and a test DNS server: --acme-ca (Pebble's HTTPS CA), --acme-dns with
# http://, and PERODUA_HTTPS_LEGO_NETWORK=NAME in the environment, which runs lego
# on that Docker network instead of the host's. None of these is needed with
# Let's Encrypt.
LE_DIR='' RENEW=0 MANUAL=0 NO_DNS_CHECK=0 ACCEPT_TOS=0 TRUST_FILE='' REQUEST_KEY='' LEGO_NETWORK=host LEGO_RESOLVERS='' LEGO_CHANGED_ACCOUNTS=0
LOOKUP='' RECORDS_BY='' PICK_FAILURE=''
SERVER='' ACME_DNS='' DNS_RESOLVERS='' EMAIL='' EXTRA_ROOT='' ACME_CA=''
ACCOUNT_HOSTS=()
declare -A GIVEN=() SETTINGS=() CNAME_STATE=() ACCOUNT=()
FRONT='' FRONT_HOST='' FRONT_PORT=443 ACCESS_LOG=/var/log/nginx/access.log ERROR_LOG=/var/log/nginx/error.log DIAG_GIVEN=0 PROBLEMS=0
DIAG_AGENT=perodua-https-diagnose SEP=$'\x1f' DIAG_RECENT=1800 ACCESS_STATE='' ERROR_STATE=''

usage() {
    cat <<HELP
Usage: sudo bash https.sh csr [--new-key | --key FILE] [--subject /C=MY/O=NAME]
       sudo bash https.sh install-cert CERT [CHAIN]
       sudo bash https.sh letsencrypt --manual [--accept-tos] [--dns-resolvers HOST[:PORT],...]
                          [--no-dns-check]
       sudo bash https.sh letsencrypt [--renew] [--accept-tos] [--email ADDRESS]
                          [--server URL] [--acme-dns URL]
                          [--dns-resolvers HOST[:PORT],...] [--extra-root FILE]
                          [--acme-ca FILE]
       sudo bash https.sh apply
       sudo bash https.sh status
       sudo bash https.sh diagnose [--front ADDRESS[:PORT]] [--access-log FILE]
                          [--error-log FILE]
       sudo bash https.sh uninstall [--purge] [--confirm yes]
Every command also takes --routes FILE (default /etc/perodua-https/routes.conf)
and --dir DIR (default /etc/perodua-https).

The routes table has one line per host name and path: HOST PATH PORT [OPTION],
with OPTION strip, http, api or a comma-separated list of them, like
strip,http,api. The first run creates it from https-routes.conf.example and
stops, so that it can be filled in.

csr           Makes the private key (RSA 2048, kept in --dir, never sent) and a
              certificate request for every host name of the table: the first
              one is the CN, all of them are subject alternative names. Send the
              request it prints to the certificate issuer. A later run reuses the
              key; --new-key makes a new one, used once its certificate is
              installed; --key FILE takes an existing key, for example one a
              request was already made with (an encrypted key asks for its pass
              phrase once, as nginx needs the key unencrypted).
install-cert  Installs the certificate the issuer returned (PEM, DER or PKCS #7,
              with its chain in the same file or in CHAIN). It must belong to this
              server's key, cover every host name of the table, be valid now and
              verify with its chain. If apply ran before, nginx is reloaded with
              it; if nginx does not then serve it, the previous one comes back.
letsencrypt   Gets the certificate for the request of csr (made first if there
              is none) from Let's Encrypt instead of an issuer, and installs it
              as install-cert does; it must also lead to a root this server
              trusts. Let's Encrypt checks each host name through the DNS
              record _acme-challenge.HOST, which the DNS administrator points
              once (CNAME) at an account on the acme-dns server. The first run
              makes these accounts, prints the records and stops (exit status
              3) without asking Let's Encrypt; run it again once they exist.
              Let's Encrypt is asked only when dig finds every record. After
              the first certificate, a daily timer (perodua-https-renew.timer)
              runs --renew. lego runs in Docker; the server needs outbound
              HTTPS to the ACME server, the acme-dns server and, for the first
              pull of lego, Docker Hub, and dig (bind9-dnsutils). Every run
              also reloads nginx if it does not serve the installed certificate
              (after apply), as after a stop in the middle of an installation.
              Each run gets a new certificate at once, except:
  --renew         only when fewer than 30 days are left, and only a certificate
                  this command installed. Also updates the timer's copy of this
                  script when run from another scripts folder.
  --accept-tos    accepts the ACME server's terms of service, which the first
                  run asks for (it prints their address).
  --email ADDRESS contact address for the ACME account, used when it is made.
  --server URL    the ACME directory, default Let's Encrypt; for a test, the
                  staging one: https://acme-staging-v02.api.letsencrypt.org/directory
  --acme-dns URL  the acme-dns server, default https://acmedns.novutal.com
                  (http:// only for a local test: it sends the passwords unencrypted).
  --dns-resolvers HOST[:PORT],...  the DNS servers that the record checks ask,
                  in this order, default this server's; lego gets the first one
                  that answered for every record. '' goes back to the default.
  --extra-root FILE  a root certificate to trust, besides this server's, only
                  when the new certificate is checked: for the staging roots,
                  which no system trusts. Kept only while --server stays the
                  same; '' removes it.
  --acme-ca FILE  the CA of a test ACME server's own HTTPS certificate, such as
                  Pebble's. Let's Encrypt, staging included, never needs it.
                  Kept, and removed with '', as --extra-root.
                  The options of the run that installed the certificate are
                  kept for later runs and the timer.
apply         Writes the nginx configuration: port 80 redirects to HTTPS, and on
              443 every host forwards its paths to 127.0.0.1:PORT, the path
              unchanged or, with strip, removed. Every other path answers 404.
              A route with http is also served on port 80, without the
              redirect and without encryption, for a TLS front (such as a
              WAF) that forwards the browsers' HTTPS to port 80. A route with
              api closes PATH docs, redoc and openapi.json (404), and takes at
              most ${API_RATE%r/s} requests a second from each caller address, for all api
              routes together, in bursts of $API_BURST (429 above that). On 443, other
              host names and the bare address get the connection closed.
              nginx is installed with apt-get if needed; nothing else, not even
              an nginx of another installation, may listen on port 80 or 443,
              and no other nginx file may be the default server of port 443.
              The change is kept only if nginx -t accepts it and nginx then runs
              it. Every PORT must be known and the certificate installed.
              Also updates the Let's Encrypt timer's copy of this script, if
              there is one, so that it reads the same table.
status        The certificate, Let's Encrypt, nginx and every route.
diagnose      Finds why browsers cannot open the host names, and changes
              nothing: no file, no nginx reload, no lock; it needs no
              certificate. It prints OK, PROBLEM and NOTE lines for: ports
              22, 80, 443 and each route PORT (ss); the nginx files that
              listen on 80 and 443, and other files that serve a host name
              of the table there (nginx -T, which is not run while the
              access or error log, or a log file that nginx -V names, is
              missing: nginx -T creates it); each host and path over HTTPS
              and HTTP on this server (port 80 redirects to https://, or
              with http serves the route as HTTPS does), and each host once
              through the front (a WAF or a load balancer) at the address
              the system resolver gives, or at --front, with its first route
              without http if it has one; the 301 answers in the access log,
              with redirect loops (5 or more answers within 10 seconds for
              one sender, request and user agent: a PROBLEM in the last 30
              minutes, a NOTE before); nginx workers that exited on a signal
              (error log). A front that sends HTTPS requests to port 80 gets
              the redirect to https:// back each time on a route without
              http: a redirect loop, which browsers show as
              ERR_TOO_MANY_REDIRECTS. Then the front must forward HTTPS to
              port 443, or the route needs http. Exit status 4 when it finds
              a problem.
  --front ADDRESS[:PORT]  checks every host name through the front at this
                  address, port 443 by default.
  --access-log FILE  nginx's access log, combined format, default
                  /var/log/nginx/access.log.
  --error-log FILE   nginx's error log, default /var/log/nginx/error.log.
uninstall     Removes the nginx configuration of this script and reloads nginx,
              and the Let's Encrypt renewal timer. --purge also deletes the key,
              certificate, request and routes, and the Let's Encrypt accounts
              and certificates. Type yes to confirm, or pass --confirm yes.
HELP
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

# A command saves the files it is about to change; until it keeps them, any exit
# (an error, a refusal, Ctrl-C) puts them back, and nginx on them if it was reloaded.
save() {
    local f
    for f; do
        rm -f -- "$f.saved"
        [[ ! -e $f ]] || cp -p -- "$f" "$f.saved"
        SAVED+=("$f")
    done
}
keep() {
    local f saved=("${SAVED[@]}")
    SAVED=() RELOADED=0   # first: an interruption from here on leaves the changed files
    for f in "${saved[@]}"; do rm -f -- "$f.saved"; done
}
cleanup() {
    local f
    set +e
    for f in "${SAVED[@]}"; do
        if [[ -e $f.saved ]]; then mv -f -- "$f.saved" "$f"; else rm -f -- "$f"; fi || warn "Could not put back $f"
    done
    if ((${#SAVED[@]} && RELOADED)) && nginx_running && nginx -t > /dev/null 2>&1; then reload_nginx; fi
    [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lock() {   # one for every command, outside --dir, so --purge cannot race with it
    local base=/run/lock
    [[ -d $base ]] || base=/run
    exec 9> "$base/perodua-https.lock"
    flock -n 9 || die 'Another https.sh command is running'
}

# ---------------------------------------------------------------- routes
load_routes() {   # HOST PATH PORT [OPTION] per line; '#' starts a comment
    local line host path port option extra n=0 i token seen tokens=()
    local fqdn='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' prefix='^/([A-Za-z0-9._~-]+/)*$' number='^[1-9][0-9]{0,4}$'
    local options='^(strip|http|api)(,(strip|http|api))*$'
    ROUTES_FILE=${ROUTES:-$STATE_DIR/routes.conf}
    if [[ ! -f $ROUTES_FILE ]]; then
        [[ -z $ROUTES ]] || die "No routes file $ROUTES_FILE"
        [[ $COMMAND != diagnose ]] || die "No routes file $ROUTES_FILE: diagnose checks the host names in it (pass --routes FILE)"
        mkdir -p -- "$STATE_DIR"   # 0700 when new; an existing directory keeps its mode
        install -m 0600 -- "$SCRIPT_DIR/https-routes.conf.example" "$ROUTES_FILE"
        die "Created $ROUTES_FILE: put this server's host names, paths and ports in it, then run the command again"
    fi
    while IFS= read -r line || [[ -n $line ]]; do
        n=$((n + 1))
        line=${line%$'\r'}
        line=${line%%#*}
        host='' path='' port='' option='' extra=''
        read -r host path port option extra <<< "$line" || true
        [[ -n $host ]] || continue
        host=${host,,}
        [[ $host =~ $fqdn ]] || die "$ROUTES_FILE:$n: $host is not a full host name"
        [[ $path =~ $prefix ]] || die "$ROUTES_FILE:$n: PATH must start and end with /, like /dev/"
        [[ $port == - ]] || { [[ $port =~ $number ]] && ((10#$port <= 65535)); } \
            || die "$ROUTES_FILE:$n: PORT must be 1-65535, or - while it is not known"
        IFS=, read -ra tokens <<< "$option"
        seen=' '
        for token in "${tokens[@]}"; do
            [[ $option =~ $options && $seen != *" $token "* ]] \
                || die "$ROUTES_FILE:$n: OPTION must be strip, http, api or a comma-separated list of them, like strip,http,api"
            seen+="$token "
        done
        [[ -z $extra ]] || die "$ROUTES_FILE:$n: too many columns"
        for ((i = 0; i < ${#R_HOST[@]}; i++)); do
            [[ ${R_HOST[i]} != "$host" || ${R_PATH[i]} != "$path" ]] || die "$ROUTES_FILE:$n: $host $path is listed twice"
        done
        R_HOST+=("$host") R_PATH+=("$path") R_PORT+=("$port")
        if [[ ,$option, == *,strip,* ]]; then R_STRIP+=(strip); else R_STRIP+=(''); fi
        if [[ ,$option, == *,http,* ]]; then R_HTTP+=(http); else R_HTTP+=(''); fi
        if [[ ,$option, == *,api,* ]]; then R_API+=(api); else R_API+=(''); fi
        [[ " ${HOSTS[*]} " == *" $host "* ]] || HOSTS+=("$host")
    done < "$ROUTES_FILE"
    ((${#HOSTS[@]})) || die "$ROUTES_FILE lists no host name"
}

# ---------------------------------------------------------------- keys and certificates
pub_of() {   # $1 key, req or cert, $2 file: SHA-256 of its public key, nothing if it has none
    local out
    if [[ $1 == key ]]; then
        out=$(openssl pkey -in "$2" -passin pass: -pubout 2> /dev/null) || return 0   # never asks for a pass phrase
    elif [[ $1 == req ]]; then
        out=$(openssl req -in "$2" -noout -pubkey 2> /dev/null) || return 0
    else
        out=$(openssl x509 -in "$2" -noout -pubkey 2> /dev/null) || return 0
    fi
    printf '%s\n' "$out" | sha256sum | cut -d' ' -f1
}
cert_names() {   # the DNS names of a certificate, lower case, one per line
    { openssl x509 -in "$1" -noout -ext subjectAltName 2> /dev/null || true; } \
        | tr ',' '\n' | sed -n 's/^[[:space:]]*DNS://p' | tr '[:upper:]' '[:lower:]'
}
missing_hosts() {   # $1 certificate: the table's host names it does not cover
    local names host
    names=$(cert_names "$1")
    for host in "${HOSTS[@]}"; do
        if ! grep -qxF -- "$host" <<< "$names" && ! grep -qxF -- "*.${host#*.}" <<< "$names"; then
            printf '%s ' "$host"
        fi
    done
}
field() { openssl x509 -in "$2" -noout "-$1" -nameopt RFC2253 | cut -d= -f2-; }   # subject, issuer, startdate, enddate
days_left() { echo $(( ($(date -d "$(field enddate "$1")" +%s) - $(date +%s)) / 86400 )); }
fingerprint() { openssl x509 -in "$1" -noout -fingerprint -sha256 2> /dev/null | cut -d= -f2; }   # of the first certificate
request_names() {   # the DNS names of a certificate request, lower case, sorted, one per line
    { openssl req -in "$1" -noout -text 2> /dev/null || true; } | grep -o 'DNS:[^,[:space:]]*' | cut -c5- \
        | tr '[:upper:]' '[:lower:]' | sort -u
}

make_key() {   # $1: a new RSA 2048 private key
    if ! openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$1.tmp" 2> /dev/null; then
        rm -f -- "$1.tmp"
        die 'Could not make the private key'
    fi
    chmod 0600 -- "$1.tmp"
    mv -f -- "$1.tmp" "$1"
}
next_key() {   # where a new key goes: key.pem for the first one, else key.new.pem until its certificate comes
    if [[ ! -f $STATE_DIR/key.pem ]]; then
        printf '%s\n' "$STATE_DIR/key.pem"
        return
    fi
    [[ ! -f $STATE_DIR/key.new.pem ]] \
        || warn 'This replaces the new key that waited for its certificate: a request made with that key can no longer be installed'
    printf '%s\n' "$STATE_DIR/key.new.pem"
}

collect_certs() {   # every certificate in the given files, one PEM file each in $TEMP_DIR/certs
    local file pem=$TEMP_DIR/all.pem begin='-----BEGIN CERTIFICATE-----' end='-----END CERTIFICATE-----'
    : > "$pem"
    for file in "$@"; do
        [[ -f $file ]] || die "No such file: $file"
        if grep -q -- "$begin" "$file" && ! openssl x509 -in "$file" -noout 2> /dev/null; then
            # Microsoft CAs label a PKCS #7 chain CERTIFICATE: read it as PKCS #7
            sed 's/^-----\(BEGIN\|END\) CERTIFICATE-----/-----\1 PKCS7-----/' "$file" > "$TEMP_DIR/relabelled.pem"
            file=$TEMP_DIR/relabelled.pem
        fi
        if grep -q -- "$begin" "$file"; then
            sed -n "/$begin/,/$end/p" "$file" >> "$pem"
        elif grep -q -- '-----BEGIN PKCS7-----' "$file"; then
            openssl pkcs7 -in "$file" -print_certs 2> /dev/null | sed -n "/$begin/,/$end/p" >> "$pem"
        elif openssl x509 -inform DER -in "$file" -noout 2> /dev/null; then
            openssl x509 -inform DER -in "$file" >> "$pem"
        elif openssl pkcs7 -inform DER -in "$file" -print_certs > "$TEMP_DIR/p7.pem" 2> /dev/null; then
            sed -n "/$begin/,/$end/p" "$TEMP_DIR/p7.pem" >> "$pem"
        else
            die "$file holds no certificate this script can read (PEM, DER or PKCS #7)"
        fi
    done
    mkdir -p "$TEMP_DIR/certs"
    awk -v dir="$TEMP_DIR/certs" -v begin="$begin" 'index($0, begin) { n++ } n { print > (dir "/" n ".pem") }' "$pem"
    [[ -n $(ls -A "$TEMP_DIR/certs") ]] || die 'No certificate found in the given files'
}

signers() {   # $1 certificate: the given certificates named as its issuer, unexpired ones first, one per line
    local issuer cert expired=()
    issuer=$(field issuer "$1")
    for cert in "$TEMP_DIR"/certs/*.pem; do
        [[ $(field subject "$cert") == "$issuer" ]] || continue
        if openssl x509 -in "$cert" -noout -checkend 0 > /dev/null; then printf '%s\n' "$cert"; else expired+=("$cert"); fi
    done
    ((${#expired[@]} == 0)) || printf '%s\n' "${expired[@]}"
}

chain_of() {   # $1 leaf: the certificates to serve after it, in issuing order, one file per line
    local current=$1 cert next seen=" $1 " n=0
    while ((n++ < 10)); do
        next=''
        while IFS= read -r cert; do
            [[ $seen != *" $cert "* ]] || continue
            # A self-signed root is the clients' own; a root cross-signed by an
            # older one is served, for clients that trust only the older one.
            [[ $(field issuer "$cert") != "$(field subject "$cert")" ]] || continue
            openssl x509 -in "$cert" -noout -checkend 0 > /dev/null || continue   # expired: of use to no client
            next=$cert
            break
        done < <(signers "$current")
        [[ -n $next ]] || return 0
        printf '%s\n' "$next"
        seen+="$next "
        current=$next
    done
}

# ---------------------------------------------------------------- nginx
ensure_nginx() {
    command -v nginx > /dev/null && return 0
    command -v apt-get > /dev/null \
        || die 'nginx is not installed, and apply installs it with apt-get (Ubuntu), which this server does not have: install nginx so that "nginx" runs from the PATH, then run apply again'
    printf 'Installing nginx with apt-get...\n'
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1   # never restart other services
    # 9>&-: a service the package starts must not inherit the lock and hold it
    if ! { apt-get install -y -q nginx || { apt-get update -q && apt-get install -y -q nginx; }; } > "$TEMP_DIR/apt.log" 2>&1 9>&-; then
        cat "$TEMP_DIR/apt.log" >&2
        die 'Could not install nginx with apt-get (above). A server without internet access needs an apt proxy or a local package mirror'
    fi
}

nginx_running() {
    if [[ -d /run/systemd/system ]]; then
        systemctl is-active -q nginx
    else
        [[ -f /run/nginx.pid ]] && kill -0 "$(cat /run/nginx.pid)" 2> /dev/null
    fi
}

reload_nginx() {   # starts nginx when it does not run yet
    if [[ -d /run/systemd/system ]]; then
        systemctl enable -q nginx
        if systemctl is-active -q nginx; then systemctl reload nginx; else systemctl start nginx; fi
    elif nginx_running; then
        nginx -s reload
    else
        nginx 9>&-   # the daemon must not inherit the lock and hold it after this script ends
    fi
}

applied() {   # whether NGINX_CONF is the configuration apply wrote for this --dir
    grep -qsxF -- "    ssl_certificate_key $STATE_DIR/key.pem;" "$NGINX_CONF"
}

# nginx reloads in the background, and keeps its old configuration when it cannot
# take the new one (a port another program holds, even in another file). So after
# a reload the script asks nginx what it runs: the generation of this script's
# configuration (a hash, answered on /.perodua-https to 127.0.0.1 only) and the
# certificate chain it presents for each host name.
conf_generation() { sed -n '/return 200 "generation /{s/.*"generation \([0-9a-f]*\)".*/\1/p;q}' "$NGINX_CONF"; }
probe() {   # $1 host name: nginx's whole answer to /.perodua-https; fails without one (refused, timed out)
    printf 'GET /.perodua-https HTTP/1.0\r\nHost: %s\r\n\r\n' "$1" \
        | timeout 2 openssl s_client -quiet -connect 127.0.0.1:443 -servername "$1" 2> /dev/null
}
running_generation() { probe "$1" | sed -n 's/^generation \([0-9a-f]*\)$/\1/p'; }   # $1 host name
chain_hash() { sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' | sha256sum; }
active() {   # whether nginx runs NGINX_CONF with the installed chain for every host name
    local want host
    [[ $(running_generation "${HOSTS[0]}") == "$(conf_generation)" ]] || return 1
    want=$(chain_hash < "$STATE_DIR/fullchain.pem")
    for host in "${HOSTS[@]}"; do
        [[ $(timeout 2 openssl s_client -showcerts -connect 127.0.0.1:443 -servername "$host" < /dev/null 2> /dev/null \
            | chain_hash) == "$want" ]] || return 1
    done
}
dropped() {   # $1 host name, $2 generation: nginx answered without it, or nothing listens on port 443 any more
    local answer
    if answer=$(probe "$1") && [[ $answer == HTTP/* ]]; then
        [[ $answer != *"generation $2"* ]]
    else   # no answer proves nothing while something listens
        [[ -z $(ss -ltnH 'sport = :443' 2> /dev/null) ]]
    fi
}
within_10s() {   # runs the check "$@" until it passes, for about 10 seconds at most
    local deadline=$((SECONDS + 10))
    until "$@"; do
        ((SECONDS < deadline)) || return 1
        sleep 0.5
    done
}

activate() {   # nginx takes the changed files; if it does not, the command fails and they come back
    if ! nginx -t > "$TEMP_DIR/nginx-t.log" 2>&1; then
        cat "$TEMP_DIR/nginx-t.log" >&2
        die 'nginx -t refused the change (above); the previous files are back'
    fi
    RELOADED=1
    reload_nginx
    within_10s active \
        || die 'nginx did not take the change within about 10 seconds (see: sudo journalctl -u nginx, /var/log/nginx/error.log); the previous files are back'
}

port_takers() {   # what else listens on port 80 or 443, as PORT: PROGRAM (ITS FILE)
    # With no nginx on the PATH, a listening nginx is another installation too:
    # apply would install a second nginx from apt next to it.
    # Processes by their pid: ss prints their names unescaped, quotes included.
    local port name pid skip=''
    if command -v nginx > /dev/null; then skip=nginx; fi
    for port in 80 443; do
        ss -ltnpH "sport = :$port" 2> /dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | while IFS= read -r pid; do
            name=$(cat "/proc/$pid/comm" 2> /dev/null) || name='?'
            [[ $name != "$skip" ]] || continue
            printf '%s: %s (%s)\n' "$port" "$name" "$(readlink "/proc/$pid/exe" 2> /dev/null || printf '?')"
        done | sort -u || true
    done
}

nginx_dump() {
    nginx -T > "$TEMP_DIR/nginx-T.conf" 2> "$TEMP_DIR/nginx-T.log" \
        || { cat "$TEMP_DIR/nginx-T.log" >&2; die 'The current nginx configuration fails nginx -t: fix it first'; }
}

nginx_statements() {
    # Words as nginx reads them: quoted or not, a # outside quotes at the start of
    # a word begins a comment, and ; { } end a statement, so every server_name
    # statement is found, whether it shares its line or spans lines.
    awk -v blocks="${1:-0}" '
        function take(w) { if (w != "") words = words "\t" w }
        function finish() { if (words != "") print file words; words = "" }
        /^# configuration file / { file = substr($0, 22); sub(/:$/, "", file); words = ""; quote = ""; next }
        {
            if (quote == "") word = ""
            for (i = 1; i <= length($0); i++) {
                c = substr($0, i, 1)
                if (quote != "") {
                    if (c == "\\") word = word substr($0, ++i, 1)
                    else if (c == quote) quote = ""
                    else word = word c
                } else if (c == "\"" || c == "\047") quote = c
                else if (c == "#" && word == "") break
                else if (c ~ /[ \t;{}]/) { take(word); word = ""; if (c ~ /[;{}]/) finish(); if (blocks && c ~ /[{}]/) print file "\t" c }
                else word = word c
            }
            if (quote == "") take(word)
        }' "$TEMP_DIR/nginx-T.conf"
}

foreign_names() {   # the table's host names that another nginx file serves, as FILE: NAME
    nginx_statements | awk -F '\t' -v ours="$NGINX_CONF" -v hosts=" ${HOSTS[*]} " '
        $1 != ours && $2 == "server_name" {
            for (i = 3; i <= NF; i++) if (index(hosts, " " tolower($i) " ")) print $1 ": " tolower($i)
        }'
}

foreign_defaults() {
    nginx_statements | awk -F '\t' -v ours="$NGINX_CONF" '
        $1 != ours && $2 == "listen" && $3 ~ /(^|:)443$/ {
            for (i = 4; i <= NF; i++) if ($i == "default_server" || $i == "default") {
                statement = $2
                for (j = 3; j <= NF; j++) statement = statement " " $j
                print $1 ": " statement
                next
            }
        }'
}

render() {   # the nginx configuration for the table; its generation is a hash of the rest
    local text generation
    text=$(nginx_conf)
    generation=$(sha256sum <<< "$text" | cut -c1-16)
    printf '%s\n' "${text//@GENERATION@/$generation}"
}

http_host() {   # $1 host name: whether a route of it with a known port has http
    local i
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_HOST[i]} != "$1" || ${R_PORT[i]} == - || ${R_HTTP[i]} != http ]] || return 0
    done
    return 1
}

api_served() {
    local i
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_PORT[i]} == - || ${R_API[i]} != api ]] || return 0
    done
    return 1
}

api_closed() {
    local i
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_HOST[i]} != "$1" || ${R_PORT[i]} == - || ${R_HTTP[i]} != http || ${R_API[i]} != api ]] \
            || [[ " ${R_PATH[i]}docs ${R_PATH[i]}redoc ${R_PATH[i]}openapi.json " != *" $2 "* ]] || return 0
    done
    return 1
}

proxy_settings() {   # $1 host name: how its server blocks forward to the services
    local name=$1
    cat <<NGINX
    # Redirects keep the host name and scheme the browser used. Uploads up to
    # 128 MB and requests up to 12 minutes, as in the App's web container.
    absolute_redirect off;
    client_max_body_size 128m;
    proxy_connect_timeout 720s;
    proxy_send_timeout 720s;
    proxy_read_timeout 720s;
    # The services see this host name and the caller's own address, whatever the
    # caller put in these headers.
    proxy_http_version 1.1;
    proxy_set_header Host $name;
    proxy_set_header X-Forwarded-Host $name;
    proxy_set_header X-Forwarded-For \$remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$perodua_https_connection;
    # Odoo sets its session cookie without Secure, and these host names also
    # answer plain HTTP (port 80, and the App's own port on the network): the
    # browser sends the cookie over HTTPS only, on every port. On stgiss the
    # cookie also signs in every module host name.
    proxy_cookie_flags session_id secure samesite=lax;
NGINX
}

# $1 host name, $2 all (its routes) or http (its routes with http; the others
# redirect), $3 and $4 the comment and the statement for every other path, left
# out when a route is /. $host below is nginx's.
# shellcheck disable=SC2016
locations() {
    local i limit root=0
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_HOST[i]} == "$1" && ${R_PORT[i]} != - ]] || continue   # port -: in the certificate, not served yet
        [[ ${R_PATH[i]} != / ]] || root=1
        if [[ $2 == http && ${R_HTTP[i]} != http ]]; then
            # Also under a route with http (/dev/api/ under /dev/): the redirect, and
            # for the path without its last /, which on 443 nginx itself redirects.
            [[ ${R_PATH[i]} == / ]] || api_closed "$1" "${R_PATH[i]%/}" \
                || printf '\n    location = %s {\n        return 301 https://$host$request_uri;\n    }\n' "${R_PATH[i]%/}"
            printf '\n    location %s {\n        return 301 https://$host$request_uri;\n    }\n' "${R_PATH[i]}"
            continue
        fi
        limit=''
        if [[ ${R_API[i]} == api ]]; then
            printf '\n    location = %s%s {\n        return 404;\n    }\n' \
                "${R_PATH[i]}" docs "${R_PATH[i]}" redoc "${R_PATH[i]}" openapi.json
            printf -v limit '        limit_req zone=perodua_https_api burst=%s nodelay;\n        limit_req_status 429;\n' "$API_BURST"
        fi
        # With a URI part (the trailing /) nginx replaces the matched path: strip.
        # nginx itself redirects the path without its last / (301, query kept).
        printf '\n    location %s {\n%s        proxy_pass http://127.0.0.1:%s%s;\n    }\n' \
            "${R_PATH[i]}" "$limit" "${R_PORT[i]}" "$([[ ${R_STRIP[i]} == strip ]] && printf /)"
    done
    ((root)) || printf '\n    # %s\n    location / {\n        %s;\n    }\n' "$3" "$4"
}

# The $ names in single quotes below are nginx variables, not shell ones; in the
# here-documents the shell fills in $name and $STATE_DIR and leaves \$ to nginx.
# shellcheck disable=SC2016
nginx_conf() {   # the configuration, with @GENERATION@ where its generation goes
    local name redirect=() front=()
    for name in "${HOSTS[@]}"; do   # a host with routes for a TLS front gets a port 80 block of its own
        if http_host "$name"; then front+=("$name"); else redirect+=("$name"); fi
    done
    printf '# Written by https.sh from %s. Do not edit: change the routes, then run: sudo bash https.sh apply\n\n' "$ROUTES_FILE"
    printf 'map $http_upgrade $perodua_https_connection {\n    default upgrade;\n    %s close;\n}\n' "''"
    if api_served; then
        printf '\nlimit_req_zone $binary_remote_addr zone=perodua_https_api:10m rate=%s;\n' "$API_RATE"
    fi
    ((${#redirect[@]} == 0)) \
        || printf '\nserver {\n    listen 80;\n    server_name %s;\n    return 301 https://$host$request_uri;\n}\n' "${redirect[*]}"
    for name in "${front[@]}"; do
        cat <<NGINX

server {
    listen 80;
    server_name $name;

    # A TLS front, such as a WAF, forwards the browsers' HTTPS to this port: the
    # routes with the http option are served here as on 443, to any caller and
    # unencrypted.

NGINX
        proxy_settings "$name"
        locations "$name" http 'Every other path is closed, as on 443.' 'return 404'
        printf '}\n'
    done
    for name in "${HOSTS[@]}"; do
        cat <<NGINX

server {
    listen 443 ssl;
    server_name $name;
    ssl_certificate $STATE_DIR/fullchain.pem;
    ssl_certificate_key $STATE_DIR/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:perodua_https:10m;
    ssl_session_timeout 1d;

NGINX
        proxy_settings "$name"
        cat <<NGINX

    # Which configuration nginx runs, for https.sh on this server only.
    location = /.perodua-https {
        if (\$remote_addr != 127.0.0.1) {
            return 404;
        }
        return 200 "generation @GENERATION@";
    }
NGINX
        locations "$name" all 'Every other path is closed.' 'return 404'
        printf '}\n'
    done
    cat <<NGINX

server {
    listen 443 ssl default_server;
    server_name _;
    ssl_certificate $STATE_DIR/fullchain.pem;
    ssl_certificate_key $STATE_DIR/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:perodua_https:10m;
    ssl_session_timeout 1d;
    return 444;
}
NGINX
}

# ---------------------------------------------------------------- commands
cmd_csr() {
    local subject='^(/[A-Za-z]+=[^/=]+)+$'
    [[ $SUBJECT =~ $subject ]] || die '--subject looks like /C=MY/O=Company Name'
    [[ ! ${SUBJECT^^} =~ /CN= ]] || die '--subject must not hold CN: the first host name of the table is the CN'
    lock
    load_routes
    make_request
    printf 'Certificate request for %s (CN %s), saved as %s.\n' "${HOSTS[*]}" "${HOSTS[0]}" "$STATE_DIR/request.csr"
    printf 'Send this request to the certificate issuer. The private key (%s) stays on this server:\n\n' "$REQUEST_KEY"
    cat -- "$STATE_DIR/request.csr"
    [[ -z $KEY_FILE ]] || printf '\nIf a request made with %s was already sent, wait for its certificate instead of sending this one.\n' "$KEY_FILE"
    printf '\nWhen the certificate comes back: sudo bash https.sh install-cert FILE [CHAIN], then sudo bash https.sh apply\n'
}

make_request() {   # request.csr for every host name of the table; REQUEST_KEY: the key it was made with
    local key san
    mkdir -p -- "$STATE_DIR"
    key=$STATE_DIR/key.pem
    if [[ -n $KEY_FILE ]]; then   # a key made elsewhere, for example one a request was already made with
        # written again unencrypted, as nginx reads it: openssl asks for the pass phrase of an encrypted key
        if ! openssl pkey -in "$KEY_FILE" -out "$key.tmp"; then
            rm -f -- "$key.tmp"
            die "$KEY_FILE is not a private key this script can read (an encrypted one needs its pass phrase on a terminal)"
        fi
        if [[ $(pub_of key "$key.tmp") == "$(pub_of key "$key")" ]]; then
            rm -f -- "$key.tmp"   # the key in use already
        else
            key=$(next_key)
            mv -f -- "$STATE_DIR/key.pem.tmp" "$key"
        fi
    elif [[ ! -f $key ]] || ((NEW_KEY)); then
        key=$(next_key)
        make_key "$key"
    elif [[ -f $STATE_DIR/key.new.pem ]]; then
        key=$STATE_DIR/key.new.pem   # a new key waits for its certificate
    fi
    san=$(printf 'DNS:%s,' "${HOSTS[@]}")
    openssl req -new -key "$key" -subj "$SUBJECT/CN=${HOSTS[0]}" -addext "subjectAltName=${san%,}" \
        -out "$STATE_DIR/request.csr.tmp" || die 'Could not make the certificate request'
    chmod 0644 -- "$STATE_DIR/request.csr.tmp"
    mv -f -- "$STATE_DIR/request.csr.tmp" "$STATE_DIR/request.csr"
    REQUEST_KEY=$key
}

cmd_install_cert() {
    local cert=${ARGS[0]:-} chain_file=${ARGS[1]:-}
    [[ -n $cert ]] || die 'Usage: sudo bash https.sh install-cert CERT [CHAIN]'
    lock
    load_routes
    install_cert "$cert" "$chain_file"
}

# $1 the certificate file, $2 its chain file or nothing, $3 optionally a function
# that saves and writes more files just before the certificate is replaced (the
# new chain is $TEMP_DIR/fullchain.pem), which then come back with the certificate
# if the change is not kept. With TRUST_FILE set (the Let's Encrypt path), the
# chain must also lead to one of the roots in it.
install_cert() {
    local cert=$1 chain_file=$2 key leaf='' kp f missing top anchor purposes chain=() anchors=() args=()
    collect_certs "$cert" ${chain_file:+"$chain_file"}
    for key in "$STATE_DIR/key.new.pem" "$STATE_DIR/key.pem"; do   # a waiting new key first
        [[ -f $key ]] || continue
        kp=$(pub_of key "$key")
        for f in "$TEMP_DIR"/certs/*.pem; do
            if [[ -n $kp && $(pub_of cert "$f") == "$kp" ]]; then leaf=$f; break 2; fi
        done
    done
    [[ -n $leaf ]] || die "None of these certificates belongs to this server's private key: have the request that csr made here signed"
    missing=$(missing_hosts "$leaf")
    [[ -z $missing ]] || die "The certificate does not cover: ${missing% }. Run csr again (it lists every host name of the table) and have that request signed"
    openssl x509 -in "$leaf" -noout -checkend 0 > /dev/null || die "The certificate expired on $(field enddate "$leaf")"
    (($(date -d "$(field startdate "$leaf")" +%s) <= $(date +%s))) || die "The certificate is valid only from $(field startdate "$leaf")"
    openssl x509 -in "$leaf" -noout -checkend 2592000 > /dev/null || warn "The certificate expires within 30 days, on $(field enddate "$leaf")"
    mapfile -t chain < <(chain_of "$leaf")
    top=$leaf
    ((${#chain[@]} == 0)) || top=${chain[${#chain[@]}-1]}
    mapfile -t anchors < <(signers "$top")   # what signed the last certificate served, a root most often
    anchor=${anchors[0]:-}
    [[ -n $anchor || $top == "$leaf" ]] || anchor=$top   # not given: the chain is checked up to its last intermediate
    if [[ -n $anchor ]]; then   # as clients check it: signatures, dates, CA and server use
        args=(-partial_chain -purpose sslserver -CAfile "$anchor")
        if ((${#chain[@]})); then cat -- "${chain[@]}" > "$TEMP_DIR/chain.pem"; args+=(-untrusted "$TEMP_DIR/chain.pem"); fi
        openssl verify "${args[@]}" "$leaf" > "$TEMP_DIR/verify.log" 2>&1 \
            || die "The certificate does not verify with its chain ($(grep -m1 '^error' "$TEMP_DIR/verify.log")): pass the chain the issuer gave for this certificate"
    else   # the certificate alone, its issuer not given: its signature cannot be checked here, its use can
        purposes=$(openssl x509 -in "$leaf" -noout -purpose)
        grep -qx 'SSL server : Yes' <<< "$purposes" || die 'The certificate is not meant for a TLS server (its key usage or extended key usage)'
        warn "No intermediate certificate of $(field issuer "$leaf") was given: pass the issuer's chain file as CHAIN, or browsers without it will refuse the site"
    fi
    if [[ -n $TRUST_FILE ]]; then   # a public CA: its chain must reach a root that browsers and this server trust
        args=(-no-CApath -no-CAstore -purpose sslserver -CAfile "$TRUST_FILE")
        if ((${#chain[@]})); then cat -- "${chain[@]}" > "$TEMP_DIR/served.pem"; args+=(-untrusted "$TEMP_DIR/served.pem"); fi
        openssl verify "${args[@]}" "$leaf" > "$TEMP_DIR/verify.log" 2>&1 \
            || die "The certificate does not lead to a root this server trusts ($(grep -m1 '^error' "$TEMP_DIR/verify.log")). A test CA such as the Let's Encrypt staging one needs its root: --extra-root FILE"
    fi
    { openssl x509 -in "$leaf"; for f in "${chain[@]}"; do openssl x509 -in "$f"; done; } > "$TEMP_DIR/fullchain.pem"
    save "$STATE_DIR/fullchain.pem" "$STATE_DIR/key.pem" "$STATE_DIR/key.new.pem"
    [[ -z ${3:-} ]] || "$3"   # the caller's own files, saved and written as part of the same change, before the certificate
    install -m 0644 -- "$TEMP_DIR/fullchain.pem" "$STATE_DIR/fullchain.pem.tmp"
    mv -f -- "$STATE_DIR/fullchain.pem.tmp" "$STATE_DIR/fullchain.pem"
    [[ $key != "$STATE_DIR/key.new.pem" ]] || mv -f -- "$STATE_DIR/key.new.pem" "$STATE_DIR/key.pem"
    if applied; then activate; fi   # apply ran before: nginx serves the new certificate now
    keep   # only now: until here, any exit puts every saved file back
    printf 'Installed the certificate for %s, issued by %s, valid until %s (%s days).\n' \
        "$(cert_names "$STATE_DIR/fullchain.pem" | paste -sd ' ' -)" "$(field issuer "$leaf")" "$(field enddate "$leaf")" "$(days_left "$leaf")"
    if applied; then printf 'nginx serves it now.\n'; else printf 'Next: sudo bash https.sh apply\n'; fi
}

# ---------------------------------------------------------------- Let's Encrypt
read_settings() {   # the options kept by the run that installed the certificate, into SETTINGS
    local key value
    [[ -f $LE_DIR/settings ]] || return 0
    while IFS='=' read -r key value || [[ -n $key ]]; do
        case $key in SERVER|ACME_DNS|DNS_RESOLVERS|EMAIL|EXTRA_ROOT_FOR|ACME_CA_FOR|TOS_ACCEPTED) SETTINGS[$key]=$value ;; esac
    done < "$LE_DIR/settings"
}

le_url() {   # $1 option, $2 value, $3 an example, $4 the schemes: an address of one of them
    local url="^($4)://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$"
    [[ $2 =~ $url ]] || die "$1 must be an ${4//|/:// or }:// address, such as $3, not $2"
}

# $1 option key, $2 its file in LE_DIR, $3 its settings key, $4 what it is: the file
# given, else the one kept for SERVER. A kept file belongs to the ACME server it
# was given with.
kept_file() {
    if [[ -v GIVEN[$1] ]]; then
        [[ -z ${GIVEN[$1]} ]] || [[ -f ${GIVEN[$1]} ]] || die "No such file: ${GIVEN[$1]}"
        [[ -z ${GIVEN[$1]} ]] || realpath -- "${GIVEN[$1]}"
    elif [[ -f $LE_DIR/$2 && ${SETTINGS[$3]:-} == "$SERVER" ]]; then
        printf '%s\n' "$LE_DIR/$2"
    elif [[ -f $LE_DIR/$2 ]]; then
        printf 'The %s kept for %s is not used for %s.\n' "$4" "${SETTINGS[$3]:-another ACME server}" "$SERVER" >&2
    fi
}

keep_file() {   # $1 the file in effect, $2 its name in LE_DIR: kept for later runs, or removed
    if [[ -z $1 ]]; then
        rm -f -- "$LE_DIR/$2"
    elif [[ $1 != "$LE_DIR/$2" ]]; then
        install -m 0600 -- "$1" "$LE_DIR/$2.tmp"
        mv -f -- "$LE_DIR/$2.tmp" "$LE_DIR/$2"
    fi
}

is_root() {   # $1: one self-signed CA certificate, in PEM
    [[ $(grep -c -- '-----BEGIN CERTIFICATE-----' "$1") == 1 ]] || return 1
    openssl x509 -in "$1" -noout -ext basicConstraints 2> /dev/null | grep -q 'CA:TRUE' || return 1
    openssl verify -no-CApath -no-CAstore -CAfile "$1" "$1" > /dev/null 2>&1   # signed by its own key
}

le_options() {   # the options given, else those kept, else the defaults; checked before anything changes
    local item port resolvers=()
    read_settings
    SERVER=${GIVEN[server]-${SETTINGS[SERVER]:-$DEFAULT_SERVER}}
    ACME_DNS=${GIVEN[acmedns]-${SETTINGS[ACME_DNS]:-$DEFAULT_ACME_DNS}}
    while [[ $ACME_DNS == */ ]]; do ACME_DNS=${ACME_DNS%/}; done
    DNS_RESOLVERS=${GIVEN[resolvers]-${SETTINGS[DNS_RESOLVERS]:-}}
    EMAIL=${GIVEN[email]-${SETTINGS[EMAIL]:-}}
    le_url --server "$SERVER" https://acme-staging-v02.api.letsencrypt.org/directory https
    le_url --acme-dns "$ACME_DNS" "$DEFAULT_ACME_DNS" 'https|http'
    [[ $ACME_DNS != http://* ]] || warn "--acme-dns $ACME_DNS sends the acme-dns passwords unencrypted: use http:// only for a local test"
    LEGO_NETWORK=${PERODUA_HTTPS_LEGO_NETWORK:-host}
    [[ $LEGO_NETWORK =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "PERODUA_HTTPS_LEGO_NETWORK must be a Docker network name, not $LEGO_NETWORK"
    [[ -z $EMAIL || $EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "--email: $EMAIL is not an e-mail address"
    if [[ -n $DNS_RESOLVERS ]]; then   # HOST[:PORT],... as lego takes them, with the port always written
        IFS=, read -ra resolvers <<< "$DNS_RESOLVERS"
        DNS_RESOLVERS=''
        for item in "${resolvers[@]}"; do
            [[ $item =~ ^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?$ ]] \
                || die "--dns-resolvers: $item is not HOST or HOST:PORT (an IPv6 address in brackets, like [2001:db8::53]:53)"
            port=${BASH_REMATCH[3]:-53}
            ((10#$port >= 1 && 10#$port <= 65535)) || die "--dns-resolvers: $item has no valid port"
            DNS_RESOLVERS+=${DNS_RESOLVERS:+,}${BASH_REMATCH[1]}:$((10#$port))
        done
    fi
    EXTRA_ROOT=$(kept_file extraroot extra-root.pem EXTRA_ROOT_FOR 'extra root')
    [[ -z $EXTRA_ROOT ]] || is_root "$EXTRA_ROOT" \
        || die "--extra-root: $EXTRA_ROOT is not one root certificate (PEM, a CA that signed itself)"
    ACME_CA=$(kept_file acmeca acme-ca.pem ACME_CA_FOR 'ACME server CA')
    [[ -z $ACME_CA ]] || openssl x509 -in "$ACME_CA" -noout 2> /dev/null || die "--acme-ca: $ACME_CA holds no PEM certificate"
}

save_settings() {   # the options of this run, for later runs and the timer
    local tos=${SETTINGS[TOS_ACCEPTED]:-}
    ((ACCEPT_TOS == 0)) || [[ " $tos " == *" $SERVER "* ]] || tos+=${tos:+ }$SERVER
    keep_file "$EXTRA_ROOT" extra-root.pem
    keep_file "$ACME_CA" acme-ca.pem
    printf 'SERVER=%s\nACME_DNS=%s\nDNS_RESOLVERS=%s\nEMAIL=%s\nEXTRA_ROOT_FOR=%s\nACME_CA_FOR=%s\nTOS_ACCEPTED=%s\n' \
        "$SERVER" "$ACME_DNS" "$DNS_RESOLVERS" "$EMAIL" "${EXTRA_ROOT:+$SERVER}" "${ACME_CA:+$SERVER}" "$tos" > "$LE_DIR/settings.tmp"
    mv -f -- "$LE_DIR/settings.tmp" "$LE_DIR/settings"
    SETTINGS[TOS_ACCEPTED]=$tos
}

le_prerequisites() {   # what the server needs to get the certificate, checked before anything changes
    local tos code
    command -v docker > /dev/null || die 'letsencrypt runs lego in Docker, which is not installed: sudo bash install-dependencies.sh --role app'
    docker info > /dev/null 2>&1 || die 'Docker does not run (docker info fails): sudo systemctl enable --now docker'
    command -v curl > /dev/null || die 'curl is required: sudo apt-get install curl'
    command -v dig > /dev/null || die 'letsencrypt looks the DNS records up with dig, which is not installed: sudo apt-get install bind9-dnsutils'
    [[ -s $SYSTEM_ROOTS ]] || die "This server has no CA certificates ($SYSTEM_ROOTS) to check the new certificate with: sudo apt-get install ca-certificates"
    [[ $SERVER != https://acme-staging-v02.api.letsencrypt.org/* || -n $EXTRA_ROOT ]] \
        || die "Certificates from the Let's Encrypt staging server lead to its test roots, which no system trusts: pass the one they lead to with --extra-root FILE (see https://letsencrypt.org/docs/staging-environment/)"
    curl -fsS --max-time 20 ${ACME_CA:+--cacert "$ACME_CA"} -o "$TEMP_DIR/directory.json" -- "$SERVER" 2> "$TEMP_DIR/curl.log" \
        || die "Could not reach the ACME server $SERVER ($(tail -n 1 "$TEMP_DIR/curl.log")): this server needs outbound HTTPS to it"
    grep -q '"newOrder"' "$TEMP_DIR/directory.json" || die "$SERVER is not an ACME directory (it has no newOrder)"
    tos=$(sed -n 's/.*"termsOfService" *: *"\([^"]*\)".*/\1/p' "$TEMP_DIR/directory.json" | head -n 1)
    if [[ -n $tos ]] && ((ACCEPT_TOS == 0)) && [[ " ${SETTINGS[TOS_ACCEPTED]:-} " != *" $SERVER "* ]]; then
        die "The terms of service of $SERVER apply to the account this server gets there: $tos. Read them; to accept them, run the same command with --accept-tos"
    fi
    local image=$LEGO_IMAGE
    if ((MANUAL)); then   # the records go into the domain's own DNS: no acme-dns server
        image=$ACMESH_IMAGE
    else
        code=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' -- "$ACME_DNS/health" 2> "$TEMP_DIR/curl.log") \
            || die "Could not reach the acme-dns server $ACME_DNS ($(tail -n 1 "$TEMP_DIR/curl.log")): this server needs outbound HTTPS to it. Without an acme-dns server, use letsencrypt --manual: the DNS administrator puts the records in by hand"
        [[ $code != 5* ]] || die "The acme-dns server $ACME_DNS answers HTTP $code: it does not work at the moment"
    fi
    if ! docker image inspect "$image" > /dev/null 2>&1; then
        printf 'Pulling %s...\n' "$image"
        if ! docker pull -q "$image" > "$TEMP_DIR/pull.log" 2>&1; then
            cat "$TEMP_DIR/pull.log" >&2
            die "Could not pull $image (above): this server needs outbound HTTPS to Docker Hub (registry-1.docker.io), or the image loaded with docker load"
        fi
    fi
}

# The acme-dns accounts are kept in LE_DIR/acme-dns.json, one per host name:
#   {"HOST":{"fulldomain":"...","subdomain":"...","username":"...","password":"...","server_url":"..."},...}
# on one line when this script writes it, as goacmedns (lego's acme-dns library)
# does. A file made elsewhere, such as one from the acme-dns operator, may be
# spread over lines, hold the fields in another order and hold more of them (such
# as allowfrom, a list); it is used as it is. lego never gets it: it gets a copy
# the script writes (lego_accounts). goacmedns reads a file it cannot parse as no
# accounts at all, and would then make new ones over it, so the file must be
# exactly this JSON: no trailing comma, nothing after the object, each host name
# and each field once. Values are names, UUIDs and passwords: no backslash, no
# control character.
json_pairs() {   # $1 such a file: a HOST<TAB><TAB> line per host name, then HOST<TAB>FIELD<TAB>VALUE per text field; fails on anything else
    awk '
        function fail() { failed = 1; exit 1 }
        function blank() { while (pos <= n && index(" \t\r\n", substr(s, pos, 1))) pos++ }
        function next_char() { blank(); return substr(s, pos, 1) }
        function expect(c) { if (next_char() != c) fail(); pos++ }
        function text(   start, c) {   # a string, without escapes
            if (next_char() != "\"") fail()
            start = ++pos
            while (pos <= n) {
                c = substr(s, pos, 1)
                if (c == "\"") { pos++; return substr(s, start, pos - start - 1) }
                if (c == "\\" || c < " ") fail()
                pos++
            }
            fail()
        }
        function scalar(   start, word) {   # a number, true, false or null
            blank()
            start = pos
            while (pos <= n && substr(s, pos, 1) ~ /[A-Za-z0-9.+-]/) pos++
            word = substr(s, start, pos - start)
            if (word !~ /^(true|false|null|-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?)$/) fail()
        }
        function list(   c) {   # [ strings or scalars ], such as allowfrom
            expect("[")
            if (next_char() == "]") { pos++; return }
            for (;;) {
                if (next_char() == "\"") text(); else scalar()
                c = next_char(); pos++
                if (c == "]") return
                if (c != ",") fail()
            }
        }
        { s = s $0 "\n" }
        END {
            if (failed) exit 1
            n = length(s); pos = 1
            expect("{")
            if (next_char() == "}") pos++
            else for (;;) {
                host = text()
                if (host in hosts) fail()
                hosts[host] = 1
                out = out host "\t\t\n"
                expect(":"); expect("{")
                if (next_char() == "}") pos++
                else for (;;) {
                    field = text()
                    if ((host SUBSEP field) in fields) fail()
                    fields[host, field] = 1
                    expect(":")
                    c = next_char()
                    if (c == "\"") out = out host "\t" field "\t" text() "\n"
                    else if (c == "[") list()
                    else scalar()
                    c = next_char(); pos++
                    if (c == "}") break
                    if (c != ",") fail()
                }
                c = next_char(); pos++
                if (c == "}") break
                if (c != ",") fail()
            }
            blank()
            if (pos <= n) fail()   # something after the object
            printf "%s", out
        }' "$1"
}

load_accounts() {   # acme-dns.json into ACCOUNT[HOST/FIELD], and ACCOUNT_HOSTS (every host name in it)
    local host field value
    ACCOUNT=() ACCOUNT_HOSTS=()
    [[ -f $LE_DIR/acme-dns.json ]] || return 0
    json_pairs "$LE_DIR/acme-dns.json" > "$TEMP_DIR/accounts" \
        || die "$LE_DIR/acme-dns.json is not a file of acme-dns accounts this script can read: one JSON object {\"HOST\":{\"fulldomain\":\"...\",...},...}, each host name and field once, no trailing comma and nothing after it. It was left as it is"
    while IFS=$'\t' read -r host field value; do
        [[ " ${ACCOUNT_HOSTS[*]} " == *" $host "* ]] || ACCOUNT_HOSTS+=("$host")
        [[ -z $field ]] || ACCOUNT[$host/$field]=$value
    done < "$TEMP_DIR/accounts"
}

acme_dns_field() { printf '%s' "${ACCOUNT[$1/$2]:-}"; }   # $1 host name, $2 field of its acme-dns account

write_accounts() {   # $1 file, then host names: their accounts from ACCOUNT as goacmedns writes them: one line, 0600, at once
    local file=$1 host line='' field value
    shift
    while IFS= read -r host; do
        line+="${line:+,}\"$host\":{"
        for field in fulldomain subdomain username password server_url; do
            value=${ACCOUNT[$host/$field]:-}
            line+="\"$field\":\"$value\"$([[ $field == server_url ]] || printf ,)"
        done
        line+='}'
    done < <(printf '%s\n' "$@" | sort)
    printf '{%s}' "$line" > "$file.tmp"
    chmod 0600 -- "$file.tmp"
    mv -f -- "$file.tmp" "$file"
}

register_account() {   # $1 host name: a new account on the acme-dns server, written to acme-dns.json at once
    local code field value safe='^[A-Za-z0-9._-]+$'
    # as goacmedns registers: POST /register without a body; the answer is
    # {"username":"...","password":"...","fulldomain":"...","subdomain":"...","allowfrom":[]}
    code=$(curl -sS --max-time 30 -X POST -H 'Accept: application/json' -o "$TEMP_DIR/register.json" -w '%{http_code}' \
        -- "$ACME_DNS/register" 2> "$TEMP_DIR/curl.log") \
        || die "Could not reach the acme-dns server $ACME_DNS to make the account of $1 ($(tail -n 1 "$TEMP_DIR/curl.log"))"
    [[ $code == 2?? ]] || die "The acme-dns server $ACME_DNS did not make an account for $1 (HTTP $code: $(head -c 200 "$TEMP_DIR/register.json" 2> /dev/null)). If it makes accounts only for its operator, put the file of accounts they give in $LE_DIR/acme-dns.json"
    { printf '{"%s":' "$1"; cat -- "$TEMP_DIR/register.json"; printf '}'; } > "$TEMP_DIR/registered.json"
    json_pairs "$TEMP_DIR/registered.json" > "$TEMP_DIR/registered" || die "The acme-dns server $ACME_DNS gave an answer that is not an account"
    while IFS=$'\t' read -r _ field value; do
        case $field in fulldomain|subdomain|username|password) ACCOUNT[$1/$field]=$value ;; esac
    done < "$TEMP_DIR/registered"
    for field in fulldomain subdomain username password; do
        [[ ${ACCOUNT[$1/$field]:-} =~ $safe ]] || die "The acme-dns server $ACME_DNS gave an account without a usable $field"
    done
    ACCOUNT[$1/server_url]=$ACME_DNS   # as goacmedns records it
    ACCOUNT_HOSTS+=("$1")
    write_accounts "$LE_DIR/acme-dns.json" "${ACCOUNT_HOSTS[@]}"
}

# $1 a DNS server (HOST:PORT, an IPv6 address in brackets; '' for this server's
# DNS), $2 host name, $3 the name its record must point to. Sets LOOKUP to found,
# missing, other:NAME (it points elsewhere), unknown (the server did not answer:
# no reply, SERVFAIL, REFUSED) or nodig.
lookup_at() {
    local name=_acme-challenge.$2 out status cname args
    LOOKUP=unknown
    command -v dig > /dev/null || { LOOKUP=nodig; return 0; }
    args=(+time=3 +tries=2 +noall +answer +comments -t CNAME "$name")
    if [[ -n $1 ]]; then
        out=${1%:*}
        out=${out#\[}
        args+=("@${out%\]}" -p "${1##*:}")
    fi
    out=$(dig "${args[@]}" 2> /dev/null) || return 0
    status=$(sed -n 's/.*status: \([A-Z]*\).*/\1/p' <<< "$out" | head -n 1)
    [[ $status == NOERROR || $status == NXDOMAIN ]] || return 0   # this one cannot tell
    cname=$(awk -v name="${name,,}." 'tolower($1) == name && $4 == "CNAME" { print tolower($5); exit }' <<< "$out")
    cname=${cname%.}
    if [[ -z $cname ]]; then LOOKUP=missing; elif [[ $cname == "${3,,}" ]]; then LOOKUP=found; else LOOKUP=other:$cname; fi
}

lookup() {   # $1 host name, $2 its target: LOOKUP from the first DNS server of --dns-resolvers that answers, else this server's
    local resolver resolvers=('')
    [[ -z $DNS_RESOLVERS ]] || IFS=, read -ra resolvers <<< "$DNS_RESOLVERS"
    for resolver in "${resolvers[@]}"; do
        lookup_at "$resolver" "$1" "$2"
        [[ $LOOKUP == unknown ]] || return 0
    done
}

# For letsencrypt: the records of the host names in $@ as ONE DNS server sees them,
# into CNAME_STATE, and that server into LEGO_RESOLVERS (nothing for this server's
# DNS). lego checks the TXT record of every host name on every DNS server it is
# given, and fails on one that does not answer, so it gets the first server of
# --dns-resolvers that answered for every host name. Fails, with what each server
# did not answer, when none did.
pick_resolver() {
    local resolver name host failed words='' own="this server's DNS" resolvers=('')
    [[ -z $DNS_RESOLVERS ]] || IFS=, read -ra resolvers <<< "$DNS_RESOLVERS"
    for resolver in "${resolvers[@]}"; do
        failed='' name=$resolver
        [[ -n $name ]] || name=$own
        for host in "$@"; do
            lookup_at "$resolver" "$host" "${ACCOUNT[$host/fulldomain]}"
            CNAME_STATE[$host]=$LOOKUP
            [[ $LOOKUP != unknown ]] || failed+="${failed:+, }_acme-challenge.$host"
        done
        if [[ -z $failed ]]; then
            LEGO_RESOLVERS=$resolver RECORDS_BY=$name
            return 0
        fi
        words+="${words:+; }$name did not answer for $failed"
    done
    PICK_FAILURE=$words
    return 1
}

state_words() {   # $1 a LOOKUP value, or new: in words
    case $1 in
        found) printf 'found' ;;
        missing) printf 'NOT FOUND' ;;
        new) printf 'NOT FOUND (its account was made now)' ;;
        other:*) printf 'WRONG: it points to %s' "${1#other:}" ;;
        nodig) printf 'not looked up: dig is not installed (apt-get install bind9-dnsutils)' ;;
        *) printf 'not looked up: no DNS server answered' ;;
    esac
}

print_records() {   # the CNAME records the DNS administrator creates, and what the DNS shows of them now
    local host target state
    printf "\nLet's Encrypt checks each host name through a DNS record. The DNS administrator\n"
    printf 'creates these CNAME records once, in the DNS that the internet sees:\n\n'
    for host in "${HOSTS[@]}"; do
        target=$(acme_dns_field "$host" fulldomain)
        [[ -z $target ]] || printf '_acme-challenge.%s CNAME %s\n' "$host" "$target"
    done
    local by=$RECORDS_BY
    [[ -n $by ]] || by=$DNS_RESOLVERS
    [[ -n $by ]] || by="this server's DNS"
    printf '\nWhat %s answers now:\n' "$by"
    for host in "${HOSTS[@]}"; do
        target=$(acme_dns_field "$host" fulldomain)
        [[ -n $target ]] || continue
        state=${CNAME_STATE[$host]:-}
        [[ -n $state ]] || { lookup "$host" "$target"; state=$LOOKUP; }
        printf '  _acme-challenge.%s: %s\n' "$host" "$(state_words "$state")"
    done
    printf "If this server's DNS keeps its own internal view of these names, the records go there\n"
    printf 'too, or name DNS servers that see the public records with --dns-resolvers.\n'
}

run_lego() {   # lego gets the certificate for request.csr into LE_DIR/lego/certificates/perodua-https.crt
    # --renew-force: a certificate lego has already got for this name would be skipped until
    # lego finds it due; this script decides when to renew. --dns.propagation.disable-ans: acme-dns
    # serves the TXT record as soon as it takes it, and a query to the authoritative servers
    # needs outbound port 53, which private networks often block; the record is still checked
    # through the resolvers. --dns.resolvers: the one DNS server that answered this script's
    # look-up of every record (LEGO_RESOLVERS, see pick_resolver); none for this server's DNS.
    local ca status=0 env=() args=(run --path /data/lego --server "$SERVER" --accept-tos --account-id perodua-https
                --cert.name perodua-https --csr /request.csr --dns acmedns --dns.propagation.disable-ans
                --renew-force --ari-disable)
    # The container sees only what lego reads and writes, all of it under --dir: its own
    # folder (its ACME account and certificates), the copy of the accounts, the request and
    # the ACME server's CA. Not acme-dns.json, not the rest of LE_DIR, never the private key.
    install -d -m 0700 -- "$LE_DIR/lego"
    local mounts=(-v "$LE_DIR/lego:/data/lego" -v "$LE_DIR/acme-dns.lego.json:/data/acme-dns.lego.json"
                  -v "$STATE_DIR/request.csr:/request.csr:ro")
    [[ -z $EMAIL ]] || args+=(--email "$EMAIL")
    [[ -z $LEGO_RESOLVERS ]] || args+=(--dns.resolvers "$LEGO_RESOLVERS")
    if [[ -n $ACME_CA ]]; then   # into LE_DIR first, when given from elsewhere
        ca=$LE_DIR/acme-ca.pem
        [[ $ACME_CA == "$ca" ]] || { ca=$LE_DIR/acme-ca.run.pem; install -m 0600 -- "$ACME_CA" "$ca"; }
        mounts+=(-v "$ca:/data/acme-ca.pem:ro")
        env=(-e LEGO_CA_CERTIFICATES=/data/acme-ca.pem)
    fi
    # lego gets a copy of the table's accounts, written here as goacmedns writes them, never
    # acme-dns.json: when an account does not work for it, lego makes another and saves its
    # storage over the file. A copy that changed tells that (LEGO_CHANGED_ACCOUNTS).
    write_accounts "$LE_DIR/acme-dns.lego.json" "${HOSTS[@]}"
    cp -- "$LE_DIR/acme-dns.lego.json" "$TEMP_DIR/acme-dns.lego.json"
    docker run --rm --network "$LEGO_NETWORK" "${mounts[@]}" \
        -e LEGO_LOG_FORMAT=text -e "ACME_DNS_API_BASE=$ACME_DNS" -e ACME_DNS_STORAGE_PATH=/data/acme-dns.lego.json "${env[@]}" \
        "$LEGO_IMAGE" "${args[@]}" > "$TEMP_DIR/lego.log" 2>&1 || status=$?
    LEGO_CHANGED_ACCOUNTS=0
    cmp -s -- "$LE_DIR/acme-dns.lego.json" "$TEMP_DIR/acme-dns.lego.json" || LEGO_CHANGED_ACCOUNTS=1
    rm -f -- "$LE_DIR/acme-ca.run.pem" "$LE_DIR/acme-dns.lego.json"
    return "$status"
}

lego_errors() {   # lego's error lines, else the end of what it printed
    grep 'level=ERROR' "$TEMP_DIR/lego.log" || tail -n 20 "$TEMP_DIR/lego.log"
}

renewal_dir() {   # the --dir the renewal timer works on; nothing without a timer
    [[ -f $UNIT_DIR/$UNIT.service ]] || return 0
    sed -n 's/^ExecStart=.* --dir \([^ ]*\).*/\1/p' "$UNIT_DIR/$UNIT.service" | head -n 1
}

renewal_args() {   # what the timer passes to the copy: this --dir, and the other paths when not the default
    local words=(--dir "$STATE_DIR")
    [[ -z $ROUTES ]] || words+=(--routes "$ROUTES")
    [[ $NGINX_CONF == "$DEFAULT_NGINX_CONF" ]] || words+=(--nginx-conf "$NGINX_CONF")
    [[ $UNIT_DIR == "$DEFAULT_UNIT_DIR" ]] || words+=(--unit-dir "$UNIT_DIR")
    [[ $LIB_DIR == "$DEFAULT_LIB_DIR" ]] || words+=(--lib-dir "$LIB_DIR")
    printf '%s\n' "${words[*]}"
}

unit_files() {   # the service and the timer, into $TEMP_DIR/units
    mkdir -p -- "$TEMP_DIR/units"
    cat > "$TEMP_DIR/units/$UNIT.service" << UNIT
# Written by https.sh letsencrypt; removed by https.sh uninstall.
[Unit]
Description=Renew the Let's Encrypt certificate of https.sh when fewer than $RENEW_DAYS days are left
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $LIB_DIR/https.sh letsencrypt --renew $(renewal_args)
UNIT
    cat > "$TEMP_DIR/units/$UNIT.timer" << UNIT
# Written by https.sh letsencrypt; removed by https.sh uninstall.
[Unit]
Description=Daily renewal check of the Let's Encrypt certificate of https.sh

[Timer]
OnCalendar=*-*-* 02:00:00
RandomizedDelaySec=4h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
}

# The timer runs a copy of this script in LIB_DIR, which stays when the downloaded
# scripts folder is moved or deleted. A run of letsencrypt or apply from another
# folder (a newer release) updates the copy; the timer's own run changes nothing
# of it. apply does, so that the copy reads the table apply took (an older copy
# refuses the http option, and the renewal would fail every day).
update_copy() {   # $1: 1 when the timer is new (then nothing is printed)
    if ! cmp -s -- "$SCRIPT_PATH" "$LIB_DIR/https.sh"; then
        install -m 0755 -- "$SCRIPT_PATH" "$LIB_DIR/https.sh.tmp"
        mv -f -- "$LIB_DIR/https.sh.tmp" "$LIB_DIR/https.sh"
        (($1)) || printf 'Updated the copy of this script that the renewal timer runs: %s\n' "$LIB_DIR/https.sh"
    fi
    if [[ -f $SCRIPT_DIR/https-routes.conf.example ]] && ! cmp -s -- "$SCRIPT_DIR/https-routes.conf.example" "$LIB_DIR/https-routes.conf.example"; then
        install -m 0644 -- "$SCRIPT_DIR/https-routes.conf.example" "$LIB_DIR/https-routes.conf.example"
    fi
}

setup_renewal() {
    local name changed=0 new=0
    [[ $SCRIPT_PATH != "$LIB_DIR/https.sh" ]] || return 0
    [[ -f $UNIT_DIR/$UNIT.timer ]] || new=1
    install -d -m 0755 -- "$LIB_DIR"
    update_copy "$new"
    unit_files
    mkdir -p -- "$UNIT_DIR"
    for name in "$UNIT.service" "$UNIT.timer"; do
        if cmp -s -- "$TEMP_DIR/units/$name" "$UNIT_DIR/$name"; then continue; fi
        install -m 0644 -- "$TEMP_DIR/units/$name" "$UNIT_DIR/$name.tmp"
        mv -f -- "$UNIT_DIR/$name.tmp" "$UNIT_DIR/$name"
        changed=1
    done
    if [[ -d /run/systemd/system ]]; then
        ((changed == 0)) || systemctl daemon-reload
        systemctl enable --now --quiet "$UNIT.timer"
        ((new == 0)) || printf 'Renewal: %s runs %s daily and renews the certificate when fewer than %s days are left.\n' \
            "$UNIT.timer" "$LIB_DIR/https.sh" "$RENEW_DAYS"
    else
        warn "systemd does not run here, so $UNIT.timer is written but not started: renew with sudo bash $LIB_DIR/https.sh letsencrypt --renew, for example daily from cron"
    fi
}

remove_renewal() {   # the timer, its units and the copy of this script, when they work on this --dir
    local owner
    owner=$(renewal_dir)
    if [[ -n $owner && $owner != "$STATE_DIR" ]]; then
        printf 'The Let'\''s Encrypt renewal timer works on %s: it is left alone.\n' "$owner"
        return 0
    fi
    if [[ -f $UNIT_DIR/$UNIT.timer || -f $UNIT_DIR/$UNIT.service ]]; then
        if [[ -d /run/systemd/system ]]; then systemctl disable --now --quiet "$UNIT.timer" 2> /dev/null || true; fi
        rm -f -- "$UNIT_DIR/$UNIT.timer" "$UNIT_DIR/$UNIT.service"
        if [[ -d /run/systemd/system ]]; then systemctl daemon-reload; fi
        printf 'Removed the Let'\''s Encrypt renewal timer.\n'
    fi
    rm -f -- "$LIB_DIR/https.sh" "$LIB_DIR/https-routes.conf.example"
    rmdir -- "$LIB_DIR" 2> /dev/null || true
}

from_letsencrypt() {   # whether the installed certificate is one letsencrypt installed: its fingerprint is a line of LE_DIR/installed
    [[ -f $LE_DIR/installed && -f $STATE_DIR/fullchain.pem ]] || return 1
    grep -qxF -- "$(fingerprint "$STATE_DIR/fullchain.pem")" "$LE_DIR/installed"
}

renewal_due() {   # for --renew: whether the certificate is to be renewed now; if not, says why
    local f=$STATE_DIR/fullchain.pem
    [[ -f $LE_DIR/installed && -f $f ]] || die 'No certificate from Let'\''s Encrypt is installed here yet: run sudo bash https.sh letsencrypt first'
    if ! from_letsencrypt; then
        printf 'The installed certificate, issued by %s, was not installed by letsencrypt: --renew leaves it alone.\n' "$(field issuer "$f")"
        return 1
    fi
    if openssl x509 -in "$f" -noout -checkend $((RENEW_DAYS * 86400)) > /dev/null; then
        printf 'The certificate is valid until %s (%s days): it is renewed when fewer than %s days are left.\n' \
            "$(field enddate "$f")" "$(days_left "$f")" "$RENEW_DAYS"
        return 1
    fi
    printf 'The certificate expires on %s (%s days left): renewing it.\n' "$(field enddate "$f")" "$(days_left "$f")"
}

# For install_cert, before the certificate is replaced: the options it comes with,
# and what the renewal knows it by. An ordinary failure puts all of it back. A hard
# stop (a kill, a power cut) runs nothing more, so LE_DIR/installed holds, until
# the change is kept, the fingerprint of the certificate in place (when it is one
# from letsencrypt) and that of the new one: whichever of the two such a stop
# leaves in place, the renewal still knows it.
# A stop between the certificate and nginx's reload (a kill, a power cut) leaves
# nginx serving the certificate it had, while the renewal finds the new one on
# disk valid and would never reload nginx. So every letsencrypt run checks, as
# apply and install-cert do, that nginx serves the installed chain for every host
# name, and reloads it if not. $1 strict: when nginx -t then refuses the files,
# fail without changing anything (the timer run shows as failed); otherwise only
# warn, as the installation of the same run checks nginx again.
nginx_serves_installed() {
    applied && [[ -f $STATE_DIR/fullchain.pem ]] || return 0   # no nginx file of this --dir
    command -v nginx > /dev/null && nginx_running || return 0   # it reads the files when it starts
    ! active || return 0
    if ! nginx -t > "$TEMP_DIR/nginx-t.log" 2>&1; then
        cat -- "$TEMP_DIR/nginx-t.log" >&2
        if [[ $1 != strict ]]; then
            warn "nginx does not serve the installed certificate, and nginx -t refuses the files (above): this run's installation checks them again"
            return 0
        fi
        die "nginx does not serve the installed certificate $STATE_DIR/fullchain.pem, and nginx -t refuses the files (above), so nginx was not reloaded and nothing was changed. A stop in the middle of an installation can leave the key and the certificate apart: get and install a new certificate with sudo bash https.sh letsencrypt"
    fi
    reload_nginx
    within_10s active \
        || die 'nginx was reloaded but, after about 10 seconds, still does not serve the installed certificate (see: sudo journalctl -u nginx, /var/log/nginx/error.log)'
    printf 'nginx was serving an older certificate than %s: it was reloaded and serves that one now.\n' "$STATE_DIR/fullchain.pem"
}

le_record() {
    local new
    save "$LE_DIR/installed" "$LE_DIR/settings" "$LE_DIR/extra-root.pem" "$LE_DIR/acme-ca.pem"
    new=$(fingerprint "$TEMP_DIR/fullchain.pem")
    { if from_letsencrypt; then fingerprint "$STATE_DIR/fullchain.pem"; fi; printf '%s\n' "$new"; } > "$LE_DIR/installed.tmp"
    mv -f -- "$LE_DIR/installed.tmp" "$LE_DIR/installed"
    save_settings
}

le_recorded() {   # after the change is kept: installed names the new certificate only
    fingerprint "$STATE_DIR/fullchain.pem" > "$LE_DIR/installed.tmp"
    mv -f -- "$LE_DIR/installed.tmp" "$LE_DIR/installed"
}

table_request() {   # the table's request, as csr makes it: every host name, with key.pem or a new key waiting
    if [[ ! -f $STATE_DIR/request.csr || $(request_names "$STATE_DIR/request.csr") != "$(printf '%s\n' "${HOSTS[@]}" | sort -u)" ]] \
        || ! { [[ -f $STATE_DIR/key.new.pem && $(pub_of req "$STATE_DIR/request.csr") == "$(pub_of key "$STATE_DIR/key.new.pem")" ]] \
               || [[ -f $STATE_DIR/key.pem && $(pub_of req "$STATE_DIR/request.csr") == "$(pub_of key "$STATE_DIR/key.pem")" ]]; }; then
        make_request
        printf 'Made the certificate request for %s with %s (it stays on this server).\n' "${HOSTS[*]}" "$REQUEST_KEY"
    fi
}

# letsencrypt --manual: the DNS administrator puts the TXT records of Let's
# Encrypt's DNS check into the domain's own public DNS by hand, instead of
# through acme-dns. acme.sh does it in two runs. The first asks Let's Encrypt
# for the checks of every host name and prints all the records at once; they
# are kept in LE_DIR/manual-records with the request they belong to. The next
# run looks the records up in public DNS first (a check that fails would spend
# them) and, once all are there, lets Let's Encrypt check them and installs the
# certificate. The order waits in acme.sh's own state (LE_DIR/manual) meanwhile.
# There is no timer: to renew, run the same command again, which starts over.
run_acmesh() {   # acme.sh in Docker, with only its own state and the request
    docker run --rm --network "$LEGO_NETWORK" -v "$LE_DIR/manual:/acme.sh" \
        -v "$STATE_DIR/request.csr:/request.csr:ro" ${ACME_CA:+-v "$ACME_CA:/acme-ca.pem:ro"} \
        "$ACMESH_IMAGE" acme.sh "$@" --server "$SERVER" ${ACME_CA:+--ca-bundle /acme-ca.pem} \
        --yes-I-know-dns-manual-mode-enough-go-ahead-please > "$TEMP_DIR/acmesh.log" 2>&1
}

manual_lookup() {   # $1 record name, $2 value: whether a public DNS server returns it; MANUAL_ASKED names the servers asked
    local item host port answer servers=()
    IFS=, read -ra servers <<< "${DNS_RESOLVERS:-$PUBLIC_RESOLVERS}"
    MANUAL_ASKED=${DNS_RESOLVERS:-$PUBLIC_RESOLVERS}
    for item in "${servers[@]}"; do
        port=${item##*:} host=${item%:*} host=${host#[} host=${host%]}
        answer=$(dig +short +time=3 +tries=2 -p "$port" TXT "$1" "@$host" 2> /dev/null) || continue
        [[ $answer != *';;'* ]] || continue   # no answer from that server
        grep -qxF -- "\"$2\"" <<< "$answer" && return 0
        return 1   # this server answered, without the value
    done
    return 1
}

le_manual() {
    local cn=${HOSTS[0]} records=$LE_DIR/manual-records request name value missing=0 status=0 crt
    install -d -m 0700 -- "$LE_DIR/manual"
    table_request
    request=$(openssl req -in "$STATE_DIR/request.csr" -outform DER | sha256sum | cut -c1-64)
    crt=$LE_DIR/manual/$cn/fullchain.cer
    if [[ -f $records && $(head -n 1 "$records") == "request $request" ]]; then
        # The second run: the records of this request's order.
        printf 'The TXT records Let'\''s Encrypt checks, in the public DNS of the domain:\n'
        while read -r name value; do
            if ((NO_DNS_CHECK)); then
                printf '  %s TXT "%s"\n' "$name" "$value"
            elif manual_lookup "$name" "$value"; then
                printf '  %s TXT "%s"  (found)\n' "$name" "$value"
            else
                printf '  %s TXT "%s"  (NOT FOUND)\n' "$name" "$value"
                missing=1
            fi
        done < <(tail -n +2 "$records")
        if ((missing)); then
            printf 'Looked up with %s. When every record is there, run the same command again. If this server cannot reach public DNS servers, name one with --dns-resolvers, or, once the records are there, skip the look-up with --no-dns-check.\n' "$MANUAL_ASKED"
            exit 3
        fi
        printf 'Asking %s to check them and issue the certificate (acme.sh)...\n' "$SERVER"
        run_acmesh --renew -d "$cn" || status=$?
        if ((status != 0)) || [[ ! -f $crt ]] || [[ $crt -ot $records ]]; then
            tail -n 25 "$TEMP_DIR/acmesh.log" >&2
            rm -f -- "$records"   # a failed check spends the order: the next run starts a new one
            die "Let's Encrypt did not issue the certificate (acme.sh's messages above). Run the same command again: it starts a new order with new records"
        fi
    else
        # The first run: a new order, and its records.
        run_acmesh --sign-csr --csr /request.csr --dns --force || status=$?
        awk -F"'" '/ Domain: / { name = $2 } / TXT value: / { if (name != "") print name, $2; name = "" }' \
            "$TEMP_DIR/acmesh.log" > "$TEMP_DIR/records"
        if ((status == 0)) && [[ -f $crt && ! -s $TEMP_DIR/records ]]; then
            :   # Let's Encrypt checked these names a short while ago and issued at once
        elif [[ -s $TEMP_DIR/records ]]; then
            { printf 'request %s\n' "$request"; cat -- "$TEMP_DIR/records"; } > "$records.tmp"
            mv -f -- "$records.tmp" "$records"
            printf 'Let'\''s Encrypt checks each host name through a TXT record. The DNS administrator creates\n'
            printf 'these in the public DNS of the domain (TTL 60 or 300 is fine):\n\n'
            while read -r name value; do printf '%s TXT "%s"\n' "$name" "$value"; done < "$TEMP_DIR/records"
            printf '\nWhen they exist, run the same command again. They are for this order only: another run of\n'
            printf 'the first step would give new values. Let'\''s Encrypt keeps the order for a few days.\n'
            exit 3
        else
            tail -n 25 "$TEMP_DIR/acmesh.log" >&2
            die "acme.sh did not start the order (its messages above)"
        fi
    fi
    TRUST_FILE=$TEMP_DIR/trust.pem
    cat -- "$SYSTEM_ROOTS" ${EXTRA_ROOT:+"$EXTRA_ROOT"} > "$TRUST_FILE"
    install_cert "$crt" ''
    rm -f -- "$records"
    printf 'To renew, run sudo bash https.sh letsencrypt --manual again within the last 30 days before %s: it prints new records.\n' \
        "$(field enddate "$STATE_DIR/fullchain.pem")"
}

cmd_letsencrypt() {
    local path owner host before crt url waiting=0 checked=()
    LE_DIR=$STATE_DIR/letsencrypt
    le_options
    for path in "$STATE_DIR" "$ROUTES" "$NGINX_CONF" "$UNIT_DIR" "$LIB_DIR"; do   # they go into the renewal unit
        [[ -z $path || $path =~ ^/[A-Za-z0-9._/-]+$ ]] || die "The renewal timer takes paths of letters, digits and . _ - / only, not $path"
    done
    lock
    load_routes
    owner=$(renewal_dir)
    [[ -z $owner || $owner == "$STATE_DIR" ]] \
        || die "The renewal timer ($UNIT_DIR/$UNIT.service) works on $owner: one Let's Encrypt setup per server"
    if ((RENEW)) && ! renewal_due; then
        nginx_serves_installed strict   # nothing else in this run would reload nginx
        setup_renewal
        return 0
    fi
    nginx_serves_installed lenient
    le_prerequisites
    install -d -m 0700 -- "$LE_DIR"
    if ((MANUAL)); then
        le_manual
        return 0
    fi
    [[ ! -f $LE_DIR/acme-dns.json ]] || chmod 0600 -- "$LE_DIR/acme-dns.json"   # it holds the accounts' passwords
    load_accounts
    for host in "${HOSTS[@]}"; do   # the accounts there are used as they are, if they are complete and of this acme-dns server
        [[ " ${ACCOUNT_HOSTS[*]} " == *" $host "* ]] || continue
        for path in fulldomain subdomain username password; do
            [[ ${ACCOUNT[$host/$path]:-} =~ ^[A-Za-z0-9._-]+$ ]] \
                || die "The acme-dns account of $host in $LE_DIR/acme-dns.json has no usable $path. The file was left as it is"
        done
        url=${ACCOUNT[$host/server_url]:-}
        while [[ $url == */ ]]; do url=${url%/}; done
        [[ -n $url ]] \
            || die "The acme-dns account of $host in $LE_DIR/acme-dns.json does not say which acme-dns server it is on (server_url). If the file came from an older tool, add \"server_url\":\"$ACME_DNS\" to each account in it. The file was left as it is"
        [[ $url == "$ACME_DNS" ]] \
            || die "The acme-dns accounts in $LE_DIR/acme-dns.json were made on $url, not $ACME_DNS. To move to $ACME_DNS, delete that file and run letsencrypt again: it makes accounts there and prints their records, which the DNS administrator puts in place of the old ones"
    done
    table_request
    # An account for every host name, made here: lego would make one only while it solves a
    # challenge, which Let's Encrypt skips for a host name it checked a short while ago.
    for host in "${HOSTS[@]}"; do
        [[ " ${ACCOUNT_HOSTS[*]} " != *" $host "* ]] || continue
        register_account "$host"
        CNAME_STATE[$host]=new   # its record cannot exist yet: not looked up, so no DNS server keeps its absence
        waiting=1
    done
    for host in "${HOSTS[@]}"; do   # lego runs only once every record points at its account
        [[ -n ${CNAME_STATE[$host]:-} ]] || checked+=("$host")
    done
    if ((${#checked[@]})) && ! pick_resolver "${checked[@]}"; then
        print_records
        die "No DNS server answered for every record, and lego checks each record on each DNS server it is given: $PICK_FAILURE. Check these DNS servers, or name one that answers for all of them with --dns-resolvers"
    fi
    for host in "${checked[@]}"; do
        [[ ${CNAME_STATE[$host]} == found ]] || waiting=1
    done
    if ((waiting)); then   # Let's Encrypt would not find them either
        print_records
        printf 'When the records exist, run the same command again.\n'
        exit 3
    fi
    crt=$LE_DIR/lego/certificates/perodua-https.crt
    before=$( [[ ! -f $crt ]] || fingerprint "$crt")
    printf 'Asking %s for a certificate for %s (lego, this takes a minute or two)...\n' "$SERVER" "${HOSTS[*]}"
    if ! run_lego; then
        lego_errors >&2
        ((LEGO_CHANGED_ACCOUNTS == 0)) \
            || die "lego made or changed an acme-dns account in the copy of the accounts it was given, which it does when one does not work for it (its messages above). $LE_DIR/acme-dns.json was left as it is"
        print_records
        die "Let's Encrypt did not issue the certificate (lego's messages above)"
    fi
    if ((LEGO_CHANGED_ACCOUNTS)); then
        cat "$TEMP_DIR/lego.log" >&2
        die "lego made or changed an acme-dns account in the copy of the accounts it was given (its messages above). The new certificate was not installed, and $LE_DIR/acme-dns.json was left as it is"
    fi
    if [[ ! -f $crt || $(fingerprint "$crt") == "$before" ]]; then
        cat "$TEMP_DIR/lego.log" >&2
        die 'lego ended without a new certificate (its messages above)'
    fi
    TRUST_FILE=$TEMP_DIR/trust.pem
    cat -- "$SYSTEM_ROOTS" ${EXTRA_ROOT:+"$EXTRA_ROOT"} > "$TRUST_FILE"
    # The certificate, the key, the fingerprint the renewal knows it by and the options it
    # came with change together: if any step fails or is interrupted, all of them come back.
    install_cert "$crt" '' le_record
    le_recorded
    setup_renewal
}

cmd_apply() {
    local i unknown='' port80='' missing takers conflicts
    lock
    load_routes
    [[ ! -e $NGINX_CONF ]] || applied || die "$NGINX_CONF was not written by apply for $STATE_DIR: it is left alone"
    if [[ $(renewal_dir) == "$STATE_DIR" && -f $LIB_DIR/https.sh && $SCRIPT_PATH != "$LIB_DIR/https.sh" ]]; then
        update_copy 0   # the renewal timer's copy reads this table too
    fi
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_PORT[i]} != - ]] || unknown+="https://${R_HOST[i]}${R_PATH[i]} "
    done
    [[ -f $STATE_DIR/fullchain.pem && -f $STATE_DIR/key.pem ]] \
        || die 'No certificate is installed yet: run csr, have the request signed, then install-cert'
    [[ $(pub_of cert "$STATE_DIR/fullchain.pem") == "$(pub_of key "$STATE_DIR/key.pem")" ]] \
        || die "The installed certificate does not belong to $STATE_DIR/key.pem"
    missing=$(missing_hosts "$STATE_DIR/fullchain.pem")
    [[ -z $missing ]] || die "The installed certificate does not cover ${missing% }: run csr, have the request signed, then install-cert"
    takers=$(port_takers)
    [[ -z $takers ]] || die "nginx needs ports 80 and 443, but something else listens there; stop or move it first (an nginx listed here is another installation than the one apply installs with apt):"$'\n'"$takers"
    ensure_nginx
    nginx_dump
    conflicts=$(foreign_names)
    [[ -z $conflicts ]] || die "Another nginx configuration already serves these host names; remove it first:"$'\n'"$conflicts"
    conflicts=$(foreign_defaults)
    [[ -z $conflicts ]] \
        || die "Another nginx configuration is already the default server for port 443; remove default_server there first:"$'\n'"$conflicts"
    render > "$TEMP_DIR/perodua-https.conf"
    mkdir -p -- "${NGINX_CONF%/*}"
    save "$NGINX_CONF"
    install -m 0644 -- "$TEMP_DIR/perodua-https.conf" "$NGINX_CONF.tmp"
    mv -f -- "$NGINX_CONF.tmp" "$NGINX_CONF"
    activate
    keep
    printf 'nginx serves HTTPS for:\n'
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_PORT[i]} != - ]] || continue
        [[ ${R_HTTP[i]} != http ]] || port80=1
        printf '  https://%s%s -> 127.0.0.1:%s%s%s%s\n' "${R_HOST[i]}" "${R_PATH[i]}" "${R_PORT[i]}" \
            "$([[ ${R_STRIP[i]} == strip ]] && printf ' (path removed)')" \
            "$([[ ${R_HTTP[i]} == http ]] && printf ' (also on port 80 over HTTP, for a TLS front)')" \
            "$([[ ${R_API[i]} == api ]] && printf ' (API: documentation closed, at most %s requests a second per caller address, bursts of %s)' \
                "${API_RATE%r/s}" "$API_BURST")"
    done
    [[ -z $unknown ]] || printf 'In the certificate, not served yet (port -, they answer 404 until the port is set in %s):\n  %s\n' \
        "$ROUTES_FILE" "${unknown% }"
    printf 'Other host names on port 443: the connection is closed.\n'
    printf 'http:// redirects to https://%s. Check it with: sudo bash https.sh status\n' \
        "${port80:+, except on the routes also on port 80}"
    if command -v ufw > /dev/null && ufw status 2> /dev/null | grep -q 'Status: active'; then
        if [[ -z $port80 ]]; then
            printf 'ufw is active: allow HTTP and HTTPS with: sudo ufw allow 80,443/tcp\n'
        else   # port 80 serves those routes unencrypted to every caller it lets in
            printf 'ufw is active: allow HTTPS with: sudo ufw allow 443/tcp, and port 80 for the TLS front only: sudo ufw allow from FRONT_ADDRESS to any port 80 proto tcp\n'
        fi
    fi
}

cmd_status() {
    local i f code plain listens takers pending=''
    load_routes
    printf 'Routes:      %s\n' "$ROUTES_FILE"
    f=$STATE_DIR/fullchain.pem
    if [[ -f $f ]]; then
        printf 'Certificate: %s\n             issued by %s, valid until %s (%s days)\n' \
            "$(cert_names "$f" | paste -sd ' ' -)" "$(field issuer "$f")" "$(field enddate "$f")" "$(days_left "$f")"
        [[ -z $(missing_hosts "$f") ]] || printf '             does NOT cover: %s\n' "$(missing_hosts "$f")"
    else
        printf 'Certificate: none installed\n'
    fi
    [[ ! -f $STATE_DIR/key.new.pem ]] || pending=' (a new key waits for its certificate)'
    [[ ! -f $STATE_DIR/request.csr ]] || printf 'Request:     %s%s\n' "$STATE_DIR/request.csr" "$pending"
    le_status
    if ! command -v nginx > /dev/null; then
        if command -v apt-get > /dev/null; then
            printf 'nginx:       not installed yet: apply installs it\n'
        else
            printf 'nginx:       not installed, and there is no apt-get to install it: install nginx before apply\n'
        fi
    elif [[ ! -f $NGINX_CONF ]]; then
        printf 'nginx:       %s not written yet (apply)\n' "$NGINX_CONF"
    elif ! applied; then
        printf 'nginx:       %s was not written by apply for %s\n' "$NGINX_CONF" "$STATE_DIR"
    else
        render > "$TEMP_DIR/now.conf"
        printf 'nginx:       %s%s\n' "$NGINX_CONF" "$(cmp -s "$TEMP_DIR/now.conf" "$NGINX_CONF" || printf ' (the routes or https.sh changed since: apply)')"
        if grep -qxF '    listen 443 ssl default_server;' "$NGINX_CONF"; then
            printf '             other host names on port 443: the connection is closed\n'
        fi
    fi
    takers=$(port_takers)   # what would stop apply
    [[ -z $takers ]] || printf 'Ports 80/443: in use by what apply cannot work next to; stop or move it first:\n             %s\n' \
        "${takers//$'\n'/$'\n'             }"
    printf 'Routes:\n'
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        listens='-' code='-' plain='-'
        if [[ ${R_PORT[i]} != - ]]; then
            listens=NO
            if [[ -n $(ss -ltnH "sport = :${R_PORT[i]}" 2> /dev/null) ]]; then listens=yes; fi
            if [[ -f $NGINX_CONF ]] && command -v curl > /dev/null; then
                code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "${R_HOST[i]}:443:127.0.0.1" \
                    "https://${R_HOST[i]}${R_PATH[i]}" || true)
                [[ ${R_HTTP[i]} != http ]] \
                    || plain=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --resolve "${R_HOST[i]}:80:127.0.0.1" \
                        "http://${R_HOST[i]}${R_PATH[i]}" || true)
            fi
        fi
        printf '  https://%s%s -> 127.0.0.1:%s  port listening: %s  HTTPS answer: %s%s%s\n' \
            "${R_HOST[i]}" "${R_PATH[i]}" "${R_PORT[i]}" "$listens" "$code" \
            "$([[ ${R_HTTP[i]} == http ]] && printf '  HTTP answer on port 80: %s' "$plain")" \
            "$([[ ${R_API[i]} == api && ${R_PORT[i]} != - ]] && printf '  API: docs closed, rate limited')"
    done
}

le_status() {   # the Let's Encrypt lines of status
    local f=$STATE_DIR/fullchain.pem host target owner timer result
    LE_DIR=$STATE_DIR/letsencrypt
    if [[ ! -d $LE_DIR ]]; then
        printf "Let's Encrypt: not used (letsencrypt gets the certificate from it)\n"
        return 0
    fi
    read_settings
    if [[ -f $LE_DIR/acme-dns.json ]] && ! json_pairs "$LE_DIR/acme-dns.json" > /dev/null; then
        printf '             %s cannot be read: letsencrypt says why\n' "$LE_DIR/acme-dns.json"
    else
        load_accounts
    fi
    DNS_RESOLVERS=${SETTINGS[DNS_RESOLVERS]:-}
    printf "Let's Encrypt: %s, acme-dns %s%s\n" "${SETTINGS[SERVER]:-$DEFAULT_SERVER}" "${SETTINGS[ACME_DNS]:-$DEFAULT_ACME_DNS}" \
        "${DNS_RESOLVERS:+, DNS checks with $DNS_RESOLVERS}"
    if [[ ! -f $LE_DIR/installed ]]; then
        printf '             no certificate from it installed yet\n'
    elif from_letsencrypt; then
        printf '             enabled: the installed certificate is from it, %s days left; renewed when fewer than %s are left\n' \
            "$(days_left "$f")" "$RENEW_DAYS"
    else
        printf '             the installed certificate is another one (install-cert): the renewal leaves it alone\n'
    fi
    owner=$(renewal_dir)
    if [[ -z $owner ]]; then
        printf '             renewal timer: none yet (the first certificate from letsencrypt sets it up)\n'
    elif [[ $owner != "$STATE_DIR" ]]; then
        printf '             renewal timer: works on %s\n' "$owner"
    elif [[ -d /run/systemd/system ]]; then
        timer=$(systemctl is-active "$UNIT.timer" 2> /dev/null || true)
        result=$(systemctl show -p Result --value "$UNIT.service" 2> /dev/null || true)
        printf '             renewal timer: %s, runs %s%s\n' "${timer:-unknown}" "$LIB_DIR/https.sh" \
            "$([[ -z $result || $result == success ]] || printf '; the last run failed (%s): sudo journalctl -u %s.service' "$result" "$UNIT")"
    else
        printf '             renewal timer: written, but systemd does not run here\n'
    fi
    printf 'DNS records: %s\n' "$([[ -n $DNS_RESOLVERS ]] && printf 'as %s answers' "$DNS_RESOLVERS" || printf "as this server's DNS answers")"
    for host in "${HOSTS[@]}"; do
        target=$(acme_dns_field "$host" fulldomain)
        if [[ -n $target ]]; then
            lookup "$host" "$target"
            printf '  _acme-challenge.%s CNAME %s  (%s)\n' "$host" "$target" "$(state_words "$LOOKUP")"
        else
            printf '  %s: no acme-dns account yet (letsencrypt makes one)\n' "$host"
        fi
    done
}

diag_ok() { printf '  OK %s\n' "$*"; }
diag_note() { printf '  NOTE %s\n' "$*"; }
diag_problem() { printf '  PROBLEM %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
diag_curl() { curl -sS --noproxy '*' --max-time 10 -A "$DIAG_AGENT" "$@"; }
diag_curl_error() { tail -n 1 -- "$TEMP_DIR/curl.log" 2> /dev/null || true; }

diag_listeners() {
    awk -v port="$1" -v sep="$SEP" '
        {
            address = $4
            p = address
            sub(/.*:/, "", p)
            if (p != port) next
            host = address
            sub(/:[^:]*$/, "", host)
            sub(/%.*/, "", host)
            list = list (list == "" ? "" : ", ") address
            if (host == "0.0.0.0" || host == "*" || host == "[::]" || host == "::") everywhere = 1
            else if (host ~ /^127\./ || host == "[::1]" || host == "::1" || host ~ /^\[::ffff:127\./) here = 1
            else network = 1
        }
        END { print (everywhere ? "all" : network ? "network" : here ? "local" : "none") sep list }'
}

diag_port_role() {
    local i urls=''
    case $1 in
        22) printf 'SSH'; return 0 ;;
        80) printf 'HTTP'; return 0 ;;
        443) printf 'HTTPS'; return 0 ;;
    esac
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_PORT[i]} != "$1" ]] || urls+="${urls:+, }https://${R_HOST[i]}${R_PATH[i]}"
    done
    printf '%s' "$urls"
}

diag_ports() {
    local i port kind list role where listening ports=(22 80 443)
    printf 'Ports:\n'
    for ((i = 0; i < ${#R_PORT[@]}; i++)); do
        [[ ${R_PORT[i]} == - || " ${ports[*]} " == *" ${R_PORT[i]} "* ]] || ports+=("${R_PORT[i]}")
    done
    if ! command -v ss > /dev/null; then
        diag_note 'ss is not installed (apt-get install iproute2): the ports were not checked'
        return 0
    fi
    if ! listening=$(ss -ltnH 2> /dev/null); then
        diag_note 'ss -ltnH failed: the ports were not checked'
        return 0
    fi
    for port in "${ports[@]}"; do
        kind=none list=''
        IFS=$SEP read -r kind list < <(diag_listeners "$port" <<< "$listening") || true
        role=$(diag_port_role "$port")
        case $kind in
            all) where="listening on all networks ($list)" ;;
            local) where="on this server only ($list)" ;;
            network) where="listening on $list" ;;
            *) where='not listening' ;;
        esac
        case $port:$kind in
            22:all|22:network) diag_ok "port 22 ($role): $where" ;;
            22:*) diag_note "port 22 ($role): $where" ;;
            80:all|80:network|443:all|443:network) diag_ok "port $port ($role): $where" ;;
            80:local) diag_note "port 80 ($role): $where: http:// addresses do not reach the redirect to https:// from the network" ;;
            443:local) diag_problem "port 443 ($role): $where: browsers and a front on the network cannot reach HTTPS" ;;
            80:none|443:none) diag_problem "port $port ($role): not listening: nginx does not run (sudo systemctl status nginx)" ;;
            *:local) diag_ok "port $port ($role): $where" ;;
            *:none) diag_problem "port $port ($role): not listening: the service does not run, and HTTPS answers 502 for its paths" ;;
            *) diag_note "port $port ($role): $where: it answers plain HTTP from the network too; only nginx needs it, on 127.0.0.1" ;;
        esac
    done
}

diag_servers() {
    nginx_statements 1 | awk -F '\t' -v sep="$SEP" -v ours="$NGINX_CONF" -v hosts=" ${HOSTS[*]} " '
        function port_of(address) {
            if (address ~ /^[0-9]+$/) return address
            if (address ~ /:[0-9]+$/) { sub(/.*:/, "", address); return address }
            return 80
        }
        function close_server(   list, count, n, k) {
            if (!listens) { print "L" sep 80 sep file sep 0; on80 = 1 }
            if (file != ours && (on80 || on443)) {
                count = split(names, list, " ")
                for (n = 1; n <= count; n++) {
                    k = file SUBSEP list[n]
                    if (!(file in served)) { served[file] = ""; order[++files] = file }
                    if (!(k in seen)) { seen[k] = 1; served[file] = served[file] " " list[n] }
                    if (on80) port80[k] = 1
                    if (on443) port443[k] = 1
                }
            }
            server = 0; listens = 0; on80 = 0; on443 = 0; names = ""
        }
        $1 != file { if (server) close_server(); file = $1; depth = 0; pending = 0 }
        $2 == "{" { depth++; if (pending) server = depth; pending = 0; next }
        $2 == "}" { if (server && depth == server) close_server(); if (depth) depth--; next }
        { pending = (NF == 2 && $2 == "server") }
        $2 == "listen" && $3 !~ /^unix:/ {
            port = port_of($3)
            chosen = 0
            for (i = 4; i <= NF; i++) if ($i == "default_server" || $i == "default") chosen = 1
            print "L" sep port sep $1 sep chosen
            if (server) { listens = 1; if (port == 80) on80 = 1; if (port == 443) on443 = 1 }
        }
        $2 == "server_name" && server {
            for (i = 3; i <= NF; i++) {
                name = tolower($i)
                if (index(hosts, " " name " ") && !index(" " names " ", " " name " ")) names = names " " name
            }
        }
        END {
            if (server) close_server()
            for (f = 1; f <= files; f++) {
                out = ""
                count = split(served[order[f]], list, " ")
                for (n = 1; n <= count; n++) {
                    k = order[f] SUBSEP list[n]
                    ports = ((k in port80) ? "80" : "") ((k in port80) && (k in port443) ? ", " : "") ((k in port443) ? "443" : "")
                    out = out (out == "" ? "" : ", ") list[n] " (" ports ")"
                }
                print "N" sep order[f] sep out
            }
        }'
}

diag_nginx_logs() {
    printf '%s\n' "$ACCESS_LOG" "$ERROR_LOG"
    { nginx -V 2>&1 || true; } | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^--(error|http)-log-path=\//) { sub(/^[^=]*=/, "", $i); print $i } }'
}

diag_file_state() {
    if [[ ! -e $1 ]]; then
        printf 'missing'
    elif [[ -d $1 || ! -r $1 ]]; then
        printf 'unreadable'
    else
        printf 'readable'
    fi
}

diag_nginx_files() {
    local port file files extra kind names missing=''
    printf 'nginx files:\n'
    if ! command -v nginx > /dev/null; then
        diag_note 'nginx is not installed (apply installs it): the nginx files were not checked'
        return 0
    fi
    while IFS= read -r file; do
        [[ -e $file || ", $missing, " == *", $file, "* ]] || missing+="${missing:+, }$file"
    done < <(diag_nginx_logs)
    if [[ -n $missing ]]; then
        diag_note "the log file $missing does not exist, and nginx -T creates the log files that nginx names: the nginx files were not checked (nginx creates its log files when it starts)"
        return 0
    fi
    if ! nginx -T > "$TEMP_DIR/nginx-T.conf" 2> "$TEMP_DIR/nginx-T.log"; then
        diag_problem "nginx -T fails, so nginx cannot load a change: $(grep -m 1 'emerg' "$TEMP_DIR/nginx-T.log" || tail -n 1 "$TEMP_DIR/nginx-T.log")"
        return 0
    fi
    diag_servers > "$TEMP_DIR/servers"
    for port in 80 443; do
        files=$(awk -F "$SEP" -v port="$port" '$1 == "L" && $2 == port && !seen[$3]++ { out = out (out == "" ? "" : ", ") $3 } END { print out }' "$TEMP_DIR/servers")
        if awk -F "$SEP" -v port="$port" -v ours="$NGINX_CONF" '$1 == "L" && $2 == port && $3 == ours { found = 1 } END { exit !found }' "$TEMP_DIR/servers"; then
            diag_ok "port $port: $files"
        elif [[ -n $files ]]; then
            diag_problem "port $port: $NGINX_CONF does not listen on it, only $files: run apply"
        else
            diag_problem "port $port: no nginx file listens on it: run apply"
        fi
    done
    while IFS=$SEP read -r port file; do
        extra=''
        [[ $file != */sites-enabled/default ]] || extra=" (Ubuntu's default site: the \"Welcome to nginx\" page)"
        diag_note "$file is the default server on port $port: it answers every other host name and the bare address$extra; a front that changes the Host header gets it, not the App"
    done < <(awk -F "$SEP" -v sep="$SEP" -v ours="$NGINX_CONF" '$1 == "L" && $4 == 1 && $3 != ours && !seen[$2 sep $3]++ { print $2 sep $3 }' "$TEMP_DIR/servers")
    while IFS=$SEP read -r kind file names; do
        diag_note "$file, a file that apply did not write, serves host names of the table: $names. apply refuses a host name that another nginx file serves, and for one host name and port nginx uses the file that it reads first. To serve them with the routes of the table, first add http to each route that a TLS front reaches on port 80 (README: Behind a TLS front on port 80), then remove $file and run apply"
    done < <(grep "^N$SEP" "$TEMP_DIR/servers" || true)
}

diag_own_address() {
    local words
    [[ $1 != 127.* ]] && [[ $1 != 0.0.0.0 ]] || return 0
    words=" $(hostname -I 2> /dev/null || true) "
    if command -v ip > /dev/null; then
        words+=" $(ip -o addr show 2> /dev/null | awk '{ sub(/\/.*/, "", $4); printf "%s ", $4 }' || true) "
    fi
    [[ $words == *" $1 "* ]]
}

diag_headers() {
    awk -v sep="$SEP" '
        { sub(/\r$/, "") }
        /^HTTP\// { n++; split($0, words, " "); code[n] = words[2]; next }
        n && index($0, ":") {
            name = tolower(substr($0, 1, index($0, ":") - 1))
            value = substr($0, index($0, ":") + 1)
            sub(/^[ \t]+/, "", value)
            sub(/[ \t]+$/, "", value)
            if (name == "server") server[n] = value
            else if (name == "location") location[n] = value
        }
        END { if (n) print code[1] sep server[1] sep location[1] sep server[n] }' "$1"
}

diag_front() {
    local host=$1 path=$2 here=$3 http=$4 url=https://$1$2 address port label code status=0 first='' server='' location='' last=''
    if [[ -n $FRONT_HOST ]]; then
        address=$FRONT_HOST port=$FRONT_PORT label=$FRONT
    else
        if ! command -v getent > /dev/null; then
            diag_note "$host: getent is not installed: the way through a front was not checked (pass --front ADDRESS)"
            return 0
        fi
        address=$(getent ahostsv4 "$host" 2> /dev/null | awk 'NR == 1 { print $1 }' || true)
        if [[ -z $address ]]; then
            diag_note "$host: the system resolver gives no IPv4 address: the way through a front was not checked (pass --front ADDRESS)"
            return 0
        fi
        if diag_own_address "$address"; then
            diag_note "$host: the system resolver gives $address, an address of this server: no front to check (pass --front ADDRESS to check one)"
            return 0
        fi
        port=443 label=$address
    fi
    : > "$TEMP_DIR/front.headers"
    code=$(diag_curl -k -L --max-redirs 5 -D "$TEMP_DIR/front.headers" -o /dev/null -w '%{http_code}' \
        --connect-to "$host:443:$address:$port" "$url" 2> "$TEMP_DIR/curl.log") || status=$?
    IFS=$SEP read -r first server location last < <(diag_headers "$TEMP_DIR/front.headers") || true
    [[ $location != /* ]] || location=https://$host$location
    if ((status == 47)) || [[ $first == 3?? && $location == "$url" ]]; then
        if [[ $http == http ]]; then
            diag_problem "$host $path: the front at $label (Server: ${server:-none}) gets a redirect to the same address again and again, although the route has http: a redirect loop. Make port 80 serve the route (see the HTTP line of this route), then ask the front's owner to check its entry for $host"
        else
            diag_problem "$host $path: the front at $label (Server: ${server:-none}) sends HTTPS requests to port 80 of this server, where this route redirects to https://: a redirect loop. Ask the front's owner to forward HTTPS to port 443 with the original Host header, or add http to this route (README: Behind a TLS front on port 80)"
        fi
    elif ((status)) || [[ $code == 000 ]]; then
        diag_note "$host: no answer through the front at $label ($(diag_curl_error)): this way was not checked from this server"
    elif [[ $code == 2?? ]]; then
        diag_ok "$host $path: through the front at $label answers $code (Server: ${last:-none})"
    elif [[ $code == "$here" && $code != 5?? ]]; then
        diag_ok "$host $path: through the front at $label answers $code (Server: ${last:-none}), as this server does"
    else
        diag_problem "$host: the front at $label answers $code (Server: ${last:-none}), this server answers $here over HTTPS: ask the front's owner to check its entry for $host"
    fi
}

diag_hosts() {
    local i host path url code answer plain location
    local -A picked=()
    printf 'Host names:\n'
    if ! command -v curl > /dev/null; then
        diag_note 'curl is not installed (apt-get install curl): the host names were not checked'
        return 0
    fi
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_PORT[i]} != - ]] || continue
        host=${R_HOST[i]}
        if [[ -z ${picked[$host]:-} ]] || [[ ${R_HTTP[${picked[$host]}]} == http && ${R_HTTP[i]} != http ]]; then
            picked[$host]=$i
        fi
    done
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        host=${R_HOST[i]} path=${R_PATH[i]}
        url=https://$host$path
        if [[ ${R_PORT[i]} == - ]]; then
            diag_note "$host $path: port - (not known yet): not checked"
            continue
        fi
        code=$(diag_curl -k -o /dev/null -w '%{http_code}' --resolve "$host:443:127.0.0.1" "$url" 2> "$TEMP_DIR/curl.log" || true)
        case $code in
            000|'') diag_problem "$host $path: no HTTPS answer on this server ($(diag_curl_error))" ;;
            5??) diag_problem "$host $path: HTTPS on this server answers $code: nginx gets no good answer from 127.0.0.1:${R_PORT[i]}" ;;
            *) diag_ok "$host $path: HTTPS on this server answers $code" ;;
        esac
        answer=$(diag_curl -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$host:80:127.0.0.1" "http://$host$path" \
            2> "$TEMP_DIR/curl.log" || true)
        plain=${answer%% *} location=${answer#* }
        [[ $answer == *' '* ]] || location=''
        if [[ $plain == 000 || -z $answer ]]; then
            diag_problem "$host $path: no HTTP answer on this server ($(diag_curl_error))"
        elif [[ ${R_HTTP[i]} == http && $plain == 301 && $location == "$url" ]]; then
            diag_problem "$host $path: HTTP on this server answers 301 to $url, but the route has http, so port 80 must serve it for a TLS front: run apply (http was added after the last apply), or another nginx file serves $host on port 80 (see nginx files)"
        elif [[ ${R_HTTP[i]} == http && $plain == "$code" ]]; then
            diag_ok "$host $path: HTTP on this server answers $plain: served on port 80 too, for a TLS front (http)"
        elif [[ ${R_HTTP[i]} == http ]]; then
            diag_problem "$host $path: HTTP on this server answers $plain${location:+ to $location}, HTTPS answers ${code:-000}: with http, port 80 must answer as HTTPS does (apply)"
        elif [[ $plain == 301 && $location == "$url" ]]; then
            diag_ok "$host $path: HTTP on this server answers 301 to $url"
        elif [[ $plain == "$code" ]]; then
            diag_problem "$host $path: HTTP on this server answers $plain as HTTPS does, but the route has no http: another nginx file serves $host on port 80 (see nginx files), or http was removed after the last apply. If a TLS front connects on port 80 for this route, first add http to the route (README: Behind a TLS front on port 80), else apply makes port 80 redirect it and the front loops. Then remove the other nginx file, if there is one, and run apply"
        else
            diag_problem "$host $path: HTTP on this server answers $plain${location:+ to $location}, not 301 to $url: without http on the route, port 80 must only send browsers to HTTPS (apply)"
        fi
        [[ ${picked[$host]:-} != "$i" ]] || diag_front "$host" "$path" "$code" "${R_HTTP[i]}"
    done
}

diag_access_summary() {
    awk -v sep="$SEP" -v agent="\"$DIAG_AGENT\"" -v now="$1" -v recent="$DIAG_RECENT" '
        function day_number(y, m, d) {
            if (m <= 2) { y--; m += 12 }
            return 365 * y + int(y / 4) - int(y / 100) + int(y / 400) + int((153 * (m - 3) + 2) / 5) + d
        }
        function seconds(t,   m, zone) {
            if (t !~ /^[0-9][0-9]\/[A-Z][a-z][a-z]\/[0-9][0-9][0-9][0-9]:[0-9][0-9]:[0-9][0-9]:[0-9][0-9] [+-][0-9][0-9][0-9][0-9]$/) return -1
            m = index("JanFebMarAprMayJunJulAugSepOctNovDec", substr(t, 4, 3))
            if (m == 0 || (m - 1) % 3) return -1
            zone = substr(t, 23, 2) * 3600 + substr(t, 25, 2) * 60
            if (substr(t, 22, 1) == "-") zone = -zone
            return (day_number(substr(t, 8, 4) + 0, (m + 2) / 3, substr(t, 1, 2) + 0) - epoch) * 86400 \
                + substr(t, 13, 2) * 3600 + substr(t, 16, 2) * 60 + substr(t, 19, 2) - zone
        }
        BEGIN { epoch = day_number(1970, 1, 1) }
        {
            lines++
            if (index($0, agent)) { own++; next }
            a = index($0, " [")
            if (a == 0) { bad++; next }
            rest = substr($0, a + 2)
            b = index(rest, "] \"")
            if (b == 0) { bad++; next }
            time = substr(rest, 1, b - 1)
            rest = substr(rest, b + 3)
            c = index(rest, "\" ")
            if (c == 0) { bad++; next }
            request = substr(rest, 1, c - 1)
            status = substr(rest, c + 2, 4)
            t = seconds(time)
            if (status !~ /^[0-9][0-9][0-9] $/ || t < 0) { bad++; next }
            if (status != "301 ") next
            browser = substr(rest, c + 6)
            if (index(browser, "\" \"")) { sub(/.*" "/, "", browser); sub(/"[ \t]*$/, "", browser) } else browser = ""
            sender = $1
            moved++
            answers[sender]++
            last[sender] = request sep time
            key = sender sep request sep browser
            count[key]++
            if (count[key] >= 5 && t - fourth[key] <= 10 && fourth[key] - t <= 10) {
                looped[key] = 1
                at[key] = t
                line[key] = NR
                when[key] = time
            }
            fourth[key] = third[key]
            third[key] = second[key]
            second[key] = first[key]
            first[key] = t
        }
        END {
            printf "S%s%d%s%d%s%d%s%d\n", sep, lines, sep, bad, sep, moved, sep, own
            for (s in answers) printf "T%s%d%s%s%s%s\n", sep, answers[s], sep, s, sep, last[s]
            for (key in looped) {
                split(key, k, sep)
                group = (now - at[key] <= recent ? "L" : "O") sep k[1]
                total[group] += count[key]
                if (line[key] > top[group]) {
                    top[group] = line[key]
                    latest[group] = k[2] sep when[key] sep int((now - at[key]) / 60)
                }
            }
            for (group in total) {
                split(group, g, sep)
                printf "%s%s%d%s%s%s%s\n", g[1], sep, total[group], sep, g[2], sep, latest[group]
            }
        }'
}

diag_access_log() {
    local f=$ACCESS_LOG kind n sender request time minutes lines=0 bad=0 moved=0 own=0 shown=0 more=0 earlier=0 older=0 skipped=''
    printf 'Access log:\n'
    case $ACCESS_STATE in
        missing)
            diag_note "$f does not exist: the access log was not checked (--access-log FILE)"
            return 0 ;;
        unreadable)
            diag_note "$f cannot be read: the access log was not checked"
            return 0 ;;
    esac
    if ! diag_access_summary "$(date +%s)" < "$f" > "$TEMP_DIR/access" 2> /dev/null; then
        diag_note "$f cannot be read: the access log was not checked"
        return 0
    fi
    IFS=$SEP read -r kind lines bad moved own < <(grep "^S$SEP" "$TEMP_DIR/access") || true
    if ((lines > own && bad == lines - own)); then
        diag_note "none of the $((lines - own)) lines of $f is in nginx's combined format: the access log was not checked"
        return 0
    fi
    while IFS=$SEP read -r kind n sender request time minutes; do
        if ((shown < 5)); then
            diag_problem "redirect loop from $sender ($n answers, last: $request at $time): it forwards HTTPS requests to port 80 of this server. Ask the front's owner to forward HTTPS to port 443 with the original Host header, or add http to the route of this request (README: Behind a TLS front on port 80)"
            shown=$((shown + 1))
        else
            more=$((more + 1))
        fi
    done < <(grep "^L$SEP" "$TEMP_DIR/access" | sort -t "$SEP" -k2,2nr -k3,3)
    ((more == 0)) || diag_note "$more more senders with a redirect loop"
    if ((shown == 0)); then
        ((bad == 0)) || skipped+=", $bad lines in another format skipped"
        ((own == 0)) || skipped+=", $own requests of diagnose itself skipped"
        if grep -q "^O$SEP" "$TEMP_DIR/access"; then
            diag_ok "no redirect loop in the last $((DIAG_RECENT / 60)) minutes of $f ($lines lines, $moved answers 301$skipped)"
        else
            diag_ok "no redirect loop in $f ($lines lines, $moved answers 301$skipped)"
        fi
    fi
    while IFS=$SEP read -r kind n sender request time minutes; do
        if ((earlier < 5)); then
            diag_note "earlier redirect loop from $sender ($n answers, last: $request at $time, $minutes minutes before this run): it forwarded HTTPS requests to port 80 of this server then"
            earlier=$((earlier + 1))
        else
            older=$((older + 1))
        fi
    done < <(grep "^O$SEP" "$TEMP_DIR/access" | sort -t "$SEP" -k2,2nr -k3,3)
    ((older == 0)) || diag_note "$older more senders with an earlier redirect loop"
    while IFS=$SEP read -r kind n sender request time; do
        diag_note "$sender: $n answers 301, the last to $request at $time"
    done < <(grep "^T$SEP" "$TEMP_DIR/access" | sort -t "$SEP" -k2,2nr -k3,3 | head -n 5)
}

diag_workers() {
    local f=$ERROR_LOG count last
    printf 'nginx workers:\n'
    case $ERROR_STATE in
        missing)
            diag_note "$f does not exist: the nginx workers were not checked (--error-log FILE)"
            return 0 ;;
        unreadable)
            diag_note "$f cannot be read: the nginx workers were not checked"
            return 0 ;;
    esac
    count=$(grep -c -- 'exited on signal' "$f" 2> /dev/null || true)
    if [[ ${count:-0} == 0 ]]; then
        diag_ok "no nginx worker exited on a signal ($f)"
        return 0
    fi
    last=$(grep -- 'exited on signal' "$f" | tail -n 1)
    diag_note "$count nginx worker exit(s) on a signal in $f (nginx starts a new worker each time); the last: $last"
}

cmd_diagnose() {
    local pattern='^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)(:([0-9]{1,5}))?$'
    if [[ -n $FRONT ]]; then
        [[ $FRONT =~ $pattern ]] \
            || die "--front must be ADDRESS or ADDRESS:PORT (an IPv6 address in brackets, like [2001:db8::1]:443), not $FRONT"
        FRONT_HOST=${BASH_REMATCH[1]} FRONT_PORT=${BASH_REMATCH[4]:-443}
        ((10#$FRONT_PORT >= 1 && 10#$FRONT_PORT <= 65535)) || die "--front: $FRONT has no valid port"
        FRONT_PORT=$((10#$FRONT_PORT))
    fi
    load_routes
    ACCESS_STATE=$(diag_file_state "$ACCESS_LOG") ERROR_STATE=$(diag_file_state "$ERROR_LOG")
    diag_ports
    diag_nginx_files
    diag_hosts
    diag_access_log
    diag_workers
    if ((PROBLEMS)); then
        printf 'Result: %s problem(s) found\n' "$PROBLEMS"
        exit 4
    fi
    printf 'Result: no problem found\n'
}

cmd_uninstall() {
    local f host generation owner timer=''
    lock
    [[ ! -e $NGINX_CONF ]] || applied || die "$NGINX_CONF was not written by apply for $STATE_DIR: it is left alone. Nothing was changed."
    owner=$(renewal_dir)
    [[ -z $owner || $owner != "$STATE_DIR" ]] || timer=", removes the Let's Encrypt renewal timer"
    printf 'This removes %s and reloads nginx: HTTPS stops for its host names%s%s.\n' "$NGINX_CONF" "$timer" \
        "$( ((PURGE)) && printf ', and deletes the key, certificate, request and routes in %s%s' "$STATE_DIR" \
            "$([[ ! -d $STATE_DIR/letsencrypt ]] || printf ", and the Let's Encrypt accounts and certificates")")"
    if [[ -z $CONFIRM ]]; then
        [[ -t 0 ]] || die 'No terminal to confirm on: pass --confirm yes. Nothing was changed.'
        read -r -p 'Type yes to confirm: ' CONFIRM || die 'Cancelled'
    fi
    [[ $CONFIRM == yes ]] || die 'Not confirmed. Nothing was changed.'
    if [[ -f $NGINX_CONF ]]; then
        host=$(sed -n '/^    server_name /{s/^    server_name \([^ ;]*\).*/\1/p;q}' "$NGINX_CONF")
        generation=$(conf_generation)
        save "$NGINX_CONF"
        rm -f -- "$NGINX_CONF"
        if command -v nginx > /dev/null; then
            if ! nginx -t > "$TEMP_DIR/nginx-t.log" 2>&1; then
                cat "$TEMP_DIR/nginx-t.log" >&2
                die 'nginx -t fails without this configuration (above): fix that first. Nothing was changed.'
            fi
            if nginx_running; then
                RELOADED=1
                reload_nginx
                [[ -z $generation ]] || within_10s dropped "$host" "$generation" \
                    || die 'nginx still runs this configuration, or did not answer, after about 10 seconds (see: sudo journalctl -u nginx, /var/log/nginx/error.log). Nothing was changed.'
            fi
        fi
        keep
    fi
    remove_renewal
    if ((PURGE)); then   # only this script's files: never the whole of --dir
        for f in "${STATE_FILES[@]}"; do
            rm -f -- "${STATE_DIR:?}/$f" "${STATE_DIR:?}/$f.tmp" "${STATE_DIR:?}/$f.saved"
        done
        rm -rf -- "${STATE_DIR:?}/letsencrypt"   # all of it this script's and lego's
        rmdir -- "$STATE_DIR" 2> /dev/null || true
    fi
    printf 'Removed.\n'
}

COMMAND=${1:-}
[[ -n $COMMAND ]] || { usage >&2; exit 2; }
shift
case $COMMAND in -h|--help|help) usage; exit 0 ;; esac
while (($#)); do
    case $1 in
        --routes|--dir|--nginx-conf|--subject|--key|--confirm|--server|--acme-dns|--dns-resolvers|--email|--extra-root|--acme-ca|--unit-dir|--lib-dir|--front|--access-log|--error-log)
            (($# >= 2)) || die "Missing value for $1"
            case $1 in
                --routes) ROUTES=$2 ;;
                --dir) STATE_DIR=$2 ;;
                --nginx-conf) NGINX_CONF=$2 ;;
                --subject) SUBJECT=$2 ;;
                --key) KEY_FILE=$2 ;;
                --confirm) CONFIRM=$2 ;;
                --server) GIVEN[server]=$2 ;;
                --acme-dns) GIVEN[acmedns]=$2 ;;
                --dns-resolvers) GIVEN[resolvers]=$2 ;;
                --email) GIVEN[email]=$2 ;;
                --extra-root) GIVEN[extraroot]=$2 ;;
                --acme-ca) GIVEN[acmeca]=$2 ;;
                --unit-dir) UNIT_DIR=$2 ;;   # --unit-dir and --lib-dir: for the tests
                --lib-dir) LIB_DIR=$2 ;;
                --front) [[ -n $2 ]] || die '--front needs ADDRESS or ADDRESS:PORT'; FRONT=$2 DIAG_GIVEN=1 ;;
                --access-log) ACCESS_LOG=$2 DIAG_GIVEN=1 ;;
                --error-log) ERROR_LOG=$2 DIAG_GIVEN=1 ;;
            esac
            shift 2 ;;
        --new-key) NEW_KEY=1; shift ;;
        --purge) PURGE=1; shift ;;
        --renew) RENEW=1; shift ;;
        --accept-tos) ACCEPT_TOS=1; shift ;;
        --manual) MANUAL=1; shift ;;
        --no-dns-check) NO_DNS_CHECK=1; shift ;;
        -*) die "Unknown option: $1 (see: bash https.sh --help)" ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
[[ $COMMAND == csr ]] || [[ $NEW_KEY == 0 && -z $KEY_FILE && $SUBJECT == "$DEFAULT_SUBJECT" ]] || die '--new-key, --key and --subject belong to csr'
[[ $NEW_KEY == 0 || -z $KEY_FILE ]] || die 'Use --new-key or --key, not both'
[[ $COMMAND == uninstall ]] || [[ $PURGE == 0 && -z $CONFIRM ]] || die '--purge and --confirm belong to uninstall'
[[ $COMMAND == letsencrypt ]] || [[ ${#GIVEN[@]} == 0 && $RENEW == 0 && $ACCEPT_TOS == 0 && $MANUAL == 0 && $NO_DNS_CHECK == 0 ]] \
    || die '--renew, --manual, --no-dns-check, --accept-tos, --email, --server, --acme-dns, --dns-resolvers, --extra-root and --acme-ca belong to letsencrypt'
((RENEW == 0 || MANUAL == 0)) || die '--manual has no --renew: to renew, run letsencrypt --manual again, and it prints new records'
((NO_DNS_CHECK == 0 || MANUAL == 1)) || die '--no-dns-check belongs to --manual'
((DIAG_GIVEN == 0)) || [[ $COMMAND == diagnose ]] || die '--front, --access-log and --error-log belong to diagnose'
[[ $COMMAND == install-cert ]] || ((${#ARGS[@]} == 0)) || die "Unexpected argument: ${ARGS[0]}"
((${#ARGS[@]} <= 2)) || die 'install-cert takes a certificate file and, optionally, a chain file'
[[ $STATE_DIR == /* ]] || die '--dir must be an absolute path'
STATE_DIR=$(realpath -m -- "$STATE_DIR")
[[ $STATE_DIR != / ]] || die '--dir cannot be /'
[[ -z $ROUTES ]] || ROUTES=$(realpath -m -- "$ROUTES")
[[ $UNIT_DIR == /* && $LIB_DIR == /* ]] || die '--unit-dir and --lib-dir must be absolute paths'
UNIT_DIR=$(realpath -m -- "$UNIT_DIR") LIB_DIR=$(realpath -m -- "$LIB_DIR")
[[ -z $KEY_FILE ]] || [[ -f $KEY_FILE ]] || die "No such file: $KEY_FILE"
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'
command -v openssl > /dev/null || die 'openssl is required'
TEMP_DIR=$(mktemp -d)

case $COMMAND in
    csr) cmd_csr ;;
    install-cert) cmd_install_cert ;;
    letsencrypt) cmd_letsencrypt ;;
    apply) cmd_apply ;;
    status) cmd_status ;;
    diagnose) cmd_diagnose ;;
    uninstall) cmd_uninstall ;;
    *) usage >&2; exit 2 ;;
esac
