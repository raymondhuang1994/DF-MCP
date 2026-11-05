import { spawnSync } from 'node:child_process';
import { accessSync, constants, existsSync, realpathSync, statSync } from 'node:fs';
import { delimiter, isAbsolute, join, resolve } from 'node:path';

function executableFile(path, platform) {
  try {
    if (!statSync(path).isFile()) return false;
    if (platform !== 'win32') accessSync(path, constants.X_OK);
    return true;
  } catch { return false; }
}

export function findExecutable(name, { platform = process.platform, env = process.env } = {}) {
  const names = platform === 'win32' && !name.toLowerCase().endsWith('.exe') ? [`${name}.exe`] : [name];
  for (const directory of (env.PATH || env.Path || '').split(platform === 'win32' ? ';' : delimiter)) {
    if (!directory || (platform === 'win32' && /(?:^|[\\/])WindowsApps(?:[\\/]|$)/i.test(directory))) continue;
    for (const entry of names) {
      const path = resolve(directory, entry);
      if (executableFile(path, platform)) return path;
    }
  }
  return null;
}

export function resolvePythonExecutable(command, { platform = process.platform, env = process.env } = {}) {
  const selected = isAbsolute(command) || command.includes('/') || command.includes('\\') ? resolve(command) : findExecutable(command, { platform, env });
  if (!selected || (platform === 'win32' && /(?:^|[\\/])WindowsApps(?:[\\/]|$)/i.test(selected)) || !executableFile(selected, platform)) throw new Error('The selected Python executable is missing or not executable. Run npm run setup or correct DF_MCP_PYTHON.');
  return selected;
}

export function currentNodeExecutable({ platform = process.platform, env = process.env, execPath = process.execPath } = {}) {
  // Keep a stable absolute PATH entry (e.g. a Homebrew symlink) only when it
  // resolves to the Node actually running setup. Never select a different Node.
  const candidate = findExecutable('node', { platform, env });
  const canonical = path => {
    const value = realpathSync.native(path);
    return platform === 'win32' ? value.toLowerCase() : value;
  };
  if (candidate) {
    try { if (canonical(candidate) === canonical(execPath)) return candidate; } catch { /* Use the running executable. */ }
  }
  return resolve(execPath);
}

export function discoverPython({ platform = process.platform, env = process.env, runner = spawnSync } = {}) {
  const explicit = env.DF_MCP_PYTHON || env.TEAM_DRIVE_PYTHON;
  const candidates = explicit ? [{ command: explicit, args: [] }] : platform === 'win32'
    ? [{ command: findExecutable('py', { platform, env }), args: ['-3'] }, { command: findExecutable('python', { platform, env }), args: [] }]
    : [{ command: findExecutable('python3', { platform, env }), args: [] }, { command: findExecutable('python', { platform, env }), args: [] }];
  for (const candidate of candidates) {
    if (!candidate.command || (platform === 'win32' && /(?:^|[\\/])WindowsApps(?:[\\/]|$)/i.test(candidate.command))) continue;
    const result = runner(candidate.command, [...candidate.args, '-c', 'import sys; print("%d.%d" % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3,9) else 1)'], { encoding: 'utf8', timeout: 15000, windowsHide: true, env: { ...env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' } });
    if (!result.error && result.status === 0) return candidate;
  }
  throw new Error('Python >=3.9 was not found. Install Python (not a Microsoft Store alias), or set DF_MCP_PYTHON to its executable.');
}

export function venvPython(repositoryRoot, platform = process.platform) {
  return join(repositoryRoot, '.venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
}

export function runChecked(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', windowsHide: true, ...options, env: { ...process.env, ...(options.env || {}), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' } });
  if (result.error || result.status !== 0) throw new Error('Setup command failed. Check the preceding output, Node >=22, and Python >=3.9.');
}

export function requireVenv(repositoryRoot, platform = process.platform) {
  const python = venvPython(repositoryRoot, platform);
  if (!existsSync(python)) throw new Error('The local Python environment is missing. Run npm run setup first.');
  return python;
}
