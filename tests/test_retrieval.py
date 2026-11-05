"""Retrieval contracts exercised with synthetic documents and a loopback fake model."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from team_drive.errors import DriveError
from team_drive.retrieval import Retrieval, _ModelFailure, _default_tokens, validate_config


class FakeModel:
    def __init__(self):
        self.requests = []
        self.fail = False
        self.invalid = False
        self.dimension_change = False
        self.redirect = False
        self.rerank_fail = False
        self.vectors = {}
        self.delay = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append((self.path, payload))
                if owner.delay:
                    time.sleep(owner.delay)
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", owner.url + "/unexpected")
                    self.end_headers()
                    return
                if owner.fail or (self.path == "/rerank" and owner.rerank_fail):
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"DO_NOT_EXPOSE_PRIVATE_MODEL_RESPONSE")
                    return
                if self.path == "/rerank":
                    result = {"results": [{"index": i, "relevance_score": i + 0.25}
                                          for i in range(len(payload["documents"]))]}
                else:
                    data = []
                    for index, text in enumerate(payload["input"]):
                        vector = owner.vectors.get(text, [1.0, 0.0])
                        if owner.dimension_change:
                            vector = [1.0, 0.0, 0.0]
                        data.append({"index": index, "embedding": vector})
                    result = {"data": list(reversed(data))}  # Index, not response order, is authoritative.
                if owner.invalid:
                    result = {"data": [{"index": 0, "embedding": [float("nan"), 0]}]}
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass  # Expected when a deadline test closes the client early.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def database(path=":memory:"):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE files(id TEXT PRIMARY KEY,path TEXT,source_version TEXT,status TEXT)")
    db.execute("CREATE TABLE segments(file_id TEXT,ordinal INTEGER,source_version TEXT,payload TEXT,PRIMARY KEY(file_id,ordinal))")
    db.execute("CREATE VIRTUAL TABLE search_index USING fts5(tokens,file_id UNINDEXED,ordinal UNINDEXED,tokenize='ascii')")
    return db


def add_file(db, file_id, text, version="v1", path=None):
    path = path or file_id + ".txt"
    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,'ready')", (file_id, path, version))
    db.execute("DELETE FROM segments WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM search_index WHERE file_id=?", (file_id,))
    db.execute("INSERT INTO search_index VALUES(?,?,-1)", (" ".join(_default_tokens(path)), file_id))
    for ordinal, value in enumerate(text if isinstance(text, list) else [text]):
        db.execute("INSERT INTO segments VALUES(?,?,?,?)", (file_id, ordinal, version,
                   json.dumps({"text": value, "locator": {"line_start": ordinal + 1}})))
        db.execute("INSERT INTO search_index VALUES(?,?,?)", (" ".join(_default_tokens(value)), file_id, ordinal))


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.model = FakeModel()
        self.db = database()

    def tearDown(self):
        self.db.close()
        self.model.close()

    def retrieval(self, **semantic):
        config = {"semantic": {"enabled": True, "endpoint": self.model.url + "/embeddings", "model": "synthetic-v1"}}
        config["semantic"].update(semantic)
        value = Retrieval(config)
        value.init_schema(self.db)
        return value

    def test_default_is_network_disabled_and_keyword_only(self):
        add_file(self.db, "budget", "synthetic budget 100")
        value = Retrieval()
        value.init_schema(self.db)
        with mock.patch("team_drive.retrieval._post", side_effect=AssertionError("network forbidden")):
            self.assertEqual(value.index_file(self.db, "budget", "v1")["state"], "disabled")
            result = value.search(self.db, "budget")
        self.assertEqual(result["hits"][0]["file_id"], "budget")
        self.assertEqual(result["diagnostics"]["mode"], "bm25")
        self.assertFalse(value.status(self.db)["network_calls_enabled"])
        traced = []
        self.db.set_trace_callback(traced.append)
        status = value.status(self.db)
        self.db.set_trace_callback(None)
        self.assertIsNone(status["semantic"]["eligible_segments"])
        self.assertFalse(status["semantic"]["coverage_checked"])
        self.assertFalse(any("segments" in statement.lower() for statement in traced))

    def test_public_dns_metadata_and_redirect_urls_are_rejected(self):
        for endpoint in ("https://api.openai.com/v1/embeddings", "http://8.8.8.8/embed",
                         "http://169.254.169.254/token", "http://model.local/embed",
                         "http://user:secret@127.0.0.1/embed", "http://127.0.0.1/embed?q=x",
                         "http://[fe80::1]/embed", "file:///tmp/model", "http://127.0.0.1:99999/embed"):
            with self.subTest(endpoint=endpoint), self.assertRaises(DriveError) as error:
                validate_config({"semantic": {"endpoint": endpoint}})
            self.assertEqual(error.exception.code, "INVALID_CONFIG")
        self.assertEqual(validate_config({"semantic": {"endpoint": "http://localhost:1234/embed"}})["semantic"]["endpoint"],
                         "http://127.0.0.1:1234/embed")
        for endpoint in ("http://10.0.0.2/embed", "https://192.168.1.1/embed", "http://[::1]/embed", "http://[fd00::2]/embed"):
            self.assertEqual(validate_config({"semantic": {"endpoint": endpoint}})["semantic"]["endpoint"], endpoint)

    def test_config_validation_requires_real_model_configuration(self):
        for config in ({"semantic": {"enabled": True}}, {"candidate_limit": True},
                       {"semantic": {"enabled": "true"}}, {"semantic": {"max_segments": 0}},
                       {"rerank": {"enabled": True}}, {"semantic": {"unknown": 5}},
                       {"rrf_k": float("nan")}, {"rrf_k": 10 ** 1000}, {"semantic": {"minimum_similarity": 2}}):
            with self.subTest(config=config), self.assertRaises(DriveError):
                Retrieval(config)

    def test_true_semantic_recall_without_keyword_overlap_and_rrf(self):
        add_file(self.db, "a", "synthetic loan repayment")
        add_file(self.db, "b", "synthetic holiday beach")
        self.model.vectors.update({"synthetic loan repayment": [1, 0], "synthetic holiday beach": [0, 1],
                                   "credit obligation": [1, 0], "loan": [1, 0]})
        value = self.retrieval(minimum_similarity=0.5)
        self.assertEqual(value.index_file(self.db, "a", "v1")["state"], "ready")
        value.index_file(self.db, "b", "v1")
        result = value.search(self.db, "credit obligation", mode="semantic")
        self.assertEqual([hit["file_id"] for hit in result["hits"]], ["a"])
        self.assertNotIn("keyword_rank", result["hits"][0]["signals"])
        hybrid = value.search(self.db, "loan", mode="hybrid")
        self.assertEqual(hybrid["hits"][0]["signals"]["keyword_rank"], 1)
        self.assertEqual(hybrid["hits"][0]["signals"]["semantic_rank"], 1)
        self.assertAlmostEqual(hybrid["hits"][0]["score"], 2 / 61)
        self.assertFalse(hybrid["diagnostics"]["snippet_is_evidence"])

    def test_file_cache_survives_new_instance_and_disk_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "index.sqlite")
            db = database(path)
            add_file(db, "a", "synthetic cache")
            value = Retrieval({"semantic": {"enabled": True, "endpoint": self.model.url + "/embeddings", "model": "test"}})
            value.init_schema(db)
            value.index_file(db, "a", "v1")
            requests = len(self.model.requests)
            db.commit()
            db.close()
            db = sqlite3.connect(path)
            restored = Retrieval(value.config)
            restored.init_schema(db)
            restored.index_file(db, "a", "v1")
            self.assertEqual(len(self.model.requests), requests)
            self.assertTrue(restored.status(db)["semantic"]["coverage_complete"])
            db.close()

    def test_changed_version_and_deleted_sources_never_return_old_vectors(self):
        add_file(self.db, "a", "old synthetic content")
        value = self.retrieval()
        value.index_file(self.db, "a", "v1")
        add_file(self.db, "a", "new synthetic content", version="v2")
        result = value.search(self.db, "old", mode="semantic")
        self.assertEqual(result["hits"], [])  # Version join works even before a cleanup hook.
        value.index_file(self.db, "a", "v2")
        self.assertEqual(self.db.execute("SELECT DISTINCT source_version FROM retrieval_vectors").fetchall(), [("v2",)])
        self.db.execute("UPDATE files SET status='deleted' WHERE id='a'")
        self.assertEqual(value.search(self.db, "synthetic")["hits"], [])
        value.prune(self.db)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM retrieval_file_state").fetchone()[0], 0)

    def test_pending_source_is_not_embedded_or_retrieved(self):
        add_file(self.db, "a", "synthetic pending")
        self.db.execute("UPDATE files SET status='pending'")
        value = self.retrieval()
        self.assertEqual(value.index_file(self.db, "a", "v1")["error_code"], "SOURCE_VERSION_UNAVAILABLE")
        self.assertEqual(self.model.requests, [])
        self.assertEqual(value.search(self.db, "pending")["hits"], [])

    def test_model_revision_change_invalidates_persistent_vectors(self):
        add_file(self.db, "a", "synthetic revision")
        value = self.retrieval(model_revision="first")
        value.index_file(self.db, "a", "v1")
        replacement = self.retrieval(model_revision="second")
        self.assertEqual(replacement.status(self.db)["semantic"]["indexed_segments"], 0)
        replacement.index_file(self.db, "a", "v1")
        self.assertEqual(len(self.model.requests), 2)

    def test_changed_text_under_same_version_is_not_reused(self):
        add_file(self.db, "a", "synthetic before")
        value = self.retrieval()
        value.index_file(self.db, "a", "v1")
        add_file(self.db, "a", "synthetic after")
        result = value.search(self.db, "semantic query", mode="semantic")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["diagnostics"]["semantic"]["query"]["stale_segments_skipped"], 1)
        self.assertEqual(result["diagnostics"]["semantic"]["query"]["state"], "partial")
        self.assertFalse(value.status(self.db)["semantic"]["coverage_complete"])
        before = len(self.model.requests)
        value.index_file(self.db, "a", "v1")
        self.assertEqual(len(self.model.requests), before + 1)

    def test_index_failure_keeps_ready_source_and_reports_safe_error(self):
        add_file(self.db, "a", "synthetic document")
        value = self.retrieval()
        self.model.fail = True
        result = value.index_file(self.db, "a", "v1")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "MODEL_HTTP_ERROR")
        self.assertEqual(self.db.execute("SELECT status FROM files").fetchone()[0], "ready")
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(value.status(self.db)))
        self.model.fail = False
        self.assertEqual(value.index_file(self.db, "a", "v1")["state"], "ready")
        self.assertEqual(value.status(self.db)["semantic"]["errors"], [])

    def test_query_failure_has_explicit_hybrid_fallback_and_no_fake_semantics(self):
        add_file(self.db, "a", "synthetic budget")
        value = self.retrieval()
        value.index_file(self.db, "a", "v1")
        self.model.fail = True
        hybrid = value.search(self.db, "budget")
        self.assertEqual(hybrid["hits"][0]["file_id"], "a")
        self.assertEqual(hybrid["diagnostics"]["semantic"]["query"]["state"], "failed")
        self.assertEqual(hybrid["diagnostics"]["mode"], "bm25")
        semantic = value.search(self.db, "budget", mode="semantic")
        self.assertEqual(semantic["hits"], [])
        self.assertEqual(semantic["diagnostics"]["mode"], "semantic_unavailable")
        self.assertEqual(semantic["diagnostics"]["semantic"]["query"]["fallback"], "none")

    def test_disabled_semantic_mode_is_explicitly_unavailable(self):
        add_file(self.db, "a", "synthetic budget")
        value = Retrieval()
        value.init_schema(self.db)
        result = value.search(self.db, "budget", mode="semantic")
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["diagnostics"]["semantic"]["query"]["error_code"], "SEMANTIC_DISABLED")

    def test_dimension_change_and_invalid_vectors_are_controlled_failures(self):
        add_file(self.db, "a", "synthetic one")
        add_file(self.db, "b", "synthetic two")
        value = self.retrieval()
        value.index_file(self.db, "a", "v1")
        self.model.dimension_change = True
        self.assertEqual(value.index_file(self.db, "b", "v1")["error_code"], "EMBEDDING_DIMENSION_MISMATCH")
        self.model.dimension_change = False
        self.model.invalid = True
        self.assertEqual(value.index_file(self.db, "b", "v1")["error_code"], "EMBEDDING_INVALID_VECTOR")
        self.assertEqual(self.db.execute("SELECT status FROM files WHERE id='b'").fetchone()[0], "ready")

    def test_redirect_is_not_followed(self):
        add_file(self.db, "a", "synthetic redirect")
        value = self.retrieval()
        self.model.redirect = True
        result = value.index_file(self.db, "a", "v1")
        self.assertEqual(result["error_code"], "MODEL_REDIRECT_BLOCKED")
        self.assertEqual(len(self.model.requests), 1)

    def test_capacity_and_input_truncation_are_visible(self):
        add_file(self.db, "a", ["synthetic line one", "synthetic line two", "synthetic line three"])
        value = self.retrieval(max_segments=2, max_text_chars=8)
        result = value.index_file(self.db, "a", "v1")
        self.assertEqual(result["state"], "partial")
        self.assertTrue(result["limit_reached"])
        status = value.status(self.db)["semantic"]
        self.assertEqual(status["eligible_segments"], 3)
        self.assertEqual(status["indexed_segments"], 2)
        self.assertEqual(status["input_truncated_segments"], 2)
        self.assertFalse(status["coverage_complete"])
        self.assertTrue(all(len(text) <= 8 for _, payload in self.model.requests for text in payload.get("input", [])))

    def test_prefix_scope_filters_before_semantic_ranking(self):
        add_file(self.db, "a", "synthetic a", path="team-a/a.txt")
        add_file(self.db, "b", "synthetic b", path="team-b/b.txt")
        value = self.retrieval()
        value.index_file(self.db, "a", "v1")
        value.index_file(self.db, "b", "v1")
        result = value.search(self.db, "synthetic", prefix="team-a")
        self.assertEqual([hit["file_id"] for hit in result["hits"]], ["a"])
        self.assertEqual(result["diagnostics"]["semantic"]["query"]["scanned_segments"], 1)

    def test_rerank_validates_permutation_and_falls_back_stably(self):
        add_file(self.db, "a", "synthetic budget a")
        add_file(self.db, "b", "synthetic budget b")
        value = Retrieval({"rerank": {"enabled": True, "endpoint": self.model.url + "/rerank", "model": "synthetic"}})
        value.init_schema(self.db)
        baseline = value.search(self.db, "budget", rerank=False)
        result = value.search(self.db, "budget", rerank=True)
        self.assertEqual(result["hits"][0]["file_id"], baseline["hits"][1]["file_id"])
        self.assertEqual(result["diagnostics"]["rerank"]["state"], "ready")
        self.model.rerank_fail = True
        fallback = value.search(self.db, "budget", rerank=True)
        self.assertEqual(fallback["hits"], baseline["hits"])
        self.assertEqual(fallback["diagnostics"]["rerank"]["fallback"], "rrf_order_preserved")
        invalid = {"results": [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 2}]}
        with mock.patch("team_drive.retrieval._post", return_value=invalid):
            malformed = value.search(self.db, "budget", rerank=True)
        self.assertEqual(malformed["hits"], baseline["hits"])
        self.assertEqual(malformed["diagnostics"]["rerank"]["error_code"], "RERANK_INVALID_RESPONSE")

    def test_expired_scan_deadline_does_not_send_document(self):
        add_file(self.db, "a", "synthetic deadline")
        value = self.retrieval()
        result = value.index_file(self.db, "a", "v1", deadline=time.monotonic() - 1)
        self.assertEqual(result["error_code"], "MODEL_BUDGET_EXCEEDED")
        self.assertEqual(result["newly_indexed_segments"], 0)
        self.assertEqual(self.model.requests, [])
        self.assertEqual(self.db.execute("SELECT status FROM files").fetchone()[0], "ready")

    def test_per_file_budget_preserves_completed_batches_and_resume(self):
        add_file(self.db, "a", ["synthetic first", "synthetic second", "synthetic third"])
        value = self.retrieval(batch_size=1, per_file_budget_seconds=1)
        now = 100.0
        batch_duration = 0.625
        requests = []

        def timed_response(_options, payload, deadline=None):
            nonlocal now
            self.assertIsNotNone(deadline)
            requests.append(tuple(payload["input"]))
            # Model transport consumes a deterministic budget: the first batch
            # completes, while the second reaches the shared file deadline.
            now = min(now + batch_duration, deadline)
            if now >= deadline:
                raise _ModelFailure("MODEL_BUDGET_EXCEEDED")
            return {"data": [{"index": index, "embedding": [1.0, 0.0]}
                             for index in range(len(payload["input"]))]}

        with mock.patch("team_drive.retrieval.time.monotonic", side_effect=lambda: now), \
                mock.patch("team_drive.retrieval._post", side_effect=timed_response):
            result = value.index_file(self.db, "a", "v1")
            self.assertEqual(result["state"], "partial")
            self.assertEqual(result["newly_indexed_segments"], 1)
            self.assertEqual(result["indexed_segments"], 1)
            self.assertEqual(result["error_code"], "MODEL_BUDGET_EXCEEDED")
            self.assertEqual(now, 101.0)
            retained = self.db.execute("SELECT * FROM retrieval_vectors WHERE ordinal=0").fetchone()
            self.assertIsNotNone(retained)
            self.assertEqual(requests, [("synthetic first",), ("synthetic second",)])

            batch_duration = 0.25
            result = value.index_file(self.db, "a", "v1")
            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["newly_indexed_segments"], 2)
            self.assertEqual(result["indexed_segments"], 3)
            self.assertIsNone(result["error_code"])
            self.assertEqual(self.db.execute("SELECT * FROM retrieval_vectors WHERE ordinal=0").fetchone(), retained)
            self.assertEqual(requests[2:], [("synthetic second",), ("synthetic third",)])
        self.assertEqual(self.model.requests, [])

    def test_model_revision_pinning_is_reported_without_claiming_weight_detection(self):
        value = self.retrieval()
        self.assertTrue(value.status(self.db)["semantic"]["model_version_unpinned"])
        pinned = self.retrieval(model_revision="synthetic-weights-sha")
        self.assertFalse(pinned.status(self.db)["semantic"]["model_version_unpinned"])

    def test_rerank_config_does_not_enable_unrequested_network_calls(self):
        add_file(self.db, "a", "synthetic budget")
        value = Retrieval({"rerank": {"enabled": True, "endpoint": self.model.url + "/rerank", "model": "synthetic"}})
        value.init_schema(self.db)
        result = value.search(self.db, "budget")
        self.assertEqual(result["diagnostics"]["rerank"]["state"], "skipped")
        self.assertEqual(self.model.requests, [])

    def test_keyword_candidate_filter_is_applied_before_limit_and_empty_stays_empty(self):
        add_file(self.db, "broad", "red apple")
        add_file(self.db, "exact", ["red", "invoice"])
        value = Retrieval({"candidate_limit": 1})
        value.init_schema(self.db)
        result = value.search(self.db, "red invoice", mode="keyword", keyword_file_ids=["exact"])
        self.assertEqual([hit["file_id"] for hit in result["hits"]], ["exact"])
        self.assertTrue(result["diagnostics"]["candidate_filter_applied"])
        self.assertEqual(value.search(self.db, "red invoice", mode="keyword", keyword_file_ids=[])["hits"], [])
        # One JSON parameter supports 1000 identifiers even on SQLite's older 999-variable builds.
        identifiers = ["missing-%d" % index for index in range(999)] + ["exact"]
        self.assertEqual(value.search(self.db, "red invoice", mode="keyword", keyword_file_ids=identifiers)["hits"][0]["file_id"], "exact")

    def test_init_schema_does_not_commit_caller_transaction(self):
        add_file(self.db, "a", "synthetic transactional")
        value = Retrieval()
        value.init_schema(self.db)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
