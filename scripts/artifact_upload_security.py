#!/usr/bin/env python3
"""Run the T016 focused security/recovery acceptance offline and write a coverage/pin bundle.

``--offline`` means no provider-backed call: provider credentials are stripped from the environment and the model is
a deterministic loopback provider inside the tests. Docker, the pinned OpenCode binary and the T002 fixtures are required
prerequisites; a missing one fails the run (it is never reported as a skipped pass). ``--output`` must be absent or empty.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/artifact_upload/test_security_recovery.py"
NATIVE_SHA256 = "d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6"
WORKER_IMAGE = "sha256:5405e4bf893e24e507137448b8baa0eb32e32717d2078c327a6224f6d659d45a"
OWNED = (TEST, "scripts/artifact_upload_security.py")
SOURCES = ("eval/artifact_upload/host_adapter.py", "eval/artifact_upload/broker.py", "eval/artifact_upload/output_store.py",
           "eval/artifact_upload/recording.py", "eval/artifact_upload/discovery.py", "eval/artifact_upload/lifecycle.py",
           "sandbox/artifact_upload/service.py", "sandbox/artifact_upload/storage.py", "sandbox/compose.artifact-upload.yaml")
PROVIDER_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def common_root() -> Path:
    out = run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT).stdout.strip()
    return (ROOT / out).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", required=True, help="no provider-backed calls (required)")
    parser.add_argument("--output", required=True, type=Path, help="new or empty evidence directory")
    args = parser.parse_args(argv)
    out = args.output.resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"refusing non-empty output {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    source = Path(os.environ.get("ARTIFACT_MIGRATION_SOURCE", common_root()))
    binary = Path(os.environ.get("OPENCODE_PINNED_BINARY", source / ".scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode"))
    problems = []
    if not binary.is_file() or sha256(binary) != NATIVE_SHA256:
        problems.append(f"pinned OpenCode binary missing or digest mismatch: {binary}")
    image = run(["docker", "image", "inspect", WORKER_IMAGE, "--format", "{{.Id}}"])
    if image.returncode or image.stdout.strip() != WORKER_IMAGE:
        problems.append("common worker image missing or changed")
    if problems:
        (out / "summary.json").write_text(json.dumps({"passed": False, "prerequisite_problems": problems}, indent=1))
        print("\n".join(problems), file=sys.stderr)
        return 3

    env = {k: v for k, v in os.environ.items() if k not in PROVIDER_ENV}
    env.update(ARTIFACT_MIGRATION_SOURCE=str(source), MIGRATION_ARTIFACT_DIR=str(out / "artifacts"), PYTHONDONTWRITEBYTECODE="1")
    started = time.time()
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={out / 'junit.xml'}", TEST],
                          cwd=ROOT, env=env, capture_output=True, text=True)
    (out / "pytest.log").write_text(proc.stdout + proc.stderr)
    (out / "pytest.exit").write_text(f"{proc.returncode}\n")
    coverage_file = out / "artifacts" / "coverage.json"
    coverage = json.loads(coverage_file.read_text()) if coverage_file.is_file() else {"missing": ["coverage.json not written"], "coverage": {}, "limitations": []}
    summary = {
        "passed": proc.returncode == 0 and not coverage["missing"],
        "pytest_exit": proc.returncode, "wall_seconds": round(time.time() - started, 1), "offline_no_provider_calls": True,
        "missing_mandatory_cases": coverage["missing"], "executed_cases": sorted(coverage["coverage"]), "blocked_cases": coverage.get("blocked", {}),
        "limitations": coverage["limitations"],
        "pins": {"native_binary_sha256": NATIVE_SHA256, "worker_image": WORKER_IMAGE, "head": run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip(),
                 "dirty": bool(run(["git", "status", "--porcelain", "--", *OWNED], cwd=ROOT).stdout.strip()),
                 "owned_sha256": {p: sha256(ROOT / p) for p in OWNED}, "exercised_sources_sha256": {p: sha256(ROOT / p) for p in SOURCES}},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps({k: summary[k] for k in ("passed", "pytest_exit", "missing_mandatory_cases")}))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
