"""Durable run checkpoints and guarded continuation primitives.

The checkpoint records a durable native-session snapshot (SQLite backup plus
private session files) separately from transcript evidence. Restoring bytes is
not claimed to be a faithful mid-turn resume: the stop boundary and the pinned
runtime's cold-resume ability are recorded as explicit prerequisites.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Any, Callable


SCHEMA = "artifact-run-checkpoint.v1"
ALLOWANCE_KEYS = ("wall_seconds", "tool_calls", "tokens", "cost_microusd")
PROXY_KEYS = {"cost_usd": float, "pairs": int}
PROXY_ACCOUNTS = ("proposer", "reviewer")
TERMINAL_JOURNAL = {"completed", "failed", "cancelled"}
_SECRET_KEY = re.compile(r"token|secret|api[_-]?key|authorization|password|bearer|credential$", re.I)
_SAFE_KEY = re.compile(r"(_sha256|_present|_id|digest|_env)$", re.I)
_TOKEN_COUNTS = frozenset({'tokens', 'input_tokens', 'output_tokens', 'prompt_tokens',
                           'completion_tokens', 'reserved_tokens', 'max_tokens'})


class ContinuationBlocked(RuntimeError):
    """Continuation cannot safely resume the saved native episode."""


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _scrub(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Redact secret-named fields and literal secret values from public checkpoint sections."""
    if isinstance(value, dict):
        return {k: ("[redacted]" if isinstance(k, str) and _SECRET_KEY.search(k) and not _SAFE_KEY.search(k)
                    and not (k in _TOKEN_COUNTS and type(v) in (int, float))
                    and not isinstance(v, (dict, list)) else _scrub(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def _safe(text: Any, limit: int = 160) -> str:
    """Bounded single-line text for blocker messages; never echoes callback exception detail."""
    return re.sub(r"[^A-Za-z0-9 _.:/=,-]", "?", str(text))[:limit]


def _validate_allowances(allowances: dict[str, Any], *, positive: bool,
                         known_stages: set[str] | None = None) -> dict[str, dict[str, int]]:
    if not isinstance(allowances, dict) or not allowances:
        raise ValueError("allowances must be a non-empty stage mapping")
    result: dict[str, dict[str, int]] = {}
    for stage, values in allowances.items():
        if not isinstance(stage, str) or not stage or not isinstance(values, dict):
            raise ValueError("each allowance must map a stage to allowance units")
        if known_stages is not None and stage not in known_stages:
            raise ValueError(f"unknown stage {_safe(stage, 40)}")
        if set(values) - set(ALLOWANCE_KEYS):
            raise ValueError(f"unknown allowance unit in stage {_safe(stage, 40)}")
        normalized = {}
        for key in ALLOWANCE_KEYS:
            value = values.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{_safe(stage, 40)}.{key} must be a non-negative integer")
            normalized[key] = value
        result[stage] = normalized
    if positive and not any(n > 0 for values in result.values() for n in values.values()):
        raise ValueError("at least one positive allowance increment is required")
    return result


def _positive_proxy_amounts(values: dict[str, Any], label: str) -> dict[str, float | int]:
    if not isinstance(values, dict) or not values or set(values) - set(PROXY_KEYS):
        raise ValueError("proxy allowances must use only cost_usd and pairs")
    result: dict[str, float | int] = {}
    for key, kind in PROXY_KEYS.items():
        if key not in values:
            continue
        value = values[key]
        ok = (isinstance(value, int) and not isinstance(value, bool)) if kind is int else \
            (isinstance(value, (int, float)) and not isinstance(value, bool))
        if not ok or not value > 0 or value != value or value == float("inf"):
            raise ValueError(f"proxy {label}{key} must be a positive finite number" + (" integer" if kind is int else ""))
        result[key] = value
    return result


def proxy_accounts(values: dict[str, Any] | None) -> dict[str, dict[str, float | int]]:
    """Normalize auxiliary budgets to ``{account: {cost_usd, pairs}}``; a flat mapping is the proposer account."""
    if not values:
        return {}
    if set(values) <= set(PROXY_KEYS):
        return {"proposer": dict(values)}
    if set(values) - set(PROXY_ACCOUNTS):
        raise ValueError("proxy allowances must be flat cost_usd/pairs or keyed by proposer/reviewer, not both")
    return {account: dict(amounts) for account, amounts in values.items() if amounts}


def _validate_proxy_allowances(values: dict[str, Any]) -> dict[str, dict[str, float | int]]:
    """Auxiliary increments: positive cost_usd/pairs per account (flat = proposer). Never credits an account twice."""
    if not isinstance(values, dict) or not values:
        raise ValueError("proxy allowances must use only cost_usd and pairs")
    if not set(values) <= set(PROXY_KEYS) and (set(values) & set(PROXY_KEYS) or set(values) - set(PROXY_ACCOUNTS)):
        raise ValueError("proxy allowances must be flat cost_usd/pairs or keyed by proposer/reviewer, not both")
    accounts = proxy_accounts(values)
    if not accounts:
        raise ValueError("proxy allowances must use only cost_usd and pairs")
    return {account: _positive_proxy_amounts(amounts, account + ".") for account, amounts in accounts.items()}


def _file_ref(base: Path, path: Path, name: str) -> dict[str, Any]:
    blob = path.read_bytes()
    return {"name": name, "path": str(path.relative_to(base)) if base in path.parents else str(path),
            "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}


def _pending_journal(path: Path) -> list[dict[str, Any]]:
    latest: dict[tuple, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            key = item.get("execution_key", {})
            latest[tuple(sorted(key.items()))] = item
    return [{"execution_key": i["execution_key"], "state": i["state"]} for i in latest.values()
            if i["state"] not in TERMINAL_JOURNAL]


def capture_checkpoint(run_dir: str | Path, *, run_id: str, session_id: str | None,
                       condition: str, model_provider_refs: dict[str, Any], workflow: dict[str, Any],
                       ledger: dict[str, Any], resources: dict[str, Any], profile: dict[str, Any] | None = None,
                       proxy: dict[str, Any] | None = None, journal_path: str | Path | None = None,
                       output_store: str | Path | None = None, session_storage: dict[str, Any] | None = None,
                       native_session: Any = None, resource_snapshot: Callable[[], dict[str, Any]] | None = None,
                       stop_reason: str | None = None, boundary: str | None = None,
                       measurement: dict[str, Any] | None = None, proxy_allowances: dict[str, Any] | None = None,
                       secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Save an atomic checkpoint and its private, hash-pinned evidence files.

    ``native_session`` is a store with ``snapshot_session(dest, session_id,
    boundary)`` or a host adapter (wrapped in ``NativeSessionStore``).
    ``boundary`` is T/P/M or None (unclassified, never resumable). Anything
    missing is recorded as a restore prerequisite, never silently assumed.
    """
    if condition not in ("natural-looking", "eval-looking"):
        raise ValueError("condition must be natural-looking or eval-looking")
    directory = Path(run_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    evidence = directory / "continuation-evidence"
    evidence.mkdir(mode=0o700, exist_ok=True)
    evidence.chmod(0o700)
    dependencies: list[dict[str, Any]] = []
    prerequisites: list[dict[str, Any]] = []
    references: dict[str, Any] = {}
    pending: list[dict[str, Any]] = []
    for name, supplied in (("execution-journal", journal_path), ("output-store", output_store)):
        if supplied is None:
            continue
        source = Path(supplied).resolve()
        if not source.exists():
            references[name] = {"available": False, "path": str(source), "reason": "source path missing"}
            continue
        target = evidence / (name + (".dir" if source.is_dir() else source.suffix or ".bin"))
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        refs = []
        for path in sorted(target.rglob("*") if target.is_dir() else (target,)):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)
                refs.append(_file_ref(directory, path, name))
        dependencies.extend(refs)
        references[name] = {"available": True, "files": refs}
        if name == "execution-journal" and target.is_file():
            pending = _pending_journal(target)
    if pending and boundary == "T":
        boundary = "M"  # never claim a clean turn boundary with open tool calls
    native_storage = deepcopy(session_storage or {})
    native_ref, assessment = None, None
    store = native_session if callable(getattr(native_session, "snapshot_session", None)) else None
    if store is None and native_session is not None and hasattr(native_session, "stage"):
        from .native_sessions import NativeSessionStore
        store = NativeSessionStore.from_adapter(native_session)
    if store is not None and session_id:
        try:
            native_ref = store.snapshot_session(evidence / "native-session", session_id, boundary or "unclassified")
            native_ref["directory"] = str((evidence / "native-session").relative_to(directory))
            dependencies.extend({"name": "native-" + f["name"], "path": str(Path(native_ref["directory"]) / f["path"]),
                                 "sha256": f["sha256"], "bytes": f["bytes"]} for f in native_ref["files"])
            assessment = store.resume_assessment(boundary or "unclassified")
            prerequisites.extend(assessment["blockers"])
            if native_ref.get("integrity_gap"):
                prerequisites.append({"name": "native-session-identity", "available": False, "reason": native_ref["integrity_gap"]})
        except Exception as exc:
            native_ref = None
            prerequisites.append({"name": "native-session-snapshot", "available": False,
                                  "reason": f"snapshot failed: {type(exc).__name__}"})
    else:
        prerequisites.append({"name": "native-session-snapshot", "available": False,
                              "reason": "no native session store or session id supplied"})
    native_storage.update({"session_id": session_id, "snapshot": native_ref, "boundary": boundary or "unclassified",
                           "resume_assessment": assessment, "transcript_is_restore_source": False})
    if resource_snapshot is not None:
        try:
            observed_resources = resource_snapshot()
        except Exception as exc:
            observed_resources = {"durable_volume_export": False, "error": type(exc).__name__}
    else:
        observed_resources = deepcopy(resources)
    if not observed_resources.get("durable_volume_export"):
        prerequisites.append({"name": "durable-guest-service-snapshot", "available": False,
            "reason": "no durable guest/service capture (volumes, writable layers, audit/receipts) was produced"
            if resource_snapshot is None or "error" not in observed_resources
            else "durable guest/service capture raised " + _safe(observed_resources["error"], 60)})
    else:
        for item in observed_resources.get("artifacts", []):
            dependencies.append({"name": f"{item['owner']}-{item['kind']}", "path": item["path"],
                                 "sha256": item["sha256"], "bytes": item["bytes"]})
    for gap in observed_resources.get("gaps", []):
        prerequisites.append({"name": "coverage-gap:" + gap["name"], "available": False, "reason": gap.get("reason", "not captured"),
                              "informational": True})
    allowances = deepcopy(ledger.get("allowances", {}))
    checkpoint = {
        "schema": SCHEMA, "checkpoint_id": uuid.uuid4().hex, "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(), "session_id": session_id,
        "model_provider_refs": _scrub(model_provider_refs, secrets), "condition": condition,
        "native_session": _scrub(native_storage, secrets), "guest_and_service": _scrub(observed_resources, secrets),
        "workflow": _scrub(workflow, secrets), "profile_proxy": {"profile": _scrub(profile, secrets), "proxy": _scrub(proxy, secrets)},
        "journal_and_outputs": references, "pending_journal": pending, "dependencies": dependencies,
        "ledger": deepcopy(ledger), "stop_reason": stop_reason, "measurement": deepcopy(measurement),
        "continuation": {"segments": [], "original_allowances": deepcopy(allowances),
                         "original_proxy_allowances": proxy_accounts(deepcopy(proxy_allowances)),
                         "cumulative_usage": deepcopy(ledger.get("usage", {})),
                         "target_allowance_totals": deepcopy(allowances),
                         "proxy_allowance_totals": proxy_accounts(deepcopy(proxy_allowances)), "measured_extension": False},
        "restore_prerequisites": prerequisites,
    }
    checkpoint["checkpoint_sha256"] = canonical_digest(checkpoint)
    _atomic_json(directory / "checkpoint.json", checkpoint)
    return checkpoint


def load_checkpoint(run_dir: str | Path) -> dict[str, Any]:
    path = Path(run_dir) / "checkpoint.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != SCHEMA:
        raise ValueError("unsupported run checkpoint schema")
    expected = value.pop("checkpoint_sha256", None)
    actual = canonical_digest(value)
    value["checkpoint_sha256"] = expected
    if expected != actual:
        raise ValueError("run checkpoint digest mismatch")
    return value


def verify_evidence(run_dir: str | Path, checkpoint: dict[str, Any]) -> None:
    """Re-hash every dependency; relative paths must stay inside the run directory."""
    base = Path(run_dir).resolve()
    for dep in checkpoint.get("dependencies", []):
        raw = Path(dep["path"])
        path = raw if raw.is_absolute() else base / raw
        if not raw.is_absolute() and (base not in path.resolve().parents or path.is_symlink()):
            raise ContinuationBlocked(f"evidence-path-escape: {_safe(dep.get('name'))}")
        if not path.is_file() or path.is_symlink():
            raise ContinuationBlocked(f"evidence-missing: {_safe(dep.get('name'))}")
        blob = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                blob.update(block)
                size += len(block)
        if blob.hexdigest() != dep["sha256"] or size != dep["bytes"]:
            raise ContinuationBlocked(f"evidence-digest-mismatch: {_safe(dep.get('name'))}")


@contextmanager
def exclusive_run_lease(run_dir: str | Path):
    """Hold an OS-backed exclusive lease for one continuation owner."""
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ".continue.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContinuationBlocked("another continuation owner holds the run lease") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _save(target: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["checkpoint_sha256"] = canonical_digest({k: v for k, v in checkpoint.items() if k != "checkpoint_sha256"})
    _atomic_json(target / "checkpoint.json", checkpoint)


class RestoreFidelityBlocked(RuntimeError):
    """Guest process state cannot be shown restorable/absent; the message is a safe, concrete blocker."""


PROCESS_ATTESTATION_SCHEMA = "guest-process-state-attestation/v1"
# Observation (docker top, ps) never qualifies, and no process checkpoint/restore exists in Episode.restore, so only
# an *enforced* absence contract can qualify -- and only when an independent verifier vouches for it.
PROCESS_ATTESTATION_METHODS = ("enforced-process-free-boundary",)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def process_restore_problem(snapshot: Any, verifier: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None) -> str | None:
    """Why this guest snapshot may not drive a cold restore, or None when evidence-backed attestation is complete.

    Pure and side-effect free (only reads the attestation artifact). The snapshot must be quiescent, durably
    exported, carry no process-state gap, and carry an attestation whose artifact hash verifies and whose
    content declares no required process state uncaptured. ``verifier(snapshot, evidence_body)`` must independently
    confirm the enforced-absence claim; production supplies none (no enforcement mechanism exists), so every real
    snapshot stays blocked. A hash-checked JSON assertion alone is never sufficient.
    """
    if not isinstance(snapshot, dict):
        return "guest snapshot is malformed"
    gaps = snapshot.get("gaps", [])
    if not isinstance(gaps, list) or any(not isinstance(g, dict) for g in gaps):
        return "guest snapshot gaps are malformed"
    for gap in gaps:
        if "process" in str(gap.get("name", "")).lower():
            return f"declared unrestorable guest process state: {_safe(gap.get('name'), 60)}"
    if snapshot.get("quiescent") is not True or snapshot.get("durable_volume_export") is not True:
        return "snapshot is not attested quiescent and durably exported"
    att = snapshot.get("process_state_attestation")
    if att is None:
        return "no process-state attestation: absence of required guest process state is not established"
    if (not isinstance(att, dict) or att.get("schema") != PROCESS_ATTESTATION_SCHEMA
            or att.get("method") not in PROCESS_ATTESTATION_METHODS or att.get("quiescent") is not True
            or att.get("required_process_state_uncaptured") is not False
            or not isinstance(att.get("evidence_sha256"), str) or not _HEX64.match(att["evidence_sha256"])):
        return "process-state attestation is malformed or does not attest quiescent capture with no uncaptured required process state"
    rows = [a for a in snapshot.get("artifacts", []) if isinstance(a, dict) and a.get("kind") == "process-state-attestation"]
    if len(rows) != 1 or rows[0].get("sha256") != att["evidence_sha256"] or not isinstance(rows[0].get("path"), str):
        return "process-state attestation has no matching evidence artifact"
    try:
        raw = Path(rows[0]["path"]).read_bytes()
        body = json.loads(raw)
    except (OSError, ValueError):
        return "process-state attestation evidence is unreadable"
    if hashlib.sha256(raw).hexdigest() != att["evidence_sha256"]:
        return "process-state attestation evidence digest mismatch"
    if not isinstance(body, dict) or any(body.get(k) != att[k] for k in ("schema", "method", "quiescent", "required_process_state_uncaptured")):
        return "process-state attestation evidence does not support the attestation"
    if verifier is None:
        return "no verifier can confirm enforced absence of guest process state in this build; process memory is not restorable"
    try:
        confirmed = verifier(snapshot, body) is True
    except Exception:
        confirmed = False
    return None if confirmed else "process-state attestation was not confirmed by the enforcement verifier"


def require_process_restore_attestation(snapshot: Any, verifier: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None) -> None:
    problem = process_restore_problem(snapshot, verifier)
    if problem:
        raise RestoreFidelityBlocked(problem)


def _block(target: Path, checkpoint: dict[str, Any], missing: list[dict[str, Any]]) -> None:
    clean = [{"name": _safe(m.get("name"), 80), "reason": _safe(m.get("reason", ""), 200)} for m in missing]
    checkpoint["last_continue_attempt"] = {"at": datetime.now(timezone.utc).isoformat(), "status": "blocked", "missing": clean}
    _save(target, checkpoint)
    raise ContinuationBlocked("; ".join(f"{m['name']}: {m['reason']}" for m in clean))


def continue_run(run_dir: str | Path, *, additional_allowances: dict[str, Any] | None = None,
                 additional_proxy_allowances: dict[str, Any] | None = None,
                 restore_resources: Callable[[dict[str, Any]], Any] | None = None,
                 preflight_resources: Callable[[dict[str, Any]], Any] | None = None,
                 native_session: Any = None, reconcile: Callable[[dict[str, Any]], Any] | None = None,
                 resume: Callable[[dict[str, Any], Any, Any], Any] | None = None) -> dict[str, Any]:
    """Lease, preflight everything, then restore, reconcile and resume the saved session.

    Nothing is mutated until the lease is held and the checkpoint digest, every
    evidence dependency, the native snapshot, the resource snapshot and all
    callbacks have passed preflight. Whether the combined result is measured is
    derived from the saved checkpoint, never from the caller.
    """
    target = Path(run_dir).resolve()
    with exclusive_run_lease(target):
        checkpoint = load_checkpoint(target)
        stages = set(checkpoint["continuation"].get("original_allowances", {}))
        target_budget = _validate_allowances(additional_allowances, positive=False, known_stages=stages) if additional_allowances else {}
        proxy_budget = _validate_proxy_allowances(additional_proxy_allowances) if additional_proxy_allowances else {}
        if not any(n > 0 for values in target_budget.values() for n in values.values()) and not proxy_budget:
            raise ValueError("at least one positive target or proxy allowance increment is required")
        missing = [x for x in checkpoint.get("restore_prerequisites", []) if not x.get("available") and not x.get("informational")]
        try:
            verify_evidence(target, checkpoint)
        except ContinuationBlocked as exc:
            missing.append({"name": "evidence-integrity", "reason": str(exc)})
        for name, present in (("native-session-store", native_session is not None and callable(getattr(native_session, "restore_session", None))),
                              ("resource-restore-adapter", callable(restore_resources)),
                              ("reconcile-callback", callable(reconcile)), ("resume-callback", callable(resume))):
            if not present:
                missing.append({"name": name, "reason": "required adapter or callback not supplied"})
        if missing:
            _block(target, checkpoint, missing)
        stored = deepcopy(checkpoint["native_session"])
        if stored.get("snapshot"):
            stored["snapshot"]["directory"] = str(target / stored["snapshot"]["directory"])
        for name, check, arg in (("native-preflight", getattr(native_session, "preflight_restore", None), stored),
                                 ("resource-preflight", preflight_resources, checkpoint["guest_and_service"])):
            if callable(check):
                try:
                    check(arg)
                except RestoreFidelityBlocked as exc:
                    _block(target, checkpoint, [{"name": "guest-process-state-unrestorable", "reason": str(exc)}])
                except Exception as exc:
                    _block(target, checkpoint, [{"name": name, "reason": type(exc).__name__}])
        try:
            resource_state = restore_resources(checkpoint["guest_and_service"])
            session = native_session.restore_session(stored)
        except Exception as exc:
            _block(target, checkpoint, [{"name": "restore-failed", "reason": type(exc).__name__}])
        try:
            outcome = reconcile(checkpoint)
        except Exception as exc:
            _block(target, checkpoint, [{"name": "reconcile-failed", "reason": type(exc).__name__}])
        if not isinstance(outcome, dict) or "unresolved" not in outcome or outcome["unresolved"]:
            _block(target, checkpoint, [{"name": "unresolved-journal-entries",
                                         "reason": "uncertain operations remain; they are never blindly retried"}])
        measurement = checkpoint.get("measurement")
        measured = measurement is None or bool(measurement.get("measured", measurement.get("measured_episode", True)))  # unknown is treated as measured
        extension = measured and any(n > 0 for values in target_budget.values() for n in values.values())
        started = datetime.now(timezone.utc)
        cont = checkpoint["continuation"]
        target_totals = deepcopy(cont.get("target_allowance_totals", {}))
        proxy_totals = proxy_accounts(deepcopy(cont.get("proxy_allowance_totals", {})))
        _add_usage(target_totals, target_budget)
        for account, amounts in proxy_budget.items():
            total = proxy_totals.setdefault(account, {})
            for key, amount in amounts.items():
                total[key] = total.get(key, 0) + amount
        pause_from = (cont["segments"][-1].get("ended_at") if cont["segments"] else None) or checkpoint["captured_at"]
        segment = {"segment_id": len(cont["segments"]) + 1, "started_at": started.isoformat(),
                   "paused_since": pause_from,
                   "paused_seconds": max(0.0, (started - datetime.fromisoformat(pause_from)).total_seconds()),
                   "target_allowances": target_budget, "proxy_allowances": proxy_budget, "status": "running",
                   "label": "extended development evidence" if extension else "continuation"}
        cont["segments"].append(segment)
        cont["target_allowance_totals"], cont["proxy_allowance_totals"] = target_totals, proxy_totals
        cont["measured_extension"] |= extension
        _save(target, checkpoint)
        before = checkpoint["checkpoint_sha256"]
        try:
            result = resume(checkpoint, session, resource_state)
        except Exception as exc:
            checkpoint, segment = _current(target, checkpoint, segment, before)
            segment["status"] = "interrupted"
            segment["ended_at"] = datetime.now(timezone.utc).isoformat()
            segment["failure_class"] = type(exc).__name__
            _save(target, checkpoint)
            raise
        checkpoint, segment = _current(target, checkpoint, segment, before)
        segment["status"] = "completed"
        segment["ended_at"] = datetime.now(timezone.utc).isoformat()
        segment["result"] = deepcopy(result)
        checkpoint["last_continue_attempt"] = {"at": segment["ended_at"], "status": "completed"}
        _save(target, checkpoint)
        return {"checkpoint": checkpoint, "result": result}


def _current(target: Path, checkpoint: dict[str, Any], segment: dict[str, Any], before: str):
    """If resume re-captured a fresh checkpoint (second budget stop), continue from that one, not the stale copy."""
    latest = load_checkpoint(target)
    if latest["checkpoint_sha256"] == before:
        return checkpoint, segment
    return latest, latest["continuation"]["segments"][-1]


def _add_usage(cumulative: dict[str, Any], allowances: dict[str, dict[str, int]]) -> None:
    for stage, values in allowances.items():
        target = cumulative.setdefault(stage, {})
        for key, amount in values.items():
            target[key] = int(target.get(key, 0)) + amount
