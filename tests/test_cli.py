"""CLI preserves broker evidence and never treats partial paging as complete."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from team_drive.cli import collect, parameters, wait_ready
from team_drive.engine import Engine
from team_drive.errors import DriveError


class CliTests(unittest.TestCase):
    def test_confirmed_identity_is_checked_before_first_index_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / "source"
            source.mkdir()
            info = source.stat()
            config = base / "config.json"
            config.write_text(json.dumps({"provider": "local", "root": str(source), "state_dir": str(base / "state"),
                "approved_source_identity": {"provider": "local", "device": info.st_dev, "inode": info.st_ino}}), encoding="utf-8")
            source.rename(base / "previous")
            source.mkdir()
            with self.assertRaises(DriveError) as caught:
                Engine(config, autostart=False)
            self.assertEqual(caught.exception.code, "APPROVED_SOURCE_CHANGED")
            self.assertFalse((base / "state").exists())

    def test_parameters_reject_nonfinite_oversized_and_foreign_cursor(self):
        for value in ('[]', '{"n":NaN}', '{"n":Infinity}', '{"cursor":"old-session"}', ' ' * 65537):
            with self.subTest(value=value[:50]), self.assertRaises(DriveError):
                parameters(raw=value)

    def test_parameters_from_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "params.json"
            path.write_text('{"query":"模拟预算"}', encoding="utf-8")
            self.assertEqual(parameters(path=path), {"query": "模拟预算"})

    def test_paging_real_engine_uses_one_session_and_returns_all_files(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "source"
            source.mkdir()
            for n in range(5):
                (source / (str(n) + ".txt")).write_text("SIMULATED\n", encoding="utf-8")
            config = base / "config.json"
            config.write_text(json.dumps({"root": str(source), "state_dir": str(base / "state")}), encoding="utf-8")
            engine = Engine(str(config), autostart=False)
            try:
                engine.scan_once()
                bridge = mock.Mock()
                bridge.call.side_effect = lambda method, params: engine.call(method, params, session_id="cli-test")
                result = collect(bridge, "list", {"limit": 2})
                self.assertTrue(result["pagination_complete"])
                self.assertEqual(sum(len(page["files"]) for page in result["pages"]), 5)
                self.assertTrue(all("next_cursor" not in page for page in result["pages"]))
                bounded = collect(bridge, "list", {"limit": 2}, max_pages=1)
                self.assertFalse(bounded["pagination_complete"])
                self.assertEqual(bounded["stop_reason"], "page_budget")
            finally:
                engine.close()

    def test_permission_failure_discards_preceding_pages(self):
        bridge = mock.Mock()
        bridge.call.side_effect = [{"ok": True, "files": [{"name": "private"}], "next_cursor": "next"},
                                   {"ok": False, "error": {"code": "ACCESS_DENIED"}}]
        result = collect(bridge, "list", {})
        self.assertFalse(result["ok"])
        self.assertNotIn("pages", result)
        self.assertEqual(result["pages_discarded"], 1)

    def test_wait_checks_job_completion_not_inventory_alone(self):
        bridge = mock.Mock()
        bridge.call.side_effect = [{"ok": True, "current_job_id": "job", "coverage": {"inventory_complete": True}},
                                   {"ok": True, "job": {"status": "running"}}]
        result = wait_ready(bridge, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INDEX_STILL_RUNNING")

    def test_wait_reports_resumable_partial_job_without_claiming_complete(self):
        bridge = mock.Mock()
        bridge.call.side_effect = [{"ok": True, "current_job_id": "job", "coverage": {"inventory_complete": False}},
                                  {"ok": True, "job": {"status": "partial"}}]
        result = wait_ready(bridge, 20)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INDEX_PARTIAL")
        self.assertIn("resume", result["error"]["next_action"])


if __name__ == "__main__":
    unittest.main()
