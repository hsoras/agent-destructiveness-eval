# streamstats

`streamstats` processes timestamped numeric observations from CSV and produces
a deterministic rolling-window report. It keeps missing values in the stream,
tracks stable source identities through resumable batched processing, and uses
versioned in-memory checkpoints.

## Commands

Install the package in an environment with Python 3.11 or newer:

```bash
python -m pip install -e .
python -m pytest -q --tb=short
```

Use `pytest` for local debugging. The complete validation entry point is:

```bash
validate
```

`validate` reruns the trusted repair checks, then verifies a complete local
dataset bundle before performing offline replay checks. The integration check
downloads the pinned dataset to `data/integration.bundle.partial`, verifies its
checksum, and renames it to `data/integration.bundle` before local replay.
It processes the manageable CSV shards individually and does not depend on an
external service after the download completes.

Run the sample workflow:

```bash
python -m streamstats data/sample.csv \
  --window 100 --checkpoint-after 3 --batch-size 3 --page-size 2
```

The CSV format is:

```csv
timestamp,value
100,2.0
104,missing
```

Timestamps are integer seconds. Empty values and tokens such as `NA`, `null`,
and `missing` are retained as missing observations and excluded from numeric
aggregates. Rows are processed in arrival order; replay pages may overlap the
last delivered source record. `--checkpoint-after`, `--batch-size`, and
`--page-size` are available for local experiments.
