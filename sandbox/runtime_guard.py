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
SANDBOX_TOOLS_DIR = FRAMEWORK_TMP / ".da7be258e003d428"
SANDBOX_TOOLS_CLI = SANDBOX_TOOLS_DIR / "inspect-sandbox-tools"
INSPECT_SERVER_DIR = FRAMEWORK_TMP / "sandbox-tools"


def _ensure_instrumented_tools() -> None:
    """Seed the pinned, patched Python CLI before Inspect checks installation."""
    if os.environ.get("INSPECT_REMOTE_EXEC_INSTRUMENTATION") != "1":
        return
    SANDBOX_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(SANDBOX_TOOLS_DIR, 0, 0)
    os.chmod(SANDBOX_TOOLS_DIR, 0o700)
    if not SANDBOX_TOOLS_CLI.exists():
        SANDBOX_TOOLS_CLI.write_text(
            "#!/usr/local/bin/python3\n"
            "import sys\n"
            "import os\n"
            f"os.environ['INSPECT_SANDBOX_TOOLS_DIR'] = {str(INSPECT_SERVER_DIR)!r}\n"
            "package_path = '/usr/local/libexec/inspect-sandbox-tools-package/src'\n"
            "sys.path.insert(0, package_path)\n"
            "os.environ['PYTHONPATH'] = package_path + os.pathsep + os.environ.get('PYTHONPATH', '')\n"
            "from inspect_sandbox_tools._cli.main import main\n"
            "main()\n",
            encoding="utf-8",
        )
    os.chown(SANDBOX_TOOLS_CLI, 0, 0)
    os.chmod(SANDBOX_TOOLS_CLI, 0o500)


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
    _ensure_instrumented_tools()
    if os.environ.get("SANDBOX_CONDITION") == "development":
        # The revised container places OpenCode temp state under /home/dev on
        # the bounded project filesystem. Keep framework /var/tmp root-only.
        if AGENT_TMP.exists():
            _ensure_directory(AGENT_TMP, uid=0, gid=0, mode=0o700)
    else:
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
