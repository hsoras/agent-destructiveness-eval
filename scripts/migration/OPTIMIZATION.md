# Orchestration usage optimization audit

Audited 2026-10-09. This change optimizes infrastructure only. No migration worker, real smoke, paid pilot, migration resume, or merge into main was run. Current persisted state remains authoritative: T001 merged; T002 rejected at attempt 3/review 3; T003 rejected at attempt 7/review 4. Historical preflight reports are not current readiness evidence.

## Measured findings

All 26 recorded worker/reviewer JSONL logs contain a completed-turn usage record. Counts below sum those reported records, including repeated input across model turns. Cached input is a subset of input; reasoning output is a subset of output. They must not be added as extra tokens. These are not Plus allowance charges, prices, or estimates of remaining quota.

| Task | Role | Calls | Input tokens | Cached input | Uncached input | Output tokens |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| T001 | coding | 5 | 7,659,774 | 7,277,312 | 382,462 | 59,159 |
| T001 | review | 4 | 1,284,936 | 1,023,488 | 261,448 | 11,004 |
| T002 | coding | 3 | 8,376,052 | 8,077,568 | 298,484 | 55,175 |
| T002 | review | 3 | 797,927 | 633,344 | 164,583 | 7,618 |
| T003 | coding | 7 | 5,823,965 | 5,370,368 | 453,597 | 48,601 |
| T003 | review | 4 | 1,081,447 | 831,744 | 249,703 | 12,210 |
| Total | both | 26 | 25,024,101 | 23,213,824 | 1,810,277 | 193,767 |

T002 coding has the largest recorded total input. T003 coding has the largest uncached input and most attempts. Reviewers account for 3,164,310 input tokens, including 675,734 uncached input. Required independent review is valuable; redundant exploration is the optimization target.

The copied spec is 74,335 bytes and the requirements matrix is 308,710 bytes. The 26 saved prompts total 2,200,203 bytes. Logs contain 53 completed commands mentioning the matrix, with 6,616,725 bytes of combined command output; these outputs sometimes also contain other source files. Seventeen of those commands return at least 300,000 bytes. Repeated searches for a task OR generic AC IDs match unrelated task rows across the matrix. One T001 binary strings investigation returned 1,048,606 bytes despite requesting 80 lines: line-count limits alone do not bound context.

The previous Boss instructions duplicated deterministic assignments, launches, monitoring, review and integration already implemented in Python. They also incorrectly told workers to commit, contrary to coordinator ownership. The coordinator's ordinary scheduling, process monitoring, checks and Git operations already make zero model calls. Calls are confined to implementation and independent review; the optional real documentation smoke also spends allowance. Repeated Boss polling adds model use outside this Python loop.

T003 rejection history identifies successive concurrency, idempotency, persistence and audit edge cases rather than one consolidated recovery model. Its latest rejection concerns pending-delete cleanup unlinking bytes from a subsequent same-ID upload. T002's latest review rejects missing candidate comparison, alternative-repair evidence and rescue controls. T002's brief says proposed quota/rescue evidence while assigning the actual service controls to T004; its matrix adds broader validity clauses. That scope ambiguity needs a consolidated evidence-backed decision before another coding attempt. This audit does not reinterpret or weaken any criterion.

Worker logs repeatedly report missing worktree-local `.venv` and sandbox-denied localhost binding. Coordinator check receipts use the prepared absolute source interpreter and retain required HTTP checks. Repeating environment discovery or treating sandbox restrictions as code failures is avoidable.

## Implemented changes

- Default to one coding worker, with an explicit runtime `--max-workers 2` option. Existing recorded live work drains normally. Configuration and input digests remain unchanged; no state migration or history rewrite is required.
- Include the complete task, exact owner-linked matrix rows and whole referenced top-level spec sections. Ranges are expanded conservatively to their top-level parents; an unknown format, missing owner rows or all-section audit uses the full spec. Every assigned source clause remains verbatim. Full files remain available for cross-reference inspection.
- Supply the configured absolute Python interpreter, targeted development-check guidance, nested-instruction discovery and bounded source/log reading guidance. Avoid generic AC searches and repeated sandbox-prerequisite retries.
- Give reviewers cumulative diff commands, changed paths, coordinator receipts, retained prior review/repair evidence and an untrusted worker report. Reviewers independently verify every AC, all task changes and regression risks. They may rerun checks when evidence has a concrete gap. Successful coordinator checks need not be routinely rerun by a model.
- Preserve repair-plan history and prior reviews. Reject identical repeated repair plans and extension reasons. Failed reviewer output permits one explicit, reasoned retry per unchanged commit. Existing coding budgets, cumulative extension ceiling, fresh reviews after repairs and commit-specific ratification gates remain intact.
- Add read-only `watch`, which waits for phase/attempt/commit/checkpoint changes or a blocker. It uses a one-second deterministic Python file poll and a deadline, with no model calls or writes. The ordinary Python coordinator owns execution; Boss wakes for decisions rather than polling through repeated model turns. This is file-backed event supervision, not an OS notification daemon.
- Add read-only per-task/per-role and per-attempt usage accounting, including agent thread IDs, coverage, reported token categories, commands, output bytes, duration and exits. New detached runner receipts include available usage; accounting cannot prevent the durable exit receipt.
- Update Boss instructions to use one coordinator, one coding worker, independent review, retained consolidated investigations and event waits. Clarify that an infrastructure audit does not authorize execution.

## Before/after measurements and limits

These comparisons regenerate prompts read-only from the same current task records and repair instructions. Byte counts are deterministic context measurements, not tokenizer or Plus quota measurements.

| Task | Last saved worker prompt bytes | New worker prompt bytes | Before prompt + full matrix read | After prompt with exact matrix rows | Combined text reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| T001 | 85,164 | 54,697 | 393,874 | 54,697 | 86.1% |
| T002 | 85,280 | 94,260 | 393,990 | 94,260 | 76.1% |
| T003 | 84,031 | 87,043 | 392,741 | 87,043 | 77.8% |

Some new prompts are larger because required matrix evidence is now included directly. The combined comparison applies to a handoff that loaded the complete matrix, a behavior observed repeatedly in the logs; it is not a guaranteed saving on every call. No real after-change model run was performed. Actual allowance savings and latency are unmeasured. Serial scheduling limits simultaneous spending and makes intervention easier; it does not inherently reduce tokens per completed task.

Boss and investigator sessions outside persisted worker logs are not measured. The old Boss instructions establish a duplication risk, but their exact token cost and frequency cannot be inferred from these logs. The contribution of model capability, task difficulty and prompt design to rejected cycles is a hypothesis, not a controlled measurement. No model tier was downgraded.

## Recommended operation and remaining inefficiencies

Use one Luna Medium coding worker and one Sol Medium independent reviewer through Python. Invoke Sol High only for a bounded, consolidated investigation when existing evidence is insufficient. Do not launch extra Boss-managed coding/review workers. Keep task commits coordinator-owned and all Git isolation, review and integration gates enabled.

Let one authorized coordinator run until completion or a genuine blocker. Use `status` at entry, `watch` for an event, and `usage` at a decision boundary. A watch timeout does not justify model investigation or repair. If the environment limits tool waits, detach the deterministic wait or return on the coordinator's terminal event; do not schedule periodic Boss turns.

Before the next T002/T003 repair, reconcile all remaining failed clauses, retained investigations and exact candidate commits into one handoff with a root-cause hypothesis, edge-case list, intended owned changes and validation. Reuse existing commits and worktrees. Resolve T002's evidence boundary without moving criteria or waiving service controls. For T003, investigate the whole pending-event/object-lifetime model, not just the latest unlink line.

Remaining costs include the legitimately large assigned matrix scope (96 T002 rows; 118 T003 rows), full cumulative independent reviews and mandatory branch/integration/wave regressions. Those checks remain unchanged. Full matrix searches and large binary/source outputs are discouraged by prompts but not forcibly truncated by a custom CLI wrapper. Semantic novelty of differently worded repair reasons still requires supervisor judgment; string equality only blocks exact repeats. Further task subdivision or gate caching would require explicit plan changes or stronger evidence and is outside this targeted optimization.

## Verification and resumption

Baseline: 33 orchestration tests passed in 43.679 seconds before changes. Expanded tests use real temporary Git repositories and a simulated Codex CLI, never the real migration graph or workers. They cover worktree isolation, protected scope and symlinks, coordinator-owned commits, independent complete-AC review, sequential integration, integration failures, wave checkpoints, commit-specific manual gates, interrupted process/status/checkpoint recovery, coordinator locking, duplicate prevention and exact baseline-exception enforcement. Added tests cover serial scheduling and existing-intent drain, immutable-config resume under runtime concurrency changes, complete assigned matrix clauses and full-spec fallback, evidence-directed read-only review prompts, deterministic read-only watch, repair-history retention and repeated-plan rejection, bounded reasoned review retries and partial/missing/multi-turn usage records.

The approved baseline exception remains byte-identical in config and documentation. Tests reject changed signatures or scanned sources, additional failures, collection errors, skips and abnormal exits. No application acceptance suite or real smoke was run because this task is infrastructure-only.

Final validation: **41 tests passed in 65.595 seconds** using `.venv/bin/python -m unittest discover -s scripts/migration -p 'test_*.py' -v`; `py_compile` and `git diff --check` passed. Read-only `status`, `usage`, `watch` and existing-state Coordinator construction passed without resuming execution. All **291 persisted files** under `.git/migration-orchestrator/` are byte-identical to the pre-edit hash manifest. Migration refs, integration/worker worktrees and worker checkout statuses are unchanged; frozen config/spec/task/plan digests still match. The read-only existing-lock probe confirms no coordinator currently holds the lock. Final local evidence is retained in `/tmp/migration-optimization-final-tests.log`, `/tmp/migration-optimization-usage.json`, `/tmp/migration-optimization-watch.json` and `/tmp/migration-optimization-preservation.json`. Infrastructure changes are committed separately on the existing source branch, without updating the migration integration branch. Infrastructure readiness does not clear the recorded migration blocker. Resumption remains blocked on consolidated T002/T003 recovery decisions, exact reviewed repair budgets and unchanged quality gates; this audit has not authorized or performed it.


## Gemini infrastructure follow-up (2026-10-09)

Current authoritative state supersedes the historical counts above: T001 and T003
are integrated; T002 is rejected at attempt 5. Migration remains explicitly paused.
The provider adapter prefers Gemini 3.8 Flash Medium through cached-account `agy`,
with sequential Luna Medium fallback for verified infrastructure errors. Runtime
routing is separate from frozen config. Partial changes stop for an explicit continuation
plan; coding/test/review defects remain evidence-based repairs on the current provider.
Mixed disjoint tasks may use one Gemini and one Luna; the default remains serial.
Terminal-event waits suppress intermediate phase churn. Checks persist raw artifact
provenance/hashes, and public contract checks can be made mandatory in new configs.

Real probes exposed permission-denied `SUCCESS`/exit-0 behavior and retained it as
regression evidence. A compact documentation-only worker prompt avoids production
migration decision boilerplate and unnecessary environment discovery. No global account
or permission settings were changed. Branch refresh remains a documented limitation;
current stale branches are preserved. See `GEMINI_RESULTS.md` for measured verification
results, limitations and retained artifact paths. No allowance savings or cross-provider
cost equivalence is claimed.
