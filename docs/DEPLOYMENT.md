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
| `APP_CIDR` | Allowed App Server IPv4 source, e.g. `10.0.0.10/32`, or several separated by commas; empty means local access only |
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
terminal it first asks where the App runs.

- **On another server** (the default): it asks for this server's internal IP,
  offering the addresses it finds (Docker network addresses are left out, and
  private addresses are suggested first). It accepts only an address of this
  server, and asks for confirmation before using a public internet address. It
  then asks for the App server's IP, or a network in CIDR form. If the answer is
  this server (its own address, `127.0.0.1` or `localhost`), it asks whether the
  App runs on this server too and, if so, continues as below.
- **On this server too**: see [One server](#one-server) below. No address is
  asked for.

It then shows the settings, and after confirmation saves them as `deploy.conf`
next to the script: `DB_MODE=empty`, database `perodua`, user `odoo`, the
installed cluster's port, `DB_LISTEN_IP` and `APP_CIDR` (`/32` for a single
address). Declining the summary starts the questions again. Whenever an answer
cannot be used, the script says why and asks again. The database password is
asked for in the same way: a password that is too short or not plain ASCII, or
two different entries, is asked for again. Choosing a restore instead saves
nothing. Later runs read the saved file and ask no setup questions; an explicit
`--config` is never guided.

### One server

When the App runs on the DB server too, its containers reach PostgreSQL on
Docker's address on this server (the default bridge, usually `172.17.0.1`),
which only this server and its containers reach. The guided setup then saves
`DB_LISTEN_IP` as that address and `APP_CIDR` as the networks Docker gives
containers their addresses in: Docker's configured address pools, or its
defaults `172.16.0.0/12,192.168.0.0/16`. Docker must be running first
(`sudo bash install-dependencies.sh --role app`); otherwise the script says so
and asks again. A hand-written `deploy.conf` with these values works the same.

Docker creates that address only when it starts, and at boot PostgreSQL would
start first and listen on its other addresses only. So whenever `DB_LISTEN_IP`
is a Docker network address of this server, `deploy-db.sh` writes
`/etc/systemd/system/postgresql@16-<cluster>.service.d/perodua-after-docker.conf`
(`After=docker.service`), which makes PostgreSQL start after Docker. No firewall
change is needed, and the final message points to `deploy-app.sh` on the same
server, which offers the recorded database settings as its defaults.
`uninstall.sh --role db` removes the setting together with the listen address.

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

The first run asks for the database settings. On one server they default to what
`deploy-db.sh` recorded there. A database host of `127.0.0.1` or `localhost` is
explained and asked again: inside the App container it would be the container
itself. On one server, use Docker's address (the `DB_HOST` `deploy-db.sh`
printed).

**An `app.env` written beforehand.** Before the first run, the deployment
directory (`/opt/perodua-app`) may hold an `app.env` written by hand, and
nothing else. The run reads it as its configuration: settings that are there
are not asked, for example `PUBLIC_ROOT` and the other settings of
[Two environments on one server](#two-environments-on-one-server-by-path-dev-uat),
`HTTP_PORT` and `BIND_IP`. When the file has no `DB_HOST`, a run on a terminal
asks the database questions, with the file's values as defaults, and saves the
answers in the same file. `--check-config`, `--non-interactive` and a run
without a terminal stop instead and name the missing `DB_HOST`. With `--config`,
the deployment directory must still be empty.

**A first run that stopped before it used the database.** The deployment
directory is bound to its database (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`)
and its release once the run has used the database: when it initializes an
empty one, finishes an interrupted setup, or accepts an initialized one. Until
then (for example after a wrong `DB_HOST`, a DB server firewall that blocks
port 5432, a DB server whose `APP_CIDR` does not contain this App server, a
wrong password, or a missing database), `.deployment-unverified` in the
directory marks it as not bound yet:

- The failed database check prints this App server's addresses, which the DB
  server must accept.
- A rerun may change the database settings and the release (a newer `scripts`
  folder). `PROJECT_NAME` must stay the same, because the project's Docker
  network and volume carry that name.
- On a terminal, the rerun asks for the database password again; Enter keeps the
  one entered before. Without a terminal, or with `DB_PASSWORD_FILE` naming a
  file of your own, the password is read as before.
- `service.sh` does not start, restart or reset such a deployment, so it never
  starts an App on, or deletes the data of, a database the directory has not
  used. It stops with the `deploy-app.sh` command that finishes the deployment.
  `stop` and `status` work as usual.

Deployments set up by earlier scripts have no such file and stay bound.

It then asks who may open the web page, and saves the answer as `BIND_IP`:

| Choice | `BIND_IP` | Reached from |
| --- | --- | --- |
| Only this server | `127.0.0.1` | this server; from a computer through the SSH tunnel the script prints at the end |
| The private network | the server's private address | computers on that network (the default when the server has one) |
| Every computer | `0.0.0.0` | every address of the server |

On a server with a public IPv4 address, choosing every computer asks for a
confirmation. Without a firewall in front of the web port, anyone on the
internet could sign in with the UAT passwords. Use the cloud provider's
firewall (security group): Docker's published ports bypass ufw. With a
configuration file that sets `BIND_IP=0.0.0.0`, the script prints the same
warning instead of asking.

If the project's attachments volume was already there (a default
`uninstall.sh --role app` keeps it), the script decides what to do with it:

- **The database already holds this system:** it uses the volume again, and
  checks the files against the database.
- **The database is new and empty (`--init-db`):** it counts the files still in
  the volume. They belong to the earlier database, so it asks before deleting
  the volume. Without a terminal, it stops with the command to delete it. The
  database is not changed.

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
| `whadmin` | `perodua` | Sumathi (Admin), created by the release |
| `admin1` | `perodua` | Haziq (Admin 1), created by the release |
| `admin2` | `perodua` | Nurul (Admin 2), created by the release |

The release's `perodua_demo_ui` module already creates these three logins. The
initialization keeps them, with their names and IDs, and gives each exactly the
groups and companies of Odoo's native Administrator (`base.user_admin`), to
which every module grants its administrator rights. It then archives the native
Administrator (login `admin`, still with Odoo's default password), so these
three are the only active administrator logins. Odoo itself recommends archiving
that user rather than deleting it. The technical superuser is not changed. If a
later release stops creating one of the three logins, it is created as a copy of
the native Administrator instead.

The same module also creates five role users that are not administrators:
`planner`, `whouse`, `sop`, `op` and `finance`, and one supplier portal login,
`supplier`. They also sign in with `perodua`. The initialization does not change
them.

The accounts are set up only during this first initialization. Later runs of
`deploy-app.sh` never reset their passwords.

**No business data.** From v1.0.1 the release image loads no sample data, and
from v1.0.2 it also leaves out the reference lists users maintain themselves. A
fresh UAT system has no parts, customers, suppliers, agents, price lists,
campaigns, routes, calendars, forecasts, IDDIs, orders or invoices; the EBS and
PROMISE registers (Master Integration, Customer Rank, Supplier Classification,
freight agents) are empty; and so are Holiday Types, Order Cycles, Order Types,
Customer Type, Payment Method and Retail Price List, which administrators fill
in on those pages with **+ New**. Currencies lists only MYR: from v1.0.3 the
other currencies Odoo ships are removed, so any currency can be added there with
**+ New** (enter its ISO code, symbol and rounding). What is set up:

- the company: Perodua Parts Sdn Bhd, Malaysia, MYR on the Malaysian chart of
  accounts, with the AR/AP journal rules;
- the HQ Distribution warehouse (`HQDC`), which the workbench cannot create;
- the weekday and material characteristic lists, which no screen edits, and
  Odoo's units of measure;
- the logins above.

Two things to know when filling in the lists:

- Sale orders take their processing method (Part-to-Part, Campaign,
  Special-Monthly) and channel (Stockist, Export) from the order types the
  release used to ship. Order types created in the workbench are not
  recognized: their orders count as Normal, with no channel.
- Campaigns target customers by customer type code. Give the service-centre
  type the code `CAT-SERVICE` and the body-and-paint type `CAT-BP`.

The client demonstration dataset (`perodua_demo_client`) and Odoo's demo data
are left out as before.

The EBS and PROMISE registers are read-only in the application: their rows come
only from those systems. Until real feeds are connected they stay empty, and
screens that pick from them (for example an EBS supplier to activate, or an OEM
material) have nothing to offer.

The integrations run in mock mode (`perodua_integration.mode`). The mock
PROMISE, PSS and PSOS feeds start empty, and the two scheduled pulls that turn
the mock PROMISE part feed and the mock PSS order feed into records are switched
off: *PROMISE: consume Part Master feed* and the PSS order pull. Switch them on
again under Settings > Technical > Scheduled Actions once real systems are
connected. Demo Control's Reset & Reseed is refused on such a database, because
it would load the sample data.

A database initialized with v1.0.0 to v1.0.2 cannot be used with v1.0.8: its
module fingerprint differs and the App refuses it. Run
`sudo bash service.sh --role app reset` from the v1.0.8 `scripts` folder on the
App Server (see [Stop, start and reset](#stop-start-and-reset-servicesh); it
deletes the data), or uninstall both servers and initialize again. v1.0.8 has
the same Odoo modules as v1.0.7, v1.0.6, v1.0.5, v1.0.4 and v1.0.3, so a directory deployed with
one of them keeps its data with `sudo bash deploy-app.sh --upgrade` from the
v1.0.8 `scripts` folder (see [Upgrade to a new release](#upgrade-to-a-new-release-keeping-the-data-deploy-appsh---upgrade),
and the steps in the README:
[Upgrade an App server to v1.0.8](../README.md#upgrade-an-app-server-to-v108)).
Without `--upgrade` such a directory is refused, because it records its release.

If the initialization stops after the modules are installed, for example on a
timeout, Ctrl-C, a lost SSH session or a failed sign-in check, the preflight
reports `SETUP_PENDING` (or `SETUP_UNMARKED` if it stopped before it could
record that). Rerun `sudo bash deploy-app.sh --init-db` to finish: it sets up
the administrators and repeats the checks, and does not reinstall modules. An
initialized (`READY`) database is never reinitialized or upgraded (`-u`), with
or without `--init-db`.

If the module installation itself failed part-way, the App refuses the database
instead, because a partial installation cannot be finished safely. Start again
from an empty database: on the App Server, run
`sudo bash service.sh --role app reset`, which empties the database and
initializes it again. Or, on both servers:

1. On the DB Server, drop the unfinished database, for example
   `sudo -u postgres dropdb perodua` (use your `DB_NAME`). Only do this for a
   database that never reached `READY`.
2. Run `sudo bash deploy-db.sh --config deploy.conf` again, with
   `DB_MODE=empty` and the same password. It creates a new empty database.
3. On the App Server, run `sudo bash deploy-app.sh --init-db` again.

A missing database is created by the App only if `DB_USER` has `CREATEDB`. With
the least-privileged role from `deploy-db.sh`, the App stops and asks for the
database to be created on the DB server with `DB_MODE=empty` first.

## Stop, start and reset (`service.sh`)

`service.sh` stops, starts and restarts a deployment without deleting anything,
and shows its status. Stop the App before the database, and start the database
before the App.

**App Server** (`sudo bash service.sh --role app [--dir PATH] stop|start|restart|status`)
works on the directory `deploy-app.sh` created (default `/opt/perodua-app`).
`stop` runs `docker compose stop`. The containers, the attachments volume and
the settings stay, and because the containers were stopped by hand, Docker does
not start them again after a reboot. `start` first runs the database check of
`deploy-app.sh` (`preflight.py check`) and goes ahead only on `READY`: on an
empty or unfinished database, Odoo would set itself up at start without the UAT
setup. It then runs `docker compose up --detach --no-recreate --wait` and waits
up to 10 minutes for both containers to report healthy; if they do not, it
prints the command that shows their logs. `restart` is the same check, `stop`
and then `start`. `status` lists the containers with their health and ports.
Like `uninstall.sh`, every action except `status` takes the lock on
`.deploy.lock` first and stops with "A deployment or uninstall is running in
this directory" while `deploy-app.sh` or `uninstall.sh` runs.

**DB Server** (`sudo bash service.sh --role db [--cluster NAME] stop|start|restart|status`)
works on the PostgreSQL 16 cluster named by `PG_CLUSTER` in `deploy.conf` next
to the script, `--cluster`, or `main`. It uses `pg_ctlcluster`, which goes
through systemd where systemd runs. `stop` names the number of open
connections it closes; an App that is still running shows errors until the
database is back. `start` waits until PostgreSQL accepts connections. `status`
shows `pg_lsclusters` and the number of open connections. PostgreSQL starts
again with the server, stopped or not.

**Reset** (`sudo bash service.sh --role app [--dir PATH] reset [--confirm DATABASE]`)
turns the deployment back into a fresh UAT system from the App Server alone.
First the `deploy-app.sh` next to it checks the saved configuration
(`deploy-app.sh --check-config`); if it refuses it, the reset stops and nothing
is changed. Then the reset shows its plan and changes nothing until the
database name is typed, or given with `--confirm` for unattended use. Then it:

1. stops the App;
2. deletes all data in the database with `reset_database.py`, run in the Odoo
   image as the application role: the tables, views, sequences and functions
   in `public` and in the schemas the role created (such as `api`). `public` is
   then created again as PostgreSQL creates it in a new database. The database
   itself, its owner, the login and password and the access rules stay;
3. removes the containers, the attachments volume and the containers'
   anonymous volumes, as `uninstall.sh --purge` does, but keeps the directory,
   its configuration and the images;
4. runs `deploy-app.sh --init-db` with the saved configuration, from the same
   `scripts` folder: see [Initialize a fresh UAT system on the App Server](#initialize-a-fresh-uat-system-on-the-app-server).
   Before that, the directory is bound to that release, so a reset run from a
   newer release's folder installs the newer release.

PostgreSQL locks every object it drops until the end of the transaction. An
Odoo database has thousands of them (each table has foreign keys to
`res_users`, each with its triggers), more than a server with default settings
has room for in one transaction ("out of shared memory"). So step 2 drops the
foreign keys first and then the tables, a few hundred per transaction, and
waits at most a minute for a lock. If it fails before anything was dropped (for
example when the database cannot be reached), the script says nothing was
deleted and leaves the App stopped; start it again with `start`. If it fails
later, the database is only partly emptied and the App stays stopped: solve the
problem and run the reset again, which finishes it. The same applies when
`deploy-app.sh` stops, for example when the registry cannot be reached.

## Upgrade to a new release, keeping the data (`deploy-app.sh --upgrade`)

`sudo bash deploy-app.sh --upgrade [--dir PATH] [--backup-dir PATH] [--non-interactive]`,
run from the `scripts` folder of the new release on the App Server, moves a
deployment directory (default `/opt/perodua-app`) to that release. The
database, the attachments volume and the settings in `app.env` stay. A plain
`deploy-app.sh` refuses a directory that another release deployed and points to
`--upgrade`. `--upgrade` does not combine with `--init-db` or `--check-config`.
For the move to v1.0.8 (from v1.0.4 to v1.0.7), the README gives the steps in
order, HTTPS and the acceptance checks included:
[Upgrade an App server to v1.0.8](../README.md#upgrade-an-app-server-to-v108).

**What it requires.** Each check below stops the upgrade before anything
changes:

| Check | Refused when |
| --- | --- |
| The directory is a deployment | It has no `.deployment-identity`, or its first deployment never used its database (`.deployment-unverified`). |
| The same deployment | `.deployment-identity` differs from the new one in anything but `release=` and `revision=`: the project, the database server (`host=`, `port=`), the database or the user. The message names the values that differ. |
| Another release | The directory already runs this release: run `deploy-app.sh` without `--upgrade`. |
| The same Odoo modules | The database check (`preflight.py check`) of the new image does not report `READY`, for example when the module fingerprint differs. |

The **module fingerprint** is the MD5 of the `__manifest__.py` files of all
`perodua_*` modules in the image, read in name order. A fresh UAT
initialization stores it as `perodua.image_modhash` when it is stamped `READY`;
every database check compares the stored value with the one of the image. So it
stays the same while no byte of any of these manifests changes and no module
folder is added or removed. A release that changes only controllers, other
Python code or the web page has the same fingerprint. A new module version, a
new data file or a new dependency changes it. Such a release needs a module
upgrade (`-u`), which `--upgrade` never runs: it stops with "a module upgrade
needs its own plan" and changes nothing.

**The steps, in order:**

1. Compare the identities (above).
2. Pull the new images. The App keeps running.
3. Check the database with the new image, from a staged copy of the new
   `compose.yml` in a temporary folder of the directory: same project, so the
   same network and attachments volume. The directory's own files are not
   touched.
4. Check the room for the backup: the size of the database
   (`pg_database_size`) and of `filestore/DB_NAME` together must fit in the free
   space of the backup folder's disk, or it stops and changes nothing. This is
   more than the backup takes: the dump leaves out the indexes and is
   compressed. Then stop the App (`docker compose stop web odoo`), so that the
   backup holds the data as it is at the switch.
5. Back up into `DIR/backups/TIME-OLD_RELEASE/` (with `--backup-dir PATH`:
   `PATH/TIME-OLD_RELEASE/`), a folder only root can read:
   - `database.dump`: `pg_dump --format=custom` of the database, run in the new
     Odoo image as the App's database user. The image's PostgreSQL 18 client
     reaches the DB Server the way Odoo does. The password comes from the
     container's environment, never a command line. The dump must read back with
     `pg_restore --list` and must contain the data of `ir_module_module`;
   - `filestore.tar.gz`: `filestore/DB_NAME/...` from the attachments volume, as
     [From a backup](../README.md#from-a-backup) restores it. It must read back
     with `tar`;
   - `deployment-identity` and `app.env`: the directory's identity and settings
     before the upgrade;
   - `restore.txt`: the restore commands below, also printed.

   `pg_dump` and `tar` write to standard output. These two runs have no
   container log (`logging: driver: none`, from an extra compose file in the
   temporary folder): Docker's default log would keep another copy, about four
   times the size, on the disk of `/var/lib/docker`. The App's own services keep
   their logs.
6. **The switch:** write the new `compose.yml`, `preflight.py` and helpers,
   rewrite `release=` and `revision=` in `.deployment-identity`, and write
   `app.env` again.
7. The usual deployment: the database check, the attachments check, `report.url`
   and `PUBLIC_BASE_URL`, `docker compose up --force-recreate`, and the HTTP
   self-check. The final message names the backup folder.

The App is down from step 4 until step 7 finishes: the size of the database and
of the attachments sets most of that time. Run `--upgrade` in `tmux` or
`screen`. A lost SSH session (SIGHUP) stops the run as Ctrl-C does: before the
switch the old App starts again, as in the table below, and the script exits
with status 129.

**When a step fails:**

| Failed step | State of the server | What to do |
| --- | --- | --- |
| 1 to 3, or the room check of 4 | Nothing changed: the directory still runs the old release, its App still runs. | Solve the cause (for the room: free space, or `--backup-dir` on another disk) and run `--upgrade` again. |
| 4 (after the room check) or 5, also a lost SSH session | The incomplete backup folder is deleted. The directory still runs the old release, with its own files. Its App is started again (`up --no-recreate`) if it was running before; if that fails, the message gives the `service.sh start` command. | Solve the cause (often the disk space), then run `--upgrade` again. |
| 6 or 7 | The directory names the new release. Before the containers are recreated, the App is stopped; the old containers stay as they are, and the database holds the data of the backup (only `report.url` and `web.base.url` may have been written again, with the values from `app.env`). After that, the containers run the new images but did not pass the self-check, and the database holds the backup's data plus what the App wrote since. The message says which. | To finish: solve the cause, then run `deploy-app.sh` (without `--upgrade`) from the new folder. To go back: see below. |

**Going back.** Put back the identity and the settings that the directory had
before the upgrade, then deploy the old release without `--upgrade`, from any
`scripts` folder of it (the folders of v1.0.4 and earlier have no `--upgrade`):

```bash
sudo cp -p BACKUP/deployment-identity DIR/.deployment-identity
sudo cp -p BACKUP/app.env DIR/app.env
sudo bash deploy-app.sh --dir DIR    # from the scripts folder of the old release
```

The failure message after the switch and step 5 of `restore.txt` print these
commands with the real paths. This works because both releases have the same
modules. A `scripts` folder of the old release that has `--upgrade` can also
run `sudo bash deploy-app.sh --upgrade --dir DIR`, which takes a new backup
first.

To also put the data back as it was before the upgrade, follow `restore.txt`
instead. In short, on the App Server: stop the App
(`service.sh --role app stop`); empty the database with `reset_database.py`, as
`service.sh reset` does; restore `database.dump`; put the attachments back and
delete the Odoo sessions; and deploy the release that the data belongs to.
After that, every user must sign in again. The restored database has the
password hashes and the single-session values of the backup, so without the
deletion a sign-in that ended after the backup (a sign-out, a new sign-in, a
password reset) would be valid again. Passwords changed after the backup are
undone: a user who changed a password for safety must change it again. The archive comes from pg_dump 18,
so restore it with the tools of the Odoo image, as `restore.txt` shows: the
`pg_restore` 16 of the DB Server stops with "unsupported version (1.16) in file
header". `restore.txt` lets `pg_restore` write SQL and leaves out its first
`SET transaction_timeout = 0;` line, which PostgreSQL 16 does not know. psql
then runs the rest in one transaction, so a failed restore leaves the database
empty, and you can run it again.

`uninstall.sh --role app` deletes the directory and the backups in it. Its plan
names them; copy them elsewhere first, or give `--backup-dir` a folder outside
the directory.

## Uninstall (`uninstall.sh`)

`uninstall.sh` undoes `deploy-app.sh` (`--role app`) or `deploy-db.sh`
(`--role db`). It lists what it will remove and changes nothing until the
database or project name is typed. For unattended use, pass the name with
`--confirm NAME`; any other value is refused.

**App Server** (`sudo bash uninstall.sh --role app [--dir PATH] [--purge]`)
removes the directory only if `deploy-app.sh` created it (it contains
`.deployment-identity`). It runs `docker compose down --remove-orphans` for that
project and deletes the containers' anonymous volumes, then deletes the directory
with its configuration, logs and copy of the database password. The attachments
volume and the pinned images are kept. A later `deploy-app.sh` of the same
project uses the volume again with the same database, and asks before deleting
it for a new empty one (see
[Initialize a fresh UAT system](#initialize-a-fresh-uat-system-on-the-app-server)).
With `--purge` the volume is removed as well (`down --volumes`), and so is each
image in `compose.yml` that no other container uses. The database is not touched.

Anonymous volumes are the unnamed volumes Docker creates for folders that the
image declares as volumes and `compose.yml` does not name, such as
`/mnt/extra-addons`. `down` keeps them, and after a redeploy so does
`down --volumes`, because Compose hands them to the new container by name. The
script lists them by Docker's `com.docker.volume.anonymous` label before it
removes the containers, and deletes them by name afterwards. If Docker cannot
delete one, for example because another container still uses it, the script
prints its name and Docker's error, and finishes the uninstall.

**Other Perodua containers.** After the uninstall, and also when `--dir` holds no
deployment, the script lists the containers whose name or image contains
"perodua" and that belong to another Compose project or to none. An example is
the App that the earlier perodua-odoo package deployed as the project
`perodua-odoo`. Each project is shown with the directory it was started from and
the name, image, state and ports of each container. It is deleted only after a
`y` on a terminal:
- a project with `docker compose --project-name NAME down --remove-orphans`, run
  from `/` so that Compose does not read a compose file in the current directory;
- containers without a project with `docker rm -f`.

A second question covers the volumes those containers mounted, including the
anonymous ones, and the project's other volumes. Enter keeps them. The images
stay, and the script prints the command that removes them. Without a terminal it
only lists the containers with the delete command. When `--dir` holds no
deployment, the script exits with 0 only if it deleted something.

`deploy-app.sh` holds a lock on `.deploy.lock` in the directory while it runs.
The uninstall takes the same lock before it lists anything and keeps it until it
exits. If a deployment is running, it stops with "A deployment is running in
this directory. Nothing was changed." A deployment started during the
uninstall, including while it waits for the project name, stops with "Another
deployment is running in this directory". The lock file is deleted last, when
nothing else is left in the directory.

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
  deployment uses it (PostgreSQL restarts; `127.0.0.1` stays), and with a
  Docker address on one server also the setting that starts PostgreSQL after
  Docker (`--purge` removes it as well);
- the deployment records, and `deploy.conf` if the guided setup wrote it. A
  hand-written `deploy.conf` is kept.

PostgreSQL stays installed. With `--purge`, and a typed `PURGE` on a terminal,
the script also uninstalls the PostgreSQL 16 packages and deletes all their data,
configuration and logs, which removes every database on the server. It first
checks that apt would remove only those packages, and refuses if another
PostgreSQL version or a dependent package would go too. Libraries pulled in by
the installation stay (`sudo apt autoremove` lists them).

## Two environments on one server, by path (`/dev`, `/uat`)

From v1.0.4, one App server can run two separate systems under one host name,
for example `https://stgissrp.perodua.com.my/dev/` and
`https://stgissrp.perodua.com.my/uat/`. Each one is a deployment of its own:
its own directory, Compose project, database, web port and release. They can
run different versions, and one can be stopped, reset or removed without the
other.

With `PUBLIC_ROOT=/dev`, the web container of that deployment serves the page
at `/dev/app/` and passes `/dev/uiux/`, `/dev/web/content/`, `/dev/web/image/`
and `/dev/report/pdf/` on to Odoo without the `/dev`. Every other path returns
404, including Odoo's own pages (`/dev/my`, `/dev/odoo`, `/dev/web/...`), which
do not work under a path, and the page without its path (`/app/`). In front of
the two deployments, F5 or an nginx on the server sends each path, unchanged,
to that deployment's web port.

### Settings

The three settings below are read only from the configuration file (`--config`),
from an `app.env` written beforehand in the empty deployment directory (see
[Initialize a fresh UAT system](#initialize-a-fresh-uat-system-on-the-app-server)),
and, on later runs, from the `app.env` there. The questions of a first
interactive run do not ask for them. Each run saves the ones that are
set in `app.env`. Empty is their default and is left out, so a deployment that
does not use them keeps an `app.env` that the scripts of v1.0.3 and earlier can
still read.

| Setting | Meaning |
| --- | --- |
| `PUBLIC_ROOT` | The path, such as `/dev`: a `/` followed by 1-31 lowercase letters, digits or `-`, without `/` at the end. Empty (the default): the page is at `/app/` as before. Needs v1.0.4 or later; the `scripts` folder of v1.0.3 stops with a message before it changes anything. |
| `ENVIRONMENT_LABEL` | The label on the sign-in page, such as `DEV` or `UAT`: up to 40 letters, digits, spaces and `. _ ( ) -`. Empty: the label the release has always shown. From v1.0.4; v1.0.3 prints a warning and ignores it. |
| `PUBLIC_BASE_URL` | The address browsers use, such as `https://stgiss.perodua.com.my/dev`, without `/` at the end. From v1.0.5 it must be the `https://` address of the sign-in host, or the sign-in hand-over stays off (see [Sign in once on stgiss](#sign-in-once-on-stgiss-v105)); `deploy-app.sh` of v1.0.5 or later warns otherwise. The host is a host name (labels of letters, digits and `-`, separated by single dots) or an IPv4 address. Its path must be `PUBLIC_ROOT`: with `PUBLIC_ROOT` empty it has no path. Empty: nothing is written, and Odoo sets its base URL (`web.base.url`) itself, from the address of each administrator sign-in. Behind the web container that address has no path (the container removes `PUBLIC_ROOT` before Odoo) and starts with `http://`, so with `PUBLIC_ROOT` set, set `PUBLIC_BASE_URL` too; `deploy-app.sh` prints a warning otherwise. |

Every run of `deploy-app.sh` writes these Odoo system parameters to the
database before the containers start, on a new and an existing database alike:

- `report.url` = `http://127.0.0.1:8069`, always. PDF reports then load their
  styles from Odoo itself, not through the front.
- with `PUBLIC_BASE_URL` set: `web.base.url` = that address, and
  `web.base.url.freeze` = `True`, so that an administrator's sign-in does not
  replace it with the address the request came through. Links Odoo builds, for
  example in e-mails, use it. Most of those links lead to Odoo's own pages
  (`/web`, `/odoo`, `/my`), which the v1.0.4 web container does not serve: they
  open a 404 page. Only the workbench under `/dev/app/` is served.

Clearing `PUBLIC_BASE_URL` later does not remove these two values; change them
under Settings > Technical > System Parameters if needed.

The two deployments differ in these settings (the ports are examples):

| Setting | `/dev` | `/uat` |
| --- | --- | --- |
| `--dir` | `/opt/perodua-dev` | `/opt/perodua-uat` |
| `PROJECT_NAME` | `perodua-dev` | `perodua-uat` |
| `DB_NAME` | `perodua_dev` | `perodua_uat` |
| `HTTP_PORT` | `8110` | `8111` |
| `PUBLIC_ROOT` | `/dev` | `/uat` |
| `PUBLIC_BASE_URL` | `https://stgiss.perodua.com.my/dev` | `https://stgiss.perodua.com.my/uat` |
| `ENVIRONMENT_LABEL` | `DEV` | `UAT` |

`BIND_IP` is `127.0.0.1` for both with an nginx on this server in front, or the
server's private address when F5 connects to the web ports directly.

### Deploy

1. On the DB server, create one empty database per environment with
   `deploy-db.sh`. Write one configuration file for each, with `DB_MODE=empty`
   and its own `DB_NAME` (copy `deploy.conf.example`, or the `deploy.conf` the
   guided setup saved), and run `sudo bash deploy-db.sh --config deploy-dev.conf`
   and `sudo bash deploy-db.sh --config deploy-uat.conf`. Both can use the same
   `DB_USER` with the same password, or a user each. Each database is recorded
   separately.
2. On the App server, keep one `scripts` folder per release, for example
   `/root/perodua-v1.0.8/scripts`. Write one configuration file per environment
   (see `app.env.example`), for example `/root/perodua-dev.env`:

   ```
   DB_HOST=192.168.1.20
   DB_PORT=5432
   DB_NAME=perodua_dev
   DB_USER=odoo
   DB_PASSWORD_FILE=/root/perodua-dev-db-password
   PROJECT_NAME=perodua-dev
   HTTP_PORT=8110
   BIND_IP=127.0.0.1
   STARTUP_TIMEOUT=600
   INIT_TIMEOUT=3600
   PUBLIC_ROOT=/dev
   ENVIRONMENT_LABEL=DEV
   PUBLIC_BASE_URL=https://stgiss.perodua.com.my/dev
   ```

3. Deploy each from the `scripts` folder of its release:

   ```sh
   sudo bash deploy-app.sh --config /root/perodua-dev.env --dir /opt/perodua-dev --init-db
   sudo bash deploy-app.sh --config /root/perodua-uat.env --dir /opt/perodua-uat --init-db
   ```

   Besides its usual checks, the script opens the page the way a browser behind
   the front does: `/dev/app/version.json`, `/dev/uiux/api/version` and the
   page itself, whose files must load from `/dev/app/assets/`. It also checks
   that `/dev/` leads to `/dev/app/` and that `/dev/my` and `/app/` return 404.
   At the end it prints the address on this server
   (`http://127.0.0.1:8110/dev/app/`, or through an SSH tunnel
   `http://localhost:8110/dev/app/`) and the public address. On this server
   and through the tunnel the page works as it does through the front,
   attachments and PDF reports included.

4. Set up the front, below.

### The front

- **F5 to the web ports directly:** one pool per path, `/dev/*` to
  `APP_SERVER:8110` and `/uat/*` to `APP_SERVER:8111`, with the path unchanged
  and the Host header passed on. Give each pool its own health check, for
  example `GET /dev/app/version.json` and `GET /uat/app/version.json` with the
  public host name.
- **nginx on this server:** [front-proxy.example.conf](front-proxy.example.conf)
  sends `/dev/` to `127.0.0.1:8110` and `/uat/` to `127.0.0.1:8111` with the
  path unchanged and the Host header passed on, allows uploads up to 128 MB and
  requests up to 720 seconds as the web container does, and returns 404 for
  everything else. Install nginx on the server itself: a container on a Docker
  network cannot reach ports published on `127.0.0.1`. F5, if any, then sends
  both paths to this nginx; its health check must still go through nginx to
  each path (`/dev/app/version.json`, `/uat/app/version.json`), because nginx
  answers while one environment is down. This nginx is not part of either
  deployment: `deploy-app.sh`, `service.sh` and `uninstall.sh` do not start,
  stop or remove it.

### Things to know

- **One release folder per environment.** `deploy-app.sh` deploys the release
  its `scripts` folder pins, and a deployment directory is bound to that
  release. Run each environment's `deploy-app.sh` and `service.sh reset` from
  the folder of the release it should run: a reset from another release's
  folder installs that release. To move an environment to another release and
  keep its data, run `deploy-app.sh --upgrade --dir` from that release's folder
  (see [Upgrade to a new release](#upgrade-to-a-new-release-keeping-the-data-deploy-appsh---upgrade)).
- **A reset checks the settings first.** `service.sh reset` has the
  `deploy-app.sh` next to it check `app.env` (`deploy-app.sh --check-config`)
  before it stops the App or deletes anything. A reset of a `/dev` environment
  from the v1.0.3 folder therefore stops with "PUBLIC_ROOT needs Client Stable
  UIUX v1.0.4 or later" and nothing is changed. The scripts of 26c70b7 and
  earlier have no such check and do not know the three settings: when
  `app.env` contains any of them, their reset empties the database first and
  then stops with "Unknown configuration key". Do not reset such an
  environment from those folders.
- **`--dir` on every command.** `service.sh` and `uninstall.sh` default to
  `/opt/perodua-app`; pass `--dir /opt/perodua-dev` or `--dir /opt/perodua-uat`.
- **Uninstalling one environment.** After `uninstall.sh --role app --dir
  /opt/perodua-uat`, the script lists the other environment's containers as
  "other Perodua containers" and asks whether to delete them. Answer `n` (or
  press Enter). Without a terminal it only lists them.
- **Both databases on one DB server.** Give `uninstall.sh --role db` the
  database to remove: `--database perodua_uat` (or that database's
  `--config`). Otherwise it takes the one in `deploy.conf` next to the script.
  `uninstall.sh --role db --purge` uninstalls PostgreSQL and deletes every
  database on the server, both environments' included. Stopping the DB server
  (`service.sh --role db stop`) takes both environments down.
- **Resources.** Two environments are two Odoo and two web containers, two
  attachments volumes and two databases. Size memory, CPU and disk on both
  servers for two systems.
- **One origin.** Under one host name the two environments are the same site
  for the browser. Each path keeps its own sign-in, but a page of one
  environment could call the other's API with the session signed in there.
  This is acceptable for test data. When UAT holds real data or real accounts,
  give it a host name of its own instead.

## Sign in once on stgiss (v1.0.5)

From v1.0.5, one Odoo serves four host names in different roles. The host table
in Odoo (system parameter `perodua_client_stable.hosts`; without it, the
release's default) names them. The table is JSON: a change of it in Odoo
changes the host names without a new release.

| Host name | Role | Shows |
| --- | --- | --- |
| `stgiss.perodua.com.my` | sign-in host (hub) | no module; after the sign-in, links to the modules the user may open |
| `stgissrp.perodua.com.my` | module host | Perodua SPD |
| `stgisssp.perodua.com.my` | module host | Supplier Portal |
| `stgisscp.perodua.com.my` | module host | Customer Portal |
| any other name: `localhost` (SSH tunnel), `127.0.0.1`, the self-check of `deploy-app.sh` | local | all three, with password sign-in as in v1.0.4 |

Each name shows only its own module, whether the hand-over below is on or not.

**The hand-over.** A user signs in one time, on stgiss. A module host without
a sign-in sends the browser to stgiss. stgiss asks for the password if needed
and sends the browser back with a one-time ticket, valid for 60 seconds. The
module host then has its own sign-in, and its cookie stays its own (host-only,
path `/dev/`). Odoo switches the hand-over on only when `web.base.url` is
frozen, starts with `https://`, names the sign-in host and has no port other
than 443. `deploy-app.sh` writes it from `PUBLIC_BASE_URL`, so:

- set `PUBLIC_BASE_URL=https://stgiss.perodua.com.my/dev` in the `app.env` of
  the grey environment (`/dev`), and `https://stgiss.perodua.com.my/uat` for a
  UAT environment at `/uat`. Then run `deploy-app.sh` (or `--upgrade`). Odoo
  takes the scheme and the path of the public addresses from this value, and
  the host names from the host table; never from a request;
- with an empty value, an `http://` value, another host name or a port other
  than 443, the hand-over stays off and every name keeps its own password
  sign-in. `deploy-app.sh` of v1.0.5 or later prints a warning, but goes on;
- the HTTPS front must send all four names to the environment's web port: the
  `/dev/` lines of [https-routes.conf.example](../scripts/https-routes.conf.example)
  do. A UAT environment needs one `/uat/` line for each of the four names. A
  front proxy or F5 in between must keep the `Host` header, the query string and
  absolute `Location` headers, and must not cache the redirects (303).

**What users and support see:**

- **Sign in at stgiss:** `https://stgiss.perodua.com.my/dev/app/`. A bookmark of
  a module address works as well: it sends the user to stgiss and back.
- **Module names send users to stgiss.** They show no sign-in form, and a
  password sign-in sent to a module name is refused.
- **One sign-out ends all names.** A sign-out on any name ends the sign-in on
  stgiss and on every module name, in all tabs, at their next request. A module
  name then sends the user to `stgiss.../app/?signed_out=1`. A new password
  sign-in of the same user, on another computer or browser, also ends the
  earlier sign-in on all names (one sign-in per user, as before).
- **Support** signs in at stgiss, or through the SSH tunnel
  (`ssh -N -L 8110:127.0.0.1:8110 USER@APP_SERVER`, then
  `http://localhost:8110/dev/app/`). The tunnel is a local name: it keeps the
  password sign-in and shows all three modules.

With `BIND_IP=0.0.0.0`, port 8110 also answers plain HTTP on the server's own
addresses, where the session cookies travel unencrypted. Prefer
`BIND_IP=127.0.0.1`, so that browsers reach the App only through the HTTPS
front. The stgiss session cookie now also signs in every module host name, so it
must never travel over plain HTTP. `https.sh` marks it `Secure` (and
`SameSite=Lax`), so that a browser does not send it to `http://` on port 80 or
on port 8110 of these names. `https.sh` sends no `Strict-Transport-Security`;
add it only when the customer decides that these names are HTTPS-only. The SSH
tunnel (`localhost`) and the server's IP address are other host names with
their own cookies: they keep working over HTTP.

## HTTPS certificate from Let's Encrypt

`https.sh` (see HTTPS in the [README](../README.md#https)) installs the
certificate that an issuer, such as the customer's own CA, signs for its
request (`csr`, then `install-cert`). `https.sh letsencrypt` gets the
certificate from Let's Encrypt instead, for the same routes table, key and
request, and a timer renews it. `apply`, `status` and `uninstall` work as
before.

### How the host names are checked

Let's Encrypt issues a certificate only for host names it has checked. The host
names of the App server point to a private address, so Let's Encrypt cannot
reach the server to check them over HTTP. It checks them through DNS instead
(the DNS-01 challenge): for each host name it looks up the TXT record
`_acme-challenge.HOST`, whose value changes at every issuance.

The customer's DNS is not changed by any script. Instead, the DNS
administrator points each `_acme-challenge.HOST` name once, with a CNAME
record, at a name of the acme-dns server `acmedns.novutal.com`, which answers
DNS queries for `acme.novutal.com`. At each issuance lego (the ACME client, run
in Docker) writes the TXT value to acme-dns, and Let's Encrypt follows the
CNAME to it. acme-dns can only change those TXT values, one account per host
name; it cannot change any other record of the customer's domain.

### First time

1. The routes table, as for any HTTPS setup (README, HTTPS, step 1). Every host
   name in it gets a record in step 3.
2. On the App server, from the `scripts` folder:

   ```bash
   sudo bash https.sh letsencrypt --accept-tos --email ops@example.com
   ```

   `--accept-tos` accepts the terms of service of Let's Encrypt; without it the
   script prints their address and stops. `--email` is optional. If there is no
   key and request yet, the script makes them as `csr` does. On this first run
   the script makes one account per host name on the acme-dns server, prints the
   records, one line per host name, and stops with exit status 3. It does not
   ask Let's Encrypt for anything yet:

   ```
   _acme-challenge.stgissrp.perodua.com.my CNAME 3f0c7a1e-5b2d-4c3e-9a8f-0d1e2f3a4b5c.acme.novutal.com
   ```

   `sudo bash https.sh status` prints them again. Before the first certificate,
   `status` looks them up only through this server's DNS (it takes no
   `--dns-resolvers`): on a server whose DNS gives an internal view, it can show
   NOT FOUND while the public records exist. Until then, the `letsencrypt` run
   itself is the check of the records: it looks them up with the DNS servers
   given and stops before it asks Let's Encrypt for anything.
3. The customer's DNS administrator creates these CNAME records once, in the
   DNS that the internet sees (the public view of `perodua.com.my`). If the App
   server's own DNS gives an internal view of these names, create them there
   too, or give the script DNS servers that see the public records with
   `--dns-resolvers`. If the domain has CAA records, they must allow
   `letsencrypt.org`.
4. Run the same command again. The script first looks the records up with `dig`:
   while one is missing or points elsewhere, it says which and stops again,
   without asking Let's Encrypt. When no DNS server answers for every record, it
   says which records each one did not answer and stops.
   With the records in place, lego gets the certificate, and the
   script installs it as `install-cert` does: it must belong to the key, cover
   every host name, be valid now and verify with its chain, and also lead to a
   root that this server trusts. If `apply` ran before, nginx is reloaded and
   must serve the new certificate, or the previous one comes back. The
   certificate, the record of it that the renewal uses and the options it came
   with change together: when a step fails or the run is interrupted, all of them
   come back. A power cut or a kill in the middle can leave the old certificate
   or the new one in place; the renewal knows either. Otherwise run
   `sudo bash https.sh apply` next.

Each run without `--renew` gets a new certificate at once. Let's Encrypt issues
at most 5 certificates for the same set of host names per week.

### Renewal

The first certificate also sets up the systemd timer `perodua-https-renew.timer`.
It runs every day between 02:00 and 06:00 (a random time), and after a boot if
the server was off at that time. It runs `https.sh letsencrypt --renew`, which
renews only when fewer than 30 days are left, with the same checks and nginx
reload. The records stay as they are, so the DNS administrator has nothing more
to do. Certificates from Let's Encrypt are at present valid for 90 days, so the
renewal comes about every 60 days; with shorter certificates it comes more often.

The timer runs a copy of the script, `/usr/local/lib/perodua-https/https.sh`,
so the downloaded `scripts` folder can be moved or deleted. After downloading a
newer `scripts` folder, run `sudo bash https.sh letsencrypt --renew` from it: it
updates the copy, and renews only if due.

Each run, the daily one included and whether or not a renewal is due, also
checks that nginx serves the installed certificate for every host name, once
`apply` has run. A power cut or a kill between the new certificate and nginx's
reload can leave nginx serving the previous one. The run then checks the files
with `nginx -t`, reloads nginx and says so. If `nginx -t` refuses the files, the
daily run fails and changes nothing, and `status` shows its last run as failed;
`sudo bash https.sh letsencrypt` then gets and installs a new certificate.

`status` shows the days left, whether the timer is active and whether its last
run failed; the log of the runs: `sudo journalctl -u perodua-https-renew.service`.

`--renew` renews only the certificate that `letsencrypt` installed. After
`install-cert` installs another one, for example from the customer's CA again,
the timer leaves it alone. `uninstall` removes the timer and the copy;
`uninstall --purge` also deletes `/etc/perodua-https/letsencrypt/`.

### What the App server needs

- Docker (`sudo bash install-dependencies.sh --role app`), running. The first
  run pulls lego (`goacme/lego` v5.5.2, pinned by its digest) from Docker Hub.
- Outbound HTTPS (port 443) to `acme-v02.api.letsencrypt.org` (Let's Encrypt),
  `acmedns.novutal.com` (acme-dns) and, for the first pull, Docker Hub
  (`registry-1.docker.io` and the hosts it downloads from). The script checks
  Let's Encrypt, acme-dns and Docker before it changes anything. Nothing needs
  to reach the App server from the internet.
- DNS look-ups of the `_acme-challenge` records through the server's DNS
  servers, or those of `--dns-resolvers`. With `--dns-resolvers`, the script
  asks them in their order and takes the first one that answers for every
  record; lego then checks on that one DNS server that the TXT record is
  visible, before Let's Encrypt checks it (lego checks each record on each server
  it is given, so a server that fails one record would stop it). lego does not
  query the authoritative servers of the domains, so outbound port 53 to the
  internet is needed only when `--dns-resolvers` names servers on the internet.
- `dig` (package `bind9-dnsutils`, part of `ubuntu-standard`; if `command -v dig`
  finds nothing: `sudo apt-get install bind9-dnsutils`). `letsencrypt` stops
  without it.

### The private key and public logs

- The private key is made on the App server and stays in
  `/etc/perodua-https/key.pem`. lego gets only the certificate request: its
  container mounts lego's own folder `/etc/perodua-https/letsencrypt/lego`, the
  copy of the accounts it needs and the request, and nothing else of the server:
  not the key, not `acme-dns.json`.
- `/etc/perodua-https/letsencrypt/` (root only) holds the acme-dns accounts with
  their passwords (`acme-dns.json`), the Let's Encrypt account and its key, and
  the certificates lego received. With `acme-dns.json`, anyone can get a
  certificate for these host names from a public CA: keep it as secret as the
  private key.
- Every certificate that a public CA issues, Let's Encrypt included, is
  published in the Certificate Transparency logs with all its host names, and
  anyone can search these logs (for example at crt.sh). The host names of the
  table therefore become public, although they point to a private address. A
  certificate from the customer's own CA is not published.

### Options

| Option | Meaning |
| --- | --- |
| `--accept-tos` | Accepts the terms of service of the ACME server; needed on the first run with each ACME server. |
| `--email ADDRESS` | Contact address for the ACME account, given when the account is made. |
| `--server URL` | The ACME directory. Default: Let's Encrypt, `https://acme-v02.api.letsencrypt.org/directory`. |
| `--acme-dns URL` | The acme-dns server. Default: `https://acmedns.novutal.com`. |
| `--dns-resolvers HOST[:PORT],...` | The DNS servers for the look-ups of the records, asked in their order; lego gets the first one that answered for every record. Default: the server's. `''` goes back to the default. |
| `--extra-root FILE` | A root certificate to trust, besides the server's, only when the new certificate is checked. For the staging test. |
| `--acme-ca FILE` | The CA of a local ACME test server's own HTTPS certificate, such as Pebble's. Never needed with Let's Encrypt. |
| `--renew` | Renews only when fewer than 30 days are left (what the timer runs). |

The options of the run that installed the certificate are kept in
`/etc/perodua-https/letsencrypt/settings`, and later runs and the timer use
them; an option given again replaces the kept value. `--extra-root` and
`--acme-ca` are kept only with the `--server` they were given with.

### The acme-dns accounts

The accounts are kept in `/etc/perodua-https/letsencrypt/acme-dns.json`, one
per host name, in the format lego reads:

```
{"HOST":{"fulldomain":"...","subdomain":"...","username":"...","password":"...","server_url":"..."},...}
```

- The script makes the account of a host name that has none (a new one in the
  table) and prints its record.
- A file made elsewhere, for example by the operator of the acme-dns server,
  with an account for every host name, can be put there before the first run
  (in a folder `/etc/perodua-https/letsencrypt` made with `sudo install -d -m 0700`).
  It is used as it is: the script makes no account, and only limits the file to
  root (0600). This is the way when the acme-dns server makes accounts only for
  its operator. The file may be spread over lines, hold the fields in any order
  and hold more of them (such as `allowfrom`). It must be exactly one JSON
  object: no trailing comma, nothing after it, each host name and each field
  once, and every account with a `server_url` that is the acme-dns server in
  use (`https://acmedns.novutal.com`). If a file from an older tool has no
  `server_url`, add `"server_url":"https://acmedns.novutal.com"` to each account.
  The script refuses any other file and leaves it as it is.
- lego never reads or writes `acme-dns.json`. It gets a copy of the accounts of
  the table's host names, which the script writes for each run and removes
  after it. If lego changes that copy, which it does when an account does not
  work for it, the script stops without installing anything.
- An account belongs to the acme-dns server that made it. To move to another
  acme-dns server, delete `acme-dns.json` and run
  `sudo bash https.sh letsencrypt --acme-dns URL`. It makes new accounts there,
  prints their records and stops. The DNS administrator puts the new records in
  place of the old ones; run the same command again to get the certificate.

### Limits

- If the process dies (a kill, a power cut) during a run that changes to another
  ACME server (`--server`, `--acme-ca`), while it replaces the kept settings and
  `acme-ca.pem`, the two may not match until the same `letsencrypt` command is
  run again. Until then the daily runs fail, and `status` shows the last run as
  failed.

### A test with the Let's Encrypt staging server

The staging server of Let's Encrypt issues test certificates that no browser
trusts. They lead to the staging roots listed at
<https://letsencrypt.org/docs/staging-environment/> (for this RSA key,
`letsencrypt-stg-root-x1.pem`, "(STAGING) Pretend Pear X1"). Download that root
to the server, then:

```bash
sudo bash https.sh letsencrypt --accept-tos \
    --server https://acme-staging-v02.api.letsencrypt.org/directory --extra-root letsencrypt-stg-root-x1.pem
```

`--extra-root` adds the root to the script's own check of the certificate only;
it is not added to the system or to nginx. To change to real certificates,
run `sudo bash https.sh letsencrypt --accept-tos --server https://acme-v02.api.letsencrypt.org/directory`:
the staging root is no longer used, and the acme-dns accounts and records stay
the same.

For a local test with Pebble, a local acme-dns and a test DNS server, the
environment variable `PERODUA_HTTPS_LEGO_NETWORK=NAME` runs lego on that Docker
network instead of the host's.

## Isolated verification

The integration suite uses real PostgreSQL 16 in a fresh Ubuntu 24.04 container.
It must never run on a production database host. See `tests/Dockerfile` and
`tests/run-deploy-db-integration.sh`; the test harness refuses to run without its
explicit disposable-container guard. Docker is needed only for this developer
test, not for server deployment.
