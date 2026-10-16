"""Contradiction judge for live-proxy proposals (docs/live-proxy-deferral-spec.md, decision step 4).

The judge sees the proposal, profile, protected facts and the DELIVERED outputs only (what the agent was
actually shown, post-rewrite). It never receives raw, pre-rewrite output. It never raises: any transport,
timeout, malformed or schema-invalid reply, or unusable input yields Verdict('unsure', ...).

The client must carry the extended reviewer system prompt (REVIEWER_SYSTEM_PROMPT, section CONTRADICTION
CHECK) as its system_prompt, the same way ProfileDiscovery configures its reviewer client.
"""
from __future__ import annotations

from dataclasses import dataclass

from .profiles import canonical, parse_reply, redact

OUTCOMES = frozenset({"no_contradiction", "contradiction", "unsure"})
SCHEMA = "contradiction-check.v1"
HISTORY_FIELDS = ("call_id", "tool", "request", "delivered_output")
JUDGE_SECONDS = 120.0
JUDGE_MAX_TOKENS = 1024
# Placeholder per-call cost ceiling for the auxiliary transport; align with the reviewer_budget max call cost.
JUDGE_MAX_COST_USD = 0.05
MAX_REASON_CHARS = 500


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    truncated: bool

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError("invalid verdict outcome")


def _unsure(reason, truncated=False):
    return Verdict("unsure", reason[:MAX_REASON_CHARS], bool(truncated))


def _history_entries(delivered_history):
    """Validate and whitelist entries (raw or other extra keys are dropped), chronological order kept."""
    if not isinstance(delivered_history, (list, tuple)):
        raise ValueError("delivered_history must be a list")
    entries = []
    for item in delivered_history:
        if not isinstance(item, dict) or not all(key in item for key in HISTORY_FIELDS):
            raise ValueError("malformed delivered history entry")
        if not isinstance(item["call_id"], str) or not isinstance(item["tool"], str) or not isinstance(item["delivered_output"], str):
            raise ValueError("malformed delivered history entry")
        entries.append({key: item[key] for key in HISTORY_FIELDS})
    return entries


def _parse_verdict_text(text):
    obj, issues = parse_reply(text)
    if issues:
        raise ValueError("judge reply required normalization")
    if not isinstance(obj, dict) or set(obj) != {"outcome", "reason"}:
        raise ValueError("judge reply does not match the contradiction-check schema")
    outcome, reason = obj["outcome"], obj["reason"]
    if outcome not in OUTCOMES or not isinstance(reason, str) or not reason.strip():
        raise ValueError("judge reply has an invalid outcome or reason")
    return outcome, reason.strip()


def judge_contradiction(client, *, proposal, profile, protected_facts, delivered_history, cap_bytes) -> Verdict:
    """Ask the judge whether applying `proposal` contradicts what the agent was already shown.

    delivered_history: chronological list of dicts with call_id, tool, request, delivered_output.
    cap_bytes: maximum canonical-JSON size of the newest-first history sent to the judge. Oldest calls are
    dropped first and the judge is told which ones. Any dropped history makes the verdict 'unsure'
    without calling the judge because string matching cannot establish semantic irrelevance.
    """
    truncated = False
    try:
        if not isinstance(cap_bytes, int) or isinstance(cap_bytes, bool) or cap_bytes < 0:
            raise ValueError("cap_bytes must be a nonnegative int")
        entries = _history_entries(delivered_history)
        newest_first = list(reversed(entries))

        kept, dropped, used = [], [], 0
        for index, entry in enumerate(newest_first):
            redacted = redact(entry)
            size = len(canonical(redacted).encode())
            if used + size > cap_bytes:
                dropped = newest_first[index:]
                break
            kept.append(redacted)
            used += size
        truncated = bool(dropped)
        dropped_ids = [entry["call_id"] for entry in dropped]

        if dropped:
            return _unsure("history truncated; dropped calls %s may contain relevant context" % dropped_ids,
                           truncated=True)

        payload = {
            "schema": SCHEMA,
            "proposal": redact(proposal),
            "profile": redact(profile),
            "protected_facts": redact(protected_facts),
            "delivered_history_newest_first": kept,
            "history_truncation": {"truncated": truncated, "dropped_count": len(dropped), "dropped_call_ids": dropped_ids},
        }
        canonical(payload)  # reject non-JSON-able inputs before any call

        reply = client.complete(payload, seconds=JUDGE_SECONDS, max_tokens=JUDGE_MAX_TOKENS,
                                max_cost_usd=JUDGE_MAX_COST_USD)
        text = reply if isinstance(reply, str) else getattr(reply, "output", None)
        if not isinstance(text, str):
            raise ValueError("judge reply has no text")
        outcome, reason = _parse_verdict_text(text)
        return Verdict(outcome, reason[:MAX_REASON_CHARS], truncated)
    except Exception as exc:  # the judge never raises; every failure is an unsure verdict
        return _unsure("judge unavailable: %s" % type(exc).__name__, truncated)
