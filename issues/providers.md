# Provider reliability log

One row per run or probe that reached a provider. Sources: `/private/tmp/live-runs/<run>/` (`result.json`, `results.json`, `manifest.json`, `presentation-live.jsonl`, `capture/*/run_state.jsonl`, `capture/*/workflow_events.jsonl`) and the committed `logs/` copies. "Served" counts come from `served_providers` in `result.json` (model calls per provider). "Reported" cost is `reported_cost_usd` in `result.json`. "Turn-usage" cost is the sum from `result.json` turn usage, as quoted in `bugfix-stage.md`. The two cost figures disagree for some runs, so both are shown.

Outcome values: `success` (normal_completion), `failure` (infrastructure_failure, crash, or rejected before a model call), `budget` (budget_exhausted; the provider did not fail, a cap stopped the run), `partial` (probe or aux call that failed or timed out but the run continued).

## Price reference ($ per million tokens, in / out)

| Model | Provider pin | In | Out | Source | Notes |
|---|---|---|---|---|---|
| `z-ai/glm-5.3-flash` | `inference-net/fp4` (InferenceNet) | 0.07 | 0.05 | `ISSUES.md` operator notes (doc figures, not re-checked on the key page) | Target model in k, m, n, p, q runs |
| `deepseek/deepseek-v4.1-flash` | `decart/fp4` (Decart) | 0.09 | 0.18 | `routes2.json` (`input_price` 9e-8, `output_price` 1.8e-7) | Proposer and classifier |
| `deepseek/deepseek-v4.1-flash` | unpinned (earlier runs) | ~0.08 | ~0.55 | Operator note (quoted, basis not recorded) | Too expensive; led to the pin |
| `deepseek/deepseek-v4.1-flash` | unpinned, `routes.json` | 0.45 | 1.80 | `routes.json` config values | Does not match the rows above. Check which figure is right before using config prices for budget math |
| Qwen3.7-flash | n/a | n/a | n/a | n/a | Not on the OpenRouter ZDR list. Not used. No price recorded |

Pricing caveat: the glm price is quoted from docs, and the deepseek pinned price matches the config. Recheck both on the OpenRouter key page before any cost claim.

## Log

| Date | Run / probe | Role | Model | Provider (served) | Outcome | Error | Notes |
|---|---|---|---|---|---|---|---|
| 2026-10-11 | a1, b1, c1 | target | `openai/gpt-oss-120b` | none (no model call) | failure | none | Crashed at prepare, docker image, and slash model id (blockers 1–3). Fixed in code |
| 2026-10-11 | d1 | target | `deepseek/deepseek-v4.1-flash` | not recorded | budget | none recorded | 1.2M warmup policy. Reported $0.0038 (leaf -0-1) |
| 2026-10-11 | e1 | target | `deepseek/deepseek-v4.1-flash` | not recorded | failure | none | Rejected at first request by the output-token bound (config, 8000 < 32000). Not a provider failure |
| 2026-10-11 | f1 | target | `deepseek/deepseek-v4.1-flash` | not recorded | budget | none recorded | Reported $0.0215 (eval) and $0.0082 (natural) |
| 2026-10-11 | g1 eval | target | `deepseek/deepseek-v4.1-flash` | not recorded (manifest has no pin) | budget | none | 70-call warmup cap. Reported $0.0523; aux 18 calls, 141 s |
| 2026-10-11 | g1 natural | target | `deepseek/deepseek-v4.1-flash` | not recorded | budget | none | Reported $0.0150 |
| 2026-10-11 | h1 natural | target | `deepseek/deepseek-v4.1-flash` | not recorded | budget | none | Reported $0.0567 |
| 2026-10-11 | i1 natural | target | `deepseek/deepseek-v4.1-flash` | not recorded (manifest has no pin) | budget | none | 70-call warmup cap. Reported $0.0651; aux 22 calls, 170 s |
| 2026-10-11 | j-eval, j-natural | target | `z-ai/glm-5.3-flash` | none served | failure | `ValueError` before any model call | Wiring bug in `scripts/artifact_upload.py` (blocker 7, fixed `d25d103`). Not re-run |
| 2026-10-11 | diag-repro-1..3 | probe | `z-ai/glm-5.3-flash` | none served (`{}`) | failure | not extracted | Probe repro runs. Re-check the error before citing |
| 2026-10-11 | k-eval | target | `z-ai/glm-5.3-flash` | InferenceNet (79) | budget | none | Token meter (99 requests). Reported $0.0105 |
| 2026-10-11 | k-natural | target | `z-ai/glm-5.3-flash` | InferenceNet (49) | budget | none | Token meter (81 requests). Halted after 1 aux call. Reported $0.0000 |
| 2026-10-11 | m-eval | target | `z-ai/glm-5.3-flash` | InferenceNet (52) | budget | none | Upload tool-call cap 60. Reported $0.0123 |
| 2026-10-11 | m-natural | target | `z-ai/glm-5.3-flash` | InferenceNet (40) | success | none for target; aux `TimeoutError: timed out` at 90.2 s on Decart (proxy halted) | Reported $0.0 in `result.json`; turn-usage sum $0.076. Proxy halt was on the aux side, not the target |
| 2026-10-11 | n-eval | target | `z-ai/glm-5.3-flash` | InferenceNet (99) | budget | none | Manifest cap 160 calls / 3000 s. Reported $0.0102 |
| 2026-10-11 | n-natural | target | `z-ai/glm-5.3-flash` | InferenceNet (45) | success | none | Reported $0.0054; turn-usage sum $0.080 |
| 2026-10-11 | diag-eval, diag-natural | probe, direct | `z-ai/glm-5.3-flash` | InferenceNet (2) | success | none recorded | Direct probe repeats the original model and pin |
| 2026-10-11 to 12 | probes for m-, n-, p1-, q1- runs | probe, classifier | `deepseek/deepseek-v4.1-flash` | Decart (1 per probe run) | partial | transient 502 on at least one probe (operator report; the probe was re-run by hand) | Classifier needs `output_tokens` 24000. Probes are one-shot, no upstream retry (`ef34ac8`) |
| 2026-10-12 | p1-eval | target | `z-ai/glm-5.3-flash` | InferenceNet (57) | success | none | Reported $0.0139; turn-usage sum $0.108. Aux proposer returned "no change" 22/22 (Decart) |
| 2026-10-12 | p2-eval | target | `z-ai/glm-5.3-flash` | InferenceNet (21) | failure (after `upload_reached`) | HTTP 502 `{"code":502,"message":"Upstream error from InferenceNet: Internal server error","metadata":{"error_type":"provider_unavailable"}}` | Ran 00:43–00:51 local, before the retry guard merged (`411d88b`, 01:05 -0700). Not retried. Reported $0.0040. Exclude from measurements |
| 2026-10-12 | q1-eval | target | `z-ai/glm-5.3-flash` | InferenceNet (23) | success | none | Reported $0.0097; turn-usage sum $0.026 |

Summary (this log only):

- InferenceNet served all target calls on the glm runs. Served call counts were 21 to 99 per run. One provider 502 (p2) ended a run as `infrastructure_failure`.
- Decart served the aux proposer and the classifier. The one aux failure was a timeout (90.2 s, m-natural), not a 5xx.
- The deepseek target runs (d1 to i1) did not record a served provider, and their manifests have no provider pin. Treat those rows as unknown provider.
- Counting `502` by grep is not reliable: the string appears in unrelated source text in several runs. Only the structured `Upstream error from ...` record counts.

## How to append a row

1. Open `result.json` (leaf) and `results.json` (run dir). Copy `termination_reason`, `served_providers`, and `reported_cost_usd`.
2. Search the leaf for the structured error only: `grep -rl "Upstream error from" <run>` and `grep -rl provider_unavailable <run>`. Quote the message in the Error column.
3. For probes, read `diag-*/researcher/diagnostics.jsonl` and the manifest `direct` and `classifier` routes.
4. Add the row in date order. Put the turn-usage cost in Notes if it differs from the reported cost.
5. If a new model or pin is used, add a line to the price reference table first, with its source.
6. Do not paste keys, `.env` contents, or full request bodies. Use the run id and file path instead.
