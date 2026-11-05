import { spawnSync } from 'node:child_process';
import { lstatSync, readFileSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { repositoryRoot, validateStatePath } from './configure.mjs';
import { resolvePythonExecutable, venvPython } from './installer-utils.mjs';
import { validateRuntimeOptions } from './config-validation.mjs';

export const loginHelp = 'Usage: node scripts/login-cloud.mjs --config <cloud-config.json>\nExplicitly starts Microsoft device-code sign-in. No cloud files are scanned.\nUse the requested verification page and code yourself; never share the code or token cache.';

export function parseLoginArguments(argv) {
  if (argv.length === 1 && argv[0] === '--help') return { help: true };
  if (argv.length !== 2 || argv[0] !== '--config' || !argv[1] || argv[1].startsWith('--')) throw new Error(loginHelp);
  return { configPath: resolve(argv[1]) };
}

function contained(parent, child, platform) {
  const normalize = value => platform === 'win32' ? value.toLowerCase() : value;
  const difference = relative(normalize(parent), normalize(child));
  return !difference || (difference !== '..' && !difference.startsWith(`..${sep}`) && !isAbsolute(difference));
}

export function inspectLoginConfiguration(configPath, { platform = process.platform, env = process.env, home = homedir() } = {}) {
  const settings = { platform, env, home };
  validateStatePath(configPath, settings);
  const info = lstatSync(configPath);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 262144) throw new Error('Use an ordinary unlinked cloud configuration JSON file no larger than 256 KiB.');
  let config;
  try { config = JSON.parse(readFileSync(configPath, 'utf8')); } catch { throw new Error('The cloud configuration JSON cannot be read.'); }
  if (!config || config.provider !== 'microsoft_graph' || !config.graph || typeof config.graph !== 'object' || Array.isArray(config.graph)) throw new Error('Explicit Microsoft Graph configuration is required before login.');
  validateRuntimeOptions(config);
  const paths = {};
  for (const [key, value] of Object.entries({ root: config.root, state: config.state_dir, auth: config.graph.auth_cache })) {
    if (typeof value !== 'string' || !value.trim() || value.includes('\0') || /^(?:\\\\|\/\/)/.test(value)) throw new Error('Cloud anchor, state and authentication cache require explicit ordinary local paths.');
    paths[key] = validateStatePath(resolve(dirname(configPath), value), settings);
  }
  if (contained(paths.root, paths.state, platform) || contained(paths.state, paths.root, platform) || contained(paths.root, paths.auth, platform) || contained(paths.state, paths.auth, platform) || paths.auth === configPath) throw new Error('Cloud anchor, state and authentication cache must be separate non-overlapping locations.');
  const root = lstatSync(paths.root);
  if (!root.isDirectory() || root.isSymbolicLink() || readdirSync(paths.root).length) throw new Error('The dedicated cloud-anchor must be an ordinary empty directory.');
  let cache;
  try { cache = lstatSync(paths.auth); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (cache && (!cache.isFile() || cache.isSymbolicLink() || cache.nlink !== 1)) throw new Error('Authentication cache must be an ordinary unlinked file.');
  return config;
}

export function runLoginCloud(argv = [], runtime = {}) {
  const args = parseLoginArguments(argv);
  if (args.help) { (runtime.output || process.stdout).write(`${loginHelp}\n`); return 0; }
  const env = runtime.env || process.env;
  const platform = runtime.platform || process.platform;
  const repo = runtime.repo || repositoryRoot;
  inspectLoginConfiguration(args.configPath, { platform, env, home: runtime.home || homedir() });
  const selected = env.DF_MCP_PYTHON || env.TEAM_DRIVE_PYTHON || venvPython(repo, platform);
  const python = (runtime.resolvePython || resolvePythonExecutable)(selected, { env, platform });
  const result = (runtime.runner || spawnSync)(python, ['-m', 'team_drive.graph_auth', '--config', args.configPath], {
    cwd: repo, stdio: 'inherit', windowsHide: true, shell: false,
    env: { ...env, PYTHONPATH: join(repo, 'python'), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' },
  });
  if (result.error) throw new Error('Cloud login could not start the configured Python runtime.');
  if (result.signal === 'SIGINT') return 130;
  return Number.isInteger(result.status) ? result.status : 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = runLoginCloud(process.argv.slice(2)); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
