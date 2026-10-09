"""Real Codex smoke in an isolated, disposable documentation-only repository."""
import json
import shlex
from pathlib import Path
import sys
import tempfile
from orchestrate import Coordinator, Stop, atomic, git, lock, read_graph, run


def fixture(root):
    repo = root / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'smoke-base')
    git(repo, 'config', 'user.name', 'Migration Smoke')
    git(repo, 'config', 'user.email', 'migration-smoke@example.invalid')
    docs = repo / 'docs/migration'
    (docs / 'tasks').mkdir(parents=True)
    (repo / 'NEW_SPEC.md').write_text('# Smoke spec\nOnly create the assigned Markdown document. No application code.\n')
    for name in ('NEW_SPEC.md', 'PLAN.md', 'REQUIREMENTS_MATRIX.md', 'DECISIONS.md'):
        (docs / name).write_text('# Documentation smoke\nTwo independent docs; no real migration tasks.\n')
    (repo / 'AGENTS.md').write_text('Only documentation changes are authorized. Never edit migration status, specs, Git metadata or branches.\n')
    (docs / 'STATUS.md').write_text('''# Smoke status
| Task ID | Title | Dependencies | Wave | Status | Assigned worker | Branch/commit | Review status | Verification status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [T001](tasks/T001.md) | Smoke alpha | — | 1 | ready | unassigned | | not_started | not_run |
| [T002](tasks/T002.md) | Smoke beta | — | 1 | ready | unassigned | | not_started | not_run |
''')
    config = dict(worker_model='gpt-6-luna', reviewer_model='gpt-6-sol', integration_branch='migration/smoke',
                  python=sys.executable, max_repairs=2, timeout_seconds=300, scopes={}, checks={},
                  regression_checks=[], manual_gates=[])
    for tid, word in [('T001', 'alpha'), ('T002', 'beta')]:
        filename = f'docs/smoke-{word}.md'
        expected = f'# Smoke {word}\n\nThis is the {word} orchestration smoke document.\n'
        brief = '\n\n'.join(f'## {i}. {title}\n\n{body}' for i, title, body in [
            (1, 'Task ID and title', f'{tid} Documentation smoke {word}'),
            (2, 'Objective', 'Prove isolated documentation work only.'),
            (3, 'Requirements addressed', 'Smoke spec; no application implementation.'),
            (4, 'Dependencies', 'None'),
            (5, 'Relevant files and symbols', f'Owned path: {filename}'),
            (6, 'Implementation instructions', f'Create {filename} containing exactly this text, preserving final newline:\n```markdown\n{expected}```'),
            (7, 'Expected deliverables', 'Only the one assigned Markdown file.'),
            (8, 'Acceptance criteria', f'AC1: {filename} has exactly the specified content.\n\nAC2: No other files changed.'),
            (9, 'Validation commands', 'Coordinator independently checks exact bytes and diff scope.'),
            (10, 'Non-goals', 'No application code or real migration.'),
            (11, 'Integration considerations', 'Boss owns all Git commits and merges.'),
            (12, 'Parallelization constraints', 'Other smoke document is disjoint.'),
            (13, 'Risks and unresolved assumptions', 'CLI authentication must already be installed.')]) + '\n'
        (docs / 'tasks' / f'{tid}.md').write_text(brief)
        config['scopes'][tid] = [filename]
        config['checks'][tid] = [[sys.executable, '-c', f'from pathlib import Path; assert Path({filename!r}).read_text() == {expected!r}']]
    config['regression_checks'] = [[sys.executable, '-c', 'from pathlib import Path; assert not list(Path(".").rglob("*.py")); assert not list(Path(".").rglob("*.js"))']]
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'documentation-only smoke fixture')
    return repo, config


class DocumentationCoordinator(Coordinator):
    def prompt(self, task, repair='', role='worker'):
        if role != 'worker':
            return super().prompt(task, repair, role)
        return ('Documentation-only smoke in an isolated Git worktree. Only this task is authorized. '
                'Use file tools directly to create the one assigned document. Do not investigate the environment, '
                'list directories through the terminal, or execute Python. The only terminal validation command '
                'needed is the supplied git diff --no-index --exit-code command; execute it after editing with '
                f'the run_command Cwd parameter set to {Path("/tmp").resolve()}, outside the Git checkout, so no shared metadata is needed. The coordinator independently checks exact bytes. '
                'Do not stage, commit, merge, push, change branches, edit other files, use network, or delegate. '
                'Leave changes uncommitted and report command results and any denials.\n'
                + task['text'] + '\nExact permitted validation command: ' + ' '.join(self.worker_checks(task['id'])[0]) + '\nRetained repair instructions: ' + repair)


def smoke(directory=None, routing=None):
    root = Path(directory).resolve() if directory else Path(tempfile.mkdtemp(prefix='migration-smoke-'))
    if directory:
        root.mkdir(parents=True, exist_ok=False)
    print(f'Smoke artifacts retained at {root}', flush=True)
    repo, config = fixture(root)
    if routing:
        config['reviewer_model'] = routing['reviewer']['model']
        if routing['primary']['backend'] == 'agy':
            config['worker_checks'] = {}
            for tid in config['scopes']:
                filename = config['scopes'][tid][0]
                expected = filename.replace('.md', '.expected.md')
                word = 'alpha' if tid == 'T001' else 'beta'
                (repo / expected).write_text(f'# Smoke {word}\n\nThis is the {word} orchestration smoke document.\n')
                config['worker_checks'][tid] = [['git', 'diff', '--no-index', '--exit-code', '{worktree}/' + expected, '{worktree}/' + filename]]
                config['checks'][tid].append(['git', 'diff', '--no-index', '--exit-code', expected, filename])
            for brief in (repo / 'docs/migration/tasks').glob('*.md'):
                tid = brief.stem
                brief.write_text(brief.read_text() + '\nBefore handoff execute exactly ' + ' '.join(config['worker_checks'][tid][0]) + ' and report its result. No other terminal command is needed; coordinator validates exact content.\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'require permitted worker validation')
    atomic(root / 'config.json', config)
    with lock(root / 'state'):
        c = DocumentationCoordinator(repo, root / 'state', config, max_workers=2, routing=routing)
        try:
            c.execute(read_graph(repo))
            # Resume a completed run and prove no duplicate attempts/commits.
            before = (root / 'state/state.json').read_bytes()
            c.execute(read_graph(repo))
            assert before == (root / 'state/state.json').read_bytes()
            state = c.state
            assert all(r['phase'] == 'merged' for r in state['records'].values())
            receipts = []
            for tid, r in state['records'].items():
                receipts.append(json.loads(c.artifact(tid, 'worker-1-exit.json').read_text()))
                assert r['attempt'] == 1
                assert r['review']['approved']
                assert all(p.startswith('docs/') for p in c.changed(tid))
                if r.get('worker_provider') == 'agy':
                    events = [json.loads(line) for line in c.artifact(tid, 'worker-1.jsonl').read_text().splitlines()]
                    command = c.worker_checks(tid)[0]
                    tool = [e.get('step_update', {}) for e in events if e.get('event') == 'step_update']
                    assert any(s.get('state') == 'DONE' and s.get('tool_name') == 'run_command' and
                               shlex.split(s.get('tool_info', {}).get('parameters', {}).get('CommandLine', '')) == command and
                               not s.get('tool_info', {}).get('error') and
                               'fatal:' not in s.get('tool_info', {}).get('output', '').lower()
                               for s in tool), 'No verified permitted Gemini validation tool execution'
                    assert not receipts[-1].get('sandbox_tool_diagnostics'), 'Worker sandbox validation failed'
            assert max(x['started'] for x in receipts) < min(x['finished'] for x in receipts), 'Workers did not overlap'
            assert git(repo, 'branch', '--show-current') == 'smoke-base'
            assert git(repo, 'rev-parse', 'HEAD') == state['baseline']
            atomic(root / 'report.json', dict(passed=True, concurrent=True, resumed_without_duplicates=True,
                                            providers=[r.get('worker_provider', 'codex') for r in state['records'].values()],
                                            gemini_completed=any(r.get('worker_provider') == 'agy' for r in state['records'].values()),
                                            state=str(c.file)))
            print('PASS: concurrent subscription docs workers, independent Sol reviews, exact-content checks, sequential merges, integration checks, verified STATUS, idempotent resume.', flush=True)
        except Exception as e:
            if c.state:
                c.state['blocked'] = str(e)
                c.save()
            atomic(root / 'report.json', dict(passed=False, error=str(e), state=str(c.file)))
            raise
