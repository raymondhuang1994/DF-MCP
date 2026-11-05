import { createHash } from 'node:crypto';
import { closeSync, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, realpathSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defaultStateBase, preflightRoot, repositoryRoot, shellQuote, validateStatePath } from './configure.mjs';
import { currentNodeExecutable, venvPython } from './installer-utils.mjs';
import { validateRuntimeOptions } from './config-validation.mjs';

export const cloudHelp = `Configure one explicitly selected Microsoft Graph drive folder.\n\n  --tenant-id <ID>     Entra directory UUID, common, organizations or consumers\n  --client-id <UUID>   Public client application ID (not a secret)\n  --drive-id <ID>      Approved OneDrive/SharePoint document library ID\n  --folder-id <ID>     Approved folder item ID, not a sharing URL or path\n  --state-dir <path>   Optional private local application state base\n  --yes               Confirm the four explicit IDs and save configuration\n  --help              Show this help\n\nThis command creates only a local configuration and empty cloud-anchor directory.\nIt does not sign in, contact Microsoft Graph, scan files or change cloud permissions.`;

const identityKeys = ['tenant_id', 'client_id', 'drive_id', 'folder_id'];
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const itemId = /^[A-Za-z0-9!._~-]{1,256}$/;

function normalizeIdentity(values) {
  const result = {};
  for (const key of identityKeys) {
    const value = values[key];
    const valid = typeof value === 'string' && (key === 'tenant_id' ? (uuid.test(value) || ['common', 'organizations', 'consumers'].includes(value.toLowerCase())) : key === 'client_id' ? uuid.test(value) : itemId.test(value) && !['.', '..', 'root'].includes(value.toLowerCase()));
    if (!valid) throw new Error(`Supply an explicit valid ${key}; IDs cannot be URLs, paths or credentials.`);
    result[key] = key === 'tenant_id' || key === 'client_id' ? value.toLowerCase() : value;
  }
  return result;
}

export function parseCloudArguments(argv) {
  const options = {};
  const valued = new Set(['--tenant-id', '--client-id', '--drive-id', '--folder-id', '--state-dir']);
  for (let index = 0; index < argv.length; index++) {
    const key = argv[index];
    if (Object.hasOwn(options, key)) throw new Error(`Duplicate option: ${key}`);
    if (valued.has(key)) {
      if (!argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`Supply exactly one value for ${key}.`);
      options[key] = argv[++index];
    } else if (key === '--yes' || key === '--help') options[key] = true;
    else throw new Error(`Unknown argument: ${key}`);
  }
  if (options['--help']) return options;
  if (!options['--yes']) throw new Error('Cloud configuration requires --yes with all four explicit IDs. No source was selected.');
  options.identity = normalizeIdentity(Object.fromEntries(identityKeys.map(key => [key, options[`--${key.replaceAll('_', '-')}`]])));
  return options;
}

function within(parent, child, platform) {
  const normalize = value => platform === 'win32' ? value.split(sep).map(part => part.replace(/[. ]+$/, '').toLowerCase()).join(sep) : value;
  const difference = relative(normalize(parent), normalize(child));
  return !difference || (difference !== '..' && !difference.startsWith(`..${sep}`) && !isAbsolute(difference));
}

function statIfPresent(path) {
  try { return lstatSync(path); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function ordinaryDirectory(path, { empty = false, required = false } = {}) {
  const info = statIfPresent(path);
  if (!info) {
    if (required) throw new Error('The cloud-anchor directory is missing; restore the original profile or configure a new one explicitly.');
    return;
  }
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Cloud profile directories must be ordinary local directories, not links or files.');
  if (empty && readdirSync(path).length) throw new Error('The dedicated cloud-anchor must remain empty; it is not a local document source.');
}

export function makeCloudPlan(identity, stateInput, { repo = repositoryRoot, platform = process.platform, env = process.env, home = homedir() } = {}) {
  const graph = normalizeIdentity(identity);
  const input = stateInput ?? defaultStateBase({ platform, env, home });
  if (typeof input !== 'string' || !input.trim() || input.includes('\0') || /^(?:\\\\|\/\/)/.test(input)) throw new Error('Choose an ordinary local --state-dir, not a network or device path.');
  const settings = { platform, env, home };
  const base = validateStatePath(resolve(input), settings);
  const install = realpathSync.native(repo);
  if (within(base, install, platform) || within(install, base, platform)) throw new Error('Cloud state storage and the df mcp installation must not overlap.');
  const key = createHash('sha256').update(JSON.stringify({ provider: 'microsoft_graph', ...graph })).digest('hex').slice(0, 24);
  const profile = join(base, 'profiles', `graph-${key}`);
  const plan = { graph, base, profile, root: join(profile, 'cloud-anchor'), state: join(profile, 'state'), authCache: join(profile, 'auth.json'), configPath: join(profile, 'config.json'), repo: install, settings, platform };
  validateCloudPaths(plan);
  return plan;
}

function validateCloudPaths(plan) {
  for (const path of [plan.base, plan.profile, plan.root, plan.state, plan.authCache, plan.configPath]) validateStatePath(path, plan.settings);
  for (const path of [plan.base, plan.profile, plan.state]) ordinaryDirectory(path);
  const profile = statIfPresent(plan.profile);
  if (profile && plan.platform !== 'win32' && (profile.mode & 0o777) !== 0o700) throw new Error('The private cloud profile directory must have owner-only permissions (0700).');
  ordinaryDirectory(plan.root, { empty: true });
  for (const path of [plan.authCache, plan.configPath]) {
    const info = statIfPresent(path);
    if (info && (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1)) throw new Error('Cloud configuration and authentication cache must be ordinary unlinked files.');
  }
}

function existingConfiguration(plan) {
  const info = statIfPresent(plan.configPath);
  if (!info) return null;
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 262144) throw new Error('Existing cloud configuration must be an ordinary JSON file no larger than 256 KiB. Nothing was changed.');
  let config;
  try { config = JSON.parse(readFileSync(plan.configPath, 'utf8')); } catch { throw new Error('Existing cloud configuration cannot be read. Nothing was changed.'); }
  if (!config || typeof config !== 'object' || Array.isArray(config) || config.provider !== 'microsoft_graph' || !config.graph || typeof config.graph !== 'object' || Array.isArray(config.graph)) throw new Error('This profile is bound to a different source. Nothing was overwritten.');
  const samePath = (value, expected) => {
    if (typeof value !== 'string' || !value.trim()) return false;
    const actual = validateStatePath(resolve(dirname(plan.configPath), value), plan.settings);
    return within(actual, expected, plan.platform) && within(expected, actual, plan.platform);
  };
  if (identityKeys.some(key => config.graph[key] !== plan.graph[key]) || !samePath(config.root, plan.root) || !samePath(config.state_dir, plan.state) || !samePath(config.graph.auth_cache, plan.authCache)) throw new Error('This profile is bound to a different cloud source, state or authentication cache. Nothing was overwritten.');
  ordinaryDirectory(plan.root, { empty: true, required: true });
  validateRuntimeOptions(config);
  return config;
}

export async function writeCloudConfiguration(plan, { validateRoot = preflightRoot, env = process.env } = {}) {
  validateCloudPaths(plan);
  const existing = existingConfiguration(plan);
  if (existing) {
    await validateRoot(plan.root, { repo: plan.repo, platform: plan.platform, env });
    validateCloudPaths(plan);
    return { config: existing, reused: true };
  }
  const config = { provider: 'microsoft_graph', root: plan.root, state_dir: plan.state, poll_seconds: 300, graph: { ...plan.graph, auth_cache: plan.authCache } };
  validateRuntimeOptions(config);
  mkdirSync(plan.profile, { recursive: true, mode: 0o700 });
  validateCloudPaths(plan);
  mkdirSync(plan.root, { recursive: true, mode: 0o700 });
  await validateRoot(plan.root, { repo: plan.repo, platform: plan.platform, env });
  validateCloudPaths(plan);
  const handle = openSync(plan.configPath, 'wx', 0o600);
  try { writeFileSync(handle, `${JSON.stringify(config, null, 2)}\n`, 'utf8'); } finally { closeSync(handle); }
  return { config, reused: false };
}

export async function runConfigureCloud(argv = [], runtime = {}) {
  const options = parseCloudArguments(argv);
  const output = runtime.output || process.stdout;
  const log = message => output.write(`${message}\n`);
  if (options['--help']) { log(cloudHelp); return { help: true }; }
  const env = runtime.env || process.env;
  const platform = runtime.platform || process.platform;
  const plan = makeCloudPlan(options.identity, options['--state-dir'], { repo: runtime.repo || repositoryRoot, platform, env, home: runtime.home || homedir() });
  const result = await writeCloudConfiguration(plan, { validateRoot: runtime.validateRoot || preflightRoot, env });
  log(`${result.reused ? 'Existing cloud configuration reused without rewriting.' : 'Cloud configuration saved.'}\nConfiguration: ${plan.configPath}\nCloud scope: the specified tenant, application, drive and folder IDs only.\nNo cloud files scanned; no authentication cache or index created.\nSign in explicitly: node scripts/login-cloud.mjs --config ${shellQuote(plan.configPath, platform)}\nRegister in Codex (PowerShell on Windows):\ncodex mcp add df_mcp -- ${shellQuote(currentNodeExecutable({ platform, env }), platform)} ${shellQuote(join(plan.repo, 'dist', 'index.js'), platform)} --config ${shellQuote(plan.configPath, platform)} --python ${shellQuote(venvPython(plan.repo, platform), platform)}\nIf df_mcp is already registered, update that entry rather than adding a duplicate. Restart existing chats after changing the connection.\nThe cloud-anchor is an empty local boundary marker. Do not store source documents in it.`);
  return { ...result, plan };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runConfigureCloud(process.argv.slice(2)).catch(error => { console.error(error.message); process.exitCode = 1; });
}
