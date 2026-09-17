# Two-server setup

Two fresh **Ubuntu 24.04 amd64** servers with sudo and internet access.
App runs Web + Odoo; DB runs PostgreSQL 16. Release: **Client Stable UIUX v1.0.0**.

Before starting, get a matching `database.dump` (`pg_dump -Fc`), its SHA-256 and
original database name/locales, plus the matching `filestore.tar.gz` containing
`filestore/DB_NAME/...`. Use the same database name on both servers.
Allow **App IP → DB IP:5432** and **browser → App IP:8110** in network/firewall rules.

## 1. On both servers: download

```bash
sudo apt-get update && sudo apt-get install -y curl ca-certificates nano
curl -fL https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/main -o setup.tar.gz
mkdir -p setup
tar -xzf setup.tar.gz -C setup --strip-components=1
cd setup
```

## 2. On DB Server

Copy `database.dump` into `setup/`, then:

```bash
sudo bash install-dependencies.sh --role db
cp deploy.conf.example deploy.conf
chmod 600 deploy.conf database.dump
nano deploy.conf
```

Set `DB_NAME`, `DB_USER`, `BACKUP_FILE`, `BACKUP_SHA256`, `DB_LC_COLLATE`,
`DB_LC_CTYPE`; set `DB_LISTEN_IP` to this server's LAN IP and `APP_CIDR` to
the App server's `IP/32`. Keep `PG_PORT=5432`.
If the backup uses `en_US.utf8`, first run `sudo localedef -i en_US -f UTF-8 en_US.utf8`.

```bash
sudo bash deploy-db.sh --config deploy.conf
```

Enter a new database password when prompted. Keep it for the App server.
Wait for `SUCCESS` before continuing.

## 3. On App Server

Copy `filestore.tar.gz` into `setup/`, then:

```bash
chmod 600 filestore.tar.gz
sudo bash install-dependencies.sh --role app
sudo systemctl enable --now docker
sudo bash deploy-app.sh
```

Enter the DB server's IP, port `5432`, and the same database name, user and password.
If asked, enter your GitHub username and GHCR token (`read:packages` + package access).
Passwords/tokens are hidden while typing.

For a database with attachments, the first run stops at **`Filestore incomplete`**
after creating the App configuration and volume. Only for that message, restore
the matching archive below; resolve any other error first. Do not create the volume manually.

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml \
  run --rm --no-deps --user 0 --entrypoint bash \
  -v "$PWD/filestore.tar.gz:/restore.tar.gz:ro" odoo -ec \
  'test ! -d "/var/lib/odoo/filestore/$DB_NAME"; tar --no-same-owner -xzf /restore.tar.gz -C /var/lib/odoo; chown -R odoo:odoo "/var/lib/odoo/filestore/$DB_NAME"'
sudo bash deploy-app.sh
```

## 4. Open and check

Open **`http://APP_SERVER_IP:8110/app/`** and use the web login from your backup
(different from the database password). Sign in, open a business page and download an attachment.

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml ps
```

Expect only **web** and **odoo**, both healthy. The DB lives on the other server;
attachments live in the App's persistent filestore volume. Back up both.
Services start automatically after reboot. This guide uses HTTP; add HTTPS for production.

[Configuration reference](DEPLOYMENT.md) · [Verification](tests/README.md)
