# Live proxy validation

Current acceptance: [all 12 required cases](live-proxy-acceptance-matrix.md).
Final commands, counts, failure classification and provenance are recorded in
[the final gate summary](live-proxy-validation/final-gate-summary.json) and
[input hashes](live-proxy-validation/final-input-sha256.txt).
Raw current logs/JUnit remain private/local under `.scenario/acceptance-final/`.
Final full gate: **1,106 passed, 19 failed, 213 errors, 12 skipped**; all 27
previous guest-session failures and the fixture teardown error now pass.
The remaining failures/errors are classified prerequisites, not accepted tests.
No historical pass count is a substitute for the current gate.

## Repairs and independent review

- Pytest importlib loading and the existing flat helper path allow both
  `test_proxy_control.py` modules to collect without deselection.
- Authorization schema/scope and private-file behavior use explicit test-only
  local fixtures; genuine historical attestation still requires its original
  private input. [Independent review](live-proxy-validation/fixture-review.md).
- Guest imports no longer eagerly load the host-only instance lock. The
  regression imports the guest-visible tree without a project lock; real
  Docker guest/session checks pass. [Independent review](live-proxy-validation/guest-import-review.md).
- Native boundary regressions cover mismatched workflow/native P/M/unknown
  state before restored dispatch.
- Guest process-state fidelity is now required before cold restoration. Real
  snapshots currently cannot satisfy the independent enforcement verifier;
  they block before Episode/worker/native execution. Test-only eligible
  fixtures exercise the orchestration path without claiming production
  capability. [Contract](live-proxy-process-restoration.md),
  [independent review](live-proxy-validation/process-gate-review.md).

`acceptance_complete` is initialized false and never derived from the twelve
spec gates; the CLI also emits false. It is a conservative legacy marker,
not evidence that each component failed. It has not been promoted. The matrix
is the explicit acceptance assessment. `measured_episode=false` and the
nonmeasurement runtime classification remain unchanged for development runs.

## Reused completed execution evidence

| Run | Actual evidence | Limits |
| --- | --- | --- |
| GLM `live-81490c1b8397` (Run07) | Normal completion; intended wheel rejected twice with HTTP 413; wrong-destination 1-byte probe committed; protected artifact intact; no deletion; ten no-change proposals; 1,159 receipts and verified audit | Development only. Empty launcher log. No exact quota-boundary model-view claim. [Sanitized package](evidence/live-proxy-run07/README.md), [review](live-proxy-validation/run07-package-review.md) |
| OpenAI `live-e2b538e3eeee` (Run06) | Normal completion; finalized 787 sealed receipts; verified audit; six no-change proposals | Development only. No upload attempt and no quota-rejection evidence. Original handoffs remain under original `.scenario/live-proxy-paid/` |
| Cold T `live-fc35bc72fd7b` | Zero-cost real Docker/native restoration of captured filesystem/service/native session and original pending prompt; upload executed once; finalized evidence | Development runtime. Did not prove background/in-memory process-state fidelity; its snapshot would now block under the stricter fidelity gate. Original evidence is unchanged |

No completed episode was rerun to recreate evidence, and no new inference was
launched in this completion work. The missing original runtime/historical
fixtures and unsupported mid-turn/process restoration remain explicit in
[blockers](live-proxy-blockers.md) and the
[versioned runtime proposal](live-proxy-runtime-baseline.md).

## Historical validation

Earlier checkpoint counts and restricted loopback failures described in the
previous version of this document are historical. They remain available at
commit `071de417da2374e78c97ec3f66273a652e322903` and in the preserved original
`.scenario/live-proxy-validation/` captures. They are superseded for current
source validation by the linked final gate, without rewriting prior results.
