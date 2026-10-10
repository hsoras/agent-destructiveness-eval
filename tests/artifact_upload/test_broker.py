from __future__ import annotations

import hashlib
import json
import os
import threading
import time

import pytest

from eval.artifact_upload.broker import ExecutionBroker, IdentityConflict
from eval.artifact_upload.contracts import argument_digest, validate
from sandbox.artifact_upload.worker import GuestWorker


def call(operation="shell", arguments=None, call_id="c1", run_id="r1"):
    arguments = arguments or {"command": "printf once"}
    names = {"shell": "bash", "write": "write", "read": "read"}
    return {"contract_version": "artifact-upload.v1", "run_id": run_id, "session_id": "s1",
            "call_id": call_id, "tool_name": names[operation], "operation": operation,
            "arguments": arguments, "argument_digest": argument_digest(arguments), "guest_profile": "eval-looking"}


@pytest.fixture
def broker(tmp_path):
    root = tmp_path / "guest"
    root.mkdir()
    worker = GuestWorker(root, output_dir=tmp_path / "spool")
    return ExecutionBroker("r1", worker, str(tmp_path / "private" / "journal.jsonl")), worker, tmp_path


def test_duplicate_terminal_conflict_and_distinct_calls_are_real_worker_executions(broker):
    host, _worker, tmp_path = broker
    first = call("shell", {"command": "printf run >> count; cat count"})
    result = host.execute(first)
    assert result["stdout"] == "run"
    assert host.execute(first) == result
    assert (tmp_path / "guest" / "count").read_text() == "run"
    changed = call("shell", {"command": "printf replay >> count"})
    with pytest.raises(IdentityConflict):
        host.execute({**changed, "call_id": first["call_id"]})
    distinct = call("shell", {"command": "printf new >> count; cat count"}, "c2")
    assert host.execute(distinct)["stdout"] == "runnew"
    assert (tmp_path / "guest" / "count").read_text() == "runnew"
    assert len(host.events) >= 6
    assert all(validate("event", event) is None for event in host.events)
    assert (tmp_path / "private" / "journal.jsonl").stat().st_mode & 0o777 == 0o600
    persisted = [json.loads(line) for line in (tmp_path / "private" / "tool_events.jsonl").read_text().splitlines()]
    assert persisted == host.events
    assert (tmp_path / "private" / "tool_events.jsonl").stat().st_mode & 0o777 == 0o600


def test_journal_cannot_be_created_in_target_workspace(broker):
    _host, worker, tmp_path = broker
    with pytest.raises(ValueError, match="outside the target workspace"):
        ExecutionBroker("r1", worker, str(tmp_path / "guest" / "journal.jsonl"))


def test_dispatch_boundary_disconnect_reconciles_worker_handle_without_replay(broker, monkeypatch):
    host, worker, tmp_path = broker
    request = call("shell", {"command": "printf one >> count; sleep 0.4"})
    real_stream = worker.stream
    real_start = worker.start
    calls = {"start": 0, "stream": 0}

    def counted_start(value):
        calls["start"] += 1
        return real_start(value)

    def lost_stream(key):
        calls["stream"] += 1
        if calls["stream"] == 1:
            raise ConnectionError("simulated response disconnect")
        return real_stream(key)

    monkeypatch.setattr(worker, "start", counted_start)
    monkeypatch.setattr(worker, "stream", lost_stream)
    unknown = host.execute(request)
    assert unknown["status"] == "unknown"
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            resolved = host.reconcile(request)
            if resolved["status"] == "ok":
                break
        except Exception:
            pass
        time.sleep(0.02)
    assert resolved["status"] == "ok"
    assert host.execute(request) == resolved
    assert calls["start"] == 1
    assert (tmp_path / "guest" / "count").read_text() == "one"


def test_restart_reconciles_live_handle_and_unknown_never_redelivers(broker, monkeypatch):
    host, worker, tmp_path = broker
    request = call("shell", {"command": "printf one >> count; sleep 0.25"})
    original_stream = worker.stream
    monkeypatch.setattr(worker, "stream", lambda _key: (_ for _ in ()).throw(ConnectionError("lost")))
    assert host.execute(request)["status"] == "unknown"
    restarted = ExecutionBroker("r1", worker, str(tmp_path / "private" / "journal.jsonl"))
    monkeypatch.setattr(worker, "stream", original_stream)
    deadline = time.monotonic() + 4
    while True:
        result = restarted.reconcile(request)
        if result["status"] != "unknown" or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    assert result["status"] == "ok"
    assert (tmp_path / "guest" / "count").read_text() == "one"


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("evidence_state", ["running", "completed", "unavailable"])
@pytest.mark.parametrize("consumer", ["execute", "reconcile", "cancel"])
def test_lost_ack_recovery_consumers(broker, monkeypatch, restart, evidence_state, consumer):
    host, worker, tmp_path = broker
    command = "printf once >> count; " + ("printf completed" if evidence_state == "completed" else "sleep 30")
    request = call("shell", {"command": command}, "lost-ack")
    key = host._key(request)
    real_start, real_status, real_cancel = worker.start, worker.status, worker.cancel
    starts, cancellations = [], []

    def lost_ack(value):
        handle = real_start(value)
        if value["call_id"] == "lost-ack":
            starts.append(handle)
            raise ConnectionError("worker launched command, start response lost")
        return handle

    def unavailable(_key):
        raise ConnectionError("temporary status outage")

    def counted_cancel(value, *, how="terminate"):
        cancellations.append((value, how))
        return real_cancel(value, how=how)

    monkeypatch.setattr(worker, "start", lost_ack)
    monkeypatch.setattr(worker, "status", unavailable)
    monkeypatch.setattr(worker, "cancel", counted_cancel)
    try:
        assert host.execute(request)["status"] == "unknown"
        assert host.journal.get(key)["state"] == "unknown"
        deadline = time.monotonic() + 4
        while not (tmp_path / "guest" / "count").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        if restart:
            host = ExecutionBroker("r1", worker, str(tmp_path / "private" / "journal.jsonl"))
        assert host.execute(request)["status"] == "unknown"
        assert len(starts) == 1

        if evidence_state != "unavailable":
            monkeypatch.setattr(worker, "status", real_status)
        if evidence_state == "completed":
            deadline = time.monotonic() + 4
            while real_status(key)["state"] != "completed":
                assert time.monotonic() < deadline
                time.sleep(0.01)
        else:
            assert real_status(key)["state"] == "running"

        result = getattr(host, consumer)(request)
        if evidence_state == "unavailable":
            assert result["status"] == "unknown"
            assert host.journal.get(key)["state"] == "unknown"
            assert host.execute(request) == result
            assert cancellations == []
            assert real_status(key)["state"] == "running"
        else:
            if evidence_state == "running":
                if consumer != "cancel":
                    assert result["status"] == "unknown"
                    assert host.journal.get(key)["state"] == "running"
                    result = host.cancel(request)
                assert cancellations == [(key, "terminate")]
                assert result["status"] == "cancelled"
                assert host.journal.get(key)["state"] == "cancelled"
            else:
                assert result["status"] == "ok"
                assert result["stdout"] == "completed"
                assert host.journal.get(key)["state"] == "completed"
                assert cancellations == []
            assert real_status(key)["state"] == "completed"
            assert result == real_status(key)["result"]
            assert host.execute(request) == result
            assert host.reconcile(request) == result
            before = list(cancellations)
            assert host.cancel(request) == result
            assert cancellations == before
        assert len(starts) == 1
        assert (tmp_path / "guest" / "count").read_text() == "once"
        if evidence_state == "completed":
            # Preserve independent delivery after recovering an uncertain call.
            distinct = call("shell", {"command": "printf another >> count; printf next"}, "distinct-after-lost-ack")
            assert host.execute(distinct)["stdout"] == "next"
            assert (tmp_path / "guest" / "count").read_text() == "onceanother"
    finally:
        real_cancel(key, how="kill")
        deadline = time.monotonic() + 4
        while real_status(key)["state"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_uncertain_after_worker_loss_is_unknown_and_cancel_kills_process_group(broker, monkeypatch, tmp_path):
    host, worker, _ = broker
    request = call("shell", {"command": "printf one >> count; sleep 10"})
    monkeypatch.setattr(worker, "stream", lambda _key: (_ for _ in ()).throw(ConnectionError("lost")))
    assert host.execute(request)["status"] == "unknown"
    monkeypatch.setattr(worker, "status", lambda _key: (_ for _ in ()).throw(ConnectionError("worker gone")))
    lost = host.reconcile(request)
    assert lost["status"] == "unknown"
    with pytest.raises(IdentityConflict):
        host.execute({**request, "arguments": {"command": "printf replay"}, "argument_digest": argument_digest({"command": "printf replay"})})
    # A new call remains a separate execution; cancellation is forwarded and its
    # terminal status is retained by the real worker.
    monkeypatch.undo()
    cancellable = call("shell", {"command": "sleep 30"}, "cancel-me")
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("result", host.execute(cancellable)))
    thread.start()
    deadline = time.monotonic() + 4
    while host.journal.by_call("r1", "s1", "cancel-me") is None or host.journal.by_call("r1", "s1", "cancel-me")["state"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    final = host.cancel(cancellable)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert final["status"] == "cancelled"
    assert outcome["result"]["status"] == "cancelled"
    assert host.invalidate_filesystem_facts(cancellable)
    read = call("read", {"filePath": "count"}, "read1")
    assert not host.invalidate_filesystem_facts(read)
    for event in host.events:
        validate("event", event)
