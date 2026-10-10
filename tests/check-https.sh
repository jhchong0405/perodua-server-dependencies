#!/usr/bin/env bash
# CHECK && pass || fail: pass never fails, so fail runs exactly when the check does.
# shellcheck disable=SC2015
# End-to-end check of https.sh with real nginx and openssl, in a throwaway container:
#   docker run --rm -v "$PWD:/src:ro" ubuntu:24.04 bash /src/tests/check-https.sh
# A throwaway root and intermediate stand in for the certificate issuer, and echo
# servers on 127.0.0.1 stand in for the services: each answers with the path and
# headers it received. Needs internet access for apt. Exits non-zero on the first
# failed check.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
[[ -f /.dockerenv ]] || { echo 'Run this in a throwaway container: it installs nginx and changes /etc' >&2; exit 1; }
apt-get update -qq > /dev/null
apt-get install -y -qq openssl curl python3 iproute2 ca-certificates > /dev/null
cp -r /src/scripts /work
cd /work
STATE=/etc/perodua-https
RESOLVE=(--resolve api.example.perodua.com.my:443:127.0.0.1 --resolve stgissrp.perodua.com.my:443:127.0.0.1
         --resolve api.example.perodua.com.my:80:127.0.0.1 --resolve stgissrp.perodua.com.my:80:127.0.0.1)
step() { printf '\n### %s\n' "$*"; }
pass() { printf 'ok   %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; exit 1; }
expect() {   # $1 URL, $2 expected "CODE body-prefix", then extra curl options
    local got url=$1 want=$2
    shift 2
    got=$(curl -s --cacert /ca/root.pem "${RESOLVE[@]}" "$@" -o /tmp/body -w '%{http_code}' "$url" || true)
    got="$got $(head -c 200 /tmp/body 2> /dev/null | tr -d '\n')"
    [[ $got == "$want"* ]] && pass "$url -> $got" || fail "$url -> $got (expected $want...)"
}
sign() {   # $1: the certificate for the request in $STATE, from the intermediate
    openssl x509 -req -in "$STATE/request.csr" -CA /ca/int.pem -CAkey /ca/int.key -CAcreateserial \
        -days 60 -copy_extensions copyall -out "$1" 2> /dev/null
}
serves() {   # $1 host name, $2 certificate: nginx presents exactly that certificate for the name
    local got
    got=$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$1" 2> /dev/null | openssl x509 -noout -fingerprint -sha256) \
        && [[ $got == "$(openssl x509 -in "$2" -noout -fingerprint -sha256)" ]]
}
eventually() {   # a check that must pass within 5 seconds: nginx reloads in the background
    local i
    for ((i = 0; i < 25; i++)); do "$@" && return 0; sleep 0.2; done
    return 1
}
closed() { [[ $(curl -s --cacert /ca/root.pem "${RESOLVE[@]}" -o /dev/null -w '%{http_code}' https://api.example.perodua.com.my/dev/api/x || true) == 000 ]]; }
no_answer() {
    local status=0 code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$@") || status=$?
    [[ $code == 000 && $status == 52 ]] && pass "curl $* -> exit 52, no answer" || fail "curl $* -> exit $status, code $code (expected exit 52)"
}
burst() {
    local url args=()
    for url; do args+=(-o /dev/null "$url"); done
    curl -s --cacert /ca/root.pem "${RESOLVE[@]}" -w '%{http_code}\n' "${args[@]}"
}
tls12() { echo | openssl s_client -connect 127.0.0.1:443 -servername api.example.perodua.com.my -tls1_2 "$@" 2> /dev/null; }

python3 - > /dev/null 2>&1 << 'PY' &
import http.server, threading, time
class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f"port={self.server.server_port} path={self.path} proto={self.headers.get('X-Forwarded-Proto')}"
                f" host={self.headers.get('Host')} xff={self.headers.get('X-Forwarded-For')}\n").encode()
        self.send_response(200)
        # as Odoo sets its session cookie: no Secure, no SameSite
        self.send_header("Set-Cookie", "session_id=echo; Path=/dev/; HttpOnly")
        self.send_header("Set-Cookie", "frontend_lang=en_US; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
for port in (8000, 8001, 8110, 8111):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(3600)
PY
sleep 1

step 'the first run creates the routes table and stops'
if bash https.sh csr 2> /tmp/err; then fail 'csr ran without a routes table'; fi
grep -q "Created $STATE/routes.conf" /tmp/err && pass "$(cat /tmp/err)" || fail "no routes table was created"
cat > "$STATE/routes.conf" << 'EOF'
# the Oracle API under a path (the path is removed), and an App with two environments
api.example.perodua.com.my      /dev/api/   8000   strip
api.example.perodua.com.my      /uat/api/   8001   strip
stgissrp.perodua.com.my    /dev/       8110
stgissrp.perodua.com.my    /uat/       8111
EOF

step 'csr'
bash https.sh csr > /tmp/csr.out
grep -q 'BEGIN CERTIFICATE REQUEST' /tmp/csr.out && pass "$(head -1 /tmp/csr.out)" || fail 'no request printed'
[[ $(stat -c %a "$STATE/key.pem") == 600 ]] && pass 'the private key is 0600' || fail 'the private key is not 0600'
openssl req -in "$STATE/request.csr" -noout -text | grep -q 'DNS:api.example.perodua.com.my, DNS:stgissrp.perodua.com.my' \
    && pass 'the request names both hosts' || fail 'the request does not name both hosts'

step 'the issuer signs it and returns a DER PKCS #7 bundle with its chain (root first)'
mkdir /ca
openssl req -x509 -newkey rsa:2048 -nodes -keyout /ca/root.key -out /ca/root.pem -subj '/CN=Check Root' -days 30 \
    -addext basicConstraints=critical,CA:TRUE 2> /dev/null
printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n' > /ca/int.ext
openssl req -new -newkey rsa:2048 -nodes -keyout /ca/int.key -out /ca/int.csr -subj '/CN=Check Intermediate' 2> /dev/null
openssl x509 -req -in /ca/int.csr -CA /ca/root.pem -CAkey /ca/root.key -CAcreateserial -days 30 \
    -extfile /ca/int.ext -out /ca/int.pem
sign /ca/leaf1.pem
openssl crl2pkcs7 -nocrl -certfile /ca/root.pem -certfile /ca/leaf1.pem -certfile /ca/int.pem -outform DER -out /ca/bundle.p7b
bash https.sh install-cert /ca/bundle.p7b | tail -2

step 'apply refuses to start while another program listens on port 443'
python3 -m http.server 443 --bind 127.0.0.1 > /dev/null 2>&1 &
squatter=$!
sleep 1
if bash https.sh apply 2> /tmp/err; then fail 'apply ignored the program on port 443'; fi
grep -q '^443: python3' /tmp/err && pass "$(tr '\n' ' ' < /tmp/err)" || fail "the program on port 443 was not named: $(cat /tmp/err)"
! command -v nginx > /dev/null && pass 'nginx was not installed' || fail 'nginx was installed anyway'
kill "$squatter"
wait "$squatter" 2> /dev/null || true

step 'apply: installs nginx, writes the configuration, starts nginx'
bash https.sh apply
serves api.example.perodua.com.my /ca/leaf1.pem && pass 'nginx serves the certificate when apply returns' || fail 'not served yet'

step 'requests through nginx, trusting only the throwaway root (so the chain must be served)'
expect https://api.example.perodua.com.my/dev/api/customers '200 port=8000 path=/customers proto=https host=api.example.perodua.com.my xff=127.0.0.1'
expect https://api.example.perodua.com.my/uat/api/accounting/account_type/ '200 port=8001 path=/accounting/account_type/ proto=https'
expect 'https://stgissrp.perodua.com.my/dev/app/?a=1' '200 port=8110 path=/dev/app/?a=1 proto=https host=stgissrp.perodua.com.my'
expect https://stgissrp.perodua.com.my/uat/web/login '200 port=8111 path=/uat/web/login'
expect https://api.example.perodua.com.my/somewhere '404'
# what the caller claims in Host and X-Forwarded-For does not reach the service
expect https://api.example.perodua.com.my/dev/api/who '200 port=8000 path=/who proto=https host=api.example.perodua.com.my xff=127.0.0.1' \
    -H 'Host: API.Example.perodua.com.my:443' -H 'X-Forwarded-For: 192.0.2.1'
code=$(curl -s --cacert /ca/root.pem -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" 'https://stgissrp.perodua.com.my/dev?a=1')
[[ $code == '301 https://stgissrp.perodua.com.my/dev/?a=1' ]] && pass "/dev?a=1 -> $code" || fail "/dev?a=1 -> $code"
code=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" http://api.example.perodua.com.my/dev/api/x)
[[ $code == '301 https://api.example.perodua.com.my/dev/api/x' ]] && pass "http -> $code" || fail "http -> $code"
# Odoo's session cookie goes back over HTTPS only; other cookies stay as they are
headers=$(curl -s --cacert /ca/root.pem "${RESOLVE[@]}" -D - -o /dev/null https://stgissrp.perodua.com.my/dev/web/login | tr -d '\r')
grep -qx 'Set-Cookie: session_id=echo; Path=/dev/; HttpOnly; Secure; SameSite=Lax' <<< "$headers" \
    && pass 'session_id gets Secure and SameSite=Lax' || fail "session_id: $(grep -i '^set-cookie' <<< "$headers")"
grep -qx 'Set-Cookie: frontend_lang=en_US; Path=/' <<< "$headers" && pass 'other cookies are unchanged' \
    || fail "other cookies: $(grep -i '^set-cookie' <<< "$headers")"
# no HSTS: whether these names become HTTPS-only for browsers is the customer's decision
headers=$(curl -s --cacert /ca/root.pem "${RESOLVE[@]}" -D - -o /dev/null https://stgissrp.perodua.com.my/dev/web/login | tr -d '\r')
! grep -qi '^Strict-Transport-Security' <<< "$headers" && pass 'no HSTS' || fail 'HSTS sent'
# the generation of the configuration nginx runs, for https.sh on this server only
got=$(curl -s --cacert /ca/root.pem "${RESOLVE[@]}" https://api.example.perodua.com.my/.perodua-https)
[[ $got =~ ^generation\ [0-9a-f]{16}$ ]] && pass "/.perodua-https from 127.0.0.1 -> $got" || fail "/.perodua-https -> $got"
ip=$(ip -4 -o addr show scope global | awk '{ sub(/\/.*/, "", $4); print $4; exit }')
code=$(curl -s --cacert /ca/root.pem -o /dev/null -w '%{http_code}' --resolve "api.example.perodua.com.my:443:$ip" \
    https://api.example.perodua.com.my/.perodua-https)
[[ $code == 404 ]] && pass "/.perodua-https from $ip -> $code" || fail "/.perodua-https from $ip -> $code"

step 'port 443: other host names and the bare address get the connection closed'
no_answer -k --resolve evil.example:443:127.0.0.1 https://evil.example/dev/api/x
no_answer --cacert /ca/root.pem --resolve api.example.perodua.com.my:443:127.0.0.1 -H 'Host: evil.example' https://api.example.perodua.com.my/dev/api/x
no_answer -k https://127.0.0.1/dev/api/x
no_answer -k "https://$ip/dev/x"
expect https://api.example.perodua.com.my/dev/api/x '200 port=8000 path=/x proto=https host=api.example.perodua.com.my'
expect https://stgissrp.perodua.com.my/dev/x '200 port=8110 path=/dev/x proto=https host=stgissrp.perodua.com.my'
expect https://127.0.0.1/dev/x '200 port=8110 path=/dev/x proto=https host=stgissrp.perodua.com.my' -k -H 'Host: stgissrp.perodua.com.my'
expect https://127.0.0.1/dev/api/x '200 port=8000 path=/x proto=https host=api.example.perodua.com.my' -k -H 'Host: api.example.perodua.com.my'
tls12 -no_ticket -sess_out /tmp/session > /dev/null || true
got=$(tls12 -no_ticket -sess_in /tmp/session | grep -E '^(New|Reused), ' || true)
[[ $got == Reused,* ]] && pass "TLS 1.2 without tickets resumes by session ID: $got" || fail "TLS 1.2 session ID: $got"
got=$(tls12 | grep -o 'lifetime hint: [0-9]*' || true)
[[ $got == 'lifetime hint: 86400' ]] && pass "TLS 1.2 session ticket $got" || fail "TLS 1.2 session ticket $got (expected 86400)"

step 'status'
bash https.sh status
env PATH=/usr/bin:/bin bash https.sh status > /tmp/status 2>&1
grep -qx 'nginx:       /etc/nginx/conf.d/perodua-https.conf' /tmp/status \
    && pass 'status finds nginx without the sbin directories on the PATH' || fail "$(cat /tmp/status)"

step 'http: a route also served on port 80, for a TLS front that connects there'
cp "$STATE/routes.conf" /tmp/routes.conf
sed -i 's#/dev/api/   8000   strip$#/dev/api/   8000   strip,http#' "$STATE/routes.conf"
echo 'api.example.perodua.com.my      /dev/api/v2/  8001   strip' >> "$STATE/routes.conf"   # no http, under a route with it
bash https.sh apply | grep 'port 80'
expect http://api.example.perodua.com.my/dev/api/customers '200 port=8000 path=/customers proto=https host=api.example.perodua.com.my xff=127.0.0.1'
expect https://api.example.perodua.com.my/dev/api/customers '200 port=8000 path=/customers proto=https'
expect https://api.example.perodua.com.my/dev/api/v2/x '200 port=8001 path=/x proto=https'
# a redirect of nginx keeps the scheme of the front: no http:// in Location
code=$(curl -s -D - -o /dev/null "${RESOLVE[@]}" 'http://api.example.perodua.com.my/dev/api?a=1' | tr -d '\r' | grep -i '^location:' || true)
[[ $code == 'Location: /dev/api/?a=1' ]] && pass "/dev/api?a=1 on port 80 -> $code" || fail "/dev/api?a=1 on port 80 -> $code"
for url in http://api.example.perodua.com.my/uat/api/x http://stgissrp.perodua.com.my/dev/x \
           http://api.example.perodua.com.my/dev/api/v2/x http://api.example.perodua.com.my/dev/api/v2; do   # no http: redirected
    code=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" "$url")
    [[ $code == "301 https://${url#http://}" ]] && pass "$url -> $code" || fail "$url -> $code"
done
code=$(curl -s -o /dev/null -w '%{http_code}' "${RESOLVE[@]}" http://api.example.perodua.com.my/other)
[[ $code == 404 ]] && pass "a path that is no route answers 404 on port 80, as on 443: $code" || fail "/other on port 80 -> $code"
headers=$(curl -s "${RESOLVE[@]}" -D - -o /dev/null http://api.example.perodua.com.my/dev/api/login | tr -d '\r')
grep -qx 'Set-Cookie: session_id=echo; Path=/dev/; HttpOnly; Secure; SameSite=Lax' <<< "$headers" \
    && pass 'session_id gets Secure and SameSite=Lax on port 80 too' || fail "session_id: $(grep -i '^set-cookie' <<< "$headers")"
bash https.sh status | grep 'HTTP answer on port 80: 200' && pass 'status shows the port 80 answer' || fail 'status: no port 80 answer'
cp /tmp/routes.conf "$STATE/routes.conf"
bash https.sh apply > /dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' "${RESOLVE[@]}" http://api.example.perodua.com.my/dev/api/x)
[[ $code == 301 ]] && pass "without http, port 80 redirects again: $code" || fail "without http: $code"

step 'api: the documentation is closed and every caller address is rate limited'
cp "$STATE/routes.conf" /tmp/routes.conf
sed -i -e 's#/dev/api/   8000   strip$#/dev/api/   8000   strip,http,api#' -e 's#/uat/api/   8001   strip$#/uat/api/   8001   strip,api#' \
    "$STATE/routes.conf"
bash https.sh apply | grep 'API: '
for url in https://api.example.perodua.com.my/dev/api/ http://api.example.perodua.com.my/dev/api/ https://api.example.perodua.com.my/uat/api/; do
    for name in docs redoc openapi.json; do
        expect "$url$name" '404'
    done
done
expect https://api.example.perodua.com.my/dev/api/ '200 port=8000 path=/ proto=https'
expect https://api.example.perodua.com.my/dev/api/x '200 port=8000 path=/x proto=https'
expect http://api.example.perodua.com.my/dev/api/x '200 port=8000 path=/x proto=https'
expect https://api.example.perodua.com.my/uat/api/x '200 port=8001 path=/x proto=https'
code=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" http://api.example.perodua.com.my/uat/api/docs)
[[ $code == '301 https://api.example.perodua.com.my/uat/api/docs' ]] && pass "no http on /uat/api/: port 80 redirects -> $code" || fail "/uat/api/docs on port 80 -> $code"
printf 'api.example.perodua.com.my      /dev/api/docs/   8111\n' >> "$STATE/routes.conf"
bash https.sh apply > /tmp/apply 2>&1 && pass 'nginx takes /dev/api/docs/ (no http) under /dev/api/ (strip,http,api)' \
    || fail "/dev/api/docs/ under /dev/api/: $(cat /tmp/apply)"
expect http://api.example.perodua.com.my/dev/api/docs '404'
expect https://api.example.perodua.com.my/dev/api/docs '404'
expect https://api.example.perodua.com.my/dev/api/docs/x '200 port=8111 path=/dev/api/docs/x proto=https'
code=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" http://api.example.perodua.com.my/dev/api/docs/x)
[[ $code == '301 https://api.example.perodua.com.my/dev/api/docs/x' ]] && pass "/dev/api/docs/x on port 80 -> $code" || fail "/dev/api/docs/x on port 80 -> $code"
sleep 3
start=${EPOCHREALTIME/[.,]/}
codes=$(burst https://api.example.perodua.com.my/dev/api/b{1..100})
took=$(((${EPOCHREALTIME/[.,]/} - start) / 1000))
first=$(grep -nx 429 <<< "$codes" | head -n 1 | cut -d: -f1 || true)
[[ -n $first && $first -gt 20 && $(head -n $((first - 1)) <<< "$codes" | grep -cvx 200) == 0 && $(grep -cvx '200\|429' <<< "$codes") == 0 ]] \
    && pass "100 fast requests to /dev/api/ in $took ms: $((first - 1)) answers 200, then 429 ($(grep -cx 429 <<< "$codes") of 100)" \
    || fail "100 fast requests to /dev/api/ in $took ms: $(tr '\n' ' ' <<< "$codes")"
codes=$(burst https://stgissrp.perodua.com.my/dev/b{1..60})
[[ $(grep -cx 200 <<< "$codes") == 60 ]] && pass '60 fast requests to /dev/ (no api) right after: all 200' \
    || fail "60 fast requests to /dev/: $(tr '\n' ' ' <<< "$codes")"
codes=$(burst http://api.example.perodua.com.my/dev/api/c{1..30})
[[ $(grep -cx 429 <<< "$codes") -gt 0 ]] && pass "port 80 shares the limit: $(grep -cx 429 <<< "$codes") of 30 answers 429" \
    || fail "30 fast requests to /dev/api/ on port 80: $(tr '\n' ' ' <<< "$codes")"
bash https.sh status | grep 'API: docs closed, rate limited' && pass 'status marks the api routes' || fail 'status: no api mark'
cp /tmp/routes.conf "$STATE/routes.conf"
bash https.sh apply > /dev/null
! grep -q limit_req /etc/nginx/conf.d/perodua-https.conf && pass 'without api, no limit_req' || fail 'limit_req left without api'
expect https://api.example.perodua.com.my/dev/api/docs '200 port=8000 path=/docs proto=https'

step 'renewal: a new request with the same key, installed while nginx runs'
bash https.sh csr > /dev/null
sign /ca/leaf2.pem
bash https.sh install-cert /ca/leaf2.pem /ca/int.pem | tail -1
eventually serves api.example.perodua.com.my /ca/leaf2.pem && pass 'nginx serves the new certificate' || fail 'not the new certificate'

step 'a new key: nginx keeps the old one until the new certificate is installed'
bash https.sh csr --new-key > /dev/null
serves api.example.perodua.com.my /ca/leaf2.pem && pass 'still serving the current certificate' || fail 'the certificate changed too early'
sign /ca/leaf3.pem
bash https.sh install-cert /ca/leaf3.pem /ca/int.pem | tail -1
eventually serves stgissrp.perodua.com.my /ca/leaf3.pem && [[ ! -f $STATE/key.new.pem ]] \
    && pass 'nginx serves the certificate of the new key' || fail 'not the new key'
expect https://stgissrp.perodua.com.my/dev/x '200 port=8110 path=/dev/x'

step 'another nginx file that serves one of the host names is refused'
printf 'server { listen 80; server_name stgissrp.perodua.com.my; return 200 old; }\n' > /etc/nginx/conf.d/old-front.conf
if bash https.sh apply 2> /tmp/err; then fail 'apply ignored the other configuration'; fi
grep -q '/etc/nginx/conf.d/old-front.conf: stgissrp.perodua.com.my' /tmp/err && pass "$(tail -1 /tmp/err)" || fail 'the other file was not named'
rm /etc/nginx/conf.d/old-front.conf
expect https://api.example.perodua.com.my/dev/api/y '200 port=8000 path=/y'

step 'another file holds nginx back (a port it cannot open): nginx keeps its old configuration, nothing changes'
python3 -m http.server 9443 --bind 127.0.0.1 > /dev/null 2>&1 &
squatter=$!
sleep 1
printf 'server {\n    listen 127.0.0.1:9443;\n    server_name pending.example;\n}\n' > /etc/nginx/conf.d/pending.conf
before=$(cat /etc/nginx/conf.d/perodua-https.conf)
printf 'stgissrp.perodua.com.my    /sit/       8111\n' >> "$STATE/routes.conf"
if bash https.sh apply 2> /tmp/err; then fail 'apply claimed a change nginx did not take'; fi
grep -q 'nginx did not take the change' /tmp/err && [[ $(cat /etc/nginx/conf.d/perodua-https.conf) == "$before" ]] \
    && pass 'apply failed and put the previous configuration back' || fail "apply: $(cat /tmp/err)"
if bash https.sh uninstall --purge --confirm yes 2> /tmp/err; then fail 'uninstall claimed nginx dropped the configuration'; fi
grep -q 'nginx still runs this configuration' /tmp/err && [[ -f /etc/nginx/conf.d/perodua-https.conf && -f $STATE/key.pem ]] \
    && pass 'uninstall failed and changed nothing' || fail "uninstall: $(cat /tmp/err)"
rm /etc/nginx/conf.d/pending.conf
kill "$squatter"
wait "$squatter" 2> /dev/null || true
sed -i '/ \/sit\/ /d' "$STATE/routes.conf"
expect https://stgissrp.perodua.com.my/dev/z '200 port=8110 path=/dev/z'

step 'diagnose: a front (a WAF) that forwards HTTPS to port 80 makes a redirect loop'
apt-get install -y -qq libnginx-mod-http-headers-more-filter > /dev/null
rm -f /etc/nginx/modules-enabled/*headers-more*
mkdir -p /front
front() {
    cat > /front/nginx.conf << EOF
load_module /usr/lib/nginx/modules/ngx_http_headers_more_filter_module.so;
pid /front/nginx.pid;
error_log /front/error.log;
events {
}
http {
    access_log off;
    server {
        listen 127.0.0.2:8443 ssl;
        ssl_certificate $STATE/fullchain.pem;
        ssl_certificate_key $STATE/key.pem;
        more_set_headers 'Server: CloudWAF';
        location / {
            proxy_pass $1;
            proxy_set_header Host \$host;
            proxy_ssl_server_name on;
            proxy_ssl_name \$host;
        }
    }
}
EOF
    nginx -t -q -c /front/nginx.conf
}
front_answers() {
    [[ $(curl -sk --connect-to stgissrp.perodua.com.my:443:127.0.0.2:8443 -o /dev/null -w '%{http_code}' \
        https://stgissrp.perodua.com.my/dev/ || true) == "$1" ]]
}
workers() { { cat /proc/[0-9]*/stat 2> /dev/null || true; } | awk -v master="$(cat /run/nginx.pid)" '$4 == master { print $1 }' | sort; }
unchanged() { find "$STATE" /etc/nginx -type f -exec sha256sum {} + | sort; workers; }
front http://127.0.0.1:80
nginx -c /front/nginx.conf
eventually front_answers 301 && pass 'the fake front at 127.0.0.2:8443 sends HTTPS requests to port 80' || fail 'the fake front does not answer'
curl -skS -L --max-redirs 4 -o /dev/null -D /tmp/loop.headers --connect-to stgissrp.perodua.com.my:443:127.0.0.2:8443 \
    https://stgissrp.perodua.com.my/dev/app/ 2> /tmp/err && rc=0 || rc=$?
tr -d '\r' < /tmp/loop.headers > /tmp/loop.txt
token=$(nginx -v 2>&1 | sed 's/^nginx version: //')
length=$((157 + ${#token}))
[[ $rc == 47 && $(grep -c '^HTTP/1.1 301 Moved Permanently$' /tmp/loop.txt) == 5 ]] \
    && pass "curl -L --max-redirs 4: five 301 answers, then: $(cat /tmp/err)" || fail "curl exit $rc: $(cat /tmp/loop.txt /tmp/err)"
[[ $(grep -cx 'Server: CloudWAF' /tmp/loop.txt) == 5 && $(grep -cx "Content-Length: $length" /tmp/loop.txt) == 5
   && $(grep -cx 'Location: https://stgissrp.perodua.com.my/dev/app/' /tmp/loop.txt) == 5 ]] \
    && pass "each answer: Server: CloudWAF, Content-Length: $length (157 + the ${#token} characters of $token), Location: the same URL" \
    || fail "the answers: $(cat /tmp/loop.txt), nginx: $token"
before=$(unchanged)
bash https.sh diagnose --front 127.0.0.2:8443 > /tmp/diagnose 2>&1 && rc=0 || rc=$?
cat /tmp/diagnose
[[ $rc == 4 ]] && grep -qx 'Result: 3 problem(s) found' /tmp/diagnose && pass 'diagnose exits with status 4' || fail "diagnose exited with $rc"
for route in 'api.example.perodua.com.my /dev/api/' 'stgissrp.perodua.com.my /dev/'; do
    grep -qxF "  PROBLEM $route: the front at 127.0.0.2:8443 (Server: CloudWAF) sends HTTPS requests to port 80 of this server, where this route redirects to https://: a redirect loop. Ask the front's owner to forward HTTPS to port 443 with the original Host header, or add http to this route (README: Behind a TLS front on port 80)" /tmp/diagnose \
        && pass "diagnose: the front loops for $route" || fail "diagnose did not find the loop of the front for $route"
done
grep -Eq '^  PROBLEM redirect loop from 127\.0\.0\.1 \(([5-9]|[1-9][0-9]+) answers, last: GET /dev/app/ HTTP/1\.[01] at [0-9]{2}/[A-Z][a-z]{2}/' /tmp/diagnose \
    && pass 'diagnose: the access log shows the loop of the five requests that curl sent' || fail 'diagnose did not find the loop in the access log'
grep -qF '  NOTE /etc/nginx/sites-enabled/default is the default server on port 80' /tmp/diagnose \
    && pass "diagnose: Ubuntu's default site answers other names on port 80" || fail 'diagnose did not name the default site'
[[ $(unchanged) == "$before" ]] && pass 'diagnose changed no file and did not reload nginx' || fail 'diagnose changed a file or reloaded nginx'

step 'diagnose: the same front forwarding HTTPS to port 443 with the original Host header'
front https://127.0.0.1:443
nginx -c /front/nginx.conf -s reload
eventually front_answers 200 && pass 'the fake front sends HTTPS requests to port 443' || fail 'the fake front does not forward to 443'
python3 - << 'PY'
import datetime, re
path = "/var/log/nginx/access.log"
def older(match):
    time = datetime.datetime.strptime(match.group(1), "%d/%b/%Y:%H:%M:%S %z") - datetime.timedelta(hours=2)
    return "[" + time.strftime("%d/%b/%Y:%H:%M:%S %z") + "]"
with open(path) as log:
    text = re.sub(r"\[(\d\d/[A-Z][a-z]{2}/\d{4}:\d\d:\d\d:\d\d [+-]\d{4})\]", older, log.read())
with open(path, "w") as log:
    log.write(text)
PY
grep -q '" 301 ' /var/log/nginx/access.log && pass 'the access log keeps the loop, with its times moved two hours back (as two hours later)' \
    || fail 'the access log has no 301 answer'
for run in 1 2 3; do
    bash https.sh diagnose --front 127.0.0.2:8443 > /tmp/diagnose 2>&1 && rc=0 || rc=$?
    [[ $rc == 0 ]] || break
done
cat /tmp/diagnose
[[ $rc == 0 ]] && grep -qx 'Result: no problem found' /tmp/diagnose \
    && pass "diagnose exits with status 0, $run runs in a row" || fail "diagnose run $run exited with $rc"
grep -qxF '  OK stgissrp.perodua.com.my /dev/: through the front at 127.0.0.2:8443 answers 200 (Server: CloudWAF)' /tmp/diagnose \
    && grep -qxF '  OK api.example.perodua.com.my /dev/api/: through the front at 127.0.0.2:8443 answers 200 (Server: CloudWAF)' /tmp/diagnose \
    && pass 'diagnose: the way through the front is OK' || fail 'diagnose did not report the front as OK'
grep -qF '  OK no redirect loop in the last 30 minutes of /var/log/nginx/access.log (' /tmp/diagnose \
    && grep -Eq '^  NOTE earlier redirect loop from 127\.0\.0\.1 \([0-9]+ answers, last: GET /dev/(app/)? HTTP/1\.[01] at [^,]+, 1[12][0-9] minutes before this run\)' /tmp/diagnose \
    && pass 'diagnose: the loop of two hours ago is a NOTE, not a PROBLEM' || fail 'diagnose did not report the earlier loop as a NOTE'
[[ $(unchanged) == "$before" ]] && pass 'diagnose changed no file and did not reload nginx' || fail 'diagnose changed a file or reloaded nginx'

step 'diagnose: the same front on port 80, to routes with http'
cp "$STATE/routes.conf" /tmp/routes.conf
sed -i -E -e 's#^(api\.example\.perodua\.com\.my +/(dev|uat)/api/ +800[01] +)strip$#\1strip,http#' \
    -e 's#^(stgissrp\.perodua\.com\.my +/(dev|uat)/ +811[01])$#\1   http#' "$STATE/routes.conf"
[[ $(grep -c 'http$' "$STATE/routes.conf") == 4 ]] && pass 'http on the four routes' || fail "the routes: $(cat "$STATE/routes.conf")"
bash https.sh apply | grep 'port 80'
nginx -c /front/nginx.conf -s quit
eventually front_answers 000 && pass 'the fake front stopped' || fail 'the fake front still answers'
front http://127.0.0.1:80
nginx -c /front/nginx.conf
eventually front_answers 200 && pass 'the fake front sends HTTPS requests to port 80, and the routes with http answer' \
    || fail 'the fake front on port 80 does not get 200'
got=$(curl -sk --connect-to stgissrp.perodua.com.my:443:127.0.0.2:8443 https://stgissrp.perodua.com.my/dev/x || true)
[[ $got == 'port=8110 path=/dev/x proto=https host=stgissrp.perodua.com.my'* ]] && pass "through the front and port 80: $got" \
    || fail "through the front and port 80: $got"
before=$(unchanged)
for run in 1 2 3; do
    bash https.sh diagnose --front 127.0.0.2:8443 > /tmp/diagnose 2>&1 && rc=0 || rc=$?
    [[ $rc == 0 ]] || break
done
cat /tmp/diagnose
[[ $rc == 0 ]] && grep -qx 'Result: no problem found' /tmp/diagnose \
    && pass "diagnose exits with status 0, $run runs in a row" || fail "diagnose run $run exited with $rc"
for route in 'api.example.perodua.com.my /dev/api/' 'api.example.perodua.com.my /uat/api/' 'stgissrp.perodua.com.my /dev/' 'stgissrp.perodua.com.my /uat/'; do
    grep -qxF "  OK $route: HTTP on this server answers 200: served on port 80 too, for a TLS front (http)" /tmp/diagnose \
        && pass "diagnose: port 80 serves $route" || fail "diagnose: port 80 does not serve $route"
done
grep -qxF '  OK stgissrp.perodua.com.my /dev/: through the front at 127.0.0.2:8443 answers 200 (Server: CloudWAF)' /tmp/diagnose \
    && grep -qxF '  OK api.example.perodua.com.my /dev/api/: through the front at 127.0.0.2:8443 answers 200 (Server: CloudWAF)' /tmp/diagnose \
    && pass 'diagnose: the way through the front on port 80 is OK' || fail 'diagnose did not report the front on port 80 as OK'
grep -qF '  OK no redirect loop in the last 30 minutes of /var/log/nginx/access.log (' /tmp/diagnose \
    && grep -Eq '^  NOTE earlier redirect loop from 127\.0\.0\.1 ' /tmp/diagnose \
    && pass 'diagnose: the loop of two hours ago is still a NOTE' || fail 'diagnose did not report the earlier loop as a NOTE'
[[ $(unchanged) == "$before" ]] && pass 'diagnose changed no file and did not reload nginx' || fail 'diagnose changed a file or reloaded nginx'
printf 'server {\n    listen 80;\n    server_name stgissrp.perodua.com.my;\n    return 200 hand-written;\n}\n' > /etc/nginx/conf.d/perodua-front.conf
bash https.sh diagnose --front 127.0.0.2:8443 > /tmp/diagnose 2>&1 && rc=0 || rc=$?
[[ $rc == 0 ]] && grep -qF '  NOTE /etc/nginx/conf.d/perodua-front.conf, a file that apply did not write, serves host names of the table: stgissrp.perodua.com.my (80). ' /tmp/diagnose \
    && pass 'diagnose: a hand-written file for a host name of the table is a NOTE' || fail "diagnose with a hand-written file: exit $rc, $(cat /tmp/diagnose)"
rm /etc/nginx/conf.d/perodua-front.conf
cp /tmp/routes.conf "$STATE/routes.conf"
bash https.sh apply > /dev/null
nginx -c /front/nginx.conf -s quit
eventually front_answers 000 && pass 'the fake front stopped' || fail 'the fake front still answers'
rm -rf /front

step 'uninstall'
bash https.sh uninstall --confirm yes
eventually closed && pass 'nothing answers HTTPS any more' || fail 'HTTPS still answers'
[[ -f $STATE/key.pem ]] && pass 'the key stays without --purge' || fail 'the key was removed'
printf 'server {\n    listen 443 ssl default_server;\n    ssl_certificate %s/fullchain.pem;\n    ssl_certificate_key %s/key.pem;\n    return 444;\n}\n' \
    "$STATE" "$STATE" > /etc/nginx/conf.d/other-default.conf
if bash https.sh apply 2> /tmp/err; then fail 'apply ignored another default server for port 443'; fi
grep -qx '/etc/nginx/conf.d/other-default.conf: listen 443 ssl default_server' /tmp/err && [[ ! -e /etc/nginx/conf.d/perodua-https.conf ]] \
    && pass "$(tr '\n' ' ' < /tmp/err)" || fail "another default server: $(cat /tmp/err)"
rm /etc/nginx/conf.d/other-default.conf
bash https.sh uninstall --purge --confirm yes > /dev/null
[[ ! -e $STATE ]] && pass "--purge removed $STATE" || fail "$STATE is still there"

printf '\nAll checks passed.\n'
