"""Inspect task for the streamstats debugging-complexity ladder."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.tool import bash_session, text_editor

from eval.dataset import DIFFICULTIES, make_calibration_dataset, make_dataset
from eval.scorer import repository_repaired


SANDBOX_CONFIG = ("docker", str(REPO_ROOT / "sandbox/compose.yaml"))
# Inspect expresses cost limits in dollars. Every cumulative tier receives the
# same five-cent per-sample budget so cost caps do not vary by condition.
COST_LIMITS = {"tier1": 0.05, "tier2": 0.05, "tier3": 0.05}
CALIBRATION_COST_LIMIT = 0.05
ISOLATION_CONTRACT = {
    "conversation_scope": "fresh per sample and epoch",
    "sandbox_scope": "fresh Docker environment per sample and epoch",
    "sandbox_cleanup": "Inspect default cleanup enabled",
    "model_generation_cache": False,
}


def _build_task(
    difficulty: str, order_seed: int | None = None, *, task_name: str
) -> Task:
    samples = make_dataset(difficulty, order_seed)
    # A single Inspect Task can contain all three tiers; every sample uses the
    # same flat budget regardless of whether it is interleaved or standalone.
    cost_limit = COST_LIMITS.get(difficulty, CALIBRATION_COST_LIMIT)
    return Task(
        dataset=samples,
        solver=react(
            tools=[bash_session(timeout=240), text_editor(timeout=180)],
            attempts=1,
        ),
        scorer=repository_repaired(),
        sandbox=SANDBOX_CONFIG,
        message_limit=100,
        cost_limit=cost_limit,
        time_limit=900,
        name=task_name,
        metadata={
            "benchmark": "streamstats-debugging-ladder",
            "sample_order_seed": order_seed,
            "sample_order": [sample.id for sample in samples],
            "sample_cost_limit_usd": cost_limit,
            "isolation_contract": ISOLATION_CONTRACT,
            "reasoning_configuration": {
                "solver": "react",
                "tools": "bash_session(timeout=240), text_editor(timeout=180)",
                "attempts": 1,
                "message_limit": 100,
                "time_limit": 900,
                "cost_limit_usd": cost_limit,
            },
        },
    )


def _build_calibration_task(defect: str, *, task_name: str) -> Task:
    samples = make_calibration_dataset(defect)
    return Task(
        dataset=samples,
        solver=react(
            tools=[bash_session(timeout=240), text_editor(timeout=180)],
            attempts=1,
        ),
        scorer=repository_repaired(),
        sandbox=SANDBOX_CONFIG,
        message_limit=100,
        cost_limit=CALIBRATION_COST_LIMIT,
        time_limit=900,
        name=task_name,
        metadata={
            "benchmark": "streamstats-debugging-ladder-calibration",
            "isolated_defect": defect,
            "sample_cost_limit_usd": CALIBRATION_COST_LIMIT,
            "isolation_contract": ISOLATION_CONTRACT,
            "reasoning_configuration": {
                "solver": "react",
                "tools": "bash_session(timeout=240), text_editor(timeout=180)",
                "attempts": 1,
                "message_limit": 100,
                "time_limit": 900,
                "cost_limit_usd": CALIBRATION_COST_LIMIT,
            },
        },
    )


@task
def streamstats_debug(
    difficulty: str = "tier1", order_seed: int | None = None
) -> Task:
    """Run one tier through the compact single-task interface."""

    if difficulty not in (*DIFFICULTIES, "all"):
        raise ValueError(
            f"unknown difficulty {difficulty!r}; choose tier1, tier2, tier3, or all"
        )
    return _build_task(difficulty, order_seed, task_name="streamstats-debug")


@task
def streamstats_tier1(order_seed: int | None = None) -> Task:
    return _build_task("tier1", order_seed, task_name="streamstats-debug-tier1")


@task
def streamstats_tier2(order_seed: int | None = None) -> Task:
    return _build_task("tier2", order_seed, task_name="streamstats-debug-tier2")


@task
def streamstats_tier3(order_seed: int | None = None) -> Task:
    return _build_task("tier3", order_seed, task_name="streamstats-debug-tier3")


@task
def streamstats_calibration_a() -> Task:
    """Run defect A alone for isolated difficulty calibration."""

    return _build_calibration_task("a", task_name="streamstats-calibration-a")


@task
def streamstats_calibration_b() -> Task:
    """Run defect B alone for isolated difficulty calibration."""

    return _build_calibration_task("b", task_name="streamstats-calibration-b")


@task
def streamstats_calibration_c() -> Task:
    """Run defect C alone for isolated difficulty calibration."""

    return _build_calibration_task("c", task_name="streamstats-calibration-c")
