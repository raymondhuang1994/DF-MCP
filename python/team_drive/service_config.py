"""Normalized, bounded settings shared by local broker and stdio bridge."""
import hashlib
import json
import math
import os
from pathlib import Path

from .errors import DriveError
from .engine import DEFAULT_LIMITS
from .fs_access import validate_state_location
from .retrieval import validate_config as retrieval_settings
from .diagnostics import settings as diagnostic_settings

SERVICE_VERSION = "0.7.0"
PROTOCOL_VERSION = 1


def settings(config_path):
    try:
        path = Path(config_path).expanduser().resolve(strict=True)
        with path.open("rb") as source:
            raw_config = source.read(262145)
        if len(raw_config) > 262144:
            raise ValueError()
        config = json.loads(raw_config.decode("utf-8"))
        if not isinstance(config, dict) or config.get("provider", "local") not in ("local", "microsoft_graph", "windows_share"):
            raise ValueError()
        for key in ("root", "state_dir"):
            if not isinstance(config.get(key), str) or not config[key]:
                raise ValueError()
            if key == "root" and config.get("provider") == "windows_share":
                from .network_source import normalize_share_path
                config[key] = normalize_share_path(config[key])
                continue
            raw = Path(config[key]).expanduser()
            config[key] = str((raw if raw.is_absolute() else path.parent / raw).absolute())
        validate_state_location(config["state_dir"])
        service = config.get("service", {})
        if not isinstance(service, dict) or set(service) - {"idle_seconds", "max_clients"}:
            raise ValueError()
        idle = service.get("idle_seconds", 60)
        clients = service.get("max_clients", 64)
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not 1 <= idle <= 3600 or not math.isfinite(idle):
            raise ValueError()
        if isinstance(clients, bool) or not isinstance(clients, int) or not 1 <= clients <= 256:
            raise ValueError()
        poll = config.get("poll_seconds", 30)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 1 <= poll <= 86400 or not math.isfinite(poll):
            raise ValueError()
        limits = config.get("limits", {})
        if not isinstance(limits, dict) or any(key not in DEFAULT_LIMITS or isinstance(value, bool) or not isinstance(value, int) or value < 1 for key, value in limits.items()):
            raise ValueError()
        aliases = config.get("aliases", {})
        if not isinstance(aliases, dict) or any(not isinstance(key, str) or not isinstance(values, list) or any(not isinstance(value, str) for value in values) for key, values in aliases.items()):
            raise ValueError()
        for key in ("retrieval", "diagnostics"):
            if key in config and not isinstance(config[key], dict):
                raise ValueError()
        effective = dict(config, provider=config.get("provider", "local"), poll_seconds=float(poll), limits=dict(DEFAULT_LIMITS, **limits),
                         aliases={key: sorted(set(values)) for key, values in aliases.items()},
                         retrieval=retrieval_settings(config.get("retrieval", {})),
                         diagnostics=diagnostic_settings(config.get("diagnostics")),
                         service={"idle_seconds": float(idle), "max_clients": clients})
        effective.pop("state_pool", None)  # A legacy pool setting cannot create extra shared indexes.
        effective["root"] = (config["root"] if effective["provider"] == "windows_share" else
                             os.path.normcase(str(Path(config["root"]).resolve())))
        effective["state_dir"] = os.path.normcase(str(Path(config["state_dir"]).resolve()))
        if effective["provider"] == "microsoft_graph":
            from .graph_config import graph_settings, validate_graph_paths
            effective["graph"] = graph_settings(config.get("graph"), path.parent)
            validate_graph_paths(effective)
        elif "graph" in config:
            raise ValueError()
        if effective["provider"] == "windows_share":
            from .network_source import share_settings
            effective["windows_share"] = share_settings(config.get("windows_share"))
        elif "windows_share" in config:
            raise ValueError()
        binding = json.dumps(effective, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        code = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob("*.py")):
            code.update(source.name.encode())
            code.update(source.read_bytes())
        return {"path": path, "root": Path(config["root"]), "base": Path(config["state_dir"]),
                "config_id": hashlib.sha256(binding.encode()).hexdigest(), "code_id": code.hexdigest(),
                "idle_seconds": float(idle), "max_clients": clients, "config": effective}
    except (OSError, ValueError, TypeError, OverflowError):
        raise DriveError("INVALID_CONFIG", "Use a valid source configuration and bounded service settings.")
