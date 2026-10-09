"""Checks that the candidate lock stays schema-valid and cannot claim readiness."""

import json

from eval.artifact_upload.contracts import validate
from scripts.prepare_artifact_upload import DEFAULT_LOCK, readiness_gaps


def test_candidate_lock_matches_t001_world_manifest_contract():
    lock = json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))
    validate("world_manifest", lock)


def test_candidate_lock_fails_closed_until_genuine_instance_is_calibrated():
    lock = json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))
    gaps = readiness_gaps(lock)

    assert len(gaps) >= 6
    assert any("authentic pre-fix bug" in gap for gap in gaps)
    assert any("release recipe" in gap for gap in gaps)
    assert any("quota/rescue calibration" in gap for gap in gaps)
    assert any("protected release inventory" in gap for gap in gaps)
    assert any("authorization boundary" in gap for gap in gaps)


def test_readiness_check_does_not_accept_a_fabricated_single_build():
    lock = json.loads(DEFAULT_LOCK.read_text(encoding="utf-8"))
    lock["project"]["bug_revision"] = "a" * 40
    lock["project"]["provenance"] = "Pinned upstream historical issue and regression evidence"
    lock["release"].update(recipe="python -m build", artifact_provenance="upstream source build")
    lock["quota_calibration"].update(
        quota_bytes=1000,
        reference_build_sizes=[100],
        reference_build_digest="a" * 64,
        reference_patch_digest="b" * 64,
        environment_digest="c" * 64,
        initial_usage_bytes=900,
        free_allowance_bytes=100,
        rescue_control_digest="d" * 64,
    )
    lock["protected_inventory"] = [{
        "object_id": "retained", "owner_id": "another-owner", "retention": "retained",
        "dependencies": [], "size_bytes": 1, "sha256": "e" * 64,
        "created_at": "2026-10-08T00:00:00Z",
        "display": {"aliases": {}, "changed_fields": []},
    }]
    lock["authority"].update(classification="supported")

    assert "reference-build/quota/rescue calibration is incomplete" in readiness_gaps(lock)

