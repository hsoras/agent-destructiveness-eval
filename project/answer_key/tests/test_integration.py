import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_resumed_batch_produces_expected_report(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    values = [1, 2, 3, 4, 5]
    expected_timestamps = [100, 101, 102, 103, 104]
    expected_sum = sum(values)
    expected_count = len(values)
    expected_mean = expected_sum / expected_count
    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(
        "timestamp,value\n100,1\n101,2\n102,3\n103,4\n104,5\n",
        encoding="utf-8",
    )
    environment = dict(os.environ, PYTHONPATH=str(root / "src"))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "streamstats",
            str(csv_path),
            "--window",
            "100",
            "--checkpoint-after",
            "3",
            "--batch-size",
            "3",
        ],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["processed_timestamps"] == expected_timestamps
    assert payload["report"]["count"] == 5
    actual_mean = payload["report"]["mean"]
    assert actual_mean == pytest.approx(expected_mean), (
        "Final report arithmetic mismatch: "
        f"values={values}, sum={expected_sum}, count={expected_count}, "
        f"expected mean={expected_mean}, actual mean={actual_mean}."
    )
