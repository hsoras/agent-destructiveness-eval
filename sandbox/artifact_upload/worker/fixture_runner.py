"""Run fixture cases through GuestWorker and compare with native receipts.

Usable on the host (tests) and inside the guest container:
``python -m sandbox.artifact_upload.worker.fixture_runner cases.json out.json ROOT``.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from eval.artifact_upload.contracts import argument_digest

from . import native_fixture as nf
from .core import GuestWorker


def make_call(case: dict[str, Any], args: dict[str, Any], n: int) -> dict[str, Any]:
    return {"contract_version": "artifact-upload.v1", "run_id": "fixture-run", "session_id": "fixture-session", "call_id": f"case-{n}-{case['id']}",
            "tool_name": case["tool"], "operation": {"bash": "shell", "read": "read", "write": "write", "edit": "edit", "apply_patch": "patch", "glob": "find", "grep": "grep"}[case["tool"]],
            "arguments": args, "argument_digest": argument_digest(args), "guest_profile": "eval-looking"}


def run_worker(cases: list[dict[str, Any]], scratch: str) -> dict[str, Any]:
    root = os.path.realpath(os.path.join(scratch, "proj")); out = os.path.realpath(os.path.join(scratch, "outside"))
    os.makedirs(root, exist_ok=True); os.makedirs(out, exist_ok=True)
    worker = GuestWorker(root, output_dir=os.path.join(scratch, "worker-output"), kill_grace=1.0)
    results = []
    for n, case in enumerate(cases):
        nf.clear(root); nf.clear(out); nf.apply_setup(root, out, case["setup"])
        args = json.loads(json.dumps(case["args"]).replace("{ROOT}", root).replace("{OUT}", out))
        res = worker.execute(make_call(case, args, n))
        results.append({"id": case["id"], "result": res, "snapshot": nf.snapshot(root)})
    return {"root": root, "out": out, "results": results, "worker_digest": worker.digest}


def result_text(res: dict[str, Any]) -> str:
    if res["encoding"] == "base64": return "<base64>"
    if res["status"] in ("ok",) and res["error_code"] is None and not res["stderr"]: return res["stdout"]
    if res["error_code"] in ("command_failed", "timeout", "cancelled"): return res["stdout"] + res["stderr"]
    return res["stderr"] if res["status"] != "ok" else res["stdout"] + res["stderr"]


def norm_text(text: str, root: str, out: str) -> str:
    text = text.replace(root, "{ROOT}").replace(out, "{OUT}").replace(root.lstrip("/"), "{ROOT}")
    text = re.sub(r"Full output saved to: \S+", "Full output saved to: <FILE>", text)
    text = re.sub(r"(?<=\n)\(Output capped", "(Output capped", text)
    return "" if text == "(no output)" else text.replace("(no output)\n\n<shell_metadata>", "\n\n<shell_metadata>")


def norm_snapshot(snap: dict[str, Any], root: str, out: str) -> dict[str, Any]:
    fixed = {}
    for k, v in snap.items():
        v = dict(v)
        if "target" in v: v["target"] = v["target"].replace(root, "{ROOT}").replace(out, "{OUT}")
        fixed[k] = v
    return fixed


def compare(case: dict[str, Any], native: dict[str, Any], worker: dict[str, Any], native_paths: tuple[str, str], worker_paths: tuple[str, str]) -> list[str]:
    """Return mismatches between a native receipt and a worker record for one case."""
    nt = norm_text(native["output"], *native_paths); wt = norm_text(result_text(worker["result"]), *worker_paths)
    ns = norm_snapshot(native["snapshot"], *native_paths); ws = norm_snapshot(worker["snapshot"], *worker_paths)
    mode = case["compare"]
    if mode == "bounded-files":
        lines = [line for line in wt.splitlines() if line]
        trailer = "(Results are truncated: showing first 100 results. Consider using a more specific path or pattern.)"
        if len(lines) != 101 or lines[-1] != trailer: return ["glob payload has an unexpected row count or truncation trailer"]
        paths = lines[:-1]
        expected = {op["path"]: 0 for op in case["setup"] if op["op"] == "write" and op["path"].endswith(".txt")}
        for op in case["setup"]:
            if op["op"] == "utime" and op["path"] in expected: expected[op["path"]] = op["mtime"]
        actual = [p.replace("{ROOT}/", "") for p in paths]
        if len(actual) != 100 or len(set(actual)) != 100:
            return [f"expected 100 unique glob paths, got {len(actual)} rows/{len(set(actual))} unique"]
        keyed = {k.replace("{ROOT}/", ""): v for k, v in expected.items()}
        if any(p not in keyed for p in actual): return ["glob payload contains a path outside the fixture corpus"]
        mtimes = [keyed[p] for p in actual]
        return [] if mtimes == sorted(mtimes, reverse=True) else ["glob payload is not ordered by descending fixture mtime"]
    if mode == "count":
        a, b = nt.split("\n"), wt.split("\n")
        return [] if len(a) == len(b) and a[-1] == b[-1] else [f"count/trailer mismatch native={len(a)} {a[-1]!r} worker={len(b)} {b[-1]!r}"]
    problems = []
    if nt != wt: problems.append(f"output mismatch\n native: {nt!r}\n worker: {wt!r}")
    if mode != "text" and ns != ws: problems.append(f"post-state mismatch\n native: {ns!r}\n worker: {ws!r}")
    return problems


def main(argv: list[str]) -> int:
    cases = json.load(sys.stdin if argv[1] == "-" else open(argv[1])); scratch = argv[3]
    data = run_worker(cases, scratch)
    json.dump(data, sys.stdout if argv[2] == "-" else open(argv[2], "w"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
