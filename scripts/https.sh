#!/usr/bin/env bash
# HTTPS on this server: nginx terminates TLS for the host names in a routes table
# and forwards each path to a local port. The table drives everything: the
# certificate request (csr), the certificate check (install-cert) and the nginx
# configuration (apply).
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077
PATH=$PATH:/usr/local/sbin:/usr/sbin:/sbin   # cron, su and some sudo setups leave these out; nginx lives there

STATE_DIR=/etc/perodua-https
NGINX_CONF=/etc/nginx/conf.d/perodua-https.conf
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMAND='' ROUTES='' ROUTES_FILE='' SUBJECT='/C=MY/O=Perodua' NEW_KEY=0 KEY_FILE='' CONFIRM='' PURGE=0 TEMP_DIR='' RELOADED=0
ARGS=() HOSTS=() R_HOST=() R_PATH=() R_PORT=() R_STRIP=() SAVED=()
# In STATE_DIR: routes.conf (the table), key.pem (the private key nginx uses),
# key.new.pem (a new key waiting for its certificate), request.csr, fullchain.pem.
STATE_FILES=(key.pem key.new.pem request.csr fullchain.pem routes.conf)

usage() {
    cat <<'HELP'
Usage: sudo bash https.sh csr [--new-key | --key FILE] [--subject /C=MY/O=NAME]
       sudo bash https.sh install-cert CERT [CHAIN]
       sudo bash https.sh apply
       sudo bash https.sh status
       sudo bash https.sh uninstall [--purge] [--confirm yes]
Every command also takes --routes FILE (default /etc/perodua-https/routes.conf)
and --dir DIR (default /etc/perodua-https).

The routes table has one line per host name and path: HOST PATH PORT [strip].
The first run creates it from https-routes.conf.example and stops, so that it
can be filled in.

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
apply         Writes the nginx configuration: port 80 redirects to HTTPS, and on
              443 every host forwards its paths to 127.0.0.1:PORT, the path
              unchanged or, with strip, removed. Every other path answers 404.
              nginx is installed with apt-get if needed; nothing else, not even
              an nginx of another installation, may listen on port 80 or 443.
              The change is kept only if nginx -t accepts it and nginx then runs
              it. Every PORT must be known and the certificate installed.
status        The certificate, nginx and every route.
uninstall     Removes the nginx configuration of this script and reloads nginx.
              --purge also deletes the key, certificate, request and routes.
              Type yes to confirm, or pass --confirm yes.
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
load_routes() {   # HOST PATH PORT [strip] per line; '#' starts a comment
    local line host path port option extra n=0 i
    local fqdn='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' prefix='^/([A-Za-z0-9._~-]+/)*$' number='^[1-9][0-9]{0,4}$'
    ROUTES_FILE=${ROUTES:-$STATE_DIR/routes.conf}
    if [[ ! -f $ROUTES_FILE ]]; then
        [[ -z $ROUTES ]] || die "No routes file $ROUTES_FILE"
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
        [[ -z $option || $option == strip ]] || die "$ROUTES_FILE:$n: the only option is strip"
        [[ -z $extra ]] || die "$ROUTES_FILE:$n: too many columns"
        for ((i = 0; i < ${#R_HOST[@]}; i++)); do
            [[ ${R_HOST[i]} != "$host" || ${R_PATH[i]} != "$path" ]] || die "$ROUTES_FILE:$n: $host $path is listed twice"
        done
        R_HOST+=("$host") R_PATH+=("$path") R_PORT+=("$port") R_STRIP+=("$option")
        [[ " ${HOSTS[*]} " == *" $host "* ]] || HOSTS+=("$host")
    done < "$ROUTES_FILE"
    ((${#HOSTS[@]})) || die "$ROUTES_FILE lists no host name"
}

# ---------------------------------------------------------------- keys and certificates
pub_of() {   # $1 key or cert, $2 file: SHA-256 of its public key, nothing if it has none
    local out
    if [[ $1 == key ]]; then
        out=$(openssl pkey -in "$2" -passin pass: -pubout 2> /dev/null) || return 0   # never asks for a pass phrase
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

foreign_names() {   # the table's host names that another nginx file serves, as FILE: NAME
    local dump
    dump=$(nginx -T 2> "$TEMP_DIR/nginx-T.log") || { cat "$TEMP_DIR/nginx-T.log" >&2; die 'The current nginx configuration fails nginx -t: fix it first'; }
    # Words as nginx reads them: quoted or not, a # outside quotes at the start of
    # a word begins a comment, and ; { } end a statement, so every server_name
    # statement is found, whether it shares its line or spans lines.
    awk -v ours="$NGINX_CONF" -v hosts=" ${HOSTS[*]} " '
        function take(w) {
            if (w == "") return
            if (names) { w = tolower(w); if (file != ours && index(hosts, " " w " ")) print file ": " w }
            else if (w == "server_name") names = 1
        }
        /^# configuration file / { file = substr($0, 22); sub(/:$/, "", file); names = 0; quote = ""; next }
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
                else if (c ~ /[ \t;{}]/) { take(word); word = ""; if (c ~ /[;{}]/) names = 0 }
                else word = word c
            }
            if (quote == "") take(word)
        }' <<< "$dump"
}

render() {   # the nginx configuration for the table; its generation is a hash of the rest
    local text generation
    text=$(nginx_conf)
    generation=$(sha256sum <<< "$text" | cut -c1-16)
    printf '%s\n' "${text//@GENERATION@/$generation}"
}

# The $ names in single quotes below are nginx variables, not shell ones; in the
# here-document the shell fills in $name and $STATE_DIR and leaves \$ to nginx.
# shellcheck disable=SC2016
nginx_conf() {   # the configuration, with @GENERATION@ where its generation goes
    local name i path root
    printf '# Written by https.sh from %s. Do not edit: change the routes, then run: sudo bash https.sh apply\n\n' "$ROUTES_FILE"
    printf 'map $http_upgrade $perodua_https_connection {\n    default upgrade;\n    %s close;\n}\n\n' "''"
    printf 'server {\n    listen 80;\n    server_name %s;\n    return 301 https://$host$request_uri;\n}\n' "${HOSTS[*]}"
    for name in "${HOSTS[@]}"; do
        root=0
        cat <<NGINX

server {
    listen 443 ssl;
    server_name $name;
    ssl_certificate $STATE_DIR/fullchain.pem;
    ssl_certificate_key $STATE_DIR/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:perodua_https:10m;
    ssl_session_timeout 1d;

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

    # Which configuration nginx runs, for https.sh on this server only.
    location = /.perodua-https {
        if (\$remote_addr != 127.0.0.1) {
            return 404;
        }
        return 200 "generation @GENERATION@";
    }
NGINX
        for ((i = 0; i < ${#R_HOST[@]}; i++)); do
            [[ ${R_HOST[i]} == "$name" ]] || continue
            path=${R_PATH[i]}
            [[ $path != / ]] || root=1
            # With a URI part (the trailing /) nginx replaces the matched path: strip.
            # nginx itself redirects the path without its last / (301, query kept).
            printf '\n    location %s {\n        proxy_pass http://127.0.0.1:%s%s;\n    }\n' \
                "$path" "${R_PORT[i]}" "$([[ ${R_STRIP[i]} == strip ]] && printf /)"
        done
        ((root)) || printf '\n    # Every other path is closed.\n    location / {\n        return 404;\n    }\n'
        printf '}\n'
    done
}

# ---------------------------------------------------------------- commands
cmd_csr() {
    local key san subject='^(/[A-Za-z]+=[^/=]+)+$'
    [[ $SUBJECT =~ $subject ]] || die '--subject looks like /C=MY/O=Company Name'
    [[ ! ${SUBJECT^^} =~ /CN= ]] || die '--subject must not hold CN: the first host name of the table is the CN'
    lock
    load_routes
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
    printf 'Certificate request for %s (CN %s), saved as %s.\n' "${HOSTS[*]}" "${HOSTS[0]}" "$STATE_DIR/request.csr"
    printf 'Send this request to the certificate issuer. The private key (%s) stays on this server:\n\n' "$key"
    cat -- "$STATE_DIR/request.csr"
    [[ -z $KEY_FILE ]] || printf '\nIf a request made with %s was already sent, wait for its certificate instead of sending this one.\n' "$KEY_FILE"
    printf '\nWhen the certificate comes back: sudo bash https.sh install-cert FILE [CHAIN]\n'
}

cmd_install_cert() {
    local cert=${ARGS[0]:-} chain_file=${ARGS[1]:-} key leaf='' kp f missing top anchor purposes chain=() anchors=() args=()
    [[ -n $cert ]] || die 'Usage: sudo bash https.sh install-cert CERT [CHAIN]'
    lock
    load_routes
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
    { openssl x509 -in "$leaf"; for f in "${chain[@]}"; do openssl x509 -in "$f"; done; } > "$TEMP_DIR/fullchain.pem"
    save "$STATE_DIR/fullchain.pem" "$STATE_DIR/key.pem" "$STATE_DIR/key.new.pem"
    install -m 0644 -- "$TEMP_DIR/fullchain.pem" "$STATE_DIR/fullchain.pem.tmp"
    mv -f -- "$STATE_DIR/fullchain.pem.tmp" "$STATE_DIR/fullchain.pem"
    [[ $key != "$STATE_DIR/key.new.pem" ]] || mv -f -- "$STATE_DIR/key.new.pem" "$STATE_DIR/key.pem"
    if applied; then activate; fi   # apply ran before: nginx serves the new certificate now
    keep
    printf 'Installed the certificate for %s, issued by %s, valid until %s (%s days).\n' \
        "$(cert_names "$STATE_DIR/fullchain.pem" | paste -sd ' ' -)" "$(field issuer "$leaf")" "$(field enddate "$leaf")" "$(days_left "$leaf")"
    if applied; then printf 'nginx serves it now.\n'; else printf 'Next: sudo bash https.sh apply\n'; fi
}

cmd_apply() {
    local i unknown='' missing takers conflicts
    lock
    load_routes
    [[ ! -e $NGINX_CONF ]] || applied || die "$NGINX_CONF was not written by apply for $STATE_DIR: it is left alone"
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        [[ ${R_PORT[i]} != - ]] || unknown+="${R_HOST[i]}${R_PATH[i]} "
    done
    [[ -z $unknown ]] || die "Set the port of ${unknown% } in $ROUTES_FILE, or put # in front of those lines"
    [[ -f $STATE_DIR/fullchain.pem && -f $STATE_DIR/key.pem ]] \
        || die 'No certificate is installed yet: run csr, have the request signed, then install-cert'
    [[ $(pub_of cert "$STATE_DIR/fullchain.pem") == "$(pub_of key "$STATE_DIR/key.pem")" ]] \
        || die "The installed certificate does not belong to $STATE_DIR/key.pem"
    missing=$(missing_hosts "$STATE_DIR/fullchain.pem")
    [[ -z $missing ]] || die "The installed certificate does not cover ${missing% }: run csr, have the request signed, then install-cert"
    takers=$(port_takers)
    [[ -z $takers ]] || die "nginx needs ports 80 and 443, but something else listens there; stop or move it first (an nginx listed here is another installation than the one apply installs with apt):"$'\n'"$takers"
    ensure_nginx
    conflicts=$(foreign_names)
    [[ -z $conflicts ]] || die "Another nginx configuration already serves these host names; remove it first:"$'\n'"$conflicts"
    render > "$TEMP_DIR/perodua-https.conf"
    mkdir -p -- "${NGINX_CONF%/*}"
    save "$NGINX_CONF"
    install -m 0644 -- "$TEMP_DIR/perodua-https.conf" "$NGINX_CONF.tmp"
    mv -f -- "$NGINX_CONF.tmp" "$NGINX_CONF"
    activate
    keep
    printf 'nginx serves HTTPS for:\n'
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        printf '  https://%s%s -> 127.0.0.1:%s%s\n' "${R_HOST[i]}" "${R_PATH[i]}" "${R_PORT[i]}" \
            "$([[ ${R_STRIP[i]} == strip ]] && printf ' (path removed)')"
    done
    printf 'http:// redirects to https://. Check it with: sudo bash https.sh status\n'
    if command -v ufw > /dev/null && ufw status 2> /dev/null | grep -q 'Status: active'; then
        printf 'ufw is active: allow HTTP and HTTPS with: sudo ufw allow 80,443/tcp\n'
    fi
}

cmd_status() {
    local i f code listens takers pending=''
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
        printf 'nginx:       %s%s\n' "$NGINX_CONF" "$(cmp -s "$TEMP_DIR/now.conf" "$NGINX_CONF" || printf ' (the routes changed since: apply)')"
    fi
    takers=$(port_takers)   # what would stop apply
    [[ -z $takers ]] || printf 'Ports 80/443: in use by what apply cannot work next to; stop or move it first:\n             %s\n' \
        "${takers//$'\n'/$'\n'             }"
    printf 'Routes:\n'
    for ((i = 0; i < ${#R_HOST[@]}; i++)); do
        listens='-' code='-'
        if [[ ${R_PORT[i]} != - ]]; then
            listens=NO
            if [[ -n $(ss -ltnH "sport = :${R_PORT[i]}" 2> /dev/null) ]]; then listens=yes; fi
            if [[ -f $NGINX_CONF ]] && command -v curl > /dev/null; then
                code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "${R_HOST[i]}:443:127.0.0.1" \
                    "https://${R_HOST[i]}${R_PATH[i]}" || true)
            fi
        fi
        printf '  https://%s%s -> 127.0.0.1:%s  port listening: %s  HTTPS answer: %s\n' \
            "${R_HOST[i]}" "${R_PATH[i]}" "${R_PORT[i]}" "$listens" "$code"
    done
}

cmd_uninstall() {
    local f host generation
    lock
    [[ ! -e $NGINX_CONF ]] || applied || die "$NGINX_CONF was not written by apply for $STATE_DIR: it is left alone. Nothing was changed."
    printf 'This removes %s and reloads nginx: HTTPS stops for its host names%s.\n' "$NGINX_CONF" \
        "$( ((PURGE)) && printf ', and deletes the key, certificate, request and routes in %s' "$STATE_DIR")"
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
    if ((PURGE)); then   # only this script's files: never the whole of --dir
        for f in "${STATE_FILES[@]}"; do
            rm -f -- "${STATE_DIR:?}/$f" "${STATE_DIR:?}/$f.tmp" "${STATE_DIR:?}/$f.saved"
        done
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
        --routes|--dir|--nginx-conf|--subject|--key|--confirm)
            (($# >= 2)) || die "Missing value for $1"
            case $1 in
                --routes) ROUTES=$2 ;;
                --dir) STATE_DIR=$2 ;;
                --nginx-conf) NGINX_CONF=$2 ;;
                --subject) SUBJECT=$2 ;;
                --key) KEY_FILE=$2 ;;
                --confirm) CONFIRM=$2 ;;
            esac
            shift 2 ;;
        --new-key) NEW_KEY=1; shift ;;
        --purge) PURGE=1; shift ;;
        -*) die "Unknown option: $1 (see: bash https.sh --help)" ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
[[ $COMMAND == csr ]] || [[ $NEW_KEY == 0 && -z $KEY_FILE && $SUBJECT == '/C=MY/O=Perodua' ]] || die '--new-key, --key and --subject belong to csr'
[[ $NEW_KEY == 0 || -z $KEY_FILE ]] || die 'Use --new-key or --key, not both'
[[ $COMMAND == uninstall ]] || [[ $PURGE == 0 && -z $CONFIRM ]] || die '--purge and --confirm belong to uninstall'
[[ $COMMAND == install-cert ]] || ((${#ARGS[@]} == 0)) || die "Unexpected argument: ${ARGS[0]}"
((${#ARGS[@]} <= 2)) || die 'install-cert takes a certificate file and, optionally, a chain file'
[[ $STATE_DIR == /* ]] || die '--dir must be an absolute path'
STATE_DIR=$(realpath -m -- "$STATE_DIR")
[[ $STATE_DIR != / ]] || die '--dir cannot be /'
[[ -z $ROUTES ]] || ROUTES=$(realpath -m -- "$ROUTES")
[[ -z $KEY_FILE ]] || [[ -f $KEY_FILE ]] || die "No such file: $KEY_FILE"
[[ $EUID -eq 0 ]] || die 'Run with sudo or as root.'
command -v openssl > /dev/null || die 'openssl is required'
TEMP_DIR=$(mktemp -d)

case $COMMAND in
    csr) cmd_csr ;;
    install-cert) cmd_install_cert ;;
    apply) cmd_apply ;;
    status) cmd_status ;;
    uninstall) cmd_uninstall ;;
    *) usage >&2; exit 2 ;;
esac
