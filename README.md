# Server setup

Client Stable UIUX v1.0.3 on Ubuntu 24.04 amd64 servers with sudo and internet access:

- **DB server**: PostgreSQL 16.
- **App server**: Odoo and web containers, pulled from `perodua-deploy.novutal.com`.

Both can also run on one server: do steps 2 and 3 on that server. With two
servers, allow **App → DB port 5432**. Browsers need **App port 8110**.

The steps below create a **fresh UAT system** with an empty database.
To restore an existing database instead, see [From a backup](#from-a-backup).
A system set up with an earlier version (v1.0.0 to v1.0.2) cannot keep its data:
download this version on the App server and run a [reset](#stop-start-and-reset)
there. The DB server needs no step.

## Repository layout

| Path | Contents |
| --- | --- |
| `scripts/` | `install-dependencies.sh`, `deploy-db.sh`, `deploy-app.sh`, `service.sh`, `uninstall.sh`, the helpers they use (`uat_guard.py`, `uat_admins.py`, `reset_database.py`) and the configuration templates. Your `deploy.conf`, backups and filestore archive also go here. |
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
This setup serves HTTP only; add HTTPS before production.

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

[Self-check](docs/RUNBOOK.md) · [Reference](docs/DEPLOYMENT.md) · [Tests](tests/README.md)
