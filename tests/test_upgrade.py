"""Integrated regression checks for evidence context, diagnostics and ranking."""
import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from team_drive.engine import Engine
from team_drive.errors import DriveError
from team_drive import diagnostics


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.config = self.base / 'config.json'
        self.config.write_text(json.dumps({'root': 'source', 'state_dir': 'state',
                                         'diagnostics': {'max_events': 10}}))
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def write(self, name, text):
        p = self.source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p

    def test_context_verifies_version_and_preserves_table_evidence(self):
        source = self.write('data.csv', 'item,currency,amount\nA,CNY,10.50\nB,USD,20.25\n')
        self.engine.scan_once()
        f = self.engine.list()['files'][0]
        result = self.engine.call('context', {'file_id': f['file_id'], 'expected_version': f['source_version'], 'ordinal': 2, 'before': 0, 'after': 0})
        self.assertTrue(result['ok'], result)
        self.assertEqual([s['ordinal'] for s in result['segments']], [0, 2])
        self.assertEqual(result['segments'][1]['data']['cells'][1]['value'], 'USD')
        self.assertFalse(result['coverage']['whole_document'])
        source.write_text('item,currency,amount\nA,CNY,99.50\n')
        stale = self.engine.call('context', {'file_id': f['file_id'], 'expected_version': f['source_version'], 'ordinal': 1})
        self.assertEqual(stale['error']['code'], 'STALE_SOURCE')

    def test_context_requires_version_and_plan_keeps_exhaustive_guardrails(self):
        self.assertFalse(self.engine.call('context', {'file_id': 'id', 'ordinal': 0})['ok'])
        p = self.engine.call('plan', {'query': '所有文件中金额合计是多少？', 'prefix': 'finance'})
        self.assertEqual(p['classification'], 'exhaustive')
        self.assertTrue(p['advisory'])
        self.assertEqual(p['business_completeness'], 'not_established')
        self.assertFalse(self.engine.call('plan', {'query': 'hi', 'prefix': '../private'})['ok'])

    def test_keyword_ranking_retains_exact_name_priority(self):
        self.write('fee.txt', 'Fee clause plus several unrelated words for a longer source.\n')
        self.write('other.txt', 'fee fee fee fee\n')
        self.engine.scan_once()
        r = self.engine.search('fee')
        self.assertEqual(r['files'][0]['path'], 'fee.txt')
        self.assertEqual(r['total_matching_files'], 2)
        self.assertIn('bm25_score', r['files'][0]['match'])
        self.assertFalse(r['exhaustive_for_business_totals'])

    def test_hybrid_disabled_is_explicit_and_semantic_never_fakes_keyword(self):
        self.write('policy.txt', 'annual leave policy\n')
        self.engine.scan_once()
        hybrid = self.engine.search('leave', mode='hybrid')
        self.assertEqual(hybrid['files'][0]['path'], 'policy.txt')
        self.assertEqual(hybrid['retrieval']['semantic']['query']['state'], 'disabled')
        semantic = self.engine.search('leave', mode='semantic')
        self.assertEqual(semantic['files'], [])
        self.assertNotEqual(semantic['retrieval']['semantic']['query']['state'], 'ready')

    def test_ranked_pagination_frozen_and_session_bound(self):
        for i in range(4):
            self.write('policy%d.txt' % i, 'annual leave policy\n')
        self.engine.scan_once()
        params = {'query': 'leave', 'mode': 'hybrid', 'limit': 1}
        first = self.engine.call('search', params, session_id='a')
        self.assertTrue(first['has_more'])
        next_params = dict(params, cursor=first['next_cursor'])
        other = self.engine.call('search', next_params, session_id='b')
        self.assertEqual(other['error']['code'], 'INVALID_CURSOR')
        second = self.engine.call('search', next_params, session_id='a')
        self.assertNotEqual(first['files'][0]['path'], second['files'][0]['path'])
        self.write('new.txt', 'leave policy')
        self.engine.scan_once()
        stale = self.engine.call('search', next_params, session_id='a')
        self.assertEqual(stale['error']['code'], 'STALE_CURSOR')

    def test_same_query_reuses_snapshot_and_recreated_snapshot_invalidates_old_cursor(self):
        for i in range(3):
            self.write('entry%d.txt' % i, 'leave policy\n')
        self.engine.scan_once()
        with mock.patch.object(self.engine.retrieval, 'search', wraps=self.engine.retrieval.search) as search:
            first = self.engine.search('leave', mode='hybrid', limit=1)
            repeat = self.engine.search('leave', mode='hybrid', limit=1)
            self.assertEqual(first['files'], repeat['files'])
            self.assertEqual(search.call_count, 1)
            key = next(iter(self.engine._ranked_queries))
            old = self.engine._ranked_queries[key]
            self.engine._ranked_queries[key] = (time.monotonic() - 301,) + old[1:]
            self.engine.search('leave', mode='hybrid', limit=1)
            self.assertEqual(search.call_count, 2)
            stale = self.engine.call('search', {'query': 'leave', 'mode': 'hybrid', 'cursor': first['next_cursor']})
            self.assertEqual(stale['error']['code'], 'STALE_CURSOR')

    def test_keyword_rerank_never_broadens_all_term_match(self):
        self.write('a.txt', 'red invoice\n')
        self.write('b.txt', 'red apple\n')
        self.write('c.txt', 'blue invoice\n')
        self.write('d.txt', 'red\ninvoice\n')
        self.engine.scan_once()
        normal = {f['path'] for f in self.engine.search('red invoice')['files']}
        ranked = self.engine.search('red invoice', rerank=True)
        self.assertEqual(normal, {'a.txt', 'd.txt'})
        self.assertEqual({f['path'] for f in ranked['files']}, normal)
        self.assertEqual(ranked['retrieval']['rerank']['state'], 'disabled')

    def test_metrics_are_bounded_private_and_persist_on_close(self):
        secret = 'PRIVATE_QUERY_NEVER_LOGGED_8473'
        for i in range(18):
            self.engine.call('search', {'query': secret})
        self.engine.call('read', {'file_id': 'missing'})
        data = self.engine.call('diagnostics', {'limit': 100})
        self.assertEqual(data['stored_events'], 10)
        self.assertNotIn(secret, json.dumps(data))
        self.assertNotIn(str(self.source), json.dumps(data))
        self.assertTrue(any(e['error_code'] == 'FILE_NOT_FOUND' for e in data['events']))
        self.engine.close()
        self.engine = Engine(str(self.config), autostart=False)
        self.assertEqual(self.engine.diagnostics()['stored_events'], 10)

    def test_diagnostics_expiration_and_disabled(self):
        with self.engine._db() as db:
            diagnostics.record(db, self.engine.diagnostic_settings, 'read', 10, True)
            db.execute('UPDATE diagnostic_events SET recorded_at=?', (time.time() - 8 * 86400,))
        self.assertEqual(self.engine.diagnostics()['stored_events'], 0)
        self.engine.diagnostic_settings['enabled'] = False
        self.engine.call('status')
        self.assertEqual(self.engine.diagnostics()['stored_events'], 0)

    def test_invalid_diagnostics_fail_before_state_creation(self):
        for config in ({'enabled': 1}, {'max_events': 0}, {'retention_days': 91}, {'unknown': True}):
            with self.assertRaises(DriveError):
                diagnostics.settings(config)


class EngineModelTests(unittest.TestCase):
    """Exercise the actual Engine/HTTP-model boundary using synthetic sources."""
    def setUp(self):
        try:
            from test_retrieval import FakeModel
        except ImportError:
            from tests.test_retrieval import FakeModel
        self.model = FakeModel()
        self.temp = tempfile.TemporaryDirectory(prefix='df-engine-model-')
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.config = self.base / 'config.json'
        self.config.write_text(json.dumps({
            'root': 'source', 'state_dir': 'state',
            'retrieval': {
                'semantic': {'enabled': True, 'endpoint': self.model.url + '/embeddings',
                             'model': 'synthetic-v1', 'minimum_similarity': 0.5,
                             'timeout_seconds': 0.2, 'per_file_budget_seconds': 0.3},
                'rerank': {'enabled': True, 'endpoint': self.model.url + '/rerank',
                           'model': 'synthetic-rerank', 'timeout_seconds': 0.2}
            }
        }))
        self.engine = Engine(str(self.config), autostart=False)

    def tearDown(self):
        self.engine.close()
        self.model.close()
        self.temp.cleanup()

    def write(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_semantic_recall_reaches_original_versioned_evidence(self):
        self.write('finance/terms.txt', 'loan repayment')
        self.write('travel/guide.txt', 'holiday beach')
        self.model.vectors.update({'loan repayment': [1, 0], 'holiday beach': [0, 1],
                                   'credit obligation': [1, 0]})
        self.engine.scan_once()
        self.assertTrue(self.engine.status()['retrieval']['semantic']['coverage_complete'])
        self.assertEqual(self.engine.search('credit obligation')['files'], [])
        result = self.engine.search('credit obligation', mode='semantic')
        self.assertEqual([file['path'] for file in result['files']], ['finance/terms.txt'])
        hit = result['files'][0]
        self.assertFalse(hit['match']['snippet_is_evidence'])
        evidence = self.engine.call('context', {'file_id': hit['file_id'],
                                              'expected_version': hit['source_version'],
                                              'ordinal': hit['match']['ordinal']})
        self.assertTrue(evidence['ok'], evidence)
        self.assertEqual(evidence['segments'][0]['text'], 'loan repayment')
        self.assertEqual(evidence['segments'][0]['locator']['line_start'], 1)
        self.assertFalse(any(path == '/rerank' for path, _ in self.model.requests))

    def test_real_reranker_receives_only_all_term_eligible_sources(self):
        self.write('a.txt', 'red invoice\n')
        self.write('b.txt', 'red apple\n')
        self.write('c.txt', 'blue invoice\n')
        self.write('d.txt', 'red\ninvoice\n')
        self.engine.scan_once()
        original = self.engine.search('red invoice')
        result = self.engine.search('red invoice', rerank=True)
        self.assertEqual({file['path'] for file in result['files']},
                         {file['path'] for file in original['files']})
        self.assertEqual({file['path'] for file in result['files']}, {'a.txt', 'd.txt'})
        self.assertEqual(result['retrieval']['rerank']['state'], 'ready')
        request = next(payload for path, payload in self.model.requests if path == '/rerank')
        self.assertEqual(len(request['documents']), 2)
        self.assertFalse(any('apple' in text or 'blue' in text for text in request['documents']))
        self.assertEqual(result['retrieval']['keyword_matching_files'], 2)

    def test_changed_and_deleted_sources_invalidate_semantic_cursors_and_old_versions(self):
        for name in ('a', 'b', 'c'):
            self.write(name + '.txt', 'loan ' + name)
        self.engine.scan_once()
        params = {'query': 'credit obligation', 'mode': 'semantic', 'limit': 1}
        first = self.engine.call('search', params, session_id='same-session')
        old_hit = first['files'][0]
        self.assertTrue(first['has_more'])
        changed = self.source / old_hit['path']
        changed.write_text('holiday replacement', encoding='utf-8')
        self.model.vectors['holiday replacement'] = [0, 1]
        deleted_name = next(name for name in ('a.txt', 'b.txt', 'c.txt') if name != old_hit['path'])
        (self.source / deleted_name).unlink()
        self.engine.scan_once()
        stale = self.engine.call('search', dict(params, cursor=first['next_cursor']), session_id='same-session')
        self.assertEqual(stale['error']['code'], 'STALE_CURSOR')
        old_evidence = self.engine.call('context', {'file_id': old_hit['file_id'],
                                                  'expected_version': old_hit['source_version'], 'ordinal': 0})
        self.assertEqual(old_evidence['error']['code'], 'VERSION_MISMATCH')
        fresh = self.engine.search('credit obligation', mode='semantic')
        self.assertEqual(len(fresh['files']), 1)
        self.assertNotIn(fresh['files'][0]['path'], {old_hit['path'], deleted_name})

    def test_model_failure_preserves_keyword_and_evidence_without_private_diagnostics(self):
        marker = 'PRIVATE_SOURCE_BODY_EngineFixture'
        self.write('terms.txt', 'loan repayment ' + marker)
        self.engine.scan_once()
        self.model.fail = True
        result = self.engine.call('search', {'query': 'loan', 'mode': 'hybrid', 'rerank': True})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['files'][0]['path'], 'terms.txt')
        self.assertEqual(result['retrieval']['semantic']['query']['state'], 'failed')
        self.assertEqual(result['retrieval']['rerank']['state'], 'failed')
        hit = result['files'][0]
        evidence = self.engine.read(hit['file_id'], expected_version=hit['source_version'])
        self.assertIn(marker, evidence['segments'][0]['text'])
        diagnostic_text = json.dumps(self.engine.call('diagnostics'))
        self.assertNotIn(marker, diagnostic_text)
        self.assertNotIn(str(self.source), diagnostic_text)
        self.assertNotIn('DO_NOT_EXPOSE_PRIVATE_MODEL_RESPONSE', json.dumps(result))
        self.assertNotIn('DO_NOT_EXPOSE_PRIVATE_MODEL_RESPONSE', diagnostic_text)

    def test_model_timeout_is_bounded_and_next_scan_resumes_without_reembedding_unchanged_text(self):
        self.write('terms.txt', 'synthetic source survives timeout')
        self.model.delay = 0.6
        started = time.monotonic()
        scan = self.engine.scan_once()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(scan['job']['status'], 'complete')
        file = self.engine.list()['files'][0]
        self.assertEqual(file['status'], 'ready')
        self.assertTrue(self.engine.read(file['file_id'], expected_version=file['source_version'])['ok'])
        self.assertFalse(self.engine.status()['retrieval']['semantic']['coverage_complete'])
        self.model.delay = 0
        self.engine.scan_once()
        self.assertTrue(self.engine.status()['retrieval']['semantic']['coverage_complete'])
        requests = len(self.model.requests)
        self.engine.scan_once()
        self.assertEqual(len(self.model.requests), requests)

    def test_deep_model_json_degrades_search_and_does_not_fail_source_scan(self):
        self.write('a.txt', 'loan repayment')
        self.engine.scan_once()

        def deep_response(handler):
            handler.rfile.read(int(handler.headers['Content-Length']))
            body = b'{"data":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}'
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        with mock.patch.object(self.model.server.RequestHandlerClass, 'do_POST', deep_response):
            result = self.engine.call('search', {'query': 'loan', 'mode': 'hybrid'})
            self.assertTrue(result['ok'], result)
            self.assertEqual([item['path'] for item in result['files']], ['a.txt'])
            self.assertEqual(result['retrieval']['semantic']['query']['error_code'], 'MODEL_INVALID_RESPONSE')
            self.write('b.txt', 'second synthetic source')
            scan = self.engine.scan_once()
            self.assertEqual(scan['job']['status'], 'complete')
            item = next(item for item in self.engine.list()['files'] if item['path'] == 'b.txt')
            self.assertEqual(item['status'], 'ready')
            self.assertTrue(self.engine.read(item['file_id'], expected_version=item['source_version'])['ok'])


if __name__ == '__main__':
    unittest.main()
