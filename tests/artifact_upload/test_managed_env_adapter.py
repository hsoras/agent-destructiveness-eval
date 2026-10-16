from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from eval.artifact_upload import host_adapter


def test_worker_core_matches_frozen_deployed_bytes():
    repo = Path(__file__).resolve().parents[2]
    expected = subprocess.run(
        ["git", "show", "5bfb3c3^:sandbox/artifact_upload/worker/core.py"],
        cwd=repo, check=True, capture_output=True,
    ).stdout
    actual = (repo / "sandbox/artifact_upload/worker/core.py").read_bytes()
    assert hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest()


def test_managed_env_is_host_parsed_and_applies_to_later_natural_shell_calls(monkeypatch):
    session = host_adapter.GuestWorkerSession.__new__(host_adapter.GuestWorkerSession)
    session._managed_home = "/home/alex"
    session.container = "natural-worker"
    files = ["export TERM='xterm'\nunset CI_RUNNER\n", "export EDITOR_HINT='vim'\nexport PATH='/bad'\nexport EDITOR_HINT='$(touch pwned)'\n"]
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        assert argv[:4] == ["docker", "exec", "-u", "0"]
        assert argv[4] == "natural-worker" and argv[-2] == "-c"
        assert kwargs["timeout"] == 10
        import json
        return SimpleNamespace(returncode=0, stdout=json.dumps(files), stderr="")

    monkeypatch.setattr(host_adapter.subprocess, "run", fake_run)
    call = {"tool_name": "bash", "operation": "shell", "arguments": {"command": "printf '%s' \"$EDITOR_HINT\"", "env": {"TERM": "explicit", "BASH_ENV": "/tmp/agent-startup"}}}
    first = session._with_managed_env(call)
    assert first["arguments"]["env"] == {"TERM": "explicit", "EDITOR_HINT": "vim", "BASH_ENV": "/dev/null"}
    assert first["arguments"]["command"].endswith("bash -c " + __import__("shlex").quote(call["arguments"]["command"]))
    assert call["arguments"]["env"] == {"TERM": "explicit", "BASH_ENV": "/tmp/agent-startup"}
    completed = real_run(["bash", "-c", first["arguments"]["command"]], env={**os.environ, **first["arguments"]["env"], "CI_RUNNER": "yes"},
                         capture_output=True, text=True, check=False)
    assert completed.returncode == 0 and completed.stdout == "vim"

    # Re-read on each call: later env_set_variables changes affect later shell calls.
    files = ["export TERM='xterm'\n", "export EDITOR_HINT='emacs'\n"]
    later = session._with_managed_env(call)
    assert later["arguments"]["env"]["EDITOR_HINT"] == "emacs"
    assert "-u CI_RUNNER" not in later["arguments"]["command"]
    assert later["arguments"]["command"] == call["arguments"]["command"]

    files = ["", ""]
    empty = session._with_managed_env(call)
    assert empty["arguments"]["env"]["BASH_ENV"] == "/dev/null"
    assert empty["arguments"]["env"]["TERM"] == "explicit"

    # Sequential assignment semantics apply across files and within each file.
    for ordered_files, expected_value in [
        (["unset FOO\n", "export FOO='bar'\n"], "bar"),
        (["export FOO='bar'\n", "unset FOO\n"], None),
        (["unset FOO\nexport FOO='bar'\n", ""], "bar"),
        (["export FOO='bar'\nunset FOO\n", ""], None),
    ]:
        files = ordered_files
        ordered = session._with_managed_env({"tool_name": "bash", "operation": "shell",
            "arguments": {"command": "printf '%s' \"${FOO-unset}\"", "env": {}}})
        assert ordered["arguments"]["env"].get("FOO") == expected_value
        if expected_value is None:
            assert "-u FOO" in ordered["arguments"]["command"]
        else:
            assert "-u FOO" not in ordered["arguments"]["command"]
            assert ordered["arguments"]["env"]["FOO"] == expected_value


def test_managed_env_adapter_gates_off_eval_and_non_shell_calls():
    session = host_adapter.GuestWorkerSession.__new__(host_adapter.GuestWorkerSession)
    session._managed_home = None
    session.container = "eval-worker"
    call = {"tool_name": "bash", "operation": "shell", "arguments": {"command": "true"}}
    assert session._with_managed_env(call) is call
    session._managed_home = "/home/alex"
    read_call = {"tool_name": "read", "operation": "read", "arguments": {"filePath": "x"}}
    assert session._with_managed_env(read_call) is read_call
