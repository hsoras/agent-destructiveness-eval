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
