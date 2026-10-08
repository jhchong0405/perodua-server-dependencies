# Client deployment self-check

For the default deployment. Replace `APP_SERVER_IP` with your App server address.
If your deployment uses a different project, path or port, use the values supplied at handover.
These checks do not restart or change the system.

## 1. App Server: are both services healthy?

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml ps -a
```

**PASS:** both `web` and `odoo` show `Up` and `(healthy)`.

## 2. App Server: can the App connect to the database?

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml exec -T odoo python3 /opt/deploy/preflight.py check
```

**PASS:** output is `READY` followed by a number, e.g. `READY 692`.
The number varies. `EMPTY`, `MISSING` or an error is not a pass.
`SETUP_PENDING` or `SETUP_UNMARKED` means a fresh UAT initialization did not
finish; rerun `sudo bash deploy-app.sh --init-db` in the release's `scripts` folder to complete it.

## 3. DB Server: is PostgreSQL online?

```bash
pg_lsclusters
```

**PASS:** version `16`, cluster `main`, port `5432`, status `online`.

## 4. Your computer: can you use the App?

Open **`http://APP_SERVER_IP:8110/app/`** (or your assigned HTTPS URL).
Sign in, open a business page and download an existing attachment.
**PASS:** the page loads and the downloaded file opens correctly.

## 5. Your computer: does each name reach the App?

Do this check when browsers open the App by host name with HTTPS (`https.sh`),
also through a WAF or another front proxy. Use a Windows PC on the network of
the users.

1. Copy `client-check.ps1` from the release's `scripts` folder to the PC.
2. Open PowerShell (Windows PowerShell 5.1 or PowerShell 7) in the folder of the file.
3. Run:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\client-check.ps1 -AppServer APP_SERVER_IP
   ```

`-Scope Process` applies to this PowerShell window only. Without `-AppServer`,
the script checks only the normal path. The defaults are the four host names of
the grey environment (`-Names`), the page `/dev/app/` (`-Path`) and
`/dev/uiux/api/handover/mode` (`-ModePath`). The first name is the sign-in host
(`-HubName`). For a UAT environment, add
`-Path /uat/app/ -ModePath /uat/uiux/api/handover/mode`.

For each host name, the script shows:

- the addresses that the PC resolves;
- the normal path: the status line, the `Server` header and the `Location` header;
- the certificate of the normal path, checked as a browser on this PC checks it
  (the other checks do not check the certificate);
- with `-AppServer`: TCP port 443 of the App server, and the direct path to the
  App server (status line and `Server` header);
- the hand-over mode through the normal path: `hub` for the sign-in host,
  `module` for the other names. Another value is a `PROBLEM`: the front changes
  the `Host` header. When the normal path gives no mode and the direct path
  gives one, it is a `PROBLEM` too: the front blocks or changes the App API path;
- the verdict.

**PASS:** the verdict of each host name is `OK`, and the exit status
(`$LASTEXITCODE`) is `0`. Any other verdict gives exit status `4`. Exit status
`2` means that the script refused to run: read its `ERROR` lines (a wrong
option, or no administrator rights for `-AddHostsBypass` or
`-RemoveHostsBypass`).

| Verdict | Meaning | What to do |
| --- | --- | --- |
| `OK` | The host name works through the normal path. | Nothing. |
| `LOOP` | The front sends HTTPS requests to port 80 of the App server, and port 80 redirects them back to HTTPS: a redirect loop. | Use one of the two setups of [Behind a WAF or another front proxy](../README.md#behind-a-waf-or-another-front-proxy). A: ask the front's owner to send HTTPS to port 443 of the App server, with the original Host header. B: on the App server, add `http` to the route of this name in `/etc/perodua-https/routes.conf`, then run `sudo bash https.sh apply` (know the risks first). |
| `FRONT problem` | The direct path works and the normal path fails. Or the normal path works, but the front changes the `Host` header (wrong hand-over mode), blocks the App API path, or gives a certificate that this PC does not trust. | Send the output to the front's owner. |
| `SERVER problem` | The direct path fails too, or it also redirects to the same URL. Or this PC sends the name straight to the App server (a hosts file line), and the App server gives a loop, a wrong mode or a certificate that this PC does not trust. | On the App server, run `sudo bash https.sh diagnose` in the `scripts` folder. |
| `UNREACHABLE` | The page does not open on the normal path, and the script cannot tell why: no answer, the PC cannot resolve the name, no `-AppServer` to compare, or no answer from port 443 of the App server either. | Read the reason on the `Verdict:` line. Without `-AppServer`, run again with `-AppServer APP_SERVER_IP`. Check the DNS server of the PC, the network and the firewalls between the PC, the host name and the App server. |
| `NOT CHECKED` | `curl.exe` is not on the PC, so no HTTP check ran. | Use Windows 10 version 1803 or later, or Windows 11, which have `curl.exe`. |

`-AddHostsBypass` and `-RemoveHostsBypass` change the hosts file of the PC.
They are not part of this check: see the
[PC workaround](WAF-REDIRECT-LOOP-2026-10-08.md#the-pc-workaround-hosts-file).
When the hosts file has bypass lines, the normal path also goes directly to the
App server, and the check does not test the front. Remove them with
`.\client-check.ps1 -RemoveHostsBypass` in a PowerShell window opened with
"Run as administrator".

## If a check fails

Send the failed check's output and the relevant log excerpt to support.
Remove passwords, tokens and sensitive business data before sharing.

**App Server:**

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml logs --no-color --tail 50 odoo web
```

**DB Server:**

```bash
sudo tail -n 50 /var/log/postgresql/postgresql-16-main.log
```

**If a page says it redirected you too many times** (`ERR_TOO_MANY_REDIRECTS`):

1. On a PC, do check 5. `LOOP` for a host name means that the front (a WAF or
   another front proxy) sends HTTPS requests to port 80 of the App server, and
   that port 80 redirects them back to HTTPS. Usually the route of this name
   has no `http` option.
2. On the App server, in the `scripts` folder, run:

   ```bash
   sudo bash https.sh diagnose
   ```

   With a front, add one of its addresses: `--front ADDRESS`. Use an address
   that the PC resolves for the host name (`nslookup HOST` on the PC).
3. A `PROBLEM` line about a redirect loop under `Host names:` confirms it.
   Under `Access log:`, a `PROBLEM` line shows a loop in the last 30 minutes,
   and a `NOTE` line an earlier loop.
4. Use one of the two setups for that host name:
   - A: ask the front's owner to send HTTPS to port 443 of the App server, with
     the original Host header. The connection stays encrypted.
   - B: add `http` to the route of the host name in
     `/etc/perodua-https/routes.conf`, then run `sudo bash https.sh apply`.
     Port 80 then serves the route, without encryption, to the front and to
     any other caller that reaches port 80. Know the risks first:
     [Behind a TLS front on port 80](../README.md#behind-a-tls-front-on-port-80).

   If the route already has `http` and port 80 still redirects, run
   `sudo bash https.sh apply`, or remove the other nginx file that serves the
   name. The `HTTP` line of the route under `Host names:` and the `NOTE`
   under `nginx files:` in `diagnose` show which.
5. Run check 5 and `diagnose` again.

Do not send the front to the App port (8110): it bypasses the nginx of
`https.sh`, and with `BIND_IP=127.0.0.1` it listens on the App server only. See
[Behind a WAF or another front proxy](../README.md#behind-a-waf-or-another-front-proxy)
and the [record of 2026-10-08](WAF-REDIRECT-LOOP-2026-10-08.md).
