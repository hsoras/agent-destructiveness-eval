Bookmark a significant run and preserve a short explanation with its evidence.

Usage: `/bookmark <run path or latest> <why it matters>`

Extract the run path and explanation from the user's request. For `latest`, let
the script locate the most recently modified run under `/private/tmp/live-runs`
or `logs/`. Call:

```sh
python3 scripts/bookmark_run.py "<run path or latest>" --why "<why it matters>"
```

Report the created bookmark path. Do not copy run files manually; the script
redacts secrets and records the run metadata.
