"""Real engine/parser/storage with synthetic Graph HTTP responses, never a tenant."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest import mock

from team_drive.engine import Engine
from team_drive.errors import DriveError
from team_drive.service_config import settings
from test_graph_source import FakeGraph, item


class CloudEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-cloud-engine-")
        self.base = Path(self.temp.name).resolve()
        self.anchor = self.base / "anchor"
        self.anchor.mkdir()
        self.config_path = self.base / "config.json"
        self.config = {"provider": "microsoft_graph", "root": str(self.anchor),
            "state_dir": str(self.base / "state"), "graph": {
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "client_id": "00000000-0000-0000-0000-000000000002",
                "drive_id": "drive-1", "folder_id": "folder-1", "token_env": "DF_MCP_GRAPH_TEST_TOKEN"}}
        self.config_path.write_text(json.dumps(self.config))
        self.environment = mock.patch.dict(os.environ, {"DF_MCP_GRAPH_TEST_TOKEN": "synthetic-access-token"})
        self.environment.start()
        self.engine = Engine(str(self.config_path), autostart=False)
        self.graph = FakeGraph()
        self.engine._source._opener = self.graph
        self.content = b"category,amount,currency\ntravel,120,CNY\nmeals,80,CNY\n"
        self.graph.add("budget.csv", item("file-1", "budget.csv", content=self.content), self.content)

    def tearDown(self):
        self.engine.close()
        self.environment.stop()
        self.temp.cleanup()

    def indexed(self):
        result = self.engine.scan_once()
        self.assertEqual(result["job"]["status"], "complete")
        return self.engine.search("travel")["files"][0]

    def fail(self, path, status):
        self.graph.overrides[path] = urllib.error.HTTPError("https://graph.microsoft.com" + path,
            status, "PRIVATE_HTTP_ERROR", {}, io.BytesIO(b"PRIVATE_HTTP_BODY"))

    def test_cloud_source_round_trip_retains_precise_evidence_and_cloud_origin(self):
        file = self.indexed()
        status = self.engine.status()
        self.assertEqual(status["provider"], "microsoft_graph")
        self.assertTrue(status["network_calls"])
        self.assertTrue(status["capabilities"]["network_source"])
        self.assertEqual(file["origin"]["etag"], "v1")
        inspected = self.engine.inspect(file["file_id"])
        read = self.engine.read(file["file_id"], expected_version=inspected["source_version"])
        self.assertEqual(read["origin"]["source_version"], read["source_version"])
        self.assertIn("sharepoint.com", read["origin"]["web_url"])
        self.assertIn("120", json.dumps(read["segments"]))
        context = self.engine.context(file["file_id"], file["source_version"], 1)
        self.assertEqual(context["origin"], read["origin"])
        exported = self.engine.export(file["file_id"], file["source_version"])
        first = json.loads(Path(exported["artifact_path"]).read_text().splitlines()[0])
        self.assertEqual(first["origin"], read["origin"])
        bundle = self.engine.evidence_bundle([{"file_id": file["file_id"], "expected_version": file["source_version"]}])
        manifest = json.loads(Path(bundle["artifact_path"]).read_text())
        self.assertEqual(manifest["source"]["provider"], "microsoft_graph")
        self.assertIsNone(manifest["source"]["root"])
        self.assertEqual(manifest["files"][0]["origin"], read["origin"])
        self.assertEqual(list(self.anchor.iterdir()), [])
        self.assertNotIn("synthetic-access-token", json.dumps(manifest))

    def test_remote_etag_change_invalidates_read_even_when_bytes_unchanged(self):
        file = self.indexed()
        self.graph.entries["budget.csv"]["eTag"] = "v2"
        result = self.engine.call("read", {"file_id": file["file_id"], "expected_version": file["source_version"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "STALE_SOURCE")
        changed = self.indexed()
        self.assertNotEqual(changed["source_version"], file["source_version"])
        self.assertEqual(changed["origin"]["etag"], "v2")

    def test_offline_scan_does_not_delete_last_inventory_or_claim_complete(self):
        file = self.indexed()
        self.fail(FakeGraph.anchor, 503)
        with self.assertRaises(DriveError):
            self.engine.scan_once()
        with self.engine._db() as db:
            row = db.execute("SELECT status,source_version FROM files WHERE id=?", (file["file_id"],)).fetchone()
            self.assertEqual(tuple(row), ("ready", file["source_version"]))
            self.assertFalse(self.engine._coverage(db)["inventory_complete"])
        answer = self.engine.call("read", {"file_id": file["file_id"]})
        self.assertFalse(answer["ok"])
        self.assertNotIn("segments", answer)
        self.assertNotIn("PRIVATE_HTTP", json.dumps(answer))
        self.graph.overrides.clear()
        self.assertEqual(self.indexed()["source_version"], file["source_version"])

    def test_revoked_child_permission_blocks_cached_keyword_and_ranked_snippets(self):
        for mode in ("keyword", "hybrid"):
            with self.subTest(mode=mode):
                self.graph.overrides.clear()
                file = self.indexed()
                self.engine.search("travel", mode=mode)
                self.fail(FakeGraph.anchor + ":/budget.csv", 403)
                result = self.engine.call("search", {"query": "travel", "mode": mode})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "GRAPH_ACCESS_DENIED")
                self.assertNotIn("files", result)
                self.assertNotIn("120", json.dumps(result))
                with self.engine._db() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM segments WHERE file_id=?", (file["file_id"],)).fetchone()[0], 0)

    def test_list_rechecks_child_access_and_deleted_file_expires_version(self):
        file = self.indexed()
        self.fail(FakeGraph.anchor + ":/budget.csv", 403)
        self.assertEqual(self.engine.call("list")["error"]["code"], "GRAPH_ACCESS_DENIED")
        self.graph.overrides.clear()
        self.graph.entries.clear()
        self.graph.children["folder-1"] = []
        self.engine.scan_once()
        self.assertEqual(self.engine.list()["files"], [])
        self.assertEqual(self.engine.call("read", {"file_id": file["file_id"]})["error"]["code"], "FILE_NOT_FOUND")

    def test_revocation_blocks_cached_text_before_optional_reranker(self):
        self.indexed()
        self.engine.retrieval.config["rerank"]["enabled"] = True
        self.fail(FakeGraph.anchor + ":/budget.csv", 403)
        with mock.patch("team_drive.retrieval._post") as post:
            result = self.engine.call("search", {"query": "travel", "mode": "hybrid", "rerank": True})
        self.assertEqual(result["error"]["code"], "GRAPH_ACCESS_DENIED")
        post.assert_not_called()

    def test_revocation_before_semantic_index_blocks_cached_text(self):
        self.indexed()
        self.engine.retrieval.config["semantic"]["enabled"] = True
        # Listing remains possible, but per-file metadata access has been revoked.
        self.fail(FakeGraph.anchor + ":/budget.csv", 403)
        with mock.patch.object(self.engine.retrieval, "index_file") as index:
            with self.assertRaises(DriveError) as error:
                self.engine.scan_once()
        self.assertEqual(error.exception.code, "GRAPH_ACCESS_DENIED")
        index.assert_not_called()

    def test_source_and_env_credential_changes_cannot_reuse_prior_cloud_index(self):
        self.indexed()
        self.engine.close()
        os.environ["DF_MCP_GRAPH_TEST_TOKEN"] = "another-synthetic-principal-token"
        with self.assertRaises(DriveError) as error:
            Engine(str(self.config_path), autostart=False)
        self.assertEqual(error.exception.code, "STATE_ROOT_MISMATCH")

    def test_normalized_configuration_and_credentials_are_separate_from_index(self):
        result = settings(self.config_path)
        self.assertEqual(result["config"]["provider"], "microsoft_graph")
        self.assertEqual(result["config"]["graph"]["max_items"], 10000)
        self.assertNotIn("synthetic-access-token", json.dumps(result["config"]))
        self.config["graph"].pop("token_env")
        self.config["graph"]["auth_cache"] = str(self.anchor / "auth.json")
        self.config_path.write_text(json.dumps(self.config))
        with self.assertRaises(DriveError) as error:
            settings(self.config_path)
        self.assertEqual(error.exception.code, "INVALID_CONFIG")


if __name__ == "__main__":
    unittest.main()
