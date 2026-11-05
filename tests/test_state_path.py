"""Synthetic packaged-process defaults; no real app package or user state."""
import contextlib
import ctypes
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from team_drive import fs_access as fs
from team_drive import onboarding as init
from team_drive import state_path as paths
from team_drive.errors import DriveError


class NativeKnownFolderCallTests(unittest.TestCase):
    def test_current_user_read_only_flags_and_allocated_result_are_used(self):
        calls, freed = [], []
        buffer = ctypes.create_unicode_buffer(r"C:\SyntheticData\PackageStorage")

        def query(guid, flags, token, output):
            calls.append((ctypes.string_at(guid, 16), flags, token))
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(buffer, ctypes.c_void_p)
            return 0

        function, release = mock.Mock(side_effect=query), mock.Mock(side_effect=lambda p: freed.append(p.value))
        libraries = {"shell32": SimpleNamespace(SHGetKnownFolderPath=function),
                     "ole32": SimpleNamespace(CoTaskMemFree=release)}
        with mock.patch.object(paths.ctypes, "WinDLL", side_effect=lambda name, **kw: libraries[name], create=True):
            result = paths._known_local_appdata(paths._RETURN_FILTER_REDIRECTION_TARGET)
        self.assertEqual(result, buffer.value)
        self.assertEqual(calls, [(uuid.UUID(paths._LOCAL_APP_DATA).bytes_le, 0x00044000, None)])
        self.assertEqual(freed, [ctypes.addressof(buffer)])
        self.assertEqual(calls[0][1] & 0x8000, 0, "No KF_FLAG_CREATE during a plan")

    def test_failed_known_folder_lookup_does_not_fall_back_and_frees_output(self):
        buffer = ctypes.create_unicode_buffer(r"C:\Unverified")
        def query(guid, flags, token, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(buffer, ctypes.c_void_p)
            return -2147467259
        release = mock.Mock()
        libs = {"shell32": SimpleNamespace(SHGetKnownFolderPath=mock.Mock(side_effect=query)),
                "ole32": SimpleNamespace(CoTaskMemFree=release)}
        with mock.patch.object(paths.ctypes, "WinDLL", side_effect=lambda name, **kw: libs[name], create=True):
            with self.assertRaises(DriveError) as error:
                paths._known_local_appdata(paths._RETURN_FILTER_REDIRECTION_TARGET)
        self.assertEqual(error.exception.code, "STATE_PATH_UNSAFE")
        release.assert_called_once()


class DefaultSelectionTests(unittest.TestCase):
    logical = PureWindowsPath(r"C:\Synthetic\Local")
    physical = PureWindowsPath(r"C:\Synthetic\PrivateAppData")

    @contextlib.contextmanager
    def selection(self, redirected=True):
        target = self.physical if redirected else self.logical
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(paths, "Path", PureWindowsPath))
            stack.enter_context(mock.patch.object(paths, "_check_spelling", side_effect=PureWindowsPath))
            stack.enter_context(mock.patch.object(fs, "_valid_windows_path", side_effect=PureWindowsPath, create=True))
            calls = stack.enter_context(mock.patch.object(paths, "_known_local_appdata", side_effect=[str(self.logical), str(target)]))
            guard = stack.enter_context(mock.patch.object(paths, "canonical_windows_state_path", side_effect=PureWindowsPath))
            equivalent = stack.enter_context(mock.patch.object(paths, "_verify_existing_alias"))
            yield calls, guard, equivalent

    def test_missing_default_uses_os_target_before_any_creation(self):
        with self.selection() as (calls, guard, equivalent):
            result = paths.windows_default_state_base(str(self.logical))
        self.assertEqual(result, self.physical / "df-mcp")
        guard.assert_called_once_with(self.physical / "df-mcp")
        equivalent.assert_called_once_with(self.logical / "df-mcp", result)
        self.assertEqual(calls.call_args_list, [mock.call(0x10000), mock.call(0x40000)])

    def test_unredirected_and_custom_environment_roots_are_not_rewritten(self):
        with self.selection(redirected=False) as (_, _, equivalent):
            self.assertEqual(paths.windows_default_state_base(str(self.logical)), self.logical / "df-mcp")
            equivalent.assert_not_called()
        custom = PureWindowsPath(r"D:\ChosenStateBase")
        with self.selection() as (_, guard, equivalent):
            self.assertEqual(paths.windows_default_state_base(str(custom)), custom / "df-mcp")
            guard.assert_called_once_with(custom / "df-mcp")
            equivalent.assert_not_called()

    def test_mapping_lookup_failure_does_not_use_unverified_logical_default(self):
        with self.selection() as (calls, guard, _):
            calls.side_effect = OSError("synthetic API failure")
            with self.assertRaises(DriveError) as error:
                paths.windows_default_state_base(str(self.logical))
            guard.assert_not_called()
        self.assertEqual(error.exception.code, "STATE_PATH_UNSAFE")


class AliasIdentityTests(unittest.TestCase):
    def setUp(self):
        self.logical = PureWindowsPath(r"C:\Synthetic\Local\df-mcp")
        self.physical = PureWindowsPath(r"C:\Synthetic\PrivateAppData\df-mcp")

    @contextlib.contextmanager
    def native(self, opens=None, identities=None, targets=None, attributes=0):
        opened, closed = mock.Mock(side_effect=opens or [11, 12]), mock.Mock()
        infos = identities or [7, 7, 7, 7]
        information = mock.Mock(side_effect=[SimpleNamespace(st_mode=stat.S_IFDIR | 0o700,
            st_dev=3, st_ino=inode, st_file_attributes=attributes) for inode in infos])
        final = mock.Mock(side_effect=targets or [str(self.physical).lower()] * 4)
        with contextlib.ExitStack() as stack:
            for key, value in {"_handle": opened, "_file_info": information, "_final_path": final,
                               "_canonical": fs.canonical_windows_path, "_REPARSE": 0x400,
                               "_kernel": SimpleNamespace(CloseHandle=closed, GetFileType=lambda h: 1)}.items():
                stack.enter_context(mock.patch.object(fs, key, value, create=True))
            yield opened, closed

    def test_same_identity_target_is_rechecked_while_both_handles_are_open(self):
        with self.native() as (opened, closed):
            paths._verify_existing_alias(self.logical, self.physical)
        self.assertEqual(opened.call_args_list, [mock.call(self.logical, directory=True), mock.call(self.physical, directory=True)])
        self.assertEqual(closed.call_args_list, [mock.call(12), mock.call(11)])

    def test_missing_old_base_does_not_create_or_open_a_second_profile(self):
        with self.native(opens=[FileNotFoundError()]) as (opened, closed):
            paths._verify_existing_alias(self.logical, self.physical)
        self.assertEqual(opened.call_count, 1)
        closed.assert_not_called()

    def test_missing_target_different_identity_reparse_or_rebinding_is_rejected(self):
        cases = [dict(opens=[11, FileNotFoundError()]), dict(identities=[7, 8]),
                 dict(attributes=0x400), dict(identities=[7, 7, 8]),
                 dict(targets=[str(self.physical).lower(), r"c:\outside"])]
        for case in cases:
            with self.subTest(case=case), self.native(**case) as (_, closed):
                with self.assertRaises(DriveError) as error:
                    paths._verify_existing_alias(self.logical, self.physical)
                self.assertEqual(error.exception.code, "STATE_PATH_UNSAFE")
                self.assertGreaterEqual(closed.call_count, 1)


class StateSelectionIntegrationTests(unittest.TestCase):
    def test_windows_configuration_write_uses_the_guard_returned_path(self):
        with tempfile.TemporaryDirectory(prefix="df-state-publication-") as folder:
            root = Path(folder).resolve()
            physical = root / "approved.json"
            logical = root / "unopened.json"
            directory = SimpleNamespace(path=physical, check=mock.Mock(), close=mock.Mock())
            operating = SimpleNamespace(name="nt", O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT,
                                        O_EXCL=os.O_EXCL, O_BINARY=getattr(os, "O_BINARY", 0),
                                        fdopen=os.fdopen, fsync=os.fsync,
                                        open=mock.Mock(wraps=os.open))
            with mock.patch.object(init, "os", operating), mock.patch("team_drive.graph_auth._CacheDirectory", return_value=directory):
                actual = init._save_config(logical, {"synthetic": True})
            self.assertEqual(actual, physical)
            self.assertEqual(operating.open.call_args.args[0], physical)
            self.assertEqual(physical.read_text(), '{"synthetic":true}\n')
            self.assertFalse(logical.exists())
            directory.close.assert_called_once()

    def test_os_selected_default_plans_without_creating_and_reuses_one_profile(self):
        with tempfile.TemporaryDirectory(prefix="df-state-model-") as folder:
            root = Path(folder).resolve()
            logical, physical = root / "ordinary", root / "app-private"
            logical.mkdir()
            physical.mkdir()
            source = root / "SIMULATED_SOURCE"
            source.mkdir()
            (source / "evidence.txt").write_text("synthetic", encoding="utf-8")
            def known(flags):
                return str(logical if flags == paths._NO_PACKAGE_REDIRECTION else physical)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(paths, "_known_local_appdata", side_effect=known))
                stack.enter_context(mock.patch.object(paths, "_check_spelling", side_effect=Path))
                stack.enter_context(mock.patch.object(fs, "_valid_windows_path", side_effect=Path, create=True))
                stack.enter_context(mock.patch.object(paths, "canonical_windows_state_path", side_effect=lambda p: Path(p).resolve()))
                stack.enter_context(mock.patch.object(paths, "_verify_existing_alias"))
                stack.enter_context(mock.patch.object(init, "default_state_base", side_effect=lambda: paths.windows_default_state_base(logical)))
                before = sorted(p.relative_to(root) for p in root.rglob("*"))
                plan = init.make_plan("local", str(source))
                self.assertEqual(sorted(p.relative_to(root) for p in root.rglob("*")), before)
                self.assertEqual(Path(plan["request"]["state_dir"]), physical / "df-mcp")
                with self.assertRaises(DriveError):
                    init.apply_plan(plan)
                self.assertFalse((physical / "df-mcp").exists())
                first = init.apply_plan(plan, confirmed=True)
                contents = Path(first["config_path"]).read_bytes()
                second = init.apply_plan(plan, confirmed=True)
                self.assertTrue(second["reused"])
                self.assertEqual(Path(second["config_path"]).read_bytes(), contents)
                profiles = init.profiles()
                self.assertEqual(len(profiles["profiles"]), 1)
                self.assertEqual(profiles["profiles"][0]["config_path"], first["config_path"])
                self.assertFalse(Path(plan["config"]["state_dir"]).exists())
                self.assertFalse((logical / "df-mcp").exists())

    def test_local_config_guard_error_is_not_reported_as_cloud_login_failure(self):
        with mock.patch("team_drive.graph_auth._CacheDirectory", side_effect=DriveError("AUTH_CACHE_UNSAFE", "synthetic")):
            for function, args in ((init._private_config, (Path("config.json"),)),
                                   (init._save_config, (Path("config.json"), {}))):
                with self.subTest(function=function.__name__), self.assertRaises(DriveError) as error:
                    function(*args)
                self.assertEqual(error.exception.code, "STATE_PATH_UNSAFE")


@unittest.skipUnless(os.name == "nt", "Native Windows fixed-local guard")
class NativeWindowsStatePathTests(unittest.TestCase):
    def test_uncreated_suffix_and_existing_file_stay_in_guarded_local_parent(self):
        with tempfile.TemporaryDirectory(prefix="df-state-native-") as folder:
            root = Path(folder).resolve()
            new = root / "Uncreated" / "state"
            self.assertEqual(paths.canonical_windows_state_path(new), new)
            self.assertFalse(new.parent.exists())
            existing = root / "config.json"
            existing.write_text("{}", encoding="utf-8")
            self.assertEqual(paths.canonical_windows_state_path(existing), existing)
            self.assertEqual(existing.read_text(), "{}")

    def test_native_known_folder_query_returns_valid_fixed_local_paths_without_creation(self):
        for flag in (paths._NO_PACKAGE_REDIRECTION, paths._RETURN_FILTER_REDIRECTION_TARGET):
            result = paths._known_local_appdata(flag)
            self.assertTrue(fs._valid_windows_path(result).is_absolute())

    def test_reserved_state_and_file_ancestor_remain_rejected_before_creation(self):
        with tempfile.TemporaryDirectory(prefix="df-state-denial-") as folder:
            root = Path(folder).resolve()
            ordinary_file = root / "file.txt"
            ordinary_file.write_text("synthetic", encoding="utf-8")
            candidates = [root / ".codex" / "state", ordinary_file / "child"]
            for candidate in candidates:
                with self.subTest(candidate=candidate), self.assertRaises(DriveError):
                    paths.canonical_windows_state_path(candidate)
            self.assertFalse((root / ".codex").exists())
            self.assertEqual(ordinary_file.read_text(), "synthetic")


if __name__ == "__main__":
    unittest.main()
