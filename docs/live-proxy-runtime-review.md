# Profile proxy runtime review — partial handoff (Opus, read-only)

Scope: spec §§3–7/9, cases 2–9. **Stopped early because the routing changed.** These findings come from
static reading of a snapshot (`/tmp/proxyreview-snap`, taken 2026-10-12 15:59; `profile_proxy.py` sha
`6aa04f415350`, `guest_operations.py` `bdd27abd7ae1`, `profiles.py` `156be24d3518`). The tree was still
changing, so check each finding against HEAD. The focused-suite run on the snapshot did not finish. No
reproducer has been executed yet. Known pending repairs are not included (confirmed_state schema,
record_delivery evidence_ref, legacy snapshots, truncation coverage, identity checks, locks/logs, reviewer budget).

## Blocking (mandatory fix; suggested test)

1. **Hostname operation not bound to the profile value** (§6 "set hostname to the profile value").
   `guest_operations._host_validate` and `profiles._validate_guest_operation` accept any valid hostname.
   Fix: require `args.hostname == desired_identity.hostname` (as set by the candidate bundle). Test: a proposal
   with `system_set_hostname{hostname:"other"}` is rejected before any guest call.
2. **Reviewer receives unredacted tool output** (§9 protect secret-bearing captures). `_install` sends
   `candidate_rendering` computed from the raw `result`, but the packet's `result` is redacted. Fix: redact
   `candidate_rendering` (and `validation_evidence` text) the same way. Test: stdout containing
   `sk-AAAAAAAAAAAAAAAAAAAA` must not appear in the reviewer payload that `Scripted` captures.
3. **`currently_presented` goes through reject-mode `_secrets`**. `validate_packet` only redacts
   `OBSERVATION_FIELDS`. Any after-execution output with a secret-looking string makes packet validation
   raise. The result is logged as `rejected:` with no model call and no coverage gap. Fix: add
   `currently_presented` to the redacted fields, or log it as an explicit coverage gap. Test: same
   stdout → the proposer is called once and the packet's `currently_presented` contains `[REDACTED]`.
4. **Delivery commitment digest is not the delivered bytes** (§4/§5 "exact delivered result", truncation).
   `RecordingPresenter.present` calls `delivered(call, output, …)` before `RouterBridge` applies
   `fit_for_runtime`. The recorded sha can therefore differ from what the target received, and a commitment
   can be marked applied after fitting removed the rewritten text. Fix: record delivery on the final fitted
   reply (or re-check the commitment against it). Test: an oversized shell output whose rewritten token falls
   beyond the fit limit → the commitment stays `proposed` and the sha equals the digest of the bridge reply.
5. **Pre-execution commitments are lost**. `_install` sets `pending["fact_ids"] = …` (overwrite). An
   after-execution install on the same call drops the fact IDs installed before execution, so they never
   become applied. Fix: extend/union the list. Test: before and after phases each set a different
   `presented_facts.*` → both are applied on delivery.
6. **Unknown successful state transitions are neither reconciled nor logged** (§3, case 3). `_reconcile`
   only probes when a command matches `_STATE_CMD`. Commands like `git -c x=y checkout release`,
   `git symbolic-ref HEAD refs/heads/release` or `git update-ref` that succeed leave stale `git_head_branch`.
   A rule with `requires: git_head_branch == main` keeps hiding the target's switch, and no gap is recorded.
   Fix: probe after any shell command that mentions git (the probe is cheap), or log a gap when the regex
   misses. Test: LocalGuest repo with `git -c core.x=1 checkout -b release` → the state shows `release` and
   the stale rule is inactive.
7. **Git operation scope comes from LLM-writable `desired_identity.worktree`** (§3 desired ≠ confirmed; §6
   scope). `Catalog(workspace=bundle.data["desired_identity"]["worktree"])`: a proposal can set
   `desired_identity.worktree`, and the next run's git operations and probes then target that path. When the
   presented path differs from the real one, probes fail. Fix: take the workspace from the harness/episode's
   actual worktree, never from desired identity. Test: an evolved bundle with a changed desired worktree still
   pins git operations to the episode worktree.
8. **Plausible, needs checking: execution continues after `GuestTransactionStop` in `before_execute`**
   (§6 "stop"). `RouterBridge` turns the exception into a single-call violation. `ProfilePresentation` keeps
   no stopped state, so later calls run against uncertain guest state, unless the adapter's
   unreported-violation stop (`host_adapter.py` ~1195) ends the run. Confirm. If it doesn't, latch a stopped
   flag that refuses further `before_execute`.

## Non-blocking notes
- `_WRITE_FILE` runs `os.makedirs(parent)` before the realpath/home check. A symlinked `~/.config` can
  create directories outside home before the operation refuses (rollback may remove them). Hardening.
- `env_set_workspace` only checks that the env file was written. Nothing sources it, yet the finding is
  marked `applied`. Worth recording as "file installed, not active" in the report.
- `run_transaction` requires each operation dependency to appear earlier in the list. An acyclic but
  differently ordered proposal is rejected cleanly (no partial install). Acceptable; document it.

## Verified OK (static)
- System prompt and exact packet transport: `ProfileDiscovery` clones the client with an installed
  prompt that is byte-identical to `docs/live-proxy-prompts.md`; the user message is the packet JSON.
- No dedupe: every eligible observation calls the proposer until `exhausted()`.
- Reviewer soft failure, hard failure, rejection or budget exhaustion leaves `self.bundle` unchanged.
- Branch operation: refs only, verified against HEAD commit and porcelain status, so dirty files are
  preserved. Rollback is checked against the saved pre-state. `recover()` runs in `bind_guest` before new
  work.
- Bundle `dump` is atomic (tmp + fsync + replace). The commit runs inside the transaction, so a persistence
  failure rolls back the guest.

## Remaining for Sonnet (not done)
Write and run the reproducers above on LocalGuest/Scripted (fixtures in `tests/artifact_upload/test_profile_proxy.py`).
Cover cases 5 (read/grep/tool-output ref parity), 7 (fault injection per phase via `fault_hook`) and 8
(deterministic: zero auxiliary calls, reproducible rendering) at runtime.
