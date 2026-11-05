"""State and source-boundary regression tests with synthetic temporary files."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from team_drive.engine import DriveError, Engine, MAX_RESPONSE_BYTES


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"provider": "local", "root": "sources", "state_dir": "state",
                                           "aliases": {"花费": ["支出"]}}), encoding="utf-8")
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text(content, encoding="utf-8")
        return target

    def file_id(self, path):
        rows = self.engine.list(limit=100)["files"]
        return next(row["file_id"] for row in rows if row["path"] == path)

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privilege unavailable; junction tests still run")
            raise

    def test_new_changed_deleted_index(self):
        target = self.write("campaign/budget.txt", "SIMULATED campaign budget 100\n")
        self.engine.scan_once()
        file_id = self.file_id("campaign/budget.txt")
        first = self.engine.read(file_id)
        self.assertTrue(first["extraction_complete"])
        self.assertIn("100", first["segments"][0]["text"])
        target.write_text("SIMULATED campaign budget 150\n", encoding="utf-8")
        self.engine.scan_once()
        second = self.engine.read(file_id)
        self.assertNotEqual(first["source_version"], second["source_version"])
        self.assertIn("150", second["segments"][0]["text"])
        target.unlink()
        self.engine.scan_once()
        self.assertEqual(self.engine.list()["files"], [])
        self.assertEqual(self.engine.search("budget")["files"], [])
        self.assertEqual(self.engine.call("read", {"file_id": file_id})["error"]["code"], "FILE_NOT_FOUND")

    def test_chinese_short_keywords_alias_and_name(self):
        self.write("资料/项目A.txt", "模拟数据，预算100元，支出50元，小红书投放。\n")
        self.engine.scan_once()
        for term in ("预算", "预", "小红书", "花费", "项目A"):
            result = self.engine.search(term)
            self.assertEqual(result["total_matching_files"], 1, term)
            self.assertFalse(result["exhaustive_for_business_totals"])
            self.assertFalse(result["files"][0]["match"]["snippet_is_evidence"])
        self.assertEqual(self.engine.search("不存在")["files"], [])

    def test_pagination_cursors_bind_query_generation_and_version(self):
        for n in range(4):
            self.write("budget-%s.txt" % n, "SIMULATED budget\n" * 12)
        self.engine.scan_once()
        first = self.engine.list(limit=2)
        second = self.engine.list(limit=2, cursor=first["next_cursor"])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({r["file_id"] for r in first["files"] + second["files"]}), 4)
        search = self.engine.search("budget", limit=1)
        bad = self.engine.call("search", {"query": "SIMULATED", "cursor": search["next_cursor"]})
        self.assertEqual(bad["error"]["code"], "STALE_CURSOR")
        self.write("new.txt", "new content")
        self.engine.scan_once()
        changed = self.engine.call("list", {"cursor": first["next_cursor"]})
        self.assertEqual(changed["error"]["code"], "STALE_CURSOR")
        file_id = first["files"][0]["file_id"]
        read = self.engine.read(file_id, limit=1)
        if read["has_more"]:
            next_read = self.engine.read(file_id, limit=1, cursor=read["next_cursor"], expected_version=read["source_version"])
            self.assertEqual(next_read["start"], 1)

    def test_digest_catches_same_size_and_mtime_change(self):
        target = self.write("source.txt", "value 100")
        self.engine.scan_once()
        file_id = self.file_id("source.txt")
        before = target.stat()
        initial = self.engine.inspect(file_id)["source_version"]
        target.write_text("value 200", encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        stale = self.engine.call("read", {"file_id": file_id, "expected_version": initial})
        self.assertEqual(stale["error"]["code"], "STALE_SOURCE")
        self.assertEqual(self.engine.search("100")["files"], [])
        self.engine.scan_once()
        self.assertIn("200", self.engine.read(file_id)["segments"][0]["text"])

    def test_path_boundary_and_symlink_are_not_read(self):
        outside = self.base / "private.txt"
        outside.write_text("TOP_SECRET_NEVER_READ")
        self.symlink(self.root / "leak.txt", outside)
        self.symlink(self.root / "linked_dir", self.base, directory=True)
        self.engine.scan_once()
        rows = self.engine.list()["files"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "blocked" for row in rows))
        self.assertEqual(self.engine.search("TOP_SECRET")["files"], [])
        self.assertEqual(self.engine.call("list", {"prefix": "../"})["error"]["code"], "INVALID_PATH")
        self.assertEqual(self.engine.call("list", {"prefix": "/private"})["error"]["code"], "INVALID_PATH")

    def test_replaced_directory_with_symlink_rejected_on_read(self):
        self.write("nested/file.txt", "allowed")
        self.engine.scan_once()
        file_id = self.file_id("nested/file.txt")
        (self.root / "nested/file.txt").unlink()
        (self.root / "nested").rmdir()
        (self.base / "file.txt").write_text("outside")
        self.symlink(self.root / "nested", self.base, directory=True)
        result = self.engine.call("read", {"file_id": file_id})
        self.assertIn(result["error"]["code"], ("SOURCE_UNAVAILABLE", "UNSAFE_FILE"))
        self.assertNotIn("outside", json.dumps(result))

    def test_export_controlled_complete_and_version_required(self):
        self.write("data.csv", "item,value\nA,10\nB,20\n")
        self.engine.scan_once()
        file_id = self.file_id("data.csv")
        version = self.engine.inspect(file_id)["source_version"]
        result = self.engine.export(file_id, version)
        self.assertTrue(result["artifact_id"].startswith("exports/"))
        self.assertEqual(Path(result["artifact_path"]), (self.base / "state" / result["artifact_id"]).resolve())
        lines = (self.base / "state" / result["artifact_id"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(result["row_count"], len(lines))
        self.assertTrue(all(json.loads(line)["source_version"] == version for line in lines))
        self.assertNotIn(str(self.base), "\n".join(lines))
        self.assertEqual(self.engine.call("export", {"file_id": file_id, "expected_version": "old"})["error"]["code"], "VERSION_MISMATCH")
        self.assertEqual(self.engine.call("export", {"file_id": file_id, "expected_version": version, "output_path": "/tmp/bad"})["error"]["code"], "INVALID_PARAMS")

    def test_failed_refresh_does_not_keep_old_searchable_content(self):
        target = self.write("data.csv", "name,value\noldkeyword,10\n")
        self.engine.scan_once()
        file_id = self.file_id("data.csv")
        self.assertEqual(self.engine.search("oldkeyword")["total_matching_files"], 1)
        target.write_bytes(b"\xff\xfe\x00")
        self.engine.scan_once()
        self.assertEqual(self.engine.search("oldkeyword")["total_matching_files"], 0)
        self.assertFalse(self.engine.call("read", {"file_id": file_id})["ok"])
        self.assertFalse(self.engine.status()["coverage"]["all_present_files_indexed"])

    def test_unsupported_and_large_files_are_visible_in_coverage(self):
        self.write("unknown.xyz", "unknown format")
        self.engine.limits["max_file_bytes"] = 10
        self.write("big.txt", "a" * 100)
        self.engine.scan_once()
        states = self.engine.status()["coverage"]["states"]
        self.assertEqual(states.get("oversized"), 2)
        self.assertFalse(self.engine.status()["coverage"]["all_present_files_indexed"])

    def test_configuration_limits_and_provider(self):
        for config, code in [
            ({"provider": "sharepoint", "root": "sources", "state_dir": "state2"}, "UNSUPPORTED_PROVIDER"),
            ({"root": "sources", "state_dir": "sources/cache"}, "UNSAFE_STATE"),
            ({"root": "sources", "state_dir": "state2", "limits": {"max_file_bytes": 0}}, "INVALID_CONFIG"),
        ]:
            self.config.write_text(json.dumps(config))
            with self.assertRaises(DriveError) as caught:
                Engine(str(self.config), autostart=False)
            self.assertEqual(caught.exception.code, code)

    def test_bound_output_and_unknown_params(self):
        self.write("large.txt", "预算" * 20000)
        self.engine.scan_once()
        file_id = self.file_id("large.txt")
        result = self.engine.call("read", {"file_id": file_id, "limit": 100})
        self.assertTrue(result["ok"], result)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), MAX_RESPONSE_BYTES)
        self.assertTrue(result["has_more"])
        self.assertEqual(self.engine.call("read", {"file_id": file_id, "limit": 101})["error"]["code"], "INVALID_LIMIT")
        self.assertEqual(self.engine.call("not_real")["error"]["code"], "UNKNOWN_METHOD")

    def test_background_worker_runs_and_startup_refresh(self):
        self.write("startup.txt", "SIMULATED startup")
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.engine.call("job_status")
            if status.get("job", {}).get("status") == "complete":
                break
            time.sleep(.05)
        self.assertEqual(self.engine.list()["files"][0]["status"], "ready")

    def test_state_is_bound_to_root_and_only_one_worker(self):
        with self.assertRaises(DriveError) as caught:
            Engine(str(self.config), autostart=False)
        self.assertEqual(caught.exception.code, "STATE_IN_USE")
        self.engine.close()
        (self.base / "different").mkdir()
        self.config.write_text(json.dumps({"root": "different", "state_dir": "state"}))
        with self.assertRaises(DriveError) as caught:
            Engine(str(self.config), autostart=False)
        self.assertEqual(caught.exception.code, "STATE_ROOT_MISMATCH")

    def test_config_changes_invalidate_old_parses(self):
        self.write("source.txt", "SIMULATED budget")
        self.engine.scan_once()
        self.assertEqual(self.engine.status()["coverage"]["indexed_files"], 1)
        self.engine.close()
        config = json.loads(self.config.read_text())
        config["limits"] = {"max_segment_chars": 4000}
        self.config.write_text(json.dumps(config))
        self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine.status()["coverage"]["indexed_files"], 0)
        self.assertEqual(self.engine.search("budget")["files"], [])
        self.engine.scan_once()
        self.assertEqual(self.engine.search("budget")["total_matching_files"], 1)

    def test_unchanged_failure_not_retried_without_force(self):
        self.write("source.txt", "SIMULATED")
        self.engine.limits["max_segments"] = 1
        self.write("source.txt", "first\nsecond\nthird\n")
        self.engine.scan_once()
        file_id = self.file_id("source.txt")
        self.assertEqual(self.engine.list()["files"][0]["status"], "failed")
        self.engine.limits["max_segments"] = 10
        self.engine.scan_once()
        self.assertEqual(self.engine.list()["files"][0]["status"], "failed")
        self.engine.scan_once(force=True)
        self.assertTrue(self.engine.call("read", {"file_id": file_id})["ok"])

    def test_structure_paginated_blocks_allow_targeted_reads(self):
        self.write("long.txt", "\n".join("line %s" % n for n in range(1, 602)))
        self.engine.scan_once()
        file_id = self.file_id("long.txt")
        info = self.engine.inspect(file_id, limit=1)
        self.assertTrue(info["has_more"])
        self.assertEqual(info["total_structure_groups"], 4)
        second = self.engine.inspect(file_id, limit=1, cursor=info["next_cursor"])
        self.assertEqual(second["structure"][0]["start"], 200)
        result = self.engine.read(file_id, start=second["structure"][0]["start"], limit=1,
                                  expected_version=second["source_version"])
        self.assertIn("201", result["segments"][0]["text"])

    def test_csv_row_selection_and_cursor_binding(self):
        self.write("data.csv", "item,value\nA,10\nB,20\nC,30\n")
        self.engine.scan_once()
        file_id = self.file_id("data.csv")
        selection = {"row_start": 2, "row_end": 3}
        first = self.engine.read(file_id, limit=1, selector=selection)
        self.assertEqual(first["total_segments"], 4)
        self.assertEqual(first["total_matching_segments"], 2)
        self.assertEqual(first["segments"][0]["locator"]["row"], 2)
        self.assertFalse(first["whole_file_in_response"])
        self.assertTrue(first["has_more"])
        second = self.engine.read(file_id, limit=1, selector=selection, cursor=first["next_cursor"])
        self.assertEqual(second["start"], 1)
        self.assertEqual(second["segments"][0]["locator"]["row"], 3)
        self.assertFalse(second["has_more"])
        wrong = self.engine.call("read", {"file_id": file_id, "selector": {"row_start": 1}, "cursor": first["next_cursor"]})
        self.assertEqual(wrong["error"]["code"], "STALE_CURSOR")
        offset = self.engine.read(file_id, start=1, selector=selection)
        self.assertEqual(offset["segments"][0]["locator"]["row"], 3)
        empty = self.engine.read(file_id, selector={"row_start": 99})
        self.assertEqual(empty["total_matching_segments"], 0)
        self.assertEqual(empty["segments"], [])
        self.assertEqual(empty["source_version"], first["source_version"])
        self.assertFalse(empty["whole_file_in_response"])

    def test_selector_rejects_incompatible_fields_and_types(self):
        self.write("data.csv", "a,b\n1,2\n")
        self.write("notes.txt", "note")
        self.engine.scan_once()
        file_id = self.file_id("data.csv")
        for selector in ({}, {"row_start": 0}, {"row_end": True}, {"row_start": 3, "row_end": 2}, {"cell": "A1"}):
            self.assertEqual(self.engine.call("read", {"file_id": file_id, "selector": selector})["error"]["code"], "INVALID_SELECTOR")
        for selector in ({"sheet": "data"}, {"row_start": 1, "page": 1}, {"slide": 1}):
            self.assertEqual(self.engine.call("read", {"file_id": file_id, "selector": selector})["error"]["code"], "UNSUPPORTED_SELECTOR")
        self.assertEqual(self.engine.call("read", {"file_id": self.file_id("notes.txt"), "selector": {"row_start": 1}})["error"]["code"], "UNSUPPORTED_SELECTOR")

    def test_xlsx_sheet_and_row_selection_uses_source_coordinates(self):
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        relations = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        package = "http://schemas.openxmlformats.org/package/2006/relationships"
        with zipfile.ZipFile(self.root / "book.xlsx", "w") as output:
            output.writestr("xl/workbook.xml", '<workbook xmlns="%s" xmlns:r="%s"><sheets><sheet name="Approved" sheetId="1" r:id="one"/><sheet name="Draft" sheetId="2" r:id="two"/></sheets></workbook>' % (namespace, relations))
            output.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="%s"><Relationship Id="one" Type="%s/worksheet" Target="worksheets/one.xml"/><Relationship Id="two" Type="%s/worksheet" Target="worksheets/two.xml"/></Relationships>' % (package, relations, relations))
            for name, amount in (("one", 100), ("two", 200)):
                output.writestr("xl/worksheets/%s.xml" % name, '<worksheet xmlns="%s"><sheetData><row r="1"><c r="A1"><v>1</v></c></row><row r="9000"><c r="A9000"><v>%s</v></c></row></sheetData></worksheet>' % (namespace, amount))
        self.engine.scan_once()
        file_id = self.file_id("book.xlsx")
        result = self.engine.read(file_id, selector={"sheet": "Approved", "row_start": 9000, "row_end": 9000})
        self.assertEqual(result["total_matching_segments"], 1)
        self.assertEqual(result["segments"][0]["data"]["cells"][0]["value"], "100")
        self.assertTrue(result["selection_complete_in_response"])
        self.assertFalse(result["whole_file_in_response"])
        self.assertEqual(self.engine.call("read", {"file_id": file_id, "selector": {"row_start": 9000}})["error"]["code"], "UNSUPPORTED_SELECTOR")
        missing = self.engine.read(file_id, selector={"sheet": "Missing"})
        self.assertEqual(missing["total_matching_segments"], 0)

    def test_pdf_page_selection(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        for number in (1, 2):
            page = writer.add_blank_page(width=300, height=300)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(("BT /F1 12 Tf 20 200 Td (SIMULATED page %s) Tj ET" % number).encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
        with (self.root / "pages.pdf").open("wb") as handle:
            writer.write(handle)
        self.engine.scan_once()
        file_id = self.file_id("pages.pdf")
        result = self.engine.read(file_id, selector={"page": 2})
        self.assertEqual(result["total_segments"], 2)
        self.assertEqual(result["total_matching_segments"], 1)
        self.assertEqual(result["segments"][0]["locator"]["page"], 2)
        self.assertIn("page 2", result["segments"][0]["text"])
        self.assertFalse(result["whole_file_in_response"])
        self.assertEqual(self.engine.call("read", {"file_id": file_id, "selector": {"page": 2, "slide": 1}})["error"]["code"], "UNSUPPORTED_SELECTOR")

    def test_direct_import_engine_sets_its_own_parser_module_path(self):
        self.write("direct.txt", "SIMULATED direct import\n")
        config = json.loads(self.config.read_text())
        config["state_dir"] = "subprocess_state"
        direct_config = self.base / "direct.json"
        direct_config.write_text(json.dumps(config))
        package_parent = str(Path(__file__).resolve().parents[1] / "python")
        script = ("import sys,json; sys.path.insert(0, " + repr(package_parent) + "); "
                  "from team_drive.engine import Engine; engine=Engine(sys.argv[1],autostart=False); "
                  "engine.scan_once(); print(json.dumps(engine.list())); engine.close()")
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, "-c", script, str(direct_config)],
                                env=environment, cwd=str(self.base), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["files"][0]["status"], "ready", response)
        self.assertEqual(response["files"][0]["segments_count"], 1)

    def test_short_text_inspection_labels_bucket_bounds_not_file_extent(self):
        self.write("short.txt", "\n".join("line %d" % n for n in range(1, 8)))
        self.engine.scan_once()
        result = self.engine.inspect(self.file_id("short.txt"))
        group = result["structure"][0]
        self.assertEqual(group["group"]["kind"], "line_bucket")
        self.assertEqual(group["group"]["bucket_line_end"], 200)
        self.assertNotIn("line_end", group["group"])
        self.assertEqual(group["end"], 6)
        self.assertEqual(group["segments"], 7)

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO regression; Windows rejects nonregular/reparse inputs separately")
    def test_indexed_file_replaced_with_fifo_returns_without_blocking(self):
        target = self.write("fifo.txt", "allowed regular file")
        self.engine.scan_once()
        file_id = self.file_id("fifo.txt")
        self.engine.close()
        package_parent = str(Path(__file__).resolve().parents[1] / "python")
        script = ("import sys,json,os; from pathlib import Path; sys.path.insert(0," + repr(package_parent) + "); "
                  "from team_drive.engine import Engine; engine=Engine(sys.argv[1],autostart=False); "
                  "target=Path(sys.argv[2]); target.unlink(); os.mkfifo(str(target)); "
                  "print(json.dumps(engine.call('read',{'file_id':sys.argv[3]}))); "
                  "print(json.dumps(engine.call('status'))); engine.close()")
        try:
            process = subprocess.run([sys.executable, "-c", script, str(self.config), str(target), file_id],
                                     capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("A replaced FIFO blocked the read operation")
        self.assertEqual(process.returncode, 0, process.stderr)
        result, status = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(result["error"]["code"], "UNSAFE_FILE")
        self.assertTrue(status["ok"])

    def _rebind_fixture(self):
        self.engine.close()
        parent = self.base / "source-parent"
        approved = parent / "approved"
        approved.mkdir(parents=True)
        (approved / "allowed.txt").write_text("APPROVED_LOCAL_CONTENT")
        outside = self.base / "outside"
        (outside / "approved").mkdir(parents=True)
        (outside / "approved" / "allowed.txt").write_text("OUTSIDE_SECRET_CONTENT")
        (outside / "approved" / "outside-name.txt").write_text("OUTSIDE_SECRET_CONTENT")
        self.config.write_text(json.dumps({"root": "source-parent/approved", "state_dir": "anchored-state"}))
        self.engine = Engine(str(self.config), autostart=False)
        return parent, outside

    @unittest.skipUnless(os.name == "posix", "Windows pins directory chains against rename; tested separately")
    def test_ancestor_symlink_rebinding_rejects_read_and_inventory(self):
        parent, outside = self._rebind_fixture()
        self.engine.scan_once()
        file_id = self.file_id("allowed.txt")
        parent.rename(self.base / "original-parent")
        parent.symlink_to(outside, target_is_directory=True)
        for method, args in (("read", {"file_id": file_id}), ("refresh", {}), ("list", {}), ("search", {"query": "OUTSIDE"})):
            result = self.engine.call(method, args)
            self.assertEqual(result["error"]["code"], "SOURCE_ROOT_CHANGED")
            self.assertNotIn("OUTSIDE_SECRET_CONTENT", json.dumps(result))
        with self.assertRaises(DriveError) as caught:
            self.engine.scan_once()
        self.assertEqual(caught.exception.code, "SOURCE_ROOT_CHANGED")
        with self.engine._db() as db:
            paths = [row[0] for row in db.execute("SELECT path FROM files")]
        self.assertEqual(paths, ["allowed.txt"])

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor walk regression")
    def test_inventory_remains_fd_anchored_if_parent_changes_mid_scan(self):
        parent, outside = self._rebind_fixture()
        original_fwalk = os.fwalk
        def rebind_then_walk(*args, **kwargs):
            parent.rename(self.base / "original-parent")
            parent.symlink_to(outside, target_is_directory=True)
            yield from original_fwalk(*args, **kwargs)
        with mock.patch("team_drive.engine.os.fwalk", rebind_then_walk):
            with self.assertRaises(DriveError) as caught:
                self.engine.scan_once()
        self.assertEqual(caught.exception.code, "SOURCE_ROOT_CHANGED")
        with self.engine._db() as db:
            paths = [row[0] for row in db.execute("SELECT path FROM files")]
        self.assertTrue(set(paths).issubset({"allowed.txt"}), paths)

    def test_unchanged_unreadable_files_do_not_invalidate_inventory_cursor(self):
        self.write("photo.png", "image")
        self.write("archive.zip", "zip")
        self.write("oversized.txt", "x" * 40)
        self.engine.limits["max_file_bytes"] = 20
        self.engine.scan_once()
        first = self.engine.list(limit=1)
        generation = first["index_generation"]
        self.engine.scan_once()
        continuation = self.engine.list(limit=1, cursor=first["next_cursor"])
        self.assertEqual(continuation["index_generation"], generation)
        self.assertEqual(self.engine.status()["coverage"]["states"], {"unsupported": 2, "oversized": 1})

    def test_failed_parser_keeps_filename_searchable_without_stale_body(self):
        source = self.write("report-marker.txt", "obsoleteword")
        self.engine.scan_once()
        source.write_bytes(b"\xff\xfe\x00")
        self.engine.scan_once()
        self.assertEqual(self.engine.search("obsoleteword")["files"], [])
        matches = self.engine.search("report marker")["files"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "failed")
        self.engine.scan_once()
        self.assertEqual(len(self.engine.search("report marker")["files"]), 1)

    def test_stable_blocked_and_online_only_metadata_keep_pagination_valid(self):
        blocked = self.write("blocked.txt", "MUST NOT PARSE")
        online = self.write("online.txt", "MUST NOT PARSE")
        entries = [("blocked.txt", blocked.stat(), True, False), ("online.txt", online.stat(), False, True)]
        with mock.patch.object(self.engine._source, "walk", return_value=entries), \
             mock.patch.object(self.engine, "_parse_file", side_effect=AssertionError("Excluded source parsed")):
            self.engine.scan_once()
            first = self.engine.list(limit=1)
            self.engine.scan_once()
            next_page = self.engine.list(limit=1, cursor=first["next_cursor"])
        self.assertEqual(next_page["index_generation"], first["index_generation"])
        self.assertEqual(self.engine.status()["coverage"]["states"], {"blocked": 1, "online_only": 1})

    def test_keyword_and_spans_name_and_body_with_stable_relevance_order(self):
        self.write("华夏.txt", "管理费\n0.20%\n")
        self.write("a.txt", "华夏 管理费\n")
        self.write("z-华夏-管理费.txt", "other\n")
        self.write("irrelevant.txt", "华夏\n")
        self.engine.scan_once()
        first = self.engine.search("华夏 管理费", limit=2)
        second = self.engine.search("华夏 管理费", limit=2, cursor=first["next_cursor"])
        files = first["files"] + second["files"]
        self.assertEqual([file["path"] for file in files], ["z-华夏-管理费.txt", "a.txt", "华夏.txt"])
        self.assertTrue(files[-1]["match"]["terms_matched_across_segments"])
        self.write("nested/华夏.txt", "管理费")
        self.engine.scan_once()
        restricted = self.engine.search("华夏 管理费", prefix="nested")
        self.assertEqual([file["path"] for file in restricted["files"]], ["nested/华夏.txt"])

    def test_unchanged_poll_does_not_rewrite_entire_checkpoint(self):
        self.write("one.txt", "one")
        with mock.patch.object(self.engine._storage, "checkpoint_database", wraps=self.engine._storage.checkpoint_database) as checkpoint:
            self.engine.scan_once()
            self.assertEqual(checkpoint.call_count, 1)
            self.engine.scan_once()
            self.assertEqual(checkpoint.call_count, 1)
            self.engine.close()
            self.assertEqual(checkpoint.call_count, 2)

    def test_inventory_time_handles_windows_stat_fstat_creation_semantics(self):
        from types import SimpleNamespace
        by_path = SimpleNamespace(st_ctime_ns=10, st_birthtime_ns=10)
        by_fd = SimpleNamespace(st_ctime_ns=20, st_birthtime_ns=10)
        legacy_or_cloud = SimpleNamespace(st_ctime_ns=30)
        with mock.patch("team_drive.engine.os.name", "nt"):
            self.assertEqual(Engine._inventory_time(by_path), Engine._inventory_time(by_fd))
            self.assertEqual(Engine._inventory_time(legacy_or_cloud), 30)
        with mock.patch("team_drive.engine.os.name", "posix"):
            self.assertEqual(Engine._inventory_time(by_fd), 20)

    def test_compact_cursors_bind_sessions_and_large_unicode_filters(self):
        binding = {"method": "search", "query": "预算" * 250, "prefix": "文" * 1024, "generation": 42}
        cursor = self.engine._cursor(binding, 17)
        self.assertLess(len(cursor), 256)
        self.assertEqual(self.engine._offset(cursor, binding), 17)
        self.write("one.txt", "one")
        self.write("two.txt", "two")
        self.engine.scan_once()
        first = self.engine.call("list", {"limit": 1}, session_id="client-a")
        other = self.engine.call("list", {"limit": 1, "cursor": first["next_cursor"]}, session_id="client-b")
        self.assertEqual(other["error"]["code"], "INVALID_CURSOR")
        continued = self.engine.call("list", {"limit": 1, "cursor": first["next_cursor"]}, session_id="client-a")
        self.assertTrue(continued["ok"])

    def test_nonfinite_and_excessive_poll_values_fail_before_state_creation(self):
        for value in (float("inf"), float("nan"), -float("inf"), 86401, 10 ** 1000):
            with self.subTest(value=value):
                self.config.write_text(json.dumps({"root": "sources", "state_dir": "new-state", "poll_seconds": value}), encoding="utf-8")
                with self.assertRaises(DriveError) as caught:
                    Engine(str(self.config), autostart=False)
                self.assertEqual(caught.exception.code, "INVALID_CONFIG")
                self.assertFalse((self.base / "new-state").exists())

    def test_fatal_index_thread_error_is_visible_and_releases_state(self):
        self.engine.refresh()
        with mock.patch.object(self.engine, "scan_once", side_effect=sqlite3.OperationalError("synthetic disk full")), \
             mock.patch.object(self.engine, "_db", side_effect=sqlite3.OperationalError("synthetic disk full")):
            self.engine._run()
        self.assertEqual(self.engine.call("status")["error"]["code"], "INDEXER_UNHEALTHY")
        self.assertIsNone(self.engine._sqlite_conn)
        self.assertIsNone(self.engine._storage.lock_fd)

    def test_state_override_uses_same_boundaries(self):
        with self.assertRaises(DriveError) as caught:
            Engine(str(self.config), autostart=False, state_override=self.root / "bad")
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        self.assertFalse((self.root / "bad").exists())
        other = Engine(str(self.config), autostart=False, state_override=self.base / "broker-state")
        try:
            self.assertEqual(other.state, (self.base / "broker-state").resolve())
        finally:
            other.close()

    def test_shutdown_interrupted_parse_is_retried_after_reopen(self):
        self.write("interrupted.txt", "resumable evidence")
        self.engine.close()
        started = threading.Event()
        def interrupted(engine, snapshot, parsed):
            started.set()
            if not engine._stop.wait(5):
                raise AssertionError("Test shutdown did not arrive")
            raise DriveError("PARSE_FAILED", "Synthetic killed parser")
        with mock.patch.object(Engine, "_extract", autospec=True, side_effect=interrupted):
            self.engine = Engine(str(self.config), autostart=True)
            self.assertTrue(started.wait(3))
            self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine.list()["files"][0]["status"], "pending")
        self.engine.scan_once()
        file = self.engine.list()["files"][0]
        self.assertEqual(file["status"], "ready")
        self.assertIn("resumable evidence", self.engine.read(file["file_id"])["segments"][0]["text"])


if __name__ == "__main__":
    unittest.main()
