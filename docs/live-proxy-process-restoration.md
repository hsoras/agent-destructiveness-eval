# Guest process state and cold T restoration

## Rule

A cold `T` continuation is refused, before any restore effect, unless the
saved guest snapshot establishes that no required guest process state is lost.
Informational warnings are insufficient.

## Gate (`continuation.process_restore_problem`)

Checked in `runtime_resume` `preflight_resources` and again in
`restore_resources`, before `new_episode()` is called, so a refusal creates no
Episode, Docker resource, worker session or native turn. `continue_run` records
checkpoint blocker `guest-process-state-unrestorable` with the concrete reason.
Blocked when any of: malformed snapshot or gaps; any gap whose name contains
"process" (including the `target-background-processes` gap emitted by
`Episode.continuation_snapshot`); snapshot not `quiescent` and
`durable_volume_export`; missing, malformed or unknown-method attestation
(`guest-process-state-attestation/v1`, method `enforced-process-free-boundary`,
`quiescent: true`, `required_process_state_uncaptured: false`); evidence
artifact (`process-state-attestation`) missing, unreadable, hash-mismatched or
not matching the claim; or no independent enforcement verifier confirms it.

`guest-process-checkpoint` is deliberately not accepted: `Episode.restore` has
no process restore implementation. `docker top` observation is never
attestation. The verifier is only reachable through the
`process_attestation_verifier` hook; production passes none, so **all real
snapshots currently remain blocked**. There is no override.

## Historical cold run

The earlier cold T run is unmodified. It remains a successful restoration of
captured filesystem, service and native-session state, but it does not
establish full process-state fidelity (spec §8). Its snapshot would be blocked
by this gate.

## Test compatibility adjustment

Existing synthetic T tests need test-only evidence. `test_runtime_resume.py`
`paused` fixture now adds `quiescent`, `gaps: []`, a
`process-state-attestation` artifact and a hook
`process_attestation_verifier` (test-only stand-in). Assertions are unchanged.
New regressions: `tests/artifact_upload/test_process_restore_gate.py`
(known gap, unknown, malformed, missing, tampered, no verifier: zero episodes,
workers, native turns; eligible fixture restores). Native P/M coverage remains
`test_native_only_mid_turn_boundary_blocks_before_any_restore`.

Not implemented: runtime repin, process checkpointing.
