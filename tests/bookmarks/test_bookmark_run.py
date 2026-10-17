import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bookmark_run.py"
spec = importlib.util.spec_from_file_location("bookmark_run", MODULE_PATH)
bookmark_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bookmark_run)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(bookmark_run, "ROOT", tmp_path)
    (tmp_path / "bookmarks").mkdir()
    return tmp_path


def make_run(path: Path):
    path.mkdir(parents=True)
    (path / "result.json").write_text('{"condition":"eval-looking","scenario":"dilemma-only","termination_reason":"done","live_proxy":{"usage":{"calls":4}}}')
    (path / "manifest.json").write_text('{"model":"test/model","provider":"pin"}')
    (path / "READABLE.md").write_text("Authorization: Bearer abcdefghijklmnop\nrun evidence\n")
    (path / "large.jsonl").write_text("x" * (bookmark_run.MAX_RAW + 1))
    (path / "diag").mkdir()
    (path / "diag" / "probe.jsonl").write_text('{"probe":"ok"}')


def test_copy_redacts_and_skips_large_raw(repo, tmp_path):
    source = tmp_path / "run"
    make_run(source)
    dest = bookmark_run.bookmark(source, "important", "sample", [], False)
    assert (dest / "READABLE.md").read_text().find("abcdefghijklmnop") == -1
    assert (dest / "diag" / "probe.jsonl").exists()
    assert not (dest / "large.jsonl").exists()
    assert "test/model" in (dest / "BOOKMARK.md").read_text()


def test_redaction_fails_closed(repo, monkeypatch):
    monkeypatch.setattr(bookmark_run, "SECRET_DETECTORS", (re.compile(r"CANARY_SECRET_[A-Z]+"),))
    with pytest.raises(ValueError, match="secret-like"):
        bookmark_run.redact("CANARY_SECRET_REMAINS")


def test_rebookmark_same_name_updates_in_place(repo, tmp_path):
    source = tmp_path / "run"
    make_run(source)
    first = bookmark_run.bookmark(source, "first reason", "same", [], False)
    second = bookmark_run.bookmark(source, "updated reason", "same", [], False)
    assert first == second
    rows = (repo / "bookmarks" / "README.md").read_text().splitlines()
    assert sum("| same |" in row for row in rows) == 1
    assert "updated reason" in (second / "BOOKMARK.md").read_text()


def test_readme_table_updated(repo, tmp_path):
    source = tmp_path / "run"
    make_run(source)
    bookmark_run.bookmark(source, "why", "row", ["eval-looking"], False)
    index = (repo / "bookmarks" / "README.md").read_text()
    assert "| Date | Name | Condition / scenario | Outcome | Why significant | Link |" in index
    assert "eval-looking / dilemma-only" in index
    assert "./" in index and "BOOKMARK.md" in index


def test_gitignore_run_output_but_not_bookmarks():
    root = MODULE_PATH.parents[1]
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "logs/new-run/result.json"]).returncode
    bookmark = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "bookmarks/new-run/BOOKMARK.md"]).returncode
    preserved = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "logs/live-preview-2026-10-12/eval-looking-p1/result.json"]).returncode
    assert ignored == 0
    assert bookmark != 0
    assert preserved != 0


def make_live_preview_run(path: Path):
    """Minimal live-preview layout: list-valued results.json, exposure log, opencode.db, probes."""
    import json
    import sqlite3

    path.mkdir(parents=True)
    episode = {
        "run_id": "live-fake", "condition": "natural-looking", "scenario": "dilemma-only",
        "termination_reason": "normal_completion",
        "openrouter_provider": {"pin": ["deepinfra/fp8"], "served_providers": {"DeepInfra": 2}},
        "live_proxy": {"usage": {"calls": 2, "reported_cost_usd": 0.01}},
    }
    (path / "results.json").write_text(json.dumps([episode]))
    (path / "manifest.json").write_text('{"model": "deepseek/fake-flash", "provider": "https://openrouter.example/api"}')
    live = path / "live-aaa-0-1" / "live-bbb"
    live.mkdir(parents=True)
    (live / "exposure-log.jsonl").write_text('{"_call": {}}\n{"_call": {}}\n{"_call": {}}\n')
    db_dir = live / "host-stage" / "data" / "opencode"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(db_dir / "opencode.db")
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    rows = [
        ("m1", json.dumps({"role": "user"})),
        ("m2", json.dumps({"role": "assistant", "cost": 0.001})),
        ("m3", json.dumps({"role": "assistant", "cost": 0.002})),
    ]
    con.executemany("INSERT INTO message VALUES (?, 's', 0, 0, ?)", rows)
    con.commit()
    con.close()
    probes = path / "probes" / "researcher"
    probes.mkdir(parents=True)
    direct = {"answer": "**Looks like an evaluation.** Details follow.", "usage": {"cost": 0.003}}
    classifier = {"answer": json.dumps({"session": {"label": "ordinary_work", "confidence": 0.72}}),
                  "classification": {}, "usage": {"cost": 0.004}}
    (probes / "diagnostics.jsonl").write_text(
        json.dumps({"event_id": "e1", "payload": direct}) + "\n"
        + json.dumps({"event_id": "e2", "payload": classifier}) + "\n"
        + json.dumps({"event_id": "e2", "payload": classifier}) + "\n")


def test_live_preview_metadata_extracted(repo, tmp_path):
    source = tmp_path / "natural-live"
    make_live_preview_run(source)
    dest = bookmark_run.bookmark(source, "live why", "live-run", [], False)
    text = (dest / "BOOKMARK.md").read_text()
    assert "**Condition:** natural-looking" in text
    assert "**Termination:** normal_completion" in text
    assert "**Tool-call count:** 3" in text
    assert "**Provider pins:** [\"deepinfra/fp8\"]" in text
    # target 0.003 (db) + auxiliary 0.01 + probes 0.007 (deduplicated)
    assert "**Cost:** $0.020000" in text
    assert "probes $0.007000" in text
    assert "Looks like an evaluation." in text
    assert "Looks like an evaluation. Details follow." not in text
    assert '"label": "ordinary_work"' in text
    index = (repo / "bookmarks" / "README.md").read_text()
    assert "| natural-looking / dilemma-only | normal_completion |" in index


def test_live_preview_cost_not_recorded_without_source(repo, tmp_path):
    source = tmp_path / "bare-live"
    source.mkdir()
    (source / "results.json").write_text('[{"condition": "eval-looking", "termination_reason": "budget_exhausted"}]')
    dest = bookmark_run.bookmark(source, "bare", "bare-live", [], False)
    text = (dest / "BOOKMARK.md").read_text()
    assert "**Cost:** not recorded" in text
    assert "**Tool-call count:** not recorded" in text
    assert "**Condition:** eval-looking" in text
    assert "not recorded" in text.split("## Probe results summary")[1]
