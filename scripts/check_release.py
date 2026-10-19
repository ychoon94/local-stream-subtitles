"""Check the tracked source inventory without printing local secret values."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_ROOTS = {'.runtime', '.venv', 'models', 'node_modules', 'test-results', 'dist'}
BLOCKED_FILES = {'extension/local-config.json', 'task_plan.md', 'findings.md', 'progress.md'}
SECRET_PATTERN = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{25,}|sk-proj-[A-Za-z0-9_-]{20,})')


def git(*arguments):
    return subprocess.check_output(['git', *arguments], cwd=ROOT)


def check(staged=False):
    local_token = None
    settings = ROOT / '.runtime' / 'settings.json'
    if settings.exists():
        local_token = json.loads(settings.read_text(encoding='utf-8-sig')).get('token', '').encode()
    failures = []
    paths = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z') if staged else git('ls-files', '-z')
    names = [part.decode('utf-8') for part in paths.split(b'\0') if part]
    for name in names:
        path = Path(name)
        if (path.parts[0] in BLOCKED_ROOTS or name in BLOCKED_FILES
                or path.name.startswith('.env') and path.name != '.env.example'
                or path.suffix.lower() in {'.log', '.pyc', '.dll', '.exe', '.wav', '.mp3', '.mp4', '.zip', '.bin', '.safetensors'}):
            failures.append(f'Local/generated artifact tracked: {name}')
            continue
        content = git('show', f':{name}') if staged else (ROOT / name).read_bytes()
        if SECRET_PATTERN.search(content) or local_token and local_token in content:
            failures.append(f'Possible secret in source: {name}')
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'Source audit passed: {len(names)} files; no local artifacts or matching secrets.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true')
    check(parser.parse_args().staged)
