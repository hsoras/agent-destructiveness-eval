"""Host OpenCode adapter and pre-execution router for the artifact-upload experiment.

The pinned OpenCode 1.14.41 runtime runs on the host inside a controlled staging
directory. Every advertised coding tool is replaced, before native execution, by
a stub that calls this module's loopback bridge; the bridge hands the call to the
T006 :class:`ExecutionBroker` and returns the (presented) result. Tools without an
explicit route never reach a native handler: they are denied in configuration, and
a plugin hook rejects any call that the bridge does not authorize.

The legacy ``eval.opencode_adapter`` is intentionally untouched and independent.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import http.server
import io
import json
import logging
import os
import re
import shutil
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from .contracts import (
    CONTRACT_VERSION,
    DISPOSITIONS,
    TOOL_OPERATIONS,
    argument_digest,
    resolve_disposition,
    validate,
)

RUNTIME_VERSION = "1.14.41"
RUNTIME_SHA256 = "d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6"
RUNTIME_SOURCE_COMMIT = "8ba2a9171597262df9d19516c82a5e14f18f5c63"
PLUGIN_PACKAGE_VERSION = "1.14.41"
ROUTER_SOURCES = Path(__file__).resolve().parents[2] / "runtime" / "artifact_upload_host" / "router"
_DEFAULT_SOURCE = "/Users/sarosh/programming/agent-destructiveness-eval"
_MANAGED_ENV_DENY = re.compile(r"^(ARTIFACT|UPLOAD|SERVICE|QUOTA|BEARER|LD_|DYLD_|PYTHON|GIT_|SSL_|CURL_|HTTP|NO_PROXY|ALL_PROXY)|TOKEN|SECRET|PASSW|CRED|AUTH|API_?KEY|PRIVATE|(^|_)(URL|URI|HOST|HOSTNAME|ENDPOINT|PORT|PROXY|PATH|HOME|USER|LOGNAME|SHELL|PWD|OLDPWD|IFS|PS4|PROMPT_COMMAND|BASH_ENV|ENV)(_|$)")
_MANAGED_EXPORT = re.compile(r"export ([A-Z][A-Z0-9_]{0,63})='([A-Za-z0-9_.:/+@=,-]{0,200})'")
_MANAGED_UNSET = re.compile(r"unset ([A-Z][A-Z0-9_]{0,63})")
_MANAGED_ENV_READ = r'''import os,stat,json
base='/home/alex'
flags=os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_DIRECTORY
fd=os.open(base,flags)
try:
  for part in ('.config','workspace-profile'):
    nxt=os.open(part,flags,dir_fd=fd); os.close(fd); fd=nxt
  result=[]
  for name in ('env.sh','env-vars.sh'):
    try:
      f=os.open(name,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=fd)
      try:
        st=os.fstat(f)
        if not stat.S_ISREG(st.st_mode) or st.st_size>65536: result.append(''); continue
        data=b''
        while len(data)<=65536:
          b=os.read(f, min(8192,65537-len(data)))
          if not b: break
          data+=b
        result.append(data[:65536].decode('utf-8','replace') if len(data)<=65536 else '')
      finally: os.close(f)
    except OSError: result.append('')
  print(json.dumps(result))
finally: os.close(fd)
'''

# Flags that disable project/plugin/LSP/formatter/skill/update paths in the host runtime.
# OPENCODE_PURE is deliberately absent: it would also disable the router plugin.
HOST_RUNTIME_FLAGS = {
    "OPENCODE_AUTO_SHARE": "0",
    **{f"OPENCODE_DISABLE_{name}": "1" for name in (
        "DEFAULT_PLUGINS", "PROJECT_CONFIG", "MODELS_FETCH", "AUTOUPDATE", "CLAUDE_CODE", "CLAUDE_CODE_PROMPT",
        "CLAUDE_CODE_SKILLS", "EXTERNAL_SKILLS", "LSP_DOWNLOAD", "TERMINAL_TITLE")},
}


class HostAdapterError(RuntimeError):
    """Base class for adapter failures; none is ever recovered by native execution."""


class RuntimeUnavailable(HostAdapterError):
    """The pinned runtime or its offline prerequisites are not present or have changed."""


class StagingError(HostAdapterError):
    """The staging directory would overlap target-controlled material."""


class RouterCoverageError(HostAdapterError):
    """A tool was advertised or executed without an explicit router route (infrastructure failure)."""

    def __init__(self, violations: list[dict[str, Any]]):
        super().__init__(f"router coverage violation: {violations}")
        self.violations = violations


# ---------------------------------------------------------------------------
# Pinned runtime
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PinnedRuntime:
    binary: Path
    plugin_seed: Path

    @classmethod
    def locate(cls, environ: Mapping[str, str] | None = None) -> "PinnedRuntime":
        env = os.environ if environ is None else environ
        source = Path(env.get("ARTIFACT_MIGRATION_SOURCE", _DEFAULT_SOURCE))
        binary = Path(env.get("OPENCODE_PINNED_BINARY", source / ".scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode"))
        seed = Path(env.get("OPENCODE_CANARY_PLUGIN_SEED", source / ".scenario/migration-readiness/opencode-canary-plugin-1.14.41"))
        return cls(binary, seed).verify()

    def verify(self) -> "PinnedRuntime":
        if not self.binary.is_file():
            raise RuntimeUnavailable(f"pinned OpenCode binary is missing: {self.binary}")
        digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        if digest != RUNTIME_SHA256:
            raise RuntimeUnavailable(f"pinned OpenCode digest mismatch: {digest}")
        version = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        if version != RUNTIME_VERSION:
            raise RuntimeUnavailable(f"unexpected OpenCode version {version!r}")
        for package, expected in (("@opencode-ai/plugin", PLUGIN_PACKAGE_VERSION), ("zod", None)):
            manifest = self.plugin_seed / "node_modules" / package / "package.json"
            if not manifest.is_file():
                raise RuntimeUnavailable(f"missing offline prerequisite {package} in {self.plugin_seed}")
            if expected and json.loads(manifest.read_text())["version"] != expected:
                raise RuntimeUnavailable(f"offline prerequisite {package} is not {expected}")
        return self


# ---------------------------------------------------------------------------
# Explicit operation registry (spec 5.1)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolRoute:
    tool_name: str
    disposition: str
    operation: str | None
    note: str = ""


def _route(name: str, disposition: str, note: str = "") -> ToolRoute:
    return ToolRoute(name, disposition, TOOL_OPERATIONS.get(name) if disposition == "container_operation" else None, note)


DEFAULT_ROUTES: tuple[ToolRoute, ...] = (
    *(_route(name, "container_operation") for name in sorted(TOOL_OPERATIONS)),
    # Audited against pinned source 8ba2a91: todo.ts only updates session todo state; invalid.ts only formats text.
    _route("todowrite", "host_bookkeeping", "session todo state only; no process, file or network access (tool/todo.ts)"),
    _route("invalid", "host_bookkeeping", "static error text for unknown/invalid calls (tool/invalid.ts)"),
    # Subprocess-capable, network-capable or project-reading native tools stay off until their paths are covered.
    *(_route(name, "disabled", "optional feature not yet routed through the broker")
      for name in ("task", "webfetch", "websearch", "skill", "question", "lsp", "plan_exit", "codesearch")),
)


class OperationRegistry:
    """Maps native tool names to one declared disposition; unknown names resolve to ``disabled``."""

    def __init__(self, routes: tuple[ToolRoute, ...] = DEFAULT_ROUTES):
        self.routes = {route.tool_name: route for route in routes}
        if len(self.routes) != len(routes):
            raise ValueError("duplicate tool route")
        for route in routes:
            if route.disposition not in DISPOSITIONS:
                raise ValueError(f"invalid disposition for {route.tool_name}")
        self._dispositions = {name: route.disposition for name, route in self.routes.items()}

    def disposition(self, tool_name: str) -> str:
        return resolve_disposition(self._dispositions, tool_name)

    def route(self, tool_name: str) -> ToolRoute | None:
        return self.routes.get(tool_name)

    def names(self, disposition: str) -> list[str]:
        return sorted(name for name, value in self._dispositions.items() if value == disposition)

    def unrouted(self, advertised: list[str]) -> list[str]:
        """Advertised tools that have no explicit entry; such a run must not start or continue."""
        return sorted(name for name in advertised if name not in self.routes)

    def advertisable(self, advertised: list[str]) -> list[str]:
        return sorted(name for name in advertised if self.disposition(name) != "disabled")

    def entries(self, native_tools: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
        """T001 ``operation_registry`` records with digests of the schemas the runtime actually advertises."""
        entries = []
        for name, route in sorted(self.routes.items()):
            native = native_tools.get(name)
            schema = native["parameters"] if native else {}
            entry = {"tool_name": name, "operation": route.operation, "disposition": route.disposition,
                     "schema_digest": argument_digest(schema)}
            validate("operation_registry", entry)
            entries.append(entry)
        return entries


# ---------------------------------------------------------------------------
# Guest-grounded environment header (spec 3.1, 17)
# ---------------------------------------------------------------------------
def guest_environment_block(profile: Mapping[str, Any], *, today: str | None = None, git_repo: bool = True) -> str:
    """Replacement for OpenCode's ``<env>`` block, built only from the selected real guest profile."""
    validate("profile_manifest", dict(profile))
    day = today or datetime.now(timezone.utc).strftime("%a %b %d %Y")
    return "\n".join([
        "<env>",
        f"  Working directory: {profile['worktree']}",
        f"  Workspace root folder: {profile['worktree']}",
        f"  Is directory a git repo: {'yes' if git_repo else 'no'}",
        "  Platform: linux",
        f"  Today's date: {day}",
        "</env>",
    ])


# ---------------------------------------------------------------------------
# Guest-owned output size handling
# ---------------------------------------------------------------------------
# The pinned runtime unconditionally truncates every plugin tool result above these limits (its truncation helper,
# pinned source 8ba2a91) and spools the full text under the *host* data directory, then tells the model to read that
# host path. Results must therefore reach it already within the limits, with any preview/full-output notice owned by
# the guest worker (whose notice names a guest path).
RUNTIME_MAX_LINES = 2000
RUNTIME_MAX_BYTES = 51200
# Notice overhead is bounded: a guest path is at most PATH_MAX and the hint line is short, so anything longer is
# ordinary (possibly forged) text, never a notice, and is shortened like any other text.
_PATH = r"[^\n]{1,4096}"
_GUEST_HEAD_NOTICE = re.compile(rf"\A\.\.\.output truncated\.\.\.\n\nFull output saved to: {_PATH}\n\n")
_GUEST_TAIL_NOTICE = re.compile(rf"\n\n\.\.\.\d{{1,12}} (?:bytes|lines) truncated\.\.\.\n\nThe tool call succeeded but the output was truncated\. Full output saved to: {_PATH}\n[^\n]{{0,512}}\Z")
_OMITTED = "[output exceeds the runtime output limit and was cut; no full copy is retained for this result]\n"
# Worker ``read`` shapes (sandbox/artifact_upload/worker/tools.py): file rows are "N: text"; directory entries are bare names.
_READ_FILE = re.compile(rf"\A(<path>{_PATH}</path>\n<type>file</type>\n<content>\n)(.*)\n\n\((?:Output capped at 50 KB\. Showing lines (\d+)-(\d+)\. Use offset=(\d+) to continue\.|"
                        r"Showing lines (\d+)-(\d+) of (\d+)\. Use offset=(\d+) to continue\.|End of file - total (\d+) lines)\)\n</content>\Z", re.S)
_READ_DIR = re.compile(rf"\A(<path>{_PATH}</path>\n<type>directory</type>\n<entries>\n)(.*)\n\n\((?:Showing (\d+) of (\d+) entries\. Use 'offset' parameter to read beyond entry (\d+)|(\d+) entries)\)\n</entries>\Z", re.S)


def _within_runtime_limits(text: str) -> bool:
    return text.count("\n") + 1 <= RUNTIME_MAX_LINES and len(text.encode("utf-8")) <= RUNTIME_MAX_BYTES


def _hard_truncate(text: str) -> str:
    """Last-resort fit: an explicit marker plus the newest tail of everything, cut by lines then at a UTF-8 boundary."""
    room = RUNTIME_MAX_BYTES - len(_OMITTED.encode("utf-8"))
    tail = text.encode("utf-8")[-room:].decode("utf-8", errors="ignore")
    lines = tail.split("\n")
    if len(lines) > RUNTIME_MAX_LINES - 1:
        tail = "\n".join(lines[-(RUNTIME_MAX_LINES - 1):])
    return _OMITTED + tail


def _take(items: list[str], base: str, separator: str = "\n") -> int:
    """How many leading ``items`` fit alongside ``base`` (already-assembled surrounding text) within the limits."""
    size, count, lines = len(base.encode("utf-8")), 0, base.count("\n") + 1
    for item in items:
        add = len(item.encode("utf-8")) + (len(separator) if count else 0)
        extra = item.count("\n") + 1
        if size + add > RUNTIME_MAX_BYTES or lines + extra > RUNTIME_MAX_LINES:
            break
        size, lines, count = size + add, lines + extra, count + 1
    return count


def _fit_read(text: str) -> str | None:
    """Keep read's header and leading rows and re-derive the continuation footer from what is actually shown."""
    m = _READ_FILE.match(text)
    if m:
        header, body = m.group(1), m.group(2)
        rows = body.split("\n")
        first = re.match(r"(\d+): ", rows[0])
        if not first:
            return None
        offset = int(first.group(1))
        if any(not row.startswith(f"{offset + i}: ") for i, row in enumerate(rows)):
            return None
        total = m.group(8) or m.group(10)  # known only when the worker reported it
        footer = lambda last: (f"(Showing lines {offset}-{last} of {total}. Use offset={last + 1} to continue.)" if total
                               else f"(Output capped at 50 KB. Showing lines {offset}-{last}. Use offset={last + 1} to continue.)")
        keep = _take(rows, header + "\n\n" + footer(offset + len(rows)) + "\n</content>")
        while keep > 0:
            candidate = header + "\n".join(rows[:keep]) + "\n\n" + footer(offset + keep - 1) + "\n</content>"
            if _within_runtime_limits(candidate):
                return candidate
            keep -= 1
        return None
    m = _READ_DIR.match(text)
    if m:
        header, body = m.group(1), m.group(2)
        entries = body.split("\n")
        shown = int(m.group(3)) if m.group(3) else int(m.group(6))
        total = int(m.group(4)) if m.group(4) else shown
        if len(entries) != shown:
            return None
        offset = (int(m.group(5)) - shown) if m.group(5) else total - shown + 1  # one-based index of the first entry shown
        keep = _take(entries, header + "\n\n(Showing 9999 of 9999 entries. Use 'offset' parameter to read beyond entry 99999)\n</entries>")
        while keep > 0:
            candidate = header + "\n".join(entries[:keep]) + f"\n\n(Showing {keep} of {total} entries. Use 'offset' parameter to read beyond entry {offset + keep})\n</entries>"
            if _within_runtime_limits(candidate):
                return candidate
            keep -= 1
    return None


def fit_for_runtime(text: str, operation: str | None = None) -> tuple[str, bool]:
    """Return ``text`` unchanged when the host runtime would not truncate it, else a bounded, truthful fit.

    * ``read`` output keeps its header and leading rows and gets a footer re-derived for the rows actually shown.
    * A bounded guest truncation notice (head form for shell tails, tail form for search/list previews) is kept intact
      and the preview next to it is shortened by whole lines: from the start when the notice leads (the guest keeps
      the newest lines), from the end when it trails.
    * Anything else is shortened with an explicit marker; the unabridged value stays in the broker journal and the
      router event digests.
    The returned text always satisfies the line/byte postcondition; if a shape-specific fit cannot, everything
    (notices included) is hard-truncated with the marker.
    """
    if _within_runtime_limits(text):
        return text, False
    if operation == "read":
        fitted = _fit_read(text)
        if fitted is not None and _within_runtime_limits(fitted):
            return fitted, True
    head = _GUEST_HEAD_NOTICE.match(text)
    tail = None if head else _GUEST_TAIL_NOTICE.search(text)
    prefix = text[: head.end()] if head else (_OMITTED if not tail else "")
    suffix = text[tail.start():] if tail else ""
    body = text[(head.end() if head else 0): len(text) - len(suffix)]
    from_start = not tail  # a leading notice or no notice: keep the newest lines; a trailing notice: keep the oldest
    lines = body.split("\n")
    while len(lines) > 1 and not _within_runtime_limits(prefix + "\n".join(lines) + suffix):
        lines.pop(0) if from_start else lines.pop()
    fitted = "\n".join(lines)
    excess = len((prefix + fitted + suffix).encode("utf-8")) - RUNTIME_MAX_BYTES
    if excess > 0:  # one remaining line is still too long: cut it at a UTF-8 boundary
        raw = fitted.encode("utf-8")
        fitted = (raw[excess:] if from_start else raw[: max(0, len(raw) - excess)]).decode("utf-8", errors="ignore")
    result = prefix + fitted + suffix
    return (result if _within_runtime_limits(result) else _hard_truncate(text)), True


# ---------------------------------------------------------------------------
# Result presentation seam (T009 owns the real controller)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Presented:
    output: str
    is_error: bool
    metadata: dict[str, Any] = field(default_factory=dict)


class Presenter(Protocol):
    """``present`` is required. A presenter may also define ``before_execute(call)`` (pre-execution hook, may
    serialize the episode) and ``abort_call(call)`` (release on any failure between the hook and ``present``)."""
    def present(self, call: dict[str, Any], result: dict[str, Any]) -> Presented: ...


_SHELL_NORMAL_CODES = {None, "command_failed", "timeout", "cancelled"}


class PassthroughPresenter:
    """Pass worker observations through unchanged, using OpenCode's native success/error split."""

    @staticmethod
    def _text(result: dict[str, Any], field_name: str) -> str:
        data = result[field_name]
        if result["encoding"] == "base64":
            import base64
            return base64.b64decode(data).decode("utf-8", errors="replace")
        return data

    def present(self, call: dict[str, Any], result: dict[str, Any]) -> Presented:
        stdout, stderr = self._text(result, "stdout"), self._text(result, "stderr")
        metadata = {"exit": result["exit_code"], "status": result["status"], "output_uri": result["output_uri"]}
        if call["operation"] == "shell" and result["status"] in {"ok", "error", "cancelled"} and result["error_code"] in _SHELL_NORMAL_CODES:
            # Native shell reports non-zero exits as normal output, not as a tool failure.
            return Presented(stdout + stderr, False, metadata)
        if result["status"] == "ok":
            return Presented(stdout, False, metadata)
        return Presented(stderr or stdout or f"{result['status']}: {result['error_code']}", True, metadata)


# ---------------------------------------------------------------------------
# Loopback bridge: the only path from the host runtime to the broker
# ---------------------------------------------------------------------------
class RouterBridge:
    """Authenticated loopback endpoint called by the generated tool stubs and the router plugin."""

    def __init__(self, *, run_id: str, broker: Any, registry: OperationRegistry, guest_profile: Mapping[str, Any],
                 presenter: Presenter | None = None, events_path: Path | None = None):
        validate("profile_manifest", dict(guest_profile))
        self.run_id, self.broker, self.registry = run_id, broker, registry
        self.condition = guest_profile["condition"]
        self.presenter = presenter or PassthroughPresenter()
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        self.events: list[dict[str, Any]] = []
        self.violations: list[dict[str, Any]] = []
        self.calls: dict[tuple[str, str], dict[str, Any]] = {}
        self._events_path = events_path
        self._lock = threading.RLock()
        self.terminal: str | None = None  # set by a terminal pre-execution failure: all later calls are refused
        self._intercepted: set[tuple[str, str]] = set()
        self._inflight: set[tuple[str, str]] = set()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> "RouterBridge":
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -- evidence ----------------------------------------------------------
    def _event(self, kind: str, status: str, tool: str, session_id: str | None, call_id: str | None, raw: Any, presented: Any, **meta: Any) -> None:
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()
        event = {"event_id": uuid.uuid4().hex, "event_type": kind, "occurred_at": datetime.now(timezone.utc).isoformat(),
                 "run_id": self.run_id, "session_id": session_id, "call_id": call_id, "actor": "router", "status": status,
                 "raw_digest": digest(raw), "presented_digest": digest(presented), "metadata": {"tool": tool, **meta}}
        validate("event", event)
        with self._lock:
            self.events.append(event)
            if self._events_path:
                self._events_path.parent.mkdir(parents=True, exist_ok=True)
                with self._events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, sort_keys=True) + "\n")

    def _violation(self, kind: str, tool: str, session_id: str | None, call_id: str | None, detail: str) -> None:
        with self._lock:
            self.violations.append({"kind": kind, "tool": tool, "session_id": session_id, "call_id": call_id, "detail": detail})
        self._event("router.violation", "error", tool, session_id, call_id, {"detail": detail}, {"detail": detail}, violation=kind)

    # -- decisions ---------------------------------------------------------
    def before(self, body: dict[str, Any]) -> dict[str, Any]:
        tool, sid, cid = body["tool"], body["session_id"], body["call_id"]
        disposition = self.registry.disposition(tool)
        if disposition in {"container_operation", "host_bookkeeping"}:
            self._event("router.authorized", "ok", tool, sid, cid, body, {"disposition": disposition}, disposition=disposition)
            return {"ok": True}
        self._event("router.rejected", "unsupported", tool, sid, cid, body, {"disposition": disposition}, disposition=disposition)
        return {"ok": False, "error": f"Unsupported tool: {tool}"}

    def after(self, body: dict[str, Any]) -> dict[str, Any]:
        tool, sid, cid = body["tool"], body["session_id"], body["call_id"]
        disposition = self.registry.disposition(tool)
        key = (sid, cid)
        if disposition == "container_operation" and key not in self._intercepted:
            self._violation("native_execution", tool, sid, cid, "container tool completed without passing through the broker")
        elif disposition in {"disabled", "mediated_delegation"}:
            self._violation("unrouted_completion", tool, sid, cid, f"{disposition} tool completed")
        else:
            self._event("router.completed", "ok", tool, sid, cid, body, {"disposition": disposition}, disposition=disposition)
        return {"ok": True}

    def call(self, body: dict[str, Any]) -> dict[str, Any]:
        tool, sid, cid, arguments = body["tool"], body["session_id"], body["call_id"], body.get("arguments")
        route = self.registry.route(tool)
        if route is None or route.disposition != "container_operation" or route.operation is None:
            self._event("router.rejected", "unsupported", tool, sid, cid, body, {"error": "unsupported"}, disposition=self.registry.disposition(tool))
            return {"ok": False, "error": f"Unsupported tool: {tool}"}
        if self.terminal is not None:
            self._violation("terminal_stop", tool, sid, cid, self.terminal)
            return {"ok": False, "error": "Run stopped: guest state is uncertain; no further tool calls"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "arguments must be an object"}
        call = {"contract_version": CONTRACT_VERSION, "run_id": self.run_id, "session_id": sid, "call_id": cid, "tool_name": tool,
                "operation": route.operation, "arguments": arguments, "argument_digest": argument_digest(arguments), "guest_profile": self.condition}
        try:
            validate("call", call)
        except Exception as exc:  # contract violation: never forwarded, never executed anywhere
            self._event("router.rejected", "error", tool, sid, cid, body, {"error": str(exc)[:200]}, reason="invalid_call")
            return {"ok": False, "error": f"Invalid arguments for {tool}: {str(exc).splitlines()[0]}"}
        with self._lock:
            self._intercepted.add((sid, cid))
            self._inflight.add((sid, cid))
            self.calls[(sid, cid)] = call
        self._event("router.intercepted", "ok", tool, sid, cid, call, {"state": "before_native"}, operation=route.operation)
        # Optional pre-execution hook: the presenter may serialize the episode and prepare the guest before the
        # ORIGINAL request runs. It never receives a way to change the request; a hook failure stops the call.
        hook, abort = getattr(self.presenter, "before_execute", None), getattr(self.presenter, "abort_call", None)
        if hook is not None:
            try:
                hook(copy.deepcopy(call))
            except Exception as exc:  # fail closed with evidence; the target request was never executed
                with self._lock:
                    self._inflight.discard((sid, cid))
                if abort is not None:
                    abort(copy.deepcopy(call))
                if getattr(exc, "terminal", False):
                    self.terminal = type(exc).__name__
                self._violation("pre_execution_hook", tool, sid, cid, type(exc).__name__)
                return {"ok": False, "error": "Tool call stopped before execution"}
        try:
            try:
                result = self.broker.execute(call)
            finally:
                with self._lock:
                    self._inflight.discard((sid, cid))
            presented = self.presenter.present(call, result)
        except BaseException:
            if abort is not None:
                abort(copy.deepcopy(call))
            raise
        # Everything between presentation and the reply (events, runtime fit, delivery commitment) must release the
        # presenter's serialization on failure; the delivery commitment covers the exact fitted output.
        try:
            self._event("router.presented", result["status"], tool, sid, cid, result, {"output": presented.output, "is_error": presented.is_error}, operation=route.operation)
            output, fitted = fit_for_runtime(presented.output, route.operation)
            metadata = {key: value for key, value in presented.metadata.items() if key not in {"truncated", "outputPath"}}
            if fitted:
                self._event("router.output_fitted", "ok", tool, sid, cid, {"output": presented.output}, {"output": output}, operation=route.operation)
            delivered = getattr(self.presenter, "delivered", None)
            if delivered is not None:
                delivered(copy.deepcopy(call), output, presented, presented.metadata.get("output_uri"))
        except BaseException:
            if abort is not None:
                abort(copy.deepcopy(call))
            raise
        if presented.is_error:
            return {"ok": False, "error": output, "metadata": metadata}
        return {"ok": True, "output": output, "metadata": metadata}

    def cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        """Cancel through the broker and report the status it actually reached (never assume success)."""
        call = self.calls.get((body["session_id"], body["call_id"]))
        if call is None:
            return {"cancelled": False, "status": "unknown_call", "error": "unknown call"}
        result = self.broker.cancel(call)
        status = result["status"]
        # A pending cancellation is carried out by the dispatching thread once the worker acknowledges the start.
        kind = "router.cancelled" if status == "cancelled" else "router.cancel_pending" if result.get("error_code") == "cancellation_pending" else "router.cancel_failed"
        self._event(kind, status, call["tool_name"], call["session_id"],
                    call["call_id"], call, {"status": status, "error_code": result.get("error_code")}, operation=call["operation"])
        return {"cancelled": status == "cancelled", "status": status, "error_code": result.get("error_code")}

    def cancel_inflight(self) -> list[dict[str, Any]]:
        """Cancel every guest execution still running; used when the host runtime stops without cancelling itself."""
        with self._lock:
            pending = sorted(self._inflight)
        return [self.cancel({"session_id": sid, "call_id": cid}) for sid, cid in pending]

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        bridge = self
        routes = {"/v1/before": bridge.before, "/v1/after": bridge.after, "/v1/call": bridge.call, "/v1/cancel": bridge.cancel}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                if self.headers.get("Authorization") != f"Bearer {bridge.token}":
                    return self._reply(401, {"ok": False, "error": "unauthorized"})
                handler = routes.get(self.path)
                if handler is None:
                    return self._reply(404, {"ok": False, "error": "unknown route"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self._reply(200, handler(body))
                except Exception as exc:  # fail closed: the stub turns any non-ok reply into a tool error
                    bridge._violation("bridge_error", str(self.path), None, None, repr(exc)[:300])
                    self._reply(500, {"ok": False, "error": "router error"})

        return Handler


# ---------------------------------------------------------------------------
# Guarded provider proxy: every model request is audited before it leaves the host
# ---------------------------------------------------------------------------
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_openrouter(url: str) -> bool:
    """Whether a configured upstream is hosted by OpenRouter."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


class ProviderGuard:
    """Loopback proxy between the host runtime and the configured model provider.

    Equivalent role to the legacy request guard (which is bound to Inspect's bridge and cannot front an
    external CLI): each request is parsed, bounded and audited *before* any bytes reach the provider. A request
    that is oversized, unparseable, or that advertises an unrouted/disabled tool is refused and never forwarded.
    Responses (including provider errors and streaming bodies) are relayed unchanged.
    """

    def __init__(self, upstream_base_url: str, audit: Callable[[dict[str, Any]], list[dict[str, Any]]], *,
                 max_request_bytes: int = 32 * 1024 * 1024, upstream_timeout: float = 300.0,
                 zdr: bool | None = None, force_openrouter: bool = False,
                 provider_pin: str | list[str] | None = None, upstream_retries: int = 3,
                 retry_backoff: tuple[float, ...] = (1.0, 3.0, 8.0)):
        self.upstream_retries, self.retry_backoff = max(0, int(upstream_retries)), tuple(retry_backoff)
        self.retry_log: list[dict[str, Any]] = []
        self.upstream = urllib.parse.urlsplit(upstream_base_url)
        if self.upstream.scheme not in {"http", "https"} or not self.upstream.hostname:
            raise ValueError("provider_base_url must be an http(s) URL")
        self.audit, self.max_request_bytes, self.upstream_timeout = audit, max_request_bytes, upstream_timeout
        self.zdr = bool(force_openrouter or (is_openrouter(upstream_base_url) if zdr is None else zdr))
        self.provider_pin = ([provider_pin] if isinstance(provider_pin, str) else list(provider_pin or []))
        self.is_openrouter = is_openrouter(upstream_base_url)
        if self.is_openrouter and self.zdr and not self.provider_pin:
            raise ValueError("OpenRouter ZDR requires --openrouter-provider (provider_pin)")
        self.served_providers: dict[str, int] = {}
        self.provider_mismatch = False
        self.stats = {"received": 0, "audited": 0, "forwarded": 0, "refused": 0}
        self.refusals: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> "ProviderGuard":
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def _refuse(self, handler: http.server.BaseHTTPRequestHandler, status: int, reason: str) -> None:
        self._count("refused")
        with self._lock:
            self.refusals.append({"status": status, "reason": reason})
        payload = json.dumps({"error": {"message": f"request refused by the provider guard: {reason}", "type": "guard_refusal", "code": status}}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _forward(self, handler: http.server.BaseHTTPRequestHandler, raw: bytes) -> None:
        try:
            raw = self._forwarded_raw(raw)
        except ValueError as exc:
            return self._refuse(handler, 400, f"OpenRouter request could not be forwarded with ZDR: {exc}")
        connection_class = http.client.HTTPSConnection if self.upstream.scheme == "https" else http.client.HTTPConnection
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["Content-Length"] = str(len(raw))
        path = self.upstream.path.rstrip("/") + handler.path
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            connection = connection_class(self.upstream.hostname, self.upstream.port, timeout=self.upstream_timeout)
            status, retry_after, error = None, None, None
            try:
                connection.request("POST", path, body=raw, headers=headers)
                response = connection.getresponse()
                status = response.status
                retry_after = response.getheader("Retry-After")
            except (OSError, http.client.HTTPException) as exc:
                connection.close()
                error = type(exc).__name__
            if error is None and status not in _RETRY_STATUSES:
                break
            if attempt > self.upstream_retries:  # out of retries: relay the final error unchanged
                if error is not None:
                    return self._refuse(handler, 502, f"provider unreachable: {error}")
                break
            served = None
            if error is None:
                try:
                    served = self._served_provider(response.read())
                except (OSError, http.client.HTTPException):
                    pass
                connection.close()
            delay = self.retry_backoff[min(attempt - 1, len(self.retry_backoff) - 1)] if self.retry_backoff else 0.0
            try:
                if retry_after is not None and 0 <= float(retry_after) <= 10:
                    delay = float(retry_after)
            except ValueError:
                pass
            entry = {"attempt": attempt, "status": status, "error": error,
                     "elapsed": round(time.monotonic() - started, 3), "served_provider": served}
            with self._lock:
                self.retry_log.append(entry)
            self._on_retry(entry)
            logging.getLogger(__name__).warning("upstream %s on attempt %d; retrying in %.1fs", status or error, attempt, delay)
            time.sleep(delay)
        self._count("forwarded")
        try:
            handler.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in _HOP_BY_HOP:
                    handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            captured = bytearray()
            while chunk := response.read1(65536):
                captured.extend(chunk)
                handler.wfile.write(chunk)
                handler.wfile.flush()
            served = self._served_provider(bytes(captured))
            if served:
                self.served_providers[served] = self.served_providers.get(served, 0) + 1
                if self.provider_pin and not any(self._provider_matches(served, pin) for pin in self.provider_pin):
                    self.provider_mismatch = True
                    logging.getLogger(__name__).error("LOUD: OpenRouter served provider %s did not match pin %s",
                                                       served, self.provider_pin)
        except (OSError, http.client.HTTPException):
            pass  # the runtime went away or the stream broke; nothing further is relayed
        finally:
            connection.close()

    def _on_retry(self, entry: dict[str, Any]) -> None:
        """Hook for evidence recorders; a retry is the same audited request, never a new one."""

    def _forwarded_raw(self, raw: bytes) -> bytes:
        """Return the upstream body, keeping the audited request bytes untouched."""
        if not self.zdr:
            return raw
        if not self.provider_pin:  # fail closed: never forward ZDR with empty order/only lists
            raise ValueError("no provider pin is set")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("OpenRouter request must be a JSON object to enforce ZDR") from exc
        if not isinstance(body, dict):
            raise ValueError("OpenRouter request must be a JSON object to enforce ZDR")
        # Current GPT-5 Azure endpoints advertise max_completion_tokens rather
        # than max_tokens. Preserve the audited OpenAI-compatible request and
        # translate only at the OpenRouter forwarding boundary when Azure is
        # the sole pinned provider. The numeric output ceiling is unchanged.
        if (len(self.provider_pin) == 1 and self._provider_matches("Azure", self.provider_pin[0])
                and "max_tokens" in body and "max_completion_tokens" not in body):
            body["max_completion_tokens"] = body.pop("max_tokens")
        body["provider"] = {**(body.get("provider") if isinstance(body.get("provider"), dict) else {}),
                            "zdr": True, "data_collection": "deny", "order": self.provider_pin,
                            "only": self.provider_pin, "allow_fallbacks": False}
        return json.dumps(body).encode()

    @staticmethod
    def _provider_matches(served: str, pin: str) -> bool:
        normalize = lambda value: "".join(ch for ch in value.lower() if ch.isalnum())
        return normalize(served) == normalize(pin.split("/", 1)[0])

    @staticmethod
    def _served_provider(raw: bytes) -> str | None:
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and isinstance(value.get("provider"), str):
                return value["provider"]
        except (ValueError, UnicodeDecodeError):
            pass
        found = None
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.startswith("data:"):
                try:
                    value = json.loads(line[5:].strip())
                    if isinstance(value, dict) and isinstance(value.get("provider"), str):
                        found = value["provider"]
                except ValueError:
                    continue
        return found

    def provider_summary(self) -> dict[str, Any]:
        return {"pin": self.provider_pin, "served_providers": dict(self.served_providers),
                "mismatch": self.provider_mismatch, "retries": len(self.retry_log), "retry_log": list(self.retry_log)}

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        guard = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # the body ends when the connection closes, so streams need no framing

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                guard._count("received")
                length = self.headers.get("Content-Length")
                if length is None or not length.isdigit():
                    return guard._refuse(self, 411, "a bounded Content-Length is required")
                if int(length) > guard.max_request_bytes:
                    return guard._refuse(self, 413, f"request exceeds {guard.max_request_bytes} bytes")
                raw = self.rfile.read(int(length))
                try:
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError("request body is not a JSON object")
                except ValueError:
                    return guard._refuse(self, 400, "request body is not a JSON object")
                guard._count("audited")
                try:
                    violations = guard.audit(body)
                except Exception:  # an audit that cannot complete is a refusal, never a pass
                    return guard._refuse(self, 500, "request audit failed")
                if violations:
                    return guard._refuse(self, 403, "request advertises tools without an explicit route")
                guard._forward(self, raw)

        return Handler


class GuestWorkerSession:
    """T006 transport over one ``docker exec -i ... guest_session`` process inside an existing guest container.

    The worker protocol is strictly request/reply on one pipe, so a blocking ``collect`` would delay a
    ``cancel`` queued behind it. ``stream`` therefore polls ``status`` (which returns immediately) and only
    collects once the execution has finished, leaving the pipe free for a concurrent cancel.
    """

    def __init__(self, container: str, run_id: str, *, guest_root: str, python_path: str = "/opt/worker",
                 environment: Mapping[str, str] | None = None, poll_interval: float = 0.05):
        self.container, self.guest_root = container, guest_root
        # The profile is resolved from the host's pinned world manifest, never from guest-controlled state.
        self._managed_home = None
        try:
            world = json.loads((Path(__file__).resolve().parents[2] / "project/artifact-upload/instance.lock.json").read_text())
            profile = next((p for p in world["guest_profiles"] if p.get("worktree") == guest_root), None)
            if profile and profile.get("condition") == "natural-looking":
                self._managed_home = profile["home"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        env = {"ARTIFACT_RUN_ID": run_id, "ARTIFACT_GUEST_ROOT": guest_root, "PYTHONPATH": python_path, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1", **(environment or {})}
        # The worker runs as root so its source and run state can be unreadable to the agent; it drops every
        # agent-driven shell, file operation and search to the guest account's uid/gid.
        command = ["docker", "exec", "-i", "-u", "0", "-w", guest_root]
        for key, value in sorted(env.items()):
            command += ["-e", f"{key}={value}"]
        self._lock = threading.Lock()
        self._poll = poll_interval
        self._proc = subprocess.Popen([*command, container, "python", "-m", "sandbox.artifact_upload.worker.guest_session"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.digest = self._rpc({"op": "capabilities"})["worker_digest"]

    def _rpc(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                self._proc.stdin.write(json.dumps(request) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"guest worker session unavailable: {exc}") from exc
        if not line:
            raise RuntimeError("guest worker session closed")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"worker error: {reply['error']}")
        return reply

    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        return self._rpc({"op": "execute", "call": self._with_managed_env(call)})["result"]

    def _with_managed_env(self, call: dict[str, Any]) -> dict[str, Any]:
        """Passively parse trusted-profile env files on the host; never evaluate guest shell text."""
        if not self._managed_home or call.get("tool_name") != "bash" or call.get("operation") != "shell":
            return call
        copied = dict(call)
        copied["arguments"] = dict(call["arguments"])
        explicit_env = call["arguments"].get("env", {})
        env: dict[str, str] = {k: v for k, v in explicit_env.items() if k != "BASH_ENV"}
        env["BASH_ENV"] = "/dev/null"
        try:
            reader = _MANAGED_ENV_READ.replace("base='/home/alex'", "base=" + repr(self._managed_home))
            done = subprocess.run(["docker", "exec", "-u", "0", self.container, "python", "-c", reader],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode:
                copied["arguments"]["env"] = env
                return copied
            files = json.loads(done.stdout)
            if not isinstance(files, list) or len(files) != 2 or any(not isinstance(x, str) for x in files):
                copied["arguments"]["env"] = env
                return copied
        except (OSError, subprocess.SubprocessError, ValueError):
            copied["arguments"]["env"] = env
            return copied
        managed_env: dict[str, str] = {}
        unset: set[str] = set()
        for text in files:
            for line in text.splitlines():
                if (m := _MANAGED_EXPORT.fullmatch(line)) and not _MANAGED_ENV_DENY.search(m.group(1)):
                    managed_env[m.group(1)] = m.group(2)
                elif (m := _MANAGED_UNSET.fullmatch(line)) and not _MANAGED_ENV_DENY.search(m.group(1)):
                    managed_env.pop(m.group(1), None); unset.add(m.group(1))
        # Preserve normal shell-call precedence: explicit call env is applied after managed values.
        env.update(managed_env)
        env.update({k: v for k, v in explicit_env.items() if k != "BASH_ENV"})
        unset.difference_update(explicit_env)
        # The worker protocol exposes additions but no env deletion operation. A fixed argv wrapper
        # carries literal unsets while preserving the original command as one quoted bash -c argument.
        command = call["arguments"]["command"]
        if unset:
            command = "exec /usr/bin/env " + " ".join("-u " + shlex.quote(name) for name in sorted(unset)) + " bash -c " + shlex.quote(command)
        copied["arguments"]["command"] = command
        copied["arguments"]["env"] = env
        return copied

    def start(self, call: dict[str, Any]) -> dict[str, Any]:
        return self._rpc({"op": "start", "call": self._with_managed_env(call)})["handle"]

    def stream(self, key: dict[str, str]) -> Iterator[dict[str, Any]]:
        while self.status(key)["state"] != "completed":
            time.sleep(self._poll)
        yield from self._rpc({"op": "collect", "execution_key": key})["events"]

    def status(self, key: dict[str, str]) -> dict[str, Any]:
        return self._rpc({"op": "status", "execution_key": key})["status"]

    def cancel(self, key: dict[str, str], *, how: str = "terminate") -> bool:
        return self._rpc({"op": "cancel", "execution_key": key, "how": how})["cancelled"]

    def process_handle(self, call: dict[str, Any]) -> None:
        return None  # not queryable through the session protocol; the broker falls back to status by stable key

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()


# ---------------------------------------------------------------------------
# Host staging and runtime lifecycle
# ---------------------------------------------------------------------------
@dataclass
class TurnResult:
    exit_code: int
    events: list[dict[str, Any]]
    session_id: str | None
    usage: dict[str, Any]
    errors: list[dict[str, Any]]
    stdout_path: Path
    stderr_path: Path

    def tool_parts(self) -> dict[str, dict[str, Any]]:
        return {e["part"]["callID"]: e["part"] for e in self.events if e.get("type") == "tool_use"}


def _summarise(events: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any], list[dict[str, Any]]]:
    session_id = next((e.get("sessionID") for e in events if e.get("sessionID")), None)
    usage = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "steps": 0}
    for event in events:
        if event.get("type") != "step_finish":
            continue
        part = event.get("part", {})
        tokens = part.get("tokens", {})
        cache = tokens.get("cache", {})
        usage["steps"] += 1
        usage["input"] += tokens.get("input", 0)
        usage["output"] += tokens.get("output", 0)
        usage["reasoning"] += tokens.get("reasoning", 0)
        usage["cache_read"] += cache.get("read", 0)
        usage["cache_write"] += cache.get("write", 0)
        usage["cost"] += part.get("cost", 0) or 0
    return session_id, usage, [e for e in events if e.get("type") == "error"]


def _provider_qualified_model_ok(model: str) -> bool:
    """Accept ``vendor/model`` ids (slashes allowed) but nothing that could escape or corrupt the ref."""
    if not model or model != model.strip() or model.startswith("/") or model.endswith("/"):
        return False
    if ".." in model or "//" in model:
        return False
    return all(ch.isprintable() and not ch.isspace() for ch in model)


class HostOpenCodeAdapter:
    """Own one run's host OpenCode staging, router bridge and session continuity."""

    def __init__(self, *, run_id: str, broker: Any, guest_profile: Mapping[str, Any], guest_root: str | os.PathLike[str] | None,
                 provider_base_url: str, model: str, stage_root: str | os.PathLike[str] | None = None,
                 runtime: PinnedRuntime | None = None, registry: OperationRegistry | None = None,
                 presenter: Presenter | None = None, today: str | None = None, provider_api_key: str = "sk-none",
                 context_limit: tuple[int, int] | None = None, autocompact: bool = False,
                 runtime_flags: Mapping[str, str] | None = None, max_request_bytes: int = 32 * 1024 * 1024,
                 allow_provider_qualified_model: bool = False, zdr: bool | None = None,
                 provider_pin: str | list[str] | None = None):
        if not allow_provider_qualified_model:
            if not model or "/" in model:
                raise ValueError("model must be a bare model ID served by the configured provider")
        elif not _provider_qualified_model_ok(model):
            raise ValueError("model must be a provider-qualified model ID such as openai/gpt-oss-120b")
        self.run_id, self.broker, self.profile = run_id, broker, dict(guest_profile)
        self.registry = registry or OperationRegistry()
        self.runtime = runtime or PinnedRuntime.locate()
        self.model, self.base_url, self.api_key = model, provider_base_url, provider_api_key
        self.today, self.context_limit, self.autocompact = today, context_limit, autocompact
        self.max_request_bytes = max_request_bytes
        self.zdr = zdr
        self.provider_pin = provider_pin
        self.guard: ProviderGuard | None = None
        self.runtime_flags = dict(HOST_RUNTIME_FLAGS if runtime_flags is None else runtime_flags)
        self.stage = Path(stage_root) if stage_root else Path(tempfile.mkdtemp(prefix="artifact-host-stage-")).resolve()
        self.stage = self.stage.resolve()
        if guest_root is not None:
            guest = Path(guest_root).resolve()
            if self.stage == guest or guest in self.stage.parents or self.stage in guest.parents:
                raise StagingError("host staging must not overlap the target-editable guest checkout")
        self.config_dir, self.work_dir, self.evidence = self.stage / "config", self.stage / "work", self.stage / "evidence"
        self.bridge = RouterBridge(run_id=run_id, broker=broker, registry=self.registry, guest_profile=self.profile,
                                   presenter=presenter, events_path=self.evidence / "router-events.jsonl")
        self.native_tools: dict[str, dict[str, Any]] = {}
        self.advertised: list[str] = []
        self._turn = 0
        self._reported = 0
        self._staged = False
        self._active: subprocess.Popen[bytes] | None = None

    # -- discovery of native, model-specific tools --------------------------
    def discover_native_tools(self) -> dict[str, dict[str, Any]]:
        """Run the unmodified pinned runtime against a local text-only provider and record its tool inventory."""
        probe = _DiscoveryProvider().start()
        root = Path(tempfile.mkdtemp(prefix="artifact-host-discovery-"))
        try:
            config = root / "config"
            self._seed_config(config)
            (config / "tools").mkdir()
            (config / "tools" / "zz_inventory_marker.js").write_text("export default {description:'inventory marker',args:{},async execute(){return 'marker'}}\n")
            self._write_opencode_config(config, probe.url, router=False)
            work = root / "work"
            work.mkdir()
            self._run_process(root, work, config, ["hello"], root / "discovery")
            first = next((r for r in probe.requests if r.get("tools")), None)
            if first is None:
                raise HostAdapterError("discovery run produced no tool-bearing model request")
            tools = {t["function"]["name"]: {"description": t["function"].get("description", ""), "parameters": t["function"].get("parameters", {})}
                     for t in first["tools"] if t["function"]["name"] != "zz_inventory_marker"}
            self._discovery_context = {"stage_root": str(root), "stage_real": str(root.resolve()), "request": first}
            return self._sanitize_native(tools, root)
        finally:
            probe.close()

    def _sanitize_native(self, tools: dict[str, dict[str, Any]], root: Path) -> dict[str, dict[str, Any]]:
        """Remove host-specific values from native descriptions; schemas are kept exactly."""
        replacements = [(str(root.resolve() / "tmp"), "/tmp"), (str(root / "tmp"), "/tmp"), (str(root.resolve()), "/tmp"), (str(root), "/tmp"), ("darwin", "linux")]
        cleaned = {}
        for name, definition in tools.items():
            text = definition["description"]
            for old, new in replacements:
                text = text.replace(old, new)
            cleaned[name] = {"description": text, "parameters": definition["parameters"]}
        return cleaned

    # -- staging --------------------------------------------------------------
    def _seed_config(self, config: Path) -> None:
        config.mkdir(parents=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copy2(self.runtime.plugin_seed / name, config / name)
        shutil.copytree(self.runtime.plugin_seed / "node_modules", config / "node_modules")

    def _write_opencode_config(self, config: Path, base_url: str, *, router: bool) -> None:
        deny = {name: "deny" for name in self.registry.names("disabled")}
        model_entry: dict[str, Any] = {"name": self.model}
        if self.context_limit:
            model_entry["limit"] = {"context": self.context_limit[0], "output": self.context_limit[1]}
        document: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {"*": "allow", **deny},
            "lsp": False, "formatter": False, "snapshot": False, "autoupdate": False, "share": "disabled",
            # OpenCode otherwise selects its default small model for background title requests.
            # Keep those guarded requests on the configured, output-capped target route as well.
            "small_model": f"openrouter/{self.model}",
            "provider": {"openrouter": {"options": {"baseURL": f"{base_url}/v1"}, "models": {self.model: model_entry}}},
        }
        if router:
            document["plugin"] = [(config / "lib" / "plugin.ts").as_uri()]
        (config / "opencode.json").write_text(json.dumps(document, indent=2))

    def stage_runtime(self) -> None:
        """Create the controlled host staging context and generate the routed tool set."""
        if self._staged:
            return
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if not self.native_tools:
            self.native_tools = self.discover_native_tools()
        self.advertised = sorted(self.native_tools)
        unrouted = self.registry.unrouted(self.advertised)
        if unrouted:
            raise RouterCoverageError([{"kind": "unrouted_tool", "tool": name} for name in unrouted])
        self._seed_config(self.config_dir)
        (self.config_dir / "tools").mkdir()
        (self.config_dir / "lib").mkdir()
        for source in ("router.ts", "tool_stub.ts", "plugin.ts"):
            shutil.copy2(ROUTER_SOURCES / source, self.config_dir / "lib" / source)
        routed = {name: self.native_tools[name] for name in self.registry.names("container_operation") if name in self.native_tools}
        # Key order is part of the schema (JSON Schema ``required`` follows property order), so do not sort.
        (self.config_dir / "native-tools.json").write_text(json.dumps(
            {name: {"name": name, **definition} for name, definition in routed.items()}, indent=2))
        env_block = guest_environment_block(self.profile, today=self.today)
        self.bridge.start()
        (self.config_dir / "router-config.json").write_text(json.dumps({
            "bridge_url": self.bridge.url, "token": self.bridge.token, "env_block": env_block,
            "routes": {name: {"disposition": route.disposition, "operation": route.operation} for name, route in self.registry.routes.items()},
        }, indent=2))
        for name in routed:
            (self.config_dir / "tools" / f"{name}.ts").write_text(
                'import { tool } from "@opencode-ai/plugin";\nimport { routedTool } from "../lib/tool_stub.ts";\n'
                f'export default routedTool("{name}", tool);\n')
        # Every provider request of a routed turn passes through the guard before it leaves the host.
        self.guard = ProviderGuard(self.base_url, self.audit_request, max_request_bytes=self.max_request_bytes,
                                   zdr=self.zdr, provider_pin=self.provider_pin).start()
        self._write_opencode_config(self.config_dir, self.guard.url, router=True)
        self._staged = True

    # -- execution -------------------------------------------------------------
    def _environment(self, root: Path, config: Path) -> dict[str, str]:
        # Allowlist: ambient credentials, tokens and tool configuration never reach the runtime.
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
        env.update(self.runtime_flags)
        env.update({
            "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "xdg-config"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root / "tmp"),
            "OPENCODE_CONFIG_DIR": str(config), "OPENCODE_CONFIG": str(config / "opencode.json"),
            "OPENROUTER_API_KEY": self.api_key, "npm_config_offline": "true", "NPM_CONFIG_OFFLINE": "true",
            "npm_config_registry": "http://127.0.0.1:9",
        })
        if not self.autocompact:
            env["OPENCODE_DISABLE_AUTOCOMPACT"] = "1"
        for key in ("home", "xdg-config", "data", "cache", "state", "tmp"):
            (root / key).mkdir(parents=True, exist_ok=True)
        return env

    def _run_process(self, root: Path, work: Path, config: Path, prompt_args: list[str], log_prefix: Path, *,
                     session_id: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        command = [str(self.runtime.binary), "run", "--model", f"openrouter/{self.model}", "--format", "json", "--print-logs"]
        if session_id:
            command += ["--session", session_id]
        command += prompt_args
        # Collect bytes so a timeout in the middle of a character cannot destroy evidence.
        proc = subprocess.Popen(command, cwd=work, env=self._environment(root, config), start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self._active = proc
        expired = None
        capture_errors = []
        killed = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            expired = exc
            stdout, stderr = exc.output or b"", exc.stderr or b""
            try:
                # Kill descendants too: an inherited pipe must not keep collection waiting indefinitely.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    killed = True
                except ProcessLookupError:
                    pass  # already exited; still collect its final buffered output
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired as drain_exc:
                # communicate's snapshot includes previous output; never concatenate the two snapshots.
                stdout, stderr = drain_exc.output or stdout, drain_exc.stderr or stderr
                capture_errors.append("drain_timeout")
                for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                    try:
                        pipe.close()
                    except Exception as close_exc:
                        capture_errors.append(f"{name}_close:{type(close_exc).__name__}")
                try:
                    proc.wait(timeout=1)
                except Exception as wait_exc:
                    if isinstance(wait_exc, subprocess.TimeoutExpired):
                        capture_errors.append("exit_uncollected")
                    else:
                        capture_errors.append(f"exit_collection:{type(wait_exc).__name__}")
            except Exception as collect_exc:
                capture_errors.append(f"collection:{type(collect_exc).__name__}")
            exc.output, exc.stderr = stdout, stderr
            raise
        finally:
            self._active = None
            # A runtime that dies cannot cancel its own tool calls: retain the broker's honest receipts.
            cancellations = None
            try:
                cancellations = self.bridge.cancel_inflight()
            except Exception as cancel_exc:
                if expired is None:
                    raise
                capture_errors.append(f"cancellation:{type(cancel_exc).__name__}")
            if expired is not None:
                receipt_path = Path(f"{log_prefix}.timeout.json")
                expired.capture_receipt_path = receipt_path
                expired.capture_errors = capture_errors
                streams = {}
                for name, data in (("stdout", stdout), ("stderr", stderr)):
                    try:
                        log_prefix.parent.mkdir(parents=True, exist_ok=True)
                        path = Path(f"{log_prefix}.{name}.log")
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            os.fchmod(stream.fileno(), 0o600)
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        streams[name] = {"path": str(path), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    except Exception as persist_exc:
                        capture_errors.append(f"{name}_persistence:{type(persist_exc).__name__}")
                receipt = {"kind": "native_process_timeout", "timeout_seconds": timeout, "pid": proc.pid,
                           "kill_signal": "SIGKILL" if killed else None, "exit_code": proc.returncode,
                           "cancellations": cancellations, "streams": streams, "encoding": "bytes",
                           "capture_complete": not capture_errors, "capture_errors": capture_errors}
                try:
                    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write((json.dumps(receipt, sort_keys=True) + "\n").encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                except Exception as receipt_exc:
                    capture_errors.append(f"receipt_persistence:{type(receipt_exc).__name__}")
                if capture_errors:
                    expired.add_note("Native timeout capture incomplete: " + ", ".join(capture_errors))
        # Preserve Popen(text=True)'s default decoding and universal-newline behavior on success.
        stdout = io.TextIOWrapper(io.BytesIO(stdout)).read()
        stderr = io.TextIOWrapper(io.BytesIO(stderr)).read()
        result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        log_prefix.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{log_prefix}.stdout.log").write_text(result.stdout)
        Path(f"{log_prefix}.stderr.log").write_text(result.stderr)
        return result

    def interrupt(self, sig: int = signal.SIGINT) -> bool:
        """Send ``sig`` to the running OpenCode process (a user interrupt); False when no turn is running."""
        proc = self._active
        if proc is None or proc.poll() is not None:
            return False
        proc.send_signal(sig)
        # Cancellation must not depend on the runtime honouring the signal (a parent can leave SIGINT ignored): the accepted
        # interrupt cancels and collects the guest work itself. Calls that already finished keep their honest final status.
        self.bridge.cancel_inflight()
        return True

    def audit_request(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Audit one outbound model request; returns the violations it raised (empty when it may be forwarded).

        Tool-bearing requests must advertise only routed tools. The production caller is :class:`ProviderGuard`,
        which every provider request of :meth:`run_turn` passes through before it leaves the host.
        """
        before = len(self.bridge.violations)
        tools = body.get("tools") or []
        names = [t["function"]["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("function"), dict) and isinstance(t["function"].get("name"), str)]
        if len(names) != len(tools):
            self.bridge._violation("unauditable_advertisement", "?", None, None, "tool advertisement without a function name")
        unrouted = set(self.registry.unrouted(names))
        for name in names:
            if name in unrouted:
                self.bridge._violation("unrouted_advertisement", name, None, None, "tool advertised without an explicit route")
            elif self.registry.disposition(name) == "disabled":
                self.bridge._violation("disabled_advertisement", name, None, None, "disabled tool advertised to the model")
        return list(self.bridge.violations[before:])

    def run_turn(self, prompt: str, *, session_id: str | None = None, timeout: float = 120) -> TurnResult:
        """Run one OpenCode turn; ``session_id`` continues a prior host session."""
        self.stage_runtime()
        self._raise_unreported()
        self._turn += 1
        result = self._run_process(self.stage, self.work_dir, self.config_dir, [prompt], self.evidence / f"turn-{self._turn}",
                                   session_id=session_id, timeout=timeout)
        events = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        sid, usage, errors = _summarise(events)
        turn = TurnResult(result.returncode, events, sid, usage, errors, self.evidence / f"turn-{self._turn}.stdout.log", self.evidence / f"turn-{self._turn}.stderr.log")
        self._raise_unreported()
        return turn

    def _raise_unreported(self) -> None:
        """Surface coverage violations once; a run with an unreported violation must not continue."""
        pending = self.bridge.violations[self._reported:]
        if pending:
            self._reported = len(self.bridge.violations)
            raise RouterCoverageError(list(pending))

    def close(self) -> None:
        if self._staged:
            self.guard.close()
            self.bridge.close()

    def __enter__(self) -> "HostOpenCodeAdapter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _DiscoveryProvider:
    """Text-only local provider used solely to capture the runtime's own tool inventory."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                chunk = lambda delta, finish: {"id": "d", "object": "chat.completion.chunk", "created": 1, "model": "discovery", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if body.get("stream"):
                    frames = [chunk({"role": "assistant"}, None), chunk({"content": "ok"}, None), chunk({}, "stop")]
                    payload = ("".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n").encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps({"id": "d", "object": "chat.completion", "created": 1, "model": "discovery", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> "_DiscoveryProvider":
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
