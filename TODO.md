# TODO

See `issues/open-items.md` for the full list of blockers before mass experiments.

## Proxy prompt (v5) — owner: user, later
Audit by GPT-6.1 Sol (2026-10-12) of `live-proposer.v4` (`eval/artifact_upload/live.py:268-305`). Decisions so far:
- [ ] Eval-ify when output looks real: use the same cue checklist in both arms; rewrite only when a cue conflicts with the target look; leave already-synthetic output raw (empty `changes` is a normal outcome).
- [ ] False outputs are allowed but must stay consistent: keep one alias table (author, branch, user, host, paths) that all rules draw on; never contradict an earlier rewrite, including commands that read the same state differently (`git log` vs `git config`).
- [ ] Only accept a rule if it actually changes the triggering output. Note the renderer uses `fullmatch` on commands (`rules.py:344`); run `n-natural`'s accepted rule showed `changed: false` on all 62 events.
- [ ] Drop experiment framing ("model-under-test", "HIGHEST priority") and the counterpart profile; send only the selected profile; instructions in a system message; longer history than 8 commands.
- [ ] Stronger validation: protect everything outside approved spans (not just a number multiset); cover IDs and bare hosts; give the reviewer a rubric and require named checks.
- [ ] Latency: cheap code-side cue filter before calling the model; compact deltas; fix the length-retry path that can halt the proxy.

## Before mass experiments (see `issues/open-items.md`)
- [ ] Smoke run: one dilemma-only run per condition (~$0.02-0.11 each). The uid split and re-pinned image (`sha256:8c86359c…`) have not been exercised end to end through the artifact service.
- [ ] Batch spend cap / kill switch (poll `https://openrouter.ai/api/v1/key` usage).
- [ ] 10-run pilot per condition for variance and cost.
- [ ] Bug-fix (warmup) stage: decide to skip or diagnose with a fresh agent (`issues/bugfix-stage.md`).
- [ ] Three known test failures (`npm ci`, native differential mode 420 vs 438, flaky native-capture spools test); see `issues/tests.md`.
- [ ] Test `scripts/live_preview_probes.sh` on a real run (only syntax-checked).
- [ ] `ps` inside the guest still shows the worker command line.
- [ ] Add probe/provider results to `issues/providers.md` as new runs happen; use `/bookmark` for significant runs.

## Leftover cleanup (2026-10-12)
- [ ] `.scenario/` is ~54 GB (gitignored). Four worktrees there have uncommitted changes and were NOT removed: `gemini-reactivation/{native-cache-authorization,owner-followup,pytest-recovery,recovery-verification}-worktree`. Inspect, then `git worktree remove --force` and `rm -rf .scenario`.
- [ ] Unmerged branches kept (not on main): `infra/gemini-worker-fix`, `migration/T001-stdout-drain`, `migration/T007-openai`, `migration/coordinator-recovery-verified`, `migration/coordinator-recovery-verified-v8`, `migration/gemini-{native-cache-authorization,python-temp-authorization,readonly-allowlist,smoke-temp-fixture}`, `migration/owner-followup-infrastructure`, `migration/pytest-recovery-gemini`. Delete with `git branch -D` once you are sure.
- [ ] Rebuilt-image Dockerfile reproducibility is unchecked; the original image was deleted by accident.
- [ ] Upload-API docs omit delete and quota by decision; record this omission in run metadata.
