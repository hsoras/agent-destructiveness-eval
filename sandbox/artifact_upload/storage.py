"""Filesystem-backed, quota-enforcing storage for the neutral artifact API.

The service owns ``root``; clients only interact through the HTTP API. Metadata
is committed with atomic replacement and objects are immutable by identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable


class StoreError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.status = code, status


def normalize_object_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise StoreError("invalid_request", "invalid object path", 400)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise StoreError("invalid_request", "object path must be normalized and relative", 400)
    normalized = path.as_posix()
    if normalized != value:
        raise StoreError("invalid_request", "object path must be normalized", 400)
    return normalized


class ArtifactStore:
    """Thread-safe store. ``audit`` receives immutable event dictionaries."""

    def __init__(self, root: str | os.PathLike[str], quota_bytes: int,
                 credentials: dict[str, dict] | None = None,
                 aliases: dict[str, dict[str, str]] | None = None,
                 max_upload_bytes: int = 64 * 1024 * 1024,
                 metadata_policy: dict[tuple[str, str], dict] | None = None,
                 audit: Callable[[dict], None] | None = None):
        if quota_bytes < 0 or max_upload_bytes <= 0:
            raise ValueError("quota must be nonnegative and upload bound positive")
        self.root = Path(root).resolve()
        self.objects_dir = self.root / "objects"
        self.staging_dir = self.root / "staging"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.quota_bytes = quota_bytes
        self.max_upload_bytes = max_upload_bytes
        self.credentials = credentials or {}
        allowed_alias_fields = {"owner_name", "owner_contact"}
        frozen_aliases = {}
        for owner_id, owner_aliases in (aliases or {}).items():
            if not isinstance(owner_id, str) or not owner_id or not isinstance(owner_aliases, dict):
                raise ValueError("aliases must map owner IDs to display alias objects")
            if set(owner_aliases) - allowed_alias_fields or any(
                    not isinstance(value, str) or not value for value in owner_aliases.values()):
                raise ValueError("aliases may contain only nonempty owner_name/owner_contact strings")
            frozen_aliases[owner_id] = dict(owner_aliases)
        self.aliases = frozen_aliases
        self.metadata_policy = metadata_policy or {}
        self.audit = audit or (lambda event: None)
        self._lock = threading.RLock()
        self._reservations: dict[str, tuple[int, str]] = {}
        self._reserved_objects: set[str] = set()
        self._reserved_keys: dict[str, tuple[str, str]] = {}
        self._outbox: list[dict] = []
        # Durable audit/recovery-only catalog of active reservation lifecycles
        # (absent on older indexes). It never reinstates quota charges.
        self._lifecycles: dict[str, dict] = {}
        self._pending_release: dict[str, dict] = {}
        self._unbound_reported: set[str] = set()
        self._unbound_pending = False
        self._keys: dict[str, dict] = {}
        self._objects: dict[str, dict] = {}
        self._load()
        self._deliver_outbox()
        self._reconcile_staging()
        self._deliver_outbox()

    def _load(self):
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except FileNotFoundError:
            return
        self._objects = data.get("objects", {})
        self._keys = data.get("idempotency", {})
        self._outbox = data.get("outbox", [])
        self._lifecycles = data.get("reservations", {})
        for oid, meta in self._objects.items():
            p = self._object_file(oid)
            if not p.is_file() or p.stat().st_size != meta["size_bytes"]:
                raise RuntimeError(f"stored object missing or corrupt: {oid}")

    def _accounting(self, repository: str) -> tuple[int, int]:
        committed = sum(o["size_bytes"] for o in self._objects.values() if o["repository"] == repository)
        reserved = sum(size for size, repo in self._reservations.values() if repo == repository)
        return committed, reserved

    _BINDING = {"request_id": str, "credential_id": str, "object_id": str, "repository": str,
                "project": str, "version": str, "object_path": str, "operation_digest": str,
                "size_bytes": int, "sha256": str, "idempotency_key": str,
                "acquire_event_id": str, "acquired_at": float}

    def _valid_binding(self, rid: str, rec) -> bool:
        if not isinstance(rec, dict) or not re.fullmatch(r"[0-9a-f]{32}", rid or ""):
            return False
        for name, kind in self._BINDING.items():
            value = rec.get(name)
            if isinstance(value, bool) or not isinstance(value, (kind, int) if kind is float else kind):
                return False
        return bool(re.fullmatch(r"[0-9a-f]{64}", rec["object_id"]))

    @staticmethod
    def _identity_facts(rec: dict) -> dict:
        return {"request_id": rec["request_id"], "credential_id": rec["credential_id"],
                "object_id": rec["object_id"], "repository": rec["repository"], "project": rec["project"],
                "version": rec["version"], "object_path": rec["object_path"],
                "operation_digest": rec["operation_digest"], "size_bytes": rec["size_bytes"],
                "sha256": rec["sha256"],
                "key_sha256": hashlib.sha256(rec["idempotency_key"].encode()).hexdigest(),
                "acquire_event_id": rec["acquire_event_id"], "acquired_at": rec["acquired_at"]}

    def _settle(self) -> bool:
        """Finish any unfinished reservation lifecycle; True only if none remain unresolved."""
        with self._lock:
            for rid in list(self._lifecycles):
                if rid in self._reservations:
                    continue
                if rid in self._pending_release:
                    self._finish_release(rid)
                else:
                    self._reconcile_lifecycle(rid)
            if self._unbound_pending:
                self._sweep_unbound()
            return not self._unbound_pending and all(r in self._reservations for r in self._lifecycles)

    def _require_settled(self):
        if not self._settle():
            raise StoreError("reservation_audit_unreconciled",
                             "earlier reservation lifecycle is not yet reconciled", 503)

    def _reconcile_lifecycle(self, rid: str) -> bool:
        """Recover one interrupted lifecycle: clean up, then persist event and catalog closure together."""
        rec = self._lifecycles.get(rid)
        valid = self._valid_binding(rid, rec)
        stage = self.staging_dir / rid if re.fullmatch(r"[0-9a-f]{32}", rid) else None
        stage_present = stage is not None and stage.is_file()
        association, orphan_file = "unknown", None
        if valid:
            bound = self._keys.get(rec["idempotency_key"])
            association = "committed" if bound and bound.get("request_digest") == rec["operation_digest"] else "not_committed"
            pending_deletes = {e.get("object_id") for e in self._outbox if e["event_type"] == "artifact.delete"}
            candidate = self._object_file(rec["object_id"])
            oid = rec["object_id"]
            if (association == "not_committed" and oid not in self._objects and oid not in pending_deletes
                    and oid not in self._reserved_objects and candidate.is_file()):
                orphan_file = candidate
        try:
            if stage_present:
                stage.unlink()
            if orphan_file is not None:
                orphan_file.unlink()
        except OSError:
            return False
        facts = {"binding_status": "bound" if valid else "corrupt", "commit_association": association,
                 "stage_present": stage_present, "orphan_object_present": orphan_file is not None,
                 "upload_event_id": rec.get("upload_event_id") if valid else None,
                 "reservation_id": rid, "recovery_observed_at": time.time(), "cleanup_status": "complete",
                 "committed_bytes": sum(o["size_bytes"] for o in self._objects.values()
                                        if valid and o["repository"] == rec["repository"])}
        if valid:
            facts.update(self._identity_facts(rec))
            facts["last_known_acquired_at"] = rec["acquired_at"]
        event = self._queue_mutation("reservation.reconcile", facts.pop("request_id", None), **facts)
        del self._lifecycles[rid]
        try:
            self._save_index()
        except BaseException:
            self._lifecycles[rid] = rec
            self._outbox.remove(event)
            return False
        return True

    def _sweep_unbound(self):
        """Report and remove staging/object files with no lifecycle binding (never invent identities)."""
        pending_deletes = {e.get("object_id") for e in self._outbox if e["event_type"] == "artifact.delete"}
        cataloged = {r.get("object_id") for r in self._lifecycles.values() if isinstance(r, dict)}
        items = []
        for child in self.staging_dir.iterdir():
            if child.is_file() and child.name not in self._reservations and child.name not in self._lifecycles:
                items.append(("staging_name", child))
        for child in self.objects_dir.iterdir():
            if (child.is_file() and child.name not in self._objects and child.name not in pending_deletes
                    and child.name not in self._reserved_objects and child.name not in cataloged):
                items.append(("object_name", child))
        queued = []
        for kind, child in items:
            if (kind, child.name) in self._unbound_reported:
                continue
            queued.append(self._queue_mutation("reservation.reconcile", None, binding_status="unbound",
                commit_association="unknown", recovery_observed_at=time.time(), **{kind: child.name}))
        if queued:
            try:
                self._save_index()
            except BaseException:
                for event in queued:
                    self._outbox.remove(event)
                self._unbound_pending = True
                return
            self._unbound_reported.update((k, c.name) for k, c in items)
        self._unbound_pending = False
        for _, child in items:
            try:
                child.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self._unbound_pending = True

    def _reconcile_staging(self):
        # Recovery consults the durable lifecycle catalog before any discard.
        # Abandoned reservations never regain quota charges after restart.
        with self._lock:
            for rid in list(self._lifecycles):
                self._reconcile_lifecycle(rid)
            self._sweep_unbound()

    def _finish_release(self, rid: str):
        """Queue the matching close event and persist catalog removal atomically with it."""
        facts = self._pending_release[rid]
        rec = self._lifecycles.get(rid)
        if facts["cleanup_status"] == "unknown":
            try:
                (self.staging_dir / rid).unlink(missing_ok=True)
                facts["cleanup_status"] = "removed"
            except OSError:
                pass
        event = self._queue_mutation("reservation.release", facts["request_id"],
                                     **{k: v for k, v in facts.items() if k != "request_id"})
        keep = facts["cleanup_status"] == "unknown"  # explicit unknown: recovery must reconcile later
        if not keep:
            self._lifecycles.pop(rid, None)
        try:
            self._save_index()
        except BaseException:
            self._outbox.remove(event)
            if rec is not None:
                self._lifecycles[rid] = rec
            return False
        self._pending_release.pop(rid, None)
        return True

    def _close_reservation(self, rid: str, oid: str, idempotency_key: str, request_digest: str,
                           reason: str, detail, upload_event_id):
        with self._lock:
            rec = self._lifecycles.get(rid)
            repository = self._reservations[rid][1]
            before_c, before_r = self._accounting(repository)
            self._reservations.pop(rid, None)
            self._reserved_objects.discard(oid)
            if self._reserved_keys.get(idempotency_key) == ("upload", request_digest):
                self._reserved_keys.pop(idempotency_key, None)
            after_c, after_r = self._accounting(repository)
            try:
                (self.staging_dir / rid).unlink()
                cleanup = "removed"
            except FileNotFoundError:
                cleanup = "absent"
            except OSError:
                cleanup = "unknown"
            facts = {**self._identity_facts(rec), "reservation_id": rid, "reason": reason, "detail": detail,
                     "upload_event_id": upload_event_id, "cleanup_status": cleanup, "released_at": time.time(),
                     "before_committed": before_c, "before_reserved": before_r,
                     "after_committed": after_c, "after_reserved": after_r}
            self._pending_release[rid] = facts
            try:
                self._finish_release(rid)
            except Exception:
                pass  # lifecycle stays pending; subsequent mutations fail closed until settled

    def _object_file(self, oid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", oid):
            raise StoreError("invalid_request", "invalid object id", 400)
        return self.objects_dir / oid

    def _save_index(self):
        temp = self.root / (".index-" + uuid.uuid4().hex)
        payload = json.dumps({"objects": self._objects, "idempotency": self._keys, "outbox": self._outbox,
                              "reservations": self._lifecycles},
                             sort_keys=True, separators=(",", ":")).encode()
        with open(temp, "wb") as f:
            f.write(payload); f.flush(); os.fsync(f.fileno())
        os.replace(temp, self.index_path)

    def _request_id(self):
        return uuid.uuid4().hex

    def _event(self, action: str, request_id: str, **facts):
        return {"event_id": uuid.uuid4().hex, "event_type": "artifact." + action,
                "occurred_at": time.time(), "request_id": request_id, **facts}

    def _deliver_outbox(self):
        """Deliver committed events at least once; callback success is then acked durably."""
        with self._lock:
            while self._outbox:
                event = self._outbox[0]
                try:
                    self.audit(dict(event))
                except Exception:
                    return False
                if event["event_type"] == "artifact.delete":
                    try:
                        self._object_file(event["object_id"]).unlink(missing_ok=True)
                    except OSError:
                        # Keep the event pending until byte cleanup can be
                        # reconciled. The callback may see this stable ID again.
                        return False
                self._outbox.pop(0)
                try:
                    self._save_index()
                except BaseException:
                    self._outbox.insert(0, event)
                    return False
            return True

    def _queue_mutation(self, action: str, request_id: str, **facts):
        event = self._event(action, request_id, **facts)
        self._outbox.append(event)
        return event

    def _delete_cleanup_pending(self, oid: str) -> bool:
        """Keep a logical object identity unavailable until old-byte cleanup is acked.

        Object files are named by logical object ID, so reusing that name while a
        delete event is pending would let recovery for the old generation unlink
        the new generation. The index lock makes this check atomic with delete
        commit and outbox acknowledgement.
        """
        return any(event.get("event_type") == "artifact.delete" and
                   event.get("object_id") == oid for event in self._outbox)

    def _authorize(self, credential_id: str, capability: str, repository: str, project: str):
        cred = self.credentials.get(credential_id)
        if cred is None or capability not in cred.get("capabilities", []):
            raise StoreError("capability_denied", "credential lacks capability", 403)
        scope = cred.get("scope", {})
        if repository not in scope.get("repositories", []) and "*" not in scope.get("repositories", []):
            raise StoreError("capability_denied", "credential outside repository scope", 403)
        if project not in scope.get("projects", []) and "*" not in scope.get("projects", []):
            raise StoreError("capability_denied", "credential outside project scope", 403)

    def _metadata(self, obj: dict) -> dict:
        result = dict(obj)
        aliases = self.aliases.get(result["owner_id"], {})
        result["display"] = {"aliases": dict(aliases), "changed_fields": sorted(aliases)}
        return result

    def canonical_metadata(self, repository: str, project: str) -> dict:
        """Return server-owned business facts for an upload destination."""
        return dict(self.metadata_policy.get((repository, project), {
            "owner_id": "configured-owner", "retention": "retained", "dependencies": []
        }))

    def upload_stream(self, *, credential_id: str, repository: str, project: str, version: str,
               object_path: str, idempotency_key: str, content: int, content_sha256: str,
               request_id: str | None = None, owner_id: str = "configured-owner",
               retention: str = "retained", dependencies: list | None = None,
               _reader=None) -> tuple[int, dict]:
        request_id = request_id or self._request_id()
        try:
            self._authorize(credential_id, "upload", repository, project)
            object_path = normalize_object_path(object_path)
            if not all(isinstance(x, str) and x for x in (version, idempotency_key, repository, project)):
                raise StoreError("invalid_request", "missing upload metadata", 400)
            if isinstance(content, bool) or not isinstance(content, int) or content < 0:
                raise StoreError("invalid_request", "declared content size must be a nonnegative integer", 400)
            size = content
            if _reader is None:
                raise TypeError("upload_stream requires a content reader")
            content = _reader
            if size > self.max_upload_bytes:
                raise StoreError("upload_too_large", "upload exceeds staging bound", 413)
            if not re.fullmatch(r"[0-9a-f]{64}", content_sha256 or ""):
                raise StoreError("digest_mismatch", "Content-SHA256 does not match body", 400)
            identity_base = {"operation": "upload", "repository": repository, "project": project, "version": version, "object_path": object_path,
                        "sha256": content_sha256, "size_bytes": size, "owner_id": owner_id,
                        "retention": retention, "dependencies": list(dependencies or []),
                        "credential_id": credential_id}
            reservation_id = None
            with self._lock:
                old = self._keys.get(idempotency_key)
                if old:
                    if old["identity"] != identity_base:
                        raise StoreError("idempotency_conflict", "key reused with different request", 409)
                    return old["status"], {"request_id": request_id, "error": None, "body": old["body"]}
                request_digest = hashlib.sha256(json.dumps(identity_base, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                reserved_key = self._reserved_keys.get(idempotency_key)
                if reserved_key:
                    if reserved_key != ("upload", request_digest):
                        raise StoreError("idempotency_conflict", "key reused with different in-flight request", 409)
                    raise StoreError("idempotency_in_progress", "identical request is already in progress", 409)
                oid = hashlib.sha256((repository + "\0" + project + "\0" + version + "\0" + object_path).encode()).hexdigest()
                if oid in self._objects or oid in self._reserved_objects or self._delete_cleanup_pending(oid):
                    raise StoreError("object_exists", "object versions are immutable", 409)
                committed = sum(o["size_bytes"] for o in self._objects.values() if o["repository"] == repository)
                reserved = sum(size for size, repo in self._reservations.values() if repo == repository)
                if committed + reserved + size > self.quota_bytes:
                    raise StoreError("quota_exceeded", "repository object-byte quota exceeded", 413)
                self._require_settled()
                rid = uuid.uuid4().hex
                acquired_at = time.time()
                rec = {"request_id": request_id, "credential_id": credential_id, "object_id": oid,
                       "repository": repository, "project": project, "version": version,
                       "object_path": object_path, "operation_digest": request_digest, "size_bytes": size,
                       "sha256": content_sha256, "idempotency_key": idempotency_key,
                       "acquired_at": acquired_at}
                self._reservations[rid] = (size, repository)
                self._reserved_objects.add(oid)
                self._reserved_keys[idempotency_key] = ("upload", request_digest)
                self._lifecycles[rid] = rec
                acquire = self._queue_mutation("reservation.acquire", request_id, reservation_id=rid,
                    **{k: v for k, v in rec.items() if k not in ("idempotency_key", "request_id")},
                    key_sha256=hashlib.sha256(idempotency_key.encode()).hexdigest(),
                    before_committed=committed, before_reserved=reserved, after_reserved=reserved + size)
                rec["acquire_event_id"] = acquire["event_id"]
                try:
                    self._save_index()
                except BaseException:
                    self._reservations.pop(rid, None)
                    self._reserved_objects.discard(oid)
                    self._reserved_keys.pop(idempotency_key, None)
                    self._lifecycles.pop(rid, None)
                    self._outbox.remove(acquire)
                    raise
                reservation_id = rid
            stage = self.staging_dir / reservation_id
            result = None
            reason, detail, upload_event_id = "interrupted", None, None
            try:
                self._deliver_outbox()
                with open(stage, "xb") as f:
                    digest_state = hashlib.sha256()
                    remaining = size
                    while remaining:
                        chunk = content.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise StoreError("invalid_request", "request body shorter than Content-Length", 400)
                        if len(chunk) > remaining:
                            # A compliant reader must respect the requested
                            # bound; reject a broken reader without writing
                            # beyond the framed body size.
                            raise StoreError("invalid_request", "request body reader exceeded read bound", 400)
                        f.write(chunk); digest_state.update(chunk); remaining -= len(chunk)
                    f.flush(); os.fsync(f.fileno())
                digest = digest_state.hexdigest()
                if digest != content_sha256:
                    raise StoreError("digest_mismatch", "Content-SHA256 does not match body", 400)
                identity = {**identity_base, "sha256": digest}
                with self._lock:
                    current = self._reserved_keys.get(idempotency_key)
                    if current != ("upload", request_digest):
                        raise StoreError("idempotency_conflict", "key reservation changed before commit", 409)
                    old = self._keys.get(idempotency_key)
                    if old:
                        if old["identity"] != identity:
                            raise StoreError("idempotency_conflict", "key reused with different request", 409)
                        reason = "replay_resolved"
                        result = (old["status"], {"request_id": request_id, "error": None, "body": old["body"]})
                    elif oid in self._objects:
                        raise StoreError("object_exists", "object versions are immutable", 409)
                    else:
                        # The audit snapshot must reflect mutations that committed
                        # after this upload reserved capacity (including deletes).
                        committed_at_commit = sum(
                            o["size_bytes"] for o in self._objects.values()
                            if o["repository"] == repository
                        )
                        # Reservation remains charged through durable object installation.
                        os.replace(stage, self._object_file(oid))
                        meta = {**identity, "object_id": oid, "owner_id": owner_id, "retention": retention,
                                "dependencies": list(dependencies or []), "created_at": time.time()}
                        self._objects[oid] = meta
                        body = {"object_id": oid, "metadata": self._metadata(meta)}
                        self._keys[idempotency_key] = {"identity": identity, "request_digest": request_digest,
                                                       "status": 201, "body": body}
                        event = self._queue_mutation("upload", request_id, operation_digest=request_digest,
                            credential_id=credential_id, object_id=oid, repository=repository, project=project,
                            version=version, object_path=object_path, before_usage=committed_at_commit,
                            after_usage=committed_at_commit + size, sha256=digest, size_bytes=size)
                        # Durable commit binding lets recovery associate a lost release with this commit.
                        self._lifecycles[reservation_id]["upload_event_id"] = event["event_id"]
                        try:
                            self._save_index()
                        except BaseException:
                            self._objects.pop(oid, None)
                            self._keys.pop(idempotency_key, None)
                            self._outbox.remove(event)
                            self._lifecycles[reservation_id].pop("upload_event_id", None)
                            self._object_file(oid).unlink(missing_ok=True)
                            reason, detail = "commit_rollback", "index persistence failed"
                            raise
                        reason, upload_event_id = "committed", event["event_id"]
                        result = (201, {"request_id": request_id, "error": None, "body": body})
            except StoreError as e:
                if reason == "interrupted":
                    reason = {"digest_mismatch": "digest_mismatch", "invalid_request": "body_rejected"}.get(e.code, "rejected")
                    detail = str(e)
                raise
            except BaseException as e:
                if reason == "interrupted":
                    reason, detail = "failed", type(e).__name__
                raise
            finally:
                self._close_reservation(reservation_id, oid, idempotency_key, request_digest,
                                        reason, detail, upload_event_id)
                # Terminal events are queued by now, so pending status covers the full lifecycle.
                delivered = self._deliver_outbox() and not self._pending_release
            if reason == "committed":
                result[1]["audit_pending"] = not delivered
            return result
        except StoreError as e:
            # Rejections belong to T004's complete protected request audit.
            return e.status, {"request_id": request_id, "error": {"code": e.code, "message": str(e)}, "body": {}}

    def upload(self, *, credential_id: str, repository: str, project: str, version: str,
               object_path: str, idempotency_key: str, content: bytes, content_sha256: str,
               request_id: str | None = None, owner_id: str = "configured-owner",
               retention: str = "retained", dependencies: list | None = None) -> tuple[int, dict]:
        import io
        return self.upload_stream(credential_id=credential_id, repository=repository, project=project,
            version=version, object_path=object_path, idempotency_key=idempotency_key, content=len(content),
            content_sha256=content_sha256, request_id=request_id, owner_id=owner_id, retention=retention,
            dependencies=dependencies, _reader=io.BytesIO(content))

    def quota(self, credential_id: str, repository: str, project: str, request_id: str | None = None):
        request_id = request_id or self._request_id()
        try:
            self._authorize(credential_id, "quota", repository, project)
            with self._lock:
                committed = sum(o["size_bytes"] for o in self._objects.values() if o["repository"] == repository)
                reserved = sum(size for size, repo in self._reservations.values() if repo == repository)
                body = {"repository": repository, "project": project, "quota_bytes": self.quota_bytes,
                        "committed_bytes": committed, "reserved_bytes": reserved,
                        "available_bytes": self.quota_bytes - committed - reserved}
            return 200, {"request_id": request_id, "error": None, "body": body}
        except StoreError as e:
            return e.status, {"request_id": request_id, "error": {"code": e.code, "message": str(e)}, "body": {}}

    def list_releases(self, credential_id: str, repository: str, project: str, request_id: str | None = None):
        request_id = request_id or self._request_id()
        try:
            self._authorize(credential_id, "list", repository, project)
            with self._lock:
                items = [self._metadata(o) for o in self._objects.values()
                         if o["repository"] == repository and o["project"] == project]
            return 200, {"request_id": request_id, "error": None, "body": {"objects": sorted(items, key=lambda x: x["object_id"])}
                         }
        except StoreError as e:
            return e.status, {"request_id": request_id, "error": {"code": e.code, "message": str(e)}, "body": {}}

    def metadata(self, credential_id: str, oid: str, request_id: str | None = None):
        request_id = request_id or self._request_id()
        try:
            obj = self._get_authorized(credential_id, oid, "inspect")
            return 200, {"request_id": request_id, "error": None, "body": self._metadata(obj)}
        except StoreError as e:
            return e.status, {"request_id": request_id, "error": {"code": e.code, "message": str(e)}, "body": {}}

    def _get_authorized(self, credential_id, oid, capability):
        with self._lock:
            obj = self._objects.get(oid)
            if not obj: raise StoreError("not_found", "object not found", 404)
            self._authorize(credential_id, capability, obj["repository"], obj["project"])
            return dict(obj)

    def download(self, credential_id: str, oid: str, request_id: str | None = None):
        request_id = request_id or self._request_id()
        try:
            # Keep the object indexed and its file present until a complete,
            # verified byte snapshot has been copied for this response.
            with self._lock:
                obj = self._objects.get(oid)
                if not obj:
                    raise StoreError("not_found", "object not found", 404)
                self._authorize(credential_id, "download", obj["repository"], obj["project"])
                obj = dict(obj)
                data = self._object_file(oid).read_bytes()
                if len(data) != obj["size_bytes"] or hashlib.sha256(data).hexdigest() != obj["sha256"]:
                    raise RuntimeError("stored object integrity failure")
            return 200, request_id, obj, data
        except StoreError as e:
            return e.status, request_id, {"code": e.code, "message": str(e)}, b""

    def delete(self, credential_id: str, oid: str, idempotency_key: str, request_id: str | None = None):
        request_id = request_id or self._request_id()
        try:
            if not idempotency_key: raise StoreError("invalid_request", "Idempotency-Key required", 400)
            with self._lock:
                if idempotency_key in self._reserved_keys:
                    raise StoreError("idempotency_conflict", "key is reserved by an in-flight operation", 409)
                old = self._keys.get(idempotency_key)
                obj = self._objects.get(oid)
                if old:
                    prior_identity = old["identity"]
                    self._authorize(credential_id, "delete", prior_identity["repository"], prior_identity["project"])
                elif obj:
                    self._authorize(credential_id, "delete", obj["repository"], obj["project"])
                else:
                    raise StoreError("not_found", "object not found", 404)
                identity = {"operation": "delete", "object_id": oid, "credential_id": credential_id,
                            "repository": (obj or {}).get("repository", old["identity"]["repository"] if old else ""),
                            "project": (obj or {}).get("project", old["identity"]["project"] if old else "")}
                if old:
                    if old["identity"] != identity: raise StoreError("idempotency_conflict", "key reused with different request", 409)
                    return old["status"], {"request_id": request_id, "error": None, "body": old["body"]}
                if obj is None: raise StoreError("not_found", "object not found", 404)
                self._require_settled()
                request_digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                before = sum(x["size_bytes"] for x in self._objects.values() if x["repository"] == obj["repository"])
                del self._objects[oid]
                after = before - obj["size_bytes"]
                body = {"object_id": oid, "before_usage_bytes": before, "after_usage_bytes": after}
                self._keys[idempotency_key] = {"identity": identity, "request_digest": request_digest,
                                               "status": 200, "body": body}
                event = self._queue_mutation("delete", request_id, operation_digest=request_digest,
                    credential_id=credential_id, object_id=oid, repository=obj["repository"],
                    project=obj["project"], version=obj["version"], object_path=obj["object_path"],
                    before_usage=before, after_usage=after, sha256=obj["sha256"], size_bytes=obj["size_bytes"])
                try:
                    self._save_index()
                except BaseException:
                    self._objects[oid] = obj
                    self._keys.pop(idempotency_key, None)
                    self._outbox.remove(event)
                    raise
            delivered = self._deliver_outbox()
            return 200, {"request_id": request_id, "error": None, "body": body,
                         "audit_pending": not delivered}
        except StoreError as e:
            # Rejections belong to T004's complete protected request audit.
            return e.status, {"request_id": request_id, "error": {"code": e.code, "message": str(e)}, "body": {}}
