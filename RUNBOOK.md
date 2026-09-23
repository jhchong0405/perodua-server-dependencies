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
`SETUP_PENDING` or `SETUP_UNMARKED` means a fresh UAT initialization did not
finish; rerun `sudo bash deploy-app.sh --init-db` to complete it.

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

**App Server:**

```bash
sudo docker compose -p perodua-client-uiux -f /opt/perodua-app/compose.yml logs --no-color --tail 50 odoo web
```

**DB Server:**

```bash
sudo tail -n 50 /var/log/postgresql/postgresql-16-main.log
```
