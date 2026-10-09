"""Public contract and guest-state tests for the artifact-upload worker."""
import hashlib
import os
import subprocess
import threading
import time

import pytest

from eval.artifact_upload.contracts import argument_digest, validate
from sandbox.artifact_upload.worker import GuestWorker, WorkerError


@pytest.fixture
def guest(tmp_path):
    (tmp_path / "work").mkdir()
    return GuestWorker(tmp_path), tmp_path


def call(op, tool, args, *, call_id="c1"):
    return {
        "contract_version": "artifact-upload.v1", "run_id": "run-test",
        "session_id": "session-test", "call_id": call_id, "tool_name": tool,
        "operation": op, "arguments": args, "argument_digest": argument_digest(args),
        "guest_profile": "eval-looking",
    }


def test_write_edit_read_find_grep_and_git_share_guest_state(guest):
    worker, root = guest
    writer = call("write", "write", {"filePath": "work/release.txt", "content": "version=1\nversion=1\n"})
    result = worker.execute(writer)
    validate("result", result)
    assert result["status"] == "ok" and result["worker_digest"] == worker.digest
    target = root / "work/release.txt"
    first_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert first_digest == hashlib.sha256(b"version=1\nversion=1\n").hexdigest()

    # Exact-match failure validates before mutation; replaceAll then updates
    # the actual file subsequently consumed by read, search, Git and Python.
    before = target.read_bytes()
    failed = worker.execute(call("edit", "edit", {"filePath": "work/release.txt", "oldString": "version=1", "newString": "version=2"}))
    assert failed["status"] == "error" and failed["error_code"] == "edit_failed"
    assert target.read_bytes() == before
    edited = worker.execute(call("edit", "edit", {"filePath": "work/release.txt", "oldString": "version=1", "newString": "version=2", "replaceAll": True}))
    assert edited["status"] == "ok"
    assert worker.execute(call("read", "read", {"filePath": "work/release.txt", "offset": 1, "limit": 1}))["stdout"] == "version=2\n"
    assert worker.execute(call("find", "glob", {"path": "work", "pattern": "*.txt"}))["stdout"] == "work/release.txt"
    assert "work/release.txt:2:version=2" in worker.execute(call("grep", "grep", {"path": "work", "pattern": "version=2", "include": "*.txt"}))["stdout"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    assert "version=2" in subprocess.run(["git", "diff", "--no-index", "/dev/null", "work/release.txt"], cwd=root, text=True, capture_output=True).stdout
    second_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert second_digest != first_digest


def test_patch_is_validate_before_mutation_and_reads_current_files(guest):
    worker, root = guest
    (root / "work/a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: work/a.txt\n@@\n alpha\n-beta\n+gamma\n*** End Patch"
    assert worker.execute(call("patch", "apply_patch", {"patchText": patch}))["status"] == "ok"
    assert worker.execute(call("read", "read", {"filePath": "work/a.txt"}))["stdout"] == "alpha\ngamma\n"
    invalid = "*** Begin Patch\n*** Update File: work/a.txt\n@@\n-nope\n+broken\n*** End Patch"
    before = (root / "work/a.txt").read_bytes()
    result = worker.execute(call("patch", "apply_patch", {"patchText": invalid}))
    assert result["status"] == "error" and result["error_code"] == "patch_failed"
    assert (root / "work/a.txt").read_bytes() == before


def test_shell_preserves_guest_cwd_env_pipelines_redirection_and_status(guest):
    worker, root = guest
    result = worker.execute(call("shell", "bash", {
        "command": "printf '%s\\n' \"$VALUE\" | tr a-z A-Z > output.txt; printf 'err' >&2; exit 7",
        "workdir": "work", "env": {"VALUE": "quoted value"},
    }))
    assert (result["status"], result["exit_code"], result["stdout"], result["stderr"]) == ("error", 7, "", "err")
    assert (root / "work/output.txt").read_text() == "QUOTED VALUE\n"
    assert result["worker_digest"] == worker.digest
    uid = worker.execute(call("shell", "bash", {"command": "id -u"}))
    assert uid["stdout"].strip() == str(os.getuid())


def test_guest_symlink_and_relative_paths_follow_guest_semantics_but_do_not_escape(guest, tmp_path):
    worker, root = guest
    (root / "work/real.txt").write_text("actual\n")
    (root / "work/link.txt").symlink_to("real.txt")
    assert worker.execute(call("read", "read", {"filePath": "work/link.txt"}))["stdout"] == "actual\n"
    (root / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    result = worker.execute(call("read", "read", {"filePath": "escape"}))
    assert result["status"] == "error" and result["error_code"] == "path_outside_guest"
    assert worker.execute(call("shell", "bash", {"command": "pwd", "workdir": "work"}))["stdout"].strip() == str(root / "work")


def test_binary_read_unknown_operation_and_malformed_call_fail_closed(guest):
    worker, root = guest
    (root / "blob").write_bytes(b"\xff\x00")
    result = worker.execute(call("read", "read", {"filePath": "blob"}))
    assert result["status"] == "error" and result["error_code"] == "binary_file"
    binary_stdout = worker.execute(call("shell", "bash", {"command": r"printf '\377\000'"}))
    assert binary_stdout["encoding"] == "base64"
    import base64
    assert base64.b64decode(binary_stdout["stdout"]) == b"\xff\x00"
    unknown = call("shell", "bash", {"command": "echo no"})
    unknown["tool_name"] = "mystery"
    with pytest.raises(WorkerError) as exc:
        worker.execute(unknown)
    assert exc.value.code == "invalid_call"
    malformed = call("read", "read", {"filePath": "blob", "host_path": "/etc/passwd"})
    with pytest.raises(WorkerError) as exc:
        worker.execute(malformed)
    assert exc.value.code == "invalid_call"
    assert (root / "blob").read_bytes() == b"\xff\x00"


def test_process_group_cancellation_reaps_shell(guest):
    worker, _root = guest
    started = threading.Event()
    result_holder = []

    def run():
        started.set()
        result_holder.append(worker.execute(call("shell", "bash", {"command": "sleep 30"}, call_id="cancel-me")))

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(1)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if worker.cancel("cancel-me", how="terminate"):
            break
        time.sleep(0.01)
    thread.join(3)
    assert not thread.is_alive()
    assert result_holder and result_holder[0]["exit_code"] != 0


def test_call_contract_rejects_cross_tool_and_output_uri_mutation(guest):
    worker, _root = guest
    mismatch = call("write", "write", {"filePath": "x", "content": "x"})
    mismatch["operation"] = "shell"
    with pytest.raises(WorkerError) as exc:
        worker.execute(mismatch)
    assert exc.value.code == "invalid_call"
    uri_write = call("write", "write", {"filePath": "tool-output://run-test/artifact", "content": "x"})
    with pytest.raises(WorkerError) as exc:
        worker.execute(uri_write)
    assert exc.value.code == "invalid_call"
