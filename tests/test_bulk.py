from __future__ import annotations

import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.tb_cmds import bulk  # noqa: E402


class SnapshotTests(unittest.TestCase):
    @mock.patch.object(bulk, "_dashboard_status")
    @mock.patch.object(bulk.ttyd, "status_all")
    @mock.patch.object(bulk.ports, "all_assignments")
    @mock.patch.object(bulk.sessions, "server_running", return_value=True)
    @mock.patch.object(
        bulk.sessions,
        "list_panes",
        return_value=[{"session": "work", "pane_id": "%1"}],
    )
    @mock.patch.object(
        bulk.sessions,
        "list_sessions",
        return_value=[{"name": "work"}],
    )
    def test_tmux_only_snapshot_skips_optional_service_probes(
        self,
        _list_sessions,
        _list_panes,
        _server_running,
        assignments,
        ttyds,
        dashboard,
    ):
        data = bulk.snapshot_data(tmux_only=True)

        self.assertEqual(data["sessions"], [{"name": "work"}])
        self.assertEqual(data["panes"][0]["pane_id"], "%1")
        self.assertTrue(data["tmux_server"])
        self.assertNotIn("ttyd", data)
        self.assertNotIn("dashboard", data)
        assignments.assert_not_called()
        ttyds.assert_not_called()
        dashboard.assert_not_called()
        _server_running.assert_not_called()

    @mock.patch.object(
        bulk, "_dashboard_status", return_value={"listening": False},
    )
    @mock.patch.object(bulk.ttyd, "status_all", return_value=[])
    @mock.patch.object(bulk.ports, "all_assignments", return_value={})
    @mock.patch.object(bulk.sessions, "server_running", return_value=False)
    @mock.patch.object(bulk.sessions, "list_panes", return_value=[])
    @mock.patch.object(bulk.sessions, "list_sessions", return_value=[])
    def test_full_snapshot_keeps_dashboard_contract(
        self,
        _list_sessions,
        _list_panes,
        _server_running,
        _assignments,
        _ttyds,
        _dashboard,
    ):
        data = bulk.snapshot_data()

        self.assertIn("ttyd", data)
        self.assertIn("dashboard", data)
        self.assertEqual(data["ttyd"]["assignments"], {})

    @mock.patch.object(bulk.output, "emit_json")
    @mock.patch.object(
        bulk.sessions,
        "capture_target",
        return_value=(True, "ready\n"),
    )
    @mock.patch.object(
        bulk,
        "snapshot_data",
        return_value={"sessions": [], "panes": [], "tmux_server": True},
    )
    def test_snapshot_can_bundle_one_preview_capture(
        self,
        snapshot_data,
        capture_target,
        emit_json,
    ):
        args = SimpleNamespace(
            tmux_only=True,
            capture="work:2.1",
            lines=30,
            json=True,
            human=False,
        )

        self.assertEqual(bulk.cmd_snapshot(args), 0)

        snapshot_data.assert_called_once_with(tmux_only=True)
        target = capture_target.call_args.args[0]
        self.assertEqual((target.session, target.window, target.pane),
                         ("work", "2", "1"))
        self.assertEqual(capture_target.call_args.kwargs["lines"], 30)
        payload = emit_json.call_args.args[0]
        self.assertEqual(payload["capture"]["content"], "ready\n")
        self.assertEqual(payload["capture"]["error"], "")

    @mock.patch.object(bulk, "snapshot_data")
    def test_capture_lines_without_capture_is_a_usage_error(self, snapshot_data):
        args = SimpleNamespace(
            tmux_only=True,
            capture=None,
            lines=30,
            json=True,
            human=False,
        )

        with self.assertRaises(bulk.UsageError) as caught:
            bulk.cmd_snapshot(args)

        self.assertIn("--lines requires --capture", str(caught.exception))
        snapshot_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
