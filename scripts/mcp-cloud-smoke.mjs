// Synthetic Graph only: real SDK stdio -> bridge -> broker -> Engine -> parser/storage.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = process.env.DF_MCP_PYTHON || process.env.TEAM_DRIVE_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const temp = mkdtempSync(join(tmpdir(), 'df-mcp-cloud-smoke-'));
const hooks = join(temp, 'python-hooks');
const control = join(temp, 'cloud-control.json');
const clients = new Set();
const states = [];
const stderr = [];
const marker = 'PRIVATE_CLOUD_FIXTURE_4927';
const csv = `project,currency,amount\n${marker},CNY,123.45\nsecond,USD,67.89\n`;
const delay = ms => new Promise(resolveDelay => setTimeout(resolveDelay, ms));
mkdirSync(hooks);
writeFileSync(control, '{}');

// This hook exists only in the disposable test interpreter environment. All real
// urllib HTTP is disabled, including OAuth, even if a fixture route is missing.
writeFileSync(join(hooks, 'sitecustomize.py'), [
  'import io, json, os, pathlib, urllib.error, urllib.parse, urllib.request',
  'from team_drive.graph_source import GraphSource',
  'from test_graph_source import FakeGraph, item',
  'def no_real_http(*args, **kwargs):',
  '    raise AssertionError("Real HTTP is forbidden in the cloud MCP smoke test")',
  'urllib.request.OpenerDirector.open = no_real_http',
  'class ControlledGraph(FakeGraph):',
  '    def __init__(self):',
  '        super().__init__()',
  `        content = ${JSON.stringify(csv)}.encode("utf-8")`,
  '        self.add("budget.csv", item("budget-item", "budget.csv", content=content, etag="fixture-etag-v1"), content)',
  '    def open(self, request, timeout):',
  '        mode = json.loads(pathlib.Path(os.environ["DF_MCP_CLOUD_FIXTURE_CONTROL"]).read_text(encoding="utf-8")).get("mode")',
  '        path = urllib.parse.unquote(urllib.parse.urlsplit(request.full_url).path)',
  '        code = 503 if mode == "offline" else 403 if mode == "file-denied" and path.startswith(self.anchor + ":/budget.csv") else None',
  '        if code is not None:',
  '            raise urllib.error.HTTPError(request.full_url, code, "synthetic failure", {}, io.BytesIO(b"PRIVATE_HTTP_BODY"))',
  '        return super().open(request, timeout)',
  '_original_graph_init = GraphSource.__init__',
  'def fixture_graph_init(self, *args, **kwargs):',
  '    _original_graph_init(self, *args, **kwargs)',
  '    self._opener = ControlledGraph()',
  'GraphSource.__init__ = fixture_graph_init',
  ''
].join('\n'));

function profile(label) {
  const anchor = join(temp, `${label}-anchor`);
  const state = join(temp, `${label}-state`);
  const config = join(temp, `${label}.json`);
  mkdirSync(anchor);
  states.push(state);
  writeFileSync(config, JSON.stringify({
    provider: 'microsoft_graph', root: anchor, state_dir: state, poll_seconds: 3600,
    service: { idle_seconds: 1 },
    graph: { tenant_id: '11111111-1111-1111-1111-111111111111', client_id: '22222222-2222-2222-2222-222222222222',
      drive_id: 'drive-1', folder_id: 'folder-1', token_env: 'DF_MCP_CLOUD_SMOKE_TOKEN' }
  }));
  return { anchor, state, config };
}

async function connect(settings, authenticated = true) {
  const client = new Client({ name: 'df-mcp-cloud-smoke', version: '1.0.0' });
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [join(root, 'dist/index.js'), '--config', settings.config, '--python', python],
    env: { PYTHONPATH: [hooks, join(root, 'python'), join(root, 'tests')].join(delimiter),
      DF_MCP_CLOUD_FIXTURE_CONTROL: control,
      DF_MCP_CLOUD_SMOKE_TOKEN: authenticated ? 'synthetic-cloud-token-no-real-account' : '' },
    stderr: 'pipe'
  });
  transport.stderr.on('data', data => stderr.push(data.toString()));
  clients.add(client);
  await client.connect(transport);
  return client;
}

async function call(client, method, args = {}, expected = true) {
  const wire = await client.callTool({ name: `team_drive_${method}`, arguments: args });
  const result = wire.structuredContent;
  assert.equal(typeof result?.ok, 'boolean', JSON.stringify(wire));
  assert.equal(result.ok, expected, JSON.stringify(result));
  assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
  return result;
}

async function ready(client) {
  for (let attempt = 0; attempt < 300; attempt++) {
    const status = await call(client, 'status');
    if (status.coverage.inventory_complete && status.coverage.all_present_files_indexed) return status;
    await delay(100);
  }
  throw new Error('Synthetic cloud inventory did not become ready.');
}

async function finishedJob(client, jobId) {
  const terminal = new Set(['complete', 'failed', 'cancelled', 'interrupted']);
  let lastStatus;
  for (let attempt = 0; attempt < 300; attempt++) {
    const result = await call(client, 'job_status', { job_id: jobId });
    lastStatus = result.job.status;
    if (terminal.has(lastStatus)) return result;
    assert.ok(['queued', 'running'].includes(lastStatus), `Unexpected job status: ${lastStatus}`);
    await delay(100);
  }
  throw new Error(`Synthetic cloud job did not finish; last status: ${lastStatus}`);
}

async function stopped() {
  for (let attempt = 0; attempt < 150; attempt++) {
    if (states.every(state => !existsSync(join(state, 'shared-v1/control/endpoint.json')))) return;
    await delay(100);
  }
  throw new Error('Synthetic cloud broker did not stop after its clients closed.');
}

try {
  const configured = profile('authorized');
  const client = await connect(configured);
  const tools = await client.listTools();
  assert.equal(tools.tools.length, 18);
  assert.equal(client.getServerVersion().version, '0.7.0');
  const status = await ready(client);
  assert.equal(status.service_version, '0.7.0');
  assert.equal(status.provider, 'microsoft_graph');
  assert.equal(status.coverage.present_files, 1);
  assert.equal(status.capabilities.http_mcp, false);
  assert.equal(status.capabilities.ocr, false);
  assert.equal(status.state_storage.mode, 'shared');

  const listed = await call(client, 'list');
  assert.equal(listed.cloud_access_checked, true);
  assert.equal(listed.files[0].path, 'budget.csv');
  const search = await call(client, 'search', { query: marker });
  assert.equal(search.files.length, 1);
  assert.equal(search.files[0].match.snippet_is_evidence, false);
  const file = search.files[0];
  const inspected = await call(client, 'inspect', { file_id: file.file_id });
  const version = inspected.source_version;
  const read = await call(client, 'read', { file_id: file.file_id, expected_version: version });
  assert.equal(read.segments.length, 3);
  assert.equal(read.segments[1].data.cells[0].value, marker);
  assert.equal(read.segments[1].locator.row, 2);
  assert.equal(read.origin.provider, 'microsoft_graph');
  assert.equal(read.origin.item_id, 'budget-item');
  assert.equal(read.origin.etag, 'fixture-etag-v1');
  assert.equal(read.origin.web_url, 'https://tenant.sharepoint.com/sites/docs/budget.csv');
  assert.equal(read.origin.content_sha256, createHash('sha256').update(csv).digest('hex'));
  assert.equal(read.origin.source_version, version);
  assert.notEqual(version, read.origin.content_sha256, 'Cloud source versions bind remote identity and tags, not only bytes.');
  await call(client, 'wiki_write', { page_id: 'cloud-budget', kind: 'source', title: 'Synthetic cloud budget',
    body: 'This draft refers to the selected synthetic cloud budget row. [1]', citations: [{
      file_id: file.file_id, source_version: version, ordinal: read.segments[1].ordinal,
      locator: read.segments[1].locator, quote: marker }] });
  const cloudWiki = await call(client, 'wiki_read', { page_id: 'cloud-budget' });
  assert.equal(cloudWiki.page.citations[0].source_version, version);
  const context = await call(client, 'context', { file_id: file.file_id, expected_version: version, ordinal: 1, before: 0, after: 0 });
  assert.deepEqual(context.segments.map(segment => segment.ordinal), [0, 1]);
  assert.equal(context.coverage.whole_document, false);
  assert.equal(context.origin.etag, read.origin.etag);
  const plan = await call(client, 'plan', { query: '全部预算金额合计是多少？' });
  assert.equal(plan.classification, 'exhaustive');
  assert.equal(plan.advisory, true);
  const exported = await call(client, 'export', { file_id: file.file_id, expected_version: version });
  const rows = readFileSync(exported.artifact_path, 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(rows.length, 3);
  assert.ok(rows.every(row => row.source_version === version && row.origin.etag === 'fixture-etag-v1'));
  const bundle = await call(client, 'evidence_bundle', { files: [{ file_id: file.file_id, expected_version: version }], scope_description: 'Synthetic cloud fixture only' });
  assert.equal(bundle.all_selected_versions_exported, true);
  const manifest = JSON.parse(readFileSync(bundle.artifact_path, 'utf8'));
  assert.equal(manifest.source.provider, 'microsoft_graph');
  assert.equal(manifest.files[0].origin.item_id, 'budget-item');

  const hybrid = await call(client, 'search', { query: marker, mode: 'hybrid' });
  assert.equal(hybrid.files.length, 1);
  writeFileSync(control, JSON.stringify({ mode: 'file-denied' }));
  const revokedWiki = await call(client, 'wiki_read', { page_id: 'cloud-budget' }, false);
  assert.equal(revokedWiki.page, undefined);
  assert.doesNotMatch(JSON.stringify(revokedWiki), /This draft refers|Synthetic cloud budget|PRIVATE_CLOUD_FIXTURE|PRIVATE_HTTP_BODY/);
  const denied = await call(client, 'search', { query: marker, mode: 'hybrid' }, false);
  assert.equal(denied.error.code, 'GRAPH_ACCESS_DENIED');
  assert.equal(denied.files, undefined, 'An old ranked snapshot must not expose revoked cloud content.');
  assert.doesNotMatch(JSON.stringify(denied), /PRIVATE_CLOUD_FIXTURE|PRIVATE_HTTP_BODY/);
  writeFileSync(control, '{}');
  const refresh = await call(client, 'refresh', { force: true });
  await ready(client);
  const job = await finishedJob(client, refresh.job_id);
  assert.equal(job.job.status, 'complete');
  writeFileSync(control, JSON.stringify({ mode: 'offline' }));
  const offline = await call(client, 'search', { query: marker }, false);
  assert.equal(offline.error.code, 'GRAPH_UNAVAILABLE');
  assert.equal(offline.files, undefined);
  writeFileSync(control, '{}');
  assert.equal((await call(client, 'status')).coverage.present_files, 1, 'An outage must not become a deletion.');
  const metrics = await call(client, 'diagnostics');
  assert.equal(metrics.content_logged, false);
  assert.doesNotMatch(JSON.stringify(metrics), /PRIVATE_CLOUD_FIXTURE|PRIVATE_HTTP_BODY|synthetic-cloud-token/);
  assert.deepEqual(readdirSync(configured.anchor), [], 'The cloud anchor is not a mirrored document directory.');

  const missing = await connect(profile('unauthorized'), false);
  const auth = await call(missing, 'status', {}, false);
  assert.equal(auth.error.code, 'AUTH_REQUIRED');
  assert.doesNotMatch(stderr.join(''), /Traceback|PRIVATE_CLOUD_FIXTURE|synthetic-cloud-token/);
  console.log(JSON.stringify({ passed: true, tools: 18, serviceVersion: '0.7.0', provider: 'microsoft_graph',
    sdkStdioBrokerParserStorage: true, scopedCloudProvenance: true, revokedCachedSearchBlocked: true, revokedWikiBlocked: true,
    offlineNotDeleted: true, authMissingExplicit: true, emptyAnchor: true,
    realMicrosoftNetwork: false, realTenantValidated: false }, null, 2));
} finally {
  await Promise.allSettled([...clients].map(client => client.close()));
  await stopped();
  rmSync(temp, { recursive: true, force: true, maxRetries: 30, retryDelay: 100 });
}
