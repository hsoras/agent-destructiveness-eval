#!/usr/bin/env python3
"""Stop user-owned task processes and flush writes before protected checks."""

import os
from pathlib import Path
import signal
import time


def user_processes():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in {0, 1, os.getpid()}:
            continue
        try:
            status = (entry / 'status').read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith('Uid:'))
            if int(uid_line.split()[1]) == 1000:
                found.append(pid)
        except (OSError, StopIteration, ValueError):
            continue
    return found


pids = user_processes()
for pid in pids:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
end = time.monotonic() + 5
while time.monotonic() < end and user_processes():
    time.sleep(0.1)
remaining = user_processes()
for pid in remaining:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
os.sync()
print({'terminated': pids, 'killed': remaining})
