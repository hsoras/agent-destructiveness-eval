"""Durable lifecycle tracing for the pinned Inspect remote-execution server.

This module is copied over the matching module in inspect_sandbox_tools 1.2.1
inside the SQLite task image. Events go to evaluator-owned state storage, not the
bounded project filesystem.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
import importlib
import os
from pathlib import Path
import time
import uuid


_startup_id: str | None = None
_started_at: str | None = None
_event_log_path: str | None = None
_server_dir: str | None = None
PATCH_ID = "inspect-sandbox-tools-1.2.1-remote-exec-retry-trace-v2"


def start_service(server_dir: str | os.PathLike[str] | None = None) -> str:
    """Start a unique trace instance and persist the server identity."""
    global _startup_id, _started_at, _event_log_path, _server_dir
    _startup_id = uuid.uuid4().hex
    _started_at = datetime.now(timezone.utc).isoformat()
    _event_log_path = os.environ.get("INSPECT_SANDBOX_TOOLS_EVENT_LOG")
    _server_dir = str(server_dir) if server_dir is not None else None
    record_event(
        "service_start",
        startup_id=_startup_id,
        service_pid=os.getpid(),
        started_at=_started_at,
        proc_start_ticks=_proc_start_ticks(os.getpid()),
        server_dir=str(server_dir) if server_dir is not None else None,
        patch_id=PATCH_ID,
    )
    return _startup_id


def record_runtime_identity() -> None:
    """Record module paths and the exact controller source loaded by this daemon."""
    controller = importlib.import_module(
        "inspect_sandbox_tools._remote_tools._exec_remote._controller"
    )
    module_names = (
        "inspect_sandbox_tools",
        "inspect_sandbox_tools.lifecycle",
        "inspect_sandbox_tools._cli.server",
        "inspect_sandbox_tools._remote_tools._exec_remote._controller",
        "inspect_sandbox_tools._remote_tools._exec_remote._job",
    )
    module_paths = {}
    for name in module_names:
        module = importlib.import_module(name)
        module_paths[name] = str(Path(module.__file__).resolve())
    controller_path = Path(controller.__file__).resolve()
    controller_hash = hashlib.sha256(controller_path.read_bytes()).hexdigest()
    record_event(
        "service_runtime_identity",
        patch_id=PATCH_ID,
        module_paths=module_paths,
        controller_source_path=str(controller_path),
        controller_source_sha256=controller_hash,
        package_version=_package_version(),
    )


def _package_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("inspect-sandbox-tools")
    except Exception:
        return None


def record_event(event: str, **fields: object) -> None:
    """Append one JSONL event; logging errors never change remote job behavior."""
    path = _event_log_path or os.environ.get("INSPECT_SANDBOX_TOOLS_EVENT_LOG")
    if not path:
        return
    row: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "service_pid": os.getpid(),
        "startup_id": _startup_id,
        "service_started_at": _started_at,
        "server_dir": _server_dir,
        **fields,
    }
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            out.flush()
            os.fsync(out.fileno())
    except Exception as exc:  # keep evidence failures visible without breaking RPC
        try:
            import sys

            print(f"lifecycle trace write failed: {exc!r}", file=sys.stderr, flush=True)
        except Exception:
            pass


def record_missing_job(pid: int) -> None:
    """Persist process and memory evidence at the exact failed lookup."""
    fields: dict[str, object] = {"job_pid": pid}
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        fields["process_state"] = stat_text[stat_text.rfind(")") + 2 :].split()[0]
        fields["process_start_ticks"] = stat_text[stat_text.rfind(")") + 2 :].split()[19]
        fields["process_cmdline"] = Path(f"/proc/{pid}/cmdline").read_bytes().replace(
            b"\0", b" "
        ).decode("utf-8", errors="replace").strip()
    except (OSError, IndexError):
        fields["process_state"] = "absent"
    fields["cgroup_memory"] = _cgroup_memory_snapshot()
    fields["service_processes"] = _process_snapshot()
    record_event("job_lookup_missing", **fields)


def _cgroup_memory_snapshot() -> dict[str, str | None]:
    """Read cgroup v2 memory counters for this task container."""
    root = Path("/sys/fs/cgroup")
    return {
        name: (root / name).read_text().strip()
        if (root / name).is_file()
        else None
        for name in ("memory.current", "memory.max", "memory.events")
    }


def _process_snapshot() -> list[dict[str, str]]:
    """Capture process identifiers/states without command arguments."""
    rows: list[dict[str, str]] = []
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text()
            fields = raw[raw.rfind(")") + 2 :].split()
            rows.append(
                {
                    "pid": stat_path.parent.name,
                    "state": fields[0],
                    "ppid": fields[1],
                    "start_ticks": fields[19],
                }
            )
        except (OSError, IndexError):
            continue
    return rows


def _proc_start_ticks(pid: int) -> str | None:
    """Return Linux process start ticks, robust to spaces in comm."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2 :].split()[19]
    except (OSError, IndexError):
        return None
