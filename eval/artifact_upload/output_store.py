"""Immutable host artifacts resolved only by run-bound broker read/search calls."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import threading
from typing import Any

from .contracts import argument_digest, validate
from .broker import IdentityConflict
from .host_adapter import RUNTIME_MAX_BYTES, RUNTIME_MAX_LINES


class OutputAccessError(ValueError):
    pass


class OutputStore:
    """Immutable artifact bytes; URIs never resolve a caller-provided host path.

    Read offsets are one-based lines (zero has the native default meaning).
    Search uses literal content matching, explicitly declared in every result.
    """

    def __init__(self, root: str | Path, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", run_id):
            raise ValueError("unsafe run ID")
        self.run_id = run_id
        self.root = Path(root).resolve() / run_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._lock = threading.RLock()

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest
        with self._lock:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            except FileExistsError:
                if self.get(f"tool-output://{self.run_id}/{digest}") != data:
                    raise OutputAccessError("artifact integrity failure")
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        return f"tool-output://{self.run_id}/{digest}"

    def get(self, uri: str) -> bytes:
        match = re.fullmatch(r"tool-output://([A-Za-z0-9_.-]+)/([0-9a-f]{64})", uri)
        if not match or match[1] != self.run_id:
            raise OutputAccessError("invalid or cross-run output reference")
        try:
            fd = os.open(self.root / match[2], os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read()
        except OSError as exc:
            raise OutputAccessError("output reference unavailable") from exc
        if hashlib.sha256(data).hexdigest() != match[2]:
            raise OutputAccessError("artifact integrity failure")
        return data

    def read(self, uri: str, *, offset: int = 1, limit: int = 2000) -> str:
        if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 0:
            raise OutputAccessError("offset and limit must be nonnegative integers")
        rows = self.get(uri).decode("utf-8", errors="replace").splitlines()
        start = (offset or 1) - 1
        selected = rows[start:start + min(limit, 1900)]
        # Avoid the runtime's second truncation, while stating any intra-line cut.
        output = [f"<path>{uri}</path>", "<content>"]
        size = len("\n".join(output).encode())
        consumed = 0
        for index, line in enumerate(selected, start + 1):
            row = f"{index}: {line}"
            room = RUNTIME_MAX_BYTES - size - 2048
            if len(row.encode()) + 1 > room:
                if consumed == 0 and room > 100:
                    output.append(row.encode()[:room - 100].decode("utf-8", errors="ignore") + " [line preview cut; use content search]")
                break
            output.append(row)
            size += len(row.encode()) + 1
            consumed += 1
        output += [f"(Showing {consumed} complete lines from {offset or 1} of {len(rows)}; next offset={start + consumed + 1})", "</content>"]
        return "\n".join(output)

    def search(self, uri: str, pattern: str, *, offset: int = 0, limit: int = 100) -> str:
        if not isinstance(pattern, str) or type(offset) is not int or type(limit) is not int or offset < 0 or limit < 0:
            raise OutputAccessError("invalid content search arguments")
        matches = [f"{i}: {row}" for i, row in enumerate(self.get(uri).decode("utf-8", errors="replace").splitlines(), 1) if pattern in row]
        selected = matches[offset:offset + min(limit, 100)]
        output = f"{uri}\nLiteral content search: {len(matches)} matching lines; match offset={offset}\n"
        for row in selected:
            room = RUNTIME_MAX_BYTES - len(output.encode()) - 512
            if room <= 0:
                break
            output += row.encode()[:room].decode("utf-8", errors="ignore") + "\n"
        return output + "Search previews are bounded; full bytes remain immutable.\n"

    def preview(self, text: str, *, force_uri: str | None = None) -> tuple[str, str | None]:
        if force_uri is None and len(text.encode()) <= RUNTIME_MAX_BYTES and text.count("\n") + 1 <= RUNTIME_MAX_LINES:
            return text, None
        uri = force_uri or self.put(text.encode())
        notice = f"\n\nFull output: {uri}\nUse read with one-based line offset/limit or grep with a literal pattern. Read/search only; shell, write, edit and patch cannot access this URI.\n"
        room = RUNTIME_MAX_BYTES - len(notice.encode()) - 32
        preview = "\n".join(text.split("\n")[:1900]).encode()[:room].decode("utf-8", errors="ignore")
        return preview + notice, uri


def _contains_uri(value: Any) -> bool:
    if isinstance(value, str):
        return "tool-output:" in value
    if isinstance(value, dict):
        return any(_contains_uri(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_uri(v) for v in value)
    return False


class OutputStoreBroker:
    """Compose with ExecutionBroker; never dispatch an artifact operation to guest tools."""

    def __init__(self, broker: Any, store: OutputStore, recorder: Any):
        self.broker, self.store, self.recorder = broker, store, recorder
        self._calls: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.broker, name)

    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        self.recorder.check()
        validate("call", call)
        if call["run_id"] != self.store.run_id or argument_digest(call["arguments"]) != call["argument_digest"]:
            raise OutputAccessError("invalid call identity")
        self.recorder.append("tool_events", {"kind": "dispatch", "call": call})
        args, op = call["arguments"], call["operation"]
        if not _contains_uri(args):
            result = self.broker.execute(call)
            self.recorder.append("raw_outputs", {"call": call, "result": result})
            return result
        with self._lock:
            key = (call["session_id"], call["call_id"])
            prior = self._calls.get(key)
            if prior:
                if prior[0] != call["argument_digest"]:
                    raise IdentityConflict("artifact call ID reused with different arguments")
                return prior[1]
            try:
                if op == "read" and set(args) <= {"filePath", "offset", "limit"}:
                    text = self.store.read(args["filePath"], offset=args.get("offset", 1), limit=args.get("limit", 2000))
                elif op == "grep" and set(args) <= {"path", "pattern"}:
                    text = self.store.search(args["path"], args["pattern"])
                else:
                    raise OutputAccessError("tool outputs support read and literal content search only")
                status, error, stderr = "ok", None, ""
            except OutputAccessError as exc:
                status, error, stderr, text = "unsupported", "output_access_denied", str(exc), ""
            result = {k: call[k] for k in ("contract_version", "run_id", "session_id", "call_id", "argument_digest")}
            result.update(status=status, exit_code=0 if status == "ok" else None, stdout=text, stderr=stderr,
                          encoding="utf-8", error_code=error, output_uri=None, display_fields={"aliases": {}, "changed_fields": []},
                          worker_digest=getattr(self.broker.worker, "digest", "0" * 64))
            validate("result", result)
            self.recorder.append("raw_outputs", {"call": call, "result": result, "capability": "immutable literal content search"})
            self._calls[key] = (call["argument_digest"], result)
            return result
