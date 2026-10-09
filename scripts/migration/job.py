#!/usr/bin/env python3
"""Detached runner: persists PID, logs and exit receipt even if coordinator exits."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from orchestrate import atomic
from usage import log_usage
from providers import agy_outcome, preflight, snapshot, permission_preflight

job_path = Path(sys.argv[1])
job = json.loads(job_path.read_text())
atomic(str(job_path) + '.pid.json', {'pid': os.getpid(), 'started': time.time()})
start = time.time()
exit_code = 125
error = None
provider = job.get('provider', 'codex')
metadata = dict(provider=provider, model=job.get('model'), role=job.get('role'),
                task=job.get('task'), attempt=job.get('attempt'))
try:
    if provider == 'agy':
        readiness = preflight(job['route'], job['cwd'], job.get('preflight_seconds', 30))
        if readiness['ready'] and job['route'].get('check_permissions'):
            permissions = permission_preflight(job.get('permission_commands', []), job['route'].get('permissions_settings'))
            readiness['permissions'] = permissions
            if not permissions['ready']:
                readiness.update(ready=False, reason='permissions')
        atomic(str(job_path) + '.preflight.json', readiness)
        if not readiness['ready']:
            metadata.update(provider_failure=readiness['reason'], success=False, preflight=str(job_path) + '.preflight.json')
            raise RuntimeError('Gemini preflight failed: ' + readiness['reason'])
    with open(job['prompt']) as prompt, open(job['log'], 'w') as log, open(job.get('stderr', job['log'] + '.stderr'), 'w') as diagnostics:
        command = list(job['command'])
        if provider == 'agy':
            command += ['-p', prompt.read()]
        p = subprocess.Popen(command, cwd=job['cwd'], stdin=prompt if provider == 'codex' else subprocess.DEVNULL,
                             stdout=log, stderr=diagnostics if provider == 'agy' else subprocess.STDOUT, start_new_session=True)
        atomic(str(job_path) + '.child.json', {'pid': p.pid, 'started': time.time()})
        try:
            exit_code = p.wait(timeout=job['timeout'])
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            exit_code = 124
except Exception as e:
    error = repr(e)
    if isinstance(e, FileNotFoundError):
        metadata.update(success=False, provider_failure='executable_missing')
finally:
    if provider == 'agy':
        if Path(job['log']).is_file():
            try:
                parsed = agy_outcome(job['log'], job['stderr'], exit_code)
                Path(job['result']).write_text(parsed.pop('response'))
                metadata.update(parsed)
            except Exception as e:
                metadata.update(success=False, parse_error=repr(e))
        try:
            metadata['after_snapshot'] = snapshot(job['cwd'])
        except Exception as e:
            metadata.update(success=False, snapshot_error=repr(e))
    usage = None
    if Path(job['log']).is_file():
        try:
            usage = log_usage(job['log'])
        except (OSError, ValueError, TypeError):
            pass  # Accounting must never suppress the recovery receipt.
    atomic(job['receipt'], {'exit': exit_code, 'error': error, 'started': start,
                            'finished': time.time(), 'log': job['log'], 'result': job['result'], 'usage': usage, **metadata})
