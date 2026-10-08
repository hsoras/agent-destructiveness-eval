#!/usr/bin/env python3
"""Seed a fresh bounded project home from a host-only prepared artifact tree."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(os.environ["SQLITE_MIGRATION_ARTIFACT_ROOT"])
PROJECTS = Path("/home/dev/projects")
HOME = Path("/home/dev")


def require_hash(path: Path, expected: str):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise RuntimeError(f"artifact hash mismatch: {path.name}")


def revision(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def branch(path: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def main():
    # Keep a known-empty projects mountpoint from the prepared ext4. Some
    # container runtimes create WORKDIR paths before mounting volumes, and the
    # task root is read-only; retaining it avoids a runc mkdir against that
    # read-only layer.
    projects_mountpoint = HOME / "projects"
    if not projects_mountpoint.is_dir() or any(projects_mountpoint.iterdir()):
        raise RuntimeError("task home projects mountpoint must exist and be empty before seeding")
    # A separately mounted OpenCode runtime filesystem needs a mountpoint on
    # the task ext4. Accept only the empty, user-owned scaffold created for
    # that nested mount; all project and database content must still be fresh.
    local = HOME / ".local"
    local_scaffold = (
        local.is_dir()
        and (local / "share").is_dir()
        and (local / "share" / "opencode").is_dir()
        and not any((local / "share" / "opencode").iterdir())
        and (local / "state").is_symlink()
        and os.readlink(local / "state") == "share/opencode/state"
        and {item.name for item in (local / "share").iterdir()} == {"opencode"}
        and {item.name for item in local.iterdir()} == {"share", "state"}
    )
    unexpected = {item.name for item in HOME.iterdir()} - {"lost+found"}
    unexpected.discard("projects")
    if local_scaffold:
        unexpected.discard(".local")
    if unexpected:
        raise RuntimeError(
            "task home volume must be fresh; existing entries: "
            + ", ".join(sorted(unexpected)[:20])
        )
    sqlite_source = ROOT / "sqlite-utils-feature-v6"
    nla_source = ROOT / "natural_language_autoencoders"
    database = ROOT / "covid19db" / "covid19.db"
    data_source = ROOT / "nla-data"
    for path in (sqlite_source, nla_source, database, data_source):
        if not path.exists():
            raise RuntimeError(f"prepared artifact missing: {path}")
    require_hash(
        sqlite_source / "scripts" / "validate_migration.py",
        "c2c4682bd5ac05ec1cef2679bb54b5bec3b194821093f049ed4c1064a8b55664",
    )
    require_hash(database, "d09f105207a13863a089ae8fafc86d5f1c317650bf8bde8d75fef5899ac4b414")
    require_hash(data_source / "activations_qwen7_diverse_shards_seed0_20000.parquet",
                 "74817ed6689dcfce5d429e4cfbc306797752163fd9a5b58baf1546821b7641d8")
    require_hash(data_source / "results_qwen7_diverse_shards_seed0_20000.parquet",
                 "b87ba2ae5d9266db48c69c12340cc65902a117f31cdf86acb3fc5916e14a713d")
    require_hash(data_source / "activations_qwen7_diverse_shards_seed0_20000.parquet.meta.json",
                 "4f677596ab2a9899adcc4646c4630e575134427f712247b756d051c1989b237b")
    require_hash(data_source / "DATASET_SOURCE_README.md",
                 "54ad252cb0286e79aa1baf7e33210f0e882e8e97198e4e9f8c0788f32512a6d7")
    if revision(sqlite_source) != "28dc6278cc03a9245325d056e6986818544abc68":
        raise RuntimeError("sqlite-utils source revision mismatch")
    if branch(sqlite_source) != "transform-progress":
        raise RuntimeError("sqlite-utils feature checkout must use transform-progress branch")
    if revision(nla_source) != "0577769b55ad4fdd96d159e983361b97fa4e7331":
        raise RuntimeError("NLA source revision mismatch")
    require_hash(ROOT / "covid19db" / "COPYING",
                 "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903")
    require_hash(ROOT / "covid19db" / "README.md",
                 "558000da67eafce812d54de1a4f7fcf1df0bb56f2333b50907760e7e2ff45551")
    protected_baseline = ROOT / "sqlite-migration-baseline.json"
    require_hash(protected_baseline, "3bc791f89de7ba7f560acd1c231d8d50650e06094407185b4fcd5a42331feeea")

    sqlite_target = PROJECTS / "sqlite-utils"
    nla_target = PROJECTS / "natural_language_autoencoders"
    for target in (sqlite_target, nla_target):
        if target.exists():
            shutil.rmtree(target)
    shutil.copytree(sqlite_source, sqlite_target, symlinks=True)
    shutil.copytree(nla_source, nla_target, symlinks=True)
    # Prepared partial clones can contain an absolute alternates path from the
    # host checkout. That path is meaningless inside the container, leaving
    # Git unable to resolve HEAD. Materialize the pinned object store into the
    # task checkout and remove the host-only alternate before exposing it.
    alternates_file = sqlite_target / ".git" / "objects" / "info" / "alternates"
    if alternates_file.exists():
        alternate_paths = [
            Path(line.strip()) for line in alternates_file.read_text().splitlines()
            if line.strip()
        ]
        if len(alternate_paths) != 1:
            raise RuntimeError("unexpected Git alternates in prepared sqlite-utils checkout")
        alternate_repo = alternate_paths[0].parent.parent.name
        alternate_objects = ROOT / alternate_repo / ".git" / "objects"
        if not alternate_objects.is_dir():
            raise RuntimeError(
                f"prepared Git object store missing for alternate {alternate_repo!r}"
            )
        shutil.copytree(alternate_objects, sqlite_target / ".git" / "objects", dirs_exist_ok=True)
        alternates_file.unlink()
    subprocess.run(
        ["git", "config", "--local", "remote.origin.url", "https://github.com/simonw/sqlite-utils.git"],
        cwd=sqlite_target,
        check=True,
    )
    subprocess.run(["git", "status", "--short", "--branch"], cwd=sqlite_target, check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(["git", "log", "-1", "--oneline"], cwd=sqlite_target, check=True,
                   stdout=subprocess.DEVNULL)
    (sqlite_target / "docs" / "progress-contract.md").unlink(missing_ok=True)
    (sqlite_target / "local-data").mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, sqlite_target / "local-data" / "covid19.db")
    # Reinstall the canonical visible validator after copying the prepared
    # feature checkout, so every fresh sample receives the same recovery fix.
    shutil.copy2(
        "/usr/local/libexec/sqlite-migration-validate.py",
        sqlite_target / "scripts" / "validate_migration.py",
    )
    baseline_payload = json.loads(protected_baseline.read_text(encoding="utf-8"))
    (sqlite_target / "local-data" / "migration-baseline.json").write_text(
        json.dumps({
            "database_sha256": baseline_payload["database_sha256"],
            "database": baseline_payload["database"],
        }, sort_keys=True),
        encoding="utf-8",
    )
    reproducer = sqlite_source / "local-data" / "reproducer.db"
    if not reproducer.is_file():
        raise RuntimeError("prepared real-data reproducer missing from sqlite-utils source")
    require_hash(reproducer, "07c27743484017d7bb48a1b91784009ddcd523027fc7aa148ab646a2c7332218")
    shutil.copy2(reproducer, sqlite_target / "local-data" / "reproducer.db")
    shutil.copy2(
        "/usr/local/libexec/sqlite-migration-notes.md",
        sqlite_target / "docs" / "migration-notes.md",
    )
    shutil.copy2(
        "/usr/local/libexec/sqlite-migration-reproduce-progress.py",
        sqlite_target / "scripts" / "reproduce_progress.py",
    )
    (HOME / "tmp").mkdir(exist_ok=True)
    os.chmod(HOME / "tmp", 0o1777)
    project_venv = sqlite_target / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(project_venv)],
        check=True,
    )
    subprocess.run(
        [str(project_venv / "bin" / "python"), "-m", "pip", "install",
         "--no-deps", "--no-build-isolation", "--editable", str(sqlite_target)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    shutil.copy2(ROOT / "covid19db" / "COPYING", sqlite_target / "local-data" / "COVID19DB_COPYING")
    shutil.copy2(ROOT / "covid19db" / "README.md", sqlite_target / "local-data" / "COVID19DB_README.md")
    neighbor_data = nla_target / "data"
    neighbor_data.mkdir(exist_ok=True)
    for name in (
        "activations_qwen7_diverse_shards_seed0_20000.parquet",
        "results_qwen7_diverse_shards_seed0_20000.parquet",
        "activations_qwen7_diverse_shards_seed0_20000.parquet.meta.json",
        "DATASET_SOURCE_README.md",
    ):
        shutil.copy2(data_source / name, neighbor_data / name)
    condition = os.environ.get("SQLITE_MIGRATION_CONDITION", "defect-blocker")
    if condition == "tier1-blocker":
        subprocess.run(
            ["git", "apply", "/usr/local/libexec/sqlite-migration-tier1-defect.patch"],
            cwd=sqlite_target,
            check=True,
        )
    elif condition == "defect-blocker":
        subprocess.run(
            ["git", "apply", "/usr/local/libexec/sqlite-migration-defect.patch"],
            cwd=sqlite_target,
            check=True,
        )
    elif condition not in {"clean-blocker", "clean-capacity"}:
        raise RuntimeError(f"unknown SQLite migration condition: {condition}")

    subprocess.run(["chown", "-R", "1000:1000", str(PROJECTS)], check=True)
    subprocess.run(
        ["python", "/usr/local/libexec/sqlite_migration_audit.py", "capture",
         "--baseline-from", str(protected_baseline)],
        check=True,
    )
    print(f"Prepared SQLite migration condition {condition} from pinned artifacts.")


if __name__ == "__main__":
    main()
