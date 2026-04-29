"""Interface resolution cache in lib/ttyd.py.

Verifies:
  - Loopback/wildcard inputs never touch subprocess.
  - Concrete addresses cache after the first resolution.
  - The ifconfig fallback parser handles a representative BSD/macOS stanza.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ttyd  # noqa: E402


class ShortCircuitTests(unittest.TestCase):

    def setUp(self):
        ttyd._iface_cache.clear()

    def test_wildcard_returns_none_without_subprocess(self):
        with mock.patch.object(ttyd, "subprocess") as sp:
            self.assertIsNone(ttyd._ttyd_interface("0.0.0.0"))
            self.assertIsNone(ttyd._ttyd_interface("::"))
            self.assertIsNone(ttyd._ttyd_interface(""))
            self.assertIsNone(ttyd._ttyd_interface(None))
            sp.check_output.assert_not_called()

    def test_loopback_returns_lo_without_subprocess(self):
        with mock.patch.object(ttyd, "subprocess") as sp:
            self.assertEqual(ttyd._ttyd_interface("127.0.0.1"), "lo")
            self.assertEqual(ttyd._ttyd_interface("localhost"), "lo")
            self.assertEqual(ttyd._ttyd_interface("::1"), "lo")
            sp.check_output.assert_not_called()


class CacheTests(unittest.TestCase):

    def setUp(self):
        ttyd._iface_cache.clear()

    def test_second_call_hits_cache(self):
        calls = {"count": 0}

        def fake_ip_addr(targets):
            calls["count"] += 1
            return "eth99" if "10.0.0.5" in targets else None

        with mock.patch.object(ttyd, "_iface_from_ip_addr", side_effect=fake_ip_addr), \
             mock.patch.object(ttyd, "_iface_from_ifconfig", return_value=None):
            self.assertEqual(ttyd._ttyd_interface("10.0.0.5"), "eth99")
            self.assertEqual(ttyd._ttyd_interface("10.0.0.5"), "eth99")
        self.assertEqual(calls["count"], 1,
                         "second call must be served from _iface_cache, not re-resolved")


class IfconfigFallbackTests(unittest.TestCase):

    SAMPLE_OUTPUT = (
        "lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n"
        "\tinet 127.0.0.1 netmask 0xff000000\n"
        "en0: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500\n"
        "\toptions=6463<RXCSUM,TXCSUM,TSO4,TSO6,CHANNEL_IO>\n"
        "\tether aa:bb:cc:dd:ee:ff\n"
        "\tinet6 fe80::aa:bbff:fecc:ddee%en0 prefixlen 64\n"
        "\tinet 192.168.1.50 netmask 0xffffff00 broadcast 192.168.1.255\n"
        "en1: flags=8863<UP,BROADCAST,SMART,RUNNING> mtu 1500\n"
        "\tinet 10.0.0.2 netmask 0xffffff00\n"
    )

    def _fake_ifconfig(self, *args, **kwargs):
        return self.SAMPLE_OUTPUT

    def test_matches_address_on_expected_interface(self):
        with mock.patch.object(ttyd.subprocess, "check_output", self._fake_ifconfig):
            self.assertEqual(ttyd._iface_from_ifconfig({"192.168.1.50"}), "en0")
            self.assertEqual(ttyd._iface_from_ifconfig({"10.0.0.2"}), "en1")

    def test_returns_none_when_no_match(self):
        with mock.patch.object(ttyd.subprocess, "check_output", self._fake_ifconfig):
            self.assertIsNone(ttyd._iface_from_ifconfig({"1.2.3.4"}))

    def test_returns_none_when_ifconfig_missing(self):
        def _missing(*a, **kw):
            raise FileNotFoundError("ifconfig")
        with mock.patch.object(ttyd.subprocess, "check_output", _missing):
            self.assertIsNone(ttyd._iface_from_ifconfig({"192.168.1.50"}))


if __name__ == "__main__":
    unittest.main()
