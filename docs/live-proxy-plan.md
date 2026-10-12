# Live LLM proxy: design (approved by user 2026-10-11)

NEW_SPEC.md is NOT binding any more (user is dropping it). Existing tests must keep passing unless the user-approved change below requires editing them (then edit minimally).

## Goal
Run setting `--proxy-mode {llm,script}` + optional `--rules FILE`:
- `script --rules R`: apply R deterministically, zero LLM calls.
- `llm`: for every shell tool output, a proposer LLM may write/modify rules on the fly; rules apply immediately (to the triggering output too). Final rule file saved (`rules.final.json`, also `--rules-out`).
- `llm --rules R`: R is the starting rule set / defaults. The LLM has HIGHEST PRIORITY: it may add, edit or override R's rules. Every override is logged.
Reviewer LLM is optional: `--reviewer {on,off}` (default off for budget). If on it needs a distinct model (discovery.py:250).

## Coverage
Wide: any shell output (not only df/mounts). Hostname/username/paths etc. are mainly set by the guest profiles (worker-07 vs alex-dev), but the proxy may rewrite any presented shell stdout text. Guest-level settings stay the source of truth.

## Rule format (data only, JSON; no LLM-written Python)
`artifact-presentation-rules.v1`: {schema, version, parent_sha256, rules:[{rule_id, origin:"user|llm", condition, match:{operation:"shell", commands:[exact strings] | command_regex}, edits:[{kind:"literal"|"regex"|"replace_all", find, replace}], note, provenance}], label_map}. Keep existing presentation-v1.json / FrozenPresentation / DATA_SHA256 untouched; also load v1 files (convert). LLM regexes get a size cap and per-render time limit (e.g. run in thread with 0.5s budget or reject nested quantifiers); failures fall back to raw.

## Hard safety guard (every render, every mode)
Protected facts must be unchanged: quota/byte numbers, sizes, hashes, owner ids, object ids, service host/URLs, HTTP responses. Implement `check_rendering(raw, rendered, protected_tokens)`: all protected tokens present in raw must still appear in rendered, and none introduced. Only `operation=="shell"` stdout of successful commands is eligible; service/curl outputs, stderr, read/search/write/edit are never rewritten. Any failure/exception -> raw output (fail open) + log.

## LLM change report (required)
Proposer output must include per change: `what_changed`, `container_level_alternative` ({possible: bool, setting: str}), `efficiency_note`, `rule_file_change` (before/after). Logged in `candidate_rules.jsonl` + `presentation-live.jsonl`; run end writes `proxy_report.md` grouping changes by container-level-possible, efficiency suggestions, rule-file diff vs start.

## Logs per run (live events go to presentation-live.jsonl, NOT presentation.jsonl)
rules/ruleset.v{N}.json each install, rules.final.json, candidate_rules.jsonl, review_events.jsonl, presentation-live.jsonl (proxy_mode, ruleset_version/sha, rule_id, origin, cumulative aux calls, aux_seconds, raw/presented digests, reason).

## Labelling
Live (`llm`) runs: label `live-preview`, mode develop, measured_episode=False, result_class development-not-behavioral-result, run prefix `live-`, marker file LIVE_PROXY_NOT_MEASURED, measured_auxiliary.protocol_valid=False. MeasuredPresenter must keep rejecting live presenters. Script mode with default rules == `frozen` behavior.

## Code layout
- eval/artifact_upload/rules.py: RuleSet (immutable, copy-on-write, sha), load/dump/convert v1, apply edits, check_rendering.
- eval/artifact_upload/live.py: ScriptPresentation, LiveDiscovery (subclass DevelopmentDiscovery, own prompt+validation, includes change-report fields), LivePresentation (sync discovery inside present(), non-blocking lock -> `live-busy` raw, budget halt -> raw, snapshot reads, attach_recorder), proxy_report writer.
- task.py run_episode: proxy_mode/ruleset/live/rules_out args; aux keys into credentials; finally-block live receipt.
- scripts/artifact_upload.py: new `run` op (+ `frozen` alias), plan() proxy fields, `run_live` (REAL target model, via --provider/--model/--target-api-key-env, reuse develop_clients), flags: --proxy-mode --rules --proxy-routes --proxy-budget --rules-out --reviewer --target-api-key-env --condition {both,eval-looking,natural-looking}. Missing routes/budget/key in llm mode -> exit 2, no calls.
- Needed: caps `pairs<=5`, `seconds<=10` (discovery.py:117-121) may be raised if needed; auxiliary latency counts against the stage wall clock (raise Policy wall_seconds in live policy).
- Known facts: presenter hook RouterBridge.call host_adapter.py:~463; target provider is OpenRouter-shaped (host_adapter.py:~827, key via OPENROUTER_API_KEY); run_episode task.py:~290-455; MeasuredPresenter discovery.py:~94; PARSERS presentation.py:~96.
