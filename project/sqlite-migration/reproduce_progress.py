#!/usr/bin/env python3
"""Reproduce and report row preservation using a fresh disposable copy."""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlite_utils import Database


def records(connection):
    # A valid transformation changes the physical column order. Compare each
    # record in a stable name order while retaining its original rowid.
    columns = sorted(row[1] for row in connection.execute("PRAGMA table_info(cdataset_raw)"))
    selected = ", ".join(["_rowid_"] + [f'"{name.replace(chr(34), chr(34)*2)}"' for name in columns])
    return connection.execute(
        f'SELECT {selected} FROM cdataset_raw ORDER BY _rowid_'
    ).fetchall()


def digest(rows):
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=lambda value: {
        "bytes": value.hex()
    })
    return hashlib.sha256(payload.encode()).hexdigest()


pristine = Path(sys.argv[1]).resolve()
if not pristine.is_file():
    raise SystemExit(f"reproducer database not found: {pristine}")
pristine_hash = hashlib.sha256(pristine.read_bytes()).hexdigest()
source = sqlite3.connect(f"file:{pristine}?mode=ro", uri=True)
source_rows = records(source)
source.close()
updates = []
run_error = None

with tempfile.TemporaryDirectory(prefix="sqlite-progress-repro-") as temporary:
    disposable = Path(temporary) / "reproducer.db"
    shutil.copy2(pristine, disposable)
    db = Database(disposable)
    table = db["cdataset_raw"]
    table._transform_progress_batch_size = 2
    try:
        table.transform(
            column_order=["date_julian", "locid"],
            progress=lambda copied, total: updates.append((copied, total)),
        )
    except Exception as error:
        run_error = f"{type(error).__name__}: {error}"
    finally:
        db.close()

    result = sqlite3.connect(f"file:{disposable}?mode=ro", uri=True)
    result_rows = records(result)
    result.close()

pristine_hash_after = hashlib.sha256(pristine.read_bytes()).hexdigest()
preserved = source_rows == result_rows
print(f"source_records={len(source_rows)} final_records={len(result_rows)}")
print(f"source_rows_sha256={digest(source_rows)} final_rows_sha256={digest(result_rows)}")
print(f"progress={updates}")
print(f"error={run_error or 'none'}")
print(f"records_and_rowids_preserved={str(preserved).lower()}")
print(f"pristine_input_unchanged={str(pristine_hash == pristine_hash_after).lower()}")
if not preserved or pristine_hash != pristine_hash_after:
    raise SystemExit(1)
