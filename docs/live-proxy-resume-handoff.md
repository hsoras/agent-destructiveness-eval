# Resume integration handoff (T-boundary continuation)

State: **no code edits were made by this worker.** Only analysis is done, and it is recorded here. Nothing needs
reverting. Owned scope for the next worker: `eval/artifact_upload/task.py`, `eval/artifact_upload/runtime_resume.py`,
a NEW receipt-interval helper module and its tests, and (only if necessary) `sandbox/artifact_upload/receipts.py`.
Do not touch `recording.py` (delivery worker), CLI, `host_adapter`, `profile_proxy`, `lifecycle` or `workflow`.
Existing tests to extend: `tests/artifact_upload/test_runtime_resume.py` (FakeEpisode/FakeAdapter/hooks pattern).

## Verified facts

- `continue_native_run` (runtime_resume.py:165) accepts `presentation_factory(checkpoint=, recorder_root=, credentials=,
  proxy_totals=, segment=)` and `finalize(ctx)` (`ResumeContext`). With no factory and a profile/proxy checkpoint it raises
  `presentation-factory-required` (line ~269). That is the placeholder to replace with a real default.
- `finalize` is skipped when the resumed workflow ends `budget_exhausted`, which then triggers `_recapture`.
- `task.run_episode` (task.py:295-560) holds the only final-evidence block, in the `else:` branch after `workflow.run()`:
  `episode.export_service_evidence(final_download)` -> `adapter.recorder.import_service_receipts` ->
  `assemble_final_inputs` -> `ledger.check` -> `score_episode` -> service provenance hash check -> credential-leak scan
  -> `final_resources`. `final_download` closes over `service`, `checkpoint`, `target`, `control`.
- `ProfilePresentation(out_dir, bundle, proposer, reviewer, budget, profile_out, await_delivery, catalog,
  reviewer_budget=)` (profile_proxy.py:204). Mode is "develop" iff a proposer is given. A reviewer requires `reviewer_budget`.
  A deterministic profile takes no clients or budgets. `usage()` aggregates proposer and reviewer accounts; `replenish`
  is additive and never resets. `receipt()` is what `_recapture` saves. **Not verified:** whether a constructor or restore
  hook can preload prior usage, halts or segments. Read `ProfileDiscovery`, the `LiveBudget` class and the account class
  (profile_proxy.py ~100-200) first. If there is no hook, request a small one from the profile_proxy owner. Do not reset counters.
- `Episode.restore` appends to the restored `/srv/store.receipts.jsonl`. `ServiceReceipts.__init__` keeps the prior bytes
  (`persisted_bytes`), generates a new `run_id` and emits `startup` with sequence 1 again. The file is therefore
  `[old interval: startup..., usually no seal because the service was `docker stop`ped at capture][new interval ... seal]`.
- `Episode.export_service_evidence` (lifecycle.py ~1029-1044) calls strict `validate_bundle(row['receipts'])`. On a
  restored run this raises (the file has two run_ids), so `complete=False` and `export_failure='ValueError'`. The retained
  files and `service-private/export.json` (which includes `source_errors`) are written *before* validation. **Lifecycle
  needs a small hook**: a `receipt_validator=` parameter defaulting to `validate_bundle`. Alternative without touching
  lifecycle: the finalize callback re-derives completeness from `export.json` (audit and storage present, no
  `source_errors`, and the multi-interval validator passes on `exported['receipts']`).
- `assemble_final_inputs` (sandbox/artifact_upload/audit.py:199) calls strict `validate_bundle` *again* on
  `validated['bundle']` and reads `validated['exchanges']` and `['run_id']`, `['bundle']['source_sha256']`. It needs a
  multi-interval variant, or a `validator=` parameter. That is audit.py, outside the stated scope, so ask the parent.
- Checkpoint binding for prior intervals: `snapshot['artifacts']` has a `layer-path` artifact with
  `source=='/srv/store.receipts.jsonl'` (plus `/srv/store.receipts.health.json`). These are **tar-wrapped** `docker cp`
  files, and the artifact sha256 is of the tar. Extract the raw member and hash it to get the pause-time prefix
  (size, sha256). `service_state.receipts_bytes` is the tar size only.

## Remaining tasks (all open)

1. **Receipt interval helper** (new module, e.g. `eval/artifact_upload/receipt_intervals.py`).
   Proposed API:
   `prior_prefixes(checkpoint_snapshots) -> [{size, sha256, health}]` (reads the tar artifacts, verifies the artifact sha)
   and `validate_intervals(bundle, prior) -> {'bundle','records','exchanges','run_id','intervals':[...]}`.
   Rules:
   - split the raw bytes by contiguous `run_id`;
   - require `len(intervals)==len(prior)+1`;
   - require each prior boundary to equal the cumulative offset, with the prefix sha matching;
   - per interval: startup first, sequence 1..n, monotonic time, known kinds, unique exchange/attempt ids across all intervals;
   - the final interval is checked strictly by calling `validate_bundle` on a sub-bundle (raw slice, own sha/size, same health);
   - earlier intervals are reported as `{sealed: False|True, unfinished_exchange_ids}`. Never treat an unfinished interval as sealed.
   Return `records` and `exchanges` as the union, so `RecordingPresenter.import_service_receipts` and
   `assemble_final_inputs` work with it.
   Keep the single-interval strict gate untouched (use this only when prior is non-empty).
   Hook request to the delivery worker: an optional `validator=` on `import_service_receipts`, defaulting to `validate_bundle`.
2. **Default presentation factory** in `runtime_resume.py` (replace the placeholder raise). Inputs come from
   `saved['profile_proxy']['profile']` (bundle data -> `ProfileBundle`; verify the sha) and `refs['auxiliary_routes']`
   (client rebuild from `api_key_env` environment references only; never a literal key).
   - deterministic checkpoint (no proposer route) -> `ProfilePresentation(..., await_delivery=True, catalog=Catalog(workspace=<actual restored worktree = episode.resources['profile_manifest']['worktree']>))`.
   - develop -> clients from routes, `budget`/`reviewer_budget` = restored totals from `proxy_totals`
     (`{'proposer':{cost_usd,pairs},'reviewer':{...}}`). Restore cumulative usage, limits and halts (see the unverified point above).
   - Legacy `LivePresentation` (non-profile `proxy_mode='llm'`) needs the equivalent, or an explicit saved blocker.
   Call `attach_recorder(adapter.recorder)` (already done), and `proxy_control.start_control(root, presentation.replenish)` only if the CLI wants live replenishment.
3. **Reusable finalize** in `task.py`. Extract the final-evidence `else:` block into
   `finalize_evidence(*, episode, adapter, ledger, service, checkpoint, world, target, result, scenario, start, control=None, approved=frozenset(), receipt_validator=None)`.
   - `run_episode` calls it; the output must be byte-identical for the single-interval path (run the existing task tests).
   - The resumed T run (`next=='evidence'`) calls it with `receipt_validator=validate_intervals`.
   - The scored window `start` must come from the original run: save it in the checkpoint workflow result, or fall back to
     the first `scored_window` start. Check whether the workflow result stores it.
   - It must preserve the original `result` outcomes and write the segment result with development labeling
     (`LABELS`, the marker file, `measured_episode` false).
   - No model or tool repeats, and no repair/upload/inventory reset.
   - `ResumeContext` should carry `result` from `workflow.resume` merged over the saved result.
4. **Faithfulness gate before the first resumed provider call** (T boundary). In `_resume`, before `workflow.resume`:
   - compare the saved native session witness (`native_session.snapshot`: session id, message count/digest) with the restored store;
   - compare the tool-schema digest (the existing `native-tools-divergence` check) and `tool_seen`;
   - the only message added may be the saved pending workflow `next` prompt;
   - on any unprovable case, raise `ContinuationBlocked` with a saved blocker.
   Open journal entries (`checkpoint['pending_journal']`) already block in preflight with no retry. Optionally reconcile
   worker status from the raw recorded call arguments if a checkpoint/capture exists, but do not implement worker logic.
5. **Tests** (bounded, fake): default factory (deterministic and develop with restored counters, a halt kept, no reset);
   finalize-only resume at `next=='evidence'` (no model turn, original outcomes preserved, development label);
   multi-interval validator (valid 2-interval, prefix-sha mismatch, extra unknown interval, duplicate exchange id,
   sealed-vs-unfinished reporting, tampered last interval); faithful-gate block cases.

## Blockers to record (BLOCKERS.md / live-proxy-blockers.md, parent owns)

- External validation is blocked: pinned native OpenCode binary is unavailable (P/M boundaries are explicit blockers).
- Mid-turn P/M resume stays a saved blocker (the runtime does not expose re-entry without adding conversation content).
- Lifecycle `export_service_evidence` and audit.py `assemble_final_inputs` each call the strict `validate_bundle`.
  They need validator hooks (or a parent decision) before a restored run can score.

## Evidence

Files read: runtime_resume.py, task.py, sandbox/artifact_upload/receipts.py, audit.py:199, lifecycle.py (restore,
export_service_evidence, continuation_snapshot), workflow.py (continuation_snapshot, resume), recording.py:40-110,
test_runtime_resume.py. No tests run; no paid or native calls.
