"""Synthetic directory batches exercise recovery without real network data."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from team_drive import fs_access
from team_drive.engine import Engine
from team_drive.errors import DriveError


class ResumableScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-batched-synthetic-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.mapping = r"\\synthetic-server\approved"
        self.fail_scope = None
        self.denied = set()
        self.calls = []
        outer = self
        local_type = fs_access.WindowsSource if os.name == "nt" else fs_access.PosixSource

        def normalized(info):
            return SimpleNamespace(**{key: (getattr(info, "st_birthtime_ns", info.st_ctime_ns) if key == "st_ctime_ns" else getattr(info, key, 0))
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink", "st_file_attributes")})

        class NativeFixture:
            def __init__(self, unused_root, allow_network=False):
                assert allow_network
                self.local = local_type(outer.root)
                self.root = self.local.root
                # This fixture observes files/directories via Python stat,
                # whose Windows identity representation can differ from the
                # local guard's native handle identity on newer Python.
                # Keep the synthetic binding in that same stat domain; the
                # real local guard retains and checks its own native identity.
                root_info = self.root.lstat()
                self.identity = (root_info.st_dev, root_info.st_ino)
                self.target = outer.mapping

            def check_root(self):
                self.local.check_root()

            def observe(self, relative):
                self.check_root()
                if relative in outer.denied:
                    raise DriveError("SHARE_UNAVAILABLE", "Synthetic current access denial")
                return normalized(self.root.joinpath(*relative.split('/')).lstat())

            def open_read(self, relative):
                self.observe(relative)
                fd, info = self.local.open_read(relative)
                return fd, normalized(info)

            def stat_fd(self, fd):
                return normalized(os.fstat(fd))

            def directory_fingerprint(self, relative="", **unused):
                if relative in outer.denied:
                    raise DriveError("SHARE_UNAVAILABLE", "Synthetic directory access denial")
                info = self.root.joinpath(*relative.split('/')).lstat()
                return [str(info.st_dev), str(info.st_ino), info.st_mtime_ns, info.st_ctime_ns]

            def scan_directory(self, relative="", expected_identity=None, max_items=None, **unused):
                outer.calls.append(relative)
                if relative == outer.fail_scope:
                    raise DriveError("SHARE_TIMEOUT", "Synthetic budget boundary")
                before = self.directory_fingerprint(relative)
                if expected_identity is not None and before[:2] != expected_identity:
                    raise DriveError("SHARE_DIRECTORY_CHANGED", "Synthetic directory replacement")
                children = sorted(self.root.joinpath(*relative.split('/')).iterdir())
                if len(children) > max_items:
                    raise DriveError("SHARE_SCAN_LIMIT", "Synthetic directory item limit")
                files, directories = [], []
                for child in children:
                    path = child.relative_to(self.root).as_posix()
                    info = self.observe(path)
                    if stat.S_ISDIR(info.st_mode):
                        directories.append({"path": path, "identity": [str(info.st_dev), str(info.st_ino)]})
                    else:
                        files.append((path, info, False, False))
                if before != self.directory_fingerprint(relative):
                    raise DriveError("SHARE_DIRECTORY_CHANGED", "Synthetic membership change")
                return {"files": files, "directories": directories, "fingerprint": before, "entries": len(children)}

            def close(self):
                self.local.close()

        self.patches = [mock.patch("team_drive.network_source.resolve_share_path", side_effect=lambda unused: self.mapping),
                        mock.patch("team_drive.network_source._principal_binding", return_value="synthetic-user"),
                        mock.patch.object(fs_access, "WindowsSource", NativeFixture, create=True)]
        for patch in self.patches:
            patch.start()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"provider": "windows_share", "root": r"Z:\Approved", "state_dir": str(self.base / "index")}))
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def fixture(self):
        self.write("a/alpha.txt", "SIMULATED alpha verified evidence 123")
        self.write("b/beta.txt", "SIMULATED beta verified evidence 456")

    def test_partial_batch_is_queryable_and_resume_skips_unchanged_directories(self):
        self.fixture()
        self.fail_scope = "b"
        first = self.engine.scan_once()
        self.assertEqual(first["job"]["status"], "partial")
        self.assertFalse(first["coverage"]["inventory_complete"])
        self.assertEqual(first["coverage"]["scan_resume"]["pending_scopes"], ["b"])
        found = self.engine.search("alpha")["files"][0]
        inspected = self.engine.inspect(found["file_id"])
        result = self.engine.read(found["file_id"], expected_version=inspected["source_version"])
        self.assertIn("123", result["segments"][0]["text"])
        self.assertTrue(result["segments"][0]["locator"])
        self.assertEqual(self.engine.search("beta")["files"], [])
        self.fail_scope = None
        second = self.engine.scan_once()
        self.assertEqual(second["job"]["status"], "complete")
        self.assertEqual(second["coverage"]["indexed_files"], 2)
        self.assertTrue(second["coverage"]["scan_resume"]["resumed"])
        self.assertEqual(self.calls.count("a"), 1)
        self.assertEqual(self.calls.count(""), 1)

    def test_resume_revalidates_changed_membership_and_discovers_new_file(self):
        self.fixture()
        self.fail_scope = "b"
        self.engine.scan_once()
        self.write("a/new.txt", "SIMULATED added during interrupted scan")
        self.fail_scope = None
        result = self.engine.scan_once()
        self.assertEqual(result["job"]["status"], "complete")
        self.assertEqual(self.calls.count("a"), 2)
        self.assertEqual(result["coverage"]["indexed_files"], 3)
        self.assertEqual(len(self.engine.search("added")["files"]), 1)

    def test_partial_inventory_never_deletes_unvisited_old_file(self):
        self.fixture()
        self.engine.scan_once()
        self.fail_scope = "b"
        (self.root / "b/beta.txt").unlink()
        result = self.engine.scan_once()
        self.assertFalse(result["coverage"]["inventory_complete"])
        with self.engine._db() as db:
            self.assertEqual(db.execute("SELECT status FROM files WHERE path='b/beta.txt'").fetchone()[0], "ready")
        self.fail_scope = None
        result = self.engine.scan_once()
        self.assertTrue(result["coverage"]["inventory_complete"])
        with self.engine._db() as db:
            self.assertEqual(db.execute("SELECT status FROM files WHERE path='b/beta.txt'").fetchone()[0], "deleted")

    def test_saved_batch_survives_restart_and_pending_parse_is_retried(self):
        self.fixture()
        self.fail_scope = "b"
        with mock.patch.object(self.engine, "_parse_file"):
            result = self.engine.scan_once()
        cycle_id = result["coverage"]["scan_resume"]["cycle_id"]
        self.assertEqual(result["coverage"]["indexed_files"], 0)
        # Release without the ordinary close checkpoint: recover the explicit
        # batch/job checkpoint, not uncommitted working state.
        self.engine._sqlite_conn.close()
        self.engine._sqlite_conn = None
        self.engine._storage.close()
        self.engine._source.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.fail_scope = None
        result = self.engine.scan_once()
        self.assertEqual(result["coverage"]["scan_resume"]["cycle_id"], cycle_id)
        self.assertEqual(result["coverage"]["indexed_files"], 2)
        self.assertEqual(self.calls.count("a"), 1)

    def test_changed_mapping_cannot_reuse_resume_checkpoint(self):
        self.fixture()
        self.fail_scope = "b"
        self.engine.scan_once()
        self.mapping = r"\\synthetic-server\other"
        with self.assertRaises(DriveError) as raised:
            self.engine.scan_once()
        self.assertEqual(raised.exception.code, "SHARE_ROOT_CHANGED")
        self.assertFalse(self.engine.job_status()["coverage"]["inventory_complete"])

    def test_revoked_completed_scope_is_not_returned_from_partial_cache(self):
        self.fixture()
        self.fail_scope = "b"
        self.engine.scan_once()
        self.denied.add("a/alpha.txt")
        result = self.engine.call("search", {"query": "alpha"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SHARE_UNAVAILABLE")

    def test_single_directory_limit_remains_pending_and_never_claims_empty(self):
        self.fixture()
        self.engine._source.limits["max_items"] = 1
        result = self.engine.scan_once()
        self.assertEqual(result["job"]["status"], "partial")
        self.assertEqual(result["coverage"]["scan_resume"]["pending_scopes"], ["."])
        self.assertEqual(result["coverage"]["present_files"], 0)
        self.assertEqual(self.engine.call("search", {"query": "alpha"})["error"]["code"], "INDEX_NOT_READY")

    def test_force_discards_old_cycle_and_starts_a_new_inventory(self):
        self.fixture()
        self.fail_scope = "b"
        first = self.engine.scan_once()
        second = self.engine.scan_once(force=True)
        self.assertNotEqual(first["coverage"]["scan_resume"]["cycle_id"], second["coverage"]["scan_resume"]["cycle_id"])
        self.assertEqual(self.calls.count("a"), 2)

    def test_final_complete_metadata_is_durable_without_close_checkpoint(self):
        self.fixture()
        result = self.engine.scan_once()
        self.assertTrue(result["coverage"]["inventory_complete"])
        self.engine._sqlite_conn.close()
        self.engine._sqlite_conn = None
        self.engine._storage.close()
        self.engine._source.close()
        self.engine = Engine(str(self.config), autostart=False)
        status = self.engine.status()
        self.assertTrue(status["coverage"]["inventory_complete"])
        self.assertEqual(status["coverage"]["scan_resume"]["phase"], "complete")

    def test_configured_semantic_index_still_runs_for_verified_partial_batch(self):
        self.fixture()
        self.fail_scope = "b"
        self.engine.retrieval.config["semantic"]["enabled"] = True
        with mock.patch.object(self.engine.retrieval, "index_file", return_value={"newly_indexed_segments": 1}) as index:
            result = self.engine.scan_once()
        self.assertEqual(result["job"]["status"], "partial")
        self.assertEqual(index.call_count, 1)
        self.assertEqual(index.call_args.args[1], hashlib.sha256(b"a/alpha.txt").hexdigest()[:32])

    def test_validation_budget_cannot_starve_a_directory_that_fits_a_fresh_job(self):
        self.write("child/value.txt", "SIMULATED resumed evidence")
        source = self.engine._source
        source.limits.update(scan_budget_seconds=1, max_items=1)
        clock = [0.0]
        original_fingerprint, original_scan = source.directory_fingerprint, source.scan_directory

        def fingerprint(relative, deadline, progress=None):
            clock[0] += 0.8
            return original_fingerprint(relative, deadline, progress)

        def scan(relative, expected_identity, deadline, max_items, progress=None):
            if relative == "child":
                clock[0] += 0.5
            return original_scan(relative, expected_identity, deadline, max_items, progress)

        with mock.patch("team_drive.resumable_scan.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(source, "directory_fingerprint", side_effect=fingerprint), \
                mock.patch.object(source, "scan_directory", side_effect=scan):
            results = [self.engine.scan_once()]
            for unused in range(5):
                results.append(self.engine.scan_once())
                if results[-1]["job"]["status"] == "complete":
                    break
        self.assertEqual(results[0]["job"]["error_code"], "SHARE_SCAN_LIMIT")
        self.assertEqual(results[1]["job"]["error_code"], "SHARE_TIMEOUT")
        self.assertEqual(results[-1]["job"]["status"], "complete")
        self.assertTrue(results[-1]["coverage"]["inventory_complete"])
        self.assertEqual(results[-1]["coverage"]["indexed_files"], 1)


if __name__ == "__main__":
    unittest.main()
