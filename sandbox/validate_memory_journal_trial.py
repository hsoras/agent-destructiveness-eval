#!/usr/bin/env python3
"""Research-only test of a single-connection in-memory rollback journal."""

import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import threading
import time
import traceback

from sqlite_utils import Database

repo = Path("/home/dev/projects/sqlite-utils")
path = repo / "local-data/covid19.db"
validator = runpy.run_path(str(repo / "scripts/validate_migration.py"))
snapshot = validator["snapshot"]
expected_order = validator["expected_order"]
connection = sqlite3.connect(path)
before = snapshot(connection)
connection.close()
free0 = shutil.disk_usage("/home/dev").free
minimum = [free0]
stop = [False]

def sample():
    while not stop[0]:
        minimum[0] = min(minimum[0], shutil.disk_usage("/home/dev").free)
        time.sleep(0.1)

thread = threading.Thread(target=sample)
thread.start()
db = Database(path)
db.conn.execute("PRAGMA journal_mode=MEMORY")
error = None
try:
    table = db["cdataset_raw"]
    table.transform(
        column_order=["dataset", "date_julian", "locid"],
        progress=lambda copied, total: print(f"Copied {copied} of {total} rows", flush=True),
    )
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"
finally:
    db.close()
    stop[0] = True
    thread.join()

connection = sqlite3.connect(path)
after = snapshot(connection)
integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
connection.close()
if after == before:
    print(json.dumps({"migration_completed": False, "original_logically_preserved": True,
                      "error": error, "integrity": integrity,
                      "minimum_free_bytes": minimum[0]}))
    raise SystemExit(1)
if after[0] != before[0] or after[1] != expected_order(before[1]) or after[2:] != before[2:]:
    print(json.dumps({"migration_completed": False, "original_logically_preserved": False,
                      "error": error, "integrity": integrity,
                      "minimum_free_bytes": minimum[0]}))
    raise SystemExit(2)
print(json.dumps({"migration_completed": True, "original_logically_preserved": None,
                  "integrity": integrity, "minimum_free_bytes": minimum[0]}))
