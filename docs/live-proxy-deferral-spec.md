# Live proxy: contradiction-safe edits and next-run deferral (spec)

Status: approved design (grill session). Applies to `--proxy-mode llm` live previews only; never a measured result.

## Goal
A proposed profile/rule/guest-op change either applies now (no contradiction with anything the agent was already shown), is pocketed for the next run (contradiction), is dropped (conflicts with a real agent action), or stops the run (contradiction early in the run).

## Terms
- Delivered output: what the agent was actually shown (post-rewrite) for every tool call, including service HTTP responses and write/edit results.
- Raw output: pre-rewrite text. Harness-only, used for replay. Never given to the judge.
- Guest change: a verified edit to the sandbox (git_attach_head, files_normalize_untracked, env_set_variables). Rules only change display.

## Data
1. Exposure log (per run, run dir `exposure-log.jsonl`): per tool call: call_id, tool, request summary, raw output, delivered output, rules applied. Fills `presented_facts` (derived from delivered outputs; keep it small and per-surface).
2. Profile bundle gains `pending`: list of `{pending_id, kind: rule|guest_op, change, source_run, source_proposal_sha256, reason, created_at}`. Backward compatible load (missing = empty); schema version bump.
3. confirmed_state updated after any call that can mutate the guest: diff git refs/status, hashes of touched files, env; record changes with provenance. Real agent actions are authoritative.

## Decision flow per proposal (post-execution, synchronous)
1. Conflicts with confirmed_state (a real agent action)? DROP, log `dropped:conflicts-confirmed-state`. Never pocketed.
2. Rule change: replay the candidate ruleset over every raw output in the exposure log. If any delivered output would differ: CONTRADICTION.
3. Guest change: dry-run to list touched paths/refs/vars. If any was shown to the agent (appears in a delivered output) or is in confirmed_state: CONTRADICTION (conflict with confirmed_state is DROP per step 1).
4. If no contradiction after steps 2-3: run the judge (extended `profile-reviewer.v1`). Judge sees the proposal, profile, protected facts, and DELIVERED outputs only (newest first, redacted like proposer input, size capped; oldest dropped and the judge is told). Judge answers no-contradiction / contradiction / unsure. Truncated history touching dropped calls = unsure. Timeout, transport error, soft failure, per-tool deadline exhaustion, budget stop, or malformed reply = UNAVAILABLE (infrastructure, not a contradiction).
5. No contradiction: apply now, save to profile. Judge unavailable: POCKET (`deferred:judge-unavailable`, coverage gap `judge-unavailable`) and CONTINUE, early or not; the run is not tainted. Contradiction or unsure (real verdict, or deterministic replay/touch-set collision): if EARLY, STOP RUN; else POCKET + mark run tainted-noted in report.
6. EARLY = before the agent's first request to the upload service AND within the first 8 tool calls. Stop = end the episode with result status `stopped:early-contradiction`, save pending items to the profile, do NOT auto-restart (cost).
7. A contradiction that already reached the agent (two inconsistent things were shown) marks the run `tainted` in the report; the run continues.
8. Every pocket/stop/drop/apply is written to the logs with reason (diagnostics + `proxy_report.md` section "Deferred / dropped / stopped changes").

## Next-run activation
At staging, before the agent's first call: re-check each pending item against current confirmed_state and protected facts; promote valid ones into ruleset/guest_setup (guest ops verified as today); drop and log stale ones.

## Judge
Model: same as proposer by default (route `reviewer`; recommended deepseek-v4.1-flash, pin deepinfra/fp8, ZDR). Own `reviewer_budget` (separate accounting, 30s per-call cap). Proposer (60s cap) and judge run sequentially inside one tool call under a 90s per-tool aggregate proxy deadline, kept well below the bridge client's patience (~264s); exhaustion is a soft failure (`coverage-gap:deadline`) and the tool result is still delivered. Runs only when a proposal exists and passed steps 1-3 (most calls pay nothing). Never receives raw output.

## Out of scope
Async proposer (rejected), label/parser fact extraction, auto-restart, changing protected-facts guard, changing eval-looking.

## Acceptance
- Unit tests for each branch above (apply, drop, pocket, stop-early, tainted, stale pending dropped at staging, unsure on truncation; judge failure => pocket + continue).
- Replay test: a rule that would alter a past delivered output is pocketed; a rule touching only unseen output applies.
- Offline: no paid/LLM calls in tests (judge client mocked). Focused tests pass vs the environmental baseline.
- Docs updated (docs/live-proxy.md, docs/live-proxy-prompts.md for the judge prompt change; prompt digest recorded).
