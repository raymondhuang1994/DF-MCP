"""Bounded JSON CLI over the existing per-user broker and evidence engine.

Pagination stays on one connection: engine cursors must never be reused by a
later CLI process. No retry of side-effecting operations is performed here.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from .bounded_json import loads
from .bridge import Bridge
from .engine import failure
from .errors import DriveError

METHODS = ("status", "list", "search", "inspect", "read", "context", "plan",
           "diagnostics", "export", "evidence_bundle", "refresh", "job_status", "cancel",
           "wiki_status", "wiki_search", "wiki_read", "wiki_write", "wiki_refresh")
PAGED = {"list", "search", "inspect", "read"}
MAX_PARAMS_BYTES = 65536
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def parameters(raw=None, path=None):
    if raw is not None and path is not None:
        raise DriveError("INVALID_PARAMS", "Use either --params or --params-file.")
    if path is not None:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_PARAMS_BYTES + 1)
    else:
        data = (raw or "{}").encode("utf-8")
    if len(data) > MAX_PARAMS_BYTES:
        raise DriveError("REQUEST_TOO_LARGE", "Parameters exceed 64 KiB.")
    try:
        result = loads(data)
        if not isinstance(result, dict):
            raise ValueError()
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DriveError("INVALID_PARAMS", "Use a finite JSON object for parameters.")
    if result.get("cursor"):
        raise DriveError("CLI_CURSOR_EXPIRED", "Cursors belong to a single connection. Restart with --paginate.")
    return result


def collect(bridge, method, params, max_pages=100):
    if method not in PAGED:
        raise DriveError("INVALID_PARAMS", "--paginate is supported for list, search, inspect and read.")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise DriveError("INVALID_PARAMS", "max_pages must be between 1 and 1000.")
    params = dict(params)
    if method in ("list", "search"):
        status = bridge.call("status", {})
        if not status.get("ok"):
            return dict(status, readiness={"method": "status", "result": status})
        if status.get("current_job_id"):
            return dict(readiness_failure(status, "status", "INDEX_STILL_RUNNING", "Wait for the current batch before paginating the index.", True,
                                         "Track this job or use a bounded non-paginated query with explicit coverage."),
                        current_job_id=status["current_job_id"], coverage=status.get("coverage", {}),
                        pagination_complete=False, pages_discarded=0)
        if status.get("provider") == "windows_share":
            requested_limit = params.get("limit", 20 if method == "list" else 10)
            if isinstance(requested_limit, int) and not isinstance(requested_limit, bool) and 1 <= requested_limit <= 100:
                params["limit"] = min(requested_limit, 8)
    pages, size, cursor = [], 0, None
    for unused in range(max_pages):
        request = dict(params)
        if cursor is not None:
            request["cursor"] = cursor
        result = bridge.call(method, request)
        if not result.get("ok"):
            # Revocation/version failures must not turn previous pages into a
            # successful complete dataset. Discard them from this result.
            return dict(result, pagination_complete=False, pages_discarded=len(pages))
        cursor = result.get("next_cursor")
        page = dict(result)
        page.pop("next_cursor", None)
        page_size = len(json.dumps(page, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size + page_size > MAX_OUTPUT_BYTES - 4096:
            return {"ok": True, "pages": pages, "pagination_complete": False,
                    "stop_reason": "output_budget", "business_completeness": "not_established",
                    "next_action": "Narrow the scope or export the selected version; do not infer totals."}
        pages.append(page)
        size += page_size
        if not cursor:
            return {"ok": True, "pages": pages, "pagination_complete": True,
                    "business_completeness": "not_established"}
    return {"ok": True, "pages": pages, "pagination_complete": False,
            "stop_reason": "page_budget", "business_completeness": "not_established",
            "next_action": "Restart with a larger --max-pages or narrower scope; cursors are connection-local."}


def readiness_failure(result, method, code, message, retryable, next_action):
    """Keep the actual observation behind a CLI readiness decision intact."""
    return dict(failure(code, message, retryable, next_action),
                coverage=result.get("coverage", {}), index_status=result.get("index_status", {}),
                readiness={"method": method, "result": result})


def wait_ready(bridge, seconds):
    deadline = time.monotonic() + seconds
    while True:
        result = bridge.call("status", {})
        if not result.get("ok"):
            return dict(result, readiness={"method": "status", "result": result})
        observation, observation_method = result, "status"
        job_id = result.get("current_job_id")
        if job_id:
            job = bridge.call("job_status", {"job_id": job_id})
            if not job.get("ok"):
                return dict(job, readiness={"method": "job_status", "result": job})
            observation, observation_method = job, "job_status"
            state = job.get("job", {}).get("status")
            if state == "partial":
                return readiness_failure(job, "job_status", "INDEX_PARTIAL", "Verified batches are available, but some directories remain unscanned.", True,
                                         "Use ordinary queries with explicit coverage, or refresh without force to resume. Check job_status for remaining scopes.")
            if state in ("failed", "cancelled"):
                return readiness_failure(job, "job_status", "INDEX_NOT_READY", "The indexing job did not complete.", False,
                                         "Inspect job_status and the reported coverage gaps.")
            if state == "complete":
                result = bridge.call("status", {})
                if not result.get("ok"):
                    return dict(result, readiness={"method": "status", "result": result})
                observation, observation_method = result, "status"
                if not result.get("current_job_id"):
                    return usable_inventory(result)
        elif result.get("coverage", {}).get("inventory_complete") is True:
            return usable_inventory(result)
        elif result.get("index_status", {}).get("state") == "partial":
            return readiness_failure(result, "status", "INDEX_PARTIAL", "Verified batches are available; the inventory remains incomplete.", True,
                                     "Use bounded queries with explicit coverage, or refresh without force to resume.")
        elif result.get("index_status", {}).get("state") in ("blocked", "failed", "cancelled"):
            return readiness_failure(result, "status", "INDEX_NOT_READY", "The index requires recovery before it can be used.", False,
                                     "Inspect the reported runtime error and explicitly retry after repair.")
        if time.monotonic() >= deadline:
            return readiness_failure(observation, observation_method, "INDEX_STILL_RUNNING", "The bounded wait ended; indexing may still be running.", True,
                                     "Check status/job_status. Do not start another index or invent completion.")
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))


def usable_inventory(result):
    coverage = result.get("coverage", {})
    current = coverage.get("current_indexed_files", coverage.get("indexed_files", 0))
    if (result.get("index_status", {}).get("state") == "blocked" or
            (coverage.get("present_files", 0) > 0 and current == 0)):
        return readiness_failure(result, "status", "INDEX_NOT_READABLE", "No current extraction is available, or the index has a runtime blocker.", False,
                                 "Inspect coverage and pending_reparse; finish extraction or repair the runtime before calculating totals.")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="DF evidence tools: JSON output, read-only source, private local state.")
    parser.add_argument("method", choices=METHODS)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--config")
    sources.add_argument("--profile-id", help="Exact ID from df-sources.py; never selects another profile implicitly")
    parser.add_argument("--state-dir", help="Configured profile catalog base; only with --profile-id")
    parser.add_argument("--config-fingerprint", help="Configuration fingerprint previously reviewed in the source catalog")
    parser.add_argument("--evidence-envelope", action="store_true", help="Preserve the native result inside a source-bound evidence envelope")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--params")
    group.add_argument("--params-file")
    parser.add_argument("--paginate", action="store_true")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--wait-ready", type=float, default=0, metavar="SECONDS")
    args = parser.parse_args(argv)
    bridge = None
    selected = None
    params = {}
    try:
        if not 0 <= args.wait_ready <= 120:
            raise DriveError("INVALID_PARAMS", "--wait-ready must be between 0 and 120 seconds.")
        params = parameters(args.params, args.params_file)
        if args.paginate and args.method not in PAGED:
            raise DriveError("INVALID_PARAMS", "This method does not paginate.")
        if args.profile_id:
            from .catalog import select
            selected = select(args.profile_id, args.state_dir, args.config_fingerprint)["selected"]
            config_path, fingerprint = selected["config_path"], selected["config_fingerprint"]
        else:
            if args.state_dir is not None:
                raise DriveError("INVALID_PARAMS", "--state-dir applies only to an explicit --profile-id.")
            config_path, fingerprint = args.config, args.config_fingerprint
        bridge = (Bridge(config_path, expected_config_fingerprint=fingerprint) if fingerprint is not None
                  else Bridge(config_path))
        ready = wait_ready(bridge, args.wait_ready) if args.wait_ready else {"ok": True}
        if not ready.get("ok"):
            result = ready
        elif args.paginate:
            result = collect(bridge, args.method, params, args.max_pages)
        else:
            result = bridge.call(args.method, params)
            if "next_cursor" in result:
                result.pop("next_cursor")
                result["cli_pagination"] = "Use --paginate in one invocation to read additional pages."
        exit_code = 0 if result.get("ok") else 1
    except DriveError as exc:
        result = failure(exc.code, exc.message, exc.retryable, exc.next_action)
        exit_code = 1
    except (OSError, ValueError, TypeError, RecursionError):
        result = failure("CLI_INPUT_FAILED", "The configuration or input could not be read; no request was replayed.")
        exit_code = 1
    except KeyboardInterrupt:
        result = failure("CLI_CANCELLED", "The CLI disconnected; a previously requested scan may still be running.")
        exit_code = 130
    finally:
        if bridge is not None:
            bridge.close()
    if args.evidence_envelope:
        from .evidence import envelope
        try:
            result = envelope(result, args.method, bridge.config if bridge else None, params,
                              selected=selected if bridge else None)
        except DriveError as exc:
            result = failure(exc.code, exc.message, exc.retryable, exc.next_action)
            exit_code = 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
    return exit_code
