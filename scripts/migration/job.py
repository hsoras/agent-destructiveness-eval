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
from worker_environment import EnvironmentUnavailable, host_readiness, create_project, RESULT_SCHEMA, valid_handoff
from providers import agy_outcome, preflight, snapshot, permission_preflight, effective_permissions

job_path = Path(sys.argv[1])
job = json.loads(job_path.read_text())
atomic(str(job_path) + '.pid.json', {'pid': os.getpid(), 'started': time.time()})
start = time.time()
exit_code = 125
error = None
provider = job.get('provider', 'codex')
metadata = dict(provider=provider, model=job.get('model'), role=job.get('role'),
                task=job.get('task'), attempt=job.get('attempt'))
project = None
guest = None
try:
    if provider == 'agy':
        if job['route'].get('scoped_worker_permissions'):
            host_readiness()
            if '--sandbox' not in job['command'] or '--dangerously-skip-permissions' in job['command']:
                raise EnvironmentUnavailable('permissions', 'Scoped worker requires internal terminal sandbox')
        readiness = preflight(job['route'], job['cwd'], job.get('preflight_seconds', 30))
        if readiness['ready'] and job['route'].get('scoped_worker_permissions'):
            clients = []
            protected = [job_path, job['prompt'], job['receipt'], job['result'], job['log'], job['stderr'],
                         str(job_path)+'.preflight.json']
            if job['route'].get('docker_guest'):
                from docker_guest import GuestBroker
                guest = GuestBroker(job['cwd'], str(job_path)+'.guest', job['route']['docker_guest'])
                clients = guest.client_commands()
                protected += [guest.receipts, guest.responses, guest.directory/'environment.json',
                              guest.directory/'summary.json']
            project = create_project(job['cwd'], job.get('permission_commands', []),
                                     client_commands=clients, protected=protected,
                                     request_directory=guest.requests if guest else None,
                                     response_directory=guest.responses if guest else None)
            metadata['worker_project'] = project
            if guest:
                guest.start()
                metadata['guest_environment'] = str(guest.directory/'environment.json')
        if readiness['ready'] and (job['route'].get('check_permissions') or project):
            settings = job['route'].get('permissions_settings')
            default_settings = Path.home() / '.gemini/antigravity-cli/settings.json'
            if settings and Path(settings).resolve() != default_settings.resolve():
                permissions = dict(ready=False, reason='permissions',
                                   error='permissions_settings does not configure agy; alternate policy files are unsupported')
            else:
                effective = effective_permissions(job['cwd'], job.get('preflight_seconds', 30), project=project)
                permissions = (permission_preflight(project['commands'] if project else job.get('permission_commands', []),
                                                    permissions=effective['permissions'])
                               if effective['ready'] else dict(effective))
                permissions['effective_policy'] = effective
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
            instructions = prompt.read()
            if project:
                command += ['--project', project['id'], '--json-schema', json.dumps(RESULT_SCHEMA)]
                instructions += ('\nScoped worker execution policy: run only these development commands, '
                                 'as separate calls with the assigned worktree Cwd: '+json.dumps(project['commands'])+
                                 '. Other required checks belong to the trusted coordinator. '
                                 'Use native file tools to read/edit; do not run Git, Docker, orb, change '
                                 'permissions, retry outside the sandbox, or access the host daemon. '
                                 'Leave edits uncommitted: coordinator validation and independent review remain mandatory. '
                                 'The guest client is the only permitted Docker interface. Report exact command evidence.')
            metadata['execution_command'] = list(command)
            command += ['-p', instructions]
        p = subprocess.Popen(command, cwd=job['cwd'], stdin=prompt if provider == 'codex' else subprocess.DEVNULL,
                             stdout=log, stderr=diagnostics if provider == 'agy' else subprocess.STDOUT, start_new_session=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1') if project else None)
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
    if isinstance(e, EnvironmentUnavailable):
        metadata.update(success=False, provider_failure=e.reason)
    if isinstance(e, FileNotFoundError):
        metadata.update(success=False, provider_failure='executable_missing')
finally:
    if guest is not None:
        metadata['guest_summary'] = str(guest.directory/'summary.json')
        try:
            guest.close()
        except Exception as cleanup_error:
            guest.error = 'Guest cleanup failed: '+repr(cleanup_error)
        if guest.error:
            metadata.update(success=False, guest_error=guest.error)
    if provider == 'agy':
        if Path(job['log']).is_file():
            try:
                parsed = agy_outcome(job['log'], job['stderr'], exit_code)
                Path(job['result']).write_text(parsed.pop('response'))
                metadata.update(parsed)
                if project and not valid_handoff(metadata.get('structured_output')):
                    metadata.update(success=False, structured_error='Missing/invalid scoped worker handoff')
                if guest is not None and guest.error:
                    metadata['success'] = False
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
