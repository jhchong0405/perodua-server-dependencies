# Deployment reference

Ubuntu 24.04 (amd64). Run the matching command on each server:

```sh
sudo bash install-dependencies.sh --role app
sudo bash install-dependencies.sh --role db
```

- `app`: Docker Engine, Compose, Buildx, curl and CA certificates.
- `db`: PostgreSQL 16 server/client, contrib, Python 3 standard library, curl,
  CA certificates and locales, including the tools needed by `deploy-db.sh`.

Requires access to APT repositories. Uses configured package versions, adding [Docker's official repository](https://docs.docker.com/engine/install/ubuntu/) when needed. Installed target packages are not upgraded. Dependencies only; application deployment and database access configuration are separate.

## Restore an existing database on a native DB server

`deploy-db.sh` starts the installed PostgreSQL 16 cluster, restores a trusted
custom-format backup, validates application-account access, and configures
restricted database access for an App Server. It does not install Odoo, import
its filestore, or start the application. Docker is not required on the DB server.

### 1. Prepare the server and backup

The deployment script targets Ubuntu 24.04 and a PostgreSQL 16 cluster
(normally `main` on port 5432). The DB installer includes its command-line
dependencies:

```sh
sudo bash install-dependencies.sh --role db
```

Python is used only for standard-library validation and password hashing. No
Python virtual environment or pip packages are required. `curl` and CA
certificates are needed for HTTPS downloads; a local backup works offline once
the other packages are installed.

For the GitHub-only script delivery path, with a backup transferred separately
to any local directory, follow [README.md](README.md). For a backup outside the
downloaded setup directory, set `BACKUP_FILE` to its absolute path.
The repository contains deployment code and example configuration only. Database
archives, real configuration and passwords are never required in GitHub.

Prepare a **complete PostgreSQL custom-format archive** using the PostgreSQL 16
tools against the original database. Replace the uppercase placeholders below:

```sh
pg_dump -h SOURCE_DB_HOST -U SOURCE_DB_USER -d SOURCE_DB_NAME \
  -Fc --no-owner --no-acl -f database.dump
sha256sum database.dump
psql -h SOURCE_DB_HOST -U SOURCE_DB_USER -d SOURCE_DB_NAME -c \
  "SELECT datcollate, datctype, datlocprovider FROM pg_database WHERE datname = current_database();"
```

Copy the checksum and source locales into `deploy.conf`. Defaults are UTF-8
encoding, `C` collation and `C.UTF-8` character classification, suitable for a
typical Odoo database on Ubuntu. Match the original database; install any needed
OS locale before restoring. This script targets libc locales (`datlocprovider=c`);
ICU/database-version upgrades need a separate reviewed migration.

The archive must include schema and data. Plain SQL, Odoo ZIP files, encrypted
archives and raw PostgreSQL/Docker data directories are deliberately rejected.
Prepare a `.dump` from the source instead. Keep the corresponding filestore and
application revision for the separate App Server deployment. Only restore your
own trusted backups: a checksum confirms file identity, not SQL safety.

### 2. Fill in configuration

```sh
cp deploy.conf.example deploy.conf
chmod 600 deploy.conf
```

Edit the file, especially these settings:

| Setting | Meaning |
| --- | --- |
| `DB_NAME`, `DB_USER` | New target database and dedicated application login |
| `BACKUP_FILE` | Local `.dump`; relative paths are relative to `deploy.conf` |
| `BACKUP_URL` | Direct HTTPS download URL; leave `BACKUP_FILE` empty to use it |
| `BACKUP_SHA256` | Required SHA-256 from the backup preparer |
| `DOWNLOAD_USER` | Optional HTTP Basic-auth username; curl prompts for its password |
| `DB_LC_COLLATE`, `DB_LC_CTYPE` | Source database locales |
| `DB_LISTEN_IP` | DB server's own IPv4 address; initially `127.0.0.1` |
| `APP_CIDR` | Allowed App Server IPv4 source, e.g. `10.0.0.10/32`; empty means local access only |
| `MIN_FREE_MB` | Minimum free space on both download and database filesystems |
| `EXPECTED_TABLES` | Comma-separated `schema.table` names checked after restore |

Size free space for the **uncompressed** database, indexes, WAL and temporary
archive. The default 1024 MiB threshold is only a guard, not a capacity estimate.
For a remote App Server, set both `DB_LISTEN_IP` and `APP_CIDR`. The IP must be
assigned to this DB server. Only IPv4 is configured by this version.

The configuration supports literal `KEY=value` lines, optional matching outer
quotes, and full-line `#` comments. It is not executed as shell code: variable
expansion, shell commands, duplicate/unknown keys and inline comments are not
supported. Do not put passwords in it.

### 3. Check and deploy

```sh
bash deploy-db.sh --config deploy.conf --check-config
sudo bash deploy-db.sh --config deploy.conf
```

The first command validates configuration without changing services or databases.
The second asks for the new database application password and its confirmation.
For authenticated HTTPS downloads it also asks for the cloud download password.
Passwords must contain 12-1024 printable ASCII characters; punctuation and spaces
are supported. Passwords are not printed, passed in command-line arguments, or
stored in configuration/success logs. The database stores a SCRAM verifier.

For controlled automation, use a root-owned regular file with mode 0600 (or 0400)
containing the application password, and a local `BACKUP_FILE`:

```sh
sudo bash deploy-db.sh --config deploy.conf --password-file /root/db-password
```

The caller manages that password file's creation and removal. Authenticated
cloud downloads remain interactive. A protected, noninteractive secret-store
integration is not included.

### What the script does

1. Verifies the installed cluster/version, configuration, existing target and
   credentials; serializes deployments with a cluster-level lock.
2. Enables and starts `postgresql@16-main` (or the configured cluster) under
   systemd and waits for it to become ready.
3. Downloads/copies the archive into a private temporary directory and checks
   SHA-256 and archive format before creating a role or database.
4. Creates a non-superuser, non-CREATEDB application role. An existing role is
   reused only if it has no elevated privileges or memberships, and the supplied
   password authenticates. Existing roles' passwords/privileges are not changed.
5. Restores into a uniquely named staging database in a single transaction, with
   all restored business objects owned by the application role. Original object
   owners/ACLs, tablespace placements, publications and subscriptions are not
   carried over. This is an Odoo single-application-owner deployment policy.
6. Verifies real TCP/SCRAM login, required tables and read access, and runs ANALYZE.
   Only then renames the staging database to the requested target.
7. Records success with the archive checksum, database OID and application role;
   prints connection details without the password.

Trusted extensions such as `unaccent` and `pg_trgm` are restored as the application
database owner. Required extension binaries must already be installed. Extensions
requiring superuser privileges fail closed; prepare a reviewed extension-specific
migration instead of making the application account a superuser.

Access rules are prepended for the target database: local application connections
and the specified App Server source use SCRAM; other TCP sources/users are
rejected for this database. When `APP_CIDR` is configured, the same `DB_USER` and
source also receive SCRAM access to the `postgres` maintenance database, which
the App preflight and Odoo entrypoint require before startup. This does not grant
database-creation or superuser privileges. Unix-socket administration and existing
HBA rules are preserved. Existing listener addresses are preserved and the requested
address is added. Adding a listener may restart this PostgreSQL cluster, briefly
disconnecting its other sessions; deploy on the intended DB server during its
setup/maintenance window. Configuration snapshots are retained; failed network
configuration is rolled back.

**Host and cloud firewalls are not modified.** If `APP_CIDR` is set, allow TCP
`PG_PORT` from that source using your existing firewall management, then test from
the App Server. Local verification does not prove the remote network path. TLS
certificate provisioning and a strict `hostssl` policy are also outside this
script; configure them according to your environment before production use.

On systems without systemd as PID 1, the script can start PostgreSQL using
`pg_ctlcluster`, but explicitly reports that boot enablement was not verified.
This path allows isolated tests; the intended server deployment uses systemd.

### Reruns and recovery

- A matching successful deployment is validated without downloading or restoring
  again. Changes made by the application after deployment are preserved.
- An unrelated same-name database, different checksum, changed owner or changed
  locale is refused. There is no overwrite, delete or force option.
- A failed import/validation does not publish a target database. Its uniquely
  named staging database is retained for inspection; retrying creates a new one.
  Inspect and remove obsolete staging databases manually when appropriate.
- If interruption occurs between successful rename and success recording, the
  pending identity allows a rerun to verify and finish recording that database.
- An incorrect password for an existing role fails; the script does not reset it.
- For `out of shared memory` / `max_locks_per_transaction` during a large restore,
  have the DB administrator size that setting and restart the cluster before
  retrying. No partial restore is promoted.

Logs, protected state, configuration snapshots and a password-free connection
summary are in `/var/lib/perodua-db-deploy/16-main/DB_NAME/` (cluster-dependent).
Private download/password temporary files are removed on normal exit/failure.
After SIGKILL or a host crash, an abandoned root-only
`/var/tmp/perodua-db-deploy.*` directory may remain; inspect and clean it manually.
Keep these state files for safe reruns. Database upgrades, automatic backup
scheduling and Odoo/filestore validation are separate tasks.

## Isolated verification

The integration suite uses real PostgreSQL 16 in a fresh Ubuntu 24.04 container.
It must never run on a production database host. See `tests/Dockerfile` and
`tests/run-deploy-db-integration.sh`; the test harness refuses to run without its
explicit disposable-container guard. Docker is needed only for this developer
test, not for server deployment.
