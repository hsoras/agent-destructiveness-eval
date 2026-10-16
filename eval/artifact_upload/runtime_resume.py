"""Cold continuation of a budget-paused native run (spec section 8, acceptance 10-11).

``continue_native_run`` is the single entry point. It never calls ``Episode.start()``: guest/service resources are
recreated by ``Episode.restore`` from the checkpoint's private, hash-verified archives, the native session store is
restored byte-for-byte at the same stage path, and the workflow resumes the *same* OpenCode session at the saved
turn boundary (T). Mid-turn boundaries (P, M) stay explicit saved blockers: there is no entry point that re-enters
the model loop without adding conversation content, and no new conversation is ever substituted.

Credentials are read only from the external environment names recorded in the checkpoint. Auxiliary (proxy)
clients are rebuilt by the default saved-profile factory or a caller-supplied ``presentation_factory`` from the checkpoint's explicit routes; counters are
restored, never reset.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time
from typing import Any, Callable, Mapping

from .continuation import (ContinuationBlocked, require_process_restore_attestation, _safe, capture_checkpoint, continue_run, load_checkpoint,
                           proxy_accounts, _validate_allowances, _validate_proxy_allowances, canonical_digest)

SPOOL = "/tmp/artifact-worker-output-1000"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
STAGE_DIRS_RENEWED = ("config", "evidence")  # adapter-created; recreated by stage_runtime, old copies kept aside


@dataclass
class ResumeContext:
    """Everything the evidence/finalization stage needs after the model work is done (passed to ``finalize``)."""
    run_dir: Path
    run_id: str
    condition: str
    scenario: str
    segment: int
    checkpoint: dict[str, Any]
    world: dict[str, Any]
    episode: Any
    adapter: Any
    ledger: Any
    workflow: Any
    recorder: Any
    worker: Any
    checkpoint_check: Any
    service: Any
    presentation: Any
    credentials: dict[str, str]
    result: dict[str, Any] = field(default_factory=dict)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refs(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    refs = dict(checkpoint.get("model_provider_refs") or {})
    refs.setdefault("api_key_env", DEFAULT_KEY_ENV)
    return refs


def _aux_env_names(refs: Mapping[str, Any]) -> list[str]:
    """Environment names of every explicit auxiliary route (proposer/reviewer); literal keys are never stored."""
    names = []
    for route in (refs.get("auxiliary_routes") or {}).values():
        if isinstance(route, Mapping):
            name = route.get("api_key_env") or route.get("key_env")
            if name:
                names.append(name)
    return names


def preflight_environment(checkpoint: Mapping[str, Any], environ: Mapping[str, str], run_dir: Path, *, runtime: Any = None,
                          additional_allowances: Mapping[str, Any] | None = None) -> dict:
    """Checks that need no mutation: pinned runtime, credentials, boundary, live journal vs evidence, accounting."""
    problems: list[dict[str, str]] = []
    refs = _refs(checkpoint)
    for name in [refs["api_key_env"], *_aux_env_names(refs)]:
        if not environ.get(name):
            problems.append({"name": "credential-missing", "reason": f"environment variable {_safe(name, 60)} is not set"})
    from .runtime_artifacts import DevelopmentRuntime
    development = isinstance(runtime, DevelopmentRuntime)
    artifact = refs.get('runtime_artifact')
    if artifact and runtime is None:
        # An override is always explicit, including on resume; never silently load a new artifact.
        problems.append({'name': 'development-runtime-required', 'reason': 'supply the original verified development runtime explicitly'})
    if development:
        measured = checkpoint.get('measurement') or {}
        proxy = (checkpoint.get('profile_proxy') or {}).get('proxy') or {}
        if measured.get('measured', measured.get('measured_episode', True)) or proxy.get('proxy_mode') != 'llm' or artifact != runtime.provenance:
            problems.append({'name': 'development-runtime-provenance', 'reason': 'override is restricted to original unmeasured llm live-preview artifact'})
        try:
            runtime.verify()
        except Exception as exc:
            problems.append({'name': 'pinned-native-runtime', 'reason': _safe(exc, 200)})
    elif runtime is None:
        from .host_adapter import PinnedRuntime, RuntimeUnavailable
        try:
            runtime = PinnedRuntime.locate(environ)
        except RuntimeUnavailable as exc:
            problems.append({"name": "pinned-native-runtime", "reason": _safe(exc, 200)})
        except Exception as exc:  # version probe / subprocess failures
            problems.append({"name": "pinned-native-runtime", "reason": type(exc).__name__})
    elif not Path(runtime.binary).is_file():
        problems.append({"name": "pinned-native-runtime", "reason": "pinned OpenCode binary is absent"})
    else:
        from . import host_adapter
        if artifact or _sha(Path(runtime.binary)) != host_adapter.RUNTIME_SHA256:
            problems.append({"name": "pinned-native-runtime", "reason": "OpenCode binary digest differs from the pinned runtime"})
    state = checkpoint.get("workflow") or {}
    if state.get("boundary") != "T":
        problems.append({"name": "workflow-boundary", "reason": f"saved boundary is {state.get('boundary')!r}; only T resumes"})
    native = (checkpoint.get("native_session") or {}).get("snapshot") or {}
    if native.get("boundary") != "T":
        problems.append({"name": "native-boundary", "reason": f"native snapshot boundary is {native.get('boundary')!r}"})
    try:
        _check_accounting(checkpoint, state, additional_allowances)
    except ValueError as exc:
        problems.append({"name": "ledger-accounting", "reason": _safe(exc, 200)})
    journal = run_dir / "broker" / "executions.jsonl"
    evidence = run_dir / "continuation-evidence" / "execution-journal.jsonl"
    if evidence.is_file() and journal.is_file() and _sha(journal) != _sha(evidence):
        problems.append({"name": "journal-diverged", "reason": "live execution journal differs from the checkpoint copy"})
    guest_resources = ((checkpoint.get("guest_and_service") or {}).get("live_inventory") or {}).get("resources") or {}
    if guest_resources.get("local_positive_control"):
        problems.append({"name": "positive-control-unsupported", "reason": "local positive-control episodes are not restorable"})
    if checkpoint.get("pending_journal"):
        problems.append({"name": "pending-journal", "reason": "open execution-journal entries at a claimed turn boundary"})
    return {"problems": problems, "runtime": runtime}


def _check_accounting(checkpoint: Mapping[str, Any], state: Mapping[str, Any],
                      additional: Mapping[str, Any] | None = None) -> None:
    """Saved spent usage must fit the cumulative allowances (plus this increment), with wall headroom for the next stage."""
    from .workflow import Ledger
    totals = deepcopy(checkpoint["continuation"]["target_allowance_totals"])
    for stage_name, values in (additional or {}).items():
        for key, amount in values.items():
            totals[stage_name][key] = totals[stage_name].get(key, 0) + amount
    ledger = Ledger.restore(checkpoint["ledger"], totals)
    stage = "upload" if state.get("next") == "upload" else "overhead"
    if ledger.snapshot()["remaining"][stage]["wall_seconds"] <= 0:
        raise ValueError(f"no wall time left in the {stage} stage; add a wall_seconds increment")


def _stage_root(checkpoint: Mapping[str, Any], run_dir: Path) -> Path:
    snap = (checkpoint.get("native_session") or {}).get("snapshot") or {}
    return Path(snap.get("stage_root") or (checkpoint.get("model_provider_refs") or {}).get("stage_root") or run_dir / "host-stage")


def _move_aside(stage: Path, segment: int) -> dict[str, str]:
    """Keep the previous segment's adapter-generated tree; the restored session store itself stays in place."""
    moved = {}
    for name in STAGE_DIRS_RENEWED:
        source = stage / name
        if source.exists():
            target = stage / f".segment-{segment - 1}-{name}"
            if target.exists():
                raise ContinuationBlocked(f"stage-aside-collision: {name}")
            source.rename(target)
            moved[name] = str(target)
    return moved


def _verify_evidence_copy(live: Path, copy: Path) -> None:
    """Every checkpointed file must still be present and byte-identical in the live tree."""
    if not copy.exists():
        return
    for path in sorted(p for p in (copy.rglob("*") if copy.is_dir() else [copy]) if p.is_file()):
        rel = path.relative_to(copy) if copy.is_dir() else Path(".")
        peer = live / rel if copy.is_dir() else live
        if not peer.is_file() or _sha(peer) != _sha(path):
            raise ContinuationBlocked(f"live-store-diverged: {_safe(rel, 80)}")


def _live_witness(store, session_id):
    """Compare a WAL-consistent SQLite backup, never an immutable view of a live WAL."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory(prefix='native-witness-') as directory:
        copy = Path(directory) / 'session.db'
        # SQLite may need to create empty WAL/SHM files after restore; mode=ro
        # cannot do so on macOS. query_only forbids SQL mutations.
        source = sqlite3.connect(f'file:{store.db_path()}?mode=rw', uri=True)
        source.execute('pragma query_only=ON')
        destination = sqlite3.connect(copy)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return store._semantic_digest(copy, session_id)


def saved_profile_presentation(*, checkpoint, recorder_root, credentials, proxy_totals, segment, environ, episode):
    """Default factory uses only explicit saved routes, environment references and cumulative accounts."""
    from .profiles import ProfileBundle
    from .profile_proxy import ProfilePresentation
    from .guest_operations import Catalog, SetupReceipt
    from .discovery import AuxiliaryClient
    from .live import LiveBudget
    saved = checkpoint.get('profile_proxy') or {}
    if not saved.get('profile'):
        if saved.get('proxy'):
            raise ContinuationBlocked('legacy-live-presentation-state-unavailable: installed rules and discovery state not checkpointed')
        from .presentation import FrozenPresentation
        return FrozenPresentation(checkpoint['condition'], events_path=Path(recorder_root).parent / 'presentation.jsonl')
    bundle = ProfileBundle.from_dict(saved['profile'])
    receipt = saved.get('proxy') or {}
    if receipt.get('profile_sha256') != bundle.sha256:
        raise ContinuationBlocked('profile-digest-divergence: saved profile receipt differs')
    refs = _refs(checkpoint)
    routes = refs.get('auxiliary_routes') or {}
    develop = receipt.get('proxy_mode') == 'llm'
    if not develop and (routes or receipt.get('auxiliary_model_calls', 0)):
        raise ContinuationBlocked('deterministic-auxiliary-divergence')
    clients, budgets = {}, {}
    if develop:
        if 'proposer' not in routes:
            raise ContinuationBlocked('auxiliary-route-missing: proposer')
        for role, route in routes.items():
            if role not in ('proposer', 'reviewer'):
                raise ContinuationBlocked('auxiliary-route-unknown')
            name = route.get('api_key_env') or route.get('key_env')
            if not name or not environ.get(name) or 'api_key' in route:
                raise ContinuationBlocked('auxiliary-credential-reference-missing')
            try:
                clients[role] = AuxiliaryClient(route['upstream'], route['model'], input_price=route['input_price'],
                    output_price=route['output_price'], api_key=environ[name],
                    provider_options=route.get('provider_options'), provider_pin=route.get('provider_pin'))
                limits = dict((refs.get('auxiliary_limits') or {})[role])
                budget_receipt = receipt.get('budget') or {}
                account_receipt = budget_receipt if role == 'proposer' else budget_receipt.get('reviewer', {})
                limits.update(account_receipt['current'])
                budgets[role] = LiveBudget(**limits)
            except (KeyError, TypeError, ValueError) as exc:
                raise ContinuationBlocked('auxiliary-limits-unavailable: ' + role) from exc
        if bool(receipt.get('reviewer')) != ('reviewer' in clients):
            raise ContinuationBlocked('auxiliary-reviewer-divergence')
    out = Path(refs.get('presentation_out') or Path(receipt.get('profile_path') or Path(recorder_root).parent / 'profile.final.json').parent)
    pres = ProfilePresentation(out, bundle, condition=checkpoint['condition'], await_delivery=True,
        catalog=Catalog(workspace=episode.resources['profile_manifest']['worktree']),
        proposer=clients.get('proposer'), reviewer=clients.get('reviewer'), budget=budgets.get('proposer'),
        reviewer_budget=budgets.get('reviewer'), profile_out=receipt.get('profile_path'),
        review_phase=receipt.get('review_phase', 'both'))  # checkpoints predating the receipt field ran with 'both'
    state = refs.get('profile_presentation_state') or {}
    if state.get('stopped') or state.get('pending'):
        raise ContinuationBlocked('profile-presentation-unresolved: stopped or undelivered transaction')
    if state.get('initial'):
        initial = ProfileBundle.from_dict(state['initial'])
        if initial.sha256 != receipt.get('initial_profile_sha256'):
            raise ContinuationBlocked('initial-profile-digest-divergence')
        pres.initial = initial
    elif receipt.get('initial_profile_sha256') and receipt['initial_profile_sha256'] != bundle.sha256:
        matches = list((out / 'profile').glob('profile-v*-' + receipt['initial_profile_sha256'] + '.json'))
        if len(matches) != 1:
            raise ContinuationBlocked('initial-profile-state-unavailable')
        pres.initial = ProfileBundle.load(matches[0])
        if pres.initial.sha256 != receipt['initial_profile_sha256']:
            raise ContinuationBlocked('initial-profile-digest-divergence')
    packets = state.get('packets', 0)
    if type(packets) is not int or packets < 0:
        raise ContinuationBlocked('profile-packet-counter-invalid')
    pres._packets = packets
    pres._deltas = deepcopy(state.get('deltas', []))
    pres._deltas_omitted = state.get('deltas_omitted', 0)
    for key in ('gaps', 'changes', 'failures', 'overrides'):
        setattr(pres, '_' + key, deepcopy(state.get(key, [])))
    if develop:
        usage = receipt.get('usage') or {}
        accounts = usage.get('accounts')
        if pres.discovery.account is not None and not accounts:
            raise ContinuationBlocked('separate-auxiliary-accounting-unavailable')
        old = deepcopy(receipt.get('budget') or {})
        if not old or any(key not in (accounts['proposer'] if accounts else usage)
                          for key in ('calls', 'reserved_cost_usd', 'reported_cost_usd', 'unaccounted_calls', 'halted')):
            raise ContinuationBlocked('auxiliary-accounting-missing: durable budget receipt required')
        if pres.discovery.account is not None:
            if any(key not in accounts.get('reviewer', {}) for key in ('calls', 'reserved_cost_usd', 'reported_cost_usd', 'unaccounted_calls', 'halted')):
                raise ContinuationBlocked('separate-auxiliary-accounting-unavailable')
        try:
            pres.discovery.restore_usage(old)
            increments = {}
            for role in clients:
                current = old['current'] if role == 'proposer' else old['reviewer']['current']
                credited = {}
                for key in ('cost_usd', 'pairs'):
                    difference = proxy_totals[role][key] - current[key]
                    if difference < 0:
                        raise ValueError('cumulative auxiliary allowance decreased')
                    if difference > 0:
                        credited[key] = difference
                if credited:
                    increments[role] = credited
            if increments:
                pres.discovery.replenish(increments)
        except (KeyError, TypeError, ValueError) as exc:
            raise ContinuationBlocked('auxiliary-accounting-invalid') from exc
    pres.guest = episode  # Restored guest already contains committed setup; never re-apply setup.
    if receipt.get('setup'):
        pres._setup = SetupReceipt(**receipt['setup'])
    return pres


def _ensure_development_marker(run_dir: Path, *, enabled: bool) -> None:
    """Preserve a recognized development marker or reject divergence before any resumed work."""
    if not enabled:
        return
    from .task import private_write
    from .live import LABELS
    marker = run_dir / LABELS['marker_file']
    marker_bytes = b'Extended development evidence; not a measured episode.\n'
    if marker.exists():
        if marker.read_bytes() not in {
            b'Development-only live proxy; not a measured episode.\n', marker_bytes
        }:
            raise ContinuationBlocked('development-marker-diverged')
    else:
        private_write(marker, marker_bytes)


def finalize_resume(ctx):
    """Default continuation finalizer, preserving the first scored window and partial result."""
    from .task import finalize_evidence
    from .receipt_intervals import interval_validator
    from .live import LABELS
    result = ctx.result
    start = result.get('scored_window_start') or (result.get('scored_window') or [None])[0]
    if start is None:
        raise ContinuationBlocked('original-scored-window-unavailable')
    checkpoints = [json.loads(p.read_text())
                   for p in sorted(ctx.run_dir.glob('checkpoint.segment-*.json'), key=lambda p: int(p.stem.split('-')[-1]))]
    if any(c.get('checkpoint_sha256') != canonical_digest({k: v for k, v in c.items() if k != 'checkpoint_sha256'}) for c in checkpoints):
        raise ContinuationBlocked('prior-checkpoint-digest-divergence')
    checkpoints.append(ctx.checkpoint)
    try:
        validator = interval_validator(checkpoints)
    except (OSError, ValueError, KeyError) as exc:
        raise ContinuationBlocked('checkpoint-receipt-prefix-unavailable: ' + type(exc).__name__) from exc
    result['continuation_segment'] = ctx.segment
    from datetime import datetime
    result['continuation_window'] = [datetime.fromisoformat(ctx.checkpoint['continuation']['segments'][-1]['started_at']).timestamp(), time.time()]
    if ctx.checkpoint['continuation'].get('measured_extension') or (ctx.presentation.receipt() if hasattr(ctx.presentation, 'receipt') else {}).get('proxy_mode') == 'llm':
        _ensure_development_marker(ctx.run_dir, enabled=True)
        result.update(LABELS, continuation_label='extended development evidence')
    try:
        finalize_evidence(episode=ctx.episode, adapter=ctx.adapter, ledger=ctx.ledger, service=ctx.service,
            checkpoint=ctx.checkpoint_check, world=ctx.world, result=result, scenario=ctx.scenario, start=start,
            receipt_validator=validator)
    except ValueError as exc:
        raise ContinuationBlocked('receipt-finalization-incomplete: ' + _safe(exc, 140)) from exc
    if hasattr(ctx.presentation, 'receipt'):
        result['profile_proxy'] = ctx.presentation.receipt()
    return result


def continue_native_run(run_dir: str | Path, *, additional_allowances: dict[str, Any] | None = None,
                        additional_proxy_allowances: dict[str, Any] | None = None,
                        environ: Mapping[str, str] | None = None, world: dict[str, Any] | None = None,
                        presentation_factory: Callable[..., Any] | None = None,
                        finalize: Callable[[ResumeContext], Any] | None = None,
                        extra_credentials: Mapping[str, str] | None = None,
                        runtime: Any = None, hooks: Mapping[str, Callable[..., Any]] | None = None) -> dict[str, Any]:
    """Preflight, lease, restore everything, reconcile, then resume the saved native session at its T boundary.

    ``additional_allowances``  ``{stage: {wall_seconds,tool_calls,tokens,cost_microusd}}`` (positive increments).
    ``additional_proxy_allowances``  flat ``{cost_usd,pairs}`` (= proposer) or ``{'proposer':{..},'reviewer':{..}}``.
    ``environ``  external credential environment (default ``os.environ``); only names stored in the checkpoint are read.
    ``presentation_factory(checkpoint=, recorder_root=, credentials=, proxy_totals=, segment=)`` rebuilds the
      presenter/auxiliary clients from the checkpoint's explicit routes with *restored* counters; it may expose
      ``attach_recorder(recorder)``. The default restores saved profiles; legacy unsaved live state blocks.
    ``finalize(ctx)``  runs the evidence/scoring stage after the workflow (the parent's final-evidence code).
    Raises ``ContinuationBlocked`` (checkpoint kept, ``last_continue_attempt`` saved) for any unmet prerequisite and
    returns ``continue_run``'s ``{'checkpoint', 'result'}`` on success.
    """
    from .lifecycle import Episode, InfrastructureError
    from .native_sessions import NativeSessionStore
    from . import host_adapter
    env = os.environ if environ is None else environ
    target = Path(run_dir).resolve()
    hooks = hooks or {}
    checkpoint = load_checkpoint(target)
    segment = len(checkpoint["continuation"]["segments"]) + 1
    refs = _refs(checkpoint)
    stage = _stage_root(checkpoint, target)
    if additional_proxy_allowances:
        _validate_proxy_allowances(additional_proxy_allowances)
    extra = (_validate_allowances(additional_allowances, positive=False,
                                  known_stages=set(checkpoint["continuation"]["original_allowances"]))
             if additional_allowances else {})

    holder: dict[str, Any] = {}
    pre = preflight_environment(checkpoint, env, target, runtime=runtime, additional_allowances=extra)
    runtime = pre["runtime"]
    from .continuation import _block, _save
    runtime_ok = not any(p["name"] == "pinned-native-runtime" for p in pre["problems"])
    stale = [x for x in checkpoint["restore_prerequisites"] if x.get("name") == "pinned-native-runtime"]
    if runtime_ok and stale:
        # Capture-time absence is not a fact about now: re-evaluated here, and again by the native store.
        checkpoint["restore_prerequisites"] = [x for x in checkpoint["restore_prerequisites"] if x not in stale]
        checkpoint.setdefault("prerequisites_rechecked", []).append({"name": "pinned-native-runtime", "satisfied": True})
        _save(target, checkpoint)
    if pre["problems"]:
        _block(target, checkpoint, pre["problems"])
    from .runtime_artifacts import DevelopmentRuntime
    development = isinstance(runtime, DevelopmentRuntime)
    store = NativeSessionStore(stage, runtime=runtime,
        runtime_sha256=runtime.expected_sha256 if development else host_adapter.RUNTIME_SHA256,
        runtime_version=runtime.version if development else host_adapter.RUNTIME_VERSION)
    snap_runtime = checkpoint['native_session']['snapshot'].get('runtime') or {}
    if snap_runtime.get('sha256') != store.runtime_sha256:
        _block(target, checkpoint, [{'name': 'native-runtime-identity', 'reason': 'saved native runtime digest differs'}])
    secret_values: dict[str, str] = {}

    def new_episode() -> Any:
        if hooks.get("episode"):
            return hooks["episode"](segment=segment)
        return Episode(checkpoint["condition"], checkpoint["run_id"], target / f"episode-s{segment}",
                       common_image=None, scenario=checkpoint["workflow"]["scenario"])

    def preflight_resources(snapshot: dict[str, Any]) -> None:
        require_process_restore_attestation(snapshot, hooks.get("process_attestation_verifier"))  # before any Episode object or Docker call exists
        episode = new_episode()
        episode.preflight_restore(snapshot, segment=segment)

    def restore_resources(snapshot: dict[str, Any]) -> Any:
        require_process_restore_attestation(snapshot, hooks.get("process_attestation_verifier"))
        episode = new_episode()
        episode.restore(snapshot, segment=segment)
        holder["episode"] = episode
        return episode

    def reconcile(saved: dict[str, Any]) -> dict[str, Any]:
        # At T there are no open calls. Anything open is *unresolved*: it is never retried, and blocks the resume.
        return {"unresolved": [item["execution_key"].get("call_id", "?") for item in saved.get("pending_journal", [])]}

    def resume(saved: dict[str, Any], session_id: str, episode: Any) -> dict[str, Any]:
        try:
            return _resume(saved, session_id, episode)
        except ContinuationBlocked as exc:
            current = load_checkpoint(target)
            current['last_continue_attempt'] = {'status': 'blocked', 'problems': [{'name': str(exc).split(':')[0], 'reason': _safe(exc, 200)}]}
            _save(target, current)
            raise
        finally:
            _teardown(holder)

    def _resume(saved, session_id, episode):
        from .broker import ExecutionBroker
        from .host_adapter import GuestWorkerSession
        from .recording import RunRecorder
        from .workflow import Ledger, TwoStageWorkflow, WorkflowAdapter
        from . import task as task_module
        profile = episode.resources["profile_manifest"]
        cont = saved["continuation"]
        _verify_evidence_copy(target / "broker" / "executions.jsonl", target / "continuation-evidence" / "execution-journal.jsonl")
        # OutputStore is namespaced by run_id; checkpoint evidence is copied
        # from this run-specific root, so compare against the same root.
        _verify_evidence_copy(target / "capture" / "canonical-private" / "outputs" / saved["run_id"],
                              target / "continuation-evidence" / "output-store.dir")
        old_tools = stage / "config" / "native-tools.json"
        old_tools_sha = _sha(old_tools) if old_tools.is_file() else None
        if not refs.get('native_tool_schema_sha256') or old_tools_sha != refs['native_tool_schema_sha256']:
            raise ContinuationBlocked('native-tool-schema-witness-unavailable: live schema must match saved checkpoint digest')
        holder["moved"] = _move_aside(stage, segment)
        if old_tools_sha is None and holder["moved"].get("config"):
            kept = Path(holder["moved"]["config"]) / "native-tools.json"
            old_tools_sha = _sha(kept) if kept.is_file() else None
        ledger = Ledger.restore(saved["ledger"], cont["target_allowance_totals"])
        provider_key = env[refs["api_key_env"]]
        credentials = {"artifact-service": episode.api_token, "model-provider": provider_key, **(extra_credentials or {})}
        credentials.update({f'live-{role}-api-key': env[route.get('api_key_env') or route.get('key_env')]
                            for role, route in (refs.get('auxiliary_routes') or {}).items()
                            if route.get('api_key_env') or route.get('key_env')})
        recorder_root = target / "capture"
        holder["credentials"] = credentials
        presentation = None
        if presentation_factory is not None:
            presentation = presentation_factory(checkpoint=saved, recorder_root=recorder_root, credentials=credentials,
                                                proxy_totals=proxy_accounts(cont.get("proxy_allowance_totals")), segment=segment)
        else:
            presentation = saved_profile_presentation(checkpoint=saved, recorder_root=recorder_root, credentials=credentials,
                proxy_totals=proxy_accounts(cont.get('proxy_allowance_totals')), segment=segment, environ=env, episode=episode)
        holder["presentation"] = presentation
        development_evidence = cont.get('measured_extension') or (
            (presentation.receipt() if hasattr(presentation, 'receipt') else {}).get('proxy_mode') == 'llm')
        _ensure_development_marker(target, enabled=bool(development_evidence))
        worker = (hooks["worker"](episode) if hooks.get("worker") else
                  GuestWorkerSession(episode.resources["worker_container"], saved["run_id"], guest_root=profile["worktree"]))
        holder["worker"] = worker
        broker = ExecutionBroker(saved["run_id"], worker, str(target / "broker" / "executions.jsonl"))

        def guest_bytes(path):
            p = PurePosixPath(path)
            if ".." in p.parts or not any(p.is_relative_to(base) for base in (profile["worktree"], SPOOL)):
                raise ValueError("outside guest content roots")
            code = "import os,sys; p=sys.argv[1]; assert os.path.realpath(p)==p; sys.stdout.buffer.write(open(p,'rb').read())"
            return task_module.docker("exec", episode.resources["worker_container"], "python", "-c", code, path)

        model = refs["model"]
        flags = refs.get("runtime_flags")
        adapter = (hooks.get("adapter") or WorkflowAdapter)(
            ledger=ledger, run_id=saved["run_id"], broker=broker, guest_profile=profile, guest_root=None,
            provider_base_url=refs["provider"], model=model, provider_api_key=provider_key, runtime=runtime,
            recording_root=recorder_root, credentials=credentials, guest_bytes=guest_bytes, guest_spool_roots=(SPOOL,),
            presenter=presentation, context_limit=(200_000, ledger.policy.output_tokens), stage_root=stage,
            allow_provider_qualified_model=bool(refs.get("allow_provider_qualified_model", "/" in model)),
            zdr=refs.get("zdr"), provider_pin=refs.get("provider_pin"),
            **({"runtime_flags": flags} if flags else {}))
        holder["adapter"] = adapter
        if hasattr(presentation, "attach_recorder"):
            presentation.attach_recorder(adapter.recorder)
        adapter.recorder.service_health = episode.receipt_health
        adapter.stage_runtime()
        new_tools = stage / "config" / "native-tools.json"
        if old_tools_sha is None or not new_tools.is_file() or _sha(new_tools) != old_tools_sha:
            raise ContinuationBlocked("native-tools-divergence: routed tool set differs from the checkpointed run")
        protected = task_module.ProtectedCheckpoint(episode, adapter.recorder.private / "protected", ledger)
        protected.serial = len(list((adapter.recorder.private / "protected").glob("check-*")))
        service = task_module.ServiceEvidence(episode, adapter.recorder, ledger)
        if hooks.get("evidence"):
            protected, service = hooks["evidence"](episode, adapter, ledger)
        world_value = world if world is not None else json.loads(Path(task_module.INSTANCE).read_text())
        workflow = TwoStageWorkflow(adapter, ledger, world_value, protected, service.snapshot,
                                    scenario=saved["workflow"]["scenario"])
        adapter.recorder.append("run_state", {"kind": "continuation_segment", "segment": segment,
                                              "session_id": session_id, "moved_aside": holder["moved"],
                                              "totals": cont["target_allowance_totals"]})
        snapshot = saved['native_session']['snapshot']
        if session_id != snapshot['session_id'] or not snapshot.get('semantic', {}).get('session_found'):
            raise ContinuationBlocked('native-session-witness-unavailable')
        witness = _live_witness(store, session_id)
        if witness != snapshot['semantic']:
            raise ContinuationBlocked('native-session-witness-divergence')
        original_turn = adapter.run_turn
        gate_failure = []
        first = True
        def check_turn(prompt, **kwargs):
            nonlocal first
            if kwargs.get('session_id') != session_id:
                raise ContinuationBlocked('session-identity: first resumed turn must use original session')
            if first:
                if _live_witness(store, session_id) != snapshot['semantic']:
                    raise ContinuationBlocked('native-session-witness-divergence')
                expected = world_value['task_prompts'].get('upload') if saved['workflow']['next'] in ('upload', 'warmup_check') else None
                if prompt != expected:
                    raise ContinuationBlocked('pending-workflow-prompt-divergence')
                if hasattr(adapter.broker, 'export_seen') and adapter.broker.export_seen() != saved['workflow'].get('tool_seen', []):
                    raise ContinuationBlocked('native-tool-accounting-divergence')
                first = False
            return original_turn(prompt, **kwargs)
        def guarded_turn(prompt, **kwargs):
            try:
                return check_turn(prompt, **kwargs)
            except ContinuationBlocked as exc:
                gate_failure.append(exc)
                raise
        adapter.run_turn = guarded_turn
        original_result = deepcopy(saved['workflow'].get('original_result') or {})
        result = workflow.resume(saved["workflow"])
        if gate_failure:
            raise gate_failure[0]
        original_result.update(result)
        result = original_result
        if session_id and any(t.get("session_id") not in (None, session_id) for t in result.get("turns", [])):
            raise ContinuationBlocked("session-identity: resumed turns used a different session id")
        ctx = ResumeContext(target, saved["run_id"], saved["condition"], saved["workflow"]["scenario"], segment, saved,
                            world_value, episode, adapter, ledger, workflow, adapter.recorder, worker, protected, service,
                            presentation, credentials, result)
        holder["ctx"] = ctx
        finalized = None
        if result.get('termination_reason') != 'budget_exhausted':
            from .workflow import BudgetExhausted
            from .recording import CaptureFailure
            try:
                finalized = (finalize or finalize_resume)(ctx)
            except BudgetExhausted:
                result['termination_reason'] = 'budget_exhausted'
                result['required_incomplete'] = ['final evidence exceeded cumulative allowance; partial evidence retained']
            except CaptureFailure:
                result['termination_reason'] = 'budget_exhausted' if ledger.failure else 'recording_failure'
                result['required_incomplete'] = ['service or model evidence incomplete; partial evidence retained; no replay']
        if result.get("termination_reason") == "budget_exhausted":
            holder["recapture"] = True
            _recapture(ctx, saved, refs, secret_values)
        summary = json.loads(json.dumps({"termination_reason": result.get("termination_reason"),
                                         "turns": result.get("turns"), "resources": ledger.snapshot(),
                                         "finalized": finalized is not None,
                                         "segment": segment}, default=str))
        out = target / "continuation-results"
        out.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(out / f"segment-{segment}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"workflow": result, "finalized": finalized}, stream, default=str, sort_keys=True, indent=2)
        return summary

    return continue_run(target, additional_allowances=additional_allowances,
                        additional_proxy_allowances=additional_proxy_allowances, native_session=store,
                        preflight_resources=preflight_resources, restore_resources=restore_resources,
                        reconcile=reconcile, resume=resume)


def _recapture(ctx: ResumeContext, saved: dict[str, Any], refs: dict[str, Any], _unused: Any) -> None:
    """A second budget stop: capture again, preserving segment history, totals and the prior checkpoint file."""
    run_dir = ctx.run_dir
    previous = load_checkpoint(run_dir)
    (run_dir / f"checkpoint.segment-{ctx.segment}.json").write_bytes((run_dir / "checkpoint.json").read_bytes())
    state = ctx.workflow.continuation_snapshot()
    if 'scored_window' not in ctx.result and ctx.result.get('scored_window_start') is not None:
        ctx.result['scored_window'] = [ctx.result['scored_window_start'], time.time()]
    state["original_result"] = ctx.result
    session_ids = state.get("session_ids", [])
    fresh = capture_checkpoint(
        run_dir, run_id=ctx.run_id, session_id=session_ids[-1] if session_ids else saved.get("session_id"),
        condition=ctx.condition, model_provider_refs=refs, workflow=state, ledger=ctx.ledger.continuation_snapshot(),
        resources=ctx.episode.resources, profile=(saved.get("profile_proxy") or {}).get("profile"),
        proxy=(ctx.presentation.receipt() if hasattr(ctx.presentation, "receipt") else (saved.get("profile_proxy") or {}).get("proxy")),
        journal_path=run_dir / "broker" / "executions.jsonl", output_store=ctx.adapter.output_store.root,
        native_session=ctx.adapter, resource_snapshot=ctx.episode.continuation_snapshot,
        measurement=previous.get("measurement"), secrets=tuple(ctx.credentials.values()),
        stop_reason="budget_exhausted", boundary=state.get("boundary"))
    # Keep the cumulative history: segments, original limits and running totals, with the running segment closed by
    # continue_run when this resume callback returns (it re-reads and saves the same checkpoint object).
    fresh["continuation"] = deepcopy(previous["continuation"])
    fresh["checkpoint_sha256"] = canonical_digest({k: v for k, v in fresh.items() if k != "checkpoint_sha256"})
    from .continuation import _atomic_json
    _atomic_json(run_dir / "checkpoint.json", fresh)


def _teardown(holder: dict[str, Any]) -> None:
    """Close what this segment opened and remove only the restored resources (they are archived in the checkpoint)."""
    for name in ("adapter", "worker"):
        try:
            if holder.get(name):
                holder[name].close()
        except Exception:
            pass
    episode = holder.get("episode")
    if episode is not None:
        try:
            episode.cleanup()
        except Exception:
            pass
