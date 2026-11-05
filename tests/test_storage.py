"""Durable snapshot, metadata and source-boundary regressions; synthetic data only."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from team_drive.errors import DriveError
from team_drive.fs_access import SourceAccess, StateAccess


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-snapshots-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "sources"
        self.root.mkdir()
        (self.root / "keep.txt").write_text("SIMULATED source must stay unchanged", encoding="utf-8")
        self.source = SourceAccess(self.root)
        self.state = self.base / "state"
        self.storage = None
        self.connection = None
        self.open()

    def open(self):
        self.storage = StateAccess(self.state, self.source, Path(__file__).resolve().parents[1])
        self.storage.lock()
        self.connection = self.storage.connect_database()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.storage is not None:
            self.storage.close()
            self.storage = None

    def tearDown(self):
        self.close()
        self.source.close()
        self.assertEqual((self.root / "keep.txt").read_text(encoding="utf-8"), "SIMULATED source must stay unchanged")
        self.temp.cleanup()

    def commit_fixture(self):
        self.connection.execute("CREATE TABLE records(value TEXT)")
        self.connection.execute("INSERT INTO records VALUES('COMMITTED')")
        self.connection.commit()
        self.storage.write_profile_manifest()
        self.storage.checkpoint_database(self.connection)

    def fingerprint(self):
        return hashlib.sha256((self.state / "index.sqlite").read_bytes()).hexdigest()

    def test_unpublished_work_is_discarded_and_committed_snapshot_survives(self):
        self.commit_fixture()
        original = self.fingerprint()
        self.connection.execute("INSERT INTO records VALUES('UNPUBLISHED')")
        self.connection.commit()
        self.assertEqual(self.fingerprint(), original)
        self.close()
        self.open()
        self.assertEqual(self.connection.execute("SELECT value FROM records").fetchall(), [("COMMITTED",)])
        self.assertTrue(self.storage.recovery["restored_snapshot"])

    def test_checkpoint_replaces_complete_snapshot_and_reopens(self):
        self.commit_fixture()
        self.connection.execute("INSERT INTO records VALUES('SECOND')")
        self.connection.commit()
        self.storage.checkpoint_database(self.connection)
        self.assertFalse((self.state / "index.next.sqlite").exists())
        self.close()
        self.open()
        self.assertEqual(self.connection.execute("SELECT value FROM records ORDER BY rowid").fetchall(), [("COMMITTED",), ("SECOND",)])

    def test_failed_publication_leaves_last_committed_snapshot_unchanged(self):
        self.commit_fixture()
        original = self.fingerprint()
        self.connection.execute("INSERT INTO records VALUES('UNPUBLISHED')")
        self.connection.commit()
        with mock.patch.object(self.storage, "_replace_root", side_effect=OSError("synthetic rename failure")):
            with self.assertRaises(DriveError) as caught:
                self.storage.checkpoint_database(self.connection)
        self.assertEqual(caught.exception.code, "STATE_CHECKPOINT_FAILED")
        self.assertEqual(self.fingerprint(), original)
        self.assertFalse((self.state / "index.next.sqlite").exists())

    def test_corrupt_owned_snapshot_is_quarantined_once_and_rebuilt(self):
        self.commit_fixture()
        self.close()
        for marker in (b"broken-first", b"broken-second"):
            (self.state / "index.sqlite").write_bytes(marker)
            self.open()
            self.assertTrue(self.storage.recovery["rebuilt_after_corruption"])
            self.assertEqual((self.state / "index.corrupt.sqlite").read_bytes(), marker)
            self.connection.execute("CREATE TABLE records(value TEXT)")
            self.connection.commit()
            self.storage.checkpoint_database(self.connection)
            self.close()
        self.assertEqual(len(list(self.state.glob("index.corrupt*"))), 1)

    def test_unmarked_corrupt_legacy_snapshot_requires_explicit_reset(self):
        self.close()
        (self.state / "index.sqlite").write_bytes(b"unowned arbitrary file")
        with self.assertRaises(DriveError) as caught:
            self.open()
        self.assertEqual(caught.exception.code, "STATE_REBUILD_REQUIRED")
        self.assertEqual((self.state / "index.sqlite").read_bytes(), b"unowned arbitrary file")
        self.assertFalse((self.state / "index.corrupt.sqlite").exists())

    def test_checkpoint_requires_completed_transaction(self):
        self.commit_fixture()
        self.connection.execute("INSERT INTO records VALUES('PENDING')")
        with self.assertRaises(DriveError) as caught:
            self.storage.checkpoint_database(self.connection)
        self.assertEqual(caught.exception.code, "STATE_TRANSACTION_ACTIVE")
        self.connection.rollback()

    def test_metadata_is_atomic_bounded_and_source_links_are_rejected(self):
        self.storage.write_metadata("endpoint.json", {"generation": 1})
        self.assertEqual(self.storage.read_metadata("endpoint.json"), {"generation": 1})
        self.storage.write_metadata("endpoint.json", {"generation": 2})
        self.assertEqual(self.storage.read_metadata("endpoint.json"), {"generation": 2})
        self.assertFalse((self.state / "endpoint.json.next").exists())
        self.assertIsNone(self.storage.read_metadata("absent.json"))
        self.storage.remove_metadata("endpoint.json")
        # Create hostile pre-existing state before guards are acquired. Windows
        # itself rejects CreateHardLink while the parent guards deny write sharing.
        self.close()
        self.source.close()
        os.link(self.root / "keep.txt", self.state / "endpoint.json")
        self.source = SourceAccess(self.root)
        self.open()
        with self.assertRaises(DriveError):
            self.storage.write_metadata("endpoint.json", {"do_not_write": True})
        self.assertTrue((self.state / "endpoint.json").exists())

    @unittest.skipUnless(os.name == "nt", "Actual Windows pinned-directory rename semantics")
    def test_windows_publication_retains_guards_and_open_metadata_reader(self):
        # Both the first profile publication and subsequent replacements used
        # to fail with ERROR_SHARING_VIOLATION despite closing all staging fds.
        self.storage.write_profile_manifest()
        self.storage.write_metadata("endpoint.json", {"generation": 1})
        guarded = tuple(self.storage.guard.handles)
        reader = self.storage._open_root("endpoint.json", metadata_read=True)
        try:
            self.storage.write_metadata("endpoint.json", {"generation": 2})
            self.assertEqual(os.read(reader, 65536), b'{"generation":1}')
            self.assertEqual(self.storage.read_metadata("endpoint.json"), {"generation": 2})
            self.assertEqual(tuple(self.storage.guard.handles), guarded)
            self.storage.guard.check()
            with self.assertRaises(OSError):
                self.state.rename(self.base / "moved-state")
            with self.assertRaises(OSError):
                self.root.rename(self.base / "moved-source")
        finally:
            os.close(reader)

    @unittest.skipUnless(os.name == "nt", "Actual Windows handle-relative rename semantics")
    def test_windows_publication_never_uses_process_working_directory(self):
        original = Path.cwd()
        destination = self.base / "unrelated"
        destination.mkdir()
        try:
            os.chdir(destination)
            self.storage.write_metadata("endpoint.json", {"name": "state-only"})
            self.assertEqual(list(destination.iterdir()), [])
            self.assertEqual(self.storage.read_metadata("endpoint.json"), {"name": "state-only"})
        finally:
            os.chdir(original)

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor verification concurrency")
    def test_export_fd_creation_cannot_contaminate_sqlite_descriptor_verification(self):
        self.commit_fixture()
        with self.storage.directory("exports", create=True):
            pass
        at_connect = threading.Event()
        export_attempt = threading.Event()
        export_opened = threading.Event()
        errors = []
        real_connect = sqlite3.connect

        def export():
            try:
                if not at_connect.wait(5):
                    raise AssertionError("SQLite did not reach the guarded connect window")
                export_attempt.set()
                with self.storage.new_private(self.state / "exports" / "evidence.jsonl") as output:
                    export_opened.set()
                    output.write('{"synthetic":"evidence"}\n')
            except Exception as exc:
                errors.append(exc)

        def connect(database, *args, **kwargs):
            if "index.next.sqlite" in str(database):
                at_connect.set()
                self.assertTrue(export_attempt.wait(5))
                self.assertFalse(export_opened.wait(0.05), "Export fd must wait until SQLite has verified its own fd")
            return real_connect(database, *args, **kwargs)

        worker = threading.Thread(target=export)
        worker.start()
        try:
            with mock.patch("sqlite3.connect", connect):
                self.storage.checkpoint_database(self.connection)
        finally:
            at_connect.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(export_opened.is_set())
        self.assertEqual((self.state / "exports" / "evidence.jsonl").read_text(), '{"synthetic":"evidence"}\n')

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor-number reuse")
    def test_sqlite_can_reuse_a_concurrently_closed_reader_descriptor(self):
        self.commit_fixture()
        reader = os.open(self.root / "keep.txt", os.O_RDONLY)
        closed = False
        reused = []
        real_connect = sqlite3.connect

        def connect(database, *args, **kwargs):
            nonlocal closed
            if "index.next.sqlite" in str(database):
                os.close(reader)
                closed = True
                connection = real_connect(database, *args, **kwargs)
                reused.append(os.fstat(reader).st_ino == (self.state / "index.next.sqlite").stat().st_ino)
                return connection
            return real_connect(database, *args, **kwargs)

        try:
            with mock.patch("sqlite3.connect", connect):
                self.storage.checkpoint_database(self.connection)
        finally:
            if not closed:
                os.close(reader)
        self.assertEqual(reused, [True], "The test must exercise SQLite reuse of the earlier reader fd number")

    @unittest.skipUnless(os.name == "posix", "POSIX fault injection; Windows pins SQLite files")
    def test_staging_file_swap_at_sqlite_open_cannot_write_source(self):
        self.commit_fixture()
        business = self.root / "business.sqlite"
        with sqlite3.connect(business) as db:
            db.execute("CREATE TABLE data(value)")
            db.execute("INSERT INTO data VALUES(731)")
        original = business.read_bytes()
        real_connect = sqlite3.connect
        swapped = []
        def swap(database, *args, **kwargs):
            if "index.next.sqlite" in str(database) and not swapped:
                staging = self.state / "index.next.sqlite"
                staging.rename(self.state / "original-stage.sqlite")
                staging.symlink_to(business)
                swapped.append(True)
            return real_connect(database, *args, **kwargs)
        with mock.patch("sqlite3.connect", swap):
            with self.assertRaises(DriveError):
                self.storage.checkpoint_database(self.connection)
        self.assertTrue(swapped)
        self.assertEqual(business.read_bytes(), original)
        (self.state / "index.next.sqlite").unlink()

    def test_hardlinked_staging_or_journal_is_never_modified(self):
        self.commit_fixture()
        for name in ("index.next.sqlite", "index.working.sqlite-journal"):
            self.close()
            self.source.close()
            os.link(self.root / "keep.txt", self.state / name)
            self.source = SourceAccess(self.root)
            with self.assertRaises(DriveError):
                self.open()
            self.assertTrue((self.state / name).exists())
            self.close()
            self.source.close()
            (self.state / name).unlink()
            self.source = SourceAccess(self.root)
            self.open()

    def test_sidecar_inserted_at_connect_is_rejected_before_any_pragma(self):
        self.commit_fixture()
        real_connect = sqlite3.connect
        statements = []
        inserted = []
        def insert_sidecar(database, *args, **kwargs):
            db = real_connect(database, *args, **kwargs)
            if "index.next.sqlite" in str(database) and not inserted:
                db.set_trace_callback(statements.append)
                (self.state / "index.next.sqlite-journal").write_bytes(b"SIMULATED untrusted journal")
                inserted.append(True)
            return db
        with mock.patch("sqlite3.connect", insert_sidecar):
            with self.assertRaises(DriveError) as caught:
                self.storage.checkpoint_database(self.connection)
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        self.assertTrue(inserted)
        self.assertEqual(statements, [], "No SQLite PRAGMA may trigger sidecar recovery after a late sidecar appears.")
        (self.state / "index.next.sqlite-journal").unlink()

    @unittest.skipUnless(os.name == "posix", "POSIX owner/mode privacy")
    def test_endpoint_requires_private_control_directory_and_metadata_mode(self):
        self.storage.write_metadata("endpoint.json", {"token": "synthetic-secret"})
        path = self.state / "endpoint.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaises(DriveError) as caught:
            self.storage.read_metadata("endpoint.json")
        self.assertEqual(caught.exception.code, "UNSAFE_STATE_PERMISSIONS")
        path.chmod(0o600)
        self.state.chmod(0o755)
        try:
            with self.assertRaises(DriveError) as caught:
                self.storage.write_metadata("endpoint.json", {"token": "must-not-publish"})
            self.assertEqual(caught.exception.code, "UNSAFE_STATE_PERMISSIONS")
        finally:
            self.state.chmod(0o700)
        self.assertEqual(self.storage.read_metadata("endpoint.json"), {"token": "synthetic-secret"})

    def test_process_killed_during_transaction_restores_prior_snapshot(self):
        self.commit_fixture()
        original = self.fingerprint()
        self.close()
        program = "\n".join([
            "import sys", "from pathlib import Path", "from team_drive.fs_access import SourceAccess, StateAccess",
            "source=SourceAccess(sys.argv[1])", "state=StateAccess(sys.argv[2],source,Path.cwd())", "state.lock()",
            "db=state.connect_database()", "db.execute('PRAGMA cache_size=5')", "db.execute('BEGIN')",
            "db.executemany('INSERT INTO records VALUES(?)', [('UNCOMMITTED-'+str(i)+'x'*200,) for i in range(20000)])",
            "print('transaction-written',flush=True)", "sys.stdin.readline()"
        ])
        process = subprocess.Popen([sys.executable, "-c", program, str(self.root), str(self.state)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "python")})
        try:
            self.assertEqual(process.stdout.readline().strip(), "transaction-written")
            process.kill()
            process.wait(timeout=10)
            self.assertEqual(self.fingerprint(), original)
            self.open()
            self.assertEqual(self.connection.execute("SELECT value FROM records").fetchall(), [("COMMITTED",)])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_process_killed_before_publish_discards_complete_staging_snapshot(self):
        self.commit_fixture()
        original = self.fingerprint()
        self.close()
        program = "\n".join([
            "import sys", "from pathlib import Path", "from team_drive.fs_access import SourceAccess, StateAccess",
            "source=SourceAccess(sys.argv[1])", "state=StateAccess(sys.argv[2],source,Path.cwd())", "state.lock()",
            "db=state.connect_database()", "db.execute(\"INSERT INTO records VALUES('NOT-PUBLISHED')\")", "db.commit()",
            "def pause(*args):", "    print('before-publish',flush=True)", "    sys.stdin.readline()",
            "state._replace_root=pause", "state.checkpoint_database(db)"
        ])
        process = subprocess.Popen([sys.executable, "-c", program, str(self.root), str(self.state)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "python")})
        try:
            self.assertEqual(process.stdout.readline().strip(), "before-publish")
            process.kill()
            process.wait(timeout=10)
            self.assertEqual(self.fingerprint(), original)
            self.assertTrue((self.state / "index.next.sqlite").exists())
            self.open()
            self.assertFalse((self.state / "index.next.sqlite").exists())
            self.assertEqual(self.connection.execute("SELECT value FROM records").fetchall(), [("COMMITTED",)])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()
