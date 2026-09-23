# UAT administrator logins for a freshly initialized database.
#
# deploy-app.sh feeds this file to `odoo shell` (Odoo 19) right after a fresh
# --init-db initialization, and again only when such an initialization stopped
# before it finished. It never runs on an ordinary redeploy, so the fixed UAT
# passwords are never reset behind anyone's back.
#
#   whadmin / perodua     The release seeds these three logins itself
#   admin1  / perodua     (perodua_demo_ui: Sumathi, Haziq, Nurul). They are
#   admin2  / perodua     kept -- names, IDs and every seeded record that
#                         points at them -- and given exactly the groups and
#                         companies of Odoo's native Administrator.
#
# The native Administrator (base.user_admin) is archived, as Odoo recommends
# instead of deleting it, so no fourth administrator login remains; after
# seeding it is admin@demo.perodua.my with Odoo's default password. A login
# that a future image no longer seeds is created as a copy of that
# Administrator. The technical superuser (base.user_root) is left alone.
#
# The fixed password is intentional: this is a UAT system, never production.
# Everything goes through the ORM -- no SQL on res_users, no hand-made password
# hashes -- and a rerun converges the same three users instead of adding more.
#
# `env` is provided by `odoo shell`, which rolls back when the script ends, so
# the work is committed explicitly, and only after every check has passed.

from odoo.exceptions import AccessDenied
from odoo.fields import Command

PASSWORD = 'perodua'
LOGINS = (('whadmin', 'WH Admin'), ('admin1', 'Admin 1'), ('admin2', 'Admin 2'))


def fail(message):
    raise SystemExit(f'UAT admins: {message}')


Users = env['res.users'].with_context(active_test=False)  # noqa: F821 (odoo shell)
admin = env.ref('base.user_admin')  # noqa: F821
root = env.ref('base.user_root')  # noqa: F821
system = env.ref('base.group_system')  # noqa: F821
# Access Rights: may create users and change groups, so it counts as an
# administrator too when checking that no other one is left.
rights = env.ref('base.group_erp_manager')  # noqa: F821
if admin == root:
    fail('base.user_admin is the technical superuser; refusing to change it')

# The reference is the native Administrator as module installation left it:
# every module grants its administrator groups to base.user_admin. Captured
# before anything changes, and copied from the record, never listed by hand.
groups, effective = admin.group_ids, admin.all_group_ids
companies, company = admin.company_ids, admin.company_id
if system not in effective:
    fail('base.user_admin is not in base.group_system; nothing to take administrator rights from')

users = Users.browse()
for login, fallback_name in LOGINS:
    user = Users.search([('login', '=', login)])
    if len(user) > 1:
        fail(f'more than one user has login "{login}"')
    if user in (admin, root):
        fail(f'login "{login}" belongs to a built-in user')
    if not user:
        user = admin.copy({'login': login, 'name': fallback_name})
    user.write({
        'active': True,
        'company_ids': [Command.set(companies.ids)],
        'company_id': company.id,
        'group_ids': [Command.set(groups.ids)],
    })
    user.write({'password': PASSWORD})
    users |= user

admin.write({'active': False})
env.flush_all()  # noqa: F821

# ── checks: any failure rolls everything above back ─────────────────────────
if sorted(users.mapped('login')) != sorted(login for login, _ in LOGINS):
    fail(f'unexpected logins {sorted(users.mapped("login"))}')
if root in users or root.active:
    fail('the technical superuser was touched')
if admin.active:
    fail('base.user_admin is still active')
others = Users.search([('active', '=', True), ('share', '=', False), ('id', 'not in', users.ids)])
others = others.filtered(lambda u: system in u.all_group_ids or rights in u.all_group_ids)
if others:
    fail(f'other active administrator logins remain: {sorted(others.mapped("login"))}')
if Users.search_count([('login', '=', 'admin'), ('active', '=', True)]):
    fail('an active user with login "admin" remains')
for user in users:
    if not user.active or user.share:
        fail(f'{user.login} is not an active internal user')
    if user.group_ids != groups:
        missing = (groups - user.group_ids).mapped('full_name')
        extra = (user.group_ids - groups).mapped('full_name')
        fail(f'{user.login} groups differ from base.user_admin: missing {missing}, extra {extra}')
    if user.all_group_ids != effective:
        fail(f'{user.login} effective (implied) groups differ from base.user_admin')
    if system not in user.all_group_ids:
        fail(f'{user.login} is not in {system.full_name} (base.group_system)')
    if user.company_ids != companies or user.company_id != company:
        fail(f'{user.login} company access differs from base.user_admin')
    # Odoo's own password check, as its login does it; interactive=True means a
    # real password only, never an API key.
    try:
        user.with_user(user).sudo()._check_credentials(
            {'type': 'password', 'login': user.login, 'password': PASSWORD},
            {'interactive': True})
    except AccessDenied:
        fail(f'{user.login} does not authenticate with the UAT password')

env.cr.commit()  # noqa: F821
print('UAT admins: ready: ' + ', '.join(
    f'{u.login} (#{u.id} {u.name}, {len(u.group_ids)} groups, {len(u.all_group_ids)} effective)'
    for u in users.sorted('login')) + f'; base.user_admin ({admin.login}) archived')
