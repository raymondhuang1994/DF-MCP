"""Discovery contracts use only synthetic metadata and forbid real networking."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from team_drive.errors import DriveError
from team_drive.graph_auth import TokenProvider
from team_drive.graph_discovery import GraphDiscovery


HOST = "tenant.sharepoint.com"
SITE_ID = HOST + ",11111111-1111-1111-1111-111111111111,22222222-2222-2222-2222-222222222222"
LIBRARY = "https://" + HOST + "/sites/Team/Shared%20Documents"
DRIVE = {"id": "drive-1", "name": "Documents", "webUrl": LIBRARY, "driveType": "documentLibrary"}


def folder(item_id="folder-1", name="Approved", parent="root-1", drive="drive-1", **changes):
    value = {"id": item_id, "name": name, "folder": {}, "parentReference": {"id": parent, "driveId": drive},
             "webUrl": LIBRARY + "/" + urllib.parse.quote(name, safe="")}
    value.update(changes)
    return value


class Token:
    def token(self):
        return "SYNTHETIC_TOKEN"


class Response:
    def __init__(self, data, status=200):
        self.body = io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.status = status
        self.headers = {"Content-Length": str(len(self.body.getvalue()))}
        self.closed = False

    def read(self, count=-1):
        return self.body.read(count)

    read1 = read

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeGraph:
    def __init__(self):
        self.requests = []
        self.routes = {
            "/me/drives": {"value": [DRIVE]},
            "/drives/drive-1": DRIVE,
            "/drives/drive-1/root": folder("root-1", "Documents", parent="outside", webUrl=LIBRARY),
            "/drives/drive-1/items/folder-1": folder(),
            "/drives/drive-1/root:/Approved": folder(),
            "/drives/drive-1/items/root-1/children": {"value": [folder()]},
            "/sites/" + HOST + ":/sites/Team": {"id": SITE_ID, "name": "Team", "webUrl": "https://" + HOST + "/sites/Team"},
            "/sites/" + SITE_ID + "/drives": {"value": [DRIVE]},
        }

    def open(self, request, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
            raise AssertionError("Discovery may only call the fixed Graph endpoint")
        if request.get_method() != "GET" or request.data is not None:
            raise AssertionError("Discovery must be read-only")
        path = urllib.parse.unquote(parsed.path[len("/v1.0"):])
        if any(value in path for value in ("/content", "/shares", "/permissions", "/delta", "/search")):
            raise AssertionError("Discovery must not read content, redeem shares or inventory the tenant")
        query = urllib.parse.parse_qs(parsed.query)
        key = path + ("?page=" + query["$skiptoken"][0] if "$skiptoken" in query else "")
        self.requests.append((request.full_url, request.get_header("Authorization"), timeout))
        result = self.routes[key]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(request, timeout)
        return Response(copy.deepcopy(result))


class GraphDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.network = mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real networking forbidden"))
        self.network.start()
        self.discovery = GraphDiscovery(Token())
        self.graph = FakeGraph()
        self.discovery._opener = self.graph

    def tearDown(self):
        self.network.stop()

    def code(self, expected, function, *args, **kwargs):
        with self.assertRaises(DriveError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def test_construction_is_offline_and_needs_no_source_root_or_drive_ids(self):
        self.assertEqual(self.graph.requests, [])
        for changes in ({"max_items": 0}, {"max_pages": 51}, {"budget_seconds": 61},
                        {"timeout_seconds": True}, {"timeout_seconds": float("nan")}):
            self.code("INVALID_CONFIG", GraphDiscovery, Token(), **changes)
        self.code("GRAPH_AUTH_REQUIRED", GraphDiscovery, None)

    def test_missing_authorization_never_starts_login_or_creates_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary).resolve() / "private" / "auth.json"
            token = TokenProvider({"tenant_id": "organizations", "client_id": "11111111-1111-1111-1111-111111111111"}, cache)
            discovery = GraphDiscovery(token)
            discovery._opener = self.graph
            self.code("AUTH_REQUIRED", discovery.list_drives)
            self.assertFalse(cache.parent.exists())
            self.assertEqual(self.graph.requests, [])

    def test_user_drives_and_explicit_site_libraries_have_precise_collections(self):
        result = self.discovery.list_drives()
        self.assertEqual(result["collection"], "user_drives")
        self.assertTrue(result["complete"])
        self.assertFalse(result["has_more"])
        self.assertEqual(result["items"][0], {"drive_id": "drive-1", "name": "Documents", "web_url": LIBRARY, "drive_type": "documentLibrary"})
        site = self.discovery.resolve_site("https://" + HOST + "/sites/Team")
        self.assertEqual(site["site_id"], SITE_ID)
        self.assertEqual(self.discovery.list_drives(site["site_id"])["collection"], "site_drives")
        self.assertEqual(len(self.graph.requests), 3)

    def test_root_site_accepts_browser_url_with_or_without_trailing_slash(self):
        self.graph.routes["/sites/" + HOST] = {"id": SITE_ID, "name": "Root", "webUrl": "https://" + HOST}
        for url in ("https://" + HOST, "https://" + HOST + "/"):
            with self.subTest(url=url):
                self.assertEqual(self.discovery.resolve_site(url)["site_id"], SITE_ID)

    def test_site_and_library_titles_are_display_text_not_path_components(self):
        self.graph.routes["/sites/" + HOST + ":/sites/Team"]["displayName"] = "Legal: Compliance"
        self.graph.routes["/me/drives"] = {"value": [dict(DRIVE, name="Reports: Q1") ]}
        self.assertEqual(self.discovery.resolve_site("https://" + HOST + "/sites/Team")["name"], "Legal: Compliance")
        self.assertEqual(self.discovery.list_drives()["items"][0]["name"], "Reports: Q1")
        for name in ("bad\ncontrol", "\ud800", "x" * 256):
            with self.subTest(name=name):
                self.graph.routes["/me/drives"] = {"value": [dict(DRIVE, name=name)]}
                self.code("GRAPH_INVALID_RESPONSE", self.discovery.list_drives)

    def test_folder_listing_is_one_level_and_excludes_files_packages_shortcuts(self):
        self.graph.routes["/drives/drive-1/items/root-1/children"] = {"value": [
            folder(), folder("shortcut", remoteItem={"id": "outside", "parentReference": {"driveId": "outside"}}),
            folder("package", package={}), {"id": "file", "name": "secret.docx", "file": {}}
        ]}
        result = self.discovery.list_folders("drive-1")
        self.assertEqual([item["folder_id"] for item in result["items"]], ["folder-1"])
        self.assertEqual(result["parent"]["folder_id"], "root-1")
        self.assertEqual(result["excluded"], {"files": 1, "shortcuts": 1, "packages": 1})
        self.assertEqual(len(self.graph.requests), 2)
        self.assertFalse(any("secret.docx" in url for url, _, _ in self.graph.requests))

    def test_folder_listing_rejects_cross_drive_parent_or_identity_change(self):
        self.graph.routes["/drives/drive-1/items/root-1/children"] = {"value": [folder(drive="outside-drive")]}
        self.code("GRAPH_SCOPE_CHANGED", self.discovery.list_folders, "drive-1")
        self.graph.routes["/drives/drive-1/items/folder-1"] = folder(item_id="different-id")
        self.code("GRAPH_SCOPE_CHANGED", self.discovery.get_folder, "drive-1", "folder-1")

    def test_pagination_stays_in_selected_collection(self):
        self.graph.routes["/me/drives"] = {"value": [DRIVE], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=second"}
        self.graph.routes["/me/drives?page=second"] = {"value": [dict(DRIVE, id="drive-2", name="Second")]}
        result = self.discovery.list_drives()
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(auth == "Bearer SYNTHETIC_TOKEN" for _, auth, _ in self.graph.requests))

    def test_pagination_limits_and_repeated_pages_never_become_complete_inventory(self):
        link = "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=second"
        self.graph.routes["/me/drives"] = {"value": [DRIVE], "@odata.nextLink": link}
        self.graph.routes["/me/drives?page=second"] = {"value": [dict(DRIVE, id="drive-2")]}
        self.discovery.max_pages = 1
        self.code("GRAPH_DISCOVERY_LIMIT", self.discovery.list_drives)
        self.assertEqual(len(self.graph.requests), 1)
        self.discovery.max_pages, self.discovery.max_items = 2, 1
        self.code("GRAPH_DISCOVERY_LIMIT", self.discovery.list_drives)
        self.discovery.max_items = 500
        self.graph.routes["/me/drives?page=second"] = {"value": [], "@odata.nextLink": link}
        self.discovery.max_pages = 3
        self.code("GRAPH_INVALID_RESPONSE", self.discovery.list_drives)
        self.graph.routes["/me/drives?page=second"] = {"value": [DRIVE]}
        self.code("GRAPH_INVALID_RESPONSE", self.discovery.list_drives)

    def test_pagination_rejects_ssrf_cross_collection_and_expanded_data(self):
        for link in (
            "http://127.0.0.1/v1.0/me/drives?$skiptoken=x", "https://evil.invalid/v1.0/me/drives?$skiptoken=x",
            "https://graph.microsoft.com" + "@" + "evil.invalid/v1.0/me/drives?$skiptoken=x",
            "https://graph.microsoft.com/v1.0/sites?search=*", "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=x",
            "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=x&$expand=children",
            "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=x&$select=id,quota",
            "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=x&$top=10000",
            "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=x&$skiptoken=y",
            "https://graph.microsoft.com/v1.0/me/drives?$skiptoken=x#fragment",
            "https://graph.microsoft.com/v1.0/me/drives\n?$skiptoken=x",
        ):
            with self.subTest(link=link):
                self.graph.routes["/me/drives"] = {"value": [], "@odata.nextLink": link}
                count = len(self.graph.requests)
                self.code("GRAPH_UNSAFE_URL", self.discovery.list_drives)
                self.assertEqual(len(self.graph.requests), count + 1)

    def test_http_auth_permission_and_throttling_failures_are_not_empty_results(self):
        for status, code in ((401, "GRAPH_AUTH_REQUIRED"), (403, "GRAPH_ACCESS_DENIED"), (404, "GRAPH_ITEM_UNAVAILABLE"), (429, "GRAPH_THROTTLED")):
            with self.subTest(status=status):
                body = io.BytesIO(b"SECRET_REMOTE_RESPONSE")
                self.graph.routes["/me/drives"] = urllib.error.HTTPError("https://graph.microsoft.com/v1.0/me/drives", status, "SECRET", {}, body)
                error = self.code(code, self.discovery.list_drives)
                self.assertNotIn("SECRET", str(error))
                self.assertTrue(body.closed)

    def test_unknown_hosts_credentials_and_sharing_tokens_are_rejected_before_network(self):
        for link in ("https://localhost/folder", "http://tenant.sharepoint.com/sites/Team", "https://tenant.sharepoint.com.evil.invalid/a",
                     "https://tenant.sharepoint.com" + "@" + "127.0.0.1/a", "https://user" + "@" + "tenant.sharepoint.com/a",
                     "https://tenant.sharepoint.com:444/a", "https://tenant.sharepoint.com:/a",
                     "https://1drv.ms/f/synthetic", "https://tenant.sharepoint.com/:f:/s/Team/opaque?e=abc",
                     "https://onedrive.live.com/?id=A!1&authkey=SECRET", "https://tenant.sharepoint.com/a?tempauth=SECRET",
                     "https://tenant.sharepoint.com/a#fragment", "https://tenant.sharepoint.com/a?ID=a&id=b"):
            with self.subTest(link=link):
                error = self.code("GRAPH_UNSUPPORTED_LINK", self.discovery.resolve_folder, link)
                self.assertIn("Choose a folder", error.next_action)
        self.assertEqual(self.graph.requests, [])

    def test_direct_link_uses_graph_metadata_only_and_retains_provider_schema(self):
        selection = self.discovery.resolve_folder(LIBRARY + "/Approved")
        self.assertEqual(selection, {"provider": "microsoft_graph", "drive_id": "drive-1", "folder_id": "folder-1", "name": "Approved", "web_url": LIBRARY + "/Approved"})
        self.assertEqual(len(self.graph.requests), 3)
        self.assertTrue(all(url.startswith("https://graph.microsoft.com/v1.0/") for url, _, _ in self.graph.requests))

    def test_known_drive_and_known_site_avoid_extra_site_lookup(self):
        self.discovery.resolve_folder(LIBRARY + "/Approved", drive_id="drive-1")
        self.assertEqual(len(self.graph.requests), 3)
        self.assertFalse(any("/sites/" in urllib.parse.urlsplit(url).path for url, _, _ in self.graph.requests))
        self.graph.requests.clear()
        self.discovery.resolve_folder(LIBRARY + "/Approved", site_id=SITE_ID)
        self.assertEqual(len(self.graph.requests), 3)
        self.assertIn("/drives?", self.graph.requests[0][0])
        self.code("INVALID_CONFIG", self.discovery.resolve_folder, LIBRARY, drive_id="drive-1", site_id=SITE_ID)

    def test_direct_team_url_falls_back_to_only_the_specified_site(self):
        self.graph.routes["/me/drives"] = {"value": []}
        result = self.discovery.resolve_folder(LIBRARY + "/Approved")
        self.assertEqual(result["folder_id"], "folder-1")
        self.assertEqual(len(self.graph.requests), 5)
        site_routes = [url for url, _, _ in self.graph.requests if "/sites/" in urllib.parse.urlsplit(url).path]
        self.assertEqual(len(site_routes), 2)

    def test_site_resolution_permission_failure_keeps_scope_unchanged(self):
        self.graph.routes["/me/drives"] = {"value": []}
        self.graph.routes["/sites/" + HOST + ":/sites/Team"] = urllib.error.HTTPError("https://graph.microsoft.com/v1.0/sites/x", 403, "private", {}, io.BytesIO())
        self.code("GRAPH_ACCESS_DENIED", self.discovery.resolve_folder, LIBRARY + "/Approved")
        self.assertEqual(len(self.graph.requests), 2)

    def test_missing_personal_drive_does_not_prevent_explicit_site_resolution(self):
        self.graph.routes["/me/drives"] = urllib.error.HTTPError("https://graph.microsoft.com/v1.0/me/drives", 404, "not provisioned", {}, io.BytesIO())
        result = self.discovery.resolve_folder(LIBRARY + "/Approved")
        self.assertEqual(result["folder_id"], "folder-1")
        self.assertEqual(len(self.graph.requests), 5)

    def test_browser_query_path_and_direct_r_link_decode_once(self):
        target = "/sites/Team/Shared Documents/Approved"
        for link in (LIBRARY + "/Forms/AllItems.aspx?id=" + urllib.parse.quote(target, safe=""),
                     LIBRARY + "/Forms/AllItems.aspx?RootFolder=" + urllib.parse.quote(target, safe=""),
                     "https://" + HOST + "/:f:/r/sites/Team/Shared%20Documents/Approved?csf=1&web=1&e=ignored"):
            with self.subTest(link=link):
                self.assertEqual(self.discovery.resolve_folder(link)["folder_id"], "folder-1")

    def test_unicode_spaces_percent_hash_and_plus_are_encoded_per_segment(self):
        name = "团队 100% + #"
        self.graph.routes["/drives/drive-1/root:/" + name] = folder(name=name)
        result = self.discovery.resolve_folder(LIBRARY + "/" + urllib.parse.quote(name, safe=""))
        self.assertEqual(result["name"], name)
        path = urllib.parse.urlsplit(self.graph.requests[-1][0]).path
        self.assertTrue(path.endswith(urllib.parse.quote(name, safe="")))
        self.assertNotIn(" ", path)

    def test_ambiguous_or_traversing_path_encoding_is_rejected_before_network(self):
        for suffix in ("/..", "/%2e%2e", "/a%2Fb", "/a%5Cb", "/%00bad", "/%GG", "/%FF", "//", "/a//b"):
            with self.subTest(suffix=suffix), self.assertRaises(DriveError):
                self.discovery.resolve_folder(LIBRARY + suffix)
        self.assertEqual(self.graph.requests, [])

    def test_shortcut_ancestors_are_never_traversed(self):
        self.graph.routes["/drives/drive-1/root:/Approved"] = folder(remoteItem={"id": "remote"})
        self.code("GRAPH_UNSUPPORTED_FOLDER", self.discovery.resolve_folder, LIBRARY + "/Approved/Secret")
        self.assertFalse(any("Secret" in url for url, _, _ in self.graph.requests))
        self.graph.routes["/drives/drive-1/items/folder-1"] = folder(remoteItem={"id": "remote"})
        self.code("GRAPH_UNSUPPORTED_FOLDER", self.discovery.get_folder, "drive-1", "folder-1")

    def test_nested_link_checks_each_parent_and_name(self):
        self.graph.routes["/drives/drive-1/root:/Approved/Child"] = folder("child", "Child", parent="folder-1")
        self.assertEqual(self.discovery.resolve_folder(LIBRARY + "/Approved/Child")["folder_id"], "child")
        self.graph.routes["/drives/drive-1/root:/Approved/Child"] = folder("child", "Child", parent="outside")
        self.code("GRAPH_SCOPE_CHANGED", self.discovery.resolve_folder, LIBRARY + "/Approved/Child")
        self.graph.routes["/drives/drive-1/root:/Approved/Child"] = folder("child", "Different", parent="folder-1")
        self.code("GRAPH_SCOPE_CHANGED", self.discovery.resolve_folder, LIBRARY + "/Approved/Child")

    def test_path_names_are_case_insensitive_but_resource_ids_are_exact(self):
        self.graph.routes["/drives/drive-1/root:/approved"] = folder()
        result = self.discovery.resolve_folder(LIBRARY.lower() + "/approved")
        self.assertEqual(result["name"], "Approved")
        self.assertEqual(result["folder_id"], "folder-1")
        self.graph.routes["/drives/drive-1/items/folder-1"] = folder(item_id="FOLDER-1")
        self.code("GRAPH_SCOPE_CHANGED", self.discovery.get_folder, "drive-1", "folder-1")

    def test_onedrive_personal_direct_ids_are_verified_by_graph(self):
        cid, item_id = "ABCDEF1234567890", "ABCDEF1234567890!123"
        self.graph.routes["/drives/" + cid + "/items/" + item_id] = folder(item_id, drive=cid, webUrl="https://onedrive.live.com/?cid=" + cid + "&id=" + item_id)
        result = self.discovery.resolve_folder("https://onedrive.live.com/?cid=" + cid + "&id=" + urllib.parse.quote(item_id))
        self.assertEqual(result["drive_id"], cid)
        self.assertEqual(result["folder_id"], item_id)
        self.assertEqual(len(self.graph.requests), 1)
        self.code("GRAPH_UNSUPPORTED_LINK", self.discovery.resolve_folder, "https://onedrive.live.com/?cid=" + cid + "&id=DIFFERENT!1")

    def test_response_metadata_cannot_inject_browser_links_or_escape_site(self):
        self.graph.routes["/me/drives"] = {"value": [dict(DRIVE, webUrl="https://evil.invalid/link")]}
        self.code("GRAPH_UNSUPPORTED_LINK", self.discovery.list_drives)
        self.graph.routes["/sites/" + HOST + ":/sites/Team"]["webUrl"] = "https://other.sharepoint.com/sites/Team"
        self.code("GRAPH_SCOPE_CHANGED", self.discovery.resolve_site, "https://" + HOST + "/sites/Team")

    def test_timeout_and_json_depth_are_bounded(self):
        self.graph.routes["/me/drives"] = lambda *_: Response(b'{"value":[],"extra":' + b'[' * 65 + b'0' + b']' * 65 + b'}')
        self.code("GRAPH_INVALID_RESPONSE", self.discovery.list_drives)
        with mock.patch("time.monotonic", side_effect=[0, 100]):
            self.code("GRAPH_TIMEOUT", self.discovery.list_drives)


if __name__ == "__main__":
    unittest.main()
