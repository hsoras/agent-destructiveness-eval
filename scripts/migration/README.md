# Migration coordinator

Orchestration infrastructure. Historical preflight reports describe the pre-start state; persisted execution state is authoritative for current progress. Requires Python 3.11+, Git, and the installed authenticated Codex and Antigravity (`agy`) CLIs (tested with 0.162.0). No API keys or Python packages are needed for orchestration. Actual migration checks separately require the project's development environment, pinned runtime and Docker where specified.

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

The smoke test runs **real** concurrent Gemini 3.8 Flash Medium and GPT-6 Luna Medium workers, then independent read-only GPT-6.1 Sol Medium reviews. An eligible Gemini infrastructure failure can instead continue on Luna under the same gates. It checks exact document content and ownership, commits the changes itself, merges sequentially, checks integration, updates only the temporary status file, and resumes a completed run to prove idempotence. It asserts overlapping worker execution and preserves the original temporary baseline. All temporary repositories, worktrees and evidence are retained; their path is printed. `--state-dir /absolute/new/path` chooses the smoke artifact root (must not already exist). Smoke never uses the application's task graph or integration branch. Codex model calls use the existing account and its usage allowance.

Before actual execution, review and commit `scripts/migration/` together with all approved planning inputs into the agreed baseline. Existing task-named branches outside recorded state (such as `agent/T001`) make initial execution stop for ownership reconciliation; they are never deleted or imported as completed work automatically. The coordinator requires a clean source checkout and starts the dedicated `migration/artifact-upload` branch from its recorded local HEAD. It never fetches, pushes, switches the user's checkout, or merges into main. The default run directory is `<git-common-dir>/migration-orchestrator`, with logs and receipts beneath it. The default integration and worker worktrees live in the persistent sibling directory `../.<repository-name>-migration-worktrees/`, outside the shared protected `.git` directory. An external `--state-dir` keeps worktrees beneath that directory instead. Keep that directory: `state.json`, logs, exit receipts, prompts, reviews, check logs and Git worktrees are the recovery record. `--state-dir` and `--config` must be supplied consistently on every command when using non-default locations.

`config.json` contains exact file ownership and argv validation checks for all 18 tasks, extracted from the approved briefs. It explicitly excludes T001's contract files from T008's runtime directory scope. Prose-only “evidence fixtures” have no implied directory ownership: add exact intended paths before approval if needed. Commands execute without a shell. `{python}` resolves to the configured interpreter; `{repo}` in the interpreter resolves to the source repository. `$USER` in the planning examples becomes a unique check ID, so output paths do not collide. `{output}` is also available for a unique evidence directory. Change configuration before starting a run; a changed config/spec/task/plan causes resumed runs to stop. Do not silently broaden ownership or edit pinned run state to make a check pass.

Task graph and waves come from `docs/migration/STATUS.md`, with dependencies checked against every task brief. Only prerequisite commits verified as merged in Git unlock tasks. One coding worker runs by default (`--max-workers 2` explicitly permits two), only in the earliest unfinished wave, with non-overlapping ownership. Both worktrees start at the latest local integration commit. Coding workers receive the entire task (including numbered acceptance criteria), whole referenced top-level spec sections, exact task-owned matrix rows and repository instructions. All-section audits or unrecognized context formats fall back to the full spec. Complete spec/matrix files remain authoritative and available for cross-reference checks. Nested instructions must be checked before editing directory-owned descendants. The configured absolute Python interpreter avoids repeated missing-worktree-venv investigation. Workers use workspace-write with noninteractive approvals disabled; Git management is coordinator-only. Reviewers use read-only and must cover every acceptance criterion with evidence. The coordinator owns commits, so workers leave changes uncommitted.

A successful worker exit is insufficient. The coordinator checks scope, rejects symlinks and unexpected branch/HEAD changes, runs required checks, and validates the review's complete criterion coverage. It checks branch cleanliness and immutability before merging, then runs the task checks plus regression tests on integration. Only then does it commit the corresponding `STATUS.md` update on the integration branch. The original checkout's status document stays unchanged until that integration branch is deliberately brought into it. After a wave it reruns cumulative task checks and regressions before launching the next wave. Missing checks, nonzero exits, timeout, pytest skips/xfails/xpasses and zero-test results block progress. Non-pytest tools may not advertise incomplete coverage consistently; Sol must inspect their actual evidence and reject missing acceptance.

T001 and T002 also stop after independent approval, before merging, for the plan's unresolved interface/instance decisions. Inspect the reviewed branch, evidence and decisions, then explicitly ratify that exact commit:

```sh
python3 scripts/migration/orchestrate.py approve-gate --task T001 --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

For rejected or incomplete unmerged work, supply concrete findings. Repairs reuse the worktree and preserve earlier commits, with at most two repair attempts. Every repair reruns verification and receives a fresh independent review; gate approvals do not carry to a changed commit.

A rejected task stays recorded for targeted repair while the coordinator finishes other already-recorded work in that same wave, including completed independent reviews and eligible integrations. It stops when no safe same-wave work remains, saves each remaining task and its rejection findings as blockers, and makes no success claim. Later waves remain gated on the full prior-wave checkpoint.

When an exhausted, rejected task has new review evidence, the supervisor may record one or two additional attempts at a time with `authorize-repairs --task T001 --additional-attempts 1 --reason 'Concrete new finding and repair hypothesis' --approved-execution`. The coordinator appends the reason and new limit to the run state under the lock, with a cumulative ceiling of six additional attempts. It changes neither the frozen run configuration nor task/spec input digests, review requirements, or commit-specific gates. If the user's standing unattended authorization applies, add `--execution-guidance routine-decisions` to that command; future worker prompts then permit routine G02/G03 decisions while retaining the pinned runtime, safety and verification boundaries. After authorization, use the ordinary `repair --task T001 --findings 'Exact targeted instruction' --approved-execution` and `resume --approved-execution` sequence.

There is one additional, narrowly verified authorization case: an exhausted `worker_finished` attempt may receive repair budget when its explicit terminal worker receipt proves successful completion (with explicit matching attempt, integer exit 0, and exact retained worker-N receipt, log and result identities) and the exact blocker identifies one retained, coordinator-origin failed mandatory branch check. The check may be an exit-2 failure with no parser error, or an exit-1 failure from the exact frozen pytest command when retained JUnit and log evidence show ordinary testcase failures, matching counts, no collection errors or skips, and the coordinator's exact unapproved-failure classification. The coordinator rechecks the current job/attempt, ordered finite runner and child process start times bound to the launch interval, live/uncertain process absence, frozen check command, branch snapshot, log and artifact hashes under its lock before recording an ordinary bounded authorization. Pytest authorizations retain the JUnit hash and every failed testcase identity. It leaves the phase and blocker intact; use the normal targeted `repair` and `resume` commands afterward, which rerun all checks and require fresh independent review. Any failure or refusal leaves state bytes completely unchanged. This path does not apply to provider failures, reviewer failures, uncertain processes, or other check errors. `retry-checks` remains for fixing a local prerequisite when the existing implementation is already sufficient; it does not authorize another coding attempt.

For this same terminal branch-check case, ordinary `repair` validates the complete retained authorization budget/task/attempt/consumption history, then admits retained dirty worker files only when exactly one current-attempt, unconsumed grant contains the matching authenticated terminal-check proof. Consumed grants remain immutable history and are not candidates for current proof matching. Under the coordinator lock it reauthenticates the current job, attempt, receipt, blocker, failed-check and artifact manifest, snapshot and process liveness, then requires the worker branch and HEAD to match, an empty index, and every changed path to be a regular in-worktree file inside the task's owned scope and outside protected or excluded paths. Stale evidence, changed refs, staged files, or unclassified and out-of-scope changes refuse before repair state is written. Admitted source bytes and index are preserved for the next ordinary worker attempt; repair does not commit, reset, discard changes, or mark checks/review passed. All other dirty worktrees remain refused.

```sh
python3 scripts/migration/orchestrate.py repair --task T003 \
  --findings 'Implement the missing AC2 transaction rollback and add the regression case.' \
  --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

When the user explicitly restores a verified primary provider (such as Gemini after a verified worker fix), coding repairs historically retain their earlier route (e.g., Luna fallback). To select the currently validated routing primary for the NEXT coding repair only without modifying frozen configuration, pass `--select-validated-primary` (or `--select-primary`) along with concrete reason or reviewed smoke artifact provenance (`--reason` or `--provenance`):

```sh
python3 scripts/migration/orchestrate.py repair --task T003 \
  --findings 'Implement the missing AC2 transaction rollback and add the regression case.' \
  --select-validated-primary \
  --provenance 'Verified integrated smoke artifact: .scenario/migration-readiness/smoke-summary.json' \
  --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

This records the selection and previous route in append-only repair and route history. The default behavior continues to preserve historical routes, and genuine failure-only fallback remains active.

After fixing a missing **local prerequisite** or investigating a transient test/review-output error, allow the existing verification stage to run again:

```sh
python3 scripts/migration/orchestrate.py retry-checks --reason 'Inspected invalid reviewer output; corrected the local prerequisite.' --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

`retry-checks` never marks a test passed and never clears an uncertain live process or merge. An integration failure retains the merge, blocks dependents, and leaves the task short of `merged` in status until its checks pass. An integrated code defect needs a manually reviewed owner-scoped fix or revert and state reconciliation; it is not automatically rewritten. A failed final wave checkpoint leaves already individually verified rows merged but blocks all future waves until the checkpoint actually passes.

Detached runners persist exits even when the coordinator is interrupted. Resume waits on recorded jobs and processes existing receipts; it does not duplicate them. On resume, already terminal recorded reviewer receipts are collected and authenticated using the shared terminal reviewer authentication path (binding exact retained current review job, receipt and result identities, task, role, current review attempt, provider, model, worktree cwd, and candidate commit, with typed exit zero, supported terminal evidence, confinement, and no failure diagnostics) before scheduling or verifying peer workers, ensuring that completed reviews are recorded even if another peer worker authentically fails. Never waiting on active reviewers in early collection, duplicating jobs, or integrating unchecked candidates ensures peer blockers and records remain safe. Crashes between Git operations and their receipts can leave uncertain state. The coordinator recognizes its exact completed merge/status commit where possible and otherwise stops for manual inspection. Unresolved merge conflicts, uncertain process launch, runner death without receipt, dirty worktrees and external branch changes retain all work and require reconciliation. Check both recorded runner and child PIDs before requesting repair. For an interrupted launch with no recorded PID, `repair --confirmed-stopped` requires the operator to first verify that no corresponding runner or child remains; it does not bypass checks on known live processes. A clean completed pending merge can be reconciled with `reconcile --task Txxx --approved-execution`; it must have the recorded parents and the exact automatic merge tree, durably persists verified recovery state and clears the blocker. Changed conflict resolutions still require manual independent review. Never delete state to “start fresh” over assigned work. There are no resets, force pushes, automatic merge aborts, branch deletion or automatic conflict selection.

Destructive schema/data changes, production actions, paid experiment pilots and merging into main are excluded from worker authorization. If a task needs any of them, stop and obtain separate approval. Sandboxing and independent checks reduce mistakes; they are not a security boundary against deliberately malicious repository code. The coordinator runs approved test commands locally, so review those commands and dependencies before execution. It does not provision environments, install dependencies automatically or bypass failed required checks.

Run deterministic coordinator safety tests (real Git, simulated Codex, no model calls):

```sh
python3 -m unittest discover -s scripts/migration -p 'test_*.py' -v
```

The completed pre-execution setup, exact environment preparation commands, test receipts and remaining baseline failures are documented in [READINESS.md](READINESS.md). Use `uv sync --locked --extra dev --group migration-regression --inexact`, then `.venv/bin/python scripts/migration/prepare_regression.py` to prepare the local patched regression package without changing vendored runtime sources. Generated backend `node_modules` is ignored; manifests and lockfiles remain tracked. T001 alone owns shared Python manifest/lockfile changes.

The accepted legacy regression exception is documented in [BASELINE_EXCEPTIONS.md](../../docs/migration/BASELINE_EXCEPTIONS.md). Pytest checks produce JUnit XML and durable JSON/log receipts. Only the exact configured full-suite command can accept the pinned guest-name assertion; receipts retain the failing test and raw exit 1. Changed failure signatures or scanned source hashes, extra failures, errors, skips, or missing reports stop integration. Do not regenerate or refresh this baseline automatically.


## Usage and supervision

`python3 scripts/migration/orchestrate.py usage` reads historical JSONL logs without changing state. It reports per-task/per-role aggregates and per-attempt agent thread IDs, reported tokens, commands, output bytes, duration and coverage. Missing usage is unknown. Cached input and reasoning output are subsets; do not add them to input/output totals. These records do not expose Plus allowance charges or Boss/investigator usage outside the recorded jobs. New runner exit receipts also contain usage when available; accounting errors never prevent a recovery receipt.

The coordinator already runs without a Boss model in its scheduling/monitoring loop. `python3 scripts/migration/orchestrate.py watch --wait-seconds 1800` waits read-only for a meaningful state change or returns the existing blocker immediately. It polls the atomic state file in Python at one-second intervals, with zero model calls. It does not start or resume execution, acquire the coordinator lock, or modify state. Run one coordinator and let it finish; use event waits at decision boundaries, not repeated model-based status checks. A timeout is informational.

Runtime concurrency is deliberately outside frozen configuration/input digests, so an existing run can adopt the default of one without rewriting its state. Existing live workers drain normally; no worker is terminated or relaunched to change concurrency. The explicit documentation-only real `smoke` command retains two workers to test concurrency and **consumes model allowance**; use the simulated safety suite for routine infrastructure verification.

Reviewers receive cumulative diff instructions, exact changed paths, coordinator receipts, prior findings and an untrusted worker handoff. Every AC still requires independent verification. Read evidence before rerunning tests; rerun when a concrete verification gap requires it. Full branch checks, integration regressions and cumulative wave checkpoints remain mandatory.

Repairs retain a history of findings and prior reviews. Repeating an identical plan or extension reason is rejected; supply new evidence and a consolidated hypothesis. A failed reviewer output gets at most one explicit, reasoned retry per unchanged commit through `retry-checks`. Code repairs retain the existing frozen budget and cumulative extension ceiling. These checks cannot determine whether differently worded evidence is genuinely new; Boss must assess that. Do not use extra workers as a substitute for resolving task-scope ambiguity.

An ordinary clean `worker_finished` repair remains available while the configured or authorized attempt budget remains, without terminal-check authorization. Retained uncommitted worker changes use a separate admission path: they require exactly one current, unconsumed terminal-check grant with matching task and attempt, a coherent bounded authorization history, and unchanged retained proof, source snapshot, branch/HEAD, empty index, owned paths, and stopped-process evidence. Refusals preserve state and worker bytes for inspection.

See [OPTIMIZATION.md](OPTIMIZATION.md) for the measured audit, validation and remaining resumption blockers.


## Subscription provider routing (infrastructure update, migration remains paused)

`providers.json` is a runtime policy separate from the frozen migration `config.json`.
Pass `--providers-config /absolute/policy.json` consistently. Existing state/config/input
hashes remain enforced; no existing record is rewritten to adopt routing. Every new
job persists its provider, model, effort, argv, permissions preflight, and before-worktree
snapshot. Historical jobs keep their original commands. Boss stays GPT-6.1 Sol Medium;
reviewers default to GPT-6.1 Sol Medium. Sol High is reserved for a justified, bounded
investigation. Fast modes are not enabled.

The default primary is `agy --model gemini-3.8-flash-medium --effort medium`, fallback
is `codex exec -m gpt-6-luna` with medium reasoning. CLI execution uses the account's
cached subscription login. No API keys, Vertex projects, API clients, purchases, or
account/settings changes are made by these scripts. If login expires, authenticate
interactively with `agy` outside orchestration; the readiness command never logs in
or changes credentials. Account entitlement/remaining allowance is unavailable to
the orchestrator; it does not independently certify billing-plan identity.

```sh
# Model/executable check (read-only; requires agy's local listener to be permitted).
python3 scripts/migration/orchestrate.py readiness
# Model plus harmless headless authentication check; new disposable repo, retained logs.
python3 scripts/migration/orchestrate.py readiness --auth-smoke
# Mixed-provider documentation-only smoke; never uses the application's graph or state.
python3 scripts/migration/orchestrate.py smoke
# Compact task status and provider/model/role/task usage aggregates.
python3 scripts/migration/orchestrate.py status
python3 scripts/migration/orchestrate.py usage
# Detailed per-attempt records and original logs on disk.
python3 scripts/migration/orchestrate.py usage --full
# Wait through intermediate phases until a terminal decision/completion or bounded timeout.
python3 scripts/migration/orchestrate.py watch --wait-seconds 1800
```

Use `--state-dir /absolute/new/path` for readiness/smoke artifact retention (path must
not exist). To inspect smoke usage, supply its `/state` subdirectory to `usage`.
The real CLI needs to create its own localhost listener and CLI logs; an outer sandbox
that prohibits those must authorize the **disposable smoke or read-only readiness**,
while keeping `agy --sandbox` on for agent terminal commands.

### Permissions and failure handling

Gemini runs in the same isolated task worktree with `-p`, explicit model/effort,
`--output-format stream-json`, `--sandbox`, `--mode accept-edits`, and
`--disable-slash-commands`. It never uses permission-bypass flags. Existing CLI
settings are inherited. Before implementation, checked routes inspect the CLI's
**effective project, shared, and global rules** using the zero-agent-turn
`agy -p /permissions --output-format json` command. A bounded conservative
literal-token check covers the task's worker validation commands. Missing/uncertain
grants, Ask/Deny conflicts, malformed/incomplete effective policies, or unrestricted
command/file grants make Gemini unavailable. Regex grants are not inferred. An
alternate `permissions_settings` JSON file is rejected: inspecting it cannot make
the CLI load it. No global/shared/project settings are rewritten.

For new configurations, `worker_checks` can name focused developer commands;
otherwise required branch/public checks supply the permission preflight. Keep
Docker, network and privileged integration checks at the trusted coordinator.
Coordinator validation remains mandatory regardless of worker checks or worker
sandbox errors. Do not rewrite an active frozen configuration to adopt new checks.
The preflight is a conservative command-policy check, not proof that a sandboxed
command can reach its executable, dependencies, or linked Git metadata.

Authentication and the documentation smoke do not prove implementation readiness.
The real implementation probe, `python3 scripts/migration/worker_smoke.py`, uses a
new disposable repository/worktree and requires genuine editing, worker test tool
evidence, a clean Git commit, structured output, and independent validation. It
consumes account allowance and exits 1 on incomplete evidence. **The 2026-10-09
implementation probe failed**: Python was soft-denied and a separate Git probe could
not reach shared worktree metadata. See [GEMINI_WORKER_DIAGNOSIS.md](GEMINI_WORKER_DIAGNOSIS.md)
for exact evidence, limitations, and the operator-only integration/reload procedure.
Do not describe Gemini implementation as fixed until that real probe passes.

stdout JSONL, stderr, final response, terminal status, errors, denied actions,
usage, exit receipt, runner/child PIDs, and preflight output are retained under
`tasks/<task>/`. A denial can accompany `SUCCESS` and exit 0; it still blocks a
successful handoff. Authentication, quota, model, initialization, provider, and
permission errors may trigger fallback. Acceptance failures, incorrect/incomplete
implementation, unknown tool errors, and review rejection use the existing repair
process, without automatic provider switching.

Fallback is sequential, once per coding attempt, with a separate `worker-N-fallback`
job/receipt. All tracked and untracked files (including ignored artifacts), modes,
symlinks, staged diff, HEAD and branch are compared before handover. An unchanged
worktree gets a persisted continuation plan automatically. Changed bytes/Git state
are preserved with before/after manifests and a binary patch; the task stops in
`provider_blocked`. No reset, checkout, cleanup, rebase, or overwrite is performed.
The supervisor must inspect provenance and establish exact remaining work:

```sh
# ONLY after separate migration authorization and inspection of retained partial files:
python3 scripts/migration/orchestrate.py continue-fallback --task Txxx \
  --findings 'Exact retained changes, safe continuation strategy, remaining work and validation.' \
  --approved-execution
python3 scripts/migration/orchestrate.py resume --approved-execution
```

The continuation snapshot must still match at launch. Unexpected worker branch/HEAD
changes require manual reconciliation. An interruption with uncertain launch stays
blocked; it never duplicates a process. Both providers failing leaves receipts/files
and stops without claiming completion. Fallback preserves repair budgets, coordinator
commits, independent complete-criterion review, manual ratification, scope checks,
integration regressions and baseline exceptions.

One worker is the default. `--max-workers 2` permits up to two disjoint ready tasks;
with `mixed_parallel` true, a second coding task uses Luna while Gemini is active.
Recorded same-task attempts cannot overlap. Ownership/dependency/wave rules remain
unchanged, and fresh tasks do not launch around occupied rejected work.

### Verification interface and retained evidence

Each coordinator check gets `MIGRATION_ARTIFACT_DIR`, `MIGRATION_RUN_ID`, and
`MIGRATION_CHECK_ORIGIN=coordinator`. Tests/tools must save raw results into that
directory **before temporary fixtures are torn down**. `{output}` resolves there.
stdout/stderr, exit code, original command, executed argv, JUnit when applicable,
origin, working directory, source HEAD, source snapshot digest, and non-symlink
artifact paths/lengths/SHA-256 are recorded automatically in the check receipt.
Deleted internal test artifacts cannot be reconstructed; the test must adopt this
interface. Hashes prove recorded bytes/provenance, not authenticity of a historical
upstream release. Review must still verify authentic source/checkout lineage.

`public_checks: {"Txxx": [["{python}", "public_cli.py", "--help"], ...]}` adds
mandatory entry-point/contract checks before review, at integration and wave gates.
Include meaningful invalid inputs, valid alternative lineage, and forged-lineage
negatives; `--help` alone is insufficient. `required_artifacts: {"Txxx": ["control.json"]}`
requires nonempty retained artifacts on each task-labelled check. These declarations
belong in an approved **new** run configuration, not silently in existing frozen state.
Existing T002 must implement its public-contract/lineage tests and save control evidence
in its separately authorized repair; this infrastructure change neither fixes application
code nor waives any T002 criterion. Reviewers must reconcile worker sandbox denials
against exact coordinator receipts and cannot invent a coordinator pass.

`status` and `watch` project bounded fields/findings rather than dumping the full
state/logs. `watch` uses monotonic deadlines and deterministic file polling, without
LLM calls, writes or coordinator locks. Worker starts/completions, commits, review
starts and checkpoints alone do not wake Boss; merged tasks, rejection, blockers,
integration failure and all-recorded-task completion do. Full findings stay in state.json.

Branch freshness is displayed as `stale_base`. No automatic refresh/rebase is added:
merging a new baseline into old task branches would require separating ownership
from inherited peer changes, invalidating old review/evidence and recovering conflicts
without losing accepted source provenance. Preserve old commits and use verified
cross-task evidence with exact revisions until a separately reviewed refresh design
exists. Sequential integration checks still validate each candidate on the current head.

Token reports group by provider, model, role, task and attempt. Google terminal
cumulative usage is counted once, without adding per-step usage again. Missing counters
and subscription allowance remain unknown. Google input/cache inclusion semantics are
not assumed, so an uncached-input estimate is emitted only for Codex. No cross-provider
cost or allowance equivalence is inferred. Boss/investigator sessions outside these
receipts remain unmeasured.

### Resuming the paused migration (separate authorization required)

T001 and T003 are integrated; T002 candidate `56b8bfa7d7754b1709c15eb483bca973e402e4e7`
remains rejected at attempt 5. The unassigned final brief is
`/private/tmp/migration-t002-final-release-repair.md`. Infrastructure verification does
not clear that rejection or the user's pause. After separate user authorization,
inspect the retained brief and budget, then record the exact targeted repair:

```sh
python3 scripts/migration/orchestrate.py repair --task T002 \
  --findings 'Read /private/tmp/migration-t002-final-release-repair.md in full. Preserve the rejected candidate and authentic evidence; implement only this consolidated brief and all required public CLI, negative lineage and retained-control tests.' \
  --approved-execution
python3 scripts/migration/orchestrate.py resume --max-workers 2 --approved-execution
```

These commands are documentation only and were not executed for this infrastructure task.


Gemini scoped execution passed a genuine disposable edit/unittest/pytest/make,
protected-path, Docker guest, coordinator-commit and independent Sol review smoke.
See [the verified diagnosis, receipts and safe reload procedure](GEMINI_WORKER_DIAGNOSIS.md).
Use `providers-gemini-scoped.json` only by an explicit operator rollout; the existing
provider/runtime configuration is unchanged. Run the coordinator in an authorized
host environment while `agy --sandbox` protects workers. Shared Git and privileged
integration checks remain coordinator-owned; Luna fallback and independent review
remain mandatory. Docker worker access is only through the bounded broker with an
explicitly prepared isolated guest and approved fixed context/recipe.
