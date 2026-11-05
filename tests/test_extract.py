"""Generated public fixtures exercise parsing boundaries and evidence fidelity."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from team_drive.extract import ExtractionError, _cell_text, _drawing_paragraphs, iter_segments


S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, text):
        path = self.root / name
        # Fixtures must have identical bytes on Windows and POSIX. Text-mode
        # writes translate LF to CRLF on Windows and obscure fidelity assertions.
        path.write_bytes(text.encode("utf-8"))
        return path

    def archive(self, name, members):
        path = self.root / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
            for filename, data in members.items():
                out.writestr(filename, data)
        return path

    def xlsx(self, rows, extra="", shared=None):
        members = {
            "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Budget" sheetId="1" state="hidden" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PR}"><Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{S}"><sheetData>{rows}</sheetData>{extra}</worksheet>',
        }
        if shared is not None:
            members["xl/sharedStrings.xml"] = f'<sst xmlns="{S}">{shared}</sst>'
        return self.archive("book.xlsx", members)

    def assertCode(self, code, path, limits=None):
        with self.assertRaises(ExtractionError) as caught:
            list(iter_segments(path, limits or {}))
        self.assertEqual(code, caught.exception.code)
        self.assertNotIn(str(self.root), caught.exception.message)

    def test_text_bom_chinese_and_long_lines_are_lossless(self):
        original = "预算" * 20 + "\nnext\n"
        path = self.file("notes.md", "\ufeff" + original)
        segments = list(iter_segments(path, {"max_segment_chars": 9}))
        self.assertEqual(original, "".join(s["text"] for s in segments))
        self.assertTrue(all(len(s["text"]) <= 9 for s in segments))
        self.assertEqual(2, segments[-1]["locator"]["line_start"])

    def test_csv_quoted_newline_uses_logical_rows(self):
        path = self.file("data.csv", 'name,amount\n"Two\nlines",10.25\n')
        segments = list(iter_segments(path, {}))
        self.assertEqual(2, len(segments))
        self.assertEqual(2, segments[1]["locator"]["row"])
        self.assertEqual("Two\nlines", segments[1]["data"]["cells"][0]["value"])
        self.assertEqual(3, segments[1]["data"]["physical_line_end"])

    def test_crlf_source_text_and_csv_quoted_newlines_are_preserved(self):
        original = "预算\r\nnext\r\n"
        path = self.file("windows.txt", original)
        source_lines = [1] * len("预算\r\n") + [2] * len("next\r\n")
        for cap in (1, 3, 8_000):
            with self.subTest(max_segment_chars=cap):
                segments = list(iter_segments(path, {"max_segment_chars": cap}))
                self.assertEqual(original, "".join(s["text"] for s in segments))
                offset = 0
                for segment in segments:
                    self.assertEqual(source_lines[offset], segment["locator"]["line_start"])
                    offset += len(segment["text"])
        bare_cr = list(iter_segments(self.file("legacy.txt", "first\rsecond\r"), {}))
        self.assertEqual([1, 2], [s["locator"]["line_start"] for s in bare_cr])
        path = self.file("windows.csv", 'name,amount\r\n"Two\r\nlines",10.25\r\n')
        segments = list(iter_segments(path, {}))
        self.assertEqual("Two\r\nlines", segments[1]["data"]["cells"][0]["value"])
        self.assertEqual(3, segments[1]["data"]["physical_line_end"])

    def test_csv_long_cell_split_preserves_all_characters(self):
        value = "long" * 1000
        path = self.file("data.csv", value + ",end\n")
        segments = list(iter_segments(path, {"max_segment_chars": 256}))
        all_cells = [c for s in segments for c in s["data"]["cells"]]
        self.assertEqual(value, "".join(c["value"] for c in all_cells if c["coordinate"] == "A1"))
        self.assertEqual("end", all_cells[-1]["value"])
        self.assertTrue(all(len(s["text"]) <= 256 for s in segments))

    def test_tsv_supported(self):
        segments = list(iter_segments(self.file("data.tsv", "first\tsecond\n"), {}))
        self.assertEqual(2, len(segments[0]["data"]["cells"]))

    def test_zero_and_false_display_are_preserved(self):
        self.assertEqual("A1: 0", _cell_text({"coordinate": "A1", "value": 0}))
        self.assertEqual("A1: False", _cell_text({"coordinate": "A1", "value": False}))
        self.assertEqual("A1: ", _cell_text({"coordinate": "A1", "value": None}))

    def test_non_utf8_and_malformed_csv_fail_explicitly(self):
        path = self.root / "bad.csv"
        path.write_bytes(b"name,\xff\n")
        self.assertCode("unsupported_encoding", path)
        self.assertCode("parse_failed", self.file("bad.csv", '"never closed'))

    def test_limits_are_errors_not_silent_truncation(self):
        path = self.file("lines.txt", "first\nsecond\n")
        iterator = iter_segments(path, {"max_segments": 1})
        self.assertEqual("first\n", next(iterator)["text"])
        with self.assertRaises(ExtractionError) as caught:
            next(iterator)
        self.assertEqual("limit_exceeded", caught.exception.code)
        self.assertCode("limit_exceeded", path, {"max_file_bytes": 2})
        self.assertCode("invalid_limits", path, {"max_segments": True})
        self.assertCode("limit_exceeded", self.file("wide.csv", "a,b,c\n"), {"max_columns": 2})

    def test_xlsx_formulas_cache_coordinates_hidden_and_merges(self):
        path = self.xlsx('<row r="1" hidden="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>SUM(B2:B3)</f><v>12.30</v></c><c r="C1"><f>B1*2</f></c></row>',
                         '<mergeCells><mergeCell ref="A1:A2"/></mergeCells>',
                         '<si><r><t>Approved </t></r><r><t>budget</t></r></si>')
        result = list(iter_segments(path, {}))[0]
        self.assertEqual("Budget", result["locator"]["sheet"])
        self.assertEqual("hidden", result["data"]["sheet_state"])
        self.assertTrue(result["data"]["row_hidden"])
        self.assertEqual(["A1:A2"], result["data"]["merged_ranges"])
        cells = result["data"]["cells"]
        self.assertEqual("Approved budget", cells[0]["value"])
        self.assertEqual("SUM(B2:B3)", cells[1]["formula"])
        self.assertEqual("12.30", cells[1]["cached_value"])
        self.assertIsNone(cells[2]["cached_value"])
        self.assertIn("formula_cache_missing", result["warnings"])
        self.assertIn("merged_cells_not_filled", result["warnings"])

    def test_xlsx_large_inline_string_and_rows_not_lost(self):
        value = "预算" * 1000
        path = self.xlsx(f'<row r="1"><c r="A1" t="inlineStr"><is><t>{value}</t></is></c></row><row r="99"><c r="B99"><v>7</v></c></row>')
        results = list(iter_segments(path, {"max_segment_chars": 256}))
        self.assertEqual(value, "".join(c["value"] for s in results for c in s["data"]["cells"] if c["coordinate"] == "A1"))
        self.assertEqual(99, results[-1]["locator"]["row"])

    def test_xlsx_merge_context_expires_and_phonetic_text_not_duplicated(self):
        path = self.xlsx('<row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2"><v>2</v></c></row><row r="3"><c r="A3"><v>3</v></c></row>',
                         '<mergeCells><mergeCell ref="A1:B2"/><mergeCell ref="A3:B4"/></mergeCells>',
                         '<si><t>预算</t><rPh sb="0" eb="2"><t>annotation</t></rPh></si>')
        results = list(iter_segments(path, {}))
        self.assertEqual("预算", results[0]["data"]["cells"][0]["value"])
        self.assertEqual(["A1:B2"], results[1]["data"]["merged_ranges"])
        self.assertEqual(["A3:B4"], results[2]["data"]["merged_ranges"])

    def test_xlsx_inconsistent_coordinates_and_row_order_fail(self):
        self.assertCode("parse_failed", self.xlsx('<row r="1"><c r="A2"><v>1</v></c></row>'))
        self.assertCode("parse_failed", self.xlsx('<row r="2"/><row r="1"/>'))
        self.assertCode("parse_failed", self.xlsx('<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>'))

    def test_empty_xlsx_sheet_preserves_drawing_coverage_warnings(self):
        path = self.xlsx("", '<drawing xmlns:r="' + R + '" r:id="drawing1"/>')
        results = list(iter_segments(path, {}))
        self.assertEqual(1, len(results))
        self.assertEqual("worksheet", results[0]["locator"]["kind"])
        self.assertEqual("Budget", results[0]["locator"]["sheet"])
        self.assertTrue(results[0]["text"])
        self.assertEqual([], results[0]["data"]["cells"])
        self.assertEqual("hidden", results[0]["data"]["sheet_state"])
        self.assertIn("images_and_charts_not_extracted", results[0]["warnings"])
        self.assertIn("empty_worksheet_no_cell_rows", results[0]["warnings"])
        empty = list(iter_segments(self.xlsx(""), {}))
        self.assertEqual(1, len(empty))
        self.assertIn("empty_worksheet_no_cell_rows", empty[0]["warnings"])

    def test_xlsx_without_sheet_declarations_fails(self):
        path = self.archive("nosheets.xlsx", {"xl/workbook.xml": f'<workbook xmlns="{S}"><sheets/></workbook>'})
        self.assertCode("unsupported_structure", path)

    def test_xlsx_temporary_database_is_closed_and_removed_on_every_exit(self):
        connect = sqlite3.connect
        for outcome in ("complete", "cancel", "limit", "parse_error"):
            with self.subTest(outcome=outcome):
                connections = []
                databases = []

                def capture_connection(filename, *args, **kwargs):
                    connection = connect(filename, *args, **kwargs)
                    connections.append(connection)
                    databases.append(Path(filename))
                    return connection

                rows = '<row r="1"><c r="A1"><v>1</v></c></row><row r="2"><c r="A2"><v>2</v></c></row>'
                if outcome == "parse_error":
                    rows = '<row r="1"><c r="A2"><v>1</v></c></row>'
                path = self.xlsx(rows)
                with mock.patch("team_drive.extract.sqlite3.connect", side_effect=capture_connection):
                    iterator = iter_segments(path, {"max_segments": 1} if outcome == "limit" else {})
                    if outcome == "complete":
                        self.assertEqual(2, len(list(iterator)))
                    elif outcome == "cancel":
                        next(iterator)
                        iterator.close()
                    else:
                        with self.assertRaises(ExtractionError) as caught:
                            list(iterator)
                        self.assertEqual("limit_exceeded" if outcome == "limit" else "parse_failed",
                                         caught.exception.code)
                self.assertEqual(1, len(connections))
                for connection, database in zip(connections, databases):
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")
                    self.assertFalse(database.parent.exists())

    def test_zip_bomb_budget_and_traversal_are_rejected(self):
        path = self.archive("large.docx", {"word/document.xml": "a" * 4096})
        self.assertCode("limit_exceeded", path, {"max_uncompressed_bytes": 100})
        path = self.archive("unsafe.docx", {"../outside": "x"})
        self.assertCode("unsafe_archive", path)

    def test_xml_entities_and_dtd_are_rejected(self):
        xml = f'<!DOCTYPE x [<!ENTITY test "unsafe">]><w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&test;</w:t></w:r></w:p></w:body></w:document>'
        self.assertCode("unsafe_xml", self.archive("unsafe.docx", {"word/document.xml": xml}))

    def test_docx_heading_and_table_locators(self):
        xml = f'<w:document xmlns:w="{W}"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Payment</w:t></w:r></w:p><w:p><w:r><w:t>Within 30 days</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Deposit</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>25%</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
        results = list(iter_segments(self.archive("terms.docx", {"word/document.xml": xml}), {}))
        self.assertEqual(3, len(results))
        self.assertEqual("Payment", results[1]["locator"]["heading"])
        self.assertEqual("table_row", results[2]["locator"]["kind"])
        self.assertEqual("25%", results[2]["data"]["cells"][1]["value"])

    def test_unsupported_word_structure_is_not_silent_success(self):
        self.assertCode("unsupported_structure", self.archive("unknown.docx", {"word/document.xml": '<document xmlns="urn:unsupported"/>'}))
        nested = f'<w:document xmlns:w="{W}"><w:body><w:tbl><w:tr><w:tc><w:tbl/></w:tc></w:tr></w:tbl></w:body></w:document>'
        self.assertCode("unsupported_structure", self.archive("nested.docx", {"word/document.xml": nested}))

    def test_ppt_order_text_table_and_notes(self):
        members = {
            "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="256" r:id="slide"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{PR}"><Relationship Id="slide" Type="{R}/slide" Target="slides/slide8.xml"/></Relationships>',
            "ppt/slides/slide8.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Overview</a:t></a:r></a:p></p:txBody></p:sp><p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Budget</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>',
            "ppt/slides/_rels/slide8.xml.rels": f'<Relationships xmlns="{PR}"><Relationship Id="notes" Type="{R}/notesSlide" Target="../notesSlides/notesSlide8.xml"/><Relationship Id="external" Type="{R}/hyperlink" Target="https://example.invalid" TargetMode="External"/></Relationships>',
            "ppt/notesSlides/notesSlide8.xml": f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><a:p><a:r><a:t>Speaker note</a:t></a:r></a:p></p:notes>',
        }
        results = list(iter_segments(self.archive("deck.pptx", members), {}))
        self.assertEqual(["slide_text", "slide_table_row", "slide_notes"], [s["locator"]["kind"] for s in results])
        self.assertTrue(all(s["locator"]["slide"] == 1 for s in results))
        self.assertEqual("Speaker note", results[-1]["text"])

    def test_ppt_explicit_line_breaks_preserve_numeric_separation(self):
        from defusedxml import ElementTree
        shape = ElementTree.fromstring(f'<p:sp xmlns:p="{P}" xmlns:a="{A}"><a:p><a:r><a:t>100</a:t></a:r><a:br/><a:r><a:t>200</a:t></a:r><a:tab/><a:fld><a:t>300</a:t></a:fld></a:p></p:sp>')
        self.assertEqual(list(_drawing_paragraphs(shape)), ["100\n200\t300"])

    def make_pdf(self, name="document.pdf", blank=False, encrypted=False):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        if not blank:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 20 200 Td (Approved budget 123.45) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
        if encrypted:
            writer.encrypt("example-test-password")
        path = self.root / name
        with path.open("wb") as handle:
            writer.write(handle)
        return path

    def test_pdf_text_citation_and_limitations(self):
        result = list(iter_segments(self.make_pdf(), {}))[0]
        self.assertEqual(1, result["locator"]["page"])
        self.assertIn("123.45", result["text"])
        self.assertIn("pdf_layout_and_tables_not_verified", result["warnings"])

    def test_pdf_missing_text_is_an_explicit_page_gap(self):
        iterator = iter_segments(self.make_pdf(blank=True), {})
        result = next(iterator)
        self.assertIn("no_extractable_text_ocr_may_be_required", result["warnings"])
        self.assertEqual(list(iterator), [])
        self.assertCode("encrypted", self.make_pdf(encrypted=True))

    def test_cli_jsonl_and_sanitized_errors(self):
        path = self.file("simple.txt", "fixture\n")
        env = dict(os.environ)
        process = subprocess.run([sys.executable, "-m", "team_drive.extract", str(path), "{}"], env=env, text=True, capture_output=True)
        self.assertEqual(0, process.returncode, process.stderr)
        lines = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual({"done": True}, lines[-1])
        self.assertEqual("fixture\n", lines[0]["segment"]["text"])
        bad = subprocess.run([sys.executable, "-m", "team_drive.extract", str(self.root / "private.txt"), "{}"], env=env, text=True, capture_output=True)
        self.assertEqual(1, bad.returncode)
        self.assertNotIn(str(self.root), bad.stdout + bad.stderr)
        self.assertEqual("source_unavailable", json.loads(bad.stdout)["error"]["code"])


if __name__ == "__main__":
    unittest.main()
