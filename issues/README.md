# Live-proxy issues: index

Branch `live-proxy`. Development preview: no run here is a measured result. Start with `bugfix-stage.md` for the open question.

| File | What it covers |
|---|---|
| [`bugfix-stage.md`](bugfix-stage.md) | Why the warmup (bug-fix) stage is slow and never reaches the checkpoint: blockers, run index, per-run measurements, g1 and i1 timelines, token-meter quirk, checkpoint gate, hypotheses and experiments, commands |
| [`dilemma-only.md`](dilemma-only.md) | Dilemma-only runs (2026-10-12): results, stimulus leak, proxy timeouts, eval-looking proposer, lock path, image pin override, probes |
| [`tests.md`](tests.md) | Known test failures, with the exact `tests/artifact_upload` failing list as of 2026-10-12 |
| [`providers.md`](providers.md) | Provider reliability log: one row per run or probe, price reference, how to append a row |
| [`fixtures/policy4.json`](fixtures/policy4.json) | Copy of `/private/tmp/live-cfg/policy4.json` (dilemma-only upload caps). Checked: no secrets |
| [`fixtures/routes2.json`](fixtures/routes2.json) | Copy of `/private/tmp/live-cfg/routes2.json` (proposer pinned to `decart/fp4`). Checked: env var name only, no key |

Raw run data stays outside the repo in `/private/tmp/live-runs/` and `/private/tmp/live-cfg/`. Committed run copies are in `logs/live-preview-2026-10-11/` and `logs/live-preview-2026-10-12/`. The root `ISSUES.md` points here.
