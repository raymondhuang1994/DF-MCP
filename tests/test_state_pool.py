"""Lease, persistence, and Windows regressions using disposable synthetic sources."""
import hashlib
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from team_drive.engine import DriveError, Engine


class StatePoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-state-pool-")
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.fixture = self.source / "shared-test.txt"
        self.fixture.write_text("SIMULATED shared file 只读测试\nlease-marker-718\n", encoding="utf-8")
        self.state = self.base / "state"
        self.config = self.base / "config.json"
        self.options = {"root": str(self.source), "state_dir": str(self.state), "poll_seconds": 3600,
                        "state_pool": {"max_slots": 2}}
        self.engines = []
        self.write_config()
        self.before = self.source_snapshot()

    def tearDown(self):
        for engine in reversed(self.engines):
            engine.close()
        self.assertEqual(self.source_snapshot(), self.before, "The state pool must not modify source files.")
        self.temp.cleanup()

    def write_config(self):
        self.config.write_text(json.dumps(self.options), encoding="utf-8")

    def start(self):
        engine = Engine(str(self.config), autostart=False, pooled=True)
        self.engines.append(engine)
        return engine

    def source_snapshot(self):
        result = {}
        for path in sorted(self.source.rglob("*")):
            info = path.stat()
            result[path.relative_to(self.source).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
                info.st_mtime_ns, stat.S_IMODE(info.st_mode))
        return result

    def parsed_row(self, engine):
        with engine._db() as db:
            return tuple(db.execute("SELECT id, source_version, updated_at FROM files WHERE path='shared-test.txt'").fetchone())

    def test_two_leases_can_index_and_read_the_same_source(self):
        first, second = self.start(), self.start()
        self.assertNotEqual(first.state, second.state)
        self.assertEqual(first.state.parent, self.state / "pool-v1")
        self.assertEqual({first.state.name, second.state.name}, {"slot-01", "slot-02"})
        for engine in (first, second):
            engine.scan_once()
            status = engine.status()
            self.assertTrue(status["ok"])
            self.assertTrue(status["coverage"]["all_present_files_indexed"])
            self.assertEqual(status["state_storage"]["mode"], "pooled")
            self.assertEqual(status["state_storage"]["max_slots"], 2)
        first_file, second_file = first.list()["files"][0], second.list()["files"][0]
        self.assertEqual(first_file["file_id"], second_file["file_id"])
        self.assertEqual(first_file["source_version"], second_file["source_version"])
        self.assertEqual(first.read(first_file["file_id"])["segments"], second.read(second_file["file_id"])["segments"])

    def test_capacity_is_bounded_and_a_closed_slot_is_immediately_reusable(self):
        first, second = self.start(), self.start()
        with self.assertRaises(DriveError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "STATE_POOL_EXHAUSTED")
        self.assertTrue(caught.exception.retryable)
        before = {path.name for path in (self.state / "pool-v1").iterdir() if path.is_dir()}
        self.assertEqual(before, {"slot-01", "slot-02"})
        first.close()
        replacement = self.start()
        self.assertEqual(replacement.state, first.state)
        self.assertTrue(second.status()["ok"])
        self.assertEqual({path.name for path in (self.state / "pool-v1").iterdir() if path.is_dir()}, before)

    def test_reconnect_reuses_index_without_reparsing_unchanged_files(self):
        first = self.start()
        first.scan_once()
        original_row = self.parsed_row(first)
        original_generation = first.status()["index_generation"]
        original_slot = first.state
        first.close()
        for _ in range(6):
            reopened = self.start()
            self.assertEqual(reopened.state, original_slot)
            self.assertEqual(self.parsed_row(reopened), original_row)
            with mock.patch.object(reopened, "_parse_file", side_effect=AssertionError("unchanged source reparsed")):
                reopened.scan_once()
            self.assertEqual(self.parsed_row(reopened), original_row)
            self.assertEqual(reopened.status()["index_generation"], original_generation)
            reopened.close()
        self.assertEqual(len(list((self.state / "pool-v1").glob("slot-*"))), 1)

    def test_invalid_pool_sizes_are_rejected_before_creating_state(self):
        for value in (0, -1, 33, True, 1.5, "2", None):
            with self.subTest(value=value):
                self.options["state_pool"] = {"max_slots": value}
                self.write_config()
                with self.assertRaises(DriveError) as caught:
                    self.start()
                self.assertEqual(caught.exception.code, "INVALID_CONFIG")
                self.assertFalse(self.state.exists())

    def test_pool_base_cannot_contain_source_or_be_inside_source(self):
        for prohibited in (self.base, self.source, self.source / "cache"):
            with self.subTest(path=prohibited):
                self.options["state_dir"] = str(prohibited)
                self.write_config()
                with self.assertRaises(DriveError) as caught:
                    self.start()
                self.assertEqual(caught.exception.code, "UNSAFE_STATE")
                self.assertFalse((prohibited / "pool-v1").exists())

    def test_codex_state_is_rejected_before_any_directory_is_created(self):
        for directory in (".codex", ".CoDeX"):
            with self.subTest(directory=directory):
                prohibited = self.base / directory / "new-index" / "nested"
                self.options["state_dir"] = str(prohibited)
                self.write_config()
                with self.assertRaises(DriveError) as caught:
                    self.start()
                self.assertEqual(caught.exception.code, "UNSAFE_STATE")
                self.assertFalse((self.base / directory).exists())

    def test_custom_codex_home_is_also_rejected_before_creation(self):
        custom_home = self.base / "custom-agent-home"
        self.options["state_dir"] = str(custom_home / "index")
        self.write_config()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom_home)}):
            with self.assertRaises(DriveError) as caught:
                self.start()
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        self.assertFalse(custom_home.exists())

    def test_default_codex_home_physical_alias_is_rejected(self):
        home = self.base / "simulated-home"
        home.mkdir()
        actual_home = self.base / "agent-data"
        actual_home.mkdir()
        try:
            (home / ".codex").symlink_to(actual_home, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privilege unavailable")
            raise
        self.options["state_dir"] = str(actual_home / "index")
        self.write_config()
        environment = dict(os.environ)
        environment.pop("CODEX_HOME", None)
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(Path, "home", return_value=home):
            with self.assertRaises(DriveError) as caught:
                self.start()
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        self.assertFalse((actual_home / "index").exists())

    def test_direct_engine_lock_remains_exclusive(self):
        first = Engine(str(self.config), autostart=False)
        self.engines.append(first)
        with self.assertRaises(DriveError) as caught:
            Engine(str(self.config), autostart=False)
        self.assertEqual(caught.exception.code, "STATE_IN_USE")

    def test_acquiring_idle_slot_reclaims_only_recognized_parser_scratch(self):
        first = self.start()
        first.scan_once()
        slot = first.state
        row = self.parsed_row(first)
        first.close()
        orphan = slot / ("job-temp-" + "a" * 32)
        orphan.mkdir()
        (orphan / ("snapshot-" + "b" * 32 + ".txt")).write_text("orphan snapshot", encoding="utf-8")
        (orphan / ("parsed-" + "c" * 32 + ".jsonl")).write_text("{}\n", encoding="utf-8")
        unknown = slot / ("job-temp-" + "d" * 32)
        unknown.mkdir()
        (unknown / "user-kept-note.txt").write_text("KEEP", encoding="utf-8")
        known_beside_unknown = unknown / ("snapshot-" + "e" * 32 + ".txt")
        known_beside_unknown.write_text("also KEEP: unexpected directory content", encoding="utf-8")
        exports = slot / "exports"
        exports.mkdir()
        exported = exports / "keep.jsonl"
        exported.write_text("{\"keep\":true}\n", encoding="utf-8")
        recovered = self.start()
        self.assertFalse(orphan.exists())
        self.assertEqual((unknown / "user-kept-note.txt").read_text(encoding="utf-8"), "KEEP")
        self.assertTrue(known_beside_unknown.exists(), "Unexpected directories must be preserved in full.")
        self.assertTrue(exported.exists(), "Exports are user artifacts, not abandoned parser scratch.")
        self.assertEqual(self.parsed_row(recovered), row)
        cleanup = recovered.status()["state_storage"]["last_scratch_cleanup"]
        self.assertEqual(cleanup["removed_files"], 2)
        self.assertEqual(cleanup["removed_directories"], 1)
        self.assertGreaterEqual(cleanup["skipped_directories"], 1)

    def test_scratch_cleanup_preserves_hardlinked_files(self):
        first = self.start()
        slot = first.state
        first.close()
        outside = self.base / "outside-scratch.txt"
        outside.write_text("KEEP HARDLINK", encoding="utf-8")
        suspect = slot / ("job-temp-" + "a" * 32)
        suspect.mkdir()
        alias = suspect / ("snapshot-" + "b" * 32 + ".txt")
        os.link(outside, alias)
        self.start()
        self.assertEqual(outside.read_text(encoding="utf-8"), "KEEP HARDLINK")
        self.assertTrue(alias.exists())
        self.assertTrue(os.path.samefile(outside, alias))

    def test_scratch_cleanup_does_not_follow_redirected_directory(self):
        first = self.start()
        slot = first.state
        first.close()
        alias = slot / ("job-temp-" + "a" * 32)
        try:
            alias.symlink_to(self.source, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privilege unavailable")
            raise
        self.start()
        self.assertTrue(alias.is_symlink())
        self.assertTrue(self.fixture.exists())

    def test_completed_job_history_remains_bounded_during_repeated_refreshes(self):
        engine = self.start()
        oldest = engine.scan_once()["job"]["id"]
        for _ in range(105):
            latest = engine.scan_once()["job"]["id"]
        with engine._db() as db:
            count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertLessEqual(count, engine.status()["state_storage"]["cleanup"]["job_history_limit"])
        self.assertEqual(engine.job_status(latest)["job"]["status"], "complete")
        self.assertEqual(engine.call("job_status", {"job_id": oldest})["error"]["code"], "JOB_NOT_FOUND")

    def test_abrupt_process_death_releases_lease_and_preserves_completed_index(self):
        self.options["state_pool"]["max_slots"] = 1
        self.write_config()
        code = "\n".join([
            "import json, sys",
            "from team_drive.engine import Engine",
            "engine = Engine(sys.argv[1], autostart=False, pooled=True)",
            "engine.scan_once()",
            "with engine._db() as db:",
            "    row = tuple(db.execute(\"SELECT id, source_version, updated_at FROM files WHERE path='shared-test.txt'\").fetchone())",
            "print(json.dumps({'state': str(engine.state), 'row': row}), flush=True)",
            "sys.stdin.read()"
        ])
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "python")
        environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen([sys.executable, "-c", code, str(self.config)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=20)
            self.assertTrue(line, "Worker exited before publishing its completed index.")
            before = json.loads(line)
            with self.assertRaises(DriveError) as caught:
                self.start()
            self.assertEqual(caught.exception.code, "STATE_POOL_EXHAUSTED")
            process.kill()  # TerminateProcess on Windows; SIGKILL on POSIX. No Engine.close().
            process.wait(timeout=10)
            recovered = self.start()
            self.assertEqual(str(recovered.state), before["state"])
            self.assertEqual(list(self.parsed_row(recovered)), before["row"])
            self.assertIn("lease-marker-718", json.dumps(recovered.read(before["row"][0]), ensure_ascii=False))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            reader.join(timeout=1)

    @unittest.skipUnless(os.name == "nt", "Requires actual Windows directory sharing semantics")
    def test_windows_codex_atomic_replace_with_two_active_index_leases(self):
        codex = self.base / ".codex"
        codex.mkdir()
        target = codex / ".codex-global-state.json"
        target.write_text('{"revision":0}', encoding="utf-8")
        first, second = self.start(), self.start()
        first.scan_once()
        second.scan_once()
        for revision in range(1, 11):
            replacement = codex / (".codex-global-state.json.%d.tmp" % revision)
            replacement.write_text(json.dumps({"revision": revision}), encoding="utf-8")
            os.replace(replacement, target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"revision": revision})
        self.assertTrue(first.status()["ok"])
        self.assertTrue(second.status()["ok"])


if __name__ == "__main__":
    unittest.main()
