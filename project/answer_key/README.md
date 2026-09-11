# streamstats

`streamstats` processes timestamped numeric observations from CSV and produces
a deterministic report. It supports missing values, time-based rolling
windows, and resumable batch processing with versioned in-memory checkpoints.

## Layout

```text
src/streamstats/   package implementation
tests/             project tests
data/              sample input
```

## Commands

Install the package in an environment with Python 3.11 or newer:

```bash
python -m pip install -e .
python -m pytest -q
python -m streamstats data/sample.csv --window 60 --batch-size 3
```

The CSV format is:

```csv
timestamp,value
100,2.0
104,4.0
```

Timestamps are integer seconds. Missing values such as an empty field, `NA`,
`null`, or `missing` remain part of the time series but are excluded from
numeric aggregates. Blank rows are skipped, while extra CSV columns are
rejected as malformed input. Source batches use a remainder-first partition:
when the row count is not divisible by the batch size, the first batch is the
remainder and subsequent batches use the requested size. The command-line
output contains the processed record timestamps and the final report. Resume
positions identify the next record to process; end of input is represented by
one cursor after the final batch.
