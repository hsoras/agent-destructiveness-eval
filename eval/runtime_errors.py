"""Identify terminal scaffold and remote-execution infrastructure errors."""

import re


_MISSING_REMOTE_JOB = re.compile(r"\bNo job found with pid\s+\d+\b", re.IGNORECASE)


def missing_remote_job_error(error: object) -> str | None:
    """Return the missing-job diagnostic when Inspect loses an exec_remote job.

    Inspect may store this as a top-level exception or inside a sample/native
    session error. Match the specific server message so ordinary command
    failures remain task errors.
    """
    if error is None:
        return None
    message = str(error)
    match = _MISSING_REMOTE_JOB.search(message)
    return match.group(0) if match else None


def native_session_error(export: object) -> str | None:
    if not isinstance(export, dict):
        return None
    assistants = [
        row for row in export.get("messages", [])
        if isinstance(row, dict)
        and isinstance(row.get("data"), dict)
        and row["data"].get("role") == "assistant"
    ]
    if not assistants:
        return None
    error = assistants[-1]["data"].get("error")
    if not error:
        return None
    if isinstance(error, dict):
        data = error.get("data", {})
        if isinstance(data, dict) and data.get("message"):
            return f"{error.get('name', 'runtime_error')}: {data['message']}"
    return str(error)
