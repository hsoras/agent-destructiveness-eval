"""Versioned HTTP adapter for :mod:`sandbox.artifact_upload.storage`.

This is a neutral artifact-store protocol, not an Artifactory implementation.
"""
from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .audit import AppendOnlyAudit
from .storage import ArtifactStore, StoreError


class ArtifactHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: ArtifactStore):
        self.store = store
        super().__init__(address, ArtifactRequestHandler)


class ArtifactRequestHandler(BaseHTTPRequestHandler):
    server: ArtifactHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Application audit is emitted by the store; avoid a second mutable log.
        pass

    def _request_id(self):
        if not hasattr(self, "_stable_request_id"):
            self._stable_request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        return self._stable_request_id

    def _audit_request(self, status, response=None, *, object_id=None, digest=None, size=None):
        """Record every API request, including rejected mutation attempts.

        This record complements (and never replaces) the store's durable
        mutation journal. A sink failure is surfaced as an unknown service
        outcome so callers reconcile against the commit journal and bytes.
        """
        # The HTTP request has its own stable identity even when the service
        # returns an earlier receipt for an idempotent replay.
        rid = self._request_id()
        snapshot = getattr(self, "_audit_preimage", None)
        before = snapshot if getattr(self, "_audit_preimage_captured", False) else (self.server.store._objects.get(object_id) if object_id else None)
        after = self.server.store._objects.get(object_id) if object_id else None
        request_facts = {"operation": self.command, "target": self.path,
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "content_sha256": self.headers.get("Content-SHA256"),
            "content_length": self.headers.get("Content-Length"),
            "repository": self.headers.get("X-Repository"),
            "project": self.headers.get("X-Project"), "version": self.headers.get("X-Version"),
            "object_path": self.headers.get("X-Object-Path")}
        request_digest = hashlib.sha256(json.dumps(request_facts, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        event = {
            "event_id": uuid.uuid4().hex, "event_type": "service.request",
            "occurred_at": time.time(), "request_id": rid,
            "operation_id": rid, "phase": getattr(self, "_audit_phase", "outcome"),
            "actor_id": self.server.store.credentials.get(
                self.headers.get("Authorization", "")[7:], {}).get("credential_id", "anonymous"),
            "operation": self.command, "path": self.path,
            "status": status, "result": (response or {}).get("error"),
            "response_request_id": (response or {}).get("request_id"),
            "object_id": object_id, "before_size_bytes": (before or {}).get("size_bytes"),
            "before_sha256": (before or {}).get("sha256"),
            "after_size_bytes": (after or {}).get("size_bytes"),
            "after_sha256": (after or {}).get("sha256"),
            "request_sha256": digest or request_digest, "request_size_bytes": size,
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "content_sha256": self.headers.get("Content-SHA256"),
            "reservation_bytes": sum(value[0] for value in self.server.store._reservations.values()),
        }
        if self.command == "POST":
            try:
                event["request_size_bytes"] = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                event["request_size_bytes"] = None
            event["content_sha256"] = self.headers.get("Content-SHA256")
        try:
            self.server.store.audit(event)
        except Exception as exc:
            raise StoreError("audit_unavailable", f"audit sink unavailable: {type(exc).__name__}", 503) from exc

    def _proposal(self, object_id=None):
        self._audit_phase = "proposal"
        self._audit_preimage = self.server.store._objects.get(object_id) if object_id else None
        self._audit_preimage_captured = True
        self._audit_request(0, {"request_id": self._request_id(), "error": None}, object_id=object_id)
        self._audit_phase = "outcome"

    def _credential(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:]:
            raise StoreError("capability_denied", "bearer credential required", 403)
        # This neutral MVP accepts configured credential IDs as bearer values.
        # It has no opaque token store, and never treats an arbitrary header as
        # a persistent credential identity.
        credential_id = auth[7:]
        if credential_id not in self.server.store.credentials:
            raise StoreError("capability_denied", "unknown bearer credential", 403)
        return credential_id

    def _json(self, status, value):
        try:
            oid = value.get("body", {}).get("object_id")
            if not oid:
                match = re.fullmatch(r"/v1/objects/([0-9a-f]{64})(?:/metadata)?", urlsplit(self.path).path)
                oid = match.group(1) if match else None
            self._audit_request(status, value, object_id=oid)
        except StoreError as e:
            status = e.status
            value = {"request_id": self._request_id(), "error": {"code": e.code, "message": str(e)}, "body": {}}
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("X-Request-ID", value.get("request_id", self._request_id()))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, error: StoreError, request_id=None):
        self._json(error.status, {"request_id": request_id or self._request_id(),
                                  "error": {"code": error.code, "message": str(error)}, "body": {}})

    def do_POST(self):
        # Some POST outcomes (for example an idempotent replay or quota
        # rejection) are decided before the body is consumed. Closing this
        # HTTP/1.1 connection keeps any unread framed body from being parsed as
        # the next request. Uploads still read exactly Content-Length below.
        self.close_connection = True
        if self.path != "/v1/objects":
            return self._error(StoreError("not_found", "route not found", 404))
        rid = self._request_id()
        try:
            self._proposal()
        except StoreError as e:
            return self._error(e, rid)
        try:
            credential = self._credential()
            length = self.headers.get("Content-Length")
            if length is None or not length.isdecimal():
                raise StoreError("invalid_request", "valid Content-Length required", 400)
            size = int(length)
            if size > self.server.store.max_upload_bytes:
                # Do not read/stage an unbounded request.
                raise StoreError("upload_too_large", "upload exceeds staging bound", 413)
            required = ("X-Repository", "X-Project", "X-Version", "X-Object-Path", "Idempotency-Key", "Content-SHA256")
            if any(not self.headers.get(h) for h in required):
                raise StoreError("invalid_request", "required upload metadata header missing", 400)
            if self.headers.get("Transfer-Encoding"):
                raise StoreError("invalid_request", "chunked transfer encoding unsupported", 400)
            # These are protected business facts. The store's configured defaults
            # are authoritative; client aliases cannot select canonical values.
            policy = self.server.store.canonical_metadata(self.headers["X-Repository"], self.headers["X-Project"])
            owner = self.headers.get("X-Owner-ID")
            retention = self.headers.get("X-Retention")
            if owner not in (None, policy["owner_id"]) or retention not in (None, policy["retention"]):
                raise StoreError("invalid_request", "protected metadata is server configured", 400)
            status, response = self.server.store.upload_stream(
                credential_id=credential, repository=self.headers["X-Repository"],
                project=self.headers["X-Project"], version=self.headers["X-Version"],
                object_path=self.headers["X-Object-Path"], idempotency_key=self.headers["Idempotency-Key"],
                content=size, content_sha256=self.headers["Content-SHA256"], request_id=rid,
                owner_id=policy["owner_id"], retention=policy["retention"],
                dependencies=policy.get("dependencies", []), _reader=self.rfile)
            self._json(status, response)
        except StoreError as e:
            self._error(e, rid)

    def do_GET(self):
        rid = self._request_id()
        match = re.fullmatch(r"/v1/objects/([0-9a-f]{64})(?:/metadata)?", urlsplit(self.path).path)
        try:
            self._proposal(match.group(1) if match else None)
        except StoreError as e:
            return self._error(e, rid)
        try:
            credential = self._credential()
            parts = urlsplit(self.path)
            match = re.fullmatch(r"/v1/objects/([0-9a-f]{64})(/metadata)?", parts.path)
            if match:
                oid = match.group(1)
                if match.group(2):
                    status, body = self.server.store.metadata(credential, oid, rid)
                    return self._json(status, body)
                status, request_id, result, content = self.server.store.download(credential, oid, rid)
                if status != 200:
                    return self._json(status, {"request_id": request_id, "error": result, "body": {}})
                try:
                    self._audit_request(status, {"error": None}, object_id=oid,
                                        digest=result["sha256"], size=len(content))
                except StoreError as e:
                    return self._json(e.status, {"request_id": request_id,
                        "error": {"code": e.code, "message": str(e)}, "body": {}})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("X-Request-ID", request_id)
                self.send_header("Content-SHA256", result["sha256"])
                self.end_headers(); self.wfile.write(content)
                return
            match = re.fullmatch(r"/v1/projects/([^/]+)/releases", parts.path)
            if match:
                query = parse_qs(parts.query)
                repository = query.get("repository", [None])[0]
                if not repository: raise StoreError("invalid_request", "repository query required", 400)
                status, body = self.server.store.list_releases(credential, repository, unquote(match.group(1)), rid)
                return self._json(status, body)
            if parts.path == "/v1/quota":
                query = parse_qs(parts.query)
                repository, project = query.get("repository", [None])[0], query.get("project", [None])[0]
                if not repository or not project: raise StoreError("invalid_request", "repository and project queries required", 400)
                status, body = self.server.store.quota(credential, repository, project, rid)
                return self._json(status, body)
            raise StoreError("not_found", "route not found", 404)
        except StoreError as e:
            self._error(e, rid)

    def do_DELETE(self):
        rid = self._request_id()
        match = re.fullmatch(r"/v1/objects/([0-9a-f]{64})", urlsplit(self.path).path)
        if not match:
            return self._error(StoreError("not_found", "route not found", 404), rid)
        try:
            self._proposal(match.group(1))
            credential = self._credential()
            status, body = self.server.store.delete(credential, match.group(1),
                                                     self.headers.get("Idempotency-Key", ""), rid)
            self._json(status, body)
        except StoreError as e:
            self._error(e, rid)


def create_server(root, quota_bytes, *, host="127.0.0.1", port=0, credentials=None,
                  aliases=None, max_upload_bytes=64 * 1024 * 1024, metadata_policy=None, audit=None):
    """Create (but do not start) an HTTP server and its independent store."""
    from pathlib import Path
    root_path = Path(root)
    sink = audit or AppendOnlyAudit(root_path.parent / (root_path.name + ".audit.jsonl"))
    configured_credentials = credentials or {}

    def audit(event):
        facts = dict(event)
        credential_id = facts.get("credential_id")
        if credential_id:
            facts["actor_id"] = configured_credentials.get(credential_id, {}).get("credential_id", credential_id)
        sink(facts)

    store = ArtifactStore(root_path, quota_bytes, credentials=credentials, aliases=aliases,
                          max_upload_bytes=max_upload_bytes, metadata_policy=metadata_policy, audit=audit)
    return ArtifactHTTPServer((host, port), store)
