"""Persistent, versioned profile bundles and proposal/review contracts for the live proxy.

Trust boundaries (docs/live-proxy-spec.md sections 3-4):
* Persisted profiles must be strict JSON with no secret-bearing fields or values.
* Packets are transport to the model: observation fields are redacted, never
  rejected, and numeric protected tokens are left untouched.
* Proposals and reviews are model output. They are validated in full before any
  state changes, and an invalid transaction never produces a new bundle.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from .guest_operations import DEFAULT_CATALOG, OperationError
from .rules import RULE_ID, SCHEMA as RULES_SCHEMA, RuleError, RuleSet, default_ruleset

SCHEMA = "artifact-proxy-profile.v1"
PACKET_SCHEMA = "profile-packet.v1"
PROPOSAL_SCHEMA = "profile-proposal.v1"
REVIEW_SCHEMA = "profile-review.v1"
VERSION = 1
CONDITIONS = ("natural-looking", "eval-looking")
PHASES = ("before_execution", "after_execution")
# Desired identity the model may change. uid/gid/os are invariants of the locked image.
IDENTITY_KEYS = ("user", "hostname", "home", "worktree", "branch")
ID_FIELDS = ("os", "user", "uid", "gid", "hostname", "home", "worktree", "display", "environment", "branch")
PROPOSAL_STATUSES = ("proposed", "unresolved", "blocked")
STORED_STATUSES = ("unresolved", "blocked", "applied", "resolved")
HARNESS_STATUSES = ("applied", "resolved")
CHANGE_LISTS = ("profile_changes", "rule_changes", "guest_operations")
HASH = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9_:-]{1,64}")  # no dots: dotted profile paths stay unambiguous
FACT = re.compile(r"[A-Za-z0-9_:-]+(?:\.[A-Za-z0-9_:-]+)*")  # confirmed fact keys, e.g. git.head_branch, git.branch:release
KIND = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,2}")  # e.g. git.create_branch
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?){0,7}")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_PROFILE_BYTES = 2_000_000
MAX_DEPTH = 64
MAX_ITEMS = 64
REDACTED = "[REDACTED]"
_GUESTS = json.loads((Path(__file__).parents[2] / "project/artifact-upload/instance.lock.json").read_text())["guest_profiles"]
_GUEST_BY_CONDITION = {x["condition"]: x for x in _GUESTS}

PACKET_FIELDS = {"schema", "packet_id", "phase", "condition", "profile_sha256", "ruleset_sha256", "observation_sha256",
                 "profile", "request", "result", "currently_presented", "verified_state_deltas", "protected_facts",
                 "supported_operations", "limits", "coverage"}
OBSERVATION_FIELDS = ("request", "result", "currently_presented", "verified_state_deltas")
PROPOSAL_FIELDS = {"schema", "packet_id", "phase", "profile_sha256", "ruleset_sha256", "observation_sha256", "summary",
                   "checked", "profile_changes", "rule_changes", "guest_operations", "findings"}
REVIEW_FIELDS = {"schema", "packet_id", "profile_sha256", "proposal_sha256", "verdict", "blocking_findings", "suggestions"}
CHANGE_FIELDS = {"change_id", "reason", "depends_on", "compatibility", "finding_id"}
FINDING_FIELDS = {"finding_id", "category", "evidence_refs", "actual", "desired", "status", "blocked_reason",
                  "container_level_alternative", "persistence_recommendation", "verification_steps", "efficiency_note"}
OP_FIELDS = {"operation_id", "kind", "args", "preconditions", "writable_scope", "rollback", "verification", "persistent_setup", "description"}
PROFILE_FIELDS = {"schema", "version", "parent_sha256", "profile_id", "condition", "preset_id", "desired_identity",
                  "guest_setup", "confirmed_state", "presented_facts", "ruleset", "findings",
                  "experimental_discrepancies", "provenance"}

# ---------------------------------------------------------------- secrets
# Key names are split into words so "output_tokens" is judged as words, not substrings.
# Numbers, booleans and null never count as secrets, so numeric budget fields stay valid.
_SECRET_WORDS = frozenset({"secret", "secrets", "password", "passwords", "passwd", "credential", "credentials",
                           "token", "tokens", "bearer", "authorization", "apikey", "privatekey", "accesskey"})
_SECRET_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key")})
_SECRET_PATTERNS = tuple(re.compile(p) for p in (
    r"sk-[A-Za-z0-9_-]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}",
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[=:]\s*(?!\d+\b)\S{6,}",
))


class ProfileError(ValueError):
    """Invalid profile, packet, or proposed transaction."""


def _plain(value, depth=0):
    """Accept only strict JSON data: no tuples, NaN/Infinity, non-string keys, or excessive nesting."""
    if depth > MAX_DEPTH:
        raise ProfileError("JSON nesting too deep")
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ProfileError("non-finite JSON number")
        return
    if type(value) is list:
        for item in value:
            _plain(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProfileError("JSON object keys must be strings")
            _plain(item, depth + 1)
        return
    raise ProfileError(f"non-JSON value of type {type(value).__name__}")


def canonical(value) -> str:
    _plain(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def loads_strict(text):
    """Parse JSON, rejecting duplicate keys and NaN/Infinity rather than silently accepting them."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ProfileError(f"duplicate JSON key {key!r}")
            out[key] = value
        return out

    def constant(name):
        raise ProfileError(f"non-finite JSON number {name}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except ProfileError:
        raise
    except ValueError as exc:
        raise ProfileError("invalid JSON") from exc


def _secret_key(key: str) -> bool:
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key).lower())
    return any(w in _SECRET_WORDS for w in words) or any(p in _SECRET_PAIRS for p in zip(words, words[1:]))


def _secrets(value, redact=False, under=False):
    """Reject (or, with redact=True, replace) secret-bearing strings. Non-string leaves pass unchanged.

    Strings under a secret-named key are secrets even without a recognisable shape.
    """
    if type(value) is dict:
        return {k: _secrets(v, redact, under or _secret_key(k)) for k, v in value.items()}
    if type(value) is list:
        return [_secrets(v, redact, under) for v in value]
    if type(value) is not str:
        return value
    if under and value:
        if not redact:
            raise ProfileError("contains a secret field with a non-numeric value")
        return REDACTED
    if not any(p.search(value) for p in _SECRET_PATTERNS):
        return value
    if not redact:
        raise ProfileError("contains a secret-looking value")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value):
    """Transport redaction for observations sent to the model."""
    return _secrets(value, redact=True)


# ---------------------------------------------------------------- field helpers
def _object(value, fields, what, optional=()):
    if type(value) is not dict:
        raise ProfileError(f"{what} must be an object")
    extra = set(value) - set(fields)
    missing = set(fields) - set(optional) - set(value)
    if extra or missing:
        raise ProfileError(f"invalid {what} fields: unknown {sorted(extra)} missing {sorted(missing)}")
    return value


def _text(value, what, maxlen, empty=False):
    if type(value) is not str or len(value) > maxlen or (not value and not empty) or "\x00" in value:
        raise ProfileError(f"invalid {what}")
    return value


def _ident(value, what):
    if type(value) is not str or not ID.fullmatch(value):
        raise ProfileError(f"invalid {what}")
    return value


def _rule_id(value, what="rule_id"):
    if type(value) is not str or not RULE_ID.fullmatch(value):
        raise ProfileError(f"invalid {what}")
    return value


def _flag(value, what):
    if type(value) is not bool:
        raise ProfileError(f"invalid {what}")
    return value


def _number(value, what, lo=0):
    if type(value) is not int or value < lo:
        raise ProfileError(f"invalid {what}")
    return value


def _texts(value, what, maxitems=MAX_ITEMS, maxlen=500):
    if type(value) is not list or len(value) > maxitems:
        raise ProfileError(f"invalid {what}")
    for item in value:
        _text(item, what, maxlen)
    return value


def _dicts(value, what):
    if type(value) is not list or len(value) > MAX_ITEMS or any(type(x) is not dict for x in value):
        raise ProfileError(f"invalid {what}")
    return value


def _abs_path(value, what):
    _text(value, what, 4096)
    if not value.startswith("/") or "\n" in value or "\r" in value or ".." in value.split("/"):
        raise ProfileError(f"invalid {what}")
    return value


def _identity_value(key, value):
    if key == "user":
        ok = type(value) is str and USER.fullmatch(value)
    elif key == "hostname":
        ok = type(value) is str and HOST.fullmatch(value)
    elif key == "branch":
        _text(value, "desired_identity.branch", 100)
        if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value) or ".." in value or "//" in value
                or value.endswith(("/", ".", ".lock")) or any(part.startswith(".") for part in value.split("/"))):
            raise ProfileError("invalid desired_identity.branch")
        return value
    else:
        return _abs_path(value, f"desired_identity.{key}")
    if not ok:
        raise ProfileError(f"invalid desired_identity.{key}")
    return value


def _confirmed_fact(key, entry):
    """Confirmed facts: ``source`` is required; ``verified``/``provenance`` are optional harness fields.

    A fact whose probe failed or is unknown is stored with verified=False and value None; it must
    never carry a known value.
    """
    if type(key) is not str or len(key) > 64 or not FACT.fullmatch(key):
        raise ProfileError("invalid confirmed fact id")
    _object(entry, {"value", "source", "evidence_ref", "verified", "provenance"}, "confirmed fact",
            optional=("verified", "provenance"))
    if entry["source"] not in ("preset", "executor", "target"):
        raise ProfileError("confirmed fact source must be preset, executor or target")
    _text(entry["evidence_ref"], "confirmed fact evidence_ref", 300, empty=entry["source"] == "preset")
    verified = entry.get("verified", True)
    _flag(verified, "confirmed fact verified")
    if "provenance" in entry:
        _text(entry["provenance"], "confirmed fact provenance", 200)
    if not verified and entry["value"] is not None:
        raise ProfileError("unverified confirmed fact must not carry a known value")
    _plain(entry["value"])
    return entry


def _pairs(value, what):
    if type(value) is not dict or len(value) > MAX_ITEMS:
        raise ProfileError(f"invalid {what}")
    for key, item in value.items():
        if type(key) is not str or not KEY.fullmatch(key):
            raise ProfileError(f"invalid {what} key")
        _text(item, f"{what} value", 1024, empty=True)
        if CONTROL.search(item):
            raise ProfileError(f"invalid {what} value")
    return value


# ---------------------------------------------------------------- profile paths and sub-records
def _profile_path(path):
    """Writable profile paths: exact allowlisted shapes only. Returns the path segments."""
    if type(path) is not str or not path or len(path) > 256:
        raise ProfileError("invalid profile path")
    parts = path.split(".")
    if not all(ID.fullmatch(part) for part in parts):
        raise ProfileError("invalid profile path")
    head, n = parts[0], len(parts)
    if head == "desired_identity" and n == 2 and parts[1] in IDENTITY_KEYS:
        return parts
    if head == "desired_identity" and n == 3 and parts[1] in ("display", "environment") and KEY.fullmatch(parts[2]):
        return parts
    if head in ("presented_facts", "findings") and n == 2:
        return parts
    raise ProfileError("profile path is harness-owned or not writable")


COMMIT_REQUIRED = ("rule_id", "presented", "status")
COMMIT_DELIVERY = ("delivered_sha256", "first_evidence_ref", "last_evidence_ref")


def _dep_ref(value):
    """A commitment dependency names a presented fact ID or a rule ID."""
    if type(value) is not str or not (FACT.fullmatch(value) or RULE_ID.fullmatch(value)):
        raise ProfileError("invalid commitment dependency")
    return value


def _commitment(value, proposal):
    """Presented-fact commitment. Model output may propose a status and dependencies only; the
    harness alone records delivery digests and first/last evidence refs."""
    optional = ("depends_on",)
    fields = set(COMMIT_REQUIRED) | {"depends_on"}
    if not proposal:
        optional = ("depends_on",) + COMMIT_DELIVERY
        fields |= set(COMMIT_DELIVERY)
    _object(value, fields, "presented commitment", optional=optional)
    _rule_id(value["rule_id"], "commitment rule_id")
    _text(value["presented"], "commitment presented", 2000)
    if "depends_on" in value:
        if type(value["depends_on"]) is not list or len(value["depends_on"]) > 16:
            raise ProfileError("invalid commitment depends_on")
        for dep in value["depends_on"]:
            _dep_ref(dep)
    status = value["status"]
    if proposal:
        if status != "proposed":
            raise ProfileError("proposed commitments must have status proposed; the harness records delivery")
    elif status == "proposed":
        if any(k in value for k in COMMIT_DELIVERY):
            raise ProfileError("undelivered commitment carries delivery fields")
    elif status == "applied":
        if type(value.get("delivered_sha256")) is not str or not HASH.fullmatch(value["delivered_sha256"]):
            raise ProfileError("applied commitment needs a delivery digest")
        _text(value.get("first_evidence_ref"), "commitment first_evidence_ref", 300)
        _text(value.get("last_evidence_ref"), "commitment last_evidence_ref", 300)
    else:
        raise ProfileError("invalid commitment status")
    return value


def _finding(f, proposal):
    """Finding record. Proposals may not carry harness-owned statuses."""
    statuses = PROPOSAL_STATUSES if proposal else STORED_STATUSES
    _object(f, FINDING_FIELDS, "finding")
    _ident(f["finding_id"], "finding_id")
    _text(f["category"], "finding category", 100)
    _texts(f["evidence_refs"], "evidence_refs", 50, 300)
    _text(f["actual"], "finding actual", 2000, empty=True)
    _text(f["desired"], "finding desired", 2000, empty=True)
    status = f["status"]
    if proposal and status in HARNESS_STATUSES:
        raise ProfileError(f"proposal cannot assign {status} status")
    if status not in statuses:
        raise ProfileError("invalid finding status")
    _text(f["blocked_reason"], "blocked_reason", 1000, empty=True)
    if status == "blocked" and not f["blocked_reason"]:
        raise ProfileError("blocked finding needs blocked_reason")
    alt = _object(f["container_level_alternative"], {"possible", "setting"}, "container_level_alternative")
    _flag(alt["possible"], "container_level_alternative.possible")
    _text(alt["setting"], "container_level_alternative.setting", 1000, empty=True)
    if alt["possible"] != bool(alt["setting"]):
        raise ProfileError("container_level_alternative setting is non-empty exactly when possible")
    _text(f["persistence_recommendation"], "persistence_recommendation", 2000, empty=True)
    _texts(f["verification_steps"], "verification_steps", 20, 500)
    _text(f["efficiency_note"], "efficiency_note", 1000, empty=True)
    return f


def _catalog(ops):
    """Harness-declared operation catalog. Every entry must name a kind the guest catalog implements."""
    if type(ops) is not list or len(ops) > MAX_ITEMS:
        raise ProfileError("invalid supported_operations")
    seen = set()
    for op in ops:
        _object(op, OP_FIELDS, "supported operation", optional=("description",))
        _ident(op["operation_id"], "operation_id")
        if type(op["kind"]) is not str or not KIND.fullmatch(op["kind"]):
            raise ProfileError("invalid operation kind")
        if op["operation_id"] in seen:
            raise ProfileError("duplicate supported operation_id")
        seen.add(op["operation_id"])
        try:
            DEFAULT_CATALOG.spec(op["kind"])
        except OperationError as exc:
            raise ProfileError(f"catalog kind not implemented: {op['kind']}") from exc
        if type(op["args"]) is not dict:
            raise ProfileError("invalid operation args description")
        _texts(op["preconditions"], "operation preconditions", 20, 200)
        for key in ("writable_scope", "rollback", "verification", "persistent_setup"):
            _text(op[key], f"operation {key}", 500)
    return ops


def _check_dependencies(proposal):
    """Change IDs are unique, dependencies name changes in the same transaction, and the graph is acyclic."""
    changes = {c["change_id"]: c for k in CHANGE_LISTS for c in proposal[k]}
    if len(changes) != sum(len(proposal[k]) for k in CHANGE_LISTS):
        raise ProfileError("duplicate change id")
    for change in changes.values():
        if any(dep not in changes for dep in change["depends_on"]):
            raise ProfileError("unknown dependency")
    state = {}

    def visit(cid):
        if state.get(cid) == "done":
            return
        if state.get(cid) == "active":
            raise ProfileError("dependencies must be acyclic")
        state[cid] = "active"
        for dep in changes[cid]["depends_on"]:
            visit(dep)
        state[cid] = "done"

    for cid in changes:
        visit(cid)


def _change(c, what, fields, optional=()):
    _object(c, CHANGE_FIELDS | set(fields), what, optional=optional)
    _ident(c["change_id"], f"{what} change_id")
    _text(c["reason"], f"{what} reason", 2000)
    _text(c["compatibility"], f"{what} compatibility", 2000)
    _ident(c["finding_id"], f"{what} finding_id")
    if type(c["depends_on"]) is not list or len(c["depends_on"]) > MAX_ITEMS:
        raise ProfileError(f"invalid {what} depends_on")
    for dep in c["depends_on"]:
        _ident(dep, f"{what} dependency")
    return c


def _validate_profile_change(c):
    _change(c, "profile change", {"action", "path", "expected_old", "value"}, optional=("value",))
    if c["action"] not in ("set", "remove"):
        raise ProfileError("invalid profile change action")
    parts = _profile_path(c["path"])
    if c["action"] == "set":
        if "value" not in c:
            raise ProfileError("profile set requires value")
        if parts[0] == "findings":
            _finding(c["value"], proposal=True)
            if c["value"]["finding_id"] != parts[1]:
                raise ProfileError("finding path and finding_id differ")
        elif parts[0] == "presented_facts":
            _commitment(c["value"], proposal=True)
        elif len(parts) == 3:
            _pairs({parts[2]: c["value"]}, "environment")
        else:
            _identity_value(parts[1], c["value"])
    else:
        if "value" in c:
            raise ProfileError("profile remove takes no value")
        if not (parts[0] == "desired_identity" and len(parts) == 3):
            raise ProfileError("only display/environment keys can be removed by a proposal")
    return parts


def _validate_rule_change(c, condition):
    _change(c, "rule change", {"action", "rule_id", "expected_old", "rule", "experimental"}, optional=("rule", "experimental"))
    if c["action"] not in ("add", "replace", "remove"):
        raise ProfileError("invalid rule change action")
    _rule_id(c["rule_id"])
    if ("rule" in c) != (c["action"] != "remove"):
        raise ProfileError("rule action/value mismatch")
    if "experimental" in c:
        _flag(c["experimental"], "experimental flag")
    if c["action"] == "add" and c["expected_old"] is not None:
        raise ProfileError("rule add expects expected_old null")
    if c["action"] != "add" and type(c["expected_old"]) is not dict:
        raise ProfileError("rule replace/remove expects the current rule as expected_old")
    if "rule" in c:
        rule = c["rule"]
        if type(rule) is not dict or rule.get("rule_id") != c["rule_id"]:
            raise ProfileError("rule change rule_id does not match rule")
        if rule.get("origin", "llm") != "llm":
            raise ProfileError("proposed rules must not claim user origin")
        if rule.get("condition", "any") not in ("any", condition):
            raise ProfileError("rule condition does not match the profile condition")
        _stored_rule({"origin": "llm", **rule})


def _stored_rule(rule):
    """The normalized form RuleSet stores (and returns as expected_old) for a proposed rule."""
    try:
        return RuleSet.from_dict({"schema": RULES_SCHEMA, "version": 1, "parent_sha256": None,
                                  "rules": [rule], "label_map": {}}).get(rule["rule_id"])
    except RuleError as exc:
        raise ProfileError(f"invalid rule: {exc}") from exc


def _validate_guest_operation(c, catalog):
    _change(c, "guest operation", {"operation_id", "kind", "args", "preconditions"})
    if type(c["kind"]) is not str or not KIND.fullmatch(c["kind"]):
        raise ProfileError("invalid guest operation kind")
    op = catalog.get(c["operation_id"])
    if op is None or op["kind"] != c["kind"]:
        raise ProfileError("unsupported guest operation")
    try:
        DEFAULT_CATALOG.validate_request({"operation_id": c["operation_id"], "kind": c["kind"], "args": c["args"]})
    except OperationError as exc:
        raise ProfileError(f"invalid guest operation arguments: {exc}") from exc
    _texts(c["preconditions"], "guest operation preconditions", 20, 200)
    if len(set(c["preconditions"])) != len(c["preconditions"]) or not set(c["preconditions"]) <= set(op["preconditions"]):
        raise ProfileError("guest operation precondition is not declared by the catalog")


def _upsert_finding(findings, incoming):
    """Model findings never set harness-owned status; existing applied/resolved status is preserved."""
    index = next((i for i, f in enumerate(findings) if f["finding_id"] == incoming["finding_id"]), None)
    old = findings[index] if index is not None else None
    item = deepcopy(incoming)
    if old is not None and old["status"] in HARNESS_STATUSES:
        item["status"] = old["status"]
    else:
        item["status"] = "blocked" if incoming["status"] == "blocked" else "unresolved"
    if index is None:
        findings.append(item)
    else:
        findings[index] = item


def _apply_profile_change(profile, change):
    parts = _profile_path(change["path"])
    if parts[0] == "findings":
        found = next((f for f in profile["findings"] if f["finding_id"] == parts[1]), None)
        if found != change["expected_old"]:
            raise ProfileError("profile expected_old mismatch")
        _upsert_finding(profile["findings"], change["value"])
        return
    parent = profile
    for key in parts[:-1]:
        parent = parent[key]
    key = parts[-1]
    if parent.get(key) != change["expected_old"]:
        raise ProfileError("profile expected_old mismatch")
    if change["action"] == "remove":
        if key not in parent:
            raise ProfileError("cannot remove missing profile value")
        del parent[key]
    else:
        parent[key] = deepcopy(change["value"])


def _apply_rule_changes(rules, changes):
    """expected_old is checked against the state left by earlier changes in the same proposal.

    The whole batch is applied in one copy-on-write step, so any failure leaves the installed set unchanged.
    No rule changes returns the same set, keeping the ruleset digest stable.
    """
    if not changes:
        return rules
    state = {r["rule_id"]: r for r in rules.rules}
    batch = []
    for c in changes:
        rid = c["rule_id"]
        if state.get(rid) != c["expected_old"]:
            raise ProfileError("rule expected_old mismatch")
        if c["action"] == "remove":
            del state[rid]
            batch.append({"action": "remove", "rule_id": rid})
        else:
            rule = {"origin": "llm", **deepcopy(c["rule"])}
            state[rid] = _stored_rule(rule)
            batch.append({"action": c["action"], "rule": rule})
    try:
        return rules.with_changes(batch)
    except RuleError as exc:
        raise ProfileError(f"invalid rule transaction: {exc}") from exc


# ---------------------------------------------------------------- profile bundle
def _profile_data(data):
    _plain(data)
    _object(data, PROFILE_FIELDS, "profile")
    if data["schema"] != SCHEMA:
        raise ProfileError("wrong profile schema")
    _number(data["version"], "profile version", lo=1)
    if data["condition"] not in CONDITIONS:
        raise ProfileError("unknown profile condition")
    parent = data["parent_sha256"]
    if parent is not None and (type(parent) is not str or not HASH.fullmatch(parent)):
        raise ProfileError("invalid profile parent digest")
    _ident(data["profile_id"], "profile_id")
    _ident(data["preset_id"], "preset_id")
    identity = _object(data["desired_identity"], ID_FIELDS, "desired_identity", optional=("branch",))
    _text(identity["os"], "desired_identity.os", 64)
    _number(identity["uid"], "desired_identity.uid")
    _number(identity["gid"], "desired_identity.gid")
    for key in IDENTITY_KEYS:
        if key in identity:
            _identity_value(key, identity[key])
    _pairs(identity["display"], "display")
    _pairs(identity["environment"], "environment")
    for key in ("confirmed_state", "provenance", "presented_facts"):
        if type(data[key]) is not dict:
            raise ProfileError(f"{key} must be an object")
    for key, entry in data["confirmed_state"].items():
        _confirmed_fact(key, entry)
    for key in data["provenance"]:
        _ident(key, "provenance key")
    for key, commitment in data["presented_facts"].items():
        _ident(key, "presented fact id")
        _commitment(commitment, proposal=False)
    _dicts(data["guest_setup"], "guest_setup")
    _dicts(data["experimental_discrepancies"], "experimental_discrepancies")
    findings = _dicts(data["findings"], "findings")
    for f in findings:
        _finding(f, proposal=False)
    if len({f["finding_id"] for f in findings}) != len(findings):
        raise ProfileError("duplicate stored finding_id")
    _secrets(data)
    if type(data["ruleset"]) is not dict:
        raise ProfileError("invalid profile ruleset")
    try:
        rules = RuleSet.from_dict(data["ruleset"])
    except (RuleError, TypeError, KeyError) as exc:
        raise ProfileError(f"invalid profile ruleset: {exc}") from exc
    if any(rule["condition"] not in ("any", data["condition"]) for rule in rules.rules):
        raise ProfileError("profile ruleset contains a rule for another condition")
    return deepcopy(data)


@dataclass(frozen=True)
class ProfileBundle:
    """Validated profile as canonical JSON. Construction always validates, even via the public constructor."""
    canon: str
    initialized: bool = False

    def __post_init__(self):
        if type(self.canon) is not str:
            raise ProfileError("profile canon must be JSON text")
        object.__setattr__(self, "canon", canonical(_profile_data(loads_strict(self.canon))))

    @classmethod
    def from_dict(cls, data):
        return cls(canonical(_profile_data(deepcopy(data))))

    @classmethod
    def preset(cls, condition):
        if condition not in CONDITIONS:
            raise ProfileError("unknown condition")
        guest = _GUEST_BY_CONDITION[condition]
        identity = {"os": "linux", "user": guest["account"], "uid": guest["uid"], "gid": guest["gid"],
                    "hostname": guest["hostname"], "home": guest["home"], "worktree": guest["worktree"],
                    "display": {}, "environment": {}}
        return cls.from_dict({"schema": SCHEMA, "version": 1, "parent_sha256": None,
            "profile_id": str(uuid.uuid4()), "condition": condition, "preset_id": condition,
            "desired_identity": identity, "guest_setup": [], "confirmed_state": {}, "presented_facts": {},
            "ruleset": default_ruleset(condition).data,
            "findings": [], "experimental_discrepancies": [], "provenance": {"preset": condition}})

    @classmethod
    def load(cls, path):
        p = Path(path)
        try:
            if p.stat().st_size > MAX_PROFILE_BYTES:
                raise ProfileError("profile exceeds size limit")
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProfileError(f"cannot read profile {p}: {exc}") from exc
        return cls.from_dict(loads_strict(text))

    @classmethod
    def load_or_initialize(cls, path, condition):
        p = Path(path)
        if p.exists():
            bundle = cls.load(p)
            if bundle.data["condition"] != condition:
                raise ProfileError(f"profile {p} belongs to condition {bundle.data['condition']}, not {condition}")
            return bundle
        bundle = cls.preset(condition)
        bundle.dump(p)
        return replace(bundle, initialized=True)

    @property
    def data(self):
        return json.loads(self.canon)

    @property
    def sha256(self):
        return hashlib.sha256(self.canon.encode()).hexdigest()

    @property
    def ruleset(self):
        return RuleSet.from_dict(self.data["ruleset"])

    def dumps(self):
        return json.dumps(self.data, indent=2, sort_keys=True) + "\n"

    def dump(self, path):
        p = Path(path)
        payload = self.dumps()
        name = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, p)
        except OSError as exc:
            _unlink(name)
            raise ProfileError(f"cannot write profile to {p}: {exc.strerror or exc}") from exc
        except BaseException:
            _unlink(name)
            raise
        return p

    def snapshot(self, directory):
        """Write an immutable, content-addressed versioned snapshot."""
        directory = Path(directory)
        path = directory / f"profile-v{self.data['version']}-{self.sha256}.json"
        if path.exists():
            if ProfileBundle.load(path).sha256 != self.sha256:
                raise ProfileError("profile snapshot path collision")
            return path
        directory.mkdir(parents=True, exist_ok=True)
        return self.dump(path)

    def candidate(self, proposal, packet):
        """Return the bundle produced by a complete, validated proposal (self when nothing changes).

        Guest operations are validated here but executed and verified by the harness. Any failure
        raises before a new bundle exists, so the installed bundle is never partially changed.
        """
        packet = validate_packet(packet)
        proposal = validate_proposal(proposal, packet)
        if packet["profile_sha256"] != self.sha256 or packet["ruleset_sha256"] != self.ruleset.sha256:
            raise ProfileError("stale profile/ruleset digest")
        if not (proposal["profile_changes"] or proposal["rule_changes"] or proposal["findings"]):
            return self
        profile = self.data
        for change in proposal["profile_changes"]:
            _apply_profile_change(profile, change)
        rules = _apply_rule_changes(self.ruleset, proposal["rule_changes"])
        for change in proposal["profile_changes"]:
            if change["path"].startswith("presented_facts.") and change["action"] == "set":
                value = change["value"]
                if rules.get(value["rule_id"]) is None:
                    raise ProfileError("presented commitment references an unknown rule")
                for dep in value.get("depends_on", []):
                    if dep not in profile["presented_facts"] and rules.get(dep) is None:
                        raise ProfileError("presented commitment depends on an unknown fact or rule")
        for finding in proposal["findings"]:
            _upsert_finding(profile["findings"], finding)
        profile["ruleset"] = rules.data
        profile["version"] += 1
        profile["parent_sha256"] = self.sha256
        return ProfileBundle.from_dict(profile)

    def record_delivery(self, fact_ids, rendering_sha256, evidence_ref):
        """Harness-only: a delivered rendering applies proposed commitments and records evidence refs.

        The first delivery sets first/last evidence refs; later deliveries keep the first and move the last.
        """
        if not fact_ids:
            return self
        if type(rendering_sha256) is not str or not HASH.fullmatch(rendering_sha256):
            raise ProfileError("invalid rendering digest")
        _text(evidence_ref, "delivery evidence_ref", 300)
        profile = self.data
        for fact_id in fact_ids:
            entry = profile["presented_facts"].get(fact_id)
            if entry is None:
                raise ProfileError(f"no commitment for {fact_id}")
            entry["status"] = "applied"
            entry["delivered_sha256"] = rendering_sha256
            entry.setdefault("first_evidence_ref", evidence_ref)
            entry["last_evidence_ref"] = evidence_ref
        profile["version"] += 1
        profile["parent_sha256"] = self.sha256
        return ProfileBundle.from_dict(profile)


def _unlink(name):
    if name is None:
        return
    try:
        os.unlink(name)
    except OSError:
        pass


# ---------------------------------------------------------------- packet / proposal / review
def validate_packet(packet):
    """Validate the harness packet and return a copy with observation fields redacted for transport."""
    _plain(packet)
    _object(packet, PACKET_FIELDS, "packet")
    if packet["schema"] != PACKET_SCHEMA:
        raise ProfileError("wrong packet schema")
    _ident(packet["packet_id"], "packet_id")
    if packet["phase"] not in PHASES or packet["condition"] not in CONDITIONS:
        raise ProfileError("invalid packet identity")
    for key in ("profile_sha256", "ruleset_sha256"):
        if type(packet[key]) is not str or not HASH.fullmatch(packet[key]):
            raise ProfileError(f"invalid {key}")
    obs = packet["observation_sha256"]
    if packet["phase"] == "before_execution" and obs is not None:
        raise ProfileError("pre-execution observation digest must be null")
    if packet["phase"] == "after_execution" and (type(obs) is not str or not HASH.fullmatch(obs)):
        raise ProfileError("invalid observation digest")
    bundle = ProfileBundle.from_dict(packet["profile"])
    if (bundle.sha256 != packet["profile_sha256"] or bundle.ruleset.sha256 != packet["ruleset_sha256"]
            or bundle.data["condition"] != packet["condition"]):
        raise ProfileError("packet profile/ruleset digest or condition mismatch")
    for key in ("request", "result", "currently_presented", "protected_facts", "limits", "coverage"):
        if type(packet[key]) is not dict:
            raise ProfileError(f"packet {key} must be an object")
    for key in ("verified_state_deltas", "supported_operations"):
        if type(packet[key]) is not list:
            raise ProfileError(f"packet {key} must be an array")
    coverage = _object(packet["coverage"], {"complete", "omitted_refs"}, "coverage")
    _flag(coverage["complete"], "coverage.complete")
    _texts(coverage["omitted_refs"], "coverage.omitted_refs", MAX_ITEMS * 16, 500)
    if coverage["complete"] and coverage["omitted_refs"]:
        raise ProfileError("complete coverage cannot list omitted refs")
    _catalog(packet["supported_operations"])
    out = deepcopy(packet)
    for key in PACKET_FIELDS - {"profile"}:
        if key in OBSERVATION_FIELDS:
            out[key] = redact(packet[key])
        elif key == "protected_facts":
            # The harness's `tokens` list holds numeric/object facts, not credential fields.
            # Keep strict rejection for unexpected metadata under secret-named keys.
            for name, value in packet[key].items():
                if name == "tokens" and isinstance(value, list):
                    out[key][name] = [redact(token) for token in value]
                else:
                    _secrets({name: value})
        else:
            _secrets(packet[key])
    return out


def validate_proposal(p, packet):
    """Validate a complete proposal against its packet. Raises before any change is applied."""
    packet = validate_packet(packet)
    _plain(p)
    _object(p, PROPOSAL_FIELDS, "proposal")
    if p["schema"] != PROPOSAL_SCHEMA:
        raise ProfileError("wrong proposal schema")
    for key in ("packet_id", "phase", "profile_sha256", "ruleset_sha256", "observation_sha256"):
        if p[key] != packet[key]:
            raise ProfileError(f"stale/mismatched proposal {key}")
    _text(p["summary"], "summary", 2000)
    _texts(p["checked"], "checked", 200, 200)
    for key in (*CHANGE_LISTS, "findings"):
        if type(p[key]) is not list or len(p[key]) > MAX_ITEMS:
            raise ProfileError(f"{key} must be a short array")
    _secrets(p)
    condition = packet["condition"]
    catalog = {op["operation_id"]: op for op in packet["supported_operations"]}
    for c in p["profile_changes"]:
        _validate_profile_change(c)
    for c in p["rule_changes"]:
        _validate_rule_change(c, condition)
    for c in p["guest_operations"]:
        _validate_guest_operation(c, catalog)
    candidate_profile = deepcopy(packet["profile"])
    for c in p["profile_changes"]:
        _apply_profile_change(candidate_profile, c)
    for c in p["guest_operations"]:
        if c["kind"] == "system_set_hostname" and c["args"]["hostname"] != candidate_profile["desired_identity"]["hostname"]:
            raise ProfileError("hostname operation must match the candidate profile hostname")
    for f in p["findings"]:
        _finding(f, proposal=True)
    if len({f["finding_id"] for f in p["findings"]}) != len(p["findings"]):
        raise ProfileError("duplicate finding_id in proposal")
    _check_dependencies(p)
    actionable = {f["finding_id"] for f in p["findings"]}
    actionable |= {f["finding_id"] for f in packet["profile"]["findings"]}
    for key in CHANGE_LISTS:
        for c in p[key]:
            if c["finding_id"] not in actionable:
                raise ProfileError("change must reference an actionable finding")
    return deepcopy(p)


def validate_review(review, packet, proposal):
    """Validate a reviewer response. Approval must carry no blocking findings; rejection must carry one."""
    packet = validate_packet(packet)
    proposal = validate_proposal(proposal, packet)
    _plain(review)
    _object(review, REVIEW_FIELDS, "review")
    _secrets(review)
    if (review["schema"] != REVIEW_SCHEMA or review["packet_id"] != packet["packet_id"]
            or review["profile_sha256"] != packet["profile_sha256"] or review["proposal_sha256"] != digest(proposal)):
        raise ProfileError("stale reviewer response")
    if review["verdict"] not in ("approve", "reject"):
        raise ProfileError("invalid review verdict")
    blocking = review["blocking_findings"]
    if type(blocking) is not list or len(blocking) > MAX_ITEMS:
        raise ProfileError("invalid blocking_findings")
    changes = {c["change_id"] for key in CHANGE_LISTS for c in proposal[key]}
    for item in blocking:
        _object(item, {"change_id", "criterion", "evidence_ref", "failure", "correction"}, "blocking finding")
        _ident(item["change_id"], "blocking finding change_id")
        if item["change_id"] not in changes:
            raise ProfileError("blocking finding references an unknown change")
        for key in ("criterion", "evidence_ref", "failure", "correction"):
            _text(item[key], f"blocking finding {key}", 2000)
    _texts(review["suggestions"], "suggestions", MAX_ITEMS, 2000)
    if review["verdict"] == "approve" and blocking:
        raise ProfileError("approval cannot carry blocking findings")
    if review["verdict"] == "reject" and not blocking:
        raise ProfileError("rejection needs a blocking finding")
    return deepcopy(review)
