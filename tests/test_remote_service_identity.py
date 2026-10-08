from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest

from eval import opencode_adapter as adapter
from sandbox import runtime_guard


class _ExecResult:
    returncode = 0
    stderr = ""

    def __init__(self, stdout: str):
        self.stdout = stdout


class _IdentitySandbox:
    def __init__(self, identity):
        self.identity = identity
        self.commands = []

    async def exec(self, cmd, **_kwargs):
        self.commands.append(cmd)
        return _ExecResult(json.dumps(self.identity))


class _Store:
    def __init__(self):
        self.values = {}

    def set(self, *_args, **_kwargs):
        self.values[_args[0]] = _args[1]


def _identity(**overrides):
    values = {
        "event": "service_runtime_identity",
        "startup_id": "service-instance-1",
        "patch_id": adapter._REMOTE_EXEC_PATCH_ID,
        "controller_source_sha256": "controller-sha",
        "module_paths": {
            "inspect_sandbox_tools._remote_tools._exec_remote._controller": (
                "/usr/local/libexec/inspect-sandbox-tools-package/src/inspect_sandbox_tools/"
                "_remote_tools/_exec_remote/_controller.py"
            )
        },
        "package_version": "1.2.1",
        "service_process_alive": True,
        "server_dir": "/var/tmp/sandbox-tools",
        "service_instance_count": 1,
        "service_directories": ["/var/tmp/sandbox-tools"],
    }
    return values | overrides


def test_identity_gate_accepts_exact_loaded_patch(monkeypatch):
    monkeypatch.setattr(adapter, "_expected_remote_controller_hash", lambda: "controller-sha")
    monkeypatch.setattr(adapter, "store", lambda: _Store())
    sbox = _IdentitySandbox(_identity())

    result = asyncio.run(adapter._verify_remote_service_identity(sbox))

    assert result["startup_id"] == "service-instance-1"
    assert len(sbox.commands) == 1
    assert sbox.commands[0][0:2] == ["python", "-c"]


def test_storage_telemetry_can_be_captured_before_failed_sandbox_teardown():
    telemetry = {"home": {"peak_used_bytes": 2_172_092_416}, "opencode_runtime": {"peak_allocated_bytes": 30_000_000}}

    class _TelemetrySandbox:
        def __init__(self):
            self.calls = []

        async def exec(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return _ExecResult(json.dumps(telemetry))

    sbox = _TelemetrySandbox()
    captured = asyncio.run(adapter._capture_storage_telemetry_after_runtime_error(sbox))

    assert captured == telemetry
    assert sbox.calls == [(
        ["cat", "/var/lib/streamstats-telemetry/sqlite-storage-telemetry.json"],
        {"user": "root", "timeout": 10},
    )]


@pytest.mark.parametrize(
    "override",
    [
        {"patch_id": "unpatched"},
        {"controller_source_sha256": "stale-controller"},
        {
            "module_paths": {
                "inspect_sandbox_tools._remote_tools._exec_remote._controller": (
                    "/usr/local/lib/python/site-packages/inspect_sandbox_tools/_controller.py"
                )
            }
        },
        {"service_process_alive": False},
        {"server_dir": "/home/dev/tmp/sandbox-tools"},
        {"service_instance_count": 2},
    ],
)
def test_identity_gate_rejects_unexpected_service(monkeypatch, override):
    monkeypatch.setattr(adapter, "_expected_remote_controller_hash", lambda: "controller-sha")
    monkeypatch.setattr(adapter, "store", lambda: _Store())
    sbox = _IdentitySandbox(_identity(**override))

    with pytest.raises(RuntimeError, match="REMOTE_SERVICE_IDENTITY_REJECTED"):
        asyncio.run(adapter._verify_remote_service_identity(sbox))


def test_rejected_identity_prevents_bridge_body_from_starting(monkeypatch):
    body_started = False
    captures = []

    @asynccontextmanager
    async def bridge(_state, **_kwargs):
        yield object()

    async def reject(_sbox):
        raise RuntimeError("REMOTE_SERVICE_IDENTITY_REJECTED")

    async def capture(_sbox, error):
        captures.append(str(error))

    monkeypatch.setattr(adapter, "sandbox_agent_bridge", bridge)
    monkeypatch.setattr(adapter, "_verify_remote_service_identity", reject)
    monkeypatch.setattr(adapter, "_capture_remote_execution_failure", capture)

    async def run():
        nonlocal body_started
        with pytest.raises(RuntimeError, match="REMOTE_SERVICE_IDENTITY_REJECTED"):
            async with adapter._captured_sandbox_agent_bridge(object(), object()):
                body_started = True

    asyncio.run(run())
    assert body_started is False
    assert captures == ["REMOTE_SERVICE_IDENTITY_REJECTED"]


def test_failure_capture_persists_before_cleanup(tmp_path, monkeypatch):
    diagnostics = tmp_path / "diagnostics"
    monkeypatch.setenv("STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR", str(diagnostics))
    monkeypatch.setattr(adapter, "store", lambda: _Store())

    class _CaptureSandbox:
        async def exec(self, _cmd, **_kwargs):
            return _ExecResult(json.dumps({"lifecycle": "trace-before-cleanup"}))

    asyncio.run(
        adapter._capture_remote_execution_failure(
            _CaptureSandbox(), RuntimeError("bridge monitor failed")
        )
    )

    files = list(diagnostics.glob("failure-*.json"))
    assert len(files) == 1
    captured = json.loads(files[0].read_text())
    assert captured["error"] == "RuntimeError('bridge monitor failed')"
    assert "trace-before-cleanup" in captured["capture_output"]


def test_seeded_cli_pins_one_sandbox_tools_directory(tmp_path, monkeypatch):
    cli_dir = tmp_path / "runtime"
    monkeypatch.setenv("INSPECT_REMOTE_EXEC_INSTRUMENTATION", "1")
    monkeypatch.setattr(runtime_guard, "SANDBOX_TOOLS_DIR", cli_dir)
    monkeypatch.setattr(runtime_guard, "SANDBOX_TOOLS_CLI", cli_dir / "inspect-sandbox-tools")
    monkeypatch.setattr(runtime_guard, "INSPECT_SERVER_DIR", Path("/var/tmp/sandbox-tools"))
    monkeypatch.setattr(runtime_guard.os, "chown", lambda *_args: None)
    monkeypatch.setattr(runtime_guard.os, "chmod", lambda *_args: None)

    runtime_guard._ensure_instrumented_tools()

    script = (cli_dir / "inspect-sandbox-tools").read_text()
    assert "INSPECT_SANDBOX_TOOLS_DIR" in script
    assert "/var/tmp/sandbox-tools" in script


@pytest.mark.parametrize("failure_point", ["entry", "body", "exit"])
def test_bridge_failure_capture_covers_context_lifecycle(monkeypatch, failure_point):
    captures = []

    @asynccontextmanager
    async def bridge(_state, **_kwargs):
        if failure_point == "entry":
            raise RuntimeError("entry failed")
        try:
            yield object()
        finally:
            if failure_point == "exit":
                raise RuntimeError("exit failed")

    async def verify(_sbox):
        return {}

    async def capture(_sbox, error):
        captures.append(str(error))

    monkeypatch.setattr(adapter, "sandbox_agent_bridge", bridge)
    monkeypatch.setattr(adapter, "_verify_remote_service_identity", verify)
    monkeypatch.setattr(adapter, "_capture_remote_execution_failure", capture)

    async def run():
        with pytest.raises(RuntimeError):
            async with adapter._captured_sandbox_agent_bridge(object(), object()):
                if failure_point == "body":
                    raise RuntimeError("body failed")

    asyncio.run(run())
    assert len(captures) == 1
    assert captures[0].endswith("failed")
