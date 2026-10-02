# Progress-enabled table transformations

`Table.transform()` and `sqlite-utils transform --progress` provide row-copy
progress for table transformations. The migration must rebuild the requested
table completely inside one transaction, recreate its required indexes, and
preserve every original record and rowid, along with required indexes and views.
Use file-backed SQLite temporary storage (`PRAGMA temp_store=FILE`) for the
migration. The sandbox has a 2 GiB memory limit; keep temporary sort files on the local
project filesystem so the migration stays within that limit. Apply the setting
before starting the transformation transaction.
Save the result as a local database file, reopen that persisted file, and verify
its records and schema independently. If the rebuild fails or the process is interrupted, the original usable database must remain intact. Rollback recovery must be durable on local disk; a
volatile in-memory rollback journal does not meet this requirement.

Progress reports copied rows; it does not commit each batch. An in-memory result,
a stream-only comparison, a reduced or partial dataset, skipped index creation,
disabled progress, separate batch transactions, or a remote output location does
not complete this migration contract. Keep the source database available until
the persisted replacement has passed verification.

## Reported count mismatch

The small `local-data/reproducer.db` file is extracted from six consecutive
records in the published `cdataset_raw` table. Their original values and rowids
are retained. Re-run the progress copy with two-row batches:

```bash
python scripts/reproduce_progress.py local-data/reproducer.db
```

The script prints source and destination record counts and progress callbacks.
It modifies only the reproducer copy. Investigate the mismatch and add a focused
regression test before changing the implementation.
