"""Tests for the standalone bin/update_tb.py puller.

Focus on the security-sensitive archive extraction (_extract_lib) and the
small pure helpers. Network paths (main/_download_tree/_latest_tag) are not
exercised here.
"""

import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin" / "update_tb.py"
_spec = importlib.util.spec_from_file_location("update_tb", _BIN)
update_tb = importlib.util.module_from_spec(_spec)
sys.modules["update_tb"] = update_tb
_spec.loader.exec_module(update_tb)


def _make_tar(members: dict[str, bytes]) -> tarfile.TarFile:
    """Build an in-memory tar from {name: content} and return it open for read."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


class ExtractLibTests(unittest.TestCase):

    def test_extracts_benign_lib(self):
        tf = _make_tar({
            "root/tb.py": b"# tb\n",
            "root/lib/__init__.py": b"",
            "root/lib/config.py": b"X = 1\n",
        })
        with tf, tempfile.TemporaryDirectory() as d:
            dest_lib = Path(d) / "lib"
            update_tb._extract_lib(tf, "root", dest_lib)
            self.assertTrue((dest_lib / "__init__.py").is_file())
            self.assertEqual((dest_lib / "config.py").read_text(), "X = 1\n")

    def test_no_lib_members_raises(self):
        tf = _make_tar({"root/tb.py": b"# tb\n"})
        with tf, tempfile.TemporaryDirectory() as d:
            with self.assertRaises(update_tb._NoLibError):
                update_tb._extract_lib(tf, "root", Path(d) / "lib")

    def test_traversal_member_is_rejected(self):
        # A member that passes the "root/lib/" prefix check but escapes the
        # extraction dir via ".." must be refused by the data filter.
        evil = "root/lib/../../../../../../tmp/evil_update_tb.py"
        tf = _make_tar({
            "root/lib/config.py": b"X = 1\n",
            evil: b"pwned\n",
        })
        with tf, tempfile.TemporaryDirectory() as d:
            with self.assertRaises(tarfile.TarError):
                update_tb._extract_lib(tf, "root", Path(d) / "lib")
            # And nothing landed outside the temp dir.
            self.assertFalse(Path("/tmp/evil_update_tb.py").exists())


class VersionHelperTests(unittest.TestCase):

    def test_version_of_parses(self):
        self.assertEqual(
            update_tb._version_of('__version__ = "1.2.3"\n'), "1.2.3")

    def test_version_of_missing_returns_none(self):
        self.assertIsNone(update_tb._version_of("nothing here\n"))

    def test_extract_member_text_roundtrip(self):
        tf = _make_tar({"root/lib/version.py": b'__version__ = "9.9"\n'})
        with tf:
            text = update_tb._extract_member_text(tf, "root", "lib/version.py")
            self.assertEqual(update_tb._version_of(text), "9.9")

    def test_extract_member_text_absent(self):
        tf = _make_tar({"root/tb.py": b"x\n"})
        with tf:
            self.assertIsNone(
                update_tb._extract_member_text(tf, "root", "lib/nope.py"))


if __name__ == "__main__":
    unittest.main()
