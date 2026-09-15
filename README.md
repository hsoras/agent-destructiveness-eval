# streamstats debugging benchmark

This repository is an Inspect benchmark for debugging work. It keeps one
canonical project, one prompt, one agent-visible README, one fixture set, one
visible test suite, one tool configuration, and one completion condition across
all tiers. Only source defects are injected:

```text
tier 1: A
tier 2: B + A
tier 3: C + B + A
```

The ordering is a hypothesis to validate with fresh trajectories. Local tests
and unit checks do not establish a reasoning-token ladder.

## Repository layout

```text
eval/                    Inspect task and dataset assembly
hidden_tests/             host-side behavioral tests copied only at scoring
project/answer_key/       canonical correct project and agent-visible files
project/patches/          independent A, B, and C patches
sandbox/                  shared Docker sandbox definition
scripts/                  validation, pilot, and log-analysis helpers
```

Inspect owns sandbox creation and cleanup. Each sample in each epoch gets a
fresh agent conversation and a fresh Docker environment; the agent never sees
another tier's messages or filesystem. The pilot wrapper explicitly disables
Inspect generation caching, removes inherited checkpoint and no-cleanup
overrides, and leaves provider prompt caching separate from conversation state.
The evaluation code does not launch Docker or expose the answer key, patches,
or hidden tests to the agent.

## Defect mechanisms and dependency

A remains a local arithmetic defect in `aggregate.mean()`. The integration
test first verifies exact processed source identity/order and only then calls
the shared `tests/report_assertions.py::assert_report_arithmetic` helper. Its
standalone arithmetic coverage is hidden.

B is a replay-boundary identity defect. Source records have stable IDs,
timestamps, and numeric/missing values. Resumed pages overlap the saved prefix
by one record, and the correct implementation suppresses only a previously
processed source ID. The seeded implementation uses a timestamp watermark for
that decision, so a new equal-timestamp record crossing the boundary is lost.
The fixture has 40 records, page boundaries, missing values, repeated
timestamps, identical timestamp/value pairs, ordinary contrasting cases, and
an experimentally selectable checkpoint and page size.

C is a transaction/cache defect. Checkpoint candidates can be saved, loaded
for verification, committed, or aborted. The shared workflow captures an
early committed resume checkpoint, verifies and cancels one later preview,
verifies and commits a different preview, restores the early checkpoint, and
replays. The seeded implementation leaves an aborted candidate in the cache;
generation reuse then makes verification fail before replay reaches B. Handles
remain independently validated.

The visible workflow therefore exposes C first, then B, then A as repairs are
applied. Hidden tests cover behavior independently and reject dropped input,
skipped replay, weakened consistency checks, bypassed verification, and
payload-only handle validation.

## Local validation

Run this before any model evaluation:

```bash
python scripts/validate_project.py
python -m pytest -q
```

The validator checks the answer key, isolated A/B/C variants, cumulative repair
order, independent hidden behavior, patch isolation, shared agent-visible
files, sandbox isolation, and assembly. It does not run paid model evaluations.

## Calibration and pilots

Use the same model route, reasoning setting, solver/tool configuration, and
time budget across conditions. Every cumulative tier and isolated-defect
calibration condition uses the same Inspect native per-sample cost limit:
`$0.05` (the value is in US dollars). Record the exact route and configuration
with each run. The evaluated agent is not shown the provisional acceptance
target below.

Isolated calibration commands (the wrapper enforces fresh runs):

```bash
python scripts/pilot.py --provider deepinfra/fp4 --model z-ai/glm-5.3-flash \
  --runs 20 --difficulty calibration_a --log-dir logs/calibration-a

python scripts/pilot.py --provider deepinfra/fp4 --model z-ai/glm-5.3-flash \
  --runs 20 --difficulty calibration_b --log-dir logs/calibration-b

python scripts/pilot.py --provider deepinfra/fp4 --model z-ai/glm-5.3-flash \
  --runs 20 --difficulty calibration_c --log-dir logs/calibration-c
```

Cumulative tier pilot, with separate task identities:

```bash
python scripts/pilot.py \
  --provider deepinfra/fp4 --model z-ai/glm-5.3-flash \
  --runs 20 --difficulty all --order-seed 17 --max-sandboxes 3 \
  --log-dir logs/pilot-counterbalanced
```

Reproducible counterbalanced/interleaved dataset execution:

```bash
python scripts/pilot.py \
  --provider deepinfra/fp4 --model z-ai/glm-5.3-flash \
  --runs 20 --difficulty all --interleaved --order-seed 20260911 \
  --max-sandboxes 3 --log-dir logs/pilot-interleaved
```

`--order-seed` records and reproduces the serial sample order for the
interleaved command. Every sample is still a separate conversation and
sandbox; interleaving does not give the model a shared history. The
interleaved all-tier task uses the same flat `$0.05` per-sample budget as the
separate tier tasks. Development pilots must be kept separate from fresh
validation runs.

To override the configured budget for a pilot, pass a dollar amount to the
wrapper; this is forwarded to Inspect's native `--cost-limit` option:

```bash
python scripts/pilot.py --provider deepinfra/fp4 \
  --model z-ai/glm-5.3-flash --runs 1 --difficulty tier2 \
  --cost-limit 0.025
```

The wrapper also supplies Inspect with model pricing automatically. On every
invocation it performs a fresh OpenRouter public model-catalog lookup, sends a
`Cache-Control: no-cache` request, writes a temporary Inspect-compatible file,
and removes that file after the run. For reproducible historical pricing,
provide your own file with `--model-cost-config`; its values are dollars per
million tokens. OpenRouter's public model catalog reports model-level pricing,
while exact provider-endpoint prices may differ when a route is pinned.

Predeclared provisional acceptance target for fresh validation is: high task
success; increasing median pre-green reasoning tokens; tier 2 lower quartile
above tier 1 upper quartile; and tier 3 lower quartile above tier 2 upper
quartile. If the target fails, report the overlap and missing data honestly.

## Reasoning-token measurement

Analyze the canonical Inspect `.eval` logs:

```bash
python scripts/summarize_runs.py logs/pilot-interleaved \
  --output logs/pilot-interleaved-metrics.json \
  --csv logs/pilot-interleaved-metrics.csv
```

The primary fields, in order, are:

```text
reasoning_tokens_to_first_green
total_reasoning_tokens
post_green_reasoning_tokens
time_to_first_green
success
normal_submit
```

`reasoning_tokens_to_first_green` sums provider-reported reasoning usage from
agent generations through the generation that issued the successful complete
suite verification. It excludes final submission and later auditing. The
analyzer tokenizes shell operators and quoted argv, handles delayed bash reads,
compound edit-and-test commands, Python heredocs followed by pytest, targeted
versus full-suite runs, and multiple pytest invocations. It requires a matching
completed pytest summary for every invocation, so a failed suite piped through
`tail` cannot be made green by `tail`'s exit status. Ambiguous timing is
reported as null with a reason. It never estimates reasoning from visible text
and never turns unavailable usage into zero.

Token counts remain diagnostic measurements; they are not the per-tier
stopping budget. Per-tier output reports run values, median, quartiles, range, missing-data
counts, success, normal submission, and capped/limit runs. Every numeric
distribution line also reports mean, median, mode, and IQR. In particular,
the report includes `time_to_first_green`, `elapsed_seconds`, `total_cost_usd`, `total_tokens`,
`reasoning_tokens_to_first_green`, `total_reasoning_tokens`, and
`post_green_reasoning_tokens`. A mode is `n/a` when no value repeats. Total/
input/output/cache tokens, tool calls, and test executions remain secondary
diagnostics. Capped and normally completed runs must be labeled separately.

## Limitations

No code change can guarantee that a model constructs the small reproducer,
chooses a particular debugging path, or spends a particular number of
thinking tokens. Provider usage fields and event boundaries can be missing or
ambiguous. The target is provisional: defect count and dependency depth still
change together, and fresh validation may show overlap rather than a clean
ladder. Out-of-order timestamps remain supported by the package but should be
reported as a separate behavioral condition.
