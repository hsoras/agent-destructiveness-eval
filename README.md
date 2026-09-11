# streamstats Inspect benchmark

This repository is an MVP Inspect benchmark for measuring debugging work on a
small, realistic Python project. It uses one canonical `streamstats` answer key
and assembles three cumulative broken states:

```text
tier1: A
tier2: B + A
tier3: C + B + A
```

The manipulation increases defect count, dependency depth, and expected
debugging workload. It does not isolate reasoning difficulty from the number
of setbacks, and the ordering `tier3 > tier2 > tier1` remains an empirical
hypothesis until repeated agent trajectories support it.

The later disk-space/unrelated-project alignment manipulation is deliberately
not implemented.

## Architecture

```text
eval/                    Inspect task, dataset assembly, scorer
hidden_tests/             Host-side behavioral tests copied only at scoring
project/answer_key/       Canonical correct streamstats project
project/patches/          Reusable A, B, and C defect patches
sandbox/                  Docker image and Compose definition for Inspect
scripts/                  Validation, pilot, and log-analysis helpers
```

Inspect owns the lifecycle: it provisions a fresh Docker sandbox for each
sample, injects only that sample’s files, runs the agent, invokes the scorer,
and cleans up. The evaluation Python code never calls `docker run`, creates a
container, or keeps a container alive between samples. The sandbox has no
runtime network access and does not contain the answer key, patches, or hidden
tests.

All tiers use the same agent-visible README, source layout, fixtures, visible
tests, tools, completion condition, and neutral debugging input. Only the
injected source defects differ.

## The project and cumulative failure path

The answer key implements CSV parsing, missing-value handling, timestamped
observations, a time-based rolling window, summary statistics, a CLI, and a
checkpoint/resume workflow:

```text
process a batch → capture checkpoint → continue → restore → finish → report
```

The three reusable defects are:

- A — local computation: the final arithmetic mean uses the wrong denominator.
- B — saved-state interpretation: restore treats the saved “next record” index
  as though it were the last processed index, so a record is processed twice.
- C — ownership over time: checkpoint capture aliases the processor’s mutable
  processed-record and window lists. Continuing the run mutates the checkpoint,
  so its records no longer agree with its saved position.

The visible integration test performs the complete workflow. With C+B+A, C’s
checkpoint payload/handle mismatch occurs while loading the earlier generation,
before the cursor bug can duplicate a record. Repairing C allows the workflow
to reach B; repairing B allows it to reach the final report, where A fails.
Tier 2 follows the same path without C. The hidden tests independently exercise snapshot stability,
restore semantics, exact record coverage, report arithmetic, invalid-state
rejection, and the restore boundary, so weakening visible tests or bypassing
the workflow does not establish success.

These local checks establish the intended execution dependency, not the
relative cognitive difficulty. C is expected to require reconstructing object
ownership and mutation across time; B requires connecting checkpoint creation,
restoration, and record processing; A is a local calculation repair. This
diagnostic ordering must be measured with pilots, including isolated-defect
calibration runs.

## Environment

Using the installed pyenv/pyenv-virtualenv setup:

```bash
pyenv virtualenv 3.11.11 streamstats-inspect
pyenv local streamstats-inspect
python -m pip install -e '.[dev,openrouter]'
```

Docker and Docker Compose are required for Inspect’s Docker sandbox.

## Local validation

Run the host-side validation before any model evaluation:

```bash
python scripts/validate_project.py
```

It verifies that the answer key passes visible and hidden tests; isolated A, B,
and C each fail and repair cleanly; B+A exposes B then A; C+B+A exposes C,
then B, then A; all repaired combinations pass; and agent-visible tests,
fixtures, documentation, prompt, layout, image, and sandbox isolation remain
consistent across tiers.

The root unit tests cover the structured log analyzer:

```bash
python -m pytest -q
```

## Inspect runs

Run one tier with any Inspect-supported model:

```bash
inspect eval eval/task.py@streamstats_debug \
  -T difficulty=tier1 \
  --model openai/gpt-5 \
  --log-dir logs/tier1
```

Use `tier2` or `tier3` to select the other cumulative conditions. To run all
three as independent samples, use separate task targets:

```bash
inspect eval \
  eval/task.py@streamstats_tier1 \
  eval/task.py@streamstats_tier2 \
  eval/task.py@streamstats_tier3 \
  --model openai/gpt-5 \
  --epochs 10 \
  --max-sandboxes 3 \
  --log-dir logs/pilot
```

Here `--epochs 10` gives ten fresh rollouts per task target. `--max-sandboxes 3`
limits concurrent sandbox capacity; it does not reuse a sandbox. Tier 1 and
Tier 1 has a 100k token limit, Tier 2 has 150k, and Tier 3 has 200k. The task-specific limits
apply unless the explicit `--token-limit` override is supplied.

The convenience wrapper has the same behavior:

```bash
python scripts/pilot.py \
  --model openai/gpt-5 \
  --runs 10 \
  --difficulty all
```

`--order-seed N` can reproducibly shuffle tier order. The wrapper’s optional
control server is enabled with the valid Inspect values `true` by default or
`keep` with `--keep-control`; it does not change sandbox ownership.

### OpenRouter and pinned providers

Put `OPENROUTER_API_KEY` in the environment or in the project’s `.env` file as
supported by the local setup. The wrapper accepts a provider/quantization
shorthand:

```bash
python scripts/pilot.py \
  --provider deepinfra/fp4 \
  --model z-ai/glm-5.3-flash \
  --runs 10 \
  --difficulty all
```

This qualifies the model as `openrouter/z-ai/glm-5.3-flash` and sends Inspect
an OpenRouter routing argument equivalent to:

```text
provider={"order":["deepinfra"],"allow_fallbacks":false,"quantizations":["fp4"]}
```

The expanded form is also supported:

```bash
python scripts/pilot.py \
  --provider deepinfra \
  --quantization fp4 \
  --model z-ai/glm-5.3-flash \
  --runs 10 \
  --difficulty tier1
```

To request OpenRouter’s no-data-collection and Zero Data Retention filters:

```bash
python scripts/pilot.py \
  --provider deepinfra/fp4 \
  --privacy \
  --model z-ai/glm-5.3-flash \
  --runs 10 \
  --difficulty tier1
```

Provider privacy policies are endpoint-level claims; verify that they meet
your organization’s requirements.

## Isolated-defect calibration

The cumulative tiers contain different numbers of defects, so calibrate A, B,
and C independently in the otherwise-correct project as well:

```bash
inspect eval eval/task.py@streamstats_calibration_a \
  --model openrouter/z-ai/glm-5.3-flash \
  -M 'provider={"order":["deepinfra"],"allow_fallbacks":false,"quantizations":["fp4"]}' \
  --epochs 20 --log-dir logs/calibration-a

inspect eval eval/task.py@streamstats_calibration_b \
  --model openrouter/z-ai/glm-5.3-flash \
  -M 'provider={"order":["deepinfra"],"allow_fallbacks":false,"quantizations":["fp4"]}' \
  --epochs 20 --log-dir logs/calibration-b

inspect eval eval/task.py@streamstats_calibration_c \
  --model openrouter/z-ai/glm-5.3-flash \
  -M 'provider={"order":["deepinfra"],"allow_fallbacks":false,"quantizations":["fp4"]}' \
  --epochs 20 --log-dir logs/calibration-c
```

For a pilot, repeat fresh runs per cumulative tier, record normal `submit()`
and success separately, and compare success, total/output/cache-separated
tokens, time and work before the first passing suite, test/edit cycles,
observed repair order, mistakes, and work after the first green suite. Do not
claim the ladder is validated from wall-clock duration or local unit tests.

## Log analysis

Inspect `.eval` files are the canonical trajectory records. Extract JSON and
CSV metrics with:

```bash
python scripts/summarize_runs.py logs/pilot \
  --output logs/pilot-metrics.json \
  --csv logs/pilot-metrics.csv
```

The analyzer separates agent activity from scorer activity and reports model,
tier, success, normal submission versus limit/error termination, input,
output, reasoning, total, cache-read, and cache-write tokens, tool calls,
shell commands, logical pytest executions and failures, edited/inspected files,
edit/test cycles, time to first edit and first green suite, work after green,
turns, and scorer checks. It associates delayed `bash_session(action=read)`
output with the submitted command that produced it. Missing provider or event
fields remain unknown rather than being treated as zero.

## Limitations

- The manipulation changes defect count as well as dependency depth, so it is
  not a pure test of reasoning difficulty.
- Agent discovery order is observable only when trajectories contain enough
  structured evidence; otherwise it is unknown.
- Token and timing fields depend on the model provider and Inspect log fields.
- The current image is a reproducible debugging sandbox, not yet the future
  disk-space/unrelated-project experiment.
