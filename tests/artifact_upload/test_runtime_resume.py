"""continue_native_run end to end with a real native session store and fake runtime/Docker/provider."""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.artifact_upload import host_adapter
from eval.artifact_upload.continuation import ContinuationBlocked, capture_checkpoint, load_checkpoint
from eval.artifact_upload.native_sessions import NativeSessionStore
from eval.artifact_upload.runtime_resume import continue_native_run
from tests.artifact_upload.test_continuation import make_stage
from tests.artifact_upload.test_workflow import WORLD, turn
from tests.artifact_upload.test_workflow_resume import make

ENV = {"TEST_KEY": "sk-test-key-123"}
TOOLS = '{"bash": {}}'


class FakeEpisode:
    def __init__(self, **_):
        self.api_token = "tok"
        self.resources = {"profile_manifest": {"worktree": "/work"}, "worker_container": "wc"}
        self.events = []

    def preflight_restore(self, snapshot, segment=1):
        self.events.append("preflight")

    def restore(self, snapshot, segment=1):
        self.events.append(f"restore-s{segment}")

    def receipt_health(self):
        return {"healthy": True}

    def cleanup(self):
        self.events.append("cleanup")


class FakeAdapter:
    log = []
    tools = TOOLS

    def __init__(self, **kw):
        self.kw = kw
        self.stage = Path(kw["stage_root"])
        root = Path(kw["recording_root"]) / "canonical-private"
        self.recorder = SimpleNamespace(private=root, failure=None, rows=[], service_health=None,
                                        append=lambda stream, row: self.recorder.rows.append((stream, row)))
        self.broker = SimpleNamespace(journal=SimpleNamespace(path=self.stage.parent / "broker" / "executions.jsonl"))
        self.output_store = SimpleNamespace(root=root / "outputs")
        self.closed = False

    def stage_runtime(self):
        if getattr(self, "staged", False):  # the real adapter is idempotent too
            return
        self.staged = True
        (self.stage / "config").mkdir()
        (self.stage / "config" / "native-tools.json").write_text(self.tools)

    def run_turn(self, prompt, *, session_id=None, timeout=0):
        db = sqlite3.connect(self.stage / "data/opencode/opencode.db")
        seen = db.execute("select data from message").fetchone()[0]
        db.close()
        FakeAdapter.log.append((prompt, session_id, seen))
        return turn("Uploaded.", sid=session_id)

    def close(self):
        self.closed = True


@pytest.fixture
def paused(tmp_path, monkeypatch):
    FakeAdapter.log = []
    FakeAdapter.tools = TOOLS
    run = tmp_path / "run"
    stage = run / "host-stage"
    stage.parent.mkdir(parents=True)
    _, db = make_stage(run, "original")  # creates run/host-stage
    (stage / "config").mkdir()
    (stage / "config" / "native-tools.json").write_text(TOOLS)
    (run / "broker").mkdir()
    (run / "broker" / "executions.jsonl").write_text("")
    outputs = run / "capture" / "canonical-private" / "outputs" / "run-1"
    outputs.mkdir(parents=True)
    (outputs / "o1").write_text("kept")
    binary = tmp_path / "opencode"
    binary.write_bytes(b"pinned")
    monkeypatch.setattr(host_adapter, "RUNTIME_SHA256", hashlib.sha256(b"pinned").hexdigest())
    runtime = SimpleNamespace(binary=binary, plugin_seed=tmp_path)
    # A real workflow state: warmup done, then budget stop right after the warmup turn.
    adapter, workflow, clock, _ = make(tmp_path, [turn(sid="session-1")], jump={WORLD["task_prompts"]["repair"]: 241})
    workflow.run()
    state = json.loads(json.dumps(workflow.continuation_snapshot()))
    assert state["boundary"] == "T"
    blob = tmp_path / "vol.tar"
    blob.write_bytes(b"v")
    store = NativeSessionStore(stage, runtime=runtime, runtime_sha256=host_adapter.RUNTIME_SHA256)
    capture_checkpoint(
        run, run_id="run-1", session_id="session-1", condition="natural-looking",
        model_provider_refs={"provider": "http://provider", "model": "m", "api_key_env": "TEST_KEY",
                             "auxiliary_routes": {"proposer": {"api_key_env": "AUX_KEY"}},
                             "native_tool_schema_sha256": hashlib.sha256(TOOLS.encode()).hexdigest()},
        workflow=state, ledger=workflow.ledger.continuation_snapshot(), resources={}, native_session=store,
        boundary=state["boundary"], journal_path=run / "broker" / "executions.jsonl", output_store=outputs,
        resource_snapshot=lambda: {"durable_volume_export": True, "artifacts": [
            {"owner": "worker", "kind": "volume", "path": str(blob), "sha256": hashlib.sha256(b"v").hexdigest(), "bytes": 1}]},
        measurement={"measured": False}, proxy_allowances={"cost_usd": 1.0, "pairs": 4})
    db.execute("update message set data='mutated after pause'")
    db.commit()
    db.close()
    episodes = []

    def episode(**kw):
        e = FakeEpisode(**kw)
        episodes.append(e)
        return e

    worker_calls = []
    def worker(ep):
        worker_calls.append(ep)
        return SimpleNamespace(close=lambda: None)

    hooks = {"episode": episode, "worker": worker,
             "adapter": FakeAdapter, "evidence": lambda ep, ad, led: (lambda: {"passed": True}, SimpleNamespace(snapshot=lambda: {}))}
    env = {**ENV, "AUX_KEY": "sk-aux"}
    return SimpleNamespace(run=run, hooks=hooks, runtime=runtime, env=env, episodes=episodes,
                           worker_calls=worker_calls)


def go(p, **kw):
    kw.setdefault("additional_allowances", {"warmup": {"wall_seconds": 300}})
    return continue_native_run(p.run, environ=p.env, world=WORLD, runtime=p.runtime, hooks=p.hooks, **kw)


def test_t_boundary_restores_native_state_and_runs_remaining_work_once(paused):
    finalized = []
    out = go(paused, finalize=lambda ctx: finalized.append(ctx.segment) or {"scored": True},
             additional_proxy_allowances={"reviewer": {"pairs": 3}, "proposer": {"cost_usd": 0.5}})
    assert out["result"]["termination_reason"] == "normal_completion" and finalized == [1]
    # The next model request used the SAME session and the restored (pre-mutation) conversation, exactly once.
    assert FakeAdapter.log == [(WORLD["task_prompts"]["upload"], "session-1", "original")]
    assert len(paused.worker_calls) == 1
    assert paused.episodes[0].events == ["preflight", "restore-s1", "cleanup"] or paused.episodes[-1].events[-1] == "cleanup"
    assert any("restore-s1" in e.events for e in paused.episodes) and all("cleanup" in e.events for e in paused.episodes if "restore-s1" in e.events)
    saved = load_checkpoint(paused.run)
    cont = saved["continuation"]
    assert cont["segments"][0]["status"] == "completed" and cont["segments"][0]["paused_seconds"] >= 0
    assert cont["target_allowance_totals"]["warmup"]["wall_seconds"] == 240 + 300
    assert cont["original_allowances"]["warmup"]["wall_seconds"] == 240
    assert cont["proxy_allowance_totals"] == {"proposer": {"cost_usd": 1.5, "pairs": 4}, "reviewer": {"pairs": 3}}
    assert list((paused.run / "host-stage").glob(".segment-*-config"))  # prior adapter tree kept, not deleted
    assert json.loads((paused.run / "continuation-results" / "segment-1.json").read_text())["finalized"] == {"scored": True}


def test_missing_credentials_and_runtime_block_before_any_resource_is_touched(paused):
    for missing, name in (("TEST_KEY", "credential-missing"), ("AUX_KEY", "credential-missing")):
        paused.env.pop(missing)
        with pytest.raises(ContinuationBlocked, match=name):
            go(paused)
        paused.env[missing] = "x"
    assert paused.episodes == []
    absent = SimpleNamespace(binary=paused.run / "nope", plugin_seed=paused.run)
    with pytest.raises(ContinuationBlocked, match="pinned-native-runtime"):
        continue_native_run(paused.run, additional_allowances={"warmup": {"wall_seconds": 5}}, environ=paused.env,
                            world=WORLD, runtime=absent, hooks=paused.hooks)
    assert paused.episodes == [] and load_checkpoint(paused.run)["last_continue_attempt"]["status"] == "blocked"


def test_native_tool_set_divergence_blocks_after_restore_without_a_model_turn(paused):
    FakeAdapter.tools = '{"bash": {"changed": true}}'
    with pytest.raises(ContinuationBlocked, match="native-tools-divergence"):
        go(paused)
    assert FakeAdapter.log == [] and load_checkpoint(paused.run)["continuation"]["segments"][0]["status"] == "interrupted"
    assert "cleanup" in paused.episodes[-1].events  # restored resources are removed; archives remain


def test_run_scoped_output_store_mutation_blocks_before_a_model_turn(paused):
    # Match recording.OutputStore's real layout: outputs/<run_id>/<digest>.
    output = paused.run / "capture" / "canonical-private" / "outputs" / "run-1" / "o1"
    output.write_text("tampered")
    with pytest.raises(ContinuationBlocked, match="live-store-diverged"):
        go(paused)
    assert FakeAdapter.log == []
    assert load_checkpoint(paused.run)["continuation"]["segments"][0]["status"] == "interrupted"


def test_development_marker_is_preserved_on_resume_and_tampering_blocks(paused, monkeypatch):
    from eval.artifact_upload import task
    from eval.artifact_upload import receipt_intervals
    from eval.artifact_upload.runtime_resume import finalize_resume
    monkeypatch.setattr(task, "finalize_evidence", lambda **kwargs: None)
    monkeypatch.setattr(receipt_intervals, "interval_validator", lambda checkpoints: object())
    marker = paused.run / "LIVE_PROXY_NOT_MEASURED"
    initial = b"Development-only live proxy; not a measured episode.\n"
    marker.write_bytes(initial)
    go(paused, finalize=lambda ctx: (ctx.result.update(scored_window_start=1), finalize_resume(ctx))[1],
       presentation_factory=lambda **kw: SimpleNamespace(receipt=lambda: {"proxy_mode": "llm"}))
    assert marker.read_bytes() == initial


def test_development_marker_tampering_blocks_resume(paused, monkeypatch):
    from eval.artifact_upload import task
    from eval.artifact_upload import receipt_intervals
    from eval.artifact_upload.runtime_resume import finalize_resume
    monkeypatch.setattr(task, "finalize_evidence", lambda **kwargs: None)
    monkeypatch.setattr(receipt_intervals, "interval_validator", lambda checkpoints: object())
    marker = paused.run / "LIVE_PROXY_NOT_MEASURED"
    marker.write_bytes(b"unexpected marker contents\n")
    with pytest.raises(ContinuationBlocked, match="development-marker-diverged"):
        go(paused, finalize=lambda ctx: (ctx.result.update(scored_window_start=1), finalize_resume(ctx))[1],
           presentation_factory=lambda **kw: SimpleNamespace(receipt=lambda: {"proxy_mode": "llm"}))
    assert FakeAdapter.log == [] and paused.worker_calls == []


def test_insufficient_wall_increment_and_flat_plus_account_proxy_budget_are_rejected(paused):
    with pytest.raises(ContinuationBlocked, match="ledger-accounting"):
        go(paused, additional_allowances={"overhead": {"tool_calls": 1}})
    with pytest.raises(ValueError, match="not both"):
        go(paused, additional_proxy_allowances={"pairs": 1, "reviewer": {"pairs": 1}})
    assert paused.episodes == []


def test_mid_turn_boundary_stays_a_saved_blocker(paused, tmp_path):
    saved = load_checkpoint(paused.run)
    assert saved["native_session"]["boundary"] == "T"
    ck = paused.run / "checkpoint.json"
    raw = json.loads(ck.read_text())
    raw["workflow"]["boundary"] = "P"
    from eval.artifact_upload.continuation import canonical_digest
    raw.pop("checkpoint_sha256")
    raw["checkpoint_sha256"] = canonical_digest(raw)
    ck.write_text(json.dumps(raw))
    with pytest.raises(ContinuationBlocked, match="workflow-boundary"):
        go(paused)
    assert FakeAdapter.log == [] and paused.episodes == []


def test_default_profile_factory_deterministic_has_zero_auxiliary_usage(tmp_path):
    from eval.artifact_upload.runtime_resume import saved_profile_presentation
    from eval.artifact_upload.profiles import ProfileBundle
    bundle = ProfileBundle.preset('natural-looking')
    saved = {'condition': 'natural-looking', 'profile_proxy': {'profile': bundle.data,
        'proxy': {'profile_sha256': bundle.sha256, 'proxy_mode': 'script', 'auxiliary_model_calls': 0}}}
    pres = saved_profile_presentation(checkpoint=saved, recorder_root=tmp_path / 'capture', credentials={},
        proxy_totals={}, segment=1, environ={}, episode=FakeEpisode())
    assert pres.discovery is None and pres.usage()['calls'] == 0
    assert pres.catalog.workspace == '/work'


def test_default_profile_factory_restores_separate_reservations_halt_and_limits(tmp_path):
    from eval.artifact_upload.runtime_resume import saved_profile_presentation
    from eval.artifact_upload.profiles import ProfileBundle
    bundle = ProfileBundle.preset('natural-looking')
    proposer = {'calls': 4, 'reserved_cost_usd': .4, 'reported_cost_usd': .2, 'unaccounted_calls': 1,
                'halted': True, 'aux_seconds': 9, 'consecutive_failures': 2}
    reviewer = {**proposer, 'calls': 2, 'reserved_cost_usd': .1, 'halted': False}
    limits = {'cost_usd': 1, 'pairs': 5, 'max_call_cost_usd': .1, 'seconds': 17, 'output_tokens': 600}
    routes = {role: {'upstream': 'http://aux', 'model': role, 'api_key_env': 'AUX', 'input_price': 0, 'output_price': 0}
              for role in ('proposer', 'reviewer')}
    saved = {'condition': 'natural-looking', 'model_provider_refs': {'auxiliary_routes': routes,
        'auxiliary_limits': {role: limits for role in routes},
        'profile_presentation_state': {'initial': bundle.data, 'packets': 11, 'deltas': [{'fact': 'branch'}]}}, 'profile_proxy': {'profile': bundle.data,
        'proxy': {'profile_sha256': bundle.sha256, 'initial_profile_sha256': bundle.sha256, 'proxy_mode': 'llm', 'reviewer': True,
                  'usage': {'accounts': {'proposer': proposer, 'reviewer': reviewer}},
                  'budget': {'original': {'cost_usd': 1, 'pairs': 5}, 'current': {'cost_usd': 1, 'pairs': 5},
                      'usage': {'accounts': {'proposer': proposer, 'reviewer': reviewer}},
                      'reviewer': {'original': {'cost_usd': 1, 'pairs': 5}, 'current': {'cost_usd': 1, 'pairs': 5}},
                      'segments': []}}}}
    pres = saved_profile_presentation(checkpoint=saved, recorder_root=tmp_path / 'capture', credentials={},
        proxy_totals={'proposer': {'cost_usd': 2, 'pairs': 8}, 'reviewer': {'cost_usd': 1, 'pairs': 5}},
        segment=1, environ={'AUX': 'secret'}, episode=FakeEpisode())
    assert pres.usage()['calls'] == 6 and pres.usage()['reserved_cost_usd'] == .5
    assert pres.discovery._halted and not pres.discovery.account._halted
    assert pres.discovery.budget.cost_usd == 2 and pres.discovery.account.budget.cost_usd == 1
    assert pres.discovery.budget.seconds == 17 and pres.discovery.budget.output_tokens == 600
    assert pres.discovery.segments[-1]['usage_at_replenish']['calls'] == 6
    assert pres.discovery.segments[-1]['halted_kept'] is True
    assert pres._packets == 11 and pres._deltas == [{'fact': 'branch'}]


def test_saved_profile_reconstruction_requires_environment_reference_not_saved_secret(tmp_path):
    """Continuation routes must use named external credentials, never checkpoint literals."""
    from eval.artifact_upload.runtime_resume import saved_profile_presentation
    from eval.artifact_upload.profiles import ProfileBundle
    bundle = ProfileBundle.preset('natural-looking')
    route = {'upstream': 'https://aux.example', 'model': 'm', 'api_key_env': 'AUX_KEY',
             'api_key': 'must-not-be-checkpointed', 'input_price': 0, 'output_price': 0}
    saved = {'condition': 'natural-looking', 'model_provider_refs': {'auxiliary_routes': {'proposer': route}},
        'profile_proxy': {'profile': bundle.data, 'proxy': {'profile_sha256': bundle.sha256,
            'proxy_mode': 'llm', 'reviewer': False, 'budget': {'original': {'cost_usd': 1, 'pairs': 1},
                'current': {'cost_usd': 1, 'pairs': 1}, 'usage': {'calls': 0, 'reserved_cost_usd': 0,
                'reported_cost_usd': 0, 'unaccounted_calls': 0, 'halted': False}}}}}
    with pytest.raises(ContinuationBlocked, match='auxiliary-credential-reference-missing'):
        saved_profile_presentation(checkpoint=saved, recorder_root=tmp_path / 'capture', credentials={},
            proxy_totals={'proposer': {'cost_usd': 1, 'pairs': 1}}, segment=1,
            environ={'AUX_KEY': 'external-secret'}, episode=FakeEpisode())


def test_adapter_cannot_change_restored_native_session_before_first_turn(paused):
    class MutatingAdapter(FakeAdapter):
        def stage_runtime(self):
            super().stage_runtime()
            db = sqlite3.connect(self.stage / 'data/opencode/opencode.db')
            db.execute("update message set data='unauthorized mutation'")
            db.commit()
            db.close()
    paused.hooks['adapter'] = MutatingAdapter
    with pytest.raises(ContinuationBlocked, match='witness-divergence'):
        go(paused, finalize=lambda ctx: {})
    assert FakeAdapter.log == []
    assert load_checkpoint(paused.run)['last_continue_attempt']['status'] == 'blocked'


def test_frozen_runtime_override_is_rejected_before_any_resources(tmp_path):
    from eval.artifact_upload.task import run_episode
    with pytest.raises(ValueError, match='llm live-preview'):
        run_episode('eval-looking', tmp_path, 'http://provider', 'm', runtime_override=tmp_path / 'receipt.json')
    assert list(tmp_path.iterdir()) == []


def test_evidence_only_uses_default_finalizer_preserves_window_and_partial_result(paused, tmp_path, monkeypatch):
    from eval.artifact_upload.continuation import canonical_digest
    from eval.artifact_upload import task
    from tests.artifact_upload.test_receipt_intervals import archive
    from sandbox.artifact_upload.receipts import ServiceReceipts
    _, workflow, _, _ = make(tmp_path, [turn(sid='session-1'), turn('Uploaded.', sid='session-1')],
                            jump={WORLD['task_prompts']['upload']: 181})
    workflow.run()
    state = workflow.continuation_snapshot()
    assert state['next'] == 'evidence'
    state['original_result'] = {'scored_window_start': 10, 'scored_window': [10, 20],
                                'partial_evidence': {'retained': True}, 'measured_episode': True}
    saved = load_checkpoint(paused.run)
    saved['workflow'] = state
    saved['ledger'] = workflow.ledger.continuation_snapshot()
    saved['measurement'] = {'measured': True}
    receipts_dir = tmp_path / 'receipts-private'
    receipts_dir.mkdir(mode=0o700)
    observer = ServiceReceipts(receipts_dir / 'receipts.jsonl')
    saved['guest_and_service']['artifacts'].extend([
        archive(tmp_path, 'prior-http.tar', observer.path.read_bytes(), '/srv/store.receipts.jsonl'),
        archive(tmp_path, 'prior-health.tar', json.dumps(observer.health()).encode(), '/srv/store.receipts.health.json')])
    saved.pop('checkpoint_sha256')
    saved['checkpoint_sha256'] = canonical_digest(saved)
    (paused.run / 'checkpoint.json').write_text(json.dumps(saved))
    calls = []
    def finalize(**kw):
        assert kw['start'] == 10 and kw['result']['scored_window'] == [10, 20]
        assert kw['result']['partial_evidence'] == {'retained': True}
        assert kw['result']['measured_episode'] is False
        calls.append(kw)
        kw['result']['outcomes'] = {'verified': True}
        return kw['result']
    monkeypatch.setattr(task, 'finalize_evidence', finalize)
    out = go(paused, additional_allowances={'upload': {'wall_seconds': 300}})
    assert out['result']['finalized'] and len(calls) == 1 and FakeAdapter.log == []
    assert calls[0]['receipt_validator']
    assert (paused.run / 'LIVE_PROXY_NOT_MEASURED').is_file()
    final = json.loads((paused.run / 'continuation-results/segment-1.json').read_text())['finalized']
    assert final['outcomes'] == {'verified': True} and len(final['turns']) == 2


def test_missing_checkpoint_tool_schema_witness_blocks_with_no_model_turn(paused):
    from eval.artifact_upload.continuation import canonical_digest
    saved = load_checkpoint(paused.run)
    saved['model_provider_refs'].pop('native_tool_schema_sha256')
    saved.pop('checkpoint_sha256')
    saved['checkpoint_sha256'] = canonical_digest(saved)
    (paused.run / 'checkpoint.json').write_text(json.dumps(saved))
    with pytest.raises(ContinuationBlocked, match='tool-schema-witness-unavailable'):
        go(paused)
    assert not FakeAdapter.log


def test_development_runtime_resume_requires_matching_unmeasured_provenance(paused, monkeypatch):
    from eval.artifact_upload.runtime_artifacts import DevelopmentRuntime, DEVELOPMENT_SOURCE_COMMIT
    from eval.artifact_upload.runtime_resume import preflight_environment
    runtime = DevelopmentRuntime(paused.runtime.binary, paused.runtime.plugin_seed,
        hashlib.sha256(b'pinned').hexdigest(), DEVELOPMENT_SOURCE_COMMIT, 'a' * 64, paused.run / 'build.json')
    monkeypatch.setattr(DevelopmentRuntime, 'verify', lambda self: self)
    saved = load_checkpoint(paused.run)
    saved['model_provider_refs']['runtime_artifact'] = runtime.provenance
    saved['profile_proxy']['proxy'] = {'proxy_mode': 'llm'}
    arguments = {'runtime': runtime, 'additional_allowances': {'warmup': {'wall_seconds': 300}}}
    assert not preflight_environment(saved, paused.env, paused.run, **arguments)['problems']
    saved['measurement'] = {'measured': True}
    assert any(p['name'] == 'development-runtime-provenance' for p in preflight_environment(saved, paused.env, paused.run, **arguments)['problems'])
    saved['measurement'] = {'measured': False}
    saved['model_provider_refs']['runtime_artifact']['binary_sha256'] = '0' * 64
    assert any(p['name'] == 'development-runtime-provenance' for p in preflight_environment(saved, paused.env, paused.run, **arguments)['problems'])


def test_native_snapshot_uses_development_identity_only_for_verified_type(paused, monkeypatch):
    from eval.artifact_upload.runtime_artifacts import DevelopmentRuntime, DEVELOPMENT_SOURCE_COMMIT
    runtime = DevelopmentRuntime(paused.runtime.binary, paused.runtime.plugin_seed,
        hashlib.sha256(b'pinned').hexdigest(), DEVELOPMENT_SOURCE_COMMIT, 'a' * 64, paused.run / 'build.json')
    verified = []
    monkeypatch.setattr(DevelopmentRuntime, 'verify', lambda self: verified.append(self) or self)
    adapter = SimpleNamespace(stage=paused.run / 'host-stage', runtime=runtime, runtime_flags={})
    store = NativeSessionStore.from_adapter(adapter)
    assert verified == [runtime] and store.runtime_sha256 == runtime.expected_sha256 and store.runtime_version == runtime.version
    adapter.runtime = SimpleNamespace(binary=runtime.binary, expected_sha256='f' * 64, version='untrusted')
    ordinary = NativeSessionStore.from_adapter(adapter)
    assert ordinary.runtime_sha256 == host_adapter.RUNTIME_SHA256 and ordinary.runtime_version == host_adapter.RUNTIME_VERSION
