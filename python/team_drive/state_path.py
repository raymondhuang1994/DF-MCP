"""Read-only Windows state-path selection, including packaged-process defaults.

Only the OS-reported LocalAppData default may be redirected here. Arbitrary
custom paths still use the ordinary directory guard; its target checks are not
relaxed. No function creates a directory or an index.
"""
from __future__ import annotations

import ctypes
import ntpath
from pathlib import Path
import stat
import uuid

from .errors import DriveError
from .fs_access import canonical_windows_path, validate_state_location


_LOCAL_APP_DATA = "f1b32785-6fba-4fcf-9d55-7b8e7f157091"
_DONT_VERIFY = 0x00004000
_NO_PACKAGE_REDIRECTION = 0x00010000
_RETURN_FILTER_REDIRECTION_TARGET = 0x00040000


def _unsafe(message="The local configuration directory cannot be verified safely."):
    raise DriveError("STATE_PATH_UNSAFE", message, False,
                     "Choose a private fixed local state directory and create a new plan.")


def _known_local_appdata(flags):
    """Ask the current process/user's shell mapping without KF_FLAG_CREATE.

    Microsoft documents RETURN_FILTER_REDIRECTION_TARGET for the actual file
    system target in packaged processes. DONT_VERIFY avoids requiring a new
    target to exist; our native ancestor guard performs the separate checks.
    https://learn.microsoft.com/windows/win32/api/shlobj_core/ne-shlobj_core-known_folder_flag
    """
    class GUID(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    function = shell.SHGetKnownFolderPath
    function.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)]
    function.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes, ole.CoTaskMemFree.restype = [ctypes.c_void_p], None
    folder = GUID.from_buffer_copy(uuid.UUID(_LOCAL_APP_DATA).bytes_le)
    result = ctypes.c_void_p()
    try:
        status = function(ctypes.byref(folder), flags | _DONT_VERIFY, None, ctypes.byref(result))
        if status != 0 or not result.value:
            _unsafe("Windows could not identify the current process's local data directory.")
        value = ctypes.wstring_at(result.value)
        if not value or len(value) > 32767 or not ntpath.isabs(value):
            _unsafe()
        return value
    finally:
        # SHGetKnownFolderPath requires freeing even a failed call's output.
        if result.value:
            ole.CoTaskMemFree(result)


def _check_spelling(path):
    from . import fs_access as fs
    path = fs._valid_windows_path(path)
    validate_state_location(path)
    for current in reversed((path,) + tuple(path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & fs._REPARSE:
            _unsafe("Local configuration paths cannot contain links, junctions or reparse points.")
        if current != path and not stat.S_ISDIR(info.st_mode):
            _unsafe("Local configuration ancestors must be ordinary directories.")
    return path


def canonical_windows_state_path(path):
    """Guard the nearest existing directory, preserving the uncreated suffix."""
    from . import fs_access as fs
    path = _check_spelling(path)
    current, missing = path, []
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current == current.parent:
                _unsafe()
            missing.insert(0, current.name)
            current = current.parent
            continue
        if not stat.S_ISDIR(info.st_mode):
            if current != path:
                _unsafe("Local configuration ancestors must be ordinary directories.")
            missing.insert(0, current.name)
            current = current.parent
        break
    guard = fs._WindowsDirectories(current, state=True, code="STATE_PATH_UNSAFE")
    try:
        guard.check()
        result = guard.path.joinpath(*missing)
        validate_state_location(result)
        return result
    finally:
        guard.close()


def _verify_existing_alias(logical, physical):
    """Refuse to silently fork a pre-existing default profile into another base."""
    from . import fs_access as fs
    handles = []
    try:
        try:
            logical_handle = fs._handle(logical, directory=True)
        except FileNotFoundError:
            return  # Nothing under the old default to silently strand.
        handles.append(logical_handle)
        try:
            physical_handle = fs._handle(physical, directory=True)
        except FileNotFoundError:
            _unsafe("The existing default state and Windows-reported target differ; no new profile was created.")
        handles.append(physical_handle)
        bindings = []
        for handle in handles:
            info = fs._file_info(handle)
            if (not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & fs._REPARSE or
                    fs._kernel.GetFileType(handle) != 1):
                _unsafe()
            bindings.append((info.st_dev, info.st_ino, fs._final_path(handle)))
        if bindings[0] != bindings[1] or bindings[1][2] != fs._canonical(physical):
            _unsafe("The existing default state and Windows-reported target have different identities.")
        # Recheck while both directory identities remain pinned.
        for handle, before in zip(handles, bindings):
            info = fs._file_info(handle)
            if (info.st_file_attributes & fs._REPARSE or
                    (info.st_dev, info.st_ino, fs._final_path(handle)) != before):
                _unsafe("The default state directory changed during verification.")
    finally:
        for handle in reversed(handles):
            fs._kernel.CloseHandle(handle)


def windows_default_state_base(local_appdata):
    """Resolve just the default base; explicit --state-dir values never remap."""
    from . import fs_access as fs
    try:
        logical_root = _check_spelling(Path(local_appdata))
        ordinary = fs._valid_windows_path(_known_local_appdata(_NO_PACKAGE_REDIRECTION))
        physical_root = fs._valid_windows_path(_known_local_appdata(_RETURN_FILTER_REDIRECTION_TARGET))
        # A custom environment location is not an OS-authorized alias. Preserve
        # its ordinary fixed-local guard rather than rewriting its components.
        if canonical_windows_path(logical_root) not in {
                canonical_windows_path(ordinary), canonical_windows_path(physical_root)}:
            return canonical_windows_state_path(logical_root / "df-mcp")
        logical, physical = logical_root / "df-mcp", physical_root / "df-mcp"
        _check_spelling(logical)
        result = canonical_windows_state_path(physical)
        if canonical_windows_path(logical) != canonical_windows_path(result):
            _verify_existing_alias(logical, result)
        return result
    except DriveError:
        raise
    except (OSError, ValueError, AttributeError):
        _unsafe("Windows could not verify the default local configuration location.")
