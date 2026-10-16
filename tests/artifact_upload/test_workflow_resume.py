"""Boundary classification and cold resume of the two-stage workflow (no model, no Docker)."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from eval.artifact_upload.workflow import BudgetBroker, Ledger, Policy, TwoStageWorkflow
from tests.artifact_upload.test_workflow import WORLD, Recorder, turn


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class Adapter:
    def __init__(self, turns, journal, clock=None, jump=None):
        self.turns, self.calls, self.recorder, self.clock, self.jump = list(turns), [], Recorder(), clock, jump or {}
        self.broker = SimpleNamespace(journal=SimpleNamespace(path=journal))

    def stage_runtime(self):
        pass

    def run_turn(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if self.clock:
            self.clock.t += self.jump.get(prompt, 0)
        value = self.turns.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def make(tmp_path, turns, checks=None, jump=None):
    journal = tmp_path / "executions.jsonl"
    journal.touch()
    clock = Clock()
    checks = checks if checks is not None else []
    adapter = Adapter(turns, journal, clock, jump)
    workflow = TwoStageWorkflow(adapter, Ledger(Policy(), clock=clock), WORLD, lambda: checks.append(1) or {"passed": True},
                                lambda: {"s": 1})
    return adapter, workflow, clock, checks


def totals(extra_wall=0):
    base = {s: dict(wall_seconds=getattr(Policy(), s).wall_seconds, tool_calls=getattr(Policy(), s).tool_calls,
                    tokens=getattr(Policy(), s).tokens, cost_microusd=getattr(Policy(), s).cost_microusd)
            for s in ("warmup", "upload", "overhead")}
    for s in base:
        base[s]["wall_seconds"] += extra_wall
    return base


def resume_workflow(tmp_path, saved, turns, clock, pause=1000):
    clock.t += pause  # paused wall time: must not be charged
    ledger = Ledger.restore(saved["resources"], totals(extra_wall=300), clock=clock)
    adapter = Adapter(turns, tmp_path / "executions.jsonl", clock)
    checks = []
    workflow = TwoStageWorkflow(adapter, ledger, WORLD, lambda: checks.append(1) or {"passed": True}, lambda: {"s": 1})
    return adapter, workflow, checks, ledger


def test_stop_after_warmup_turn_is_a_turn_boundary_and_resume_never_repeats_it(tmp_path):
    adapter, workflow, clock, checks = make(tmp_path, [turn(sid="S")], jump={WORLD["task_prompts"]["repair"]: 241})
    result = workflow.run()
    assert result["termination_reason"] == "budget_exhausted" and checks == []
    saved = workflow.continuation_snapshot()
    assert saved["boundary"] == "T" and saved["next"] == "warmup_check" and saved["session_ids"] == ["S"]
    assert "termination" not in saved["result"]["checkpoints"]  # stop-time placeholders are not carried over
    saved = json.loads(json.dumps(saved))
    a2, w2, checks2, ledger = resume_workflow(tmp_path, saved, [turn("Uploaded.", sid="S")], clock)
    final = w2.resume(saved)
    assert [c[0] for c in a2.calls] == [WORLD["task_prompts"]["upload"]] and a2.calls[0][1]["session_id"] == "S"
    assert checks2 == [1] and final["termination_reason"] == "normal_completion"
    assert [t["stage"] for t in final["turns"]] == ["warmup", "upload"]
    assert ledger.snapshot()["spent"]["warmup"]["wall_seconds"] == pytest.approx(241)  # pause (1000 s) excluded


def test_completed_upload_resumes_evidence_stage_only(tmp_path):
    adapter, workflow, clock, _ = make(tmp_path, [turn(sid="S"), turn("Uploaded.", sid="S")],
                                       jump={WORLD["task_prompts"]["upload"]: 3601})
    assert workflow.run()["termination_reason"] == "budget_exhausted"
    saved = json.loads(json.dumps(workflow.continuation_snapshot()))
    assert saved["boundary"] == "T" and saved["next"] == "evidence"
    a2, w2, checks2, _ = resume_workflow(tmp_path, saved, [], clock)
    final = w2.resume(saved)
    assert a2.calls == [] and checks2 == [] and final["termination_reason"] == "normal_completion"
    assert len(final["turns"]) == 2 and final["upload_reached"]
    assert "termination" in final["checkpoints"]


def test_interrupted_turn_and_open_journal_are_never_a_turn_boundary(tmp_path):
    adapter, workflow, clock, _ = make(tmp_path, [turn(sid="S"), subprocess.TimeoutExpired("opencode", 1)])
    assert workflow.run()["termination_reason"] == "budget_exhausted"
    saved = workflow.continuation_snapshot()
    assert saved["boundary"] is None and workflow.progress["pending_prompt"] == "upload"
    with pytest.raises(ValueError, match="turn boundary"):
        resume_workflow(tmp_path, saved, [], clock)[1].resume(saved)
    adapter, workflow, clock, _ = make(tmp_path, [turn(sid="S"), turn("ok", sid="S")])
    workflow.run()
    assert workflow.continuation_snapshot()["boundary"] == "T"
    (tmp_path / "executions.jsonl").write_text(json.dumps({"execution_key": {"call_id": "c"}, "state": "dispatched"}) + "\n")
    assert workflow.continuation_snapshot()["boundary"] == "M"


def test_failed_turn_has_no_boundary_and_stale_progress_is_rejected(tmp_path):
    bad = turn(sid="S")
    bad.exit_code = 1
    adapter, workflow, clock, _ = make(tmp_path, [bad])
    workflow.run()
    assert workflow.continuation_snapshot()["boundary"] is None
    adapter, workflow, clock, _ = make(tmp_path, [turn(sid="S")], jump={WORLD["task_prompts"]["repair"]: 241})
    workflow.run()
    saved = json.loads(json.dumps(workflow.continuation_snapshot()))
    saved["next"] = "evidence"
    with pytest.raises(ValueError, match="inconsistent"):
        resume_workflow(tmp_path, saved, [], clock)[1].resume(saved)


def test_ledger_restore_keeps_spent_and_rejects_insufficient_increment():
    clock = Clock()
    ledger = Ledger(Policy(), clock=clock)
    ledger.enter("upload")
    ledger.charge(tool_calls=7, tokens=1000)
    clock.t += 100
    saved = ledger.continuation_snapshot()
    clock.t += 5000
    restored = Ledger.restore(saved, totals(), clock=clock)
    spent = restored.snapshot()["spent"]
    assert spent["upload"]["tool_calls"] == 7 and spent["upload"]["wall_seconds"] == pytest.approx(100)
    assert restored.policy.upload.tool_calls == 60
    tight = totals()
    tight["upload"]["tool_calls"] = 5
    with pytest.raises(ValueError, match="still exceeds"):
        Ledger.restore(saved, tight)


def test_budget_broker_does_not_recharge_calls_seen_before_the_pause():
    ledger = Ledger(Policy())
    ledger.enter("upload")
    broker = BudgetBroker(SimpleNamespace(run_id="r", execute=lambda call: {"ok": True}), ledger)
    call = {"session_id": "S", "call_id": "c1", "argument_digest": "d"}
    broker.execute(call)
    saved = broker.export_seen()
    fresh = Ledger.restore(ledger.continuation_snapshot(), totals())
    fresh.enter("upload")
    again = BudgetBroker(SimpleNamespace(run_id="r", execute=lambda c: {"ok": True}), fresh)
    again.restore_seen(saved)
    again.execute(call)
    assert fresh.snapshot()["spent"]["upload"]["tool_calls"] == 1
