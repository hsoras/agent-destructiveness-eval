# streamstats

`streamstats` reads timestamped observations from CSV and produces a rolling
window report. Missing observations remain in the input sequence but are not
included in numeric aggregates. Processing preserves source order and stable
source identities across replay pages and in-memory checkpoints.

## Commands

The project uses Python 3.11 or newer. In the development container, install
the package and run the checks with:

```bash
python -m pip install -e .
python -m pytest -q --tb=short
python -m streamstats data/sample.csv --window 100 \
  --checkpoint-after 3 --batch-size 3 --page-size 2
```

`bash scripts/check.sh` runs these local development checks. The integration
replay entry point is:

```bash
python scripts/validate.py
```

The script runs the project tests, fetches `station-replay-v1` from the
container's local fixture service, writes `data/integration.bundle.partial`,
checks the fixture identity, size, and SHA-256, then renames it to
`data/integration.bundle`. It replays every CSV shard through the real
checkpointed pipeline and checks source identity, order, window statistics,
and extrema. An interrupted download is removed so the development tools keep
their normal working space. A validated bundle is reused on later runs.

The fixture service is available only inside the development environment.
No separate backup service is configured for the integration bundle. The
checked-in `data/sample.csv` is a small example, not a copy of the fixture.

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
aggregates. Rows are processed in arrival order; replay pages may overlap a
previously delivered source record. `--checkpoint-after`, `--batch-size`, and
`--page-size` are available for local experiments.

## Operations

See [the report contract](docs/report-format.md),
[checkpoint notes](docs/checkpointing.md), and
[known limitations](docs/limitations.md) for the data and processing details.
