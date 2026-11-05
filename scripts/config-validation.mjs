// Offline validation shared by configure/doctor. Keep bounds and keys aligned
// with team_drive.service_config.settings and Engine; this never starts a worker.
import { isIP } from 'node:net';
const limitKeys = new Set(['max_file_bytes', 'max_uncompressed_bytes', 'max_segments', 'max_segment_chars', 'max_columns', 'max_pdf_pages', 'parse_timeout_seconds']);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const bounded = (value, min, max, integer = false) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max && (!integer || Number.isSafeInteger(value));

function localEndpoint(value) {
  if (typeof value !== 'string' || value.length > 2048 || /\s/.test(value)) return false;
  try {
    // Check the supplied host before URL() canonicalizes shorthand/integer IPv4.
    const raw = /^https?:\/\/(\[[^\]]+\]|[^:/?#@\\]+)(?::([0-9]+))?(?:[/?#]|$)/i.exec(value);
    if (!raw || (raw[2] !== undefined && !bounded(Number(raw[2]), 1, 65535, true))) return false;
    const literal = raw[1].replace(/^\[|\]$/g, '').toLowerCase();
    if (literal !== 'localhost' && !isIP(literal)) return false;
    const u = new URL(value);
    if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password || u.search || u.hash) return false;
    const host = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    if (host === 'localhost' || host === '::1') return true;
    if (isIP(host) === 4) {
      const [a, b] = host.split('.').map(Number);
      return a === 127 || a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168);
    }
    return isIP(host) === 6 && /^f[cd][0-9a-f]{2}:/.test(host);
  } catch { return false; }
}

export function validateRuntimeOptions(config) {
  if (!object(config) || !['local', 'microsoft_graph'].includes(config.provider ?? 'local')) throw new Error('Use a local or microsoft_graph source configuration.');
  if (config.provider === 'microsoft_graph') validateGraphOptions(config.graph);
  else if (config.graph !== undefined) throw new Error('Graph settings require provider=microsoft_graph.');
  if (config.poll_seconds !== undefined && (typeof config.poll_seconds !== 'number' || !Number.isFinite(config.poll_seconds) || config.poll_seconds < 1 || config.poll_seconds > 86400)) throw new Error('poll_seconds must be a finite number between 1 and 86400.');
  if (config.limits !== undefined) {
    if (!object(config.limits) || Object.entries(config.limits).some(([key, value]) => !limitKeys.has(key) || !Number.isSafeInteger(value) || value < 1)) throw new Error('limits must use known keys and positive safe integers.');
  }
  if (config.aliases !== undefined && (!object(config.aliases) || Object.values(config.aliases).some(values => !Array.isArray(values) || values.some(value => typeof value !== 'string')))) throw new Error('aliases must map strings to lists of strings.');
  if (config.service !== undefined) {
    if (!object(config.service)) throw new Error('service must be an object.');
    for (const [name, value] of Object.entries(config.service)) {
      const maximum = name === 'idle_seconds' ? 3600 : name === 'max_clients' ? 256 : undefined;
      if (!maximum || typeof value !== 'number' || !Number.isFinite(value) || (name === 'max_clients' && !Number.isInteger(value)) || value < 1 || value > maximum) throw new Error(`Unsupported service setting: ${name}.`);
    }
  }
  if (config.diagnostics !== undefined) {
    const d = config.diagnostics;
    if (!object(d) || Object.keys(d).some(k => !['enabled', 'max_events', 'retention_days'].includes(k)) ||
        (d.enabled !== undefined && typeof d.enabled !== 'boolean') ||
        (d.max_events !== undefined && !bounded(d.max_events, 1, 10000, true)) ||
        (d.retention_days !== undefined && !bounded(d.retention_days, 1, 90, true))) throw new Error('Invalid bounded diagnostics settings.');
  }
  if (config.retrieval !== undefined) {
    const r = config.retrieval;
    if (!object(r) || Object.keys(r).some(k => !['candidate_limit', 'rrf_k', 'semantic', 'rerank'].includes(k))) throw new Error('Invalid retrieval options.');
    for (const k of ['candidate_limit', 'rrf_k']) if (r[k] !== undefined && !bounded(r[k], 1, 1000, true)) throw new Error(`Invalid retrieval.${k}.`);
    for (const kind of ['semantic', 'rerank']) {
      if (r[kind] === undefined) continue;
      const model = r[kind];
      const allowed = ['enabled', 'endpoint', 'model', 'timeout_seconds', ...(kind === 'semantic' ? ['model_revision', 'batch_size', 'max_segments', 'max_text_chars', 'minimum_similarity', 'per_file_budget_seconds'] : ['max_candidates'])];
      if (!object(model) || Object.keys(model).some(k => !allowed.includes(k))) throw new Error(`Invalid retrieval.${kind} options.`);
      if (model.enabled !== undefined && typeof model.enabled !== 'boolean') throw new Error('Model enabled must be boolean.');
      for (const k of ['model', 'endpoint', 'model_revision']) if (model[k] !== undefined && (typeof model[k] !== 'string' || model[k].length > 2048)) throw new Error('Model names and endpoints must be bounded strings.');
      if (model.endpoint && !localEndpoint(model.endpoint)) throw new Error('Use an explicit loopback or private IP model endpoint without credentials, query or fragment.');
      if (model.enabled && (!model.endpoint || !model.model?.trim())) throw new Error('Enabled models require a local endpoint and model name.');
      const ranges = { timeout_seconds: [0.1, 20, false], batch_size: [1, 64, true], max_segments: [1, 100000, true], max_text_chars: [1, 32000, true], minimum_similarity: [-1, 1, false], max_candidates: [1, 100, true], per_file_budget_seconds: [0.1, 20, false] };
      for (const [k, range] of Object.entries(ranges)) if (model[k] !== undefined && !bounded(model[k], ...range)) throw new Error(`Invalid retrieval.${kind}.${k}.`);
    }
  }
  return config;
}

export function validateGraphOptions(graph) {
  const keys = ['tenant_id', 'client_id', 'drive_id', 'folder_id', 'scopes', 'token_env', 'auth_cache',
    'timeout_seconds', 'max_items', 'max_pages', 'max_depth', 'scan_budget_seconds'];
  if (!object(graph) || Object.keys(graph).some(key => !keys.includes(key))) throw new Error('Use supported Microsoft Graph configuration fields.');
  // JS $ can match before a final newline; Python fullmatch rejects that input.
  const full = (pattern, value) => typeof value === 'string' && value.match(pattern)?.[0] === value;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!(full(uuid, graph.tenant_id) || ['common', 'organizations', 'consumers'].includes(graph.tenant_id))) throw new Error('Use a tenant UUID or an official Microsoft tenant alias.');
  if (!full(uuid, graph.client_id)) throw new Error('Use the registered application client UUID.');
  for (const key of ['drive_id', 'folder_id']) if (!full(/^[A-Za-z0-9!._~-]{1,256}$/, graph[key]) || ['.', '..', 'root'].includes(graph[key])) throw new Error('Use explicit drive and folder item IDs.');
  if (graph.scopes !== undefined) {
    const resource = s => typeof s === 'string' && ['files.read', 'files.read.all', 'sites.read.all'].includes(s.replace(/^https:\/\/graph\.microsoft\.com\//, '').toLowerCase());
    const identity = s => typeof s === 'string' && ['offline_access', 'openid', 'profile'].includes(s.replace(/^https:\/\/graph\.microsoft\.com\//, ''));
    if (!Array.isArray(graph.scopes) || !graph.scopes.length || graph.scopes.length > 6 || !graph.scopes.some(resource) || graph.scopes.some(s => !resource(s) && !identity(s))) throw new Error('Only documented read-only Graph scopes are supported.');
  }
  if (graph.token_env !== undefined && !full(/^[A-Za-z_][A-Za-z0-9_]{0,127}$/, graph.token_env)) throw new Error('token_env must name an environment variable, never contain a token.');
  if (graph.auth_cache !== undefined && (typeof graph.auth_cache !== 'string' || !graph.auth_cache.trim() || graph.auth_cache.includes('\0'))) throw new Error('auth_cache must be a private local path.');
  if (!graph.auth_cache && !graph.token_env) throw new Error('Configure a private auth_cache or an explicit token_env.');
  const limits = { timeout_seconds: [0.1, 20, false], max_items: [1, 100000, true], max_pages: [1, 10000, true], max_depth: [1, 128, true], scan_budget_seconds: [1, 600, false] };
  for (const [key, range] of Object.entries(limits)) if (graph[key] !== undefined && !bounded(graph[key], ...range)) throw new Error(`Invalid graph.${key}.`);
  return graph;
}
