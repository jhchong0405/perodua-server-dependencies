# Server setup

Client Stable UIUX v1.2.0 on Ubuntu 24.04 amd64 servers with sudo and internet access:

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
An App server that runs v1.0.5, v1.0.6, v1.0.7 or v1.0.8 keeps its data
through a module upgrade: see
[Upgrade an App server to v1.2.0](#upgrade-an-app-server-to-v120).
A server on v1.0.3 or v1.0.4 must first move to v1.0.8, with the `scripts`
folder of kit commit `8867545` (that folder pins v1.0.8): see
[From v1.0.3 or v1.0.4](#from-v103-or-v104).
A system set up with v1.0.0 to v1.0.2 cannot keep its data: download this
version on the App server and run a [reset](#stop-start-and-reset) there. The DB
server needs no step.

## What v1.2.0 changes

v1.2.0 (revision `09d8c712`) changes the Odoo modules, the menus and the web
page. Its modules are not those of v1.0.3 to v1.0.8, so
`deploy-app.sh --upgrade` runs a **module upgrade** (`-u`) on the database,
after a backup of the database and the attachments. The steps are in
[Upgrade an App server to v1.2.0](#upgrade-an-app-server-to-v120).

- **Four workspaces,** from the menu arrangement of Outcome 0930: RESOURCES
  PLANNING (MASTER), RESOURCES PLANNING (OPERATION), CUSTOMER PORTAL and
  SUPPLIER PORTAL. They take the place of the three modules of v1.0.8
  (Perodua SPD, Customer Portal, Supplier Portal).
- **What each host name shows:**

  | Host name | Shows |
  | --- | --- |
  | `stgissrp.perodua.com.my` | Master and Operation (the two RESOURCES PLANNING workspaces) |
  | `stgisssp.perodua.com.my` | Supplier Portal |
  | `stgisscp.perodua.com.my` | Customer Portal |
  | `stgiss.perodua.com.my` | The sign-in only, then a card for each workspace |
  | Any other name, such as `127.0.0.1` or `localhost` (SSH tunnel) | All four |

  The host names, the sign-in hand-over, `PUBLIC_BASE_URL` and the HTTPS
  routes stay as in v1.0.5 to v1.0.8. The host table
  (`perodua_client_stable.hosts`) keeps its codes `rp`, `sp` and `cp`; `rp`
  now names the two RESOURCES PLANNING workspaces.
- **The Odoo modules change.** Every `perodua_*` module of the release has a
  new version. Three modules are retired, and the upgrade uninstalls them:
  `perodua_hw_sim`, `perodua_supplier_transport_ext` and
  `perodua_warehouse_ext`. The upgrade deletes the data of the retired modules
  and pages only with `--drop-retired-data`, after the owner agreed. After the
  upgrade, v1.0.8 and earlier cannot run on the database: the only way back is
  the backup, before users write data with v1.2.0.
- **Only from v1.0.5 to v1.0.8.** A server on v1.0.3 or v1.0.4 must first move
  to v1.0.8, with the `scripts` folder of kit commit `8867545`: see
  [From v1.0.3 or v1.0.4](#from-v103-or-v104).
- **Owner decisions that the upgrade applies:**
  - **ECC purchase orders (decision D10 = A, accepted).** When the IDDIs of an
    ECC purchase order already received its goods, the upgrade cancels the
    open moves of the second receipt that the order raised. A validated
    receipt stays as it is. When the database has ECC purchase orders,
    `module-upgrade.log` has a line that starts with `perodua_rp 1.21:`.
  - **Personas (decision D8 = A).** `planner` gets the group Non-OEM BOM
    Maintainer. `sop` gets the groups Geographical Zone Maintainer and RPPC
    DIO Publisher. No persona is archived or deleted.
- **Two order types are back.** v1.2.0 has the order types Export Order and
  IHC / Engineering Order: pages of the workspaces use them. A fresh UAT
  system starts with them, and the upgrade makes them again on a database
  where an earlier release removed them.
- **The mock pulls stay off.** v1.2.0 has four scheduled pulls that read a
  mock feed (PROMISE, PSS, PSOS and P-Circle). On a system without sample data
  they are off while their system is in mock mode, after `--init-db` and after
  the upgrade.
- **The upgrade holds the mail queue.** After the module upgrade, every mail
  that waits in the mail queue (state Outgoing) is held: the mail that the
  upgrade queued, and the mail that waited in the queue before the upgrade.
  Odoo sends none of it until you run `deploy-app.sh --release-queued-mail`,
  after the owner's review.

A fresh UAT system is set up as before, with `deploy-app.sh --init-db`.

## What v1.0.8 changes

v1.0.8 (revision `0a36b810`) changes one controller and the web page. No
module version changed: the Odoo modules are the same as in v1.0.4 to v1.0.7,
so `deploy-app.sh --upgrade` keeps the data and runs no module upgrade.

- **The launcher of stgiss looks as before v1.0.5.** Each module is a card
  with its icon, its name and its line of description, without the host name,
  in the order Perodua SPD, Customer Portal, Supplier Portal. A module that the
  user may not open shows "No permission".
- **The left rail lists every portal that the user may open,** on stgiss and
  on each module name. The current portal is marked. A click on another
  portal opens it on its own name, signed in through the hand-over.

## What v1.0.7 changes

v1.0.7 (revision `4cbd9791`) changes the web page only. The Odoo modules are
the same as in v1.0.4 to v1.0.6, so `deploy-app.sh --upgrade` keeps the data
and runs no module upgrade.

- **A module name opens its workspace directly.** After the sign-in
  hand-over, `stgissrp`, `stgisssp` and `stgisscp` open the first page of their
  workspace, not a "Workspaces" page with one card. When the user may not open
  that workspace, the "Workspaces" page stays and says so.
- **One launcher, on stgiss.** On a module name, the "Workspaces" button of
  the left rail goes to the launcher of `stgiss`, which lists the modules.

## What v1.0.6 changes

v1.0.6 (revision `32600679`) corrects one defect of v1.0.5 in the web page:
after the sign-in hand-over, every page of `stgissrp`, `stgisssp` and
`stgisscp` showed "Something went wrong" (the browser console showed
`TypeError: Illegal invocation`). The Odoo modules are the same as in v1.0.4
and v1.0.5, so `deploy-app.sh --upgrade` keeps the data and runs no module
upgrade. A server that runs v1.0.5 must move to v1.0.6.

## What v1.0.5 changes

v1.0.5 (revision `bd9848e4`) changes the sign-in and the web page. The Odoo
modules are the same as in v1.0.4: no module version changed, so
`deploy-app.sh --upgrade` keeps the data and runs no module upgrade.

- **Each host name shows only its own workspace:**

  | Host name | Shows |
  | --- | --- |
  | `stgissrp.perodua.com.my` | Perodua SPD (Resource Planning) |
  | `stgisssp.perodua.com.my` | Supplier Portal |
  | `stgisscp.perodua.com.my` | Customer Portal |
  | `stgiss.perodua.com.my` | The sign-in only, then links to the user's modules |
  | Any other name, such as `127.0.0.1` or `localhost` (SSH tunnel) | All three |

- **Sign in once:** a module name sends the user to stgiss to sign in, then
  back, signed in, with a one-time ticket.
- **One sign-out ends every name.** A new password sign-in of the same user
  elsewhere also ends them: one sign-in per user, as before.
- **The hand-over needs `PUBLIC_BASE_URL` on the sign-in host:**
  `https://stgiss.perodua.com.my/dev` in the grey environment (`https://`, the
  sign-in host, no port other than 443). Otherwise each name keeps its own
  password sign-in, as in v1.0.4, and `deploy-app.sh` prints a warning.
- **The host table** is the Odoo system parameter
  `perodua_client_stable.hosts` (JSON). Change it in Odoo; no new release is
  necessary.

This `scripts` folder also adds `deploy-app.sh --upgrade`, and `https.sh apply`
now marks the session cookie `Secure`. See [Sign in once](#sign-in-once-v105).

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
the settings. `PUBLIC_BASE_URL` names the sign-in host `stgiss`: from v1.0.5 the
other host names hand their sign-in over to it ([Sign in once](#sign-in-once-v105)).

```
HTTP_PORT=8110
BIND_IP=127.0.0.1
PUBLIC_ROOT=/dev
ENVIRONMENT_LABEL=DEV environment
PUBLIC_BASE_URL=https://stgiss.perodua.com.my/dev
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
suppliers, orders or EBS/PROMISE register rows, and no customer types, holiday
types, order cycles, payment terms or price lists. From v1.2.0 the only order
types are Export Order and IHC / Engineering Order; MYR is the only currency.
Only the company (Perodua Parts Sdn Bhd) and the HQ warehouse are
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
To move to a newer release and keep the data, see
[Upgrade to a new release](#upgrade-to-a-new-release).

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

## Sign in once (v1.0.5)

From v1.0.5, users sign in one time, on `stgiss.perodua.com.my`. The module host
names hand that sign-in over: `stgissrp` (Perodua SPD; from v1.2.0 the two
RESOURCES PLANNING workspaces, Master and Operation), `stgisssp` (Supplier
Portal) and `stgisscp` (Customer Portal). Each name shows only its own module.

- **Prerequisite:** `PUBLIC_BASE_URL` in `app.env` is the `https://` address of
  the sign-in host. For the grey environment (`/dev`) that is
  `PUBLIC_BASE_URL=https://stgiss.perodua.com.my/dev`. With an empty value, an
  `http://` value, another host name or a port other than 443, the hand-over
  stays off: each name keeps its own password sign-in, as in v1.0.4, and
  `deploy-app.sh` prints a warning.
  After you change the value, run `deploy-app.sh` again (`--upgrade` when you
  move to a new release).
- **Sign in at stgiss:** `https://stgiss.perodua.com.my/dev/app/`. After the
  sign-in, stgiss shows no module, but links to the modules the user may open.
- **Module names send users to stgiss:** a module name without a sign-in sends
  the browser to stgiss, and back after the sign-in. A password sign-in on a
  module name is refused.
- **One sign-out ends all names:** a sign-out on any name ends the sign-in on
  stgiss and on every module name. A new sign-in of the same user, on another
  computer or browser, also ends it.
- **Support:** sign in at stgiss, or through the SSH tunnel
  (`http://localhost:8110/dev/app/`). The tunnel keeps the password sign-in and
  shows all modules.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#sign-in-once-on-stgiss-v105).

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
(MYR), 2 for Order Types (Export Order and IHC / Engineering Order, from
v1.2.0) and 0 for everything else.

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

## Upgrade to a new release

An upgrade keeps the data. For the move to v1.2.0, follow
[the steps below](#upgrade-an-app-server-to-v120). Put the `scripts`
folder of the new release next to the old one, and keep the old one. Then run
this on the App server, from the new folder:

```bash
sudo bash deploy-app.sh --upgrade
```

Add `--dir PATH` for a directory other than `/opt/perodua-app`. The settings
come from `app.env` there. The upgrade goes ahead only when:

- the directory runs the same project and the same database (server, port,
  name and user). Its `.deployment-identity` differs from the new one only in
  the release;
- the new release has the same Odoo modules (the database check with the new
  image reports `READY` with the same module fingerprint), or the release
  upgrades the modules of this database. See
  [Module upgrade](#module-upgrade--u). v1.0.3 to v1.0.8 have the same
  modules, so no module upgrade runs between them. v1.2.0 has other modules:
  the move to it runs a module upgrade.

Then it stops the App and saves the database (`pg_dump -Fc`) and the
attachments in `/opt/perodua-app/backups/TIME-OLD_RELEASE/` (or in a folder
that you give with `--backup-dir PATH`). If the backup fails, it starts the old
App again and stops. Then it deploys the new release with the usual checks.
`restore.txt` in the backup folder gives the commands that put the data back.

If it stops before the switch, the directory and its App stay as they were. If
it stops later, it tells you the state of the server and the ways back:

- To go back to the old release with the current data, copy
  `deployment-identity` and `app.env` from the backup folder back into the
  directory (as `.deployment-identity` and `app.env`), then run
  `sudo bash deploy-app.sh` from the old release's `scripts` folder, without
  `--upgrade`. The message prints these commands.
- To also put back the data from before the upgrade, follow `restore.txt`
  instead. It also ends every sign-in: all users sign in again.

After a module upgrade, such as the move to v1.2.0, the first way does not
exist: the old release cannot run on the upgraded database, and `restore.txt`
is the only way back. See [Module upgrade](#module-upgrade--u).

Before it stops the App, `--upgrade` checks that the backup folder's disk has
room for the size of the database and of the attachments, and stops if not.
The App is down while the backup runs, so run it in `tmux` or `screen`: a lost
SSH session stops the upgrade (before the switch, the old App starts again).

Uninstall deletes `/opt/perodua-app` and the backups in it: copy them elsewhere
first. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#upgrade-to-a-new-release-keeping-the-data-deploy-appsh---upgrade).

### Module upgrade (-u)

A release with other Odoo modules (from v1.2.0) upgrades the modules of the
database in the same `--upgrade`. It does this only when:

- the database has the modules of v1.0.3 to v1.0.8 (module fingerprint
  `3b62a97697d2974ef37328de7f3d034d`), and the directory runs v1.0.5, v1.0.6,
  v1.0.7 or v1.0.8;
- the database is a UAT database that `--init-db` set up, without sample data
  and without `perodua_demo_client`. A restored database is refused;
- the checks of [the module upgrade](docs/DEPLOYMENT.md#module-upgrade--u)
  find no other problem.

A module upgrade asks you to type the database name. With `--non-interactive`,
give it with `--confirm DATABASE`. When the retired modules left data (the
check lists it), add `--drop-retired-data` after the owner agreed: the rows are
saved as CSV in the backup folder (`retired-data.tar.gz`) before the upgrade
deletes them.

After the backup, with the App stopped, the upgrade checks the database
again: users could write while the first checks ran. Retired data written in
that time also needs `--drop-retired-data`. If this check refuses, the old App
starts again and the backup stays. Then the upgrade marks the database, removes
the containers of the old release and runs the module upgrade once. **From the
mark on, the old release cannot run on the database.** If the upgrade stops
after the mark, it does not start the old App again: the only way back is
`restore.txt` in the backup folder, all of its steps.

**Point of no return.** Go back with `restore.txt` only before users write data
with the new release. All data written after the backup is lost by the
restore. After that point, fix forward.

After the module upgrade, every mail that waits in the mail queue (state
Outgoing) is held: the mail that the upgrade queued, and the mail that waited
in the queue before it. Mail in another state stays as it is. Review the held
mail (step 13 below lists it without the web page; in Odoo it is in Settings >
Technical > Emails, with the status Delivery Failed), then send it:

```bash
sudo bash deploy-app.sh --release-queued-mail
```

## Upgrade an App server to v1.2.0

These steps move a server from v1.0.5, v1.0.6, v1.0.7 or v1.0.8 to v1.2.0.
v1.2.0 has other Odoo modules than these releases, so the upgrade runs a
**module upgrade** (`-u`): it saves the database and the attachments, upgrades
the modules of the database one time, checks the result and starts v1.2.0. The
data stays. Do the steps on the App server only; the DB server needs no step.
The commands use the deployment directory `/opt/perodua-app` and the database
name `perodua`: if yours are others, use them in their place.
[Upgrade to a new release](#upgrade-to-a-new-release) and
[Module upgrade](#module-upgrade--u) tell what `--upgrade` checks.

**Before you start.**

- **The release.** Show the release that the directory runs:

  ```bash
  sudo grep -E '^(release|revision)=' /opt/perodua-app/.deployment-identity
  ```

  It must show `release=client-stable-uiux-v1.0.5`, `-v1.0.6`, `-v1.0.7` or
  `-v1.0.8`. For v1.0.3 or v1.0.4, first do
  [From v1.0.3 or v1.0.4](#from-v103-or-v104).
- **The database.** The module upgrade takes only a UAT database that
  `deploy-app.sh --init-db` set up, without sample data. It refuses a restored
  database.
- **The registry account** for `perodua-deploy.novutal.com`: the upgrade can
  ask for it.
- **The owner's agreement.** The upgrade applies the two owner decisions in
  [What v1.2.0 changes](#what-v120-changes). When step 11 lists retired data,
  the owner must also agree that the upgrade deletes it.
- **A time when nobody works in the system.** The App is down during step 12:
  for the backup, the module upgrade and the start of v1.2.0. The backup time
  increases with the size of the database and of the attachments. The module
  upgrade loads every installed `perodua_*` module again; the script stops it
  after `INIT_TIMEOUT` seconds (3600, one hour, unless `app.env` has another
  value). Tell the users before you start.
- **The mail queue.** After the module upgrade, every mail that waits in the
  mail queue (state Outgoing) is held until the owner's review (step 13): the
  mail that the upgrade queues, and the mail that waits in the queue before
  the upgrade. v1.2.0 gives a waiting IDDI release mail that has no address
  the address of its supplier: this mail is held too. This counts the mail
  that waits now, on the DB server (with one server, on that server):

  ```bash
  sudo -u postgres psql -X -At -d perodua -c "SELECT count(*) FROM mail_mail WHERE state = 'outgoing'"
  ```

  When it is not 0, tell the owner before the upgrade: after the upgrade,
  Odoo does not send this mail until the owner releases it. The count can
  change until the App stops in step 12, because the old release works on
  its mail queue while it runs.
- **The way back is the backup.** After the upgrade, the release that ran
  before cannot run on the database. See "The way back" at the end.

**The new `scripts` folder.** Start in the folder that holds the old `setup`
folders. Keep them: the way back uses the folder of the release that runs now.

1. Start a `tmux` session, so that a lost SSH connection does not stop the
   upgrade (`screen` also works). After a lost connection, connect again and
   run `tmux attach -t upgrade`. If `tmux` is missing, first run
   `sudo apt-get install -y tmux`.

   ```bash
   tmux new -s upgrade
   ```

2. Download the new release:

   ```bash
   curl -fL https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/main -o setup-v1.2.0.tar.gz
   ```

3. Make a new folder for it, next to the old ones:

   ```bash
   mkdir setup-v1.2.0
   ```

4. Unpack the release into the new folder:

   ```bash
   tar -xzf setup-v1.2.0.tar.gz -C setup-v1.2.0 --strip-components=1
   ```

5. Go to its `scripts` folder. All the commands below run there.

   ```bash
   cd setup-v1.2.0/scripts
   ```

6. Make sure that the folder pins v1.2.0:

   ```bash
   grep -E '^(RELEASE|REVISION)=' deploy-app.sh
   ```

   It shows `RELEASE=client-stable-uiux-v1.2.0` and
   `REVISION=09d8c712f27c29facaad53c0a34de7df78e0b3f2`. If it shows another
   release, stop: these steps are for v1.2.0.

With git, in place of steps 2 to 5:
`git clone https://github.com/jhchong0405/perodua-server-dependencies.git perodua-v1.2.0`,
then `cd perodua-v1.2.0/scripts`. Do not run `git pull` in an old folder.

**The settings and HTTPS.** v1.2.0 uses the settings and the four host names
of v1.0.5 to v1.0.8. These steps make sure that they are still correct.

7. Show the address that browsers use:

   ```bash
   sudo grep -E '^(PUBLIC_ROOT|PUBLIC_BASE_URL)=' /opt/perodua-app/app.env
   ```

   The grey environment shows `PUBLIC_ROOT=/dev` and
   `PUBLIC_BASE_URL=https://stgiss.perodua.com.my/dev`. If `PUBLIC_BASE_URL`
   is missing or has another value, edit the file
   (`sudo nano /opt/perodua-app/app.env`) and set
   `PUBLIC_BASE_URL=https://stgiss.perodua.com.my/dev`. Its path must be the
   same as `PUBLIC_ROOT`. Keep another value only if the hand-over must stay
   off, for example without HTTPS: then each host name keeps its own password
   sign-in.

8. Check the settings with the new scripts. This changes nothing:

   ```bash
   sudo bash deploy-app.sh --check-config --dir /opt/perodua-app
   ```

   It shows `Configuration valid for client-stable-uiux-v1.2.0. Nothing was changed.`
   A line `Warning: PUBLIC_BASE_URL ...` tells you that the hand-over stays
   off, and why. Correct the value (step 7) and do this step again.

9. Show the HTTPS routes and the certificate. This changes nothing:

   ```bash
   sudo bash https.sh status
   ```

   The routes must send all four names, `stgiss`, `stgissrp`, `stgisssp` and
   `stgisscp` (`.perodua.com.my`), with the path `/dev/`, to the App's port
   (`8110`), each with `port listening: yes`. The certificate must cover them:
   there is no `does NOT cover` line. v1.2.0 needs no other route. If a name
   is missing, see [HTTPS](#https) first. The `nginx:` line can end with
   `(the routes or https.sh changed since: apply)`: the `https.sh` of this
   folder is newer than the one that wrote the configuration.

10. Only when step 9 showed `(the routes or https.sh changed since: apply)`:
    apply the HTTPS configuration of the new folder.

    ```bash
    sudo bash https.sh apply
    ```

    The routes stay as they are. This `https.sh` also makes nginx close the
    connection on port 443 for a host name that is not in the table and for
    the server's IP address. Before you apply, make sure that no monitor,
    health check or front calls port 443 with the IP address or with another
    name (see [HTTPS](#https)). The command ends with
    `nginx serves HTTPS for:` and the routes. From now on, run `https.sh` from
    the new folder.

**The upgrade.**

11. Start the upgrade, in the `tmux` session:

    ```bash
    sudo bash deploy-app.sh --upgrade --dir /opt/perodua-app
    ```

    When it asks, enter the registry username and password. The first part
    changes nothing, and the App keeps running. The script:

    - names the two releases
      (`Upgrade of /opt/perodua-app from client-stable-uiux-v1.0.8 (...) to client-stable-uiux-v1.2.0 (...)`)
      and pulls the v1.2.0 images;
    - checks the database with them. It shows
      `Database perodua has the modules of client-stable-uiux-v1.0.8 (fingerprint 3b62a97697d2974ef37328de7f3d034d). client-stable-uiux-v1.2.0 has other modules: this upgrade runs a module upgrade (-u) of 30 modules. N attachments.`
      The release is the one that runs now. A database that `--init-db` set
      up has 30 modules to upgrade;
    - counts the data of the retired modules. `Retired data: none.` is the
      usual answer. If it lists tables, columns or attachments with a count,
      it stops with `The module upgrade deletes the retired data above`: get
      the owner's agreement, then run the same command again with
      `--drop-retired-data` at the end. The rows are then saved as CSV in the
      backup folder (`retired-data.tar.gz`) before the upgrade deletes them;
    - checks the modules of v1.2.0. It shows
      `UAT guard: accepted: of at most N modules Odoo could install, none is perodua_demo_client and none relies on it`;
    - prints the plan of the module upgrade, with the point of no return, and
      asks `Type perodua to start the module upgrade:`.

    A line `Database preflight: module upgrade refused: ...` gives a reason
    why this database cannot take the module upgrade. A stop in this part
    ends with `Nothing was changed: /opt/perodua-app still runs client-stable-uiux-v1.0.8.`
    To stop at the question, press Enter without the name: the same line
    shows.

12. Type the database name (`perodua`) and press Enter. From here the App is
    down. The script:

    - checks that the disk has room for the backup, then stops the App;
    - saves the database (`database.dump`, `pg_dump -Fc`), the attachments
      (`filestore.tar.gz`), the old `deployment-identity` and `app.env`, and
      `restore.txt` in `/opt/perodua-app/backups/TIME-OLD_RELEASE/`, for
      example `TIME-client-stable-uiux-v1.0.8/`. It shows
      `Backup complete in ...` and prints `restore.txt`;
    - checks the database again, now that nothing writes to it
      (`Database perodua: the same modules to upgrade, and still no retired data.`).
      If this check refuses, the old App starts again and the backup stays;
    - marks the database and removes the containers of the old release
      (`Removing the containers of client-stable-uiux-v1.0.8 ...`). **From
      here on the old release cannot run on the database**, and the script
      does not start it again;
    - upgrades the modules one time
      (`Upgrading the Odoo modules of perodua (-u, at most INIT_TIMEOUT=3600 seconds).`).
      This is the long step. To follow it, open a second terminal and run
      `sudo tail -f` on the `module-upgrade.log` that the line names;
    - checks the result (`The upgraded database passed its checks.`). Before
      that, it gives each scheduled action the on/off flag that it had before
      the upgrade (`Cron jobs: N flags put back as before the upgrade.`), keeps
      the pulls that read a mock feed off
      (`Cron jobs: ... stays off: it reads the mock feed while its system resolves to mock.`),
      names the scheduled actions that are new, and holds every mail that
      waits in the mail queue
      (`Mail held (state exception) until deploy-app.sh --release-queued-mail: N that the upgrade queued, M that waited in the queue before it.`).
      Between the stop of the App and this hold, Odoo's mail queue does not
      run: the module upgrade runs no scheduled action;
    - writes the files of v1.2.0 into the directory, starts v1.2.0 and does
      the usual self-check.

    When the upgrade is successful, the output shows these lines, and no
    `Warning: PUBLIC_BASE_URL` line:

    ```
    Deployment verified: client-stable-uiux-v1.2.0 (09d8c712f27c29facaad53c0a34de7df78e0b3f2)
    Upgraded from client-stable-uiux-v1.0.8. The data as it was before: /opt/perodua-app/backups/TIME-client-stable-uiux-v1.0.8 (restore.txt explains how to put it back).
    The Odoo modules were upgraded (-u). client-stable-uiux-v1.0.8 cannot run on this database any more: the backup is the only way back, and only until users write data (the point of no return).
    Held mail: N that the module upgrade queued, M that waited in the queue before the upgrade.
    Review the held mail: README.md, "Upgrade an App server to v1.2.0", step 13 lists it with psql on the DB server (in Odoo: Settings > Technical > Emails, status Delivery Failed). Then send it with: sudo bash deploy-app.sh --release-queued-mail --dir /opt/perodua-app
    ```

    The second and third lines name the release that ran before. Write down
    the backup folder of the second line: the way back needs it.

    Without a terminal, add `--non-interactive --confirm perodua` to the
    command of step 11: it then does steps 11 and 12 in one run. Log in to the
    registry first (`sudo docker login perodua-deploy.novutal.com`). Without
    `--confirm`, a `--non-interactive` run does the first part and stops
    before the App stops (`A module upgrade needs a confirmation`).

13. The held mail. The line `Held mail:` has two numbers:

    - the mail that the module upgrade queued, such as the release email to
      the supplier of an IDDI that was released without one;
    - the mail that waited in the mail queue before the upgrade (state
      Outgoing), such as an IDDI release mail that had no address: the
      upgrade gives it the address of its supplier.

    When the two numbers are 0, this step is done. If not, the mail is held:
    Odoo does not send it. This lists it, on the DB server (with one server,
    on that server). The column `queued` shows `upgrade` for a mail that the
    upgrade queued, and `before` for a mail that waited before the upgrade:

    ```bash
    sudo -u postgres psql -X -P pager=off -d perodua -c "SELECT m.id, CASE WHEN m.id > (SELECT (value::json->>'mail_max_id')::bigint FROM ir_config_parameter WHERE key = 'perodua.kit_upgrade') THEN 'upgrade' ELSE 'before' END AS queued, g.subject, m.email_to FROM mail_mail m JOIN mail_message g ON g.id = m.mail_message_id WHERE m.state = 'exception' AND m.failure_reason LIKE 'Held by deploy-app.sh%' ORDER BY m.id"
    ```

    In Odoo, the same mail is in Settings > Technical > Emails, with the
    status Delivery Failed (the filter Failed). With `PUBLIC_ROOT` set, the
    public host names do not open Odoo's own pages: use the list above.

    Review the list with the owner. To send the mail, run this on the App
    server, from the new folder:

    ```bash
    sudo bash deploy-app.sh --release-queued-mail --dir /opt/perodua-app
    ```

    It shows `N held mail(s) are queued again (outgoing): A that the module upgrade queued, B that waited in the queue before the upgrade. Odoo sends them with its mail queue.`
    The command queues every held mail again, and no other mail. If the owner
    does not want the mail, do not run the command: the mail stays held.

    While the release mail of an IDDI is held, v1.2.0 shows the Release Email
    of that IDDI as Failed, with the text `Held by deploy-app.sh --upgrade ...`
    as its error (v1.2.0 reads the state of the mail every 15 minutes). Do
    not use **Retry Supplier Email** on such an IDDI before the release: it
    queues a new mail, which is not held, and the supplier then gets the mail
    again after the release.

**When the upgrade stops.** The last lines of the output tell the state of
the server:

| The output says | State | What to do |
| --- | --- | --- |
| `Nothing was changed: /opt/perodua-app still runs ...` | The App of the old release runs. No backup was made. | Solve the cause that the lines above give, then do step 11 again. |
| `The upgrade stopped before the switch: ... still runs ...`, then `The App of ... runs again.` | The App of the old release runs again. A complete backup stays (`The backup in ... is complete and kept.`). | Solve the cause, then do step 11 again. It makes a new backup. |
| `The module upgrade from ... to ... stopped ...`, then the text of `restore.txt` | The database carries the mark of the upgrade, or is partly upgraded. The containers of the old release are removed. No App runs. | Follow "The way back". Then solve the cause (`module-upgrade.log` in the backup folder has the log of `-u`) and do step 11 again. |
| `The module upgrade from ... to ... stopped after the switch.` | The modules are upgraded and checked. The directory names v1.2.0. The start or the self-check of v1.2.0 failed. | Solve the cause, then run `sudo bash deploy-app.sh --dir /opt/perodua-app` from the new folder, without `--upgrade`. It runs no module upgrade again. |

A lost SSH session or Ctrl-C stops the upgrade in the same way. If the module
upgrade stopped at `INIT_TIMEOUT`, follow "The way back", then set a higher
`INIT_TIMEOUT` in `/opt/perodua-app/app.env` (seconds, at most 65535) and do
step 11 again.

**Acceptance.** Use a browser that reaches the HTTPS names. A private window
does not use an earlier sign-in.

14. On the App server, both containers must be `healthy`:

    ```bash
    sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml ps
    ```

15. Open `https://stgiss.perodua.com.my/dev/app/` and sign in. stgiss shows a
    card for each of the four workspaces, with its icon, name and
    description: RESOURCES PLANNING (MASTER), RESOURCES PLANNING (OPERATION),
    CUSTOMER PORTAL and SUPPLIER PORTAL. A workspace that the user may not
    open shows "No permission". The left rail shows one icon for each
    workspace that the user may open.
16. Open each workspace with its card. The two RESOURCES PLANNING cards open
    `stgissrp`: its "Workspaces" page shows these two cards, and a click on
    one opens its first page. The SUPPLIER PORTAL card opens `stgisssp` and
    the CUSTOMER PORTAL card opens `stgisscp`, each directly on a page of its
    workspace. No name asks for the password again, and no page shows
    "Something went wrong". In each workspace, open some pages of the left
    menu: each one shows its list.
17. Open a record that was in the system before the upgrade, for example a
    customer or a part: its data is there.
18. Sign out on one name. Then reload the page of another name: it sends you
    to the sign-in on stgiss.
19. On the App server, check HTTPS again:

    ```bash
    sudo bash https.sh status
    ```

    Each of the four names shows `port listening: yes`. After step 10, the
    `nginx:` line does not end with
    `(the routes or https.sh changed since: apply)`.
20. Only when steps 14 to 19 are correct: as a user who is not an
    administrator, make a new record on one page and save it.

Step 20 writes data with v1.2.0: it is the point of no return. From then on,
fix forward. Keep the backup folders until v1.2.0 is accepted.

**The way back.** v1.0.5 to v1.0.8 cannot run on a database that carries the
mark of the upgrade or has the modules of v1.2.0. The only way back is to put
the backup back: the database and the attachments are then as they were when
the App stopped in step 12, and everything written after that is lost. Do it
only before users write data with v1.2.0. Do not copy only
`deployment-identity` and `app.env` back, as after an upgrade with the same
modules: the old release then refuses the database.

Follow `restore.txt` in the backup folder of step 12
(`/opt/perodua-app/backups/TIME-client-stable-uiux-v1.0.8/restore.txt`), all
of its steps, in order. It has the commands with the real paths:

1. Stop the App.
2. Empty the database. Run this command in a `scripts` folder:
   `reset_database.py` is there.
3. Restore `database.dump`, with the tools of the Odoo image as the command
   does. The `pg_restore` of the DB server cannot read the file.
4. Put the attachments back. This also ends every sign-in: all users must
   sign in again.
5. Copy `deployment-identity` and `app.env` from the backup folder back into
   the directory. Then go to the old `scripts` folder of the release that ran
   before (for v1.0.8, for example `../../setup-v1.0.8/scripts`) and deploy
   that release, without `--upgrade`:

   ```bash
   sudo bash deploy-app.sh --dir /opt/perodua-app
   ```

   It shows `Deployment verified:` with the old release.

The HTTPS configuration of step 10 can stay: v1.0.5 to v1.0.8 work with it.
When the cause is solved, do step 11 again from the folder of v1.2.0.

### From v1.0.3 or v1.0.4

v1.2.0 upgrades the modules only in a directory that runs v1.0.5 to v1.0.8. A
server on v1.0.3 or v1.0.4 first moves to v1.0.8, which has the same modules
and keeps the data, with the `scripts` folder of kit commit `8867545` (it pins
v1.0.8). Get that folder, next to the old `setup` folder:

```bash
curl -fL https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/886754540c8d6f3204e29b1960b49220c86da3e2 -o setup-v1.0.8.tar.gz
mkdir setup-v1.0.8
tar -xzf setup-v1.0.8.tar.gz -C setup-v1.0.8 --strip-components=1
```

With git: `git clone https://github.com/jhchong0405/perodua-server-dependencies.git perodua-v1.0.8`,
then `git -C perodua-v1.0.8 checkout 886754540c8d6f3204e29b1960b49220c86da3e2`.

Then follow "Upgrade an App server to v1.0.8" in the `README.md` of that
folder (`setup-v1.0.8/README.md`), without its steps 2 to 4: they download the
newest release, which is now v1.2.0. Its step 6 must show
`RELEASE=client-stable-uiux-v1.0.8`. When v1.0.8 runs and is accepted, do the
steps above for v1.2.0.

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

### Publish it with HTTPS

1. Install with the default **Only this server**: the API listens on
   `127.0.0.1:8000`, and only nginx on this server reaches it.
2. Add the route of the API to the table of [https.sh](#https),
   `/etc/perodua-https/routes.conf`. On the grey DEV server, where the
   customer's WAF connects to port 80:

   ```
   stgiss.perodua.com.my           /api/       8000   strip,http,api
   ```

   On a server without such a front, use `strip,api`. `strip` sends `/api/x` to
   the API as `/x`; `api` closes the documentation pages and limits the requests
   (see [An API route](#an-api-route)). First remove every nginx file that was
   written by hand for the API, such as one with its own default server for
   port 443 or its own rate limit: `apply` refuses the first, and the second
   adds a second limit.
3. Write the nginx configuration and check it:

   ```bash
   sudo bash https.sh apply
   sudo bash https.sh status
   ```

The calling systems then use `https://stgiss.perodua.com.my/api/...`:

- Use the exact paths. The seven `/accounting/...` list routes end with `/`, the
  other routes do not. With a wrong `/`, the API redirects to an address outside
  `/api/`, where this server answers 404.
- Do not follow redirects.
- Send the key in the `X-API-Key` header.

Behind the WAF, the request log records the WAF's address as `CLIENT_IP`, not
the address of the calling system.

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
   api.example.perodua.com.my      /dev/api/   8000   strip,api
   ```

   Paths are forwarded as they are; `strip` removes the path first, for a service
   that expects `/`, such as the ISS-Oracle API published under `/dev/api/`. A port
   that is not known yet can be `-`: the host name is in the certificate, and nginx
   answers 404 for it until the port is set and `apply` runs again (the template
   has WOM and TMS like that). `http` also serves the route on port 80, for a TLS
   front that connects to port 80: see
   [Behind a TLS front on port 80](#behind-a-tls-front-on-port-80). `api` closes
   the documentation pages of an API and limits its requests: see
   [An API route](#an-api-route). OPTION is one of these words, or several of them
   separated by commas without spaces, in any order, such as `strip,http,api`.
   Put every host name that needs HTTPS in the table now: the certificate is
   requested for exactly these names.
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
   port 80 redirects to HTTPS (except the routes with `http`), and on 443 each
   host forwards its paths to `127.0.0.1:PORT` and answers 404 for any other
   path. On 443, a host name that is not in the table, and the server's IP
   address, get the connection closed without an answer. A front that connects
   to 443 without the host name in TLS (SNI) still reaches the host name that it
   sends in the `Host` header. A front or a health check that connects to 443
   must send HTTP/1.1 with a host name of the table in the `Host` header: a
   request without `Host`, or with the IP address in it, also gets the
   connection closed. Earlier versions of `https.sh` answered these requests
   with the first host name of the table. On a server that already runs
   `https.sh`, make sure before `apply` that no monitor, health check or front
   calls port 443 with the IP address or another name. The services get the
   host name, `X-Forwarded-Proto: https` and the caller's address in
   `X-Forwarded-For` and `X-Real-IP`, replacing whatever the caller sent. The
   Odoo session cookie
   (`session_id`) gets `Secure` and `SameSite=Lax`, so browsers send it over HTTPS
   only, on every port. `apply` sends no `Strict-Transport-Security`: making these
   host names HTTPS-only in browsers for a long time is the customer's decision,
   and hard to undo. Ports 80 and 443 must be
   free for nginx: another program there, or an nginx of another installation
   (one that is not on the PATH, such as a build in `/usr/local/nginx`), stops
   `apply` before anything changes. `apply` also refuses host names that another
   nginx file serves, such as a copy of the HTTP-only
   `docs/front-proxy.example.conf`: remove that file first. It also refuses
   another nginx file that is already the default server for port 443 (a line
   such as `listen 443 ssl default_server;`), because the block that closes the
   unknown host names must be that default server: remove `default_server` from
   that line, or the file, then run `apply` again. The change is kept only
   if `nginx -t` accepts it and nginx then runs it; otherwise the previous
   configuration comes back. If ufw is active, allow ports 80 and 443 (`apply`
   prints the command). `status` shows the certificate, its expiry, whether nginx
   is installed (if not, `apply` installs it), whatever would stop `apply` on ports
   80 and 443, and every route (a route with `http` also with its answer on port
   80, a route with `api` with `API: docs closed, rate limited`).

Behind HTTPS, the App's `PUBLIC_BASE_URL` starts with `https://` (in the grey
environment `https://stgiss.perodua.com.my/dev`, see
[Sign in once](#sign-in-once-v105)), and every service listens on `127.0.0.1`
only (`BIND_IP=127.0.0.1`), so that browsers reach it through nginx
([DEPLOYMENT.md](docs/DEPLOYMENT.md)).

### Behind a TLS front on port 80

Some fronts take the browsers' HTTPS themselves and connect to this server over
plain HTTP on port 80, such as a WAF (web application firewall) or F5. Port 80
normally redirects to HTTPS, so the front gets the redirect again and again, and
the browser shows `ERR_TOO_MANY_REDIRECTS`. For such a front, add the option
`http` to the routes it uses (with `strip`: `strip,http`), then run `apply`:

```
# HOST                          PATH        PORT   OPTION
stgiss.perodua.com.my           /dev/       8110   http
api.example.perodua.com.my      /dev/api/   8000   strip,http
```

A host name with an `http` route gets its own server block on port 80. There,
nginx forwards each `http` route as on 443: with the same headers (also
`X-Forwarded-Proto: https`, because the browser used HTTPS to the front), the
same `Secure` session cookie and the same limits. A route of that host name
without `http` still redirects to HTTPS, also one under a route with it (such as
`/dev/api/` under `/dev/`). Behind the front, such a route loops, so give `http`
to every route that the front sends to port 80. Every other path answers 404, as
on 443. Host names without an `http` route and port 443 do not change. `apply` marks these routes,
and `status` shows their answer on port 80. If the Let's Encrypt renewal timer
is installed, `apply` also updates the copy of `https.sh` that the timer runs:
an older copy refuses `http`, and then the renewal fails.

Know these risks before you use `http`:

- Port 80 serves these routes, without encryption, to anybody who can reach it.
  `https.sh` does not check the address of the caller. If only the front must
  connect, let only the front's addresses reach port 80 (a firewall or a cloud
  security group; if ufw is active, `apply` prints the command).
- The connection from the front to this server is not encrypted. Use `http`
  only on a network that you trust.
- A user who types `http://` and reaches this server directly is not
  redirected to HTTPS on these paths.
- The front must redirect `http://` to `https://` itself, or take HTTPS only.
  If it forwards the browsers' plain HTTP to port 80, this server cannot tell
  those requests from HTTPS ones and serves them without a redirect. The
  browser then keeps no `Secure` cookie, and the sign-in fails without an error
  message.
- The front must send the browser's host name (the `Host` header). The services
  see the front's address as the caller (`X-Forwarded-For`, `X-Real-IP`), not
  the browser's.

Do not use `http` when browsers connect to this server directly: then port 80
must redirect them to HTTPS.

### An API route

Add the option `api` to the route of an API, such as the
[ISS-Oracle API](#iss-oracle-api), then run `apply`:

```
api.example.perodua.com.my      /dev/api/   8000   strip,api
```

On such a route, on 443 and (with `http`) on port 80:

- PATH followed by `docs`, `redoc` or `openapi.json` (here `/dev/api/docs`,
  `/dev/api/redoc` and `/dev/api/openapi.json`) answers 404. A FastAPI service,
  such as the ISS-Oracle API, shows its documentation there, with every route,
  to callers without a key.
- Each caller address can send at most 10 requests a second, with bursts of
  20. Above that, nginx answers 429 (Too Many Requests) and does not forward
  the request. The limit is for all `api` routes of this server together, on
  443 and on port 80. Every request to the ISS-Oracle API, also one without a
  key, opens a database session and writes a row in the request log.
- Behind a front (a WAF or F5), nginx sees the front's address as the caller.
  The limit is then for each front node, not for each calling system.

The numbers are `API_RATE` (requests a second, such as `10r/s`) and
`API_BURST` at the top of `https.sh`. If you change them, also change them in
this section and in `https-routes.conf.example`. Routes without `api` do not
change. `apply` marks the `api` routes, and `status` shows
`API: docs closed, rate limited` on them. As with `http`, `apply` also updates
the copy of `https.sh` that the Let's Encrypt renewal timer runs: an older copy
refuses `api`.

### Later

Each is `sudo bash https.sh COMMAND` from the `scripts` folder; `--help` lists them all.

| To | Run |
|---|---|
| Renew the certificate before it expires | `csr` (it keeps the key), have the request signed, `install-cert FILE [CHAIN]`. nginx is reloaded and must serve the new certificate, or the previous one comes back. |
| Renew a certificate from Let's Encrypt | Nothing: `perodua-https-renew.timer` renews it when fewer than 30 days are left. After downloading a newer `scripts` folder, run `letsencrypt --renew` from it, which updates the copy of the script that the timer runs (`apply` from it also updates that copy). |
| Change to a new key | `csr --new-key`, have the request signed, `install-cert FILE [CHAIN]` (or `letsencrypt`). nginx keeps the old key until then. |
| Change a path or port | Edit the table, then `apply`. |
| Add a host name | Add it to the table, `csr`, have the request signed, `install-cert FILE [CHAIN]`, then `apply`. With Let's Encrypt: add it to the table, `letsencrypt` (it prints the new record), have the record created, `letsencrypt` again, then `apply`. |
| Go back from Let's Encrypt to the issuer | `csr`, have the request signed, `install-cert FILE [CHAIN]`. The timer leaves that certificate alone. |
| Remove HTTPS | `uninstall --confirm yes`, which also removes the Let's Encrypt timer. With `--purge` it also deletes the key, certificate, request and table, and the Let's Encrypt accounts and certificates. |

[Self-check](docs/RUNBOOK.md) · [Reference](docs/DEPLOYMENT.md) · [Tests](tests/README.md)
