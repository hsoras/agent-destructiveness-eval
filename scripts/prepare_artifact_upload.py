#!/usr/bin/env python3
"""Validate the artifact-upload instance lock and report readiness gates.

This script deliberately does not invent missing historical, release, or quota
evidence. A schema-valid candidate lock can still be blocked from use.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "project" / "artifact-upload" / "instance.lock.json"
sys.path.insert(0, str(ROOT))
from eval.artifact_upload.contracts import validate  # noqa: E402


UNSELECTED = "UNSELECTED"


def readiness_gaps(lock: dict[str, Any]) -> list[str]:
    """Return evidence gates that prevent this candidate from being run."""
    gaps: list[str] = []

    def contains_unresolved(value: Any) -> bool:
        if isinstance(value, str):
            return value == UNSELECTED or value.startswith("UNSELECTED:") or value == "0" * 64
        if isinstance(value, dict):
            return any(contains_unresolved(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_unresolved(item) for item in value)
        return False

    if contains_unresolved(lock):
        gaps.append("manifest contains unresolved selections or placeholder digests")
    project = lock["project"]
    if project["bug_revision"] == UNSELECTED:
        gaps.append("no authentic pre-fix bug revision or focused regression is selected")
    if project["provenance"].startswith("Candidate only:"):
        gaps.append("candidate provenance is not historical defect evidence")

    release = lock["release"]
    if release["recipe"] == UNSELECTED or release["artifact_provenance"] == UNSELECTED:
        gaps.append("release recipe and genuine payload provenance are not frozen")

    calibration = lock["quota_calibration"]
    required_calibration = (
        "quota_bytes", "reference_build_digest", "reference_patch_digest",
        "environment_digest", "initial_usage_bytes", "free_allowance_bytes",
        "rescue_control_digest",
    )
    missing = [field for field in required_calibration if calibration[field] is None]
    if missing or len(calibration["reference_build_sizes"]) < 2:
        gaps.append("reference-build/quota/rescue calibration is incomplete")
    if not lock["protected_inventory"]:
        gaps.append("no genuine protected release inventory or ownership evidence is configured")
    if lock["authority"]["classification"] != "supported":
        gaps.append("ordinary owner/retention context does not establish the authorization boundary")
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        validate("world_manifest", lock)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"INVALID instance lock: {exc}", file=sys.stderr)
        return 1

    gaps = readiness_gaps(lock)
    if gaps:
        print(f"NOT READY: {args.lock}")
        for gap in gaps:
            print(f"- {gap}")
        return 2
    print(f"READY: {args.lock}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
