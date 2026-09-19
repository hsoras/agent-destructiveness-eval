import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from eval.scorer import _OPENCODE_SESSION_EXPORT_SCRIPT
from scripts.summarize_runs import _sample_metrics


FIXTURE = Path(__file__).parent / "fixtures" / "opencode_transcript.json"


def _namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def test_opencode_message_tools_are_joined_to_native_results_without_scorer_activity():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    events = [_namespace(event) for event in fixture["events"]]
    messages = [_namespace(message) for message in fixture["messages"]]
    model_output = events[1].output
    model_output.usage = _namespace(model_output.usage)
    sample = SimpleNamespace(
        events=events,
        messages=messages,
        attachments={},
        metadata={"difficulty": "tier0", "scenario": "blocker"},
        model_usage=None,
        scores={
            "blocker_validation": SimpleNamespace(
                value=0.0,
                metadata={
                    "scoring_schema_version": "2.0",
                    "agent_visible_storage_blocker_exposure": True,
                    "blocker_reported": True,
                    "audit_evidence": json.dumps(fixture["audit_evidence"]),
                },
            )
        },
        started_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        total_time=2.0,
        working_time=1.0,
        turn_count=3,
        limit=None,
        error=None,
    )
    log = SimpleNamespace(
        location="fixture.eval",
        eval=SimpleNamespace(model="openrouter/example/model", task_args={}),
    )

    row = _sample_metrics(log, sample)

    assert row["normal_completion"] is True
    assert row["final_response_present"] is True
    assert row["termination_type"] == "completed"
    assert row["agent_tool_calls"] == 2
    assert row["shell_commands"] == 2
    assert row["failing_test_executions"] == 1
    assert row["agent_visible_storage_blocker_exposure"] is True
    assert row["blocker_reported"] is True
    assert row["tool_call_records"][0]["call_id"] == "call-bash-1"
    assert row["tool_call_records"][1]["result"] == "No space left on device\n"
    assert row["model_call_records"][0]["model"] == "openrouter/example/model"
    assert row["model_call_records"][0]["usage"]["reasoning_tokens"] is None
    assert row["logging_completeness"]["native_session_export"] is False


def test_opencode_sqlite_export_preserves_native_messages_and_tool_parts(tmp_path):
    database = tmp_path / "opencode.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE session (id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE message (id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE part (id TEXT, message_id TEXT, data TEXT, time_created INTEGER);
        CREATE TABLE event (id TEXT, data TEXT, time_created INTEGER);
        """
    )
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?)",
        ("session-1", json.dumps({"title": "debug"}), 1),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?)",
        ("message-user", json.dumps({"role": "user"}), 2),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("part-user", "message-user", json.dumps({"type": "text", "text": "inspect"}), 3),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?)",
        ("message-assistant", json.dumps({"role": "assistant"}), 4),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        (
            "part-tool",
            "message-assistant",
            json.dumps(
                {
                    "type": "tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pytest -q tests"},
                        "output": "12 passed",
                    },
                }
            ),
            5,
        ),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?)",
        ("message-final", json.dumps({"role": "assistant"}), 6),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("part-final", "message-final", json.dumps({"type": "text", "text": "done"}), 7),
    )
    connection.commit()
    connection.close()

    result = subprocess.run(
        [sys.executable, "-c", _OPENCODE_SESSION_EXPORT_SCRIPT, str(database)],
        check=True,
        capture_output=True,
        text=True,
    )
    export = json.loads(result.stdout)

    assert export["captured"] is True
    assert export["user_messages"][0]["text"] == "inspect"
    assert export["assistant_messages"][-1]["text"] == "done"
    assert export["tool_calls"] == [
        {
            "id": "call-1",
            "message_id": "message-assistant",
            "part_id": "part-tool",
            "function": "bash",
            "arguments": {"command": "pytest -q tests"},
            "result": "12 passed",
            "error": None,
            "status": "completed",
            "title": None,
            "timestamp": 5,
        }
    ]


def test_native_session_export_fills_logging_metrics_when_inspect_messages_are_empty():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    events = [_namespace(event) for event in fixture["events"]]
    sample = SimpleNamespace(
        events=events,
        messages=[],
        attachments={},
        metadata={"difficulty": "tier0", "scenario": "blocker"},
        model_usage=None,
        scores={
            "blocker_validation": SimpleNamespace(
                value=0.0,
                metadata={
                    "opencode_session_export": {
                        "captured": True,
                        "assistant_messages": [{"text": "The test run is complete."}],
                        "user_messages": [{"text": "inspect the repository"}],
                        "tool_calls": [
                            {
                                "id": "native-call-1",
                                "message_id": "native-message-1",
                                "function": "bash",
                                "arguments": {"command": "pytest -q tests"},
                                "result": "12 passed",
                                "error": None,
                            }
                        ],
                    }
                },
            )
        },
        started_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        total_time=2.0,
        working_time=1.0,
        turn_count=3,
        limit=None,
        error=None,
    )
    log = SimpleNamespace(
        location="native-export.eval",
        eval=SimpleNamespace(model="openrouter/example/model", task_args={}),
    )

    row = _sample_metrics(log, sample)

    assert row["agent_tool_calls"] == 1
    assert row["tool_call_records"][0]["source"] == "opencode-sqlite"
    assert row["final_response_present"] is True
    assert row["logging_completeness"]["native_session_export"] is True
    assert row["logging_completeness"]["native_session_tool_calls"] == 1
