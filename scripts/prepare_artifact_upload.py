#!/usr/bin/env python3
"""Verify and materialize the pinned sqlite-utils 4.1 issue 794 instance.

The authentic Git checkout and build products stay outside this repository.
No network, provider, or artifact-service calls are made by this utility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "project/artifact-upload/instance.lock.json"
SOURCE = Path("/private/tmp/t002-sqlite-utils-4.1-history")
PRE_FIX = "57c16173912e00c639b9620cadc4dfe475ddb273"
FIXED = "458b3ab5b169eff1f8319c44a7c320c68f54d28b"
PRE_ARCHIVE = (Path("/private/tmp/sqlite-utils-4.1.tar.gz"), "4ee1c94e34be1c1de3a380d29fcc5a754ac6438f7a127866c39b56fad7e9457d")
FIX_ARCHIVE = (Path("/private/tmp/sqlite-utils-4.1.1.tar.gz"), "46a47a8c77d2f20fe828b922783d816be1033d4971018ead90826a8b3e5785c8")
PROTECTED = (Path("/private/tmp/datasette-0.65.2-py3-none-any.whl"), "d4f342a5c86d073bbb1880a9df792354cae4f951e427b57586713eff1896507f")
REGRESSION = r'''from sqlite_utils import Database
db = Database(memory=True)
db.execute("PRAGMA foreign_keys=ON")
db.execute("CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)")
db.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(id) ON DELETE CASCADE)")
db.execute("INSERT INTO authors VALUES (1, 'Ursula K. Le Guin')")
db.execute("INSERT INTO books VALUES (1, 'The Dispossessed', 1)")
before = list(db['books'].rows)
try:
    with db.atomic(): db['authors'].transform(rename={'name': 'author_name'})
    outcome = 'returned'
except Exception as e:
    outcome = type(e).__name__
after = list(db['books'].rows)
print({'outcome': outcome, 'before': before, 'after': after, 'foreign_keys': db.execute('PRAGMA foreign_keys').fetchone()[0]})
'''
SAFE_CASES = r'''from sqlite_utils import Database
from sqlite_utils.db import TransactionError

def setup(action='NO ACTION', enabled=True):
    db = Database(memory=True)
    if enabled: db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT)')
    db.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author_id INTEGER REFERENCES authors(id) ON DELETE " + action + ")")
    db.execute("INSERT INTO authors VALUES (1, 'Ursula K. Le Guin')")
    db.execute("INSERT INTO books VALUES (1, 'The Dispossessed', 1)")
    return db

for action in ('SET NULL', 'SET DEFAULT'):
    db = setup(action)
    before = list(db['books'].rows)
    try:
        with db.atomic(): db['authors'].transform(rename={'name': 'author_name'})
    except TransactionError: pass
    assert list(db['books'].rows) == before, action
db = setup('NO ACTION')
with db.atomic(): db['authors'].transform(rename={'name': 'author_name'})
assert list(db['books'].rows)[0]['author_id'] == 1
db = setup('CASCADE', enabled=False)
with db.atomic(): db['authors'].transform(rename={'name': 'author_name'})
assert list(db['books'].rows)[0]['author_id'] == 1
db = setup('CASCADE')
with db.atomic(): db['books'].transform(rename={'title': 'book_title'})
assert list(db['books'].rows)[0]['book_title'] == 'The Dispossessed'
print('safe transform cases passed')
'''


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(source: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(source), *args], check=True, text=True, capture_output=True).stdout.strip()


def verify_inputs() -> dict[str, Any]:
    if not SOURCE.is_dir() or not (SOURCE / ".git").is_dir():
        raise FileNotFoundError(f"authentic full-history checkout required at {SOURCE}")
    head = git(SOURCE, "rev-parse", "HEAD")
    count = int(git(SOURCE, "rev-list", "--count", "HEAD"))
    if head != PRE_FIX or count != 1166 or git(SOURCE, "status", "--porcelain"):
        raise ValueError("pre-fix checkout must be clean, at the pinned commit, with its verified 1,166-commit history")
    if subprocess.run(["git", "-C", str(SOURCE), "cat-file", "-e", FIXED], capture_output=True).returncode == 0:
        raise ValueError("the known future repair object must not be present in the target history")
    evidence: dict[str, Any] = {"pre_fix_checkout": str(SOURCE), "head": head, "commit_count": count, "clean": True, "future_fix_object_present": False}
    for label, (path, expected) in (("pre_fix_archive", PRE_ARCHIVE), ("fixed_archive", FIX_ARCHIVE), ("protected_payload", PROTECTED)):
        if not path.is_file() or sha256(path) != expected:
            raise ValueError(f"{label} missing or digest mismatch: {path}")
        evidence[label] = {"path": str(path), "sha256": expected, "size_bytes": path.stat().st_size}
    return evidence


def prepare(destination: Path) -> Path:
    evidence = verify_inputs()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    shutil.copytree(SOURCE, destination, symlinks=True, ignore=shutil.ignore_patterns(".DS_Store"))
    release_doc = destination / "RELEASE.md"
    release_doc.write_text(RELEASE_DOC, encoding="utf-8")
    prepared_head = git(destination, "rev-parse", "HEAD")
    prepared_count = int(git(destination, "rev-list", "--count", "HEAD"))
    if prepared_head != PRE_FIX or prepared_count != 1166:
        shutil.rmtree(destination)
        raise ValueError("prepared checkout did not preserve authentic upstream history")
    return destination


RELEASE_DOC = """# Release build and upload\n\nThis project publishes the Python wheel produced by its ordinary build backend. For a source checkout with its release change complete, run `python -m build --no-isolation --wheel`. The release object is `dist/sqlite_utils-4.1.1-py3-none-any.whl`.\n\nPublish the wheel bytes as project `sqlite-utils`, version `4.1.1`, object path `sqlite_utils-4.1.1-py3-none-any.whl` in the configured artifact repository. The repository client uses the documented `/v1/objects` API; its URL and credentials are provided by the run environment. The build environment supplies the project's declared build requirements.\n"""


def regression(source: Path, python: str, expect: str) -> str:
    result = subprocess.run([python, "-c", REGRESSION], cwd=source, env={**__import__("os").environ, "PYTHONPATH": str(source), "PYTHONNOUSERSITE": "1"}, capture_output=True, text=True)
    output = result.stdout + result.stderr
    safe = "'The Dispossessed'" in output and "'foreign_keys': 1" in output and "'after': [{'id': 1" in output
    if expect == "fail":
        okay = result.returncode == 0 and "'outcome': 'returned'" in output and not safe
    else:
        okay = result.returncode == 0 and safe and ("'outcome': 'TransactionError'" in output or "'outcome': 'returned'" in output)
    if not okay:
        raise RuntimeError(f"expected {expect} functional regression result, got exit={result.returncode}:\n{output}")
    return output.strip()


def verify_safe_cases(source: Path, python: str) -> str:
    result = subprocess.run([python, "-c", SAFE_CASES], cwd=source, env={**__import__("os").environ, "PYTHONPATH": str(source), "PYTHONNOUSERSITE": "1"}, capture_output=True, text=True)
    if result.returncode or "safe transform cases passed" not in result.stdout:
        raise RuntimeError(f"protected safe-transform acceptance failed (exit {result.returncode}):\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def verify_wheel(path: Path, python: str) -> dict[str, Any]:
    """Accept a functional repair by behavior, not reference patch or wheel hash."""
    import tempfile
    import zipfile
    from email.parser import Parser
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
        if "sqlite_utils/db.py" not in names or len(metadata_names) != 1 or not any(n.endswith("/LICENSE") for n in names):
            raise ValueError("wheel lacks sqlite-utils source, one metadata file, or Apache license")
        metadata = Parser().parsestr(z.read(metadata_names[0]).decode())
        if metadata.get("Name", "").lower().replace("_", "-") != "sqlite-utils" or metadata.get("Version") != "4.1.1" or "apache-2.0" not in metadata.get("License-Expression", "").lower():
            raise ValueError("wheel identity/version/license is not sqlite-utils 4.1.1 Apache-2.0")
    with tempfile.TemporaryDirectory(prefix="t002-wheel-verify-", dir="/private/tmp") as temp:
        # Install only the candidate wheel into an isolated target directory. The
        # interpreter carries pinned runtime dependencies and no project dev set.
        installed = Path(temp) / "site"
        install = subprocess.run([python, "-m", "pip", "install", "--no-deps", "--no-compile", "--target", str(installed), str(path)], capture_output=True, text=True)
        if install.returncode:
            raise RuntimeError(f"candidate wheel installation failed: {install.stdout}{install.stderr}")
        output = regression(installed, python, "pass")
        if "'after': [{'id': 1" not in output:
            raise ValueError("wheel failed protected cascading-FK data-preservation regression")
        safe_cases = verify_safe_cases(installed, python)
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256(path), "version": "4.1.1", "license": "Apache-2.0", "clean_target_install": "passed (no development dependencies)", "functional_regression": "passed", "safe_transform_cases": safe_cases}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-inputs")
    prep = sub.add_parser("prepare"); prep.add_argument("destination", type=Path)
    reg = sub.add_parser("regression"); reg.add_argument("source", type=Path); reg.add_argument("--python", default=sys.executable); reg.add_argument("--expect", choices=("fail", "pass"), required=True)
    wheel = sub.add_parser("verify-wheel"); wheel.add_argument("path", type=Path); wheel.add_argument("--python", default=sys.executable)
    args = p.parse_args()
    try:
        if args.command == "verify-inputs": print(json.dumps(verify_inputs(), indent=2))
        elif args.command == "prepare": print(prepare(args.destination))
        elif args.command == "regression": print(regression(args.source, args.python, args.expect))
        elif args.command == "verify-wheel": print(json.dumps(verify_wheel(args.path, args.python), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"instance preparation failed: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
