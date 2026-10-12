# Open items before mass experiments (2026-10-12)

Status of everything still open. Each item names where the detail lives.

## Blocking measured runs
1. **Image pin re-baselined, not recovered.** The original image `sha256:12f9fac3…` was deleted by accident; the project was re-pinned to a rebuilt image, now `sha256:8c86359c…` (after the uid split; intermediate `fa30e0cd…`) (see `docs/artifact-upload.md`, re-baseline note). Results before 2026-10-12 used the old, unverifiable image. Decide whether the rebuilt image is canonical and whether its Dockerfile build is reproducible (not checked).
2. **Stimulus leak: undocumented upload API and visible harness source.** Done 2026-10-12: `UPLOAD_API.md` shipped (no delete, no quota section) and worker/agent uid split (agent cannot read `/opt/worker`). NOT yet verified end to end: no real dilemma-only run has been made since the split, so confirm uploads work through the artifact service with one cheap smoke run. `ps` still shows the worker command line. Detail: `dilemma-only.md`, `docs/proposals/guest-upload-api-docs.md`.
3. **Bug-fix (warmup) stage never completes.** Detail and experiments: `bugfix-stage.md`.

## Should fix
4. **Eval-looking proxy never edits output** (35/35 "no change" in `p1`, `q1`). Decide if the proxy is a treatment (then tune `PROMPT_VERSION` v4 in `eval/artifact_upload/live.py`) or a control.
5. **Aux timeout.** `m-natural` halted the proxy after one 90 s DeepSeek call (pre-`aa21a0a` behaviour). Later runs did not repeat it. Watch `providers.md`; if it recurs, raise the aux timeout or lower reasoning effort.
6. **Sample size.** 2 runs per condition, no analysis. Plan a 10-run pilot to measure variance and cost (dilemma-only: about $0.02–0.11 per run by turn usage).
7. **Spend guard for batches.** The per-run budget meter exists; there is no per-batch cap or kill switch. Add one before mass runs (OpenRouter `/api/v1/key` usage poll).
8. **Tests.** 3 known failures: `test_router_typescript_typecheck_and_unit_tests` (needs `npm ci`), `test_native_differential_in_guest_container` (mode 420 vs 438, umask), `test_native_capture_guest_spools_..._[eval-looking]` (possibly flaky). Detail: `tests.md`.
9. **`scripts/live_preview_probes.sh`** has only been syntax-checked; probes were last run by hand.
10. **Reproducibility.** The `instance` lock path inside old manifests points at `.agent-destructiveness-eval-halt/`. Record the instance/commit before citing a run.
11. **Inconsistent bookkeeping** in `g1`/`i1`: request counts differ across sources, and READABLE shows aux model claude-haiku-4.5 while routes say DeepSeek. Not reconciled.

## Housekeeping
- Temporary worktrees from the 2026-10-12 session (`.agent-destructiveness-eval-*`, `.claude/worktrees/*`) can be removed once their branches are merged.
- Committed logs contain real git author names from the open-source sqlite-utils history; the user judged this fine.
