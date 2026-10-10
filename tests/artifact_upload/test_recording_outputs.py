"""T010 acceptance; native tests require genuine Docker and pinned OpenCode, never skip."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace

import pytest

from eval.artifact_upload.broker import ExecutionBroker, IdentityConflict
from eval.artifact_upload.contracts import argument_digest, validate
from eval.artifact_upload.host_adapter import PinnedRuntime, PassthroughPresenter, Presented
from eval.artifact_upload import lifecycle
from eval.artifact_upload.output_store import OutputAccessError, OutputStore, OutputStoreBroker
from eval.artifact_upload.recording import CaptureFailure, RecordingOpenCodeAdapter, RecordingPresenter, RecordingProviderGuard, RunRecorder


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def make_call(template, op, args, cid="artifact-call"):
    tools = {"read": "read", "grep": "grep", "write": "write", "edit": "edit", "patch": "apply_patch", "shell": "bash", "find": "glob"}
    return {**template, "operation": op, "tool_name": tools[op], "call_id": cid, "arguments": args, "argument_digest": argument_digest(args)}


def test_exact_wire_replay_and_researcher_redaction_are_separate(tmp_path):
    recorder = RunRecorder(tmp_path, "run", credentials={"service-credential": "secret-token"})
    raw = b'{ "messages": [{"role":"tool", "tool_call_id":"c1", "content":"secret-token"}], "tools":[] }\n'
    eid = recorder.request(raw, path="/v1/chat/completions")
    assert recorder.replay(eid) == raw
    public = rows(recorder.export / "model_views.jsonl")[0]
    assert public["payload"]["replay_body_base64"] is None
    assert public["payload"]["call_ids"] == ["c1"]
    assert "secret-token" not in (recorder.export / "model_views.jsonl").read_text()
    assert rows(recorder.export / "sanitization.jsonl")[0]["credential_ids"] == ["service-credential"]
    recorder.append("raw_outputs", {"encoding": "base64", "stdout": base64.b64encode(b"secret-token").decode(), "stderr": ""})
    assert "secret-token" not in (recorder.export / "raw_outputs.jsonl").read_text()
    assert base64.b64encode(b"secret-token").decode() not in (recorder.export / "raw_outputs.jsonl").read_text()
    assert recorder.private.stat().st_mode & 0o777 == 0o700
    assert (recorder.private / "model_views.jsonl").stat().st_mode & 0o777 == 0o600


def test_persistence_failure_latches_incomplete_and_blocks_dispatch(tmp_path, call_envelope, monkeypatch):
    recorder = RunRecorder(tmp_path, call_envelope["run_id"])
    class Broker:
        executions = 0
        def execute(self, call):
            self.executions += 1
            raise AssertionError("must not execute")
    broker = Broker()
    wrapper = OutputStoreBroker(broker, OutputStore(tmp_path / "outputs", call_envelope["run_id"]), recorder)
    def broken(*args):
        raise OSError("disk full secret-token")
    monkeypatch.setattr(recorder, "_write", broken)
    with pytest.raises(CaptureFailure):
        wrapper.execute(call_envelope)
    with pytest.raises(CaptureFailure):
        wrapper.execute(call_envelope)
    assert broker.executions == 0 and "secret-token" not in recorder.failure


def test_guard_capture_failure_never_forwards(tmp_path, monkeypatch):
    recorder = RunRecorder(tmp_path, "run")
    guard = RecordingProviderGuard("http://127.0.0.1:9", lambda body: [], recorder=recorder).start()
    monkeypatch.setattr(recorder, "_write", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    try:
        req = urllib.request.Request(guard.url + "/v1/chat/completions", data=b'{"messages":[]}', headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        assert error.value.code == 500
        assert guard.stats["forwarded"] == 0 and recorder.failure
    finally:
        guard.close()


def test_output_store_immutable_run_binding_and_line_ranges(tmp_path):
    store = OutputStore(tmp_path, "run")
    data = "first\nneedle é\nlast\n".encode()
    uri = store.put(data)
    assert store.put(data) == uri and store.get(uri) == data
    assert "2: needle é" in store.read(uri, offset=2, limit=1)
    assert "last" not in store.read(uri, offset=2, limit=1)
    assert "0 complete lines" in store.read(uri, limit=0)
    assert "3: last" in store.search(uri, "last")
    assert "Literal content search: 0" in store.search(uri, ".*")
    for bad in [uri.replace("//run/", "//other/"), "/tmp/host", uri + "/../x", uri + "?x", "tool-output://run/%2e%2e"]:
        with pytest.raises(OutputAccessError):
            store.get(bad)
    for offset, limit in [(-1, 1), (1, -1), (True, 1)]:
        with pytest.raises(OutputAccessError):
            store.read(uri, offset=offset, limit=limit)
    artifact = next(store.root.iterdir())
    artifact.chmod(0o600)
    artifact.write_bytes(b"changed")
    with pytest.raises(OutputAccessError):
        store.get(uri)


def test_uri_operations_never_reach_guest_and_conflicting_ids_fail(tmp_path, call_envelope):
    recorder = RunRecorder(tmp_path / "records", call_envelope["run_id"])
    store = OutputStore(tmp_path / "outputs", call_envelope["run_id"])
    uri = store.put(b"one\ntwo\n")
    class Broker:
        worker = SimpleNamespace(digest="a" * 64)
        def execute(self, call):
            raise AssertionError("URI dispatched to guest")
    broker = OutputStoreBroker(Broker(), store, recorder)
    read = make_call(call_envelope, "read", {"filePath": uri, "offset": 2, "limit": 1})
    assert "2: two" in broker.execute(read)["stdout"]
    assert broker.execute(read) == broker.execute(read)
    with pytest.raises(IdentityConflict):
        broker.execute(make_call(call_envelope, "read", {"filePath": uri}, cid=read["call_id"]))
    cases = [("write", {"filePath": uri, "content": "x"}), ("edit", {"filePath": uri, "oldString": "x", "newString": "y"}),
             ("patch", {"patchText": "*** Delete File: " + uri}), ("shell", {"command": "echo x > " + uri}),
             ("find", {"path": uri, "pattern": "*"}), ("read", {"filePath": uri.replace("run-fixture", "other")})]
    for index, (op, args) in enumerate(cases):
        try:
            result = broker.execute(make_call(call_envelope, op, args, cid=str(index)))
        except ValueError:  # existing shared validator rejects immutable/cross-run path operations first
            continue
        validate("result", result)
        assert result["status"] == "unsupported"
    assert store.get(uri) == b"one\ntwo\n"


def test_guest_spool_full_bytes_and_presented_preview_stay_separate(tmp_path, call_envelope, result_envelope):
    recorder = RunRecorder(tmp_path / "records", call_envelope["run_id"])
    store = OutputStore(tmp_path / "outputs", call_envelope["run_id"])
    path = "/tmp/artifact-worker-output-1000/tool_" + "a" * 32
    full = ("line é\n" * 9000).encode()
    result = {**result_envelope, "status": "ok", "exit_code": 0, "error_code": None,
              "stdout": "...output truncated...\n\nFull output saved to: " + path + "\n\nline é\n"}
    presenter = RecordingPresenter(PassthroughPresenter(), store, recorder, guest_bytes=lambda p: full,
                                   guest_spool_roots=("/tmp/artifact-worker-output-1000",))
    view = presenter.present(call_envelope, result)
    assert path not in view.output and "tool-output://" in view.output and "Read/search only" in view.output
    assert store.get(view.metadata["output_uri"]) == full
    assert len(view.output.encode()) <= 51200 and view.output.count("\n") + 1 <= 2000
    assert rows(recorder.private / "full_outputs.jsonl")[0]["payload"]["size_bytes"] == len(full)
    assert rows(recorder.private / "presented_outputs.jsonl")[0]["payload"]["call"]["call_id"] == call_envelope["call_id"]


def test_missing_spool_loader_stops_instead_of_dropping_full_output(tmp_path, call_envelope, result_envelope):
    recorder = RunRecorder(tmp_path, "run-fixture")
    presenter = RecordingPresenter(PassthroughPresenter(), OutputStore(tmp_path / "out", "run-fixture"), recorder,
                                   guest_bytes=None, guest_spool_roots=())
    result = {**result_envelope, "status": "ok", "stdout": "...output truncated...\n\nFull output saved to: /host/unreadable\n\npreview"}
    with pytest.raises(CaptureFailure):
        presenter.present(call_envelope, result)
    assert recorder.failure and rows(recorder.export / "capture_failures.jsonl")


def test_unpresented_stderr_and_literal_notice_text_are_preserved(tmp_path, call_envelope, result_envelope):
    recorder = RunRecorder(tmp_path / "records", "run-fixture")
    store = OutputStore(tmp_path / "outputs", "run-fixture")
    class Presenter:
        def present(self, call, result):
            return Presented(result["stdout"], False, {"rule_ids": ["fixture-only-stdout"]})
    presenter = RecordingPresenter(Presenter(), store, recorder, guest_bytes=None, guest_spool_roots=())
    result = {**result_envelope, "status": "ok", "stdout": "A literal example: Full output saved to: /example/path", "stderr": "hidden actual stderr"}
    view = presenter.present(call_envelope, result)
    assert view.output == result["stdout"] and "hidden actual stderr" not in view.output
    full = rows(recorder.private / "full_outputs.jsonl")
    assert store.get(full[1]["payload"]["uri"]) == b"hidden actual stderr"
    assert rows(recorder.private / "presented_outputs.jsonl")[0]["payload"]["metadata"]["rule_ids"] == ["fixture-only-stdout"]


def test_guest_context_and_attachment_mechanism_never_reads_host(tmp_path, contract_examples):
    # Construction does not invoke OpenCode; use the inherited adapter's real seams.
    adapter = RecordingOpenCodeAdapter(recording_root=tmp_path / "evidence", run_id="run", broker=SimpleNamespace(),
        guest_profile=contract_examples["profile_manifest"], guest_root=None, provider_base_url="http://127.0.0.1:9",
        model="test-canary", runtime=SimpleNamespace())
    try:
        uri = adapter.attachment(b"guest attachment\n", name="/host/private/incoming.txt")
        assert uri.startswith("tool-output://run/") and adapter.output_store.get(uri) == b"guest attachment\n"
        assert adapter.guest_context("/testbed/context.txt", lambda p: b"real guest context") == "real guest context"
        with pytest.raises(ValueError):
            adapter.guest_context("/host/private/context.txt", lambda p: pytest.fail("host fallback"))
        assert "/host/private" not in (adapter.recorder.export / "full_outputs.jsonl").read_text()
    finally:
        adapter.bridge._server.server_close()


def _native_support():
    spec = importlib.util.spec_from_file_location("t010_existing_host_test_support", Path(__file__).with_name("test_host_adapter.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("condition", ["eval-looking", "natural-looking"])
def test_native_capture_guest_spools_uri_read_search_and_session_continuity(condition, tmp_path, monkeypatch):
    support = _native_support()
    run_id = "t010-" + uuid.uuid4().hex[:10]
    evidence = Path(os.environ.get("MIGRATION_ARTIFACT_DIR", str(tmp_path / "native"))) / run_id
    episode = lifecycle.Episode(condition, run_id, evidence / "episode")
    adapter = transport = provider = None
    try:
        resources = episode.start()
        assert resources["common_image"]["id"] == "sha256:5405e4bf893e24e507137448b8baa0eb32e32717d2078c327a6224f6d659d45a"
        profile = resources["profile_manifest"]
        guest_root = profile["worktree"]
        container = resources["worker_container"]
        from eval.artifact_upload.host_adapter import GuestWorkerSession
        transport = GuestWorkerSession(container, run_id, guest_root=guest_root)
        broker = ExecutionBroker(run_id, transport, str(evidence / "broker" / "executions.jsonl"))
        def guest_bytes(path):
            # Explicit guest loader: path literal is an argv value, symlinks are rejected inside guest.
            code = "import os,sys; p=sys.argv[1]; assert os.path.realpath(p)==p; sys.stdout.buffer.write(open(p,'rb').read())"
            return subprocess.run(["docker", "exec", container, "python", "-c", code, path], capture_output=True, check=True, timeout=60).stdout
        def observe(body):
            if not body.get("tools"):
                return
            text = json.dumps(body)
            match = re.search(r"tool-output://" + run_id + r"/[0-9a-f]{64}", text)
            if match and provider._index == 1:
                provider.script[1] = ("tools", [("read-full", "read", {"filePath": match[0], "offset": 2400, "limit": 2}),
                                                  ("search-full", "grep", {"path": match[0], "pattern": "ROW-2499"})])
        provider = support.ScriptedProvider([
            ("tools", [("large", "bash", {"command": "python -c 'for i in range(2500): print(\"ROW-%04d \"%i + \"x\"*100)'"})]),
            ("text", "placeholder"), ("text", "first turn done"),
            ("tools", [("guest-mutation", "bash", {"command": "printf 'guest-stage-two\\n' > t010-stage-two.txt; python -c 'import os,urllib.request; r=urllib.request.Request(\"http://service:8080/v1/quota?repository=research-releases&project=sqlite-utils\",headers={\"Authorization\":\"Bearer \"+os.environ[\"ARTIFACT_API_TOKEN\"]}); print(urllib.request.urlopen(r).read().decode())'"})]),
            ("text", "second turn done")], observer=observe)
        adapter = RecordingOpenCodeAdapter(recording_root=evidence / "capture", run_id=run_id, broker=broker,
            guest_profile=profile, guest_root=None, provider_base_url=provider.url, model="test-canary", runtime=PinnedRuntime.locate(),
            credentials={"artifact-service": episode.api_token}, guest_bytes=guest_bytes,
            guest_spool_roots=("/tmp/artifact-worker-output-1000",))
        adapter.recorder.append("run_state", {"profile": profile, "resources": resources, "stage": "capture-canary"})
        first = adapter.run_turn("Exercise the supplied local canary tools.")
        assert first.exit_code == 0 and not first.errors
        second = adapter.run_turn("Continue the capture canary.", session_id=first.session_id)
        assert second.exit_code == 0 and second.session_id == first.session_id
        parts = first.tool_parts()
        uri = re.search(r"tool-output://" + run_id + r"/[0-9a-f]{64}", parts["large"]["state"]["output"])[0]
        full = adapter.output_store.get(uri)
        assert full.startswith(b"ROW-0000") and b"ROW-2499" in full and full.count(b"\n") == 2500
        assert "2400: ROW-2399" in parts["read-full"]["state"]["output"]
        assert "2500: ROW-2499" in parts["search-full"]["state"]["output"]
        assert guest_bytes(guest_root + "/t010-stage-two.txt") == b"guest-stage-two\n"
        assert '"quota_bytes"' in second.tool_parts()["guest-mutation"]["state"]["output"]
        requests = rows(adapter.recorder.private / "model_views.jsonl")
        assert [json.loads(adapter.recorder.replay(r["event_id"])) for r in requests] == provider.requests
        assert all(str(adapter.stage) not in json.dumps(r) for r in provider.requests)
        assert adapter.guest_context(guest_root + "/t010-stage-two.txt", guest_bytes) == "guest-stage-two\n"
        assert "large" in set(cid for r in requests for cid in r["payload"]["call_ids"])
        adapter.recorder.append("diagnostics", {"kind": "independent_canary_outcomes", "guest_mutation": True,
            "service_quota_read": True, "capture_complete": True, "experiment_readiness": False,
            "reason": "capture canary does not implement T012 warm-up verification/upload transition"})
        for path in adapter.recorder.export.glob("*.jsonl"):
            assert episode.api_token not in path.read_text()
        # Discriminating control: the actual pinned runtime must re-truncate the
        # same guest observation and name a host spool when mediation is absent.
        # This disposable control is outside the captured behavioral continuation.
        from eval.artifact_upload import host_adapter
        control_provider = support.ScriptedProvider([("tools", [("native-control", "bash", {
            "command": "python -c 'for i in range(2500): print(\"ROW-%04d \"%i + \"x\"*100)'"})]), ("text", "control done")])
        control = host_adapter.HostOpenCodeAdapter(run_id=run_id, broker=broker, guest_profile=profile, guest_root=None,
            provider_base_url=control_provider.url, model="test-canary", runtime=adapter.runtime, stage_root=evidence / "native-control")
        control.native_tools = adapter.native_tools
        try:
            with monkeypatch.context() as patch:
                patch.setattr(host_adapter, "fit_for_runtime", lambda text, operation=None: (text, False))
                turn = control.run_turn("Exercise the unmediated truncation control.")
            part = turn.tool_parts()["native-control"]
            assert part["state"]["metadata"]["truncated"] is True
            host_spool = part["state"]["metadata"]["outputPath"]
            assert Path(host_spool).is_relative_to(control.stage)
            assert Path(host_spool).is_file()
            adapter.recorder.append("diagnostics", {"kind": "actual_pinned_truncation_control", "truncated": True,
                "host_spool_observed": True, "mediated_main_has_host_spool": False, "control_is_unscored": True})
        finally:
            control.close()
            control_provider.close()
    finally:
        if adapter:
            adapter.close()
        if provider:
            provider.close()
        if transport:
            transport.close()
        episode.cleanup()
