import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const help = 'Usage: node scripts/call-tool.mjs [tools|TOOL_NAME] [JSON_ARGUMENTS] --config <config.json>\nOr pass --demo explicitly for the SIMULATED example library.\nThis opens an MCP session; use doctor.mjs for offline diagnostics without scanning. Ordinary reads wait up to 60 seconds for initial indexing; status is immediate.';

export function parseCallArguments(argv) {
  if (argv.length === 1 && argv[0] === '--help') return { help: true };
  let config, demo = false;
  const positional = [];
  for (let index = 0; index < argv.length; index++) {
    const value = argv[index];
    if (value === '--config') {
      if (config || !argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error('Supply exactly one --config path.');
      config = resolve(argv[++index]);
    } else if (value === '--demo') { if (demo) throw new Error('Duplicate --demo.'); demo = true; }
    else if (value === '--list') positional.push('tools');
    else if (value.startsWith('--')) throw new Error(`Unknown option: ${value}`);
    else positional.push(value);
  }
  if (Boolean(config) === demo) throw new Error('Choose exactly one explicit --config <path> or --demo. No implicit source is selected.');
  if (positional.length > 2) throw new Error(help);
  const [name = 'team_drive_status', raw = '{}'] = positional;
  if (name !== 'tools' && !/^team_drive_[a-z_]+$/.test(name)) throw new Error('Use a declared team_drive_ tool name or tools.');
  const params = JSON.parse(raw);
  if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('Tool arguments must be a JSON object.');
  return { name, params, config: config || join(root, 'examples/config.sample.json'), demo };
}

export async function runCall(argv) {
  const args = parseCallArguments(argv);
  if (args.help) { console.log(help); return; }
  if (args.demo) console.error('SIMULATED example library selected explicitly.');
  const client = new Client({ name: 'df-mcp-cli', version: '0.5.0' });
  async function waitForJob(jobId) {
    const until = Date.now() + 60_000;
    while (Date.now() < until) {
      const wire = await client.callTool({ name: 'team_drive_job_status', arguments: jobId ? { job_id: jobId } : {} });
      const result = wire.structuredContent;
      if (!result?.ok || !['queued', 'running'].includes(result.job.status)) return result;
      await new Promise(resolveDelay => setTimeout(resolveDelay, 500));
    }
    throw new Error('Indexing is still running. Use a persistent MCP client for this library.');
  }
  try {
    await client.connect(new StdioClientTransport({ command: process.execPath, args: [join(root, 'dist/index.js'), '--config', args.config], stderr: 'pipe' }));
    if (args.name === 'tools') { console.log(JSON.stringify(await client.listTools(), null, 2)); return; }
    if (!['team_drive_status', 'team_drive_cancel', 'team_drive_job_status'].includes(args.name)) {
      const startup = await waitForJob();
      if (!startup?.ok) { console.log(JSON.stringify(startup, null, 2)); process.exitCode = 1; return; }
    }
    const result = await client.callTool({ name: args.name, arguments: args.params });
    const body = result.structuredContent || result;
    if (args.name === 'team_drive_refresh' && body.ok) body.completion = await waitForJob(body.job_id);
    console.log(JSON.stringify(body, null, 2));
    if (result.isError || body.completion?.ok === false || ['failed', 'cancelled', 'interrupted'].includes(body.completion?.job?.status)) process.exitCode = 1;
  } finally { await client.close(); }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runCall(process.argv.slice(2)).catch(error => { console.error(`Tool call failed: ${error.message}`); process.exitCode = 1; });
}
