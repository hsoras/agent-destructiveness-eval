"""Scoped worker and broker checks: disposable files, no model or Docker calls."""
import hashlib
import json
import os
import runpy
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from worker_environment import (create_project, selected_project_policy, development_commands,
                                EnvironmentUnavailable, host_readiness, valid_handoff,
                                scoped_worker_environment)
from docker_guest import GuestBroker, bounded_process
from providers import effective_permissions, permission_preflight


class ScopedWorkerTests(unittest.TestCase):
    def temporary_project(self, root, attempt=1):
        repo=root/'repo';repo.mkdir(exist_ok=True)
        with patch('worker_environment.git_boundary',return_value=dict(common_git_dir=str(repo/'.git'))):
            return create_project(repo,[],attempt=attempt,projects_dir=root/'projects')

    def test_temporary_launch_executes_fixture_inside_exact_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();project=self.temporary_project(root,7)
            other=self.temporary_project(root,8)
            env=scoped_worker_environment(project,root/'repo',7,
                environ=dict(os.environ,TMPDIR='/outside',TEMP='/outside',TMP='/outside'))
            expected=str(root/'repo'/project['temporary_directory']['relative_path'])
            self.assertEqual([env[k] for k in ('TMPDIR','TEMP','TMP')],[expected]*3)
            self.assertNotEqual(project['temporary_directory']['relative_path'],other['temporary_directory']['relative_path'])
            self.assertEqual(env['PYTHONDONTWRITEBYTECODE'],'1')
            script=('import tempfile,pathlib,subprocess,sys; '
                    'p=pathlib.Path(tempfile.mkdtemp()); '
                    'f=p/"mock"; f.write_text("#!"+sys.executable+"\\nprint(\\\"MOCK_OK\\\")\\n"); '
                    'f.chmod(0o700); print(p); subprocess.run([str(f)],check=True)')
            result=subprocess.run([sys.executable,'-B','-c',script],env=env,
                                  text=True,capture_output=True,check=True)
            self.assertIn('MOCK_OK',result.stdout)
            self.assertIn(expected+'/',result.stdout)
            self.assertTrue(Path(expected).is_dir())

    def test_temporary_metadata_attempt_workspace_and_inode_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();project=self.temporary_project(root,3)
            for change in ({'temporary_directory':None},{'workspace':str(root)},
                           {'temporary_directory':dict(project['temporary_directory'],attempt=4)},
                           {'temporary_directory':dict(project['temporary_directory'],relative_path='../outside')}):
                with self.subTest(change=change),self.assertRaises(EnvironmentUnavailable):
                    scoped_worker_environment(dict(project,**change),root/'repo',3)
            with self.assertRaises(EnvironmentUnavailable):
                scoped_worker_environment(project,root/'repo',4)
            path=root/'repo'/project['temporary_directory']['relative_path']
            path.rename(path.with_name('retained'));path.mkdir(mode=0o700)
            with self.assertRaises(EnvironmentUnavailable):selected_project_policy(project,root/'repo')

    def test_temporary_symlink_collision_and_unsafe_mode_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();project=self.temporary_project(root)
            path=root/'repo'/project['temporary_directory']['relative_path']
            path.chmod(0o755)
            with self.assertRaises(EnvironmentUnavailable):selected_project_policy(project,root/'repo')
            path.chmod(0o700)
            with patch('worker_environment.uuid.uuid4',return_value=project['id']):
                with self.assertRaises(EnvironmentUnavailable):self.temporary_project(root)
            path.rename(path.with_name('retained'));path.symlink_to(root,target_is_directory=True)
            with self.assertRaises(EnvironmentUnavailable):selected_project_policy(project,root/'repo')
            (root/'repo/.scenario').rename(root/'retained-scenario')
            (root/'repo/.scenario').symlink_to(root,target_is_directory=True)
            with self.assertRaises(EnvironmentUnavailable):self.temporary_project(root)

    def test_temporary_invalid_attempts_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            for attempt in (0,-1,True,'1',None):
                with self.subTest(attempt=attempt),self.assertRaises(EnvironmentUnavailable):
                    self.temporary_project(root,attempt)

    def test_temporary_parent_swap_after_authentic_open_fails_closed(self):
        for mode in ('symlink','relocate_leaf','creation'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary).resolve();project=self.temporary_project(root,7)
                parent=root/'repo/.scenario/worker-tmp';outside=root/'outside';outside.mkdir()
                original_open=os.open;swapped=False
                def swap(path,flags,*args,**kwargs):
                    nonlocal swapped
                    descriptor=original_open(path,flags,*args,**kwargs)
                    if not swapped and str(path).endswith('-7'):
                        swapped=True;parent.rename(parent.with_name('retained'))
                        if mode=='relocate_leaf':
                            parent.mkdir()
                            (parent.with_name('retained')/str(path)).rename(parent/str(path))
                        else:
                            parent.symlink_to(outside,target_is_directory=True)
                    return descriptor
                with patch('worker_environment.os.open',side_effect=swap):
                    with self.assertRaises(EnvironmentUnavailable):
                        if mode=='creation':self.temporary_project(root,7)
                        else:scoped_worker_environment(project,root/'repo',7)
                self.assertTrue(swapped)

    def test_temporary_parent_swap_between_policy_and_handoff_fails_closed(self):
        for component in ('parent','workspace'):
            with self.subTest(component=component),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary).resolve();project=self.temporary_project(root,7)
                parent=root/'repo/.scenario/worker-tmp';outside=root/'outside'
                def swap_after_policy(*args):
                    rules=selected_project_policy(*args)
                    if component=='workspace':
                        (root/'repo').rename(outside)
                        (root/'repo').symlink_to(outside,target_is_directory=True)
                    else:
                        outside.mkdir();parent.rename(parent.with_name('retained'))
                        parent.symlink_to(outside,target_is_directory=True)
                    return rules
                with patch('worker_environment.selected_project_policy',side_effect=swap_after_policy):
                    with self.assertRaises(EnvironmentUnavailable):
                        scoped_worker_environment(project,root/'repo',7)

    def test_temporary_job_environment_only_changes_scoped_agy(self):
        for provider, scoped in (('agy',True),('agy',False),('codex',False)):
            with self.subTest(provider=provider,scoped=scoped),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary).resolve();project=self.temporary_project(root,2)
                job=dict(provider=provider,command=[provider,'--sandbox'],cwd=str(root/'repo'),
                         route=dict(scoped_worker_permissions=scoped),attempt=2,timeout=5)
                for key in ('prompt','receipt','result','log','stderr'):job[key]=str(root/key)
                Path(job['prompt']).write_text('fixture prompt')
                path=root/'job.json';path.write_text(json.dumps(job))
                process=Mock(pid=123);process.wait.return_value=0
                parsed=dict(response='done',success=True,structured_output=dict(
                    changed_paths=[],tests=[],guest_operations=[],limitations=[]))
                with patch.object(sys,'argv',['job.py',str(path)]),patch(
                        'worker_environment.host_readiness'),patch(
                        'worker_environment.create_project',return_value=project) as create,patch(
                        'providers.preflight',return_value=dict(ready=True)),patch(
                        'providers.effective_permissions',return_value=dict(ready=True,permissions=project['permissions'])),patch(
                        'providers.permission_preflight',return_value=dict(ready=True)),patch(
                        'providers.snapshot',return_value={}),patch(
                        'providers.agy_outcome',return_value=parsed),patch(
                        'subprocess.Popen',return_value=process) as launch,patch.dict(os.environ,TMPDIR='/outside'):
                    runpy.run_path(str(Path(__file__).parent/'job.py'),run_name='__main__')
                actual=launch.call_args.kwargs['env']
                if scoped:
                    self.assertEqual(actual['TMPDIR'],str(root/'repo'/project['temporary_directory']['relative_path']))
                    self.assertEqual(create.call_args.kwargs['attempt'],2)
                    receipt=json.loads(Path(job['receipt']).read_text())
                    self.assertEqual(receipt['worker_project']['temporary_directory'],project['temporary_directory'])
                else:
                    self.assertIsNone(actual);create.assert_not_called()

    def test_boundary_control_requires_exact_command_and_immutable_test(self):
        from scoped_worker_smoke import boundary_control
        from orchestrate import Stop
        command=['python3','-B','-m','unittest','-v','test_boundary']
        for mode in ('pass','wrong_command','wrong_cwd','missing_init','test_modified','exit_zero_denial'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);test=root/'test_boundary.py';test.write_text('# immutable\n')
                job=dict(job_path=str(root/'job.json'),cwd=str(root),model='gemini-3.8-flash-medium',route={})
                outcome=dict(worker_project=dict(id='disposable',sha256='policy hash'))
                actual=command if mode!='wrong_command' else ['echo','forged output']
                events=[dict(event='init',init=dict(cwd=str(root))),
                    dict(event='step_update',step_update=dict(state='DONE',step_type='tool',tool_name='run_command',
                    tool_info=dict(parameters=dict(CommandLine=shlex.join(actual)),output='test_protected_writes_denied ... ok\nRan 1 test\nOK\n'))),
                    dict(event='result',result=dict(status='SUCCESS',response='done'))]
                if mode=='exit_zero_denial':events[-1]['result']['denied_actions']=[dict(action='command')]
                if mode=='wrong_cwd':events[1]['step_update']['tool_info']['parameters']['Cwd']='/elsewhere'
                if mode=='missing_init':events=events[1:]
                process=subprocess.CompletedProcess([],0,'\n'.join(json.dumps(e) for e in events),'')
                def run(*args,**kwargs):
                    self.assertIn('--sandbox',args[0]);self.assertNotIn('--dangerously-skip-permissions',args[0])
                    if mode=='test_modified':test.write_text('# changed\n')
                    return process
                with patch('scoped_worker_smoke.selected_project_policy'), patch(
                        'scoped_worker_smoke.snapshot',return_value={}), patch('scoped_worker_smoke.bounded_process',side_effect=run):
                    if mode=='pass':self.assertTrue(boundary_control(job,outcome,command,root)['passed'])
                    else:
                        with self.assertRaises(Stop):boundary_control(job,outcome,command,root)

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
            self.assertFalse(any(rule.startswith('unsandboxed(') for rule in rules['allow']))
            self.assertNotIn('unsandboxed(*)',rules['deny'])
            self.assertIn('write_file('+str(repo/'.git')+')',rules['deny'])
            self.assertIn('write_file('+str(root/'receipt.json')+')',rules['deny'])
            self.assertEqual(project['git_boundary']['common_git_dir'],str(repo/'.git'))
            binding=project['source_pyproject']
            if binding is not None:
                exact='read_file('+binding['path']+')'
                self.assertIn(exact,rules['allow'])
                self.assertNotIn('read_file('+str(Path(binding['path']).parent)+')',rules['allow'])
                self.assertNotIn('write_file('+binding['path']+')',rules['allow'])
                self.assertEqual(binding['sha256'],hashlib.sha256(Path(binding['path']).read_bytes()).hexdigest())
            self.assertIn('command(git)',rules['deny'])
            self.assertIn('read_file('+str(Path.home()/'.docker')+')',rules['deny'])
            path=Path(project['path']);path.write_text(path.read_text()+' ')
            with self.assertRaises(EnvironmentUnavailable):selected_project_policy(project,repo)

    def test_pyproject_binding_tamper_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'symbolic-ref','HEAD','refs/heads/disposable'],check=True)
            source=root/'trusted';(source/'scripts/migration').mkdir(parents=True)
            (source/'pyproject.toml').write_text('[tool.pytest]\n')
            with patch('worker_environment.__file__',str(source/'scripts/migration/worker_environment.py')):
                project=create_project(repo,[],projects_dir=root/'projects')
                rules=selected_project_policy(project,repo)
                grant='read_file('+str((source/'pyproject.toml').resolve())+')'
                self.assertIn(grant,rules['allow'])
                self.assertNotIn('read_file('+str(source.resolve())+')',rules['allow'])
                self.assertNotIn('write_file('+str((source/'pyproject.toml').resolve())+')',rules['allow'])
                tampered=dict(project,source_pyproject=dict(project['source_pyproject'],sha256='0'*64))
                with self.assertRaises(EnvironmentUnavailable):selected_project_policy(tampered,repo)
                (source/'pyproject.toml').unlink()
                (source/'pyproject.toml').symlink_to('/etc/hosts')
                with self.assertRaises(EnvironmentUnavailable):create_project(repo,[],projects_dir=root/'projects')

    def test_absent_pyproject_has_no_grant_and_lost_binding_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);repo=root/'repo';repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'symbolic-ref','HEAD','refs/heads/disposable'],check=True)
            source=root/'trusted';(source/'scripts/migration').mkdir(parents=True)
            module=source/'scripts/migration/worker_environment.py'
            with patch('worker_environment.__file__',str(module)):
                # An initially absent optional source file is a legitimate no-grant case.
                absent=create_project(repo,[],projects_dir=root/'projects')
                absent_rules=selected_project_policy(absent,repo)
                source_path=str((source/'pyproject.toml').resolve())
                self.assertIsNone(absent['source_pyproject'])
                self.assertNotIn('read_file('+source_path+')',absent_rules['allow'])

                (source/'pyproject.toml').write_text('[tool.pytest]\n')
                project=create_project(repo,[],projects_dir=root/'projects')
                policy_path=Path(project['path']);policy_bytes=policy_path.read_bytes()
                self.assertIn('read_file('+source_path+')',project['permissions']['allow'])
                (source/'pyproject.toml').unlink()
                for altered in (dict((k,v) for k,v in project.items() if k!='source_pyproject'),
                                dict(project,source_pyproject=None)):
                    with self.subTest(metadata='missing' if 'source_pyproject' not in altered else 'null'):
                        with self.assertRaises(EnvironmentUnavailable):
                            selected_project_policy(altered,repo)
                        self.assertEqual(policy_path.read_bytes(),policy_bytes)

                # A lost binding cannot be hidden by removing its grant metadata either.
                project_without_claim=dict(project,permissions=dict(project['permissions'],
                    allow=[rule for rule in project['permissions']['allow'] if rule!='read_file('+source_path+')']))
                with self.assertRaises(EnvironmentUnavailable):
                    selected_project_policy(project_without_claim,repo)
                self.assertEqual(policy_path.read_bytes(),policy_bytes)

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
