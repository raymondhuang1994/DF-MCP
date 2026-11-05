"""Real engine state and bounded diagnostics for an unpublished inventory."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from team_drive.engine import Engine
from team_drive.errors import DriveError
from team_drive.scan_progress import ScanProgress, error_detail


class ProgressTests(unittest.TestCase):
    def test_inflight_timing_failure_and_fixed_vocabulary(self):
        clock = [1.0]
        progress = ScanProgress(lambda: clock[0])
        progress.set_stage("enumerating")
        with self.assertRaises(OSError):
            with progress.operation("directory_enumeration"):
                progress.increment("entries_enumerated", 7)
                clock[0] = 3.5
                current = progress.snapshot()
                self.assertEqual(current["operations"]["directory_enumeration"]["inflight"], 1)
                self.assertEqual(current["operations"]["directory_enumeration"]["active_ms"], 2500)
                raise OSError(5, "SECRET_FILENAME and confidential text")
        progress.finish("failed")
        clock[0] = 100
        snapshot = progress.snapshot()
        self.assertEqual(snapshot["elapsed_seconds"], 2.5)
        self.assertEqual(snapshot["failed_stage"], "enumerating")
        self.assertEqual(snapshot["failed_operation"], "directory_enumeration")
        self.assertFalse(snapshot["inventory_published"])
        with self.assertRaises(ValueError):
            progress.set_stage("SECRET_FILENAME")
        with self.assertRaises(ValueError):
            progress.increment("SECRET_FILENAME")
        self.assertNotIn("SECRET", json.dumps(snapshot))

    def test_error_context_preserves_native_code_but_not_arbitrary_text(self):
        try:
            try:
                raise OSError(13, "PRIVATE\\FILE and query and body")
            except OSError:
                raise DriveError("SHARE_ENUMERATION_FAILED", "PRIVATE\\FILE") from None
        except DriveError as exc:
            detail = error_detail(exc)
        self.assertEqual(detail["native_errno"], 13)
        self.assertEqual(detail["code"], "SHARE_ENUMERATION_FAILED")
        self.assertNotIn("PRIVATE", json.dumps(detail))


class EngineProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "SIMULATED"
        self.root.mkdir()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"root": str(self.root), "state_dir": str(self.base / "state")}))
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_progress_visible_before_publication_and_failed_snapshot_survives_next_job(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow_walk(_errors, progress=None):
            progress.set_stage("enumerating")
            with progress.operation("directory_enumeration"):
                progress.increment("directories_enumerated")
                progress.increment("entries_enumerated", 9)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release scan")
                raise DriveError("SHARE_TIMEOUT", "SECRET_SOURCE_PATH")
            yield  # This deliberately never publishes an incomplete inventory.

        def scan():
            try:
                self.engine.scan_once()
            except DriveError as exc:
                errors.append(exc.code)

        self.engine._share_binding = {"synthetic": True}
        with mock.patch.object(self.engine._source, "walk", side_effect=slow_walk):
            worker = threading.Thread(target=scan)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                during = self.engine.job_status()
                self.assertEqual(during["job"]["scan_progress"]["counters"]["entries_enumerated"], 9)
                self.assertEqual(during["job"]["discovered"], 0)
                self.assertFalse(during["coverage"]["inventory_complete"])
                self.assertEqual(self.engine.status()["index_status"]["state"], "scanning")
                self.assertEqual(self.engine.call("list")["error"]["code"], "INDEX_NOT_READY")
                self.assertEqual(self.engine.call("search", {"query": "SIMULATED"})["error"]["code"], "INDEX_NOT_READY")
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["SHARE_TIMEOUT"])
        failed = self.engine.job_status()
        self.assertEqual(failed["job"]["status"], "failed")
        self.assertEqual(failed["job"]["error_detail"]["code"], "SHARE_TIMEOUT")
        self.assertEqual(failed["job"]["scan_progress"]["failed_operation"], "directory_enumeration")
        self.assertEqual(failed["coverage"]["present_files"], 0)
        self.assertEqual(self.engine.status()["index_generation"], 0)
        self.assertEqual(self.engine.status()["index_status"]["state"], "failed")
        self.assertNotIn("SECRET", json.dumps(failed))
        failed_id = failed["job"]["id"]
        self.engine._share_binding = None
        self.engine.scan_once()
        self.assertEqual(self.engine.job_status(failed_id)["job"], failed["job"])
        self.assertEqual(self.engine.status()["index_status"]["state"], "ready")
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine.job_status(failed_id)["job"], failed["job"])

    def test_failed_walk_keeps_previous_inventory_and_versions(self):
        (self.root / "budget.txt").write_text("SIMULATED budget 321")
        self.engine.scan_once()
        before = self.engine.list()["files"][0]
        generation = self.engine.status()["index_generation"]
        with mock.patch.object(self.engine._source, "walk", side_effect=DriveError("SHARE_TIMEOUT", "private")):
            with self.assertRaises(DriveError):
                self.engine.scan_once()
        self.assertEqual(self.engine.status()["index_generation"], generation)
        self.assertFalse(self.engine.status()["coverage"]["inventory_complete"])
        current = self.engine.list()["files"][0]
        self.assertEqual(current["source_version"], before["source_version"])
        self.assertEqual(current["status"], "ready")
        read = self.engine.read(current["file_id"], expected_version=current["source_version"])
        self.assertIn("321", read["segments"][0]["text"])

    def test_final_checkpoint_finishes_before_a_second_scan_can_enumerate(self):
        (self.root / "budget.txt").write_text("SIMULATED budget 321")
        checkpoint_entered, release_checkpoint = threading.Event(), threading.Event()
        second_attempted, second_enumerated = threading.Event(), threading.Event()
        first_name, second_name = "scan-first-checkpoint", "scan-second-waiting"
        actual_lock = self.engine._scan_lock
        original_walk, original_checkpoint = self.engine._source.walk, self.engine._checkpoint
        errors, results = [], []

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == second_name:
                    second_attempted.set()
                actual_lock.acquire()
                return self

            def __exit__(self, *_args):
                actual_lock.release()

        def observed_walk(*args, **kwargs):
            if threading.current_thread().name == second_name:
                second_enumerated.set()
            yield from original_walk(*args, **kwargs)

        def paused_checkpoint(*args, **kwargs):
            if threading.current_thread().name == first_name:
                checkpoint_entered.set()
                if not release_checkpoint.wait(5):
                    raise AssertionError("Test did not release the first checkpoint")
            return original_checkpoint(*args, **kwargs)

        def scan():
            try:
                results.append(self.engine.scan_once()["job"]["status"])
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=scan, name=first_name)
        second = threading.Thread(target=scan, name=second_name)
        with mock.patch.object(self.engine, "_scan_lock", ObservedLock()), \
                mock.patch.object(self.engine._source, "walk", side_effect=observed_walk), \
                mock.patch.object(self.engine, "_checkpoint", side_effect=paused_checkpoint):
            first.start()
            try:
                self.assertTrue(checkpoint_entered.wait(5))
                second.start()
                self.assertTrue(second_attempted.wait(5))
                self.assertFalse(second_enumerated.wait(0.2),
                                 "A previous scan must not checkpoint another scan's partial inventory")
            finally:
                release_checkpoint.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_enumerated.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(results, ["complete", "complete"])


if __name__ == "__main__":
    unittest.main()
