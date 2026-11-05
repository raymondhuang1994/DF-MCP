"""Wiki acceptance on synthetic originals: provenance, durability and revocation."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from team_drive.engine import Engine
from team_drive.errors import DriveError
from team_drive.wiki import WikiStore


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "originals"
        self.source.mkdir()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"root": str(self.source), "state_dir": str(self.base / "state")}), encoding="utf-8")
        self.engine = Engine(str(self.config), autostart=False)
        self.wiki = WikiStore(self.engine)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def file(self, name, text):
        path = self.source / name
        path.write_text(text, encoding="utf-8")
        self.engine.scan_once()
        file_id = next(row["file_id"] for row in self.engine.list()["files"] if row["path"] == name)
        inspected = self.engine.inspect(file_id)
        result = self.engine.read(file_id, expected_version=inspected["source_version"])
        segment = result["segments"][0]
        return {"file_id": file_id, "source_version": result["source_version"], "ordinal": segment["ordinal"],
                "locator": segment["locator"], "quote": segment["text"]}

    def write_page(self, page_id="budget", citation=None, **kwargs):
        citation = citation or self.file("budget.txt", "SIMULATED budget 100 units.\n")
        values = dict(page_id=page_id, kind="source", title="Budget summary", body="The simulated budget is 100 units. [1]", citations=[citation])
        values.update(kwargs)
        return self.wiki.write(**values)

    def assert_code(self, code, callable_, *args, **kwargs):
        try:
            result = callable_(*args, **kwargs)
        except DriveError as exc:
            self.assertEqual(exc.code, code)
        else:
            self.assertFalse(result.get("ok", True))
            self.assertEqual(result["error"]["code"], code)

    def test_persistent_markdown_index_log_and_originals_unchanged(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        original = (self.source / "budget.txt").read_bytes()
        self.write_page(citation=citation)
        self.assertEqual((self.source / "budget.txt").read_bytes(), original)
        self.assertTrue((self.engine.state / "wiki-page-budget.md").is_file())
        self.assertIn(citation["source_version"], (self.engine.state / "wiki-page-budget.md").read_text())
        self.assertIn("budget", (self.engine.state / "wiki-index.md").read_text())
        self.assertIn("created", (self.engine.state / "wiki-log.md").read_text())
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.wiki = WikiStore(self.engine)
        result = self.wiki.read("budget")
        self.assertEqual(result["page"]["revision"], 1)
        self.assertEqual(result["page"]["citations"], [citation])
        self.assertEqual(result["claim_entailment"], "not_established")
        self.assertEqual(self.wiki.search("budget")["returned"], 1)
        self.assertEqual(self.wiki.status()["counts"], {"current": 1, "stale": 0})

    def test_exact_quote_locator_and_version_required(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        bads = [dict(citation, quote="budget 900"), dict(citation, locator={"line": 999}), dict(citation, ordinal=99)]
        for bad in bads:
            self.assert_code("WIKI_CITATION_MISMATCH", self.write_page, citation=bad)
        self.assert_code("VERSION_MISMATCH", self.write_page, citation=dict(citation, source_version="0" * 64))
        self.assertEqual(self.wiki.status()["counts"]["current"], 0)

    def test_unsupported_summary_is_never_certified_as_entailment(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        # Exact citations are checkable. Semantic entailment is deliberately not
        # advertised: the host must compare a derived statement to its original.
        self.write_page(citation=citation, body="A draft interpretation requiring review. [1]")
        result = self.wiki.read("budget")
        self.assertEqual(result["claim_entailment"], "not_established")
        self.assertTrue(result["page"]["derived"])
        self.assertEqual(result["page"]["review_status"], "unreviewed")

    def test_changed_original_cannot_serve_cached_title_or_body(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        self.write_page(citation=citation, title="SENSITIVE_CACHED_TITLE", body="SENSITIVE_CACHED_BODY [1]")
        (self.source / "budget.txt").write_text("SIMULATED budget 200 units.\n")
        result = self.wiki.search("SENSITIVE_CACHED")
        self.assertEqual(result["pages"], [])
        self.assertNotIn("SENSITIVE_CACHED_TITLE", json.dumps(result))
        self.assertNotIn("SENSITIVE_CACHED_BODY", json.dumps(self.wiki.status()))
        stale = self.wiki.read("budget")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["code"], "WIKI_STALE")
        self.assertEqual(stale["rebuild"]["sources"], [{"file_id": citation["file_id"], "previous_source_version": citation["source_version"]}])
        self.assertEqual(stale["rebuild"]["revision"], 1)
        self.assertNotIn("SENSITIVE_CACHED", json.dumps(stale))
        self.wiki.refresh()
        self.assertNotIn("SENSITIVE_CACHED", (self.engine.state / "wiki-page-budget.md").read_text())

    def test_revoked_access_hides_page_even_with_unchanged_index(self):
        self.write_page()
        failure = DriveError("SHARE_ACCESS_DENIED", "Synthetic permission revoked.")
        with mock.patch.object(self.engine, "_validated", side_effect=failure):
            self.assert_code("SHARE_ACCESS_DENIED", self.wiki.read, "budget")
        self.assertEqual(self.wiki.status()["pending_total"], 1)
        self.assertEqual(self.wiki.search("budget")["pages"], [])

    def test_transient_access_recovery_restores_exact_original_without_rebuild(self):
        self.write_page()
        original = self.wiki.read("budget")["page"]
        with mock.patch.object(self.engine, "_validated", side_effect=DriveError("SHARE_ACCESS_DENIED", "Synthetic denial.")):
            self.assert_code("SHARE_ACCESS_DENIED", self.wiki.read, "budget")
        self.assert_code("WIKI_STALE", self.wiki.read, "budget")
        refreshed = self.wiki.refresh()
        self.assertEqual(refreshed["revalidated_page_ids"], ["budget"])
        self.assertEqual(refreshed["pending_total"], 0)
        current = self.wiki.read("budget")["page"]
        self.assertEqual(current["revision"], original["revision"])
        self.assertEqual(current["body"], original["body"])
        self.assertIn("revalidated", (self.engine.state / "wiki-log.md").read_text())

    def test_new_original_version_never_restores_old_page(self):
        self.write_page()
        (self.source / "budget.txt").write_text("SIMULATED budget 900 units.\n")
        self.engine.scan_once()
        refreshed = self.wiki.refresh()
        self.assertEqual(refreshed["revalidated_pages"], 0)
        self.assertEqual(refreshed["pending_total"], 1)
        self.assert_code("WIKI_STALE", self.wiki.read, "budget")

    def test_incremental_invalidation_only_affected_pages(self):
        first = self.file("alpha.txt", "SIMULATED alpha amount 100.\n")
        second = self.file("beta.txt", "SIMULATED beta amount 200.\n")
        self.write_page("alpha", first)
        self.write_page("beta", second)
        self.write_page("topic", first, kind="topic", citations=[first, second], body="Two simulated amounts need comparison. [1] [2]")
        (self.source / "alpha.txt").write_text("SIMULATED alpha amount 300.\n")
        self.engine.scan_once()
        status = self.wiki.status()
        self.assertEqual(status["counts"], {"current": 1, "stale": 2})
        self.assertEqual({p["page_id"] for p in status["pending_pages"]}, {"alpha", "topic"})
        self.assertEqual(self.wiki.read("beta")["page"]["revision"], 1)
        new = self.engine.read(first["file_id"])
        updated = dict(first, source_version=new["source_version"], locator=new["segments"][0]["locator"], quote=new["segments"][0]["text"])
        self.write_page("alpha", updated, expected_revision=1, body="The updated amount is 300. [1]")
        self.assertEqual(self.wiki.status()["counts"], {"current": 2, "stale": 1})
        self.assertEqual(self.wiki.read("alpha")["page"]["revision"], 2)

    def test_delete_marks_dependents_stale_and_incomplete_inventory_is_not_deletion(self):
        self.write_page()
        with self.engine._db() as db:
            db.execute("UPDATE meta SET value='false' WHERE key='inventory_complete'")
        self.assertEqual(self.wiki.status()["counts"], {"current": 1, "stale": 0})
        self.assertTrue(self.wiki.read("budget")["ok"])
        (self.source / "budget.txt").unlink()
        self.engine.scan_once()
        self.assertEqual(self.wiki.status()["counts"], {"current": 0, "stale": 1})

    def test_revision_guard_prevents_replaying_write_or_lost_update(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        self.write_page(citation=citation)
        self.assert_code("WIKI_REVISION_MISMATCH", self.write_page, citation=citation)
        updated = self.write_page(citation=citation, expected_revision=1, body="Revised draft. [1]")
        self.assertEqual(updated["revision"], 2)
        self.assert_code("WIKI_REVISION_MISMATCH", self.write_page, citation=citation, expected_revision=1)

    def test_original_versions_checked_once_per_operation(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        self.write_page("first", citation, kind="topic", citations=[citation, citation], body="Simulated budget. [1] [2]")
        self.write_page("second", citation)
        with mock.patch.object(self.engine, "_validated", wraps=self.engine._validated) as verify:
            result = self.wiki.search("budget")
        self.assertEqual(result["returned"], 2)
        self.assertEqual(verify.call_count, 1)

    def test_search_and_refresh_navigation_are_bounded(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        for number in range(3):
            self.write_page("page-%d" % number, citation)
        first = self.wiki.search("budget", limit=1)
        self.assertEqual(first["returned"], 1)
        self.assertTrue(first["has_more"])
        second = self.wiki.search("budget", limit=2, after=first["next_after"])
        self.assertEqual(second["returned"], 2)
        self.assertFalse(second["has_more"])
        refresh = self.wiki.refresh(limit=1)
        self.assertEqual(refresh["visited_pages"], 1)
        self.assertTrue(refresh["has_more"])
        self.assertEqual(self.wiki.refresh(after=refresh["next_after"])["visited_pages"], 2)

    def test_chinese_keyword_and_sql_text_are_not_executed(self):
        citation = self.file("budget.txt", "模拟预算100元。\n")
        self.write_page(citation=citation, title="模拟预算", body="模拟预算待核对。 [1]")
        self.assertEqual(self.wiki.search("预算")["returned"], 1)
        self.assertEqual(self.wiki.search("预算' OR 1=1")["returned"], 0)

    def test_verification_budget_never_returns_unchecked_content(self):
        self.write_page()
        with mock.patch.object(self.wiki, "_context", return_value={"deadline": 0, "files": {}}):
            self.assert_code("WIKI_VERIFICATION_BUDGET", self.wiki.read, "budget")
            result = self.wiki.search("budget")
            self.assertEqual(result["pages"], [])
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_after"], "")
            self.assertEqual(result["stop_reason"], "verification_budget")
        self.assertEqual(self.wiki.status()["pending_total"], 0)

    def test_path_and_citation_input_bounds(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        for page_id in ("../escape", "UPPER", "", "a" * 65, "/absolute"):
            self.assert_code("INVALID_WIKI_INPUT", self.write_page, page_id, citation)
        self.assert_code("INVALID_WIKI_INPUT", self.write_page, citation=citation, body="Unreferenced statement")
        self.assert_code("INVALID_WIKI_INPUT", self.write_page, citation=citation, body="Extra citation. [1] [2]")
        self.assert_code("INVALID_WIKI_INPUT", self.write_page, citation=citation, body="字" * 5000 + " [1]")
        self.assert_code("INVALID_WIKI_INPUT", self.write_page, citation=dict(citation, quote="x" * 1025))
        self.assert_code("INVALID_WIKI_INPUT", self.write_page, citation=citation, links=["../escape"])

    def test_markdown_tampering_never_becomes_evidence(self):
        self.write_page()
        (self.engine.state / "wiki-page-budget.md").write_text("ATTACKER_SAYS_AMOUNT_999")
        result = self.wiki.read("budget")
        self.assertNotIn("ATTACKER", json.dumps(result))
        self.assertIn("100", result["page"]["body"])

    def test_symlink_mirror_does_not_write_outside_state(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        outside = self.base / "outside.txt"
        outside.write_text("DO_NOT_CHANGE")
        link = self.engine.state / "wiki-page-budget.md"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privilege unavailable")
            raise
        result = self.write_page(citation=citation)
        self.assertTrue(result["ok"])
        self.assertFalse(result["markdown"]["published"])
        self.assertEqual(outside.read_text(), "DO_NOT_CHANGE")
        self.assertEqual(self.wiki.read("budget")["page"]["revision"], 1)

    def test_mirror_error_does_not_rollback_committed_wiki(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        with mock.patch.object(self.wiki, "_atomic_text", side_effect=OSError("synthetic disk failure")):
            result = self.write_page(citation=citation)
        self.assertTrue(result["ok"])
        self.assertFalse(result["markdown"]["published"])
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.wiki = WikiStore(self.engine)
        self.assertEqual(self.wiki.read("budget")["page"]["revision"], 1)
        self.wiki.refresh()
        self.assertTrue((self.engine.state / "wiki-page-budget.md").is_file())

    def test_source_index_race_blocks_publication(self):
        citation = self.file("budget.txt", "SIMULATED budget 100 units.\n")
        original_verify = self.wiki._verify
        def mutate_after_verification(citations, context):
            original_verify(citations, context)
            with self.engine._db() as db:
                db.execute("UPDATE files SET status='pending',source_version=NULL WHERE id=?", (citation["file_id"],))
        with mock.patch.object(self.wiki, "_verify", side_effect=mutate_after_verification):
            self.assert_code("INDEX_CHANGED", self.write_page, citation=citation)
        self.assertEqual(self.wiki.status()["counts"]["current"], 0)

    def test_navigation_links_do_not_become_fact_sources(self):
        first = self.file("alpha.txt", "SIMULATED alpha 100.\n")
        second = self.file("beta.txt", "SIMULATED beta 200.\n")
        self.write_page("alpha", first)
        self.write_page("beta", second, links=["alpha"])
        (self.source / "alpha.txt").write_text("SIMULATED alpha 300.\n")
        self.engine.scan_once()
        self.assertEqual(self.wiki.status()["counts"], {"current": 1, "stale": 1})
        self.assertTrue(self.wiki.read("beta")["ok"])
        self.assertEqual(self.wiki.read("beta")["page"]["citations"], [second])


if __name__ == "__main__":
    unittest.main()
