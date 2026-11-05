"""Exercise Windows DirEntry's incomplete link metadata on every platform."""
import contextlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from team_drive.fs_access import SourceAccess, StateAccess


class ScratchMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-scratch-metadata-")
        self.base = Path(self.temp.name).resolve()
        root = self.base / "source"
        root.mkdir()
        self.source = SourceAccess(root)
        self.storage = StateAccess(self.base / "state", self.source, Path(__file__).resolve().parents[1])
        self.storage.lock()
        self.scratch = self.storage.path / ("job-temp-" + "a" * 32)
        self.scratch.mkdir()
        self.snapshot = self.scratch / ("snapshot-" + "b" * 32 + ".txt")

    def tearDown(self):
        self.storage.close()
        self.source.close()
        self.temp.cleanup()

    @contextlib.contextmanager
    def cached_link_count(self, count):
        # Windows DirEntry.stat() reports st_nlink=0, unlike os.stat(). It
        # also caches observations; cleanup must obtain fresh no-follow data.
        entry = SimpleNamespace(name=self.snapshot.name, path=str(self.snapshot),
                                stat=mock.Mock(return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
                                                                           st_nlink=count, st_file_attributes=0)))
        original_scandir = os.scandir

        @contextlib.contextmanager
        def scandir(path):
            if Path(path) == self.scratch:
                yield iter([entry])
            else:
                with original_scandir(path) as entries:
                    yield entries

        with mock.patch("team_drive.fs_access.os.scandir", side_effect=scandir):
            yield entry

    def test_zero_cached_link_count_does_not_skip_safe_scratch(self):
        self.snapshot.write_text("SIMULATED abandoned snapshot", encoding="utf-8")
        with self.cached_link_count(0) as entry:
            preview = self.storage.cleanup_scratch(dry_run=True)
            self.assertEqual(preview["eligible_files"], 1)
            self.assertTrue(self.snapshot.exists())
            result = self.storage.cleanup_scratch()
            self.assertEqual(result["removed_files"], 1)
            self.assertEqual(result["removed_directories"], 1)
            entry.stat.assert_not_called()
        self.assertFalse(self.scratch.exists())

    def test_cached_single_link_cannot_authorize_hardlink_cleanup(self):
        outside = self.base / "keep.txt"
        outside.write_text("SIMULATED retained data", encoding="utf-8")
        os.link(outside, self.snapshot)
        with self.cached_link_count(1) as entry:
            result = self.storage.cleanup_scratch()
            self.assertEqual(result["eligible_files"], 0)
            self.assertEqual(result["removed_files"], 0)
            self.assertEqual(result["skipped_directories"], 1)
            entry.stat.assert_not_called()
        self.assertTrue(self.snapshot.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "SIMULATED retained data")


if __name__ == "__main__":
    unittest.main()
