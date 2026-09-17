# Two-server acceptance — 2026-09-18

Two fresh Ubuntu 24.04 amd64 KVM guests were tested from Windows/WSL. Each guest
had its own disk, kernel, machine ID, boot ID and LAN address. The App guest ran
Docker 29.8.1 / Compose 5.5.1; the DB guest ran native PostgreSQL 16.15. Neither
guest initially had Docker or PostgreSQL installed. No production stack was used.

## Tested inputs

- App release: Client Stable UIUX v1.0.0, revision `ec7c22386204371975ad79931f57c8091c07935d`.
- DB script baseline: `5324abc5e15965a9c14aff0dbd764a9c4cafd2a0`, plus the scoped
  maintenance-database HBA correction in this change.
- App/DB LAN addresses: `10.233.10.10` / `10.233.10.20`; ports `8110` / `5432`.
- A consistent custom-format database backup and matching filestore archive from
  the same release. Source locale `en_US.utf8` was generated before restoration.
- The normal App deployment path was used throughout, without `--init-db`.

The scripts actually executed in the guests match these SHA-256 values:

```text
15c5b77115c4923a408e669dac336626c882597ce824dd256e9163ec4230fdf1  deploy-app.sh
9e1ee970e0f391d12dfcbfe8cd78bc42965b392c3b554d335102a4b3274f3b57  deploy-db.sh
61eabeb995d0aa9455c7aa4b075d18e01b82b7739241d3b12cd5ea632ffae1a2  install-dependencies.sh
```

## Results

- Both role-specific dependency installations passed on fresh guests.
- Original DB restoration passed, but the first App deployment correctly failed:
  PostgreSQL denied the App role/source access to the `postgres` maintenance DB.
  The scoped HBA correction resolved the actual cross-guest connection failure.
- GHCR hidden-token login and both pinned image pulls passed.
- Paired filestore recovery and ordinary App deployment passed; only Web and Odoo
  were started on App. PostgreSQL sessions showed the separate App LAN address.
- Browser login and the OEM Material list passed. HTTP checks covered fresh
  login/logout, menus, matching frontend/backend revision and attachment upload/download.
- App container recreation, DB script rerun, App guest reboot and DB guest reboot
  each passed a fresh-login and attachment-read check. The other guest's boot ID
  stayed unchanged during each independent reboot; services started automatically.
- Database OID `16388` and 18,805 product records survived. A new test record,
  message and attachment retained the same IDs across all four recovery checks.
  Its 4,179-byte attachment retained SHA-256
  `1c0ff74c0c44c4944b7d0e1da8dcceb5ea9c60a7a45bfd429b78183dfb29fbf8`.
  Final readback confirmed the physical file on App and its relation in DB.
- The 13 App CLI regressions and 4 HBA-writer regressions passed separately.
- The README's exact filestore restore command was also run against a fresh,
  isolated volume with networking disabled. It restored 552 files, verified all
  551 content files against their SHA-1 filenames and set ownership to 100:101.
  A second invocation exited 1 without overwriting the restored directory.

An initial post-reboot check encountered a Windows WSL transport timeout; a
metadata collection also overlapped the DB reboot. These incomplete checks were
retained locally and replaced by a fully serial App reboot and successful HTTP
verification. Final verification used fresh logins, not session survival claims.

This is deployment acceptance on independent VMs, not a full business regression
or verification of a customer's physical network, cloud firewall, HTTPS or Oracle
integration. Database and filestore restoration are separate preparation steps;
the App script does not automatically transfer backups. Raw local evidence and
backups are excluded from this public repository.
