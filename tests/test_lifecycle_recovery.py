"""Synthetic local-only recovery tests; no network source or broker is started."""
import json
import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from team_drive.engine import Engine
from team_drive.errors import DriveError, is_runtime_error, is_transient_source_error
from team_drive.broker import Broker
from team_drive.cli import collect, wait_ready
from team_drive.service_config import settings


class EngineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-recovery-synthetic-")
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.source / name).write_text("synthetic evidence\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"root": str(self.source), "state_dir": str(self.root / "state")}), encoding="utf-8")
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_runtime_failure_stops_batch_without_marking_sources_failed(self):
        with mock.patch.object(self.engine, "_parse_file_guarded", side_effect=DriveError("UNSAFE_STATE", "synthetic runtime fault")) as parse:
            with self.assertRaises(DriveError) as raised:
                self.engine.scan_once()
        self.assertEqual(raised.exception.code, "UNSAFE_STATE")
        self.assertEqual(parse.call_count, 1)
        status = self.engine.status()
        self.assertEqual(status["coverage"]["states"], {"pending": 3})
        self.assertEqual(status["index_status"]["state"], "blocked")
        self.assertFalse(status["index_status"]["queryable"])
        self.assertEqual(self.engine.job_status()["job"]["error_code"], "UNSAFE_STATE")

        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine.status()["runtime_error"]["code"], "UNSAFE_STATE")
        with self.assertRaises(DriveError) as raised:
            self.engine.scan_once()
        self.assertEqual(raised.exception.code, "INDEX_RUNTIME_BLOCKED")
        with mock.patch.object(self.engine, "_extract", return_value=(0, [], {})):
            result = self.engine.scan_once(force=True)
        self.assertEqual(result["job"]["status"], "complete")
        self.assertEqual(result["coverage"]["indexed_files"], 3)
        self.assertIsNone(self.engine.status()["runtime_error"])

    def test_unchanged_file_failures_need_explicit_force(self):
        with mock.patch.object(self.engine, "_extract", side_effect=DriveError("PARSE_FAILED", "synthetic damaged source")) as parse:
            first = self.engine.scan_once()
            self.assertEqual(parse.call_count, 3)
            second = self.engine.scan_once()
            self.assertEqual(parse.call_count, 3)
        self.assertEqual(first["coverage"]["states"], {"failed": 3})
        self.assertEqual(second["index_status"]["state"], "blocked")
        self.assertFalse(second["index_status"]["queryable"])
        with mock.patch.object(self.engine, "_extract", return_value=(0, [], {})) as parse:
            recovered = self.engine.scan_once(force=True)
            self.assertEqual(parse.call_count, 3)
        self.assertEqual(recovered["coverage"]["indexed_files"], 3)

    def test_cleanup_error_does_not_replace_primary_parse_error(self):
        with mock.patch.object(self.engine, "_extract", side_effect=DriveError("PARSE_FAILED", "synthetic original failure")), \
             mock.patch.object(self.engine._storage, "unlink_private", side_effect=DriveError("STATE_IO_ERROR", "synthetic cleanup failure")):
            with self.assertRaises(DriveError) as raised:
                self.engine.scan_once()
        self.assertEqual(raised.exception.code, "STATE_IO_ERROR")
        with self.engine._db() as db:
            codes = [(row[0], row[1]) for row in db.execute("SELECT status,error_code FROM files ORDER BY path")]
        self.assertEqual(codes, [("failed", "PARSE_FAILED"), ("pending", None), ("pending", None)])
        self.assertEqual(self.engine.status()["runtime_error"]["primary_error_code"], "PARSE_FAILED")

    def test_private_write_failure_stops_batch_as_runtime(self):
        original = self.engine._storage.new_private

        class FailingWrite:
            def write(self, data):
                raise DriveError("STATE_IO_ERROR", "synthetic private snapshot write failure")

        @contextlib.contextmanager
        def failing_output(path, *args, **kwargs):
            with original(path, *args, **kwargs) as output:
                yield FailingWrite() if Path(path).name.startswith("snapshot-") else output

        with mock.patch.object(self.engine._storage, "new_private", side_effect=failing_output):
            with self.assertRaises(DriveError) as raised:
                self.engine.scan_once()
        self.assertEqual(raised.exception.code, "STATE_IO_ERROR")
        self.assertEqual(self.engine.status()["coverage"]["states"], {"pending": 3})

    def test_cleanup_failure_after_success_blocks_without_erasing_ready_file(self):
        with mock.patch.object(self.engine, "_extract", return_value=(0, [], {})), \
             mock.patch.object(self.engine._storage, "unlink_private", side_effect=DriveError("STATE_IO_ERROR", "synthetic cleanup failure")):
            with self.assertRaises(DriveError) as raised:
                self.engine.scan_once()
        self.assertEqual(raised.exception.code, "STATE_IO_ERROR")
        self.assertEqual(self.engine.status()["coverage"]["states"], {"pending": 2, "ready": 1})

    def test_force_is_not_claimed_applied_to_an_existing_job(self):
        first = self.engine.refresh(force=True)
        second = self.engine.refresh(force=True)
        self.assertTrue(first["force_applied"])
        self.assertFalse(second["force_applied"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_explicit_cancel_still_ends_a_queued_scan(self):
        job = self.engine.refresh()
        self.engine.cancel(job["job_id"])
        result = self.engine.scan_once(job["job_id"])
        self.assertEqual(result["job"]["status"], "cancelled")

    def test_parser_start_failure_is_a_runtime_error(self):
        with mock.patch("team_drive.engine.subprocess.Popen", side_effect=OSError("synthetic executable missing")):
            with self.assertRaises(DriveError) as raised:
                self.engine.scan_once()
        self.assertEqual(raised.exception.code, "RUNTIME_PARSER_START_FAILED")
        self.assertEqual(self.engine.status()["coverage"]["states"], {"pending": 3})

    def test_background_worker_stays_observable_and_recovers_only_on_force(self):
        self.engine.close()
        with mock.patch.object(Engine, "_parse_file_guarded", side_effect=DriveError("STATE_PATH_TOO_LONG", "synthetic runtime failure")):
            self.engine = Engine(str(self.config), autostart=True)
            deadline = time.monotonic() + 30
            while self.engine._pending_job and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNone(self.engine._pending_job)
            self.assertTrue(self.engine._thread.is_alive())
            self.assertEqual(self.engine.status()["indexer_health"], "blocked")
        with self.assertRaises(DriveError) as raised:
            self.engine.refresh()
        self.assertEqual(raised.exception.code, "INDEX_RUNTIME_BLOCKED")
        with mock.patch.object(self.engine, "_extract", return_value=(0, [], {})):
            job = self.engine.refresh(force=True)
            # Windows validates the private directory chain for each I/O;
            # test recovery semantics without a five-second machine-speed SLA.
            deadline = time.monotonic() + 30
            while self.engine._pending_job and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertIsNone(self.engine._pending_job)
        self.assertEqual(self.engine.job_status(job["job_id"])["job"]["status"], "complete")
        self.assertEqual(self.engine.status()["coverage"]["indexed_files"], 3)

    def test_bounded_grace_setting_validation(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["service"] = {"scan_grace_seconds": 600}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(settings(self.config)["scan_grace_seconds"], 600)
        for invalid in (-1, 3601, True):
            config["service"]["scan_grace_seconds"] = invalid
            self.config.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(DriveError):
                settings(self.config)

    def _seed_readable_cache(self):
        def synthetic_extract(snapshot, parsed):
            with self.engine._storage.open_private(parsed, "w") as output:
                output.write(json.dumps({"text": "synthetic cached evidence", "locator": {"line_start": 1}}) + "\n")
            return 1, [], {}

        with mock.patch.object(self.engine, "_extract", side_effect=synthetic_extract):
            self.engine.scan_once()
        with self.engine._db() as db:
            return self.engine._file_out(db.execute("SELECT * FROM files ORDER BY path LIMIT 1").fetchone())

    def _cache_snapshot(self):
        with self.engine._db() as db:
            return {"files": [tuple(row) for row in db.execute("SELECT id,status,source_version,segments_count,error_code FROM files ORDER BY path")],
                    "segments": [tuple(row) for row in db.execute("SELECT * FROM segments ORDER BY file_id,ordinal")],
                    "generation": self.engine._generation(db),
                    "coverage": self.engine._coverage(db)}

    def test_transient_page_failures_return_no_evidence_and_preserve_cache(self):
        item = self._seed_readable_cache()
        before = self._cache_snapshot()
        for code, is_share in (("SHARE_TIMEOUT", True), ("GRAPH_TIMEOUT", False),
                               ("GRAPH_THROTTLED", False), ("GRAPH_UNAVAILABLE", False)):
            with self.subTest(code=code):
                source = SimpleNamespace(verify_origin=mock.Mock(side_effect=DriveError(code, "synthetic transient failure", True)))
                with mock.patch.object(self.engine, "_remote_source", True), \
                     mock.patch.object(self.engine, "_share_binding", {"synthetic": True} if is_share else None), \
                     mock.patch.object(self.engine, "_source", source):
                    with self.assertRaises(DriveError) as raised:
                        self.engine._verify_cloud_page({"files": [item]})
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self._cache_snapshot(), before)

    def test_version_validation_timeout_preserves_inventory_and_cache(self):
        item = self._seed_readable_cache()
        before = self._cache_snapshot()
        with mock.patch.object(self.engine, "_remote_source", True), \
             mock.patch.object(self.engine, "_digest", side_effect=DriveError("SHARE_TIMEOUT", "synthetic deadline", True)):
            with self.assertRaises(DriveError) as raised:
                self.engine._validated(item["file_id"], item["source_version"])
        self.assertEqual(raised.exception.code, "SHARE_TIMEOUT")
        self.assertEqual(self._cache_snapshot(), before)

    def test_parser_upgrade_retains_candidates_but_blocks_old_evidence_until_reparsed(self):
        item = self._seed_readable_cache()
        before = self._cache_snapshot()
        with self.engine._db() as db:
            db.execute("UPDATE meta SET value='synthetic-previous-parser' WHERE key='parse_binding'")
            db.execute("UPDATE files SET indexed_parse_binding='synthetic-previous-parser'")
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        status = self.engine.status()
        self.assertEqual(status["coverage"]["pending_reparse"], 3)
        self.assertEqual(status["coverage"]["current_indexed_files"], 0)
        self.assertEqual(status["coverage"]["parse_generation"], 2)
        self.assertFalse(status["index_status"]["queryable"])
        self.assertFalse(status["coverage"]["all_present_files_extracted_completely"])
        self.assertEqual(self._cache_snapshot()["segments"], before["segments"])
        candidates = self.engine.search("cached")["files"]
        self.assertEqual(len(candidates), 3)
        self.assertTrue(all(candidate["pending_reparse"] and not candidate["extraction_complete"] for candidate in candidates))
        for method in ("inspect", "read", "export"):
            params = {"file_id": item["file_id"]}
            if method != "inspect":
                params["expected_version"] = item["source_version"]
            result = self.engine.call(method, params)
            self.assertEqual(result["error"]["code"], "EXTRACTION_UPDATE_REQUIRED")
            self.assertNotIn("segments", result)
            self.assertNotIn("artifact_path", result)
        self._seed_readable_cache()
        status = self.engine.status()
        self.assertEqual(status["coverage"]["pending_reparse"], 0)
        self.assertEqual(status["coverage"]["current_indexed_files"], 3)
        self.assertTrue(self.engine.read(item["file_id"], expected_version=item["source_version"])["ok"])

    def test_query_code_or_alias_change_does_not_invalidate_extraction(self):
        self._seed_readable_cache()
        before = self._cache_snapshot()
        original_binding = self.engine._parse_binding
        self.engine.close()
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["aliases"] = {"amount": ["cost"]}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        read_bytes = Path.read_bytes

        def query_only_change(path):
            value = read_bytes(path)
            return value + b"\n# synthetic query-only change\n" if path.name == "engine.py" else value

        with mock.patch.object(Path, "read_bytes", query_only_change):
            self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine._parse_binding, original_binding)
        self.assertEqual(self.engine.status()["coverage"]["pending_reparse"], 0)
        self.assertEqual(self._cache_snapshot()["segments"], before["segments"])

    def test_failed_parser_upgrade_preserves_old_candidates_without_serving_them(self):
        self._seed_readable_cache()
        before = self._cache_snapshot()
        with self.engine._db() as db:
            db.execute("UPDATE meta SET value='synthetic-previous-parser' WHERE key='parse_binding'")
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        with mock.patch.object(self.engine, "_extract", side_effect=DriveError("STATE_IO_ERROR", "synthetic output failure")):
            with self.assertRaises(DriveError):
                self.engine.scan_once()
        self.assertEqual(self._cache_snapshot()["segments"], before["segments"])
        self.assertEqual(self.engine.status()["coverage"]["pending_reparse"], 3)
        self.assertFalse(self.engine.status()["index_status"]["queryable"])

    def test_transient_reparse_errors_preserve_candidates_until_retry(self):
        item = self._seed_readable_cache()
        before = self._cache_snapshot()
        with self.engine._db() as db:
            db.execute("UPDATE meta SET value='synthetic-previous-parser' WHERE key='parse_binding'")
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        for code in ("SHARE_TIMEOUT", "GRAPH_TIMEOUT", "GRAPH_THROTTLED", "GRAPH_UNAVAILABLE"):
            with self.subTest(code=code), mock.patch.object(self.engine, "_extract", side_effect=DriveError(code, "synthetic bounded interruption", True)):
                with self.assertRaises(DriveError) as raised:
                    self.engine.scan_once()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self._cache_snapshot()["segments"], before["segments"])
                self.assertEqual(self.engine.status()["coverage"]["pending_reparse"], 3)
                self.assertIsNone(self.engine.status()["runtime_error"])
                self.assertEqual(self.engine.call("read", {"file_id": item["file_id"]})["error"]["code"], "EXTRACTION_UPDATE_REQUIRED")
        self._seed_readable_cache()
        self.assertEqual(self.engine.status()["coverage"]["pending_reparse"], 0)

    def test_changed_inventory_limit_clears_obsolete_upgrade_flag(self):
        item = self._seed_readable_cache()
        self.engine.close()
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["limits"] = {"max_file_bytes": 1}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine.status()["coverage"]["pending_reparse"], 3)
        self.engine.scan_once()
        coverage = self.engine.status()["coverage"]
        self.assertEqual(coverage["pending_reparse"], 0)
        self.assertEqual(coverage["states"], {"oversized": 3})
        self.assertEqual(self.engine.call("read", {"file_id": item["file_id"]})["error"]["code"], "FILE_TOO_LARGE")
        self.assertTrue(all(not row["pending_reparse"] and row["indexed_parse_binding"] is None for row in self.engine.list()["files"]))

    def test_real_change_or_access_loss_still_invalidates_cached_evidence(self):
        for code, is_share in (("SHARE_SOURCE_CHANGED", True), ("SHARE_UNAVAILABLE", True),
                               ("GRAPH_SOURCE_CHANGED", False), ("GRAPH_ACCESS_DENIED", False)):
            with self.subTest(code=code):
                item = self._seed_readable_cache()
                before = self._cache_snapshot()
                source = SimpleNamespace(verify_origin=mock.Mock(side_effect=DriveError(code, "synthetic authoritative failure", True)))
                with mock.patch.object(self.engine, "_remote_source", True), \
                     mock.patch.object(self.engine, "_share_binding", {"synthetic": True} if is_share else None), \
                     mock.patch.object(self.engine, "_source", source):
                    with self.assertRaises(DriveError):
                        self.engine._verify_cloud_page({"files": [item]})
                after = self._cache_snapshot()
                self.assertEqual(after["files"][0][1], "stale")
                self.assertGreater(after["generation"], before["generation"])
                self.assertEqual(len(after["segments"]), len(before["segments"]) - 1)
                self.assertFalse(after["coverage"]["inventory_complete"])


class StatusAndLifecycleTests(unittest.TestCase):
    def test_runtime_classifier_does_not_label_source_guards_as_runtime(self):
        for code in ("UNSAFE_STATE", "UNSAFE_STATE_PERMISSIONS", "STATE_PATH_TOO_LONG", "STATE_IO_ERROR", "RUNTIME_PARSER_START_FAILED"):
            self.assertTrue(is_runtime_error(code))
        for code in ("UNSAFE_FILE", "SOURCE_CHANGED", "PARSE_FAILED", "SHARE_ACCESS_DENIED"):
            self.assertFalse(is_runtime_error(code))

    def test_pagination_rejects_known_running_scan_before_reading_pages(self):
        bridge = mock.Mock()
        bridge.call.return_value = {"ok": True, "current_job_id": "synthetic-job"}
        result = collect(bridge, "list", {})
        self.assertEqual(result["error"]["code"], "INDEX_STILL_RUNNING")
        self.assertEqual(result["pages_discarded"], 0)
        bridge.call.assert_called_once_with("status", {})

    def test_share_pagination_caps_pages_without_changing_requested_population(self):
        for requested, expected in ((None, 8), (100, 8), (3, 3)):
            with self.subTest(requested=requested):
                bridge = mock.Mock()
                bridge.call.side_effect = [
                    {"ok": True, "provider": "windows_share", "current_job_id": None},
                    {"ok": True, "files": [], "next_cursor": "next"},
                    {"ok": True, "files": [], "next_cursor": None},
                ]
                params = {"prefix": "approved/scope"}
                if requested is not None:
                    params["limit"] = requested
                original = dict(params)
                result = collect(bridge, "list", params)
                self.assertTrue(result["pagination_complete"])
                self.assertEqual(params, original)
                self.assertEqual(bridge.call.call_args_list[1], mock.call("list", {"prefix": "approved/scope", "limit": expected}))
                self.assertEqual(bridge.call.call_args_list[2], mock.call("list", {"prefix": "approved/scope", "limit": expected, "cursor": "next"}))

    def test_share_timeout_is_not_automatically_replayed(self):
        bridge = mock.Mock()
        bridge.call.side_effect = [
            {"ok": True, "provider": "windows_share", "current_job_id": None},
            {"ok": True, "files": [{"file_id": "old-page"}], "next_cursor": "next"},
            {"ok": False, "error": {"code": "SHARE_TIMEOUT"}},
        ]
        result = collect(bridge, "list", {})
        self.assertEqual(result["error"]["code"], "SHARE_TIMEOUT")
        self.assertEqual(result["pages_discarded"], 1)
        self.assertNotIn("pages", result)
        self.assertEqual(bridge.call.call_count, 3)

    def test_uncertain_unavailability_is_not_treated_as_transient_share_timeout(self):
        self.assertTrue(is_transient_source_error("SHARE_TIMEOUT"))
        for code in ("SHARE_UNAVAILABLE", "SHARE_SOURCE_CHANGED", "GRAPH_ACCESS_DENIED", "GRAPH_AUTH_REQUIRED", "GRAPH_SCOPE_CHANGED"):
            self.assertFalse(is_transient_source_error(code))

    def test_wait_ready_does_not_accept_zero_readable_files(self):
        bridge = mock.Mock()
        bridge.call.return_value = {"ok": True, "current_job_id": None,
                                    "coverage": {"inventory_complete": True, "present_files": 149, "indexed_files": 0},
                                    "index_status": {"state": "blocked"}}
        result = wait_ready(bridge, 0)
        self.assertEqual(result["error"]["code"], "INDEX_NOT_READABLE")

    def test_generation_change_still_discards_partial_pages(self):
        bridge = mock.Mock()
        bridge.call.side_effect = [
            {"ok": True, "current_job_id": None},
            {"ok": True, "files": [{"file_id": "synthetic"}], "next_cursor": "old"},
            {"ok": False, "error": {"code": "STALE_CURSOR"}},
        ]
        result = collect(bridge, "list", {})
        self.assertFalse(result["pagination_complete"])
        self.assertEqual(result["pages_discarded"], 1)
        self.assertNotIn("pages", result)

    def test_wait_ready_refreshes_status_after_job_completion(self):
        bridge = mock.Mock()
        ready = {"ok": True, "current_job_id": None, "coverage": {"inventory_complete": True, "present_files": 1, "indexed_files": 1}}
        bridge.call.side_effect = [{"ok": True, "current_job_id": "job"}, {"ok": True, "job": {"status": "complete"}}, ready]
        self.assertEqual(wait_ready(bridge, 0), ready)

    def test_scan_grace_is_bounded_and_does_not_disable_normal_idle(self):
        broker = Broker.__new__(Broker)
        broker.connections = set()
        broker.last_empty = 100
        broker.config = {"idle_seconds": 60, "scan_grace_seconds": 600}
        broker.engine = SimpleNamespace(_pending_job="job")
        self.assertFalse(broker._idle_shutdown_due(161))
        self.assertFalse(broker._idle_shutdown_due(759))
        self.assertTrue(broker._idle_shutdown_due(760))
        broker.engine._pending_job = None
        self.assertTrue(broker._idle_shutdown_due(161))
        broker.connections.add("connected-client")
        self.assertFalse(broker._idle_shutdown_due(5000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
