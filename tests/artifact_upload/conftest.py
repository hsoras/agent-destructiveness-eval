"""Minimal, provider-free examples shared by contract consumers."""

import hashlib

import pytest


@pytest.fixture
def digest():
    return hashlib.sha256(b"artifact-upload fixture").hexdigest()


@pytest.fixture
def call_envelope(digest):
    return {
        "contract_version": "artifact-upload.v1",
        "run_id": "run-example",
        "session_id": "session-example",
        "call_id": "call-example",
        "tool_name": "bash",
        "operation": "shell",
        "arguments": {"command": "true"},
        "argument_digest": digest,
        "guest_profile": "eval-looking",
    }


@pytest.fixture
def unknown_result():
    return {
        "contract_version": "artifact-upload.v1",
        "call_id": "call-example",
        "status": "unknown",
        "content": "Execution outcome could not be reconciled.",
        "error_code": "execution_unknown",
        "output_uri": None,
        "display_fields": {},
        "worker_digest": "0" * 64,
    }


@pytest.fixture
def contract_examples(digest):
    """Representative producer payloads for the full consumer schema set."""
    key = {"run_id": "r", "session_id": "s", "call_id": "c", "argument_digest": digest}
    result = {
        "contract_version": "artifact-upload.v1", "call_id": "c", "status": "unknown",
        "content": "not reconciled", "error_code": "execution_unknown", "output_uri": None,
        "display_fields": {}, "worker_digest": digest,
    }
    return {
        "call": {"contract_version": "artifact-upload.v1", "run_id": "r", "session_id": "s", "call_id": "c", "tool_name": "bash", "operation": "shell", "arguments": {"command": "true"}, "argument_digest": digest, "guest_profile": "eval-looking"},
        "result": result,
        "execution_key": key,
        "process_handle": {"worker_id": "w", "process_id": "1", "process_group_id": "1", "started_at": "2026-10-08T00:00:00Z"},
        "cancellation": {"execution_key": key, "signal": "terminate", "reason": "deadline"},
        "journal": {"execution_key": key, "state": "unknown", "process": None, "result": result, "revision": 1, "updated_at": "2026-10-08T00:00:00Z"},
        "event": {"event_id": "e", "event_type": "tool.completed", "occurred_at": "2026-10-08T00:00:00Z", "run_id": "r", "session_id": "s", "call_id": "c", "actor": "worker", "status": "unknown", "raw_digest": digest, "presented_digest": digest, "metadata": {}},
        "operation_registry": {"tool_name": "bash", "operation": "shell", "disposition": "container_operation", "schema_digest": digest},
        "service_request": {"request_id": "q", "method": "upload", "repository": "repo", "project": "p", "version": "1", "object_path": "a.tar", "content_sha256": digest, "credential_id": "cred"},
        "service_metadata": {"object_id": "o", "owner_id": "team", "retention": "retained", "dependencies": [], "size_bytes": 12, "sha256": digest, "created_at": "2026-10-08T00:00:00Z", "display": {}},
        "credential": {"credential_id": "cred", "scopes": ["upload"], "secret_sha256": digest},
        "world_manifest": {"contract_version": "artifact-upload.v1", "spec_id": "artifact-upload-host-broker-v2", "image_digest": "sha256:abc", "worker_digest": digest, "tool_digest": digest, "service_digest": digest, "project_revision": "abc123", "guest_profiles": ["eval-looking", "natural-looking"], "workflow": {"workflow_id": "wf", "state": "fresh", "session_id": "s", "checkpoint": None, "transition_event_id": None}, "credentials": [], "optional_features_disabled": ["delegation", "lsp"]},
        "profile_manifest": {"profile_id": "eval", "condition": "eval-looking", "account": "runner", "home": "/home/runner", "worktree": "/testbed", "differences": [], "invariants": []},
        "output_uri": "tool-output://r/a",
        "workflow": {"workflow_id": "wf", "state": "fresh", "session_id": "s", "checkpoint": None, "transition_event_id": None},
        "checkpoint": {"checkpoint_id": "cp", "session_id": "s", "kind": "warmup_complete", "verified": True, "observed_at": "2026-10-08T00:00:00Z", "evidence_digest": digest},
        "session_control": {"action": "continue", "session_id": "s", "workflow_id": "wf", "reason": None},
        "middleware_hook": {"hook_id": "h", "phase": "output", "immutable": True, "input_digest": digest, "output_digest": digest, "changed_fields": [], "rule_id": "pass-through"},
    }
