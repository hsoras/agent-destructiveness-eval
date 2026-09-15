from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.summarize_runs import (
    _is_complete_test_command,
    _pytest_commands,
    _pytest_failed,
    _pytest_green,
    _distribution_stats,
    _print_summary,
    _sample_metrics,
)


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
                total_cost=0.0125,
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
        _model(agent, start, input_tokens=10, output_tokens=4, reasoning_tokens=7, total_tokens=14),
        _tool(agent, "bash_session", start, {"action": "type_submit", "input": "pytest -q tests"}, "1 failed, 11 passed"),
        _tool(agent, "text_editor", start, {"command": "view", "path": "src/foo.py"}, "cat -n src/foo.py"),
        _tool(agent, "text_editor", start, {"command": "str_replace", "path": "src/foo.py"}, "edited"),
        _tool(agent, "bash_session", start, {"action": "type_submit", "input": "pytest -q tests"}, "12 passed in 0.1s"),
        _model(agent, start, input_tokens=20, output_tokens=8, reasoning_tokens=11, total_tokens=28),
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
    assert row["total_cost_usd"] == 0.0125
    assert row["reasoning_tokens_to_first_green"] == 7
    assert row["total_reasoning_tokens"] == 5
    assert row["post_green_reasoning_tokens"] == 11
    assert row["first_green_detection_reason"] is None


def test_metrics_report_cost_limit_separately_from_success():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _span("agent", "agent"),
        _model("agent", start, input_tokens=10, output_tokens=4, total_tokens=14),
    ]
    limit = SimpleNamespace(type="cost", reason="Cost limit exceeded")

    row = _sample_metrics(_log(), _sample(events, start, limit=limit))

    assert row["success"] is True
    assert row["normal_submit"] is False
    assert row["termination_type"] == "limit"
    assert row["limit_type"] == "cost"


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


def test_first_green_handles_compound_edits_and_python_heredoc():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _span("agent", "agent"),
        _model("agent", start, input_tokens=10, output_tokens=4, reasoning_tokens=13, total_tokens=27),
        _tool(
            "agent",
            "bash_session",
            start,
            {
                "action": "type_submit",
                "input": "python - <<'PY'\nfrom pathlib import Path\nPath('src/foo.py').touch()\nPY\npytest --tb=short -q tests",
            },
            "12 passed in 0.1s",
        ),
    ]
    row = _sample_metrics(_log(), _sample(events, start))

    assert row["test_executions"] == 1
    assert row["reasoning_tokens_to_first_green"] == 13
    assert row["first_green_detection_reason"] is None
    assert _is_complete_test_command("python -c 'edit()'; pytest -q tests")
    assert _is_complete_test_command("python -m pytest -q tests")
    assert _pytest_commands("python - <<'PY'\npytest -q\nPY\npytest -q tests") == [
        "pytest -q tests"
    ]
    assert not _is_complete_test_command("pytest -q tests/test_pipeline.py")
    assert not _is_complete_test_command("pytest -q tests -k replay")


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest",
        "pytest -q",
        "cd /home/dev/streamstats && python -m pytest -q",
        "python -m pytest -q 2>&1 | tail -15",
        "cd /home/dev/streamstats && python -m pytest -q --tb=short 2>&1 | tail -15",
        "python -m pytest -q --tb short -r a 2>&1 | tail -15",
        "python -m pytest -q && git diff --stat",
    ],
)
def test_complete_suite_parser_supports_shell_compounds(command):
    assert _pytest_commands(command)
    assert _is_complete_test_command(command)


def test_pytest_parser_ignores_quoted_and_non_executed_occurrences():
    assert _pytest_commands("echo 'pytest -q tests'") == []
    assert _pytest_commands('python -c "print(\'pytest -q tests\')"') == []
    assert _pytest_commands("printf '%s' 'pytest -q tests'") == []
    assert not _is_complete_test_command("echo 'pytest -q tests'")


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q tests/test_pipeline.py",
        "pytest -q tests/test_pipeline.py::test_resume",
        "pytest -q tests -k resume",
        "pytest -q tests -m integration",
        "pytest -q --deselect tests/test_pipeline.py::test_resume",
        "pytest -q --lf",
        "pytest -q --collect-only",
    ],
)
def test_targeted_or_nonexecuting_pytest_runs_are_not_complete_suites(command):
    assert not _is_complete_test_command(command)


def test_failed_pytest_piped_through_tail_is_not_green_even_when_tail_succeeds():
    event = SimpleNamespace(result=0, error=None)
    output = "1 failed, 14 passed in 0.2s\n"

    assert _pytest_failed(event, output) is True
    assert not _pytest_green(
        event,
        "python -m pytest -q 2>&1 | tail -15",
        output,
    )


def test_green_requires_every_pytest_invocation_to_have_its_own_passing_summary():
    event = SimpleNamespace(result=0, error=None)
    command = "python -m pytest -q && python -m pytest -q && git diff --stat"
    passing = "15 passed in 0.1s\n15 passed in 0.1s\n"
    one_failed = "1 failed, 14 passed in 0.1s\n15 passed in 0.1s\n"

    assert len(_pytest_commands(command)) == 2
    assert _pytest_green(event, command, passing)
    assert not _pytest_green(event, command, one_failed)
    assert _pytest_failed(event, one_failed, invocation_index=0) is True
    assert _pytest_failed(event, one_failed, invocation_index=1) is False


@pytest.mark.parametrize(
    "output",
    [
        "KeyboardInterrupt\n15 passed in 0.1s\n",
        "ERROR collecting tests/test_pipeline.py\n15 passed in 0.1s\n",
        "1 xfailed in 0.1s\n",
        "no tests ran in 0.1s\n",
        "README excerpt:\n15 passed\n",
    ],
)
def test_incomplete_or_inconclusive_output_is_not_green(output):
    event = SimpleNamespace(result=0, error=None)
    assert not _pytest_green(event, "pytest -q", output)


def test_first_green_accounting_stops_at_the_corresponding_tool_completion():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _span("agent", "agent"),
        _model("agent", start, reasoning_tokens=7),
        _tool(
            "agent",
            "bash_session",
            start + timedelta(seconds=2),
            {"action": "type_submit", "input": "cd /home/dev/streamstats && pytest -q"},
            "15 passed in 0.1s\n",
        ),
        _model("agent", start + timedelta(seconds=3), reasoning_tokens=11),
    ]

    row = _sample_metrics(_log(), _sample(events, start))

    assert row["reasoning_tokens_to_first_green"] == 7
    assert row["post_green_reasoning_tokens"] == 11
    assert row["time_to_first_green"] == 2.0


def test_ambiguous_old_output_does_not_create_a_false_first_green():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _span("agent", "agent"),
        _model("agent", start, input_tokens=10, output_tokens=4, reasoning_tokens=13, total_tokens=27),
        _tool(
            "agent",
            "bash_session",
            start,
            {"action": "type_submit", "input": "pytest -q tests"},
            "1 failed, 11 passed in 0.1s",
        ),
        _tool(
            "agent",
            "bash_session",
            start,
            {"action": "type_submit", "input": "pytest -q tests"},
            "1 failed, 11 passed in 0.1s\n12 passed in 0.1s",
        ),
    ]
    row = _sample_metrics(_log(), _sample(events, start))

    assert row["reasoning_tokens_to_first_green"] is None
    assert row["first_green_detection_reason"] == "ambiguous_shell_output_or_event_boundary"


def test_delayed_output_without_a_terminal_boundary_stays_ambiguous():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        _span("agent", "agent"),
        _model("agent", start, input_tokens=10, output_tokens=4, reasoning_tokens=13, total_tokens=27),
        _tool(
            "agent",
            "bash_session",
            start,
            {"action": "type_submit", "input": "pytest -q"},
            "============================= test session starts =============================\n",
        ),
        _tool(
            "agent",
            "bash_session",
            start + timedelta(seconds=1),
            {"action": "type_submit", "input": "pytest -q"},
            "15 passed in 0.1s\n",
        ),
    ]

    row = _sample_metrics(_log(), _sample(events, start))

    assert row["reasoning_tokens_to_first_green"] is None
    assert row["first_green_detection_reason"] == "ambiguous_shell_output_or_event_boundary"


def test_distribution_stats_report_mean_median_mode_and_iqr():
    stats = _distribution_stats([1, 2, 2, 4])

    assert stats == {
        "mean": 2.25,
        "median": 2.0,
        "mode": 2,
        "q1": 1.75,
        "q3": 2.5,
        "iqr": 0.75,
    }
    assert _distribution_stats([1, 2, 3])["mode"] is None


def test_summary_prints_requested_time_and_usage_distributions(capsys):
    rows = [
        {
            "tier": "tier1",
            "success": True,
            "normal_submit": True,
            "termination_type": "submit",
            "reasoning_tokens_to_first_green": value,
            "total_reasoning_tokens": value + 10,
            "post_green_reasoning_tokens": 0,
            "time_to_first_green": float(value),
            "total_tokens": value * 100,
            "total_cost_usd": value / 100,
            "elapsed_seconds": float(value),
            **{
                field: 0
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "agent_tool_calls",
                    "test_executions",
                    "post_green_tool_calls",
                )
            },
        }
        for value in (1, 2, 2, 4)
    ]

    _print_summary(rows)
    output = capsys.readouterr().out

    assert "reasoning_tokens_to_first_green: values=[1, 2, 2, 4] mean=2.25 median=2.0 mode=2" in output
    assert "total_reasoning_tokens: values=[11, 12, 12, 14] mean=12.25 median=12.0 mode=12" in output
    assert "time_to_first_green: values=[1.0, 2.0, 2.0, 4.0] mean=2.25 median=2.0 mode=2.0" in output
    assert "total_tokens: values=[100, 200, 200, 400] mean=225 median=200.0 mode=200" in output
    assert "elapsed_seconds: values=[1.0, 2.0, 2.0, 4.0] mean=2.25 median=2.0 mode=2.0" in output
    assert "iqr=0.75" in output
