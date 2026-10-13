"""Profile-driven presentation proxy (docs/live-proxy-spec.md sections 5-7).

Runtime API (what the run wiring uses)::

    pres = ProfilePresentation(out_dir, bundle, proposer=None, reviewer=None, budget=None, profile_out=None,
                               condition=None, await_delivery=False)
    # proposer None  -> deterministic mode: zero auxiliary calls, applies saved setup + explicit rules.
    # proposer given -> development mode (LiveBudget required); reviewer optional, distinct model identity.
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
import difflib
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
from .guest_operations import (Catalog, GuestTransactionStop, OperationError, SetupReceipt, TransactionLog, apply_setup,
                               probe_git, probe_identity, recover, run_transaction, setup_entry)
from .host_adapter import PassthroughPresenter, Presented
from .live import LABELS, LiveBudget, LiveDiscovery, _Events, _SoftFailure, _event as _legacy_event, redact
from .presentation import FrozenPresentation
from .profile_prompts import (PROPOSER_PROMPT_SHA256, PROPOSER_PROMPT_VERSION, PROPOSER_SYSTEM_PROMPT,
                              REVIEWER_PROMPT_SHA256, REVIEWER_PROMPT_VERSION, REVIEWER_SYSTEM_PROMPT)
from .profiles import (PROPOSAL_FIELDS, PROPOSAL_SCHEMA, REVIEW_FIELDS, REVIEW_SCHEMA, ProfileBundle, ProfileError,
                       digest, redact as redact_observation, validate_packet, validate_proposal, validate_review)
from .rules import RuleError, check_surface, protected_facts

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


class _Bundle:
    """Small helper: harness-owned evolutions of a bundle (version+1, parent digest), always re-validated."""

    @staticmethod
    def evolve(bundle: ProfileBundle, fn) -> ProfileBundle:
        data = bundle.data
        fn(data)
        data["version"] += 1
        data["parent_sha256"] = bundle.sha256
        return ProfileBundle.from_dict(data)


class _Timed:
    """Accumulates wall time of every accounted call exactly once (LiveDiscovery only does so inside discover())."""

    def _call(self, role, payload, transaction):
        started = time.monotonic()
        try:
            return super()._call(role, payload, transaction)
        finally:
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


class _ReviewerAccount(_Timed, LiveDiscovery):
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


def _positive(increments: dict) -> dict:
    """-> ``{account: {cost_usd?, pairs?}}``; flat input is the proposer alias. Strictly positive, finite, typed."""
    from .proxy_control import _allowances
    out = _allowances(increments)
    return out


class ProfileDiscovery(_Timed, LiveDiscovery):
    """Budgeted transport for profile packets: system prompt + packet user message (client.system_prompt).
    Reuses LiveDiscovery._call (reservations, receipts, halting, soft failures). The proposer and the
    reviewer have separate budgets (``budget`` / ``reviewer_budget``); ``usage()`` aggregates both."""

    def __init__(self, *, proposer, reviewer=None, recorder=None, budget: LiveBudget, reviewer_budget: LiveBudget | None = None):
        if (reviewer is None) != (reviewer_budget is None):
            raise ValueError("an enabled reviewer needs its own explicit reviewer_budget (and vice versa)")

        def with_system(client, prompt):
            clone = copy(client)
            clone.system_prompt = prompt
            return clone
        reviewer_client = with_system(reviewer, REVIEWER_SYSTEM_PROMPT) if reviewer is not None else None
        super().__init__(proposer=with_system(proposer, PROPOSER_SYSTEM_PROMPT), reviewer=reviewer_client,
                         recorder=recorder, budget=budget, retry_length=False)
        self.recorder = _RedactingRecorder(self.recorder)
        self.account = (_ReviewerAccount(client=reviewer_client, recorder=self.recorder, budget=reviewer_budget,
                                         format_instructions=self._format_instructions,
                                         response_format=self._response_format)
                        if reviewer is not None else None)
        self.has_reviewer = reviewer is not None
        self.original_budget, self.original_reviewer_budget = budget, reviewer_budget
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
            example = {
                "schema": PROPOSAL_SCHEMA,
                "packet_id": payload["packet_id"],
                "phase": payload["phase"],
                "profile_sha256": payload["profile_sha256"],
                "ruleset_sha256": payload["ruleset_sha256"],
                "observation_sha256": payload["observation_sha256"],
                "summary": "No changes proposed.", "checked": ["reviewed the supplied packet"],
                "profile_changes": [], "rule_changes": [], "guest_operations": [], "findings": [],
            }
            fields = sorted(PROPOSAL_FIELDS)
            return ("RESPONSE CONTRACT (strict): Return exactly one JSON object with exactly these required top-level "
                    "fields: " + json.dumps(fields) + ". The `schema` value must be the literal " +
                    json.dumps(PROPOSAL_SCHEMA) + ". Echo the packet values shown in this valid no-op example; "
                    "arrays may be empty when no changes/findings are proposed. Do not omit fields or add fields.\n" +
                    json.dumps(example, ensure_ascii=True, separators=(",", ":")))
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

    def reviewer_exhausted(self) -> bool:
        return self.account is not None and self.account.exhausted()

    def usage(self):
        mine = super().usage()
        if self.account is None:
            return mine
        other = self.account.usage()
        total = dict(mine)
        for key in ("calls", "reserved_cost_usd", "reported_cost_usd", "unaccounted_calls", "aux_seconds", "aux_soft_failures"):
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
                  "unaccounted_calls": "_unaccounted", "aux_seconds": "aux_seconds",
                  "aux_soft_failures": "soft_failures", "consecutive_failures": "consecutive_failures"}
        for name, account, budget_data, original in targets:
            saved = accounts.get(name)
            if not isinstance(saved, dict):
                raise ValueError("receipt lacks account usage")
            values = {}
            for key, attr in fields.items():
                value = saved.get(key, 0)
                integer = key in {"calls", "unaccounted_calls", "aux_soft_failures", "consecutive_failures"}
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
                 check_identity=True, fault_hook=None, reviewer_budget: LiveBudget | None = None):
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
        if reviewer is None and reviewer_budget is not None:
            raise ValueError("reviewer_budget given without a reviewer")
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
        self._recorder_buffer = []
        self._recorder = None
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
        receipt = self.apply_saved_setup()
        self.refresh_state(reason="prepare")
        self.verify_identity()
        episode.resources["presentation"] = {**episode.resources["presentation"], **self.provenance,
                                             "setup": receipt.to_dict(), "measurement_blockers": self.measurement_eligibility()[1]}
        return expected

    def apply_saved_setup(self) -> SetupReceipt:
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
                    if f["status"] in ("unresolved", "blocked") and f["category"].lower().startswith("operational")]
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
        current = self.state().get("git_head_branch")
        if probe is None:
            self._gap("unknown-state-transition", call=call, reason="authoritative scope could not be probed")
        elif previous is not None and previous != current:
            self._set_state({"git_target_head_branch": current}, "target", ref)

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
        try:
            if self.control is not None:
                try:
                    applied = self.control.apply_pending()
                    if applied is not None:
                        self._event({"event_type": "profile.replenished", "segment": applied,
                                     "note": "missed observations are not reviewed retroactively"})
                except Exception as exc:  # noqa: BLE001
                    self._failure("replenish", None, type(exc).__name__)
            if self.mode == "develop":
                self._develop("before_execution", call, None)
        except GuestTransactionStop as exc:
            self._stop_run(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - nothing here may block the original request
            self._failure("before_execution", None, type(exc).__name__)

    def abort_call(self, call):
        self._pending.pop(self._key(call), None)
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
        try:
            self._reconcile(call, result)
            if self.mode == "develop":
                try:
                    self._develop("after_execution", deepcopy(call), result)
                except GuestTransactionStop as exc:
                    self._stop_run(exc)
                    raise
            out, events = self.render(self.bundle, call, result)
            presented = self._baseline.present(call, out)
            raw_presented = self._baseline.present(call, result)
            self._pending.setdefault(key, {})["delivered_sha"] = None
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
        try:
            pending = self._pending.pop(key, {})
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

    def _develop(self, phase, call, result):
        ok, why = self._eligibility(call, result or {"encoding": "utf-8"})
        if not ok:
            return self._gap("observation-excluded", call=call, phase=phase, reason=why)
        if self.discovery.exhausted():
            return self._gap("llm-coverage-stopped", call=call, phase=phase, reason="auxiliary budget exhausted or halted")
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
        try:
            packet = validate_packet(packet)
            proposal = self.discovery.propose(packet, tid)
            proposal = validate_proposal(proposal, packet)
            outcome = self._install(packet, proposal, call, result, tid, record)
        except _SoftFailure as exc:
            outcome = "aux-soft-failure:" + exc.cls
            self._gap("llm-soft-failure", call=call, phase=phase, reason=exc.cls)
        except GuestTransactionStop:
            raise
        except Exception as exc:  # noqa: BLE001 - stale/invalid/unsupported: installed bundle untouched
            outcome = "rejected:%s: %s" % (type(exc).__name__, redact(str(exc)))
            if isinstance(exc, ValueError) and "budget" in str(exc):
                self._gap("llm-coverage-stopped", call=call, phase=phase, reason="budget")
        record.update(outcome=outcome, profile_sha256_after=self.bundle.sha256, proposal=proposal,
                      proposal_sha256=digest(proposal) if proposal else None)
        legacy = {"transaction_id": tid, "command": (call.get("arguments") or {}).get("command") or _ref_path(call),
                  "operation": call.get("operation"), "run_id": call.get("run_id"), "session_id": call.get("session_id"),
                  "call_id": call.get("call_id"), "phase": phase,
                  "raw_sha256": argument_digest({"output": (result or {}).get("stdout", "")}) if result else None,
                  "accepted": outcome == "installed", "reason": outcome, "outcome": outcome, "proposal": proposal,
                  "review": record.get("review"), "warnings": [], "soft_failure": None,
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
            bad = [e for e in events if str(e.get("reason", "")).startswith(("guard-rejected", "render-failed"))]
            if bad:
                raise ProfileError("candidate rendering rejected by guard: %s" % bad[0]["reason"])
            rendering = {k: new_out.get(k, "") for k in ("stdout", "stderr")}
            notes.append({"surface_events": events, "protected_facts_guard": "passed"})
        return rendering, notes

    def _install(self, packet, proposal, call, result, tid, record):
        before = self.bundle
        empty = not (proposal["profile_changes"] or proposal["rule_changes"] or proposal["guest_operations"] or proposal["findings"])
        if empty:
            return "no-change-proposed"
        candidate = before.candidate(proposal, packet)  # validates the whole transaction; raises before any change
        rendering, evidence = self._validation_evidence(candidate, proposal, call, result)
        if self.discovery.has_reviewer:
            if self.discovery.reviewer_exhausted():
                self._gap("review-skipped", call=call, phase=packet["phase"], reason="reviewer budget exhausted; proposal skipped")
                return "review-budget-exhausted"
            review = self.discovery.review({**packet, "proposal": proposal, "proposal_sha256": digest(proposal),
                                            "candidate_rendering": redact_observation(rendering),
                                            "validation_evidence": redact_observation(evidence)}, tid)
            record["review"] = review
            with open(self.out_dir / "review_events.jsonl", "a") as f:
                f.write(json.dumps(redact_observation({"transaction_id": tid, "verdict": review}), sort_keys=True, default=str) + "\n")
            review = validate_review(review, packet, proposal)
            if review["verdict"] != "approve":
                return "review-rejected"
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
                "budget": self.discovery.budget_receipt() if self.discovery else None}
        if self.mode == "develop":
            base.update(LABELS, proxy_mode="llm", reviewer=self.discovery.has_reviewer,
                        proposer_prompt_version=PROPOSER_PROMPT_VERSION, proposer_prompt_sha256=PROPOSER_PROMPT_SHA256,
                        reviewer_prompt_version=REVIEWER_PROMPT_VERSION, reviewer_prompt_sha256=REVIEWER_PROMPT_SHA256)
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
