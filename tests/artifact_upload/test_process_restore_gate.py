"""Cold T restore fails closed unless guest process state is attested restorable or absent."""
import hashlib
import json
from copy import deepcopy

import pytest

from eval.artifact_upload.continuation import (ContinuationBlocked, PROCESS_ATTESTATION_SCHEMA, _save, load_checkpoint,
                                               process_restore_problem)
from tests.artifact_upload.test_runtime_resume import FakeAdapter, go, paused  # noqa: F401  (fixture)

GAP = {"name": "target-background-processes", "reason": "running guest processes and in-memory state are not restorable"}


def attestation(tmp_path, **override):
    body = {"schema": PROCESS_ATTESTATION_SCHEMA, "method": "enforced-process-free-boundary", "quiescent": True,
            "required_process_state_uncaptured": False}
    body.update(override)
    raw = json.dumps(body, sort_keys=True).encode()
    path = tmp_path / "process-attestation.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    claim = {k: body[k] for k in ("schema", "method", "quiescent", "required_process_state_uncaptured")}
    claim["evidence_sha256"] = digest
    art = {"kind": "process-state-attestation", "owner": "episode", "path": str(path), "sha256": digest, "bytes": len(raw)}
    return claim, art


def eligible(tmp_path, snapshot=None, **override):
    snap = deepcopy(snapshot) if snapshot else {"durable_volume_export": True, "quiescent": True, "artifacts": [], "gaps": []}
    snap["durable_volume_export"], snap["quiescent"], snap["gaps"] = True, True, []
    claim, art = attestation(tmp_path, **override)
    snap["process_state_attestation"] = claim
    snap["artifacts"] = [a for a in snap.get("artifacts", []) if a["kind"] != "process-state-attestation"] + [art]
    return snap


def test_eligible_fixture_passes_only_with_an_enforcement_verifier(tmp_path):
    snap = eligible(tmp_path)
    assert process_restore_problem(snap, lambda s, b: True) is None
    assert "no verifier" in process_restore_problem(snap)  # production: nothing can confirm enforced absence
    assert process_restore_problem(snap, lambda s, b: False)
    assert process_restore_problem(snap, lambda s, b: 1 / 0)
    claim, art = attestation(tmp_path, method="guest-process-checkpoint")
    snap["process_state_attestation"], snap["artifacts"][-1] = claim, art
    assert "malformed" in process_restore_problem(snap, lambda s, b: True)  # no process restore implementation exists


def test_production_hooks_never_restore_even_with_eligible_evidence(paused):
    paused.hooks.pop("process_attestation_verifier")
    with pytest.raises(ContinuationBlocked, match="no verifier"):
        go(paused)
    assert paused.episodes == [] and paused.worker_calls == [] and FakeAdapter.log == []


@pytest.mark.parametrize("mutate", [
    lambda s: s.__setitem__("gaps", [GAP]),
    lambda s: s.__setitem__("gaps", [{"name": "Guest-Process-State"}]),
    lambda s: s.pop("process_state_attestation"),
    lambda s: s.__setitem__("process_state_attestation", "yes"),
    lambda s: s["process_state_attestation"].__setitem__("required_process_state_uncaptured", True),
    lambda s: s["process_state_attestation"].__setitem__("required_process_state_uncaptured", None),
    lambda s: s["process_state_attestation"].__setitem__("quiescent", "true"),
    lambda s: s["process_state_attestation"].__setitem__("method", "docker-top"),
    lambda s: s["process_state_attestation"].__setitem__("schema", "other"),
    lambda s: s["process_state_attestation"].__setitem__("evidence_sha256", "abc"),
    lambda s: s.__setitem__("artifacts", [a for a in s["artifacts"] if a["kind"] != "process-state-attestation"]),
    lambda s: s.__setitem__("quiescent", False),
    lambda s: s.__setitem__("durable_volume_export", False),
    lambda s: s.__setitem__("gaps", "none"),
])
def test_gap_unknown_malformed_or_missing_attestation_blocks(tmp_path, mutate):
    snap = eligible(tmp_path)
    mutate(snap)
    assert process_restore_problem(snap, lambda s, b: True)


def test_evidence_tamper_or_mismatch_blocks(tmp_path):
    snap = eligible(tmp_path)
    open(snap["artifacts"][-1]["path"], "ab").write(b" ")
    assert "digest mismatch" in process_restore_problem(snap, lambda s, b: True)
    # Evidence that is self-consistent but says something else does not support the claim.
    snap = eligible(tmp_path)
    claim, art = attestation(tmp_path, required_process_state_uncaptured=True)
    snap["process_state_attestation"]["evidence_sha256"] = art["sha256"]
    snap["artifacts"][-1] = art
    assert "does not support" in process_restore_problem(snap, lambda s, b: True)


def test_non_dict_snapshot_blocks():
    assert process_restore_problem(None, lambda s, b: True) and process_restore_problem([], lambda s, b: True)


def rewrite_snapshot(run, snapshot):
    checkpoint = load_checkpoint(run)
    checkpoint["guest_and_service"] = snapshot
    checkpoint["restore_prerequisites"] = [x for x in checkpoint["restore_prerequisites"] if not x["name"].startswith("coverage-gap")]
    _save(run, checkpoint)


@pytest.mark.parametrize("variant", ["known-gap", "missing", "malformed"])
def test_blocked_snapshot_creates_no_episode_worker_or_native_turn(paused, variant):
    snap = load_checkpoint(paused.run)["guest_and_service"]
    if variant == "known-gap":
        snap["gaps"] = [GAP]
    elif variant == "missing":
        snap.pop("process_state_attestation", None)
    else:
        snap["process_state_attestation"] = {"schema": PROCESS_ATTESTATION_SCHEMA}
    rewrite_snapshot(paused.run, snap)
    with pytest.raises(ContinuationBlocked, match="guest-process-state-unrestorable"):
        go(paused)
    assert paused.episodes == [] and paused.worker_calls == [] and FakeAdapter.log == []
    attempt = load_checkpoint(paused.run)["last_continue_attempt"]
    assert attempt["status"] == "blocked" and attempt["missing"][0]["name"] == "guest-process-state-unrestorable"
    assert attempt["missing"][0]["reason"]
    assert load_checkpoint(paused.run)["continuation"]["segments"] == []


def test_eligible_evidence_backed_snapshot_restores_and_runs(paused, tmp_path):
    snap = eligible(tmp_path, load_checkpoint(paused.run)["guest_and_service"])
    rewrite_snapshot(paused.run, snap)
    out = go(paused, finalize=lambda ctx: {"scored": True})
    assert out["result"]["termination_reason"] == "normal_completion"
    assert any("restore-s1" in e.events for e in paused.episodes)
    assert len(FakeAdapter.log) == 1
