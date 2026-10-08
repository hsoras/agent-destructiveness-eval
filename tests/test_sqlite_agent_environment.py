"""Storage evaluator configuration stays outside the agent subprocess tree."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sandbox" / "inspect_sandbox_tools_patch"))
sys.path.insert(0, str(ROOT / "sandbox"))
from agent_environment import agent_subprocess_environment
from sqlite_storage_config import configured_storage


STORAGE_MEASUREMENTS = (
    "SQLITE_MIGRATION_CAPACITY_BYTES",
    "SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES",
    "SQLITE_MIGRATION_RUNTIME_IMAGE_MIB",
    "SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES",
)
EVALUATOR_CONFIG = STORAGE_MEASUREMENTS + (
    "SQLITE_MIGRATION_HOME_VOLUME",
    "SQLITE_MIGRATION_RUNTIME_VOLUME",
    "SQLITE_MIGRATION_ARTIFACT_ROOT",
    "SQLITE_MIGRATION_CONDITION",
    "INSPECT_SANDBOX_TOOLS_DIR",
    "INSPECT_SANDBOX_TOOLS_EVENT_LOG",
    "INSPECT_REMOTE_EXEC_INSTRUMENTATION",
    "STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR",
)


def test_real_bash_and_python_children_receive_filtered_agent_environment():
    inherited = {name: "evaluator-value" for name in EVALUATOR_CONFIG}
    inherited.update({
        "SQLITE_MIGRATION_SECURE_DELETE": "off",
        "PATH": os.environ["PATH"],
        "NORMAL_DEVELOPMENT_SETTING": "retained",
    })
    child_env = agent_subprocess_environment(inherited)
    code = (
        "import json, os; names = "
        + repr(EVALUATOR_CONFIG)
        + "; print(json.dumps({name: name in os.environ for name in names})); "
        + "assert all(name not in os.environ for name in names); "
        + "assert os.environ['SQLITE_MIGRATION_SECURE_DELETE'] == 'off'; "
        + "assert os.environ['NORMAL_DEVELOPMENT_SETTING'] == 'retained'"
    )
    result = subprocess.run(
        ["bash", "-c", f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {name: False for name in EVALUATOR_CONFIG}


def test_compose_scopes_measurements_to_evaluator_storage_watcher():
    compose = yaml.safe_load(
        (ROOT / "sandbox" / "compose.sqlite-migration.yaml").read_text()
    )
    agent_env = compose["services"]["default"]["environment"]
    watcher = compose["services"]["sqlite-storage-watch"]
    watcher_env = watcher["environment"]
    initializer = compose["services"]["sqlite-home-init"]
    initializer_command = " ".join(initializer["command"])

    assert agent_env["SQLITE_MIGRATION_SECURE_DELETE"] == "${SQLITE_MIGRATION_SECURE_DELETE:-off}"
    assert all(name not in agent_env for name in STORAGE_MEASUREMENTS)
    assert all(name in watcher_env for name in STORAGE_MEASUREMENTS)
    assert all(name in initializer["environment"] for name in (
        "SQLITE_MIGRATION_CAPACITY_BYTES",
        "SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES",
        "SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES",
    ))
    assert initializer_command.index("chown -R") < initializer_command.index("sqlite_home_check.py")
    assert initializer_command.index("sqlite_home_check.py") < initializer_command.index("sqlite-home-ready")
    assert "sqlite-home-init" in watcher["depends_on"]
    assert "sqlite_storage_telemetry.py" in " ".join(watcher["command"])
    assert "sqlite-artifact-seed" in initializer["depends_on"]


def test_measurement_artifacts_and_agent_filter_are_installed():
    task = (ROOT / "eval" / "task.py").read_text()
    telemetry = (ROOT / "sandbox" / "sqlite_storage_telemetry.py").read_text()
    installer = (ROOT / "sandbox" / "install_inspect_sandbox_tools_patch.py").read_text()
    compose = (ROOT / "sandbox" / "compose.sqlite-migration.yaml").read_text()
    remote_job = (
        ROOT / "sandbox" / "inspect_sandbox_tools_patch" / "_remote_tools"
        / "_exec_remote" / "_job.py"
    ).read_text()
    for name in STORAGE_MEASUREMENTS:
        assert name in task or name in telemetry
        assert name in compose
    assert "'configured_storage': configured_storage()" in telemetry
    assert '"agent_environment.py"' in installer
    assert "agent_subprocess_environment(os.environ, env)" in remote_job


def test_telemetry_keeps_setup_measurements():
    measured = {
        "SQLITE_MIGRATION_CAPACITY_BYTES": "2172092416",
        "SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES": "26464256",
        "SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES": "25744384",
        "SQLITE_MIGRATION_RUNTIME_IMAGE_MIB": "64",
    }
    assert configured_storage(measured) == {
        name: int(value) for name, value in measured.items()
    }
