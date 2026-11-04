# UAT setup of a database of the demo-uiux line, after uat_admins.py.
#
# deploy-app.sh feeds this file to `odoo shell` (Odoo 19) on the demo-uiux line
# only. The environment variable PERODUA_UAT_DEMO names the job:
#
#   install  right after a fresh --init-db, and again when such a run stopped
#            before it finished. The demo seed gives its accounts (the
#            business roles, the portal logins of the gallery and of the
#            supply seed, the personas) the password "perodua", which a server
#            that the client network reaches must not keep. So every account
#            that has a password gets a new random one, except the UAT
#            administrators whadmin, admin1 and admin2 (uat_admins.py gives
#            them "perodua", as on the client-stable-uiux line), the native
#            Administrator (uat_admins.py archives it) and the technical
#            superuser. Then the portal mode is off, through its own switch:
#            portal accounts sign in through the hub only later. And the 17:00
#            run of Job Order Inbound is off (owner decision w4d).
#   upgrade  after a module upgrade (-u) to another demo-uiux release: each
#            account with the same exceptions whose password is still
#            "perodua" (such as a persona that the new release adds) gets a
#            new random one. Nothing else changes.
#
# PERODUA_UAT_DEMO_STEP names the step. Each job has two, so that the host has
# the new passwords before the database has them:
#
#   plan   changes nothing. It chooses the accounts of the job and a new random
#          password for each, and prints one line per account
#            SEEDED_ACCOUNT<TAB>login<TAB>password<TAB>kind of account
#          then "UAT demo: planned: N". deploy-app.sh writes these lines, and
#          only these, to seeded-accounts.pending in the deployment directory
#          (root only) and shows none of them.
#   apply  reads those lines on file descriptor 3, gives each account its
#          password, makes the other changes of the job, checks them and
#          commits. It prints no password. deploy-app.sh then adds the lines to
#          seeded-accounts.txt (root only) and removes seeded-accounts.pending.
#
# A run that stops after the commit of apply, before seeded-accounts.txt has
# the lines, leaves seeded-accounts.pending. The next upgrade job applies it
# again first (the same passwords) and lists it; an install job gives every
# account a new password again, so its plan replaces the file.
#
# The passwords are written through the ORM as the superuser, so they are
# permanent: no forced change at the first sign-in. SQL only reads which
# accounts have a password. Odoo's own password context checks each stored
# password; unlike a sign-in, that counts no failed attempt toward a lock.
#
# `env` is provided by `odoo shell`, which rolls back when the script ends, so
# the work is committed explicitly, and only after every check has passed.

import os
import secrets

MODES = ('install', 'upgrade')
MODE = os.environ.get('PERODUA_UAT_DEMO', '')
STEPS = ('plan', 'apply')
STEP = os.environ.get('PERODUA_UAT_DEMO_STEP', '')
PLAN_FD = 3
UAT_LOGINS = ('whadmin', 'admin1', 'admin2')
SEED_PASSWORD = 'perodua'
# Letters and digits that a person cannot confuse (no I, O, l, 0 or 1).
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
LENGTH = 20
PORTAL_MODE_MODEL = 'perodua.portal.mode'
PORTAL_MODE_PARAM = 'perodua_uiux_api.portal_mode'
PORTAL_MODE = 'off'
INBOUND_JOB = 'perodua_supplier_execution.cron_perodua_inbound_job_orders'


def fail(message):
    raise SystemExit(f'UAT demo: {message}')


def new_password(login):
    """A random password with a letter and a digit, without the login in it."""
    while True:
        password = ''.join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
            continue
        if login and login.lower() in password.lower():
            continue
        return password


def stored_passwords(ids):
    """{user id: stored password hash} of these accounts."""
    if not ids:
        return {}
    env.cr.execute("SELECT id, COALESCE(password, '') FROM res_users WHERE id IN %s", [tuple(ids)])  # noqa: F821
    return dict(env.cr.fetchall())  # noqa: F821


def matches(password, hashed):
    if not hashed:
        return False
    try:
        return bool(crypt.verify(password, hashed))
    except ValueError:
        return False


def kind(user):
    return ('portal' if user.share else 'internal') + ('' if user.active else ', archived')


def inbound_job():
    """The 17:00 Job Order Inbound run, when the image has both switches of the install job."""
    if PORTAL_MODE_MODEL not in env:  # noqa: F821
        fail(f'{PORTAL_MODE_MODEL} is missing: the image has no portal mode switch')
    job = env.ref(INBOUND_JOB, raise_if_not_found=False)  # noqa: F821
    if not job:
        fail(f'{INBOUND_JOB} is missing, so its 17:00 run cannot be kept off')
    return job


def read_plan():
    """{login: password} of the plan on file descriptor PLAN_FD. No message names a password."""
    try:
        with os.fdopen(PLAN_FD, encoding='utf-8') as source:
            text = source.read()
    except OSError as exc:
        fail(f'cannot read the plan of the new passwords on file descriptor {PLAN_FD}: {exc.strerror}')
    plan = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) != 4 or fields[0] != 'SEEDED_ACCOUNT' or not fields[1]:
            fail(f'line {number} of the plan is not a SEEDED_ACCOUNT line')
        login, password = fields[1], fields[2]
        if len(password) != LENGTH or any(char not in ALPHABET for char in password):
            fail(f'the plan gives {login} a password that this script did not make')
        if login in plan:
            fail(f'the plan names {login} twice')
        plan[login] = password
    return plan


def plan_step():
    """Prints the accounts of the job, each with a new random password. Writes nothing."""
    env.cr.execute("SELECT id FROM res_users WHERE COALESCE(password, '') <> '' ORDER BY id")  # noqa: F821
    accounts = Users.browse([row[0] for row in env.cr.fetchall() if row[0] not in kept.ids])  # noqa: F821
    if MODE == 'upgrade':
        stored = stored_passwords(accounts.ids)
        accounts = accounts.filtered(lambda user: matches(SEED_PASSWORD, stored[user.id]))
    for user in accounts.sorted('login'):
        print('\t'.join(('SEEDED_ACCOUNT', user.login, new_password(user.login), kind(user))))
    print(f'UAT demo: planned: {len(accounts)}')


def apply_step():
    """Gives each account of the plan its password, makes the rest of the job, checks it and commits."""
    plan = read_plan()
    accounts = Users.search([('login', 'in', list(plan))]) if plan else Users.browse()
    missing = sorted(set(plan) - set(accounts.mapped('login')))
    if missing:
        fail(f'the plan names accounts that this database does not have: {", ".join(missing)}')
    if accounts & kept:
        fail(f'the plan names {", ".join(sorted((accounts & kept).mapped("login")))}, which keep their passwords')
    passwords = {user.id: plan[user.login] for user in accounts}
    for user in accounts:
        user.write({'password': passwords[user.id]})
    if MODE == 'install':
        env[PORTAL_MODE_MODEL].set_mode(PORTAL_MODE)  # noqa: F821
        job.write({'active': False})
    env.flush_all()  # noqa: F821

    # ── checks: any failure rolls everything above back ─────────────────────
    stored = stored_passwords(list(passwords))
    forced = 'perodua_must_change_password' in Users._fields
    for user in accounts:
        if not matches(passwords[user.id], stored.get(user.id)):
            fail(f'{user.login} does not have its new password')
        if forced and user.perodua_must_change_password:
            fail(f'{user.login} must change its new password at the first sign-in')
    if MODE == 'install':
        stored = stored_passwords(admins.ids)
        for user in admins:
            if not matches(SEED_PASSWORD, stored.get(user.id)):
                fail(f'{user.login} does not have the UAT password that uat_admins.py gave it')
        env.cr.execute('SELECT value FROM ir_config_parameter WHERE key = %s', [PORTAL_MODE_PARAM])  # noqa: F821
        row = env.cr.fetchone()  # noqa: F821
        if not row or row[0] != PORTAL_MODE:
            fail(f'the portal mode is not {PORTAL_MODE}')
        if job.active:
            fail(f'{INBOUND_JOB} is still active')

    env.cr.commit()  # noqa: F821
    done = f'{len(accounts)} seeded accounts have a new random password'
    if MODE == 'install':
        done += f'; portal mode {PORTAL_MODE}; {INBOUND_JOB} off'
    print(f'UAT demo: ready: {done}')


if MODE not in MODES:
    fail(f'PERODUA_UAT_DEMO is {MODE!r}, not one of {", ".join(MODES)}')
if STEP not in STEPS:
    fail(f'PERODUA_UAT_DEMO_STEP is {STEP!r}, not one of {", ".join(STEPS)}')
Users = env['res.users'].with_context(active_test=False)  # noqa: F821 (odoo shell)
crypt = Users._crypt_context()
admins = Users.search([('login', 'in', UAT_LOGINS)])
if sorted(admins.mapped('login')) != sorted(UAT_LOGINS):
    fail(f'the UAT administrators {", ".join(UAT_LOGINS)} are not all there: uat_admins.py runs first')
kept = admins | env.ref('base.user_root') | env.ref('base.user_admin')  # noqa: F821
job = inbound_job() if MODE == 'install' else None
if STEP == 'plan':
    plan_step()
else:
    apply_step()
