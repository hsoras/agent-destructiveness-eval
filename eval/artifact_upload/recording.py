"""Opt-in exact capture composed with the T008 host adapter, not a second runtime.

Canonical records are private (0700 directory, 0600 files), never researcher
exports. Researcher streams redact declared credentials and record every change.
T012 must supply guest loaders and checkpoint/service/outcome records.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import threading
from typing import Any, Callable, Mapping
import uuid

from .host_adapter import (HostOpenCodeAdapter, PassthroughPresenter, Presented, ProviderGuard, RouterBridge,
                           _GUEST_HEAD_NOTICE, _GUEST_TAIL_NOTICE)
from .output_store import OutputStore, OutputStoreBroker


class CaptureFailure(RuntimeError):
    """Evidence is incomplete. No further dispatch or provider forwarding allowed."""


class RunRecorder:
    STREAMS = frozenset({"tool_events", "raw_outputs", "presented_outputs", "model_views", "full_outputs", "proxy_events",
                         "service_events", "quota_telemetry", "workflow_events", "diagnostics", "sanitization", "run_state"})

    def __init__(self, root: str | Path, run_id: str, *, credentials: Mapping[str, str] | None = None):
        self.root, self.run_id = Path(root).resolve(), run_id
        self.private = self.root / "canonical-private"
        self.export = self.root / "researcher"
        for path in (self.root, self.private, self.export):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        self.credentials = dict(credentials or {})
        if any(not key or not secret for key, secret in self.credentials.items()):
            raise ValueError("credentials require nonempty IDs and secrets")
        self._lock = threading.RLock()
        self.failure: str | None = None
        self.service_health: Callable[[], Mapping] | None = None
        self._checking_health = False
        self.host_paths: set[str] = set()

    def check(self, *, service=True) -> None:
        if self.failure:
            raise CaptureFailure(self.failure)
        if service and self.service_health is not None and not self._checking_health:
            self._checking_health = True
            try:
                health = self.service_health()
                if health.get('healthy') is not True or health.get('sealed') is True:
                    self.fail('service receipt observer unhealthy or stopped')
            except CaptureFailure:
                raise
            except Exception:
                self.fail('service receipt health unavailable')
            finally:
                self._checking_health = False

    def fail(self, reason: str) -> None:
        # Do not include exception text: it may itself contain a credential.
        self.failure = f"capture incomplete: {reason}; policy=stop"
        try:
            self._write(self.export / "capture_failures.jsonl", {"run_id": self.run_id, "occurred_at": self.now(), "reason": self.failure})
        except OSError:
            pass  # in-memory failure remains authoritative when the disk itself fails
        raise CaptureFailure(self.failure)

    def import_service_receipts(self, bundle):
        from sandbox.artifact_upload.receipts import validate_bundle
        try:
            # Preserve exact source even if validation fails, for diagnosis.
            raw = base64.b64decode(bundle['source_bytes_b64'], validate=True)
            source = self.private / ('service-receipts-' + hashlib.sha256(raw).hexdigest() + '.jsonl')
            if source.exists():
                if source.is_symlink() or source.read_bytes() != raw:
                    raise ValueError('source retention collision')
            else:
                fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            validated = validate_bundle(bundle)
        except Exception:
            self.fail('service receipt source incomplete')
        # Evidence writes remain allowed after the terminal seal; dispatch and
        # provider forwarding still call check() with live health enabled.
        for record in validated['records']:
            self.append('service_events', record)
        return validated

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _write(path: Path, record: Any) -> None:
        data = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def sanitize(self, value: Any) -> tuple[Any, list[str]]:
        used: set[str] = set()
        def walk(item: Any) -> Any:
            if isinstance(item, str):
                for path in sorted(self.host_paths, key=len, reverse=True):
                    item = item.replace(path, '[private host runtime]')
                for credential_id, secret in sorted(self.credentials.items(), key=lambda pair: -len(pair[1])):
                    if secret in item:
                        item = item.replace(secret, f"[credential:{credential_id}]")
                        used.add(credential_id)
                return item
            if isinstance(item, dict):
                copied = dict(item)
                for field in list(copied):
                    if isinstance(field, str) and (field.endswith('_b64') or field.endswith('_base64')):
                        copied[field + '_decoded'] = base64.b64decode(copied.pop(field), validate=True).decode('utf-8', errors='replace')
                        copied['researcher_decoding'] = 'decoded; exact source retained privately'
                if copied.get("encoding") == "base64":
                    for field in ("stdout", "stderr"):
                        if field in copied:
                            copied[field] = base64.b64decode(copied[field]).decode("utf-8", errors="replace")
                    copied["encoding"] = "utf-8"
                    copied["researcher_decoding"] = "base64 decoded; canonical bytes retained privately"
                return {walk(k): walk(v) for k, v in copied.items()}
            if isinstance(item, (list, tuple)):
                return [walk(v) for v in item]
            return item
        return walk(value), sorted(used)

    def append(self, stream: str, payload: Any) -> str:
        with self._lock:
            self.check(service=False)
            if stream not in self.STREAMS:
                raise ValueError("unknown evidence stream")
            eid = uuid.uuid4().hex
            canonical = {"event_id": eid, "run_id": self.run_id, "occurred_at": self.now(), "payload": payload}
            try:
                sanitized, ids = self.sanitize(canonical)
                sanitized["sanitization"] = {"credential_ids": ids, "canonical_retained_privately": True,
                                              "host_path_redaction_enabled": bool(self.host_paths)}
                self._write(self.private / f"{stream}.jsonl", canonical)
                self._write(self.export / f"{stream}.jsonl", sanitized)
                if ids or self.host_paths:
                    self._write(self.export / "sanitization.jsonl", {"event_id": eid, "stream": stream, "credential_ids": ids, "host_path_redaction_enabled": bool(self.host_paths), "occurred_at": self.now()})
            except Exception:
                self.fail("evidence persistence failed")
            return eid

    def request(self, raw: bytes, *, path: str) -> str:
        # Parse only for researcher export; the canonical wire body retains whitespace/key order exactly.
        body = json.loads(raw)
        sanitized, ids = self.sanitize(body)
        payload = {"path": path, "request": body, "wire_sha256": hashlib.sha256(raw).hexdigest(), "wire_bytes": len(raw),
                   "replay_body_base64": base64.b64encode(raw).decode(), "call_ids": self._call_ids(body),
                   "sanitized_request": sanitized, "credential_ids": ids}
        # Never export base64 containing secrets (ordinary string replacement cannot redact base64).
        with self._lock:
            self.check()
            eid = uuid.uuid4().hex
            record = {"event_id": eid, "run_id": self.run_id, "occurred_at": self.now(), "payload": payload}
            export, _ = self.sanitize(record)
            export["payload"]["replay_body_base64"] = None if ids else payload["replay_body_base64"]
            export["sanitization"] = {"credential_ids": ids, "exact_replay_available_privately": True}
            try:
                self._write(self.private / "model_views.jsonl", record)
                self._write(self.export / "model_views.jsonl", export)
                if ids:
                    self._write(self.export / "sanitization.jsonl", {"event_id": eid, "stream": "model_views", "credential_ids": ids})
            except Exception:
                self.fail("model request persistence failed")
            return eid

    @staticmethod
    def _call_ids(body: dict[str, Any]) -> list[str]:
        ids = set()
        for message in body.get("messages", []):
            if message.get("tool_call_id"):
                ids.add(message["tool_call_id"])
            ids.update(c["id"] for c in message.get("tool_calls", []) if c.get("id"))
        return sorted(ids)

    def replay(self, event_id: str) -> bytes:
        self.check(service=False)
        for line in (self.private / "model_views.jsonl").read_text().splitlines():
            record = json.loads(line)
            if record["event_id"] == event_id:
                raw = base64.b64decode(record["payload"]["replay_body_base64"])
                if hashlib.sha256(raw).hexdigest() != record["payload"]["wire_sha256"]:
                    self.fail("replay integrity failure")
                return raw
        raise KeyError(event_id)


class RecordingProviderGuard(ProviderGuard):
    def __init__(self, *args: Any, recorder: RunRecorder, after_capture=None, route_source=None, **kwargs: Any):
        self.recorder = recorder
        self.after_capture = after_capture
        self.route_source = dict(route_source or {})
        super().__init__(*args, **kwargs)

    def _on_retry(self, entry: dict[str, Any]) -> None:
        self.recorder.append("proxy_events", {"kind": "upstream_retry", **entry})

    def _forward(self, handler: Any, raw: bytes) -> None:
        try:
            self.recorder.check()
            eid = self.recorder.request(raw, path=handler.path)
            body = json.loads(raw)
            # The guard's actual transport destination, never a supplied diagnostic route.
            host = self.upstream.hostname
            if ':' in host:
                host = '[' + host + ']'
            base = self.upstream.scheme + '://' + host
            if self.upstream.port:
                base += ':' + str(self.upstream.port)
            route = {"base_url": base + self.upstream.path, "model": body.get('model'),
                     "model_revision": None, "model_revision_status": "unknown",
                     "source_config": self.route_source}
            captured = {"model_view_event_id": eid, "wire_sha256": hashlib.sha256(raw).hexdigest(),
                        "route_provenance": route,
                        "original_provider": body.get("provider"),
                        "forwarded_provider": json.loads(self._forwarded_raw(raw)).get("provider")}
            self.recorder.append("proxy_events", {"kind": "captured_route", **captured})
            if self.after_capture is not None:
                try:
                    self.after_capture(captured)
                except CaptureFailure:
                    raise
                except Exception:
                    self.recorder.fail("post-capture checkpoint callback failed")
            self.recorder.check()
            self.recorder.append("proxy_events", {"kind": "forward_authorized", "model_view_id": eid})
        except CaptureFailure:
            return self._refuse(handler, 500, "capture incomplete; run stopped")
        super()._forward(handler, raw)  # original exact wire bytes, original ProviderGuard transport


class RecordingBridge(RouterBridge):
    def __init__(self, *args: Any, recorder: RunRecorder, **kwargs: Any):
        self.recorder = recorder
        super().__init__(*args, **kwargs)

    def _event(self, kind: str, status: str, tool: str, session_id: str | None, call_id: str | None, raw: Any, presented: Any, **meta: Any) -> None:
        self.recorder.append("tool_events", {"kind": kind, "status": status, "tool": tool, "session_id": session_id,
                                             "call_id": call_id, "raw": raw, "presented": presented, "metadata": meta})
        super()._event(kind, status, tool, session_id, call_id, raw, presented, **meta)

    def call(self, body: dict[str, Any]) -> dict[str, Any]:
        self.recorder.check()
        reply = super().call(body)
        self.recorder.append("presented_outputs", {"session_id": body["session_id"], "call_id": body["call_id"], "reply": reply,
                                                   "authority": "final bridge reply before runtime serialization"})
        return reply


_SPOOL = re.compile(r"Full output saved to: ([^\n]{1,4096})")


class RecordingPresenter:
    def __init__(self, presenter: Any, store: OutputStore, recorder: RunRecorder, *, guest_bytes: Callable[[str], bytes] | None,
                 guest_spool_roots: tuple[str, ...]):
        self.presenter, self.store, self.recorder = presenter, store, recorder
        self.guest_bytes, self.guest_spool_roots = guest_bytes, guest_spool_roots
        # Raw evidence is never registered with the target's read/search broker.
        self.raw_store = OutputStore(recorder.private / "raw-outputs", store.run_id)

    def present(self, call: dict[str, Any], result: dict[str, Any]) -> Presented:
        self.recorder.check()
        try:
            full_result = dict(result)
            expanded = False
            # Reconstruct the original operation's full result before applying
            # its approved Presenter. A later read/grep cannot reapply shell rules.
            for field in ("stdout", "stderr"):
                data = base64.b64decode(result[field]) if result["encoding"] == "base64" else result[field].encode()
                stream_text = data.decode("utf-8", errors="replace")
                notice = _GUEST_HEAD_NOTICE.match(stream_text) or _GUEST_TAIL_NOTICE.search(stream_text)
                path = _SPOOL.search(notice[0])[1] if notice else None
                if path:
                    pure = PurePosixPath(path)
                    if not self.guest_bytes or ".." in pure.parts or not any(pure.parent == PurePosixPath(root) for root in self.guest_spool_roots) or not re.fullmatch(r"tool_[0-9a-f]{32}", pure.name):
                        self.recorder.fail("unresolvable guest full-output spool")
                    data = self.guest_bytes(path)
                    expanded = True
                stream_uri = self.raw_store.put(data)
                self.recorder.append("full_outputs", {"call_id": call["call_id"], "session_id": call["session_id"], "uri": stream_uri,
                                                     "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                                                     "text": data.decode("utf-8", errors="replace"), "source": "guest_spool" if path else "raw_stream",
                                                     "stream": field, "target_readable": False})
                full_result[field] = base64.b64encode(data).decode() if result["encoding"] == "base64" else data.decode("utf-8", errors="replace")
            try:
                presented = self.presenter.present(call, full_result)
            except Exception:
                self.recorder.fail("full-stream presentation unavailable")
            # A presenter that still advertises a guest spool has not established
            # a self-contained presented continuation. Stop rather than expose raw.
            if expanded and (_GUEST_HEAD_NOTICE.match(presented.output) or _GUEST_TAIL_NOTICE.search(presented.output)):
                self.recorder.fail("full-stream presentation unresolved")
            full_data = presented.output.encode()
            full_uri = self.store.put(full_data)
            self.recorder.append("full_outputs", {"call_id": call["call_id"], "session_id": call["session_id"], "uri": full_uri,
                                                 "sha256": hashlib.sha256(full_data).hexdigest(), "size_bytes": len(full_data),
                                                 "text": presented.output, "source": "presenter_full", "target_readable": True,
                                                 "metadata": presented.metadata})
            output, mediated_uri = self.store.preview(presented.output, force_uri=full_uri if expanded else None)
            metadata = {**presented.metadata, "output_uri": mediated_uri or full_uri}
            self.recorder.append("presented_outputs", {"call": call, "output": output, "is_error": presented.is_error,
                                                       "metadata": metadata, "output_uri": metadata["output_uri"],
                                                       "capability_difference": "immutable broker URI; literal search; no shell projection"})
            return Presented(output, presented.is_error, metadata)
        except CaptureFailure:
            raise
        except Exception:
            self.recorder.fail("full output capture failed")


class RecordingOpenCodeAdapter(HostOpenCodeAdapter):
    """T012 entry point; same constructor as HostOpenCodeAdapter plus capture hooks.

    ``guest_bytes`` MUST read actual bytes inside this run's guest and reject
    symlink escapes. No host-file fallback is permitted. ``guest_spool_roots``
    must identify that worker's actual generated spool directory.
    """

    def __init__(self, *, recording_root: str | Path, credentials: Mapping[str, str] | None = None,
                 guest_bytes: Callable[[str], bytes] | None = None, guest_spool_roots: tuple[str, ...] = (), **kwargs: Any):
        self.recorder = RunRecorder(recording_root, kwargs["run_id"], credentials=credentials)
        self.output_store = OutputStore(self.recorder.private / "outputs", kwargs["run_id"])
        presenter = RecordingPresenter(kwargs.pop("presenter", None) or PassthroughPresenter(), self.output_store, self.recorder,
                                       guest_bytes=guest_bytes, guest_spool_roots=guest_spool_roots)
        kwargs["broker"] = OutputStoreBroker(kwargs["broker"], self.output_store, self.recorder)
        kwargs.setdefault("stage_root", self.recorder.private / "runtime-stage")
        super().__init__(presenter=presenter, **kwargs)
        old = self.bridge
        self.bridge = RecordingBridge(run_id=self.run_id, broker=self.broker, registry=self.registry, guest_profile=self.profile,
                                      presenter=presenter, events_path=self.evidence / "router-events.jsonl", recorder=self.recorder)
        old._server.server_close()  # not started: shutdown() would wait forever

    def _run_process(self, root, work, config, prompt_args, log_prefix, *, session_id=None, timeout=120):
        try:
            return super()._run_process(root, work, config, prompt_args, log_prefix,
                                        session_id=session_id, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                if getattr(exc, 'capture_errors', None) != []:
                    raise ValueError('adapter timeout collection errors or missing API')
                prefix = Path(log_prefix).absolute()
                owner_root = Path(root).resolve()
                if not prefix.resolve().is_relative_to(owner_root):
                    raise ValueError('timeout prefix outside adapter invocation')
                expected_receipt = Path(str(prefix) + '.timeout.json')
                if Path(exc.capture_receipt_path).absolute() != expected_receipt:
                    raise ValueError('timeout receipt is not invocation-owned')
                def read_owned(path):
                    # Reject symlink components as well as a symlink leaf.
                    if path.resolve() != path or not path.is_relative_to(owner_root):
                        raise ValueError('timeout source path escaped')
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'rb') as stream:
                        facts = os.fstat(stream.fileno())
                        if not stat.S_ISREG(facts.st_mode) or facts.st_mode & 0o077:
                            raise ValueError('timeout source must be a private regular file')
                        return stream.read()
                receipt = json.loads(read_owned(expected_receipt))
                if (receipt.get('kind') != 'native_process_timeout' or receipt.get('capture_complete') is not True
                        or receipt.get('capture_errors') != [] or receipt.get('encoding') != 'bytes'
                        or receipt.get('timeout_seconds') != timeout):
                    raise ValueError('timeout receipt incomplete or mismatched')
                streams = {}
                for name in ('stdout', 'stderr'):
                    path = Path(str(prefix) + '.' + name + '.log')
                    source = receipt['streams'][name]
                    if Path(source['path']).absolute() != path:
                        raise ValueError('timeout stream is not invocation-owned')
                    data = read_owned(path)
                    if len(data) != source['size_bytes'] or hashlib.sha256(data).hexdigest() != source['sha256']:
                        raise ValueError('timeout stream size or hash mismatch')
                    if data != (exc.output if name == 'stdout' else exc.stderr):
                        raise ValueError('timeout exception disagrees with retained stream')
                    streams[name] = data
                payload = {key: receipt[key] for key in ('kind', 'timeout_seconds', 'pid', 'kill_signal', 'exit_code', 'cancellations')}
                payload.update({'capture_complete': True, 'encoding': 'base64',
                    'streams': {name: {'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                                for name, data in streams.items()},
                    **{name: base64.b64encode(data).decode() for name, data in streams.items()}})
            except Exception:
                self.recorder.fail('native timeout capture incomplete')
            self.recorder.host_paths.update(str(Path(path).resolve()) for path in (root, self.stage, config, work))
            self.recorder.append('diagnostics', payload)
            raise  # preserve the same original timeout and never redispatch

    def audit_request(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.recorder.check()
        if str(self.stage) in json.dumps(body, ensure_ascii=False):
            self.recorder.fail("host staging path in model input")
        return super().audit_request(body)

    def stage_runtime(self) -> None:
        self.recorder.check()
        if self._staged:
            return
        super().stage_runtime()
        self.guard.close()
        self.guard = RecordingProviderGuard(self.base_url, self.audit_request, recorder=self.recorder,
                                           max_request_bytes=self.max_request_bytes, zdr=self.zdr,
                                           provider_pin=self.provider_pin).start()
        self._write_opencode_config(self.config_dir, self.guard.url, router=True)

    def run_turn(self, prompt: str, **kwargs: Any) -> Any:
        self.recorder.check()
        self.recorder.append("workflow_events", {"kind": "turn_started", "prompt": prompt, "session_id": kwargs.get("session_id")})
        try:
            turn = super().run_turn(prompt, **kwargs)
        except Exception:
            self.recorder.check()
            raise
        self.recorder.check()
        self.recorder.append("workflow_events", {"kind": "turn_finished", "session_id": turn.session_id, "exit_code": turn.exit_code,
                                                 "usage": turn.usage, "errors": turn.errors, "events": turn.events})
        return turn

    def guest_context(self, path: str, loader: Callable[[str], bytes]) -> str:
        if not PurePosixPath(path).is_relative_to(PurePosixPath(self.profile["worktree"])) or ".." in PurePosixPath(path).parts:
            raise ValueError("context must be in the guest worktree")
        self.recorder.check()
        try:
            data = loader(path)
            self.recorder.append("workflow_events", {"kind": "guest_context", "path": path, "sha256": hashlib.sha256(data).hexdigest(), "text": data.decode()})
            return data.decode()
        except Exception:
            self.recorder.fail("guest context capture failed")

    def attachment(self, data: bytes, *, name: str) -> str:
        """Declared artifact mechanism; never pass raw host attachment names to OpenCode."""
        self.recorder.check()
        try:
            uri = self.output_store.put(data)
            self.recorder.append("full_outputs", {"kind": "attachment", "name": PurePosixPath(name).name, "uri": uri,
                                                 "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "text": data.decode("utf-8", errors="replace")})
            return uri
        except Exception:
            self.recorder.fail("attachment capture failed")
