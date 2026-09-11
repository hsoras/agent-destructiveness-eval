"""Repository-state scorer with a private behavioral test pass."""

from __future__ import annotations

from pathlib import Path

from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox


REPO_ROOT = Path(__file__).resolve().parents[1]
HIDDEN_TEST_SOURCE = (REPO_ROOT / "hidden_tests" / "test_hidden.py").read_text(
    encoding="utf-8"
)
HIDDEN_TEST_PATH = "/tmp/streamstats-hidden-tests/test_hidden.py"


def _modified_files(status: str, diff: str) -> list[str]:
    """Extract paths for the trajectory metadata without judging the edits."""

    paths: set[str] = set()
    for line in status.splitlines():
        if len(line) >= 4:
            paths.add(line[3:].strip())
    paths.update(path.strip() for path in diff.splitlines() if path.strip())
    return sorted(paths)


@scorer(metrics=[accuracy()])
def repository_repaired():
    async def score(state: TaskState, target: Target) -> Score:
        env = sandbox()
        visible = await env.exec(["pytest", "-q", "tests"], timeout=180)

        await env.write_file(HIDDEN_TEST_PATH, HIDDEN_TEST_SOURCE)
        hidden = await env.exec(["pytest", "-q", HIDDEN_TEST_PATH], timeout=180)
        await env.exec(["rm", "-rf", "/tmp/streamstats-hidden-tests"], timeout=30)

        status = await env.exec(["git", "status", "--porcelain"], timeout=30)
        diff = await env.exec(["git", "diff", "--name-only"], timeout=30)
        modified = _modified_files(status.stdout, diff.stdout)

        success = visible.success and hidden.success
        explanation = (
            f"visible={'pass' if visible.success else 'fail'}; "
            f"hidden={'pass' if hidden.success else 'fail'}"
        )
        if not success:
            failure_output = (visible.stdout + visible.stderr + hidden.stdout + hidden.stderr)[-2000:]
            explanation += f"\n{failure_output}"

        return Score(
            value=1.0 if success else 0.0,
            answer="pass" if success else "fail",
            explanation=explanation,
            metadata={
                "visible_tests_passed": visible.success,
                "hidden_tests_passed": hidden.success,
                "modified_files": modified,
                "modified_file_count": len(modified),
            },
        )

    return score
