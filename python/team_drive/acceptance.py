"""Bounded, content-free acceptance report over one confirmed broker connection.

This starts no installation or configuration flow. Connecting can start the
configured indexer, so the CLI requires explicit confirmation of that config.
Raw replies stay in memory; report fields are projected through allowlists.
"""
import argparse
import json
import math
import platform
import re
import time

from . import __version__
from .bridge import Bridge
from .errors import DriveError
from .extract import SUPPORTED_SUFFIXES

STAGES = {"validating_root", "enumerating", "verifying", "publishing", "parsing",
          "complete", "failed", "cancelled"}
OPERATIONS = {"root_validation", "directory_enumeration", "item_observation",
              "directory_validation", "native_metadata"}
COUNTERS = {"directories_enumerated", "entries_enumerated", "items_verified", "pending_items"}
FILE_STATES = {"ready", "failed", "blocked", "unsupported", "online_only", "oversized", "pending", "stale", "deleted"}
FORMATS = {value.lstrip(".") for value in SUPPORTED_SUFFIXES}
LOCATOR_KINDS = {"line", "row", "worksheet", "paragraph", "table_row", "slide_text",
                 "slide_table_row", "slide_notes", "slide", "page"}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return (round(value, 6) if not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 10 ** 15 else None)


def _enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z_0-9]{0,79}", value) else "UNCLASSIFIED_ERROR"


def _error(result):
    error = _dict(_dict(result).get("error"))
    return {"code": _code(error.get("code")), "retryable": error.get("retryable") is True}


def _coverage(value):
    value = _dict(value)
    result = {key: value.get(key) is True for key in ("inventory_complete", "all_present_files_indexed",
                                                    "all_present_files_extracted_completely")}
    result.update({key: _number(value.get(key)) for key in ("present_files", "indexed_files", "partial_files")})
    states = _dict(value.get("states"))
    result["states"] = {key: _number(states[key]) for key in sorted(FILE_STATES & set(states))}
    result["business_completeness"] = "not_established"
    return result


def _progress(value):
    value = _dict(value)
    if not value:
        return None
    result = {"stage": _enum(value.get("stage"), STAGES),
              "elapsed_seconds": _number(value.get("elapsed_seconds")),
              "last_operation": _enum(value.get("last_operation"), OPERATIONS),
              "failed_stage": _enum(value.get("failed_stage"), STAGES),
              "failed_operation": _enum(value.get("failed_operation"), OPERATIONS)}
    stamp = value.get("last_progress_at")
    result["last_progress_at"] = (stamp if isinstance(stamp, str) and
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)", stamp) else None)
    counters = _dict(value.get("counters"))
    result["counters"] = {key: _number(counters.get(key)) for key in sorted(COUNTERS)}
    operations = _dict(value.get("operations"))
    result["operations"] = {key: {field: _number(_dict(operations[key]).get(field))
        for field in ("calls", "total_ms", "max_ms", "inflight", "active_ms")}
        for key in sorted(OPERATIONS & set(operations))}
    return result


def _job(value):
    value = _dict(value)
    detail = _dict(value.get("error_detail"))
    result = {"status": _enum(value.get("status"), {"queued", "running", "complete", "failed", "cancelled", "interrupted"}),
              "discovered": _number(value.get("discovered")), "processed": _number(value.get("processed")),
              "scan_progress": _progress(value.get("scan_progress")),
              "error_code": _code(value["error_code"]) if value.get("error_code") else None}
    for key in ("created_at", "started_at", "finished_at"):
        result[key] = _number(value.get(key))
    start, end = result["started_at"], result["finished_at"]
    result["duration_seconds"] = round(end - start, 6) if start is not None and end is not None and end >= start else None
    if detail:
        # Never pass through message, traceback, paths or frame names.
        result["error_detail"] = {"code": _code(detail.get("code")),
            "stage": _enum(detail.get("stage"), STAGES),
            "operation": _enum(detail.get("operation"), OPERATIONS),
            "native_errno": _number(detail.get("native_errno")), "winerror": _number(detail.get("winerror"))}
    return result


def _status(value):
    value = _dict(value)
    version = value.get("service_version")
    result = {"service_version": version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) else None,
              "provider": _enum(value.get("provider"), {"local", "windows_share", "microsoft_graph"}),
              "index_generation": _number(value.get("index_generation")),
              "coverage": _coverage(value.get("coverage"))}
    readiness = _dict(value.get("index_status"))
    result["index_state"] = _enum(readiness.get("state"), {"empty", "scanning", "failed", "ready", "partial", "cancelled", "not_ready"})
    storage = _dict(value.get("state_storage"))
    result["storage"] = {"persistent": storage.get("persistent") is True,
                         "index_copies": _number(storage.get("index_copies")),
                         "mode": _enum(storage.get("mode"), {"shared"})}
    return result


def _same(before, after, key):
    return bool(before.get(key)) and before.get(key) == after.get(key)


def report(bridge, wait_seconds=30, query=None):
    """Run bounded technical checks; this does not judge business correctness."""
    result = {"schema": "df-mcp-acceptance-v1", "ok": False,
              "runtime_version": __version__, "python_version": platform.python_version(),
              "platform": _enum(platform.system(), {"Windows", "Darwin", "Linux"}),
              "privacy": {"paths_included": False, "identity_included": False, "content_included": False,
                          "query_included": False, "file_names_included": False},
              "business_accuracy": "requires_local_human_review", "timings_ms": {}}

    def call(method, params):
        start = time.perf_counter()
        response = bridge.call(method, params)
        elapsed = round(max(0, time.perf_counter() - start) * 1000, 3)
        return response, elapsed

    first, result["timings_ms"]["initial_status"] = call("status", {})
    if first.get("ok") is not True:
        return dict(result, outcome="status_failed", error=_error(first))
    result["initial_status"] = _status(first)
    result["service_matches_loaded_runtime"] = first.get("service_version") == __version__
    if not result["service_matches_loaded_runtime"]:
        return dict(result, outcome="service_version_mismatch", error={"code": "SERVICE_VERSION_MISMATCH"})
    first_session = getattr(bridge, "session_id", None)
    job_id = first.get("current_job_id")
    job_response, unused = call("job_status", {"job_id": job_id} if job_id else {})
    if job_response.get("ok") is not True:
        if _error(job_response)["code"] != "JOB_NOT_FOUND":
            return dict(result, outcome="job_status_failed", error=_error(job_response))
        job = {}
    else:
        job = _dict(job_response.get("job"))
        job_id = job.get("id") or job_id
    # Follow this task, even if background polling later creates another one.
    deadline = time.monotonic() + wait_seconds
    wait_start = time.perf_counter()
    while job.get("status") in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        job_response, unused = call("job_status", {"job_id": job_id})
        if job_response.get("ok") is not True:
            return dict(result, outcome="job_status_failed", error=_error(job_response))
        job = _dict(job_response.get("job"))
    result["timings_ms"]["task_wait"] = round(max(0, time.perf_counter() - wait_start) * 1000, 3)
    result["tracked_job"] = _job(job) if job else None
    coverage = job_response.get("coverage", first.get("coverage"))
    result["latest_job_coverage"] = _coverage(coverage)
    if job.get("status") in ("failed", "cancelled", "interrupted"):
        return dict(result, outcome="index_task_failed", sample={"attempted": False},
                    search={"attempted": False, "reason": "index_task_failed"})
    if (job.get("status") in ("queued", "running") or
            _dict(coverage).get("inventory_complete") is not True):
        return dict(result, outcome="index_not_ready", sample={"attempted": False},
                    search={"attempted": False, "reason": "index_not_ready"})
    sample, sample_ms = _sample(call)
    result["sample"] = sample
    result["timings_ms"].update(sample_ms)
    result["search"] = {"attempted": False, "reason": "query_not_supplied"}
    if query is not None and sample.get("technical_result") == "passed":
        result["search"] = _queries(call, query, {"coverage": coverage})
    elif query is not None:
        result["search"] = {"attempted": False, "reason": "sample_check_incomplete"}
    if sample.get("technical_result") != "passed" or (result["search"]["attempted"] and
            not result["search"].get("calls_succeeded")):
        return dict(result, outcome="technical_checks_incomplete")
    final, unused = call("status", {})
    if final.get("ok") is not True:
        return dict(result, outcome="status_failed", error=_error(final))
    result["final_status"] = _status(final)
    result["reuse"] = {"same_instance": _same(first, final, "instance_id"),
                       "same_config": _same(first, final, "config_id"),
                       "same_source": _same(first, final, "source_id"),
                       "same_connection": bool(first_session) and first_session == getattr(bridge, "session_id", None)}
    result["ok"] = all(result["reuse"].values()) and final.get("service_version") == __version__ and (
        _dict(final.get("coverage")).get("inventory_complete") is True) and (
        not result["search"]["attempted"] or result["search"].get("calls_succeeded") is True)
    result["outcome"] = "technical_checks_passed" if result["ok"] else "technical_checks_incomplete"
    return result


def _sample(call):
    times, cursor, selected = {}, None, None
    for page in range(10):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        listing, elapsed = call("list", params)
        times["sample_discovery"] = round(times.get("sample_discovery", 0) + elapsed, 3)
        if listing.get("ok") is not True:
            return {"attempted": False, "error": _error(listing)}, times
        selected = next((item for item in listing.get("files", []) if isinstance(item, dict)
                         and item.get("status") == "ready" and item.get("format") in FORMATS), None)
        if selected:
            break
        cursor = listing.get("next_cursor")
        if not cursor:
            break
    if selected is None:
        return {"attempted": False, "reason": "sample_page_budget" if cursor else "no_ready_supported_sample"}, times
    file_id = selected.get("file_id")
    inspected, times["inspect"] = call("inspect", {"file_id": file_id, "limit": 1})
    result = {"attempted": True, "format": _enum(selected.get("format"), FORMATS),
              "size_bytes": _number(selected.get("size_bytes")), "technical_result": "failed",
              "business_content_verified": False, "locator_accuracy_verified": False,
              "local_human_review_required": True}
    if inspected.get("ok") is not True:
        return dict(result, error=_error(inspected)), times
    version = inspected.get("source_version") or _dict(inspected.get("file")).get("source_version")
    if not isinstance(version, str) or not version:
        return dict(result, error={"code": "MISSING_SOURCE_VERSION"}), times
    read, times["read"] = call("read", {"file_id": file_id, "expected_version": version, "limit": 3})
    if read.get("ok") is not True:
        return dict(result, error=_error(read)), times
    segments = read.get("segments") if isinstance(read.get("segments"), list) else []
    content = [item for item in segments if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()]
    kinds = {_dict(item.get("locator")).get("kind") for item in content}
    result.update(same_source_version=read.get("source_version") == version,
                  same_file=read.get("file_id") == file_id,
                  nonempty_text_segments=len(content),
                  locators_present=bool(content) and all(bool(_dict(item.get("locator"))) for item in content),
                  locator_kinds=sorted(kind for kind in kinds if isinstance(kind, str) and kind in LOCATOR_KINDS),
                  whole_file_in_response=read.get("whole_file_in_response") is True,
                  extraction_complete=read.get("extraction_complete") is True,
                  format_warning_count=len(read.get("format_warnings", [])) if isinstance(read.get("format_warnings"), list) else None,
                  read_scope="at_most_three_segments")
    if result["same_source_version"] and result["same_file"] and content and result["locators_present"]:
        result["technical_result"] = "passed"
    return result, times


def _queries(call, query, status):
    attempts = []
    for unused in range(2):
        response, elapsed = call("search", {"query": query, "limit": 10, "mode": "keyword", "rerank": False})
        attempt = {"duration_ms": elapsed, "ok": response.get("ok") is True,
                   "returned": _number(response.get("returned")),
                   "inventory_complete": _dict(response.get("coverage")).get("inventory_complete") is True,
                   "index_generation": _number(response.get("index_generation"))}
        if not attempt["ok"]:
            attempt["error"] = _error(response)
        attempts.append(attempt)
        if not attempt["ok"]:
            break  # Do not reconnect/replay after a failed request.
    same_generation = (len(attempts) == 2 and attempts[0]["index_generation"] is not None and
                       attempts[0]["index_generation"] == attempts[1]["index_generation"])
    qualified = (same_generation and all(item["ok"] and item["inventory_complete"] and
                 (item["returned"] or 0) > 0 for item in attempts) and
                 (_dict(status.get("coverage")).get("indexed_files") or 0) > 0)
    return {"attempted": True, "calls_succeeded": len(attempts) == 2 and all(item["ok"] for item in attempts),
            "attempts": attempts, "latency_scope": "single_connection_tool_roundtrip_excludes_cli_startup",
            "same_index_generation": same_generation,
            "performance_basis": "nonempty_index_and_results" if qualified else "not_representative",
            "production_performance_verified": False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error can echo private argument values.
        raise DriveError("INVALID_ARGUMENTS", "Use --help for the supported acceptance arguments.")


def main(argv=None):
    bridge = None
    exit_code = 1
    try:
        parser = _Parser(description="Check one confirmed configuration; stdout contains a content-free JSON report.")
        parser.add_argument("--config", required=True)
        parser.add_argument("--confirmed-config", action="store_true",
                            help="The source, subfolders and local index location already have user confirmation; connecting may start indexing.")
        parser.add_argument("--wait-ready", type=float, default=30, metavar="SECONDS")
        parser.add_argument("--query", help="Optional keyword checked twice; never included in the JSON report.")
        args = parser.parse_args(argv)
        if not args.confirmed_config:
            raise DriveError("CONFIG_CONFIRMATION_REQUIRED", "Confirm the existing source and local index configuration before connecting.")
        if not math.isfinite(args.wait_ready) or not 0 <= args.wait_ready <= 120:
            raise DriveError("INVALID_ARGUMENTS", "wait-ready must be between 0 and 120 seconds.")
        if args.query is not None and (not args.query.strip() or len(args.query) > 500):
            raise DriveError("INVALID_QUERY", "Use a nonempty query of at most 500 characters.")
        started = time.perf_counter()
        bridge = Bridge(args.config)
        connected = time.perf_counter()
        result = report(bridge, args.wait_ready, args.query)
        result["timings_ms"]["connection"] = round(max(0, connected - started) * 1000, 3)
        result["timings_ms"]["total"] = round(max(0, time.perf_counter() - started) * 1000, 3)
        exit_code = 0 if result["ok"] else 1
    except DriveError as exc:
        result = {"schema": "df-mcp-acceptance-v1", "ok": False, "outcome": "check_failed",
                  "error": {"code": _code(exc.code), "retryable": exc.retryable is True}}
    except KeyboardInterrupt:
        result = {"schema": "df-mcp-acceptance-v1", "ok": False, "outcome": "disconnected_scan_may_continue",
                  "error": {"code": "CHECK_CANCELLED"}}
        exit_code = 130
    except Exception:
        result = {"schema": "df-mcp-acceptance-v1", "ok": False, "outcome": "check_failed",
                  "error": {"code": "CHECK_INPUT_OR_RUNTIME_FAILED"}}
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                result = {"schema": "df-mcp-acceptance-v1", "ok": False, "outcome": "check_failed",
                          "error": {"code": "CHECK_DISCONNECT_FAILED"}}
                exit_code = 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
    return exit_code
