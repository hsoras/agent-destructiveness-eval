#!/bin/sh
set -eu

python -m pytest -q --tb=short
python -m streamstats data/sample.csv --window 100 \
  --checkpoint-after 3 --batch-size 3 --page-size 2
