"""Read-only discovery and confirmed, Python-only source configuration.

No command scans document contents or builds an index. Cloud authorization is a
separate explicit action; folder plans only inspect the saved account binding.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

from .bounded_json import loads
from .errors import DriveError
from .fs_access import overlaps, validate_state_location

TEAM_NAMES = ("Digital Finance", "Digital Finance_0")
MAX_JSON_BYTES = 262144


def _fail(code, message, next_action="Review the source and create a new plan."):
    raise DriveError(code, message, False, next_action)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def installation_root():
    root = Path(__file__).resolve().parents[2]
    return root.parent if root.name == "runtime" else root


def default_state_base():
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root or not ntpath.isabs(root):
            _fail("UNSAFE_STATE", "LOCALAPPDATA is unavailable; specify a private fixed local state directory.")
        from .state_path import windows_default_state_base
        return windows_default_state_base(root)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "df-mcp"
    root = os.environ.get("XDG_STATE_HOME")
    if root and not Path(root).is_absolute():
        _fail("UNSAFE_STATE", "XDG_STATE_HOME must be an absolute local path.")
    return (Path(root) if root else Path.home() / ".local" / "state") / "df-mcp"


def _local_path(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or "\0" in str(value):
        _fail("UNSAFE_STATE", "Use an ordinary private local path.")
    if str(value).startswith(("\\\\", "//")):
        _fail("UNSAFE_STATE", "State and credentials must be on a fixed local drive.")
    path = Path(value).expanduser().absolute()
    validate_state_location(path)
    if os.name == "nt":
        from .fs_access import _valid_windows_path
        path = _valid_windows_path(path)
    for current in reversed((path,) + tuple(path.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("UNSAFE_STATE", "State paths cannot traverse symbolic links, junctions or reparse points.")
        if current != path and not stat.S_ISDIR(info.st_mode):
            _fail("UNSAFE_STATE", "State ancestors must be ordinary directories.")
    if os.name == "nt":
        from .state_path import canonical_windows_state_path
        return canonical_windows_state_path(path)
    return path.resolve()


def _base(state_dir=None):
    path = _local_path(default_state_base() if state_dir is None else state_dir)
    if overlaps(path.resolve(), installation_root()):
        _fail("UNSAFE_STATE", "The installation and private state directory must not overlap.")
    if path.exists() and not path.is_dir():
        _fail("UNSAFE_STATE", "The private state base must be a directory.")
    return path


def _directory_path(value, private=False, empty=False):
    path = _local_path(value)
    try:
        info = path.stat()
    except FileNotFoundError:
        return path
    if not stat.S_ISDIR(info.st_mode):
        _fail("UNSAFE_STATE", "Profile, anchor and state locations must be ordinary directories.")
    if private and os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        _fail("UNSAFE_STATE", "Existing profile and state directories must belong to this user and be private (0700).")
    if empty and next(path.iterdir(), None) is not None:
        _fail("UNSAFE_ROOT", "The cloud anchor must remain empty; it is not a source document directory.")
    return path


def _windows_drive_type(path):
    from ctypes import wintypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    function.argtypes, function.restype = [wintypes.LPCWSTR], wintypes.UINT
    return int(function(ntpath.splitdrive(path)[0] + "\\"))


def classify(source):
    if not isinstance(source, str) or not source.strip() or len(source) > 8192 or "\0" in source:
        _fail("INVALID_SOURCE", "Supply one directory path or an HTTPS OneDrive/SharePoint folder URL.")
    source = source.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", source):
        try:
            url = urlsplit(source)
            host = (url.hostname or "").lower()
            valid = (url.scheme == "https" and url.port in (None, 443) and
                     not url.username and not url.password and not url.fragment and
                     (host == "onedrive.live.com" or host.endswith(".sharepoint.com")))
        except ValueError:
            valid = False
        if not valid:
            _fail("UNSUPPORTED_CLOUD_URL", "Use a direct HTTPS OneDrive or SharePoint folder URL; shortened sharing links are not supported.")
        return {"provider": "microsoft_graph", "kind": "cloud_url", "source": source,
                "requires_authorization": True, "supported_on_this_platform": True}
    if source.startswith(("\\\\", "//")):
        from .network_source import normalize_share_path
        normalized = normalize_share_path(source)
        return {"provider": "windows_share", "kind": "unc", "source": normalized,
                "requires_authorization": False, "supported_on_this_platform": os.name == "nt"}
    if re.match(r"^[A-Za-z]:", source):
        if not re.match(r"^[A-Za-z]:[\\/]", source):
            _fail("INVALID_SOURCE", "Use an absolute drive path, not a drive-relative path.")
        if os.name != "nt":
            return {"provider": None, "kind": "windows_drive", "source": ntpath.normpath(source),
                    "requires_drive_type_check": True, "supported_on_this_platform": False}
        drive_type = _windows_drive_type(source)
        if drive_type not in (3, 4):
            _fail("UNSUPPORTED_ROOT_LOCATION", "Choose a fixed local drive or an already connected network drive.")
        if drive_type == 4:
            from .network_source import normalize_share_path
            source = normalize_share_path(source)
        else:
            source = str(Path(source).absolute())
        return {"provider": "windows_share" if drive_type == 4 else "local",
                "kind": "mapped_network_drive" if drive_type == 4 else "local_directory",
                "source": source, "drive_type": drive_type,
                "requires_authorization": False, "supported_on_this_platform": True}
    return {"provider": "local", "kind": "local_directory", "source": str(Path(source).expanduser().absolute()),
            "requires_authorization": False, "supported_on_this_platform": True}


def _connected_windows_resources():
    """Enumerate only this logon's connected disk resources, never global servers."""
    if os.name != "nt":
        return []
    from ctypes import wintypes

    class Resource(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ("scope", "type", "display", "usage")] + [
            (name, wintypes.LPWSTR) for name in ("local", "remote", "comment", "provider")]

    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    mpr.WNetOpenEnumW.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(Resource), ctypes.POINTER(wintypes.HANDLE)]
    mpr.WNetOpenEnumW.restype = wintypes.DWORD
    mpr.WNetEnumResourceW.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                     wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD)]
    mpr.WNetEnumResourceW.restype = wintypes.DWORD
    mpr.WNetCloseEnum.argtypes, mpr.WNetCloseEnum.restype = [wintypes.HANDLE], wintypes.DWORD
    handle = wintypes.HANDLE()
    # RESOURCE_CONNECTED=1 and RESOURCETYPE_DISK=1 intentionally exclude GLOBALNET.
    if mpr.WNetOpenEnumW(1, 1, 0, None, ctypes.byref(handle)) != 0:
        _fail("DISCOVERY_UNAVAILABLE", "Connected network mappings could not be read for the current Windows user.")
    found = []
    try:
        for _ in range(8):
            buffer = ctypes.create_string_buffer(65536)
            count, size = wintypes.DWORD(64), wintypes.DWORD(len(buffer))
            result = mpr.WNetEnumResourceW(handle, ctypes.byref(count), buffer, ctypes.byref(size))
            if result == 259:
                break
            if result != 0:
                _fail("DISCOVERY_UNAVAILABLE", "The connected resource inventory could not be read completely.")
            rows = ctypes.cast(buffer, ctypes.POINTER(Resource))
            for index in range(min(count.value, 64)):
                row = rows[index]
                if row.remote:
                    found.append({"source": row.local + "\\" if row.local else row.remote,
                                  "remote": row.remote, "name": ntpath.basename(row.remote.rstrip("\\/")),
                                  "discovery": "current_user_connected_resource"})
        else:
            _fail("DISCOVERY_LIMIT", "Connected resources exceeded the bounded discovery limit; select a known source explicitly.")
        return found
    finally:
        mpr.WNetCloseEnum(handle)


def _sync_roots():
    roots = [Path(value) for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
             if (value := os.environ.get(name))]
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts") as accounts:
                for index in range(min(winreg.QueryInfoKey(accounts)[0], 64)):
                    try:
                        with winreg.OpenKey(accounts, winreg.EnumKey(accounts, index)) as account:
                            value, kind = winreg.QueryValueEx(account, "UserFolder")
                            if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                                roots.append(Path(os.path.expandvars(value)))
                    except OSError:
                        continue
        except OSError:
            pass
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "CloudStorage"
        try:
            with os.scandir(folder) as entries:
                for index, entry in enumerate(entries):
                    if index >= 64:
                        break
                    if entry.name.startswith("OneDrive") and entry.is_dir(follow_symlinks=False):
                        roots.append(Path(entry.path))
        except OSError:
            pass
    return roots


def probe():
    candidates, warnings = [], []
    try:
        candidates.extend(_connected_windows_resources())
    except DriveError as exc:
        warnings.append({"code": exc.code, "message": exc.message})
    # Only inspect known names under known sync roots; no recursive folder scan.
    for parent in [Path.home()] + _sync_roots():
        for path in [parent] + [parent / name for name in TEAM_NAMES]:
            try:
                if path.name.casefold() not in {name.casefold() for name in TEAM_NAMES} or not path.is_dir() or path.is_symlink():
                    continue
            except OSError:
                continue
            candidates.append({"source": str(path), "name": path.name,
                               "discovery": "known_local_or_synced_team_directory"})
    unique = {}
    for candidate in candidates:
        key = os.path.normcase(candidate["source"])
        if key in unique:
            continue
        candidate["preferred"] = candidate["name"].casefold() in {name.casefold() for name in TEAM_NAMES}
        try:
            candidate["classification"] = classify(candidate["source"])
        except DriveError as exc:
            candidate["error"] = {"code": exc.code, "message": exc.message}
        unique[key] = candidate
    result = sorted(unique.values(), key=lambda item: (not item["preferred"], item["source"].casefold()))
    preferred = [item for item in result if item["preferred"] and "classification" in item]
    return {"ok": True, "platform": sys.platform, "python": list(sys.version_info[:3]),
            "choices": [{"id": "team", "label": "A 团队公盘 / 云盘"}, {"id": "local", "label": "B 本地目录"}],
            "default_state_base": str(default_state_base()), "candidates": result,
            "suggested_source": preferred[0]["source"] if len(preferred) == 1 and not warnings else None,
            "selection_required": len(preferred) != 1 or bool(warnings), "warnings": warnings,
            "discovery_scope": "current_user_connected_resources_and_known_team_directory_names",
            "document_contents_read": False, "files_created": False}


def profiles(state_dir=None):
    """Inspect bounded immediate profile configs, without opening source/auth/state."""
    base = _base(state_dir)
    directory = _local_path(base / "profiles")
    found, warnings, truncated = [], [], False
    if not directory.exists():
        return {"ok": True, "state_base": str(base), "profiles": [], "truncated": False,
                "warnings": [], "sources_checked": False, "files_created": False}
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 128:
                truncated = True
                break
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                profile = _local_path(entry.path)
                config_path = profile / "config.json"
                config = _private_config(config_path)
                if config is None:
                    continue
                provider = config.get("provider", "local")
                if provider not in ("local", "windows_share", "microsoft_graph"):
                    raise ValueError()
                root, state = config.get("root"), config.get("state_dir")
                if not isinstance(root, str) or not root or not isinstance(state, str) or not state:
                    raise ValueError()
                state_path = Path(state).expanduser()
                if not state_path.is_absolute():
                    state_path = profile / state_path
                # Report paths only; do not open an index, source or auth cache.
                if state_path.absolute() != profile / "state":
                    raise ValueError()
                source = root
                if provider == "microsoft_graph":
                    graph = config.get("graph", {})
                    if not isinstance(graph, dict):
                        raise ValueError()
                    for key in ("drive_id", "folder_id"):
                        item = graph.get(key)
                        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9!._~-]{1,256}", item) or item in (".", "..", "root"):
                            raise ValueError()
                    source = {"drive_id": graph["drive_id"], "folder_id": graph["folder_id"]}
                found.append({"provider": provider, "source": source, "config_path": str(config_path),
                              "state_path": str(state_path), "source_availability": "not_checked"})
            except (DriveError, OSError, ValueError, TypeError):
                warnings.append({"code": "PROFILE_SKIPPED", "message": "An unsafe or invalid profile configuration was skipped."})
    return {"ok": True, "state_base": str(base), "profiles": sorted(found, key=lambda item: item["config_path"]),
            "truncated": truncated, "warnings": warnings, "sources_checked": False, "files_created": False}


def account_plan(tenant_id, client_id, scopes=None, state_dir=None):
    from .graph_auth import validate_auth
    auth = validate_auth({"tenant_id": tenant_id, "client_id": client_id,
                          "scopes": ["Files.Read"] if scopes is None else scopes})
    base = _base(state_dir)
    profile = base / "accounts" / _digest(auth)[:24]
    result = {"auth_config": auth, "auth_cache": str(profile / "auth.json"),
              "account_config_path": str(profile / "config.json"), "profile": str(profile), "base": str(base)}
    for key in ("profile", "auth_cache", "account_config_path"):
        _local_path(result[key])
    _directory_path(profile, private=True)
    return result


def _private_config(path):
    from .graph_auth import _CacheDirectory
    try:
        directory = _CacheDirectory(path)
    except FileNotFoundError:
        return None
    except DriveError as exc:
        if exc.code == "AUTH_CACHE_UNSAFE":
            _fail("STATE_PATH_UNSAFE", "The local configuration directory failed its path or access checks.")
        raise
    try:
        try:
            descriptor = directory.open(Path(path).name)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(MAX_JSON_BYTES + 1)
        directory.check()
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError()
        result = loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        _fail("INVALID_CONFIG", "The existing private configuration is invalid; it was not changed.")
    finally:
        directory.close()


def _save_config(path, config):
    """Exclusive creation inside a pinned private directory; never overwrite."""
    from .graph_auth import _CacheDirectory
    try:
        directory = _CacheDirectory(path, create=True)
    except DriveError as exc:
        if exc.code == "AUTH_CACHE_UNSAFE":
            _fail("STATE_PATH_UNSAFE", "The local configuration directory failed its path or access checks.")
        raise
    try:
        directory.check()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if os.name == "nt":
            descriptor = os.open(directory.path, flags, 0o600)
        else:
            descriptor = os.open(Path(path).name, flags, 0o600, dir_fd=directory.fd)
        with os.fdopen(descriptor, "wb") as target:
            target.write((_json(config) + "\n").encode("utf-8"))
            target.flush()
            os.fsync(target.fileno())
        directory.check()
        return Path(directory.path)
    finally:
        directory.close()


def cloud_login(tenant_id, client_id, scopes=None, state_dir=None, confirmed=False, emit=None):
    if confirmed is not True:
        _fail("CONFIRMATION_REQUIRED", "Starting cloud login requires explicit confirmation.")
    from .graph_auth import device_login
    plan = account_plan(tenant_id, client_id, scopes, state_dir)
    config = {"graph": dict(plan["auth_config"], auth_cache=plan["auth_cache"])}
    existing = _private_config(plan["account_config_path"])
    if existing is not None and existing != config:
        _fail("CONFIG_BINDING_MISMATCH", "This account profile has a different configuration; nothing was overwritten.")
    if existing is None:
        try:
            _save_config(plan["account_config_path"], config)
        except FileExistsError:
            if _private_config(plan["account_config_path"]) != config:
                _fail("CONFIG_BINDING_MISMATCH", "A different account configuration was created concurrently.")
    result = device_login(plan["auth_config"], plan["auth_cache"], emit or (lambda value: None))
    return dict(result, ok=True, account_config_path=plan["account_config_path"], auth_cache=plan["auth_cache"],
                source_configured=False, index_created=False)


def _source_check(source, provider):
    if provider == "windows_share":
        from .network_source import WindowsShareSource
        access = WindowsShareSource(source)
    else:
        from .fs_access import SourceAccess
        access = SourceAccess(source)
    try:
        access.check_root()
        identity = (dict(access.binding) if provider == "windows_share" else
                    {"provider": "local", "device": access.identity[0], "inode": access.identity[1]})
        return {"root": str(access.root), "identity": identity}
    finally:
        access.close()


def _selection(value):
    required = {"provider", "drive_id", "folder_id", "name", "web_url"}
    if isinstance(value, dict) and value.get("ok") is True:
        value = {key: item for key, item in value.items() if key != "ok"}
    if not isinstance(value, dict) or set(value) != required or value.get("provider") != "microsoft_graph":
        _fail("INVALID_SELECTION", "Use the folder selection returned by the authenticated cloud discovery command.")
    if not isinstance(value["name"], str) or not value["name"] or len(value["name"]) > 1024:
        _fail("INVALID_SELECTION", "The cloud folder display name is invalid.")
    if classify(value["web_url"])["provider"] != "microsoft_graph":
        _fail("INVALID_SELECTION", "The cloud selection must contain a direct OneDrive or SharePoint URL.")
    return dict(value)


def make_plan(choice, source=None, *, selection=None, tenant_id=None, client_id=None, scopes=None, state_dir=None):
    if choice not in ("team", "local"):
        _fail("INVALID_CHOICE", "Choose team (A) or local (B).")
    base = _base(state_dir)
    account = principal = resolved_root = source_identity = None
    if selection is not None:
        if choice != "team" or source is not None:
            _fail("INVALID_SELECTION", "A cloud selection belongs to team (A); do not supply a second source.")
        selection = _selection(selection)
        account = account_plan(tenant_id, client_id, scopes, str(base))
        from .graph_auth import TokenProvider
        principal = TokenProvider(account["auth_config"], account["auth_cache"]).account_binding
        if not principal:
            _fail("AUTH_REQUIRED", "Authorize the cloud account explicitly before planning a folder.",
                  "Run cloud-login, then select a folder with df-cloud.")
        from .graph_config import graph_settings
        graph = graph_settings(dict(account["auth_config"], drive_id=selection["drive_id"],
                                    folder_id=selection["folder_id"], auth_cache=account["auth_cache"]))
        provider, source = "microsoft_graph", selection["web_url"]
        binding = {"provider": provider, "tenant_id": graph["tenant_id"], "client_id": graph["client_id"],
                   "drive_id": graph["drive_id"], "folder_id": graph["folder_id"], "account_binding": principal}
    else:
        if any(value is not None for value in (tenant_id, client_id, scopes)):
            _fail("INVALID_SELECTION", "Cloud account settings require an authenticated cloud folder selection.")
        if source is None and choice == "team":
            source = probe()["suggested_source"]
        if source is None:
            _fail("SOURCE_SELECTION_REQUIRED", "Choose a source from the detected candidates or provide its directory path.")
        kind = classify(source)
        if kind["provider"] == "microsoft_graph":
            _fail("CLOUD_SELECTION_REQUIRED", "Authorize your cloud account and resolve this URL before creating a plan.",
                  "Run cloud-login and df-cloud resolve; use its folder selection with plan --selection-file.")
        if not kind["supported_on_this_platform"]:
            _fail("UNSUPPORTED_PLATFORM", "This source must be configured on Windows to verify the current user's drive mapping.")
        provider = kind["provider"]
        if choice == "local" and provider != "local":
            _fail("INVALID_CHOICE", "A network share belongs to team (A), not local (B).")
        checked = _source_check(kind["source"], provider)
        root, source_identity = checked["root"], checked["identity"]
        # Keep Windows share syntax; Path.resolve would misclassify mapped/UNC roots.
        source = kind["source"] if provider == "windows_share" else root
        if provider == "local" and (overlaps(Path(root), base.resolve()) or overlaps(Path(root), installation_root())):
            _fail("UNSAFE_ROOT", "The source, private state and installation must not overlap.")
        binding = {"provider": provider, "root": source, "source_identity": source_identity}
        if provider == "windows_share":
            resolved_root = root
            binding["resolved_root"] = root
    profile = base / "profiles" / (provider + "-" + _digest(binding)[:24])
    config = {"provider": provider, "root": str(profile / "cloud-anchor") if account else source,
              "state_dir": str(profile / "state"), "poll_seconds": 300 if provider != "local" else 30}
    if account:
        config["graph"] = graph
    else:
        config["approved_source_identity"] = source_identity
    for path in (profile, profile / "config.json", profile / "state"):
        _local_path(path)
    _directory_path(profile, private=True)
    _directory_path(profile / "state", private=True)
    if account:
        _directory_path(config["root"], private=True, empty=True)
        from .graph_config import validate_graph_paths
        validate_graph_paths(config)
    request = {"choice": choice, "source": None if account else source, "selection": selection,
               "tenant_id": account["auth_config"]["tenant_id"] if account else None,
               "client_id": account["auth_config"]["client_id"] if account else None,
               "scopes": account["auth_config"]["scopes"] if account else None, "state_dir": str(base)}
    plan = {"version": 1, "request": request, "config": config, "config_path": str(profile / "config.json"),
            "account_binding": principal, "source": source, "resolved_root": resolved_root,
            "source_identity": source_identity, "index_created": False}
    plan["plan_id"] = _digest(plan)
    return plan


def _verify_cloud(plan):
    from .graph_auth import TokenProvider
    from .graph_discovery import GraphDiscovery
    graph = plan["config"]["graph"]
    provider = TokenProvider(graph, graph["auth_cache"])
    if provider.account_binding != plan["account_binding"]:
        _fail("AUTH_BINDING_MISMATCH", "The signed-in account changed; review a new folder plan.")
    actual = GraphDiscovery(provider).get_folder(graph["drive_id"], graph["folder_id"])
    if provider.account_binding != plan["account_binding"]:
        _fail("AUTH_BINDING_MISMATCH", "The signed-in account changed; review a new folder plan.")
    if any(actual.get(key) != plan["request"]["selection"][key] for key in ("provider", "drive_id", "folder_id", "name", "web_url")):
        _fail("SOURCE_CHANGED", "The selected cloud folder changed; review a new plan before applying.")


def apply_plan(plan, confirmed=False):
    if confirmed is not True:
        _fail("CONFIRMATION_REQUIRED", "Saving this source requires explicit confirmation of its plan.")
    if not isinstance(plan, dict) or set(plan) != {"version", "request", "config", "config_path", "account_binding", "source", "resolved_root", "source_identity", "index_created", "plan_id"}:
        _fail("INVALID_PLAN", "Use the complete unmodified plan JSON.")
    request = plan.get("request")
    expected_keys = {"choice", "source", "selection", "tenant_id", "client_id", "scopes", "state_dir"}
    if not isinstance(request, dict) or set(request) != expected_keys:
        _fail("INVALID_PLAN", "The plan request is invalid.")
    fresh = make_plan(**request)
    if fresh != plan:
        _fail("PLAN_CHANGED", "The plan was modified or its source/account changed; review a new plan.")
    config, path = plan["config"], Path(plan["config_path"])
    existing = _private_config(path)
    if existing is not None:
        from .service_config import settings
        normalized = settings(path)["config"]
        same_path = lambda a, b: isinstance(a, str) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
        if (normalized.get("provider") != config["provider"] or
                any(not same_path(normalized.get(key), config[key]) for key in ("root", "state_dir"))):
            _fail("CONFIG_BINDING_MISMATCH", "This profile is bound to a different source or state; nothing was overwritten.")
        if config["provider"] != "microsoft_graph" and normalized.get("approved_source_identity") != config["approved_source_identity"]:
            _fail("CONFIG_BINDING_MISMATCH", "This profile lacks the confirmed source identity or has a different one; nothing was overwritten.")
        if config["provider"] == "microsoft_graph" and any(normalized["graph"].get(key) != config["graph"][key]
                for key in ("tenant_id", "client_id", "drive_id", "folder_id", "auth_cache", "scopes")):
            _fail("CONFIG_BINDING_MISMATCH", "This profile is bound to a different cloud account or folder.")
    if config["provider"] == "microsoft_graph":
        _verify_cloud(plan)
        anchor = Path(config["root"])
        if anchor.exists() and (not anchor.is_dir() or next(anchor.iterdir(), None) is not None):
            _fail("UNSAFE_ROOT", "The cloud anchor must be an empty ordinary local directory.")
        if existing is not None and not anchor.exists():
            _fail("UNSAFE_ROOT", "The existing cloud anchor is missing; restore it or select a new profile.")
        if existing is None:
            from .graph_auth import _CacheDirectory
            guard = _CacheDirectory(anchor / "placeholder", create=True)
            guard.close()  # Creates only the empty anchor, never a placeholder file.
    reused = existing is not None
    if not reused:
        try:
            path = _save_config(path, config)
        except FileExistsError:
            _fail("CONFIG_CHANGED", "A configuration was created concurrently; run apply again to validate and reuse it.")
    return {"ok": True, "config_path": str(path), "provider": config["provider"], "source": plan["source"],
            "reused": reused, "index_created": False, "source_files_modified": False,
            "next_action": "Use df-tool with this config path to inspect status and initialize the source index."}


def read_json(path):
    with Path(path).expanduser().open("rb") as source:
        raw = source.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        _fail("INVALID_INPUT", "JSON input must not exceed 256 KiB.")
    return loads(raw)


def main(argv=None):
    class JSONParser(argparse.ArgumentParser):
        def error(self, message):
            _fail("INVALID_ARGUMENT", "Use the documented onboarding command and required arguments.")

    parser = JSONParser(description="Python-only, confirmed team/local source onboarding.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe")
    profiles_parser = commands.add_parser("profiles")
    profiles_parser.add_argument("--state-dir")
    classify_parser = commands.add_parser("classify")
    classify_parser.add_argument("--source", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--choice", choices=("team", "local"), required=True)
    plan_parser.add_argument("--source")
    plan_parser.add_argument("--selection-file")
    login_parser = commands.add_parser("cloud-login")
    login_parser.add_argument("--confirmed", action="store_true")
    for command in (plan_parser, login_parser):
        command.add_argument("--tenant-id", required=command is login_parser)
        command.add_argument("--client-id", required=command is login_parser)
        command.add_argument("--scope", action="append", dest="scopes")
        command.add_argument("--state-dir")
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--confirmed", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.command == "probe":
            result = probe()
        elif args.command == "profiles":
            result = profiles(args.state_dir)
        elif args.command == "classify":
            result = dict(classify(args.source), ok=True)
        elif args.command == "cloud-login":
            result = cloud_login(args.tenant_id, args.client_id, args.scopes, args.state_dir, args.confirmed,
                                 emit=lambda value: print(_json(value), flush=True))
        elif args.command == "plan":
            selection = read_json(args.selection_file) if args.selection_file else None
            result = make_plan(args.choice, args.source, selection=selection, tenant_id=args.tenant_id,
                               client_id=args.client_id, scopes=args.scopes, state_dir=args.state_dir)
        else:
            result = apply_plan(read_json(args.plan), args.confirmed)
        print(_json(result), flush=True)
        return 0
    except KeyboardInterrupt:
        print(_json({"ok": False, "error": {"code": "CANCELLED", "message": "Onboarding was cancelled."}}), flush=True)
        return 130
    except DriveError as exc:
        print(_json({"ok": False, "error": {"code": exc.code, "message": exc.message,
                                               "next_action": exc.next_action}}), flush=True)
        return 1
    except (OSError, ValueError, TypeError, RuntimeError):
        print(_json({"ok": False, "error": {"code": "INVALID_INPUT", "message": "The input or local configuration could not be validated safely."}}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
