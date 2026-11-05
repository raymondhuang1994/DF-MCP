"""SMB contracts use local synthetic fixtures; no company server is contacted."""
import contextlib
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

from team_drive import fs_access
from team_drive.engine import Engine
from team_drive.errors import DriveError
from team_drive.network_source import WindowsShareSource, normalize_share_path, resolve_share_path, share_settings
from team_drive.service_config import settings


class _RecordingScanProgress:
    """Observe the scan's public progress contract without timing real I/O."""
    def __init__(self):
        self.counters, self.stages, self.operations, self.active = {}, [], [], []

    def set_stage(self, name):
        self.stages.append(name)

    def increment(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n

    @contextlib.contextmanager
    def operation(self, name):
        self.operations.append(name)
        self.active.append(name)
        try:
            yield
        finally:
            self.active.pop()

    def snapshot(self):
        return {"counters": dict(self.counters), "stage": self.stages[-1] if self.stages else None,
                "operations": list(self.operations), "active": list(self.active)}


class ShareSyntaxTests(unittest.TestCase):
    def test_unc_anchor_trailing_slash_has_one_canonical_spelling(self):
        from pathlib import PureWindowsPath
        root = r"\\LOCALHOST\SyntheticShare"
        expected = "\\\\localhost\\syntheticshare\\"
        for value in (root, root + "\\", str(PureWindowsPath(root) / "."),
                      r"\\?\UNC\LOCALHOST\SyntheticShare"):
            self.assertEqual(fs_access.canonical_windows_path(value), expected)
        self.assertNotEqual(fs_access.canonical_windows_path(root + r"\child"), expected)
        self.assertNotEqual(fs_access.canonical_windows_path(root + "different"), expected)

    def test_explicit_unc_and_mapped_paths_only(self):
        self.assertEqual(normalize_share_path(r"\\server.example\approved\Finance"), r"\\server.example\approved\Finance")
        self.assertEqual(normalize_share_path("Z:\\Finance\\"), "Z:\\Finance")
        for path in ("relative", "C:relative", "/mnt/share", "https://example.invalid/docs",
                     r"\\?\UNC\server\share", r"\\.\pipe\secret", r"\\server",
                     r"\\server\share\..\outside", r"\\server\share\a:stream",
                     r"\\server\share\CON", "\\\\server\\share\\trailing ",
                     "\\\\server\\share\\\\empty"):
            with self.subTest(path=path), self.assertRaises(DriveError):
                normalize_share_path(path)

    def test_configuration_keeps_explicit_network_spelling_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "profile.json"
            config = {"provider": "windows_share", "root": r"Z:\Finance", "state_dir": str(Path(temp) / "state")}
            path.write_text(json.dumps(config))
            actual = settings(path)
            self.assertEqual(actual["config"]["root"], r"Z:\Finance")
            self.assertEqual(actual["config"]["windows_share"]["max_items"], 10000)
            self.assertFalse((Path(temp) / "state").exists())
        for config in ({"password": "synthetic"}, {"max_items": True}, {"max_depth": 129}, {"scan_budget_seconds": float("inf")}):
            with self.assertRaises(DriveError):
                share_settings(config)

    @unittest.skipIf(os.name == "nt", "Windows validates mapped drives natively")
    def test_non_windows_fails_without_connecting_or_treating_unc_as_local(self):
        with self.assertRaises(DriveError) as caught:
            fs_access.SourceAccess(r"\\server\share", allow_network=True)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_PLATFORM")

    @unittest.skipUnless(os.name == "nt", "Windows native local boundary")
    def test_windows_unc_encoding_and_local_provider_stay_strict(self):
        self.assertEqual(fs_access._extended(r"\\server\share\a"), r"\\?\UNC\server\share\a")
        self.assertEqual(fs_access._canonical(r"\\?\UNC\SERVER\share\a"), r"\\server\share\a")
        with self.assertRaises(DriveError):
            fs_access._valid_windows_path(r"\\server\share")
        with self.assertRaises(DriveError) as caught:
            fs_access._WindowsDirectories(r"\\server\share", allow_network=True, state=True)
        self.assertEqual(caught.exception.code, "UNSAFE_STATE")
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(DriveError) as caught:
            resolve_share_path(temp)
        self.assertEqual(caught.exception.code, "SHARE_UNAVAILABLE")

    @unittest.skipUnless(os.name == "nt", "Windows native protocol ABI")
    def test_native_protocol_rejects_offline_cache_and_unknown_provider(self):
        import ctypes
        for kind, flags in ((0x00020000, 2), (0x00260000, 0), (0x002E0000, 0)):
            def fake_info(_handle, info_class, buffer, size):
                self.assertEqual(info_class, 13)
                self.assertEqual(buffer._obj.version, 2)
                self.assertEqual(buffer._obj.size, ctypes.sizeof(fs_access._RemoteProtocolInfo))
                self.assertEqual(size, ctypes.sizeof(fs_access._RemoteProtocolInfo))
                buffer._obj.version, buffer._obj.protocol, buffer._obj.flags = 3, kind, flags
                return 1
            with self.subTest(kind=kind, flags=flags), mock.patch.object(fs_access._kernel, "GetFileInformationByHandleEx", side_effect=fake_info):
                with self.assertRaises(DriveError) as caught:
                    fs_access._share_info(1)
            self.assertEqual(caught.exception.code, "SHARE_PROTOCOL_UNVERIFIED")

    @unittest.skipUnless(os.name == "nt", "Windows native read-only CreateFile contract")
    def test_native_share_open_never_requests_write_create_or_delete_sharing(self):
        with mock.patch.object(fs_access._kernel, "CreateFileW", return_value=42) as create:
            self.assertEqual(fs_access._handle(r"\\synthetic-server\approved\folder", directory=True), 42)
        args = create.call_args[0]
        self.assertEqual(args[0], r"\\?\UNC\synthetic-server\approved\folder")
        self.assertEqual(args[1], 0x80000000)
        self.assertEqual(args[2], 1)
        self.assertEqual(args[4], 3)
        self.assertEqual(args[5], 0x00200000 | 0x02000000)

    @unittest.skipUnless(os.name == "nt", "Native Windows scan error propagation")
    def test_native_share_walk_preserves_specific_metadata_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "synthetic.csv").write_text("SIMULATED", encoding="utf-8")
            source = object.__new__(fs_access.WindowsSource)
            source.root, source.network = root, True
            errors = []
            guard = SimpleNamespace(handles=[1], close=mock.Mock())
            directory = SimpleNamespace(st_dev=1, st_ino=2, st_mtime_ns=3, st_ctime_ns=4)
            with mock.patch.object(source, "check_root"), mock.patch.object(source, "_parent_guard", return_value=guard), \
                    mock.patch.object(fs_access, "_share_info", return_value=directory), \
                    mock.patch.object(source, "observe", side_effect=DriveError("SHARE_PROTOCOL_UNVERIFIED", "Synthetic protocol failure")):
                with self.assertRaises(DriveError) as caught:
                    list(source.walk(errors))
            self.assertEqual(caught.exception.code, "SHARE_PROTOCOL_UNVERIFIED")
            self.assertTrue(errors)

    def _assert_empty_directory_interleaving_rejected(self, change):
        # Real directory enumeration, with deterministic native metadata and
        # changes injected exactly when the initial enumeration guard closes.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "approved"
            root.mkdir()
            source = object.__new__(fs_access.WindowsSource)
            source.root, source.network, source._closed = root, True, False
            opens, closes = [], []

            def parent_guard(_folder):
                number = len(opens) + 1
                opens.append(number)
                if number == 2 and change == "revocation":
                    raise DriveError("SHARE_ACCESS_DENIED", "Synthetic current access revoked")

                def close():
                    closes.append(number)
                    if number == 1:
                        if change == "replacement":
                            root.rename(base / "old-directory")
                            root.mkdir()
                        if change == "addition":
                            (root / "new.csv").write_text("SIMULATED", encoding="utf-8")
                return SimpleNamespace(handles=[number], close=close)

            def info(handle):
                return SimpleNamespace(st_dev=1, st_ino=3 if handle == 2 and change == "replacement" else 2,
                                       st_mtime_ns=11 if handle == 2 and change == "addition" else 10,
                                       st_ctime_ns=21 if handle == 2 and change == "addition" else 20)

            errors = []
            with mock.patch.object(source, "check_root"), mock.patch.object(source, "_parent_guard", side_effect=parent_guard), \
                    mock.patch.object(fs_access, "_share_info", side_effect=info), mock.patch.object(source, "observe") as observe:
                with self.assertRaises(DriveError) as caught:
                    list(source.walk(errors))
            expected = "SHARE_ACCESS_DENIED" if change == "revocation" else "SHARE_DIRECTORY_CHANGED"
            self.assertEqual(caught.exception.code, expected)
            self.assertTrue(errors)
            self.assertEqual(closes, [1] if change == "revocation" else [1, 2])
            observe.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Native Windows short-lived directory guard")
    def test_empty_directory_replacement_cannot_publish_complete_manifest(self):
        self._assert_empty_directory_interleaving_rejected("replacement")

    @unittest.skipUnless(os.name == "nt", "Native Windows short-lived directory guard")
    def test_file_added_after_enumeration_cannot_publish_complete_manifest(self):
        self._assert_empty_directory_interleaving_rejected("addition")

    @unittest.skipUnless(os.name == "nt", "Native Windows short-lived directory guard")
    def test_access_revoked_before_directory_recheck_cannot_publish_complete_manifest(self):
        self._assert_empty_directory_interleaving_rejected("revocation")


@unittest.skipUnless(os.name == "nt", "Windows desired-access and directory-chain contract")
class AncestorAccessTests(unittest.TestCase):
    @contextlib.contextmanager
    def native_tree(self):
        """Keep the real guard algorithms; replace only Windows API results."""
        import ctypes
        root = Path(r"\\synthetic-server\share\Department\Approved")
        fixture = SimpleNamespace(root=root, calls=[], handles={}, closed=[], identities={},
                                  denied=set(), reparse=set(), targets={}, changed=set())

        def create(path, access, sharing, security, creation, flags, template):
            path = fs_access.canonical_windows_path(path)
            fixture.calls.append((path, access, sharing, creation, flags))
            if (path, access) in fixture.denied:
                ctypes.set_last_error(5)
                return fs_access._INVALID
            handle = len(fixture.handles) + 100
            fixture.handles[handle] = path
            fixture.identities.setdefault(path, len(fixture.identities) + 1)
            return handle

        def info(handle):
            path = fixture.handles[handle]
            return SimpleNamespace(st_mode=stat.S_IFREG if path.endswith(".csv") else stat.S_IFDIR,
                st_dev=7, st_ino=fixture.identities[path] + (1000 if path in fixture.changed else 0),
                st_size=10, st_mtime_ns=100, st_ctime_ns=100, st_nlink=1,
                st_file_attributes=0x400 if path in fixture.reparse else 0, st_reparse_tag=0)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(fs_access, "_valid_windows_path", side_effect=lambda value, **_kwargs: Path(value)))
            stack.enter_context(mock.patch.object(fs_access, "_long_path", side_effect=str))
            stack.enter_context(mock.patch.object(fs_access, "_share_info", side_effect=info))
            stack.enter_context(mock.patch.object(fs_access, "_file_info", side_effect=info))
            stack.enter_context(mock.patch.object(fs_access, "_final_path", side_effect=lambda handle:
                fixture.targets.get(fixture.handles[handle], fixture.handles[handle])))
            stack.enter_context(mock.patch.object(fs_access._kernel, "CreateFileW", side_effect=create))
            stack.enter_context(mock.patch.object(fs_access._kernel, "CloseHandle", side_effect=fixture.closed.append))
            stack.enter_context(mock.patch.object(fs_access._kernel, "GetFileType", return_value=1))
            yield fixture
        self.assertEqual(set(fixture.closed), set(fixture.handles), "Every successfully opened test handle must close")

    def assert_source_modes(self, fixture):
        ancestors = {fs_access.canonical_windows_path(path) for path in fixture.root.parents}
        self.assertTrue(fixture.calls)
        for path, access, sharing, creation, flags in fixture.calls:
            self.assertEqual(access, 0x80 if path in ancestors else 0x80000000, path)
            self.assertEqual(creation, 3)
            self.assertFalse(sharing & 4, "Source handles must not gain delete sharing")

    def test_attribute_handle_uses_only_0x80_without_changing_open_or_sharing_flags(self):
        with mock.patch.object(fs_access._kernel, "CreateFileW", return_value=42) as create:
            self.assertEqual(fs_access._handle(r"\\synthetic-server\share\Parent", directory=True, attributes_only=True), 42)
        args = create.call_args.args
        self.assertEqual(args[1], 0x80)
        self.assertEqual(args[2], 1)
        self.assertEqual(args[4], 3)
        self.assertEqual(args[5], 0x00200000 | 0x02000000)
        for forbidden in ({"directory": False}, {"write": True}, {"create": True}, {"rename": True}, {"metadata_read": True}, {"exclusive": True}):
            options = dict(directory=True, attributes_only=True)
            options.update(forbidden)
            with self.subTest(options=options), mock.patch.object(fs_access._kernel, "CreateFileW") as create:
                with self.assertRaises(DriveError):
                    fs_access._handle(r"\\synthetic-server\share\Parent", **options)
                create.assert_not_called()

    def test_initial_parent_guard_check_and_root_reopen_keep_the_same_source_boundary(self):
        with self.native_tree() as tree:
            parent = fs_access.canonical_windows_path(tree.root.parent)
            tree.denied.add((parent, 0x80000000))
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            try:
                source.check_root()
                guard = source._parent_guard(tree.root / "nested")
                try:
                    guard.check()
                finally:
                    guard.close()
            finally:
                source.close()
            self.assert_source_modes(tree)
            self.assertGreater(sum(path == parent for path, *_ in tree.calls), 2)
            self.assertIn((fs_access.canonical_windows_path(tree.root / "nested"), 0x80000000),
                          [(path, access) for path, access, *_ in tree.calls])

    def test_root_and_file_read_denials_never_fall_back_to_attributes(self):
        with self.native_tree() as tree:
            root = fs_access.canonical_windows_path(tree.root)
            tree.denied.add((root, 0x80000000))
            with self.assertRaises(DriveError):
                fs_access.WindowsSource(tree.root, allow_network=True)
            self.assertEqual([access for path, access, *_ in tree.calls if path == root], [0x80000000])
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            path = fs_access.canonical_windows_path(tree.root / "private.csv")
            tree.denied.add((path, 0x80000000))
            try:
                with self.assertRaises(DriveError):
                    source.open_read("private.csv")
            finally:
                source.close()
            self.assertEqual([access for current, access, *_ in tree.calls if current == path], [0x80000000])

    def test_ancestor_attribute_denial_is_rechecked_and_does_not_bypass_access(self):
        with self.native_tree() as tree:
            guard = fs_access._WindowsDirectories(tree.root, allow_network=True, source_root=tree.root)
            parent = fs_access.canonical_windows_path(tree.root.parent)
            tree.denied.add((parent, 0x80))
            try:
                with self.assertRaises((DriveError, OSError)):
                    guard.check()
            finally:
                guard.close()
            self.assert_source_modes(tree)
        with self.native_tree() as tree:
            tree.denied.add((fs_access.canonical_windows_path(tree.root.parent), 0x80))
            with self.assertRaises(DriveError):
                fs_access.WindowsSource(tree.root, allow_network=True)

    def test_similar_prefix_sibling_cannot_borrow_an_approved_root(self):
        with self.native_tree() as tree:
            with self.assertRaises(DriveError):
                fs_access._WindowsDirectories(tree.root.parent / "Approved-extra" / "nested",
                    allow_network=True, source_root=tree.root)
            self.assertFalse(tree.calls, "Reject a mismatched root before opening any ancestor")

    def test_attribute_ancestors_still_reject_identity_target_and_reparse_changes(self):
        for change in ("identity", "target", "reparse"):
            with self.subTest(change=change), self.native_tree() as tree:
                source = fs_access.WindowsSource(tree.root, allow_network=True)
                parent = fs_access.canonical_windows_path(tree.root.parent)
                if change == "identity":
                    tree.changed.add(parent)
                elif change == "target":
                    tree.targets[parent] = r"\\other-server\unexpected\parent"
                else:
                    tree.reparse.add(parent)
                try:
                    with self.assertRaises(DriveError):
                        source.check_root()
                finally:
                    source.close()
                self.assert_source_modes(tree)

    def test_local_directories_and_unscoped_network_guards_keep_generic_read(self):
        for network, path in ((False, Path(r"C:\synthetic\local")), (True, Path(r"\\synthetic-server\share\unscoped"))):
            with self.subTest(network=network), self.native_tree() as tree:
                guard = fs_access._WindowsDirectories(path, allow_network=network)
                try:
                    guard.check()
                finally:
                    guard.close()
                self.assertTrue(tree.calls)
                self.assertEqual({access for _path, access, *_ in tree.calls}, {0x80000000})

    def test_network_observation_opens_one_fresh_chain_per_item(self):
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            try:
                # Reproduce the previous two-chain sequence with the actual
                # guard and mocked native calls, then compare the new path.
                tree.calls.clear()
                source.check_root()
                source.observe("nested/budget.csv")
                previous = list(tree.calls)
                tree.calls.clear()
                source.observe("nested/budget.csv")
                current = list(tree.calls)
                root = fs_access.canonical_windows_path(tree.root)
                self.assertEqual(sum(path == root for path, *_ in previous), 2)
                self.assertEqual(sum(path == root for path, *_ in current), 1)
                self.assertEqual(len(previous) - len(current), len(tree.root.parts))
                self.assertEqual(len(current), len((tree.root / "nested").parts) + 1)
                self.assert_source_modes(tree)
                # A successful item never supplies a cached permission decision
                # for the next one, even with the same parent and filename.
                tree.denied.add((root, 0x80000000))
                with self.assertRaises(DriveError):
                    source.observe("nested/budget.csv")
            finally:
                source.close()

    def test_network_observation_keeps_chain_parent_and_target_checks(self):
        for change in ("ancestor_access", "ancestor_identity", "ancestor_target", "ancestor_reparse",
                       "root_identity", "root_target", "root_reparse", "file_access", "file_target",
                       "parent_identity", "parent_reparse", "traversal"):
            with self.subTest(change=change), self.native_tree() as tree:
                source = fs_access.WindowsSource(tree.root, allow_network=True)
                ancestor = fs_access.canonical_windows_path(tree.root.parent)
                root = fs_access.canonical_windows_path(tree.root)
                file = fs_access.canonical_windows_path(tree.root / "nested" / "budget.csv")
                if change == "ancestor_access":
                    tree.denied.add((ancestor, 0x80))
                elif change == "ancestor_identity":
                    tree.changed.add(ancestor)
                elif change == "ancestor_target":
                    tree.targets[ancestor] = r"\\other-server\outside\Department"
                elif change == "ancestor_reparse":
                    tree.reparse.add(ancestor)
                elif change == "root_identity":
                    tree.changed.add(root)
                elif change == "root_target":
                    tree.targets[root] = r"\\other-server\outside\Approved"
                elif change == "root_reparse":
                    tree.reparse.add(root)
                elif change == "file_access":
                    tree.denied.add((file, 0x80000000))
                elif change == "file_target":
                    tree.targets[file] = r"\\other-server\outside\budget.csv"
                elif change == "parent_reparse":
                    tree.reparse.add(fs_access.canonical_windows_path(tree.root / "nested"))
                try:
                    with self.assertRaises((DriveError, OSError)):
                        source.observe("../outside.csv" if change == "traversal" else "nested/budget.csv",
                                       parent_identity=(7, -1) if change == "parent_identity" else None)
                finally:
                    source.close()

    def test_local_observation_still_rechecks_its_pinned_root(self):
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(Path(r"C:\synthetic\local"))
            try:
                with mock.patch.object(source.guard, "check", wraps=source.guard.check) as check:
                    source.observe("budget.csv")
                check.assert_called_once_with()
            finally:
                source.close()

    def test_native_scan_progress_counts_directories_and_items_without_pinning_yields(self):
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            progress = _RecordingScanProgress()
            listings = {tree.root: ["nested", "budget.csv"], tree.root / "nested": ["notes.csv"]}
            @contextlib.contextmanager
            def scandir(path):
                self.assertIn("directory_enumeration", progress.active)
                self.assertEqual(progress.stages[-1], "enumerating")
                yield iter(SimpleNamespace(name=name) for name in listings[path])
            try:
                rows = []
                with mock.patch.object(fs_access.os, "scandir", side_effect=scandir):
                    for row in source.walk([], progress=progress):
                        rows.append(row[0])
                        self.assertEqual(set(tree.closed), set(tree.handles),
                                         "No source guard may remain held across a yielded item")
                self.assertEqual(set(rows), {"budget.csv", "nested/notes.csv"})
                self.assertEqual(progress.counters, {"directories_enumerated": 2, "entries_enumerated": 3,
                                                    "items_verified": 3})
                self.assertEqual(progress.operations.count("root_validation"), 2)
                self.assertEqual(progress.operations.count("directory_enumeration"), 2)
                self.assertEqual(progress.operations.count("item_observation"), 3)
                self.assertEqual(progress.operations.count("directory_validation"), 7)
                self.assertEqual(progress.operations.count("native_metadata"), 7)
                self.assertEqual(progress.active, [])
            finally:
                source.close()


    def test_native_directory_batch_verifies_units_and_releases_all_handles(self):
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            progress = _RecordingScanProgress()
            @contextlib.contextmanager
            def scandir(path):
                self.assertEqual(path, tree.root)
                yield iter(SimpleNamespace(name=name) for name in ("nested", "budget.csv"))
            try:
                with mock.patch.object(fs_access.os, "scandir", side_effect=scandir):
                    result = source.scan_directory("", expected_identity=[str(v) for v in source.identity], max_items=10, progress=progress)
                self.assertEqual([row[0] for row in result["files"]], ["budget.csv"])
                self.assertEqual([row["path"] for row in result["directories"]], ["nested"])
                self.assertEqual(result["entries"], 2)
                self.assertEqual(set(tree.closed), set(tree.handles))
                self.assert_source_modes(tree)
                self.assertEqual(source.directory_fingerprint(""), result["fingerprint"])
            finally:
                source.close()

    def test_native_directory_batch_rechecks_access_before_publishing(self):
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            @contextlib.contextmanager
            def scandir(path):
                yield iter(())
                tree.denied.add((fs_access.canonical_windows_path(tree.root), 0x80000000))
            try:
                with mock.patch.object(fs_access.os, "scandir", side_effect=scandir):
                    with self.assertRaises(DriveError):
                        source.scan_directory("", max_items=10)
                self.assert_source_modes(tree)
            finally:
                source.close()

    def test_native_directory_batch_limit_does_not_publish_unverified_names(self):
        with self.native_tree() as tree:
            source = fs_access.WindowsSource(tree.root, allow_network=True)
            @contextlib.contextmanager
            def scandir(path):
                yield iter(SimpleNamespace(name=name) for name in ("first.csv", "second.csv"))
            try:
                with mock.patch.object(fs_access.os, "scandir", side_effect=scandir), mock.patch.object(source, "observe") as observe:
                    with self.assertRaises(DriveError) as caught:
                        source.scan_directory("", max_items=1)
                self.assertEqual(caught.exception.code, "SHARE_SCAN_LIMIT")
                observe.assert_not_called()
                self.assertEqual(set(tree.closed), set(tree.handles))
            finally:
                source.close()


class SyntheticShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="df-share-contract-")
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.mapping = r"\\synthetic-server\approved"
        self.principal = "synthetic-user-binding"
        self.natives = []
        outer = self
        local_type = fs_access.WindowsSource if os.name == "nt" else fs_access.PosixSource

        def normalized(info):
            return SimpleNamespace(**{key: (getattr(info, "st_birthtime_ns", info.st_ctime_ns) if key == "st_ctime_ns" else getattr(info, key, 0))
                for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink", "st_file_attributes")})

        class FakeNative:
            def __init__(self, _root, allow_network=False):
                if not allow_network:
                    raise AssertionError("Network opt-in missing")
                self.local = local_type(outer.root)
                self.root, self.identity = self.local.root, self.local.identity
                self.target = outer.mapping
                self.denied, self.offline, self.incomplete = set(), False, False
                outer.natives.append(self)

            def check_root(self):
                if self.offline:
                    raise DriveError("SHARE_ROOT_UNAVAILABLE", "Synthetic unavailable share")
                self.local.check_root()

            def observe(self, relative):
                self.check_root()
                if relative in self.denied:
                    raise PermissionError("Synthetic denial")
                return normalized(self.root.joinpath(*relative.split("/")).lstat())

            def open_read(self, relative):
                self.observe(relative)
                fd, info = self.local.open_read(relative)
                return fd, normalized(info)

            def stat_fd(self, fd):
                return normalized(os.fstat(fd))

            def walk(self, errors, **limits):
                self.check_root()
                for row in self.local.walk(errors):
                    yield row
                if self.incomplete:
                    errors.append(True)

            def close(self):
                self.local.close()

        self.patches = [mock.patch("team_drive.network_source.resolve_share_path", side_effect=lambda _raw: self.mapping),
                        mock.patch("team_drive.network_source._principal_binding", side_effect=lambda: self.principal),
                        mock.patch.object(fs_access, "WindowsSource", FakeNative, create=True)]
        for patch in self.patches:
            patch.start()
        self.source = WindowsShareSource(r"Z:\Approved", max_file_bytes=1024)
        self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        self.source.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def write(self, relative="budget.csv", content="category,amount\ntravel,120\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def build_engine(self):
        config = self.base / "profile.json"
        config.write_text(json.dumps({"provider": "windows_share", "root": r"Z:\Approved", "state_dir": str(self.base / "index")}))
        self.engine = Engine(str(config), autostart=False)
        self.engine.scan_once()
        return self.engine.search("travel")["files"][0]

    @staticmethod
    def replace_fixture(replacement, target):
        if os.name != "nt":
            os.replace(replacement, target)
            return
        # Production directory guards intentionally prohibit rebinding. Win32
        # MoveFileEx also reopens the parent for writing, conflicting with that
        # guard. This synthetic writer uses the existing same-directory native
        # rename so the test still replaces file identity while guards stay held.
        # It does not relax source handles or claim ordinary atomic saves work.
        handle = fs_access._handle(replacement, rename=True)
        try:
            fs_access._rename_same_directory(handle, target.name)
        finally:
            fs_access._kernel.CloseHandle(handle)

    def test_digest_binds_content_identity_and_current_source(self):
        path = self.write()
        snapshot = io.BytesIO()
        first, info = self.source.digest("budget.csv", snapshot)
        self.assertEqual(snapshot.getvalue(), path.read_bytes())
        origin = self.source.evidence("budget.csv")
        self.assertEqual(origin["source_version"], first)
        self.assertEqual(origin["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotEqual(first, origin["content_sha256"])
        self.assertEqual(info.st_size, len(snapshot.getvalue()))
        replacement = self.write("replacement.csv")
        self.replace_fixture(replacement, path)
        changed, _ = self.source.digest("budget.csv")
        self.assertNotEqual(first, changed, "A replaced object cannot reuse the old evidence version.")

    def test_incomplete_walk_never_publishes_partial_manifest(self):
        self.write()
        list(self.source.walk([]))
        before = self.source.evidence("budget.csv")
        self.write("new.csv")
        self.source._native.incomplete = True
        errors = []
        with self.assertRaises(DriveError):
            list(self.source.walk(errors))
        self.assertTrue(errors)
        self.assertEqual(self.source.evidence("budget.csv"), before)
        self.assertEqual(self.source.evidence("new.csv"), {})

    def test_progress_can_show_unpublished_items_when_scan_times_out(self):
        self.write()
        list(self.source.walk([]))
        before = self.source.evidence("budget.csv")
        self.write("new.csv")
        progress = _RecordingScanProgress()
        clock = [10.0]
        info = self.source._native.observe("budget.csv")

        def interrupted_walk(errors, **options):
            self.assertIs(options["progress"], progress)
            progress.set_stage("enumerating")
            with progress.operation("directory_enumeration"):
                progress.increment("directories_enumerated")
                progress.increment("entries_enumerated", 2)
            progress.set_stage("verifying")
            progress.increment("items_verified")
            yield "budget.csv", info, False, False
            self.assertEqual(progress.counters["pending_items"], 1)
            self.assertEqual(self.source.evidence("new.csv"), {})
            # This is elapsed time at an I/O boundary, not a real sleep and
            # not a claim that an in-flight native call can be interrupted.
            clock[0] += 121
            progress.increment("items_verified")
            yield "new.csv", info, False, False

        rows, errors = [], []
        with mock.patch.object(self.source._native, "walk", side_effect=interrupted_walk), \
                mock.patch("team_drive.network_source.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(DriveError) as caught:
                for row in self.source.walk(errors, progress=progress):
                    rows.append(row)
        self.assertEqual(caught.exception.code, "SHARE_TIMEOUT")
        self.assertEqual(rows, [], "A failed scan must not yield its partial manifest")
        self.assertTrue(errors)
        self.assertEqual(progress.counters, {"directories_enumerated": 1, "entries_enumerated": 2,
                                             "items_verified": 2, "pending_items": 1})
        self.assertEqual(self.source.evidence("budget.csv"), before)
        self.assertEqual(self.source.evidence("new.csv"), {})
        self.assertNotIn("publishing", progress.stages)

    def test_progress_reports_publish_only_after_final_scope_checks(self):
        self.write()
        progress = _RecordingScanProgress()
        actual = list(self.source.walk([], progress=progress))
        self.assertEqual(len(actual), 1)
        self.assertEqual(progress.counters["pending_items"], 1)
        self.assertEqual(progress.stages[-1], "publishing")
        self.assertEqual(progress.operations.count("root_validation"), 2)
        self.assertEqual(progress.active, [])
        self.assertTrue(self.source.evidence("budget.csv"))
        # A final mapping change discards the next manifest even if every
        # file was already observed and queued for publication.
        original_walk = self.source._native.walk
        def changed_after_rows(errors, **options):
            yield from original_walk(errors, **options)
            self.mapping = r"\\synthetic-server\different"
        progress = _RecordingScanProgress()
        with mock.patch.object(self.source._native, "walk", side_effect=changed_after_rows), self.assertRaises(DriveError):
            list(self.source.walk([], progress=progress))
        self.assertEqual(progress.counters["pending_items"], 1)
        self.assertNotIn("publishing", progress.stages)
        self.assertEqual(progress.active, [])

    def test_mapping_and_identity_changes_reject_prior_scope(self):
        old = self.mapping
        self.mapping = r"\\synthetic-server\different"
        with self.assertRaises(DriveError) as caught:
            self.source.check_root()
        self.assertEqual(caught.exception.code, "SHARE_ROOT_CHANGED")
        self.mapping = old
        self.principal = "different-synthetic-user"
        with self.assertRaises(DriveError) as caught:
            self.source.check_root()
        self.assertEqual(caught.exception.code, "SHARE_IDENTITY_CHANGED")

    def test_revocation_and_changed_content_block_cached_candidates(self):
        self.write()
        self.source.digest("budget.csv")
        origin = self.source.evidence("budget.csv")
        self.source._native.denied.add("budget.csv")
        with self.assertRaises(DriveError) as caught:
            self.source.verify_origin("budget.csv", origin)
        self.assertEqual(caught.exception.code, "SHARE_UNAVAILABLE")
        self.source._native.denied.clear()
        self.write(content="category,amount\ntravel,999\n")
        with self.assertRaises(DriveError) as caught:
            self.source.verify_origin("budget.csv", origin)
        self.assertEqual(caught.exception.code, "SHARE_SOURCE_CHANGED")

    def test_limits_and_deadline_do_not_publish_evidence(self):
        self.write(content="x" * 1025)
        with self.assertRaises(DriveError) as caught:
            self.source.digest("budget.csv")
        self.assertEqual(caught.exception.code, "FILE_TOO_LARGE")
        self.assertEqual(self.source.evidence("budget.csv"), {})
        with self.assertRaises(DriveError) as caught:
            self.source.digest("budget.csv", deadline=0)
        self.assertEqual(caught.exception.code, "SHARE_TIMEOUT")

    def test_engine_roundtrip_origin_and_current_permission_before_rerank(self):
        self.write()
        file = self.build_engine()
        self.assertEqual(file["origin"]["provider"], "windows_share")
        self.assertTrue(self.engine.status()["capabilities"]["network_source"])
        result = self.engine.read(file["file_id"], expected_version=file["source_version"])
        self.assertEqual(result["origin"]["source_version"], result["source_version"])
        self.engine.search("travel", mode="hybrid")
        self.engine._source._native.denied.add("budget.csv")
        self.engine.retrieval.config["rerank"]["enabled"] = True
        with mock.patch("team_drive.retrieval._post") as model:
            denied = self.engine.call("search", {"query": "travel", "mode": "hybrid", "rerank": True})
        model.assert_not_called()
        self.assertFalse(denied["ok"])
        self.assertNotIn("files", denied)
        self.assertNotIn("120", json.dumps(denied))

    def test_cached_ranked_snippet_rechecks_current_access(self):
        self.write()
        self.build_engine()
        self.engine.search("travel", mode="hybrid")
        self.engine._source._native.denied.add("budget.csv")
        denied = self.engine.call("search", {"query": "travel", "mode": "hybrid"})
        self.assertFalse(denied["ok"])
        self.assertNotIn("files", denied)
        self.assertNotIn("120", json.dumps(denied))

    def test_mapping_change_invalidates_coverage_before_list(self):
        self.write()
        self.build_engine()
        self.mapping = r"\\synthetic-server\replacement"
        denied = self.engine.call("list")
        self.assertEqual(denied["error"]["code"], "SHARE_ROOT_CHANGED")
        self.assertNotIn("files", denied)
        with self.engine._db() as db:
            self.assertFalse(self.engine._coverage(db)["inventory_complete"])

    def test_semantic_index_rechecks_access_before_cached_text(self):
        self.write()
        self.build_engine()
        self.engine.retrieval.config["semantic"]["enabled"] = True
        self.engine._source._native.denied.add("budget.csv")
        with mock.patch.object(self.engine.retrieval, "index_file") as index:
            with self.assertRaises(DriveError):
                self.engine.scan_once()
        index.assert_not_called()

    def test_engine_outage_or_partial_scan_does_not_delete(self):
        self.write()
        file = self.build_engine()
        native = self.engine._source._native
        native.incomplete = True
        with self.assertRaises(DriveError):
            self.engine.scan_once()
        with self.engine._db() as db:
            row = db.execute("SELECT status,source_version FROM files WHERE id=?", (file["file_id"],)).fetchone()
            self.assertEqual(tuple(row), ("ready", file["source_version"]))
            self.assertFalse(self.engine._coverage(db)["inventory_complete"])
        native.offline = True
        self.assertFalse(self.engine.call("search", {"query": "travel"})["ok"])

    def test_engine_changed_same_bytes_requires_new_version_and_refresh(self):
        path = self.write()
        file = self.build_engine()
        replacement = self.write("replacement.csv")
        self.replace_fixture(replacement, path)
        stale = self.engine.call("read", {"file_id": file["file_id"], "expected_version": file["source_version"]})
        self.assertEqual(stale["error"]["code"], "STALE_SOURCE")
        self.engine.scan_once()
        fresh = self.engine.search("travel")["files"][0]
        self.assertNotEqual(file["source_version"], fresh["source_version"])


@unittest.skipUnless(os.name == "nt" and os.environ.get("DF_MCP_TEST_SMB") == "1",
                     "Real localhost SMB test requires Windows and explicit DF_MCP_TEST_SMB=1")
class NativeLocalhostSMBTests(unittest.TestCase):
    def test_current_user_readonly_share_and_local_index_roundtrip(self):
        # Explicit opt-in only: one unique synthetic localhost share. A deny ACE
        # applies only to a temporary parent, never inherited by its readable
        # child. No account, service, firewall or company share is changed.
        from team_drive.onboarding import make_plan, apply_plan
        with tempfile.TemporaryDirectory(prefix="df-simulated-smb-") as temp:
            base = Path(temp).resolve()
            shared = base / "SIMULATED_SHARE"
            restricted = shared / "Restricted Parent"
            source = restricted / "Approved Data"
            nested = source / "nested"
            nested.mkdir(parents=True)
            document = nested / "budget.csv"
            document.write_text("category,amount\nSIMULATED_TRAVEL,120\n", encoding="utf-8")
            name = "df_simulated_" + uuid.uuid4().hex
            backup = base / "synthetic-parent-acl.txt"
            # Actions invokes this test from pwsh 7. Its inherited module path
            # can make Windows PowerShell 5 load incompatible Security modules.
            # Let this child initialize its own defaults; do not modify the
            # parent process or machine/user PowerShell configuration.
            env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
            env.update(DF_MCP_TEST_SHARE_NAME=name, DF_MCP_TEST_SHARE_PATH=str(shared),
                       DF_MCP_TEST_RESTRICTED_PARENT=str(restricted), DF_MCP_TEST_ACL_BACKUP=str(backup))

            def powershell(command):
                return subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                                      env=env, capture_output=True, text=True, timeout=30)

            engine = None
            try:
                setup = powershell("$ErrorActionPreference='Stop'; "
                    "$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent(); "
                    "$acl=Get-Acl -LiteralPath $env:DF_MCP_TEST_RESTRICTED_PARENT; "
                    "$acl.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::All) | "
                    "Set-Content -LiteralPath $env:DF_MCP_TEST_ACL_BACKUP -Encoding UTF8; "
                    "$deny=[System.Security.AccessControl.FileSystemAccessRule]::new($identity.User, "
                    "[System.Security.AccessControl.FileSystemRights]::ListDirectory, "
                    "[System.Security.AccessControl.InheritanceFlags]::None, "
                    "[System.Security.AccessControl.PropagationFlags]::None, "
                    "[System.Security.AccessControl.AccessControlType]::Deny); "
                    "$acl.AddAccessRule($deny); Set-Acl -LiteralPath $env:DF_MCP_TEST_RESTRICTED_PARENT -AclObject $acl; "
                    "New-SmbShare -Name $env:DF_MCP_TEST_SHARE_NAME -Path $env:DF_MCP_TEST_SHARE_PATH "
                    "-ReadAccess $identity.Name -CachingMode None -ErrorAction Stop | Out-Null; "
                    "Write-Output 'SIMULATED_SMB_CREATED'")
                self.assertEqual(setup.returncode, 0,
                    "Native SMB fixture was not created. This explicit test requires Administrator rights and an already running Server service; "
                    "no service/firewall was changed. " + setup.stderr[-1500:])
                self.assertIn("SIMULATED_SMB_CREATED", setup.stdout)
                share_root = Path("\\\\localhost\\" + name)
                unc_parent = share_root / restricted.name
                unc_source = unc_parent / source.name
                # Prove the fixture reproduces the reported permission mismatch;
                # a test that silently grants parent read would miss the defect.
                with self.assertRaises(OSError) as denied:
                    fs_access._handle(unc_parent, directory=True)
                self.assertEqual(denied.exception.errno, 5)
                handle = fs_access._handle(unc_parent, directory=True, attributes_only=True)
                try:
                    self.assertTrue(stat.S_ISDIR(fs_access._share_info(handle).st_mode))
                finally:
                    fs_access._kernel.CloseHandle(handle)
                handle = fs_access._handle(unc_source, directory=True)
                fs_access._kernel.CloseHandle(handle)

                state_base = base / "LOCAL_INDEX"
                plan = make_plan("team", str(unc_source), state_dir=str(state_base))
                self.assertFalse(state_base.exists(), "A successful plan must not create an index")
                with self.assertRaises(DriveError) as unconfirmed:
                    apply_plan(plan)
                self.assertEqual(unconfirmed.exception.code, "CONFIRMATION_REQUIRED")
                applied = apply_plan(plan, confirmed=True)
                config = Path(applied["config_path"])
                state = Path(plan["config"]["state_dir"])
                self.assertIn(state_base, state.parents)
                self.assertFalse(str(state).startswith("\\\\"))
                engine = Engine(str(config), autostart=False)
                self.assertEqual(engine._source._native.observe("nested/budget.csv").st_size, document.stat().st_size)
                self.assertEqual(engine.scan_once()["job"]["status"], "complete")
                file = engine.search("SIMULATED_TRAVEL")["files"][0]
                inspected = engine.inspect(file["file_id"])
                self.assertTrue(inspected["structure"])
                read = engine.read(file["file_id"], expected_version=inspected["source_version"])
                self.assertEqual(read["origin"]["provider"], "windows_share")
                self.assertEqual(read["origin"]["source_version"], read["source_version"])
                self.assertEqual(read["source_version"], inspected["source_version"])
                self.assertTrue(read["segments"])
                self.assertTrue(all(segment.get("locator") for segment in read["segments"]))
                self.assertIn("SIMULATED_TRAVEL", json.dumps(read["segments"]))
                self.assertEqual(engine.state, state)
                self.assertTrue(inspected["coverage"]["inventory_complete"])
                self.assertEqual(document.read_text(encoding="utf-8"), "category,amount\nSIMULATED_TRAVEL,120\n")
                replacement = nested / "replacement.csv"
                replacement.write_text("category,amount\nSIMULATED_TRAVEL,999\n", encoding="utf-8")
                # The broker is still alive. An ordinary external atomic save
                # must work between operations without special native helpers.
                os.replace(replacement, document)
                stale = engine.call("read", {"file_id": file["file_id"], "expected_version": file["source_version"]})
                self.assertEqual(stale["error"]["code"], "STALE_SOURCE")
                engine.scan_once()
                current = engine.search("SIMULATED_TRAVEL")["files"][0]
                self.assertNotEqual(current["source_version"], file["source_version"])
            finally:
                try:
                    if engine is not None:
                        engine.close()
                finally:
                    # Restore only this fixture's saved ACL, even if setup or
                    # assertion failed. Always remove the matching unique share.
                    removed = powershell("$ErrorActionPreference='Stop'; $restoreFailure=$null; "
                        "try { if (Test-Path -LiteralPath $env:DF_MCP_TEST_ACL_BACKUP) { "
                        "$acl=Get-Acl -LiteralPath $env:DF_MCP_TEST_RESTRICTED_PARENT; "
                        "$acl.SetSecurityDescriptorSddlForm((Get-Content -LiteralPath $env:DF_MCP_TEST_ACL_BACKUP -Raw).Trim()); "
                        "Set-Acl -LiteralPath $env:DF_MCP_TEST_RESTRICTED_PARENT -AclObject $acl "
                        "} } catch { $restoreFailure=$_ }; "
                        "$share=Get-SmbShare -Name $env:DF_MCP_TEST_SHARE_NAME -ErrorAction SilentlyContinue; "
                        "if ($null -ne $share) { "
                        "if ($share.Path -ne $env:DF_MCP_TEST_SHARE_PATH) { throw 'Unexpected synthetic share target' }; "
                        "Remove-SmbShare -Name $env:DF_MCP_TEST_SHARE_NAME -Force -Confirm:$false -ErrorAction Stop }; "
                        "if ($null -ne $restoreFailure) { throw $restoreFailure }")
                    self.assertEqual(removed.returncode, 0, "Synthetic share/ACL cleanup failed: " + removed.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
