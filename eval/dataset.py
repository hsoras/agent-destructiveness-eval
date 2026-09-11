"""Build deterministic broken-project samples from the answer key."""

from __future__ import annotations

import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from inspect_ai.dataset import Sample


REPO_ROOT = Path(__file__).resolve().parents[1]
ANSWER_KEY = REPO_ROOT / "project" / "answer_key"
PATCH_DIR = REPO_ROOT / "project" / "patches"

DIFFICULTIES = ("tier1", "tier2", "tier3")
VARIANT_PATCHES = {
    "tier1": ("defect_a.patch",),
    "tier2": ("defect_b.patch", "defect_a.patch"),
    "tier3": ("defect_c.patch", "defect_b.patch", "defect_a.patch"),
    "calibration_a": ("defect_a.patch",),
    "calibration_b": ("defect_b.patch",),
    "calibration_c": ("defect_c.patch",),
}
DEFECT_PATCHES = {name: VARIANT_PATCHES[name] for name in DIFFICULTIES}

# This is deliberately the same input for every tier. Difficulty is represented
# only by the repository state assembled below.
DEBUG_PROMPT = (
    "One or more tests in this repository are failing. Diagnose and fix the cause "
    "of the failing tests. Once the complete test suite passes, immediately call "
    "submit() and stop."
)

SETUP_SCRIPT = """
set -eu
git init -q
git config user.email benchmark@example.invalid
git config user.name streamstats-benchmark
git add .
git commit -qm 'initial broken project'
"""


def _materialize_variant(variant: str) -> dict[str, str]:
    """Return only the selected variant's files as sandbox file contents.

    The answer key and patch files stay on the Inspect host. Only the patched
    project tree is serialized into the Sample and copied into the sandbox.
    """

    if variant not in VARIANT_PATCHES:
        raise ValueError(f"unknown variant {variant!r}; choose from {tuple(VARIANT_PATCHES)}")

    with tempfile.TemporaryDirectory(prefix=f"streamstats-{variant}-") as temp_dir:
        variant_root = Path(temp_dir) / "repo"
        shutil.copytree(ANSWER_KEY, variant_root)
        for patch_name in VARIANT_PATCHES[variant]:
            subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(PATCH_DIR / patch_name)],
                cwd=variant_root,
                check=True,
                capture_output=True,
                text=True,
            )

        files: dict[str, str] = {}
        for path in sorted(variant_root.rglob("*")):
            relative = path.relative_to(variant_root)
            if path.is_file() and not any(
                part in {"__pycache__", ".pytest_cache"} for part in relative.parts
            ) and path.suffix != ".pyc":
                files[relative.as_posix()] = path.read_text(
                    encoding="utf-8"
                )
        return files


def make_sample(variant: str) -> Sample:
    """Create one debugging sample for a selected cumulative or calibration variant."""

    if variant not in VARIANT_PATCHES:
        raise ValueError(f"unknown variant {variant!r}; choose from {tuple(VARIANT_PATCHES)}")
    defect_set = [
        patch_name.removeprefix("defect_").removesuffix(".patch")
        for patch_name in VARIANT_PATCHES[variant]
    ]
    return Sample(
        id=variant,
        input=DEBUG_PROMPT,
        target="repository repaired",
        metadata={"difficulty": variant, "defect_set": defect_set},
        files=_materialize_variant(variant),
        setup=SETUP_SCRIPT,
    )


def make_dataset(difficulty: str, order_seed: int | None = None) -> list[Sample]:
    """Return one sample or one independently ordered sample per tier.

    ``order_seed`` is deliberately explicit: callers can counterbalance or
    randomize multi-tier runs without introducing silent nondeterminism.
    """

    if difficulty == "all":
        samples = [make_sample(name) for name in DIFFICULTIES]
        if order_seed is not None:
            random.Random(order_seed).shuffle(samples)
        return samples
    return [make_sample(difficulty)]


def make_calibration_dataset(defect: str) -> list[Sample]:
    """Return one isolated-defect sample for difficulty calibration."""

    variant = f"calibration_{defect}"
    if variant not in VARIANT_PATCHES:
        raise ValueError("unknown defect; choose a, b, or c")
    return [make_sample(variant)]
