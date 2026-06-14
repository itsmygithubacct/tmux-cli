"""loader.load_one path-containment: manifest-supplied static_dir /
ui_blocks_path must not escape the extension's own directory."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.extensions import loader  # noqa: E402
from lib.version import __version__ as CORE_VERSION  # noqa: E402


def _write_ext(root: Path, manifest: dict) -> Path:
    ext = root / "ext_x"
    ext.mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps(manifest))
    return ext


_BASE_MANIFEST = {
    "name": "ext_x",
    "version": "0.0.1",
    "module": "ext_x_module",
    "min_tmux_browse": "0.0.1",
}


class ContainmentTests(unittest.TestCase):

    def test_static_dir_escape_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            m = dict(_BASE_MANIFEST, static_dir="../../../../tmp")
            ext = _write_ext(Path(d), m)
            with self.assertRaises(loader.ExtensionLoadError) as cm:
                loader.load_one(ext, core_version=CORE_VERSION)
            self.assertEqual(cm.exception.stage, "static")

    def test_ui_blocks_absolute_path_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            m = dict(_BASE_MANIFEST, ui_blocks_path="/etc/passwd")
            ext = _write_ext(Path(d), m)
            with self.assertRaises(loader.ExtensionLoadError) as cm:
                loader.load_one(ext, core_version=CORE_VERSION)
            self.assertEqual(cm.exception.stage, "ui_blocks")

    def test_legitimate_static_dir_loads(self):
        with tempfile.TemporaryDirectory() as d:
            m = dict(_BASE_MANIFEST, static_dir="static")
            ext = _write_ext(Path(d), m)
            (ext / "static").mkdir()
            (ext / "static" / "a.js").write_text("//\n")
            reg = loader.load_one(ext, core_version=CORE_VERSION)
            self.assertEqual([p.name for p in reg.static_js], ["a.js"])

    def test_entry_naming_core_module_is_refused(self):
        # routes_entry names a core module (lib.config), which is importable
        # but lives outside the extension tree — must be refused at 'import'.
        with tempfile.TemporaryDirectory() as d:
            m = dict(_BASE_MANIFEST, routes_entry="lib.config:ensure_dirs")
            ext = _write_ext(Path(d), m)
            with self.assertRaises(loader.ExtensionLoadError) as cm:
                loader.load_one(ext, core_version=CORE_VERSION)
            self.assertEqual(cm.exception.stage, "import")

    def test_entry_naming_stdlib_module_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            m = dict(_BASE_MANIFEST, cli_entry="os:getcwd")
            ext = _write_ext(Path(d), m)
            with self.assertRaises(loader.ExtensionLoadError) as cm:
                loader.load_one(ext, core_version=CORE_VERSION)
            self.assertEqual(cm.exception.stage, "import")

    def test_legitimate_in_tree_entry_loads(self):
        with tempfile.TemporaryDirectory() as d:
            m = dict(_BASE_MANIFEST, cli_entry="ext_x_cli:register_verb")
            ext = _write_ext(Path(d), m)
            (ext / "ext_x_cli.py").write_text(
                "def register_verb():\n"
                "    return {'hi': lambda *a: None}\n"
            )
            reg = loader.load_one(ext, core_version=CORE_VERSION)
            self.assertIn("hi", reg.cli_verbs)

    def test_helper_allows_contained_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "sub").mkdir()
            ok = loader._contained_path(base, "sub", "ext_x", "static")
            self.assertTrue(ok.is_relative_to(base.resolve()))
            with self.assertRaises(loader.ExtensionLoadError):
                loader._contained_path(base, "../escape", "ext_x", "static")


if __name__ == "__main__":
    unittest.main()
