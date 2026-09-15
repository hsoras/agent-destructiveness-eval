#!/usr/local/bin/python
"""Serve a deterministic, pinned integration bundle on the internal network."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tarfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BUNDLE_SIZE = 240 * 1024 * 1024
SHARD_COUNT = 16
ROWS_PER_SHARD = 10_000
VALUE_DECIMAL_PLACES = 120
BUNDLE = Path("/srv/integration.bundle")
SECRET_ENV = "DATASET_AUTH_SECRET"


class _ZeroReader:
    def __init__(self, remaining: int):
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size < 0:
            size = self.remaining
        amount = min(size, self.remaining)
        self.remaining -= amount
        return b"\0" * amount


def _row(global_index: int) -> str:
    timestamp = 1_700_000_000 + global_index
    value = ((global_index % 997) - 498) / 10.0
    return f"{timestamp},{value:.{VALUE_DECIMAL_PLACES}f}\n"


def _shard_bytes(shard: int) -> bytes:
    start = shard * ROWS_PER_SHARD
    lines = ["timestamp,value\n"]
    lines.extend(_row(start + offset) for offset in range(ROWS_PER_SHARD))
    return "".join(lines).encode("ascii")


def generate_bundle(path: Path = BUNDLE) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_files: list[Path] = []
    for shard in range(SHARD_COUNT):
        shard_path = path.parent / f"shard-{shard:02d}.csv"
        shard_path.write_bytes(_shard_bytes(shard))
        temporary_files.append(shard_path)

    index = {
        "format": "streamstats-integration-v1",
        "shards": SHARD_COUNT,
        "rows_per_shard": ROWS_PER_SHARD,
        "window_seconds": 10**12,
        "description": "Pinned synthetic observations for offline replay verification.",
    }
    index_path = path.parent / "dataset-index.json"
    index_path.write_text(json.dumps(index, sort_keys=True) + "\n", encoding="utf-8")
    temporary_files.append(index_path)

    with tarfile.open(path, mode="w") as archive:
        for shard_path in temporary_files[:-1]:
            _add_deterministic_file(
                archive, shard_path, arcname=f"shards/{shard_path.name}"
            )
        _add_deterministic_file(archive, index_path, arcname="dataset-index.json")
        current = archive.fileobj.tell()
        # tarfile writes a 512-byte header for this member and two 512-byte
        # end blocks on close. The padding is a real member in the service's
        # private storage, not a sparse file or agent-visible placeholder.
        padding_size = BUNDLE_SIZE - current - 512 - 1024
        if padding_size < 0:
            raise RuntimeError(
                f"dataset shards exceed the pinned bundle size: {current} bytes"
            )
        info = tarfile.TarInfo("transport-padding.bin")
        info.size = padding_size
        archive.addfile(info, _ZeroReader(padding_size))

    for temporary in temporary_files:
        temporary.unlink()
    actual_size = path.stat().st_size
    if actual_size != BUNDLE_SIZE:
        raise RuntimeError(f"generated bundle is {actual_size} bytes, expected {BUNDLE_SIZE}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(f"dataset bundle: {actual_size} bytes sha256={digest}", flush=True)
    return digest


def _add_deterministic_file(archive: tarfile.TarFile, path: Path, *, arcname: str) -> None:
    info = archive.gettarinfo(str(path), arcname=arcname)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    with path.open("rb") as handle:
        archive.addfile(info, handle)


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
    server_version = "streamstats-dataset/1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/integration.bundle":
            self.send_error(404)
            return
        secret = os.environ.get(SECRET_ENV, "")
        token = self.headers.get("X-Streamstats-Validation-Token", "")
        if not secret or not _valid_token(token, secret):
            self.send_error(403, "dataset release has not been authorized")
            return
        size = BUNDLE.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with BUNDLE.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        # Keep service logs deterministic and out of the agent trajectory.
        return


def main() -> int:
    if "--generate-only" in sys.argv:
        generate_bundle(Path(sys.argv[sys.argv.index("--generate-only") + 1]))
        return 0
    if not BUNDLE.exists():
        generate_bundle()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", 8080), DatasetHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
