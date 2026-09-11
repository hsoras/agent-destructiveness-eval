"""Thin convenience wrapper around Inspect's native eval command.

Inspect remains responsible for sample execution, Docker sandboxes, epochs, and
log writing. This script only expands a small, repeatable pilot command.
"""

from __future__ import annotations

import argparse
import json
import random
import shlex
import shutil
import subprocess


def qualify_model(model: str, provider: str | None) -> str:
    """Add an Inspect provider prefix when the caller supplied one."""

    model = model.strip()
    if not model:
        raise ValueError("model must not be empty")
    if provider is None:
        return model

    provider = provider.strip().rstrip("/")
    if not provider:
        raise ValueError("provider must not be empty")
    prefix = f"{provider}/"
    return model if model.startswith(prefix) else prefix + model


def openrouter_provider_arg(
    provider: str,
    quantizations: list[str] | None = None,
    privacy: bool = False,
) -> str:
    """Build Inspect's OpenRouter routing argument with no fallback.

    OpenRouter represents the upstream provider and quantization as separate
    fields. For convenience, ``provider/quantization`` is also accepted as a
    shorthand (for example, ``deepinfra/fp4``).
    """

    provider = provider.strip()
    if not provider:
        raise ValueError("OpenRouter provider must not be empty")

    requested_quantizations = list(quantizations or [])
    if "/" in provider:
        provider_name, shorthand_quantization = provider.split("/", 1)
        if not provider_name or not shorthand_quantization:
            raise ValueError("OpenRouter provider shorthand must be PROVIDER/QUANTIZATION")
        if requested_quantizations:
            raise ValueError(
                "do not combine PROVIDER/QUANTIZATION shorthand with --quantization"
            )
        provider = provider_name
        requested_quantizations = [shorthand_quantization]

    requested_quantizations = [item.strip() for item in requested_quantizations]
    if any(not item for item in requested_quantizations):
        raise ValueError("OpenRouter quantization must not be empty")

    routing = {"order": [provider], "allow_fallbacks": False}
    if requested_quantizations:
        routing["quantizations"] = requested_quantizations
    if privacy:
        routing["data_collection"] = "deny"
        routing["zdr"] = True
    return "provider=" + json.dumps(routing, separators=(",", ":"))


def openrouter_privacy_arg() -> str:
    """Build OpenRouter routing filters without pinning a provider."""

    return "provider=" + json.dumps(
        {"data_collection": "deny", "zdr": True},
        separators=(",", ":"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        help="model id; use a fully-qualified Inspect name unless --provider is set",
    )
    parser.add_argument(
        "--provider",
        "--openrouter-provider",
        dest="openrouter_provider",
        help="pin the upstream OpenRouter provider, e.g. anthropic or google-vertex",
    )
    parser.add_argument(
        "--inspect-provider",
        help="Inspect API provider prefix for non-OpenRouter use, e.g. openai",
    )
    parser.add_argument(
        "--model-arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="provider-specific Inspect model argument; may be repeated",
    )
    parser.add_argument(
        "--quantization",
        action="append",
        default=[],
        metavar="NAME",
        help="OpenRouter quantization filter; may be repeated, e.g. fp4",
    )
    parser.add_argument(
        "--privacy",
        action="store_true",
        help="require no provider data collection and Zero Data Retention",
    )
    parser.add_argument("--runs", type=int, default=1, help="independent epochs per tier")
    parser.add_argument(
        "--difficulty",
        choices=("tier1", "tier2", "tier3", "all"),
        default="all",
    )
    parser.add_argument(
        "--order-seed",
        type=int,
        default=None,
        help="deterministically shuffle all-tier sample order with this seed",
    )
    parser.add_argument("--log-dir", default="logs/pilot")
    parser.add_argument("--max-sandboxes", type=int, default=None)
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="maximum retries for a failed model API request (default: 3)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="model API request timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--turn-limit",
        type=int,
        default=30,
        help="maximum model generations per sample (default: 30)",
    )
    parser.add_argument(
        "--token-limit",
        default=None,
        help="override the task's tier-specific token limit for every sample",
    )
    parser.add_argument(
        "--keep-control",
        action="store_true",
        help="keep Inspect's control process alive after completion for inspection",
    )
    parser.add_argument(
        "--log-buffer",
        type=int,
        default=1,
        help="number of samples to buffer before writing log events (default: 1)",
    )
    args = parser.parse_args()

    try:
        if args.openrouter_provider:
            model = qualify_model(args.model, "openrouter")
        else:
            model = qualify_model(args.model, args.inspect_provider)
    except ValueError as exc:
        parser.error(str(exc))

    if args.openrouter_provider:
        if any(argument.strip().startswith("provider=") for argument in args.model_arg):
            parser.error("use --provider or --model-arg provider=..., not both")
        try:
            args.model_arg.insert(
                0,
                openrouter_provider_arg(
                    args.openrouter_provider,
                    args.quantization,
                    privacy=args.privacy,
                ),
            )
        except ValueError as exc:
            parser.error(str(exc))
    elif args.quantization:
        parser.error("--quantization requires --provider/--openrouter-provider")
    elif args.privacy:
        if args.inspect_provider and args.inspect_provider != "openrouter":
            parser.error("--privacy requires OpenRouter; do not combine it with another --inspect-provider")
        if any(argument.strip().startswith("provider=") for argument in args.model_arg):
            parser.error("use --privacy or --model-arg provider=..., not both")
        args.model_arg.insert(0, openrouter_privacy_arg())

    inspect_cli = shutil.which("inspect")
    if inspect_cli is None:
        parser.error("Inspect CLI not found; install the project environment first")

    if args.difficulty == "all":
        task_targets = [
            "eval/task.py@streamstats_tier1",
            "eval/task.py@streamstats_tier2",
            "eval/task.py@streamstats_tier3",
        ]
        if args.order_seed is not None:
            random.Random(args.order_seed).shuffle(task_targets)
    else:
        task_targets = ["eval/task.py@streamstats_debug"]

    command = [inspect_cli, "eval", *task_targets]
    if args.difficulty != "all":
        command.extend(["-T", f"difficulty={args.difficulty}"])
    command.extend([
        "--model",
        model,
        "--epochs",
        str(args.runs),
        "--log-dir",
        args.log_dir,
        "--ctl-server",
        "keep" if args.keep_control else "true",
        "--log-buffer",
        str(args.log_buffer),
        "--max-retries",
        str(args.max_retries),
        "--timeout",
        str(args.timeout),
        "--turn-limit",
        str(args.turn_limit),
    ])
    if args.token_limit is not None:
        command.extend(["--token-limit", args.token_limit])
    if args.order_seed is not None:
        command.extend(["-T", f"order_seed={args.order_seed}"])
    for model_arg in args.model_arg:
        command.extend(["-M", model_arg])
    if args.max_sandboxes is not None:
        command.extend(["--max-sandboxes", str(args.max_sandboxes)])

    print("$ " + shlex.join(command))
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
