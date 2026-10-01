"""Thin convenience wrapper around Inspect's native eval command.

Inspect remains responsible for sample execution, Docker sandboxes, epochs, and
log writing. This script only expands a small, repeatable pilot command.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


CALIBRATION_DIFFICULTIES = ("calibration_a", "calibration_b", "calibration_c")
DIFFICULTIES = ("tier0", "tier1", "tier2", "tier3")
SCENARIO_CHOICES = (
    "development container",
    "blocker",
    "no blocker",
    "synthetic blocker",
    "sqlite migration",
)
SQLITE_CONDITIONS = ("defect-blocker", "clean-blocker", "clean-capacity")
ISOLATION_ENV_OVERRIDES = (
    "INSPECT_EVAL_NO_SANDBOX_CLEANUP",
    "INSPECT_EVAL_CHECKPOINT",
)


def resolve_inspect_command(project_root: Path) -> list[str] | None:
    """Select an Inspect CLI that can import the pinned OpenCode adapter.

    It is common for ``inspect`` to resolve to an older pyenv environment while
    the repository's locked dependencies live in ``.venv``. In that case use
    ``uv run`` for the child CLI so task loading and solver construction happen
    in the same environment. This keeps ``python scripts/pilot.py`` reliable
    without mutating a global interpreter.
    """

    try:
        import inspect_swe  # noqa: F401
    except ImportError:
        inspect_swe_available = False
    else:
        inspect_swe_available = True

    interpreter_cli = Path(sys.executable).with_name("inspect")
    if inspect_swe_available and interpreter_cli.is_file():
        return [str(interpreter_cli)]

    uv = shutil.which("uv")
    local_cli = project_root / ".venv" / "bin" / "inspect"
    if uv and local_cli.is_file():
        return [uv, "run", "--frozen", "inspect"]

    if inspect_swe_available:
        inspect_cli = shutil.which("inspect")
        if inspect_cli:
            return [inspect_cli]
    return None

def fresh_eval_environment(
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an eval environment that cannot inherit cross-run state settings."""

    environment = dict(os.environ if base_environment is None else base_environment)
    for variable in ISOLATION_ENV_OVERRIDES:
        environment.pop(variable, None)
    # Inspect's generation cache is distinct from provider prompt caching. The
    # former can replay a previous answer, so it is disabled for every pilot.
    environment["INSPECT_EVAL_CACHE"] = "false"
    return environment


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


def openrouter_dev_arg(quantizations: list[str] | None = None) -> str:
    """Try eligible ZDR endpoints in price order, with provider failover."""
    routing = {
        "sort": "price", "allow_fallbacks": False,
        "data_collection": "deny", "zdr": True,
    }
    if quantizations:
        routing["quantizations"] = quantizations
    return "provider=" + json.dumps(routing, separators=(",", ":"))


def fetch_openrouter_dev_routes(model: str, quantizations=None, *, opener=urlopen) -> list[str]:
    """Order active tool-capable endpoints by prompt plus completion price."""
    model_id = _openrouter_model_id(model)
    request = Request(
        f"https://openrouter.ai/api/v1/models/{quote(model_id, safe='/')}/endpoints",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with opener(request, timeout=10) as response:
            endpoints = json.loads(response.read())["data"]["endpoints"]
        candidates = [e for e in endpoints if e.get("status", 0) == 0
            and "tools" in e.get("supported_parameters", [])
            and (not quantizations or e.get("quantization") in quantizations)]
        candidates.sort(key=lambda e: (
            float(e["pricing"]["prompt"]) + float(e["pricing"]["completion"]), e["tag"]
        ))
        routes = list(dict.fromkeys(e["tag"] for e in candidates))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("could not retrieve OpenRouter dev endpoint ordering") from exc
    if not routes:
        raise ValueError("no active tool-capable endpoints match the dev filters")
    # Catalog ordering is not a privacy assertion: every actual request must
    # still carry deny + ZDR; OpenRouter rejects ineligible pinned endpoints.
    return routes


def _openrouter_model_id(model: str) -> str:
    """Return the OpenRouter catalog id without Inspect's provider prefix."""

    return model.strip().removeprefix("openrouter/")


def _openrouter_provider_name(provider: str) -> str:
    """Return the upstream name from PROVIDER or PROVIDER/QUANTIZATION."""

    return provider.strip().split("/", 1)[0]


def _model_cost_from_catalog(pricing: Mapping[str, object]) -> dict[str, float]:
    """Convert OpenRouter's per-token pricing object to Inspect's $/million form."""

    try:
        input_price = float(pricing["prompt"])
        output_price = float(pricing["completion"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("OpenRouter returned incomplete model pricing data") from exc

    def optional_price(name: str) -> float:
        raw = pricing.get(name, "0")
        try:
            return float(raw) * 1_000_000
        except (TypeError, ValueError) as exc:
            raise ValueError(f"OpenRouter returned invalid {name} pricing") from exc

    values = {
        "input": input_price * 1_000_000,
        "output": output_price * 1_000_000,
        "input_cache_write": optional_price("input_cache_write"),
        "input_cache_read": optional_price("input_cache_read"),
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("OpenRouter returned negative model pricing")
    return values


def fetch_openrouter_model_cost(
    model: str,
    *,
    opener: Callable[..., object] = urlopen,
    timeout: int = 10,
) -> dict[str, float]:
    """Fetch fallback model pricing from OpenRouter's public model API."""

    model_id = _openrouter_model_id(model)
    try:
        author, slug = model_id.split("/", 1)
    except ValueError as exc:
        raise ValueError(
            f"OpenRouter model must look like author/slug, got {model_id!r}"
        ) from exc
    url = (
        "https://openrouter.ai/api/v1/model/"
        f"{quote(author, safe='')}/{quote(slug, safe=':')}"
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "streamstats-inspect-eval/0.1",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not retrieve pricing for OpenRouter model {model_id!r}; "
            "pass --model-cost-config with a local pricing snapshot"
        ) from exc

    try:
        pricing = payload["data"]["pricing"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"OpenRouter returned no pricing for model {model_id!r}; "
            "pass --model-cost-config with a local pricing snapshot"
        ) from exc
    if not isinstance(pricing, Mapping):
        raise ValueError("OpenRouter returned an invalid model pricing object")
    return _model_cost_from_catalog(pricing)


def openrouter_model_cost(
    model: str,
    provider: str | None,
    *,
    fetcher: Callable[[str], dict[str, float]] | None = None,
) -> tuple[dict[str, float], str]:
    """Refresh a cost table for an OpenRouter model and describe its source."""

    resolver = fetcher or fetch_openrouter_model_cost
    cost = resolver(model)
    provider_note = (
        f" for pinned route {_openrouter_provider_name(provider)}"
        if provider
        else ""
    )
    return cost, f"fresh OpenRouter public model catalog lookup{provider_note}"


def write_model_cost_config(
    path: Path,
    model: str,
    cost: Mapping[str, float],
) -> None:
    """Write the JSON shape accepted by Inspect's --model-cost-config."""

    path.write_text(json.dumps({model: dict(cost)}, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dev", action="store_true",
        help="cheapest-first OpenRouter ZDR routing with fallbacks and a $0.20 per-sample cost limit",
    )
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
        "--probe",
        choices=("indirect", "direct", "all"),
        default=None,
        help="tool-free follow-up after completion; all launches independent indirect and direct runs",
    )
    parser.add_argument(
        "--difficulty",
        choices=(*DIFFICULTIES, "all", *CALIBRATION_DIFFICULTIES),
        default="all",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIO_CHOICES,
        default="development container",
        help="scenario label (default: development container)",
    )
    parser.add_argument(
        "--condition",
        choices=SQLITE_CONDITIONS,
        default="defect-blocker",
        help="SQLite migration condition (default: defect-blocker)",
    )
    parser.add_argument(
        "--blocker",
        action="store_true",
        help="select the preserved blocker baseline",
    )
    parser.add_argument(
        "--prompt-policy",
        choices=("none", "scope", "strong"),
        default="none",
        help="legacy compatibility selector; blocker prompts remain neutral (default: none)",
    )
    parser.add_argument(
        "--interleaved",
        action="store_true",
        help="run all tiers as one counterbalanced dataset task",
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
        default=5,
        help="maximum retries for a failed model API request (default: 5; additional endpoints in --dev)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="total model request retry budget in seconds (default: 900)",
    )
    parser.add_argument(
        "--attempt-timeout",
        type=int,
        default=180,
        help="hard deadline for one model API attempt in seconds (default: 180)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=8192,
        help="maximum completion tokens per model call (default: 8192)",
    )
    parser.add_argument(
        "--turn-limit",
        type=int,
        default=100,
        help="maximum model generations per sample (default: 100)",
    )
    parser.add_argument(
        "--cost-limit",
        type=float,
        default=None,
        help="override the task cost limit for every sample, in USD (for example: 0.025)",
    )
    parser.add_argument(
        "--model-cost-config",
        type=Path,
        default=None,
        help="Inspect YAML/JSON pricing file; otherwise resolve OpenRouter pricing automatically",
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

    if args.dev:
        if any(argument.strip().startswith("strict_tools=") for argument in args.model_arg):
            parser.error("--dev uses strict_tools=false to preserve OpenCode's optional tool arguments; omit strict_tools overrides")
        if args.openrouter_provider or any(
            argument.strip().startswith("provider=") for argument in args.model_arg
        ):
            parser.error("--dev selects providers automatically; omit --provider and provider= model arguments")
        if args.inspect_provider and args.inspect_provider != "openrouter":
            parser.error("--dev requires OpenRouter")
        if args.cost_limit is not None and args.cost_limit > 0.20:
            parser.error("--dev cost limit cannot exceed $0.20 per sample")
        if args.cost_limit is None:
            args.cost_limit = 0.20
        args.inspect_provider = "openrouter"

    if args.blocker:
        args.scenario = "blocker"
    if args.scenario == "sqlite migration" and args.difficulty not in {"all", "tier1"}:
        parser.error("--difficulty does not apply to sqlite migration; use --condition")

    if args.cost_limit is not None and args.cost_limit <= 0:
        parser.error("--cost-limit must be greater than zero")
    if args.attempt_timeout <= 0:
        parser.error("--attempt-timeout must be greater than zero")
    if args.max_retries < 0:
        parser.error("--max-retries must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be greater than zero")

    try:
        if args.openrouter_provider:
            model = qualify_model(args.model, "openrouter")
        else:
            model = qualify_model(args.model, args.inspect_provider)
    except ValueError as exc:
        parser.error(str(exc))

    if not model.startswith("openrouter/"):
        parser.error(
            "this harness runs OpenCode through the Inspect model bridge; "
            "--model must be an OpenRouter route (for example z-ai/glm-5.3-flash)"
        )

    if args.dev:
        try:
            dev_routes = fetch_openrouter_dev_routes(model, args.quantization)
        except ValueError as exc:
            parser.error(str(exc))
        args.model_arg.insert(0, openrouter_dev_arg(args.quantization))
        # OpenCode's native tools include optional properties. The compatible
        # provider defaults to strict=True without converting their schemas,
        # which strict validators reject before the agent can begin.
        args.model_arg.append("strict_tools=false")
        print("dev routing: cheapest-first, ZDR only, explicit endpoint failover; sample budget $" + str(args.cost_limit))
        print("dev endpoint order: " + ", ".join(dev_routes))
    elif args.openrouter_provider:
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

    inspect_command = resolve_inspect_command(Path(__file__).resolve().parents[1])
    if inspect_command is None:
        parser.error(
            "Inspect SWE is unavailable in the active interpreter and no synced "
            "project .venv was found; run `uv sync --frozen` first"
        )

    task_prefixes = {
        "development container": "streamstats_development_",
        "blocker": "streamstats_blocker_",
        "no blocker": "streamstats_no_blocker_",
        "synthetic blocker": "streamstats_synthetic_blocker_",
    }
    if args.scenario == "sqlite migration":
        task_targets = ["eval/task.py@sqlite_migration"]
        command_prompt_policy = ["-T", f"condition={args.condition}"]
    else:
        task_prefix = task_prefixes[args.scenario]
    if args.scenario != "sqlite migration" and args.difficulty == "all" and args.interleaved:
        task_targets = [f"eval/task.py@{task_prefix}debug"]
    elif args.scenario != "sqlite migration" and args.difficulty == "all":
        task_targets = [
            f"eval/task.py@{task_prefix}tier0",
            f"eval/task.py@{task_prefix}tier1",
            f"eval/task.py@{task_prefix}tier2",
            f"eval/task.py@{task_prefix}tier3",
        ]
        if args.order_seed is not None:
            random.Random(args.order_seed).shuffle(task_targets)
    elif args.scenario != "sqlite migration" and args.difficulty in CALIBRATION_DIFFICULTIES:
        task_targets = [f"eval/task.py@{task_prefix}{args.difficulty}"]
    elif args.scenario != "sqlite migration":
        task_targets = [f"eval/task.py@{task_prefix}debug"]

    if args.scenario in {"blocker", "synthetic blocker"}:
        command_prompt_policy = ["-T", f"prompt_policy={args.prompt_policy}"]
    elif args.scenario != "sqlite migration":
        command_prompt_policy = []

    temporary_cost_dir: tempfile.TemporaryDirectory[str] | None = None
    model_cost_config = args.model_cost_config
    pricing_source: str | None = None
    if model_cost_config is None and model.startswith("openrouter/"):
        try:
            cost, pricing_source = openrouter_model_cost(
                model,
                args.openrouter_provider,
            )
        except ValueError as exc:
            parser.error(str(exc))
        temporary_cost_dir = tempfile.TemporaryDirectory(prefix="streamstats-model-cost-")
        model_cost_config = Path(temporary_cost_dir.name) / "model-costs.json"
        write_model_cost_config(model_cost_config, model, cost)
        print(f"pricing: {pricing_source}")

    command = [*inspect_command, "eval", *task_targets]
    command.extend(command_prompt_policy)
    if args.difficulty in (*CALIBRATION_DIFFICULTIES, *DIFFICULTIES):
        if args.difficulty in DIFFICULTIES:
            command.extend(["-T", f"difficulty={args.difficulty}"])
    elif args.interleaved:
        command.extend(["-T", "difficulty=all"])
    if args.difficulty == "all" and args.interleaved:
        # A serial sample queue makes the seeded order an actual execution
        # order while retaining a fresh sandbox and conversation per sample.
        command.extend(["--max-samples", "1"])
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
        "--attempt-timeout",
        str(args.attempt_timeout),
        "--max-tokens",
        str(args.max_tokens),
        "--turn-limit",
        str(args.turn_limit),
        "--cache=false",
    ])
    if args.cost_limit is not None:
        command.extend(["--cost-limit", str(args.cost_limit)])
    if model_cost_config is not None:
        command.extend(["--model-cost-config", str(model_cost_config)])
    if args.order_seed is not None and args.difficulty in (
        "all",
        "tier0",
        "tier1",
        "tier2",
        "tier3",
    ):
        command.extend(["-T", f"order_seed={args.order_seed}"])
    for model_arg in args.model_arg:
        command.extend(["-M", model_arg])
    if args.max_sandboxes is not None:
        command.extend(["--max-sandboxes", str(args.max_sandboxes)])

    environment = fresh_eval_environment()
    environment.pop("STREAMSTATS_PROBE", None)
    environment.pop("STREAMSTATS_DEV_ROUTES", None)
    if args.dev:
        environment["STREAMSTATS_DEV_ROUTES"] = json.dumps(dev_routes)
    # The task solver receives the same fully-qualified route that Inspect uses
    # for pricing and model generation. OpenCode's provider name selects its
    # request format; the Inspect bridge still owns the actual OpenRouter call.
    environment["STREAMSTATS_OPENCODE_MODEL"] = model
    if args.scenario == "sqlite migration":
        project_root = Path(__file__).resolve().parents[1]
        environment["SQLITE_MIGRATION_ARTIFACT_ROOT"] = str(
            Path(os.environ.get(
                "SQLITE_MIGRATION_ARTIFACT_ROOT",
                project_root / ".scenario" / "sqlite-migration-artifacts",
            )).resolve()
        )
        environment["SQLITE_MIGRATION_CONDITION"] = args.condition
        environment["SQLITE_MIGRATION_CAPACITY"] = (
            "3072m" if args.condition == "clean-capacity" else "2048m"
        )
    try:
        probe_modes = ("indirect", "direct") if args.probe == "all" else (args.probe,)
        exit_code = 0
        for probe_mode in probe_modes:
            run_command = command.copy()
            run_environment = environment.copy()
            if probe_mode is not None:
                run_environment["STREAMSTATS_PROBE"] = probe_mode
            if args.probe == "all":
                run_command[run_command.index("--log-dir") + 1] = str(
                    Path(args.log_dir) / probe_mode
                )
                print(f"probe: {probe_mode}", flush=True)
            print("$ " + shlex.join(run_command), flush=True)
            result = subprocess.run(run_command, check=False, env=run_environment)
            # Still run the other probe after a failed evaluation, and report
            # the first nonzero exit code once both independent runs finish.
            exit_code = exit_code or result.returncode
        return exit_code
    finally:
        if temporary_cost_dir is not None:
            temporary_cost_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
