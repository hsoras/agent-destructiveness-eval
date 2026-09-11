"""Extract trajectory, termination, and usage metrics from Inspect logs.

Inspect's ``.eval`` file is the canonical trajectory. This module reads the
structured ``ToolEvent``, ``SandboxEvent``, ``ModelEvent``, and span records
instead of treating the rendered terminal UI as the source of truth.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log


USAGE_FIELDS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _log_paths(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.eval")))
        elif path.suffix == ".eval":
            paths.append(path)
    return paths


def _value(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_values(usage: Any) -> dict[str, int | None]:
    """Normalize Inspect's ModelUsage field names for analysis output."""

    if usage is None:
        return {field: None for field in USAGE_FIELDS}
    values: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        inspect_name = {
            "cache_read_tokens": "input_tokens_cache_read",
            "cache_write_tokens": "input_tokens_cache_write",
        }.get(field, field)
        raw = _value(usage, inspect_name)
        values[field] = int(raw) if raw is not None else None
    return values


def _sum_usage(usages: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Sum usage without turning unavailable provider fields into zero."""

    result: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        inspect_name = {
            "cache_read_tokens": "input_tokens_cache_read",
            "cache_write_tokens": "input_tokens_cache_write",
        }.get(field, field)
        values = [_value(item, inspect_name) for item in (usages or {}).values()]
        present = [int(value) for value in values if value is not None]
        result[field] = sum(present) if present else None
    return result


def _add_usage(total: dict[str, int | None], usage: Any) -> None:
    values = _usage_values(usage)
    for field in USAGE_FIELDS:
        value = values[field]
        if value is not None:
            total[field] = (total[field] or 0) + value


def _span_maps(events: list[Any]) -> tuple[dict[str, str | None], dict[str, str]]:
    parents: dict[str, str | None] = {}
    types: dict[str, str] = {}
    for event in events:
        if getattr(event, "event", None) != "span_begin":
            continue
        span_id = getattr(event, "span_id", None) or getattr(event, "id", None)
        if span_id is None:
            continue
        span_id = str(span_id)
        parents[span_id] = getattr(event, "parent_id", None)
        types[span_id] = str(getattr(event, "type", ""))
    return parents, types


def _has_span_type(
    event: Any,
    wanted: str,
    parents: Mapping[str, str | None],
    types: Mapping[str, str],
) -> bool:
    span_id = getattr(event, "span_id", None)
    seen: set[str] = set()
    while span_id is not None and str(span_id) not in seen:
        span_id = str(span_id)
        seen.add(span_id)
        if types.get(span_id) == wanted:
            return True
        span_id = parents.get(span_id)
    return False


def _is_scorer_event(event: Any, parents: Mapping[str, str | None], types: Mapping[str, str]) -> bool:
    return _has_span_type(event, "scorer", parents, types)


def _is_agent_event(event: Any, parents: Mapping[str, str | None], types: Mapping[str, str]) -> bool:
    if _is_scorer_event(event, parents, types):
        return False
    if types:
        return _has_span_type(event, "agent", parents, types)
    return getattr(event, "event", None) in {"tool", "model"}


def _resolve_attachment(sample: Any, value: Any) -> Any:
    """Resolve Inspect's attachment URI used for large tool arguments."""

    attachments = getattr(sample, "attachments", None) or {}
    current = value
    for _ in range(3):
        if not isinstance(current, str) or not current.startswith("attachment://"):
            return current
        current = attachments.get(current.removeprefix("attachment://"))
        if current is None:
            return value
        if isinstance(current, Mapping):
            current = current.get("content", current.get("text", current))
    return current


def _tool_arguments(event: Any, sample: Any) -> dict[str, Any]:
    arguments = getattr(event, "arguments", None) or {}
    if not isinstance(arguments, Mapping):
        return {}
    return {str(key): _resolve_attachment(sample, value) for key, value in arguments.items()}


def _shell_command(event: Any, sample: Any) -> str:
    value = _tool_arguments(event, sample).get("input")
    return value if isinstance(value, str) else ""


_PYTEST_COMMAND = re.compile(
    r"(?:^|[;&|]+\s*)(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?pytest(?:\s|$)"
)


def _pytest_invocations(command: str) -> int:
    return len(_PYTEST_COMMAND.findall(command))


def _is_complete_test_command(command: str) -> bool:
    if not _pytest_invocations(command):
        return False
    # A command naming one test module is useful, but is not a complete suite.
    return not re.search(r"(?:^|[\s/])test_[A-Za-z0-9_.-]+\.py(?:\s|$)", command)


def _result_text(event: Any) -> str:
    result = getattr(event, "result", "")
    return result if isinstance(result, str) else repr(result)


def _pytest_failed(event: Any, output: str | None = None) -> bool | None:
    result = getattr(event, "result", None)
    if isinstance(result, (int, float)) and not isinstance(result, bool) and result != 0:
        return True
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return False
    if getattr(event, "error", None) is not None:
        return True
    text = _result_text(event) if output is None else output
    if (
        re.search(r"\b\d+\s+failed\b", text, re.IGNORECASE)
        or re.search(r"\bFAILED\b", text)
        or re.search(r"\b\d+\s+errors?\b", text, re.IGNORECASE)
        or re.search(r"\bERRORS?\b", text)
        or re.search(r"\bno tests ran\b", text, re.IGNORECASE)
        or re.search(r"(?:non-zero|exit (?:code|status)).*\b[1-9]\b", text, re.IGNORECASE)
    ):
        return True
    if re.search(r"\b\d+\s+passed\b", text, re.IGNORECASE):
        return False
    return None


def _pytest_green(event: Any, command: str, output: str | None = None) -> bool:
    if not _is_complete_test_command(command) or _pytest_failed(event, output) is not False:
        return False
    text = _result_text(event) if output is None else output
    return bool(re.search(r"\b\d+\s+passed\b", text, re.IGNORECASE))


def _shell_commands(agent_tools: list[tuple[int, Any]], sample: Any) -> list[dict[str, Any]]:
    """Associate delayed ``bash_session(action=read)`` output with its command.

    Inspect's interactive shell may return only a short prefix from
    ``type_submit`` and place the pytest summary in one or more later
    ``read`` calls. Keep the command's start index for trajectory ordering,
    while aggregating all output received before the next submitted command in
    that shell instance.
    """

    commands: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}
    for index, event in agent_tools:
        if getattr(event, "function", None) != "bash_session":
            continue
        args = _tool_arguments(event, sample)
        action = str(args.get("action", ""))
        instance = str(args.get("instance", "default"))
        output = _result_text(event)

        if action == "type_submit":
            command = args.get("input")
            if not isinstance(command, str) or not command.strip():
                continue
            record = {
                "index": index,
                "event": event,
                "command": command,
                "outputs": [output],
            }
            commands.append(record)
            active[instance] = record
        elif action in {"read", "interrupt"}:
            record = active.get(instance)
            if record is not None:
                record["outputs"].append(output)
                record["event"] = event

    for record in commands:
        record["output"] = "".join(record.pop("outputs"))
    return commands


def _normalize_repo_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip().strip("`'\".,:;()")
    prefix = "/home/dev/streamstats/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    elif path == "/home/dev/streamstats":
        return None
    elif path.startswith("/"):
        return None
    path = path.removeprefix("./").rstrip("/")
    if not path or path in {".", ".."}:
        return None
    return path


def _looks_like_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith(".") or "." in name


def _paths_in_text(text: str) -> set[str]:
    paths: set[str] = set()
    for match in re.findall(r"/home/dev/streamstats(?:/[^\s`'\"):,;]+)?", text):
        normalized = _normalize_repo_path(match)
        if normalized and _looks_like_file(normalized):
            paths.add(normalized)
    return paths


def _paths_in_shell_command(command: str) -> set[str]:
    paths: set[str] = set()
    for match in re.findall(
        r"(?:README(?:\.md)?|pyproject\.toml|(?:src|tests|data)/[A-Za-z0-9_./-]+)",
        command,
    ):
        normalized = _normalize_repo_path(match)
        if normalized and _looks_like_file(normalized):
            paths.add(normalized)
    return paths


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _event_timestamp(event: Any) -> datetime | None:
    return _as_datetime(getattr(event, "timestamp", None))


def _seconds_since(start: Any, timestamp: Any) -> float | None:
    start = _as_datetime(start)
    timestamp = _as_datetime(timestamp)
    if start is None or timestamp is None:
        return None
    return max(0.0, (timestamp - start).total_seconds())


def _model_event_usage(event: Any) -> Any:
    output = getattr(event, "output", None)
    usage = getattr(output, "usage", None)
    return usage if usage is not None else getattr(event, "usage", None)


def _score_metadata(sample: Any) -> dict[str, Any]:
    score = next(iter((getattr(sample, "scores", None) or {}).values()), None)
    return getattr(score, "metadata", None) or {}


def _sample_metrics(log: Any, sample: Any) -> dict[str, Any]:
    events = list(getattr(sample, "events", None) or [])
    parents, types = _span_maps(events)
    agent_events = [
        (index, event)
        for index, event in enumerate(events)
        if _is_agent_event(event, parents, types)
    ]
    agent_tools = [
        (index, event)
        for index, event in agent_events
        if getattr(event, "event", None) == "tool"
    ]
    agent_sandbox = [
        (index, event)
        for index, event in enumerate(events)
        if getattr(event, "event", None) == "sandbox"
        and _is_agent_event(event, parents, types)
    ]
    scorer_sandbox = [
        event
        for event in events
        if getattr(event, "event", None) == "sandbox"
        and _is_scorer_event(event, parents, types)
    ]

    tool_names = [str(getattr(event, "function", "")) for _, event in agent_tools]
    shell_tools = [
        (index, event, _shell_command(event, sample))
        for index, event in agent_tools
        if getattr(event, "function", None) == "bash_session"
    ]
    shell_commands = _shell_commands(agent_tools, sample)
    test_runs: list[tuple[int, Any, str, bool | None, bool]] = []
    for record in shell_commands:
        index = record["index"]
        event = record["event"]
        command = record["command"]
        output = record["output"]
        for _ in range(_pytest_invocations(command)):
            failed = _pytest_failed(event, output)
            test_runs.append((index, event, command, failed, _pytest_green(event, command, output)))

    source_reads: set[str] = set()
    edited_paths: set[str] = set()
    for _, event in agent_tools:
        function = getattr(event, "function", None)
        args = _tool_arguments(event, sample)
        command = str(args.get("command", ""))
        path = _normalize_repo_path(args.get("path"))
        result = _result_text(event)
        if function == "text_editor":
            if command in {"view", "read", "list"}:
                if path and _looks_like_file(path):
                    source_reads.add(path)
                source_reads.update(_paths_in_text(result))
            elif command and getattr(event, "error", None) is None:
                if path:
                    edited_paths.add(path)
                source_reads.update(_paths_in_text(result))
        elif function == "bash_session":
            source_reads.update(_paths_in_shell_command(_shell_command(event, sample)))
            source_reads.update(_paths_in_text(result))

    for _, event in agent_sandbox:
        action = getattr(event, "action", None)
        path = _normalize_repo_path(getattr(event, "file", None))
        if action == "read_file" and path:
            source_reads.add(path)
        if action == "write_file" and path:
            edited_paths.add(path)

    metadata = _score_metadata(sample)
    score = next(iter((getattr(sample, "scores", None) or {}).values()), None)
    success = bool(score and float(getattr(score, "value", 0.0)) >= 1.0)

    usage = _sum_usage(getattr(sample, "model_usage", None))
    if all(value is None for value in usage.values()):
        usage = {field: None for field in USAGE_FIELDS}
        for _, event in agent_events:
            if getattr(event, "event", None) == "model":
                _add_usage(usage, _model_event_usage(event))

    first_edit_index: int | None = None
    first_edit_time: datetime | None = None
    for index, event in agent_tools:
        if getattr(event, "function", None) != "text_editor":
            continue
        args = _tool_arguments(event, sample)
        if str(args.get("command", "")) in {"view", "read", "list"}:
            continue
        if getattr(event, "error", None) is None:
            first_edit_index = index
            first_edit_time = _event_timestamp(event)
            break

    first_green_index: int | None = None
    first_green_time: datetime | None = None
    for index, event, _, _, green in test_runs:
        if green:
            first_green_index = index
            first_green_time = _event_timestamp(event)
            break

    edit_test_cycles = 0
    edit_pending = False
    test_indices = {index for index, *_ in test_runs}
    for index, event in sorted(agent_tools + agent_sandbox, key=lambda item: item[0]):
        if getattr(event, "event", None) == "tool":
            if getattr(event, "function", None) == "text_editor":
                args = _tool_arguments(event, sample)
                if str(args.get("command", "")) not in {"view", "read", "list"}:
                    edit_pending = True
        elif getattr(event, "event", None) == "sandbox":
            if getattr(event, "action", None) == "write_file":
                edit_pending = True
        if edit_pending and index in test_indices:
            edit_test_cycles += 1
            edit_pending = False

    scorer_test_runs = [
        event
        for event in scorer_sandbox
        if getattr(event, "action", None) == "exec"
        and _pytest_invocations(str(getattr(event, "cmd", "")))
    ]
    scorer_failures = [event for event in scorer_test_runs if _pytest_failed(event)]

    normal_submit = any(
        getattr(event, "function", None) == "submit" for _, event in agent_tools
    )
    limit = getattr(sample, "limit", None)
    if normal_submit:
        termination_type = "submit"
        termination_reason = None
    elif limit is not None:
        termination_type = "limit"
        termination_reason = getattr(limit, "reason", None)
    elif getattr(sample, "error", None) is not None:
        termination_type = "error"
        termination_reason = str(getattr(sample, "error"))
    else:
        termination_type = "completed"
        termination_reason = None

    post_green_tool_calls = None
    post_green_usage = {field: None for field in USAGE_FIELDS}
    if first_green_index is not None:
        post_green_tool_calls = sum(index > first_green_index for index, _ in agent_tools)
        for index, event in agent_events:
            if index > first_green_index and getattr(event, "event", None) == "model":
                _add_usage(post_green_usage, _model_event_usage(event))

    task_args = getattr(getattr(log, "eval", None), "task_args", None) or {}
    sample_metadata = getattr(sample, "metadata", None) or {}
    order_seed = sample_metadata.get("order_seed", task_args.get("order_seed"))
    defect_set = sample_metadata.get("defect_set")
    if isinstance(defect_set, (list, tuple)):
        defect_set = ",".join(str(defect) for defect in defect_set)
    score_metadata = metadata
    return {
        "log": str(getattr(log, "location", "")),
        "model": getattr(getattr(log, "eval", None), "model", None),
        "tier": sample_metadata.get("difficulty", task_args.get("difficulty")),
        "defect_set": defect_set,
        "order_seed": order_seed,
        "success": success,
        "normal_submit": normal_submit,
        "termination_type": termination_type,
        "termination_reason": termination_reason,
        "limit_type": getattr(limit, "type", None) if limit is not None else None,
        "input_tokens": usage["input_tokens"],
        "cache_read_tokens": usage["cache_read_tokens"],
        "cache_write_tokens": usage["cache_write_tokens"],
        "output_tokens": usage["output_tokens"],
        "reasoning_tokens": usage["reasoning_tokens"],
        "total_tokens": usage["total_tokens"],
        "agent_tool_calls": len(agent_tools),
        "tool_calls": len(agent_tools),
        "tool_names": ",".join(tool_names),
        "shell_commands": len(shell_commands),
        "shell_tool_calls": len(shell_tools),
        "test_executions": len(test_runs),
        "failing_test_executions": sum(failed is True for _, _, _, failed, _ in test_runs),
        "unclassified_test_executions": sum(
            failed is None for _, _, _, failed, _ in test_runs
        ),
        "edit_test_cycles": edit_test_cycles,
        "files_modified": score_metadata.get("modified_file_count"),
        "modified_file_names": ",".join(score_metadata.get("modified_files", [])),
        "agent_files_edited": len(edited_paths),
        "files_inspected": len(source_reads),
        "time_to_first_edit_seconds": _seconds_since(
            getattr(sample, "started_at", None), first_edit_time
        ),
        "time_to_first_green_suite_seconds": _seconds_since(
            getattr(sample, "started_at", None), first_green_time
        ),
        "post_green_tool_calls": post_green_tool_calls,
        "post_green_input_tokens": post_green_usage["input_tokens"],
        "post_green_cache_read_tokens": post_green_usage["cache_read_tokens"],
        "post_green_output_tokens": post_green_usage["output_tokens"],
        "post_green_reasoning_tokens": post_green_usage["reasoning_tokens"],
        "post_green_total_tokens": post_green_usage["total_tokens"],
        "scorer_test_executions": len(scorer_test_runs),
        "scorer_failing_test_executions": len(scorer_failures),
        "visible_tests_passed": score_metadata.get("visible_tests_passed"),
        "hidden_tests_passed": score_metadata.get("hidden_tests_passed"),
        "elapsed_seconds": getattr(sample, "total_time", None),
        "working_seconds": getattr(sample, "working_time", None),
        "turns": getattr(sample, "turn_count", None),
    }


def read_metrics(inputs: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _log_paths(inputs):
        log = read_eval_log(path)
        for sample in getattr(log, "samples", None) or []:
            rows.append(_sample_metrics(log, sample))
    return rows


def _print_summary(rows: list[dict[str, Any]]) -> None:
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_tier.setdefault(str(row["tier"]), []).append(row)
    numeric_fields = (
        "total_tokens",
        "agent_tool_calls",
        "test_executions",
        "time_to_first_green_suite_seconds",
        "post_green_tool_calls",
        "elapsed_seconds",
    )
    for tier, tier_rows in by_tier.items():
        print(tier)
        print("-" * len(tier))
        print(f"runs:                {len(tier_rows)}")
        print(f"success:             {sum(row['success'] for row in tier_rows)}/{len(tier_rows)}")
        print(f"normal submit:       {sum(row['normal_submit'] for row in tier_rows)}/{len(tier_rows)}")
        for field in numeric_fields:
            values = [row[field] for row in tier_rows if row[field] is not None]
            label = field.replace("_", " ")
            value = statistics.median(values) if values else "n/a"
            print(f"median {label}:".ljust(21) + f"{value}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", help="Inspect .eval files or log directories")
    parser.add_argument("--output", type=Path, help="write extracted rows as JSON")
    parser.add_argument("--csv", type=Path, help="write extracted rows as CSV")
    args = parser.parse_args()

    rows = read_metrics(args.logs)
    _print_summary(rows)
    if args.output:
        args.output.write_text(json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(rows[0]) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
