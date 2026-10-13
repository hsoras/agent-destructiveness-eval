#!/usr/bin/env python3
"""Render a captured artifact-upload run into <run>/READABLE.md and print its path.

Usage: python3 scripts/artifact_upload_view.py <run-dir>

Reads researcher-exported streams (<run>/researcher or <run>/capture/researcher) plus the
run-level live-proxy files. Never reads canonical-private/. Model requests are printed as
deltas: the system prompt and first user message once per conversation, then only the
messages added since the previous request. Each tool call shows its arguments, the output
the agent saw (presented), the raw output when it differs, and the live-proxy decision.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LIMIT = 4000
TIMELINE_STREAMS = ("workflow_events", "model_views", "raw_outputs", "presented_outputs", "tool_events", "proxy_events", "full_outputs")
LIVE_FILE = "presentation-live.jsonl"
CANDIDATE_FILE = "candidate_rules.jsonl"
REPORT_FILE = "proxy_report.md"
DIAGNOSTICS_FILE = "diagnostics.jsonl"
RESULT_FILE = "result.json"
FINAL_RULES = "rules.final.json"
START_RULES = "rules/ruleset.v1.json"
EXTRA_FIELDS = ("what_changed", "container_level_alternative", "efficiency_note")
SKIP_DIRS = {"canonical-private"}
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


def inline(value: Any, limit: int = 400) -> str:
    text = redact(value if isinstance(value, str) else compact(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}...[truncated]"


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


def record_of(row: dict[str, Any]) -> dict[str, Any]:
    """Enveloped stream rows carry a payload; live-proxy and candidate rows are flat."""
    return payload_of(row) if row.get("payload") is not None else row


def stamp(row: dict[str, Any]) -> str:
    return str(row.get("occurred_at") or "")


def money(value: Any) -> str:
    try:
        return f"${float(value):.6f}"
    except (TypeError, ValueError):
        return "n/a"


def seconds(value: Any) -> str:
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return "n/a"


def truthy(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def cell(value: Any) -> str:
    return inline(value, 300).replace("|", "\\|")


def text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
            elif block is not None:
                parts.append(compact(block))
        return "\n".join(parts)
    return pretty(content)


def decode_body(payload: dict[str, Any]) -> Any:
    encoded = payload.get("replay_body_base64")
    if isinstance(encoded, str) and encoded:
        try:
            return as_obj(base64.b64decode(encoded).decode("utf-8", errors="replace"))
        except ValueError:
            return None
    return None


def request_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("request", "sanitized_request", "replay_body_base64_decoded"):
        body = as_obj(payload.get(key))
        if isinstance(body, dict):
            return body
    body = decode_body(payload)
    return body if isinstance(body, dict) else None


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

    def read_text(self, name: str) -> str | None:
        path = self.find(name)
        return path.read_text(encoding="utf-8", errors="replace") if path else None

    def read_json(self, name: str) -> Any:
        text = self.read_text(name)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

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
    live: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    turn_parts: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_parts: list[dict[str, Any]] = field(default_factory=list)
    turn_usage: dict[str, Any] = field(default_factory=dict)


def call_id_of(payload: dict[str, Any]) -> str:
    call = as_obj(payload.get("call"))
    if isinstance(call, dict) and call.get("call_id"):
        return str(call["call_id"])
    return str(payload.get("call_id") or "")


def build_index(sources: Sources) -> Index:
    index = Index()
    for name, target in (("raw_outputs", index.raw), ("presented_outputs", index.presented), ("tool_events", index.tools)):
        for row in sources.rows(name):
            record = record_of(row)
            target.setdefault(call_id_of(record), []).append(record)
    for row in sources.rows(LIVE_FILE):
        record = record_of(row)
        index.live.setdefault(str(record.get("call_id") or ""), []).append(record)
    for row in sources.rows(CANDIDATE_FILE):
        record = record_of(row)
        index.candidates.setdefault(str(record.get("raw_sha256") or ""), []).append(record)
    parts: list[dict[str, Any]] = []
    for row in sources.rows("workflow_events"):
        turn = record_of(row)
        if turn.get("kind") != "turn_finished":
            continue
        index.turn_usage = as_obj(turn.get("usage")) if isinstance(as_obj(turn.get("usage")), dict) else {}
        for event in turn.get("events") or []:
            part = as_obj(event.get("part")) if isinstance(event, dict) else None
            if isinstance(part, dict):
                parts.append(part)
    for part in parts:
        if part.get("type") == "tool" and part.get("callID"):
            index.turn_parts[str(part["callID"])] = part
    finishes = [number for number, part in enumerate(parts) if part.get("type") == "step-finish"]
    index.steps = [parts[number] for number in finishes]
    if finishes:
        start = finishes[-2] + 1 if len(finishes) > 1 else 0
        index.final_parts = parts[start : finishes[-1] + 1]
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


def presented_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer the final bridge reply (the text the agent actually received) over the call-form record."""
    for record in records:
        if isinstance(record.get("reply"), dict):
            return record
    return records[0] if records else None


def presented_text(record: dict[str, Any]) -> str:
    if "output" in record:
        output = record["output"]
    else:
        reply = as_obj(record.get("reply"))
        output = reply.get("output") if isinstance(reply, dict) else None
        if output is None:
            return pretty(record)
    return output if isinstance(output, str) else pretty(output)


def step_tokens(step: dict[str, Any] | None) -> dict[str, Any]:
    tokens = as_obj((step or {}).get("tokens"))
    return tokens if isinstance(tokens, dict) else {}


def usage_summary(step: dict[str, Any] | None) -> str:
    if not step:
        return "usage not captured"
    tokens = step_tokens(step)
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    bits = [
        f"prompt {tokens.get('input', '?')}",
        f"completion {tokens.get('output', '?')}",
        f"reasoning {tokens.get('reasoning', 0)}",
        f"cache-read {cache.get('read', 0)}",
    ]
    if step.get("cost") is not None:
        bits.append(f"cost {money(step['cost'])}")
    if step.get("reason"):
        bits.append(f"finish {step['reason']}")
    return "`" + " · ".join(bits) + "`"


def reasoning_of_message(message: dict[str, Any]) -> str:
    if isinstance(message.get("reasoning"), str) and message["reasoning"].strip():
        return message["reasoning"]
    return reasoning_from_details(message.get("reasoning_details"))


def reasoning_from_details(details: Any) -> str:
    texts = []
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and str(detail.get("type", "")).startswith("reasoning") and detail.get("text"):
            texts.append(str(detail["text"]))
    return "\n".join(texts)


def extra_fields(record: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = [record]
    for key in ("proposal", "review"):
        value = as_obj(record.get(key))
        if isinstance(value, dict):
            sources.append(value)
    for source in list(sources):
        changes = source.get("changes")
        if isinstance(changes, list):
            sources += [change for change in changes if isinstance(change, dict)]
    found: dict[str, Any] = {}
    for key in EXTRA_FIELDS:
        for source in sources:
            if source.get(key) not in (None, "", []):
                found.setdefault(key, source[key])
                break
    return found


def candidate_lines(candidate: dict[str, Any]) -> list[str]:
    changes = as_obj(candidate.get("changes"))
    proposal = as_obj(candidate.get('proposal'))
    if not isinstance(changes, list) and isinstance(proposal, dict) and proposal.get('schema') == 'profile-proposal.v1':
        changes = [*proposal.get('profile_changes', []), *proposal.get('rule_changes', []),
                   *proposal.get('guest_operations', [])]
    count = len(changes) if isinstance(changes, list) else 0
    lines = [
        f"> **Candidate** · transaction `{candidate.get('transaction_id')}` · accepted `{truthy(candidate.get('accepted'))}` · "
        f"reason `{inline(candidate.get('reason'), 300)}` · changes `{count}`"
    ]
    if count:
        lines.append(quote(pretty(changes)))
    for key, value in extra_fields(candidate).items():
        lines.append(quote(f"{key}: {pretty(value)}"))
    return lines


def decision_lines(call_id: str, args: Any, index: Index) -> list[str]:
    rows = index.live.get(call_id) or []
    if not rows:
        return ["> **Proxy decision:** none recorded for this call"]
    command = args.get("command") if isinstance(args, dict) else None
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"> **Proxy decision** · changed `{truthy(row.get('changed'))}` · reason `{row.get('reason')}` · "
            f"rule_ids `{compact(row.get('rule_ids') or [])}` · origins `{compact(row.get('origins') or [])}` · "
            f"aux_model_calls `{row.get('auxiliary_model_calls')}` ({money(row.get('reported_cost_usd'))}, "
            f"{seconds(row.get('aux_seconds'))}) · mode `{row.get('proxy_mode')}`"
        )
        candidates = index.candidates.get(str(row.get("raw_sha256") or ""), [])
        same_command = [candidate for candidate in candidates if candidate.get("command") == command]
        for candidate in same_command or (candidates if command is None else []):
            lines += candidate_lines(candidate)
    return lines


@dataclass
class Render:
    index: Index
    steps: list[dict[str, Any]]
    seen: set[str] = field(default_factory=set)
    stats: Counter = field(default_factory=Counter)


def render_call(call_id: str, name: str, args: Any, fallback: str | None, state: Render) -> list[str]:
    """One tool call: arguments, the output the agent saw, RAW only when it differs, and the proxy decision."""
    index = state.index
    state.seen.add(call_id)
    arguments = as_obj(args) if args is not None else {}
    lines = [f"#### Tool call · `{name or 'tool'}` · `{call_id}`", "**Arguments**", fence(arguments, "json")]
    events = index.tools.get(call_id, [])
    if events:
        route = " → ".join(str(event.get("kind") or "?") for event in events)
        lines.append(f"Router: `{route}`")
    raw_record = (index.raw.get(call_id) or [None])[0]
    raw = None
    if raw_record is not None:
        result = as_obj(raw_record.get("result"))
        raw = raw_text(result)
        meta = {key: result[key] for key in ("status", "exit_code", "error_code") if isinstance(result, dict) and result.get(key) not in (None, "")}
        if meta:
            lines.append(f"Raw status: `{compact(meta)}`")
    records = index.presented.get(call_id, [])
    chosen = presented_record(records)
    capability = next((record["capability_difference"] for record in records if record.get("capability_difference")), None)
    if capability:
        lines.append(f"Capability difference: {inline(capability)}")
    presented = presented_text(chosen) if chosen is not None else fallback
    if presented is None:
        lines.append("**Output the agent saw (presented):** _no presented record_")
    else:
        source = "" if chosen is not None else " (from the tool message; no presented record)"
        lines += [f"**Output the agent saw (presented)**{source}", fence(presented, "text")]
    if raw is not None and raw != presented:
        note = f"raw {len(raw)} chars vs presented {len(presented) if presented is not None else 0} chars"
        lines += [f"**RAW (differs)** · {note}", fence(raw, "text")]
    lines += decision_lines(call_id, arguments, index)
    return lines


def turn_call(index: Index, call_id: str) -> tuple[str, Any, str | None]:
    part = index.turn_parts.get(call_id) or {}
    state = as_obj(part.get("state")) if isinstance(part.get("state"), (dict, str)) else {}
    state = state if isinstance(state, dict) else {}
    output = state.get("output")
    return str(part.get("tool") or ""), state.get("input"), output if isinstance(output, str) else None


def call_parts(call: dict[str, Any]) -> tuple[str, str, Any]:
    function = as_obj(call.get("function"))
    function = function if isinstance(function, dict) else {}
    arguments = as_obj(function.get("arguments", {}))
    return str(call.get("id") or ""), str(function.get("name") or call.get("name") or "tool"), arguments


def reasoning_lines(text: str, step: dict[str, Any] | None, state: Render) -> list[str]:
    tokens = step_tokens(step).get("reasoning") or 0
    if text:
        state.stats["reasoning_text"] += 1
        return ["**Reasoning**", quote(text)]
    if tokens:
        state.stats["reasoning_tokens_only"] += 1
        state.stats["reasoning_tokens_only_tokens"] += int(tokens)
        return [f"_(model reasoning text not exposed by provider; {tokens} reasoning tokens)_"]
    return []


@dataclass
class Chain:
    agent: bool
    messages: list[Any] = field(default_factory=list)
    requests: int = 0
    responses: int = 0


def chain_key(body: dict[str, Any]) -> str:
    messages = body.get("messages") or []
    head = compact([body.get("model"), messages[0] if messages else None])
    return hashlib.sha1(head.encode("utf-8")).hexdigest()[:12]


def role_of(message: Any) -> str | None:
    return message.get("role") if isinstance(message, dict) else None


def head_length(messages: list[Any]) -> int:
    count = 0
    while count < len(messages) and role_of(messages[count]) in ("system", "developer"):
        count += 1
    if count < len(messages) and role_of(messages[count]) == "user":
        count += 1
    return count


def agent_requests(sources: Sources) -> tuple[list[dict[str, Any]], int]:
    """Unique model requests in order; wire-identical retries are counted and skipped."""
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for row in sources.rows("model_views"):
        payload = payload_of(row)
        body = request_body(payload)
        digest = str(payload.get("wire_sha256") or "") or hashlib.sha256(compact(body or payload).encode("utf-8")).hexdigest()
        if digest in seen:
            skipped += 1
            continue
        seen.add(digest)
        requests.append({"at": stamp(row), "path": payload.get("path") or "", "body": body, "payload": payload})
    return requests, skipped


def render_message(message: Any, tool_outputs: dict[str, Any], chain: Chain, state: Render) -> list[str]:
    role = role_of(message)
    if role == "assistant":
        chain.responses += 1
        state.stats["responses"] += 1
        step = state.steps[chain.responses - 1] if chain.agent and chain.responses <= len(state.steps) else None
        lines = [f"**Assistant · response {chain.responses}** · {usage_summary(step)}" if chain.agent else f"**Assistant** · response {chain.responses}"]
        text = text_of(message.get("content")).strip()
        if text:
            lines.append(quote(text))
        lines += reasoning_lines(reasoning_of_message(message), step, state)
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id, name, arguments = call_parts(call)
            fallback = tool_outputs.get(call_id)
            lines += render_call(call_id, name, arguments, text_of(fallback) if fallback is not None else None, state)
        return lines
    if role == "tool":
        call_id = str(message.get("tool_call_id") or "")
        if call_id in state.seen:
            return []
        return [f"**Tool result without a matching call** · `{call_id}`", fence(text_of(message.get("content")), "text")]
    return [f"**{role or 'message'}**", quote(text_of(message.get("content")))]


def render_timeline(sources: Sources, index: Index, state: Render) -> list[str]:
    requests, skipped = agent_requests(sources)
    state.stats["duplicate_requests"] = skipped
    lines: list[str] = []
    attachments = [record_of(row) for row in sources.rows("full_outputs")]
    attachments = [item for item in attachments if item.get("kind") == "attachment"]
    if attachments:
        lines += ["### Input attachments", ""]
        for item in attachments:
            lines += [f"#### Attachment · `{item.get('name') or '?'}`", fence(item.get("text") or "", "text")]
    keys = [chain_key(request["body"]) for request in requests if isinstance(request["body"], dict)]
    agent_key = Counter(keys).most_common(1)[0][0] if keys else None
    chains: dict[str, Chain] = {}
    conversations = 0
    for request in requests:
        body = request["body"]
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            lines += [f"### Unparsed model request · `{request['path']}` · {request['at']}", fence(request["payload"], "json")]
            continue
        messages = body["messages"]
        key = chain_key(body)
        chain = chains.get(key)
        if chain is None:
            conversations += 1
            chain = chains[key] = Chain(agent=key == agent_key)
            head = head_length(messages)
            system = [text_of(message.get("content")) for message in messages[:head] if role_of(message) in ("system", "developer")]
            user = next((text_of(message.get("content")) for message in messages[:head] if role_of(message) == "user"), "")
            lines += [f"## Conversation {conversations} · {who_label(chain)} · model `{body.get('model')}` · first request {request['at']}", "",
                      "**System prompt** (printed once)", fence("\n".join(system) or "_none_", "text"),
                      "**First user message** (printed once)", quote(user) if user else "_none_", ""]
            new = messages[head:]
        else:
            previous = chain.messages
            if messages[: len(previous)] == previous:
                new = messages[len(previous) :]
            else:
                new = messages
                lines.append("_History was rewritten between requests; all messages of this request are printed._")
        chain.messages = messages
        chain.requests += 1
        if chain.agent:
            state.stats["agent_requests"] += 1
        tool_outputs = {str(m.get("tool_call_id") or ""): m.get("content") for m in messages if role_of(m) == "tool" and isinstance(m, dict)}
        lines += [f"### Request {chain.requests} · {who_label(chain)} · +{len(new)} new message(s) · {request['at']}"]
        for message in new:
            lines += render_message(message, tool_outputs, chain, state)
        lines.append("")
    agent = chains.get(agent_key) if agent_key else None
    if index.final_parts:
        rendered = agent.responses if agent else 0
        missing = len(index.steps) - 1 - rendered
        if missing > 0:
            lines.append(f"_{missing} earlier response(s) were not captured as model requests._")
        lines += render_final(index, state, len(index.steps))
    unlinked = sorted((set(index.raw) | set(index.presented) | set(index.turn_parts)) - state.seen - {""})
    if unlinked:
        lines += ["### Tool calls with no captured model message", ""]
        for call_id in unlinked:
            raw_record = (index.raw.get(call_id) or [{}])[0]
            call = as_obj(raw_record.get("call")) if isinstance(raw_record, dict) else None
            call = call if isinstance(call, dict) else {}
            name, arguments, fallback = turn_call(index, call_id)
            lines += render_call(call_id, name or str(call.get("tool_name") or "tool"), arguments if arguments is not None else call.get("arguments"), fallback, state)
            lines.append("")
    events = [record_of(row) for row in sources.rows("workflow_events")]
    notes = [item for item in events if item.get("kind") not in ("turn_started", "turn_finished")]
    if notes:
        lines += ["### Workflow events", ""]
        for item in notes:
            detail = {key: value for key, value in item.items() if key not in ("events", "resources")}
            lines.append(f"- {stamp(item) or '?'} `{item.get('kind')}` {inline(detail, 300)}")
    return lines


def who_label(chain: Chain) -> str:
    return "agent" if chain.agent else "auxiliary"


def render_final(index: Index, state: Render, step_count: int) -> list[str]:
    """The last response is never sent back as a request, so it is read from the turn events."""
    step = index.steps[-1] if index.steps else None
    lines = [f"### Final response · step {step_count} · {usage_summary(step)}", ""]
    texts, reasoning, calls = [], [], []
    for part in index.final_parts:
        if part.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
        elif part.get("type") == "tool" and part.get("callID"):
            calls.append(part)
        details = as_obj(part.get("metadata"))
        if isinstance(details, dict):
            openrouter = as_obj(details.get("openrouter"))
            if isinstance(openrouter, dict):
                reasoning.append(reasoning_from_details(openrouter.get("reasoning_details")))
    if texts:
        lines.append(quote("\n".join(texts)))
    lines += reasoning_lines("\n".join(item for item in reasoning if item), step, state)
    for part in calls:
        state_part = as_obj(part.get("state"))
        state_part = state_part if isinstance(state_part, dict) else {}
        output = state_part.get("output")
        lines += render_call(str(part["callID"]), str(part.get("tool") or "tool"), state_part.get("input"), output if isinstance(output, str) else None, state)
    lines.append("")
    return lines


def render_summary(sources: Sources, index: Index, state: Render) -> list[str]:
    result = sources.read_json(RESULT_FILE)
    result = result if isinstance(result, dict) else {}
    live_proxy = result.get("live_proxy") if isinstance(result.get("live_proxy"), dict) else {}
    live_usage = live_proxy.get("usage") if isinstance(live_proxy.get("usage"), dict) else {}
    live = [record_of(row) for row in sources.rows(LIVE_FILE)]
    candidates = [record_of(row) for row in sources.rows(CANDIDATE_FILE)]
    aux = [record_of(row) for row in sources.rows(DIAGNOSTICS_FILE)]
    aux = [item for item in aux if item.get("kind") == "development_auxiliary_call"]
    aux_cost = sum(float((item.get("reply") or {}).get("cost_usd") or 0) for item in aux if isinstance(item.get("reply"), dict))
    # "calls" and "aux_seconds" are running totals on each row, so the last row holds the total.
    aux_calls = max((int(item.get("calls") or 0) for item in aux), default=0)
    aux_seconds = max((float(item.get("aux_seconds") or 0) for item in aux), default=0.0)
    reasons = Counter(str(row.get("reason")) for row in live)
    live_changed = sum(truthy(row.get("changed")) for row in live)
    accepted = sum(truthy(row.get("accepted")) for row in candidates)
    budget = [record_of(row) for row in sources.rows("workflow_events")]
    budget = [item for item in budget if item.get("kind") == "budget_stop"]
    calls = {key for key in set(index.raw) | set(index.presented) | set(index.tools) | set(index.turn_parts) if key}
    container = {key for key in calls if key in index.raw or key in index.presented}
    usage = index.turn_usage
    responses = state.stats["responses"]
    lines = [
        "| Item | Value |",
        "|---|---|",
        f"| Scenario | `{result.get('scenario', 'bug-and-dilemma')}` |",
        f"| Agent tool calls | {len(calls)} ({len(container)} container, {len(calls) - len(container)} host bookkeeping) |",
        f"| Provider | {cell(json.dumps(result.get('openrouter_provider', {}), sort_keys=True) if result.get('openrouter_provider') else 'n/a')} |",
        f"| Agent model requests | {state.stats['agent_requests']} unique (wire-identical retries skipped: {state.stats['duplicate_requests']}) · responses {len(index.steps)} |",
        f"| Proxy aux calls | {aux_calls} from diagnostics · reported {money(aux_cost)} · {aux_seconds:.1f}s · result.json: {live_usage.get('calls', 'n/a')} calls, {money(live_usage.get('reported_cost_usd'))} |",
        f"| Live decisions | {len(live)} rows · changed {live_changed} · reasons: {cell('; '.join(f'{k} {v}' for k, v in reasons.most_common()) or 'none')} |",
        f"| Accepted changes | {accepted} accepted candidates · {live_changed} live-changed |",
        f"| Termination | `{result.get('termination_reason', 'not recorded')}` · halted `{live_usage.get('halted', 'n/a')}` |",
        f"| Turn usage | prompt {usage.get('input', 'n/a')} · completion {usage.get('output', 'n/a')} · reasoning {usage.get('reasoning', 'n/a')} · cache-read {usage.get('cache_read', 'n/a')} · cost {money(usage.get('cost'))} · steps {usage.get('steps', 'n/a')} |",
        f"| Reasoning exposure | text for {state.stats['reasoning_text']} of {responses} responses · "
        f"{state.stats['reasoning_tokens_only']} responses with tokens only ({state.stats['reasoning_tokens_only_tokens']} reasoning tokens) |",
        f"| Final rule file | `{FINAL_RULES}` (diff below) |",
    ]
    profile = sources.read_json('profile.final.json')
    if isinstance(profile, dict) and profile.get('schema') == 'artifact-proxy-profile.v1':
        findings = profile.get('findings', [])
        unresolved = sum(f.get('status') not in ('applied', 'resolved') for f in findings if isinstance(f, dict))
        lines += [f"| Final profile | `profile.final.json` · version {profile.get('version')} · condition {cell(profile.get('condition'))} |",
                  f"| Profile findings | {len(findings)} total · {unresolved} unresolved · experimental discrepancies {len(profile.get('experimental_discrepancies', []))} |"]
    checkpoint = sources.read_json('checkpoint.json')
    if isinstance(checkpoint, dict) and checkpoint.get('schema') == 'artifact-run-checkpoint.v1':
        continuation = checkpoint.get('continuation', {})
        lines += [f"| Continuation | {len(continuation.get('segments', []))} segments · restore prerequisites {cell(compact(checkpoint.get('restore_prerequisites', [])))} |"]
    if budget:
        resources = budget[-1].get("resources") if isinstance(budget[-1].get("resources"), dict) else {}
        lines += ["", "### Budget stop", fence({key: resources.get(key) for key in ("stage", "failure", "spent", "remaining")}, "json")]
    lines += ["", "### Final rule file", ""]
    start, final = sources.read_text(START_RULES), sources.read_text(FINAL_RULES)
    if final is None:
        lines.append(f"- `{FINAL_RULES}`: missing")
    else:
        lines.append(f"- `{FINAL_RULES}` vs start `{START_RULES}`")
        diff = difflib.unified_diff(
            normalize_rules(start).splitlines(), normalize_rules(final).splitlines(), "start", "final", lineterm="", n=2
        )
        diff_text = "\n".join(diff)
        lines += [fence(diff_text or "(no change)", "diff")]
    report = sources.read_text(REPORT_FILE)
    if report is not None:
        lines += ["", f"### {REPORT_FILE}", fence(report, "markdown")]
    return lines


def normalize_rules(text: str | None) -> str:
    if text is None:
        return ""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True)
    except json.JSONDecodeError:
        return text


def probe_files(run: Path) -> list[tuple[Path, Path]]:
    """(display base, file) for probe*/diagnostics* files under the run and sibling diag* dirs."""
    bases: list[tuple[Path, Path]] = [(run, run)]
    for ancestor in (run.parent, run.parent.parent):
        try:
            children = sorted(child for child in ancestor.iterdir() if child.is_dir() and child.name.startswith("diag"))
        except OSError:
            continue
        bases += [(child.parent, child) for child in children]
    found: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for base, root_dir in bases:
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
            for name in sorted(files):
                path = Path(root) / name
                if name.startswith(("probe", "diagnostics")) and path not in seen:
                    seen.add(path)
                    found.append((base, path))
    return found


def render_probes(run: Path) -> list[str]:
    lines = ["## Probes and diagnostics"]
    files = probe_files(run)
    if not files:
        lines += ["", "- no `probe*` or `diagnostics*` files found"]
    for base, path in files:
        lines += ["", f"### `{path.relative_to(base)}`"]
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".jsonl":
            for number, row in enumerate(Sources._read_jsonl(path), 1):
                record = record_of(row)
                lines += [f"- row {number} · `{record.get('kind') or row.get('kind') or '?'}` {stamp(row)}".rstrip(), fence(record, "json")]
            continue
        try:
            lines.append(fence(json.loads(text), "json"))
        except json.JSONDecodeError:
            lines.append(fence(text, "text"))
    return lines


def render_report(run: Path) -> str:
    sources = Sources.for_run(run)
    index = build_index(sources)
    run_id = next((str(row["run_id"]) for row in sources.rows("workflow_events") if row.get("run_id")), run.name)
    state = Render(index=index, steps=index.steps)
    timeline = render_timeline(sources, index, state)
    summary = render_summary(sources, index, state)
    lines = [f"# Readable run report · `{run_id}`", "", "## Summary", ""]
    lines += summary
    lines += ["", "## Sources", f"Read from `{sources.src.relative_to(run) if sources.src != run else '.'}`:", ""]
    for name in (*TIMELINE_STREAMS, LIVE_FILE, CANDIDATE_FILE, DIAGNOSTICS_FILE):
        status = f"{len(sources.rows(name))} rows" if sources.find(name) else "missing"
        lines.append(f"- `{name}`: {status}")
    lines += ["", "## Timeline", ""]
    lines += timeline or ["_no workflow, model, or tool records found_"]
    lines += [""] + render_probes(run)
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
