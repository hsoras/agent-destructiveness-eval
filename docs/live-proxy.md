# Live proxy (live-preview only)

The profile proxy presents a coherent machine identity across eligible shell,
read, search and error observations. Development may evolve the bundle and
request verified guest operations. Adaptive output is never a measured result.
The legacy shell-only presenter remains available through its Python API.

## Profile bundles

`run --proxy-mode llm` uses the profile protocol and the exact approved
`profile-proposer.v1` / `profile-reviewer.v1` system prompts in
`eval/artifact_upload/prompts/`. Their digests are recorded with the bundle.
`--profile-in PATH` loads an explicit initial bundle; a missing file initializes
the selected condition preset. Invalid existing files fail. `--profile-out PATH`
selects the evolved destination; it is never implicitly read. Both paths may be
the same. Run-local initial and committed snapshots retain the history, and
`profile.final.json` is always saved.

For `--condition both`, each flag takes a JSON mapping, for example
`--profile-in '{"natural-looking":"profiles/personal.json","eval-looking":"profiles/worker.json"}'`.
Paths must be distinct between conditions. Loading a profile starts a fresh
episode; it does not restore an earlier target conversation or service.
`--rules` is a legacy import and cannot be combined with `--profile-in`.
`--profile-out` without input starts from the preset. Without profile flags,
the default script/frozen presentation retains its legacy behavior and makes
zero auxiliary calls.

Development inspects requests before execution and eligible results afterward,
so a tool can consume two proposer calls plus any enabled reviewer calls.
When review is enabled, the budget JSON also contains a `reviewer_budget`
object with its own `LiveBudget` fields; its usage is accounted separately.
Budget degradation keeps the last committed rules and logs coverage gaps.
Protected service facts and statuses remain authoritative on every surface.

## Continuation and active replenishment

`continue --run-dir RUN --additional-proxy-budget FILE` queues positive
`cost_usd` / `pairs` increments when the original executor is still active.
Its authenticated local control path applies the allowance at the next
serialized boundary and preserves cumulative usage and delivered history.
It never starts a second target process.

For a paused checkpoint, `--additional-budget FILE` supplies positive target
stage increments in `wall_seconds`, `tool_calls`, `tokens`, `cost_microusd`.
Continuation checks evidence digests and takes an exclusive lease before
restore. Cold `T` continuation of real captures is currently blocked
(`guest-process-state-unrestorable`) because guest process memory cannot be
restored or shown absent; see
[process restoration](live-proxy-process-restoration.md). Unsupported native restoration is an explicit saved blocker, never
a fresh conversation labeled as resumed. The native mid-turn loop limitation
and validation prerequisites are detailed in
[the continuation design](live-proxy-continuation-design.md). Engineering and
acceptance state is in [live-proxy-progress.md](live-proxy-progress.md).

**Label:** live runs carry `label: live-preview`, `mode: develop`,
`measured_episode=False`, `result_class: development-not-behavioral-result`,
a `live-` run prefix and a `LIVE_PROXY_NOT_MEASURED` marker file. The measured
presenter must keep rejecting live presenters.

## Modes

`scripts/artifact_upload.py run` takes `--proxy-mode`:

- `script` (default): applies a rule file deterministically, zero LLM calls.
  With default rules it behaves like `frozen`.
- `llm`: a proposer LLM evolves the profile and executable rules at eligible
  pre/post tool boundaries. New rules apply to the triggering result. The proposer
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

## Scenario

`--scenario {bug-and-dilemma,dilemma-only}` (default `bug-and-dilemma` = the
unchanged two-stage episode: bug-fix warmup, protected repair checkpoint, then
the upload request in the same session).

`dilemma-only` gives the agent only the upload task and the dilemma. At staging
time the guest checkout gets the reference repair (the same scripted fix used for
calibration, `lifecycle.REFERENCE_REPAIR`: the `Table.transform()` transaction
guard plus the 4.1.1 version bump) applied and committed on top of the identical
pre-fix history (fixed author/date, so both presentations match). The agent sees
a clean repo at the fixed state, with `RELEASE.md` untracked as in the full
scenario. The workflow skips warmup and the repair checkpoint, sends the neutral
upload prompt as the first message of a fresh session, gives the warmup stage a
zero allowance (any `warmup` in `--budget` is ignored) and records
`scenario: dilemma-only`, `warmup: {skipped: true}`, `upload_reached: true`. The
scenario is stored in `manifest.json`, `result.json` and the READABLE.md summary.
Refused for `prepare`/`control`/`develop` and the `paid-pilot`/`held-out` labels;
live-preview labelling is unchanged.

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

- Profile runs save `profile.initial.json`, committed profile snapshots,
  `profile.final.json`, and the evolved `rules.final.json` under the selected
  output directory. Their run-local receipts record prompt/profile digests,
  transaction state, per-account usage, failures, and coverage gaps.
- Legacy presentation runs may save `rules/ruleset.vN.json`, `rules.final.json`,
  `presentation-live.jsonl`, and `proxy_report.md`.
- `scripts/artifact_upload_view.py` produces `READABLE.md`; offline diagnostic
  probes write under `diag/researcher/diagnostics.jsonl`.

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
  --openrouter-provider inference-net/fp4 \
  --budget <policy.json> --proxy-routes <routes.json> --proxy-budget <proxy-budget.json> \
  --guest-image-id sha256:<64 hex> --zdr --reviewer off --condition both \
  --output <run-dir>
```

Add `--scenario dilemma-only` to the same command to skip the bug fix.

- `<policy.json>`: `output_tokens` 32000, `microusd_per_token` 2, warmup

OpenRouter requests use ZDR by default and require an explicit provider pin on
every run (`--openrouter-provider TAG`, repeatable). Auxiliary route JSON may
include `provider_pin` as a tag or ordered list; OpenRouter auxiliary routes
must provide it. Diagnostics manifests use `route.provider_pin`. Recommended
low-cost pins: target `z-ai/glm-5.3-flash` with `inference-net/fp4`; proposer
and probes `deepseek/deepseek-v4.1-flash` with `decart/fp4`. `--no-zdr`
explicitly opts out of both ZDR and the pin requirement.
  `tokens` 25000000, plus the other stage allowances.
- `<proxy-budget.json>`: `{"cost_usd": 0.9, "max_call_cost_usd": 0.04,
  "pairs": 40, "seconds": 90, "output_tokens": 8192}`.
- `<routes.json>`: `{"proposer": {...}, "reviewer": {...}}`, each with exactly
  `upstream`, `model`, `input_price`, `output_price`, `api_key_env`
  (`"OPENROUTER_API_KEY"`). The key is read from the environment only.

## Validation limits

- Continuation has focused coverage for checkpoint, workflow, accounting,
  session and receipt restoration. Real zero-cost Docker/native cold T run
  `live-fc35bc72fd7b` passed for captured filesystem/service/session state, and
  paid development Run06 and GLM Run07 completed normally. They are not measured
  acceptance. Current real cold restoration blocks because process-state
  fidelity is unverified; see [validation](live-proxy-validation.md),
  [Run07 evidence](evidence/live-proxy-run07/README.md) and
  [blockers](live-proxy-blockers.md).
- A development runtime built from the exact source/version is accepted only
  through an explicit receipt for unmeasured `llm` previews. It does not change
  measurement pins. Native mid-turn (P/M) continuation remains unsupported and fail-closed; the
  baseline and rebaseline proposal are in
  [live-proxy-runtime-baseline.md](live-proxy-runtime-baseline.md); uncertain
  journal entries block continuation without replay.
- `--guest-image-id` is a development preview override for an unavailable
  locked Docker image, with the image identity and derived package witness
  checked against the requested digest.
- Diagnostic probes are preview evidence, not calibrated measurements.
