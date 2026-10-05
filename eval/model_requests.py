"""Bound model retries and propagate exhausted requests to the eval runner."""

import asyncio
import inspect
import logging
import time
from typing import Any

from inspect_ai.agent._bridge.util import bridge_model_generate
from inspect_ai.model import GenerateInput, Model, ModelOutput, get_model
from inspect_ai.model._model import AttemptTimeoutError, StreamIdleTimeoutError
from inspect_ai._util.http import status_code_of

logger = logging.getLogger(__name__)


class InfrastructureRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, details=None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


def request_failure(exc: Exception) -> tuple[str, int]:
    details = request_failure_details(exc)
    return details["error_type"], details["http_status"]


def request_failure_details(exc: Exception, *, model: str = "") -> dict[str, Any]:
    """Unwrap retry wrappers without discarding the underlying failure."""
    if isinstance(exc, InfrastructureRequestError) and exc.details:
        return dict(exc.details)
    seen: set[int] = set()
    current: BaseException = exc
    chain = []
    while id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        attempt = getattr(current, "last_attempt", None)
        nested = attempt.exception() if attempt is not None else None
        nested = nested or current.__cause__ or current.__context__
        if not isinstance(nested, BaseException):
            break
        current = nested
    # The SDK generally has the useful body/status even when its cause is a
    # lower-level HTTP exception. Inspect also exposes dict response errors.
    body: dict[str, Any] = {}
    status = None
    selected = chain[-1]
    response = None
    for item in chain:
        candidate = getattr(item, "body", None)
        item_response = getattr(item, "response", None)
        if not isinstance(candidate, dict) and isinstance(item_response, dict):
            candidate = item_response
        code = status_code_of(item)
        if isinstance(candidate, dict):
            candidate = candidate.get("error", candidate)
            if isinstance(candidate, dict):
                body = candidate
                if code is None and isinstance(body.get("code"), int):
                    code = body["code"]
                selected = item
        if code is not None:
            status, selected = code, item
        if item_response is not None and not isinstance(item_response, dict):
            response = item_response
    metadata = body.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    typed_code = metadata.get("error_type") or body.get("type") or body.get("code") or getattr(selected, "code", None)
    if not isinstance(typed_code, (str, int)):
        typed_code = None
    timeout = any(isinstance(item, (AttemptTimeoutError, StreamIdleTimeoutError, TimeoutError)) for item in chain)
    if timeout or status in {408, 504}:
        kind, status = "timeout", status or 504
    elif status == 429 or typed_code in {"rate_limit_exceeded", "rate_limited"} or any(type(item).__name__ == "RateLimitError" for item in chain):
        kind, status = "rate_limit", status or 429
    elif status in {401, 402, 403, 400, 422, 404}:
        kind = {401: "authentication_error", 402: "insufficient_credits", 403: "permission_or_policy_rejection", 400: "invalid_request", 422: "invalid_request", 404: "routing_rejection"}[status]
    elif status is not None or typed_code in {"server_error", "server"}:
        kind, status = "provider_error", status or 502
    elif any(type(item).__name__ == "APIConnectionError" for item in chain):
        kind, status = "connection_error", 503
    else:
        kind, status = "request_error", 500
    provider = metadata.get("provider_name") or body.get("provider")
    if provider or metadata.get("provider_code") or metadata.get("raw"):
        source = "provider"
    elif model.startswith("openrouter/") and (status in {401, 402, 404} or str(metadata.get("limit_source", "")).startswith("openrouter")):
        source = "openrouter"
    elif timeout or kind == "connection_error":
        source = "transport"
    else:
        source = "openrouter_or_provider" if model.startswith("openrouter/") else "unknown"
    details = dict(error_type=kind, http_status=status, error_source=source,
                   error_class=type(selected).__name__,
                   error_message=str(body.get("message") or str(selected) or type(selected).__name__)[:2000])
    # Keep selected diagnostics, never dump headers, raw bodies, or flagged input.
    for key in ("error_type", "provider_code", "limit_source"):
        if isinstance(metadata.get(key), (str, int)):
            details["upstream_" + key] = metadata[key]
    if isinstance(provider, str):
        details["upstream_provider"] = provider
    headers = getattr(response, "headers", {})
    for header, key in (("retry-after", "retry_after"), ("x-request-id", "request_id")):
        value = headers.get(header)
        if value:
            details[key] = str(value)
    return details


def failure_summary(details):
    parts = [details["error_type"], "source=" + details["error_source"],
             "HTTP " + str(details["http_status"]), details["error_class"]]
    for key in ("upstream_provider", "upstream_provider_code", "retry_after", "request_id"):
        if key in details:
            parts.append(f"{key}={details[key]}")
    return f"{', '.join(parts)}: {details['error_message']}"


class ModelRequestGuard:
    def __init__(self, generation_filter=None, *, dev_routes=None) -> None:
        self.generation_filter = generation_filter
        self.failed = asyncio.Event()
        self.error: InfrastructureRequestError | None = None
        self.audit: list[dict[str, Any]] = []
        self.dev_routes = list(dev_routes or [])
        self.unavailable: set[str] = set()
        self.dev_lock = asyncio.Lock()

    async def _generate_dev(self, model, input, tools, tool_choice, config, record):
        # Serialize title/main requests so both observe routing exclusions.
        async with self.dev_lock:
            resolved = model._resolve_config(config)
            started = time.monotonic()
            budget = resolved.timeout or 600
            retries = resolved.max_retries if resolved.max_retries is not None else 1
            attempts = record.setdefault("provider_attempts", [])
            record["configured_providers"] = list(self.dev_routes)
            last_error = None
            last_route = None
            sweep_attempts: dict[str, int] = {}
            used = 0
            while True:
                eligible = [route for route in self.dev_routes if route not in self.unavailable]
                if not eligible:
                    break
                remaining = budget - (time.monotonic() - started)
                remaining_seconds = int(remaining)
                if remaining_seconds <= 0:
                    record["fallback_stop_reason"] = "request timeout budget exhausted"
                    break
                if used > retries:
                    record["fallback_stop_reason"] = "configured additional attempt limit exhausted"
                    break
                # Give cheaper routes a second chance without letting them use
                # every retry. Reach all eligible routes before starting over.
                candidates = [route for route in eligible if sweep_attempts.get(route, 0) < 2]
                if not candidates:
                    sweep_attempts.clear()
                    candidates = eligible
                route = next((route for route in candidates if route != last_route), candidates[0])
                sweep_attempts[route] = sweep_attempts.get(route, 0) + 1
                args = dict(model.model_args)
                routing = dict(args.get("provider") or {})
                routing.update(only=[route], allow_fallbacks=False, data_collection="deny", zdr=True)
                routing.pop("order", None)
                args["provider"] = routing
                args["strict_tools"] = False
                call_config = resolved.model_copy(update={
                    "max_retries": 0, "timeout": remaining_seconds,
                    "attempt_timeout": min(resolved.attempt_timeout or 300, remaining_seconds),
                })
                row = {"provider": route, "status": "pending", "sweep_attempt": sweep_attempts[route]}
                attempts.append(row)
                call_started = time.monotonic()
                used += 1
                try:
                    # Separate instances avoid mutating provider routing on a
                    # shared model. Inspect still records usage and sample limits.
                    async with get_model(str(model), config=resolved, memoize=False,
                                         base_url=getattr(model, "explicit_base_url", None),
                                         api_key=getattr(getattr(model, "api", None), "api_key", None),
                                         **args) as candidate:
                        output = await candidate.generate(input=input, tools=tools,
                            tool_choice=tool_choice, config=call_config)
                    row["status"] = "completed"
                    if last_error is not None:
                        logger.warning("Model request recovered on dev endpoint %s (%s)", route, model)
                    return output
                except Exception as exc:
                    from inspect_ai.util._limit import LimitExceededError
                    if isinstance(exc, LimitExceededError):
                        row["status"] = "limited"
                        raise
                    details = request_failure_details(exc, model=str(model))
                    kind, status = details["error_type"], details["http_status"]
                    row.update(status="failed", **details)
                    if kind != "timeout" and status not in {408, 429, 500, 502, 503, 504, 404}:
                        row["fallback_action"] = "abort"
                        record["fallback_stop_reason"] = "non-retryable request rejection"
                        logger.warning("Dev endpoint %s rejected request; stopping: %s", route, failure_summary(details))
                        raise
                    last_error = exc
                    last_route = route
                    if status == 404:
                        # A pinned endpoint rejected by privacy/parameter filters
                        # is ineligible, rather than an API retry opportunity.
                        self.unavailable.add(route)
                        row["fallback_action"] = "exclude_ineligible_endpoint"
                        used -= 1
                    else:
                        row["fallback_action"] = "restart_cheapest_search"
                    logger.warning("Dev endpoint %s failed (attempt %s/2 in this sweep); retrying in price order within request limits: %s", route, sweep_attempts[route], failure_summary(details))
                finally:
                    row["elapsed_seconds"] = round(time.monotonic() - call_started, 3)
            record.setdefault("fallback_stop_reason", "no remaining eligible dev provider endpoints")
            record["unavailable_providers"] = sorted(self.unavailable)
            attempted = {row["provider"] for row in attempts}
            record["untried_providers"] = [route for route in self.dev_routes if route not in attempted and route not in self.unavailable]
            if last_error is not None:
                raise last_error
            raise RuntimeError("No remaining eligible dev provider endpoints for this sample")

    async def generate(self, model: Model, input, tools, tool_choice, config):
        started = time.monotonic()
        record = {"model": str(model), "status": "pending"}
        self.audit.append(record)
        try:
            if self.generation_filter is not None:
                first = next(iter(inspect.signature(self.generation_filter).parameters.values()))
                resolved = model.name if first.annotation is str else model
                result = await self.generation_filter(resolved, input, tools, tool_choice, config)
                if isinstance(result, ModelOutput):
                    record["status"] = "completed"
                    return result
                if isinstance(result, GenerateInput):
                    input, tools, tool_choice, config = result
            # Inspect owns retries, per-attempt deadlines, limits, and usage.
            with bridge_model_generate():
                if self.dev_routes:
                    output = await self._generate_dev(model, input, tools, tool_choice, config, record)
                else:
                    output = await model.generate(
                        input=input, tools=tools, tool_choice=tool_choice, config=config
                    )
            record["status"] = "completed"
            return output
        except Exception as exc:
            # Sample budget limits must retain Inspect's original semantics.
            from inspect_ai.util._limit import LimitExceededError
            if isinstance(exc, LimitExceededError):
                raise
            details = request_failure_details(exc, model=str(model))
            status = details["http_status"]
            record.update(status="failed", **details)
            attempts = record.get("provider_attempts", [])
            context = record.get("fallback_stop_reason", "configured retries exhausted")
            attempt_summary = "; ".join(
                f"{row['provider']}: {row.get('error_type', row['status'])}"
                f" (HTTP {row.get('http_status', 'n/a')}, source={row.get('error_source', 'unknown')})"
                for row in attempts
            )
            self.error = InfrastructureRequestError(
                f"Model request failed ({str(model)}; {context}): {failure_summary(details)}"
                + (f". Provider attempts: {attempt_summary}" if attempts else "")
                + (f". Unavailable endpoints: {', '.join(record['unavailable_providers'])}" if record.get("unavailable_providers") else "")
                + (f". Untried endpoints: {', '.join(record['untried_providers'])}" if record.get("untried_providers") else ""),
                status_code=status, details=details,
            )
            self.failed.set()
            raise self.error from exc
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)

    async def run_agent(self, execution):
        """Stop the scaffold when its model request has exhausted retries."""
        agent_task = asyncio.ensure_future(execution)
        failure_task = asyncio.create_task(self.failed.wait())
        try:
            await asyncio.wait((agent_task, failure_task), return_when=asyncio.FIRST_COMPLETED)
            if self.failed.is_set():
                assert self.error is not None
                raise self.error
            return await agent_task
        finally:
            for task in (agent_task, failure_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(agent_task, failure_task, return_exceptions=True)


def cli_error_event(stdout: str) -> str | None:
    import json
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            return str(event.get("error") or event)
    return None
