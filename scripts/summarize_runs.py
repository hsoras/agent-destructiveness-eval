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
import shlex
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
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
    """Sum complete provider aggregates without turning missing fields into zero."""

    result: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        inspect_name = {
            "cache_read_tokens": "input_tokens_cache_read",
            "cache_write_tokens": "input_tokens_cache_write",
        }.get(field, field)
        values = [_value(item, inspect_name) for item in (usages or {}).values()]
        if not values or any(value is None for value in values):
            result[field] = None
        else:
            result[field] = sum(int(value) for value in values)
    return result


def _sum_cost(usages: Mapping[str, Any] | None) -> float | None:
    """Sum provider-reported costs without treating unavailable data as zero."""

    values = [_value(item, "total_cost") for item in (usages or {}).values()]
    if not values or any(value is None for value in values):
        return None
    return sum(float(value) for value in values)


def _sum_generation_usage(model_events: list[Any]) -> dict[str, int | None]:
    """Sum per-generation provider usage, preserving an unavailable field."""

    if not model_events:
        return {field: None for field in USAGE_FIELDS}
    values = [_usage_values(_model_event_usage(event)) for event in model_events]
    return {
        field: (
            None
            if any(item[field] is None for item in values)
            else sum(int(item[field]) for item in values)
        )
        for field in USAGE_FIELDS
    }


def _sum_generation_cost(model_events: list[Any]) -> float | None:
    """Sum per-generation provider costs, preserving an unavailable field."""

    values = [_value(_model_event_usage(event), "total_cost") for event in model_events]
    if not values or any(value is None for value in values):
        return None
    return sum(float(value) for value in values)


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


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PYTEST_STATUS = re.compile(
    r"(?im)^\s*(?:=+\s*)?(?:(?:\d+\s+(?:failed|passed|errors?|skipped|xfailed|xpassed|warnings?)\s*,?\s*)+"
    r"(?:in\s+[\d.]+\s*s)?|no tests ran(?:\s+in\s+[\d.]+\s*s)?)(?:\s*=+)?\s*$"
)
_PYTEST_INTERRUPTION = re.compile(
    r"(?i)(?:keyboard\s*interrupt|\binterrupted\b|\bsig(?:int|term)\b)"
)
_PYTEST_COLLECTION_ERROR = re.compile(
    r"(?im)(?:error\s+collecting|collection\s+error|importerror\s+while\s+importing\s+test\s+module)"
)
_PYTEST_FAILURE_OUTPUT = re.compile(
    r"(?im)^\s*(?:\d+\s+(?:failed|errors?)\b|FAILED(?:\s|$)|ERRORS?(?:\s|$)|"
    r"=+\s*(?:FAILURES|ERRORS?)\s*=+|E\s+)"
)

_PYTEST_SELECTION_OPTIONS = {
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--lf",
    "--last-failed",
    "--last-failed-no-failures",
    "--keyword",
    "--keyword-expr",
    "--markexpr",
    "--pyargs",
    "--stepwise",
    "--stepwise-skip",
}
_PYTEST_NO_RUN_OPTIONS = {
    "-h",
    "--collect-only",
    "--co",
    "--fixtures",
    "--fixtures-per-test",
    "--help",
    "--setup-only",
    "--setup-plan",
    "--trace",
    "--version",
}
_PYTEST_OPTIONS_WITH_ARGUMENTS = {
    "-c",
    "-p",
    "-r",
    "-W",
    "--basetemp",
    "--capture",
    "--color",
    "--confcutdir",
    "--doctest-glob",
    "--durations",
    "--durations-min",
    "--import-mode",
    "--junit-prefix",
    "--junitxml",
    "--log-cli-date-format",
    "--log-cli-format",
    "--log-cli-level",
    "--log-date-format",
    "--log-file",
    "--log-file-date-format",
    "--log-file-format",
    "--log-file-level",
    "--log-format",
    "--log-level",
    "--maxfail",
    "--override-ini",
    "--pdbcls",
    "--rootdir",
    "--show-capture",
    "--tb",
    "--verbosity",
    "-o",
}


def _shell_fragments(command: str) -> list[str]:
    """Split at unquoted shell control operators without executing the command."""

    fragments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    pending_heredoc: str | None = None
    i = 0

    def emit(end: int) -> None:
        fragment = command[start:end].strip()
        if fragment:
            fragments.append(fragment)

    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            i += 1
            continue
        if char in ";\n":
            emit(i)
            start = i + 1
            i += 1
            if char == "\n" and pending_heredoc is not None:
                while i <= len(command):
                    line_end = command.find("\n", i)
                    if line_end < 0:
                        line_end = len(command)
                    line = command[i:line_end].rstrip("\r")
                    if line == pending_heredoc or line.lstrip("\t") == pending_heredoc:
                        i = line_end + (line_end < len(command))
                        start = i
                        pending_heredoc = None
                        break
                    if line_end == len(command):
                        i = line_end
                        pending_heredoc = None
                        break
                    i = line_end + 1
            continue
        if char == "<" and i + 1 < len(command) and command[i + 1] == "<":
            delimiter_start = i + 2
            if delimiter_start < len(command) and command[delimiter_start] == "-":
                delimiter_start += 1
            while delimiter_start < len(command) and command[delimiter_start] in " \t":
                delimiter_start += 1
            if delimiter_start < len(command) and command[delimiter_start] in {"'", '"'}:
                delimiter_quote = command[delimiter_start]
                delimiter_start += 1
                delimiter_end = command.find(delimiter_quote, delimiter_start)
                if delimiter_end >= 0:
                    pending_heredoc = command[delimiter_start:delimiter_end]
            else:
                delimiter_end = delimiter_start
                while delimiter_end < len(command) and command[delimiter_end] not in " \t\r\n;|&":
                    delimiter_end += 1
                if delimiter_end > delimiter_start:
                    pending_heredoc = command[delimiter_start:delimiter_end]
            i += 2
            continue
        if char == "|":
            emit(i)
            i += 2 if i + 1 < len(command) and command[i + 1] == "|" else 1
            start = i
            continue
        if char == "&":
            # ``2>&1`` and ``&>file`` are redirections, not command boundaries.
            redirection = (
                (i > start and command[i - 1] in "<>")
                or (i + 1 < len(command) and command[i + 1] == ">")
            )
            if not redirection:
                emit(i)
                i += 2 if i + 1 < len(command) and command[i + 1] == "&" else 1
                start = i
                continue
        i += 1
    emit(len(command))
    return fragments


def _shell_words(fragment: str) -> list[str] | None:
    try:
        return shlex.split(fragment, comments=True, posix=True)
    except ValueError:
        # An unterminated quote cannot be attributed conservatively.
        return None


def _pytest_args(fragment: str) -> list[str] | None:
    words = _shell_words(fragment)
    if not words:
        return None

    index = 0
    while index < len(words) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index]):
        index += 1
    if index < len(words) and words[index] == "env":
        index += 1
        while index < len(words):
            if words[index].startswith("-"):
                index += 2 if words[index] in {"-u", "--unset"} else 1
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index]):
                index += 1
                continue
            break
    if index >= len(words):
        return None

    executable = Path(words[index]).name
    if executable == "pytest":
        return words[index + 1 :]
    if (
        re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable)
        and index + 3 <= len(words)
        and words[index + 1 : index + 3] == ["-m", "pytest"]
    ):
        return words[index + 3 :]
    return None


def _pytest_commands(command: str) -> list[str]:
    return [
        fragment
        for fragment in _shell_fragments(command)
        if _pytest_args(fragment) is not None
    ]


def _pytest_invocations(command: str) -> int:
    return len(_pytest_commands(command))


def _pytest_statuses(output: str) -> list[str]:
    return [match.group(0) for match in _PYTEST_STATUS.finditer(_ANSI_ESCAPE.sub("", output))]


def _normalized_pytest_status(status: str) -> str:
    return status.strip().strip("=").strip()


def _is_shell_redirection(word: str) -> bool:
    return bool(re.match(r"^(?:\d+)?(?:>>?|<<|<|>&|<>|&>).*$", word))


def _is_selection_option(word: str) -> bool:
    option = word.split("=", 1)[0]
    if option in _PYTEST_SELECTION_OPTIONS:
        return True
    return word == "-k" or word.startswith("-k") or word == "-m" or word.startswith("-m")


def _is_complete_pytest_invocation(invocation: str) -> bool:
    args = _pytest_args(invocation)
    if args is None:
        return False

    positional: list[str] = []
    end_options = False
    index = 0
    while index < len(args):
        arg = args[index]
        if _is_shell_redirection(arg):
            # ``shlex`` keeps a redirection such as ``2>&1`` as one word.
            if re.fullmatch(r"^(?:\d+)?(?:>>?|<<|<|>&|<>|&>)$", arg):
                index += 2
            else:
                index += 1
            continue
        if end_options:
            positional.append(arg)
            index += 1
            continue
        if arg == "--":
            end_options = True
            index += 1
            continue
        if _is_selection_option(arg) or arg.split("=", 1)[0] in _PYTEST_NO_RUN_OPTIONS:
            return False
        if arg.startswith("-"):
            option = arg.split("=", 1)[0]
            if option in _PYTEST_OPTIONS_WITH_ARGUMENTS and "=" not in arg:
                index += 2
            else:
                index += 1
            continue
        positional.append(arg)
        index += 1

    for target in positional:
        normalized = target.rstrip("/")
        if normalized in {"", ".", "./", "tests", "./tests"}:
            continue
        # Any other positional target can be a module, file, node ID, or glob.
        # Treat it conservatively as a selected run.
        return False
    return True


def _is_complete_test_command(command: str) -> bool:
    invocations = _pytest_commands(command)
    if not invocations:
        return False
    return all(_is_complete_pytest_invocation(invocation) for invocation in invocations)


def _result_text(event: Any) -> str:
    result = getattr(event, "result", "")
    return result if isinstance(result, str) else repr(result)


def _pytest_failed(
    event: Any,
    output: str | None = None,
    *,
    invocation_index: int | None = None,
) -> bool | None:
    result = getattr(event, "result", None)
    if getattr(event, "error", None) is not None:
        return True
    text = _result_text(event) if output is None else output
    statuses = _pytest_statuses(text)
    status = None
    if invocation_index is not None and invocation_index < len(statuses):
        status = statuses[invocation_index]
    elif statuses:
        status = statuses[-1]
    if status is not None:
        lowered = _normalized_pytest_status(status).lower()
        if (
            "no tests ran" in lowered
            or re.search(r"\b(?:failed|errors?|xfailed|xpassed)\b", lowered)
        ):
            return True
        if "passed" in lowered:
            return False
    if invocation_index is not None and statuses:
        # A short-circuited ``&&`` chain may never have executed a later
        # invocation. Do not attribute the earlier invocation's failure to it.
        return None
    if _PYTEST_INTERRUPTION.search(text) or _PYTEST_COLLECTION_ERROR.search(text):
        return True
    if _PYTEST_FAILURE_OUTPUT.search(text) or re.search(
        r"\bno tests ran\b|(?:non-zero|exit (?:code|status)).*\b[1-9]\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"\b\d+\s+passed\b", text, re.IGNORECASE):
        return False
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return result != 0
    return None


def _pytest_green(
    event: Any,
    command: str,
    output: str | None = None,
    *,
    ambiguous: bool = False,
    interrupted: bool = False,
) -> bool:
    invocations = _pytest_commands(command)
    if ambiguous or not _is_complete_test_command(command):
        return False
    text = _result_text(event) if output is None else output
    result = getattr(event, "result", None)
    if interrupted or getattr(event, "error", None) is not None:
        return False
    if isinstance(result, (int, float)) and not isinstance(result, bool) and result != 0:
        return False
    if _PYTEST_INTERRUPTION.search(text) or _PYTEST_COLLECTION_ERROR.search(text):
        return False
    if _PYTEST_FAILURE_OUTPUT.search(text) or re.search(
        r"\bno tests ran\b|(?:non-zero|exit (?:code|status)).*\b[1-9]\b",
        text,
        re.IGNORECASE,
    ):
        return False
    statuses = _pytest_statuses(text)
    if len(statuses) != len(invocations):
        return False
    return all(
        "passed" in _normalized_pytest_status(status).lower()
        and re.search(
            r"\bin\s+[\d.]+\s*s\s*$",
            _normalized_pytest_status(status),
            re.IGNORECASE,
        )
        is not None
        and not re.search(
            r"\b(?:failed|errors?|no tests ran|xfailed|xpassed)\b",
            _normalized_pytest_status(status),
            re.IGNORECASE,
        )
        for status in statuses
    )


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
            previous = active.get(instance)
            if previous is not None:
                previous_output = "".join(previous["outputs"])
                previous_invocations = _pytest_commands(previous["command"])
                previous_statuses = _pytest_statuses(previous_output)
                if (
                    previous_invocations
                    and len(previous_statuses) < len(previous_invocations)
                    and not _PYTEST_INTERRUPTION.search(previous_output)
                    and not _PYTEST_COLLECTION_ERROR.search(previous_output)
                ):
                    # A new command arrived before the prior pytest output had
                    # a terminal summary. Its later output cannot be assigned
                    # confidently to either command.
                    previous["boundary_ambiguous"] = True
            record = {
                "index": index,
                "event": event,
                "command": command,
                "outputs": [output],
                "boundary_ambiguous": bool(
                    previous is not None
                    and previous.get("boundary_ambiguous")
                ),
                "interrupted": False,
            }
            commands.append(record)
            active[instance] = record
        elif action in {"read", "interrupt"}:
            record = active.get(instance)
            if record is not None:
                record["outputs"].append(output)
                record["event"] = event
                if action == "interrupt":
                    record["interrupted"] = True

    for record in commands:
        record["output"] = "".join(record.pop("outputs"))
        statuses = _pytest_statuses(record["output"])
        invocations = _pytest_commands(record["command"])
        record["ambiguous"] = bool(
            record.pop("boundary_ambiguous", False)
            or (invocations and len(statuses) > len(invocations))
        )
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
        for invocation_index in range(_pytest_invocations(command)):
            failed = _pytest_failed(
                event,
                output,
                invocation_index=invocation_index,
            )
            test_runs.append(
                (
                    index,
                    event,
                    command,
                    failed,
                    _pytest_green(
                        event,
                        command,
                        output,
                        ambiguous=record["ambiguous"],
                        interrupted=record["interrupted"],
                    ),
                )
            )

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
    total_cost_usd = _sum_cost(getattr(sample, "model_usage", None))
    agent_model_events = [
        event
        for _, event in agent_events
        if getattr(event, "event", None) == "model"
    ]
    if all(value is None for value in usage.values()):
        usage = _sum_generation_usage(agent_model_events)
    if total_cost_usd is None:
        total_cost_usd = _sum_generation_cost(agent_model_events)

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

    complete_suite_commands = [
        record
        for record in shell_commands
        if _is_complete_test_command(record["command"])
    ]
    ambiguous_suite_commands = [
        record for record in complete_suite_commands if record["ambiguous"]
    ]
    if first_green_index is not None:
        first_green_reason = None
    elif ambiguous_suite_commands:
        first_green_reason = "ambiguous_shell_output_or_event_boundary"
    elif complete_suite_commands:
        first_green_reason = "no_complete_suite_pass_observed"
    else:
        first_green_reason = "no_complete_suite_run_observed"

    if first_green_index is not None:
        pre_green_models = [
            event
            for index, event in agent_events
            if index <= first_green_index and getattr(event, "event", None) == "model"
        ]
        pre_green_usage = _sum_generation_usage(pre_green_models)
    else:
        pre_green_usage = {field: None for field in USAGE_FIELDS}

    if pre_green_usage["reasoning_tokens"] is None:
        reasoning_to_green_reason = (
            "provider_reasoning_usage_unavailable_before_first_green"
            if first_green_index is not None
            else first_green_reason
        )
    else:
        reasoning_to_green_reason = None

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
        post_green_models = [
            event
            for index, event in agent_events
            if index > first_green_index and getattr(event, "event", None) == "model"
        ]
        post_green_usage = _sum_generation_usage(post_green_models)
        if not post_green_models:
            post_green_usage = {field: 0 for field in USAGE_FIELDS}

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
        "total_cost_usd": total_cost_usd,
        "reasoning_tokens_to_first_green": pre_green_usage["reasoning_tokens"],
        "reasoning_tokens_to_first_green_reason": reasoning_to_green_reason,
        "total_reasoning_tokens": usage["reasoning_tokens"],
        "first_green_detection_reason": first_green_reason,
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
        "time_to_first_green": _seconds_since(
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


def _distribution_stats(values: Sequence[int | float]) -> dict[str, Any]:
    """Return descriptive statistics without manufacturing a mode."""

    if not values:
        return {
            "mean": None,
            "median": None,
            "mode": None,
            "q1": None,
            "q3": None,
            "iqr": None,
        }

    counts = Counter(values)
    highest_count = max(counts.values())
    modes = sorted(value for value, count in counts.items() if count == highest_count)
    mode: int | float | list[int | float] | None
    if highest_count == 1:
        mode = None
    elif len(modes) == 1:
        mode = modes[0]
    else:
        mode = modes

    if len(values) == 1:
        q1 = q3 = values[0]
    else:
        quartiles = statistics.quantiles(values, n=4, method="inclusive")
        q1, q3 = quartiles[0], quartiles[2]
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "mode": mode,
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
    }


def _print_summary(rows: list[dict[str, Any]]) -> None:
    """Print primary reasoning metrics before secondary trajectory diagnostics."""

    def describe(tier_rows: list[dict[str, Any]], field: str) -> None:
        values = [row[field] for row in tier_rows if row[field] is not None]
        missing = len(tier_rows) - len(values)
        if not values:
            print(
                f"{field}: values=[] mean=n/a median=n/a mode=n/a "
                f"q1=n/a q3=n/a iqr=n/a range=n/a missing={missing}"
            )
            return
        stats = _distribution_stats(values)
        print(
            f"{field}: values={values} mean={stats['mean']} "
            f"median={stats['median']} mode={stats['mode'] if stats['mode'] is not None else 'n/a'} "
            f"q1={stats['q1']} q3={stats['q3']} iqr={stats['iqr']} "
            f"range={min(values)}..{max(values)} missing={missing}"
        )

    by_tier: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_tier.setdefault(str(row["tier"]), []).append(row)
    primary_fields = (
        "reasoning_tokens_to_first_green",
        "total_reasoning_tokens",
        "post_green_reasoning_tokens",
        "time_to_first_green",
    )
    secondary_fields = (
        "total_tokens",
        "total_cost_usd",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "agent_tool_calls",
        "test_executions",
        "post_green_tool_calls",
        "elapsed_seconds",
    )
    for tier, tier_rows in by_tier.items():
        print(tier)
        print("-" * len(tier))
        print(f"runs:                {len(tier_rows)}")
        print(f"success:             {sum(row['success'] for row in tier_rows)}/{len(tier_rows)}")
        print(f"normal submit:       {sum(row['normal_submit'] for row in tier_rows)}/{len(tier_rows)}")
        print(f"capped/limit runs:   {sum(row['termination_type'] == 'limit' for row in tier_rows)}")
        print("primary metrics (provider-reported):")
        for field in primary_fields:
            describe(tier_rows, field)
        print("secondary metrics:")
        for field in secondary_fields:
            describe(tier_rows, field)
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
