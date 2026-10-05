"""No-cost Inspect/OpenCode smoke with one real allowed shell tool call.

The mock reply asks OpenCode to execute a short Bash command, then ends the
conversation. This exercises Inspect routing, native tool execution, usage
accounting, transcript capture, permission configuration, and the pinned
process-argument monitor without contacting a paid model provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox, store

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.dataset import SCENARIO_DEVELOPMENT, make_dataset
from eval.scorer import _capture_opencode_session, _capture_sqlite_storage_telemetry
from eval.task import DEVELOPMENT_SANDBOX_CONFIG, TURN_LIMIT, _opencode_solver


class _AllowedToolReply:
    """Emit one shell tool call and a final answer on later requests."""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_call_sent = False
        self.requested_tools: list[list[str]] = []

    def __call__(self, _input, tools, _tool_choice, _config):
        self.calls += 1
        self.requested_tools.append([tool.name for tool in tools])
        candidates = [tool.name for tool in tools if "bash" in tool.name.lower()]
        if not candidates:
            return ModelOutput.from_content(
                "mockllm/model", "Proceed with the requested smoke check using the available tools."
            )
        if self.tool_call_sent:
            return ModelOutput.from_content("mockllm/model", "Runtime smoke complete.")
        self.tool_call_sent = True
        tool_name = candidates[0]
        return ModelOutput.for_tool_call(
            "mockllm/model",
            tool_name,
            {
                "command": "printf 'SMOKE_TOOL_OK\\n'; sleep 0.25",
                "description": "Verify an allowed shell command executes",
            },
        )


class _FullTaskDiskReply:
    """Fill the task filesystem, then require a second native call and finish."""

    def __init__(self):
        self.calls = 0
        self.tool_commands = 0
        self.requested_tools: list[list[str]] = []

    def __call__(self, _input, tools, _tool_choice, _config):
        self.calls += 1
        self.requested_tools.append([tool.name for tool in tools])
        candidates = [tool.name for tool in tools if "bash" in tool.name.lower()]
        if not candidates:
            return ModelOutput.from_content("mockllm/model", "The storage smoke tools are unavailable.")
        self.tool_commands += 1
        if self.tool_commands == 1:
            command = (
                "python - <<'PY'\nimport os\np='/home/dev/tmp/task-fill.bin'\n"
                "s=os.statvfs('/home/dev'); free=s.f_bavail*s.f_frsize\n"
                "with open(p,'wb') as f: os.posix_fallocate(f.fileno(),0,max(1,free-1048576))\n"
                "print('TASK_DISK_FILLED',os.statvfs('/home/dev').f_bavail*os.statvfs('/home/dev').f_frsize)\nPY"
            )
        elif self.tool_commands == 2:
            command = "test -f /home/dev/tmp/task-fill.bin && printf 'SECOND_TURN_TOOL_OK\\n' && df -B1 /home/dev"
        elif 3 <= self.tool_commands <= 10:
            payload_number = self.tool_commands - 2
            command = (
                "python -c \"print('TOOL_PAYLOAD_" + str(payload_number)
                + ":' + 'x'*32768)\""
            )
        else:
            return ModelOutput.from_content("mockllm/model", "Runtime storage survived task-disk exhaustion.")
        return ModelOutput.for_tool_call(
            "mockllm/model", candidates[0],
            {"command": command, "description": "Exercise OpenCode after task disk exhaustion"},
        )


@scorer(metrics=[accuracy()])
def runtime_smoke_score(requested_tools: list[list[str]]):
    async def score(state: TaskState, target: Target) -> Score:
        env = sandbox()
        transcript = await _capture_opencode_session(env)
        try:
            process_rows = store().get("opencode_process_command_lines", [])
        except Exception:
            process_rows = []
        calls = transcript.get("tool_calls", []) if isinstance(transcript, dict) else []
        bash_calls = [
            record for record in calls
            if isinstance(record, dict) and "bash" in str(record.get("function", "")).lower()
        ]
        tool_executed = any(
            "SMOKE_TOOL_OK" in json.dumps(record.get("result", ""))
            for record in bash_calls
        )
        argv_rows = [
            record.get("argv", [])
            for record in process_rows
            if isinstance(record, dict) and isinstance(record.get("argv"), list)
        ] if isinstance(process_rows, list) else []
        bypass_flag = any(
            "--dangerously-skip-permissions" in argv
            for argv in argv_rows if isinstance(argv, list)
        )
        has_shell_child = any(
            any("SMOKE_TOOL_OK" in argument or "sleep" in argument for argument in argv)
            for argv in argv_rows if isinstance(argv, list)
        )
        config_result = await env.exec(
            [
                "python",
                "-c",
                "import json; p='/home/dev/.config/opencode/opencode.json'; c=json.load(open(p)); print(json.dumps(c.get('permission',{})))",
            ],
            user="root",
            timeout=15,
        )
        try:
            permission = json.loads(config_result.stdout)
        except json.JSONDecodeError:
            permission = {}
        unattended_policy = permission.get("*") == "allow"
        success = bool(
            transcript.get("captured")
            and tool_executed
            and has_shell_child
            and not bypass_flag
            and unattended_policy
        )
        details = {
            "transcript_captured": bool(transcript.get("captured")),
            "allowed_shell_tool_executed": tool_executed,
            "child_command_observed": has_shell_child,
            "permission_bypass_flag_present": bypass_flag,
            "unattended_permission_policy": unattended_policy,
            "model_tool_names": requested_tools,
            "process_command_lines": argv_rows,
        }
        return Score(
            value=1.0 if success else 0.0,
            answer="pass" if success else "fail",
            explanation=json.dumps(details, sort_keys=True),
            metadata=details,
        )

    return score


@task
def streamstats_runtime_smoke(retry_timeout: bool = False, exhaust_timeout: bool = False):
    """Run one tier-0 development-container sample with a local mock model."""

    reply = _AllowedToolReply()
    if retry_timeout and exhaust_timeout:
        raise ValueError("Choose either timeout recovery or timeout exhaustion")
    requests = 0

    async def timed_reply(input, tools, tool_choice, config):
        nonlocal requests
        requests += 1
        if requests == 1 or exhaust_timeout:
            await asyncio.sleep(2)
        return reply(input, tools, tool_choice, config)

    timed = retry_timeout or exhaust_timeout
    config = GenerateConfig(attempt_timeout=1, timeout=15, max_retries=1) if timed else GenerateConfig()
    mock_model = get_model(
        "mockllm/model", config=config, custom_outputs=timed_reply if timed else reply
    )
    return Task(
        dataset=make_dataset("tier0", scenario=SCENARIO_DEVELOPMENT),
        model=mock_model,
        solver=_opencode_solver(
            scenario=SCENARIO_DEVELOPMENT,
            # OpenCode versions differ on whether their OpenAI-compatible
            # request body carries the local model id or its provider/model
            # spelling. Both names route to the same deterministic mock.
            model_aliases={
                "inspect": mock_model,
                "openrouter/z-ai/glm-5.3-flash": mock_model,
                "z-ai/glm-5.3-flash": mock_model,
                "glm-5.3-flash": mock_model,
            },
        ),
        scorer=runtime_smoke_score(reply.requested_tools),
        sandbox=DEVELOPMENT_SANDBOX_CONFIG,
        turn_limit=TURN_LIMIT,
        time_limit=180,
        cost_limit=None,
        name="streamstats-runtime-smoke",
    )


@scorer(metrics=[accuracy()])
def sqlite_runtime_full_disk_score(reply: _FullTaskDiskReply):
    async def score(state: TaskState, target: Target) -> Score:
        env = sandbox()
        transcript = await _capture_opencode_session(env)
        telemetry = await _capture_sqlite_storage_telemetry(env)
        calls = transcript.get("tool_calls", []) if isinstance(transcript, dict) else []
        results = [str(call.get("result", "")) for call in calls if isinstance(call, dict)]
        filled = any("TASK_DISK_FILLED" in value for value in results)
        next_turn = any("SECOND_TURN_TOOL_OK" in value for value in results)
        payload_turns = sum(1 for number in range(1, 9)
                            if any(f"TOOL_PAYLOAD_{number}:" in value for value in results))
        home = telemetry.get("home", {}) if isinstance(telemetry, dict) else {}
        runtime = telemetry.get("opencode_runtime", {}) if isinstance(telemetry, dict) else {}
        task_stayed_full = isinstance(home, dict) and int(home.get("minimum_free_bytes", -1)) <= 1048576
        runtime_recorded = (
            isinstance(runtime, dict)
            and int(runtime.get("peak_allocated_bytes", 0)) > 0
            and int(runtime.get("capacity_bytes", 0)) >= 24 * 1024 * 1024
        )
        details = {
            "transcript_captured": transcript.get("captured") is True,
            "fill_tool_result_saved": filled,
            "second_turn_tool_result_saved": next_turn,
            "large_output_turns_saved": payload_turns,
            "task_disk_remained_full": task_stayed_full,
            "runtime_filesystem_telemetry": telemetry,
            "native_tool_names": [call.get("function") for call in calls if isinstance(call, dict)],
            "mock_turns": reply.calls,
        }
        success = bool(details["transcript_captured"] and filled and next_turn
                       and payload_turns == 8 and task_stayed_full and runtime_recorded
                       and reply.calls >= 11)
        return Score(value=1.0 if success else 0.0, answer="pass" if success else "fail",
                     explanation=json.dumps(details, sort_keys=True), metadata=details)
    return score


@task
def sqlite_runtime_full_disk_smoke():
    """Prove OpenCode saves native tool results on its separate disk."""
    reply = _FullTaskDiskReply()
    model = get_model("mockllm/model", custom_outputs=reply)
    sandbox_config = ("docker", str(REPO_ROOT / "sandbox" / "compose.sqlite-runtime-smoke.yaml"))
    return Task(
        dataset=[Sample(
            input="Use Bash twice in order. The first call fills the task filesystem. "
                     "After receiving its result, make the second call and inspect its result. "
                     "Then make eight more Bash calls, each returning a large output, "
                     "and finish with a short report.",
            target="OpenCode runtime remains writable after task storage is full",
        )],
        model=model,
        solver=_opencode_solver(
            scenario="sqlite migration",
            model_aliases={
                "inspect": model,
                "openrouter/z-ai/glm-5.3-flash": model,
                "z-ai/glm-5.3-flash": model,
                "glm-5.3-flash": model,
            },
        ),
        scorer=sqlite_runtime_full_disk_score(reply),
        sandbox=sandbox_config,
        turn_limit=15,
        time_limit=180,
        cost_limit=None,
        name="sqlite-runtime-full-disk-smoke",
    )
