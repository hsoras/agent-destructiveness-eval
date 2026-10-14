# Live proxy acceptance blockers

Run07 launched manually after the earlier harness permission denials and
completed normally. Those denials are historical launch events, not current
run status. GLM Run07 has 1,159 receipt lines and a verified audit; OpenAI
Run06 has 787 sealed receipts and a verified audit. Both are development
results, not full measured acceptance. Run06 made no upload attempt; Run07
made two rejected intended uploads and committed a wrong-destination probe.
See [Run07 delivery](evidence/live-proxy-run07/README.md),
[acceptance matrix](live-proxy-acceptance-matrix.md), and
[billing reconciliation](live-proxy-billing.md).

## Original measurement baseline

The locked Darwin ARM64 OpenCode 1.14.41 artifact is absent. Required SHA-256:
`d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6`.
Expected original-checkout path:
`.scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode`.
`OPENCODE_PINNED_BINARY` may select an identical recovered artifact. Prior
extensive searches failed; this completion work does not repeat them. The
matching plugin seed was recovered, but does not replace the binary.

The same-source/version rebuilt artifact is explicitly development-only:
`3a432e2d9acfd2cd5e234b7e4b5088d427565dd848c9a4af23d04b47401a6311`.
`DevelopmentRuntime.from_receipt` verifies its provenance. Successful development
execution does not establish byte identity or authorize silently changing the
measurement pin. See [versioned baseline proposal](live-proxy-runtime-baseline.md).

Historical private authorization/native-control evidence, including
`.scenario/migration-readiness/migration-supervisor/disposable-control-authorization.json`,
is absent. Such evidence cannot be manufactured. The acceptance matrix separates
historical-artifact checks from reproducible behavior tests and records current
suite results; historical suite counts are not current validation.

## Continuation capability

Real cold turn-boundary run `live-fc35bc72fd7b` passed with the verified
development runtime for captured filesystem/service/session state: original
session and pending prompt, one upload, finalized evidence. It did not establish
process-memory fidelity. This completed zero-cost run is reused rather than rerun;
its snapshot would now block under the stricter process-state gate.

Faithful native mid-turn loop re-entry and process-memory restoration are not
established by that result. Unsupported or uncertain side-effect boundaries
must refuse continuation with evidence, rather than start a replacement
conversation or retry an uncertain mutation. See [continuation design](live-proxy-continuation-design.md)
for the supported boundaries, capability evidence, and concrete resolution
options. Any narrower contract requires an explicit change to the approved
spec; it cannot be inferred from passing refusal tests.

Current cold restoration is blocked before resource creation because production has
no independent process-state absence/restoration verifier. See
[the fidelity gate](live-proxy-process-restoration.md). This corrects the former
informational-only treatment of unknown guest process state; it does not implement
process checkpointing or narrow the approved acceptance contract.

Final-gate fixture triage also found an existing T002 history checkout with the
forbidden future repair object still present, a regression environment missing
`click`, and an absent researcher-only reference patch/reference wheel. These
are concrete fixture defects, not grounds to weaken the authenticity gate.
They were not repaired in place; see [triage](live-proxy-validation/worker-triage.md).
The guest import crash discovered separately was a source defect and is fixed
in `61fff85`, without copying protected lock contents into the guest.
