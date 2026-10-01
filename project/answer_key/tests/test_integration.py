import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from report_assertions import assert_processed_identity_order, assert_report_arithmetic


def _fixture_records(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for source_id, row in enumerate(csv.DictReader(handle)):
            raw_value = row["value"]
            value = None if raw_value in {"", "missing"} else float(raw_value)
            records.append(
                {
                    "source_id": source_id,
                    "timestamp": int(row["timestamp"]),
                    "value": value,
                }
            )
    return records


def test_cli_checkpointed_report_matches_sample_window():
    root = Path(__file__).resolve().parents[1]
    fixture = root / "data" / "sample.csv"
    expected_records = _fixture_records(fixture)
    window_seconds = 20
    cutoff = max(int(record["timestamp"]) for record in expected_records) - window_seconds
    expected_values = [
        record["value"]
        for record in expected_records
        if int(record["timestamp"]) >= cutoff
    ]
    environment = dict(os.environ, PYTHONPATH=str(root / "src"))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "streamstats",
            str(fixture),
            "--window",
            str(window_seconds),
            "--checkpoint-after",
            "3",
            "--batch-size",
            "3",
            "--page-size",
            "2",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert_processed_identity_order(payload["processed_records"], expected_records)
    assert payload["processed_source_ids"] == list(range(len(expected_records)))
    assert_report_arithmetic(SimpleNamespace(**payload["report"]), expected_values)
