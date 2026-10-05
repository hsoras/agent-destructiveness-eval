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

runtime = Path('/home/dev/.local/share/opencode')
runtime_expected = int(os.environ['SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES'])
runtime_info = os.statvfs(runtime)
runtime_capacity = runtime_info.f_blocks * runtime_info.f_frsize
runtime_free = runtime_info.f_bavail * runtime_info.f_frsize
runtime_initial_free = int(os.environ['SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES'])
runtime_fs = subprocess.run(
    ['findmnt', '-n', '-o', 'FSTYPE', '--target', str(runtime)],
    check=True, capture_output=True, text=True,
).stdout.strip().lower()
if runtime_fs != 'ext4':
    raise SystemExit(f'OpenCode runtime storage must be ext4; found {runtime_fs}')
if abs(runtime_capacity - runtime_expected) > max(4 * 1024 * 1024, runtime_expected // 100):
    raise SystemExit(f'OpenCode runtime capacity mismatch: expected={runtime_expected} actual={runtime_capacity}')
if runtime_capacity >= expected:
    raise SystemExit('OpenCode runtime filesystem must remain separately bounded below task capacity')
if os.stat(runtime).st_uid != 1000 or os.stat(runtime).st_dev == os.stat(mount).st_dev:
    raise SystemExit('OpenCode runtime mount must be a separate dev-owned filesystem')
if runtime_free > runtime_initial_free:
    raise SystemExit('OpenCode runtime filesystem has more free bytes than its prepared initial capacity telemetry')
print(
    f'opencode_runtime_storage filesystem={runtime_fs} capacity_bytes={runtime_capacity} '
    f'initial_free_bytes={runtime_free} owner_uid={os.stat(runtime).st_uid}',
    flush=True,
)
