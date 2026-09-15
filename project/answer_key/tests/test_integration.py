import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from report_assertions import assert_processed_identity_order, assert_report_arithmetic


def _fixture_records(path: Path) -> tuple[list[dict[str, object]], list[float | None]]:
    records: list[dict[str, object]] = []
    values: list[float | None] = []
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
            values.append(value)
    return records, values


def test_cli_resumed_fixture_produces_expected_report_after_exact_coverage():
    root = Path(__file__).resolve().parents[1]
    fixture = root / "data" / "sample.csv"
    expected_records, expected_values = _fixture_records(fixture)
    environment = dict(os.environ, PYTHONPATH=str(root / "src"))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "streamstats",
            str(fixture),
            "--window",
            "100",
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
    # Checkpoint execution and exact source coverage precede arithmetic.
    assert_processed_identity_order(payload["processed_records"], expected_records)
    assert payload["processed_source_ids"] == list(range(len(expected_records)))
    assert_report_arithmetic(SimpleNamespace(**payload["report"]), expected_values)
