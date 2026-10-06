"""Root-only peak allocation monitor for the task and OpenCode ext4 filesystems."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import time
from sqlite_storage_config import configured_storage

HOME = Path('/home/dev')
RUNTIME = HOME / '.local/share/opencode'
OUTPUT = Path('/var/lib/streamstats-telemetry/sqlite-storage-telemetry.json')
PIDFILE = Path('/var/lib/streamstats-telemetry/sqlite-storage-telemetry.pid')
STOP = False

def stop(_signum, _frame):
    global STOP
    STOP = True

def fs(path):
    stat = os.statvfs(path)
    capacity = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    return {'mount_path': str(path),
            'capacity_bytes': capacity, 'free_bytes': free,
            'used_bytes': capacity - stat.f_bfree * stat.f_frsize}

def runtime_allocation():
    total, groups = 0, {}
    for current, dirs, files in os.walk(RUNTIME):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            try:
                allocated = path.stat(follow_symlinks=False).st_blocks * 512
            except OSError:
                continue
            total += allocated
            relative = path.relative_to(RUNTIME)
            group = relative.parts[0] if len(relative.parts) > 1 else relative.name
            groups[group] = groups.get(group, 0) + allocated
    return total, groups

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
PIDFILE.write_text(str(os.getpid()), encoding='ascii')
home_start, runtime_start = fs(HOME), fs(RUNTIME)
if os.stat(HOME).st_dev == os.stat(RUNTIME).st_dev:
    raise SystemExit('task and OpenCode runtime storage must be separate filesystems')
runtime_used, runtime_groups = runtime_allocation()
result = {
    'format': 'sqlite-storage-telemetry-v1', 'sample_interval_ms': 100,
    'configured_storage': configured_storage(),
    'home': {**home_start, 'minimum_free_bytes': home_start['free_bytes'],
             'peak_used_bytes': home_start['used_bytes']},
    'opencode_runtime': {**runtime_start,
        'minimum_free_bytes': runtime_start['free_bytes'],
        'peak_used_bytes': runtime_start['used_bytes'],
        'peak_allocated_bytes': runtime_used,
        'peak_allocated_by_directory_bytes': runtime_groups},
    'samples': 1,
}
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
def save(indent=None):
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, sort_keys=True, indent=indent) + '\n', encoding='utf-8')
    os.replace(temporary, OUTPUT)
    OUTPUT.chmod(0o444)
while not STOP:
    home, runtime = fs(HOME), fs(RUNTIME)
    allocated, groups = runtime_allocation()
    h, r = result['home'], result['opencode_runtime']
    h['minimum_free_bytes'] = min(h['minimum_free_bytes'], home['free_bytes'])
    h['peak_used_bytes'] = max(h['peak_used_bytes'], home['used_bytes'])
    r['minimum_free_bytes'] = min(r['minimum_free_bytes'], runtime['free_bytes'])
    r['peak_used_bytes'] = max(r['peak_used_bytes'], runtime['used_bytes'])
    if allocated > r['peak_allocated_bytes']:
        r['peak_allocated_bytes'] = allocated
        r['peak_allocated_by_directory_bytes'] = groups
    result['samples'] += 1
    result['last_sample_unix'] = time.time()
    save()
    time.sleep(0.1)
result['stopped_unix'] = time.time()
save(indent=2)
PIDFILE.unlink(missing_ok=True)
