"""Remove evaluator storage and tool-server instrumentation from agent jobs."""

from __future__ import annotations

import os


_EVALUATOR_ONLY_ENV = {
    "INSPECT_SANDBOX_TOOLS_DIR",
    "INSPECT_SANDBOX_TOOLS_EVENT_LOG",
    "INSPECT_REMOTE_EXEC_INSTRUMENTATION",
    "STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR",
}


def agent_subprocess_environment(
    inherited: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment without evaluator-only storage cues.

    The assigned SQLite secure-delete mode is intentionally retained because
    the migration runner reads it when configuring its database connection.
    """
    result = {**(os.environ if inherited is None else inherited), **(extra or {})}
    for name in tuple(result):
        if (
            name.startswith("SQLITE_MIGRATION_")
            and name != "SQLITE_MIGRATION_SECURE_DELETE"
        ):
            result.pop(name, None)
    for name in _EVALUATOR_ONLY_ENV:
        result.pop(name, None)
    return result
