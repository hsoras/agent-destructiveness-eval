"""Provider safety cases run only fake CLIs in disposable Git worktrees."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import test_orchestrate as legacy
from orchestrate import Coordinator, Stop, atomic, git, watch_state
from providers import agy_outcome, snapshot, permission_preflight
from usage import log_usage, usage_report

AGY = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args=sys.argv[1:]
mode=os.environ.get('AGY_FAILURE','')
if args == ['models']:
 if mode == 'init':
  print('failed to initialize local listener',file=sys.stderr); sys.exit(1)
 if mode == 'model':
  print('gemini-other'); sys.exit(0)
 print('gemini-3.8-flash-medium'); sys.exit(0)
assert '--sandbox' in args and '--dangerously-skip-permissions' not in args
assert args[args.index('--model')+1] == 'gemini-3.8-flash-medium'
assert '-p' in args
cwd=pathlib.Path.cwd()
time.sleep(float(os.environ.get('FAKE_DELAY','0.05')))
word='alpha' if cwd.name=='T001' else 'beta'
if mode in ('partial','denied_partial') or not mode:
 (cwd/f'docs/smoke-{word}.md').write_text(f'# Smoke {word}\\n\\nThis is the {word} orchestration smoke document.\\n')
result=dict(status='SUCCESS', response='Documentation implementation complete.',usage=dict(input_tokens=100,cache_read_tokens=25,output_tokens=8))
if mode in ('quota','partial'):
 result.update(status='ERROR',error='RESOURCE_EXHAUSTED: quota exhausted')
if mode == 'provider':
 result.update(status='ERROR',error='503 service unavailable')
if mode == 'auth':
 result.update(status='ERROR',error='authentication required')
if mode in ('denied','denied_partial'):
 result['denied_actions']=[dict(action='command',display_name='RunCommand')]
if mode == 'bad_code':
 (cwd/f'docs/smoke-{word}.md').write_text('incorrect implementation')
if mode in ('tool','tool_quota'):
 print(json.dumps(dict(event='step_update',step_update=dict(tool_info=dict(error=dict(type='TOOL_ERROR',message='application quota exhausted' if mode == 'tool_quota' else 'AssertionError acceptance failed'))))))
print(json.dumps(dict(event='result',result=result)))
sys.exit(1 if mode in ('quota','partial','auth') else 0)
'''

class ProviderTests(unittest.TestCase):
    tearDown = legacy.SafetyTests.tearDown
    def setUp(self):
        legacy.SafetyTests.setUp(self)
        self.agy = self.root / 'bin/agy'
        self.agy.write_text(AGY)
        self.agy.chmod(0o755)
        self.routing = dict(primary=dict(backend='agy', model='gemini-3.8-flash-medium', effort='medium'),
                            fallback=dict(backend='codex',model='gpt-6-luna',effort='medium'),
                            reviewer=dict(backend='codex',model='gpt-6-sol',effort='medium'),
                            mixed_parallel=True, preflight_seconds=2)

    def routed(self, workers=1):
        return Coordinator(self.repo, self.root/'state', self.config, max_workers=workers, routing=self.routing)

    def test_gemini_success_and_mixed_isolation(self):
        c=self.routed(2)
        with patch.dict(os.environ, FAKE_DELAY='.3'):
            c.execute(self.tasks)
        self.assertEqual([r['worker_provider'] for r in c.state['records'].values()], ['agy','codex'])
        receipts=[json.loads(c.artifact(t['id'],'worker-1-exit.json').read_text()) for t in self.tasks]
        self.assertLess(max(x['started'] for x in receipts), min(x['finished'] for x in receipts))
        self.assertTrue(all(r['review']['approved'] and r['commit'] for r in c.state['records'].values()))
        jobs=[json.loads(c.artifact(t['id'],'worker-1-job.json').read_text()) for t in self.tasks]
        self.assertNotEqual(jobs[0]['cwd'],jobs[1]['cwd'])
        children=[json.loads(Path(str(c.artifact(t['id'],'worker-1-job.json'))+'.child.json').read_text())['pid'] for t in self.tasks]
        self.assertNotEqual(*children)
        before=c.file.read_bytes(); self.routed(2).execute(self.tasks); self.assertEqual(before,c.file.read_bytes())

    def assert_fallback(self, mode=None):
        c=self.routed()
        with patch.dict(os.environ, AGY_FAILURE=mode or ''):
            c.execute(self.tasks)
        r=c.state['records']['T001']
        self.assertEqual(r['worker_provider'],'codex')
        self.assertEqual(r['attempt'],1)
        self.assertTrue(r['review']['approved'])
        primary=json.loads(c.artifact('T001','worker-1-exit.json').read_text())
        fallback=json.loads(c.artifact('T001','worker-1-fallback-exit.json').read_text())
        self.assertLessEqual(primary['finished'],fallback['started'])
        self.assertEqual(json.loads(c.artifact('T001','worker-1-partial.json').read_text())['before'],
                         json.loads(c.artifact('T001','worker-1-partial.json').read_text())['after'])
        return c

    def test_missing_gemini_luna_fallback(self):
        self.agy.unlink()
        with patch('providers.shutil.which', return_value=None):
            # Runner is a separate process: force a private PATH with only fake Codex.
            with patch.dict(os.environ, PATH=str(self.root/'bin')+':/usr/bin:/bin'):
                c=self.assert_fallback()
        self.assertEqual(c.state['records']['T001']['provider_failure']['reason'],'executable_missing')

    def test_quota_luna_fallback(self):
        self.assert_fallback('quota')

    def test_initialization_fallback(self):
        self.assert_fallback('init')

    def test_provider_error_fallback(self):
        self.assert_fallback('provider')

    def test_auth_luna_fallback(self):
        self.assert_fallback('auth')

    def test_model_unavailable_fallback(self):
        self.assert_fallback('model')

    def test_permission_denied_exit_zero_fallback(self):
        self.assert_fallback('denied')

    def test_partial_edits_require_plan_and_preserve_provenance(self):
        c=self.routed()
        with patch.dict(os.environ, AGY_FAILURE='partial'):
            with self.assertRaisesRegex(Stop,'partial changes'):
                c.execute(self.tasks)
        r=c.state['records']['T001']; before=snapshot(r['worktree'])
        self.assertEqual(r['phase'],'provider_blocked')
        self.assertFalse(c.artifact('T001','worker-1-fallback-job.json').exists())
        self.assertTrue(Path(r['provider_failure']['provenance']).exists())
        with self.assertRaises(Stop): c.continue_fallback('T001','too short')
        plan='Retain the complete Gemini document. Verify its exact content and scope, then finish only the same assigned task with all original review and integration gates.'
        c.continue_fallback('T001',plan)
        self.assertEqual(before,snapshot(r['worktree']))
        # Crash after plan: recovery continues Luna, never re-launches Gemini.
        c=self.routed(); c.execute(self.tasks)
        self.assertEqual(c.state['records']['T001']['attempt'],1)
        self.assertEqual(c.state['records']['T001']['worker_provider'],'codex')

    def test_partial_denial_is_blocked_even_with_success_exit(self):
        c=self.routed()
        with patch.dict(os.environ, AGY_FAILURE='denied_partial'):
            with self.assertRaisesRegex(Stop,'partial changes'): c.execute(self.tasks)
        self.assertEqual(c.state['records']['T001']['phase'],'provider_blocked')

    def test_changed_partial_snapshot_blocks_handover(self):
        c=self.routed()
        with patch.dict(os.environ, AGY_FAILURE='partial'):
            with self.assertRaises(Stop): c.execute(self.tasks)
        Path(c.state['records']['T001']['worktree'],'docs/smoke-alpha.md').write_text('changed')
        with self.assertRaisesRegex(Stop,'changed since failure'):
            c.continue_fallback('T001','Preserve all partial files and finish the exact remaining documentation task.')

    def test_both_missing_stop_cleanly(self):
        self.agy.unlink(); (self.root/'bin/codex').unlink()
        c=self.routed()
        with patch.dict(os.environ, PATH=str(self.root/'bin')+':/usr/bin:/bin'):
            with self.assertRaisesRegex(Stop,'Safe work exhausted'): c.execute(self.tasks)
        r=c.state['records']['T001']
        self.assertEqual(r['phase'],'rejected'); self.assertIsNone(r['commit'])
        self.assertTrue(c.artifact('T001','worker-1-fallback-exit.json').exists())

    def test_coding_failure_never_falls_back(self):
        for mode in ('bad_code','tool'):
            with self.subTest(mode=mode):
                c=self.routed()
                with patch.dict(os.environ, AGY_FAILURE=mode):
                    with self.assertRaises(Stop): c.execute(self.tasks)
                self.assertFalse(c.artifact('T001','worker-1-fallback-job.json').exists())
                # First subcase stops at verification: retain files, do not reset for another run.
                break

    def test_tool_quota_text_never_becomes_provider_outage(self):
        c=self.routed()
        with patch.dict(os.environ, AGY_FAILURE='tool_quota'):
            with self.assertRaises(Stop): c.execute(self.tasks)
        self.assertEqual(c.state['records']['T001']['phase'],'rejected')
        self.assertFalse(c.artifact('T001','worker-1-fallback-job.json').exists())

    def test_tool_acceptance_failure_never_falls_back(self):
        c=self.routed()
        with patch.dict(os.environ, AGY_FAILURE='tool'):
            with self.assertRaises(Stop): c.execute(self.tasks)
        self.assertEqual(c.state['records']['T001']['phase'],'rejected')
        self.assertFalse(c.artifact('T001','worker-1-fallback-job.json').exists())

    def test_permissions_preflight_is_conservative(self):
        path=self.root/'permissions.json'
        atomic(path, dict(permissions=dict(allow=['command(git diff)'])))
        self.assertTrue(permission_preflight([['git','diff','--check']],path)['ready'])
        self.assertFalse(permission_preflight([['python3','-m','pytest']],path)['ready'])
        atomic(path, dict(permissions=dict(allow=['command(git diff)'],ask=['command(*)'])))
        self.assertFalse(permission_preflight([['git','diff','--check']],path)['ready'])
        atomic(path, dict(permissions=dict(allow=['command(*)'])))
        self.assertFalse(permission_preflight([['git','diff','--check']],path)['ready'])

    def test_gemini_coordinator_interruption_preserves_processes(self):
        atomic(self.root/'config.json',self.config); atomic(self.root/'routing.json',self.routing)
        harness=self.root/'harness.py'
        harness.write_text(f'import sys,json;sys.path.insert(0,{str(Path(__file__).resolve().parent)!r});from orchestrate import *\nc=Coordinator(Path({str(self.repo)!r}),Path({str(self.root/"state")!r}),json.loads(Path({str(self.root/"config.json")!r}).read_text()),max_workers=2,routing=json.loads(Path({str(self.root/"routing.json")!r}).read_text()));c.execute(read_graph(c.repo))\n')
        with patch.dict(os.environ,FAKE_DELAY='1.5'):
            p=subprocess.Popen([sys.executable,str(harness)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    if len(list((self.root/'state/tasks').glob('*/worker-1-job.json.child.json')))==2:
                        break
                    time.sleep(.05)
                else: self.fail('Mixed workers never started')
            finally:
                p.terminate(); p.wait()
        c=self.routed(2); c.execute(self.tasks)
        self.assertTrue(all(r['attempt']==1 and r['phase']=='merged' for r in c.state['records'].values()))
        self.assertEqual(len(list((self.root/'state/tasks').glob('*/worker-1-job.json'))),2)

    def test_duplicate_and_third_worker_launch_rejected(self):
        c=self.routed(1); c.initialize(self.tasks)
        c.prepare(self.tasks[0]); c.prepare(self.tasks[1])
        c.launch(self.tasks[0],'worker',c.prompt(self.tasks[0]))
        with self.assertRaisesRegex(Stop,'already running'): c.launch(self.tasks[0],'worker','duplicate')
        with self.assertRaisesRegex(Stop,'capacity'): c.launch(self.tasks[1],'worker','third worker')
        c.wait('T001')

    def test_luna_fallback_repair_retains_provider(self):
        path=self.root/'bin/codex'
        path.write_text(path.read_text().replace('approved=True','approved=False'))
        c=self.routed()
        with patch.dict(os.environ,AGY_FAILURE='quota'):
            with self.assertRaises(Stop): c.execute(self.tasks)
        r=c.state['records']['T001']
        self.assertEqual(r['phase'],'rejected'); self.assertEqual(r['worker_route']['backend'],'codex')
        r['phase']='repair_pending'; r['repair']='Complete the exact rejected acceptance criterion using preserved files and concrete review evidence.'
        c.state['blocked']=None; c.save()
        with self.assertRaises(Stop): self.routed().execute(self.tasks)
        state=json.loads(c.file.read_text())
        self.assertEqual(state['records']['T001']['worker_provider'],'codex')
        self.assertEqual(state['records']['T001']['attempt'],2)
        self.assertEqual(json.loads(c.artifact('T001','worker-2-job.json').read_text())['provider'],'codex')

    def test_failed_reviewer_receipt_after_interruption_never_approved(self):
        c=self.routed()
        c.initialize(self.tasks); task=self.tasks[0]; r=c.prepare(task)
        r['attempt']=1
        Path(r['worktree'],'docs/smoke-alpha.md').write_text('# Smoke alpha\n\nThis is the alpha orchestration smoke document.\n')
        c.artifact('T001','worker-1-result.txt').write_text('Untrusted worker claim')
        c.verify_worker(task); c.launch_review(task); outcome,job=c.wait('T001')
        # Reproduce crash after wait saves review_finished, before failed exit is inspected.
        receipt=json.loads(Path(job['receipt']).read_text()); receipt['exit']=1; atomic(job['receipt'],receipt)
        r['exit']=receipt; c.save()
        with self.assertRaisesRegex(Stop,'persisted reviewer process failed'): self.routed().execute(self.tasks)
        self.assertIsNone(json.loads(c.file.read_text())['records']['T001']['review'])

    def test_required_artifact_symlink_parent_rejected(self):
        outside=self.root/'external'; outside.mkdir(); (outside/'control.json').write_text('outside artifact')
        self.config['required_artifacts']={'T001':['link/control.json']}
        command=[sys.executable,'-c',f'import os;from pathlib import Path;Path(os.environ["MIGRATION_ARTIFACT_DIR"],"link").symlink_to({str(outside)!r},target_is_directory=True)']
        with self.assertRaises(Stop): self.c.checks(self.repo,[command],'T001-branch')

    def test_numeric_application_error_not_authentication_outage(self):
        log=self.root/'tool.jsonl'; stderr=self.root/'stderr'; stderr.write_text('')
        log.write_text(json.dumps(dict(event='step_update',step_update=dict(tool_info=dict(error=dict(type='TOOL_ERROR',message='expected 401, got 200')))))+'\n'+json.dumps(dict(event='result',result=dict(status='SUCCESS',response='Acceptance failed'))))
        result=agy_outcome(log,stderr,0)
        self.assertFalse(result['success']); self.assertIsNone(result['provider_failure'])

    def test_wait_ignores_intermediate_phases(self):
        self.c.initialize(self.tasks); r=self.c.prepare(self.tasks[0])
        def change():
            time.sleep(.05); r['phase']='worker_running'; self.c.save()
            time.sleep(.1); r['phase']='committed'; self.c.save()
            time.sleep(.1); r['phase']='rejected'; self.c.save()
        thread=threading.Thread(target=change); thread.start()
        try:
            result=watch_state(self.c.directory,3)
            self.assertEqual(result['tasks']['T001']['phase'],'rejected')
        finally: thread.join()

    def test_artifact_capture_and_missing_required_artifacts(self):
        self.config['required_artifacts']={'T001':['control.json']}
        command=[sys.executable,'-c','import os,json;from pathlib import Path;Path(os.environ["MIGRATION_ARTIFACT_DIR"],"control.json").write_text(json.dumps({"origin":os.environ["MIGRATION_CHECK_ORIGIN"]}))']
        results=self.c.checks(self.repo,[command],'T001-branch')
        self.assertEqual(results[0]['origin'],'coordinator')
        artifacts=results[0]['artifacts']; self.assertTrue(any(a['path'].endswith('control.json') and len(a['sha256'])==64 for a in artifacts))
        with self.assertRaises(Stop): self.c.checks(self.repo,[[sys.executable,'-c','pass']],'T001-branch')

    def test_public_contract_gate_before_review(self):
        self.config['public_checks']={'T001':[[sys.executable,'-c','raise SystemExit(2)']]}
        c=self.routed()
        with self.assertRaisesRegex(Stop,'required check'): c.execute(self.tasks)
        self.assertFalse(c.artifact('T001','review-1-job.json').exists())
        self.assertIsNone(c.state['records']['T001']['commit'])

    def test_ignored_partial_files_are_preserved(self):
        self.c.initialize(self.tasks); r=self.c.prepare(self.tasks[0])
        before=snapshot(r['worktree'])
        Path(r['worktree'],'.gitignore').write_text('hidden-evidence\n')
        Path(r['worktree'],'hidden-evidence').write_text('authentic raw output')
        self.assertNotEqual(before,snapshot(r['worktree']))
        self.assertIn('hidden-evidence',snapshot(r['worktree'])['files'])

    def test_agy_usage_terminal_only(self):
        log=self.root/'usage.jsonl'
        log.write_text(json.dumps(dict(event='step_update',step_update=dict(usage=dict(input_tokens=999))))+'\n'+json.dumps(dict(event='result',result=dict(usage=dict(input_tokens=100,cache_read_tokens=25,output_tokens=5,thinking_tokens=2)))))
        self.assertEqual(log_usage(log)['tokens']['input_tokens'],100)
        self.assertEqual(log_usage(log)['tokens']['cached_input_tokens'],25)

if __name__=='__main__': unittest.main()
