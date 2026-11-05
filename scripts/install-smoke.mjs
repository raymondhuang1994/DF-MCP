// Exercise the public installation/configuration path after npm ci + setup have
// installed dependencies and built dist/. All profiles, sources and skills use
// a disposable user home; this is not a dependency-download or Codex UI test.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const repository = realpathSync.native(resolve(dirname(fileURLToPath(import.meta.url)), '..'));
const version = JSON.parse(readFileSync(join(repository, 'package.json'), 'utf8')).version;
const python = join(repository, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
assert.ok(existsSync(python) && existsSync(join(repository, 'dist/index.js')), 'Run npm ci and npm run setup -- --skip-config first.');
// Match configure's native path canonicalization, including Windows 8.3 aliases.
const temporary = realpathSync.native(mkdtempSync(join(tmpdir(), 'df-mcp-install-')));
const testHome = join(temporary, '模拟用户 home');
const codexHome = join(testHome, '.codex');
const source = join(temporary, '团队资料 read only');
const sourceName = '共同证据 sample.txt';
const sourceText = 'SIMULATED installation evidence\nshared-install-marker-728 中文支出\n';
const childEnvironment = {
  ...Object.fromEntries(Object.entries(process.env).filter(([, value]) => typeof value === 'string')),
  HOME: testHome, USERPROFILE: testHome, CODEX_HOME: codexHome,
  LOCALAPPDATA: join(testHome, 'AppData', 'Local'), XDG_STATE_HOME: join(testHome, '.local', 'state'),
  PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1'
};
// The public default must find this checkout's installed interpreter. Do not
// let a developer's override or import path make an incomplete installation pass.
for (const name of ['DF_MCP_PYTHON', 'TEAM_DRIVE_PYTHON', 'PYTHONPATH']) delete childEnvironment[name];
mkdirSync(source);
mkdirSync(codexHome, { recursive: true });
writeFileSync(join(source, sourceName), sourceText);
const clients = new Set();
const stderr = [];
let state;
let endpoint;
let registration;
const pause = milliseconds => new Promise(done => setTimeout(done, milliseconds));

function snapshot(directory) {
  const result = [];
  for (const name of readdirSync(directory).sort()) {
    const path = join(directory, name), info = statSync(path);
    result.push({ name, mtime: info.mtimeMs, mode: info.mode, size: info.size,
      sha256: createHash('sha256').update(readFileSync(path)).digest('hex') });
  }
  return result;
}
const originalSource = snapshot(source);

function cli(script, args = [], expected = 0) {
  const result = spawnSync(process.execPath, [join(repository, 'scripts', script), ...args], {
    cwd: repository, env: childEnvironment, encoding: 'utf8', timeout: 30000, windowsHide: true
  });
  assert.ifError(result.error);
  assert.equal(result.status, expected, `${script}: ${result.stderr || result.stdout}`);
  return result.stdout;
}

function configurationFrom(output) {
  const match = /^Configuration: (.+)$/m.exec(output);
  assert.ok(match, 'The real configuration CLI must print its generated profile path.');
  const path = match[1].trim();
  assert.ok(isAbsolute(path));
  return path;
}

function outside(parent, child) {
  const path = relative(parent, child);
  return path === '..' || path.startsWith(`..${sep}`) || isAbsolute(path);
}

function registrationFrom(output, config) {
  const match = /^codex mcp add df_mcp -- (.+)$/m.exec(output);
  assert.ok(match, 'The configuration CLI must print its complete MCP registration command.');
  // Decode the documented POSIX/PowerShell single-quoted arguments as data.
  // Never invoke a shell or execute the printed command.
  const tokens = [];
  let value = '', quoted = false, started = false;
  const line = match[1].trim();
  for (let index = 0; index < line.length; index++) {
    const char = line[index];
    if (quoted) {
      if (char === "'") {
        if (process.platform === 'win32' && line[index + 1] === "'") { value += "'"; index++; }
        else quoted = false;
      } else value += char;
      continue;
    }
    if (char === "'") { quoted = true; started = true; }
    else if (char === '\\' && process.platform !== 'win32') {
      assert.ok(index + 1 < line.length, 'Incomplete shell escape in registration output.');
      value += line[++index]; started = true;
    } else if (/\s/.test(char)) {
      if (started) { tokens.push(value); value = ''; started = false; }
    } else {
      assert.ok(!';|&`$"'.includes(char), 'Registration output must quote shell-special characters.');
      value += char; started = true;
    }
  }
  assert.equal(quoted, false, 'Unclosed registration quote.');
  if (started) tokens.push(value);
  assert.ok(isAbsolute(tokens[0]));
  assert.equal(realpathSync.native(tokens[0]), realpathSync.native(process.execPath), 'The absolute launcher must refer to the current validated Node interpreter.');
  assert.deepEqual(tokens.slice(1), [join(repository, 'dist/index.js'), '--config', config, '--python', python]);
  return { command: tokens[0], args: tokens.slice(1) };
}

async function connect(config, label) {
  const client = new Client({ name: `df-mcp-install-${label}`, version: '1.0.0' });
  clients.add(client);
  const transport = new StdioClientTransport({
    ...registration,
    // Use the CLI's actual recommendation from a different working directory.
    cwd: temporary,
    env: { ...childEnvironment, PATH: join(temporary, 'empty desktop PATH'), DF_MCP_PYTHON: join(temporary, 'wrong-python') },
    stderr: 'pipe'
  });
  transport.stderr.on('data', data => stderr.push(data.toString()));
  await client.connect(transport);
  return client;
}

async function close(client) {
  await client.close();
  clients.delete(client);
}

async function call(client, name, args = {}) {
  const result = (await client.callTool({ name: `team_drive_${name}`, arguments: args })).structuredContent;
  assert.equal(result?.ok, true, JSON.stringify(result));
  assert.ok(Buffer.byteLength(JSON.stringify(result)) <= 65536);
  return result;
}

async function ready(client) {
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const status = await call(client, 'status');
    assert.equal(status.service_version, version);
    assert.equal(status.state_storage.mode, 'shared');
    assert.equal(status.state_storage.index_copies, 1);
    if (status.coverage.inventory_complete && status.coverage.all_present_files_indexed) {
      assert.equal(status.coverage.present_files, 1);
      return status;
    }
    await pause(100);
  }
  throw new Error('The configured synthetic source did not finish indexing.');
}

async function readFixture(client) {
  const listed = await call(client, 'list');
  assert.equal(listed.files.length, 1);
  const file = listed.files[0];
  assert.equal(file.path, sourceName);
  const inspected = await call(client, 'inspect', { file_id: file.file_id });
  const read = await call(client, 'read', { file_id: file.file_id, expected_version: inspected.source_version });
  assert.match(read.segments.map(segment => segment.text).join('\n'), /shared-install-marker-728 中文支出/);
  return { file_id: file.file_id, source_version: inspected.source_version, segments: read.segments };
}

async function stopped() {
  if (!endpoint) return;
  const deadline = Date.now() + 15000;
  while (existsSync(endpoint) && Date.now() < deadline) await pause(100);
  assert.equal(existsSync(endpoint), false, 'The last client must release the shared service after its configured idle period.');
}

function persistedRows() {
  assert.equal(clients.size, 0);
  const result = spawnSync(python, ['-c', [
    'import json, pathlib, sqlite3, sys',
    'path = pathlib.Path(sys.argv[1]) / "shared-v1" / "index" / "index.sqlite"',
    'with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:',
    '    print(json.dumps(db.execute("SELECT path, source_version, updated_at FROM files ORDER BY path").fetchall()))'
  ].join('\n'), state], { cwd: repository, env: childEnvironment, encoding: 'utf8', timeout: 15000, windowsHide: true });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

function codexAtomicReplacement() {
  // These are disposable fixture files, never a user's real Codex global state.
  const target = join(codexHome, '.codex-global-state.json');
  writeFileSync(target, '{"revision":0}');
  const incoming = join(codexHome, 'atomic-replacement.tmp');
  writeFileSync(incoming, '{"revision":1}');
  renameSync(incoming, target);
  assert.deepEqual(JSON.parse(readFileSync(target, 'utf8')), { revision: 1 });
}

try {
  const configured = cli('configure.mjs', ['--root', source, '--yes']);
  const config = configurationFrom(configured);
  registration = registrationFrom(configured, config);
  const initial = JSON.parse(readFileSync(config, 'utf8'));
  const expectedBase = process.platform === 'win32' ? join(childEnvironment.LOCALAPPDATA, 'df-mcp')
    : process.platform === 'darwin' ? join(testHome, 'Library', 'Application Support', 'df-mcp')
    : join(childEnvironment.XDG_STATE_HOME, 'df-mcp');
  assert.equal(dirname(dirname(dirname(config))), expectedBase);
  assert.equal(initial.root, source);
  state = initial.state_dir;
  endpoint = join(state, 'shared-v1', 'control', 'endpoint.json');
  assert.ok(outside(codexHome, state) && outside(source, state) && outside(repository, state));
  assert.equal(existsSync(state), false, 'Configuring a source must not create or scan an index.');
  assert.deepEqual(snapshot(source), originalSource);
  assert.deepEqual(readdirSync(join(expectedBase, 'profiles')), [dirname(config).split(sep).at(-1)]);

  // Reconfiguration must preserve a user's valid choices and byte formatting.
  const customized = { service: { idle_seconds: 1, max_clients: 4 }, aliases: { 花费: ['支出'] },
    poll_seconds: 3600, state_dir: state, root: source, provider: 'local' };
  const customizedText = JSON.stringify(customized, null, 4) + '\n';
  writeFileSync(config, customizedText);
  const reused = cli('configure.mjs', ['--root', source, '--yes']);
  assert.equal(configurationFrom(reused), config);
  assert.deepEqual(registrationFrom(reused, config), registration);
  assert.match(reused, /Existing configuration reused/);
  assert.equal(readFileSync(config, 'utf8'), customizedText);
  assert.equal(readdirSync(join(expectedBase, 'profiles')).length, 1);
  assert.equal(existsSync(state), false);

  cli('install-skill.mjs');
  const skill = join(codexHome, 'skills', 'df-mcp');
  assert.equal(readFileSync(join(skill, 'SKILL.md'), 'utf8'), readFileSync(join(repository, 'skills/df-mcp/SKILL.md'), 'utf8'));
  writeFileSync(join(skill, 'local-note.txt'), 'SIMULATED user customization: preserve in the explicit-upgrade backup.');
  cli('install-skill.mjs', [], 1);
  assert.ok(existsSync(join(skill, 'local-note.txt')));
  cli('install-skill.mjs', ['--replace']);
  assert.equal(existsSync(join(skill, 'local-note.txt')), false);
  const backups = readdirSync(join(codexHome, 'skill-backups'));
  assert.equal(backups.length, 1);
  assert.ok(existsSync(join(codexHome, 'skill-backups', backups[0], 'df-mcp', 'local-note.txt')));

  const doctor = JSON.parse(cli('doctor.mjs', ['--config', config]));
  assert.equal(doctor.ok, true);
  assert.equal(doctor.source_path, source);
  assert.equal(doctor.state_path, state);
  assert.equal(doctor.connection_checked, false);
  assert.equal(doctor.source_scanned, false);
  assert.equal(existsSync(state), false);

  const pair = await Promise.all([connect(config, 'first-a'), connect(config, 'first-b')]);
  const statuses = await Promise.all(pair.map(ready));
  assert.equal(statuses[0].instance_id, statuses[1].instance_id);
  assert.equal(statuses[0].source_id, statuses[1].source_id);
  const evidence = await Promise.all(pair.map(readFixture));
  assert.deepEqual(evidence[0], evidence[1]);
  const alias = await call(pair[0], 'search', { query: '花费' });
  assert.ok(alias.files.some(file => file.path === sourceName));
  codexAtomicReplacement();
  await Promise.all(pair.map(close));
  await stopped();
  const persisted = persistedRows();
  assert.equal(persisted.length, 1);

  const reopened = await Promise.all([connect(config, 'reopen-a'), connect(config, 'reopen-b')]);
  const reopenedStatuses = await Promise.all(reopened.map(ready));
  assert.equal(reopenedStatuses[0].instance_id, reopenedStatuses[1].instance_id);
  assert.notEqual(reopenedStatuses[0].instance_id, statuses[0].instance_id);
  for (const client of reopened) assert.deepEqual(await readFixture(client), evidence[0]);
  codexAtomicReplacement();
  await Promise.all(reopened.map(close));
  await stopped();
  assert.deepEqual(persistedRows(), persisted, 'A reopened installation must reuse parsed versions and timestamps.');
  assert.equal(existsSync(join(state, 'pool-v1')), false);
  assert.equal(readdirSync(join(expectedBase, 'profiles')).length, 1);
  assert.equal(readFileSync(config, 'utf8'), customizedText);
  assert.deepEqual(snapshot(source), originalSource);
  assert.doesNotMatch(stderr.join(''), /STATE_IN_USE|EBUSY/);
  const record = {
    passed: true, platform: process.platform, serviceVersion: version,
    realConfigurationCli: true, defaultStateOutsideCodex: true,
    unicodeAndSpaces: true, profiles: 1, preservedCustomConfiguration: true,
    skillInstallAndExplicitUpgradeBackup: true, actualRecommendedLaunchCommand: true, absoluteInstalledInterpreter: true,
    desktopPathAndPythonOverrideIsolation: true,
    simultaneousClients: 2, persistentIndexes: 1, idleRestartReusedParsedRows: true,
    codexAtomicReplacement: true, sourceUnchanged: true,
    scope: 'Fresh generated profile and Skill installation on an already installed and built checkout; no dependency downloads or Codex UI automation.'
  };
  mkdirSync(join(repository, '.local'), { recursive: true });
  writeFileSync(join(repository, '.local/install-smoke-result.json'), JSON.stringify(record, null, 2));
  console.log(JSON.stringify(record, null, 2));
} finally {
  await Promise.allSettled([...clients].map(close));
  await stopped();
  rmSync(temporary, { recursive: true, force: true, maxRetries: 30, retryDelay: 100 });
}
