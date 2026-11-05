"""Persistent derived Wiki with original-source verification and bounded repair queues.

The SQLite checkpoint is authoritative. Markdown is an atomic, disposable mirror,
never an input to source verification. A host model writes drafts from original
file evidence; no model endpoint or unattended model job is introduced here.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from .errors import DriveError

PAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
FILE_ID = re.compile(r"[0-9a-f]{32}\Z")
VERSION = re.compile(r"[0-9a-f]{64}\Z")
KINDS = {"source", "topic", "faq", "conflict"}
MAX_PAGE_BYTES = 24576
VERIFY_SECONDS = 20
EVENT_LIMIT = 1000


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _error(message):
    raise DriveError("INVALID_WIKI_INPUT", message)


def _id(value, empty=False):
    if empty and value == "":
        return value
    if not isinstance(value, str) or PAGE_ID.fullmatch(value) is None:
        _error("page_id must use 1 to 64 lowercase letters, digits or hyphens, starting with a letter or digit.")
    return value


def _limit(value, maximum=100):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        _error("limit must be an integer from 1 to %d." % maximum)
    return value


def _tokens(value):
    folded = value.casefold()
    tokens = {"w" + word for word in re.findall(r"[a-z0-9]+", folded)}
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", folded):
        tokens.update("c" + char.encode("utf-8").hex() for char in run)
        tokens.update("b" + run[i:i + 2].encode("utf-8").hex() for i in range(len(run) - 1))
    return sorted(tokens)


def _query_tokens(value):
    folded = value.casefold()
    tokens = {"w" + word for word in re.findall(r"[a-z0-9]+", folded)}
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", folded):
        if len(run) == 1:
            tokens.add("c" + run.encode("utf-8").hex())
        else:
            tokens.update("b" + run[i:i + 2].encode("utf-8").hex() for i in range(len(run) - 1))
    return sorted(tokens)


class WikiStore:
    def __init__(self, engine):
        self.engine = engine
        # Normally initialized during Engine._init_db; lazy initialization keeps
        # the helper usable in migrations and isolated engine acceptance tests.
        with engine._db() as db:
            self.init_schema(db)

    @staticmethod
    def init_schema(db):
        # execute individually: executescript would commit the caller's scan.
        statements = (
            "CREATE TABLE IF NOT EXISTS wiki_pages (page_id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,citations TEXT NOT NULL,links TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,verified_at REAL)",
            "CREATE TABLE IF NOT EXISTS wiki_dependencies (page_id TEXT NOT NULL,file_id TEXT NOT NULL,source_version TEXT NOT NULL,PRIMARY KEY(page_id,file_id))",
            "CREATE INDEX IF NOT EXISTS wiki_dependencies_file ON wiki_dependencies(file_id,page_id)",
            "CREATE TABLE IF NOT EXISTS wiki_events (id INTEGER PRIMARY KEY AUTOINCREMENT,page_id TEXT NOT NULL,event TEXT NOT NULL,reason TEXT,at REAL NOT NULL)",
            "CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search USING fts5(tokens,page_id UNINDEXED,tokenize='ascii')",
        )
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _event(db, page_id, event, reason=None):
        db.execute("INSERT INTO wiki_events(page_id,event,reason,at) VALUES(?,?,?,?)", (page_id, event, reason, time.time()))
        db.execute("DELETE FROM wiki_events WHERE id NOT IN (SELECT id FROM wiki_events ORDER BY id DESC LIMIT ?)", (EVENT_LIMIT,))

    @staticmethod
    def invalidate_changed(db):
        """Only invalidate pages whose original-file dependencies changed.

        Missing an item during an incomplete scan does not change its file row;
        consequently incomplete inventory never becomes deletion evidence here.
        No source text or title is returned by this database-only hook.
        """
        rows = db.execute("SELECT DISTINCT p.page_id FROM wiki_pages p JOIN wiki_dependencies d ON d.page_id=p.page_id LEFT JOIN files f ON f.id=d.file_id WHERE p.state='current' AND (f.id IS NULL OR f.status!='ready' OR f.source_version IS NULL OR f.source_version!=d.source_version)").fetchall()
        for row in rows:
            db.execute("UPDATE wiki_pages SET state='stale',reason='SOURCE_INDEX_CHANGED' WHERE page_id=?", (row[0],))
            WikiStore._event(db, row[0], "invalidated", "SOURCE_INDEX_CHANGED")
        if rows:
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
        return [row[0] for row in rows]

    @staticmethod
    def _input(page_id, kind, title, body, citations, expected_revision, links):
        _id(page_id)
        if not isinstance(kind, str) or kind not in KINDS:
            _error("kind must be source, topic, faq or conflict.")
        if not isinstance(title, str) or not title.strip() or len(title) > 160 or len(title.encode("utf-8")) > 1024 or "\x00" in title or "\n" in title:
            _error("title must contain 1 to 160 characters on one line.")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 12000 or "\x00" in body:
            _error("body must contain nonempty Markdown up to 12000 UTF-8 bytes.")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            _error("expected_revision must be zero for a new page or the current positive revision for an update.")
        if not isinstance(citations, list) or not 1 <= len(citations) <= 12:
            _error("citations must contain 1 to 12 original-file references.")
        versions = {}
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"file_id", "source_version", "ordinal", "locator", "quote"}:
                _error("Each citation must contain file_id, source_version, ordinal, locator and quote only.")
            file_id, version = citation["file_id"], citation["source_version"]
            if not isinstance(file_id, str) or FILE_ID.fullmatch(file_id) is None or not isinstance(version, str) or VERSION.fullmatch(version) is None:
                _error("Citations must use the file_id and SHA-256 source_version returned by inspect/read.")
            if file_id in versions and versions[file_id] != version:
                _error("A page cannot cite different versions of one file.")
            versions[file_id] = version
            ordinal = citation["ordinal"]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 0 <= ordinal <= 2147483647:
                _error("Citation ordinal must be a nonnegative segment ordinal.")
            locator = citation["locator"]
            if (not isinstance(locator, dict) or not locator or
                    any(not isinstance(k, str) or not k or not isinstance(v, (str, int)) or isinstance(v, bool) for k, v in locator.items()) or
                    len(_json(locator).encode("utf-8")) > 1024):
                _error("Citation locator must be the original scalar-field locator object, up to 1024 JSON bytes.")
            quote = citation["quote"]
            if not isinstance(quote, str) or not quote.strip() or "\x00" in quote or len(quote.encode("utf-8")) > 1024:
                _error("Citation quote must be nonempty exact source text up to 1024 UTF-8 bytes.")
        if kind == "source" and len(versions) != 1:
            _error("A source page describes exactly one original file.")
        markers = {int(x) for x in re.findall(r"\[(\d{1,2})\]", body)}
        if markers != set(range(1, len(citations) + 1)):
            _error("Reference every provided citation in body with [1], [2], and so on; do not reference missing citation numbers.")
        links = [] if links is None else links
        if not isinstance(links, list) or len(links) > 20:
            _error("links must be at most 20 Wiki page IDs used for navigation only.")
        for link in links:
            _id(link)
        if page_id in links or len(links) != len(set(links)):
            _error("Navigation links must be unique and cannot point to the page itself.")
        if len(_json({"title": title, "body": body, "citations": citations, "links": links}).encode("utf-8")) > MAX_PAGE_BYTES:
            _error("The complete page payload exceeds 24576 UTF-8 bytes.")
        return links

    def _context(self):
        return {"deadline": time.monotonic() + VERIFY_SECONDS, "files": {}}

    @staticmethod
    def _budget(context):
        if time.monotonic() >= context["deadline"]:
            raise DriveError("WIKI_VERIFICATION_BUDGET", "The bounded Wiki verification window ended before every source could be checked.", True,
                             "Retry a smaller page or continue the returned review/search position; no unverified Wiki text is evidence.")

    def _verify(self, citations, context):
        """Use the same source gate as inspect/read, once per distinct file.

        Segment fetches then bind the exact version under the database lock.
        The cooperative deadline cannot interrupt blocking OS/SMB calls.
        """
        self._budget(context)
        for citation in citations:
            key = (citation["file_id"], citation["source_version"])
            if key not in context["files"]:
                self._budget(context)
                context["files"][key] = self.engine._validated(*key)
                self._budget(context)
            with self.engine._db() as db:
                row = db.execute("SELECT status,source_version FROM files WHERE id=?", (key[0],)).fetchone()
                if not row or row["status"] != "ready" or row["source_version"] != key[1]:
                    raise DriveError("INDEX_CHANGED", "The source index changed during Wiki verification.", True, "Inspect the source and rebuild the affected page.")
                segment = db.execute("SELECT payload FROM segments WHERE file_id=? AND source_version=? AND ordinal=?", (*key, citation["ordinal"])).fetchone()
            if segment is None:
                raise DriveError("WIKI_CITATION_MISMATCH", "The cited segment does not exist in the selected original version.")
            payload = json.loads(segment[0])
            if payload.get("locator") != citation["locator"] or citation["quote"] not in payload.get("text", ""):
                raise DriveError("WIKI_CITATION_MISMATCH", "The quote or exact locator does not match the original source segment.", False,
                                 "Use inspect/read and copy the exact original locator and supporting text.")
        return True

    def _invalidate(self, page_id, reason, revision=None):
        with self.engine._db() as db:
            row = db.execute("SELECT revision,state FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
            if row and (revision is None or row["revision"] == revision) and row["state"] == "current":
                db.execute("UPDATE wiki_pages SET state='stale',reason=? WHERE page_id=?", (reason, page_id))
                self._event(db, page_id, "invalidated", reason)
                self.engine._bump(db)
        self.engine._checkpoint()

    def _sync(self):
        with self.engine._db() as db:
            changed = self.invalidate_changed(db)
        if changed:
            self.engine._checkpoint()
        return changed

    def _page(self, page_id):
        with self.engine._db() as db:
            row = db.execute("SELECT * FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
        if row is None:
            raise DriveError("WIKI_PAGE_NOT_FOUND", "The requested Wiki page does not exist.", False, "Search the Wiki or create a source-backed page.")
        return dict(row)

    def _verified_page(self, row, context):
        if row["state"] != "current":
            raise DriveError("WIKI_STALE", "The Wiki page is queued for rebuilding from original evidence.", True,
                             "Inspect/read the current originals and replace the page using its current revision.")
        try:
            self._verify(json.loads(row["citations"]), context)
        except DriveError as exc:
            if exc.code != "WIKI_VERIFICATION_BUDGET":
                self._invalidate(row["page_id"], exc.code, row["revision"])
            raise
        with self.engine._db() as db:
            current = db.execute("SELECT revision,state FROM wiki_pages WHERE page_id=?", (row["page_id"],)).fetchone()
            if not current or current["revision"] != row["revision"] or current["state"] != "current":
                raise DriveError("WIKI_PAGE_CHANGED", "The Wiki page changed during verification.", True, "Read the current page again.")
            db.execute("UPDATE wiki_pages SET verified_at=? WHERE page_id=?", (time.time(), row["page_id"]))
        return row

    @staticmethod
    def _summary(row):
        return {"page_id": row["page_id"], "kind": row["kind"], "title": row["title"], "revision": row["revision"],
                "citation_count": len(json.loads(row["citations"])), "derived": True, "review_status": "unreviewed"}

    def status(self, limit=20):
        _limit(limit)
        self._sync()
        with self.engine._db() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM wiki_pages GROUP BY state")}
            pending = [dict(row) for row in db.execute("SELECT page_id,revision,reason FROM wiki_pages WHERE state='stale' ORDER BY page_id LIMIT ?", (limit,))]
        return {"ok": True, "counts": {"current": counts.get("current", 0), "stale": counts.get("stale", 0)},
                "pending_pages": pending, "pending_total": counts.get("stale", 0), "has_more_pending": counts.get("stale", 0) > len(pending),
                "storage": {"database": str(self.engine.db_path), "markdown_pattern": str(self.engine.state / "wiki-page-<page_id>.md"),
                            "index": str(self.engine.state / "wiki-index.md"), "log": str(self.engine.state / "wiki-log.md")},
                "freshness": "counts_from_index_not_current_permission_verification", "derived": True,
                "next_action": "Use Wiki search/read for currently verified navigation; rebuild queued pages from inspect/read evidence."}

    def _rebuild(self, row):
        # IDs and versions are routing metadata. No cached title, body, quote,
        # locator or original path is exposed after source access fails.
        dependencies = sorted({(c["file_id"], c["source_version"]) for c in json.loads(row["citations"])})
        return {"page_id": row["page_id"], "revision": row["revision"], "kind": row["kind"],
                "sources": [{"file_id": file_id, "previous_source_version": version} for file_id, version in dependencies],
                "next_action": "Inspect these original file IDs and read their current versions; rebuild with expected_revision. Unavailable originals remain unresolved."}

    def read(self, page_id):
        _id(page_id)
        self._sync()
        row = self._page(page_id)
        try:
            self._verified_page(row, self._context())
        except DriveError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message,
                    "retryable": exc.retryable, "next_action": exc.next_action}, "rebuild": self._rebuild(row)}
        page = self._summary(row)
        page.update(body=row["body"], citations=json.loads(row["citations"]), links=json.loads(row["links"]), updated_at=row["updated_at"])
        return {"ok": True, "page": page, "verification": "source_versions_and_exact_quotes_checked",
                "claim_entailment": "not_established", "business_completeness": "not_established",
                "source_observation": "Sequential source checks during this operation; not an atomic snapshot.",
                "next_action": "Treat the page as derived navigation. Inspect/read original versions before answering, especially amounts, terms and current status."}

    def search(self, query, limit=10, after=""):
        _limit(limit, 20)
        _id(after, empty=True)
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or "\x00" in query:
            _error("query must contain 1 to 200 characters.")
        tokens = _query_tokens(query)
        if not tokens:
            _error("query must contain a searchable word or CJK character.")
        invalidated = self._sync()
        # Scan at most 100 matching pages per operation; source checks share one
        # deadline and one per-file validation cache across candidate pages.
        with self.engine._db() as db:
            rows = [dict(row) for row in db.execute("SELECT p.* FROM wiki_pages p JOIN wiki_search s ON s.page_id=p.page_id WHERE wiki_search MATCH ? AND p.page_id>? ORDER BY p.page_id LIMIT 101", (" AND ".join(tokens), after))]
        context, pages, position, stop = self._context(), [], after, None
        processed = 0
        for row in rows[:100]:
            if len(pages) >= limit:
                stop = "page_limit"
                break
            if row["state"] == "stale":
                position, processed = row["page_id"], processed + 1
                continue
            try:
                self._verified_page(row, context)
            except DriveError as exc:
                if exc.code == "WIKI_VERIFICATION_BUDGET":
                    stop = "verification_budget"
                    break
                invalidated.append(row["page_id"])
            else:
                pages.append(self._summary(row))
            position, processed = row["page_id"], processed + 1
        more = processed < len(rows)
        if more and stop is None:
            stop = "candidate_limit"
        return {"ok": True, "pages": pages, "returned": len(pages), "has_more": more, "next_after": position if more else None,
                "stop_reason": stop, "invalidated_page_ids": sorted(set(invalidated))[:100],
                "invalidated_total": len(set(invalidated)), "invalidated_ids_truncated": len(set(invalidated)) > 100, "derived": True,
                "verification": "returned_pages_sources_checked", "business_completeness": "not_established",
                "scope": "Bounded Wiki candidates ordered by page_id; zero matches do not prove absence in original files."}

    def write(self, page_id, kind, title, body, citations, expected_revision=0, links=None):
        links = self._input(page_id, kind, title, body, citations, expected_revision, links)
        # Validate before entering the transaction; no stale version or revoked
        # source is accepted merely because a host supplied a plausible quote.
        self._verify(citations, self._context())
        now = time.time()
        with self.engine._db() as db:
            old = db.execute("SELECT revision,created_at FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
            current_revision = old["revision"] if old else 0
            if current_revision != expected_revision:
                raise DriveError("WIKI_REVISION_MISMATCH", "The page revision changed or an existing page was not explicitly selected for replacement.", True,
                                 "Read the page or its queued revision, then supply that expected_revision.")
            # Index changes may race verification while a background scan runs.
            for citation in citations:
                source = db.execute("SELECT status,source_version FROM files WHERE id=?", (citation["file_id"],)).fetchone()
                if not source or source["status"] != "ready" or source["source_version"] != citation["source_version"]:
                    raise DriveError("INDEX_CHANGED", "A source changed before the Wiki page could be published.", True, "Read current source evidence and retry.")
            revision = current_revision + 1
            db.execute("INSERT INTO wiki_pages(page_id,kind,title,body,citations,links,revision,state,reason,created_at,updated_at,verified_at) VALUES(?,?,?,?,?,?,?,'current',NULL,?,?,?) ON CONFLICT(page_id) DO UPDATE SET kind=excluded.kind,title=excluded.title,body=excluded.body,citations=excluded.citations,links=excluded.links,revision=excluded.revision,state='current',reason=NULL,updated_at=excluded.updated_at,verified_at=excluded.verified_at", (page_id, kind, title, body, _json(citations), _json(links), revision, old["created_at"] if old else now, now, now))
            db.execute("DELETE FROM wiki_dependencies WHERE page_id=?", (page_id,))
            db.executemany("INSERT INTO wiki_dependencies(page_id,file_id,source_version) VALUES(?,?,?)", sorted({(page_id, c["file_id"], c["source_version"]) for c in citations}))
            db.execute("DELETE FROM wiki_search WHERE page_id=?", (page_id,))
            db.execute("INSERT INTO wiki_search(tokens,page_id) VALUES(?,?)", (" ".join(_tokens(title + "\n" + body)), page_id))
            self._event(db, page_id, "created" if not old else "rebuilt")
            self.engine._bump(db)
        # Durable database first. A mirror publication failure is explicit and
        # must not cause the caller to replay an already committed write.
        self.engine._checkpoint()
        mirrors = self._materialize([page_id])
        return {"ok": True, "page_id": page_id, "revision": revision, "state": "current", "derived": True,
                "review_status": "unreviewed", "verification": "source_versions_and_exact_quotes_checked",
                "claim_entailment": "not_established", "markdown": mirrors,
                "next_action": "Use Wiki read/search for current permission and version verification; do not read the Markdown mirror as fresh evidence."}

    def refresh(self, limit=20, after=""):
        _limit(limit)
        _id(after, empty=True)
        invalidated = self._sync()
        with self.engine._db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM wiki_pages WHERE page_id>? ORDER BY page_id LIMIT ?", (after, limit + 1))]
        context, position, processed, checked, stop = self._context(), after, 0, 0, None
        visited, revalidated = [], []
        for row in rows[:limit]:
            try:
                if row["state"] == "current":
                    self._verified_page(row, context)
                else:
                    # A transient denial or metadata-only scan can invalidate a
                    # page without changing its original content. Verify the
                    # exact original versions and quotes before restoring it;
                    # this is revalidation, never model rewriting or inference.
                    self._verify(json.loads(row["citations"]), context)
                    with self.engine._db() as db:
                        current = db.execute("SELECT revision,state FROM wiki_pages WHERE page_id=?", (row["page_id"],)).fetchone()
                        if not current or current["revision"] != row["revision"] or current["state"] != "stale":
                            raise DriveError("WIKI_PAGE_CHANGED", "The queued Wiki page changed during revalidation.", True, "Refresh the current page again.")
                        for citation in json.loads(row["citations"]):
                            source = db.execute("SELECT status,source_version FROM files WHERE id=?", (citation["file_id"],)).fetchone()
                            if not source or source["status"] != "ready" or source["source_version"] != citation["source_version"]:
                                raise DriveError("INDEX_CHANGED", "A source changed during Wiki revalidation.", True, "Refresh the current source index.")
                        db.execute("UPDATE wiki_pages SET state='current',reason=NULL,verified_at=? WHERE page_id=?", (time.time(), row["page_id"]))
                        self._event(db, row["page_id"], "revalidated")
                        self.engine._bump(db)
                    revalidated.append(row["page_id"])
            except DriveError as exc:
                if exc.code == "WIKI_VERIFICATION_BUDGET":
                    stop = "verification_budget"
                    break
                invalidated.append(row["page_id"])
            checked += 1
            position, processed = row["page_id"], processed + 1
            visited.append(row["page_id"])
        invalidated = [page_id for page_id in invalidated if page_id not in revalidated]
        more = processed < len(rows)
        if more and stop is None:
            stop = "page_limit"
        self.engine._checkpoint()
        mirrors = self._materialize(visited)
        with self.engine._db() as db:
            pending_total = db.execute("SELECT COUNT(*) FROM wiki_pages WHERE state='stale'").fetchone()[0]
        return {"ok": True, "checked_pages": checked, "visited_pages": processed, "invalidated_page_ids": sorted(set(invalidated))[:100],
                "invalidated_total": len(set(invalidated)), "invalidated_ids_truncated": len(set(invalidated)) > 100,
                "pending_total": pending_total, "has_more": more, "next_after": position if more else None, "stop_reason": stop,
                "revalidated_page_ids": revalidated, "revalidated_pages": len(revalidated), "markdown": mirrors, "rebuild_mode": "host_model_from_original_evidence", "business_completeness": "not_established",
                "next_action": "Rebuild only queued page IDs from current inspect/read results and expected_revision; no LLM was run automatically."}

    def _atomic_text(self, name, text):
        storage = self.engine._storage
        staging = name + ".next"
        storage.unlink_root(staging)
        fd = storage._open_root(staging, write=True, create=True, exclusive=True)
        try:
            with os.fdopen(fd, "wb") as stream:
                if os.name == "posix":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            storage._replace_root(staging, name)
        except Exception:
            storage.unlink_root(staging)
            raise

    def _materialize(self, page_ids):
        """The DB remains usable if a disposable mirror cannot be published."""
        try:
            # Serialize mirror writers and source DB snapshots through the same
            # lock; source checks are intentionally never read from these files.
            with self.engine._db_lock:
                for page_id in page_ids:
                    row = self._page(page_id)
                    preamble = "# %s\n\nDerived, unreviewed Wiki mirror. Use wiki_read to check current source permissions and versions.\n\nPage: `%s` · revision: %d · state: %s\n\n" % (row["title"] if row["state"] == "current" else "Rebuild required", page_id, row["revision"], row["state"])
                    if row["state"] == "current":
                        content = preamble + row["body"] + "\n\n## Original references\n\n"
                        for index, citation in enumerate(json.loads(row["citations"]), 1):
                            content += "[%d] `%s` · version `%s` · ordinal %d · locator `%s`\n\n> %s\n\n" % (index, citation["file_id"], citation["source_version"], citation["ordinal"], _json(citation["locator"]), citation["quote"].replace("\n", "\n> "))
                        links = json.loads(row["links"])
                        if links:
                            content += "## Navigation only\n\n" + "\n".join("- [%s](wiki-page-%s.md)" % (link, link) for link in links) + "\n"
                    else:
                        content = preamble + "Original source verification is required. Cached page text is withheld from this mirror.\n"
                    self._atomic_text("wiki-page-" + page_id + ".md", content)
                with self.engine._db() as db:
                    rows = db.execute("SELECT page_id,state,revision FROM wiki_pages ORDER BY page_id LIMIT 1001").fetchall()
                    events = db.execute("SELECT page_id,event,reason,at FROM wiki_events ORDER BY id DESC LIMIT ?", (EVENT_LIMIT,)).fetchall()
                index_text = "# Wiki navigation\n\nDerived mirror; use wiki_search/wiki_read for current verification. Titles are withheld here.\n\n"
                index_text += "\n".join("- [%s](wiki-page-%s.md) · revision %d · %s" % (r["page_id"], r["page_id"], r["revision"], r["state"]) for r in rows[:1000]) + "\n"
                if len(rows) > 1000:
                    index_text += "\nMirror limited to 1000 pages. Use the SQLite-backed Wiki tools for further navigation.\n"
                self._atomic_text("wiki-index.md", index_text)
                log = "# Wiki change log\n\nMost recent %d events. No source content or page titles.\n\n" % EVENT_LIMIT
                log += "\n".join("- %s · %s · %s%s" % (datetime.fromtimestamp(r["at"], timezone.utc).isoformat(), r["page_id"], r["event"], (" · " + r["reason"]) if r["reason"] else "") for r in events) + "\n"
                self._atomic_text("wiki-log.md", log)
            return {"published": True, "disposable_mirror": True}
        except (DriveError, OSError):
            return {"published": False, "disposable_mirror": True, "error_code": "WIKI_MIRROR_FAILED",
                    "next_action": "The SQLite write is committed. Repair private state storage and run wiki_refresh; do not replay the Wiki write blindly."}
