# Redirect loop behind the WAF — 2026-10-08

This is the record of an incident in the grey DEV environment. It also shows how
to repeat each check, with the scripts of this kit and with plain commands.
For the two setups that a WAF or another front proxy can use, see
[Behind a WAF or another front proxy](../README.md#behind-a-waf-or-another-front-proxy).

## Summary

- On 2026-10-07, Edge showed `ERR_TOO_MANY_REDIRECTS` on
  `stgissrp.perodua.com.my`.
- GICT had put a Huawei Cloud WAF in front of the App server. The WAF sent the
  HTTPS requests of the browsers to the App server over plain HTTP, port 80.
- Port 80 of the App server answered every request with a redirect (301) to
  `https://` with the same host name and path. The WAF sent this redirect back
  to the browser. The browser asked the WAF again, and the WAF asked port 80
  again: a redirect loop.
- On 2026-10-08, GICT changed the WAF entry of `stgissrp` to HTTPS, port 443
  (setup A). `stgissrp` then worked through the WAF.
- After that change, `stgiss`, `stgisssp` and `stgisscp` still looped. Each
  host name is a separate WAF entry. After a sign-out, users could not sign in
  again, because all sign-ins happen on `stgiss`.
- For these three names, the owner decided to serve the App on port 80 of the
  App server to the WAF (setup B). On 2026-10-08, the App server got a
  temporary hand-written nginx file for this. At 19:16 and 19:17 GMT on
  2026-10-08 (03:16 and 03:17 MYT on 2026-10-09), the owner merged the route
  option `http` for this setup into the kit (kit PR #34), and the route option
  `api` with a default server for port 443 (kit PR #35).
- On 2026-10-09, from a PC on the Perodua network, all four names answered
  `200 OK` through the WAF. The loop is gone for all four names
  ([evidence 7](#7-all-four-names-through-the-waf-2026-10-09)).
- It is not known whether the App server now uses the kit's `http` routes or
  still the hand-written file ([Still open](#still-open)).
- The hosts-file workaround of the PCs is no longer necessary. Remove it
  ([The PC workaround](#the-pc-workaround-hosts-file)).

## The environment

| Item | Value |
| --- | --- |
| Environment | Grey DEV environment |
| App server | `vciniss0`, `192.168.23.13`, Ubuntu |
| HTTPS on the App server | Host nginx, `nginx/1.24.0 (Ubuntu)`, configuration from `https.sh apply`. On 2026-10-08, also a temporary hand-written `/etc/nginx/conf.d/perodua-front.conf` for port 443 and port 80 of the four names (from the description of kit PR #34). |
| Host names | `stgiss.perodua.com.my`, `stgissrp.perodua.com.my`, `stgisssp.perodua.com.my`, `stgisscp.perodua.com.my` |
| Route of each host name | Path `/dev/` to `127.0.0.1:8110` |
| Release | Client Stable UIUX v1.0.8 |
| Front | Huawei Cloud WAF, set up by GICT. Each host name is a separate WAF entry. The WAF connects to port 443 for `stgissrp` (setup A, since 2026-10-08) and to port 80 for `stgiss`, `stgisssp` and `stgisscp` (setup B by the owner's decision; which nginx file serves port 80 is in [Still open](#still-open)). |

## Timeline

MYT is GMT+8.

| MYT | GMT | Event |
| --- | --- | --- |
| 2026-10-07 17:48:44 | 2026-10-07 09:48:44 | Edge shows `ERR_TOO_MANY_REDIRECTS` on `stgissrp`. From a Windows PC, `curl.exe` gets the same 301 from `CloudWAF` for each redirect ([evidence 1](#1-the-loop-through-the-waf-2026-10-07)). |
| 2026-10-08 14:20:33 | 2026-10-08 06:20:33 | The access log of the App server records a 301 for a portal link in Edge. The sender is the WAF address `202.165.23.137` ([evidence 3](#3-through-the-waf-and-directly-in-the-same-second-2026-10-08)). |
| 2026-10-08 14:24:21 | 2026-10-08 06:24:21 | From one Windows PC, in the same second: `stgiss` through the WAF gives a 301 (`Server: CloudWAF`). Directly, it gives `200 OK` (`Server: nginx/1.24.0 (Ubuntu)`). The access log records the 301 for the WAF address `202.165.23.17`. |
| 2026-10-08, time not recorded | — | Checks of the DNS, the ports and the nginx files ([evidence 4](#4-dns) and [5](#5-ports-and-nginx-files-of-the-app-server-2026-10-08)). |
| 2026-10-08, time not recorded | — | GICT changes only the WAF entry of `stgissrp` to HTTPS, port 443. `stgissrp` answers 200 through `CloudWAF`. `stgiss`, `stgisssp` and `stgisscp` still loop. A sign-in after a sign-out fails. |
| 2026-10-08, time not recorded | — | The App server runs a temporary hand-written `/etc/nginx/conf.d/perodua-front.conf`, for port 443 and port 80 of the four names. The source is the description of kit PR #34. |
| 2026-10-09 03:16 | 2026-10-08 19:16 | The owner merges kit PR #34: the route option `http`. A host name with an `http` route gets a port-80 server that forwards that route as on port 443, for a TLS front that connects on port 80. |
| 2026-10-09 03:17 | 2026-10-08 19:17 | The owner merges kit PR #35: the route option `api` (the API documentation closed, a rate limit), and a default server on port 443 that closes the connection for unknown names and the bare IP address. |
| 2026-10-09, time not recorded | — | From a PC on the Perodua network, read-only checks: the four names answer `HTTP/1.1 200 OK` with `Server: CloudWAF`, the hand-over modes are correct, and `http://stgiss.perodua.com.my/dev/app/` gets a 301 to `https://` from `CloudWAF` ([evidence 7](#7-all-four-names-through-the-waf-2026-10-09)). |

## Evidence

### 1. The loop through the WAF (2026-10-07)

On a Windows PC:

```
curl.exe -skS -L --max-redirs 4 -o NUL -D - https://stgissrp.perodua.com.my/dev/app/
```

Each redirect gave the same block. The record keeps these lines (the cookie
values are removed):

```
HTTP/1.1 301 Moved Permanently
Server: CloudWAF
Content-Type: text/html
Content-Length: 178
Set-Cookie: HWWAFSESID=...; path=/
Set-Cookie: HWWAFSESTIME=...; path=/
Location: https://stgissrp.perodua.com.my/dev/app/
```

What this shows:

- The `Location` is the URL of the request. The redirect goes back to the same
  address.
- `Server: CloudWAF` is the WAF. `HWWAFSESID` and `HWWAFSESTIME` are cookies of
  Huawei Cloud WAF.
- The body has 178 bytes. The 301 page of nginx has 157 bytes plus the server
  token, and a token such as `nginx/1.24.0 (Ubuntu)` has 21 characters. The
  redirect came from an nginx behind the WAF.

### 2. The same answer on a sandbox

A sandbox gave the same answers:

- HTTP to port 80 with the host name `stgissrp.perodua.com.my`: the same 301,
  178 bytes, the same `Location`.
- HTTPS to port 443: 200.

### 3. Through the WAF and directly, in the same second (2026-10-08)

On the same Windows PC, at 14:24:21 +0800, through the WAF:

```
curl.exe -skS -o NUL -D - https://stgiss.perodua.com.my/dev/app/
```

Result: `301 Moved Permanently`, `Server: CloudWAF`.

In the same second, directly to the App server:

```
curl.exe -skS -o NUL -D - --resolve stgiss.perodua.com.my:443:192.168.23.13 https://stgiss.perodua.com.my/dev/app/
```

Result: `HTTP/1.1 200 OK`, `Server: nginx/1.24.0 (Ubuntu)`.

The access log of the App server, `/var/log/nginx/access.log`, had this line:

```
202.165.23.17 - - [08/Oct/2026:14:24:21 +0800] "GET /dev/app/ HTTP/1.1" 301 178 "-" "curl/8.13.0"
```

Earlier, a portal link in Edge gave this line. The record keeps the line up to
the referrer:

```
202.165.23.137 - - [08/Oct/2026:14:20:33 +0800] "GET /dev/uiux/api/handover/start?next=%2Fapp%2F HTTP/1.1" 301 178 "https://stgissrp.perodua.com.my/"
```

What this shows:

- The App server works. HTTPS on port 443 answers 200.
- `202.165.23.17` and `202.165.23.137` are back-to-source addresses of the WAF.
  They are not the address of the PC.
- On port 443, the same request for `/dev/app/` answers 200. The 301 of 178
  bytes came from the port-80 server: the WAF sent the request over plain HTTP
  to port 80.

### 4. DNS

On the PC, the internal DNS gives three WAF addresses for each name:

| Host name | Addresses |
| --- | --- |
| `stgiss.perodua.com.my` | `202.165.23.249`, `202.165.23.69`, `202.165.23.66` |
| `stgissrp.perodua.com.my` | `202.165.23.49`, `202.165.23.200`, `202.165.23.187` |
| `stgisssp.perodua.com.my` | `202.165.23.126`, `202.165.23.209`, `202.165.23.172` |
| `stgisscp.perodua.com.my` | `202.165.23.252`, `202.165.23.235`, `202.165.23.110` |

Public DNS (`8.8.8.8`, `1.1.1.1`) had no record for the four names. A server on
the internet checked this on 2026-10-08. The site is therefore not reachable by
name from the internet.

### 5. Ports and nginx files of the App server (2026-10-08)

`ss -tln` showed these listening addresses:

| Address and port | Use |
| --- | --- |
| `0.0.0.0:22`, `[::]:22` | SSH |
| `0.0.0.0:80`, `[::]:80` | nginx, port 80 |
| `0.0.0.0:443` | nginx, HTTPS |
| `127.0.0.1:8110` | The App web container, this server only |
| `127.0.0.1:5432`, `127.0.1.1:5432` | PostgreSQL |
| `127.0.0.1:28001`, `127.0.0.1:28002` | Local services, this server only |
| `127.0.0.53:53`, `127.0.0.54:53` | The local DNS resolver |

`nginx -T` showed two files that listen on port 80:

- `/etc/nginx/conf.d/perodua-https.conf`, from `https.sh apply`: `listen 80`
  for the four names, and four `listen 443 ssl` servers.
- `/etc/nginx/sites-enabled/default`, the default site of Ubuntu:
  `listen 80 default_server;` and `listen [::]:80 default_server;`. It answers
  every other name, and the bare IP address, on port 80 with the
  "Welcome to nginx" page.

From the PC, TCP port 443 of `192.168.23.13` answered. Port 8110 gave `000`
(no connection): the App port is open to this server only.

### 6. An nginx worker restarted once

One nginx worker had a process ID out of sequence: one worker restarted one
time. This is not related to the loop. To examine it, look for
`exited on signal` in `/var/log/nginx/error.log`.

### 7. All four names through the WAF (2026-10-09)

From a PC on the Perodua network, with read-only requests:

| Request | Result |
| --- | --- |
| `https://NAME/dev/app/`, for each of the four names | `HTTP/1.1 200 OK`, `Server: CloudWAF` |
| `https://stgiss.perodua.com.my/dev/uiux/api/handover/mode` | Mode `hub` |
| `https://NAME/dev/uiux/api/handover/mode` for `stgissrp`, `stgisssp` and `stgisscp` | Mode `module`, `enabled` true |
| `http://stgiss.perodua.com.my/dev/app/` | `301 Moved Permanently`, `Server: CloudWAF`, `Location: https://stgiss.perodua.com.my/dev/app/` |

What this shows:

- `Server: CloudWAF`: the requests went through the WAF, not directly to the
  App server.
- The loop is gone for all four names. `stgissrp` uses setup A. For the other
  three, the WAF connects on port 80 and port 80 serves the App (setup B, by
  the owner's decision).
- The modes are correct, so the WAF keeps the `Host` header.
- The WAF itself redirects `http://` to `https://` for `stgiss`. Setup B needs
  this. The record has this answer for `stgiss` only.
- The checks do not show which nginx file serves port 80 on the App server:
  the kit's `http` routes or the hand-written file ([Still open](#still-open)).

## Root cause

```
Browser --HTTPS--> WAF --HTTP, port 80--> nginx on the App server
Browser <---301--- WAF <------301-------- Location: https://<same host><same path>
Browser --HTTPS--> WAF --HTTP, port 80--> nginx on the App server   (again, until the browser stops)
```

- The WAF entries sent the HTTPS requests of the browsers to the origin (the
  App server) over plain HTTP, port 80.
- At that time, `https.sh apply` wrote one port-80 server for all names. It
  answered every request with `return 301 https://$host$request_uri;`. This
  sends browsers that type `http://` to HTTPS. It is correct when the
  browsers, or the front, use port 443 for HTTPS.
- The WAF passed each 301 to the browser. The browser asked the same URL again.
  Edge stopped with `ERR_TOO_MANY_REDIRECTS`.

Huawei documents this case:
[Why Was My Website Redirected So Many Times?](https://support.huaweicloud.com/eu/trouble-waf/waf_01_0117.html)

The WAF keeps the `Host` header. After the repair of `stgissrp`,
`https://stgissrp.perodua.com.my/dev/uiux/api/handover/mode` returned
`"mode": "module"`.

The sign-in failed after the repair of `stgissrp`, because all sign-ins happen
on `stgiss`, the sign-in host. `stgissrp` sends the browser to `stgiss`, and
`stgiss` still looped.

Since kit PR #34, a route with the `http` option is served on port 80 as on
port 443, and only the routes without `http` get the redirect. A front that
connects on port 80 to a route without `http` still makes this loop.

## What was changed

| Date | Who | Change | Result |
| --- | --- | --- | --- |
| 2026-10-08 | GICT | The WAF entry of `stgissrp.perodua.com.my` only: back-to-source HTTPS, port 443 (setup A) | `stgissrp` answers 200 through `CloudWAF`. `/dev/uiux/api/handover/mode` returns `"mode": "module"`. |
| 2026-10-08 | The owner | Port 80 of the App server serves the App to the WAF for `stgiss`, `stgisssp` and `stgisscp` (setup B), first with the temporary hand-written `/etc/nginx/conf.d/perodua-front.conf` | On 2026-10-09, the three names answer 200 through `CloudWAF` ([evidence 7](#7-all-four-names-through-the-waf-2026-10-09)). |
| 2026-10-08 19:16 GMT | The owner | Kit PR #34: the route option `http` for setup B | The kit can serve setup B without a hand-written file. |
| 2026-10-08 19:17 GMT | The owner | Kit PR #35: the route option `api`, and the default server of port 443 that closes unknown names | `apply` refuses another default server for port 443. |

This record comes with two checks in this kit: `https.sh diagnose` on the App
server and `scripts/client-check.ps1` on a Windows PC. See
[How to repeat the checks](#how-to-repeat-the-checks).

## Still open

This list is the state on 2026-10-09. Check each item again before you mark it
done.

On the App server:

- [ ] Move the App server from the hand-written
  `/etc/nginx/conf.d/perodua-front.conf` to the kit's `http` routes, if this
  is not done. First check what nginx runs:

  ```bash
  sudo bash https.sh status
  sudo nginx -T | grep -E 'configuration file|listen|server_name'
  ```

  The move is done when no other nginx file than
  `/etc/nginx/conf.d/perodua-https.conf` serves the four names, and `status`
  shows `HTTP answer on port 80: 200` for the routes that the WAF sends to
  port 80. Otherwise, in the folder of the kit
  (`/app01/perodua/perodua-server-dependencies`): run `git pull`, add `http`
  to the `/dev/` lines of `stgiss`, `stgisssp` and `stgisscp` in
  `/etc/perodua-https/routes.conf` (the steps of kit PR #34 add it to all four
  lines, which also works), remove the hand-written file, then run
  `sudo bash scripts/https.sh apply` and `sudo bash scripts/https.sh status`.
  Do the steps in this order: if `apply` runs while these routes have no
  `http`, port 80 redirects again and the WAF loops for the three names.
  `apply` refuses names that another nginx file serves, and `diagnose` gives
  a `NOTE` for such a file.

With GICT:

- [ ] Set the read and write timeouts from the WAF to the origin to 720
  seconds, for long reports and imports.
- [ ] Give the full back-to-source address ranges of the WAF. Then a firewall
  can let only these ranges reach port 80, which serves the App without
  encryption in setup B.
- [ ] Tell whether the WAF appends the client address to `X-Forwarded-For` or
  replaces it ([Later: client addresses behind the WAF](#later-client-addresses-behind-the-waf)).
  This also applies to the limit for each caller address of an `api` route
  (kit PR #35).
- [ ] Use a WAF certificate that covers all four host names.
- [ ] Use no JavaScript anti-crawler and no CC "verification code" on
  `/dev/uiux/api/`.

On the PCs:

- [ ] Remove the bypass lines from the hosts file of each PC that has them
  ([The PC workaround](#the-pc-workaround-hosts-file)).
- [ ] Run `client-check.ps1` on a PC without bypass lines. Each verdict must be
  `OK`.

## The two setups and their risks

Each host name uses one setup. Both are supported by the kit. A WAF entry that
connects on port 80 to a route without `http` makes the redirect loop of this
record. The remedy is setup A or setup B.

| | A: HTTPS to port 443 | B: HTTP to port 80, with `http` |
| --- | --- | --- |
| Change on the WAF | Back-to-source HTTPS, port 443, with the original `Host` header | None. The WAF keeps port 80 and the original `Host` header. |
| Change on the App server | None | `http` on the routes that the WAF reaches on port 80, then `apply` |
| The connection from the WAF to the App server | Encrypted | Not encrypted |

On the grey DEV server on 2026-10-09, the WAF connects to port 443 for
`stgissrp` (setup A). For `stgiss`, `stgisssp` and `stgisscp`, it connects to
port 80, and the owner decided on setup B. Port 80 serves the App to the WAF
for these names. It is not known whether the kit's `http` routes or the
hand-written file serve it ([Still open](#still-open)).

The risks of setup B, as the owner accepted them in kit PR #34:

- Port 80 serves these routes, without encryption, to anybody who can reach
  it. `https.sh` does not check the address of the caller. Let only the WAF's
  back-to-source ranges reach port 80, when GICT gives them.
- The connection from the WAF to the App server is not encrypted. Passwords
  and session cookies cross it as plain text. The WAF addresses are public
  addresses (`202.165.23.x`).
- The WAF must redirect `http://` to `https://` itself. On 2026-10-09, it did
  this for `stgiss` ([evidence 7](#7-all-four-names-through-the-waf-2026-10-09)).
- The WAF must keep the `Host` header. On 2026-10-09, the hand-over modes
  showed that it does.

If the owner later chooses setup A for `stgiss`, `stgisssp` and `stgisscp`:
GICT forwards these names to HTTPS, port 443. Then the connection from the WAF
to the App server is encrypted. After that change, remove `http` from these
routes and run `apply`.

In both setups:

- Do not send the WAF to the App port (8110). That port bypasses the nginx of
  the kit: no `Secure` session cookie, no `X-Forwarded-Proto: https`. On the
  App server, it listens on `127.0.0.1` only, so the WAF cannot reach it.
- Do not keep a hosts-file bypass on a PC. The PC then does not use the WAF,
  and a check from that PC does not test the WAF.

## The PC workaround (hosts file)

Before the repair, a PC could open a name directly on the App server, with a
hosts-file line for each name:

```
192.168.23.13 stgiss.perodua.com.my # perodua-bypass
```

`client-check.ps1 -AppServer 192.168.23.13 -AddHostsBypass` added these lines.
On 2026-10-09, all four names worked through the WAF, so the workaround is no
longer necessary. Remove it from each PC that has it.

**With the script.** Open PowerShell with "Run as administrator". Go to the
folder of `client-check.ps1`, then run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\client-check.ps1 -RemoveHostsBypass
.\client-check.ps1 -AppServer 192.168.23.13
```

`-RemoveHostsBypass` removes every line that maps only these names, with or
without the `# perodua-bypass` mark. On a line that also maps other names, it
removes only these names and keeps the line for the other names. Then it
clears the DNS cache and prints what it changed. It refuses without
administrator rights. The second command checks each name through the WAF.

**By hand.** Open Notepad with "Run as administrator". Open
`C:\Windows\System32\drivers\etc\hosts` and remove the lines of the four names
that point to `192.168.23.13`. Save the file. In a command prompt opened with
"Run as administrator", clear the DNS cache:

```
ipconfig /flushdns
```

Close all Edge windows, then open Edge again.

## How to repeat the checks

### With the scripts of this kit

**On the App server**, in the `scripts` folder:

```bash
sudo bash https.sh diagnose --front 202.165.23.249
```

`202.165.23.249` is one of the WAF addresses of `stgiss`
([evidence 4](#4-dns)). The PCs resolve other WAF addresses for each name. Run
`diagnose` once with an address of each name, for example
`--front 202.165.23.126` for `stgisssp`, and read the line of that name. The
check through the WAF needs a network path from the App server to that address
on port 443. Without `--front`, `diagnose` uses the address that the App server
itself resolves for each name. It skips that check with a `NOTE` when there is
no address, or when the address is one of the App server's own.

`diagnose` changes no file, does not reload nginx and needs no certificate. It
prints the sections `Ports:`, `nginx files:`, `Host names:`, `Access log:` and
`nginx workers:`. Each line under a section starts with `OK`, `PROBLEM` or
`NOTE`.

Under `nginx files:`, a hand-written file such as
`/etc/nginx/conf.d/perodua-front.conf` that serves names of the table gives a
line of this form:

```
NOTE FILE, a file that apply did not write, serves host names of the table: NAME (80, 443). apply refuses a host name that another nginx file serves, and for one host name and port nginx uses the file that it reads first. To serve them with the routes of the table, first add http to each route that a TLS front reaches on port 80 (README: Behind a TLS front on port 80), then remove FILE and run apply
```

Under `Host names:`, a route with `http` (setup B) that port 80 serves gives:

```
OK HOST PATH: HTTP on this server answers 200: served on port 80 too, for a TLS front (http)
```

A route without `http` that a hand-written file serves on port 80 gives a
line of this form:

```
PROBLEM HOST PATH: HTTP on this server answers 200 as HTTPS does, but the route has no http: another nginx file serves HOST on port 80 (see nginx files), or http was removed after the last apply. If a TLS front connects on port 80 for this route, first add http to the route (README: Behind a TLS front on port 80), else apply makes port 80 redirect it and the front loops. Then remove the other nginx file, if there is one, and run apply
```

For such a route that the WAF reaches on port 80: add `http` to the route,
remove the hand-written file, then run `apply`. `apply` refuses names that
another nginx file serves (see the `NOTE` under `nginx files:`). A route with
`http` whose port 80 still answers the redirect gives a `PROBLEM` too: run
`apply`, or remove the other nginx file.

For a name that loops, `Host names:` has a line of this form:

```
PROBLEM HOST PATH: the front at ADDRESS (Server: X) sends HTTPS requests to port 80 of this server, where this route redirects to https://: a redirect loop. Ask the front's owner to forward HTTPS to port 443 with the original Host header, or add http to this route (README: Behind a TLS front on port 80)
```

`Access log:` shows the senders of 301 answers (the top 5) as `NOTE` lines. Here
these are WAF addresses, such as `202.165.23.17` and `202.165.23.137`. When one
sender gets 5 or more 301 answers within 10 seconds for the same request from
the same browser (user agent), it is a redirect loop. A browser in the loop
sends about 20 requests in a few seconds, and the `curl.exe` command of
evidence 1 sends 5. For a loop in the last 30 minutes, it adds a line of this
form:

```
PROBLEM redirect loop from SENDER (N answers, last: REQUEST at TIME): it forwards HTTPS requests to port 80 of this server. Ask the front's owner to forward HTTPS to port 443 with the original Host header, or add http to the route of this request (README: Behind a TLS front on port 80)
```

For an earlier loop in the same file, it adds a `NOTE` line instead:

```
NOTE earlier redirect loop from SENDER (N answers, last: REQUEST at TIME, M minutes before this run): it forwarded HTTPS requests to port 80 of this server then
```

The loops of 2026-10-07 and 2026-10-08 are older than 30 minutes. `diagnose`
shows them as `NOTE` lines while they are in the current access log. It does
not read the rotated files.

`nginx workers:` counts the `exited on signal` lines of the error log
([evidence 6](#6-an-nginx-worker-restarted-once)). The last line is
`Result: no problem found` (exit status 0) or `Result: N problem(s) found`
(exit status 4).

**On a Windows PC**, in PowerShell, in the folder of `client-check.ps1`:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\client-check.ps1 -AppServer 192.168.23.13
```

For each name, the script shows the addresses that the PC resolves, the normal
path (through the WAF), TCP port 443 of the App server, the direct path, the
hand-over mode and a verdict. Remove the bypass lines first: with them, the
normal path also goes directly to the App server. With the state after the
repair of `stgissrp` on 2026-10-08, the verdicts are:

| Host name | Verdict |
| --- | --- |
| `stgiss.perodua.com.my` | `LOOP` |
| `stgissrp.perodua.com.my` | `OK` |
| `stgisssp.perodua.com.my` | `LOOP` |
| `stgisscp.perodua.com.my` | `LOOP` |

The exit status is then 4. The reason of a `LOOP` gives the two remedies:
HTTPS to port 443 on the WAF, or `http` on the route of the name. With the
state of [evidence 7](#7-all-four-names-through-the-waf-2026-10-09), each
verdict is expected to be `OK`, with exit status 0. This record has no output
of the script from 2026-10-09. The `Certificate:` line checks the WAF
certificate as Edge on the PC does. If the WAF certificate does not cover a
name, that name gets `FRONT problem`, not `OK`. The meaning of each verdict is
in [check 5 of the self-check](RUNBOOK.md#5-your-computer-does-each-name-reach-the-app).

### With plain commands

**On a Windows PC**, in PowerShell. `curl.exe` is part of Windows 10 version
1803 and later, and of Windows 11. `-k` does not check the certificate: these checks read only the headers.

```powershell
nslookup stgiss.perodua.com.my
curl.exe -skS -o NUL -D - https://stgiss.perodua.com.my/dev/app/
curl.exe -skS -o NUL -D - --resolve stgiss.perodua.com.my:443:192.168.23.13 https://stgiss.perodua.com.my/dev/app/
curl.exe -skS -L --max-redirs 4 -o NUL -D - https://stgiss.perodua.com.my/dev/app/
curl.exe -sS -o NUL -D - http://stgiss.perodua.com.my/dev/app/
curl.exe -skS https://stgiss.perodua.com.my/dev/uiux/api/handover/mode
curl.exe -skS https://stgissrp.perodua.com.my/dev/uiux/api/handover/mode
Test-NetConnection 192.168.23.13 -Port 443
Test-NetConnection 192.168.23.13 -Port 8110
```

| Result | Meaning |
| --- | --- |
| `nslookup` gives `202.165.23.x` addresses | The name goes through the WAF |
| Through the WAF: `HTTP/1.1 200 OK` with `Server: CloudWAF` | The name works through the WAF (2026-10-09: all four names) |
| Through the WAF: a 301 with `Server: CloudWAF` and the same URL in `Location`, again for each redirect | The loop: the WAF sends HTTPS requests to port 80 for a route without `http` |
| `http://` through the WAF: a 301 with `Server: CloudWAF` and `Location: https://...` | The WAF redirects `http://` to `https://` itself, as setup B needs |
| Directly (`--resolve`): `HTTP/1.1 200 OK`, `Server: nginx/1.24.0 (Ubuntu)` | The App server works |
| The mode: `"mode": "hub"` on `stgiss`, `"mode": "module"` on the other names | The WAF keeps the `Host` header |
| Port 443: `TcpTestSucceeded : True` | The PC reaches the App server directly |
| Port 8110: `TcpTestSucceeded : False` | As expected: the App port is open to the App server only |

Repeat the `curl.exe` and `nslookup` lines for each of the four names.

**On the App server:**

```bash
sudo ss -ltnp
sudo nginx -T 2>/dev/null | grep -E 'configuration file|listen|server_name'
curl -sS -o /dev/null -D - --max-time 10 --resolve stgiss.perodua.com.my:80:127.0.0.1 http://stgiss.perodua.com.my/dev/app/
curl -ksS -o /dev/null -D - --max-time 10 --resolve stgiss.perodua.com.my:443:127.0.0.1 https://stgiss.perodua.com.my/dev/app/
sudo grep '" 301 ' /var/log/nginx/access.log | tail -n 20
sudo awk '$9 == 301 { print $1 }' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -n 5
sudo grep -c 'exited on signal' /var/log/nginx/error.log
```

| Command | Expected result |
| --- | --- |
| `ss -ltnp` | nginx on `0.0.0.0:80` and `0.0.0.0:443`. The App on `127.0.0.1:8110` only. |
| `nginx -T` | `/etc/nginx/conf.d/perodua-https.conf` with `listen 80;`, `listen 443 ssl;` and `listen 443 ssl default_server;`. `/etc/nginx/sites-enabled/default` with `listen 80 default_server;`. No other file with the four names: a hand-written `perodua-front.conf` there is still open ([Still open](#still-open)). |
| `curl` to port 80 | For a route with `http` (setup B): the answer of port 443, such as `HTTP/1.1 200 OK`. For a route without `http`: `HTTP/1.1 301 Moved Permanently` and `Location: https://stgiss.perodua.com.my/dev/app/`. The 301 is correct for browsers. It makes the loop only when a front sends HTTPS requests here. |
| `curl` to port 443 | `HTTP/1.1 200 OK` |
| `grep '" 301 '` and `awk` | 301 lines and their senders. WAF addresses (`202.165.23.x`) among the senders show that the WAF uses port 80 for a route without `http`. |
| `grep -c 'exited on signal'` | The number of worker restarts. One restart is not related to the loop. |

## Later: client addresses behind the WAF

The nginx of `https.sh` sets `X-Forwarded-For` and `X-Real-IP` to the address
that connects to it (`$remote_addr`). It replaces what the caller sent. Behind
the WAF, this address is a back-to-source address of the WAF, such as
`202.165.23.17`. The App therefore sees WAF addresses, not the addresses of the
users' PCs. The access log shows the same. The limit of an `api` route (kit PR
#35) is for each caller address, so behind the WAF all callers share one limit
for each WAF address.

A later sign-in limit for each client needs the address of the user's PC.
Before that work:

1. GICT tells whether the WAF appends the client address to `X-Forwarded-For`
   or replaces it.
2. GICT gives the full back-to-source address ranges of the WAF.
3. Then nginx can take the client address from `X-Forwarded-For`, but only for
   requests from these ranges.

This kit does not change this now.
