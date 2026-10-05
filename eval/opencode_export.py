"""Standalone OpenCode transcript export contract, shared with host tooling."""

OPENCODE_SESSION_DB = "/home/dev/.local/share/opencode/opencode.db"

_OPENCODE_SESSION_EXPORT_SCRIPT = r'''
import base64
import json
import os
import sqlite3
import sys

path = sys.argv[1]
empty = {
    "captured": False,
    "format": "opencode-sqlite-v1",
    "path": path,
    "reason": "database_not_found",
    "sessions": [],
    "messages": [],
    "parts": [],
    "events": [],
    "session_messages": [],
    "runtime_logs": [],
    "assistant_messages": [],
    "user_messages": [],
    "tool_calls": [],
}
if not os.path.isfile(path):
    print(json.dumps(empty, separators=(",", ":")))
    raise SystemExit(0)

def decode(value):
    if isinstance(value, (bytes, bytearray)):
        return {"encoding": "base64", "data": base64.b64encode(value).decode()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed
    return value

def body(row):
    for key in ("data", "json", "value", "content"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = decode(value)
            if isinstance(parsed, dict):
                return parsed
    return {}

def first(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None

def row_dict(row):
    return {str(key): decode(value) for key, value in zip(row.keys(), row)}

try:
    # URI read-only mode still follows the WAL file, which is important when
    # OpenCode has just flushed its final assistant/tool part.
    connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
except Exception as exc:
    empty["reason"] = f"database_open_failed: {type(exc).__name__}: {exc}"
    print(json.dumps(empty, separators=(",", ":")))
    raise SystemExit(0)

def table_rows(name, limit=5000):
    try:
        quoted = '"' + name.replace('"', '""') + '"'
        rows = connection.execute(
            f"SELECT * FROM {quoted} ORDER BY rowid LIMIT ?", (limit,)
        ).fetchall()
        return [row_dict(row) for row in rows]
    except Exception:
        return []

table_names = {
    row[0]
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
}
export = dict(empty)
export.update({"captured": True, "reason": None})
raw = {}
for name in ("session", "message", "part", "event", "session_message"):
    if name in table_names:
        raw[name] = table_rows(name)
export["sessions"] = raw.get("session", [])
export["messages"] = raw.get("message", [])
export["parts"] = raw.get("part", [])
export["events"] = raw.get("event", [])
export["session_messages"] = raw.get("session_message", [])
export["tables"] = sorted(table_names)
log_dir = os.path.join(os.path.dirname(path), "log")
if os.path.isdir(log_dir):
    for name in sorted(os.listdir(log_dir)):
        log_path = os.path.join(log_dir, name)
        if not os.path.isfile(log_path):
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                export["runtime_logs"].append({"name": name, "text": handle.read()[-200000:]})
        except OSError:
            continue

message_by_id = {}
for row in export["messages"]:
    row_id = first(row, "id", "message_id")
    if row_id is not None:
        message_by_id[str(row_id)] = row

parts_by_message = {}
for row in export["parts"]:
    message_id = first(row, "message_id", "messageId")
    if message_id is not None:
        parts_by_message.setdefault(str(message_id), []).append(row)

def text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_value(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "output", "content", "value"):
            text = text_value(value.get(key))
            if text:
                return text
    return ""

def message_role(row):
    data = body(row)
    return first(data, "role", "messageRole") or first(row, "role")

def message_text(row, message_id):
    data = body(row)
    text = text_value(first(data, "content", "text", "parts"))
    if text:
        return text
    return "\n".join(
        text_value(first(body(part), "text", "content"))
        for part in parts_by_message.get(str(message_id), [])
        if text_value(first(body(part), "text", "content"))
    )

for row in export["messages"]:
    message_id = first(row, "id", "message_id")
    role = message_role(row)
    text = message_text(row, message_id)
    record = {
        "id": message_id,
        "role": role,
        "text": text,
        "timestamp": first(row, "time_created", "created_at", "timestamp", "time"),
    }
    if role == "assistant":
        export["assistant_messages"].append(record)
    elif role == "user":
        export["user_messages"].append(record)

for row in export["parts"]:
    data = body(row)
    if first(data, "type") != "tool":
        continue
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    message_id = first(row, "message_id", "messageId")
    export["tool_calls"].append({
        "id": first(data, "callID", "callId", "call_id", "id") or first(row, "id"),
        "message_id": message_id,
        "part_id": first(row, "id", "part_id"),
        "function": first(data, "tool", "name", "function") or "tool",
        "arguments": first(state, "input", "arguments") or first(data, "input", "arguments") or {},
        "result": first(state, "output", "result") or first(data, "output", "result") or "",
        "error": first(state, "error") or first(data, "error"),
        "status": first(state, "status") or first(data, "status"),
        "title": first(state, "title") or first(data, "title"),
        "timestamp": first(row, "time_created", "created_at", "timestamp", "time"),
    })

connection.close()
print(json.dumps(export, separators=(",", ":")))
'''
