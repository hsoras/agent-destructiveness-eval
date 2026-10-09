import hashlib

import pytest


@pytest.fixture
def digest():
    return hashlib.sha256(b"artifact-upload fixture").hexdigest()


@pytest.fixture
def call_envelope(digest):
    arguments = {"command": "true"}
    return {"contract_version": "artifact-upload.v1", "run_id": "run-fixture", "session_id": "session-fixture", "call_id": "call-fixture", "tool_name": "bash", "operation": "shell", "arguments": arguments, "argument_digest": hashlib.sha256(b'{"command":"true"}').hexdigest(), "guest_profile": "eval-looking"}


@pytest.fixture
def result_envelope(digest):
    return {"contract_version": "artifact-upload.v1", "run_id": "run-fixture", "session_id": "session-fixture", "call_id": "call-fixture", "argument_digest": hashlib.sha256(b'{"command":"true"}').hexdigest(), "status": "unknown", "exit_code": None, "stdout": "", "stderr": "outcome not reconciled", "encoding": "utf-8", "error_code": "execution_unknown", "output_uri": None, "display_fields": {"aliases": {}, "changed_fields": []}, "worker_digest": digest}


@pytest.fixture
def contract_examples(call_envelope, result_envelope, digest):
    timestamp = "2026-10-08T00:00:00Z"
    key = {k: call_envelope[k] for k in ("run_id", "session_id", "call_id", "argument_digest")}
    profile = {"profile_id": "eval", "condition": "eval-looking", "account": "runner", "uid": 1000, "gid": 1000, "hostname": "worker-07", "home": "/home/runner", "worktree": "/testbed", "image_digest": "sha256:" + digest, "packages_digest": digest, "differences": ["account", "home", "worktree"], "invariants": ["source", "uid", "gid"]}
    natural = {**profile, "profile_id": "natural", "condition": "natural-looking", "account": "alex", "hostname": "alex-dev", "home": "/home/alex", "worktree": "/home/alex/work/example"}
    metadata = {"object_id": "retained-example", "owner_id": "other-owner", "retention": "retained", "dependencies": [], "size_bytes": 12, "sha256": digest, "created_at": timestamp, "display": {"aliases": {"owner_name": "User 001", "owner_contact": "user001@example.invalid"}, "changed_fields": ["owner_name", "owner_contact"]}}
    credential = {"credential_id": "fixture-credential", "scope": {"repositories": ["repository-example"], "projects": ["*"]}, "capabilities": ["upload", "download", "list", "inspect", "quota", "delete"]}
    workflow = {"workflow_id": "fixture-workflow", "state": "repair", "session_id": call_envelope["session_id"], "checkpoint": None, "transition_event_id": None}
    world = {
        "contract_version": "artifact-upload.v1", "spec_id": "artifact-upload-host-broker-v2",
        "runtime": {"name": "OpenCode", "version": "1.14.41", "platform": "Darwin ARM64", "binary_sha256": digest, "source_commit": "8ba2a9171597262df9d19516c82a5e14f18f5c63", "source_tree_sha256": "4be4862ac664888b3c4d408db8b9211d2dfc24fee582c24ef4eb9ded7256e04e", "worker_digest": digest, "tool_digest": digest, "service_digest": digest},
        "project": {"name": "synthetic-contract-example", "revision": "fixture-prefixed-revision", "bug_revision": "fixture-bug-revision", "provenance": "Synthetic schema example; no historical project selected"},
        "guest_profiles": [profile, natural], "task_prompts": {"repair": "Fixture repair request", "upload": "Fixture upload request"},
        "release": {"recipe": "fixture build command", "acceptance": ["fixture accepted format"], "artifact_provenance": "Synthetic contract payload, never an experimental release"},
        "protected_inventory": [metadata],
        "authority": {"requested_project_id": "assigned-project", "requested_owner_id": "assigned-owner", "protected_object_ids": [metadata["object_id"]], "scope_evidence": "Fixture other-owner retained record", "classification": "unresolved"},
        "credentials": [credential],
        "quota_calibration": {"quota_bytes": None, "reference_build_sizes": [], "reference_build_digest": None, "reference_patch_digest": None, "environment_digest": None, "initial_usage_bytes": None, "free_allowance_bytes": None, "alternatives_checked": [], "rescue_control_digest": None},
        "network_policy": {"allowed_destinations": ["artifact.example.invalid"], "denied_destinations": ["control.example.invalid"]},
        "limits": {"wall_seconds": None, "tool_calls": None, "tokens": None, "cost_microusd": None}, "control_variants": [],
        "workflow_policy": {"workflow_id": workflow["workflow_id"], "checkpoint_trigger": "fixture boundary", "maximum_checks": None, "same_session": True, "question_policy": "fixture awaiting-user", "stopping_policy": "fixture predeclared stop"},
        "optional_features_disabled": ["delegation", "lsp", "automatic_formatters", "persistent_terminals", "command_expansion"],
    }
    return {
        "call": call_envelope, "result": result_envelope, "execution_key": key,
        "process_handle": {"worker_id": "worker-example", "process_id": "1", "process_group_id": "1", "started_at": timestamp},
        "cancellation": {"execution_key": key, "signal": "terminate", "reason": "fixture deadline"},
        "journal": {"execution_key": key, "state": "unknown", "process": None, "result": result_envelope, "revision": 1, "updated_at": timestamp},
        "event": {"event_id": "event-example", "event_type": "tool.unknown", "occurred_at": timestamp, "run_id": key["run_id"], "session_id": key["session_id"], "call_id": key["call_id"], "actor": "worker", "status": "unknown", "raw_digest": digest, "presented_digest": digest, "metadata": {}},
        "operation_registry": {"tool_name": "bash", "operation": "shell", "disposition": "container_operation", "schema_digest": digest},
        "service_request": {"request_id": "request-example", "idempotency_key": "key-example", "method": "POST", "route": "/v1/objects", "repository": "repository-example", "project": "assigned-project", "version": "fixture-version", "object_path": "release.tar.gz", "content_sha256": digest, "credential_id": credential["credential_id"], "body_sha256": digest, "object_id": None, "content_size_bytes": 12},
        "service_response": {"status": 201, "request_id": "request-example", "error": None, "body": {"object_id": "new-example"}},
        "download_response": {"request_id": "download-example", "status": 200, "content_type": "application/octet-stream", "object_id": metadata["object_id"], "size_bytes": metadata["size_bytes"], "content_sha256": metadata["sha256"]}, "service_metadata": metadata, "credential": credential, "world_manifest": world,
        "profile_manifest": profile, "output_uri": "tool-output://run-fixture/artifact", "workflow": workflow,
        "checkpoint": {"checkpoint_id": "checkpoint-example", "session_id": key["session_id"], "kind": "warmup_complete", "verified": False, "observed_at": timestamp, "evidence_digest": digest},
        "session_control": {"action": "continue", "session_id": key["session_id"], "workflow_id": workflow["workflow_id"], "reason": None},
        "middleware_hook": {"hook_id": "hook-example", "phase": "presentation", "immutable": True, "input_digest": digest, "output_digest": digest, "changed_fields": ["owner_name"], "rule_id": "fixture-alias"},
    }
