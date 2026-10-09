"""Real Codex smoke in an isolated, disposable documentation-only repository."""
import json
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


def smoke(directory=None):
    root = Path(directory).resolve() if directory else Path(tempfile.mkdtemp(prefix='migration-smoke-'))
    if directory:
        root.mkdir(parents=True, exist_ok=False)
    print(f'Smoke artifacts retained at {root}', flush=True)
    repo, config = fixture(root)
    atomic(root / 'config.json', config)
    with lock(root / 'state'):
        c = Coordinator(repo, root / 'state', config)
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
            assert max(x['started'] for x in receipts) < min(x['finished'] for x in receipts), 'Workers did not overlap'
            assert git(repo, 'branch', '--show-current') == 'smoke-base'
            assert git(repo, 'rev-parse', 'HEAD') == state['baseline']
            atomic(root / 'report.json', dict(passed=True, concurrent=True, resumed_without_duplicates=True, state=str(c.file)))
            print('PASS: concurrent Luna docs workers, independent Sol reviews, exact-content checks, sequential merges, integration checks, verified STATUS, idempotent resume.', flush=True)
        except Exception as e:
            if c.state:
                c.state['blocked'] = str(e)
                c.save()
            atomic(root / 'report.json', dict(passed=False, error=str(e), state=str(c.file)))
            raise
