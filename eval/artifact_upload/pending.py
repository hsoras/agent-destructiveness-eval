"""Pure operations for changes deferred to a later live-proxy run."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from .guest_operations import DEFAULT_CATALOG, OperationError
from .profiles import ProfileBundle, ProfileError, canonical
from .rules import RuleError, RuleSet, check_rendering, apply_edits


def _validate_change(kind, change):
    if kind == "rule":
        try:
            # RuleSet is the canonical validator and normalizer for stored rules.
            parsed = RuleSet.from_dict({"schema": "artifact-presentation-rules.v1", "version": 1,
                                        "parent_sha256": None, "rules": [change], "label_map": {}})
        except (RuleError, TypeError, KeyError) as exc:
            raise ProfileError(f"invalid pending rule: {exc}") from exc
        return parsed.rules[0]
    if kind == "guest_op":
        try:
            return DEFAULT_CATALOG.validate_request(change)
        except (OperationError, TypeError, KeyError) as exc:
            raise ProfileError(f"invalid pending guest operation: {exc}") from exc
    raise ProfileError("pending kind must be rule or guest_op")


def _bundle_data(bundle):
    return bundle.data if isinstance(bundle, ProfileBundle) else deepcopy(bundle)


def add_pending(bundle, *, kind, change, source_run, source_proposal_sha256, reason):
    """Return a new bundle with a validated deferred change; identical changes dedupe."""
    data = _bundle_data(bundle)
    normalized = _validate_change(kind, change)
    for item in data["pending"]:
        if item["kind"] == kind and canonical(item["change"]) == canonical(normalized):
            return ProfileBundle.from_dict(data)
    entry = {"pending_id": uuid.uuid4().hex, "kind": kind, "change": normalized,
             "source_run": source_run, "source_proposal_sha256": source_proposal_sha256,
             "reason": reason, "created_at": datetime.now(timezone.utc).isoformat()}
    data["pending"].append(entry)
    return ProfileBundle.from_dict(data)


def _touches_confirmed(item, keys):
    change, kind = item["change"], item["kind"]
    if kind == "rule":
        required = {r["fact"] for r in change.get("match", {}).get("requires", [])}
        return bool(required & keys)
    op = change["kind"]
    args = change.get("args", {})
    if op == "git_attach_head":
        return bool(keys & {"git_head_branch", "git_head_commit", "git_branch:" + args.get("name", "")})
    if op == "git_create_branch":
        touched = {"git_branch:" + args.get("name", "")}
        if args.get("attach", False):
            touched.add("git_head_branch")
        return bool(keys & touched)
    if op == "files_normalize_untracked":
        paths = set(args.get("paths", [])) | {args.get("mtime_from", "")}
        return any(k.startswith("file:") and k[5:] in paths for k in keys)
    if op == "env_set_variables":
        names = set(args.get("set", {})) | set(args.get("unset", []))
        return bool(keys & {"env_" + name for name in names})
    if op in ("env_set_workspace", "system_set_hostname"):
        return any(k.startswith("env_") or k == "hostname" for k in keys)
    if op == "files_install_preset":
        return "file:" + args.get("path", "") in keys
    return False


def _rule_protected(rule, protected_facts):
    if isinstance(protected_facts, dict):
        tokens = [str(k) for k, v in protected_facts.items() if v]
    elif isinstance(protected_facts, (list, tuple, set)):
        tokens = [x for x in protected_facts if isinstance(x, str)]
    else:
        tokens = []
    # Each protected literal is exercised through the candidate edits and guarded by
    # the same invariant used by the renderer.
    for token in tokens:
        rendered = apply_edits(token, rule["edits"])
        ok, _ = check_rendering(token, rendered, [token])
        if not ok:
            return False
    return True


def promote_pending(bundle, confirmed_state, protected_facts):
    """Promote still-valid deferred items, returning (new bundle, promoted, stale)."""
    data = _bundle_data(bundle)
    keys = set(confirmed_state or {})
    promoted, stale, kept = [], [], []
    rules = RuleSet.from_dict(data["ruleset"])
    for original in data["pending"]:
        item = deepcopy(original)
        try:
            change = _validate_change(item["kind"], item["change"])
            reason = None
            if _touches_confirmed(item, keys):
                reason = "touches-confirmed-state"
            elif item["kind"] == "rule" and not _rule_protected(change, protected_facts):
                reason = "protected-facts-changed"
            elif item["kind"] == "rule":
                existing = rules.get(change["rule_id"])
                action = "replace" if existing is not None else "add"
                rules = rules.with_changes([{"action": action, "rule": change}])
                data["ruleset"] = rules.data
            else:
                if any(op.get("operation_id") == change["operation_id"] for op in data["guest_setup"]):
                    reason = "guest-operation-already-present"
                else:
                    data["guest_setup"].append(change)
            if reason:
                item["reason"] = reason
                stale.append(item)
            else:
                promoted.append(item)
        except (ProfileError, RuleError, OperationError, KeyError, TypeError, ValueError) as exc:
            item["reason"] = "invalid-at-promotion: " + str(exc)[:300]
            stale.append(item)
    data["pending"] = kept
    return ProfileBundle.from_dict(data), promoted, stale
