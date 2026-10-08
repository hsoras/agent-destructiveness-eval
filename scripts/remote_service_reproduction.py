#!/usr/bin/env python3
"""Run an unpaid OpenCode/Inspect remote-job diagnostic with local mock replies."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pilot import (
    _DockerEventCapture,
    cleanup_sqlite_migration_home,
    cleanup_opencode_runtime_disk,
    prepare_sqlite_migration_home,
    prepare_opencode_runtime_disk,
    resolve_inspect_command,
    sqlite_migration_capacity,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-dir",
        default=None,
        help="fresh Inspect and lifecycle-evidence output directory",
    )
    parser.add_argument(
        "--idle-control",
        action="store_true",
        help="run the 305-second concurrent idle-job control instead of migration",
    )
    args = parser.parse_args()
    log_dir = Path(
        args.log_dir
        or (
            "logs/inspect-remote-service-idle-control"
            if args.idle_control
            else "logs/inspect-remote-service-reproduction"
        )
    ).resolve()
    diagnostics_dir = log_dir / "remote-exec-diagnostics"
    inspect_command = resolve_inspect_command(REPO_ROOT)
    if inspect_command is None:
        parser.error("run `uv sync --frozen` to prepare the pinned Inspect runtime")

    environment = os.environ.copy()
    environment["INSPECT_EVAL_CACHE"] = "false"
    environment["SQLITE_MIGRATION_ARTIFACT_ROOT"] = str(
        Path(
            environment.get(
                "SQLITE_MIGRATION_ARTIFACT_ROOT",
                REPO_ROOT / ".scenario" / "sqlite-migration-artifacts",
            )
        ).resolve()
    )
    environment["SQLITE_MIGRATION_CONDITION"] = "clean-capacity"
    environment["SQLITE_MIGRATION_SECURE_DELETE"] = "off"
    environment["SQLITE_MIGRATION_CAPACITY_BYTES"] = sqlite_migration_capacity(
        "clean-capacity", "off"
    )
    environment["STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR"] = str(diagnostics_dir)

    label, volume = prepare_sqlite_migration_home(
        REPO_ROOT, "clean-capacity", "off"
    )
    runtime_label, runtime_volume, runtime_capacity, runtime_free = prepare_opencode_runtime_disk(REPO_ROOT)
    environment["SQLITE_MIGRATION_HOME_VOLUME"] = volume
    environment["SQLITE_MIGRATION_RUNTIME_VOLUME"] = runtime_volume
    environment["SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES"] = str(runtime_capacity)
    environment["SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES"] = str(runtime_free)
    environment["SQLITE_MIGRATION_RUNTIME_IMAGE_MIB"] = "64"
    command = [
        *inspect_command,
        "eval",
        "eval/remote_service_diagnostic.py@"
        + ("remote_service_idle_control" if args.idle_control else "remote_service_migration"),
        "--model",
        "mockllm/model",
        "--max-retries",
        "1",
        "--timeout",
        "600",
        "--attempt-timeout",
        "300",
        "--max-tokens",
        "8192",
        "--turn-limit",
        "100",
        "--log-dir",
        str(log_dir),
        "--cache=false",
        "--ctl-server",
        "true",
    ]
    try:
        print("$ " + " ".join(command), flush=True)
        with _DockerEventCapture(diagnostics_dir):
            return subprocess.run(command, check=False, env=environment).returncode
    finally:
        try:
            cleanup_opencode_runtime_disk(REPO_ROOT, runtime_label)
        finally:
            cleanup_sqlite_migration_home(REPO_ROOT, label, volume)


if __name__ == "__main__":
    raise SystemExit(main())
