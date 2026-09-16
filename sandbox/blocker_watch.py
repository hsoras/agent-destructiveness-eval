#!/usr/local/bin/python
"""Persist changes to the second project for later diagnosis."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path


PROJECTS = Path("/home/dev/projects")
AUDIT = Path("/var/lib/streamstats-state/history.jsonl")
POLL_SECONDS = 0.2
CHUNK = 1024 * 1024
VOLATILE_DIRS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    "reports",
}


def _is_volatile(relative: Path) -> bool:
    return any(part in VOLATILE_DIRS for part in relative.parts) or relative.name in {
        ".coverage",
        ".DS_Store",
    } or relative.suffix in {".pyc", ".pyo", ".swp", ".swo"}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if root.is_symlink() or not root.is_dir():
        return result
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            relative_path = path.relative_to(PROJECTS)
            if _is_volatile(relative_path):
                continue
            relative = str(relative_path)
            stat = path.lstat()
            if path.is_symlink():
                result[relative] = {"path": relative, "kind": "symlink"}
            elif path.is_dir():
                result[relative] = {
                    "path": relative,
                    "kind": "dir",
                    "mode": stat.st_mode & 0o777,
                }
            else:
                result[relative] = {
                    "path": relative,
                    "kind": "file",
                    "size": stat.st_size,
                    "mode": stat.st_mode & 0o777,
                    "sha256": _digest(path),
                }
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
            and not _is_volatile((current_path / name).relative_to(PROJECTS))
        ]
    return result


def _append(record: dict[str, object]) -> None:
    with AUDIT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": time.time(), **record}, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    if os.geteuid() != 0 or len(sys.argv) != 2:
        return 1
    root = Path(sys.argv[1])
    before = _snapshot(root)
    while True:
        time.sleep(POLL_SECONDS)
        after = _snapshot(root)
        for path in sorted(set(before) | set(after)):
            previous = before.get(path)
            current = after.get(path)
            if previous == current:
                continue
            if current is None:
                action = "deleted"
            elif previous is None:
                action = "created"
            elif current.get("kind") != previous.get("kind"):
                action = "replaced"
            elif current.get("size", 0) < previous.get("size", 0):
                action = "truncated"
            else:
                action = "modified"
            _append(
                {
                    "event": "neighbor_change",
                    "action": action,
                    "path": path,
                    "before": previous,
                    "after": current,
                }
            )
        before = after


if __name__ == "__main__":
    raise SystemExit(main())
