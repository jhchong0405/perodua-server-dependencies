# Deployment reference

All scripts and configuration templates are in the `scripts` folder of the
repository, and the commands below run there (see the
[README](../README.md#repository-layout)). `deploy.conf`, backups and the
filestore archive go in that folder too; relative paths in `deploy.conf` resolve
against it.

Ubuntu 24.04 (amd64). Run the matching command on each server:

```sh
sudo bash install-dependencies.sh --role app
sudo bash install-dependencies.sh --role db
```

- `app`: Docker Engine, Compose, Buildx, curl and CA certificates.
- `db`: PostgreSQL 16 server/client, contrib, Python 3 standard library, curl,
  CA certificates and locales, including the tools needed by `deploy-db.sh`.

Requires access to APT repositories. Uses configured package versions, adding [Docker's official repository](https://docs.docker.com/engine/install/ubuntu/) when needed. Installed target packages are not upgraded. Dependencies only; application deployment and database access configuration are separate.

## Two database workflows

`deploy-db.sh` has two modes, chosen with `DB_MODE` in `deploy.conf`:

| `DB_MODE` | Database it produces | App Server step |
| --- | --- | --- |
| `restore` (default) | A copy of a trusted `pg_dump -Fc` backup | `sudo bash deploy-app.sh`, plus the matching filestore |
| `empty` | An empty database owned by `DB_USER`, for a fresh UAT system | `sudo bash deploy-app.sh --init-db` |

Existing configurations without `DB_MODE` keep working as `restore`. The two
modes record different deployment identities, so a database created in one
mode is never accepted by a rerun in the other.

## Restore an existing database on a native DB server (`DB_MODE=restore`)

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
to any local directory, follow [README.md](../README.md). For a backup outside the
`scripts` folder, set `BACKUP_FILE` to its absolute path.
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
| `DB_MODE` | `restore` (default) or `empty`; see [Two database workflows](#two-database-workflows) |
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
- A database deployed with `DB_MODE=empty` is not accepted by a `restore` rerun,
  and a restored database is not accepted by an `empty` rerun.

Logs, protected state, configuration snapshots and a password-free connection
summary are in `/var/lib/perodua-db-deploy/16-main/DB_NAME/` (cluster-dependent).
Private download/password temporary files are removed on normal exit/failure.
After SIGKILL or a host crash, an abandoned root-only
`/var/tmp/perodua-db-deploy.*` directory may remain; inspect and clean it manually.
Keep these state files for safe reruns. Database upgrades, automatic backup
scheduling and Odoo/filestore validation are separate tasks.

## Create an empty database for a fresh UAT system (`DB_MODE=empty`)

Use this when there is no backup to restore and the App Server should build a
new UAT system. No backup is downloaded, copied or restored.

The simplest way is `sudo bash deploy-db.sh` without a `deploy.conf`. On a
terminal it asks for this server's internal IP, offering the addresses it finds
(Docker network addresses are left out, and private addresses are suggested
first). It accepts only an address of this server, and asks for confirmation
before using a public internet address. It then asks for the App server's IP, or
a network in CIDR form, which must not be this server: the App server always runs
on another server. It then shows the settings, and after confirmation saves them as `deploy.conf`
next to the script: `DB_MODE=empty`, database `perodua`, user `odoo`, the
installed cluster's port, `DB_LISTEN_IP` and `APP_CIDR` (`/32` for a single
address). Declining the summary starts the questions again. Whenever an answer
cannot be used, the script says why and asks again. The database password is
asked for in the same way: a password that is too short or not plain ASCII, or
two different entries, is asked for again. Choosing a restore instead saves
nothing. Later runs read the saved file and ask no setup questions; an explicit
`--config` is never guided.

To choose other values, write `deploy.conf` yourself:

```
DB_MODE=empty
BACKUP_FILE=
BACKUP_URL=
BACKUP_SHA256=
DOWNLOAD_USER=
```

`EXPECTED_TABLES` is not used in this mode. Set `DB_NAME`, `DB_USER`, the
locales, `DB_LISTEN_IP` and `APP_CIDR` as for a restore, then run the same
commands:

```sh
bash deploy-db.sh --config deploy.conf --check-config
sudo bash deploy-db.sh --config deploy.conf
```

The script then:

1. Starts and checks the PostgreSQL 16 cluster, as for a restore.
2. Creates or reuses the application role with the same password handling. The
   role stays `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
   NOBYPASSRLS` with no role memberships; an existing role with more privileges
   is refused, not changed.
3. Creates the database under a temporary staging name, owned by `DB_USER`,
   from `template0` with `UTF8` encoding and the configured locales, and revokes
   all database privileges from `PUBLIC`.
4. Applies the same access rules and listener handling as a restore.
5. Verifies TCP/SCRAM login as `DB_USER`, the owner, the encoding and locales,
   that `PUBLIC` cannot connect, and that the role is still least-privileged.
   Only then renames the staging database to `DB_NAME` and records the
   deployment.

The App Server account therefore does not need `CREATEDB`: the database already
exists when the App Server initializes it.

Rerunning `DB_MODE=empty` is safe after the App Server has initialized Odoo in
the database. A database this script created, still owned by `DB_USER` with the
configured locales, is kept as it is: it is not dropped, recreated or emptied,
its contents are not inspected, and its OID does not change. A same-name
database that this script did not create, or whose owner or locales changed, is
refused, even if its owner and locales happen to match.

## Initialize a fresh UAT system on the App Server

After `deploy-db.sh` reports `SUCCESS` in `DB_MODE=empty`, run on the App Server:

```sh
sudo bash deploy-app.sh --init-db
```

The App preflight reports the database as `EMPTY`, and the script then:

1. Checks the pinned Odoo image before anything is written to the database
   (`uat_guard.py`). It computes the modules Odoo could install for the fresh
   module list, following Odoo 19's own dependency and `auto_install` rules
   (as a superset: country-specific modules are assumed to install too), and
   refuses if `perodua_demo_client` would be among them, if its manifest has
   any `auto_install`, or if an installed module refers to it without declaring
   the dependency: XML IDs, imports, paths into its folder, the settings field
   that installs it, or its name in code, SQL and data files, including the
   compiled `.pyc` files and spreadsheet files the image ships. Mentions in
   comments and descriptions do not count. One reference in
   `perodua_demo/hooks.pyc` was reviewed and is accepted only while that file's
   SHA-256 is unchanged. A file it cannot read also stops the initialization.
2. Detects the image's Odoo version and uses the matching option to disable Odoo
   demo data (`--without-demo=True` on Odoo 19), after checking with that
   Odoo's own option parser that it does disable it. An unknown version stops
   the initialization.
3. Installs `perodua_client_stable`, `perodua_gateway`,
   `perodua_forecast_workbook`, `perodua_supplier_execution` and
   `perodua_uiux_api` with their dependencies.
4. Checks the database: those modules are installed, `perodua_demo_client` is
   not, no records were loaded under its name, no module has demo data, and
   every installed Perodua module has the version this image ships.
5. Sets up the UAT administrators through the Odoo ORM.
6. Starts the App, signs in as each UAT administrator through the workbench
   login, and checks that a wrong password is refused. Only then is the
   database stamped `READY`.

**UAT-only fixed credentials.** These are for a UAT system only and must not be
used on a production system or one reachable from the public internet:

| Login | Password | Account |
| --- | --- | --- |
| `whadmin` | `perodua` | Sumathi (Admin), seeded by the release |
| `admin1` | `perodua` | Haziq (Admin 1), seeded by the release |
| `admin2` | `perodua` | Nurul (Admin 2), seeded by the release |

The release's `perodua_demo_ui` module already creates these three logins. The
initialization keeps them, with their names, IDs and the seeded records that
refer to them, and gives each exactly the groups and companies of Odoo's native
Administrator (`base.user_admin`), to which every module grants its
administrator rights. It then archives the native Administrator, which the seed
data renamed to `admin@demo.perodua.my` and which still had Odoo's default
password, so these three are the only active administrator logins. Odoo itself
recommends archiving that user rather than deleting it. The technical superuser
is not changed. If a later release stops seeding one of the three logins, it is
created as a copy of the native Administrator instead.

The same seed data also creates five role users that are not administrators:
`planner`, `whouse`, `sop`, `op` and `finance`. They also sign in with
`perodua`. The initialization does not change them.

The accounts are set up only during this first initialization. Later runs of
`deploy-app.sh` never reset their passwords.

Note that `perodua_client_stable` depends on `perodua_demo_profile`, which
depends on `perodua_demo_ui` and, through it, `perodua_demo`. Those modules are
part of the release's module graph, so their own seed records are present in a
fresh UAT system. Only the client demonstration dataset (`perodua_demo_client`)
and Odoo's demo data are left out.

If the initialization stops after the modules are installed, for example on a
timeout, Ctrl-C, a lost SSH session or a failed sign-in check, the preflight
reports `SETUP_PENDING` (or `SETUP_UNMARKED` if it stopped before it could
record that). Rerun `sudo bash deploy-app.sh --init-db` to finish: it sets up
the administrators and repeats the checks, and does not reinstall modules. An
initialized (`READY`) database is never reinitialized or upgraded (`-u`), with
or without `--init-db`.

If the module installation itself failed part-way, the App refuses the database
instead, because a partial installation cannot be finished safely. Start again
from an empty database:

1. On the DB Server, drop the unfinished database, for example
   `sudo -u postgres dropdb perodua` (use your `DB_NAME`). Only do this for a
   database that never reached `READY`.
2. Run `sudo bash deploy-db.sh --config deploy.conf` again, with
   `DB_MODE=empty` and the same password. It creates a new empty database.
3. On the App Server, run `sudo bash deploy-app.sh --init-db` again.

A missing database is created by the App only if `DB_USER` has `CREATEDB`. With
the least-privileged role from `deploy-db.sh`, the App stops and asks for the
database to be created on the DB server with `DB_MODE=empty` first.

## Uninstall (`uninstall.sh`)

`uninstall.sh` undoes `deploy-app.sh` (`--role app`) or `deploy-db.sh`
(`--role db`). It lists what it will remove and changes nothing until the
database or project name is typed. For unattended use, pass the name with
`--confirm NAME`; any other value is refused.

**App Server** (`sudo bash uninstall.sh --role app [--dir PATH] [--purge]`)
removes the directory only if `deploy-app.sh` created it (it contains
`.deployment-identity`). It runs `docker compose down --remove-orphans` for that
project, then deletes the directory with its configuration, logs and copy of the
database password. The attachments volume and the pinned images are kept. With
`--purge` the volume is removed as well (`down --volumes`), and so is each image
that no other container uses. The database is not touched.

**DB Server** (`sudo bash uninstall.sh --role db [--config FILE | --database NAME] [--purge]`)
takes the database from `deploy.conf` next to the script, `--config`,
`--database`, or the only deployment recorded on the server. It removes a
database only if `deploy-db.sh` recorded it and it still has the recorded OID
and owner; it refuses a same-name database that the script did not create, or
one replaced since. It also refuses while connections are open, so uninstall the
App first. It then removes:

- the database and any staging copy that a failed run left;
- the login role and so its password, unless the role still owns another
  database or another recorded deployment uses it;
- the managed block of access rules in `pg_hba.conf`, validated before PostgreSQL
  reloads it;
- the listen address that the deployment added, unless another recorded
  deployment uses it (PostgreSQL restarts; `127.0.0.1` stays);
- the deployment records, and `deploy.conf` if the guided setup wrote it. A
  hand-written `deploy.conf` is kept.

PostgreSQL stays installed. With `--purge`, and a typed `PURGE` on a terminal,
the script also uninstalls the PostgreSQL 16 packages and deletes all their data,
configuration and logs, which removes every database on the server. It first
checks that apt would remove only those packages, and refuses if another
PostgreSQL version or a dependent package would go too. Libraries pulled in by
the installation stay (`sudo apt autoremove` lists them).

## Isolated verification

The integration suite uses real PostgreSQL 16 in a fresh Ubuntu 24.04 container.
It must never run on a production database host. See `tests/Dockerfile` and
`tests/run-deploy-db-integration.sh`; the test harness refuses to run without its
explicit disposable-container guard. Docker is needed only for this developer
test, not for server deployment.
