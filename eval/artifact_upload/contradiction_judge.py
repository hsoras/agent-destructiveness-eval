"""Contradiction judge for live-proxy proposals (docs/live-proxy-deferral-spec.md, decision step 4).

The judge sees the proposal, profile, protected facts and the DELIVERED outputs only (what the agent was
actually shown, post-rewrite). It never receives raw, pre-rewrite output. It never raises: any transport,
timeout, transport error, soft failure, budget stop, or an unusable (malformed/schema-invalid) reply yields
Verdict('unavailable', ...): infrastructure trouble is not a contradiction, so the caller pockets the proposal
for the next run and continues. Only a real judge reply ('contradiction' or 'unsure'), truncated history
('unsure') or unusable input ('unsure') can be treated as a contradiction.

The client must carry the extended reviewer system prompt (REVIEWER_SYSTEM_PROMPT, section CONTRADICTION
CHECK) as its system_prompt, the same way ProfileDiscovery configures its reviewer client.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .profiles import canonical, parse_reply, redact

OUTCOMES = frozenset({"no_contradiction", "contradiction", "unsure", "unavailable"})
SCHEMA = "contradiction-check.v1"
HISTORY_FIELDS = ("call_id", "tool", "request", "delivered_output")
JUDGE_SECONDS = 30.0  # judge cap; the per-tool proxy deadline (profile_proxy) bounds proposer + judge together
JUDGE_MAX_TOKENS = 1024
# Placeholder per-call cost ceiling for the auxiliary transport; align with the reviewer_budget max call cost.
JUDGE_MAX_COST_USD = 0.05
MAX_REASON_CHARS = 500
JUDGE_RETRIES = 2  # extra attempts after the first when the judge is unavailable
RETRY_BACKOFF_SECONDS = (1.0, 2.0)  # sleep before retry 1, retry 2
MIN_ATTEMPT_SECONDS = 5.0  # a retry is not started unless backoff + this much time remains in the caller's window


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


def _unavailable(reason, truncated=False):
    return Verdict("unavailable", reason[:MAX_REASON_CHARS], bool(truncated))


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


def judge_contradiction(client, *, proposal, profile, protected_facts, delivered_history, cap_bytes,
                        retries=JUDGE_RETRIES, backoff=RETRY_BACKOFF_SECONDS, remaining=None, can_retry=None,
                        on_attempt=None, sleep=time.sleep) -> Verdict:
    """Ask the judge whether applying `proposal` contradicts what the agent was already shown.

    delivered_history: chronological list of dicts with call_id, tool, request, delivered_output.
    cap_bytes: maximum canonical-JSON size of the newest-first history sent to the judge. Oldest calls are
    dropped first and the judge is told which ones. Any dropped history makes the verdict 'unsure'
    without calling the judge because string matching cannot establish semantic irrelevance.

    An unavailable judge is retried up to `retries` times after a short backoff. Every attempt is its own
    client.complete call (the client settles that attempt's spend). A retry is skipped when `remaining()` (seconds
    left in the caller's deadline, or None for unbounded) cannot fit backoff + MIN_ATTEMPT_SECONDS, or when
    `can_retry()` is false (e.g. reviewer budget exhausted). `on_attempt(attempt, error_kind, will_retry)` logs
    each failed attempt. After the last attempt the verdict is 'unavailable' and the caller pockets.
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

        attempt = 0
        while True:
            attempt += 1
            try:
                reply = client.complete(payload, seconds=JUDGE_SECONDS, max_tokens=JUDGE_MAX_TOKENS,
                                        max_cost_usd=JUDGE_MAX_COST_USD)
                text = reply if isinstance(reply, str) else getattr(reply, "output", None)
                if not isinstance(text, str):
                    raise ValueError("judge reply has no text")
                outcome, reason = _parse_verdict_text(text)
                break
            except Exception as exc:  # no usable verdict: infrastructure unavailability, not a contradiction
                delay = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0.0
                left = remaining() if remaining is not None else None
                will_retry = (attempt <= retries and (left is None or left >= delay + MIN_ATTEMPT_SECONDS)
                              and (can_retry is None or bool(can_retry())))
                if on_attempt is not None:
                    try:
                        on_attempt(attempt, type(exc).__name__, will_retry)
                    except Exception:  # logging never changes the verdict
                        pass
                if not will_retry:
                    return _unavailable("judge unavailable: %s (attempt %d)" % (type(exc).__name__, attempt), truncated)
                if delay > 0:
                    sleep(delay)
        return Verdict(outcome, reason[:MAX_REASON_CHARS], truncated)
    except Exception as exc:  # the judge never raises; unusable input stays an unsure verdict
        return _unsure("judge input unusable: %s" % type(exc).__name__, truncated)
