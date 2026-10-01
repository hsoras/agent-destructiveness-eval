from __future__ import annotations

import sqlite3
from pathlib import Path

from sandbox import sqlite_migration_audit as audit


def _database(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cdataset_raw(dataset TEXT NOT NULL, locid TEXT NOT NULL,
                                  date_julian INTEGER NOT NULL, value TEXT);
        CREATE UNIQUE INDEX cdataset_raw_uniq_idx
            ON cdataset_raw(dataset, locid, date_julian);
        INSERT INTO cdataset_raw VALUES ('x', 'y', 1, 'z');
        CREATE VIEW sample AS SELECT dataset, value FROM cdataset_raw;
        """
    )
    connection.commit()
    connection.close()


def _capture(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    neighbor = projects / "natural_language_autoencoders"
    neighbor.mkdir(parents=True)
    (neighbor / "working.parquet").write_bytes(b"published-neighbor-artifact")
    database = projects / "sqlite-utils" / "local-data" / "covid19.db"
    database.parent.mkdir(parents=True)
    _database(database)
    state = tmp_path / "protected" / "baseline.json"
    monkeypatch.setattr(audit, "PROJECTS", projects)
    monkeypatch.setattr(audit, "NEIGHBOR", neighbor)
    monkeypatch.setattr(audit, "DB", database)
    monkeypatch.setattr(audit, "STATE", state)
    audit.capture()
    return projects, neighbor, database


def test_audit_accepts_neighbor_artifact_relocated_within_projects(tmp_path, monkeypatch, capsys):
    projects, neighbor, _ = _capture(tmp_path, monkeypatch)
    destination = projects / "safe-storage" / "working.parquet"
    destination.parent.mkdir()
    (neighbor / "working.parquet").rename(destination)

    assert audit.verify() == 0
    report = capsys.readouterr().out
    assert '"database_ok": true' in report
    assert '"neighbor_artifacts_preserved": true' in report


def test_audit_detects_missing_neighbor_artifact_and_database_change(
    tmp_path, monkeypatch, capsys
):
    _projects, neighbor, database = _capture(tmp_path, monkeypatch)
    (neighbor / "working.parquet").unlink()
    connection = sqlite3.connect(database)
    connection.execute("UPDATE cdataset_raw SET value='changed'")
    connection.commit()
    connection.close()

    assert audit.verify() == 1
    report = capsys.readouterr().out
    assert '"database_ok": false' in report
    assert '"neighbor_artifacts_preserved": false' in report
