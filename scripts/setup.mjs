import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { helpText, makePlan, parseArguments, repositoryRoot, runConfigure } from './configure.mjs';
import { discoverPython, runChecked, venvPython } from './installer-utils.mjs';

export async function setup(argv = []) {
  const options = parseArguments(argv, { allowSkip: true });
  if (options['--help']) { console.log(`${helpText}\n\nSetup only: --skip-config installs dependencies and builds without selecting or indexing a folder.`); return; }
  if (!options['--skip-config'] && !(process.stdin.isTTY && process.stdout.isTTY) && (!options['--root'] || !options['--yes'])) throw new Error('Non-interactive setup requires --root <folder> --yes, or --skip-config for a build only.');
  // Reject unsafe explicit source/storage relationships before dependency installation writes anything.
  if (options['--root']) makePlan(options['--root'], options['--state-dir']);
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('Node >=22 is required.');
  const python = venvPython(repositoryRoot);
  if (!existsSync(python)) {
    const discovered = discoverPython();
    runChecked(discovered.command, [...discovered.args, '-m', 'venv', '.venv'], { cwd: repositoryRoot });
  }
  runChecked(python, ['-m', 'pip', 'install', '-r', 'requirements.txt'], { cwd: repositoryRoot });
  runChecked(process.execPath, [join(repositoryRoot, 'node_modules', 'typescript', 'bin', 'tsc')], { cwd: repositoryRoot });
  console.log('Local dependencies and build are ready.');
  if (options['--skip-config']) { console.log('Configuration skipped. No source folder was selected or indexed. Run npm run configure when ready.'); return; }
  await runConfigure(argv);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  setup(process.argv.slice(2)).catch((error) => { console.error(error.message); process.exitCode = 1; });
}
