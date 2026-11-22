"""Synthetic routing boundaries; no real source, account or company documents."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from team_drive import catalog, cli, onboarding
from team_drive.bridge import Bridge
from team_drive.errors import DriveError
from team_drive.evidence import envelope


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-catalog-")
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state-base"
        self.configs = []
        for name in ("Library", "Finance"):
            source = self.root / name
            source.mkdir()
            plan = onboarding.make_plan("local", str(source), state_dir=str(self.state))
            onboarding.apply_plan(plan, confirmed=True)
            self.configs.append(Path(plan["config_path"]))

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_is_read_only_and_never_connects_sources(self):
        before = {p: p.read_bytes() for p in self.configs}
        with mock.patch("team_drive.fs_access.SourceAccess", side_effect=AssertionError("No source access")):
            result = catalog.catalog(str(self.state))
        self.assertEqual(len(result["profiles"]), 2)
        self.assertFalse(result["sources_checked"])
        self.assertFalse(result["files_created"])
        self.assertEqual(len({p["profile_id"] for p in result["profiles"]}), 2)
        for p in result["profiles"]:
            self.assertEqual(len(p["config_fingerprint"]), 64)
            self.assertEqual(p["source_availability"], "not_checked")
            self.assertFalse(Path(p["state_path"]).exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.configs})

    def test_select_is_explicit_and_rejects_changed_configuration(self):
        item = catalog.catalog(str(self.state))["profiles"][0]
        selected = catalog.select(item["profile_id"], str(self.state), item["config_fingerprint"])
        self.assertEqual(selected["selected"], item)
        path = Path(item["config_path"])
        config = json.loads(path.read_text())
        config["poll_seconds"] = 95
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(DriveError) as error:
            catalog.select(item["profile_id"], str(self.state), item["config_fingerprint"])
        self.assertEqual(error.exception.code, "CONFIG_CHANGED")

    def test_no_implicit_fallback_or_path_selector(self):
        for selector in (None, "../Finance", "unknown-profile"):
            with self.subTest(selector=selector), self.assertRaises(DriveError):
                catalog.select(selector, str(self.state))

    def test_incomplete_discovery_cannot_prove_absence(self):
        with mock.patch.object(catalog, "catalog", return_value={"profiles": [], "warnings": [], "truncated": True}):
            with self.assertRaises(DriveError) as error:
                catalog.select("unknown-profile")
        self.assertEqual(error.exception.code, "CATALOG_INCOMPLETE")

    def test_bridge_locks_selected_config_before_connecting(self):
        item = catalog.catalog(str(self.state))["profiles"][0]
        with mock.patch.object(Bridge, "connect") as connect:
            bridge = Bridge(item["config_path"], expected_config_fingerprint=item["config_fingerprint"])
            self.assertEqual(bridge.config["config_fingerprint"], item["config_fingerprint"])
            connect.assert_called_once()
        with mock.patch.object(Bridge, "connect") as connect:
            with self.assertRaises(DriveError) as error:
                Bridge(item["config_path"], expected_config_fingerprint="0" * 64)
            self.assertEqual(error.exception.code, "CONFIG_CHANGED")
            connect.assert_not_called()

    def test_profile_to_cli_reader_and_envelope_end_to_end(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "df-tool.py"
        for path in self.configs:
            config = json.loads(path.read_text())
            config["service"] = {"idle_seconds": 1}
            config["poll_seconds"] = 3600
            path.write_text(json.dumps(config), encoding="utf-8")
        selected = next(item for item in catalog.catalog(str(self.state))["profiles"]
                        if Path(item["source"]).name == "Finance")
        other = next(item for item in catalog.catalog(str(self.state))["profiles"]
                     if item["profile_id"] != selected["profile_id"])
        (Path(selected["source"]) / "cost.txt").write_text("SIMULATED finance amount 100 CNY.\n", encoding="utf-8")
        (Path(other["source"]) / "other.txt").write_text("SIMULATED other scope.\n", encoding="utf-8")
        common = ["--profile-id", selected["profile_id"], "--state-dir", str(self.state),
                  "--config-fingerprint", selected["config_fingerprint"], "--evidence-envelope"]

        def invoke(method, extra=()):
            process = subprocess.run([sys.executable, "-X", "utf8", "-B", str(script), method, *common, *extra],
                                     text=True, encoding="utf-8", capture_output=True, timeout=75, env=dict(os.environ))
            return process, json.loads(process.stdout)

        try:
            process, listed = invoke("list", ["--paginate", "--wait-ready", "30"])
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            self.assertEqual(listed["source"]["profile_id"], selected["profile_id"])
            self.assertTrue(listed["technical"]["inventory_complete"])
            self.assertTrue(listed["technical"]["pagination_complete"])
            files = [item for page in listed["result"]["pages"] for item in page["files"]]
            self.assertEqual([item["path"] for item in files], ["cost.txt"])
            self.assertFalse(Path(other["state_path"]).exists())
            process, read = invoke("read", ["--params", json.dumps({"file_id": files[0]["file_id"],
                                                "expected_version": files[0]["source_version"]}), "--wait-ready", "30"])
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            self.assertEqual(read["result"]["source_version"], files[0]["source_version"])
            self.assertIn("100 CNY", read["result"]["segments"][0]["text"])
            self.assertEqual(read["business_completeness"], "not_established")
            path = Path(selected["config_path"])
            config = json.loads(path.read_text())
            config["root"] = other["source"]
            path.write_text(json.dumps(config), encoding="utf-8")
            process, failed = invoke("status")
            self.assertEqual(process.returncode, 1)
            self.assertEqual(failed["result"]["error"]["code"], "CONFIG_CHANGED")
            self.assertIsNone(failed["source"]["config_id"])
            self.assertFalse(Path(other["state_path"]).exists())
        finally:
            # The real broker exits after its configured one-second idle lease.
            time.sleep(2)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = {"config_id": "synthetic-config", "config": {"provider": "local", "root": "/synthetic/library"}}

    def test_unknown_checks_stay_unknown_and_errors_are_preserved(self):
        result = {"ok": False, "error": {"code": "SHARE_TIMEOUT", "retryable": True},
                  "pagination_complete": False, "pages_discarded": 3}
        wrapped = envelope(result, "search", self.config, {"prefix": "Finance"})
        self.assertFalse(wrapped["ok"])
        self.assertEqual(wrapped["result"], result)
        self.assertIsNone(wrapped["technical"]["extraction_complete"])
        self.assertIsNone(wrapped["technical"]["inventory_complete"])
        self.assertEqual(wrapped["source"]["scope"]["prefix"], "Finance")

    def test_wait_failure_keeps_full_job_observation_inside_envelope(self):
        for state in ("partial", "failed", "cancelled", "running"):
            with self.subTest(state=state):
                observed = {"ok": True, "job": {"id": "synthetic-job", "status": state,
                            "error_code": "SHARE_TIMEOUT", "error_detail": {
                                "code": "SHARE_TIMEOUT", "stage": "enumerating",
                                "operation": "directory_enumeration", "winerror": 121,
                                "frames": [{"function": "synthetic_scan"}]},
                            "scan_progress": {"pending_directories": 3}},
                            "coverage": {"inventory_complete": False},
                            "index_status": {"state": state}}
                bridge = mock.Mock()
                bridge.call.side_effect = [{"ok": True, "current_job_id": "synthetic-job"}, observed]
                result = cli.wait_ready(bridge, 0)
                wrapped = envelope(result, "read", self.config)
                self.assertFalse(wrapped["ok"])
                self.assertEqual(wrapped["method"], "read")
                self.assertEqual(wrapped["result"]["readiness"], {"method": "job_status", "result": observed})
                self.assertFalse(wrapped["technical"]["inventory_complete"])

    def test_wait_failure_keeps_native_status_and_primary_error(self):
        for observed in (
            {"ok": False, "error": {"code": "UNSAFE_STATE", "retryable": False, "next_action": "Repair runtime"}},
            {"ok": True, "index_status": {"state": "blocked", "latest_error_code": "UNSAFE_STATE"}},
            {"ok": True, "coverage": {"inventory_complete": True, "present_files": 1,
                                      "current_indexed_files": 0, "pending_reparse": 1}},
        ):
            with self.subTest(observed=observed):
                bridge = mock.Mock()
                bridge.call.return_value = observed
                result = cli.wait_ready(bridge, 0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["readiness"], {"method": "status", "result": observed})
                if "error" in observed:
                    self.assertEqual(result["error"], observed["error"])

    def test_pagination_precheck_failure_identifies_unexecuted_search(self):
        observed = {"ok": False, "error": {"code": "UNSAFE_STATE", "retryable": False}}
        bridge = mock.Mock()
        bridge.call.return_value = observed
        result = cli.collect(bridge, "search", {"query": "synthetic"})
        self.assertEqual(result["error"], observed["error"])
        self.assertEqual(result["readiness"], {"method": "status", "result": observed})
        bridge.call.assert_called_once_with("status", {})

    def test_successful_selection_does_not_claim_business_completeness(self):
        result = {"ok": True, "has_more": False, "extraction_complete": True,
                  "segments": [{"locator": {"row": 2}, "text": "synthetic"}],
                  "format_warnings": ["Formula caches are unverified"], "source_version": "synthetic-version"}
        original = copy.deepcopy(result)
        wrapped = envelope(result, "read", self.config, {"file_id": "example", "expected_version": "synthetic-version"})
        self.assertTrue(wrapped["technical"]["pagination_complete"])
        self.assertTrue(wrapped["technical"]["extraction_complete"])
        self.assertIsNone(wrapped["technical"]["inventory_complete"])
        self.assertEqual(wrapped["business_completeness"], "not_established")
        wrapped["result"]["segments"].clear()
        self.assertEqual(result, original)

    def test_partial_page_and_pending_reparse_remain_visible(self):
        result = {"ok": True, "pagination_complete": False,
                  "pages": [{"ok": True, "coverage": {"inventory_complete": False, "pending_reparse": 2},
                             "files": [{"pending_reparse": True, "extraction_current": False}]}]}
        wrapped = envelope(result, "list", self.config)
        self.assertFalse(wrapped["technical"]["pagination_complete"])
        self.assertFalse(wrapped["technical"]["inventory_complete"])
        self.assertEqual(wrapped["result"], result)
        self.assertIsNone(wrapped["technical"]["extraction_complete"])

    def test_graph_scope_never_exposes_credentials(self):
        config = {"config_id": "synthetic-config", "config": {"provider": "microsoft_graph",
                  "graph": {"drive_id": "demo-drive", "folder_id": "demo-folder", "auth_cache": "private-cache",
                            "auth": {"client_id": "private-client"}}}}
        wrapped = envelope({"ok": True}, "status", config)
        self.assertEqual(wrapped["source"]["scope"]["source"], {"drive_id": "demo-drive", "folder_id": "demo-folder"})
        self.assertNotIn("private-cache", json.dumps(wrapped))
        self.assertNotIn("private-client", json.dumps(wrapped))

    def test_catalog_identity_is_distinct_and_bound_to_connection(self):
        config = dict(self.config, config_fingerprint="synthetic-fingerprint", path="/synthetic/config.json")
        selected = {"profile_id": "example", "catalog_source_id": "catalog-identity",
                    "config_fingerprint": "synthetic-fingerprint", "config_path": "/synthetic/config.json"}
        wrapped = envelope({"ok": True, "source_id": "observed-identity"}, "status", config, selected=selected)
        self.assertEqual(wrapped["source"]["source_id"], "observed-identity")
        self.assertEqual(wrapped["source"]["catalog_source_id"], "catalog-identity")
        with self.assertRaises(DriveError):
            envelope({"ok": True}, "read", config, selected=dict(selected, config_path="/other/config.json"))
        with self.assertRaises(DriveError):
            envelope({"ok": True}, "read", config, selected=dict(selected, config_fingerprint="different"))


if __name__ == "__main__":
    unittest.main()
