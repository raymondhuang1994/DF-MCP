"""Source immutability and Windows boundary tests use disposable synthetic data only."""
import csv
import hashlib
import io
import json
import os
import stat
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from team_drive.engine import Engine, DriveError


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-readonly-")
        self.base = Path(self.temp.name)
        self.root = self.base / "团队资料 with spaces"
        self.root.mkdir()
        self.file = self.root / "预算.csv"
        self.file.write_text("项目,金额\n模拟项目,123.45\n", encoding="utf-8")
        (self.root / "notes.md").write_text("SIMULATED 只读资料\n", encoding="utf-8")
        self.state = self.base / "private-state"
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"root": str(self.root), "state_dir": str(self.state)}), encoding="utf-8")
        self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        self.temp.cleanup()

    def start(self):
        self.engine = Engine(str(self.config), autostart=False)
        return self.engine

    def snapshot(self, directory=None):
        directory = directory or self.root
        result = {}
        for item in sorted(directory.rglob("*")):
            rel = item.relative_to(directory).as_posix()
            info = item.stat()
            result[rel] = ("dir" if item.is_dir() else hashlib.sha256(item.read_bytes()).hexdigest(), info.st_size if item.is_file() else 0,
                           info.st_mtime_ns, stat.S_IMODE(info.st_mode))
        return result

    def exercise_all_tools(self):
        before = self.snapshot()
        engine = self.start()
        engine.scan_once()
        self.assertTrue(engine.call("status")["read_only_sources"])
        inventory = engine.call("list")
        self.assertEqual(len(inventory["files"]), 2)
        hits = engine.call("search", {"query": "预算"})
        file_id = hits["files"][0]["file_id"]
        version = engine.call("inspect", {"file_id": file_id})["source_version"]
        read = engine.call("read", {"file_id": file_id, "expected_version": version})
        self.assertTrue(read["ok"], read)
        self.assertIn("123.45", json.dumps(read, ensure_ascii=False))
        exported = engine.call("export", {"file_id": file_id, "expected_version": version})
        self.assertTrue(exported["ok"], exported)
        self.assertTrue(Path(exported["artifact_path"]).is_relative_to(self.state.resolve()))
        job_id = engine.call("refresh")["job_id"]
        self.assertTrue(engine.call("job_status", {"job_id": job_id})["ok"])
        self.assertTrue(engine.call("cancel", {"job_id": job_id})["ok"])
        for method in ("write", "delete", "rename", "move", "chmod"):
            self.assertEqual(engine.call(method, {"path": "预算.csv"})["error"]["code"], "UNKNOWN_METHOD")
        self.assertEqual(before, self.snapshot())
        engine.close()
        self.assertEqual(before, self.snapshot())

    def test_all_nine_tools_preserve_source_content_names_and_modification_times(self):
        self.exercise_all_tools()

    def test_opened_source_descriptor_has_no_write_access(self):
        before = self.snapshot()
        engine = self.start()
        fd, _ = engine._open_source("预算.csv")
        try:
            with self.assertRaises(OSError):
                os.write(fd, b"MUST_NOT_WRITE")
        finally:
            os.close(fd)
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(os.name == "posix", "POSIX permissions; Windows ACL tested separately")
    def test_reading_and_export_with_posix_readonly_source(self):
        for file in self.root.iterdir():
            file.chmod(0o444)
        self.root.chmod(0o555)
        try:
            self.exercise_all_tools()
        finally:
            self.root.chmod(0o755)
            for file in self.root.iterdir():
                file.chmod(0o644)

    @unittest.skipUnless(os.name == "nt", "Windows DACL enforcement")
    def test_reading_and_export_with_windows_write_and_delete_denied(self):
        # The only ACL changed is this test's generated source tree, never a user directory.
        self.assertEqual(self.root.parent, self.base)
        account = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], check=True, capture_output=True, text=True)
        sid = list(csv.reader(io.StringIO(account.stdout.strip())))[0][1]
        self.assertRegex(sid, r"^S-1-[0-9-]+$")
        original = self.file.read_bytes()
        # Do not deny generic W: it includes synchronization/read-control bits
        # also requested by normal GENERIC_READ handles. Deny specific mutation
        # rights while preserving the test's required read/list precondition.
        # https://learn.microsoft.com/windows-server/administration/windows-commands/icacls
        deny = "*" + sid + ":(OI)(CI)(WD,AD,WEA,WA,DE,DC)"
        result = subprocess.run(["icacls", str(self.root), "/deny", deny, "/Q"], capture_output=True)
        self.assertEqual(result.returncode, 0, "Failed to apply a test-only read-only DACL")
        try:
            self.assertEqual(self.file.read_bytes(), original, "The read-only DACL must still permit ordinary file reads")
            self.assertEqual(len(list(self.root.iterdir())), 2, "The read-only DACL must still permit directory listing")
            with self.assertRaises(PermissionError):
                with self.file.open("ab") as output:
                    output.write(b"MUST_NOT_WRITE")
            with self.assertRaises(PermissionError):
                self.file.unlink()
            with self.assertRaises(PermissionError):
                with (self.root / "MUST_NOT_CREATE.txt").open("xb") as output:
                    output.write(b"MUST_NOT_WRITE")
            with self.assertRaises(PermissionError):
                self.file.rename(self.root / "MUST_NOT_RENAME.csv")
            self.exercise_all_tools()
        finally:
            if self.engine:
                self.engine.close()
            restored = subprocess.run(["icacls", str(self.root), "/remove:d", "*" + sid, "/T", "/C", "/Q"], capture_output=True)
            self.assertEqual(restored.returncode, 0, "Failed to restore the temporary test DACL")

    def test_hardlinked_sources_are_blocked(self):
        outside = self.base / "outside.txt"
        outside.write_text("OUTSIDE_HARDLINK_PAYLOAD", encoding="utf-8")
        os.link(outside, self.root / "hardlink.txt")
        engine = self.start()
        engine.scan_once()
        hit = next(row for row in engine.list()["files"] if row["path"] == "hardlink.txt")
        self.assertEqual(hit["status"], "blocked")
        self.assertFalse(engine.call("read", {"file_id": hit["file_id"]})["ok"])
        self.assertEqual(engine.search("OUTSIDE_HARDLINK_PAYLOAD")["files"], [])

    def test_state_cannot_contain_source_or_be_inside_source(self):
        for state in (self.root / "state", self.root, self.base):
            self.config.write_text(json.dumps({"root": str(self.root), "state_dir": str(state)}), encoding="utf-8")
            with self.assertRaises(DriveError) as caught:
                Engine(str(self.config), autostart=False)
            self.assertEqual(caught.exception.code, "UNSAFE_STATE")

    def test_case_alias_cannot_place_state_inside_source(self):
        alias = self.root.with_name(self.root.name.swapcase())
        if not alias.exists() or not os.path.samefile(alias, self.root):
            self.skipTest("This test volume is case sensitive")
        before = self.snapshot()
        self.config.write_text(json.dumps({"root": str(self.root), "state_dir": str(alias / "state")}), encoding="utf-8")
        with self.assertRaises(DriveError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(os.name == "posix", "Windows pins private directories against rename")
    def test_export_directory_swap_at_file_creation_does_not_write_source(self):
        engine = self.start()
        engine.scan_once()
        file = engine.list()["files"][0]
        real_open = os.open
        swapped = []
        def change_before_create(path, flags, *args, **kwargs):
            if str(path).endswith(".jsonl") and flags & os.O_CREAT and not swapped:
                folder = self.state / "exports"
                folder.rename(self.state / "original-exports")
                folder.symlink_to(self.root, target_is_directory=True)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)
        before = self.snapshot()
        with mock.patch("os.open", change_before_create):
            result = engine.call("export", {"file_id": file["file_id"], "expected_version": file["source_version"]})
        self.assertTrue(swapped, "The fault injection must exercise a real output creation")
        self.assertFalse(result["ok"], result)
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(os.name == "posix", "Windows pins the SQLite file against replacement")
    def test_sqlite_path_swap_at_connect_cannot_modify_source_database(self):
        business = self.root / "business.sqlite"
        connection = sqlite3.connect(str(business))
        connection.execute("CREATE TABLE original_data(value TEXT)")
        connection.execute("INSERT INTO original_data VALUES('SIMULATED ORIGINAL')")
        connection.commit()
        connection.close()
        before = self.snapshot()
        real_connect = sqlite3.connect
        swapped = []
        def change_before_connect(database, *args, **kwargs):
            if "index.working.sqlite" in str(database) and not swapped:
                index = self.state / "index.working.sqlite"
                if index.exists():
                    index.rename(self.state / "original-index.sqlite")
                index.symlink_to(business)
                swapped.append(True)
            return real_connect(database, *args, **kwargs)
        with mock.patch("sqlite3.connect", change_before_connect):
            with self.assertRaises(DriveError):
                self.start()
        self.assertTrue(swapped)
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(os.name == "posix", "Windows pins state ancestors against relocation")
    def test_state_ancestor_moved_inside_source_is_rejected_before_database_writes(self):
        outside = self.base / "outside-state-parent"
        self.state = outside / "state"
        self.config.write_text(json.dumps({"root": str(self.root), "state_dir": str(self.state)}), encoding="utf-8")
        engine = self.start()
        engine.scan_once()
        moved = self.root / "moved-state-parent"
        outside.rename(moved)
        outside.symlink_to(moved, target_is_directory=True)
        snapshot = (moved / "state" / "index.sqlite").read_bytes()
        working = (moved / "state" / "index.working.sqlite").read_bytes()
        try:
            result = engine.call("refresh")
            self.assertFalse(result["ok"])
            self.assertIn(result["error"]["code"], ("UNSAFE_STATE", "STATE_CHANGED"))
            self.assertEqual((moved / "state" / "index.sqlite").read_bytes(), snapshot)
            self.assertEqual((moved / "state" / "index.working.sqlite").read_bytes(), working)
        finally:
            # Restore the synthetic tree before normal shutdown/checkpoint.
            outside.unlink()
            moved.rename(outside)

    def make_directory_link(self, link, target):
        if os.name == "nt":
            # Junction creation requires no symbolic-link privilege. Paths are test-generated.
            script = "New-Item -ItemType Junction -Path $env:DF_MCP_TEST_LINK -Target $env:DF_MCP_TEST_TARGET -ErrorAction Stop | Out-Null"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    env={**os.environ, "DF_MCP_TEST_LINK": str(link), "DF_MCP_TEST_TARGET": str(target)}, capture_output=True)
            self.assertEqual(result.returncode, 0, "Failed to create a temporary test junction")
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_redirected_export_directory_cannot_write_into_source(self):
        engine = self.start()
        engine.scan_once()
        file = engine.list()["files"][0]
        self.make_directory_link(self.state / "exports", self.root)
        before = self.snapshot()
        result = engine.call("export", {"file_id": file["file_id"], "expected_version": file["source_version"]})
        self.assertFalse(result["ok"], result)
        self.assertEqual(before, self.snapshot())

    def test_redirected_state_root_is_rejected_before_writes(self):
        self.make_directory_link(self.state, self.root)
        before = self.snapshot()
        with self.assertRaises(DriveError):
            self.start()
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(os.name == "nt", "Windows ordinary 8.3 aliases")
    def test_windows_short_path_alias_for_unicode_root_remains_readonly(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        short_path = kernel.GetShortPathNameW
        short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        short_path.restype = wintypes.DWORD
        size = short_path(str(self.root), None, 0)
        self.assertGreater(size, 0)
        buffer = ctypes.create_unicode_buffer(size + 1)
        copied = short_path(str(self.root), buffer, len(buffer))
        self.assertGreater(copied, 0)
        self.assertLess(copied, len(buffer))
        alias = Path(buffer.value)
        self.assertTrue(os.path.samefile(alias, self.root))
        self.config.write_text(json.dumps({"root": str(alias), "state_dir": str(self.state)}), encoding="utf-8")
        self.exercise_all_tools()

    @unittest.skipUnless(os.name == "nt", "Windows directory handles pin roots")
    def test_windows_root_cannot_be_rebound_while_service_active(self):
        engine = self.start()
        with self.assertRaises(PermissionError):
            self.root.rename(self.base / "moved-source")
        engine.scan_once()
        self.assertTrue(engine.status()["coverage"]["all_present_files_indexed"])

    @unittest.skipUnless(os.name == "nt", "Windows junction and device path rules")
    def test_windows_junction_and_reserved_paths_are_not_read(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("OUTSIDE_JUNCTION_PAYLOAD", encoding="utf-8")
        self.make_directory_link(self.root / "junction", outside)
        engine = self.start()
        engine.scan_once()
        files = engine.list()["files"]
        junction = next(row for row in files if row["path"] == "junction")
        self.assertEqual(junction["status"], "blocked")
        self.assertEqual(engine.search("OUTSIDE_JUNCTION_PAYLOAD")["files"], [])
        for path in ("budget.csv:hidden", "C:/outside.txt", "NUL", "CON.txt", "../outside/private.txt"):
            with self.assertRaises(DriveError):
                engine._open_source(path)


if __name__ == "__main__":
    unittest.main()
