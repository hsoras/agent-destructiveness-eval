import asyncio
from copy import deepcopy

import pytest
from inspect_ai.agent import AgentState
from inspect_ai.model import ChatMessageAssistant, ChatMessageUser, ModelOutput
from inspect_ai.tool import ToolCall

from eval.model_requests import ModelRequestGuard
from eval.probes import PROBE_PROMPTS, configured_probe, run_evaluation_probe


class MemoryStore:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = deepcopy(value)


@pytest.mark.parametrize("mode", ["indirect", "direct"])
def test_probe_continues_history_without_mutating_scored_state_or_executing_tools(mode):
    state = AgentState(messages=[
        ChatMessageUser(content="Fix the repository."),
        ChatMessageAssistant(content="Fixed the bug; validation is blocked."),
    ])
    state.output = ModelOutput.from_content("test/model", state.messages[-1].text)
    original = [m.model_dump() for m in state.messages]
    original_output = state.output
    calls = []

    class Model:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            # A provider mutation must not leak into the scored history.
            kwargs["input"][0].content = "mutated"
            return ModelOutput.from_content("test/model", "I was uncertain.")

    record_store = MemoryStore()
    asyncio.run(run_evaluation_probe(state, mode=mode, model=Model(),
        request_guard=ModelRequestGuard(), record_store=record_store))
    assert [m.model_dump() for m in state.messages] == original
    assert state.output is original_output
    assert len(calls) == 1
    assert calls[0]["tools"] == []
    assert calls[0]["tool_choice"] == "none"
    assert calls[0]["input"][-2].text == "Fixed the bug; validation is blocked."
    assert calls[0]["input"][-1].text == PROBE_PROMPTS[mode]
    record = record_store.values["evaluation_probe"]
    assert record["status"] == "completed"
    assert record["answer"] == "I was uncertain."
    assert record["response"]["role"] == "assistant"


def test_probe_failure_does_not_mark_original_request_guard_failed():
    class Model:
        async def generate(self, **kwargs):
            raise RuntimeError("diagnostic provider failure")

    guard = ModelRequestGuard()
    record_store = MemoryStore()
    state = AgentState(messages=[ChatMessageAssistant(content="Done.")])
    asyncio.run(run_evaluation_probe(state, mode="direct", model=Model(),
        request_guard=guard, record_store=record_store))
    assert not guard.failed.is_set()
    assert guard.error is None
    assert guard.audit == []
    record = record_store.values["evaluation_probe"]
    assert record["status"] == "error"
    assert "diagnostic provider failure" in record["error"]
    assert record["request_audit"][0]["status"] == "failed"
    assert state.messages[-1].text == "Done."


def test_unexpected_probe_tool_calls_are_recorded_without_execution():
    class Model:
        async def generate(self, **kwargs):
            output = ModelOutput.from_content("test/model", "")
            output.message.tool_calls = [ToolCall(id="bad", function="bash",
                arguments={"command": "rm user-data"})]
            return output

    record_store = MemoryStore()
    state = AgentState(messages=[ChatMessageAssistant(content="Done.")])
    asyncio.run(run_evaluation_probe(state, mode="direct", model=Model(),
        request_guard=ModelRequestGuard(), record_store=record_store))
    record = record_store.values["evaluation_probe"]
    assert record["status"] == "error"
    assert "none were executed" in record["error"]
    assert state.messages[-1].tool_calls is None


def test_probe_skips_incomplete_agent_response():
    record_store = MemoryStore()
    state = AgentState(messages=[ChatMessageUser(content="Fix the repository.")])
    asyncio.run(run_evaluation_probe(state, mode="indirect", model=None,
        request_guard=ModelRequestGuard(), record_store=record_store))
    assert record_store.values["evaluation_probe"]["status"] == "skipped"


def test_probe_configuration_is_optional_and_validated(monkeypatch):
    monkeypatch.delenv("STREAMSTATS_PROBE", raising=False)
    assert configured_probe() is None
    monkeypatch.setenv("STREAMSTATS_PROBE", "indirect")
    assert configured_probe() == "indirect"
    monkeypatch.setenv("STREAMSTATS_PROBE", "invalid")
    with pytest.raises(ValueError, match="unknown probe"):
        configured_probe()


def test_task_passes_probe_to_adapter_and_records_mode(monkeypatch):
    import sys
    from types import SimpleNamespace
    from eval import task

    received = []
    monkeypatch.setenv("STREAMSTATS_PROBE", "direct")
    monkeypatch.setitem(sys.modules, "eval.opencode_adapter", SimpleNamespace(
        opencode=lambda **kwargs: received.append(kwargs) or "agent"))
    assert task._opencode_solver(scenario="development container") == "agent"
    assert received[0]["probe"] == "direct"
    assert task._runtime_metadata(scenario="development container", model=None,
        cost_limit=0.15)["probe"] == "direct"


def test_inspect_log_keeps_probe_separate_from_scored_answer(tmp_path, monkeypatch):
    from inspect_ai import Task, eval as evaluate
    from inspect_ai.dataset import Sample
    from inspect_ai.model import get_model
    from inspect_ai.scorer import match
    from inspect_ai.solver import solver
    from inspect_ai.util import store
    from inspect_ai._util import appdirs

    monkeypatch.setattr(appdirs, "user_data_path", lambda package: tmp_path / "data")
    monkeypatch.setattr(appdirs, "user_cache_path", lambda package: tmp_path / "cache")

    @solver
    def completed_task_with_probe():
        async def solve(state, generate):
            state.messages.append(ChatMessageAssistant(content="Original task answer"))
            state.output = ModelOutput.from_content("mockllm/model", "Original task answer")
            await run_evaluation_probe(state, mode="direct", model=get_model(),
                request_guard=ModelRequestGuard(), record_store=store())
            return state
        return solve

    model = get_model("mockllm/model", custom_outputs=[
        ModelOutput.from_content("mockllm/model", "Probe answer")], memoize=False)
    log = evaluate(Task(dataset=[Sample(input="Fix the repo", target="Original task answer")],
        solver=completed_task_with_probe(), scorer=match()), model=model,
        log_dir=str(tmp_path), display="none")[0]
    assert log.status == "success"
    sample = log.samples[0]
    assert sample.output.completion == "Original task answer"
    assert sample.messages[-1].text == "Original task answer"
    assert sample.scores["match"].value == "C"
    assert sample.store["evaluation_probe"]["answer"] == "Probe answer"
    events = [e for e in sample.events if e.event == "model"]
    assert len(events) == 1
    prompt = events[0].input[-1].text
    if prompt.startswith("attachment://"):
        prompt = sample.attachments[prompt.removeprefix("attachment://")]
    assert prompt == PROBE_PROMPTS["direct"]
