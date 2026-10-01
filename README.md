# Server setup

Client Stable UIUX v1.0.4 on Ubuntu 24.04 amd64 servers with sudo and internet access:

- **DB server**: PostgreSQL 16.
- **App server**: Odoo and web containers, pulled from `perodua-deploy.novutal.com`.
- **ISS-Oracle API** (separate): the HTTP service in front of the Oracle EBS
  database, also pulled from there. See [ISS-Oracle API](#iss-oracle-api).
- **RP adapter** (separate): the RP integration gateway's read-only command line
  for the Oracle EBS database (health check, survey, exports), also pulled from
  there. See [RP adapter](#rp-adapter).

Both can also run on one server: do steps 2 and 3 on that server. With two
servers, allow **App → DB port 5432**. Browsers need **App port 8110**.

The steps below create a **fresh UAT system** with an empty database.
To restore an existing database instead, see [From a backup](#from-a-backup).
A system set up with an earlier version (v1.0.0 to v1.0.3) cannot keep its data:
download this version on the App server and run a [reset](#stop-start-and-reset)
there. The DB server needs no step.

## Repository layout

| Path | Contents |
| --- | --- |
| `scripts/` | `install-dependencies.sh`, `deploy-db.sh`, `deploy-app.sh`, `service.sh`, `uninstall.sh`, the helpers they use (`uat_guard.py`, `uat_admins.py`, `reset_database.py`), `iss-api.sh` (ISS-Oracle API), `rp-adapter.sh` (RP adapter), `https.sh` (HTTPS with nginx, the certificate from an issuer or Let's Encrypt) and the configuration templates. Your `deploy.conf`, backups and filestore archive also go here. |
| `docs/` | [DEPLOYMENT.md](docs/DEPLOYMENT.md) (full reference) and [RUNBOOK.md](docs/RUNBOOK.md) (self-check after deployment) |
| `tests/` | Automated checks, see [tests/README.md](tests/README.md) |

## 1. Download (both servers)

```bash
curl -fL https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/main -o setup.tar.gz
mkdir -p setup && tar -xzf setup.tar.gz -C setup --strip-components=1 && cd setup/scripts
```

Or, with git: `git clone https://github.com/jhchong0405/perodua-server-dependencies.git`,
then `cd perodua-server-dependencies/scripts`; update later with `git pull`. Use one
of the two, not both in the same place.

**All commands below run in this `scripts` folder.**

Standard Ubuntu servers include `curl`. On a minimal image without it, first run
`sudo apt-get update && sudo apt-get install -y curl ca-certificates`.

## 2. DB server

```bash
sudo bash install-dependencies.sh --role db
sudo bash deploy-db.sh
```

On one server, run `sudo bash install-dependencies.sh --role app` as well before
`deploy-db.sh`: the App runs in Docker, and the database setup needs Docker there.

The script asks:

1. What to set up: press Enter for a fresh UAT system.
2. Where the App runs: press Enter for another server, or enter 2 if it runs on
   this server too. With 2, questions 3 and 4 are skipped: PostgreSQL listens
   only on Docker's address on this server, accepts only the App's containers,
   and starts after Docker when the server boots.
3. This server's internal IP: it lists the addresses it found; press Enter to use
   the suggested one.
4. The App server's IP, as this server sees it: its private IP if both servers
   share a private network, otherwise its public IP (`curl -4 -s ifconfig.me`
   on the App server shows it). If you enter this server's own address, the
   script offers the one-server setup instead.
5. A new database password (at least 12 characters), twice. Keep it for the App server.

If an answer cannot be used, the script says why and asks again. Wait for
`SUCCESS`; it ends by printing the `DB_HOST` for the App server. A fresh UAT
system needs no backup settings. The answers are saved in `scripts/deploy.conf`
and reused by later runs.

## 3. App server

```bash
sudo bash install-dependencies.sh --role app
sudo systemctl enable --now docker
sudo bash deploy-app.sh --init-db
```

Enter the DB server's IP (the `DB_HOST` printed by `deploy-db.sh`). On one
server the database settings are already filled in; press Enter. Press Enter to
keep the defaults for the database port, name and user and the web port (8110).

Then choose who may open the web page:

- **Only this server**: open it from your computer through the SSH tunnel that
  the script prints at the end.
- **The private network**, through the server's private address (the default
  when the server has one).
- **Every computer that can reach the server.** On a server with a public IP,
  anyone on the internet could then sign in with the UAT passwords below. Put a
  firewall in front of the web port first: your cloud provider's firewall
  (security group), because ufw does not filter ports that Docker publishes. The
  script asks you to confirm this choice.

Then enter the database password. When asked, enter the registry username and
password for `perodua-deploy.novutal.com`. The first run takes several minutes.

**Serving the page under a path, such as `/dev`:** before the first run, write
only those settings into `/opt/perodua-app/app.env`, one per line, without
quotes. The script asks for the database settings as above and adds them to the
file. [Two environments on one server](#two-environments-on-one-server) explains
the settings.

```
HTTP_PORT=8110
BIND_IP=127.0.0.1
PUBLIC_ROOT=/dev
ENVIRONMENT_LABEL=DEV environment
PUBLIC_BASE_URL=https://stgissrp.perodua.com.my/dev
```

**If the first run stops at the database check** (a wrong `DB_HOST`, the DB
server's firewall, or a DB server that does not accept this App server), nothing
has used the database yet. The script prints this server's addresses. Correct
the setting in `/opt/perodua-app/app.env` or on the DB server and run
`sudo bash deploy-app.sh --init-db` again: it asks for the database password
again (press Enter to keep the one entered before).

## 4. Sign in and check

Open `http://APP_SERVER_IP:8110/app/` (or the web port you chose). If only this
server may open the page, open the SSH tunnel that `deploy-app.sh` printed and
use `http://localhost:8110/app/`.

| Login | Password | Access |
| --- | --- | --- |
| `whadmin`, `admin1`, `admin2` | `perodua` | Administrator |
| `planner`, `whouse`, `sop`, `op`, `finance` | `perodua` | Business role |

These accounts are for UAT only. Do not use this setup for production or expose it
to the internet. The database starts without business data: no parts, customers,
suppliers, orders or EBS/PROMISE register rows, and no order types, customer
types, holiday types, order cycles, payment terms or price lists; MYR is the
only currency. Only the company (Perodua Parts Sdn Bhd) and the HQ warehouse are
set up ([check it without the web page](#check-the-data-without-the-web-page)).
Administrators add the lists with **+ New**; see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#initialize-a-fresh-uat-system-on-the-app-server)
for the two codes campaigns need and how order types behave.

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml ps
```

`web` and `odoo` should both be `healthy`. Running `deploy-app.sh` again keeps the
database and passwords and does not reinstall or upgrade modules. If the first run
stops part-way, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#initialize-a-fresh-uat-system-on-the-app-server).

## From a backup

You need a `database.dump` (`pg_dump -Fc`) with its SHA-256 and original locales,
and the matching `filestore.tar.gz` (`filestore/DB_NAME/...`). Use the same
database name on both servers.

- **DB server:** copy `database.dump` into the `scripts` folder and `chmod 600` it. Before
  running `deploy-db.sh`, run `cp deploy.conf.example deploy.conf && chmod 600 deploy.conf`
  and edit it: keep `DB_MODE=restore`, set `BACKUP_FILE`, `BACKUP_SHA256`,
  `DB_LC_COLLATE`, `DB_LC_CTYPE`, `DB_LISTEN_IP` (this server's internal IP) and
  `APP_CIDR` (the App server's `IP/32`). With a `deploy.conf` present the script
  asks no setup questions. For `en_US.utf8` locales, first run
  `sudo localedef -i en_US -f UTF-8 en_US.utf8`.
- **App server:** copy `filestore.tar.gz` into the `scripts` folder, `chmod 600` it and run
  `sudo bash deploy-app.sh` without `--init-db`. Only if it stops at
  `Filestore incomplete`, restore the archive and run it again:

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml \
  run --rm --no-deps --user 0 --entrypoint bash \
  -v "$PWD/filestore.tar.gz:/restore.tar.gz:ro" odoo -ec \
  'test ! -d "/var/lib/odoo/filestore/$DB_NAME"; tar --no-same-owner -xzf /restore.tar.gz -C /var/lib/odoo; chown -R odoo:odoo "/var/lib/odoo/filestore/$DB_NAME"'
sudo bash deploy-app.sh
```

Sign in with the web login from the backup.

## Operations

Back up the database on the DB server and the filestore volume on the App server.
Services start again after a reboot, except an App you stopped with `service.sh`.
This setup serves HTTP only; add HTTPS before production, for example with
[https.sh](#https).

## Two environments on one server

From v1.0.4, two separate systems can run on one App server under one host
name, for example `/dev/` and `/uat/`: two deployments, each with its own
directory, project name, database, web port and `PUBLIC_ROOT`, behind F5 or an
nginx that sends each path to its port. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#two-environments-on-one-server-by-path-dev-uat).
v1.0.3 does not support it.

## Check the data without the web page

On the DB server (with one server, on that server), this counts the records
behind the main workbench pages straight from the database, archived records
included. Use your database name if it is not `perodua`.

```bash
sudo -u postgres psql -X -P pager=off -d perodua <<'SQL'
SELECT 'Company' AS data, count(*) AS records FROM res_company
UNION ALL SELECT 'Warehouses', count(*) FROM stock_warehouse
UNION ALL SELECT 'Currencies', count(*) FROM res_currency
UNION ALL SELECT 'Products (parts)', count(*) FROM product_template WHERE perodua_item_no IS NOT NULL
UNION ALL SELECT 'BOM List', count(*) FROM perodua_bom
UNION ALL SELECT 'Customer', count(*) FROM res_partner WHERE customer_rank > 0
UNION ALL SELECT 'Supplier', count(*) FROM res_partner WHERE supplier_rank > 0
UNION ALL SELECT 'Sales Order', count(*) FROM sale_order
UNION ALL SELECT 'Purchase Order', count(*) FROM purchase_order
UNION ALL SELECT 'Forecast', count(*) FROM perodua_forecast
UNION ALL SELECT 'IDDI', count(*) FROM perodua_spdio
UNION ALL SELECT 'Campaign', count(*) FROM perodua_campaign
UNION ALL SELECT 'Supplier (EBS)', count(*) FROM perodua_ebs_supplier_mirror
UNION ALL SELECT 'Warehouses (EBS)', count(*) FROM perodua_ebs_branch_mirror
UNION ALL SELECT 'OEM Material (PROMISE)', count(*) FROM perodua_promise_material
UNION ALL SELECT 'Supplier Price List (PROMISE)', count(*) FROM perodua_supplier_pricing
UNION ALL SELECT 'Order Types', count(*) FROM sale_order_type
UNION ALL SELECT 'Customer Type', count(*) FROM perodua_customer_category
UNION ALL SELECT 'Holiday Types', count(*) FROM perodua_holiday_type
UNION ALL SELECT 'Order Cycles', count(*) FROM perodua_order_cycle
UNION ALL SELECT 'Payment Method', count(*) FROM account_payment_term
UNION ALL SELECT 'Retail Price List', count(*) FROM product_pricelist;
SQL
```

The names are the workbench pages the counts belong to. A fresh UAT system
shows 1 for Company (Perodua Parts Sdn Bhd), Warehouses (HQDC) and Currencies
(MYR) and 0 for everything else.

## Stop, start and reset

`service.sh` stops and starts the services without deleting anything. To stop
everything, stop the App first, on the App server:

```bash
sudo bash service.sh --role app stop
```

Then, on the DB server:

```bash
sudo bash service.sh --role db stop
```

To start again, run `start`: the database first, then the App. `restart` and
`status` work the same way. To stop only the App, leave the database running.
`start` checks the database first and refuses if it is not reachable or not set
up. A stopped App stays stopped after a reboot until you start it; PostgreSQL
starts again with the server.

To go back to a fresh UAT system, run this on the App server only:

```bash
sudo bash service.sh --role app reset
```

It shows its plan and asks you to type the database name. Then it stops the
App, deletes all data in the database and all attachments, and runs
`deploy-app.sh --init-db` again, which takes several minutes and may ask for
the registry account like the first deployment. The database, its login and
password, the web port and the other settings stay. `whadmin`, `admin1` and
`admin2` get the password `perodua` again. The reset installs the release of
the `scripts` folder you run it from, so it also moves a system set up with an
earlier version to this one. If it stops part-way, solve the problem it shows
and run it again.

## Uninstall

Uninstall removes the setup from the servers. To start over with an empty
system, a [reset](#stop-start-and-reset) is enough.

Uninstall the App first; the DB server refuses while the App is still connected.

On the App server:

```bash
sudo bash uninstall.sh --role app
```

It removes the containers, their anonymous Docker volumes and `/opt/perodua-app`.
The attachments volume and the images stay unless you add `--purge`. Deploying
the App again uses the kept volume with the same database; for a new empty
database, `deploy-app.sh` asks before deleting the old files in it.

Afterwards, and also when there is no `/opt/perodua-app`, it lists the other
containers with "perodua" in their name or image. An example is the App that the
earlier perodua-odoo package deployed (Compose project `perodua-odoo`). It
deletes each project only after you answer `y`, then asks separately about the
volumes those containers used. The images stay.

On the DB server:

```bash
sudo bash uninstall.sh --role db
```

It removes the database, its login role (and so its password), its access rules,
the listen address it added, its records and a guided `deploy.conf`. PostgreSQL
stays installed; `--purge` also uninstalls it and deletes every database on the
server. Both commands list what they will remove and ask you to type the
database or project name first.

## ISS-Oracle API

The ISS-Oracle API answers read-only queries on the Perodua Oracle EBS database
over HTTP and logs every request in the database. `iss-api.sh` runs the pinned
image `perodua-deploy.novutal.com/iss-oracle-api:v1.0.0` with Docker Compose on
an Ubuntu 24.04 server that can reach the database, for example the App server.
The server needs no Oracle client.

Before the first run:

- This server reaches the database port: `timeout 5 bash -c '</dev/tcp/DB_HOST/1521' && echo reachable`
  prints `reachable`. If not, open the firewall between them.
- The DBA has created [the request log table](#request-log-table-for-the-dba), and the
  database user can read the EBS views and tables the API serves.
- Docker is installed: `sudo bash install-dependencies.sh --role app`, then
  `sudo systemctl enable --now docker`.

```bash
sudo bash iss-api.sh install
```

It asks for the database server, port (1521), service name, user and password,
then who may call the API:

- **Only this server** (the default): for a reverse proxy such as [https.sh](#https), or an SSH tunnel, on this server.
- **Every computer that can reach the server.** Put your cloud provider's firewall
  (security group) in front first: ufw does not filter ports that Docker publishes.

When asked, enter the registry username and password for `perodua-deploy.novutal.com`;
the login is not kept on the server. Wait for `Deployment verified`: from the
container, the database was reachable, the request log was written and a request
without the key was refused; when the install issued a new key, a query with it
was answered too (`check --key` tests a query with a key you already have). The
script prints a new **API key once**. Give it to the calling system's backend,
which sends it in the `X-API-Key` header.

The answers are saved in `/opt/perodua-iss-api/iss-api.env` and the password in
`/opt/perodua-iss-api/secrets/db_password`, which only root can read on the host
and which never appears in `docker inspect`. The first install needs a new or
empty directory; a second deployment on one server needs its own `--dir` and
`PROJECT_NAME`. Running `install` again reuses the settings and asks nothing; to
change a setting, edit `iss-api.env` and run `install` again. To change the
database password, add `DB_PASSWORD_FILE=/root/db-password` (a file that holds
only the new password) to `iss-api.env` and run `install`: the container is
recreated with it. If the result does not become healthy, the last release that
did runs again with its settings and password, and the settings that failed are
kept in `iss-api.env.failed`. For an unattended install, pass the keys of
[iss-api.env.example](scripts/iss-api.env.example) with `--config` and add
`--non-interactive`; without `API_KEY_HASH` there, the deployed key stays.

| Command | What it does |
| --- | --- |
| `sudo bash iss-api.sh status` | State, image, listen address and database |
| `sudo bash iss-api.sh check` | The deployment check again; `--key` also tests a query with a key you enter |
| `sudo bash iss-api.sh logs` | The service log; `--follow` keeps reading |
| `sudo bash iss-api.sh stop` / `start` / `restart` | A stopped API stays stopped after a reboot until `start` |
| `sudo bash iss-api.sh gen-key` | Issues a new API key for the release that runs; the old one stops working at once. It refuses while `iss-api.env` holds changes that `install` has not applied |
| `sudo bash iss-api.sh uninstall` | After you type the project name, removes the container and the files `install` created in `/opt/perodua-iss-api`; `--purge` also removes the image |

A later version of this repository pins a later image: running its `install`
upgrades, and if the new image does not become healthy the last release that did
runs again.

| Symptom | Cause |
| --- | --- |
| `FAIL database ... is not reachable from the container` | No network path to the database port: firewall, VPN, or a wrong `DB_HOST` / `DB_PORT` |
| `FAIL GET / -> 500` | The API cannot sign in (`ORA-01017`), or the request log table or a grant is missing (`ORA-00942`). The error is in `sudo bash iss-api.sh logs` |
| A query with a key gets 401 | Wrong key. Issue a new one with `gen-key` |

### Request log table for the DBA

Every request, including one without a key, writes a row. Create the table in
the API user's schema (Oracle 12c or later) and delete old rows on a schedule:

```sql
CREATE TABLE API_REQUEST_LOG (
  LOG_ID          NUMBER GENERATED BY DEFAULT ON NULL AS IDENTITY PRIMARY KEY,
  REQUEST_DATE    DATE NOT NULL,
  REQUEST_METHOD  VARCHAR2(10),
  API_MODULE      VARCHAR2(100),
  API_URL         VARCHAR2(1000),
  CLIENT_IP       VARCHAR2(64),
  API_KEY_HASH    VARCHAR2(64),
  QUERY_STRING    VARCHAR2(4000),
  RESPONSE_STATUS NUMBER(3),
  EXECUTION_TIME  NUMBER(10),
  USER_AGENT      VARCHAR2(1000),
  REQUEST_BODY    VARCHAR2(4000),
  ERROR_MESSAGE   VARCHAR2(4000)
);
CREATE INDEX API_REQUEST_LOG_DATE_IX ON API_REQUEST_LOG (REQUEST_DATE);
-- for example daily: DELETE FROM API_REQUEST_LOG WHERE REQUEST_DATE < SYSDATE - 90;
```

The API queries the EBS objects by their plain names, so the user needs a
synonym and `SELECT` (`EXECUTE` for functions) for each of them.

## RP adapter

The RP adapter is the RP integration gateway's command line. It reads the Oracle
EBS database directly and read-only: a health check for monitoring, a survey of
every object the RP integrations use (columns, Oracle types, row counts, sample
rows) and exports of whole datasets. It can also check the ISS-Oracle API.
`rp-adapter.sh` runs the pinned image `perodua-deploy.novutal.com/rp-adapter:v0.1.0`,
one short-lived container per command, on an Ubuntu 24.04 server that can reach
the database, for example the App server. Nothing keeps running and the server
needs no Oracle client. The adapter runs only its own reviewed queries, each in a
read-only transaction; it never runs SQL typed at run time.

Before the first run:

- This server reaches the database port: `timeout 5 bash -c '</dev/tcp/DB_HOST/1521' && echo reachable`
  prints `reachable`.
- A database account for the adapter. **One with `SELECT` grants only is
  safest**: read-only transactions refuse writes, but a PL/SQL function with its
  own (autonomous) transaction could still write with a more powerful account.
  It needs a synonym and `SELECT` (`EXECUTE` for `GEN_GET_PART_MODEL`) for the
  objects in `sudo bash rp-adapter.sh run db datasets`, like the API's user.
- Docker is installed: `sudo bash install-dependencies.sh --role app`, then
  `sudo systemctl enable --now docker`.

```bash
sudo bash rp-adapter.sh install
```

It asks for the database server, port (1521), service name, user and password,
and, optionally, the ISS-Oracle API's address and key (when [iss-api.sh](#iss-oracle-api)
runs on this server: `http://127.0.0.1:8000`). When asked, enter the registry
username and password for `perodua-deploy.novutal.com`; the login is not kept on
the server. It ends with the database health check from the container; wait for
`Set up and verified`. A `1` in that check (some objects or datasets failed) is
usually a missing grant: the setup is kept, fix the grant and run `health`.

The answers are saved in `/opt/perodua-rp-adapter/rp-adapter.env`, the password
(and API key) in `/opt/perodua-rp-adapter/secrets/`, which only root can read on
the host; they reach the container as mounted files, never as arguments or
environment variables. Results go to `/opt/perodua-rp-adapter/output/`, readable
only by root. To change a setting, edit `rp-adapter.env` and run `install` again;
to change the password, add `DB_PASSWORD_FILE=/root/new-password` (a file that
holds only the password) to `rp-adapter.env` and run `install`: the password is
stored and the line removed. For an unattended install, pass the keys of
[rp-adapter.env.example](scripts/rp-adapter.env.example) with `--config` and add
`--non-interactive`.

| Command | What it does |
| --- | --- |
| `sudo bash rp-adapter.sh health` | Database health check: connection, sign-in, every object and dataset. `--deep` reads one row of each dataset; `--json` for tools |
| `sudo bash rp-adapter.sh survey` | The first look at the database: a report folder under `output/survey/` (`survey.md` to read, `survey.json` for tools, sample CSVs). `--exact` also counts every row (slow on large views); `--rows N` sample size |
| `sudo bash rp-adapter.sh export --all` | Every dataset to CSV (`--format jsonl` for JSON Lines) under `output/export/`, with `manifest.json` (row counts, sha256, column types). Name datasets instead of `--all` for fewer; `--max-rows N` to cap each |
| `sudo bash rp-adapter.sh api-health` | The ISS-Oracle API's health check (when set up with its address and key) |
| `sudo bash rp-adapter.sh run ...` | Any other adapter command: `run db datasets`, `run db objects`, `run db columns ISS_CUSTOMERS_V`, `run db size --exact`, `run db sample customers --rows 5` |
| `sudo bash rp-adapter.sh status` | Settings (no secrets), image, latest survey and export |
| `sudo bash rp-adapter.sh uninstall` | After you type the directory, removes the settings and secrets; the results stay unless `--purge`, which also removes the image |

A later version of this repository pins a later image; its commands pull it on
first use.

### Scripts and monitoring

`health`, `survey`, `export`, `api-health` and `run` return the adapter's exit
code, and with `--json` the adapter prints one JSON document on stdout (messages
go to stderr). `install` returns 0 once the setup is in place (its database
check was OK or WARNING, as it prints) and 3 when it is not. Errors of the script
itself print no JSON:

| Exit code | `health` | `survey`, `export`, `run` |
| --- | --- | --- |
| 0 | OK | done |
| 1 | WARNING: the database answers, some object or dataset failed | done, some parts failed |
| 2 | CRITICAL: not reachable, sign-in refused | nothing usable |
| 3 | UNKNOWN: settings or usage, no setup, or another install/uninstall running | the same |

Other codes come from Docker itself (125 to 127: the container could not start).
Keep stdout and stderr apart when a tool reads the JSON: a first run may print
pull progress on stderr.

A health check every 15 minutes, logging problems to the system journal
(`/etc/cron.d/rp-adapter-health`):

```bash
*/15 * * * * root bash /root/perodua-server-dependencies/scripts/rp-adapter.sh health --json > /var/log/rp-adapter-health.json 2>> /var/log/rp-adapter-health.err || logger -t rp-adapter "health exit $?"
```

A nightly export for another job to pick up (`/etc/cron.d/rp-adapter-export`):

```bash
30 2 * * * root bash /root/perodua-server-dependencies/scripts/rp-adapter.sh export --all --format jsonl >> /var/log/rp-adapter-export.log 2>&1
```

Health checks and exports may run at the same time; while `install` or
`uninstall` changes the setup, other commands stop at once with exit 3.

Each survey or export writes a new folder, named by UTC time, under
`output/survey/` or `output/export/`. The command prints the folder with the
results (`Results on this server: ...`) and `status` shows the latest; the
`manifest.json` there lists the files. Old folders are not deleted
automatically. A `--json` health result carries `overall`, `exit_code` and one
entry per check (`name`, `group`, `status`, `detail`).

If an install is stopped while it replaces the settings (a reboot, `kill -9`),
the other commands refuse to run with exit 3 until you run `install` again; it
first puts the previous settings and secrets back.

| Symptom | Cause |
| --- | --- |
| `login ... ORA-01017` | Wrong user or password: `install --config` with a new `DB_PASSWORD_FILE` |
| `tcp ... not reachable` | No network path to the database port: firewall, VPN, or a wrong `DB_HOST` / `DB_PORT` |
| An object `is not visible to this account` | The user lacks the synonym or the `SELECT` grant on it |
| `Oracle call timed out` | A query ran longer than `CALL_TIMEOUT` seconds (`rp-adapter.env`); raise it for `--exact` on large views |

## HTTPS

`https.sh` puts nginx with HTTPS in front of the services of one server, when the
host names point at that server directly (A records: nginx listens on IPv4 only).
One table, `/etc/perodua-https/routes.conf`, lists the server's host names, paths
and local ports; the certificate request (CSR), the certificate check and the
nginx configuration all come from it. Run the commands from the `scripts` folder.

### First time

1. **The table.** The first run creates it from
   [https-routes.conf.example](scripts/https-routes.conf.example) and stops. Fill
   it in:

   ```bash
   sudo bash https.sh csr
   sudo nano /etc/perodua-https/routes.conf
   ```

   One line per host name and path:

   ```
   # HOST                          PATH        PORT   OPTION
   stgissrp.perodua.com.my         /dev/       8110
   stgissrp.perodua.com.my         /uat/       8111
   api.example.perodua.com.my      /dev/api/   8000   strip
   ```

   Paths are forwarded as they are; `strip` removes the path first, for a service
   that expects `/`, such as the ISS-Oracle API published under `/dev/api/`. A port
   that is not known yet can be `-`: the host name is in the certificate, and nginx
   answers 404 for it until the port is set and `apply` runs again (the template
   has WOM and TMS like that). Put every host name that needs HTTPS in the table
   now: the certificate is requested for exactly these names.
2. **The certificate request (CSR).**

   ```bash
   sudo bash https.sh csr
   ```

   It makes the private key `/etc/perodua-https/key.pem` (RSA 2048) and the request
   `/etc/perodua-https/request.csr` for every host name of the table (the first
   one is the CN, all of them are subject alternative names), and prints the
   request. Send only the request to the certificate issuer, never the key;
   `sudo cat /etc/perodua-https/request.csr` prints it again. The subject is the one GICT
   asks for (`/C=MY/ST=Selangor/L=Rawang/O=Perusahaan Otomobil Kedua Sdn Bhd/OU=GICT`);
   `--subject` sets another one, without the CN.

   If a request made by hand, with its own key, was already sent, do not send
   another: take that key over, so that the certificate the issuer returns fits it
   (an encrypted key asks for its pass phrase once: nginx needs it unencrypted):

   ```bash
   sudo bash https.sh csr --key /path/to/that.key
   ```
3. **The certificate.** Copy what the issuer returns to the server and install it,
   with the issuer's chain as a second file if it came separately:

   ```bash
   sudo bash https.sh install-cert certificate.cer chain.p7b
   ```

   PEM, DER and PKCS #7 (`.p7b`, also as Microsoft CAs label it) are read. The
   certificate must belong to the key, cover every host name of the table, be
   valid now and verify with its chain.

   **Or from Let's Encrypt with records by hand**, instead of steps 2 and 3, when
   there is no acme-dns server: the DNS administrator adds one TXT record per host
   name in the domain's public DNS for each certificate. The first run prints all
   of them and stops:

   ```bash
   sudo bash https.sh letsencrypt --manual --accept-tos
   ```

   ```
   _acme-challenge.stgiss.perodua.com.my TXT "rgTepGogD-f1UpXVImshaGXVoJ6X4KLkT8kzuZbYXzg"
   ```

   Once they are in public DNS, run the same command again: it looks them up with
   public DNS servers (8.8.8.8, 1.1.1.1, or `--dns-resolvers`), then has Let's
   Encrypt check them and installs the certificate (valid 90 days). `--no-dns-check`
   skips that look-up when this server cannot reach public DNS. There is no timer:
   to renew, run the same command again in the last 30 days; it prints new records.
   The server needs Docker and outbound HTTPS to Let's Encrypt and Docker Hub
   (acme.sh image); the private key stays on the server.

   **Or from Let's Encrypt**, instead of steps 2 and 3: `letsencrypt` gets the
   certificate for the same table, key and request, and a daily timer renews it.
   Let's Encrypt checks each host name through a DNS record that the customer's
   DNS administrator creates once. The first run prints these records and stops:

   ```bash
   sudo bash https.sh letsencrypt --accept-tos
   ```

   ```
   _acme-challenge.stgissrp.perodua.com.my CNAME 3f0c7a1e-5b2d-4c3e-9a8f-0d1e2f3a4b5c.acme.novutal.com
   ```

   Once the records exist, run the same command again. The server needs Docker,
   `dig` and outbound HTTPS to Let's Encrypt and `acmedns.novutal.com`. The private key
   stays on the server; the host names become public in the Certificate
   Transparency logs. See
   [DEPLOYMENT.md](docs/DEPLOYMENT.md#https-certificate-from-lets-encrypt).
4. **nginx.** With every port filled in:

   ```bash
   sudo bash https.sh apply
   sudo bash https.sh status
   ```

   `apply` installs nginx if needed, with apt-get: the server needs the Ubuntu
   package mirrors, or an apt proxy. It then writes `/etc/nginx/conf.d/perodua-https.conf`:
   port 80 redirects to HTTPS, and on 443 each host forwards its paths to
   `127.0.0.1:PORT` and answers 404 for any other path. The services get the host
   name, `X-Forwarded-Proto: https` and the caller's address in `X-Forwarded-For`
   and `X-Real-IP`, replacing whatever the caller sent. Ports 80 and 443 must be
   free for nginx: another program there, or an nginx of another installation
   (one that is not on the PATH, such as a build in `/usr/local/nginx`), stops
   `apply` before anything changes. `apply` also refuses host names that another
   nginx file serves, such as a copy of the HTTP-only
   `docs/front-proxy.example.conf`: remove that file first. The change is kept only
   if `nginx -t` accepts it and nginx then runs it; otherwise the previous
   configuration comes back. If ufw is active, allow ports 80 and 443 (`apply`
   prints the command). `status` shows the certificate, its expiry, whether nginx
   is installed (if not, `apply` installs it), whatever would stop `apply` on ports
   80 and 443, and every route.

Behind HTTPS, the App's `PUBLIC_BASE_URL` starts with `https://`, and every
service listens on `127.0.0.1` only (`BIND_IP=127.0.0.1`), so that browsers reach
it through nginx ([DEPLOYMENT.md](docs/DEPLOYMENT.md)).

### Later

Each is `sudo bash https.sh COMMAND` from the `scripts` folder; `--help` lists them all.

| To | Run |
|---|---|
| Renew the certificate before it expires | `csr` (it keeps the key), have the request signed, `install-cert FILE [CHAIN]`. nginx is reloaded and must serve the new certificate, or the previous one comes back. |
| Renew a certificate from Let's Encrypt | Nothing: `perodua-https-renew.timer` renews it when fewer than 30 days are left. After downloading a newer `scripts` folder, run `letsencrypt --renew` from it, which updates the copy of the script that the timer runs. |
| Change to a new key | `csr --new-key`, have the request signed, `install-cert FILE [CHAIN]` (or `letsencrypt`). nginx keeps the old key until then. |
| Change a path or port | Edit the table, then `apply`. |
| Add a host name | Add it to the table, `csr`, have the request signed, `install-cert FILE [CHAIN]`, then `apply`. With Let's Encrypt: add it to the table, `letsencrypt` (it prints the new record), have the record created, `letsencrypt` again, then `apply`. |
| Go back from Let's Encrypt to the issuer | `csr`, have the request signed, `install-cert FILE [CHAIN]`. The timer leaves that certificate alone. |
| Remove HTTPS | `uninstall --confirm yes`, which also removes the Let's Encrypt timer. With `--purge` it also deletes the key, certificate, request and table, and the Let's Encrypt accounts and certificates. |

[Self-check](docs/RUNBOOK.md) · [Reference](docs/DEPLOYMENT.md) · [Tests](tests/README.md)
