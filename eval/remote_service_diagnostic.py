"""Unpaid OpenCode/bridge exercise for Inspect remote-job lifecycle tracing.

Run against a disposable SQLite task volume with `inspect eval`. The local
mock makes OpenCode invoke the real migration workflow without a model API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

from inspect_ai import Task, task
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.dataset import SCENARIO_SQLITE_MIGRATION, make_sqlite_migration_sample
from eval.scorer import _capture_opencode_session
from eval.task import SAMPLE_TIME_LIMIT, SQLITE_MIGRATION_SANDBOX_CONFIG, _opencode_solver


class _MigrationToolReply:
    def __init__(self, mode: str = "migration") -> None:
        self.mode = mode
        self.calls = 0
        self.tool_calls = 0
        self.observed_tools: list[str] = []

    def __call__(self, _input, tools, _tool_choice, _config):
        self.calls += 1
        self.observed_tools.extend(tool.name for tool in tools)
        bash_tools = [tool for tool in tools if "bash" in tool.name.lower()]
        if self.tool_calls == 0 and bash_tools:
            self.tool_calls += 1
            tool = bash_tools[0]
            parameters = getattr(tool, "parameters", {})
            if hasattr(parameters, "model_dump"):
                parameters = parameters.model_dump()
            properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
            if "timeout" not in properties:
                raise RuntimeError("OpenCode Bash tool does not expose its native timeout parameter")
            command = (
                "python scripts/reproduce_progress.py local-data/reproducer.db && "
                "python -m pytest tests/test_transform.py "
                "tests/test_transform_progress.py "
                "tests/test_cli_transform_progress.py && "
                "python scripts/validate_migration.py; "
                "status=$?; printf 'REMOTE_MIGRATION_STATUS=%s\\n' \"$status\"; "
                "sleep 305; exit \"$status\""
                if self.mode == "migration"
                else "sleep 305; status=$?; printf 'REMOTE_IDLE_STATUS=%s\\n' \"$status\"; exit \"$status\""
            )
            return ModelOutput.for_tool_call(
                "mockllm/model",
                tool.name,
                {
                    "command": command,
                    "timeout": 600000,
                    "description": "Run the requested validation workflow",
                },
            )
        if self.tool_calls:
            return ModelOutput.from_content(
                "mockllm/model", "The requested validation command was run."
            )
        return ModelOutput.from_content(
            "mockllm/model", "No Bash tool was offered; diagnostic cannot proceed."
        )


@scorer(metrics=[accuracy()])
def diagnostic_completed(reply: _MigrationToolReply):
    async def score(_state: TaskState, _target: Target) -> Score:
        env = sandbox()
        script = r'''
import json, pathlib, subprocess
root = pathlib.Path('/var/lib/streamstats-state')
def read(path):
    try: return path.read_text(errors='replace')
    except Exception as exc: return f'<unavailable: {exc!r}>'
result = {
  'processes': subprocess.run(['ps','-eo','pid,ppid,stat,lstart,comm','--sort','pid'], capture_output=True, text=True).stdout,
  'proc1_cgroup': read(pathlib.Path('/proc/1/cgroup')),
  'memory_current': read(pathlib.Path('/sys/fs/cgroup/memory.current')),
  'memory_max': read(pathlib.Path('/sys/fs/cgroup/memory.max')),
  'memory_events': read(pathlib.Path('/sys/fs/cgroup/memory.events')),
  'lifecycle': read(root/'inspect-sandbox-tools-lifecycle.jsonl'),
}
for line in (root/'inspect-sandbox-tools-lifecycle.jsonl').read_text(errors='replace').splitlines():
  try:
    row = json.loads(line)
  except Exception:
    continue
  if row.get('event') == 'service_start' and row.get('server_dir'):
    server = pathlib.Path(row['server_dir'])
    result['service_server_dir'] = str(server)
    result['service_stdout'] = read(server/'server-stdout.log')
    result['service_stderr'] = read(server/'server-stderr.log')
    result['service_pid_file'] = read(server/'server.pid')
    result['service_shutdown_status'] = read(server/'shutdown-status.json')
    break
print(json.dumps(result, sort_keys=True))
'''
        capture_result = await env.exec(["python", "-c", script], user="root", timeout=20)
        transcript = await _capture_opencode_session(env)
        tool_calls = transcript.get("tool_calls", [])
        expected_marker = (
            "scripts/validate_migration.py"
            if reply.mode == "migration"
            else "REMOTE_IDLE_STATUS=0"
        )
        observed_calls = [
            item for item in tool_calls
            if isinstance(item, dict)
            and expected_marker in json.dumps(item)
        ]
        transcript_text = json.dumps(observed_calls)
        verified = bool(observed_calls) and (
            (
                "REMOTE_MIGRATION_STATUS=0" in transcript_text
                and "persistent migration validated" in transcript_text.lower()
            )
            if reply.mode == "migration"
            else "REMOTE_IDLE_STATUS=0" in transcript_text
        )
        diagnostics = os.environ.get("STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR")
        if diagnostics:
            target = Path(diagnostics)
            target.mkdir(parents=True, exist_ok=True)
            (target / "successful-container-capture.json").write_text(
                json.dumps(
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "exit_code": capture_result.returncode,
                        "stdout": capture_result.stdout,
                        "stderr": capture_result.stderr,
                        "mock_tool_calls": reply.tool_calls,
                        "mock_observed_tools": reply.observed_tools,
                        "mode": reply.mode,
                        "observed_calls": observed_calls,
                        "migration_verified": verified if reply.mode == "migration" else None,
                        "idle_command_verified": verified if reply.mode == "idle" else None,
                        "transcript_captured": transcript.get("captured"),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        return Score(
            value=1.0 if capture_result.success and verified else 0.0,
            answer=(f"{reply.mode} command ran successfully" if verified else
                    f"{reply.mode} command was not observed to finish successfully"),
        )

    return score


def _diagnostic_task(mode: str, name: str) -> Task:
    reply = _MigrationToolReply(mode)
    model = get_model("mockllm/model", custom_outputs=reply)
    aliases = {
        "inspect": model,
        "openrouter/z-ai/glm-5.3-flash": model,
        "z-ai/glm-5.3-flash": model,
    }
    return Task(
        dataset=[make_sqlite_migration_sample("clean-capacity")],
        model=model,
        solver=_opencode_solver(
            scenario=SCENARIO_SQLITE_MIGRATION,
            opencode_model="openrouter/z-ai/glm-5.3-flash",
            model_aliases=aliases,
        ),
        scorer=diagnostic_completed(reply),
        sandbox=SQLITE_MIGRATION_SANDBOX_CONFIG,
        turn_limit=100,
        time_limit=SAMPLE_TIME_LIMIT,
        cost_limit=None,
        name=name,
    )


@task
def remote_service_migration():
    """Run actual OpenCode, its Inspect bridge, and the full local migration."""
    return _diagnostic_task("migration", "sqlite-remote-service-diagnostic")


@task
def remote_service_idle_control():
    """Hold a real OpenCode remote shell job alongside the local mock proxy."""
    return _diagnostic_task("idle", "sqlite-remote-service-idle-control")
