#!/usr/bin/env python3
"""Serve the versioned station replay fixture on the internal network."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import tarfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


FIXTURE_ID = "station-replay-v1"
FIXTURE_ROUTE = f"/fixtures/{FIXTURE_ID}"
FIXTURE_FILENAME = f"{FIXTURE_ID}.bundle"
SHARD_COUNT = 16
ROWS_PER_SHARD = 491_712
SAMPLE_INTERVAL_SECONDS = 5
BASE_TIMESTAMP = 1_680_307_200
# The size follows from the fixed-format CSV rows and tar headers. It is not
# produced by appending a padding member.
BUNDLE_SIZE = 240 * 1024 * 1024 + 10_240
BUNDLE = Path("/srv") / FIXTURE_FILENAME
SECRET_ENV = "DATASET_AUTH_SECRET"
AUTH_REQUIRED_ENV = "DATASET_AUTH_REQUIRED"


def _row(global_index: int) -> str:
    state = (0xA5A5A5A5 ^ (global_index * 0x9E3779B1)) & 0xFFFFFFFF
    state ^= (state << 13) & 0xFFFFFFFF
    state ^= state >> 17
    state ^= (state << 5) & 0xFFFFFFFF
    value = ((state % 90_000_000_000) - 45_000_000_000) / 1_000_000
    timestamp = BASE_TIMESTAMP + global_index * SAMPLE_INTERVAL_SECONDS
    value_text = "missing" if global_index % 997 == 0 else f"{value:+020.12e}"
    return (
        f"{timestamp:010d},{value_text}\n"
    )


def _shard_bytes(shard: int) -> bytes:
    start = shard * ROWS_PER_SHARD
    lines = ["timestamp,value\n"]
    lines.extend(_row(start + offset) for offset in range(ROWS_PER_SHARD))
    return "".join(lines).encode("ascii")


def _add_bytes(archive: tarfile.TarFile, payload: bytes, *, arcname: str) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(payload)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(payload))


def generate_bundle(path: Path = BUNDLE) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    index = {
        "fixture_id": FIXTURE_ID,
        "format": "station-replay-csv-v1",
        "shards": SHARD_COUNT,
        "rows_per_shard": ROWS_PER_SHARD,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "start_timestamp": BASE_TIMESTAMP,
        "end_timestamp": BASE_TIMESTAMP
        + (SHARD_COUNT * ROWS_PER_SHARD - 1) * SAMPLE_INTERVAL_SECONDS,
        "columns": ["timestamp", "value"],
        "locations": ["west-yard", "east-yard"],
        "channels": ["ch0", "ch1", "ch2", "ch3"],
        "missing_every": 997,
        "purpose": "long-sequence checkpoint and rolling-window replay",
    }
    with tarfile.open(path, mode="w") as archive:
        for shard in range(SHARD_COUNT):
            _add_bytes(
                archive,
                _shard_bytes(shard),
                arcname=f"shards/capture-{shard:02d}.csv",
            )
        _add_bytes(
            archive,
            (json.dumps(index, sort_keys=True) + "\n").encode("utf-8"),
            arcname="dataset-index.json",
        )
    actual_size = path.stat().st_size
    if actual_size != BUNDLE_SIZE:
        raise RuntimeError(f"generated bundle is {actual_size} bytes, expected {BUNDLE_SIZE}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"integration export: {actual_size} bytes sha256={digest}", flush=True)
    return digest


def _valid_token(token: str, secret: str) -> bool:
    try:
        timestamp_text, signature = token.split(".", 1)
        timestamp = int(timestamp_text)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - timestamp) > 900:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), timestamp_text.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


class DatasetHandler(BaseHTTPRequestHandler):
    server_version = "fixture-service/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != FIXTURE_ROUTE:
            self.send_error(404)
            return
        secret = os.environ.get(SECRET_ENV, "")
        token = self.headers.get("X-Streamstats-Validation-Token", "")
        auth_required = os.environ.get(AUTH_REQUIRED_ENV, "1").lower() not in {
            "0", "false", "no"
        }
        if auth_required and (not secret or not _valid_token(token, secret)):
            self.send_error(403, "export access has not been authorized")
            return
        size = BUNDLE.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/x-tar")
        self.send_header("X-Fixture-Id", FIXTURE_ID)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with BUNDLE.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    if "--generate-only" in sys.argv:
        generate_bundle(Path(sys.argv[sys.argv.index("--generate-only") + 1]))
        return 0
    if not BUNDLE.exists():
        generate_bundle()
    ThreadingHTTPServer(("0.0.0.0", 8080), DatasetHandler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
