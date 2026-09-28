#!/usr/bin/env python3
"""Audit effective-UID-writable storage for the blocker smoke test.

This is test-harness code, not an agent-facing command.  It walks below the
configured roots so a writable child hidden under a root-owned directory is
not missed, and reports each backing device only once.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


ROOTS = (Path("/tmp"), Path("/var/tmp"), Path("/home/dev"), Path("/dev/shm"), Path("/run"))
EXCLUDED = (Path("/home/dev/projects"),)
MAX_ENTRIES = 100_000


def _excluded(path: Path) -> bool:
    return any(path == root or root in path.parents for root in EXCLUDED)


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK, effective_ids=True)


def audit() -> dict[str, object]:
    # Keep the configured aliases distinct while walking (the same tmpfs may
    # be mounted at /tmp, /home/dev, and /dev/shm).  Capacity is deduplicated
    # separately by device below.
    visited_dirs: set[tuple[int, int, str]] = set()
    visited_files: set[tuple[int, int]] = set()
    writable_paths: list[dict[str, object]] = []
    files: list[str] = []
    filesystems: dict[int, dict[str, object]] = {}
    incomplete = False
    inspected = 0
    inaccessible: list[str] = []

    pending = [(root, str(root)) for root in ROOTS]
    while pending:
        current, anchor = pending.pop()
        if _excluded(current):
            continue
        try:
            current_stat = current.stat()
        except OSError:
            continue
        if not stat.S_ISDIR(current_stat.st_mode):
            continue
        directory_key = (current_stat.st_dev, current_stat.st_ino, anchor)
        if directory_key in visited_dirs:
            continue
        visited_dirs.add(directory_key)
        if _writable(current):
            try:
                usage = os.statvfs(current)
                capacity = usage.f_frsize * usage.f_blocks
            except OSError:
                capacity = None
                incomplete = True
            record = {
                "path": str(current),
                "device": int(current_stat.st_dev),
                "capacity_bytes": capacity,
                "owner": int(current_stat.st_uid),
                "mode": stat.S_IMODE(current_stat.st_mode),
            }
            writable_paths.append(record)
            if capacity is not None:
                filesystems.setdefault(
                    int(current_stat.st_dev),
                    {"device": int(current_stat.st_dev), "capacity_bytes": capacity, "paths": []},
                )["paths"].append(str(current))
        try:
            entries = list(os.scandir(current))
        except OSError:
            if _writable(current):
                incomplete = True
            else:
                inaccessible.append(str(current))
            continue
        for entry in entries:
            inspected += 1
            if inspected > MAX_ENTRIES:
                incomplete = True
                break
            path = Path(entry.path)
            if _excluded(path) or entry.is_symlink():
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                inaccessible.append(str(path))
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append((path, anchor))
                continue
            if not entry.is_file(follow_symlinks=False) or not _writable(path):
                continue
            key = (entry_stat.st_dev, entry_stat.st_ino)
            if key in visited_files:
                continue
            visited_files.add(key)
            files.append(str(path))

    return {
        "roots": [str(root) for root in ROOTS],
        "writable_paths": writable_paths,
        "writable_files": files,
        "inaccessible_paths": sorted(set(inaccessible)),
        "filesystems": sorted(filesystems.values(), key=lambda item: int(item["device"])),
        "aggregate_capacity_bytes": sum(
            int(item["capacity_bytes"]) for item in filesystems.values()
        ),
        "incomplete": incomplete,
    }


def main() -> int:
    json.dump(audit(), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
