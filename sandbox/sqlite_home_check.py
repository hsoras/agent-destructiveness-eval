#!/usr/bin/env python3
"""Check the externally prepared task filesystem before launching tools."""

import os
import subprocess
import stat
from pathlib import Path

mount = Path('/home/dev')
expected = int(os.environ['SQLITE_MIGRATION_CAPACITY_BYTES'])
info = os.statvfs(mount)
capacity = info.f_blocks * info.f_frsize
filesystem = subprocess.run(
    ['findmnt', '-n', '-o', 'FSTYPE', '--target', str(mount)],
    check=True, capture_output=True, text=True,
).stdout.strip().lower()
if filesystem in {'tmpfs', 'ramfs', 'overlay'}:
    raise SystemExit(f'home must be disk-backed; found {filesystem}')
if abs(capacity - expected) > max(16 * 1024 * 1024, expected // 100):
    raise SystemExit(f'home capacity mismatch: expected={expected} actual={capacity} filesystem={filesystem}')
tmp_stat = os.stat('/tmp')
if not Path('/tmp').is_dir() or tmp_stat.st_uid != 1000 or stat.S_IMODE(tmp_stat.st_mode) != 0o1777:
    raise SystemExit('/tmp must resolve to a user-owned directory with mode 1777')
if os.stat(mount).st_uid != 1000:
    raise SystemExit('/home/dev must be owned by the task user')
print(f'home_storage filesystem={filesystem} capacity_bytes={capacity}', flush=True)
