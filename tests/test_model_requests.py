import asyncio
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from tenacity import RetryError
from inspect_ai.model import ModelOutput, GenerateConfig
from inspect_ai.model._model import AttemptTimeoutError

from eval.model_requests import (
    ModelRequestGuard, InfrastructureRequestError, cli_error_event,
    request_failure_details,
)
from eval.runtime_errors import missing_remote_job_error, native_session_error
from eval.scorer import _termination_reason


@pytest.mark.parametrize("first_failure", [429, "timeout", 404])
def test_dev_reconsiders_transient_failures_but_excludes_ineligible_routes(monkeypatch, first_failure):
    import eval.model_requests as requests
    calls = []

    class ProviderFailure(Exception):
        status_code = first_failure

    class Original:
        model_args = {"provider": {"sort": "price", "zdr": True, "data_collection": "deny"}}
        def __str__(self): return "openrouter/test/model"
        def _resolve_config(self, config): return config

    class Candidate:
        def __init__(self, routing): self.routing = routing
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def generate(self, **kwargs):
            calls.append((self.routing, kwargs["config"]))
            if self.routing["only"] == ["cheap"]:
                if first_failure == "timeout": raise AttemptTimeoutError(1)
                raise ProviderFailure("provider unavailable")
            return ModelOutput.from_content("test/model", "done")

    monkeypatch.setattr(requests, "get_model", lambda *args, **kwargs: Candidate(kwargs["provider"]))
    async def scenario():
        guard = ModelRequestGuard(dev_routes=["cheap", "next"])
        # Each pinned call has no Inspect retries; the host chooses the route.
        config = GenerateConfig(max_retries=3, timeout=600, attempt_timeout=120)
        assert (await guard.generate(Original(), [], [], None, config)).completion == "done"
        assert (await guard.generate(Original(), [], [], None, config)).completion == "done"
        assert not guard.failed.is_set()
        assert guard.unavailable == ({"cheap"} if first_failure == 404 else set())
        assert guard.audit[0]["provider_attempts"][0]["status"] == "failed"
    asyncio.run(scenario())
    expected = [["cheap"], ["next"], ["next"]] if first_failure == 404 else [["cheap"], ["next"], ["cheap"], ["next"]]
    assert [routing["only"] for routing, _ in calls] == expected
    assert all(r["zdr"] and r["data_collection"] == "deny" and not r["allow_fallbacks"] for r, _ in calls)
    assert all(c.max_retries == 0 for _, c in calls)
    assert all(isinstance(c.timeout, int) and isinstance(c.attempt_timeout, int) for _, c in calls)
    assert all(GenerateConfig.model_validate(c.model_dump()) for _, c in calls)


def test_dev_missing_request_limits_use_revision5_fallbacks(monkeypatch):
    import eval.model_requests as requests
    calls = []

    class ProviderFailure(Exception):
        status_code = 503

    class Original:
        model_args = {}
        def __str__(self): return "openrouter/test/model"
        def _resolve_config(self, config): return config

    class Candidate:
        def __init__(self, route): self.route = route
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def generate(self, **kwargs):
            calls.append((self.route, kwargs["config"]))
            if self.route == "first":
                raise ProviderFailure("unavailable")
            return ModelOutput.from_content("test/model", "done")

    monkeypatch.setattr(
        requests, "get_model",
        lambda *args, **kwargs: Candidate(kwargs["provider"]["only"][0]),
    )

    async def scenario():
        config = GenerateConfig(max_retries=None, timeout=None, attempt_timeout=None)
        guard = ModelRequestGuard(dev_routes=["first", "second"])
        await guard.generate(Original(), [], [], None, config)

    asyncio.run(scenario())
    assert [route for route, _ in calls] == ["first", "second"]
    assert all(config.max_retries == 0 for _, config in calls)
    assert all(config.attempt_timeout == 300 for _, config in calls)
    assert all(590 <= config.timeout <= 600 for _, config in calls)


def test_dev_real_inspect_http_429_switches_without_waiting_retry_after(monkeypatch):
    import json
    import httpx2
    from inspect_ai.model import get_model
    import eval.model_requests as requests
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload["provider"]["only"] == ["cheap"]:
            return httpx2.Response(429, headers={"retry-after": "3600"},
                json={"error": {"message": "provider rate limited", "code": 429,
                    "metadata": {"provider_name": "Cheap", "provider_code": "rate_limited"}}})
        return httpx2.Response(200, json={"id": "reply", "object": "chat.completion",
            "created": 0, "model": "test/model", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "recovered"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})

    def candidate(*args, **kwargs):
        kwargs["http_client"] = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        kwargs["stream"] = False
        return get_model(*args, **kwargs)

    monkeypatch.setattr(requests, "get_model", candidate)
    async def scenario():
        model = get_model("openrouter/test/model", api_key="test-key", memoize=False)
        guard = ModelRequestGuard(dev_routes=["cheap", "next"])
        from inspect_ai.model import ChatMessageUser
        result = await asyncio.wait_for(guard.generate(model,
            [ChatMessageUser(content="Public synthetic request")], [], None,
            GenerateConfig(max_retries=3, timeout=10, attempt_timeout=2, max_tokens=10)), timeout=5)
        assert result.completion == "recovered"
        failed = guard.audit[0]["provider_attempts"][0]
        assert failed["error_type"] == "rate_limit"
        assert failed["error_source"] == "provider"
        assert failed["upstream_provider"] == "Cheap"
        assert failed["retry_after"] == "3600"
        assert failed["error_message"] == "provider rate limited"
        await model.api.aclose()
    asyncio.run(scenario())
    assert [p["provider"]["only"] for p in payloads] == [["cheap"], ["next"]]
    assert all(p["provider"]["zdr"] and p["provider"]["data_collection"] == "deny" for p in payloads)


def test_exhausted_timeout_aborts_zero_exit_scaffold_and_preserves_cause():
    async def scenario():
        class FailedModel:
            async def generate(self, **kwargs):
                future = Future()
                future.set_exception(AttemptTimeoutError(1))
                raise RetryError(future)

        guard = ModelRequestGuard()
        cancelled = asyncio.Event()

        async def scaffold():
            try:
                await guard.generate(FailedModel(), [], [], None, None)
            except InfrastructureRequestError:
                # OpenCode can swallow the API error and keep running or exit 0.
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
                return SimpleNamespace(success=True)

        with pytest.raises(InfrastructureRequestError) as caught:
            await asyncio.wait_for(guard.run_agent(scaffold()), timeout=1)
        assert caught.value.status_code == 504
        assert guard.audit[-1]["error_type"] == "timeout"
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_successful_model_call_allows_scaffold_completion():
    async def scenario():
        class HealthyModel:
            async def generate(self, **kwargs):
                return ModelOutput.from_content("mockllm/model", "done")

        guard = ModelRequestGuard()

        async def scaffold():
            return await guard.generate(HealthyModel(), [], [], None, None)

        result = await guard.run_agent(scaffold())
        assert result.completion == "done"
        assert guard.error is None
        assert guard.audit[-1]["status"] == "completed"

    asyncio.run(scenario())


def test_terminal_native_api_error_is_not_normal_completion():
    export = {"messages": [{"data": {"role": "assistant", "error": {
        "name": "APIError", "data": {"message": "attempt timeout exhausted"}
    }}}]}
    assert native_session_error(export) == "APIError: attempt timeout exhausted"
    assert _termination_reason(
        SimpleNamespace(error=None, limit=None), final_response="earlier progress",
        normal_submit=False, export=export,
    ) == "infrastructure_error"
    export["messages"].append({"data": {"role": "assistant"}})
    assert native_session_error(export) is None


def test_missing_exec_remote_job_is_classified_as_infrastructure_error():
    error = RuntimeError("No job found with pid 325")
    assert missing_remote_job_error(error) == "No job found with pid 325"
    assert missing_remote_job_error(RuntimeError("command exited with status 1")) is None
    assert _termination_reason(
        SimpleNamespace(error=error, limit=None),
        final_response="partial output",
        normal_submit=False,
    ) == "infrastructure_error"


def test_cli_error_is_detected_even_when_process_exits_zero():
    assert cli_error_event('{"type":"error","error":{"name":"APIError"}}') is not None
    assert cli_error_event('{"type":"text","part":{"text":"done"}}') is None


@pytest.mark.parametrize("status,metadata,kind,source", [
    (429, {"provider_name": "DeepInfra", "provider_code": "rate_limited"}, "rate_limit", "provider"),
    (429, {"limit_source": "openrouter_in_flight_budget"}, "rate_limit", "openrouter"),
    (429, {}, "rate_limit", "openrouter_or_provider"),
    (401, {}, "authentication_error", "openrouter"),
    (402, {}, "insufficient_credits", "openrouter"),
    (404, {}, "routing_rejection", "openrouter"),
    (400, {"provider_name": "DeepInfra"}, "invalid_request", "provider"),
    (403, {}, "permission_or_policy_rejection", "openrouter_or_provider"),
    (503, {}, "provider_error", "openrouter_or_provider"),
])
def test_retry_error_exposes_sdk_body_and_origin(status, metadata, kind, source):
    import httpx
    from openai import APIStatusError
    response = httpx.Response(status, headers={"retry-after": "60", "x-request-id": "req-test"},
                              request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    error = APIStatusError("wrapper", response=response, body={"error": {
        "message": "Useful rejection reason", "code": status,
        "metadata": {**metadata, "flagged_input": "private input", "raw": ""},
    }})
    future = Future()
    future.set_exception(error)
    details = request_failure_details(RetryError(future), model="openrouter/test/model")
    assert details["http_status"] == status
    assert details["error_type"] == kind
    assert details["error_source"] == source
    assert details["error_message"] == "Useful rejection reason"
    assert details["error_class"] == "APIStatusError"
    assert details["retry_after"] == "60"
    assert details["request_id"] == "req-test"
    assert "private input" not in str(details)
    assert "Future" not in str(details)


def test_inspect_stream_error_without_http_status_is_classified():
    from inspect_ai.model._providers.openrouter import OpenAIResponseError, OpenRouterError
    assert request_failure_details(OpenAIResponseError("rate_limit_exceeded", "Busy"))["http_status"] == 429
    error = OpenRouterError({"code": 404, "message": "No endpoints satisfy routing"})
    details = request_failure_details(error, model="openrouter/test/model")
    assert details["error_type"] == "routing_rejection"
    assert details["error_message"] == "No endpoints satisfy routing"


@pytest.mark.parametrize("status,retries,expected_calls,stop_reason", [
    (429, 1, ["a", "b"], "configured additional attempt limit exhausted"),
    (429, 5, ["a", "b", "a", "b", "c", "c"], "configured additional attempt limit exhausted"),
    (429, 8, ["a", "b", "a", "b", "c", "c", "a", "b", "a"], "configured additional attempt limit exhausted"),
    (404, 5, ["a", "b", "c"], "no remaining eligible dev provider endpoints"),
    (401, 5, ["a"], "non-retryable request rejection"),
])
def test_terminal_dev_failure_explains_attempts(monkeypatch, caplog, status, retries, expected_calls, stop_reason):
    import eval.model_requests as requests
    calls = []
    class Failure(Exception):
        status_code = status
    class Original:
        model_args = {}
        def __str__(self): return "openrouter/test/model"
        def _resolve_config(self, config): return config
    class Candidate:
        def __init__(self, route): self.route = route
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def generate(self, **kwargs):
            calls.append(self.route)
            raise Failure("specific failure message")
    monkeypatch.setattr(requests, "get_model", lambda *args, **kwargs: Candidate(kwargs["provider"]["only"][0]))
    async def scenario():
        guard = ModelRequestGuard(dev_routes=["a", "b", "c"])
        with pytest.raises(InfrastructureRequestError) as caught:
            await guard.generate(Original(), [], [], None, GenerateConfig(max_retries=retries, timeout=30))
        assert stop_reason in str(caught.value)
        assert "specific failure message" in str(caught.value)
        assert "HTTP " + str(status) in str(caught.value)
        assert "Provider attempts: a:" in str(caught.value)
        assert caught.value.__cause__ is not None
        assert guard.failed.is_set()
        assert request_failure_details(caught.value) == caught.value.details
        assert guard.audit[-1]["fallback_stop_reason"] == stop_reason
    asyncio.run(scenario())
    assert calls == expected_calls
    assert "specific failure message" in caplog.text


@pytest.mark.parametrize("routes,expected_calls", [
    (["cheap", "middle", "expensive"], ["cheap", "middle", "cheap"]),
    (["cheap"], ["cheap", "cheap", "cheap"]),
    (["cheap", "middle", "expensive"], ["cheap", "middle", "cheap", "middle", "expensive"]),
    (["cheap", "middle"], ["cheap", "middle", "cheap", "middle", "cheap"]),
])
def test_dev_recovers_by_retrying_cheapest_provider(monkeypatch, routes, expected_calls):
    import eval.model_requests as requests
    calls = []
    class Failure(Exception):
        status_code = 429
    class Original:
        model_args = {}
        def __str__(self): return "openrouter/test/model"
        def _resolve_config(self, config): return config
    class Candidate:
        def __init__(self, route): self.route = route
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def generate(self, **kwargs):
            calls.append(self.route)
            if len(calls) < len(expected_calls):
                if self.route == "cheap":
                    raise AttemptTimeoutError(180)
                raise Failure("temporarily busy")
            return ModelOutput.from_content("test/model", "recovered")
    monkeypatch.setattr(requests, "get_model", lambda *args, **kwargs: Candidate(kwargs["provider"]["only"][0]))
    async def scenario():
        guard = ModelRequestGuard(dev_routes=routes)
        result = await guard.generate(Original(), [], [], None, GenerateConfig(max_retries=len(expected_calls) - 1, timeout=30))
        assert result.completion == "recovered"
        assert guard.unavailable == set()
        assert not guard.failed.is_set()
        assert [row["provider"] for row in guard.audit[0]["provider_attempts"]] == expected_calls
        assert guard.audit[0]["provider_attempts"][0]["fallback_action"] == "restart_cheapest_search"
    asyncio.run(scenario())
    assert calls == expected_calls
