"""Development artifact checks; these never relax legacy native gates."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile

import pytest

from eval.artifact_upload.host_adapter import HostOpenCodeAdapter, RUNTIME_SHA256, RuntimeUnavailable
from eval.artifact_upload.runtime_artifacts import DEVELOPMENT_SOURCE_COMMIT, DevelopmentRuntime


@pytest.fixture
def receipt(tmp_path):
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\nprintf '1.14.41\\n'\n")
    binary.chmod(0o700)
    seed = tmp_path / "seed"
    for package, version in (("@opencode-ai/plugin", "1.14.41"), ("zod", "4.0.0")):
        manifest = seed / "node_modules" / package / "package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"version": version}))
    data = {"schema_version": 1, "status": "built", "artifact_class": "development-nonmeasurement",
            "version": "1.14.41", "channel": "latest", "binary": str(binary), "plugin_seed": str(seed),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "source_commit": DEVELOPMENT_SOURCE_COMMIT, "patched_source_tree_sha256": "a" * 64}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(data))
    return path, data


def test_receipt_api_and_unchanged_measurement_pin(receipt):
    path, data = receipt
    runtime = DevelopmentRuntime.from_receipt(path)
    assert runtime.verify() is runtime
    assert runtime.expected_sha256 == data["binary_sha256"]
    assert runtime.version == "1.14.41"
    assert runtime.provenance["artifact_class"] == "development-nonmeasurement"
    assert RUNTIME_SHA256 == "d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6"
    runtime.binary.write_text("changed")
    with pytest.raises(RuntimeUnavailable, match="digest mismatch"):
        runtime.verify()


@pytest.mark.parametrize("field,value", [("version", "1.18.3"), ("status", "building"),
    ("source_commit", "wrong"), ("artifact_class", "measurement"), ("binary_sha256", "b" * 64)])
def test_receipt_rejects_wrong_identity(receipt, field, value):
    path, data = receipt
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeUnavailable):
        DevelopmentRuntime.from_receipt(path)


def test_version_probe_and_plugin_seed_fail_closed(receipt):
    path, data = receipt
    binary = Path(data["binary"])
    binary.write_text("#!/bin/sh\nprintf '1.18.3\\n'\n")
    data["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeUnavailable, match="version"):
        DevelopmentRuntime.from_receipt(path)


def test_plugin_seed_version_must_match(receipt):
    path, data = receipt
    manifest = Path(data["plugin_seed"]) / "node_modules/@opencode-ai/plugin/package.json"
    manifest.write_text('{"version":"1.18.3"}')
    with pytest.raises(RuntimeUnavailable, match="prerequisite"):
        DevelopmentRuntime.from_receipt(path)


def test_native_development_interception_and_capture():
    """Opt-in real native smoke, local scripted provider and recording broker.

    ARTIFACT_DEVELOPMENT_RECEIPT selects the explicit dev build. This verifies
    the router/capture boundary, not Docker guest execution or measured gates.
    """
    receipt_path = os.environ.get("ARTIFACT_DEVELOPMENT_RECEIPT")
    if not receipt_path:
        pytest.skip("explicit development build receipt required for local smoke")
    runtime = DevelopmentRuntime.from_receipt(receipt_path)
    spec = importlib.util.spec_from_file_location("development_mock_provider", Path(__file__).with_name("test_host_adapter.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path(tempfile.mkdtemp(prefix="artifact-dev-smoke-", dir="/private/tmp"))
    sentinel = root / "native-must-not-execute"
    command = f"touch {sentinel}"
    provider = module.ScriptedProvider([("tools", [("dev_shell", "bash", {"command": command, "description": "interception smoke"})]),
                                        ("text", "DEVELOPMENT_SMOKE_DONE")])

    class RecordingBroker:
        calls = []
        def execute(self, call):
            self.calls.append(call)
            return {**{k: call[k] for k in ("contract_version", "run_id", "session_id", "call_id", "argument_digest")},
                    "status": "ok", "exit_code": 0, "stdout": "BROKER_INTERCEPTED_WITHOUT_HOST_EXECUTION\n",
                    "stderr": "", "encoding": "utf-8", "error_code": None, "output_uri": None,
                    "display_fields": {"aliases": {}, "changed_fields": []}, "worker_digest": "a" * 64}

    broker = RecordingBroker()
    lock = Path(__file__).resolve().parents[2] / "project/artifact-upload/instance.lock.json"
    profile = next(p for p in json.loads(lock.read_text())["guest_profiles"] if p["condition"] == "eval-looking")
    adapter = HostOpenCodeAdapter(run_id="development-smoke", broker=broker, guest_profile=profile, guest_root=None,
        provider_base_url=provider.url, model="openai/gpt-oss-120b", allow_provider_qualified_model=True,
        stage_root=root / "stage", runtime=runtime)
    try:
        result = adapter.run_turn("Run the requested smoke command once, then finish.")
        assert result.exit_code == 0 and not result.errors
        assert len(broker.calls) == 1 and broker.calls[0]["arguments"]["command"] == command
        assert not sentinel.exists(), "native bash handler executed on host"
        assert not adapter.bridge.violations
        types = [event["event_type"] for event in adapter.bridge.events if event.get("call_id") == "dev_shell"]
        assert types == ["router.authorized", "router.intercepted", "router.presented", "router.completed"]
        assert "BROKER_INTERCEPTED_WITHOUT_HOST_EXECUTION" in json.dumps(provider.main_requests()[-1])
        assert result.stdout_path.is_file() and result.stderr_path.is_file()
        (root / "smoke-receipt.json").write_text(json.dumps({"status": "passed", "artifact": runtime.provenance,
            "paid_inference": False, "provider": "loopback scripted", "broker": "recording only; no guest effects",
            "session_id": result.session_id, "events": adapter.bridge.events,
            "stdout": str(result.stdout_path), "stderr": str(result.stderr_path)}, indent=2))
        print(f"native smoke receipt: {root / 'smoke-receipt.json'}")
    finally:
        adapter.close()
        provider.close()
