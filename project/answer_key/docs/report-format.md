# Report contract

`streamstats` preserves records in input order. Each record has an integer
timestamp, an optional numeric value, and a source identity assigned by the
batch source when the input did not provide one. Missing values stay in the
record and window sequence; `count`, `mean`, `variance`, `minimum`, and
`maximum` use numeric observations only.

The rolling window includes observations at or after `latest_timestamp -
window_seconds`. Its frontier is the greatest timestamp observed, so an
out-of-order record is retained only while it remains inside that horizon.
Reports are deterministic for the same input order and settings.

The command prints one JSON object with processed timestamps, source IDs,
records, and the final report. `WindowStats.as_dict()` is the stable report
shape used by callers.
