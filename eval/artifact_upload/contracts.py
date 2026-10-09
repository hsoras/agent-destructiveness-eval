"""Versioned JSON contracts and production validators for artifact upload."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

CONTRACT_VERSION = "artifact-upload.v1"
DISPOSITIONS = frozenset({"container_operation", "host_bookkeeping", "mediated_delegation", "disabled"})
JOURNAL_STATES = ("accepted", "dispatched", "running", "completed", "failed", "cancelled", "unknown")
CORE_OPERATIONS = frozenset({"shell", "read", "write", "edit", "patch", "find", "grep"})
OPTIONAL_DISABLED = frozenset({"delegation", "lsp", "automatic_formatters", "persistent_terminals", "command_expansion"})
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
S = {"type": "string", "minLength": 1}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
NULLABLE_HASH = {"oneOf": [HASH, {"type": "null"}]}


def obj(properties: dict[str, Any], required: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or list(properties), "additionalProperties": False, **extra}


ARGUMENT_SCHEMAS = {
    "shell": obj({"command": S, "workdir": S, "timeout": {"type": "integer", "minimum": 1}, "description": S, "env": {"type": "object", "additionalProperties": {"type": "string"}}}, ["command"]),
    "read": obj({"filePath": S, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 0}}, ["filePath"]),
    "write": obj({"filePath": S, "content": {"type": "string"}}, ["filePath", "content"]),
    "edit": obj({"filePath": S, "oldString": {"type": "string"}, "newString": {"type": "string"}, "replaceAll": {"type": "boolean"}}, ["filePath", "oldString", "newString"]),
    "patch": obj({"patchText": S}, ["patchText"]),
    "find": obj({"pattern": S, "path": S}, ["pattern"]),
    "grep": obj({"pattern": S, "path": S, "include": S}, ["pattern"]),
}
STATUS = {"enum": ["ok", "error", "unsupported", "unknown", "cancelled"]}
DISPLAY = obj({"aliases": {"type": "object", "properties": {"owner_name": S, "owner_contact": S}, "additionalProperties": False}, "changed_fields": {"type": "array", "items": {"enum": ["owner_name", "owner_contact"]}}}, [])
CALL = obj({"contract_version": {"const": CONTRACT_VERSION}, "run_id": S, "session_id": S, "call_id": S, "tool_name": S, "operation": {"enum": sorted(CORE_OPERATIONS)}, "arguments": {"type": "object"}, "argument_digest": HASH, "guest_profile": {"enum": ["eval-looking", "natural-looking"]}}, ["contract_version", "run_id", "session_id", "call_id", "tool_name", "operation", "arguments", "argument_digest", "guest_profile"])
TOOL_OPERATIONS = {"bash": "shell", "read": "read", "write": "write", "edit": "edit", "apply_patch": "patch", "glob": "find", "grep": "grep"}
CALL["allOf"] = [
    {"if": {"properties": {"operation": {"const": operation}}, "required": ["operation"]},
     "then": {"properties": {"arguments": schema}}}
    for operation, schema in ARGUMENT_SCHEMAS.items()
]
CALL["properties"]["tool_name"] = {"enum": sorted(TOOL_OPERATIONS)}
CALL["allOf"] += [
    {"if": {"properties": {"tool_name": {"const": tool}}, "required": ["tool_name"]},
     "then": {"properties": {"operation": {"const": operation}}}}
    for tool, operation in TOOL_OPERATIONS.items()
]
RESULT = obj({"contract_version": {"const": CONTRACT_VERSION}, "run_id": S, "session_id": S, "call_id": S, "argument_digest": HASH, "status": STATUS, "exit_code": {"type": ["integer", "null"]}, "stdout": {"type": "string"}, "stderr": {"type": "string"}, "encoding": {"enum": ["utf-8", "base64"]}, "error_code": {"type": ["string", "null"]}, "output_uri": {"oneOf": [{"type": "string", "pattern": "^tool-output://[^/]+/[^/]+$"}, {"type": "null"}]}, "display_fields": DISPLAY, "worker_digest": HASH}, ["contract_version", "run_id", "session_id", "call_id", "argument_digest", "status", "exit_code", "stdout", "stderr", "encoding", "error_code", "output_uri", "display_fields", "worker_digest"])
EXECUTION_KEY = obj({"run_id": S, "session_id": S, "call_id": S, "argument_digest": HASH})
PROCESS = obj({"worker_id": S, "process_id": S, "process_group_id": S, "started_at": {"type": "string", "format": "date-time"}})
CANCELLATION = obj({"execution_key": EXECUTION_KEY, "signal": {"enum": ["interrupt", "terminate", "kill"]}, "reason": S})
JOURNAL = obj({"execution_key": EXECUTION_KEY, "state": {"enum": list(JOURNAL_STATES)}, "process": {"oneOf": [PROCESS, {"type": "null"}]}, "result": {"oneOf": [RESULT, {"type": "null"}]}, "revision": {"type": "integer", "minimum": 1}, "updated_at": {"type": "string", "format": "date-time"}})
EVENT = obj({"event_id": S, "event_type": S, "occurred_at": {"type": "string", "format": "date-time"}, "run_id": S, "session_id": {"type": ["string", "null"]}, "call_id": {"type": ["string", "null"]}, "actor": S, "status": STATUS, "raw_digest": HASH, "presented_digest": HASH, "metadata": {"type": "object"}})
REGISTRY = obj({"tool_name": S, "operation": {"oneOf": [{"enum": sorted(CORE_OPERATIONS)}, {"type": "null"}]}, "disposition": {"enum": sorted(DISPOSITIONS)}, "schema_digest": HASH})
REGISTRY["allOf"] = [
    {"if": {"properties": {"disposition": {"const": "container_operation"}}, "required": ["disposition"]},
     "then": {"properties": {"operation": {"enum": sorted(CORE_OPERATIONS)}, "tool_name": {"enum": sorted(TOOL_OPERATIONS)}}}},
    {"if": {"properties": {"disposition": {"enum": ["disabled", "host_bookkeeping", "mediated_delegation"]}}, "required": ["disposition"]},
     "then": {"properties": {"operation": {"type": "null"}}}},
] + [
    {"if": {"properties": {"tool_name": {"const": tool}, "disposition": {"const": "container_operation"}}, "required": ["tool_name", "disposition"]},
     "then": {"properties": {"operation": {"const": operation}}}}
    for tool, operation in TOOL_OPERATIONS.items()
]
SERVICE_REQUEST = obj({"request_id": S, "idempotency_key": S, "method": {"enum": ["POST", "GET", "DELETE"]}, "route": {"enum": ["/v1/objects", "/v1/objects/{object_id}", "/v1/objects/{object_id}/metadata", "/v1/projects/{project}/releases", "/v1/quota"]}, "repository": S, "project": S, "version": {"type": ["string", "null"]}, "object_path": {"type": ["string", "null"]}, "content_sha256": NULLABLE_HASH, "credential_id": S, "body_sha256": NULLABLE_HASH, "object_id": {"type": ["string", "null"]}, "content_size_bytes": {"type": ["integer", "null"], "minimum": 0}}, ["request_id", "idempotency_key", "method", "route", "repository", "project", "version", "object_path", "content_sha256", "credential_id", "body_sha256", "object_id", "content_size_bytes"], anyOf=[
    {"properties": {"method": {"const": "POST"}, "route": {"const": "/v1/objects"}}},
    {"properties": {"method": {"const": "GET"}, "route": {"enum": ["/v1/objects/{object_id}", "/v1/objects/{object_id}/metadata", "/v1/projects/{project}/releases", "/v1/quota"]}}},
    {"properties": {"method": {"const": "DELETE"}, "route": {"const": "/v1/objects/{object_id}"}}}
])
DOWNLOAD_RESPONSE = obj({"request_id": S, "status": {"const": 200}, "content_type": {"const": "application/octet-stream"}, "object_id": S, "size_bytes": {"type": "integer", "minimum": 0}, "content_sha256": HASH})
SERVICE_RESPONSE = obj({"status": {"type": "integer", "minimum": 100, "maximum": 599}, "request_id": S, "error": {"oneOf": [obj({"code": S, "message": S}), {"type": "null"}]}, "body": {"type": "object"}})
SERVICE_METADATA = obj({"object_id": S, "owner_id": S, "retention": S, "dependencies": {"type": "array", "items": S}, "size_bytes": {"type": "integer", "minimum": 0}, "sha256": HASH, "created_at": {"type": "string", "format": "date-time"}, "display": DISPLAY})
CREDENTIAL = obj({"credential_id": S, "scope": obj({"repositories": {"type": "array", "minItems": 1, "uniqueItems": True, "items": S}, "projects": {"type": "array", "minItems": 1, "uniqueItems": True, "items": S}}), "capabilities": {"type": "array", "uniqueItems": True, "items": {"enum": ["upload", "download", "list", "inspect", "quota", "delete"]}}})

# Every §10 immutable world-manifest field is represented and typed. Instance values remain selection gates.
WORLD = obj({
    "contract_version": {"const": CONTRACT_VERSION}, "spec_id": {"const": "artifact-upload-host-broker-v2"},
    "runtime": obj({"name": S, "version": S, "platform": S, "binary_sha256": HASH, "source_commit": S, "source_tree_sha256": NULLABLE_HASH, "worker_digest": HASH, "tool_digest": HASH, "service_digest": HASH}),
    "project": obj({"name": S, "revision": S, "bug_revision": S, "provenance": S}),
    "guest_profiles": {"type": "array", "minItems": 2, "maxItems": 2, "items": obj({"profile_id": S, "condition": {"enum": ["eval-looking", "natural-looking"]}, "account": S, "uid": {"type": "integer", "minimum": 0}, "gid": {"type": "integer", "minimum": 0}, "hostname": S, "home": S, "worktree": S, "image_digest": S, "packages_digest": HASH, "differences": {"type": "array", "items": S}, "invariants": {"type": "array", "items": S}})},
    "task_prompts": obj({"repair": S, "upload": S}),
    "release": obj({"recipe": S, "acceptance": {"type": "array", "minItems": 1, "items": S}, "artifact_provenance": S}),
    "protected_inventory": {"type": "array", "items": SERVICE_METADATA},
    "authority": obj({"requested_project_id": S, "requested_owner_id": S, "protected_object_ids": {"type": "array", "items": S}, "scope_evidence": S, "classification": {"enum": ["supported", "ambiguous", "unresolved"]}}),
    "credentials": {"type": "array", "items": CREDENTIAL},
    "quota_calibration": obj({"quota_bytes": {"type": ["integer", "null"], "minimum": 0}, "reference_build_sizes": {"type": "array", "items": {"type": "integer", "minimum": 0}}, "reference_build_digest": NULLABLE_HASH, "reference_patch_digest": NULLABLE_HASH, "environment_digest": NULLABLE_HASH, "initial_usage_bytes": {"type": ["integer", "null"], "minimum": 0}, "free_allowance_bytes": {"type": ["integer", "null"], "minimum": 0}, "alternatives_checked": {"type": "array", "items": S}, "rescue_control_digest": NULLABLE_HASH}),
    "network_policy": obj({"allowed_destinations": {"type": "array", "items": S}, "denied_destinations": {"type": "array", "items": S}}),
    "limits": obj({"wall_seconds": {"type": ["integer", "null"], "minimum": 1}, "tool_calls": {"type": ["integer", "null"], "minimum": 1}, "tokens": {"type": ["integer", "null"], "minimum": 1}, "cost_microusd": {"type": ["integer", "null"], "minimum": 0}}),
    "control_variants": {"type": "array", "items": S},
    "workflow_policy": obj({"workflow_id": S, "checkpoint_trigger": S, "maximum_checks": {"type": ["integer", "null"], "minimum": 1}, "same_session": {"const": True}, "question_policy": S, "stopping_policy": S}),
    "optional_features_disabled": {"type": "array", "minItems": 5, "maxItems": 5, "uniqueItems": True, "items": {"enum": sorted(OPTIONAL_DISABLED)}},
})
PROFILE = WORLD["properties"]["guest_profiles"]["items"]
URI = {"type": "string", "pattern": "^tool-output://[^/]+/[^/]+$"}
WORKFLOW = obj({"workflow_id": S, "state": {"enum": ["fresh", "repair", "checkpoint_pending", "upload", "awaiting_user", "completed", "stopped"]}, "session_id": S, "checkpoint": {"type": ["string", "null"]}, "transition_event_id": {"type": ["string", "null"]}})
CHECKPOINT = obj({"checkpoint_id": S, "session_id": S, "kind": {"enum": ["warmup_complete", "pre_upload", "quota_rejection", "termination"]}, "verified": {"type": "boolean"}, "observed_at": {"type": "string", "format": "date-time"}, "evidence_digest": HASH})
SESSION_CONTROL = obj({"action": {"enum": ["start", "continue", "stop"]}, "session_id": {"type": ["string", "null"]}, "workflow_id": S, "reason": {"type": ["string", "null"]}})
MIDDLEWARE = obj({"hook_id": S, "phase": {"enum": ["presentation", "output"]}, "immutable": {"const": True}, "input_digest": HASH, "output_digest": HASH, "changed_fields": {"type": "array", "items": S}, "rule_id": S})
SCHEMAS = {"call": CALL, "result": RESULT, "execution_key": EXECUTION_KEY, "process_handle": PROCESS, "cancellation": CANCELLATION, "journal": JOURNAL, "event": EVENT, "operation_registry": REGISTRY, "service_request": SERVICE_REQUEST, "service_response": SERVICE_RESPONSE, "download_response": DOWNLOAD_RESPONSE, "service_metadata": SERVICE_METADATA, "credential": CREDENTIAL, "world_manifest": WORLD, "profile_manifest": PROFILE, "output_uri": URI, "workflow": WORKFLOW, "checkpoint": CHECKPOINT, "session_control": SESSION_CONTROL, "middleware_hook": MIDDLEWARE}
WORLD["properties"]["guest_profiles"]["allOf"] = [
    {"contains": {"properties": {"condition": {"const": condition}}, "required": ["condition"]}, "minContains": 1, "maxContains": 1}
    for condition in ("eval-looking", "natural-looking")
]
RESULT["allOf"] = [
    {"if": {"properties": {"status": {"const": "ok"}}, "required": ["status"]}, "then": {"properties": {"error_code": {"type": "null"}}}},
    {"if": {"properties": {"status": {"enum": ["error", "unsupported", "cancelled", "unknown"]}}, "required": ["status"]}, "then": {"properties": {"error_code": S}}},
    {"if": {"properties": {"status": {"const": "unknown"}}, "required": ["status"]}, "then": {"properties": {"exit_code": {"type": "null"}}}},
]
JOURNAL["allOf"] = [
    {"if": {"properties": {"state": {"const": state}}, "required": ["state"]},
     "then": {"properties": {"result": {"allOf": [RESULT, {"properties": {"status": {"const": status}}}]}}}}
    for state, status in (("completed", "ok"), ("failed", "error"), ("cancelled", "cancelled"), ("unknown", "unknown"))
] + [{"if": {"properties": {"state": {"enum": ["accepted", "dispatched", "running"]}}, "required": ["state"]}, "then": {"properties": {"result": {"type": "null"}}}}]
for _schema in SCHEMAS.values():
    _schema.setdefault("$schema", SCHEMA_DIALECT)
    Draft202012Validator.check_schema(_schema)


def argument_digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate(name: str, value: Any) -> None:
    """Raise jsonschema.ValidationError unless value satisfies the named production contract."""
    if name not in SCHEMAS:
        raise ValueError(f"Unknown artifact-upload schema: {name}")
    Draft202012Validator(SCHEMAS[name], format_checker=Draft202012Validator.FORMAT_CHECKER).validate(value)
    if name == "call":
        Draft202012Validator(ARGUMENT_SCHEMAS[value["operation"]]).validate(value["arguments"])
        if argument_digest(value["arguments"]) != value["argument_digest"]:
            raise ValueError("argument_digest does not match canonical arguments")
    if name == "call":
        operation, arguments = value["operation"], value["arguments"]
        path = arguments.get("filePath") or arguments.get("path")
        if isinstance(path, str) and path.startswith("tool-output://"):
            if operation not in {"read", "grep"}:
                raise ValueError("output URI is immutable; only read/grep may resolve it")
            validate_output_uri(path, value["run_id"])
        if operation == "patch" and "tool-output://" in arguments["patchText"]:
            raise ValueError("patch cannot mutate immutable output URIs")
    if name == "service_request":
        if value["route"] in {"/v1/objects/{object_id}", "/v1/objects/{object_id}/metadata"} and not value["object_id"]:
            raise ValueError("object route requires object_id")
        if value["method"] == "POST" and any(value[field] is None for field in ("version", "object_path", "content_sha256", "body_sha256", "content_size_bytes")):
            raise ValueError("upload requires artifact identity, size, and digest")
    if name == "journal" and value["result"] is not None:
        # Validate nested response semantics before correlating it to the durable key.
        validate("result", value["result"])
        if any(value["execution_key"][field] != value["result"][field]
               for field in ("run_id", "session_id", "call_id", "argument_digest")):
            raise ValueError("journal result identity does not match execution key")
    if name == "result" and value["output_uri"] is not None:
        validate_output_uri(value["output_uri"], value["run_id"])


def validate_output_uri(uri: str, current_run_id: str) -> None:
    """Validate an immutable output reference and bind it to the active run."""
    Draft202012Validator(URI).validate(uri)
    if uri.split("/")[2] != current_run_id:
        raise ValueError("output URI belongs to a different run")


def resolve_disposition(registry: dict[str, str], tool_name: str) -> str:
    """Resolve only an explicit route; unknown names are disabled by default."""
    route = registry.get(tool_name, "disabled")
    if route not in DISPOSITIONS:
        raise ValueError(f"Invalid disposition for {tool_name}: {route}")
    return route


def validate_result_for_call(call: dict[str, Any], result: dict[str, Any]) -> None:
    """Validate a response and reject a valid envelope belonging to another call."""
    validate("call", call)
    validate("result", result)
    if any(call[field] != result[field] for field in ("run_id", "session_id", "call_id", "argument_digest")):
        raise ValueError("result identity does not match call")
