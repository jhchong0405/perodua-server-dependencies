# Client deployment self-check

For the default deployment. Replace `APP_SERVER_IP` with your App server address.
If your deployment uses a different project, path or port, use the values supplied at handover.
These checks do not restart or change the system.

## 1. App Server: are both services healthy?

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml ps -a
```

**PASS:** both `web` and `odoo` show `Up` and `(healthy)`.

## 2. App Server: can the App connect to the database?

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml exec -T odoo python3 /opt/deploy/preflight.py check
```

**PASS:** output is `READY` followed by a number, e.g. `READY 692`.
The number varies. `EMPTY`, `MISSING` or an error is not a pass.

## 3. DB Server: is PostgreSQL online?

```bash
pg_lsclusters
```

**PASS:** version `16`, cluster `main`, port `5432`, status `online`.

## 4. Your computer: can you use the App?

Open **`http://APP_SERVER_IP:8110/app/`** (or your assigned HTTPS URL).
Sign in, open a business page and download an existing attachment.
**PASS:** the page loads and the downloaded file opens correctly.

## If a check fails

Send the failed check's output and the relevant log excerpt to support.
Remove passwords, tokens and sensitive business data before sharing.

**Image pull failures:** the deployment first reuses an exact pinned digest from
the local cache. Downloads show Odoo/Web progress; a successful interactive
GHCR login is confirmed. Temporary network failures allow at most 3 total
attempts, with 3 seconds between attempts.
The printed diagnostic directory contains each image/attempt's exit code and
original Docker error with credentials redacted:

```bash
sudo ls -lt /opt/perodua-app.logs
```

Logs are retained under `run-*`; a custom `--dir` uses `<DEPLOY_DIR>.logs/run-*`.
Share the failed attempt's log. Tokens are excluded from saved diagnostics and
temporary registry credentials are cleared when deployment exits.

**Missing filestore directory:** if restoration stops at `chown: cannot access`,
compare `DB_NAME` in the DB server's `deploy.conf`, the App server's
`/opt/perodua-app/app.env`, and the `filestore/<database-name>/` archive directory.
All three names must match. Inspect the archive without extracting it:

```bash
tar -tzf /path/to/filestore.tar.gz | sed -n '1,10p'
```

Use the actual archive path. Keep existing data and stop repeated extraction
attempts until the mismatch is resolved. Follow the separate filestore restore
step in [README.md](README.md).

**App Server:**

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml logs --no-color --tail 50 odoo web
```

**DB Server:**

```bash
sudo tail -n 50 /var/log/postgresql/postgresql-16-main.log
```
