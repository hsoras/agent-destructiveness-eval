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


def test_unknown_event_preserves_worker_exception_diagnostics(broker, monkeypatch):
    host, worker, _tmp_path = broker
    request = call("shell", {"command": "true"})

    def invalid_call(_call):
        raise ValueError("invalid_call: argument digest mismatch")

    monkeypatch.setattr(worker, "start", invalid_call)
    monkeypatch.setattr(worker, "process_handle", lambda _call: None)
    monkeypatch.setattr(worker, "status", lambda _key: (_ for _ in ()).throw(LookupError("no execution record")))
    result = host.execute(request)
    assert result["status"] == "unknown"
    event = next(event for event in reversed(host.events) if event["event_type"] == "tool.unknown")
    assert event["metadata"]["exception_type"] == "ValueError"
    assert event["metadata"]["exception"] == "invalid_call: argument digest mismatch"


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


@pytest.mark.parametrize("phase", ["registered", "dispatched", "lost_ack", "lost_ack_no_handle", "running", "completed"])
def test_cancellation_intent_survives_every_dispatch_phase(broker, monkeypatch, phase):
    """An accepted cancel is never lost between registration, dispatch, start acknowledgement and collection."""
    host, worker, tmp_path = broker
    marker = tmp_path / "guest" / "mutated"
    request = call("shell", {"command": "printf done" if phase == "completed" else "sleep 1; touch mutated"}, f"phase-{phase}")
    key = {name: request[name] for name in ("run_id", "session_id", "call_id", "argument_digest")}
    real_start, starts = worker.start, []
    entered, release = threading.Event(), threading.Event()

    def gated_start(value):
        starts.append(value["call_id"])
        entered.set()
        assert release.wait(10)
        handle = real_start(value)
        if phase.startswith("lost_ack"):
            raise ConnectionError("start acknowledgement lost after crossing the worker boundary")
        return handle

    if phase == "registered":
        cancelled = host.cancel(request)
        assert cancelled["status"] == "cancelled" and cancelled["error_code"] == "cancelled_before_dispatch"
        assert host.execute(request) == cancelled
        assert starts == [] and host.journal.get(key)["state"] == "cancelled"
        return
    if phase == "completed":
        done = host.execute(request)
        assert host.cancel(request) == done and done["status"] == "ok"
        assert host.journal.get(key)["state"] == "completed"
        return
    if phase == "lost_ack_no_handle":
        # As GuestWorkerSession: no handle; recovery is by stable-key status only.
        monkeypatch.setattr(worker, "process_handle", lambda _call: None)
    if phase != "running":
        monkeypatch.setattr(worker, "start", gated_start)
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("result", host.execute(request)))
    thread.start()
    try:
        if phase == "running":
            deadline = time.monotonic() + 4
            while (host.journal.get(key) or {}).get("state") != "running":
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert host.cancel(request)["status"] == "cancelled"
        else:
            assert entered.wait(4)
            assert host.journal.get(key)["state"] == "dispatched"
            pending = host.cancel(request)
            # The worker does not know the execution yet: report the truth, keep the intent.
            assert pending["status"] == "unknown" and pending["error_code"] == "cancellation_pending"
            release.set()
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["result"]["status"] == "cancelled", outcome
    assert host.journal.get(key)["state"] == "cancelled"
    time.sleep(1.5)  # past the point where the abandoned command would have mutated the guest
    assert not marker.exists(), "accepted cancellation let the command mutate the guest"
    for event in host.events:
        validate("event", event)


@pytest.mark.parametrize("evidence", ["completed", "unavailable"])
def test_pending_cancel_after_lost_ack_without_handle_keeps_honest_evidence(broker, monkeypatch, evidence):
    """Status-only recovery with a pending cancel: completed stays completed; no evidence stays unknown, never replayed."""
    host, worker, tmp_path = broker
    request = call("shell", {"command": "printf once >> count"}, f"pending-{evidence}")
    key = {name: request[name] for name in ("run_id", "session_id", "call_id", "argument_digest")}
    real_start, real_status, real_cancel = worker.start, worker.status, worker.cancel
    starts, cancellations = [], []
    entered, release = threading.Event(), threading.Event()

    def gated_start(value):
        starts.append(value["call_id"])
        entered.set()
        assert release.wait(10)
        real_start(value)
        deadline = time.monotonic() + 4
        while real_status(key)["state"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        if evidence == "unavailable":
            monkeypatch.setattr(worker, "status", lambda _key: (_ for _ in ()).throw(ConnectionError("status outage")))
        raise ConnectionError("start acknowledgement lost after crossing the worker boundary")

    monkeypatch.setattr(worker, "process_handle", lambda _call: None)
    monkeypatch.setattr(worker, "start", gated_start)
    monkeypatch.setattr(worker, "cancel", lambda value, how="terminate": cancellations.append(value) or real_cancel(value, how=how))
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("result", host.execute(request)))
    thread.start()
    try:
        assert entered.wait(4)
        assert host.cancel(request)["error_code"] == "cancellation_pending"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    result = outcome["result"]
    if evidence == "completed":
        assert result["status"] == "ok" and host.journal.get(key)["state"] == "completed"
        assert result == real_status(key)["result"]
        assert host._cancel_intent == set()
    else:
        assert result["status"] == "unknown" and host.journal.get(key)["state"] == "unknown"
        assert host.execute(request) == result
    assert cancellations == [] and starts == [request["call_id"]]
    assert (tmp_path / "guest" / "count").read_text() == "once"


def test_cancel_reconciliation_never_overwrites_the_collected_terminal_result(broker, monkeypatch):
    """The worker's collected cancellation wins over a concurrent reconcile that lost the worker's record."""
    host, worker, _ = broker
    request = call("shell", {"command": "sleep 5"}, "race")
    key = {name: request[name] for name in ("run_id", "session_id", "call_id", "argument_digest")}
    real_stream = worker.stream
    queried, delivered = threading.Event(), threading.Event()

    def stream(value):
        for event in real_stream(value):
            if event["type"] == "result":
                # Hold the terminal result until cancel's reconciliation has read the running entry.
                assert queried.wait(5)
                delivered.set()
            yield event

    def status_after_delivery(value):
        # As observed on the real session transport, the worker has no record once the result was delivered.
        if queried.is_set():
            raise ConnectionError("worker dropped the delivered execution")
        queried.set()
        assert delivered.wait(5)
        # Let an unserialised terminal write land first (the observed interleaving). A serialised
        # broker blocks that write behind this reconcile, so the bounded wait simply expires.
        deadline = time.monotonic() + 1
        while host.journal.get(key)["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        raise ConnectionError("worker dropped the delivered execution")

    monkeypatch.setattr(worker, "stream", stream)
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("result", host.execute(request)))
    thread.start()
    deadline = time.monotonic() + 4
    while (host.journal.get(key) or {}).get("state") != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    real_cancel = worker.cancel

    def cancel_then_lose_record(value, *, how="terminate"):
        sent = real_cancel(value, how=how)
        monkeypatch.setattr(worker, "status", status_after_delivery)
        return sent

    monkeypatch.setattr(worker, "cancel", cancel_then_lose_record)
    final = host.cancel(request)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["result"]["status"] == "cancelled"
    assert host.journal.get(key)["state"] == "cancelled", host.journal.get(key)
    assert final["status"] == "cancelled", final
