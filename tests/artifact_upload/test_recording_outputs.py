"""T010 acceptance; native tests require genuine Docker and pinned OpenCode, never skip."""
from __future__ import annotations

import base64
import importlib.util
import hashlib
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
              "stdout": "...output truncated...\n\nFull output saved to: " + path + "\n\nline é\n", "stderr": ""}
    presenter = RecordingPresenter(PassthroughPresenter(), store, recorder, guest_bytes=lambda p: full,
                                   guest_spool_roots=("/tmp/artifact-worker-output-1000",))
    view = presenter.present(call_envelope, result)
    assert path not in view.output and "tool-output://" in view.output and "Read/search only" in view.output
    assert store.get(view.metadata["output_uri"]) == full
    assert len(view.output.encode()) <= 51200 and view.output.count("\n") + 1 <= 2000
    assert rows(recorder.private / "full_outputs.jsonl")[0]["payload"]["size_bytes"] == len(full)
    assert rows(recorder.private / "presented_outputs.jsonl")[0]["payload"]["call"]["call_id"] == call_envelope["call_id"]


@pytest.mark.parametrize("encoding", ["utf-8", "base64"])
def test_transformed_full_spool_read_search_and_replay(tmp_path, call_envelope, result_envelope, encoding):
    recorder = RunRecorder(tmp_path / "records", call_envelope["run_id"])
    store = OutputStore(tmp_path / "outputs", call_envelope["run_id"])
    path = "/tmp/artifact-worker-output-1000/tool_" + "a" * 32
    raw = b"owner_name=Canonical Example\n" * 2500
    class ShellOwnerPresenter:
        calls = []
        def present(self, call, result):
            self.calls.append((call, result))
            text = PassthroughPresenter._text(result, "stdout")
            if call["operation"] == "shell":
                text = text.replace("owner_name=Canonical Example", "owner_name=User 001")
            return Presented(text, False, {"rule_ids": ["shell-owner-display"]})
    approved = ShellOwnerPresenter()
    presenter = RecordingPresenter(approved, store, recorder, guest_bytes=lambda p: raw,
                                   guest_spool_roots=("/tmp/artifact-worker-output-1000",))
    preview = "...output truncated...\n\nFull output saved to: " + path + "\n\nowner_name=Canonical Example\n"
    result = {**result_envelope, "status": "ok", "exit_code": 0, "error_code": None, "encoding": encoding,
              "stdout": base64.b64encode(preview.encode()).decode() if encoding == "base64" else preview, "stderr": ""}
    view = presenter.present(call_envelope, result)
    uri = view.metadata["output_uri"]
    expected = b"owner_name=User 001\n" * 2500
    assert store.get(uri) == expected and "Canonical Example" not in view.output
    assert "owner_name=User 001" in view.output and path not in view.output
    assert approved.calls[0][0] == call_envelope
    assert PassthroughPresenter._text(approved.calls[0][1], "stdout").encode() == raw
    assert result["stdout"] != approved.calls[0][1]["stdout"]  # original envelope remains intact
    records = rows(recorder.private / "full_outputs.jsonl")
    raw_record, full_record = records[0]["payload"], records[-1]["payload"]
    assert presenter.raw_store.get(raw_record["uri"]) == raw and not raw_record["target_readable"]
    with pytest.raises(OutputAccessError):
        store.get(raw_record["uri"])
    assert full_record["uri"] == uri and full_record["sha256"] == uri.rsplit("/", 1)[1]
    assert full_record["target_readable"] and full_record["metadata"]["rule_ids"] == ["shell-owner-display"]
    assert rows(recorder.private / "presented_outputs.jsonl")[0]["payload"]["metadata"] == view.metadata
    class NoGuest:
        worker = SimpleNamespace(digest="a" * 64)
        def execute(self, call):
            raise AssertionError("URI reached guest")
    broker = OutputStoreBroker(NoGuest(), store, recorder)
    read = make_call(call_envelope, "read", {"filePath": uri, "offset": 2400, "limit": 1}, "read")
    read_view = presenter.present(read, broker.execute(read))
    assert "2400: owner_name=User 001" in read_view.output and "Canonical Example" not in read_view.output
    for pattern, expected_text in [("Canonical Example", "0 matching lines"), ("User 001", "2500 matching lines")]:
        search = make_call(call_envelope, "grep", {"path": uri, "pattern": pattern}, pattern)
        search_view = presenter.present(search, broker.execute(search))
        assert expected_text in search_view.output and "Canonical Example" not in search_view.output
    wire = json.dumps({"messages": [{"role": "tool", "tool_call_id": "read", "content": read_view.output}]}).encode()
    event = recorder.request(wire, path="/v1/chat/completions")
    assert recorder.replay(event) == wire
    assert not recorder.failure


@pytest.mark.parametrize("mode", ["raises", "unresolved"])
def test_full_stream_presentation_unavailable_stops_with_private_raw(tmp_path, call_envelope, result_envelope, mode):
    recorder = RunRecorder(tmp_path / "records", call_envelope["run_id"])
    store = OutputStore(tmp_path / "outputs", call_envelope["run_id"])
    path = "/tmp/artifact-worker-output-1000/tool_" + "a" * 32
    notice = "...output truncated...\n\nFull output saved to: " + path + "\n\npreview"
    class BoundedPresenter:
        def present(self, call, result):
            if mode == "raises":
                raise ValueError("full stream outside approved parser coverage")
            return Presented(notice, False, {})
    presenter = RecordingPresenter(BoundedPresenter(), store, recorder, guest_bytes=lambda p: b"private full stream\n",
                                   guest_spool_roots=("/tmp/artifact-worker-output-1000",))
    with pytest.raises(CaptureFailure, match="full-stream presentation"):
        presenter.present(call_envelope, {**result_envelope, "stdout": notice})
    assert recorder.failure and not list(store.root.iterdir())
    assert not (recorder.private / "presented_outputs.jsonl").exists()
    raw = rows(recorder.private / "full_outputs.jsonl")[0]["payload"]
    assert presenter.raw_store.get(raw["uri"]) == b"private full stream\n"
    with pytest.raises(CaptureFailure):
        presenter.present(call_envelope, result_envelope)


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
    assert presenter.raw_store.get(full[1]["payload"]["uri"]) == b"hidden actual stderr"
    with pytest.raises(OutputAccessError):
        store.get(full[1]["payload"]["uri"])
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
                                                  ("search-full", "grep", {"path": match[0], "pattern": "DISPLAY-2499"}),
                                                  ("search-raw", "grep", {"path": match[0], "pattern": "ROW-2499"})])
        provider = support.ScriptedProvider([
            ("tools", [("large", "bash", {"command": "python -c 'for i in range(2500): print(\"ROW-%04d \"%i + \"x\"*100)'"})]),
            ("text", "placeholder"), ("text", "first turn done"),
            ("tools", [("guest-mutation", "bash", {"command": "printf 'guest-stage-two\\n' > t010-stage-two.txt; python -c 'import os,urllib.request; r=urllib.request.Request(\"http://service:8080/v1/quota?repository=research-releases&project=sqlite-utils\",headers={\"Authorization\":\"Bearer \"+os.environ[\"ARTIFACT_API_TOKEN\"]}); print(urllib.request.urlopen(r).read().decode())'"})]),
            ("text", "second turn done")], observer=observe)
        class ShellDisplayPresenter:
            def present(self, call, result):
                view = PassthroughPresenter().present(call, result)
                text = view.output.replace("ROW-", "DISPLAY-") if call["operation"] == "shell" else view.output
                return Presented(text, view.is_error, {**view.metadata, "rule_ids": ["shell-row-display"]})
        adapter = RecordingOpenCodeAdapter(recording_root=evidence / "capture", run_id=run_id, broker=broker,
            presenter=ShellDisplayPresenter(),
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
        assert full.startswith(b"DISPLAY-0000") and b"DISPLAY-2499" in full and full.count(b"\n") == 2500
        assert b"ROW-" not in full
        raw_record = next(r["payload"] for r in rows(adapter.recorder.private / "full_outputs.jsonl")
                          if r["payload"].get("source") == "guest_spool")
        assert adapter.bridge.presenter.raw_store.get(raw_record["uri"]).startswith(b"ROW-0000")
        with pytest.raises(OutputAccessError):
            adapter.output_store.get(raw_record["uri"])
        assert "2400: DISPLAY-2399" in parts["read-full"]["state"]["output"]
        assert "2500: DISPLAY-2499" in parts["search-full"]["state"]["output"]
        assert "0 matching lines" in parts["search-raw"]["state"]["output"]
        assert "ROW-" not in parts["read-full"]["state"]["output"]
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


def test_service_receipt_health_and_decoded_base64_redaction_use_existing_stop_hook(tmp_path):
    recorder = RunRecorder(tmp_path, 'receipt-run', credentials={'credential': 'secret-token'})
    recorder.service_health = lambda: {'healthy': True, 'sealed': False}
    recorder.check()
    encoded = base64.b64encode(b'HTTP/1.1 400\r\n\r\nsecret-token').decode()
    recorder.append('service_events', {'bytes_b64': encoded, 'body_base64': encoded})
    export = (recorder.export / 'service_events.jsonl').read_text()
    assert 'secret-token' not in export and encoded not in export
    assert '[credential:credential]' in export
    assert rows(recorder.export / 'sanitization.jsonl')[0]['credential_ids'] == ['credential']
    recorder.service_health = lambda: {'healthy': False, 'sealed': False}
    with pytest.raises(CaptureFailure, match='service receipt observer'):
        recorder.check()
    with pytest.raises(CaptureFailure):
        recorder.append('service_events', {'must_not_forward': True})


def test_business_audit_cannot_substitute_for_service_receipt_source(tmp_path):
    from sandbox.artifact_upload.receipts import SCHEMA, validate_bundle
    raw = b'{"audit_schema":"artifact-audit.v1","event_type":"service.request"}\n'
    bundle = {'schema': SCHEMA, 'source_bytes_b64': base64.b64encode(raw).decode(),
              'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_size_bytes': len(raw)}
    recorder = RunRecorder(tmp_path, 'fake-source')
    with pytest.raises((ValueError, KeyError)):
        validate_bundle(bundle)
    with pytest.raises(CaptureFailure, match='source incomplete'):
        recorder.import_service_receipts(bundle)
    assert next(recorder.private.glob('service-receipts-*.jsonl')).read_bytes() == raw


@pytest.mark.parametrize('fault', [None, 'capture_errors', 'missing_api', 'hash', 'receipt_path', 'symlink'], ids=['complete', 'memory-failure', 'dependency-gate', 'tampered-stream', 'unowned-source', 'symlink-source'])
def test_native_timeout_hook_verifies_invocation_sources_redacts_and_rethrows_same_timeout(tmp_path, monkeypatch, fault):
    from eval.artifact_upload.host_adapter import HostOpenCodeAdapter
    adapter = RecordingOpenCodeAdapter.__new__(RecordingOpenCodeAdapter)
    adapter.recorder = RunRecorder(tmp_path / 'capture', 'timeout-hook', credentials={'provider': 'secret-token'})
    adapter.stage = adapter.recorder.private / 'runtime-stage'
    adapter.stage.mkdir(mode=0o700)
    prefix = adapter.stage / 'turn-1'
    out, err = b'\xff secret-token\r\n' + str(adapter.stage).encode(), b'incomplete utf8 \xe2\x82'
    paths = {name: Path(str(prefix) + '.' + name + '.log') for name in ('stdout', 'stderr')}
    for name, data in (('stdout', out), ('stderr', err)):
        paths[name].write_bytes(data); paths[name].chmod(0o600)
    receipt = {'kind': 'native_process_timeout', 'timeout_seconds': 3, 'pid': 123,
               'kill_signal': 'SIGKILL', 'exit_code': -9, 'cancellations': [{'state': 'unknown'}],
               'capture_complete': True, 'capture_errors': [], 'encoding': 'bytes',
               'streams': {name: {'path': str(paths[name]), 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                           for name, data in (('stdout', out), ('stderr', err))}}
    receipt_path = Path(str(prefix) + '.timeout.json')
    receipt_path.write_text(json.dumps(receipt)); receipt_path.chmod(0o600)
    original = subprocess.TimeoutExpired(['native', 'run'], 3, output=out, stderr=err)
    original.capture_receipt_path = receipt_path
    original.capture_errors = []
    if fault == 'capture_errors': original.capture_errors = ['receipt_persistence:OSError']
    if fault == 'missing_api': del original.capture_errors
    if fault == 'hash': paths['stdout'].write_bytes(b'tampered')
    if fault == 'receipt_path': original.capture_receipt_path = tmp_path / 'unowned.json'
    if fault == 'symlink':
        paths['stdout'].unlink(); paths['stdout'].symlink_to(paths['stderr'])
    calls = []
    def producer(*args, **kwargs):
        calls.append((args, kwargs))
        raise original
    monkeypatch.setattr(HostOpenCodeAdapter, '_run_process', producer)
    if fault:
        with pytest.raises(CaptureFailure, match='native timeout capture incomplete'):
            adapter._run_process(adapter.stage, adapter.stage, adapter.stage, ['prompt'], prefix, timeout=3)
        with pytest.raises(CaptureFailure): adapter.recorder.check()
        assert not (adapter.recorder.export / 'diagnostics.jsonl').exists()
    else:
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            adapter._run_process(adapter.stage, adapter.stage, adapter.stage, ['prompt'], prefix, timeout=3)
        assert captured.value is original
        private = rows(adapter.recorder.private / 'diagnostics.jsonl')[0]['payload']
        assert base64.b64decode(private['stdout']) == out and base64.b64decode(private['stderr']) == err
        assert private['cancellations'] == [{'state': 'unknown'}]
        exported = (adapter.recorder.export / 'diagnostics.jsonl').read_text()
        assert 'secret-token' not in exported and str(adapter.stage) not in exported
        assert '[credential:provider]' in exported and '[private host runtime]' in exported
        assert rows(adapter.recorder.export / 'sanitization.jsonl')[0]['credential_ids'] == ['provider']
    assert len(calls) == 1


@pytest.mark.parametrize('fault', [None, 'callback', 'capture', 'health'])
def test_post_capture_checkpoint_hook_binds_exact_wire_and_failures_stop(tmp_path, monkeypatch, fault):
    from urllib.parse import urlsplit
    from eval.artifact_upload.host_adapter import ProviderGuard
    recorder = RunRecorder(tmp_path / 'capture', 'hook', credentials={'model-provider': 'secret-token'})
    guard = RecordingProviderGuard.__new__(RecordingProviderGuard)
    guard.recorder = recorder
    guard.upstream = urlsplit('https://user:secret-token@provider.example/api')
    guard.route_source = {'component': 'fixture', 'credential_id': 'model-provider'}
    forwarded, callbacks, refusals = [], [], []
    raw = b'{ "model":"exact-main", "messages":[], "max_tokens":8 }\n'
    def callback(captured):
        assert recorder.replay(captured['model_view_event_id']) == raw
        assert captured['wire_sha256'] == hashlib.sha256(raw).hexdigest()
        assert set(captured) == {'model_view_event_id', 'wire_sha256', 'route_provenance'}
        assert captured['route_provenance']['base_url'] == 'https://provider.example/api'
        assert captured['route_provenance']['model_revision_status'] == 'unknown'
        assert captured['route_provenance']['model_revision'] is None
        assert not forwarded
        callbacks.append(captured)
        if fault == 'callback':
            raise RuntimeError('secret-token callback failed')
    guard.after_capture = callback
    guard._refuse = lambda handler, code, reason: refusals.append(code)
    monkeypatch.setattr(ProviderGuard, '_forward', lambda self, handler, body: forwarded.append(body))
    if fault == 'capture':
        monkeypatch.setattr(recorder, '_write', lambda *args: (_ for _ in ()).throw(OSError()))
    if fault == 'health':
        recorder.service_health = lambda: {'healthy': False, 'sealed': False}
    guard._forward(SimpleNamespace(path='/v1/chat/completions'), raw)
    if fault:
        assert forwarded == [] and refusals == [500] and recorder.failure
        guard._forward(SimpleNamespace(path='/v1/chat/completions'), raw)
        assert not forwarded and len(callbacks) == (1 if fault == 'callback' else 0)
    else:
        assert forwarded == [raw] and len(callbacks) == 1
        assert len(rows(recorder.private / 'model_views.jsonl')) == 1
        assert 'secret-token' not in (recorder.export / 'proxy_events.jsonl').read_text()
