#!/usr/bin/env python3
"""Build a nonmeasurement OpenCode runtime in a new private temporary tree.

No credentials or inference are used. Dependencies require network access.
Run with a downloaded Bun 1.3.13 binary and the recovered upstream source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "8ba2a9171597262df9d19516c82a5e14f18f5c63"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if any(p in ("node_modules", ".git", "dist", "anomalyco-opencode-8ba2a91") for p in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            data = b"link:" + os.readlink(path).encode()
        elif path.is_file():
            data = bytes.fromhex(sha256(path))
        else:
            continue
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--bun", type=Path, required=True)
    parser.add_argument("--models-snapshot", type=Path, help="Previously downloaded public models.dev api.json")
    parser.add_argument("--plugin-seed", type=Path, default=REPO / ".scenario/migration-readiness/opencode-canary-plugin-1.14.41")
    args = parser.parse_args()
    if json.loads((args.source / "packages/opencode/package.json").read_text())["version"] != "1.14.41":
        raise RuntimeError("recovered source must have package version 1.14.41")
    root = Path(tempfile.mkdtemp(prefix="artifact-runtime-", dir="/private/tmp"))
    print(root, flush=True)
    source = root / "source"
    shutil.copytree(args.source, source, symlinks=True, ignore=shutil.ignore_patterns("node_modules", ".git", "dist", ".env", ".env.local", "anomalyco-opencode-8ba2a91"))
    receipt = {"schema_version": 1, "artifact_class": "development-nonmeasurement", "source_commit": SOURCE_COMMIT,
               "source_identity_basis": "recovered source supplied by operator; no git metadata",
               "source_tree_sha256": tree_hash(source), "source_tree_hash_algorithm": "sorted path NUL (file SHA256 bytes or link:target) NUL; excludes node_modules,.git,dist,nested archive copy",
               "version": "1.14.41", "channel": "latest", "plugin_seed": str(args.plugin_seed.resolve()),
               "bun": str(args.bun.resolve()), "bun_sha256": sha256(args.bun), "patches": [], "status": "building"}
    receipt["build_env"] = {"OPENCODE_VERSION": "1.14.41", "OPENCODE_CHANNEL": "latest", "OPENCODE_RELEASE": None}
    receipt["build_flags"] = ["--single", "--skip-install"]
    receipt["embedded_web_ui"] = True
    receipt["bun_version"] = subprocess.check_output([str(args.bun.resolve()), "--version"], text=True).strip()
    if receipt["bun_version"] != "1.3.13":
        raise RuntimeError("original build requires Bun 1.3.13")
    receipt_path = root / "build-receipt.json"
    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    save()
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
    env.update(HOME=str(root / "home"), BUN_INSTALL_CACHE_DIR=str(root / "bun-cache"),
               PATH=str(args.bun.resolve().parent) + os.pathsep + env.get("PATH", ""),
               OPENCODE_VERSION="1.14.41", OPENCODE_CHANNEL="latest")
    (root / "home").mkdir()
    def run(command):
        with (root / "build.log").open("a") as log:
            log.write("\n" + repr(command) + "\n")
            log.flush()
            subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    try:
        for name in ("native-stdout-drain.patch", "build-input-repair.patch"):
            patch = REPO / "runtime/artifact_upload_host/native_patch" / name
            run(["patch", "-p1", "--batch", "-i", str(patch)])
            receipt["patches"].append({"name": name, "sha256": sha256(patch)})
        receipt["patched_source_tree_sha256"] = tree_hash(source)
        receipt["repaired_lock_sha256"] = sha256(source / "bun.lock")
        snapshot = root / "models-dev-api.json"
        if args.models_snapshot:
            shutil.copy2(args.models_snapshot, snapshot)
        else:
            urllib.request.urlretrieve("https://models.dev/api.json", snapshot)
        json.loads(snapshot.read_text())
        receipt["models_snapshot"] = {"url": "https://models.dev/api.json", "sha256": sha256(snapshot), "path": str(snapshot)}
        env["MODELS_DEV_API_JSON"] = str(snapshot)
        save()
        run([str(args.bun.resolve()), "install", "--frozen-lockfile"])
        if sha256(source / "bun.lock") != receipt["repaired_lock_sha256"]:
            raise RuntimeError("frozen installation modified repaired lockfile")
        command = [str(args.bun.resolve()), "run", "--cwd", "packages/opencode", "script/build.ts", "--single", "--skip-install"]
        receipt["build_command"] = command
        save()
        run(command)
        binary = source / "packages/opencode/dist/opencode-darwin-arm64/bin/opencode"
        version = subprocess.check_output([str(binary), "--version"], env=env, text=True).strip()
        if version != "1.14.41":
            raise RuntimeError(f"unexpected version: {version}")
        receipt.update(binary=str(binary), binary_sha256=sha256(binary), status="built")
    except Exception as exc:
        receipt.update(status="failed", failure=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        save()
    print(receipt_path, flush=True)


if __name__ == "__main__":
    main()
