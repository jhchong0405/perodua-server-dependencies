"""https.sh against fake nginx, systemctl, ss and curl, with real openssl.

The script requires root; run these in the test image (tests/Dockerfile) or skip
them elsewhere. A throwaway certificate authority (a root and an intermediate)
signs the requests the script makes, the way the certificate issuer would. The
fake nginx records every call, fails `-t` and `-T` on request, and prints for
`-T` the configuration files the test gives it plus the one the script wrote.
A reload or start through the fake systemctl copies the script's configuration
and the chain it names to what nginx "runs", unless the test says the reload
does not take; `openssl s_client`, which the script uses to ask nginx what it
runs, answers from that copy. Every other openssl command is the real one.
Nothing outside temporary directories is changed.
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
FAKE_CURL = "#!/bin/sh\nprintf 200\n"
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


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and Path("/proc/self/fd").is_dir(),
                     "needs root on Linux (the test image)")
class HttpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / "state"
        self.dir.mkdir()
        self.routes = self.dir / "routes.conf"
        self.routes.write_text(ROUTES)
        self.conf = self.tmp / "nginx" / "conf.d" / "perodua-https.conf"
        self.log = self.tmp / "calls.log"
        self.state = self.tmp / "state.json"
        self.set_state()
        bin_dir = self.bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fakes = {"nginx": FAKE_NGINX, "systemctl": FAKE_SYSTEMCTL, "ss": FAKE_SS, "curl": FAKE_CURL, "apt-get": FAKE_APT,
                 "openssl": FAKE_OPENSSL.replace("REAL_OPENSSL", shutil.which("openssl"))}
        for name, text in fakes.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_LOG=str(self.log),
                        FAKE_STATE=str(self.state), FAKE_OURS=str(self.conf), FAKE_RUNNING=str(self.tmp / "running"))
        self.ca = Authority(self.tmp / "ca")
        self.addCleanup(LOCK.unlink, missing_ok=True)

    def set_state(self, t_status=0, T_status=0, others=None, active=True, ss="", reload_takes=True, probe_fails=False):
        self.state.write_text(json.dumps({"t_status": t_status, "T_status": T_status, "others": others or {},
                                          "active": active, "ss": ss, "reload_takes": reload_takes,
                                          "probe_fails": probe_fails}))

    def run_script(self, *args, directory=None):
        self.log.unlink(missing_ok=True)
        # a session of its own: no terminal, as over ssh without one
        return subprocess.run(["bash", str(SCRIPT), *map(str, args), "--dir", str(directory or self.dir),
                               "--nginx-conf", str(self.conf)], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120, start_new_session=True)

    def ok(self, *args):
        result = self.run_script(*args)
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

    def test_apply_needs_every_port_and_a_certificate(self):
        self.routes.write_text(ROUTES + "api.example.perodua.com.my /dev/ -\n")
        self.ok("csr")
        self.assertIn("Set the port of api.example.perodua.com.my/dev/", self.refused("apply"))
        self.routes.write_text(ROUTES)
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
        for args in (["csr"], ["install-cert", self.ca.d / "int.pem"], ["apply"], ["uninstall", "--confirm", "yes"]):
            with self.subTest(args=args):
                self.assertIn("Another https.sh command is running", self.refused(*args))
        self.assertEqual(self.files(), ["routes.conf"])


if __name__ == "__main__":
    unittest.main()
