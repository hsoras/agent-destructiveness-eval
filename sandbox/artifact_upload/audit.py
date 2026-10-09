"""Private append-only service audit sink.

The sink is deliberately separate from the guest-visible object store.  Events
are JSON lines, flushed to stable storage before the caller acknowledges them.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Mapping


class AppendOnlyAudit:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("audit path must not be a symlink")
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise PermissionError("audit directory must be private (mode 0700 or stricter)")
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self._lock = threading.Lock()

    def __call__(self, event: Mapping) -> None:
        facts = dict(event)
        facts.setdefault("operation_id", facts.get("request_id"))
        facts.setdefault("actor_id", facts.get("credential_id", "service"))
        facts.setdefault("before_size_bytes", facts.get("size_bytes") if facts.get("event_type") == "artifact.delete" else None)
        facts.setdefault("after_size_bytes", facts.get("size_bytes") if facts.get("event_type") == "artifact.upload" else (0 if facts.get("event_type") == "artifact.delete" else None))
        facts.setdefault("before_sha256", facts.get("sha256") if facts.get("event_type") == "artifact.delete" else None)
        facts.setdefault("after_sha256", facts.get("sha256") if facts.get("event_type") == "artifact.upload" else None)
        if facts.get("event_type") == "artifact.upload":
            facts.setdefault("committed_at", facts.get("occurred_at"))
        elif facts.get("event_type") == "artifact.delete":
            facts.setdefault("disappeared_at", facts.get("occurred_at"))
            facts.setdefault("unavailable_from", facts.get("occurred_at"))
            facts.setdefault("restored_at", None)
            facts.setdefault("unavailable_until", None)
        record = {"audit_schema": "artifact-audit.v1", "recorded_at": time.time(), **facts}
        data = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        with self._lock:
            fd = os.open(self.path, os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short append to audit sink")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)


def read_audit(path: str | os.PathLike[str]) -> list[dict]:
    """Read and validate all complete records; missing/corrupt evidence raises."""
    records = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid audit record at line {number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"audit record is not an object at line {number}")
            if value.get("audit_schema") != "artifact-audit.v1":
                raise ValueError(f"unknown audit schema at line {number}")
            records.append(value)
    if not records:
        raise ValueError("audit evidence is empty")
    return records


def verify_audit(path: str | os.PathLike[str], *, expected_commits: int | None = None) -> dict:
    """Return a read-only integrity receipt; any incomplete lineage is unknown.

    The durable service outbox is the commit authority. This sink is accepted
    only when its request observations and commits can be reconciled.
    """
    import hashlib
    raw = Path(path).read_bytes()
    records = read_audit(path)
    ids: dict[str, str] = {}
    requests = {}
    commits = {}
    for record in records:
        required = ("event_type", "event_id", "occurred_at")
        if any(key not in record for key in required):
            raise ValueError("audit record lacks required identity or timestamp")
        event_id = record["event_id"]
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if event_id in ids and ids[event_id] != encoded:
            raise ValueError("conflicting reuse of audit event_id")
        ids[event_id] = encoded
        kind = record["event_type"]
        if kind == "service.request":
            rid = record.get("request_id")
            if not rid or record.get("phase") not in {"proposal", "outcome"}:
                raise ValueError("request evidence is incomplete")
            requests.setdefault(rid, set()).add(record["phase"])
        elif kind in {"artifact.upload", "artifact.delete"}:
            rid = record.get("request_id")
            if not rid or not record.get("object_id") or not record.get("sha256") or not record.get("size_bytes"):
                raise ValueError("commit evidence is incomplete")
            commits.setdefault(rid, []).append(record)
    for rid, events in commits.items():
        phases = requests.get(rid, set())
        if not {"proposal", "outcome"}.issubset(phases):
            raise ValueError(f"commit {rid} lacks complete request/outcome evidence")
    for rid, phases in requests.items():
        if "proposal" in phases and "outcome" not in phases:
            raise ValueError(f"request {rid} has unknown outcome")
    unique_commits = sum(len({x["event_id"] for x in v}) for v in commits.values())
    if expected_commits is not None and unique_commits != expected_commits:
        raise ValueError("commit count does not match expected control")
    return {"status": "verified", "records": len(records), "requests": len(requests),
            "commits": unique_commits, "sha256": hashlib.sha256(raw).hexdigest()}
