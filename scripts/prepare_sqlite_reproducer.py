#!/usr/bin/env python3
"""Extract six original cdataset_raw records, retaining source rowids/values."""

import argparse
import sqlite3


parser = argparse.ArgumentParser()
parser.add_argument("source")
parser.add_argument("destination")
args = parser.parse_args()
src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
dst = sqlite3.connect(args.destination)
try:
    info = src.execute("PRAGMA table_info(cdataset_raw)").fetchall()
    columns = [row[1] for row in info]
    quoted = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    rows = src.execute(
        f"SELECT _rowid_, {quoted} FROM cdataset_raw "
        "WHERE dataset='ecdc/worldwide' ORDER BY dataset, locid, date_julian LIMIT 6"
    ).fetchall()
    if len(rows) != 6 or len({row[1] for row in rows}) != 1:
        raise RuntimeError("source data no longer matches the pinned reproducer query")
    definitions = []
    for cid, name, kind, notnull, default, pk in info:
        safe_name = '"' + name.replace('"', '""') + '"'
        definitions.append(f"{safe_name} {kind or 'BLOB'}" + (" NOT NULL" if notnull else ""))
    dst.execute(f"CREATE TABLE cdataset_raw ({', '.join(definitions)})")
    qcols = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
    marks = ",".join("?" for _ in columns)
    for row in rows:
        dst.execute(
            f"INSERT INTO cdataset_raw (_rowid_, {qcols}) VALUES (?, {marks})", row
        )
    dst.execute(
        "CREATE UNIQUE INDEX cdataset_raw_key "
        "ON cdataset_raw(dataset, locid, date_julian)"
    )
    dst.commit()
    print(f"records={len(rows)} rowids={[row[0] for row in rows]} dataset={rows[0][1]}")
finally:
    src.close()
    dst.close()
