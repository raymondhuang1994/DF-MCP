import { createHash, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function ordinaryDirectory(path) {
  const stat = fs.lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('Skill locations must be ordinary directories, not links.');
}

function packageManifest(root) {
  const entries = [];
  function visit(path, relative) {
    const stat = fs.lstatSync(path);
    if (stat.isSymbolicLink()) throw new Error('The skill package contains a link and cannot be installed.');
    if (stat.isDirectory()) {
      entries.push({ path: relative, type: 'directory' });
      for (const name of fs.readdirSync(path).sort()) visit(join(path, name), relative ? `${relative}/${name}` : name);
    } else if (stat.isFile()) {
      const bytes = fs.readFileSync(path);
      entries.push({ path: relative, type: 'file', bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') });
    } else throw new Error('The skill package contains an unsupported file type.');
  }
  ordinaryDirectory(root);
  visit(root, '');
  return entries;
}

function manifestSummary(manifest) {
  return { entries: manifest.length, sha256: createHash('sha256').update(JSON.stringify(manifest)).digest('hex') };
}

function copyPackage(source, target) {
  ordinaryDirectory(source);
  fs.mkdirSync(target, { mode: 0o700 });
  for (const name of fs.readdirSync(source).sort()) {
    const input = join(source, name);
    const output = join(target, name);
    const stat = fs.lstatSync(input);
    if (stat.isSymbolicLink()) throw new Error('The skill package contains a link and cannot be installed.');
    if (stat.isDirectory()) copyPackage(input, output);
    else if (stat.isFile()) fs.copyFileSync(input, output, fs.constants.COPYFILE_EXCL);
    else throw new Error('The skill package contains an unsupported file type.');
  }
}

function pathSummary(path, includeManifest = false) {
  try {
    const stat = fs.lstatSync(path);
    const result = { path, exists: true, directory: stat.isDirectory(), link: stat.isSymbolicLink() };
    if (includeManifest && stat.isDirectory() && !stat.isSymbolicLink()) result.manifest = manifestSummary(packageManifest(path));
    return result;
  } catch (error) {
    return { path, exists: error.code === 'ENOENT' ? false : null, error: error.code || error.message };
  }
}

export function installSkill(argv = [], { repo = repositoryRoot, codexHome = process.env.CODEX_HOME || join(homedir(), '.codex'), log = console.log } = {}) {
  if (argv.includes('--help')) { log('Install df-mcp: npm run install:skill\nExplicit upgrade/migration: npm run install:skill -- --replace\nExisting skills are backed up outside the skills directory before replacement.'); return { help: true }; }
  if (argv.some((arg) => arg !== '--replace') || argv.length > 1) throw new Error('Only --replace or --help is supported.');
  const replace = argv.includes('--replace');
  const source = join(repo, 'skills', 'df-mcp');
  const home = resolve(codexHome);
  const skills = join(home, 'skills');
  const target = join(skills, 'df-mcp');
  const legacy = join(skills, 'team-drive-read');
  const expected = packageManifest(source);
  for (const directory of [home, skills, join(home, 'skill-backups')]) if (fs.existsSync(directory)) ordinaryDirectory(directory);
  const previous = [target, legacy].filter(fs.existsSync);
  for (const directory of previous) ordinaryDirectory(directory);
  if (previous.length && !replace) throw new Error('An existing df-mcp or team-drive-read skill was found. Nothing was changed. Review it, then use --replace to back it up and explicitly upgrade/migrate.');
  // Prepare the entire package outside the discoverable skills directory before changing old skills.
  const transaction = join(home, 'skill-backups', `${new Date().toISOString().replaceAll(':', '-')}-${randomUUID().slice(0, 8)}`);
  const staged = join(transaction, 'incoming');
  fs.mkdirSync(transaction, { recursive: true, mode: 0o700 });
  const moved = [];
  let phase = 'copy package';
  try {
    // Node 22's native recursive cp uses std::filesystem paths on Windows. Use
    // libuv-backed primitives for Unicode paths and cross-volume source copies.
    // Both staging and publication remain on the destination's volume.
    copyPackage(source, staged);
    phase = 'verify staged package';
    const actual = packageManifest(staged);
    if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error('Staged package does not match the source file manifest.');
    phase = 'prepare destination';
    fs.mkdirSync(skills, { recursive: true, mode: 0o700 });
    ordinaryDirectory(skills);
    ordinaryDirectory(staged);
    phase = 'back up previous skill';
    for (const old of previous) {
      const backup = join(transaction, old === legacy ? 'team-drive-read' : 'df-mcp');
      fs.renameSync(old, backup);
      moved.push({ old, backup });
    }
    phase = 'publish staged package';
    if (fs.existsSync(target)) throw new Error('The destination changed during installation; refusing to overwrite it.');
    fs.renameSync(staged, target);
  } catch (error) {
    // Capture evidence before rollback/cleanup changes these paths. Hashes and
    // counts diagnose an incomplete copy without logging any package contents.
    const diagnostics = { phase, source, expected: manifestSummary(expected), staged: pathSummary(staged, true), parent: pathSummary(skills), target: pathSummary(target) };
    const recoveryErrors = [];
    for (const item of [...moved].reverse()) {
      try {
        if (fs.existsSync(item.old)) throw new Error(`Restore destination is occupied; backup retained at ${item.backup}`);
        fs.renameSync(item.backup, item.old);
      } catch (recoveryError) { recoveryErrors.push(`Backup retained at ${item.backup}: ${recoveryError.message}`); }
    }
    try { fs.rmSync(staged, { recursive: true, force: true }); } catch (cleanupError) { recoveryErrors.push(cleanupError.message); }
    if (!recoveryErrors.length) {
      try { fs.rmSync(transaction, { recursive: true }); } catch (cleanupError) { recoveryErrors.push(cleanupError.message); }
    }
    throw new Error(`Skill installation failed during ${phase}: ${error.message}\nDiagnostics: ${JSON.stringify(diagnostics)}${recoveryErrors.length ? `\nRecovery needs attention: ${recoveryErrors.join('; ')}` : ''}`, { cause: error });
  }
  if (!moved.length) fs.rmSync(transaction, { recursive: true });
  log(`Installed df mcp (skill identifier: df-mcp). Start a new session to use $df-mcp.\nConfigure the MCP source separately with npm run configure.${moved.length ? `\nPrevious skill content was preserved at: ${transaction}` : ''}`);
  return { target, backups: moved.map((item) => item.backup) };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { installSkill(process.argv.slice(2)); } catch (error) { console.error(error.message); process.exitCode = 1; }
}
