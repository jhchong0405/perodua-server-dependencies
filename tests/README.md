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
- the backup folder (`backups/TIME-OLD_RELEASE`, 0700, or under `--backup-dir`)
  holds the dump, the archive, the old identity and `app.env`, and
  `restore.txt`, which is printed as well and names each restore command: step
  4 also deletes the Odoo sessions and says that every user signs in again;
  step 5 copies the old identity and `app.env` back and deploys without
  `--upgrade`;
- a failure after the switch (recording the addresses, the HTTP check) says
  that the directory names the new release, whether the App is stopped or the
  containers were recreated, and how to finish, go back and restore; the
  printed copies, followed by a plain run of a `deploy-app.sh` that pins the old
  release, bring the directory back (as a v1.0.3 folder, which has no
  `--upgrade` and refuses the directory before the copies);
- `--upgrade` from a folder of another release with the same modules moves the
  directory there again, with a second backup.

`test_deploy_app_module_upgrade.py` runs `deploy-app.sh --upgrade` with a module
upgrade (`-u`) against the same fake Docker, in a directory of v1.0.6. The fake
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
  migration log handler, no `-i`, the new compose file, and its log in the
  backup folder; no argument holds the password;
- the cron and mail lines of `verify-upgrade` come before `up`, and the final
  message names the held mail and `--release-queued-mail`; `restore.txt` names
  the module upgrade and the point of no return;
- each accepted old release (v1.0.5 to v1.0.7, v1.0.8 being this one) goes
  through; v1.0.4, another fingerprint, every refusal of `upgrade-check`, retired
  data without `--drop-retired-data`, a refusal of the guard and a missing or
  wrong confirmation stop before the App stops, and nothing changes;
  `--confirm`, `--drop-retired-data` and `--release-queued-mail` are refused
  where they do not apply;
- after the backup, `upgrade-check` runs again with the App stopped. Retired
  data that it counts when the first check counted none needs
  `--drop-retired-data` (without it: refused, and the old App starts again
  with the backup kept; with it: `retired-data.tar.gz` is made). A refusal or
  other modules to upgrade at this second check also start the old App again;
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
  directory runs;
- as root on Linux: `service.sh` (byte for byte that of v1.0.5 to v1.0.8, pinned
  by its sha256) refuses `start` and `restart` on a marked database, before and
  after the switch, without `up` and without its advice to reset.

`test_preflight_module_upgrade.py` runs the exact `preflight.py` that
`deploy-app.sh` writes, and the one of v1.0.5 to v1.0.8
(`fixtures/preflight-v1.0.5-v1.0.8.py`, identical in kit 4198af8 to 8867545),
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
their system resolves to mock, a live one restored), the mail hold of the rows
`-u` created and their release after `stamp-upgrade` only, the refusals of
`verify-upgrade` (which still writes the cron flags and the hold), and the CSV
files of `retired-export`.

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

`test_https.py` runs `https.sh` with real openssl against fake `nginx`, `systemctl`,
`ss` and `curl`. A throwaway root and intermediate sign the requests the script
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
`uninstall` must fail and change nothing. Then it uninstalls, and `apply` then
refuses another nginx file that is the default server for port 443. It refuses to run outside a container.

`check-upgrade-backup.sh` runs the backup of `deploy-app.sh --upgrade` and the
restore steps of its `restore.txt` against real PostgreSQL 16 (a `postgres:16`
container) and the PostgreSQL 18 client tools of an Odoo image that is already
on the host, such as the pinned release image:

```bash
bash tests/check-upgrade-backup.sh IMAGE
```

It takes the command lines from `deploy-app.sh`, and checks that the dump and
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
keeps its previous check, which requires the dataset module. `public-urls`
refuses an empty database, writes only `report.url` without a base URL, commits
`web.base.url` and `web.base.url.freeze` with one (a rerun changes nothing),
refuses an invalid base URL without changing anything, and leaves the database
`READY`.

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
