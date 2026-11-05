"""Bounded, path-free scan telemetry, independent of published inventory."""
import contextlib
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
import time

STAGES = frozenset(("validating_root", "enumerating", "verifying", "publishing", "parsing",
                    "complete", "partial", "failed", "cancelled"))
COUNTERS = frozenset(("directories_enumerated", "entries_enumerated", "items_verified", "pending_items"))
OPERATIONS = frozenset(("root_validation", "directory_enumeration", "item_observation",
                        "directory_validation", "native_metadata"))


def _utc():
    return datetime.now(timezone.utc).isoformat()


class ScanProgress:
    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._started = self._clock()
        self._finished = None
        self._stage = "validating_root"
        self._published = False
        self._updated = _utc()
        self._counters = {key: 0 for key in sorted(COUNTERS)}
        self._operations = {key: {"calls": 0, "total_ms": 0.0, "max_ms": 0.0} for key in sorted(OPERATIONS)}
        self._active = {}
        self._serial = 0
        self._last_operation = self._failed_operation = self._failed_stage = None

    def set_stage(self, stage):
        if stage not in STAGES:
            raise ValueError("Unknown scan stage")
        with self._lock:
            self._stage, self._updated = stage, _utc()
            if stage == "parsing":
                self._published = True

    def increment(self, counter, n=1):
        if counter not in COUNTERS or isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("Invalid scan counter")
        with self._lock:
            self._counters[counter] = min(10**12, self._counters[counter] + n)
            self._updated = _utc()

    @contextlib.contextmanager
    def operation(self, name):
        if name not in OPERATIONS:
            raise ValueError("Unknown scan operation")
        with self._lock:
            if len(self._active) >= 16:
                raise ValueError("Scan instrumentation nesting exceeded")
            self._serial += 1
            token, started = self._serial, self._clock()
            self._active[token] = (name, started)
            self._operations[name]["calls"] += 1
            self._last_operation, self._updated = name, _utc()
        try:
            yield
        except Exception:
            with self._lock:
                if self._failed_operation is None:
                    self._failed_operation, self._failed_stage = name, self._stage
            raise
        finally:
            with self._lock:
                duration = max(0.0, (self._clock() - started) * 1000)
                values = self._operations[name]
                values["total_ms"] += duration
                values["max_ms"] = max(values["max_ms"], duration)
                self._active.pop(token, None)
                self._updated = _utc()

    def finish(self, outcome):
        if outcome not in ("complete", "partial", "failed", "cancelled"):
            raise ValueError("Unknown scan outcome")
        with self._lock:
            if outcome == "failed" and self._failed_stage is None:
                self._failed_stage = self._stage
            self._stage, self._finished, self._updated = outcome, self._clock(), _utc()

    def snapshot(self):
        with self._lock:
            now = self._clock() if self._finished is None else self._finished
            operations = {}
            for name, values in self._operations.items():
                active = [max(0, (now - started) * 1000) for key, started in self._active.values() if key == name]
                operations[name] = dict(values, total_ms=round(values["total_ms"], 3),
                                        max_ms=round(values["max_ms"], 3), inflight=len(active),
                                        active_ms=round(max(active, default=0.0), 3))
            return {"schema": "scan-progress-v1", "stage": self._stage,
                    "elapsed_seconds": round(max(0, now - self._started), 6),
                    "last_progress_at": self._updated, "counters": dict(self._counters),
                    "operations": operations, "last_operation": self._last_operation,
                    "failed_operation": self._failed_operation, "failed_stage": self._failed_stage,
                    "inventory_published": self._published,
                    "timing_scope": "Nested wall timings overlap; native I/O is not forcibly cancelled."}


def error_detail(exc, progress=None):
    """Keep operational context without arbitrary exception text, paths or locals."""
    raw_code = getattr(exc, "code", "SCAN_FAILED")
    code = raw_code if isinstance(raw_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", raw_code) else "SCAN_FAILED"
    messages = {
        "SHARE_TIMEOUT": "The scan exceeded its configured budget between Windows I/O calls.",
        "SHARE_SCAN_LIMIT": "The scan exceeded its configured item or depth limit.",
        "SHARE_SCAN_INCOMPLETE": "Some shared entries could not be verified; no complete inventory was published.",
        "SHARE_ROOT_UNAVAILABLE": "The approved shared root could not be verified with current access.",
        "SHARE_ROOT_CHANGED": "The approved shared root or an ancestor changed identity or target.",
        "SHARE_DIRECTORY_CHANGED": "A shared directory changed during enumeration.",
        "SHARE_ENUMERATION_FAILED": "A native shared directory or metadata operation failed.",
    }
    snapshot = progress.snapshot() if progress is not None else {}
    result = {"code": code, "message": messages.get(code, "The indexing task failed; inspect its stage and error code."),
              "stage": snapshot.get("failed_stage") or snapshot.get("stage"),
              "operation": snapshot.get("failed_operation") or snapshot.get("last_operation"),
              "native_errno": None, "winerror": None, "frames": []}
    seen = set()
    current = exc
    for unused in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        for field, attr in (("native_errno", "errno"), ("winerror", "winerror")):
            value = getattr(current, attr, None)
            if result[field] is None and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**32-1:
                result[field] = value
        tb = current.__traceback__
        while tb is not None:
            filename = Path(tb.tb_frame.f_code.co_filename)
            if filename.parent == Path(__file__).parent and re.fullmatch(r"[a-z_]+\.py", filename.name):
                function = tb.tb_frame.f_code.co_name
                if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", function):
                    result["frames"].append({"module": filename.stem, "function": function, "line": tb.tb_lineno})
                    result["frames"] = result["frames"][-8:]
            tb = tb.tb_next
        current = current.__cause__ or current.__context__
    return result
