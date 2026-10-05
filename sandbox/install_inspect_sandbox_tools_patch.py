#!/usr/bin/env python3
"""Install lifecycle and retry fixes over pinned inspect_sandbox_tools 1.2.1."""

from __future__ import annotations

from pathlib import Path
import shutil

SOURCE = Path("/usr/local/libexec/inspect-sandbox-tools-patch")
PACKAGE = Path("/usr/local/libexec/inspect-sandbox-tools-package/src/inspect_sandbox_tools")
FILES = (
    "__init__.py",
    "lifecycle.py",
    "_cli/server.py",
    "_remote_tools/_exec_remote/_controller.py",
    "_remote_tools/_exec_remote/_job.py",
)


def main() -> int:
    for relative in FILES:
        source = SOURCE / relative
        target = PACKAGE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o444)
        print(f"installed Inspect remote-execution patch: {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
