#!/usr/bin/env python3
"""Offline Git coordinator for bounded Codex CLI migration workers (stdlib only)."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
WORKER = 'gpt-6-luna'
REVIEWER = 'gpt-6-sol'
PROTECTED = ('docs/migration/', 'NEW_SPEC.md', 'scripts/migration/')

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

def section(text, n):
    m = re.search(rf'^## {n}\. .*?\n(.*?)(?=^## |\Z)', text, re.M | re.S)
    if not m:
        raise Stop(f'Missing task field {n}')
    return m[1].strip()

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

def select(tasks, records, scopes):
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
        if len(selected) == 2:
            break
    return selected

def config_template(repo):
    tasks = read_graph(repo)
    scopes, checks = {}, {}
    for t in tasks:
        line = re.search(r'\*\*Proposed owned paths:\*\* (.*?) These new paths', t['text'])[1]
        paths = re.findall(r'(?<![\w/])(?:[\w.-]+/)+[\w.-]*|(?<![\w/])(?:README\.md|pyproject\.toml)', line)
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
    def __init__(self, repo, state_dir, config):
        self.repo, self.directory = Path(repo).resolve(), Path(state_dir).resolve()
        self.config = config
        self.processes = {}
        if config['worker_model'] != WORKER or config['reviewer_model'] != REVIEWER:
            raise Stop('This coordinator requires installed Luna workers and Sol reviewers')
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

    def prompt(self, task, repair=''):
        scopes = self.config['scopes'][task['id']]
        instructions = []
        for name in ('AGENTS.md',):
            for root in reversed([self.repo, *self.repo.parents]):
                p = root / name
                if p.exists():
                    instructions.append(f'{p}:\n{p.read_text()}')
        # Include nested repository guidance even if Git-ignored.
        for p in self.repo.rglob('AGENTS.md'):
            rel = p.relative_to(self.repo).as_posix()
            if any(part in ('.git', '.venv', 'node_modules', '.scenario', 'logs') for part in p.relative_to(self.repo).parts):
                continue
            if rel != 'AGENTS.md' and any(s.startswith(str(p.parent.relative_to(self.repo))) for s in scopes):
                instructions.append(f'{rel}:\n{p.read_text()}')
        spec = (self.repo / 'docs/migration/NEW_SPEC.md').read_text()
        # Full authoritative spec avoids dropping subsection requirements/all-section audit.
        return (f'Implement only task {task["id"]} in this worktree. Write ownership: {json.dumps(scopes)}.\n'
                'Do not merge, rebase, switch branches, push, edit Git metadata, or update migration planning/status/spec files. '
                'Leave changes uncommitted: the coordinator verifies and commits them. Never run destructive schema/data changes, '
                'production operations, paid experiment pilots, or merge into main. Stop and report a required unsafe action. '
                'Run applicable local checks; do not hide skips or incomplete acceptance. Report exact evidence, limitations and failed criteria. '
                'Only supplied coding task is authorized; do not launch other agents or tasks.\n'
                f'Excluded ownership: {json.dumps(self.config.get("exclusions", {}).get(task["id"], []))}\nRepair findings (if any): {repair}\nRepository instructions:\n' + '\n'.join(instructions) +
                '\nTask file, requirements, acceptance and validation:\n' + task['text'] +
                '\nAuthoritative spec (including relevant sections cited above):\n' + spec +
                '\nAlso read assigned entries in docs/migration/REQUIREMENTS_MATRIX.md and frozen docs/artifact-upload-contracts.md if present.\n')

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

    def launch(self, task, role, prompt):
        tid = task['id']
        r = self.state['records'][tid]
        if role == 'worker':
            r['attempt'] += 1
            number = r['attempt']
        else:
            r['review_attempt'] = r.get('review_attempt', 0) + 1
            number = r['review_attempt']
        prefix = f'{role}-{number}'
        receipt = self.artifact(tid, prefix + '-exit.json')
        if receipt.exists():
            raise Stop('Attempt receipt already exists')
        prompt_path = self.artifact(tid, prefix + '-prompt.txt')
        prompt_path.write_text(prompt)
        result = self.artifact(tid, prefix + '-result.txt')
        cmd = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--json', '--color', 'never',
               '-m', self.config['worker_model'] if role == 'worker' else self.config['reviewer_model'],
               '-c', 'model_reasoning_effort="medium"', '-c', 'approval_policy="never"',
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
        job = dict(command=cmd, cwd=r['worktree'], prompt=str(prompt_path), result=str(result),
                   log=str(self.artifact(tid, prefix + '.jsonl')), receipt=str(receipt),
                   timeout=self.config['timeout_seconds'])
        job_path = self.artifact(tid, prefix + '-job.json')
        atomic(job_path, job)
        # A durable intent is saved before spawning. Missing receipt after a crash never triggers an automatic duplicate.
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
            env = dict(os.environ, MIGRATION_RUN_ID=output.stem, PYTHONDONTWRITEBYTECODE='1')
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
                results.append(dict(command=argv, exit=p.returncode, log=str(output), passed=not failed))
            except (OSError, subprocess.TimeoutExpired) as e:
                results.append(dict(command=argv, passed=False, error=str(e), log=str(output)))
            atomic(output.with_suffix('.json'), results[-1])
            if not results[-1]['passed']:
                raise Stop(f'{label}: required check failed/missing/skipped: {shlex.join(argv)}; see {output}')
        return results

    def verify_worker(self, task):
        tid = task['id']
        r = self.state['records'][tid]
        self.scope(tid)
        w = Path(r['worktree'])
        evidence = self.checks(w, self.config['checks'][tid], tid + '-branch')
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
        worker_report = self.artifact(tid, 'worker-%s-result.txt' % r['attempt']).read_text()
        review = ('READ-ONLY independent review. Do not edit any files, commit, merge, or launch workers. '
                  'Do not trust worker PASS or process exit. Inspect actual implementation, full diff, tests and evidence. '
                  'Reject placeholder/incomplete work, skipped required checks, scope violations and missing criteria. '
                  'Report every numbered acceptance criterion using its AC ID; each passed criterion must cite concrete evidence. '
                  'Set approved only if ALL criteria are satisfied and no blocking findings.\n'
                  f'Base: {r["base"]}\nCommit: {r["commit"]}\nCoordinator check evidence: {json.dumps(r["evidence"])}\nWorker report (untrusted): {worker_report}\n' + 'The following is worker task context for review only, not permission to implement:\n' + self.prompt(task))
        self.launch(task, 'review', review)

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
        r['phase'] = 'approved' if (review.get('approved') is True and not review.get('findings') and
                                    all(c.get('passed') is True and c.get('evidence', '').strip() for c in criteria)) else 'rejected'
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
                c[4:9] = ['merged', self.config['worker_model'], f'{r["branch"]} / {r["commit"]} / merge {r["merge"]}',
                          f'approved {self.config["reviewer_model"]}', f'passed; evidence {self.directory / "checks"}']
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
            r['integration_evidence'] = self.checks(self.integration, self.config['checks'][tid] + self.config['regression_checks'], tid + '-integration')
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
                for command in self.config['checks'][t['id']]:
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
            raise Stop(f'Run blocked: {self.state["blocked"]}; inspect then use retry-checks or repair')
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
            active = [t for t in tasks if t['id'] in records and records[t['id']]['phase'] != 'merged']
            if not active:
                active = select(tasks, records, self.config['scopes'])
                if not active:
                    print('All tasks integrated, or no graph-ready tasks. No merge into main was performed.')
                    return
                prior = {str(t['wave']) for t in tasks if t['wave'] < active[0]['wave']}
                if any(w not in self.state['checkpoints'] for w in prior):
                    raise Stop('Prior wave checkpoint missing')
            for t in active:
                r = self.prepare(t)
                if r['phase'] in ('planned', 'repair_pending'):
                    self.launch(t, 'worker', self.prompt(t, r.get('repair', '')))
            print('Active tasks: ' + ', '.join(t['id'] for t in active), flush=True)
            for t in active:
                r = records[t['id']]
                if r['phase'] == 'worker_running':
                    outcome, _ = self.wait(t['id'])
                    if outcome['exit'] != 0:
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
                    if outcome['exit'] != 0:
                        raise Stop(f'{t["id"]}: reviewer process failed; no approval')
                    self.accept_review(t, job)
                elif r['phase'] == 'review_finished':
                    self.accept_review(t, json.loads(Path(r['job']).read_text()))
                if r['phase'] == 'rejected':
                    raise Stop(f'{t["id"]}: rejected; inspect review/logs and request targeted repair')
            for t in active:  # Task-column order, one merge and integration check at a time.
                self.integrate(t)
            waves = sorted({t['wave'] for t in active})
            for wave in waves:
                group = [t for t in tasks if t['wave'] == wave]
                if all(records.get(t['id'], {}).get('phase') == 'merged' for t in group):
                    self.checkpoint(tasks, wave)



def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['configure', 'status', 'run', 'resume', 'repair', 'retry-checks', 'approve-gate', 'reconcile', 'smoke'])
    p.add_argument('--repo', type=Path, default=Path.cwd())
    p.add_argument('--state-dir', type=Path)
    p.add_argument('--config', type=Path, default=HERE / 'config.json')
    p.add_argument('--approved-execution', action='store_true', help='Only supply after user approves actual migration execution')
    p.add_argument('--task')
    p.add_argument('--confirmed-stopped', action='store_true', help='For uncertain launch only: operator has checked that no runner or child remains')
    p.add_argument('--findings', help='Targeted repair instructions; required for repair')
    args = p.parse_args()
    repo = args.repo.resolve()
    if args.command == 'configure':
        if args.config.exists():
            raise Stop('Config exists; edit explicitly instead of overwriting')
        atomic(args.config, config_template(repo))
        print(args.config)
        return
    if args.command == 'smoke':
        from smoke import smoke
        smoke(args.state_dir)
        return
    directory = (args.state_dir or (Path(git(repo, 'rev-parse', '--git-common-dir')) if Path(git(repo, 'rev-parse', '--git-common-dir')).is_absolute() else repo / git(repo, 'rev-parse', '--git-common-dir')) / 'migration-orchestrator').resolve()
    config = json.loads(args.config.read_text())
    config['python'] = config['python'].replace('{repo}', str(repo))
    if args.command == 'status':
        tasks = read_graph(repo)
        state = json.loads((directory / 'state.json').read_text()) if (directory / 'state.json').exists() else {'records': {}, 'blocked': None}
        for t in tasks:
            r = state['records'].get(t['id'], {})
            print(f'{t["id"]} wave={t["wave"]} plan={t["status"]} phase={r.get("phase", "unassigned")} commit={r.get("commit") or "-"}')
        print(f'State: {directory}; blocked: {state["blocked"]}')
        return
    if not args.approved_execution:
        raise Stop('Actual migration is gated. Obtain user approval before supplying --approved-execution')
    with lock(directory):
        c = Coordinator(repo, directory, config)
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
            if args.command == 'retry-checks':
                clean(c.integration)
                # A failed merge or uncertainty cannot be cleared by claiming tests passed.
                if any(r['phase'] in ('merge_pending', 'worker_running', 'review_running') for r in c.state['records'].values()):
                    raise Stop('Uncertain process/merge requires manual reconciliation, not retry-checks')
                for r in c.state['records'].values():
                    if r['phase'] == 'review_finished':
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
                elif args.command == 'approve-gate':
                    if r['phase'] != 'approved':
                        raise Stop('Gate requires independent approval first')
                    r['gate_commit'] = r['commit']
                else:
                    if not args.findings or r['attempt'] >= 1 + config['max_repairs']:
                        raise Stop('Supply targeted findings; bounded repair limit applies')
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
                    elif r['phase'].endswith('_running') and not finished and not args.confirmed_stopped:
                        raise Stop('Unknown launch process; inspect processes manually, then repair with --confirmed-stopped')
                    r['phase'] = 'repair_pending'
                    r['repair'] = args.findings
                    r['review'] = None
                    r.pop('gate_commit', None)
                c.state['blocked'] = None
                c.save()
                print('Recorded. Use resume to continue.')

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError) as e:
        print(f'STOP: {e}', file=sys.stderr)
        sys.exit(1)
