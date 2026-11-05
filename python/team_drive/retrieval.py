"""Bounded BM25/semantic discovery; source reads remain the evidence authority.

The caller owns the SQLite connection, transaction and lock. This module never
commits and never changes ``files.status``. Model requests are opt-in, sent only
to explicit loopback/RFC1918/ULA addresses, and never follow redirects or proxies.
Vectors are a disposable, version-bound SQLite cache, not an exhaustive dataset.
"""
from __future__ import annotations

import hashlib
import ipaddress
import itertools
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import DriveError
from .bounded_json import loads as bounded_json_loads


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_DIMENSIONS = 4096
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "::1/128", "fc00::/7"))
_SEMANTIC_DEFAULTS = {"enabled": False, "endpoint": "", "model": "",
                      "model_revision": "", "timeout_seconds": 10,
                      "batch_size": 16, "max_segments": 20000,
                      "max_text_chars": 8000, "minimum_similarity": 0.0,
                      "per_file_budget_seconds": 20}
_RERANK_DEFAULTS = {"enabled": False, "endpoint": "", "model": "",
                    "timeout_seconds": 10, "max_candidates": 30}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _invalid(message):
    raise DriveError("INVALID_CONFIG", message)


def _number(config, key, minimum, maximum, integer=False):
    value = config[key]
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not minimum <= value <= maximum or not math.isfinite(value) or
            (integer and not isinstance(value, int))):
        _invalid("retrieval.%s must be a finite %s between %s and %s." % (
            key, "integer" if integer else "number", minimum, maximum))


def _local_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        _invalid("Retrieval endpoints must be explicit local HTTP(S) URLs.")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        _invalid("Retrieval endpoint URL is invalid.")
    if (parsed.scheme not in ("http", "https") or not host or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or parsed.netloc.endswith(":") or
            (port is not None and not 1 <= port <= 65535)):
        _invalid("Retrieval endpoints require HTTP(S), without credentials, query or fragment.")
    # Resolve no arbitrary hostname: this avoids DNS rebinding and accidental cloud use.
    if host == "localhost":
        # Pin localhost to loopback instead of trusting DNS or the hosts file.
        netloc = "127.0.0.1" + (":" + str(port) if port is not None else "")
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))
    if "%" in host:
        _invalid("Retrieval endpoints do not accept IPv6 scope identifiers.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        _invalid("Retrieval endpoints must use localhost or a literal private IP address.")
    if not any(address in network for network in _PRIVATE_NETWORKS if address.version == network.version):
        _invalid("Retrieval endpoints must be loopback or RFC1918/ULA private addresses.")
    return value


def validate_config(config=None):
    """Return a normalized configuration; invalid config is the only public exception."""
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - {"candidate_limit", "rrf_k", "semantic", "rerank"}:
        _invalid("retrieval must be an object containing supported retrieval options.")
    result = {"candidate_limit": config.get("candidate_limit", 100), "rrf_k": config.get("rrf_k", 60)}
    _number(result, "candidate_limit", 1, 1000, True)
    _number(result, "rrf_k", 1, 1000, True)
    for kind, defaults in (("semantic", _SEMANTIC_DEFAULTS), ("rerank", _RERANK_DEFAULTS)):
        supplied = config.get(kind, {})
        if not isinstance(supplied, dict) or set(supplied) - set(defaults):
            _invalid("retrieval.%s must contain supported options." % kind)
        options = dict(defaults, **supplied)
        if not isinstance(options["enabled"], bool):
            _invalid("retrieval.%s.enabled must be boolean." % kind)
        for key in ("endpoint", "model") + (("model_revision",) if kind == "semantic" else ()):
            if not isinstance(options[key], str) or len(options[key]) > 2048:
                _invalid("Retrieval model and endpoint values must be strings of at most 2048 characters.")
        if options["endpoint"]:
            options["endpoint"] = _local_endpoint(options["endpoint"])
        if options["enabled"] and (not options["endpoint"] or not options["model"].strip()):
            _invalid("Enabled retrieval models require an explicit local endpoint and model name.")
        _number(options, "timeout_seconds", 0.1, 20)
        if kind == "semantic":
            _number(options, "batch_size", 1, 64, True)
            _number(options, "max_segments", 1, 100000, True)
            _number(options, "max_text_chars", 1, 32000, True)
            _number(options, "minimum_similarity", -1, 1)
            _number(options, "per_file_budget_seconds", 0.1, 20)
        else:
            _number(options, "max_candidates", 1, 100, True)
        result[kind] = options
    return result


class _ModelFailure(Exception):
    def __init__(self, code):
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _ModelFailure("MODEL_REDIRECT_BLOCKED")


def _post(options, payload, deadline=None):
    """No server response body or exception text is exposed in public diagnostics."""
    request = urllib.request.Request(options["endpoint"], data=_json(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + options["timeout_seconds"])
    if deadline <= time.monotonic():
        raise _ModelFailure("MODEL_BUDGET_EXCEEDED")
    try:
        with opener.open(request, timeout=max(0.001, deadline - time.monotonic())) as response:
            if response.status != 200:
                raise _ModelFailure("MODEL_HTTP_ERROR")
            chunks, size = [], 0
            while size <= _MAX_RESPONSE_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ModelFailure("MODEL_BUDGET_EXCEEDED")
                # Bound each socket read by the remaining overall request budget.
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(max(0.001, remaining))
                chunk = response.read1(min(65536, _MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise _ModelFailure("MODEL_RESPONSE_TOO_LARGE")
        value = bounded_json_loads(body)
        if not isinstance(value, dict):
            raise _ModelFailure("MODEL_INVALID_RESPONSE")
        return value
    except _ModelFailure:
        raise
    except urllib.error.HTTPError:
        raise _ModelFailure("MODEL_HTTP_ERROR") from None
    except (TimeoutError, OSError, urllib.error.URLError):
        raise _ModelFailure("MODEL_BUDGET_EXCEEDED" if time.monotonic() >= deadline else "MODEL_UNAVAILABLE") from None
    except (ValueError, UnicodeError, RecursionError):
        raise _ModelFailure("MODEL_INVALID_RESPONSE") from None


def _vector(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= _MAX_DIMENSIONS or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                abs(v) > 1e308 or not math.isfinite(v) for v in value)):
        raise _ModelFailure("EMBEDDING_INVALID_VECTOR")
    # hypot is overflow-safe, unlike sqrt(sum(x*x)).
    norm = math.hypot(*value)
    if not math.isfinite(norm) or norm == 0:
        raise _ModelFailure("EMBEDDING_INVALID_VECTOR")
    return [v / norm for v in value]


def _default_tokens(query):
    """Match engine token encoding without importing Engine (which imports us)."""
    tokens = {"w" + word for word in re.findall(r"[a-z0-9]+", query.casefold())}
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", query):
        if len(run) == 1:
            tokens.add("c" + run.encode().hex())
        else:
            tokens.update("b" + run[i:i + 2].encode().hex() for i in range(len(run) - 1))
    return sorted(tokens)


class Retrieval:
    """Connection-independent retrieval helper; use under the engine's DB lock."""

    def __init__(self, config=None):
        self.config = validate_config(config)
        semantic = self.config["semantic"]
        self.fingerprint = _digest(_json({key: semantic[key] for key in
            ("endpoint", "model", "model_revision", "max_text_chars")}))
        self._last_semantic_error = None
        self._last_rerank_error = None
        self._rerank_checked = False

    def init_schema(self, db):
        # execute rather than executescript: the latter implicitly commits caller transactions.
        db.execute("""CREATE TABLE IF NOT EXISTS retrieval_vectors(
            file_id TEXT NOT NULL, source_version TEXT NOT NULL, ordinal INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, text_digest TEXT NOT NULL, vector TEXT NOT NULL,
            dimensions INTEGER NOT NULL, input_truncated INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(file_id,ordinal))""")
        db.execute("""CREATE TABLE IF NOT EXISTS retrieval_file_state(
            file_id TEXT PRIMARY KEY,source_version TEXT NOT NULL,fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,error_code TEXT,eligible_segments INTEGER NOT NULL,
            updated_at REAL NOT NULL)""")
        db.execute("DELETE FROM retrieval_vectors WHERE fingerprint!=?", (self.fingerprint,))
        db.execute("DELETE FROM retrieval_file_state WHERE fingerprint!=?", (self.fingerprint,))
        self.prune(db)
        db.execute("""DELETE FROM retrieval_vectors WHERE rowid NOT IN (
            SELECT rowid FROM retrieval_vectors ORDER BY file_id,ordinal LIMIT ?)""",
                   (self.config["semantic"]["max_segments"],))

    @staticmethod
    def delete_file(db, file_id):
        db.execute("DELETE FROM retrieval_vectors WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM retrieval_file_state WHERE file_id=?", (file_id,))

    def prune(self, db):
        db.execute("""DELETE FROM retrieval_vectors WHERE NOT EXISTS(
            SELECT 1 FROM files f JOIN segments s ON s.file_id=f.id
            WHERE f.id=retrieval_vectors.file_id AND f.status='ready'
            AND f.source_version=retrieval_vectors.source_version
            AND s.source_version=f.source_version AND s.ordinal=retrieval_vectors.ordinal)
            OR fingerprint!=?""", (self.fingerprint,))
        db.execute("""DELETE FROM retrieval_file_state WHERE NOT EXISTS(
            SELECT 1 FROM files f WHERE f.id=retrieval_file_state.file_id AND f.status='ready'
            AND f.source_version=retrieval_file_state.source_version) OR fingerprint!=?""", (self.fingerprint,))

    def _embed(self, texts, deadline=None):
        options = self.config["semantic"]
        response = _post(options, {"model": options["model"], "input": texts}, deadline=deadline)
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise _ModelFailure("EMBEDDING_INVALID_RESPONSE")
        indexed = {}
        for item in data:
            if not isinstance(item, dict):
                raise _ModelFailure("EMBEDDING_INVALID_RESPONSE")
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(texts) or index in indexed:
                raise _ModelFailure("EMBEDDING_INVALID_RESPONSE")
            indexed[index] = _vector(item.get("embedding"))
        vectors = [indexed[i] for i in range(len(texts))]
        if len({len(v) for v in vectors}) != 1:
            raise _ModelFailure("EMBEDDING_DIMENSION_MISMATCH")
        return vectors

    def index_file(self, db, file_id, source_version, deadline=None):
        """Cache current segment embeddings. Model failures return diagnostics, never raise.

        SQLite errors are intentionally not swallowed. They signal state corruption,
        not model unavailability, and should follow the engine's existing state policy.
        """
        options = self.config["semantic"]
        if not options["enabled"]:
            return {"enabled": False, "state": "disabled", "error_code": None, "newly_indexed_segments": 0}
        deadline = min(deadline if deadline is not None else float("inf"),
                       time.monotonic() + options["per_file_budget_seconds"])
        self.prune(db)
        current = db.execute("SELECT source_version,status FROM files WHERE id=?", (file_id,)).fetchone()
        if not current or current[0] != source_version or current[1] != "ready":
            self.delete_file(db, file_id)
            return {"enabled": True, "state": "not_ready", "error_code": "SOURCE_VERSION_UNAVAILABLE", "newly_indexed_segments": 0}
        eligible = db.execute("""SELECT COUNT(*) FROM segments WHERE file_id=? AND source_version=?
            AND length(json_extract(payload,'$.text'))>0""", (file_id, source_version)).fetchone()[0]
        other_count = db.execute("SELECT COUNT(*) FROM retrieval_vectors WHERE file_id!=?", (file_id,)).fetchone()[0]
        capacity = max(0, options["max_segments"] - other_count)
        db.execute("""DELETE FROM retrieval_vectors WHERE file_id=? AND ordinal NOT IN (
            SELECT ordinal FROM segments WHERE file_id=? AND source_version=?
            AND length(json_extract(payload,'$.text'))>0 ORDER BY ordinal LIMIT ?)""",
            (file_id, file_id, source_version, capacity))
        error = None
        newly_indexed = 0
        dimensions = db.execute("SELECT dimensions FROM retrieval_vectors LIMIT 1").fetchone()

        def cache_batch(batch):
            nonlocal dimensions, newly_indexed
            if time.monotonic() >= deadline:
                raise _ModelFailure("MODEL_BUDGET_EXCEEDED")
            vectors = self._embed([item[2] for item in batch], deadline=deadline)
            if dimensions and len(vectors[0]) != dimensions[0]:
                raise _ModelFailure("EMBEDDING_DIMENSION_MISMATCH")
            dimensions = (len(vectors[0]),)
            current = db.execute("SELECT source_version,status FROM files WHERE id=?", (file_id,)).fetchone()
            if not current or current[0] != source_version or current[1] != "ready":
                raise _ModelFailure("SOURCE_VERSION_CHANGED")
            for item, vector in zip(batch, vectors):
                db.execute("INSERT OR REPLACE INTO retrieval_vectors VALUES(?,?,?,?,?,?,?,?)",
                           (file_id, source_version, item[0], self.fingerprint, item[1], _json(vector),
                            len(vector), item[3]))
                newly_indexed += 1

        try:
            # Stream source payloads and retain at most one model batch in memory.
            rows = db.execute("""SELECT ordinal,payload FROM segments WHERE file_id=? AND source_version=?
                AND length(json_extract(payload,'$.text'))>0 ORDER BY ordinal LIMIT ?""",
                (file_id, source_version, capacity))
            batch = []
            for ordinal, payload in rows:
                if time.monotonic() >= deadline:
                    raise _ModelFailure("MODEL_BUDGET_EXCEEDED")
                text = json.loads(payload)["text"]
                text_hash = _digest(text)
                cache = db.execute("SELECT text_digest FROM retrieval_vectors WHERE file_id=? AND ordinal=?",
                                   (file_id, ordinal)).fetchone()
                if cache and cache[0] == text_hash:
                    continue
                db.execute("DELETE FROM retrieval_vectors WHERE file_id=? AND ordinal=?", (file_id, ordinal))
                batch.append((ordinal, text_hash, text[:options["max_text_chars"]], int(len(text) > options["max_text_chars"])))
                if len(batch) >= options["batch_size"]:
                    cache_batch(batch)
                    batch = []
            if batch:
                cache_batch(batch)
        except _ModelFailure as exc:
            error = exc.code
            if error == "SOURCE_VERSION_CHANGED":
                self.delete_file(db, file_id)
                return {"enabled": True, "state": "not_ready", "error_code": error, "newly_indexed_segments": 0}
        self._last_semantic_error = error
        indexed = db.execute("SELECT COUNT(*) FROM retrieval_vectors WHERE file_id=?", (file_id,)).fetchone()[0]
        state = "failed" if error and not indexed else ("partial" if error or indexed < eligible else "ready")
        db.execute("INSERT OR REPLACE INTO retrieval_file_state VALUES(?,?,?,?,?,?,?)",
                   (file_id, source_version, self.fingerprint, state, error, eligible, time.time()))
        return {"enabled": True, "state": state, "indexed_segments": indexed,
                "eligible_segments": eligible, "error_code": error, "limit_reached": indexed < eligible and not error,
                "newly_indexed_segments": newly_indexed}

    def status(self, db):
        options = self.config["semantic"]
        common = {"keyword": {"enabled": True, "method": "sqlite_fts5_bm25"},
                  "rerank": {"enabled": self.config["rerank"]["enabled"],
                    "state": "disabled" if not self.config["rerank"]["enabled"] else (
                        "failed" if self._last_rerank_error else (
                            "ready" if self._rerank_checked else "configured_not_health_checked")),
                    "last_error": self._last_rerank_error},
                  "network_policy": "explicit_local_model_endpoints_only",
                  "network_calls_enabled": options["enabled"] or self.config["rerank"]["enabled"],
                  "candidate_limit": self.config["candidate_limit"], "exhaustive_for_business_totals": False}
        if not options["enabled"]:
            # Default keyword-only status must not scan and parse the entire corpus.
            common["semantic"] = {"enabled": False, "state": "disabled", "model": options["model"] or None,
                "model_revision": options["model_revision"] or None, "indexed_segments": None,
                "eligible_segments": None, "coverage_complete": False, "coverage_checked": False,
                "file_states": {}, "errors": [], "input_truncated_segments": None,
                "max_segments": options["max_segments"], "max_text_chars": options["max_text_chars"],
                "limit_reached": False, "scan_method": "bounded_sqlite_exact_cosine", "cache_persistent": True}
            return common
        eligible = db.execute("""SELECT COUNT(*) FROM segments s JOIN files f ON f.id=s.file_id
            WHERE f.status='ready' AND s.source_version=f.source_version
            AND length(json_extract(s.payload,'$.text'))>0""").fetchone()[0]
        indexed, truncated = db.execute("""SELECT COUNT(*),COALESCE(SUM(v.input_truncated),0)
            FROM retrieval_vectors v JOIN files f ON f.id=v.file_id
            JOIN segments s ON s.file_id=v.file_id AND s.ordinal=v.ordinal
            WHERE f.status='ready' AND f.source_version=v.source_version
            AND s.source_version=v.source_version AND v.fingerprint=?""", (self.fingerprint,)).fetchone()
        states = {row[0]: row[1] for row in db.execute("""SELECT r.status,COUNT(*) FROM retrieval_file_state r
            JOIN files f ON f.id=r.file_id WHERE f.status='ready' AND f.source_version=r.source_version
            AND r.fingerprint=? GROUP BY r.status""", (self.fingerprint,))}
        errors = sorted({row[0] for row in db.execute("""SELECT DISTINCT r.error_code FROM retrieval_file_state r
            JOIN files f ON f.id=r.file_id WHERE f.status='ready' AND f.source_version=r.source_version
            AND r.fingerprint=? AND r.error_code IS NOT NULL""", (self.fingerprint,))})
        if self._last_semantic_error:
            errors = sorted(set(errors + [self._last_semantic_error]))
        if not options["enabled"]:
            state = "disabled"
        elif errors:
            state = "degraded" if indexed else "failed"
        elif indexed == eligible:
            state = "ready"
        else:
            state = "partial" if indexed else "pending"
        common["semantic"] = {"enabled": options["enabled"], "state": state,
                    "model": options["model"] or None, "model_revision": options["model_revision"] or None,
                    "model_version_unpinned": not bool(options["model_revision"]),
                    "indexed_segments": indexed, "eligible_segments": eligible,
                    "coverage_checked": True,
                    "coverage_complete": options["enabled"] and indexed == eligible and not errors and not truncated,
                    "file_states": states, "errors": errors, "input_truncated_segments": truncated,
                    "max_segments": options["max_segments"], "max_text_chars": options["max_text_chars"],
                    "limit_reached": indexed >= options["max_segments"] and indexed < eligible,
                    "scan_method": "bounded_sqlite_exact_cosine", "cache_persistent": True,
                    "cache_binding": "source_version+segment_text_digest+endpoint+model+model_revision+text_limit",
                    "model_change_detection": "Model weights cannot be inferred from a name; change model_revision when weights change."}
        return common

    @staticmethod
    def _scope(prefix, alias="f"):
        if not prefix:
            return "", []
        return (" AND (" + alias + ".path=? OR substr(" + alias + ".path,1,?)=?)",
                [prefix, len(prefix) + 1, prefix + "/"])

    def _keyword(self, db, query, prefix, expression, file_ids=None):
        if file_ids == []:
            return [], False
        if expression is None:
            expression = " OR ".join('"' + term + '"' for term in _default_tokens(query))
        if not expression:
            return [], False
        scope, args = self._scope(prefix)
        if file_ids is not None:
            scope += " AND f.id IN (SELECT value FROM json_each(?))"
            args.append(_json(file_ids))
        # FTS5 bm25 cannot be used inside every aggregate/window context. Rank
        # segments first, then deduplicate bounded candidates to file-level hits.
        segment_limit = self.config["candidate_limit"] * 20
        rows = db.execute("""SELECT search_index.file_id,CAST(search_index.ordinal AS INTEGER),
            bm25(search_index),f.source_version,f.path FROM search_index
            JOIN files f ON f.id=search_index.file_id
            WHERE search_index MATCH ? AND f.status='ready'""" + scope +
            " ORDER BY bm25(search_index),f.path,CAST(search_index.ordinal AS INTEGER) LIMIT ?",
            [expression] + args + [segment_limit + 1]).fetchall()
        truncated = len(rows) > segment_limit
        found = {}
        for file_id, ordinal, score, version, path in rows[:segment_limit]:
            if file_id not in found:
                found[file_id] = {"file_id": file_id, "source_version": version, "ordinal": ordinal,
                                  "path": path, "raw_score": -score}
        return list(found.values())[:self.config["candidate_limit"]], truncated or len(found) > self.config["candidate_limit"]

    def _semantic(self, db, query, prefix, deadline=None):
        options = self.config["semantic"]
        scope, args = self._scope(prefix)
        rows = db.execute("""SELECT v.file_id,v.source_version,v.ordinal,v.vector,v.text_digest,
            s.payload,f.path FROM retrieval_vectors v JOIN files f ON f.id=v.file_id
            JOIN segments s ON s.file_id=v.file_id AND s.ordinal=v.ordinal
            WHERE f.status='ready' AND f.source_version=v.source_version
            AND s.source_version=v.source_version AND v.fingerprint=?""" + scope +
            " ORDER BY f.path,v.ordinal LIMIT ?", [self.fingerprint] + args + [options["max_segments"]])
        first = rows.fetchone()
        if first is None:
            return [], {"state": "no_indexed_segments", "scanned_segments": 0, "error_code": None}
        try:
            query_vector = self._embed([query[:options["max_text_chars"]]], deadline=deadline)[0]
            best = {}
            stale, scanned = [], 0
            for file_id, version, ordinal, encoded, text_hash, payload, path in itertools.chain([first], rows):
                if deadline is not None and time.monotonic() >= deadline:
                    raise _ModelFailure("MODEL_BUDGET_EXCEEDED")
                scanned += 1
                if _digest(json.loads(payload)["text"]) != text_hash:
                    stale.append((file_id, ordinal))
                    continue
                vector = _vector(json.loads(encoded))
                if len(vector) != len(query_vector):
                    raise _ModelFailure("EMBEDDING_DIMENSION_MISMATCH")
                similarity = sum(a * b for a, b in zip(vector, query_vector))
                if similarity < options["minimum_similarity"]:
                    continue
                if file_id not in best or similarity > best[file_id]["raw_score"]:
                    best[file_id] = {"file_id": file_id, "source_version": version, "ordinal": ordinal,
                                     "path": path, "raw_score": similarity}
            ordered = sorted(best.values(), key=lambda item: (-item["raw_score"], item["path"]))
            for file_id, ordinal in stale:
                db.execute("DELETE FROM retrieval_vectors WHERE file_id=? AND ordinal=?", (file_id, ordinal))
                db.execute("UPDATE retrieval_file_state SET status='partial',error_code='EMBEDDING_CACHE_STALE' WHERE file_id=?",
                           (file_id,))
            self._last_semantic_error = None
            return ordered[:self.config["candidate_limit"]], {"state": "partial" if stale else "ready", "scanned_segments": scanned,
                "stale_segments_skipped": len(stale), "cache_invalidated_segments": len(stale),
                "error_code": "EMBEDDING_CACHE_STALE" if stale else None,
                "candidate_limit_reached": len(ordered) > self.config["candidate_limit"]}
        except _ModelFailure as exc:
            self._last_semantic_error = exc.code
            return [], {"state": "failed", "scanned_segments": 0, "error_code": exc.code,
                        "fallback": "keyword_bm25"}

    def _rerank(self, db, query, hits, requested=False, deadline=None):
        options = self.config["rerank"]
        if requested is False:
            return hits, {"state": "skipped", "error_code": None}
        if not options["enabled"] or not hits:
            return hits, {"state": "disabled" if not options["enabled"] else "no_candidates", "error_code": None}
        candidates = hits[:options["max_candidates"]]
        documents = []
        for item in candidates:
            row = db.execute("SELECT payload FROM segments WHERE file_id=? AND ordinal=? AND source_version=?",
                             (item["file_id"], item["ordinal"], item["source_version"])).fetchone()
            documents.append((json.loads(row[0]).get("text", "") if row else item["path"])[:8000])
        try:
            response = _post(options, {"model": options["model"], "query": query,
                                      "documents": documents, "top_n": len(documents)}, deadline=deadline)
            results = response.get("results")
            if not isinstance(results, list) or len(results) != len(candidates):
                raise _ModelFailure("RERANK_INVALID_RESPONSE")
            scores = {}
            for result in results:
                if not isinstance(result, dict):
                    raise _ModelFailure("RERANK_INVALID_RESPONSE")
                index, score = result.get("index"), result.get("relevance_score")
                if (isinstance(index, bool) or not isinstance(index, int) or index in scores or
                        not 0 <= index < len(candidates) or isinstance(score, bool) or
                        not isinstance(score, (int, float)) or abs(score) > 1e308 or not math.isfinite(score)):
                    raise _ModelFailure("RERANK_INVALID_RESPONSE")
                scores[index] = score
            order = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
            reranked = []
            for index in order:
                item = dict(candidates[index], signals=dict(candidates[index]["signals"]))
                item["signals"]["rerank_score"] = scores[index]
                reranked.append(item)
            self._last_rerank_error = None
            self._rerank_checked = True
            return reranked + hits[len(candidates):], {"state": "ready", "error_code": None,
                                                       "reranked_candidates": len(candidates)}
        except _ModelFailure as exc:
            self._last_rerank_error = exc.code
            return hits, {"state": "failed", "error_code": exc.code, "fallback": "rrf_order_preserved"}

    def search(self, db, query, prefix="", keyword_expression=None, mode="hybrid", rerank=False, keyword_file_ids=None,
               before_rerank=None):
        """Return bounded discovery hits and transparent degradation diagnostics.

        Caller validates query/prefix and retains existing evidence-read safeguards.
        Hits are file-level; each ordinal is a navigation hint, never final evidence.
        """
        if mode not in ("keyword", "hybrid", "semantic"):
            raise DriveError("INVALID_MODE", "Retrieval mode must be keyword, hybrid or semantic.")
        if rerank is not None and not isinstance(rerank, bool):
            raise DriveError("INVALID_PARAMS", "rerank must be boolean when specified.")
        if keyword_file_ids is not None and (mode != "keyword" or not isinstance(keyword_file_ids, list) or
                len(keyword_file_ids) > 1000 or any(not isinstance(value, str) or not value for value in keyword_file_ids)):
            raise DriveError("INVALID_PARAMS", "keyword_file_ids must be at most 1000 nonempty identifiers in keyword mode.")
        started = time.monotonic()
        deadline = started + 30
        keywords, keyword_truncated = ([], False) if mode == "semantic" else self._keyword(
            db, query, prefix, keyword_expression, file_ids=keyword_file_ids)
        semantics, semantic_diag = ([], {"state": "disabled", "error_code": None})
        if mode != "keyword" and self.config["semantic"]["enabled"]:
            semantics, semantic_diag = self._semantic(db, query, prefix, deadline=deadline)
            if mode == "semantic" and "fallback" in semantic_diag:
                semantic_diag["fallback"] = "none"
        elif mode == "semantic":
            semantic_diag["error_code"] = "SEMANTIC_DISABLED"
        fused = {}
        for label, candidates in (("keyword", keywords), ("semantic", semantics)):
            for rank, item in enumerate(candidates, 1):
                key = (item["file_id"], item["source_version"])
                if key not in fused:
                    fused[key] = {key: item[key] for key in ("file_id", "source_version", "ordinal", "path")}
                    fused[key].update(score=0.0, signals={})
                fused[key]["score"] += 1.0 / (self.config["rrf_k"] + rank)
                fused[key]["signals"][label + "_rank"] = rank
                fused[key]["signals"][label + "_score"] = item["raw_score"]
        hits = sorted(fused.values(), key=lambda item: (-item["score"], item["path"]))[:self.config["candidate_limit"]]
        if before_rerank is not None and rerank and self.config["rerank"]["enabled"] and hits:
            before_rerank(hits[:self.config["rerank"]["max_candidates"]], deadline)
        hits, rerank_diag = self._rerank(db, query, hits, requested=rerank, deadline=deadline)
        status = self.status(db)
        effective_mode = (("semantic_partial" if semantic_diag["state"] == "partial" else "semantic")
                          if semantic_diag["state"] in ("ready", "partial") else "semantic_unavailable") if mode == "semantic" else (
            "bm25_rrf_semantic_partial" if semantic_diag["state"] == "partial" else (
                "bm25_rrf_semantic" if semantic_diag["state"] == "ready" else "bm25"))
        return {"hits": hits, "diagnostics": {"mode": effective_mode, "requested_mode": mode,
                "candidate_filter_applied": keyword_file_ids is not None,
                "keyword_candidate_limit_reached": keyword_truncated,
                "semantic": dict(status["semantic"], query=semantic_diag), "rerank": rerank_diag,
                "candidate_limit": self.config["candidate_limit"], "returned_candidates": len(hits),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "freshness": "indexed_not_revalidated", "exhaustive_for_business_totals": False,
                "snippet_is_evidence": False}}
