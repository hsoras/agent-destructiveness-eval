"""Replay a logged request through pinned routes without running agent tools."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from inspect_ai.log import read_eval_log
from inspect_ai.log._condense import resolve_sample_attachments
from inspect_ai.model import ChatMessageUser, GenerateConfig, get_model
from inspect_ai.tool import ToolInfo, ToolParams, ToolParam
from scripts.pilot import openrouter_provider_arg, openrouter_dev_arg, fetch_openrouter_dev_routes
from eval.model_requests import request_failure_details, ModelRequestGuard


async def probe(args):
    load_dotenv(ROOT / ".env")
    if args.synthetic:
        # This prompt is self-contained and never reads any repository/log data.
        request = SimpleNamespace(
            model="openrouter/z-ai/glm-5.3-flash", timestamp=None, tools=[], tool_choice=None,
            input=[ChatMessageUser(content=(
                "Fix this small Python function and return only the corrected function. "
                "keep([1, 4, 5], 4) must return [4, 5].\n"
                "def keep(values, cutoff):\n    return [value for value in values if value > cutoff]\n"
            ))],
        )
        if args.synthetic_tools:
            request.input = [ChatMessageUser(content="Call bash with command 'printf PROBE_OK' and description 'Public tool probe'. Do not execute anything yourself.")]
            request.tools = [ToolInfo(name="bash", description="Describe a shell command to execute.",
                parameters=ToolParams(properties={
                    "command": ToolParam(type="string"),
                    "description": ToolParam(type="string"),
                    "timeout": ToolParam(type="number"),
                    "workdir": ToolParam(type="string"),
                }, required=["command", "description"]))]
            request.tool_choice = "auto"
    else:
        log = read_eval_log(args.log)
        sample = resolve_sample_attachments(log.samples[0], "full")
        requests = [event for event in sample.events if event.event == "model"]
        request = requests[-1]
    result = {
        "source_log": args.log,
        "source_request_timestamp": str(request.timestamp),
        "model": request.model,
        "probe_config": {
            "max_tokens": args.max_tokens,
            "attempt_timeout": args.attempt_timeout,
            "max_retries": args.max_retries if args.dev else 0,
            "calls_per_route": args.calls,
            "allow_fallbacks": args.dev,
            "strict_tools": False,
        },
        "note": (
            "Public synthetic function only; no logged or repository content sent."
            if args.synthetic else
            "Direct Inspect provider replay; no scaffold/tool execution. Short output cap differs from the pilot."
        ),
        "requests": [],
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    guard = ModelRequestGuard(dev_routes=fetch_openrouter_dev_routes(request.model)) if args.dev else None
    for provider in (["dev-cheapest-zdr"] if args.dev else args.providers):
        routing_arg = openrouter_dev_arg() if args.dev else openrouter_provider_arg(provider, privacy=args.privacy)
        routing = json.loads(routing_arg.split("=", 1)[1])
        config = GenerateConfig(
            timeout=args.attempt_timeout + 5, attempt_timeout=args.attempt_timeout,
            max_retries=args.max_retries if args.dev else 0, max_tokens=args.max_tokens, cache=False,
            parallel_tool_calls=False,
        )
        model = get_model(request.model, config=config, provider=routing, strict_tools=False)
        for index in range(args.calls):
            row = {"provider": provider, "call": index + 1}
            started = time.monotonic()
            try:
                if guard:
                    output = await guard.generate(model, request.input, request.tools, request.tool_choice, config)
                else:
                    output = await model.generate(
                        input=request.input, tools=request.tools, tool_choice=request.tool_choice,
                        config=config, cache=False,
                    )
                row.update(
                    status="completed", stop_reason=output.stop_reason,
                    usage=output.usage.model_dump() if output.usage else None,
                    tool_calls=[call.function for call in output.message.tool_calls or []],
                )
            except Exception as exc:
                row.update(status="failed", **request_failure_details(exc, model=request.model))
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if guard:
                row["provider_attempts"] = guard.audit[-1].get("provider_attempts", [])
            result["requests"].append(row)
            target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?")
    parser.add_argument("--synthetic", action="store_true", help="use a public synthetic prompt instead of exporting a logged request")
    parser.add_argument("--synthetic-tools", action="store_true", help="include optional tool arguments in the public synthetic request")
    parser.add_argument("--dev", action="store_true", help="test cheapest-first ZDR routing instead of pinned routes")
    parser.add_argument("--privacy", action="store_true", help="require ZDR for pinned routes")
    parser.add_argument("--providers", nargs="+", default=["deepinfra/fp4", "open-inference/fp4"])
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--attempt-timeout", type=int, default=45)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--output", default="analysis/model-route-probe.json")
    args = parser.parse_args()
    if bool(args.log) == args.synthetic:
        parser.error("provide a log path or --synthetic, exclusively")
    if args.synthetic_tools and not args.synthetic:
        parser.error("--synthetic-tools requires --synthetic")
    if min(args.calls, args.attempt_timeout, args.max_tokens) <= 0:
        parser.error("calls, attempt-timeout, and max-tokens must be positive")
    asyncio.run(probe(args))


if __name__ == "__main__":
    main()
