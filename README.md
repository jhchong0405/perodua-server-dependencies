# Server setup

Ubuntu 24.04 (amd64). Requires access to GitHub and Ubuntu package repositories.

## Download

If download tools are missing: `sudo apt-get update && sudo apt-get install -y curl ca-certificates`.

```bash
curl -fL https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/main -o setup.tar.gz
mkdir setup
tar -xzf setup.tar.gz -C setup --strip-components=1
cd setup
```

## Database server

The installer includes PostgreSQL 16 and all tools required by `deploy-db.sh`.

```bash
sudo bash install-dependencies.sh --role db
cp deploy.conf.example deploy.conf
chmod 600 deploy.conf
```

Copy your `pg_dump -Fc` backup to the server. Edit `deploy.conf` with its absolute
`BACKUP_FILE` path, source `BACKUP_SHA256`, database name/user and source locales.
Generate the locale if needed, e.g. `sudo localedef -i en_US -f UTF-8 en_US.UTF-8`.

```bash
bash deploy-db.sh --config deploy.conf --check-config
sudo bash deploy-db.sh --config deploy.conf
```

Enter the database password when prompted. The script starts PostgreSQL and restores the backup.
For remote app access, set `DB_LISTEN_IP` and `APP_CIDR`, and allow the port through your firewall.
Odoo and its filestore are deployed separately.

Docker app server dependencies: `sudo bash install-dependencies.sh --role app`.

[Detailed reference](DEPLOYMENT.md) · [Tests](tests/README.md)
