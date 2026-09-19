#!/usr/bin/env python3
"""Warm the pinned OpenCode bundle before starting an eval.

The supported Inspect SWE adapter can fall back to downloading an npm bundle
when its host cache is cold. Running this command once, outside the paid eval,
makes eval-time sandbox setup a cache hit and keeps the model-run path free of
package-resolution network activity.
"""

from __future__ import annotations

import argparse
import shutil

from eval.opencode_config import OPENCODE_VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=OPENCODE_VERSION)
    parser.add_argument(
        "--platform",
        choices=("linux-x64", "linux-arm64"),
        default="linux-x64",
        help="Linux platform used by the Docker sandbox (default: linux-x64)",
    )
    args = parser.parse_args()

    if shutil.which("npm") is None:
        parser.error("npm is required for preflight; install Node.js/npm on the host")
    try:
        from inspect_swe._util.node import create_npm_bundle
    except ImportError as exc:
        parser.error(
            "inspect-swe is required for preflight; install "
            "inspect-swe==0.2.70 in the project environment"
        )
        raise AssertionError from exc

    bundle = create_npm_bundle(
        package="opencode-ai",
        version=args.version,
        platform=args.platform,
        cache_name="opencode-bundles",
        ignore_scripts=True,
    )
    print(
        f"OpenCode {args.version} bundle is warm for {args.platform}: {bundle} "
        "(postinstall runs in the sandbox)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
