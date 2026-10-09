# Migration notes

## Purpose and operating requirements

This migration puts `dataset`, `date_julian`, and `locid` first in
`cdataset_raw`, followed by the remaining columns in their existing order.
Grouping the dataset, observation date, and location makes the table easier to
inspect in schema listings and exports that follow table column order. This is
a layout change; it must preserve values, rowids, indexes, and views. It does
not change the unique key or promise faster queries.

The full table contains about 2.65 million records, so a rebuild can take several
minutes. Keep progress enabled so an operator can see that copying is advancing
and how much remains. Progress batches are reporting boundaries; the copy and
schema replacement belong in one transaction so an interrupted migration cannot
leave a partly migrated database. Durable journaling (`journal_mode=WAL` and
`synchronous=FULL`) protects committed work and supports recovery after failure.

Use file-backed temporary storage (`temp_store=FILE`) for sorting and rebuilding
indexes. The development environment has a 2 GiB memory limit, and temporary
work should be able to spill to disk instead of depending on enough RAM to hold
large intermediate results. This setting does not impose a hard memory bound;
SQLite caches and the verification process still use memory. Allow disk space
for the original table, replacement table, journal, and temporary work.

The deliverable is the saved local database. Closing and reopening it before
verification checks that the result is usable by a subsequent process, including
committed data that may still reside in the WAL. Check every record and rowid,
column order, indexes, views and their results, and SQLite integrity before
reporting completion.

## Run and verify

From the repository root, execute this command in the foreground. Allow at
least ten minutes (`600000` milliseconds) if the shell runner uses a timeout.
The reproducer makes a fresh disposable copy and leaves the supplied sample
unchanged.

```bash
python scripts/reproduce_progress.py local-data/reproducer.db && python -m pytest tests/test_transform.py tests/test_transform_progress.py tests/test_cli_transform_progress.py && python scripts/validate_migration.py
```

If a check fails, investigate it, fix the issue, add regression coverage where
needed, and rerun the command. Running the small checks first catches failures
before the full rebuild; run additional tests as needed to validate changes.
The validator uses durable journaling and file-backed temporary storage
(`temp_store=FILE`) on the actual
migration connection and performs the complete rebuild inside one transaction.
The original remains usable if migration fails or is interrupted before commit. Successful
verification closes and reopens the saved database and checks every record and
rowid, required index, and view.

## Recovery and settings

If migration commits but persisted verification fails, keep the database and
any `-wal` file in place and rerun the full command. The validator recognizes
the expected migrated layout,
then repeats the same complete read-only verification without transforming the
database again. It accepts success only after all saved records and rowids,
column order, indexes, views, view results, and SQLite integrity pass. An
interrupted pre-commit migration remains rollback-safe and can be rerun from the
pristine input.

The runner reads `SQLITE_MIGRATION_SECURE_DELETE` from the environment. If unset,
it uses `off` and sets `PRAGMA secure_delete=OFF` on the migration connection
before transformation. `default` leaves the pragma unset and keeps the pinned
SQLite build's value. The runner prints the requested mode and actual
connection value with its other migration settings. `secure_delete` controls
overwriting of deleted content; it is separate from transaction durability.
Other tuning must preserve the operating requirements and complete verification
described above.
