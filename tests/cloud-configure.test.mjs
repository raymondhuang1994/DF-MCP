import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, linkSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { makeCloudPlan, parseCloudArguments, runConfigureCloud } from '../scripts/configure-cloud.mjs';
import { parseLoginArguments, runLoginCloud } from '../scripts/login-cloud.mjs';
import { preflightRoot, repositoryRoot } from '../scripts/configure.mjs';
import { validateGraphOptions } from '../scripts/config-validation.mjs';
import { inspectConfiguration } from '../scripts/doctor.mjs';
import { venvPython } from '../scripts/installer-utils.mjs';

const identity = { tenant_id: '00000000-0000-0000-0000-000000000001', client_id: '00000000-0000-0000-0000-000000000002', drive_id: 'b!SYNTHETIC_DRIVE', folder_id: 'SYNTHETIC_FOLDER' };

function fixture(t) {
  const base = realpathSync.native(mkdtempSync(join(tmpdir(), 'df-mcp-cloud-config-')));
  const repo = join(base, 'cloud-reader-installation'); mkdirSync(repo);
  t.after(() => rmSync(base, { recursive: true, force: true }));
  return { base, repo, state: join(base, '私有 云盘状态') };
}

function argumentsFor(f, extra = {}) {
  const values = { ...identity, ...extra };
  return [...Object.entries(values).flatMap(([key, value]) => [`--${key.replaceAll('_', '-')}`, value]), '--state-dir', f.state, '--yes'];
}

function runtime(f, extra = {}) {
  let output = '';
  return { repo: f.repo, env: {}, home: f.base, validateRoot: () => {}, output: { write: text => { output += text; } }, getOutput: () => output, ...extra };
}

function linkOrSkip(t, target, path, directory = true) {
  try { symlinkSync(target, path, directory ? (process.platform === 'win32' ? 'junction' : 'dir') : 'file'); return true; }
  catch (error) { if (error.code === 'EPERM') { t.skip('Symlink privilege unavailable'); return false; } throw error; }
}

test('cloud configuration requires four explicit IDs and confirmation, never selecting a default source', t => {
  const f = fixture(t);
  assert.throws(() => parseCloudArguments([]), /requires --yes/);
  assert.throws(() => parseCloudArguments(['--yes']), /tenant_id/);
  assert.throws(() => parseCloudArguments(argumentsFor(f).slice(0, -1)), /requires --yes/);
  for (const values of [{ tenant_id: 'other-directory' }, { client_id: 'not-a-guid' }, { drive_id: 'https://example.invalid/shared' }, { folder_id: '../folder' }, { folder_id: 'root' }, { drive_id: '..' }]) assert.throws(() => parseCloudArguments(argumentsFor(f, values)), /valid/);
  for (const alias of ['common', 'organizations', 'consumers']) assert.equal(parseCloudArguments(argumentsFor(f, { tenant_id: alias })).identity.tenant_id, alias);
  assert.throws(() => parseCloudArguments([...argumentsFor(f), '--client-secret', 'synthetic']), /Unknown argument/);
  assert.throws(() => parseCloudArguments([...argumentsFor(f), '--yes']), /Duplicate/);
  assert.equal(existsSync(f.state), false);
});

test('configuration creates only config and empty anchor, prints pinned registration and no credentials', async t => {
  const f = fixture(t), r = runtime(f);
  const result = await runConfigureCloud(argumentsFor(f), r);
  assert.equal(result.config.provider, 'microsoft_graph');
  assert.deepEqual(result.config.graph, { ...identity, auth_cache: result.plan.authCache });
  assert.deepEqual(readdirSync(result.plan.profile).sort(), ['cloud-anchor', 'config.json']);
  assert.deepEqual(readdirSync(result.plan.root), []);
  assert.equal(existsSync(result.plan.state), false);
  assert.equal(existsSync(result.plan.authCache), false);
  assert.match(r.getOutput(), /No cloud files scanned/);
  assert.match(r.getOutput(), /codex mcp add df_mcp/);
  assert.ok(r.getOutput().includes(join(f.repo, 'dist', 'index.js')));
  assert.doesNotMatch(readFileSync(result.plan.configPath, 'utf8'), /access_token|refresh_token|client_secret/);
});

test('same cloud binding preserves existing formatting and runtime options byte for byte', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const existing = { ...first.config, root: './cloud-anchor', state_dir: './state', poll_seconds: 600, aliases: { '预算': ['支出'] }, graph: { ...first.config.graph, auth_cache: './auth.json', token_env: 'DF_MCP_SYNTHETIC_TOKEN' } };
  const bytes = JSON.stringify(existing) + '\n'; writeFileSync(first.plan.configPath, bytes);
  const reused = await runConfigureCloud(argumentsFor(f), runtime(f));
  assert.equal(reused.reused, true);
  assert.equal(readFileSync(first.plan.configPath, 'utf8'), bytes);
  assert.deepEqual(reused.config, existing);
});

test('changed source IDs produce a different profile and never alter the original', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const bytes = readFileSync(first.plan.configPath, 'utf8');
  const next = await runConfigureCloud(argumentsFor(f, { folder_id: 'ANOTHER_SYNTHETIC_FOLDER' }), runtime(f));
  assert.notEqual(next.plan.profile, first.plan.profile);
  assert.equal(readFileSync(first.plan.configPath, 'utf8'), bytes);
});

test('existing profile cannot be rebound to another tenant, folder, root, state or auth cache', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  for (const change of [{ root: join(f.base, 'other') }, { state_dir: join(f.base, 'other') }, { graph: { ...first.config.graph, tenant_id: '00000000-0000-0000-0000-000000000003' } }, { graph: { ...first.config.graph, folder_id: 'OTHER' } }, { graph: { ...first.config.graph, auth_cache: join(f.base, 'other.json') } }]) {
    const bytes = JSON.stringify({ ...first.config, ...change }); writeFileSync(first.plan.configPath, bytes);
    await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /different cloud source/);
    assert.equal(readFileSync(first.plan.configPath, 'utf8'), bytes);
  }
});

test('state paths reject installation overlap, .codex, CODEX_HOME and network syntax before writes', t => {
  const f = fixture(t);
  for (const path of [f.base, join(f.repo, 'state')]) assert.throws(() => makeCloudPlan(identity, path, { repo: f.repo, home: f.base, env: {} }), /installation must not overlap/);
  for (const path of [join(f.base, '.codex', 'state'), join(f.base, 'custom-codex', 'state')]) assert.throws(() => makeCloudPlan(identity, path, { repo: f.repo, home: f.base, env: { CODEX_HOME: join(f.base, 'custom-codex') } }), /outside .codex and CODEX_HOME/);
  for (const path of ['\\\\server\\share', '//server/share']) assert.throws(() => makeCloudPlan(identity, path, { repo: f.repo }), /ordinary local/);
  assert.equal(existsSync(f.state), false);
});

test('linked storage ancestor and linked anchor, auth cache or config are rejected without following them', async t => {
  const f = fixture(t), actual = join(f.base, 'actual'); mkdirSync(actual);
  const linked = join(f.base, 'linked');
  if (!linkOrSkip(t, actual, linked)) return;
  assert.throws(() => makeCloudPlan(identity, join(linked, 'state'), { repo: f.repo }), /symbolic links or junctions/);
  const first = await runConfigureCloud(argumentsFor(f), runtime(f));
  for (const key of ['root', 'authCache', 'configPath']) {
    const destination = first.plan[key];
    const bytes = key === 'configPath' ? readFileSync(destination) : null;
    rmSync(destination, { recursive: true, force: true });
    const file = join(actual, 'ordinary.json'); writeFileSync(file, '{}');
    if (!linkOrSkip(t, key === 'root' ? actual : file, destination, key === 'root')) return;
    await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /symbolic links or junctions/);
    rmSync(destination);
    if (key === 'root') mkdirSync(destination);
    if (bytes) writeFileSync(destination, bytes);
  }
});

test('anchor must remain empty and a missing bound anchor is not silently recreated', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  writeFileSync(join(first.plan.root, 'unexpected.txt'), 'synthetic');
  await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /must remain empty/);
  rmSync(first.plan.root, { recursive: true });
  await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /cloud-anchor directory is missing/);
  assert.equal(existsSync(first.plan.root), false);
});

test('failed local boundary preflight never publishes a cloud config or creates auth/index', async t => {
  const f = fixture(t);
  await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f, { validateRoot: () => { throw new Error('UNSAFE_ROOT'); } })), /UNSAFE_ROOT/);
  const plan = makeCloudPlan(identity, f.state, { repo: f.repo, home: f.base, env: {} });
  assert.equal(existsSync(plan.configPath), false);
  assert.equal(existsSync(plan.authCache), false);
  assert.equal(existsSync(plan.state), false);
});

test('configuration size and hardlinked cache are rejected without modifying their contents', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const bytes = readFileSync(first.plan.configPath);
  writeFileSync(first.plan.configPath, ' '.repeat(262145));
  await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /no larger than 256 KiB/);
  writeFileSync(first.plan.configPath, bytes);
  const original = join(f.base, 'cache-original.json'); writeFileSync(original, '{}');
  linkSync(original, first.plan.authCache);
  await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /ordinary unlinked files/);
  assert.equal(readFileSync(original, 'utf8'), '{}');
});

test('POSIX profile with wider permissions is rejected before reading credentials or rewriting config', async t => {
  if (process.platform === 'win32') { t.skip('POSIX mode check; Windows credential protection uses DPAPI'); return; }
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const bytes = readFileSync(first.plan.configPath);
  chmodSync(first.plan.profile, 0o755);
  await assert.rejects(runConfigureCloud(argumentsFor(f), runtime(f)), /owner-only permissions/);
  assert.deepEqual(readFileSync(first.plan.configPath), bytes);
});

test('real local Python boundary preflight accepts the generated empty anchor without cloud contact', async t => {
  const f = fixture(t);
  const result = await runConfigureCloud(argumentsFor(f), runtime(f, { repo: repositoryRoot, validateRoot: preflightRoot }));
  assert.deepEqual(readdirSync(result.plan.root), []);
  assert.equal(existsSync(result.plan.authCache), false);
  assert.equal(existsSync(result.plan.state), false);
});

test('login explicitly dispatches the documented Python module without shell and preserves exit status', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const before = readdirSync(first.plan.profile).sort();
  let calls = 0;
  const options = runtime(f, { resolvePython: value => value, runner: (python, args, options) => {
    calls++;
    assert.ok(python.includes('.venv'));
    assert.deepEqual(args, ['-m', 'team_drive.graph_auth', '--config', first.plan.configPath]);
    assert.equal(options.shell, false);
    assert.equal(options.stdio, 'inherit');
    assert.equal(options.env.PYTHONPATH, join(f.repo, 'python'));
    return { status: 130 };
  } });
  assert.equal(runLoginCloud(['--config', first.plan.configPath], options), 130);
  assert.equal(calls, 1);
  assert.deepEqual(readdirSync(first.plan.profile).sort(), before);
});

test('login rejects local configurations and unsafe auth paths before invoking Python', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const options = runtime(f, { runner: () => assert.fail('must not start authentication') });
  for (const config of [{ ...first.config, provider: 'local' }, { ...first.config, graph: { ...first.config.graph, auth_cache: join(first.plan.root, 'auth.json') } }, { ...first.config, graph: { ...first.config.graph, auth_cache: join(f.base, '.codex', 'auth.json') } }]) {
    writeFileSync(first.plan.configPath, JSON.stringify(config));
    assert.throws(() => runLoginCloud(['--config', first.plan.configPath], options), /Microsoft Graph configuration|non-overlapping|outside .codex/);
  }
  assert.throws(() => parseLoginArguments([]), /Usage/);
  assert.equal(runLoginCloud(['--help'], options), 0);
});

test('JS and actual Python agree on cloud IDs, scopes, explicit null cache and bounded limits without network', t => {
  const f = fixture(t), base = { ...identity, auth_cache: join(f.base, 'private', 'auth.json') };
  const cases = [{ label: 'defaults', graph: base, valid: true }];
  const add = (label, change, valid) => cases.push({ label, graph: { ...base, ...change }, valid });
  for (const alias of ['common', 'organizations', 'consumers']) add(alias, { tenant_id: alias }, true);
  add('uppercase UUID', { client_id: 'ABCDEF01-2345-6789-ABCD-EF0123456789' }, true);
  add('opaque ID boundary', { drive_id: 'b!' + 'a'.repeat(254), folder_id: 'A_-.~!' }, true);
  for (const key of ['tenant_id', 'client_id', 'drive_id', 'folder_id']) {
    for (const value of [null, true, 7, [], {}, '', '../escape', 'https://example.invalid', base[key] + '\n']) add(`${key} rejects ${JSON.stringify(value)}`, { [key]: value }, false);
  }
  for (const key of ['drive_id', 'folder_id']) for (const value of ['.', '..', 'root', 'a,b', 'a'.repeat(257)]) add(`${key} rejects reserved/oversized ID ${value.length}`, { [key]: value }, false);
  for (const scopes of [['Files.Read'], ['Files.Read.All', 'Sites.Read.All'], ['files.read', 'https://graph.microsoft.com/SITES.READ.ALL'], ['Files.Read', 'offline_access', 'openid', 'profile'], Array(6).fill('Files.Read')]) add(`valid scopes ${JSON.stringify(scopes)}`, { scopes }, true);
  for (const scopes of [null, [], 'Files.Read', ['openid'], ['Files.ReadWrite'], ['Files.Read', null], ['Files.Read', 'User.Read'], Array(7).fill('Files.Read'), ['Files.Read\n'], ['HTTPS://graph.microsoft.com/Files.Read'], ['Files.Read', 'OPENID']]) add(`invalid scopes ${JSON.stringify(scopes)}`, { scopes }, false);
  for (const value of [null, '', ' ', true, 1, [], {}, 'bad\0path']) add(`auth_cache rejects ${JSON.stringify(value)}`, { auth_cache: value, token_env: 'SYNTHETIC_CLOUD_TOKEN' }, false);
  for (const value of [null, '', true, '1TOKEN', 'TOKEN\n', 'a'.repeat(129)]) add(`token_env rejects ${JSON.stringify(value)}`, { token_env: value }, false);
  const envOnly = { ...identity, token_env: 'SYNTHETIC_CLOUD_TOKEN' };
  cases.push({ label: 'env only without cache', graph: envOnly, valid: true }, { label: 'no credentials configured', graph: { ...identity }, valid: false }, { label: 'unknown field', graph: { ...base, client_secret: 'not-a-real-secret' }, valid: false });
  const limits = { timeout_seconds: [0.1, 20, false], max_items: [1, 100000, true], max_pages: [1, 10000, true], max_depth: [1, 128, true], scan_budget_seconds: [1, 600, false] };
  for (const [key, [minimum, maximum, integer]] of Object.entries(limits)) {
    add(`${key} minimum`, { [key]: minimum }, true); add(`${key} maximum`, { [key]: maximum }, true);
    for (const value of [null, true, String(minimum), minimum - 0.01, maximum + 0.01]) add(`${key} invalid ${value}`, { [key]: value }, false);
    if (integer) add(`${key} fractional`, { [key]: 1.5 }, false);
  }
  const js = cases.map(({ graph }) => { try { validateGraphOptions(graph); return true; } catch { return false; } });
  const script = `import json,sys
from unittest import mock
from team_drive.graph_config import graph_settings
from team_drive.errors import DriveError
results=[]
with mock.patch('team_drive.graph_auth._request',side_effect=AssertionError('Offline configuration must not authenticate')):
 for graph in json.load(sys.stdin):
  try: graph_settings(graph); results.append(True)
  except DriveError: results.append(False)
print(json.dumps(results))`;
  const child = spawnSync(venvPython(repositoryRoot), ['-c', script], { input: JSON.stringify(cases.map(item => item.graph)), encoding: 'utf8', timeout: 15000,
    env: { ...process.env, PYTHONPATH: join(repositoryRoot, 'python'), PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' } });
  assert.equal(child.status, 0, child.stderr);
  const python = JSON.parse(child.stdout);
  const mismatches = cases.flatMap((entry, index) => js[index] === entry.valid && python[index] === entry.valid ? [] : [{ label: entry.label, expected: entry.valid, js: js[index], python: python[index] }]);
  assert.deepEqual(mismatches, []);
  assert.equal(existsSync(join(f.base, 'private')), false);
  t.diagnostic(`${cases.length} cloud configuration cases agreed across JS/Python.`);
});

function offlineDoctorRuntime(f) {
  mkdirSync(join(f.repo, 'dist'), { recursive: true });
  writeFileSync(join(f.repo, 'package.json'), JSON.stringify({ version: '0.5.0' }));
  writeFileSync(join(f.repo, 'dist', 'index.js'), "const server = { name: 'df-mcp', version: '0.5.0' };\n");
  const python = venvPython(f.repo);
  mkdirSync(join(f.repo, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin'), { recursive: true });
  writeFileSync(python, 'not-executed-by-offline-doctor'); chmodSync(python, 0o700);
  return { repo: f.repo, env: {}, home: f.base };
}

test('offline doctor checks an empty cloud anchor and rejects misplaced auth cache without creating state', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const options = offlineDoctorRuntime(f);
  const good = inspectConfiguration(first.plan.configPath, options);
  assert.equal(good.ok, true); assert.equal(good.connection_checked, false);
  writeFileSync(join(first.plan.root, 'unexpected.txt'), 'SYNTHETIC source marker');
  const badAnchor = inspectConfiguration(first.plan.configPath, options);
  assert.equal(badAnchor.checks.find(item => item.name === 'cloud_anchor').ok, false);
  assert.equal(readFileSync(join(first.plan.root, 'unexpected.txt'), 'utf8'), 'SYNTHETIC source marker');
  rmSync(join(first.plan.root, 'unexpected.txt'));
  for (const auth_cache of [join(first.plan.root, 'auth.json'), join(first.plan.state, 'auth.json'), join(f.repo, 'auth.json'), first.plan.configPath, null]) {
    const config = { ...first.config, graph: { ...first.config.graph, auth_cache } };
    writeFileSync(first.plan.configPath, JSON.stringify(config));
    const result = inspectConfiguration(first.plan.configPath, options);
    assert.equal(result.ok, false);
    assert.equal(result.checks.find(item => item.name === 'cloud_auth_path').ok, false);
    assert.deepEqual(JSON.parse(readFileSync(first.plan.configPath, 'utf8')), config);
  }
  assert.equal(existsSync(first.plan.state), false);
  assert.equal(existsSync(first.plan.authCache), false);
});

test('offline cloud doctor checks credential metadata without reading or modifying cached secrets', async t => {
  const f = fixture(t), first = await runConfigureCloud(argumentsFor(f), runtime(f));
  const options = offlineDoctorRuntime(f);
  writeFileSync(first.plan.authCache, 'SYNTHETIC_PRIVATE_CREDENTIAL_CONTENT', { mode: 0o600 });
  const good = inspectConfiguration(first.plan.configPath, options);
  assert.equal(good.ok, true);
  assert.doesNotMatch(JSON.stringify(good), /SYNTHETIC_PRIVATE_CREDENTIAL_CONTENT/);
  const duplicate = join(f.base, 'duplicate-cache'); linkSync(first.plan.authCache, duplicate);
  assert.equal(inspectConfiguration(first.plan.configPath, options).checks.find(item => item.name === 'cloud_auth_path').ok, false);
  rmSync(duplicate);
  if (process.platform !== 'win32') {
    chmodSync(first.plan.authCache, 0o644);
    assert.equal(inspectConfiguration(first.plan.configPath, options).checks.find(item => item.name === 'cloud_auth_path').ok, false);
    chmodSync(first.plan.authCache, 0o600); chmodSync(first.plan.profile, 0o755);
    assert.equal(inspectConfiguration(first.plan.configPath, options).checks.find(item => item.name === 'cloud_auth_path').ok, false);
  }
  assert.equal(readFileSync(first.plan.authCache, 'utf8'), 'SYNTHETIC_PRIVATE_CREDENTIAL_CONTENT');
  assert.equal(existsSync(first.plan.state), false);
});
