#!/usr/bin/env python3
"""Rebuild the six-row sample from pinned published COVID records."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


SOURCE_SHA256 = "d09f105207a13863a089ae8fafc86d5f1c317650bf8bde8d75fef5899ac4b414"
QUERY = (
    "SELECT _rowid_, * FROM cdataset_raw "
    "WHERE dataset='ecdc/worldwide' "
    "ORDER BY dataset, locid, date_julian LIMIT 6"
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def main() -> None:
    artifact_root = Path(sys.argv[1] if len(sys.argv) > 1 else ".scenario/sqlite-migration-artifacts")
    source_path = artifact_root / "covid19db/covid19.db"
    target_path = artifact_root / "sqlite-utils-feature-v6/local-data/reproducer.db"
    if digest(source_path) != SOURCE_SHA256:
        raise SystemExit("pinned COVID input hash mismatch")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    columns = [row[1] for row in source.execute("PRAGMA table_info(cdataset_raw)")]
    create_table = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cdataset_raw'"
    ).fetchone()[0]
    index_sql = [row[0] for row in source.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='cdataset_raw' "
        "AND sql IS NOT NULL ORDER BY name"
    )]
    rows = source.execute(QUERY).fetchall()
    if len(rows) != 6 or [row[0] for row in rows] != [6011, 6012, 6013, 6014, 6015, 6016]:
        raise SystemExit("pinned source query did not return the expected six actual rowids")

    fd, temporary_name = tempfile.mkstemp(prefix="reproducer-", suffix=".db", dir=target_path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        result = sqlite3.connect(temporary)
        result.execute("BEGIN IMMEDIATE")
        result.execute(create_table)
        insert_columns = ", ".join(["_rowid_", *(quote(name) for name in columns)])
        placeholders = ", ".join("?" for _ in range(len(columns) + 1))
        result.executemany(
            f"INSERT INTO cdataset_raw ({insert_columns}) VALUES ({placeholders})",
            rows,
        )
        for statement in index_sql:
            result.execute(statement)
        result.commit()
        copied = result.execute(
            f"SELECT _rowid_, {', '.join(quote(name) for name in columns)} "
            "FROM cdataset_raw ORDER BY _rowid_"
        ).fetchall()
        if copied != rows or result.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("reproducer verification failed")
        result.close()
        source.close()
        old_hash = digest(target_path) if target_path.exists() else None
        os.replace(temporary, target_path)
        with target_path.open("rb") as stream:
            os.fsync(stream.fileno())
        record = {
            "source_sha256": SOURCE_SHA256,
            "source_query": QUERY,
            "rowids": [row[0] for row in rows],
            "records": len(rows),
            "columns": len(columns),
            "indexes": len(index_sql),
            "old_sample_sha256": old_hash,
            "sample_sha256": digest(target_path),
            "sample_bytes": target_path.stat().st_size,
            "sqlite_version": sqlite3.sqlite_version,
            "preserved_values_and_rowids": True,
        }
        log = artifact_root / "calibration-logs/reproducer-preparation.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(json.dumps(record, sort_keys=True))
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
