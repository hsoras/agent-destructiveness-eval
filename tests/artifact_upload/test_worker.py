"""Acceptance tests for the artifact-upload guest worker (T005).

Required prerequisites (pinned native executable, ripgrep, Docker) are asserted,
never skipped: a missing prerequisite fails the run.  Raw native receipts,
differential results, guest container, process and HTTP receipts are retained
under ``MIGRATION_ARTIFACT_DIR`` (default: a timestamped directory below the
repository's ``.scenario/migration-readiness/evidence``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from eval.artifact_upload.contracts import argument_digest, validate
from sandbox.artifact_upload.worker import GuestWorker, WorkerError
from sandbox.artifact_upload.worker import fixture_runner as fr
from sandbox.artifact_upload.worker import native_cases as nc
from sandbox.artifact_upload.worker import native_fixture as nf
from sandbox.artifact_upload.worker.core import group_members

REPO = Path(__file__).resolve().parents[2]
PIN_VERSION = "1.14.41"
DERIVED_BINARY = ".scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode"
DERIVED_BINARY_SHA256 = "d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6"
ALL_CASES = nc.all_cases()
CASE_INDEX = {c["id"]: c for v in ALL_CASES.values() for c in v}
TOOL_OPS = {"bash": "shell", "read": "read", "write": "write", "edit": "edit", "apply_patch": "patch", "glob": "find", "grep": "grep"}
RUN_ID = "t005-run-" + uuid.uuid4().hex[:8]
SUFFIX = "t005-" + uuid.uuid4().hex[:10]


def _common_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    return (REPO / out).resolve().parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def call(op, tool, args, *, call_id=None, run_id="run-test"):
    return {"contract_version": "artifact-upload.v1", "run_id": run_id, "session_id": "session-test", "call_id": call_id or "c-" + uuid.uuid4().hex[:8],
            "tool_name": tool, "operation": op, "arguments": args, "argument_digest": argument_digest(args), "guest_profile": "eval-looking"}


def key_of(c):
    return {k: c[k] for k in ("run_id", "session_id", "call_id", "argument_digest")}


@pytest.fixture(scope="session")
def artifact_dir():
    base = os.environ.get("MIGRATION_ARTIFACT_DIR")
    path = Path(base) if base else _common_root() / ".scenario/migration-readiness/evidence" / ("t005-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(artifact_dir: Path, name: str, data) -> None:
    (artifact_dir / name).write_text(data if isinstance(data, str) else json.dumps(data, indent=1, sort_keys=True))


@pytest.fixture
def guest(tmp_path):
    root = tmp_path / "guest"; root.mkdir()
    (tmp_path / "outside").mkdir()
    return GuestWorker(root, output_dir=tmp_path / "spool", kill_grace=0.5), root, tmp_path / "outside"


# --------------------------------------------------------------- native pin

@pytest.fixture(scope="session")
def native_binary():
    explicit = os.environ.get("OPENCODE_NATIVE_BINARY")
    candidates = [Path(explicit) if explicit else _common_root() / DERIVED_BINARY]
    assert candidates and candidates[0].is_file(), "required prerequisite missing: pinned OpenCode 1.14.41 executable (set OPENCODE_NATIVE_BINARY)"
    binary = str(candidates[0])
    assert hashlib.sha256(candidates[0].read_bytes()).hexdigest() == DERIVED_BINARY_SHA256, "native executable is not the activated derived build"
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    assert version == PIN_VERSION, f"native executable is {version!r}, expected pinned {PIN_VERSION}"
    return binary


@pytest.fixture(scope="session")
def native_results(native_binary, artifact_dir):
    """Run every fixture case through the exact pinned executable (loopback mock provider only)."""
    assert shutil.which("rg"), "required prerequisite missing: ripgrep (rg) on the host"
    receipts = {"binary": native_binary, "binary_sha256": hashlib.sha256(Path(native_binary).read_bytes()).hexdigest(), "version": PIN_VERSION, "variants": {}}
    tarball = _common_root() / ".scenario/migration-readiness/evidence/tmp-preserved-2026-10-09/opencode-8ba2a917.tar.gz"
    if tarball.is_file(): receipts["source_archive_sha256"] = hashlib.sha256(tarball.read_bytes()).hexdigest()
    receipts.update(derived_runtime_manifest="runtime/artifact_upload_host/native_patch/build-manifest.json", derived_source_archive_sha256="474edc2b546ba90e2e3d02a95af34a68394e87af0089dd8cfe1f0aa6e30eb3c8", derived_source_full_tree_sha256="3336169452bba05ada8b7eb17951e408304e491e3351b9d040e77b9d6a989add")
    for variant, cases in ALL_CASES.items():
        scratch = tempfile.mkdtemp(prefix=f"t005-native-{variant}-")
        attempt = {"attempt": 1, "scratch": scratch, "variant": variant}
        receipts["variants"][variant] = {"attempts": [attempt]}
        save(artifact_dir, "native-receipts.json", receipts)
        try:
            run = nf.run_native(native_binary, cases, variant, scratch, _free_port(), timeout=900)
        except Exception as exc:
            # Preserve transport diagnostics, including raw subprocess output,
            # before propagating. No automatic retry of an undiagnosed failure.
            def diagnostic(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            attempt.update({"exception_type": type(exc).__name__, "exception": str(exc),
                            "stdout": diagnostic(getattr(exc, "stdout", None)),
                            "stderr": diagnostic(getattr(exc, "stderr", None)),
                            "returncode": getattr(exc, "returncode", None)})
            save(artifact_dir, "native-receipts.json", receipts)
            raise
        attempt.update(run)
        receipts["variants"][variant] = {**run, "attempts": [attempt]}
        save(artifact_dir, "native-receipts.json", receipts)
        assert run["returncode"] == 0, f"native {variant} exited {run['returncode']}: {run['stderr'][-1000:]}"
        assert len(run["results"]) == len(cases), f"native {variant} run incomplete: {run['stderr'][-500:]}"
    return receipts


@pytest.mark.parametrize("failure", ["incomplete", "nonzero", "later_variant", "transport"])
def test_native_failed_attempt_receipts_are_durable(tmp_path, monkeypatch, failure, artifact_dir):
    binary = tmp_path / "binary"; binary.write_bytes(b"fixture-only")
    variants = {"edit": [{"id": "one"}], "patch": [{"id": "two"}]}
    monkeypatch.setitem(globals(), "ALL_CASES", variants)
    monkeypatch.setattr(shutil, "which", lambda _: "/fixture/rg")
    monkeypatch.setitem(globals(), "_free_port", lambda: 12345)
    calls = []

    def run(*args, **kwargs):
        variant = args[2]; calls.append(variant)
        # The earlier variant must already be durable before another executes.
        retained = json.loads((tmp_path / "native-receipts.json").read_text())
        if variant == "patch":
            assert retained["variants"]["edit"]["stdout"] == "SUCCESS_RAW"
            assert retained["variants"]["edit"]["attempts"][0]["returncode"] == 0
        if failure == "transport":
            raise subprocess.CalledProcessError(9, ["fixture"], output=b"TRANSPORT_RAW", stderr=b"transport diagnostic")
        success = failure == "later_variant" and variant == "edit"
        return {"returncode": 0 if success or failure == "incomplete" else 7,
                "stdout": "SUCCESS_RAW" if success else "FAILURE_RAW",
                "stderr": "raw diagnostic", "results": [{}] if success else []}

    monkeypatch.setattr(nf, "run_native", run)
    with pytest.raises(subprocess.CalledProcessError if failure == "transport" else AssertionError):
        native_results.__wrapped__(str(binary), tmp_path)
    retained = json.loads((tmp_path / "native-receipts.json").read_text())
    failed_variant = "patch" if failure == "later_variant" else "edit"
    attempt = retained["variants"][failed_variant]["attempts"][0]
    assert attempt["attempt"] == 1 and Path(attempt["scratch"]).is_dir()
    assert attempt["stdout"] == ("TRANSPORT_RAW" if failure == "transport" else "FAILURE_RAW")
    assert attempt["stderr"] == ("transport diagnostic" if failure == "transport" else "raw diagnostic")
    assert attempt["returncode"] == (9 if failure == "transport" else 0 if failure == "incomplete" else 7)
    if failure == "transport": assert attempt["exception_type"] == "CalledProcessError"
    assert calls == (["edit", "patch"] if failure == "later_variant" else ["edit"])
    save(artifact_dir, f"fixture-failure-{failure}.json", retained)


def test_native_pin_and_tool_inventory(native_results, artifact_dir):
    assert native_results["version"] == PIN_VERSION
    patch_inv, edit_inv = (native_results["variants"][v]["inventory"] for v in ("patch", "edit"))
    assert {"bash", "read", "glob", "grep", "apply_patch"} <= set(patch_inv) and not {"edit", "write"} & set(patch_inv)
    assert {"bash", "read", "glob", "grep", "edit", "write"} <= set(edit_inv) and "apply_patch" not in edit_inv
    save(artifact_dir, "native-inventory.json", {"patch_model": patch_inv, "edit_model": edit_inv})


@pytest.fixture(scope="session")
def worker_host_run(native_results):
    return {variant: fr.run_worker(cases, tempfile.mkdtemp(prefix=f"t005-worker-{variant}-")) for variant, cases in ALL_CASES.items()}


def _pair(native_results, worker_run, case):
    variant = case["variant"]
    native, worker = native_results["variants"][variant], worker_run[variant]
    n = next(r for r in native["results"] if r["id"] == case["id"]); w = next(r for r in worker["results"] if r["id"] == case["id"])
    return n, w, (native["root"], native["out"]), (worker["root"], worker["out"])


@pytest.mark.parametrize("case_id", sorted(CASE_INDEX))
def test_native_differential_host(case_id, native_results, worker_host_run):
    case = CASE_INDEX[case_id]
    n, w, npaths, wpaths = _pair(native_results, worker_host_run, case)
    if case["compare"] == "worker-stricter":
        assert w["result"]["status"] == "error", w["result"]  # state preservation is proven in test_worker_stricter_cases_leave_state_unchanged
        return
    problems = fr.compare(case, n, w, npaths, wpaths)
    assert not problems, "\n".join(problems)


def test_differential_summary_receipt(native_results, worker_host_run, artifact_dir):
    rows = []
    for case in CASE_INDEX.values():
        n, w, npaths, wpaths = _pair(native_results, worker_host_run, case)
        problems = [] if case["compare"] == "worker-stricter" else fr.compare(case, n, w, npaths, wpaths)
        rows.append({"id": case["id"], "compare": case["compare"], "native_output": n["output"], "worker": w["result"], "problems": problems})
    save(artifact_dir, "differential-host.json", {"count": len(rows), "cases": rows})
    assert len(rows) >= 150 and not [r for r in rows if r["problems"]]


def test_worker_stricter_cases_leave_state_unchanged(guest):
    worker, root, out = guest
    stricter = [x for x in CASE_INDEX.values() if x["compare"] == "worker-stricter"]
    assert stricter
    for c in stricter:
        nf.clear(str(root)); nf.apply_setup(str(root), str(out), c["setup"])
        before = nf.snapshot(str(root))
        args = json.loads(json.dumps(c["args"]).replace("{ROOT}", str(root.resolve())))
        res = worker.execute(call(TOOL_OPS[c["tool"]], c["tool"], args))
        assert res["status"] == "error" and res["error_code"] in ("patch_failed", "permission_denied"), (c["id"], res)
        assert nf.snapshot(str(root)) == before, c["id"]


# ------------------------------------------------- finding-specific checks

def test_read_search_edit_write_patch_share_state_and_native_wrappers(guest):
    worker, root, _ = guest
    ex = lambda *a, **k: worker.execute(call(*a, **k))
    assert ex("write", "write", {"filePath": "w/a.txt", "content": "one\ntwo\n"})["stdout"] == "Wrote file successfully."
    assert "2: two" in ex("read", "read", {"filePath": "w/a.txt", "offset": 2})["stdout"]  # one-based offset
    assert ex("read", "read", {"filePath": "w/a.txt", "offset": 1, "limit": 1})["stdout"].count(": ") == 1
    r = ex("read", "read", {"filePath": "w/a.txt", "offset": 9})
    assert r["status"] == "error" and r["stderr"] == "Offset 9 is out of range for this file (2 lines)"
    assert ex("edit", "edit", {"filePath": "w/a.txt", "oldString": "two", "newString": "2"})["stdout"] == "Edit applied successfully."
    assert ex("find", "glob", {"pattern": "**/*.txt"})["stdout"] == str(root.resolve() / "w/a.txt")
    assert "Line 2: 2" in ex("grep", "grep", {"pattern": "^2$", "include": "*.txt"})["stdout"]
    assert ex("shell", "bash", {"command": "cat w/a.txt"})["stdout"] == "one\n2\n"
    assert ex("patch", "apply_patch", {"patchText": "*** Begin Patch\n*** Update File: w/a.txt\n@@\n-one\n+ONE\n*** End Patch"})["status"] == "ok"
    assert ex("shell", "bash", {"command": "head -1 w/a.txt"})["stdout"] == "ONE\n"


def test_encoding_and_permission_behaviour(guest):
    worker, root, _ = guest
    p = root / "f.txt"; p.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    assert worker.execute(call("edit", "edit", {"filePath": "f.txt", "oldString": "two", "newString": "three"}))["status"] == "ok"
    assert p.read_bytes() == b"\xef\xbb\xbfone\r\nthree\r\n"
    assert worker.execute(call("write", "write", {"filePath": "f.txt", "content": "plain\n"}))["status"] == "ok"
    assert p.read_bytes() == b"\xef\xbb\xbfplain\n"
    assert worker.execute(call("edit", "edit", {"filePath": "new.txt", "oldString": "", "newString": "created"}))["status"] == "ok"
    same = worker.execute(call("edit", "edit", {"filePath": "f.txt", "oldString": "plain", "newString": "plain"}))
    assert same["status"] == "error" and "identical" in same["stderr"]
    p.chmod(0o444)
    denied = worker.execute(call("write", "write", {"filePath": "f.txt", "content": "x"}))
    assert denied["status"] == "error" and denied["error_code"] == "permission_denied" and p.read_bytes() == b"\xef\xbb\xbfplain\n"
    (root / "nul.txt").write_bytes(b"ok\x00ok\n")
    assert worker.execute(call("read", "read", {"filePath": "nul.txt"}))["error_code"] == "binary_file"


def test_patch_delete_forms_and_atomic_failure(guest):
    worker, root, _ = guest
    (root / "a.txt").write_text("a\n"); (root / "d").mkdir(); (root / "d/keep").write_text("k")
    for form in (f"{root.resolve()}/a.txt", "./a.txt", "a.txt"):
        (root / "a.txt").write_text("a\n")
        res = worker.execute(call("patch", "apply_patch", {"patchText": f"*** Begin Patch\n*** Delete File: {form}\n*** End Patch"}))
        assert res["status"] == "ok" and not (root / "a.txt").exists(), (form, res)
    before = nf.snapshot(str(root))
    bad = worker.execute(call("patch", "apply_patch", {"patchText": "*** Begin Patch\n*** Add File: n.txt\n+n\n*** Delete File: d\n*** End Patch"}))
    assert bad["status"] == "error" and nf.snapshot(str(root)) == before
    assert worker.execute(call("patch", "apply_patch", {"patchText": "*** Begin Patch\n*** End Patch"}))["stderr"] == "patch rejected: empty patch"


def test_late_io_failure_rolls_back_patch(guest, monkeypatch):
    worker, root, _ = guest
    (root / "a.txt").write_text("alpha\n"); (root / "b.txt").write_text("beta\n")
    from sandbox.artifact_upload.worker import tools
    real = os.unlink

    def failing(path, *a, **k):
        if str(path).endswith("b.txt"): raise OSError(28, "No space left on device", str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(tools.os, "unlink", failing)
    patch = "*** Begin Patch\n*** Add File: new.txt\n+n\n*** Update File: a.txt\n@@\n-alpha\n+ALPHA\n*** Delete File: b.txt\n*** End Patch"
    before = nf.snapshot(str(root))
    res = worker.execute(call("patch", "apply_patch", {"patchText": patch}))
    assert res["status"] == "error" and nf.snapshot(str(root)) == before


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_patch_hardlink_unlink_rejected_without_mutation(guest, monkeypatch, operation):
    worker, root, _ = guest
    (root / "a").write_text("alpha\n"); (root / "a").chmod(0o640)
    os.link(root / "a", root / "alias")
    (root / "fail").write_text("failure\n")
    before = nf.snapshot(str(root)); writes = []; unlinks = []
    from sandbox.artifact_upload.worker import tools
    real_write, real_unlink = tools.write_with_dirs, tools.os.unlink
    monkeypatch.setattr(tools, "write_with_dirs", lambda *a, **k: (writes.append(a[0]), real_write(*a, **k))[1])
    def unlink(path, *a, **k):
        unlinks.append(str(path))
        if str(path) == str((root / "fail").resolve()): raise OSError(28, "late ENOSPC", str(path))
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(tools.os, "unlink", unlink)
    first = "*** Delete File: a" if operation == "delete" else "*** Update File: a\n*** Move to: moved\n@@\n-alpha\n+one"
    result = worker.execute(call("patch", "apply_patch", {"patchText": f"*** Begin Patch\n{first}\n*** Delete File: fail\n*** End Patch"}))
    assert result["status"] == "error" and "multiply linked" in result["stderr"]
    assert not writes and not unlinks and nf.snapshot(str(root)) == before
    assert os.path.samefile(root / "a", root / "alias")
    (root / "a").write_text("later\n")
    assert (root / "alias").read_text() == "later\n"


def test_patch_hardlink_update_late_rollback_and_valid_symlink_delete(guest, monkeypatch):
    worker, root, _ = guest
    (root / "a").write_text("alpha\n"); os.link(root / "a", root / "alias")
    (root / "link").symlink_to("a"); (root / "fail").write_text("failure\n")
    from sandbox.artifact_upload.worker import tools
    real = tools.os.unlink
    def unlink(path, *a, **k):
        if str(path) == str((root / "fail").resolve()): raise OSError(28, "late ENOSPC", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(tools.os, "unlink", unlink)
    before = nf.snapshot(str(root))
    patch = "*** Begin Patch\n*** Update File: a\n@@\n-alpha\n+one\n*** Delete File: fail\n*** End Patch"
    assert worker.execute(call("patch", "apply_patch", {"patchText": patch}))["status"] == "error"
    assert nf.snapshot(str(root)) == before
    (root / "a").write_text("later\n"); assert (root / "alias").read_text() == "later\n"
    patch = "*** Begin Patch\n*** Delete File: link\n*** Add File: nested/new\n+new\n*** End Patch"
    assert worker.execute(call("patch", "apply_patch", {"patchText": patch}))["status"] == "ok"
    assert not (root / "link").is_symlink() and os.path.samefile(root / "a", root / "alias")
    assert (root / "nested/new").read_text() == "new\n"


@pytest.mark.parametrize("patch", [
    "*** Begin Patch\n*** Update File: a.txt\n*** Move to: link.txt\n@@\n-alpha\n+changed\n*** End Patch",
    "*** Begin Patch\n*** Update File: a.txt\n@@\n-alpha\n+first\n*** Update File: link.txt\n@@\n-alpha\n+second\n*** End Patch",
])
def test_patch_rejects_symlink_alias_targets_before_writes(guest, monkeypatch, patch):
    worker, root, _ = guest
    target = root / "a.txt"; target.write_text("alpha\n"); (root / "link.txt").symlink_to("a.txt")
    before = nf.snapshot(str(root)); calls = []
    from sandbox.artifact_upload.worker import tools
    real = tools.write_with_dirs
    monkeypatch.setattr(tools, "write_with_dirs", lambda *args, **kwargs: (calls.append(args[0]), real(*args, **kwargs))[1])
    result = worker.execute(call("patch", "apply_patch", {"patchText": patch}))
    assert result["status"] == "error" and calls == []
    assert nf.snapshot(str(root)) == before


def test_patch_rejects_ancestor_destination_conflict_before_writes(guest, monkeypatch):
    worker, root, _ = guest
    target = root / "a.txt"; target.write_text("alpha\n"); (root / "link.txt").symlink_to("a.txt")
    before = nf.snapshot(str(root)); calls = []; unlinks = []
    from sandbox.artifact_upload.worker import tools
    real_write, real_unlink = tools.write_with_dirs, tools.os.unlink
    monkeypatch.setattr(tools, "write_with_dirs", lambda *args, **kwargs: (calls.append(args[0]), real_write(*args, **kwargs))[1])
    monkeypatch.setattr(tools.os, "unlink", lambda *args, **kwargs: (unlinks.append(args[0]), real_unlink(*args, **kwargs))[1])
    patch = "*** Begin Patch\n*** Delete File: link.txt\n*** Add File: d/a.txt\n+x\n*** Add File: d\n+y\n*** End Patch"
    result = worker.execute(call("patch", "apply_patch", {"patchText": patch}))
    assert result["status"] == "error" and calls == [] and unlinks == []
    assert nf.snapshot(str(root)) == before


def test_late_io_rollback_preserves_symlink_identity(guest, monkeypatch):
    worker, root, _ = guest
    (root / "target.txt").write_text("target\n"); (root / "link.txt").symlink_to("target.txt"); (root / "fail.txt").write_text("fail\n")
    before = nf.snapshot(str(root))
    from sandbox.artifact_upload.worker import tools
    real = os.unlink
    def failing(path, *a, **k):
        if str(path).endswith("fail.txt"): raise OSError(28, "No space left on device", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(tools.os, "unlink", failing)
    patch = "*** Begin Patch\n*** Delete File: link.txt\n*** Delete File: fail.txt\n*** End Patch"
    result = worker.execute(call("patch", "apply_patch", {"patchText": patch}))
    assert result["status"] == "error" and nf.snapshot(str(root)) == before


def test_boundary_applies_to_every_handler_and_traversal_candidate(tmp_path):
    root = tmp_path / "guest"; root.mkdir(); vol = tmp_path / "service-volume"; vol.mkdir()
    (vol / "secret.txt").write_text("service-secret\n")
    allowed = tmp_path / "elsewhere"; allowed.mkdir(); (allowed / "ok.txt").write_text("legit-guest-file\n")
    worker = GuestWorker(root, protected_paths=[str(vol)], output_dir=tmp_path / "spool")
    (root / "to_vol").symlink_to(vol, target_is_directory=True)
    (root / "vol_file").symlink_to(vol / "secret.txt")
    (root / "to_ok").symlink_to(allowed / "ok.txt")
    (root / "plain.txt").write_text("plain service-secret\n")
    ex = lambda *a: worker.execute(call(*a))
    for op, tool, args in (("read", "read", {"filePath": "vol_file"}), ("read", "read", {"filePath": str(vol / "secret.txt")}), ("read", "read", {"filePath": "to_vol"}),
                           ("write", "write", {"filePath": "to_vol/new.txt", "content": "x"}), ("edit", "edit", {"filePath": "vol_file", "oldString": "a", "newString": "b"}),
                           ("find", "glob", {"pattern": "*", "path": "to_vol"}), ("grep", "grep", {"pattern": "service-secret", "path": str(vol)}),
                           ("patch", "apply_patch", {"patchText": f"*** Begin Patch\n*** Add File: {vol}/p.txt\n+x\n*** End Patch"}),
                           ("shell", "bash", {"command": "pwd", "workdir": "to_vol"})):
        res = ex(op, tool, args)
        assert res["status"] == "error" and res["error_code"] == "boundary_violation", (op, res)
    assert not (vol / "new.txt").exists() and not (vol / "p.txt").exists()
    # Traversal never follows symlinks and every returned candidate is boundary-checked.
    grep = ex("grep", "grep", {"pattern": "service-secret"})["stdout"]
    assert "plain.txt" in grep and "vol_file" not in grep and "secret.txt" not in grep
    glob = ex("find", "glob", {"pattern": "**/*"})["stdout"]
    assert "plain.txt" in glob and "vol_file" not in glob and "to_vol" not in glob
    assert "legit-guest-file" in ex("read", "read", {"filePath": "to_ok"})["stdout"]  # in-guest symlinks keep normal semantics
    from sandbox.artifact_upload.worker import tools
    with pytest.raises(tools.ToolError): worker._ctx().check(str(root / "vol_file"))
    # Defence in depth: even if ripgrep reported a candidate that resolves into protected space it is dropped.
    fake = tmp_path / "fake-rg"; fake.write_text(f"#!/bin/sh\necho ./vol_file\necho ./plain.txt\n"); fake.chmod(0o755)
    worker.rg = str(fake)
    listed = ex("find", "glob", {"pattern": "*"})["stdout"]
    assert listed == str(root.resolve() / "plain.txt")


def test_forbidden_identity_arguments_and_unknown_operations_fail_closed(guest):
    worker, _, _ = guest
    for extra in ({"host_path": "/etc/passwd"}, {"container": "other"}, {"volume": "svc"}, {"run_id": "other"}):
        with pytest.raises(WorkerError) as exc: worker.execute(call("read", "read", {"filePath": "x", **extra}))
        assert exc.value.code == "invalid_call"
    unknown = call("shell", "bash", {"command": "true"}); unknown["tool_name"] = "mystery"
    with pytest.raises(WorkerError): worker.execute(unknown)
    mismatch = call("write", "write", {"filePath": "x", "content": "x"}); mismatch["operation"] = "shell"
    with pytest.raises(WorkerError): worker.execute(mismatch)
    with pytest.raises(WorkerError): worker.execute(call("grep", "grep", {"pattern": ""}))
    assert worker.capabilities()["persistent_sessions"] is False and "lsp" in worker.capabilities()["disabled"]


def test_shell_quoting_pipeline_redirection_exit_streams_and_binary(guest):
    worker, root, _ = guest
    (root / "work").mkdir()
    res = worker.execute(call("shell", "bash", {"command": "printf '%s\\n' \"$VALUE\" | tr a-z A-Z > output.txt; printf 'err' >&2; exit 7", "workdir": "work", "env": {"VALUE": "quoted value"}}))
    assert (res["status"], res["exit_code"], res["stdout"], res["stderr"]) == ("error", 7, "", "err")
    assert (root / "work/output.txt").read_text() == "QUOTED VALUE\n"
    assert worker.execute(call("shell", "bash", {"command": "id -u"}))["stdout"].strip() == str(os.getuid())
    binary = worker.execute(call("shell", "bash", {"command": r"printf '\377\000'"}))
    assert binary["encoding"] == "base64" and base64.b64decode(binary["stdout"]) == b"\xff\x00"
    validate("result", binary)


def test_shell_timeout_is_milliseconds_with_native_default(guest):
    worker, _, _ = guest
    t0 = time.monotonic()
    res = worker.execute(call("shell", "bash", {"command": "echo started; sleep 20", "timeout": 600}))
    assert time.monotonic() - t0 < 10
    assert res["error_code"] == "timeout" and res["stdout"].startswith("started\n") and "exceeding timeout 600 ms" in res["stdout"]
    from sandbox.artifact_upload.worker.core import DEFAULT_SHELL_TIMEOUT_MS
    assert DEFAULT_SHELL_TIMEOUT_MS == 120_000


def _alive(pid):
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def test_background_descendants_do_not_outlive_shell(guest, tmp_path):
    worker, _, _ = guest
    pidfile = tmp_path / "bg.pid"
    res = worker.execute(call("shell", "bash", {"command": f"sleep 60 >/dev/null 2>&1 & echo $! > {pidfile}; nohup sleep 61 >/dev/null 2>&1 & echo done"}))
    assert res["status"] == "ok" and res["stdout"] == "done\n"
    assert not _alive(int(pidfile.read_text())), "background process survived its shell"


def test_session_escaping_descendant_is_also_reaped(guest, tmp_path):
    worker, _, _ = guest
    pidfile = tmp_path / "setsid.pid"
    py = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); os.setsid(); time.sleep(60)"
    worker.execute(call("shell", "bash", {"command": f"python3 -c \"{py}\" >/dev/null 2>&1 & sleep 1; echo ok"}))
    assert pidfile.exists() and not _alive(int(pidfile.read_text()))


def test_cancellation_reaches_descendants_and_reports_cancelled(guest, tmp_path):
    worker, _, _ = guest
    pidfile = tmp_path / "child.pid"
    request = call("shell", "bash", {"command": f"sleep 60 & echo $! > {pidfile}; wait"}, call_id="cancel-tree")
    handle = worker.start(request)
    assert handle["process_id"] == handle["process_group_id"] and handle["worker_id"] == worker.worker_id
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline: time.sleep(0.05)
    child = int(pidfile.read_text()); assert _alive(child)
    assert worker.cancel(dict(key_of(request), run_id="other-run")) is False  # cross-run identity cannot cancel
    with pytest.raises(WorkerError): worker.cancel("cancel-tree")  # call_id alone is rejected
    assert worker.cancel(key_of(request), how="terminate") is True
    result = list(worker.stream(key_of(request)))[-1]["result"]
    assert result["status"] == "cancelled" and result["error_code"] == "cancelled"
    assert not _alive(child) and not group_members(int(handle["process_group_id"]))
    assert worker.cancel(key_of(request)) is False  # stale handle
    with pytest.raises(WorkerError) as exc: worker.start(request)
    assert exc.value.code == "duplicate_execution"


def test_cancellation_escalates_when_term_is_ignored(guest):
    worker, _, _ = guest
    request = call("shell", "bash", {"command": "trap '' TERM; while true; do sleep 1; done"}, call_id="stubborn")
    handle = worker.start(request); time.sleep(0.3)
    assert worker.cancel(key_of(request), how="terminate")
    result = list(worker.stream(key_of(request)))[-1]["result"]
    assert result["status"] == "cancelled" and not group_members(int(handle["process_group_id"]))


def test_stream_emits_chunks_promptly_in_order_and_fast_commands_keep_their_handle(guest):
    worker, _, _ = guest
    request = call("shell", "bash", {"command": "printf one; sleep 1; printf two >&2; sleep 1; printf three"}, call_id="stream-slow")
    assert worker.start(request)["process_id"]
    t0 = time.monotonic(); first = None; events = []
    for event in worker.stream(key_of(request)):
        events.append(event)
        if first is None and event["type"] != "result": first = time.monotonic() - t0
    assert first is not None and first < 0.9, "first chunk was not streamed before the command finished"
    assert b"".join(base64.b64decode(e["data_base64"]) for e in events if e["type"] == "stdout") == b"onethree"
    assert any(e["type"] == "stderr" for e in events) and events[-1]["result"]["stdout"] == "onethree"
    fast = call("shell", "bash", {"command": "printf fast"}, call_id="stream-fast")
    h = worker.start(fast)
    assert h["process_id"] == h["process_group_id"]
    deadline = time.monotonic() + 3
    while worker.status(key_of(fast))["state"] != "completed" and time.monotonic() < deadline: time.sleep(0.01)
    assert worker.process_handle(fast) == h and worker.status(key_of(fast))["result"]["stdout"] == "fast"
    assert list(worker.stream(key_of(fast)))[-1]["type"] == "result" and worker.process_handle(fast) is None
    with pytest.raises(WorkerError) as exc: worker.status(key_of(fast))
    assert exc.value.code == "unknown_execution"
    with pytest.raises(WorkerError): worker.status({"run_id": "x"})


def test_large_output_is_bounded_and_spooled(guest):
    worker, _, _ = guest
    res = worker.execute(call("shell", "bash", {"command": "seq 1 5000"}))
    assert res["stdout"].startswith("...output truncated...\n\nFull output saved to: ") and res["stdout"].rstrip().endswith("5000")
    assert Path(res["stdout"].split("Full output saved to: ")[1].split("\n")[0]).read_text().splitlines()[-1] == "5000"


# --------------------------------------------------------- Docker guest

def _docker(*args, check=True, timeout=600):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed ({p.returncode}): {p.stderr[-2000:]}")
    return p


class GuestSession:
    """JSON-line transport to the worker inside the guest container."""

    def __init__(self, container, log, run_id=RUN_ID):
        self.log = log
        self.proc = subprocess.Popen(["docker", "exec", "-i", "-u", "0", "-e", "ARTIFACT_AGENT_UID=10001", "-e", "PYTHONPATH=/opt/worker", "-e", "PYTHONSAFEPATH=1", "-e", f"ARTIFACT_RUN_ID={run_id}", "-e", "ARTIFACT_PROTECTED_PATHS=/svc-private", container, "python", "-m", "sandbox.artifact_upload.worker.guest_session"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def send(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n"); self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        self.log.append({"request": req, "reply": reply})
        return reply

    def run(self, op, tool, args, **kw):
        reply = self.send({"op": "execute", "call": call(op, tool, args, run_id=RUN_ID, **kw)})
        assert "result" in reply, reply
        return reply["result"]


@pytest.fixture(scope="session")
def docker_env(artifact_dir):
    assert shutil.which("docker"), "required prerequisite missing: docker CLI"
    info = _docker("info", "--format", "{{.ServerVersion}} {{.OSType}}/{{.Architecture}}").stdout.strip()
    # ARTIFACT_WORKER_TEST_IMAGE runs the same cases in an existing guest image (e.g. the production candidate); it is never removed here.
    external = os.environ.get("ARTIFACT_WORKER_TEST_IMAGE") or None
    image = external or f"artifact-upload-{SUFFIX}:guest"; net = f"{SUFFIX}-net"; vol = f"{SUFFIX}-svcvol"
    guest_name = f"{SUFFIX}-guest"; svc_name = f"{SUFFIX}-svc"
    created = {"containers": [], "networks": [], "volumes": [], "images": []}
    env = {"info": info, "image": image, "net": net, "guest": guest_name, "svc": svc_name, "vol": vol, "log": [], "created": created}
    try:
        if external is None:
            p = subprocess.run(["docker", "build", "--file", "sandbox/artifact_upload/worker/Dockerfile", "--tag", image, "."], cwd=REPO, capture_output=True, text=True, timeout=1800)
            save(artifact_dir, "docker-build.log", p.stdout + "\n" + p.stderr)
            assert p.returncode == 0, f"guest image build failed: {p.stderr[-3000:]}"
            created["images"].append(image)
        env["image_inspect"] = json.loads(_docker("image", "inspect", image).stdout)[0]
        _docker("network", "create", net); created["networks"].append(net)
        _docker("volume", "create", vol); created["volumes"].append(vol)
        # Service: a separate container with a private volume serving ordinary HTTP on :8080.
        _docker("run", "-d", "--name", svc_name, "--network", net, "--network-alias", "service", "-v", f"{vol}:/svc-private", "--user", "0:0", image,
                "sh", "-c", "echo service-ready > /svc-private/index.txt && cd /svc-private && exec python -m http.server 8080")
        created["containers"].append(svc_name)
        _docker("run", "-d", "--init", "--name", guest_name, "--network", net, "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=CHOWN", "--cap-add=KILL", "--cap-add=DAC_OVERRIDE", "--security-opt", "no-new-privileges", image, "sleep", "3600")
        created["containers"].append(guest_name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _docker("exec", guest_name, "python", "-c", "import urllib.request;urllib.request.urlopen('http://service:8080/index.txt',timeout=2)", check=False).returncode == 0: break
            time.sleep(0.5)
        yield env
    finally:
        save(artifact_dir, "docker-session-log.json", env["log"])
        teardown = {}
        for c in created["containers"]:
            teardown["logs " + c] = subprocess.run(["docker", "logs", c], capture_output=True, text=True).stdout[-4000:]
            teardown["rm " + c] = subprocess.run(["docker", "rm", "-f", c], capture_output=True, text=True).returncode
        for n in created["networks"]: teardown["network rm " + n] = subprocess.run(["docker", "network", "rm", n], capture_output=True, text=True).returncode
        for v in created["volumes"]: teardown["volume rm " + v] = subprocess.run(["docker", "volume", "rm", v], capture_output=True, text=True).returncode
        for i in created["images"]: teardown["image rm " + i] = subprocess.run(["docker", "rmi", i], capture_output=True, text=True).returncode
        save(artifact_dir, "docker-teardown.json", teardown)


@pytest.fixture(scope="session")
def session(docker_env):
    s = GuestSession(docker_env["guest"], docker_env["log"])
    yield s
    s.proc.stdin.close(); s.proc.wait(timeout=20)


def test_guest_container_identity_isolation_and_runtime(docker_env, artifact_dir, session):
    name = docker_env["guest"]
    inspect = json.loads(_docker("inspect", name).stdout)[0]
    img = docker_env["image_inspect"]
    py = _docker("exec", name, "python", "--version").stdout.strip()
    assert py.startswith("Python 3.11"), py
    ident = _docker("exec", name, "sh", "-c", "id -u; id -g; test ! -e /var/run/docker.sock && echo no-docker-sock; ls /svc-private 2>&1 | head -1; env").stdout
    assert ident.splitlines()[:2] == ["10001", "10001"] and "no-docker-sock" in ident and "No such file" in ident
    assert not any(tok in ident.upper() for tok in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")), "provider secret-like variable present in guest environment"
    assert inspect["Mounts"] == [], inspect["Mounts"]
    assert inspect["HostConfig"]["Binds"] in (None, []) and inspect["HostConfig"]["Privileged"] is False
    pkgs = _docker("exec", name, "dpkg-query", "-W", "git", "ripgrep", "curl", "procps").stdout
    caps = session.send({"op": "capabilities"})
    assert caps["uid"] == 10001 and caps["gid"] == 10001 and "lsp" in caps["capabilities"]["disabled"]
    save(artifact_dir, "guest-runtime.json", {"docker": docker_env["info"], "image": docker_env["image"], "image_id": img["Id"], "base_repo_digests": img.get("RepoDigests"), "python": py,
                                              "uid_gid_env": ident, "mounts": inspect["Mounts"], "binds": inspect["HostConfig"]["Binds"], "network_mode": inspect["HostConfig"]["NetworkMode"],
                                              "container_env_names": sorted(e.split("=")[0] for e in inspect["Config"]["Env"]), "packages": pkgs, "worker": caps})


def test_guest_handlers_git_python_tests_and_http_share_one_filesystem(docker_env, artifact_dir, session):
    run = session.run
    assert run("write", "write", {"filePath": "app/greet.py", "content": "def greet():\n    return 'hello'\n"})["status"] == "ok"
    assert run("write", "write", {"filePath": "app/test_greet.py", "content": "import unittest\nfrom greet import greet\n\nclass T(unittest.TestCase):\n    def test_greet(self):\n        self.assertEqual(greet(), 'hello world')\n"})["status"] == "ok"
    failing = run("shell", "bash", {"command": "cd app && python -m unittest test_greet 2>&1; echo rc=$?"})
    assert "FAIL" in failing["stdout"] and "rc=1" in failing["stdout"]
    assert run("edit", "edit", {"filePath": "app/greet.py", "oldString": "'hello'", "newString": "'hello world'"})["status"] == "ok"
    passing = run("shell", "bash", {"command": "cd app && python -m unittest test_greet 2>&1 && python -m compileall -q . && echo BUILD-OK"})
    assert passing["status"] == "ok" and "OK" in passing["stdout"] and "BUILD-OK" in passing["stdout"], passing
    assert run("patch", "apply_patch", {"patchText": "*** Begin Patch\n*** Add File: app/README.txt\n+docs\n*** Update File: app/greet.py\n@@\n-def greet():\n+def greet():  # patched\n*** End Patch"})["status"] == "ok"
    assert "patched" in run("read", "read", {"filePath": "app/greet.py"})["stdout"]
    assert "app/README.txt" in run("find", "glob", {"pattern": "**/*.txt"})["stdout"]
    assert "Line 1: def greet():  # patched" in run("grep", "grep", {"pattern": "patched", "path": "app"})["stdout"]
    git = run("shell", "bash", {"command": "git init -q && git config user.email t@example.invalid && git config user.name t && git add -A && git commit -q -m init && git status --short && git log --oneline"})
    assert git["status"] == "ok" and "init" in git["stdout"], git
    run("write", "write", {"filePath": "app/greet.py", "content": "def greet():\n    return 'changed'\n"})
    diff = run("shell", "bash", {"command": "git diff --stat && git diff | grep -c changed"})
    assert "app/greet.py" in diff["stdout"]
    same_uid = run("shell", "bash", {"command": "id -u; stat -c '%u' app/greet.py app/README.txt; python -c \"import os;print(os.stat('app/greet.py').st_uid)\""})
    assert set(same_uid["stdout"].split()) == {"10001"}, same_uid
    http = run("shell", "bash", {"command": "curl -sS -m 5 -i http://service:8080/index.txt; echo; python -c \"import urllib.request;print(urllib.request.urlopen('http://service:8080/index.txt',timeout=5).read().decode())\""})
    assert http["status"] == "ok" and http["stdout"].count("service-ready") >= 2 and "200 OK" in http["stdout"], http
    # The service's private volume is neither part of the guest filesystem nor reachable through any handler.
    assert run("read", "read", {"filePath": "/svc-private/index.txt"})["status"] == "error"
    assert run("shell", "bash", {"command": "ls /svc-private"})["status"] == "error"
    save(artifact_dir, "guest-http-and-state-receipts.json", {"http_stdout": http["stdout"], "git": git["stdout"], "diff": diff["stdout"], "uid_check": same_uid["stdout"], "tests": passing["stdout"]})


def test_guest_patch_preflight_preserves_aliases_and_overlaps(session, artifact_dir):
    run = session.run
    assert run("write", "write", {"filePath": "alias-target.txt", "content": "original\n"})["status"] == "ok"
    assert run("shell", "bash", {"command": "ln -s alias-target.txt alias-link.txt"})["status"] == "ok"
    alias_patch = "*** Begin Patch\n*** Update File: alias-target.txt\n*** Move to: alias-link.txt\n@@\n-original\n+changed\n*** End Patch"
    alias_result = run("patch", "apply_patch", {"patchText": alias_patch})
    alias_type = run("shell", "bash", {"command": "test -L alias-link.txt && readlink alias-link.txt && cat alias-target.txt"})
    assert alias_result["status"] == "error" and alias_type["stdout"] == "alias-target.txt\noriginal\n", (alias_result, alias_type)
    overlap_patch = "*** Begin Patch\n*** Delete File: alias-link.txt\n*** Add File: d/a.txt\n+x\n*** Add File: d\n+y\n*** End Patch"
    overlap_result = run("patch", "apply_patch", {"patchText": overlap_patch})
    overlap_state = run("shell", "bash", {"command": "test -L alias-link.txt && test ! -e d && cat alias-target.txt && echo unchanged"})
    assert overlap_result["status"] == "error" and overlap_state["stdout"] == "original\nunchanged\n", (overlap_result, overlap_state)
    save(artifact_dir, "guest-patch-preflight.json", {"alias_error": alias_result, "alias_state": alias_type["stdout"], "overlap_error": overlap_result, "overlap_state": overlap_state["stdout"]})


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_guest_hardlink_patch_rejection_preserves_write_relationship(session, artifact_dir, operation):
    run = session.run
    setup = run("shell", "bash", {"command": "rm -f hard-a hard-alias hard-moved hard-fail; printf 'alpha\\n' > hard-a; chmod 640 hard-a; ln hard-a hard-alias; printf 'failure\\n' > hard-fail"})
    assert setup["exit_code"] == 0
    inspect = "python -c \"import os,json; from pathlib import Path; print(json.dumps({p: [Path(p).read_text(),os.stat(p).st_mode,os.stat(p).st_nlink] for p in ['hard-a','hard-alias','hard-fail']},sort_keys=True))\""
    before = run("shell", "bash", {"command": inspect})
    first = "*** Delete File: hard-a" if operation == "delete" else "*** Update File: hard-a\n*** Move to: hard-moved\n@@\n-alpha\n+one"
    result = run("patch", "apply_patch", {"patchText": f"*** Begin Patch\n{first}\n*** Delete File: hard-fail\n*** End Patch"})
    after = run("shell", "bash", {"command": inspect})
    assert result["status"] == "error" and "multiply linked" in result["stderr"]
    assert before["stdout"] == after["stdout"]
    followup = run("shell", "bash", {"command": "test ! -e hard-moved && printf 'later\\n' > hard-a && cat hard-alias"})
    assert followup["exit_code"] == 0 and followup["stdout"] == "later\n"
    save(artifact_dir, f"guest-hardlink-{operation}.json", {"before": before, "error": result, "after": after, "followup": followup})


def test_guest_identity_arguments_fail_closed_and_host_sentinel_intact(docker_env, artifact_dir, session):
    sentinel = Path(tempfile.mkdtemp()) / "host-sentinel.txt"; sentinel.write_text("host-sentinel\n"); digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    assert session.send({"op": "execute", "call": call("shell", "bash", {"command": "true"}, run_id="someone-else")})["error"]["code"] == "identity_mismatch"
    for extra in ({"container_id": "abc"}, {"volume": "svc"}, {"host_path": str(sentinel)}):
        assert session.send({"op": "execute", "call": call("read", "read", {"filePath": "x", **extra}, run_id=RUN_ID)})["error"]["code"] == "invalid_call"
    assert session.send({"op": "execute", "call": call("read", "read", {"filePath": "x"}, run_id=RUN_ID), "container": "other"})["error"]["code"] == "invalid_request"
    assert session.send({"op": "teleport"})["error"]["code"] == "unsupported_operation"
    for path in (str(sentinel), "/svc-private/index.txt", "/var/run/docker.sock"):
        assert session.run("read", "read", {"filePath": path})["status"] == "error"
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == digest
    save(artifact_dir, "guest-identity-receipts.json", {"sentinel_sha256": digest, "unchanged": True})


def test_guest_process_group_lifetime_and_cancellation(docker_env, artifact_dir, session):
    name = docker_env["guest"]
    bg = session.run("shell", "bash", {"command": "sleep 300 >/dev/null 2>&1 & sleep 301 </dev/null >/dev/null 2>&1 & echo launched"})
    assert bg["stdout"] == "launched\n"
    ps = _docker("exec", name, "sh", "-c", "ps -eo pid,pgid,stat,args | grep -E 'sleep (300|301)' | grep -v grep || true").stdout
    assert ps.strip() == "", f"background descendants survived their shell: {ps}"
    request = call("shell", "bash", {"command": "sleep 400 & sleep 401 & wait"}, run_id=RUN_ID, call_id="docker-cancel")
    handle = session.send({"op": "start", "call": request})["handle"]
    time.sleep(1.0)
    running = _docker("exec", name, "sh", "-c", "ps -eo pid,pgid,stat,args | grep -E 'sleep (400|401)' | grep -v grep").stdout
    assert str(handle["process_group_id"]) in running and running.count("sleep") >= 2
    assert session.send({"op": "cancel", "execution_key": key_of(request), "how": "terminate"})["cancelled"] is True
    events = session.send({"op": "collect", "execution_key": key_of(request)})["events"]
    assert events[-1]["result"]["status"] == "cancelled"
    after = _docker("exec", name, "sh", "-c", "ps -eo pid,pgid,stat,args | grep -E 'sleep (400|401)' | grep -v grep || true").stdout
    assert after.strip() == ""
    save(artifact_dir, "guest-process-receipts.json", {"background_after_shell_exit": ps, "running_before_cancel": running, "after_cancel": after, "handle": handle, "result": events[-1]["result"]})


# ------------------------------------------- worker-session loss and recovery
# Consolidated AC2 regression set. Real-guest cases run in the container built from this checkout's worker source.

def _wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate(): time.sleep(0.1)
    return predicate()


# /proc probes run with the guest's own python: the production image has no procps (ps, pkill).
_PROC_PROBE = ("import os,sys\nme=os.getpid(); pat,sig=sys.argv[1],sys.argv[2]\nout=[]\n"
               "for p in os.listdir('/proc'):\n"
               " if not p.isdigit() or int(p)==me: continue\n"
               " try: c=open('/proc/'+p+'/cmdline','rb').read().replace(b'\\0',b' ').decode().strip()\n"
               " except OSError: continue\n"
               " if pat in c:\n"
               "  out.append(p+' '+c)\n"
               "  if int(sig): os.kill(int(p), int(sig))\n"
               "print('\\n'.join(out))")


def _guest_procs(name, marker):
    """Live processes whose command line names this sleep marker (zombies have no command line and are not counted)."""
    return _docker("exec", name, "python", "-c", _PROC_PROBE, f"sleep {marker}", "0").stdout.strip()


def _signal_guest_session(name, sig):
    """Signal only the worker session process; its shells are left alone (no cleanup runs on SIGKILL)."""
    _docker("exec", "-u", "0", "-e", "PYTHONPATH=/opt/worker", name, "python", "-c", _PROC_PROBE, "sandbox.artifact_upload.worker.guest_session", str(sig))


def _settled(session, request, timeout=30.0):
    """Terminal result from a session that did not start the execution (status is served by the ledger)."""
    deadline = time.monotonic() + timeout
    while True:
        status = session.send({"op": "status", "execution_key": key_of(request)})
        if "error" in status: return status
        if status["status"]["state"] == "completed" or time.monotonic() >= deadline: return status["status"]["result"]
        time.sleep(0.1)


@pytest.mark.parametrize("loss", ["client_killed", "guest_sigkilled", "guest_sigterm", "guest_sighup", "stdin_eof"])
def test_worker_session_loss_leaves_no_uncontrolled_jobs_and_recovers_by_key(docker_env, artifact_dir, loss):
    name = docker_env["guest"]; marker = {"client_killed": "331.1", "guest_sigkilled": "332.1", "guest_sigterm": "333.1", "stdin_eof": "334.1", "guest_sighup": "335.1"}[loss]
    request = call("shell", "bash", {"command": f"sleep {marker} & setsid sleep {marker} & wait"}, run_id=RUN_ID, call_id=f"loss-{loss}")
    first = GuestSession(name, docker_env["log"])
    first.send({"op": "start", "call": request})
    assert _wait_for(lambda: len(_guest_procs(name, marker).splitlines()) >= 3), _guest_procs(name, marker)
    if loss == "client_killed": first.proc.kill()
    elif loss == "stdin_eof": first.proc.stdin.close()
    else: _signal_guest_session(name, 9 if loss == "guest_sigkilled" else (1 if loss == "guest_sighup" else 15))
    first.proc.wait(timeout=30)
    second = GuestSession(name, docker_env["log"])
    try:
        status = second.send({"op": "status", "execution_key": key_of(request)})
        assert "status" in status, status  # recovered by its exact key, not reported unknown
        assert second.send({"op": "start", "call": request})["error"]["code"] == "duplicate_execution"  # never redispatched
        second.send({"op": "cancel", "execution_key": key_of(request), "how": "terminate"})
        result = _settled(second, request)
        assert result["status"] != "ok" and result.get("error_code"), result
        assert _wait_for(lambda: _guest_procs(name, marker) == ""), _guest_procs(name, marker)
        save(artifact_dir, f"ac2-session-loss-{loss}.json", {"recovered_status": status, "result": result, "leftover": _guest_procs(name, marker)})
    finally:
        second.proc.stdin.close(); second.proc.wait(timeout=20)


@pytest.mark.parametrize("variant", ["delayed_spawn", "delayed_mutation"])
def test_descendants_created_after_worker_loss_are_cancelled_and_cannot_mutate(docker_env, artifact_dir, variant):
    """A child created after the worker session died is an owned job: cancel must reach it before any terminal result.

    The trailing `; true` keeps bash from exec-optimizing the spawning shell away, so the parent is a live owned
    process when its child appears; the worker is killed during the first sleep, before the child exists.
    """
    name = docker_env["guest"]; marker = {"delayed_spawn": "371.1", "delayed_mutation": "372.1"}[variant]
    mutated = f"/tmp/t016-{variant}.mutated"
    script = f"sleep 3; sleep {marker}; true" if variant == "delayed_spawn" else f'sleep 3; bash -c "sleep 5.5; date > {mutated}; sleep {marker}"; true'
    request = call("shell", "bash", {"command": f"setsid bash -c '{script}' & wait"}, run_id=RUN_ID, call_id=f"after-loss-{variant}")
    first = GuestSession(name, docker_env["log"])
    first.send({"op": "start", "call": request})
    assert _wait_for(lambda: _guest_procs(name, marker) != ""), "owned shell never started"
    _signal_guest_session(name, 9); first.proc.wait(timeout=20)
    # Wait for the descendant itself, not the parent (whose command line also names the marker).
    if variant == "delayed_spawn":
        appeared = lambda: any(line.endswith(f" sleep {marker}") for line in _guest_procs(name, marker).splitlines())
    else:
        # The inner shell's own command line starts with its script; the parent's line starts with `bash -c sleep 3;`.
        appeared = lambda: any(line.split(" ", 1)[1].startswith(f"bash -c sleep 5.5; date > {mutated}") for line in _guest_procs(name, marker).splitlines())
    assert _wait_for(appeared), "the post-loss descendant never appeared"
    second = GuestSession(name, docker_env["log"])
    try:
        status = second.send({"op": "status", "execution_key": key_of(request)})
        assert status["status"]["state"] == "running", status  # recovered live by key, not reported unknown
        assert second.send({"op": "cancel", "execution_key": key_of(request), "how": "terminate"})["cancelled"] is True
        result = _settled(second, request)
        assert result.get("error_code") == "outcome_unobserved" and result["status"] == "unknown", result
        # The terminal result is only published once nothing is left: check immediately, without waiting.
        survivors = _guest_procs(name, marker)
        assert survivors == "", f"owned descendant survived cancel: {survivors}"
        time.sleep(6.5)  # past the point where the surviving child would have written its file
        written = _docker("exec", name, "python", "-c", "import os,sys; print(os.path.exists(sys.argv[1]))", mutated, check=False).stdout.strip()
        assert written != "True", "a cancelled descendant mutated the workspace after cancel"
        save(artifact_dir, f"ac2-after-loss-{variant}.json", {"recovered_status": status, "result": result, "survivors": survivors, "mutated_file_written": written})
    finally:
        second.proc.stdin.close(); second.proc.wait(timeout=20)


@pytest.mark.parametrize("shape", ["ordinary", "nohup", "setsid"])
@pytest.mark.parametrize("mutation", [False, True])
def test_late_orphans_after_all_shell_ancestors_exit(docker_env, artifact_dir, shape, mutation):
    """B2: kill at 1s, let shell spawn at 3s and exit, then recover after 3.5s.

    Ordinary/nohup/setsid and delayed mutation share one ownership requirement.
    Shell-exit cleanup may already finish; otherwise fresh cancel must finish it.
    """
    name = docker_env["guest"]
    marker = {"ordinary": "391.9", "nohup": "392.9", "setsid": "393.9"}[shape]
    mutated = f"/tmp/r2-{shape}-{mutation}-{uuid.uuid4().hex}.mut"
    job = f"(sleep 2; date > {mutated}; sleep {marker})" if mutation else f"sleep {marker}"
    if shape == "ordinary": command = f"sleep 3; {job} &"
    elif shape == "nohup": command = f"sleep 3; nohup bash -c 'sleep 2; date > {mutated}; sleep {marker}' >/dev/null 2>&1 &" if mutation else f"sleep 3; nohup sleep {marker} >/dev/null 2>&1 &"
    else: command = f"setsid bash -c 'sleep 3; {job} & true' & wait"
    request = call("shell", "bash", {"command": command}, run_id=RUN_ID, call_id=f"b2-{shape}-{mutation}")
    first = GuestSession(name, docker_env["log"])
    first.send({"op": "start", "call": request})
    time.sleep(1)
    _signal_guest_session(name, 9); first.proc.wait(timeout=20)
    time.sleep(3.5)
    second = GuestSession(name, docker_env["log"])
    try:
        status = second.send({"op": "status", "execution_key": key_of(request)})
        cancelled = second.send({"op": "cancel", "execution_key": key_of(request)})
        result = _settled(second, request)
        assert result["status"] == "unknown" and result["exit_code"] is None and result["error_code"] == "outcome_unobserved", result
        assert second.send({"op": "start", "call": request})["error"]["code"] == "duplicate_execution"
        survivors = _guest_procs(name, marker)
        assert survivors == "", f"late orphan survived terminal recovery/cancel: {survivors}"
        if mutation:
            time.sleep(2.5)
            assert _docker("exec", name, "python", "-c", "import os,sys;assert not os.path.exists(sys.argv[1])", mutated).returncode == 0
        collected = second.send({"op": "collect", "execution_key": key_of(request)})["events"][-1]["result"]
        assert collected == result and collected["stdout"] == ""
        save(artifact_dir, f"ac2-b2-{shape}-{mutation}.json", {"command": command, "status": status, "cancel": cancelled, "result": result, "survivors": survivors, "mutation_absent": True})
    finally:
        second.proc.stdin.close(); second.proc.wait(timeout=20)


@pytest.mark.parametrize("shape", ["ordinary", "nohup", "setsid"])
def test_live_shell_exit_reaps_detached_jobs(docker_env, shape):
    marker = "395.9"
    command = {"ordinary": f"sleep {marker} &", "nohup": f"nohup sleep {marker} >/dev/null 2>&1 &", "setsid": f"setsid sleep {marker} &"}[shape]
    s = GuestSession(docker_env["guest"], docker_env["log"])
    try:
        request = call("shell", "bash", {"command": command}, run_id=RUN_ID, call_id="live-exit-" + shape)
        result = s.send({"op": "execute", "call": request})["result"]
        assert result["status"] == "ok" and result["exit_code"] == 0
        assert _guest_procs(docker_env["guest"], marker) == ""
    finally:
        s.proc.stdin.close(); s.proc.wait(timeout=20)


def test_recovery_isolated_by_run_and_unobserved_natural_exit(docker_env):
    name = docker_env["guest"]
    request = call("shell", "bash", {"command": "printf before; sleep 3; printf after"}, run_id=RUN_ID, call_id="unobserved-natural")
    a = GuestSession(name, docker_env["log"])
    a.send({"op": "start", "call": request})
    time.sleep(.5); _signal_guest_session(name, 9); a.proc.wait(timeout=20)
    foreign = GuestSession(name, docker_env["log"], run_id=RUN_ID + "-other")
    try:
        assert foreign.send({"op": "status", "execution_key": key_of(request)})["error"]["code"] == "unknown_execution"
        assert foreign.send({"op": "cancel", "execution_key": key_of(request)})["cancelled"] is False
    finally:
        foreign.proc.stdin.close(); foreign.proc.wait(timeout=20)
    b = GuestSession(name, docker_env["log"])
    try:
        result = _settled(b, request)
        assert result["status"] == "unknown" and result["exit_code"] is None and result["stdout"] == ""
        assert b.send({"op": "cancel", "execution_key": key_of(request)})["cancelled"] is False
        assert b.send({"op": "collect", "execution_key": key_of(request)})["events"][-1]["result"] == result
    finally:
        b.proc.stdin.close(); b.proc.wait(timeout=20)


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_cancel_and_timeout_reap_setsid_and_nohup_descendants(docker_env, mode):
    name = docker_env["guest"]; marker = {"cancel": "341.1", "timeout": "342.1"}[mode]
    args = {"command": f"sleep {marker} & setsid sleep {marker} & nohup sleep {marker} >/dev/null 2>&1 & wait"}
    if mode == "timeout": args["timeout"] = 1000
    request = call("shell", "bash", args, run_id=RUN_ID, call_id=f"reap-{mode}")
    session = GuestSession(name, docker_env["log"])
    try:
        session.send({"op": "start", "call": request})
        assert _wait_for(lambda: len(_guest_procs(name, marker).splitlines()) >= 3), _guest_procs(name, marker)
        if mode == "cancel": assert session.send({"op": "cancel", "execution_key": key_of(request), "how": "terminate"})["cancelled"] is True
        result = session.send({"op": "collect", "execution_key": key_of(request)})["events"][-1]["result"]
        assert result["status"] == ("cancelled" if mode == "cancel" else "error") and result["error_code"] == ("cancelled" if mode == "cancel" else "timeout"), result
        assert _wait_for(lambda: _guest_procs(name, marker) == ""), _guest_procs(name, marker)
    finally:
        session.proc.stdin.close(); session.proc.wait(timeout=20)


def _result_recorded(name, request):
    """True once the guest has durably recorded this execution's terminal result (checked inside the guest)."""
    fname = hashlib.sha256(json.dumps(list(key_of(request).values())).encode()).hexdigest() + ".result.json"
    code = ("import os,sys\nfrom sandbox.artifact_upload.worker.guest_session import state_dir_for\n"
            "print(os.path.exists(os.path.join(state_dir_for(sys.argv[1]), sys.argv[2])))")
    return _docker("exec", "-u", "0", "-e", "PYTHONPATH=/opt/worker", name, "python", "-c", code, RUN_ID, fname).stdout.strip() == "True"


def test_lost_start_reply_and_restart_dedup_serve_the_recorded_result(docker_env):
    name = docker_env["guest"]
    request = call("shell", "bash", {"command": "printf lost-ack"}, run_id=RUN_ID, call_id="lost-ack")
    first = GuestSession(name, docker_env["log"])
    first.proc.stdin.write(json.dumps({"op": "start", "call": request}) + "\n"); first.proc.stdin.flush()
    assert first.proc.stdout.readline(), "guest must acknowledge the start before the host loses the reply"
    assert _wait_for(lambda: _result_recorded(name, request)), "the shell's terminal result was never recorded"
    # The host never learns the handle; the worker session then dies without cleanup, so its result must come from the ledger.
    _signal_guest_session(name, 9); first.proc.wait(timeout=20)
    second = GuestSession(name, docker_env["log"])
    try:
        assert _settled(second, request)["stdout"] == "lost-ack"  # recovered by key, no second start
        assert second.send({"op": "start", "call": request})["error"]["code"] == "duplicate_execution"
        assert second.send({"op": "collect", "execution_key": key_of(request)})["events"][-1]["result"]["stdout"] == "lost-ack"
    finally:
        second.proc.stdin.close(); second.proc.wait(timeout=20)


def test_recovered_execution_is_adopted_by_key_and_cancelled_without_restart(tmp_path):
    state = tmp_path / "state"; root = tmp_path / "guest"; root.mkdir()
    first = GuestWorker(root, output_dir=tmp_path / "spool", kill_grace=0.5, state_dir=state)
    request = call("shell", "bash", {"command": "sleep 351.1 & wait"}, call_id="adopt-unit")
    handle = first.start(request)
    assert _wait_for(lambda: (rec := first.ledger.identity(GuestWorker._key(request))) and rec["members"]), "descendant was never recorded"
    # The first worker is abandoned without shutdown: its in-memory registry is lost, as after SIGKILL.
    second = GuestWorker(root, output_dir=tmp_path / "spool", kill_grace=0.5, state_dir=state)
    assert second.status(key_of(request))["state"] == "running"
    with pytest.raises(WorkerError) as exc: second.start(request)
    assert exc.value.code == "duplicate_execution"
    assert second.cancel(key_of(request), how="terminate") is True
    settled = second.status(key_of(request))
    assert settled["state"] == "completed" and settled["result"]["status"] != "ok", settled
    assert _wait_for(lambda: not group_members(int(handle["process_group_id"]))), "owned group survived recovered cancellation"


@pytest.mark.parametrize("boundary", ["legacy", "supervisor"])
def test_recorded_identity_with_a_reused_pid_is_never_signalled(tmp_path, boundary):
    victim = subprocess.Popen(["sleep", "352.1"])
    try:
        root = tmp_path / "guest"; root.mkdir()
        worker = GuestWorker(root, output_dir=tmp_path / "spool", kill_grace=0.5, state_dir=tmp_path / "state")
        key = {"run_id": "run-reuse", "session_id": "s", "call_id": "c-reuse", "argument_digest": "d" * 8}
        stale = "Mon Jan  1 00:00:00 1990"  # the recorded start time belongs to a different process that once had this pid
        record = {"key": list(GuestWorker._key(key)), "handle": {"process_id": str(victim.pid)},
                  "leader": [victim.pid, stale], "members": {str(victim.pid): stale}}
        if boundary == "supervisor": record["supervisor"] = [victim.pid, stale]
        worker.ledger.write_identity(GuestWorker._key(key), record)
        assert worker.status(key)["state"] == "completed"  # nothing owned is alive, so the exit is reported unobserved
        assert worker.cancel(key, how="kill") is False
        assert victim.poll() is None, "a process whose identity does not match the record was signalled"
    finally:
        victim.kill(); victim.wait()


def test_native_differential_in_guest_container(docker_env, native_results, artifact_dir):
    name = docker_env["guest"]
    for variant, cases in ALL_CASES.items():
        p = subprocess.run(["docker", "exec", "-i", name, "python", "-m", "sandbox.artifact_upload.worker.fixture_runner", "-", "-", f"/tmp/fixture-{variant}-{uuid.uuid4().hex[:6]}"],
                           input=json.dumps(cases), capture_output=True, text=True, timeout=900)
        assert p.returncode == 0, p.stderr[-3000:]
        data = json.loads(p.stdout)
        native = native_results["variants"][variant]
        problems = []
        for c, w in zip(cases, data["results"]):
            if c["compare"] == "worker-stricter":
                assert w["result"]["status"] == "error", c["id"]
                continue
            n = next(r for r in native["results"] if r["id"] == c["id"])
            problems += [f"{c['id']}: {x}" for x in fr.compare(c, n, w, (native["root"], native["out"]), (data["root"], data["out"]))]
        save(artifact_dir, f"differential-guest-{variant}.json", data)
        assert not problems, "\n".join(problems[:10])


@pytest.mark.skip(reason="TRIAGE: obsolete since 6b3a0fe; managed env parsing moved to the host adapter and the frozen worker no longer applies it. Superseded by test_managed_env_adapter.py")
def test_managed_env_files_reach_later_shell_calls_without_being_executed(guest, tmp_path, monkeypatch):
    from eval.artifact_upload import guest_operations as go
    worker, root, _ = guest
    home = tmp_path / "home"; managed = home / ".config/workspace-profile"; managed.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home)); monkeypatch.setenv("CI_RUNNER", "yes")
    sh = lambda c: worker.execute(call("shell", "bash", {"command": c}))["stdout"]
    show = 'printf "%s|%s|%s|%s" "${EDITOR_HINT-unset}" "${CI_RUNNER-unset}" "${TERM}" "${ARTIFACT_X-unset}"'
    assert sh(show).startswith("unset|yes|")
    (managed / "env-vars.sh").write_bytes(go._vars_render({"set": {"EDITOR_HINT": "vim"}, "unset": ["CI_RUNNER"]}))
    (managed / "env.sh").write_bytes(go._env_render({"TERM": "xterm"}))
    assert sh(show) == "vim|unset|xterm|unset"
    # Parsed, never executed; hand-edited denied names are ignored.
    (managed / "env-vars.sh").write_text("touch %s/pwned\nexport ARTIFACT_X='1'\nexport LD_PRELOAD='x'\nexport PATH='/nope'\nexport EDITOR_HINT='$(id)'\n" % root)
    assert sh(show) == "unset|yes|xterm|unset"
    assert not (root / "pwned").exists() and sh('echo "$PATH"').strip() != "/nope" and sh('echo "${LD_PRELOAD-unset}"').strip() == "unset"
    (managed / "env-vars.sh").write_text("export EDITOR_HINT='vim'\n")
    assert sh(show).startswith("vim|yes|")
