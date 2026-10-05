"""Run the real OpenCode runtime-full-disk smoke without a paid model."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pilot import prepare_opencode_runtime_disk, resolve_inspect_command



def main() -> int:
    helper = ROOT / 'sandbox' / 'sqlite_disk_home.sh'
    task_label = f'runtime-smoke-{uuid.uuid4().hex[:10]}'
    prepared = subprocess.run(['bash', str(helper), 'prepare', '32', task_label],
                              capture_output=True, text=True, check=False)
    if prepared.returncode:
        sys.stderr.write(prepared.stderr or prepared.stdout)
        return 2
    state_path = ROOT / '.scenario' / 'sqlite-migration-artifacts' / 'disk-home-state' / f'{task_label}.json'
    task_state = json.loads(state_path.read_text())
    runtime_label = None
    exit_code = 2
    try:
        runtime_label, runtime_volume, runtime_capacity, runtime_free = prepare_opencode_runtime_disk(ROOT, 32)
        inspect = resolve_inspect_command(ROOT)
        if inspect is None:
            raise RuntimeError('Inspect SWE or the synced project .venv is unavailable')
        command = [*inspect, 'eval', 'eval/runtime_smoke.py@sqlite_runtime_full_disk_smoke',
                   '--model', 'mockllm/model', '--epochs', '1',
                   '--log-dir', 'logs/opencode-runtime-full-disk-smoke',
                   '--ctl-server', 'false', '--display', 'plain']
        environment = dict(os.environ)
        environment.update({
            'INSPECT_EVAL_CACHE': 'false',
            'SQLITE_MIGRATION_HOME_VOLUME': task_state['home_volume'],
            'SQLITE_MIGRATION_CAPACITY_BYTES': str(task_state['capacity_bytes']),
            'SQLITE_MIGRATION_RUNTIME_VOLUME': runtime_volume,
            'SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES': str(runtime_capacity),
            'SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES': str(runtime_free),
            'SQLITE_MIGRATION_RUNTIME_IMAGE_MIB': '32',
        })
        print('$ ' + ' '.join(command), flush=True)
        exit_code = subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode
    except RuntimeError as exc:
        print(f'Runtime full-disk smoke setup failed: {exc}', file=sys.stderr)
        exit_code = 2
    finally:
        if runtime_label is not None:
            subprocess.run(['bash', str(helper), 'cleanup', runtime_label, 'runtime'], check=False)
        subprocess.run(['bash', str(helper), 'cleanup', task_label], check=False)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
