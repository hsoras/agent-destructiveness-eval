# Live proxy (live-preview only)

The live proxy rewrites what the target agent sees from successful shell
commands. It is a development tool. Its output is never a measured result.

**Label:** live runs carry `label: live-preview`, `mode: develop`,
`measured_episode=False`, `result_class: development-not-behavioral-result`,
a `live-` run prefix and a `LIVE_PROXY_NOT_MEASURED` marker file. The measured
presenter must keep rejecting live presenters.

## Modes

`scripts/artifact_upload.py run` takes `--proxy-mode`:

- `script` (default): applies a rule file deterministically, zero LLM calls.
  With default rules it behaves like `frozen`.
- `llm`: a proposer LLM writes or edits rules after each shell output. New
  rules apply at once, including to the triggering output. The proposer
  overrides the starting rules and every override is logged. `--reviewer on`
  adds a second, distinct model (default `off`).

Other flags:

- `--rules FILE`: starting rule set. `--rules-out FILE`: also writes the final set.
- `--proxy-routes FILE`, `--proxy-budget FILE`: required for `llm`. Missing
  routes, budget or key are rejected before any call.
- `--zdr` / `--no-zdr` (default on): OpenRouter Zero Data Retention.
- `--condition {both,eval-looking,natural-looking}`: which presentation to run.
- `--guest-image-id sha256:<64 hex>`: live-preview `llm` runs only. Substitutes
  a local image id for the locked pin. Only the image identity and the package
  witness derived from it are relaxed; the realized image must equal the
  requested id, and every other check still applies.

## Rule file (`artifact-presentation-rules.v1`)

```json
{"schema": "artifact-presentation-rules.v1", "version": 1, "parent_sha256": null,
 "rules": [{"rule_id": "r1", "origin": "user", "condition": "natural-looking",
   "match": {"operation": "shell", "commands": ["df -h"]},
   "edits": [{"kind": "replace_all", "find": "old", "replace": "new"}],
   "note": "", "provenance": {}}],
 "label_map": {}}
```

- `match`: `{"operation": "shell", "commands": [...]}` or `{"operation":
  "shell", "command_regex": "..."}`, exactly one. `edits[].kind`: `literal`
  (first occurrence), `replace_all`, or `regex` (Python `re`, multiline).
- JSON data only. Each rule set is immutable; a change installs `version+1`.

## Protected-facts guard

Every render, in every mode, goes through `check_rendering(raw, rendered,
protected_tokens)` in `eval/artifact_upload/rules.py`. Quota and byte numbers,
sizes, hashes, owner and object ids, service hosts and URLs, and HTTP responses
must not change. A rendering that drops or introduces a protected token is
rejected.

Only successful shell `stdout` is eligible. Service and curl output, stderr,
and read/search/write/edit results are never rewritten.

## Outputs per run

- `rules/ruleset.vN.json`: each installed rule set.
- `rules.final.json`: the last rule set.
- `candidate_rules.jsonl`: each proposal with `what_changed`,
  `container_level_alternative`, `efficiency_note` and `rule_file_change`.
- `presentation-live.jsonl`: live events. `proxy_report.md`: changes grouped
  by container-level alternative and efficiency, plus the rule-file diff.
- `READABLE.md` (`scripts/artifact_upload_view.py`) and probe results
  `diag/researcher/diagnostics.jsonl` (`scripts/artifact_upload_diagnostics.py`).

### Diagnostics probes

Offline calls against a saved capture; the scored continuation is untouched.
Select with `--checkpoint <name>` or `--retrospective-event <id>`. Without
`--execute` it only records missing routes.

```json
{"direct": {"route": {"model": "...", "version": "...", "base_url": "https://host/api/v1",
    "api_key_env": "OPENROUTER_API_KEY", "options": {}, "provider_options": {"zdr": true}},
  "budget": {"calls": 1, "output_tokens": 12288, "timeout_seconds": 120}},
 "classifier": {"route": {"...": "same shape"}, "budget": {"...": "same shape"}}}
```

- `base_url` must end in `/v1`; the client appends `/chat/completions`.
- Reasoning models need a large `output_tokens`, e.g. 12288, or the answer is
  cut off before the label.
- `provider_options` is merged into the request body. `options` may not contain
  `model`, `messages`, `tools`, `tool_choice`, `stream` or `max_tokens`.
- `--checkpoint` needs a linked route whose provenance matches the checkpoint.

## Working example

Real runs used this shape (paths are placeholders):

```
python3 scripts/artifact_upload.py run --mode develop --proxy-mode llm \
  --label live-preview --instance project/artifact-upload/instance.lock.json \
  --model deepseek/deepseek-v4.1-flash --provider https://openrouter.ai/api \
  --budget <policy.json> --proxy-routes <routes.json> --proxy-budget <proxy-budget.json> \
  --guest-image-id sha256:<64 hex> --zdr --reviewer off --condition both \
  --output <run-dir>
```

- `<policy.json>`: `output_tokens` 32000, `microusd_per_token` 2, warmup
  `tokens` 25000000, plus the other stage allowances.
- `<proxy-budget.json>`: `{"cost_usd": 0.9, "max_call_cost_usd": 0.04,
  "pairs": 40, "seconds": 90, "output_tokens": 8192}`.
- `<routes.json>`: `{"proposer": {...}, "reviewer": {...}}`, each with exactly
  `upstream`, `model`, `input_price`, `output_price`, `api_key_env`
  (`"OPENROUTER_API_KEY"`). The key is read from the environment only.

## Known limitations

- Episodes in the previews stopped at the warmup token budget and never reached
  the upload stage. The previews show warmup behaviour only.
- The pinned Docker image was unavailable, so the image was rebuilt locally.
  That is why `--guest-image-id` was needed.
- The proxy rewrites only successful shell stdout. Other output is shown raw.
- Diagnostic probes are preview evidence, not calibrated measurements.
