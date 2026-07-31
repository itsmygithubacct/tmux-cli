"""Tests for the standalone bin/update_tb.py puller.

Focus on the security-sensitive archive extraction (_extract_lib) and the
small pure helpers. Network paths (main/_download_tree/_latest_tag) are not
exercised here.
"""

import importlib.util
import io
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def _make_tar_with_symlink(members: dict[str, bytes],
                           link_name: str) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        info = tarfile.TarInfo(link_name)
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        tf.addfile(info)
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
        # extraction dir via ".." must be refused by the validator.
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
            self.assertFalse((Path(d) / "lib").exists())

    def test_symlink_member_is_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("root/lib/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            tf.addfile(info)
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tf, \
                tempfile.TemporaryDirectory() as d:
            with self.assertRaises(tarfile.TarError):
                update_tb._extract_lib(tf, "root", Path(d) / "lib")
            self.assertFalse((Path(d) / "lib").exists())

    def test_extracted_files_are_not_executable(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            content = b"X = 1\n"
            info = tarfile.TarInfo("root/lib/config.py")
            info.size = len(content)
            info.mode = 0o6755
            tf.addfile(info, io.BytesIO(content))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tf, \
                tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "lib"
            update_tb._extract_lib(tf, "root", dest)
            self.assertEqual(dest.joinpath("config.py").stat().st_mode & 0o777,
                             0o644)


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


class TransactionalInstallTests(unittest.TestCase):

    def test_rejected_lib_does_not_replace_existing_tb(self):
        tf = _make_tar_with_symlink({
            "root/tb.py": b"NEW TB\n",
            "root/lib/version.py": b'__version__ = "2.0"\n',
        }, "root/lib/link")
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            (dest / "tb.py").write_text("OLD TB\n")
            (dest / "lib").mkdir()
            (dest / "lib" / "version.py").write_text(
                '__version__ = "1.0"\n')
            with mock.patch.object(update_tb, "_download_tree", return_value=tf):
                rc = update_tb.main([
                    "--repo", "example/project", "--ref", "bad", "--dir", d,
                ])

            self.assertEqual(rc, 1)
            self.assertEqual((dest / "tb.py").read_text(), "OLD TB\n")
            self.assertIn("1.0", (dest / "lib" / "version.py").read_text())

    def test_success_replaces_tb_and_the_complete_library(self):
        tf = _make_tar({
            "root/tb.py": b"NEW TB\n",
            "root/lib/version.py": b'__version__ = "2.0"\n',
            "root/lib/current.py": b"CURRENT = True\n",
        })
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            (dest / "tb.py").write_text("OLD TB\n")
            (dest / "lib").mkdir()
            (dest / "lib" / "stale.py").write_text("STALE = True\n")
            with mock.patch.object(update_tb, "_download_tree", return_value=tf):
                rc = update_tb.main([
                    "--repo", "example/project", "--ref", "good", "--dir", d,
                ])

            self.assertEqual(rc, 0)
            self.assertEqual((dest / "tb.py").read_text(), "NEW TB\n")
            self.assertTrue((dest / "lib" / "current.py").is_file())
            self.assertFalse((dest / "lib" / "stale.py").exists())

    def test_success_installs_owner_only_logging_manual(self):
        tf = _make_tar({
            "root/tb.py": b"NEW TB\n",
            "root/lib/version.py": b'__version__ = "2.0"\n',
            "root/docs/logging.md": b"# Permanent logs\n",
        })
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dest = base / "program"
            data = base / "data"
            with mock.patch.object(update_tb, "_download_tree", return_value=tf):
                rc = update_tb.main([
                    "--repo", "example/project",
                    "--ref", "good",
                    "--dir", str(dest),
                    "--data-dir", str(data),
                ])

            self.assertEqual(rc, 0)
            manual = data / "logging.md"
            self.assertEqual(manual.read_text(), "# Permanent logs\n")
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(manual.stat().st_mode), 0o600)

    def test_file_only_still_installs_logging_manual(self):
        tf = _make_tar({
            "root/tb.py": b"NEW TB\n",
            "root/docs/logging.md": b"# Read me after uninstall\n",
        })
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            dest = base / "program"
            data = base / "data"
            with mock.patch.object(update_tb, "_download_tree", return_value=tf):
                rc = update_tb.main([
                    "--repo", "example/project",
                    "--ref", "good",
                    "--dir", str(dest),
                    "--data-dir", str(data),
                    "--file-only",
                ])

            self.assertEqual(rc, 0)
            self.assertFalse((dest / "lib").exists())
            self.assertEqual(
                (data / "logging.md").read_text(),
                "# Read me after uninstall\n",
            )

    def test_update_refuses_symlink_logging_data_directory(self):
        tf = _make_tar({
            "root/tb.py": b"NEW TB\n",
            "root/docs/logging.md": b"# Must not follow link\n",
        })
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            target = base / "target"
            target.mkdir()
            data = base / "data-link"
            data.symlink_to(target, target_is_directory=True)
            with mock.patch.object(update_tb, "_download_tree", return_value=tf):
                rc = update_tb.main([
                    "--repo", "example/project",
                    "--ref", "good",
                    "--dir", str(base / "program"),
                    "--data-dir", str(data),
                    "--file-only",
                ])

            self.assertEqual(rc, 1)
            self.assertFalse((target / "logging.md").exists())

    def test_install_io_failure_restores_both_old_paths(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            stage = dest / "stage"
            stage.mkdir()
            staged_tb = stage / "tb.py"
            staged_tb.write_text("NEW TB\n")
            staged_lib = stage / "lib"
            staged_lib.mkdir()
            (staged_lib / "version.py").write_text("NEW LIB\n")
            (dest / "tb.py").write_text("OLD TB\n")
            (dest / "lib").mkdir()
            (dest / "lib" / "version.py").write_text("OLD LIB\n")

            real_replace = Path.replace

            def fail_tb_install(path, target):
                if path == staged_tb:
                    raise OSError("simulated tb install failure")
                return real_replace(path, target)

            with mock.patch.object(Path, "replace", new=fail_tb_install):
                with self.assertRaises(OSError):
                    update_tb._install_staged(staged_tb, staged_lib, dest)

            self.assertEqual((dest / "tb.py").read_text(), "OLD TB\n")
            self.assertEqual(
                (dest / "lib" / "version.py").read_text(), "OLD LIB\n")


if __name__ == "__main__":
    unittest.main()
