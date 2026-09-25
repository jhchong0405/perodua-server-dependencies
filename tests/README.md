# Deployment checks

App script and scoped database-access regressions (standard-library Python;
temporary files and a fake Docker CLI, no real deployment):

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The App cases cover argument/configuration validation, literal value handling,
hidden registry prompts, credential cleanup and registry error handling.

`test_deploy_app_uat.py` drives `deploy-app.sh` against a scripted fake Docker
and checks the order and absence of steps: an initialized (`READY`) database is
never reinitialized, upgraded (`-u`) or given new UAT passwords, even with
`--init-db`; a refused dependency guard stops before any module is installed; a
missing database without `CREATEDB` points to `deploy-db.sh DB_MODE=empty`; an
interrupted fresh initialization (`SETUP_PENDING` or `SETUP_UNMARKED`) finishes
without reinstalling modules; a failed sign-in check leaves the database
unstamped; the database password never appears on a command line; and the
helper files are installed read-only and mounted into the container. It also
checks static properties of `uat_admins.py` (ORM password writes, groups copied
from `base.user_admin`, one commit after all checks).

`test_deploy_db_guided.py` answers the guided setup of `deploy-db.sh` on a
pseudo-terminal with `--check-config`, so nothing is deployed. It checks:
- the saved `deploy.conf` and its permissions, and that a later run asks nothing;
- asking again after an unknown menu choice, an address that is not on this host,
  an address that is not IPv4, or a declined summary;
- a CIDR for the App side;
- Docker network addresses neither offered nor accepted as the internal IP;
- the App on this server: Docker's bridge address as `DB_LISTEN_IP`, Docker's
  default networks as `APP_CIDR` (a comma-separated list the configuration check
  accepts), and no address questions; without Docker, the reason and the
  question again;
- this server's own address or `127.0.0.1` as the App address offers the
  one-server setup, and "no" asks for the App server's IP again;
- no file written when a restore is chosen, no terminal is available,
  `--config` is given or PostgreSQL is not installed.

Docker is a stub that reports Docker's multi-line output. The public-address
warning needs a public address on the host, so it was checked on a real server
instead. The test needs one non-loopback IPv4 address on the host and is skipped
without one (for example inside `docker run --network none`). `test_deploy_app.py` also
checks the first interactive `deploy-app.sh` run, with a stub `hostname` that
gives the host one private and one public address:
- it asks for the web port and saves it;
- a database host of `127.0.0.1` is explained and asked again, and in a
  configuration file it stops before any Docker call;
- who may open the page: the private network is the default, this server only
  binds `127.0.0.1`, a wrong number is asked again, and every address on a
  server with a public address needs a "y" (a configuration file gets a
  warning instead).

`test_deploy_app_uat.py` also covers an attachments volume from an earlier
deployment of the project. Files in it stop a new system without a terminal
(with the delete command), and on a terminal they are deleted only after "y".
An empty one is used as is. In a new directory, a volume that an uninstall kept
is used again with the same database; other project resources still stop it.

`test_uninstall_app.py` runs `uninstall.sh --role app` against a fake Docker CLI.
The fake keeps containers, volumes and networks in a file, including those of
another Compose project, answers only the commands and filters the script uses,
and fails on anything else. It also records, for each call, whether the
deployment lock (`.deploy.lock`) was held. A fake `rm` records the same, and
what is left in the directory when the lock file is removed. The tests check
the exact Docker calls and what is left:
- the default removes the containers, network and directory and the containers'
  anonymous volumes, and keeps the attachments volume, the images and the other
  project's resources;
- `--purge` adds `--volumes` and removes the images; the anonymous volume that
  `down --volumes` keeps after a redeploy is deleted by name, and the one it
  deleted itself is not reported;
- an anonymous volume that another container uses, or that Docker cannot
  delete, is kept and named with Docker's error, and the uninstall finishes;
- without a compose file, the labelled resources and the anonymous volumes are
  removed, and with `--purge` the project's named volumes too;
- containers without anonymous volumes lose no volume, and with no containers
  left no volume is looked up;
- nothing changes if the volumes cannot be read before the plan, after a wrong
  confirmation, with no terminal and no `--confirm`, or in a directory
  `deploy-app.sh` did not create, where no lock file is created either;
- while another process holds the lock the way `deploy-app.sh` does, the
  uninstall stops before any Docker call, with or without `--purge`, and the
  directory stays;
- if the lock file is deleted, or deleted and created again, between the
  script's open and its lock (another uninstall finished, then a deployment
  started), the lock on the old file does not count: the uninstall stops before
  any Docker call;
- `deploy-app.sh` started while the uninstall waits for the project name on a
  pseudo-terminal stops with "Another deployment is running in this directory"
  and changes nothing, and the uninstall then finishes;
- after every run, each changing Docker call and each removal found the lock
  held, including the image removals of `--purge`, and the lock file was removed
  last, when nothing else was left in the directory.

It needs root on Ubuntu 24.04, so run it in the test image. The fake follows
what a real engine did on 2026-09-24 (Docker 29, Compose v5): after
`up --force-recreate`, Compose passes the old anonymous volume to the new
container by name, and neither `compose rm -v` nor `down --volumes` deletes it.

`test_service_app.py` runs `service.sh --role app` against a smaller fake
Docker CLI of the same kind, with a fake `deploy-app.sh` next to a copy of the
script. It checks:
- `stop`, `start` and `restart` make only their Compose calls, each with the
  deployment lock held, and `status` works while another process holds it;
- `start` and `restart` run the database check first and change nothing when
  it reports `EMPTY` or `SETUP_PENDING`, or fails;
- that check does not read stdin, so `service.sh` also works in a script fed
  on stdin (on a real server, it had read the rest of such a script);
- while `deploy-app.sh` runs, every action except `status` stops before any
  Docker call;
- `reset` lists the anonymous volumes, stops the App, sends `reset_database.py`
  to the Odoo container on standard input, removes the containers and volumes,
  deletes the anonymous volumes by name, binds the directory to the release next
  to it and runs `deploy-app.sh --dir DIR --init-db` last, with the lock free;
- nothing changes after a wrong confirmation, without a terminal and without
  `--confirm`, or without `app.env`;
- a failed wipe stops before the containers and the attachments volume go, and
  says whether anything was deleted (exit 3 of `reset_database.py`: nothing).

`test_uat_guard.py` runs `uat_guard.py` against temporary addon trees shaped
like the pinned image (literal manifests, code shipped as bare `.pyc`): direct,
transitive and `auto_install` dependencies on `perodua_demo_client`, XML IDs,
imports, paths into its folder, the settings field that installs it, and its
name in SQL, data files, spreadsheets and compiled code; `auto_install=True` and
`auto_install=[]` (which Odoo 19 treats as "always install"); the module named in
the requested list; prose mentions that are not references; reviewed references
pinned to file hashes; addons-path shadowing; and inputs it cannot inspect,
including an unexpected error, which must stop the initialization (exit 2).
See [the two-server acceptance record](TWO-SERVER-2026-09-18.md) for the real
Ubuntu VM deployment, attachment persistence and separate reboot checks.

## Database deployment integration checks

The scoped App maintenance-database rule has a separate, non-destructive regression:

```bash
python3 -m unittest discover -s tests -p test_app_maintenance_hba.py -v
```

It executes the script's HBA writer against temporary files, checking the exact
role/source/SCRAM scope, unchanged existing rules, repeat-run idempotence, source
replacement and removal when `APP_CIDR` is cleared. It does not start PostgreSQL
or establish network connectivity; verify those from the App Server separately.

These tests install **real PostgreSQL 16 on Ubuntu 24.04** in a disposable Docker
image, generate custom-format fixture backups, and invoke `deploy-db.sh` with the
same configuration and secret-file interfaces used for deployment. They do not
mock PostgreSQL, `pg_restore`, or authentication.

Run from the repository root:

```bash
docker build -t perodua-deploy-db-test -f tests/Dockerfile tests
docker run --rm --network none \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  perodua-deploy-db-test
```

For Windows PowerShell, put the `docker run` command on one line. `$PWD` supplies
the current repository path in both shells.

The source is mounted read-only. PostgreSQL data, generated secrets, fixtures,
and logs stay in the container. The harness refuses to run outside a root-owned
Docker test environment and requires an initially empty test cluster. **Do not
mount any existing database data or host service directories into this image.**

Coverage includes successful restoration of extensions, tables, sequences,
views and SQL functions; ownership and application privileges; TCP password
authentication; startup from a stopped cluster; idempotent reruns that preserve
subsequent writes; interrupted-publication recovery; existing-database protection; wrong passwords; invalid hashes;
invalid archives and corrupt compressed payloads; failure before publication and recovery with a valid
backup; missing expected tables; configuration injection rejection; secret-file
permissions; literal SQL/HBA keyword names and access isolation; nonlocal listener
refusal; a real listener update and restart; configuration rollback on a conflicting
`postgresql.auto.conf`; password absence in output logs; interactive database
password entry on a pseudo-terminal that asks again after a too-short password
or two different entries and never echoes it; and real HTTPS Basic-auth downloads
with correct and incorrect passwords supplied through a pseudo-terminal. The
HTTPS test generates a temporary certificate trusted only inside its disposable
container and serves the backup over loopback.

`DB_MODE=empty` coverage: configuration without backup, checksum or
`EXPECTED_TABLES`, and refusal of leftover backup settings; database owner,
encoding, locales, `PUBLIC` access and TCP/SCRAM login; a role that is not
superuser, `CREATEDB` or `CREATEROLE`; a rerun after simulated App
initialization that keeps the OID, tables and data; interrupted-publication
recovery; refusal of a wrong password, different locales, a changed owner, an
unrelated same-name database and a same-name database with matching owner and
locales that the script did not create; cross-mode reruns in both directions;
an existing `CREATEDB` role; and a failed verification that keeps staging and
succeeds on retry.

On the empty-mode database, `check_app_preflight.py` then runs the exact
`preflight.py` text that `deploy-app.sh` generates, over TCP as the App role:
`EMPTY`, `SETUP_UNMARKED`, `SETUP_PENDING` and `READY` states; the fresh-state
marker is actually committed; an installed `perodua_demo_client`, records
loaded under its name, demo data and modules from another release are refused;
a stamped database is never marked or stamped again; and a restored database
keeps its previous check, which requires the dataset module.

`uninstall.sh --role db` coverage:
- removing a deployment only after an exact confirmation and with no open
  connection: the database, the role and its password, the access rules
  (validated), the listen address and the records. A hand-written config is
  kept, and a fresh deploy with a new password succeeds afterwards;
- keeping a role and a listen address that another deployment uses, and deleting
  a guided `deploy.conf`;
- refusing a database the script did not create, or one replaced since.

`service.sh` coverage, on real PostgreSQL:
- `check_reset_database.py` fills an empty-mode database as the App role the way
  Odoo does (900 tables with `create_uid`/`write_uid` foreign keys to
  `res_users`, tables that inherit another, an `api` schema with views, a free
  sequence, a function). As a control, dropping it in one transaction fails with
  "out of shared memory". `reset_database.py` exits 3 on a wrong password and
  changes nothing; with the right one it empties the database, `public` is as in
  a new database (owner `pg_database_owner`, `USAGE` for everyone, the standard
  comment), the database, its owner and who may connect are unchanged, the role
  can create tables again, a second run passes, and the generated `preflight.py`
  reports `EMPTY`;
- `--role db`: `status` shows the cluster online; `stop` closes an open
  connection and says so, the cluster is down and a second `stop` says it
  already is; `start` and `restart` bring it back with the same databases.

One server: with a stub Docker that reports the listen address as Docker's,
`deploy-db.sh` writes the setting that starts PostgreSQL after Docker, and a
rerun keeps exactly one. The final message points to `deploy-app.sh` on this
server. `uninstall.sh --role db` lists the setting and removes it with the
listen address. systemd is not PID 1 in the container, so an actual boot order
was checked on a real server.

The last check, `check-uninstall-purge.py`, runs `--purge` on a pseudo-terminal:
a wrong answer changes nothing, and `PURGE` removes the PostgreSQL 16 packages,
data and records. It uninstalls PostgreSQL, so nothing can run after it.

The container has no systemd PID 1. This exercises the `pg_ctlcluster` startup
fallback, **not** host reboot persistence, systemd unit enablement, external App
Server connectivity, or a particular cloud provider. Those require separate
deployment acceptance checks.

## GitHub delivery with a separately transferred backup

Follow the download and DB Server steps in [README.md](../README.md) in a fresh
Ubuntu 24.04 environment. Retrieve the
repository from GitHub at the exact commit under test, run that downloaded
`install-dependencies.sh --role db`, and transfer the backup separately to a path
outside the repository. Do not mount a local checkout or use this suite's
PostgreSQL test image for that acceptance run. Only curl and CA certificates
should be bootstrapped before the installer; preinstalling Python or PostgreSQL
would hide missing installer dependencies.

For independently prepared, read-only source-snapshot metadata,
`snapshot_audit.py` checks all logical table contents/counts, schema definitions,
modules and target role privileges. `check_archive_sequences.py` compares the
restored sequence values against the custom archive's SEQUENCE SET entries.
These optional audit tools are standard-library Python and PostgreSQL clients;
they are not needed for deployment. Never commit the dump, source audit metadata,
real configuration or passwords to this repository. No-writer assumptions and
the shared-source-snapshot contract are documented in `snapshot_audit.py`.
