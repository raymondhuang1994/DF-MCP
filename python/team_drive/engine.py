"""Bounded local evidence indexing; optional, explicitly configured local retrieval."""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional
from collections import OrderedDict

from .errors import DriveError
from .fs_access import SourceAccess, StateAccess, online_only, validate_state_location
from .state_pool import JOB_HISTORY_LIMIT, StateLease, pool_settings
from . import diagnostics as operational
from .contextual import select_context, plan as evidence_plan
from .retrieval import Retrieval
from .scan_progress import ScanProgress, error_detail
from .resumable_scan import ResumableScan
from .wiki import WikiStore


DEFAULT_LIMITS = {
    "max_file_bytes": 268435456, "max_uncompressed_bytes": 536870912,
    "max_segments": 100000, "max_segment_chars": 8000,
    "max_columns": 512, "max_pdf_pages": 2000, "parse_timeout_seconds": 30,
}
SUPPORTED_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".xlsx", ".docx", ".pptx", ".pdf"}
MAX_RESPONSE_BYTES = 65536
MAX_SEGMENT_BYTES = 49152
MAX_CURSOR_CHARS = 1024
MAX_QUERY_CHARS = 500
MAX_PREFIX_CHARS = 1024
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
LATIN_RE = re.compile(r"[a-z0-9]+")


def failure(code: str, message: str, retryable: bool = False,
            next_action: str = "Check the request and configuration.") -> dict:
    return {"ok": False, "error": {"code": code, "message": message,
            "retryable": retryable, "next_action": next_action}}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def tokenize(value: str) -> list:
    """Exact keyword tokens, including one/two-character Chinese terms, not semantics."""
    folded = value.casefold()
    tokens = {"w" + word for word in LATIN_RE.findall(folded)}
    for run in CJK_RE.findall(folded):
        tokens.update("c" + char.encode("utf-8").hex() for char in run)
        tokens.update("b" + run[i:i + 2].encode("utf-8").hex() for i in range(len(run) - 1))
    return sorted(tokens)


def query_tokens(value: str) -> list:
    folded = value.casefold()
    tokens = {"w" + word for word in LATIN_RE.findall(folded)}
    for run in CJK_RE.findall(folded):
        if len(run) == 1:
            tokens.add("c" + run.encode("utf-8").hex())
        else:
            tokens.update("b" + run[i:i + 2].encode("utf-8").hex() for i in range(len(run) - 1))
    return sorted(tokens)


class Engine:
    def __init__(self, config_path: str, autostart: bool = True, pooled: bool = False, state_override=None, config_snapshot=None):
        config_file = Path(config_path).expanduser().resolve(strict=config_snapshot is None)
        try:
            config = copy.deepcopy(config_snapshot) if config_snapshot is not None else json.loads(config_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise DriveError("INVALID_CONFIG", "Configuration must be valid UTF-8 JSON.")
        if not isinstance(config, dict) or config.get("provider", "local") not in ("local", "microsoft_graph", "windows_share"):
            raise DriveError("UNSUPPORTED_PROVIDER", "Use local, windows_share or microsoft_graph with an explicit source configuration.")
        self.provider = config.get("provider", "local")
        for key in ("root", "state_dir"):
            if not isinstance(config.get(key), str) or not config[key]:
                raise DriveError("INVALID_CONFIG", "root and state_dir must be nonempty paths.")
        configured_limits = config.get("limits", {})
        if not isinstance(configured_limits, dict):
            raise DriveError("INVALID_CONFIG", "limits must be an object.")
        self.limits = dict(DEFAULT_LIMITS)
        for key, val in configured_limits.items():
            if key not in DEFAULT_LIMITS or isinstance(val, bool) or not isinstance(val, int) or val < 1:
                raise DriveError("INVALID_CONFIG", "Limits must use known keys and positive integers.")
            self.limits[key] = val
        poll = config.get("poll_seconds", 30)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 1 <= poll <= 86400 or not math.isfinite(poll):
            raise DriveError("INVALID_CONFIG", "poll_seconds must be a finite number between 1 and 86400 seconds.")
        self.poll_seconds = poll
        self.aliases = config.get("aliases", {})
        if not isinstance(self.aliases, dict) or any(
                not isinstance(k, str) or not isinstance(v, list) or
                any(not isinstance(a, str) for a in v) for k, v in self.aliases.items()):
            raise DriveError("INVALID_CONFIG", "aliases must map strings to lists of strings.")
        for key in ("retrieval", "diagnostics"):
            if key in config and not isinstance(config[key], dict):
                raise DriveError("INVALID_CONFIG", key + " must be an object.")
        self.retrieval = Retrieval(config.get("retrieval", {}))
        self.diagnostic_settings = operational.settings(config.get("diagnostics"))
        self._diagnostic_failures = 0
        self._ranked_queries = OrderedDict()
        self._query_lock = threading.RLock()
        max_slots = pool_settings(config)
        parser_signature = hashlib.sha256(Path(__file__).with_name("extract.py").read_bytes()).hexdigest()
        indexer_signature = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name("fs_access.py").read_bytes()
                                           + Path(__file__).with_name("network_source.py").read_bytes()).hexdigest()
        self._parse_binding = hashlib.sha256(_json({"parser": parser_signature, "indexer": indexer_signature, "limits": self.limits, "aliases": self.aliases, "schema": 4}).encode()).hexdigest()
        raw_state = Path(config["state_dir"] if state_override is None else state_override).expanduser()
        raw_state = raw_state if raw_state.is_absolute() else config_file.parent / raw_state
        validate_state_location(raw_state)
        raw_root = config["root"] if self.provider == "windows_share" else Path(config["root"]).expanduser()
        if self.provider != "windows_share":
            raw_root = raw_root if raw_root.is_absolute() else config_file.parent / raw_root
        self._cloud_binding = None
        self._share_binding = None
        if "windows_share" in config and self.provider != "windows_share":
            raise DriveError("INVALID_CONFIG", "Shared-folder settings require provider=windows_share.")
        if self.provider == "microsoft_graph":
            from .graph_config import graph_settings, validate_graph_paths, source_binding
            from .graph_auth import TokenProvider
            from .graph_source import GraphSource
            graph = graph_settings(config.get("graph"), config_file.parent)
            validate_graph_paths(dict(root=str(raw_root), state_dir=str(raw_state), graph=graph))
            tokens = TokenProvider(graph, graph.get("auth_cache"))
            self._source = GraphSource(raw_root, graph, self.limits["max_file_bytes"], token_provider=tokens)
            self._cloud_binding = source_binding(graph)
            self._cloud_binding["account_binding"] = tokens.account_binding
        elif self.provider == "windows_share":
            from .network_source import WindowsShareSource
            if "graph" in config:
                raise DriveError("INVALID_CONFIG", "Graph settings cannot be used for a Windows shared folder.")
            self._source = WindowsShareSource(raw_root, config.get("windows_share"), self.limits["max_file_bytes"])
            self._share_binding = self._source.binding
        else:
            if "graph" in config:
                raise DriveError("INVALID_CONFIG", "Graph settings require provider=microsoft_graph.")
            self._source = SourceAccess(raw_root)
        self._remote_source = bool(self._cloud_binding or self._share_binding)
        self.root = self._source.root
        self._root_identity = self._source.identity
        self._storage = None
        self._lease = None
        try:
            approved_identity = config.get("approved_source_identity")
            if approved_identity is not None:
                current_identity = (self._share_binding if self.provider == "windows_share" else
                                    {"provider": "local", "device": self._root_identity[0], "inode": self._root_identity[1]})
                if self.provider == "microsoft_graph" or not isinstance(approved_identity, dict) or approved_identity != current_identity:
                    raise DriveError("APPROVED_SOURCE_CHANGED", "The source identity differs from the confirmed setup, before indexing could start.", False,
                                     "Review the actual folder and create a new configuration plan; do not delete the identity binding.")
            root_binding = {"path": str(self.root), "identity": self._root_identity}
            if self._cloud_binding:
                root_binding["cloud"] = self._cloud_binding
            if self._share_binding:
                root_binding["share"] = self._share_binding
            self._root_binding = hashlib.sha256(_json(root_binding).encode()).hexdigest()
            installation = Path(__file__).resolve().parents[2]
            if pooled:
                self._lease = StateLease(raw_state, self._source, installation, max_slots)
                self._storage = self._lease.storage
                self._state_metadata = self._lease.metadata
            else:
                self._storage = StateAccess(raw_state, self._source, installation)
                self._storage.lock()
                self._state_metadata = {"mode": "exclusive", "persistent": True,
                                        "cleanup": {"job_history_limit": JOB_HISTORY_LIMIT,
                                                    "temporary_files": "reclaimed_on_slot_acquire", "exports": "manual"}}
            self.state = self._storage.path
            self.db_path = self.state / "index.sqlite"
        except Exception:
            if self._storage is not None:
                self._storage.close()
            self._source.close()
            raise
        self._secret = secrets.token_bytes(32)
        self._session = threading.local()
        self._fatal_error = None
        self._stop = threading.Event()
        self._scan_lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._live_progress = {}
        self._queue = queue.Queue()
        self._cancelled = set()
        self._pending_job = None
        self._thread = None
        self._process_lock = threading.Lock()
        self._active_process = None
        self._force_jobs = set()
        self._db_lock = threading.RLock()
        self._sqlite_conn = None
        self._checkpoint_generation = None
        self._checkpoint_at = None
        try:
            self._scratch_cleanup = self._storage.cleanup_scratch()
            self._sqlite_conn = self._storage.connect_database()
            self._sqlite_conn.row_factory = sqlite3.Row
            try:
                if self._sqlite_conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError()
            except sqlite3.DatabaseError:
                raise DriveError("STATE_REBUILD_REQUIRED", "The index failed its integrity check.", False,
                                 "Stop connections using this slot and reset its disposable index with the maintenance command.")
            self._init_db()
            write_profile = getattr(self._storage, "write_profile_manifest", None)
            if write_profile is not None:
                write_profile()
            self._checkpoint()
        except Exception:
            if self._sqlite_conn is not None:
                self._sqlite_conn.close()
            self._storage.close()
            self._source.close()
            raise
        if autostart:
            self._thread = threading.Thread(target=self._run, name="team-drive-index", daemon=True)
            self._thread.start()
            self.refresh()

    @contextlib.contextmanager
    def _db(self):
        with self._db_lock:
            self._storage.check()
            if self._sqlite_conn is None:
                raise DriveError("STATE_CLOSED", "The index is closed.")
            with self._sqlite_conn:
                yield self._sqlite_conn

    def _init_db(self):
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('generation','0');
                INSERT OR IGNORE INTO meta VALUES('inventory_complete','false');
                INSERT OR IGNORE INTO meta VALUES('last_scan_at','null');
                CREATE TABLE IF NOT EXISTS files(
                  id TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE,suffix TEXT,size INTEGER,
                  mtime_ns INTEGER,ctime_ns INTEGER,source_version TEXT,status TEXT NOT NULL,
                  error_code TEXT,error_message TEXT,segments_count INTEGER NOT NULL DEFAULT 0,
                  seen_scan TEXT,updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS files_status ON files(status,path);
                CREATE TABLE IF NOT EXISTS segments(
                  file_id TEXT NOT NULL,ordinal INTEGER NOT NULL,source_version TEXT NOT NULL,
                  payload TEXT NOT NULL,PRIMARY KEY(file_id,ordinal));
                CREATE INDEX IF NOT EXISTS segments_sheet_row ON segments(
                  file_id,source_version,json_extract(payload,'$.locator.sheet'),
                  json_extract(payload,'$.locator.row'),ordinal);
                CREATE INDEX IF NOT EXISTS segments_row ON segments(
                  file_id,source_version,json_extract(payload,'$.locator.row'),ordinal);
                CREATE INDEX IF NOT EXISTS segments_page ON segments(
                  file_id,source_version,json_extract(payload,'$.locator.page'),ordinal);
                CREATE INDEX IF NOT EXISTS segments_slide ON segments(
                  file_id,source_version,json_extract(payload,'$.locator.slide'),ordinal);
                CREATE TABLE IF NOT EXISTS structure(
                  file_id TEXT NOT NULL,position INTEGER NOT NULL,group_key TEXT NOT NULL,
                  start_ordinal INTEGER NOT NULL,end_ordinal INTEGER NOT NULL,
                  PRIMARY KEY(file_id,position));
                CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                  tokens,file_id UNINDEXED,ordinal UNINDEXED,tokenize='ascii');
                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at REAL NOT NULL,
                  started_at REAL,finished_at REAL,discovered INTEGER NOT NULL DEFAULT 0,
                  processed INTEGER NOT NULL DEFAULT 0,error_code TEXT);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(files)")}
            job_columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            for column in ("scan_progress", "error_detail"):
                if column not in job_columns:
                    db.execute("ALTER TABLE jobs ADD COLUMN " + column + " TEXT")
            if "format_warnings" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN format_warnings TEXT NOT NULL DEFAULT '[]'")
            if "extraction_complete" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN extraction_complete INTEGER NOT NULL DEFAULT 1")
            if "content_coverage" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN content_coverage TEXT NOT NULL DEFAULT '{}'")
            if "origin" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN origin TEXT NOT NULL DEFAULT '{}'")
            WikiStore.init_schema(db)
            root_binding = db.execute("SELECT value FROM meta WHERE key='root_binding'").fetchone()
            if root_binding and root_binding[0] != self._root_binding:
                raise DriveError("STATE_ROOT_MISMATCH", "This state directory belongs to a different source root.", False, "Choose a new empty state directory for the new source root.")
            db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('root_binding',?)", (self._root_binding,))
            old_binding = db.execute("SELECT value FROM meta WHERE key='parse_binding'").fetchone()
            if old_binding and old_binding[0] != self._parse_binding:
                db.execute("UPDATE files SET status='pending',source_version=NULL,segments_count=0,format_warnings='[]' WHERE status!='deleted'")
                db.execute("DELETE FROM segments")
                db.execute("DELETE FROM structure")
                db.execute("DELETE FROM search_index")
                self._bump(db)
            db.execute("INSERT INTO meta(key,value) VALUES('parse_binding',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self._parse_binding,))
            db.execute("UPDATE jobs SET status='interrupted',finished_at=? WHERE status IN ('queued','running')", (time.time(),))
            self._prune_jobs(db)
            ResumableScan.init_schema(db)
            operational.init_schema(db)
            operational.prune(db, self.diagnostic_settings)
            self.retrieval.init_schema(db)
            self.retrieval.prune(db)

    @staticmethod
    def _prune_jobs(db):
        db.execute("DELETE FROM jobs WHERE status NOT IN ('queued','running') AND id NOT IN "
                   "(SELECT id FROM jobs WHERE status NOT IN ('queued','running') "
                   "ORDER BY created_at DESC,id DESC LIMIT ?)", (JOB_HISTORY_LIMIT,))

    @staticmethod
    def _bump(db):
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
        WikiStore.invalidate_changed(db)

    @staticmethod
    def _generation(db):
        return int(db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])

    def _coverage(self, db):
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM files GROUP BY status")}
        present = sum(n for s, n in counts.items() if s != "deleted")
        inventory = db.execute("SELECT value FROM meta WHERE key='inventory_complete'").fetchone()[0] == "true"
        partial = db.execute("SELECT COUNT(*) FROM files WHERE status='ready' AND extraction_complete=0").fetchone()[0]
        return {"inventory_complete": inventory, "present_files": present,
                "indexed_files": counts.get("ready", 0), "states": counts,
                "all_present_files_indexed": inventory and present == counts.get("ready", 0),
                "partial_files": partial,
                "all_present_files_extracted_completely": inventory and present == counts.get("ready", 0) and partial == 0,
                "scope": "Configured remote folder; current access and version checked before returning cached candidates or evidence." if self._remote_source else "Configured local root; content freshness is revalidated before evidence reads.",
                "business_completeness": "not_established",
                "scan_resume": ResumableScan.status(db, self._share_binding and getattr(self._source, "resumable", False))}

    def _file_out(self, row):
        return {"file_id": row["id"], "path": row["path"], "format": row["suffix"].lstrip("."),
                "size_bytes": row["size"], "modified_ns": row["mtime_ns"],
                "source_version": row["source_version"], "status": row["status"],
                "segments_count": row["segments_count"],
                "format_warnings": json.loads(row["format_warnings"]),
                "extraction_complete": row["status"] == "ready" and bool(row["extraction_complete"]),
                "content_coverage": json.loads(row["content_coverage"]),
                "origin": json.loads(row["origin"]),
                "error": ({"code": row["error_code"], "message": row["error_message"]}
                          if row["error_code"] else None)}

    def _verify_cloud_page(self, result, deadline=None):
        """Recheck access and remote version before exposing cached cloud candidates."""
        if not self._remote_source:
            return result
        deadline = deadline if deadline is not None else time.monotonic() + 30
        for item in result["files"]:
            try:
                self._source.verify_origin(item["path"], item.get("origin", {}), deadline=deadline)
            except DriveError as exc:
                with self._db() as db:
                    db.execute("UPDATE meta SET value='false' WHERE key='inventory_complete'")
                if self._share_binding or exc.code in ("GRAPH_SOURCE_CHANGED", "GRAPH_SCOPE_CHANGED", "GRAPH_ACCESS_DENIED", "GRAPH_ITEM_UNAVAILABLE"):
                    self._invalidate(item["file_id"], exc.code, exc.message,
                                     expected_version=item.get("source_version"))
                raise
        name = "share" if self._share_binding else "cloud"
        result[name + "_access_checked"] = True
        result[name + "_access_scope"] = "Returned candidates checked sequentially; not an atomic permission snapshot of the whole folder. Windows server/OS authorization semantics apply to shared folders."
        return result

    def _verify_cloud_hits(self, db, hits, deadline):
        if not self._remote_source:
            return
        files, seen = [], set()
        for hit in hits:
            identity = (hit["file_id"], hit["source_version"])
            if identity in seen:
                continue
            seen.add(identity)
            row = db.execute("SELECT * FROM files WHERE id=? AND source_version=? AND status='ready'",
                             (hit["file_id"], hit["source_version"])).fetchone()
            if row is None:
                raise DriveError("STALE_SOURCE", "Cloud retrieval candidates changed; repeat the search.")
            files.append(self._file_out(row))
        self._verify_cloud_page({"files": files}, deadline=deadline)

    @staticmethod
    def _prefix(prefix):
        if not isinstance(prefix, str) or len(prefix) > MAX_PREFIX_CHARS or "\\" in prefix or "\x00" in prefix:
            raise DriveError("INVALID_PATH", "Use a relative path within the configured root.")
        p = PurePosixPath(prefix)
        if p.is_absolute() or ".." in p.parts:
            raise DriveError("INVALID_PATH", "Absolute paths and parent traversal are forbidden.")
        return "" if str(p) == "." else str(p).rstrip("/")

    @staticmethod
    def _limit(limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise DriveError("INVALID_LIMIT", "limit must be an integer between 1 and 100.")
        return limit

    def _cursor(self, binding, offset):
        raw = _json({"binding": self._binding_digest(binding), "offset": offset}).encode()
        signature = hmac.new(self._cursor_key(), raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")

    @staticmethod
    def _binding_digest(binding):
        return hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _cursor_key(self):
        session = getattr(self._session, "session_id", "")
        return hmac.new(self._secret, session.encode("utf-8"), hashlib.sha256).digest()

    def _offset(self, cursor, binding):
        if cursor is None:
            return 0
        try:
            if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_CHARS:
                raise ValueError()
            decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            sig, raw = decoded[:32], decoded[32:]
            obj = json.loads(raw)
            if not hmac.compare_digest(sig, hmac.new(self._cursor_key(), raw, hashlib.sha256).digest()):
                raise ValueError()
            if obj["binding"] != self._binding_digest(binding):
                raise DriveError("STALE_CURSOR", "The query, index generation, or source version has changed.", True, "Restart pagination without a cursor.")
            offset = obj["offset"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError()
            return offset
        except DriveError:
            raise
        except (ValueError, TypeError, KeyError):
            raise DriveError("INVALID_CURSOR", "The cursor is invalid or belongs to another server session.", True, "Restart pagination without a cursor.")

    def status(self):
        self._check_health()
        self._check_root()
        with self._db() as db:
            return {"ok": True, "provider": self.provider, "index_generation": self._generation(db),
                    "coverage": self._coverage(db), "limits": self.limits,
                    "index_status": self._index_status(db),
                    "last_scan_at": json.loads(db.execute("SELECT value FROM meta WHERE key='last_scan_at'").fetchone()[0]),
                    "update_mode": "startup_and_periodic_polling_while_server_runs",
                    "poll_seconds": self.poll_seconds,
                    "state_storage": dict(self._state_metadata, last_scratch_cleanup=self._scratch_cleanup,
                                          recovery=getattr(self._storage, "recovery", {}),
                                          durable_index_generation=self._checkpoint_generation,
                                          checkpoint_at=self._checkpoint_at),
                    "source_id": self._root_binding,
                    "cloud_source": self._cloud_binding,
                    "shared_source": self._share_binding,
                    "indexer_health": "healthy" if self._thread is not None else "manual",
                    "current_job_id": self._pending_job,
                    "search_mode": "keyword_with_aliases_and_bm25",
                    "retrieval": self.retrieval.status(db),
                    "capabilities": {"versioned_context": True, "evidence_plan": True,
                                     "resumable_scan": bool(self._share_binding and getattr(self._source, "resumable", False)),
                                     "diagnostics": True, "search_modes": ["keyword", "hybrid", "semantic"],
                                     "ocr": False, "network_source": self._remote_source,
                                     "source_providers": ["local", "windows_share", "microsoft_graph"], "http_mcp": False},
                    "diagnostics": dict(self.diagnostic_settings, write_failures=self._diagnostic_failures),
                    "freshness": "Metadata checked during scans; full digest checked for inspect/read/export.",
                    "network_calls": bool(self._remote_source or self.retrieval.config.get("semantic", {}).get("enabled") or
                                          self.retrieval.config.get("rerank", {}).get("enabled")),
                    "read_only_sources": True}

    def refresh(self, force=False):
        if self._cloud_binding:
            self._check_health()
            self._source.check_local_root()
            self._storage.check()
        else:
            self._check_root()
        if not isinstance(force, bool):
            raise DriveError("INVALID_PARAMS", "force must be a boolean.")
        with self._job_lock:
            if self._pending_job:
                return {"ok": True, "job_id": self._pending_job, "status": "already_queued_or_running", "force_applied": False}
            job_id = uuid.uuid4().hex
            with self._db() as db:
                db.execute("INSERT INTO jobs(id,status,created_at) VALUES(?,'queued',?)", (job_id, time.time()))
            self._pending_job = job_id
            if force:
                self._force_jobs.add(job_id)
            self._queue.put(job_id)
            return {"ok": True, "job_id": job_id, "status": "queued"}

    def job_status(self, job_id=None):
        with self._db() as db:
            if job_id is None:
                row = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise DriveError("JOB_NOT_FOUND", "No matching indexing job exists.")
            job = dict(row)
            for field in ("scan_progress", "error_detail"):
                job[field] = json.loads(job[field]) if job[field] else None
            with self._progress_lock:
                progress = self._live_progress.get(job["id"])
                if progress is not None:
                    job["scan_progress"] = progress.snapshot()
            return {"ok": True, "job": job, "coverage": self._coverage(db),
                    "index_status": self._index_status(db)}

    def _index_status(self, db):
        coverage = self._coverage(db)
        latest = db.execute("SELECT status,error_code FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        latest_status = latest["status"] if latest else None
        if latest_status in ("queued", "running"):
            state = "scanning"
        elif latest_status == "partial":
            state = "partial"
        elif latest_status in ("failed", "interrupted", "cancelled"):
            state = "failed" if latest_status != "cancelled" else "cancelled"
        elif coverage["inventory_complete"]:
            state = "ready" if coverage["all_present_files_extracted_completely"] else "partial"
        else:
            state = "not_ready"
        return {"state": state, "inventory_complete": coverage["inventory_complete"],
                "latest_job_status": latest_status, "latest_error_code": latest["error_code"] if latest else None,
                "indexed_files": coverage["indexed_files"],
                "meaning": "Service availability is separate from inventory completion and evidence coverage."}

    def _require_share_inventory(self, db):
        # Existing version-verified candidates remain usable after a failed
        # refresh. A first, unpublished share inventory is not an empty folder.
        if self._share_binding:
            coverage = self._coverage(db)
            if not coverage["inventory_complete"] and coverage["present_files"] == 0:
                raise DriveError("INDEX_NOT_READY", "The shared inventory is not ready; an empty result cannot establish absence.",
                                 True, "Check job_status for scan progress and the task error before querying again.")

    def cancel(self, job_id):
        result = self.job_status(job_id)
        if result["job"]["status"] in ("queued", "running"):
            self._cancelled.add(job_id)
            return {"ok": True, "job_id": job_id, "status": "cancellation_requested", "boundary": "next_file_boundary"}
        return {"ok": True, "job_id": job_id, "status": result["job"]["status"]}

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    job_id = self._queue.get(timeout=self.poll_seconds)
                except queue.Empty:
                    if not self._stop.is_set():
                        try:
                            self.refresh()
                        except DriveError as exc:
                            if exc.code.startswith(("STATE_", "INDEXER_")):
                                raise
                            # Missing/rebound sources remain visible to status/read.
                    continue
                if job_id is None:
                    break
                try:
                    self.scan_once(job_id)
                except Exception as exc:
                    with self._db() as db:
                        code = exc.code if isinstance(exc, DriveError) else "SCAN_FAILED"
                        db.execute("UPDATE jobs SET status='failed',error_code=?,finished_at=? WHERE id=?", (code, time.time(), job_id))
                        self._prune_jobs(db)
                    if isinstance(exc, sqlite3.Error) or (isinstance(exc, DriveError) and exc.code.startswith("STATE_")):
                        raise
                finally:
                    with self._job_lock:
                        if self._pending_job == job_id:
                            self._pending_job = None
                    self._cancelled.discard(job_id)
                    self._force_jobs.discard(job_id)
                    self._queue.task_done()
        except Exception as exc:
            self._fatal_error = exc.code if isinstance(exc, DriveError) else "INDEX_STORAGE_FAILED"
            self._stop.set()
        finally:
            self._release_state_lock()

    def _check_health(self):
        if self._fatal_error is not None:
            raise DriveError("INDEXER_UNHEALTHY", "The index worker stopped after a storage or internal failure.", True,
                             "Retry to restart the index service; if it recurs, check available disk space and service diagnostics.")

    def _checkpoint(self, force=False):
        with self._db_lock:
            if self._sqlite_conn is not None:
                generation = self._generation(self._sqlite_conn)
                if not force and self._checkpoint_generation == generation:
                    return
                checkpoint = getattr(self._storage, "checkpoint_database", None)
                if checkpoint is not None:
                    checkpoint(self._sqlite_conn)
                    self._checkpoint_generation = generation
                    self._checkpoint_at = self._utc_now()

    def _release_state_lock(self):
        with self._db_lock:
            try:
                if self._sqlite_conn is not None and self._fatal_error is None:
                    self._checkpoint(force=True)
            except Exception:
                self._fatal_error = "STATE_CHECKPOINT_FAILED"
            finally:
                try:
                    if self._sqlite_conn is not None:
                        self._sqlite_conn.close()
                        self._sqlite_conn = None
                finally:
                    try:
                        if self._storage is not None:
                            self._storage.close()
                    finally:
                        self._source.close()

    def close(self):
        self._stop.set()
        if self._pending_job:
            self._cancelled.add(self._pending_job)
        self._queue.put(None)
        with self._process_lock:
            if self._active_process is not None and self._active_process.poll() is None:
                self._active_process.kill()
        if self._thread:
            self._thread.join(timeout=2)
        if not self._thread or not self._thread.is_alive():
            self._release_state_lock()

    def _check_root(self):
        self._check_health()
        try:
            self._source.check_root()
        except DriveError:
            if self._remote_source:
                with self._db() as db:
                    old = db.execute("SELECT value FROM meta WHERE key='inventory_complete'").fetchone()
                    if old and old[0] == "true":
                        db.execute("UPDATE meta SET value='false' WHERE key='inventory_complete'")
                        self._bump(db)
            raise
        self._storage.check()

    def _open_source(self, rel):
        self._check_root()
        fd, info = self._source.open_read(rel)
        if info.st_size > self.limits["max_file_bytes"]:
            os.close(fd)
            raise DriveError("FILE_TOO_LARGE", "The file exceeds max_file_bytes.", False, "Use a smaller approved source or deliberately adjust the limit.")
        return fd, info

    _online_only = staticmethod(online_only)

    @staticmethod
    def _fingerprint(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _inventory_time(info):
        # Windows stat/fstat ctime semantics differ in recent CPython versions.
        # Use creation time for the inventory cache, as older Windows Python did;
        # keep the live descriptor fingerprint and content digest checks intact.
        if hasattr(info, "st_network_change_ns"):
            return info.st_network_change_ns
        return getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns

    def _digest(self, rel, snapshot=None):
        if self._remote_source:
            return self._source.digest(rel, snapshot)
        fd, before = self._open_source(rel)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > self.limits["max_file_bytes"]:
                    raise DriveError("FILE_TOO_LARGE", "The source grew beyond max_file_bytes during reading.")
                digest.update(block)
                if snapshot is not None:
                    snapshot.write(block)
            after = os.fstat(source.fileno())
        if self._fingerprint(before) != self._fingerprint(after):
            raise DriveError("SOURCE_CHANGED", "The source changed during reading.", True, "Wait for writing to finish, then refresh and retry.")
        # Reopen the pathname: a replacement can leave the old open descriptor intact.
        fd2, current = self._open_source(rel)
        os.close(fd2)
        if self._fingerprint(after) != self._fingerprint(current):
            raise DriveError("SOURCE_CHANGED", "The source pathname changed during reading.", True, "Refresh and retry.")
        return digest.hexdigest(), before

    def _invalidate(self, file_id, code, message, status="stale", expected_version=None):
        with self._db() as db:
            current = db.execute("SELECT path,source_version,status FROM files WHERE id=?", (file_id,)).fetchone()
            if current is None or (expected_version is not None and current["source_version"] != expected_version):
                return
            db.execute("UPDATE files SET status=?,error_code=?,error_message=?,segments_count=0,updated_at=? WHERE id=?", (status, code, message, time.time(), file_id))
            db.execute("DELETE FROM segments WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM structure WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM search_index WHERE file_id=?", (file_id,))
            self.retrieval.delete_file(db, file_id)
            if current["status"] != "deleted":
                db.execute("INSERT INTO search_index(tokens,file_id,ordinal) VALUES(?,?,-1)", (" ".join(tokenize(current["path"])), file_id))
            self._bump(db)

    def _validated(self, file_id, expected_version=None):
        self._check_root()
        with self._db() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None or row["status"] == "deleted":
            raise DriveError("FILE_NOT_FOUND", "The file is not present in this index.", True, "Refresh or select a file from list/search.")
        if expected_version is not None and expected_version != row["source_version"]:
            raise DriveError("VERSION_MISMATCH", "The expected source version does not match the indexed version.", True, "Inspect the current source and restart the operation.")
        if row["status"] != "ready":
            raise DriveError(row["error_code"] or "FILE_NOT_READY", row["error_message"] or "The file has not been completely indexed.", True, "Check status, refresh if appropriate, and resolve the stated limitation.")
        try:
            version, _ = self._digest(row["path"])
        except DriveError as exc:
            if self._remote_source and exc.code.startswith(("GRAPH_", "AUTH_", "SHARE_")):
                # Preserve the last parsed snapshot for recovery, but never serve it
                # as verified evidence when cloud access/version checks fail.
                with self._db() as db:
                    db.execute("UPDATE meta SET value='false' WHERE key='inventory_complete'")
            else:
                self._invalidate(file_id, exc.code, exc.message, expected_version=row["source_version"])
            raise
        if version != row["source_version"]:
            self._invalidate(file_id, "STALE_SOURCE", "Source content differs from the indexed version.", expected_version=row["source_version"])
            raise DriveError("STALE_SOURCE", "Source content differs from the indexed version.", True, "Refresh and wait for the file to be indexed again.")
        return row

    def _page(self, base, key, items, limit, offset, total, binding):
        returned = []
        for item in items[:limit]:
            test = dict(base, **{key: returned + [item], "has_more": True, "next_cursor": self._cursor(binding, offset + len(returned) + 1)})
            if len(_json(test).encode()) > MAX_RESPONSE_BYTES - 1024:
                if not returned:
                    raise DriveError("RESPONSE_TOO_LARGE", "One result exceeds the response budget.", False, "Use a controlled export or reduce source segment size.")
                break
            returned.append(item)
        has_more = offset + len(returned) < total
        return dict(base, **{key: returned, "returned": len(returned), "has_more": has_more,
                            "next_cursor": self._cursor(binding, offset + len(returned)) if has_more else None,
                            "truncated": has_more})

    def list(self, prefix="", limit=20, cursor=None):
        self._check_root()
        prefix, limit = self._prefix(prefix), self._limit(limit)
        with self._db() as db:
            self._require_share_inventory(db)
            db.execute("BEGIN")
            generation = self._generation(db)
            binding = {"method": "list", "prefix": prefix, "generation": generation}
            offset = self._offset(cursor, binding)
            clause, args = "status!='deleted'", []
            if prefix:
                clause += " AND (path=? OR substr(path,1,?)=?)"
                args.extend([prefix, len(prefix) + 1, prefix + "/"])
            total = db.execute("SELECT COUNT(*) FROM files WHERE " + clause, args).fetchone()[0]
            rows = db.execute("SELECT * FROM files WHERE " + clause + " ORDER BY path LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
            base = {"ok": True, "index_generation": generation, "total_matching_files": total,
                    "coverage": self._coverage(db), "freshness": "inventory_metadata"}
        return self._verify_cloud_page(self._page(base, "files", [self._file_out(row) for row in rows], limit, offset, total, binding))

    def _search_groups(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise DriveError("INVALID_QUERY", "query must contain 1 to 500 characters.")
        variants = {query}
        for key, values in self.aliases.items():
            group = [key] + values
            for term in group:
                if term and term in query:
                    variants.update(query.replace(term, alternative) for alternative in group)
        if len(variants) > 32:
            raise DriveError("QUERY_TOO_BROAD", "Too many configured alias expansions.", False, "Use a more specific term.")
        groups = []
        for variant in sorted(variants):
            tokens = query_tokens(variant)
            if tokens and tokens not in groups:
                groups.append(tokens)
        if not groups:
            raise DriveError("INVALID_QUERY", "Use Chinese, Latin words, or numeric identifiers.")
        if sum(len(group) for group in groups) > 256:
            raise DriveError("QUERY_TOO_BROAD", "The query has too many keyword terms or alias combinations.", False,
                             "Use a shorter query or fewer alias combinations.")
        return groups

    def _search_expression(self, query):
        return " OR ".join("(" + " AND ".join('"' + token + '"' for token in group) + ")"
                           for group in self._search_groups(query))

    def search(self, query, prefix="", limit=10, cursor=None, mode="keyword", rerank=False):
        self._check_root()
        prefix, limit = self._prefix(prefix), self._limit(limit)
        if mode not in ("keyword", "hybrid", "semantic") or not isinstance(rerank, bool):
            raise DriveError("INVALID_PARAMS", "Use keyword, hybrid or semantic search and a boolean rerank flag.")
        groups = self._search_groups(query)
        if self._share_binding:
            with self._db() as db:
                self._require_share_inventory(db)
        if mode != "keyword" or rerank:
            return self._ranked_search(query, prefix, limit, cursor, mode, rerank)
        expression = " OR ".join("(" + " AND ".join('"' + token + '"' for token in group) + ")" for group in groups)
        any_terms = " OR ".join('"' + token + '"' for token in sorted({token for group in groups for token in group}))
        ctes, search_args = [], []
        for index, group in enumerate(groups):
            ctes.append("v%d AS (%s)" % (index, " INTERSECT ".join(
                "SELECT DISTINCT file_id FROM search_index WHERE search_index MATCH ?" for _ in group)))
            search_args.extend('"' + token + '"' for token in group)
        ctes.append("candidates AS (" + " UNION ".join("SELECT file_id FROM v%d" % i for i in range(len(groups))) + ")")
        ctes.append("dense AS (SELECT file_id,MIN(CAST(ordinal AS INTEGER)) ordinal FROM search_index WHERE search_index MATCH ? GROUP BY file_id)")
        ctes.append("hits AS (SELECT file_id,MIN(CASE WHEN CAST(ordinal AS INTEGER)>=0 THEN CAST(ordinal AS INTEGER) END) ordinal FROM search_index WHERE search_index MATCH ? GROUP BY file_id)")
        ctes.append("scores AS (SELECT file_id,MIN(rank) keyword_rank FROM search_index WHERE search_index MATCH ? GROUP BY file_id)")
        search_args.extend([expression, any_terms, any_terms])
        cte = "WITH " + ",".join(ctes) + " "
        with self._db() as db:
            db.execute("BEGIN")
            generation = self._generation(db)
            binding = {"method": "search", "query": query, "prefix": prefix, "generation": generation}
            offset = self._offset(cursor, binding)
            clause, args = "f.status!='deleted'", list(search_args)
            if prefix:
                clause += " AND (f.path=? OR substr(f.path,1,?)=?)"
                args.extend([prefix, len(prefix) + 1, prefix + "/"])
            join = "FROM files f JOIN candidates c ON f.id=c.file_id LEFT JOIN dense d ON f.id=d.file_id LEFT JOIN hits h ON f.id=h.file_id LEFT JOIN scores k ON f.id=k.file_id WHERE " + clause
            total = db.execute(cte + "SELECT COUNT(*) " + join, args).fetchone()[0]
            # Exact path phrase, all terms in a path, and all terms in one content
            # segment rank ahead of cross-segment matches. Path breaks ties stably.
            score = "(CASE WHEN instr(lower(f.path),?)>0 THEN 100 ELSE 0 END + CASE WHEN d.ordinal=-1 THEN 30 WHEN d.ordinal>=0 THEN 20 ELSE 0 END)"
            rows = db.execute(cte + "SELECT f.*,COALESCE(d.ordinal,h.ordinal,-1) ordinal,CASE WHEN d.file_id IS NULL THEN 1 ELSE 0 END across_segments,COALESCE(k.keyword_rank,0.0) bm25_score," + score + " relevance " + join +
                              " ORDER BY relevance DESC,bm25_score ASC,f.path LIMIT ? OFFSET ?", search_args + [query.casefold()] + args[len(search_args):] + [limit, offset]).fetchall()
            files = []
            for row in rows:
                item = self._file_out(row)
                segment = db.execute("SELECT payload FROM segments WHERE file_id=? AND ordinal=?", (row["id"], row["ordinal"])).fetchone()
                payload = json.loads(segment[0]) if segment else None
                item["match"] = {"kind": "content" if payload else "path", "ordinal": row["ordinal"],
                                 "locator": payload.get("locator") if payload else None,
                                 "snippet": payload.get("text", "")[:600] if payload else row["path"],
                                 "snippet_is_evidence": False,
                                 "terms_matched_across_segments": bool(row["across_segments"]),
                                 "relevance_score": row["relevance"], "bm25_score": row["bm25_score"]}
                files.append(item)
            base = {"ok": True, "index_generation": generation, "query": query,
                    "total_matching_files": total, "coverage": self._coverage(db),
                    "mode": "file_level_keyword_candidates", "requested_mode": "keyword", "order": "exact_path_and_phrase_then_bm25_then_path",
                    "matching_scope": "Each alias alternative requires all its terms somewhere in the same file's path or extracted text.",
                    "freshness": "indexed_not_revalidated", "exhaustive_for_business_totals": False,
                    "next_action": "Inspect and read matching sources before answering; keyword matches do not establish a complete business dataset."}
        return self._verify_cloud_page(self._page(base, "files", files, limit, offset, total, binding))

    def _ranked_search(self, query, prefix, limit, cursor, mode, rerank):
        """Freeze bounded model-assisted results for pagination within this session."""
        session = getattr(self._session, "session_id", "")
        with self._query_lock, self._db() as db:
            generation = self._generation(db)
            key = (session, generation, query, prefix, mode, rerank)
            binding = {"method": "ranked_search", "query": query, "prefix": prefix,
                       "generation": generation, "mode": mode, "rerank": rerank}
            cached = self._ranked_queries.get(key)
            if cursor and (cached is None or time.monotonic() - cached[0] > 300):
                # Authenticate first so another session's cursor remains invalid.
                self._offset(cursor, dict(binding, snapshot=None))
                raise DriveError("STALE_CURSOR", "The ranked result snapshot expired.", True, "Restart search without a cursor.")
            if not cursor and (cached is None or time.monotonic() - cached[0] > 300):
                terms = sorted({term for group in self._search_groups(query) for term in group})
                expression = " OR ".join('"' + term + '"' for term in terms)
                keyword_ids, keyword_total = None, None
                if mode == "keyword":
                    # Reordering must not change the default all-terms/alias matching
                    # condition. Filter eligible IDs before the bounded BM25 pool.
                    keyword_ids = []
                    keyword_cursor = None
                    cap = self.retrieval.config["candidate_limit"]
                    while len(keyword_ids) < cap:
                        page = self.search(query, prefix=prefix, limit=min(100, cap - len(keyword_ids)),
                                           cursor=keyword_cursor)
                        keyword_total = page["total_matching_files"]
                        keyword_ids.extend(item["file_id"] for item in page["files"])
                        keyword_cursor = page["next_cursor"]
                        if not keyword_cursor:
                            break
                ranked = self.retrieval.search(db, query, prefix=prefix, keyword_expression=expression,
                                               mode=mode, rerank=rerank, keyword_file_ids=keyword_ids,
                                               before_rerank=lambda hits, deadline: self._verify_cloud_hits(db, hits, deadline))
                if keyword_ids is not None:
                    ranked["diagnostics"].update(keyword_matching_files=keyword_total,
                                                 keyword_eligible_files=len(keyword_ids),
                                                 keyword_scope_truncated=keyword_total > len(keyword_ids))
                files, seen = [], set()
                for hit in ranked["hits"]:
                    if hit["file_id"] in seen:
                        continue
                    row = db.execute("SELECT * FROM files WHERE id=? AND source_version=? AND status='ready'",
                                     (hit["file_id"], hit["source_version"])).fetchone()
                    if row is None:
                        continue
                    seen.add(hit["file_id"])
                    segment = db.execute("SELECT payload FROM segments WHERE file_id=? AND source_version=? AND ordinal=?",
                                         (hit["file_id"], hit["source_version"], hit["ordinal"])).fetchone()
                    payload = json.loads(segment[0]) if segment else None
                    item = self._file_out(row)
                    item["match"] = {"kind": "content" if payload else "path", "ordinal": hit["ordinal"],
                                     "locator": payload.get("locator") if payload else None,
                                     "snippet": payload.get("text", "")[:600] if payload else row["path"],
                                     "snippet_is_evidence": False, "relevance_score": hit["score"],
                                     "signals": hit.get("signals", {})}
                    files.append(item)
                cached = (time.monotonic(), files, ranked["diagnostics"], uuid.uuid4().hex)
                self._ranked_queries[key] = cached
                self._ranked_queries.move_to_end(key)
                while len(self._ranked_queries) > 32:
                    self._ranked_queries.popitem(last=False)
            binding["snapshot"] = cached[3]
            offset = self._offset(cursor, binding)
            files, diagnostics = cached[1], cached[2]
            base = {"ok": True, "index_generation": generation, "query": query,
                    "requested_mode": mode, "rerank_requested": rerank,
                    "mode": "bounded_ranked_candidates", "order": "retrieval_score_then_path",
                    "total_matching_files": len(files), "total_scope": "This bounded candidate pool only; not all matching files.",
                    "coverage": self._coverage(db), "retrieval": diagnostics,
                    "freshness": "indexed_not_revalidated", "exhaustive_for_business_totals": False,
                    "next_action": "Inspect and read source versions; inspect retrieval diagnostics for unavailable or partial model indexes."}
            return self._verify_cloud_page(self._page(base, "files", files[offset:offset + limit], limit, offset, len(files), binding))

    def inspect(self, file_id, limit=20, cursor=None):
        limit = self._limit(limit)
        row = self._validated(file_id)
        binding = {"method": "inspect", "file_id": file_id, "version": row["source_version"]}
        offset = self._offset(cursor, binding)
        with self._db() as db:
            db.execute("BEGIN")
            current = db.execute("SELECT source_version,status FROM files WHERE id=?", (file_id,)).fetchone()
            if not current or current["source_version"] != row["source_version"] or current["status"] != "ready":
                raise DriveError("INDEX_CHANGED", "The index changed during inspection.", True, "Restart inspection.")
            total = db.execute("SELECT COUNT(*) FROM structure WHERE file_id=?", (file_id,)).fetchone()[0]
            groups = [{"group": json.loads(r["group_key"]), "start": r["start_ordinal"], "end": r["end_ordinal"],
                       "segments": r["end_ordinal"] - r["start_ordinal"] + 1}
                      for r in db.execute("SELECT * FROM structure WHERE file_id=? ORDER BY position LIMIT ? OFFSET ?", (file_id, limit, offset))]
            coverage = self._coverage(db)
        base = {"ok": True, "file": self._file_out(row), "coverage": coverage,
                "source_version": row["source_version"], "freshness": "digest_verified_at_read_start",
                "total_structure_groups": total, "structure_scope": "Extracted content groups, not visual document layout or business authority. Line bucket boundaries are navigation buckets, not observed file line ranges; start/end are exact segment ordinals.",
                "next_action": "Read at a group's start segment using this expected_version; all groups and their full ranges are needed for exhaustive extraction."}
        return self._page(base, "structure", groups, limit, offset, total, binding)

    @staticmethod
    def _selector(selector, suffix):
        if selector is None:
            return "", [], None
        allowed = {"sheet", "row_start", "row_end", "page", "slide"}
        if not isinstance(selector, dict) or not selector or set(selector) - allowed:
            raise DriveError("INVALID_SELECTOR", "selector must contain supported nonempty location fields.")
        for key, value in selector.items():
            if key == "sheet":
                if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
                    raise DriveError("INVALID_SELECTOR", "sheet must be a nonempty worksheet name.")
            elif isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 2147483647:
                raise DriveError("INVALID_SELECTOR", "Row, page, and slide numbers must be positive integers.")
        if "row_start" in selector and "row_end" in selector and selector["row_start"] > selector["row_end"]:
            raise DriveError("INVALID_SELECTOR", "row_start cannot exceed row_end.")
        keys = set(selector)
        valid = ((suffix in (".csv", ".tsv") and keys <= {"row_start", "row_end"}) or
                 (suffix == ".xlsx" and "sheet" in keys and keys <= {"sheet", "row_start", "row_end"}) or
                 (suffix == ".pdf" and keys == {"page"}) or
                 (suffix == ".pptx" and keys == {"slide"}))
        if not valid:
            raise DriveError("UNSUPPORTED_SELECTOR", "Use rows for CSV/TSV, sheet with optional rows for XLSX, page for PDF, or slide for PPTX.", False, "Inspect the source and choose a selector compatible with its format.")
        clauses, values = [], []
        for field in ("sheet", "page", "slide"):
            if field in selector:
                clauses.append("json_extract(payload,'$.locator." + field + "')=?")
                values.append(selector[field])
        for field, operator in (("row_start", ">="), ("row_end", "<=")):
            if field in selector:
                clauses.append("json_extract(payload,'$.locator.row')" + operator + "?")
                values.append(selector[field])
        return " AND " + " AND ".join(clauses), values, dict(selector)

    def read(self, file_id, start=0, limit=10, expected_version=None, cursor=None, selector=None):
        limit = self._limit(limit)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise DriveError("INVALID_RANGE", "start must be a nonnegative segment offset.")
        row = self._validated(file_id, expected_version)
        selector_sql, selector_values, selector = self._selector(selector, row["suffix"])
        binding = {"method": "read", "file_id": file_id, "version": row["source_version"], "selector": selector}
        offset = self._offset(cursor, binding) if cursor else start
        with self._db() as db:
            db.execute("BEGIN")
            where = "file_id=? AND source_version=?" + selector_sql
            values = [file_id, row["source_version"]] + selector_values
            rows = db.execute("SELECT ordinal,payload FROM segments WHERE " + where + " ORDER BY ordinal LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
            items = [dict(json.loads(r["payload"]), ordinal=r["ordinal"]) for r in rows]
            total = db.execute("SELECT COUNT(*) FROM segments WHERE file_id=? AND source_version=?", (file_id, row["source_version"])).fetchone()[0]
            matched = db.execute("SELECT COUNT(*) FROM segments WHERE " + where, values).fetchone()[0]
        if total != row["segments_count"]:
            raise DriveError("INDEX_CHANGED", "The index changed during this read.", True, "Inspect and restart the read.")
        base = {"ok": True, "file_id": file_id, "path": row["path"], "source_version": row["source_version"],
                "origin": json.loads(row["origin"]),
                "total_segments": total, "start": offset, "freshness": "digest_verified_at_read_start",
                "extraction_complete": bool(row["extraction_complete"]), "format_warnings": json.loads(row["format_warnings"]),
                "content_coverage": json.loads(row["content_coverage"]),
                "extraction_scope": "Supported extracted content; see format_warnings for omitted content.",
                "selector": selector, "selection_scope": "filtered_segments" if selector else "whole_file_segments",
                "total_matching_segments": matched,
                "business_completeness": "not_established",
                "scan_resume": ResumableScan.status(db, self._share_binding and getattr(self._source, "resumable", False))}
        result = self._page(base, "segments", items, limit, offset, matched, binding)
        result["whole_file_in_response"] = selector is None and offset == 0 and result["returned"] == total
        result["selection_complete_in_response"] = offset == 0 and result["returned"] == matched
        return result

    def context(self, file_id, expected_version, ordinal, before=2, after=2, max_segments=30):
        if not isinstance(expected_version, str) or not expected_version:
            raise DriveError("EXPECTED_VERSION_REQUIRED", "Context requires the inspected source version.")
        row = self._validated(file_id, expected_version)
        with self._db() as db:
            db.execute("BEGIN")
            result = select_context(db, file_id, expected_version, ordinal, before, after, max_segments)
            total = db.execute("SELECT COUNT(*) FROM segments WHERE file_id=? AND source_version=?",
                               (file_id, expected_version)).fetchone()[0]
        if total != row["segments_count"]:
            raise DriveError("INDEX_CHANGED", "The index changed during context selection.", True, "Inspect and restart.")
        self._validated(file_id, expected_version)
        result.update(path=row["path"], total_segments=total,
                      origin=json.loads(row["origin"]),
                      freshness="digest_verified_before_and_after_context",
                      extraction_complete=bool(row["extraction_complete"]),
                      format_warnings=json.loads(row["format_warnings"]),
                      content_coverage=json.loads(row["content_coverage"]))
        return result

    def plan(self, query, prefix=""):
        return evidence_plan(query, self._prefix(prefix))

    def diagnostics(self, limit=20):
        limit = self._limit(limit)
        with self._db() as db:
            result = operational.report(db, self.diagnostic_settings, limit)
        result["write_failures"] = self._diagnostic_failures
        return result

    def _record_operation(self, operation, started, result):
        if not self.diagnostic_settings["enabled"]:
            return
        try:
            with self._db() as db:
                operational.record(db, self.diagnostic_settings, operation,
                                   (time.perf_counter() - started) * 1000,
                                   result.get("ok", False), result.get("error", {}).get("code"))
        except Exception:
            # Optional metrics must not replace an evidence result or leak content.
            self._diagnostic_failures += 1

    def export(self, file_id, expected_version, format="jsonl"):
        if format != "jsonl":
            raise DriveError("UNSUPPORTED_EXPORT", "Only JSONL exports are supported.")
        if not isinstance(expected_version, str) or not expected_version:
            raise DriveError("EXPECTED_VERSION_REQUIRED", "Export requires an expected source version.")
        row = self._validated(file_id, expected_version)
        with self._storage.directory("exports", create=True) as folder:
            return self._export_guarded(row, file_id, expected_version, folder)

    def _export_guarded(self, row, file_id, expected_version, folder):
        artifact_id = "exports/" + uuid.uuid4().hex + ".jsonl"
        path = folder / PurePosixPath(artifact_id).name
        count = 0
        try:
            with self._storage.new_private(path) as output, self._db() as db:
                db.execute("BEGIN")
                for item in db.execute("SELECT ordinal,payload FROM segments WHERE file_id=? AND source_version=? ORDER BY ordinal", (file_id, expected_version)):
                    output.write(_json({"file_id": file_id, "path": row["path"], "source_version": expected_version,
                                        "origin": json.loads(row["origin"]),
                                        "ordinal": item["ordinal"], "segment": json.loads(item["payload"])}) + "\n")
                    count += 1
            if count != row["segments_count"]:
                raise DriveError("INDEX_CHANGED", "The index changed during export.", True, "Inspect and retry.")
            self._validated(file_id, expected_version)
        except Exception:
            self._storage.unlink_private(path)
            raise
        return {"ok": True, "artifact_id": artifact_id, "artifact_path": str(path),
                "artifact_path_scope": "Local runtime path for analysis tools; do not publish runtime traces or exports.",
                "format": "jsonl", "row_count": count,
                "source_version": expected_version, "extraction_complete": bool(row["extraction_complete"]),
                "origin": json.loads(row["origin"]),
                "content_coverage": json.loads(row["content_coverage"]),
                "format_warnings": json.loads(row["format_warnings"]),
                "business_completeness": "not_established", "includes_original_binary": False}

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def evidence_bundle(self, files, scope_description=""):
        """Export exactly the selected versions with a durable, explicit gap manifest."""
        if not isinstance(files, list) or not 1 <= len(files) <= 50:
            raise DriveError("INVALID_PARAMS", "files must contain between 1 and 50 explicit file selections.")
        if not isinstance(scope_description, str) or len(scope_description) > 2000:
            raise DriveError("INVALID_PARAMS", "scope_description must contain at most 2000 characters.")
        seen = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"file_id", "expected_version"}:
                raise DriveError("INVALID_PARAMS", "Each selection must contain only file_id and expected_version.")
            if not isinstance(item["file_id"], str) or not 1 <= len(item["file_id"]) <= 128:
                raise DriveError("INVALID_PARAMS", "Each file_id must be a nonempty identifier of at most 128 characters.")
            if not isinstance(item["expected_version"], str) or not 1 <= len(item["expected_version"]) <= 256:
                raise DriveError("EXPECTED_VERSION_REQUIRED", "Every selected file requires its expected source version.")
            if item["file_id"] in seen:
                raise DriveError("INVALID_PARAMS", "Select each file only once.")
            seen.add(item["file_id"])
        self._check_root()
        started = self._utc_now()
        entries = []
        for item in files:
            entry = dict(item, observed_at=self._utc_now(), status="failed")
            try:
                inspected = self.inspect(item["file_id"], limit=1)
                entry.update(path=inspected["file"]["path"], observed_version=inspected["source_version"],
                             origin=inspected["file"]["origin"])
                exported = self.export(item["file_id"], item["expected_version"])
                entry.update(status="exported", source_version=exported["source_version"],
                             artifact_id=exported["artifact_id"], artifact_path=exported["artifact_path"],
                             segment_count=exported["row_count"], extraction_complete=exported["extraction_complete"],
                             content_coverage=exported["content_coverage"], format_warnings=exported["format_warnings"],
                             exported_at=self._utc_now())
            except DriveError as exc:
                entry["error"] = failure(exc.code, exc.message, exc.retryable, exc.next_action)["error"]
            entries.append(entry)
        exported_count = sum(entry["status"] == "exported" for entry in entries)
        complete_count = sum(entry.get("extraction_complete", False) for entry in entries)
        manifest = {
            "schema": "df-mcp-evidence-bundle-v1", "created_at": self._utc_now(),
            "source": {"provider": self.provider, "root": None if self._cloud_binding else str(self.root),
                       "cloud": self._cloud_binding, "share": self._share_binding, "source_id": self._root_binding,
                       "root_identity": list(self._root_identity)},
            "scope": {"selection": "exactly_explicit_file_ids", "description": scope_description,
                      "description_authority": "user_supplied_not_verified", "requested_files": len(files)},
            "observation_interval": {"started_at": started, "ended_at": self._utc_now()},
            "snapshot_semantics": "Sequential version-pinned reads; not an atomic snapshot of the folder.",
            "exported_files": exported_count, "failed_files": len(files) - exported_count,
            "all_selected_versions_exported": exported_count == len(files),
            "all_selected_text_complete": complete_count == len(files),
            "business_completeness": "not_established", "files": entries,
            "gaps": [{"file_id": entry["file_id"], "error": entry.get("error"),
                      "content_coverage": entry.get("content_coverage", {})}
                     for entry in entries if entry["status"] != "exported" or not entry.get("extraction_complete", False)],
            "interpretation": "Exports contain supported extracted segments, including explicit gaps. Review per-file format warnings; no calculations or business completeness are asserted."
        }
        artifact_id = "bundles/" + uuid.uuid4().hex + ".json"
        with self._storage.directory("bundles", create=True) as folder:
            path = folder / PurePosixPath(artifact_id).name
            try:
                with self._storage.new_private(path) as output:
                    output.write(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            except Exception:
                self._storage.unlink_private(path)
                raise
        return {"ok": True, "artifact_id": artifact_id, "artifact_path": str(path),
                "artifact_path_scope": "Local evidence manifest; contains source paths and must not be published without review.",
                "requested_files": len(files), "exported_files": exported_count, "failed_files": len(files) - exported_count,
                "all_selected_versions_exported": exported_count == len(files),
                "all_selected_text_complete": complete_count == len(files),
                "business_completeness": "not_established", "observation_interval": manifest["observation_interval"],
                "files": [{"file_id": entry["file_id"], "status": entry["status"],
                           "error_code": entry.get("error", {}).get("code"),
                           "extraction_complete": entry.get("extraction_complete", False)} for entry in entries]}

    def wiki_status(self, limit=20):
        return WikiStore(self).status(limit)

    def wiki_search(self, query, limit=10, after=""):
        return WikiStore(self).search(query, limit, after)

    def wiki_read(self, page_id):
        return WikiStore(self).read(page_id)

    def wiki_write(self, page_id, kind, title, body, citations, expected_revision=0, links=None):
        return WikiStore(self).write(page_id, kind, title, body, citations, expected_revision, links)

    def wiki_refresh(self, limit=20, after=""):
        return WikiStore(self).refresh(limit, after)

    def call(self, method, params=None, session_id=None):
        methods = {name: getattr(self, name) for name in ("status", "refresh", "job_status", "cancel", "list", "search", "inspect", "read", "context", "plan", "diagnostics", "export", "evidence_bundle", "wiki_status", "wiki_search", "wiki_read", "wiki_write", "wiki_refresh")}
        if not isinstance(method, str) or method not in methods:
            return failure("UNKNOWN_METHOD", "The requested method is not supported.")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return failure("INVALID_PARAMS", "params must be an object.")
        if session_id is not None and (not isinstance(session_id, str) or not 1 <= len(session_id) <= 256):
            return failure("INVALID_SESSION", "The service session identifier is invalid.")
        previous_session = getattr(self._session, "session_id", "")
        self._session.session_id = session_id or ""
        started = time.perf_counter()
        result = failure("INTERNAL_ERROR", "The operation did not finish.")
        try:
            self._check_health()
            result = methods[method](**params)
            if len(_json(result).encode()) > MAX_RESPONSE_BYTES:
                result = failure("RESPONSE_TOO_LARGE", "The response exceeds the bounded output size.", False, "Narrow the query or reduce the page size.")
            return result
        except DriveError as exc:
            result = failure(exc.code, exc.message, exc.retryable, exc.next_action)
            return result
        except TypeError:
            result = failure("INVALID_PARAMS", "Parameters do not match this tool's input schema.")
            return result
        except Exception:
            result = failure("INTERNAL_ERROR", "The operation could not complete.", True, "Check local service health; no source content was modified.")
            return result
        finally:
            self._session.session_id = previous_session
            self._record_operation(method, started, result)

    def scan_once(self, job_id=None, force=False):
        """Synchronous scan used by the worker thread and deterministic tests."""
        # Keep the completed inventory and its final telemetry/checkpoint in
        # one scan boundary, including for concurrent synchronous callers.
        with self._scan_lock:
            return self._scan_tracked(job_id, force)

    def _scan_tracked(self, job_id, force):
        if job_id is None:
            job_id = uuid.uuid4().hex
            with self._db() as db:
                db.execute("INSERT INTO jobs(id,status,created_at) VALUES(?,'queued',?)", (job_id, time.time()))
        progress = ScanProgress()
        started = time.perf_counter()
        with self._progress_lock:
            self._live_progress[job_id] = progress
        try:
            result = self._scan_once(job_id, force, progress)
            outcome = result["job"]["status"]
            progress.finish(outcome)
            detail = error_detail(DriveError(result["job"]["error_code"], ""), progress) if outcome in ("failed", "partial") else None
            with self._db() as db:
                db.execute("UPDATE jobs SET scan_progress=?,error_detail=? WHERE id=?",
                           (_json(progress.snapshot()), _json(detail) if detail else None, job_id))
            self._checkpoint(force=outcome != "complete" or bool(self._share_binding and getattr(self._source, "resumable", False)))
            return self.job_status(job_id)
        except Exception as exc:
            progress.finish("failed")
            detail = error_detail(exc, progress)
            with self._db() as db:
                db.execute("UPDATE jobs SET status='failed',error_code=?,finished_at=?,scan_progress=?,error_detail=? WHERE id=?",
                           (detail["code"], time.time(), _json(progress.snapshot()), _json(detail), job_id))
                self._prune_jobs(db)
            self._record_operation("scan", started, {"ok": False, "error": {"code": detail["code"]}})
            self._checkpoint(force=True)
            raise
        finally:
            with self._progress_lock:
                self._live_progress.pop(job_id, None)

    def _scan_once(self, job_id, force, progress):
        if self._share_binding and getattr(self._source, "resumable", False):
            return ResumableScan(self).run(job_id, force or job_id in self._force_jobs, progress)
        started = time.perf_counter()
        if self._remote_source:
            self._check_health()
            if self._cloud_binding:
                self._source.check_local_root()
            self._storage.check()
        else:
            self._check_root()
        with self._scan_lock:
            scan_id = uuid.uuid4().hex
            force = force or job_id in self._force_jobs
            if job_id is None:
                job_id = uuid.uuid4().hex
                with self._db() as db:
                    db.execute("INSERT INTO jobs(id,status,created_at) VALUES(?,'queued',?)", (job_id, time.time()))
            with self._db() as db:
                db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (time.time(), job_id))
                db.execute("UPDATE meta SET value='false' WHERE key='inventory_complete'")
            complete = True
            discovered = 0
            walk_errors = []
            progress.set_stage("enumerating")
            entries = self._source.walk(walk_errors, progress=progress) if self._share_binding else self._source.walk(walk_errors)
            for relative, info, blocked, online_only in entries:
                if job_id in self._cancelled or self._stop.is_set():
                    complete = False
                    break
                self._publish_inventory_entry(relative, info, blocked, online_only, scan_id, force)
                discovered += 1
            complete = complete and not walk_errors
            self._check_root()
            with self._db() as db:
                if complete:
                    removed = [r[0] for r in db.execute("SELECT id FROM files WHERE seen_scan!=? AND status!='deleted'", (scan_id,))]
                    for file_id in removed:
                        db.execute("UPDATE files SET status='deleted',source_version=NULL,segments_count=0,error_code=NULL,error_message=NULL,origin='{}' WHERE id=?", (file_id,))
                        db.execute("DELETE FROM segments WHERE file_id=?", (file_id,))
                        db.execute("DELETE FROM structure WHERE file_id=?", (file_id,))
                        db.execute("DELETE FROM search_index WHERE file_id=?", (file_id,))
                        self.retrieval.delete_file(db, file_id)
                    if removed:
                        self._bump(db)
                db.execute("UPDATE meta SET value=? WHERE key='inventory_complete'", ("true" if complete else "false",))
                db.execute("UPDATE jobs SET discovered=? WHERE id=?", (discovered, job_id))
            processed = 0
            if complete:
                progress.set_stage("parsing")
            last_path = ""
            while complete and job_id not in self._cancelled and not self._stop.is_set():
                with self._db() as db:
                    row = db.execute("SELECT * FROM files WHERE status='pending' AND path>? ORDER BY path LIMIT 1", (last_path,)).fetchone()
                if row is None:
                    break
                last_path = row["path"]
                self._parse_file(row)
                processed += 1
                with self._db() as db:
                    db.execute("UPDATE jobs SET processed=? WHERE id=?", (processed, job_id))
            cancelled = job_id in self._cancelled or self._stop.is_set()
            if complete and not cancelled:
                self._index_semantic_pending(job_id)
            cancelled = job_id in self._cancelled or self._stop.is_set()
            with self._db() as db:
                db.execute("UPDATE jobs SET status=?,finished_at=?,error_code=? WHERE id=?", ("cancelled" if cancelled else "complete" if complete else "failed", time.time(), None if complete else "INVENTORY_INCOMPLETE", job_id))
                db.execute("UPDATE meta SET value=? WHERE key='last_scan_at'", (_json(time.time()),))
                self._prune_jobs(db)
            result = self.job_status(job_id)
            self._record_operation("scan", started, {"ok": complete and not cancelled,
                                   "error": {"code": "CANCELLED" if cancelled else "INVENTORY_INCOMPLETE" if not complete else None}})
            return result

    def _index_semantic_pending(self, job_id):
        if not self.retrieval.config["semantic"]["enabled"]:
            return
        # Text extraction stays usable if the optional model is unavailable.
        # One scan spends at most a bounded window filling the vector cache;
        # later scans resume from persistent segment digests.
        deadline = time.monotonic() + 20
        with self._db() as db:
            pending_vectors = db.execute("SELECT * FROM files WHERE status='ready' ORDER BY path").fetchall()
        for vector_file in pending_vectors:
            if time.monotonic() >= deadline or self._stop.is_set() or job_id in self._cancelled:
                break
            vector_start = time.perf_counter()
            self._verify_cloud_page({"files": [self._file_out(vector_file)]}, deadline=deadline)
            with self._db() as db:
                indexed = self.retrieval.index_file(db, vector_file["id"], vector_file["source_version"],
                                                   deadline=deadline)
                if indexed.get("newly_indexed_segments", 0):
                    self._bump(db)
            self._record_operation("semantic_index", vector_start,
                                   {"ok": not indexed.get("error_code"),
                                    "error": {"code": indexed.get("error_code")}})
            if indexed.get("error_code"):
                break

    def _publish_inventory_entry(self, relative, info, blocked, online_only, scan_id, force):
        file_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:32]
        suffix = PurePosixPath(relative).suffix.lower()
        initial = "blocked" if blocked else "online_only" if online_only else "oversized" if info.st_size > self.limits["max_file_bytes"] else "unsupported" if suffix not in SUPPORTED_SUFFIXES else "pending"
        reason = {"blocked": ("UNSAFE_FILE", "Links, reparse points, multiply linked files, and nonregular objects are not read."),
                  "online_only": ("ONLINE_ONLY", "The file is an online-only placeholder and will not be hydrated automatically."),
                  "oversized": ("FILE_TOO_LARGE", "The file exceeds max_file_bytes."),
                  "unsupported": ("UNSUPPORTED_FORMAT", "This file format is not supported.")}.get(initial, (None, None))
        with self._db() as db:
            old = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            stable_state = old is not None and (
                (initial == "pending" and old["status"] in ("ready", "failed")) or
                (initial != "pending" and old["status"] == initial and
                 old["error_code"] == reason[0] and old["error_message"] == reason[1]))
            unchanged = (stable_state and old["size"] == info.st_size and
                         old["mtime_ns"] == info.st_mtime_ns and old["ctime_ns"] == self._inventory_time(info) and
                         (not self._share_binding or json.loads(old["origin"]).get("file_identity") ==
                          self._source.evidence(relative).get("file_identity")) and
                         not force)
            if unchanged:
                db.execute("UPDATE files SET seen_scan=? WHERE id=?", (scan_id, file_id))
            else:
                db.execute("INSERT INTO files(id,path,suffix,size,mtime_ns,ctime_ns,status,error_code,error_message,seen_scan,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,suffix=excluded.suffix,size=excluded.size,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,status=excluded.status,error_code=excluded.error_code,error_message=excluded.error_message,source_version=NULL,segments_count=0,seen_scan=excluded.seen_scan,updated_at=excluded.updated_at", (file_id, relative, suffix, info.st_size, info.st_mtime_ns, self._inventory_time(info), initial, reason[0], reason[1], scan_id, time.time()))
                db.execute("DELETE FROM segments WHERE file_id=?", (file_id,))
                db.execute("DELETE FROM structure WHERE file_id=?", (file_id,))
                db.execute("DELETE FROM search_index WHERE file_id=?", (file_id,))
                self.retrieval.delete_file(db, file_id)
                db.execute("UPDATE files SET origin=? WHERE id=?", (
                    _json(self._source.evidence(relative)) if self._remote_source else "{}", file_id))
                db.execute("INSERT INTO search_index(tokens,file_id,ordinal) VALUES(?,?,-1)", (" ".join(tokenize(relative)), file_id))
                self._bump(db)

    def _parse_file(self, row):
        started = time.perf_counter()
        name = "job-temp-" + uuid.uuid4().hex
        try:
            with self._storage.directory(name, create=True) as work_dir:
                self._parse_file_guarded(row, work_dir)
            self._storage.remove_empty_directory(name)
        except DriveError as exc:
            self._parse_failure(row["id"], exc.code, exc.message)
        finally:
            with self._db() as db:
                state = db.execute("SELECT status,error_code FROM files WHERE id=?", (row["id"],)).fetchone()
            self._record_operation("parse", started, {"ok": bool(state and state["status"] == "ready"),
                                   "error": {"code": state["error_code"] if state else "FILE_NOT_FOUND"}})

    def _parse_failure(self, file_id, code, message):
        if self._stop.is_set():
            self._invalidate(file_id, "PARSE_INTERRUPTED", "Parsing stopped during service shutdown; it will be retried on the next scan.", "pending")
        else:
            self._invalidate(file_id, code, message, "failed")

    def _parse_file_guarded(self, row, work_dir):
        snapshot_path = None
        parsed_path = None
        try:
            snapshot_path = work_dir / ("snapshot-" + uuid.uuid4().hex + row["suffix"])
            with self._storage.new_private(snapshot_path, binary=True) as snapshot:
                version, info = self._digest(row["path"], snapshot)
            parsed_path = work_dir / ("parsed-" + uuid.uuid4().hex + ".jsonl")
            with self._storage.new_private(parsed_path):
                pass
            count, warnings, content_coverage = self._extract(snapshot_path, parsed_path)
            current_version, current_info = self._digest(row["path"])
            if current_version != version:
                raise DriveError("SOURCE_CHANGED", "The source changed during parsing.", True, "Wait for writing to finish, then refresh.")
            with self._db() as db, self._storage.open_private(parsed_path, "r") as parsed:
                db.execute("DELETE FROM segments WHERE file_id=?", (row["id"],))
                db.execute("DELETE FROM structure WHERE file_id=?", (row["id"],))
                db.execute("DELETE FROM search_index WHERE file_id=?", (row["id"],))
                db.execute("INSERT INTO search_index(tokens,file_id,ordinal) VALUES(?,?,-1)", (" ".join(tokenize(row["path"])), row["id"]))
                group_position, group_start, group_key, group_end = 0, 0, None, 0
                for ordinal, line in enumerate(parsed):
                    payload = json.loads(line)
                    db.execute("INSERT INTO segments VALUES(?,?,?,?)", (row["id"], ordinal, version, line.strip()))
                    db.execute("INSERT INTO search_index(tokens,file_id,ordinal) VALUES(?,?,?)", (" ".join(tokenize(payload.get("text", ""))), row["id"], ordinal))
                    key = self._structure_group(payload.get("locator", {}))
                    if group_key is not None and key != group_key:
                        db.execute("INSERT INTO structure VALUES(?,?,?,?,?)", (row["id"], group_position, group_key, group_start, ordinal - 1))
                        group_position, group_start = group_position + 1, ordinal
                    group_key, group_end = key, ordinal
                if group_key is not None:
                    db.execute("INSERT INTO structure VALUES(?,?,?,?,?)", (row["id"], group_position, group_key, group_start, group_end))
                complete = not content_coverage.get("missing_text_pages")
                db.execute("UPDATE files SET source_version=?,status='ready',error_code=NULL,error_message=NULL,segments_count=?,size=?,mtime_ns=?,ctime_ns=?,format_warnings=?,extraction_complete=?,content_coverage=?,updated_at=? WHERE id=?", (version, count, current_info.st_size, current_info.st_mtime_ns, self._inventory_time(current_info), _json(warnings), int(complete), _json(content_coverage), time.time(), row["id"]))
                if self._remote_source:
                    origin = self._source.evidence(row["path"])
                    if origin.get("source_version") != version:
                        raise DriveError("SOURCE_CHANGED", "The cloud evidence metadata changed during parsing.", True, "Refresh and retry.")
                    db.execute("UPDATE files SET origin=? WHERE id=?", (_json(origin), row["id"]))
                self._bump(db)
        except DriveError as exc:
            self._parse_failure(row["id"], exc.code, exc.message)
        except Exception:
            self._parse_failure(row["id"], "PARSE_FAILED", "The file could not be parsed safely.")
        finally:
            if snapshot_path:
                self._storage.unlink_private(snapshot_path)
            if parsed_path:
                self._storage.unlink_private(parsed_path)

    @staticmethod
    def _structure_group(locator):
        if not isinstance(locator, dict):
            return _json({"kind": "document"})
        for key in ("sheet", "page", "slide", "heading", "table"):
            if locator.get(key) is not None:
                return _json({key: locator[key]})
        # Long plain text is navigable in 200-line blocks.
        if "line_start" in locator:
            start = ((locator["line_start"] - 1) // 200) * 200 + 1
            return _json({"kind": "line_bucket", "bucket_line_start": start, "bucket_line_end": start + 199})
        return _json({"kind": locator.get("kind", "document")})

    def _extract(self, snapshot_path, parsed_path):
        environment = os.environ.copy()
        environment.update({"TMPDIR": str(snapshot_path.parent), "TMP": str(snapshot_path.parent), "TEMP": str(snapshot_path.parent), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
        package_parent = str(Path(__file__).resolve().parent.parent)
        inherited_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = package_parent + (os.pathsep + inherited_pythonpath if inherited_pythonpath else "")
        process = subprocess.Popen([sys.executable, "-m", "team_drive.extract", str(snapshot_path), _json(self.limits)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
        with self._process_lock:
            self._active_process = process
        timed_out = threading.Event()
        def stop_parser():
            timed_out.set()
            process.kill()
        timer = threading.Timer(self.limits["parse_timeout_seconds"], stop_parser)
        timer.daemon = True
        timer.start()
        count, total_bytes, done = 0, 0, False
        warnings = set()
        pages, missing_pages = set(), set()
        try:
            with self._storage.open_private(parsed_path, "w") as output:
                while True:
                    line = process.stdout.readline(MAX_SEGMENT_BYTES + 2048)
                    if not line:
                        break
                    total_bytes += len(line)
                    if len(line) >= MAX_SEGMENT_BYTES + 2048 or total_bytes > self.limits["max_uncompressed_bytes"]:
                        raise DriveError("PARSE_OUTPUT_LIMIT", "Extracted content exceeds the configured output budget.", False, "Use smaller source files or deliberately adjust limits.")
                    try:
                        item = json.loads(line)
                    except ValueError:
                        raise DriveError("PARSER_PROTOCOL_ERROR", "The parser returned an invalid result.")
                    if item.get("error"):
                        err = item["error"]
                        # Only controlled parser codes/messages; parser does not expose local paths.
                        raise DriveError(str(err.get("code", "PARSE_FAILED"))[:80], str(err.get("message", "Parsing failed."))[:600])
                    if item.get("done") is True:
                        done = True
                        continue
                    segment = item.get("segment")
                    if done or not isinstance(segment, dict) or not isinstance(segment.get("text"), str) or "locator" not in segment:
                        raise DriveError("PARSER_PROTOCOL_ERROR", "The parser returned an invalid segment.")
                    if len(segment["text"]) > self.limits["max_segment_chars"] or len(_json(segment).encode()) > MAX_SEGMENT_BYTES:
                        raise DriveError("SEGMENT_TOO_LARGE", "An extracted segment exceeds the response budget.", False, "Reduce source column widths or adjust segmentation.")
                    count += 1
                    warnings.update(str(value)[:200] for value in segment.get("warnings", []))
                    if snapshot_path.suffix == ".pdf":
                        page = segment.get("locator", {}).get("page")
                        if isinstance(page, int):
                            pages.add(page)
                            if "no_extractable_text_ocr_may_be_required" in segment.get("warnings", []):
                                missing_pages.add(page)
                    if count > self.limits["max_segments"]:
                        raise DriveError("SEGMENT_LIMIT", "The file exceeds max_segments.")
                    output.write(_json(segment) + "\n")
            code = process.wait()
            if timed_out.is_set():
                raise DriveError("PARSE_TIMEOUT", "Parsing exceeded parse_timeout_seconds.", True, "Use a smaller source or deliberately adjust the timeout.")
            if code != 0 or not done:
                raise DriveError("PARSE_FAILED", "The parser did not complete successfully.")
            coverage = ({"unit": "page", "total_pages": len(pages), "pages_with_text": len(pages - missing_pages),
                         "missing_text_pages": sorted(missing_pages)[:2000],
                         "missing_text_page_count": len(missing_pages),
                         "missing_text_pages_truncated": len(missing_pages) > 2000,
                         "gap_locations_scope": "First 2000 missing pages; every gap remains a located segment in the full export.",
                         "ocr_performed": False,
                         "gap_interpretation": "Blank and image-only pages are not distinguished."}
                        if snapshot_path.suffix == ".pdf" else {})
            return count, sorted(warnings), coverage
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()
            with self._process_lock:
                if self._active_process is process:
                    self._active_process = None
