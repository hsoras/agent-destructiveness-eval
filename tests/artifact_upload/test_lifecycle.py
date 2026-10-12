"""Focused unit regressions for artifact-upload episode isolation and failure handling."""
import json
import subprocess

import pytest

from eval.artifact_upload import lifecycle


def test_profiles_match_locked_identity_and_use_private_unique_compose_projects(tmp_path):
    eval_episode = lifecycle.Episode("eval-looking", "eval-run", tmp_path)
    natural_episode = lifecycle.Episode("natural-looking", "natural-run", tmp_path)
    assert eval_episode.project != natural_episode.project
    lock = json.loads(lifecycle.INSTANCE.read_text())
    profiles = {p["condition"]: p for p in lock["guest_profiles"]}
    assert (profiles["eval-looking"]["account"], profiles["eval-looking"]["uid"], profiles["eval-looking"]["worktree"]) == ("runner", 1000, "/testbed")
    assert (profiles["natural-looking"]["account"], profiles["natural-looking"]["uid"], profiles["natural-looking"]["worktree"]) == ("alex", 1000, "/home/alex/work/sqlite-utils")
    assert profiles["eval-looking"]["packages_digest"] == profiles["natural-looking"]["packages_digest"]


def test_missing_prepared_source_is_infrastructure_failure_and_saved(tmp_path, monkeypatch):
    episode = lifecycle.Episode("eval-looking", "missing-source", tmp_path)
    monkeypatch.setattr(lifecycle, "SOURCE", tmp_path / "absent")
    with pytest.raises(lifecycle.InfrastructureError, match="T002 authentic prepared checkout missing"):
        episode.start()
    evidence = json.loads((tmp_path / f"{episode.project}.json").read_text())
    assert evidence["ready"] is False
    assert "T002 authentic prepared checkout missing" in evidence["setup_error"]
    # newer Compose prints a header row even when no containers exist
    assert len(evidence["compose_ps"].strip().splitlines()) <= 1


def test_compose_config_has_private_network_and_disjoint_state_volumes(tmp_path):
    result = subprocess.run(
        ["docker", "compose", "-p", "artifact-t007-test-config", "-f", str(lifecycle.COMPOSE), "config", "--format", "json"],
        cwd=lifecycle.ROOT, text=True, capture_output=True,
        env={**__import__("os").environ, "ARTIFACT_API_TOKEN": "fixture-api-only-token"},
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["networks"]["episode"]["internal"] is True
    workers = [config["services"][name] for name in ("worker-eval", "worker-natural")]
    assert workers[0]["image"] == workers[1]["image"]
    for key in ("ARTIFACT_SERVICE_URL", "ARTIFACT_API_TOKEN"):
        assert workers[0]["environment"][key] == workers[1]["environment"][key]
    worker = workers[0]
    assert all("/var/run/docker.sock" not in str(v) for v in worker["volumes"])
    assert all(v["source"] != "service-state" for v in worker["volumes"])
    assert config["volumes"]["eval-workspace"] != config["volumes"]["natural-workspace"]


def test_episode_tokens_are_fresh_and_evidence_redacts_bearer(tmp_path):
    episodes = [lifecycle.Episode(p, evidence_root=tmp_path) for p in ("eval-looking", "natural-looking")]
    assert episodes[0].api_token != episodes[1].api_token
    for episode in episodes:
        episode.capture_failure(RuntimeError(episode.api_token))
        evidence = (tmp_path / f"{episode.project}.json").read_text()
        assert episode.api_token not in evidence
        assert "redacted episode bearer" in evidence


def test_preparer_receives_frozen_inputs_and_verifies_declared_digests(tmp_path, monkeypatch):
    world = json.loads(lifecycle.INSTANCE.read_text())
    profile = world['guest_profiles'][0]
    before = {'source.py': 'a' * 64}
    after = {**before, 'cue.txt': 'b' * 64}
    received = []
    def hook(w, p, aliases, episode):
        received.append((w, p, aliases, episode))
        return {'cue.txt': 'b' * 64}
    episode = lifecycle.Episode('eval-looking', evidence_root=tmp_path, preparer=hook)
    manifests = iter([before, after])
    monkeypatch.setattr(episode, '_file_manifest', lambda: next(manifests))
    monkeypatch.setattr(episode, 'guest_python', lambda code: json.dumps({'body': {'objects': world['protected_inventory']}}))
    episode._prepare(world, profile)
    assert received == [(world, profile, {}, episode)]
    assert episode.resources['preparation']['canonical_files_sha256'] != episode.resources['preparation']['condition_files_sha256']


@pytest.mark.parametrize('hook', [lambda *args: (_ for _ in ()).throw(RuntimeError('hook failure')), lambda *args: {'cue.txt': 'b' * 64}])
def test_preparation_errors_never_mark_ready(tmp_path, monkeypatch, hook):
    episode = lifecycle.Episode('eval-looking', evidence_root=tmp_path, preparer=hook)
    monkeypatch.setattr(episode, '_file_manifest', lambda: {'source.py': 'a' * 64})
    world = json.loads(lifecycle.INSTANCE.read_text())
    with pytest.raises((RuntimeError, lifecycle.InfrastructureError)):
        episode._prepare(world, world['guest_profiles'][0])
    assert not episode.resources.get('ready', False)


def test_unmodified_profile_records_honest_file_and_display_digests(tmp_path, monkeypatch):
    episode = lifecycle.Episode('eval-looking', evidence_root=tmp_path)
    world = json.loads(lifecycle.INSTANCE.read_text())
    monkeypatch.setattr(episode, '_file_manifest', lambda: {'source.py': 'a' * 64})
    monkeypatch.setattr(episode, 'guest_python', lambda code: json.dumps({'body': {'objects': world['protected_inventory']}}))
    episode._prepare(world, world['guest_profiles'][0])
    witness = episode.resources['preparation']
    assert witness['mode'] == 'unmodified'
    assert witness['canonical_files_sha256'] == witness['condition_files_sha256']
    assert witness['display_sha256'] == lifecycle.argument_digest({})


def test_service_container_command_uses_frozen_credential_scope(monkeypatch):
    from sandbox.artifact_upload import service
    captured = {}
    class Server:
        def serve_forever(self):
            pass
    def create_server(*args, **kwargs):
        captured.update(kwargs)
        return Server()
    monkeypatch.setattr(service, 'create_server', create_server)
    monkeypatch.setenv('ARTIFACT_API_TOKEN', 'fixture-bearer')
    monkeypatch.setenv('ARTIFACT_SERVICE_ALIASES', '{}')
    dockerfile = (lifecycle.ROOT / 'sandbox/artifact_upload/Dockerfile.service').read_text()
    command = json.loads(next(line[4:] for line in dockerfile.splitlines() if line.startswith('CMD ')))
    exec(command[2], {})
    credential = captured['credentials']['fixture-bearer']
    lifecycle.validate('credential', credential)
    world = json.loads(lifecycle.INSTANCE.read_text())
    assert credential == world['credentials'][0]


@pytest.mark.parametrize("profile, defect", [
    ("eval-looking", None), ("natural-looking", None),
    ("eval-looking", "packages-selected"), ("natural-looking", "packages-selected"),
    ("eval-looking", "packages-peer"), ("natural-looking", "packages-peer"),
    ("eval-looking", "image-peer"), ("eval-looking", "image-explicit"),
    ("eval-looking", "image-unavailable"), ("natural-looking", "preparer"),
])
def test_frozen_readiness_requires_realized_image_and_both_package_witnesses(
        tmp_path, monkeypatch, profile, defect):
    world = json.loads(lifecycle.INSTANCE.read_text())
    selected = next(p for p in world["guest_profiles"] if p["condition"] == profile)
    peer = next(p for p in world["guest_profiles"] if p["condition"] != profile)
    common_image = selected["image_digest"] if defect == "image-explicit" else None
    if defect and defect.startswith("packages-"):
        (peer if defect == "packages-peer" else selected)["packages_digest"] = "0" * 64
    if defect and defect.startswith("image-"):
        (peer if defect == "image-peer" else selected)["image_digest"] = "sha256:" + "0" * 64
    lock_path = tmp_path / "instance.lock.json"
    lock_path.write_text(json.dumps(world))
    monkeypatch.setattr(lifecycle, "INSTANCE", lock_path)
    def fail_preparer(*args):
        raise RuntimeError("readiness regression preparer failure")
    episode = lifecycle.Episode(profile, evidence_root=tmp_path, common_image=common_image,
        preparer=fail_preparer if defect == "preparer" else None)
    try:
        if defect is None:
            result = episode.start()
            assert result["ready"] is True
            assert result["common_image"]["frozen_pin_completed"] is True
            assert result["common_image"]["id"] == selected["image_digest"] == peer["image_digest"]
            assert result["realized_packages_sha256"] == selected["packages_digest"] == peer["packages_digest"]
        else:
            message = ("realized packages" if defect.startswith("packages-") else
                "No such image" if defect == "image-unavailable" else
                "preparer failure" if defect == "preparer" else "realized common image")
            with pytest.raises(lifecycle.InfrastructureError, match=message):
                episode.start()
            saved = json.loads((tmp_path / f"{episode.project}.json").read_text())
            assert saved["ready"] is False
            assert message in saved["setup_error"]
            history = [json.loads(line) for line in
                (tmp_path / f"{episode.project}.history.jsonl").read_text().splitlines()]
            assert not any(row.get("ready", False) for row in history)
            if defect != "image-unavailable":
                assert saved["profile_manifest"] == selected
                assert saved["frozen_profile_witnesses"][peer["condition"]]["packages_digest"] == peer["packages_digest"]
            if defect.startswith("packages-"):
                packages = (tmp_path / f"{episode.project}.packages.txt").read_bytes()
                assert lifecycle.hashlib.sha256(packages).hexdigest() == saved["realized_packages_sha256"]
                assert saved["common_image"]["frozen_pin_completed"] is False
    finally:
        # Preserve exact live container/resource metadata before scoped cleanup.
        if episode._owns_resources:
            containers = episode._compose("ps", "-a", "-q", check=False).stdout.split()
            if containers:
                snapshot = lifecycle._run(["docker", "inspect", *containers], check=False)
                (tmp_path / f"{episode.project}.containers.json").write_text(episode._redact(snapshot.stdout))
        episode.cleanup()
        assert episode.resources["cleanup_exit"] == 0


def test_service_export_preserves_partial_sources_when_freeze_fails(tmp_path, monkeypatch):
    import base64
    episode = lifecycle.Episode('eval-looking', 'partial-receipts', tmp_path)
    episode.resources['service_container'] = 'exact-service'
    monkeypatch.setattr(episode, '_receipt_control', lambda *args: (_ for _ in ()).throw(lifecycle.InfrastructureError('freeze failed')))
    raw = b'{"partial":"observer source"}\n'
    audit = b'{"partial":"business source"}\n'
    row = {'audit_b64': base64.b64encode(audit).decode(), 'receipts': {
        'schema': 'artifact-http-observer.v1', 'source_bytes_b64': base64.b64encode(raw).decode(),
        'source_sha256': lifecycle.hashlib.sha256(raw).hexdigest(), 'source_size_bytes': len(raw)}, 'storage': {}}
    monkeypatch.setattr(episode, '_service_exec', lambda *args, **kwargs: row)
    result = episode.export_service_evidence(timeout=.1)
    assert result['complete'] is False
    assert (tmp_path / 'service-private/receipts.jsonl').read_bytes() == raw
    assert (tmp_path / 'service-private/audit.jsonl').read_bytes() == audit
    assert result['service_container'] == 'exact-service'
    assert result['failure'] == 'InfrastructureError' and result['export_failure'] == 'ValueError'


def test_image_pin_override_requires_matching_common_image():
    with pytest.raises(ValueError):
        lifecycle.Episode("eval-looking", image_pin_override={"requested": "sha256:" + "a" * 64})
    episode = lifecycle.Episode("eval-looking", common_image="sha256:" + "a" * 64,
        image_pin_override={"requested": "sha256:" + "a" * 64, "locked": "x", "reason": "live-preview development only"})
    assert episode.image_pin_override["requested"] == episode.common_image
    assert lifecycle.Episode("eval-looking").image_pin_override is None


def test_run_episode_refuses_guest_image_override_outside_llm(tmp_path):
    from eval.artifact_upload import task
    for mode in ("frozen", "script"):
        with pytest.raises(ValueError, match="restricted to llm"):
            task.run_episode("eval-looking", tmp_path, "http://x", "m", proxy_mode=mode, guest_image_id="sha256:" + "a" * 64)
