"""Host-side execution broker: durable deduplication around the guest worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .contracts import argument_digest, validate, validate_result_for_call
from .journal import ExecutionJournal


class IdentityConflict(ValueError):
    """A call ID was reused with different arguments in the same run/session."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class ExecutionBroker:
    """Bind calls to one run and keep retries from replaying uncertain mutations.

    ``worker`` is the T005 GuestWorker or its compatible transport facade. A
    transport must implement execute/start/stream/status/cancel as supported by
    the worker. The journal path belongs to the host and must not be mounted in
    the target workspace.
    """

    def __init__(self, run_id: str, worker: Any, journal_path: str):
        if not run_id:
            raise ValueError("run_id is required")
        worker_root = getattr(worker, "root", None)
        if worker_root is not None:
            journal_target = Path(journal_path).resolve()
            guest_root = Path(worker_root).resolve()
            if journal_target == guest_root or guest_root in journal_target.parents:
                raise ValueError("execution journal must be outside the target workspace")
        self.run_id, self.worker = run_id, worker
        self.journal = ExecutionJournal(journal_path)
        self._lock = threading.RLock()
        # Cancellations accepted while a shell start may still be crossing the
        # worker boundary. The dispatching thread owns carrying them out.
        self._cancel_intent: set[tuple[str, ...]] = set()
        self.events: list[dict[str, Any]] = []

    @staticmethod
    def _key(call: dict[str, Any]) -> dict[str, str]:
        return {name: call[name] for name in ("run_id", "session_id", "call_id", "argument_digest")}

    @staticmethod
    def _intent_key(key: dict[str, str]) -> tuple[str, ...]:
        return tuple(key[name] for name in ("run_id", "session_id", "call_id", "argument_digest"))

    def _take_intent(self, key: dict[str, str]) -> bool:
        with self._lock:
            intent = self._intent_key(key)
            if intent not in self._cancel_intent:
                return False
            self._cancel_intent.discard(intent)
            return True

    def _event(self, call: dict[str, Any], kind: str, status: str, raw: Any, presented: Any) -> None:
        event = {"event_id": uuid.uuid4().hex,
                            "event_type": kind, "occurred_at": datetime.now(timezone.utc).isoformat(),
                            "run_id": call["run_id"], "session_id": call["session_id"], "call_id": call["call_id"],
                            "actor": "broker", "status": status, "raw_digest": _digest(raw),
                            "presented_digest": _digest(presented), "metadata": {"operation": call["operation"]}}
        validate("event", event)
        self.journal.append_event(event)
        self.events.append(event)

    def _validate_call(self, call: dict[str, Any]) -> dict[str, str]:
        validate("call", call)
        if call["run_id"] != self.run_id:
            raise ValueError("call belongs to a different run")
        if argument_digest(call["arguments"]) != call["argument_digest"]:
            raise ValueError("argument digest mismatch")
        return self._key(call)

    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        """Execute once, return a stored terminal response on duplicate delivery."""
        key = self._validate_call(call)
        with self._lock:
            prior = self.journal.by_call(key["run_id"], key["session_id"], key["call_id"])
            if prior:
                if prior["execution_key"] != key:
                    raise IdentityConflict("call ID reused with different arguments")
                if prior["result"] is not None and prior["state"] != "unknown":
                    return prior["result"]
                if prior["state"] == "unknown":
                    return self.reconcile(call)
                return self._unknown(call, "execution_in_progress", "execution is accepted but not yet reconciled")

            self.journal.put(key, "accepted")
            self._event(call, "tool.accepted", "ok", call, {"state": "accepted"})
        with self._lock:
            # Commit dispatch only if no cancellation won the race to this point.
            entry = self.journal.get(key)
            if entry["state"] == "cancelled":
                return entry["result"]
            self.journal.put(key, "dispatched")
        try:
            if call["operation"] == "shell":
                handle = self.worker.start(call)
                with self._lock:
                    self.journal.put(key, "running", process=handle)
                    cancel_now = self._take_intent(key)
                self._event(call, "tool.running", "ok", handle, {"state": "running"})
                if cancel_now:
                    # The interrupt arrived while start() was crossing the boundary;
                    # stop the process group and collect its final status below.
                    try:
                        self.worker.cancel(key, how="terminate")
                    except Exception:
                        pass
                result = self._collect(call)
            else:
                result = self.worker.execute(call)
            validate_result_for_call(call, result)
        except Exception as exc:
            # Once dispatch could have crossed the boundary, transport errors
            # cannot establish whether a mutation happened.
            entry = self.journal.get(key)
            state = "unknown" if entry and entry["state"] in {"dispatched", "running"} else "failed"
            if state == "unknown":
                process = entry["process"]
                if process is None and call["operation"] == "shell":
                    # start() may have crossed the worker boundary before its
                    # acknowledgement was lost. Recover its stable-key handle.
                    try:
                        process = self.worker.process_handle(call)
                    except Exception:
                        process = None
                    if process is not None:
                        entry = self.journal.put(key, "running", process=process)
                        if self._take_intent(key):
                            return self.cancel(call)
                # A dropped acknowledgement or stream is not evidence that the
                # worker lost its execution record. Query by stable key even
                # when no process handle could be recovered.
                try:
                    return self.reconcile(call)
                except Exception:
                    pass
                result = self._unknown(call, "worker_outcome_unknown", str(exc))
                self.journal.put(key, state, process=process, result=result)
                self._event(call, "tool.unknown", "unknown", {"error": str(exc)}, result)
                return result
            result = self._unknown(call, "worker_dispatch_failed", str(exc), status="error")
            self.journal.put(key, "failed", result=result)
            return result

        state = {"ok": "completed", "cancelled": "cancelled", "unknown": "unknown"}.get(result["status"], "failed")
        with self._lock:
            # A concurrent cancel's reconciliation holds this lock across its read and write, so the
            # worker's terminal result is never overwritten by a stale "unknown".
            self.journal.put(key, state, result=result)
            self._event(call, "tool." + state, result["status"], result, result)
        return result

    def _collect(self, call: dict[str, Any]) -> dict[str, Any]:
        key = self._key(call)
        chunks: list[dict[str, Any]] = []
        for event in self.worker.stream(key):
            if event["type"] == "result":
                return event["result"]
            chunks.append(event)
            self._event(call, "tool." + event["type"], "ok", event,
                        {"data_base64": event.get("data_base64")})
        raise RuntimeError("worker stream ended without terminal result")

    def _unknown(self, call: dict[str, Any], code: str, message: str, *, status: str = "unknown") -> dict[str, Any]:
        return {"contract_version": "artifact-upload.v1", "run_id": call["run_id"], "session_id": call["session_id"],
                "call_id": call["call_id"], "argument_digest": call["argument_digest"], "status": status,
                "exit_code": None, "stdout": "", "stderr": message, "encoding": "utf-8", "error_code": code,
                "output_uri": None, "display_fields": {"aliases": {}, "changed_fields": []},
                "worker_digest": getattr(self.worker, "digest", "0" * 64)}

    def reconcile(self, call: dict[str, Any]) -> dict[str, Any]:
        """Query worker evidence after a broker/connection interruption; never redispatch."""
        key = self._validate_call(call)
        with self._lock:
            entry = self.journal.get(key)
            if entry is None:
                raise KeyError("execution is not journaled")
            if entry["result"] is not None and entry["state"] != "unknown":
                return entry["result"]
            try:
                status = self.worker.status(key)
                if status["state"] == "completed" and status.get("result"):
                    result = status["result"]
                else:
                    process = status.get("process", entry["process"])
                    return self.journal.put(key, "running", process=process)["result"] or self._unknown(call, "execution_running", "execution remains active")
            except Exception:
                if entry["state"] == "unknown" and entry["result"] is not None:
                    return entry["result"]
                result = self._unknown(call, "execution_unknown", "worker has no authoritative outcome for this handle")
                self.journal.put(key, "unknown", process=entry["process"], result=result)
                self._event(call, "tool.unknown", "unknown", {"state": "unavailable"}, result)
                return result
            validate_result_for_call(call, result)
            state = {"ok": "completed", "cancelled": "cancelled", "unknown": "unknown"}.get(result["status"], "failed")
            self.journal.put(key, state, process=entry["process"], result=result)
            self._event(call, "tool." + state, result["status"], result, result)
            return result

    def cancel(self, call: dict[str, Any], *, how: str = "terminate") -> dict[str, Any]:
        """Cancel the run-bound worker process group and persist its final result."""
        key = self._validate_call(call)
        with self._lock:
            entry = self.journal.get(key)
            if entry is None or entry["state"] == "accepted":
                # Dispatch is not committed yet: suppress it with a terminal result.
                result = self._unknown(call, "cancelled_before_dispatch", "cancelled before the worker started",
                                       status="cancelled")
                self.journal.put(key, "cancelled", result=result)
                self._event(call, "tool.cancelled", "cancelled", {"state": "before_dispatch"}, result)
                return result
            if entry["state"] == "dispatched" and entry["process"] is None and call["operation"] == "shell":
                try:
                    status = self.worker.status(key)
                except Exception:
                    status = None
                if not status or status.get("state") not in {"running", "completed"}:
                    # start() may still cross the worker boundary. Its dispatching
                    # thread cancels and collects once the start is acknowledged.
                    self._cancel_intent.add(self._intent_key(key))
                    result = self._unknown(call, "cancellation_pending", "cancellation is pending worker start acknowledgement")
                    self._event(call, "tool.cancel_requested", "unknown", {"state": "dispatched"}, result)
                    return result
            if entry is None or entry["state"] != "running":
                result = self.reconcile(call)
                # Reconciliation may recover a live execution after a lost
                # acknowledgement. Carry this cancellation through that state
                # change instead of requiring the caller to cancel again.
                entry = self.journal.get(key)
                if entry["state"] != "running":
                    return result
            if not self.worker.cancel(key, how=how):
                return self.reconcile(call)
        deadline = time.monotonic() + 5
        while True:
            result = self.reconcile(call)
            if result["status"] != "unknown" or time.monotonic() >= deadline:
                return result
            time.sleep(0.02)

    def invalidate_filesystem_facts(self, call: dict[str, Any]) -> bool:
        """Tell a consumer to discard cached filesystem facts after arbitrary shell."""
        self._validate_call(call)
        return call["operation"] == "shell"
