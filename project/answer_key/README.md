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
