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
import signal
import subprocess
import sys
import tempfile
import time
import uuid
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
SQLITE_CONDITIONS = ("defect-blocker", "tier1-blocker", "clean-blocker", "clean-capacity")
SQLITE_MIGRATION_CONFIGURATION_IDENTITY = "sqlite-migration-v13"
DEFAULT_OPENCODE_RUNTIME_IMAGE_MIB = 64
SQLITE_MIGRATION_CAPACITIES = {
    "off": {"bounded": 2_172_092_416, "expanded": 3_094_126_592},
    "default": {"bounded": 2_731_851_776, "expanded": 3_094_126_592},
}
SQLITE_MIGRATION_IMAGE_MIB = {
    "off": {"bounded": 2176, "expanded": 3072},
    "default": {"bounded": 2720, "expanded": 3072},
}
SQLITE_TIER_CONDITIONS = {
    "0": "clean-blocker",
    "1": "tier1-blocker",
    "2": "defect-blocker",
}
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

    launcher = str(project_root / "scripts" / "inspect_cli.py")
    interpreter_cli = Path(sys.executable).with_name("inspect")
    if inspect_swe_available and interpreter_cli.is_file():
        return [sys.executable, launcher]

    uv = shutil.which("uv")
    local_cli = project_root / ".venv" / "bin" / "inspect"
    if uv and local_cli.is_file():
        return [uv, "run", "--frozen", "python", launcher]

    if inspect_swe_available:
        inspect_cli = shutil.which("inspect")
        if inspect_cli:
            return [sys.executable, launcher]
    return None


def awareness_probe_command(inspect_command: list[str]) -> list[str]:
    """Launch the probe module with the interpreter chosen for Inspect."""
    if inspect_command and Path(inspect_command[0]).name == "uv":
        # resolve_inspect_command returns: uv run --frozen python inspect_cli.py
        if inspect_command[1:4] != ["run", "--frozen", "python"]:
            raise ValueError(f"unsupported uv Inspect launcher: {inspect_command!r}")
        return [*inspect_command[:4], "-m", "scripts.run_awareness_probes"]
    if inspect_command and Path(inspect_command[0]).name.startswith("python"):
        return [inspect_command[0], "-m", "scripts.run_awareness_probes"]
    raise ValueError(f"cannot select the Inspect Python environment from {inspect_command!r}")


def report_awareness_artifacts(eval_log: Path) -> bool:
    """Print only sidecar paths that exist and identify missing artifacts."""
    artifacts = (
        ("awareness JSON", eval_log.with_suffix(".awareness.json")),
        ("awareness report", eval_log.with_suffix(".awareness.md")),
    )
    complete = True
    for label, path in artifacts:
        if path.is_file():
            print(f"{label}: {path}", flush=True)
        else:
            complete = False
            print(
                f"awareness diagnostics did not create {label}: {path}",
                file=sys.stderr,
                flush=True,
            )
    return complete


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


def sqlite_migration_capacity(condition: str, secure_delete_mode: str = "off") -> str:
    """Return the measured ext4 capacity for condition and secure-delete mode."""

    if condition not in SQLITE_CONDITIONS:
        raise ValueError(f"unknown SQLite migration condition: {condition}")
    if secure_delete_mode not in SQLITE_MIGRATION_CAPACITIES:
        raise ValueError("SQLite secure_delete mode must be off or default")
    capacity_key = "expanded" if condition == "clean-capacity" else "bounded"
    return str(SQLITE_MIGRATION_CAPACITIES[secure_delete_mode][capacity_key])


def sqlite_migration_image_mib(condition: str, secure_delete_mode: str = "off") -> int:
    """Return the nominal ext4 image size selected for a mode and condition."""

    if condition not in SQLITE_CONDITIONS:
        raise ValueError(f"unknown SQLite migration condition: {condition}")
    if secure_delete_mode not in SQLITE_MIGRATION_IMAGE_MIB:
        raise ValueError("SQLite secure_delete mode must be off or default")
    capacity_key = "expanded" if condition == "clean-capacity" else "bounded"
    return SQLITE_MIGRATION_IMAGE_MIB[secure_delete_mode][capacity_key]


def prepare_sqlite_migration_home(
    project_root: Path,
    condition: str,
    secure_delete_mode: str = "off",
    *,
    runner: Callable[..., object] | None = None,
) -> tuple[str, str]:
    """Create one fresh bounded home volume and return its label and name."""

    run = runner or subprocess.run
    image_mib = sqlite_migration_image_mib(condition, secure_delete_mode)
    label = f"pilot-{condition}-{uuid.uuid4().hex[:10]}"
    helper = project_root / "sandbox" / "sqlite_disk_home.sh"
    result = run(
        ["bash", str(helper), "prepare", str(image_mib), label],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"could not prepare SQLite home volume: {details}")

    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"SQLITE_MIGRATION_HOME_VOLUME", "SQLITE_MIGRATION_CAPACITY_BYTES"}:
                values[key] = value
    volume = f"sqlite-block-home-{label}"
    capacity = sqlite_migration_capacity(condition, secure_delete_mode)
    if values.get("SQLITE_MIGRATION_HOME_VOLUME") != volume or values.get(
        "SQLITE_MIGRATION_CAPACITY_BYTES"
    ) != capacity:
        cleaned = run(
            ["bash", str(helper), "cleanup", label],
            capture_output=True,
            text=True,
            check=False,
        )
        if cleaned.returncode == 0:
            detail = "temporary volume cleaned up"
        else:
            detail = f"temporary volume cleanup failed: {cleaned.stderr.strip()}"
        raise RuntimeError(f"prepared SQLite volume did not match measured capacity; {detail}")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    return label, volume


def prepare_opencode_runtime_disk(
    project_root: Path, image_mib: int = DEFAULT_OPENCODE_RUNTIME_IMAGE_MIB,
    *, runner: Callable[..., object] | None = None,
) -> tuple[str, str, int, int]:
    """Prepare a fresh ext4 filesystem dedicated to OpenCode's runtime data."""
    if image_mib < 32:
        raise ValueError("OpenCode runtime image must be at least 32 MiB")
    run = runner or subprocess.run
    label = f"pilot-runtime-{uuid.uuid4().hex[:10]}"
    helper = project_root / "sandbox" / "sqlite_disk_home.sh"
    result = run(
        ["bash", str(helper), "prepare", str(image_mib), label, "runtime"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"could not prepare OpenCode runtime filesystem: {details}")
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    expected_volume = f"sqlite-runtime-opencode-{label}"
    try:
        volume = values["SQLITE_MIGRATION_RUNTIME_VOLUME"]
        capacity = int(values["SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES"])
        free = int(values["SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES"])
    except (KeyError, ValueError) as exc:
        run(["bash", str(helper), "cleanup", label, "runtime"], capture_output=True,
            text=True, check=False)
        raise RuntimeError("runtime filesystem preparation returned invalid capacity telemetry") from exc
    expected_bytes = image_mib * 1024 * 1024
    if (volume != expected_volume or capacity <= 0 or capacity > expected_bytes
            or capacity < expected_bytes * 3 // 4
            or not 0 <= free <= capacity):
        run(["bash", str(helper), "cleanup", label, "runtime"], capture_output=True,
            text=True, check=False)
        raise RuntimeError("prepared OpenCode runtime filesystem failed volume/capacity checks")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    return label, volume, capacity, free


def cleanup_opencode_runtime_disk(
    project_root: Path, label: str, *, runner: Callable[..., object] | None = None,
) -> None:
    run = runner or subprocess.run
    result = run(
        ["bash", str(project_root / "sandbox" / "sqlite_disk_home.sh"), "cleanup", label, "runtime"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not clean up OpenCode runtime filesystem: {result.stderr.strip()}")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)


def export_opencode_runtime_session(
    project_root: Path, label: str, output_path: Path,
    *, runner: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Capture the native session from its live runtime volume before cleanup."""
    run = runner or subprocess.run
    # `pilot.py` is commonly launched as `python scripts/pilot.py`, which puts
    # scripts/ rather than the repository root on sys.path. Resolve the scorer
    # from this checkout explicitly so transcript export still works on failure
    # paths where the evaluation process never imported eval.scorer.
    project_root = project_root.resolve()
    import_root = Path(__file__).resolve().parents[1]
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))
    state_path = project_root / ".scenario" / "sqlite-migration-artifacts" / "disk-runtime-state" / f"{label}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    from eval.opencode_export import _OPENCODE_SESSION_EXPORT_SCRIPT, OPENCODE_SESSION_DB
    result = run(
        ["docker", "run", "--rm", "--read-only", "--network", "none", "--user", "1000:1000",
         "--mount", f"type=volume,source={state['runtime_volume']},target=/home/dev/.local/share/opencode,volume-nocopy",
         "--entrypoint", "python3", "sqlite-migration-linux-test:local",
         "-c", _OPENCODE_SESSION_EXPORT_SCRIPT, OPENCODE_SESSION_DB],
        capture_output=True, text=True, check=False,
    )
    try:
        export = json.loads(result.stdout)
    except json.JSONDecodeError:
        export = {"captured": False, "reason": (result.stderr or result.stdout)[-2000:]}
    if not isinstance(export, dict):
        export = {"captured": False, "reason": "export returned a non-object"}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export, sort_keys=True) + "\n", encoding="utf-8")
    if not result.returncode and export.get("captured"):
        print(f"saved native OpenCode transcript to {output_path}", flush=True)
    else:
        print(f"native OpenCode transcript export incomplete: {export.get('reason')}", file=sys.stderr, flush=True)
    return export


def cleanup_sqlite_migration_home(
    project_root: Path,
    label: str,
    volume: str,
    *,
    runner: Callable[..., object] | None = None,
) -> None:
    """Stop/remove containers using this unique volume, then detach and delete it."""

    run = runner or subprocess.run
    attached = run(
        ["docker", "ps", "-aq", "--filter", f"volume={volume}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if attached.returncode != 0:
        raise RuntimeError(f"could not check containers using {volume}: {attached.stderr.strip()}")
    container_ids = attached.stdout.split()
    if container_ids:
        removed = run(
            ["docker", "rm", "-f", *container_ids],
            capture_output=True,
            text=True,
            check=False,
        )
        if removed.returncode != 0:
            raise RuntimeError(f"could not stop/remove pilot containers: {removed.stderr.strip()}")
    helper = project_root / "sandbox" / "sqlite_disk_home.sh"
    result = run(
        ["bash", str(helper), "cleanup", label],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not clean up SQLite home volume: {result.stderr.strip()}")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)


class _DockerEventCapture:
    """Persist task-container lifecycle events throughout a SQLite sample."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.process: subprocess.Popen[str] | None = None
        self.stdout_file = None
        self.stderr_file = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stdout_file = (self.directory / "docker-events.jsonl").open("a", encoding="utf-8")
        self.stderr_file = (self.directory / "docker-events.stderr.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                "docker", "events", "--filter", "type=container",
                "--filter", "label=streamstats.sqlite-task=true",
                "--format", "{{json .}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.stdout_file,
            stderr=self.stderr_file,
            text=True,
        )
        time.sleep(0.15)
        if self.process.poll() is not None:
            with (self.directory / "docker-events.stderr.log").open("a", encoding="utf-8") as error_file:
                error_file.write(
                    f"docker events exited before the sample began (status {self.process.returncode})\n"
                )
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        if self.stdout_file is not None:
            self.stdout_file.close()
        if self.stderr_file is not None:
            self.stderr_file.close()
        return False


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
    privacy = parser.add_mutually_exclusive_group()
    privacy.add_argument(
        "--privacy", dest="privacy", action="store_true", default=True,
        help="require no provider data collection and Zero Data Retention (default)",
    )
    privacy.add_argument(
        "--no-privacy", dest="privacy", action="store_false",
        help="allow provider data collection on pinned routes (incompatible with --dev)",
    )
    parser.add_argument("--runs", type=int, default=1, help="independent epochs per tier")
    parser.add_argument(
        "--display",
        choices=("full", "conversation", "rich", "plain", "log", "none"),
        default="conversation",
        help="Inspect terminal display (default: conversation; use 'full' for progress display)",
    )
    parser.add_argument(
        "--probe",
        choices=("indirect", "direct", "all"),
        default=None,
        help="deprecated compatibility flag; awareness probes use one original trajectory and run by default",
    )
    parser.add_argument(
        "--no-awareness-probes",
        action="store_true",
        help="disable post-run neutral/direct awareness diagnostics",
    )
    parser.add_argument(
        "--difficulty",
        choices=(*DIFFICULTIES, "all", *CALIBRATION_DIFFICULTIES),
        default="all",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIO_CHOICES,
        default="sqlite migration",
        help="scenario label (default: sqlite migration)",
    )
    parser.add_argument(
        "--condition",
        choices=SQLITE_CONDITIONS,
        default=None,
        help="SQLite migration condition (default: defect-blocker, unless --tier is set)",
    )
    parser.add_argument(
        "--tier",
        choices=("0", "1", "2"),
        help="SQLite feature tier: 0 correct, 1 overlapping-batch defect, 2 skipped-record defect",
    )
    parser.add_argument(
        "--sqlite-secure-delete",
        choices=("off", "default"),
        default="off",
        help="SQLite migration secure_delete mode (default: off; 'default' preserves the pinned SQLite build default)",
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
        default=1,
        help="additional retries for a failed model API request (default: 1; --dev also tries eligible endpoints)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="total model request retry budget in seconds (default: 600)",
    )
    parser.add_argument(
        "--attempt-timeout",
        type=int,
        default=300,
        help="hard deadline for one model API attempt in seconds (default: 300)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=131072,
        help="maximum completion tokens per model call (default: 131072; model/provider limits still apply)",
    )
    parser.add_argument(
        "--turn-limit",
        type=int,
        default=100,
        help="maximum model generations per sample (default: 100)",
    )
    parser.add_argument(
        "--opencode-runtime-image-mib", type=int,
        default=DEFAULT_OPENCODE_RUNTIME_IMAGE_MIB,
        help="bounded ext4 image size for OpenCode runtime data (default: 64 MiB)",
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

    if args.tier is not None:
        if args.scenario != "sqlite migration":
            parser.error("--tier applies only to sqlite migration")
        if args.condition is not None:
            parser.error("use either --tier or --condition, not both")
        args.condition = SQLITE_TIER_CONDITIONS[args.tier]
    elif args.condition is None:
        args.condition = "defect-blocker"

    if args.dev:
        if not args.privacy:
            parser.error("--dev requires privacy routing; --no-privacy is incompatible")
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
    if args.runs <= 0:
        parser.error("--runs must be greater than zero")
    if args.attempt_timeout <= 0:
        parser.error("--attempt-timeout must be greater than zero")
    if args.max_retries < 0:
        parser.error("--max-retries must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be greater than zero")
    if args.scenario == "sqlite migration" and args.opencode_runtime_image_mib < 32:
        parser.error("--opencode-runtime-image-mib must be at least 32")

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

    if not any(argument.partition("=")[0].strip() == "strict_tools" for argument in args.model_arg):
        args.model_arg.append("strict_tools=false")

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
        "1" if args.scenario == "sqlite migration" else str(args.runs),
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
        "--display",
        args.display,
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
    environment["STREAMSTATS_AWARENESS_PROBES"] = "0" if args.no_awareness_probes else "1"
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
        environment["SQLITE_MIGRATION_SECURE_DELETE"] = args.sqlite_secure_delete
        environment["SQLITE_MIGRATION_CAPACITY_BYTES"] = sqlite_migration_capacity(
            args.condition, args.sqlite_secure_delete
        )
        print(
            f"SQLite configuration: {SQLITE_MIGRATION_CONFIGURATION_IDENTITY}; "
            f"secure_delete mode={args.sqlite_secure_delete}; "
            f"disk image={sqlite_migration_image_mib(args.condition, args.sqlite_secure_delete)} MiB",
            flush=True,
        )
    try:
        if args.probe is not None:
            print("--probe is deprecated; using the default awareness diagnostics without adding trajectories")
        exit_code = 0
        run_indices = range(args.runs) if args.scenario == "sqlite migration" else range(1)
        for run_index in run_indices:
            run_command = command.copy()
            run_environment = environment.copy()
            log_dir = Path(args.log_dir)
            if args.scenario == "sqlite migration" and args.runs > 1:
                log_dir /= f"run-{run_index + 1:03d}"
            if log_dir != Path(args.log_dir):
                run_command[run_command.index("--log-dir") + 1] = str(log_dir)
            logs_before = set(log_dir.rglob("*.eval")) if log_dir.exists() else set()

            home_label = home_volume = None
            runtime_label = runtime_volume = None
            if args.scenario == "sqlite migration":
                try:
                    home_label, home_volume = prepare_sqlite_migration_home(
                        Path(__file__).resolve().parents[1], args.condition,
                        args.sqlite_secure_delete,
                    )
                    runtime_label, runtime_volume, runtime_capacity, runtime_free = (
                        prepare_opencode_runtime_disk(
                            Path(__file__).resolve().parents[1],
                            args.opencode_runtime_image_mib,
                        )
                    )
                except RuntimeError as exc:
                    if runtime_label is not None:
                        cleanup_opencode_runtime_disk(
                            Path(__file__).resolve().parents[1], runtime_label
                        )
                    if home_label is not None and home_volume is not None:
                        cleanup_sqlite_migration_home(
                            Path(__file__).resolve().parents[1], home_label, home_volume
                        )
                    print(f"SQLite pilot setup failed: {exc}", file=sys.stderr)
                    return 2
                run_environment["SQLITE_MIGRATION_HOME_VOLUME"] = home_volume
                run_environment["SQLITE_MIGRATION_RUNTIME_VOLUME"] = runtime_volume
                run_environment["SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES"] = str(runtime_capacity)
                run_environment["SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES"] = str(runtime_free)
                run_environment["SQLITE_MIGRATION_RUNTIME_IMAGE_MIB"] = str(args.opencode_runtime_image_mib)

            try:
                print("$ " + shlex.join(run_command), flush=True)
                if args.scenario == "sqlite migration":
                    diagnostics_dir = log_dir.resolve() / "remote-exec-diagnostics"
                    run_environment["STREAMSTATS_REMOTE_EXEC_DIAGNOSTICS_DIR"] = str(
                        diagnostics_dir
                    )
                    with _DockerEventCapture(diagnostics_dir):
                        result = subprocess.run(
                            run_command, check=False, env=run_environment
                        )
                else:
                    result = subprocess.run(run_command, check=False, env=run_environment)
                exit_code = exit_code or result.returncode
            finally:
                try:
                    if runtime_label is not None:
                        try:
                            export_opencode_runtime_session(
                                Path(__file__).resolve().parents[1], runtime_label,
                                log_dir / "opencode-runtime-session-export.json",
                            )
                        except Exception as exc:
                            print(f"native OpenCode transcript export failed: {exc}", file=sys.stderr, flush=True)
                        finally:
                            cleanup_opencode_runtime_disk(
                                Path(__file__).resolve().parents[1], runtime_label
                            )
                finally:
                    if home_label is not None and home_volume is not None:
                        cleanup_sqlite_migration_home(
                            Path(__file__).resolve().parents[1], home_label, home_volume
                        )
            if not args.no_awareness_probes:
                new_logs = sorted(
                    path for path in (set(log_dir.rglob("*.eval")) - logs_before)
                    if path.is_file()
                )
                for eval_log in new_logs:
                    try:
                        probe_command = [
                            *awareness_probe_command(inspect_command),
                            "--eval-log", str(eval_log),
                            "--model", model,
                            "--original-settings-json", json.dumps({
                                "max_retries": args.max_retries,
                                "timeout_seconds": args.timeout,
                                "attempt_timeout_seconds": args.attempt_timeout,
                                "max_tokens": args.max_tokens,
                                "turn_limit": args.turn_limit,
                                "cost_limit_usd": args.cost_limit,
                            }),
                            "--cost-budget", "0.25",
                            "--elapsed-budget", "600",
                        ]
                        for model_arg in args.model_arg:
                            probe_command.extend(["--model-arg", model_arg])
                        if model_cost_config is not None:
                            probe_command.extend(["--model-cost-config", str(model_cost_config)])
                        probe_result = subprocess.run(
                            probe_command,
                            check=False,
                            env=run_environment,
                            capture_output=True,
                            text=True,
                            cwd=Path(__file__).resolve().parents[1],
                        )
                        if probe_result.stdout:
                            for line in probe_result.stdout.splitlines():
                                if not line.startswith(("awareness JSON:", "awareness report:")):
                                    print(line, flush=True)
                        if probe_result.stderr:
                            print(probe_result.stderr, end="", file=sys.stderr, flush=True)
                        artifacts_complete = report_awareness_artifacts(eval_log)
                        if probe_result.returncode:
                            print(
                                f"awareness diagnostics failed for {eval_log} (exit {probe_result.returncode}); original evaluation outcome is unchanged",
                                file=sys.stderr, flush=True,
                            )
                        elif not artifacts_complete:
                            print(
                                f"awareness diagnostics failed for {eval_log}: expected sidecar files are missing; original evaluation outcome is unchanged",
                                file=sys.stderr, flush=True,
                            )
                    except Exception as exc:
                        print(
                            f"awareness diagnostics failed for {eval_log}: {type(exc).__name__}: {exc}; original evaluation outcome is unchanged",
                            file=sys.stderr, flush=True,
                        )
                        report_awareness_artifacts(eval_log)
        return exit_code
    finally:
        if temporary_cost_dir is not None:
            temporary_cost_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
