"""Durable snapshots of the private native OpenCode session store.

The store is SQLite under the adapter's private ``<stage>/data`` and
``<stage>/state`` roots. A snapshot is an online ``sqlite3`` backup plus tar
archives of the remaining private session files, with hashes and a semantic
witness of the session rows. Restoring bytes is not the same as cold-resuming
the model loop: that needs the pinned runtime and a quiescent turn boundary,
and both are reported as explicit prerequisites rather than assumed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
from typing import Any, Mapping

# Boundaries (continuation design 2): T turn boundary, P mid-turn provider
# boundary, M mid-step. Anything else is unclassified and never resumable.
FAITHFUL_RESUME = {"T"}
BOUNDARY_BLOCKERS = {
    "P": ("native-loop-resume-entrypoint",
          "pinned runtime has no entry point that re-enters the model loop without adding conversation content"),
    "M": ("mid-step-tool-state",
          "a tool call was held or running; its native tool-part state cannot be restored faithfully"),
}
SEMANTIC_TABLES = {"session": "id", "message": "session_id", "part": "session_id", "todo": "session_id"}


class NativeSessionError(RuntimeError):
    """Snapshot or restore integrity failure."""


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_boundary(*, open_tool_calls: int = 0, held_calls: int = 0, dispatches_since_turn_start: int | None = None,
                      at_turn_boundary: bool = False) -> str:
    """Classify a stop. Unknown inputs never classify as the faithful boundary."""
    if open_tool_calls or held_calls:
        return "M"
    if at_turn_boundary:
        return "T"
    if dispatches_since_turn_start is None:
        return "unclassified"
    return "P" if dispatches_since_turn_start else "T"


class NativeSessionStore:
    """Snapshot, verify and restore one run's private native session storage."""

    def __init__(self, stage_root: str | Path, *, db_path: str | Path | None = None,
                 runtime: Any = None, runtime_sha256: str | None = None, runtime_version: str | None = None):
        self.stage = Path(stage_root).resolve()
        self._db_override = Path(db_path) if db_path else None
        self.runtime = runtime
        self.runtime_sha256, self.runtime_version = runtime_sha256, runtime_version

    @classmethod
    def from_adapter(cls, adapter: Any) -> "NativeSessionStore":
        from . import host_adapter
        flags = getattr(adapter, "runtime_flags", {}) or {}
        override = flags.get("OPENCODE_DB")
        runtime = getattr(adapter, "runtime", None)
        from .runtime_artifacts import DevelopmentRuntime
        if isinstance(runtime, DevelopmentRuntime):
            runtime.verify()
            return cls(adapter.stage, db_path=override, runtime=runtime,
                       runtime_sha256=runtime.expected_sha256, runtime_version=runtime.version)
        return cls(adapter.stage, db_path=override, runtime=runtime,
                   runtime_sha256=host_adapter.RUNTIME_SHA256, runtime_version=host_adapter.RUNTIME_VERSION)

    # -- locations ------------------------------------------------------------
    def db_path(self) -> Path:
        if self._db_override:
            return self._db_override
        matches = sorted((self.stage / "data" / "opencode").glob("opencode*.db"))
        if len(matches) != 1:
            raise NativeSessionError(f"expected exactly one native session database, found {len(matches)}")
        return matches[0]

    def _archive_roots(self) -> list[tuple[str, Path]]:
        db = self.db_path()
        roots = [("state", self.stage / "state"), ("data-opencode", self.stage / "data" / "opencode")]
        return [(name, root) for name, root in roots if root.is_dir()], db

    # -- runtime capability ---------------------------------------------------
    def cold_resume_status(self) -> dict[str, Any]:
        """Whether the exact pinned native binary can cold-resume a restored session."""
        binary = getattr(self.runtime, "binary", None)
        if binary is None or not Path(binary).is_file():
            return {"available": False, "name": "pinned-native-runtime",
                    "reason": "pinned OpenCode binary is absent, so cold resume cannot be validated"}
        if self.runtime_sha256 and _sha_file(Path(binary)) != self.runtime_sha256:
            return {"available": False, "name": "pinned-native-runtime",
                    "reason": "OpenCode binary digest differs from the pinned runtime"}
        return {"available": True, "name": "pinned-native-runtime", "reason": None,
                "version": self.runtime_version, "sha256": self.runtime_sha256}

    def resume_assessment(self, boundary: str) -> dict[str, Any]:
        """Truthful per-boundary resume verdict: ``blockers`` empty means resumable."""
        blockers = []
        if boundary in BOUNDARY_BLOCKERS:
            name, reason = BOUNDARY_BLOCKERS[boundary]
            blockers.append({"name": name, "available": False, "reason": reason})
        elif boundary not in FAITHFUL_RESUME:
            blockers.append({"name": "boundary-unclassified", "available": False,
                             "reason": "stop point was not classified as a turn boundary, so fidelity is unproven"})
        runtime = self.cold_resume_status()
        if not runtime["available"]:
            blockers.append({k: runtime[k] for k in ("name", "available", "reason")})
        return {"boundary": boundary, "faithful": boundary in FAITHFUL_RESUME, "blockers": blockers}

    # -- snapshot -------------------------------------------------------------
    def _semantic_digest(self, db: Path, session_id: str) -> dict[str, Any]:
        # Snapshot copies are never written; immutable avoids creating -wal/-shm beside them.
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
            witness: dict[str, Any] = {}
            for table, column in SEMANTIC_TABLES.items():
                if table not in tables:
                    continue
                columns = [r[1] for r in conn.execute(f'pragma table_info("{table}")')]
                if column not in columns:
                    continue
                rows = conn.execute(f'select * from "{table}" where "{column}"=? order by 1', (session_id,)).fetchall()
                witness[table] = {"rows": len(rows), "sha256": hashlib.sha256(
                    json.dumps(rows, default=repr, sort_keys=True).encode()).hexdigest()}
            return {"session_found": bool(witness.get("session", {}).get("rows")), "tables": witness}
        finally:
            conn.close()

    def snapshot(self, dest: str | Path, session_id: str, *, boundary: str = "unclassified") -> dict[str, Any]:
        """Take a WAL-consistent copy of the store into ``dest`` (mode 0700)."""
        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        out.chmod(0o700)
        roots, db = self._archive_roots()
        copy = out / "native-session.db"
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()
        finally:
            source.close()
        copy.chmod(0o600)
        files = [{"name": "native-session.db", "path": copy.name, "sha256": _sha_file(copy), "bytes": copy.stat().st_size}]
        for name, root in roots:
            archive = out / f"native-{name}.tar"
            with tarfile.open(archive, "w") as tar:
                for path in sorted(root.rglob("*")):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        continue
                    if path == db or path.name.startswith(db.name + "-") or path.suffix in (".db-wal", ".db-shm"):
                        continue  # the sqlite backup is the authoritative copy of the live database
                    tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
            archive.chmod(0o600)
            files.append({"name": f"native-{name}.tar", "path": archive.name, "sha256": _sha_file(archive),
                          "bytes": archive.stat().st_size})
        witness = self._semantic_digest(copy, session_id)
        manifest = {"kind": "native-session-snapshot", "captured_at": time.time(), "session_id": session_id,
                    "boundary": boundary, "database": {"relative": str(db.relative_to(self.stage)) if self.stage in db.parents else db.name},
                    "files": files, "semantic": witness,
                    "runtime": {"version": self.runtime_version, "sha256": self.runtime_sha256},
                    "stage_root": str(self.stage), "directory": str(out)}
        if not witness["session_found"]:
            manifest["integrity_gap"] = "session id not present in the copied database"
        return manifest

    # -- restore --------------------------------------------------------------
    @staticmethod
    def verify_snapshot(manifest: Mapping[str, Any], directory: str | Path | None = None) -> None:
        """Preflight: every file hashed, session identity present. Raises on any mismatch."""
        base = Path(directory or manifest["directory"])
        if manifest.get("integrity_gap"):
            raise NativeSessionError(manifest["integrity_gap"])
        for item in manifest["files"]:
            path = base / item["path"]
            if Path(item["path"]).name != item["path"] or not path.is_file() or path.is_symlink():
                raise NativeSessionError(f"snapshot file missing or unsafe: {item['name']}")
            if _sha_file(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
                raise NativeSessionError(f"snapshot digest mismatch: {item['name']}")

    def restore(self, manifest: Mapping[str, Any], directory: str | Path | None = None) -> str:
        """Restore into this store's stage root; the old trees are moved aside, never deleted."""
        self.verify_snapshot(manifest, directory)
        if str(self.stage) != manifest["stage_root"]:
            raise NativeSessionError("restore stage root differs from the captured path embedded in native messages")
        base = Path(directory or manifest["directory"])
        stamp = f"{int(time.time())}-{os.getpid()}"
        db_rel = Path(manifest["database"]["relative"])
        staging = Path(tempfile.mkdtemp(prefix=".native-restore-", dir=self.stage))
        try:
            for item in manifest["files"]:
                if not item["name"].endswith(".tar"):
                    continue
                part = item["name"][len("native-"):-len(".tar")]
                dest = staging / part
                dest.mkdir()
                with tarfile.open(base / item["path"]) as tar:
                    for member in tar.getmembers():
                        target = (dest / member.name).resolve()
                        if dest.resolve() not in (target, *target.parents) or member.issym() or member.islnk() \
                                or not (member.isfile() or member.isdir()):
                            raise NativeSessionError("unsafe member in native session archive")
                    tar.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(dest)
            shutil.copyfile(base / "native-session.db", staging / "restored.db")
            check = self._semantic_digest(staging / "restored.db", manifest["session_id"])
            if check != manifest["semantic"]:
                raise NativeSessionError("restored session rows differ from the captured semantic witness")
            for part, root in (("state", self.stage / "state"), ("data-opencode", self.stage / "data" / "opencode")):
                if root.exists():
                    root.rename(root.with_name(root.name + f".replaced-{stamp}"))
                if (staging / part).is_dir():
                    root.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(staging / part), str(root))
                else:
                    root.mkdir(parents=True)
            target_db = self.stage / db_rel if not db_rel.is_absolute() else db_rel
            target_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging / "restored.db"), str(target_db))
            target_db.chmod(0o600)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return manifest["session_id"]

    # -- capture_checkpoint / continue_run protocol -----------------------------
    def snapshot_session(self, dest: str | Path, session_id: str, boundary: str = "unclassified") -> dict[str, Any]:
        return self.snapshot(dest, session_id, boundary=boundary)

    def preflight_restore(self, stored: Mapping[str, Any]) -> None:
        snapshot = stored.get("snapshot")
        if not snapshot:
            raise NativeSessionError("checkpoint holds no native session snapshot")
        self.verify_snapshot(snapshot)
        assessment = self.resume_assessment(snapshot.get("boundary", "unclassified"))
        if assessment["blockers"]:
            raise NativeSessionError("; ".join(b["name"] for b in assessment["blockers"]))

    def restore_session(self, stored: Mapping[str, Any]) -> str:
        self.preflight_restore(stored)
        return self.restore(stored["snapshot"])

    def resume(self, runner: Any, stored: Mapping[str, Any], prompt: str, **kwargs: Any) -> Any:
        """Re-enter the original session at a faithful (turn) boundary only."""
        snapshot = stored["snapshot"]
        self.preflight_restore(stored)
        return runner.run_turn(prompt, session_id=snapshot["session_id"], **kwargs)
