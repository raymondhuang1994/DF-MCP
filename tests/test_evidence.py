"""Version-pinned evidence bundles and explicit partial-text coverage."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from team_drive.engine import Engine


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-evidence-")
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"root": str(self.source), "state_dir": str(self.base / "state")}), encoding="utf-8")
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def text(self, name, value):
        path = self.source / name
        path.write_text(value, encoding="utf-8")
        return path

    def selection(self, name):
        file = next(file for file in self.engine.list()["files"] if file["path"] == name)
        return {"file_id": file["file_id"], "expected_version": file["source_version"]}

    def mixed_pdf(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 200 Td (Available evidence 123.45) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_blank_page(width=300, height=300)
        path = self.source / "mixed.pdf"
        with path.open("wb") as output:
            writer.write(output)
        return path

    def test_partial_pdf_preserves_available_text_and_exact_page_gaps(self):
        source = self.mixed_pdf()
        original = source.read_bytes()
        self.engine.scan_once()
        selected = self.selection("mixed.pdf")
        status = self.engine.status()["coverage"]
        self.assertTrue(status["all_present_files_indexed"])
        self.assertFalse(status["all_present_files_extracted_completely"])
        self.assertEqual(status["partial_files"], 1)
        read = self.engine.read(**selected)
        self.assertFalse(read["extraction_complete"])
        self.assertEqual(read["content_coverage"]["missing_text_pages"], [2])
        self.assertEqual(read["content_coverage"]["pages_with_text"], 1)
        self.assertIn("123.45", read["segments"][0]["text"])
        gap = self.engine.read(**selected, selector={"page": 2})
        self.assertEqual(gap["segments"][0]["locator"]["page"], 2)
        self.assertIn("no_extractable_text_ocr_may_be_required", gap["segments"][0]["warnings"])
        self.assertEqual(len(self.engine.search("Available")["files"]), 1)
        exported = self.engine.export(**selected)
        self.assertFalse(exported["extraction_complete"])
        self.assertEqual(source.read_bytes(), original)

    def test_bundle_exports_exact_selection_with_versions_and_observation_scope(self):
        self.text("selected.txt", "SELECTED EVIDENCE")
        self.text("unselected.txt", "MUST NOT EXPORT")
        self.engine.scan_once()
        selected = self.selection("selected.txt")
        result = self.engine.evidence_bundle([selected], scope_description="Selected fixture only")
        self.assertTrue(result["all_selected_versions_exported"])
        self.assertTrue(result["all_selected_text_complete"])
        manifest = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["scope"]["selection"], "exactly_explicit_file_ids")
        self.assertEqual(manifest["scope"]["description"], "Selected fixture only")
        self.assertEqual(manifest["business_completeness"], "not_established")
        self.assertEqual(manifest["source"]["root"], str(self.source))
        self.assertEqual(manifest["source"]["source_id"], self.engine.status()["source_id"])
        self.assertEqual(len(manifest["files"]), 1)
        entry = manifest["files"][0]
        self.assertEqual(entry["source_version"], selected["expected_version"])
        self.assertEqual(entry["observed_version"], selected["expected_version"])
        self.assertLessEqual(manifest["observation_interval"]["started_at"], manifest["observation_interval"]["ended_at"])
        export_text = Path(entry["artifact_path"]).read_text(encoding="utf-8")
        self.assertIn("SELECTED EVIDENCE", export_text)
        self.assertNotIn("MUST NOT EXPORT", export_text)
        self.assertEqual(len(list((self.base / "state" / "exports").glob("*.jsonl"))), 1)

    def test_bundle_records_changed_missing_and_partial_files_without_business_claims(self):
        changed = self.text("changed.txt", "BEFORE")
        self.mixed_pdf()
        self.engine.scan_once()
        selection = self.selection("changed.txt")
        partial = self.selection("mixed.pdf")
        changed.write_text("AFTER", encoding="utf-8")
        requests = [selection, partial, {"file_id": "missing-id", "expected_version": "unknown"}]
        result = self.engine.evidence_bundle(requests)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exported_files"], 1)
        self.assertEqual(result["failed_files"], 2)
        self.assertFalse(result["all_selected_versions_exported"])
        self.assertFalse(result["all_selected_text_complete"])
        manifest = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["gaps"]), 3)
        self.assertEqual(manifest["files"][0]["error"]["code"], "STALE_SOURCE")
        self.assertEqual(manifest["files"][2]["error"]["code"], "FILE_NOT_FOUND")
        self.assertEqual(manifest["files"][1]["content_coverage"]["missing_text_pages"], [2])

    def test_bundle_rejects_invalid_or_duplicate_selections_before_output(self):
        self.text("one.txt", "one")
        self.engine.scan_once()
        item = self.selection("one.txt")
        for files in ([], [item] * 2, [item] * 51, [{"file_id": item["file_id"]}], [dict(item, path="../outside")]):
            with self.subTest(files=files[:2]):
                result = self.engine.call("evidence_bundle", {"files": files})
                self.assertFalse(result["ok"])
        self.assertFalse((self.base / "state" / "bundles").exists())
        self.assertFalse((self.base / "state" / "exports").exists())

    def test_bundle_does_not_substitute_a_different_available_version(self):
        self.text("one.txt", "current")
        self.engine.scan_once()
        actual = self.selection("one.txt")
        requested = dict(actual, expected_version="previous-version")
        result = self.engine.evidence_bundle([requested])
        self.assertEqual(result["exported_files"], 0)
        manifest = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"][0]["expected_version"], "previous-version")
        self.assertEqual(manifest["files"][0]["observed_version"], actual["expected_version"])
        self.assertEqual(manifest["files"][0]["error"]["code"], "VERSION_MISMATCH")
        self.assertFalse((self.base / "state" / "exports").exists())

    def test_bundle_never_modifies_source_names_bytes_or_mtime(self):
        self.text("one.txt", "one")
        self.text("two.txt", "two")
        def snapshot():
            return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in self.source.iterdir()}
        before = snapshot()
        self.engine.scan_once()
        self.engine.evidence_bundle([self.selection("one.txt"), self.selection("two.txt")])
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
