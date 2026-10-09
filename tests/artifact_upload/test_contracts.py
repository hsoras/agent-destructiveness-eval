import re

from eval.artifact_upload.contracts import (
    CALL_ENVELOPE,
    CORE_OPERATIONS,
    CONTRACT_VERSION,
    DISPOSITIONS,
    JOURNAL_ENTRY,
    JOURNAL_STATES,
    OPTIONAL_DISABLED,
    RESULT_ENVELOPE,
    SCHEMAS,
)


def _validate(schema, value):
    """Validate the contract subset used here, recursively and dependency-free."""
    if "anyOf" in schema:
        assert any(_valid_alternative(option, value) for option in schema["anyOf"])
        return
    if "not" in schema:
        assert schema["not"] != {}
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    kind = schema.get("type")
    if kind is None:
        return
    if isinstance(kind, list):
        assert any(_matches_type(option, value) for option in kind)
        if value is None:
            return
        kind = next(option for option in kind if _matches_type(option, value))
    assert _matches_type(kind, value)
    if kind == "object":
        assert set(schema.get("required", [])) <= value.keys()
        if schema.get("additionalProperties") is False:
            assert not (value.keys() - schema.get("properties", {}).keys())
        for key, rule in schema.get("properties", {}).items():
            if key in value:
                _validate(rule, value[key])
    elif kind == "array":
        assert len(value) >= schema.get("minItems", 0)
        for item in value:
            _validate(schema["items"], item)
    elif kind == "string":
        assert len(value) >= schema.get("minLength", 0)
        if schema.get("pattern"):
            assert re.fullmatch(schema["pattern"], value)
    elif kind == "integer":
        assert value >= schema.get("minimum", value)


def _matches_type(kind, value):
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[kind]()


def _valid_alternative(schema, value):
    try:
        _validate(schema, value)
        return True
    except (AssertionError, KeyError, TypeError):
        return False


def test_contract_inventory_and_versions(call_envelope, unknown_result):
    assert CONTRACT_VERSION == "artifact-upload.v1"
    assert {"call", "result", "execution_key", "process_handle", "cancellation", "journal", "event",
            "operation_registry", "service_request", "service_metadata", "credential", "world_manifest",
            "profile_manifest", "output_uri", "workflow", "checkpoint", "session_control", "middleware_hook"} <= SCHEMAS.keys()
    _validate(CALL_ENVELOPE, call_envelope)
    _validate(RESULT_ENVELOPE, unknown_result)
    assert unknown_result["status"] == "unknown"


def test_operation_and_journal_contracts_are_explicit():
    assert CORE_OPERATIONS == {"shell", "read", "write", "edit", "patch", "find", "grep"}
    assert DISPOSITIONS == {"container_operation", "host_bookkeeping", "mediated_delegation", "disabled"}
    assert JOURNAL_STATES == ("accepted", "dispatched", "running", "completed", "failed", "cancelled", "unknown")
    _validate(JOURNAL_ENTRY, {
        "execution_key": {"run_id": "r", "session_id": "s", "call_id": "c", "argument_digest": "0" * 64},
        "state": "unknown", "process": None, "result": None, "revision": 1, "updated_at": "2026-10-08T00:00:00Z",
    })
    assert OPTIONAL_DISABLED == {"delegation", "lsp", "automatic_formatters", "persistent_terminals", "command_expansion"}


def test_unknown_tool_is_not_a_native_fallback():
    # Registry resolution has no implicit native disposition.
    registry = {"bash": "container_operation", "todo": "host_bookkeeping"}
    assert registry.get("unrecognized-native-tool") is None


def test_producer_examples_match_every_consumer_schema(contract_examples):
    assert set(contract_examples) == set(SCHEMAS)
    for name, example in contract_examples.items():
        _validate(SCHEMAS[name], example)


def test_nested_contract_shapes_reject_malformed_producer_data(contract_examples):
    malformed = {**contract_examples["call"], "arguments": "command=bad"}
    try:
        _validate(CALL_ENVELOPE, malformed)
    except AssertionError:
        pass
    else:
        raise AssertionError("call arguments must remain an object")
    malformed_result = {**contract_examples["result"], "worker_digest": "short"}
    try:
        _validate(RESULT_ENVELOPE, malformed_result)
    except AssertionError:
        pass
    else:
        raise AssertionError("result digest must be a SHA-256")
