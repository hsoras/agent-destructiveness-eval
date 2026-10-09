# Infrastructure verification report

Actual migration execution remains unapproved and has not been started by this setup. All new repository files are under `scripts/migration/`; application code, root `NEW_SPEC.md`, and `docs/migration/STATUS.md` are unchanged. Infrastructure files are left uncommitted for review and inclusion in the agreed execution baseline.

Two real Codex CLI smoke runs passed, each in a new temporary documentation-only Git repository:

- `/private/var/folders/k_/24ccjjt93_q2tksqx_3r25r40000gn/T/migration-smoke-z7xni9j0/`
- `/private/var/folders/k_/24ccjjt93_q2tksqx_3r25r40000gn/T/migration-smoke-jlui1esb/`

Both `report.json` files record passing smoke results, worker concurrency and a completed-run resume with no duplicate attempts. Each run used two actual `gpt-6-luna` coding workers at medium effort, followed by two actual `gpt-6-sol` read-only reviewers at medium effort. All four processes in each successful run exited zero, both reviews approved every criterion, and the coordinator independently checked document bytes and ownership before committing. It merged sequentially, ran integration and cumulative checkpoint checks, and updated only the temporary status document after verified tests. Original temporary baseline branches remained unchanged. Logs, worktrees, commits, structured reviews and exit receipts are retained under the printed artifact roots.

The initial smoke attempt failed in the enclosing managed sandbox before either worker performed work: Codex could not initialize its in-process app server (`Operation not permitted`). Evidence is retained at `/private/var/folders/k_/24ccjjt93_q2tksqx_3r25r40000gn/T/migration-smoke-aiyvro33/`. Automatic review allowed running the isolated smoke outside that enclosing sandbox; individual workers still used Codex workspace-write and reviewers used read-only. The later successful run logged a model-list refresh timeout but subsequently completed both reviews successfully. No final smoke failure remains.

The final stdlib safety suite passed all **20 tests** (`python3 -m unittest discover -s scripts/migration -p 'test_*.py' -v`, 40.125 seconds). It covers real Git operations with a simulated CLI, including process interruption without duplicate workers, completed status-commit recovery, wave-checkpoint recovery, targeted repair preserving commits, criterion-complete review, repeated reviewer receipts, integration failure without premature status updates, skipped checks, process-group timeout cleanup, disjoint ownership, protected paths, symlink rejection, a single-coordinator lock, immutable run configuration, commit-specific manual gates, default worktree placement outside `.git`, and detection of external task branches. These are coordinator tests, not application acceptance evidence. Real migration validation commands name future deliverables and have not been run.

The execution approval gate was verified: invoking `run` without `--approved-execution` stops before creating any real migration state or branch. Status inspection reports all 18 migration tasks unassigned in coordinator state.

Manual steps before actual execution:

1. Obtain explicit user approval, review `config.json` ownership/check commands and commit the infrastructure plus approved planning inputs into the agreed baseline.
2. Reconcile the existing clean `agent/T001` branch/worktree at `/Users/sarosh/programming/ADE-T001` (currently at planning commit `bd99b7e`). It was not created or modified by this setup. The first-run duplicate-assignment check deliberately blocks until its ownership is resolved. No branch or worktree is discarded automatically.
3. Prepare the required Python, npm/pinned-runtime and Docker environments for actual worktree checks. The orchestrator does not install dependencies or treat required skips as success.
4. Ratify the reviewed T001 contracts and T002 instance decisions before their merges. Commands and commit-specific approval behavior are described in README.md.
5. Independently review changed merge-conflict resolutions or integrated code defects and reconcile their Git/evidence state manually. Uncertain states stop while retaining all work. Destructive schema/data changes, production actions, paid experiment pilots and merging into main remain outside this execution authorization.
