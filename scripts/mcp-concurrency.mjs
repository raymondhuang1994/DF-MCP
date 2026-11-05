// Disposable fixtures only: exercise the real Node MCP transport and Python workers.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, existsSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = process.env.DF_MCP_PYTHON || process.env.TEAM_DRIVE_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const temp = mkdtempSync(join(tmpdir(), 'df-mcp-concurrency-'));
const source = join(temp, 'source');
const state = join(temp, 'state');
const codex = join(temp, '.codex');
const testHooks = join(temp, 'test-python-hooks');
mkdirSync(source);
mkdirSync(codex);
mkdirSync(testHooks);
// A temporary interpreter hook records only this test worker's own PID. It avoids
// OS-wide process enumeration and production diagnostics/test hooks, including
// on Windows where a virtualenv launcher can sit between Node and Python.
writeFileSync(join(testHooks, 'sitecustomize.py'), [
  'import os, pathlib, sys',
  'if "--config" in sys.argv and os.environ.get("DF_MCP_TEST_PID_FILE"):',
  '    try: pathlib.Path(os.environ["DF_MCP_TEST_PID_FILE"]).open("x", encoding="ascii").write(str(os.getpid()))',
  '    except FileExistsError: pass',
  ''
].join('\n'));
const fixture = join(source, 'shared-test.txt');
writeFileSync(fixture, 'SIMULATED shared evidence 同一份测试文件\nconcurrent-read-marker-718\n');
const config = join(codex, 'config.json');
writeFileSync(config, JSON.stringify({ root: source, state_dir: state, poll_seconds: 3600, service: { idle_seconds: 1, max_clients: 6 } }));
const clients = new Set();
const diagnostics = [];
const delay = milliseconds => new Promise(resolveDelay => setTimeout(resolveDelay, milliseconds));

function sourceSnapshot() {
  return readdirSync(source).map(name => {
    const path = join(source, name);
    const info = statSync(path);
    return { name, mtime: info.mtimeMs, mode: info.mode, hash: createHash('sha256').update(readFileSync(path)).digest('hex') };
  });
}
const originalSource = sourceSnapshot();

async function connect(label) {
  const client = new Client({ name: `df-mcp-concurrency-${label}`, version: '1.0.0' });
  const workerPidFile = join(testHooks, `${label}.pid`);
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [join(root, 'dist/index.js'), '--config', config, '--python', python],
    env: { PYTHONPATH: testHooks, DF_MCP_TEST_PID_FILE: workerPidFile },
    stderr: 'pipe'
  });
  transport.stderr.on('data', data => diagnostics.push(data.toString()));
  const session = { client, transport, workerPidFile };
  clients.add(session);
  await client.connect(transport);
  return session;
}

async function close(session) {
  await session.client.close();
  clients.delete(session);
}

async function call(session, method, args = {}, expectOk = true) {
  const wire = await session.client.callTool({ name: `team_drive_${method}`, arguments: args });
  const result = wire.structuredContent;
  assert.equal(typeof result?.ok, 'boolean', JSON.stringify(wire));
  if (expectOk !== null) assert.equal(result.ok, expectOk, JSON.stringify(result));
  assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
  assert.notEqual(result.error?.code, 'STATE_IN_USE', 'Concurrent transports must share one owner.');
  return result;
}

async function ready(session, allowedErrors = []) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const result = await call(session, 'status', {}, null);
    if (!result.ok) {
      assert.ok(allowedErrors.includes(result.error.code), JSON.stringify(result));
      assert.equal(result.error.retryable, true);
    } else if (result.coverage.inventory_complete && result.coverage.all_present_files_indexed) {
      assert.equal(result.coverage.present_files, 1);
      assert.equal(result.state_storage.mode, 'shared');
      assert.equal(result.state_storage.index_copies, 1);
      return result;
    }
    await delay(100);
  }
  throw new Error('Concurrent worker did not become ready within 30 seconds.');
}

async function readFixture(session) {
  const list = await call(session, 'list');
  assert.equal(list.files.length, 1);
  assert.equal(list.files[0].path, 'shared-test.txt');
  const fileId = list.files[0].file_id;
  const inspected = await call(session, 'inspect', { file_id: fileId });
  const read = await call(session, 'read', { file_id: fileId, expected_version: inspected.source_version });
  assert.match(read.segments.map(segment => segment.text).join('\n'), /concurrent-read-marker-718/);
  return { fileId, version: inspected.source_version, segments: read.segments };
}

const endpointPath = join(state, 'shared-v1/control/endpoint.json');
async function stopped() {
  const deadline = Date.now() + 15000;
  while (existsSync(endpointPath) && Date.now() < deadline) await delay(100);
  assert.equal(existsSync(endpointPath), false, 'Unused service must shut down and release its state.');
}
function assertOneIndex() {
  assert.equal(existsSync(join(state, 'pool-v1')), false);
  assert.ok(existsSync(join(state, 'shared-v1/index/index.sqlite')));
}

function persistedIndexes() {
  // Inspect only after all clients exit, avoiding an extra database reader during Windows leases.
  assert.equal(clients.size, 0);
  const code = [
    'import json, pathlib, sqlite3, sys',
    'out = {}',
    'for path in sorted(pathlib.Path(sys.argv[1]).glob("shared-v1/index/index.sqlite")):',
    '    with sqlite3.connect(path) as db:',
    '        out[path.parent.name] = db.execute("SELECT path, source_version, updated_at FROM files ORDER BY path").fetchall()',
    'print(json.dumps(out))'
  ].join('\n');
  const result = spawnSync(python, ['-c', code, state], { encoding: 'utf8', timeout: 10_000 });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

function checkCodexAtomicRename() {
  const destination = join(codex, '.codex-global-state.json');
  writeFileSync(destination, '{"revision":0}');
  for (let revision = 1; revision <= 10; revision++) {
    const replacement = join(codex, `.codex-global-state.json.${revision}.tmp`);
    writeFileSync(replacement, JSON.stringify({ revision }));
    renameSync(replacement, destination);
    assert.deepEqual(JSON.parse(readFileSync(destination, 'utf8')), { revision });
  }
}

function workerPid(session) {
  const pid = Number(readFileSync(session.workerPidFile, 'ascii'));
  assert.ok(Number.isSafeInteger(pid) && pid > 0, 'A test worker must have recorded its process ID.');
  return pid;
}

try {
  const six = await Promise.all(Array.from({length: 6}, (_, i) => connect(`parallel-${i}`)));
  const statuses = await Promise.all(six.map(session => ready(session)));
  assert.equal(new Set(statuses.map(result => result.instance_id)).size, 1);
  assert.equal(new Set(statuses.map(result => result.source_id)).size, 1);
  const evidence = await Promise.all(six.map(readFixture));
  evidence.forEach(item => assert.deepEqual(item, evidence[0]));
  checkCodexAtomicRename();
  assertOneIndex();
  const waiting = await connect('capacity');
  const exhausted = await call(waiting, 'status', {}, false);
  assert.equal(exhausted.error.code, 'SERVICE_CAPACITY');
  assert.equal(exhausted.error.retryable, true);
  const waitingPid = waiting.transport.pid;
  await close(six.shift());
  await ready(waiting, ['SERVICE_CAPACITY']);
  assert.equal(waiting.transport.pid, waitingPid);
  assert.deepEqual(await readFixture(waiting), evidence[0]);
  await Promise.all([...six, waiting].map(close));
  await stopped();
  const persisted = persistedIndexes();
  assert.equal(Object.keys(persisted).length, 1);
  let previousInstance = statuses[0].instance_id;
  for (let cycle = 0; cycle < 3; cycle++) {
    const pair = await Promise.all([connect(`restart-${cycle}-a`), connect(`restart-${cycle}-b`)]);
    const reopened = await Promise.all(pair.map(session => ready(session)));
    assert.equal(reopened[0].instance_id, reopened[1].instance_id);
    assert.notEqual(reopened[0].instance_id, previousInstance);
    previousInstance = reopened[0].instance_id;
    const reads = await Promise.all(pair.map(readFixture));
    reads.forEach(read => assert.deepEqual(read, evidence[0]));
    checkCodexAtomicRename();
    await Promise.all(pair.map(close));
    await stopped();
    assertOneIndex();
    assert.deepEqual(persistedIndexes(), persisted);
  }
  const [doomed, survivor] = await Promise.all([connect('abrupt'), connect('survivor')]);
  await Promise.all([ready(doomed), ready(survivor)]);
  const survivorPid = survivor.transport.pid;
  const crashedWorkerPid = workerPid(survivor);
  rmSync(survivor.workerPidFile);
  process.kill(crashedWorkerPid, 'SIGKILL');
  await ready(survivor, ['BACKEND_UNAVAILABLE']);
  assert.equal(survivor.transport.pid, survivorPid);
  assert.notEqual(workerPid(survivor), crashedWorkerPid);
  assert.deepEqual(await readFixture(survivor), evidence[0]);
  process.kill(doomed.transport.pid, 'SIGKILL');
  await close(doomed);
  const replacement = await connect('after-abrupt-parent-exit');
  await ready(replacement);
  assert.deepEqual(await readFixture(replacement), evidence[0]);
  const owner = JSON.parse(readFileSync(endpointPath, 'utf8'));
  process.kill(owner.pid, 'SIGKILL');
  const recovered = await Promise.all([survivor, replacement].map(session => ready(session, ['BACKEND_UNAVAILABLE'])));
  assert.equal(recovered[0].instance_id, recovered[1].instance_id);
  assert.notEqual(recovered[0].instance_id, owner.instance_id);
  assert.deepEqual(await readFixture(survivor), evidence[0]);
  await Promise.all([close(replacement), close(survivor)]);
  await stopped();
  assertOneIndex();
  assert.deepEqual(persistedIndexes(), persisted);
  assert.deepEqual(sourceSnapshot(), originalSource);
  assert.doesNotMatch(diagnostics.join(''), /STATE_IN_USE|EBUSY/);
  const record = {
    passed: true, platform: process.platform, simultaneousClients: 6, persistentIndexes: 1,
    reconnectCycles: 3, capacityRecoveryOnSameTransport: true,
    bridgeCrashRecoveryOnSameTransport: true, brokerCrashRecoveryOnSameTransport: true,
    abruptParentExitRecovery: true, unchangedParsedRowsReused: true, sourceUnchanged: true,
    codexAtomicRename: process.platform === 'win32' ? 'passed on Windows' : 'passed; Windows locking semantics require Windows CI',
    actualCodexUiRestart: 'not exercised by this protocol integration test'
  };
  mkdirSync(join(root, '.local'), { recursive: true });
  writeFileSync(join(root, '.local/mcp-concurrency-result.json'), JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record, null, 2));
} finally {
  await Promise.allSettled([...clients].map(close));
  await stopped();
  rmSync(temp, { recursive: true, force: true, maxRetries: 30, retryDelay: 100 });
}
