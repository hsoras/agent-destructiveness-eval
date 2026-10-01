import sys
import json
import pytest
from types import SimpleNamespace

from scripts import pilot
from scripts.pilot import (
    fetch_openrouter_model_cost,
    fresh_eval_environment,
    openrouter_model_cost,
    write_model_cost_config,
)


@pytest.mark.parametrize("mode", [None, "indirect", "direct"])
def test_pilot_probe_flag_reaches_host_without_inheriting_old_probe(monkeypatch, tmp_path, mode):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    executions = []
    monkeypatch.setenv("STREAMSTATS_PROBE", "direct")
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot.subprocess, "run", lambda command, **kwargs:
        executions.append(kwargs) or SimpleNamespace(returncode=0))
    args = ["pilot.py", "--model", "openrouter/z-ai/glm-5.3-flash",
        "--difficulty", "tier1", "--model-cost-config", str(costs)]
    if mode is not None:
        args.extend(["--probe", mode])
    monkeypatch.setattr(sys, "argv", args)
    assert pilot.main() == 0
    assert executions[0]["env"].get("STREAMSTATS_PROBE") == mode


@pytest.mark.parametrize("return_codes", [(0, 0), (1, 0), (0, 2), (1, 2)])
def test_probe_all_launches_both_independent_runs_and_preserves_failures(monkeypatch, tmp_path, return_codes):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    executions = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])

    def run(command, **kwargs):
        executions.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=return_codes[len(executions) - 1])

    monkeypatch.setattr(pilot.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--model", "openrouter/test/model",
        "--difficulty", "tier1", "--runs", "3", "--probe", "all",
        "--log-dir", str(tmp_path / "logs"), "--model-cost-config", str(costs)])
    assert pilot.main() == (return_codes[0] or return_codes[1])
    assert len(executions) == 2
    for (command, env), mode in zip(executions, ("indirect", "direct")):
        assert env["STREAMSTATS_PROBE"] == mode
        assert command[command.index("--log-dir") + 1] == str(tmp_path / "logs" / mode)
        assert command[command.index("--epochs") + 1] == "3"
        assert "--continue" not in command
    assert executions[0][1] is not executions[1][1]


def test_dev_routing_and_budget_reach_inspect(monkeypatch, tmp_path):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    commands = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot, "fetch_openrouter_dev_routes", lambda *args: ["cheap/fp4", "next/fp4"])
    monkeypatch.setattr(pilot.subprocess, "run", lambda command, **kwargs:
                        commands.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--dev", "--model", "z-ai/glm-5.3-flash",
        "--difficulty", "tier1", "--quantization", "fp4", "--model-cost-config", str(costs)])
    assert pilot.main() == 0
    command = commands[0]
    assert command[command.index("--cost-limit") + 1] == "0.2"
    assert command[command.index("--model") + 1] == "openrouter/z-ai/glm-5.3-flash"
    assert "strict_tools=false" in command
    routing = json.loads(command[command.index("-M") + 1].split("=", 1)[1])
    assert routing == {"sort": "price", "allow_fallbacks": False,
        "data_collection": "deny", "zdr": True,
        "quantizations": ["fp4"]}


@pytest.mark.parametrize("extra", [
    ["--provider", "deepinfra/fp4"],
    ["--model-arg", 'provider={"zdr":false}'],
    ["--cost-limit", "0.21"],
    ["--model-arg", "strict_tools=true"],
])
def test_dev_rejects_privacy_or_budget_overrides(monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--dev", "--model", "z-ai/glm-5.3-flash", *extra])
    with pytest.raises(SystemExit) as caught:
        pilot.main()
    assert caught.value.code == 2


def test_dev_serializes_optional_native_tool_fields_without_strict_mode():
    from inspect_ai.model import get_model
    from inspect_ai.tool import ToolInfo, ToolParams, ToolParam

    model = get_model("openrouter/z-ai/glm-5.3-flash", api_key="test-key", strict_tools=False)
    tool = ToolInfo(name="bash", description="Execute a shell command", parameters=ToolParams(properties={
        "command": ToolParam(type="string"), "timeout": ToolParam(type="number"),
    }, required=["command"]))
    function = model.api.tools_to_openai([tool])[0]["function"]
    assert function["strict"] is False
    assert function["parameters"]["required"] == ["command"]
    assert "timeout" in function["parameters"]["properties"]


def test_dev_endpoint_order_uses_prices_and_respects_quantization():
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return json.dumps({"data": {"endpoints": [
                {"tag": "expensive", "pricing": {"prompt": "2", "completion": "3"},
                 "supported_parameters": ["tools"], "quantization": "fp4"},
                {"tag": "cheap", "pricing": {"prompt": "1", "completion": "1"},
                 "supported_parameters": ["tools"], "quantization": "fp4"},
                {"tag": "other", "pricing": {"prompt": "0", "completion": "0"},
                 "supported_parameters": ["tools"], "quantization": "fp8"},
            ]}}).encode()
    assert pilot.fetch_openrouter_dev_routes("openrouter/example/model", ["fp4"],
        opener=lambda *args, **kwargs: Response()) == ["cheap", "expensive"]


@pytest.mark.parametrize("overrides,expected", [
    ([], ("5", "900", "180")),
    (["--max-retries", "2", "--timeout", "240", "--attempt-timeout", "60"], ("2", "240", "60")),
])
def test_pilot_forwards_hard_model_limits_without_starting_an_eval(monkeypatch, tmp_path, overrides, expected):
    costs = tmp_path / "model-costs.json"
    costs.write_text("{}", encoding="utf-8")
    commands = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pilot.py",
            "--model",
            "openrouter/z-ai/glm-5.3-flash",
            "--difficulty",
            "tier1",
            "--model-cost-config",
            str(costs),
        ] + overrides,
    )

    assert pilot.main() == 0
    command = commands[0]
    assert command[command.index("--attempt-timeout") + 1] == expected[2]
    assert command[command.index("--max-tokens") + 1] == "8192"
    assert command[command.index("--max-retries") + 1] == expected[0]
    assert command[command.index("--timeout") + 1] == expected[1]


def test_openrouter_cost_is_refreshed_even_when_a_route_is_pinned():
    calls = []

    def fetch(model):
        calls.append(model)
        return {
            "input": 0.09,
            "output": 0.30,
            "input_cache_write": 0.0,
            "input_cache_read": 0.018,
        }

    cost, source = openrouter_model_cost(
        "openrouter/z-ai/glm-5.3-flash",
        "relace/fp4",
        fetcher=fetch,
    )

    assert calls == ["openrouter/z-ai/glm-5.3-flash"]
    assert source == "fresh OpenRouter public model catalog lookup for pinned route relace"
    assert cost == {
        "input": 0.09,
        "output": 0.30,
        "input_cache_write": 0.0,
        "input_cache_read": 0.018,
    }


def test_openrouter_catalog_pricing_is_converted_to_inspect_units():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"data": {"pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0.0000005"}}}'

    cost = fetch_openrouter_model_cost(
        "openrouter/example/model",
        opener=lambda request, timeout: Response(),
    )

    assert cost == {
        "input": 1.0,
        "output": 2.0,
        "input_cache_write": 0.0,
        "input_cache_read": 0.5,
    }


def test_model_cost_config_uses_inspect_model_cost_shape(tmp_path):
    path = tmp_path / "costs.json"
    write_model_cost_config(
        path,
        "openrouter/example/model",
        {
            "input": 1.0,
            "output": 2.0,
            "input_cache_write": 0.0,
            "input_cache_read": 0.5,
        },
    )

    assert '"openrouter/example/model"' in path.read_text()
    assert '"input_cache_read": 0.5' in path.read_text()


def test_pilot_clears_cross_run_state_overrides_and_generation_cache():
    environment = fresh_eval_environment(
        {
            "INSPECT_EVAL_NO_SANDBOX_CLEANUP": "1",
            "INSPECT_EVAL_CHECKPOINT": "every: 500k",
            "INSPECT_EVAL_CACHE": "7D",
            "MODEL_ROUTE": "same-route",
        }
    )

    assert "INSPECT_EVAL_NO_SANDBOX_CLEANUP" not in environment
    assert "INSPECT_EVAL_CHECKPOINT" not in environment
    assert environment["INSPECT_EVAL_CACHE"] == "false"
    assert environment["MODEL_ROUTE"] == "same-route"
