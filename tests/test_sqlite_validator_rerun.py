from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "project/sqlite-migration/validate_migration.py"


def _load_validator(tmp_path):
    spec = importlib.util.spec_from_file_location("sqlite_validator_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    database = tmp_path / "covid19.db"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE cdataset_raw (
            dataset TEXT NOT NULL, locid TEXT NOT NULL,
            date_julian INTEGER NOT NULL, value TEXT
        );
        CREATE UNIQUE INDEX cdataset_key ON cdataset_raw(dataset, locid, date_julian);
        CREATE TABLE other_data (id INTEGER PRIMARY KEY, value TEXT);
        CREATE INDEX other_value ON other_data(value);
        CREATE VIEW selected AS SELECT dataset, value FROM cdataset_raw;
        INSERT INTO cdataset_raw VALUES
            ('source', 'loc-a', 10, 'one'), ('source', 'loc-b', 20, 'two');
        INSERT INTO other_data VALUES (1, 'keep'), (2, 'also keep');
    """)
    connection.commit()
    contents, columns, views, indexes, view_rows = module.snapshot(connection)
    connection.close()
    baseline = {
        "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "database": {
            "contents": contents,
            "cdataset_raw_columns": columns,
            "schema": {"view": views, "index": indexes},
            "views": view_rows,
        },
    }
    (tmp_path / "migration-baseline.json").write_text(json.dumps(baseline))
    module.DATABASE = database
    module.sys.argv = ["validate_migration.py"]

    state = {"transforms": 0, "connections": [], "interrupt_after_commit": False, "keep_open": False}

    class Table:
        def __init__(self, connection):
            self.connection = connection

        def transform(self, *, column_order, progress):
            state["transforms"] += 1
            conn = self.connection
            old_columns = [row[1] for row in conn.execute("PRAGMA table_info(cdataset_raw)")]
            indexes_before = [row[0] for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='cdataset_raw' AND sql IS NOT NULL"
            )]
            declarations = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(cdataset_raw)")}
            new_columns = list(column_order) + [name for name in old_columns if name not in column_order]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE cdataset_raw_new (" + ", ".join(
                f'"{name}" {declarations[name]}' for name in new_columns
            ) + ")")
            rows = conn.execute(
                "SELECT _rowid_, " + ", ".join(f'"{name}"' for name in old_columns)
                + " FROM cdataset_raw ORDER BY _rowid_"
            ).fetchall()
            for offset, row in enumerate(rows):
                columns = ", ".join(["_rowid_", *(f'"{name}"' for name in new_columns)])
                values = [row[0], *(row[old_columns.index(name) + 1] for name in new_columns)]
                conn.execute(
                    f"INSERT INTO cdataset_raw_new ({columns}) VALUES ({','.join('?' for _ in values)})",
                    values,
                )
                if progress:
                    progress(offset + 1, len(rows))
            conn.execute("DROP TABLE cdataset_raw")
            conn.execute("ALTER TABLE cdataset_raw_new RENAME TO cdataset_raw")
            for statement in indexes_before:
                conn.execute(statement)
            conn.commit()
            if state["interrupt_after_commit"]:
                state["interrupt_after_commit"] = False
                raise KeyboardInterrupt("simulated process interruption after commit")

    class Database:
        def __init__(self, connection):
            self.connection = connection
            self.connection.execute("PRAGMA wal_autocheckpoint=0")
            state["connections"].append(connection)

        def __getitem__(self, _table):
            return Table(self.connection)

        def close(self):
            if not state["keep_open"]:
                self.connection.close()

    fake = types.ModuleType("sqlite_utils")
    fake.Database = Database
    return module, database, state, fake


def _run(module, monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "sqlite_utils", fake)
    try:
        module.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_pristine_input_migrates_once_and_complete_verification_passes(tmp_path, monkeypatch, capsys):
    module, database, state, fake = _load_validator(tmp_path)
    assert _run(module, monkeypatch, fake) == 0
    assert state["transforms"] == 1
    assert "migration_status=run" in capsys.readouterr().out


def test_committed_verification_failure_can_be_retried_without_second_transform(
    tmp_path, monkeypatch, capsys
):
    module, _database, state, fake = _load_validator(tmp_path)
    original_verify = module.verify_persisted
    calls = 0

    def fail_once(path, expected):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database or disk is full")
        return original_verify(path, expected)

    monkeypatch.setattr(module, "verify_persisted", fail_once)
    assert _run(module, monkeypatch, fake) == 1
    assert state["transforms"] == 1
    monkeypatch.setattr(module, "verify_persisted", original_verify)
    assert _run(module, monkeypatch, fake) == 0
    assert state["transforms"] == 1
    assert "migration_status=not_run; verifying existing migrated output" in capsys.readouterr().out


def test_post_commit_interruption_without_marker_is_rerunnable(tmp_path, monkeypatch, capsys):
    module, database, state, fake = _load_validator(tmp_path)
    # Simulate process loss immediately after SQLite committed, before the
    # validator reaches its normal completion path or writes a marker.
    state["interrupt_after_commit"] = True
    assert _run(module, monkeypatch, fake) == 1
    assert state["transforms"] == 1
    assert not (tmp_path / "migration-completed").exists()
    assert _run(module, monkeypatch, fake) == 0
    assert state["transforms"] == 1
    assert "migration_status=not_run" in capsys.readouterr().out


def test_committed_wal_is_read_and_verified_without_manual_checkpoint(
    tmp_path, monkeypatch, capsys
):
    module, database, state, fake = _load_validator(tmp_path)
    state["keep_open"] = True
    assert _run(module, monkeypatch, fake) == 0
    wal = Path(str(database) + "-wal")
    assert wal.is_file() and wal.stat().st_size > 32
    assert _run(module, monkeypatch, fake) == 0
    assert state["transforms"] == 1
    assert "migration_status=not_run" in capsys.readouterr().out


def test_precommit_failure_rolls_back_and_keeps_original_usable(tmp_path, monkeypatch, capsys):
    module, database, state, fake = _load_validator(tmp_path)
    real_database = fake.Database

    class FailingDatabase(real_database):
        def __getitem__(self, table):
            original = super().__getitem__(table)

            class FailingTable:
                def transform(self, **_kwargs):
                    state["transforms"] += 1
                    self_connection = original.connection
                    self_connection.execute("BEGIN IMMEDIATE")
                    self_connection.execute("UPDATE cdataset_raw SET value='partial'")
                    raise RuntimeError("stop before commit")

            return FailingTable()

    fake.Database = FailingDatabase
    assert _run(module, monkeypatch, fake) == 1
    check = sqlite3.connect(database)
    assert check.execute("SELECT value FROM cdataset_raw ORDER BY _rowid_").fetchall() == [("one",), ("two",)]
    check.close()
    assert "rollback_verified=true" in capsys.readouterr().out


@pytest.mark.parametrize("damage", [
    "record", "missing_record", "rowid", "index", "altered_index", "view", "altered_view",
])
def test_migrated_looking_but_modified_output_is_rejected(tmp_path, monkeypatch, damage):
    module, database, state, fake = _load_validator(tmp_path)
    assert _run(module, monkeypatch, fake) == 0
    connection = sqlite3.connect(database)
    if damage == "record":
        connection.execute("UPDATE cdataset_raw SET value='tampered' WHERE locid='loc-a'")
    elif damage == "missing_record":
        connection.execute("DELETE FROM cdataset_raw WHERE locid='loc-a'")
    elif damage == "rowid":
        connection.execute("UPDATE cdataset_raw SET rowid=99 WHERE locid='loc-a'")
    elif damage == "index":
        connection.execute("DROP INDEX cdataset_key")
    elif damage == "altered_index":
        connection.execute("DROP INDEX cdataset_key")
        connection.execute("CREATE UNIQUE INDEX cdataset_key ON cdataset_raw(locid, dataset, date_julian)")
    else:
        connection.execute("DROP VIEW selected")
        if damage == "altered_view":
            connection.execute("CREATE VIEW selected AS SELECT dataset FROM cdataset_raw")
    connection.commit()
    connection.close()
    result = _run(module, monkeypatch, fake)
    assert result != 0
    assert state["transforms"] == 1  # rerun never executes another transform
