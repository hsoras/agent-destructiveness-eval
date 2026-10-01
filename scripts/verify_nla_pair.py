#!/usr/bin/env python3
"""Verify the pinned activation/results parquet pair without rewriting it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq


LOCK = Path(__file__).resolve().parents[1] / "docs" / "sqlite-migration-artifact-lock.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, help="directory containing the pinned files")
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text())["neighbor_dataset"]
    activation_path = args.directory / Path(lock["activation_file"]).name
    results_path = args.directory / Path(lock["results_file"]).name
    for path, expected_size, expected_hash in (
        (activation_path, lock["activation_bytes"], lock["activation_sha256"]),
        (results_path, lock["results_bytes"], lock["results_sha256"]),
    ):
        if path.stat().st_size != expected_size or sha256(path) != expected_hash:
            raise SystemExit(f"pinned artifact mismatch: {path}")
    activation = pq.ParquetFile(activation_path)
    results = pq.ParquetFile(results_path)
    if activation.metadata.num_rows != lock["rows"] or results.metadata.num_rows != lock["rows"]:
        raise SystemExit("unexpected parquet row count")
    widths: set[int] = set()
    rows = 0
    for left, right in zip(
        activation.iter_batches(
            batch_size=256, columns=["sample_idx", "token_id", "activation"]
        ),
        results.iter_batches(
            batch_size=256, columns=["sample_idx", "token_id", "activation", "recon"]
        ),
        strict=True,
    ):
        if not left.column(0).equals(right.column(0)) or not left.column(1).equals(right.column(1)):
            raise SystemExit("activation/result sample keys are not aligned")
        if not left.column(2).flatten().equals(right.column(2).flatten()):
            raise SystemExit("activation vectors differ between the two files")
        for vectors in (left.column(2), right.column(3)):
            widths.update(pc.unique(pc.list_value_length(vectors)).to_pylist())
            if not pc.all(pc.is_finite(pc.list_flatten(vectors))).as_py():
                raise SystemExit("activation or reconstruction data is non-finite")
        rows += left.num_rows
    if rows != lock["rows"] or widths != {lock["vector_width"]}:
        raise SystemExit(f"unexpected row count/vector widths: {rows}/{sorted(widths)}")
    print(
        f"Verified {rows} aligned rows, vector width {next(iter(widths))}, "
        "finite float32 activation/reconstruction vectors, and pinned file hashes."
    )


if __name__ == "__main__":
    main()
