#!/usr/bin/env python3
"""Fail-closed checks for a fresh UAT database that must not get one module.

Standard library only. deploy-app.sh runs it inside the pinned Odoo image,
before anything touches the database, so it reads the real addon tree the
image will install from. The tests run it against temporary addon trees.

  guard      refuse unless the excluded module (perodua_demo_client) can be
             kept out of a fresh initialization of the requested modules:
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
REVIEWED_REFERENCES = (
    {
        'file': 'perodua_demo/hooks.pyc',
        'finding': "constant 'perodua_demo_client.loaded'",
        'sha256': '3ed0f998c31504b81e5e7f6fd9340b83318f33968e19fe6935eae47581f1e2d6',
        'reason': (
            'A system-parameter key, not an XML ID: it is read with a default that '
            'treats a missing value as "not loaded", only to word a log message, and '
            'the surrounding code handles the dataset being absent. Optional '
            'detection, not reliance. Reviewed against the pinned '
            'client-stable-uiux-v1.0.0 image '
            '(sha256:c16053940627c1c939a742327c2426b83355bf388a5fd2796894cb21770870da) '
            'and again for client-stable-uiux-v1.0.1 '
            '(sha256:8a55a0688ef42b9c67f088e74e0f815498e9371a554994b35729140925bb83d4), '
            'whose perodua_demo changes do not touch this key.'),
    },
)


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


def run_guard(args, reviewed=REVIEWED_REFERENCES) -> dict:
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
    excluded = args.exclude
    problems: list[str] = []
    report: dict = {'excluded': excluded, 'requested': requested,
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
    index = {(r['file'], r['finding'], r['sha256']): r['reason'] for r in reviewed}
    used = set()
    inside, outside, accepted = {}, {}, {}
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
            else:
                used.add((file, hit, digest))
                accepted.setdefault(name, []).append(
                    {'finding': f'{file}: {hit}', 'sha256': digest, 'reason': reason})
    for name, findings in inside.items():
        problems.append(f'{name} would be installed but relies on {excluded} '
                        f'without declaring it: {findings[0]}'
                        + (f' (+{len(findings) - 1} more)' if len(findings) > 1 else ''))
    report['references_in_install_set'] = inside
    report['reviewed_references'] = accepted
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
    guard.add_argument('--exclude', default='perodua_demo_client')
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
          f"none is {args.exclude} and none relies on it", file=sys.stderr)
    return ACCEPTED


if __name__ == '__main__':
    sys.exit(main())
