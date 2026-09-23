"""uat_guard.py against temporary addon trees. Standard library only.

The guard runs inside the pinned Odoo image before a fresh UAT database is
initialized; here it runs against fake trees shaped like that image's:
manifests as literals and module code shipped as bare .pyc files.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
GUARD = SCRIPTS / 'uat_guard.py'
SPEC = importlib.util.spec_from_file_location('uat_guard', GUARD)
uat_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uat_guard)

EXCLUDED = 'perodua_demo_client'
REQUESTED = 'perodua_client_stable,perodua_uiux_api'


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='perodua-guard-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core = self.root / 'core'
        self.addons = self.root / 'perodua'
        self.core.mkdir()
        self.addons.mkdir()
        self.module('base', depends=[], root=self.core)
        self.module('web', depends=['base'], root=self.core)
        # A safe, image-shaped graph: the excluded module exists, is opt-in,
        # and only its own declared bridge depends on it.
        self.module('perodua_master_data', depends=['base'])
        self.module('perodua_client_stable', depends=['perodua_master_data', 'web'])
        self.module('perodua_uiux_api', depends=['perodua_client_stable'])
        self.module(EXCLUDED, depends=['perodua_master_data'])
        self.module('perodua_demo_client_ui', depends=[EXCLUDED, 'perodua_uiux_api'],
                    auto_install=True)
        self.pyc('perodua_demo_client_ui', 'models/bridge',
                 "from odoo.addons.perodua_demo_client import hooks\n")

    # ── fixtures ────────────────────────────────────────────────────────────
    def module(self, name, depends, root=None, **manifest):
        path = (root or self.addons) / name
        path.mkdir(parents=True, exist_ok=True)
        manifest = {'name': name, 'depends': depends, **manifest}
        (path / '__manifest__.py').write_text(repr(manifest) + '\n', encoding='utf-8')
        return path

    def write(self, module, relative, text, root=None):
        path = (root or self.addons) / module / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding='utf-8')
        return path

    def pyc(self, module, stem, source):
        """Ship code the way the image does: a bare .pyc, no source."""
        source_path = self.write(module, stem + '.py', source)
        compiled = source_path.with_suffix('.pyc')
        py_compile.compile(str(source_path), cfile=str(compiled), doraise=True)
        source_path.unlink()
        return compiled

    def run_guard(self, modules=REQUESTED, paths=None):
        paths = paths or [self.core, self.addons]
        result = subprocess.run(
            [sys.executable, str(GUARD), 'guard', '--json', '--modules', modules,
             '--addons-path', ','.join(str(p) for p in paths)],
            text=True, capture_output=True, timeout=60)
        report = json.loads(result.stdout) if result.stdout.strip() else None
        return result.returncode, report, result.stderr

    def assert_accepted(self, **kwargs):
        code, report, stderr = self.run_guard(**kwargs)
        self.assertEqual(code, uat_guard.ACCEPTED, stderr)
        self.assertEqual(report['verdict'], 'accepted')
        self.assertNotIn(EXCLUDED, report['install_set'])
        return report

    def assert_refused(self, expected, **kwargs):
        code, report, stderr = self.run_guard(**kwargs)
        self.assertEqual(code, uat_guard.REFUSED, stderr)
        self.assertEqual(report['verdict'], 'refused')
        self.assertIn(expected, stderr)
        return report

    # ── 1. safe graph ───────────────────────────────────────────────────────
    def test_safe_graph_is_accepted_and_the_declared_bridge_stays_out(self):
        report = self.assert_accepted()
        self.assertEqual(report['declared_dependents'], {'perodua_demo_client_ui': False})
        self.assertIn('perodua_demo_client_ui', report['references_outside_install_set'])
        self.assertEqual(report['excluded_auto_install'], 'auto_install is off')

    # ── 2/3. manifest dependencies ─────────────────────────────────────────
    def test_direct_dependency_is_refused(self):
        self.module('perodua_uiux_api', depends=['perodua_client_stable', EXCLUDED])
        report = self.assert_refused('Odoo would install perodua_demo_client')
        self.assertIn(EXCLUDED, report['install_set'])

    def test_transitive_dependency_is_refused_with_the_chain(self):
        self.module('perodua_rp', depends=[EXCLUDED])
        self.module('perodua_uiux_api', depends=['perodua_client_stable', 'perodua_rp'])
        self.assert_refused('perodua_demo_client <- perodua_rp <- perodua_uiux_api '
                            '<- requested for initialization')

    def test_auto_install_bridge_that_pulls_the_module_in_is_refused(self):
        # Triggers are satisfied by the requested graph, and the bridge drags
        # its non-trigger dependency -- the excluded module -- in with it.
        self.module('perodua_bridge', depends=['perodua_uiux_api', EXCLUDED],
                    auto_install=['perodua_uiux_api'])
        self.assert_refused("perodua_demo_client <- perodua_bridge <- auto_install fires once "
                            "all of ['perodua_uiux_api'] are installed")

    # ── 4. hidden references ────────────────────────────────────────────────
    def test_xml_id_reference_in_data_is_refused(self):
        self.write('perodua_client_stable', 'data/menus.xml', '''
            <odoo><record id="menu" model="ir.ui.menu">
              <field name="parent_id" ref="perodua_demo_client.some_record"/>
            </record></odoo>''')
        self.assert_refused('data/menus.xml: line 3')

    def test_env_ref_inside_compiled_code_is_refused(self):
        self.pyc('perodua_uiux_api', 'controllers/main', '''
            def lookup(env):
                """Nothing in this docstring matters."""
                return env.ref('perodua_demo_client.some_record')
            ''')
        self.assert_refused("constant 'perodua_demo_client.some_record'")

    def test_import_of_the_module_is_refused(self):
        self.pyc('perodua_uiux_api', 'models/loader', 'import odoo.addons.perodua_demo_client\n')
        self.assert_refused('name odoo.addons.perodua_demo_client')

    def test_formatted_reference_inside_compiled_code_is_refused(self):
        self.pyc('perodua_uiux_api', 'models/fmt',
                 "def ref(env, x):\n    return env.ref(f'perodua_demo_client.{x}')\n")
        self.assert_refused("constant 'perodua_demo_client.'")

    def test_bare_module_name_in_code_is_refused(self):
        self.pyc('perodua_client_stable', 'hooks', '''
            def post_init(env):
                env['ir.module.module'].search([('name', '=', 'perodua_demo_client')]).button_install()
            ''')
        self.assert_refused("constant 'perodua_demo_client'")

    def test_csv_reference_is_refused(self):
        self.write('perodua_client_stable', 'data/partners.csv',
                   'id,parent_id:id\nx,perodua_demo_client.partner_hq\n')
        self.assert_refused('data/partners.csv: line 2')

    def test_prose_mentions_are_not_reliance(self):
        # The real image mentions the module in XML comments, manifest comments,
        # docstrings and log text; none of those is a dependency.
        self.write('perodua_client_stable', 'data/sequence.xml', '''
            <odoo>
              <!-- renumbered from spreadsheets owned by perodua_demo_client.
                   see perodua_demo_client.hooks for the loader -->
              <record id="seq" model="ir.sequence"><field name="name">X</field></record>
            </odoo>''')
        manifest = self.addons / 'perodua_master_data' / '__manifest__.py'
        manifest.write_text("# baked perodua_demo_client dataset; see perodua_demo_client.hooks\n"
                            + manifest.read_text(), encoding='utf-8')
        self.pyc('perodua_uiux_api', 'models/notes', '''
            """When the OPTIONAL perodua_demo_client module is present, see perodua_demo_client.hooks."""
            import logging
            def note():
                logging.getLogger(__name__).info("perodua_demo_client is not installed here")
            ''')
        self.assert_accepted()

    def test_reference_outside_the_install_set_is_reported_not_refused(self):
        self.module('perodua_optional_report', depends=['perodua_uiux_api'])
        self.write('perodua_optional_report', 'views/view.xml',
                   '<odoo><record id="x" model="ir.ui.view" inherit_id="perodua_demo_client.v"/></odoo>')
        report = self.assert_accepted()
        self.assertIn('perodua_optional_report', report['references_outside_install_set'])

    # Forms found missing by review: each loads or installs the module's
    # content without an XML ID, so a prefix match on constants missed them.
    def test_path_into_the_module_folder_in_compiled_code_is_refused(self):
        self.pyc('perodua_client_stable', 'hooks', """
            from odoo.tools import file_open
            def post_init(env):
                return file_open('perodua_demo_client/data/client/parts.csv').read()
            """)
        self.assert_refused("perodua_demo_client/data/client/parts.csv")

    def test_module_name_inside_sql_in_compiled_code_is_refused(self):
        self.pyc('perodua_client_stable', 'hooks', """
            def post_init(env):
                env.cr.execute("SELECT id FROM ir_model_data WHERE module = 'perodua_demo_client'")
            """)
        self.assert_refused("WHERE module = 'perodua_demo_client'")

    def test_settings_field_that_installs_the_module_is_refused(self):
        self.pyc('perodua_uiux_api', 'models/settings', """
            class ResConfigSettings:
                module_perodua_demo_client = True
            """)
        self.assert_refused('name module_perodua_demo_client')

    def test_settings_field_in_a_view_is_refused(self):
        self.write('perodua_uiux_api', 'views/settings.xml',
                   '<odoo><record id="v" model="ir.ui.view"><field name="arch" type="xml">'
                   '<field name="module_perodua_demo_client"/></field></record></odoo>')
        self.assert_refused('views/settings.xml: line 1')

    def test_asset_path_in_a_manifest_is_refused(self):
        self.module('perodua_uiux_api', depends=['perodua_client_stable'],
                    assets={'web.assets_backend': ['perodua_demo_client/static/src/demo.js']})
        self.assert_refused('perodua_uiux_api/__manifest__.py')

    def test_spreadsheet_data_file_is_scanned(self):
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('xl/sharedStrings.xml', '<sst><si><t>perodua_demo_client.partner</t></si></sst>')
        path = self.addons / 'perodua_client_stable' / 'data' / 'import.xlsx'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.getvalue())
        self.assert_refused('data/import.xlsx: xl/sharedStrings.xml')

    def test_unexpected_errors_are_a_refusal_not_a_crash(self):
        original = uat_guard.run_guard
        def explode(*args, **kwargs):
            raise PermissionError('fixture')
        uat_guard.run_guard = explode
        try:
            code = uat_guard.main(['guard', '--modules', REQUESTED, '--addons-path', str(self.addons)])
        finally:
            uat_guard.run_guard = original
        self.assertEqual(code, uat_guard.BROKEN)

    # ── 5/6. auto_install of the excluded module ────────────────────────────
    def test_auto_install_true_is_refused(self):
        self.module(EXCLUDED, depends=['perodua_master_data'], auto_install=True)
        self.assert_refused('could be installed automatically')

    def test_auto_install_empty_list_means_always_and_is_refused(self):
        # Odoo 19 normalizes [] to an empty trigger set and installs such a
        # module unconditionally ("special case: [] to always install").
        self.module(EXCLUDED, depends=['perodua_master_data'], auto_install=[])
        report = self.assert_refused('empty list: Odoo installs it unconditionally')
        self.assertIn(EXCLUDED, report['install_set'])

    def test_any_other_auto_install_empty_list_module_joins_the_install_set(self):
        self.module('perodua_always', depends=['perodua_master_data'], auto_install=[])
        report = self.assert_accepted()
        self.assertIn('perodua_always', report['install_set'])

    def test_auto_install_list_naming_a_trigger(self):
        self.module(EXCLUDED, depends=['perodua_master_data'], auto_install=['perodua_master_data'])
        self.assert_refused('auto_install fires once all of')

    # ── 7. requested list itself ────────────────────────────────────────────
    def test_requesting_the_module_itself_is_refused(self):
        self.assert_refused('the requested modules name perodua_demo_client itself',
                            modules=REQUESTED + ',' + EXCLUDED)

    # ── inspection failures are refusals too ────────────────────────────────
    def test_missing_requested_module_cannot_be_verified(self):
        code, _, stderr = self.run_guard(modules=REQUESTED + ',perodua_missing')
        self.assertEqual(code, uat_guard.BROKEN)
        self.assertIn('perodua_missing', stderr)

    def test_non_literal_manifest_cannot_be_verified(self):
        (self.addons / 'perodua_uiux_api' / '__manifest__.py').write_text(
            "{'name': 'x', 'depends': __import__('os').listdir('/')}\n")
        code, _, stderr = self.run_guard()
        self.assertEqual(code, uat_guard.BROKEN)
        self.assertIn('not a readable Python literal', stderr)

    def test_pyc_for_another_python_cannot_be_verified(self):
        compiled = self.pyc('perodua_uiux_api', 'models/other', 'X = 1\n')
        data = bytearray(compiled.read_bytes())
        data[0] ^= 0xFF
        compiled.write_bytes(bytes(data))
        code, _, stderr = self.run_guard()
        self.assertEqual(code, uat_guard.BROKEN)
        self.assertIn('different Python', stderr)

    def test_first_module_on_the_path_wins_like_odoo(self):
        shadow = self.root / 'data-dir-addons'
        shadow.mkdir()
        self.module('perodua_uiux_api', depends=['perodua_client_stable', EXCLUDED], root=shadow)
        self.assert_refused('Odoo would install perodua_demo_client',
                            paths=[self.core, shadow, self.addons])

    # ── reviewed references are pinned to file bytes ───────────────────────
    def reviewed_run(self, reviewed):
        args = argparse.Namespace(modules=REQUESTED, exclude=EXCLUDED, odoo_config=None,
                                  addons_path=f'{self.core},{self.addons}', json=True)
        return uat_guard.run_guard(args, reviewed=reviewed)

    def test_reviewed_reference_is_accepted_only_for_the_reviewed_bytes(self):
        compiled = self.pyc('perodua_client_stable', 'hooks',
                            "FLAG = 'perodua_demo_client.loaded'\n")
        entry = {'file': 'perodua_client_stable/hooks.pyc',
                 'finding': "constant 'perodua_demo_client.loaded'",
                 'sha256': hashlib.sha256(compiled.read_bytes()).hexdigest(),
                 'reason': 'config-parameter key read with a default'}
        self.assertEqual(self.reviewed_run([])['verdict'], 'refused')
        report = self.reviewed_run([entry])
        self.assertEqual(report['verdict'], 'accepted')
        self.assertIn('perodua_client_stable', report['reviewed_references'])
        # Any change to the file voids the review.
        self.pyc('perodua_client_stable', 'hooks',
                 "FLAG = 'perodua_demo_client.loaded'\nOTHER = 1\n")
        report = self.reviewed_run([entry])
        self.assertEqual(report['verdict'], 'refused')
        self.assertEqual(report['unused_reviews'],
                         ["perodua_client_stable/hooks.pyc: constant 'perodua_demo_client.loaded'"])

    def test_shipped_review_is_pinned_to_one_file(self):
        self.assertEqual(len(uat_guard.REVIEWED_REFERENCES), 1)
        review = uat_guard.REVIEWED_REFERENCES[0]
        self.assertRegex(review['sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(review['file'], 'perodua_demo/hooks.pyc')


class NormalizationTests(unittest.TestCase):
    """Manifest normalization mirrors odoo/modules/module.py in 19.0."""

    def test_empty_depends_means_base(self):
        self.assertEqual(uat_guard.normalize('x', {})['depends'], ['base'])
        self.assertEqual(uat_guard.normalize('base', {})['depends'], [])

    def test_auto_install_forms(self):
        norm = uat_guard.normalize
        self.assertIs(norm('x', {'depends': ['a']})['auto_install'], False)
        self.assertIs(norm('x', {'depends': ['a'], 'auto_install': False})['auto_install'], False)
        self.assertEqual(norm('x', {'depends': ['a', 'b'], 'auto_install': True})['auto_install'], {'a', 'b'})
        self.assertEqual(norm('x', {'depends': ['a'], 'auto_install': []})['auto_install'], set())
        self.assertEqual(norm('x', {'depends': ['a', 'b'], 'auto_install': ['a']})['auto_install'], {'a'})

    def test_auto_install_trigger_outside_depends_is_rejected_like_odoo(self):
        with self.assertRaises(uat_guard.Broken):
            uat_guard.normalize('x', {'depends': ['a'], 'auto_install': ['b']})

    def test_only_verified_odoo_versions_have_a_demo_flag(self):
        self.assertEqual(uat_guard.DEMO_FLAGS, {19: '--without-demo=True'})


if __name__ == '__main__':
    unittest.main()
