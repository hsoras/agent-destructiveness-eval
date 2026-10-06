#!/usr/bin/env python3
"""Run bounded awareness diagnostics from captured Inspect conversations."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

# Support both the pilot's module launch and direct CLI execution.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.probes import (
    CHECKPOINTS,
    DIAGNOSTIC_COST_BUDGET_USD,
    PROBE_PROMPTS,
    _sanitize,
    run_awareness_checkpoint,
)


def _parse_model_arg(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise ValueError(f"model argument must be KEY=VALUE: {value!r}")
    key, raw = value.split("=", 1)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return key, parsed


def _safe_settings(values: list[str]) -> dict[str, Any]:
    output = {}
    for value in values:
        key, parsed = _parse_model_arg(value)
        output[key] = parsed
    return _sanitize(output)


def _store_value(sample: Any, key: str) -> Any:
    store = getattr(sample, "store", None)
    if store is None:
        return None
    try:
        return store.get(key)
    except AttributeError:
        return store.get(key) if isinstance(store, dict) else None


def _probe_unavailable(checkpoint: str, mode: str, reason: str) -> dict[str, Any]:
    return {
        "checkpoint": checkpoint,
        "mode": mode,
        "prompt": PROBE_PROMPTS[mode],
        "status": "error",
        "error_type": "AwarenessPipelineError",
        "error": reason,
    }


def _sample_identity(sample: Any, index: int) -> dict[str, Any]:
    return {
        "sample_id": getattr(sample, "id", None) or f"sample-{index + 1}",
        "epoch": getattr(sample, "epoch", None),
        "sample_index": index,
        "eval_status": getattr(sample, "status", None),
    }


async def _run_sample_probes(
    capture: dict[str, Any], *, model: Any, cost_budget: float,
    elapsed_budget_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    start = time.monotonic()
    remaining_cost = cost_budget
    checkpoints = {}
    for checkpoint in CHECKPOINTS:
        elapsed = time.monotonic() - start
        if elapsed >= elapsed_budget_seconds:
            checkpoints[checkpoint] = {
                "checkpoint": checkpoint,
                "status": "unavailable",
                "reason": "separate awareness wall-clock budget exhausted",
                "probes": {
                    mode: _probe_unavailable(checkpoint, mode, "separate awareness wall-clock budget exhausted")
                    for mode in PROBE_PROMPTS
                },
            }
            continue
        try:
            result, remaining_cost = await run_awareness_checkpoint(
                capture,
                checkpoint=checkpoint,
                model=model,
                diagnostic_cost_remaining=remaining_cost,
            )
            checkpoints[checkpoint] = result
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            checkpoints[checkpoint] = {
                "checkpoint": checkpoint,
                "status": "error",
                "reason": f"diagnostic orchestration failed: {type(exc).__name__}: {exc}",
                "probes": {
                    mode: _probe_unavailable(
                        checkpoint, mode,
                        f"diagnostic orchestration failed: {type(exc).__name__}: {exc}",
                    )
                    for mode in PROBE_PROMPTS
                },
            }
    return checkpoints, {
        "budget_usd": cost_budget,
        "remaining_usd": max(0.0, remaining_cost),
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "elapsed_budget_seconds": elapsed_budget_seconds,
        "max_output_tokens_per_call": 512,
        "max_calls": 4,
        "per_call_timeout_seconds": 120,
    }


def _markdown(data: dict[str, Any]) -> str:
    lines = [
        "# Awareness diagnostics",
        "",
        "These are retrospective reports prompted after the original trajectory. "
        "Direct answers may be induced by the question and do not establish prior awareness.",
        "",
        f"Eval log: `{data.get('eval_log')}`",
        f"Model: `{data.get('model_route')}`",
        "",
    ]
    for key in ("read_error", "pipeline_error"):
        error = data.get(key)
        if error:
            lines += [f"Pipeline {key.replace('_', ' ')}: {error.get('type', 'error')}: {error.get('message', '')}", ""]
    if not data.get("samples"):
        lines += ["No readable sample records were found in this eval log.", ""]
    for sample in data.get("samples", []):
        ident = sample.get("identity", {})
        lines += [f"## Sample {ident.get('sample_id')} (epoch {ident.get('epoch')})", ""]
        for checkpoint in CHECKPOINTS:
            detail = sample.get("checkpoints", {}).get(checkpoint, {})
            lines += [f"### {checkpoint.replace('_', ' ').title()}", ""]
            lines.append(f"Status: **{detail.get('status', 'unavailable')}**")
            if detail.get("reason"):
                lines.append(f"Reason: {detail['reason']}")
            for limitation in detail.get("timing_limitations", []):
                lines.append(f"Timing limitation: {limitation}")
            for mode in PROBE_PROMPTS:
                probe = detail.get("probes", {}).get(mode, {})
                lines += ["", f"**{mode.title()}** — {probe.get('status', 'unavailable')}", ""]
                if probe.get("answer"):
                    lines.append(probe["answer"])
                elif probe.get("error") or probe.get("reason"):
                    lines.append(f"{probe.get('error') or probe.get('reason')}")
                else:
                    lines.append("No response was captured.")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def process_eval_log(
    eval_path: Path, *, model_route: str, model_args: list[str],
    original_settings: dict[str, Any] | None = None,
    model_cost_config: Path | None = None,
    cost_budget: float = DIAGNOSTIC_COST_BUDGET_USD,
    elapsed_budget_seconds: int = 600,
) -> tuple[Path, Path]:
    from inspect_ai.log import read_eval_log
    from inspect_ai.model import get_model

    eval_path = eval_path.resolve()
    json_path = eval_path.with_suffix(".awareness.json")
    markdown_path = eval_path.with_suffix(".awareness.md")
    data: dict[str, Any] = {
        "schema": "awareness-diagnostics-v1",
        "eval_log": str(eval_path),
        "model_route": model_route,
        "model_settings": _safe_settings(model_args),
        "original_generation_settings": _sanitize(original_settings or {}),
        "diagnostic_settings": {
            "generation": "ordinary Inspect model API call; no tools",
            "max_output_tokens_per_call": 512,
            "per_call_timeout_seconds": 120,
            "total_elapsed_budget_seconds": elapsed_budget_seconds,
            "cost_budget_usd_per_sample": cost_budget,
            "max_calls_per_sample": 4,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "samples": [],
    }
    log = None
    try:
        log = read_eval_log(str(eval_path))
    except Exception as exc:
        data["read_error"] = {"type": type(exc).__name__, "message": str(exc)}

    samples = getattr(log, "samples", None) or []
    try:
        if model_cost_config is not None:
            from inspect_ai.model._model_info import ModelCost, set_model_cost

            configured_costs = json.loads(model_cost_config.read_text(encoding="utf-8"))
            model_cost = configured_costs.get(model_route)
            if model_cost is None:
                raise ValueError(f"no model pricing found for {model_route}")
            set_model_cost(model_route, ModelCost(**model_cost))
        kwargs = dict(_parse_model_arg(arg) for arg in model_args)
        model = get_model(model_route, memoize=False, **kwargs)
    except Exception as exc:
        model = None
        model_error = f"could not initialize original model route for diagnostics: {type(exc).__name__}: {exc}"
    else:
        model_error = None

    async def run_all():
        for index, sample in enumerate(samples):
            capture = _store_value(sample, "awareness_capture") or {}
            item = {
                "identity": _sample_identity(sample, index),
                "capture": _sanitize(capture),
                "checkpoints": {},
            }
            if model_error:
                for checkpoint in CHECKPOINTS:
                    source = capture.get(checkpoint, {})
                    item["checkpoints"][checkpoint] = {
                        "checkpoint": checkpoint,
                        "status": source.get("status", "unavailable"),
                        "reason": model_error,
                        "snapshot": _sanitize(source.get("messages", [])),
                        "probes": {
                            mode: _probe_unavailable(checkpoint, mode, model_error)
                            for mode in PROBE_PROMPTS
                        },
                    }
                item["diagnostic_budget"] = {
                    "budget_usd": cost_budget, "remaining_usd": cost_budget,
                    "elapsed_seconds": 0, "elapsed_budget_seconds": elapsed_budget_seconds,
                    "max_output_tokens_per_call": 512, "max_calls": 4,
                    "per_call_timeout_seconds": 120,
                }
            else:
                checkpoints, budget = await _run_sample_probes(
                    capture, model=model, cost_budget=cost_budget,
                    elapsed_budget_seconds=elapsed_budget_seconds,
                )
                item["checkpoints"] = checkpoints
                item["diagnostic_budget"] = budget
            data["samples"].append(_sanitize(item))
            # Persist each sample promptly; an interruption must not discard
            # checkpoint snapshots or already completed diagnostic responses.
            json_path.write_text(json.dumps(_sanitize(data), indent=2) + "\n", encoding="utf-8")

    try:
        asyncio.run(run_all())
    except Exception as exc:
        data["pipeline_error"] = {"type": type(exc).__name__, "message": str(exc)}
    json_path.write_text(json.dumps(_sanitize(data), indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(data), encoding="utf-8")
    return json_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-log", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-arg", action="append", default=[])
    parser.add_argument("--original-settings-json", default="{}")
    parser.add_argument("--model-cost-config", type=Path)
    parser.add_argument("--cost-budget", type=float, default=DIAGNOSTIC_COST_BUDGET_USD)
    parser.add_argument("--elapsed-budget", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        original_settings = json.loads(args.original_settings_json)
        if not isinstance(original_settings, dict):
            raise ValueError("original settings must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        parser.error(f"invalid --original-settings-json: {exc}")
    json_path, markdown_path = process_eval_log(
        args.eval_log, model_route=args.model, model_args=args.model_arg,
        original_settings=original_settings,
        model_cost_config=args.model_cost_config,
        cost_budget=args.cost_budget, elapsed_budget_seconds=args.elapsed_budget,
    )
    complete = True
    for label, path in (("awareness JSON", json_path), ("awareness report", markdown_path)):
        if path.is_file():
            print(f"{label}: {path}")
        else:
            complete = False
            print(f"awareness diagnostics did not create {label}: {path}", file=sys.stderr)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
