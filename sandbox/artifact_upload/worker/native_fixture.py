"""Scratch-state helpers and the pinned-native differential runner.

``run_native`` drives the exact pinned OpenCode executable with scripted
responses from a loopback mock provider (no network, no paid provider).  The
mock sits between model turns, so it can reset and snapshot the scratch
project between cases.  ``run_worker`` executes the identical cases through
GuestWorker.  Both return the same record shape for comparison.
"""
from __future__ import annotations

import base64, hashlib, json, os, shutil, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def apply_setup(root, out, ops):
    for op in ops:
        p = op["path"].replace("{ROOT}", root).replace("{OUT}", out)
        k = op["op"]
        if k == "mkdir": os.makedirs(p, exist_ok=True)
        elif k == "write":
            os.makedirs(os.path.dirname(p), exist_ok=True)
            data = base64.b64decode(op["b64"]) if "b64" in op else op["text"].encode()
            with open(p, "wb") as f: f.write(data)
        elif k == "symlink":
            os.makedirs(os.path.dirname(p), exist_ok=True)
            os.symlink(op["target"].replace("{ROOT}", root).replace("{OUT}", out), p)
        elif k == "git_init":
            subprocess.run(["git", "init", "-q", p], check=True)
            if op.get("commit"): subprocess.run(["git", "-C", p, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        elif k == "chmod": os.chmod(p, op["mode"])
        elif k == "utime": os.utime(p, (op["mtime"], op["mtime"]))
        else: raise ValueError(k)

def clear(d):
    if not os.path.isdir(d): os.makedirs(d); return
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if os.path.islink(p) or os.path.isfile(p):
            os.chmod(d, 0o755); os.unlink(p)
        else:
            for r, ds, fs in os.walk(p):
                os.chmod(r, 0o755)
            shutil.rmtree(p)

def snapshot(root):
    snap = {}
    for r, ds, fs in os.walk(root):
        for n in ds + fs:
            p = os.path.join(r, n); rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if os.path.islink(p): snap[rel] = {"type": "symlink", "target": os.readlink(p)}
            elif os.path.isdir(p): snap[rel] = {"type": "dir", "mode": st.st_mode & 0o777}
            else:
                data = open(p, "rb").read()
                snap[rel] = {"type": "file", "mode": st.st_mode & 0o777, "sha256": hashlib.sha256(data).hexdigest(), "b64": base64.b64encode(data).decode()}
    return snap

def run_native(binary, cases, variant, scratch, port, timeout=900):
    root = os.path.join(scratch, "proj"); out = os.path.join(scratch, "outside")
    os.makedirs(root, exist_ok=True); os.makedirs(out, exist_ok=True)
    root = os.path.realpath(root); out = os.path.realpath(out)
    state = {"k": 0, "results": [], "inventory": None, "lock": threading.Lock(), "requests": []}
    def prep(case):
        clear(root); clear(out); apply_setup(root, out, case["setup"])
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            with state["lock"]:
                state["requests"].append(body)
                self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
                def ev(d): self.wfile.write(("data: " + json.dumps(d) + "\n\n").encode()); self.wfile.flush()
                base = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": body.get("model")}
                usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                if "tools" not in body:
                    ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "t"}, "finish_reason": None}]})
                    ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage})
                else:
                    if state["inventory"] is None: state["inventory"] = [t["function"]["name"] for t in body["tools"]]
                    k = state["k"]
                    if k > 0:
                        last = body["messages"][-1]
                        c = cases[k - 1]
                        content = last["content"]
                        if isinstance(content, list): content = "".join(x.get("text", "") for x in content)
                        state["results"].append({"id": c["id"], "role": last["role"], "output": content, "snapshot": snapshot(root)})
                    if k < len(cases):
                        c = cases[k]; prep(c); state["k"] += 1
                        args = json.loads(json.dumps(c["args"]).replace("{ROOT}", root).replace("{OUT}", out))
                        ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_%d" % k, "type": "function", "function": {"name": c["tool"], "arguments": json.dumps(args)}}]}, "finish_reason": None}]})
                        ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": usage})
                    else:
                        state["k"] += 1
                        ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]})
                        ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    model = "gpt-mock" if variant == "patch" else "plain-mock"
    cfg = {"permission": {"*": "allow"}, "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "mock", "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "x"}, "models": {model: {"name": model, "tool_call": True, "limit": {"context": 1000000, "output": 10000}}}}}}
    env = {"PATH": os.environ["PATH"], "HOME": os.path.join(scratch, "home"), "XDG_DATA_HOME": scratch + "/xdg/data", "XDG_CONFIG_HOME": scratch + "/xdg/config", "XDG_CACHE_HOME": scratch + "/xdg/cache", "XDG_STATE_HOME": scratch + "/xdg/state", "OPENCODE_CONFIG_CONTENT": json.dumps(cfg), "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1", "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "1", "OPENCODE_DISABLE_AUTOCOMPACT": "1"}
    os.makedirs(env["HOME"], exist_ok=True)
    clear(root)
    try:
        p = subprocess.run([binary, "run", "--model", f"mock/{model}", "--format", "json", "go"], cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        p = subprocess.CompletedProcess(e.cmd, -9, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "TIMEOUT after case %d" % state["k"])
    srv.shutdown()
    return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "inventory": state["inventory"], "results": state["results"], "root": root, "out": out, "requests": state["requests"]}
