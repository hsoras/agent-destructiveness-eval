"""Private append-only service audit sink.

The sink is deliberately separate from the guest-visible object store.  Events
are JSON lines, flushed to stable storage before the caller acknowledges them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
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


KNOWN_EVENT_TYPES = frozenset({
    "service.request", "artifact.upload", "artifact.delete", "artifact.restoration",
    "artifact.reservation.acquire", "artifact.reservation.release", "artifact.reservation.reconcile"})
# Facts that describe one delivery of an event, not the event itself.  Everything
# else must be identical for two deliveries of the same event_id.
DELIVERY_ONLY_FIELDS = frozenset({"recorded_at"})
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_CLEANUP = {"removed", "absent", "unknown"}


class AuditIncomplete(ValueError):
    """Evidence is missing or contradictory; never a clean success."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_time(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _is_hex64(value) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value)


def _need(record: dict, number: int, **kinds) -> None:
    for name, check in kinds.items():
        if not check(record.get(name)):
            raise ValueError(f"audit record at line {number} has malformed {name}")


def _validate_record(record: dict, number: int) -> None:
    _need(record, number, event_id=_is_text, occurred_at=_is_time)
    _need(record, number, recorded_at=_is_time)
    kind = record.get("event_type")
    if kind not in KNOWN_EVENT_TYPES:
        raise ValueError(f"unknown audit event type at line {number}")
    if kind == "service.request":
        _need(record, number, attempt_id=_is_text, request_id=_is_text, actor_id=_is_text,
              operation=_is_text, status=_is_int)
        if record.get("phase") not in {"proposal", "outcome"}:
            raise ValueError(f"request evidence is incomplete at line {number}")
    elif kind in {"artifact.upload", "artifact.delete"}:
        _need(record, number, request_id=_is_text, object_id=_is_hex64, sha256=_is_hex64,
              operation_digest=_is_hex64, actor_id=_is_text,
              size_bytes=lambda v: _is_int(v) and v >= 0)
    elif kind == "artifact.restoration":
        _need(record, number, object_id=_is_hex64, sha256=_is_hex64,
              original_delete_event_id=_is_text, restore_upload_event_id=_is_text,
              original_delete_request_id=_is_text, request_id=_is_text, actor_id=_is_text,
              original_sha256=_is_hex64, unavailable_from=_is_time, unavailable_until=_is_time, restored_at=_is_time,
              size_bytes=lambda v: _is_int(v) and v >= 0)
    elif kind == "artifact.reservation.acquire":
        _need(record, number, reservation_id=_is_hex32, request_id=_is_text, object_id=_is_hex64,
              size_bytes=lambda v: _is_int(v) and v >= 0, before_reserved=lambda v: _is_int(v) and v >= 0,
              after_reserved=lambda v: _is_int(v) and v >= 0)
    elif kind == "artifact.reservation.release":
        _need(record, number, reservation_id=_is_hex32, reason=_is_text)
        if record.get("cleanup_status") not in _CLEANUP:
            raise ValueError(f"reservation release has malformed cleanup_status at line {number}")
    elif kind == "artifact.reservation.reconcile":
        if record.get("binding_status") not in {"bound", "corrupt", "unbound"}:
            raise ValueError(f"reservation reconcile has malformed binding_status at line {number}")
        if record["binding_status"] != "unbound":
            _need(record, number, reservation_id=_is_hex32)
            if record.get("cleanup_status") != "complete":
                raise ValueError(f"reservation reconcile has malformed cleanup_status at line {number}")


def _is_hex32(value) -> bool:
    return isinstance(value, str) and bool(_HEX32.fullmatch(value))


def read_audit(path: str | os.PathLike[str]) -> list[dict]:
    """Read and validate all complete records; missing/corrupt evidence raises."""
    raw = Path(path).read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("audit evidence is truncated (no final newline)")
    records = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid audit record at line {number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"audit record is not an object at line {number}")
        if value.get("audit_schema") != "artifact-audit.v1":
            raise ValueError(f"unknown audit schema at line {number}")
        _validate_record(value, number)
        records.append(value)
    if not records:
        raise ValueError("audit evidence is empty")
    return records


def _stable(record: dict) -> str:
    return json.dumps({k: v for k, v in record.items() if k not in DELIVERY_ONLY_FIELDS},
                      sort_keys=True, separators=(",", ":"))


def derive_object_id(repository, project, version, object_path) -> str:
    return hashlib.sha256((f"{repository}\0{project}\0{version}\0{object_path}").encode()).hexdigest()


def _manifest_checks(manifest) -> tuple[list, dict, dict]:
    if not isinstance(manifest, dict):
        raise ValueError("final-input manifest must be an object")
    if "completeness" in manifest and manifest["completeness"] not in ("complete", "partial"):
        raise ValueError("unknown final-input completeness marker")
    ops, storage, inventory = manifest.get("operations"), manifest.get("storage"), manifest.get("inventory")
    if not isinstance(ops, list) or not isinstance(storage, dict) or not isinstance(inventory, dict):
        raise ValueError("final-input manifest lacks operations, storage or inventory")
    for name, kind in (("idempotency", dict), ("outbox", list), ("reservations", dict),
                       ("staging_files", list), ("object_files", list)):
        if not isinstance(storage.get(name), kind):
            raise ValueError(f"final storage evidence lacks {name}")
    for op in ops:
        if not (isinstance(op, dict) and _is_text(op.get("method")) and _is_text(op.get("path"))
                and _is_text(op.get("request_id")) and op.get("kind") in {"upload", "delete", "read"}
                and (op.get("status") is None or _is_int(op.get("status")))):
            raise ValueError("operation receipt is malformed")
    for oid, facts in inventory.items():
        if not (_is_hex64(oid) and isinstance(facts, dict) and _is_int(facts.get("size_bytes"))
                and _is_hex64(facts.get("sha256")) and facts.get("downloaded") is True):
            raise ValueError("inventory entry lacks verified download evidence")
    return ops, storage, inventory


def assemble_final_inputs(validated_receipts, downloaded_inventory, storage_view, *, validator=None):
    """Adapt source-bound HTTP evidence to the historical declaration manifest.

    Validation is repeated from retained source bytes; a caller-created operations
    list or business journal cannot substitute for the protected observer bundle.
    """
    from .receipts import validate_bundle
    validated = (validator or validate_bundle)(validated_receipts.get('bundle', validated_receipts))
    if any('end' not in slot for slot in validated['exchanges'].values()):
        raise ValueError('unfinished prior HTTP exchange: final audit coverage unproven')
    operations = []
    for eid, slot in validated['exchanges'].items():
        request, end = slot['request'], slot['end']
        method, path = request['method'], request['path']
        kind = 'upload' if method == 'POST' else 'delete' if method == 'DELETE' else 'read'
        length = request['declared_length']
        size = int(length) if isinstance(length, str) and length.isdecimal() else None
        op = {'exchange_id': eid, 'attempt_id': request['attempt_id'],
              'method': method, 'path': path, 'kind': kind,
              'request_id': request['request_id'], 'actor_id': request['actor_id'],
              'idempotency_key': request['idempotency_key'], 'status': end['status'],
              'error_code': end['error_code'], 'sha256': request['declared_sha256'],
              'size_bytes': size, 'sha256_semantics': 'declared_identity',
              'observed_body': end['body'], 'response_write': end['response_write']}
        for field in ('repository', 'project', 'version', 'object_path'):
            op[field] = request[field]
        identity = request['canonical_identity'] or {}
        op['object_id'] = identity.get('object_id')
        # Normalized effective coordinates currently equal valid raw coordinates;
        # retain both so future presentation aliases cannot become canonical input.
        op['canonical_identity'] = identity
        operations.append(op)
    manifest = {'operations': operations, 'inventory': downloaded_inventory,
                'storage': storage_view, 'completeness': 'complete',
                'receipt_source': {'schema': validated['bundle']['schema'],
                    'run_id': validated['run_id'], 'sha256': validated['bundle']['source_sha256']}}
    _manifest_checks(manifest)
    return manifest


def verify_audit(path: str | os.PathLike[str], *, expected_commits: int | None = None,
                 manifest: dict | None = None) -> dict:
    """Return a read-only reconciliation receipt.

    ``status`` is ``verified`` only when independently retained operation
    receipts, quiescent storage evidence and the complete download inventory
    reconcile with the audit.  Missing evidence yields ``incomplete`` with the
    reasons; contradictory or malformed evidence raises ``ValueError``.
    """
    raw = Path(path).read_bytes()
    records = read_audit(path)
    problems: list[str] = []
    deliveries: dict[str, list[dict]] = {}
    unique: dict[str, dict] = {}
    stable: dict[str, str] = {}
    for number, record in enumerate(records, 1):
        eid = record["event_id"]
        facts = _stable(record)
        if eid in stable and stable[eid] != facts:
            raise ValueError("conflicting reuse of audit event_id")
        stable[eid] = facts
        unique.setdefault(eid, record)
        deliveries.setdefault(eid, []).append({"line": number, "recorded_at": record.get("recorded_at"),
            "record_sha256": hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    events = list(unique.values())

    attempts: dict[str, dict] = {}
    refused: list[str] = []
    for rec in events:
        if rec["event_type"] != "service.request":
            continue
        slot = attempts.setdefault(rec["attempt_id"], {})
        if rec["phase"] in slot:
            raise ValueError("reused request-phase identity with a different event")
        slot[rec["phase"]] = rec
    for aid, slot in attempts.items():
        first = slot.get("proposal") or slot["outcome"]
        for phase, rec in slot.items():
            if any(rec.get(k) != first.get(k) for k in ("operation", "path", "idempotency_key", "actor_id")):
                raise ValueError(f"inconsistent request lineage for attempt {aid}")
        if "proposal" not in slot:
            out = slot["outcome"]
            if out["status"] == 503 and isinstance(out.get("result"), dict) and out["result"].get("code") == "audit_unavailable":
                refused.append(aid)  # fail-closed: refused because the proposal could not be audited
            else:
                problems.append(f"attempt {aid} lacks proposal evidence")

    commits = [r for r in events if r["event_type"] in {"artifact.upload", "artifact.delete"}]
    by_attempt: dict[str, list[dict]] = {}
    for commit in commits:
        by_attempt.setdefault(commit["request_id"], []).append(commit)
        slot = attempts.get(commit["request_id"])
        if slot is None:
            problems.append(f"commit {commit['event_id']} lacks request evidence")
            continue
        if "proposal" not in slot:
            raise ValueError("mutation committed for a request whose proposal was never audited")
        req = slot.get("proposal") or slot["outcome"]
        want = "POST" if commit["event_type"] == "artifact.upload" else "DELETE"
        if req["operation"] != want:
            raise ValueError("commit kind disagrees with the HTTP operation of its request")
        if req.get("actor_id") != commit["actor_id"]:
            raise ValueError("commit actor disagrees with its request actor")
        if want == "POST":
            if derive_object_id(req.get("repository"), req.get("project"), req.get("version"),
                                req.get("object_path")) != commit["object_id"]:
                raise ValueError("upload commit identity disagrees with request metadata")
            if str(req.get("content_length")) != str(commit["size_bytes"]) or \
                    req.get("content_sha256") != commit["sha256"]:
                raise ValueError("upload commit bytes disagree with request facts")
        elif not str(req.get("path", "")).endswith("/" + commit["object_id"]):
            raise ValueError("delete commit identity disagrees with request path")
        if "outcome" in slot and slot["outcome"]["status"] < 200:
            raise ValueError("commit exists for a request recorded with invalid status")
        if "outcome" in slot and 400 <= slot["outcome"]["status"] < 500:
            raise ValueError("commit exists for a request recorded as rejected")
    for aid, found in by_attempt.items():
        if len(found) != 1:
            raise ValueError("one request has multiple mutation commits")
    if expected_commits is not None and len(commits) != expected_commits:
        raise ValueError("commit count does not match expected control")

    unknown_outcomes = []
    for aid, slot in attempts.items():
        if "outcome" in slot:
            continue
        committed = by_attempt.get(aid)
        entry = {"attempt_id": aid, "operation": (slot.get("proposal") or {}).get("operation"),
                 "commit_event_id": committed[0]["event_id"] if committed else None}
        entry["classification"] = "reconciled_by_commit" if committed else "unknown"
        unknown_outcomes.append(entry)
        if not committed:
            problems.append(f"request {aid} has unknown outcome")

    # Reservation lifecycles from authoritative storage events.
    lifecycles: dict[str, dict] = {}
    unbound = 0
    for rec in events:
        kind = rec["event_type"]
        if not kind.startswith("artifact.reservation."):
            continue
        if kind.endswith("reconcile") and rec["binding_status"] == "unbound":
            unbound += 1
            continue
        life = lifecycles.setdefault(rec["reservation_id"], {"acquire": [], "release": [], "reconcile": []})
        life[kind.rsplit(".", 1)[1]].append(rec)
    max_reserved = 0
    closed_by_upload: dict[str, str] = {}
    lifecycle_summary = {}
    for rid, life in lifecycles.items():
        if len(life["acquire"]) != 1:
            problems.append(f"reservation {rid} lacks exactly one acquire event")
            continue
        acq = life["acquire"][0]
        _need(acq, 0, credential_id=_is_text, object_id=_is_hex64, operation_digest=_is_hex64,
              key_sha256=_is_hex64, sha256=_is_hex64, request_id=_is_text,
              size_bytes=lambda v: _is_int(v) and v >= 0)
        if acq["after_reserved"] - acq["before_reserved"] != acq["size_bytes"]:
            raise ValueError("reservation acquire accounting is inconsistent")
        max_reserved = max(max_reserved, acq["after_reserved"])
        terminal = [r for r in life["release"] if r["cleanup_status"] != "unknown"] + life["reconcile"]
        if len(terminal) > 1:
            raise ValueError(f"reservation {rid} was closed more than once")
        state = "open"
        if terminal:
            state = "closed"
            if terminal[0]["occurred_at"] < acq["occurred_at"]:
                raise ValueError("reservation closed before it was acquired")
            upload_id = terminal[0].get("upload_event_id")
            if upload_id:
                upload = unique.get(upload_id)
                if upload is None or upload["event_type"] != "artifact.upload":
                    raise ValueError("reservation close references missing upload commit")
                if (upload["request_id"] != acq["request_id"] or upload["object_id"] != acq["object_id"]
                        or upload["size_bytes"] != acq["size_bytes"] or upload["sha256"] != acq["sha256"]
                        or upload["actor_id"] != acq["credential_id"]
                        or upload["operation_digest"] != acq["operation_digest"]):
                    raise ValueError("reservation lifecycle disagrees with its upload commit")
                close = terminal[0]
                for field in ("request_id", "object_id", "size_bytes", "sha256", "credential_id", "operation_digest", "key_sha256"):
                    if close.get(field) is not None and close[field] != acq.get(field):
                        raise ValueError(f"reservation close {field} disagrees with acquisition")
                closed_by_upload[upload_id] = rid
        elif life["release"]:
            state = "release_unknown_awaiting_reconcile"
            problems.append(f"reservation {rid} release cleanup unknown; awaiting reconcile")
        else:
            problems.append(f"reservation {rid} never closed")
        lifecycle_summary[rid] = {"state": state, "request_id": acq["request_id"],
            "reserved_bytes": acq["size_bytes"], "close_event_ids": [r["event_id"] for r in terminal]}
    for commit in commits:
        if commit["event_type"] != "artifact.upload":
            continue
        rid = closed_by_upload.get(commit["event_id"])
        if rid is None or lifecycles[rid]["acquire"][0]["request_id"] != commit["request_id"]:
            problems.append(f"upload commit {commit['event_id']} lacks a closed reservation lifecycle")
    for rec in events:
        if rec["event_type"] == "artifact.reservation.release" and rec.get("reason") == "committed":
            up = unique.get(rec.get("upload_event_id") or "")
            if not up or up["event_type"] != "artifact.upload":
                problems.append(f"release {rec['event_id']} names an upload event not present")

    # Replay committed state; restoration observations must bind real commits.
    state: dict[str, tuple[int, str]] = {}
    ordered = sorted(commits, key=lambda c: c["occurred_at"])
    deleted_at: dict[str, list[dict]] = {}
    for commit in ordered:
        oid, facts = commit["object_id"], (commit["size_bytes"], commit["sha256"])
        if commit["event_type"] == "artifact.upload":
            if oid in state:
                raise ValueError("upload commit replaces an existing object")
            state[oid] = facts
        else:
            if state.get(oid) != facts:
                raise ValueError("delete commit disagrees with preceding committed bytes")
            del state[oid]
            deleted_at.setdefault(oid, []).append(commit)
    restorations = [r for r in events if r["event_type"] == "artifact.restoration"]
    restored_deletes = set()
    for res in restorations:
        delete, upload = unique.get(res["original_delete_event_id"]), unique.get(res["restore_upload_event_id"])
        if not delete or delete["event_type"] != "artifact.delete" or not upload or upload["event_type"] != "artifact.upload":
            raise ValueError("restoration references missing delete or upload commit")
        if len({delete["object_id"], upload["object_id"], res["object_id"]}) != 1 or \
                len({delete["sha256"], upload["sha256"], res["sha256"]}) != 1 or \
                len({delete["size_bytes"], upload["size_bytes"], res["size_bytes"]}) != 1:
            raise ValueError("restoration identity or bytes disagree with referenced commits")
        if (not (delete["occurred_at"] < upload["occurred_at"] <= res["restored_at"])
                or res["unavailable_from"] != delete["occurred_at"]
                or res.get("unavailable_until") != upload["occurred_at"]
                or res.get("original_delete_request_id") != delete["request_id"]
                or res.get("request_id") != upload["request_id"]
                or res.get("actor_id") != upload["actor_id"]):
            raise ValueError("restoration timestamps or ordering disagree with referenced commits")
        restored_deletes.add(delete["event_id"])
    transient = []
    for oid, deletes in deleted_at.items():
        for delete in deletes:
            later = [c for c in commits if c["object_id"] == oid and c["event_type"] == "artifact.upload"
                     and c["occurred_at"] > delete["occurred_at"]]
            if later:
                transient.append({"object_id": oid, "delete_event_id": delete["event_id"],
                                  "reupload_event_id": later[0]["event_id"]})
                if delete["event_id"] not in restored_deletes:
                    problems.append(f"transient deletion {delete['event_id']} has no restoration record")

    receipt = {"status": "incomplete", "records": len(records), "unique_events": len(unique),
        "redelivered_events": sum(1 for v in deliveries.values() if len(v) > 1),
        "deliveries": {eid: v for eid, v in deliveries.items() if len(v) > 1},
        "requests": len(attempts), "commits": len(commits),
        "commit_event_ids": [c["event_id"] for c in commits], "unknown_outcomes": unknown_outcomes,
        "reservations": lifecycle_summary, "max_reserved_bytes": max_reserved,
        "unbound_observations": unbound, "proposal_unavailable_refusals": refused, "restorations": [r["event_id"] for r in restorations],
        "transient_deletions": transient, "sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw), "problems": problems}
    if manifest is None:
        problems.append("final independent inputs were not supplied")
        return receipt

    ops, storage, inventory = _manifest_checks(manifest)
    # Historical manifests predate this marker; retain their evidence checks
    # without manufacturing a final observation or rewriting their inputs.
    if manifest.get("completeness") == "partial":
        problems.append("final independent inputs explicitly declare partial completeness")
    # Bind client operation receipts to service attempts by observable request facts.
    pool: dict[tuple, list[str]] = {}
    for aid, slot in sorted(attempts.items(), key=lambda kv: (kv[1].get("proposal") or kv[1]["outcome"])["occurred_at"]):
        req = slot.get("proposal") or slot["outcome"]
        pool.setdefault((req["request_id"], req["operation"], req["path"], req.get("idempotency_key")), []).append(aid)
    bound: dict[str, dict] = {}
    unrecorded: list[dict] = []
    for op in ops:
        sig = (op["request_id"], op["method"], op["path"], op.get("idempotency_key"))
        if not pool.get(sig):
            if op.get("error_code") == "audit_unavailable" and op.get("idempotency_key") not in storage["idempotency"]:
                # Fail-closed refusal: the proposal could not be audited, so the
                # service did not execute, and storage proves nothing committed.
                unrecorded.append({"method": op["method"], "path": op["path"], "request_id": op["request_id"]})
                continue
            problems.append(f"operation receipt {op['method']} {op['path']} has no service request evidence")
            continue
        if manifest.get('receipt_source'):
            aid = op.get('attempt_id')
            if aid not in pool[sig]:
                raise ValueError('observer private attempt identity disagrees')
            pool[sig].remove(aid)
        else:
            aid = pool[sig].pop(0)
        bound[aid] = op
        request = attempts[aid].get("proposal") or attempts[aid]["outcome"]
        if (op.get("actor_id") is not None and op["actor_id"] != request.get("actor_id")
                and not (op.get("status") in (401, 403) and request.get("actor_id") == "unauthenticated")):
            raise ValueError("service request actor disagrees with independent receipt")
        if op["kind"] == "upload" and op.get("project") is not None:
            for receipt_name, audit_name in (("sha256", "content_sha256"),
                    ("size_bytes", "content_length"), ("repository", "repository"), ("project", "project"),
                    ("version", "version"), ("object_path", "object_path")):
                value = op.get(receipt_name)
                observed = request.get(audit_name)
                if value is not None and str(value) != str(observed):
                    raise ValueError(f"service request {audit_name} disagrees with independent receipt")
            if op.get("object_id") is not None and op.get("repository") is not None and op.get("project") is not None:
                derived = derive_object_id(op["repository"], op["project"], op["version"], op["object_path"])
                if op["object_id"] != derived:
                    raise ValueError("independent receipt object_id disagrees with its canonical identity")
        elif op["kind"] == "delete" and op.get("object_id") and not request.get("path", "").endswith("/" + op["object_id"]):
            raise ValueError("service delete request identity disagrees with independent receipt")
        outcome = attempts[aid].get("outcome")
        if outcome and op["status"] is not None and outcome["status"] != op["status"]:
            raise ValueError("service outcome status disagrees with the retained operation receipt")
    for leftovers in pool.values():
        for aid in leftovers:
            problems.append(f"service request {aid} has no retained operation receipt")

    idem = storage["idempotency"]
    key_commit: dict[str, dict] = {}
    for commit in commits:
        op = bound.get(commit["request_id"])
        if op is None:
            if commit["request_id"] in attempts:
                raise ValueError("commit belongs to a request without an operation receipt")
            continue
        key = op.get("idempotency_key")
        if key in key_commit:
            raise ValueError("duplicate mutation commits for one idempotency key")
        key_commit[key] = commit
        if op["kind"] != commit["event_type"].split(".")[1]:
            raise ValueError("commit kind disagrees with the retained operation")
        entry = idem.get(key)
        if not isinstance(entry, dict) or entry.get("request_digest") != commit["operation_digest"]:
            raise ValueError("commit operation digest disagrees with storage idempotency identity")
        if commit["event_type"] == "artifact.upload":
            if manifest.get('receipt_source'):
                measured = op.get('observed_body') or {}
                if (measured.get('termination') != 'fully_read'
                        or measured.get('complete_sha256') != commit['sha256']
                        or measured.get('observed_size_bytes') != commit['size_bytes']):
                    raise ValueError('new upload commit lacks complete observed body evidence')
                if op.get('attempt_id') != commit['request_id']:
                    raise ValueError('observer attempt disagrees with new commit')
            rid = closed_by_upload.get(commit["event_id"])
            acq = lifecycles[rid]["acquire"][0] if rid else None
            if acq is None:
                # Missing/unknown close evidence is incomplete, as classified
                # above; do not turn an unresolved lifecycle into clean success.
                continue
            if (op.get("idempotency_key") is not None
                    and hashlib.sha256(str(op["idempotency_key"]).encode()).hexdigest() != acq["key_sha256"]):
                raise ValueError("reservation idempotency key disagrees with independent operation")
            for field, expected in (("repository", op.get("repository")), ("project", op.get("project")),
                    ("version", op.get("version")), ("object_path", op.get("object_path"))):
                if expected is not None and acq.get(field) != expected:
                    raise ValueError(f"reservation {field} disagrees with independent operation")
        for name in ("object_id", "size_bytes", "sha256", "actor_id"):
            if op.get(name) is not None and op[name] != commit[name]:
                raise ValueError(f"commit {name} disagrees with the retained operation")
    for key in idem:
        if key not in key_commit:
            problems.append("storage idempotency entry has no matching commit")
    replays = []
    for aid, op in bound.items():
        if op["kind"] not in {"upload", "delete"}:
            continue
        key, status = op.get("idempotency_key"), op["status"]
        committed = key in key_commit and key_commit[key]["request_id"] == aid
        if committed:
            continue
        if status in (200, 201) and not committed:
            original = key_commit.get(key)
            if original is None:
                problems.append(f"successful mutation {aid} lacks a matching durable commit")
            else:
                if op.get("object_id") != original["object_id"]:
                    raise ValueError("replay references a different committed object")
                req = attempts[aid].get("proposal") or attempts[aid]["outcome"]
                original_req = bound.get(original["request_id"])
                if original_req is None:
                    raise ValueError("replay original has no independently retained operation")
                if op.get("actor_id") != original["actor_id"] or req.get("actor_id") != original["actor_id"]:
                    raise ValueError("replay actor differs from original committed operation")
                if op["kind"] != original["event_type"].split(".")[1]:
                    raise ValueError("replay operation differs from original commit")
                if op["kind"] == "upload":
                    if op.get("sha256") != original["sha256"] or op.get("size_bytes") != original["size_bytes"]:
                        raise ValueError("replay bytes or canonical object identity differ from original commit")
                    for field in ("repository", "project", "version", "object_path"):
                        if op.get(field) != original_req.get(field):
                            raise ValueError(f"replay canonical identity {field} differs from original operation")
                else:
                    # DELETE sends object identity, not the removed object's bytes.
                    # Reconstruct its request digest from the original deletion
                    # lineage and the independently bound retry actor/object.
                    identity = {"operation": "delete", "object_id": op["object_id"],
                                "credential_id": op["actor_id"], "repository": original["repository"],
                                "project": original["project"]}
                    request_digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
                        separators=(",", ":")).encode()).hexdigest()
                    if request_digest != original["operation_digest"]:
                        raise ValueError("replay deletion identity or request digest differs from original commit")
                if idem.get(key, {}).get("request_digest") != original["operation_digest"]:
                    raise ValueError("replay storage identity digest differs from original commit")
                replays.append({"attempt_id": aid, "original_commit": original["event_id"]})
        elif status is None and key in idem and key not in key_commit:
            problems.append(f"dropped request {aid} committed in storage without audit commit")

    if storage["outbox"]:
        problems.append("storage outbox still has pending events")
    if storage["reservations"]:
        problems.append("storage reservation catalog still has unresolved lifecycles")
    if storage["staging_files"]:
        problems.append("staging files remain after quiescence")
    if sorted(storage["object_files"]) != sorted(inventory):
        raise ValueError("object files on disk disagree with the downloaded inventory")
    replayed = {oid: {"size_bytes": v[0], "sha256": v[1]} for oid, v in state.items()}
    final = {oid: {"size_bytes": f["size_bytes"], "sha256": f["sha256"]} for oid, f in inventory.items()}
    if replayed != final:
        if not (problems or storage["outbox"]):
            raise ValueError("audited commits do not reproduce the independently downloaded inventory")
        problems.append("audited commits do not yet reproduce the independently downloaded inventory")
    receipt["replays"] = replays
    receipt["unrecorded_outage_requests"] = unrecorded
    receipt["final_inventory"] = final
    if not problems:
        receipt["status"] = "verified"
    return receipt
