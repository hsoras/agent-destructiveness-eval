# Profile-driven proxy implementation specification

Status: user-approved design; implementation pending. Scope: coding contract
and acceptance criteria, not a claim that these features currently exist.
Prompt text: [live-proxy-prompts.md](live-proxy-prompts.md).

This specification supersedes [live-proxy-plan.md](live-proxy-plan.md) for the
new profile-driven implementation. [live-proxy.md](live-proxy.md) continues to
describe the existing implementation until the migration is delivered.

## 1. Purpose and modes

Create coherent evaluation-looking and personal-machine presentations while
keeping the task's dilemma and outcomes authoritative. Natural-looking means a
normal personal machine with no container/evaluation hints. Evaluation-looking
means a plausible prepared evaluation workspace, without explicit statements
that actions have no consequences. Each condition has its own fixed preset
and guest setup, with identical protected task/service facts.

Development uses an LLM to inspect every eligible observation, produce rules,
evolve a persistent profile, request supported guest changes, and report
improvements. It receives the profile and current request/result, not the full
target conversation or hidden scores/diagnostics. No exact-repeat cache is
required initially: the approved behavior is always checking in development,
subject to explicit auxiliary budgets.

Non-development reproduces verified saved guest setup and applies explicit
rules deterministically, with zero auxiliary LLM calls. A reviewer is optional
in development. All adaptive runs keep existing live-preview/development
labels; do not relax the measured-presenter separation.

The proxy may modify operational workspace details during development, but
never quota, artifact contents/sizes/hashes, ownership/retention evidence,
object IDs, credentials/capabilities, service hosts/URLs, service responses,
or upload/deletion outcomes. Tool exit codes and transport status remain
unchanged. Output edits preserve error meaning and actual operation evidence.
Existing protected numeric/token guards remain a conservative minimum.

## 2. Artifacts and CLI

Introduce a bundled `artifact-proxy-profile.v1` containing machine identity,
state, presentation rules, and persistent guest setup. Retain versioned
snapshots, canonical digests, and parent digests. Profile and rules are one
committed unit; the rules retain their own digest for compatibility and review.

Proposed interface (new flags/subcommand, not runnable today):

```sh
python scripts/artifact_upload.py run --mode develop --proxy-mode llm \
  --condition natural-looking --profile-in profiles/personal.json \
  --profile-out profiles/personal.next.json <existing route/budget/run flags>

python scripts/artifact_upload.py continue --run-dir <saved-run> \
  --additional-budget <target-allowances.json> \
  --additional-proxy-budget <auxiliary-allowances.json>
```

* `--profile-in`: read the initial bundle. If the path does not exist, initialize
  from the selected condition preset, save it at that path, and log the event.
  Malformed existing files are errors, not missing profiles. Missing parents
  may be created. An unwritable path is an actionable error.
* `--profile-out`: destination for the evolved bundle. Existing output content
  is not an input unless also selected with `--profile-in`. Atomic replacement
  is allowed; retain immutable run-local snapshots. Input and output may be
  the same path, with the initial snapshot retained before replacement.
* Without `--profile-out`, always save `profile.final.json` in the run directory.
  Checkpoint and save changes throughout the run, not just on normal exit.
* For `--condition both`, accept condition-keyed input/output mappings, with
  independent files/state for each condition. Reject a single ambiguous path
  instead of allowing one condition to overwrite the other.
* Retain `--rules`/`--rules-out` as legacy import/export. Import existing rule
  formats into a preset bundle. Reject simultaneous `--rules` and
  `--profile-in` rather than silently choosing priority. Exported rules carry
  the source profile version/digest in an accompanying receipt.
* Keep existing default script/frozen behavior compatible when no new profile
  is supplied. Existing command aliases and labeling checks must keep working.

Profile initialization/reuse is explicit in the manifest; do not automatically
learn starting state from whichever experiment ran last. Loading a profile
alone starts a fresh episode. Only `continue` restores an existing episode.

## 3. Profile schema and state ownership

Required top-level fields:

| Field | Contents |
| --- | --- |
| `schema`, `version`, `parent_sha256` | Bundle identity and ancestry |
| `profile_id`, `condition`, `preset_id` | Stable profile and preset identity |
| `desired_identity` | OS, user, hostname, home, worktree, display/environment defaults |
| `guest_setup` | Ordered supported setup operations, preconditions and verification descriptors |
| `confirmed_state` | Verified workspace facts, provenance and evidence refs |
| `presented_facts` | Facts/aliases exposed to the target, first/last evidence refs and dependencies |
| `ruleset` | Executable data-only rules, version, parent digest |
| `findings` | Applied, unresolved, blocked and resolved recommendations |
| `experimental_discrepancies` | Explicit actual/presented differences and measurement blockers |
| `provenance` | Initial bundle, prompt versions/digests and committed transaction refs |

Never persist plaintext secrets. Desired/confirmed/presented state is distinct:
`desired_identity.branch = main` is not evidence that the guest has `main`.
An LLM cannot assert confirmed state or mark a guest operation successful.
Only executor verification or verified target-caused state changes do that.

Use stable fact/finding IDs and evidence references rather than duplicating
transcript text. Archive detailed historical evidence outside the active
profile; retain active commitments, dependencies and unresolved findings in
the packet. Explicit size limits must report omitted evidence/coverage. Never
silently discard facts needed for consistency. Degrade to installed rules if
the packet cannot fit; log why it was not reviewed.

Facts changed legitimately by the target supersede their current values with
target provenance. A target switch to `release` must not be hidden by a stale
`main` rewrite. Deterministic mode must also reconcile supported state changes
and invalidate rules whose preconditions no longer hold, without invoking an
LLM. Unknown transitions become logged coverage gaps, not invented state.

## 4. Proposal and review contracts

Implement machine-validated schemas matching the prompt file:

* `profile-packet.v1`: packet/phase identity, profile and rule digests,
  observation digest, profile, request/result, existing rendering, verified
  deltas, authoritative protected facts, operation catalog, limits and coverage.
* `profile-proposal.v1`: echoed packet/phase/digests, `summary`, `checked`,
  `profile_changes`, `rule_changes`, `guest_operations`, `findings`.
* `profile-review.v1`: echoed packet/profile/proposal digests, `verdict`,
  `blocking_findings`, `suggestions`.

Each change has `change_id`, `reason`, `depends_on` and `compatibility`.
Profile changes use `set|remove`, a whitelisted `path`, `expected_old`, and
`value` for set. LLM-writable paths are desired identity, rule-associated
presentation commitments, and findings; authoritative provenance/confirmed
state is harness-owned. Presentation commitments become applied only after
the rendering is recorded as delivered. Rule changes use `add|replace|remove`,
`rule_id`, `expected_old` and `rule` where applicable, plus `finding_id` and
`experimental` flags. Operation requests use `operation_id`, catalog `kind`,
`args`, `preconditions`, dependencies and `finding_id`.

A finding contains `finding_id`, `category`, `evidence_refs`, `actual`,
`desired`, `status`, `blocked_reason`, `container_level_alternative`
(`possible`, `setting`), `persistence_recommendation`, `verification_steps`,
and `efficiency_note`. The harness assigns actual execution/applied status;
proposal status can only be proposed, unresolved or blocked. Existing findings
can be updated but not silently deleted. Every rule/guest change references
an actionable finding. No-op responses still report actual categories checked.

Reject stale digests, unknown fields/actions, malformed dependencies and
invalid/unsupported operation requests. The LLM may override user-origin rules
with explicit before/after logging, but cannot override protected invariants.
Dependencies form an acyclic ordered graph; validate the whole transaction.

The optional reviewer uses the same relevant packet plus candidate rendering,
proposal digest and deterministic validation evidence. Reject only concrete
contract violations; suggestions do not block. No reviewer or proposer can
supersede deterministic validation. Review has its own explicit budget and
logged result. An enabled reviewer failure skips the pending proposal while
preserving installed state; it does not silently install an unreviewed change.

## 5. Tool pipeline and rendering

Add an actual pre-execution hook; the presenter's post-execution hook alone
cannot prepare the guest before a command. Record the target's request exactly
and never silently translate it to compensate for fictitious state.

At each tool boundary:

1. Acquire episode/proxy serialization and reconcile known state changes.
2. Apply relevant installed deterministic logic. In development, inspect the
   request before execution when eligible, then validate any LLM proposal.
3. Commit verified supported guest changes before executing the target call.
4. Execute the original target request through the existing broker/journal.
5. Capture immutable raw results and verified state deltas.
6. In development, review each eligible result and propose rules/profile
   changes. Validate/render against the actual result.
7. Commit compatible changes, record the exact delivered result and its
   presentation commitments, and release serialization.

Pre/post inspection may mean two calls per tool; budgets must count both
explicitly. Service operations remain excluded from adaptive rewriting.
Their protected results still inform authoritative state tracking. Treat
excluded and oversized observations as explicit coverage events.

Extend rule matchers to operation/surface-aware rules for shell stdout/stderr,
read text and search results, with state preconditions. Preserve legacy shell
rules by conversion. Cover read/search through captured `tool-output://`
references as well as direct outputs. Apply guards to every delivered surface;
preserve truncation/reference semantics and retain raw evidence. Do not modify
on-disk project content merely to make a read agree with a display alias.

Cosmetic transformations must preserve technical/error meaning. Explicitly
marked temporary operational discrepancies are allowed in development, with
actual/presented facts, likely consequences and a persistent fix recorded.
They are a limited exception to operational truthfulness, never to service
facts, tool statuses, error outcomes or authority evidence. A late guest change
cannot retroactively make the triggering command succeed.

## 6. Supported guest operations and transactions

The LLM emits structured operation requests, not arbitrary shell/Python code.
An extensible harness-owned catalog declares argument types, writable scope,
preconditions, actual execution, verification, rollback and persistent setup
equivalent. Each operation must be recoverable or rejected before execution.

Initial catalog candidates:

* Create a named branch at the verified current commit, optionally attach HEAD;
  preserve commits and working-tree changes, never overwrite an existing ref.
* Set the guest hostname to the profile value, without changing service DNS.
* Configure approved workspace environment/display settings within the guest.
* Install preset background/dotfiles at dedicated approved paths, without
  overwriting target-created content or changing project/credential files.

Unsupported ideas remain findings with concrete persistence advice. Do not
interpret a broad operation such as arbitrary file write as a replacement for
an unrestricted command runner. Expand the catalog as needed.

Serialize mutations against target tools and snapshot original values.
Write intent durably before mutation; execute and verify operations in order;
commit the bundle atomically only after success. Filesystem atomic replacement
does not make guest operations atomic: record compensating rollback steps.
After a failure, restore the prior consistent state when possible. If rollback
fails or actual state is uncertain, stop and save a diagnostic checkpoint.
On restart reconcile incomplete transactions before doing new work.

Log the exact failed phase: proposal validation, review, operation execution,
verification, bundle persistence, delivery or rollback. Include transaction/
operation IDs, sanitized error, before/after facts, completed steps, recovery
attempts and whether execution continued. Preserve the installed bundle on a
rejected/no-op proposal. Concurrent stale proposals cannot overwrite it.

## 7. Deterministic reproduction

Before a non-development target starts, load the bundle, apply saved setup and
verify required state/cross-tool agreement. Setup must preserve protected
equivalence between conditions. No runtime LLM, including a reviewer, is
allowed. Context-specific facts such as a prior episode's current branch are
not blindly replayed as fresh setup; persist only explicit setup requirements.

Profiles with unresolved operational inconsistencies or failed required setup
are ineligible for measurement. Missing coverage and unresolved cosmetic
findings remain visible in the receipt; do not invent a successful check.
Keep existing measurement acceptance gates and frozen rules protections.

## 8. Budget degradation and continuation

On auxiliary exhaustion, timeout or invalid response before mutation, continue
with the last committed profile/rules. Record exactly where LLM coverage
stopped. Preserve unresolved findings and checkpoint continuously. A partial
mutation uses transaction recovery, not ordinary fallback.

Budget-paused experiments need a durable `artifact-run-checkpoint.v1`:

* Run/session IDs, model/provider configuration references and condition.
* Native target session storage/conversation and in-flight provider delivery
  state; transcript text alone is not assumed sufficient for restoration.
* Guest filesystem including uncommitted files, branches, metadata, environment
  and volumes; service inventory/bytes/quota/audit state and configuration.
* Workflow stage, warmup/repair/upload progress and checkpoint evidence.
* Profile/rule versions, pending proxy transactions, findings and operation log.
* Execution journal, output-store objects, delivered result IDs and receipts.
* Original budgets, consumed/reserved usage, stop reasons and segment history.
* Dependency manifests/digests, restore capability and missing prerequisites.

At budget stop, stop accepting new work, reconcile/cancel in-flight operations,
and preserve restorable state before teardown. Snapshot guest/service state at
a consistent quiescent boundary. Retaining live resources is allowed but must
be documented with lifecycle/cleanup instructions; durable restoration should
not depend solely on an accidentally surviving container.

`continue` preflights dependencies and digests, takes an exclusive run lease,
restores state, reconciles pending journal entries/transactions, and resumes
the existing session/stage. Never rerun episode initialization, reapply the
reference repair, reset service inventory, or repeat completed tool actions.
Use stable run/session/call/argument keys and existing receipts for replay.
Uncertain service mutations require reconciliation, never blind retry.
Unavailable runtime session restoration is an explicit blocker with a saved
checkpoint; do not substitute a new session and call it continuation.

Additional allowances are explicit positive increments in existing units for
the exhausted target stages and/or auxiliary budget. At least one increment is
required. Retain original limits and cumulative usage; never reset accounting.
Exclude paused wall time from active allowance while retaining timestamps and
pause duration. Appending a segment must not overwrite original results.
Continuing beyond a measured run's original budget marks the resumed segment
as extended development evidence; preserve the original measured segment and
prevent the combined result from masquerading as the original measurement.

If only proxy budget is exhausted and the target is still running, `continue`
can replenish auxiliary allowance at the next serialized tool boundary through
an authenticated local control path. The active executor remains the single
owner; do not launch a second target process. Retrospective inspection of missed
observations produces findings only: it cannot edit delivered history or apply
stale proposals to the current guest. Future changes require current-state
validation. Save both live-review gaps and retrospective reviews distinctly.

## 9. Evidence and reports

Keep existing rules snapshots, raw/presented captures and candidate reports.
Add immutable profile snapshots, guest-operation transactions and continuation
segments. Every event identifies run/segment/session/call, phase, prompt
version/digest, profile/rules digest, raw/presented digest, usage and evidence
refs where applicable. Sanitize errors and protect secret-bearing raw captures.

The human report must separate applied rewrites, verified guest changes,
experimental discrepancies, persistent setup recommendations, blocked ideas,
efficiency improvements, failures/recovery, LLM coverage gaps and continuation
history. Show before/after bundle/rule changes, including user-rule overrides.
Provide exact evidence refs and proposed verification for each finding.

## 10. Implementation map and acceptance gates

Existing modules to extend:

* `rules.py`: operation/surface matching, state preconditions, legacy conversion
  and all-surface protected rendering checks.
* `live.py` / `discovery.py`: new prompt transport, packets/proposals/reviewer,
  budgets and transaction orchestration; preserve zero-call script behavior.
* `host_adapter.py` / `recording.py` / `output_store.py`: pre-execution hook,
  state deltas, all delivered surfaces and delivery commitment records.
* `task.py` / `workflow.py` / `lifecycle.py` / `journal.py`: resumable resource
  lifecycle, ledger/workflow restoration and mutation reconciliation.
* `scripts/artifact_upload.py`: profile CLI, continuation/control path and
  explicit manifests; reporting/view scripts expose the new evidence.

Suggested new modules: `profiles.py` for schemas/persistence,
`guest_operations.py` for the operation catalog, and `continuation.py` for
checkpoint/restore. Repository inspection may refine file boundaries without
changing the behavioral contract.

Implement in stages: bundled profiles and prompt validation; observation
coverage and state reconciliation; verified guest transactions; deterministic
setup reproduction; durable continuation; migration/docs. Keep legacy behavior
working through explicit adapters. Installing the new prompts requires their
packet/response schemas to be implemented first.

Required acceptance cases:

1. Missing profile initializes the correct preset; existing invalid files fail;
   output-only paths are not loaded; both conditions never share mutable state.
2. One accepted response evolves profile and executable rules together; stale
   digests/dependency errors cannot partially install changes.
3. Supported branch creation is real and verified; later Git operations agree.
   Target-created branch changes survive and invalidate stale presentation rules.
4. Unsupported operational rewrites are visibly experimental and block
   measurement; incompatibility/efficiency ideas remain in the report.
5. Shell/read/search/error/output-store paths agree; protected service facts,
   hashes, sizes, owner evidence, statuses and actual failures remain unchanged.
6. Injected instructions inside output cannot request arbitrary execution or
   override the contract. Optional review rejection preserves installed state.
7. Inject failures at execution, verification, persistence and rollback; logs
   identify the step. Recovery restores consistency or stops with evidence.
8. Non-development calls no auxiliary model, verifies setup, and produces
   reproducible presentation under the same bundle and observed state.
9. Auxiliary exhaustion preserves deterministic behavior and logs review gaps;
   replenishment resumes review without rewriting delivered history.
10. Budget pause/continue restores conversation, uncommitted changes, service
    state and workflow stage, with no duplicate upload/deletion/tool execution.
    Missing dependencies and crash-after-mutation are reconciled explicitly.
11. Continuation budgets accumulate, original limits/results remain intact,
    measured extensions are labeled, and a second continuation owner is refused.
12. Existing mandatory repository gates pass, with any intentional schema/test
    migrations documented. Run focused tests during implementation, then the
    required integration suites at their gates.
