"""Acceptance probes retain useful failures without exporting source data."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from team_drive import __version__
from team_drive.acceptance import _job, main, report
from team_drive.engine import Engine
from team_drive.errors import DriveError


class SyntheticBridge:
    session_id = "fixture-session"

    def __init__(self, ready=True, job=None, replies=None):
        self.calls = []
        self.job = job or {"id": "first-job", "status": "complete", "discovered": 1, "processed": 1}
        self.replies = replies or {}
        self.status = {"ok": True, "service_version": __version__, "provider": "windows_share", "index_generation": 1,
                       "instance_id": "instance-secret", "config_id": "config-secret", "source_id": "source-secret",
                       "current_job_id": None, "coverage": {"inventory_complete": ready, "indexed_files": 1 if ready else 0,
                       "present_files": 1 if ready else 0, "states": {}},
                       "state_storage": {"persistent": True, "mode": "shared", "index_copies": 1,
                                         "path": "PRIVATE-PATH"}}

    def call(self, method, params):
        self.calls.append((method, params))
        if method in self.replies:
            reply = self.replies[method]
            return reply(params) if callable(reply) else reply
        if method == "status":
            return self.status
        if method == "job_status":
            return {"ok": True, "job": self.job}
        if method == "list":
            return {"ok": True, "files": [{"file_id": "fixture-file", "path": "PRIVATE-FILENAME.docx",
                    "status": "ready", "format": "docx", "size_bytes": 123}], "next_cursor": None}
        if method == "inspect":
            return {"ok": True, "source_version": "private-version", "file": {"path": "PRIVATE-FILENAME.docx"}}
        if method == "read":
            return {"ok": True, "file_id": "fixture-file", "source_version": "private-version",
                    "origin": {"relative_path": "PRIVATE-FILENAME.docx"},
                    "segments": [{"text": "PRIVATE-CONTENT", "locator": {"kind": "paragraph", "paragraph": 1,
                    "heading": "PRIVATE-HEADING"}}], "format_warnings": ["PRIVATE-WARNING"],
                    "whole_file_in_response": True, "extraction_complete": True}
        if method == "search":
            return {"ok": True, "returned": 1, "index_generation": 1, "coverage": {"inventory_complete": True},
                    "files": [{"path": "PRIVATE-FILENAME.docx", "snippet": "PRIVATE-CONTENT"}]}
        raise AssertionError(method)


class AcceptanceTests(unittest.TestCase):
    def test_one_connection_checks_version_pinned_sample_and_query_without_data(self):
        bridge = SyntheticBridge()
        result = report(bridge, 0, "PRIVATE-QUERY")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sample"]["technical_result"], "passed")
        self.assertTrue(result["sample"]["same_source_version"])
        self.assertTrue(result["sample"]["local_human_review_required"])
        self.assertFalse(result["sample"]["locator_accuracy_verified"])
        self.assertEqual(result["search"]["performance_basis"], "nonempty_index_and_results")
        self.assertTrue(result["search"]["same_index_generation"])
        self.assertTrue(all(result["reuse"].values()))
        serialized = json.dumps(result)
        for private in ("PRIVATE-", "private-version", "fixture-file", "instance-secret", "config-secret", "source-secret"):
            self.assertNotIn(private, serialized)
        reads = [params for method, params in bridge.calls if method == "read"]
        self.assertEqual(reads, [{"file_id": "fixture-file", "expected_version": "private-version", "limit": 3}])
        self.assertTrue(all(method not in {"refresh", "cancel", "export", "evidence_bundle"} for method, params in bridge.calls))

    def test_failed_first_scan_preserves_progress_and_never_calls_empty_search(self):
        job = {"id": "first-job", "status": "failed", "discovered": 0, "processed": 0,
               "started_at": 10, "finished_at": 130.07, "error_code": "SHARE_TIMEOUT",
               "error_detail": {"code": "SHARE_TIMEOUT", "message": "PRIVATE-PATH", "stage": "enumerating",
                                "operation": "item_observation", "winerror": None,
                                "frames": [{"module": "PRIVATE-PATH", "function": "PRIVATE-FUNCTION", "line": 1}]},
               "scan_progress": {"stage": "failed", "elapsed_seconds": 120.07,
                                 "counters": {"entries_enumerated": 72, "pending_items": 40, "PRIVATE-PATH": 9},
                                 "operations": {"item_observation": {"calls": 40, "max_ms": 900}},
                                 "last_progress_at": "2026-11-02T00:00:00Z", "path": "PRIVATE-PATH"}}
        bridge = SyntheticBridge(False, job)
        result = report(bridge, 0, "PRIVATE-QUERY")
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "index_task_failed")
        self.assertEqual(result["tracked_job"]["scan_progress"]["counters"]["entries_enumerated"], 72)
        self.assertAlmostEqual(result["tracked_job"]["duration_seconds"], 120.07)
        self.assertFalse(result["sample"]["attempted"])
        self.assertFalse(result["search"]["attempted"])
        self.assertNotIn("PRIVATE-", json.dumps(result))
        self.assertEqual([method for method, params in bridge.calls], ["status", "job_status"])

    def test_wait_follows_original_job_and_reports_running_as_not_ready(self):
        bridge = SyntheticBridge(False, {"id": "first-job", "status": "running"})
        bridge.status["current_job_id"] = "first-job"
        clock = [0.0]
        with mock.patch("team_drive.acceptance.time.monotonic", side_effect=lambda: clock[0]), \
             mock.patch("team_drive.acceptance.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = report(bridge, 2)
        self.assertEqual(result["outcome"], "index_not_ready")
        job_params = [params for method, params in bridge.calls if method == "job_status"]
        self.assertEqual(job_params, [{"job_id": "first-job"}] * 3)
        self.assertFalse(result["sample"]["attempted"])

    def test_changed_version_and_failed_read_do_not_become_successful_evidence(self):
        for response in ({"ok": False, "error": {"code": "SOURCE_CHANGED", "message": "PRIVATE-PATH"}},
                         {"ok": True, "file_id": "fixture-file", "source_version": "different-version",
                          "segments": [{"text": "PRIVATE-CONTENT", "locator": {"kind": "line", "line_start": 1}}]}):
            bridge = SyntheticBridge(replies={"read": response})
            result = report(bridge, 0, "PRIVATE-QUERY")
            self.assertFalse(result["ok"])
            self.assertFalse(result["search"]["attempted"])
            self.assertNotIn("PRIVATE-", json.dumps(result))

    def test_empty_query_results_and_generation_change_are_not_performance_claims(self):
        for response in ({"ok": True, "returned": 0, "index_generation": 1, "coverage": {"inventory_complete": True}},
                         {"ok": True, "returned": 1, "index_generation": 1, "coverage": {"inventory_complete": False}}):
            result = report(SyntheticBridge(replies={"search": response}), 0, "query")
            self.assertEqual(result["search"]["performance_basis"], "not_representative")
            self.assertFalse(result["search"]["production_performance_verified"])
        index = [0]
        def changing(params):
            index[0] += 1
            return {"ok": True, "returned": 1, "index_generation": index[0], "coverage": {"inventory_complete": True}}
        result = report(SyntheticBridge(replies={"search": changing}), 0, "query")
        self.assertFalse(result["search"]["same_index_generation"])
        self.assertEqual(result["search"]["performance_basis"], "not_representative")

    def test_sample_discovery_budget_does_not_claim_no_files_exist(self):
        bridge = SyntheticBridge(replies={"list": {"ok": True, "files": [], "next_cursor": "next-private"}})
        result = report(bridge, 0)
        self.assertEqual(result["sample"]["reason"], "sample_page_budget")
        self.assertEqual(sum(method == "list" for method, params in bridge.calls), 10)
        self.assertFalse(result["ok"])

    def test_runtime_mismatch_does_not_test_the_wrong_service(self):
        bridge = SyntheticBridge()
        bridge.status["service_version"] = "0.0.1"
        result = report(bridge, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "service_version_mismatch")
        self.assertEqual(len(bridge.calls), 1)

    def test_reconnection_or_source_change_does_not_claim_index_reuse(self):
        bridge = SyntheticBridge()
        seen = [0]
        def changed_status(params):
            seen[0] += 1
            if seen[0] > 1:
                bridge.session_id = "changed-session"
                return dict(bridge.status, source_id="different-source")
            return bridge.status
        bridge.replies["status"] = changed_status
        result = report(bridge, 0)
        self.assertFalse(result["ok"])
        self.assertFalse(result["reuse"]["same_source"])
        self.assertFalse(result["reuse"]["same_connection"])

    def test_cli_requires_confirmation_and_redacts_errors_before_connection(self):
        for arguments, code in ((["--config", "PRIVATE-PATH"], "CONFIG_CONFIRMATION_REQUIRED"),
                                (["--config", "PRIVATE-PATH", "--confirmed-config", "--wait-ready", "nan"], "INVALID_ARGUMENTS"),
                                (["--config", "PRIVATE-PATH", "--confirmed-config", "--bad", "PRIVATE-ARG"], "INVALID_ARGUMENTS")):
            with mock.patch("team_drive.acceptance.Bridge") as bridge, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(arguments), 1)
            bridge.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], code)
            self.assertNotIn("PRIVATE-", output.getvalue())
        with mock.patch("team_drive.acceptance.Bridge", side_effect=DriveError("UNSAFE_STATE", "PRIVATE-PATH")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--config", "PRIVATE-PATH", "--confirmed-config"]), 1)
        self.assertNotIn("PRIVATE-", output.getvalue())

    def test_real_engine_reads_synthetic_file_without_changing_source(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            source = base / "source"
            source.mkdir()
            document = source / "PRIVATE-NAME.txt"
            document.write_text("SIMULATED PRIVATE-BODY budget 123\n", encoding="utf-8")
            config = base / "config.json"
            config.write_text(json.dumps({"root": str(source), "state_dir": str(base / "state")}), encoding="utf-8")
            engine = Engine(str(config), autostart=False)
            try:
                engine.scan_once()
                bridge = mock.Mock(session_id="fixture-session")
                def invoke(method, params):
                    response = engine.call(method, params, session_id="fixture-session")
                    if method == "status":
                        response.update(service_version=__version__, instance_id="fixture-instance", config_id="fixture-config")
                    return response
                bridge.call.side_effect = invoke
                result = report(bridge, 0, "budget")
            finally:
                engine.close()
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["sample"]["format"], "txt")
            self.assertEqual(document.read_text(encoding="utf-8"), "SIMULATED PRIVATE-BODY budget 123\n")
            self.assertNotIn("PRIVATE-", json.dumps(result))
            self.assertNotIn(str(base), json.dumps(result))


if __name__ == "__main__":
    unittest.main()
