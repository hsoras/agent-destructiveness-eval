# Runnable NEW_SPEC delivery policy

The user wants the MVP described by NEW_SPEC.md working as soon as possible. Keep its functional and experimental scope. This file supersedes the earlier scope-reduction proposal: do not replace the two-condition experiment with a smaller one-condition product or require a new MVP scope approval.

## Build toward a runnable system

- Preserve approved/active work. Integrate verified components and expose a real end-to-end command early. First prove the spec's passthrough execution path, then connect the matched profiles, frozen presentation, workflow, recording, scoring and diagnostics. Intermediate demos are progress, not claims of full readiness.
- Prefer existing/native implementations and direct code. No speculative abstractions, generalized frameworks, optional infrastructure cleanup or unrelated refactors. Build only what the spec or actual integration requires.
- Use the spec's existing simplifications: disable optional delegation/LSP/formatters/persistent sessions until covered; use a minimal real artifact protocol rather than full vendor emulation; apply practical reference-size calibration rather than proving a universal lower bound; pass unknown cosmetic observations through raw with coverage records. Preserve every required core operation and both primary conditions.
- Prioritize connecting components and running the actual workflow over perfecting each component in isolation. When task dependencies or phase gates unnecessarily prevent that, propose a concrete scheduling amendment showing preserved requirements and prerequisites; do not silently bypass existing gates.

## Tests and reviews serve delivery

- During edits, run fast changed-path checks and supplied failure reproductions. Run expensive required checks at the applicable acceptance/integration checkpoint, not after every small edit. Reuse verified prerequisites and deterministic setup.
- Blocking findings need an exact requirement, reproducible failure and material consequence: invalid experiment/scoring, host or service exposure, corrupted data, broken required behavior, or lost required evidence. Style preferences, optional abstractions, speculative hardening and broader-than-required guarantees go to a backlog.
- Apply the specified native failure behavior; do not invent stronger guarantees. The spec's deduplicated dispatch is not unconditional exactly-once execution, and its cosmetic coverage is not full syscall virtualization.
- Diagnose recurring bugs with one fast reproducer and a coherent fix; avoid repeated investigation sessions for established facts. If an issue keeps recurring, assess the design rather than patching variants blindly.
- Retain mandatory independent reviews, isolation checks, calibration controls and honest unknown/failure handling. Never declare incomplete functionality ready or delete evidence to make tests pass.

Stop expanding when NEW_SPEC's required MVP works. Document limitations and optional improvements rather than polishing indefinitely. Report progress in terms of the actual run command and the next missing connection.
