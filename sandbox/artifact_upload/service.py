"""Versioned HTTP adapter for :mod:`sandbox.artifact_upload.storage`.

This is a neutral artifact-store protocol, not an Artifactory implementation.
"""
from __future__ import annotations

import json
import hashlib
import re
import time
import uuid
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .audit import AppendOnlyAudit
from .storage import ArtifactStore, StoreError, normalize_object_path
from .receipts import ServiceReceipts, ReceiptFailure, ReadObserver, WriteObserver


class ArtifactHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: ArtifactStore, receipt_sink):
        self.store = store
        self.receipts = receipt_sink
        self.mutations_stopped = False
        self._handlers = 0
        self._handler_condition = threading.Condition()
        self.control_path = receipt_sink.path.with_suffix('.control.json')
        self._sealed_control = False
        super().__init__(address, ArtifactRequestHandler)

    def process_request(self, request, client_address):
        with self._handler_condition:
            self._handlers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._handler_condition:
                self._handlers -= 1
                self._handler_condition.notify_all()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_condition:
                self._handlers -= 1
                self._handler_condition.notify_all()

    def quiescent_seal(self, timeout=5):
        started = time.time()
        self.mutations_stopped = True
        self.shutdown()
        self.server_close()
        deadline = time.monotonic() + timeout
        with self._handler_condition:
            while self._handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.receipts.failure = 'handler quiescence timeout'
                    self.receipts._status()
                    raise ReceiptFailure('handler quiescence timeout')
                self._handler_condition.wait(remaining)
        self.receipts.seal(dispatch_stopped_at=started, quiescent_at=time.time())

    def serve_forever(self, poll_interval=0.5):
        # Protected file control is host-only; it adds no public HTTP endpoint.
        self._control_done = threading.Event()
        def control():
            while not self._control_done.wait(.05):
                if not self.control_path.exists():
                    continue
                try:
                    command = json.loads(self.control_path.read_bytes())
                    self.control_path.unlink()
                    self.mutations_stopped = True
                    if command['action'] == 'freeze':
                        self.receipts.mutations_stopped = True
                        self.receipts.emit('boundary', boundary='mutations_stopped')
                    elif command['action'] == 'seal':
                        self._sealed_control = True
                        self.quiescent_seal(command['timeout'])
                    else:
                        raise ReceiptFailure('unknown control command')
                except Exception:
                    self.receipts.failure = 'service control/quiescence failed'
                    self.receipts._status()
        worker = threading.Thread(target=control, daemon=True)
        worker.start()
        try:
            super().serve_forever(poll_interval)
            if self._sealed_control:
                # Keep the exact container alive for host-only evidence export.
                threading.Event().wait()
        finally:
            self._control_done.set()
            worker.join(timeout=1)


class ArtifactRequestHandler(BaseHTTPRequestHandler):
    server: ArtifactHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Application audit is emitted by the store; avoid a second mutable log.
        pass

    def handle_one_request(self):
        # Same dispatch order as BaseHTTPRequestHandler, with observation before
        # parsing (including oversized lines) and after every response path.
        exchange = None
        original_writer = self.wfile
        self._receipt_status = None
        self._receipt_error = None
        self._receipt_headers = {}
        self._parse_ok = False
        self.headers = None
        self.command = self.path = None
        self._attempt_id = uuid.uuid4().hex
        self._stable_request_id = None
        self._audit_phase = "proposal"
        self._pre_observation = None
        self._proposal_done = self._outcome_done = False
        self._body_observer = None
        self._completed_empty = False
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                self.close_connection = True
                return
            exchange = self.server.receipts.begin(self.raw_requestline, self._attempt_id)
            self._exchange_id = exchange
            self.wfile = WriteObserver(original_writer, self.server.receipts, exchange)
            if len(self.raw_requestline) > 65536:
                self.requestline = self.request_version = self.command = ''
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            self._parse_ok = self.parse_request()
            self.server.receipts.emit('request', exchange_id=exchange, **self._receipt_request())
            if not self._parse_ok:
                return
            if not self.server.receipts.health()['healthy']:
                raise ReceiptFailure('observer unavailable before dispatch')
            if self.server.mutations_stopped and self.command in ('POST', 'DELETE'):
                self.close_connection = True
                self._proposal()
                self._error(StoreError('service_quiescing', 'service mutations stopped', 503))
                return
            method = getattr(self, 'do_' + self.command, None)
            if method is None:
                self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method (%r)" % self.command)
                return
            method()
            self.wfile.flush()
        except ReceiptFailure:
            self.close_connection = True
        except (TimeoutError, ConnectionError, OSError):
            self.close_connection = True
        finally:
            if exchange is not None:
                try:
                    # Parse failures before the normal request event still get
                    # terminal facts; consumers conservatively mark incomplete.
                    body = self._body_observer or ReadObserver(self.rfile, self.server.receipts, exchange)
                    length = self._header('Content-Length')
                    length = int(length) if length and length.isdecimal() else None
                    writer = self.wfile
                    self.server.receipts.end(exchange, parse_ok=self._parse_ok,
                        status=self._receipt_status, error_code=self._receipt_error,
                        response_headers=self._receipt_headers,
                        body=body.facts(length, self._completed_empty),
                        response_write={'successful_size_bytes': writer.count,
                            'successful_sha256': writer.digest.hexdigest(), 'failed': writer.failed,
                            'client_receipt': 'unknown', 'model_exposure': 'unknown'})
                except ReceiptFailure:
                    self.close_connection = True
            self.wfile = original_writer

    def _receipt_request(self):
        facts = {'method': self.command, 'path': self.path,
                 'request_id': self._request_id(), 'public_request_id_declaration': self._header('X-Request-ID'),
                 'actor_id': self._actor(),
                 'attempt_id': self._attempt_id,
                 'idempotency_key': self._header('Idempotency-Key'),
                 'declared_sha256': self._header('Content-SHA256'),
                 'declared_length': self._header('Content-Length')}
        for field, header in (('repository', 'X-Repository'), ('project', 'X-Project'),
                              ('version', 'X-Version'), ('object_path', 'X-Object-Path')):
            facts[field] = self._header(header)
        facts['protocol_version'] = getattr(self, 'request_version', None)
        facts['effective_route'] = (self.path if self.command == 'POST' else urlsplit(self.path).path) if self.path else None
        length = facts['declared_length']
        facts['declared_size_bytes'] = int(length) if length and length.isdecimal() else None
        sha = facts['declared_sha256']
        facts['validated_declared_sha256'] = sha if sha and re.fullmatch(r'[0-9a-f]{64}', sha) else None
        facts['canonical_identity'] = None
        if self.command == 'POST' and self.path == '/v1/objects':
            try:
                identity = {k: facts[k] for k in ('repository', 'project', 'version', 'object_path')}
                identity['object_path'] = normalize_object_path(identity['object_path'])
                if all(isinstance(v, str) and v for v in identity.values()):
                    from .audit import derive_object_id
                    identity['object_id'] = derive_object_id(**identity)
                    facts['canonical_identity'] = identity
            except (StoreError, TypeError):
                pass
        elif self.path:
            match = re.fullmatch(r'/v1/objects/([0-9a-f]{64})(?:/metadata)?', urlsplit(self.path).path)
            if match:
                facts['canonical_identity'] = {'object_id': match.group(1)}
        return facts

    def send_response(self, code, message=None):
        self._receipt_status = code
        return super().send_response(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() not in ('authorization', 'set-cookie'):
            self._receipt_headers[keyword] = value
        return super().send_header(keyword, value)

    def _header(self, name):
        headers = getattr(self, "headers", None)
        return headers.get(name) if headers is not None else None

    def _request_id(self):
        """Public request ID: one stable value per request; client IDs may repeat."""
        if getattr(self, "_stable_request_id", None) is None:
            supplied = self._header("X-Request-ID")
            ok = supplied and len(supplied) <= 128 and supplied.isprintable()
            self._stable_request_id = supplied if ok else uuid.uuid4().hex
        return self._stable_request_id

    def _actor(self):
        """Configured credential ID, or a constant; never echo an unknown bearer."""
        auth = self._header("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else ""
        configured = self.server.store.credentials.get(token)
        if configured is None:
            return "unauthenticated"
        return self.server.store.public_credential_id(token)

    def _observe(self, object_id):
        """Copy the object and aggregate reservation bytes inside one store-lock boundary.

        This is a sample taken at ``observed_at``; the authoritative before/after
        facts and reservation lifecycle are the store's commit and reservation events.
        """
        store = self.server.store
        with store._lock:
            found = store._objects.get(object_id) if object_id else None
            found = dict(found) if found else None
            reserved = sum(value[0] for value in store._reservations.values())
        return {"boundary": "store_lock_snapshot", "observed_at": time.time(),
                "object_present": found is not None,
                "size_bytes": found["size_bytes"] if found else None,
                "sha256": found["sha256"] if found else None,
                "reservation_bytes_observed": reserved}

    def _audit_request(self, status, response=None, *, object_id=None, digest=None, size=None):
        """Record a request phase (proposal or outcome), including rejected requests.

        This complements, and never replaces, the store's durable mutation
        journal. A sink failure is surfaced as an unknown service outcome so
        callers reconcile against the commit journal and bytes.
        """
        phase = getattr(self, "_audit_phase", "outcome")
        pre = getattr(self, "_pre_observation", None)
        post = self._observe(object_id)
        command, path = getattr(self, "command", None), getattr(self, "path", None)
        request_facts = {"operation": command, "target": path,
            "idempotency_key": self._header("Idempotency-Key"),
            "content_sha256": self._header("Content-SHA256"),
            "content_length": self._header("Content-Length"),
            "repository": self._header("X-Repository"), "project": self._header("X-Project"),
            "version": self._header("X-Version"), "object_path": self._header("X-Object-Path")}
        # HTTP-fact digest.  It is NOT the storage operation_digest; the verifier
        # binds them through the retained idempotency identity.
        request_digest = hashlib.sha256(json.dumps(request_facts, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        length = self._header("Content-Length")
        event = {
            "event_id": uuid.uuid4().hex, "event_type": "service.request",
            "occurred_at": time.time(), "attempt_id": self._attempt_id,
            "request_id": self._request_id(), "phase": phase,
            "actor_id": self._actor(), "operation": command, "path": path,
            "status": status, "result": (response or {}).get("error"),
            "object_id": object_id,
            "pre_observation": pre, "post_observation": post,
            "before_size_bytes": (pre or {}).get("size_bytes"), "before_sha256": (pre or {}).get("sha256"),
            "after_size_bytes": post["size_bytes"], "after_sha256": post["sha256"],
            "request_sha256": digest or request_digest, "request_size_bytes": size,
            "idempotency_key": request_facts["idempotency_key"],
            "content_sha256": request_facts["content_sha256"],
            "content_length": length, "repository": request_facts["repository"],
            "project": request_facts["project"], "version": request_facts["version"],
            "object_path": request_facts["object_path"],
            "reservation_bytes": post["reservation_bytes_observed"],
        }
        if command == "POST":
            try:
                event["request_size_bytes"] = int(length or "0")
            except ValueError:
                event["request_size_bytes"] = None
        try:
            self.server.store.audit(event)
        except Exception as exc:
            raise StoreError("audit_unavailable", f"audit sink unavailable: {type(exc).__name__}", 503) from exc
        if phase == "outcome":
            self._outcome_done = True

    def _proposal(self, object_id=None):
        self._audit_phase = "proposal"
        try:
            self._pre_observation = self._observe(object_id)
            self._audit_request(0, {"request_id": self._request_id(), "error": None}, object_id=object_id)
            self._proposal_done = True
        finally:
            self._audit_phase = "outcome"

    def send_error(self, code, message=None, explain=None):
        # Malformed/unsupported framing or methods are still audited attempts.
        self._receipt_error = 'http_error'
        try:
            if not getattr(self, "_proposal_done", False):
                self._proposal()
            if not getattr(self, "_outcome_done", False):
                self._audit_request(code, {"error": {"code": "http_error", "message": message or ""}})
        except Exception:
            pass
        super().send_error(code, message, explain)

    def _credential(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:]:
            raise StoreError("capability_denied", "bearer credential required", 403)
        # The private lookup key is retained for authorization and idempotency;
        # storage serialization resolves the configured nonsecret identity.
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
        # The store works with the private per-attempt ID; callers see the one
        # stable public ID of this request.
        value = {**value, "request_id": self._request_id()}
        self._receipt_error = (value.get('error') or {}).get('code')
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("X-Request-ID", self._request_id())
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
        rid = self._attempt_id  # store-facing ID: private and unique per attempt
        try:
            self._proposal()
        except StoreError as e:
            return self._error(e)
        try:
            if self.path != "/v1/objects":
                raise StoreError("not_found", "route not found", 404)
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
            self._body_observer = ReadObserver(self.rfile, self.server.receipts, self._exchange_id)
            status, response = self.server.store.upload_stream(
                credential_id=credential, repository=self.headers["X-Repository"],
                project=self.headers["X-Project"], version=self.headers["X-Version"],
                object_path=self.headers["X-Object-Path"], idempotency_key=self.headers["Idempotency-Key"],
                content=size, content_sha256=self.headers["Content-SHA256"], request_id=rid,
                owner_id=policy["owner_id"], retention=policy["retention"],
                dependencies=policy.get("dependencies", []), _reader=self._body_observer)
            if size == 0 and status == 201:
                # New storage responses retain the installed body object;
                # replay returns a copy. Only a newly evaluated zero-byte
                # upload may attest the complete empty application body.
                with self.server.store._lock:
                    installed = self.server.store._keys.get(self.headers['Idempotency-Key'], {})
                    self._completed_empty = response.get('body') is installed.get('body')
            self._json(status, response)
        except StoreError as e:
            self._error(e)

    def do_GET(self):
        rid = self._attempt_id
        match = re.fullmatch(r"/v1/objects/([0-9a-f]{64})(?:/metadata)?", urlsplit(self.path).path)
        try:
            self._proposal(match.group(1) if match else None)
        except StoreError as e:
            return self._error(e)
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
                self.send_header("X-Request-ID", self._request_id())
                self.send_header("Content-SHA256", result["sha256"])
                self.end_headers(); self.wfile.write(content)
                return
            match = re.fullmatch(r"/v1/repositories/([^/]+)/releases", parts.path)
            if match:
                status, body = self.server.store.list_repository(credential, unquote(match.group(1)), rid)
                return self._json(status, body)
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
            self._error(e)

    def do_DELETE(self):
        rid = self._attempt_id
        match = re.fullmatch(r"/v1/objects/([0-9a-f]{64})", urlsplit(self.path).path)
        try:
            self._proposal(match.group(1) if match else None)
            if not match:
                raise StoreError("not_found", "route not found", 404)
            credential = self._credential()
            status, body = self.server.store.delete(credential, match.group(1),
                                                     self.headers.get("Idempotency-Key", ""), rid)
            self._json(status, body)
        except StoreError as e:
            self._error(e)


def create_server(root, quota_bytes, *, host="127.0.0.1", port=0, credentials=None,
                  aliases=None, max_upload_bytes=64 * 1024 * 1024, metadata_policy=None, audit=None, receipt_sink=None,
                  usage_visibility=None):
    """Create (but do not start) an HTTP server and its independent store."""
    from pathlib import Path
    root_path = Path(root)
    sink = audit or AppendOnlyAudit(root_path.parent / (root_path.name + ".audit.jsonl"))
    store = ArtifactStore(root_path, quota_bytes, credentials=credentials, aliases=aliases,
                          max_upload_bytes=max_upload_bytes, metadata_policy=metadata_policy, audit=sink)
    observer = receipt_sink if receipt_sink is not None else ServiceReceipts(root_path.parent / (root_path.name + ".receipts.jsonl"))
    return ArtifactHTTPServer((host, port), store, observer)
