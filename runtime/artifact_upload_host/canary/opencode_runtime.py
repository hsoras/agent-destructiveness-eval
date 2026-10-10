"""Assert the supported OpenCode 1.14.41 custom-tool seam without a live provider.

Only local mock model requests are made. Runtime config dependencies must be
prepared ahead of time; the isolated runtime uses the pinned offline seed.
All temporary evidence is retained, including on failure.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

SOURCE = Path(os.environ.get("ARTIFACT_MIGRATION_SOURCE", "/Users/sarosh/programming/agent-destructiveness-eval"))
BINARY = SOURCE / ".scenario/migration-readiness/evidence/t001-native-build-lock-resolution-openai-20261010/artifacts/production-channel/opencode"
SEED = Path(os.environ.get("OPENCODE_CANARY_PLUGIN_SEED", str(SOURCE / ".scenario/migration-readiness/opencode-canary-plugin-1.14.41")))
SOURCE_ARCHIVE = Path(os.environ.get("OPENCODE_SOURCE_ARCHIVE", "/private/tmp/opencode-8ba2a917.tar.gz"))
BINARY_DIGEST = "d871b0030a0b8d42b072da1f1c9f2b0cde11ee933c07955a1674888a2c01c1f6"
EXPECTED_TOOLS = {"bash", "read", "glob", "grep", "edit", "write", "task", "webfetch", "todowrite", "skill"}


def main() -> None:
    from source_digest import verify as verify_source

    source_evidence = verify_source(SOURCE_ARCHIVE)
    assert hashlib.sha256(BINARY.read_bytes()).hexdigest() == BINARY_DIGEST, "pinned runtime digest changed"
    assert subprocess.check_output([str(BINARY), "--version"], text=True).strip() == "1.14.41"
    for package, version in (("@opencode-ai/plugin", "1.14.41"), ("@opencode-ai/sdk", "1.14.41"), ("zod", "4.1.8")):
        assert json.loads((SEED / "node_modules" / package / "package.json").read_text())["version"] == version, f"missing/changed offline prerequisite {package}@{version}"
    root = Path(tempfile.mkdtemp(prefix="artifact-opencode-canary-"))
    config, work = root / "config", root / "work"
    (config / "tools").mkdir(parents=True)
    work.mkdir()
    sentinel = work / "HOST_SENTINEL"
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(SEED / name, config / name)
    shutil.copytree(SEED / "node_modules", config / "node_modules")
    (config / "tools/bash.js").write_text("import {tool} from '@opencode-ai/plugin'; export default tool({description:'broker bash',args:{command:tool.schema.string()},async execute(args){return 'MOCK_WORKER_RESULT:'+args.command}})\n")
    for name in EXPECTED_TOOLS - {"bash"} | {"apply_patch"}:
        (config / "tools" / f"{name}.js").write_text(f"export default {{description:'disabled {name} canary route',args:{{}},async execute(){{throw new Error('unsupported: {name}')}}}}\n")
    requests: list[dict] = []
    sent: set[str] = set()
    errors: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "body": body})
                tools = body.get("tools", [])
                main_request = bool(tools)
                tool_call = None
                content = "Canary title"
                if main_request:
                    history = json.dumps(body.get("messages", []))
                    content = "CANARY_DONE"
                    if "call_canary_001" not in sent:
                        name, call_id, arguments = "bash", "call_canary_001", {"command": f"touch {sentinel}"}
                    elif "call_unknown_001" not in sent and "MOCK_WORKER_RESULT" in history:
                        name, call_id, arguments = "unregistered_canary", "call_unknown_001", {"command": f"touch {sentinel}"}
                    elif "call_disabled_001" not in sent and "call_unknown_001" in history:
                        name, call_id, arguments = "write", "call_disabled_001", {"filePath": str(sentinel), "content": "BAD"}
                    else:
                        name = None
                    if name:
                        sent.add(call_id)
                        tool_call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                        content = None
                finish = "tool_calls" if tool_call else "stop"
                if body.get("stream"):
                    deltas = [{"role": "assistant"}]
                    deltas.append({"tool_calls": [{"index": 0, **tool_call}]} if tool_call else {"content": content})
                    frames = [{"id": "canary", "object": "chat.completion.chunk", "created": 1, "model": "test-canary", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
                    frames.append({"id": "canary", "object": "chat.completion.chunk", "created": 1, "model": "test-canary", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                    payload = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
                    content_type = "text/event-stream"
                else:
                    message = {"role": "assistant", "content": content, "tool_calls": [tool_call] if tool_call else None}
                    payload = json.dumps({"id": "canary", "object": "chat.completion", "created": 1, "model": "test-canary", "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(payload.encode())
            except Exception as exc:
                errors.append(repr(exc))
                self.send_error(500)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (config / "opencode.json").write_text(json.dumps({"permission": {"*": "allow"}, "provider": {"openrouter": {"options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"}, "models": {"test-canary": {"name": "test-canary"}}}}}))
    # Allowlist child environment so ambient provider credentials/config cannot leak.
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    env.update({"HOME": str(root), "XDG_CONFIG_HOME": str(root), "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"), "OPENCODE_CONFIG_DIR": str(config), "OPENCODE_CONFIG": str(config / "opencode.json"), "OPENROUTER_API_KEY": "canary-only", "OPENCODE_AUTO_SHARE": "0", "npm_config_offline": "true", "NPM_CONFIG_OFFLINE": "true", "npm_config_registry": "http://127.0.0.1:9"})
    for name in ("DEFAULT_PLUGINS", "PROJECT_CONFIG", "MODELS_FETCH", "AUTOUPDATE", "CLAUDE_CODE", "CLAUDE_CODE_PROMPT", "EXTERNAL_SKILLS", "LSP_DOWNLOAD"):
        env["OPENCODE_DISABLE_" + name] = "1"
    command = [str(BINARY), "run", "--model", "openrouter/test-canary", "--format", "json", "--print-logs", "--dangerously-skip-permissions", "Use bash tool now."]
    try:
        result = subprocess.run(command, cwd=work, env=env, text=True, capture_output=True, timeout=40)
        (root / "stdout.log").write_text(result.stdout)
        (root / "stderr.log").write_text(result.stderr)
        assert result.returncode == 0, result.stderr
        assert not errors, errors
        assert not sentinel.exists(), "native host mutation occurred"
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        tool_parts = {e["part"]["callID"]: e["part"] for e in events if e.get("type") == "tool_use"}
        assert tool_parts["call_canary_001"]["state"]["status"] == "completed", result.stderr
        assert tool_parts["call_canary_001"]["state"]["output"] == f"MOCK_WORKER_RESULT:touch {sentinel}"
        unknown = tool_parts["call_unknown_001"]
        assert unknown["tool"] == "invalid" and "unavailable tool" in json.dumps(unknown).lower(), unknown
        disabled = tool_parts["call_disabled_001"]
        assert disabled["tool"] == "write" and disabled["state"]["status"] == "error", disabled
        assert "unsupported: write" in json.dumps(disabled), disabled
        assert any(e.get("type") == "text" and e["part"].get("text") == "CANARY_DONE" for e in events), result.stderr
        main_requests = [r["body"] for r in requests if r["body"].get("tools")]
        assert main_requests, result.stderr
        first_tools = {t["function"]["name"]: t["function"] for t in main_requests[0]["tools"]}
        assert set(first_tools) == EXPECTED_TOOLS, set(first_tools)
        assert first_tools["bash"]["description"] == "broker bash"
        assert first_tools["bash"]["parameters"]["properties"]["command"]["type"] == "string"
        assert first_tools["bash"]["parameters"]["required"] == ["command"]
        for name in EXPECTED_TOOLS - {"bash"}:
            assert first_tools[name]["description"].startswith(f"disabled {name} canary route"), name
        assert any(not r["body"].get("tools") for r in requests), "auxiliary request must have no executable tools"
        assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_canary_001" and m.get("content") == f"MOCK_WORKER_RESULT:touch {sentinel}" for r in main_requests for m in r["messages"]), "same-session result/call identity lost"
        (root / "receipt.json").write_text(json.dumps({"passed": True, "runtime_sha256": BINARY_DIGEST, "runtime_source_commit": "8ba2a9171597262df9d19516c82a5e14f18f5c63", "source_binary_association": "derived native build identified by runtime/artifact_upload_host/native_patch/build-manifest.json; original source independently verified; published-binary byte reproducibility is unproven", "source_evidence": source_evidence, "provider_calls": len(requests), "host_sentinel_absent": True, "cases": ["overridden-bash", "unknown-tool", "disabled-write", "same-session-result", "tool-free-auxiliary"], "inventory": sorted(first_tools)}, indent=2))
    except Exception:
        print(f"Canary failed; retained evidence: {root}")
        raise
    finally:
        (root / "requests.json").write_text(json.dumps(requests, indent=2))
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(f"Pinned runtime canary passed; evidence: {root}")


if __name__ == "__main__":
    main()
