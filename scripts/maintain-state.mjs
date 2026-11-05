import { spawnSync } from 'node:child_process';
import { dirname, delimiter, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = process.env.DF_MCP_PYTHON || process.env.TEAM_DRIVE_PYTHON || join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const result = spawnSync(python, ['-m', 'team_drive.maintenance', ...process.argv.slice(2)], {
  cwd: root, stdio: 'inherit', windowsHide: true,
  env: { ...process.env, PYTHONPATH: [join(root, 'python'), process.env.PYTHONPATH].filter(Boolean).join(delimiter), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' }
});
if (result.error) console.error('State maintenance could not start. Run setup or set DF_MCP_PYTHON.');
process.exitCode = result.status ?? 1;
