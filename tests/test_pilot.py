from scripts.pilot import (
    fetch_openrouter_model_cost,
    fresh_eval_environment,
    openrouter_model_cost,
    write_model_cost_config,
)


def test_openrouter_cost_is_refreshed_even_when_a_route_is_pinned():
    calls = []

    def fetch(model):
        calls.append(model)
        return {
            "input": 0.09,
            "output": 0.30,
            "input_cache_write": 0.0,
            "input_cache_read": 0.018,
        }

    cost, source = openrouter_model_cost(
        "openrouter/z-ai/glm-5.3-flash",
        "relace/fp4",
        fetcher=fetch,
    )

    assert calls == ["openrouter/z-ai/glm-5.3-flash"]
    assert source == "fresh OpenRouter public model catalog lookup for pinned route relace"
    assert cost == {
        "input": 0.09,
        "output": 0.30,
        "input_cache_write": 0.0,
        "input_cache_read": 0.018,
    }


def test_openrouter_catalog_pricing_is_converted_to_inspect_units():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"data": {"pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0.0000005"}}}'

    cost = fetch_openrouter_model_cost(
        "openrouter/example/model",
        opener=lambda request, timeout: Response(),
    )

    assert cost == {
        "input": 1.0,
        "output": 2.0,
        "input_cache_write": 0.0,
        "input_cache_read": 0.5,
    }


def test_model_cost_config_uses_inspect_model_cost_shape(tmp_path):
    path = tmp_path / "costs.json"
    write_model_cost_config(
        path,
        "openrouter/example/model",
        {
            "input": 1.0,
            "output": 2.0,
            "input_cache_write": 0.0,
            "input_cache_read": 0.5,
        },
    )

    assert '"openrouter/example/model"' in path.read_text()
    assert '"input_cache_read": 0.5' in path.read_text()


def test_pilot_clears_cross_run_state_overrides_and_generation_cache():
    environment = fresh_eval_environment(
        {
            "INSPECT_EVAL_NO_SANDBOX_CLEANUP": "1",
            "INSPECT_EVAL_CHECKPOINT": "every: 500k",
            "INSPECT_EVAL_CACHE": "7D",
            "MODEL_ROUTE": "same-route",
        }
    )

    assert "INSPECT_EVAL_NO_SANDBOX_CLEANUP" not in environment
    assert "INSPECT_EVAL_CHECKPOINT" not in environment
    assert environment["INSPECT_EVAL_CACHE"] == "false"
    assert environment["MODEL_ROUTE"] == "same-route"
