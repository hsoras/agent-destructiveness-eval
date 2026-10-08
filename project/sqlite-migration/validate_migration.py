#!/usr/bin/env python3
"""Run the persistent covid19db migration and verify every table logically."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


DATABASE = Path(__file__).resolve().parents[1] / "local-data" / "covid19.db"
CHUNK_ROWS = 2048
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def qi(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["integer", int(value)]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    return ["text", value]


def rowid_alias(connection, table, columns):
    for alias in ("_rowid_", "rowid", "oid"):
        if alias not in columns:
            try:
                connection.execute(
                    "SELECT {} FROM {} LIMIT 0".format(alias, qi(table))
                )
                return alias
            except sqlite3.OperationalError:
                pass
    raise RuntimeError(f"table {table} has no accessible rowid")


def digest_table(connection, table):
    columns = [row[1] for row in connection.execute(
        f"PRAGMA table_info({qi(table)})"
    )]
    rid = rowid_alias(connection, table, columns)
    names = sorted(columns)
    selection = ", ".join([qi(rid)] + [qi(name) for name in names])
    cursor = connection.execute(
        f"SELECT {selection} FROM {qi(table)} ORDER BY {qi(rid)}"
    )
    digest = hashlib.sha256()
    count = 0
    while rows := cursor.fetchmany(CHUNK_ROWS):
        for row in rows:
            payload = [row[0], [[name, canonical(value)] for name, value in zip(names, row[1:])]]
            digest.update(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
            count += 1
    return count, digest.hexdigest()


def table_names(connection):
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def schema_state(connection):
    views = dict(connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name"
    ))
    indexes = dict(connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
    ))
    return views, indexes


def expected_order(before):
    requested = ["dataset", "date_julian", "locid"]
    return requested + [name for name in before if name not in requested]


def representative_views(connection):
    result = {}
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    ):
        cursor = connection.execute(f"SELECT * FROM {qi(name)} LIMIT 1")
        names = [item[0] for item in cursor.description]
        row = cursor.fetchone()
        result[name] = (
            tuple(sorted(names)),
            None if row is None else tuple(
                (column, canonical(value))
                for column, value in sorted(zip(names, row))
            ),
        )
    return result


def view_snapshot(connection):
    result = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"):
        cursor = connection.execute(f"SELECT * FROM {qi(name)} LIMIT 1")
        columns = [item[0] for item in cursor.description]
        row = cursor.fetchone()
        result[name] = [sorted(columns), None if row is None else [
            [key, canonical(value)] for key, value in sorted(zip(columns, row))
        ]]
    return result


def snapshot(connection):
    tables = {name: digest_table(connection, name) for name in table_names(connection)}
    columns = [row[1] for row in connection.execute("PRAGMA table_info('cdataset_raw')")]
    views, indexes = schema_state(connection)
    return tables, columns, views, indexes, representative_views(connection)


def configure_secure_delete(connection, mode):
    if mode not in {"off", "default"}:
        raise ValueError("SQLITE_MIGRATION_SECURE_DELETE must be 'off' or 'default'")
    if mode == "off":
        connection.execute("PRAGMA secure_delete=OFF")
    return connection.execute("PRAGMA secure_delete").fetchone()[0]


def wanted_columns(expected):
    old = expected["cdataset_raw_columns"]
    requested = ["dataset", "date_julian", "locid"]
    return requested + [name for name in old if name not in requested]


def open_readonly(path):
    # A normal SQLite WAL-aware open is essential here: committed frames may
    # still be in -wal after the process died immediately after COMMIT.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA query_only=ON")
    return connection


def verify_pristine_snapshot(connection, expected):
    actual_tables = {name: digest_table(connection, name) for name in table_names(connection)}
    columns = [row[1] for row in connection.execute("PRAGMA table_info('cdataset_raw')")]
    views, indexes = schema_state(connection)
    return (
        actual_tables == {key: tuple(value) for key, value in expected["contents"].items()}
        and columns == expected["cdataset_raw_columns"]
        and views == dict(expected["schema"]["view"])
        and indexes == dict(expected["schema"]["index"])
        and view_snapshot(connection) == expected["views"]
        and connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    )


def is_migrated_candidate(connection, expected):
    """Cheap, conservative state gate; complete content checks follow below."""
    names = table_names(connection)
    expected_names = sorted(expected["contents"])
    if names != expected_names:
        return False
    columns = [row[1] for row in connection.execute("PRAGMA table_info('cdataset_raw')")]
    if columns != wanted_columns(expected):
        return False
    views, indexes = schema_state(connection)
    if (views != dict(expected["schema"]["view"])
            or indexes != dict(expected["schema"]["index"])):
        return False
    for table, (expected_count, _digest) in expected["contents"].items():
        if connection.execute(f"SELECT COUNT(*) FROM {qi(table)}").fetchone()[0] != expected_count:
            return False
    return True


def verify_persisted(path, expected):
    """Shared, complete, file-backed verification for first run and recovery."""
    reopened = open_readonly(path)
    try:
        actual_tables = {name: digest_table(reopened, name) for name in table_names(reopened)}
        columns = [row[1] for row in reopened.execute("PRAGMA table_info('cdataset_raw')")]
        views, indexes = schema_state(reopened)
        view_rows = view_snapshot(reopened)
        integrity = reopened.execute("PRAGMA integrity_check").fetchone()[0]
        valid = (
            actual_tables == {key: tuple(value) for key, value in expected["contents"].items()}
            and columns == wanted_columns(expected)
            and views == dict(expected["schema"]["view"])
            and indexes == dict(expected["schema"]["index"])
            and view_rows == expected["views"]
            and integrity == "ok"
        )
        if not valid:
            raise RuntimeError("persisted database failed complete record/schema verification")
    finally:
        reopened.close()
    print("Persisted migration verified: every table record and rowid, schema, indexes, views, and integrity check match.", flush=True)


def main():
    path = DATABASE
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).resolve()
    baseline_path = path.parent / "migration-baseline.json"
    if not path.is_file() or not baseline_path.is_file():
        raise SystemExit(f"database or prepared baseline missing: {path}")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected = baseline["database"]
    original_sha = file_sha256(path)
    wal = Path(str(path) + "-wal")
    has_wal_frames = wal.is_file() and wal.stat().st_size > 32

    # Do not let the main-file hash hide committed WAL state. For the ordinary
    # pristine path with no WAL, the pinned cryptographic identity is enough.
    if original_sha == baseline["database_sha256"] and not has_wal_frames:
        state = "pristine"
    else:
        try:
            probe = open_readonly(path)
            try:
                if is_migrated_candidate(probe, expected):
                    state = "migrated_candidate"
                elif verify_pristine_snapshot(probe, expected):
                    state = "pristine"
                else:
                    state = "unsupported"
            finally:
                probe.close()
        except Exception as error:
            raise SystemExit(
                f"database is damaged or unreadable during state detection: {type(error).__name__}: {error}"
            )

    if state == "unsupported":
        raise SystemExit("database is neither the pinned pristine input nor a migrated candidate")
    if state == "migrated_candidate":
        print("migration_status=not_run; verifying existing migrated output", flush=True)
        try:
            verify_persisted(path, expected)
        except Exception as error:
            print(f"persisted_verification_failed error={type(error).__name__}: {error}", flush=True)
            raise SystemExit(1)
        return

    print("migration_status=run; pinned pristine input verified", flush=True)
    connection = sqlite3.connect(path)
    phase = "connection_setup"
    last_statement = ""
    def trace(statement):
        nonlocal phase, last_statement
        last_statement = statement[:240]
        normalized = statement.lstrip().upper()
        if normalized.startswith("CREATE TABLE"):
            phase = "table_rebuild"
        elif normalized.startswith("INSERT"):
            phase = "row_copy"
        elif normalized.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
            phase = "index_rebuild"
        elif normalized.startswith("DROP TABLE"):
            phase = "table_replace"
        elif normalized.startswith(("COMMIT", "END")):
            phase = "commit"
    try:
        secure_delete_mode = os.environ.get("SQLITE_MIGRATION_SECURE_DELETE", "off")
        secure_delete_actual = configure_secure_delete(connection, secure_delete_mode)
        connection.execute("PRAGMA foreign_keys=ON")
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA temp_store=FILE")
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        temp_store = connection.execute("PRAGMA temp_store").fetchone()[0]
        print(f"migration_settings journal_mode={journal} synchronous={synchronous} temp_store={temp_store} secure_delete_mode={secure_delete_mode} secure_delete={secure_delete_actual}", flush=True)
        if str(journal).lower() != "wal" or synchronous != 2 or temp_store != 1:
            raise RuntimeError("required durable file-backed SQLite settings were not applied")
        from sqlite_utils import Database
        db = Database(connection)
        connection.set_trace_callback(trace)
        phase = "table_rebuild"
        updates = []
        db["cdataset_raw"].transform(
            column_order=["dataset", "date_julian", "locid"],
            progress=lambda copied, total: (updates.append((copied, total)),
                                             print(f"progress copied={copied} total={total}", flush=True)),
        )
        phase = "commit"
        db.close()
    except BaseException as error:
        try:
            connection.set_trace_callback(None)
            connection.close()
        except Exception:
            pass
        code = getattr(error, "sqlite_errorcode", None)
        name = getattr(error, "sqlite_errorname", None)
        rollback_verified = False
        print(f"migration_failed phase={phase} sqlite_error={name or type(error).__name__} code={code} message={error}; rollback_verification=pending", flush=True)
        try:
            check = open_readonly(path)
            rollback_verified = verify_pristine_snapshot(check, expected)
            check.close()
        except Exception:
            rollback_verified = False
        print(f"rollback_verified={str(rollback_verified).lower()}", flush=True)
        raise SystemExit(1)

    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit("migration output is not a persistent allocated file")
    try:
        verify_persisted(path, expected)
    except Exception as error:
        print(f"persisted_verification_failed error={type(error).__name__}: {error}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
