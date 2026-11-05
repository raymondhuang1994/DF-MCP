"""Bounded, metadata-only discovery for an explicitly authorized Graph account.

No source profile, index, source contents or tenant-wide inventory is created.
The supplied TokenProvider may refresh its private OAuth cache. Call this from
an explicit discovery action, not an offline/dry-run configuration planner.
Sharing-token redemption (/shares) is deliberately unsupported because its
documented delegated permissions include write access.
"""
from __future__ import annotations

import math
import re
import time
import urllib.parse
import urllib.request

from .errors import DriveError
from .graph_source import GraphSource, _GRAPH, _NoRedirect, _component, _fail, _identifier


_DRIVE_FIELDS = "id,name,webUrl,driveType"
_ITEM_FIELDS = "id,name,webUrl,folder,file,remoteItem,package,deleted,parentReference"
_SITE_FIELDS = "id,name,displayName,webUrl"
_SHAREPOINT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.sharepoint\.com$")
_GUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_FALLBACK = "Choose a folder from an already authorized drive or ask IT for its drive and folder IDs; no write scope is requested."


def _unsupported(message="This link cannot be resolved with this read-only discovery flow."):
    _fail("GRAPH_UNSUPPORTED_LINK", message, False, _FALLBACK)


def _url(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip() or
            any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value or
            re.search(r"%(?![0-9A-Fa-f]{2})", value)):
        _unsupported("Use an ordinary HTTPS SharePoint or OneDrive folder link.")
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or
                parsed.port not in (None, 443) or parsed.netloc.endswith(":") or parsed.fragment or
                not (_SHAREPOINT.fullmatch(host) or host in {"onedrive.live.com", "1drv.ms"})):
            raise ValueError()
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                                       encoding="utf-8", errors="strict", max_num_fields=64) if parsed.query else []
        query = {}
        for key, item in pairs:
            key = key.casefold()
            if key in query or any(ord(char) < 32 for char in key + item):
                raise ValueError()
            query[key] = item
        if set(query) & {"authkey", "access_token", "tempauth", "token"}:
            _unsupported("Credential-bearing sharing links are not used by this read-only discovery flow.")
        return parsed, host, query
    except (ValueError, UnicodeError):
        _unsupported("Use an ordinary HTTPS SharePoint or OneDrive folder link.")


def _path(value, encoded=True):
    """Decode each segment once; encoded separators never become routing syntax."""
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or
            value.endswith("//") or len(value) > 4096):
        _unsupported("The folder link does not contain an unambiguous server-relative path.")
    raw_parts = value.strip("/").split("/") if value.strip("/") else []
    if len(raw_parts) > 64:
        _unsupported("The folder link exceeds the supported path-depth limit.")
    parts = []
    for part in raw_parts:
        try:
            part = urllib.parse.unquote(part, encoding="utf-8", errors="strict") if encoded else part
            parts.append(_component(part))
        except (ValueError, UnicodeError):
            _unsupported("The folder link contains invalid path encoding.")
    return tuple(parts)


def _quote_path(parts):
    return "/".join(urllib.parse.quote(part, safe="") for part in parts)


def _label(value):
    # Site/library display titles are not filesystem path components: colons
    # and other punctuation may be ordinary text in these metadata fields.
    if (not isinstance(value, str) or not value or len(value) > 255 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail("GRAPH_INVALID_RESPONSE", "The cloud display name is invalid.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("GRAPH_INVALID_RESPONSE", "The cloud display name is invalid.")
    return value


def _same_path(left, right):
    # Graph's filesystem path identifiers are case-insensitive. IDs remain
    # exact strings throughout discovery and the final source configuration.
    return tuple(part.casefold() for part in left) == tuple(part.casefold() for part in right)


def _site_id(value):
    if not isinstance(value, str) or len(value) > 256:
        _fail("INVALID_CONFIG", "Use the explicit site ID returned by discovery or supplied by IT.")
    parts = value.split(",")
    if len(parts) != 3 or not _SHAREPOINT.fullmatch(parts[0].lower()) or any(not re.fullmatch(_GUID, part) for part in parts[1:]):
        _fail("INVALID_CONFIG", "Use the explicit site ID returned by discovery or supplied by IT.")
    return value


def _display_url(value):
    if value is None:
        return None
    parsed, host, _ = _url(value)
    if host == "1drv.ms":
        _unsupported("The cloud metadata returned a sharing shortcut instead of a stable browser URL.")
    return urllib.parse.urlunsplit(("https", host, parsed.path, parsed.query, ""))


class GraphDiscovery:
    """Find bounded selections; final source approval belongs to the caller.

    ``list_drives()`` lists /me/drives, not every shared library in the tenant.
    To inspect a known site, pass its explicit ID or first ``resolve_site`` its
    URL. Site resolution may require an already granted Sites.Read.All scope;
    no method changes scopes or starts interactive authorization.
    """
    # Reuse the source's hardened fixed-host GET transport, byte/depth bounds,
    # timeout handling, no-proxy/no-redirect policy and sanitized HTTP errors.
    # No GraphSource instance/anchor is created and no content API is exposed.
    _remaining = GraphSource._remaining
    _open = GraphSource._open
    _chunks = GraphSource._chunks
    _json = GraphSource._json

    def __init__(self, token_provider, *, timeout_seconds=10, max_pages=10, max_items=500, budget_seconds=20):
        if not callable(getattr(token_provider, "token", None)):
            _fail("GRAPH_AUTH_REQUIRED", "An explicitly authorized Graph token provider is required.")
        for value, minimum, maximum, integer in ((timeout_seconds, .1, 20, False), (budget_seconds, .1, 60, False),
                                                  (max_pages, 1, 50, True), (max_items, 1, 5000, True)):
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or
                    not minimum <= value <= maximum or (integer and not isinstance(value, int))):
                _fail("INVALID_CONFIG", "Discovery limits must be bounded finite numbers.")
        self.token_provider = token_provider
        self.limits = {"timeout_seconds": timeout_seconds}
        self.max_pages, self.max_items, self.budget_seconds = max_pages, max_items, budget_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _deadline(self):
        return time.monotonic() + self.budget_seconds

    def _get(self, route, fields, deadline):
        return self._json(_GRAPH + route + "?$select=" + fields, deadline)

    def _collection(self, route, fields, deadline):
        url = _GRAPH + route + "?$select=" + fields + "&$top=" + str(min(self.max_items, 200))
        values, seen_urls, seen_ids, pages = [], set(), set(), 0
        expected_path = "/v1.0" + route
        while url:
            if pages >= self.max_pages:
                _fail("GRAPH_DISCOVERY_LIMIT", "This selection exceeds the discovery page limit.", False, _FALLBACK)
            if url in seen_urls:
                _fail("GRAPH_INVALID_RESPONSE", "Cloud discovery repeated a page.", True)
            seen_urls.add(url)
            page = self._json(url, deadline)
            pages += 1
            raw_items = page.get("value")
            if not isinstance(raw_items, list):
                _fail("GRAPH_INVALID_RESPONSE", "Cloud discovery returned an invalid collection.", True)
            if len(values) + len(raw_items) > self.max_items:
                _fail("GRAPH_DISCOVERY_LIMIT", "This selection exceeds the discovery item limit.", False, _FALLBACK)
            for item in raw_items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen_ids:
                    _fail("GRAPH_INVALID_RESPONSE", "Cloud discovery returned a missing or repeated item identity.", True)
                seen_ids.add(item["id"])
                values.append(item)
            next_link = page.get("@odata.nextLink")
            if next_link is None:
                url = None
                continue
            if (not isinstance(next_link, str) or len(next_link) > 16384 or "\\" in next_link or
                    any(ord(char) < 32 or ord(char) == 127 for char in next_link)):
                _fail("GRAPH_UNSAFE_URL", "Discovery pagination escaped the selected collection.")
            try:
                parsed = urllib.parse.urlsplit(next_link)
                query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
                keys = [key.casefold() for key, _ in query]
                if (parsed.scheme != "https" or parsed.netloc.lower() != "graph.microsoft.com" or parsed.fragment or
                        parsed.path != expected_path or len(keys) != len(set(keys)) or
                        not set(keys) <= {"$skiptoken", "$top", "$select", "$orderby"} or "$skiptoken" not in keys):
                    raise ValueError()
                for key, value in query:
                    if (key.casefold() == "$select" and value != fields or
                            key.casefold() == "$orderby" and value not in ("name", "name asc", "name desc") or
                            key.casefold() == "$top" and (not value.isdigit() or not 1 <= int(value) <= 200)):
                        raise ValueError()
            except (ValueError, TypeError):
                _fail("GRAPH_UNSAFE_URL", "Discovery pagination escaped the selected metadata collection.")
            url = next_link
        return values, pages

    def _drive(self, raw):
        drive_type = raw.get("driveType")
        return {"drive_id": _identifier(raw.get("id")), "name": _label(raw.get("name")),
                "web_url": _display_url(raw.get("webUrl")),
                "drive_type": drive_type if isinstance(drive_type, str) and drive_type in {"personal", "business", "documentLibrary"} else None}

    def _drives(self, site_id, deadline):
        route = "/me/drives" if site_id is None else "/sites/" + urllib.parse.quote(_site_id(site_id), safe=",") + "/drives"
        raw, pages = self._collection(route, _DRIVE_FIELDS, deadline)
        return {"items": [self._drive(item) for item in raw], "complete": True, "has_more": False, "pages": pages,
                "collection": "user_drives" if site_id is None else "site_drives"}

    def list_drives(self, site_id=None):
        return self._drives(site_id, self._deadline())

    def _site(self, host, parts, deadline):
        route = "/sites/" + host + (":/" + _quote_path(parts) if parts else "")
        raw = self._get(route, _SITE_FIELDS, deadline)
        site_id = _site_id(raw.get("id"))
        display = _display_url(raw.get("webUrl"))
        if not display:
            _fail("GRAPH_INVALID_RESPONSE", "The discovered site has no stable browser URL.")
        parsed, actual_host, _ = _url(display)
        if actual_host != host or not _same_path(_path(parsed.path or "/"), parts) or site_id.split(",")[0].casefold() != host:
            _fail("GRAPH_SCOPE_CHANGED", "The site link resolved outside the specified site.")
        name = raw.get("displayName", raw.get("name"))
        return {"site_id": site_id, "name": _label(name), "web_url": display}

    def resolve_site(self, site_url):
        parsed, host, query = _url(site_url)
        if not _SHAREPOINT.fullmatch(host) or query:
            _unsupported("Use the direct SharePoint site URL without a sharing token or query.")
        return self._site(host, _path(parsed.path or "/"), self._deadline())

    def _folder(self, raw, drive_id, expected_id=None, parent_id=None):
        if not isinstance(raw, dict):
            _fail("GRAPH_INVALID_RESPONSE", "Cloud discovery returned an invalid folder.")
        item_id = _identifier(raw.get("id"))
        parent = raw.get("parentReference", {})
        if (raw.get("deleted") is not None or not isinstance(raw.get("folder"), dict) or
                raw.get("remoteItem") is not None or raw.get("package") is not None):
            _fail("GRAPH_UNSUPPORTED_FOLDER", "Select an ordinary folder; files, packages and shared shortcuts are not source roots.", False, _FALLBACK)
        if (not isinstance(parent, dict) or parent.get("driveId", drive_id) != drive_id or
                expected_id is not None and item_id != expected_id or
                parent_id is not None and (parent.get("id") != parent_id or parent.get("driveId") != drive_id)):
            _fail("GRAPH_SCOPE_CHANGED", "The folder no longer belongs to the selected drive or parent.")
        return {"provider": "microsoft_graph", "drive_id": drive_id, "folder_id": item_id,
                "name": _component(raw.get("name")), "web_url": _display_url(raw.get("webUrl"))}

    def _get_folder(self, drive_id, folder_id, deadline):
        drive_id = _identifier(drive_id)
        suffix = "/root" if folder_id is None else "/items/" + urllib.parse.quote(_identifier(folder_id), safe="")
        raw = self._get("/drives/" + urllib.parse.quote(drive_id, safe="") + suffix, _ITEM_FIELDS, deadline)
        return self._folder(raw, drive_id, folder_id)

    def get_folder(self, drive_id, folder_id=None):
        return self._get_folder(drive_id, folder_id, self._deadline())

    def list_folders(self, drive_id, folder_id=None):
        deadline = self._deadline()
        parent = self._get_folder(drive_id, folder_id, deadline)
        route = "/drives/%s/items/%s/children" % (urllib.parse.quote(drive_id, safe=""), urllib.parse.quote(parent["folder_id"], safe=""))
        raw, pages = self._collection(route, _ITEM_FIELDS, deadline)
        items, excluded = [], {"files": 0, "shortcuts": 0, "packages": 0}
        for item in raw:
            if item.get("remoteItem") is not None:
                excluded["shortcuts"] += 1
            elif item.get("package") is not None:
                excluded["packages"] += 1
            elif not isinstance(item.get("folder"), dict):
                excluded["files"] += 1
            else:
                items.append(self._folder(item, drive_id, parent_id=parent["folder_id"]))
        return {"items": items, "parent": parent, "complete": True, "has_more": False, "pages": pages,
                "collection": "folders", "excluded": excluded}

    def _match_drive(self, drives, host, parts):
        matches = []
        for drive in drives:
            if not drive["web_url"]:
                continue
            parsed, drive_host, _ = _url(drive["web_url"])
            prefix = _path(parsed.path or "/")
            if drive_host == host and _same_path(parts[:len(prefix)], prefix):
                matches.append((drive, parts[len(prefix):]))
        if len(matches) > 1:
            _unsupported("The link matches multiple libraries; select the intended library explicitly.")
        return matches[0] if matches else None

    def resolve_folder(self, url, *, drive_id=None, site_id=None):
        """Resolve a direct browser path; never fetch the supplied URL itself."""
        if drive_id is not None:
            drive_id = _identifier(drive_id)
        if site_id is not None:
            site_id = _site_id(site_id)
        if drive_id is not None and site_id is not None:
            _fail("INVALID_CONFIG", "Select either a known drive or a known site, not both.")
        parsed, host, query = _url(url)
        if host == "1drv.ms":
            _unsupported("Short sharing links require folder selection or IT-provided IDs in this read-only flow.")
        deadline = self._deadline()
        if host == "onedrive.live.com":
            cid, item_id = query.get("cid"), query.get("id", query.get("resid"))
            if (parsed.path not in ("", "/") or not isinstance(cid, str) or not re.fullmatch(r"[A-Fa-f0-9]{16}", cid) or
                    not isinstance(item_id, str) or not re.fullmatch(re.escape(cid) + r"![A-Za-z0-9]+", item_id) or
                    drive_id is not None and drive_id != cid or site_id is not None):
                _unsupported("Use a direct OneDrive folder URL with its CID and item ID, or select the folder.")
            return self._get_folder(cid, item_id, deadline)
        if parsed.path.startswith("/:f:/r/"):
            source_path = parsed.path[len("/:f:/r"):]
        elif parsed.path.startswith("/:"):
            _unsupported("This sharing link has no direct folder path; use folder selection instead.")
        else:
            source_path = parsed.path
        selectors = [query[key] for key in ("id", "rootfolder") if key in query]
        if len(selectors) > 1:
            _unsupported("The link has multiple folder selectors; choose the folder explicitly.")
        parts = _path(selectors[0], encoded=False) if selectors else _path(source_path)
        if any(part.casefold() in {"_layouts", "forms"} or part.casefold().endswith(".aspx") for part in parts):
            _unsupported("The browser page does not identify a direct folder path.")
        if drive_id is not None:
            selected = self._drive(self._get("/drives/" + urllib.parse.quote(_identifier(drive_id), safe=""), _DRIVE_FIELDS, deadline))
            if selected["drive_id"] != drive_id:
                _fail("GRAPH_SCOPE_CHANGED", "The selected drive identity changed.")
            match = self._match_drive([selected], host, parts)
        elif site_id is not None:
            match = self._match_drive(self._drives(site_id, deadline)["items"], host, parts)
        else:
            try:
                available = self._drives(None, deadline)["items"]
            except DriveError as exc:
                # A user can have SharePoint access without a provisioned
                # personal OneDrive. Only that missing-resource response may
                # fall through to this one explicitly named site. Permission,
                # authentication and transient failures remain visible.
                if exc.code != "GRAPH_ITEM_UNAVAILABLE":
                    raise
                available = []
            match = self._match_drive(available, host, parts)
            if match is None:
                # Only one site derived from this URL, never /sites?search or a
                # tenant-wide list. Known site IDs avoid this scope-dependent call.
                site_parts = parts[:2] if len(parts) >= 2 and parts[0].casefold() in {"sites", "teams", "personal"} else ()
                site = self._site(host, site_parts, deadline)
                match = self._match_drive(self._drives(site["site_id"], deadline)["items"], host, parts)
        if match is None:
            _unsupported("The link did not match an authorized library; select a library and folder or ask IT for IDs.")
        drive, relative = match
        parent = self._get_folder(drive["drive_id"], None, deadline)
        for length in range(1, len(relative) + 1):
            route = "/drives/" + urllib.parse.quote(drive["drive_id"], safe="") + "/root:/" + _quote_path(relative[:length])
            raw = self._get(route, _ITEM_FIELDS, deadline)
            current = self._folder(raw, drive["drive_id"], parent_id=parent["folder_id"])
            if not _same_path((current["name"],), (relative[length - 1],)):
                _fail("GRAPH_SCOPE_CHANGED", "The browser path resolved to a different folder name.")
            parent = current
        return parent
