"""Python guest reimplementation of the pinned OpenCode core handlers.

This module is installed only in the disposable guest.  Calls are data; the only
subprocess entry points are the guest shell started with argv (never a host
shell string) and ripgrep started with argv.  File handlers live in ``tools.py``
and port the pinned OpenCode 1.14.41 semantics; this module owns the call
contract, the declared path boundary and process lifetime ownership.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from eval.artifact_upload.contracts import CONTRACT_VERSION, TOOL_OPERATIONS, validate

from . import tools
from .tools import Ctx, ToolError

DEFAULT_SHELL_TIMEOUT_MS = 2 * 60 * 1000  # native tool/shell.ts DEFAULT_TIMEOUT
KEY_FIELDS = ("run_id", "session_id", "call_id", "argument_digest")
DISABLED_FEATURES = tools.DISABLED_FEATURES


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message); self.code = code


def _proc_table() -> list[tuple[int, int, int, str, str]]:
    """(pid, ppid, pgid, state, start-marker) for every visible process."""
    rows: list[tuple[int, int, int, str, str]] = []
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit(): continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f: raw = f.read().decode("utf-8", "replace")
                rest = raw[raw.rindex(")") + 2:].split()
                rows.append((int(name), int(rest[1]), int(rest[2]), rest[0], rest[19]))
            except (OSError, ValueError, IndexError):
                continue
        return rows
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return rows
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], ""))
    return rows


def group_members(pgid: int) -> list[int]:
    return [pid for pid, _pp, pg, st, _s in _proc_table() if pg == pgid and not st.startswith("Z")]


def _proc_start(pid: int) -> str | None:
    """Start time of a live, non-zombie process; (pid, start time) names one process even after pid reuse."""
    if not os.path.isdir("/proc/self"):  # non-Linux host (direct in-process use): ps reports the same identity
        try: out = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.SubprocessError): return None
        return None if not out or out[0].startswith("Z") else " ".join(out[1:])
    try:
        with open(f"/proc/{pid}/stat", "rb") as f: raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    rest = raw[raw.rindex(")") + 2:].split()
    return None if rest[0] == "Z" else rest[19]


def _is_owned(pid: int, start: str | None) -> bool:
    return start is not None and _proc_start(pid) == start


class _Ledger:
    """Run-private guest record of shell executions, so a fresh worker session can see what a dead session started.

    Per execution: an identity file (exact key, process handle, leader and member process identities; replaced
    atomically as members appear) and, once the outcome is known, one exclusively linked result file.
    Files are named by a digest of the full key and hold the key, so a digest collision cannot alias two executions.
    """

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)

    def _path(self, key: tuple[str, str, str, str], suffix: str) -> str:
        return os.path.join(self.root, hashlib.sha256(json.dumps(list(key)).encode()).hexdigest() + suffix)

    def _read(self, key: tuple[str, str, str, str], suffix: str) -> dict[str, Any] | None:
        try:
            with open(self._path(key, suffix), "rb") as f: return json.loads(f.read())
        except FileNotFoundError:
            return None

    def identity(self, key: tuple[str, str, str, str]) -> dict[str, Any] | None:
        rec = self._read(key, ".json")
        return rec if rec is not None and rec.get("key") == list(key) else None

    def result(self, key: tuple[str, str, str, str]) -> dict[str, Any] | None:
        return self._read(key, ".result.json")

    def write_identity(self, key: tuple[str, str, str, str], record: dict[str, Any]) -> None:
        path = self._path(key, ".json")
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(record, f)
        os.replace(tmp, path)

    def record_result_once(self, key: tuple[str, str, str, str], result: dict[str, Any]) -> dict[str, Any]:
        """Keep the first terminal result for this key; a link is an exclusive create that never exposes partial content."""
        path = self._path(key, ".result.json")
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(result, f)
        try:
            os.link(tmp, path)
            return result
        except FileExistsError:
            return self.result(key)  # type: ignore[return-value]
        finally:
            os.unlink(tmp)


def select_shell() -> str:
    env_shell = os.environ.get("SHELL")
    if env_shell and os.path.exists(env_shell) and os.path.basename(env_shell) not in ("fish", "nu"):
        return env_shell
    return shutil.which("bash") or "/bin/sh"


class _Stream:
    """Bounded byte accumulation with a full-output spool, per stream."""

    def __init__(self, spool_dir: str):
        self.tail = bytearray(); self.total = 0; self.cut = False; self.spool_dir = spool_dir
        self.sink = None; self.path = ""

    def _open_spool(self, initial: bytes) -> None:
        os.makedirs(self.spool_dir, exist_ok=True)
        self.path = os.path.join(self.spool_dir, "tool_" + uuid.uuid4().hex)
        self.sink = open(self.path, "wb"); self.sink.write(initial)

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        if self.sink is not None:
            self.sink.write(data)
        elif self.total > tools.MAX_BYTES:
            self._open_spool(bytes(self.tail) + data); self.cut = True
        self.tail += data
        keep = tools.MAX_BYTES * 2
        if len(self.tail) > keep:
            del self.tail[: len(self.tail) - keep]; self.cut = True

    def close(self) -> None:
        if self.sink is not None:
            self.sink.close(); self.sink = None

    def render(self) -> bytes:
        raw = bytes(self.tail)
        text = raw
        lines = raw.split(b"\n")
        if not (len(lines) <= tools.MAX_LINES and len(raw) <= tools.MAX_BYTES):
            out: list[bytes] = []; size = 0
            for i in range(len(lines) - 1, -1, -1):
                if len(out) >= tools.MAX_LINES: break
                add = len(lines[i]) + (1 if out else 0)
                if size + add > tools.MAX_BYTES:
                    if not out:
                        start = max(len(lines[i]) - tools.MAX_BYTES, 0)
                        while start < len(lines[i]) and (lines[i][start] & 0xC0) == 0x80: start += 1
                        out.insert(0, lines[i][start:])
                    break
                out.insert(0, lines[i]); size += add
            text = b"\n".join(out); self.cut = True
            if not self.path:
                self._open_spool(raw); self.close()
        if self.cut and self.path:
            text = b"...output truncated...\n\nFull output saved to: " + self.path.encode() + b"\n\n" + text
        return text


class _Execution:
    def __init__(self, key: tuple[str, str, str, str], proc: Any, worker_id: str, stream: bool, spool_dir: str):
        self.key, self.proc, self.pgid = key, proc, proc.pid
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.handle = {"worker_id": worker_id, "process_id": str(proc.pid), "process_group_id": str(proc.pid), "started_at": self.started_at}
        self.events: queue.Queue[dict[str, Any]] | None = queue.Queue(maxsize=4096) if stream else None
        self.dropped = 0
        self.out, self.err = _Stream(spool_dir), _Stream(spool_dir)
        self.cancel_signal: int | None = None; self.cancel_at = 0.0
        self.expired = False; self.done = threading.Event(); self.result: dict[str, Any] | None = None
        self.tracked: dict[int, str] = {}
        # Durable identity: the leader is the process group; members are its tracked descendants (any group).
        self.record: dict[str, Any] = {"key": list(key), "handle": self.handle, "leader": [proc.pid, _proc_start(proc.pid)], "members": {}}

    def emit(self, event: dict[str, Any]) -> None:
        if self.events is None: return
        try: self.events.put_nowait(event)
        except queue.Full: self.dropped += 1


class _NoProc:
    """Popen stand-in for an execution that was rejected before spawning."""
    pid = 0
    returncode = 1


class GuestWorker:
    """Handlers sharing one guest filesystem and one fixed worker identity."""

    def __init__(self, root: str | os.PathLike[str], *, worker_id: str = "guest-worker", protected_paths: tuple[str, ...] | list[str] = (),
                 output_dir: str | os.PathLike[str] | None = None, kill_grace: float = 3.0, state_dir: str | os.PathLike[str] | None = None):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir(): raise ValueError("guest root must be a directory")
        self.worker_id = worker_id
        self.kill_grace = kill_grace
        self.output_dir = str(output_dir) if output_dir else os.path.join(tempfile.gettempdir(), f"artifact-worker-output-{os.getuid()}")
        self.protected = tuple(os.path.realpath(p) for p in protected_paths)
        self.rg = shutil.which("rg")
        self._lock = threading.RLock()
        self._execs: dict[tuple[str, str, str, str], _Execution] = {}
        self._finished: set[tuple[str, str, str, str]] = set()
        # Without an explicit run-private state dir the ledger is ephemeral (direct in-process use only).
        self.ledger = _Ledger(str(state_dir) if state_dir else tempfile.mkdtemp(prefix="artifact-worker-state-"))

    # ------------------------------------------------------------ identity
    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for p in sorted(Path(__file__).parent.glob("*.py")):
            h.update(p.name.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
        return h.hexdigest()

    def capabilities(self) -> dict[str, Any]:
        return {"operations": sorted(TOOL_OPERATIONS.values()), "disabled": list(DISABLED_FEATURES), "persistent_sessions": False, "background_jobs": False}

    @staticmethod
    def _key(c: dict[str, Any]) -> tuple[str, str, str, str]:
        return c["run_id"], c["session_id"], c["call_id"], c["argument_digest"]

    @staticmethod
    def _exec_key(k: Any) -> tuple[str, str, str, str]:
        if not isinstance(k, dict) or set(k) != set(KEY_FIELDS) or any(not isinstance(k[f], str) or not k[f] for f in KEY_FIELDS):
            raise WorkerError("invalid_execution_key", "complete execution_key (run_id, session_id, call_id, argument_digest) is required")
        return tuple(k[f] for f in KEY_FIELDS)  # type: ignore[return-value]

    # ------------------------------------------------------------ boundary
    def check_path(self, path: str) -> None:
        """Reject any path whose real target lies in a protected guest location.

        Applied to every handler path argument and to every traversal candidate
        returned by glob/grep, after symlink resolution.
        """
        real = os.path.realpath(path)
        for p in self.protected:
            if real == p or real.startswith(p.rstrip(os.sep) + os.sep):
                raise ToolError(f"path is outside the guest boundary: {path}", "boundary_violation")

    def _ctx(self) -> Ctx:
        return Ctx(str(self.root), self.check_path, self.output_dir, self.rg)

    # ------------------------------------------------------------- execute
    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        op = self._validated(call)
        if op == "shell":
            ex = self._launch(call, stream=False)
            ex.done.wait()
            assert ex.result is not None
            with self._lock:
                self._execs.pop(ex.key, None); self._finished.add(ex.key)
            return ex.result
        try:
            out = self._dispatch(op, call["arguments"])
            return self._result(call, "ok", 0, out, "", "utf-8", None)
        except ToolError as e:
            return self._result(call, "unsupported" if e.code == "unsupported_attachment" else "error", 1, "", str(e), "utf-8", e.code)
        except OSError as e:
            return self._result(call, "error", 1, "", str(e), "utf-8", "filesystem_error")

    def _validated(self, call: dict[str, Any]) -> str:
        try: validate("call", call)
        except Exception as e: raise WorkerError("invalid_call", str(e)) from e
        op = call["operation"]
        if TOOL_OPERATIONS.get(call["tool_name"]) != op: raise WorkerError("invalid_call", "tool and operation do not match")
        return op

    def _result(self, call, status, code, out, err, enc, error) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "run_id": call["run_id"], "session_id": call["session_id"], "call_id": call["call_id"],
                "argument_digest": call["argument_digest"], "status": status, "exit_code": code, "stdout": out, "stderr": err, "encoding": enc,
                "error_code": error, "output_uri": None, "display_fields": {"aliases": {}, "changed_fields": []}, "worker_digest": self.digest}

    def _dispatch(self, op: str, a: dict[str, Any]) -> str:
        ctx = self._ctx()
        table = {"read": tools.read, "write": tools.write, "edit": tools.edit, "patch": tools.apply_patch, "find": tools.glob, "grep": tools.grep}
        if op not in table: raise WorkerError("unsupported_operation", f"unsupported operation: {op}")
        out = table[op](ctx, a)
        return out if op == "read" else tools.truncate_output(ctx, out)

    # ----------------------------------------------------- process handles
    def process_handle(self, call: dict[str, Any]) -> dict[str, str] | None:
        """Handle for this exact execution key until its stream/result is consumed."""
        with self._lock:
            ex = self._execs.get(self._key(call))
            return dict(ex.handle) if ex else None

    def start(self, call: dict[str, Any]) -> dict[str, str]:
        """Start a shell execution; the handle is published before returning."""
        if self._validated(call) != "shell": raise WorkerError("unsupported_operation", "only shell has a process handle")
        return dict(self._launch(call, stream=True).handle)

    def stream(self, execution_key: dict[str, str]) -> Iterator[dict[str, Any]]:
        """Yield stdout/stderr chunk events in arrival order, then one result event."""
        key = self._exec_key(execution_key)
        with self._lock: ex = self._execs.get(key)
        if ex is None:
            result = self._recovered(key)[1]
            if result is None: raise WorkerError("unknown_execution", "execution is not complete")
            yield {"type": "result", "result": result}
            return
        if ex.events is None: raise WorkerError("unknown_execution", "execution is not active")
        while True:
            item = ex.events.get()
            yield item
            if item["type"] == "result":
                with self._lock:
                    self._execs.pop(key, None); self._finished.add(key)
                return

    def status(self, execution_key: dict[str, str]) -> dict[str, Any]:
        key = self._exec_key(execution_key)
        with self._lock: ex = self._execs.get(key)
        if ex is None:
            rec, result = self._recovered(key)
            return {"state": "running" if result is None else "completed", "result": result, "process": dict(rec["handle"])}
        return {"state": "completed" if ex.done.is_set() else "running", "result": ex.result, "process": dict(ex.handle)}

    def cancel(self, execution_key: dict[str, str] | str, *, how: str = "terminate") -> bool:
        """Signal the whole process group of the exact execution; False if not active."""
        key = self._exec_key(execution_key)
        sig = {"interrupt": signal.SIGINT, "terminate": signal.SIGTERM, "kill": signal.SIGKILL}.get(how)
        if sig is None: raise ValueError("signal must be interrupt, terminate, or kill")
        with self._lock: ex = self._execs.get(key)
        if ex is None: return self._cancel_recovered(key, sig)
        if ex.done.is_set(): return False
        ex.cancel_signal = sig; ex.cancel_at = time.monotonic()
        self._signal_group(ex, sig)
        return True

    # ------------------------------------------------- recovered executions
    # An execution started by an earlier worker session is found through its ledger identity. Its process
    # identities (leader, tracked descendants) are compared by start time, so a reused pid is never signalled.
    def _owned_alive(self, rec: dict[str, Any]) -> bool:
        pid, start = rec["leader"]
        return _is_owned(pid, start) or any(_is_owned(int(p), s) for p, s in rec["members"].items())

    def _recovered(self, key: tuple[str, str, str, str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Identity and terminal result of an execution this session did not start; result is None while running."""
        with self._lock:
            if key in self._finished: raise WorkerError("unknown_execution", "execution is not active")  # consumed here: forgotten
        rec = self.ledger.identity(key)
        if rec is None: raise WorkerError("unknown_execution", "execution is not active")
        result = self.ledger.result(key)
        if result is None and not self._owned_alive(rec):
            # Processes are gone but nothing recorded their exit: report that honestly, not a fabricated status.
            result = self.ledger.record_result_once(key, self._unobserved(key, "worker session ended before this execution's exit was observed"))
        return rec, result

    def _cancel_recovered(self, key: tuple[str, str, str, str], sig: int) -> bool:
        rec = self.ledger.identity(key)
        if rec is None or self.ledger.result(key) is not None or not self._owned_alive(rec): return False
        self._terminate_owned(rec, sig)
        if not self._owned_alive(rec):
            self.ledger.record_result_once(key, self._unobserved(key, "terminated after worker session loss; exit not observed"))
        return True

    def _terminate_owned(self, rec: dict[str, Any], sig: int) -> None:
        """Signal the recorded group (only while its leader is still the recorded process) and members; escalate after grace."""
        for step, wait in ((sig, self.kill_grace), (signal.SIGKILL, 5.0)):
            pid, start = rec["leader"]
            if _is_owned(pid, start) and pid > 1:
                try: os.killpg(pid, step)
                except OSError: pass
            for member, mstart in rec["members"].items():
                if _is_owned(int(member), mstart):
                    try: os.kill(int(member), step)
                    except OSError: pass
            deadline = time.monotonic() + wait
            while self._owned_alive(rec) and time.monotonic() < deadline: time.sleep(0.02)
            if not self._owned_alive(rec): return

    def _unobserved(self, key: tuple[str, str, str, str], message: str) -> dict[str, Any]:
        """Unknown outcome for an execution whose completion was not observed: no exit status is fabricated."""
        return self._result(dict(zip(KEY_FIELDS, key)), "unknown", None, "", message, "utf-8", "outcome_unobserved")

    def shutdown(self) -> None:
        """Graceful session end (stdin EOF, SIGTERM or SIGHUP): terminate this session's live executions and their groups."""
        with self._lock: live = [ex for ex in self._execs.values() if not ex.done.is_set()]
        for ex in live:
            ex.cancel_signal = ex.cancel_signal or signal.SIGTERM; ex.cancel_at = time.monotonic()
            self._signal_group(ex, signal.SIGTERM)
        for ex in live: ex.done.wait(self.kill_grace + 10)

    # ------------------------------------------------------------- shell
    def _launch(self, call: dict[str, Any], *, stream: bool) -> _Execution:
        a = call["arguments"]; key = self._key(call)
        cwd = os.path.normpath(os.path.join(str(self.root), a["workdir"])) if a.get("workdir") else str(self.root)
        try: self.check_path(cwd)
        except ToolError as e: return self._failed(call, key, str(e), e.code, stream)
        if not os.path.isdir(cwd): return self._failed(call, key, f"not a directory: {a.get('workdir', '.')}", "not_a_directory", stream)
        env = os.environ.copy(); env.update(a.get("env", {}))
        with self._lock:
            if key in self._execs or key in self._finished or self.ledger.identity(key) is not None:
                raise WorkerError("duplicate_execution", "execution key was already used")
            proc = subprocess.Popen([select_shell(), "-c", a["command"]], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            ex = _Execution(key, proc, self.worker_id, stream, self.output_dir)
            self._execs[key] = ex
            # Durable before the handle is returned: a session that dies after this point leaves a recoverable record.
            try: self.ledger.write_identity(key, ex.record)
            except OSError as exc:
                self._signal_group(ex, signal.SIGKILL); self._execs.pop(key, None)
                raise WorkerError("ledger_unavailable", f"execution identity could not be recorded: {exc}") from exc
        threading.Thread(target=self._supervise, args=(call, ex, a.get("timeout", DEFAULT_SHELL_TIMEOUT_MS)), name="artifact-worker-shell", daemon=True).start()
        return ex

    def _failed(self, call, key, message, code, stream) -> _Execution:
        with self._lock:
            if key in self._execs or key in self._finished: raise WorkerError("duplicate_execution", "execution key was already used")
            ex = _Execution(key, _NoProc(), self.worker_id, stream, self.output_dir)
            ex.result = self._result(call, "error", 1, "", message, "utf-8", code)
            ex.done.set(); self._execs[key] = ex
        ex.emit({"type": "result", "result": ex.result})
        return ex

    def _supervise(self, call: dict[str, Any], ex: _Execution, timeout_ms: int) -> None:
        proc = ex.proc

        def drain(name: str, sink: _Stream, pipe) -> None:
            fd = pipe.fileno()
            while True:
                try: data = os.read(fd, 65536)
                except OSError: break
                if not data: break
                sink.feed(data)
                ex.emit({"type": name, "data_base64": base64.b64encode(data).decode("ascii")})

        readers = [threading.Thread(target=drain, args=("stdout", ex.out, proc.stdout), daemon=True), threading.Thread(target=drain, args=("stderr", ex.err, proc.stderr), daemon=True)]
        for r in readers: r.start()
        deadline = time.monotonic() + (timeout_ms + 100) / 1000
        term_at = 0.0; escalated = False
        try:
            while proc.poll() is None:
                now = time.monotonic()
                self._track(ex)
                if not ex.expired and now >= deadline:
                    ex.expired = True; term_at = now
                    self._signal_group(ex, signal.SIGTERM)
                if not term_at and ex.cancel_signal is not None and ex.cancel_signal != signal.SIGKILL:
                    term_at = ex.cancel_at
                if term_at and not escalated and now - term_at >= self.kill_grace:
                    escalated = True; self._signal_group(ex, signal.SIGKILL)
                time.sleep(0.02)
            # The shell has exited: its group (and descendants that left the group)
            # must not outlive it.  Sessions/background jobs are disabled by design.
            self._reap_group(ex)
            for r in readers: r.join(5)
            for pipe in (proc.stdout, proc.stderr):
                try: pipe.close()
                except OSError: pass
        finally:
            ex.out.close(); ex.err.close()
        ex.result = self._finish(call, ex)
        try: self.ledger.record_result_once(ex.key, ex.result)
        except OSError: pass  # live delivery below still succeeds; only cross-session recovery is lost
        ex.done.set()
        if ex.dropped: ex.emit({"type": "dropped", "count": ex.dropped})
        if ex.events is not None: ex.events.put({"type": "result", "result": ex.result})

    @staticmethod
    def _signal_group(ex: _Execution, sig: int) -> None:
        if ex.pgid <= 1: return
        try: os.killpg(ex.pgid, sig)
        except ProcessLookupError: pass

    def _track(self, ex: _Execution) -> None:
        table = _proc_table(); children: dict[int, list[tuple[int, str]]] = {}
        for pid, ppid, _pg, _st, start in table: children.setdefault(ppid, []).append((pid, start))
        todo = [ex.pgid]; before = len(ex.tracked)
        while todo:
            for pid, start in children.get(todo.pop(), []):
                if pid not in ex.tracked: ex.tracked[pid] = start; todo.append(pid)
        if len(ex.tracked) != before:
            # Descendants that leave the group (setsid, nohup) are only recoverable if their identity is recorded now.
            # Best effort: a lost write must not stop supervision, which still delivers the live result.
            ex.record["members"] = {str(pid): start for pid, start in ex.tracked.items()}
            try: self.ledger.write_identity(ex.key, ex.record)
            except OSError: pass

    def _reap_group(self, ex: _Execution) -> None:
        for sig, wait in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 5.0)):
            self._signal_group(ex, sig)
            for pid in self._tracked_alive(ex):
                try: os.kill(pid, sig)
                except ProcessLookupError: pass
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if not group_members(ex.pgid) and not self._tracked_alive(ex): return
                time.sleep(0.02)

    @staticmethod
    def _tracked_alive(ex: _Execution) -> list[int]:
        alive = {pid: start for pid, _pp, _pg, st, start in _proc_table() if not st.startswith("Z")}
        return [pid for pid, start in ex.tracked.items() if pid in alive and alive[pid] == start]

    def _finish(self, call: dict[str, Any], ex: _Execution) -> dict[str, Any]:
        out_b, err_b = ex.out.render(), ex.err.render()
        code = ex.proc.returncode
        meta: list[str] = []
        if ex.expired:
            meta.append(f"shell tool terminated command after exceeding timeout {call['arguments'].get('timeout', DEFAULT_SHELL_TIMEOUT_MS)} ms. If this command is expected to take longer and is not waiting for interactive input, retry with a larger timeout value in milliseconds.")
        cancelled = ex.cancel_signal is not None and not ex.expired
        if cancelled: meta.append("User aborted the command")
        suffix = ("\n\n<shell_metadata>\n" + "\n".join(meta) + "\n</shell_metadata>") if meta else ""
        if ex.expired: status, error = "error", "timeout"
        elif cancelled: status, error = "cancelled", "cancelled"
        elif code == 0: status, error = "ok", None
        else: status, error = "error", "command_failed"
        try:
            return self._result(call, status, code, out_b.decode() + suffix, err_b.decode(), "utf-8", error)
        except UnicodeDecodeError:
            return self._result(call, status, code, base64.b64encode(out_b + suffix.encode()).decode(), base64.b64encode(err_b).decode(), "base64", error)
