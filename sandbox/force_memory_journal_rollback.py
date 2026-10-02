#!/usr/bin/env python3
"""Force SQLITE_FULL in a disposable full COVID database and verify rollback."""

import hashlib
import json
from pathlib import Path
import sqlite3

from sqlite_utils import Database

path = Path("/home/dev/projects/sqlite-utils/local-data/covid19.db")

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

initial_sha256 = sha256_file(path)
size_before = path.stat().st_size
db = Database(path)
mode = db.conn.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
page_count = db.conn.execute("PRAGMA page_count").fetchone()[0]
limited_pages = max(page_count + 8, int(page_count * 1.00005))
db.conn.execute(f"PRAGMA max_page_count={limited_pages}")
transaction_start_sha256 = sha256_file(path)
error = None
try:
    db["cdataset_raw"].transform(
        column_order=["dataset", "date_julian", "locid"],
        progress=lambda _copied, _total: None,
    )
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"
finally:
    db.close()

after = sha256_file(path)
connection = sqlite3.connect(path)
integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
rows = connection.execute("SELECT count(*) FROM cdataset_raw").fetchone()[0]
connection.close()
record = {
    "journal_mode_during_transaction": mode,
    "source_page_count": page_count,
    "max_page_count": limited_pages,
    "forced_error": error,
    "source_sha256_before_journal_configuration": initial_sha256,
    "source_sha256_at_transaction_start": transaction_start_sha256,
    "source_sha256_after": after,
    "transaction_byte_identical_after_failure": transaction_start_sha256 == after,
    "source_bytes_before": size_before,
    "source_bytes_after": path.stat().st_size,
    "source_rows_after": rows,
    "integrity_after": integrity,
}
print(json.dumps(record, sort_keys=True))
if not (mode == "memory" and error and "disk is full" in error.lower()
        and transaction_start_sha256 == after and integrity == "ok" and rows == 2652938):
    raise SystemExit(1)
