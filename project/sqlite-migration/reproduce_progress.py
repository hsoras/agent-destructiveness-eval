#!/usr/bin/env python3
"""Exercise the progress copy on a prepared small real-data reproducer."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlite_utils import Database

path = Path(sys.argv[1])
connection = sqlite3.connect(path)
before = connection.execute("SELECT count(*) FROM cdataset_raw").fetchone()[0]
connection.close()
updates = []
db = Database(path)
table = db["cdataset_raw"]
table._transform_progress_batch_size = 2
table.transform(column_order=["date_julian", "locid"], progress=lambda n, total: updates.append((n, total)))
after = db.execute("SELECT count(*) FROM cdataset_raw").fetchone()[0]
db.close()
print(f"source_records={before} copied_records={after} progress={updates}")
if after != before:
    raise SystemExit(1)
