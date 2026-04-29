"""TLS path loading + context building."""

import os
import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import tls  # noqa: E402
from lib.errors import StateError  # noqa: E402


def _have_openssl() -> bool:
    try:
        subprocess.run(["openssl", "version"], check=True,
                       capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _make_selfsigned(dir: Path) -> tuple[Path, Path]:
    cert = dir / "cert.pem"
    key = dir / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-days", "1", "-keyout", str(key), "-out", str(cert),
         "-subj", "/CN=test"],
        check=True, capture_output=True,
    )
    return cert, key


class LoadPathsTests(unittest.TestCase):

    def setUp(self):
        self._cert_env = os.environ.pop(tls.ENV_CERT, None)
        self._key_env = os.environ.pop(tls.ENV_KEY, None)
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._cert = self._dir / "cert.pem"
        self._key = self._dir / "key.pem"
        self._cert.write_text("dummy cert")
        self._key.write_text("dummy key")

    def tearDown(self):
        if self._cert_env is not None:
            os.environ[tls.ENV_CERT] = self._cert_env
        if self._key_env is not None:
            os.environ[tls.ENV_KEY] = self._key_env
        self._tmp.cleanup()

    def test_no_cli_no_env_returns_none(self):
        self.assertIsNone(tls.load_tls_paths())

    def test_cli_wins_over_env(self):
        os.environ[tls.ENV_CERT] = "/env/cert"
        os.environ[tls.ENV_KEY] = "/env/key"
        c, k = tls.load_tls_paths(cli_cert=str(self._cert), cli_key=str(self._key))
        self.assertEqual(c, self._cert)
        self.assertEqual(k, self._key)

    def test_half_configured_raises(self):
        with self.assertRaises(StateError):
            tls.load_tls_paths(cli_cert=str(self._cert))
        with self.assertRaises(StateError):
            tls.load_tls_paths(cli_key=str(self._key))

    def test_missing_file_raises(self):
        with self.assertRaises(StateError) as ctx:
            tls.load_tls_paths(cli_cert="/nope/x", cli_key=str(self._key))
        self.assertIn("not found", ctx.exception.message)

    def test_env_vars_picked_up(self):
        os.environ[tls.ENV_CERT] = str(self._cert)
        os.environ[tls.ENV_KEY] = str(self._key)
        c, k = tls.load_tls_paths()
        self.assertEqual((c, k), (self._cert, self._key))


@unittest.skipUnless(_have_openssl(), "requires openssl for self-signed cert")
class BuildContextTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cert, self.key = _make_selfsigned(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_builds_context_successfully(self):
        ctx = tls.build_context(self.cert, self.key)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_minimum_version_is_tls_1_2(self):
        ctx = tls.build_context(self.cert, self.key)
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_invalid_cert_raises_state_error(self):
        bad = Path(self._tmp.name) / "bad.pem"
        bad.write_text("not a cert")
        with self.assertRaises(StateError):
            tls.build_context(bad, self.key)


if __name__ == "__main__":
    unittest.main()
