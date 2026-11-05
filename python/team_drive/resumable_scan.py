"""Durable, directory-atomic SMB inventory cycles.

Completed directories describe observed membership, not a transactional server
snapshot or immutable file content. Every returned remote candidate/evidence
still passes the existing current-access and content-version checks.
"""
import json
import time
import uuid

from .errors import DriveError


class ResumableScan:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def init_schema(db):
        statements = ("""CREATE TABLE IF NOT EXISTS scan_cycle (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                cycle_id TEXT NOT NULL, phase TEXT NOT NULL,
                verify_target TEXT NOT NULL, verify_after TEXT,
                started_at REAL NOT NULL, last_error_code TEXT, resumed INTEGER NOT NULL DEFAULT 0,
                force_refresh INTEGER NOT NULL DEFAULT 0,
                collect_verified INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS scan_directories (
                path TEXT PRIMARY KEY, depth INTEGER NOT NULL, status TEXT NOT NULL,
                identity TEXT NOT NULL, fingerprint TEXT)""")
        for statement in statements:
            db.execute(statement)
        if "force_refresh" not in {r[1] for r in db.execute("PRAGMA table_info(scan_cycle)")}:
            db.execute("ALTER TABLE scan_cycle ADD COLUMN force_refresh INTEGER NOT NULL DEFAULT 0")
        if "collect_verified" not in {r[1] for r in db.execute("PRAGMA table_info(scan_cycle)")}:
            db.execute("ALTER TABLE scan_cycle ADD COLUMN collect_verified INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def status(db, enabled):
        if not enabled:
            return {"enabled": False}
        cycle = db.execute("SELECT * FROM scan_cycle WHERE singleton=1").fetchone()
        counts = dict(db.execute("SELECT status,COUNT(*) FROM scan_directories GROUP BY status").fetchall())
        pending = [r[0] or "." for r in db.execute("SELECT path FROM scan_directories WHERE status='pending' ORDER BY depth,path LIMIT 20")]
        return {"enabled": True, "cycle_id": cycle["cycle_id"] if cycle else None,
                "phase": cycle["phase"] if cycle else "not_started",
                "completed_directories": counts.get("complete", 0),
                "pending_directories": counts.get("pending", 0),
                "pending_scopes": pending, "pending_scopes_truncated": counts.get("pending", 0) > len(pending),
                "last_error_code": cycle["last_error_code"] if cycle else None,
                "resumed": bool(cycle["resumed"]) if cycle else False,
                "next_action": "refresh resumes the saved cycle; force=true starts a new cycle",
                "unit": "one fully verified directory; a directory exceeding its budget remains pending",
                "content_freshness": "Directory fingerprints cover membership; individual evidence is verified on use."}

    def _start(self, force):
        e = self.engine
        with e._db() as db:
            cycle = db.execute("SELECT * FROM scan_cycle WHERE singleton=1").fetchone()
            if force or cycle is None or cycle["phase"] == "complete":
                cycle_id = uuid.uuid4().hex
                db.execute("DELETE FROM scan_directories")
                db.execute("INSERT INTO scan_cycle VALUES(1,?,'collect','collect',NULL,?,NULL,0,?,0) "
                           "ON CONFLICT(singleton) DO UPDATE SET cycle_id=excluded.cycle_id,phase='collect',"
                           "verify_target='collect',verify_after=NULL,started_at=excluded.started_at,last_error_code=NULL,resumed=0,force_refresh=excluded.force_refresh,collect_verified=0",
                           (cycle_id, time.time(), int(force)))
                db.execute("INSERT INTO scan_directories VALUES('',0,'pending',?,NULL)",
                           (json.dumps([str(v) for v in e._source.identity]),))
            else:
                cycle_id = cycle["cycle_id"]
                # Resume reauthorizes completed scopes before reusing their
                # enumeration work. The verification cursor itself is durable,
                # so many directories cannot starve scanning across jobs.
                if cycle["phase"] == "collect" and not cycle["collect_verified"]:
                    db.execute("UPDATE scan_cycle SET phase='verify',verify_target='collect',verify_after=NULL WHERE singleton=1")
                # Consume one persisted verification handoff. If validation
                # leaves too little budget for the next atomic directory, a
                # fresh job can attempt that directory with its full budget.
                # Its root, ancestor, identity and membership guards still run.
                db.execute("UPDATE scan_cycle SET resumed=1,last_error_code=NULL,collect_verified=0 WHERE singleton=1")
            db.execute("UPDATE meta SET value='false' WHERE key='inventory_complete'")
        return cycle_id

    def _invalidate_scope(self, path, fingerprint):
        """A changed membership epoch invalidates this scope and descendants."""
        e = self.engine
        prefix = path + "/" if path else ""
        with e._db() as db:
            if path:
                db.execute("DELETE FROM scan_directories WHERE path=? OR substr(path,1,?)=?", (path, len(prefix), prefix))
                db.execute("UPDATE files SET seen_scan=NULL WHERE substr(path,1,?)=?", (len(prefix), prefix))
            else:
                db.execute("DELETE FROM scan_directories")
                db.execute("UPDATE files SET seen_scan=NULL")
            db.execute("INSERT INTO scan_directories VALUES(?,?,'pending',?,NULL)",
                       (path, len(path.split('/')) if path else 0, json.dumps(fingerprint[:2])))
            db.execute("UPDATE scan_cycle SET phase='collect',verify_target='collect',verify_after=NULL,collect_verified=0 WHERE singleton=1")

    def _parse_pending(self, job_id, progress, counters, deadline):
        e = self.engine
        last_path = ""
        while job_id not in e._cancelled and not e._stop.is_set():
            e._source._deadline(deadline)
            with e._db() as db:
                row = db.execute("SELECT * FROM files WHERE status='pending' AND path>? ORDER BY path LIMIT 1", (last_path,)).fetchone()
            if row is None:
                break
            progress.set_stage("parsing")
            last_path = row["path"]
            e._parse_file(row)
            counters["processed"] += 1
            with e._db() as db:
                db.execute("UPDATE jobs SET processed=? WHERE id=?", (counters["processed"], job_id))

    def _finish_inventory(self, cycle_id):
        e = self.engine
        e._check_root()
        with e._db() as db:
            removed = [r[0] for r in db.execute("SELECT id FROM files WHERE (seen_scan IS NULL OR seen_scan!=?) AND status!='deleted'", (cycle_id,))]
            for file_id in removed:
                db.execute("UPDATE files SET status='deleted',source_version=NULL,segments_count=0,error_code=NULL,error_message=NULL,origin='{}' WHERE id=?", (file_id,))
                db.execute("DELETE FROM segments WHERE file_id=?", (file_id,))
                db.execute("DELETE FROM structure WHERE file_id=?", (file_id,))
                db.execute("DELETE FROM search_index WHERE file_id=?", (file_id,))
                e.retrieval.delete_file(db, file_id)
            if removed:
                e._bump(db)
            db.execute("UPDATE meta SET value='true' WHERE key='inventory_complete'")
            db.execute("UPDATE scan_cycle SET phase='complete',verify_after=NULL,last_error_code=NULL WHERE singleton=1")

    def run(self, job_id, force, progress):
        e = self.engine
        started = time.perf_counter()
        e._check_root()
        deadline = time.monotonic() + e._source.limits["scan_budget_seconds"]
        cycle_id = self._start(force)
        counters = {"discovered": 0, "processed": 0}
        consumed = 0
        outcome, error_code = "partial", None
        with e._db() as db:
            db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (time.time(), job_id))
        try:
            # A process can stop after publishing a verified batch but before
            # parsing it. Retry these rows even if no unvisited directory remains.
            self._parse_pending(job_id, progress, counters, deadline)
            while True:
                if job_id in e._cancelled or e._stop.is_set():
                    outcome = "cancelled"
                    break
                e._source._deadline(deadline)
                with e._db() as db:
                    cycle = dict(db.execute("SELECT * FROM scan_cycle WHERE singleton=1").fetchone())
                if cycle["phase"] == "verify":
                    with e._db() as db:
                        row = db.execute("SELECT * FROM scan_directories WHERE status='complete' AND (? IS NULL OR path>?) ORDER BY path LIMIT 1",
                                         (cycle["verify_after"], cycle["verify_after"])).fetchone()
                    if row is None:
                        if cycle["verify_target"] == "complete":
                            self._finish_inventory(cycle_id)
                            outcome = "complete"
                            break
                        with e._db() as db:
                            db.execute("UPDATE scan_cycle SET phase='collect',verify_after=NULL,collect_verified=1 WHERE singleton=1")
                        continue
                    progress.set_stage("verifying")
                    current = e._source.directory_fingerprint(row["path"], deadline, progress)
                    if current != json.loads(row["fingerprint"]):
                        self._invalidate_scope(row["path"], current)
                    else:
                        with e._db() as db:
                            db.execute("UPDATE scan_cycle SET verify_after=? WHERE singleton=1", (row["path"],))
                    continue
                with e._db() as db:
                    row = db.execute("SELECT * FROM scan_directories WHERE status='pending' ORDER BY depth,path LIMIT 1").fetchone()
                if row is None:
                    with e._db() as db:
                        db.execute("UPDATE scan_cycle SET phase='verify',verify_target='complete',verify_after=NULL WHERE singleton=1")
                    continue
                remaining = e._source.limits["max_items"] - consumed
                if remaining <= 0:
                    raise DriveError("SHARE_SCAN_LIMIT", "The job reached its batch item budget; refresh resumes remaining directories.", True)
                batch = e._source.scan_directory(row["path"], json.loads(row["identity"]), deadline, remaining, progress)
                for relative, info, blocked, online in batch["files"]:
                    e._publish_inventory_entry(relative, info, blocked, online, cycle_id, bool(cycle["force_refresh"]))
                    counters["discovered"] += 1
                with e._db() as db:
                    for child in batch["directories"]:
                        db.execute("INSERT OR IGNORE INTO scan_directories VALUES(?,?,'pending',?,NULL)",
                                   (child["path"], row["depth"] + 1, json.dumps(child["identity"])))
                    db.execute("UPDATE scan_directories SET status='complete',fingerprint=? WHERE path=?",
                               (json.dumps(batch["fingerprint"]), row["path"]))
                    db.execute("UPDATE scan_cycle SET collect_verified=0 WHERE singleton=1")
                    db.execute("UPDATE jobs SET discovered=? WHERE id=?", (counters["discovered"], job_id))
                consumed += batch["entries"]
                # Publish and parse this verified subset now; a later timeout
                # cannot discard it or turn an unfinished scope into absence.
                self._parse_pending(job_id, progress, counters, deadline)
                e._checkpoint(force=True)
        except DriveError as exc:
            error_code = exc.code
            outcome = "partial" if exc.code in ("SHARE_TIMEOUT", "SHARE_SCAN_LIMIT") else "failed"
            if outcome == "failed":
                with e._db() as db:
                    db.execute("UPDATE scan_cycle SET last_error_code=? WHERE singleton=1", (error_code,))
                raise
        except OSError as exc:
            error_code, outcome = "SHARE_ENUMERATION_FAILED", "failed"
            raise DriveError(error_code, "A shared directory or metadata operation failed under current Windows access.", True) from exc
        finally:
            with e._db() as db:
                db.execute("UPDATE scan_cycle SET last_error_code=? WHERE singleton=1", (error_code,))
                db.execute("UPDATE jobs SET status=?,finished_at=?,error_code=? WHERE id=?", (outcome, time.time(), error_code, job_id))
                db.execute("UPDATE meta SET value=? WHERE key='last_scan_at'", (json.dumps(time.time()),))
                e._prune_jobs(db)
        if outcome in ("complete", "partial") and job_id not in e._cancelled and not e._stop.is_set():
            # Preserve explicitly configured semantic indexing for usable
            # subsets. Defaults remain keyword-only; failures remain bounded.
            e._index_semantic_pending(job_id)
        e._record_operation("scan", started, {"ok": outcome == "complete",
                            "error": {"code": "CANCELLED" if outcome == "cancelled" else error_code}})
        return e.job_status(job_id)
