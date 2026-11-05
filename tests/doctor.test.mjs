import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import test from 'node:test';
import { inspectConfiguration, parseDoctorArguments } from '../scripts/doctor.mjs';
import { parseCallArguments } from '../scripts/call-tool.mjs';
import { validateRuntimeOptions } from '../scripts/config-validation.mjs';
import { venvPython } from '../scripts/installer-utils.mjs';

function fixture(t) {
  const base = realpathSync(mkdtempSync(join(tmpdir(), 'df-doctor-')));
  const repo = join(base, 'install'), source = join(base, 'source'), state = join(base, 'state');
  mkdirSync(join(repo, 'dist'), { recursive: true }); mkdirSync(source);
  writeFileSync(join(repo, 'package.json'), JSON.stringify({ version: '0.3.0' }));
  writeFileSync(join(repo, 'dist/index.js'), "const server = { name: 'df-mcp', version: '0.3.0' };\n");
  const pythonDirectory = join(repo, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin'); mkdirSync(pythonDirectory, { recursive: true });
  const python = join(pythonDirectory, process.platform === 'win32' ? 'python.exe' : 'python'); writeFileSync(python, 'not-executed-by-offline-doctor'); chmodSync(python, 0o700);
  const config = join(base, 'config.json');
  writeFileSync(config, JSON.stringify({ root: './source', state_dir: './state', service: { idle_seconds: 60, max_clients: 64 } }));
  writeFileSync(join(source, 'evidence.txt'), 'SIMULATED do not alter');
  t.after(() => rmSync(base, { recursive: true, force: true }));
  return { base, repo, source, state, config, python, env: {} };
}

test('offline doctor shows explicit source and build without creating state or changing files', t => {
  const f = fixture(t), original = readFileSync(f.config);
  const result = inspectConfiguration(f.config, { repo: f.repo, env: {}, home: f.base });
  assert.equal(result.ok, true);
  assert.equal(result.source_path, f.source);
  assert.equal(result.package_version, '0.3.0');
  assert.equal(result.built_version, '0.3.0');
  assert.equal(result.connection_checked, false);
  assert.equal(result.source_scanned, false);
  assert.equal(existsSync(f.state), false);
  assert.deepEqual(readFileSync(f.config), original);
  assert.equal(readFileSync(join(f.source, 'evidence.txt'), 'utf8'), 'SIMULATED do not alter');
});

test('doctor reports stale build and unsafe Codex storage without creating it', t => {
  const f = fixture(t);
  writeFileSync(join(f.repo, 'dist/index.js'), "const server = { name: 'df-mcp', version: '0.2.0' };\n");
  const state = join(f.base, '.codex', 'state');
  writeFileSync(f.config, JSON.stringify({ root: f.source, state_dir: state }));
  const result = inspectConfiguration(f.config, { repo: f.repo, env: {}, home: f.base });
  assert.equal(result.ok, false);
  assert.equal(result.checks.find(item => item.name === 'built_entrypoint').ok, false);
  assert.equal(result.checks.find(item => item.name === 'state_location').ok, false);
  assert.equal(existsSync(state), false);
});

test('doctor rejects overlap and malformed service options while retaining diagnostics', t => {
  const f = fixture(t);
  writeFileSync(f.config, JSON.stringify({ root: f.source, state_dir: join(f.source, 'cache'), service: { max_clients: 0 } }));
  const result = inspectConfiguration(f.config, { repo: f.repo, env: {}, home: f.base });
  assert.equal(result.ok, false);
  assert.equal(result.checks.find(item => item.name === 'state_location').ok, false);
  assert.equal(result.checks.find(item => item.name === 'runtime_options').ok, false);
  assert.equal(existsSync(join(f.source, 'cache')), false);
});

test('doctor rejects every runtime setting the installer would refuse to reuse', t => {
  const f = fixture(t), original = JSON.parse(readFileSync(f.config, 'utf8'));
  const bad = [{ poll_seconds: 86401 }, { poll_seconds: true }, { aliases: [] }, { aliases: { word: ['valid', 1] } }, { limits: null }, { limits: { max_pdf_pages: 1.5 } }, { limits: { unsupported: 1 } }, { service: JSON.parse('{"constructor":1}') }];
  for (const change of bad) {
    writeFileSync(f.config, JSON.stringify({ ...original, ...change }));
    const result = inspectConfiguration(f.config, { repo: f.repo, env: {}, home: f.base });
    assert.equal(result.ok, false, JSON.stringify(change));
    assert.equal(result.checks.find(item => item.name === 'runtime_options').ok, false);
    assert.equal(existsSync(f.state), false);
  }
});

test('doctor fails missing interpreters and resolves an explicitly selected executable on PATH', t => {
  const f = fixture(t);
  rmSync(f.python);
  const missing = inspectConfiguration(f.config, { repo: f.repo, env: {}, home: f.base });
  assert.equal(missing.ok, false);
  assert.equal(missing.checks.find(item => item.name === 'python_executable').ok, false);
  const name = process.platform === 'win32' ? 'python.exe' : 'python';
  const executable = join(f.base, name); writeFileSync(executable, 'not-executed'); chmodSync(executable, 0o700);
  const selected = inspectConfiguration(f.config, { repo: f.repo, env: { DF_MCP_PYTHON: 'python', PATH: f.base }, home: f.base });
  assert.equal(selected.ok, true);
  assert.equal(selected.python, executable);
  if (process.platform !== 'win32') {
    chmodSync(executable, 0o600);
    assert.equal(inspectConfiguration(f.config, { repo: f.repo, env: { DF_MCP_PYTHON: executable }, home: f.base }).ok, false);
  }
  assert.equal(existsSync(f.state), false);
});

test('offline runtime-option checks agree with real Python service configuration', t => {
  const f = fixture(t), repo = resolve('.');
  const cases = [{}, { poll_seconds: 1 }, { poll_seconds: 86400 }, { poll_seconds: 86401 }, { poll_seconds: true }, { aliases: {} }, { aliases: { word: ['synonym'] } }, { aliases: { word: 'bad' } }, { limits: { max_segments: 1 } }, { limits: { max_segments: 1.5 } }, { limits: { unknown: 1 } }, { service: { idle_seconds: 1.5, max_clients: 256 } }, { service: { idle_seconds: 3601 } }, { service: { max_clients: 1.5 } }, { service: JSON.parse('{"__proto__":1}') }];
  const expected = cases.map(config => { try { validateRuntimeOptions(config); return true; } catch { return false; } });
  const script = 'import json,pathlib,sys\nfrom team_drive.service_config import settings\nfrom team_drive.errors import DriveError\np=pathlib.Path(sys.argv[1]); results=[]\nfor options in json.loads(sys.argv[2]):\n p.write_text(json.dumps(dict(root=sys.argv[3],state_dir=sys.argv[4],**options)),encoding="utf-8")\n try: settings(str(p)); results.append(True)\n except DriveError: results.append(False)\nprint(json.dumps(results))';
  const child = spawnSync(venvPython(repo), ['-c', script, f.config, JSON.stringify(cases), f.source, f.state], { encoding: 'utf8', env: { ...process.env, PYTHONPATH: join(repo, 'python'), PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }, timeout: 15000 });
  assert.equal(child.status, 0, child.stderr);
  assert.deepEqual(JSON.parse(child.stdout), expected);
  assert.equal(existsSync(f.state), false);
});

test('retrieval and diagnostics bounds agree across offline JS and Python without starting models', t => {
  const f = fixture(t), repo = resolve('.');
  const cases = [
    { label: 'safe defaults', options: {}, valid: true },
    { label: 'explicit empty defaults', options: { retrieval: {}, diagnostics: {} }, valid: true },
    { label: 'complete local model configuration', valid: true, options: {
      retrieval: { candidate_limit: 1000, rrf_k: 1000,
        semantic: { enabled: true, endpoint: 'http://localhost:11434/v1/embeddings', model: 'synthetic-embedding',
          model_revision: 'synthetic-revision', timeout_seconds: 20, batch_size: 64, max_segments: 100000,
          max_text_chars: 32000, minimum_similarity: -1, per_file_budget_seconds: 20 },
        rerank: { enabled: true, endpoint: 'https://192.168.1.2:8443/rerank', model: 'synthetic-rerank', timeout_seconds: 0.1, max_candidates: 100 } },
      diagnostics: { enabled: false, max_events: 10000, retention_days: 90 } } },
  ];
  const allowedEndpoints = ['http://127.0.0.1:11434/embed', 'http://localhost:1234/v1/embeddings',
    'http://[::1]:1234/embed', 'http://[0:0:0:0:0:0:0:1]/embed', 'http://[fc00::1]/embed',
    'http://[fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/embed', 'https://10.1.2.3/embed',
    'http://172.16.0.1/embed', 'http://172.31.255.254/embed', 'http://192.168.1.2/embed'];
  for (const endpoint of allowedEndpoints) cases.push({ label: `local ${endpoint}`, valid: true,
    options: { retrieval: { semantic: { enabled: true, endpoint, model: 'synthetic' } } } });
  const badEndpoints = ['https://api.example.com/v1/embeddings', 'http://8.8.8.8/embed',
    'http://169.254.169.254/embed', 'http://172.32.0.1/embed', 'http://[fe80::1]/embed',
    'http://[::ffff:127.0.0.1]/embed', 'http://model.internal/embed', 'http://user:secret@127.0.0.1/embed',
    'http://@127.0.0.1/embed', 'http://127.0.0.1/embed?key=secret', 'http://127.0.0.1/embed#fragment',
    'http://127.0.0.1:0/embed', 'http://127.0.0.1:65536/embed', 'http://127.0.0.1:/embed',
    'http://[::1]:/embed', 'http://2130706433/embed', 'http://127.1/embed', 'http://0x7f000001/embed'];
  for (const endpoint of badEndpoints) for (const kind of ['semantic', 'rerank']) cases.push({
    label: `${kind} rejects ${endpoint}`, valid: false, options: { retrieval: { [kind]: { endpoint } } } });
  for (const options of [{ retrieval: null }, { retrieval: [] }, { retrieval: { enabled: true } },
    { diagnostics: null }, { diagnostics: [] }, { diagnostics: { enabled: 'true' } },
    { diagnostics: { unknown: 1 } }, { diagnostics: { max_events: 10001 } }, { diagnostics: { retention_days: 91 } }]) {
    cases.push({ label: JSON.stringify(options), options, valid: false });
  }
  for (const key of ['max_events', 'retention_days']) for (const value of [-1, 0, true, 1.5]) {
    cases.push({ label: `diagnostics.${key}=${value}`, valid: false, options: { diagnostics: { [key]: value } } });
  }
  const badSemantic = [['enabled', 'true'], ['enabled', true], ['timeout_seconds', 0.09], ['timeout_seconds', 20.01],
    ['timeout_seconds', true], ['per_file_budget_seconds', 0], ['per_file_budget_seconds', 20.01],
    ['per_file_budget_seconds', true], ['batch_size', 65], ['max_segments', 100001], ['max_text_chars', 32001],
    ['minimum_similarity', -1.01], ['minimum_similarity', 1.01]];
  const badRerank = [['enabled', true], ['timeout_seconds', 0], ['timeout_seconds', 21], ['max_candidates', 0],
    ['max_candidates', 101], ['model_revision', 'not-supported']];
  for (const [kind, invalid] of [['semantic', badSemantic], ['rerank', badRerank]]) for (const [key, value] of invalid) {
    cases.push({ label: `${kind}.${key}=${value}`, valid: false, options: { retrieval: { [kind]: { [key]: value } } } });
  }
  for (const key of ['candidate_limit', 'rrf_k']) for (const value of [0, 1001, true]) {
    cases.push({ label: `${key}=${value}`, valid: false, options: { retrieval: { [key]: value } } });
  }
  const js = cases.map(({ options }) => { try { validateRuntimeOptions(options); return true; } catch { return false; } });
  const script = 'import json,pathlib,sys\nfrom unittest import mock\nfrom team_drive.service_config import settings\nfrom team_drive.errors import DriveError\np=pathlib.Path(sys.argv[1]); results=[]\nwith mock.patch("team_drive.retrieval._post",side_effect=AssertionError("Configuration cannot call models")):\n for options in json.loads(sys.argv[2]):\n  p.write_text(json.dumps(dict(root=sys.argv[3],state_dir=sys.argv[4],**options)),encoding="utf-8")\n  try: settings(str(p)); results.append(True)\n  except DriveError: results.append(False)\nprint(json.dumps(results))';
  const child = spawnSync(venvPython(repo), ['-c', script, f.config, JSON.stringify(cases.map(item => item.options)), f.source, f.state], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: join(repo, 'python'), PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }, timeout: 15000 });
  assert.equal(child.status, 0, child.stderr);
  const python = JSON.parse(child.stdout);
  const mismatches = cases.flatMap((entry, index) => js[index] === entry.valid && python[index] === entry.valid ? [] :
    [{ label: entry.label, expected: entry.valid, js: js[index], python: python[index] }]);
  assert.deepEqual(mismatches, []);
  assert.equal(existsSync(f.state), false);
  assert.equal(readFileSync(join(f.source, 'evidence.txt'), 'utf8'), 'SIMULATED do not alter');
});

test('diagnostic CLI requires a source selection before starting a service', t => {
  const f = fixture(t);
  assert.throws(() => parseCallArguments([]), /No implicit source/);
  assert.throws(() => parseCallArguments(['--demo', '--config', f.config]), /exactly one/);
  assert.throws(() => parseCallArguments(['team_drive_read', '[]', '--config', f.config]), /JSON object/);
  assert.throws(() => parseDoctorArguments([]), /--config/);
  assert.equal(parseCallArguments(['team_drive_status', '--config', f.config]).config, f.config);
  assert.equal(parseCallArguments(['tools', '--demo']).demo, true);
  const child = spawnSync(process.execPath, [resolve('scripts/call-tool.mjs')], { encoding: 'utf8', env: { ...process.env, DF_MCP_PYTHON: join(f.base, 'must-not-start') } });
  assert.equal(child.status, 1);
  assert.match(child.stderr, /No implicit source/);
  assert.equal(existsSync(f.state), false);
});
