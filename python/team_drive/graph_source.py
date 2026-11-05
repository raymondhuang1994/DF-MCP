"""Read-only, folder-scoped Microsoft Graph source; no whole-library mirror.

Remote file contents only enter caller-owned parser snapshots. Bearer tokens,
download URLs and HTTP error bodies are never retained as source metadata.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

from .errors import DriveError
from .bounded_json import loads as bounded_json_loads
from .fs_access import SourceAccess


_GRAPH = "https://graph.microsoft.com/v1.0"
_ID = re.compile(r"^[A-Za-z0-9!._~-]{1,256}$")
_FIELDS = "id,name,size,file,folder,remoteItem,parentReference,eTag,cTag,lastModifiedDateTime,createdDateTime,webUrl,deleted,package"
_JSON_LIMIT = 4 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _fail(code, message, retryable=False, next_action="Check the configured cloud source and reconnect before retrying."):
    raise DriveError(code, message, retryable, next_action)


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in (".", "..", "root"):
        _fail("INVALID_CONFIG", "Graph sources require explicit drive and folder item IDs, not URLs, paths or aliases.")
    return value


def _component(value):
    if (not isinstance(value, str) or not value or len(value) > 255 or value in (".", "..") or
            any(c in value for c in '/\\<>:"|?*') or value[-1:] in (".", " ") or
            any(ord(c) < 32 for c in value) or
            re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I)):
        _fail("GRAPH_UNSAFE_PATH", "Cloud item names must be ordinary unambiguous relative path components.")
    return value


def _parts(relative):
    if not isinstance(relative, str) or not relative or len(relative) > 4096:
        _fail("INVALID_PATH", "Use an indexed relative cloud path.")
    return tuple(_component(part) for part in relative.split("/"))


def _tag(item):
    tag = item.get("eTag")
    if not isinstance(tag, str) or not tag or len(tag) > 4096:
        _fail("GRAPH_INVALID_RESPONSE", "The cloud item has no usable version tag.", True)
    return tag


def _timestamp(value):
    try:
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError()
        # Graph DateTimeOffset may carry seven fractional digits. Python 3.9's
        # fromisoformat accepts only three or six; parse whole seconds there
        # and retain the fraction separately without floating-point rounding.
        match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
                             r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})", value)
        if match is None:
            raise ValueError()
        zone = "+00:00" if match[3] == "Z" else match[3]
        stamp = datetime.fromisoformat(match[1] + zone)
        fraction_ns = int((match[2] or "").ljust(9, "0"))
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = stamp.astimezone(timezone.utc) - epoch
        return (delta.days * 86400 + delta.seconds) * 1000000000 + fraction_ns
    except (ValueError, TypeError, OverflowError):
        _fail("GRAPH_INVALID_RESPONSE", "The cloud item modification timestamp is invalid.", True)


def _safe_web_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        url = urllib.parse.urlsplit(value)
        host = (url.hostname or "").lower()
        if (url.scheme == "https" and not url.username and not url.password and url.port in (None, 443)
                and (host.endswith(".sharepoint.com") or host in ("onedrive.live.com", "onedrive.com", "www.onedrive.com"))):
            # A citation is a browser page URL, never a temporary content credential.
            if not any(word in url.query.casefold() for word in ("tempauth=", "access_token=", "authkey=")):
                return value
    except ValueError:
        pass
    return None


class GraphSource:
    provider = "microsoft_graph"

    def __init__(self, root, graph_config, max_file_bytes, token_provider=None):
        if not isinstance(graph_config, dict):
            _fail("INVALID_CONFIG", "Graph source settings must be an object.")
        self.drive_id = _identifier(graph_config.get("drive_id"))
        self.folder_id = _identifier(graph_config.get("folder_id"))
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
            _fail("INVALID_CONFIG", "The cloud source file-size limit must be positive.")
        self.max_file_bytes = max_file_bytes
        self.limits = {}
        for name, default, maximum, integer in (
            ("timeout_seconds", 10, 20, False), ("max_items", 10000, 100000, True),
            ("max_pages", 1000, 10000, True), ("max_depth", 32, 128, True),
            ("scan_budget_seconds", 120, 600, False),
        ):
            value = graph_config.get(name, default)
            minimum = 1 if integer else .1
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    value > maximum or value < minimum or not math.isfinite(value) or
                    (integer and not isinstance(value, int))):
                _fail("INVALID_CONFIG", "Graph source limits must be finite values within their supported bounds.")
            self.limits[name] = value
        if token_provider is None or not callable(getattr(token_provider, "token", None)):
            _fail("GRAPH_AUTH_REQUIRED", "The configured Graph token provider is required.")
        self.token_provider = token_provider
        self._local = SourceAccess(root)
        self.root, self.identity = self._local.root, self._local.identity
        self._anchor = "/drives/%s/items/%s" % (urllib.parse.quote(self.drive_id, safe=""), urllib.parse.quote(self.folder_id, safe=""))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._metadata = {}
        self._lock = threading.RLock()
        try:
            self.check_local_root()
        except Exception:
            self._local.close()
            raise

    def check_local_root(self):
        self._local.check_root()
        try:
            with os.scandir(self.root) as children:
                if next(children, None) is not None:
                    _fail("GRAPH_ANCHOR_NOT_EMPTY", "The dedicated cloud anchor must remain empty; cloud files are not mirrored there.")
            self._local.check_root()
        except OSError:
            _fail("SOURCE_ROOT_CHANGED", "The dedicated cloud anchor is inaccessible.")

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("GRAPH_TIMEOUT", "The bounded cloud operation timed out.", True)
        return min(self.limits["timeout_seconds"], remaining)

    def _open(self, url, deadline, authenticated=True, accept_redirect=False):
        self._remaining(deadline)
        headers = {"Accept": "application/octet-stream" if accept_redirect else "application/json"}
        if authenticated:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc.lower() != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/") or parsed.fragment:
                _fail("GRAPH_UNSAFE_URL", "A cloud API request escaped the fixed Graph endpoint.")
            token = self.token_provider.token()
            if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
                _fail("GRAPH_AUTH_REQUIRED", "The configured Graph token is unavailable.")
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=self._remaining(deadline))
        except urllib.error.HTTPError as exc:
            if accept_redirect and exc.code in (301, 302, 303, 307, 308):
                return exc
            code = {401: "GRAPH_AUTH_REQUIRED", 403: "GRAPH_ACCESS_DENIED", 404: "GRAPH_ITEM_UNAVAILABLE", 429: "GRAPH_THROTTLED"}.get(exc.code, "GRAPH_UNAVAILABLE")
            exc.close()
            _fail(code, "The cloud service did not authorize or complete the request.", exc.code not in (401, 403),
                  "Restore access or retry after the service's throttling interval; an HTTP failure is not an empty folder.")
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            _fail("GRAPH_UNAVAILABLE", "The cloud service could not be reached within the request budget.", True)
        if getattr(response, "status", None) != 200 and not (accept_redirect and getattr(response, "status", None) in (301, 302, 303, 307, 308)):
            response.close()
            _fail("GRAPH_INVALID_RESPONSE", "The cloud response status is unsupported.", True)
        return response

    def _chunks(self, response, deadline, limit):
        total = 0
        expected = response.headers.get("Content-Length")
        if expected is not None:
            if not re.fullmatch(r"\d{1,20}", expected):
                _fail("GRAPH_INVALID_RESPONSE", "The cloud response length is invalid.", True)
            expected = int(expected)
            if expected > limit:
                _fail("FILE_TOO_LARGE", "The cloud response exceeds its configured size limit.")
        try:
            while True:
                remaining = self._remaining(deadline)
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                reader = getattr(response, "read1", response.read)
                chunk = reader(min(65536, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    _fail("FILE_TOO_LARGE", "The cloud response exceeds its configured size limit.")
                yield chunk
            if expected is not None and total != expected:
                _fail("GRAPH_INVALID_RESPONSE", "The cloud response ended before its declared length.", True)
        except (OSError, http.client.HTTPException):
            _fail("GRAPH_UNAVAILABLE", "The cloud response was interrupted; partial content is not evidence.", True)

    def _json(self, url, deadline):
        with self._open(url, deadline) as response:
            raw = b"".join(self._chunks(response, deadline, _JSON_LIMIT))
        try:
            result = bounded_json_loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            _fail("GRAPH_INVALID_RESPONSE", "The cloud response is not a bounded valid JSON object.", True)
        if not isinstance(result, dict):
            _fail("GRAPH_INVALID_RESPONSE", "The cloud response is not a JSON object.", True)
        return result

    def _metadata_url(self, relative=None):
        route = self._anchor
        if relative is not None:
            route += ":/" + "/".join(urllib.parse.quote(part, safe="") for part in _parts(relative))
        return _GRAPH + route + "?$select=" + _FIELDS

    def _validate_item(self, item, expected_name=None, parent_id=None, folder=False):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]):
            _fail("GRAPH_INVALID_RESPONSE", "The cloud item identity is invalid.", True)
        name = _component(item.get("name"))
        if expected_name is not None and name != expected_name:
            _fail("GRAPH_SCOPE_CHANGED", "The relative cloud path resolved to a different item name.", True)
        parent = item.get("parentReference", {})
        if not isinstance(parent, dict) or parent.get("driveId", self.drive_id) != self.drive_id or (parent_id is not None and (parent.get("id") != parent_id or parent.get("driveId") != self.drive_id)):
            _fail("GRAPH_SCOPE_CHANGED", "The cloud item no longer belongs to the approved drive or parent folder.", True)
        if item.get("deleted") is not None:
            _fail("GRAPH_ITEM_UNAVAILABLE", "The cloud item is deleted or unavailable.", True)
        if folder and (not isinstance(item.get("folder"), dict) or item.get("remoteItem") is not None or item.get("package") is not None):
            _fail("GRAPH_SCOPE_CHANGED", "The configured scope is not an ordinary accessible folder.")
        return item

    def _remote_root(self, deadline):
        result = self._validate_item(self._json(self._metadata_url(), deadline), folder=True)
        if result["id"] != self.folder_id:
            _fail("GRAPH_SCOPE_CHANGED", "The configured folder identity has changed.")
        return result

    def check_root(self):
        self.check_local_root()
        self._remote_root(time.monotonic() + self.limits["timeout_seconds"])

    def _resolve(self, relative, deadline):
        parts = _parts(relative)
        if len(parts) > self.limits["max_depth"] + 1:
            _fail("GRAPH_SCAN_LIMIT", "The cloud path exceeds the configured folder depth.")
        parent_id = self.folder_id
        # A path through a shared shortcut could resolve outside the approved
        # folder even when its final item has no remoteItem facet. Verify every
        # ancestor from the configured root; never trust only the final item ID.
        for length in range(1, len(parts)):
            parent = self._validate_item(self._json(self._metadata_url("/".join(parts[:length])), deadline),
                                         expected_name=parts[length - 1], parent_id=parent_id, folder=True)
            parent_id = parent["id"]
        return self._validate_item(self._json(self._metadata_url(relative), deadline), expected_name=parts[-1], parent_id=parent_id)

    def _origin(self, item):
        def bounded(value, maximum):
            return value if isinstance(value, str) and len(value) <= maximum else None
        return {"provider": self.provider, "drive_id": self.drive_id, "folder_id": self.folder_id,
                "item_id": item["id"], "web_url": _safe_web_url(item.get("webUrl")),
                "etag": bounded(item.get("eTag"), 4096), "ctag": bounded(item.get("cTag"), 4096),
                "remote_modified_at": bounded(item.get("lastModifiedDateTime"), 80),
                "verified_at": datetime.now(timezone.utc).isoformat()}

    def _info(self, item, blocked=False):
        size = item.get("size", 0)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            _fail("GRAPH_INVALID_RESPONSE", "The cloud item size is invalid.", True)
        etag = item.get("eTag", "") if blocked else _tag(item)
        modified = _timestamp(item["lastModifiedDateTime"]) if item.get("lastModifiedDateTime") else 0
        return SimpleNamespace(st_size=size, st_mtime_ns=modified,
                               st_ctime_ns=int.from_bytes(hashlib.sha256(str(etag).encode()).digest()[:8], "big") & ((1 << 63) - 1),
                               st_mode=stat.S_IFREG, st_nlink=1,
                               st_ino=int.from_bytes(hashlib.sha256(item["id"].encode()).digest()[:8], "big"),
                               st_dev=int.from_bytes(hashlib.sha256(self.drive_id.encode()).digest()[:8], "big"))

    def _next_page(self, value, expected_path):
        if not isinstance(value, str) or len(value) > 16384:
            _fail("GRAPH_UNSAFE_URL", "Cloud pagination escaped the approved collection.")
        try:
            parsed = urllib.parse.urlsplit(value)
            allowed_query = {"$skiptoken", "$top", "$select", "$orderby"}
            keys = {key.casefold() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
            if (parsed.scheme != "https" or
                    parsed.netloc.lower() != "graph.microsoft.com" or parsed.fragment or
                    urllib.parse.unquote(parsed.path) != urllib.parse.unquote(expected_path) or not keys <= allowed_query):
                raise ValueError()
        except (ValueError, TypeError):
            _fail("GRAPH_UNSAFE_URL", "Cloud pagination escaped the approved collection.")
        return value

    def walk(self, errors):
        """Publish no partial manifest; HTTP failures cannot become empty inventories."""
        self.check_local_root()
        deadline = time.monotonic() + self.limits["scan_budget_seconds"]
        observed, output, visited, observed_ids, pages = {}, [], set(), set(), 0
        try:
            self._remote_root(deadline)
            pending = [(self.folder_id, "", 0)]
            while pending:
                self._remaining(deadline)
                folder_id, relative, depth = pending.pop()
                if depth > self.limits["max_depth"] or folder_id in visited:
                    _fail("GRAPH_SCOPE_CHANGED", "The cloud folder hierarchy exceeds its depth bound or contains a cycle.")
                visited.add(folder_id)
                before = self._resolve(relative, deadline) if relative else self._remote_root(deadline)
                if before["id"] != folder_id:
                    _fail("GRAPH_SCOPE_CHANGED", "A cloud folder moved while it was being enumerated.", True)
                self._validate_item(before, folder=True)
                route = "/v1.0/drives/%s/items/%s/children" % (urllib.parse.quote(self.drive_id, safe=""), urllib.parse.quote(folder_id, safe=""))
                url, seen_links = "https://graph.microsoft.com" + route + "?$select=" + _FIELDS, set()
                while url:
                    if url in seen_links:
                        _fail("GRAPH_INVALID_RESPONSE", "Cloud pagination repeated a page.", True)
                    seen_links.add(url)
                    pages += 1
                    if pages > self.limits["max_pages"]:
                        _fail("GRAPH_SCAN_LIMIT", "Cloud inventory exceeds the configured page limit.")
                    page = self._json(url, deadline)
                    values = page.get("value")
                    if not isinstance(values, list):
                        _fail("GRAPH_INVALID_RESPONSE", "A cloud inventory page has no valid item collection.", True)
                    for raw in values:
                        item = self._validate_item(raw, parent_id=folder_id)
                        path = relative + "/" + item["name"] if relative else item["name"]
                        _parts(path)
                        if path in observed or item["id"] in observed_ids:
                            _fail("GRAPH_SCOPE_CHANGED", "A cloud item appeared under conflicting paths during enumeration.", True)
                        observed[path] = self._origin(item)
                        observed_ids.add(item["id"])
                        if len(observed) > self.limits["max_items"]:
                            _fail("GRAPH_SCAN_LIMIT", "Cloud inventory exceeds the configured item limit.")
                        blocked = item.get("remoteItem") is not None or item.get("package") is not None
                        if isinstance(item.get("folder"), dict) and not blocked:
                            pending.append((item["id"], path, depth + 1))
                        else:
                            blocked = blocked or not isinstance(item.get("file"), dict)
                            output.append((path, self._info(item, blocked), blocked, False))
                    next_link = page.get("@odata.nextLink")
                    url = self._next_page(next_link, route) if next_link else None
                after = self._resolve(relative, deadline) if relative else self._remote_root(deadline)
                if after["id"] != before["id"] or after.get("eTag") != before.get("eTag"):
                    _fail("GRAPH_SCOPE_CHANGED", "A cloud folder changed during inventory; retry the complete scan.", True)
            self._remote_root(deadline)
            self.check_local_root()
        except DriveError:
            errors.append(True)
            raise
        with self._lock:
            self._metadata = observed
        yield from output

    @staticmethod
    def _download_url(value):
        if not isinstance(value, str) or len(value) > 32768:
            _fail("GRAPH_UNSAFE_URL", "The content redirect is not an approved Microsoft HTTPS download host.")
        try:
            parsed = urllib.parse.urlsplit(value)
            host = (parsed.hostname or "").lower()
            allowed = (host.endswith(".sharepoint.com") or host.endswith(".sharepointonline.com") or
                       host.endswith(".1drv.com") or host in ("onedrive.live.com", "onedrive.com", "www.onedrive.com"))
            if (parsed.scheme != "https" or not allowed or
                    parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
                raise ValueError()
        except (ValueError, TypeError):
            _fail("GRAPH_UNSAFE_URL", "The content redirect is not an approved Microsoft HTTPS download host.")
        return value

    def digest(self, relative, snapshot=None):
        """Hash downloaded bytes and bind the result to an unchanged remote item/tag."""
        self.check_local_root()
        deadline = time.monotonic() + 30
        self._remote_root(deadline)
        before = self._resolve(relative, deadline)
        if before.get("remoteItem") is not None or before.get("package") is not None or not isinstance(before.get("file"), dict):
            _fail("GRAPH_SCOPE_CHANGED", "Shortcuts, packages and non-file cloud items are not read.")
        info = self._info(before)
        if info.st_size > self.max_file_bytes:
            _fail("FILE_TOO_LARGE", "The cloud file exceeds max_file_bytes.")
        url = _GRAPH + self._anchor + ":/" + "/".join(urllib.parse.quote(part, safe="") for part in _parts(relative)) + ":/content"
        digest, size, authenticated = hashlib.sha256(), 0, True
        for attempt in range(4):
            with self._open(url, deadline, authenticated, accept_redirect=True) as response:
                if response.status != 200:
                    url = self._download_url(response.headers.get("Location"))
                    authenticated = False
                    continue
                for chunk in self._chunks(response, deadline, self.max_file_bytes):
                    digest.update(chunk)
                    size += len(chunk)
                    if snapshot is not None:
                        snapshot.write(chunk)
                break
        else:
            _fail("GRAPH_UNSAFE_URL", "The content download exceeded the redirect bound.")
        if size != info.st_size:
            _fail("GRAPH_SOURCE_CHANGED", "Downloaded size does not match the selected cloud version.", True)
        after = self._resolve(relative, deadline)
        if (after["id"] != before["id"] or _tag(after) != _tag(before) or after.get("size") != before.get("size") or
                after.get("remoteItem") is not None or not isinstance(after.get("file"), dict)):
            _fail("GRAPH_SOURCE_CHANGED", "The cloud file identity or version changed during download.", True)
        self._remote_root(deadline)
        self.check_local_root()
        content_hash = digest.hexdigest()
        version_fields = {"provider": self.provider, "drive_id": self.drive_id, "item_id": before["id"],
                          "etag": _tag(before), "content_sha256": content_hash}
        version = hashlib.sha256(json.dumps(version_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        origin = dict(self._origin(after), content_sha256=content_hash, source_version=version)
        with self._lock:
            self._metadata[relative] = origin
        return version, info

    def evidence(self, relative):
        """Return only already-observed safe provenance; never perform network I/O."""
        with self._lock:
            return dict(self._metadata.get(relative, {}))

    def verify_origin(self, relative, origin, deadline=None):
        """Reauthorize cached discovery metadata at its current scoped cloud path.

        This is deliberately independent of cached inventory and downloads no
        content. A caller may pass one deadline shared by all candidate checks.
        """
        self.check_local_root()
        deadline = time.monotonic() + 30 if deadline is None else deadline
        self._remaining(deadline)
        if (not isinstance(origin, dict) or origin.get("provider") != self.provider or
                origin.get("drive_id") != self.drive_id or origin.get("folder_id") != self.folder_id or
                not isinstance(origin.get("item_id"), str)):
            _fail("GRAPH_SOURCE_CHANGED", "Cached provenance does not belong to this configured cloud scope.", True)
        current = self._resolve(relative, deadline)
        if current["id"] != origin["item_id"] or current.get("eTag") != origin.get("etag"):
            _fail("GRAPH_SOURCE_CHANGED", "The cloud item identity or version no longer matches cached provenance.", True)
        if origin.get("content_sha256") and (current.get("remoteItem") is not None or current.get("package") is not None or not isinstance(current.get("file"), dict)):
            _fail("GRAPH_SOURCE_CHANGED", "Cached file evidence now resolves to a shortcut or a non-file cloud item.", True)
        self._remaining(deadline)
        self.check_local_root()
        return self._origin(current)

    def close(self):
        self._local.close()
