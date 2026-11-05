import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { closeSync, existsSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { createInterface } from 'node:readline/promises';
import { fileURLToPath } from 'node:url';
import { currentNodeExecutable, findExecutable, requireVenv, venvPython } from './installer-utils.mjs';
import { validateRuntimeOptions } from './config-validation.mjs';

export const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const helpText = `Configure df mcp to read one explicitly selected local folder.\n\n  --root <folder>       Source folder (read only; relative paths use the current directory)\n  --state-dir <folder>  Override the application state base; profiles are created below it\n  --yes                Confirm without a prompt; requires explicit --root\n  --help               Show this help\n\nWindows uses a folder picker when no --root is provided; other systems ask for a path.\nNo source directory is scanned and no MCP process is started by this installer.`;

export function parseArguments(argv, { allowSkip = false } = {}) {
  const options = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--root' || arg === '--state-dir') {
      if (options[arg] !== undefined || !argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error(`Supply exactly one value for ${arg}.`);
      options[arg] = argv[++i];
    } else if (arg === '--yes' || arg === '--help' || (arg === '--skip-config' && allowSkip)) {
      if (options[arg]) throw new Error(`Duplicate option: ${arg}`);
      options[arg] = true;
    } else throw new Error(`Unknown argument: ${arg}`);
  }
  if (options['--yes'] && !options['--root']) throw new Error('--yes requires an explicit --root.');
  if (options['--skip-config'] && (options['--root'] || options['--state-dir'] || options['--yes'])) throw new Error('--skip-config cannot be combined with configuration options.');
  return options;
}

export function defaultStateBase({ platform = process.platform, env = process.env, home = homedir() } = {}) {
  if (platform === 'win32') {
    if (!env.LOCALAPPDATA || !isAbsolute(env.LOCALAPPDATA)) throw new Error('LOCALAPPDATA is unavailable. Supply --state-dir explicitly.');
    return join(env.LOCALAPPDATA, 'df-mcp');
  }
  if (platform === 'darwin') return join(home, 'Library', 'Application Support', 'df-mcp');
  if (env.XDG_STATE_HOME && !isAbsolute(env.XDG_STATE_HOME)) throw new Error('XDG_STATE_HOME must be absolute.');
  return join(env.XDG_STATE_HOME || join(home, '.local', 'state'), 'df-mcp');
}

function stripOuterQuotes(value) {
  const trimmed = value.trim();
  return trimmed.length > 1 && ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) ? trimmed.slice(1, -1) : trimmed;
}

export function canonicalFuturePath(path) {
  const missing = [];
  let current = resolve(path);
  while (!existsSync(current)) {
    const parent = dirname(current);
    if (parent === current) throw new Error('Unable to resolve the storage location.');
    missing.unshift(current.slice(parent.length + (parent.endsWith(sep) ? 0 : 1)));
    current = parent;
  }
  return resolve(realpathSync.native(current), ...missing);
}

function contained(parent, child, platform) {
  const normalize = (value) => platform === 'win32' ? value.split(sep).map((part) => part.replace(/[. ]+$/, '').toLowerCase()).join(sep) : value;
  const normalizedParent = normalize(parent);
  const normalizedChild = normalize(child);
  const difference = relative(normalizedParent, normalizedChild);
  return difference === '' || (!difference.startsWith(`..${sep}`) && difference !== '..' && !isAbsolute(difference));
}

function rejectCodexStorage(path, { platform, codexHomes }) {
  const candidates = [resolve(path), canonicalFuturePath(path)];
  for (const candidate of candidates) {
    const parts = candidate.split(sep);
    const insideNamedDirectory = parts.some((part) => (platform === 'win32' ? part.replace(/[. ]+$/, '') : part).toLowerCase() === '.codex');
    if (insideNamedDirectory || codexHomes.some((home) => contained(home, candidate, platform))) {
      throw new Error('State storage must be outside .codex and CODEX_HOME. Choose another --state-dir.');
    }
  }
}

function rejectStorageLinks(path) {
  let current = resolve(path);
  while (true) {
    try {
      if (lstatSync(current).isSymbolicLink()) throw new Error('The state location must not use symbolic links or junctions. Choose another --state-dir.');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const parent = dirname(current);
    if (parent === current) return;
    current = parent;
  }
}

export function validateStatePath(path, { platform = process.platform, env = process.env, home = homedir() } = {}) {
  const codexHome = resolve(env.CODEX_HOME || join(home, '.codex'));
  rejectCodexStorage(path, { platform, codexHomes: [codexHome, canonicalFuturePath(codexHome)] });
  rejectStorageLinks(path);
  return canonicalFuturePath(path);
}

export function makePlan(rootInput, stateInput, { repo = repositoryRoot, platform = process.platform, env = process.env, home = homedir() } = {}) {
  const sourceInput = stripOuterQuotes(rootInput);
  if (!sourceInput || sourceInput.includes('\0')) throw new Error('A non-empty source folder is required.');
  const sourcePath = resolve(sourceInput);
  const sourceStat = lstatSync(sourcePath);
  if (!sourceStat.isDirectory() || sourceStat.isSymbolicLink()) throw new Error('Select an ordinary local directory, not a link or a file.');
  const root = realpathSync.native(sourcePath);
  const installRoot = realpathSync.native(repo);
  const baseInput = resolve(stateInput ? stripOuterQuotes(stateInput) : defaultStateBase({ platform, env, home }));
  const configuredCodexHome = resolve(env.CODEX_HOME || join(home, '.codex'));
  const codexHomes = [configuredCodexHome, canonicalFuturePath(configuredCodexHome)];
  rejectCodexStorage(baseInput, { platform, codexHomes });
  rejectStorageLinks(baseInput);
  const base = canonicalFuturePath(baseInput);
  const key = createHash('sha256').update(platform === 'win32' ? root.toLowerCase() : root).digest('hex').slice(0, 16);
  const profile = join(base, 'profiles', key);
  const state = join(profile, 'state');
  if (contained(root, installRoot, platform) || contained(installRoot, root, platform)) throw new Error('The source folder and df mcp installation must not overlap. Select a dedicated document folder outside the installation.');
  if (contained(root, base, platform) || contained(base, root, platform)) throw new Error('Source and state storage must be separate, non-overlapping folders.');
  if (contained(installRoot, base, platform) || contained(base, installRoot, platform)) throw new Error('State storage and the df mcp installation must not overlap.');
  rejectStorageLinks(profile);
  return { root, sourcePath, base, profile, state, configPath: join(profile, 'config.json'), repo: installRoot, platform, codexHomes };
}

export function preflightRoot(root, { repo = repositoryRoot, platform = process.platform, env = process.env } = {}) {
  const python = requireVenv(repo, platform);
  const result = spawnSync(python, ['-m', 'team_drive.fs_access', '--validate-root', root], {
    cwd: repo, env: { ...env, PYTHONPATH: join(repo, 'python'), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' }, encoding: 'utf8', timeout: 15000, windowsHide: true,
  });
  let response;
  try { response = JSON.parse(result.stdout || '{}'); } catch { /* Report a bounded diagnostic without subprocess traces. */ }
  if (result.error || result.status !== 0 || response?.ok !== true) throw new Error(`Source folder security validation failed (${response?.error?.code || response?.code || 'VALIDATION_FAILED'}). Choose a normal local folder and check that dependencies are installed.`);
}

export function windowsFolderPicker({ env = process.env, runner = spawnSync } = {}) {
  const executable = findExecutable('powershell', { platform: 'win32', env });
  if (!executable) return { unavailable: true };
  const script = `[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description = 'Select the folder df mcp may read (read only)'; $dialog.ShowNewFolderButton = $false; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { @{ selected = $dialog.SelectedPath } | ConvertTo-Json -Compress } else { @{ cancelled = $true } | ConvertTo-Json -Compress }; $dialog.Dispose()`;
  const result = runner(executable, ['-NoProfile', '-STA', '-Command', script], { env, encoding: 'utf8', windowsHide: true });
  if (result.error || result.status !== 0) return { unavailable: true };
  try { return JSON.parse(result.stdout.trim().replace(/^\uFEFF/, '')); } catch { return { unavailable: true }; }
}

function configuredPath(value, configPath) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const expanded = value === '~' ? homedir() : value.startsWith(`~${sep}`) ? join(homedir(), value.slice(2)) : value;
  return resolve(dirname(configPath), expanded);
}

function sameSourceAndState(existing, plan) {
  if (!existing || typeof existing !== 'object' || Array.isArray(existing) || (existing.provider ?? 'local') !== 'local') return false;
  const expand = (value) => {
    const path = configuredPath(value, plan.configPath);
    return path === null ? null : canonicalFuturePath(path);
  };
  const root = expand(existing.root), state = expand(existing.state_dir);
  const same = (left, right) => left !== null && contained(left, right, plan.platform) && contained(right, left, plan.platform);
  return same(root, plan.root) && same(state, plan.state);
}

export function writeConfiguration(plan) {
  const config = { provider: 'local', root: plan.root, state_dir: plan.state, poll_seconds: 30 };
  rejectCodexStorage(plan.profile, plan);
  rejectStorageLinks(plan.profile);
  if (existsSync(plan.configPath)) {
    const info = lstatSync(plan.configPath);
    if (info.isSymbolicLink()) throw new Error('Refusing to replace a linked configuration file.');
    if (!info.isFile() || info.size > 262144) throw new Error('Existing configuration must be an ordinary JSON file no larger than 256 KiB. It was not changed.');
    let existing;
    try { existing = JSON.parse(readFileSync(plan.configPath, 'utf8')); } catch { throw new Error('An existing configuration cannot be read. It was not changed.'); }
    if (!sameSourceAndState(existing, plan)) throw new Error('This profile is bound to a different source or state location. Nothing was overwritten. Confirm the intended source with configure; a new source receives its own profile.');
    const source = configuredPath(existing.root, plan.configPath);
    if (!lstatSync(source).isDirectory() || lstatSync(source).isSymbolicLink()) throw new Error('The existing configured source must be an ordinary directory, not a linked root. Nothing was changed.');
    if (plan.platform === 'win32') rejectStorageLinks(source);
    const state = configuredPath(existing.state_dir, plan.configPath);
    rejectCodexStorage(state, plan);
    rejectStorageLinks(state);
    validateRuntimeOptions(existing);
    // Preserve formatting, key order and all user-selected runtime options byte for byte.
    return { config: existing, reused: true };
  }
  mkdirSync(plan.profile, { recursive: true, mode: 0o700 });
  rejectStorageLinks(plan.profile);
  const handle = openSync(plan.configPath, 'wx', 0o600);
  try { writeFileSync(handle, `${JSON.stringify(config, null, 2)}\n`, 'utf8'); } finally { closeSync(handle); }
  return { config, reused: false };
}

export function shellQuote(value, platform = process.platform) {
  return platform === 'win32' ? `'${value.replaceAll("'", "''")}'` : `'${value.replaceAll("'", "'\\''")}'`;
}

export async function runConfigure(argv = [], runtime = {}) {
  const options = parseArguments(argv);
  const platform = runtime.platform || process.platform;
  const output = runtime.output || process.stdout;
  const input = runtime.input || process.stdin;
  const log = (message) => output.write(`${message}\n`);
  if (options['--help']) { log(helpText); return { help: true }; }
  const interactive = runtime.interactive ?? Boolean(input.isTTY && output.isTTY);
  if (!interactive && !options['--root']) throw new Error('Non-interactive configuration requires --root <folder> --yes. No default source folder is selected.');
  if (!interactive && !options['--yes']) throw new Error('Non-interactive confirmation requires --yes together with an explicit --root.');
  let reader;
  const ask = runtime.ask || (async (question) => {
    reader ||= createInterface({ input, output });
    return reader.question(question);
  });
  try {
    let source = options['--root'];
    if (!source && platform === 'win32') {
      const picked = await (runtime.pickFolder || windowsFolderPicker)({ env: runtime.env || process.env });
      if (picked.cancelled) { log('Cancelled. No configuration or index was created.'); return { cancelled: true }; }
      source = picked.selected;
      if (!source) log('Folder picker unavailable. Paste the folder path instead.');
    }
    if (!source) source = await ask('Source folder (read only; blank cancels): ');
    if (!source.trim()) { log('Cancelled. No configuration or index was created.'); return { cancelled: true }; }
    const plan = makePlan(source, options['--state-dir'], { repo: runtime.repo || repositoryRoot, platform, env: runtime.env || process.env, home: runtime.home || homedir() });
    const validate = runtime.validateRoot || preflightRoot;
    await validate(plan.sourcePath, { repo: plan.repo, platform, env: runtime.env || process.env });
    log(`Source folder (READ ONLY): ${plan.root}\nPrivate state/index directory: ${plan.state}\nConfiguration: ${plan.configPath}\nSource files will not be created, edited, renamed, or deleted. No source ACLs are changed.`);
    if (!options['--yes']) {
      const answer = await ask('Save this configuration? [y/N]: ');
      if (!/^(y|yes)$/i.test(answer.trim())) { log('Cancelled. No configuration or index was created.'); return { cancelled: true }; }
    }
    // Revalidate after the user confirms, before creating any storage.
    const fresh = makePlan(source, options['--state-dir'], { repo: runtime.repo || repositoryRoot, platform, env: runtime.env || process.env, home: runtime.home || homedir() });
    if (fresh.root !== plan.root || fresh.configPath !== plan.configPath) throw new Error('A selected path changed during configuration. Nothing was written; try again.');
    await validate(plan.sourcePath, { repo: plan.repo, platform, env: runtime.env || process.env });
    const result = writeConfiguration(plan);
    log(`${result.reused ? 'Existing configuration reused; runtime options and formatting were preserved.' : 'Configuration saved.'} Indexing starts only when the MCP client starts the server.\nRegister in Codex (PowerShell on Windows):\ncodex mcp add df_mcp -- ${shellQuote(currentNodeExecutable({ platform, env: runtime.env || process.env }), platform)} ${shellQuote(join(plan.repo, 'dist', 'index.js'), platform)} --config ${shellQuote(plan.configPath, platform)} --python ${shellQuote(venvPython(plan.repo, platform), platform)}\nThe command pins this installation's Node and Python paths, so desktop PATH differences or inherited Python overrides cannot select another runtime.\nInstall the skill separately with: npm run install:skill\nConcurrent MCP connections share one managed local index. Restart existing chats after changing the connection configuration.`);
    return { ...result, plan };
  } finally { reader?.close(); }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runConfigure(process.argv.slice(2)).catch((error) => { console.error(error.message); process.exitCode = 1; });
}
