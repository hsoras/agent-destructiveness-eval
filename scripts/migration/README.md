# Migration coordinator

Infrastructure only. No real migration task has been started. Requires Python 3.11+, Git, and the installed authenticated Codex CLI (tested with 0.162.0). No API keys or Python packages are needed for orchestration. Actual migration checks separately require the project's development environment, pinned runtime and Docker where specified.

Run from the repository root:

```sh
# Safe: creates a new temporary, documentation-only Git repository.
python3 scripts/migration/orchestrate.py smoke

# Safe: inspect the existing graph and persistent execution state.
python3 scripts/migration/orchestrate.py status

# Only AFTER the user approves actual migration execution:
python3 scripts/migration/orchestrate.py run --approved-execution

# Reattach after interruption; does not relaunch recorded running attempts.
python3 scripts/migration/orchestrate.py resume --approved-execution
```

The smoke test runs **real** concurrent `gpt-6-luna` workers at medium effort, then independent read-only `gpt-6-sol` reviews at medium effort. It checks exact document content and ownership, commits the changes itself, merges sequentially, checks integration, updates only the temporary status file, and resumes a completed run to prove idempotence. It asserts overlapping worker execution and preserves the original temporary baseline. All temporary repositories, worktrees and evidence are retained; their path is printed. `--state-dir /absolute/new/path` chooses the smoke artifact root (must not already exist). Smoke never uses the application's task graph or integration branch. Codex model calls use the existing account and its usage allowance.

Before actual execution, review and commit `scripts/migration/` together with all approved planning inputs into the agreed baseline. Existing task-named branches outside recorded state (such as `agent/T001`) make initial execution stop for ownership reconciliation; they are never deleted or imported as completed work automatically. The coordinator requires a clean source checkout and starts the dedicated `migration/artifact-upload` branch from its recorded local HEAD. It never fetches, pushes, switches the user's checkout, or merges into main. The default run directory is `<git-common-dir>/migration-orchestrator`, with logs and receipts beneath it. The default integration and worker worktrees live in the persistent sibling directory `../.<repository-name>-migration-worktrees/`, outside the shared protected `.git` directory. An external `--state-dir` keeps worktrees beneath that directory instead. Keep that directory: `state.json`, logs, exit receipts, prompts, reviews, check logs and Git worktrees are the recovery record. `--state-dir` and `--config` must be supplied consistently on every command when using non-default locations.

`config.json` contains exact file ownership and argv validation checks for all 18 tasks, extracted from the approved briefs. It explicitly excludes T001's contract files from T008's runtime directory scope. Prose-only “evidence fixtures” have no implied directory ownership: add exact intended paths before approval if needed. Commands execute without a shell. `{python}` resolves to the configured interpreter; `{repo}` in the interpreter resolves to the source repository. `$USER` in the planning examples becomes a unique check ID, so output paths do not collide. `{output}` is also available for a unique evidence directory. Change configuration before starting a run; a changed config/spec/task/plan causes resumed runs to stop. Do not silently broaden ownership or edit pinned run state to make a check pass.

Task graph and waves come from `docs/migration/STATUS.md`, with dependencies checked against every task brief. Only prerequisite commits verified as merged in Git unlock tasks. At most two workers run, only in the earliest unfinished wave, with non-overlapping ownership. Both worktrees start at the latest local integration commit. Coding workers receive the entire task (including numbered acceptance criteria), authoritative spec, matrix pointers and repository instructions. Full spec context deliberately includes all relevant sections without a brittle subsection parser. Workers use workspace-write with noninteractive approvals disabled; Git management is coordinator-only. Reviewers use read-only and must cover every acceptance criterion with evidence. The coordinator owns commits, so workers leave changes uncommitted.

A successful worker exit is insufficient. The coordinator checks scope, rejects symlinks and unexpected branch/HEAD changes, runs required checks, and validates the review's complete criterion coverage. It checks branch cleanliness and immutability before merging, then runs the task checks plus regression tests on integration. Only then does it commit the corresponding `STATUS.md` update on the integration branch. The original checkout's status document stays unchanged until that integration branch is deliberately brought into it. After a wave it reruns cumulative task checks and regressions before launching the next wave. Missing checks, nonzero exits, timeout, pytest skips/xfails/xpasses and zero-test results block progress. Non-pytest tools may not advertise incomplete coverage consistently; Sol must inspect their actual evidence and reject missing acceptance.

T001 and T002 also stop after independent approval, before merging, for the plan's unresolved interface/instance decisions. Inspect the reviewed branch, evidence and decisions, then explicitly ratify that exact commit:

```sh
python3 scripts/migration/orchestrate.py approve-gate --task T001 --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

For rejected or incomplete unmerged work, supply concrete findings. Repairs reuse the worktree and preserve earlier commits, with at most two repair attempts. Every repair reruns verification and receives a fresh independent review; gate approvals do not carry to a changed commit.

```sh
python3 scripts/migration/orchestrate.py repair --task T003 \
  --findings 'Implement the missing AC2 transaction rollback and add the regression case.' \
  --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

After fixing a missing **local prerequisite** or investigating a transient test/review-output error, allow the existing verification stage to run again:

```sh
python3 scripts/migration/orchestrate.py retry-checks --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

`retry-checks` never marks a test passed and never clears an uncertain live process or merge. An integration failure retains the merge, blocks dependents, and leaves the task short of `merged` in status until its checks pass. An integrated code defect needs a manually reviewed owner-scoped fix or revert and state reconciliation; it is not automatically rewritten. A failed final wave checkpoint leaves already individually verified rows merged but blocks all future waves until the checkpoint actually passes.

Detached runners persist exits even when the coordinator is interrupted. Resume waits on recorded jobs and processes existing receipts; it does not duplicate them. Crashes between Git operations and their receipts can leave uncertain state. The coordinator recognizes its exact completed merge/status commit where possible and otherwise stops for manual inspection. Unresolved merge conflicts, uncertain process launch, runner death without receipt, dirty worktrees and external branch changes retain all work and require reconciliation. Check both recorded runner and child PIDs before requesting repair. For an interrupted launch with no recorded PID, `repair --confirmed-stopped` requires the operator to first verify that no corresponding runner or child remains; it does not bypass checks on known live processes. A clean completed pending merge can be reconciled with `reconcile --task Txxx --approved-execution`; it must have the recorded parents and the exact automatic merge tree. Changed conflict resolutions still require manual independent review. Never delete state to “start fresh” over assigned work. There are no resets, force pushes, automatic merge aborts, branch deletion or automatic conflict selection.

Destructive schema/data changes, production actions, paid experiment pilots and merging into main are excluded from worker authorization. If a task needs any of them, stop and obtain separate approval. Sandboxing and independent checks reduce mistakes; they are not a security boundary against deliberately malicious repository code. The coordinator runs approved test commands locally, so review those commands and dependencies before execution. It does not provision environments, install dependencies automatically or bypass failed required checks.

Run deterministic coordinator safety tests (real Git, simulated Codex, no model calls):

```sh
python3 -m unittest discover -s scripts/migration -p 'test_*.py' -v
```
