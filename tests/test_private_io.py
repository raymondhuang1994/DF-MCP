"""Private-storage I/O and Windows long paths use synthetic temporary data."""
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid
import zipfile

from team_drive import fs_access as fs
from team_drive.errors import DriveError
from team_drive.extract import iter_segments


def workbook_bytes():
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in {
            "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets><sheet name="Sample" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{package}"><Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": f'<sst xmlns="{ns}"><si><t>SIMULATED value</t></si></sst>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>',
        }.items():
            archive.writestr(name, body)
    return data.getvalue()


class PrivateIOTests(unittest.TestCase):
    deep = False

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-mcp-private-io-")
        self.base = Path(self.temp.name).resolve()
        source = self.base / "source"
        source.mkdir()
        self.source = fs.SourceAccess(source)
        length = max(190, len(str(self.base)) + 8) if self.deep else len(str(self.base)) + 6
        self.state = self.base / ("s" * (length - len(str(self.base)) - 1))
        self.storage = fs.StateAccess(self.state, self.source, Path(__file__).resolve().parents[1])

    def tearDown(self):
        self.storage.close()
        self.source.close()
        # The exact, newly created synthetic root is the only removal target.
        self.assertEqual(self.base, Path(self.temp.name).resolve())
        shutil.rmtree(fs.private_io_path(self.base))
        self.temp.cleanup()

    def private_name(self):
        return "job-temp-" + uuid.uuid4().hex

    def test_directory_does_not_reclassify_body_oserror(self):
        marker = OSError(errno.EIO, "SIMULATED source I/O")
        with self.assertRaises(OSError) as caught:
            with self.storage.directory("body", create=True):
                raise marker
        self.assertIs(caught.exception, marker)

    def test_private_stream_does_not_reclassify_source_failure(self):
        marker = DriveError("SOURCE_UNAVAILABLE", "SIMULATED source failure")
        with self.storage.directory(self.private_name(), create=True) as directory:
            with self.assertRaises(DriveError) as caught:
                with self.storage.new_private(directory / "sample.txt"):
                    raise marker
        self.assertIs(caught.exception, marker)

    def test_private_stream_operations_report_storage_io(self):
        original_fdopen = os.fdopen
        class FailingStream:
            def __init__(self, stream, operation):
                self.stream, self.operation = stream, operation
            def __getattr__(self, name):
                if name == self.operation:
                    def fail(*args, **kwargs):
                        if name == "close":
                            self.stream.close()
                        raise OSError(errno.ENOSPC, "SIMULATED full private disk")
                    return fail
                return getattr(self.stream, name)
            def __next__(self):
                raise OSError(errno.EIO, "SIMULATED private read failure")
        for operation in ("write", "read", "flush", "close", "next"):
            with self.subTest(operation=operation):
                with self.storage.directory(self.private_name(), create=True) as directory:
                    path = directory / "sample.txt"
                    if operation in ("read", "next"):
                        with self.storage.new_private(path) as output:
                            output.write("SIMULATED\n")
                    def fdopen(*args, **kwargs):
                        return FailingStream(original_fdopen(*args, **kwargs), operation)
                    with mock.patch.object(fs.os, "fdopen", side_effect=fdopen):
                        manager = self.storage.open_private(path, "r") if operation in ("read", "next") else self.storage.new_private(path)
                        with self.assertRaises(DriveError) as caught:
                            with manager as stream:
                                if operation == "write":
                                    stream.write("SIMULATED")
                                elif operation == "next":
                                    next(stream)
                                elif operation != "close":
                                    getattr(stream, operation)()
                    self.assertEqual(caught.exception.code, "STATE_IO_ERROR")
                    self.assertNotIn(str(path), caught.exception.message)

    def test_close_failure_preserves_primary_source_error(self):
        raw = mock.Mock()
        raw.close.side_effect = OSError(errno.ENOSPC, "SIMULATED close failure")
        marker = DriveError("SOURCE_UNAVAILABLE", "SIMULATED source failure")
        with self.assertRaises(DriveError) as caught:
            with fs._private_stream(raw, self.state / "sample.txt"):
                raise marker
        self.assertIs(caught.exception, marker)
        raw.close.assert_called_once()


@unittest.skipUnless(os.name == "nt", "Windows native long-path regression")
class WindowsLongPathTests(PrivateIOTests):
    deep = True

    def test_private_snapshot_create_read_delete_beyond_max_path(self):
        name = self.private_name()
        with self.storage.directory(name, create=True) as directory:
            path = directory / ("snapshot-" + uuid.uuid4().hex + ".xlsx")
            self.assertGreater(len(str(path)), 260)
            with self.storage.new_private(path, binary=True) as output:
                output.write(b"SIMULATED")
            with self.storage.open_private(path, "r") as source:
                self.assertEqual(source.read(), "SIMULATED")
            self.storage.unlink_private(path)
            self.assertFalse(os.path.exists(fs.private_io_path(path)))
        self.storage.remove_empty_directory(name)
        self.assertFalse(os.path.exists(fs.private_io_path(directory)))

    def test_shared_strings_and_subprocess_keep_long_scratch_local(self):
        name = self.private_name()
        with self.storage.directory(name, create=True) as directory:
            path = directory / ("snapshot-" + uuid.uuid4().hex + ".xlsx")
            with self.storage.new_private(path, binary=True) as output:
                output.write(workbook_bytes())
            records = list(iter_segments(path, {}))
            self.assertEqual(records[0]["data"]["cells"][0]["value"], "SIMULATED value")
            env = dict(os.environ, TMP=str(directory), TEMP=str(directory), TMPDIR=str(directory), PYTHONDONTWRITEBYTECODE="1")
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "python") + os.pathsep + env.get("PYTHONPATH", "")
            result = subprocess.run([sys.executable, "-B", "-m", "team_drive.extract", fs.private_io_path(path), "{}"], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(json.loads(result.stdout.splitlines()[-1])["done"])
            self.assertEqual(os.listdir(fs.private_io_path(directory)), [path.name])

    def test_private_adapter_does_not_accept_nonlocal_or_relative_paths(self):
        for path in ("relative.txt", "\\\\server\\share\\file", "\\\\.\\C:\\file", "\\\\?\\UNC\\server\\share\\file"):
            with self.subTest(path=path), self.assertRaises(DriveError):
                fs.private_io_path(path)


if __name__ == "__main__":
    unittest.main()
