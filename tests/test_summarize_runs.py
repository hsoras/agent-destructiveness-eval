from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.summarize_runs import _sample_metrics


def _span(span_id, span_type, parent_id=None):
    return SimpleNamespace(
        event="span_begin",
        id=span_id,
        span_id=span_id,
        parent_id=parent_id,
        type=span_type,
    )


def _model(span_id, timestamp, **usage):
    return SimpleNamespace(
        event="model",
        span_id=span_id,
        timestamp=timestamp,
        output=SimpleNamespace(usage=SimpleNamespace(**usage)),
    )


def _tool(span_id, function, timestamp, arguments, result):
    return SimpleNamespace(
        event="tool",
        span_id=span_id,
        function=function,
        timestamp=timestamp,
        arguments=arguments,
        result=result,
        error=None,
    )


def _sample(events, start, *, limit=None):
    return SimpleNamespace(
        events=events,
        attachments={},
        metadata={"difficulty": "tier3"},
        model_usage={
            "test-model": SimpleNamespace(
                input_tokens=100,
                input_tokens_cache_read=40,
                input_tokens_cache_write=0,
                output_tokens=20,
                reasoning_tokens=5,
                total_tokens=160,
            )
        },
        scores={
            "repository_repaired": SimpleNamespace(
                value=1.0,
                metadata={
                    "visible_tests_passed": True,
                    "hidden_tests_passed": True,
                    "modified_files": ["src/foo.py"],
                    "modified_file_count": 1,
                },
            )
        },
        started_at=start,
        total_time=3.0,
        working_time=2.0,
        turn_count=4,
        limit=limit,
        error=None,
    )


def _log():
    return SimpleNamespace(
        location="example.eval",
        eval=SimpleNamespace(model="test-model", task_args={"difficulty": "all"}),
    )


def test_metrics_separate_agent_activity_from_scorer_and_track_submit():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    agent = "agent"
    scorer = "scorer"
    events = [
        _span(agent, "agent"),
        _model(agent, start, input_tokens=10, output_tokens=4, total_tokens=14),
        _tool(agent, "bash_session", start, {"action": "type_submit", "input": "pytest -q tests"}, "1 failed, 11 passed"),
        _tool(agent, "text_editor", start, {"command": "view", "path": "src/foo.py"}, "cat -n src/foo.py"),
        _tool(agent, "text_editor", start, {"command": "str_replace", "path": "src/foo.py"}, "edited"),
        _tool(agent, "bash_session", start, {"action": "type_submit", "input": "pytest -q tests"}, "12 passed in 0.1s"),
        _model(agent, start, input_tokens=20, output_tokens=8, total_tokens=28),
        _tool(agent, "submit", start, {"answer": "done"}, "submitted"),
        _span(scorer, "scorer"),
        SimpleNamespace(event="sandbox", span_id=scorer, action="exec", cmd="pytest -q tests", result=0),
        SimpleNamespace(event="sandbox", span_id=scorer, action="exec", cmd="pytest -q /tmp/hidden.py", result=0),
    ]

    row = _sample_metrics(_log(), _sample(events, start))

    assert row["success"] is True
    assert row["normal_submit"] is True
    assert row["termination_type"] == "submit"
    assert row["test_executions"] == 2
    assert row["failing_test_executions"] == 1
    assert row["scorer_test_executions"] == 2
    assert row["scorer_failing_test_executions"] == 0
    assert row["files_inspected"] == 1
    assert row["agent_files_edited"] == 1
    assert row["edit_test_cycles"] == 1
    assert row["post_green_tool_calls"] == 1
    assert row["post_green_total_tokens"] == 28


def test_metrics_report_token_limit_separately_from_success():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _span("agent", "agent"),
        _model("agent", start, input_tokens=10, output_tokens=4, total_tokens=14),
    ]
    limit = SimpleNamespace(type="token", reason="Token limit exceeded")

    row = _sample_metrics(_log(), _sample(events, start, limit=limit))

    assert row["success"] is True
    assert row["normal_submit"] is False
    assert row["termination_type"] == "limit"
    assert row["limit_type"] == "token"


def test_metrics_join_delayed_bash_reads_to_the_submitted_test():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    agent = "agent"
    events = [
        _span(agent, "agent"),
        _tool(
            agent,
            "bash_session",
            start,
            {"action": "type_submit", "input": "pytest -q tests"},
            "============================= test session starts =============================\n",
        ),
        _tool(
            agent,
            "bash_session",
            start,
            {"action": "read"},
            "1 failed, 11 passed in 0.2s\nroot@container:/home/dev/streamstats# ",
        ),
    ]

    row = _sample_metrics(_log(), _sample(events, start))

    assert row["shell_commands"] == 1
    assert row["shell_tool_calls"] == 2
    assert row["test_executions"] == 1
    assert row["failing_test_executions"] == 1
    assert row["unclassified_test_executions"] == 0
