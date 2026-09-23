"""Interactive database password entry against real PostgreSQL: asked again, never echoed.

Only used by the disposable-container integration harness. Runs deploy-db.sh
on a pseudo-terminal without --password-file: a too-short password and two
different entries are each refused with a reason and asked again, then a valid
password deploys.

usage: check-password-prompt.py SCRIPTS_DIR CONFIG
"""

import os
from pathlib import Path
import pty
import select
import signal
import sys
import time


if os.environ.get("DEPLOY_DB_TEST_ISOLATED") != "1" or not Path("/.dockerenv").is_file():
    raise SystemExit("This check must run inside the disposable integration container")

scripts_dir, config = map(Path, sys.argv[1:])
SHORT = b"Sh0rt!pw"
VALID = b"Prompt Fixture #2026"
OTHER = b"Different Fixture #2026"
PASSWORD = b"(12 or more characters): "
CONFIRM = b"Confirm database password: "
answers = [(PASSWORD, SHORT), (PASSWORD, VALID), (CONFIRM, OTHER), (PASSWORD, VALID), (CONFIRM, VALID)]

child, terminal = pty.fork()
if child == 0:
    os.execv("/usr/bin/bash", ["bash", str(scripts_dir / "deploy-db.sh"), "--config", str(config)])

output, cursor, status = b"", 0, None
deadline = time.monotonic() + 120


def read_more(wait=0.1):
    global output
    if not select.select([terminal], [], [], wait)[0]:
        return True
    try:
        chunk = os.read(terminal, 65536)
    except OSError:
        return False
    output += chunk
    return bool(chunk)


try:
    for prompt, answer in answers:
        while prompt not in output[cursor:]:
            if time.monotonic() > deadline or not read_more():
                raise AssertionError(f"missing prompt {prompt!r}:\n{output.decode(errors='replace')}")
        cursor = output.index(prompt, cursor) + len(prompt)
        os.write(terminal, answer + b"\n")
    while status is None and time.monotonic() < deadline:
        read_more()
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

text = output.decode(errors="replace")
(config.parent / "password-prompt.log").write_text(text)
assert status is not None and os.waitstatus_to_exitcode(status) == 0, "interactive deployment failed:\n" + text
assert "Please try again" in text, "a too-short password was not refused with a reason"
assert "The two entries are different" in text, "different entries were not refused with a reason"
assert "SUCCESS" in text, text
for secret in (SHORT, VALID, OTHER):
    assert secret not in output, "a password was echoed to the terminal"
    for log in Path("/var/lib/perodua-db-deploy").rglob("*.log"):
        assert secret not in log.read_bytes(), f"a password reached {log}"
print("password prompt: short and mismatched entries asked again, valid entry deployed, nothing echoed")
