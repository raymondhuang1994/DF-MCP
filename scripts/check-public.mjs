import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = process.env.DF_MCP_PYTHON || process.env.TEAM_DRIVE_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const result = spawnSync(python, [join(root, 'scripts/check_public.py')], {
  cwd: root, stdio: 'inherit', env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' }
});
if (result.error) console.error('Public-content check requires the project Python environment. Run setup first.');
process.exit(result.status ?? 1);
