"""Synthetic Graph responses verify scope, provenance and failure boundaries."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from team_drive.errors import DriveError
from team_drive.graph_source import GraphSource, _timestamp


class Token:
    def token(self):
        return 'synthetic-access-token'


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {'Content-Length': str(len(body))}
        self.headers.update(headers or {})
        self.closed = False

    def read(self, size=-1):
        return self.body.read(size)

    read1 = read

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def item(item_id, name, parent='folder-1', folder=False, content=b'content', etag='v1'):
    value = {'id': item_id, 'name': name, 'parentReference': {'driveId': 'drive-1', 'id': parent},
             'eTag': etag, 'size': len(content), 'lastModifiedDateTime': '2020-01-01T00:00:00Z',
             'webUrl': 'https://tenant.sharepoint.com/sites/docs/' + urllib.parse.quote(name)}
    value['folder' if folder else 'file'] = {}
    return value


class FakeGraph:
    anchor = '/v1.0/drives/drive-1/items/folder-1'

    def __init__(self):
        self.root = item('folder-1', 'Approved', parent='outside-parent', folder=True)
        self.entries = {}
        self.children = {'folder-1': []}
        self.content = {}
        self.requests = []
        self.overrides = {}

    def add(self, path, value, content=b'content'):
        self.entries[path] = value
        self.children.setdefault(value['parentReference']['id'], []).append(value)
        if 'folder' in value:
            self.children.setdefault(value['id'], [])
        else:
            self.content[path] = content

    def open(self, request, timeout):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parsed.path)
        key = path + ('?' + parsed.query if '$skiptoken=' in parsed.query else '')
        self.requests.append((url, request.get_header('Authorization'), timeout))
        override = self.overrides.get(key)
        if override is not None:
            if callable(override):
                return override(request, timeout)
            if isinstance(override, Exception):
                raise override
            return copy.deepcopy(override)
        if parsed.netloc != 'graph.microsoft.com':
            raise AssertionError('Unexpected external request')
        if path == self.anchor:
            result = self.root
        elif path.startswith(self.anchor + ':/'):
            relative = path[len(self.anchor) + 2:]
            if relative.endswith(':/content'):
                relative = relative[:-9]
                return Response(self.content[relative])
            if relative not in self.entries:
                raise urllib.error.HTTPError(url, 404, 'private error text', {}, io.BytesIO(b'PRIVATE_HTTP_BODY'))
            result = self.entries[relative]
        elif path.endswith('/children'):
            folder_id = path.split('/')[-2]
            result = {'value': self.children[folder_id]}
        else:
            raise AssertionError('Unexpected route: ' + path)
        return Response(json.dumps(result).encode())


class GraphSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='df-graph-source-')
        self.root = Path(self.temp.name) / 'anchor'
        self.root.mkdir()
        self.source = GraphSource(self.root, {'drive_id': 'drive-1', 'folder_id': 'folder-1'}, 1024, Token())
        self.graph = FakeGraph()
        self.source._opener = self.graph

    def tearDown(self):
        self.source.close()
        self.temp.cleanup()

    def assert_code(self, expected, function, *args, **kwargs):
        with self.assertRaises(DriveError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, expected)
        return context.exception

    def test_initialization_is_local_and_cloud_anchor_remains_empty(self):
        self.assertEqual(self.graph.requests, [])
        self.source.check_local_root()
        self.assertEqual(self.graph.requests, [])
        (self.root / 'unexpected.txt').write_text('must not be indexed')
        self.assert_code('GRAPH_ANCHOR_NOT_EMPTY', self.source.check_root)
        self.assertEqual(self.graph.requests, [])

    def test_complete_recursive_inventory_stays_inside_approved_folder(self):
        self.graph.add('finance', item('folder-2', 'finance', folder=True))
        self.graph.add('finance/a.txt', item('file-1', 'a.txt', parent='folder-2'))
        errors = []
        result = list(self.source.walk(errors))
        self.assertEqual(errors, [])
        self.assertEqual([row[0] for row in result], ['finance/a.txt'])
        self.assertEqual(result[0][1].st_mtime_ns, 1577836800000000000)
        self.assertEqual(result[0][2:], (False, False))
        origin = self.source.evidence('finance/a.txt')
        self.assertEqual(origin['item_id'], 'file-1')
        self.assertEqual(origin['folder_id'], 'folder-1')
        self.assertFalse(any('downloadUrl' in key for key in origin))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_graph_seven_digit_timestamp_survives_scan_and_digest_on_python39(self):
        value = item('file-1', 'a.txt')
        value['lastModifiedDateTime'] = '2020-01-01T08:00:00.1234567+08:00'
        self.graph.add('a.txt', value)
        rows = list(self.source.walk([]))
        expected = 1577836800123456700
        self.assertEqual(rows[0][1].st_mtime_ns, expected)
        _, info = self.source.digest('a.txt')
        self.assertEqual(info.st_mtime_ns, expected)
        self.assertEqual(self.source.evidence('a.txt')['remote_modified_at'], value['lastModifiedDateTime'])

    def test_timestamp_precision_timezone_and_invalid_values(self):
        for value, expected in (
            ('1970-01-01T00:00:00Z', 0),
            ('1970-01-01T00:00:00.1Z', 100000000),
            ('1970-01-01T00:00:00.123456789Z', 123456789),
            ('1969-12-31T23:59:59.9999999Z', -100),
        ):
            with self.subTest(value=value):
                self.assertEqual(_timestamp(value), expected)
        for value in ('2020-01-01T00:00:00.1234567890Z', '2020-01-01T00:00:00',
                      '2020-02-30T00:00:00.1234567Z', '2020-01-01T00:00:00.1234567+25:00'):
            with self.subTest(value=value):
                self.assert_code('GRAPH_INVALID_RESPONSE', _timestamp, value)

    def test_pagination_accepts_only_same_collection_and_discards_partial_manifest(self):
        a = item('file-1', 'a.txt')
        b = item('file-2', 'b.txt')
        self.graph.add('a.txt', a)
        list(self.source.walk([]))
        old = self.source.evidence('a.txt')
        collection = '/v1.0/drives/drive-1/items/folder-1/children'
        next_url = 'https://graph.microsoft.com' + collection + '?$skiptoken=page2'
        self.graph.overrides[collection] = Response(json.dumps({'value': [b], '@odata.nextLink': next_url}).encode())
        self.graph.overrides[collection + '?$skiptoken=page2'] = urllib.error.HTTPError(next_url, 429, 'PRIVATE', {}, io.BytesIO(b'PRIVATE_HTTP_BODY'))
        errors = []
        self.assert_code('GRAPH_THROTTLED', lambda: list(self.source.walk(errors)))
        self.assertTrue(errors)
        self.assertEqual(self.source.evidence('a.txt'), old)
        self.assertEqual(self.source.evidence('b.txt'), {})
        self.graph.overrides[collection + '?$skiptoken=page2'] = Response(json.dumps({'value': [a]}).encode())
        self.assertEqual({row[0] for row in self.source.walk([])}, {'a.txt', 'b.txt'})
        for unsafe in ('https://evil.example/v1.0/drives/drive-1/items/folder-1/children',
                       'https://graph.microsoft.com/v1.0/drives/drive-1/root/children',
                       'https://graph.microsoft.com' + collection + '?$expand=children', {'url': next_url}):
            self.graph.overrides[collection] = Response(json.dumps({'value': [], '@odata.nextLink': unsafe}).encode())
            self.assert_code('GRAPH_UNSAFE_URL', lambda: list(self.source.walk([])))

    def test_direct_content_hash_binds_remote_version_and_preserves_local_anchor(self):
        content = b'synthetic evidence'
        self.graph.add('a.txt', item('file-1', 'a.txt', content=content), content)
        snapshot = io.BytesIO()
        version, info = self.source.digest('a.txt', snapshot)
        self.assertEqual(snapshot.getvalue(), content)
        self.assertEqual(info.st_size, len(content))
        origin = self.source.evidence('a.txt')
        self.assertEqual(origin['content_sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(origin['source_version'], version)
        self.assertNotEqual(version, origin['content_sha256'])
        self.graph.entries['a.txt']['eTag'] = 'metadata-changed-same-content'
        updated, _ = self.source.digest('a.txt')
        self.assertNotEqual(version, updated)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_signed_download_redirect_never_receives_graph_authorization(self):
        self.graph.add('a.txt', item('file-1', 'a.txt'))
        signed = 'https://tenant.sharepoint.com/download?tempauth=synthetic-secret'
        self.graph.overrides[FakeGraph.anchor + ':/a.txt:/content'] = Response(status=302, headers={'Location': signed})
        self.graph.overrides['/download'] = Response(b'content')
        self.source.digest('a.txt')
        external = [request for request in self.graph.requests if request[0] == signed]
        self.assertEqual(len(external), 1)
        self.assertIsNone(external[0][1])
        self.assertNotIn('synthetic-secret', json.dumps(self.source.evidence('a.txt')))
        for unsafe in ('http://tenant.sharepoint.com/download', 'https://127.0.0.1/private',
                       'https://tenant.sharepoint.com.evil.example/x', 'https://evil.example/x',
                       'https://user:password' + '@tenant.sharepoint.com/x'):
            self.graph.overrides[FakeGraph.anchor + ':/a.txt:/content'] = Response(status=302, headers={'Location': unsafe})
            self.assert_code('GRAPH_UNSAFE_URL', self.source.digest, 'a.txt')

    def test_permission_and_missing_paths_fail_closed_with_no_http_body_leak(self):
        self.graph.add('a.txt', item('file-1', 'a.txt'))
        self.source.digest('a.txt')
        for status, code in ((401, 'GRAPH_AUTH_REQUIRED'), (403, 'GRAPH_ACCESS_DENIED'), (404, 'GRAPH_ITEM_UNAVAILABLE'), (503, 'GRAPH_UNAVAILABLE')):
            self.graph.overrides[FakeGraph.anchor] = urllib.error.HTTPError('https://graph.microsoft.com/private', status, 'PRIVATE_HTTP_BODY', {}, io.BytesIO(b'PRIVATE_HTTP_BODY'))
            error = self.assert_code(code, self.source.digest, 'a.txt')
            self.assertNotIn('PRIVATE', str(error))
        self.graph.overrides.clear()
        del self.graph.entries['a.txt']
        request_count = len(self.graph.requests)
        self.assert_code('GRAPH_ITEM_UNAVAILABLE', self.source.digest, 'a.txt')
        self.assertFalse(any(request[0].endswith(':/content') for request in self.graph.requests[request_count:]))

    def test_version_change_during_download_never_publishes_provenance(self):
        self.graph.add('a.txt', item('file-1', 'a.txt'))
        def replace(_request, _timeout):
            self.graph.entries['a.txt']['eTag'] = 'v2'
            return Response(b'content')
        self.graph.overrides[FakeGraph.anchor + ':/a.txt:/content'] = replace
        self.assert_code('GRAPH_SOURCE_CHANGED', self.source.digest, 'a.txt')
        self.assertEqual(self.source.evidence('a.txt'), {})

    def test_shortcuts_are_blocked_and_ancestor_shortcuts_cannot_escape_scope(self):
        shortcut = item('shortcut-1', 'shared', folder=True)
        shortcut['remoteItem'] = {'id': 'outside'}
        self.graph.add('shared', shortcut)
        rows = list(self.source.walk([]))
        self.assertTrue(rows[0][2])
        self.graph.add('shared/a.txt', item('file-1', 'a.txt', parent='shortcut-1'))
        self.assert_code('GRAPH_SCOPE_CHANGED', self.source.digest, 'shared/a.txt')
        self.assertFalse(any(request[0].endswith(':/content') for request in self.graph.requests))

    def test_invalid_names_parent_and_scope_changes_are_not_empty_scans(self):
        for value in (item('file-1', '../bad'), item('file-1', 'CON.txt'), item('file-1', 'bad.txt', parent='outside')):
            self.graph.children['folder-1'] = [value]
            errors = []
            with self.assertRaises(DriveError):
                list(self.source.walk(errors))
            self.assertTrue(errors)
            self.assertEqual(self.source.evidence('bad.txt'), {})
        self.graph.root['id'] = 'replacement-root'
        self.assert_code('GRAPH_SCOPE_CHANGED', self.source.check_root)

    def test_limits_and_size_mismatch_fail_without_truncation(self):
        large = item('file-1', 'large.txt', content=b'x' * 1025)
        self.graph.add('large.txt', large, b'x' * 1025)
        self.assert_code('FILE_TOO_LARGE', self.source.digest, 'large.txt')
        self.graph.add('short.txt', item('file-2', 'short.txt', content=b'longer'), b'bad')
        self.assert_code('GRAPH_SOURCE_CHANGED', self.source.digest, 'short.txt')
        self.source.limits['max_items'] = 1
        self.assert_code('GRAPH_SCAN_LIMIT', lambda: list(self.source.walk([])))

    def test_shared_digest_deadline_bounds_metadata_and_download_together(self):
        self.graph.add('a.txt', item('file-1', 'a.txt'))
        clock = [0.0]
        real_open = self.graph.open
        def advance(request, timeout):
            clock[0] += 9
            return real_open(request, timeout)
        with mock.patch('team_drive.graph_source.time.monotonic', side_effect=lambda: clock[0]):
            with mock.patch.object(self.graph, 'open', side_effect=advance):
                self.assert_code('GRAPH_TIMEOUT', self.source.digest, 'a.txt')
        self.assertEqual(self.source.evidence('a.txt'), {})
        self.assertTrue(all(timeout <= 10 for _, _, timeout in self.graph.requests))

    def test_configuration_does_not_accept_paths_aliases_or_unbounded_limits(self):
        for graph in ({'drive_id': 'drive-1', 'folder_id': 'root'},
                      {'drive_id': 'https://graph.microsoft.com', 'folder_id': 'folder-1'},
                      {'drive_id': 'drive-1', 'folder_id': 'folder-1', 'timeout_seconds': 60},
                      {'drive_id': 'drive-1', 'folder_id': 'folder-1', 'max_items': True}):
            self.assert_code('INVALID_CONFIG', GraphSource, self.root, graph, 1024, Token())
        self.assert_code('GRAPH_AUTH_REQUIRED', GraphSource, self.root,
                         {'drive_id': 'drive-1', 'folder_id': 'folder-1'}, 1024)

    def test_cached_origin_is_reauthorized_without_downloading_and_rejects_revocation(self):
        self.graph.add('a.txt', item('file-1', 'a.txt'))
        self.source.digest('a.txt')
        origin = self.source.evidence('a.txt')
        count = len(self.graph.requests)
        verified = self.source.verify_origin('a.txt', origin)
        self.assertEqual(verified['item_id'], 'file-1')
        self.assertFalse(any(request[0].endswith(':/content') for request in self.graph.requests[count:]))
        metadata_path = FakeGraph.anchor + ':/a.txt'
        self.graph.overrides[metadata_path] = urllib.error.HTTPError('https://graph.microsoft.com/secret', 403, 'PRIVATE', {}, io.BytesIO(b'PRIVATE'))
        self.assert_code('GRAPH_ACCESS_DENIED', self.source.verify_origin, 'a.txt', origin)
        self.graph.overrides.clear()
        self.graph.entries['a.txt']['eTag'] = 'v2'
        self.assert_code('GRAPH_SOURCE_CHANGED', self.source.verify_origin, 'a.txt', origin)
        self.graph.entries['a.txt']['eTag'] = 'v1'
        self.graph.entries['a.txt']['id'] = 'replacement-id'
        self.assert_code('GRAPH_SOURCE_CHANGED', self.source.verify_origin, 'a.txt', origin)
        self.assert_code('GRAPH_SOURCE_CHANGED', self.source.verify_origin, 'a.txt', dict(origin, folder_id='outside'))

    def test_blocked_shortcut_metadata_can_be_explained_without_following_it(self):
        shortcut = item('shortcut-1', 'shared', folder=True)
        shortcut['remoteItem'] = {'id': 'outside'}
        self.graph.add('shared', shortcut)
        list(self.source.walk([]))
        origin = self.source.evidence('shared')
        self.assertEqual(self.source.verify_origin('shared', origin)['item_id'], 'shortcut-1')
        self.assertFalse(any('outside' in request[0] for request in self.graph.requests))


if __name__ == '__main__':
    unittest.main()
