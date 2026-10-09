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

job_path = Path(sys.argv[1])
job = json.loads(job_path.read_text())
atomic(str(job_path) + '.pid.json', {'pid': os.getpid(), 'started': time.time()})
start = time.time()
exit_code = 125
error = None
try:
    with open(job['prompt']) as prompt, open(job['log'], 'w') as log:
        p = subprocess.Popen(job['command'], cwd=job['cwd'], stdin=prompt,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        atomic(str(job_path) + '.child.json', {'pid': p.pid, 'started': start})
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
finally:
    atomic(job['receipt'], {'exit': exit_code, 'error': error, 'started': start,
                            'finished': time.time(), 'log': job['log'], 'result': job['result']})
