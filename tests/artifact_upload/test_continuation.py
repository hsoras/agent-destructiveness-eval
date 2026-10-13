import hashlib
import json
import sqlite3

import pytest

from eval.artifact_upload.continuation import (
    ContinuationBlocked,
    capture_checkpoint,
    continue_run,
    load_checkpoint,
)
from eval.artifact_upload.native_sessions import NativeSessionStore, classify_boundary


class FakeRuntime:
    def __init__(self, binary):
        self.binary = binary


def make_stage(tmp_path, text="hello"):
    stage = tmp_path / "host-stage"
    (stage / "data" / "opencode" / "storage" / "session_diff").mkdir(parents=True)
    (stage / "state").mkdir()
    (stage / "data/opencode/storage/session_diff/s1.json").write_text("[]")
    (stage / "state/frecency").write_text("x")
    conn = sqlite3.connect(stage / "data/opencode/opencode.db")
    conn.execute("pragma journal_mode=wal")
    conn.execute("create table session (id text primary key, directory text)")
    conn.execute("create table message (id text primary key, session_id text, data text)")
    conn.execute("insert into session values ('session-1', '/work')")
    conn.execute("insert into message values ('m1', 'session-1', ?)", (text,))
    conn.commit()  # keep the connection open so recent rows live in the WAL
    return stage, conn


def checkpoint(run_dir, store=None, boundary="T", **extra):
    return capture_checkpoint(run_dir, run_id="run-1", session_id="session-1",
        condition="natural-looking", model_provider_refs={"model": "pinned", "api_key": "sk-secret"},
        workflow={"stage": "upload", "progress": {"completed": 1}},
        ledger={"allowances": {"upload": {"tool_calls": 3}}, "usage": {"upload": {"tool_calls": 2}}},
        resources={"workspace": "workspace-volume"}, native_session=store, boundary=boundary, **extra)


def durable_resources(tmp_path):
    blob = tmp_path / "volume.tar"
    blob.write_bytes(b"volume")
    return lambda: {"durable_volume_export": True, "artifacts": [
        {"owner": "worker", "kind": "volume", "path": str(blob), "sha256": hashlib.sha256(b"volume").hexdigest(), "bytes": 6}]}


def pinned_store(stage, tmp_path):
    binary = tmp_path / "opencode"
    binary.write_bytes(b"binary")
    return NativeSessionStore(stage, runtime=FakeRuntime(binary), runtime_sha256=hashlib.sha256(b"binary").hexdigest())


def ready_kwargs(store):
    return dict(additional_allowances={"upload": {"tool_calls": 1}}, native_session=store,
                restore_resources=lambda s: s, reconcile=lambda c: {"unresolved": []}, resume=lambda *a: None)


def test_snapshot_captures_wal_data_and_blocks_without_pinned_runtime(tmp_path):
    stage, conn = make_stage(tmp_path)
    store = NativeSessionStore(stage, runtime=FakeRuntime(tmp_path / "absent"))
    saved = checkpoint(tmp_path / "run", store)
    assert {p["name"] for p in saved["restore_prerequisites"]} == {"pinned-native-runtime", "durable-guest-service-snapshot"}
    snap = saved["native_session"]["snapshot"]
    assert snap["semantic"]["session_found"] and snap["semantic"]["tables"]["message"]["rows"] == 1
    assert saved["native_session"]["transcript_is_restore_source"] is False
    assert "sk-secret" not in json.dumps(saved)
    conn.close()


def test_boundary_classification_and_truthful_midturn_blocker(tmp_path):
    assert classify_boundary(at_turn_boundary=True) == "T"
    assert classify_boundary(held_calls=1, at_turn_boundary=True) == "M"
    assert classify_boundary() == "unclassified"
    stage, conn = make_stage(tmp_path)
    store = pinned_store(stage, tmp_path)
    for boundary, blocker in (("P", "native-loop-resume-entrypoint"), ("M", "mid-step-tool-state"), (None, "boundary-unclassified")):
        run = tmp_path / f"run-{boundary}"
        saved = checkpoint(run, store, boundary=boundary, resource_snapshot=durable_resources(tmp_path))
        assert [p["name"] for p in saved["restore_prerequisites"]] == [blocker]
        with pytest.raises(ContinuationBlocked, match=blocker):
            continue_run(run, **ready_kwargs(store))
    conn.close()


def test_open_journal_calls_downgrade_a_claimed_turn_boundary(tmp_path):
    stage, conn = make_stage(tmp_path)
    journal = tmp_path / "executions.jsonl"
    journal.write_text(json.dumps({"execution_key": {"call_id": "c1"}, "state": "dispatched"}) + "\n")
    saved = checkpoint(tmp_path / "run", pinned_store(stage, tmp_path), journal_path=journal,
                       resource_snapshot=durable_resources(tmp_path))
    assert saved["native_session"]["boundary"] == "M" and saved["pending_journal"][0]["state"] == "dispatched"
    conn.close()


def test_turn_boundary_restores_and_resumes_with_derived_measurement(tmp_path):
    stage, conn = make_stage(tmp_path, "original")
    store = pinned_store(stage, tmp_path)
    run = tmp_path / "run"
    saved = checkpoint(run, store, resource_snapshot=durable_resources(tmp_path),
                       measurement={"measured": True}, proxy_allowances={"cost_usd": 1.0, "pairs": 5})
    assert saved["restore_prerequisites"] == []
    conn.execute("update message set data='changed after capture'")
    conn.commit()
    conn.close()
    order = []
    result = continue_run(run, additional_allowances={"upload": {"tool_calls": 2}},
        additional_proxy_allowances={"cost_usd": 0.5, "pairs": 2}, native_session=store,
        preflight_resources=lambda s: order.append("pre"), restore_resources=lambda s: order.append("restore") or "res",
        reconcile=lambda c: order.append("reconcile") or {"unresolved": []},
        resume=lambda c, session, res: {"session": session, "res": res})
    assert order == ["pre", "restore", "reconcile"]
    assert result["result"] == {"session": "session-1", "res": "res"}
    assert sqlite3.connect(stage / "data/opencode/opencode.db").execute("select data from message").fetchone()[0] == "original"
    cont = load_checkpoint(run)["continuation"]
    assert cont["segments"][0]["label"] == "extended development evidence" and cont["measured_extension"]
    assert cont["target_allowance_totals"]["upload"]["tool_calls"] == 5
    assert cont["proxy_allowance_totals"] == {"proposer": {"cost_usd": 1.5, "pairs": 7}}  # flat input is the proposer account
    assert cont["segments"][0]["paused_seconds"] >= 0


def test_unmeasured_run_is_not_labelled_extension(tmp_path):
    stage, conn = make_stage(tmp_path)
    store = pinned_store(stage, tmp_path)
    checkpoint(tmp_path / "run", store, resource_snapshot=durable_resources(tmp_path), measurement={"measured": False})
    conn.close()
    continue_run(tmp_path / "run", **ready_kwargs(store))
    assert load_checkpoint(tmp_path / "run")["continuation"]["segments"][0]["label"] == "continuation"


def test_tampered_evidence_blocks_before_any_restore(tmp_path):
    stage, conn = make_stage(tmp_path)
    store = pinned_store(stage, tmp_path)
    checkpoint(tmp_path / "run", store, resource_snapshot=durable_resources(tmp_path))
    conn.close()
    (tmp_path / "run/continuation-evidence/native-session/native-session.db").write_bytes(b"corrupt")
    touched = []
    kwargs = ready_kwargs(store)
    kwargs["restore_resources"] = lambda s: touched.append(1)
    with pytest.raises(ContinuationBlocked, match="evidence-digest-mismatch"):
        continue_run(tmp_path / "run", **kwargs)
    assert touched == []


def test_unreconciled_operations_and_missing_callbacks_block(tmp_path):
    stage, conn = make_stage(tmp_path)
    store = pinned_store(stage, tmp_path)
    checkpoint(tmp_path / "run", store, resource_snapshot=durable_resources(tmp_path))
    conn.close()
    kwargs = ready_kwargs(store)
    with pytest.raises(ContinuationBlocked, match="reconcile-callback"):
        continue_run(tmp_path / "run", **{**kwargs, "reconcile": None})
    with pytest.raises(ContinuationBlocked, match="unresolved-journal-entries"):
        continue_run(tmp_path / "run", **{**kwargs, "reconcile": lambda c: {"unresolved": ["call-1"]}})
    assert load_checkpoint(tmp_path / "run")["last_continue_attempt"]["status"] == "blocked"


def test_allowance_validation_rejects_unknown_stage_and_mixed_units(tmp_path):
    checkpoint(tmp_path)
    for bad in ({"bogus": {"tool_calls": 1}}, {"upload": {"cost_usd": 1}}):
        with pytest.raises(ValueError):
            continue_run(tmp_path, additional_allowances=bad)
    for bad in ({"tool_calls": 1}, {"cost_usd": 0}, {"pairs": 1.5}, {"cost_usd": True}):
        with pytest.raises(ValueError):
            continue_run(tmp_path, additional_proxy_allowances=bad)
    with pytest.raises(ValueError, match="positive"):
        continue_run(tmp_path, additional_allowances={"upload": {"tool_calls": 0}})


def test_callback_error_text_is_not_echoed(tmp_path):
    stage, conn = make_stage(tmp_path)
    store = pinned_store(stage, tmp_path)
    checkpoint(tmp_path / "run", store, resource_snapshot=durable_resources(tmp_path))
    conn.close()
    def boom(state):
        raise RuntimeError("Bearer sk-live-123")
    with pytest.raises(ContinuationBlocked) as info:
        continue_run(tmp_path / "run", preflight_resources=boom, **ready_kwargs(store))
    assert "sk-live" not in str(info.value) and "sk-live" not in json.dumps(load_checkpoint(tmp_path / "run"))


def test_live_inventory_is_not_durable(tmp_path):
    saved = checkpoint(tmp_path, resource_snapshot=lambda: {"kind": "live-resource-inventory", "durable_volume_export": False})
    assert "durable-guest-service-snapshot" in {p["name"] for p in saved["restore_prerequisites"]}


def test_checkpoint_digest_detects_tampering(tmp_path):
    checkpoint(tmp_path)
    path = tmp_path / "checkpoint.json"
    value = json.loads(path.read_text())
    value["workflow"]["stage"] = "fresh"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        load_checkpoint(tmp_path)
