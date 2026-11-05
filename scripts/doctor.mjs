// Offline diagnostics: no worker, socket, source scan, lock, or directory creation.
import { existsSync, lstatSync, opendirSync, readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { validateStatePath } from './configure.mjs';
import { validateRuntimeOptions } from './config-validation.mjs';
import { resolvePythonExecutable } from './installer-utils.mjs';

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const help = 'Usage: node scripts/doctor.mjs --config <config.json>\nRead-only local setup inspection. Does not start a worker, scan source files, connect, repair or clean state.';

export function parseDoctorArguments(argv) {
  if (argv.length === 1 && argv[0] === '--help') return { help: true };
  if (argv.length !== 2 || argv[0] !== '--config' || !argv[1] || argv[1].startsWith('--')) throw new Error(help);
  return { config: resolve(argv[1]) };
}

export function inspectConfiguration(configInput, { repo = repositoryRoot, env = process.env, platform = process.platform, home = homedir() } = {}) {
  const checks = [], warnings = [];
  const configPath = resolve(configInput);
  const check = (name, operation) => {
    try { const value = operation(); checks.push({ name, ok: true }); return value; }
    catch (error) { checks.push({ name, ok: false, message: error.message }); return undefined; }
  };
  const config = check('configuration', () => {
    const info = lstatSync(configPath);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 262144) throw new Error('Use an ordinary JSON configuration file no larger than 256 KiB.');
    const value = JSON.parse(readFileSync(configPath, 'utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value) || !['local', 'microsoft_graph'].includes(value.provider ?? 'local')) throw new Error('Use a local or microsoft_graph source configuration.');
    for (const name of ['root', 'state_dir']) if (typeof value[name] !== 'string' || !value[name].trim()) throw new Error(`${name} must be a nonempty path.`);
    return value;
  });
  const expand = value => resolve(dirname(configPath), value === '~' ? home : value.startsWith(`~${sep}`) ? join(home, value.slice(2)) : value);
  const inside = (parent, child) => {
    const normalized = value => platform === 'win32' ? value.toLowerCase() : value;
    const rel = relative(normalized(parent), normalized(child));
    return rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel));
  };
  const source = config && expand(config.root);
  const state = config && expand(config.state_dir);
  if (config) {
    check('source_directory', () => {
      const info = lstatSync(source);
      if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('The configured source must be an ordinary existing directory.');
    });
    check('state_location', () => {
      const canonical = validateStatePath(state, { env, platform, home });
      const canonicalSource = realpathSync.native(source);
      if (inside(canonicalSource, canonical) || inside(canonical, canonicalSource)) throw new Error('Source and state must not overlap.');
    });
    check('runtime_options', () => validateRuntimeOptions(config));
    if (config.provider === 'microsoft_graph') {
      check('cloud_anchor', () => {
        validateStatePath(source, { env, platform, home });
        // Only inspect the first local anchor entry; never enumerate cloud files.
        const directory = opendirSync(source, { bufferSize: 1 });
        try { if (directory.readSync() !== null) throw new Error('The cloud anchor must be empty; it is not a document directory.'); }
        finally { directory.closeSync(); }
      });
      if (config.graph && Object.hasOwn(config.graph, 'auth_cache')) check('cloud_auth_path', () => {
        const value = config.graph.auth_cache;
        if (typeof value !== 'string' || !value.trim()) throw new Error('auth_cache must be an explicit private local file path.');
        const cache = validateStatePath(expand(value), { env, platform, home });
        const boundaries = [validateStatePath(source, { env, platform, home }), validateStatePath(state, { env, platform, home }), realpathSync.native(repo)];
        const overlaps = (left, right) => inside(left, right) || inside(right, left);
        if (boundaries.some(parent => overlaps(parent, cache)) || inside(cache, configPath) && inside(configPath, cache)) throw new Error('Cloud credentials must be separate from the anchor, index, installation and config file.');
        try {
          const info = lstatSync(cache);
          if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error('Cloud authentication cache must be an ordinary file with one hard link.');
          if (platform !== 'win32' && ((info.mode & 0o777) !== 0o600 || (typeof process.getuid === 'function' && info.uid !== process.getuid()))) throw new Error('Cloud authentication cache must belong to this user and have mode 0600.');
        } catch (error) { if (error.code !== 'ENOENT') throw error; }
        try {
          const parent = lstatSync(dirname(cache));
          if (!parent.isDirectory() || parent.isSymbolicLink()) throw new Error('Cloud credential parent must be an ordinary directory.');
          if (platform !== 'win32' && ((parent.mode & 0o777) !== 0o700 || (typeof process.getuid === 'function' && parent.uid !== process.getuid()))) throw new Error('Cloud credential parent must belong to this user and have mode 0700.');
        } catch (error) { if (error.code !== 'ENOENT') throw error; }
      });
      warnings.push('The source path is an empty local cloud anchor. Microsoft login, consent, cloud permissions and connectivity are not checked offline.');
    }
  }
  const sourceVersion = check('installed_package', () => JSON.parse(readFileSync(join(repo, 'package.json'), 'utf8')).version);
  const entry = join(repo, 'dist', 'index.js');
  const builtVersion = check('built_entrypoint', () => {
    const text = readFileSync(entry, 'utf8');
    const match = text.match(/name:\s*['"]df-mcp['"],\s*version:\s*['"]([^'"]+)['"]/);
    if (!match) throw new Error('Cannot identify the built MCP version; rebuild from the installed source.');
    if (sourceVersion !== match[1]) throw new Error(`Built version ${match[1]} differs from package ${sourceVersion}; rebuild before reconnecting.`);
    return match[1];
  });
  const python = env.DF_MCP_PYTHON || env.TEAM_DRIVE_PYTHON || join(repo, '.venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const pythonExecutable = check('python_executable', () => resolvePythonExecutable(python, { env, platform }));
  if (config?.state_pool) warnings.push('state_pool is a legacy isolated-worker setting; ordinary MCP connections use the shared service.');
  warnings.push('Final runtime configuration parsing, interpreter version, lock ownership, parser dependencies and source access security are not verified offline. Use status in a new MCP session for live health.');
  return {
    ok: checks.every(item => item.ok), mode: 'offline_read_only', config_path: configPath,
    source_path: source, state_path: state, selected_source: config?.provider === 'microsoft_graph' ? 'configured Microsoft Graph folder (local path is an anchor)' : source && resolve(source) === resolve(repo, 'examples', 'sample-drive') ? 'SIMULATED example source' : 'configured local source',
    installation: repo, package_version: sourceVersion, built_version: builtVersion, entrypoint: entry,
    python: pythonExecutable || python, state_exists: Boolean(state && existsSync(state)), shared_index: state && join(state, 'shared-v1', 'index'),
    connection_checked: false, source_scanned: false, state_modified: false, checks, warnings,
    privacy: 'Local diagnostic paths are private; redact before sharing.'
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = parseDoctorArguments(process.argv.slice(2));
    if (args.help) console.log(help);
    else { const result = inspectConfiguration(args.config); console.log(JSON.stringify(result, null, 2)); process.exitCode = result.ok ? 0 : 1; }
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
