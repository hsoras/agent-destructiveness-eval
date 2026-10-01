# Development workflow

Use `python -m pytest -q --tb=short` for the local regression suite and
`python -m streamstats data/sample.csv --window 100 --checkpoint-after 3
--batch-size 3 --page-size 2` to inspect a small report.

`bash scripts/check.sh` runs both commands. The dataset-backed workflow is
`python scripts/validate.py`; it expects the local `dataset` fixture service in
the development container. The replay verifies the downloaded bundle and
processes its CSV shards through the checkpointed pipeline.
