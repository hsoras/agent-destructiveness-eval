from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

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


def test_precomputed_baseline_is_database_hash_bound_and_protected(
    tmp_path, monkeypatch, capsys
):
    _projects, _neighbor, database = _capture(tmp_path, monkeypatch)
    precomputed = tmp_path / "precomputed.json"
    precomputed.write_text(audit.STATE.read_text(encoding="utf-8"), encoding="utf-8")

    protected_state = tmp_path / "next-protected" / "baseline.json"
    monkeypatch.setattr(audit, "STATE", protected_state)
    audit.capture(precomputed)
    saved = json.loads(protected_state.read_text(encoding="utf-8"))
    assert saved["database"] == json.loads(precomputed.read_text())["database"]
    assert protected_state.stat().st_mode & 0o777 == 0o400
    assert '"captured": true' in capsys.readouterr().out

    connection = sqlite3.connect(database)
    connection.execute("UPDATE cdataset_raw SET value='changed'")
    connection.commit()
    connection.close()
    monkeypatch.setattr(audit, "STATE", tmp_path / "changed-protected" / "baseline.json")
    with pytest.raises(RuntimeError, match="precomputed protected baseline"):
        audit.capture(precomputed)
