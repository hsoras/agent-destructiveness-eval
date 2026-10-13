"""Authenticated, same-process-owner control for live proxy replenishment.

The executor owns this server for the lifetime of an active run. Requests only
queue additive allowances; the executor applies them at its serialized boundary.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import secrets
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import tempfile
import threading
from typing import Any, Callable
import urllib.request
import urllib.error


DESCRIPTOR = "proxy-control.json"
_ALLOWANCE_FIELDS = {"cost_usd", "pairs"}


ACCOUNTS = ("proposer", "reviewer")


def _fields(value: Any) -> dict[str, float | int]:
    if not isinstance(value, dict) or not value:
        raise ValueError("proxy allowance increments must be a non-empty object")
    if set(value) - _ALLOWANCE_FIELDS:
        raise ValueError("unsupported proxy allowance field; only cost_usd and pairs can be replenished")
    result: dict[str, float | int] = {}
    for key, amount in value.items():
        if key == "cost_usd":
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
                raise ValueError("cost_usd increment must be a finite positive number")
            result[key] = float(amount)
        else:
            if type(amount) is not int or amount <= 0:
                raise ValueError("pairs increment must be a positive integer")
            result[key] = amount
    return result


def _allowances(value: Any) -> dict[str, dict[str, float | int]]:
    """Normalize to ``{account: {cost_usd?, pairs?}}``. A flat mapping is the legacy alias for the proposer account
    only (no credit is ever invented for the reviewer); account-keyed input names each account explicitly."""
    if isinstance(value, dict) and value and set(value) <= set(ACCOUNTS):
        return {account: _fields(amounts) for account, amounts in value.items()}
    return {"proposer": _fields(value)}


def merge_allowances(left: dict, right: dict) -> dict:
    out = {a: dict(f) for a, f in left.items()}
    for account, fields in right.items():
        slot = out.setdefault(account, {})
        for key, amount in fields.items():
            slot[key] = slot.get(key, 0) + amount
    return out


def callback_batch(batch: dict) -> dict:
    """Legacy callbacks receive a flat mapping when only the proposer account is replenished."""
    return dict(batch["proposer"]) if set(batch) == {"proposer"} else {a: dict(f) for a, f in batch.items()}


def _write_descriptor(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".proxy-control-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class _Server(ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address, handler, controller):
        self.controller = controller
        super().__init__(address, handler)


class _Handler(BaseRequestHandler):
    def handle(self):
        try:
            stream = self.request.makefile("rb")
            line = stream.readline(4096)
            if not line.startswith(b"POST /replenish HTTP/1."):
                return self._reply(400, {"error": "invalid request"})
            headers = {}
            while True:
                line = stream.readline(4096)
                if line in (b"\r\n", b"\n", b""):
                    break
                if b":" in line:
                    key, value = line.split(b":", 1)
                    headers[key.decode("ascii", "ignore").strip().lower()] = value.decode("ascii", "ignore").strip()
            size = int(headers.get("content-length", "0"))
            if size < 0 or size > 65536:
                return self._reply(413, {"error": "request too large"})
            request = json.loads(stream.read(size).decode("utf-8"))
            if not isinstance(request, dict) or request.get("token") != self.server.controller.token:
                return self._reply(403, {"error": "authentication failed"})
            increments = _allowances(request.get("additional_proxy_allowances"))
            self.server.controller.queue(increments)
            self._reply(202, {"queued": True})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)})
        except OSError:
            return

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.request.sendall((f"HTTP/1.1 {status} {'Accepted' if status == 202 else 'Error'}\r\n"
                              f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                              "Connection: close\r\n\r\n").encode() + body)


class ProxyControl:
    def __init__(self, run_dir: str | Path, apply_callback: Callable[[dict[str, float | int]], Any]):
        self.run_dir = Path(run_dir).resolve()
        self.descriptor_path = self.run_dir / DESCRIPTOR
        if self.descriptor_path.exists():
            raise RuntimeError("proxy control descriptor already exists; refusing to replace an active owner")
        self.apply_callback = apply_callback
        self.token = secrets.token_urlsafe(32)
        self.owner_id = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, float | int]] = {}
        self._closed = False
        self._server = _Server(("127.0.0.1", 0), _Handler, self)
        self._thread = threading.Thread(target=self._server.serve_forever, name="proxy-control", daemon=True)
        self._thread.start()
        self.descriptor = {"schema": "artifact-proxy-control.v1", "host": "127.0.0.1",
                           "port": self._server.server_address[1], "token": self.token,
                           "owner_pid": os.getpid(), "owner_id": self.owner_id}
        try:
            _write_descriptor(self.descriptor_path, self.descriptor)
        except Exception:
            self.close()
            raise

    def queue(self, increments: dict[str, Any]) -> None:
        increments = _allowances(increments)
        with self._lock:
            if self._closed:
                raise RuntimeError("proxy control owner is closed")
            self._pending = merge_allowances(self._pending, increments)

    def apply_pending(self) -> Any:
        """Apply queued increments at a caller-controlled serialized boundary."""
        with self._lock:
            if self._closed:
                raise RuntimeError("proxy control owner is closed")
            if not self._pending:
                return None
            batch = {a: dict(f) for a, f in self._pending.items()}
            self._pending.clear()
            try:
                return self.apply_callback(callback_batch(batch))
            except Exception:
                self._pending = merge_allowances(self._pending, batch)
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        try:
            saved = json.loads(self.descriptor_path.read_text(encoding="utf-8"))
            if saved.get("owner_id") == self.owner_id:
                self.descriptor_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def start_control(run_dir: str | Path, apply_callback: Callable[[dict[str, float | int]], Any]) -> ProxyControl:
    """Start an authenticated local control endpoint owned by this executor."""
    if not callable(apply_callback):
        raise TypeError("apply_callback must be callable")
    return ProxyControl(run_dir, apply_callback)


def replenish_active(run_dir: str | Path, additional_proxy_allowances: dict[str, Any]) -> dict[str, Any]:
    """Queue auxiliary allowance increments to the already active executor."""
    increments = _allowances(additional_proxy_allowances)
    path = Path(run_dir).resolve() / DESCRIPTOR
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("no active proxy control owner (descriptor missing or stale)") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("proxy control descriptor is unreadable or stale") from exc
    if (descriptor.get("schema") != "artifact-proxy-control.v1" or descriptor.get("host") != "127.0.0.1"
            or type(descriptor.get("port")) is not int or not 1 <= descriptor["port"] <= 65535
            or type(descriptor.get("owner_pid")) is not int or not descriptor.get("owner_id")
            or not isinstance(descriptor.get("token"), str)):
        raise RuntimeError("proxy control descriptor is invalid or stale")
    try:
        os.kill(descriptor["owner_pid"], 0)
    except OSError as exc:
        raise RuntimeError("proxy control descriptor is stale: active executor owner is not running") from exc
    body = json.dumps({"token": descriptor["token"], "additional_proxy_allowances": increments}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{descriptor['port']}/replenish", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("proxy control descriptor is stale: active executor did not accept replenishment") from exc
    return {"status": "queued", **result}
