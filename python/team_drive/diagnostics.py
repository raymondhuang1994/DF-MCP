"""Bounded operational measurements; never persist requests or document content."""
import math
import time

from .errors import DriveError


def settings(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"enabled", "max_events", "retention_days"}:
        raise DriveError("INVALID_CONFIG", "diagnostics must contain only enabled, max_events and retention_days.")
    result = dict(enabled=True, max_events=1000, retention_days=7)
    result.update(value)
    if not isinstance(result["enabled"], bool):
        raise DriveError("INVALID_CONFIG", "diagnostics.enabled must be boolean.")
    for key, maximum in (("max_events", 10000), ("retention_days", 90)):
        val = result[key]
        if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= maximum:
            raise DriveError("INVALID_CONFIG", "Diagnostic retention settings are outside their bounded range.")
    return result


def init_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS diagnostic_events("
               "id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at REAL NOT NULL, "
               "operation TEXT NOT NULL,duration_ms REAL NOT NULL,ok INTEGER NOT NULL,error_code TEXT)")


def prune(db, config):
    db.execute("DELETE FROM diagnostic_events WHERE recorded_at<?", (time.time() - config["retention_days"] * 86400,))
    db.execute("DELETE FROM diagnostic_events WHERE id NOT IN "
               "(SELECT id FROM diagnostic_events ORDER BY id DESC LIMIT ?)", (config["max_events"],))


OPERATIONS = {"status", "list", "search", "inspect", "read", "context", "plan", "export",
              "evidence_bundle", "refresh", "cancel", "job_status", "parse", "scan", "semantic_index",
              "wiki_status", "wiki_search", "wiki_read", "wiki_write", "wiki_refresh"}


def record(db, config, operation, duration_ms, ok, error_code=None):
    if not config["enabled"] or operation not in OPERATIONS:
        return
    # Only fixed operation names and controlled error codes reach this table.
    if not isinstance(duration_ms, (int, float)) or not math.isfinite(duration_ms):
        return
    if error_code is not None:
        error_code = str(error_code)
        if not error_code or len(error_code) > 80 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in error_code):
            error_code = "UNCLASSIFIED_ERROR"
    db.execute("INSERT INTO diagnostic_events(recorded_at,operation,duration_ms,ok,error_code) VALUES(?,?,?,?,?)",
               (time.time(), operation, round(max(0, duration_ms), 3), int(bool(ok)), error_code))
    prune(db, config)


def report(db, config, limit=20):
    prune(db, config)
    rows = db.execute("SELECT recorded_at,operation,duration_ms,ok,error_code FROM diagnostic_events ORDER BY id DESC").fetchall()
    grouped = {}
    for row in rows:
        values = grouped.setdefault(row["operation"], {"durations": [], "failures": 0})
        values["durations"].append(row["duration_ms"])
        values["failures"] += not row["ok"]
    aggregate = []
    for name, values in sorted(grouped.items()):
        times = sorted(values["durations"])
        def percentile(p):
            return times[max(0, math.ceil(len(times) * p) - 1)]
        aggregate.append({"operation": name, "count": len(times), "failed": values["failures"],
                          "p50_ms": percentile(.5), "p95_ms": percentile(.95)})
    return {"ok": True, "enabled": config["enabled"], "retention": dict(config),
            "stored_events": len(rows), "events": [dict(row) for row in rows[:limit]],
            "aggregates": aggregate,
            "scope": "Bounded local operation samples, not all-time totals or answer accuracy.",
            "content_logged": False, "query_logged": False, "paths_logged": False}
