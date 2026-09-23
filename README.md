# Two-server setup

Client Stable UIUX v1.0.0 on two Ubuntu 24.04 amd64 servers with sudo and internet access:

- **DB server**: PostgreSQL 16.
- **App server**: Odoo and web containers, pulled from `perodua-deploy.novutal.com`.

Allow **App → DB port 5432** and **browser → App port 8110**.

The steps below create a **fresh UAT system** with an empty database.
To restore an existing database instead, see [From a backup](#from-a-backup).

## 1. Download (both servers)

```bash
curl -fL https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/main -o setup.tar.gz
mkdir -p setup && tar -xzf setup.tar.gz -C setup --strip-components=1 && cd setup
```

Standard Ubuntu servers include `curl`. On a minimal image without it, first run
`sudo apt-get update && sudo apt-get install -y curl ca-certificates`.

## 2. DB server

```bash
sudo bash install-dependencies.sh --role db
sudo bash deploy-db.sh
```

The script asks:

1. What to set up: press Enter for a fresh UAT system.
2. This server's internal IP: it lists the addresses it found; press Enter to use the first.
3. The App server's IP.
4. A new database password (at least 12 characters), twice. Keep it for the App server.

Wait for `SUCCESS`. A fresh UAT system needs no backup settings. The answers
are saved in `deploy.conf` and reused by later runs.

## 3. App server

```bash
sudo bash install-dependencies.sh --role app
sudo systemctl enable --now docker
sudo bash deploy-app.sh --init-db
```

Enter the DB server's IP. Press Enter to keep the defaults for the database port,
name and user and the web port (8110), then enter the database password. When
asked, enter the registry username and password for `perodua-deploy.novutal.com`.
The first run takes several minutes.

## 4. Sign in and check

Open `http://APP_SERVER_IP:8110/app/` (or the web port you chose).

| Login | Password | Access |
| --- | --- | --- |
| `whadmin`, `admin1`, `admin2` | `perodua` | Administrator |
| `planner`, `whouse`, `sop`, `op`, `finance` | `perodua` | Business role |

These accounts are for UAT only. Do not use this setup for production or expose it
to the internet. The database includes the release's sample records (products,
partners, orders); the client dataset and Odoo demo data are not installed.

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml ps
```

`web` and `odoo` should both be `healthy`. Running `deploy-app.sh` again keeps the
database and passwords and does not reinstall or upgrade modules. If the first run
stops part-way, see [DEPLOYMENT.md](DEPLOYMENT.md#initialize-a-fresh-uat-system-on-the-app-server).

## From a backup

You need a `database.dump` (`pg_dump -Fc`) with its SHA-256 and original locales,
and the matching `filestore.tar.gz` (`filestore/DB_NAME/...`). Use the same
database name on both servers.

- **DB server:** copy `database.dump` into `setup/` and `chmod 600` it. Before
  running `deploy-db.sh`, run `cp deploy.conf.example deploy.conf && chmod 600 deploy.conf`
  and edit it: keep `DB_MODE=restore`, set `BACKUP_FILE`, `BACKUP_SHA256`,
  `DB_LC_COLLATE`, `DB_LC_CTYPE`, `DB_LISTEN_IP` (this server's internal IP) and
  `APP_CIDR` (the App server's `IP/32`). With a `deploy.conf` present the script
  asks no setup questions. For `en_US.utf8` locales, first run
  `sudo localedef -i en_US -f UTF-8 en_US.utf8`.
- **App server:** copy `filestore.tar.gz` into `setup/`, `chmod 600` it and run
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
Services start again after a reboot. This setup serves HTTP only; add HTTPS before production.

[Self-check](RUNBOOK.md) · [Reference](DEPLOYMENT.md) · [Tests](tests/README.md)
