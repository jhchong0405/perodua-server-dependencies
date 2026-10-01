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

The script asks systemd, the fake systemctl here, only where systemd runs
(/run/systemd/system). In a container without systemd, such as the test image,
that directory is made for these tests and removed after them. Nothing else
outside temporary directories is changed.
"""

import datetime
import fcntl
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
FAKE_NGINX = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["nginx"] + args) + "\n")
with open(os.environ["FAKE_STATE"]) as f:
    state = json.load(f)
if (args == ["-t"] and state["t_status"]) or (args == ["-T"] and state["T_status"]):
    sys.exit("nginx: [emerg] fake failure")
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
DIRECTORY = {"newNonce": "https://acme.example/nonce", "newAccount": "https://acme.example/account",
             "newOrder": "https://acme.example/order", "meta": {"termsOfService": TERMS}}
DEFAULT_STATE = {"t_status": 0, "T_status": 0, "others": {}, "active": True, "ss": "", "reload_takes": True,
                 "probe_fails": False, "timer": "active", "renewal_result": "success", "unreachable": [],
                 "directory": DIRECTORY, "acme_dns_code": "200", "register_code": "201",
                 "docker": {"running": True, "image": True, "pull": True},
                 "lego": {"days": 90}, "dns": {"": {}}}
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
                 "docker": FAKE_DOCKER, "dig": FAKE_DIG,
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
                 ("api.example.perodua.com.my /dev/ 8000 rewrite", "the only option is strip"),
                 ("api.example.perodua.com.my /dev/ 8000 strip more", "too many columns"),
                 ("api.example.perodua.com.my /dev/ 8000\nAPI.EXAMPLE.perodua.com.my /dev/ 8001", "is listed twice"),
                 ("# nothing but a comment", "lists no host name")]
        for table, message in cases:
            with self.subTest(table=table):
                self.routes.write_text(table + "\n")
                self.assertIn(message, self.refused("csr"))
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
        self.assertIn("the routes changed since: apply", result.stdout)
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


if __name__ == "__main__":
    unittest.main()
