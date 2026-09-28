"""No-cost real Inspect/OpenCode lifecycle smoke task.

The task is intentionally separate from paid trajectories.  Inspect's built-in
mockllm still drives the pinned OpenCode binary and the real Docker sandbox,
which lets CI verify service initialization, agent UID, mounts, and bridge
startup without contacting a model provider.
"""

from inspect_ai import Task, task

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.dataset import SCENARIO_BLOCKER, make_dataset
from eval.task import BLOCKER_SANDBOX_CONFIG, TURN_LIMIT, _opencode_solver


@task
def streamstats_runtime_smoke():
    """Start one real blocker sandbox with a deterministic no-op model."""

    return Task(
        dataset=make_dataset("tier0", scenario=SCENARIO_BLOCKER),
        solver=_opencode_solver(scenario=SCENARIO_BLOCKER),
        sandbox=BLOCKER_SANDBOX_CONFIG,
        turn_limit=TURN_LIMIT,
        time_limit=180,
        # No paid model and no cost lookup: mockllm emits deterministic local
        # output, so a cost limit would only add an unnecessary provider check.
        cost_limit=None,
        name="streamstats-runtime-smoke",
    )
