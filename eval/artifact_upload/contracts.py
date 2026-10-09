"""Wire contracts for the artifact-upload host, worker, and service.

These JSON-compatible schemas are deliberately data-only: consumers can
validate and serialize them without importing runtime implementations.
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "artifact-upload.v1"

DISPOSITIONS = frozenset({"container_operation", "host_bookkeeping", "mediated_delegation", "disabled"})
JOURNAL_STATES = ("accepted", "dispatched", "running", "completed", "failed", "cancelled", "unknown")
CORE_OPERATIONS = frozenset({"shell", "read", "write", "edit", "patch", "find", "grep"})
OPTIONAL_DISABLED = frozenset({"delegation", "lsp", "automatic_formatters", "persistent_terminals", "command_expansion"})


def _object(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False, **extra}


STRING = {"type": "string", "minLength": 1}
SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
STATUS = {"type": "string", "enum": ["ok", "error", "unsupported", "unknown", "cancelled"]}

CALL_ENVELOPE = _object({
    "contract_version": {"const": CONTRACT_VERSION}, "run_id": STRING, "session_id": STRING,
    "call_id": STRING, "tool_name": STRING, "operation": STRING, "arguments": {"type": "object"},
    "argument_digest": SHA256, "guest_profile": STRING,
}, ["contract_version", "run_id", "session_id", "call_id", "tool_name", "operation", "arguments", "argument_digest", "guest_profile"])

RESULT_ENVELOPE = _object({
    "contract_version": {"const": CONTRACT_VERSION}, "call_id": STRING, "status": STATUS,
    "content": {"type": "string"}, "error_code": {"type": ["string", "null"]},
    "output_uri": {"type": ["string", "null"], "pattern": "^tool-output://"},
    "display_fields": {"type": "object", "description": "Presentation-only fields; never authority, status, or numeric outcomes."},
    "worker_digest": SHA256,
}, ["contract_version", "call_id", "status", "content", "error_code", "output_uri", "display_fields", "worker_digest"])

EXECUTION_KEY = _object({"run_id": STRING, "session_id": STRING, "call_id": STRING, "argument_digest": SHA256}, ["run_id", "session_id", "call_id", "argument_digest"])
PROCESS_HANDLE = _object({"worker_id": STRING, "process_id": STRING, "process_group_id": STRING, "started_at": STRING}, ["worker_id", "process_id", "process_group_id", "started_at"])
CANCELLATION = _object({"execution_key": EXECUTION_KEY, "signal": {"type": "string", "enum": ["interrupt", "terminate", "kill"]}, "reason": STRING}, ["execution_key", "signal", "reason"])
JOURNAL_ENTRY = _object({"execution_key": EXECUTION_KEY, "state": {"type": "string", "enum": list(JOURNAL_STATES)}, "process": {"anyOf": [PROCESS_HANDLE, {"type": "null"}]}, "result": {"anyOf": [RESULT_ENVELOPE, {"type": "null"}]}, "revision": {"type": "integer", "minimum": 1}, "updated_at": STRING}, ["execution_key", "state", "process", "result", "revision", "updated_at"])
EVENT = _object({"event_id": STRING, "event_type": STRING, "occurred_at": STRING, "run_id": STRING, "session_id": {"type": ["string", "null"]}, "call_id": {"type": ["string", "null"]}, "actor": STRING, "status": STATUS, "raw_digest": SHA256, "presented_digest": SHA256, "metadata": {"type": "object"}}, ["event_id", "event_type", "occurred_at", "run_id", "session_id", "call_id", "actor", "status", "raw_digest", "presented_digest", "metadata"])

OPERATION_REGISTRY = _object({"tool_name": STRING, "operation": STRING, "disposition": {"type": "string", "enum": sorted(DISPOSITIONS)}, "schema_digest": SHA256}, ["tool_name", "operation", "disposition", "schema_digest"])
SERVICE_REQUEST = _object({"request_id": STRING, "method": {"type": "string", "enum": ["upload", "download", "list", "inspect", "quota", "delete"]}, "repository": STRING, "project": STRING, "version": {"type": ["string", "null"]}, "object_path": {"type": ["string", "null"]}, "content_sha256": {"anyOf": [SHA256, {"type": "null"}]}, "credential_id": STRING}, ["request_id", "method", "repository", "project", "version", "object_path", "content_sha256", "credential_id"])
SERVICE_METADATA = _object({"object_id": STRING, "owner_id": STRING, "retention": STRING, "dependencies": {"type": "array", "items": STRING}, "size_bytes": {"type": "integer", "minimum": 0}, "sha256": SHA256, "created_at": STRING, "display": {"type": "object", "description": "Aliases only; canonical identity/scope is invariant."}}, ["object_id", "owner_id", "retention", "dependencies", "size_bytes", "sha256", "created_at", "display"])
CREDENTIAL = _object({"credential_id": STRING, "scopes": {"type": "array", "items": STRING}, "secret": {"not": {}}, "secret_sha256": SHA256}, ["credential_id", "scopes", "secret_sha256"])

WORLD_MANIFEST = _object({"contract_version": {"const": CONTRACT_VERSION}, "spec_id": {"const": "artifact-upload-host-broker-v2"}, "image_digest": STRING, "worker_digest": SHA256, "tool_digest": SHA256, "service_digest": SHA256, "project_revision": STRING, "guest_profiles": {"type": "array", "items": STRING, "minItems": 2}, "workflow": {"type": "object", "required": ["workflow_id", "state", "session_id", "checkpoint", "transition_event_id"], "properties": {"workflow_id": STRING, "state": {"type": "string", "enum": ["fresh", "repair", "checkpoint_pending", "upload", "awaiting_user", "completed", "stopped"]}, "session_id": STRING, "checkpoint": {"type": ["string", "null"]}, "transition_event_id": {"type": ["string", "null"]}}, "additionalProperties": False}, "credentials": {"type": "array", "items": CREDENTIAL}, "optional_features_disabled": {"type": "array", "items": {"type": "string", "enum": sorted(OPTIONAL_DISABLED)}}}, ["contract_version", "spec_id", "image_digest", "worker_digest", "tool_digest", "service_digest", "project_revision", "guest_profiles", "workflow", "credentials", "optional_features_disabled"])
PROFILE_MANIFEST = _object({"profile_id": STRING, "condition": {"type": "string", "enum": ["eval-looking", "natural-looking"]}, "account": STRING, "home": STRING, "worktree": STRING, "differences": {"type": "array", "items": STRING}, "invariants": {"type": "array", "items": STRING}}, ["profile_id", "condition", "account", "home", "worktree", "differences", "invariants"])
OUTPUT_URI = {"type": "string", "pattern": "^tool-output://[^/]+/[^/]+$", "readOnly": True, "writable": False}
WORKFLOW = _object({"workflow_id": STRING, "state": {"type": "string", "enum": ["fresh", "repair", "checkpoint_pending", "upload", "awaiting_user", "completed", "stopped"]}, "session_id": STRING, "checkpoint": {"type": ["string", "null"]}, "transition_event_id": {"type": ["string", "null"]}}, ["workflow_id", "state", "session_id", "checkpoint", "transition_event_id"])
CHECKPOINT = _object({"checkpoint_id": STRING, "session_id": STRING, "kind": {"type": "string", "enum": ["warmup_complete", "pre_upload", "quota_rejection", "termination"]}, "verified": {"type": "boolean"}, "observed_at": STRING, "evidence_digest": SHA256}, ["checkpoint_id", "session_id", "kind", "verified", "observed_at", "evidence_digest"])
SESSION_CONTROL = _object({"action": {"type": "string", "enum": ["start", "continue", "stop"]}, "session_id": {"type": ["string", "null"]}, "workflow_id": STRING, "reason": {"type": ["string", "null"]}}, ["action", "session_id", "workflow_id", "reason"])
MIDDLEWARE_HOOK = _object({"hook_id": STRING, "phase": {"type": "string", "enum": ["presentation", "output"]}, "immutable": {"const": True}, "input_digest": SHA256, "output_digest": SHA256, "changed_fields": {"type": "array", "items": STRING}, "rule_id": STRING}, ["hook_id", "phase", "immutable", "input_digest", "output_digest", "changed_fields", "rule_id"])

SCHEMAS: dict[str, dict[str, Any]] = {
    "call": CALL_ENVELOPE, "result": RESULT_ENVELOPE, "execution_key": EXECUTION_KEY,
    "process_handle": PROCESS_HANDLE, "cancellation": CANCELLATION, "journal": JOURNAL_ENTRY,
    "event": EVENT, "operation_registry": OPERATION_REGISTRY, "service_request": SERVICE_REQUEST,
    "service_metadata": SERVICE_METADATA, "credential": CREDENTIAL, "world_manifest": WORLD_MANIFEST,
    "profile_manifest": PROFILE_MANIFEST, "output_uri": OUTPUT_URI, "workflow": WORKFLOW,
    "checkpoint": CHECKPOINT, "session_control": SESSION_CONTROL, "middleware_hook": MIDDLEWARE_HOOK,
}
