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
         --resolve api.example.perodua.com.my:80:127.0.0.1)
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

python3 - > /dev/null 2>&1 << 'PY' &
import http.server, threading, time
class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f"port={self.server.server_port} path={self.path} proto={self.headers.get('X-Forwarded-Proto')}"
                f" host={self.headers.get('Host')} xff={self.headers.get('X-Forwarded-For')}\n").encode()
        self.send_response(200)
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
    -H 'Host: evil.example' -H 'X-Forwarded-For: 192.0.2.1'
code=$(curl -s --cacert /ca/root.pem -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" 'https://stgissrp.perodua.com.my/dev?a=1')
[[ $code == '301 https://stgissrp.perodua.com.my/dev/?a=1' ]] && pass "/dev?a=1 -> $code" || fail "/dev?a=1 -> $code"
code=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "${RESOLVE[@]}" http://api.example.perodua.com.my/dev/api/x)
[[ $code == '301 https://api.example.perodua.com.my/dev/api/x' ]] && pass "http -> $code" || fail "http -> $code"
# the generation of the configuration nginx runs, for https.sh on this server only
got=$(curl -s --cacert /ca/root.pem "${RESOLVE[@]}" https://api.example.perodua.com.my/.perodua-https)
[[ $got =~ ^generation\ [0-9a-f]{16}$ ]] && pass "/.perodua-https from 127.0.0.1 -> $got" || fail "/.perodua-https -> $got"
ip=$(ip -4 -o addr show scope global | awk '{ sub(/\/.*/, "", $4); print $4; exit }')
code=$(curl -s --cacert /ca/root.pem -o /dev/null -w '%{http_code}' --resolve "api.example.perodua.com.my:443:$ip" \
    https://api.example.perodua.com.my/.perodua-https)
[[ $code == 404 ]] && pass "/.perodua-https from $ip -> $code" || fail "/.perodua-https from $ip -> $code"

step 'status'
bash https.sh status

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

step 'uninstall'
bash https.sh uninstall --confirm yes
eventually closed && pass 'nothing answers HTTPS any more' || fail 'HTTPS still answers'
[[ -f $STATE/key.pem ]] && pass 'the key stays without --purge' || fail 'the key was removed'
bash https.sh uninstall --purge --confirm yes > /dev/null
[[ ! -e $STATE ]] && pass "--purge removed $STATE" || fail "$STATE is still there"

printf '\nAll checks passed.\n'
