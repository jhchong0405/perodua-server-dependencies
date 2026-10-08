"""https.sh against fake nginx, systemctl, ss, curl, docker and dig, with real openssl.

The script requires root; run these in the test image (tests/Dockerfile) or skip
them elsewhere. A throwaway certificate authority (a root and an intermediate)
signs the requests the script makes, the way the certificate issuer would. The
fake nginx records every call, fails `-t` and `-T` on request, and prints for
`-T` the configuration files the test gives it plus the one the script wrote.
A reload or start through the fake systemctl copies the script's configuration
and the chain it names to what nginx "runs", unless the test says the reload
does not take; `openssl s_client`, which the script uses to ask nginx what it
runs, answers from that copy. Every other openssl command is the real one.

For letsencrypt, the fake docker plays lego: for a host name of the request
without an acme-dns account it makes one in the accounts file and fails as lego
does; with every account made it has the throwaway intermediate sign the request
it was given and writes the chain where lego does. The fake curl answers the ACME
directory and the acme-dns server, the fake dig the CNAME records the test puts
in each DNS server. The renewal units and the script's copy go to temporary
directories (--unit-dir, --lib-dir).

For diagnose (its user agent), the fake curl answers HTTPS (port 443) and HTTP
(port 80) on 127.0.0.1 as a kit nginx without http routes does unless the test
says otherwise, and a front at
the address and port the test names, a front that answers each request with a
redirect to the same URL included; the fake getent gives the addresses the
test puts in for a host name, and the fake ss the listening sockets. The fake
nginx -V names the log files the test gives it, and the fake date gives the
time the test sets for "date +%s" (every other date call is the real one).

The script asks systemd, the fake systemctl here, only where systemd runs
(/run/systemd/system). In a container without systemd, such as the test image,
that directory is made for these tests and removed after them. Nothing else
outside temporary directories is changed.
"""

import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "https.sh"
EXAMPLE = SCRIPT.with_name("https-routes.conf.example")
LOCK = Path("/run/lock/perodua-https.lock" if os.path.isdir("/run/lock") else "/run/perodua-https.lock")
SYSTEMD = Path("/run/systemd/system")
IN_CONTAINER = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
LEGO_IMAGE = "goacme/lego@sha256:1944e8c36055beec47c7de6f15202b41128be75eea0ffa257f0c14d93c5155fd"
LE_SERVER = "https://acme-v02.api.letsencrypt.org/directory"
ACME_DNS = "https://acmedns.novutal.com"
TERMS = "https://letsencrypt.example/terms.pdf"
UNIT = "perodua-https-renew"
HOSTS = ["api.example.perodua.com.my", "stgissrp.perodua.com.my"]
LEAF = "subject=CN=api.example.perodua.com.my,O=Perodua,C=MY"
INTERMEDIATE = "subject=CN=Test Intermediate"
ROOT = "subject=CN=Test Root"
ROUTES = """# the Oracle API under a path, and an App with two environments
api.example.perodua.com.my      /dev/api/   8000   strip
api.example.perodua.com.my      /uat/api/   8001   strip
stgissrp.perodua.com.my    /dev/       8110
stgissrp.perodua.com.my    /uat/       8111   # a comment
"""
# What apply wrote for this table before the http option, byte for byte: without
# http and api the file stays the same, but for the catch-all server of port 443.
BEFORE_HTTP_TABLE = "stgissrp.perodua.com.my /dev/ 8110\nstgissrp.perodua.com.my /dev/api/ 8000 strip\n"
BEFORE_HTTP = """# Written by https.sh from @ROUTES@. Do not edit: change the routes, then run: sudo bash https.sh apply

map $http_upgrade $perodua_https_connection {
    default upgrade;
    '' close;
}

server {
    listen 80;
    server_name stgissrp.perodua.com.my;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name stgissrp.perodua.com.my;
    ssl_certificate @DIR@/fullchain.pem;
    ssl_certificate_key @DIR@/key.pem;
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
    proxy_set_header Host stgissrp.perodua.com.my;
    proxy_set_header X-Forwarded-Host stgissrp.perodua.com.my;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $perodua_https_connection;
    # Odoo sets its session cookie without Secure, and these host names also
    # answer plain HTTP (port 80, and the App's own port on the network): the
    # browser sends the cookie over HTTPS only, on every port. On stgiss the
    # cookie also signs in every module host name.
    proxy_cookie_flags session_id secure samesite=lax;

    # Which configuration nginx runs, for https.sh on this server only.
    location = /.perodua-https {
        if ($remote_addr != 127.0.0.1) {
            return 404;
        }
        return 200 "generation @GENERATION@";
    }

    location /dev/ {
        proxy_pass http://127.0.0.1:8110;
    }

    location /dev/api/ {
        proxy_pass http://127.0.0.1:8000/;
    }

    # Every other path is closed.
    location / {
        return 404;
    }
}
"""
CATCH_ALL = """
server {
    listen 443 ssl default_server;
    server_name _;
    ssl_certificate @DIR@/fullchain.pem;
    ssl_certificate_key @DIR@/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:perodua_https:10m;
    ssl_session_timeout 1d;
    return 444;
}
"""
OPTION_MESSAGE = "OPTION must be strip, http, api or a comma-separated list of them, like strip,http,api"
FAKE_NGINX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["nginx"] + args) + "\n")
with open(os.environ["FAKE_STATE"]) as f:
    state = json.load(f)
if (args == ["-t"] and state["t_status"]) or (args == ["-T"] and state["T_status"]):
    sys.exit("nginx: [emerg] fake failure")
if args == ["-V"]:
    paths = "".join(f" --{kind}-log-path={path}" for kind, path in zip(("http", "error"), state["nginx_logs"]))
    sys.exit(f"nginx version: nginx/1.24.0 (Ubuntu)\nconfigure arguments: --prefix=/usr/share/nginx{paths}")
if args == ["-T"]:
    print("# configuration file /etc/nginx/nginx.conf:\nhttp { include /etc/nginx/conf.d/*.conf; }\n")
    files = dict(state["others"])
    if os.path.exists(os.environ["FAKE_OURS"]):
        files[os.environ["FAKE_OURS"]] = open(os.environ["FAKE_OURS"]).read()
    for path, text in files.items():
        print(f"# configuration file {path}:\n{text}\n")
'''
FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, re, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["systemctl"] + args) + "\n")
state = json.load(open(os.environ["FAKE_STATE"]))
if any(arg.startswith("perodua-https-renew") for arg in args):  # the renewal timer and its service
    if args[0] == "is-active":
        print(state["timer"])
        sys.exit(0 if state["timer"] == "active" else 3)
    if args[0] == "show":
        print(state["renewal_result"])
    sys.exit(0)
if args[:1] == ["is-active"]:
    sys.exit(0 if state["active"] else 3)
if args[:1] in (["reload"], ["start"]) and state["reload_takes"]:
    ours, running = os.environ["FAKE_OURS"], os.environ["FAKE_RUNNING"]
    for suffix in (".conf", ".pem"):
        if os.path.exists(running + suffix):
            os.unlink(running + suffix)
    if os.path.exists(ours):
        shutil.copy(ours, running + ".conf")
        chain = re.search(r"^ *ssl_certificate (\S+);$", open(ours).read(), re.M).group(1)
        shutil.copy(chain, running + ".pem")
'''
FAKE_SS = r'''#!/usr/bin/env python3
import json, os
print(json.load(open(os.environ["FAKE_STATE"]))["ss"], end="")
'''
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, secrets, sys, uuid
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["curl"] + args) + "\n")
state = json.load(open(os.environ["FAKE_STATE"]))
url = args[-1]
if "perodua-https-diagnose" in args:
    flag = "--resolve" if "--resolve" in args else "--connect-to"
    parts = args[args.index(flag) + 1].split(":")
    target = parts[2] + ":" + (parts[1] if flag == "--resolve" else parts[3])
    web = {"127.0.0.1:443": {"code": "200", "server": "nginx/1.24.0 (Ubuntu)"},
           "127.0.0.1:80": {"code": "301", "server": "nginx/1.24.0 (Ubuntu)", "location": "{https}"}}
    web.update(state["web"])
    answer = web.get(f"{target} {url}", web.get(target))
    code, location, hops, status = "000", "", 1, 0
    if answer:
        code = answer["code"]
        location = (answer.get("location", "").replace("{url}", url).replace("{https}", "https" + url[url.index(":"):])
                    .replace("{path}", "/" + url.split("/", 3)[3]))
        if code.startswith("3") and "-L" in args and (location == url or answer.get("endless")):
            hops, status = int(args[args.index("--max-redirs") + 1]) + 1, 47
    if "-D" in args:
        block = (f"HTTP/1.1 {code} Fake\r\nServer: {answer['server']}\r\n" + (f"Location: {location}\r\n" if location else "")
                 + "\r\n") if answer else ""
        with open(args[args.index("-D") + 1], "w") as f:
            f.write(block * hops)
    if "-w" in args:
        print(args[args.index("-w") + 1].replace("%{http_code}", code)
              .replace("%{redirect_url}", location if code.startswith("3") else ""), end="")
    if not answer:
        print(f"curl: (7) Failed to connect to {parts[2]} port {target.rsplit(':', 1)[1]}: Connection refused", file=sys.stderr)
        sys.exit(7)
    if status:
        print(f"curl: (47) Maximum ({hops - 1}) redirects followed", file=sys.stderr)
    sys.exit(status)
if any(url.startswith(prefix) for prefix in state["unreachable"]):
    sys.exit("curl: (28) Connection timed out after 20002 milliseconds")
out = args[args.index("-o") + 1] if "-o" in args else None
code = "200"
if url.endswith("/register"):  # acme-dns: a new account, with spaces in its JSON unlike acme-dns, which the script must take
    code, sub = state["register_code"], str(uuid.uuid4())
    answer = {"username": str(uuid.uuid4()), "password": secrets.token_urlsafe(30), "fulldomain": sub + ".acme.novutal.com",
              "subdomain": sub, "allowfrom": []} if code.startswith("2") else {"error": "internal error"}
    with open(out, "w") as f:
        json.dump(answer, f)
elif url.endswith("/health"):
    code = state["acme_dns_code"]
elif out and out != "/dev/null":  # the ACME directory
    with open(out, "w") as f:
        json.dump(state["directory"], f)
if "-w" in args:
    print(code, end="")
'''
FAKE_DOCKER = r'''#!/usr/bin/env python3
import datetime, hashlib, json, os, re, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["docker"] + args) + "\n")
state = json.load(open(os.environ["FAKE_STATE"]))
docker = state["docker"]
if args[:1] == ["info"]:
    sys.exit(0 if docker["running"] else "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if docker["image"] else "Error: No such image")
if args[:1] == ["pull"]:
    sys.exit(0 if docker["pull"] else 'Error response from daemon: Get "https://registry-1.docker.io/v2/": net/http: TLS handshake timeout')
assert args[:1] == ["run"], args
i, mounts, env = 1, {}, {}  # docker run OPTIONS IMAGE lego-arguments
while args[i].startswith("-"):
    if args[i] in ("-v", "-e", "--network"):
        if args[i] == "-v":
            source, target = args[i + 1].split(":")[:2]
            mounts[target] = source
        elif args[i] == "-e":
            key, _, value = args[i + 1].partition("=")
            env[key] = value
        i += 2
    else:
        i += 1
def host_path(path):  # a path in the container as it is on this server (through the longest mount that holds it)
    target = max((t for t in mounts if path == t or path.startswith(t.rstrip("/") + "/")), key=len, default=None)
    if target is None:
        sys.exit(f"open {path}: no such file or directory")  # as lego fails on a path it cannot see
    return mounts[target] + path[len(target):]
request = mounts["/request.csr"]
accounts_file = host_path(env["ACME_DNS_STORAGE_PATH"])
ca_file = env.get("LEGO_CA_CERTIFICATES")
if ca_file and not os.path.exists(host_path(ca_file)):
    sys.exit(f'panic: create certificates pool: error reading "{ca_file}"')  # as lego does
text = subprocess.run(["openssl", "req", "-in", request, "-noout", "-text"], check=True, capture_output=True, text=True).stdout
with open(os.environ["FAKE_LOG"], "a") as log:  # the accounts storage lego was given, and its mode
    log.write(json.dumps(["lego-storage", env["ACME_DNS_STORAGE_PATH"], open(accounts_file).read() if os.path.exists(accounts_file) else None,
                          oct(os.stat(accounts_file).st_mode & 0o777) if os.path.exists(accounts_file) else None]) + "\n")
accounts = json.load(open(accounts_file)) if os.path.exists(accounts_file) else {}
if state["lego"].get("rewrite"):  # lego saves its storage, as when an account did not work for it and it made another
    with open(accounts_file, "w") as f:
        json.dump(dict(accounts, extra={"fulldomain": "new.acme.novutal.com"}), f)
    if state["lego"]["rewrite"] == "fail":
        print('time=2026-09-30T00:00:00Z level=ERROR msg=Error error="acme-dns: new account created for \\"extra\\""')
        sys.exit(1)
# lego makes an account only while it solves a challenge; with "reuse", Let's Encrypt has checked every
# host name a short while ago, so lego solves none and touches no account
new = [] if state["lego"].get("reuse") else [name for name in re.findall(r"DNS:([^,\s]+)", text) if name not in accounts]
for name in new:  # as lego and acme-dns register them
    sub = hashlib.sha256(name.encode()).hexdigest()[:8] + "-4b1e-4a0c-9d3e-0123456789ab"
    accounts[name] = {"fulldomain": sub + ".acme.novutal.com", "subdomain": sub, "username": "user-" + sub,
                      "password": "secret-" + sub, "server_url": env["ACME_DNS_API_BASE"]}
if new:
    fd = os.open(accounts_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(accounts, separators=(",", ":")).encode())
    os.close(fd)
    print(f'time=2026-09-30T00:00:00Z level=ERROR msg=Error error="acme-dns: new account created for \\"{new[0]}\\""')
    sys.exit(1)
lego = state["lego"]
if lego.get("error"):
    print(f'time=2026-09-30T00:00:00Z level=ERROR msg=Error error="{lego["error"]}"')
    sys.exit(1)
ca, now = os.environ["FAKE_CA"], datetime.datetime.now(datetime.timezone.utc)
start, end = [(now + datetime.timedelta(days=n)).strftime("%Y%m%d%H%M%SZ") for n in (-1, lego["days"])]
certificates = host_path("/data/lego/certificates")  # under --path /data/lego
os.makedirs(certificates, exist_ok=True)
leaf = os.path.join(certificates, "leaf.tmp")
subprocess.run(["openssl", "ca", "-batch", "-notext", "-config", f"{ca}/ca.cnf", "-cert", f"{ca}/int.pem", "-keyfile",
                f"{ca}/int.key", "-in", request, "-out", leaf, "-startdate", start, "-enddate", end], check=True, capture_output=True)
with open(os.path.join(certificates, "perodua-https.crt"), "w") as bundle:  # the certificate, then its issuer
    bundle.write(open(leaf).read() + open(f"{ca}/int.pem").read())
os.unlink(leaf)
'''
FAKE_DIG = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["dig"] + args) + "\n")
state = json.load(open(os.environ["FAKE_STATE"]))
name = next(arg for arg in args if arg.startswith("_acme-challenge."))
server = next((arg[1:] for arg in args if arg.startswith("@")), "")  # "": this server's DNS
if server not in state["dns"]:
    print(";; connection timed out; no servers could be reached")
    sys.exit(9)
answer = state["dns"][server].get(name, "NXDOMAIN")
status = answer if answer in ("NXDOMAIN", "SERVFAIL") else "NOERROR"
print(f";; Got answer:\n;; ->>HEADER<<- opcode: QUERY, status: {status}, id: 4242\n")
if status == "NOERROR":
    print(f"{name}.\t\t300\tIN\tCNAME\t{answer}.")
'''
FAKE_GETENT = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["getent"] + args) + "\n")
addresses = json.load(open(os.environ["FAKE_STATE"]))["getent"].get(args[-1], [])
for address in addresses:
    print(f"{address}  STREAM {args[-1]}\n{address}  DGRAM  \n{address}  RAW    ")
sys.exit(0 if addresses else 2)
'''
FAKE_DATE = r'''#!/usr/bin/env python3
import json, os, sys
now = json.load(open(os.environ["FAKE_STATE"]))["now"]
if now is not None and sys.argv[1:] == ["+%s"]:
    print(now)
    sys.exit(0)
os.execv("REAL_DATE", ["date"] + sys.argv[1:])
'''
DIRECTORY = {"newNonce": "https://acme.example/nonce", "newAccount": "https://acme.example/account",
             "newOrder": "https://acme.example/order", "meta": {"termsOfService": TERMS}}
DEFAULT_STATE = {"t_status": 0, "T_status": 0, "others": {}, "active": True, "ss": "", "reload_takes": True,
                 "probe_fails": False, "timer": "active", "renewal_result": "success", "unreachable": [],
                 "directory": DIRECTORY, "acme_dns_code": "200", "register_code": "201",
                 "docker": {"running": True, "image": True, "pull": True},
                 "lego": {"days": 90}, "dns": {"": {}}, "web": {}, "getent": {}, "now": None, "nginx_logs": []}
FAKE_APT = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["apt-get"] + sys.argv[1:]) + "\n")
sys.exit("E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?")
'''
FAKE_OPENSSL = r'''#!/bin/sh
if [ "$1" = s_client ]; then
    [ -f "$FAKE_RUNNING.conf" ] || exit 1
    case " $* " in
        *" -quiet "*) grep -q '"probe_fails": true' "$FAKE_STATE" && exit 124  # as timeout reports it
                      printf 'HTTP/1.0 200 OK\r\n\r\n'
                      sed -n 's/^ *return 200 "\(generation [0-9a-f]*\)";$/\1/p' "$FAKE_RUNNING.conf" | head -n 1 ;;
        *) cat "$FAKE_RUNNING.pem" ;;
    esac
    exit 0
fi
exec REAL_OPENSSL "$@"
'''


def openssl(*args):
    return subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True, text=True).stdout


def public_key(path, kind):  # kind: pkey (a key), req (a request) or x509 (a certificate)
    if kind == "pkey":
        return openssl("pkey", "-in", path, "-pubout")
    return openssl(kind, "-in", path, "-noout", "-pubkey")


def dns_names(path, kind):
    text = openssl(kind, "-in", path, "-noout", "-text")
    return [part.strip()[4:] for line in text.splitlines() if "DNS:" in line for part in line.split(",")]


def blocks(path):  # the certificates of a PEM file, in order
    return [b + "-----END CERTIFICATE-----\n" for b in path.read_text().split("-----END CERTIFICATE-----\n")[:-1]]


def subjects(path):  # the subjects of the certificates in a PEM file, in order
    names = []
    for block in blocks(path):
        one = path.with_name("one.pem")
        one.write_text(block)
        names.append(openssl("x509", "-in", one, "-noout", "-subject", "-nameopt", "RFC2253").strip())
    return names


class Authority:
    """A root and an intermediate that sign requests with the dates a test asks for."""

    def __init__(self, directory):
        d = self.d = directory
        d.mkdir()
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "root.key", "-out", d / "root.pem",
                "-subj", "/CN=Test Root", "-days", "30", "-addext", "basicConstraints=critical,CA:TRUE")
        (d / "int.ext").write_text("basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n")
        self.intermediate("int")
        (d / "index.txt").write_text("")
        (d / "serial").write_text("1000\n")
        (d / "ca.cnf").write_text(f"[ ca ]\ndefault_ca = test\n[ test ]\ndir = {d}\ndatabase = $dir/index.txt\n"
                                  "new_certs_dir = $dir\nserial = $dir/serial\ndefault_md = sha256\npolicy = any\n"
                                  "copy_extensions = copy\nunique_subject = no\n[ any ]\ncountryName = optional\n"
                                  "organizationName = optional\ncommonName = supplied\n")

    def intermediate(self, name):  # "Test Intermediate" from the root, with a key of its own
        d = self.d
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", d / f"{name}.key", "-out", d / f"{name}.csr",
                "-subj", "/CN=Test Intermediate")
        openssl("x509", "-req", "-in", d / f"{name}.csr", "-CA", d / "root.pem", "-CAkey", d / "root.key",
                "-CAcreateserial", "-days", "30", "-extfile", d / "int.ext", "-out", d / f"{name}.pem")
        return d / f"{name}.pem"

    def cross_signed_root(self, name, **dates):  # the root signed by an older root, as bundles carry it in a CA change
        d = self.d
        if not (d / "old.pem").exists():
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "old.key", "-out", d / "old.pem",
                    "-subj", "/CN=Old Root", "-days", "30")
            openssl("x509", "-x509toreq", "-in", d / "root.pem", "-signkey", d / "root.key", "-out", d / "root.csr")
        return self.sign(d / "root.csr", d / f"{name}.pem", ca="old", ext="basicConstraints=critical,CA:TRUE\n", **dates)

    def sign(self, request, out, days_from=-1, days_to=90, ca="int", ext=None):  # by the intermediate unless ca says
        now = datetime.datetime.now(datetime.timezone.utc)
        dates = [(now + datetime.timedelta(days=n)).strftime("%Y%m%d%H%M%SZ") for n in (days_from, days_to)]
        args = ["ca", "-batch", "-notext", "-config", self.d / "ca.cnf", "-cert", self.d / f"{ca}.pem",
                "-keyfile", self.d / f"{ca}.key", "-in", request, "-out", out, "-startdate", dates[0], "-enddate", dates[1]]
        if ext:
            (self.d / "extra.ext").write_text(ext)
            args += ["-extfile", self.d / "extra.ext"]
        openssl(*args)
        return out


MADE_SYSTEMD = False


def setUpModule():
    global MADE_SYSTEMD
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not SYSTEMD.is_dir() and IN_CONTAINER:
        SYSTEMD.mkdir(parents=True)
        MADE_SYSTEMD = True


def tearDownModule():
    if MADE_SYSTEMD:
        SYSTEMD.rmdir()


needs_root = unittest.skipUnless(
    hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir() and (SYSTEMD.is_dir() or IN_CONTAINER),
    "needs root on Linux, with systemd or in a container (the test image)")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / "state"
        self.dir.mkdir()
        self.routes = self.dir / "routes.conf"
        self.routes.write_text(ROUTES)
        self.conf = self.tmp / "nginx" / "conf.d" / "perodua-https.conf"
        self.units = self.tmp / "units"
        self.lib = self.tmp / "lib"
        self.le_dir = self.dir / "letsencrypt"
        self.log = self.tmp / "calls.log"
        self.state = self.tmp / "state.json"
        self.set_state()
        bin_dir = self.bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fakes = {"nginx": FAKE_NGINX, "systemctl": FAKE_SYSTEMCTL, "ss": FAKE_SS, "curl": FAKE_CURL, "apt-get": FAKE_APT,
                 "docker": FAKE_DOCKER, "dig": FAKE_DIG, "getent": FAKE_GETENT,
                 "date": FAKE_DATE.replace("REAL_DATE", shutil.which("date")),
                 "openssl": FAKE_OPENSSL.replace("REAL_OPENSSL", shutil.which("openssl"))}
        for name, text in fakes.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.ca = Authority(self.tmp / "ca")
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_LOG=str(self.log),
                        FAKE_STATE=str(self.state), FAKE_OURS=str(self.conf), FAKE_RUNNING=str(self.tmp / "running"),
                        FAKE_CA=str(self.ca.d))
        self.addCleanup(LOCK.unlink, missing_ok=True)

    def set_state(self, **changes):
        self.state.write_text(json.dumps(dict(DEFAULT_STATE, **changes)))

    def run_script(self, *args, directory=None, script=SCRIPT):
        return self.run_argv(["bash", str(script), *map(str, args), "--dir", str(directory or self.dir),
                              "--nginx-conf", str(self.conf), "--unit-dir", str(self.units), "--lib-dir", str(self.lib)])

    def run_argv(self, argv):
        self.log.unlink(missing_ok=True)
        # a session of its own: no terminal, as over ssh without one
        return subprocess.run(argv, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
                              start_new_session=True)

    def ok(self, *args, **options):
        result = self.run_script(*args, **options)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def refused(self, *args, directory=None):
        result = self.run_script(*args, directory=directory)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def reloads(self):
        return sum(call in (["systemctl", "reload", "nginx"], ["systemctl", "start", "nginx"],
                            ["nginx", "-s", "reload"], ["nginx"]) for call in self.calls())

    def files(self, directory=None):
        return sorted(p.name for p in (directory or self.dir).iterdir())

    def without_nginx(self):  # no nginx on the PATH (https.sh adds the sbin directories itself)
        if any(Path(directory, "nginx").exists() for directory in ("/usr/local/sbin", "/usr/sbin", "/sbin")):
            self.skipTest("the test host has nginx in an sbin directory")
        (self.bin_dir / "nginx").unlink()

    def program(self, name):  # a running program of that name, from tmp/local/NAME/sbin/NAME
        binary = self.tmp / "local" / name / "sbin" / name
        binary.parent.mkdir(parents=True)
        shutil.copy("/bin/sleep", binary)
        process = subprocess.Popen([binary, "300"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return binary, process.pid

    def listening(self, *owners):  # what ss reports on the ports: (name as ss prints it, pid) pairs
        users = ",".join(f'("{name}",pid={pid},fd=6)' for name, pid in owners)
        self.set_state(ss=f"LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:({users})\n")

    def other_nginx(self):  # an nginx of another installation, listening on port 80
        binary, pid = self.program("nginx")
        self.listening(("nginx", pid))
        return binary

    def signed(self, name="leaf.pem", **dates):  # the issuer's answer to the script's request
        return self.ca.sign(self.dir / "request.csr", self.tmp / name, **dates)

    def installed(self):
        self.ok("csr")
        return self.ok("install-cert", self.signed(), self.ca.d / "int.pem")


@needs_root
class HttpsTest(Base):
    def test_the_routes_are_checked_before_anything_is_written(self):
        cases = [("api /dev/ 8000", "is not a full host name"),
                 ("api.example.perodua.com.my dev/ 8000", "PATH must start and end with /"),
                 ("api.example.perodua.com.my /dev 8000", "PATH must start and end with /"),
                 ("api.example.perodua.com.my /dev/ 70000", "PORT must be 1-65535"),
                 ("api.example.perodua.com.my /dev/ 8000 rewrite", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 ,api", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 api,", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 strip,,api", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 api,api", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 api,strip,api", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 API", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 apis", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 strip, api", OPTION_MESSAGE),
                 ("api.example.perodua.com.my /dev/ 8000 strip ,api", "too many columns"),
                 ("api.example.perodua.com.my /dev/ 8000 strip,rewrite", "OPTION must be"),
                 ("api.example.perodua.com.my /dev/ 8000 http,http", "OPTION must be"),
                 ("api.example.perodua.com.my /dev/ 8000 strip,", "OPTION must be"),
                 ("api.example.perodua.com.my /dev/ 8000 HTTP", "OPTION must be"),
                 ("api.example.perodua.com.my /dev/ 8000 strip more", "too many columns"),
                 ("api.example.perodua.com.my /dev/ 8000 strip http", "too many columns"),  # strip,http
                 ("api.example.perodua.com.my /dev/ 8000\nAPI.EXAMPLE.perodua.com.my /dev/ 8001", "is listed twice"),
                 ("# nothing but a comment", "lists no host name")]
        for table, message in cases:
            with self.subTest(table=table):
                self.routes.write_text(table + "\n")
                self.assertIn(message, self.refused("csr"))
                self.assertEqual(self.files(), ["routes.conf"])

    def test_the_option_is_a_list_of_strip_http_and_api(self):
        for option in ("", "strip", "http", "api", "strip,http", "http,strip", "strip,api", "http,api", "strip,http,api",
                       "api,http,strip", "http,api,strip"):
            with self.subTest(option=option):
                self.routes.write_text(f"api.example.perodua.com.my /dev/api/ 8000 {option}\n")
                result = self.ok("status")
                self.assertIn("https://api.example.perodua.com.my/dev/api/ -> 127.0.0.1:8000", result.stdout)
                self.assertEqual(self.files(), ["routes.conf"])

    def test_the_first_run_creates_the_routes_table_and_stops(self):
        fresh = self.tmp / "fresh"
        self.assertIn("Created", self.refused("csr", directory=fresh))
        self.assertEqual((fresh / "routes.conf").read_text(), EXAMPLE.read_text())
        self.assertEqual(self.files(fresh), ["routes.conf"])
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o700)
        existing = self.tmp / "existing"  # a directory that is there already keeps its mode
        existing.mkdir(mode=0o755)
        existing.chmod(0o755)
        self.refused("csr", directory=existing)
        self.assertEqual(existing.stat().st_mode & 0o777, 0o755)

    def test_csr_names_every_host_and_keeps_its_key(self):
        result = self.ok("csr")
        key, request = self.dir / "key.pem", self.dir / "request.csr"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertIn("-----BEGIN CERTIFICATE REQUEST-----", result.stdout)
        self.assertIn("CN=api.example.perodua.com.my", openssl("req", "-in", request, "-noout", "-subject", "-nameopt", "RFC2253"))
        self.assertEqual(dns_names(request, "req"), HOSTS)
        first = key.read_bytes()
        self.ok("csr", "--subject", "/C=MY/O=Perodua Sdn Bhd")
        self.assertEqual(key.read_bytes(), first)  # the same key again
        self.assertIn("O=Perodua Sdn Bhd", openssl("req", "-in", request, "-noout", "-subject", "-nameopt", "RFC2253"))
        self.assertIn("must not hold CN", self.refused("csr", "--subject", "/O=X/CN=other"))
        self.assertNotIn("WARNING", self.ok("csr", "--new-key").stderr)
        self.assertEqual(key.read_bytes(), first)  # nginx keeps its key until the new one has a certificate
        self.assertEqual(public_key(request, "req"), public_key(self.dir / "key.new.pem", "pkey"))
        self.assertIn("replaces the new key that waited", self.ok("csr", "--new-key").stderr)

    def test_csr_takes_a_key_that_a_request_was_already_made_with(self):
        existing = self.tmp / "handmade.key"
        openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", existing)
        result = self.ok("csr", "--key", existing)
        self.assertIn(f"If a request made with {existing} was already sent, wait for its certificate", result.stdout)
        key = self.dir / "key.pem"
        self.assertEqual(public_key(key, "pkey"), public_key(existing, "pkey"))
        self.assertEqual(public_key(self.dir / "request.csr", "req"), public_key(existing, "pkey"))
        inode = key.stat().st_ino
        self.ok("csr", "--key", existing)  # the key in use already: left as it is, and no new key waits
        self.assertEqual(key.stat().st_ino, inode)
        self.assertEqual(self.files(), ["key.pem", "request.csr", "routes.conf"])
        other = self.tmp / "other.key"
        openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", other)
        self.ok("csr", "--key", other)  # the installed key stays until the other one has a certificate
        self.assertEqual(public_key(key, "pkey"), public_key(existing, "pkey"))
        self.assertEqual(public_key(self.dir / "key.new.pem", "pkey"), public_key(other, "pkey"))
        self.assertIn("not a private key", self.refused("csr", "--key", self.routes))
        encrypted = self.tmp / "encrypted.key"  # its pass phrase cannot be asked for without a terminal
        openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-aes256", "-pass", "pass:secret",
                "-out", encrypted)
        self.assertIn("needs its pass phrase on a terminal", self.refused("csr", "--key", encrypted))
        self.assertEqual(self.files(), ["key.new.pem", "key.pem", "request.csr", "routes.conf"])

    def test_install_cert_reads_pem_der_and_pkcs7_and_orders_the_chain(self):
        self.ok("csr")
        leaf, fullchain = self.signed(), self.dir / "fullchain.pem"
        result = self.ok("install-cert", leaf, self.ca.d / "int.pem")
        self.assertEqual(subjects(fullchain), [LEAF, INTERMEDIATE])
        self.assertIn("Next: sudo bash https.sh apply", result.stdout)
        bundle = self.tmp / "bundle.p7b"  # root, leaf and intermediate in one DER PKCS #7 file, out of order
        openssl("crl2pkcs7", "-nocrl", "-certfile", self.ca.d / "root.pem", "-certfile", leaf,
                "-certfile", self.ca.d / "int.pem", "-outform", "DER", "-out", bundle)
        microsoft = self.tmp / "certnew.p7b"  # PEM PKCS #7 labelled CERTIFICATE, as Microsoft CAs hand it out
        openssl("crl2pkcs7", "-nocrl", "-certfile", leaf, "-certfile", self.ca.d / "int.pem", "-out", microsoft)
        microsoft.write_text(microsoft.read_text().replace("PKCS7", "CERTIFICATE"))
        # The root also cross-signed by an older one: that copy is served, for clients that trust only the older
        # root, unless it has expired. The self-signed root is the clients' own.
        expired, bridged = self.tmp / "expired.pem", self.tmp / "bridged.pem"
        for answer, cross in ((expired, self.ca.cross_signed_root("stale", days_from=-60, days_to=-30)),
                              (bridged, self.ca.cross_signed_root("cross"))):
            answer.write_text("".join(p.read_text() for p in (leaf, cross, self.ca.d / "int.pem", self.ca.d / "root.pem")))
        for answer, chain in ((bundle, [LEAF, INTERMEDIATE]), (microsoft, [LEAF, INTERMEDIATE]),
                              (expired, [LEAF, INTERMEDIATE]), (bridged, [LEAF, INTERMEDIATE, ROOT])):
            with self.subTest(answer=answer.name):
                fullchain.unlink()
                self.ok("install-cert", answer)
                self.assertEqual(subjects(fullchain), chain)
        served = self.tmp / "served.pem"  # what nginx sends after the leaf: enough for a client of the older root only
        served.write_text("".join(blocks(fullchain)[1:]))
        openssl("verify", "-CAfile", self.ca.d / "old.pem", "-untrusted", served, leaf)
        direct = self.ca.sign(self.dir / "request.csr", self.tmp / "direct.pem", ca="root")  # no intermediate at all
        result = self.ok("install-cert", direct, self.ca.d / "root.pem")
        self.assertEqual(subjects(fullchain), [LEAF])
        self.assertNotIn("No intermediate", result.stderr)
        der = self.tmp / "leaf.cer"
        openssl("x509", "-in", leaf, "-outform", "DER", "-out", der)
        result = self.ok("install-cert", der)
        self.assertEqual(subjects(fullchain), [LEAF])
        self.assertIn("No intermediate certificate", result.stderr)

    def test_install_cert_refuses_a_certificate_that_does_not_fit(self):
        self.ok("csr")
        other_key, other_request = self.tmp / "other.key", self.tmp / "other.csr"
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", other_key, "-out", other_request,
                "-subj", "/CN=api.example.perodua.com.my", "-addext", f"subjectAltName=DNS:{HOSTS[0]},DNS:{HOSTS[1]}")
        partial = self.tmp / "partial.csr"  # this server's key, but one host name only
        openssl("req", "-new", "-key", self.dir / "key.pem", "-out", partial, "-subj", "/CN=api.example.perodua.com.my",
                "-addext", f"subjectAltName=DNS:{HOSTS[0]}")
        intermediate = self.ca.d / "int.pem"
        cases = [(self.ca.sign(other_request, self.tmp / "other.pem"), intermediate, "belongs to this server's private key"),
                 (self.ca.sign(partial, self.tmp / "partial.pem"), intermediate, "does not cover: stgissrp.perodua.com.my"),
                 (self.signed("expired.pem", days_from=-60, days_to=-30), intermediate, "The certificate expired"),
                 (self.signed("later.pem", days_from=4 / 1440), intermediate, "valid only from"),  # in 4 minutes
                 # an intermediate with the right name but another key did not sign it
                 (self.signed("right.pem"), self.ca.intermediate("impostor"), "does not verify with its chain"),
                 # for TLS clients only, signed by the root itself (no intermediate to check it with), or alone
                 (self.ca.sign(self.dir / "request.csr", self.tmp / "client.pem", ca="root",
                               ext="extendedKeyUsage=clientAuth\n"), self.ca.d / "root.pem", "unsuitable certificate purpose"),
                 (self.tmp / "client.pem", None, "not meant for a TLS server")]
        for cert, chain, message in cases:
            with self.subTest(message=message):
                self.assertIn(message, self.refused("install-cert", cert, *([chain] if chain else [])))
                self.assertEqual(self.files(), ["key.pem", "request.csr", "routes.conf"])
        result = self.ok("install-cert", self.signed("soon.pem", days_to=10), intermediate)
        self.assertIn("expires within 30 days", result.stderr)

    def test_a_new_key_takes_over_with_its_certificate(self):
        self.installed()
        self.ok("csr", "--new-key")
        new_key = public_key(self.dir / "key.new.pem", "pkey")
        self.ok("install-cert", self.signed("new.pem"), self.ca.d / "int.pem")
        self.assertEqual(public_key(self.dir / "key.pem", "pkey"), new_key)
        self.assertEqual(public_key(self.dir / "fullchain.pem", "x509"), new_key)
        self.assertEqual(self.files(), ["fullchain.pem", "key.pem", "request.csr", "routes.conf"])

    def test_a_host_whose_port_is_not_known_is_in_the_certificate_and_answers_404(self):
        self.routes.write_text(ROUTES + "wom.example.perodua.com.my /dev/ -\n")
        self.ok("csr")
        self.ok("install-cert", self.signed(), self.ca.d / "int.pem")
        result = self.ok("apply")
        self.assertIn("not served yet (port -", result.stdout)
        self.assertIn("https://wom.example.perodua.com.my/dev/", result.stdout)
        conf = self.conf.read_text()
        wom = conf[conf.index("server_name wom.example.perodua.com.my;"):]
        wom = wom[:wom.index("\n}\n")]
        self.assertNotIn("proxy_pass", wom)
        self.assertIn("location / {\n        return 404;", wom)

    def test_apply_needs_a_certificate(self):
        self.routes.write_text(ROUTES)
        self.ok("csr")
        self.assertIn("No certificate is installed yet", self.refused("apply"))
        self.ok("install-cert", self.signed(), self.ca.d / "int.pem")
        self.routes.write_text(ROUTES + "other.example.perodua.com.my /dev/ 8120\n")  # a host added after the certificate
        self.assertIn("does not cover other.example.perodua.com.my", self.refused("apply"))
        self.assertFalse(self.conf.exists())

    def test_apply_writes_the_routes_into_nginx(self):
        self.installed()
        result = self.ok("apply")
        conf = self.conf.read_text()
        self.assertIn(f"listen 80;\n    server_name {' '.join(HOSTS)};\n    return 301 https://$host$request_uri;", conf)
        for host in HOSTS:
            self.assertIn(f"listen 443 ssl;\n    server_name {host};\n    ssl_certificate {self.dir}/fullchain.pem;\n"
                          f"    ssl_certificate_key {self.dir}/key.pem;", conf)
            # the host name and the caller's address, never what the caller sent
            self.assertIn(f"proxy_set_header Host {host};\n    proxy_set_header X-Forwarded-Host {host};\n"
                          "    proxy_set_header X-Forwarded-For $remote_addr;\n    proxy_set_header X-Forwarded-Proto https;", conf)
        # Odoo's session cookie only over HTTPS; no HSTS: that is the customer's decision
        self.assertEqual(conf.count("\n    proxy_cookie_flags session_id secure samesite=lax;\n"), len(HOSTS))
        self.assertNotIn("Strict-Transport-Security", conf)
        self.assertNotIn("return 302", conf)  # nginx itself sends /dev/api to /dev/api/, the query kept
        # the generation the script asks nginx for, answered to this server only
        self.assertEqual(len(re.findall(r'location = /\.perodua-https \{\n        if \(\$remote_addr != 127\.0\.0\.1\) \{\n'
                                        r'            return 404;\n        \}\n        return 200 "generation [0-9a-f]{16}";', conf)), 2)
        self.assertIn("location /dev/api/ {\n        proxy_pass http://127.0.0.1:8000/;\n    }", conf)  # strip
        self.assertIn("location /uat/api/ {\n        proxy_pass http://127.0.0.1:8001/;\n    }", conf)
        self.assertIn("location /dev/ {\n        proxy_pass http://127.0.0.1:8110;\n    }", conf)  # path kept
        self.assertEqual(conf.count("location / {\n        return 404;"), 2)
        self.assertIn(["nginx", "-t"], self.calls())
        self.assertEqual(self.reloads(), 1)
        self.assertIn("https://api.example.perodua.com.my/dev/api/ -> 127.0.0.1:8000 (path removed)", result.stdout)
        self.assertEqual(self.files(self.conf.parent), [self.conf.name])
        self.assertNotIn("changed since", self.ok("status").stdout)
        self.routes.write_text(ROUTES.replace("stgissrp.perodua.com.my    /uat/       8111", "stgissrp.perodua.com.my / 8110"))
        self.ok("apply")  # a / route takes the whole host: no 404 there
        self.assertEqual(self.conf.read_text().count("location / {\n        return 404;"), 1)
        self.assertIn("location / {\n        proxy_pass http://127.0.0.1:8110;", self.conf.read_text())

    def test_without_http_the_configuration_stays_as_before(self):
        self.routes.write_text(BEFORE_HTTP_TABLE)
        self.installed()
        self.ok("apply")
        text = (BEFORE_HTTP + CATCH_ALL).rstrip("\n").replace("@ROUTES@", str(self.routes)).replace("@DIR@", str(self.dir))
        generation = hashlib.sha256((text + "\n").encode()).hexdigest()[:16]  # as render makes it
        self.assertEqual(self.conf.read_text(), text.replace("@GENERATION@", generation) + "\n")

    def test_http_routes_are_also_served_on_port_80_for_a_tls_front(self):
        api, rp, wom = "api.example.perodua.com.my", "stgissrp.perodua.com.my", "wom.example.perodua.com.my"
        table = (f"{api} /dev/api/ 8000 strip\n{api} /uat/api/ 8001 strip\n{rp} /dev/ 8110\n{rp} /uat/ 8111\n"
                 f"{wom} /dev/ -\n")
        self.routes.write_text(table)
        self.installed()
        self.ok("apply")
        before = self.conf.read_text()
        self.assertNotIn("HTTP answer on port 80", self.ok("status").stdout)
        self.routes.write_text(table.replace("/dev/api/ 8000 strip", "/dev/api/ 8000 strip,http"))
        result = self.ok("apply")
        conf = self.conf.read_text()
        blocks = servers(conf)
        # the host with an http route leaves the shared redirect for a port 80 block of its own
        self.assertEqual(blocks["80", rp], f"    listen 80;\n    server_name {rp} {wom};\n    return 301 https://$host$request_uri;\n}}\n")
        port80 = blocks["80", api]
        self.assertTrue(port80.startswith(f"    listen 80;\n    server_name {api};\n"), port80)
        self.assertNotIn("ssl_", port80)
        self.assertNotIn(".perodua-https", port80)  # the generation probe stays on 443
        # its http route as on 443: the same headers (X-Forwarded-Proto https), cookie flags, limits and strip
        settings = re.compile(r"    # Redirects keep .*?samesite=lax;\n", re.S)
        self.assertEqual(settings.search(port80).group(0), settings.search(blocks["443", api]).group(0))
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", port80)
        self.assertIn("location /dev/api/ {\n        proxy_pass http://127.0.0.1:8000/;\n    }", port80)
        redirect = lambda path: f"\n    location {path} {{\n        return 301 https://$host$request_uri;\n    }}\n"
        self.assertIn(redirect("= /uat/api") + redirect("/uat/api/"), port80)  # no http: redirected
        self.assertNotIn("proxy_pass http://127.0.0.1:8001", port80)
        self.assertFalse(self.lib.exists())  # no renewal timer: no copy of the script
        self.assertTrue(port80.endswith("    # Every other path is closed, as on 443.\n    location / {\n"
                                        "        return 404;\n    }\n}\n"), port80)
        # port 443 does not change
        unchanged = lambda text: re.sub(r"generation [0-9a-f]{16}", "generation", text[text.index("\nserver {\n    listen 443"):])
        self.assertEqual(unchanged(conf), unchanged(before))
        self.assertIn(f"https://{api}/dev/api/ -> 127.0.0.1:8000 (path removed) (also on port 80 over HTTP, for a TLS front)",
                      result.stdout)
        self.assertIn(f"https://{api}/uat/api/ -> 127.0.0.1:8001 (path removed)\n", result.stdout)
        self.assertIn("http:// redirects to https://, except on the routes also on port 80.", result.stdout)
        status = self.ok("status").stdout
        self.assertNotIn("changed since", status)
        self.assertIn(f"https://{api}/dev/api/ -> 127.0.0.1:8000  port listening: NO  HTTPS answer: 200  HTTP answer on port 80: 200\n",
                      status)
        self.assertEqual(status.count("HTTP answer on port 80"), 1)
        self.assertIn(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10", "--resolve",
                       f"{api}:80:127.0.0.1", f"http://{api}/dev/api/"], self.calls())
        # http with strip in either order and on a / route; a route with port - is served on neither port
        self.routes.write_text(f"{api} /dev/api/ 8000 strip,http\n{api} /uat/api/ 8001 http,strip\n{rp} / 8110 http\n"
                               f"{wom} /dev/ - http\n")
        self.ok("apply")
        blocks = servers(self.conf.read_text())
        self.assertIn("location /uat/api/ {\n        proxy_pass http://127.0.0.1:8001/;\n    }", blocks["80", api])
        self.assertIn("location / {\n        proxy_pass http://127.0.0.1:8110;\n    }", blocks["80", rp])
        self.assertNotIn("return 301", blocks["80", rp])  # the / route takes every path
        self.assertEqual(blocks["80", wom], f"    listen 80;\n    server_name {wom};\n    return 301 https://$host$request_uri;\n}}\n")
        self.assertIn("HTTP answer on port 80: -", self.ok("status").stdout)  # wom: not served
        # a route without http under one with it keeps the redirect, with and without its last /
        self.routes.write_text(f"{api} /dev/ 8110 http\n{api} /dev/api/ 8000 strip\n{rp} / 8110 http\n"
                               f"{rp} /dev/api/ 8000 strip\n{wom} / 8120\n{wom} /dev/api/ 8000 strip,http\n")
        self.ok("apply")
        blocks = servers(self.conf.read_text())
        for host in (api, rp):
            self.assertIn(redirect("= /dev/api") + redirect("/dev/api/"), blocks["80", host])
        self.assertIn("location /dev/ {\n        proxy_pass http://127.0.0.1:8110;\n    }", blocks["80", api])
        self.assertIn("location / {\n        proxy_pass http://127.0.0.1:8110;\n    }", blocks["80", rp])
        self.assertIn(redirect("/") + "\n    location /dev/api/ {\n        proxy_pass http://127.0.0.1:8000/;\n    }\n}\n",
                      blocks["80", wom])
        self.assertEqual(blocks["80", wom].count("    location "), 2)  # the / route without http is the redirect
        self.assertIn("location /dev/api/ {\n        proxy_pass http://127.0.0.1:8000/;\n    }", blocks["443", api])
        # every host name with an http route: no shared redirect
        self.routes.write_text(f"{api} /dev/api/ 8000 strip,http\n{rp} /dev/ 8110 http\n")
        self.ok("apply")
        conf = self.conf.read_text()
        self.assertEqual(sorted(servers(conf)), [("443", "_"), ("443", api), ("443", rp), ("80", api), ("80", rp)])
        self.assertEqual(conf.count("\n    listen 80;\n"), 2)

    def test_api_routes_close_the_documentation_and_are_rate_limited(self):
        api, rp, wom = "api.example.perodua.com.my", "stgissrp.perodua.com.my", "wom.example.perodua.com.my"
        self.routes.write_text(f"{api} /dev/api/ 8000 strip,http,api\n{api} /uat/api/ 8001 strip,api\n{api} /sit/api/ 8002 strip\n"
                               f"{rp} /dev/ 8110\n{wom} /dev/ - api\n")
        self.installed()
        result = self.ok("apply")
        conf = self.conf.read_text()
        zone = "limit_req_zone $binary_remote_addr zone=perodua_https_api:10m rate=10r/s;\n"
        self.assertEqual(conf.count("limit_req_zone"), 1)
        self.assertIn("    '' close;\n}\n\n" + zone + "\nserver {\n", conf)
        self.assertLess(conf.index(zone), conf.index("server {"))
        docs = lambda path: "".join(f"\n    location = {path}{name} {{\n        return 404;\n    }}\n"
                                    for name in ("docs", "redoc", "openapi.json"))
        limited = lambda path, port: (f"\n    location {path} {{\n        limit_req zone=perodua_https_api burst=20 nodelay;\n"
                                      f"        limit_req_status 429;\n        proxy_pass http://127.0.0.1:{port}/;\n    }}\n")
        blocks = servers(conf)
        self.assertIn(docs("/dev/api/") + limited("/dev/api/", 8000), blocks["443", api])
        self.assertIn(docs("/uat/api/") + limited("/uat/api/", 8001), blocks["443", api])
        self.assertIn("\n    location /sit/api/ {\n        proxy_pass http://127.0.0.1:8002/;\n    }\n", blocks["443", api])
        self.assertEqual(blocks["443", api].count("limit_req "), 2)
        self.assertEqual(blocks["443", api].count("return 404;\n    }\n"), 7)
        port80 = blocks["80", api]
        self.assertIn(docs("/dev/api/") + limited("/dev/api/", 8000), port80)
        self.assertEqual(port80.count("limit_req "), 1)
        self.assertNotIn("/uat/api/docs", port80)
        self.assertIn("\n    location /uat/api/ {\n        return 301 https://$host$request_uri;\n    }\n", port80)
        for block in (blocks["443", rp], blocks["443", wom], blocks["80", rp]):
            self.assertNotIn("limit_req", block)
            self.assertNotIn("docs", block)
        marker = " (API: documentation closed, at most 10 requests a second per caller address, bursts of 20)"
        self.assertIn(f"https://{api}/dev/api/ -> 127.0.0.1:8000 (path removed) (also on port 80 over HTTP, for a TLS front){marker}\n",
                      result.stdout)
        self.assertIn(f"https://{api}/uat/api/ -> 127.0.0.1:8001 (path removed){marker}\n", result.stdout)
        self.assertIn(f"https://{api}/sit/api/ -> 127.0.0.1:8002 (path removed)\n", result.stdout)
        self.assertIn(f"https://{rp}/dev/ -> 127.0.0.1:8110\n", result.stdout)
        self.assertEqual(result.stdout.count("(API: "), 2)
        status = self.ok("status").stdout
        self.assertNotIn("changed since", status)
        self.assertIn(f"https://{api}/dev/api/ -> 127.0.0.1:8000  port listening: NO  HTTPS answer: 200  HTTP answer on port 80: 200"
                      "  API: docs closed, rate limited\n", status)
        self.assertIn(f"https://{api}/uat/api/ -> 127.0.0.1:8001  port listening: NO  HTTPS answer: 200  API: docs closed, rate limited\n",
                      status)
        self.assertEqual(status.count("API: docs closed"), 2)
        self.routes.write_text(f"{api} /dev/api/ 8000 strip\n{rp} /dev/ 8110\n{wom} /dev/ - api\n")
        self.ok("apply")
        conf = self.conf.read_text()
        self.assertNotIn("limit_req", conf)
        self.assertNotIn("docs", conf)
        self.assertNotIn("API: ", self.ok("status").stdout)
        self.routes.write_text(f"{api} / 8000 api\n")
        self.ok("apply")
        blocks = servers(self.conf.read_text())
        self.assertIn(docs("/") + "\n    location / {\n        limit_req zone=perodua_https_api burst=20 nodelay;\n"
                      "        limit_req_status 429;\n        proxy_pass http://127.0.0.1:8000;\n    }\n}\n", blocks["443", api])

    def test_an_api_route_on_port_80_keeps_its_404_over_the_redirect_of_a_route_under_it(self):
        api, rp = "api.example.perodua.com.my", "stgissrp.perodua.com.my"
        self.routes.write_text(f"{api} /dev/api/ 8000 strip,http,api\n{api} /dev/api/docs/ 8001\n{api} /dev/api/redoc/ 8002\n"
                               f"{api} /dev/api/openapi.json/ 8003\n{api} /dev/api/x/ 8004\n{rp} / 8110 http,api\n{rp} /docs/ 8111\n"
                               f"{rp} /uat/api/ 8001 strip,api\n{rp} /uat/api/docs/ 8002\n")
        self.installed()
        self.ok("apply")
        blocks = servers(self.conf.read_text())
        closed = lambda path: f"\n    location = {path} {{\n        return 404;\n    }}\n"
        redirect = lambda path: f"\n    location {path} {{\n        return 301 https://$host$request_uri;\n    }}\n"
        for host, path, under in ((api, "/dev/api/", ("docs", "redoc", "openapi.json")), (rp, "/", ("docs",))):
            for name in under:
                for port in ("443", "80"):
                    self.assertEqual(blocks[port, host].count(f"location = {path}{name} "), 1, (port, host, name))
                    self.assertIn(closed(path + name), blocks[port, host])
                self.assertIn(redirect(f"{path}{name}/"), blocks["80", host])
                self.assertIn(f"\n    location {path}{name}/ {{\n        proxy_pass http://127.0.0.1:", blocks["443", host])
        self.assertIn(redirect("= /dev/api/x") + redirect("/dev/api/x/"), blocks["80", api])
        self.assertIn(redirect("= /uat/api") + redirect("/uat/api/") + redirect("= /uat/api/docs") + redirect("/uat/api/docs/"),
                      blocks["80", rp])

    def test_the_help_and_apply_take_the_numbers_of_api_rate_and_api_burst(self):
        script = self.tmp / "scripts" / "https.sh"
        script.parent.mkdir()
        text = SCRIPT.read_text()
        self.assertEqual(text.count("\nAPI_RATE=10r/s API_BURST=20\n"), 1)
        script.write_text(text.replace("\nAPI_RATE=10r/s API_BURST=20\n", "\nAPI_RATE=7r/s API_BURST=9\n"))
        self.routes.write_text("api.example.perodua.com.my /dev/api/ 8000 strip,api\n")
        self.installed()
        help_text = " ".join(self.ok("--help", script=script).stdout.split())
        self.assertIn("most 7 requests a second from each caller address, for all api routes together, in bursts of 9 (429",
                      help_text)
        result = self.ok("apply", script=script)
        self.assertIn(" (API: documentation closed, at most 7 requests a second per caller address, bursts of 9)\n", result.stdout)
        conf = self.conf.read_text()
        self.assertIn("limit_req_zone $binary_remote_addr zone=perodua_https_api:10m rate=7r/s;\n", conf)
        self.assertIn("        limit_req zone=perodua_https_api burst=9 nodelay;\n", conf)
        self.assertNotIn("burst=20", conf)

    def test_port_443_closes_other_host_names(self):
        self.installed()
        result = self.ok("apply")
        conf = self.conf.read_text()
        self.assertTrue(conf.endswith(CATCH_ALL.replace("@DIR@", str(self.dir))), conf)
        self.assertEqual(conf.count("default_server"), 1)
        self.assertEqual(servers(conf)["443", "_"], CATCH_ALL.replace("@DIR@", str(self.dir)).split("server {\n", 1)[1])
        self.assertIn("Other host names on port 443: the connection is closed.\n", result.stdout)
        status = self.ok("status").stdout
        self.assertIn("nginx:       " + str(self.conf) + "\n             other host names on port 443: the connection is closed\n",
                      status)
        self.ok("uninstall", "--confirm", "yes")
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.reloads(), 1)

    def test_apply_refuses_another_default_server_for_port_443(self):
        self.installed()
        stock = ("server {\n\tlisten 80 default_server;\n\tlisten [::]:80 default_server;\n\n\t# SSL configuration\n\t#\n"
                 "\t# listen 443 ssl default_server;\n\t# listen [::]:443 ssl default_server;\n\n\troot /var/www/html;\n"
                 "\tserver_name _;\n}\n")
        fine = {"/etc/nginx/sites-enabled/default": stock,
                "/etc/nginx/conf.d/tls.conf": "server {\n    listen 443 ssl;\n    server_name other.example.com;\n}",
                "/etc/nginx/conf.d/alt.conf": 'server { listen 8443 ssl default_server; return 200 "listen 443 default_server"; }',
                "/etc/nginx/conf.d/port.conf": "server { listen 127.0.0.1:4430 default; }"}
        cases = [("/etc/nginx/conf.d/x.conf", "server { listen 443 ssl default_server; server_name x.example.com; }",
                  "listen 443 ssl default_server"),
                 ("/etc/nginx/conf.d/old.conf", "server {\n    listen *:443\n        default ssl;\n}", "listen *:443 default ssl"),
                 ("/etc/nginx/sites-enabled/v6", "server { listen '[::]:443' ssl default_server; }", "listen [::]:443 ssl default_server"),
                 ("/etc/nginx/conf.d/any.conf", "server { listen 0.0.0.0:443 default_server ssl; }",
                  "listen 0.0.0.0:443 default_server ssl")]
        for path, text, statement in cases:
            with self.subTest(statement=statement):
                self.set_state(others=dict(fine, **{path: text}))
                stderr = self.refused("apply")
                self.assertIn("Another nginx configuration is already the default server for port 443; remove default_server there "
                              f"first:\n{path}: {statement}\n", stderr)
                self.assertEqual(stderr.count("default server for port 443"), 1)
                self.assertFalse(self.conf.exists())
                self.assertEqual(self.reloads(), 0)
        self.set_state(others=fine)
        self.ok("apply")
        self.assertEqual(self.calls().count(["nginx", "-T"]), 1)
        self.ok("apply")
        before = self.conf.read_text()
        self.set_state(others=dict(fine, **{cases[0][0]: cases[0][1]}))
        self.assertIn(f"{cases[0][0]}: {cases[0][2]}", self.refused("apply"))
        self.assertEqual(self.conf.read_text(), before)
        self.assertEqual(self.reloads(), 0)

    def test_apply_refuses_host_names_that_another_configuration_serves(self):
        self.installed()
        self.set_state(others={
            "/etc/nginx/conf.d/old.conf": "server { listen 80; server_name stgissrp.perodua.com.my; return 200 old; }",
            "/etc/nginx/sites-enabled/api": 'server {\n    listen 443 ssl;\n    server_name\n        www.example.com\n'
                                            '        "API.example.perodua.com.my";\n}',
            "/etc/nginx/conf.d/fine.conf": "server {\n    server_name *.perodua.com.my;  # server_name stgissrp.perodua.com.my;\n}",
            "/etc/nginx/conf.d/hash.conf": 'server { set $tag "#"; server_name api.example.perodua.com.my; }'})
        stderr = self.refused("apply")
        self.assertIn("/etc/nginx/conf.d/old.conf: stgissrp.perodua.com.my", stderr)
        self.assertIn("/etc/nginx/sites-enabled/api: api.example.perodua.com.my", stderr)
        self.assertIn("/etc/nginx/conf.d/hash.conf: api.example.perodua.com.my", stderr)  # a quoted # is no comment
        self.assertNotIn("fine.conf", stderr)
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.reloads(), 0)

    def test_apply_refuses_ports_another_program_listens_on(self):
        self.installed()
        proxy, proxy_pid = self.program("docker-proxy")
        _, nginx_pid = self.program("nginx")
        self.listening(("nginx", nginx_pid), ('docker"proxy', proxy_pid))  # ss prints names unescaped
        stderr = self.refused("apply")
        self.assertIn(f"80: docker-proxy ({proxy})", stderr)
        self.assertNotIn("80: nginx", stderr)  # the nginx on the PATH may have them
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.calls(), [])  # before nginx is installed or asked anything
        self.listening(("nginx", nginx_pid))
        self.ok("apply")

    def test_apply_does_not_install_a_second_nginx(self):
        self.installed()
        self.without_nginx()
        other = self.other_nginx()
        stderr = self.refused("apply")
        self.assertIn("an nginx listed here is another installation than the one apply installs with apt", stderr)
        self.assertIn(f"80: nginx ({other})", stderr)
        self.assertEqual(self.calls(), [])  # neither apt-get nor nginx
        self.assertFalse(self.conf.exists())

    def test_apply_explains_a_failed_nginx_installation(self):
        self.installed()
        self.without_nginx()
        stderr = self.refused("apply")
        self.assertIn("E: Unable to fetch some archives", stderr)  # apt's own words first
        self.assertIn("Could not install nginx with apt-get (above). A server without internet access needs an apt proxy", stderr)
        self.assertEqual([call[:2] for call in self.calls()], [["apt-get", "install"], ["apt-get", "update"]])
        self.assertFalse(self.conf.exists())

    def test_status_says_what_apply_will_meet(self):
        self.without_nginx()
        result = self.ok("status")
        self.assertIn("nginx:       not installed yet: apply installs it\n", result.stdout)
        self.assertNotIn("Ports 80/443", result.stdout)
        other = self.other_nginx()
        result = self.ok("status")
        self.assertIn("Ports 80/443: in use by what apply cannot work next to", result.stdout)
        self.assertIn(f"80: nginx ({other})", result.stdout)

    def test_a_configuration_nginx_refuses_is_taken_back(self):
        self.installed()
        self.ok("apply")
        before = self.conf.read_text()
        self.routes.write_text(ROUTES + "stgissrp.perodua.com.my /sit/ 8112\n")
        self.set_state(t_status=1)  # nginx -t refuses the new file
        self.assertIn("nginx -t refused the change", self.refused("apply"))
        self.assertEqual(self.conf.read_text(), before)
        self.assertEqual(self.reloads(), 0)
        self.set_state(T_status=1)  # the configuration is broken before the script runs
        self.assertIn("The current nginx configuration fails nginx -t", self.refused("apply"))
        self.assertEqual(self.conf.read_text(), before)
        self.assertEqual(self.files(self.conf.parent), [self.conf.name])

    def test_a_configuration_nginx_does_not_take_is_taken_back(self):
        self.installed()
        self.set_state(reload_takes=False)  # nginx keeps its old configuration, as when it cannot open a port
        self.assertIn("nginx did not take the change", self.refused("apply"))
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.reloads(), 2)  # with the new file, then without it
        self.assertEqual(self.files(self.conf.parent), [])
        self.set_state()
        self.ok("apply")
        before = self.conf.read_text()
        self.routes.write_text(ROUTES + "stgissrp.perodua.com.my /sit/ 8112\n")
        self.set_state(reload_takes=False)  # the same certificate as before: only the generation tells
        self.assertIn("nginx did not take the change", self.refused("apply"))
        self.assertEqual(self.conf.read_text(), before)

    def test_a_renewed_certificate_is_served_at_once(self):
        self.installed()
        self.ok("apply")
        self.ok("csr")
        result = self.ok("install-cert", self.signed("renewed.pem"), self.ca.d / "int.pem")
        self.assertIn("nginx serves it now", result.stdout)
        self.assertIn(["nginx", "-t"], self.calls())
        self.assertEqual(self.reloads(), 1)

    def test_a_certificate_nginx_refuses_is_taken_back(self):
        self.installed()
        self.ok("apply")
        names = ("fullchain.pem", "key.pem", "key.new.pem")
        self.ok("csr", "--new-key")
        before = {name: (self.dir / name).read_bytes() for name in names}
        self.set_state(t_status=1)
        self.assertIn("nginx -t refused the change", self.refused("install-cert", self.signed("new.pem"), self.ca.d / "int.pem"))
        self.assertEqual({name: (self.dir / name).read_bytes() for name in names}, before)
        self.assertEqual(self.reloads(), 0)
        self.set_state(reload_takes=False)  # nginx -t passes, but nginx keeps the old certificate
        self.assertIn("nginx did not take the change", self.refused("install-cert", self.tmp / "new.pem", self.ca.d / "int.pem"))
        self.assertEqual({name: (self.dir / name).read_bytes() for name in names}, before)
        self.assertEqual(self.files(), ["fullchain.pem", "key.new.pem", "key.pem", "request.csr", "routes.conf"])

    def test_a_new_chain_for_the_same_certificate_must_be_taken(self):
        self.ok("csr")
        leaf, der = self.signed(), self.tmp / "leaf.cer"
        openssl("x509", "-in", leaf, "-outform", "DER", "-out", der)
        self.ok("install-cert", der)  # without its intermediate at first
        self.ok("apply")
        self.set_state(reload_takes=False)  # the same leaf: only the chain nginx sends tells
        self.assertIn("nginx did not take the change", self.refused("install-cert", leaf, self.ca.d / "int.pem"))
        self.assertEqual(subjects(self.dir / "fullchain.pem"), [LEAF])
        self.set_state()
        self.ok("install-cert", leaf, self.ca.d / "int.pem")
        self.assertEqual(subjects(self.dir / "fullchain.pem"), [LEAF, INTERMEDIATE])

    def test_status_reports_the_certificate_and_changed_routes(self):
        self.installed()
        self.ok("apply")
        self.routes.write_text(ROUTES + "stgissrp.perodua.com.my /sit/ 8112\n")
        result = self.ok("status")
        self.assertIn("issued by CN=Test Intermediate", result.stdout)
        self.assertIn("the routes or https.sh changed since: apply", result.stdout)
        self.assertIn("https://stgissrp.perodua.com.my/sit/ -> 127.0.0.1:8112", result.stdout)

    def test_the_nginx_file_of_another_dir_is_left_alone(self):
        self.installed()
        self.ok("apply")
        before = self.conf.read_text()
        other = self.tmp / "other"
        other.mkdir()
        self.assertIn("was not written by apply for", self.refused("apply", "--routes", self.routes, directory=other))
        self.assertIn("was not written by apply for", self.refused("uninstall", "--confirm", "yes", directory=other))
        self.assertEqual(self.conf.read_text(), before)

    def test_uninstall_needs_a_confirmation_and_removes_only_its_files(self):
        self.installed()
        self.ok("apply")
        self.assertIn("pass --confirm yes. Nothing was changed", self.refused("uninstall"))
        self.assertTrue(self.conf.exists())
        listening = 'LISTEN 0 511 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=8,fd=7))\n'
        for state, message in (({"t_status": 1}, "nginx -t fails without this configuration"),  # another file is broken
                               ({"reload_takes": False}, "nginx still runs this configuration"),  # nginx keeps it
                               # nginx keeps it and does not answer while it listens: no answer proves nothing
                               ({"reload_takes": False, "probe_fails": True, "ss": listening}, "or did not answer")):
            with self.subTest(message=message):
                self.set_state(**state)
                self.assertIn(message, self.refused("uninstall", "--purge", "--confirm", "yes"))
                self.assertTrue(self.conf.exists())
                self.assertEqual(self.files(), ["fullchain.pem", "key.pem", "request.csr", "routes.conf"])
        self.set_state()
        self.ok("uninstall", "--confirm", "yes")
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.reloads(), 1)
        self.assertTrue((self.dir / "key.pem").exists())
        (self.dir / "notes.txt").write_text("the administrator's")
        self.ok("uninstall", "--purge", "--confirm", "yes")
        self.assertEqual(self.files(), ["notes.txt"])

    def test_a_held_lock_stops_every_change(self):
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for args in (["csr"], ["install-cert", self.ca.d / "int.pem"], ["apply"], ["uninstall", "--confirm", "yes"],
                     ["letsencrypt", "--accept-tos"]):
            with self.subTest(args=args):
                self.assertIn("Another https.sh command is running", self.refused(*args))
        self.assertEqual(self.files(), ["routes.conf"])


def servers(conf):  # {(port, host name): its server block}; a host name twice on one port fails
    found = {}
    for block in conf.split("\nserver {\n")[1:]:
        port = re.search(r"^    listen (\d+)", block, re.M).group(1)
        for name in re.search(r"^    server_name ([^;]+);", block, re.M).group(1).split():
            assert (port, name) not in found, (port, name)
            found[port, name] = block
    return found


def given(call, *words):  # whether the words follow each other in the call's arguments
    return "\0" + "\0".join(map(str, words)) + "\0" in "\0" + "\0".join(call) + "\0"


def days_left(path):
    end = openssl("x509", "-in", path, "-noout", "-enddate").strip().split("=", 1)[1]
    end = datetime.datetime.strptime(end, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
    return (end - datetime.datetime.now(datetime.timezone.utc)).days


@needs_root
class LetsEncryptTest(Base):
    def le(self, *args, **options):
        return self.run_script("letsencrypt", *args, **options)

    def le_ok(self, *args, **options):
        result = self.le(*args, **options)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def root(self):  # the throwaway root, which no system trusts, as the staging roots
        return ["--accept-tos", "--extra-root", self.ca.d / "root.pem"]

    def accounts(self):
        return json.loads((self.le_dir / "acme-dns.json").read_text())

    def records(self):  # the CNAME records the DNS administrator creates, as {name: target}
        return {f"_acme-challenge.{host}": account["fulldomain"] for host, account in self.accounts().items()}

    def ready(self, **options):  # the acme-dns accounts made, and their records in this server's DNS
        first = self.le(*self.root(), **options)
        self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
        self.set_state(dns={"": self.records()})

    def le_installed(self):
        self.ready()
        return self.le_ok(*self.root())

    def lego_runs(self):
        return [call for call in self.calls() if call[:2] == ["docker", "run"]]

    def registrations(self):
        return [call for call in self.calls() if call[0] == "curl" and call[-1].endswith("/register")]

    @staticmethod
    def mounts(run):  # the -v arguments of a docker run, as (source on this server, rest)
        return [tuple(run[i + 1].split(":", 1)) for i, arg in enumerate(run) if arg == "-v"]

    def assert_lego_sees_only_its_files(self, run, ca=None):
        """lego's container mounts its own folder, the copy of the accounts, the request and the ACME CA (if any):
        not acme-dns.json, not the rest of the Let's Encrypt folder, not the private key."""
        expected = [(f"{self.le_dir}/lego", "/data/lego"), (f"{self.le_dir}/acme-dns.lego.json", "/data/acme-dns.lego.json"),
                    (f"{self.dir}/request.csr", "/request.csr:ro")]
        if ca:
            expected.append((f"{self.le_dir}/{ca}", "/data/acme-ca.pem:ro"))
        self.assertEqual(self.mounts(run), expected)
        for secret in (self.le_dir / "acme-dns.json", self.dir / "key.pem", self.le_dir / "settings"):
            self.assertFalse([source for source, _ in self.mounts(run) if secret.is_relative_to(source)], secret)

    def lego_storage(self):  # the accounts file lego was given: (its path in the container, its content, its mode)
        [(_, path, content, mode)] = [call for call in self.calls() if call[0] == "lego-storage"]
        return path, content, mode

    def stop_hard_at(self, program, word):
        """The next call of PROGRAM with WORD in its arguments kills the script with SIGKILL, as a power cut
        would stop it: its EXIT trap does not run. PROGRAM stays what it was otherwise (a fake or the real one)."""
        wrapper = self.bin_dir / program
        self.wrapped = getattr(self, "wrapped", {})
        if program not in self.wrapped:  # the fake of this test, or the real program
            if wrapper.exists():
                self.wrapped[program] = wrapper.rename(self.bin_dir / f"{program}.real")
            else:
                self.wrapped[program] = Path(shutil.which(program))
        real = self.wrapped[program]
        switch = self.tmp / f"stop-at-{program}"
        switch.touch()
        wrapper.write_text(f'#!/bin/sh\nif [ -e "{switch}" ]; then\n    case " $* " in *"{word} "*)\n'
                           f'        rm -f "{switch}"; kill -9 "$PPID"; exit 137 ;;\n    esac\nfi\nexec "{real}" "$@"\n')
        wrapper.chmod(0o755)
        return switch

    def recorded(self):  # the fingerprints in LE_DIR/installed, which the renewal knows its certificates by
        return (self.le_dir / "installed").read_text().splitlines()

    @staticmethod
    def fingerprint(path):
        return openssl("x509", "-in", path, "-noout", "-fingerprint", "-sha256").strip().split("=", 1)[1]

    def unit(self, suffix):
        return (self.units / f"{UNIT}.{suffix}").read_text()

    def test_the_first_run_prints_the_records_and_stops(self):
        result = self.le(*self.root())
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stderr, "")  # nothing went wrong: the records are missing, and that is said plainly
        accounts = self.accounts()
        self.assertEqual(sorted(accounts), HOSTS)
        for host in HOSTS:
            self.assertIn(f"\n_acme-challenge.{host} CNAME {accounts[host]['fulldomain']}\n", result.stdout)
            self.assertIn(f"  _acme-challenge.{host}: NOT FOUND (its account was made now)\n", result.stdout)
            self.assertEqual(sorted(accounts[host]), ["fulldomain", "password", "server_url", "subdomain", "username"])
            self.assertEqual(accounts[host]["server_url"], ACME_DNS)
        self.assertIn("When the records exist, run the same command again.", result.stdout)
        # the script made the accounts, as goacmedns would: one POST each, and no lego, no order at the ACME server
        registrations = self.registrations()
        self.assertEqual(len(registrations), 2)
        self.assertTrue(all(given(call, "-X", "POST") and "--cacert" not in call for call in registrations))
        self.assertEqual(self.lego_runs(), [])
        self.assertEqual([call[-1] for call in self.calls() if call[0] == "curl" and "/register" not in call[-1]],
                         [LE_SERVER, f"{ACME_DNS}/health"])  # the directory only
        self.assertEqual([call for call in self.calls() if call[0] == "dig"], [])  # new records are not looked up
        # one line of JSON, as goacmedns writes it; the key and the request, as csr makes them; root only
        self.assertEqual(len((self.le_dir / "acme-dns.json").read_text().splitlines()), 1)
        self.assertEqual((self.dir / "key.pem").stat().st_mode & 0o777, 0o600)
        self.assertEqual(dns_names(self.dir / "request.csr", "req"), HOSTS)
        self.assertEqual(self.le_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.le_dir / "acme-dns.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.dir / "fullchain.pem").exists())
        self.assertFalse(self.units.exists())
        # again before the records exist: it stops without asking Let's Encrypt, with the same records
        result = self.le(*self.root())
        self.assertEqual(result.returncode, 3, result.stderr)
        for host in HOSTS:
            self.assertIn(f"  _acme-challenge.{host}: NOT FOUND\n", result.stdout)
        self.assertEqual(self.lego_runs(), [])
        self.assertEqual(self.registrations(), [])
        self.assertEqual(self.accounts(), accounts)
        records = self.records()
        self.set_state(dns={"": dict(records, **{f"_acme-challenge.{HOSTS[1]}": "old.acme.example.com"})})
        result = self.le(*self.root())
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(f"  _acme-challenge.{HOSTS[0]}: found\n", result.stdout)
        self.assertIn(f"  _acme-challenge.{HOSTS[1]}: WRONG: it points to old.acme.example.com\n", result.stdout)
        self.assertEqual(self.lego_runs(), [])
        self.set_state(dns={"": records})
        result = self.le_ok(*self.root())
        [run] = self.lego_runs()
        # lego gets its own folder, the copy of the accounts and the request: never acme-dns.json or the private key
        self.assert_lego_sees_only_its_files(run)
        self.assertFalse(any("key" in arg and "pem" in arg for arg in run))
        self.assertTrue(given(run, LEGO_IMAGE, "run"))
        for words in (["--server", LE_SERVER], ["--accept-tos"], ["--csr", "/request.csr"], ["--dns", "acmedns"],
                      ["--path", "/data/lego"], ["--cert.name", "perodua-https"], ["--renew-force"],
                      ["-e", f"ACME_DNS_API_BASE={ACME_DNS}"], ["-e", "ACME_DNS_STORAGE_PATH=/data/acme-dns.lego.json"]):
            self.assertTrue(given(run, *words), words)
        # lego gets a copy of the accounts, in goacmedns' own form, which is removed after it
        path, content, mode = self.lego_storage()
        self.assertEqual((path, mode), ("/data/acme-dns.lego.json", "0o600"))
        self.assertEqual(content, json.dumps(dict(sorted(accounts.items())), separators=(",", ":")))
        self.assertFalse((self.le_dir / "acme-dns.lego.json").exists())
        self.assertNotIn("--email", run)
        self.assertNotIn("--dns.resolvers", run)  # this server's DNS answered: lego uses it too
        self.assertEqual(subjects(self.dir / "fullchain.pem"), [LEAF, INTERMEDIATE])
        self.assertIn("Next: sudo bash https.sh apply", result.stdout)
        self.assertEqual(self.accounts(), accounts)
        self.assertEqual(self.registrations(), [])

    def test_the_certificate_goes_through_the_checks_of_install_cert(self):
        self.ready()
        # without the root it leads to: refused after lego, and nothing changes here
        stderr = self.refused("letsencrypt", "--accept-tos")
        self.assertIn("The certificate does not lead to a root this server trusts", stderr)
        self.assertIn("--extra-root FILE", stderr)
        self.assertEqual(self.files(), ["key.pem", "letsencrypt", "request.csr", "routes.conf"])
        self.assertFalse((self.le_dir / "installed").exists())
        self.assertFalse((self.le_dir / "settings").exists())
        self.assertFalse(self.units.exists())
        self.le_ok(*self.root())
        self.assertEqual(public_key(self.dir / "fullchain.pem", "x509"), public_key(self.dir / "key.pem", "pkey"))
        self.ok("apply")
        before = {name: (self.le_dir / name).read_bytes() for name in ("installed", "settings")}
        before["fullchain.pem"] = (self.dir / "fullchain.pem").read_bytes()
        self.set_state(dns={"": self.records()}, reload_takes=False)  # nginx keeps serving the old one
        self.assertIn("nginx did not take the change", self.refused("letsencrypt", "--email", "new@perodua.com.my"))
        after = {name: (self.le_dir / name).read_bytes() for name in ("installed", "settings")}
        after["fullchain.pem"] = (self.dir / "fullchain.pem").read_bytes()
        self.assertEqual(after, before)  # the certificate, its fingerprint and the options came back together
        self.set_state(dns={"": self.records()})
        result = self.le_ok()
        self.assertIn("nginx serves it now", result.stdout)
        self.assertNotEqual((self.dir / "fullchain.pem").read_bytes(), before)
        self.assertEqual(self.reloads(), 1)
        self.ok("csr", "--new-key")  # a new key waits: the request made with it is used, and the key takes over
        new_key = public_key(self.dir / "key.new.pem", "pkey")
        self.le_ok()
        self.assertEqual(public_key(self.dir / "key.pem", "pkey"), new_key)
        self.assertEqual(public_key(self.dir / "fullchain.pem", "x509"), new_key)

    def test_renew_waits_until_fewer_than_30_days_are_left(self):
        self.le_installed()
        result = self.le_ok("--renew")
        self.assertRegex(result.stdout, r"valid until .* \(8\d days\): it is renewed when fewer than 30 days are left")
        self.assertEqual([call for call in self.calls() if call[0] in ("docker", "curl", "dig")], [])
        self.set_state(dns={"": self.records()}, lego={"days": 20})
        self.assertIn("expires within 30 days", self.le_ok().stderr)
        self.set_state(dns={"": self.records()}, lego={"days": 90})
        result = self.le_ok("--renew")
        self.assertRegex(result.stdout, r"The certificate expires on .* \(1\d days left\): renewing it")
        self.assertEqual(len(self.lego_runs()), 1)
        self.assertGreater(days_left(self.dir / "fullchain.pem"), 80)
        # a certificate installed with install-cert is not the renewal's to replace, even when it is due
        self.ok("install-cert", self.signed("issuer.pem", days_to=10), self.ca.d / "int.pem")
        result = self.le_ok("--renew")
        self.assertIn("was not installed by letsencrypt: --renew leaves it alone", result.stdout)
        self.assertEqual(self.lego_runs(), [])

    def test_an_interrupted_installation_leaves_the_renewal_working(self):
        self.ready()
        self.set_state(dns={"": self.records()}, lego={"days": 20})  # a certificate that is due for renewal
        self.le_ok(*self.root())
        kept = {"fullchain.pem": (self.dir / "fullchain.pem").read_bytes(),
                "installed": (self.le_dir / "installed").read_bytes(), "settings": (self.le_dir / "settings").read_bytes()}
        # The next certificate is written, then the run stops before its fingerprint (installed.tmp)
        # or the options (settings.tmp) are: a directory in the way fails the write, as an interruption would.
        for blocker in ("installed.tmp", "settings.tmp"):
            with self.subTest(blocker=blocker):
                (self.le_dir / blocker).mkdir()
                self.set_state(dns={"": self.records()}, lego={"days": 90})
                result = self.le("--email", "new@perodua.com.my")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                (self.le_dir / blocker).rmdir()
                self.assertEqual({"fullchain.pem": (self.dir / "fullchain.pem").read_bytes(),
                                  "installed": (self.le_dir / "installed").read_bytes(),
                                  "settings": (self.le_dir / "settings").read_bytes()}, kept)
                self.assertFalse([p.name for p in self.le_dir.iterdir() if p.name.endswith(".saved")])
        self.set_state(dns={"": self.records()}, lego={"days": 90})
        result = self.le_ok("--renew")  # the certificate is still the one the renewal knows, and due
        self.assertRegex(result.stdout, r"\(1\d days left\): renewing it")
        self.assertEqual(len(self.lego_runs()), 1)
        self.assertGreater(days_left(self.dir / "fullchain.pem"), 80)
        self.assertEqual(self.recorded(), [self.fingerprint(self.dir / "fullchain.pem")])

    def test_a_hard_stop_during_the_installation_leaves_the_renewal_working(self):
        # A kill or a power cut runs no EXIT trap: nothing is put back, and the .saved copies stay.
        # Whichever certificate such a stop leaves in place, the renewal must still know it.
        self.env["TMPDIR"] = str(self.tmp)  # the killed run's temporary directory, removed with the test's
        self.ready()
        self.set_state(dns={"": self.records()}, lego={"days": 20})  # every certificate here is due for renewal
        self.le_ok(*self.root())
        self.ok("apply")
        # just before the new certificate replaces the old one, and just after (nginx -t comes next)
        for program, word, replaced in (("install", "fullchain.pem.tmp", False), ("nginx", "-t", True)):
            with self.subTest(stop=f"{program} {word}"):
                old = self.fingerprint(self.dir / "fullchain.pem")
                self.assertEqual(self.recorded(), [old])
                switch = self.stop_hard_at(program, word)
                self.set_state(dns={"": self.records()}, lego={"days": 20})
                result = self.le()
                self.assertEqual(result.returncode, -9, result.stdout + result.stderr)  # killed
                self.assertFalse(switch.exists())
                self.assertTrue([p for p in self.dir.rglob("*.saved")])  # as a hard stop leaves them
                now = self.fingerprint(self.dir / "fullchain.pem")
                self.assertEqual(now != old, replaced)
                self.assertIn(now, self.recorded())
                self.assertEqual(len(self.recorded()), 2)  # the one in place before, and the new one
                result = self.le_ok("--renew")
                self.assertRegex(result.stdout, r"\(1\d days left\): renewing it")
                self.assertNotEqual(self.fingerprint(self.dir / "fullchain.pem"), now)
                self.assertEqual(self.recorded(), [self.fingerprint(self.dir / "fullchain.pem")])
                self.assertEqual([p for p in self.dir.rglob("*.saved")], [])
        # a certificate that install-cert put there is not added: after a stop it is still not the renewal's
        self.ok("install-cert", self.signed("issuer.pem", days_to=10), self.ca.d / "int.pem")
        self.stop_hard_at("install", "fullchain.pem.tmp")
        self.assertEqual(self.le().returncode, -9)
        self.assertNotIn(self.fingerprint(self.dir / "fullchain.pem"), self.recorded())
        self.assertIn("was not installed by letsencrypt: --renew leaves it alone", self.le_ok("--renew").stdout)

    def test_a_certificate_nginx_did_not_load_is_loaded_by_the_next_run(self):
        # A hard stop between the new certificate and nginx's reload: B is on disk, valid for 89 days
        # (not due), and nginx still serves A. Every letsencrypt run brings nginx to what is installed.
        self.env["TMPDIR"] = str(self.tmp)
        self.ready()
        records = self.records()
        self.set_state(dns={"": records}, lego={"days": 89})
        self.le_ok(*self.root())
        self.ok("apply")
        running, fullchain = self.tmp / "running.pem", self.dir / "fullchain.pem"  # what nginx serves; what is installed
        self.stop_hard_at("nginx", "-t")
        self.assertEqual(self.le().returncode, -9)
        self.assertNotEqual(running.read_bytes(), fullchain.read_bytes())
        self.assertIn(self.fingerprint(fullchain), self.recorded())

        def files():
            return {p: p.read_bytes() for p in sorted(self.dir.rglob("*")) if p.is_file()}

        # A manual run only warns when nginx -t refuses the files: its own installation checks nginx again
        # (and here, with nginx -t still refusing, puts everything back).
        self.set_state(dns={"": records}, lego={"days": 89}, t_status=1)
        before = files()
        result = self.le()
        self.assertIn("nginx does not serve the installed certificate, and nginx -t refuses the files (above): "
                      "this run's installation checks them again", result.stderr)
        self.assertEqual(len(self.lego_runs()), 1)
        self.assertIn("nginx -t refused the change", result.stderr)

        def kept(snapshot):  # its rollback puts the files back (and uses up the .saved copies the stop left)
            return {p: b for p, b in snapshot.items() if "/lego/" not in str(p) and not p.name.endswith(".saved")}
        self.assertEqual(kept(files()), kept(before))
        # The timer's run, with nothing due: nginx -t refuses the files, so it fails and changes nothing.
        before = files()
        result = self.le("--renew")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"nginx does not serve the installed certificate {fullchain}, and nginx -t refuses the files "
                      "(above), so nginx was not reloaded and nothing was changed", result.stderr)
        self.assertEqual(files(), before)
        self.assertEqual(self.reloads(), 0)
        self.assertEqual(self.lego_runs(), [])
        # nginx -t takes them: the same run reloads nginx, which then serves B; no new certificate is asked for
        self.set_state(dns={"": records}, lego={"days": 89})
        result = self.le_ok("--renew")
        self.assertIn("it is renewed when fewer than 30 days are left", result.stdout)
        self.assertIn(f"nginx was serving an older certificate than {fullchain}: it was reloaded and serves that one now.",
                      result.stdout)
        self.assertEqual(self.reloads(), 1)
        self.assertEqual(self.lego_runs(), [])
        self.assertEqual(running.read_bytes(), fullchain.read_bytes())
        result = self.le_ok("--renew")  # nginx serves it: nothing to do
        self.assertNotIn("nginx was serving", result.stdout)
        self.assertEqual(self.reloads(), 0)

    def test_the_timer_runs_a_copy_of_the_script(self):
        download = self.tmp / "download"
        download.mkdir()
        shutil.copy(SCRIPT, download / "https.sh")
        shutil.copy(EXAMPLE, download / EXAMPLE.name)
        self.ready(script=download / "https.sh")
        result = self.le_ok(*self.root(), script=download / "https.sh")
        copy = self.lib / "https.sh"
        self.assertIn(f"Renewal: {UNIT}.timer runs {copy} daily and renews the certificate when fewer than 30 days are left.",
                      result.stdout)
        self.assertEqual(copy.read_bytes(), SCRIPT.read_bytes())
        self.assertEqual(copy.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.lib / EXAMPLE.name).read_bytes(), EXAMPLE.read_bytes())
        command = (f"/bin/bash {copy} letsencrypt --renew --dir {self.dir} --nginx-conf {self.conf} "
                   f"--unit-dir {self.units} --lib-dir {self.lib}")
        service = self.unit("service")
        self.assertIn(f"\nExecStart={command}\n", service)
        self.assertIn("\nType=oneshot\n", service)
        timer = self.unit("timer")
        for line in ("OnCalendar=*-*-* 02:00:00", "RandomizedDelaySec=4h", "Persistent=true", "WantedBy=timers.target"):
            self.assertIn(f"\n{line}\n", timer)
        self.assertIn(["systemctl", "daemon-reload"], self.calls())
        self.assertIn(["systemctl", "enable", "--now", "--quiet", f"{UNIT}.timer"], self.calls())
        shutil.rmtree(download)  # the timer does not need the downloaded folder
        result = self.run_argv(command.split())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("it is renewed when fewer than 30 days are left", result.stdout)
        self.assertEqual(self.calls(), [])  # the copy rewrites neither itself nor the units
        newer = self.tmp / "newer"  # a later release: its run updates the copy; the units are the same
        newer.mkdir()
        (newer / "https.sh").write_text(SCRIPT.read_text() + "# a later release\n")
        result = self.le_ok("--renew", script=newer / "https.sh")
        self.assertIn(f"Updated the copy of this script that the renewal timer runs: {copy}", result.stdout)
        self.assertEqual(copy.read_text(), (newer / "https.sh").read_text())
        self.assertIn(f"\nExecStart={command}\n", self.unit("service"))
        self.assertNotIn(["systemctl", "daemon-reload"], self.calls())
        self.assertIn(["systemctl", "enable", "--now", "--quiet", f"{UNIT}.timer"], self.calls())

    def test_apply_updates_the_copy_that_the_timer_runs(self):
        self.le_installed()
        copy = self.lib / "https.sh"
        older = SCRIPT.read_text().replace("(strip|http|api)", "(strip)")  # before http
        self.assertNotEqual(older, SCRIPT.read_text())
        copy.write_text(older)
        command = re.search(r"^ExecStart=(.*)$", self.unit("service"), re.M).group(1).split()
        self.routes.write_text(ROUTES.replace("/dev/       8110", "/dev/       8110   http"))
        result = self.run_argv(command)
        self.assertNotEqual(result.returncode, 0, result.stdout)  # the older copy refuses the table: no renewal
        self.assertIn("OPTION must be", result.stderr)
        result = self.ok("apply")
        self.assertIn(f"Updated the copy of this script that the renewal timer runs: {copy}", result.stdout)
        self.assertEqual(copy.read_bytes(), SCRIPT.read_bytes())
        result = self.run_argv(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("it is renewed when fewer than 30 days are left", result.stdout)
        self.assertNotIn("Updated the copy", self.ok("apply").stdout)  # the same: nothing to update

    def test_the_options_are_checked_before_anything_changes(self):
        cases = [(["--server", "http://acme.example/directory"], "--server must be an https:// address"),
                 (["--acme-dns", "https://acme dns.example"], "--acme-dns must be an https:// or http:// address"),
                 (["--acme-dns", "ftp://acmedns.example"], "--acme-dns must be an https:// or http:// address"),
                 (["--acme-ca", self.routes], "holds no PEM certificate"),
                 (["--dns-resolvers", "1.1.1.1;reboot"], "--dns-resolvers: 1.1.1.1;reboot is not HOST or HOST:PORT"),
                 (["--dns-resolvers", "1.1.1.1:70000"], "--dns-resolvers: 1.1.1.1:70000 has no valid port"),
                 (["--email", "ops"], "--email: ops is not an e-mail address"),
                 (["--extra-root", self.tmp / "missing.pem"], "No such file"),
                 (["--extra-root", self.ca.d / "int.pem"], "is not one root certificate"),
                 (["--renew"], "No certificate from Let's Encrypt is installed here yet: run sudo bash https.sh letsencrypt first"),
                 (["--accept-tos", "--server", "https://acme-staging-v02.api.letsencrypt.org/directory"],
                  "Certificates from the Let's Encrypt staging server lead to its test roots, which no system trusts"),
                 ([], f"The terms of service of {LE_SERVER} apply to the account this server gets there: {TERMS}. "
                      "Read them; to accept them, run the same command with --accept-tos")]
        for args, message in cases:
            with self.subTest(args=args):
                self.assertIn(message, self.refused("letsencrypt", *args))
                self.assertEqual(self.files(), ["routes.conf"])
                self.assertEqual(self.lego_runs(), [])
        for args in (["csr", "--email", "ops@example.com"], ["apply", "--renew"], ["status", "--accept-tos"],
                     ["uninstall", "--confirm", "yes", "--server", LE_SERVER]):
            with self.subTest(args=args):
                self.assertIn("belong to letsencrypt", self.refused(*args))

    def test_letsencrypt_says_what_the_server_lacks(self):
        cases = [({"docker": {"running": False, "image": True, "pull": True}},
                  "Docker does not run (docker info fails): sudo systemctl enable --now docker"),
                 ({"unreachable": [LE_SERVER]}, f"Could not reach the ACME server {LE_SERVER} (curl: (28) Connection timed "
                                                "out after 20002 milliseconds): this server needs outbound HTTPS to it"),
                 ({"directory": {"hello": "world"}}, f"{LE_SERVER} is not an ACME directory"),
                 ({"unreachable": [ACME_DNS]}, f"Could not reach the acme-dns server {ACME_DNS} (curl: (28)"),
                 ({"acme_dns_code": "502"}, f"The acme-dns server {ACME_DNS} answers HTTP 502"),
                 ({"docker": {"running": True, "image": False, "pull": False}},
                  f"Could not pull {LEGO_IMAGE} (above): this server needs outbound HTTPS to Docker Hub")]
        for state, message in cases:
            with self.subTest(message=message):
                self.set_state(**state)
                self.assertIn(message, self.refused("letsencrypt", "--accept-tos"))
                self.assertEqual(self.files(), ["routes.conf"])
                self.assertEqual(self.lego_runs(), [])
        self.set_state(register_code="500")  # acme-dns does not make the accounts
        self.assertIn(f"The acme-dns server {ACME_DNS} did not make an account for {HOSTS[0]} (HTTP 500: ",
                      self.refused("letsencrypt", "--accept-tos"))
        self.assertFalse((self.le_dir / "acme-dns.json").exists())
        self.assertEqual(self.lego_runs(), [])
        self.set_state(docker={"running": True, "image": False, "pull": True})  # pulled at once, run once the records exist
        self.assertEqual(self.le("--accept-tos").returncode, 3)
        self.assertIn(["docker", "pull", "-q", LEGO_IMAGE], self.calls())
        self.assertEqual(self.lego_runs(), [])
        path = f"{os.environ['PATH']}:/usr/local/sbin:/usr/sbin:/sbin"
        if shutil.which("dig", path=path) is None:  # the records are looked up before lego runs
            (self.bin_dir / "dig").unlink()
            self.assertIn("letsencrypt looks the DNS records up with dig, which is not installed: sudo apt-get install bind9-dnsutils",
                          self.refused("letsencrypt", "--accept-tos"))
            self.assertIn("(not looked up: dig is not installed", self.ok("status").stdout)
        if shutil.which("docker", path=path) is None:
            (self.bin_dir / "docker").unlink()
            self.assertIn("letsencrypt runs lego in Docker, which is not installed: sudo bash install-dependencies.sh --role app",
                          self.refused("letsencrypt", "--accept-tos"))

    def test_the_records_are_checked_with_the_dns_servers_given(self):
        self.ready()  # the records in this server's DNS
        records, resolvers = self.records(), "1.1.1.1:53,[2001:db8::53]:5353"
        self.set_state(dns={"": records, "1.1.1.1": {}})  # 1.1.1.1 answers without them
        result = self.le(*self.root(), "--dns-resolvers", "1.1.1.1,[2001:db8::53]:5353")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("What 1.1.1.1:53 answers now:", result.stdout)  # the one that answered for every record
        digs = [call for call in self.calls() if call[0] == "dig"]
        self.assertTrue(digs)
        self.assertTrue(all(given(call, "@1.1.1.1", "-p", "53") for call in digs))
        a, b = (f"_acme-challenge.{host}" for host in HOSTS)
        both = f"{a}, {b}"
        # None of them answers for every record: said plainly, per DNS server, and lego is not run. With
        # 1.1.1.1 answering only one record and the second server only the other, neither can check both.
        for dns, failures in (({}, f"1.1.1.1:53 did not answer for {both}; [2001:db8::53]:5353 did not answer for {both}"),
                              ({"1.1.1.1": {b: "SERVFAIL"}, "2001:db8::53": {a: "SERVFAIL"}},
                               f"1.1.1.1:53 did not answer for {b}; [2001:db8::53]:5353 did not answer for {a}")):
            with self.subTest(dns=dns):
                self.set_state(dns=dns)
                stderr = self.refused("letsencrypt", *self.root(), "--dns-resolvers", "1.1.1.1,[2001:db8::53]:5353")
                self.assertIn("No DNS server answered for every record, and lego checks each record on each DNS server "
                              f"it is given: {failures}. Check these DNS servers", stderr)
                self.assertEqual(self.lego_runs(), [])
        # 1.1.1.1 answers one record and fails the other; the second server answers both: lego gets that one only
        self.set_state(dns={"1.1.1.1": {a: records[a], b: "SERVFAIL"}, "2001:db8::53": records})
        self.le_ok(*self.root(), "--dns-resolvers", "1.1.1.1,[2001:db8::53]:5353")
        [run] = self.lego_runs()
        self.assertTrue(given(run, "--dns.resolvers", "[2001:db8::53]:5353"))
        self.set_state(dns={"2001:db8::53": records})  # 1.1.1.1 does not answer, the next one does
        self.le_ok(*self.root(), "--dns-resolvers", "1.1.1.1,[2001:db8::53]:5353", "--email", "ops@perodua.com.my")
        [run] = self.lego_runs()
        self.assertTrue(given(run, "--dns.resolvers", "[2001:db8::53]:5353"))  # lego asks only the one that answered
        self.assertTrue(given(run, "--email", "ops@perodua.com.my"))
        self.assertTrue(any(given(call, "@2001:db8::53", "-p", "5353") for call in self.calls() if call[0] == "dig"))
        settings = (self.le_dir / "settings").read_text()
        self.assertIn(f"\nDNS_RESOLVERS={resolvers}\nEMAIL=ops@perodua.com.my\n", settings)  # the whole list is kept
        self.assertIn(f"DNS records: as {resolvers} answers", self.ok("status").stdout)
        # Both work: the first answers, the second is not asked, and lego, which queries each of its
        # resolvers and fails on one that does not answer, gets only the first.
        self.set_state(dns={"1.1.1.1": records, "2001:db8::53": records})
        self.le_ok()
        [run] = self.lego_runs()
        self.assertTrue(given(run, "--dns.resolvers", "1.1.1.1:53"))
        self.assertFalse(any("@2001:db8::53" in call for call in self.calls() if call[0] == "dig"))
        self.set_state(dns={"": records})
        self.le_ok("--dns-resolvers", "")  # back to this server's DNS; the e-mail address is kept
        [run] = self.lego_runs()
        self.assertNotIn("--dns.resolvers", run)
        self.assertTrue(given(run, "--email", "ops@perodua.com.my"))

    def test_the_extra_root_belongs_to_the_acme_server_it_was_given_with(self):
        self.le_installed()
        before = (self.dir / "fullchain.pem").read_bytes()
        other = "https://acme.example.test/directory"
        self.set_state(dns={"": self.records()})
        self.assertIn(f"The terms of service of {other} apply", self.refused("letsencrypt", "--server", other))
        result = self.le("--server", other, "--accept-tos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"The extra root kept for {LE_SERVER} is not used for {other}.", result.stderr)
        self.assertIn("The certificate does not lead to a root this server trusts", result.stderr)
        self.assertEqual((self.dir / "fullchain.pem").read_bytes(), before)
        settings = (self.le_dir / "settings").read_text()  # the timer goes on as before
        self.assertIn(f"SERVER={LE_SERVER}\n", settings)
        self.assertIn(f"EXTRA_ROOT_FOR={LE_SERVER}\n", settings)
        self.le_ok("--server", other, *self.root())
        settings = (self.le_dir / "settings").read_text()
        self.assertIn(f"SERVER={other}\n", settings)
        self.assertIn(f"TOS_ACCEPTED={LE_SERVER} {other}\n", settings)

    def test_the_hooks_for_a_local_test_against_pebble(self):
        pebble, acme_dns, network = "https://pebble:14000/dir", "http://acmedns:80", "perodua_le-test.1"
        acme_ca = self.tmp / "pebble.minica.pem"  # stands in for the CA of Pebble's own HTTPS certificate
        shutil.copy(self.ca.d / "root.pem", acme_ca)
        self.env["PERODUA_HTTPS_LEGO_NETWORK"] = network
        args = ["--server", pebble, "--acme-ca", acme_ca, "--acme-dns", acme_dns, "--dns-resolvers", "10.253.50.11:53", *self.root()]
        result = self.le(*args)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(f"--acme-dns {acme_dns} sends the acme-dns passwords unencrypted", result.stderr)
        [directory] = [call for call in self.calls() if call[0] == "curl" and call[-1] == pebble]
        self.assertTrue(given(directory, "--cacert", acme_ca))
        # --acme-ca is for the ACME server only: acme-dns is reached with this server's trust
        self.assertEqual([call[-1] for call in self.registrations()], [f"{acme_dns}/register"] * 2)
        self.assertFalse(any("--cacert" in call for call in self.registrations()))
        self.assertEqual({account["server_url"] for account in self.accounts().values()}, {acme_dns})
        self.assertEqual(self.lego_runs(), [])
        self.set_state(dns={"10.253.50.11": self.records()})
        self.le_ok(*args)
        [run] = self.lego_runs()
        for words in (["--network", network], ["-e", "LEGO_CA_CERTIFICATES=/data/acme-ca.pem"],
                      ["-e", f"ACME_DNS_API_BASE={acme_dns}"], ["--server", pebble], ["--dns.resolvers", "10.253.50.11:53"]):
            self.assertTrue(given(run, *words), words)
        # every file the container sees is under --dir: the CA given from elsewhere, copied there for the run
        self.assert_lego_sees_only_its_files(run, ca="acme-ca.run.pem")
        self.assertFalse((self.le_dir / "acme-ca.run.pem").exists())
        settings = (self.le_dir / "settings").read_text()
        self.assertIn(f"SERVER={pebble}\nACME_DNS={acme_dns}\nDNS_RESOLVERS=10.253.50.11:53\n", settings)
        self.assertIn(f"ACME_CA_FOR={pebble}\n", settings)
        self.assertEqual((self.le_dir / "acme-ca.pem").read_bytes(), acme_ca.read_bytes())
        self.le_ok()  # kept, and read from the state directory
        [run] = self.lego_runs()
        self.assertTrue(given(run, "-e", "LEGO_CA_CERTIFICATES=/data/acme-ca.pem"))
        self.assert_lego_sees_only_its_files(run, ca="acme-ca.pem")
        self.assertTrue(given(run, "--network", network))
        self.env["PERODUA_HTTPS_LEGO_NETWORK"] = "perodua net"
        self.assertIn("PERODUA_HTTPS_LEGO_NETWORK must be a Docker network name, not perodua net", self.refused("letsencrypt"))
        del self.env["PERODUA_HTTPS_LEGO_NETWORK"]
        self.le_ok()
        [run] = self.lego_runs()
        self.assertTrue(given(run, "--network", "host"))  # the default: lego uses this server's network and DNS

    def test_accounts_of_another_acme_dns_server_are_not_used(self):
        self.ready()
        stderr = self.refused("letsencrypt", *self.root(), "--acme-dns", "https://acmedns.example.org/")
        self.assertIn(f"The acme-dns accounts in {self.le_dir}/acme-dns.json were made on {ACME_DNS}, "
                      "not https://acmedns.example.org. To move", stderr)
        self.assertEqual(self.lego_runs(), [])

    def test_a_lego_failure_shows_lego_s_own_error(self):
        self.ready()
        error = "acme: error: 403 :: urn:ietf:params:acme:error:unauthorized :: No TXT record found at _acme-challenge.stgissrp.perodua.com.my"
        self.set_state(dns={"": self.records()}, lego={"days": 90, "error": error})
        result = self.le(*self.root())
        self.assertEqual(result.returncode, 1)
        self.assertIn(error, result.stderr)
        self.assertIn("Let's Encrypt did not issue the certificate (lego's messages above)", result.stderr)
        self.assertIn(f"  _acme-challenge.{HOSTS[1]}: found\n", result.stdout)
        self.assertFalse((self.dir / "fullchain.pem").exists())

    def test_moving_to_another_acme_dns_server_makes_the_new_accounts_first(self):
        self.le_installed()
        old = self.records()
        new_server = "https://acmedns.example.org"
        (self.le_dir / "acme-dns.json").unlink()  # as the refusal above says to do
        # Let's Encrypt checked these host names a short while ago: lego would solve no challenge, so it would
        # make no acme-dns account. The script makes them before lego runs, and stops for the new records.
        self.set_state(dns={"": old}, lego={"days": 90, "reuse": True})
        result = self.le("--acme-dns", new_server)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.lego_runs(), [])
        self.assertEqual([call[-1] for call in self.registrations()], [f"{new_server}/register"] * 2)
        new = self.records()
        self.assertEqual({account["server_url"] for account in self.accounts().values()}, {new_server})
        for name, target in new.items():
            self.assertNotEqual(target, old[name])
            self.assertIn(f"\n{name} CNAME {target}\n", result.stdout)
        self.set_state(dns={"": old}, lego={"days": 90, "reuse": True})  # the old records are not the new ones
        result = self.le("--acme-dns", new_server)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("WRONG: it points to", result.stdout)
        self.assertEqual(self.lego_runs(), [])
        self.set_state(dns={"": new}, lego={"days": 90, "reuse": True})
        self.le_ok("--acme-dns", new_server)
        self.assertEqual(len(self.lego_runs()), 1)
        self.assertIn(f"ACME_DNS={new_server}\n", (self.le_dir / "settings").read_text())
        self.assertEqual(self.records(), new)

    def test_an_acme_dns_file_from_elsewhere_is_used_as_it_is(self):
        # as the acme-dns operator could hand it over: spread over lines, fields in another order, an extra field
        accounts = {host: {"username": f"user-{i}", "password": f"secret_{i}-X", "allowfrom": ["192.168.23.13/32"],
                           "subdomain": f"sub-{i}", "fulldomain": f"sub-{i}.acme.novutal.com", "server_url": f"{ACME_DNS}/"}
                    for i, host in enumerate(HOSTS)}
        self.le_dir.mkdir(mode=0o700)
        seeded = self.le_dir / "acme-dns.json"
        seeded.write_text(json.dumps(accounts, indent=4) + "\n")
        seeded.chmod(0o644)
        before = seeded.read_bytes()
        self.set_state(dns={"": {f"_acme-challenge.{host}": f"sub-{i}.acme.novutal.com" for i, host in enumerate(HOSTS)}})
        self.le_ok(*self.root())
        self.assertEqual(self.registrations(), [])
        [run] = self.lego_runs()
        self.assert_lego_sees_only_its_files(run)  # the operator's file is not among lego's mounts
        self.assertEqual(seeded.read_bytes(), before)  # used as it is; only no longer readable by others
        self.assertEqual(seeded.stat().st_mode & 0o777, 0o600)
        # lego got the script's own copy of the accounts it uses, one line in goacmedns' form, not this file
        path, content, mode = self.lego_storage()
        self.assertEqual((path, mode), ("/data/acme-dns.lego.json", "0o600"))
        fields = ("fulldomain", "subdomain", "username", "password", "server_url")
        self.assertEqual(content, json.dumps({host: {field: accounts[host][field] for field in fields}
                                              for host in sorted(HOSTS)}, separators=(",", ":")))
        self.assertEqual(sorted(p.name for p in self.le_dir.iterdir() if "acme-dns" in p.name), ["acme-dns.json"])
        good = json.dumps(accounts)
        one, two = (json.dumps(accounts[host]) for host in HOSTS)
        cases = [  # what goacmedns cannot read, and would start again from no accounts: refused, the file left as it is
            (good[:-1] + ",}", "not a file of acme-dns accounts"),  # a trailing comma
            (good.replace('"}', '",}', 1), "not a file of acme-dns accounts"),  # a trailing comma in an account
            (good + "\n{}", "not a file of acme-dns accounts"),  # something after the object
            (good + " x", "not a file of acme-dns accounts"),
            # a host name twice, the second time with no field that could repeat one of the first
            (f'{{"{HOSTS[0]}": {one}, "{HOSTS[1]}": {two}, "{HOSTS[0]}": {{}}}}', "not a file of acme-dns accounts"),
            (good.replace('"password":', '"password": "twice", "password":', 1), "not a file of acme-dns accounts"),
            ('{"a": [}', "not a file of acme-dns accounts"),
            (json.dumps({HOSTS[0]: dict(accounts[HOSTS[0]], password="a b"), HOSTS[1]: accounts[HOSTS[1]]}),
             f"The acme-dns account of {HOSTS[0]} in {seeded} has no usable password"),
            (json.dumps({HOSTS[0]: {}, HOSTS[1]: accounts[HOSTS[1]]}),  # an empty account is not made again
             f"The acme-dns account of {HOSTS[0]} in {seeded} has no usable fulldomain"),
            # no server_url (an older tool), or an empty one: which acme-dns server the account is on is not known
            (json.dumps({host: {k: v for k, v in accounts[host].items() if k != "server_url"} for host in HOSTS}),
             f'does not say which acme-dns server it is on (server_url). If the file came from an older tool, '
             f'add "server_url":"{ACME_DNS}" to each account in it. The file was left as it is'),
            (json.dumps({host: dict(accounts[host], server_url="") for host in HOSTS}), "does not say which acme-dns server")]
        for text, message in cases:
            with self.subTest(text=text[:70]):
                seeded.write_text(text)
                self.assertIn(message, self.refused("letsencrypt"))
                self.assertEqual(seeded.read_text(), text)
                self.assertEqual(self.registrations(), [])
                self.assertEqual(self.lego_runs(), [])

    def test_lego_never_writes_the_accounts_file(self):
        self.ready()
        before = (self.le_dir / "acme-dns.json").read_bytes()
        fullchain = (self.dir / "fullchain.pem").exists()
        for mode, message in (("fail", "lego made or changed an acme-dns account in the copy of the accounts it was given, "
                                       "which it does when one does not work for it"),
                              ("issue", "lego made or changed an acme-dns account in the copy of the accounts it was given "
                                        "(its messages above). The new certificate was not installed")):
            with self.subTest(mode=mode):
                self.set_state(dns={"": self.records()}, lego={"days": 90, "rewrite": mode})
                stderr = self.refused("letsencrypt", *self.root())
                self.assertIn(message, stderr)
                self.assertIn(f"{self.le_dir}/acme-dns.json was left as it is", stderr)
                self.assertEqual((self.le_dir / "acme-dns.json").read_bytes(), before)
                self.assertFalse((self.le_dir / "acme-dns.lego.json").exists())
                self.assertEqual((self.dir / "fullchain.pem").exists(), fullchain)

    def test_status_shows_lets_encrypt(self):
        self.assertIn("Let's Encrypt: not used (letsencrypt gets the certificate from it)\n", self.ok("status").stdout)
        self.ready()
        out = self.ok("status").stdout
        self.assertIn(f"Let's Encrypt: {LE_SERVER}, acme-dns {ACME_DNS}\n", out)
        self.assertIn("no certificate from it installed yet", out)
        self.assertIn("renewal timer: none yet (the first certificate from letsencrypt sets it up)", out)
        for name, target in self.records().items():
            self.assertIn(f"  {name} CNAME {target}  (found)\n", out)
        self.le_ok(*self.root())
        self.set_state(dns={"": self.records()}, renewal_result="exit-code")
        out = self.ok("status").stdout
        self.assertRegex(out, r"enabled: the installed certificate is from it, 8\d days left; renewed when fewer than 30 are left")
        self.assertIn(f"renewal timer: active, runs {self.lib}/https.sh; the last run failed (exit-code): "
                      f"sudo journalctl -u {UNIT}.service", out)
        self.ok("install-cert", self.signed(), self.ca.d / "int.pem")
        self.assertIn("the installed certificate is another one (install-cert)", self.ok("status").stdout)

    def test_uninstall_removes_the_timer_and_purge_the_lets_encrypt_files(self):
        self.le_installed()
        other = self.tmp / "other"  # one setup per server: another --dir leaves the timer alone
        other.mkdir()
        (other / "routes.conf").write_text(ROUTES)
        self.assertIn(f"The renewal timer ({self.units}/{UNIT}.service) works on {self.dir}: one Let's Encrypt setup per server",
                      self.refused("letsencrypt", "--accept-tos", directory=other))
        result = self.run_script("uninstall", "--confirm", "yes", directory=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"The Let's Encrypt renewal timer works on {self.dir}: it is left alone.", result.stdout)
        self.assertTrue((self.units / f"{UNIT}.timer").exists())
        self.ok("apply")
        result = self.ok("uninstall", "--confirm", "yes")
        self.assertIn("removes the Let's Encrypt renewal timer", result.stdout)
        self.assertEqual(sorted(p.name for p in self.units.iterdir()), [])
        self.assertFalse(self.lib.exists())
        self.assertIn(["systemctl", "disable", "--now", "--quiet", f"{UNIT}.timer"], self.calls())
        self.assertIn(["systemctl", "daemon-reload"], self.calls())
        self.assertTrue((self.le_dir / "acme-dns.json").exists())  # the accounts stay: their records go on working
        (self.dir / "notes.txt").write_text("the administrator's")
        result = self.ok("uninstall", "--purge", "--confirm", "yes")
        self.assertIn("and the Let's Encrypt accounts and certificates", result.stdout)
        self.assertEqual(self.files(), ["notes.txt"])


LISTENING = "".join(f"LISTEN 0 511 {address} 0.0.0.0:*\n" for address in (
    "0.0.0.0:22", "[::]:22", "0.0.0.0:80", "[::]:80", "0.0.0.0:443", "127.0.0.1:8000", "127.0.0.1:8001", "127.0.0.1:8110",
    "127.0.0.1:8111", "127.0.0.1:5432", "127.0.1.1:5432", "127.0.0.53%lo:53"))
UBUNTU_DEFAULT = {"/etc/nginx/sites-enabled/default": "server {\n\tlisten 80 default_server;\n\tlisten [::]:80 default_server;\n"
                                                      "\t# listen 443 ssl default_server;\n\troot /var/www/html;\n"
                                                      "\tserver_name _;\n}"}
EDGE = '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"'
CHROME = '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"'
MYT = datetime.timezone(datetime.timedelta(hours=8))
NOW = int(datetime.datetime(2026, 10, 8, 14, 30, tzinfo=MYT).timestamp())
NOVEMBER = int(datetime.datetime(2026, 11, 1, 0, 10, tzinfo=MYT).timestamp())
SBIN = ("/usr/local/sbin", "/usr/sbin", "/sbin")
BROWSER_LOG = "".join(line + "\n" for line in [
    f'10.1.2.3 - - [08/Oct/2026:09:00:00 +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.1.2.3 - - [08/Oct/2026:09:00:00 +0800] "GET /dev/ HTTP/1.1" 303 0 "-" {EDGE}',
    f'10.1.2.3 - - [08/Oct/2026:09:00:01 +0800] "GET /dev/web/login HTTP/1.1" 200 5120 "-" {EDGE}',
    f'10.1.2.3 - - [08/Oct/2026:09:00:20 +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.1.2.3 - - [08/Oct/2026:09:00:40 +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.1.2.3 - - [08/Oct/2026:09:00:41 +0800] "GET /uat HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.1.2.3 - - [08/Oct/2026:09:00:41 +0800] "GET /dev/api HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.4.5.6 - - [08/Oct/2026:09:01:00 +0800] "GET /dev/api HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.4.5.6 - - [08/Oct/2026:09:01:02 +0800] "GET /dev/api HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.7.8.9 - - [08/Oct/2026:09:02:00 +0800] "GET /uat HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.7.8.10 - - [08/Oct/2026:09:02:01 +0800] "GET /uat HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.7.8.11 - - [08/Oct/2026:09:02:02 +0800] "GET /uat HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.9.9.9 - - [08/Oct/2026:09:03:00 +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.9.9.9 - - [09/Oct/2026:09:03:01 +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}',
    f'10.9.9.9 - - [09/Oct/2026:09:03:02 +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}',
    *[f'10.20.30.40 - - [08/Oct/2026:09:05:0{n} +0800] "GET /dev HTTP/1.1" 301 178 "-" {EDGE}' for n in range(4)],
    *[f'10.20.30.40 - - [08/Oct/2026:09:06:0{n} +0800] "GET /dev/app/ HTTP/1.1" 301 178 "-" {EDGE if n < 3 else CHROME}'
      for n in range(5)],
    '127.0.0.1 - - [09/Oct/2026:10:00:00 +0800] "GET /dev/ HTTP/1.1" 301 178 "-" "perodua-https-diagnose"',
    '127.0.0.1 - - [09/Oct/2026:10:00:00 +0800] "GET /dev/ HTTP/1.1" 301 178 "-" "perodua-https-diagnose"',
    '127.0.0.1 - - [09/Oct/2026:10:00:01 +0800] "GET /dev/ HTTP/1.1" 301 178 "-" "perodua-https-diagnose"',
    'a line in another format',
    '10.1.2.3 - - [99/Foo/2026:09:00:00 +0800] "GET /dev HTTP/1.1" 301 178 "-" "-"',
    '10.1.2.3 - - [08/Oct/2026:09:00:00 +0800] "GET /dev HTTP/1.1" 3o1 178 "-" "-"'])
WAF_LOG = "".join(line + "\n" for line in [
    *[f'202.165.23.137 - - [08/Oct/2026:14:20:{second} +0800] "GET /dev/uiux/api/handover/start?next=%2Fapp%2F HTTP/1.1" '
      f'301 178 "https://stgissrp.perodua.com.my/" {EDGE}' for second in (33, 33, 34, 34, 36)],
    *['202.165.23.17 - - [08/Oct/2026:14:24:21 +0800] "GET /dev/app/ HTTP/1.1" 301 178 "-" "curl/8.13.0"'] * 5])
MONTH_END_LOG = "".join(f'202.165.23.66 - - [{when} +0800] "GET /dev/web/login HTTP/1.1" 301 178 "-" {EDGE}\n'
                        for when in ("31/Oct/2026:23:59:57", "31/Oct/2026:23:59:58", "31/Oct/2026:23:59:59",
                                     "01/Nov/2026:00:00:00", "01/Nov/2026:00:00:01"))
LOOP = ("sends HTTPS requests to port 80 of this server, where this route redirects to https://: a redirect loop. Ask the "
        "front's owner to forward HTTPS to port 443 with the original Host header, or add http to this route (README: Behind "
        "a TLS front on port 80)\n")
REMEDY = (". Ask the front's owner to forward HTTPS to port 443 with the original Host header, or add http to the route of this "
          "request (README: Behind a TLS front on port 80)")
NOT_HTTP = ": without http on the route, port 80 must only send browsers to HTTPS (apply)\n"
FIRST = {HOSTS[0]: "/dev/api/", HOSTS[1]: "/dev/"}
HTTP_ROUTES = """api.example.perodua.com.my /dev/api/ 8000 strip,http
api.example.perodua.com.my /uat/api/ 8001 http,strip
stgissrp.perodua.com.my /dev/ 8110 http
stgissrp.perodua.com.my /uat/ 8111 http
"""


def answers(sender, request, *times, agent='"curl/8.13.0"'):
    return "".join(f'{sender} - - [08/Oct/2026:{when}] "{request}" 301 178 "-" {agent}\n' for when in times)


def loop_lines(out):
    return [line for line in out.splitlines() if "redirect loop" in line]


def served(host, path, code="200"):
    return (f"  PROBLEM {host} {path}: HTTP on this server answers {code} as HTTPS does, but the route has no http: another "
            f"nginx file serves {host} on port 80 (see nginx files), or http was removed after the last apply. If a TLS front "
            "connects on port 80 for this route, first add http to the route (README: Behind a TLS front on port 80), else "
            "apply makes port 80 redirect it and the front loops. Then remove the other nginx file, if there is one, and run "
            "apply\n")


def other_file(path, names):
    return (f"  NOTE {path}, a file that apply did not write, serves host names of the table: {names}. apply refuses a host "
            "name that another nginx file serves, and for one host name and port nginx uses the file that it reads first. To "
            "serve them with the routes of the table, first add http to each route that a TLS front reaches on port 80 "
            f"(README: Behind a TLS front on port 80), then remove {path} and run apply\n")


@needs_root
class DiagnoseTest(Base):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.access, self.errors = self.tmp / "access.log", self.tmp / "error.log"
        self.access.write_text(BROWSER_LOG)
        self.errors.write_text("2026/10/08 14:00:00 [error] 1234#1234: *1 connect() failed (111: Connection refused) while "
                               "connecting to upstream, client: 10.1.2.3, server: stgissrp.perodua.com.my\n")

    def set_state(self, **changes):
        super().set_state(**dict({"now": getattr(self, "now", None)}, **changes))

    def diagnose(self, *args, access=None, errors=None):
        return self.run_script("diagnose", "--access-log", access or self.access, "--error-log", errors or self.errors, *args)

    def healthy(self, **state):
        self.installed()
        self.ok("apply")
        self.set_state(**dict({"ss": LISTENING, "others": UBUNTU_DEFAULT}, **state))

    def expect(self, result, status):
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_a_healthy_server_with_browser_redirects_has_no_problem(self):
        self.healthy()
        out = self.expect(self.diagnose(), 0)
        self.assertEqual([line for line in out.splitlines() if not line.startswith("  ")],
                         ["Ports:", "nginx files:", "Host names:", "Access log:", "nginx workers:", "Result: no problem found"])
        for line in out.splitlines():
            if line.startswith("  "):
                self.assertRegex(line, r"^  (OK|NOTE) \S")
        self.assertIn("  OK port 22 (SSH): listening on all networks (0.0.0.0:22, [::]:22)\n", out)
        self.assertIn("  OK port 80 (HTTP): listening on all networks (0.0.0.0:80, [::]:80)\n", out)
        self.assertIn("  OK port 443 (HTTPS): listening on all networks (0.0.0.0:443)\n", out)
        self.assertIn("  OK port 8110 (https://stgissrp.perodua.com.my/dev/): on this server only (127.0.0.1:8110)\n", out)
        self.assertIn("  OK port 8000 (https://api.example.perodua.com.my/dev/api/): on this server only (127.0.0.1:8000)\n", out)
        self.assertEqual(out[:out.index("nginx files:")].count("  OK port "), 7)
        self.assertIn(f"  OK port 80: /etc/nginx/sites-enabled/default, {self.conf}\n", out)
        self.assertIn(f"  OK port 443: {self.conf}\n", out)
        self.assertIn("  NOTE /etc/nginx/sites-enabled/default is the default server on port 80: it answers every other host "
                      "name and the bare address (Ubuntu's default site: the \"Welcome to nginx\" page); a front that changes "
                      "the Host header gets it, not the App\n", out)
        for host, path in (("api.example.perodua.com.my", "/dev/api/"), ("api.example.perodua.com.my", "/uat/api/"),
                           ("stgissrp.perodua.com.my", "/dev/"), ("stgissrp.perodua.com.my", "/uat/")):
            self.assertIn(f"  OK {host} {path}: HTTPS on this server answers 200\n", out)
            self.assertIn(f"  OK {host} {path}: HTTP on this server answers 301 to https://{host}{path}\n", out)
        for host in HOSTS:
            self.assertIn(f"  NOTE {host}: the system resolver gives no IPv4 address: the way through a front was not checked "
                          "(pass --front ADDRESS)\n", out)
        self.assertEqual(loop_lines(out), [f"  OK no redirect loop in {self.access} (30 lines, 22 answers 301, 3 lines in "
                                           "another format skipped, 3 requests of diagnose itself skipped)"])
        self.assertIn("  NOTE 10.20.30.40: 9 answers 301, the last to GET /dev/app/ HTTP/1.1 at 08/Oct/2026:09:06:04 +0800\n",
                      out)
        self.assertIn("  NOTE 10.1.2.3: 5 answers 301, the last to GET /dev/api HTTP/1.1 at 08/Oct/2026:09:00:41 +0800\n", out)
        self.assertIn("  NOTE 10.9.9.9: 3 answers 301, the last to GET /dev HTTP/1.1 at 09/Oct/2026:09:03:02 +0800\n", out)
        self.assertEqual(out.count(" answers 301, the last to "), 5)
        self.assertEqual(out.count(": 1 answers 301"), 1)
        self.assertNotIn("127.0.0.1:", out[out.index("Access log:"):])
        self.assertIn(f"  OK no nginx worker exited on a signal ({self.errors})\n", out)
        signal = "2026/10/07 17:40:02 [alert] 2120#2120: worker process 2125 exited on signal 9"
        self.errors.write_text(f"2026/10/01 10:00:00 [alert] 2120#2120: worker process 2121 exited on signal 11\n{signal}\n")
        out = self.expect(self.diagnose(), 0)
        self.assertIn(f"  NOTE 2 nginx worker exit(s) on a signal in {self.errors} (nginx starts a new worker each time); "
                      f"the last: {signal}\n", out)

    def test_a_front_that_sends_https_to_port_80_is_a_redirect_loop(self):
        self.healthy(web={"203.0.113.10:443": {"code": "301", "server": "CloudWAF", "location": "{url}"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        for host in HOSTS:
            self.assertIn(f"  PROBLEM {host} {FIRST[host]}: the front at 203.0.113.10 (Server: CloudWAF) {LOOP}", out)
        self.assertTrue(out.endswith("\nResult: 2 problem(s) found\n"), out)
        curls = [call for call in self.calls() if call[0] == "curl"]
        self.assertEqual(len(curls), 10)
        for call in curls:
            self.assertTrue(given(call, "--max-time", "10"), call)
        fronts = [call for call in curls if "--connect-to" in call]
        self.assertEqual([call[call.index("--connect-to") + 1] for call in fronts],
                         [f"{host}:443:203.0.113.10:443" for host in HOSTS])
        self.assertEqual([call[-1] for call in fronts], [f"https://{HOSTS[0]}/dev/api/", f"https://{HOSTS[1]}/dev/"])
        for call in fronts:
            self.assertTrue(given(call, "-L", "--max-redirs", "5"), call)
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT,
                       web={"203.0.113.10:8443": {"code": "301", "server": "CloudWAF", "location": "{path}"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10:8443"), 4)
        self.assertIn(f"  PROBLEM stgissrp.perodua.com.my /dev/: the front at 203.0.113.10:8443 (Server: CloudWAF) {LOOP}", out)
        self.assertIn(f"{HOSTS[1]}:443:203.0.113.10:8443", [call[call.index("--connect-to") + 1] for call in self.calls()
                                                            if "--connect-to" in call])
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT,
                       web={"203.0.113.10:443": {"code": "302", "server": "CloudWAF", "location": "{url}?again",
                                                 "endless": True}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        for host in HOSTS:
            self.assertIn(f"  PROBLEM {host} {FIRST[host]}: the front at 203.0.113.10 (Server: CloudWAF) {LOOP}", out)
        self.assertTrue(out.endswith("\nResult: 2 problem(s) found\n"), out)

    def test_a_front_that_forwards_to_443_is_ok(self):
        self.healthy(web={"203.0.113.10:443": {"code": "200", "server": "CloudWAF"}},
                     getent={HOSTS[0]: ["127.0.0.1"], HOSTS[1]: ["203.0.113.10", "203.0.113.11", "203.0.113.12"]})
        out = self.expect(self.diagnose(), 0)
        self.assertIn(f"  NOTE {HOSTS[0]}: the system resolver gives 127.0.0.1, an address of this server: no front to check "
                      "(pass --front ADDRESS to check one)\n", out)
        self.assertIn(f"  OK {HOSTS[1]} /dev/: through the front at 203.0.113.10 answers 200 (Server: CloudWAF)\n", out)
        self.assertIn(["getent", "ahostsv4", HOSTS[1]], self.calls())
        out = self.expect(self.diagnose("--front", "203.0.113.10:443"), 0)
        self.assertIn(f"  OK {HOSTS[0]} /dev/api/: through the front at 203.0.113.10:443 answers 200 (Server: CloudWAF)\n", out)
        self.assertNotIn("getent", [call[0] for call in self.calls()])
        out = self.expect(self.diagnose("--front", "203.0.113.99"), 0)
        self.assertIn(f"  NOTE {HOSTS[1]}: no answer through the front at 203.0.113.99 (curl: (7) Failed to connect to "
                      "203.0.113.99 port 443: Connection refused): this way was not checked from this server\n", out)
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT, web={"203.0.113.10:443": {"code": "403", "server": "CloudWAF"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        self.assertIn(f"  PROBLEM {HOSTS[1]}: the front at 203.0.113.10 answers 403 (Server: CloudWAF), this server answers "
                      f"200 over HTTPS: ask the front's owner to check its entry for {HOSTS[1]}\n", out)
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT, web={"127.0.0.1:443": {"code": "404", "server": "nginx"},
                                                                 "203.0.113.10:443": {"code": "404", "server": "CloudWAF"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 0)
        self.assertIn(f"  OK {HOSTS[1]} /dev/: through the front at 203.0.113.10 answers 404 (Server: CloudWAF), as this "
                      "server does\n", out)
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT, web={"127.0.0.1:443": {"code": "502", "server": "nginx"},
                                                                 "203.0.113.10:443": {"code": "502", "server": "CloudWAF"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        self.assertIn(f"  PROBLEM {HOSTS[1]}: the front at 203.0.113.10 answers 502 (Server: CloudWAF), this server answers "
                      f"502 over HTTPS: ask the front's owner to check its entry for {HOSTS[1]}\n", out)

    def test_the_access_log_finds_the_redirect_loop_of_a_waf(self):
        self.healthy()
        self.access.write_text(BROWSER_LOG + WAF_LOG)
        out = self.expect(self.diagnose(), 4)
        self.assertEqual(loop_lines(out), [
            "  PROBLEM redirect loop from 202.165.23.137 (5 answers, last: GET /dev/uiux/api/handover/start?next=%2Fapp%2F "
            "HTTP/1.1 at 08/Oct/2026:14:20:36 +0800): it forwards HTTPS requests to port 80 of this server" + REMEDY,
            "  PROBLEM redirect loop from 202.165.23.17 (5 answers, last: GET /dev/app/ HTTP/1.1 at 08/Oct/2026:14:24:21 "
            "+0800): it forwards HTTPS requests to port 80 of this server" + REMEDY])
        self.assertIn("  NOTE 202.165.23.17: 5 answers 301, the last to GET /dev/app/ HTTP/1.1 at 08/Oct/2026:14:24:21 +0800\n",
                      out)
        self.assertTrue(out.endswith("\nResult: 2 problem(s) found\n"), out)
        self.now = NOVEMBER
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT)
        self.access.write_text(WAF_LOG + MONTH_END_LOG)
        out = self.expect(self.diagnose(), 4)
        self.assertEqual(loop_lines(out), [
            "  PROBLEM redirect loop from 202.165.23.66 (5 answers, last: GET /dev/web/login HTTP/1.1 at 01/Nov/2026:00:00:01 "
            "+0800): it forwards HTTPS requests to port 80 of this server" + REMEDY,
            "  NOTE earlier redirect loop from 202.165.23.137 (5 answers, last: GET /dev/uiux/api/handover/start?next=%2Fapp%2F "
            "HTTP/1.1 at 08/Oct/2026:14:20:36 +0800, 33709 minutes before this run): it forwarded HTTPS requests to port 80 "
            "of this server then",
            "  NOTE earlier redirect loop from 202.165.23.17 (5 answers, last: GET /dev/app/ HTTP/1.1 at 08/Oct/2026:14:24:21 "
            "+0800, 33705 minutes before this run): it forwarded HTTPS requests to port 80 of this server then"])
        self.assertTrue(out.endswith("\nResult: 1 problem(s) found\n"), out)
        self.now = NOW
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT)
        browsers = "".join(f'10.{n // 65536 % 256}.{n // 256 % 256}.{n % 256} - - [08/Oct/2026:{n // 3600 % 24:02}:'
                           f'{n // 60 % 60:02}:{n % 60:02} +0800] "GET /dev{"" if n % 50 else "/web"} HTTP/1.1" '
                           f'{200 if n % 50 else 301} 178 "-" {EDGE}\n' for n in range(200000))
        self.access.write_text(browsers + WAF_LOG)
        out = self.expect(self.diagnose(), 4)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("  PROBLEM redirect loop")]), 2)
        self.assertEqual(out.count("answers 301, the last to"), 5)

    def test_the_redirect_loop_rule_of_the_access_log(self):
        self.healthy()

        def six_senders(when):
            return "".join(answers(f"10.0.0.{n}", "GET /dev/app/ HTTP/1.1", *[when] * (11 - n)) for n in range(1, 7))

        self.access.write_text(six_senders("14:25:00 +0800"))
        out = self.expect(self.diagnose(), 4)
        self.assertEqual(loop_lines(out), [f"  PROBLEM redirect loop from 10.0.0.{n} ({11 - n} answers, last: GET /dev/app/ "
                                           "HTTP/1.1 at 08/Oct/2026:14:25:00 +0800): it forwards HTTPS requests to port 80 "
                                           "of this server" + REMEDY for n in range(1, 6)]
                         + ["  NOTE 1 more senders with a redirect loop"])
        self.assertTrue(out.endswith("\nResult: 5 problem(s) found\n"), out)
        self.access.write_text(six_senders("10:00:00 +0800"))
        out = self.expect(self.diagnose(), 0)
        self.assertEqual(loop_lines(out), [f"  OK no redirect loop in the last 30 minutes of {self.access} (45 lines, 45 "
                                           "answers 301)"]
                         + [f"  NOTE earlier redirect loop from 10.0.0.{n} ({11 - n} answers, last: GET /dev/app/ HTTP/1.1 "
                            "at 08/Oct/2026:10:00:00 +0800, 270 minutes before this run): it forwarded HTTPS requests to port "
                            "80 of this server then" for n in range(1, 6)]
                         + ["  NOTE 1 more senders with an earlier redirect loop"])
        self.access.write_text(
            answers("10.2.2.2", "GET /dev/app/ HTTP/1.1", "14:10:01 +0800", "06:10:02 +0000", "14:10:03 +0800",
                    "06:10:04 +0000", "06:10:05 +0000")
            + answers("10.6.6.6", "GET /dev/app/ HTTP/1.1", "01:10:01 -0500", "06:10:02 +0000", "01:10:03 -0500",
                      "14:10:04 +0800", "06:10:05 +0000")
            + answers("10.3.3.3", "GET /dev/app/ HTTP/1.1", "14:05:00 +0800", "14:04:00 +0800", "14:03:00 +0800",
                      "14:02:00 +0800", "14:01:00 +0800")
            + answers("10.5.5.5", "GET /dev/app/ HTTP/1.1", "14:20:00 +0800", "14:20:05 +0800", "14:20:10 +0800",
                      "14:20:15 +0800", "14:20:20 +0800"))
        out = self.expect(self.diagnose(), 4)
        self.assertEqual(loop_lines(out), [f"  PROBLEM redirect loop from {sender} (5 answers, last: GET /dev/app/ HTTP/1.1 at "
                                           "08/Oct/2026:06:10:05 +0000): it forwards HTTPS requests to port 80 of this server"
                                           + REMEDY for sender in ("10.2.2.2", "10.6.6.6")])
        self.assertTrue(out.endswith("\nResult: 2 problem(s) found\n"), out)

    def test_missing_logs_and_tools_are_notes(self):
        self.healthy()
        out = self.expect(self.diagnose(access=self.tmp / "none.log", errors=self.tmp / "none-error.log"), 0)
        self.assertIn(f"Access log:\n  NOTE {self.tmp}/none.log does not exist: the access log was not checked "
                      "(--access-log FILE)\nnginx workers:\n", out)
        self.assertIn(f"nginx workers:\n  NOTE {self.tmp}/none-error.log does not exist: the nginx workers were not checked "
                      "(--error-log FILE)\nResult: no problem found\n", out)
        out = self.expect(self.diagnose(access=self.tmp, errors=self.tmp), 0)
        self.assertIn(f"  NOTE {self.tmp} cannot be read: the access log was not checked\n", out)
        self.assertIn(f"  NOTE {self.tmp} cannot be read: the nginx workers were not checked\n", out)
        self.access.write_text('{"time": "2026-10-08T14:24:21+08:00", "status": 301}\nanother line\n')
        out = self.expect(self.diagnose(), 0)
        self.assertIn(f"  NOTE none of the 2 lines of {self.access} is in nginx's combined format: the access log was not "
                      "checked\n", out)
        made = self.tmp / "nginx-logs" / "access.log"
        made.parent.mkdir()
        skipped = (f"nginx files:\n  NOTE the log file {made} does not exist, and nginx -T creates the log files that nginx "
                   "names: the nginx files were not checked (nginx creates its log files when it starts)\nHost names:\n")
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT, nginx_logs=[str(made), str(self.errors)])
        out = self.expect(self.diagnose(), 0)
        self.assertIn(skipped, out)
        self.assertIn(["nginx", "-V"], self.calls())
        self.assertNotIn(["nginx", "-T"], self.calls())
        self.assertFalse(made.exists())
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT)
        out = self.expect(self.diagnose(access=made), 0)
        self.assertIn(skipped, out)
        self.assertIn(f"Access log:\n  NOTE {made} does not exist: the access log was not checked (--access-log FILE)\n", out)
        self.assertNotIn(["nginx", "-T"], self.calls())
        self.assertFalse(made.exists())
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT)
        tools = self.tmp / "tools"
        tools.mkdir()
        replaced = ("curl", "getent", "ss", "nginx")
        for directory in ("/usr/local/bin", "/usr/bin", "/bin"):
            for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
                if name not in replaced and not os.path.lexists(tools / name):
                    (tools / name).symlink_to(Path(directory, name))
        in_sbin = [name for name in replaced if any(Path(directory, name).exists() for directory in SBIN)]
        for name in ("getent", "ss", "nginx"):
            if name not in in_sbin:
                (self.bin_dir / name).unlink()
        self.env["PATH"] = f"{self.bin_dir}:{tools}"
        out = self.expect(self.diagnose(), 0)
        if "ss" not in in_sbin:
            self.assertIn("Ports:\n  NOTE ss is not installed (apt-get install iproute2): the ports were not checked\n", out)
        if "nginx" not in in_sbin:
            self.assertIn("nginx files:\n  NOTE nginx is not installed (apply installs it): the nginx files were not "
                          "checked\n", out)
        if "getent" not in in_sbin:
            for host in HOSTS:
                self.assertIn(f"  NOTE {host}: getent is not installed: the way through a front was not checked "
                              "(pass --front ADDRESS)\n", out)
        if "curl" not in in_sbin:
            (self.bin_dir / "curl").unlink()
            out = self.expect(self.diagnose(), 0)
            self.assertIn("Host names:\n  NOTE curl is not installed (apt-get install curl): the host names were not "
                          "checked\nAccess log:\n", out)
        if in_sbin:
            self.skipTest(f"the test host has {', '.join(in_sbin)} in an sbin directory, which https.sh puts on its PATH: "
                          "the check without it did not run")

    def test_what_stops_the_host_names_is_a_problem(self):
        self.healthy()
        self.routes.write_text(ROUTES + "wom.example.perodua.com.my /dev/ -\n")
        ports = LISTENING.replace("0.0.0.0:443", "127.0.0.1:443").replace("127.0.0.1:8110", "0.0.0.0:8110")
        ports = "".join(line + "\n" for line in ports.splitlines() if ":22 " not in line and ":8111 " not in line)
        self.set_state(ss=ports, others=UBUNTU_DEFAULT,
                       web={"127.0.0.1:443 https://stgissrp.perodua.com.my/uat/": {"code": "502", "server": "nginx"},
                            "127.0.0.1:443 https://api.example.perodua.com.my/dev/api/": {"code": "401", "server": "nginx"},
                            "127.0.0.1:80": {"code": "200", "server": "nginx"},
                            "127.0.0.1:80 http://stgissrp.perodua.com.my/uat/": {"code": "301", "server": "nginx",
                                                                                "location": "{url}"},
                            "127.0.0.1:80 http://api.example.perodua.com.my/uat/api/": {
                                "code": "301", "server": "nginx", "location": "https://www.perodua.com.my/"}})
        out = self.expect(self.diagnose(), 4)
        self.assertIn("  NOTE port 22 (SSH): not listening\n", out)
        self.assertIn("  PROBLEM port 443 (HTTPS): on this server only (127.0.0.1:443): browsers and a front on the network "
                      "cannot reach HTTPS\n", out)
        self.assertIn("  NOTE port 8110 (https://stgissrp.perodua.com.my/dev/): listening on all networks (0.0.0.0:8110): it "
                      "answers plain HTTP from the network too; only nginx needs it, on 127.0.0.1\n", out)
        self.assertIn("  PROBLEM port 8111 (https://stgissrp.perodua.com.my/uat/): not listening: the service does not run, and "
                      "HTTPS answers 502 for its paths\n", out)
        self.assertIn("  PROBLEM stgissrp.perodua.com.my /uat/: HTTPS on this server answers 502: nginx gets no good answer "
                      "from 127.0.0.1:8111\n", out)
        self.assertIn(served("stgissrp.perodua.com.my", "/dev/"), out)
        self.assertIn("  PROBLEM api.example.perodua.com.my /dev/api/: HTTP on this server answers 200, not 301 to "
                      f"https://api.example.perodua.com.my/dev/api/{NOT_HTTP}", out)
        self.assertIn("  PROBLEM stgissrp.perodua.com.my /uat/: HTTP on this server answers 301 to "
                      f"http://stgissrp.perodua.com.my/uat/, not 301 to https://stgissrp.perodua.com.my/uat/{NOT_HTTP}", out)
        self.assertIn("  PROBLEM api.example.perodua.com.my /uat/api/: HTTP on this server answers 301 to "
                      f"https://www.perodua.com.my/, not 301 to https://api.example.perodua.com.my/uat/api/{NOT_HTTP}", out)
        self.assertIn("  NOTE wom.example.perodua.com.my /dev/: port - (not known yet): not checked\n", out)
        self.assertTrue(out.endswith("\nResult: 7 problem(s) found\n"), out)
        self.conf.unlink()
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT, web={"127.0.0.1:443": None})
        out = self.expect(self.diagnose(), 4)
        self.assertIn(f"  PROBLEM port 80: {self.conf} does not listen on it, only /etc/nginx/sites-enabled/default: run "
                      "apply\n", out)
        self.assertIn("  PROBLEM port 443: no nginx file listens on it: run apply\n", out)
        self.assertIn("  PROBLEM stgissrp.perodua.com.my /dev/: no HTTPS answer on this server (curl: (7) Failed to connect "
                      "to 127.0.0.1 port 443: Connection refused)\n", out)
        self.set_state(T_status=1)
        out = self.expect(self.diagnose(), 4)
        self.assertIn("nginx files:\n  PROBLEM nginx -T fails, so nginx cannot load a change: nginx: [emerg] fake failure\n"
                      "Host names:\n", out)

    def test_diagnose_changes_nothing(self):
        self.healthy(web={"203.0.113.10:443": {"code": "301", "server": "CloudWAF", "location": "{url}"}})
        self.access.write_text(WAF_LOG)

        def snapshot():
            return {path: (path.stat().st_mtime_ns, path.read_bytes()) for top in (self.tmp / "state", self.conf.parent,
                                                                                    self.access.parent / "ca")
                    for path in sorted(top.rglob("*")) if path.is_file()}

        before = snapshot()
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        self.assertEqual(snapshot(), before)
        self.assertEqual([call for call in self.calls() if call[0] in ("nginx", "systemctl")], [["nginx", "-V"], ["nginx", "-T"]])
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        (fresh / "routes.conf").write_text(ROUTES)
        result = self.run_script("diagnose", "--access-log", self.access, "--error-log", self.errors, directory=fresh)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertTrue(result.stdout.endswith("problem(s) found\n"), result.stdout)
        self.assertEqual(self.files(fresh), ["routes.conf"])
        empty = self.tmp / "empty"
        self.assertIn(f"No routes file {empty}/routes.conf: diagnose checks the host names in it", self.refused("diagnose",
                                                                                                                 directory=empty))
        self.assertFalse(empty.exists())

    def test_the_options_of_diagnose(self):
        self.assertIn("--front, --access-log and --error-log belong to diagnose", self.refused("status", "--front", "203.0.113.10"))
        self.assertIn("--front, --access-log and --error-log belong to diagnose", self.refused("apply", "--access-log", "/x"))
        self.assertIn("--front: 203.0.113.10:70000 has no valid port", self.refused("diagnose", "--front", "203.0.113.10:70000"))
        self.assertIn("--front must be ADDRESS or ADDRESS:PORT", self.refused("diagnose", "--front", "https://203.0.113.10"))
        self.assertIn("--front needs ADDRESS or ADDRESS:PORT", self.refused("diagnose", "--front", ""))
        for value in ("-x", "...", "waf.example.", "a b"):
            with self.subTest(front=value):
                self.assertIn("--front must be ADDRESS or ADDRESS:PORT", self.refused("diagnose", "--front", value))
        self.assertIn("Unknown option: --frontend", self.refused("diagnose", "--frontend", "203.0.113.10"))
        self.assertIn("Unexpected argument: now", self.refused("diagnose", "now"))
        self.assertIn("diagnose [--front ADDRESS[:PORT]] [--access-log FILE]", self.ok("--help").stdout)
        self.assertIn("--front, --access-log and --error-log belong to diagnose", self.refused("csr", "--error-log", "/x"))
        self.assertEqual(self.files(), ["routes.conf"])
        for option in ("strip", "http", "api", "strip,http", "http,api", "strip,http,api", "api,http,strip"):
            with self.subTest(option=option):
                self.routes.write_text(f"{HOSTS[0]} /dev/api/ 8000 {option}\n{HOSTS[1]} /dev/ 8110 http\n")
                result = self.diagnose("--front", "203.0.113.10")
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertTrue(result.stdout.endswith(" problem(s) found\n"), result.stdout)
        for option in ("strip, api", "api,api", "rewrite"):
            with self.subTest(option=option):
                self.routes.write_text(f"{HOSTS[0]} /dev/api/ 8000 {option}\n")
                self.assertIn(OPTION_MESSAGE, self.refused("diagnose"))
        self.assertEqual(self.files(), ["routes.conf"])

    def test_an_http_route_answers_on_port_80_as_on_443(self):
        api, rp = HOSTS
        self.routes.write_text(HTTP_ROUTES)
        login = {"code": "303", "server": "nginx", "location": f"https://{rp}/uat/web/login"}
        self.healthy(web={"127.0.0.1:80": {"code": "200", "server": "nginx/1.24.0 (Ubuntu)"},
                          f"127.0.0.1:443 https://{rp}/uat/": login, f"127.0.0.1:80 http://{rp}/uat/": login})
        self.assertIn(f"    listen 80;\n    server_name {api};\n", self.conf.read_text())
        out = self.expect(self.diagnose(), 0)
        for host, path in ((api, "/dev/api/"), (api, "/uat/api/"), (rp, "/dev/")):
            self.assertIn(f"  OK {host} {path}: HTTP on this server answers 200: served on port 80 too, for a TLS front (http)\n", out)
        self.assertIn(f"  OK {rp} /uat/: HTTPS on this server answers 303\n", out)
        self.assertIn(f"  OK {rp} /uat/: HTTP on this server answers 303: served on port 80 too, for a TLS front (http)\n", out)
        self.assertNotIn("answers 301", out[out.index("Host names:"):out.index("Access log:")])
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT,
                       web={"127.0.0.1:80": {"code": "200", "server": "nginx/1.24.0 (Ubuntu)"},
                            f"127.0.0.1:80 http://{rp}/dev/": {"code": "404", "server": "nginx"},
                            f"127.0.0.1:443 https://{rp}/uat/": login,
                            f"127.0.0.1:80 http://{rp}/uat/": {"code": "301", "server": "nginx", "location": f"https://{rp}/"}})
        out = self.expect(self.diagnose(), 4)
        self.assertIn(f"  PROBLEM {rp} /dev/: HTTP on this server answers 404, HTTPS answers 200: with http, port 80 must answer "
                      "as HTTPS does (apply)\n", out)
        self.assertIn(f"  PROBLEM {rp} /uat/: HTTP on this server answers 301 to https://{rp}/, HTTPS answers 303: with http, port "
                      "80 must answer as HTTPS does (apply)\n", out)
        self.assertTrue(out.endswith("\nResult: 2 problem(s) found\n"), out)

    def test_an_http_route_whose_port_80_still_redirects_is_a_problem(self):
        self.healthy(web={"203.0.113.10:443": {"code": "301", "server": "CloudWAF", "location": "{url}"}})
        self.routes.write_text(HTTP_ROUTES)
        out = self.expect(self.diagnose(), 4)
        for host, path in ((HOSTS[0], "/dev/api/"), (HOSTS[0], "/uat/api/"), (HOSTS[1], "/dev/"), (HOSTS[1], "/uat/")):
            self.assertIn(f"  PROBLEM {host} {path}: HTTP on this server answers 301 to https://{host}{path}, but the route has "
                          "http, so port 80 must serve it for a TLS front: run apply (http was added after the last apply), or "
                          f"another nginx file serves {host} on port 80 (see nginx files)\n", out)
        self.assertTrue(out.endswith("\nResult: 4 problem(s) found\n"), out)
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        for host in HOSTS:
            self.assertIn(f"  PROBLEM {host} {FIRST[host]}: the front at 203.0.113.10 (Server: CloudWAF) gets a redirect to the same "
                          "address again and again, although the route has http: a redirect loop. Make port 80 serve the route (see "
                          f"the HTTP line of this route), then ask the front's owner to check its entry for {host}\n", out)
        self.assertNotIn("or add http to this route", out)
        self.assertTrue(out.endswith("\nResult: 6 problem(s) found\n"), out)

    def test_a_front_on_port_80_to_http_routes_is_ok(self):
        api, rp = HOSTS
        port80 = {"code": "200", "server": "nginx/1.24.0 (Ubuntu)"}
        self.routes.write_text(HTTP_ROUTES)
        self.healthy(web={"127.0.0.1:80": port80, "203.0.113.10:443": {"code": "200", "server": "CloudWAF"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 0)
        for host in HOSTS:
            self.assertIn(f"  OK {host} {FIRST[host]}: through the front at 203.0.113.10 answers 200 (Server: CloudWAF)\n", out)
        self.assertNotIn("PROBLEM", out)
        self.assertEqual([call[-1] for call in self.calls() if "--connect-to" in call], [f"https://{api}/dev/api/", f"https://{rp}/dev/"])
        mixed = HTTP_ROUTES.replace("/uat/ 8111 http", "/uat/ 8111").replace("/dev/api/ 8000 strip,http", "/dev/api/ 8000 strip")
        self.routes.write_text(mixed)
        self.ok("apply")
        self.set_state(ss=LISTENING, others=UBUNTU_DEFAULT,
                       web={"127.0.0.1:80": port80,
                            f"127.0.0.1:80 http://{rp}/uat/": {"code": "301", "server": "nginx", "location": "{https}"},
                            f"127.0.0.1:80 http://{api}/dev/api/": {"code": "301", "server": "nginx", "location": "{https}"},
                            "203.0.113.10:443": {"code": "200", "server": "CloudWAF"},
                            f"203.0.113.10:443 https://{rp}/uat/": {"code": "301", "server": "CloudWAF", "location": "{url}"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        self.assertIn(f"  OK {rp} /dev/: HTTP on this server answers 200: served on port 80 too, for a TLS front (http)\n", out)
        self.assertIn(f"  OK {rp} /uat/: HTTP on this server answers 301 to https://{rp}/uat/\n", out)
        self.assertIn(f"  PROBLEM {rp} /uat/: the front at 203.0.113.10 (Server: CloudWAF) {LOOP}", out)
        self.assertLess(out.index(f"  OK {rp} /uat/: HTTP on this server"), out.index(f"  PROBLEM {rp} /uat/: the front"))
        self.assertIn(f"  OK {api} /dev/api/: through the front at 203.0.113.10 answers 200 (Server: CloudWAF)\n", out)
        self.assertEqual([call[-1] for call in self.calls() if "--connect-to" in call], [f"https://{api}/dev/api/", f"https://{rp}/uat/"])
        self.assertTrue(out.endswith("\nResult: 1 problem(s) found\n"), out)

    def test_api_routes_and_the_default_server_of_port_443_give_no_false_problem(self):
        api, rp = HOSTS
        self.routes.write_text(f"{api} /dev/api/ 8000 strip,api\n{api} /uat/api/ 8001 strip,http,api\n{rp} /dev/ 8110\n"
                               f"{rp} /uat/ - http\n")
        unauthorized = {"code": "401", "server": "nginx"}
        self.healthy(web={f"127.0.0.1:443 https://{api}/dev/api/": unauthorized, f"127.0.0.1:443 https://{api}/uat/api/": unauthorized,
                          f"127.0.0.1:80 http://{api}/uat/api/": unauthorized})
        conf = self.conf.read_text()
        self.assertIn("\n    listen 443 ssl default_server;\n", conf)
        self.assertIn("limit_req zone=perodua_https_api", conf)
        out = self.expect(self.diagnose(), 0)
        self.assertNotIn("PROBLEM", out)
        self.assertIn(f"  OK port 443: {self.conf}\n", out)
        self.assertNotIn(f"{self.conf} is the default server", out)
        self.assertEqual(out.count(" is the default server on port "), 1)
        self.assertNotIn("apply did not write", out)
        self.assertIn(f"  OK {api} /dev/api/: HTTPS on this server answers 401\n", out)
        self.assertIn(f"  OK {api} /dev/api/: HTTP on this server answers 301 to https://{api}/dev/api/\n", out)
        self.assertIn(f"  OK {api} /uat/api/: HTTP on this server answers 401: served on port 80 too, for a TLS front (http)\n", out)
        self.assertIn(f"  NOTE {rp} /uat/: port - (not known yet): not checked\n", out)

    def test_another_nginx_file_for_a_name_of_the_table_is_a_note(self):
        api, rp = HOSTS
        handmade = "/etc/nginx/conf.d/perodua-front.conf"
        front = (f"server {{\n    listen 443 ssl;\n    server_name stgiss.perodua.com.my {rp.upper()};\n"
                 "    location /dev/ {\n        proxy_pass http://127.0.0.1:8110;\n    }\n}\n"
                 f"server {{\n    listen 80;\n    server_name '{rp}'\n        {api}; # other.example\n"
                 "    location / { proxy_pass http://127.0.0.1:8110; }\n}\n")
        others = dict(UBUNTU_DEFAULT, **{
            handmade: front,
            "/etc/nginx/conf.d/alt-port.conf": f"server {{ listen 8080; server_name {rp}; }}",
            "/etc/nginx/conf.d/commented.conf": f"server {{\n    listen 80;\n    # server_name {rp};\n    server_name other.example;\n}}",
            "/etc/nginx/conf.d/upstream.conf": f"upstream app {{ server 127.0.0.1:8110; }}\nserver {{ server_name {api}; return 404; }}"})
        self.healthy(others=others)
        out = self.expect(self.diagnose(), 0)
        self.assertIn(other_file(handmade, f"{rp} (80, 443), {api} (80)"), out)
        self.assertIn(other_file("/etc/nginx/conf.d/upstream.conf", f"{api} (80)"), out)
        self.assertEqual(out.count("a file that apply did not write"), 2)
        self.assertIn(f"  OK port 80: /etc/nginx/sites-enabled/default, {handmade}, /etc/nginx/conf.d/commented.conf, "
                      f"/etc/nginx/conf.d/upstream.conf, {self.conf}\n", out)
        self.assertIn(f"  OK port 443: {handmade}, {self.conf}\n", out)
        self.assertLess(out.index("nginx files:"), out.index(other_file(handmade, f"{rp} (80, 443), {api} (80)")))
        self.assertLess(out.index(other_file(handmade, f"{rp} (80, 443), {api} (80)")), out.index("Host names:"))
        self.set_state(ss=LISTENING, others=dict(others, **{handmade: front.replace(f"'{rp}'", "other.example").replace(
            rp.upper(), "other.example").replace(api, "other.example")}))
        out = self.expect(self.diagnose(), 0)
        self.assertNotIn(f"{handmade}, a file that apply did not write", out)
        default = "/etc/nginx/sites-enabled/default"
        self.set_state(ss=LISTENING, others={default: UBUNTU_DEFAULT[default].replace("server_name _;", f"server_name _ {rp};")})
        out = self.expect(self.diagnose(), 0)
        self.assertIn(other_file(default, f"{rp} (80)"), out)
        self.assertIn(f"  NOTE {default} is the default server on port 80: ", out)
        self.assertEqual(out.count("a file that apply did not write"), 1)
        self.assertIn(f"Another nginx configuration already serves these host names; remove it first:\n{default}: {rp}\n",
                      self.refused("apply"))

    def test_a_hand_written_front_file_on_port_80_for_routes_without_http(self):
        handmade = "/etc/nginx/conf.d/perodua-front.conf"
        front = "".join(f"server {{\n    listen 443 ssl;\n    listen 80;\n    server_name {host};\n    location / {{\n"
                        "        proxy_pass http://127.0.0.1:8110;\n    }\n}\n" for host in HOSTS)
        self.healthy(others=dict(UBUNTU_DEFAULT, **{handmade: front}),
                     web={"127.0.0.1:80": {"code": "200", "server": "nginx/1.24.0 (Ubuntu)"},
                          "203.0.113.10:443": {"code": "200", "server": "CloudWAF"}})
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 4)
        self.assertIn(other_file(handmade, ", ".join(f"{host} (80, 443)" for host in HOSTS)), out)
        for host, path in (("api.example.perodua.com.my", "/dev/api/"), ("api.example.perodua.com.my", "/uat/api/"),
                           ("stgissrp.perodua.com.my", "/dev/"), ("stgissrp.perodua.com.my", "/uat/")):
            self.assertIn(f"  OK {host} {path}: HTTPS on this server answers 200\n", out)
            self.assertIn(served(host, path), out)
        for host in HOSTS:
            self.assertIn(f"  OK {host} {FIRST[host]}: through the front at 203.0.113.10 answers 200 (Server: CloudWAF)\n", out)
        self.assertNotIn("must only send browsers to HTTPS", out)
        self.assertTrue(out.endswith("\nResult: 4 problem(s) found\n"), out)
        self.routes.write_text(HTTP_ROUTES)
        out = self.expect(self.diagnose("--front", "203.0.113.10"), 0)
        for host, path in (("api.example.perodua.com.my", "/dev/api/"), ("stgissrp.perodua.com.my", "/uat/")):
            self.assertIn(f"  OK {host} {path}: HTTP on this server answers 200: served on port 80 too, for a TLS front (http)\n", out)
        self.assertIn(other_file(handmade, ", ".join(f"{host} (80, 443)" for host in HOSTS)), out)


if __name__ == "__main__":
    unittest.main()
