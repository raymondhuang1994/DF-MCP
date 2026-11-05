"""Read-only source boundary and guarded private state on POSIX and Windows.

Windows handles use OPEN_EXISTING and GENERIC_READ for source files. Only strict
ancestors outside an approved network source use FILE_READ_ATTRIBUTES. Readable
directory handles retain their no-delete sharing; attribute-only ancestors also
undergo identity, actual-target and reparse checks when opened and reopened.
Reparse points,
remote/device paths, and multiply linked source files are deliberately excluded.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import ntpath
import os
import re
import stat
import sys
import threading
from pathlib import Path, PurePosixPath
from .errors import DriveError

_DB_OPEN_LOCK = threading.Lock()
DATABASE_NAMES = ("index.sqlite", "index.working.sqlite", "index.next.sqlite", "index.corrupt.sqlite")
DATABASE_FILES = tuple(name + suffix for name in DATABASE_NAMES for suffix in ("", "-wal", "-shm", "-journal"))


def _live_fds():
    result = set()
    for value in os.listdir("/dev/fd"):
        if value.isdigit():
            try:
                info = os.fstat(int(value))
                # A concurrent reader may close its descriptor and SQLite may
                # reuse that number. Include identity, not just the fd number.
                result.add((value, info.st_dev, info.st_ino))
            except OSError:
                pass
    return result


def _state_open(*args, **kwargs):
    # SQLite descriptor verification examines process-wide new writable fds.
    # Serialize only their creation, never the potentially long file I/O, so a
    # concurrent export cannot be mistaken for a redirected database handle.
    with _DB_OPEN_LOCK:
        return os.open(*args, **kwargs)


def _state_dup(fd):
    with _DB_OPEN_LOCK:
        return os.dup(fd)


def online_only(info):
    return bool(getattr(info, "st_flags", 0) & getattr(stat, "UF_DATALESS", 0x40000000)) or bool(getattr(info, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000))


def relative_parts(relative):
    if not isinstance(relative, str) or not relative or len(relative) > 4096 or "\\" in relative or "\0" in relative:
        raise DriveError("INVALID_PATH", "Use a relative file path within the approved root.")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise DriveError("INVALID_PATH", "Absolute paths and parent traversal are forbidden.")
    if os.name == "nt":
        for part in path.parts:
            _windows_component(part)
    return path.parts


def _windows_component(part):
    if not part or any(c in part for c in '<>:"|?*') or part[-1:] in (".", " ") or any(ord(c) < 32 for c in part) or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I):
        raise DriveError("INVALID_PATH", "Device names, alternate streams, and ambiguous Windows paths are forbidden.")


def canonical_windows_path(path):
    """Pure Windows spelling normalization, including UNC share-root anchors.

    Win32 can return a share root without its trailing slash; pathlib adds it.
    ntpath.normpath alone preserves that difference. Subdirectory and different
    share names remain distinct, and this never resolves or follows a path.
    """
    value = str(path)
    if value.upper().startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    value = ntpath.normcase(ntpath.normpath(value))
    drive, tail = ntpath.splitdrive(value)
    if drive.startswith("\\\\") and tail in ("", "\\"):
        return drive + "\\"
    return value


def _identity(path):
    try:
        info = path.stat()
        return info.st_dev, info.st_ino
    except FileNotFoundError:
        return None


def contains_physical(parent, child):
    """Compare actual ancestors, including case/Unicode aliases on APFS/NTFS."""
    expected = _identity(parent)
    if expected is None:
        return parent == child or parent in child.parents
    return any(_identity(candidate) == expected for candidate in (child,) + tuple(child.parents))


def overlaps(a, b):
    return contains_physical(a, b) or contains_physical(b, a)


def validate_state_location(path):
    """Reject Codex-owned storage before making or pinning any state directory.

    Check both spelling and physical ancestry: resolve expands existing symlink,
    junction and Windows short-name aliases even when the final child is absent.
    Directory guards still perform their independent no-redirection checks.
    """
    raw = Path(path).expanduser().absolute()
    try:
        resolved = raw.resolve()
        candidates = (raw, resolved)
        if any((part.rstrip(" .") if os.name == "nt" else part).casefold() == ".codex"
               for item in candidates for part in item.parts):
            raise DriveError("UNSAFE_STATE", "Index state must be outside .codex and CODEX_HOME.", False,
                             "Choose an external application-data state directory; existing state is not moved automatically.")
        configured_home = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        if configured_home:
            home = Path(configured_home).expanduser().absolute()
            homes = (home, home.resolve())
            for parent in homes:
                for child in candidates:
                    # normcase covers Windows spellings of a not-yet-created home;
                    # inode ancestry also covers existing case and short-name aliases.
                    parent_parts = tuple(os.path.normcase(part) for part in parent.parts)
                    child_parts = tuple(os.path.normcase(part) for part in child.parts)
                    if child_parts[:len(parent_parts)] == parent_parts or contains_physical(parent, child):
                        raise DriveError("UNSAFE_STATE", "Index state must be outside .codex and CODEX_HOME.", False,
                                         "Choose an external application-data state directory; existing state is not moved automatically.")
    except (OSError, RuntimeError):
        raise DriveError("UNSAFE_STATE", "The state location cannot be validated safely.")


def validate_state_boundaries(path, source, installation):
    """Validate a state base without creating it or inspecting legacy files."""
    raw = Path(path).expanduser()
    validate_state_location(raw)
    if raw.is_symlink():
        raise DriveError("UNSAFE_STATE", "The state directory cannot be a symbolic link.")
    path = _valid_windows_path(raw) if os.name == "nt" else raw.resolve()
    if overlaps(path, source.root):
        raise DriveError("UNSAFE_STATE", "Source and state directories must not contain each other.")
    install = Path(installation).resolve()
    if contains_physical(source.root, install):
        raise DriveError("UNSAFE_ROOT", "The source directory cannot contain the installed application.")
    if contains_physical(path, install):
        raise DriveError("UNSAFE_STATE", "The state directory cannot contain the installed application.")
    return path


def _make_posix_state(path, source_identity):
    """Create each state component relative to its already opened parent."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino) == source_identity:
                raise DriveError("UNSAFE_STATE", "State creation cannot enter the approved source directory.")
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    # A concurrent pool claimant may have created this component.
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) == source_identity:
            raise DriveError("UNSAFE_STATE", "State cannot be the approved source directory.")
        return fd
    except Exception as exc:
        os.close(fd)
        if isinstance(exc, DriveError):
            raise
        raise DriveError("UNSAFE_STATE", "The state directory cannot be created without following a redirected path.")


class PosixSource:
    def __init__(self, root):
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fwalk"):
            raise DriveError("UNSUPPORTED_PLATFORM", "Safe directory-relative file access is unavailable.")
        self.fd = None
        raw = Path(root).expanduser()
        try:
            if raw.is_symlink():
                raise DriveError("UNSAFE_ROOT", "The source root cannot be a symbolic link.")
            self.root = raw.resolve(strict=True)
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise DriveError("INVALID_ROOT", "The source root must be an existing directory.")
            self.identity = (info.st_dev, info.st_ino)
            self.fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            pinned = os.fstat(self.fd)
            if (pinned.st_dev, pinned.st_ino) != self.identity:
                raise DriveError("SOURCE_ROOT_CHANGED", "The source directory changed during initialization.")
        except Exception as exc:
            self.close()
            if isinstance(exc, DriveError):
                raise
            raise DriveError("INVALID_ROOT", "The source root is missing or inaccessible.")

    def check_root(self):
        try:
            info = os.stat(self.root, follow_symlinks=False)
            valid = self.fd is not None and stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == self.identity
        except OSError:
            valid = False
        if not valid:
            raise DriveError("SOURCE_ROOT_CHANGED", "The approved source directory is missing or has been rebound.", False, "Restore the approved directory or configure a new source root and state directory.")

    def open_read(self, relative):
        self.check_root()
        parts = relative_parts(relative)
        parent_fd = os.dup(self.fd)
        source_fd = None
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise DriveError("UNSAFE_FILE", "Only ordinary files with one hard link are supported.")
            if online_only(before):
                raise DriveError("ONLINE_ONLY", "Online-only files are not hydrated automatically.")
            source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DriveError("UNSAFE_FILE", "Only ordinary files with one hard link are supported.")
            if online_only(info):
                raise DriveError("ONLINE_ONLY", "Online-only files are not hydrated automatically.")
            return source_fd, info
        except Exception as exc:
            if source_fd is not None:
                os.close(source_fd)
            if isinstance(exc, DriveError):
                raise
            raise DriveError("SOURCE_UNAVAILABLE", "The source is missing, inaccessible, or traverses a symbolic link.", True, "Refresh and check the approved folder.")
        finally:
            os.close(parent_fd)

    def walk(self, errors):
        self.check_root()
        for folder, dirs, files, folder_fd in os.fwalk(".", dir_fd=self.fd, follow_symlinks=False, onerror=lambda _: errors.append(True)):
            self.check_root()
            blocked_dirs = []
            for name in dirs:
                try:
                    info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or online_only(info):
                        blocked_dirs.append(name)
                except OSError:
                    errors.append(True)
                    blocked_dirs.append(name)
            dirs[:] = [name for name in dirs if name not in blocked_dirs]
            for name in files + blocked_dirs:
                try:
                    info = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
                except OSError:
                    errors.append(True)
                    continue
                relative = (PurePosixPath(folder) / name).as_posix()
                online = online_only(info)
                blocked = not online and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
                yield relative, info, blocked, online

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
    from types import SimpleNamespace

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _INVALID = ctypes.c_void_p(-1).value
    _READ = 0x80000000
    _READ_ATTRIBUTES = 0x80
    _WRITE = 0x40000000
    _DELETE = 0x00010000
    _SHARE_READ_WRITE = 0x1 | 0x2  # Never FILE_SHARE_DELETE for pinned objects.
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _OPEN_REPARSE = 0x00200000
    _BACKUP = 0x02000000
    _REPARSE = 0x400
    _DIRECTORY = 0x10

    class _FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel.GetLongPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    _kernel.GetLongPathNameW.restype = wintypes.DWORD
    _kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel.GetDriveTypeW.restype = wintypes.UINT
    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    class _RemoteProtocolInfo(ctypes.Structure):
        # FILE_REMOTE_PROTOCOL_INFO, Win8+ layout: the protocol-specific
        # union's reserved member contains 16 ULONGs, with DWORD alignment.
        _fields_ = [("version", wintypes.WORD), ("size", wintypes.WORD),
                    ("protocol", wintypes.DWORD), ("major", wintypes.WORD),
                    ("minor", wintypes.WORD), ("revision", wintypes.WORD),
                    ("reserved", wintypes.WORD), ("flags", wintypes.DWORD),
                    ("generic", wintypes.DWORD * 8), ("specific", wintypes.DWORD * 16)]

    _kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _kernel.ReadFile.restype = wintypes.BOOL
    _kernel.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel.GetFileType.restype = wintypes.DWORD
    _kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _kernel.MoveFileExW.restype = wintypes.BOOL

    class _IoStatusBlock(ctypes.Structure):
        # The first native field is a union of NTSTATUS and pointer. Keeping its
        # pointer width supplies the required 32/64-bit alignment.
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    class _RenameInformation(ctypes.Structure):
        # The native union holds BOOLEAN ReplaceIfExists for class 10 and
        # ULONG Flags for class 65. The DWORD form preserves both ABI layouts.
        _fields_ = [("flags", wintypes.DWORD), ("root", wintypes.HANDLE),
                    ("length", wintypes.DWORD), ("name", wintypes.WCHAR * 1)]

    _ntdll.NtSetInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock),
                                          ctypes.c_void_p, wintypes.ULONG, ctypes.c_int]
    _ntdll.NtSetInformationFile.restype = wintypes.LONG
    _ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    def _extended(path):
        value = str(path)
        return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value

    def _canonical(path):
        return canonical_windows_path(path)

    def _file_info(handle):
        result = _FileInfo()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(result)):
            raise OSError("Cannot inspect an opened file.")
        attribute_tag = _AttributeTagInfo()
        if result.attributes & _REPARSE and not _kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attribute_tag), ctypes.sizeof(attribute_tag)):
            raise OSError("Cannot inspect a reparse point.")
        def ns(value):
            return (((value.dwHighDateTime << 32) | value.dwLowDateTime) - 116444736000000000) * 100
        return SimpleNamespace(st_mode=stat.S_IFDIR if result.attributes & _DIRECTORY else stat.S_IFREG,
                               st_dev=result.volume, st_ino=(result.index_high << 32) | result.index_low,
                               st_size=(result.size_high << 32) | result.size_low,
                               st_mtime_ns=ns(result.write), st_ctime_ns=ns(result.creation),
                               st_nlink=result.links, st_file_attributes=result.attributes, st_reparse_tag=attribute_tag.tag)

    def _final_path(handle):
        required = _kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not required:
            raise OSError("Cannot resolve an opened file.")
        buffer = ctypes.create_unicode_buffer(required + 1)
        copied = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not copied or copied >= len(buffer):
            raise OSError("Cannot resolve an opened file.")
        return _canonical(buffer.value)

    def _long_path(path):
        """Expand 8.3 names only after this component and all parents are pinned.

        This is not a substitute for reparse checks: callers first open every
        directory with OPEN_REPARSE_POINT and reject its reparse attributes.
        """
        required = _kernel.GetLongPathNameW(_extended(path), None, 0)
        if not required:
            raise OSError("Cannot expand the approved directory name.")
        buffer = ctypes.create_unicode_buffer(required + 1)
        copied = _kernel.GetLongPathNameW(_extended(path), buffer, len(buffer))
        if not copied or copied >= len(buffer):
            raise OSError("Cannot expand the approved directory name.")
        value = buffer.value
        if value.upper().startswith("\\\\?\\UNC\\"):
            return "\\\\" + value[8:]
        return value[4:] if value.startswith("\\\\?\\") else value

    def _share_info(handle):
        """Require a live SMB handle, usable identity and native change time.

        Unknown remote providers and Windows Offline Files are not evidence.
        Use the documented Win8+ caller version and exact structure size.
        """
        protocol = _RemoteProtocolInfo()
        protocol.version = 2
        protocol.size = ctypes.sizeof(protocol)
        if not _kernel.GetFileInformationByHandleEx(handle, 13, ctypes.byref(protocol), ctypes.sizeof(protocol)):
            raise DriveError("SHARE_PROTOCOL_UNVERIFIED", "The server did not provide verifiable SMB handle information.")
        if (not 1 <= protocol.version <= 4 or not 20 <= protocol.size <= ctypes.sizeof(protocol) or
                protocol.protocol != 0x00020000 or protocol.flags & 0x2):
            raise DriveError("SHARE_PROTOCOL_UNVERIFIED", "Only online SMB files are supported; offline caches and other remote providers are excluded.")
        info = _file_info(handle)
        class FileIdInfo(ctypes.Structure):
            _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]
        identity = FileIdInfo()
        if not _kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
            raise DriveError("SHARE_IDENTITY_UNVERIFIED", "The server did not provide a stable full file identity.")
        info.st_dev = identity.volume
        info.st_ino = int.from_bytes(bytes(identity.identifier), "little")
        if not info.st_ino or not info.st_dev or online_only(info):
            raise DriveError("SHARE_IDENTITY_UNVERIFIED", "The remote object identity is unavailable or the object is offline.")
        class BasicInfo(ctypes.Structure):
            _fields_ = [("creation", ctypes.c_longlong), ("access", ctypes.c_longlong),
                        ("write", ctypes.c_longlong), ("change", ctypes.c_longlong),
                        ("attributes", wintypes.DWORD)]
        basic = BasicInfo()
        if not _kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise DriveError("SHARE_IDENTITY_UNVERIFIED", "The remote change timestamp cannot be verified.")
        info.st_birthtime_ns = info.st_ctime_ns
        info.st_ctime_ns = (basic.change - 116444736000000000) * 100
        info.st_network_change_ns = info.st_ctime_ns
        return info

    def _handle(path, directory=False, write=False, create=False, exclusive=False, metadata_read=False, rename=False,
                attributes_only=False):
        # This mode is selected only for strict network-source ancestors by
        # _WindowsDirectories. It is not the private JSON sharing opt-in below.
        if attributes_only and (not directory or write or create or exclusive or metadata_read or rename):
            raise DriveError("INVALID_ACCESS_MODE", "Attribute-only access is limited to existing guarded directories.")
        access = _READ_ATTRIBUTES if attributes_only else _READ | (_WRITE if write else 0) | (_DELETE if rename else 0)
        # Preserve existing directory sharing flags. Attribute-only handles do
        # not substitute for the guard's identity/target/reparse verification.
        sharing = 0 if exclusive else 0x1 if directory else _SHARE_READ_WRITE
        if metadata_read and not directory and not write and not create and not exclusive:
            # Atomic endpoint JSON publication may replace a file being read.
            # This opt-in never applies to source, directory or SQLite handles.
            sharing |= 0x4
        value = _kernel.CreateFileW(_extended(path), access, sharing,
                                    None, _OPEN_ALWAYS if create else _OPEN_EXISTING,
                                    _OPEN_REPARSE | (_BACKUP if directory else 0), None)
        if value == _INVALID:
            raise OSError(ctypes.get_last_error(), "Cannot open an approved local object.")
        return value

    def _rename_same_directory(handle, name):
        """Atomically replace a sibling without reopening its pinned parent.

        A full-path MoveFileEx/Win32 rename opens the target parent with write
        access, conflicting with the guard that prevents in-place reparse
        changes. Native FileRenameInformation explicitly permits a basename
        with a null RootDirectory for a rename within the source directory.
        The source DELETE handle and every directory guard stay pinned; no
        source-read sharing rules are relaxed. See Microsoft's documented
        FILE_RENAME_INFORMATION same-directory form.
        """
        encoded = name.encode("utf-16-le")
        size = _RenameInformation.name.offset + len(encoded)
        buffer = ctypes.create_string_buffer(max(size, ctypes.sizeof(_RenameInformation)))
        info = _RenameInformation.from_buffer(buffer)
        # POSIX replacement keeps existing metadata readers on the old complete
        # file while new readers open the new complete file. This opt-in applies
        # only to guarded private state publication, never source handles.
        info.flags = 0x1 | 0x2  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
        info.root = None
        info.length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + _RenameInformation.name.offset, encoded, len(encoded))
        result = _IoStatusBlock()
        status = _ntdll.NtSetInformationFile(handle, ctypes.byref(result), buffer, size, 65)
        if status < 0 and _ntdll.RtlNtStatusToDosError(status) in (1, 50, 87, 124):
            # Older local filesystems may lack the extended information class.
            # Keep the safe classic behavior there; do not retry access-denied
            # or sharing failures, and never loosen directory/source guards.
            info.flags = 1
            status = _ntdll.NtSetInformationFile(handle, ctypes.byref(result), buffer, size, 10)
        if status < 0:
            raise ctypes.WinError(_ntdll.RtlNtStatusToDosError(status))

    def _cloud(info):
        # Cloud tags have provider-specific bits in positions 12 through 15.
        return online_only(info) or bool(info.st_file_attributes & _REPARSE and getattr(info, "st_reparse_tag", 0) & 0xFFFF0FFF == 0x9000001A)

    def _valid_windows_path(raw, allow_network=False):
        if allow_network:
            from .network_source import resolve_share_path
            return Path(resolve_share_path(str(raw)))
        value = os.path.abspath(os.path.expanduser(str(raw)))
        if not re.match(r"^[A-Za-z]:\\", value) or str(raw).startswith(("\\\\", "\\?", "\\.")):
            raise DriveError("UNSUPPORTED_ROOT_LOCATION", "Use an ordinary folder on a fixed local Windows drive; network and device paths are unsupported.")
        path = Path(value)
        if _kernel.GetDriveTypeW(path.anchor) != 3:
            raise DriveError("UNSUPPORTED_ROOT_LOCATION", "Only fixed local Windows drives are supported.")
        for part in path.parts[1:]:
            _windows_component(part)
        return path

    class _WindowsDirectories:
        def __init__(self, path, create=False, code="UNSAFE_ROOT", state=False, allow_network=False, boundary=None,
                     source_root=None):
            if allow_network and (create or state):
                raise DriveError("UNSAFE_STATE", "Network source access never permits state creation.")
            if source_root is not None and not allow_network:
                raise DriveError(code, "An attribute-only ancestor boundary is valid only for a network source.")
            self.network = allow_network
            self.path = _valid_windows_path(path, allow_network=allow_network)
            self.source_root = _valid_windows_path(source_root, allow_network=True) if source_root is not None else None
            if self.source_root is not None:
                # Windows Path comparisons use components and case-insensitive
                # spelling; a similarly named sibling is not an ancestor.
                if self.path != self.source_root and self.source_root not in self.path.parents:
                    raise DriveError(code, "The guarded directory is outside the approved network source.")
                if boundary is not None and _canonical(boundary[0]) != _canonical(self.source_root):
                    raise DriveError(code, "The ancestor-access boundary must match the approved source boundary.")
            if state:
                validate_state_location(self.path)
            self.handles = []
            self.identities = []
            self.targets = []
            self.attribute_modes = []
            self.code = code
            try:
                current = Path(self.path.anchor)
                for depth, part in enumerate((None,) + self.path.parts[1:], start=1):
                    if part is not None:
                        current = current / part
                        if create:
                            try:
                                current.mkdir(mode=0o700)
                            except FileExistsError:
                                pass
                    # The component-prefix check above proves these positions
                    # are strict ancestors. Keep the classification stable if
                    # GetLongPathName expands an ordinary 8.3 component below.
                    attributes_only = self.source_root is not None and depth < len(self.source_root.parts)
                    handle = _handle(current, directory=True, attributes_only=attributes_only)
                    self.handles.append(handle)
                    self.attribute_modes.append(attributes_only)
                    info = _share_info(handle) if self.network else _file_info(handle)
                    if not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & _REPARSE:
                        raise DriveError(code, "Directory links, junctions, and reparse points are forbidden.")
                    # TEMP commonly contains a normal 8.3 account-directory
                    # alias. Expand it only once its no-reparse identity is pinned.
                    current = Path(_long_path(current))
                    target = _final_path(handle)
                    if not self.network and target != _canonical(current):
                        raise DriveError(code, "The opened directory resolves to an unexpected location.")
                    if self.network and boundary is not None:
                        base, expected_target, expected_identity = boundary
                        if current == base or base in current.parents:
                            expected = _canonical(Path(expected_target) / current.relative_to(base))
                            if (target != expected or info.st_dev != expected_identity[0] or
                                    (current == base and (info.st_dev, info.st_ino) != expected_identity)):
                                raise DriveError("SHARE_ROOT_CHANGED", "The shared folder target changed or a nested referral escaped its approved target.")
                    if state:
                        # Recheck canonical components while pinned, before any
                        # child is created, to close alias/rebinding races.
                        validate_state_location(current)
                    self.identities.append((current, info.st_dev, info.st_ino))
                    self.targets.append(target)
                self.path = current
            except Exception as exc:
                self.close()
                if isinstance(exc, DriveError):
                    raise
                raise DriveError(code, "The directory is missing, inaccessible, or cannot be pinned safely.")

        def check(self):
            if not self.handles:
                raise DriveError(self.code, "The guarded directory is no longer open.")
            for handle, (path, device, inode), target, attributes_only in zip(
                    self.handles, self.identities, self.targets, self.attribute_modes):
                info = _share_info(handle) if self.network else _file_info(handle)
                if (info.st_dev, info.st_ino) != (device, inode) or info.st_file_attributes & _REPARSE or _final_path(handle) != target:
                    raise DriveError(self.code, "The guarded directory identity has changed.")
                if self.network:
                    # An old open handle can outlive an ACL change or DFS referral.
                    # Freshly reopen every component under the current identity.
                    fresh = _handle(path, directory=True, attributes_only=attributes_only)
                    try:
                        current = _share_info(fresh)
                        if (current.st_dev, current.st_ino) != (device, inode) or current.st_file_attributes & _REPARSE or _final_path(fresh) != target:
                            raise DriveError("SHARE_ROOT_CHANGED", "The shared folder identity or DFS target has changed.")
                    finally:
                        _kernel.CloseHandle(fresh)

        def close(self):
            for handle in reversed(self.handles):
                _kernel.CloseHandle(handle)
            self.handles = []

    class WindowsSource:
        def __init__(self, root, allow_network=False):
            self.network = allow_network
            self._closed = False
            self.guard = _WindowsDirectories(root, allow_network=allow_network,
                                             source_root=root if allow_network else None)
            self.root = self.guard.path
            self.identity = self.guard.identities[-1][1:]
            self.target = self.guard.targets[-1]
            self._root_chain = tuple(zip(self.guard.identities, self.guard.targets))
            if self.network:
                # Persist identities, not directory locks. A shared-folder
                # broker must not block other users' atomic saves while idle.
                self.guard.close()
                self.guard = None

        def _parent_guard(self, path):
            if self._closed:
                raise DriveError("SOURCE_UNAVAILABLE", "The source access session is closed.")
            guard = _WindowsDirectories(path, code="SOURCE_UNAVAILABLE", allow_network=self.network,
                                        boundary=(self.root, self.target, self.identity) if self.network else None,
                                        source_root=self.root if self.network else None)
            if self.network:
                observed = tuple(zip(guard.identities, guard.targets))
                if observed[:len(self._root_chain)] != self._root_chain:
                    guard.close()
                    raise DriveError("SHARE_ROOT_CHANGED", "A shared-folder ancestor or approved root changed identity or target.")
            return guard

        def _expected_target(self, path):
            return _canonical(Path(self.target) / path.relative_to(self.root)) if self.network else _canonical(path)

        def check_root(self):
            if self._closed:
                raise DriveError("SOURCE_UNAVAILABLE", "The source access session is closed.")
            try:
                if self.network:
                    current = self._parent_guard(self.root)
                    current.close()
                else:
                    self.guard.check()
            except (DriveError, OSError):
                if self.network:
                    raise DriveError("SHARE_ROOT_UNAVAILABLE", "The shared folder is inaccessible, permissions changed, or its DFS target changed.", True,
                                     "Restore the same approved target and Windows access; a new target requires a separate index.")
                raise DriveError("SOURCE_ROOT_CHANGED", "The approved source directory identity changed.")

        def open_read(self, relative):
            self.check_root()
            parts = relative_parts(relative)
            path = self.root.joinpath(*parts)
            guard = self._parent_guard(path.parent)
            handle = None
            fd = None
            try:
                handle = _handle(path)
                info = _share_info(handle) if self.network else _file_info(handle)
                if _final_path(handle) != self._expected_target(path) or _kernel.GetFileType(handle) != 1:
                    raise DriveError("UNSAFE_FILE", "The source is redirected or is not a local disk file.")
                if info.st_file_attributes & _REPARSE:
                    if _cloud(info):
                        raise DriveError("ONLINE_ONLY", "Cloud reparse files are not read; copy approved content into an ordinary local folder.")
                    raise DriveError("UNSAFE_FILE", "Source links and reparse points are not read.")
                if online_only(info):
                    raise DriveError("ONLINE_ONLY", "Online-only files are not hydrated automatically.")
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DriveError("UNSAFE_FILE", "Only ordinary files with one hard link are supported.")
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                handle = None  # The C runtime now owns the handle.
                result = (fd, self.stat_fd(fd))
                fd = None
                return result
            except OSError:
                raise DriveError("SOURCE_UNAVAILABLE", "The source is missing, occupied, or inaccessible.", True, "Close applications with exclusive file locks and refresh.")
            finally:
                if handle is not None:
                    _kernel.CloseHandle(handle)
                if fd is not None:
                    os.close(fd)
                guard.close()

        def stat_fd(self, fd):
            return _share_info(msvcrt.get_osfhandle(fd)) if self.network else os.fstat(fd)

        def observe(self, relative, parent_identity=None, progress=None):
            """Fresh no-follow metadata, including excluded files; no content read."""
            if not self.network:
                self.check_root()
            path = self.root.joinpath(*relative_parts(relative))
            # A fresh network parent guard already checks the complete approved
            # root/ancestor chain. Opening that chain separately immediately
            # before it adds SMB requests but no additional scope check. Never
            # reuse this guard across items, recursive visits or yielded rows.
            with progress.operation("directory_validation") if progress is not None else contextlib.nullcontext():
                guard = self._parent_guard(path.parent)
            handle = None
            try:
                if parent_identity is not None and guard.identities[-1][1:] != parent_identity:
                    raise DriveError("SHARE_DIRECTORY_CHANGED", "A shared parent directory changed during enumeration.")
                with progress.operation("native_metadata") if progress is not None else contextlib.nullcontext():
                    handle = _handle(path, directory=True)
                    info = _share_info(handle) if self.network else _file_info(handle)
                    target = _final_path(handle)
                    if target != self._expected_target(path) or _kernel.GetFileType(handle) != 1:
                        raise DriveError("SHARE_ROOT_CHANGED", "The shared item resolves outside its approved target.")
                return info
            finally:
                if handle is not None:
                    _kernel.CloseHandle(handle)
                guard.close()

        def walk(self, errors, max_depth=None, max_items=None, deadline=None, progress=None):
            if self.network:
                yield from self._walk_network(errors, max_depth=max_depth, max_items=max_items,
                                              deadline=deadline, progress=progress)
                return
            self.check_root()
            def visit(folder, prefix):
                try:
                    guard = self._parent_guard(folder)
                except DriveError:
                    errors.append(True)
                    return
                try:
                    self.check_root()
                    with os.scandir(folder) as entries:
                        names = [entry.name for entry in entries]
                    for name in names:
                        try:
                            _windows_component(name)
                            path = folder / name
                            relative = (PurePosixPath(prefix) / name).as_posix()
                            info = os.stat(path, follow_symlinks=False)
                            attributes = getattr(info, "st_file_attributes", 0)
                            reparse = bool(attributes & _REPARSE)
                            online = online_only(info) or (reparse and getattr(info, "st_reparse_tag", 0) & 0xFFFF0FFF == 0x9000001A)
                            if stat.S_ISDIR(info.st_mode) and not reparse and not online:
                                yield from visit(path, relative)
                            else:
                                blocked = not online and (reparse or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
                                yield relative, info, blocked, online
                        except (OSError, DriveError):
                            errors.append(True)
                except OSError:
                    errors.append(True)
                finally:
                    guard.close()
            yield from visit(self.root, "")

        def directory_fingerprint(self, relative="", deadline=None, progress=None):
            """Freshly authorize a directory and bind its membership epoch.

            This does not claim that file contents are unchanged. Evidence
            reads continue to check each source's current content and access.
            """
            if not self.network:
                raise DriveError("UNSUPPORTED_PROVIDER", "Directory checkpoints require a network source.")
            import time
            if deadline is not None and time.monotonic() >= deadline:
                raise DriveError("SHARE_TIMEOUT", "The directory check exceeded its scan budget.")
            folder = self.root.joinpath(*relative_parts(relative)) if relative else self.root
            with progress.operation("directory_validation") if progress is not None else contextlib.nullcontext():
                guard = self._parent_guard(folder)
            try:
                with progress.operation("native_metadata") if progress is not None else contextlib.nullcontext():
                    info = _share_info(guard.handles[-1])
                return [str(info.st_dev), str(info.st_ino), info.st_mtime_ns, info.st_ctime_ns]
            finally:
                guard.close()

        def scan_directory(self, relative="", expected_identity=None, max_depth=None,
                           max_items=None, deadline=None, progress=None):
            """Return one complete, verified directory; never a partial listing.

            No handle survives this call. Child directories are queued by the
            caller and must pass the same fresh ancestor/DFS/access checks.
            """
            import time
            def budget():
                if deadline is not None and time.monotonic() >= deadline:
                    raise DriveError("SHARE_TIMEOUT", "The directory batch exceeded its scan budget.")
            def operation(name):
                return progress.operation(name) if progress is not None else contextlib.nullcontext()
            if not self.network:
                raise DriveError("UNSUPPORTED_PROVIDER", "Directory checkpoints require a network source.")
            budget()
            if max_depth is not None and len(PurePosixPath(relative).parts) > max_depth:
                raise DriveError("SHARE_SCAN_LIMIT", "The shared directory exceeds its configured depth limit.")
            folder = self.root.joinpath(*relative_parts(relative)) if relative else self.root
            if progress is not None:
                progress.set_stage("enumerating")
            with operation("directory_validation"):
                guard = self._parent_guard(folder)
            try:
                with operation("native_metadata"):
                    before = _share_info(guard.handles[-1])
                identity = (before.st_dev, before.st_ino)
                if expected_identity is not None and [str(v) for v in identity] != list(expected_identity):
                    raise DriveError("SHARE_DIRECTORY_CHANGED", "A queued shared directory changed identity.")
                names = []
                with operation("directory_enumeration"):
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            budget()
                            if max_items is not None and len(names) >= max_items:
                                raise DriveError("SHARE_SCAN_LIMIT", "One directory exceeds the remaining batch item budget.")
                            names.append(entry.name)
                            if progress is not None:
                                progress.increment("entries_enumerated")
                if progress is not None:
                    progress.increment("directories_enumerated")
            finally:
                guard.close()
            files, directories = [], []
            for name in sorted(names):
                budget()
                _windows_component(name)
                child = (PurePosixPath(relative) / name).as_posix()
                if progress is not None:
                    progress.set_stage("verifying")
                with operation("item_observation"):
                    info = self.observe(child, parent_identity=identity, progress=progress)
                if progress is not None:
                    progress.increment("items_verified")
                reparse = bool(info.st_file_attributes & _REPARSE)
                online = online_only(info) or (reparse and getattr(info, "st_reparse_tag", 0) & 0xFFFF0FFF == 0x9000001A)
                if stat.S_ISDIR(info.st_mode) and not reparse and not online:
                    directories.append({"path": child, "identity": [str(info.st_dev), str(info.st_ino)]})
                else:
                    blocked = not online and (reparse or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
                    files.append((child, info, blocked, online))
            fingerprint = [str(before.st_dev), str(before.st_ino), before.st_mtime_ns, before.st_ctime_ns]
            if self.directory_fingerprint(relative, deadline=deadline, progress=progress) != fingerprint:
                raise DriveError("SHARE_DIRECTORY_CHANGED", "The directory changed before its batch was verified.")
            budget()
            return {"files": files, "directories": directories, "fingerprint": fingerprint, "entries": len(names)}

        def _walk_network(self, errors, max_depth=None, max_items=None, deadline=None, progress=None):
            """Pin only enumeration/observation windows; verify each directory.

            No handle spans a yielded manifest item or a recursive visit. A
            replacement parent or a changed directory listing aborts the scan,
            which WindowsShareSource discards before publishing any inventory.
            """
            import time
            count = 0

            def budget():
                if deadline is not None and time.monotonic() >= deadline:
                    raise DriveError("SHARE_TIMEOUT", "The shared-folder scan exceeded its budget between Windows I/O calls.")

            def signature(info):
                return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)

            def operation(name):
                return progress.operation(name) if progress is not None else contextlib.nullcontext()

            def visit(folder, prefix, expected_identity=None):
                nonlocal count
                budget()
                if max_depth is not None and len(PurePosixPath(prefix).parts) > max_depth:
                    raise DriveError("SHARE_SCAN_LIMIT", "The shared-folder scan exceeded its directory depth limit.")
                if progress is not None:
                    progress.set_stage("enumerating")
                with operation("directory_validation"):
                    guard = self._parent_guard(folder)
                try:
                    with operation("native_metadata"):
                        before = _share_info(guard.handles[-1])
                    identity = (before.st_dev, before.st_ino)
                    if expected_identity is not None and identity != expected_identity:
                        raise DriveError("SHARE_DIRECTORY_CHANGED", "A shared directory was replaced before enumeration.")
                    names = []
                    with operation("directory_enumeration"):
                        with os.scandir(folder) as entries:
                            for entry in entries:
                                budget()
                                count += 1
                                if progress is not None:
                                    progress.increment("entries_enumerated")
                                if max_items is not None and count > max_items:
                                    raise DriveError("SHARE_SCAN_LIMIT", "The shared-folder scan exceeded its item limit.")
                                names.append(entry.name)
                    if progress is not None:
                        progress.increment("directories_enumerated")
                finally:
                    guard.close()
                for name in names:
                    budget()
                    _windows_component(name)
                    relative = (PurePosixPath(prefix) / name).as_posix()
                    if progress is not None:
                        progress.set_stage("verifying")
                    with operation("item_observation"):
                        info = self.observe(relative, parent_identity=identity, progress=progress)
                    if progress is not None:
                        progress.increment("items_verified")
                    reparse = bool(info.st_file_attributes & _REPARSE)
                    online = online_only(info) or (reparse and getattr(info, "st_reparse_tag", 0) & 0xFFFF0FFF == 0x9000001A)
                    if stat.S_ISDIR(info.st_mode) and not reparse and not online:
                        yield from visit(folder / name, relative, (info.st_dev, info.st_ino))
                    else:
                        blocked = not online and (reparse or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
                        yield relative, info, blocked, online
                budget()
                if progress is not None:
                    progress.set_stage("verifying")
                with operation("directory_validation"):
                    guard = self._parent_guard(folder)
                try:
                    with operation("native_metadata"):
                        after = _share_info(guard.handles[-1])
                    if signature(before) != signature(after):
                        raise DriveError("SHARE_DIRECTORY_CHANGED", "A shared directory changed while its inventory was being collected.", True,
                                         "Retry after directory updates finish; the previous inventory has not been replaced.")
                finally:
                    guard.close()

            try:
                if progress is not None:
                    progress.set_stage("validating_root")
                with operation("root_validation"):
                    self.check_root()
                yield from visit(self.root, "")
                if progress is not None:
                    progress.set_stage("validating_root")
                with operation("root_validation"):
                    self.check_root()
            except (OSError, DriveError) as exc:
                errors.append(True)
                if isinstance(exc, DriveError):
                    raise
                code = getattr(exc, "winerror", None) or exc.errno
                raise DriveError("SHARE_ENUMERATION_FAILED", "Shared metadata or directory enumeration failed (Windows error %s)." % code,
                                 True, "Restore current Windows access before retrying; this is not an empty folder.") from None

        def close(self):
            self._closed = True
            if self.guard is not None:
                self.guard.close()
                self.guard = None


class StateAccess:
    """Guard state paths; never let source paths double as disposable storage."""
    def __init__(self, path, source, installation):
        self.fd = None
        self.lock_fd = None
        self.guard = None
        self.db_handle = None
        self.private_db_fd = None
        self.source_root = Path(source.root)
        self.source_identity = source.identity
        self.network_source = getattr(source, "provider", None) == "windows_share"
        self.recovery = {"restored_snapshot": False, "discarded_working": False, "rebuilt_after_corruption": False}
        self.path = validate_state_boundaries(path, source, installation)
        if os.name == "nt":
            self.guard = _WindowsDirectories(self.path, create=True, code="UNSAFE_STATE", state=True)
            self.path = self.guard.path
            self.identity = self.guard.identities[-1][1:]
        else:
            self.fd = _make_posix_state(self.path, source.identity)
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino)
        self._ancestor_identities = tuple((ancestor, _identity(ancestor)) for ancestor in (self.path,) + tuple(self.path.parents))
        try:
            self.check()
        except Exception:
            self.close()
            raise

    def check(self):
        try:
            validate_state_location(self.path)
            # A network source is pinned separately and cannot become state:
            # state paths remain fixed-local and every ancestor stays guarded.
            # Do not make private DB recovery depend on a live SMB connection.
            if not self.network_source and overlaps(self.path.resolve(), self.source_root.resolve()):
                raise DriveError("UNSAFE_STATE", "State storage has moved into the approved source boundary.")
            if self.guard:
                self.guard.check()
            else:
                info = os.stat(self.path, follow_symlinks=False)
                if self.fd is None or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
                    raise OSError()
                if self.path.resolve() != self.path or any(_identity(path) != identity for path, identity in self._ancestor_identities):
                    raise DriveError("STATE_CHANGED", "A state ancestor has moved or been redirected.")
                if self.lock_fd is not None:
                    pinned = os.fstat(self.lock_fd)
                    current = os.stat(".worker.lock", dir_fd=self.fd, follow_symlinks=False)
                    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
                        raise DriveError("STATE_CHANGED", "The state lease file has been replaced.")
            # The lock is verified through its opened handle in lock(). Inspecting
            # its pathname here may fail on Windows while another worker leases
            # it with sharing disabled, before we can report a normal busy slot.
            for name in DATABASE_FILES:
                path = self.path / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise DriveError("UNSAFE_STATE", "State files must be ordinary files with one hard link.")
        except OSError:
            raise DriveError("STATE_CHANGED", "The state directory is missing or has been rebound.")

    def lock(self):
        if self.lock_fd is not None:
            return
        self.check()
        path = self.path / ".worker.lock"
        if os.name == "nt":
            handle = None
            try:
                handle = _handle(path, write=True, create=True, exclusive=True)
                info = _file_info(handle)
                if not stat.S_ISREG(info.st_mode) or info.st_file_attributes & _REPARSE or info.st_nlink != 1 or _final_path(handle) != _canonical(path):
                    raise DriveError("UNSAFE_STATE", "The state lock is redirected or multiply linked.")
                self.lock_fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
                handle = None
            except OSError as exc:
                if exc.errno in (32, 33):
                    raise DriveError("STATE_IN_USE", "Another worker is using this state directory.", True, "Reuse the existing worker or use a separate state directory.")
                raise DriveError("STATE_LOCK_FAILED", "The state lease could not be acquired safely.")
            finally:
                if handle is not None:
                    _kernel.CloseHandle(handle)
        else:
            import fcntl
            # macOS can transiently return ENOENT when two O_CREAT|O_NOFOLLOW
            # opens race on a previously absent entry. Revalidate the pinned
            # directory and retry only that error, never links or permission errors.
            for attempt in range(3):
                try:
                    self.lock_fd = _state_open(".worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
                    break
                except FileNotFoundError:
                    self.check()
                    if attempt == 2:
                        raise DriveError("STATE_LOCK_FAILED", "The state lease could not be created safely.")
                except OSError:
                    raise DriveError("UNSAFE_STATE", "The state lock is redirected or inaccessible.")
            try:
                info = os.fstat(self.lock_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DriveError("UNSAFE_STATE", "The state lock is unsafe.")
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception as exc:
                os.close(self.lock_fd)
                self.lock_fd = None
                if isinstance(exc, BlockingIOError):
                    raise DriveError("STATE_IN_USE", "Another worker is using this state directory.", True, "Reuse the existing worker or use a separate state directory.")
                raise

    @contextlib.contextmanager
    def directory(self, name, create=False):
        self.check()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise DriveError("UNSAFE_STATE", "Invalid private state subdirectory.")
        path = self.path / name
        guard = None
        directory_fd = None
        try:
            if os.name == "nt":
                guard = _WindowsDirectories(path, create=create, code="UNSAFE_STATE", state=True)
            else:
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=self.fd)
                    except FileExistsError:
                        pass
                directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                if path.resolve() != path:
                    raise DriveError("UNSAFE_STATE", "A private state subdirectory is redirected.")
            self.check()
            yield path
            self.check()
            if directory_fd is not None:
                before = os.fstat(directory_fd)
                after = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if not stat.S_ISDIR(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise DriveError("UNSAFE_STATE", "A private state subdirectory changed during the operation.")
        except OSError:
            raise DriveError("UNSAFE_STATE", "A private state subdirectory is missing, redirected, or inaccessible.")
        finally:
            if guard:
                guard.close()
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _root_name(name):
        if not isinstance(name, str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,96}", name) is None or name in (".", ".."):
            raise DriveError("UNSAFE_STATE", "Invalid private metadata filename.")
        if os.name == "nt":
            _windows_component(name)
        return name

    def _require_lease(self):
        self.check()
        if self.lock_fd is None:
            raise DriveError("STATE_NOT_LEASED", "This operation requires an exclusive state lease.")

    def _root_exists(self, name):
        self._root_name(name)
        return os.path.lexists(self.path / name)

    def _open_root(self, name, write=False, create=False, exclusive=False, metadata_read=False):
        """Open a regular single-link file; callers own the returned descriptor."""
        self.check()
        name = self._root_name(name)
        path = self.path / name
        fd = None
        handle = None
        try:
            if os.name == "nt":
                if exclusive and self._root_exists(name):
                    raise FileExistsError()
                handle = _handle(path, write=write, create=create, metadata_read=metadata_read)
                info = _file_info(handle)
                if info.st_file_attributes & (_REPARSE | _DIRECTORY) or _final_path(handle) != _canonical(path):
                    raise DriveError("UNSAFE_STATE", "A private state file has been redirected.")
                fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY)
                handle = None
            else:
                flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
                if create:
                    flags |= os.O_CREAT
                if exclusive:
                    flags |= os.O_EXCL
                fd = _state_open(name, flags, 0o600, dir_fd=self.fd)
                info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DriveError("UNSAFE_STATE", "Private state files must be ordinary files with one hard link.")
            self.check()
            return fd
        except Exception:
            if fd is not None:
                os.close(fd)
            raise
        finally:
            if handle is not None:
                _kernel.CloseHandle(handle)

    def unlink_root(self, name):
        self._require_lease()
        if not self._root_exists(name):
            return
        fd = self._open_root(name)
        os.close(fd)
        if os.name == "nt":
            (self.path / name).unlink()
        else:
            os.unlink(name, dir_fd=self.fd)

    def _replace_root(self, source_name, destination_name):
        self._require_lease()
        for name in (source_name, destination_name):
            self._root_name(name)
            if self._root_exists(name):
                fd = self._open_root(name)
                os.close(fd)
        self.check()
        if os.name == "nt":
            handle = _handle(self.path / source_name, rename=True)
            try:
                info = _file_info(handle)
                if (info.st_file_attributes & (_REPARSE | _DIRECTORY) or info.st_nlink != 1 or
                        _final_path(handle) != _canonical(self.path / source_name)):
                    raise DriveError("UNSAFE_STATE", "The private publication source has been redirected.")
                # Writers already fsync and close staging data before publishing.
                # Keep this verified source handle open through the atomic rename.
                # This is process-crash recovery, not a verified power-loss
                # guarantee or a claim of MOVEFILE_WRITE_THROUGH equivalence.
                _rename_same_directory(handle, destination_name)
                if _final_path(handle) != _canonical(self.path / destination_name):
                    raise DriveError("STATE_CHANGED", "The published state file resolved outside its expected name.")
            finally:
                _kernel.CloseHandle(handle)
        else:
            os.replace(source_name, destination_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        self.check()

    def read_metadata(self, name, max_bytes=65536):
        """Read bounded JSON with no lock required, e.g. a live broker endpoint."""
        name = self._root_name(name)
        if not name.endswith(".json"):
            raise DriveError("UNSAFE_STATE", "Metadata files must use a JSON filename.")
        try:
            fd = self._open_root(name, metadata_read=True)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "rb") as source:
                self._metadata_privacy(name, source.fileno())
                data = source.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise DriveError("INVALID_STATE_METADATA", "Private metadata exceeds its size limit.")
            result = json.loads(data.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise DriveError("INVALID_STATE_METADATA", "Private metadata must be a JSON object.")

    def write_metadata(self, name, value):
        self._require_lease()
        name = self._root_name(name)
        if not name.endswith(".json") or not isinstance(value, dict):
            raise DriveError("INVALID_STATE_METADATA", "Metadata must be a JSON object with a JSON filename.")
        self._metadata_privacy(name)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > 65536:
            raise DriveError("INVALID_STATE_METADATA", "Private metadata exceeds its size limit.")
        staging = self._root_name(name + ".next")
        self.unlink_root(staging)
        fd = self._open_root(staging, write=True, create=True, exclusive=True)
        try:
            with os.fdopen(fd, "wb") as output:
                if os.name == "posix":
                    os.fchmod(output.fileno(), 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._replace_root(staging, name)
        except Exception:
            self.unlink_root(staging)
            raise

    def _metadata_privacy(self, name, fd=None):
        if os.name != "posix":
            return  # Windows metadata inherits the guarded local account directory ACL.
        if name in ("endpoint.json", "startup-error.json"):
            folder = os.fstat(self.fd)
            if folder.st_uid != os.geteuid() or stat.S_IMODE(folder.st_mode) & 0o077:
                raise DriveError("UNSAFE_STATE_PERMISSIONS", "Broker control must be owned by the current user with mode 0700.")
        if fd is not None:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise DriveError("UNSAFE_STATE_PERMISSIONS", "Private metadata must be owned by the current user with mode 0600.")

    def remove_metadata(self, name):
        if not self._root_name(name).endswith(".json"):
            raise DriveError("UNSAFE_STATE", "Metadata files must use a JSON filename.")
        self.unlink_root(name)

    def profile_manifest(self):
        return {"format": "df-mcp-state", "version": 1, "root": str(self.source_root),
                "root_identity": list(self.source_identity) if self.source_identity is not None else None,
                "storage": "atomic-sqlite-snapshot-v1"}

    def write_profile_manifest(self):
        expected = self.profile_manifest()
        existing = self.read_metadata("profile.json")
        if existing is not None and (existing.get("format") != expected["format"] or
                existing.get("version") != 1 or existing.get("root") != expected["root"] or
                existing.get("root_identity") != expected["root_identity"]):
            raise DriveError("STATE_ROOT_MISMATCH", "State belongs to another source identity.", False,
                             "Use explicit maintenance --reset-index --accept-source-replacement after reviewing the source change.")
        if existing != expected:
            self.write_metadata("profile.json", expected)

    def _sqlite_open(self, name):
        """Open SQLite without permitting any journal-path recovery or writes yet."""
        import sqlite3
        self._require_lease()
        for suffix in ("-wal", "-shm", "-journal"):
            if self._root_exists(name + suffix):
                raise DriveError("STATE_REBUILD_REQUIRED", "Unexpected SQLite sidecars require explicit maintenance.")
        fd = self._open_root(name, write=True, create=True)
        expected = os.fstat(fd)
        connection = None
        try:
            with os.fdopen(_state_dup(fd), "rb") as header_file:
                header = header_file.read(20)
            if len(header) >= 20 and header[:16] == b"SQLite format 3\0" and (header[18] == 2 or header[19] == 2):
                raise DriveError("STATE_REBUILD_REQUIRED", "Legacy WAL format requires explicit maintenance before migration.")
            with _DB_OPEN_LOCK:
                before_fds = _live_fds() if os.name == "posix" else set()
                connection = sqlite3.connect((self.path / name).as_uri() + "?mode=rw", uri=True, timeout=15, check_same_thread=False)
                if os.name == "posix":
                    import fcntl
                    verified = False
                    for value, _, _ in _live_fds() - before_fds:
                        try:
                            opened = os.fstat(int(value))
                            writable = fcntl.fcntl(int(value), fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
                        except OSError:
                            continue
                        if stat.S_ISREG(opened.st_mode) and writable:
                            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                                raise DriveError("UNSAFE_STATE", "SQLite opened a redirected database file.")
                            verified = True
                    if not verified:
                        raise DriveError("UNSAFE_STATE", "The SQLite descriptor could not be verified safely.")
            self.check()
            # SQLite may inspect a rollback journal on its first database read.
            # Refuse even ordinary sidecars that appeared during connect before
            # allowing any PRAGMA/schema operation to run.
            if any(self._root_exists(name + suffix) for suffix in ("-wal", "-shm", "-journal")):
                raise DriveError("UNSAFE_STATE", "A SQLite sidecar appeared during guarded database opening.")
            connection.execute("PRAGMA locking_mode=EXCLUSIVE")
            connection.execute("PRAGMA journal_mode=MEMORY")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA temp_store=MEMORY")
            return connection, fd
        except Exception:
            if connection is not None:
                connection.close()
            os.close(fd)
            raise

    def _copy_snapshot_to_working(self):
        # No SQLite connection opens committed data: copy via guarded descriptors.
        source_fd = self._open_root("index.sqlite")
        target_fd = None
        try:
            target_fd = self._open_root("index.working.sqlite", write=True, create=True, exclusive=True)
            with os.fdopen(_state_dup(source_fd), "rb") as source, os.fdopen(_state_dup(target_fd), "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                target.flush()
            self.check()
        finally:
            os.close(source_fd)
            if target_fd is not None:
                os.close(target_fd)

    @staticmethod
    def _corrupt_error(exc):
        import sqlite3
        return isinstance(exc, sqlite3.DatabaseError) and (
            getattr(exc, "sqlite_errorcode", None) in (11, 26) or
            "malformed" in str(exc).casefold() or "not a database" in str(exc).casefold())

    def connect_database(self):
        """Use a disposable working database and retain a complete committed snapshot.

        MEMORY journaling is confined to working/staging files. A process crash
        never publishes them: startup restores the last atomically committed
        snapshot and reindexes any work that did not reach a checkpoint.
        """
        import sqlite3
        self._require_lease()
        manifest = self.read_metadata("profile.json")
        if manifest is not None:
            expected = self.profile_manifest()
            if any(manifest.get(key) != expected[key] for key in ("format", "version", "root", "root_identity")):
                raise DriveError("STATE_ROOT_MISMATCH", "State belongs to another source identity.", False,
                                 "Review the changed source, then use explicit maintenance --reset-index --accept-source-replacement.")
        for name in ("index.working.sqlite", "index.next.sqlite"):
            if self._root_exists(name):
                self.recovery["discarded_working"] = True
                self.unlink_root(name)
            for suffix in ("-wal", "-shm", "-journal"):
                if self._root_exists(name + suffix):
                    # These are known disposable derivatives, but reject redirects.
                    self.unlink_root(name + suffix)
        for suffix in ("-wal", "-shm", "-journal"):
            if self._root_exists("index.sqlite" + suffix):
                raise DriveError("STATE_REBUILD_REQUIRED", "Legacy SQLite sidecars require explicit maintenance before migration.")
        committed = self._root_exists("index.sqlite")
        if committed:
            self._copy_snapshot_to_working()
        try:
            connection, fd = self._sqlite_open("index.working.sqlite")
            try:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("database disk image is malformed")
            except Exception:
                connection.close()
                os.close(fd)
                raise
        except Exception as exc:
            if not committed or not self._corrupt_error(exc):
                raise
            # Auto-recovery only owns snapshots marked by our protected manifest.
            # Unmarked legacy/unknown files require an explicit reset instead.
            if manifest is None:
                raise DriveError("STATE_REBUILD_REQUIRED", "The unmarked legacy index is corrupt; explicit maintenance is required.")
            self.unlink_root("index.working.sqlite")
            self._replace_root("index.sqlite", "index.corrupt.sqlite")
            self.recovery["rebuilt_after_corruption"] = True
            connection, fd = self._sqlite_open("index.working.sqlite")
        self.private_db_fd = fd
        self.recovery["restored_snapshot"] = committed and not self.recovery["rebuilt_after_corruption"]
        return connection

    def checkpoint_database(self, connection):
        """Publish one full consistent snapshot outside an active transaction.

        Fixed derived names bound retained disk usage to working + committed +
        staging and at most one quarantined corrupt snapshot. Failed publication
        leaves the previous committed snapshot untouched.
        """
        import sqlite3
        self._require_lease()
        if connection.in_transaction:
            raise DriveError("STATE_TRANSACTION_ACTIVE", "Checkpoint requires a completed SQLite transaction.")
        self.unlink_root("index.next.sqlite")
        target = None
        fd = None
        try:
            target, fd = self._sqlite_open("index.next.sqlite")
            connection.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DriveError("STATE_REBUILD_REQUIRED", "The working index failed snapshot verification.")
            target.close()
            target = None
            os.fsync(fd)
            os.close(fd)
            fd = None
            self._replace_root("index.next.sqlite", "index.sqlite")
            return {"published": True, "storage": "atomic-sqlite-snapshot-v1"}
        except sqlite3.DatabaseError:
            raise DriveError("STATE_CHECKPOINT_FAILED", "The complete index snapshot could not be saved.", True,
                             "Check free space and local storage; only complete snapshots are published.")
        except OSError:
            raise DriveError("STATE_CHECKPOINT_FAILED", "The complete index snapshot could not be saved.", True,
                             "Check free space and local storage; only complete snapshots are published.")
        finally:
            if target is not None:
                target.close()
            if fd is not None:
                os.close(fd)
            if self._root_exists("index.next.sqlite"):
                self.unlink_root("index.next.sqlite")

    @contextlib.contextmanager
    def new_private(self, path, binary=False):
        parent, name = self._private_location(path)
        parent_fd = None
        fd = None
        with self.directory(parent):
            try:
                if os.name == "nt":
                    # The directory is pinned against replacement and reparse changes.
                    fd = _state_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
                else:
                    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                    fd = _state_open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                with os.fdopen(fd, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as output:
                    fd = None
                    yield output
            finally:
                if fd is not None:
                    os.close(fd)
                if parent_fd is not None:
                    os.close(parent_fd)

    def _private_location(self, path):
        self.check()
        try:
            parts = Path(path).relative_to(self.path).parts
        except ValueError:
            raise DriveError("UNSAFE_STATE", "Private output must remain inside the state directory.")
        if len(parts) != 2 or any(part in (".", "..") for part in parts):
            raise DriveError("UNSAFE_STATE", "Unexpected private output location.")
        return parts

    @contextlib.contextmanager
    def open_private(self, path, mode):
        parent, name = self._private_location(path)
        if mode not in ("r", "w"):
            raise DriveError("UNSAFE_STATE", "Unsupported private file access mode.")
        fd = None
        parent_fd = None
        with self.directory(parent):
            try:
                if os.name == "nt":
                    handle = _handle(path, write=mode == "w")
                    info = _file_info(handle)
                    if info.st_file_attributes & (_REPARSE | _DIRECTORY) or info.st_nlink != 1 or _final_path(handle) != _canonical(path):
                        _kernel.CloseHandle(handle)
                        raise DriveError("UNSAFE_STATE", "A private parser file has been redirected.")
                    fd = msvcrt.open_osfhandle(handle, (os.O_RDWR if mode == "w" else os.O_RDONLY) | os.O_BINARY)
                else:
                    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                    fd = _state_open(name, (os.O_RDWR if mode == "w" else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise DriveError("UNSAFE_STATE", "A private parser file has been redirected.")
                if mode == "w":
                    os.ftruncate(fd, 0)
                with os.fdopen(fd, mode, encoding="utf-8") as output:
                    fd = None
                    yield output
            finally:
                if fd is not None:
                    os.close(fd)
                if parent_fd is not None:
                    os.close(parent_fd)

    def unlink_private(self, path):
        parent, name = self._private_location(path)
        with self.directory(parent):
            if os.name == "nt":
                Path(path).unlink(missing_ok=True)
            else:
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                try:
                    try:
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                finally:
                    os.close(fd)

    def remove_empty_directory(self, name):
        self.check()
        if not name.startswith("job-temp-") or "/" in name or "\\" in name:
            raise DriveError("UNSAFE_STATE", "Unexpected temporary directory name.")
        try:
            if os.name == "nt":
                os.rmdir(self.path / name)
            else:
                os.rmdir(name, dir_fd=self.fd)
        except OSError:
            # Never recurse into a directory with unexpected content.
            pass

    def cleanup_scratch(self, dry_run=False):
        """Reclaim recognized orphan parser files only while owning this slot.

        This deliberately never walks recursively, follows a redirected child,
        removes exports, or guesses ownership of unknown files. The OS lease
        ensures another index worker cannot be parsing in this directory.
        """
        self.check()
        if self.lock_fd is None:
            raise DriveError("STATE_NOT_LEASED", "Scratch cleanup requires an exclusive state lease.")
        result = {"removed_files": 0, "removed_directories": 0, "skipped_directories": 0,
                  "eligible_files": 0, "eligible_directories": 0, "dry_run": dry_run}
        for name in os.listdir(self.path if os.name == "nt" else self.fd):
            if re.fullmatch(r"job-temp-[0-9a-f]{32}", name) is None:
                continue
            try:
                with self.directory(name) as folder:
                    with os.scandir(folder) as entries:
                        children = list(entries)
                    safe = True
                    for entry in children:
                        # Windows DirEntry caches report nlink/ino/dev as zero.
                        # Query the pinned-directory child for real link count;
                        # never treat an unknown/zero count as a safe single link.
                        info = os.stat(folder / entry.name, follow_symlinks=False)
                        recognized = re.fullmatch(r"(?:parsed-[0-9a-f]{32}\.jsonl|snapshot-[0-9a-f]{32}\.(?:txt|md|csv|tsv|xlsx|docx|pptx|pdf))", entry.name)
                        if not recognized or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
                            safe = False
                            break
                    if not safe:
                        result["skipped_directories"] += 1
                        continue
                    result["eligible_files"] += len(children)
                    result["eligible_directories"] += 1
                    if dry_run:
                        continue
                    for entry in children:
                        self.unlink_private(folder / entry.name)
                        result["removed_files"] += 1
                self.remove_empty_directory(name)
                if not (self.path / name).exists():
                    result["removed_directories"] += 1
            except (DriveError, OSError):
                # Unknown or concurrently altered contents need manual review.
                result["skipped_directories"] += 1
        return result

    def close(self):
        if self.private_db_fd is not None:
            os.close(self.private_db_fd)
            self.private_db_fd = None
        if self.db_handle is not None:
            _kernel.CloseHandle(self.db_handle)
            self.db_handle = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.guard:
            self.guard.close()
            self.guard = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def SourceAccess(root, *, allow_network=False):
    if allow_network:
        from .network_source import WindowsShareSource
        return WindowsShareSource(root)
    return WindowsSource(root) if os.name == "nt" else PosixSource(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-root", required=True)
    args = parser.parse_args()
    source = None
    try:
        source = SourceAccess(args.validate_root)
        source.check_root()
        print(json.dumps({"ok": True, "platform": sys.platform, "read_only_sources": True}))
    except DriveError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": {"code": "UNSAFE_ROOT", "message": "The directory could not be validated safely."}}))
        return 1
    finally:
        if source:
            source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
