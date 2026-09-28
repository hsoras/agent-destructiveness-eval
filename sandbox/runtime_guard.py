#!/usr/bin/env python3
"""Keep framework-created temporary paths inside the trusted storage boundary."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


FRAMEWORK_TMP = Path("/var/tmp")
SANDBOX_SERVICES = FRAMEWORK_TMP / "sandbox-services"
AGENT_TMP = FRAMEWORK_TMP / "agent-tmp"
AGENT_OPENCODE_TMP = AGENT_TMP / "opencode"


def _ensure_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    try:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)


def harden() -> None:
    # Inspect/OpenCode may initialize these after the base service starts. The
    # parent is a small root-only filesystem, so even a brief framework mode
    # change cannot provide bulk storage; this loop restores the intended
    # permissions after initialization and restart as well.
    os.chown(FRAMEWORK_TMP, 0, 0)
    os.chmod(FRAMEWORK_TMP, 0o755)
    _ensure_directory(SANDBOX_SERVICES, uid=0, gid=0, mode=0o700)
    _ensure_directory(AGENT_TMP, uid=1000, gid=1000, mode=0o1777)
    _ensure_directory(AGENT_OPENCODE_TMP, uid=1000, gid=1000, mode=0o700)


def main() -> int:
    if os.geteuid() != 0:
        return 1
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        harden()
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
