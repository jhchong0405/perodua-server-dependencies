# Deployment checks

App script and scoped database-access regressions (standard-library Python;
temporary files and a fake Docker CLI, no real deployment):

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The App cases cover argument/configuration validation, literal value handling,
hidden registry prompts, credential cleanup and registry error handling. The
error traps of `deploy-app.sh`, `deploy-db.sh` and `install-dependencies.sh` report
only failures the main shell meets: a command that fails inside `$( )` or `<( )`
while the script goes on prints nothing, and a real failure (also inside a function
or an assignment from `$( )`) prints one "failed at line" message. The first
interactive run below prints none before the fixture stops it, whether or not
`deploy-db.sh` ever ran on the test host.

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

The Odoo command. `test_deploy_app.py` checks that the command of the Odoo
service in `compose.yml` is `odoo -c /etc/odoo/odoo.conf -d DB --proxy-mode
--workers=0 --limit-time-real-cron=900`: one cron pass may run 900 seconds,
not Odoo's default of 120 (owner decision of 2026-11-08, for the 17:00 Back
Order POs run).

The page under a path (`PUBLIC_ROOT`, `ENVIRONMENT_LABEL`, `PUBLIC_BASE_URL`).
The tests run a copy of `deploy-app.sh` with `PUBLIC_ROOT_SUPPORTED` set to 1
or 0, as a release that does or does not support it. `test_deploy_app.py`
checks:
- the three settings are saved in `app.env` when set, and a later run from
  `app.env` alone saves the same file and `compose.yml`; the web service, and
  not the Odoo service, gets `PUBLIC_ROOT` and `ENVIRONMENT_LABEL`;
- empty ones are left out, so `app.env` has only the keys the scripts of
  v1.0.3 and earlier accept, and clearing one removes it;
- invalid values (paths with a second segment, a trailing `/`, capitals or
  shell syntax; labels over 40 characters or with other characters; base URLs
  with a trailing `/`, another scheme, a user, a query, a bad port or two path
  segments; hosts that are not host names, such as `-`, `a..b`, `a-.b`, a
  trailing dot, a label over 63 characters or a name over 253) and a base URL
  whose path is not `PUBLIC_ROOT` (including `https://dev` for `/dev`) stop
  before any Docker call, and nothing runs;
- `PUBLIC_ROOT` without `PUBLIC_BASE_URL` prints a warning;
- `--check-config` checks `--config` or the directory's `app.env` without any
  Docker call or file change, refuses what a deployment refuses (such as
  `PUBLIC_ROOT` on a release without support, or an unknown key), and without
  settings to check it stops instead of asking;
- a release without support refuses `PUBLIC_ROOT` before any Docker call and
  only warns about a label; the pinned release supports `PUBLIC_ROOT` exactly
  when it is v1.0.4 or later.

The sign-in hand-over of v1.0.5. The tests run a copy of `deploy-app.sh` with
`HANDOVER_SUPPORTED` set to 1 or 0. `test_deploy_app.py` checks:
- an empty `PUBLIC_BASE_URL`, an `http://` one, one on another host than
  `stgiss.perodua.com.my` and one with a port other than 443 each print their
  warning with the value to set (`https://stgiss.perodua.com.my` and
  `PUBLIC_ROOT`), in `--check-config` and in a deployment; the settings stay
  valid and nothing changes;
- `https://stgiss.perodua.com.my/dev`, in any case of letters and also with
  `:443`, prints none, and a release without the hand-over prints none;
- the pinned release has `HANDOVER_SUPPORTED=1` exactly when it is v1.0.5 or
  later, and `--help` names the pinned release.

Outgoing email (v1.2.2). The tests run a copy of `deploy-app.sh` with
`MAIL_SUPPORTED` set to 1 or 0, as a release that does or does not have the
mail code. Every SMTP password in them is the fake `fake-app-password`, and no
test connects to an SMTP server: the fake Docker answers `perodua-mail-test`.
`test_deploy_app.py` checks:
- the pinned release has `MAIL_SUPPORTED=1` exactly when it is v1.2.2 or later;
  with 0, each mail setting (also with `--check-config`), `--mail` and
  `--send-test-mail` stop before any Docker call with "needs client-stable-uiux
  v1.2.2 or later";
- the settings of a configuration are saved in `app.env`, with
  `SMTP_PASSWORD_FILE` naming `secrets/smtp_password`, and given to the Odoo
  service only: the `PERODUA_SMTP_*` lines, `PERODUA_MAIL_REDIRECT_TO`,
  `secrets: [db_password, smtp_password]` and the top-level secret. A later run
  from `app.env` writes the same files. The secret rules of `iss-api.sh`: the
  password is in no Docker argument and not in `compose.yml`, `app.env`,
  `.deployment-identity` or the output; the secret is 0444 in the 0700
  `secrets` folder; `app.env` is 0600, also after an `app.env` written by hand
  with 0644;
- the encryption gives the default port (587, 465 or 25); without a user there
  is no user line, password file or secret; without mail settings, `app.env`
  holds only the keys of v1.0.3 and `app.env` and `compose.yml` are the same,
  byte for byte, as the release without mail writes them; clearing the settings
  removes them and the saved password;
- invalid values, a missing `SMTP_FROM` or `MAIL_REDIRECT_TO`, `none` with a
  user, a user without `SMTP_PASSWORD_FILE` in a non-interactive run, a
  relative or missing password file or a folder, a password file without a
  user and any mail setting without `SMTP_HOST` stop before any Docker call,
  and shell syntax in a value never runs;
- the table of values that the image's entrypoint also accepts or refuses (host
  names, addresses, sender names): through `mail_problem` itself (also the
  empty host and an address with a line break) and through `--check-config`;
- a sign-in user with a byte from 0x80 up (UTF-8 or not) stops the run and
  `--check-config` before any Docker call or file;
- `--check-config` refuses an SMTP password file that is empty or holds a line
  break, also the directory's own `secrets/smtp_password`;
- on a pseudo-terminal: the first run asks `Configure outgoing email now?` after
  the page question (a release without mail does not), then the questions in
  their order, with echo off for the password. A wrong host, menu number, port
  or sender is asked again; `off` is not a test address; real mail needs `SEND
  REAL MAIL` typed exactly; None asks for no user or password. `--mail` asks
  again with the saved values as defaults: another encryption gives its port,
  Enter keeps the saved password, `-` empties the sender name, and a new
  password replaces the secret. On a deployment that signs in, `-` at the user
  or None removes the user, `SMTP_PASSWORD_FILE`, the `smtp_password` secret
  and the saved password. Each of the three password questions keeps the
  spaces at the start and the end. It can switch mail off, and needs a terminal
  and a plain run;
- `--send-test-mail` runs only `compose run --rm --no-deps -T odoo
  perodua-mail-test ADDRESS` with the directory's compose file, names the test
  address that gets the email, reports a failure of the helper, and refuses a
  bad address, other options, a directory without a deployment, a deployment
  without mail and a first run that never used its database.

`test_deploy_app_uat.py` checks that the mail settings add no container step
and that the final message names them. `test_deploy_app_upgrade.py` checks that
`--upgrade` copies the SMTP password next to the staged `compose.yml` of the new
release. `test_uninstall_app.py` checks that the plan names the SMTP password
when the directory has one.

`test_deploy_app_uat.py` checks that `preflight.py public-urls` runs once on
every path (an initialized database with and without `--init-db`, a fresh
initialization, `SETUP_PENDING`, `SETUP_UNMARKED`), after the fresh checks and
right before `up`, with `PUBLIC_BASE_URL` as its argument (an empty argument
without one); that its failure stops before `up`; that the HTTP verification is
given `PUBLIC_ROOT`; that the web health check sends `Host: web`; and the
addresses in the final message. It also runs the
HTTP verification that `deploy-app.sh` sends to the Odoo container, unchanged,
against a fake web container on `127.0.0.1` that it reaches as an HTTP proxy.
The fake sees the host names the requests carry and answers as the web
container's nginx does: `Host: web` is the internal server, every other name
(`127.0.0.1` and `localhost` included) the public one. Every request goes to
`web`. A page under `/dev` and a page at the root
(as v1.0.3 serves it) pass; open `/dev/my` or `/app/` (200, or a redirect,
which is read and not followed), a page loading files from `/app/assets/`, a
leftover build placeholder, another revision, a missing or non-JSON answer and
a wrong redirect of `/dev/` fail with their own message.

`test_deploy_app_uat.py` also covers an attachments volume from an earlier
deployment of the project. Files in it stop a new system without a terminal
(with the delete command), and on a terminal they are deleted only after "y".
An empty one is used as is. In a new directory, a volume that an uninstall kept
is used again with the same database; other project resources still stop it.

`test_deploy_app_upgrade.py` runs `deploy-app.sh --upgrade` in a directory that
an earlier release (`client-stable-uiux-v1.0.3`) left, against a scripted fake
Docker. For every call the fake records the compose file it got (the
directory's own, or the staged one of the new release) and the release that
`.deployment-identity` named at that moment. It checks:
- another project, database server, port, database or user in `app.env` is
  refused, with the differing value named, before any pull or container; so is
  the release the directory already runs, a first deployment that never used
  its database, a directory without a deployment or `app.env`, `--init-db`,
  `--check-config` and a relative `--backup-dir` with `--upgrade`, and
  `--backup-dir` without it. Each time the identity, `compose.yml` and
  `app.env` stay as they were, and the message says that nothing was changed;
- a module fingerprint that differs (and is not the one a module upgrade
  starts from) is refused after the new image's database check
  (`upgrade-check`), with the fingerprint it upgrades from, and so is a database that
  is not `READY` or cannot be checked: the App is not stopped and no backup
  folder is made;
- a plain run in a directory of another release points to `--upgrade`;
- a failed `pg_dump`, a dump without `ir_module_module` data and a failed
  attachments archive stop before the switch: the incomplete backup folder is
  deleted, the old App is started again from its own `compose.yml` with
  `up --no-recreate` (not when it was not running before; when it does not
  start, the message gives the `service.sh start` command), and the directory
  keeps its identity and files;
- a lost SSH session while `pg_dump` runs (SIGHUP to the process group, or to
  the script only while the dump ends by itself) does the same, with exit
  status 129, also when standard error takes nothing more (`/dev/full`, as a
  hung-up terminal). Its `app.env` names the sign-in host, so that no
  hand-over warning goes to that standard error before the backup;
- a backup that may not fit (the database and attachment sizes from the staged
  size check, against the free space of the backup folder's disk), or sizes
  that cannot be read, are refused before the App stops; nothing changes;
- `pg_dump` and the archive run with an extra compose file that turns off the
  container log (`logging: driver: none`); no other run has it, and the
  directory's `compose.yml` keeps the logs;
- the order: two pulls, the staged database check, the staged size check,
  whether the App runs, its stop, `pg_dump`, `pg_restore --list` and the archive (all staged, all while
  the directory names the old release), then the switch and the usual
  deployment, all with the new identity. No step has `-u` or `-i`, and no
  argument holds the password;
- after the upgrade, `.deployment-identity` differs from the old one only in
  `release=` and `revision=`, `app.env` and the password are unchanged, and
  `compose.yml` names the new image and the same attachments volume;
- a demo-uiux directory of the release in `NO_IMAGE_MAIL_RELEASES`
  (v0.55.0) with `IMAGE_MAIL=on`, on a database that the check finds with the
  Odoo modules of the pinned release (`READY`), moves to it with no `-u`, and
  its new `compose.yml` has the mail of the image; its backup saves `app.env`
  without `IMAGE_MAIL` and `MAIL_REDIRECT_TO` (mode 0600), which the scripts
  folder of that release refuses, and `restore.txt` says so; a release that
  knows `IMAGE_MAIL` (not in `NO_IMAGE_MAIL_RELEASES`) gets `app.env` back
  byte for byte. The module upgrade of the demo-uiux line is in
  `test_deploy_app_module_upgrade.py` (below);
- the backup folder (`backups/TIME-OLD_RELEASE`, 0700, or under `--backup-dir`)
  holds the dump, the archive, the old identity and `app.env`, and
  `restore.txt`, which is printed as well and names each restore command: step
  4 also deletes the Odoo sessions and says that every user signs in again;
  step 5 copies the old identity and `app.env` back and deploys without
  `--upgrade`. Before step 1 it names the scripts folder of the upgrade for
  steps 1 to 4, says that each command must end with exit status 0 and that
  the next step waits for it, what success looks like (step 3 with its
  Docker lines and the perl warnings), that the 7 `perl: warning` lines of
  step 3, shown two times, are harmless (the test compares them with the
  output of a restore with v1.0.8), the down time with the size of the
  database and of the attachments before the backup, and that the HTTPS
  front stays; the 7 lines are compared byte for byte, with their tabs; step
  3 gives the lock numbers of the check and, when the room at the backup was
  too small, the commands that set 128 or more on the DB server; else it says
  that the room was enough and names no value. A DB server at 256 gets no
  value to set when it has the room, and 303 when 40 connections need it:
  never a value lower than it has;
- the check prints `Locks for the way back:` with the numbers of
  `RESTORE_LOCKS`; with the same modules, a lock table too small for
  `restore.txt` step 3 is a warning with the value to set (128, or 234 for a
  database that needs 234 locks per connection), and the upgrade goes on;
  nothing says that step 3 "would fail";
- a failure after the switch (recording the addresses, the HTTP check) says
  that the directory names the new release, whether the App is stopped or the
  containers were recreated, and how to finish, go back and restore; the
  printed copies, followed by a plain run of a `deploy-app.sh` that pins the old
  release, bring the directory back (as a v1.0.3 folder, which has no
  `--upgrade` and refuses the directory before the copies);
- `--upgrade` from a folder of another release with the same modules moves the
  directory there again, with a second backup.

`test_deploy_app_module_upgrade.py` runs `deploy-app.sh --upgrade` with a module
upgrade (`-u`) against the same fake Docker, in a directory of v1.2.1. The fake
keeps the database's module fingerprint in a file (`from`, `upgrading`,
`upgraded`, `new`) and answers each preflight mode as the real one does; the
directory's `preflight.py` is the old release's until the switch, and accepts
only `from`. It checks:
- the order: the pulls, `upgrade-check`, `uat_guard.py guard` and `demo-flag`,
  the backup, `upgrade-check` again, `mark-upgrading`, the removal of the old
  containers (`rm --stop --force odoo web`, the directory's own compose
  file), `docker rm -f` of the
  one-off container, one `-u` run, `verify-upgrade`, then the switch and the
  deployment with `check --accept-pending` and `stamp-upgrade` at the end;
- the `-u` run: `bash -c`, `--name PROJECT-module-upgrade`, the modules and the
  demo flag as `-e` values, `--max-cron-threads=0`, `--stop-after-init`, the
  migration log handler, no `-i`, no `--limit-time-real-cron` (only the App's
  cron thread gets 900 seconds), the new compose file, and its log in the
  backup folder; no argument holds the password;
- the cron and mail lines of `verify-upgrade` come before `up`. Between the
  stop of the App and `verify-upgrade` there are only one-off runs, the
  removal of the old containers and the one `-u` run (no cron thread, stops
  after the upgrade), so no mail queue runs before the hold. The final message
  gives the held mail as two numbers (the mail that the upgrade queued, and
  the mail that waited in the queue before it), names `--release-queued-mail`
  and names the README step that lists the held mail with `psql`: the test
  reads `README.md` and checks that this section and step exist, that its
  list selects by the state and the reason of the hold, and that the lines it
  quotes are those of the script. An answer of `verify-upgrade` or
  `release-mail` without the two numbers is refused. `restore.txt` names
  the module upgrade and the point of no return;
- the accepted old release (v1.2.1) goes
  through; v1.0.8, v1.2.0, another fingerprint, every refusal of `upgrade-check` (the
  lock table of the way back included), a lock count that cannot be read,
  retired data without `--drop-retired-data`, a refusal of the guard and a
  missing or wrong confirmation stop before the App stops, and nothing changes;
  the lock numbers are in the output and in step 3 of `restore.txt`. A
  refusal for the lock table is followed by the numbers and the five steps
  that set 128 on a DB server below it (with the `--dir` of the upgrade, the
  `pg_conftool` command, `service.sh --role db restart` and the read-only
  query of README.md), then the error; another refusal prints no steps. On a
  DB server at 256 with 40 connections, the steps name 303.
  `deploy-app.sh` (bash and `preflight.py`), `deploy-db.sh` and README.md
  agree on 128;
  `--confirm`, `--drop-retired-data` and `--release-queued-mail` are refused
  where they do not apply;
- after the backup, `upgrade-check` runs again with the App stopped. Retired
  data that it counts when the first check counted none needs
  `--drop-retired-data` (without it: refused, and the old App starts again
  with the backup kept; with it: `retired-data.tar.gz` is made). A refusal or
  other modules to upgrade at this second check also start the old App again.
  A refusal for the lock table alone names the lock table (not "changed while
  the App ran") and prints the steps;
- with `--drop-retired-data`, `retired-export` runs after the second check and
  before the mark, into `retired-data.tar.gz`; a failed export starts the old
  App again;
- `mark-upgrading` runs before the containers go: its refusal (exit 3, nothing
  written) starts the old App again and keeps the backup, without the text of
  `restore.txt`;
- a mark that failed otherwise (also one committed before its answer was lost)
  removes the containers, a failed `-u`, a `-u` past `INIT_TIMEOUT`, a lost SSH session
  during `-u` (SIGHUP to the process group or to the script only) and a failed
  `verify-upgrade` never start the old App: no `up`, the one-off container is
  removed with `docker rm -f`, the `-u` process does not go on, and the message
  prints `restore.txt` as the only way back. A rerun of `--upgrade` before the
  restore stops at `upgrade-check` and says that the database carries the mark;
- a failure after the switch says that the database is upgraded and checked
  (`UPGRADE_PENDING`) and that a plain rerun finishes it; that rerun deploys
  with no `-u`; a rerun of `--upgrade` that finds `UPGRADE_PENDING` (a stop
  between the check and the switch) switches with no backup and no `-u`;
- `--release-queued-mail` runs only `preflight.py release-mail` with the
  directory's compose file, and only from the folder of the release the
  directory runs; it prints the two numbers of the released mail;
- the demo-uiux line (`DemoModuleUpgradeTests`): the block upgrades the
  modules of grey (the fingerprint `f439f02d...` of v0.55.0 and v0.55.1 is in
  `DEMO_MODULE_UPGRADE_FROM`, both releases are in
  `DEMO_OLD_RELEASES_ACCEPTED`, and only v0.55.0 is in
  `NO_IMAGE_MAIL_RELEASES`). A directory of v0.55.1 with `IMAGE_MAIL=on` and
  `MAIL_REDIRECT_TO=off` takes the module upgrade to the pinned release: the
  same steps, with the plan of `uat_demo.py` (the accounts that still have
  the seed password; the fake finds none) after the switch and before `up`;
  the guard of `-u` with `--line demo-uiux` and `--exclude
  perodua_client_stable`; the names of both releases, the fingerprint and
  the number of modules in the output; the new identity with
  `line=demo-uiux`, the pinned image, the Odoo command with
  `--limit-time-real-cron=900`, the held mail and the README part that
  lists it. The two mail lines stay in the new `app.env` and in the `app.env`
  of the backup (byte for byte), `compose.yml` has the mail of the image and
  no `SMTP_` value, and the mail hold comes after `-u` and before the App
  starts. A directory of v0.55.0 with the two lines gets a backup `app.env`
  without them (0600, named in `restore.txt`) and keeps them in the new
  `app.env`; one without them keeps the mail off. A directory of an unknown
  grey release, or a database with another fingerprint, is refused before
  the App stops, and nothing changes. A run that stops at the question (no
  `--confirm`) shows the lock line, the guard and the plan, and changes
  nothing (grey step 3 of the README). The grey step of the README that
  gives `womtest1` and `tmstest1` the password `perodua` again runs `odoo
  shell` with the options that `deploy-app.sh` uses for `uat_admins.py`, in
  the compose project of the demo-uiux line;
- as root on Linux: the `service.sh` of v1.2.1
  (`fixtures/service-v1.2.1.sh`, pinned by its sha256) refuses `start`
  and `restart` on a marked database, before and after the switch, without
  `up` and without its advice to reset. The `service.sh` of this kit is the
  same file apart from its help text and `reset` (which reads the release of
  the line of the directory), and `reset_database.py` is pinned by its sha256:
  `restore.txt` says that both do the same in the folders of the old and the
  new release.

`test_preflight_module_upgrade.py` runs the exact `preflight.py` that
`deploy-app.sh` writes, and the one of v1.2.1
(`fixtures/preflight-v1.2.1.py`, identical in kit 7045544 and 07af48b),
against a database: SQLite behind a small stand-in for `psycopg2` by default,
or real PostgreSQL 16 with `PREFLIGHT_TEST_PG='HOST PORT USER PASSWORD DATABASE'`
(the user owns the database; each test drops and creates its `public` schema).
It checks the report of `upgrade-check` (retired tables, columns, attachments,
the modules to upgrade), each of its refusals, what it leaves to the new
release (an adoptable reference code, the default host table, one
`sample_data` parameter), that a refusal of `mark-upgrading` exits 3 and
writes nothing while its other failures exit 1, that from `mark-upgrading`
on no release accepts the
database (the old preflight included), the cron rule (flags back as before,
new crons as the upgrade set them, the four mock intake pulls kept off while
their system resolves to mock, a live one restored), the mail hold (every row
in state `outgoing` is held: the rows `-u` created, an older row that waited
in the queue, and an older row that `-u` gave an address; rows in the states
`sent`, `cancel` and `exception` with another reason stay as they are, also
those `-u` created; the output and the record give the two counts), the
release after `stamp-upgrade` only (exactly the held rows that are still
held: not a held row that was cancelled or deleted since, and not a row with
the same reason that this upgrade did not hold), a second `verify-upgrade` on
the same database (after a first one that held the mail and refused the
result: the held rows and the counts stay the same, and a row that entered
the queue in between is counted once), the mail that an earlier module
upgrade still holds (the mark keeps the rows of its record that are still
held, not those cancelled, deleted or sent since; they count with the mail
that waited before the upgrade, and one release sends them with the mail of
this upgrade; no record, or a record whose hold was released, adds none; a
record that does not list its held mail as ids refuses `upgrade-check`, and
`mark-upgrading` with exit 3 and nothing written), the refusals of
`verify-upgrade` (which still writes the cron flags and the hold), and the
CSV files of `retired-export`.

The lock check of the way back: `upgrade-check` prints `RESTORE_LOCKS` before
`READY` and before `MODULES`. With SQLite the lock table comes from
`FAKE_PG_LOCK_TABLE`: exactly enough room passes (also with a prepared
transaction), one lock more refuses the module upgrade ("the way back can
fail") with `RESTORE_LOCKS` on standard output, both numbers and the value to
set (128, or more when the database needs more, never less than the value of
the server: 276 for a server at 256 with 4 connections), `mark-upgrading`
then exits 3 and writes nothing, and the same modules are not refused. On PostgreSQL,
with `pg_dump`, `pg_restore` and `psql` on the PATH, a database with each
kind of object that the count covers is made a few objects per
transaction: 150 Odoo-shaped tables, the extensions `pg_trgm` and `unaccent`,
enum, domain, composite and range types, a collation, a text search
dictionary and configuration, a conversion, columns of user array types,
functions (one with a `BEGIN ATOMIC` body), an operator and a cast, a
generated column, statistics, a trigger, a rule and a policy with comments, a
publication, a partitioned, an inheriting, a typed and an unlogged table, 20
triggers without a comment on the partitioned table (one with `DEPENDS ON
EXTENSION`), a foreign key of it and a default, a constraint trigger, a table
with 3 defaults and 3 `INHERITS` children of it and a grandchild (one child
without these defaults, one with its own; the restore deletes the copies of
12 defaults and keeps a lock on each), 5 foreign keys of the partitioned table
to a partitioned table with 3 partitions and, on each partition, a copy of
these keys and of its key to `res_users` (12, which the restore makes one
with the keys of the table), a view, two populated materialized views (one
with TOAST) and one without data, and a second schema. It is dumped, emptied
a few objects per transaction and restored in one transaction, as
`restore.txt` step 3 does. Before the commit, the transaction reads `pg_locks`: the count
is at least the locks that the restore holds in the lock table (not those in
the fast-path slots of the session, and not the lock of the probe on
`pg_locks`), and so is the count of each catalog (with the two types of each
`REFRESH` for `pg_type`, and the inherited defaults for `pg_attrdef`); the
restored relations are in the count, and the restored database counts the
same, less what the restore dropped: no copy of a key is left (with
PostgreSQL 16.15: 2,414 counted, 1,834 held; triggers 242 counted, 118 held;
inherited defaults 13 counted, 12 held). The test names each catalog with
more locks than its count. With the count of 7666b07, which left out the
inherited defaults, it fails with `pg_attrdef: counted 0, locked 12`; with a
count of only the defaults that the children have, with
`pg_attrdef: counted 10, locked 12`. With the count of 3044160, which also
left out the internal triggers of partitions, it fails with these two:
`pg_attrdef: counted 0, locked 12` and `pg_trigger: counted 62, locked 118`;
with the count of 3902553, which counted only triggers with a comment, with
`pg_attrdef: counted 0, locked 12` and `pg_trigger: counted 1, locked 118`.
When the server has no room for
the count, the check refuses, and a restore that fails stops with
`out of shared memory`.

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
  last, when nothing else was left in the directory. The other Perodua
  containers below are not guarded by this lock.

Other Perodua containers (an App of the earlier perodua-odoo package, with an
attachments volume, an anonymous volume and a project volume that no container
mounts, and containers without a Compose project):
- after an uninstall without a terminal, they are listed by project with the
  delete command and kept;
- on a pseudo-terminal, `n` or Enter keeps a project, and `y` takes it down by
  name from `/` (the fake refuses a compose call without `--file` in any other
  directory). The volume question lists all three volumes; `n` or Enter keeps
  them, and `y` deletes each by name. The images stay;
- containers without a project are matched by name in any case or by image,
  and removed by ID; a container that does not match is neither listed nor
  removed;
- with no deployment directory, the exit status is 0 after a deletion, and 1
  with "Nothing was changed." after `n` or without a terminal;
- a failed deletion is reported and the next project is still asked about;
- containers of the uninstalled project that appear afterwards (a new
  deployment) are not offered.

The plan of `uninstall.sh --role app` names the `backups` folder that
`deploy-app.sh --upgrade` made in the directory, and only when there is one.

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
- before the plan and any Docker call, `reset` runs `deploy-app.sh --dir DIR
  --init-db --check-config`; when it refuses the settings, nothing changes.
  With the real `deploy-app.sh` of a release without `PUBLIC_ROOT` support and
  the `app.env` of a `/dev` environment, the reset stops with its reason and
  no Docker call;
- nothing changes after a wrong confirmation, without a terminal and without
  `--confirm`, or without `app.env`;
- a failed wipe stops before the containers and the attachments volume go, and
  says whether anything was deleted (exit 3 of `reset_database.py`: nothing).

`test_iss_api.py` runs `iss-api.sh` against a fake Docker CLI, a fake `ss`, and
`mv` and `tar` that can be made to fail. The fake Docker records every call, its
standard input and whether the script's lock (in `/run/lock`) was held, models
the Compose project's working directory, and fails on any command the script
should not use; only the port the container publishes listens. It checks:
- an install from `--config` pulls the pinned image and writes the database
  password (with `$`, `#`, quotes, a space and a backslash, in a file with a
  Windows line break) only to `secrets/db_password`, 0444 in a 0700 directory:
  no Docker argument or `compose.yml` contains it. `iss-api.env` is 0600 and
  literal, and a `$` in the user name is `$$` for Compose. The port binds
  `127.0.0.1`; the identity and the last good snapshot are written; `up` never
  pulls; the pull and `up` hold the lock;
- the new API key reaches the check on standard input and no argument, only its
  SHA-256 is saved, and it is printed once, also when the first start fails, but
  not when the new files were never published. Rerunning an install with the same
  `--config`, which has no `API_KEY_HASH`, keeps the key and the container, and
  its success message does not claim a query with a key;
- a new password recreates the container, which reads it;
- shell syntax, a loopback database host, a user name with a space, a port over
  65535, an address or hash that is not one, and an unknown key stop before any
  Docker call and create nothing; a compose file Compose refuses changes none of
  the deployed files;
- a release that does not become healthy is replaced by the last one that
  started, with its compose file, settings and password, also when only the
  password changed or the settings were edited in place; the settings that failed
  go to `iss-api.env.failed`; when the last good release does not start either,
  the message says so. A move that fails while the new files are published puts
  back the files that were there before, also on a first install, whose retry
  then issues a key that works; a snapshot that cannot be written keeps the
  previous one;
- `gen-key` keeps the running image and every other setting (no pull), and
  refuses while `iss-api.env` differs from the last good snapshot;
- `PROJECT_NAME` cannot change, and a project that already runs from another
  directory is refused by `install`, `stop` and `uninstall`;
- `install` refuses a non-empty directory it did not create, and `uninstall`
  refuses a directory without its identity, changing nothing; `uninstall` without
  a terminal or with a wrong `--confirm` changes nothing, and `--purge --confirm`
  takes the project down with `--rmi all` and removes only the files `install`
  created, leaving the administrator's own;
- while another process holds the lock, `install`, `start`, `stop`, `restart`,
  `gen-key` and `uninstall` stop before any Docker call.

`test_rp_adapter.py` runs `rp-adapter.sh` against a fake Docker CLI that records
every call and, for `docker run`, what the container would read from its mounted
settings and secret files; when the adapter is told to write to `/output` it
creates a run folder there. It checks:
- an install from `--config` pulls the pinned image, writes the database password
  (with `$`, `#`, quotes, a backslash and a backtick) and the API key only to
  `secrets/`, 0444 in a 0700 directory, and they reach the container only as
  mounted files: no Docker argument contains them. `rp-adapter.env` is 0600 without
  the `*_FILE` lines, `adapter.conf` holds container paths and no secret, and
  `output/` is 0700 for the image's uid 10001. Every run is `--read-only`, drops
  all capabilities, runs as 10001 on the configured network, and the install ends
  with the database health check (and the API's connection check when set up);
- a rerun asks nothing, keeps the password and pulls nothing; a new
  `DB_PASSWORD_FILE` (with a Windows line break) replaces the password;
- a first unattended install without a password file, an unknown key, invalid
  values and a loopback database host outside the host network stop before any
  Docker call; a non-empty directory it did not create is refused; a database
  that fails the check at install keeps the settings; a registry that wants a
  login without a terminal says `docker login`;
- `health` passes its options and returns the adapter's exit code with the
  adapter's stdout untouched; `survey` and `export` write under `output/` and
  print the host folder; `export` needs datasets or `--all`; `--config` and
  `--out`, which the script sets, are refused; `run` passes any adapter command;
  `api-health` needs `API_URL`; commands without a setup point to `install`;
  `status` shows no secret;
- `uninstall` with a wrong `--confirm` changes nothing, keeps `output/` unless
  `--purge` (which also removes the image), and a later `install` into a folder
  holding only `output/` is allowed;
- each survey or export gets a folder of its own even when another run is writing,
  prints the adapter's folder with `manifest.json` in it, and `status` shows the
  newest by version order (`-p10` after `-p9`);
- a first install whose pull fails leaves nothing staged and can be run again or
  uninstalled; a move that fails (a fake `mv`) puts the previous settings and
  password back; an install killed half-way (the fake `mv` kills it) leaves the
  other commands refusing to run until the next install puts the previous files
  back. That next install does not publish what the killed one had only staged
  (a new password next to the old user), and when it is killed too while putting
  the files back (a fake `cp`), the one after it still restores them all;
- usage errors and a local step that fails (a file where a folder must go) exit 3,
  which monitoring reads as UNKNOWN, not WARNING or CRITICAL.

`test_no_pull_apps.py` checks `--no-pull` (and `KIT_NO_PULL`) of `deploy-app.sh`,
`iss-api.sh` and `rp-adapter.sh`, and the Mac helper `stream-image.sh`. The App
tests run as root on Linux (the test image; `deploy-app.sh` needs Linux only)
against the fake Docker CLI of each script's own tests, behind a fake that
answers what `--no-pull` adds: `docker info` (the image store), `docker image
inspect` of the digest reference `REPOSITORY@DIGEST`, and the start of a pinned
image (`docker run --rm --pull never --network none --entrypoint sh IMAGE -c :`).
In a no-pull test it refuses `docker pull`, `docker login` and `docker context`,
and it records each call with its `DOCKER_CONFIG` and whether the deployment
directory existed at that moment. It checks:
- `NoPullCodeTest` (any host), read from the scripts: no `read`, no `docker pull`
  or `docker login` command, no terminal and no throwaway `DOCKER_CONFIG` in the
  no-pull step; `docker pull` and `docker login` only in `pull_image`, which
  runs only without `--no-pull`; the check before the first change (`install -d`
  of `iss-api.sh`; `discard_staged`, `put_back` and `install -d` of
  `rp-adapter.sh`; `mkdir`, the lock and the temporary folder of
  `deploy-app.sh`), and in it the start after the check of the digest; each of
  the 24 Compose `run` and `up` calls of `deploy-app.sh` with `"${PULL_NEVER[@]}"`
  (`--pull never` with `--no-pull`), not the text of `rollback.sh` and
  `restore.txt`; `--pull never` on every `docker run` and Compose `up` of
  `iss-api.sh` and `rp-adapter.sh`; the parsing of the option and the variable
  (`--no-pull` makes the run non-interactive, before the export of
  `KIT_NON_INTERACTIVE`); the pins of the three scripts are the ones of before
  (an owner decision), each script names `PLATFORM=linux/amd64` and makes
  `REPOSITORY`, `DIGEST`, `PINNED` and `STREAM_TAG` from its pin (a registry port
  stays); the usage text names the option, the variable and the helper; each
  `deploy-app.sh` step that a run prints goes through `DEPLOY_COMMAND` or
  `init_again` (`sudo env KIT_NO_PULL=1 bash deploy-app.sh` in a no-pull run),
  except the go-back step of a run without no-pull and `--mail`;
- `NoPullOptionTest` (Linux): another value of `KIT_NO_PULL` stops each script at
  once (exit 1, 3 for `rp-adapter.sh`) and makes nothing; `--no-pull` belongs to
  `install` (`iss-api.sh`, `rp-adapter.sh`) and to a deployment (`deploy-app.sh`:
  not with `--check-config`, `--release-queued-mail` or `--send-test-mail`);
  `--mail` stops with `--no-pull` and with `KIT_NO_PULL=1`;
- `iss-api.sh install --no-pull` and `rp-adapter.sh install --no-pull` with the
  pinned image on the server: no pull, no login and no throwaway `DOCKER_CONFIG`,
  the check (one `docker info`, the digest reference, one start; for the
  adapter as its user 10001) before the directory exists, the message that the
  image was not pulled (on stderr for `rp-adapter.sh`), Compose `up` and the
  adapter runs with `--pull never`, and `status` names the result in its
  `Local:` line; `KIT_NO_PULL=1` does the same as the option;
- without the image: exit 1 (3) before any change, with the stream command for
  the Mac (`bash scripts/stream-image.sh SSH_HOST SCRIPT` and the Docker
  commands with the values of the pin and the host name); an installed setup
  stays the same, byte for byte, also the files that an install stopped
  half-way left for `put_back`; an image with another ID or registry digest, or
  one that does not start, stops it the same way; on the classic image store a
  missing image stops it with the classic message, and an image that a pull
  brought (ID: the digest of its config) is used; an engine that does not answer
  `docker info` stops it;
- `KIT_NO_PULL=1` also covers `health`, `survey`, `export` and `run` of
  `rp-adapter.sh`: without the image they stop before a run folder is made,
  with it the adapter runs with `--pull never` and the standard output is the
  adapter's only; without the variable a missing image is pulled as before;
- `deploy-app.sh --no-pull` on both lines (the pinned client-stable-uiux images,
  and the demo-uiux line of the fixture release of `test_deploy_app_uat.py`):
  both images checked and started before the directory exists, no pull, and
  every Compose `run` and `up` with `--pull never` right after the subcommand;
  one image or both missing stops it before any change, with the lines of the
  missing images only and `--line LINE` in the command for the Mac; another
  digest, a start that fails, the classic store and an engine that does not
  answer stop it; Compose older than 2.32.2 (no `run --pull`) stops it before
  the image check; `--release-queued-mail` with `KIT_NO_PULL=1` checks no image
  and runs with `--pull never`; without the option or with `KIT_NO_PULL=0` the
  run pulls both images as before and passes no `--pull`;
- `deploy-app.sh --init-db --check-config` with `KIT_NO_PULL=1` (as `service.sh
  reset` runs it): one image or both missing, another digest, a start that
  fails and Compose older than 2.32.2 stop it with the message of the
  deployment and no "Configuration valid"; with both images it checks them (one
  `docker info`, the digest references, the starts) and makes no directory;
  without the variable, or with `0`, it calls no Docker, as before;
- a `--replace` with `--no-pull` that fails after the other App stopped starts
  that App again with `up --pull never`, and nothing pulls;
- on a pseudo-terminal, `--no-pull` (and `KIT_NO_PULL=1`) asks nothing: a run
  without settings stops and names `--config`.
- `ServiceResetNoPullTest` (root on Linux): `service.sh reset` with
  `KIT_NO_PULL=1` and the real `deploy-app.sh` next to it, on the fake Docker of
  `test_service_app.py` behind the no-pull fake. One image of the line or both
  missing stop the reset in its settings check, before the plan: no stop, no
  wipe, no `down`, the identity and the attachments volume as they were, and
  the message names the images. Another value of the variable stops it at once.
  With both images the check passes and the reset stops at the confirmation:
  no-pull never asks, also on a pseudo-terminal. With the `deploy-app.sh`
  fixture, the container that empties the database starts with `--pull never`
  right after `run`, and both `deploy-app.sh` runs get `--non-interactive` and
  the variable; without it the reset is the one of before.
- `DeployAppUpgradeNoPullTest` (Linux): a failed `--upgrade --no-pull` on the
  fake Docker of `test_deploy_app_upgrade.py`: before the switch, the old App
  starts again with `up --pull never`; after the switch, the step to finish the
  upgrade is `sudo env KIT_NO_PULL=1 bash deploy-app.sh`, and the way back
  (printed and in step 5 of `restore.txt`) says to stream the old images when
  they are missing and to use a scripts folder with `--no-pull`; every Compose
  `run` and `up` has `--pull never`, and nothing pulls. Without `--no-pull` the
  steps are the ones of before.

`StreamImageTest` runs `stream-image.sh` with `/bin/bash` (bash 3.2 on the Mac,
no root) against a fake `docker` of the Mac, a fake `ssh` that refuses any call
without the options of the helper and answers the commands of the server (its
home folder is a folder of the test), a fake `rsync` that can break half-way
(it keeps the part, as `--partial` does) or damage the copy, and a fake
`shasum`. It checks:
- each SOURCE streams the pins that its script holds, each pin once and in the
  order of the command line: `iss-api.sh`, `rp-adapter.sh`, `oracle-view.sh`,
  `deploy-app.sh` (the default line, and `--line demo-uiux`), and a list of them;
  the next step of each kit script (for `deploy-app.sh` with the mark of owner
  rule C1 before the command); the order of the checks (every image on the
  Mac, then `sudo -n docker version` and `info` on the server, the check of the
  digest reference, the load, the check again), `ssh -n` on each check, the
  archive only on the standard input of the load; a server with the image gets
  nothing, and a stream tag that names the pin is not tagged again;
- `--image` with the rows 1 to 3 of the image table of the rebuild plan of .13:
  their media type read from the registry before the server and the pulls,
  pulled on the Mac with `--platform linux/amd64` (or the `--platform` given),
  streamed, no next step; a pull that brings another ID reaches no server, and
  without the login of the Mac the registry read stops it before the server and
  the pull;
- usage errors (exit 2) before any call: no SSH_HOST or SOURCE, an unknown
  SOURCE, `--line` without the `deploy-app.sh` before it, twice or with another
  line, an `--image` that is not a pin (no tag, no or a short digest, capitals,
  shell syntax), `--platform` without `--image`, twice or not `linux/ARCH`, an
  unknown option, an SSH_HOST with other characters, and another value of
  `KIT_NON_INTERACTIVE`;
- `--dry-run` (also with `--file`) pulls, tags, saves, copies and loads nothing
  and makes no folder; it prints the steps (with `--file` also the two `df`
  checks); a pin that is not on the Mac is read from the registry in the dry run
  too: an index with another platform, another digest in the registry and
  another media type stop it before the server;
- `--file`: `docker save -o` to a 0700 folder of the Mac, `rsync --partial` with
  the ssh options of the helper to `~/stream-image` (0700) on the server, the
  same SHA-256 there, `docker load -i`, the check, and then no archive on either
  side; after a break the archive and its SHA-256 stay on the Mac and the part
  on the server, and the next run saves nothing again and copies over the part;
  a kept archive whose SHA-256 changed is saved again; a copy with another
  SHA-256 is removed on the server and loads nothing; a server without `rsync`
  and `sha256sum` gets nothing; each server has its own folder of archives on
  the Mac (`stream-image-archives/HOST_PORT`): a run to another server leaves
  the kept archive of a broken run as it is, and that run resumes from it; a
  folder that is a link stops `--file` before the server; before the save, the
  free space of the Mac (a fake `df`) must hold the size of the image there, and
  before the copy, the free space on the server must hold the archive and the
  image, on one file system or on each of two, else it stops (the archive stays
  on the Mac, nothing is copied); the archive that a broken `--file` run kept is
  removed on both sides when the pin is on the server: by the stream without
  `--file` that puts it there, and by a `--file` run that finds it there (a dry
  run names it and removes nothing); a run without `--file` and with nothing
  kept sends the server the checks only;
- a stop after an image reached the server (a pull that brings another ID, a
  `docker tag` that fails) names the image that it stops at ("... was not
  streamed") and the images that the run streamed and checked; with nothing
  streamed before, the message stays "Nothing was streamed." (or "Nothing was
  copied.", and no part of the archive stays);
- an index (the pins of `iss-api.sh`, `rp-adapter.sh` and the two Odoo images
  of `deploy-app.sh` are one, as measured on 2026-11-10): its bytes from the
  registry (`docker buildx imagetools inspect --raw`, the fake `shasum` of the
  standard input) must have the pinned digest, and its entries (`--format`) must
  be one image of the platform (any variant of it) and attestation manifests
  (`unknown/unknown`, `attestation-manifest`); then `docker save` of the stream
  tag, without `--platform`, and the server finds the pinned digest. Another
  platform, two images of the platform, an `unknown/unknown` entry that is no
  attestation manifest, a nested index, no entry, another digest in the
  registry, a registry that wants a login and another media type stop it before
  the server, also in a dry run; an index that the Mac pulls now is checked
  before the server and the pull (the media type, the bytes and the entries
  from the registry);
- the classic store on the server streams nothing (and a pin that a pull
  brought there counts as there), a stream tag of another image and the
  classic store on the Mac stop it before the server; a refused
  `sudo` or connection, a load that prints `Error unpacking` and a load that the
  server does not find by its digest stop it with exit 1;
- one run at a time for each server, whatever name `ssh -G` resolves to it;
- read from the helper: no `read`, ssh only with `-T -a -x`, batch mode, no
  forwarding and no local command (and `ssh -G` in batch mode), each command on
  the server `sudo -n docker` or one of the five commands of `--file` (`df -Pk`
  too; the Docker root folder only as a plain absolute path), `docker
  save` only of the stream tag and never with `--platform`, the template of the
  entries of an index, and no pin in the helper: it reads them from the kit
  scripts.

`test_oracle_view.py` runs `oracle-view.sh` against fakes and, in the test image,
its role SQL on a real throwaway PostgreSQL 16 cluster. Every password, key and
verifier in it is made up. On the App side, a fake Docker CLI records every call,
its standard input, its `DOCKER_CONFIG` and whether the script's lock was held,
and fails on any command the script should not use; its `docker run` of
`make_user.py` prints a users line made from the password it reads. Fake `ss` and
`curl` answer from the same state, and `mv` and `tar` can be made to fail. It
checks:
- the pin: one `IMAGE=` line, `perodua-deploy.novutal.com/oracle-view` by
  `sha256` digest, the release name from its tag; `install` pulls exactly it
  (once), `compose.yml` names it with `pull_policy: never` and `up` never pulls;
  the usage text and every `oracle-view:grey-v...` tag of the README name that
  release; the pin is exactly `grey-v1.0.16@sha256:d23ecb4a...` (linux/amd64,
  label `org.opencontainers.image.version` `grey-v1.0.16`, revision `910f8e0`)
  and neither the script nor the README names the tag `grey-v1.0.1` (the full tag, not the start of `grey-v1.0.16`), `8e82b47f`,
  `a5b48111`, `ed104afa`, `cf9a9170`, `9d3883e8`, `317f9423`, `f40f832a`, `0b842a9a`, `b84b67c4`, `30d14c39`, `a991556e`, `a938bd2d`, `6609b96e`, `b4399dde` or `011d6087`
  (`OracleViewDocsTest`,
  which needs no root, also checks the README sentences of the tests below and
  that only the lines `RP_DATABASE=perodua_oracle2` and
  `OLD_RP_DATABASE=perodua_oracle` of the script name a database);
- the Docker engine: an API older than 1.44 (Docker 25, for `start_interval`) or
  an engine that does not answer stops `install` before the pull and before any
  file is made; 1.44, 1.100 and 2.0 pass;
- the registry login: without a terminal or with `--non-interactive` the message
  names `docker login perodua-deploy.novutal.com` and nothing is made; on a
  pseudo-terminal the username and the hidden password go to `docker login
  --password-stdin` with a throwaway `DOCKER_CONFIG` that is gone afterwards, and
  the password is in no argument and no output;
- `install --no-pull` (and `OVB_NO_PULL=1` or `KIT_NO_PULL=1`, the same): with
  the pinned image on the server it installs with no `docker pull`, no `docker
  login` and no throwaway `DOCKER_CONFIG`, also when the registry wants a login;
  the check of
  the image (`docker info` for the image store, `docker image inspect` of
  `REPOSITORY@DIGEST`) and one start of the image (`docker run --rm --pull never
  --network none`) hold the lock and come before `make_user.py`, which runs
  with `--pull never`; on a pseudo-terminal it asks nothing, with the image and
  without it; without the image it exits 1 before any change (no directory, an
  installed release untouched) and prints the stream command for the Mac
  (`docker pull --platform linux/amd64 IMAGE && docker tag REPOSITORY@DIGEST
  REPOSITORY:sha256-HEX && docker save REPOSITORY:sha256-HEX | ssh SSH_HOST
  'sudo -n docker load'`); an image that does not start (a layer that did not
  unpack), an image whose ID or registry digest is not the pinned digest, and an
  engine that does not answer `docker info`, also stop it before any change; on
  the classic image store it stops when the image is missing (a loaded image
  has no registry digest) and accepts an image that a pull brought (its ID is
  the digest of its config); `OVB_NO_PULL=0` or empty is the pull as before,
  another value is a usage error (exit 2), and `--no-pull` belongs to `install`
  only; `KIT_NO_PULL` works the same way (another value stops every command at
  once, the option wins over `0`), and `KIT_NO_PULL` and `OVB_NO_PULL` with
  two different values are a usage error before Docker; without `--no-pull` the
  image step is unchanged (no `docker info`);
  `status` names the result of the same check in its `Local:` line; a release
  that does not become healthy under `--no-pull` brings the last good release
  back with `up --pull never` and no pull or login;
  `OracleViewNoPullCodeTest` (no root) reads the image step of `--no-pull` from
  the script: no `read`, no `docker pull` or `docker login` command, no
  `pull_image`, the check and the start before the first change, both
  `docker run` calls with `--pull never --network none`, the stream tag
  `REPOSITORY:sha256-HEX` made from the pin, and neither `oracle-view.sh`
  nor `oracle-view-stream.sh` names `deploy-app`, `service.sh`, `rollback.sh`,
  `restore.txt` or `grey-oracle-sync-wayback` (owner rule C1: forbidden while
  the grey App runs on an Oracle DB);
- `oracle-view-stream.sh` (`OracleViewStreamTest`, no root, run with
  `/bin/bash`, which is bash 3.2 on the Mac; a fake `docker` of the Mac with
  `docker buildx imagetools inspect` of an index, a fake `shasum` and a fake
  `ssh` that refuses any call without the options of the helper, reads the
  standard input like ssh unless `-n` is given, answers `ssh -G` in batch mode
  and the commands of the server): the script runs `stream-image.sh` with the
  source `oracle-view.sh`, so its messages, its lock (`stream-image-HOST_PORT`)
  and its checks are those of `stream-image.sh`; it gives the pin its stream tag
  `REPOSITORY:sha256-HEX` and streams `docker save` of that tag to `sudo -n
  docker load`, then checks the server by the digest reference; each check on
  the server runs with `ssh -n` and reads nothing of the standard input, the
  load reads the archive; its output names no command of owner rule C1; a stream
  tag that already names the pin is not tagged again, and one that names another
  image stops it before the server; an index of one linux/amd64 image and its
  attestation manifest streams, an index with another platform stops it before
  the stream (also when the pin is not on the Mac: the registry gives its media
  type, bytes and entries before the pull); it pulls on the Mac (`--platform
  linux/amd64`) only when the pin is missing there, after it read the media type
  from the registry, and a registry read without the login of the Mac or a pull
  that brings another ID reaches no server; a failed `docker info`, `docker tag` (with its message) or `docker
  save` on the Mac, a failed `docker info` or `docker load` on the server, and a
  load that prints `Error unpacking` stop it with exit 1; a server with the pin
  gets nothing; a load that the server does not find by the digest exits 1;
  `--dry-run` runs only the checks; a classic image store on the server stops
  it before the stream, unless a pull brought the pin there (as `install
  --no-pull` accepts it), and a classic store on the Mac stops it before ssh; a
  `sudo` that wants a password and a refused connection stop it with the
  message of the server; one run at a time for each server, whatever name
  `ssh -G` resolves to it (a lock directory of a live process, or one with no
  process number yet, stops it; one of an ended process is taken over; a file or
  a link there stops it); usage errors exit 2; the script has no `read` and no
  `ssh`, `docker`, `sudo` or `rsync` of its own, runs `stream-image.sh` with
  `exec`, and holds no pin (`stream-image.sh` reads it from `oracle-view.sh`);
- the container of `deploy/grey/compose.yml`: read-only root file system, tmpfs
  `/tmp`, the state volume, the users file bound read-only without
  `create_host_path`, `127.0.0.1:8797` only, no capabilities, `no-new-privileges`,
  1 GB memory and swap, 256 processes, 1.5 CPUs, the `/healthz` health check and
  `OVB_RP_SSLMODE=require`; `compose.yml` and `oracle-view.env` 0600 in a 0700
  directory; the pull and `up` hold the lock;
- the settings: each bad value (memory form and minimum, processes, CPUs,
  `OVB_RP_SSLMODE` other than `require` or `prefer`, a time-out outside 5-3600
  or not plain digits, shell syntax) and an unknown key stop before any Docker
  call and make nothing; `--config` reads over the saved settings, and the
  `.env` of the manual install (comments, Windows line ends) and
  `oracle-view.env.example` are a valid `--config`;
- the time-outs `OVB_LIVE_TIMEOUT` and `OVB_SQL_TIMEOUT`: without a value (also
  when the environment of the command has them) neither `compose.yml` nor
  `oracle-view.env` has them and `status` names the image default (300 s); 5 and
  3600 go to the environment of the container as strings and stay saved; a key
  with no value in `--config` removes a saved value;
- the users: `perodua` (admin) and `pm` (editor) get random passwords through
  `make_user.py` on standard input only (`--network none`, `--read-only`, no
  capabilities, uid 10001); the users file is 0400 for 10001, the passwords are
  only in `initial-login.txt` (root, 0600) and in no argument or output; a second
  run makes no user, keeps the file and the container; a removed `pm` line gets a
  new password; any change of the users file (also a last line without its line
  end) makes the container again, with the new file;
- the staged install: a failed `make_user.py` or a compose file that Compose
  refuses publishes nothing, also no password; a move that fails puts back the
  users file, the passwords file, the settings and `compose.yml`; a release that
  does not become healthy is replaced by the last good one (its image and
  settings) with the failed settings in `oracle-view.env.failed`, and the message
  says when that one does not start either; a first start that fails keeps the
  users for the retry, which makes the container again; a snapshot that cannot be
  written keeps the previous one;
- `/healthz` on the server, a port 8797 used by another program (before the pull),
  the manual install's container (named with `uninstall-app.sh, without --purge`)
  and a non-empty directory that is not its own each stop the install;
- `status` shows the release, the users with their roles but no hash and no
  password, the volume and the health;
- `uninstall` needs `--confirm oracle-view` (or a terminal); without `--purge` it
  keeps the state volume, the users file, the identity and the image, says so,
  and a later install uses the same users; `--purge` removes the volume, every
  local image of the repository and the users, lists what it removed, leaves the
  administrator's files, and removes the directory when it is empty;
- a volume or an image that `--purge` cannot remove: the message lists what was
  removed and what was not, the identity stays, the exit status is 1, and the
  next `--purge` removes the rest;
- the route hint: the table of `https-routes.conf.example`, where the route of
  the tool is a comment only, gets the "Next: publish it" step from `install`
  and no "route stays" line from `uninstall`; an uncommented route line turns
  both around;
- a held lock stops `install` and `uninstall` before any change.

On the DB side, fake `runuser`, `id` and `psql` record every `psql` call with its
standard input and the user. It checks:
- the verifier file: a password, an `md5` hash, a short key, a quote, two lines,
  the `role.sql` of the bundle, an empty file and a missing file are refused
  before any SQL, and the content is never printed; a Windows line end is
  accepted;
- the role SQL: it runs as `postgres` on the local socket with `ON_ERROR_STOP`
  and the psql variables `ovb_db=perodua_oracle2` and `ovb_old_db=perodua_oracle`;
  the verifier is only in the first stdin line (`\set ovb_verifier`), in no
  argument and no output; `log_statement`, `log_min_duration_statement`,
  `log_min_error_statement`, `log_min_duration_sample`,
  `log_transaction_sample_rate`, `pg_stat_statements.track_utility`,
  `VERBOSITY terse` and `SHOW_CONTEXT never` come before each statement with the
  verifier, also after `\connect postgres`; before any change, after the check
  of the old database, the script reads the owners of `perodua_oracle2` and
  `perodua_oracle` and the databases and tablespaces of the role (`OWNERS_SQL`,
  read from the script: `pg_shdepend` with `dbid` 0), and stops for a superuser
  owner of `perodua_oracle2`, for a superuser owner of `perodua_oracle` when the
  command cleans it, when the role owns `perodua_oracle2` or `perodua_oracle`,
  and for a database or a tablespace of the role, in that order; the grants of the owner
  decision are there, with one membership in the
  owner of `perodua_oracle2` (read again, never a superuser or the role, `WITH
  INHERIT TRUE, SET FALSE`, without a `NOTICE` on a later run) after `CONNECT`
  and before `\connect` to that database; no `TRUNCATE`, `SUPERUSER`, `GRANT
  CREATE`, `GRANT ALL`, `SET TRUE`, `ADMIN`, `ALTER SYSTEM`, `OWNER TO` or reload;
  the schema `ovb_stage` of Copy to RP comes last, in
  `perodua_oracle2` (`CREATE SCHEMA IF NOT EXISTS ovb_stage AUTHORIZATION
  oracle_view_rp`, `REVOKE ALL ... FROM PUBLIC`, `GRANT USAGE, CREATE`), and that
  `GRANT` and that `REVOKE` are the only `CREATE ... ON SCHEMA` and the only
  `REVOKE` of the role SQL; in the old database, after `NOLOGIN` (the only one
  of the role SQL) and the end of the sessions of the role there (each one
  waited for), one transaction holds the SQL that gives the objects of the role
  there to the owner of that database and names them, `DROP OWNED BY
  oracle_view_rp` right after it and `GRANT CONNECT, TEMPORARY ON DATABASE
  :"ovb_db"`, and only when that database exists; after the `COMMIT`, the end of
  the sessions there again and `LOGIN` when the role had it (`ovb_rp_login`);
  `oracle_view_ro` gets `DROP OWNED BY` in both databases before `DROP ROLE`;
- that SQL (`REASSIGN_SQL`, read from the script) lists the objects of the role
  from `pg_shdepend` (not its default privileges or user mappings), reads the
  owner again and stops when the role owns the database ("A different owner is
  needed first"), when the owner is a superuser and when the role owns a
  database or a tablespace, runs `REASSIGN OWNED BY oracle_view_rp TO` the owner
  of the database, names the objects, and drops nothing; the role SQL has it
  once and the drop SQL twice, each time inside a transaction of that database
  (no `\connect` between `BEGIN` and it), right before `DROP OWNED BY
  oracle_view_rp` and before the `COMMIT`;
- a failed `role` or `drop-role` reads the `LOGIN` of the role before its SQL
  (once), and after the SQL sends the `login-back` SQL (`LOGIN` when the role
  has `NOLOGIN`) only when the role had `LOGIN` before the run; not with
  `NOLOGIN` before, not without a role, and not after a run that succeeds;
- a `FAIL` right (also `TRUNCATE on every table of public` and `CREATE in
  public`), a `FAIL` line of the membership in the owner of the database (not a
  member, `SET TRUE`, a superuser owner, `SET ROLE` or `ADMIN` through another
  role; with the hint for the membership, and only then), a right on another
  database (with the way to remove it, and the `REASSIGN OWNED BY` that keeps
  the objects of the role there), a `FAIL` line of `ovb_stage` (with the hint
  for the schema, and only then), a `FAIL` line of `temp_file_limit=8GB` or a
  short answer (12, 14, 15, 16, 17 or 18 lines, also the 18 lines without the
  line of `temp_file_limit`; not 19) stops before the `pg_hba.conf` check; the
  role SQL sets `temp_file_limit = '8GB'` beside `statement_timeout` and
  `idle_in_transaction_session_timeout`, and the check SQL reads it in the
  settings of the role (line 16); the check SQL reads each direct
  grant and the paths through other roles (`pg_has_role` with `SET` and
  `MEMBER WITH ADMIN OPTION`), and shows each control character of a line as
  `\uXXXX`; the three `INFO` lines (objects of the role
  outside `ovb_stage`, the other databases of the owner, the other roles of
  which it is a member) never stop it, also with objects and roles there;
- `role` and `role --check` list the staging tables of `ovb_stage` with their
  sizes and the total, after the rights and before TLS, and add the hint to free
  the disk; an answer that the script cannot read stops the command;
- the `pg_hba.conf` check sends the App address as a psql variable, prints the
  line, the backup, the reload and the check commands, exits 3 (`NOT OK`), 4
  (`CHECK BY HAND`, with `--app-ip` and `--pg-port` in the commands) or 0, and no
  call reloads or writes a file; `role --check` needs the role and sends no role
  SQL;
- a `pg_hba.conf` with the block of `deploy-db.sh` for `perodua_oracle2`: the
  line goes above its `# BEGIN perodua-db-deploy perodua_oracle2` line (with the
  line number), and a `NOTE` says that each later `deploy-db.sh` run puts the
  block above the line again, with the `role --check` to run after it; also
  with `RESULT: OK`, and not for the block of another database (also
  `perodua_oracle` and `perodua_oracle22`);
- a `pg_hba.conf` that PostgreSQL does not load (an error line) gives exit 4 and
  the steps to correct it;
- `ssl = off` and `dblink` or `postgres_fdw` are reported, and nothing is revoked;
- a stopped PostgreSQL or a missing `perodua_oracle2` stops `role` and
  `drop-role` before any SQL; usage errors exit 2 with no call;
- `drop-role` needs `--confirm oracle_view_rp` and sends the steps in order, for
  each role (`NOLOGIN`, end the sessions, `DROP OWNED BY` in `perodua_oracle2`
  and then in `perodua_oracle` when it exists, `DROP ROLE`); for
  `oracle_view_rp`, first `OWNERS_SQL` and its four stops (before `NOLOGIN`),
  then the end of each session, waited for (`pg_terminate_backend(pid,
  5000)`), then in `perodua_oracle2` one transaction from `BEGIN` to the
  `COMMIT` after `DROP OWNED BY`, with the
  count of the staging tables, the stop that gives the `LOGIN` back when they
  hold data without the choice (`-v ovb_drop_staging=off`), the count of the
  objects outside `ovb_stage` that depend on it and the stop that gives the
  `LOGIN` back when there are any, and `DROP SCHEMA ovb_stage CASCADE`; for a
  schema `ovb_stage` of another owner, one `DROP MATERIALIZED VIEW` of the
  materialized views of the role there, then one `DROP TABLE` of its tables;
  then, in each database (in `perodua_oracle` one transaction too), the SQL that
  gives the other objects of the role to the owner of the database right before
  `DROP OWNED BY`; tables,
  partitioned tables and materialized views count as staging tables; the output
  says that the staging tables and their data are dropped, and that the other
  objects of the role stay with the owner of their database;
- `drop-role` with staging tables lists them and stops (exit 1, no SQL script)
  before the confirmation, with the `pg_dump` hint and `--drop-staging`;
  `--drop-staging` alone does not confirm; with both, the script sends
  `-v ovb_drop_staging=on`; without tables, `off` and no stop; `--drop-staging`
  belongs only to `drop-role` (usage error 2 elsewhere);
- `drop-role` with objects outside `ovb_stage` that depend on it names them and
  stops (exit 1, no SQL script) before the confirmation, also with
  `--drop-staging`; the query looks only at a schema `ovb_stage` of the role and
  leaves out internal dependencies;
- a held lock stops `role` and `drop-role`.

`RoleOnPostgresTest` (root and PostgreSQL 16, as in the test image) starts a
cluster on port 55432 with `log_statement = all`, an Odoo-like owner, the RP
database `perodua_oracle2` (a table and a view) and the old database
`perodua_oracle` (a table), both without the rights of `PUBLIC` as `deploy-db.sh`
makes them, and the role `oracle_view_ro` of an earlier bundle with rights in
both. `role` sets exactly the verifier of the file, drops `oracle_view_ro`, and
keeps the verifier out of the server log and the output (16 `OK` lines, and the
`INFO` lines: no object outside `ovb_stage`, and `perodua_oracle` as the other
database of `odoo`). As the tool signs in (TCP, the made-up password), the
role reads, is read-only by default, writes in a `READ WRITE` transaction (also
in a table that the owner makes later), and with the rights of `odoo` can
`TRUNCATE`, `ALTER` and `CREATE` (rolled back), but cannot `SET ROLE odoo`.
A session of the role reads `temp_file_limit` = `8GB`, and `SET` or `SET
LOCAL temp_file_limit = '-1'` fails with `permission denied to set parameter`
(a superuser setting). Without the setting of the role (`RESET`) or with
`4GB`, `role --check` shows `FAIL temporary files of a session up to 8GB
(temp_file_limit=8GB)` and exits 1; `role` sets `8GB` again.
The server also logs by sampling (`log_min_duration_sample = 0`,
`log_statement_sample_rate = 1`, `log_transaction_sample_rate = 1`), and the
verifier stays out of that log too. Without `DELETE` on one table, `role --check`
shows `FAIL` for INSERT, UPDATE and DELETE (15 `OK` lines) and exits 1: each
right is checked alone. The membership in `odoo` gives every right on a table of
`odoo`, so for this check the table has an unrelated owner (and `TRUNCATE` of
the role on it, so that only that line fails); then `odoo` owns it again, with
its sequence. With only the default IPv6 and `127.0.0.1` lines the check is
`NOT OK` (exit 3), not `CHECK BY HAND`; the printed line above them gives
`RESULT: OK`; a `samehost` line above it gives exit 4; a second `role` run
succeeds; `drop-role` drops the role and the empty schema `ovb_stage` and keeps
the rows. The cases of the check, each on the real server:

| Line (above the default lines) | `ssl` | Exit |
| --- | --- | --- |
| `hostnossl` for the App server | off | 0 |
| `hostssl` for the App server (PostgreSQL loads it with a warning: it never matches) | off | 3 |
| `hostssl` for the App server (a made-up certificate) | on | 0 |
| `hostnossl` for the App server | on | 3 |
| `md5` for the App server (`RESULT: OK` with the note; the tool signs in through an `md5` line) | off | 0 |
| the address `all` | off | 0 |
| `reject` above the line, `password` above the line | off | 3 |
| `samenet`, a database `/^perodua`, a user `/^oracle_view` or the database `samegroup` above the line | off | 4 |
| an error line (`10.0.0.0/33`) above the line | off | 4 |
| a line with an unsure field and a field that cannot match, above the line: `host all +admins ::1/128`, `host replication +repl 10.0.0.0/8`, `host all +dba 10.1.2.3/32`, `+dba` or `samenet` on another database, a host name for another user, `hostgssenc` with `+dba`, `hostssl` with `samenet` | off | 0 |
| `+dba`, a host name, `sameuser` or `hostnossl ... samehost` above the line, each other field matching | off | 4 |
| `samenet` below the line | off | 0 |
| a line for `oracle_view_rp` on `perodua_oracle` above the line (and a `NOTE`) | off | 0 |
| `include` of a file whose line 10 is a `reject`, above line 4 of the line | off | 3 |

The pg_hba.conf writer of `deploy-db.sh` (its Python program, taken from the
script) then writes its block for `perodua_oracle2` at the top: the line below the
block gives exit 3, with the line number of `# BEGIN perodua-db-deploy
perodua_oracle2` and the `NOTE`; the line moved above the block gives exit 0; the
next run of the writer puts the block above it again (exit 3).

The old database, on the same cluster. `odoo` owns both databases (as
`deploy-db.sh` makes them), so the membership in `odoo` gives the role the
rights of `odoo` in `perodua_oracle` too; where a test checks the rights of the
role itself, it revokes the membership for that check:
- a role with the rights of the earlier releases in `perodua_oracle` only (a
  grant on the database, the schema, a table, a sequence and a default
  privilege) and a session there: `role` ends the session, leaves no right
  there (the database ACL and `pg_shdepend`), names `perodua_oracle` in the
  `INFO` line of the other databases of `odoo` (the tool reads it through the
  membership), and without the membership the tool cannot connect there and
  still reads `perodua_oracle2` (its own `CONNECT` and `TEMPORARY`); with the
  printed line the run ends with exit 0 and 16 `OK` lines; a later grant in
  another database (`postgres`) gives the
  `FAIL` line with that name and exit 1 in `role --check`, and the hint prints
  the safe form `BEGIN; DROP OWNED BY oracle_view_rp; GRANT CONNECT, TEMPORARY
  ON DATABASE perodua_oracle2 TO oracle_view_rp; COMMIT;`;
- one transaction, with two stops (without the membership in `odoo`, so that
  `CONNECT` on `perodua_oracle2` is the grant of the role itself): a `psql` in
  front of the real one adds an
  error. The first stop is right after `DROP OWNED BY oracle_view_rp` in the old
  database: `role` exits 1, and the role keeps `CONNECT` on `perodua_oracle2`
  (the tool reads it) and its old rights. The second stop is right after
  `COMMIT;`: `role` exits 1, the old rights are gone, and the role keeps
  `CONNECT` on `perodua_oracle2` (the tool reads it). After each stop the role
  has its `LOGIN` again (the script says so). A role with `NOLOGIN` before the
  run keeps it after a stop. The next run without a stop ends as usual, with
  `LOGIN`, and grants the membership again;
- a second `role` run, with a session of the tool in `perodua_oracle` (through
  the membership in `odoo`, the owner of both databases): the session ends
  (`terminating connection due to administrator command`), and a `\!` that the
  `psql` in front adds right after the `REASSIGN OWNED BY` there tries to log in
  as the tool and commit a table there: the login is refused (`not permitted to
  log in`), so no table of the tool is dropped by `DROP OWNED BY`. After the run
  the role has `LOGIN`, and the tool reads both databases;
- `perodua_oracle` without connections (`ALLOW_CONNECTIONS false`, and
  `datconnlimit = -2`, a database half dropped): with rights of the role in it,
  `role` skips the step and check 12 gives the `FAIL` line with
  `perodua_oracle` (exit 1); without rights there, `role` gives 16 `OK` lines
  and `drop-role` succeeds;
- a grant on a third database (`GRANT CONNECT ON DATABASE made_up_third`, only
  in the ACL of that database): `role --check` gives the `FAIL` line of check
  12 with `made_up_third` and exit 1; the printed safe form, run in that
  database, removes the grant, the tool still reads `perodua_oracle2`, and the
  next `role --check` exits 0;
- `perodua_oracle` renamed away: `role` skips the step (exit 0, 16 `OK` lines,
  `oracle_view_ro` dropped) and `drop-role` succeeds;
- `drop-role` with rights of `oracle_view_rp` and `oracle_view_ro` in both
  databases drops both roles and keeps the rows;
- a table that the tool makes in `perodua_oracle` (through the membership in
  `odoo`) survives the next `role` run: `role` names it, it is then owned by
  `odoo` with its row, and the role has no right left there; a third run names
  nothing; a later table there survives `drop-role` too, which names it.

The staging schema `ovb_stage` of Copy to RP, on the same cluster:
- `role` makes it, owned by `oracle_view_rp` with the ACL
  `{oracle_view_rp=UC/oracle_view_rp}` (no right of `PUBLIC` or the Odoo role),
  and gives the three `OK` lines of the schema and `none` in the list;
- a table made right after the `\connect` of `drop-role` to `perodua_oracle2`
  (a `psql` in front of the real one adds it, after the check of the script):
  the drop stops (exit 1), the role keeps `LOGIN` and the tool still connects,
  and the table stays;
- as the tool signs in, a `READ WRITE` transaction makes a staging table with an
  index in `ovb_stage`; with the rights of `odoo`, `CREATE TABLE` in `public`
  (also without a schema name), `TRUNCATE` of an Odoo table, `CREATE SCHEMA`
  and `ALTER TABLE` of an Odoo table succeed (rolled back); the Odoo role
  cannot read the staging table; `role --check` exits 0 with 16 `OK` lines and
  lists the table with its size and owner;
- a function of the role in `public` gives the `INFO` line `owns 1 objects
  outside the schema ovb_stage` with its name, and `role --check` exits 0;
- `drop-role` without `--drop-staging` lists the table, stops (exit 1) and
  changes nothing; with it, the schema, its table and the role go, and the rows
  of the Odoo table stay;
- a materialized view of the role in `ovb_stage` is listed and stops
  `drop-role` without `--drop-staging`; a view and a foreign key in `public` on
  a staging table stop `drop-role` before the confirmation, with and without
  `--drop-staging`, and name both; a view made right after the `\connect` of
  `drop-role` stops the SQL, which names it, gives the `LOGIN` back and drops
  nothing; without the dependency, `--drop-staging` drops the schema with the
  table and the materialized view, and the table in `public` keeps its row;
- a schema `ovb_stage` of the Odoo role, with a table, made before the role:
  `role` gives no right there and exits 1 with the `FAIL` line `owns the schema
  ovb_stage` (with the owner `odoo`), 15 `OK` lines (`USAGE and CREATE` there
  come through the membership in `odoo`) and the hint; with those rights the
  tool makes a staging table there; `drop-role` lists it and stops without
  `--drop-staging`; with it, it drops only that table and the role, and leaves
  that schema and the table of `odoo`.

The rights of the owner of the database (owner decision of 2026-11-09), on the
same cluster:
- `role` makes one grant of `odoo` to the role (grantor `postgres`, `INHERIT`
  true, `SET` and `ADMIN` false) and the `OK` line of the membership; a second
  run keeps that one grant and prints no `NOTICE`;
- as the tool signs in, a `READ WRITE` transaction makes a table of the role in
  `public` (committed), and `ALTER TABLE` and `TRUNCATE` of an Odoo table
  succeed (rolled back); `SET ROLE odoo` and `ALTER TABLE ... OWNER TO odoo`
  fail; `role --check` exits 0 and names the table in the `INFO` line;
- without the membership, and with a grant `WITH INHERIT TRUE, SET TRUE` (the
  tool can then `SET ROLE odoo`), `role --check` gives the `FAIL` line of the
  membership with what it has and the hint, and exits 1; `role` sets `SET
  FALSE` again on its own grant; with `ADMIN TRUE`, `role` exits 1 and `role
  --check` gives the `FAIL` line until `REVOKE ADMIN OPTION FOR` by hand;
- a bridge role between the role and `odoo`, with the direct grant still
  right: `SET` on both links (the tool can `SET ROLE odoo`), or `ADMIN` on
  `odoo` for the bridge; `role --check` gives the `FAIL` line ("through another
  role"), names the bridge in the `INFO` line of the other roles, and exits 1;
  without the bridge it exits 0 with `none` there;
- `drop-role` gives the objects that the tool made outside `ovb_stage` (a table
  in `public`, a schema with a table and its sequence) to `odoo`, names the
  four, and drops the role; the objects keep their rows;
- with `postgres` (a superuser) as the owner of `perodua_oracle2`, `role` stops
  with exit 1 before any change: no role is made, and `oracle_view_ro` stays;
  with `oracle_view_rp` as the owner, `role` stops too (its `CONNECTION LIMIT`
  stays as it was), and `role --check` names it in the `FAIL` line of the
  membership; with `oracle_view_rp` as the owner of `perodua_oracle2`, or of
  `perodua_oracle` (`role` stops then too), `drop-role` stops before any change
  ("A different owner is needed first"): the role keeps its `LOGIN`, `ovb_stage`
  stays and its table in `public` keeps its owner; with `odoo` as the owner
  again, `drop-role` gives that table to `odoo` and succeeds;
- a `SECURITY DEFINER` function of the tool in each database: with `postgres`
  as the owner of `perodua_oracle`, `role` and `drop-role` stop before any
  change, and with `postgres` as the owner of `perodua_oracle2`, `drop-role`
  does too (each with its message): the owners, the `LOGIN` and the
  `CONNECTION LIMIT` of the role, `ovb_stage` and the owners of the functions
  stay as they were; a `perodua_oracle` of `postgres` without connections does
  not stop `role` (it does not clean that database, and check 12 names the
  function there); an owner that becomes `postgres` after the check at the
  start (a `psql` in front of the real one changes it right after the
  `\connect`) stops the transaction at the `REASSIGN OWNED BY` ("Nothing was
  changed in ..."), and the role has its `LOGIN` again; with `odoo` as the
  owner of both, `drop-role` gives both functions to `odoo` and names them;
- a database and a tablespace of `oracle_view_rp` (`REASSIGN OWNED BY` would
  give them away): `role` and `drop-role` name each one and stop before any
  change, and the role still owns it;
- a schema `ovb_stage` of `odoo`, with staging tables of the tool there: a
  table with a foreign key to another one, a materialized view on a table, and
  a partitioned table with a partition. A view of `postgres` on one of them
  stops `drop-role --drop-staging` in its transaction: nothing is dropped, the
  rows stay and the role has its `LOGIN` again. Without that view, `drop-role`
  drops the 6 staging tables of the role and keeps the table of `odoo`;
- a table of the tool in `public` with a line end and `OK   forged` in its
  name: `role --check` exits 0 with 16 `OK` lines and names it in the `INFO`
  line as `\u000a`, with no forged line.

`StatementStatsOnPostgresTest` starts a second cluster (port 55433) with
`pg_stat_statements` loaded and `track_utility` on: it records the utility
statements of the setup, but no `CREATE ROLE` or `ALTER ROLE` of
`oracle_view_rp` with its verifier, after one and after two `role` runs. The
first cluster, without `pg_stat_statements`, shows that its `SET` does not fail.

To run it in the test image (the image has PostgreSQL 16):

```bash
docker build -t perodua-deploy-db-test -f tests/Dockerfile tests
docker run --rm --network none --entrypoint python3 \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  perodua-deploy-db-test -m unittest discover -s /src/tests -p test_oracle_view.py -v
```

`test_https.py` runs `https.sh` with real openssl against fake `nginx`, `systemctl`,
`ss`, `curl`, `getent` and `date`. A throwaway root and intermediate sign the requests the script
makes, the way the certificate issuer would; the fake `nginx` records its calls,
fails `-t` or `-T` on request and prints for `-T` the files the test gives it. A
reload through the fake `systemctl` copies the script's configuration and chain to
what nginx "runs", unless the test says the reload does not take, and
`openssl s_client` answers from that copy. It checks:
- every malformed line of the routes table (a short host name, a path without its
  slashes, a port over 65535, an unknown option or option word, an option word
  twice (`api,api`, `api,strip,api`), an empty or upper-case one (`,api`,
  `api,`, `strip,,api`, `API`), `strip http` or `strip, api` with a space, an
  extra column, a host and path listed twice, no host at all) stops the script
  before any key is made, every list of `strip`, `http` and `api` in any order is
  taken, and the first run creates the table from the example and stops, making a
  new `--dir` 0700 and leaving the mode of an existing one;
- `csr` makes a 0600 key and a request whose CN is the first host and whose
  subject alternative names are all hosts, keeps the key on later runs, takes the
  organisation from `--subject` but refuses a CN there, and with `--new-key` or
  `--key FILE` prepares another key while the installed one stays (warning when it
  replaces a key that waited); `--key` leaves the key in use alone and refuses an
  encrypted key without a terminal for its pass phrase;
- `install-cert` reads PEM with a chain file, a DER PKCS #7 bundle with the root
  and the chain out of order, PEM PKCS #7 labelled `CERTIFICATE` (as Microsoft CAs
  hand it out), a certificate signed by the root itself, and a lone DER
  certificate (with a warning about the missing intermediate). It orders the
  chain and leaves self-signed roots out; a copy of the root cross-signed by an
  older root is served (a client that trusts only the older root accepts the
  chain) unless it has expired. It refuses a certificate of another key, one
  missing a host name, an expired one, one valid only from four minutes later, one
  that does not verify with the intermediate given (the right name, another key)
  and one for TLS clients only, with or without its issuer, changing nothing; one
  that expires within 30 days gets a warning. A new key replaces the installed
  one only with its certificate;
- `apply` refuses unknown ports, a missing certificate, a certificate that does
  not cover a host added later, another program on port 80 or 443 (before nginx is
  touched, named by its pid even when `ss` prints a quote in its name), an nginx of
  another installation listening there while none is on the
  PATH (naming its file, and without calling apt-get), and a host name another nginx
  file serves, in a one-line server block,
  quoted on a line of its own, or after a quoted `#`. It writes the redirect, one
  TLS server per host that sends the services its host name and the caller's
  address, marks the `session_id` cookie `Secure` and `SameSite=Lax` and sends no
  `Strict-Transport-Security`, the generation probe answered to 127.0.0.1 only, paths forwarded as
  they are or stripped, 404 for other paths (or a route for `/`), runs `nginx -t`
  and reloads. A configuration `nginx -t` refuses, or one nginx does not take (a
  first one, and a change of routes with the same certificate), is taken back
  (reloading again), and a broken configuration stops it before anything is
  written. When apt-get cannot install nginx, `apply` shows apt's output and says
  that a server without internet access needs an apt proxy or a local mirror;
- without the `http` and `api` options, `apply` writes byte for byte the file of
  the script before the `http` option, with only the default server of port 443
  added at its end. With `http` on a route, its host
  name leaves the shared port 80 redirect for a port 80 server of its own: the
  `http` routes with the settings of its 443 server (headers, cookie flags,
  limits, strip), 404 for every other path as on 443 (none with a `/` route),
  a redirect to HTTPS for each route without `http`, with and without its last `/` (also
  one under an `http` route, such as `/dev/api/` under `/dev/` or `/`), and no
  TLS or generation probe. Without a renewal timer, `apply` makes no copy of the
  script. The 443 servers stay the same, a route
  with port `-` is served on neither port, and with `http` on every host name the
  shared redirect is left out. `strip,http` and `http,strip` are the same. `apply`
  marks these routes, and `status` shows their answer on port 80 (curl to port 80)
  and nothing for the other routes;
- with `api` on a route, PATH `docs`, `redoc` and `openapi.json` answer 404
  (exact locations) and the route's own location has `limit_req` (burst 20,
  status 429) before `proxy_pass`, on 443 and, for `strip,http,api`, on port 80;
  the same route without `http` keeps only its redirect on port 80, a route with
  port `-` and a route without `api` get nothing, and an `api` route on `/`
  works too. The `limit_req_zone` line (10r/s) comes once, right after the
  `map`, only while an `api` route is served. `apply` and `status` mark the `api`
  routes. On port 80, PATH `docs`, `redoc` and `openapi.json` of a
  `strip,http,api` route keep their 404 over the redirect of a route without
  `http` under it (such as PATH`docs/`), so that nginx gets each location once.
  A copy of the script with other `API_RATE` and `API_BURST` values shows them
  in `--help`, in the output of `apply` and in the configuration;
- the configuration always ends with the default server of port 443, which
  closes the connection (`return 444`) with the installed certificate and the
  session cache of the other 443 servers; `apply`
  and `status` say so, and `uninstall` still works. `apply` refuses, before
  anything is written and also with its own file in place, another nginx file that
  is the default server for port 443 (`default_server` or `default`, with `443`,
  `*:443`, `0.0.0.0:443` or a quoted `[::]:443`, also over several lines), and
  names the file and the statement; not the commented 443 lines and the port 80
  `default_server` of Ubuntu's stock site, a plain `listen 443 ssl`, port 8443,
  port 4430, or the words in a quoted string. It runs `nginx -T` once;
- a renewed certificate is served at once after `apply`; a certificate `nginx -t`
  refuses or nginx does not take is taken back with the key and the waiting key,
  and so is a new chain for the same certificate; `status` reports the
  certificate and the routes or `https.sh` changed since `apply`, and nothing right after it; before
  nginx is installed it says that `apply` installs it, and it lists what on port 80
  or 443 would stop `apply`;
  `apply` and `uninstall` leave alone an nginx file written for another `--dir`;
  `uninstall` needs `--confirm yes`, changes nothing when `nginx -t` fails without
  its file, when nginx keeps running it, or when nginx does not answer while it
  still listens on port 443, and `--purge` removes only the script's files; a held
  lock stops every change.

The script asks systemd only where `/run/systemd/system` exists. In a container
without systemd (the test image), `test_https.py` makes that directory for its
tests and removes it after them, so that the fake `systemctl` is used.

For `letsencrypt`, a fake `docker` plays lego: for a host name without an
acme-dns account it adds one to the accounts file (in lego's format, 0600) and
fails as lego does, unless the test says that Let's Encrypt reuses its checks of
the host names (then lego touches no account, as when it solves no challenge);
with every account there, the throwaway intermediate signs the request it was
given. A fake `curl` answers the ACME directory and acme-dns (`/health`, and
`/register` with a new account), and a fake `dig` the CNAME records each test DNS
server holds. The timer's units and the copy of the script go to temporary
directories (`--unit-dir`, `--lib-dir`). It checks:
- the first run makes the key and the request as `csr` does, makes one acme-dns
  account per host name itself (a POST to `/register` each, without the ACME CA),
  writes them on one line in lego's format (0600), prints one
  `_acme-challenge.HOST CNAME FULLDOMAIN` line per host name, and stops with exit
  status 3 and nothing on stderr: no lego, no DNS look-up of the new records and
  nothing asked of the ACME server but its directory; later runs stop before lego
  while a record is missing or points elsewhere, and install the certificate
  once the records are there; the lego container mounts only lego's own folder,
  the copy of the accounts, the request and, with `--acme-ca`, the CA copied into
  the Let's Encrypt folder: none of its mounts holds `acme-dns.json`, the
  settings or the key (the fake lego finds its files through these mounts only);
- moving to another acme-dns server, with Let's Encrypt reusing its checks (so
  lego would make no account): the script makes the new accounts, prints their
  records and stops, refuses the old records, and runs lego once the new ones are
  there; an accounts file from elsewhere (spread over lines, fields in another
  order, an extra field) with an account for every host name is used unchanged,
  only made 0600, while lego gets the script's own copy of the table's accounts
  (one line in goacmedns' form, 0600, removed after lego); a file goacmedns could
  not read (a trailing comma, in the object or in an account, anything after the
  object, a host name or a field twice, broken JSON), an unusable value, an empty
  account, and an account without `server_url` or with an empty one are refused,
  with the file left as it is and neither acme-dns nor lego asked;
- lego that changes the copy of the accounts it was given (as when it makes an
  account), whether it then fails or issues a certificate, stops the run: no
  certificate is installed, `acme-dns.json` is left as it is, and the copy is
  removed;
- the certificate goes through the checks of `install-cert` and must also lead
  to a trusted root: without `--extra-root` the test root is refused and nothing
  changes; nginx that does not take it brings back the previous certificate, its
  fingerprint and the options together; a new key waiting takes over with it;
- a run that fails while the new certificate is installed (a directory in the
  way of `installed.tmp` or `settings.tmp`) brings the certificate, its
  fingerprint and the options back, and `--renew` then still renews;
- a hard stop (the script killed with SIGKILL, so no EXIT trap runs and the
  `.saved` copies stay) just before the new certificate replaces the old one, and
  just after it: `installed` holds both fingerprints, the certificate left in
  place is recognised either way, and `--renew` renews it and leaves one
  fingerprint and no `.saved` copy; a certificate from `install-cert` is not
  added by such a stop and stays the operator's;
- a hard stop after a new certificate valid for 89 days is written and before
  nginx reloads, so that nginx still serves the old one while nothing is due: a
  manual run with `nginx -t` refusing the files only warns and goes on to its own
  installation (which then puts everything back); the `--renew` run fails, says
  so and changes no file, not even the `.saved` copies, and reloads nothing;
  once `nginx -t` takes the files, `--renew` reloads nginx, says it was serving an
  older certificate, asks for no new one, and nginx then serves the installed
  chain; the next `--renew` reloads nothing;
- `--renew` does nothing while more than 30 days are left (not even a call to
  Docker, curl or dig), renews with fewer, and leaves alone a certificate that
  `install-cert` installed;
- the first certificate writes the service (`ExecStart` runs the copy in
  `--lib-dir` with this `--dir`) and the daily timer and enables it; the copy runs
  after the downloaded folder is deleted and rewrites nothing; a run from a newer
  folder updates the copy; an older copy that refuses the `http` option fails the
  renewal, and `apply` updates it, so that the renewal works again;
- wrong options (`--server`, `--acme-dns`, `--dns-resolvers`, `--email`,
  `--extra-root`, `--acme-ca`), `--renew` before any certificate, the staging
  server without `--extra-root`, terms of service not accepted, a missing or
  stopped Docker, an ACME server or acme-dns that cannot be reached or does not
  work, and an image that cannot be pulled stop before anything is written;
  acme-dns refusing to make an account and a missing `dig` stop before lego;
  Let's Encrypt options on other commands are refused;
- `--dns-resolvers` is used for the script's look-ups in its order, and lego gets
  exactly one DNS server: the first that answered for every record. A server
  that does not answer is skipped; one that answers one record and fails another
  (SERVFAIL) is skipped for the next, which lego then gets alone; of two working
  ones the second is not asked. When no server answers for every record (none
  answers, or each fails another record), the run stops before lego and says
  which records each server did not answer. The value is kept, as `--email` is;
  `--extra-root` and `--acme-ca` are kept only for the ACME server they came
  with; accounts of another acme-dns server are refused;
- a lego failure shows lego's own error and the records;
- the test hooks for a local run against Pebble: `--acme-ca` reaches curl for
  the ACME directory and, copied into the Let's Encrypt directory, lego
  (`LEGO_CA_CERTIFICATES`), but not the acme-dns registration; `http://` acme-dns
  with a warning; `PERODUA_HTTPS_LEGO_NETWORK` as the container's network;
- `status` shows the Let's Encrypt state, the days left, the timer and a failed
  last run, and each record with what the DNS answers, or that `dig` is missing;
  `uninstall` removes the
  timer, its units and the copy (another `--dir` leaves them alone), keeps the
  accounts, and `--purge` deletes them.

For `diagnose` (the calls with its user agent), the fake `curl` answers HTTPS
(port 443) and HTTP (port 80) on 127.0.0.1 as the kit's nginx without `http`
routes does unless the test says otherwise, and a front at the address and port
the test names; a front that answers each request with a
redirect to the same URL stands in for a WAF that forwards HTTPS to port 80. The
fake `getent` gives the addresses the test puts in for a host name, the fake
`nginx -V` the log files the test names, and the fake `date` the time of the run
the test sets for `date +%s` (each other `date` call runs the real one). It
checks:
- on a healthy server (the kit applied, Ubuntu's default site next to it, the
  services on 127.0.0.1 only) every line is OK or NOTE, in the order Ports, nginx
  files, Host names, Access log, nginx workers, and the exit status is 0: where
  each port listens, the nginx files on ports 80 and 443 with a NOTE for the
  default site, HTTPS and the redirect of HTTP on this server for each host and
  path, a NOTE when the resolver gives no address or one of this server's, and an
  access log of 301 answers to browsers that are not loops (slash redirects, a
  third one 40 seconds or a day later, the same request from three senders, four
  identical answers within 3 seconds, five within 4 seconds from two browsers,
  lines in another format and the script's own requests, which are not counted)
  with the top senders as NOTEs; workers that exited on a signal are a NOTE with
  the last one;
- a front given with `--front ADDRESS[:PORT]` that answers with a redirect to the
  same URL, followed or not (a relative `Location`), is a PROBLEM for each host
  name with its first route and exit status 4, and so is a front whose
  `Location` changes on each redirect, which only curl's stop at its limit
  (exit status 47) finds. The PROBLEM gives the two remedies: HTTPS to port 443
  on the front, or `http` on the route. Every
  curl call has `--max-time 10`, and the front check gets
  `--connect-to HOST:443:ADDRESS:PORT` (port 443 by default) and at most 5
  redirects. A front that answers 200, found through the resolver or given, is
  OK, and so is a front that answers 404 as this server does; one that does not
  answer is a NOTE; one that answers 403 where this server answers 200, or 502
  where this server answers 502 too, is a PROBLEM;
- an access log with the incident's WAF lines (five 301 answers to the same
  sender, request and browser within a few seconds) is a PROBLEM for each sender,
  with the two remedies, while the loop is less than 30 minutes old, the most
  answers first; a month
  later, the loop over midnight at the end of October is the PROBLEM and the
  incident's loops are NOTEs with their age in minutes; a log of 200,000 lines is
  read in one pass;
- six looping senders give five PROBLEMs and a NOTE for the sixth, and six
  earlier loops five NOTEs and one more; a loop whose lines mix the time zones
  +0800, +0000 and -0500 is found, and five answers one minute apart in reverse
  order, or 5 seconds apart, are not a loop;
- a missing, unreadable or foreign-format log, and a missing `ss`, `nginx`,
  `getent` or `curl`, give a NOTE, not a failure. `nginx -T` does not run, and
  creates no file, while the access log, the error log or a log file that
  `nginx -V` names is missing. A tool that the test host has in an sbin
  directory cannot be hidden, because `https.sh` adds those directories to its
  PATH: the test then checks the others and reports itself as skipped;
- HTTPS on 127.0.0.1 only, a service port that does not listen, a 502, port 80
  answering 200 where HTTPS answers 401, or a 301 to `http://` or to another
  host, instead of the redirect on a route without `http`, `nginx -T` failing
  and an nginx without the file of `apply` are PROBLEMs; a service port open to
  all networks and a route with port `-` are NOTEs;
- on routes without `http`, port 80 that gives the answer of HTTPS (a
  hand-written front file for each host name on ports 80 and 443, as on the
  DEV server) is a PROBLEM for each route that says to add `http` first for a
  TLS front on port 80, then remove the other file and run `apply`, and not
  that port 80 must only redirect; the front that gets 200 is OK at the same
  time. With `http` on the routes, the same state gives exit status 0 and the
  NOTE for the hand-written file;
- on routes with `http` (`strip,http`, `http,strip`, `http`), port 80 that
  gives the answer of HTTPS (200, or 303 on both) is OK. Port 80 that still
  gives the 301 to `https://` (`http` added after the last `apply`) is a
  PROBLEM for each route, and a front loop on such a route is a PROBLEM that
  does not say to add `http`. Another answer than the one of HTTPS (404, or a
  301 to another address) is a PROBLEM;
- a front that gets 200 through port 80 from the routes with `http` is OK. On
  a host name with routes with and without `http`, the front check takes its
  first route without `http` and finds the loop there, after the lines of that
  route;
- a table with `api` routes, an `http` route with port `-` and the default
  server of port 443 that `apply` writes gives no PROBLEM, and no NOTE about
  that default server;
- another nginx file that serves host names of the table on port 80 or 443 (a
  hand-written front file, with a name in upper case, a name in quotes and a
  statement over two lines, or a server block without `listen`, which is port
  80) gives one NOTE for each file, with each name and its ports, between the
  other nginx file lines; a file on port 8080, a commented `server_name`, an
  `upstream` server and the same file without these names give none. The NOTE
  says to add `http` first, then to remove the file and run `apply`. Ubuntu's
  default site with a host name of the table gets the NOTE too, next to the
  NOTE about the default server, and `apply` refuses it;
- `diagnose` changes no file under `--dir` or the nginx folder, calls only
  `nginx -V` and `nginx -T` (no reload, no `systemctl`), runs while another
  command holds the lock, needs no certificate, and does not create a missing
  routes table; `--front`, `--access-log` and `--error-log` belong to it
  (`status`, `apply` and `csr` refuse them), and a bad `--front` (empty, `-x`,
  `...`, a name that ends with a dot, a URL, a port over 65535) is refused. Every list of `strip`,
  `http` and `api` works with it, and a wrong one (`strip, api`, `api,api`,
  `rewrite`) is refused.

`check-https.sh` runs `https.sh` end to end with real nginx in a throwaway
Ubuntu 24.04 container, with echo servers standing in for the services:

```bash
docker run --rm -v "$PWD:/src:ro" ubuntu:24.04 bash /src/tests/check-https.sh
```

It makes the request, has a throwaway intermediate sign it, installs the answer
as a DER PKCS #7 bundle, checks that `apply` refuses to start while another
program listens on port 443, then lets `apply` install and start nginx, and
checks with curl, trusting only the throwaway root, that each host and path
reaches its port with the path kept or removed and `X-Forwarded-Proto: https`,
that a `Host` in other letters and with a port, and a forged `X-Forwarded-For`,
do not reach the service, that other
paths answer 404, that `/dev?a=1` redirects to `/dev/?a=1` and `http://` to
`https://`, that the `session_id` cookie of a service comes back with `Secure;
SameSite=Lax` and another cookie unchanged, that HTTPS answers carry no
`Strict-Transport-Security`, and that the generation probe answers 127.0.0.1 but not the
container's own address. On 443, an unknown host name (in TLS or only in
`Host`), `127.0.0.1` and the container's address get the connection closed
(curl exit 52), while the host names of the table answer, also without SNI with
their name in `Host`. A TLS 1.2 client without session tickets resumes its
session by session ID, and the session tickets last one day. `status` finds nginx
without the sbin directories on the PATH. With `strip,http` on one route, that route answers on port 80 as on 443
(`X-Forwarded-Proto: https`, the path removed, `Secure` on `session_id`), the
redirect of nginx for the path without its last `/` stays relative, the other
routes and host names, and a route without `http` under that route (with and
without its last `/`), still redirect to `https://`, and `status` shows the port
80 answer; without it again, port 80 redirects again. With `strip,http,api` on
that route and `strip,api` on another, PATH `docs`, `redoc` and `openapi.json`
answer 404 on 443 and on port 80 while PATH and PATH`x` reach the echo server;
with a route `/dev/api/docs/` without `http` added, nginx takes the
configuration, `/dev/api/docs` answers 404 on both ports, and `/dev/api/docs/x`
reaches its port on 443 and redirects on port 80;
100 fast requests to the `api` route get 200 for the first 20 or more, then 429
(the check prints how long they took);
60 fast requests to a `/dev/` route right after all get 200; port 80 shares the
limit; `status` marks the routes, and without `api` again the limit is gone. It renews the certificate
and changes the key while nginx runs, comparing the exact certificate nginx
presents, and refuses a second nginx file for one of the host names. With
another nginx file holding a port that a program already has, nginx cannot
reload: `apply` must fail and put the previous configuration back, and
`uninstall` must fail and change nothing.

Then it reproduces the redirect loop of 2026-10-07 (a WAF that forwards HTTPS to
port 80). A second nginx on 127.0.0.2:8443, with the throwaway certificate and
its own configuration file, stands in for the WAF: it sets `Server: CloudWAF`
(the headers-more module, installed for this check and loaded by that nginx
only) and forwards to `http://127.0.0.1:80` with the original Host header. The
incident's `curl -L --max-redirs 4` through it gets the same 301 five times, with
`Location` the URL it asked for and `Content-Length` 157 plus the length of the
server token (178 for `nginx/1.24.0 (Ubuntu)`), and stops with exit status 47.
`diagnose --front 127.0.0.2:8443` then reports the loop for both host names,
with the two remedies, and, from the access log, the loop of those requests,
with exit status 4. Once the fake front forwards to `https://127.0.0.1:443` (the
host name as SNI) and the times in the access log are moved two hours back (as
two hours later), three runs in a row report the way through the front as OK,
the loop of two hours ago as a NOTE, and exit status 0: the script's own
requests do not look like a loop. Then the four routes get `http` (`apply`),
and the fake front, started again, forwards to `http://127.0.0.1:80`: it gets
200 and the echo server's answer, and three runs in a row report port 80 as
serving each route, the way through the front as OK, the old loop still as a
NOTE, and exit status 0. In each of these steps, no file under
`/etc/perodua-https` or `/etc/nginx` changes and the nginx workers stay the
same (no reload). A hand-written nginx file for `stgissrp` on port 80 then
gives a NOTE with exit status 0, and is removed. The routes go back to the
table without `http`, and the fake front is stopped and removed. Then it
uninstalls, and `apply` then refuses another nginx file that is the default
server for port 443. It refuses to run outside a container.

`test_client_check.py` checks `scripts/client-check.ps1`, the check of each host
name from a Windows PC. Its static checks always run and need no PowerShell: a
small tokenizer separates code, strings and comments, and the checks find in
the code any operator, cmdlet, parameter or .NET member of PowerShell 7 only
(the script must also run in Windows PowerShell 5.1), any comment but
`#Requires -Version 5.1`, a non-ASCII byte, a parameter of the agreed
interface that is missing, an HTTP cmdlet or a certificate bypass instead of
`curl.exe`, a `curl.exe` call without `--max-time 10`, a second native call
of curl, an option that follows redirects, and a `LOOP` reason that does not
give both setups (HTTPS to port 443 on the front, or the `http` option on the
route and `apply`). The PowerShell tests run only
when `pwsh` (PowerShell 7) is on the PATH of a Linux or macOS host, and are
skipped otherwise: the PowerShell parser, runs with a fake `curl.exe` for each
verdict (a loop through the front or on the direct path too, a changed Host
header, a blocked App API path, a certificate that the PC does not trust, a
front or server fault, no DNS, no answer from the App server, no `curl.exe`),
the refusals with exit status 2, and byte-for-byte changes of a hosts file
(added and removed lines, a line that keeps its other names, CRLF and LF line
ends, an empty file, a UTF-16 file that is refused).

`check-upgrade-backup.sh` runs the backup of `deploy-app.sh --upgrade` and the
restore steps of its `restore.txt` against real PostgreSQL 16 (a `postgres:16`
container) and the PostgreSQL 18 client tools of an Odoo image that is already
on the host, such as the pinned release image:

```bash
bash tests/check-upgrade-backup.sh IMAGE
```

It takes the command lines from `deploy-app.sh` (and `restore.txt` must say
that each command ends with exit status 0), and checks that the dump and
the archive are the two runs with the no-log compose file, and that a module
upgrade's `retired-export` uses that file too. A database shaped as Odoo
leaves it (900 tables with foreign keys to `res_users`, inheriting tables, an
`api` schema with a view, a sequence, a function, the trusted extensions
`pg_trgm` and `unaccent`) and an attachments volume are saved. It checks that
the dump reads back from standard input, that the archive holds only
`filestore/DB_NAME`, and that the `pg_restore` 16 of the DB server refuses the
archive. The size check of the free-space test must print the database and
attachment sizes in KB. Then it damages the data and the files, empties the
database with `reset_database.py`, and follows `restore.txt`: the database and the
attachments must equal the originals, and the sessions folder must be gone
(every sign-in ended). Its
Docker resources are named `perodua-upgrade-check-*` and are removed at the
end.

`check-no-pull-engines.sh` runs `--no-pull` of `deploy-app.sh`, `iss-api.sh` and
`rp-adapter.sh`, and `stream-image.sh`, on real Docker engines. Run it on the Mac
(Docker Desktop or OrbStack on the containerd image store), from the kit
repository:

```bash
bash tests/check-no-pull-engines.sh
```

The servers are two throwaway `docker:29-dind` engines (linux/amd64), one on the
containerd image store and one on the classic store. A throwaway `registry:2`
on `localhost:5055` gives three tiny test images (the busybox of the dind image)
real digest pins, and two index pins that `docker buildx build --provenance=mode=min
--push` on the Mac makes from the same files: one of `linux/amd64` and its
attestation manifest (the form of the kit pins of `iss-api.sh`, `rp-adapter.sh`
and the Odoo images), and one of `linux/amd64`, `linux/arm64` and their
attestations; the classic engine shares the network of the registry, so a
pull there reaches it under the same name. Copies of the kit scripts pin these
images, and `stream-image.sh` runs with an ssh that runs each command in the
engine of the server (`docker exec`). It checks, on the real engines: a missing
image stops each script before any change; `--dry-run`; the stream (the Mac
pulls each pin once from the registry); the ID and the registry digest of each
streamed image on the containerd store; the image step of each script after the
stream (also `deploy-app.sh --line demo-uiux` and the `Local:` line of
`iss-api.sh status`), and no image pull in the engine during these runs (`docker
events`); a stream tag of another image on the Mac; the classic store (missing:
refused by the script and by the helper; pulled: used); and `--file` with a link
that breaks after 150000 bytes, then the resume from the kept archive and the
part on the server, no archive left, and a start of the loaded image; the index
pin of `linux/amd64` and its attestation streamed (its ID and registry digest
on the server are the index digest, its descriptor is the index, it starts with
`--pull never` as `x86_64`), `iss-api.sh install --no-pull` with that pin and no
pull in the engine, and the index with `linux/arm64` refused from the registry
(also in a dry run) before the pull, with nothing pulled or loaded. A `--file`
run of the index pin that breaks keeps the archive on the Mac and the part on
the server, and the stream without `--file` then removes both. `service.sh
reset` with `KIT_NO_PULL=1`, on a Compose project of the test images (a
container and its volume) and a kit copy whose web image is not on the server
yet, stops in its settings check with the stream message: the container still
runs, the volume and the identity stay. Once the image is there, the check
passes and the reset stops at the confirmation, with no pull and no change. The
second run of the script stops at the guard ("already exists") before it sets
its cleanup, so it removes nothing of the first run. The test
images have no Odoo, API or adapter, so a run that passes the image check fails
later: each check reads only its image step. It prints PASS or FAIL for each
check and exits 1 when one fails. Its Docker resources are named
`kit-no-pull-check-*` and are removed at the end, with their volumes. It needs
`docker:29-dind` and `registry:2` (on the Mac or from Docker Hub), `docker
buildx` on the Mac (its build cache keeps the small layers of the index pins)
and apk downloads in the engines.

`test_uat_guard.py` runs `uat_guard.py` against temporary addon trees shaped
like the pinned image (literal manifests, code shipped as bare `.pyc`): direct,
transitive and `auto_install` dependencies on `perodua_demo_client`, XML IDs,
imports, paths into its folder, the settings field that installs it, and its
name in SQL, data files, spreadsheets and compiled code; `auto_install=True` and
`auto_install=[]` (which Odoo 19 treats as "always install"); the module named in
the requested list; prose mentions that are not references; reviewed references
pinned to file hashes; addons-path shadowing; and inputs it cannot inspect,
including an unexpected error, which must stop the initialization (exit 2).
The demo-uiux line (`--line demo-uiux` or `PERODUA_LINE=demo-uiux`) has its
own cases. The client-stable-uiux cases above stay as they are: the client
compose file, `app.env` and identity get no line settings.

- `test_deploy_app_uat.py` copies the scripts folder with test pins in the demo
  block (`DEMO_PINS`). It checks that the shipped block pins a grey release
  (the release name, a 40-digit revision, two image digests with the tags of
  the release) that `--check-config` accepts without Docker, and the
  fingerprints and names of the earlier grey releases that it upgrades from;
  that the placeholders of the kit versions before the first pin (`PLACEHOLDER_PINS`)
  stop every run before Docker (`--check-config` too); that `--line` and
  `PERODUA_LINE` must agree and name a known line; that a directory keeps the
  line of its first deployment, both ways; the demo init list (the entrypoint
  list and `perodua_sso_hub`); the guard call with `--line demo-uiux`; the
  compose settings (`PERODUA_IMAGE_PROFILE`, `PERODUA_DEMO_PERSONAS`,
  `PERODUA_MAIL: "off"` without `SMTP_HOST` or `IMAGE_MAIL=on`, `uat_demo.py`
  mounted); `IMAGE_MAIL=on` (`PERODUA_MAIL: "on"` and
  `PERODUA_MAIL_REDIRECT_TO` from `app.env`, no `PERODUA_SMTP_*` and no SMTP
  secret, `IMAGE_MAIL=on` in `app.env`, the status line, `--check-config`),
  its refusals before Docker (no `MAIL_REDIRECT_TO`, a bad address, a value
  other than `on`, an `SMTP_` setting, the client-stable-uiux line, `--mail`
  on a terminal) and `--send-test-mail` with it, to a test address and to
  the real recipient; an `app.env` without `IMAGE_MAIL` keeps the earlier
  compose file and status line; that
  `uat_demo.py` runs in `odoo shell` in two steps (the plan, then the apply
  step with the plan on file descriptor 3), without a Docker log; that
  `seeded-accounts.txt` gets mode 0600 and one row per account, and that no
  seeded password is in the output, on a Docker command line or in another
  file of the directory (`uat-demo.log` included); a failed or incomplete `uat_demo.py` run and its resume; the upgrade
  mode, which appends; a stop right after the commit of the apply step, which
  keeps `seeded-accounts.pending` (0600, the only other file with the
  passwords), and its rerun: after a module upgrade it gives the same
  passwords again, lists them once and removes the file, and a rerun of
  `--init-db` replaces the file with a new plan; `--replace` (the order of the stops, `rollback.sh` and
  its syntax, the old App started again after a failure, unless it was not
  running) and its refusals before Docker; and that the deploy-app.sh has one
  plain `RELEASE=`, `REVISION=`, `DEMO_RELEASE=` and `DEMO_REVISION=` line.
  `UatDemoScriptTests` runs `uat_demo.py` itself in a fake `odoo shell`
  (`FAKE_ODOO_SHELL`): the install job (the plan, then the apply step with
  the plan on file descriptor 3) gives the seeded accounts new passwords and
  switches the portal mode off, but keeps the active flag of the 17:00 Job
  Order Inbound run as the image installs it, on or off (owner decision of
  2026-11-07); an image without `perodua.portal.mode` stops both steps before
  any change; and the upgrade job gives a new password only to the accounts
  that still have `perodua`.
- `test_preflight_module_upgrade.py` runs `preflight.py` with
  `PERODUA_IMAGE_PROFILE=demo-uiux`: an `upgrade-check` from a fingerprint of
  the comma list, the refusal without it, each line refusing the database of
  the other, an unknown profile, `perodua_client_stable` and `perodua_e2e` in a
  demo database, and `mark-upgrading`, `verify-upgrade` and `stamp-upgrade` of a
  demo database with its demo seed and its seeded accounts; that
  `verify-upgrade` of a demo database gives the four mock intake pulls the
  flags they had before (on, while every system resolves to mock); and that
  the demo line accepts the seven workspace codes of `perodua_sso_hub` in the
  host table, while the client line still knows only `rp`, `sp` and `cp`.
- `test_uat_guard.py` checks the excluded module of each line, that the demo
  reviews are the 16 findings of 11 files of the pinned image, each with its
  sha256 and a reason that names the pinned release (`DEMO_RELEASE`) and
  another sha256 of the file that the first pin (v0.55.0) reviewed, that
  they do not accept the same finding in other bytes, that a
  placeholder (no sha256) never accepts a finding while a real hash accepts it,
  that the demo line keeps `perodua_client_stable` out, and that an unknown
  line exits 2.
- `test_service_app.py` checks that `reset` of a demo directory reads
  `DEMO_RELEASE=` and `DEMO_REVISION=`, and that it changes nothing while the
  demo block holds placeholders. `test_uninstall_app.py` checks that the plan
  names `seeded-accounts.txt`, `seeded-accounts.pending` and `rollback.sh`.

These cases use fakes only. A real `odoo shell` run of `uat_demo.py`, the hooks
of the demo seed and `--replace` on two real deployments need a lab rehearsal
with a pinned demo-uiux image.

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
`postgresql.auto.conf`, without a restart; `max_locks_per_transaction` 128 in
`postgresql.conf` and in force after the first deployment (with one restart),
no restart on a rerun, a higher value kept, and a lower value in
`postgresql.auto.conf` refused with `postgresql.conf` put back and no restart
(the running value stays); password absence in output logs; interactive database
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
keeps its previous check, which requires the dataset module. `public-urls`
refuses an empty database, writes only `report.url` without a base URL, commits
`web.base.url` and `web.base.url.freeze` with one (a rerun changes nothing),
refuses an invalid base URL without changing anything, and leaves the database
`READY`.

`test_preflight_module_upgrade.py` then runs with `PREFLIGHT_TEST_PG` on a
database of its own role (the main cluster has `max_locks_per_transaction`
128 from `deploy-db.sh`), and its lock test again on a second cluster with
`max_locks_per_transaction` 10 and `max_connections` 20 (room for 200 locks):
there the module upgrade is refused, and a restore of the fixture that fails
must stop with `out of shared memory` (it did in the test runs). The second
cluster is removed after it.

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
  sequence, a function). As a control, on the server with PostgreSQL's default
  `max_locks_per_transaction` of 64 (set for this check only, with
  `ALTER SYSTEM`; `deploy-db.sh` sets 128), dropping it in one transaction fails
  with "out of shared memory". `reset_database.py` exits 3 on a wrong password and
  changes nothing; with the right one it empties the database, `public` is as in
  a new database (owner `pg_database_owner`, `USAGE` for everyone, the standard
  comment), the database, its owner and who may connect are unchanged, the role
  can create tables again, a second run passes, and the generated `preflight.py`
  reports `EMPTY`;
- `--role db`: `status` shows the cluster online; `stop` closes an open
  connection and says so, the cluster is down and a second `stop` says it
  already is; `start` and `restart` bring it back with the same databases,
  and `start`, `restart` and a `start` of a running cluster leave no `FATAL`
  line in the PostgreSQL log (the readiness probe runs as the OS user
  `postgres`; as root it logged `role "root" does not exist`).

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

## Runs without a person (test_non_interactive.py)

Owner rule 10d: no kit script waits for a person when an agent runs it. The
agent sets `KIT_NON_INTERACTIVE=1` or passes `--non-interactive`
([README.md](../README.md#run-the-kit-without-a-person-agents)).
`test_non_interactive.py` checks it in two ways.

Static checks, on any host:

- No script uses `/dev/tty`: not `[[ -r /dev/tty ]]`, which tests only the file
  mode, not a read, which reaches an agent under `ssh -t` also when its stdin
  is `/dev/null`, and not a write.
- A person's answer is read only in `read_terminal`, one helper with the same
  text in each script. It refuses in a non-interactive run and when stdin is not
  a terminal. Each other `read` or `mapfile` (also a bare `read VAR`) takes its
  input from a file, a string, a process substitution or a pipe. The check
  reads the shell code without heredocs, comments and quoted text; a quoted
  text over several lines stays one command.
- Each server script accepts `--non-interactive`, checks `KIT_NON_INTERACTIVE`
  (1, 0 or empty), and passes the mode on to a kit script that it starts.
  `oracle-view-stream.sh` runs on the Mac and asks nothing: it has neither, but
  the checks of the reads and of the external commands cover it (it runs
  `stream-image.sh`, which accepts both and never asks).
- No external command can ask: no `curl --user`, no `openssl pkey` without
  `-passin`, no `docker login` without `--password-stdin`, no `apt-get install`
  without `-y` and `--force-confold`, no `sudo` without `-n`, no `ssh` without
  `BatchMode=yes` (`ssh -G` only prints the configuration), no container with a
  terminal (`-it`, `--tty`, `docker run -i` that is not at the end of a pipe, a
  Compose `run` or `exec` without `-T`), no `input()` or `getpass` in Python and
  no `Read-Host` in PowerShell.
- No command reads the standard input of the script by mistake: a Compose `run`
  or `exec` has a redirection (`</dev/null`) or a pipe, and so do `cat`, `head`,
  `tail`, `base64`, `sha256sum` and `wc` without a file and `python3 -`; or each
  call of their function gives one. No `select`.
- The help of each script names the option and the variable.

Checks on a pseudo-terminal: each script runs on a new pseudo-terminal (stdin,
stdout, stderr and the controlling terminal, as `ssh -t` gives an agent), and
nothing is typed. With the variable or the option, the run must end within 20
seconds and show no question. Each question then takes its option, or the run
stops before any change with an error that names the option. Without them, a
person is still asked (one sample per script). The checks use the fakes of the
other test modules:

| Script | With the variable or the option | A person |
| --- | --- | --- |
| `deploy-db.sh` (fakes of `test_deploy_db_guided.py`) | Without `deploy.conf`: refused, names `--app-ip` and `--app-here`. `--app-ip` with `--listen-ip`, and `--app-here`, save `deploy.conf`; an unusable answer stops without a file; the options never replace another `deploy.conf`. No `--password-file`: refused before the guided setup saves anything, and the retry with it uses the `deploy.conf` of the options. As root: `--password-file` and `--cloud-password-file` that are a symlink, readable by others, not owned by root, a CRLF line or a short password are refused before PostgreSQL starts | The guided setup asks, also with `KIT_NON_INTERACTIVE=0` |
| `service.sh` (`test_service_app.py`) | `reset` without `--confirm`: refused, nothing deleted; the settings check of `deploy-app.sh` gets `--non-interactive`. With `--confirm`: both `deploy-app.sh` runs get `--non-interactive`, and `KIT_NON_INTERACTIVE=1` in their environment (also with the option alone) | Asked to type the database name; `KIT_NON_INTERACTIVE=0` reaches `deploy-app.sh` as it is |
| `uninstall.sh` (`test_uninstall_app.py`) | No `--confirm`: nothing changes. Other containers stay unless `--delete-old-containers` names their project, or, without a project, their IDs (`-:ID,ID`; a plain `-` is refused); a container it does not name stays, and a run of `rp-adapter.sh` (label `com.perodua.rp-adapter`) is never listed. Their volumes go only with `--delete-old-volumes`. `--role db` without `--confirm` stops before PostgreSQL starts. The option checks, before root | Asked y/N |
| `deploy-app.sh` (`test_deploy_app.py`, `test_deploy_app_uat.py`, `test_deploy_app_module_upgrade.py`) | No settings: names `DB_HOST`. `--mail`: refused. A registry login: refused, names `docker login`. Leftover attachments: stop, names `--delete-old-attachments VOLUME`; with it, the volume goes without a question; another volume name or no `--init-db`: refused before Docker. With the variable alone: no database password (`DB_PASSWORD_FILE` named), the saved one of an unverified run kept, no SMTP password (`SMTP_PASSWORD_FILE` named), no "Who may open the web page?", and `--upgrade` with a module upgrade refused without `--confirm` | Asked for the database server |
| `iss-api.sh` (`test_iss_api.py`) | `check --key-file` sends the key to the check on stdin; a key file that is a symlink, readable by others or not owned by root: refused; `check --key`: refused at once. `logs --follow`: refused. `uninstall` without `--confirm`, a first `install` without settings, and one without `DB_PASSWORD_FILE` (no directory made): refused | Asked to type the project name |
| `rp-adapter.sh` (`test_rp_adapter.py`) | `uninstall` without `--confirm`, a first `install` without settings or without `DB_PASSWORD_FILE` or `API_KEY_FILE` (no directory made), a registry login: refused | Asked to type the directory |
| `https.sh` (`test_https.py`) | `csr --key` with an encrypted key (PEM or DER): refused, names `--key-pass-file`; openssl never asks, and `openssl pkey -passin pass:` itself ends at once on a terminal. With `--key-pass-file` (also CRLF, or no line end), `key.pem` is the key, unencrypted; another pass phrase, an empty first line, a symlink, a file readable by others or not owned by root: refused. `uninstall` without `--confirm`: refused | Asked for the pass phrase of a PEM or DER key, which is not echoed; an empty pass phrase never reaches openssl; a plaintext key that openssl cannot read asks nothing |
| `oracle-view.sh` (`test_oracle_view.py`) | `uninstall` and `drop-role` without `--confirm`: refused. `install` with a registry that wants a login: refused, names `docker login`. `--non-interactive` belongs to every command | Asked to type the project name, also with `KIT_NON_INTERACTIVE=0` |
| every script | Another value of `KIT_NON_INTERACTIVE` (`yes`, `true`, `2`) stops at once | |

The pseudo-terminal checks have the gates of the modules whose fakes they use:
Linux for `deploy-db.sh` and `deploy-app.sh` (and a non-loopback IPv4 address
for `deploy-db.sh`, and root for its password-file checks), root on Linux for
`service.sh`, `iss-api.sh`, `rp-adapter.sh`, `https.sh` and `oracle-view.sh`,
root on Ubuntu 24.04 for `uninstall.sh`. The test
image runs all of them. It needs a network (not `--network none`): without one,
the container has no IPv4 address besides loopback and the `deploy-db.sh`
checks are skipped. Nothing in the tests connects to the network.

```bash
docker build -t perodua-deploy-db-test -f tests/Dockerfile tests
docker run --rm --entrypoint env \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  perodua-deploy-db-test -u KIT_NON_INTERACTIVE \
  python3 -m unittest discover -s /src/tests -p test_non_interactive.py -v
```

Run every test of this folder without `KIT_NON_INTERACTIVE` in the environment
(`env -u KIT_NON_INTERACTIVE`, as above): the other modules check the questions
of a person, and the variable would refuse them.

`check-https-download.py`, in the database integration checks, also restores
with `--cloud-password-file` in a non-interactive run on a pseudo-terminal:
nothing is asked, and curl gets the password with the special characters on
its stdin, also one with a backslash, a `#`, and spaces and tabs at its start
and end. Without the file, a non-interactive run stops before the download (no
request reaches the server) for a database that is not deployed, and a rerun of
a deployed one needs no file. `check-uninstall-purge.py` ends with a
non-interactive `--purge --confirm-purge PURGE` that asks nothing.
