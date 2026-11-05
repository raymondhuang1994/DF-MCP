"""Explicit Windows SMB source using the current Windows identity.

No credentials, drive mapping, source writes or permissive DFS fallback. Native
Windows I/O may block until the OS returns; budgets are checked between calls,
not advertised as cancellation of an in-flight SMB request.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import PureWindowsPath
import re
import threading
import time

from .errors import DriveError
from .fs_access import _windows_component, relative_parts


def _error(code, message):
    return DriveError(code, message, True, "Restore the approved shared folder and current Windows access, then refresh. A different source requires a separate index.")


def normalize_share_path(raw):
    """Pure syntax validation; never resolve a path or contact a server."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or "/" in raw or "\0" in raw or raw.startswith(("\\\\?\\", "\\\\.\\")):
        raise _error("INVALID_SHARE_PATH", "Use an ordinary UNC path or an absolute mapped Windows drive path.")
    if raw.startswith("\\\\"):
        parts = raw[2:].rstrip("\\").split("\\")
        if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", parts[0]):
            raise _error("INVALID_SHARE_PATH", "A UNC path must include an explicit server and share.")
    elif re.match(r"^[A-Za-z]:\\", raw):
        parts = raw[3:].rstrip("\\").split("\\") if raw[3:].rstrip("\\") else []
    else:
        raise _error("INVALID_SHARE_PATH", "Relative paths, URLs and device paths are not shared-folder sources.")
    if len(parts) > 128:
        raise _error("INVALID_SHARE_PATH", "The shared-folder path exceeds its component limit.")
    for part in parts:
        if not part or part in (".", ".."):
            raise _error("INVALID_SHARE_PATH", "Shared-folder paths cannot contain traversal or empty components.")
        _windows_component(part)
    return str(PureWindowsPath(raw))


def resolve_share_path(raw):
    value = normalize_share_path(raw)
    if os.name != "nt":
        raise _error("UNSUPPORTED_PLATFORM", "Windows shared-folder sources require a Windows execution environment and its current network identity.")
    import ctypes
    from ctypes import wintypes
    from .fs_access import _kernel
    path = PureWindowsPath(value)
    if _kernel.GetDriveTypeW(path.anchor) != 4:  # DRIVE_REMOTE only.
        raise _error("SHARE_UNAVAILABLE", "The selected path is not an accessible mapped network drive or UNC share.")
    if value.startswith("\\\\"):
        return value
    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    mpr.WNetGetConnectionW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    mpr.WNetGetConnectionW.restype = wintypes.DWORD
    length = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(length.value)
    if mpr.WNetGetConnectionW(path.drive, buffer, ctypes.byref(length)):
        raise _error("SHARE_UNAVAILABLE", "The current user's drive mapping cannot be verified.")
    unc = normalize_share_path(str(PureWindowsPath(buffer.value).joinpath(*path.parts[1:])))
    if not unc.startswith("\\\\"):
        raise _error("INVALID_SHARE_PATH", "The mapped drive does not resolve to an ordinary UNC share.")
    return unc


def share_settings(config=None):
    config = {} if config is None else config
    defaults = {"max_items": 10000, "max_depth": 32, "scan_budget_seconds": 120}
    maximum = {"max_items": 100000, "max_depth": 128, "scan_budget_seconds": 600}
    if not isinstance(config, dict) or set(config) - set(defaults):
        raise _error("INVALID_CONFIG", "Use only bounded shared-folder scan settings; credentials are supplied by Windows.")
    result = dict(defaults, **config)
    for name, value in result.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not 1 <= value <= maximum[name] or (name != "scan_budget_seconds" and not isinstance(value, int))):
            raise _error("INVALID_CONFIG", "Shared-folder scan limits are outside their supported bounds.")
    return result


def _principal_binding():
    """Bind persistent state to the Windows user SID, never an account password."""
    from .windows_identity import principal_binding
    return principal_binding()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_nlink, getattr(info, "st_file_attributes", 0))


def _digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


class WindowsShareSource:
    provider = "windows_share"

    def __init__(self, root, config=None, max_file_bytes=268435456):
        self.configured_root = normalize_share_path(str(root))
        self.limits = share_settings(config)
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
            raise _error("INVALID_CONFIG", "The shared source size limit must be positive.")
        self.max_file_bytes = max_file_bytes
        resolved = resolve_share_path(self.configured_root)
        from .fs_access import WindowsSource
        self._native = WindowsSource(resolved, allow_network=True)
        self.root, self.identity = self._native.root, self._native.identity
        try:
            self._resolved = str(PureWindowsPath(resolved)).casefold()
            self.binding = {"provider": self.provider, "unc": self._resolved,
                            "target": self._native.target, "identity": [str(value) for value in self.identity],
                            "principal": _principal_binding()}
            self._binding_id = _digest_json(self.binding)
            self._metadata = {}
            self._lock = threading.RLock()
        except Exception:
            self._native.close()
            raise

    @staticmethod
    def _deadline(deadline):
        if time.monotonic() >= deadline:
            raise _error("SHARE_TIMEOUT", "The shared-folder operation exceeded its budget between Windows I/O calls.")

    def check_root(self):
        if str(PureWindowsPath(resolve_share_path(self.configured_root))).casefold() != self._resolved:
            raise _error("SHARE_ROOT_CHANGED", "The current drive mapping resolves to a different shared folder.")
        if _principal_binding() != self.binding["principal"]:
            raise _error("SHARE_IDENTITY_CHANGED", "The current Windows account differs from the indexed account.")
        self._native.check_root()

    def _origin(self, relative, info):
        return {"provider": self.provider, "source_binding": self._binding_id,
                "relative_path": relative, "file_identity": [str(info.st_dev), str(info.st_ino)],
                "size_bytes": info.st_size, "modified_ns": info.st_mtime_ns,
                "change_ns": info.st_ctime_ns}

    def evidence(self, relative):
        with self._lock:
            return dict(self._metadata.get(relative, {}))

    @property
    def resumable(self):
        return callable(getattr(self._native, "scan_directory", None))

    def directory_fingerprint(self, relative, deadline, progress=None):
        self._deadline(deadline)
        self.check_root()
        result = self._native.directory_fingerprint(relative, deadline=deadline, progress=progress)
        self._deadline(deadline)
        return result

    def scan_directory(self, relative, expected_identity, deadline, max_items, progress=None):
        """Publish only a verified directory unit, while preserving root guards."""
        self._deadline(deadline)
        with progress.operation("root_validation") if progress is not None else contextlib.nullcontext():
            self.check_root()
        result = self._native.scan_directory(relative, expected_identity=expected_identity,
                    max_depth=self.limits["max_depth"], max_items=max_items,
                    deadline=deadline, progress=progress)
        with progress.operation("root_validation") if progress is not None else contextlib.nullcontext():
            self.check_root()
        self._deadline(deadline)
        metadata = {}
        for path, info, unused_blocked, unused_online in result["files"]:
            relative_parts(path)
            metadata[path] = self._origin(path, info)
        with self._lock:
            self._metadata.update(metadata)
        if progress is not None:
            progress.increment("pending_items", len(result["files"]))
            progress.set_stage("publishing")
        return result

    def walk(self, errors, progress=None):
        """Publish only a complete manifest. Any inaccessible subtree aborts it."""
        deadline = time.monotonic() + self.limits["scan_budget_seconds"]
        pending, metadata, walk_errors = [], {}, []
        try:
            if progress is not None:
                progress.set_stage("validating_root")
            with progress.operation("root_validation") if progress is not None else contextlib.nullcontext():
                self.check_root()
            for relative, info, blocked, online in self._native.walk(
                    walk_errors, max_depth=self.limits["max_depth"], max_items=self.limits["max_items"],
                    deadline=deadline, progress=progress):
                self._deadline(deadline)
                relative_parts(relative)
                if len(pending) >= self.limits["max_items"]:
                    raise _error("SHARE_SCAN_LIMIT", "The shared-folder scan exceeds its item limit.")
                pending.append((relative, info, blocked, online))
                metadata[relative] = self._origin(relative, info)
                if progress is not None:
                    progress.increment("pending_items")
            if walk_errors:
                raise _error("SHARE_SCAN_INCOMPLETE", "At least one shared folder or item could not be checked; the inventory was not replaced.")
            if progress is not None:
                progress.set_stage("validating_root")
            with progress.operation("root_validation") if progress is not None else contextlib.nullcontext():
                self.check_root()
            self._deadline(deadline)
            with self._lock:
                self._metadata = metadata
        except (DriveError, OSError) as exc:
            errors.append(True)
            if isinstance(exc, DriveError):
                raise
            raise _error("SHARE_UNAVAILABLE", "The shared folder could not be enumerated under the current Windows identity.") from None
        if progress is not None:
            progress.set_stage("publishing")
        yield from pending

    def digest(self, relative, snapshot=None, deadline=None):
        relative_parts(relative)
        deadline = time.monotonic() + 30 if deadline is None else deadline
        self._deadline(deadline)
        self.check_root()
        fd = None
        try:
            fd, before = self._native.open_read(relative)
            if before.st_size > self.max_file_bytes:
                raise _error("FILE_TOO_LARGE", "The shared source exceeds max_file_bytes.")
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "rb") as source:
                fd = None
                while True:
                    self._deadline(deadline)
                    block = source.read(min(1048576, self.max_file_bytes + 1 - size))
                    if not block:
                        break
                    size += len(block)
                    if size > self.max_file_bytes:
                        raise _error("FILE_TOO_LARGE", "The shared source grew beyond its size limit.")
                    digest.update(block)
                    if snapshot is not None:
                        snapshot.write(block)
                after = self._native.stat_fd(source.fileno())
            current = self._native.observe(relative)
            self.check_root()
            self._deadline(deadline)
            if size != before.st_size or _signature(before) != _signature(after) or _signature(after) != _signature(current):
                raise _error("SHARE_SOURCE_CHANGED", "The shared file or its pathname changed while it was read.")
            origin = self._origin(relative, current)
            origin["content_sha256"] = digest.hexdigest()
            version = _digest_json(origin)
            origin["source_version"] = version
            with self._lock:
                self._metadata[relative] = origin
            return version, current
        except OSError:
            raise _error("SHARE_UNAVAILABLE", "The shared file could not be read under the current Windows identity.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def verify_origin(self, relative, origin, deadline=None):
        self.check_root()
        if not isinstance(origin, dict) or origin.get("source_binding") != self._binding_id or origin.get("relative_path") != relative:
            raise _error("SHARE_SOURCE_CHANGED", "The cached candidate does not belong to the current shared source.")
        if origin.get("source_version"):
            version, _ = self.digest(relative, deadline=deadline)
            if version != origin["source_version"]:
                raise _error("SHARE_SOURCE_CHANGED", "The current shared file differs from the cached candidate.")
        else:
            self._deadline(time.monotonic() + 30 if deadline is None else deadline)
            try:
                current = self._native.observe(relative)
            except OSError:
                raise _error("SHARE_UNAVAILABLE", "The shared candidate is no longer accessible.") from None
            if self._origin(relative, current) != origin:
                raise _error("SHARE_SOURCE_CHANGED", "The shared candidate metadata changed.")
        return self.evidence(relative)

    def close(self):
        self._native.close()
