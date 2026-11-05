"""Offline, explicit Microsoft Graph source configuration (no authentication)."""
import math
from pathlib import Path
import re

from .errors import DriveError
from .fs_access import overlaps, validate_state_location


GRAPH_LIMITS = {"timeout_seconds": (10, 0.1, 20, False),
                "max_items": (10000, 1, 100000, True),
                "max_pages": (1000, 1, 10000, True),
                "max_depth": (32, 1, 128, True),
                "scan_budget_seconds": (120, 1, 600, False)}
AUTH_KEYS = {"tenant_id", "client_id", "scopes", "token_env", "auth_cache"}


def graph_settings(value, config_dir=None):
    from .graph_auth import validate_auth
    if not isinstance(value, dict) or set(value) - (AUTH_KEYS | set(GRAPH_LIMITS) | {"drive_id", "folder_id"}):
        raise DriveError("INVALID_CONFIG", "graph must contain supported Microsoft Graph source settings.")
    result = dict(value)
    for key in ("drive_id", "folder_id"):
        item = value.get(key)
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9!._~-]{1,256}", item) or item in (".", "..", "root"):
            raise DriveError("INVALID_CONFIG", "Use explicit drive_id and folder_id identifiers, not URLs or paths.")
    result.update(validate_auth(value))
    for key, (default, minimum, maximum, integer) in GRAPH_LIMITS.items():
        item = value.get(key, default)
        if (isinstance(item, bool) or not isinstance(item, (int, float)) or
                not minimum <= item <= maximum or not math.isfinite(item) or
                (integer and not isinstance(item, int))):
            raise DriveError("INVALID_CONFIG", "Graph request and inventory limits must be bounded numbers.")
        result[key] = item
    cache = value.get("auth_cache")
    if "auth_cache" in value:
        if not isinstance(cache, str) or not cache.strip() or "\0" in cache:
            raise DriveError("INVALID_CONFIG", "graph.auth_cache must be a private local path.")
        path = Path(cache).expanduser()
        if config_dir is not None and not path.is_absolute():
            path = Path(config_dir) / path
        result["auth_cache"] = str(path.absolute())
        validate_state_location(path)
    elif not result.get("token_env"):
        raise DriveError("INVALID_CONFIG", "Configure a private auth_cache or an explicit token_env name.")
    return result


def validate_graph_paths(config):
    """The anchor, state and credentials are local infrastructure, never cloud paths."""
    anchor = Path(config["root"]).expanduser().absolute()
    state = Path(config["state_dir"]).expanduser().absolute()
    validate_state_location(anchor)
    validate_state_location(state)
    if overlaps(anchor.resolve(), state.resolve()):
        raise DriveError("INVALID_CONFIG", "The cloud anchor and index state must be separate directories.")
    cache = config["graph"].get("auth_cache")
    if cache:
        path = Path(cache).resolve()
        installation = Path(__file__).resolve().parents[2]
        if any(overlaps(path, parent.resolve()) for parent in (anchor, state, installation)):
            raise DriveError("INVALID_CONFIG", "Keep cloud credentials outside the anchor, index and installation.")


def source_binding(config):
    return {key: config[key] for key in ("tenant_id", "client_id", "drive_id", "folder_id")}
