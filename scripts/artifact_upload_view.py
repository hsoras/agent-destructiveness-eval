#!/usr/bin/env python3
"""Render a captured artifact-upload run into <run>/READABLE.md and print its path.

Usage: python3 scripts/artifact_upload_view.py <run-dir>

Reads only researcher-exported streams (<run>/researcher or <run>/capture/researcher),
never canonical-private/. Missing files are skipped; unknown fields are shown as JSON.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import re
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from typing import Any

LIMIT = 4000
TIMELINE_STREAMS = ("workflow_events", "model_views", "raw_outputs", "presented_outputs", "tool_events", "proxy_events", "full_outputs")
LIVE_FILES = ("presentation-live.jsonl", "candidate_rules.jsonl")
REPORT_FILE = "proxy_report.md"
DIAGNOSTICS_FILE = "diagnostics.jsonl"
SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "[REDACTED-KEY]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:api[_-]?key|token|secret|authorization|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.IGNORECASE), r"\1[REDACTED]"),
)


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clip(text: str) -> str:
    text = redact(text)
    if len(text) <= LIMIT:
        return text
    return f"{text[:LIMIT]}\n...[truncated {len(text) - LIMIT} chars]"


def fence(text: Any, lang: str = "") -> str:
    text = clip(text if isinstance(text, str) else pretty(text))
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    mark = "`" * max(3, longest + 1)
    return f"{mark}{lang}\n{text}\n{mark}"


def quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in (clip(text).splitlines() or [""]))


def pretty(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def as_obj(value: Any) -> Any:
    """Parse JSON-looking strings; leave everything else untouched."""
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def payload_of(row: dict[str, Any]) -> dict[str, Any]:
    payload = as_obj(row.get("payload"))
    return payload if isinstance(payload, dict) else {"value": payload}


def stamp(row: dict[str, Any]) -> str:
    return str(row.get("occurred_at") or "")


def label(row: dict[str, Any]) -> str:
    kind = payload_of(row).get("kind") or row.get("kind") or row.get("type") or ""
    return f"{stamp(row)} `{kind}`".strip()


def decode_body(payload: dict[str, Any]) -> Any:
    encoded = payload.get("replay_body_base64")
    if isinstance(encoded, str) and encoded:
        try:
            return as_obj(base64.b64decode(encoded).decode("utf-8", errors="replace"))
        except ValueError:
            return None
    return None


@dataclass
class Sources:
    """Resolves files inside the researcher export (preferred) or the run root."""

    run: Path
    src: Path
    _rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def for_run(cls, run: Path) -> Sources:
        for candidate in (run / "researcher", run / "capture" / "researcher"):
            if candidate.is_dir():
                return cls(run, candidate)
        return cls(run, run)

    def find(self, name: str) -> Path | None:
        file_name = name if "." in name else f"{name}.jsonl"
        for base in (self.src, self.run):
            if (base / file_name).is_file():
                return base / file_name
        return None

    def rows(self, name: str) -> list[dict[str, Any]]:
        if name not in self._rows:
            self._rows[name] = self._read_jsonl(self.find(name))
        return self._rows[name]

    @staticmethod
    def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
        if path is None:
            return []
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                row = {"unparsed_line": number, "text": line}
            rows.append(row if isinstance(row, dict) else {"value": row})
        return rows


@dataclass
class Index:
    raw: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    presented: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tools: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    matched: set[str] = field(default_factory=set)


def call_id_of(payload: dict[str, Any]) -> str:
    call = as_obj(payload.get("call"))
    if isinstance(call, dict) and call.get("call_id"):
        return str(call["call_id"])
    return str(payload.get("call_id") or "")


def build_index(sources: Sources) -> Index:
    index = Index()
    for row in sources.rows("workflow_events"):
        payload = payload_of(row)
        if payload.get("kind") != "turn_finished":
            continue
        for event in payload.get("events") or []:
            part = as_obj(event.get("part")) if isinstance(event, dict) else None
            if isinstance(part, dict) and (part.get("type") or event.get("type")) == "tool" and part.get("callID"):
                index.matched.add(str(part["callID"]))
    for row in sources.rows("raw_outputs"):
        payload = payload_of(row)
        index.raw.setdefault(call_id_of(payload), []).append(payload)
    for row in sources.rows("presented_outputs"):
        payload = payload_of(row)
        index.presented.setdefault(call_id_of(payload), []).append(payload)
    for row in sources.rows("tool_events"):
        payload = payload_of(row)
        index.tools.setdefault(call_id_of(payload), []).append(payload)
    return index


def raw_text(result: Any) -> str:
    if not isinstance(result, dict):
        return pretty(result)
    if isinstance(result.get("output"), str):
        return result["output"]
    parts = []
    if result.get("stdout"):
        parts.append(str(result["stdout"]))
    if result.get("stderr"):
        parts.append(f"[stderr]\n{result['stderr']}")
    return "\n".join(parts) if parts else pretty(result)


def presented_text(payload: dict[str, Any]) -> str:
    if "output" in payload:
        output = payload["output"]
    else:
        reply = as_obj(payload.get("reply"))
        output = reply.get("output") if isinstance(reply, dict) else None
        if output is None:
            return pretty(payload)
    return output if isinstance(output, str) else pretty(output)


def render_records(call_id: str, index: Index, state: dict[str, Any] | None = None) -> list[str]:
    lines: list[str] = []
    events = index.tools.get(call_id, [])
    if events:
        lines.append("**Router events**")
        shown = []
        for event in events:
            detail = {key: value for key, value in (("presented", event.get("presented")), ("metadata", event.get("metadata"))) if value}
            shown.append(f"{event.get('kind', '?')}: {compact(detail)[:300]}")
        lines.append(fence("\n".join(shown), "text"))
    raws = index.raw.get(call_id, [])
    presented = index.presented.get(call_id, [])
    if not raws and not presented:
        if state:
            lines += ["No raw or presented records; agent-visible tool state:", fence(state, "json")]
        return lines
    for raw_payload, pres_payload in zip_longest(raws, presented):
        raw = None
        if raw_payload is not None:
            result = as_obj(raw_payload.get("result"))
            raw = raw_text(result)
            meta = {key: result.get(key) for key in ("status", "exit_code", "error_code") if isinstance(result, dict) and key in result}
            if meta:
                lines.append(f"Status: `{compact(meta)}`")
            lines += ["**RAW output**", fence(raw, "text")]
        else:
            lines.append("**RAW output:** _no raw record_")
        if pres_payload is not None:
            if pres_payload.get("capability_difference"):
                lines.append(f"Capability difference: {pres_payload['capability_difference']}")
            presented_out = presented_text(pres_payload)
            lines += ["**PRESENTED output**", fence(presented_out, "text")]
            if raw is not None and raw != presented_out:
                diff = difflib.unified_diff(raw.splitlines(), presented_out.splitlines(), "raw", "presented", lineterm="", n=1)
                lines += [f"> **Diff note:** presented output differs from raw ({len(raw)} → {len(presented_out)} chars).", fence("\n".join(diff), "diff")]
        else:
            lines.append("**PRESENTED output:** _no presented record_")
    return lines


def render_tool(part: dict[str, Any], index: Index) -> list[str]:
    call_id = str(part.get("callID") or "")
    state = as_obj(part.get("state"))
    state = state if isinstance(state, dict) else {"state": state}
    lines = [f"#### Tool call · `{part.get('tool') or 'tool'}` · `{call_id}`", "**Arguments**", fence(state.get("input", {}), "json")]
    return lines + render_records(call_id, index, state)


def render_turn(payload: dict[str, Any], index: Index) -> str:
    lines = ["### Agent output"]
    if payload.get("usage"):
        lines.append(f"Usage: `{compact(payload['usage'])}`")
    if payload.get("errors"):
        lines += ["**Errors**", fence(payload["errors"], "json")]
    for event in payload.get("events") or []:
        part = as_obj(event.get("part")) if isinstance(event, dict) else None
        if not isinstance(part, dict):
            lines += [fence(event, "json")]
            continue
        kind = part.get("type") or (event.get("type") if isinstance(event, dict) else None)
        if kind == "text":
            lines += ["#### Assistant output", clip(str(part.get("text") or ""))]
        elif kind == "reasoning":
            lines += ["#### Thinking", quote(str(part.get("text") or ""))]
        elif kind == "tool":
            lines += render_tool(part, index)
        elif kind == "step-finish":
            lines.append(f"_step finished: reason `{part.get('reason')}`, tokens `{compact(as_obj(part.get('tokens')))}`_")
        elif kind == "step-start":
            continue
        else:
            lines += [f"#### Event · `{kind}`", fence(part, "json")]
    return "\n\n".join(lines)


def render_model_view(payload: dict[str, Any]) -> str:
    request = as_obj(payload.get("request")) or as_obj(payload.get("sanitized_request")) or decode_body(payload)
    lines = [f"### Model request · `{payload.get('path') or ''}`"]
    if not isinstance(request, dict):
        return "\n\n".join(lines + [fence(request if request is not None else payload, "json")])
    if request.get("model"):
        lines.append(f"Model: `{request['model']}`")
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            lines.append(fence(message, "json"))
            continue
        lines.append(f"**{message.get('role') or '?'}**")
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                lines.append(quote(block["text"]))
            elif isinstance(block, str):
                lines.append(quote(block))
            else:
                lines.append(fence(block, "json"))
    return "\n\n".join(lines)


def render_timeline(sources: Sources, index: Index) -> list[str]:
    entries: list[tuple[str, int, str]] = []

    def add(ts: str, markdown: str) -> None:
        entries.append((ts, len(entries), markdown))

    turn = 0
    for row in sources.rows("workflow_events"):
        payload = payload_of(row)
        kind = payload.get("kind")
        if kind == "turn_started":
            turn += 1
            add(stamp(row), f"## Turn {turn} · started\n\n#### User prompt\n{quote(str(payload.get('prompt') or ''))}")
        elif kind == "turn_finished":
            add(stamp(row), render_turn(payload, index))
        else:
            add(stamp(row), f"#### Workflow event · `{kind}`\n{fence(payload, 'json')}")
    for row in sources.rows("model_views"):
        add(stamp(row), render_model_view(payload_of(row)))
    for row in sources.rows("full_outputs"):
        payload = payload_of(row)
        if payload.get("kind") == "attachment":
            add(stamp(row), f"#### Attachment input · `{payload.get('name') or '?'}`\n{fence(payload.get('text') or '', 'text')}")
    emitted: set[str] = set()
    for name in ("raw_outputs", "presented_outputs", "tool_events"):
        for row in sources.rows(name):
            call_id = call_id_of(payload_of(row))
            if call_id and call_id not in index.matched and call_id not in emitted:
                emitted.add(call_id)
                body = "\n\n".join(render_records(call_id, index))
                add(stamp(row), f"#### Tool record without turn part · `{call_id}`\n\n{body}")
    for row in sources.rows("proxy_events"):
        payload = payload_of(row)
        add(stamp(row), f"#### Proxy event · `{payload.get('kind') or '?'}`\n{fence(payload, 'json')}")
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [markdown for _, _, markdown in entries]


def render_live(sources: Sources) -> list[str]:
    lines = ["## Live proxy"]
    for name in LIVE_FILES:
        if sources.find(name) is None:
            lines.append(f"- `{name}`: not present")
            continue
        rows = sources.rows(name)
        lines += ["", f"### {name} ({len(rows)} rows)"]
        for number, row in enumerate(rows, 1):
            lines += [f"- {number}. {label(row)}", fence(row, "json")]
    report = sources.find(REPORT_FILE)
    lines.append("")
    if report is None:
        lines.append(f"- `{REPORT_FILE}`: not present")
    else:
        lines += [f"### {REPORT_FILE}", fence(report.read_text(encoding="utf-8", errors="replace"), "markdown")]
    return lines


def render_diagnostics(sources: Sources) -> list[str]:
    lines = ["## Diagnostics and probes"]
    if sources.find(DIAGNOSTICS_FILE) is None:
        lines.append(f"- `{DIAGNOSTICS_FILE}`: not present")
    for number, row in enumerate(sources.rows(DIAGNOSTICS_FILE), 1):
        lines += ["", f"### Diagnostic {number} · {label(row)}", fence(payload_of(row), "json")]
    probes = sorted({path for base in (sources.src, sources.run) for path in base.glob("*probe*") if path.is_file()})
    if not probes:
        lines.append("- no `*probe*` files present")
    for probe in probes:
        lines += ["", f"### {probe.name}"]
        if probe.suffix == ".jsonl":
            lines += [fence(row, "json") for row in Sources._read_jsonl(probe)]
            continue
        text = probe.read_text(encoding="utf-8", errors="replace")
        try:
            lines.append(fence(json.loads(text), "json"))
        except json.JSONDecodeError:
            lines.append(fence(text, "text"))
    return lines


def render_report(run: Path) -> str:
    sources = Sources.for_run(run)
    index = build_index(sources)
    run_id = next((str(row["run_id"]) for row in sources.rows("workflow_events") if row.get("run_id")), run.name)
    lines = [f"# Readable run report · `{run_id}`", "", "## Sources", f"Read from `{sources.src.relative_to(run) if sources.src != run else '.'}`:", ""]
    known = (*TIMELINE_STREAMS, *LIVE_FILES, DIAGNOSTICS_FILE)
    for name in known:
        status = f"{len(sources.rows(name))} rows" if sources.find(name) else "missing"
        lines.append(f"- `{name}`: {status}")
    lines.append(f"- `{REPORT_FILE}`: {'present' if sources.find(REPORT_FILE) else 'missing'}")
    lines += ["", "## Timeline", ""]
    lines += ["\n\n".join(render_timeline(sources, index)) or "_no workflow, model, or tool records found_"]
    lines += [""] + render_live(sources) + [""] + render_diagnostics(sources)
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path, help="run directory (containing researcher/ or capture/researcher/)")
    args = parser.parse_args(argv)
    run = args.run_dir.resolve()
    if not run.is_dir():
        parser.error(f"not a directory: {run}")
    target = run / "READABLE.md"
    target.write_text(render_report(run), encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
