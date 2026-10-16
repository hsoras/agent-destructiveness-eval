#!/usr/bin/env python3
"""Offline replay of saved proposer replies through the current proposal validator (no network, no paid calls).

    python3 scripts/replay_proposals.py <run-dir> [--keep DIR] [--json]

<run-dir> is a profile-proxy run directory (the one holding candidate_rules.jsonl, profile.final.json and
capture/canonical-private/{diagnostics,full_outputs}.jsonl). For every saved proposer reply the script rebuilds
the call and its result from the capture, starts a fresh ProfilePresentation from the run's start profile, feeds
the saved reply through the real _develop path (parse, normalize, validate, per-change drops, guard pruning,
install) and prints one outcome per reply: installed / normalized / dropped / rejected plus the reason.

Replay caveats: the original packet was not saved, so it is rebuilt from the call/result and the start profile; a
reply whose echoed packet_id/hashes differ from the rebuilt packet has them rebased (reported as `rebased`).
Guest operations are validated but not executed (a stub transaction runner stands in for the guest).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.artifact_upload import profile_proxy  # noqa: E402
from eval.artifact_upload.discovery import Reply  # noqa: E402
from eval.artifact_upload.live import LiveBudget  # noqa: E402
from eval.artifact_upload.profile_proxy import FORMAT_ISSUES_FILE, ProfilePresentation  # noqa: E402
from eval.artifact_upload.profiles import ProfileBundle  # noqa: E402


class _Replay:
    """AuxiliaryClient stand-in that returns one saved reply, rebasing the packet echoes when they differ."""
    upstream, model, system_prompt = "replay://offline", "replay", None

    def __init__(self, raw):
        self.raw, self.rebased = raw, False

    def complete(self, payload, **_limits):
        raw = self.raw
        for key in ("packet_id", "phase", "profile_sha256", "ruleset_sha256", "observation_sha256"):
            # textual rebase so truncated/invalid text is still replayed as-is apart from the echoed values
            pattern = re.compile(r'("%s"\s*:\s*)("[^"]*"|null)' % key)
            found = pattern.search(raw)
            if found and json.loads(found.group(2)) != payload[key]:
                raw = pattern.sub(lambda m: m.group(1) + json.dumps(payload[key]), raw, count=1)
                self.rebased = True
        return Reply(raw, 0, 0, 0.0)


def _jsonl(path):
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def load_replies(run):
    """-> list of dicts(transaction_id, packet_id, call, result, raw) for every saved proposer reply."""
    cap = run / "capture" / "canonical-private"
    candidates = {c["transaction_id"]: c for c in _jsonl(run / "candidate_rules.jsonl")}
    texts = {}
    for event in _jsonl(cap / "full_outputs.jsonl"):
        p = event["payload"]
        if "stream" not in p or "call_id" not in p:
            continue
        texts.setdefault(p["call_id"], {})[p["stream"]] = p.get("text", "")
    replies = []
    for event in _jsonl(cap / "diagnostics.jsonl"):
        p = event["payload"]
        if p.get("kind") != "development_auxiliary_call" or p.get("role") != "proposer":
            continue
        cand = candidates.get(p.get("transaction_id"))
        raw = (p.get("reply") or {}).get("output")
        replies.append({"transaction_id": p.get("transaction_id"), "candidate": cand, "raw": raw,
                        "status": p.get("status"), "failure": p.get("failure")})
    for r in replies:
        cand = r["candidate"] or {}
        op, target = cand.get("operation"), cand.get("command")
        args = ({"command": target} if op == "shell" else {"filePath": target} if op == "read" else {"path": target})
        r["call"] = {"operation": op, "tool_name": op, "arguments": args, "call_id": cand.get("call_id"),
                     "session_id": cand.get("session_id"), "run_id": cand.get("run_id")}
        streams = texts.get(cand.get("call_id"), {})
        r["result"] = {"status": "ok", "exit_code": 0, "error_code": None, "encoding": "utf-8", "output_uri": None,
                       "stdout": streams.get("stdout", ""), "stderr": streams.get("stderr", "")}
        r["packet_no"] = int((_packet_of(run, r["transaction_id"]) or "packet-0001").split("-")[1])
    return replies


def _packet_of(run, tid):
    for c in _jsonl(run / "candidate_profile.jsonl"):
        if c.get("transaction_id") == tid:
            return c["packet_id"]
    return None


def replay_one(item, start_profile, workdir):
    out = Path(workdir)
    client = _Replay(item["raw"])
    proxy = ProfilePresentation(out, ProfileBundle.from_dict(json.loads(json.dumps(start_profile))), proposer=client,
                                budget=LiveBudget(cost_usd=1.0, max_call_cost_usd=0.5, pairs=2, output_tokens=16384),
                                check_identity=False)
    proxy._packets = item["packet_no"] - 1
    proxy.refresh_state_probe = lambda: {"facts": {}, "remove": [], "omitted": []}
    proxy.guest = SimpleNamespace()
    proxy._develop("after_execution", item["call"], item["result"])
    rec = _jsonl(out / "candidate_profile.jsonl")[-1]
    issues = _jsonl(out / FORMAT_ISSUES_FILE)
    return rec, issues, client.rebased


def verdict(rec, issues):
    outcome = rec["outcome"]
    prop = rec.get("proposal_after_drops") or rec.get("proposal") or {}
    n_edits = sum(len(prop.get(k, [])) for k in ("rule_changes", "profile_changes", "guest_operations")) if outcome == "installed" else 0
    if outcome == "installed":
        kinds = {i["issue_kind"] for i in issues}
        if any(i["action"] == "dropped" for i in issues):
            label = "installed+dropped"
        elif kinds:
            label = "installed+normalized"
        else:
            label = "installed"
        return label, n_edits, len(prop.get("findings", []))
    return ("rejected" if outcome.startswith("rejected") else outcome.split(":")[0]), 0, 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true", help="print machine-readable results")
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    if (run / "candidate_rules.jsonl").exists() is False:  # accept the experiment root too
        found = sorted(run.glob("**/candidate_rules.jsonl"))
        if not found:
            sys.exit("no candidate_rules.jsonl under " + str(run))
        run = found[0].parent
    start = json.loads((run / "profile.final.json").read_text())
    results, totals, kinds = [], Counter(), Counter()
    profile_proxy.run_transaction = lambda guest, requests, log, **kw: (kw["commit"](), SimpleNamespace(
        ok=True, to_dict=lambda: {"status": "replayed-not-executed", "operations": len(requests)}))[1]
    for item in load_replies(run):
        if not item["raw"]:
            row = {"packet": item["candidate"] and item["candidate"].get("transaction_id", "")[:6], "outcome": "no-reply",
                   "label": "no-reply", "reason": "no reply text saved (%s)" % str(item["failure"] and item["failure"].get("error"))[:80],
                   "edits": 0, "findings": 0, "issues": []}
            results.append(row)
            totals["no-reply"] += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            rec, issues, rebased = replay_one(item, start, tmp)
        label, edits, findings = verdict(rec, issues)
        old = (item["candidate"] or {}).get("outcome", "?")
        row = {"packet": rec["packet_id"], "was": old[:70], "outcome": rec["outcome"][:140], "label": label,
               "edits": edits, "findings": findings, "rebased": rebased,
               "issues": [{k: i[k] for k in ("issue_kind", "field", "action")} for i in issues]}
        results.append(row)
        totals[label] += 1
        totals["installs_with_edit"] += bool(edits)
        totals["installs_findings_only"] += bool(label.startswith("installed") and not edits)
        for i in issues:
            kinds[(i["issue_kind"], i["action"])] += 1
    if args.json:
        print(json.dumps({"results": results, "totals": totals, "issue_kinds": {"%s/%s" % k: v for k, v in kinds.items()}}, indent=1))
        return 0
    print("replay of %s (%d saved proposer calls, offline)" % (run, len(results)))
    for r in results:
        print("%-11s %-20s edits=%d findings=%d%s | was: %s" % (r["packet"], r["label"], r["edits"], r["findings"],
                                                             " rebased-echo" if r.get("rebased") else "", r.get("was", "")))
        if r["label"] in ("rejected", "no-reply") or r["outcome"] not in ("installed",):
            print("    reason: %s" % (r.get("reason") or r["outcome"]))
        if r["issues"]:
            print("    issues: " + ", ".join("%s/%s%s" % (i["issue_kind"], i["action"], (":" + i["field"]) if i["field"] else "")
                                             for i in r["issues"][:8]))
    replayed = sum(v for k, v in totals.items() if k not in ("installs_with_edit", "installs_findings_only", "no-reply"))
    print("\nsummary: %d replayed, %d without reply text" % (replayed, totals["no-reply"]))
    print("  installed with >=1 edit: %d ; installed findings-only: %d" % (totals["installs_with_edit"], totals["installs_findings_only"]))
    print("  outcomes: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(totals.items())
                                     if k not in ("installs_with_edit", "installs_findings_only")))
    print("  issue kinds: " + (", ".join("%s/%s=%d" % (k[0], k[1], v) for k, v in sorted(kinds.items())) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
