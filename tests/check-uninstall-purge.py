"""uninstall.sh --role db --purge against the real PostgreSQL 16 packages.

Only used by the disposable-container integration harness, as its last check:
it uninstalls PostgreSQL. A wrong answer to the PURGE question must change
nothing. Then a non-interactive run (KIT_NON_INTERACTIVE=1 on a terminal, as
ssh -t gives an agent) with --confirm-purge PURGE removes the deployment, the
packages and their data without a question.

usage: check-uninstall-purge.py SCRIPTS_DIR CONFIG DB_NAME
"""

import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time


if os.environ.get("DEPLOY_DB_TEST_ISOLATED") != "1" or not Path("/.dockerenv").is_file():
    raise SystemExit("This check must run inside the disposable integration container")

scripts_dir, config, db_name = sys.argv[1:]
PROMPT = b"Type PURGE to uninstall PostgreSQL 16"


def run(answer, *extra):
    """uninstall.sh --purge on a pseudo-terminal. answer: typed at the PURGE
    question; None: a non-interactive run with extra options, that must not ask."""
    env = {key: value for key, value in os.environ.items() if key != "KIT_NON_INTERACTIVE"}
    if answer is None:
        env["KIT_NON_INTERACTIVE"] = "1"
    child, terminal = pty.fork()
    if child == 0:
        os.execve("/usr/bin/bash", ["bash", f"{scripts_dir}/uninstall.sh", "--role", "db",
                                    "--config", config, "--confirm", db_name, "--purge", *extra], env)
    output, status = b"", None
    deadline = time.monotonic() + 180
    sent = False

    def read_more(wait=0.1):
        nonlocal output
        if not select.select([terminal], [], [], wait)[0]:
            return True
        try:
            chunk = os.read(terminal, 65536)
        except OSError:
            return False
        output += chunk
        return bool(chunk)

    try:
        while status is None and time.monotonic() < deadline:
            read_more()
            if not sent and PROMPT in output and answer is not None:
                os.write(terminal, answer + b"\n")
                sent = True
            done, result = os.waitpid(child, os.WNOHANG)
            if done:
                status = result
        while read_more(0.3) and select.select([terminal], [], [], 0.3)[0]:
            pass
    finally:
        if status is None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(terminal)
    assert status is not None, "the purge waited:\n" + output.decode(errors="replace")
    if answer is None:
        assert PROMPT not in output, "a non-interactive purge asked:\n" + output.decode(errors="replace")
    else:
        assert sent, "the PURGE question was not asked:\n" + output.decode(errors="replace")
    return os.waitstatus_to_exitcode(status), output.decode(errors="replace")


def installed(package):
    result = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package], capture_output=True, text=True)
    return result.stdout.strip() == "install ok installed"


def databases():
    return subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-XAt", "-c",
                           f"SELECT count(*) FROM pg_database WHERE datname = '{db_name}'"],
                          capture_output=True, text=True).stdout.strip()


code, output = run(b"no")
assert code != 0 and "did not match" in output, "a wrong PURGE answer was accepted:\n" + output
assert installed("postgresql-16") and databases() == "1", "a declined purge changed something"
assert "Other databases deleted with it:" in output, output

code, output = run(None)  # non-interactive without --confirm-purge: refused before any change
assert code != 0 and "--purge needs --confirm-purge PURGE" in output, output
assert installed("postgresql-16") and databases() == "1", "a refused purge changed something"

code, output = run(None, "--confirm-purge", "PURGE")
Path(config).with_suffix(".purge.log").write_text(output)
assert code == 0, "purge failed:\n" + output
for package in ("postgresql-16", "postgresql-client-16", "postgresql-contrib",
                "postgresql-common", "postgresql-client-common"):
    assert not installed(package), f"{package} is still installed"
for path in ("/var/lib/postgresql", "/etc/postgresql", "/var/lib/perodua-db-deploy"):
    assert not Path(path).exists(), f"{path} is still present"
print("uninstall purge: a wrong typed answer and a non-interactive run without --confirm-purge changed nothing; "
      "--confirm-purge PURGE in a non-interactive run removed the packages, data and records without a question")
