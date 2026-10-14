# Process-restore gate review (spec §8)

Sources verified: continuation.py fa1c99a2…15b1dd, runtime_resume.py 521b81fd…c78 (hashes match).

Verdict: no required-behavior defects.

- Fail-closed: all real snapshots blocked (no production verifier); known/unknown/malformed/missing/tampered evidence and missing verifier blocked; `guest-process-checkpoint` not accepted.
- No bypass: `scripts/artifact_upload.py` continue calls `continue_native_run` with no hooks; `process_attestation_verifier` is only reachable via the Python `hooks` arg (test-only). No default verifier. Verifier exceptions or non-`True` returns block.
- Ordering: gate runs in `preflight_resources` before `new_episode()`; native preflight before it is read-only (`verify_snapshot`, `resume_assessment`). `restore_resources` re-gates. Segment/budget/ledger writes occur only after restore+reconcile, so a block consumes nothing; blocker saved via `_block` as `guest-process-state-unrestorable`.
- No retries: gate precedes reconcile/resume; uncertain journal handling unchanged.
- Docs (live-proxy.md, process-restoration, continuation-design) state cold T of real captures is blocked; none imply full support.
- Tests: test_process_restore_gate.py + test_runtime_resume.py = 43 passed (run here).

Optional hardening only: attestation artifact `path` is read as-is (not confined to run dir; the digest check bounds impact); `gaps` substring match "process" is name-based.
