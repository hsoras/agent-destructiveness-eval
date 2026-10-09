"""Scoped worker and broker checks: disposable files, no model or Docker calls."""
import hashlib
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from worker_environment import (create_project, selected_project_policy, development_commands,
                                EnvironmentUnavailable, host_readiness, valid_handoff)
from docker_guest import GuestBroker, bounded_process
from providers import effective_permissions, permission_preflight


class ScopedWorkerTests(unittest.TestCase):
    def test_project_failure_and_cleanup_failure_still_write_fallback_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); path=root/'job.json'
            job=dict(provider='agy',command=['agy','--sandbox'],cwd=str(root),route=dict(
                scoped_worker_permissions=True,docker_guest=dict(machine='gemini-worker-unit')))
            for key in ('prompt','receipt','result','log','stderr'):job[key]=str(root/key)
            path.write_text(json.dumps(job))
            guest=Mock(directory=root/'guest',receipts=root/'guest/receipts',responses=root/'guest/responses',
                       requests=root/'guest/requests',error=None)
            guest.client_commands.return_value=[]
            guest.close.side_effect=RuntimeError('cleanup failed')
            with patch.object(sys,'argv',['job.py',str(path)]), patch('worker_environment.host_readiness'), patch(
                    'worker_environment.create_project',side_effect=EnvironmentUnavailable('permissions','project denied')), patch(
                    'providers.preflight',return_value=dict(ready=True)), patch('providers.snapshot',return_value={}), patch(
                    'docker_guest.GuestBroker',return_value=guest):
                runpy.run_path(str(Path(__file__).parent/'job.py'),run_name='__main__')
            receipt=json.loads(Path(job['receipt']).read_text())
            self.assertEqual(receipt['provider_failure'],'permissions');self.assertFalse(receipt['success'])
            self.assertIn('cleanup failed',receipt['guest_error']);guest.start.assert_not_called()

    def test_unsandboxed_deny_does_not_false_certify_sandboxed_command(self):
        result=permission_preflight([['python3','-m','pytest']],
            permissions=dict(allow=['command(python3 -m pytest)'],deny=['unsandboxed(*)']))
        self.assertFalse(result['ready'])

    def test_selected_policy_keeps_shared_ask_and_rejects_bypass(self):
        policy=dict(project='selected')
        scopes=[dict(scope='project'),dict(scope='shared',ask=['command(*)']),dict(scope='global')]
        process=subprocess.CompletedProcess([],0,json.dumps(dict(status='SUCCESS',
            command=dict(data=dict(permissions=scopes)))),'')
        with patch('providers.subprocess.run',return_value=process), patch(
                'worker_environment.selected_project_policy',return_value=dict(allow=['command(make build)'],deny=[])):
            effective=effective_permissions('/tmp',project=policy)
        self.assertTrue(effective['ready'])
        self.assertFalse(permission_preflight([['make','build']],permissions=effective['permissions'])['ready'])
        with patch('providers.subprocess.run',return_value=process), patch(
                'worker_environment.selected_project_policy',return_value=dict(allow=['unsandboxed(make build)'])):
            self.assertFalse(effective_permissions('/tmp',project=policy)['ready'])

    def test_development_commands_exclude_privileged_checks(self):
        commands = [['python3','-B','-m','pytest','-q'], ['make','build'], ['npm','run','test'],
                    ['docker','build','.'], ['python3','script.py'], ['make','build; whoami']]
        allowed, trusted = development_commands(commands)
        self.assertEqual(allowed,commands[:3]); self.assertEqual(trusted,commands[3:])

    def test_project_exact_worktree_runtime_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(); repo=root/'repo';repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'symbolic-ref','HEAD','refs/heads/disposable'],check=True)
            executable=root/'venv/bin/python';executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
            project=create_project(repo,[[str(executable),'-B','-m','pytest','-q']],
                                   protected=[root/'receipt.json'],projects_dir=root/'projects')
            rules=selected_project_policy(project,repo)
            actual=Path(sys.executable).resolve().parent.parent
            self.assertIn('read_file('+str(actual)+')',rules['allow'])
            self.assertNotIn('write_file('+str(actual)+')',rules['allow'])
            self.assertIn('write_file('+str(repo/'.git')+')',rules['deny'])
            self.assertIn('write_file('+str(root/'receipt.json')+')',rules['deny'])
            self.assertEqual(project['git_boundary']['common_git_dir'],str(repo/'.git'))
            path=Path(project['path']);path.write_text(path.read_text()+' ')
            with self.assertRaises(EnvironmentUnavailable):selected_project_policy(project,repo)

    def test_localhost_failure_is_initialization(self):
        with patch('worker_environment.socket.socket',side_effect=PermissionError('outer sandbox')):
            with self.assertRaises(EnvironmentUnavailable) as context:host_readiness()
        self.assertEqual(context.exception.reason,'initialization')

    def test_structured_handoff_rejects_incomplete_receipt(self):
        self.assertFalse(valid_handoff({'changed_paths':['probe.py']}))
        receipt=dict(changed_paths=['probe.py'],tests=[dict(command=['make','build'],passed=True,evidence='BUILD_OK')],
                     guest_operations=[dict(operation='run',passed=True,evidence='receipt')],limitations=[])
        self.assertTrue(valid_handoff(receipt));receipt['tests'][0]['passed']=1
        self.assertFalse(valid_handoff(receipt))


class GuestSafetyTests(unittest.TestCase):
    def test_close_cancels_owned_active_process_and_skips_queued_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);requests=root/'requests';requests.mkdir()
            for token in ('a'*32,'b'*32):
                (requests/(token+'.json')).write_text('{"operation":"build"}')
            broker=GuestBroker.__new__(GuestBroker);broker.directory=root;broker.records=[];broker.error=None
            broker.machine='gemini-worker-unit';broker.daemon_id='daemon';broker.stop=threading.Event()
            broker.requests=requests;broker.responses=root/'responses';broker.responses.mkdir()
            broker.receipts=root/'receipts';broker.receipts.mkdir()
            broker.root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            broker.request_fd=os.open(requests,os.O_RDONLY|os.O_DIRECTORY)
            started=threading.Event()
            def execute(operation):
                started.set()
                result=bounded_process([sys.executable,'-c','import time;time.sleep(30)'],cancel=broker.stop)
                return dict(operation=operation,exit=result.returncode,stderr=result.stderr)
            broker.execute=execute;broker.thread=threading.Thread(target=broker.serve,daemon=False)
            broker.start();self.assertTrue(started.wait(2))
            before=time.monotonic();broker.close()
            self.assertLess(time.monotonic()-before,2);self.assertFalse(broker.thread.is_alive())
            self.assertEqual(len(broker.records),1);self.assertEqual(broker.records[0]['exit'],130)

    def test_close_before_start_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);broker=GuestBroker.__new__(GuestBroker)
            broker.directory=root;broker.records=[];broker.error=None;broker.machine='gemini-worker-unit'
            broker.daemon_id='daemon';broker.stop=threading.Event();broker.thread=threading.Thread(target=lambda:None)
            broker.root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            broker.request_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            broker.close()
            self.assertTrue((root/'summary.json').is_file());self.assertIsNone(broker.error)

    def test_request_symlink_and_growth_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);broker=GuestBroker.__new__(GuestBroker)
            broker.request_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            self.addCleanup(os.close,broker.request_fd)
            (root/'request.json').symlink_to('/etc/hosts')
            with self.assertRaises(OSError):broker.read_request('request.json')
            (root/'request.json').unlink();(root/'request.json').write_text('{"operation":"build"}')
            real_stat=os.fstat
            def grow(fd):
                result=real_stat(fd)
                with (root/'request.json').open('a') as file:file.write(' '*5000)
                return result
            with patch('docker_guest.os.fstat',side_effect=grow):
                with self.assertRaises(ValueError):broker.read_request('request.json')

    def test_output_limit_and_timeout(self):
        output=bounded_process([sys.executable,'-c','import sys;sys.stdout.write("x"*1000000)'],limit=1024)
        self.assertEqual(output.returncode,125);self.assertLessEqual(len(output.stdout),1024)
        self.assertIn('output exceeded limit',output.stderr)
        output=bounded_process([sys.executable,'-c','import time;time.sleep(10)'],timeout=.1)
        self.assertEqual(output.returncode,124);self.assertIn('timed out',output.stderr)

    def test_closed_stdin_and_streamed_input(self):
        command=[sys.executable,'-c','import sys;print(len(sys.stdin.buffer.read()))']
        self.assertEqual(bounded_process(command).stdout,'0\n')
        self.assertEqual(bounded_process(command,b'x'*100000).stdout,'100000\n')

    def test_recipe_and_path_reservations(self):
        for filename in ('Dockerfile','nested/Dockerfile','.dockerignore','../source','/source'):
            with tempfile.TemporaryDirectory() as temporary:
                with patch.object(GuestBroker,'inspect_machine') as inspect:
                    with self.assertRaises(EnvironmentUnavailable):
                        GuestBroker(temporary,Path(temporary)/'queue',dict(machine='gemini-worker-unit',files=[filename]))
                    inspect.assert_not_called()

    def test_sources_reject_symlinks_and_large_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'source').symlink_to('/etc/hosts')
            broker=GuestBroker.__new__(GuestBroker);broker.cwd=root;broker.files=['source']
            broker.root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            self.addCleanup(os.close,broker.root_fd)
            with self.assertRaises(ValueError):broker.sources()
            (root/'source').unlink()
            with (root/'source').open('wb') as f:f.truncate(33*1024*1024)
            with self.assertRaises(ValueError):broker.sources()

    def test_parent_swap_after_open_never_reads_external_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'nested').mkdir();(root/'outside').mkdir()
            (root/'nested/source').write_bytes(b'allowed');(root/'outside/source').write_bytes(b'secret')
            broker=GuestBroker.__new__(GuestBroker);broker.cwd=root;broker.files=['nested/source']
            broker.root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            self.addCleanup(os.close,broker.root_fd)
            original_open=os.open
            def swap(path,flags,*args,**kwargs):
                if path=='source':
                    (root/'nested').rename(root/'original')
                    (root/'nested').symlink_to(root/'outside',target_is_directory=True)
                return original_open(path,flags,*args,**kwargs)
            with patch('docker_guest.os.open',side_effect=swap):
                self.assertEqual(broker.sources(),{'nested/source':b'allowed'})

    def test_failed_run_cleans_exact_owned_container(self):
        broker=GuestBroker.__new__(GuestBroker)
        record=dict(id='dedicated',config=dict(isolated=True))
        broker.config=record;broker.inspect_machine=lambda:record
        broker.sources=lambda:{'probe.py':b'example'}
        broker.image_id='sha256:image';broker.built_sources={'probe.py':hashlib.sha256(b'example').hexdigest()}
        broker.container_name='gemini-worker-exact';broker.machine='gemini-worker-unit';broker.daemon_id='daemon'
        with patch.object(broker,'orb',side_effect=[subprocess.CompletedProcess(['run'],124,'','timeout'),
                                                   subprocess.CompletedProcess(['rm'],0,'','')]) as orb:
            receipt=broker.execute('run')
        self.assertEqual(receipt['exit'],124)
        self.assertEqual(orb.call_args_list[1].args[0],['docker','rm','-f','gemini-worker-exact'])
        self.assertIn('--network',orb.call_args_list[0].args[0])

    def test_changed_source_cannot_run_old_image(self):
        broker=GuestBroker.__new__(GuestBroker)
        broker.config={'id':'dedicated','config':{}};broker.inspect_machine=lambda:broker.config
        broker.sources=lambda:{'probe.py':b'changed'};broker.image_id='sha256:image';broker.built_sources={}
        with patch.object(broker,'orb') as orb:
            with self.assertRaises(ValueError):broker.execute('run')
            orb.assert_not_called()


if __name__=='__main__':unittest.main()
