"""Coordinator safety tests use real temporary Git repositories and a simulated CLI."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
import orchestrate

from orchestrate import Coordinator, Stop, atomic, config_template, digest, focused_context, git, lock, overlap, pytest_evidence, read_graph, select, watch_state
from usage import log_usage, usage_report
from smoke import fixture
from providers import snapshot

FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args=sys.argv[1:]
cwd=pathlib.Path(args[args.index('-C')+1])
out=pathlib.Path(args[args.index('-o')+1])
prompt=sys.stdin.read()
assert 'model_reasoning_effort="medium"' in args
assert 'approval_policy="never"' in args
review='--output-schema' in args
assert args[args.index('-m')+1] == ('gpt-6-sol' if review else 'gpt-6-luna')
assert args[args.index('-s')+1] == ('read-only' if review else 'workspace-write')
time.sleep(float(os.environ.get('FAKE_DELAY','0.05')))
word='alpha' if cwd.name=='T001' else 'beta'
if not review:
 p=cwd/f'docs/smoke-{word}.md'
 p.write_text(f'# Smoke {word}\\n\\nThis is the {word} orchestration smoke document.\\n')
 out.write_text('Worker claims PASS; coordinator must still verify.')
else:
 out.write_text(json.dumps(dict(approved=True,findings=[],criteria=[dict(id='AC1',passed=True,evidence='exact bytes checked'),dict(id='AC2',passed=True,evidence='Git diff shows one owned document')])) )
print(json.dumps(dict(type='turn.completed')))
'''

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='migration-tests-')
        self.root = Path(self.tmp.name)
        self.repo, self.config = fixture(self.root)
        bindir = self.root / 'bin'
        bindir.mkdir()
        (bindir / 'codex').write_text(FAKE)
        (bindir / 'codex').chmod(0o755)
        self.env = patch.dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        self.env.start()
        self.c = Coordinator(self.repo, self.root / 'state', self.config, max_workers=2)
        self.tasks = read_graph(self.repo)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_parallel_review_merge_and_idempotent_resume(self):
        self.c.execute(self.tasks)
        before = self.c.file.read_bytes()
        self.c.execute(self.tasks)
        self.assertEqual(before, self.c.file.read_bytes())
        self.assertEqual([r['phase'] for r in self.c.state['records'].values()], ['merged', 'merged'])
        exits = [json.loads(self.c.artifact(t['id'],'worker-1-exit.json').read_text()) for t in self.tasks]
        self.assertLess(max(e['started'] for e in exits), min(e['finished'] for e in exits))
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.c.state['baseline'])
        log=git(self.c.integration,'log','--format=%s')
        self.assertIn('migration: record verified T001',log)
        self.assertIn('migration: record verified T002',log)

    def test_default_serial_workers_and_frozen_config_resume(self):
        c = Coordinator(self.repo, self.root / 'state', self.config)
        c.execute(self.tasks)
        exits = [json.loads(c.artifact(t['id'], 'worker-1-exit.json').read_text()) for t in self.tasks]
        self.assertLessEqual(exits[0]['finished'], exits[1]['started'])
        self.assertTrue(all(r['phase'] == 'merged' for r in c.state['records'].values()))
        before = c.file.read_bytes()
        Coordinator(self.repo, self.root / 'state', self.config, max_workers=2).execute(self.tasks)
        self.assertEqual(before, c.file.read_bytes())
        self.assertEqual(len(select(self.tasks, {}, self.config['scopes'])), 1)
        self.assertEqual(len(select(self.tasks, {}, self.config['scopes'], 2)), 2)

    def test_default_capacity_drains_existing_planned_tasks(self):
        self.c.initialize(self.tasks)
        for t in self.tasks:
            self.c.prepare(t)
        c = Coordinator(self.repo, self.root / 'state', self.config)
        c.execute(self.tasks)
        exits = [json.loads(c.artifact(t['id'], 'worker-1-exit.json').read_text()) for t in self.tasks]
        self.assertLessEqual(exits[0]['finished'], exits[1]['started'])
        self.assertTrue(all(r['attempt'] == 1 for r in c.state['records'].values()))

    def test_watch_reports_events_without_writes_or_launches(self):
        import threading
        self.c.initialize(self.tasks)
        self.c.prepare(self.tasks[0])
        before = self.c.file.read_bytes()
        self.assertEqual(watch_state(self.c.directory, 0)['event'], 'timeout')
        self.assertEqual(before, self.c.file.read_bytes())
        def update():
            time.sleep(.05)
            self.c.state['blocked'] = 'Concrete blocker'
            self.c.save()
        thread = threading.Thread(target=update)
        thread.start()
        try:
            self.assertEqual(watch_state(self.c.directory, 3)['blocked'], 'Concrete blocker')
        finally:
            thread.join()
        before = self.c.file.read_bytes()
        self.assertEqual(watch_state(self.c.directory, 0)['blocked'], 'Concrete blocker')
        self.assertEqual(before, self.c.file.read_bytes())
        self.assertFalse(list(self.c.directory.glob('tasks/*/*-job.json')))

    def test_focused_context_preserves_every_owned_matrix_clause(self):
        repo = Path(__file__).resolve().parents[2]
        spec = (repo / 'docs/migration/NEW_SPEC.md').read_text()
        matrix = (repo / 'docs/migration/REQUIREMENTS_MATRIX.md').read_text()
        for task in read_graph(repo):
            text = focused_context(repo, task, spec)
            for row in matrix.splitlines():
                if f'[{task["id"]} AC' in row:
                    self.assertIn(row, text)
            if task['id'] == 'T018':
                self.assertIn(spec, text)
            else:
                self.assertLess(len(text), len(spec) + len(matrix))
        t = dict(self.tasks[0], text=self.tasks[0]['text'])
        self.assertIn('Documentation smoke', focused_context(self.repo, t, (self.repo / 'docs/migration/NEW_SPEC.md').read_text()))

    def test_reviewer_context_is_read_only_and_evidence_directed(self):
        self.c.initialize(self.tasks)
        t = self.tasks[0]
        r = self.c.prepare(t)
        r['attempt'] = 1
        (Path(r['worktree']) / 'docs/smoke-alpha.md').write_text('# Smoke alpha\n\nThis is the alpha orchestration smoke document.\n')
        self.c.artifact('T001', 'worker-1-result.txt').write_text('Untrusted PASS')
        self.c.verify_worker(t)
        with patch.object(self.c, 'launch') as launch:
            self.c.launch_review(t)
        prompt = launch.call_args.args[2]
        self.assertNotIn('Implement only task', prompt)
        self.assertIn(f'git diff {r["base"]} {r["commit"]}', prompt)
        self.assertIn('every numbered acceptance criterion', prompt)
        self.assertIn('Coordinator check evidence', prompt)
        self.assertIn('instead of rerunning successful checks', prompt)

    def recovery_command(self, *args):
        config = self.root / 'cli-config.json'
        atomic(config, self.config)
        return subprocess.run([sys.executable, str(Path(__file__).with_name('orchestrate.py')),
            *args, '--repo', str(self.repo), '--state-dir', str(self.c.directory),
            '--config', str(config), '--approved-execution'], text=True, capture_output=True)

    def test_repair_requires_new_plan_and_retains_review_evidence(self):
        self.c.initialize(self.tasks)
        r = self.c.prepare(self.tasks[0])
        review = dict(approved=False, findings=['Concrete defect'], criteria=[])
        r.update(phase='rejected', attempt=1, review=review)
        self.c.save()
        findings = 'Retained evidence shows a concrete defect; repair it and test its regression.'
        result = self.recovery_command('repair', '--task', 'T001', '--findings', findings)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.c.file.read_text())
        record = state['records']['T001']
        self.assertEqual(record['phase'], 'repair_pending')
        self.assertEqual(record['prior_review'], review)
        self.assertEqual(record['repair_history'][0]['review'], review)
        before = self.c.file.read_bytes()
        result = self.recovery_command('repair', '--task', 'T001', '--findings', findings)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Repeated repair plan', result.stderr)
        self.assertEqual(before, self.c.file.read_bytes())
        self.assertFalse(list(self.c.directory.glob('tasks/*/*-job.json')))

    def test_unchanged_commit_review_retry_is_reasoned_and_bounded(self):
        self.c.initialize(self.tasks)
        r = self.c.prepare(self.tasks[0])
        r.update(phase='review_finished', attempt=1, commit=r['base'])
        self.c.save()
        before = self.c.file.read_bytes()
        result = self.recovery_command('retry-checks')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('concrete retry reason', result.stderr)
        self.assertEqual(before, self.c.file.read_bytes())
        reason = 'Inspected invalid schema output; rerun one independent review.'
        result = self.recovery_command('retry-checks', '--reason', reason)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.c.file.read_text())
        state['records']['T001']['phase'] = 'review_finished'
        atomic(self.c.file, state)
        before = self.c.file.read_bytes()
        result = self.recovery_command('retry-checks', '--reason', reason)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Review retry budget exhausted', result.stderr)
        self.assertEqual(before, self.c.file.read_bytes())
        self.assertFalse(list(self.c.directory.glob('tasks/*/*-job.json')))

    def test_rejected_task_does_not_block_recorded_peer(self):
        self.c.initialize(self.tasks)
        rejected = self.c.prepare(self.tasks[0])
        rejected.update(phase='rejected', attempt=2,
                        review={'approved': False, 'findings':['Review found a concrete defect.'],
                                'criteria': []})
        peer = self.c.prepare(self.tasks[1])
        peer['attempt'] = 1
        w = Path(peer['worktree'])
        (w / 'docs/smoke-beta.md').write_text(
            '# Smoke beta\n\nThis is the beta orchestration smoke document.\n')
        self.c.verify_worker(self.tasks[1])
        peer['phase'] = 'approved'
        peer['review'] = {'approved': True, 'findings': [], 'criteria': [
            {'id':'AC1','passed':True,'evidence':'Exact expected bytes are present.'},
            {'id':'AC2','passed':True,'evidence':'Only the assigned file changed.'}]}
        self.c.save()
        with self.assertRaisesRegex(Stop, 'Safe work exhausted.*No success claim'):
            self.c.execute(self.tasks)
        self.assertEqual(rejected['phase'], 'rejected')
        self.assertEqual(peer['phase'], 'merged')
        self.assertIn('Safe work exhausted', self.c.state['blocked'])
        self.assertIn('T001 rejected: Review found a concrete defect.', self.c.state['blocked'])
        self.assertNotIn('No success claim', self.c.state['blocked'])
        self.assertNotIn('1', self.c.state['checkpoints'])
        self.assertEqual(peer['attempt'], 1)

    def test_rejected_task_blocks_new_same_wave_assignment(self):
        self.config['scopes']['T002'] = self.config['scopes']['T001']
        self.c.initialize(self.tasks)
        rejected = self.c.prepare(self.tasks[0])
        rejected.update(phase='rejected', attempt=2,
                        review={'approved': False, 'findings':['Review found a concrete defect.'],
                                'criteria': []})
        self.c.save()
        with patch.object(self.c, 'launch') as launch:
            with self.assertRaisesRegex(Stop, 'Safe work exhausted'):
                self.c.execute(self.tasks)
        self.assertNotIn('T002', self.c.state['records'])
        launch.assert_not_called()
        self.assertIn('T001 rejected: Review found a concrete defect.', self.c.state['blocked'])

    def test_failed_integration_never_marks_merged(self):
        self.config['regression_checks']=[[sys.executable,'-c','raise SystemExit(7)']]
        with self.assertRaisesRegex(Stop,'required check failed'):
            self.c.execute(self.tasks)
        self.assertEqual(self.c.state['records']['T001']['phase'],'integration_testing')
        self.assertNotIn('| merged |', (self.c.integration/'docs/migration/STATUS.md').read_text())
        self.assertFalse(self.c.state['checkpoints'])

    def test_worker_pass_with_wrong_bytes_fails(self):
        self.c.initialize(self.tasks)
        t=self.tasks[0]
        r=self.c.prepare(t)
        r['attempt']=1
        (Path(r['worktree'])/'docs/smoke-alpha.md').write_text('PASS')
        with self.assertRaisesRegex(Stop,'required check failed'):
            self.c.verify_worker(t)
        self.assertIsNone(r['commit'])

    def test_protected_and_unowned_files_rejected(self):
        self.c.initialize(self.tasks)
        r=self.c.prepare(self.tasks[0])
        w=Path(r['worktree'])
        (w/'docs/migration/STATUS.md').write_text('PASS')
        with self.assertRaisesRegex(Stop,'out-of-scope'):
            self.c.scope('T001')

    def test_symlink_rejected(self):
        self.c.initialize(self.tasks)
        r=self.c.prepare(self.tasks[0])
        (Path(r['worktree'])/'docs/smoke-alpha.md').symlink_to(self.repo/'NEW_SPEC.md')
        with self.assertRaisesRegex(Stop,'unsafe deliverable'):
            self.c.scope('T001')

    def test_skipped_required_check_fails(self):
        with self.assertRaisesRegex(Stop,'skipped'):
            self.c.checks(self.repo,[[sys.executable,'-c','print("1 passed, 2 skipped")']], 'skip')

    def test_pytest_check_accepts_only_exact_command_and_failure(self):
        checks = self.repo / 'checks'
        checks.mkdir()
        source = checks / 'test_baseline.py'
        source.write_text('def test_ok():\n    pass\n\ndef test_known():\n    assert False, "known"\n')
        command = ['{python}', '-m', 'pytest', '-q', 'checks']
        with self.assertRaisesRegex(Stop, 'required check failed'):
            self.c.checks(self.repo, [command], 'baseline-unapproved')
        report = next((self.c.directory / 'checks/baseline-unapproved').glob('*.xml'))
        case = ET.parse(report).find('.//testcase/failure/..')
        failure = case.find('failure')
        self.config['baseline_exceptions'] = [dict(
            command=command, classname=case.get('classname'), name=case.get('name'),
            failure_sha256=digest(json.dumps(dict(message=failure.get('message'), text=failure.text), sort_keys=True)),
            source_sha256={'checks/test_baseline.py': digest(source.read_text())})]
        result = self.c.checks(self.repo, [command], 'baseline-approved')[0]
        self.assertTrue(result['passed'])
        self.assertEqual(result['exit'], 1)
        self.assertEqual(result['tests_failed'], 1)
        self.assertEqual(len(result['accepted_baseline_exceptions']), 1)
        # Task briefs use .venv/bin/python; it resolves to the same interpreter.
        task_command = ['.venv/bin/python', *command[1:]]
        self.assertTrue(self.c.checks(self.repo, [task_command], 'same-resolved-command')[0]['passed'])
        with self.assertRaisesRegex(Stop, 'required check failed'):
            self.c.checks(self.repo, [command + ['-x']], 'different-command')
        (checks / 'test_new.py').write_text('def test_new():\n    assert False, "new regression"\n')
        with self.assertRaisesRegex(Stop, 'required check failed'):
            self.c.checks(self.repo, [command], 'additional-failure')

    def test_dependency_wave_and_ownership(self):
        tasks=[dict(id='A',wave=1,deps=[]),dict(id='B',wave=1,deps=[]),dict(id='C',wave=2,deps=['A'])]
        scopes={'A':['docs/'],'B':['docs/a.md'],'C':['code.py']}
        self.assertTrue(overlap(scopes['A'],scopes['B']))
        self.assertEqual([x['id'] for x in select(tasks,{},scopes)],['A'])
        self.assertEqual([x['id'] for x in select(tasks,{'A':{'phase':'merged'}},scopes)],['B'])

    def test_single_coordinator_lock(self):
        with lock(self.root/'locks'):
            with self.assertRaisesRegex(Stop,'Another coordinator'):
                with lock(self.root/'locks'):
                    pass

    def test_config_changes_block_resume(self):
        self.c.initialize(self.tasks)
        changed=dict(self.config,timeout_seconds=1)
        with self.assertRaisesRegex(Stop,'config differs'):
            Coordinator(self.repo,self.root/'state',changed)

    def test_gate_is_commit_specific(self):
        self.config['manual_gates']=['T001']
        with self.assertRaisesRegex(Stop,'ratification'):
            self.c.execute(self.tasks)
        r=self.c.state['records']['T001']
        self.assertEqual(r['phase'],'approved')
        r['gate_commit']=r['commit']
        self.c.state['blocked']=None
        self.c.save()
        self.c.execute(self.tasks)
        self.assertEqual(r['phase'],'merged')

    def test_targeted_repair_retains_previous_commit(self):
        self.c.initialize(self.tasks)
        t=self.tasks[0]
        r=self.c.prepare(t)
        r['attempt']=1
        w=Path(r['worktree'])
        (w/'docs/smoke-alpha.md').write_text('# Smoke alpha\n\nThis is the alpha orchestration smoke document.\n')
        self.c.verify_worker(t)
        old=r['commit']
        (w/'docs/smoke-alpha.md').write_text('# Smoke alpha\n\nThis is the alpha orchestration smoke document.\n\nRepair evidence.\n')
        # Explicit different acceptance for this targeted simulated repair.
        self.config['checks']['T001']=[[sys.executable,'-c','from pathlib import Path; assert "Repair evidence." in Path("docs/smoke-alpha.md").read_text()']]
        r['attempt']=2
        self.c.verify_worker(t)
        self.assertNotEqual(old,r['commit'])
        self.assertEqual(git(w,'rev-parse','HEAD^'),old)

    def test_bounded_repair_authorization_preserves_gates_and_audit(self):
        self.c.initialize(self.tasks)
        r = self.c.prepare(self.tasks[0])
        r.update(phase='rejected', attempt=3, review={'approved': False}, commit=r['base'])
        self.c.state['blocked'] = 'T001: rejected'
        self.c.save()
        before = self.c.file.read_bytes()
        with self.assertRaisesRegex(Stop, '20–2000'):
            self.c.authorize_repairs('T001', 1, 'vague')
        with self.assertRaisesRegex(Stop, 'one or two'):
            self.c.authorize_repairs('T001', 3, 'Fresh review found a specific journal correlation defect.')
        self.assertEqual(self.c.file.read_bytes(), before)
        entry = self.c.authorize_repairs(
            'T001', 1, 'Fresh review found journal/result identity mismatch; repair all four key fields.',
            'routine-decisions')
        self.assertEqual((entry['previous_limit'], entry['new_limit']), (3, 4))
        self.assertEqual(r['phase'], 'rejected')
        self.assertEqual(r['review'], {'approved': False})
        self.assertEqual(self.c.state['blocked'], 'T001: rejected')
        self.assertNotIn('gate_commit', r)
        persisted = Coordinator(self.repo, self.root / 'state', self.config)
        self.assertEqual(persisted.repair_limit(persisted.state['records']['T001']), 4)
        self.assertEqual(persisted.state['records']['T001']['repair_authorizations'], [entry])
        prompt = persisted.prompt(self.tasks[0])
        self.assertIn('authorized routine NEW_SPEC-consistent technical selections', prompt)
        self.assertNotIn('do not finalize a candidate', prompt)
        self.assertIn('independent review', prompt)
        self.assertIn('runtime pin changes', prompt)
        with self.assertRaisesRegex(Stop, 'existing repair budget'):
            persisted.authorize_repairs('T001', 1, entry['reason'])
        persisted.state['records']['T001']['attempt'] = 4
        persisted.save()
        before = persisted.file.read_bytes()
        with self.assertRaisesRegex(Stop, 'Repeated repair reason'):
            persisted.authorize_repairs('T001', 1, entry['reason'])
        self.assertEqual(before, persisted.file.read_bytes())

    def test_authorization_rejects_changed_inputs_and_branch(self):
        self.c.initialize(self.tasks)
        r = self.c.prepare(self.tasks[0])
        r.update(phase='rejected', attempt=3)
        self.c.save()
        reason = 'Fresh review found a concrete journal/result identity mismatch.'
        (self.repo / 'NEW_SPEC.md').write_text('Changed input')
        with self.assertRaisesRegex(Stop, 'inputs changed'):
            self.c.authorize_repairs('T001', 1, reason)
        git(self.repo, 'checkout', '--', 'NEW_SPEC.md')
        r['commit'] = '0' * 40
        with self.assertRaisesRegex(Stop, 'branch/HEAD changed'):
            self.c.authorize_repairs('T001', 1, reason)

    def terminal_check_failure_fixture(self):
        # Each subtest gets a fresh retained-evidence tree even when it shares
        # the surrounding temporary coordinator/worktree fixture.
        task_dir = self.c.directory / 'tasks' / 'T001'
        check_dir = self.c.directory / 'checks' / 'T001-branch'
        for old in (task_dir, check_dir):
            if old.is_symlink():
                old.unlink()
            else:
                shutil.rmtree(old, ignore_errors=True)
        worktree = (self.c.state or {}).get('records', {}).get('T001', {}).get('worktree')
        if worktree:
            for path in Path(worktree).glob('untracked_mutation.txt'):
                path.unlink()
        if self.c.state:
            prior = self.c.state.get('records', {}).get('T001')
            if prior:
                prior['commit'] = None
                prior.pop('gate_commit', None)
                prior['review'] = None
        self.c.initialize(self.tasks)
        r = self.c.prepare(self.tasks[0])
        task_dir.mkdir(parents=True, exist_ok=True)
        r.update(phase='worker_finished', attempt=3, exit=None, pid=99999998,
                 commit=None, review=None, repair_authorizations=[])
        r.pop('gate_commit', None)
        job_path = task_dir / 'worker-3-job.json'
        log = task_dir / 'worker-3.jsonl'
        result = task_dir / 'worker-3-result.txt'
        log.write_text('{}\n')
        result.write_text('finished')
        now = time.time() - 10
        job = dict(task='T001', role='worker', attempt=3, cwd=r['worktree'], receipt=str(task_dir/'worker-3-exit.json'),
                   log=str(log), result=str(result), provider='codex', model='gpt-6-luna')
        atomic(job_path, job)
        # Model job.py's durable job write before the runner and receipt starts.
        os.utime(job_path, (now - 0.02, now - 0.02))
        r['job'] = str(job_path)
        # job.py records runner start just before receipt.started and child
        # start just after it, matching the genuine launcher ordering.
        atomic(str(job_path)+'.pid.json', dict(pid=99999998, started=now - 0.01))
        atomic(str(job_path)+'.child.json', dict(pid=99999997, started=now + 0.01))
        check_dir = self.c.directory / 'checks' / 'T001-branch'
        check_dir.mkdir(parents=True)
        stem = 'a' * 32 + '-0'
        check_log = check_dir / (stem + '.log')
        check_log.write_text('simulated check failure\n')
        artifact_dir = check_dir / stem
        artifact_dir.mkdir()
        template = self.c.task_checks('T001')[0]
        argv = [x.replace('{python}', self.config['python']).replace('$USER', stem).replace('{output}', str(artifact_dir)) for x in template]
        if argv[0] == '.venv/bin/python':
            argv[0] = self.config['python']
        check = dict(command=argv, exit=2, log=str(check_log), passed=False, origin='coordinator', original_command=template,
                     cwd=str(Path(r['worktree']).resolve()), source_head=git(r['worktree'], 'rev-parse', 'HEAD'),
                     source_snapshot_sha256=digest(json.dumps(snapshot(r['worktree']), sort_keys=True)),
                     artifacts=[dict(path=str(check_log), bytes=check_log.stat().st_size,
                                     sha256=hashlib.sha256(check_log.read_bytes()).hexdigest())])
        check_path = check_dir / (stem + '.json')
        atomic(check_path, check)
        receipt = dict(exit=0, error=None, started=now, finished=now+1, log=str(log), result=str(result),
                       task='T001', role='worker', attempt=3, provider='codex', model='gpt-6-luna')
        atomic(job['receipt'], receipt)
        r['exit'] = receipt
        self.c.state['blocked'] = f"T001-branch: required check failed/missing/skipped: {' '.join(argv)}; see {check_log}"
        # The production error formats argv with shlex.join.
        import shlex
        self.c.state['blocked'] = f"T001-branch: required check failed/missing/skipped: {shlex.join(argv)}; see {check_log}"
        self.c.save()
        return r

    def test_terminal_branch_check_failure_allows_bounded_authorization(self):
        r = self.terminal_check_failure_fixture()
        before_phase, before_blocker = r['phase'], self.c.state['blocked']
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
            entry = self.c.authorize_repairs('T001', 1, reason)
        self.assertEqual(entry['terminal_check_repair']['attempt'], 3)
        self.assertEqual(r['phase'], before_phase)
        self.assertEqual(self.c.state['blocked'], before_blocker)
        self.assertEqual(self.c.repair_limit(r), 4)
        persisted = Coordinator(self.repo, self.root/'state', self.config)
        self.assertEqual(persisted.state['records']['T001']['repair_authorizations'], [entry])

    def test_terminal_authorization_refusal_leaves_state_bytes_unchanged(self):
        self.terminal_check_failure_fixture()
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        before = self.c.file.read_bytes()
        with patch('orchestrate.os.kill', return_value=None):
            with self.assertRaisesRegex(Stop, 'Target runner or child'):
                self.c.authorize_repairs('T001', 1, reason)
        self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_tampered_receipt_blocker_and_artifact(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        for mutate in ('receipt', 'blocker', 'artifact', 'exit'):
            with self.subTest(mutate=mutate):
                r = self.terminal_check_failure_fixture()
                job = json.loads(Path(r['job']).read_text())
                if mutate == 'receipt':
                    Path(job['receipt']).write_text('{malformed')
                elif mutate == 'blocker':
                    self.c.state['blocked'] = 'Unrelated blocker'
                    self.c.save()
                elif mutate == 'exit':
                    check_path = next((self.c.directory/'checks'/'T001-branch').glob('*.json'))
                    check = json.loads(check_path.read_text())
                    check['exit'] = 1
                    atomic(check_path, check)
                else:
                    check_path = next((self.c.directory/'checks'/'T001-branch').glob('*.json'))
                    check = json.loads(check_path.read_text())
                    Path(check['log']).write_text('tampered after receipt\n')
                before = self.c.file.read_bytes()
                with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
                    with self.assertRaises(Stop):
                        self.c.authorize_repairs('T001', 1, reason)
                self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_permission_denied_pid_probe(self):
        self.terminal_check_failure_fixture()
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        before = self.c.file.read_bytes()
        with patch('orchestrate.os.kill', side_effect=PermissionError):
            with self.assertRaisesRegex(Stop, 'liveness is uncertain'):
                self.c.authorize_repairs('T001', 1, reason)
        self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_target_path_stale_job_and_receipt(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        for stale in ('stale_job', 'stale_receipt', 'missing_attempt', 'stale_attempt'):
            with self.subTest(stale=stale):
                r = self.terminal_check_failure_fixture()
                task_dir = self.c.directory / 'tasks' / 'T001'
                if stale == 'stale_job':
                    stale_job_path = task_dir / 'worker-2-job.json'
                    stale_job_path.write_text((task_dir / 'worker-3-job.json').read_text())
                    r['job'] = str(stale_job_path)
                    self.c.save()
                elif stale == 'stale_receipt':
                    job_path = Path(r['job'])
                    job = json.loads(job_path.read_text())
                    stale_receipt = task_dir / 'worker-2-exit.json'
                    stale_receipt.write_text(Path(job['receipt']).read_text())
                    job['receipt'] = str(stale_receipt)
                    atomic(job_path, job)
                elif stale == 'missing_attempt':
                    job = json.loads(Path(r['job']).read_text())
                    receipt = json.loads(Path(job['receipt']).read_text())
                    receipt.pop('attempt', None)
                    atomic(job['receipt'], receipt)
                    r['exit'] = receipt
                    self.c.save()
                elif stale == 'stale_attempt':
                    job = json.loads(Path(r['job']).read_text())
                    receipt = json.loads(Path(job['receipt']).read_text())
                    receipt['attempt'] = 2
                    atomic(job['receipt'], receipt)
                    r['exit'] = receipt
                    self.c.save()
                before = self.c.file.read_bytes()
                with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
                    with self.assertRaises(Stop):
                        self.c.authorize_repairs('T001', 1, reason)
                self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_identity_mismatches_and_boolean_exit(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        for mismatch in ('job_task', 'job_role', 'job_attempt', 'job_cwd', 'receipt_task', 'receipt_role',
                          'receipt_model', 'receipt_provider', 'exit_false', 'exit_float_zero', 'exit_nonzero',
                          'log_mismatch', 'result_mismatch', 'unmanifested_log_name', 'unmanifested_result_name'):
            with self.subTest(mismatch=mismatch):
                r = self.terminal_check_failure_fixture()
                job_path = Path(r['job'])
                job = json.loads(job_path.read_text())
                receipt_path = Path(job['receipt'])
                receipt = json.loads(receipt_path.read_text())
                task_dir = self.c.directory / 'tasks' / 'T001'

                if mismatch == 'job_task':
                    job['task'] = 'T002'
                elif mismatch == 'job_role':
                    job['role'] = 'reviewer'
                elif mismatch == 'job_attempt':
                    job['attempt'] = 2
                elif mismatch == 'job_cwd':
                    job['cwd'] = str(self.repo)
                elif mismatch == 'receipt_task':
                    receipt['task'] = 'T002'
                elif mismatch == 'receipt_role':
                    receipt['role'] = 'reviewer'
                elif mismatch == 'receipt_model':
                    receipt['model'] = 'other-model'
                elif mismatch == 'receipt_provider':
                    receipt['provider'] = 'other-provider'
                elif mismatch == 'exit_false':
                    receipt['exit'] = False
                elif mismatch == 'exit_float_zero':
                    receipt['exit'] = 0.0
                elif mismatch == 'exit_nonzero':
                    receipt['exit'] = 1
                elif mismatch == 'log_mismatch':
                    receipt['log'] = str(task_dir / 'other.jsonl')
                    (task_dir / 'other.jsonl').write_text('{}\n')
                elif mismatch == 'result_mismatch':
                    receipt['result'] = str(task_dir / 'other.txt')
                    (task_dir / 'other.txt').write_text('other')
                elif mismatch == 'unmanifested_log_name':
                    other_log = task_dir / 'worker-2.jsonl'
                    other_log.write_text('{}\n')
                    job['log'] = str(other_log)
                    receipt['log'] = str(other_log)
                elif mismatch == 'unmanifested_result_name':
                    other_result = task_dir / 'worker-2-result.txt'
                    other_result.write_text('res')
                    job['result'] = str(other_result)
                    receipt['result'] = str(other_result)

                atomic(job_path, job)
                atomic(receipt_path, receipt)
                r['exit'] = receipt
                self.c.save()

                before = self.c.file.read_bytes()
                with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
                    with self.assertRaises(Stop):
                        self.c.authorize_repairs('T001', 1, reason)
                self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_live_child_and_pid_metadata_defects(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        for defect in ('live_child', 'missing_pid_file', 'missing_child_file', 'symlink_pid_file',
                       'malformed_pid_file', 'missing_started', 'bool_started', 'string_started', 'nonfinite_started',
                       'negative_runner_started', 'stale_runner_started_one', 'pre_job_runner_started',
                       'job_mtime_after_runner', 'runner_started_after_receipt_start', 'stale_started_after_finish', 'child_before_runner',
                       'recorded_pid_mismatch'):
            with self.subTest(defect=defect):
                r = self.terminal_check_failure_fixture()
                job_path = Path(r['job'])
                receipt = json.loads(Path(json.loads(job_path.read_text())['receipt']).read_text())
                started, finished = receipt['started'], receipt['finished']
                pid_file = Path(str(job_path) + '.pid.json')
                child_file = Path(str(job_path) + '.child.json')

                kill_side_effects = [ProcessLookupError, ProcessLookupError, ProcessLookupError]
                if defect == 'live_child':
                    def kill_mock(pid, sig):
                        if pid == 99999997:  # child pid
                            return None  # alive
                        raise ProcessLookupError
                else:
                    kill_mock = ProcessLookupError

                if defect == 'missing_pid_file':
                    pid_file.unlink()
                elif defect == 'missing_child_file':
                    child_file.unlink()
                elif defect == 'symlink_pid_file':
                    target = pid_file.parent / 'real.pid.json'
                    target.write_text(pid_file.read_text())
                    pid_file.unlink()
                    pid_file.symlink_to(target)
                elif defect == 'malformed_pid_file':
                    pid_file.write_text('{invalid')
                elif defect == 'missing_started':
                    meta = json.loads(pid_file.read_text())
                    meta.pop('started', None)
                    atomic(pid_file, meta)
                elif defect == 'bool_started':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = True
                    atomic(pid_file, meta)
                elif defect == 'string_started':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = 'not-a-time'
                    atomic(pid_file, meta)
                elif defect == 'nonfinite_started':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = float('inf')
                    atomic(pid_file, meta)
                elif defect == 'negative_runner_started':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = -1.0
                    atomic(pid_file, meta)
                elif defect == 'stale_runner_started_one':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = 1.0
                    atomic(pid_file, meta)
                elif defect == 'pre_job_runner_started':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = Path(job_path).stat().st_mtime - 1
                    atomic(pid_file, meta)
                elif defect == 'job_mtime_after_runner':
                    os.utime(job_path, (started + 1, started + 1))
                elif defect == 'runner_started_after_receipt_start':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = started + 0.01
                    atomic(pid_file, meta)
                elif defect == 'stale_started_after_finish':
                    meta = json.loads(pid_file.read_text())
                    meta['started'] = finished + 100
                    atomic(pid_file, meta)
                elif defect == 'child_before_runner':
                    meta_child = json.loads(child_file.read_text())
                    meta_child['started'] = started - 1
                    atomic(child_file, meta_child)
                elif defect == 'recorded_pid_mismatch':
                    r['pid'] = 11111111
                    self.c.save()

                before = self.c.file.read_bytes()
                with patch('orchestrate.os.kill', side_effect=kill_mock if defect == 'live_child' else ProcessLookupError):
                    with self.assertRaises(Stop):
                        self.c.authorize_repairs('T001', 1, reason)
                self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_check_command_cwd_head_snapshot_and_symlinks(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        for defect in ('command_mutation', 'cwd_mismatch', 'head_mismatch', 'snapshot_mutation',
                       'symlink_checks_dir', 'symlink_check_file', 'symlink_artifact',
                       'ambiguous_multiple_checks', 'missing_manifest', 'malformed_manifest',
                       'missing_artifact_file'):
            with self.subTest(defect=defect):
                r = self.terminal_check_failure_fixture()
                check_dir = self.c.directory / 'checks' / 'T001-branch'
                check_path = next(check_dir.glob('*.json'))
                check = json.loads(check_path.read_text())

                if defect == 'command_mutation':
                    check['command'] = check['command'] + ['--extra-flag']
                    atomic(check_path, check)
                elif defect == 'cwd_mismatch':
                    check['cwd'] = str(self.repo)
                    atomic(check_path, check)
                elif defect == 'head_mismatch':
                    check['source_head'] = '0' * 40
                    atomic(check_path, check)
                elif defect == 'snapshot_mutation':
                    w = Path(r['worktree'])
                    (w / 'untracked_mutation.txt').write_text('mutated')
                elif defect == 'symlink_checks_dir':
                    real_checks = check_dir.parent / 'real-checks'
                    shutil.move(check_dir, real_checks)
                    check_dir.symlink_to(real_checks)
                elif defect == 'symlink_check_file':
                    real_file = check_dir / 'real.receipt'
                    real_file.write_text(check_path.read_text())
                    check_path.unlink()
                    check_path.symlink_to(real_file)
                elif defect == 'symlink_artifact':
                    log_file = Path(check['log'])
                    real_log = check_dir / 'real.log'
                    real_log.write_text(log_file.read_text())
                    log_file.unlink()
                    log_file.symlink_to(real_log)
                elif defect == 'ambiguous_multiple_checks':
                    dup = check_dir / 'another-match.json'
                    atomic(dup, check)
                elif defect == 'missing_manifest':
                    check.pop('artifacts', None)
                    atomic(check_path, check)
                elif defect == 'malformed_manifest':
                    check['artifacts'] = ['not-a-dict']
                    atomic(check_path, check)
                elif defect == 'missing_artifact_file':
                    Path(check['log']).unlink()

                before = self.c.file.read_bytes()
                with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
                    with self.assertRaises(Stop):
                        self.c.authorize_repairs('T001', 1, reason)
                self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_rejects_dirty_changed_integration_and_all_excluded_phases(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        # Dirty integration or changed integration HEAD
        r = self.terminal_check_failure_fixture()
        (self.c.integration / 'dirty.txt').write_text('dirty')
        before = self.c.file.read_bytes()
        with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
            with self.assertRaisesRegex(Stop, 'Dirty worktree: .*integration'):
                self.c.authorize_repairs('T001', 1, reason)
        self.assertEqual(before, self.c.file.read_bytes())
        (self.c.integration / 'dirty.txt').unlink()

        # Integration HEAD changed
        self.c.state['expected_head'] = '0' * 40
        self.c.save()
        before = self.c.file.read_bytes()
        with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
            with self.assertRaisesRegex(Stop, 'Integration HEAD changed'):
                self.c.authorize_repairs('T001', 1, reason)
        self.assertEqual(before, self.c.file.read_bytes())

        # Excluded phases
        excluded_phases = ['planned', 'worker_running', 'committed', 'review_running',
                           'review_finished', 'approved', 'merge_pending', 'integration_testing',
                           'status_pending', 'merged', 'provider_blocked', 'fallback_pending', 'repair_pending']
        for phase in excluded_phases:
            with self.subTest(phase=phase):
                r = self.terminal_check_failure_fixture()
                r['phase'] = phase
                if phase in ('committed', 'approved', 'merged'):
                    r['commit'] = '1' * 40
                self.c.save()
                before = self.c.file.read_bytes()
                with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
                    with self.assertRaises(Stop):
                        self.c.authorize_repairs('T001', 1, reason)
                self.assertEqual(before, self.c.file.read_bytes())

    def test_terminal_authorization_preserves_unrelated_running_t005_and_immutable_evidence(self):
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        r = self.terminal_check_failure_fixture()
        # Add unrelated running T005
        t005_dir = self.c.directory / 'tasks' / 'T005'
        t005_dir.mkdir(parents=True, exist_ok=True)
        t005_job = t005_dir / 'worker-3-job.json'
        atomic(t005_job, dict(task='T005', role='worker', attempt=3, cwd=str(self.root / 'T005-worktree')))
        atomic(str(t005_job) + '.pid.json', dict(pid=78381, started=time.time() - 50))
        atomic(str(t005_job) + '.child.json', dict(pid=78382, started=time.time() - 40))
        self.c.state['records']['T005'] = dict(
            phase='worker_running', attempt=3, pid=78381, job=str(t005_job),
            branch='migration/T005', base='b' * 40, commit=None, worktree=str(self.root / 'T005-worktree')
        )
        self.c.save()

        # Capture pre-hashes for T005 files and T001 evidence files
        t005_files = {p: p.read_bytes() for p in t005_dir.glob('*') if p.is_file()}
        t001_evidence = {p: p.read_bytes() for p in (self.c.directory / 'tasks' / 'T001').glob('*') if p.is_file()}
        check_evidence = {p: p.read_bytes() for p in (self.c.directory / 'checks' / 'T001-branch').rglob('*') if p.is_file()}

        # Mock kill such that T005 pid 78381 is reported alive, but T001 pid 99999998 is dead
        def kill_mock(pid, sig):
            if pid in (78381, 78382):
                return None  # T005 alive!
            raise ProcessLookupError  # T001 dead

        with patch('orchestrate.os.kill', side_effect=kill_mock):
            entry = self.c.authorize_repairs('T001', 1, reason)

        # Assert T005 was untouched
        self.assertEqual(self.c.state['records']['T005']['phase'], 'worker_running')
        self.assertEqual(self.c.state['records']['T005']['pid'], 78381)
        for p, content in t005_files.items():
            self.assertEqual(p.read_bytes(), content)

        # Assert all T001 task and check evidence files are bitwise immutable
        for p, content in t001_evidence.items():
            self.assertEqual(p.read_bytes(), content)
        for p, content in check_evidence.items():
            self.assertEqual(p.read_bytes(), content)

    def test_ordinary_repair_resume_fresh_checks_and_independent_review_lifecycle(self):
        r = self.terminal_check_failure_fixture()
        reason = 'Failed mandatory branch check is traced to a concrete local prerequisite mismatch.'
        with patch('orchestrate.os.kill', side_effect=ProcessLookupError):
            entry = self.c.authorize_repairs('T001', 1, reason)

        # Phase remains worker_finished and blocker remains set after authorization
        self.assertEqual(r['phase'], 'worker_finished')
        self.assertTrue(bool(self.c.state['blocked']))

        # Direct resume is refused because state is still blocked
        with self.assertRaisesRegex(Stop, 'Run blocked'):
            self.c.execute(self.tasks)

        # Use the ordinary repair command path to clear the blocker.
        findings = 'Implement the missing AC2 transaction rollback and fix the local prerequisite.'
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(self.config))
        providers_path = self.root / 'providers.json'
        providers_path.write_text(json.dumps({
            'primary': {'backend': 'codex', 'model': 'gpt-6-luna', 'effort': 'medium'},
            'fallback': {'backend': 'codex', 'model': 'gpt-6-luna', 'effort': 'medium'},
            'reviewer': {'backend': 'codex', 'model': 'gpt-6-sol', 'effort': 'medium'},
            'mixed_parallel': False, 'preflight_seconds': 1,
        }))
        common = ['--repo', str(self.repo), '--state-dir', str(self.c.directory),
                  '--config', str(config_path), '--providers-config', str(providers_path),
                  '--approved-execution']
        with patch.object(sys, 'argv', ['orchestrate.py', *common, 'repair', '--task', 'T001', '--findings', findings]):
            orchestrate.main()
        repaired = json.loads(self.c.file.read_text())
        self.assertEqual(repaired['records']['T001']['phase'], 'repair_pending')
        self.assertIsNone(repaired['blocked'])

        # The ordinary resume performs attempt four, fresh mandatory checks,
        # a fresh independent review, and the normal integration path.
        with patch.object(sys, 'argv', ['orchestrate.py', *common, 'resume']):
            orchestrate.main()
        state = json.loads(self.c.file.read_text())
        record = state['records']['T001']
        self.assertEqual(record['attempt'], 4)
        self.assertEqual(record['phase'], 'merged')
        self.assertEqual(record['review_attempt'], 1)
        self.assertTrue(record['evidence'])
        self.assertTrue(record['review']['approved'])

    def test_repair_authorization_cumulative_ceiling_remains_six(self):
        self.c.initialize(self.tasks)
        r = self.c.prepare(self.tasks[0])
        r.update(phase='rejected', attempt=3)
        self.c.save()
        for index in range(3):
            self.c.authorize_repairs('T001', 2, f'New documented evidence and repair hypothesis number {index}.')
            r['attempt'] = self.c.repair_limit(r)
            self.c.save()
        before = self.c.file.read_bytes()
        with self.assertRaisesRegex(Stop, 'Cumulative additional repair budget exceeded'):
            self.c.authorize_repairs('T001', 1, 'Another concrete hypothesis after all six extensions are used.')
        self.assertEqual(before, self.c.file.read_bytes())

    def test_coordinator_interruption_does_not_duplicate_workers(self):
        atomic(self.root/'config.json',self.config)
        harness=self.root/'harness.py'
        harness.write_text(f'import sys;sys.path.insert(0,{str(Path(__file__).resolve().parent)!r});from orchestrate import *;import json\nc=Coordinator(Path({str(self.repo)!r}),Path({str(self.root/"state")!r}),json.loads(Path({str(self.root/"config.json")!r}).read_text()),max_workers=2);c.execute(read_graph(c.repo))\n')
        with patch.dict(os.environ,FAKE_DELAY='2'):
            p=subprocess.Popen([sys.executable,str(harness)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            deadline=time.time()+15
            while time.time()<deadline:
                if len(list((self.root/'state/tasks').glob('*/worker-1-job.json.pid.json')))==2:
                    break
                time.sleep(.1)
            else:
                p.kill();p.wait();self.fail('Workers never started')
            p.terminate();p.wait()
        resumed=Coordinator(self.repo,self.root/'state',self.config)
        resumed.execute(self.tasks)
        self.assertTrue(all(r['attempt']==1 for r in resumed.state['records'].values()))
        self.assertEqual(len(list((self.root/'state/tasks').glob('*/worker-*-exit.json'))),2)

    def test_wave_checkpoint_failure_can_be_resumed(self):
        original=self.c.checks
        def interrupted(cwd,commands,label):
            if label=='wave-1':
                raise Stop('checkpoint failure')
            return original(cwd,commands,label)
        with patch.object(self.c,'checks',side_effect=interrupted):
            with self.assertRaisesRegex(Stop,'checkpoint failure'):
                self.c.execute(self.tasks)
        self.assertTrue(all(r['phase']=='merged' for r in self.c.state['records'].values()))
        self.assertNotIn('1',self.c.state['checkpoints'])
        self.c.execute(self.tasks)
        self.assertIn('1',self.c.state['checkpoints'])
        self.assertTrue(all(r['attempt']==1 for r in self.c.state['records'].values()))

    def test_status_commit_interruption_can_be_resumed(self):
        original=self.c.status_update
        def interrupted(tid):
            original(tid)
            raise Stop('interrupted after status commit')
        with patch.object(self.c,'status_update',side_effect=interrupted):
            with self.assertRaisesRegex(Stop,'interrupted after status'):
                self.c.execute(self.tasks)
        resumed=Coordinator(self.repo,self.root/'state',self.config)
        resumed.execute(self.tasks)
        self.assertTrue(all(r['phase']=='merged' for r in resumed.state['records'].values()))
        subjects=git(resumed.integration,'log','--format=%s').splitlines()
        self.assertEqual(subjects.count('migration: record verified T001'),1)

    def test_review_requires_every_criterion_and_actual_pass(self):
        self.c.initialize(self.tasks)
        t=self.tasks[0]
        r=self.c.prepare(t)
        r['attempt']=1
        w=Path(r['worktree'])
        (w/'docs/smoke-alpha.md').write_text('# Smoke alpha\n\nThis is the alpha orchestration smoke document.\n')
        self.c.verify_worker(t)
        result=self.root/'review.json'
        atomic(result,dict(approved=True,findings=[],criteria=[dict(id='AC1',passed=True,evidence='claimed')]))
        with self.assertRaisesRegex(Stop,'every acceptance'):
            self.c.accept_review(t,{'result':str(result)})
        atomic(result,dict(approved=True,findings=[],criteria=[dict(id='AC1',passed=True,evidence='checked'),dict(id='AC2',passed=False,evidence='incomplete')]))
        self.c.accept_review(t,{'result':str(result)})
        self.assertEqual(r['phase'],'rejected')

    def test_reviewer_retries_preserve_receipts(self):
        self.c.initialize(self.tasks)
        t=self.tasks[0]
        r=self.c.prepare(t)
        r['attempt']=1
        w=Path(r['worktree'])
        (w/'docs/smoke-alpha.md').write_text('# Smoke alpha\n\nThis is the alpha orchestration smoke document.\n')
        self.c.artifact('T001','worker-1-result.txt').write_text('Untrusted worker PASS')
        self.c.verify_worker(t)
        self.c.launch_review(t)
        outcome,job=self.c.wait('T001')
        self.c.accept_review(t,job)
        self.c.launch_review(t)
        outcome,job=self.c.wait('T001')
        self.c.accept_review(t,job)
        self.assertEqual(r['review_attempt'],2)
        self.assertEqual(r['attempt'],1)
        self.assertTrue(self.c.artifact('T001','review-1-exit.json').exists())
        self.assertTrue(self.c.artifact('T001','review-2-exit.json').exists())

    def test_check_timeout_stops_child_process_group(self):
        self.config['timeout_seconds']=0.2
        marker=self.root/'unexpected-child-output'
        script='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr('import time;from pathlib import Path;time.sleep(1);Path('+repr(str(marker))+').write_text("leaked")')+']);time.sleep(5)'
        with self.assertRaisesRegex(Stop,'required check failed'):
            self.c.checks(self.repo,[[sys.executable,'-c',script]],'timeout')
        time.sleep(1.1)
        self.assertFalse(marker.exists())

    def test_default_git_state_keeps_worktrees_outside_git(self):
        c=Coordinator(self.repo,self.repo/'.git/migration-orchestrator',self.config)
        c.initialize(self.tasks)
        r=c.prepare(self.tasks[0])
        self.assertFalse(c.integration.is_relative_to(self.repo/'.git'))
        self.assertFalse(Path(r['worktree']).is_relative_to(self.repo/'.git'))
        self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_existing_external_task_branch_blocks_duplicates(self):
        git(self.repo,'branch','agent/T001')
        with self.assertRaisesRegex(Stop,'Existing task branches'):
            self.c.initialize(self.tasks)
        self.assertFalse(self.c.file.exists())

    def test_dependency_lock_ownership_and_generated_modules_ignore(self):
        repo=Path(__file__).resolve().parents[2]
        cfg=config_template(repo)
        from orchestrate import owns
        self.assertTrue(owns('uv.lock',cfg['scopes']['T001']))
        self.assertFalse(owns('uv.lock',cfg['scopes']['T002']))
        result=subprocess.run(['git','check-ignore','--no-index','runtime/artifact_upload_host/node_modules/example.js'],cwd=repo,text=True,capture_output=True)
        self.assertEqual(result.returncode,0)
        for name in ('runtime/artifact_upload_host/canary/node_modules/example.js', 'sandbox/artifact_upload/worker/native/node_modules/example.js'):
            result=subprocess.run(['git','check-ignore','--no-index',name],cwd=repo,text=True,capture_output=True)
            self.assertEqual(result.returncode,0)
        self.assertFalse(owns('runtime/artifact_upload_host/node_modules/example.js',cfg['scopes']['T001']))
        self.assertIn('uv.lock',cfg['exclusions']['T008'])

    def test_real_plan_parses_all_tasks_and_scopes(self):
        repo=Path(__file__).resolve().parents[2]
        cfg=config_template(repo)
        graph=read_graph(repo)
        self.assertEqual(len(graph),18)
        self.assertEqual([t['id'] for t in select(graph,{},cfg['scopes'])],['T001'])
        for scopes in cfg['scopes'].values():
            self.assertTrue(all(not p.endswith('.') for p in scopes))
        for wave in range(1,11):
            group=[t for t in graph if t['wave']==wave]
            self.assertLessEqual(len(group),2)
            if len(group)==2:
                self.assertFalse(overlap(cfg['scopes'][group[0]['id']],cfg['scopes'][group[1]['id']]))

class UsageTests(unittest.TestCase):
    def test_multiple_turns_missing_usage_and_partial_logs(self):
        with tempfile.TemporaryDirectory(prefix='migration-usage-tests-') as temp:
            root = Path(temp)
            log = root / 'tasks/T003/worker-1.jsonl'
            log.parent.mkdir(parents=True)
            events = [dict(type='thread.started', thread_id='independent-session'),
                      dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=80, output_tokens=10, reasoning_output_tokens=3)),
                      dict(type='turn.completed', usage=dict(input_tokens=50, cached_input_tokens=20, output_tokens=5)),
                      dict(type='item.completed', item=dict(type='command_execution', aggregated_output='abc'))]
            log.write_text('\n'.join(json.dumps(e) for e in events) + '\n{partial\n[]\n')
            result = log_usage(log)
            self.assertEqual(result['tokens']['input_tokens'], 150)
            self.assertEqual(result['tokens']['cached_input_tokens'], 100)
            self.assertEqual(result['uncached_input_tokens'], 50)
            self.assertEqual(result['tokens']['output_tokens'], 15)
            self.assertEqual(result['usage_records'], 2)
            self.assertEqual(result['malformed_lines'], 2)
            self.assertEqual(result['command_output_bytes'], 3)
            missing = log.with_name('worker-2.jsonl')
            missing.write_text('{"type":"turn.completed"}\n')
            before = {p: p.read_bytes() for p in (log, missing)}
            report = usage_report(root)
            self.assertEqual(report['groups'][0]['calls'], 2)
            self.assertEqual(report['groups'][0]['calls_with_usage'], 1)
            self.assertIsNone(report['agents'][1]['tokens'])
            self.assertEqual(before, {p: p.read_bytes() for p in before})


class BaselineExceptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='migration-baseline-tests-')
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / 'source.py').write_text('original source\n')
        self.report = self.root / 'report.xml'
        self.suite = ET.Element('testsuite')
        ET.SubElement(self.suite, 'testcase', classname='tests.example', name='test_ok')
        self.case = ET.SubElement(self.suite, 'testcase', classname='tests.example', name='test_known')
        self.failure = ET.SubElement(self.case, 'failure', message='AssertionError: known')
        self.failure.text = 'exact known traceback'
        self.exception = dict(classname='tests.example', name='test_known',
                              failure_sha256=digest(json.dumps(dict(message=self.failure.get('message'), text=self.failure.text), sort_keys=True)),
                              source_sha256={'source.py': digest('original source\n')})

    def audit(self, exit_code=1, exceptions=None):
        ET.ElementTree(self.suite).write(self.report)
        return pytest_evidence(self.report, self.root, exit_code,
                               [self.exception] if exceptions is None else exceptions)

    def test_exact_exception_recorded_without_claiming_all_tests_pass(self):
        result = self.audit()
        self.assertEqual(result['tests_passed'], 1)
        self.assertEqual(result['tests_failed'], 1)
        self.assertEqual(result['accepted_baseline_exceptions'], ['tests.example::test_known'])

    def test_changed_signature_or_hidden_source_change_blocks(self):
        for field in ('message', 'text', 'source'):
            with self.subTest(field=field):
                if field == 'message':
                    self.failure.set('message', 'different error')
                elif field == 'text':
                    self.failure.text = 'different traceback'
                else:
                    (self.root / 'source.py').write_text('hidden new leak\n')
                with self.assertRaisesRegex(Stop, 'changed'):
                    self.audit()
                self.failure.set('message', 'AssertionError: known')
                self.failure.text = 'exact known traceback'

    def test_additional_failure_and_duplicate_known_failure_block(self):
        for name in ('test_new', 'test_known'):
            with self.subTest(name=name):
                case = ET.SubElement(self.suite, 'testcase', classname='tests.example', name=name)
                ET.SubElement(case, 'failure', message='AssertionError: known').text = 'exact known traceback'
                with self.assertRaisesRegex(Stop, 'Unapproved or duplicate'):
                    self.audit()
                self.suite.remove(case)

    def test_skips_collection_errors_and_abnormal_exits_block(self):
        for tag in ('skipped', 'error'):
            element = ET.SubElement(self.case, tag)
            with self.assertRaisesRegex(Stop, 'collection errors or skipped'):
                self.audit()
            self.case.remove(element)
        for code in (0, 2, 3, 4, 5, -15):
            with self.subTest(code=code), self.assertRaisesRegex(Stop, 'exit status'):
                self.audit(code)

    def test_exception_not_required_when_test_passes(self):
        self.case.remove(self.failure)
        self.assertEqual(self.audit(0)['accepted_baseline_exceptions'], [])

    def test_missing_empty_or_malformed_report_blocks(self):
        with self.assertRaises(FileNotFoundError):
            pytest_evidence(self.report, self.root, 1, [self.exception])
        self.report.write_text('<broken')
        with self.assertRaises(ET.ParseError):
            pytest_evidence(self.report, self.root, 1, [self.exception])
        self.suite = ET.Element('testsuite')
        with self.assertRaisesRegex(Stop, 'Missing tests'):
            self.audit()

    def test_unconfigured_exception_blocks(self):
        with self.assertRaisesRegex(Stop, 'Unapproved'):
            self.audit(exceptions=[])


if __name__=='__main__':
    unittest.main()
