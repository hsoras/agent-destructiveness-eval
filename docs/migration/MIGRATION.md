# Migration entry point

This is a planning-only handoff for migrating the repository to the artifact-upload experiment. No implementation, worktrees, branches, agents, commits, or paid model runs have been started.

Read [PLAN.md](PLAN.md) for the ordered execution waves, parallel pairs, synchronous gates, integration checkpoints and rollback. The immutable planning snapshot is [NEW_SPEC.md](NEW_SPEC.md), copied byte-for-byte from the root specification. Supporting documents are [CURRENT_STATE.md](CURRENT_STATE.md), [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md), [DECISIONS.md](DECISIONS.md), [RISKS.md](RISKS.md), [STATUS.md](STATUS.md) and [REQUIREMENTS_MATRIX.md](REQUIREMENTS_MATRIX.md). Individual worker briefs are in [tasks/](tasks/).

Use the wave table in PLAN.md as the execution order. STATUS.md supplies dependency status; a task marked ready means graph-ready, not approved to begin. Boss alone maintains centralized tracking. Every worker starts in an isolated worktree from successfully integrated prerequisites, and every merge is sequential.
