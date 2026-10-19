# Blockers

## OPEN: Probe "direct" measurements return invalid (missing text response)

- Runs 16–21 probe sets: 14 `direct` entries with `status: invalid, reason: missing text response` (natural 16: 2, 18: 4, 20: 1; eval 17: 3, 19: 1, 21: 3). The classifier probes still complete.
- Evidence: `logs/live-preview-2026-10-16/*-probes.summary.txt` and `*-probes.log`.
- Cause not investigated. Unblock: decide whether invalid direct probes are accepted measurement gaps or need a retry/repair. If repair, first gather failed-reply evidence cheaply.

## OPEN: Run 16 aborted at guest setup; profile carries a setup op that needs a decision

- Evidence: `natural-deepseek-run-16`, `result.json` `development-not-behavioral-result`; `files_normalize_untracked` on `dist/sqlite_utils-4.1.1-py3-none-any.whl` (not present before the target builds it) fails `arguments must be exactly ['mode', 'mtime_from', 'paths']` in `proposal_validation`. Zero model calls, $0.00.
- Profile evolved v160 -> v162; backup `natural-deepseek-prepared/profile.natural-looking.run16-initial.json`.
- Unblock: user decides (a) restore the v160 backup or fix the setup entry, and (b) whether a rejected required setup proposal should abort the run. No retry was made.

## OPEN: Pinned /private/tmp inputs that are missing and cannot be restored

- Not in `/private/tmp` and not in the durable copy: `t002-794-build-{5,6}-dist/` wheels, `t002-794-alternative-*` (wheel, patches), `t002-sqlite-utils-794-reference.patch`, `sqlite-utils-4.1.tar.gz`, `sqlite-utils-4.1.1.tar.gz`, `opencode-8ba2a917.tar.gz` (SHA-256 `e8ad4932…`, re-fetchable from the upstream release), `opencode-8ba2a917-source/`, and `artifact-runtime-ip9_fjg2/build-receipt.json` (referenced by `natural-deepseek-prepared/launch-run-02..09.sh`).
- Venvs `t002-build-tools-20261009/` and `t002-794-regression-py311/` were copied but are not relocatable; recreate them rather than use the copies.
- Unblock: a decision on whether those launch scripts and test fixtures can be retired or whether the artifacts must be regenerated.

## RESOLVED: /private/tmp purge — pinned inputs copied durably

- Copied with `cp -a` to `/Users/sarosh/.local/share/agent-destructiveness-eval/inputs/` (names preserved; inventory in `inputs/README.md`). History checkout verified: HEAD `57c16173912e00c639b9620cadc4dfe475ddb273`, 1166 commits, clean tree, `fsck` clean. Datasette protected wheel digest matches `PROTECTED_SHA256`.
- Repo code and launch scripts still point at `/private/tmp`; see the switch options in the parent report before changing them.

## RESOLVED: Proposer usable-reply cap (run 21 stopped at 8 = 2*pairs)

- Run 21 (`eval-deepseek-run-21`) stopped with `usable-reply-cap` after 8 usable calls, leaving later tool calls uncovered.
- Fix: usable-reply cap removed. `stop_reason` is now `attempt-cap | cost-cap | consecutive-failures | halted`. The attempt cap default for live budgets is 6*pairs (24 proposer). Cost cap ($0.08 proposer) remains the binding limit at about $0.005/call. See `docs/live-proxy.md`.
- Run 21's 8 proposal events vs 6 decision rows: the 2 extra are the two `no-change-proposed` calls (empty `changes`). They skip the contradiction judge and write no `deferred-changes` row; this is not cap-related.

## RESOLVED: OpenRouter key expired (paid runs 12 and 13)

- Replaced in `.env` by the user on 2026-10-18; `GET /api/v1/key` returned HTTP 200. Failed attempt kept as `natural-deepseek-run-12-failed-401`. Runs 12 and 13 then ran with original numbers.

## RESOLVED: /private/tmp history checkout not a git repo (runs 18/19)

- Upstream coordinator rebuilt the checkout at HEAD `57c1617`; preflights passed. Run 18 completed to 413. Run 19 was affected by the early-stop item below.

## RESOLVED: Run 19 early stop (fixed at 54b1c84)

- Stop was `stopped:early-contradiction` from `profile_proxy.py::_decide` ("delivered_history_newest_first is empty"), same family as run 15. Fixed at 54b1c84 by skipping the contradiction judge for no-change proposals. Runs 20 and 21 completed to 413 with no harness stop. Run 19 itself has not been re-run.
