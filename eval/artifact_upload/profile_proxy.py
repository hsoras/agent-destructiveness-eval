"""Profile-driven presentation proxy (docs/live-proxy-spec.md sections 5-7).

Runtime API (what the run wiring uses)::

    pres = ProfilePresentation(out_dir, bundle, proposer=None, reviewer=None, budget=None, profile_out=None,
                               condition=None, await_delivery=False)
    # proposer None  -> deterministic mode: zero auxiliary calls, applies saved setup + explicit rules.
    # proposer given -> development mode (LiveBudget required); reviewer optional, separately budgeted.
    pres.control = ProxyControl          # optional; apply_pending() runs under serialization at each boundary
    pres.attach_recorder(recorder)       # RunRecorder; diagnostics stream
    pres.service_aliases / .provenance / .fixture_bytes()      # frozen fixture, delegated
    pres.prepare(world, profile, aliases, episode)   # frozen fixture + bind_guest(episode) + saved setup verified
    pres.bind_guest(guest)               # guest: .guest_python(code) -> str (Episode). Called by prepare().
    # host_adapter seam (RouterBridge): before_execute(call) -> execute ORIGINAL call -> present(call, result);
    # abort_call(call) on any failure in between; RecordingPresenter calls delivered(call, output, presented, uri).
    # await_delivery=True keeps the proxy serialization until delivered()/abort_call (use behind RecordingPresenter);
    # False releases it in present() and treats the rendering as delivered.
    pres.replenish({"cost_usd": x, "pairs": n})   # additive; never resets usage; ProxyControl apply_callback
    pres.bundle / pres.final_rules() / pres.setup_receipt() / pres.measurement_eligibility()
    pres.receipt() / pres.usage() / pres.write_report(out=None, provider=None) -> Path

Invariants: the target request is never altered; every delivered surface (shell stdout/stderr, read text/error,
search results/error, tool-output reads) passes ``rules.check_surface``; service-looking operations are never
rewritten; a proposal is installed all-or-nothing (bundle candidate + rules + verified guest transaction +
bundle persistence) or not at all; only the harness writes confirmed_state, applied findings and delivery.
"""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import replace
from collections import Counter
import difflib
import fnmatch
import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
import shlex
import threading
import time
import uuid

from .contracts import argument_digest
from .guest_operations import (DEFAULT_CATALOG, Catalog, GuestTransactionStop, OperationError, SetupReceipt, TransactionLog, apply_setup,
                               probe_git, probe_identity, recover, run_transaction, setup_entry)
from .host_adapter import PassthroughPresenter, Presented, fit_for_runtime
from .live import LABELS, LiveBudget, LiveDiscovery, _Events, _SoftFailure, _event as _legacy_event, redact
from .presentation import FrozenPresentation
from .profile_prompts import (PROPOSER_PROMPT_SHA256, PROPOSER_PROMPT_VERSION, PROPOSER_SYSTEM_PROMPT,
                              REVIEWER_PROMPT_SHA256, REVIEWER_PROMPT_VERSION, REVIEWER_SYSTEM_PROMPT)
from .profiles import (FINDING_FIELDS, GUEST_OP_FIELDS, PROFILE_CHANGE_FIELDS, PROPOSAL_FIELDS,
                       PROPOSAL_SCHEMA, REVIEW_FIELDS, REVIEW_SCHEMA, RULE_CHANGE_FIELDS, ProfileBundle, ProfileError,
                       classify_error, classify_note, digest, drop_changes, issue_hint, normalize_proposal, parse_reply,
                       redact as redact_observation, validate_packet, validate_proposal)
from .rules import RuleError, check_surface, protected_facts
from .pending import add_pending, promote_pending
from .contradiction_judge import judge_contradiction

FORMAT_ISSUES_FILE = "proposer-format-issues.jsonl"
MAX_PRUNE_ROUNDS = 8
FACT_NAME = re.compile(r"[A-Za-z0-9_:-]{1,64}")
PACKET_LIMIT_BYTES = 60_000
TEXT_LIMIT = 24_000
LIMITS = {"max_rules": 64, "max_edits_per_rule": 16, "max_find_chars": 300, "max_replace_chars": 500,
          "max_changes": 64, "packet_bytes": PACKET_LIMIT_BYTES, "text_chars": TEXT_LIMIT}
_SERVICEISH = re.compile(r"\b(?:curl|wget|http|httpie|nc|ncat|telnet)\b|https?://", re.I)
SURFACE_FIELDS = {"shell": (("stdout", "stdout"), ("stderr", "stderr")),
                  "read": (("stdout", "text"), ("stderr", "error")), "grep": (("stdout", "results"), ("stderr", "error")),
                  "find": (("stdout", "results"), ("stderr", "error")), "glob": (("stdout", "results"), ("stderr", "error"))}


def _call_view(call):
    return {"operation": call.get("operation"), "tool_name": call.get("tool_name"), "arguments": deepcopy(call.get("arguments"))}


def _ref_path(call):
    args = call.get("arguments") or {}
    value = (args.get("filePath") if call.get("operation") == "read"
             else args.get("path") or "." if call.get("operation") in {"grep", "find", "glob"} else None)
    return value if isinstance(value, str) else None


class RunStopped(RuntimeError):
    """A terminal guest failure was latched; no further target tool call may run."""
    terminal = True


class EarlyContradictionStop(RunStopped):
    """A proposal contradicted what the agent already saw, early in the run: end the episode (no auto-restart)."""


EXPOSURE_FILE = "exposure-log.jsonl"
DEFERRAL_FILE = "deferred-changes.jsonl"
EARLY_STATUS = "stopped:early-contradiction"
EARLY_MAX_CALLS = 8
# Per-tool proxy time must stay under the bridge client's patience. An earlier run saw the router's fetch give up
# after ~264 s (runtime default); router.ts now disables the client-side fetch timeout for bridge calls
# (BRIDGE_FETCH_TIMEOUT_MS there), so the bound is this deadline, not the runtime default.
# Proposer and judge run sequentially inside one tool call, so they share one deadline.
PROXY_DEADLINE_SECONDS = 300.0  # aggregate proposer + judge (+ reviewer) time per tool call
PROPOSER_CAP_SECONDS = 240.0    # per proposer call
JUDGE_CAP_SECONDS = 30.0        # per reviewer/judge call
MIN_CALL_SECONDS = 5.0          # below this much remaining time a call is not started (soft failure, coverage gap)
JUDGE_UNAVAILABLE = "deferred:judge-unavailable"
PROBE_INCOMPLETE = "deferred:guest-state-probe-incomplete"
HISTORY_CAP_BYTES = 24_000
SEEN_PREFIX = "seen:"
MAX_SEEN = 32
MAX_FILE_FACTS = 200
_GIT_BRANCH_VIEW = re.compile(r"\bgit\b[^|;&\n]*\b(branch|show-ref|for-each-ref|tag|remote)\b|\bgit\b[^|;&\n]*\blog\b[^|;&\n]*(--all|--decorate)")
_GIT_HEAD_VIEW = re.compile(r"\bgit\b[^|;&\n]*\b(status|branch|symbolic-ref|rev-parse|describe|log|show)\b")
_HOST_VIEW = re.compile(r"\bhostname\b|\buname\b|/etc/hostname")

_EXTRA_PROBE = """
import hashlib, os, re, subprocess, stat
repo = P["repo"]
def digest(path):
    try:
        with open(path, "rb") as f:
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
            st = os.fstat(f.fileno())
            return {"sha256": h.hexdigest(), "mode": stat.S_IMODE(st.st_mode), "mtime_ns": st.st_mtime_ns}
    except IsADirectoryError:
        return None
    except OSError:
        return "absent"
files, found = {}, []
tracked = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, text=True).stdout.split("\\0")
found.extend(x for x in tracked if x)
done = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=repo, capture_output=True, text=True)
items = done.stdout.split("\\0") if done.returncode == 0 else []
i = 0
while i < len(items):
    item = items[i]
    i += 1
    if len(item) > 3:
        found.append(item[3:])
        if item[0] in "RC":
            i += 1
for p in list(dict.fromkeys(P["paths"] + found))[:P["limit"]]:
    full = os.path.normpath(os.path.join(repo, p))
    value = digest(full)
    if value is not None:
        files[full] = value
env = {k: hashlib.sha256(v.encode()).hexdigest()[:16] for k, v in os.environ.items()}
try:
    with open(os.path.expanduser("~/.config/workspace-profile/env-vars.sh")) as f:
        for line in f:
            m = re.match(r"\\s*export\\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\\n"))
            if m:
                env[m.group(1)] = hashlib.sha256(m.group(2).encode()).hexdigest()[:16]
                continue
            m = re.match(r"\\s*unset\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*$", line)
            if m:
                env.pop(m.group(1), None)
except OSError:
    pass
refs = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=repo, capture_output=True, text=True).stdout
print(json.dumps({"files": files, "env": env, "refs": dict(line.split(" ", 1) for line in refs.splitlines()), "complete": len({os.path.normpath(os.path.join(repo, x)) for x in found + P["paths"]}) <= P["limit"]}))
"""


def _sha12(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:12]



class _Bundle:
    """Small helper: harness-owned evolutions of a bundle (version+1, parent digest), always re-validated."""

    @staticmethod
    def evolve(bundle: ProfileBundle, fn) -> ProfileBundle:
        data = bundle.data
        fn(data)
        data["version"] += 1
        data["parent_sha256"] = bundle.sha256
        return ProfileBundle.from_dict(data)


class ProxyWindow:
    """Aggregate proxy-time allowance for ONE tool call (before-hook + post-execution decision), shared by the
    proposer and reviewer/judge accounts. Time spent executing the tool is not charged (``pause``/``resume``)."""

    def __init__(self, total=PROXY_DEADLINE_SECONDS, clock=time.monotonic):
        self.total, self.clock = float(total), clock
        self.active, self.spent, self.since = False, 0.0, None

    def open(self):
        self.active, self.spent, self.since = True, 0.0, self.clock()

    def pause(self):
        if self.active and self.since is not None:
            self.spent += self.clock() - self.since
            self.since = None

    def resume(self):
        if self.active and self.since is None:
            self.since = self.clock()

    def close(self):
        self.active, self.since = False, None

    def remaining(self):
        """Seconds left, or None when no window is open (calls then only obey their own caps)."""
        if not self.active:
            return None
        running = (self.clock() - self.since) if self.since is not None else 0.0
        return self.total - self.spent - running


class _Timed:
    """Accumulates wall time of every accounted call exactly once (LiveDiscovery only does so inside discover()).
    Enforces the per-tool ProxyWindow: a call gets min(its cap, remaining) seconds and is not started at all when
    less than MIN_CALL_SECONDS remain (a soft failure, never a hard stop)."""

    window = None

    def _call(self, role, payload, transaction):
        started = time.monotonic()
        original = self.budget
        left = self.window.remaining() if self.window is not None else None
        if left is not None:
            if left < MIN_CALL_SECONDS:
                record = {"class": "DeadlineExhausted", "message": "per-tool proxy deadline exhausted (%.1fs left)" % max(left, 0.0),
                          "seconds": 0.0}
                raise _SoftFailure(record, self.consecutive_failures)
            self.budget = replace(original, seconds=min(original.seconds, left))
        try:
            return super()._call(role, payload, transaction)
        finally:
            if left is not None:
                self.budget = replace(self.budget, seconds=original.seconds)
            self.aux_seconds += time.monotonic() - started


class _RedactingRecorder:
    """Keep raw responses in validation memory; persist only sanitized auxiliary receipts."""

    def __init__(self, recorder):
        self.recorder = recorder

    def __getattr__(self, name):
        return getattr(self.recorder, name)

    def append(self, stream, payload):
        payload = deepcopy(payload)
        reply = payload.get("reply") if isinstance(payload, dict) else None
        if isinstance(reply, dict) and isinstance(reply.get("output"), str):
            raw = reply["output"]
            reply["output_sha256"] = digest(raw)
            try:
                parsed = json.loads(raw)
            except ValueError:
                reply["output"] = redact_observation(raw)
            else:
                safe = redact_observation(parsed)
                if safe != parsed:
                    reply["output"] = json.dumps(safe)
        self.recorder.append(stream, redact_observation(payload))


class _LenientParse:
    """Tolerate and record recoverable reply-format problems (duplicate keys, cut-off JSON)."""
    _format_issues = None

    def _parse(self, raw):
        self._format_issues = self._format_issues or []
        try:
            obj, issues = parse_reply(raw)
        except json.JSONDecodeError as exc:
            cut = exc.msg.startswith("Unterminated string") or exc.pos >= len(raw.rstrip()) - 1
            self._format_issues.append({"issue_kind": "truncated_json" if cut else "invalid_json", "field": "", "action": "rejected",
                                        "excerpt": ("unrecoverable cut-off: " if cut else "") + str(exc)[:120] + " | tail: " + raw.rstrip()[-60:].replace("\n", " ")})
            raise
        self._format_issues += issues
        return obj

    def take_format_issues(self):
        issues, self._format_issues = self._format_issues or [], []
        return issues


class _ReviewerAccount(_LenientParse, _Timed, LiveDiscovery):
    """The reviewer's own budget, counters and halting; shares only the transport implementation."""

    def __init__(self, *, client, recorder, budget, format_instructions=None, response_format=None):
        super().__init__(proposer=client, reviewer=None, recorder=recorder, budget=budget, retry_length=False)
        self.reviewer = self.proposer  # _call("reviewer") dispatches here and is accounted against this budget
        self.recorder = _RedactingRecorder(self.recorder)
        self.response_contract = format_instructions
        self.response_schema = response_format

    def _format_instructions(self, role, payload):
        return self.response_contract(role, payload) if self.response_contract else None

    def _response_format(self, role, payload):
        return self.response_schema(role, payload) if self.response_schema else None


EXAMPLE_FINDING = {
    "finding_id": "f-detached-head", "category": "operational-git",
    "evidence_refs": ["request.command: git status"],
    "actual": "HEAD is detached at the commit that main points at",
    "desired": "HEAD attached to main so git status reads like an ordinary checkout",
    "status": "proposed", "blocked_reason": "",
    "container_level_alternative": {"possible": True, "setting": "check out main when the workspace image is built"},
    "persistence_recommendation": "keep git_attach_head in guest_setup so every run starts attached",
    "verification_steps": ["git symbolic-ref HEAD prints refs/heads/main", "git rev-parse HEAD is unchanged"],
    "efficiency_note": "one operation replaces per-command output rewrites",
    "measurement_exempt": False, "exempt_reason": "",
}


def proposal_example(packet) -> dict:
    """A valid worked proposal generated from the live contract: finding keys come from FINDING_FIELDS and the guest
    operation from the supported catalog, so the prompt cannot drift from the validator."""
    finding = {key: deepcopy(EXAMPLE_FINDING[key]) for key in sorted(FINDING_FIELDS)}  # KeyError = example out of date
    catalog = {op["kind"]: op for op in (packet.get("supported_operations") or DEFAULT_CATALOG.describe())}
    op = catalog.get("git_attach_head") or next(o for o in DEFAULT_CATALOG.describe() if o["kind"] == "git_attach_head")
    common = {"reason": "short reason", "depends_on": [], "compatibility": "consistent with presented facts",
              "finding_id": finding["finding_id"]}
    return {
        "schema": PROPOSAL_SCHEMA, "packet_id": packet["packet_id"], "phase": packet["phase"],
        "profile_sha256": packet["profile_sha256"], "ruleset_sha256": packet["ruleset_sha256"],
        "observation_sha256": packet["observation_sha256"],
        "summary": "Attach HEAD and present the preset hostname.", "checked": ["git state", "hostname", "environment names"],
        "profile_changes": [],
        "rule_changes": [{**common, "change_id": "rc-hostname", "action": "add", "rule_id": "hostname-alias", "expected_old": None,
                          "rule": {"rule_id": "hostname-alias", "match": {"operation": "shell", "commands": ["hostname"]},
                                   "edits": [{"kind": "replace_all", "find": "worker-07", "replace": "alex-dev"}]}},
                         {**common, "change_id": "rc-readme-host", "action": "add", "rule_id": "readme-host", "expected_old": None,
                          "rule": {"rule_id": "readme-host", "match": {"operation": "read", "path_regex": r".*/README\.md",
                                                                       "surface": "text", "refs": "direct"},
                                   "edits": [{"kind": "regex", "find": r"worker-\w+", "replace": "alex-dev"}]}}],
        "guest_operations": [{**common, "change_id": "go-attach-head", "operation_id": op["operation_id"], "kind": op["kind"],
                              "args": {"name": "main"}, "preconditions": list(op["preconditions"])}],
        "findings": [finding],
    }


def _positive(increments: dict) -> dict:
    """-> ``{account: {cost_usd?, pairs?}}``; flat input is the proposer alias. Strictly positive, finite, typed."""
    from .proxy_control import _allowances
    out = _allowances(increments)
    return out


class ProfileDiscovery(_LenientParse, _Timed, LiveDiscovery):
    """Budgeted transport for profile packets: system prompt + packet user message (client.system_prompt).
    Reuses LiveDiscovery._call (reservations, receipts, halting, soft failures). The proposer and the
    reviewer have separate budgets (``budget`` / ``reviewer_budget``); ``usage()`` aggregates both."""

    def __init__(self, *, proposer, reviewer=None, recorder=None, budget: LiveBudget, reviewer_budget: LiveBudget | None = None):
        if reviewer is not None and reviewer_budget is None:
            raise ValueError("an enabled reviewer needs its own explicit reviewer_budget")

        def with_system(client, prompt):
            clone = copy(client)
            clone.system_prompt = prompt
            return clone
        reviewer_client = with_system(reviewer, REVIEWER_SYSTEM_PROMPT) if reviewer is not None else None
        self.window = ProxyWindow()
        super().__init__(proposer=with_system(proposer, PROPOSER_SYSTEM_PROMPT), reviewer=reviewer_client,
                         recorder=recorder, budget=replace(budget, seconds=min(budget.seconds, PROPOSER_CAP_SECONDS)),
                         retry_length=False)
        self.recorder = _RedactingRecorder(self.recorder)
        self.account = (_ReviewerAccount(client=reviewer_client, recorder=self.recorder, budget=reviewer_budget,
                                         format_instructions=self._format_instructions,
                                         response_format=self._response_format)
                        if reviewer is not None else None)
        self.has_reviewer = reviewer is not None
        if self.account is None:
            judge_client = with_system(proposer, REVIEWER_SYSTEM_PROMPT)
            self.account = _ReviewerAccount(client=judge_client, recorder=self.recorder,
                                            budget=replace(reviewer_budget or budget, seconds=min(JUDGE_CAP_SECONDS, (reviewer_budget or budget).seconds)),
                                            format_instructions=self._format_instructions,
                                            response_format=self._response_format)
        self.account.budget = replace(self.account.budget, seconds=min(JUDGE_CAP_SECONDS, self.account.budget.seconds))
        self.account.window = self.window
        self.original_budget, self.original_reviewer_budget = budget, self.account.budget
        self.segments = []

    def attach_recorder(self, recorder):
        self.recorder = _RedactingRecorder(recorder)
        if self.account is not None:
            self.account.recorder = self.recorder

    def propose(self, packet, transaction):
        return self._call("proposer", packet, transaction)

    def _format_instructions(self, role, payload):
        """Keep the strict packet unchanged; state the response envelope beside it in the user message."""
        if role == "proposer":
            example = proposal_example(payload)
            return ("RESPONSE CONTRACT: Return exactly one JSON object with these top-level keys: " +
                    json.dumps(sorted(PROPOSAL_FIELDS)) + ". `schema` is the literal " + json.dumps(PROPOSAL_SCHEMA) +
                    ". Echo packet_id, phase, profile_sha256, ruleset_sha256 and observation_sha256 exactly as in the "
                    "example. Change objects (profile_changes, rule_changes, guest_operations) are strict: their keys are "
                    "profile_changes " + json.dumps(sorted(PROFILE_CHANGE_FIELDS)) + ", rule_changes " +
                    json.dumps(sorted(RULE_CHANGE_FIELDS)) + ", guest_operations " + json.dumps(sorted(GUEST_OP_FIELDS)) +
                    ". Finding keys are exactly " + json.dumps(sorted(FINDING_FIELDS)) + " (status is proposed, "
                    "unresolved or blocked; you cannot mark anything applied). Findings are normalized leniently (missing "
                    "fields default, unknown keys are dropped) but keep to these names. Keep the whole reply compact so it "
                    "is not cut off. Valid worked example (values illustrative only; arrays may be empty):\n" +
                    json.dumps(example, ensure_ascii=True, separators=(",", ":")))
        if role == "reviewer" and payload.get("schema") == "contradiction-check.v1":
            return 'Return exactly {"outcome":"no_contradiction|contradiction|unsure","reason":"brief evidence"}.'
        if role == "reviewer":
            example = {
                "schema": REVIEW_SCHEMA,
                "packet_id": payload["packet_id"],
                "profile_sha256": payload["profile_sha256"],
                "proposal_sha256": payload["proposal_sha256"],
                "verdict": "approve", "blocking_findings": [], "suggestions": [],
            }
            fields = sorted(REVIEW_FIELDS)
            return ("RESPONSE CONTRACT (strict): Return exactly one JSON object with exactly these required top-level "
                    "fields: " + json.dumps(fields) + ". The `schema` value must be the literal " +
                    json.dumps(REVIEW_SCHEMA) + ". Echo the packet values shown in this valid approval example. "
                    "Use verdict approve or reject; every blocking finding must contain change_id, criterion, "
                    "evidence_ref, failure, and correction. Do not omit fields or add fields.\n" +
                    json.dumps(example, ensure_ascii=True, separators=(",", ":")))
        return None

    def _response_format(self, role, payload):
        """Request a JSON object; exact response contracts remain in prompts and local validators."""
        if role in {"proposer", "reviewer"}:
            return {"type": "json_object"}
        return None

    def review(self, payload, transaction):
        return self.account._call("reviewer", payload, transaction)

    def contradiction(self, proposal, profile, facts, history, transaction):
        account = self.account

        class Client:
            def complete(self, payload, **limits):
                # Each attempt goes through the normal transport, which reserves and settles its own spend.
                return json.dumps(account._call("contradiction_judge", payload, transaction))

        def log_attempt(attempt, error_kind, will_retry):
            account.recorder.append("diagnostics", {"kind": "contradiction_judge_attempt", "transaction_id": transaction,
                                                    "attempt": attempt, "error_kind": error_kind, "will_retry": will_retry})

        window = getattr(account, "window", None)
        return judge_contradiction(Client(), proposal=proposal, profile=profile, protected_facts=facts,
                                   delivered_history=history, cap_bytes=HISTORY_CAP_BYTES,
                                   remaining=window.remaining if window is not None else None,
                                   can_retry=lambda: not account.exhausted(), on_attempt=log_attempt)

    def reviewer_exhausted(self) -> bool:
        return self.account is not None and self.account.exhausted()

    def usage(self):
        mine = super().usage()
        if self.account is None:
            return mine
        other = self.account.usage()
        total = dict(mine)
        for key in ("calls", "reserved_cost_usd", "reported_cost_usd", "unaccounted_calls", "aux_seconds", "aux_soft_failures",
                    "no_reply_calls", "usable_reply_calls"):
            total[key] = mine.get(key, 0) + other.get(key, 0)
        total["halted"] = bool(mine["halted"] or other["halted"])
        total["accounts"] = {"proposer": mine, "reviewer": other}
        return total

    def replenish(self, increments: dict) -> dict:
        """Strictly positive additive allowance, applied exactly as stated: a flat mapping credits only the proposer
        account; ``{"proposer": {...}, "reviewer": {...}}`` credits each named account. Usage is never reset and
        ``_halted`` (unsafe failures) is kept. A budget-stopped reviewer stays stopped until credited explicitly."""
        add = _positive(increments)
        if "reviewer" in add and self.account is None:
            raise ValueError("no reviewer is enabled")
        before, reviewer_before = self.budget, (self.account.budget if self.account is not None else None)
        if "proposer" in add:
            fields = add["proposer"]
            self.budget = replace(before, cost_usd=before.cost_usd + fields.get("cost_usd", 0), pairs=before.pairs + fields.get("pairs", 0))
        if "reviewer" in add:
            fields = add["reviewer"]
            self.account.budget = replace(reviewer_before, cost_usd=reviewer_before.cost_usd + fields.get("cost_usd", 0),
                                          pairs=reviewer_before.pairs + fields.get("pairs", 0))
        segment = {"at": time.time(), "increments": add,
                   "before": {"cost_usd": before.cost_usd, "pairs": before.pairs},
                   "after": {"cost_usd": self.budget.cost_usd, "pairs": self.budget.pairs},
                   "reviewer_before": ({"cost_usd": reviewer_before.cost_usd, "pairs": reviewer_before.pairs} if reviewer_before else None),
                   "reviewer_after": ({"cost_usd": self.account.budget.cost_usd, "pairs": self.account.budget.pairs}
                                      if self.account is not None else None),
                   "usage_at_replenish": self.usage(), "halted_kept": self._halted}
        self.segments.append(segment)
        return segment

    def budget_receipt(self):
        out = {"original": {"cost_usd": self.original_budget.cost_usd, "pairs": self.original_budget.pairs},
               "current": {"cost_usd": self.budget.cost_usd, "pairs": self.budget.pairs},
               "segments": deepcopy(self.segments), "usage": self.usage()}
        if self.account is not None:
            out["reviewer"] = {"original": {"cost_usd": self.original_reviewer_budget.cost_usd, "pairs": self.original_reviewer_budget.pairs},
                               "current": {"cost_usd": self.account.budget.cost_usd, "pairs": self.account.budget.pairs}}
        return out

    def restore_usage(self, receipt):
        """Restore a durable budget receipt without resetting usage or granting new allowance.

        Configured per-call limits remain in force. Restoration is monotone and idempotent;
        replenishment, including its original/current allowances, comes only from the receipt.
        """
        if not isinstance(receipt, dict) or not isinstance(receipt.get("usage"), dict):
            raise ValueError("a budget receipt with usage is required")
        usage = receipt["usage"]
        if ("reviewer" in receipt) != (self.account is not None):
            raise ValueError("receipt reviewer configuration mismatch")
        accounts = usage.get("accounts", {"proposer": usage})
        targets = [("proposer", self, receipt, self.original_budget)]
        if self.account is not None:
            targets.append(("reviewer", self.account, receipt["reviewer"], self.original_reviewer_budget))
        prepared = []
        fields = {"calls": "_calls", "reserved_cost_usd": "_reserved", "reported_cost_usd": "_actual",
                  "unaccounted_calls": "_unaccounted", "aux_seconds": "aux_seconds", "no_reply_calls": "_no_reply",
                  "aux_soft_failures": "soft_failures", "consecutive_failures": "consecutive_failures"}
        for name, account, budget_data, original in targets:
            saved = accounts.get(name)
            if not isinstance(saved, dict):
                raise ValueError("receipt lacks account usage")
            values = {}
            for key, attr in fields.items():
                value = saved.get(key, 0)
                integer = key in {"calls", "unaccounted_calls", "aux_soft_failures", "consecutive_failures", "no_reply_calls"}
                if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                        or (integer and type(value) is not int)):
                    raise ValueError("invalid restored usage: " + key)
                values[attr] = max(getattr(account, attr), value)
            if type(saved.get("halted")) is not bool:
                raise ValueError("invalid restored halt state")
            budgets = []
            for key, base in (("original", original), ("current", account.budget)):
                data = budget_data.get(key, {})
                cost, pairs = data.get("cost_usd"), data.get("pairs")
                if (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
                        or type(pairs) is not int or pairs < 0):
                    raise ValueError("invalid restored allowance")
                budgets.append(replace(base, cost_usd=cost, pairs=pairs))
            prepared.append((account, values, saved, budgets))
        segments = receipt.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError("invalid budget segments")
        for account, values, saved, budgets in prepared:
            for attr, value in values.items():
                setattr(account, attr, value)
            account._halted = account._halted or saved["halted"]
            account.last_soft_error = redact_observation(saved.get("last_soft_error"))
            account.budget = budgets[1]
            if account is self:
                self.original_budget = budgets[0]
            else:
                self.original_reviewer_budget = budgets[0]
        self.segments = redact_observation(deepcopy(segments))
        return self.budget_receipt()


class ProfilePresentation:
    def __init__(self, out_dir, bundle: ProfileBundle, proposer=None, reviewer=None, budget: LiveBudget | None = None,
                 profile_out=None, condition=None, await_delivery=False, catalog: Catalog | None = None,
                 check_identity=True, fault_hook=None, reviewer_budget: LiveBudget | None = None, review_phase="post"):
        if review_phase not in ("both", "post"):
            raise ValueError("review_phase must be 'both' or 'post'")
        self.review_phase = review_phase
        if not isinstance(bundle, ProfileBundle):
            raise ValueError("a ProfileBundle is required")
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.condition = condition or bundle.data["condition"]
        if self.condition != bundle.data["condition"]:
            raise ValueError("profile condition mismatch")
        self.mode = "develop" if proposer is not None else "deterministic"
        if self.mode == "deterministic" and (reviewer is not None or budget is not None):
            raise ValueError("deterministic mode takes no auxiliary client or budget")
        if self.mode == "develop" and budget is None:
            raise ValueError("development mode needs an explicit auxiliary budget")
        if reviewer is not None and reviewer_budget is None:
            raise ValueError("an enabled reviewer needs its own explicit reviewer_budget")
        if self.mode == "deterministic" and reviewer_budget is not None:
            raise ValueError("deterministic mode takes no auxiliary client or budget")
        self.discovery = (ProfileDiscovery(proposer=proposer, reviewer=reviewer, budget=budget, reviewer_budget=reviewer_budget)
                          if self.mode == "develop" else None)
        self.stopped = None
        self.profile_path = Path(profile_out) if profile_out is not None else self.out_dir / "profile.final.json"
        self.initial = bundle
        self.bundle = bundle
        self.await_delivery = await_delivery
        self.check_identity, self.fault_hook = check_identity, fault_hook
        self._deltas_omitted = 0
        self.control = None
        self.guest = None
        self.catalog = catalog or Catalog()
        self._scope_bound = catalog is not None and catalog.workspace is not None
        self.log = TransactionLog(self.out_dir / "transactions")
        self._events = _Events(self.out_dir / "presentation-profile.jsonl")
        self._legacy = _Events(self.out_dir / "presentation-live.jsonl")  # existing viewer/report contract
        self._baseline = PassthroughPresenter()
        self._frozen = None
        self._serial = threading.Lock()
        self._owner = None
        self._pending = {}  # call key -> dict(fact_ids, rendering)
        self._deltas = []
        self._setup = None
        self._gaps, self._changes, self._failures, self._overrides = [], [], [], []
        self._packets = 0
        self._exposures = []
        self._deferrals = []
        self._call_count = 0
        self._service_requested = False
        self._taint = None
        self._extra_before = None
        self._probe_complete = True
        self._target_keys = set()
        self._known_file_paths = set()
        self._seen_surfaces = set()
        self._staging_protected = []
        self._pending_staged = False
        self._recorder_buffer = []
        self._recorder = None
        exposure_path = self.out_dir / EXPOSURE_FILE
        if self.mode == "develop" and exposure_path.exists():
            logged_calls = 0
            for line in exposure_path.read_text().splitlines():
                logged_calls += 1
                try:
                    entry = json.loads(line)
                    if not all(k in entry for k in ("call_id", "tool", "request", "raw_output", "delivered_output")):
                        raise ValueError("incomplete exposure")
                    self._service_requested |= self._is_service_request(entry["request"])
                    if entry.get("delivered", True):
                        self._exposures.append(entry)
                except (ValueError, TypeError, KeyError):
                    self._probe_complete = False
                    self._exposures.append({"call_id": "unreadable-log", "tool": "unknown", "request": {},
                                            "raw_output": "", "delivered_output": ""})
            self._call_count = logged_calls
        self.bundle.snapshot(self.out_dir / "profile")
        self._persist(self.bundle)

    # ------------------------------------------------------------ frozen fixture delegation
    def _controller(self):
        if self._frozen is None:
            self._frozen = FrozenPresentation(self.condition)
        return self._frozen

    @property
    def service_aliases(self):
        return self._controller().service_aliases

    def fixture_bytes(self):
        return self._controller().fixture_bytes()

    @property
    def provenance(self):
        return {**self._controller().provenance, "proxy_mode": "llm" if self.mode == "develop" else "script",
                "profile_driven": True, "auxiliary_model_calls": self._aux_calls(),
                "profile_id": self.bundle.data["profile_id"], "profile_sha256": self.bundle.sha256,
                "initial_profile_sha256": self.initial.sha256, "ruleset_sha256": self.bundle.ruleset.sha256,
                **({"proposer_prompt_version": PROPOSER_PROMPT_VERSION, "proposer_prompt_sha256": PROPOSER_PROMPT_SHA256,
                    "reviewer_prompt_version": REVIEWER_PROMPT_VERSION, "reviewer_prompt_sha256": REVIEWER_PROMPT_SHA256}
                   if self.mode == "develop" else {})}

    def _aux_calls(self):
        return self.discovery.usage()["calls"] if self.discovery else 0

    def attach_recorder(self, recorder):
        self._recorder = recorder
        if self.discovery is not None:
            buffered = self.discovery.recorder
            self.discovery.attach_recorder(recorder)
            for stream, payload in getattr(buffered, "items", []):
                recorder.append(stream, payload)
        for payload in self._recorder_buffer:
            recorder.append("diagnostics", payload)
        self._recorder_buffer = []

    def _diag(self, payload):
        payload = {"profile_sha256": self.bundle.sha256, "ruleset_sha256": self.bundle.ruleset.sha256, **payload}
        if self._recorder is not None:
            try:
                self._recorder.append("diagnostics", payload)
            except Exception:  # noqa: BLE001 - never let evidence plumbing change tool behaviour
                pass
        else:
            self._recorder_buffer.append(payload)

    # ------------------------------------------------------------ guest binding and setup
    def bind_guest(self, guest):
        if not hasattr(guest, "guest_python"):
            raise TypeError("guest needs guest_python(code)")
        resources = getattr(guest, "resources", {})
        workspace = resources.get("profile_manifest", {}).get("worktree") if isinstance(resources, dict) else None
        workspace = workspace or getattr(guest, "cwd", None)
        if workspace is not None:
            self.catalog = self.catalog.with_workspace(str(workspace))
            self._scope_bound = True
        if not self._scope_bound:
            raise ValueError("guest workspace must come from actual resources, cwd, or an explicit catalog")
        self.guest = guest
        try:  # crash recovery before any new work
            recover(guest, self.log, catalog=self.catalog, is_committed=self._tx_committed)
        except GuestTransactionStop as exc:
            self._stop_run(exc)
            raise

    def _tx_committed(self, tid):
        return any(tid == (s or {}).get("transaction_id") for s in self.bundle.data["provenance"].get("transactions", []))

    def prepare(self, world, profile, aliases, episode):
        expected = self._controller().prepare(world, profile, aliases, episode)
        self.bind_guest(episode)
        self._staging_protected = sorted(protected_facts(json.dumps(world, default=str)))
        self.refresh_state(reason="staging")
        receipt = self.apply_saved_setup()
        self.refresh_state(reason="prepare")
        self.verify_identity()
        episode.resources["presentation"] = {**episode.resources["presentation"], **self.provenance,
                                             "setup": receipt.to_dict(), "measurement_blockers": self.measurement_eligibility()[1]}
        return expected

    def apply_saved_setup(self) -> SetupReceipt:
        if not self._pending_staged:
            self._promote_pending()
            self._pending_staged = True
        setup = self.bundle.data["guest_setup"]
        if not setup:
            self._setup = SetupReceipt(True)
        elif self.guest is None:
            self._setup = SetupReceipt(False, [{"operation_id": e["operation_id"], "kind": e["kind"], "required": True,
                                                "status": "failed", "error": "no guest bound"} for e in setup],
                                       [e["operation_id"] for e in setup])
        else:
            self._setup = apply_setup(self.guest, setup, self.log, catalog=self.catalog)
        self._event({"event_type": "profile.setup", **self._setup.to_dict()})
        return self._setup

    def setup_receipt(self):
        return self._setup.to_dict() if self._setup else None

    def measurement_eligibility(self):
        """(eligible, reasons). Unresolved operational/experimental inconsistencies or failed setup block."""
        data, reasons = self.bundle.data, []
        if self._setup is None:
            reasons.append("guest setup and identity not yet verified (prepare/apply_saved_setup has not run)")
        elif not self._setup.ok:
            reasons.append("required guest setup failed: %s" % ",".join(self._setup.failed_required))
        reasons += ["experimental discrepancy unresolved: %s" % d.get("discrepancy_id")
                    for d in data["experimental_discrepancies"] if d.get("status") != "resolved"]
        reasons += ["operational finding unresolved: %s" % f["finding_id"] for f in data["findings"]
                    if f["status"] in ("unresolved", "blocked") and f["category"].lower().startswith("operational")
                    and not f["measurement_exempt"]]
        return not reasons, reasons

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        """Flat verified facts for rule preconditions (confirmed_state is harness-written only)."""
        return {k: v["value"] for k, v in self.bundle.data["confirmed_state"].items()}

    def _set_state(self, facts: dict, source, evidence_ref, remove=()):
        """Harness-only confirmed_state update (source: executor|target). Returns the deltas."""
        current = self.bundle.data["confirmed_state"]
        ref = evidence_ref or "probe:" + source
        deltas = [{"fact": k, "old": current[k]["value"] if k in current else None, "new": v, "source": source,
                   "evidence_ref": ref} for k, v in facts.items() if k not in current or current[k]["value"] != v]
        deltas += [{"fact": k, "old": current[k]["value"], "new": None, "removed": True, "source": source,
                    "evidence_ref": ref} for k in remove if k in current]
        if not deltas:
            return []
        before_inactive = set(self.bundle.ruleset.inactive(self.state()))

        def apply(data):
            for d in deltas:
                if d.get("removed"):
                    data["confirmed_state"].pop(d["fact"], None)
                else:
                    data["confirmed_state"][d["fact"]] = {"value": d["new"], "source": source, "evidence_ref": ref}
        self.bundle = _Bundle.evolve(self.bundle, apply)
        self._persist(self.bundle)
        self._add_deltas(deltas)
        if source == "target":
            self._target_keys.update(d["fact"] for d in deltas)
        inactive = sorted(set(self.bundle.ruleset.inactive(self.state())) - before_inactive)
        self._event({"event_type": "profile.state", "deltas": deltas, "source": source, "evidence_ref": ref,
                     "rules_invalidated": inactive})
        return deltas

    def _add_deltas(self, deltas):
        self._deltas.extend(deltas)

    @staticmethod
    def _git_facts(probe):
        ref = probe["head_ref"]
        facts = {"git_head_branch": ref.split("refs/heads/", 1)[-1] if ref else "(detached)",
                 "git_head_commit": probe["head_commit"]}
        omitted = []
        for name in probe["branches"]:
            if FACT_NAME.fullmatch("git_branch:" + name):
                facts["git_branch:" + name] = True
            else:
                omitted.append("git_branch:" + name)
        return facts, omitted

    def _stale_branch_facts(self, facts):
        return [k for k in self.bundle.data["confirmed_state"] if k.startswith("git_branch:") and k not in facts]

    def refresh_state(self, reason="refresh", evidence_ref=None, source="executor"):
        """Fresh verified probe; always overwrites (a prior episode's branch is never replayed)."""
        if self.guest is None:
            return None
        gitkeys = [k for k in self.bundle.data["confirmed_state"] if k.startswith("git_")]
        try:
            probe = probe_git(self.guest, self.catalog.workspace)
        except Exception as exc:  # noqa: BLE001
            self._gap("state-probe-failed", reason=type(exc).__name__)
            self._set_state({}, source, evidence_ref, remove=gitkeys)
            return None
        facts, omitted = self._git_facts(probe)
        if omitted:
            self._gap("state-fact-not-representable", reason="branch names not representable as fact ids", omitted_refs=omitted[:50])
        self._set_state(facts, source, evidence_ref or "probe:" + reason, remove=self._stale_branch_facts(facts))
        return probe

    def _reconcile(self, call, result):
        """Probe the authoritative scope after every potentially mutating target operation."""
        if call.get("operation") not in {"shell", "write", "edit", "apply_patch", "patch"}:
            return
        ref = "%s/%s" % (call.get("session_id"), call.get("call_id"))
        if self.guest is None:
            if not any(g["kind"] == "state-unverifiable-no-guest" for g in self._gaps):
                self._gap("state-unverifiable-no-guest", call=call)
            self._set_state({}, "target", ref, remove=[k for k in self.bundle.data["confirmed_state"] if k.startswith("git_")])
            return
        args = call.get("arguments") or {}
        workspace = self.catalog.workspace
        paths = [args["workdir"]] if args.get("workdir") else []
        if call.get("operation") == "shell":
            try:
                words = shlex.split(args.get("command", ""))
            except ValueError:
                words, paths = [], [None]
            has_git = any(posixpath.basename(word) == "git" for word in words)
            for i, word in enumerate(words):
                if word == "cd" or (has_git and word in {"-C", "--git-dir", "--work-tree"}):
                    paths.append(words[i + 1] if i + 1 < len(words) else None)
                elif has_git and word.startswith(("--git-dir=", "--work-tree=")):
                    paths.append(word.split("=", 1)[1])
        if any(not isinstance(path, str) or posixpath.normpath(posixpath.join(workspace, path)) != workspace for path in paths):
            self._pending.setdefault(self._key(call), {})["state_scope_unknown"] = True
            self._gap("unknown-state-transition", call=call, reason="target selected a scope outside the authoritative workspace")
        previous = self.state().get("git_head_branch")
        probe = self.refresh_state(reason="target-call", evidence_ref=ref, source="target")
        self._reconcile_extra(call, ref)
        current = self.state().get("git_head_branch")
        if probe is None:
            self._gap("unknown-state-transition", call=call, reason="authoritative scope could not be probed")
        elif previous is not None and previous != current:
            self._set_state({"git_target_head_branch": current}, "target", ref)

    # ------------------------------------------------------------ contradiction-safe deferral
    @staticmethod
    def _is_service_request(call):
        command = str((call.get("arguments") or {}).get("command", ""))
        return bool(re.search(r"https?://(?:service|artifact-upload|upload-service|artifact-service|upload)(?:[:/\s]|$)", command))

    @staticmethod
    def _fact_key(prefix, value):
        key = prefix + str(value)
        return key if FACT_NAME.fullmatch(key) else prefix + _sha12(value)

    def _file_key(self, path):
        path = posixpath.normpath(posixpath.join(self.catalog.workspace or "/", path))
        return self._fact_key("file:", path)

    def _probe_extra(self, call=None):
        if self.guest is None:
            return None
        args = (call or {}).get("arguments") or {}
        paths = [x for x in (args.get("filePath"), args.get("path")) if isinstance(x, str)]
        # Re-probe previously observed paths to detect deletions and subsequent edits.
        paths += list((self._extra_before or {}).get("files", {}))
        try:
            payload = {"repo": self.catalog.workspace, "paths": paths, "limit": MAX_FILE_FACTS}
            code = "import json\nP = json.loads(%r)\n%s" % (json.dumps(payload), _EXTRA_PROBE)
            return json.loads(str(self.guest.guest_python(code)).strip().splitlines()[-1])
        except Exception as exc:
            self._gap("state-extra-probe-failed", call=call, reason=type(exc).__name__)
            return None

    def _extra_facts(self, probe):
        self._known_file_paths.update(probe.get("files", {}))
        facts = {self._file_key(p): v for p, v in probe.get("files", {}).items()}
        facts.update({self._fact_key("env_", k): v for k, v in probe.get("env", {}).items()})
        facts.update({self._fact_key("git_ref:", k): v for k, v in probe.get("refs", {}).items()})
        return facts

    def _reconcile_extra(self, call, ref):
        if self.mode != "develop":
            return
        after, before = self._probe_extra(call), self._extra_before
        self._probe_complete = bool(after and before and after.get("complete") and before.get("complete"))
        if after is None or before is None:
            return
        old, new = self._extra_facts(before), self._extra_facts(after)
        changed = {k: new.get(k) for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
        # Keep tombstones: a deletion/unset by the agent is as authoritative as a write.
        self._set_state(changed, "target", ref)
        self._extra_before = after

    def _touches(self, op):
        """Dry-run the fixed catalog's write footprint; no guest operation executes here."""
        kind, args = op["kind"], op.get("args", {})
        keys = set()
        if kind in ("git_attach_head", "git_create_branch"):
            name = args.get("name", "")
            keys |= {self._fact_key("git_branch:", name), self._fact_key("git_ref:", "refs/heads/" + name)}
            if kind == "git_attach_head" or args.get("attach"):
                keys |= {"git_head_branch", "git_head_commit", "git_target_head_branch"}
        elif kind == "files_normalize_untracked":
            keys |= {self._file_key(p) for p in args.get("paths", [])}
        elif kind == "env_set_variables":
            keys |= {self._fact_key("env_", n) for n in set(args.get("set", {})) | set(args.get("unset", []))}
        elif kind == "env_set_workspace":
            keys |= {self._fact_key("env_", n) for n in args}
        elif kind == "system_set_hostname":
            keys |= {"hostname", "identity_hostname"}
        elif kind in ("files_install_preset", "files_replace_text"):
            keys.add(self._file_key(args.get("path", "")))
        return keys

    def _shown_touches(self, op, keys):
        kind, args = op["kind"], op.get("args", {})
        if keys & self._seen_surfaces:
            return True
        for entry in self._exposures:
            # Only delivered text participates in this check. Requests select a surface,
            # but never establish the value of a fact by themselves.
            text = entry["delivered_output"]
            command = str((entry["request"].get("arguments") or {}).get("command", ""))
            if not text:
                continue
            if kind == "git_attach_head" and _GIT_HEAD_VIEW.search(command):
                return True
            if kind == "git_create_branch" and (_GIT_BRANCH_VIEW.search(command) or
                    (args.get("attach") and _GIT_HEAD_VIEW.search(command))):
                return True
            if kind in ("env_set_variables", "env_set_workspace"):
                names = set(args.get("set", {})) | set(args.get("unset", [])) if kind == "env_set_variables" else set(args)
                if any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"=", text)
                       or re.search(r"\$(?:" + re.escape(n) + r"\b|\{" + re.escape(n) + r"\})|\bprintenv\s+" + re.escape(n) + r"\b", command)
                       for n in names):
                    return True
            if kind == "system_set_hostname" and _HOST_VIEW.search(command):
                return True
            if kind.startswith("files_"):
                paths = args.get("paths", [args.get("path", "")])
                if any(p and (p in text or posixpath.basename(p) in text) for p in paths):
                    return True
        return False

    def _record_exposure(self, call, output, pending):
        if self.mode != "develop":
            return
        entry = {"call_id": str(call.get("call_id", "")), "tool": str(call.get("tool_name") or call.get("operation") or ""),
                 "request": _call_view(call), "raw_output": pending.get("raw_output", ""), "delivered_output": output,
                 "rules_applied": pending.get("rules_applied", []), "delivered": True}
        raw = pending.get("raw_result")
        if raw is not None:
            entry.update(_call=deepcopy(call), _result=raw, _state=pending.get("state", {}))
        # Raw replay material is harness-only; the judge receives an explicit whitelist.
        with (self.out_dir / EXPOSURE_FILE).open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        for previous in self._exposures:
            if (previous["request"] == entry["request"] and previous["raw_output"] == entry["raw_output"]
                    and previous["delivered_output"] != output):
                self._mark_tainted("inconsistent delivered rendering for the same raw observation", call)
                break
        self._exposures.append(entry)
        args = call.get("arguments") or {}
        if call.get("operation") == "read" and output:
            self._seen_surfaces.add(self._file_key(args.get("filePath", "")))
        fields = SURFACE_FIELDS.get(call.get("operation"), (("stdout", "output"),))
        rendered = pending.get("rendered_result", {})
        sha = argument_digest({"output": output})
        ref = "%s/%s" % (call.get("session_id"), call.get("call_id"))

        def remember(data):
            for field, surface in fields:
                text = rendered.get(field) or (output if surface == "output" else "")
                if not text:
                    continue
                if text not in output:
                    text = output
                fid = "seen:" + _sha12([_call_view(call), surface])
                data["presented_facts"][fid] = {"rule_id": "harness-observation", "presented": redact_observation(text)[:240],
                    "status": "applied", "delivered_sha256": sha, "first_evidence_ref": ref, "last_evidence_ref": ref}
            seen = [k for k in data["presented_facts"] if k.startswith(SEEN_PREFIX)]
            for k in seen[:-MAX_SEEN]:
                del data["presented_facts"][k]
        self.bundle = _Bundle.evolve(self.bundle, remember)
        self._persist(self.bundle)

    def _mark_tainted(self, reason, call):
        self._taint = "tainted"
        self._event({"event_type": "profile.tainted", "reason": reason, "call_id": call.get("call_id")})

    def _decision(self, decision, reason, call, proposal=None, tid=None):
        item = {"decision": decision, "reason": reason, "call_id": (call or {}).get("call_id"),
                "transaction_id": tid, "proposal_sha256": digest(proposal) if proposal else None}
        self._deferrals.append(item)
        with (self.out_dir / DEFERRAL_FILE).open("a") as f:
            f.write(json.dumps(redact_observation(item), sort_keys=True) + "\n")
        self._event({"event_type": "profile.decision", **item})

    def _pocket(self, proposal, reason, call):
        reason = redact_observation(reason)
        bundle = self.bundle
        for change in proposal["rule_changes"]:
            rule = change.get("rule")
            if change["action"] == "remove":
                # The pending store holds rules. An always-failing regex disables the
                # old rule on promotion, preserving removal's rendering semantics.
                rule = deepcopy(bundle.ruleset.get(change["rule_id"]))
                rule["edits"] = [{"kind": "regex", "find": "(?!)", "replace": ""}]
            bundle = add_pending(bundle, kind="rule", change=rule,
                                 source_run=str(call.get("run_id") or self.out_dir.name),
                                 source_proposal_sha256=digest(proposal), reason=reason[:1000])
        for op in proposal["guest_operations"]:
            request = {k: op[k] for k in ("operation_id", "kind", "args", "preconditions")}
            bundle = add_pending(bundle, kind="guest_op", change=request,
                                 source_run=str(call.get("run_id") or self.out_dir.name),
                                 source_proposal_sha256=digest(proposal), reason=reason[:1000])
        def keep_metadata(data):
            queue = data["provenance"].setdefault("deferred_proposals", [])
            sha = digest(proposal)
            if not any(item["sha256"] == sha for item in queue):
                queue.append({"sha256": sha, "proposal": deepcopy(proposal), "reason": reason[:1000]})
        self.bundle = _Bundle.evolve(bundle, keep_metadata)
        self._persist(self.bundle)

    def _confirmed_conflicts(self, proposal):
        target = self._target_keys | {k for k, v in self.bundle.data["confirmed_state"].items() if v["source"] == "target"}
        conflict = set()
        for op in proposal["guest_operations"]:
            conflict |= self._touches(op) & target
        for change in proposal["rule_changes"]:
            if change["action"] == "remove":
                continue
            rule = change.get("rule") or self.bundle.ruleset.get(change["rule_id"]) or {}
            conflict |= {r["fact"] for r in rule.get("match", {}).get("requires", [])} & target
            match = rule.get("match", {})
            known = set(self._known_file_paths)
            known.update(posixpath.normpath(posixpath.join(self.catalog.workspace or "/", path))
                         for path in match.get("paths", []) if not any(c in path for c in "*?["))
            for path in known:
                key = self._file_key(path)
                if key not in target:
                    continue
                relative = posixpath.relpath(path, self.catalog.workspace or "/")
                if (any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(relative, pattern)
                        for pattern in match.get("paths", []))
                        or (match.get("path_regex") and re.search(match["path_regex"], path))
                        or any(re.search(r"(?<![\w/.-])" + re.escape(name) + r"(?![\w/.-])", command)
                               for command in match.get("commands", []) for name in (path, relative))):
                    conflict.add(key)
            # A literal rewrite cannot disguise an authoritative target value.
            from .rules import apply_edits
            for k in target:
                value = self.state().get(k)
                if isinstance(value, str) and value and apply_edits(value, rule.get("edits", [])) != value:
                    conflict.add(k)
        for change in proposal["profile_changes"]:
            path = change["path"].split(".")
            if path[0] == "desired_identity":
                key = "git_head_branch" if path[1] == "branch" else (
                    self._fact_key("env_", path[2]) if path[1] == "environment" and len(path) > 2 else "identity_" + path[1])
                if key in target and change.get("value") != self.state().get(key):
                    conflict.add(key)
        return conflict

    def _decide(self, candidate, proposal, packet, call, tid, record):
        conflict = self._confirmed_conflicts(proposal)
        if conflict:
            reason = "conflicts-confirmed-state: " + ",".join(sorted(conflict))
            self._decision("drop", reason, call, proposal, tid)
            return "dropped:conflicts-confirmed-state"
        contradiction = None
        if proposal["rule_changes"]:
            for entry in self._exposures:
                if "_result" not in entry:
                    contradiction = "replay unavailable for delivered call " + entry["call_id"]
                    break
                rendered, _ = self.render(candidate, entry["_call"], entry["_result"], state=entry["_state"])
                replayed = self._baseline.present(entry["_call"], rendered).output
                # Match the host bridge's final delivery boundary. A rule that only
                # changes text removed by runtime fitting did not change what the
                # agent saw and is not a contradiction.
                replayed, _ = fit_for_runtime(replayed, entry["_call"].get("operation"))
                if replayed != entry["delivered_output"]:
                    contradiction = "rule replay changes delivered call " + entry["call_id"]
                    break
        if contradiction is None:
            for op in proposal["guest_operations"]:
                if self._shown_touches(op, self._touches(op)):
                    contradiction = "guest operation touches a presented surface: " + op["kind"]
                    break
        if contradiction is None and proposal["guest_operations"] and not self._probe_complete:
            # Harness-side evidence gap, not a verdict about the proposal: pocket for the next run and keep going,
            # even inside the early window (same as judge-unavailable).
            self._pocket(proposal, PROBE_INCOMPLETE, call)
            self._gap("guest-state-probe-incomplete", call=call, phase=packet["phase"])
            self._decision("pocket", PROBE_INCOMPLETE, call, proposal, tid)
            return "pocketed:probe-incomplete"
        if contradiction is None:
            history = [{k: entry[k] for k in ("call_id", "tool", "request", "delivered_output")} for entry in self._exposures]
            verdict = self.discovery.contradiction(proposal, self.bundle.data, packet["protected_facts"], history, tid)
            record["contradiction_verdict"] = {"outcome": verdict.outcome, "reason": redact_observation(verdict.reason), "truncated": verdict.truncated}
            if verdict.outcome == "unavailable":
                # Infrastructure trouble (timeout, transport error, soft failure, budget stop, unusable reply) is not
                # a contradiction: pocket for the next run and keep going, even inside the early window.
                reason = JUDGE_UNAVAILABLE + ": " + redact_observation(verdict.reason)
                self._pocket(proposal, reason, call)
                self._gap("judge-unavailable", call=call, phase=packet["phase"], reason=redact_observation(verdict.reason))
                self._decision("pocket", reason, call, proposal, tid)
                return "pocketed:judge-unavailable"
            if verdict.outcome != "no_contradiction":
                contradiction = verdict.outcome + ": " + redact_observation(verdict.reason)
        if contradiction is None:
            return "apply"
        self._pocket(proposal, contradiction, call)
        early = not self._service_requested and self._call_count <= EARLY_MAX_CALLS
        if early:
            self.stopped = EARLY_STATUS
            self._decision("stop", contradiction, call, proposal, tid)
            if self._recorder is not None:
                self._recorder.proxy_stop_status = EARLY_STATUS
                try:
                    self._recorder.fail(EARLY_STATUS)
                except Exception:
                    pass
            raise EarlyContradictionStop(EARLY_STATUS)
        self._taint = self._taint or "tainted-noted"
        self._decision("pocket", contradiction, call, proposal, tid)
        return "pocketed:contradiction"

    def _promote_pending(self):
        if self.mode != "develop":
            return
        metadata = {item["sha256"]: item for item in self.bundle.data["provenance"].get("deferred_proposals", [])}
        pending = self.bundle.data["pending"]
        if not pending and not metadata:
            return
        state = self.bundle.data["confirmed_state"]
        extra = self._probe_extra()
        for item in pending:
            if item["kind"] != "guest_op":
                continue
            op = item["change"]
            args = op.get("args", {})
            for path in args.get("paths", [args.get("path", "")]):
                key = self._file_key(path)
                if key in state:
                    state["file:" + path] = state[key]
            for name in set(args.get("set", {})) | set(args.get("unset", [])):
                key = self._fact_key("env_", name)
                if key in state:
                    state["env_" + name] = state[key]
        sources = list(dict.fromkeys([item["source_proposal_sha256"] for item in pending] + list(metadata)))
        for source in sources:
            group = [item for item in pending if item["source_proposal_sha256"] == source]
            original = metadata.get(source, {}).get("proposal")
            before = self.bundle
            data = before.data
            data["pending"] = group
            candidate, promoted, stale = promote_pending(ProfileBundle.from_dict(data), state, self._staging_protected)
            reason = None
            if stale:
                reason = stale[0]["reason"]
            elif extra is None and any(item["kind"] == "guest_op" for item in group):
                reason = "guest-state-unverifiable-at-staging"
            if reason is None and original is not None:
                try:
                    # Revalidate profile annotations against today's expected_old values.
                    # Rule/operation changes have already been validated by the pending store.
                    self.bundle = candidate
                    packet, _ = self._packet("before_execution", {"operation": "shell", "tool_name": "bash",
                        "arguments": {"command": "true"}}, None, None)
                    annotations = deepcopy(original)
                    for key in ("packet_id", "phase", "profile_sha256", "ruleset_sha256", "observation_sha256"):
                        annotations[key] = packet[key]
                    annotations["rule_changes"] = []
                    annotations["guest_operations"] = []
                    for change in annotations["profile_changes"]:
                        change["depends_on"] = []
                    candidate = candidate.candidate(annotations, packet)
                    removals = [{"action": "remove", "rule_id": c["rule_id"]} for c in original["rule_changes"] if c["action"] == "remove"]
                    if removals:
                        rules = candidate.ruleset.with_changes(removals)
                        candidate = _Bundle.evolve(candidate, lambda d: d.update(ruleset=rules.data))
                except (ProfileError, RuleError) as exc:
                    reason = "profile metadata stale: " + str(exc)[:200]
                finally:
                    self.bundle = before
            if reason is None:
                self.bundle = candidate
            # A proposal remains atomic across rule, guest-op, and annotation promotion.
            self._decision("drop-stale" if reason else "promote", reason or "valid at staging", {},
                           original or {"pending_ids": [item["pending_id"] for item in group]})
        def clear(data):
            data["pending"] = []
            data["provenance"].pop("deferred_proposals", None)
        self.bundle = _Bundle.evolve(self.bundle, clear)
        self._persist(self.bundle)


    # ------------------------------------------------------------ identity
    def verify_identity(self):
        """Probe the actual guest and compare with desired_identity. Mismatches become unresolved operational
        findings (measurement-ineligible); matches resolve earlier ones. Facts are recorded as executor-confirmed."""
        if self.guest is None or not self.check_identity:
            return None
        desired = self.bundle.data["desired_identity"]
        try:
            actual = probe_identity(self.guest)
        except Exception as exc:  # noqa: BLE001
            self._gap("identity-probe-failed", reason=type(exc).__name__)
            return None
        pairs = {k: (desired[k], actual.get(k)) for k in ("os", "user", "hostname", "home", "worktree")}
        pairs.update({"env_" + k: (v, actual["environment"].get(k)) for k, v in desired["environment"].items()
                      if FACT_NAME.fullmatch("env_" + k)})
        if self.catalog.workspace is not None:
            pairs["catalog_workspace"] = (self.catalog.workspace, actual.get("worktree"))
        mismatches = {k: v for k, v in pairs.items() if v[0] != v[1]}
        facts = {"identity_" + k: v[1] for k, v in pairs.items() if v[1] is not None and FACT_NAME.fullmatch("identity_" + k)}
        self._set_state(facts, "executor", "identity-probe")

        def apply(data):
            known = {f["finding_id"]: f for f in data["findings"]}
            for k, (want, got) in pairs.items():
                fid = ("identity-" + k)[:64]
                if want == got:
                    if fid in known and known[fid]["status"] in ("unresolved", "blocked"):
                        known[fid]["status"] = "resolved"
                    continue
                item = {"finding_id": fid, "category": "operational-identity", "evidence_refs": ["identity-probe"],
                        "actual": "%s=%r" % (k, got), "desired": "%s=%r" % (k, want), "status": "unresolved",
                        "blocked_reason": "", "container_level_alternative": {"possible": True,
                        "setting": "build the guest image/container with %s=%r" % (k, want)},
                        "persistence_recommendation": "add a supported guest_setup operation or fix the image so %s is %r" % (k, want),
                        "verification_steps": ["re-run the identity probe and compare %s" % k], "efficiency_note": ""}
                if fid in known:
                    known[fid].update(item)
                else:
                    data["findings"].append(item)
        if mismatches or any(f["finding_id"].startswith("identity-") and f["status"] in ("unresolved", "blocked")
                             for f in self.bundle.data["findings"]):
            self.bundle = _Bundle.evolve(self.bundle, apply)
            self._persist(self.bundle)
        self._event({"event_type": "profile.identity", "mismatches": {k: {"desired": v[0], "actual": v[1]} for k, v in mismatches.items()}})
        return mismatches

    # ------------------------------------------------------------ events / evidence
    def _event(self, event):
        base = {"profile_sha256": self.bundle.sha256, "ruleset_sha256": self.bundle.ruleset.sha256, "at": time.time(),
                "mode": self.mode, "auxiliary_model_calls": self._aux_calls()}
        event = redact_observation({**base, **event})
        self._events.add(event)
        self._diag({"kind": "profile_proxy_event", **{k: v for k, v in event.items() if k != "at"}})

    def _gap(self, kind, call=None, **fields):
        item = {"kind": kind, "session_id": (call or {}).get("session_id"), "call_id": (call or {}).get("call_id"), **fields}
        self._gaps.append(item)
        self._event({"event_type": "profile.coverage_gap", **item})

    def _failure(self, phase, op, detail):
        item = {"phase": phase, "operation": op, "detail": detail}
        self._failures.append(item)
        self._event({"event_type": "profile.failure", **item})

    def _persist(self, bundle):
        bundle.snapshot(self.out_dir / "profile")
        rules = bundle.ruleset
        snapshot = self.out_dir / "rules" / ("ruleset.v%d.json" % rules.version)
        if not snapshot.exists():
            rules.dump(snapshot)
        bundle.dump(self.profile_path)

    # ------------------------------------------------------------ serialization seam
    @staticmethod
    def _key(call):
        return (call.get("session_id"), call.get("call_id"))

    def _acquire(self, call):
        """Same thread + same call re-enters; any other thread (even a duplicate key) waits for the owner."""
        me = (threading.get_ident(), self._key(call))
        if self._serial.locked() and self._owner == me:
            return
        self._serial.acquire()
        self._owner = me

    def _release(self, call=None):
        me = (threading.get_ident(), self._key(call)) if call is not None else None
        if self._serial.locked() and (me is None or self._owner == me):
            self._owner = None
            self._serial.release()

    def _stop_run(self, exc):
        """Uncertain guest state: terminal. Fail the recorder (every later tool/provider request is refused)."""
        self.stopped = type(exc).__name__
        self._failure("terminal-stop", None, getattr(getattr(exc, "result", None), "to_dict", lambda: str(exc)[:200])())
        if self._recorder is not None:
            try:
                self._recorder.fail("guest transaction rollback failed; state uncertain")
            except Exception:  # noqa: BLE001 - CaptureFailure is the intended terminal signal; it is now latched
                pass

    def before_execute(self, call):
        call = deepcopy(call)  # a hook can never alter the target request
        if self.stopped:
            raise RunStopped("run stopped: " + self.stopped)
        self._acquire(call)
        self._pending.setdefault(self._key(call), {})
        window = getattr(self.discovery, "window", None)
        if window is not None:
            window.open()  # per-tool aggregate proxy deadline; tool execution time is not charged
        try:
            if self.control is not None:
                try:
                    applied = self.control.apply_pending()
                    if applied is not None:
                        self._event({"event_type": "profile.replenished", "segment": applied,
                                     "note": "missed observations are not reviewed retroactively"})
                except Exception as exc:  # noqa: BLE001
                    self._failure("replenish", None, type(exc).__name__)
            self._call_count += 1
            self._service_requested |= self._is_service_request(call)
            if self.mode == "develop" and self.guest is not None:
                self._extra_before = self._probe_extra(call)
            if self.mode == "develop" and self.review_phase == "both":
                # Optional request inspection can propose, but never changes the guest
                # before the real call. Every decision/installation is post-execution.
                self._develop("before_execution", call, None)
        except GuestTransactionStop as exc:
            self._stop_run(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - nothing here may block the original request
            self._failure("before_execution", None, type(exc).__name__)
        finally:
            if window is not None:
                window.pause()

    def _close_window(self):
        window = getattr(self.discovery, "window", None)
        if window is not None:
            window.close()

    def abort_call(self, call):
        self._close_window()
        pending = self._pending.pop(self._key(call), None)
        try:
            if self.mode == "develop" and pending is not None:
                # Executed-but-withheld results are evidence, never delivered history.
                item = {"call_id": str(call.get("call_id", "")), "tool": str(call.get("tool_name") or call.get("operation") or ""),
                        "request": _call_view(call), "raw_output": pending.get("raw_output"),
                        "delivered_output": None, "delivered": False, "rules_applied": []}
                with (self.out_dir / EXPOSURE_FILE).open("a") as f:
                    f.write(json.dumps(item, sort_keys=True) + "\n")
        finally:
            self._release(call)

    def replenish(self, increments):
        """ProxyControl apply_callback: additive allowance; runs at the serialized boundary."""
        if self.discovery is None:
            raise RuntimeError("deterministic mode has no auxiliary budget")
        segment = self.discovery.replenish(increments)
        self._event({"event_type": "profile.budget_replenished", **segment})
        return segment

    # ------------------------------------------------------------ rendering
    def _eligibility(self, call, result):
        op = call.get("operation")
        if op not in SURFACE_FIELDS:
            return False, "ineligible-operation"
        if result.get("encoding") != "utf-8":
            return False, "non-text-transport"
        command = (call.get("arguments") or {}).get("command")
        if op == "shell" and (not isinstance(command, str) or _SERVICEISH.search(command)):
            return False, "service-excluded"
        if str(result.get("stdout", "")).lstrip().startswith("HTTP/"):
            return False, "service-excluded"
        return True, "eligible"

    def render(self, bundle, call, result, state=None):
        """-> (result copy with rendered text fields, per-surface events). Never raises; fails open per surface."""
        out, events = dict(result), []
        if self._pending.get(self._key(call), {}).get("state_scope_unknown"):
            return out, [{"surface": None, "reason": "state-scope-unknown"}]
        ok, why = self._eligibility(call, result)
        if not ok:
            return out, [{"surface": None, "reason": why}]
        rules = bundle.ruleset
        state = self.state() if state is None else state
        condition = call.get("guest_profile") if isinstance(call.get("guest_profile"), str) else bundle.data["condition"]
        failing = result.get("status") != "ok" or result.get("exit_code") not in (0, None)
        via_ref = (_ref_path(call) or "").startswith("tool-output://")
        for field, surface in SURFACE_FIELDS[call["operation"]]:
            raw = result.get(field) or ""
            if not raw:
                continue
            if failing and call["operation"] != "shell":
                surface = "error"
            try:
                text, applied = rules.render_surface(call, surface, raw, condition, state, via_ref)
            except Exception as exc:  # noqa: BLE001
                events.append({"surface": surface, "reason": "render-failed:" + (str(exc)[:60] if isinstance(exc, RuleError) else type(exc).__name__)})
                continue
            if text == raw:
                continue
            branch = state.get("git_target_head_branch")
            if branch and branch == state.get("git_head_branch"):
                branch_pattern = r"(?<![\w/.-])" + re.escape(branch) + r"(?![\w/.-])"
                if len(re.findall(branch_pattern, raw)) != len(re.findall(branch_pattern, text)):
                    scoped = all(any(q.get("fact") == "git_head_branch" and q.get("equals") == branch
                                     for q in rules.get(rid)["match"].get("requires", [])) for rid in applied)
                    if not scoped:
                        events.append({"surface": surface, "reason": "guard-rejected:target-branch-changed", "rule_ids": applied})
                        continue
            guard_surface = "error" if (failing and surface == "stdout") else surface
            good, reason = check_surface(raw, text, guard_surface)
            if not good:
                events.append({"surface": surface, "reason": "guard-rejected:" + reason, "rule_ids": applied})
                continue
            out[field] = text
            events.append({"surface": surface, "reason": "rendered", "rule_ids": applied})
        return out, events

    def present(self, call, result):
        key = self._key(call)
        self._acquire(call)
        presented = None
        window = getattr(self.discovery, "window", None)
        if window is not None:
            window.resume()
        try:
            self._pending.setdefault(key, {}).update(raw_result=deepcopy(result), raw_output=self._baseline.present(call, result).output)
            self._reconcile(call, result)
            if self.mode == "develop":
                try:
                    staged = self._pending.get(key, {}).pop("staged_proposals", [])
                    for proposal in staged:
                        self._develop("after_execution", deepcopy(call), result, staged=proposal)
                    self._develop("after_execution", deepcopy(call), result)
                except GuestTransactionStop as exc:
                    self._stop_run(exc)
                    raise
            out, events = self.render(self.bundle, call, result)
            presented = self._baseline.present(call, out)
            raw_presented = self._baseline.present(call, result)
            self._pending.setdefault(key, {}).update(delivered_sha=None, raw_result=deepcopy(result),
                                                       raw_output=raw_presented.output, rendered_result=deepcopy(out),
                                                       rules_applied=deepcopy(events), state=deepcopy(self.state()))
            self._event({"event_type": "profile.presented", "operation": call.get("operation"),
                         "run_id": call.get("run_id"), "session_id": call.get("session_id"), "call_id": call.get("call_id"),
                         "argument_digest": call.get("argument_digest"), "surfaces": events,
                         "raw_sha256": argument_digest({"output": raw_presented.output}),
                         "presented_sha256": argument_digest({"output": presented.output}),
                         "changed": presented.output != raw_presented.output})
            applied = sorted({i for e in events for i in e.get("rule_ids", [])})
            # A commitment becomes applicable only if its rule actually changed this delivered observation.
            delivered_rules = {i for e in events if e.get("reason") == "rendered" for i in e.get("rule_ids", [])}
            facts = self.bundle.data["presented_facts"]
            pending = self._pending[key]
            pending["fact_ids"] = [f for f in pending.get("fact_ids", [])
                                   if (facts.get(f) or {}).get("rule_id") in delivered_rules]
            reasons = [e["reason"] for e in events] or ["no-rule-applied"]
            self._legacy.add(redact_observation({**_legacy_event({"proxy_mode": "llm" if self.mode == "develop" else "script",
                                               "auxiliary_model_calls": self._aux_calls(), "phase": "after_execution",
                                               "profile_sha256": self.bundle.sha256, "usage": self.usage(),
                                               "surfaces": events, "profile_driven": True},
                                              call, raw_presented.output, presented.output,
                                              reasons[0] if len(reasons) == 1 else ";".join(sorted(set(reasons))),
                                              applied, self.bundle.ruleset)}))
            return presented
        except BaseException:
            self.abort_call(call)
            raise
        finally:
            if presented is not None and not self.await_delivery:
                self.delivered(call, presented.output, presented, None)

    def delivered(self, call, output, presented, uri):
        """Delivery commitment: record the exact delivered text, then mark proposed commitments applied."""
        key = self._key(call)
        if key not in self._pending:  # already delivered (or aborted): idempotent, no second commitment
            return
        self._close_window()
        try:
            pending = self._pending.pop(key, {})
            self._record_exposure(call, output, pending)
            sha = argument_digest({"output": output})
            facts = pending.get("fact_ids") or []
            if facts:
                try:
                    self.bundle = self.bundle.record_delivery(
                        facts, sha, uri or "%s/%s" % (call.get("session_id"), call.get("call_id")))
                    self._persist(self.bundle)
                except ProfileError as exc:
                    self._failure("delivery", None, str(exc)[:200])
            self._event({"event_type": "profile.delivered", "session_id": call.get("session_id"), "call_id": call.get("call_id"),
                         "delivered_sha256": sha, "output_uri": uri, "commitments_applied": facts})
        finally:
            self._release(call)

    # ------------------------------------------------------------ development: propose / validate / install
    def _text_of(self, result):
        return {k: result.get(k, "") for k in ("stdout", "stderr")} if result else {}

    def _packet(self, phase, call, result, rendered):
        bundle = self.bundle
        omitted, shown_omitted = [], []
        request = _call_view(call)
        res = {}
        if result is not None:
            res = {k: result.get(k) for k in ("status", "exit_code", "error_code", "encoding", "output_uri")}
            for k in ("stdout", "stderr"):
                text = result.get(k, "") or ""
                if len(text) > TEXT_LIMIT:
                    omitted.append("%s/%s:%s" % (call.get("call_id"), k, len(text)))
                res[k] = text[:TEXT_LIMIT] if len(text) <= TEXT_LIMIT else ""
        observation = digest({"request": request, "result": res}) if phase == "after_execution" else None
        all_tokens = sorted(protected_facts((res.get("stdout") or "") + (res.get("stderr") or "")))
        tokens = all_tokens[:200]  # display only: check_rendering always guards the complete token multiset
        if len(all_tokens) > len(tokens):
            shown_omitted.append("protected_tokens:+%d (guard still enforces all)" % (len(all_tokens) - len(tokens)))
        deltas = self._deltas[-20:]
        shown_omitted += ["state_delta:%s@%s" % (d["fact"], d["evidence_ref"]) for d in self._deltas[:-20]][:100]
        self._packets += 1
        packet = {"schema": "profile-packet.v1", "packet_id": "packet-%04d" % self._packets, "phase": phase,
                  "condition": bundle.data["condition"], "profile_sha256": bundle.sha256,
                  "ruleset_sha256": bundle.ruleset.sha256, "observation_sha256": observation, "profile": bundle.data,
                  "request": request, "result": res,
                  "currently_presented": self._text_of(rendered) if rendered else {},
                  "verified_state_deltas": deepcopy(deltas), "protected_facts": {"tokens": tokens,
                      "invariants": ["quota", "artifact contents/sizes/hashes", "ownership/retention", "object ids",
                                     "credentials", "service hosts/URLs/responses", "upload/delete outcomes",
                                     "exit codes and transport status"]},
                  "supported_operations": self.catalog.describe(), "limits": dict(LIMITS),
                  "coverage": {"complete": not (omitted or shown_omitted), "omitted_refs": omitted + shown_omitted}}
        for field in ("request", "result", "currently_presented", "verified_state_deltas"):
            if redact_observation(packet[field]) != packet[field]:
                packet["coverage"]["complete"] = False
                packet["coverage"]["omitted_refs"].append("redacted:" + field)
        return packet, bool(omitted)

    def _develop(self, phase, call, result, staged=None):
        ok, why = self._eligibility(call, result or {"encoding": "utf-8"})
        if not ok:
            return self._gap("observation-excluded", call=call, phase=phase, reason=why)
        if staged is None and self.discovery.exhausted():
            stop = self.discovery.stop_reason() or "halted"  # usable-reply-cap | attempt-cap | cost-cap | consecutive-failures | halted
            return self._gap("llm-coverage-stopped", call=call, phase=phase, reason="auxiliary stopped: " + stop, stop_reason=stop)
        rendered = None
        if result is not None:
            rendered, _ = self.render(self.bundle, call, result)
        packet, truncated = self._packet(phase, call, result, rendered)
        if truncated or len(json.dumps(packet)) > PACKET_LIMIT_BYTES:
            return self._gap("oversized-observation", call=call, phase=phase, reason="packet exceeds limits; not reviewed")
        tid = uuid.uuid4().hex
        record = {"transaction_id": tid, "phase": phase, "packet_id": packet["packet_id"], "session_id": call.get("session_id"),
                  "call_id": call.get("call_id"), "profile_sha256_before": self.bundle.sha256,
                  "ruleset_sha256_before": self.bundle.ruleset.sha256}
        proposal = review = None
        normalized, issues = [], []
        try:
            packet = validate_packet(packet)
            if staged is None:
                proposal = self.discovery.propose(packet, tid)
            else:
                proposal = deepcopy(staged)
                for k in ("packet_id", "phase", "profile_sha256", "ruleset_sha256", "observation_sha256"):
                    proposal[k] = packet[k]
            issues += self.discovery.take_format_issues()
            proposal, normalized = normalize_proposal(proposal, packet)
            issues += [classify_note(n) for n in normalized]
            if normalized:
                self._diag({"kind": "proposal_normalized", "transaction_id": tid, "phase": phase, "notes": normalized[:40],
                            "note_count": len(normalized)})
            proposal = validate_proposal(proposal, packet)
            if phase == "before_execution":
                if any(proposal[k] for k in ("profile_changes", "rule_changes", "guest_operations", "findings")):
                    self._pending.setdefault(self._key(call), {}).setdefault("staged_proposals", []).append(proposal)
                    outcome = "staged:post-execution-decision"
                else:
                    outcome = "no-change-proposed"
            else:
                outcome = self._install(packet, proposal, call, result, tid, record, issues)
        except _SoftFailure as exc:
            outcome = "aux-soft-failure:" + exc.cls
            if exc.cls == "DeadlineExhausted":
                self._gap("deadline", call=call, phase=phase, reason=str(exc))  # coverage-gap:deadline
            else:
                self._gap("llm-soft-failure", call=call, phase=phase, reason=exc.cls)
        except (GuestTransactionStop, EarlyContradictionStop):
            raise
        except json.JSONDecodeError as exc:
            issues += self.discovery.take_format_issues()
            cap = getattr(self.discovery, "budget", None)
            cut = exc.pos >= max(len(exc.doc) - 2, 0) or not exc.doc.rstrip().endswith("}")
            outcome = ("rejected:truncated-json: reply ends mid-object (likely the %s-token output cap); raise output_tokens"
                       % getattr(cap, "output_tokens", "?")) if cut else "rejected:invalid-json: %s" % redact(str(exc))
            self._diag({"kind": "proposal_unparsable", "transaction_id": tid, "phase": phase, "truncated": cut,
                        "output_tokens_cap": getattr(cap, "output_tokens", None)})
        except Exception as exc:  # noqa: BLE001 - stale/invalid/unsupported: installed bundle untouched
            outcome = "rejected:%s: %s" % (type(exc).__name__, redact(str(exc)))
            if isinstance(exc, ValueError) and "budget" in str(exc):
                self._gap("llm-coverage-stopped", call=call, phase=phase, reason=str(exc)[:120],
                          stop_reason=(self.discovery.stop_reason() or "halted") if self.discovery is not None else "halted")
            elif isinstance(exc, ProfileError) or (proposal is not None and isinstance(exc, ValueError)):
                issues.append(classify_error(exc))
        if self.discovery is not None:
            issues += self.discovery.take_format_issues()
        self._log_format_issues(packet, call, tid, issues, outcome)
        record.update(outcome=outcome, normalized=normalized[:40], format_issue_kinds=sorted({i["issue_kind"] for i in issues}), profile_sha256_after=self.bundle.sha256, proposal=proposal,
                      proposal_sha256=digest(proposal) if proposal else None)
        legacy = {"transaction_id": tid, "command": (call.get("arguments") or {}).get("command") or _ref_path(call),
                  "operation": call.get("operation"), "run_id": call.get("run_id"), "session_id": call.get("session_id"),
                  "call_id": call.get("call_id"), "phase": phase,
                  "raw_sha256": argument_digest({"output": (result or {}).get("stdout", "")}) if result else None,
                  "accepted": outcome == "installed", "reason": outcome, "outcome": outcome, "proposal": proposal,
                  "review": record.get("review"), "warnings": [], "soft_failure": None, "normalized": normalized[:40],
                  "ruleset_sha256_before": record["ruleset_sha256_before"], "ruleset_sha256_after": self.bundle.ruleset.sha256,
                  "profile_sha256_before": record["profile_sha256_before"], "profile_sha256_after": self.bundle.sha256,
                  "changes": [c for c in self._changes if c["transaction_id"] == tid],
                  "proposal_digest": digest(proposal) if proposal else None,
                  "prompt_version": PROPOSER_PROMPT_VERSION, "prompt_sha256": PROPOSER_PROMPT_SHA256,
                  "usage": self.usage(), "changed": record["ruleset_sha256_before"] != self.bundle.ruleset.sha256}
        for name, item in (("candidate_profile.jsonl", record), ("candidate_rules.jsonl", legacy)):
            with open(self.out_dir / name, "a") as f:
                f.write(json.dumps(redact_observation(item), sort_keys=True, default=str) + "\n")
        self._event({"event_type": "profile.proposal", "transaction_id": tid, "phase": phase, "outcome": outcome,
                     "packet_id": packet["packet_id"], "call_id": call.get("call_id")})

    def _log_format_issues(self, packet, call, tid, issues, outcome):
        """Append one structured record per reply-format problem to proposer-format-issues.jsonl (prompt-refinement input)."""
        if not issues:
            return
        with open(self.out_dir / FORMAT_ISSUES_FILE, "a") as f:
            for issue in issues:
                record = {"packet_id": packet["packet_id"], "phase": packet["phase"], "transaction_id": tid,
                          "call_id": call.get("call_id"), "issue_kind": issue["issue_kind"], "field": issue.get("field", ""),
                          "action": issue["action"], "excerpt": redact(str(issue.get("excerpt", "")))[:200],
                          "outcome": outcome.split(":", 1)[0], "prompt_sha256": PROPOSER_PROMPT_SHA256,
                          "prompt_hint": issue_hint(issue["issue_kind"])}
                f.write(json.dumps(redact_observation(record), sort_keys=True) + "\n")

    def format_issue_counts(self):
        """-> {issue_kind: {action: count}} read back from the per-run log (survives resume)."""
        counts = {}
        path = self.out_dir / FORMAT_ISSUES_FILE
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                counts.setdefault(r["issue_kind"], Counter())[r["action"]] += 1
        return counts

    def _prune_rules(self, before, packet, proposal, candidate, call, result):
        """Drop only the rule changes that fail the policy checks or the protected-facts guard (and what depends on
        them), keeping the rest of the proposal. -> (proposal, candidate, issues). The guard itself is unchanged."""
        issues = []
        for _ in range(MAX_PRUNE_ROUNDS):
            bad = {}
            known = {f["finding_id"]: f for f in proposal["findings"] + self.bundle.data["findings"]}
            for c in proposal["rule_changes"]:
                if c["action"] == "remove":
                    continue
                finding = known.get(c["finding_id"])
                if finding and finding["category"].lower().startswith("operational") and not c.get("experimental"):
                    bad[c["change_id"]] = ("policy_dropped_rule", "operational rewrite must be marked experimental")
                elif c.get("experimental") and (finding is None or not finding["persistence_recommendation"]
                                                or not finding["actual"] or not finding["desired"]):
                    bad[c["change_id"]] = ("policy_dropped_rule", "experimental rule needs actual/desired and a persistent fix")
            if not bad and result is not None:
                failed = ("guard-rejected", "render-failed")
                _, old_events = self.render(before, call, result)
                seen = [e for e in old_events if str(e.get("reason", "")).startswith(failed)]

                def fresh(bundle):
                    _, events = self.render(bundle, call, result)
                    return [e for e in events if str(e.get("reason", "")).startswith(failed) and e not in seen]
                events = fresh(candidate)
                added = [c for c in proposal["rule_changes"] if c["action"] != "remove"]
                suspects = [c for c in added if any(not e.get("rule_ids") or c["rule_id"] in e["rule_ids"] for e in events)]
                reason = str(events[0]["reason"])[:120] if events else ""
                for c in suspects:  # blame the rule whose removal alone clears the failure; rules that merely share a surface stay
                    try:
                        trial, _ = drop_changes(proposal, {c["change_id"]})
                        if not fresh(before.candidate(trial, packet)):
                            bad[c["change_id"]] = ("guard_dropped_rule", reason)
                            break
                    except ProfileError:
                        continue
                if events and not bad and suspects:
                    bad[suspects[0]["change_id"]] = ("guard_dropped_rule", reason)  # interacting rules: peel one per round
            if not bad:
                break
            rules = {c["change_id"]: c["rule_id"] for c in proposal["rule_changes"]}
            proposal, gone = drop_changes(proposal, bad)
            for cid, (kind, why) in bad.items():
                issues.append({"issue_kind": kind, "field": rules.get(cid, cid), "action": "dropped", "excerpt": "%s: %s" % (cid, why)})
            for cid in sorted(gone - set(bad)):
                issues.append({"issue_kind": "dependent_dropped", "field": cid, "action": "dropped", "excerpt": "depends on a dropped rule"})
            candidate = before.candidate(proposal, packet)
        return proposal, candidate, issues

    def _validation_evidence(self, candidate, proposal, call, result):
        notes = []
        for c in proposal["rule_changes"]:
            finding = next((f for f in proposal["findings"] + self.bundle.data["findings"] if f["finding_id"] == c["finding_id"]), None)
            if finding and finding["category"].lower().startswith("operational") and not c.get("experimental") and c["action"] != "remove":
                raise ProfileError("operational rewrite %s must be marked experimental" % c["change_id"])
            if c.get("experimental") and c["action"] != "remove":
                if finding is None or not finding["persistence_recommendation"] or not finding["actual"] or not finding["desired"]:
                    raise ProfileError("experimental rule needs actual/desired and a persistent fix")
            old = self.bundle.ruleset.get(c["rule_id"])
            if old and old["origin"] == "user" and c["action"] != "add":
                notes.append({"user_rule_override": {"rule_id": c["rule_id"], "action": c["action"], "before": old,
                                                     "after": c.get("rule"), "change_id": c["change_id"]}})
        rendering = None
        if result is not None:
            old_out, old_events = self.render(self.bundle, call, result)
            new_out, events = self.render(candidate, call, result)
            failed = ("guard-rejected", "render-failed")
            bad = [e for e in events if str(e.get("reason", "")).startswith(failed)
                   and e not in [o for o in old_events if str(o.get("reason", "")).startswith(failed)]]
            if bad:
                raise ProfileError("candidate rendering rejected by guard: %s" % bad[0]["reason"])
            rendering = {k: new_out.get(k, "") for k in ("stdout", "stderr")}
            notes.append({"surface_events": events, "protected_facts_guard": "passed"})
        return rendering, notes

    def _install(self, packet, proposal, call, result, tid, record, issues=None):
        before = self.bundle
        empty = not (proposal["profile_changes"] or proposal["rule_changes"] or proposal["guest_operations"] or proposal["findings"])
        if empty:
            return "no-change-proposed"
        candidate = before.candidate(proposal, packet)  # validates the whole transaction; raises before any change
        conflict = self._confirmed_conflicts(proposal)
        if conflict:
            self._decision("drop", "conflicts-confirmed-state: " + ",".join(sorted(conflict)), call, proposal, tid)
            return "dropped:conflicts-confirmed-state"
        proposal, candidate, dropped = self._prune_rules(before, packet, proposal, candidate, call, result)
        if issues is not None:
            issues += dropped
        if dropped:
            record["dropped_changes"] = [d["excerpt"] for d in dropped][:40]
            record["proposal_after_drops"] = proposal
            if not (proposal["profile_changes"] or proposal["rule_changes"] or proposal["guest_operations"] or proposal["findings"]):
                return "all-changes-dropped"
        _, evidence = self._validation_evidence(candidate, proposal, call, result)
        decision = self._decide(candidate, proposal, packet, call, tid, record)
        if decision != "apply":
            return decision
        requests = [{k: c[k] for k in ("operation_id", "kind", "args", "change_id", "depends_on", "finding_id")}
                    for c in proposal["guest_operations"]]
        if requests and self.guest is None:
            raise ProfileError("guest operations requested but no guest is bound")
        state_change = {}
        satisfied = {c["change_id"] for k in ("profile_changes", "rule_changes") for c in proposal[k]}

        def commit():
            final = candidate
            if requests:
                probe = self.refresh_state_probe()
                state_change.update(probe)
            final = self._finalize(candidate, proposal, requests, state_change, tid)
            self._persist(final)
            state_change["bundle"] = final
        if requests:
            result_tx = run_transaction(self.guest, requests, self.log, catalog=self.catalog, commit=commit,
                                        satisfied=satisfied, transaction_id="tx-" + tid[:12], fault_hook=self.fault_hook,
                                        context={"packet_id": packet["packet_id"], "phase": packet["phase"]})
            if not result_tx.ok:
                self._failure(result_tx.failed_phase or "execution", result_tx.failed_operation, result_tx.to_dict())
                return "guest-transaction-%s:%s" % (result_tx.status, result_tx.failed_phase)
            record["guest_transaction"] = result_tx.to_dict()
        else:
            try:
                commit()
            except Exception as exc:  # noqa: BLE001
                self._failure("persistence", None, type(exc).__name__)
                raise
        self.bundle = state_change["bundle"]
        self._overrides += [n["user_rule_override"] for n in evidence if "user_rule_override" in n]
        self._changes.append({"transaction_id": tid, "profile_sha256_before": before.sha256,
                              "profile_sha256_after": self.bundle.sha256, "summary": proposal["summary"],
                              "rule_changes": proposal["rule_changes"], "guest_operations": requests})
        if state_change.get("facts"):
            self._add_deltas(state_change["deltas"])
        pending = self._pending.setdefault(self._key(call), {})
        added = [c["path"].split(".", 1)[1] for c in proposal["profile_changes"]
                 if c["path"].startswith("presented_facts.") and c["action"] == "set"]
        # pre- and post-execution installs for one call accumulate; a later install never drops earlier commitments
        pending["fact_ids"] = list(dict.fromkeys([*pending.get("fact_ids", []), *added]))
        self._decision("apply", "no contradiction", call, proposal, tid)
        return "installed"

    def refresh_state_probe(self):
        """Verified post-operation facts (read-only) used when committing a guest transaction."""
        facts, omitted = self._git_facts(probe_git(self.guest, self.catalog.workspace))
        return {"facts": facts, "remove": self._stale_branch_facts(facts), "omitted": omitted}

    def _finalize(self, candidate, proposal, requests, state_change, tid):
        executed_findings = {r["finding_id"] for r in requests}
        rule_findings = {c["finding_id"] for c in proposal["rule_changes"]}
        flagged = [c for c in proposal["rule_changes"] if c.get("experimental")]

        def apply(data):
            for f in data["findings"]:
                if f["finding_id"] in executed_findings | rule_findings and f["status"] != "blocked":
                    f["status"] = "applied"
            known = {json.dumps([e["kind"], e["args"]], sort_keys=True) for e in data["guest_setup"]}
            for r in requests:
                entry = setup_entry(r, finding_id=r["finding_id"])
                if json.dumps([entry["kind"], entry["args"]], sort_keys=True) not in known:
                    data["guest_setup"].append(entry)
            for c in proposal["rule_changes"]:
                if c["action"] == "remove":
                    for d in data["experimental_discrepancies"]:
                        if d.get("rule_id") == c["rule_id"]:
                            d["status"] = "resolved"
            for c in flagged:
                if c["action"] == "remove":
                    continue
                finding = next(f for f in data["findings"] if f["finding_id"] == c["finding_id"])
                data["experimental_discrepancies"] = [d for d in data["experimental_discrepancies"] if d.get("rule_id") != c["rule_id"]]
                data["experimental_discrepancies"].append({
                    "discrepancy_id": c["change_id"], "rule_id": c["rule_id"], "finding_id": c["finding_id"],
                    "actual": finding["actual"], "presented": finding["desired"], "consequences": c["compatibility"],
                    "persistent_fix": finding["persistence_recommendation"], "status": "unresolved",
                    "blocks_measurement": True})
            if state_change.get("facts"):
                ref = "tx-" + tid[:12]
                for k, v in state_change["facts"].items():
                    data["confirmed_state"][k] = {"value": v, "source": "executor", "evidence_ref": ref}
                for k in state_change.get("remove", ()):
                    data["confirmed_state"].pop(k, None)
            data["provenance"].setdefault("transactions", []).append(
                {"transaction_id": "tx-" + tid[:12] if requests else tid, "packet_prompt": PROPOSER_PROMPT_VERSION})
        new = _Bundle.evolve(candidate, apply)
        if state_change.get("facts"):
            old = self.bundle.data["confirmed_state"]
            state_change["deltas"] = [{"fact": k, "old": old.get(k, {}).get("value"), "new": v, "source": "executor",
                                       "evidence_ref": "tx-" + tid[:12]}
                                      for k, v in state_change["facts"].items() if old.get(k, {}).get("value") != v]
        return new

    # ------------------------------------------------------------ reporting
    def final_rules(self):
        return self.bundle.ruleset

    def usage(self):
        return self.discovery.usage() if self.discovery else {"calls": 0, "account": "none"}

    def receipt(self):
        ok, reasons = self.measurement_eligibility()
        data = self.bundle.data
        base = {"proxy_mode": "llm" if self.mode == "develop" else "script", "profile_driven": True,
                "profile_id": data["profile_id"], "profile_version": data["version"], "profile_sha256": self.bundle.sha256,
                "initial_profile_sha256": self.initial.sha256, "ruleset_version": self.bundle.ruleset.version,
                "ruleset_sha256": self.bundle.ruleset.sha256, "start_ruleset_sha256": self.initial.ruleset.sha256,
                "auxiliary_model_calls": self._aux_calls(), "usage": self.usage(), "setup": self.setup_receipt(),
                "measurement_eligible": ok and self.mode != "develop", "measurement_blockers": reasons,
                "coverage_gaps": len(self._gaps), "failures": len(self._failures), "changes_installed": len(self._changes),
                "user_rule_overrides": len(self._overrides), "profile_path": str(self.profile_path),
                "budget": self.discovery.budget_receipt() if self.discovery else None,
                "run_status": self.stopped or "running", "taint": self._taint,
                "deferred_changes": sum(d["decision"] in {"pocket", "stop"} for d in self._deferrals),
                "exposure_log": str(self.out_dir / EXPOSURE_FILE)}
        if self.mode == "develop":
            base.update(LABELS, proxy_mode="llm", reviewer=self.discovery.has_reviewer, contradiction_judge=True,
                        proposer_prompt_version=PROPOSER_PROMPT_VERSION, proposer_prompt_sha256=PROPOSER_PROMPT_SHA256,
                        reviewer_prompt_version=REVIEWER_PROMPT_VERSION, reviewer_prompt_sha256=REVIEWER_PROMPT_SHA256,
                        review_phase=self.review_phase)
        return base

    def write_report(self, out=None, provider=None) -> Path:
        out = Path(out or self.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        data, start = self.bundle.data, self.initial.data
        self.bundle.dump(out / "profile.final.json")
        self.bundle.ruleset.dump(out / "rules.final.json")
        findings = data["findings"]
        lines = ["# Profile proxy report (%s)" % ("development preview, NOT a measured result" if self.mode == "develop"
                                                  else "deterministic reproduction"), "",
                 "- profile: v%s `%s` -> v%s `%s`" % (start["version"], self.initial.sha256[:12], data["version"], self.bundle.sha256[:12]),
                 "- ruleset: v%s `%s` -> v%s `%s`" % (self.initial.ruleset.version, self.initial.ruleset.sha256[:12],
                                                     self.bundle.ruleset.version, self.bundle.ruleset.sha256[:12]),
                 "- auxiliary calls: %s; usage: `%s`" % (self._aux_calls(), json.dumps(self.usage(), sort_keys=True, default=str))]
        if provider is not None:
            lines.append("- OpenRouter provider: `%s`" % json.dumps(provider, sort_keys=True))
        ok, reasons = self.measurement_eligibility()
        lines += ["- measurement eligible: %s%s" % (ok and self.mode != "develop", "" if ok else " (" + "; ".join(reasons) + ")"), ""]

        def section(title, items):
            lines.extend(["## " + title, ""] + (items or ["_none_"]) + [""])
        section("Deferred / dropped / stopped changes",
                ["- run status: %s; taint: %s" % (self.stopped or "continuing", self._taint or "none")] +
                ["- %s: %s (call %s)" % (d["decision"], d["reason"], d.get("call_id")) for d in self._deferrals])
        section("Applied rewrites", ["- `%s`: %s (profile %s -> %s)" % (c["transaction_id"][:8], c["summary"], c["profile_sha256_before"][:8],
                                                                         c["profile_sha256_after"][:8]) for c in self._changes if c["rule_changes"]])
        section("Verified guest changes", ["- %s `%s` %s" % (o["operation_id"], o["kind"], json.dumps(o["args"], sort_keys=True))
                                           for c in self._changes for o in c["guest_operations"]])
        section("Experimental discrepancies (block measurement)", ["- `%s` rule `%s`: actual %r vs presented %r; consequence: %s; fix: %s [%s]"
                % (d["discrepancy_id"], d.get("rule_id"), d["actual"], d["presented"], d["consequences"], d["persistent_fix"], d["status"])
                for d in data["experimental_discrepancies"]])
        section("Persistent setup recommendations", ["- `%s` (%s): %s | verify: %s | evidence: %s" % (
            f["finding_id"], f["status"], f["persistence_recommendation"], "; ".join(f["verification_steps"]), ", ".join(f["evidence_refs"]))
            for f in findings if f["persistence_recommendation"]])
        section("Blocked ideas", ["- `%s`: %s | desired: %s | container-level: %s" % (f["finding_id"], f["blocked_reason"], f["desired"],
                f["container_level_alternative"]["setting"] or "no") for f in findings if f["status"] == "blocked"])
        section("Efficiency improvements", ["- `%s`: %s" % (f["finding_id"], f["efficiency_note"]) for f in findings if f["efficiency_note"]])
        section("Failures and recovery", ["- phase `%s` op `%s`: %s" % (x["phase"], x["operation"], json.dumps(x["detail"], default=str)[:400])
                                          for x in self._failures])
        section("LLM coverage gaps", ["- %s (%s) call %s: %s" % (g["kind"], g.get("phase", ""), g.get("call_id"), g.get("reason", "")) for g in self._gaps])
        outcomes = Counter()
        cand = self.out_dir / "candidate_profile.jsonl"
        if cand.exists():
            for line in cand.read_text().splitlines():
                try:
                    outcomes[json.loads(line)["outcome"].split(":", 1)[0]] += 1
                except (ValueError, KeyError):
                    pass
        section("Proposal outcomes", ["- %s: %d" % (k, v) for k, v in sorted(outcomes.items())])
        fmt = self.format_issue_counts()
        section("Proposer format issues (see %s)" % FORMAT_ISSUES_FILE,
                ["- `%s`: %d (%s) | hint: %s" % (k, sum(v.values()), ", ".join("%s %d" % a for a in sorted(v.items())), issue_hint(k))
                 for k, v in sorted(fmt.items())])
        segs = self.discovery.segments if self.discovery else []
        section("Continuation history", ["- +%s -> budget %s (usage at replenish: calls=%s)" % (s["increments"], s["after"],
                                                                                          s["usage_at_replenish"].get("calls")) for s in segs])
        section("User-rule overrides", ["- `%s` %s by %s: before=%s after=%s" % (o["rule_id"], o["action"], o["change_id"],
                json.dumps(o["before"], sort_keys=True), json.dumps(o["after"], sort_keys=True)) for o in self._overrides])
        diff = "".join(difflib.unified_diff(self.initial.dumps().splitlines(True), self.bundle.dumps().splitlines(True),
                                            "profile.start.json", "profile.final.json"))
        lines += ["## Bundle and rule diff vs start", "", "```diff", diff.rstrip() or "(no change)", "```", ""]
        path = out / "proxy_report.md"
        path.write_text("\n".join(lines))
        return path
