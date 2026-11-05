import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { Backend } from '../dist/backend.js';

const python = process.env.DF_MCP_PYTHON || process.env.TEAM_DRIVE_PYTHON || resolve('.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const worker = `import json, pathlib, sys
config = pathlib.Path(sys.argv[sys.argv.index('--config') + 1])
mode = config.read_text()
counter = config.with_suffix('.starts')
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
if mode == 'permanent' or mode == 'transient' and count == 1:
    print(json.dumps({'id': None, 'result': {'ok': False, 'error': {'code': 'STATE_POOL_EXHAUSTED' if mode == 'transient' else 'UNSAFE_STATE', 'retryable': mode == 'transient'}}}), flush=True)
    sys.exit(1)
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'crash':
        sys.exit(7)
    print(json.dumps({'id': request['id'], 'result': {'ok': True, 'starts': count}}), flush=True)
`;

async function fixture(mode, exercise) {
  const project = mkdtempSync(join(tmpdir(), 'df-mcp-backend-'));
  mkdirSync(join(project, 'python/team_drive'), { recursive: true });
  writeFileSync(join(project, 'python/team_drive/worker.py'), worker);
  const config = join(project, 'config.txt');
  writeFileSync(config, mode);
  const backend = new Backend({ python, project, config });
  try { await exercise(backend, () => Number(readFileSync(join(project, 'config.starts'), 'utf8'))); }
  finally { await backend.stop(); rmSync(project, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
}

const delay = ms => new Promise(resolveDelay => setTimeout(resolveDelay, ms));

test('structured permanent startup failure is preserved and never respawned', async () => {
  await fixture('permanent', async (backend, starts) => {
    for (let i = 0; i < 4; i++) {
      const result = await backend.call('status', {});
      assert.equal(result.error.code, 'UNSAFE_STATE');
      await delay(25);
    }
    assert.equal(starts(), 1);
  });
});

test('capacity failure keeps transport backend usable and retries only on a later call', async () => {
  await fixture('transient', async (backend, starts) => {
    const result = await backend.call('status', {});
    assert.equal(result.error.code, 'STATE_POOL_EXHAUSTED');
    await delay(100);
    assert.equal(starts(), 1, 'No background respawn loop.');
    const recovered = await backend.call('status', {});
    assert.equal(recovered.ok, true);
    assert.equal(recovered.starts, 2);
  });
});

test('worker crash resolves in-flight requests without replay and recovers on demand', async () => {
  await fixture('normal', async (backend, starts) => {
    assert.equal((await backend.call('status', {})).ok, true);
    const failed = await backend.call('crash', {});
    assert.equal(failed.error.code, 'BACKEND_UNAVAILABLE');
    assert.equal(failed.error.retryable, true);
    assert.equal(starts(), 1);
    const recovered = await backend.call('status', {});
    assert.equal(recovered.ok, true);
    assert.equal(recovered.starts, 2);
    await backend.stop();
    assert.equal((await backend.call('status', {})).ok, false);
    assert.equal(starts(), 2, 'Closing cannot spawn another worker.');
  });
});
