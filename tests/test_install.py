"""Installation contract for the executable and permanent-log manual."""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class MakeInstallTests(unittest.TestCase):

    def test_install_copies_private_manual_and_uninstall_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prefix = base / "bin"
            data = base / "data"
            variables = [
                f"PREFIX={prefix}",
                f"TMUX_CLI_HOME={data}",
            ]
            subprocess.run(
                ["make", "install", *variables],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            executable = prefix / "tb"
            manual = data / "logging.md"
            self.assertTrue(executable.is_symlink())
            self.assertEqual(
                manual.read_text(),
                (ROOT / "docs" / "logging.md").read_text(),
            )
            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(manual.stat().st_mode), 0o600)

            subprocess.run(
                ["make", "uninstall", *variables],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(executable.exists())
            self.assertTrue(manual.is_file())

    def test_install_refuses_symlink_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            data = base / "data-link"
            data.symlink_to(target, target_is_directory=True)
            result = subprocess.run(
                [
                    "make",
                    "install",
                    f"PREFIX={base / 'bin'}",
                    f"TMUX_CLI_HOME={data}",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((base / "bin" / "tb").exists())
            self.assertFalse((target / "logging.md").exists())

    def test_install_and_uninstall_preserve_unmanaged_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "prefix with spaces"
            state = root / "state"
            prefix.mkdir()
            command = prefix / "tb"
            command.write_text("user-owned\n", encoding="utf-8")
            variables = [
                f"PREFIX={prefix}",
                f"TMUX_CLI_HOME={state}",
            ]
            installed = subprocess.run(
                ["make", "install", *variables],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(installed.returncode, 0)
            self.assertEqual(command.read_text(encoding="utf-8"), "user-owned\n")

            subprocess.run(
                ["make", "uninstall", *variables],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(command.read_text(encoding="utf-8"), "user-owned\n")


if __name__ == "__main__":
    unittest.main()
