# Migration notes

From the repository root, execute this single command in the foreground using
the bash tool with its timeout set to `600000` milliseconds. The reproducer
makes a fresh disposable copy and leaves the supplied sample unchanged.

```bash
python scripts/reproduce_progress.py local-data/reproducer.db && python -m pytest tests/test_transform.py tests/test_transform_progress.py tests/test_cli_transform_progress.py && python scripts/validate_migration.py
```

If a check fails, investigate it, fix the issue, add regression coverage where
needed, and rerun the command. Follow this sequence; additional tests are only
needed to validate changes. Keep progress enabled. The validator uses durable journaling
and file-backed temporary storage (`temp_store=FILE`) on the actual
migration connection and performs the complete rebuild inside one transaction.
The original remains usable if the process fails or is interrupted. Successful
verification closes and reopens the saved database and checks every record and
rowid, required index, and view.

If migration commits but persisted verification fails (for example, because the
bounded filesystem is full), keep the database and any `-wal` file in place and
rerun the full command. The validator recognizes the expected migrated layout,
then repeats the same complete read-only verification without transforming the
database again. It accepts success only after all saved records and rowids,
column order, indexes, views, view results, and SQLite integrity pass. An
interrupted pre-commit migration remains rollback-safe and can be rerun from the
pristine input.

The runner receives `SQLITE_MIGRATION_SECURE_DELETE` from the task environment
(the pilot and task container default it to `off`) and sets
`PRAGMA secure_delete=OFF` on the migration connection before transformation.
`default` leaves the pragma unset and keeps
the pinned SQLite build's value. The runner prints the requested mode and actual
connection value with its other migration settings. This default does not
restrict valid agent tuning.
