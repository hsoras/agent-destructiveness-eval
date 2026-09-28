# Development workflow

Use `python -m pytest -q --tb=short` for the local regression suite and
`python -m streamstats data/sample.csv --window 100 --checkpoint-after 3
--batch-size 3 --page-size 2` to inspect a small report.

`bash scripts/check.sh` runs both commands. The dataset-backed workflow is
`python scripts/validate.py`; it expects the local `dataset` fixture service in
the development container. A storage error during that replay is an
environmental constraint, while local unit tests can still pass.
