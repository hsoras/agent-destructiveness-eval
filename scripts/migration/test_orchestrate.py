"""Coordinator safety tests use real temporary Git repositories and a simulated CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from orchestrate import Coordinator, Stop, atomic, config_template, digest, git, lock, overlap, pytest_evidence, read_graph, select
from smoke import fixture

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
        self.c = Coordinator(self.repo, self.root / 'state', self.config)
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

    def test_coordinator_interruption_does_not_duplicate_workers(self):
        atomic(self.root/'config.json',self.config)
        harness=self.root/'harness.py'
        harness.write_text(f'import sys;sys.path.insert(0,{str(Path(__file__).resolve().parent)!r});from orchestrate import *;import json\nc=Coordinator(Path({str(self.repo)!r}),Path({str(self.root/"state")!r}),json.loads(Path({str(self.root/"config.json")!r}).read_text()));c.execute(read_graph(c.repo))\n')
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
