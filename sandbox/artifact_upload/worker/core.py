"""Python 3.11 guest compatibility handlers for artifact-upload.v1."""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from eval.artifact_upload.contracts import (
    CONTRACT_VERSION, TOOL_OPERATIONS, argument_digest, validate,
)


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GuestWorker:
    """Execute contract calls against one fixed guest worktree.

    `root` and `worker_id` are provisioned by the container launcher; neither
    is exposed as an operation argument. This class does not create isolation:
    it must only be instantiated in the guest container.
    """

    def __init__(self, root: str | os.PathLike[str], *, worker_id: str = "guest-worker"):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("guest root must be a directory")
        self.worker_id = worker_id
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    @property
    def digest(self) -> str:
        """Digest implementation source files for result lineage."""
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob("*.py")):
            digest.update(source.name.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(source.read_bytes()).digest())
        return digest.hexdigest()

    def _path(self, name: str, *, create: bool = False) -> Path:
        if name.startswith("tool-output://"):
            raise WorkerError("unsupported_output_uri", "logical output references require the broker output resolver")
        raw = Path(name)
        candidate = raw if raw.is_absolute() else self.root / raw
        # Resolve existing symlinks with normal guest semantics, but prevent a
        # caller from escaping the fixed worktree through a path or symlink.
        resolved = candidate.resolve(strict=not create)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkerError("path_outside_guest", "path resolves outside the guest worktree") from exc
        return resolved

    @staticmethod
    def _text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            raise WorkerError("binary_file", "file is not valid UTF-8 text; use shell for binary data")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".artifact-worker-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, path.stat().st_mode & 0o777 if path.exists() else 0o644)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        """Validate and execute one v1 call, returning its correlated result."""
        try:
            validate("call", call)
        except Exception as exc:
            raise WorkerError("invalid_call", str(exc)) from exc
        operation = call["operation"]
        if TOOL_OPERATIONS.get(call["tool_name"]) != operation:
            raise WorkerError("invalid_call", "tool and operation do not match")
        try:
            if operation == "shell":
                out, err, code, encoding = self._shell(call)
                if call["call_id"] in self._cancelled:
                    status, error = "cancelled", "cancelled"
                    self._cancelled.discard(call["call_id"])
                else:
                    status, error = ("ok", None) if code == 0 else ("error", "command_failed")
            else:
                out = self._dispatch(operation, call["arguments"])
                err, code, status, error = "", 0, "ok", None
                encoding = "utf-8"
        except WorkerError as exc:
            out, err, code, status, error, encoding = "", str(exc), 1, "error", exc.code, "utf-8"
        except OSError as exc:
            out, err, code, status, error, encoding = "", str(exc), 1, "error", "filesystem_error", "utf-8"
        return {
            "contract_version": CONTRACT_VERSION, "run_id": call["run_id"],
            "session_id": call["session_id"], "call_id": call["call_id"],
            "argument_digest": call["argument_digest"], "status": status,
            "exit_code": code, "stdout": out, "stderr": err,
            "encoding": encoding, "error_code": error, "output_uri": None,
            "display_fields": {"aliases": {}, "changed_fields": []},
            "worker_digest": self.digest,
        }

    def cancel(self, call_id: str, *, how: str = "terminate") -> bool:
        """Signal the process group for a running shell call."""
        signals = {"interrupt": signal.SIGINT, "terminate": signal.SIGTERM, "kill": signal.SIGKILL}
        if how not in signals:
            raise ValueError("signal must be interrupt, terminate, or kill")
        with self._lock:
            proc = self._processes.get(call_id)
        if proc is None or proc.poll() is not None:
            return False
        try:
            os.killpg(proc.pid, signals[how])
        except ProcessLookupError:
            return False
        with self._lock:
            self._cancelled.add(call_id)
        return True

    def _shell(self, call: dict[str, Any]) -> tuple[str, str, int, str]:
        args = call["arguments"]
        cwd = self._path(args.get("workdir", "."))
        if not cwd.is_dir():
            raise WorkerError("not_a_directory", f"not a directory: {args.get('workdir', '.')}")
        env = os.environ.copy()
        env.update(args.get("env", {}))
        proc = subprocess.Popen(
            ["/bin/sh", "-c", args["command"]], cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with self._lock:
            self._processes[call["call_id"]] = proc
        try:
            timeout = args.get("timeout")
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    stdout, stderr = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate()
                raise WorkerError("timeout", "command timed out")
        finally:
            with self._lock:
                self._processes.pop(call["call_id"], None)
        try:
            return stdout.decode("utf-8"), stderr.decode("utf-8"), proc.returncode, "utf-8"
        except UnicodeDecodeError:
            # Preserve arbitrary process output without corrupting bytes.
            return base64.b64encode(stdout).decode(), base64.b64encode(stderr).decode(), proc.returncode, "base64"

    def _dispatch(self, op: str, a: dict[str, Any]) -> str:
        if op == "read":
            path = self._path(a["filePath"])
            data = path.read_bytes()
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise WorkerError("binary_file", "file is not valid UTF-8 text; use shell for binary data")
            lines = text.splitlines(keepends=True)
            start = a.get("offset", 0)
            selected = lines[start:] if "limit" not in a else lines[start:start + a["limit"]]
            return "".join(selected)
        if op == "write":
            path = self._path(a["filePath"], create=True)
            self._atomic_write(path, a["content"].encode("utf-8"))
            return ""
        if op == "edit":
            path = self._path(a["filePath"])
            old, new = a["oldString"], a["newString"]
            if not old:
                raise WorkerError("edit_failed", "oldString must not be empty")
            data = self._text(path)
            count = data.count(old)
            replace_all = a.get("replaceAll", False)
            if count == 0 or (count > 1 and not replace_all):
                raise WorkerError("edit_failed", f"expected one occurrence of oldString; found {count}")
            changed = data.replace(old, new) if replace_all else data.replace(old, new, 1)
            self._atomic_write(path, changed.encode("utf-8"))
            return ""
        if op == "patch":
            return self._patch(a["patchText"])
        if op == "find":
            base = self._path(a.get("path", "."))
            if base.is_file():
                candidates = [base]
            else:
                candidates = sorted(p for p in base.rglob("*") if p.is_file())
            return "\n".join(str(p.relative_to(self.root)) for p in candidates
                             if fnmatch.fnmatch(p.name, a["pattern"]))
        if op == "grep":
            base = self._path(a.get("path", "."))
            candidates = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
            include = a.get("include", "*")
            try:
                pattern = re.compile(a["pattern"])
            except re.error as exc:
                raise WorkerError("invalid_pattern", str(exc)) from exc
            matches = []
            for path in candidates:
                if not fnmatch.fnmatch(path.name, include):
                    continue
                try:
                    contents = path.read_text(encoding="utf-8-sig")
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(contents.splitlines(), 1):
                    if pattern.search(line):
                        matches.append(f"{path.relative_to(self.root)}:{number}:{line}")
            return "\n".join(matches)
        raise WorkerError("unsupported_operation", f"unsupported operation: {op}")

    def _patch(self, patch: str) -> str:
        """Apply the OpenAI-style Begin/End Patch subset atomically per file."""
        lines = patch.splitlines()
        if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise WorkerError("patch_failed", "invalid patch: expected Begin Patch / End Patch")
        changes: list[tuple[Path, bytes]] = []
        i = 1
        while i < len(lines) - 1:
            head = lines[i]
            if head.startswith("*** Update File: "):
                path = self._path(head[len("*** Update File: "):])
                i += 1
                original_bytes = path.read_bytes()
                try:
                    current = original_bytes.decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise WorkerError("patch_failed", f"binary file cannot be patched: {path.name}")
                hunks = []
                while i < len(lines) - 1 and not lines[i].startswith("*** "):
                    if lines[i].startswith("@@"):
                        i += 1
                        chunk = []
                        while i < len(lines) - 1 and not lines[i].startswith(("@@", "*** ")):
                            chunk.append(lines[i]); i += 1
                        hunks.append(chunk)
                    else:
                        raise WorkerError("patch_failed", f"unexpected patch line: {lines[i]}")
                for chunk in hunks:
                    before = "\n".join(x[1:] for x in chunk if x.startswith((" ", "-")))
                    after = "\n".join(x[1:] for x in chunk if x.startswith((" ", "+")))
                    if not before or before not in current:
                        raise WorkerError("patch_failed", "patch context did not match")
                    if current.count(before) != 1:
                        raise WorkerError("patch_failed", "patch context is ambiguous")
                    current = current.replace(before, after, 1)
                changes.append((path, current.encode("utf-8")))
            elif head.startswith("*** Delete File: "):
                path = self._path(head[len("*** Delete File: "):])
                changes.append((path, b""))
                i += 1
            elif head.startswith("*** Add File: "):
                path = self._path(head[len("*** Add File: "):], create=True)
                i += 1; added = []
                while i < len(lines) - 1 and not lines[i].startswith("*** "):
                    if not lines[i].startswith("+"):
                        raise WorkerError("patch_failed", "added file lines must start with +")
                    added.append(lines[i][1:]); i += 1
                if path.exists():
                    raise WorkerError("patch_failed", f"file already exists: {path.name}")
                changes.append((path, ("\n".join(added) + "\n").encode()))
            else:
                raise WorkerError("patch_failed", f"unsupported patch directive: {head}")
        # Validate the entire patch before any filesystem mutation.
        for path, _ in changes:
            if path.exists() and not os.access(path, os.W_OK):
                raise WorkerError("permission_denied", f"permission denied: {path.name}")
        for path, contents in changes:
            if contents == b"" and path.exists() and any(x.startswith("*** Delete File: " + str(path.relative_to(self.root))) for x in lines):
                path.unlink()
            else:
                self._atomic_write(path, contents)
        return ""
