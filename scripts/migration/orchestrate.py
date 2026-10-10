#!/usr/bin/env python3
"""Offline Git coordinator for bounded Codex CLI migration workers (stdlib only)."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from providers import snapshot, validate_routing

HERE = Path(__file__).resolve().parent
WORKER = 'gpt-6-luna'
REVIEWER = 'gpt-6.1-sol'
PROTECTED = ('docs/migration/', 'NEW_SPEC.md', 'scripts/migration/')
MAX_ADDITIONAL_REPAIRS = 6
ROUTINE_DECISION_GUIDANCE = (
    'The user has authorized routine NEW_SPEC-consistent technical selections, including '
    'G02/G03 candidate and release decisions, during this unattended run. Record concrete '
    'evidence and leave commit-specific T001/T002 ratification to the coordinator after '
    'independent review. This does not authorize runtime pin changes, upstream patches, '
    'production deployment or database operations, destructive Git operations, merging '
    'into main, or weakening tests, independent review, or acceptance evidence. '
    'No provider-backed local acceptance calls are authorized by this guidance.\n'
)

class Stop(RuntimeError):
    pass

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def run(argv, cwd, check=True):
    p = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise Stop(f"Command failed ({p.returncode}): {shlex.join(argv)}\n{p.stderr[-4000:]}")
    return p

def git(cwd, *args, check=True):
    return run(['git', *args], cwd, check).stdout.strip()

def clean(cwd):
    if git(cwd, 'status', '--porcelain', '--untracked-files=all'):
        raise Stop(f'Dirty worktree: {cwd}; preserve changes and inspect manually')

def ancestor(cwd, a, b):
    return run(['git', 'merge-base', '--is-ancestor', a, b], cwd, False).returncode == 0

def digest(data):
    return hashlib.sha256(data.encode()).hexdigest()

def pytest_evidence(report, cwd, exit_code, exceptions):
    """Audit every collected result; allow only an exact approved baseline failure."""
    tree = ET.parse(report)
    cases = tree.findall('.//testcase')
    if not cases or tree.findall('.//error') or tree.findall('.//skipped'):
        raise Stop('Missing tests, collection errors or skipped required tests')
    failures = tree.findall('.//failure')
    if exit_code != (1 if failures else 0):
        raise Stop('Unexpected pytest exit status')
    accepted = []
    for case in cases:
        failed = case.findall('failure')
        if not failed:
            continue
        identity = (case.get('classname'), case.get('name'))
        candidates = [e for e in exceptions if (e['classname'], e['name']) == identity]
        if len(candidates) != 1 or len(failed) != 1 or identity in accepted:
            raise Stop(f'Unapproved or duplicate pytest failure: {identity}')
        exception = candidates[0]
        failure = failed[0]
        signature = digest(json.dumps({'message': failure.get('message', ''),
                                       'text': (failure.text or '').replace(str(Path(cwd).resolve()), '{repo}')}, sort_keys=True))
        if signature != exception['failure_sha256']:
            raise Stop(f'Baseline failure signature changed: {identity}')
        for name, expected in exception['source_sha256'].items():
            if hashlib.sha256((Path(cwd) / name).read_bytes()).hexdigest() != expected:
                raise Stop(f'Baseline failure source changed: {name}')
        accepted.append(identity)
    if len(accepted) != len(failures):
        raise Stop('Unaccounted pytest failures')
    return dict(tests_passed=len(cases) - len(failures), tests_failed=len(failures),
                accepted_baseline_exceptions=[f'{c}::{n}' for c, n in accepted], junit=str(report))

def section(text, n):
    m = re.search(rf'^## {n}\. .*?\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if not m:
        raise Stop(f'Missing task field {n}')
    return m[1].strip()

def focused_context(repo, task, spec):
    """Exact owner clauses plus whole referenced top-level sections; fail open to full spec."""
    matrix = (repo / 'docs/migration/REQUIREMENTS_MATRIX.md').read_text()
    rows = [line for line in matrix.splitlines()
            if re.search(rf'\[{task["id"]} AC\d+\]\(tasks/{task["id"]}\.md', line)]
    requirements = section(task['text'], 3)
    refs = re.search(r'exact sections ([\d.,–\- ]+)', requirements)
    headings = list(re.finditer(r'^## (\d+)\. .*$', spec, re.M))
    chosen = set()
    if refs and 'all-section audit' not in requirements:
        for ref in refs[1].strip(' .').split(','):
            ends = re.split('[–-]', ref.strip())
            chosen.update(range(int(ends[0].split('.')[0]), int(ends[-1].split('.')[0]) + 1))
    if chosen and rows and chosen.issubset({int(h[1]) for h in headings}):
        chunks = [spec[:headings[0].start()]]
        for i, h in enumerate(headings):
            if int(h[1]) in chosen:
                chunks.append(spec[h.start():headings[i+1].start() if i+1 < len(headings) else len(spec)])
        excerpt = ''.join(chunks)
    else:
        excerpt = spec  # Unknown format or all-section audit must never omit requirements.
    return ('\nAuthoritative spec context (whole referenced top-level sections; full file at docs/migration/NEW_SPEC.md):\n'
            + excerpt + '\nExact assigned requirements (complete source clauses, no summarized criteria):\n'
            + ('\n'.join(rows) if rows else matrix) + '\n')

def status_projection(state):
    records = state.get('records', {})
    return dict(blocked=(str(state.get('blocked'))[:600] if state.get('blocked') else None),
                tasks={tid: {k: r.get(k) for k in ('phase', 'attempt', 'commit', 'provider', 'model')}
                       for tid, r in records.items()},
                assigned_records_complete=bool(records) and all(r['phase'] == 'merged' for r in records.values()),
                evidence='state.json and tasks/ contain complete findings and logs')


def state_event(state):
    """Terminal decisions only: intermediate process/commit/checkpoint churn is silent."""
    terminal = {'merged', 'rejected', 'provider_blocked', 'integration_testing', 'merge_pending'}
    # Integration phases wake only with a blocker, not merely because a merge started.
    return dict(blocked=state.get('blocked'), tasks={tid: {
        k: r.get(k) for k in ('phase', 'attempt', 'commit')}
        for tid, r in state.get('records', {}).items()
        if r['phase'] in terminal - {'integration_testing', 'merge_pending'}},
        assigned_records_complete=status_projection(state)['assigned_records_complete'])


def watch_state(directory, timeout=1800):
    """Read-only bounded terminal-event wait, without model calls or mutations."""
    if not 0 <= timeout <= 86400:
        raise Stop('Watch timeout must be between zero and 86400 seconds')
    path = Path(directory) / 'state.json'
    if not path.is_file():
        raise Stop('No execution state to watch')
    first = state_event(json.loads(path.read_text()))
    deadline = time.monotonic() + timeout
    while True:
        state = json.loads(path.read_text())
        current = state_event(state)
        result = status_projection(state)
        if current != first or current['blocked'] or current['assigned_records_complete']:
            return result
        if time.monotonic() >= deadline:
            return dict(event='timeout', state=result)
        time.sleep(min(.25, max(0, deadline - time.monotonic())))


def read_graph(repo):
    rows = []
    for line in (repo / 'docs/migration/STATUS.md').read_text().splitlines():
        if not re.match(r'\| \[T\d{3}\]', line):
            continue
        c = [s.strip() for s in line.strip('|').split('|')]
        tid = re.search(r'T\d{3}', c[0])[0]
        text = (repo / f'docs/migration/tasks/{tid}.md').read_text()
        deps = re.findall(r'T\d{3}', c[2])
        if deps != re.findall(r'T\d{3}', section(text, 4)):
            raise Stop(f'{tid}: STATUS/brief dependency mismatch')
        rows.append(dict(id=tid, title=c[1], deps=deps, wave=int(c[3]), status=c[4], text=text))
    ids = {r['id'] for r in rows}
    if len(ids) != len(rows) or not rows:
        raise Stop('Empty or duplicate task graph')
    for r in rows:
        for dep in r['deps']:
            if dep not in ids or next(x for x in rows if x['id'] == dep)['wave'] >= r['wave']:
                raise Stop(f'Invalid dependency/wave: {r["id"]} -> {dep}')
    return rows

def owns(path, scopes):
    return any(path == s or (s.endswith('/') and path.startswith(s)) for s in scopes)

def overlap(a, b):
    return any(owns(x, b) or owns(y, a) for x in a for y in b)

def select(tasks, records, scopes, max_workers=1):
    unfinished = [t for t in tasks if records.get(t['id'], {}).get('phase') != 'merged']
    if not unfinished:
        return []
    wave = min(t['wave'] for t in unfinished)
    selected = []
    for t in unfinished:
        if t['wave'] != wave or t['id'] in records:
            continue
        if not all(records.get(d, {}).get('phase') == 'merged' for d in t['deps']):
            continue
        if any(overlap(scopes[t['id']], scopes[x['id']]) for x in selected):
            continue
        selected.append(t)
        if len(selected) == max_workers:
            break
    return selected

def config_template(repo):
    tasks = read_graph(repo)
    scopes, checks = {}, {}
    for t in tasks:
        line = re.search(r'\*\*Proposed owned paths:\*\* (.*?) These new paths', t['text'])[1]
        paths = re.findall(r'(?<![\w/])(?:[\w.-]+/)+[\w.-]*|(?<![\w/])(?:README\.md|pyproject\.toml|uv\.lock)', line)
        # The prose cannot name the chosen npm lockfile; explicitly bound known candidates.
        if t['id'] == 'T001':
            paths += ['runtime/artifact_upload_host/package-lock.json', 'runtime/artifact_upload_host/pnpm-lock.yaml', 'runtime/artifact_upload_host/yarn.lock']
        scopes[t['id']] = list(dict.fromkeys(p.rstrip('.') for p in paths if p != 'router/runtime'))
        code = re.findall(r'```sh\n(.*?)```', section(t['text'], 9), re.S)
        checks[t['id']] = [shlex.split(l) for block in code for l in block.splitlines() if l.strip()]
    return dict(worker_model=WORKER, reviewer_model=REVIEWER, integration_branch='migration/artifact-upload',
                python='{repo}/.venv/bin/python', max_repairs=2, timeout_seconds=1800,
                scopes=scopes, checks=checks, exclusions={'T008': scopes['T001']},
                regression_checks=[['{python}', '-m', 'pytest', '-q', 'tests']],
                manual_gates=['T001', 'T002'],
                note='Review exact scopes/checks before execution. Add named evidence fixture paths explicitly; no broad ownership inferred from prose.')

@contextlib.contextmanager
def lock(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop('Another coordinator owns this run')
        yield

class Coordinator:
    def __init__(self, repo, state_dir, config, max_workers=1, routing=None):
        self.repo, self.directory = Path(repo).resolve(), Path(state_dir).resolve()
        self.config = config
        self.routing = validate_routing(routing) if routing else None
        if max_workers not in (1, 2):
            raise Stop('Worker concurrency must be one or two')
        # Runtime scheduling policy; do not invalidate the historical config digest.
        self.max_workers = max_workers
        self.processes = {}
        common = Path(git(self.repo, 'rev-parse', '--git-common-dir'))
        common = (common if common.is_absolute() else self.repo / common).resolve()
        # Never put writable coding workspaces inside the shared protected .git directory.
        default_workspace = self.repo.parent / ('.' + self.repo.name + '-migration-worktrees') if self.directory.is_relative_to(common) else self.directory
        self.file = self.directory / 'state.json'
        self.state = json.loads(self.file.read_text()) if self.file.exists() else None
        self.workspaces = Path(self.state.get('workspace_root', str(default_workspace))) if self.state else default_workspace
        if self.state and (self.state['repo'] != str(self.repo) or self.state['config_digest'] != digest(json.dumps(config, sort_keys=True))):
            raise Stop('Repository/config differs from recorded run; do not silently alter a resumed run')

    def save(self):
        atomic(self.file, self.state)

    def repair_limit(self, record):
        limit = 1 + self.config['max_repairs']
        authorizations = record.get('repair_authorizations', [])
        if not isinstance(authorizations, list):
            raise Stop('Malformed repair authorization history')
        for entry in authorizations:
            if not isinstance(entry, dict):
                raise Stop('Malformed repair authorization history')
            grant = entry.get('additional_attempts')
            previous, new = entry.get('previous_limit'), entry.get('new_limit')
            attempt = entry.get('attempt_at_authorization')
            when = entry.get('time')
            if (not isinstance(grant, int) or isinstance(grant, bool) or not 1 <= grant <= 2 or
                    not isinstance(previous, int) or isinstance(previous, bool) or previous != limit or
                    not isinstance(new, int) or isinstance(new, bool) or new != previous + grant or
                    not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < previous or attempt >= new or
                    not isinstance(when, (int, float)) or isinstance(when, bool) or
                    not math.isfinite(when) or when <= 0):
                raise Stop('Inconsistent repair authorization budget history')
            if ('terminal_repair_consumed' in entry and
                    not isinstance(entry['terminal_repair_consumed'], bool)):
                raise Stop('Malformed terminal repair consumption marker')
            limit = new
        if limit > 1 + self.config['max_repairs'] + MAX_ADDITIONAL_REPAIRS:
            raise Stop('Cumulative additional repair budget exceeded')
        return limit

    def _current_dirty_repair_authorization(self, task_id, record, authorizations):
        """Validate retained grant history, then select its sole current unconsumed grant."""
        if not isinstance(authorizations, list):
            raise Stop('Malformed repair authorization history')
        history = record.get('repair_authorizations', [])
        if not isinstance(history, list) or any(not isinstance(item, dict) or item.get('task') != task_id for item in history):
            raise Stop('Repair authorization history task mismatch')
        self.repair_limit(record)
        terminal_history = [item for item in history if 'terminal_check_repair' in item]
        if any(not isinstance(item.get('terminal_check_repair'), dict) or
               item.get('phase') is not None for item in terminal_history):
            raise Stop('Malformed historical terminal repair authorization')
        attempt = record.get('attempt')
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            raise Stop('Dirty terminal repair authorization attempt mismatch')
        candidates = [item for item in terminal_history
                      if item.get('attempt_at_authorization') == attempt and
                      item.get('terminal_repair_consumed', False) is False]
        if len(candidates) != 1:
            raise Stop('Dirty terminal repair requires exactly one current authorization')
        entry = candidates[0]
        if not isinstance(entry.get('terminal_repair_consumed', False), bool):
            raise Stop('Malformed terminal repair consumption marker')
        attempt = record.get('attempt')
        granted_at = entry.get('attempt_at_authorization')
        if (not isinstance(attempt, int) or isinstance(attempt, bool) or
                not isinstance(granted_at, int) or isinstance(granted_at, bool) or granted_at != attempt):
            raise Stop('Dirty terminal repair authorization attempt mismatch')
        consumed = entry.get('terminal_repair_consumed', False)
        if not isinstance(consumed, bool) or consumed:
            raise Stop('Dirty terminal repair authorization is consumed or malformed')
        if entry.get('phase') is not None:
            raise Stop('Malformed dirty terminal repair authorization')
        if not isinstance(entry.get('terminal_check_repair'), dict):
            raise Stop('Dirty terminal repair authorization lacks terminal proof')
        position = next(index for index, candidate in enumerate(history) if candidate is entry)
        expected_previous = 1 + self.config['max_repairs']
        for prior in history[:position]:
            expected_previous = prior['new_limit']
        if entry['previous_limit'] != expected_previous:
            raise Stop('Dirty terminal repair grant does not follow the authorization chain')
        grant = entry['additional_attempts']
        if entry['new_limit'] != entry['previous_limit'] + grant or not entry['previous_limit'] <= attempt < entry['new_limit']:
            raise Stop('Dirty terminal repair grant has inconsistent bounds')
        if entry['new_limit'] > 1 + self.config['max_repairs'] + MAX_ADDITIONAL_REPAIRS:
            raise Stop('Dirty terminal repair grant exceeds the cumulative ceiling')
        return entry

    def authorize_repairs(self, task_id, additional_attempts, reason, execution_guidance=None):
        """Record a bounded, task-specific extension without changing frozen run inputs."""
        if not self.state or task_id not in self.state['records']:
            raise Stop('Unknown assigned task')
        if not isinstance(additional_attempts, int) or isinstance(additional_attempts, bool) or not 1 <= additional_attempts <= 2:
            raise Stop('Authorize one or two additional attempts at a time')
        if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
            raise Stop('Provide a concrete 20–2000 character repair reason')
        if execution_guidance not in (None, 'routine-decisions'):
            raise Stop('Unknown execution guidance')
        if self.inputs(read_graph(self.repo)) != self.state['input_digest']:
            raise Stop('Approved task/spec/plan inputs changed; no repair authorization')
        if git(self.integration, 'rev-parse', 'HEAD') != self.state['expected_head']:
            raise Stop('Integration HEAD changed outside coordinator')
        if git(self.integration, 'branch', '--show-current') != self.state['branch']:
            raise Stop('Integration branch changed outside coordinator')
        clean(self.integration)
        record = self.state['records'][task_id]
        if record['attempt'] < self.repair_limit(record):
            raise Stop('Extend only a task after its existing repair budget is exhausted')
        worktree = Path(record['worktree'])
        if (git(worktree, 'branch', '--show-current') != record['branch'] or
                git(worktree, 'rev-parse', 'HEAD') != (record['commit'] or record['base'])):
            raise Stop('Worker branch/HEAD changed; inspect before authorizing repair')
        granted = sum(entry['additional_attempts'] for entry in record.get('repair_authorizations', []))
        if granted + additional_attempts > MAX_ADDITIONAL_REPAIRS:
            raise Stop('Cumulative additional repair budget exceeded')
        if any(e['reason'].strip() == reason.strip() for e in record.get('repair_authorizations', [])):
            raise Stop('Repeated repair reason; consolidate findings and provide new evidence')
        evidence = None
        if record['phase'] == 'worker_finished':
            evidence = self._terminal_check_repair_evidence(task_id, record)
        elif record['phase'] != 'rejected':
            raise Stop('Extend only a rejected task or a terminal branch-check failure after budget exhaustion')
        prior_guidance = self.state.get('execution_guidance')
        if prior_guidance and execution_guidance and prior_guidance != execution_guidance:
            raise Stop('Existing execution guidance cannot be replaced')
        entry = dict(time=time.time(), task=task_id, previous_limit=self.repair_limit(record),
                     additional_attempts=additional_attempts, new_limit=self.repair_limit(record) + additional_attempts,
                     attempt_at_authorization=record['attempt'], reason=reason.strip(),
                     execution_guidance=execution_guidance)
        if evidence:
            entry['terminal_check_repair'] = evidence
        record.setdefault('repair_authorizations', []).append(entry)
        if execution_guidance:
            self.state['execution_guidance'] = execution_guidance
        self.save()
        return entry

    def _terminal_check_repair_evidence(self, task_id, record):
        """Verify the narrow retained evidence path for a worker stopped at branch checks."""
        if record.get('review') or record.get('gate_commit'):
            raise Stop('Terminal branch-check repair requires an unreviewed worker without a gate commit')
        if record.get('commit') and record.get('commit') != git(record['worktree'], 'rev-parse', 'HEAD'):
            raise Stop('Terminal worker commit no longer matches its current branch HEAD')
        if record.get('phase') != 'worker_finished':
            raise Stop('Not a terminal worker result')
        tasks_root = self.directory / 'tasks'
        task_root = tasks_root / task_id
        if tasks_root.is_symlink() or task_root.is_symlink():
            raise Stop('Symlink in retained task evidence directory')
        task_dir = task_root.resolve()
        if not task_dir.is_relative_to(tasks_root.resolve()):
            raise Stop('Task evidence directory escapes coordinator state')
        def retained(raw, base=task_dir):
            if not isinstance(raw, str) or not raw:
                raise Stop('Missing retained terminal evidence path')
            p = Path(raw)
            if not p.is_absolute():
                p = base / p
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(task_dir):
                raise Stop('Terminal evidence escapes its retained task directory')
            if any(parent.is_symlink() for parent in p.parents if parent != task_dir.parent):
                raise Stop('Symlink in retained evidence path')
            return p.resolve()
        job_path = retained(record.get('job'))
        expected_job_name = f'worker-{record["attempt"]}-job.json'
        if job_path.name != expected_job_name or job_path != (task_dir / expected_job_name).resolve():
            raise Stop('Worker job is not the exact current attempt')
        job_mtime = job_path.stat().st_mtime
        if not math.isfinite(job_mtime) or job_mtime <= 0:
            raise Stop('Current worker job launch time is malformed')
        try:
            job = json.loads(job_path.read_text())
        except (OSError, ValueError) as e:
            raise Stop('Malformed current worker job') from e
        if not isinstance(job, dict):
            raise Stop('Malformed current worker job')
        if (job.get('task') != task_id or job.get('role') != 'worker' or not isinstance(job.get('cwd'), str) or
                not isinstance(job.get('attempt'), int) or isinstance(job.get('attempt'), bool) or job.get('attempt') != record['attempt'] or
                Path(job.get('cwd', '')).resolve() != Path(record['worktree']).resolve()):
            raise Stop('Current worker job identity mismatch')
        receipt_path = retained(job.get('receipt'))
        expected_receipt_name = f'worker-{record["attempt"]}-exit.json'
        if receipt_path.name != expected_receipt_name or receipt_path != (task_dir / expected_receipt_name).resolve():
            raise Stop('Worker terminal receipt is not the exact current attempt')
        expected_log_path = (task_dir / f'worker-{record["attempt"]}.jsonl').resolve()
        expected_result_path = (task_dir / f'worker-{record["attempt"]}-result.txt').resolve()
        if job.get('log') is None or retained(job['log']) != expected_log_path:
            raise Stop('Current worker job log identity mismatch')
        if job.get('result') is None or retained(job['result']) != expected_result_path:
            raise Stop('Current worker job result identity mismatch')
        try:
            receipt = json.loads(receipt_path.read_text())
        except (OSError, ValueError) as e:
            raise Stop('Malformed worker terminal receipt') from e
        if not isinstance(receipt, dict):
            raise Stop('Malformed worker terminal receipt')
        started, finished = receipt.get('started'), receipt.get('finished')
        receipt_attempt = receipt.get('attempt')
        if (receipt.get('task') != task_id or receipt.get('role') != 'worker' or
                not isinstance(receipt_attempt, int) or isinstance(receipt_attempt, bool) or receipt_attempt != record['attempt'] or
                not isinstance(started, (int, float)) or isinstance(started, bool) or
                not isinstance(finished, (int, float)) or isinstance(finished, bool) or
                not (math.isfinite(started) and math.isfinite(finished) and 0 < started <= finished) or
                receipt.get('provider') != job.get('provider') or receipt.get('model') != job.get('model') or
                not isinstance(receipt.get('exit'), int) or isinstance(receipt.get('exit'), bool) or receipt.get('exit') != 0 or
                record.get('exit') != receipt or receipt.get('error') or
                receipt.get('success') is False or receipt.get('provider_failure') or receipt.get('parse_error') or
                receipt.get('timeout') or receipt.get('launch_error')):
            raise Stop('Worker terminal receipt is unsuccessful, malformed or mismatched')
        if job.get('provider') == 'agy' and (receipt.get('success') is not True or receipt.get('terminal_status') != 'SUCCESS'):
            raise Stop('Provider terminal result does not prove successful completion')
        for key in ('log', 'result'):
            if receipt.get(key) != job.get(key):
                raise Stop('Worker terminal receipt log/result identity mismatch')
            retained(receipt[key])
        if retained(receipt['log']) != expected_log_path or retained(receipt['result']) != expected_result_path:
            raise Stop('Worker terminal receipt log/result identity mismatch')
        # All known target process identities must be demonstrably gone. Unknown or denied probes fail closed.
        identities = [(Path(str(job_path) + '.pid.json'), 'pid'),
                      (Path(str(job_path) + '.child.json'), 'pid')]
        if any(not path.is_file() or path.is_symlink() for path, _ in identities):
            raise Stop('Missing current runner/child PID receipt')
        recorded_pid = record.get('pid')
        if not isinstance(recorded_pid, int) or isinstance(recorded_pid, bool) or recorded_pid <= 0:
            raise Stop('Uncertain current worker launch identity')
        prev_started = None
        for index, (path, field) in enumerate(identities):
            if path.exists() or path.is_symlink():
                p = retained(str(path))
                try:
                    meta = json.loads(p.read_text())
                except (OSError, ValueError) as e:
                    raise Stop('Malformed target process identity') from e
                if not isinstance(meta, dict):
                    raise Stop('Malformed target process identity')
                pid = meta.get(field)
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                    raise Stop('Malformed target process identity')
                proc_started = meta.get('started')
                if (not isinstance(proc_started, (int, float)) or isinstance(proc_started, bool) or
                        not math.isfinite(proc_started)):
                    raise Stop('Target process started metadata is malformed or out of order')
                # job.py writes runner metadata before it captures receipt.started;
                # it writes child metadata after that capture and before finished.
                in_launch_interval = (job_mtime <= proc_started <= started if index == 0
                                      else started <= proc_started <= finished)
                if (not in_launch_interval or
                        (prev_started is not None and proc_started < prev_started)):
                    raise Stop('Target process started metadata is malformed or out of order')
                prev_started = proc_started
                if path == identities[0][0] and record.get('pid') != pid:
                    raise Stop('Recorded runner PID does not match its retained PID receipt')
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise Stop('Target process liveness is uncertain')
                else:
                    raise Stop('Target runner or child process is still alive')
        try:
            os.kill(recorded_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            raise Stop('Target launch process liveness is uncertain')
        else:
            raise Stop('Target launch process is still alive')
        checks_root = self.directory / 'checks' / f'{task_id}-branch'
        checks_dir = checks_root.resolve()
        if ((self.directory / 'checks').is_symlink() or checks_root.is_symlink() or
                not checks_dir.is_dir() or not checks_dir.is_relative_to(self.directory)):
            raise Stop('Missing retained branch-check evidence')
        matches = []
        for path in checks_dir.glob('*.json'):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(checks_dir):
                continue
            try:
                check = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            if not isinstance(check, dict):
                continue
            log = check.get('log')
            command = check.get('command')
            if isinstance(log, str) and isinstance(command, list) and all(isinstance(x, str) for x in command) and self.state.get('blocked') == (
                    f"{task_id}-branch: required check failed/missing/skipped: " +
                    shlex.join(command) + f"; see {log}"):
                matches.append((path.resolve(), check))
        if len(matches) != 1:
            raise Stop('Blocker does not identify exactly one retained failed branch check')
        check_path, check = matches[0]
        terminal_pytest = (check.get('exit') == 1 and not isinstance(check.get('exit'), bool) and
                           self._terminal_pytest_failure(check, check_path, task_id, finished))
        if (check.get('origin') != 'coordinator' or check.get('passed') is not False or
                not isinstance(check.get('exit'), int) or isinstance(check.get('exit'), bool) or
                (check['exit'] != 2 and not terminal_pytest) or
                (check.get('error') and not terminal_pytest) or check.get('cwd') != str(Path(record['worktree']).resolve()) or
                check.get('source_head') != git(record['worktree'], 'rev-parse', 'HEAD') or
                check.get('source_snapshot_sha256') != digest(json.dumps(snapshot(record['worktree']), sort_keys=True))):
            raise Stop('Failed-check receipt does not match the current coordinator snapshot')
        expected = self.task_checks(task_id)
        if check.get('original_command') not in expected:
            raise Stop('Failed-check command is not a frozen required task check')
        stem = check_path.stem
        template = check['original_command']
        argv = [x.replace('{python}', self.config['python']).replace('$USER', stem).replace('{output}', str(check_path.parent / stem)) for x in template]
        if argv and argv[0] == '.venv/bin/python':
            argv[0] = self.config['python']
        if argv[1:3] == ['-m', 'pytest']:
            argv += ['-o', 'junit_family=xunit2', '--junitxml=' + str(check_path.with_suffix('.xml'))]
        if check.get('command') != argv:
            raise Stop('Failed-check executed command does not match its frozen template')
        if terminal_pytest and argv[1:3] != ['-m', 'pytest']:
            raise Stop('Exit-1 repair evidence is limited to a required pytest command')
        def retained_check(raw):
            p = Path(raw)
            lexical_root = self.directory / 'checks' / f'{task_id}-branch'
            lexical = p.absolute()
            if (p.is_symlink() or not lexical.is_relative_to(lexical_root.absolute()) or
                    not p.is_file() or not p.resolve().is_relative_to(checks_dir)):
                raise Stop('Failed-check log/artifact escapes retained check directory')
            if any(parent.is_symlink() for parent in p.parents if parent != lexical_root.parent):
                raise Stop('Symlink in failed-check evidence path')
            return p.resolve()
        log_path = retained_check(check['log'])
        if (log_path.parent != checks_dir or log_path.suffix != '.log' or
                log_path.stat().st_mtime < finished or check_path.stat().st_mtime < finished):
            raise Stop('Failed-check log is not a retained branch-check log')
        artifacts = check.get('artifacts')
        if not isinstance(artifacts, list) or not artifacts:
            raise Stop('Failed-check artifact manifest is missing')
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise Stop('Malformed failed-check artifact manifest')
            p = retained_check(artifact.get('path'))
            if (not p.is_file() or p.stat().st_size != artifact.get('bytes') or
                    hashlib.sha256(p.read_bytes()).hexdigest() != artifact.get('sha256') or
                    p.stat().st_mtime < finished):
                raise Stop('Failed-check log/artifact bytes do not match their receipt')
        log_item = next((a for a in artifacts if Path(a.get('path', '')).resolve() == log_path), None)
        if not log_item:
            raise Stop('Failed-check log predates terminal worker completion or is unmanifested')
        pytest_details = self._terminal_pytest_failure(check, check_path, task_id, finished) if terminal_pytest else None
        return dict(phase=record['phase'], attempt=record['attempt'], job=str(job_path),
                    job_sha256=hashlib.sha256(job_path.read_bytes()).hexdigest(), receipt=str(receipt_path),
                    receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                    check_receipt=str(check_path), check_receipt_sha256=hashlib.sha256(check_path.read_bytes()).hexdigest(),
                    blocker=self.state.get('blocked'), snapshot_sha256=check['source_snapshot_sha256'],
                    **({'pytest_failure': pytest_details} if pytest_details else {}))

    def _terminal_pytest_failure(self, check, check_path, task_id, worker_finished):
        """Classify only a retained, ordinary failed pytest run suitable for a repair."""
        if check.get('exit') != 1 or not isinstance(check.get('exit'), int) or isinstance(check.get('exit'), bool):
            return None
        template = check.get('original_command')
        if not isinstance(template, list) or len(template) < 3 or template[1:3] != ['-m', 'pytest']:
            return None
        if template not in self.task_checks(task_id) or check.get('cwd') != str(Path(self.state['records'][task_id]['worktree']).resolve()):
            raise Stop('Failed pytest command or working directory is not frozen for this task')
        error = check.get('error')
        if not isinstance(error, str) or not error.startswith('Unapproved or duplicate pytest failure: ('):
            return None
        report_path = check_path.with_suffix('.xml')
        if (report_path.is_symlink() or not report_path.is_file() or
                not report_path.resolve().is_relative_to(check_path.parent.resolve())):
            raise Stop('Failed pytest JUnit report is missing or escapes retained check directory')
        artifacts = check.get('artifacts')
        if not isinstance(artifacts, list):
            raise Stop('Failed pytest JUnit report is absent from retained artifact manifest')
        junit_items = [item for item in artifacts if isinstance(item, dict) and item.get('path') == str(report_path)]
        if len(junit_items) != 1:
            raise Stop('Failed pytest JUnit report must have one exact retained manifest entry')
        junit_item = junit_items[0]
        if (not isinstance(junit_item.get('bytes'), int) or isinstance(junit_item.get('bytes'), bool) or
                not isinstance(junit_item.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', junit_item['sha256']) or
                report_path.stat().st_size != junit_item['bytes'] or
                hashlib.sha256(report_path.read_bytes()).hexdigest() != junit_item['sha256'] or
                report_path.stat().st_mtime < worker_finished):
            raise Stop('Failed pytest JUnit bytes, hash or completion time do not match retained manifest')
        try:
            root = ET.parse(report_path).getroot()
        except (OSError, ET.ParseError) as e:
            raise Stop('Failed pytest JUnit report is malformed') from e
        if root.tag == 'testsuite':
            suites = [root]
        elif root.tag == 'testsuites' and root and all(child.tag == 'testsuite' for child in root):
            suites = list(root)
        else:
            raise Stop('Failed pytest report has an unknown or malformed JUnit hierarchy')
        cases = []
        identities = []
        testcase_ids = set()
        accounted_outcomes = set()
        totals = dict(tests=0, failures=0, errors=0, skipped=0)
        for suite in suites:
            if sum(1 for node in suite.iter() if node.tag == 'testsuite') != 1:
                raise Stop('Failed pytest report has nested testsuite records')
            suite_cases = [child for child in suite if child.tag == 'testcase']
            if sum(1 for node in suite.iter() if node.tag == 'testcase') != len(suite_cases):
                raise Stop('Failed pytest report has testcases outside direct suite records')
            counts = {}
            for key in totals:
                raw = suite.get(key)
                if not isinstance(raw, str) or not re.fullmatch(r'\d+', raw):
                    raise Stop('Failed pytest report has missing or negative per-suite counts')
                counts[key] = int(raw)
            if not suite_cases:
                raise Stop('Failed pytest report contains an empty testsuite')
            suite_actual = dict(tests=len(suite_cases), failures=0, errors=0, skipped=0)
            for case in suite_cases:
                cases.append(case)
                identity = (case.get('classname'), case.get('name'))
                if not all(isinstance(part, str) and part for part in identity) or identity in testcase_ids:
                    raise Stop('Failed pytest report has missing or duplicate testcase identities')
                testcase_ids.add(identity)
                outcomes = {name: case.findall(name) for name in ('failure', 'error', 'skipped')}
                if any(len(nodes) > 1 for nodes in outcomes.values()) or sum(map(len, outcomes.values())) > 1:
                    raise Stop('Failed pytest testcase has duplicate or conflicting outcome elements')
                for name, nodes in outcomes.items():
                    if nodes:
                        accounted_outcomes.add(id(nodes[0]))
                        suite_actual['failures' if name == 'failure' else name + ('s' if name == 'error' else '')] += 1
                if outcomes['failure']:
                    identities.append(f'{identity[0]}::{identity[1]}')
            if counts != suite_actual:
                raise Stop('Failed pytest per-suite counts do not match its testcase outcomes')
            for key in totals:
                totals[key] += counts[key]
        if not cases or not identities or len(identities) != len(set(identities)):
            raise Stop('Failed pytest report has no genuine failures or duplicate failed identities')
        all_outcomes = [node for node in root.iter() if node.tag in ('failure', 'error', 'skipped')]
        if len(all_outcomes) != len(accounted_outcomes) or {id(node) for node in all_outcomes} != accounted_outcomes:
            raise Stop('Failed pytest report contains outcome elements outside testcase records')
        if totals['errors'] or totals['skipped'] or totals['failures'] != len(identities):
            raise Stop('Failed pytest report has collection errors, skips or incoherent failure counts')
        if root.tag == 'testsuites':
            for key, actual in totals.items():
                raw = root.get(key)
                if raw is not None and (not re.fullmatch(r'\d+', raw) or int(raw) != actual):
                    raise Stop('Failed pytest aggregate counts do not match per-suite totals')
        log = check.get('log')
        log_path = Path(log) if isinstance(log, str) else Path('/')
        if (not isinstance(log, str) or log_path.is_symlink() or not log_path.is_file() or
                not log_path.resolve().is_relative_to(check_path.parent.resolve())):
            raise Stop('Failed pytest log is missing')
        log_text = Path(log).read_text(errors='replace')
        summary = re.findall(r'(?m)^(\d+) failed, (\d+) passed in [^\n]+$', log_text)
        if len(summary) != 1 or tuple(map(int, summary[0])) != (totals['failures'], len(cases) - totals['failures']):
            raise Stop('Failed pytest log summary does not match retained JUnit results')
        # Reuse the coordinator's strict pytest policy: the retained error must be
        # exactly the parser's classification of an unapproved testcase failure.
        exceptions = [e for e in self.config.get('baseline_exceptions', [])
                      if [x.replace('{python}', self.config['python']) for x in e.get('command', [])] == template]
        exception_ids = [(e.get('classname'), e.get('name')) for e in exceptions]
        for identity in identities:
            classname, name = identity.split('::', 1)
            if (classname, name) in exception_ids:
                raise Stop('Failed pytest report includes an approved baseline exception')
        try:
            pytest_evidence(report_path, Path(check['cwd']), 1, exceptions)
        except Stop as e:
            if not str(e).startswith('Unapproved or duplicate pytest failure: (') or str(e) != error:
                raise Stop('Failed pytest receipt does not match coordinator failure policy') from e
        else:
            raise Stop('Pytest report contains no policy-rejected failure')
        return dict(junit=str(report_path), junit_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    tests=len(cases), failures=[dict(identity=x) for x in identities],
                    log=check.get('log'))

    @property
    def integration(self):
        return Path(self.state['integration'])

    def initialize(self, tasks):
        if self.state:
            return
        clean(self.repo)
        branch = self.config['integration_branch']
        if not branch.startswith('migration/') or branch in ('main', 'master'):
            raise Stop('Integration must be a dedicated migration/* branch')
        if run(['git', 'show-ref', '--verify', f'refs/heads/{branch}'], self.repo, False).returncode == 0:
            raise Stop('Existing integration branch without state; explicit manual reconciliation required')
        if any(t['status'] not in ('ready', 'blocked') for t in tasks):
            raise Stop('Existing assigned/completed tasks require imported, verified evidence; refusing to duplicate')
        refs = git(self.repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').splitlines()
        task_ids = {t['id'] for t in tasks}
        collisions = [ref for ref in refs if any(tid in task_ids for tid in re.findall(r'(?:^|[/_-])(T\d{3})(?=$|[/_-])', ref))]
        if collisions:
            raise Stop('Existing task branches have no coordinator record; reconcile ownership before execution: ' + ', '.join(collisions))
        baseline = git(self.repo, 'rev-parse', 'HEAD')
        self.state = dict(version=1, repo=str(self.repo), config_digest=digest(json.dumps(self.config, sort_keys=True)),
                          baseline=baseline, workspace_root=str(self.workspaces), integration=str(self.workspaces / 'integration'), branch=branch,
                          records={}, checkpoints={}, blocked=None, expected_head=baseline,
                          input_digest=self.inputs(tasks))
        self.save()  # Intent before worktree/ref creation.
        self.ensure_integration()

    def inputs(self, tasks):
        return digest(json.dumps([(t['id'], t['text']) for t in tasks]) +
                      ''.join((self.repo / p).read_text() for p in ('NEW_SPEC.md', 'docs/migration/NEW_SPEC.md', 'docs/migration/PLAN.md', 'docs/migration/REQUIREMENTS_MATRIX.md', 'docs/migration/DECISIONS.md')))

    def ensure_integration(self):
        if not self.integration.exists():
            exists = run(['git', 'show-ref', '--verify', f'refs/heads/{self.state["branch"]}'], self.repo, False).returncode == 0
            args = ['worktree', 'add'] + ([] if exists else ['-b', self.state['branch']])
            git(self.repo, *args, str(self.integration), self.state['branch'] if exists else self.state['baseline'])
        if git(self.integration, 'branch', '--show-current') != self.state['branch']:
            raise Stop('Integration worktree branch mismatch')

    def artifact(self, tid, name):
        p = self.directory / 'tasks' / tid / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def prompt(self, task, repair='', role='worker'):
        scopes = self.config['scopes'][task['id']]
        instructions = []
        for name in ('AGENTS.md',):
            for root in reversed([self.repo, *self.repo.parents]):
                p = root / name
                if p.exists():
                    instructions.append(f'{p}:\n{p.read_text()}')
        # Walk owned ancestors only, including Git-ignored guidance. Directory-owned
        # descendants discover their local guidance before editing, as instructed below.
        guidance = set()
        for scope in scopes:
            target = self.repo / scope
            parent = target if scope.endswith('/') else target.parent
            while parent != self.repo:
                guidance.add(parent / 'AGENTS.md')
                parent = parent.parent
        for p in sorted(guidance):
            if p.is_file():
                rel = p.relative_to(self.repo).as_posix()
                instructions.append(f'{rel}:\n{p.read_text()}')
        spec = (self.repo / 'docs/migration/NEW_SPEC.md').read_text()
        decision_guidance = (ROUTINE_DECISION_GUIDANCE if self.state and
                             self.state.get('execution_guidance') == 'routine-decisions' else
                             'G02/G03 permit candidate research and evidence gathering; do not finalize a candidate, release contract, authority or quota without separate user approval. '
                             'If these approvals are needed, report the gate and stop; later explicit user approvals may be supplied in targeted repair instructions.\n')
        context = focused_context(self.repo, task, spec)
        preamble = (f'Implement only task {task["id"]} in this worktree. Write ownership: {json.dumps(scopes)}.\n'
                'Do not merge, rebase, switch branches, push, edit Git metadata, or update migration planning/status/spec files. '
                'Leave changes uncommitted: the coordinator verifies and commits them. Never run destructive schema/data changes, '
                'production operations, paid experiment pilots, or merge into main. Stop and report a required unsafe action. '
                'Run available public entry-point/contract tests, relevant negative cases and authentic artifact lineage checks before handoff. Never fabricate historical evidence. '
                'Run applicable local checks; do not hide skips or incomplete acceptance. Report exact evidence, limitations and failed criteria. '
                'Only supplied coding task is authorized; do not launch other agents or tasks.\n') if role == 'worker' else (
                f'Review only task {task["id"]}. Allowed deliverable scope: {json.dumps(scopes)}. '
                'Read-only; do not implement, edit, commit, merge, or launch agents.\n')
        return (preamble +
                'User decision limits: G01 permits supported-hook feasibility investigation only; no runtime pin changes or upstream patches without separate approval. '
                'D02 permits a neutral minimal artifact API only if it satisfies the spec. D03 permits symmetric MVP exclusion of the spec-optional features. '
                + decision_guidance +
                f'Excluded ownership: {json.dumps(self.config.get("exclusions", {}).get(task["id"], []))}\nRepair findings (if any): {repair}\nRepository instructions:\n' + '\n'.join(instructions) +
                '\nBefore editing directory-owned descendants, check their nested AGENTS.md. '
                'Use the supplied exact task-owned matrix rows; do not search the matrix for generic AC IDs. '
                'Read source and logs in bounded relevant ranges. The complete spec and matrix remain authoritative and available; '
                'follow cross-references when needed and report contradictions rather than relaxing criteria.\n' +
                '\nTask file, requirements, acceptance and validation:\n' + task['text'] +
                context +
                f'\nWorker validation commands: {json.dumps(self.worker_checks(task["id"]))}. '
                f'Configured Python interpreter: {self.config["python"]}. Use this absolute interpreter; '
                'a missing worktree-local .venv does not require dependency installation. '
                'Run targeted checks during development; the coordinator owns all required branch/integration/regression checks. '
                'Report sandbox limitations once with exact errors; do not repeatedly retry blocked localhost or tool access. '
                'Read frozen docs/artifact-upload-contracts.md if present. Handoff: changed paths, AC evidence, '
                'exact tests, unresolved findings, and any cross-owner proposals.\n')

    def prepare(self, task):
        tid = task['id']
        if tid not in self.state['records']:
            base = git(self.integration, 'rev-parse', 'HEAD')
            self.state['records'][tid] = dict(phase='planned', base=base, branch=f'migration/{tid}-{self.directory.name}',
                worktree=str(self.workspaces / 'worktrees' / tid), attempt=0, review_attempt=0, review=None, commit=None)
            self.save()
        r = self.state['records'][tid]
        w = Path(r['worktree'])
        if not w.exists():
            exists = run(['git', 'show-ref', '--verify', f'refs/heads/{r["branch"]}'], self.repo, False).returncode == 0
            git(self.repo, 'worktree', 'add', *([] if exists else ['-b', r['branch']]), str(w), r['branch'] if exists else r['base'])
        if git(w, 'branch', '--show-current') != r['branch'] or git(w, 'rev-parse', 'HEAD') != (r['commit'] or r['base']):
            raise Stop(f'{tid}: worker changed branch/HEAD; inspect manually')
        return r

    def launch(self, task, role, prompt, fallback=False):
        tid = task['id']
        r = self.state['records'][tid]
        if r['phase'].endswith('_running'):
            raise Stop('Attempt already running; never duplicate it')
        if role == 'worker':
            if any(other != tid and x['phase'] == 'worker_running' and
                   overlap(self.config['scopes'][tid], self.config['scopes'][other])
                   for other, x in self.state['records'].items()):
                raise Stop('Conflicting live worker ownership')
            if sum(x['phase'] == 'worker_running' for x in self.state['records'].values()) >= self.max_workers:
                raise Stop('Worker capacity exhausted')
            if not fallback:
                r['attempt'] += 1
            number = r['attempt']
        else:
            r['review_attempt'] = r.get('review_attempt', 0) + 1
            number = r['review_attempt']
        prefix = f'{role}-{number}' + ('-fallback' if fallback else '')
        route = dict(backend='codex', model=self.config['worker_model'] if role == 'worker' else self.config['reviewer_model'], effort='medium')
        if self.routing:
            route = self.routing['reviewer'] if role == 'review' else self.routing['primary']
            if role == 'worker' and (fallback or (self.routing.get('mixed_parallel') and any(
                    x['phase'] == 'worker_running' and x.get('provider') == 'agy' for x in self.state['records'].values()))):
                route = self.routing['fallback']
        if role == 'worker' and r['phase'] == 'repair_pending' and r.get('worker_route'):
            route = r['worker_route']  # Coding repairs retain their provider; no outage inferred.
        if fallback and not r.get('fallback_plan'):
            raise Stop('Fallback requires a recorded safe continuation plan')
        if fallback:
            route = r.get('fallback_plan', {}).get('route', route)
            if snapshot(r['worktree']) != r['fallback_plan']['snapshot']:
                raise Stop('Worktree changed after continuation plan; no fallback launch')
        if fallback and (route['backend'] != 'codex' or not r.get('fallback_plan')):
            raise Stop('Fallback requires a recorded safe continuation plan')
        receipt = self.artifact(tid, prefix + '-exit.json')
        if receipt.exists():
            raise Stop('Attempt receipt already exists')
        prompt_path = self.artifact(tid, prefix + '-prompt.txt')
        prompt_path.write_text(prompt)
        result = self.artifact(tid, prefix + '-result.txt')
        cmd = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--json', '--color', 'never',
               '-m', route['model'],
               '-c', 'model_reasoning_effort=' + json.dumps(route.get('effort', 'medium')), '-c', 'approval_policy="never"',
               '-c', 'features.multi_agent=false', '-s', 'workspace-write' if role == 'worker' else 'read-only',
               '-C', r['worktree'], '-o', str(result)]
        if role == 'review':
            schema = self.artifact(tid, 'review-schema.json')
            atomic(schema, dict(type='object', properties={
                'approved': {'type': 'boolean'}, 'findings': {'type': 'array', 'items': {'type': 'string'}},
                'criteria': {'type': 'array', 'items': {'type': 'object', 'properties': {
                    'id': {'type': 'string'}, 'passed': {'type': 'boolean'}, 'evidence': {'type': 'string'}},
                    'required': ['id', 'passed', 'evidence'], 'additionalProperties': False}}},
                required=['approved', 'findings', 'criteria'], additionalProperties=False))
            cmd += ['--output-schema', str(schema)]
        cmd += ['-']
        if route['backend'] == 'agy':
            cmd = ['agy', '--model', route['model'], '--effort', route.get('effort', 'medium'),
                   '--output-format', 'stream-json', '--sandbox', '--mode', 'accept-edits',
                   '--disable-slash-commands', '--print-timeout', str(self.config['timeout_seconds']) + 's']
        job = dict(command=cmd, cwd=r['worktree'], prompt=str(prompt_path), result=str(result),
                   log=str(self.artifact(tid, prefix + '.jsonl')), receipt=str(receipt),
                   timeout=self.config['timeout_seconds'], provider=route['backend'], model=route['model'],
                   route=route, role=role, task=tid, attempt=number,
                   stderr=str(self.artifact(tid, prefix + '.stderr')),
                   preflight_seconds=(self.routing or {}).get('preflight_seconds', 30),
                   permission_commands=self.worker_checks(tid) if role == 'worker' else [],
                   before_snapshot=snapshot(r['worktree']) if role == 'worker' else None)
        job_path = self.artifact(tid, prefix + '-job.json')
        atomic(job_path, job)
        # A durable intent is saved before spawning. Missing receipt after a crash never triggers an automatic duplicate.
        r['provider'], r['model'] = route['backend'], route['model']
        if role == 'worker':
            r['worker_provider'], r['worker_model'] = route['backend'], route['model']
            r['worker_route'] = route
            r['worker_result'] = str(result)
        r['phase'] = role + '_running'
        r['job'] = str(job_path)
        r['pid'] = None
        self.save()
        p = subprocess.Popen([sys.executable, str(HERE / 'job.py'), str(job_path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        self.processes[p.pid] = p
        r['pid'] = p.pid
        self.save()

    def worker_checks(self, tid):
        commands = self.config.get('worker_checks', {}).get(tid, self.task_checks(tid))
        return [[x.replace('{python}', self.config['python']).replace('{worktree}', self.state['records'][tid]['worktree'] if self.state and tid in self.state['records'] else '{worktree}') if x != '.venv/bin/python' else self.config['python'] for x in argv] for argv in commands]

    def task_checks(self, tid):
        return self.config['checks'][tid] + self.config.get('public_checks', {}).get(tid, [])

    def handle_provider_failure(self, task, outcome, job):
        tid = task['id']
        r = self.state['records'][tid]
        current = snapshot(r['worktree'])
        provenance = dict(reason=outcome['provider_failure'], job=r['job'], receipt=job['receipt'],
                          provider=job['provider'], model=job['model'], attempt=r['attempt'],
                          before=job['before_snapshot'], after=current)
        path = self.artifact(tid, f'worker-{r["attempt"]}-partial.json')
        atomic(path, provenance)
        diff = self.artifact(tid, f'worker-{r["attempt"]}-partial.patch')
        diff.write_text(git(r['worktree'], 'diff', '--binary', job['before_snapshot']['head']))
        r['provider_failure'] = dict(reason=outcome['provider_failure'], provenance=str(path), diff=str(diff))
        if current != job['before_snapshot']:
            r['phase'] = 'provider_blocked'
            self.save()
            raise Stop(f'{tid}: Gemini {outcome["provider_failure"]} after partial changes; '
                       f'preserved at {path}. Record a safe continuation plan with continue-fallback')
        if not self.routing:
            raise Stop('No fallback routing configured; evidence retained')
        r['fallback_plan'] = dict(plan='Verified byte-identical worktree, branch, HEAD and staged diff. '
                                 'Continue the same task and retained repair brief; no changes discarded.',
                                 provenance=str(path), snapshot=current, route=self.routing['fallback'])
        r['phase'] = 'fallback_pending'
        self.save()  # Recovery reuses this intent; a crash cannot relaunch Gemini.

    def continue_fallback(self, tid, plan):
        if not self.state or tid not in self.state['records']:
            raise Stop('Unknown task')
        r = self.state['records'][tid]
        if r['phase'] != 'provider_blocked' or not plan or not 40 <= len(plan.strip()) <= 6000:
            raise Stop('Partial provider failure requires an explicit 40–6000 character continuation plan')
        job = json.loads(Path(r['job']).read_text())
        if not Path(job['receipt']).is_file():
            raise Stop('No terminal receipt; never duplicate a potentially live process')
        provenance = json.loads(Path(r['provider_failure']['provenance']).read_text())
        current = snapshot(r['worktree'])
        if current != provenance['after']:
            raise Stop('Partial files or Git state changed since failure; investigate before continuation')
        if current['head'] != job['before_snapshot']['head'] or current['branch'] != r['branch']:
            raise Stop('Unexpected worker Git changes; manual reconciliation required')
        if not self.routing:
            raise Stop('No fallback routing configured')
        r['fallback_plan'] = dict(plan=plan.strip(), provenance=r['provider_failure']['provenance'],
                                 snapshot=current, route=self.routing['fallback'])
        r['phase'] = 'fallback_pending'
        self.state['blocked'] = None
        self.save()
    def repair(self, tid, findings, confirmed_stopped=False, select_validated_primary=False, reason=None):
        if not self.state or tid not in self.state['records']:
            raise Stop('Unknown assigned task')
        r = self.state['records'][tid]
        if not findings or len(findings.strip()) < 20 or r['attempt'] >= self.repair_limit(r):
            raise Stop('Supply targeted findings; bounded repair limit applies')
        if any(e['findings'] == findings.strip() for e in r.get('repair_history', [])):
            raise Stop('Repeated repair plan; consolidate findings and provide a new hypothesis')
        if r['phase'] in ('merged', 'merge_pending', 'integration_testing', 'status_pending'):
            raise Stop('Already integrated or uncertain merge; manual owner-scoped fix/revert required')
        job_path = Path(r.get('job', ''))
        pid_path = Path(str(job_path) + '.pid.json')
        job = json.loads(job_path.read_text()) if job_path.is_file() else {}
        child_path = Path(str(job_path) + '.child.json')
        finished = bool(job.get('receipt') and Path(job['receipt']).exists())
        if child_path.exists() and not finished:
            try:
                os.kill(json.loads(child_path.read_text())['pid'], 0)
            except ProcessLookupError:
                pass
            else:
                raise Stop('Worker/reviewer child still alive; inspect it before repair')
        if pid_path.exists() and not finished:
            pid = json.loads(pid_path.read_text())['pid']
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise Stop('Runner still alive; wait for it, never duplicate it')
        elif r.get('pid') and not finished:
            try:
                os.kill(r['pid'], 0)
            except ProcessLookupError:
                pass
            else:
                raise Stop('Launch process still alive')
        elif r['phase'].endswith('_running') and not finished and not confirmed_stopped:
            raise Stop('Unknown launch process; inspect processes manually, then repair with --confirmed-stopped')
        w = Path(r['worktree']) if r.get('worktree') else None
        dirty_terminal_authorization = None
        if w and w.exists():
            if r.get('phase') == 'worker_finished':
                dirty = bool(git(w, 'status', '--porcelain', '--untracked-files=all'))
                if not dirty:
                    clean(w)
                else:
                    history = r.get('repair_authorizations', [])
                    if not isinstance(history, list):
                        raise Stop('Malformed repair authorization history')
                    authorizations = [entry for entry in history if isinstance(entry, dict) and
                                      'terminal_check_repair' in entry]
                    authorization = self._current_dirty_repair_authorization(tid, r, authorizations)
                    proof = self._terminal_check_repair_evidence(tid, r)
                    if proof != authorization['terminal_check_repair']:
                        raise Stop('Retained terminal repair authorization is stale or changed')
                    current = snapshot(w)
                    if (current['head'] != (r['commit'] or r['base']) or current['branch'] != r['branch'] or
                            current['index'] != hashlib.sha256(b'').hexdigest()):
                        raise Stop('Terminal repair requires unchanged branch/HEAD and an empty index')
                    paths = self.changed(tid)
                    if not paths:
                        raise Stop('Terminal repair authorization requires retained worker changes')
                    for name in paths:
                        path = w / name
                        if (owns(name, list(PROTECTED)) or not owns(name, self.config['scopes'][tid]) or
                                owns(name, self.config.get('exclusions', {}).get(tid, [])) or
                                path.is_symlink() or (path.exists() and not path.is_file()) or
                                (path.exists() and not path.resolve().is_relative_to(w.resolve()))):
                            raise Stop(f'{tid}: unsafe or out-of-scope retained worker change: {name}')
                    dirty_terminal_authorization = authorization
            else:
                clean(w)
        primary_selection = None
        if select_validated_primary:
            if not self.routing or not self.routing.get('primary'):
                raise Stop('No validated routing primary configured')
            primary_route = self.routing['primary']
            if not isinstance(primary_route, dict) or not primary_route.get('backend') or not primary_route.get('model'):
                raise Stop('Invalid routing primary configuration')
            if not reason or len(str(reason).strip()) < 20:
                raise Stop('Explicit primary selection requires concrete reason or smoke artifact provenance')
            primary_selection = dict(
                route=dict(primary_route),
                reason=str(reason).strip(),
                old_route=dict(r['worker_route']) if r.get('worker_route') else None,
            )
        r['phase'] = 'repair_pending'
        if dirty_terminal_authorization is not None:
            dirty_terminal_authorization['terminal_repair_consumed'] = True
        repair_entry = dict(
            time=time.time(), attempt=r['attempt'],
            commit=r['commit'], findings=findings.strip(), review=r.get('review')
        )
        if primary_selection:
            repair_entry['old_route'] = primary_selection['old_route']
            repair_entry['selected_route'] = primary_selection['route']
            repair_entry['primary_reason'] = primary_selection['reason']
            r.setdefault('route_history', []).append(dict(
                time=time.time(), attempt=r['attempt'],
                old_route=primary_selection['old_route'],
                selected_route=primary_selection['route'],
                reason=primary_selection['reason']
            ))
            r['worker_route'] = primary_selection['route']
        r.setdefault('repair_history', []).append(repair_entry)
        r['repair'] = findings.strip()
        r['prior_review'] = r.get('review')
        r['review'] = None
        r.pop('gate_commit', None)
        self.state['blocked'] = None
        self.save()

    def wait(self, tid):
        r = self.state['records'][tid]
        job = json.loads(Path(r['job']).read_text())
        receipt = Path(job['receipt'])
        while not receipt.exists():
            pid_path = Path(r['job'] + '.pid.json')
            if not pid_path.exists():
                # Runner can take a moment to start; uncertain launch after interruption stays blocked.
                if not r.get('pid'):
                    raise Stop(f'{tid}: uncertain launch; inspect job and processes, then use repair')
                pid = r['pid']
            else:
                pid = json.loads(pid_path.read_text())['pid']
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                raise Stop(f'{tid}: runner died without receipt; work retained. Inspect before explicit repair')
            time.sleep(1)
        outcome = json.loads(receipt.read_text())
        process = self.processes.pop(r.get('pid'), None)
        if process is not None:
            process.wait(timeout=10)
        r['exit'] = outcome
        r['phase'] = 'worker_finished' if r['phase'] == 'worker_running' else 'review_finished'
        self.save()
        return outcome, job

    def changed(self, tid):
        r = self.state['records'][tid]
        w = Path(r['worktree'])
        names = git(w, 'diff', '--name-only', '--no-renames', r['base']).splitlines()
        names += git(w, 'ls-files', '--others', '--exclude-standard').splitlines()
        return sorted(set(names))

    def scope(self, tid):
        r = self.state['records'][tid]
        w = Path(r['worktree'])
        if git(w, 'branch', '--show-current') != r['branch'] or git(w, 'rev-parse', 'HEAD') != (r['commit'] or r['base']):
            raise Stop(f'{tid}: unexpected branch/commit')
        paths = self.changed(tid)
        if not paths:
            raise Stop(f'{tid}: no deliverables')
        for name in paths:
            p = w / name
            if owns(name, list(PROTECTED)) or not owns(name, self.config['scopes'][tid]) or owns(name, self.config.get('exclusions', {}).get(tid, [])):
                raise Stop(f'{tid}: out-of-scope change: {name}')
            if p.is_symlink() or (p.exists() and not p.is_file()):
                raise Stop(f'{tid}: unsafe deliverable type: {name}')
            if p.exists() and not p.resolve().is_relative_to(w.resolve()):
                raise Stop(f'{tid}: deliverable escapes worktree: {name}')
        return paths

    def checks(self, cwd, commands, label):
        if not commands:
            raise Stop(f'{label}: no required checks configured')
        results = []
        for i, template in enumerate(commands):
            output = self.directory / 'checks' / label / f'{uuid.uuid4().hex}-{i}.log'
            output.parent.mkdir(parents=True, exist_ok=True)
            artifact_dir = output.parent / output.stem
            argv = [x.replace('{python}', self.config['python']).replace('$USER', output.stem).replace('{output}', str(artifact_dir)) for x in template]
            if argv[0] == '.venv/bin/python':
                argv[0] = self.config['python']
            if any('$' in a or '`' in a for a in argv):
                raise Stop(f'Unresolved shell syntax in check: {argv}')
            artifact_dir.mkdir(parents=True, exist_ok=False)
            env = dict(os.environ, MIGRATION_RUN_ID=output.stem, PYTHONDONTWRITEBYTECODE='1',
                       MIGRATION_ARTIFACT_DIR=str(artifact_dir), MIGRATION_CHECK_ORIGIN='coordinator')
            pytest = argv[1:3] == ['-m', 'pytest']
            if pytest:
                report = output.with_suffix('.xml')
                check_argv = list(argv)
                argv += ['-o', 'junit_family=xunit2', '--junitxml=' + str(report)]
            p = None
            try:
                with output.open('w') as f:
                    p = subprocess.Popen(argv, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=env, start_new_session=True)
                    try:
                        p.wait(timeout=self.config['timeout_seconds'])
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid, signal.SIGTERM)
                        try:
                            p.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(p.pid, signal.SIGKILL)
                            p.wait()
                        raise
                text = output.read_text()
                failed = p.returncode != 0 or bool(re.search(r'\b(?:\d+ skipped|\d+ xfailed|\d+ xpassed|no tests ran|0 passed)\b', text))
                evidence = {}
                if pytest:
                    exceptions = [e for e in self.config.get('baseline_exceptions', [])
                                  if [x.replace('{python}', self.config['python']) for x in e['command']] == check_argv]
                    evidence = pytest_evidence(report, cwd, p.returncode, exceptions)
                    failed = bool(re.search(r'\b(?:\d+ skipped|\d+ xfailed|\d+ xpassed|no tests ran|0 passed)\b', text))
                results.append(dict(command=argv, exit=p.returncode, log=str(output), passed=not failed, **evidence))
            except (OSError, subprocess.TimeoutExpired, ET.ParseError, Stop) as e:
                results.append(dict(command=argv, exit=p.returncode if p else None,
                                    passed=False, error=str(e), log=str(output)))
                if pytest:
                    results[-1]['junit'] = str(report)
            def retained_file(path):
                return (path.is_file() and not path.is_symlink() and
                        path.resolve().is_relative_to(artifact_dir.resolve()) and
                        not any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(artifact_dir)))
            artifacts = []
            for path in sorted([output, *artifact_dir.rglob('*'), *([report] if pytest else [])]):
                if path in (output, report if pytest else None) or retained_file(path):
                    if not path.is_file() or path.is_symlink():
                        continue
                    artifacts.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size))
            results[-1].update(origin='coordinator', original_command=template, cwd=str(cwd),
                               source_head=git(cwd, 'rev-parse', 'HEAD'),
                               source_snapshot_sha256=digest(json.dumps(snapshot(cwd), sort_keys=True)),
                               artifact_directory=str(artifact_dir), artifacts=artifacts)
            requirements = self.config.get('required_artifacts', {}).get(label.split('-')[0], [])
            missing = [pattern for pattern in requirements if not any(
                retained_file(p) and p.stat().st_size > 0 and any(a['path'] == str(p) for a in artifacts) for p in artifact_dir.glob(pattern))]
            if missing:
                results[-1].update(passed=False, error='Required verification artifacts missing: ' + ', '.join(missing))
            atomic(output.with_suffix('.json'), results[-1])
            if not results[-1]['passed']:
                raise Stop(f'{label}: required check failed/missing/skipped: {shlex.join(argv)}; see {output}')
        return results

    def verify_worker(self, task):
        tid = task['id']
        r = self.state['records'][tid]
        self.scope(tid)
        w = Path(r['worktree'])
        evidence = self.checks(w, self.task_checks(tid), tid + '-branch')
        self.scope(tid)  # Checks cannot broaden the committed scope.
        git(w, 'add', '--', *self.changed(tid))
        if not set(git(w, 'diff', '--cached', '--name-only').splitlines()).issubset(set(self.changed(tid))):
            raise Stop(f'{tid}: unexpected staged files')
        if run(['git', 'diff', '--cached', '--quiet'], w, False).returncode != 0:
            git(w, 'commit', '-m', f'migration: {tid} attempt {r["attempt"]}')
        r['commit'] = git(w, 'rev-parse', 'HEAD')
        r['evidence'] = evidence
        r['phase'] = 'committed'
        self.save()
        clean(w)

    def launch_review(self, task):
        tid = task['id']
        r = self.state['records'][tid]
        clean(r['worktree'])
        worker_report = Path(r.get('worker_result', self.artifact(tid, 'worker-%s-result.txt' % r['attempt']))).read_text()
        review = ('READ-ONLY independent review. Do not edit any files, commit, merge, or launch workers. '
                  'Do not trust worker PASS or process exit. Inspect actual implementation, full diff, tests and evidence. '
                  'Reject placeholder/incomplete work, skipped required checks, scope violations and missing criteria. '
                  'Worker sandbox denial alone does not fail a check whose exact authorized coordinator receipt passed; '
                  'a claimed success without coordinator evidence is unverified. Inspect public CLI/contract negatives and artifact lineage. '
                  'Report every numbered acceptance criterion using its AC ID; each passed criterion must cite concrete evidence. '
                  'Set approved only if ALL criteria are satisfied and no blocking findings.\n'
                  'Start with the cumulative base-to-commit diff and changed paths below. Read check receipts/logs '
                  'instead of rerunning successful checks unless a specific gap requires it. Prior reviews are '
                  'navigation evidence only; independently verify every criterion and check repairs for regressions. '
                  'Consolidate all blocking findings in one review with locations and reproducible cases.\n'
                  f'Base: {r["base"]}\nCommit: {r["commit"]}\n'
                  f'Diff command: git diff {r["base"]} {r["commit"]} --\nChanged paths: {json.dumps(self.changed(tid))}\n'
                  f'Prior review (untrusted): {json.dumps(r.get("review") or r.get("prior_review"))}\n'
                  f'Repair plan: {r.get("repair", "")}\n'
                  f'Coordinator check evidence: {json.dumps(r["evidence"])}\nWorker report (untrusted): {worker_report}\n' + self.prompt(task, role='review'))
        self.launch(task, 'review', review)

    def authenticate_review(self, task, job=None):
        """Authenticate current terminal reviewer evidence for early and ordinary collection."""
        tid = task['id']
        r = self.state['records'][tid]
        review_attempt = r.get('review_attempt')
        if not isinstance(review_attempt, int) or isinstance(review_attempt, bool) or review_attempt <= 0:
            raise Stop(f'{tid}: invalid or missing reviewer attempt')

        tasks_root = self.directory / 'tasks'
        task_root = tasks_root / tid
        if tasks_root.is_symlink() or task_root.is_symlink():
            raise Stop(f'{tid}: symlink in retained task evidence directory')
        task_dir = task_root.resolve()
        if not task_dir.is_relative_to(tasks_root.resolve()):
            raise Stop(f'{tid}: task evidence directory escapes coordinator state')

        def retained(raw, base=task_dir):
            if not isinstance(raw, str) or not raw:
                raise Stop(f'{tid}: missing retained reviewer evidence path')
            p = Path(raw)
            if not p.is_absolute():
                p = base / p
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(task_dir):
                raise Stop(f'{tid}: reviewer evidence escapes its retained task directory')
            if any(parent.is_symlink() for parent in p.parents if parent != task_dir.parent):
                raise Stop(f'{tid}: symlink in retained reviewer evidence path')
            return p.resolve()

        expected_job_name = f'review-{review_attempt}-job.json'
        expected_receipt_name = f'review-{review_attempt}-exit.json'
        expected_result_name = f'review-{review_attempt}-result.txt'
        expected_log_name = f'review-{review_attempt}.jsonl'

        if not r.get('job'):
            raise Stop(f'{tid}: missing recorded reviewer job')
        job_path = retained(r['job'])
        if job_path.name != expected_job_name or job_path != (task_dir / expected_job_name).resolve():
            raise Stop(f'{tid}: reviewer job is not the exact current review attempt')

        try:
            persisted_job = json.loads(job_path.read_text())
        except (OSError, ValueError) as e:
            raise Stop(f'{tid}: malformed current reviewer job') from e
        if not isinstance(persisted_job, dict):
            raise Stop(f'{tid}: malformed current reviewer job')

        if job is None:
            job = persisted_job
        elif isinstance(job, dict):
            for k in ('task', 'role', 'attempt', 'receipt', 'result'):
                if k in job and persisted_job.get(k) != job.get(k):
                    raise Stop(f'{tid}: reviewer job does not match persisted current attempt')

        if (job.get('task') != tid or job.get('role') != 'review' or
                not isinstance(job.get('attempt'), int) or isinstance(job.get('attempt'), bool) or
                job['attempt'] != review_attempt or not isinstance(job.get('cwd'), str) or
                Path(job.get('cwd', '')).resolve() != Path(r['worktree']).resolve()):
            raise Stop(f'{tid}: current reviewer job identity mismatch')

        receipt_path = retained(job.get('receipt'))
        if receipt_path.name != expected_receipt_name or receipt_path != (task_dir / expected_receipt_name).resolve():
            raise Stop(f'{tid}: reviewer terminal receipt is not the exact current attempt')

        result_path = retained(job.get('result'))
        if result_path.name != expected_result_name or result_path != (task_dir / expected_result_name).resolve():
            raise Stop(f'{tid}: reviewer result is not the exact current attempt')

        if job.get('log'):
            log_path = retained(job['log'])
            if log_path.name != expected_log_name or log_path != (task_dir / expected_log_name).resolve():
                raise Stop(f'{tid}: reviewer log is not the exact current attempt')
        else:
            log_path = None

        candidate_commit = r.get('commit')
        if not candidate_commit or not isinstance(candidate_commit, str):
            raise Stop(f'{tid}: missing candidate commit for review')
        w = Path(r['worktree'])
        if git(w, 'rev-parse', 'HEAD') != candidate_commit:
            raise Stop(f'{tid}: candidate commit no longer matches current branch HEAD')
        if git(w, 'branch', '--show-current') != r.get('branch'):
            raise Stop(f'{tid}: reviewer worktree branch mismatch')
        if job.get('commit') and job['commit'] != candidate_commit:
            raise Stop(f'{tid}: reviewer job commit mismatch')

        try:
            receipt = json.loads(receipt_path.read_text())
        except (OSError, ValueError) as e:
            raise Stop(f'{tid}: malformed reviewer terminal receipt') from e
        if not isinstance(receipt, dict):
            raise Stop(f'{tid}: malformed reviewer terminal receipt')

        receipt_attempt = receipt.get('attempt')
        if (receipt.get('task') != tid or receipt.get('role') not in ('review', 'reviewer') or
                not isinstance(receipt_attempt, int) or isinstance(receipt_attempt, bool) or
                receipt_attempt != review_attempt):
            raise Stop(f'{tid}: reviewer terminal receipt task/role/attempt identity mismatch')

        if job.get('provider') and receipt.get('provider') != job.get('provider'):
            raise Stop(f'{tid}: reviewer receipt provider mismatch')
        if r.get('provider') and receipt.get('provider') != r.get('provider'):
            raise Stop(f'{tid}: reviewer receipt provider mismatch with record')
        if job.get('model') and receipt.get('model') != job.get('model'):
            raise Stop(f'{tid}: reviewer receipt model mismatch')
        if r.get('model') and receipt.get('model') != r.get('model'):
            raise Stop(f'{tid}: reviewer receipt model mismatch with record')

        if receipt.get('cwd') and Path(receipt['cwd']).resolve() != Path(r['worktree']).resolve():
            raise Stop(f'{tid}: reviewer receipt cwd mismatch')
        if receipt.get('commit') and receipt['commit'] != candidate_commit:
            raise Stop(f'{tid}: reviewer receipt commit mismatch')
        if receipt.get('result') and retained(receipt['result']) != result_path:
            raise Stop(f'{tid}: reviewer receipt result path mismatch')
        if log_path and receipt.get('log') and retained(receipt['log']) != log_path:
            raise Stop(f'{tid}: reviewer receipt log path mismatch')

        exit_code = receipt.get('exit')
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            raise Stop(f'{tid}: persisted reviewer process failed; no approval')

        failure_diagnostics = ('error', 'provider_failure', 'parse_error', 'launch_error',
                               'timeout', 'guest_error', 'structured_error', 'snapshot_error')
        present_diagnostics = [k for k in failure_diagnostics if receipt.get(k)]
        if present_diagnostics:
            raise Stop(f'{tid}: reviewer receipt contains failure diagnostics: {", ".join(f"{k}={receipt[k]!r}" for k in present_diagnostics)}; no approval')

        if receipt.get('success') is False:
            raise Stop(f'{tid}: persisted reviewer process failed; no approval')
        if job.get('provider') == 'agy':
            if receipt.get('success') is not True or receipt.get('terminal_status') != 'SUCCESS':
                raise Stop(f'{tid}: Gemini reviewer terminal evidence does not prove SUCCESS; no approval')
        elif receipt.get('success') is not None and receipt.get('success') is not True:
            raise Stop(f'{tid}: persisted reviewer process failed; no approval')

        started, finished = receipt.get('started'), receipt.get('finished')
        if started is not None:
            if not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started) or started <= 0:
                raise Stop(f'{tid}: malformed reviewer receipt start timestamp')
        if finished is not None:
            if not isinstance(finished, (int, float)) or isinstance(finished, bool) or not math.isfinite(finished) or finished <= 0:
                raise Stop(f'{tid}: malformed reviewer receipt finish timestamp')
        if started is not None and finished is not None:
            if finished < started:
                raise Stop(f'{tid}: reviewer receipt finish precedes start timestamp')

        if not result_path.is_file() or result_path.stat().st_size == 0:
            raise Stop(f'{tid}: reviewer result file is missing or empty')

        return job, receipt

    def accept_review(self, task, job):
        r = self.state['records'][task['id']]
        clean(r['worktree'])
        self.scope(task['id'])
        try:
            review = json.loads(Path(job['result']).read_text())
        except (OSError, ValueError) as e:
            raise Stop(f'Invalid reviewer output: {e}')
        if not isinstance(review, dict) or not isinstance(review.get('findings'), list) or not isinstance(review.get('criteria'), list):
            raise Stop('Reviewer output has invalid structure')
        if any(not isinstance(c, dict) or not isinstance(c.get('evidence'), str) or not isinstance(c.get('passed'), bool) or not isinstance(c.get('id'), str) for c in review['criteria']):
            raise Stop('Reviewer criterion has invalid structure')
        required = set(re.findall(r'^(AC\d+):', section(task['text'], 8), re.M))
        criteria = review.get('criteria', [])
        if not required or {c.get('id') for c in criteria} != required or len(criteria) != len(required):
            raise Stop('Reviewer did not cover every acceptance criterion')
        r['review'] = review
        approved = (review.get('approved') is True and not review.get('findings') and
                    all(c.get('passed') is True and c.get('evidence', '').strip() for c in criteria))
        if approved:
            if job.get('receipt') or r.get('job'):
                self.authenticate_review(task, job)
            r['phase'] = 'approved'
        else:
            r['phase'] = 'rejected'
        self.save()

    def status_update(self, tid):
        r = self.state['records'][tid]
        path = self.integration / 'docs/migration/STATUS.md'
        baseline_text = run(['git', 'show', r['merge'] + ':docs/migration/STATUS.md'], self.integration).stdout
        text = baseline_text.replace('Planning is complete; implementation has not begun.', 'Execution evidence is recorded below after independent review and integration checks.').replace('No worker or branch has been assigned or created.', 'Assignments and verified commits are recorded below.')
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f'| [{tid}]'):
                c = [s.strip() for s in line.strip('|').split('|')]
                c[4:9] = ['merged', r.get('worker_model', self.config['worker_model']), f'{r["branch"]} / {r["commit"]} / merge {r["merge"]}',
                          f'approved {r.get("model", self.config["reviewer_model"])}', f'passed; evidence {self.directory / "checks"}']
                lines[i] = '| ' + ' | '.join(c) + ' |'
        desired = '\n'.join(lines) + '\n'
        if path.read_text() not in (baseline_text, desired):
            raise Stop('Status content changed unexpectedly; preserve it and inspect')
        dirty = git(self.integration, 'status', '--porcelain', '--untracked-files=all').splitlines()
        if any(line[3:] != 'docs/migration/STATUS.md' for line in dirty):
            raise Stop('Unexpected dirty integration files during status recovery')
        path.write_text(desired)
        git(self.integration, 'add', '--', 'docs/migration/STATUS.md')
        git(self.integration, 'commit', '-m', f'migration: record verified {tid}')
        self.state['expected_head'] = git(self.integration, 'rev-parse', 'HEAD')
        self.save()

    def integrate(self, task):
        tid = task['id']
        r = self.state['records'][tid]
        if r['phase'] != 'status_pending':
            clean(self.integration)
        if r['phase'] == 'approved':
            if tid in self.config.get('manual_gates', []) and r.get('gate_commit') != r['commit']:
                raise Stop(f'{tid}: manual decision/contract ratification required. Use approve-gate after inspecting this commit')
            self.scope(tid)
            clean(r['worktree'])
            if git(self.integration, 'rev-parse', 'HEAD') != self.state['expected_head']:
                raise Stop('Integration HEAD changed outside coordinator')
            r['phase'] = 'merge_pending'
            r['pre_merge'] = self.state['expected_head']
            self.save()
        if r['phase'] == 'merge_pending':
            head = git(self.integration, 'rev-parse', 'HEAD')
            if not ancestor(self.integration, r['commit'], head):
                if head != r['pre_merge']:
                    raise Stop('Uncertain merge: HEAD differs; manual reconciliation required')
                # Conflict remains in place; never abort/discard/reset automatically.
                git(self.integration, 'merge', '--no-ff', '--no-edit', r['commit'])
            else:
                parents = git(self.integration, 'rev-list', '--parents', '-n', '1', head).split()
                if len(parents) != 3 or parents[1:] != [r['pre_merge'], r['commit']]:
                    raise Stop('Unexpected already-integrated state')
            r['merge'] = git(self.integration, 'rev-parse', 'HEAD')
            r['phase'] = 'integration_testing'
            self.state['expected_head'] = r['merge']
            self.save()
        if r['phase'] == 'integration_testing':
            if git(self.integration, 'rev-parse', 'HEAD') != r['merge']:
                raise Stop('Integration changed before verification')
            r['integration_evidence'] = self.checks(self.integration, self.task_checks(tid) + self.config['regression_checks'], tid + '-integration')
            clean(self.integration)
            r['phase'] = 'status_pending'
            self.save()
        if r['phase'] == 'status_pending':
            head = git(self.integration, 'rev-parse', 'HEAD')
            if head != r['merge']:
                clean(self.integration)
                # Only recover our exact one-file status commit; prove its ancestry/message/content.
                if (git(self.integration, 'rev-parse', 'HEAD^') != r['merge'] or
                    git(self.integration, 'log', '-1', '--format=%s') != f'migration: record verified {tid}' or
                    git(self.integration, 'diff', '--name-only', 'HEAD^', 'HEAD') != 'docs/migration/STATUS.md' or
                    r['commit'] not in (self.integration / 'docs/migration/STATUS.md').read_text()):
                    raise Stop('Uncertain status commit; inspect manually')
                self.state['expected_head'] = head
            else:
                self.status_update(tid)
            r['phase'] = 'merged'
            self.save()

    def checkpoint(self, tasks, wave):
        commands = []
        for t in tasks:
            if t['wave'] <= wave:
                for command in self.task_checks(t['id']):
                    if command not in commands:
                        commands.append(command)
        evidence = self.checks(self.integration, commands + self.config['regression_checks'], f'wave-{wave}')
        clean(self.integration)
        self.state['checkpoints'][str(wave)] = dict(head=git(self.integration, 'rev-parse', 'HEAD'), evidence=evidence)
        self.save()

    def execute(self, tasks):
        self.initialize(tasks)
        self.ensure_integration()
        if self.inputs(tasks) != self.state['input_digest']:
            raise Stop('Approved task/spec/plan inputs changed; revise plan before continuing')
        if self.state['blocked']:
            raise Stop(f'Run blocked: {str(self.state["blocked"])[:600]}; complete findings: {self.file}')
        records = self.state['records']
        recoverable = any(r['phase'] in ('merge_pending', 'status_pending') for r in records.values())
        if not recoverable:
            clean(self.integration)
            if git(self.integration, 'rev-parse', 'HEAD') != self.state['expected_head']:
                raise Stop('Integration HEAD changed outside coordinator; inspect before launching tasks')
        # Resume only existing intent. Never infer completion from markdown or a worker claim.
        while True:
            for tid, r in records.items():
                if r['phase'] == 'merged' and (not ancestor(self.integration, r['commit'], 'HEAD') or not ancestor(self.integration, r['merge'], 'HEAD')):
                    raise Stop(f'{tid}: recorded merge missing from integration')
            for wave in sorted({t['wave'] for t in tasks}):
                group = [t for t in tasks if t['wave'] == wave]
                if str(wave) not in self.state['checkpoints'] and all(records.get(t['id'], {}).get('phase') == 'merged' for t in group):
                    self.checkpoint(tasks, wave)
            # A rejected task is retained for explicit repair, while independent recorded
            # work in the same wave remains eligible for review and integration.
            unfinished_waves = [t['wave'] for t in tasks
                                if records.get(t['id'], {}).get('phase') != 'merged']
            earliest_wave = min(unfinished_waves) if unfinished_waves else None
            active = [t for t in tasks if t['id'] in records and
                      t['wave'] == earliest_wave and
                      records[t['id']]['phase'] not in ('merged', 'rejected', 'provider_blocked')]
            if not active:
                rejected_in_wave = any(
                    t['wave'] == earliest_wave and
                    records.get(t['id'], {}).get('phase') in ('rejected', 'provider_blocked')
                    for t in tasks)
                # Do not assign fresh work alongside a rejected attempt whose scope
                # remains occupied; only recorded peer work can safely drain.
                active = [] if rejected_in_wave else select(tasks, records, self.config['scopes'], self.max_workers)
                if not active:
                    if all(records.get(t['id'], {}).get('phase') == 'merged' for t in tasks):
                        print('All tasks integrated. No merge into main was performed.')
                        return
                    blockers = []
                    for task in tasks:
                        record = records.get(task['id'])
                        if not record or record['phase'] == 'merged':
                            continue
                        if record['phase'] == 'rejected':
                            review = record.get('review') or {}
                            detail = '; '.join(str(x) for x in review.get('findings', []))
                            if not detail:
                                detail = record.get('repair', 'independent review rejected this attempt')
                            blockers.append(f'{task["id"]} rejected: {detail}')
                        else:
                            blockers.append(f'{task["id"]} {record["phase"]}')
                    if not blockers:
                        blockers.append('no graph-ready task')
                    message = 'Safe work exhausted: ' + ' | '.join(blockers)
                    self.state['blocked'] = message
                    self.save()
                    raise Stop(message[:800] + f'; complete findings: {self.file}. No success claim; no merge into main was performed.')
                prior = {str(t['wave']) for t in tasks if t['wave'] < active[0]['wave']}
                if any(w not in self.state['checkpoints'] for w in prior):
                    raise Stop('Prior wave checkpoint missing')
            # Early collection of already terminal recorded reviewer receipts before scheduling/verifying peer workers.
            for t in list(active):
                r = records.get(t['id'])
                if not r:
                    continue
                if r['phase'] == 'review_finished':
                    job_path = Path(r.get('job', ''))
                    if job_path.is_file():
                        job = json.loads(job_path.read_text())
                        receipt_path = Path(job.get('receipt', ''))
                        if receipt_path.is_file():
                            self.authenticate_review(t, job)
                            self.accept_review(t, job)
                elif r['phase'] == 'review_running':
                    job_path = Path(r.get('job', ''))
                    if job_path.is_file():
                        job = json.loads(job_path.read_text())
                        receipt_path = Path(job.get('receipt', ''))
                        if receipt_path.is_file():
                            outcome, job = self.wait(t['id'])
                            self.authenticate_review(t, job)
                            self.accept_review(t, job)
            for t in active:
                r = self.prepare(t)
                if r['phase'] in ('planned', 'repair_pending'):
                    if sum(x['phase'] == 'worker_running' for x in records.values()) >= self.max_workers:
                        continue
                    self.launch(t, 'worker', self.prompt(t, r.get('repair', '')))
                elif r['phase'] == 'fallback_pending':
                    if sum(x['phase'] == 'worker_running' for x in records.values()) >= self.max_workers:
                        continue
                    self.launch(t, 'worker', self.prompt(t, r.get('repair', '')) + '\nSafe provider continuation plan:\n' + r['fallback_plan']['plan'], fallback=True)
            print('Active tasks: ' + ', '.join(t['id'] for t in active), flush=True)
            for t in active:
                r = records[t['id']]
                if r['phase'] == 'worker_running':
                    self.wait(t['id'])
                if r['phase'] == 'worker_finished':
                    outcome = r['exit']
                    job = json.loads(Path(r['job']).read_text())
                    if outcome.get('provider_failure') and job.get('provider') == 'agy':
                        self.handle_provider_failure(t, outcome, job)
                        continue
                    if outcome['exit'] != 0 or outcome.get('success') is False:
                        r['phase'] = 'rejected'
                        r['repair'] = f'Worker exited {outcome["exit"]}; inspect log and finish task.'
                        self.save()
                if r['phase'] == 'worker_finished':
                    self.verify_worker(t)
                if r['phase'] == 'committed':
                    self.launch_review(t)
            for t in active:
                r = records[t['id']]
                if r['phase'] == 'review_running':
                    outcome, job = self.wait(t['id'])
                    self.authenticate_review(t, job)
                    self.accept_review(t, job)
                elif r['phase'] == 'review_finished':
                    job = json.loads(Path(r['job']).read_text())
                    self.authenticate_review(t, job)
                    self.accept_review(t, job)
            for t in active:  # Task-column order, one merge and integration check at a time.
                self.integrate(t)
            waves = sorted({t['wave'] for t in active})
            for wave in waves:
                group = [t for t in tasks if t['wave'] == wave]
                if all(records.get(t['id'], {}).get('phase') == 'merged' for t in group):
                    self.checkpoint(tasks, wave)



def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['configure', 'status', 'watch', 'usage', 'run', 'resume', 'repair', 'authorize-repairs', 'retry-checks', 'approve-gate', 'reconcile', 'smoke', 'readiness', 'continue-fallback'])
    p.add_argument('--providers-config', type=Path, default=HERE / 'providers.json')
    p.add_argument('--auth-smoke', action='store_true', help='Readiness only: harmless cached-account headless prompt in a disposable repository')
    p.add_argument('--full', action='store_true', help='Include detailed usage attempts')
    p.add_argument('--repo', type=Path, default=Path.cwd())
    p.add_argument('--state-dir', type=Path)
    p.add_argument('--config', type=Path, default=HERE / 'config.json')
    p.add_argument('--approved-execution', action='store_true', help='Only supply after user approves actual migration execution')
    p.add_argument('--task')
    p.add_argument('--confirmed-stopped', action='store_true', help='For uncertain launch only: operator has checked that no runner or child remains')
    p.add_argument('--findings', help='Targeted repair instructions; required for repair')
    p.add_argument('--additional-attempts', type=int, help='One or two bounded attempts for an exhausted rejected task')
    p.add_argument('--reason', help='Concrete evidence supporting the additional repair budget')
    p.add_argument('--provenance', help='Provenance / smoke artifact path for explicit repair routing choice')
    p.add_argument('--select-validated-primary', nargs='?', const=True, default=False, help='Opt-in: select currently validated routing primary for the next coding repair only')
    p.add_argument('--select-primary', action='store_true', dest='select_validated_primary', help='Alias for --select-validated-primary')
    p.add_argument('--execution-guidance', choices=['routine-decisions'], help='Persist the user\'s standing routine-decision authorization in worker prompts')
    p.add_argument('--max-workers', type=int, choices=[1, 2], default=1, help='Runtime concurrency; default one, without changing frozen config')
    p.add_argument('--wait-seconds', type=float, default=1800, help='Read-only watch deadline')
    args = p.parse_args()
    repo = args.repo.resolve()
    if args.command == 'configure':
        if args.config.exists():
            raise Stop('Config exists; edit explicitly instead of overwriting')
        atomic(args.config, config_template(repo))
        print(args.config)
        return
    routing = validate_routing(json.loads(args.providers_config.read_text()))
    if args.command == 'readiness':
        from providers import preflight
        if args.auth_smoke:
            from providers import auth_smoke
            result = auth_smoke(routing['primary'], args.state_dir)
        else:
            result = preflight(routing['primary'], repo, routing.get('preflight_seconds', 30))
            result.pop('stdout', None)
            if result.get('stderr'):
                result['stderr'] = result['stderr'][-1500:]
        print(json.dumps(result, indent=2))
        if not result['ready']:
            raise Stop('Gemini readiness failed; inspect diagnostics')
        return
    if args.command == 'smoke':
        from smoke import smoke
        smoke(args.state_dir, routing=routing)
        return
    directory = (args.state_dir or (Path(git(repo, 'rev-parse', '--git-common-dir')) if Path(git(repo, 'rev-parse', '--git-common-dir')).is_absolute() else repo / git(repo, 'rev-parse', '--git-common-dir')) / 'migration-orchestrator').resolve()
    config = json.loads(args.config.read_text())
    config['python'] = config['python'].replace('{repo}', str(repo))
    if args.command == 'usage':
        from usage import usage_report
        report = usage_report(directory)
        if not args.full:
            report.pop('agents', None)
        print(json.dumps(report, indent=2))
        return
    if args.command == 'watch':
        if args.wait_seconds < 0:
            raise Stop('Watch timeout must be nonnegative')
        print(json.dumps(watch_state(directory, args.wait_seconds), indent=2))
        return
    if args.command == 'status':
        tasks = read_graph(repo)
        state = json.loads((directory / 'state.json').read_text()) if (directory / 'state.json').exists() else {'records': {}, 'blocked': None}
        for t in tasks:
            r = state['records'].get(t['id'], {})
            print(f'{t["id"]} wave={t["wave"]} plan={t["status"]} phase={r.get("phase", "unassigned")} commit={r.get("commit") or "-"} provider={r.get("worker_provider", "codex") if r else "-"} model={r.get("worker_model", config["worker_model"]) if r else "-"} stale_base={bool(r and r.get("base") != state.get("expected_head") and r.get("phase") != "merged")}')
        print(f'State: {directory}; blocked: {status_projection(state)["blocked"]}; full findings in state.json')
        return
    if not args.approved_execution:
        raise Stop('Actual migration is gated. Obtain user approval before supplying --approved-execution')
    with lock(directory):
        c = Coordinator(repo, directory, config, max_workers=args.max_workers, routing=routing)
        if args.command in ('run', 'resume'):
            try:
                c.execute(read_graph(repo))
            except (Stop, OSError, ValueError) as e:
                if c.state:
                    c.state['blocked'] = str(e)
                    c.save()
                raise
        else:
            if not c.state:
                raise Stop('No run to recover')
            if args.command == 'continue-fallback':
                c.continue_fallback(args.task, args.findings)
                print('Safe continuation plan recorded. Resume remains separately authorized.')
            elif args.command == 'authorize-repairs':
                entry = c.authorize_repairs(args.task, args.additional_attempts, args.reason, args.execution_guidance)
                print(f'Authorized {entry["additional_attempts"]} additional {args.task} attempt(s); limit {entry["new_limit"]}. State: {c.file}')
            elif args.command == 'retry-checks':
                clean(c.integration)
                # A failed merge or uncertainty cannot be cleared by claiming tests passed.
                if any(r['phase'] in ('merge_pending', 'worker_running', 'review_running') for r in c.state['records'].values()):
                    raise Stop('Uncertain process/merge requires manual reconciliation, not retry-checks')
                for r in c.state['records'].values():
                    if r['phase'] == 'review_finished':
                        if not args.reason or len(args.reason.strip()) < 20:
                            raise Stop('Fresh review requires a concrete retry reason after inspecting output')
                        history = r.get('review_retries', [])
                        if len([e for e in history if e['commit'] == r['commit']]) >= 1:
                            raise Stop('Review retry budget exhausted for this commit; inspect and repair with new evidence')
                        r.setdefault('review_retries', []).append(dict(commit=r['commit'], reason=args.reason.strip(), time=time.time()))
                        r['phase'] = 'committed'  # Fresh independent review; preserve the failed receipt.
                c.state['blocked'] = None
                c.save()
            else:
                if args.task not in c.state['records']:
                    raise Stop('Unknown assigned task')
                r = c.state['records'][args.task]
                if args.command == 'reconcile':
                    clean(c.integration)
                    if r['phase'] != 'merge_pending':
                        raise Stop('Reconcile only handles a completed pending merge; other uncertainty requires explicit inspection')
                    head = git(c.integration, 'rev-parse', 'HEAD')
                    parents = git(c.integration, 'rev-list', '--parents', '-n', '1', head).split()
                    if len(parents) != 3 or parents[1:] != [r['pre_merge'], r['commit']]:
                        raise Stop('Pending merge is not complete with the recorded parents')
                    tree_result = run(['git', 'merge-tree', '--write-tree', r['pre_merge'], r['commit']], c.integration, False)
                    if tree_result.returncode or tree_result.stdout.splitlines()[0] != git(c.integration, 'rev-parse', 'HEAD^{tree}'):
                        raise Stop('Conflict resolution changed the reviewed merge result; manual independent review is required')
                    r['merge'] = head
                    r['phase'] = 'integration_testing'
                    c.state['expected_head'] = head
                    c.state['blocked'] = None
                    c.save()
                    print('Reconciled pending merge. Use resume to continue.')
                elif args.command == 'approve-gate':
                    if r['phase'] != 'approved':
                        raise Stop('Gate requires independent approval first')
                    r['gate_commit'] = r['commit']
                    c.state['blocked'] = None
                    c.save()
                else:
                    choice_reason = args.provenance or args.reason or (args.select_validated_primary if isinstance(args.select_validated_primary, str) else None)
                    c.repair(args.task, args.findings, confirmed_stopped=args.confirmed_stopped,
                             select_validated_primary=bool(args.select_validated_primary), reason=choice_reason)
                    print('Recorded. Use resume to continue.')

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError) as e:
        print(f'STOP: {e}', file=sys.stderr)
        sys.exit(1)
