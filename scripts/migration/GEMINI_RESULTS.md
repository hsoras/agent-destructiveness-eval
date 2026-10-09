# Gemini orchestration infrastructure verification — 2026-10-09

Actual migration remains paused. T001/T003 integration history, T002 rejected
candidate/attempt 5, final unassigned repair brief and application/spec/runtime pins
were preserved. No application implementation, migration worker, main merge,
deployment, separate billable model API, API-key setup, login or global CLI setting
change was performed.

## Readiness and real CLI evidence

Installed executable: `/Users/sarosh/.local/bin/agy`; its language server reports
version 1.3.2. `agy models` exposed `gemini-3.8-flash-medium` after permitting the
CLI's localhost listener outside the outer sandbox. Agent terminal commands retain
`--sandbox`. Existing cached Google account authentication passed a harmless READY
prompt, with no tool calls or repository edits:
`/private/tmp/gemini-auth-readiness-20261009/exit.json` and `auth.jsonl`.
Account subscription entitlement/remaining allowance was not exposed and is not inferred.

The first documentation probe created its document, then soft-denied Python validation
with SUCCESS/exit 0. Logs: `/private/tmp/gemini-permission-probe/`. This exposed the
need to inspect structured `denied_actions` and tool errors independently of process exit.
A mixed smoke retained its partial edits and stopped without Luna handover:
`/private/tmp/gemini-mixed-smoke-20261009/`.

The second mixed smoke encountered an unavailable directory-listing permission before
changes. It preserved a byte-identical snapshot, recorded a continuation plan, launched
Luna sequentially, passed independent GPT-6.1 Sol Medium review and integration, and
resumed without duplicate attempts:
`/private/tmp/gemini-mixed-smoke-20261009-r2/report.json`. Both final workers were Luna;
this is successful fallback evidence, not successful Gemini implementation evidence.

An earlier mixed smoke passed exact-content/coordinator gates with one Gemini/one Luna,
but its worker Git validation reported inaccessible shared worktree Git metadata:
`/private/tmp/gemini-mixed-smoke-20261009-r3/`. A subsequent no-index/check experiment
also failed its independent coordinator command gate (Git returned 1 for a difference):
`/private/tmp/gemini-mixed-smoke-20261009-r4/`. These runs do not establish successful
worker validation. Their diagnostics and files remain intact.

A focused probe then proved the permitted command
`git diff --no-index --exit-code <expected-document> <document>` with its working
directory outside the Git checkout. Gemini created a disposable worktree document;
the raw tool event completed without an error. Its Cwd and numeric tool exit status
are not exposed in the raw event; the agent reported exit 0, which alone is not an
independent pass:
`/private/tmp/gemini-cwd-validation/probe.jsonl`. No shared Git metadata was accessed.

A fifth mixed smoke passed coordinator checks/reviews/integration but its worker comparison
used a temporary parent directory not mounted by agy; the final actual-tool assertion
correctly rejected that smoke: `/private/tmp/gemini-mixed-smoke-20261009-r5/`.

The final mixed smoke at `/private/tmp/gemini-mixed-smoke-20261009-r6/` uses the
explicit `/private/tmp` working directory proven by the focused probe, and that
same validation, asserts its actual Gemini tool event, rejects sandbox diagnostics,
and independently runs exact-content and comparison checks. It uses isolated task
worktrees, coordinator-owned commits, independent Sol reviews, sequential merges,
integration/wave checks and idempotent resume. Final completion is recorded in
that directory's `report.json`; full provider/model/task/role usage is retained separately.

## Safety suite and independent infrastructure review

The existing baseline safety suite passed (41 tests). Provider coverage adds executable,
model, quota, authentication, provider and initialization failures; exit-zero denials;
partial and ignored-file preservation; explicit continuation; failed continuation
snapshots; both providers missing; application/quota/numeric assertion errors that
must not become outages; mixed isolation; concurrency/duplicate guards; interrupted
coordinator recovery; public-check gates; artifact capture/missing artifacts/symlink
parents; terminal-only waiting; provider/model usage; Luna repair-provider persistence;
and failed reviewer receipt recovery. Existing commit, complete-criterion independent
review, ownership, baseline-exception and integration gates remain tested.

The reviewed suite passed **68 tests in 199.753 seconds**:
`/private/tmp/gemini-infra-reviewed-tests.log`. A final run after small smoke/receipt
updates is retained at `/private/tmp/gemini-infra-release-tests.log`.

Independent read-only GPT-6.1 Sol Medium infrastructure review identified provider
error text misclassification, fallback repair-provider drift, and the failed-reviewer
receipt interruption window. Those were corrected with regression tests. Regex
permission uncertainty now fails closed, and artifact requirements reject escaping
or symlink-parent paths. Review evidence:
`/private/tmp/gemini-infra-review-result.json`; the final follow-up review is retained
at `/private/tmp/gemini-infra-review-final-result.json` and the release evidence review at
`/private/tmp/gemini-infra-release-review-result.json`. No commit is authorized until
required tests and independent review pass.

## Preservation and remaining limitations

The preservation manifest covers 353 pre-existing files, including persisted run
artifacts, the pause handoff, final T002 brief and NEW_SPEC.md. At verification all
were byte-identical, and all migration refs were unchanged. State SHA-256:
`99e1725fa637c320b78587941979d672fbe2389a9d70911bf773a7e3a227f279`.
Evidence: `/private/tmp/gemini-infra-preservation-before.json` and
`/private/tmp/gemini-infra-preservation-after.json`. Untracked supervision notes
remain user-owned and are not included in the infrastructure commit.

- Existing CLI permissions are inherited. Required literal validation command grants
  are preflighted; unsupported regex grants are conservatively unavailable. Real
  Python acceptance commands currently need narrow grants or fall back to Luna.
- Shared Git metadata can be inaccessible inside agy's terminal sandbox. Worker
  sandbox failures remain distinct from independently verified coordinator results.
  The raw CLI tool envelope does not consistently expose a numeric tool exit code;
  missing values remain unknown, while coordinator command exits are explicit.
- Tests that delete internal evidence must adopt `MIGRATION_ARTIFACT_DIR`; infrastructure
  cannot recover deleted historical evidence. T002 public CLI/negative lineage tests
  and retained control evidence still belong to its separately authorized repair.
  Frozen existing config/state were not modified to add declarations.
- Branch refresh/rebase is deliberately deferred. Old worker commits/evidence remain
  intact, freshness is visible, and current-head integration verification still gates
  completion. No fragile conflict or interrupted-refresh behavior was introduced.
- Google input/cache inclusion and subscription allowance semantics are unavailable;
  counters are reported without invented uncached totals, cost equivalence or savings.
  Boss/investigator calls outside coordinator logs are not automatically accounted.
- The coordinator uses bounded deterministic file polling, not an OS notification
  daemon or an LLM monitor. Multiple source worktrees/repos are not governed by a
  host-wide worker quota; each locked run limits itself to one or two coding workers.

Commands for readiness, mixed smoke, usage and separately authorized resumption are
in `README.md`. Infrastructure completion does not authorize migration resumption.

## Final release evidence

The final r6 mixed smoke passed with one Gemini worker and one Luna worker, independent
GPT-6.1 Sol Medium approvals, coordinator commits and integration, and duplicate-free
resume. Both coordinator document checks recorded exit 0. The matching raw Gemini
tool event recorded DONE without an error or sandbox diagnostic; its numeric tool
exit is unavailable and is not claimed as independently measured. The earlier r5
failure remains documented above. Provider usage is retained in the r6 usage.json.

The release suite passed 68 tests in 209.687 seconds. Both focused watch/wait tests
passed after renaming the ambiguous completion field to assigned_records_complete.
py_compile and git diff --check passed. Infrastructure review usage is retained by
provider/model/role/task/attempt in /private/tmp/gemini-infra-review-usage.json.

Final independent release evidence review approved with no findings:
/private/tmp/gemini-infra-release-review-result.json. The preservation check again
confirmed all 353 original files and migration refs unchanged before the infrastructure
commit. Actual migration remains paused.
