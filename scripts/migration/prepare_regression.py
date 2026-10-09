#!/usr/bin/env python3
"""Stage the existing pinned sandbox-tools overlay for local regression tests.

Install dependencies first with:
  uv sync --extra dev --group migration-regression --inexact
No tracked runtime source or upstream package is modified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from orchestrate import atomic

PATCH_FILES = (
    '__init__.py', 'agent_environment.py', 'lifecycle.py', '_cli/server.py',
    '_remote_tools/_exec_remote/_controller.py', '_remote_tools/_exec_remote/_job.py',
)


def prepare(repo, python):
    repo = Path(repo).resolve()
    vendor = repo / 'sandbox/vendor/inspect-sandbox-tools/src/inspect_sandbox_tools'
    overlay = repo / 'sandbox/inspect_sandbox_tools_patch'
    sources = {p.relative_to(vendor).as_posix(): p for p in vendor.rglob('*.py')}
    for name in PATCH_FILES:
        sources[name] = overlay / name
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sorted(sources.items())}
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    root = repo / '.scenario/migration-readiness' / ('sandbox-tools-' + identity[:16])
    package = root / 'src/inspect_sandbox_tools'
    if not root.exists():
        shutil.copytree(vendor, package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in PATCH_FILES:
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(overlay / name, target)
        shutil.copyfile(repo / 'sandbox/vendor/inspect-sandbox-tools/LICENSE', root / 'LICENSE')
        shutil.copyfile(repo / 'sandbox/vendor/inspect-sandbox-tools/UPSTREAM.md', root / 'UPSTREAM.md')
    for name, expected in hashes.items():
        if hashlib.sha256((package / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Prepared source mismatch: {name}; preserve it and investigate')
    atomic(root / 'manifest.json', {'source_digest': identity, 'package_version': '1.2.1',
                                  'files': hashes, 'overlay': list(PATCH_FILES)})
    purelib = Path(subprocess.check_output([str(python), '-c', 'import sysconfig;print(sysconfig.get_path("purelib"))'], text=True).strip())
    env_root = Path(python).absolute().parent.parent
    if not purelib.is_relative_to(env_root):
        raise RuntimeError('Select a project virtualenv, not a global interpreter')
    pth = purelib / 'migration_sandbox_tools.pth'
    pth.write_text(str(root / 'src') + '\n')
    probe = subprocess.check_output([str(python), '-c',
        'import json,inspect_sandbox_tools as p;from inspect_sandbox_tools.lifecycle import PATCH_ID;'
        'print(json.dumps({"version":p.__version__,"path":p.__file__,"patch_id":PATCH_ID}))'], text=True)
    result = json.loads(probe)
    if result['version'] != '1.2.1' or Path(result['path']).resolve() != (package / '__init__.py').resolve():
        raise RuntimeError('Interpreter did not load the prepared pinned package')
    print(json.dumps({'manifest': str(root / 'manifest.json'), 'pth': str(pth), **result}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--python', type=Path)
    args = parser.parse_args()
    prepare(args.repo, args.python or args.repo / '.venv/bin/python')
