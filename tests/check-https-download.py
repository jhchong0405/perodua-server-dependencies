"""Exercise real curl HTTPS Basic authentication through its terminal prompt.

Only used by the disposable-container integration harness. No external cloud
service, network connection, persistent credential, or production DB is used.
"""

import base64
import http.server
import os
from pathlib import Path
import pty
import select
import signal
import ssl
import subprocess
import sys
import threading
import time


if os.environ.get("DEPLOY_DB_TEST_ISOLATED") != "1" or not Path("/.dockerenv").is_file():
    raise SystemExit("This check must run inside the disposable integration container")

source_dir, test_dir = map(Path, sys.argv[1:])
download_password = "Cloud:fixture'\"!password"
download_user = "download_user"
auth_header = "Basic " + base64.b64encode(
    f"{download_user}:{download_password}".encode()
).decode()
accepted_requests = []

subprocess.run(
    [
        "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
        "-keyout", str(test_dir / "https.key"),
        "-out", str(test_dir / "https.crt"), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
Path("/usr/local/share/ca-certificates/deploy-db-test.crt").write_bytes(
    (test_dir / "https.crt").read_bytes()
)
with (test_dir / "https-ca-install.log").open("w") as log:
    subprocess.run(["update-ca-certificates"], check=True, stdout=log, stderr=log)


class BackupHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        accepted = self.headers.get("Authorization") == auth_header
        accepted_requests.append(accepted)
        if not accepted:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="database fixture"')
            self.end_headers()
            return
        if self.path != "/fixture.dump":
            self.send_response(404)
            self.end_headers()
            return
        payload = (test_dir / "fixture.dump").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), BackupHandler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(test_dir / "https.crt", test_dir / "https.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()


def deployment(name, cloud_password):
    template = (test_dir / "config_only.conf").read_text()
    lines = [
        line for line in template.splitlines()
        if not line.startswith(("DB_NAME=", "DB_USER=", "BACKUP_FILE="))
    ]
    lines += [
        f"DB_NAME={name}", f"DB_USER=app_{name}",
        f"BACKUP_URL=https://127.0.0.1:{server.server_port}/fixture.dump",
        f"DOWNLOAD_USER={download_user}",
    ]
    config = test_dir / f"{name}.conf"
    config.write_text("\n".join(lines) + "\n")
    config.chmod(0o600)
    child, terminal = pty.fork()
    if child == 0:
        os.execv("/usr/bin/bash", [
            "bash", str(source_dir / "deploy-db.sh"), "--config", str(config),
            "--password-file", str(test_dir / "app.password"),
        ])

    output = bytearray()
    sent = False
    status = None
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.1)[0]:
                try:
                    chunk = os.read(terminal, 65536)
                except OSError:
                    chunk = b""
                output.extend(chunk)
                if b"password for user" in output.lower() and not sent:
                    os.write(terminal, cloud_password.encode() + b"\n")
                    sent = True
            exited, result = os.waitpid(child, os.WNOHANG)
            if exited:
                status = result
                break
        if status is None:
            os.killpg(child, signal.SIGKILL)
            os.waitpid(child, 0)
            raise AssertionError(f"{name} timed out waiting for interactive download")
    finally:
        os.close(terminal)
        (test_dir / f"{name}.log").write_bytes(output)
    assert sent, f"{name} did not request the cloud password from its terminal"
    assert cloud_password.encode() not in output, "Cloud password appeared in terminal output"
    return os.waitstatus_to_exitcode(status)


try:
    assert deployment("https_restored", download_password) == 0, "Authenticated HTTPS restore failed"
    assert deployment("https_refused", "incorrect-cloud-password") != 0, "Bad cloud credentials accepted"
    assert True in accepted_requests and False in accepted_requests, "HTTPS auth checks were not exercised"
finally:
    server.shutdown()
    server.server_close()

for log in Path("/var/lib/perodua-db-deploy").rglob("*.log"):
    assert download_password.encode() not in log.read_bytes(), "Cloud password in deployment log"

print("HTTPS fixture: correct and incorrect Basic-auth passwords exercised via PTY")
