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
        self.aliases = aliases or {}
        self.metadata_policy = metadata_policy or {}
        self.audit = audit or (lambda event: None)
        self._lock = threading.RLock()
        self._reservations: dict[str, tuple[int, str]] = {}
        self._reserved_objects: set[str] = set()
        self._keys: dict[str, dict] = {}
        self._objects: dict[str, dict] = {}
        self._load()
        self._reconcile_staging()

    def _load(self):
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except FileNotFoundError:
            return
        self._objects = data.get("objects", {})
        self._keys = data.get("idempotency", {})
        for oid, meta in self._objects.items():
            p = self._object_file(oid)
            if not p.is_file() or p.stat().st_size != meta["size_bytes"]:
                raise RuntimeError(f"stored object missing or corrupt: {oid}")

    def _reconcile_staging(self):
        # An interrupted uncommitted upload has no accepted reservation after restart.
        for child in self.staging_dir.iterdir():
            if child.is_file():
                child.unlink()
        # A crash after object installation but before index replacement leaves an
        # uncommitted content file. It is not exposed and must not consume quota.
        committed = set(self._objects)
        for child in self.objects_dir.iterdir():
            if child.is_file() and child.name not in committed:
                child.unlink()

    def _object_file(self, oid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", oid):
            raise StoreError("invalid_request", "invalid object id", 400)
        return self.objects_dir / oid

    def _save_index(self):
        temp = self.root / (".index-" + uuid.uuid4().hex)
        payload = json.dumps({"objects": self._objects, "idempotency": self._keys},
                             sort_keys=True, separators=(",", ":")).encode()
        with open(temp, "wb") as f:
            f.write(payload); f.flush(); os.fsync(f.fileno())
        os.replace(temp, self.index_path)

    def _request_id(self):
        return uuid.uuid4().hex

    def _emit(self, action: str, request_id: str, **facts):
        self.audit({"event_id": uuid.uuid4().hex, "event_type": "artifact." + action,
                    "occurred_at": time.time(), "request_id": request_id, **facts})

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
            size = content if isinstance(content, int) else None
            if size is None:
                raise TypeError("upload_stream expects declared content size")
            if _reader is None:
                raise TypeError("upload_stream requires a content reader")
            content = _reader
            if size > self.max_upload_bytes:
                raise StoreError("upload_too_large", "upload exceeds staging bound", 413)
            if not re.fullmatch(r"[0-9a-f]{64}", content_sha256 or ""):
                raise StoreError("digest_mismatch", "Content-SHA256 does not match body", 400)
            identity_base = {"repository": repository, "project": project, "version": version, "object_path": object_path,
                        "sha256": content_sha256, "size_bytes": size, "owner_id": owner_id,
                        "retention": retention, "dependencies": list(dependencies or []),
                        "credential_id": credential_id}
            with self._lock:
                old = self._keys.get(idempotency_key)
                if old:
                    if old["identity"] != identity_base:
                        raise StoreError("idempotency_conflict", "key reused with different request", 409)
                    return old["status"], {"request_id": request_id, "error": None, "body": old["body"]}
                oid = hashlib.sha256((repository + "\0" + project + "\0" + version + "\0" + object_path).encode()).hexdigest()
                if oid in self._objects or oid in self._reserved_objects:
                    raise StoreError("object_exists", "object versions are immutable", 409)
                committed = sum(o["size_bytes"] for o in self._objects.values() if o["repository"] == repository)
                reserved = sum(size for size, repo in self._reservations.values() if repo == repository)
                if committed + reserved + size > self.quota_bytes:
                    raise StoreError("quota_exceeded", "repository object-byte quota exceeded", 413)
                reservation_id = uuid.uuid4().hex
                self._reservations[reservation_id] = (size, repository)
                self._reserved_objects.add(oid)
            stage = self.staging_dir / reservation_id
            try:
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
                    if oid in self._objects:
                        raise StoreError("object_exists", "object versions are immutable", 409)
                    # Reservation remains charged through durable object installation.
                    os.replace(stage, self._object_file(oid))
                    meta = {**identity, "object_id": oid, "owner_id": owner_id, "retention": retention,
                            "dependencies": list(dependencies or []), "created_at": time.time()}
                    self._objects[oid] = meta
                    body = {"object_id": oid, "metadata": self._metadata(meta)}
                    self._keys[idempotency_key] = {"identity": identity, "status": 201, "body": body}
                    try:
                        self._save_index()
                    except BaseException:
                        self._objects.pop(oid, None)
                        self._keys.pop(idempotency_key, None)
                        self._object_file(oid).unlink(missing_ok=True)
                        raise
                    self._emit("upload", request_id, object_id=oid, before_usage=committed,
                               after_usage=committed + size, sha256=digest, size_bytes=size)
                    return 201, {"request_id": request_id, "error": None, "body": body}
            finally:
                with self._lock:
                    self._reservations.pop(reservation_id, None)
                    self._reserved_objects.discard(oid)
                try: stage.unlink()
                except FileNotFoundError: pass
        except StoreError as e:
            self._emit("upload.rejected", request_id, status=e.status, code=e.code)
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
            obj = self._get_authorized(credential_id, oid, "download")
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
                before = sum(x["size_bytes"] for x in self._objects.values() if x["repository"] == obj["repository"])
                del self._objects[oid]
                after = before - obj["size_bytes"]
                body = {"object_id": oid, "before_usage_bytes": before, "after_usage_bytes": after}
                self._keys[idempotency_key] = {"identity": identity, "status": 200, "body": body}
                try:
                    self._save_index()
                except BaseException:
                    self._objects[oid] = obj
                    self._keys.pop(idempotency_key, None)
                    raise
                # The index replacement is the commit point. A crash before it
                # leaves the old index and intact bytes; a crash after it leaves
                # an unreferenced object which startup reconciliation removes.
                self._object_file(oid).unlink(missing_ok=True)
                self._emit("delete", request_id, object_id=oid, before_usage=before, after_usage=after,
                           sha256=obj["sha256"], size_bytes=obj["size_bytes"])
                return 200, {"request_id": request_id, "error": None, "body": body}
        except StoreError as e:
            self._emit("delete.rejected", request_id, object_id=oid, status=e.status, code=e.code)
            return e.status, {"request_id": request_id, "error": {"code": e.code, "message": str(e)}, "body": {}}
