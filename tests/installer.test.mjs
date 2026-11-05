import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import test from 'node:test';
import { defaultStateBase, makePlan, parseArguments, preflightRoot, repositoryRoot, runConfigure, shellQuote, windowsFolderPicker } from '../scripts/configure.mjs';
import { currentNodeExecutable, discoverPython, venvPython } from '../scripts/installer-utils.mjs';
import { installSkill } from '../scripts/install-skill.mjs';

function fixture(t) {
  const base = realpathSync.native(mkdtempSync(join(tmpdir(), 'df-mcp-install-')));
  const repo = join(base, 'installation');
  const source = join(base, '资料 空格');
  const state = join(base, 'private state');
  mkdirSync(repo); mkdirSync(source);
  writeFileSync(join(source, '预算.txt'), 'read-only source evidence\n');
  t.after(() => { try { chmodSync(source, 0o700); chmodSync(join(source, '预算.txt'), 0o600); } catch {} rmSync(base, { recursive: true, force: true }); });
  return { base, repo, source, state };
}
function runtime(f, extra = {}) {
  let output = '';
  return { repo: f.repo, interactive: false, validateRoot: () => {}, output: { write: (text) => { output += text; } }, getOutput: () => output, ...extra };
}
function args(f) { return ['--root', f.source, '--state-dir', f.state, '--yes']; }
function fakeSkill(repo) {
  mkdirSync(join(repo, 'skills', 'df-mcp'), { recursive: true });
  writeFileSync(join(repo, 'skills', 'df-mcp', 'SKILL.md'), '---\nname: df-mcp\ndescription: Fixture.\n---\nRead only.\n');
}

test('explicit source with Chinese/spaces creates only private configuration, never indexes or modifies source', async (t) => {
  const f = fixture(t);
  const original = readFileSync(join(f.source, '预算.txt'));
  const r = runtime(f);
  const result = await runConfigure(args(f), r);
  assert.equal(result.plan.root, f.source);
  assert.equal(result.config.state_dir, result.plan.state);
  assert.ok(existsSync(result.plan.configPath));
  assert.equal(existsSync(result.plan.state), false);
  assert.deepEqual(readFileSync(join(f.source, '预算.txt')), original);
  assert.match(r.getOutput(), /READ ONLY/);
  assert.match(r.getOutput(), /codex mcp add df_mcp/);
  assert.match(r.getOutput(), /share one managed local index/);
  assert.doesNotMatch(r.getOutput(), /Do not start two MCP connections/);
  assert.ok(r.getOutput().includes(`-- ${shellQuote(currentNodeExecutable())} ${shellQuote(join(f.repo, 'dist', 'index.js'))}`));
  assert.ok(r.getOutput().includes(`--python ${shellQuote(venvPython(f.repo))}`));
});

test('blank manual selection cancels without configuration or validation', async (t) => {
  const f = fixture(t);
  const result = await runConfigure(['--state-dir', f.state], runtime(f, { interactive: true, pickFolder: () => ({ unavailable: true }), ask: async () => '', validateRoot: () => assert.fail('must not validate') }));
  assert.equal(result.cancelled, true); assert.equal(existsSync(f.state), false);
});

test('Windows picker cancellation does not fall back to another folder', async (t) => {
  const f = fixture(t);
  const result = await runConfigure(['--state-dir', f.state], runtime(f, { interactive: true, platform: 'win32', pickFolder: () => ({ cancelled: true }), ask: () => assert.fail('must not prompt') }));
  assert.equal(result.cancelled, true); assert.equal(existsSync(f.state), false);
});

test('Windows picker unavailable allows manual Unicode path and still requires confirmation', async (t) => {
  const f = fixture(t); const answers = [f.source, 'y'];
  const result = await runConfigure(['--state-dir', f.state], runtime(f, { interactive: true, platform: 'win32', pickFolder: () => ({ unavailable: true }), ask: async () => answers.shift() }));
  assert.equal(result.plan.root, f.source); assert.equal(answers.length, 0);
});

test('declining confirmation and failed security validation write no state', async (t) => {
  const f = fixture(t);
  const cancelled = await runConfigure(['--root', f.source, '--state-dir', f.state], runtime(f, { interactive: true, ask: async () => 'n' }));
  assert.equal(cancelled.cancelled, true); assert.equal(existsSync(f.state), false);
  await assert.rejects(runConfigure(args(f), runtime(f, { validateRoot: () => { throw new Error('UNSAFE_ROOT'); } })), /UNSAFE_ROOT/);
  assert.equal(existsSync(f.state), false);
});

test('non-interactive configuration cannot silently select or confirm a source', async (t) => {
  const f = fixture(t);
  await assert.rejects(runConfigure([], runtime(f)), /requires --root/);
  await assert.rejects(runConfigure(['--root', f.source], runtime(f)), /requires --yes/);
  assert.throws(() => parseArguments(['--yes']), /explicit --root/);
  assert.equal(existsSync(f.state), false);
});

test('source must not contain installation or overlap state in either direction', (t) => {
  const f = fixture(t);
  assert.throws(() => makePlan(f.base, f.state, { repo: f.repo }), /installation/);
  const inside = join(f.repo, 'documents'); mkdirSync(inside);
  assert.throws(() => makePlan(inside, f.state, { repo: f.repo }), /installation must not overlap/);
  assert.throws(() => makePlan(f.source, join(f.source, 'cache'), { repo: f.repo }), /non-overlapping/);
  assert.throws(() => makePlan(f.source, f.base, { repo: f.repo }), /non-overlapping/);
  assert.throws(() => makePlan(f.source, join(f.repo, 'cache'), { repo: f.repo }), /installation must not overlap/);
  assert.equal(existsSync(f.state), false);
});

test('linked source and linked state paths are rejected', (t) => {
  const f = fixture(t); const linked = join(f.base, 'linked');
  try { symlinkSync(f.source, linked, process.platform === 'win32' ? 'junction' : 'dir'); } catch (error) { if (error.code === 'EPERM') { t.skip('Symlink privilege unavailable'); return; } throw error; }
  assert.throws(() => makePlan(linked, f.state, { repo: f.repo }), /ordinary local directory/);
  assert.throws(() => makePlan(f.source, join(linked, 'cache'), { repo: f.repo }), /symbolic links or junctions/);
});

test('state below any .codex ancestor is rejected before validation or filesystem writes', async (t) => {
  const f = fixture(t);
  for (const named of ['.codex', '.CODEX', '.codex.', '.codex ']) {
    const codex = join(f.base, 'nested', named);
    const state = join(codex, 'df-mcp', 'state');
    await assert.rejects(runConfigure(['--root', f.source, '--state-dir', state, '--yes'], runtime(f, {
      platform: 'win32', env: {}, home: f.base,
      validateRoot: () => assert.fail('must reject state before source preflight'),
    })), /outside .codex and CODEX_HOME/);
    assert.equal(existsSync(codex), false);
  }
  assert.throws(() => makePlan(f.source, join(f.base, '.CODEX', 'state'), { repo: f.repo, env: {}, home: f.base }), /outside .codex and CODEX_HOME/);
});

test('custom CODEX_HOME and a state default inside it are rejected before writes', async (t) => {
  const f = fixture(t);
  const codexHome = join(f.base, 'custom-client-data');
  const r = runtime(f, { platform: 'win32', env: { CODEX_HOME: codexHome, LOCALAPPDATA: codexHome }, home: f.base });
  await assert.rejects(runConfigure(['--root', f.source, '--state-dir', codexHome, '--yes'], r), /outside .codex and CODEX_HOME/);
  await assert.rejects(runConfigure(['--root', f.source, '--state-dir', `${codexHome}.`, '--yes'], r), /outside .codex and CODEX_HOME/);
  await assert.rejects(runConfigure(['--root', f.source, '--yes'], r), /outside .codex and CODEX_HOME/);
  assert.equal(existsSync(codexHome), false);
  const allowed = makePlan(f.source, f.state, { repo: f.repo, env: { CODEX_HOME: codexHome }, home: f.base });
  assert.equal(allowed.base, f.state);
});

test('a physical alias of CODEX_HOME cannot conceal state inside Codex storage', (t) => {
  const f = fixture(t);
  const actual = join(f.base, 'client-data');
  const codexHome = join(f.base, 'codex-alias');
  mkdirSync(actual);
  try { symlinkSync(actual, codexHome, process.platform === 'win32' ? 'junction' : 'dir'); } catch (error) { if (error.code === 'EPERM') { t.skip('Symlink privilege unavailable'); return; } throw error; }
  const state = join(actual, 'df-mcp');
  assert.throws(() => makePlan(f.source, state, { repo: f.repo, env: { CODEX_HOME: codexHome }, home: f.base }), /outside .codex and CODEX_HOME/);
  assert.equal(existsSync(state), false);
});

test('a linked .codex directory cannot conceal an existing Codex state tree', (t) => {
  const f = fixture(t);
  const codexHome = join(f.base, '.codex');
  const alias = join(f.base, 'external-looking-state');
  mkdirSync(codexHome);
  try { symlinkSync(codexHome, alias, process.platform === 'win32' ? 'junction' : 'dir'); } catch (error) { if (error.code === 'EPERM') { t.skip('Symlink privilege unavailable'); return; } throw error; }
  const state = join(alias, 'df-mcp');
  assert.throws(() => makePlan(f.source, state, { repo: f.repo, env: {}, home: f.base }), /outside .codex and CODEX_HOME/);
  assert.equal(existsSync(join(codexHome, 'df-mcp')), false);
});

test('equivalent profile preserves key order and custom runtime options without rewriting', async (t) => {
  const f = fixture(t); const first = await runConfigure(args(f), runtime(f));
  assert.equal((await runConfigure(args(f), runtime(f))).reused, true);
  const changed = { aliases: { '花费': ['支出'] }, state_pool: { max_slots: 8 }, poll_seconds: 60, state_dir: './state', root: f.source, provider: 'local' };
  const previous = `${JSON.stringify(changed)}\n`; writeFileSync(first.plan.configPath, previous);
  const reused = await runConfigure(args(f), runtime(f));
  assert.equal(reused.reused, true);
  assert.deepEqual(reused.config, changed);
  assert.equal(readFileSync(first.plan.configPath, 'utf8'), previous);
});

test('existing profile with a different source or state is never rebound or overwritten', async (t) => {
  const f = fixture(t); const first = await runConfigure(args(f), runtime(f));
  const other = join(f.base, 'other'); mkdirSync(other);
  for (const change of [{ root: other }, { state_dir: other }]) {
    const previous = `${JSON.stringify({ ...first.config, ...change })}\n`;
    writeFileSync(first.plan.configPath, previous);
    await assert.rejects(runConfigure(args(f), runtime(f)), /different source or state/);
    assert.equal(readFileSync(first.plan.configPath, 'utf8'), previous);
  }
});

test('reuse rejects invalid runtime options without overwriting the existing profile', async (t) => {
  const f = fixture(t), first = await runConfigure(args(f), runtime(f));
  const bad = [{ poll_seconds: 86401 }, { aliases: { word: 'not-a-list' } }, { limits: { max_pdf_pages: 0 } }, { limits: { unknown_limit: 1 } }, { service: { max_clients: 0 } }, { service: JSON.parse('{"__proto__":1}') }];
  for (const change of bad) {
    const previous = JSON.stringify({ ...first.config, ...change }) + '\n';
    writeFileSync(first.plan.configPath, previous);
    await assert.rejects(runConfigure(args(f), runtime(f)), /poll_seconds|aliases|limits|service setting/);
    assert.equal(readFileSync(first.plan.configPath, 'utf8'), previous);
    assert.equal(existsSync(first.plan.state), false);
  }
});

test('reuse bounds the configuration before parsing it or accepting a non-file', async (t) => {
  const f = fixture(t), first = await runConfigure(args(f), runtime(f));
  writeFileSync(first.plan.configPath, ' '.repeat(262145));
  await assert.rejects(runConfigure(args(f), runtime(f)), /no larger than 256 KiB/);
  assert.equal(readFileSync(first.plan.configPath).length, 262145);
  rmSync(first.plan.configPath); mkdirSync(first.plan.configPath);
  await assert.rejects(runConfigure(args(f), runtime(f)), /ordinary JSON file/);
});

test('equivalent profile cannot hide an unsupported linked source root', async (t) => {
  const f = fixture(t), first = await runConfigure(args(f), runtime(f));
  const alias = join(f.base, 'source-alias');
  try { symlinkSync(f.source, alias, process.platform === 'win32' ? 'junction' : 'dir'); } catch (error) { if (error.code === 'EPERM') { t.skip('Symlink privilege unavailable'); return; } throw error; }
  const previous = JSON.stringify({ ...first.config, root: alias }); writeFileSync(first.plan.configPath, previous);
  await assert.rejects(runConfigure(args(f), runtime(f)), /linked root/);
  assert.equal(readFileSync(first.plan.configPath, 'utf8'), previous);
});

test('changing folders produces a distinct profile without replacing an earlier one', async (t) => {
  const f = fixture(t); const first = await runConfigure(args(f), runtime(f));
  const other = join(f.base, 'other source'); mkdirSync(other);
  const second = await runConfigure(['--root', other, '--state-dir', f.state, '--yes'], runtime(f));
  assert.notEqual(first.plan.configPath, second.plan.configPath);
  assert.equal(JSON.parse(readFileSync(first.plan.configPath)).root, f.source);
});

test('read-only source remains usable without test writes or permission changes', async (t) => {
  const f = fixture(t); const file = join(f.source, '预算.txt');
  chmodSync(file, 0o444); if (process.platform !== 'win32') chmodSync(f.source, 0o555);
  t.after(() => { try { chmodSync(file, 0o600); } catch {} });
  const before = readFileSync(file);
  await runConfigure(args(f), runtime(f));
  assert.deepEqual(readFileSync(file), before);
});

test('source security validation repeats after confirmation before any configuration is written', async (t) => {
  const f = fixture(t); let checks = 0;
  await assert.rejects(runConfigure(args(f), runtime(f, { validateRoot: () => { if (++checks === 2) throw new Error('SOURCE_ROOT_CHANGED'); } })), /SOURCE_ROOT_CHANGED/);
  assert.equal(checks, 2); assert.equal(existsSync(f.state), false);
});

test('arguments distinguish setup build-only from configuration, rejecting ambiguous flags', () => {
  assert.equal(parseArguments(['--skip-config'], { allowSkip: true })['--skip-config'], true);
  assert.throws(() => parseArguments(['--skip-config']), /Unknown argument/);
  assert.throws(() => parseArguments(['--skip-config', '--root', 'source'], { allowSkip: true }), /cannot be combined/);
  assert.throws(() => parseArguments(['--root']), /exactly one value/);
  assert.throws(() => parseArguments(['--root', 'a', '--root', 'b']), /exactly one value/);
  assert.throws(() => parseArguments(['--force']), /Unknown argument/);
});

test('platform state defaults and shell quoting preserve literal paths', () => {
  const home = process.platform === 'win32' ? 'C:\\Example Home' : '/example-home';
  assert.equal(defaultStateBase({ platform: 'darwin', env: {}, home }), join(home, 'Library', 'Application Support', 'df-mcp'));
  assert.equal(defaultStateBase({ platform: 'linux', env: {}, home }), join(home, '.local', 'state', 'df-mcp'));
  assert.throws(() => defaultStateBase({ platform: 'win32', env: {}, home }), /LOCALAPPDATA/);
  assert.equal(shellQuote("D:\\资料 O'Brien $x", 'win32'), "'D:\\资料 O''Brien $x'");
  assert.equal(shellQuote("a'b", 'linux'), "'a'\\''b'");
});

test('Windows Python discovery uses py -3, skips Store aliases, and never uses shell concatenation', (t) => {
  const f = fixture(t); const tools = join(f.base, 'tools'); const store = join(f.base, 'WindowsApps'); mkdirSync(tools); mkdirSync(store);
  writeFileSync(join(tools, 'py.exe'), ''); writeFileSync(join(store, 'python.exe'), '');
  const calls = [];
  const python = discoverPython({ platform: 'win32', env: { PATH: `${store};${tools}` }, runner: (command, argv, options) => { calls.push({ command, argv, options }); return { status: 0 }; } });
  assert.equal(python.command, join(tools, 'py.exe')); assert.deepEqual(python.args, ['-3']);
  assert.equal(calls[0].argv[0], '-3'); assert.equal(calls[0].options.shell, undefined);
  assert.equal(calls[0].options.env.PYTHONUTF8, '1');
});

test('registration keeps a stable absolute Node entry only for the currently running executable', (t) => {
  const f = fixture(t), bin = join(f.base, 'bin'); mkdirSync(bin);
  const name = process.platform === 'win32' ? 'node.exe' : 'node';
  const running = join(f.repo, name), other = join(bin, name);
  writeFileSync(running, 'running-node'); writeFileSync(other, 'other-node');
  chmodSync(running, 0o700); chmodSync(other, 0o700);
  assert.equal(currentNodeExecutable({ env: { PATH: bin }, execPath: running }), running);
  rmSync(other);
  try { symlinkSync(running, other, 'file'); } catch (error) { if (error.code === 'EPERM') { t.skip('File symlink privilege unavailable'); return; } throw error; }
  assert.equal(currentNodeExecutable({ env: { PATH: bin }, execPath: running }), other);
});

test('Windows folder picker uses a fixed command, disables new folders, and preserves Unicode', (t) => {
  const f = fixture(t); writeFileSync(join(f.repo, 'powershell.exe'), '');
  const selected = windowsFolderPicker({ env: { PATH: f.repo }, runner: (command, argv, options) => {
    assert.equal(command, join(f.repo, 'powershell.exe')); assert.equal(options.shell, undefined);
    assert.match(argv.at(-1), /ShowNewFolderButton = \$false/);
    return { status: 0, stdout: JSON.stringify({ selected: f.source }) };
  } });
  assert.equal(selected.selected, f.source);
});

test('skill installs under df-mcp and existing target requires explicit replacement', (t) => {
  const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, 'codex');
  const first = installSkill([], { repo: f.repo, codexHome: home, log: () => {} });
  assert.equal(first.target, join(home, 'skills', 'df-mcp'));
  writeFileSync(join(first.target, 'personal-note.md'), 'local customization');
  assert.throws(() => installSkill([], { repo: f.repo, codexHome: home, log: () => {} }), /Nothing was changed/);
  assert.equal(readFileSync(join(first.target, 'personal-note.md'), 'utf8'), 'local customization');
});

test('explicit skill migration backs up both names outside the skills directory', (t) => {
  const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, 'codex');
  for (const name of ['team-drive-read', 'df-mcp']) { mkdirSync(join(home, 'skills', name), { recursive: true }); writeFileSync(join(home, 'skills', name, 'custom.md'), `custom ${name}`); }
  assert.throws(() => installSkill([], { repo: f.repo, codexHome: home, log: () => {} }), /--replace/);
  const result = installSkill(['--replace'], { repo: f.repo, codexHome: home, log: () => {} });
  assert.equal(result.backups.length, 2);
  assert.equal(existsSync(join(home, 'skills', 'team-drive-read')), false);
  for (const backup of result.backups) { assert.ok(relative(join(home, 'skills'), backup).startsWith('..')); assert.match(readFileSync(join(backup, 'custom.md'), 'utf8'), /^custom /); }
  assert.ok(existsSync(join(home, 'skills', 'df-mcp', 'SKILL.md')));
});

test('real skill CLI copies every package file to a Unicode home on the temporary volume', (t) => {
  const f = fixture(t);
  const home = join(f.base, '模拟用户 home', '.codex');
  const result = spawnSync(process.execPath, [join(repositoryRoot, 'scripts', 'install-skill.mjs')], {
    cwd: repositoryRoot, env: { ...process.env, CODEX_HOME: home }, encoding: 'utf8', timeout: 15000, windowsHide: true
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  function compareTree(source, target) {
    assert.deepEqual(fs.readdirSync(target).sort(), fs.readdirSync(source).sort());
    for (const name of fs.readdirSync(source)) {
      const input = join(source, name), output = join(target, name);
      if (fs.lstatSync(input).isDirectory()) compareTree(input, output);
      else assert.deepEqual(readFileSync(output), readFileSync(input), name);
    }
  }
  // In Windows CI the checkout is on D: and tmpdir on C:, covering the actual
  // cross-volume source + Unicode destination that same-fixture copies missed.
  compareTree(join(repositoryRoot, 'skills', 'df-mcp'), join(home, 'skills', 'df-mcp'));
  assert.deepEqual(fs.readdirSync(join(home, 'skill-backups')), []);
});

for (const failure of ['missing file', 'corrupted file', 'missing staging directory']) {
  test(`skill staging detects ${failure} before moving an existing installation`, (t) => {
    const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, '模拟用户 home', '.codex');
    const target = join(home, 'skills', 'df-mcp');
    mkdirSync(target, { recursive: true }); writeFileSync(join(target, 'custom.md'), 'preserve this customization');
    const copy = fs.copyFileSync;
    t.mock.method(fs, 'copyFileSync', (source, destination, flags) => {
      if (failure === 'missing file') return;
      copy(source, destination, flags);
      if (failure === 'corrupted file') writeFileSync(destination, 'invalid copy');
      else rmSync(join(destination, '..'), { recursive: true });
    });
    assert.throws(() => installSkill(['--replace'], { repo: f.repo, codexHome: home, log: () => {} }), (error) => {
      assert.match(error.message, /failed during verify staged package/);
      const details = JSON.parse(error.message.split('\nDiagnostics: ')[1]);
      assert.equal(details.phase, 'verify staged package');
      assert.equal(details.expected.entries, 2);
      assert.match(details.expected.sha256, /^[a-f0-9]{64}$/);
      assert.equal(details.staged.exists, failure !== 'missing staging directory');
      if (details.staged.exists) assert.notEqual(details.staged.manifest.sha256, details.expected.sha256);
      assert.equal(details.parent.directory, true);
      assert.equal(details.target.directory, true);
      return true;
    });
    assert.equal(readFileSync(join(target, 'custom.md'), 'utf8'), 'preserve this customization');
    assert.deepEqual(fs.readdirSync(join(home, 'skill-backups')), []);
  });
}

test('skill publication failure restores both old skill names with useful diagnostics', (t) => {
  const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, '模拟用户 home', '.codex');
  for (const name of ['team-drive-read', 'df-mcp']) {
    mkdirSync(join(home, 'skills', name), { recursive: true }); writeFileSync(join(home, 'skills', name, 'custom.md'), `old ${name}`);
  }
  const rename = fs.renameSync;
  t.mock.method(fs, 'renameSync', (source, destination) => {
    if (source.endsWith('incoming')) throw Object.assign(new Error('simulated rename failure'), { code: 'EBUSY' });
    rename(source, destination);
  });
  assert.throws(() => installSkill(['--replace'], { repo: f.repo, codexHome: home, log: () => {} }), (error) => {
    assert.match(error.message, /failed during publish staged package: simulated rename failure/);
    const details = JSON.parse(error.message.split('\nDiagnostics: ')[1]);
    assert.equal(details.parent.directory, true);
    assert.deepEqual(details.staged.manifest, details.expected);
    assert.equal(details.target.exists, false);
    return true;
  });
  for (const name of ['team-drive-read', 'df-mcp']) assert.equal(readFileSync(join(home, 'skills', name, 'custom.md'), 'utf8'), `old ${name}`);
  assert.deepEqual(fs.readdirSync(join(home, 'skill-backups')), []);
});

test('skill rollback failure preserves the backup and reports its location', (t) => {
  const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, 'codex');
  const target = join(home, 'skills', 'df-mcp');
  mkdirSync(target, { recursive: true }); writeFileSync(join(target, 'custom.md'), 'recoverable customization');
  const rename = fs.renameSync;
  t.mock.method(fs, 'renameSync', (source, destination) => {
    if (destination === target) throw new Error('simulated publication/restore failure');
    rename(source, destination);
  });
  let failure;
  assert.throws(() => installSkill(['--replace'], { repo: f.repo, codexHome: home, log: () => {} }), (error) => {
    failure = error.message;
    assert.match(failure, /Recovery needs attention/);
    return true;
  });
  const backups = fs.readdirSync(join(home, 'skill-backups'));
  assert.equal(backups.length, 1);
  assert.ok(failure.includes(join(home, 'skill-backups', backups[0], 'df-mcp')));
  assert.equal(readFileSync(join(home, 'skill-backups', backups[0], 'df-mcp', 'custom.md'), 'utf8'), 'recoverable customization');
  assert.equal(existsSync(target), false);
});

test('failure backing up the second old skill restores the first one', (t) => {
  const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, 'codex');
  for (const name of ['df-mcp', 'team-drive-read']) {
    mkdirSync(join(home, 'skills', name), { recursive: true }); writeFileSync(join(home, 'skills', name, 'custom.md'), `old ${name}`);
  }
  const rename = fs.renameSync;
  t.mock.method(fs, 'renameSync', (source, destination) => {
    if (source === join(home, 'skills', 'team-drive-read')) throw new Error('simulated second backup failure');
    rename(source, destination);
  });
  assert.throws(() => installSkill(['--replace'], { repo: f.repo, codexHome: home, log: () => {} }), /failed during back up previous skill/);
  for (const name of ['df-mcp', 'team-drive-read']) assert.equal(readFileSync(join(home, 'skills', name, 'custom.md'), 'utf8'), `old ${name}`);
  assert.deepEqual(fs.readdirSync(join(home, 'skill-backups')), []);
});

test('a concurrently occupied restore destination and the old backup are both preserved', (t) => {
  const f = fixture(t); fakeSkill(f.repo); const home = join(f.base, 'codex');
  const target = join(home, 'skills', 'df-mcp');
  mkdirSync(target, { recursive: true }); writeFileSync(join(target, 'custom.md'), 'old customization');
  const rename = fs.renameSync;
  t.mock.method(fs, 'renameSync', (source, destination) => {
    if (source.endsWith('incoming')) {
      mkdirSync(destination); writeFileSync(join(destination, 'concurrent.md'), 'another installer owns this');
      throw new Error('simulated occupied target');
    }
    rename(source, destination);
  });
  assert.throws(() => installSkill(['--replace'], { repo: f.repo, codexHome: home, log: () => {} }), /Restore destination is occupied/);
  assert.equal(readFileSync(join(target, 'concurrent.md'), 'utf8'), 'another installer owns this');
  const backups = fs.readdirSync(join(home, 'skill-backups'));
  assert.equal(backups.length, 1);
  assert.equal(readFileSync(join(home, 'skill-backups', backups[0], 'df-mcp', 'custom.md'), 'utf8'), 'old customization');
});


test('real Python preflight verifies a selected local folder without indexing or source writes', async (t) => {
  const f = fixture(t); const original = readFileSync(join(f.source, '预算.txt'));
  const result = await runConfigure(args(f), runtime(f, { repo: repositoryRoot, validateRoot: preflightRoot }));
  assert.equal(existsSync(result.plan.state), false);
  assert.deepEqual(readFileSync(join(f.source, '预算.txt')), original);
});
