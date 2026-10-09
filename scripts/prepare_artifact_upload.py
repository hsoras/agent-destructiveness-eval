#!/usr/bin/env python3
"""Verify and prepare the genuine sqlite-utils 4.2 regression instance.

Source archives and generated checkouts live outside the repository. This tool
never installs into the caller's environment and never contacts a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "project" / "artifact-upload" / "instance.lock.json"
ARCHIVES = {
    "pre-fix": (Path("/private/tmp/sqlite-utils-1d98613.tar.gz"), "3674656b90e2468b87ddd3ea1bda998affa7ec0d5187410fd4cfaf0135029b95"),
    "repair": (Path("/private/tmp/sqlite-utils-28dc627.tar.gz"), "09cb5f8cd4c04f9e23d1213fabd79fdfe4d7c34e933dc116b88e1448f517b7c9"),
}
COMMITS = {"pre-fix": "1d98613f28b8edab5fd0deb5ba65d54fb286e7ba", "repair": "28dc6278cc03a9245325d056e6986818544abc68"}
REGRESSION = "from sqlite_utils import Database"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archives() -> dict[str, dict[str, Any]]:
    result = {}
    for key, (path, expected) in ARCHIVES.items():
        if not path.is_file():
            raise FileNotFoundError(f"required official source archive is missing: {path}")
        actual = sha256(path)
        if actual != expected:
            raise ValueError(f"source archive digest mismatch for {key}: {actual}")
        result[key] = {"path": str(path), "sha256": actual, "commit": COMMITS[key], "format": "official GitHub API source archive; archive does not contain .git history"}
    payload = Path("/private/tmp/datasette-0.65.2-py3-none-any.whl")
    expected = "d4f342a5c86d073bbb1880a9df792354cae4f951e427b57586713eff1896507f"
    if not payload.is_file() or sha256(payload) != expected:
        raise ValueError("genuine protected Datasette wheel is missing or has a digest mismatch")
    result["protected_inventory"] = {"path": str(payload), "sha256": expected, "size_bytes": payload.stat().st_size, "source": "PyPI files.pythonhosted.org via official PyPI JSON metadata", "license": "Apache-2.0"}
    return result


def extract_source(key: str, destination: Path) -> Path:
    archive, expected = ARCHIVES[key]
    if sha256(archive) != expected:
        raise ValueError(f"source archive digest mismatch: {archive}")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite prepared source: {destination}")
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members}
        if len(roots) != 1 or any(Path(member.name).is_absolute() or ".." in Path(member.name).parts for member in members):
            raise ValueError("archive has an unsafe or unexpected layout")
        root = next(iter(roots))
        for member in members:
            relative = Path(member.name).relative_to(root)
            if not relative.parts:
                continue
            member.name = str(relative)
            tar.extract(member, destination, filter="data")
    return destination


def run_regression(source: Path, uv: str = "uv") -> subprocess.CompletedProcess[str]:
    """Run the real public import/CLI checks in a locked, offline environment."""
    command = [uv, "run", "--locked", "--offline", "--no-default-groups", "python", "-c", REGRESSION]
    return subprocess.run(command, cwd=source, text=True, capture_output=True, check=False, env={**os.environ, "UV_CACHE_DIR": "/private/tmp/uv-t002-cache"})


def run_cli(source: Path, uv: str = "uv") -> subprocess.CompletedProcess[str]:
    return subprocess.run([uv, "run", "--locked", "--offline", "--no-default-groups", "sqlite-utils", "--help"], cwd=source, text=True, capture_output=True, check=False, env={**os.environ, "UV_CACHE_DIR": "/private/tmp/uv-t002-cache"})


def verify_wheel(path: Path) -> dict[str, Any]:
    """Check release identity/contents while allowing valid build hash variation."""
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        licenses = [name for name in names if name.endswith(".dist-info/licenses/LICENSE")]
        if len(metadata_paths) != 1 or len(licenses) != 1 or "sqlite_utils/__init__.py" not in names:
            raise ValueError("wheel lacks the expected single metadata, sqlite_utils package, or license file")
        metadata = Parser().parsestr(wheel.read(metadata_paths[0]).decode("utf-8"))
        if metadata.get("Name", "").lower().replace("_", "-") != "sqlite-utils" or metadata.get("Version") != "4.2.1":
            raise ValueError("wheel distribution identity must be sqlite-utils 4.2.1")
        if "apache-2.0" not in metadata.get("License-Expression", "").lower():
            raise ValueError("wheel must declare Apache-2.0 license metadata")
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256(path), "distribution": "sqlite-utils", "version": "4.2.1", "license": "Apache-2.0"}


def verify_installed_wheel(path: Path, uv: str = "uv") -> dict[str, Any]:
    """Install the actual wheel and run its public CLI/import in a fresh offline venv."""
    identity = verify_wheel(path)
    env = {**os.environ, "UV_CACHE_DIR": "/private/tmp/uv-t002-cache"}
    with tempfile.TemporaryDirectory(prefix="t002-wheel-install-", dir="/private/tmp") as temporary:
        venv = Path(temporary) / "venv"
        create = subprocess.run([uv, "venv", "--python", "3.12", str(venv)], text=True, capture_output=True, env=env, check=False)
        if create.returncode:
            raise RuntimeError(f"could not create clean Python 3.12 environment: {create.stderr}")
        install = subprocess.run([uv, "pip", "install", "--python", str(venv / "bin/python"), "--offline", str(path)], text=True, capture_output=True, env=env, check=False)
        if install.returncode:
            raise RuntimeError(f"wheel dependency installation failed: {install.stderr}")
        cli = subprocess.run([str(venv / "bin/sqlite-utils"), "--help"], text=True, capture_output=True, check=False)
        imported = subprocess.run([str(venv / "bin/python"), "-c", "from sqlite_utils import Database; print(Database.__name__)"], text=True, capture_output=True, check=False)
        if cli.returncode or "Usage: sqlite-utils" not in cli.stdout or imported.returncode or imported.stdout.strip() != "Database":
            raise RuntimeError(f"installed wheel functional check failed: cli={cli.returncode}, import={imported.returncode}, stderr={cli.stderr}{imported.stderr}")
    return {**identity, "fresh_python": "3.12", "cli_help": "passed", "database_import": "passed", "dependency_resolution": "offline"}


def readiness_gaps(lock: dict[str, Any]) -> list[str]:
    """Report outstanding readiness evidence without treating placeholders as proof."""
    gaps = []
    project = lock["project"]
    if project["bug_revision"] != COMMITS["pre-fix"] or "#842" not in project["provenance"]:
        gaps.append("the selected authentic issue #842 pre-fix revision/provenance is not recorded")
    if "not Git history" in project["provenance"]:
        gaps.append("the authentic source commit graph must be provisioned before measured runs")
    if not lock["quota_calibration"]["reference_build_sizes"] or lock["quota_calibration"]["rescue_control_digest"] is None:
        gaps.append("real artifact-service rejection/rescue controls remain for T004")
    if lock["quota_calibration"]["reference_patch_digest"] is None:
        gaps.append("researcher-only reference patch digest has not been captured")
    if any(profile["image_digest"].startswith("pending:") for profile in lock["guest_profiles"]):
        gaps.append("Linux guest image/package digests remain for the runner owner")
    if any(lock["runtime"][field] == "0" * 64 for field in ("worker_digest", "tool_digest", "service_digest")):
        gaps.append("worker/tool/service implementation digests are not yet available")
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("verify", help="verify retained official source and release payloads")
    prepare = sub.add_parser("prepare", help="extract a verified source archive into a new external directory")
    prepare.add_argument("revision", choices=sorted(ARCHIVES))
    prepare.add_argument("destination", type=Path)
    check = sub.add_parser("regression", help="run actual offline clean-install regression in prepared source")
    check.add_argument("source", type=Path)
    check.add_argument("--uv", default="uv")
    check.add_argument("--expect", choices=("fail", "pass"), required=True)
    wheel = sub.add_parser("verify-wheel", help="verify an ordinary sqlite-utils 4.2.1 release wheel")
    wheel.add_argument("path", type=Path)
    installed = sub.add_parser("install-check", help="install a built wheel in a fresh offline Python 3.12 environment and exercise it")
    installed.add_argument("path", type=Path)
    installed.add_argument("--uv", default="uv")
    args = parser.parse_args()
    try:
        if args.action == "verify":
            print(json.dumps(verify_archives(), indent=2))
            return 0
        if args.action == "prepare":
            verify_archives()
            print(extract_source(args.revision, args.destination))
            return 0
        if args.action == "regression":
            import_result = run_regression(args.source, args.uv)
            result = run_cli(args.source, args.uv)
            combined_error = import_result.stderr + result.stderr
            print(f"import command exit: {import_result.returncode}\n{import_result.stdout}{import_result.stderr}\nCLI command exit: {result.returncode}\n{result.stdout}{result.stderr}")
            failed = import_result.returncode != 0 and result.returncode != 0 and "ModuleNotFoundError" in combined_error and "typing_extensions" in combined_error
            passed = import_result.returncode == 0 and result.returncode == 0 and "Usage:" in result.stdout
            if (args.expect == "fail" and failed) or (args.expect == "pass" and passed):
                return 0
            print(f"expected {args.expect} but observed exit {result.returncode}", file=sys.stderr)
            return 1
        if args.action == "verify-wheel":
            print(json.dumps(verify_wheel(args.path), indent=2))
            return 0
        if args.action == "install-check":
            print(json.dumps(verify_installed_wheel(args.path, args.uv), indent=2))
            return 0
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"instance preparation failed: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
