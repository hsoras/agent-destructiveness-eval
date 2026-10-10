"""Protected append-only execution journal for the host-side broker."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate


class ExecutionJournal:
    """Persist the latest contracted snapshot for each stable execution key."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.event_path = self.path.with_name("tool_events.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.event_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)
        os.chmod(self.event_path, 0o600)
        self._lock = threading.RLock()
        self._records: dict[tuple[str, ...], dict[str, Any]] = {}
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                validate("journal", item)
                self._records[self._key(item["execution_key"])] = item

    def append_event(self, event: dict[str, Any]) -> None:
        """Durably append a validated contracted tool event outside guest storage."""
        validate("event", event)
        with self._lock:
            with self.event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(self.event_path, 0o600)

    @staticmethod
    def _key(key: dict[str, str]) -> tuple[str, ...]:
        return tuple(key[field] for field in ("run_id", "session_id", "call_id", "argument_digest"))

    def get(self, key: dict[str, str]) -> dict[str, Any] | None:
        with self._lock:
            item = self._records.get(self._key(key))
            return json.loads(json.dumps(item)) if item else None

    def by_call(self, run_id: str, session_id: str, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._records.values():
                key = item["execution_key"]
                if (key["run_id"], key["session_id"], key["call_id"]) == (run_id, session_id, call_id):
                    return json.loads(json.dumps(item))
        return None

    def put(self, key: dict[str, str], state: str, process: dict[str, str] | None = None,
            result: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            old = self._records.get(self._key(key))
            item = {"execution_key": dict(key), "state": state, "process": process,
                    "result": result, "revision": (old["revision"] + 1) if old else 1,
                    "updated_at": datetime.now(timezone.utc).isoformat()}
            validate("journal", item)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(self.path, 0o600)
            self._records[self._key(key)] = item
            return json.loads(json.dumps(item))
