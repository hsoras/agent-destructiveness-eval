import sys
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from types import SimpleNamespace

from scripts import pilot
from scripts.pilot import (
    fetch_openrouter_model_cost,
    fresh_eval_environment,
    openrouter_model_cost,
    write_model_cost_config,
)


@pytest.mark.parametrize("disabled", [False, True])
def test_pilot_awareness_probes_default_and_opt_out(monkeypatch, tmp_path, disabled):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    executions = []
    monkeypatch.setenv("STREAMSTATS_PROBE", "direct")
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot.subprocess, "run", lambda command, **kwargs:
        executions.append(kwargs) or SimpleNamespace(returncode=0))
    args = ["pilot.py", "--model", "openrouter/z-ai/glm-5.3-flash",
        "--scenario", "development container", "--difficulty", "tier1",
        "--model-cost-config", str(costs)]
    if disabled:
        args.append("--no-awareness-probes")
    monkeypatch.setattr(sys, "argv", args)
    assert pilot.main() == 0
    assert executions[0]["env"].get("STREAMSTATS_AWARENESS_PROBES") == ("0" if disabled else "1")
    assert "STREAMSTATS_PROBE" not in executions[0]["env"]


@pytest.mark.parametrize("return_code", [0, 1])
def test_probe_all_does_not_duplicate_original_trajectory(monkeypatch, tmp_path, return_code):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    executions = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: [sys.executable, "scripts/inspect_cli.py"])

    def run(command, **kwargs):
        executions.append((command, kwargs["env"]))
        if "scripts/inspect_cli.py" in command:
            log_dir = Path(command[command.index("--log-dir") + 1])
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "sample.eval").write_text("placeholder")
            return SimpleNamespace(returncode=return_code)
        eval_log = Path(command[command.index("--eval-log") + 1])
        eval_log.with_suffix(".awareness.json").write_text("{}")
        eval_log.with_suffix(".awareness.md").write_text("report")
        return SimpleNamespace(returncode=0, stdout="sidecars written\n", stderr="")

    monkeypatch.setattr(pilot.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--model", "openrouter/test/model",
        "--scenario", "development container", "--difficulty", "tier1",
        "--runs", "3", "--probe", "all",
        "--log-dir", str(tmp_path / "logs"), "--model-cost-config", str(costs)])
    assert pilot.main() == return_code
    original_runs = [(command, env) for command, env in executions if "scripts/inspect_cli.py" in command]
    diagnostic_runs = [(command, env) for command, env in executions if "scripts.run_awareness_probes" in " ".join(command)]
    assert len(original_runs) == 1
    assert len(diagnostic_runs) == 1
    command, env = original_runs[0]
    assert command[command.index("--epochs") + 1] == "3"
    assert env["STREAMSTATS_AWARENESS_PROBES"] == "1"
    assert "--continue" not in command
    assert diagnostic_runs[0][0][diagnostic_runs[0][0].index("--eval-log") + 1].endswith("sample.eval")


def test_diagnostic_runner_failure_does_not_change_original_result(monkeypatch, tmp_path, capsys):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    calls = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: [sys.executable, "scripts/inspect_cli.py"])

    def run(command, **kwargs):
        calls.append(command)
        if "scripts/inspect_cli.py" in command:
            log_dir = Path(command[command.index("--log-dir") + 1])
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "sample.eval").write_text("placeholder")
            return SimpleNamespace(returncode=7)
        return SimpleNamespace(returncode=1, stdout="", stderr="probe failed")

    monkeypatch.setattr(pilot.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--model", "openrouter/test/model",
        "--scenario", "development container", "--difficulty", "tier1",
        "--log-dir", str(tmp_path / "logs"),
        "--model-cost-config", str(costs)])
    assert pilot.main() == 7
    assert len(calls) == 2
    captured = capsys.readouterr()
    assert "awareness diagnostics failed" in captured.err
    assert "did not create awareness JSON" in captured.err
    assert "did not create awareness report" in captured.err
    assert "awareness JSON:" not in captured.out
    assert "awareness report:" not in captured.out


@pytest.mark.parametrize("launcher", ["direct", "uv"])
def test_awareness_helper_real_cli_help_works_in_inspect_launch_modes(tmp_path, launcher):
    root = Path(pilot.__file__).resolve().parents[1]
    inspect_script = str(root / "scripts" / "inspect_cli.py")
    if launcher == "direct":
        inspect_command = [sys.executable, inspect_script]
    else:
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is not installed")
        inspect_command = [uv, "run", "--frozen", "python", inspect_script]
    command = [*pilot.awareness_probe_command(inspect_command), "--help"]
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    result = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--eval-log" in result.stdout
    assert "--model" in result.stdout


def test_awareness_opt_out_skips_sidecar_runner(monkeypatch, tmp_path):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    calls = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])

    def run(command, **kwargs):
        calls.append(command)
        log_dir = Path(command[command.index("--log-dir") + 1])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "sample.eval").write_text("placeholder")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pilot.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--model", "openrouter/test/model",
        "--scenario", "development container", "--difficulty", "tier1",
        "--no-awareness-probes", "--log-dir", str(tmp_path / "logs"),
        "--model-cost-config", str(costs)])
    assert pilot.main() == 0
    assert len(calls) == 1


def test_dev_routing_and_budget_reach_inspect(monkeypatch, tmp_path):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    commands = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot, "fetch_openrouter_dev_routes", lambda *args: ["cheap/fp4", "next/fp4"])
    monkeypatch.setattr(pilot.subprocess, "run", lambda command, **kwargs:
                        commands.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, "argv", ["pilot.py", "--dev", "--model", "z-ai/glm-5.3-flash",
        "--scenario", "development container", "--difficulty", "tier1",
        "--quantization", "fp4", "--model-cost-config", str(costs)])
    assert pilot.main() == 0
    command = commands[0]
    assert command[command.index("--cost-limit") + 1] == "0.2"
    assert command[command.index("--model") + 1] == "openrouter/z-ai/glm-5.3-flash"
    assert "strict_tools=false" in command
    routing = json.loads(command[command.index("-M") + 1].split("=", 1)[1])
    assert routing == {"sort": "price", "allow_fallbacks": False,
        "data_collection": "deny", "zdr": True,
        "quantizations": ["fp4"]}


@pytest.mark.parametrize("display,expected", [(None, "conversation"), ("full", "full")])
def test_pilot_forwards_display_to_inspect(monkeypatch, tmp_path, display, expected):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    commands = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot.subprocess, "run", lambda command, **kwargs:
        commands.append(command) or SimpleNamespace(returncode=0))
    args = ["pilot.py", "--model", "openrouter/test/model", "--scenario",
        "development container", "--difficulty", "tier1"]
    if display:
        args.extend(["--display", display])
    args.extend(["--model-cost-config", str(costs)])
    monkeypatch.setattr(sys, "argv", args)
    assert pilot.main() == 0
    command = commands[0]
    assert command[command.index("--display") + 1] == expected


def test_pilot_defaults_to_sqlite_with_privacy_and_non_strict_tools(monkeypatch, tmp_path):
    costs = tmp_path / "costs.json"
    costs.write_text("{}")
    commands = []
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot, "prepare_sqlite_migration_home", lambda *args: ("home", "home-volume"))
    monkeypatch.setattr(pilot, "prepare_opencode_runtime_disk", lambda *args: ("runtime", "runtime-volume", 58 * 1024 * 1024, 55 * 1024 * 1024))
    monkeypatch.setattr(pilot, "export_opencode_runtime_session", lambda *args: {})
    monkeypatch.setattr(pilot, "cleanup_opencode_runtime_disk", lambda *args: None)
    monkeypatch.setattr(pilot, "cleanup_sqlite_migration_home", lambda *args: None)
    monkeypatch.setattr(pilot.subprocess, "run", lambda command, **kwargs:
        commands.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(sys, "argv", [
        "pilot.py", "--model", "deepseek/deepseek-v4.1-flash",
        "--provider", "morph/fp8", "--tier", "1",
        "--no-awareness-probes", "--model-cost-config", str(costs),
    ])

    assert pilot.main() == 0
    command = commands[0]
    assert "eval/task.py@sqlite_migration" in command
    assert "condition=tier1-blocker" in command
    assert command[command.index("--display") + 1] == "conversation"
    model_args = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-M"]
    parsed = {key: json.loads(value) if value.startswith("{") else value
        for key, value in (argument.split("=", 1) for argument in model_args)}
    assert parsed["strict_tools"] == "false"
    assert parsed["provider"]["data_collection"] == "deny"
    assert parsed["provider"]["zdr"] is True


def test_runtime_session_export_resolves_repo_imports_when_launched_as_script(tmp_path, monkeypatch):
    state_dir = tmp_path / ".scenario/sqlite-migration-artifacts/disk-runtime-state"
    state_dir.mkdir(parents=True)
    (state_dir / "sample.json").write_text(json.dumps({"runtime_volume": "runtime-volume"}))
    monkeypatch.setattr(sys, "path", ["/repo/scripts"])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='{"captured":true,"sessions":[]}', stderr="")

    output = tmp_path / "logs/opencode-runtime-session-export.json"
    result = pilot.export_opencode_runtime_session(tmp_path, "sample", output, runner=run)

    assert result["captured"] is True
    assert json.loads(output.read_text())["captured"] is True
    assert calls[0][0:3] == ["docker", "run", "--rm"]
    assert str(Path(pilot.__file__).resolve().parents[1]) in sys.path


@pytest.mark.parametrize("extra", [
    ["--provider", "deepinfra/fp4"],
    ["--no-privacy"],
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
    ([], ("1", "600", "300", "131072")),
    (["--max-retries", "2", "--timeout", "240", "--attempt-timeout", "60",
      "--max-tokens", "200000"], ("2", "240", "60", "200000")),
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
                "--scenario",
                "development container",
                "--difficulty",
            "tier1",
            "--model-cost-config",
            str(costs),
        ] + overrides,
    )

    assert pilot.main() == 0
    command = commands[0]
    assert command[command.index("--attempt-timeout") + 1] == expected[2]
    assert command[command.index("--max-tokens") + 1] == expected[3]
    assert command[command.index("--max-retries") + 1] == expected[0]
    assert command[command.index("--timeout") + 1] == expected[1]
    assert command[command.index("--turn-limit") + 1] == "100"


def test_sqlite_task_keeps_matched_45_minute_sample_limit(monkeypatch):
    from inspect_ai.solver import chain_of_thought
    import eval.task as task_module
    monkeypatch.setattr(task_module, "_opencode_solver", lambda **_kwargs: chain_of_thought())
    monkeypatch.setenv("SQLITE_MIGRATION_CAPACITY_BYTES", "2172092416")
    monkeypatch.setenv("SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES", "26464256")
    monkeypatch.setenv("SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES", "25744384")
    monkeypatch.setenv("SQLITE_MIGRATION_RUNTIME_IMAGE_MIB", "64")

    tasks = [task_module._build_sqlite_migration_task(condition) for condition in (
        "clean-blocker", "tier1-blocker", "defect-blocker", "clean-capacity"
    )]
    assert {task.time_limit for task in tasks} == {2700}
    assert {task.turn_limit for task in tasks} == {100}
    assert {task.metadata["configuration_identity"].rsplit("-", 1)[-1] for task in tasks} == {"v13"}
    assert {task.metadata["sqlite_secure_delete_mode"] for task in tasks} == {"off"}
    assert {task.metadata["storage_capacity_status"] for task in tasks} == {
        "off_mode_2176_mib_required_verification_enospc_intact_three_pruned_pairs_passed"
    }
    assert [task.metadata["storage_capacity_bytes"] for task in tasks] == [
        2_172_092_416, 2_172_092_416, 2_172_092_416, 3_094_126_592
    ]
    assert tasks[0].metadata["task_filesystem_capacity_bytes"] == 2_172_092_416
    assert tasks[0].metadata["opencode_runtime_storage"] == {
        "mount_path": "/home/dev/.local/share/opencode",
        "filesystem": "disk-backed ext4",
        "default_image_mib": 64,
        "capacity_bytes": 26_464_256,
        "initial_free_bytes": 25_744_384,
        "ownership": "dev uid 1000 initialized before OpenCode starts",
        "cache_and_state_paths": "/home/dev/.local/share/opencode/cache and /home/dev/.local/share/opencode/state",
    }
    monkeypatch.setenv("SQLITE_MIGRATION_SECURE_DELETE", "default")
    default_task = task_module._build_sqlite_migration_task("clean-blocker")
    assert default_task.metadata["sqlite_secure_delete_mode"] == "default"
    assert default_task.metadata["storage_capacity_bytes"] == 2_731_851_776


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


def test_sqlite_migration_capacity_uses_measured_disk_capacities():
    for condition in ("defect-blocker", "tier1-blocker", "clean-blocker"):
        assert pilot.sqlite_migration_capacity(condition, "off") == "2172092416"
        assert pilot.sqlite_migration_capacity(condition, "default") == "2731851776"
        assert pilot.sqlite_migration_image_mib(condition, "off") == 2176
        assert pilot.sqlite_migration_image_mib(condition, "default") == 2720
    assert pilot.sqlite_migration_capacity("clean-capacity", "off") == "3094126592"
    assert pilot.sqlite_migration_capacity("clean-capacity", "default") == "3094126592"
    assert pilot.sqlite_migration_image_mib("clean-capacity", "off") == 3072
    assert pilot.sqlite_migration_image_mib("clean-capacity", "default") == 3072
    with pytest.raises(ValueError, match="unknown SQLite"):
        pilot.sqlite_migration_capacity("unknown")
    with pytest.raises(ValueError, match="secure_delete"):
        pilot.sqlite_migration_capacity("clean-blocker", "memory")
    with pytest.raises(ValueError, match="secure_delete"):
        pilot.sqlite_migration_image_mib("clean-blocker", "memory")


def test_sqlite_tier_flag_maps_to_correct_feature_variants():
    assert pilot.SQLITE_TIER_CONDITIONS == {
        "0": "clean-blocker",
        "1": "tier1-blocker",
        "2": "defect-blocker",
    }


def test_sqlite_container_checks_opencode_version_as_the_task_user():
    compose = (Path(__file__).resolve().parents[1] / "sandbox" / "compose.sqlite-migration.yaml").read_text(encoding="utf-8")
    assert "su -s /bin/sh dev -c '/opt/opencode/node_modules/.bin/opencode --version'" in compose


@pytest.mark.parametrize("runs", [1, 2])
@pytest.mark.parametrize("secure_delete_args,expected_mode", [
    ([], "off"),
    (["--sqlite-secure-delete", "default"], "default"),
])
def test_sqlite_pilot_prepares_fresh_volume_and_cleans_it_per_run(
    monkeypatch, tmp_path, runs, secure_delete_args, expected_mode
):
    monkeypatch.delenv("SQLITE_MIGRATION_HOME_VOLUME", raising=False)
    costs = tmp_path / "model-costs.json"
    costs.write_text("{}", encoding="utf-8")
    calls = []
    inspect_environments = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "bash" and command[2] == "prepare":
            if command[-1] == "runtime":
                label = command[-2]
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        f"SQLITE_MIGRATION_RUNTIME_VOLUME=sqlite-runtime-opencode-{label}\n"
                        "SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES=60817408\n"
                        "SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES=57671680\n"
                    ), stderr="",
                )
            label = command[-1]
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"SQLITE_MIGRATION_HOME_VOLUME=sqlite-block-home-{label}\n"
                    f"SQLITE_MIGRATION_CAPACITY_BYTES={'2731851776' if expected_mode == 'default' else '2172092416'}\n"
                ),
                stderr="",
            )
        if command[0] == "bash" and command[2] == "cleanup":
            return SimpleNamespace(returncode=0, stdout="cleaned\n", stderr="")
        if command[0] == "docker" and command[1:3] == ["ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="container-a\n", stderr="")
        if command[0] == "docker" and command[1:3] == ["rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="container-a\n", stderr="")
        if command[0] == "inspect":
            inspect_environments.append(kwargs["env"])
            return SimpleNamespace(returncode=0)
        pytest.fail(f"unexpected command: {command}")

    monkeypatch.setattr(sys, "argv", [
        "pilot.py", "--model", "z-ai/glm-5.3-flash", "--provider", "novita/fp8",
        "--scenario", "sqlite migration", "--condition", "defect-blocker",
        "--runs", str(runs), "--model-cost-config", str(costs), *secure_delete_args,
    ])
    monkeypatch.setattr(pilot, "resolve_inspect_command", lambda root: ["inspect"])
    monkeypatch.setattr(pilot.subprocess, "run", run)

    assert pilot.main() == 0

    prepare_calls = [call for call in calls if call[0] == "bash" and call[2] == "prepare" and call[-1] != "runtime"]
    runtime_prepare_calls = [call for call in calls if call[0] == "bash" and call[2] == "prepare" and call[-1] == "runtime"]
    cleanup_calls = [call for call in calls if call[0] == "bash" and call[2] == "cleanup" and len(call) == 4]
    runtime_cleanup_calls = [call for call in calls if call[0] == "bash" and call[2] == "cleanup" and len(call) == 5]
    docker_removal_calls = [call for call in calls if call[:3] == ["docker", "rm", "-f"]]
    assert len(prepare_calls) == len(cleanup_calls) == len(runtime_prepare_calls) == len(runtime_cleanup_calls) == runs
    assert len(docker_removal_calls) == runs
    assert all(call[3] == ("2720" if expected_mode == "default" else "2176") for call in prepare_calls)
    volumes = [env["SQLITE_MIGRATION_HOME_VOLUME"] for env in inspect_environments]
    assert len(volumes) == runs
    assert len(set(volumes)) == runs
    assert all(volume.startswith("sqlite-block-home-pilot-defect-blocker-") for volume in volumes)
    assert all(env["SQLITE_MIGRATION_SECURE_DELETE"] == expected_mode for env in inspect_environments)
    assert all(env["SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES"] == "60817408" for env in inspect_environments)
    assert all(env["SQLITE_MIGRATION_RUNTIME_IMAGE_MIB"] == "64" for env in inspect_environments)
    assert all(
        command[command.index("--epochs") + 1] == "1"
        for command in calls if command[0] == "inspect"
    )
