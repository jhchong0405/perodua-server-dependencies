# Deployment checks

App script and scoped database-access regressions (standard-library Python;
temporary files and a fake Docker CLI, no real deployment):

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The App cases cover argument/configuration validation, literal value handling,
hidden GHCR prompts, credential cleanup and registry error handling.
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

The container has no systemd PID 1. This exercises the `pg_ctlcluster` startup
fallback, **not** host reboot persistence, systemd unit enablement, external App
Server connectivity, or a particular cloud provider. Those require separate
deployment acceptance checks.

## GitHub delivery with a separately transferred backup

Follow `GITHUB-DEPLOY.zh-CN.md` in a fresh Ubuntu 24.04 environment. Retrieve the
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
