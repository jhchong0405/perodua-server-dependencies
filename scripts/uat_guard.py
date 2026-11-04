#!/usr/bin/env python3
"""Fail-closed checks for a fresh UAT database that must not get one module.

Standard library only. deploy-app.sh runs it inside the pinned Odoo image,
before anything touches the database, so it reads the real addon tree the
image will install from. The tests run it against temporary addon trees.

  guard      refuse unless the excluded module of the line (--line:
             perodua_demo_client on client-stable-uiux, the default, and
             perodua_client_stable on demo-uiux) can be kept out of a fresh
             initialization (or a module upgrade) of the requested modules:
             A. no module Odoo would install depends on it, directly or
                transitively, or pulls it in through auto_install;
             B. no module Odoo would install references it without declaring
                the dependency (XML IDs, imports, paths into its folder, the
                settings field that installs it, its quoted name), including
                string constants inside compiled .pyc files and spreadsheet
                data files;
             C. its own manifest carries no auto_install at all.
  demo-flag  print the command-line argument that stops Odoo loading demo
             data, for the Odoo version inside this image, after proving it
             with that Odoo's own option parser.

Exit status: 0 accepted, 3 refused, 2 usage error or something that could not
be inspected (callers must treat 2 as a refusal as well).
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.util
import inspect
import io
import json
import marshal
import pathlib
import re
import sys
import tokenize
import types
import zipfile
from collections.abc import Iterable

ACCEPTED, BROKEN, REFUSED = 0, 2, 3
# Data and code formats where any whole-word occurrence of the module counts.
# Every other text format may carry prose (comments, docstrings, descriptions)
# and is searched for the reference forms only; see Scanner.
DATA_SUFFIXES = ('.csv', '.json', '.yml', '.yaml', '.sql', '.js', '.ts')
# Spreadsheet/office data that Odoo modules load, stored as zip archives.
ZIP_SUFFIXES = ('.xlsx', '.xlsm', '.ods', '.docx', '.odt')
# Documentation, translations, images, fonts and source maps: never loaded as
# references to another module.
SKIPPED_SUFFIXES = ('.md', '.rst', '.txt', '.po', '.pot', '.png', '.jpg', '.jpeg', '.gif',
                    '.ico', '.webp', '.bmp', '.woff', '.woff2', '.ttf', '.otf', '.eot', '.map')
# Demo-data CLI argument per Odoo major version. Only versions whose option
# parser has been read are listed: 19.0's odoo/tools/config.py turns
# --without-demo=BOOL into with_demo = not BOOL, and warns on anything that is
# not a boolean ("since 19.0, invalid boolean value"), so the pre-19 spelling
# --without-demo=all is deliberately not used for it.
DEMO_FLAGS = {19: '--without-demo=True'}
# References that were read and found NOT to be reliance, each pinned to the
# exact bytes of the file it was reviewed in. A changed file voids its entry,
# so a different image can never inherit an old judgement: it is refused until
# someone reviews it again.
# The line blocks of deploy-app.sh, each with the module that a database of
# the line never gets (the default of --exclude).
LINE_EXCLUDED = {'client-stable-uiux': 'perodua_demo_client', 'demo-uiux': 'perodua_client_stable'}
DEFAULT_LINE = 'client-stable-uiux'
SHA256 = re.compile(r'[0-9a-f]{64}')


_DEMO_PIN = ('the pinned demo-uiux-grey-v0.56.1 image '
             '(sha256:74286eb8321a965dbcdd0541ae5961135645be6e38da1a0a7acdce3c2544ae73) of revision '
             'eefe5d72492b0cbfd81564b1dd6fd72faed394ee')
_DEMO_EARLIER = ('the images of demo-uiux-grey-v0.55.0 '
                 '(sha256:ef83c1f11dcad817ffefda3b9a6477a49d1b3a0354c83128a94af6813bfb9235) and v0.55.1 '
                 '(sha256:bbbc994a6091d775a41a42d18f5f5fed6ce57776c77d30607b09b33c513004df) of revision '
                 '634599e70b38471b6ecb9bf331177fc5be31e822')


def _review(file, sha256, constants, reason, earlier, change=None):
    """The review of one file of the pinned demo-uiux image: an entry for each
    of its findings, all pinned to the sha256 of the reviewed bytes. The
    reviewed file has the same code objects as its source at the pinned
    revision, compiled by the Python of the image. ``earlier`` is the sha256
    of the file that the first pin reviewed (v0.55.0, the same bytes in
    v0.55.1), and ``change`` what its source changed since (None: nothing)."""
    reason += (f' Reviewed against {_DEMO_PIN}, whose {file} is the compiled '
               f'custom_addons/{file[:-1]} of that revision. ')
    if change is None:
        reason += (f'The file of {_DEMO_EARLIER} (sha256 {earlier}) has the same code objects: only the source '
                   'time in the header of the .pyc differs.')
    else:
        reason += (f'The source changed since the file of {_DEMO_EARLIER} (sha256 {earlier}): {change} '
                   'The use of each reviewed constant is as before.')
    return tuple({'file': file, 'finding': f'constant {constant!r}', 'sha256': sha256, 'reason': reason}
                 for constant in constants)


_PARAMETER = ('a key of ir.config_parameter that keeps the name it has on the client-stable-uiux line '
              '(owner answer d05), not an XML ID, an import or a path')
_HMAC_SCOPE = ('the scope label of odoo.tools.misc.hmac, which signs repr((scope, message)) with '
               'database.secret: it reads no row, and it is not an XML ID, an import or a path')
_MENU_SNAPSHOT = (
    'CLIENT_STATE_KEY, ' + _PARAMETER + ': the snapshot of the menu states that perodua_client_stable '
    'writes when it applies its menu arrangement. migrate() reads it only when Odoo gives an installed '
    'version, and a missing or empty key gives no snapshot: the script returns without a write. Odoo 19 '
    'runs a migration script only for a module that it upgrades (odoo/modules/migration.py, migrate_module: '
    'load_state "to upgrade"), never in the -i of a new database, and only when the installed version is '
    'lower than the version of the script. The image has perodua_demo_profile 19.0.1.240.0. --init-db runs '
    'no migration script, and the module upgrade from v0.55.0 or v0.55.1 (perodua_demo_profile '
    '19.0.1.237.0, DEMO_MODULE_UPGRADE_FROM) runs only its scripts above 19.0.1.237.0, and the image has '
    'none (the last one is 19.0.1.196.0): so neither runs this script. A demo-uiux database has no such key. '
    'A migration of an earlier version, not reliance.')
# References that were read and found NOT to be reliance, per line, each pinned
# to the exact bytes of the file it was reviewed in. A changed file voids its
# entry, so a different image can never inherit an old judgement: it is
# refused until someone reviews it again.
REVIEWED_REFERENCES = {
    'client-stable-uiux': (
        {
            'file': 'perodua_demo/hooks.pyc',
            'finding': "constant 'perodua_demo_client.loaded'",
            'sha256': 'e33ac1c570ca2838e76157cce108b00acf80932b309a52256dd0825c4d18b46c',
            'reason': (
                'A system-parameter key, not an XML ID: one function '
                '(reconcile_fixture_identifiers) reads it with SQL, a missing row means '
                '"not loaded", and the value only words a log line and an error text; no '
                'branch tests it. The surrounding code detects the client_data column and '
                'handles the dataset being absent. post_init_hook calls that function only '
                'when perodua_demo.seeded is set, and the function returns before the read '
                'without it, so a fresh UAT initialization (no sample data: the hook only '
                'configures the company) never reads the key. The 19.0.1.12.0 migration of '
                'perodua_demo also calls it; the module upgrade from v1.2.1 (perodua_demo '
                '19.0.1.46.0) does not run that migration. Optional detection, not reliance. '
                'Reviewed against the pinned client-stable-uiux-v1.2.2 image '
                '(sha256:772500ec34d45c24cf9f66aa2bf26c3287d5c81ce8b8173f82ffed282d18bdfb), '
                'whose hooks.pyc is the compiled perodua_demo/hooks.py of revision '
                'ecd966496435b9999da2624ba8cdc349bce3f852. The function that reads the key '
                'and post_init_hook are the same as in client-stable-uiux-v1.2.1 (hooks.pyc '
                'sha256 853d3bd13a94f38d8b6f1c6390af24f04cdf74b18bdd71a6cf792681f0453777); '
                'the other changes of the file (such as the Perodua logo of the demo company, '
                'which configure_company leaves out, and the warehouse addresses and part '
                'prices of the sample data) do not use the key. The code that uses this '
                'key is the same as in the file reviewed for client-stable-uiux-v1.0.0 to '
                'v1.0.8 (sha256 '
                'db90973ddb2b77efd08746d109a5ce22c6043ee3be20effe2143537af53f7269), except '
                'one error text.'),
        },
        {
            'file': 'perodua_demo/supply_seed.pyc',
            'finding': "constant 'perodua_demo_client'",
            'sha256': '0ee305b0244e20230b8c068b4c05ff393e657a8c2c8197616e127e7e3624650d',
            'reason': (
                'A module name as a search value, not an import, an XML ID or a path: '
                'mvp_demo_parts() searches ir.model.data for the product rows of that '
                'module; with no row it returns nothing, and the two steps that call it do '
                'nothing. Each step first asks gallery.mock_data_refusal, which refuses on '
                'a database without sample data, on a client-stable-uiux image and with '
                'perodua_client_stable installed. The steps run from seed_supply_demo and '
                'seed_mvp_part_demo only. Each caller of these returns when '
                'perodua_demo.seeded is not set (the post_init_hook and the 19.0.1.50.0 and '
                '19.0.1.71.0 migrations of perodua_demo_profile, the gallery pass of '
                'perodua_demo_ui, the 19.0.1.7.0 and 19.0.1.8.0 migrations of '
                'perodua_demo), except seed_all, which runs only with sample data. No other '
                'caller is new in v1.2.2, and the module upgrade from v1.2.1 '
                '(perodua_demo_profile 19.0.1.71.0, perodua_demo 19.0.1.46.0) runs none of '
                'these migrations. So neither a fresh UAT initialization nor the module '
                'upgrade from v1.2.1 reads the rows. Optional detection, not reliance. '
                'Reviewed against the pinned client-stable-uiux-v1.2.2 image '
                '(sha256:772500ec34d45c24cf9f66aa2bf26c3287d5c81ce8b8173f82ffed282d18bdfb), '
                'whose supply_seed.pyc is the compiled perodua_demo/supply_seed.py of '
                'revision ecd966496435b9999da2624ba8cdc349bce3f852. The source is the same '
                'as in client-stable-uiux-v1.2.1 (supply_seed.pyc sha256 '
                'a15b9f326ccadf0c7a3574b5feec59e8a9198d7e7524969ba9412047a70a4fa1): the two '
                'files differ only in the source time in the .pyc header.'),
        },
    ),
    # The 16 findings of the guard of the demo-uiux-grey-v0.56.1 image
    # (--line demo-uiux, DEMO_INIT_MODULES and DEMO_RESTORED_MODULES), in 11
    # files of perodua_demo, perodua_demo_profile and perodua_sso_hub: the 16
    # of v0.55.0 and v0.55.1, in other bytes. Each file was read at the pinned
    # revision; perodua_sso_hub keeps the names of perodua_client_stable for
    # its parameters and its signatures.
    'demo-uiux': (
        *_review(
            'perodua_demo/gallery.pyc', '6a53291c1916ece242132ed2c226dce9089e184022017a994b0401817862756a',
            ('perodua_client_stable',),
            'EXPORT_MOCK_BLOCKING_MODULE, a module name as a search value, not an import, an XML ID or a '
            'path: mock_data_refusal() counts the ir.module.module rows of that name in a state other than '
            'uninstalled or uninstallable, and only then refuses the mock data ("perodua_client_stable is '
            'installed"). The constant has no other use. On the demo-uiux line the module is never '
            'installed: this guard keeps it out of the install set, and preflight.py verify-fresh refuses a '
            'database that has it (LINE_RULES, never_installed). So the count is 0, and with '
            'perodua_demo.seed_mode not "none" and PERODUA_IMAGE_PROFILE=demo-uiux (NO_SAMPLE_PROFILES of '
            'perodua_master_sync holds only client-stable-uiux) the function returns False: the demo seed '
            'loads its mock data, as the line needs. The module is absent in the normal case. Optional '
            'detection, not reliance.',
            'a46f478959bc7b30948ead7010b8f2cf5fa27901e9d905d4f4e504d375b8b535'),
        *(review
          for version, sha256, earlier, change in (
              ('19.0.1.100.0', '9181ec9e17a4843546e813a0323e0efe71daab8067c1d1b8ca1e299a6edd2993',
               'a9f21831d2c25141574a3eb51b5dcb3e31bf2cd78d86b44dbe3e5c1645f740a1',
               'Outcome 10.22 Setup > Material (#1721): Part Material, Storage Zone and Car Model'),
              ('19.0.1.102.0', '0700bf7a08518779dfc375ae171857ad4fac1fea1fa724fe933766a3f9720ac2',
               '200abc91b84d2bcfbd6ab3980d9a840325736edc817e3a57ee4868dd166185fa',
               'Outcome 10.22 Setup > Material (#1721): Box Size'),
              ('19.0.1.103.0', '53168e099c94107a307056610a76f1b66c00233a183b7f28e2f02d318b09b11f',
               '30b6ae9e2872c13e432ef362debfef0898ba7eddc93e019c0ab58379f405e728',
               'Test Outcome 10.22 R28 (#1738): Public Holiday'),
              ('19.0.1.128.0', '5e25dec3dc2bd40c8986dfae089b0fdcd9a93142426cbb3d4403e1aa7d7e1ce5',
               'b270c1bb58c9692b58af07ed62cb3d9cecd108ef302a0afddcda7817c37c12af',
               'Test Outcome 10.26 Menu Arrangement tab (#1887) and Sales Order (#1906): five menus'),
              ('19.0.1.196.0', '361826f5327d126fafd38f4cfafbfbdb026c52f80172e2d73e3c2086191f22f0',
               '9925054de5bd788841d7913692c6d605d4541ea7603c5d77f58429be8481700a',
               'Test Outcome 10.27 R41 (#1991): Direct Delivery Job'))
          for review in _review(
              f'perodua_demo_profile/migrations/{version}/post-migrate.pyc', sha256,
              ('perodua_client_stable.menu_arrangement.v1',),
              f'The post-migrate script of version {version} ({change}). ' + _MENU_SNAPSHOT, earlier)),
        *_review(
            'perodua_sso_hub/controllers/hosts.pyc', 'c4fd93355f2dac808105f5802f0427c043e1dc55cd96382b998afefb79e21985',
            ('perodua_client_stable.hosts',),
            'HOSTS_PARAM, ' + _PARAMETER + ': host_table() reads the host table (JSON) with get_param, and '
            'without the key, or with a value that is not valid, it uses DEFAULT_HOSTS of the same file '
            '(stgiss the sign-in host; stgissrp, stgisssp and stgisscp the module hosts). The workspace codes '
            'are WORKSPACE_CODES of the same file, the seven codes of LINE_RULES of preflight.py, which reads '
            'the same key. A new demo-uiux database has no such key and uses the default table. Not '
            'reliance.',
            '252a27a17b4e3c3793f3bf0ed8cea7d3fc03f6c667d11e926ba36e6fd4fd39c8',
            'app PR #2242 (W2 of the /my removal) adds CODE_ROLES (the external roles that the codes cp and sp '
            'admit) and external_roles(), which reads the same table through host_table(). WORKSPACE_CODES '
            'keeps its seven codes.'),
        *_review(
            'perodua_sso_hub/controllers/handover.pyc', 'beddf5ba2ec071bba5b30fa758d68cb91514ca7b294136d26180bbb865bc23df',
            ('perodua_client_stable.handover.pending.v1', 'perodua_client_stable.handover.spent.',
             'perodua_client_stable.handover.v1', 'perodua_client_stable.handover.verifier.v1',
             'perodua_client_stable.module_signin'),
            'Five names. SIGNIN_PARAM (perodua_client_stable.module_signin) is ' + _PARAMETER + ': '
            'module_signin() reads it with SQL at each request, and without the row the module sign-in uses '
            'the oidc wire (with a usable signing key; without one it is unavailable and fails closed). '
            'SPENT_PREFIX (perodua_client_stable.handover.spent.) starts the keys of the ir_config_parameter '
            'rows that this file itself writes and deletes with SQL (_spend, _spent) for a refused or used '
            'ticket or id_token: a new database has none and needs none. TICKET_SCOPE, VERIFIER_SCOPE and '
            'PENDING_SCOPE (perodua_client_stable.handover.v1, .handover.verifier.v1 and '
            '.handover.pending.v1) are each ' + _HMAC_SCOPE + '. They keep the signatures of the ticket and '
            'of the two hand-over cookies apart. No row, field or code of perodua_client_stable is used. Not '
            'reliance.',
            '1d1cb61c53ac8a70b758eb405d9053a8fd21b33f0906c92bb0c2aa2883e117bc',
            'app PR #2242 (W2 of the /my removal) lets _eligible() admit a share account to a module host only '
            'while the parameter perodua_sso_hub.external_signin is on (a name of perodua_sso_hub, off without '
            'the row), while external_door of perodua_uiux_api serves the account, and only for a role that '
            'hosts.external_roles() gives that host.'),
        *_review(
            'perodua_sso_hub/controllers/oidc_clients.pyc',
            'b64beb5336f4a5622d1e44ce7f616a4f7c7f8f598b525273bc7c04ce48d3b1f9',
            ('perodua_client_stable.oidc.sid.v2', 'perodua_client_stable.oidc_clients'),
            'Two names. CLIENTS_PARAM (perodua_client_stable.oidc_clients) is ' + _PARAMETER + ': '
            'external_clients() reads it with get_param, and without the key it uses default_clients() of '
            'the same file (wom and tms); the clients of the module hosts are built in code from the host '
            'table. SID_SCOPE (perodua_client_stable.oidc.sid.v2) is ' + _HMAC_SCOPE + ', in family_sid(). '
            'The field that family_sid() reads, res.users.perodua_oidc_sid_generations, is declared by '
            'perodua_sso_hub itself (models/res_users.py). Not reliance.',
            '221ec7ce1c5cd0dd51d298e7c919a5338c91a128a7624a994c0adb4312f6d1ee'),
        *_review(
            'perodua_sso_hub/controllers/binding.pyc', 'b93d541abcdaeaea6ded012f05586b1129338cfd7d1f919e1b46dbb1e17334f0',
            ('perodua_client_stable.bind.v1',),
            'SCOPE, ' + _HMAC_SCOPE + ', for the value of the __Host-perodua_bind_<env> cookie (an HMAC '
            'of the random perodua_bind value of the session). It is not a key of ir.config_parameter. Not '
            'reliance.',
            '838cb6dcad8f0d712deaf6f117da35bc80ecc3939ac0c9947572a3a84ce67cd0'),
        *_review(
            'perodua_sso_hub/hooks.pyc', '7d30f39d4d43122bcbd3af8c26748297dca46df4e0408348861c12493e186af4',
            ('perodua_client_stable',),
            'FORMER_OWNER, a module name as a value in the SQL of pre_init_hook, not an import, an XML ID or '
            'a path: the hook moves to perodua_sso_hub the ir_model_data rows of that module, and the '
            'ir_model_constraint rows of its ir_module_module row, for the hub models perodua.oidc.pending '
            'and perodua.oidc.signin and the field res.users.perodua_oidc_sid_generations (option A, an '
            'upgrade of a client-stable-uiux database). perodua_sso_hub declares these models and the field '
            '(models/oidc.py, models/res_users.py). On a database where perodua_client_stable was never '
            'installed, as each new demo-uiux database, the queries find no row: nothing moves, no row is '
            'left, and the hook logs zero counts (former_constraint_ids gives no row also when the module has '
            'no ir_module_module row). The module upgrade from v0.55.0 or v0.55.1 does not run the hook: Odoo '
            'runs pre_init_hook only when it installs the module. It moves rows; it does not install or need the '
            'module. Not reliance.',
            'c4e762e454e4ad18767d80e911a6182a339c2374d1e8d1d560c10eae741c07af'),
    ),
}


class Refused(Exception):
    """Fresh initialization must not proceed."""


class Broken(Exception):
    """Inputs could not be inspected; never read as permission to proceed."""


# ── Manifests, normalized the way Odoo 19 does (odoo/modules/module.py) ─────

def read_manifest(path: pathlib.Path) -> dict:
    try:
        data = ast.literal_eval(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, SyntaxError, UnicodeDecodeError) as exc:
        raise Broken(f'{path}: manifest is not a readable Python literal ({exc})') from None
    if not isinstance(data, dict):
        raise Broken(f'{path}: manifest is not a dict')
    return data


def normalize(name: str, raw: dict) -> dict:
    depends = raw.get('depends', [])
    if isinstance(depends, str) or not isinstance(depends, Iterable) \
            or not all(isinstance(dep, str) for dep in depends):
        raise Broken(f'{name}: "depends" must be a list of module names')
    # Odoo: base depends on nothing, and an empty list means ['base'].
    depends = [] if name == 'base' else (list(depends) or ['base'])
    auto = raw.get('auto_install', False)
    if isinstance(auto, Iterable):
        # Any iterable -- including [] and even a string -- becomes a set of
        # triggers. An EMPTY set is Odoo's "always install" special case.
        auto = set(auto)
        stray = auto.difference(depends)
        if stray:
            raise Broken(f'{name}: auto_install triggers are not dependencies: {sorted(stray)}')
    elif auto:
        auto = set(depends)
    else:
        auto = False
    return {'depends': depends, 'auto_install': auto,
            'installable': bool(raw.get('installable', True))}


def describe_auto_install(info: dict) -> str:
    auto = info['auto_install']
    if auto is False:
        return 'auto_install is off'
    if not auto:
        return 'auto_install is an empty list: Odoo installs it unconditionally in every new database'
    return f'auto_install fires once all of {sorted(auto)} are installed'


def discover(addons_paths: list[pathlib.Path]) -> dict[str, dict]:
    """Every module on the path; the first occurrence wins, as in Odoo."""
    modules: dict[str, dict] = {}
    for root in addons_paths:
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            raise Broken(f'{root}: cannot list addons ({exc})') from None
        for entry in entries:
            manifest = entry / '__manifest__.py'
            if entry.name in modules or not manifest.is_file():
                continue
            info = normalize(entry.name, read_manifest(manifest))
            info['path'] = entry
            modules[entry.name] = info
    return modules


# ── A. What a fresh initialization would install ────────────────────────────

def install_closure(modules: dict[str, dict], requested: list[str]) -> dict[str, str]:
    """A superset of what Odoo marks 'to install' for `-i requested` on a new
    database.

    Mirrors odoo/modules/db.py initialize() and ir.module.module's
    button_install(): base, the requested modules and all their dependencies,
    plus -- to a fixpoint -- every installable auto_install module whose
    triggers are all in the set (an empty trigger set is always satisfied)
    and whose dependencies all exist. On a new database nothing is installed
    beforehand, so both code paths reduce to this with one exception: Odoo
    auto-installs a module restricted to `countries` only when a company is in
    one of them. The guard assumes it might, so the set can only be larger
    than Odoo's, never smaller. (On the pinned image, dropping exactly those
    modules gives the same 162 modules Odoo installs.) Returns {module: why}.
    """
    why: dict[str, str] = {}

    def add(name: str, reason: str) -> None:
        pending = [(name, reason)]
        while pending:
            current, because = pending.pop()
            if current in why:
                continue
            info = modules.get(current)
            if info is None:
                raise Broken(f'{current} ({because}) is not in any addons path')
            if not info['installable']:
                raise Broken(f'{current} ({because}) is not installable')
            why[current] = because
            pending.extend((dep, f'dependency of {current}') for dep in info['depends'])

    add('base', 'always installed')
    for name in requested:
        add(name, 'requested for initialization')
    while True:
        triggered = sorted(
            name for name, info in modules.items()
            if name not in why and info['installable'] and info['auto_install'] is not False
            and all(dep in modules for dep in info['depends'])
            and info['auto_install'] <= why.keys())
        if not triggered:
            return why
        for name in triggered:
            add(name, describe_auto_install(modules[name]))


def chain(why: dict[str, str], name: str) -> str:
    """'x <- dependency of y <- requested for initialization'."""
    links, seen = [name], {name}
    while True:
        reason = why[links[-1]]
        match = re.fullmatch(r'dependency of (\S+)', reason)
        if not match or match.group(1) in seen:
            return ' <- '.join(links + [reason])
        links.append(match.group(1))
        seen.add(match.group(1))


# ── B. References to the excluded module ─────────────────────────────────────

class Scanner:
    """Finds places that rely on the excluded module, not places that name it.

    Reliance has recognisable forms: an XML ID or dotted name
    (`perodua_demo_client.x`), a Python import (`odoo.addons.perodua_demo_client`),
    a path into the module's folder (`perodua_demo_client/data/...`), the
    settings field through which Odoo installs a module
    (`module_perodua_demo_client`), or the module name as a quoted string in
    code, SQL or a domain. Prose -- comments, docstrings, descriptions, log
    text -- names the module without any of these forms, and the real image
    has plenty of it, so prose does not count.

    Data formats without prose (.csv/.json/.yml/.yaml/.sql/.js/.ts, and the
    cells of spreadsheet files) count any whole-word occurrence. In compiled
    .pyc files every string constant and name is searched for the forms that
    never occur in prose, and a constant that starts with the module name
    counts as well (`env.ref('perodua_demo_client.x')`, f-strings, the bare
    name). Files that cannot be read make the whole check fail.
    """

    def __init__(self, excluded: str):
        m = re.escape(excluded)
        embedded = (rf'\bodoo\.addons\.{m}\b'   # Python import path
                    rf'|(["\']){m}(?:\1|\.)'    # quoted name, or quoted XML ID
                    rf'|(?<![\w.-]){m}/'        # a path into the module folder
                    rf'|\bmodule_{m}\b')        # settings field that installs it
        self.embedded = re.compile(embedded)
        self.reference = re.compile(embedded + rf'|\b{m}\.[A-Za-z_]'
                                    rf'|\bfrom\s+odoo\.addons\s+import\s[^\n]*\b{m}\b')
        self.word = re.compile(rf'(?<![A-Za-z0-9]){m}\b')
        self.prefix = re.compile(rf'(?:odoo\.addons\.)?{m}(?:$|[.%{{/])')
        self.name = re.compile(rf'(?:odoo\.addons\.|module_)?{m}(?:\..*)?')
        self.magic = importlib.util.MAGIC_NUMBER

    @staticmethod
    def _strip_python_comments(text: str) -> str:
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            return tokenize.untokenize(t for t in tokens if t.type != tokenize.COMMENT)
        except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
            return text  # unparseable: scan everything rather than less

    def text_findings(self, text: str, suffix: str) -> list[str]:
        if suffix == '.xml':
            text = re.sub(r'<!--.*?-->', lambda c: '\n' * c.group(0).count('\n'), text, flags=re.S)
        elif suffix == '.py':
            text = self._strip_python_comments(text)
        pattern = self.word if suffix in DATA_SUFFIXES else self.reference
        return [f'line {number}: {line.strip()[:160]}'
                for number, line in enumerate(text.splitlines(), 1) if pattern.search(line)]

    def pyc_findings(self, data: bytes, where: str) -> list[str]:
        if data[:4] != self.magic:
            raise Broken(f'{where}: compiled for a different Python; cannot inspect its constants')
        try:
            code = marshal.loads(data[16:])
        except (EOFError, ValueError, TypeError) as exc:
            raise Broken(f'{where}: unreadable compiled module ({exc})') from None
        found: set[str] = set()
        stack = [code]
        while stack:
            current = stack.pop()
            for name in current.co_names:
                if self.name.fullmatch(name):
                    found.add(f'name {name}')
            values = list(current.co_consts)
            while values:
                value = values.pop()
                if isinstance(value, types.CodeType):
                    stack.append(value)
                elif isinstance(value, (tuple, frozenset)):
                    values.extend(value)
                elif isinstance(value, (str, bytes)):
                    text = value.decode('latin-1') if isinstance(value, bytes) else value
                    if self.prefix.match(text) or self.embedded.search(text):
                        found.add(f'constant {text[:160]!r}')
        return sorted(found)

    def zip_findings(self, data: bytes, where: str) -> list[str]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return [f'{member}: {hit}' for member in archive.namelist()
                        for hit in self.text_findings(
                            archive.read(member).decode('utf-8', 'replace'), '.csv')]
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
            raise Broken(f'{where}: unreadable archive ({exc})') from None

    def module_findings(self, module_dir: pathlib.Path) -> list[tuple[str, str, str]]:
        """[(file relative to the addons dir, finding, sha256 of the file)]."""
        findings = []
        try:
            paths = sorted(module_dir.rglob('*'))
        except OSError as exc:
            raise Broken(f'{module_dir}: cannot list files ({exc})') from None
        for path in paths:
            if not path.is_file():
                continue
            rel = path.relative_to(module_dir.parent).as_posix()
            suffix = path.suffix.lower()
            if suffix in SKIPPED_SUFFIXES:
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise Broken(f'{rel}: cannot be read ({exc})') from None
            if suffix == '.pyc':
                hits = self.pyc_findings(data, rel)
            elif suffix == '.gz':
                inner = path.suffixes[-2].lower() if len(path.suffixes) >= 2 else ''
                try:
                    text = gzip.decompress(data).decode('utf-8', 'replace')
                except (OSError, EOFError) as exc:
                    raise Broken(f'{rel}: unreadable compressed file ({exc})') from None
                hits = self.text_findings(text, inner)
            elif suffix in ZIP_SUFFIXES:
                hits = self.zip_findings(data, rel)
            else:
                hits = self.text_findings(data.decode('utf-8', 'replace'), suffix)
            if hits:
                digest = hashlib.sha256(data).hexdigest()
                findings.extend((rel, hit, digest) for hit in hits)
        return findings


# ── Commands ─────────────────────────────────────────────────────────────────

def odoo_addons_paths(config_file: str) -> list[pathlib.Path]:
    """The addons path this image's own Odoo would use, in its own order.

    It includes the data directory's addons folder, which sits on the
    persistent filestore volume ahead of the baked modules and could shadow
    one, so the guard must see exactly what Odoo sees.
    """
    try:
        from odoo.tools import config  # noqa: PLC0415
        kwargs = {}
        if 'setup_logging' in inspect.signature(config.parse_config).parameters:
            kwargs['setup_logging'] = False
        config.parse_config(['-c', config_file], **kwargs)
        import odoo.addons  # noqa: PLC0415
        return [pathlib.Path(p) for p in odoo.addons.__path__ if pathlib.Path(p).is_dir()]
    except Exception as exc:  # any failure here is a reason not to proceed
        raise Broken(f'cannot read the effective addons path from Odoo: {exc}') from None


def run_guard(args, reviewed=None) -> dict:
    """The guard report. ``reviewed``: the reviewed references to apply, by
    default those of the line of ``args`` (``--line``)."""
    line = getattr(args, 'line', None) or DEFAULT_LINE
    if line not in REVIEWED_REFERENCES:
        raise Broken(f'{line} is not a line of this guard (known: {", ".join(sorted(REVIEWED_REFERENCES))})')
    if reviewed is None:
        reviewed = REVIEWED_REFERENCES[line]
    requested = [m.strip() for m in args.modules.split(',') if m.strip()]
    if not requested:
        raise Broken('no modules requested')
    if args.addons_path:
        paths = [pathlib.Path(p) for p in args.addons_path.split(',') if p]
        missing = [str(p) for p in paths if not p.is_dir()]
        if missing:
            raise Broken(f'addons path is not a directory: {", ".join(missing)}')
    else:
        paths = odoo_addons_paths(args.odoo_config)
    modules = discover(paths)
    excluded = getattr(args, 'exclude', None) or LINE_EXCLUDED[line]
    problems: list[str] = []
    report: dict = {'line': line, 'excluded': excluded, 'requested': requested,
                    'addons_paths': [str(p) for p in paths]}

    if excluded in requested:
        problems.append(f'the requested modules name {excluded} itself')
    closure = install_closure(modules, requested)
    report['install_set'] = sorted(closure)

    # A. dependency / auto_install closure
    if excluded in closure:
        problems.append(f'Odoo would install {excluded}: {chain(closure, excluded)}')
    dependents = sorted(n for n, i in modules.items() if excluded in i['depends'])
    report['declared_dependents'] = {n: n in closure for n in dependents}

    # C. the excluded module's own auto_install
    info = modules.get(excluded)
    report['excluded_present'] = info is not None
    if info is not None:
        report['excluded_auto_install'] = describe_auto_install(info)
        if info['auto_install'] is not False:
            problems.append(f'{excluded} could be installed automatically: {describe_auto_install(info)}')

    # B. undeclared reliance inside everything that would be installed
    scanner = Scanner(excluded)
    # Only a review with the sha256 of the reviewed file counts; a placeholder
    # (no sha256 yet) only says which finding waits for its review.
    index = {(r['file'], r['finding'], r['sha256']): r['reason'] for r in reviewed
             if isinstance(r.get('sha256'), str) and SHA256.fullmatch(r['sha256'])}
    placeholders = {(r['file'], r['finding']) for r in reviewed
                    if not (isinstance(r.get('sha256'), str) and SHA256.fullmatch(r['sha256']))}
    used = set()
    inside, outside, accepted, waiting = {}, {}, {}, []
    for name, module in sorted(modules.items()):
        if name == excluded:
            continue
        for file, hit, digest in scanner.module_findings(module['path']):
            if name not in closure:
                outside.setdefault(name, []).append(f'{file}: {hit}')
                continue
            reason = index.get((file, hit, digest))
            if reason is None:
                inside.setdefault(name, []).append(f'{file}: {hit}')
                if (file, hit) in placeholders:
                    waiting.append(f'{file}: {hit}')
            else:
                used.add((file, hit, digest))
                accepted.setdefault(name, []).append(
                    {'finding': f'{file}: {hit}', 'sha256': digest, 'reason': reason})
    for name, findings in inside.items():
        problems.append(f'{name} would be installed but relies on {excluded} '
                        f'without declaring it: {findings[0]}'
                        + (f' (+{len(findings) - 1} more)' if len(findings) > 1 else ''))
    if waiting:
        problems.append(f'{len(waiting)} of these findings have only a placeholder review of the {line} line, '
                        'which the first pin of its release replaces with a review of the pinned bytes: '
                        + ', '.join(waiting[:3]) + (f' (+{len(waiting) - 3} more)' if len(waiting) > 3 else ''))
    report['references_in_install_set'] = inside
    report['reviewed_references'] = accepted
    report['placeholder_reviews'] = waiting
    # Entries that no longer match anything: harmless, but worth pruning.
    report['unused_reviews'] = [f'{f}: {h}' for f, h, d in index if (f, h, d) not in used]
    # Not installed, so not a risk to this database. Counted for the record but
    # not quoted: the report is written in plain text on the App server, and
    # nothing from inside the image needs to be copied there.
    report['references_outside_install_set'] = {name: len(hits) for name, hits in outside.items()}
    report['problems'] = problems
    report['verdict'] = 'refused' if problems else 'accepted'
    return report


def run_demo_flag(args) -> dict:
    try:
        from odoo import release  # noqa: PLC0415
        from odoo.tools import config  # noqa: PLC0415
    except Exception as exc:
        raise Broken(f'Odoo is not importable here: {exc}') from None
    major = release.version_info[0]
    flag = DEMO_FLAGS.get(major)
    if flag is None:
        raise Broken(f'Odoo {release.version} is not a version whose demo-data option has been '
                     f'verified (known: {sorted(DEMO_FLAGS)}); refusing to guess')
    kwargs = {}
    if 'setup_logging' in inspect.signature(config.parse_config).parameters:
        kwargs['setup_logging'] = False
    config.parse_config(['-c', args.odoo_config, flag], **kwargs)
    if config['with_demo'] is not False:
        raise Broken(f'{flag} did not disable demo data in Odoo {release.version} '
                     f'(with_demo={config["with_demo"]!r})')
    return {'odoo_version': release.version, 'major': major, 'flag': flag, 'with_demo': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    guard = sub.add_parser('guard')
    guard.add_argument('--modules', required=True, help='comma-separated modules to initialize')
    guard.add_argument('--line', choices=sorted(REVIEWED_REFERENCES), default=DEFAULT_LINE,
                       help='the line block of deploy-app.sh: its reviewed references, and its excluded module')
    guard.add_argument('--exclude', default=None,
                       help='the module to keep out (default: the one of the line, '
                            + ', '.join(f'{k}: {v}' for k, v in sorted(LINE_EXCLUDED.items())) + ')')
    where = guard.add_mutually_exclusive_group(required=True)
    where.add_argument('--odoo-config', help="derive the addons path from this image's Odoo")
    where.add_argument('--addons-path', help='explicit comma-separated addons directories')
    guard.add_argument('--json', action='store_true',
                       help='print the full JSON report on stdout (verdict lines stay on stderr)')
    flag = sub.add_parser('demo-flag')
    flag.add_argument('--odoo-config', required=True)
    args = parser.parse_args(argv)
    try:
        report = run_guard(args) if args.command == 'guard' else run_demo_flag(args)
    except Broken as exc:
        print(f'UAT guard: cannot verify: {exc}', file=sys.stderr)
        return BROKEN
    except Exception as exc:  # anything unforeseen is a reason not to proceed
        print(f'UAT guard: cannot verify: unexpected {type(exc).__name__}: {exc}', file=sys.stderr)
        return BROKEN
    if args.command == 'demo-flag':
        print(report['flag'])
        print(f"Odoo {report['odoo_version']}: {report['flag']} verified to disable demo data",
              file=sys.stderr)
        return ACCEPTED
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    for entries in report['reviewed_references'].values():
        for entry in entries:
            print(f"UAT guard: reviewed, not reliance: {entry['finding']} "
                  f"(file sha256 {entry['sha256'][:16]})", file=sys.stderr)
    for problem in report['problems']:
        print(f'UAT guard: REFUSED: {problem}', file=sys.stderr)
    if report['problems']:
        return REFUSED
    print(f"UAT guard: accepted: of at most {len(report['install_set'])} modules Odoo could install, "
          f"none is {report['excluded']} and none relies on it", file=sys.stderr)
    return ACCEPTED


if __name__ == '__main__':
    sys.exit(main())
