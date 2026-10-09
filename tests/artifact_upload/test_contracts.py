import copy

import pytest
from jsonschema import ValidationError

from eval.artifact_upload.contracts import (
    ARGUMENT_SCHEMAS, CALL, CORE_OPERATIONS, DISPOSITIONS, OPTIONAL_DISABLED,
    SCHEMAS, TOOL_OPERATIONS, argument_digest, resolve_disposition, validate, validate_output_uri, validate_result_for_call,
)


def test_call_result_and_every_operation_validate(call_envelope, result_envelope):
    validate("call", call_envelope)
    validate("result", result_envelope)
    assert set(ARGUMENT_SCHEMAS) == CORE_OPERATIONS
    examples = {
        "shell": {"command": "true"}, "read": {"filePath": "src/a"},
        "write": {"filePath": "src/a", "content": "x"},
        "edit": {"filePath": "src/a", "oldString": "x", "newString": "y"},
        "patch": {"patchText": "*** Begin Patch"}, "find": {"pattern": "*.py"},
        "grep": {"pattern": "needle"},
    }
    for operation, arguments in examples.items():
        envelope = {**call_envelope, "operation": operation, "tool_name": next(name for name, op in TOOL_OPERATIONS.items() if op == operation), "arguments": arguments, "argument_digest": argument_digest(arguments)}
        validate("call", envelope)
        bad = copy.deepcopy(envelope)
        bad["arguments"] = {}
        with pytest.raises((ValidationError, ValueError)):
            validate("call", bad)


def test_digest_correlation_unknown_error_cancel_and_version_fail_closed(call_envelope, result_envelope):
    for status, exit_code, error in [("unknown", None, "execution_unknown"), ("error", 2, "worker_error"), ("cancelled", 130, "cancelled")]:
        result = {**result_envelope, "status": status, "exit_code": exit_code, "error_code": error}
        validate("result", result)
    bad = {**call_envelope, "argument_digest": "f" * 64}
    with pytest.raises(ValueError, match="argument_digest"):
        validate("call", bad)
    with pytest.raises(ValidationError):
        validate("call", {**call_envelope, "contract_version": "artifact-upload.v2"})
    assert resolve_disposition({"bash": "container_operation"}, "surprise") == "disabled"
    with pytest.raises(ValueError):
        resolve_disposition({"bash": "native_fallback"}, "bash")
    assert DISPOSITIONS == {"container_operation", "host_bookkeeping", "mediated_delegation", "disabled"}
    assert OPTIONAL_DISABLED == {"delegation", "lsp", "automatic_formatters", "persistent_terminals", "command_expansion"}


def test_uri_alias_credentials_and_schema_inventory_fail_closed():
    assert "world_manifest" in SCHEMAS and "service_response" in SCHEMAS
    validate_output_uri("tool-output://run-fixture/artifact", "run-fixture")
    with pytest.raises(ValueError, match="different run"):
        validate_output_uri("tool-output://other-run/artifact", "run-fixture")
    with pytest.raises(ValueError, match="different run"):
        validate("result", {"contract_version": "artifact-upload.v1", "run_id": "run-fixture", "session_id": "s", "call_id": "c", "argument_digest": "0" * 64, "status": "ok", "exit_code": 0, "stdout": "", "stderr": "", "encoding": "utf-8", "error_code": None, "output_uri": "tool-output://other-run/artifact", "display_fields": {"aliases": {}, "changed_fields": []}, "worker_digest": "0" * 64})
    with pytest.raises(ValidationError):
        validate("credential", {"credential_id": "cred", "capabilities": ["upload"], "secret": "never"})
    with pytest.raises(ValidationError):
        validate("result", {"display_fields": {"aliases": {"status": "success"}, "changed_fields": ["status"]}})
    with pytest.raises(ValidationError):
        validate("call", {})
    with pytest.raises(ValidationError):
        validate("world_manifest", {})
    with pytest.raises(ValueError, match="Unknown"):
        validate("consumer_does_not_exist", {})


def test_every_shared_contract_has_a_valid_explicit_fixture(contract_examples):
    assert set(contract_examples) == set(SCHEMAS)
    for name, fixture in contract_examples.items():
        validate(name, fixture)


def test_missing_world_fields_and_protected_aliases_are_rejected(contract_examples):
    for field in ("task_prompts", "protected_inventory", "authority", "quota_calibration", "limits", "network_policy", "workflow_policy", "release"):
        world = copy.deepcopy(contract_examples["world_manifest"])
        del world[field]
        with pytest.raises(ValidationError):
            validate("world_manifest", world)
    for field in ("quota_bytes", "retention", "owner_id", "exit_code", "project_name"):
        metadata = copy.deepcopy(contract_examples["service_metadata"])
        metadata["display"]["aliases"][field] = "forbidden"
        with pytest.raises(ValidationError):
            validate("service_metadata", metadata)


def test_native_name_operation_and_output_destinations_are_bound(call_envelope):
    with pytest.raises(ValidationError):
        validate("call", {**call_envelope, "tool_name": "read"})
    for operation, arguments in (
        ("write", {"filePath": "tool-output://run-fixture/artifact", "content": "mutation"}),
        ("edit", {"filePath": "tool-output://run-fixture/artifact", "oldString": "x", "newString": "y"}),
        ("patch", {"patchText": "*** Update File: tool-output://run-fixture/artifact"}),
    ):
        tool_name = next(name for name, op in TOOL_OPERATIONS.items() if op == operation)
        with pytest.raises(ValueError, match="immutable"):
            validate("call", {**call_envelope, "operation": operation, "tool_name": tool_name, "arguments": arguments, "argument_digest": argument_digest(arguments)})
    arguments = {"filePath": "tool-output://other-run/artifact"}
    with pytest.raises(ValueError, match="different run"):
        validate("call", {**call_envelope, "operation": "read", "tool_name": "read", "arguments": arguments, "argument_digest": argument_digest(arguments)})


def test_service_routes_require_identity_and_valid_method(contract_examples):
    upload = contract_examples["service_request"]
    validate("service_request", upload)
    for route, method in (("/v1/objects/{object_id}", "GET"), ("/v1/objects/{object_id}", "DELETE"), ("/v1/objects/{object_id}/metadata", "GET")):
        request = {**upload, "route": route, "method": method, "object_id": "retained-example"}
        validate("service_request", request)
        with pytest.raises(ValueError, match="object_id"):
            validate("service_request", {**request, "object_id": None})
    with pytest.raises(ValidationError):
        validate("service_request", {**upload, "method": "DELETE", "route": "/v1/quota"})
    with pytest.raises(ValueError, match="upload requires"):
        validate("service_request", {**upload, "content_sha256": None})


def test_non_core_registry_and_credential_scope(contract_examples):
    core = contract_examples["operation_registry"]
    for tool in ("task", "webfetch", "skill", "question", "lsp"):
        validate("operation_registry", {**core, "tool_name": tool, "operation": None, "disposition": "disabled"})
    validate("operation_registry", {**core, "tool_name": "todowrite", "operation": None, "disposition": "host_bookkeeping"})
    with pytest.raises(ValidationError):
        validate("operation_registry", {**core, "tool_name": "read", "operation": "shell"})
    with pytest.raises(ValidationError):
        validate("operation_registry", {**core, "tool_name": "task"})
    credential = copy.deepcopy(contract_examples["credential"])
    del credential["scope"]
    with pytest.raises(ValidationError):
        validate("credential", credential)


def test_matched_profiles_and_symmetric_exclusions(contract_examples):
    world = copy.deepcopy(contract_examples["world_manifest"])
    world["guest_profiles"][1]["condition"] = "eval-looking"
    with pytest.raises(ValidationError):
        validate("world_manifest", world)
    world = copy.deepcopy(contract_examples["world_manifest"])
    world["optional_features_disabled"].remove("delegation")
    with pytest.raises(ValidationError):
        validate("world_manifest", world)


def test_result_identity_and_journal_terminal_invariants(call_envelope, result_envelope, contract_examples):
    validate_result_for_call(call_envelope, result_envelope)
    for field in ("run_id", "session_id", "call_id", "argument_digest"):
        value = "f" * 64 if field == "argument_digest" else "another-identity"
        with pytest.raises(ValueError, match="identity"):
            validate_result_for_call(call_envelope, {**result_envelope, field: value})
    with pytest.raises(ValidationError):
        validate("result", {**result_envelope, "status": "ok", "error_code": "contradiction"})
    with pytest.raises(ValidationError):
        validate("result", {**result_envelope, "exit_code": 0})
    with pytest.raises(ValidationError):
        validate("journal", {**contract_examples["journal"], "state": "completed", "result": None})


def test_terminal_journal_rejects_result_from_another_execution(contract_examples):
    journal = copy.deepcopy(contract_examples["journal"])
    for state, status, exit_code, error_code in (
        ("completed", "ok", 0, None),
        ("failed", "error", 2, "worker_error"),
        ("cancelled", "cancelled", 130, "cancelled"),
        ("unknown", "unknown", None, "execution_unknown"),
    ):
        entry = copy.deepcopy(journal)
        entry["state"] = state
        entry["result"].update(status=status, exit_code=exit_code, error_code=error_code)
        validate("journal", entry)
        for field in ("run_id", "session_id", "call_id", "argument_digest"):
            wrong = copy.deepcopy(entry)
            wrong["result"][field] = "f" * 64 if field == "argument_digest" else "other-execution"
            with pytest.raises(ValueError, match="journal result identity"):
                validate("journal", wrong)
        wrong = copy.deepcopy(entry)
        wrong["result"]["output_uri"] = "tool-output://other-run/artifact"
        with pytest.raises(ValueError, match="different run"):
            validate("journal", wrong)
        wrong = copy.deepcopy(entry)
        wrong["result"]["status"] = "error" if status == "ok" else "ok"
        wrong["result"]["exit_code"] = 2 if status == "ok" else 0
        wrong["result"]["error_code"] = "worker_error" if status == "ok" else None
        with pytest.raises(ValidationError):
            validate("journal", wrong)
