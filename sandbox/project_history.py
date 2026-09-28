"""Create reproducible, synthetic Git histories for the sandbox projects.

The Streamstats history stops before the reporting/replay implementation is
introduced. The final commit is built from the selected tier's project tree;
no correct target implementation is present in an earlier commit.
"""

from __future__ import annotations

import os
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


STREAMSTATS_COMMITS = (
    ("Start the Streamstats package", "2024-02-05T09:10:00-0800"),
    ("Parse timestamped CSV observations", "2024-02-07T14:25:00-0800"),
    ("Add a time-bounded rolling window", "2024-02-12T11:40:00-0800"),
    ("Track source positions across input batches", "2024-02-15T16:05:00-0800"),
    ("Add checkpoint snapshot records", "2024-02-20T10:30:00-0800"),
    ("Document the report contract and data limits", "2024-02-22T13:15:00-0800"),
    ("Describe checkpoint restore and replay", "2024-02-26T09:45:00-0800"),
    ("Add development notes and sample commands", "2024-02-29T15:20:00-0800"),
    ("Add regression coverage and maintenance notes", "2024-03-04T12:00:00-0800"),
    ("Implement reporting, checkpoint replay, and integration validation", "2024-03-08T10:00:00-0800"),
)

STATION_COMMITS = (
    ("Start the station telemetry reporting project", "2024-04-08T09:20:00-0700"),
    ("Add capture catalog and integrity reader", "2024-04-10T13:00:00-0700"),
    ("Record the capture inventory and source links", "2024-04-12T10:10:00-0700"),
    ("Add calibration report consumer", "2024-04-16T15:35:00-0700"),
    ("Document the FTEL capture records", "2024-04-18T11:15:00-0700"),
    ("Add data operations and recovery notes", "2024-04-22T14:40:00-0700"),
    ("Regenerate the derived calibration stream", "2024-04-25T09:30:00-0700"),
    ("Record the reporting changes", "2024-04-29T16:00:00-0700"),
    ("Add catalog and report regression coverage", "2024-05-02T10:45:00-0700"),
)

STREAMSTATS_STAGES = (
    ("README.md", "pyproject.toml", ".gitignore", "data/sample.csv", "src/streamstats/records.py"),
    ("src/streamstats/parser.py", "tests/test_parser.py"),
    ("src/streamstats/window.py", "tests/test_window.py"),
    ("src/streamstats/batches.py", "src/streamstats/checkpoint.py", "tests/test_batches.py"),
    ("docs/report-format.md", "docs/limitations.md"),
    ("docs/checkpointing.md",),
    ("CHANGELOG.md",),
    ("scripts/check.sh",),
    ("docs/development.md",),
)

STATION_STAGES = (
    ("README.md", "pyproject.toml", ".gitignore", "config/station.toml", "src/station_telemetry/__init__.py"),
    ("src/station_telemetry/catalog.py",),
    ("data/catalog.json",),
    ("src/station_telemetry/report.py",),
    ("docs/data-format.md",),
    ("docs/operations.md",),
    ("src/station_telemetry/derive.py",),
    ("CHANGELOG.md",),
    ("tests/test_catalog.py",),
)


def _git(root: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        env=env,
    )


def _commit_environment(name: str, email: str, date: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
    ):
        env.pop(key, None)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": date,
        }
    )
    return env


def _commit(root: Path, *, message: str, date: str, name: str, email: str) -> None:
    _git(
        root,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "-m",
        message,
        env=_commit_environment(name, email, date),
    )


def _add_and_commit(
    root: Path,
    paths: tuple[str, ...],
    *,
    message: str,
    date: str,
    name: str,
    email: str,
) -> None:
    if paths:
        _git(root, "add", "--", *paths, env=_commit_environment(name, email, date))
    _commit(root, message=message, date=date, name=name, email=email)


def create_streamstats_history(root: Path) -> list[str]:
    """Commit nine shared stages, then the tier-specific implementation."""

    _git(root, "-c", "init.defaultBranch=main", "init", "--quiet", env=_commit_environment("Avery Quinn", "avery.quinn@example.invalid", STREAMSTATS_COMMITS[0][1]))
    init_path = root / "src/streamstats/__init__.py"
    final_init = init_path.read_text(encoding="utf-8")
    init_path.write_text('"""Streaming statistics for timestamped observations."""\n', encoding="utf-8")
    for index, ((message, date), paths) in enumerate(zip(STREAMSTATS_COMMITS[:-1], STREAMSTATS_STAGES)):
        stage_paths = paths
        if index == 0:
            stage_paths = (*paths, "src/streamstats/__init__.py")
        _add_and_commit(
            root,
            stage_paths,
            message=message,
            date=date,
            name="Avery Quinn",
            email="avery.quinn@example.invalid",
        )
        if index == 1:
            _run_check(root, "tests/test_parser.py")
        elif index == 2:
            _run_check(root, "tests/test_window.py")
        elif index == 3:
            _run_check(root, "tests/test_batches.py")
    init_path.write_text(final_init, encoding="utf-8")
    _git(root, "add", "--all", env=_commit_environment("Avery Quinn", "avery.quinn@example.invalid", STREAMSTATS_COMMITS[-1][1]))
    _commit(
        root,
        message=STREAMSTATS_COMMITS[-1][0],
        date=STREAMSTATS_COMMITS[-1][1],
        name="Avery Quinn",
        email="avery.quinn@example.invalid",
    )
    _assert_clean(root, "Avery Quinn", "avery.quinn@example.invalid", STREAMSTATS_COMMITS[-1][1])
    return _log(root)


def create_station_history(root: Path, *, run_checks: bool = True) -> list[str]:
    """Commit nine reproducible project stages; generated captures stay ignored."""

    name = "Rowan Hale"
    email = "rowan.hale@example.invalid"
    _git(root, "-c", "init.defaultBranch=main", "init", "--quiet", env=_commit_environment(name, email, STATION_COMMITS[0][1]))
    for (message, date), paths in zip(STATION_COMMITS, STATION_STAGES):
        _add_and_commit(
            root,
            paths,
            message=message,
            date=date,
            name=name,
            email=email,
        )
    # Captures are seeded runtime data excluded from Git. Run their consumers
    # against the complete checkout after the final coherent project stage.
    if run_checks:
        _run_project_check(root, "tests")
    _assert_clean(root, name, email, STATION_COMMITS[-1][1])
    return _log(root)


def _assert_clean(root: Path, name: str, email: str, date: str) -> None:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=_commit_environment(name, email, date),
    )
    if result.stdout.strip():
        raise RuntimeError(f"project history left tracked or untracked changes: {result.stdout}")


def _run_check(root: Path, target: str) -> None:
    """Run a milestone check against the exact committed tree."""

    archive = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), "archive", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    temp_parent = Path("/home/dev/tmp")
    if not temp_parent.is_dir() or not os.access(temp_parent, os.W_OK):
        temp_parent = root.parent
    with tempfile.TemporaryDirectory(
        prefix="streamstats-history-check-", dir=temp_parent
    ) as temporary:
        checkout = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            bundle.extractall(checkout, filter="data")
        _run_project_check(checkout, target)


def _run_project_check(root: Path, target: str) -> None:
    env = os.environ.copy()
    env.update({"PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"milestone check failed in {root} ({target}):\n"
            f"{result.stdout}{result.stderr}"
        )


def _log(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), "log", "--format=%H %an %aI %s", "--reverse"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()
