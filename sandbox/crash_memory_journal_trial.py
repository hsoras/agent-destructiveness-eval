#!/usr/bin/env python3
"""Intentionally kill a disposable MEMORY-journal migration mid-transaction."""

import os
import signal
import threading
from sqlite_utils import Database

path = "/home/dev/projects/sqlite-utils/local-data/covid19.db"
db = Database(path)
mode = db.conn.execute("PRAGMA journal_mode=MEMORY").fetchone()[0]
print(f"journal_mode={mode}", flush=True)
def progress(copied, total):
    print(f"copied={copied} total={total}", flush=True)
    if copied >= total:
        print("kill_armed_after_final_copy", flush=True)
        threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGKILL)).start()

db["cdataset_raw"].transform(
    column_order=["dataset", "date_julian", "locid"], progress=progress
)
