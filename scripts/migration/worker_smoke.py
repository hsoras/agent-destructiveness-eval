#!/usr/bin/env python3
"""Real Gemini implementation probe; disposable repositories, no migration state.

Consumes account allowance. Never edits permission settings or retries unsandboxed.
A successful documentation/authentication smoke is not implementation readiness.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from orchestrate import HERE, atomic, git
from providers import snapshot


SCHEMA = dict(type='object', properties={
    'changed_paths': dict(type='array', items=dict(type='string')),
    'tests': dict(type='array', items=dict(type='string')),
    'commit': dict(type='string'),
    'limitations': dict(type='array', items=dict(type='string'))},
    required=['changed_paths', 'tests', 'commit', 'limitations'], additionalProperties=False)


def smoke(directory=None):
    root = Path(directory).resolve() if directory else Path(tempfile.mkdtemp(prefix='gemini-worker-smoke-'))
    if directory:
        root.mkdir(parents=True, exist_ok=False)
    repo = root / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'smoke-base')
    git(repo, 'config', 'user.name', 'Disposable Gemini Smoke')
    git(repo, 'config', 'user.email', 'smoke@example.invalid')
    (repo/'probe.py').write_text('def answer():\n    return 0\n')
    (repo/'test_probe.py').write_text('import unittest\nfrom probe import answer\n\n'
                                   'class ProbeTest(unittest.TestCase):\n'
                                   '    def test_answer(self):\n        self.assertEqual(answer(), 42)\n')
    git(repo, 'add', '--', 'probe.py', 'test_probe.py')
    git(repo, 'commit', '-m', 'disposable implementation fixture')
    worktree = root/'worktree'
    git(repo, 'worktree', 'add', '-b', 'smoke-worker', str(worktree))
    before = snapshot(worktree)
    baseline = snapshot(repo)
    commands = [[sys.executable, '-B', '-m', 'unittest', '-v', 'test_probe'],
                ['git', 'diff', '--check'], ['git', 'add', '--', 'probe.py'],
                ['git', 'commit', '-m', 'gemini implementation smoke']]
    prompt = root/'prompt.txt'
    prompt.write_text(
        f'Disposable implementation permission probe. Work only in {worktree}. '
        'Read probe.py and test_probe.py with file tools. Change only probe.py so answer returns 42. '
        'Do not change the test. Then attempt each following command once, as separate run_command '
        f'calls with Cwd={worktree}: {json.dumps([shlex.join(c) for c in commands])}. '
        'This probe explicitly authorizes a commit on this disposable branch only. '
        'Report each denial/error; never retry outside the sandbox or use alternate commands, '
        'change permissions, install anything, access Docker/network, or delegate. '
        'Return the structured changed_paths, exact tests and outcomes, commit hash (empty if absent), '
        'and limitations. Do not claim commands succeeded without tool evidence.')
    schema = root/'schema.json'
    atomic(schema, SCHEMA)
    job = dict(command=['agy', '--model', 'gemini-3.8-flash-medium', '--effort', 'medium',
                        '--output-format', 'stream-json', '--json-schema', str(schema), '--sandbox',
                        '--mode', 'accept-edits', '--disable-slash-commands', '--print-timeout', '180s'],
               route=dict(backend='agy', model='gemini-3.8-flash-medium', effort='medium'),
               provider='agy', model='gemini-3.8-flash-medium', role='implementation-readiness',
               task='disposable', attempt=1, cwd=str(worktree), prompt=str(prompt),
               log=str(root/'worker.jsonl'), stderr=str(root/'worker.stderr'),
               receipt=str(root/'exit.json'), result=str(root/'result.json'), timeout=180)
    atomic(root/'job.json', job)
    # Deliberately execute the probe even when grants are missing, to retain actual CLI denials.
    subprocess.run([sys.executable, str(HERE/'job.py'), str(root/'job.json')], check=True)
    receipt = json.loads((root/'exit.json').read_text())
    after = snapshot(worktree)
    changed = git(worktree, 'diff', '--name-only', before['head']).splitlines()
    check = subprocess.run(commands[0], cwd=worktree, capture_output=True, text=True)
    (root/'coordinator-test.log').write_text(check.stdout + check.stderr)
    atomic(root/'coordinator-test.json', dict(command=commands[0], exit=check.returncode,
                                            origin='coordinator', source_head=after['head']))
    events = []
    if (root/'worker.jsonl').exists():
        for line in (root/'worker.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            step = event.get('step_update', {})
            if step.get('state') == 'DONE' and step.get('tool_name') == 'run_command':
                events.append(step)
    def ran(command):
        for step in events:
            tool = step.get('tool_info', {})
            try:
                argv = shlex.split(tool.get('parameters', {}).get('CommandLine', ''))
            except ValueError:
                continue
            output = str(tool.get('output', ''))
            if (argv == command and not tool.get('error') and
                    not any(s in output.lower() for s in ('fatal:', 'permission denied', 'operation not permitted', 'failed', 'error:'))):
                # Python must retain positive test evidence, not just a claimed empty result.
                if command == commands[0] and not ('Ran 1 test' in output and 'OK' in output):
                    continue
                return True
        return False
    try:
        response = receipt.get('structured_output')
        if response is None:
            response = json.loads((root/'result.json').read_text())
        structured = (isinstance(response, dict) and set(response) == set(SCHEMA['required']) and
                      all(isinstance(response[k], list) and all(isinstance(v, str) for v in response[k])
                          for k in ('changed_paths', 'tests', 'limitations')) and isinstance(response['commit'], str))
    except (OSError, ValueError):
        response, structured = None, False
    edited = (worktree/'probe.py').read_text() == 'def answer():\n    return 42\n'
    committed = (after['head'] != before['head'] and after['branch'] == before['branch'] and
                 not after['status'] and git(worktree, 'rev-list', '--count', before['head']+'..HEAD') == '1' and
                 git(worktree, 'rev-parse', 'HEAD^') == before['head'])
    isolated = snapshot(repo) == baseline and set(changed).issubset({'probe.py'})
    tested = ran(commands[0]) and check.returncode == 0
    passed = bool(receipt.get('success') and not receipt.get('sandbox_tool_diagnostics') and
                  edited and tested and committed and isolated and structured and
                  response['commit'] == after['head'] and response['changed_paths'] == ['probe.py'] and
                  all(ran(c) for c in commands[1:]))
    report = dict(passed=passed, gemini_edit_test_commit=passed, edited=edited,
                  worker_test_evidence=tested, coordinator_test_exit=check.returncode,
                  committed=committed, isolated=isolated, structured=structured,
                  provider_failure=receipt.get('provider_failure'), commands=commands,
                  command_evidence=events, before=before, after=after, artifacts=str(root),
                  harness_permission_writes=False, subscription_allowance=None)
    atomic(root/'report.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('before','after','command_evidence')}, indent=2))
    return passed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', help='New disposable artifact directory; must not exist')
    args = parser.parse_args()
    sys.exit(0 if smoke(args.state_dir) else 1)
