"""Inspect Git-tracked publish candidates without printing matched secrets."""
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
patterns = {
    'private_home_path': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'windows_home_path': re.compile(r'[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_.-]+[\\/]'),
    'access_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    'email': re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
}
bad = []
for name in filter(None, tracked):
    path = root / name
    if any(part in {'.local', '.venv', 'node_modules', '.env', 'private', '__pycache__'} for part in path.relative_to(root).parts) or path.suffix in {'.sqlite', '.db', '.log'}:
        bad.append((name, 'runtime_or_private_file'))
    if path.is_symlink():
        bad.append((name, 'symlink'))
        continue
    try:
        content = path.read_text(encoding='utf-8')
    except (UnicodeError, OSError):
        bad.append((name, 'unexpected_binary_or_unreadable'))
        continue
    for label, pattern in patterns.items():
        if pattern.search(content):
            bad.append((name, label))
if bad:
    for name, label in bad:
        print('{}: {}'.format(name, label))
    sys.exit(1)
print('Public-content scan passed for {} tracked text files.'.format(len(list(filter(None, tracked)))))
