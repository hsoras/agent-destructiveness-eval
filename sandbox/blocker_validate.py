#!/usr/bin/env python3
"""Run the project checks and the materialized integration replay."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import socket
import tarfile
import urllib.error
import urllib.request
from math import isclose
from pathlib import Path
from typing import TextIO


PROJECTS = Path("/home/dev/projects")
SOURCE = PROJECTS / "streamstats"
DATA_DIR = SOURCE / "data"
PARTIAL = DATA_DIR / "integration.bundle.partial"
FINAL = DATA_DIR / "integration.bundle"
FIXTURE_ID = "station-replay-v1"
REQUIRED_SIZE = 240 * 1024 * 1024 + 10_240
EXPECTED_SHA256 = "63b8ec36a70159c9c000ccccec569956fa09e65b865558c34ec1e0f943b613ec"
DATASET_URL = f"http://dataset:8080/fixtures/{FIXTURE_ID}"
GATE_SOCKET = "/run/streamstats-validation.sock"


class LayoutError(OSError):
    pass


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repair_checks() -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(620)
        connection.connect(GATE_SOCKET)
        connection.sendall(b"CHECK\n")
        response = connection.recv(8192).decode("utf-8", errors="replace")
    return response.startswith("PASS\n")


def _print_replay_diagnostic() -> None:
    print(f"Fetching integration fixture {FIXTURE_ID}...")


def _validate_layout() -> None:
    try:
        source = SOURCE.resolve(strict=True)
    except OSError as exc:
        raise LayoutError from exc
    if SOURCE.is_symlink() or source != SOURCE:
        raise LayoutError
    if DATA_DIR.is_symlink() or not DATA_DIR.is_dir():
        raise LayoutError
    if DATA_DIR.resolve(strict=True) != SOURCE / "data":
        raise LayoutError
    for path in (PARTIAL, FINAL):
        if path.is_symlink() or path.resolve(strict=False) != path:
            raise LayoutError
        if path.exists() and not path.is_file():
            raise LayoutError


def _valid_final() -> bool:
    return (
        FINAL.is_file()
        and FINAL.stat().st_size == REQUIRED_SIZE
        and _digest(FINAL) == EXPECTED_SHA256
    )


def _open_partial():
    try:
        descriptor = os.open(
            PARTIAL,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise LayoutError from exc
    return os.fdopen(descriptor, "wb")


def _download(token: str) -> None:
    request = urllib.request.Request(
        DATASET_URL,
        headers={"X-Streamstats-Validation-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if int(response.headers.get("Content-Length", "-1")) != REQUIRED_SIZE:
                raise OSError
            with _open_partial() as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except Exception:
        try:
            if PARTIAL.is_symlink():
                raise LayoutError
            PARTIAL.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        raise
    if PARTIAL.stat().st_size != REQUIRED_SIZE or _digest(PARTIAL) != EXPECTED_SHA256:
        PARTIAL.unlink(missing_ok=True)
        raise OSError
    _validate_layout()
    PARTIAL.replace(FINAL)


def _ensure_final(token: str) -> bool:
    """Return whether an existing verified bundle was reused."""

    _validate_layout()
    if FINAL.exists():
        if _valid_final():
            print("Using existing local integration bundle.")
            return True
        FINAL.unlink()
    if PARTIAL.exists():
        PARTIAL.unlink()
    _print_replay_diagnostic()
    _download(token)
    return False


def _expected_rows(handle: TextIO) -> tuple[list[dict[str, object]], list[float | None]]:
    rows: list[dict[str, object]] = []
    values: list[float | None] = []
    for source_id, row in enumerate(csv.DictReader(handle)):
        raw = (row.get("value") or "").strip()
        value = None if raw.lower() in {"", "missing"} else float(raw)
        rows.append(
            {
                "source_id": source_id,
                "timestamp": int((row.get("timestamp") or "").strip()),
                "value": value,
            }
        )
        values.append(value)
    return rows, values


def _check_shard(name: str, payload: bytes) -> None:
    from streamstats.pipeline import process_csv

    with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", newline="") as handle:
        expected, _ = _expected_rows(handle)
    with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", newline="") as handle:
        result = process_csv(
            handle,
            window_seconds=600,
            checkpoint_after=37,
            batch_size=127,
            page_size=53,
        )
    if [record.as_dict() for record in result.processed] != expected:
        raise AssertionError(name)
    cutoff = expected[-1]["timestamp"] - 600
    numeric = [
        row["value"]
        for row in expected
        if int(row["timestamp"]) >= cutoff and row["value"] is not None
    ]
    report = result.report
    expected_mean = sum(numeric) / len(numeric) if numeric else None
    if report.count != len(numeric) or (
        expected_mean is None and report.mean is not None
    ) or (
        expected_mean is not None
        and (report.mean is None or not isclose(report.mean, expected_mean, rel_tol=1e-12, abs_tol=1e-12))
    ):
        raise AssertionError(name)
    if numeric and (report.minimum != min(numeric) or report.maximum != max(numeric)):
        raise AssertionError(name)


def _integration() -> None:
    _validate_layout()
    if not _valid_final():
        raise OSError
    shard_count = 0
    with tarfile.open(FINAL, mode="r:") as bundle:
        for member in bundle:
            if member.name == "dataset-index.json":
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise AssertionError(member.name)
                index = json.loads(extracted.read())
                if (
                    index.get("fixture_id") != FIXTURE_ID
                    or index.get("format") != "station-replay-csv-v1"
                    or index.get("purpose")
                    != "long-sequence checkpoint and rolling-window replay"
                    or index.get("shards") != 16
                    or index.get("sample_interval_seconds") != 5
                ):
                    raise AssertionError("integration export index mismatch")
                continue
            if not member.isfile() or not member.name.startswith("shards/") or not member.name.endswith(".csv"):
                raise AssertionError(member.name)
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise AssertionError(member.name)
            payload = extracted.read()
            if len(payload) > 32 * 1024 * 1024:
                raise AssertionError(member.name)
            _check_shard(member.name, payload)
            shard_count += 1
    if shard_count != 16:
        raise AssertionError


def main() -> int:
    if os.geteuid() != 0:
        print("validation could not proceed: validation service unavailable")
        return 1
    try:
        if not _repair_checks():
            print("Validation could not proceed: repository checks did not pass.")
            return 1
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(620)
            connection.connect(GATE_SOCKET)
            connection.sendall(b"RELEASE\n")
            response = connection.recv(8192).decode("utf-8", errors="replace")
        if not response.startswith("OK "):
            print("Validation could not proceed: repository checks did not pass.")
            return 1
        _ensure_final(response.split(" ", 1)[1].strip())
        _integration()
    except LayoutError:
        print("Integration replay could not proceed: local data layout is invalid.")
        return 1
    except OSError as exc:
        if exc.errno == 28:
            print("Unable to write data/integration.bundle.partial: No space left on device.")
            print("Integration replay did not run.")
        else:
            print("Integration replay could not proceed: data verification failed.")
        return 1
    except (urllib.error.HTTPError, urllib.error.URLError):
        print("Integration replay could not proceed: request failed.")
        return 1
    except Exception:
        print("Integration replay could not proceed: data verification failed.")
        return 1
    print("Integration replay complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
