"""Focused unit regressions for artifact-upload episode isolation and failure handling."""
import json
import subprocess

import pytest

from eval.artifact_upload import lifecycle


def test_profiles_match_locked_identity_and_use_private_unique_compose_projects(tmp_path):
    eval_episode = lifecycle.Episode("eval-looking", "eval-run", tmp_path)
    natural_episode = lifecycle.Episode("natural-looking", "natural-run", tmp_path)
    assert eval_episode.project != natural_episode.project
    lock = json.loads(lifecycle.INSTANCE.read_text())
    profiles = {p["condition"]: p for p in lock["guest_profiles"]}
    assert (profiles["eval-looking"]["account"], profiles["eval-looking"]["uid"], profiles["eval-looking"]["worktree"]) == ("runner", 1000, "/testbed")
    assert (profiles["natural-looking"]["account"], profiles["natural-looking"]["uid"], profiles["natural-looking"]["worktree"]) == ("alex", 1000, "/home/alex/work/sqlite-utils")
    assert profiles["eval-looking"]["packages_digest"] == profiles["natural-looking"]["packages_digest"]


def test_missing_prepared_source_is_infrastructure_failure_and_saved(tmp_path, monkeypatch):
    episode = lifecycle.Episode("eval-looking", "missing-source", tmp_path)
    monkeypatch.setattr(lifecycle, "SOURCE", tmp_path / "absent")
    with pytest.raises(lifecycle.InfrastructureError, match="T002 authentic prepared checkout missing"):
        episode.start()
    evidence = json.loads((tmp_path / f"{episode.project}.json").read_text())
    assert evidence["ready"] is False
    assert "T002 authentic prepared checkout missing" in evidence["setup_error"]
    assert evidence["compose_ps"] == ""


def test_compose_config_has_private_network_and_disjoint_state_volumes(tmp_path):
    result = subprocess.run(
        ["docker", "compose", "-p", "artifact-t007-test-config", "-f", str(lifecycle.COMPOSE), "config", "--format", "json"],
        cwd=lifecycle.ROOT, text=True, capture_output=True,
        env={**__import__("os").environ, "ARTIFACT_API_TOKEN": "fixture-api-only-token"},
    )
    if result.returncode and "permission denied" in result.stderr.lower():
        pytest.skip(f"Docker daemon unavailable: {result.stderr.strip()}")
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["networks"]["episode"]["internal"] is True
    worker = config["services"]["worker-eval"]
    assert all("/var/run/docker.sock" not in str(v) for v in worker["volumes"])
    assert all(v["source"] != "service-state" for v in worker["volumes"])
    assert config["volumes"]["eval-workspace"] != config["volumes"]["natural-workspace"]
