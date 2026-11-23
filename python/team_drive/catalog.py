"""Read-only source selection over existing profiles; never starts a reader."""
import argparse
import json
import re

from .errors import DriveError
from .onboarding import profiles

SCHEMA = "df-source-catalog/1"


def catalog(state_dir=None):
    result = profiles(state_dir)
    return dict(result, schema_version=SCHEMA,
                selection_policy="explicit_profile_id",
                business_completeness="not_established")


def select(profile_id, state_dir=None, expected_fingerprint=None):
    if not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", profile_id):
        raise DriveError("INVALID_PROFILE", "Use an exact profile_id returned by the source catalog.")
    if expected_fingerprint is not None and (not isinstance(expected_fingerprint, str) or
                                             not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint)):
        raise DriveError("INVALID_PROFILE", "Use the configuration fingerprint returned by the catalog.")
    result = catalog(state_dir)
    matches = [item for item in result["profiles"] if item["profile_id"] == profile_id]
    if len(matches) != 1:
        code = "CATALOG_INCOMPLETE" if result["truncated"] or result["warnings"] else "PROFILE_NOT_FOUND"
        raise DriveError(code, "The requested profile was not uniquely found in the inspected catalog.", False,
                         "Inspect the catalog warnings and select an explicitly configured source; do not substitute another profile.")
    chosen = matches[0]
    if expected_fingerprint is not None and expected_fingerprint != chosen["config_fingerprint"]:
        raise DriveError("CONFIG_CHANGED", "The selected source configuration changed since it was reviewed.", False,
                         "Inspect the source catalog again before connecting.")
    return {"ok": True, "schema_version": SCHEMA, "selected": chosen,
            "sources_checked": False, "files_created": False,
            "business_completeness": "not_established"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("list", "select"))
    parser.add_argument("--state-dir")
    parser.add_argument("--profile-id")
    parser.add_argument("--config-fingerprint")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            if args.profile_id is not None or args.config_fingerprint is not None:
                raise DriveError("INVALID_PARAMS", "Profile selectors apply to select, not list.")
            result = catalog(args.state_dir)
        else:
            result = select(args.profile_id, args.state_dir, args.config_fingerprint)
        code = 0
    except DriveError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": exc.message,
                                         "retryable": exc.retryable, "next_action": exc.next_action}}
        code = 1
    except (OSError, ValueError, TypeError):
        result = {"ok": False, "error": {"code": "CATALOG_UNAVAILABLE",
                                         "message": "The source catalog could not be inspected."}}
        code = 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code
