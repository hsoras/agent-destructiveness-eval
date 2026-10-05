import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from inspect_ai.model import ModelInfo, get_model_info, set_model_info

from scripts.inspect_cli import register_priced_models


@pytest.mark.parametrize("suffix", ["json", "yaml"])
def test_missing_openrouter_model_can_receive_inspect_cost_override(tmp_path, suffix):
    from inspect_ai.model import ModelCost, set_model_cost

    model = f"openrouter/example/pricing-regression-{suffix}"
    cost = dict(input=1.0, output=2.0, input_cache_read=0.5, input_cache_write=0.0)
    path = tmp_path / f"costs.{suffix}"
    if suffix == "json":
        path.write_text(json.dumps({model: cost}))
    else:
        import yaml
        path.write_text(yaml.safe_dump({model: cost}))
    assert get_model_info(model) is None
    register_priced_models(["eval", "--model-cost-config", str(path)])
    set_model_cost(model, ModelCost(**cost))
    assert get_model_info(model).cost.output == 2.0
    assert get_model_info(model).context_length is None


def test_registration_preserves_existing_model_metadata(tmp_path):
    model = "openrouter/example/existing-pricing-regression"
    info = ModelInfo(context_length=32000, output_tokens=4096)
    set_model_info(model, info)
    path = tmp_path / "costs.json"
    path.write_text(json.dumps({model: dict(input=1, output=2, input_cache_read=0, input_cache_write=0)}))
    register_priced_models([f"--model-cost-config={path}"])
    assert get_model_info(model) == info


def test_launcher_starts_inspect_with_model_missing_from_catalog(tmp_path):
    model = "mockllm/pricing-registration-regression"
    costs = tmp_path / "costs.json"
    costs.write_text(json.dumps({model: dict(input=1, output=2, input_cache_read=0, input_cache_write=0)}))
    task = tmp_path / "task.py"
    task.write_text(
        "from inspect_ai import Task, task\n"
        "from inspect_ai.dataset import Sample\n"
        "from inspect_ai.solver import generate\n"
        "@task\n"
        "def smoke():\n"
        "    return Task(dataset=[Sample(input='Hello')], solver=generate())\n"
    )
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "inspect_cli.py"
    # Keep Inspect's trace and sample-buffer files inside the test directory.
    bootstrap = (
        "import platformdirs, runpy, sys\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv.pop(1))\n"
        "platformdirs.user_data_path = lambda *a, **k: root / 'data'\n"
        "platformdirs.user_cache_path = lambda *a, **k: root / 'cache'\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, str(tmp_path), str(launcher), "eval", f"{task}@smoke", "--model", model,
         "--model-cost-config", str(costs), "--log-dir", str(tmp_path / "logs"),
         "--display", "plain", "--no-score", "--ctl-server", "false"],
        env={**os.environ, "INSPECT_EVAL_CACHE": "false",
             "INSPECT_TRACE_FILE": str(tmp_path / "trace.log")},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list((tmp_path / "logs").glob("*.eval"))
