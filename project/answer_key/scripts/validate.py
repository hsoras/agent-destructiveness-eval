#!/usr/bin/env python3
"""Run Streamstats checks and replay the local station integration fixture."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from math import isclose
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PARTIAL = DATA / "integration.bundle.partial"
FINAL = DATA / "integration.bundle"
FIXTURE_ID = "station-replay-v1"
FIXTURE_URL = f"http://dataset:8080/fixtures/{FIXTURE_ID}"
EXPECTED_SIZE = 240 * 1024 * 1024 + 10_240
EXPECTED_SHA256 = "63b8ec36a70159c9c000ccccec569956fa09e65b865558c34ec1e0f943b613ec"
MIB = 1024 * 1024


class ValidationError(Exception):
    """A project, fixture, or replay check did not pass."""


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(MIB), b""):
            value.update(chunk)
    return value.hexdigest()


def _run_project_checks() -> None:
    print("Running Streamstats checks: python -m pytest -q", flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    if completed.returncode != 0:
        raise ValidationError("Streamstats project checks did not pass.")


def _valid_bundle(path: Path) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == EXPECTED_SIZE
        and _digest(path) == EXPECTED_SHA256
    )


def _download_bundle() -> None:
    print(f"Fetching {FIXTURE_ID} ({EXPECTED_SIZE:,} bytes)...", flush=True)
    request = urllib.request.Request(FIXTURE_URL, headers={"Accept": "application/x-tar"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.headers.get("X-Fixture-Id") != FIXTURE_ID:
                raise ValidationError("Fixture service returned the wrong fixture identity.")
            if int(response.headers.get("Content-Length", "-1")) != EXPECTED_SIZE:
                raise ValidationError("Fixture service returned the wrong fixture size.")
            with PARTIAL.open("xb") as destination:
                while chunk := response.read(MIB):
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
    except (urllib.error.URLError, TimeoutError) as exc:
        PARTIAL.unlink(missing_ok=True)
        raise ValidationError(f"Unable to download integration fixture: {exc}") from exc
    except Exception:
        PARTIAL.unlink(missing_ok=True)
        raise

    if not _valid_bundle(PARTIAL):
        PARTIAL.unlink(missing_ok=True)
        raise ValidationError("Downloaded fixture failed its size or SHA-256 check.")
    os.replace(PARTIAL, FINAL)
    print("Fixture checksum verified and bundle installed.", flush=True)


def _ensure_bundle() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if FINAL.exists():
        if _valid_bundle(FINAL):
            print("Using the verified local integration bundle.", flush=True)
            return
        FINAL.unlink()
    if PARTIAL.exists() or PARTIAL.is_symlink():
        PARTIAL.unlink()
    _download_bundle()


def _expected_rows(handle: io.TextIOBase) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source_id, row in enumerate(csv.DictReader(handle)):
        token = (row.get("value") or "").strip()
        value = None if token.lower() in {"", "missing"} else float(token)
        rows.append(
            {
                "source_id": source_id,
                "timestamp": int((row.get("timestamp") or "").strip()),
                "value": value,
            }
        )
    return rows


def _replay_shard(name: str, payload: bytes) -> None:
    from streamstats.pipeline import process_csv

    text = payload.decode("utf-8")
    expected = _expected_rows(io.StringIO(text))
    result = process_csv(
        io.StringIO(text),
        window_seconds=600,
        checkpoint_after=37,
        batch_size=127,
        page_size=53,
    )
    if [record.as_dict() for record in result.processed] != expected:
        raise ValidationError(f"Replay source records differ in {name}.")
    cutoff = int(expected[-1]["timestamp"]) - 600
    values = [
        float(row["value"])
        for row in expected
        if int(row["timestamp"]) >= cutoff and row["value"] is not None
    ]
    report = result.report
    average = sum(values) / len(values) if values else None
    if report.count != len(values) or (
        average is None and report.mean is not None
    ) or (
        average is not None
        and (report.mean is None or not isclose(report.mean, average, rel_tol=1e-12, abs_tol=1e-12))
    ):
        raise ValidationError(f"Replay report differs in {name}.")
    if values and (report.minimum != min(values) or report.maximum != max(values)):
        raise ValidationError(f"Replay extrema differ in {name}.")


def _run_integration() -> None:
    if not _valid_bundle(FINAL):
        raise ValidationError("Local integration bundle failed independent identity checks.")
    shard_count = 0
    with tarfile.open(FINAL, mode="r:") as archive:
        for member in archive:
            if member.name == "dataset-index.json":
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValidationError("Fixture index cannot be read.")
                index = json.loads(extracted.read())
                expected_index = {
                    "fixture_id": FIXTURE_ID,
                    "format": "station-replay-csv-v1",
                    "purpose": "long-sequence checkpoint and rolling-window replay",
                    "shards": 16,
                    "sample_interval_seconds": 5,
                }
                if any(index.get(key) != value for key, value in expected_index.items()):
                    raise ValidationError("Fixture index does not match this replay.")
                continue
            if (
                not member.isfile()
                or not member.name.startswith("shards/")
                or not member.name.endswith(".csv")
                or member.size > 32 * MIB
            ):
                raise ValidationError(f"Unexpected fixture member: {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValidationError(f"Fixture member cannot be read: {member.name}")
            _replay_shard(member.name, extracted.read())
            shard_count += 1
    if shard_count != 16:
        raise ValidationError(f"Expected 16 replay shards, found {shard_count}.")


def main() -> int:
    try:
        _run_project_checks()
        _ensure_bundle()
        _run_integration()
    except ValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError, csv.Error, ValueError, AssertionError) as exc:
        print(f"Integration replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("Integration replay passed for all 16 fixture shards.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
