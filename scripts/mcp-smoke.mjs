import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import { cpSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, readdirSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = process.env.DF_MCP_PYTHON || process.env.TEAM_DRIVE_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const temp = mkdtempSync(join(tmpdir(), 'team-drive-smoke-'));
const source = join(temp, 'source');
cpSync(join(root, 'examples/sample-drive'), source, { recursive: true });
const config = join(temp, 'config.json');
writeFileSync(config, JSON.stringify({ provider: 'local', root: 'source', state_dir: 'state', poll_seconds: 3600, service: { idle_seconds: 1 }, aliases: { 花费: ['支出'] } }));
const client = new Client({ name: 'team-drive-integration-test', version: '0.1.0' });
const timings = {};
function sourceSnapshot(directory) {
  const entries = {};
  function visit(folder, prefix = '') {
    for (const entry of readdirSync(folder, { withFileTypes: true })) {
      const path = join(folder, entry.name);
      const name = `${prefix}${entry.name}`;
      const metadata = statSync(path);
      entries[name] = { type: entry.isDirectory() ? 'directory' : 'file', modified: metadata.mtimeMs,
        digest: entry.isDirectory() ? null : createHash('sha256').update(readFileSync(path)).digest('hex') };
      if (entry.isDirectory()) visit(path, `${name}/`);
    }
  }
  visit(directory);
  return entries;
}
const originalSources = sourceSnapshot(source);
async function call(name, args = {}, expectOk = true) {
  const before = performance.now();
  const wire = await client.callTool({ name: `team_drive_${name}`, arguments: args });
  const result = wire.structuredContent;
  assert.equal(typeof result?.ok, 'boolean');
  assert.equal(result.ok, expectOk, JSON.stringify(result));
  assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
  timings[name] = Math.round((performance.now() - before) * 100) / 100;
  return result;
}
async function waitForIndex() {
  for (let i = 0; i < 300; i++) {
    const status = await call('status');
    if (status.coverage.inventory_complete && status.coverage.all_present_files_indexed) return status;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Index did not become ready for the fixed sample corpus.');
}
try {
  const missingConfig = spawnSync(process.execPath, [join(root, 'dist/index.js')], { encoding: 'utf8', timeout: 10_000 });
  assert.equal(missingConfig.status, 1);
  assert.match(missingConfig.stderr, /configured source is required/);
  const start = performance.now();
  await client.connect(new StdioClientTransport({ command: process.execPath, args: [join(root, 'dist/index.js'), '--config', config, '--python', python], stderr: 'pipe' }));
  const tools = await client.listTools();
  assert.equal(tools.tools.length, 18);
  assert.equal(tools.tools.find(t => t.name === 'team_drive_export').annotations.readOnlyHint, false);
  assert.equal(tools.tools.find(t => t.name === 'team_drive_wiki_write').annotations.readOnlyHint, false);
  const status = await waitForIndex();
  const coldIndexMs = Math.round(performance.now() - start);
  let list = await call('list', { limit: 2 });
  const files = [...list.files];
  while (list.has_more) { list = await call('list', { limit: 2, cursor: list.next_cursor }); files.push(...list.files); }
  assert.equal(files.length, status.coverage.present_files);
  assert.equal(new Set(files.map(f => f.file_id)).size, files.length);
  const hit = await call('search', { query: '星河' });
  assert.ok(hit.files.some(f => f.path === 'budget-approved.csv'));
  assert.equal(hit.exhaustive_for_business_totals, false);
  const chinese = await call('search', { query: '预算' });
  assert.ok(chinese.files.some(f => f.path === 'terms.md'));
  const alias = await call('search', { query: '花费' });
  assert.ok(alias.files.some(f => f.path === 'terms.md'));
  const file = files.find(f => f.path === 'budget-approved.csv');
  const inspected = await call('inspect', { file_id: file.file_id });
  const version = inspected.source_version;
  const context = await call('context', { file_id: file.file_id, expected_version: version, ordinal: 1 });
  assert.ok(context.segments.some(segment => segment.ordinal === 0));
  assert.ok(context.segments.some(segment => segment.ordinal === 1));
  assert.equal(context.coverage.whole_document, false);
  const plan = await call('plan', { query: '全部项目金额合计是多少？' });
  assert.equal(plan.classification, 'exhaustive');
  assert.equal(plan.advisory, true);
  const diagnostics = await call('diagnostics');
  assert.equal(diagnostics.content_logged, false);
  assert.ok(diagnostics.aggregates.some(item => item.operation === 'context'));
  const hybrid = await call('search', { query: '星河', mode: 'hybrid' });
  assert.equal(hybrid.retrieval.semantic.enabled, false);
  assert.ok(hybrid.files.some(item => item.path === 'budget-approved.csv'));
  let page = await call('read', { file_id: file.file_id, expected_version: version, limit: 1 });
  const segments = [...page.segments];
  while (page.has_more) { page = await call('read', { file_id: file.file_id, expected_version: version, limit: 1, cursor: page.next_cursor }); segments.push(...page.segments); }
  assert.equal(segments.length, 4);
  const selected = await call('read', { file_id: file.file_id, expected_version: version, selector: { row_start: 2, row_end: 2 } });
  assert.equal(selected.total_matching_segments, 1);
  assert.equal(selected.whole_file_in_response, false);
  assert.equal(selected.segments[0].data.cells[0].value, 'P-001');
  assert.ok(segments.every(s => s.locator && s.data.cells.length === 8));
  const citation = { file_id: file.file_id, source_version: version, ordinal: segments[1].ordinal,
    locator: segments[1].locator, quote: segments[1].text.slice(0, 100) };
  await call('wiki_write', { page_id: 'budget-summary', kind: 'source', title: 'Synthetic budget summary',
    body: 'The selected synthetic budget row is available in the source. [1]', citations: [citation] });
  const wiki = await call('wiki_read', { page_id: 'budget-summary' });
  assert.match(JSON.stringify(wiki), /Synthetic budget summary/);
  const wikiSearch = await call('wiki_search', { query: 'budget' });
  assert.match(JSON.stringify(wikiSearch), /budget-summary/);
  await call('wiki_status');
  await call('wiki_refresh');
  const cents = text => BigInt(text.replace('.', ''));
  const rows = segments.slice(1).map(s => s.data.cells.map(c => c.value));
  assert.equal(rows.reduce((sum, r) => sum + cents(r[4]) - cents(r[5]) - cents(r[6]), 0n), 6999965n);
  const exported = await call('export', { file_id: file.file_id, expected_version: version });
  assert.equal(exported.row_count, 4);
  const exportedRows = readFileSync(exported.artifact_path, 'utf8').trim().split('\n').map(JSON.parse);
  assert.ok(exportedRows.every(r => r.source_version === version));
  const bundle = await call('evidence_bundle', { files: [{ file_id: file.file_id, expected_version: version }], scope_description: 'Explicit synthetic CSV fixture' });
  assert.equal(bundle.all_selected_versions_exported, true);
  assert.equal(bundle.all_selected_text_complete, true);
  const manifest = JSON.parse(readFileSync(bundle.artifact_path, 'utf8'));
  assert.equal(manifest.business_completeness, 'not_established');
  assert.equal(manifest.files.length, 1);
  assert.equal(manifest.files[0].source_version, version);
  const tail = await call('search', { query: 'ARCHIVE-END-742' });
  const tailHit = tail.files.find(f => f.path === 'long-policy.md');
  assert.ok(tailHit);
  const tailInspect = await call('inspect', { file_id: tailHit.file_id });
  const tailRead = await call('read', { file_id: tailHit.file_id, expected_version: tailInspect.source_version, start: tailHit.match.ordinal, limit: 1 });
  assert.match(tailRead.segments[0].text, /ARCHIVE-END-742/);
  const pathRejection = await call('list', { prefix: '../outside' }, false);
  assert.equal(pathRejection.error.code, 'INVALID_PATH');
  assert.deepEqual(sourceSnapshot(source), originalSources, 'Every read/index/export operation must preserve source names, contents and modification times.');
  writeFileSync(join(source, 'budget-approved.csv'), readFileSync(join(source, 'budget-approved.csv'), 'utf8').replace('100000.00', '100001.00'));
  const staleWiki = await call('wiki_read', { page_id: 'budget-summary' }, false);
  assert.doesNotMatch(JSON.stringify(staleWiki), /The selected synthetic budget row/);
  const stale = await call('read', { file_id: file.file_id, expected_version: version }, false);
  assert.equal(stale.error.code, 'STALE_SOURCE');
  const refresh = await call('refresh');
  const job = await call('job_status', { job_id: refresh.job_id });
  assert.ok(job.job);
  for (let i = 0; i < 300; i++) {
    const current = await call('job_status', { job_id: refresh.job_id });
    if (current.job.status === 'complete') break;
    assert.ok(!['failed', 'cancelled', 'interrupted'].includes(current.job.status));
    if (i === 299) throw new Error('Refresh did not complete.');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  await waitForIndex();
  const changed = await call('inspect', { file_id: file.file_id });
  assert.notEqual(changed.source_version, version);
  await call('read', { file_id: file.file_id, expected_version: version }, false);
  const record = { passed: true, tools: tools.tools.length, sampleFiles: files.length, coldIndexMs, lastOperationMs: timings, note: 'Synthetic small-corpus integration measurement, not a production performance promise.' };
  mkdirSync(join(root, '.local'), { recursive: true });
  writeFileSync(join(root, '.local/mcp-smoke-result.json'), JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record, null, 2));
} finally {
  await client.close();
  for (let attempt = 0; attempt < 100; attempt++) {
    try { statSync(join(temp, 'state/shared-v1/control/endpoint.json')); } catch { break; }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  rmSync(temp, { recursive: true, force: true, maxRetries: 20, retryDelay: 100 });
}
