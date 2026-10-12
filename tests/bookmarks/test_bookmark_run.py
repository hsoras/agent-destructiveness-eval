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
