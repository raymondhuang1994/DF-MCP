"""Explicit lease-protected maintenance for shared and legacy index storage."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import time
from types import SimpleNamespace

from .errors import DriveError
from .fs_access import DATABASE_FILES, SourceAccess, StateAccess, validate_state_boundaries, validate_state_location
from .state_pool import MAX_SLOTS, pool_settings, slot_path

INDEX_FILES = DATABASE_FILES


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise DriveError("UNSAFE_STATE", "Maintenance refuses redirected or multiply linked files.")
    return info


def _verify_profile(storage, source_missing, accept_replacement):
    manifest = storage.read_metadata("profile.json")
    if manifest is None:
        if source_missing:
            raise DriveError("ORPHAN_STATE_UNVERIFIED", "Missing-source cleanup requires a previously recorded profile manifest.")
        return False
    expected = storage.profile_manifest()
    if any(manifest.get(key) != expected[key] for key in ("format", "version", "root")):
        raise DriveError("STATE_ROOT_MISMATCH", "The stored profile does not match this configured source path.")
    recorded_identity = manifest.get("root_identity")
    if not isinstance(recorded_identity, list) or len(recorded_identity) != 2 or any(not isinstance(item, int) for item in recorded_identity):
        raise DriveError("INVALID_STATE_METADATA", "The stored source identity is invalid.")
    changed = not source_missing and recorded_identity != expected["root_identity"]
    if changed and not accept_replacement:
        raise DriveError("STATE_ROOT_MISMATCH", "The source directory identity has changed.", False,
                         "Review the replacement, then use --reset-index --accept-source-replacement explicitly.")
    return changed


def _bundle_exports(storage):
    """Retained evidence manifests pin their exports during age-based cleanup."""
    pinned = set()
    if not os.path.lexists(storage.path / "bundles"):
        return pinned
    with storage.directory("bundles") as directory:
        for path in directory.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue
            with storage.open_private(path, "r") as manifest_file:
                text = manifest_file.read(1024 * 1024 + 1)
            try:
                manifest = json.loads(text)
                if len(text) > 1024 * 1024 or manifest.get("schema") != "df-mcp-evidence-bundle-v1" or not isinstance(manifest.get("files"), list):
                    raise ValueError()
                for entry in manifest["files"]:
                    if not isinstance(entry, dict):
                        raise ValueError()
                    artifact = entry.get("artifact_id")
                    if artifact is not None:
                        if not isinstance(artifact, str) or not re.fullmatch(r"exports/[0-9a-f]{32}\.jsonl", artifact):
                            raise ValueError()
                        pinned.add(artifact.split("/")[1])
            except (ValueError, AttributeError):
                raise DriveError("BUNDLE_REFERENCES_UNVERIFIED", "An evidence manifest could not be verified; export cleanup was refused.")
    return pinned


def maintain(config_path, apply=False, reset_index=False, prune_exports_days=None, accept_source_replacement=False):
    """Preview by default. Lease control before index; never delete recursively."""
    config_file = Path(config_path).expanduser().resolve(strict=True)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("provider", "local") not in ("local", "microsoft_graph", "windows_share"):
        raise DriveError("INVALID_CONFIG", "Use an explicit local, Windows share or Microsoft Graph source configuration.")
    for key in ("root", "state_dir"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise DriveError("INVALID_CONFIG", "root and state_dir must be nonempty paths.")
    if accept_source_replacement and not reset_index:
        raise DriveError("INVALID_CONFIG", "Accepting a replaced source requires --reset-index.")
    if prune_exports_days is not None and (isinstance(prune_exports_days, bool) or not isinstance(prune_exports_days, int) or prune_exports_days < 0):
        raise DriveError("INVALID_CONFIG", "Export retention days must be a nonnegative integer.")
    def configured_path(key):
        value = Path(config[key]).expanduser()
        return value if value.is_absolute() else config_file.parent / value
    base = configured_path("state_dir")
    validate_state_location(base)
    pool_settings(config)  # Validate config, but enumerate all historical slots.
    root = configured_path("root")
    network = config.get("provider") == "windows_share"
    # An unavailable share is not an orphaned source. Refuse maintenance until
    # access can be verified, rather than accepting a new remote identity.
    if network:
        source = SourceAccess(root, allow_network=True)
        source_missing = False
    else:
        source_missing = not root.exists()
        source = SimpleNamespace(root=root.resolve(), identity=None) if source_missing else SourceAccess(root)
    installation = Path(__file__).resolve().parents[2]
    results = []
    try:
        base = validate_state_boundaries(base, source, installation)
        candidates = [(number, "pool", slot_path(base, number), None) for number in range(1, MAX_SLOTS + 1)]
        candidates.append(("shared", "shared", base / "shared-v1" / "index", base / "shared-v1" / "control"))
        for slot, mode, path, control_path in candidates:
            if not os.path.lexists(path):
                continue
            storage = None
            control = None
            row = {"slot": slot, "mode": mode, "status": "preview", "index_bytes": 0, "export_bytes": 0,
                   "export_files": 0, "scratch": None, "source_missing": source_missing}
            try:
                if control_path is not None:
                    # Broker startup owns control before opening index. Maintenance
                    # takes leases in the same order and cannot reset a live broker.
                    control = StateAccess(control_path, source, installation)
                    control.lock()
                storage = StateAccess(path, source, installation)
                storage.lock()
                replaced = _verify_profile(storage, source_missing, accept_source_replacement)
                row["source_replacement_accepted"] = replaced and accept_source_replacement
                targets = []
                for name in INDEX_FILES:
                    target = storage.path / name
                    if os.path.lexists(target):
                        row["index_bytes"] += regular(target).st_size
                        targets.append(name)
                if reset_index and apply:
                    for name in targets:
                        storage.unlink_root(name)
                    if replaced:
                        storage.write_metadata("profile.json", storage.profile_manifest())
                if prune_exports_days is not None and os.path.lexists(storage.path / "exports"):
                    cutoff = time.time() - prune_exports_days * 86400
                    pinned_exports = _bundle_exports(storage)
                    row["bundle_exports_preserved"] = len(pinned_exports)
                    with storage.directory("exports") as directory:
                        for target in directory.iterdir():
                            if not re.fullmatch(r"[0-9a-f]{32}\.jsonl", target.name) or target.name in pinned_exports:
                                continue
                            info = regular(target)
                            if info.st_mtime > cutoff:
                                continue
                            row["export_files"] += 1
                            row["export_bytes"] += info.st_size
                            if apply:
                                storage.unlink_private(target)
                row["scratch"] = storage.cleanup_scratch(dry_run=not apply)
                row["status"] = "applied" if apply else "preview"
            except DriveError as exc:
                row["status"] = "busy" if exc.code == "STATE_IN_USE" else "error"
                row["error"] = {"code": exc.code, "message": exc.message}
            except OSError:
                row["status"] = "error"
                row["error"] = {"code": "MAINTENANCE_FAILED", "message": "A guarded state operation could not complete."}
            finally:
                if storage is not None:
                    storage.close()
                if control is not None:
                    control.close()
            results.append(row)
        return {"ok": all(row["status"] != "error" for row in results), "applied": apply,
                "slots": results, "busy_slots_skipped": sum(row["status"] == "busy" for row in results),
                "scope": "Existing shared index and all legacy pool slots (1..32). Sources, unknown files, bundles, and directories are preserved."}
    finally:
        if not source_missing:
            source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="Apply maintenance; default is preview only.")
    parser.add_argument("--reset-index", action="store_true", help="Remove idle derived databases for rebuilding or reclaiming disk space.")
    parser.add_argument("--accept-source-replacement", action="store_true", help="With --reset-index, explicitly accept a new source identity at the configured path.")
    parser.add_argument("--prune-exports-days", type=int, help="Remove generated exports older than this many days; preserve exports by default.")
    args = parser.parse_args()
    try:
        result = maintain(args.config, args.apply, args.reset_index, args.prune_exports_days, args.accept_source_replacement)
    except DriveError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
    except (OSError, ValueError, TypeError):
        result = {"ok": False, "error": {"code": "MAINTENANCE_FAILED", "message": "Maintenance could not complete; check configuration and state permissions."}}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
