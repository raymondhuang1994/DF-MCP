"""Pure configuration and mocked bridge protocol checks; no IPC listener needed."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from team_drive.bridge import Bridge
from team_drive.engine import DEFAULT_LIMITS, Engine
from team_drive.errors import DriveError
from team_drive.service_config import settings


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-service-config-")
        self.base = Path(self.temp.name).resolve()
        (self.base / "source").mkdir()
        self.path = self.base / "config.json"
        self.config = {"root": "source", "state_dir": "state"}
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def test_equivalent_paths_defaults_key_order_and_legacy_slots_share_binding(self):
        minimal = settings(self.path)
        self.config = {"aliases": {}, "limits": dict(DEFAULT_LIMITS), "poll_seconds": 30.0,
                       "service": {"max_clients": 64, "idle_seconds": 60.0}, "provider": "local",
                       "state_dir": str(self.base / "state"), "root": str(self.base / "source"),
                       "state_pool": {"max_slots": 17}}
        self.write()
        explicit = settings(self.path)
        self.assertEqual(explicit["config_id"], minimal["config_id"])
        self.assertEqual(explicit["config"], minimal["config"])
        self.assertNotIn("state_pool", explicit["config"])

    def test_actual_content_settings_change_configuration_binding(self):
        original = settings(self.path)["config_id"]
        for key, value in (("poll_seconds", 60), ("limits", {"max_segments": 123}), ("aliases", {"fee": ["cost"]})):
            with self.subTest(setting=key):
                self.config = {"root": "source", "state_dir": "state", key: value}
                self.write()
                self.assertNotEqual(settings(self.path)["config_id"], original)

    def test_retrieval_and_diagnostics_defaults_and_complete_local_configuration(self):
        minimal = settings(self.path)["config"]
        self.assertFalse(minimal["retrieval"]["semantic"]["enabled"])
        self.assertFalse(minimal["retrieval"]["rerank"]["enabled"])
        self.assertEqual(minimal["retrieval"]["semantic"]["per_file_budget_seconds"], 20)
        self.assertEqual(minimal["diagnostics"], {"enabled": True, "max_events": 1000, "retention_days": 7})
        self.config.update({
            "retrieval": {"candidate_limit": 1000, "rrf_k": 1000,
                "semantic": {"enabled": True, "endpoint": "http://localhost:11434/v1/embeddings",
                    "model": "synthetic-embedding", "model_revision": "synthetic-revision",
                    "timeout_seconds": 20, "batch_size": 64, "max_segments": 100000,
                    "max_text_chars": 32000, "minimum_similarity": -1, "per_file_budget_seconds": 20},
                "rerank": {"enabled": True, "endpoint": "https://192.168.1.2:8443/rerank",
                    "model": "synthetic-rerank", "timeout_seconds": 0.1, "max_candidates": 100}},
            "diagnostics": {"enabled": False, "max_events": 10000, "retention_days": 90}})
        self.write()
        with mock.patch("team_drive.retrieval._post", side_effect=AssertionError("Configuration cannot call models")):
            configured = settings(self.path)["config"]
        self.assertEqual(configured["retrieval"]["semantic"]["endpoint"], "http://127.0.0.1:11434/v1/embeddings")
        self.assertEqual(configured["retrieval"]["rerank"]["model"], "synthetic-rerank")
        self.assertEqual(configured["diagnostics"]["retention_days"], 90)
        self.assertFalse((self.base / "state").exists())

    def test_only_literal_local_model_endpoints_are_accepted(self):
        allowed = ["http://127.0.0.1:11434/embed", "http://[::1]:1234/embed", "http://[0:0:0:0:0:0:0:1]/embed",
                   "https://10.1.2.3/embed", "http://172.16.0.1/embed", "http://172.31.255.254/embed",
                   "http://192.168.1.2/embed", "http://[fc00::1]/embed", "http://[fdff::1]/embed",
                   "http://[fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/embed"]
        for endpoint in allowed:
            with self.subTest(endpoint=endpoint):
                self.config = {"root": "source", "state_dir": "state", "retrieval": {
                    "semantic": {"enabled": True, "endpoint": endpoint, "model": "synthetic"}}}
                self.write()
                self.assertEqual(settings(self.path)["config"]["retrieval"]["semantic"]["endpoint"], endpoint)
                self.assertFalse((self.base / "state").exists())

    def test_retrieval_and_diagnostics_invalid_options_reject_before_state_creation(self):
        bad_endpoints = ["https://api.example.com/v1/embeddings", "http://8.8.8.8/embed",
            "http://169.254.169.254/embed", "http://172.32.0.1/embed", "http://[fe80::1]/embed",
            "http://model.internal/embed", "http://user:secret@127.0.0.1/embed",
            "http://@127.0.0.1/embed", "http://127.0.0.1/embed?key=secret", "http://127.0.0.1/embed#fragment",
            "http://127.0.0.1:0/embed", "http://127.0.0.1:65536/embed", "http://127.0.0.1:/embed", "http://[::1]:/embed",
            "http://2130706433/embed", "http://127.1/embed", "http://0x7f000001/embed"]
        cases = [{"retrieval": {kind: {"endpoint": endpoint}}}
                 for endpoint in bad_endpoints for kind in ("semantic", "rerank")]
        cases += [{"retrieval": None}, {"retrieval": []}, {"retrieval": {"enabled": True}},
                  {"diagnostics": None}, {"diagnostics": []}, {"diagnostics": {"unknown": 1}}]
        cases += [{"diagnostics": {key: value}} for key in ("max_events", "retention_days")
                  for value in (-1, 0, True, 1.5)]
        cases += [{"diagnostics": {"max_events": 10001}}, {"diagnostics": {"retention_days": 91}},
                  {"diagnostics": {"enabled": "true"}}]
        cases += [{"retrieval": {"semantic": {key: value}}} for key, value in (
            ("enabled", "true"), ("enabled", True), ("timeout_seconds", 0.09), ("timeout_seconds", 20.01),
            ("timeout_seconds", True), ("per_file_budget_seconds", 0), ("per_file_budget_seconds", 20.01),
            ("per_file_budget_seconds", True), ("batch_size", 65), ("max_segments", 100001),
            ("max_text_chars", 32001), ("minimum_similarity", -1.01), ("minimum_similarity", 1.01))]
        cases += [{"retrieval": {"rerank": {key: value}}} for key, value in (
            ("enabled", True), ("timeout_seconds", 0), ("timeout_seconds", 21),
            ("max_candidates", 0), ("max_candidates", 101), ("model_revision", "not-supported"))]
        cases += [{"retrieval": {key: value}} for key in ("candidate_limit", "rrf_k") for value in (0, 1001, True)]
        for extra in cases:
            with self.subTest(extra=extra):
                self.config = dict(root="source", state_dir="state", **extra)
                self.write()
                with self.assertRaises(DriveError) as caught:
                    settings(self.path)
                self.assertEqual(caught.exception.code, "INVALID_CONFIG")
                self.assertFalse((self.base / "state").exists())

    def test_model_revision_and_diagnostics_change_binding(self):
        original = settings(self.path)["config_id"]
        for extra in ({"retrieval": {"semantic": {"model_revision": "synthetic-revision"}}},
                      {"diagnostics": {"enabled": False}}, {"diagnostics": {"retention_days": 8}}):
            with self.subTest(extra=extra):
                self.config = dict(root="source", state_dir="state", **extra)
                self.write()
                self.assertNotEqual(settings(self.path)["config_id"], original)

    def test_code_binding_changes_only_when_selected_code_contents_change(self):
        code = self.base / "synthetic-code.py"
        code.write_text("VALUE = 1\n", encoding="utf-8")
        with mock.patch("team_drive.service_config.Path.glob", return_value=[code]):
            before = settings(self.path)
            code.write_text("VALUE = 2\n", encoding="utf-8")
            after = settings(self.path)
        self.assertEqual(before["config_id"], after["config_id"])
        self.assertNotEqual(before["code_id"], after["code_id"])

    def test_finite_service_and_poll_bounds_reject_before_state_creation(self):
        cases = [{"service": {"idle_seconds": value}} for value in (0, 3601, True, float("nan"), float("inf"), 10 ** 1000)]
        cases += [{"service": {"max_clients": value}} for value in (0, 257, True, 1.5)]
        cases += [{"poll_seconds": value} for value in (0, 86401, float("nan"), float("inf"), 10 ** 1000)]
        for extra in cases:
            with self.subTest(extra=extra):
                self.config = dict(root="source", state_dir="state", **extra)
                self.write()
                with self.assertRaises(DriveError) as caught:
                    settings(self.path)
                self.assertEqual(caught.exception.code, "INVALID_CONFIG")
                self.assertFalse((self.base / "state").exists())

    def test_codex_and_custom_home_state_are_rejected(self):
        for state in (self.base / ".codex" / "state", self.base / ".CoDeX" / "state"):
            self.config["state_dir"] = str(state)
            self.write()
            with self.assertRaises(DriveError) as caught:
                settings(self.path)
            self.assertEqual(caught.exception.code, "UNSAFE_STATE")
            self.assertFalse(state.parent.exists())
        custom = self.base / "custom-home"
        self.config["state_dir"] = str(custom / "state")
        self.write()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
            with self.assertRaises(DriveError):
                settings(self.path)
        self.assertFalse(custom.exists())

    def test_configuration_read_is_bounded(self):
        self.path.write_bytes(b" " * 262145)
        with self.assertRaises(DriveError) as caught:
            settings(self.path)
        self.assertEqual(caught.exception.code, "INVALID_CONFIG")

    def test_engine_uses_frozen_snapshot_even_if_configuration_changes_or_disappears(self):
        frozen = settings(self.path)
        (self.base / "source" / "original.txt").write_text("ORIGINAL", encoding="utf-8")
        other = self.base / "different-source"
        other.mkdir()
        (other / "unexpected.txt").write_text("UNEXPECTED", encoding="utf-8")
        self.config["root"] = str(other)
        self.write()
        self.path.unlink()
        engine = Engine(str(self.path), autostart=False, config_snapshot=frozen["config"])
        try:
            frozen["config"]["aliases"]["injected"] = ["value"]
            self.assertNotIn("injected", engine.aliases)
            engine.scan_once()
            self.assertEqual([file["path"] for file in engine.list()["files"]], ["original.txt"])
        finally:
            engine.close()


class BridgeProtocolTests(unittest.TestCase):
    def bridge(self):
        bridge = Bridge.__new__(Bridge)
        bridge.connection = mock.Mock()
        bridge.counter = 0
        bridge.session_id = "synthetic-session"
        bridge.instance_id = "synthetic-instance"
        return bridge

    def test_disconnected_request_is_never_replayed_and_next_call_reconnects(self):
        bridge = self.bridge()
        old = bridge.connection
        def reconnect():
            bridge.connection = mock.Mock()
        with mock.patch("team_drive.bridge.send") as send, \
             mock.patch("team_drive.bridge.receive", side_effect=[EOFError(), {"id": 2, "result": {"ok": True}}]), \
             mock.patch.object(bridge, "connect", side_effect=reconnect) as connect:
            first = bridge.call("export", {"file_id": "id", "expected_version": "version"})
            self.assertEqual(first["error"]["code"], "BACKEND_UNAVAILABLE")
            self.assertEqual(send.call_count, 1)
            connect.assert_not_called()
            old.close.assert_called_once()
            self.assertTrue(bridge.call("status", {})["ok"])
            self.assertEqual(send.call_count, 2)
            connect.assert_called_once()
        bridge.close()

    def test_timeout_and_mismatched_response_fail_without_replay(self):
        for response, expected in ((TimeoutError(), "REQUEST_TIMEOUT"), ({"id": 999, "result": {"ok": True}}, "BACKEND_UNAVAILABLE")):
            with self.subTest(expected=expected):
                bridge = self.bridge()
                with mock.patch("team_drive.bridge.send") as send, mock.patch("team_drive.bridge.receive", side_effect=[response]):
                    result = bridge.call("refresh", {})
                self.assertEqual(result["error"]["code"], expected)
                self.assertEqual(send.call_count, 1)
                self.assertIsNone(bridge.connection)


if __name__ == "__main__":
    unittest.main()
