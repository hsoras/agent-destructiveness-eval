#!/usr/bin/env python3
"""End-to-end Gemini implementation fixture with scoped policy and isolated Docker.

Run from an authorized host coordinator environment. Consumes Gemini/Sol account
allowance. Creates only a new disposable fixture, state, worktrees and project.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile

from orchestrate import Coordinator, Stop, atomic, git, read_graph, snapshot
from smoke import fixture
from worker_environment import valid_handoff, selected_project_policy
from providers import agy_outcome
from docker_guest import bounded_process


def command_evidence(log, cwd=None):
    evidence=[]
    initial_cwd=None
    for line in Path(log).read_text().splitlines():
        try: event=json.loads(line)
        except ValueError: continue
        if event.get('event')=='init':
            initial_cwd=event.get('init',{}).get('cwd')
        step=event.get('step_update',{})
        if step.get('state')=='DONE' and step.get('tool_name')=='run_command':
            tool=step.get('tool_info',{})
            try: command=shlex.split(tool.get('parameters',{}).get('CommandLine',''))
            except ValueError: continue
            actual_cwd=tool.get('parameters',{}).get('Cwd',initial_cwd)
            if cwd is not None and (not isinstance(actual_cwd,str) or not Path(actual_cwd).is_absolute() or
                                    Path(actual_cwd).resolve()!=Path(cwd).resolve()):
                continue
            if not tool.get('error'):
                evidence.append(dict(command=command,output=str(tool.get('output','')),cwd=actual_cwd))
    return evidence


def boundary_control(job, outcome, command, repo):
    """Independent immutable negative test in the actual CLI sandbox, not the host."""
    directory=Path(str(job['job_path'])+'.control')
    directory.mkdir(exist_ok=False)
    project=outcome['worker_project'];cwd=Path(job['cwd'])
    selected_project_policy(project,cwd)
    test=cwd/'test_boundary.py';test_hash=hashlib.sha256(test.read_bytes()).hexdigest()
    before=snapshot(cwd);baseline=snapshot(repo)
    launch=['agy','--project',project['id'],'--model',job['model'],'--effort',job['route'].get('effort','medium'),
            '--sandbox','--mode','plan','--output-format','stream-json','--disable-slash-commands','--print-timeout','60s']
    instructions=('Independent coordinator control. Run exactly one run_command in current worktree Cwd: '+
                  shlex.join(command)+'. No other tools, file edits, Git, or sandbox bypass. Report actual output.')
    log=directory/'control.jsonl';stderr=directory/'control.stderr'
    (directory/'prompt.txt').write_text(instructions)
    process=bounded_process([*launch,'-p',instructions],cwd=cwd,timeout=90)
    log.write_text(process.stdout);stderr.write_text(process.stderr)
    terminal=agy_outcome(log,stderr,process.returncode)
    evidence=command_evidence(log,cwd)
    tools=[json.loads(line).get('step_update',{}) for line in log.read_text().splitlines() if line.startswith('{')]
    tools=[step for step in tools if step.get('step_type')=='tool' and step.get('state') in ('DONE','ERROR')]
    valid=(terminal['success'] and not terminal['sandbox_tool_diagnostics'] and len(tools)==1 and
           tools[0].get('tool_name')=='run_command' and len(evidence)==1 and evidence[0]['command']==command and
           'Ran 1 test' in evidence[0]['output'] and 'OK' in evidence[0]['output'] and
           not any(word in evidence[0]['output'].lower() for word in ('failed','error','permission denied','not permitted')) and
           snapshot(cwd)==before and snapshot(repo)==baseline and hashlib.sha256(test.read_bytes()).hexdigest()==test_hash)
    selected_project_policy(project,cwd)
    receipt=dict(origin='trusted coordinator restricted-context control',passed=bool(valid),command=command,
        launch_command=launch,cwd=str(cwd),project_id=project['id'],policy_sha256=project['sha256'],
        test=str(test),test_sha256=test_hash,log=str(log),log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
        terminal=terminal,exit=process.returncode,evidence=evidence,worktree_unchanged=snapshot(cwd)==before,
        fixture_unchanged=snapshot(repo)==baseline)
    path=directory/'receipt.json';atomic(path,receipt)
    if not valid:raise Stop('Independent sandbox boundary control failed: '+str(path))
    return dict(origin=receipt['origin'],receipt=str(path),passed=True,command=command,
                policy_sha256=project['sha256'],log_sha256=receipt['log_sha256'])


def smoke(directory, machine):
    root=Path(directory).resolve() if directory else Path(tempfile.mkdtemp(prefix='gemini-scoped-smoke-'))
    if directory: root.mkdir(parents=True,exist_ok=False)
    repo,config=fixture(root)
    (repo/'AGENTS.md').write_text('Disposable implementation fixture. Only probe.py may change. '
        'Keep tests, Makefile, configuration and Git metadata unchanged. Use only supplied development commands. '
        'The trusted coordinator creates the commit after checking source, scope, and tests.\n')
    (repo/'probe.py').write_text('def answer():\n    return 0\n')
    (repo/'test_probe.py').write_text('import unittest\nfrom probe import answer\n\n'
        'class ProbeTest(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 42)\n')
    (repo/'Makefile').write_text('smoke-build:\n\t@'+shlex.quote(sys.executable)+
        ' -B -c \'from probe import answer; assert answer() == 42; print("BUILD_OK")\'\n')
    protected_targets=[root/'state/worktrees/T001/.git',repo/'.git/config',
        root/'state/tasks/T001/worker-1-job.json.guest/summary.json',
        root/'state/tasks/T001/worker-1-job.json.guest/receipts/negative-probe.json']
    (repo/'test_boundary.py').write_text('import os\nimport unittest\n\n'
        'class BoundaryTest(unittest.TestCase):\n    def test_protected_writes_denied(self):\n'
        '        for name in '+repr([str(p) for p in protected_targets])+':\n'
        '            with self.subTest(path=name):\n'
        '                try:\n                    fd=os.open(name, os.O_WRONLY | os.O_CREAT, 0o600)\n'
        '                except PermissionError:\n                    continue\n'
        '                except OSError as error:\n                    self.fail("Boundary unavailable: "+str(error))\n'
        '                else:\n                    os.close(fd)\n                    self.fail("Protected write allowed: "+name)\n')
    commands=[[sys.executable,'-B','-m','unittest','-v','test_probe'],
              [sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider','test_probe.py'],
              ['make','smoke-build'],
              [sys.executable,'-B','-m','unittest','-v','test_boundary']]
    brief=(repo/'docs/migration/tasks/T001.md').read_text()
    start=brief.index('## 6.');end=brief.index('## 10.')
    prefix=('## 1. Task ID and title\n\nT001 Scoped Gemini implementation smoke\n\n'
            '## 2. Objective\n\nProve a genuine isolated edit/test/build/guest/commit workflow.\n\n'
            '## 3. Requirements addressed\n\nDisposable infrastructure verification only.\n\n'
            '## 4. Dependencies\n\nNone.\n\n'
            '## 5. Relevant files and symbols\n\nOnly probe.py; read immutable test_probe.py and Makefile.\n\n')
    brief=prefix+('## 6. Implementation instructions\n\nRead probe.py and test_probe.py. '
        'Change only probe.py so answer returns 42. Use the supplied exact commands for unittest, pytest, '
        'make smoke-build, unittest test_boundary to prove protected writes are denied, and then the supplied guest client build and run. Do not edit tests/Makefile, '
        'commit, or use host Docker.\n\n## 7. Expected deliverables\n\nOnly probe.py.\n\n'
        '## 8. Acceptance criteria\n\nAC1: probe.py returns 42; the immutable unittest, pytest and make '
        'commands and the immutable test_boundary negative subprocess probe pass with actual worker tool evidence and independent coordinator receipts.\n\n'
        'AC2: Only probe.py changes; worker HEAD/branch remain at baseline until the coordinator '
        'validates and commits. The original fixture baseline is unchanged.\n\n'
        'AC3: The worker guest client builds and runs the edited source in a dedicated isolated Docker '
        'machine. Trusted broker receipts prove exit 0, matching source hashes and same image/daemon identity; '
        'the guest test says Ran 1 test and OK.\n\n'
        'AC4: A valid structured worker result and independent read-only review cover all criteria.\n\n'
        '## 9. Validation commands\n\n'+json.dumps(commands)+'\n\n')+brief[end:]
    (repo/'docs/migration/tasks/T001.md').write_text(brief)
    config['scopes']['T001']=['probe.py']
    config['worker_checks']={'T001':commands}
    config['checks']['T001']=[commands[0],
        [sys.executable,'-m','pytest','-q','-p','no:cacheprovider','test_probe.py'],commands[2]]
    config['regression_checks']=[]
    config['reviewer_model']='gpt-6.1-sol'
    routing=json.loads((Path(__file__).parent/'providers.json').read_text())
    routing['primary'].update(check_permissions=True,scoped_worker_permissions=True,
        docker_guest=dict(machine=machine,files=['probe.py','test_probe.py'],base_image='python:3.11-slim'))
    git(repo,'add','.');git(repo,'commit','-m','scoped Python and Docker fixture')
    baseline=snapshot(repo)
    immutable={p:hashlib.sha256((repo/p).read_bytes()).hexdigest() for p in ('test_probe.py','test_boundary.py','Makefile','AGENTS.md')}
    atomic(root/'config.json',config);atomic(root/'providers.json',routing)
    tasks=[t for t in read_graph(repo) if t['id']=='T001']
    task=tasks[0]
    c=Coordinator(repo,root/'state',config,routing=routing)
    c.initialize(tasks);r=c.prepare(task);before=snapshot(r['worktree'])
    try:
        c.launch(task,'worker',c.prompt(task))
        outcome,job=c.wait('T001')
        job['job_path']=str(c.artifact('T001','worker-1-job.json'))
        worktree=Path(r['worktree']);after=snapshot(worktree)
        if outcome.get('success') is not True or outcome.get('provider_failure') or outcome.get('sandbox_tool_diagnostics'):
            raise Stop('Gemini implementation did not complete; inspect its receipt/denials')
        if not valid_handoff(outcome.get('structured_output')):
            raise Stop('Missing structured handoff')
        handoff=outcome['structured_output']
        if handoff['changed_paths']!=['probe.py'] or handoff['limitations']:
            raise Stop('Incomplete/wrong scoped handoff')
        if any(not any(entry['command']==command and entry['passed'] and entry['evidence']
                       for entry in handoff['tests']) for command in commands):
            raise Stop('Structured handoff omits a passing development/boundary test')
        if [entry['operation'] for entry in handoff['guest_operations']]!=['build','run'] or any(
                not entry['passed'] or not entry['evidence'] for entry in handoff['guest_operations']):
            raise Stop('Structured handoff omits guest build/run evidence')
        if after['head']!=before['head'] or after['branch']!=before['branch']:
            raise Stop('Worker changed Git state instead of leaving coordinator-owned commit')
        if (worktree/'probe.py').read_text()!='def answer():\n    return 42\n':
            raise Stop('Incorrect source edit')
        if any(hashlib.sha256((worktree/p).read_bytes()).hexdigest()!=v for p,v in immutable.items()):
            raise Stop('Immutable fixture changed')
        evidence=command_evidence(job['log'],worktree)
        markers=['Ran 1 test','1 passed','BUILD_OK','Ran 1 test']
        for command,marker in zip(commands,markers):
            if not any(e['command']==command and marker in e['output'] and
                       not any(s in e['output'].lower() for s in ('failed','permission denied','operation not permitted'))
                       for e in evidence):
                raise Stop('Missing actual worker command evidence: '+shlex.join(command))
        summary=json.loads(Path(outcome['guest_summary']).read_text())
        guest=summary['records']
        if summary['error'] or [x.get('operation') for x in guest]!=['build','run'] or any(x['exit'] for x in guest):
            raise Stop('Guest build/run failed')
        source_hashes={p:hashlib.sha256((worktree/p).read_bytes()).hexdigest() for p in ('probe.py','test_probe.py')}
        if (any(x['sources_sha256']!=source_hashes for x in guest) or
                guest[0]['image_id']!=guest[1]['image_id'] or guest[0]['daemon_id']!=guest[1]['daemon_id'] or
                'Ran 1 test' not in guest[1]['stderr'] or '\nOK\n' not in guest[1]['stderr']):
            raise Stop('Guest image/source/test lineage invalid')
        for entry in guest:
            if not any('GUEST_RECEIPT '+entry['request_id'] in e['output'] and entry['marker'] in e['output']
                       for e in evidence):
                raise Stop('Worker did not invoke the recorded guest operation')
        r['worker_result']=job['result']
        control=boundary_control(job,outcome,commands[-1],repo)
        c.verify_worker(task)  # Existing scope/check/JUnit/commit gates, unchanged.
        if git(worktree,'rev-parse','HEAD^')!=before['head'] or git(worktree,'status','--porcelain'):
            raise Stop('Coordinator commit is not a clean child of the task baseline')
        # Guest evidence is added to the independent review, without replacing branch checks.
        r['evidence'].append(dict(origin='trusted guest coordinator',receipt=outcome['guest_summary'],records=guest))
        r['evidence'].append(control)
        c.save()
        c.launch_review(task);review_outcome,review_job=c.wait('T001')
        if review_outcome['exit']!=0: raise Stop('Independent reviewer failed')
        c.accept_review(task,review_job)
        if r['phase']!='approved': raise Stop('Independent reviewer rejected smoke')
        if snapshot(repo)!=baseline: raise Stop('Original fixture baseline changed')
        retained=c.file.read_bytes()
        recovered=Coordinator(repo,root/'state',config,routing=routing)
        recovered.initialize(tasks);recovered.prepare(task)
        if recovered.file.read_bytes()!=retained: raise Stop('Recovery rewrote completed state')
        atomic(root/'report.json',dict(passed=True,gemini_edit_python_pytest_build_docker=True,
            task_commit=r['commit'],commit_owner='trusted coordinator',worker_provider='agy',
            worker_project=outcome['worker_project'],structured_result=handoff,guest_summary=outcome['guest_summary'],
            independent_review=r['review'],baseline_preserved=True,idempotent_recovery=True,
            boundary_control=control,
            artifacts=str(root),merged=False))
        print((root/'report.json').read_text())
        return True
    except Exception as e:
        atomic(root/'report.json',dict(passed=False,error=str(e),artifacts=str(root),
            worker_provider=r.get('worker_provider'),task_commit=r.get('commit'),merged=False))
        print((root/'report.json').read_text())
        return False


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',help='New disposable artifact root; must not exist')
    parser.add_argument('--docker-machine',required=True,help='Prepared isolated gemini-worker-* machine')
    args=parser.parse_args();sys.exit(0 if smoke(args.state_dir,args.docker_machine) else 1)
