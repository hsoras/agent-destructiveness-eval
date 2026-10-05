"""Pinned OpenCode runtime settings shared by tasks, pilots, and preflight.

The benchmark deliberately keeps the OpenCode binary independent from the
model provider. ``inspect_swe.opencode`` owns the model bridge; OpenCode only
needs a provider/model identifier so its request format is selected.
"""

from __future__ import annotations

import os


INSPECT_AI_VERSION = "0.3.263"
INSPECT_SWE_VERSION = "0.2.70"
# inspect_swe 0.2.70 uses the v1 OpenCode npm package. Pinning this exact
# release makes the sandbox setup reproducible and avoids a latest-version
# lookup during an eval.
OPENCODE_VERSION = "1.14.41"
DEFAULT_OPENCODE_MODEL = "openrouter/z-ai/glm-5.3-flash"

# These variables are intentionally scoped to OpenCode's own runtime. The
# adapter adds its bridge URL, API keys, PATH, HOME, and OPENCODE_CONFIG after
# merging this mapping. Values are strings because they are sent to a remote
# sandbox process.
OPENCODE_RUNTIME_ENV = {
    "OPENCODE_AUTO_SHARE": "0",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_AUTOCOMPACT": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_TERMINAL_TITLE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_PURE": "1",
    # OpenCode validates provider credentials before sending the request. The
    # Inspect bridge terminates at localhost and does not use this value, but
    # the provider still requires the variable to be present.
    "OPENROUTER_API_KEY": "sk-none",
    # Keep OpenCode's writable state on its separately bounded disk so logs or
    # cache writes remain available when a migration fills /home/dev.
    "XDG_CACHE_HOME": "/home/dev/.local/share/opencode/cache",
    "XDG_DATA_HOME": "/home/dev/.local/share",
    "XDG_STATE_HOME": "/home/dev/.local/share/opencode/state",
    "TMPDIR": "/home/dev/.local/share/opencode/tmp",
}


def configured_opencode_model(value: str | None = None) -> str:
    """Resolve and validate the OpenRouter provider/model route."""

    model = (value or os.environ.get("STREAMSTATS_OPENCODE_MODEL") or DEFAULT_OPENCODE_MODEL).strip()
    if not model.startswith("openrouter/") or "/" not in model.removeprefix("openrouter/"):
        raise ValueError(
            "OpenCode model must be an OpenRouter route such as "
            "openrouter/author/model"
        )
    return model


def runtime_metadata() -> dict[str, object]:
    """Return a serializable, audit-friendly copy of the runtime contract."""

    return {
        "backend": "inspect_swe.opencode",
        "inspect_ai_version": INSPECT_AI_VERSION,
        "inspect_swe_version": INSPECT_SWE_VERSION,
        "opencode_version": OPENCODE_VERSION,
        "opencode_runtime_env": dict(OPENCODE_RUNTIME_ENV),
        "native_tools": True,
        "agent_attempts": 1,
        "native_record_capture": (
            "inspect_swe_debug_trace plus scorer-time OpenCode SQLite export "
            "before sandbox teardown"
        ),
        "compaction": "disabled via OPENCODE_DISABLE_AUTOCOMPACT=1",
    }
