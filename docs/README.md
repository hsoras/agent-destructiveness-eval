# Docs index

Project overview: [../README.md](../README.md). Run records are dated; check dates before relying on them.

## Start here

- [live-proxy.md](live-proxy.md): how to run the live-preview proxy (llm mode, budgets, deferral, review phase).
- [artifact-upload.md](artifact-upload.md): researcher handoff for setup, commands, limits and records.
- [STATUS.md](STATUS.md): current engineering and readiness status (linked from the README).
- [opencode-harness.md](opencode-harness.md): OpenCode runtime contract for the experiment.

## Design and specs

- [live-proxy-spec.md](live-proxy-spec.md): user-approved profile-proxy implementation spec.
- [live-proxy-deferral-spec.md](live-proxy-deferral-spec.md): approved contradiction-safe edit and next-run deferral design.
- [live-proxy-prompts.md](live-proxy-prompts.md): proposer, reviewer and judge prompt contracts.
- [live-proxy-continuation-design.md](live-proxy-continuation-design.md): durable continuation design (historical basis).
- [live-proxy-process-restoration.md](live-proxy-process-restoration.md): rule for cold T restoration (referenced by tests).
- [live-proxy-test-fixtures.md](live-proxy-test-fixtures.md): pytest collection and historical fixture migration.
- [artifact-upload-contracts.md](artifact-upload-contracts.md): v1 interfaces and interception contracts.
- [managed-env-adapter.md](managed-env-adapter.md): host-side managed environment adapter behavior (no inbound references).
- [live-proxy-plan.md](live-proxy-plan.md): original 2026-10-11 design; superseded by the spec, kept for history.

## Run records and status

- [live-proxy-validation.md](live-proxy-validation.md): acceptance summary, linking the matrix and gate outputs.
- [live-proxy-acceptance-matrix.md](live-proxy-acceptance-matrix.md): 12-case acceptance matrix and run record.
- [live-proxy-blockers.md](live-proxy-blockers.md): acceptance blockers and their status.
- [live-proxy-billing.md](live-proxy-billing.md) and [live-proxy-billing-reconciliation.json](live-proxy-billing-reconciliation.json): billing reconciliation for paid attempts.
- [live-proxy-paid-preflight.md](live-proxy-paid-preflight.md): read-only preflight for the one paid preview run.
- [live-proxy-findings-2026-10-11.md](live-proxy-findings-2026-10-11.md): development-only findings from the 2026-10-11 preview runs.
- [live-proxy-runtime-baseline.md](live-proxy-runtime-baseline.md): runtime baseline and rebaseline proposal (not approved).
- [live-proxy-progress.md](live-proxy-progress.md), [live-proxy-delivery-handoff.md](live-proxy-delivery-handoff.md): working progress and delivery handoff notes.
- [live-proxy-runtime-review.md](live-proxy-runtime-review.md), [live-proxy-runtime-reproductions.md](live-proxy-runtime-reproductions.md): partial static reviews, no reproduction run.

## Evidence and subfolders

- [evidence/](evidence/): captured run artifacts (live-proxy-run07).
- [live-proxy-validation/](live-proxy-validation/): gate summaries, review notes and input hashes for acceptance.
- [proposals/](proposals/): API documentation proposals, not adopted.
