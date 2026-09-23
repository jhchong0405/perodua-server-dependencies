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
`postgresql.auto.conf`; password absence in output logs; and real HTTPS Basic-auth downloads
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
