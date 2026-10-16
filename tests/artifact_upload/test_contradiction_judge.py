import json
import types

import pytest

from eval.artifact_upload import contradiction_judge as cj
from eval.artifact_upload.contradiction_judge import Verdict, judge_contradiction


SENTINEL_RAW = "RAW-PRE-REWRITE-SENTINEL-7731"


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def complete(self, payload, **limits):
        self.calls.append((payload, limits))
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(output=self.reply, input_tokens=1, output_tokens=1, cost_usd=0.0)


def reply(outcome, reason="because call-2"):
    return json.dumps({"outcome": outcome, "reason": reason})


def entry(n, output=None, **extra):
    base = {"call_id": "call-%d" % n, "tool": "bash", "request": {"command": "echo %d" % n},
            "delivered_output": output if output is not None else "output of call %d" % n}
    base.update(extra)
    return base


PROPOSAL = {"changes": [{"change_id": "c1", "kind": "rule", "rule_id": "r-branch",
                         "reason": "wording"}], "checked": ["shell"]}
PROFILE = {"profile_id": "p1", "version": 1}
FACTS = {"protected": ["exit_code"]}


def run(client, history, cap=10_000, proposal=PROPOSAL):
    return judge_contradiction(client, proposal=proposal, profile=PROFILE, protected_facts=FACTS,
                               delivered_history=history, cap_bytes=cap)


def sent_text(client):
    return "\n".join(json.dumps(payload, sort_keys=True) + json.dumps(limits, sort_keys=True)
                     for payload, limits in client.calls)


def test_verdict_is_frozen_and_validates_outcome():
    v = Verdict("unsure", "x", False)
    with pytest.raises(Exception):
        v.outcome = "contradiction"
    with pytest.raises(ValueError):
        Verdict("maybe", "x", False)


def test_contradiction_outcome_passes_through():
    client = FakeClient(reply("contradiction", "call-2 shows main"))
    v = run(client, [entry(1), entry(2)])
    assert v == Verdict("contradiction", "call-2 shows main", False)
    assert len(client.calls) == 1


def test_no_contradiction_outcome_passes_through():
    client = FakeClient(reply("no_contradiction", "nothing shown conflicts"))
    assert run(client, [entry(1)]) == Verdict("no_contradiction", "nothing shown conflicts", False)


def test_unsure_reply_passes_through():
    client = FakeClient(reply("unsure", "ambiguous"))
    assert run(client, [entry(1)]).outcome == "unsure"


@pytest.mark.parametrize("error", [TimeoutError("deadline"), ConnectionError("reset"), RuntimeError("boom"),
                                   ValueError("auxiliary reply lacks usage")])
def test_client_exceptions_become_unsure_without_raising(error):
    v = run(FakeClient(error=error), [entry(1)])
    assert v.outcome == "unsure" and v.truncated is False
    assert type(error).__name__ in v.reason


@pytest.mark.parametrize("text", [
    "not json at all",
    "",
    "[1, 2]",
    json.dumps({"outcome": "maybe", "reason": "x"}),
    json.dumps({"outcome": "contradiction"}),
    json.dumps({"outcome": "contradiction", "reason": ""}),
    json.dumps({"outcome": "contradiction", "reason": "x", "extra": 1}),
    '{"outcome":"contradiction","outcome":"no_contradiction","reason":"duplicate key"}',
    '{"schema": "profile-review.v1", "outcome": "contradiction", "reason": "cut',
    None,
])
def test_malformed_or_schema_invalid_reply_is_unsure(text):
    v = run(FakeClient(reply=text), [entry(1)])
    assert v.outcome == "unsure"


def test_reply_object_without_output_text_is_unsure():
    class NoText:
        def complete(self, payload, **limits):
            return types.SimpleNamespace(output=None)

    assert run(NoText(), [entry(1)]).outcome == "unsure"


def test_malformed_history_is_unsure_and_not_sent():
    client = FakeClient(reply("no_contradiction"))
    assert run(client, [{"call_id": "x"}]).outcome == "unsure"
    assert run(client, "not-a-list").outcome == "unsure"
    assert run(client, [entry(1)], cap=-1).outcome == "unsure"
    assert client.calls == []


def test_non_json_proposal_is_unsure_without_call():
    client = FakeClient(reply("no_contradiction"))
    assert run(client, [entry(1)], proposal={"x": object()}).outcome == "unsure"
    assert client.calls == []


def test_history_is_sent_newest_first():
    client = FakeClient(reply("no_contradiction"))
    run(client, [entry(1), entry(2), entry(3)])
    payload, limits = client.calls[0]
    ids = [e["call_id"] for e in payload["delivered_history_newest_first"]]
    assert ids == ["call-3", "call-2", "call-1"]
    assert payload["history_truncation"] == {"truncated": False, "dropped_count": 0, "dropped_call_ids": []}
    assert limits["seconds"] == cj.JUDGE_SECONDS == 45.0


def test_oldest_calls_are_dropped_first_and_judge_is_told():
    history = [entry(i, output="x" * 400) for i in range(1, 7)]
    one = len(cj.canonical(cj.redact(entry(6, output="x" * 400))).encode())
    client = FakeClient(reply("no_contradiction"))
    v = run(client, history, cap=one * 3 + 10)  # room for the three newest entries only
    payload, _ = client.calls[0]
    kept = [e["call_id"] for e in payload["delivered_history_newest_first"]]
    assert kept == ["call-6", "call-5", "call-4"]
    assert payload["history_truncation"] == {"truncated": True, "dropped_count": 3,
                                             "dropped_call_ids": ["call-3", "call-2", "call-1"]}
    assert v.truncated is True and v.outcome == "no_contradiction"


def test_truncation_with_no_overlap_still_asks_judge():
    history = [entry(1, output="z" * 2000), entry(2, output="alpha")]
    client = FakeClient(reply("no_contradiction"))
    v = run(client, history, cap=600)
    assert v == Verdict("no_contradiction", "because call-2", True)
    assert client.calls[0][0]["history_truncation"]["dropped_call_ids"] == ["call-1"]


def test_truncation_touching_proposal_ref_is_unsure_without_call():
    proposal = {"changes": [{"change_id": "c1", "path": "/work/secret-config.yaml"}]}
    history = [entry(1, output="cat /work/secret-config.yaml " + "z" * 2000), entry(2, output="alpha")]
    client = FakeClient(reply("no_contradiction"))
    v = run(client, history, cap=600, proposal=proposal)
    assert v.outcome == "unsure" and v.truncated is True
    assert "call-1" in v.reason
    assert client.calls == []


def test_truncation_of_unrelated_calls_does_not_force_unsure():
    proposal = {"changes": [{"change_id": "c1", "path": "/work/only-here.txt"}]}
    history = [entry(1, output="unrelated text " + "z" * 2000), entry(2, output="alpha")]
    client = FakeClient(reply("contradiction", "call-2 shows it"))
    v = run(client, history, cap=600, proposal=proposal)
    assert v == Verdict("contradiction", "call-2 shows it", True)
    assert len(client.calls) == 1


def test_oversized_newest_call_drops_everything():
    client = FakeClient(reply("no_contradiction"))
    v = run(client, [entry(1, output="w" * 5000)], cap=100, proposal={"changes": []})
    assert v.truncated is True
    assert client.calls[0][0]["delivered_history_newest_first"] == []
    assert client.calls[0][0]["history_truncation"]["dropped_call_ids"] == ["call-1"]


def test_raw_output_never_reaches_the_request():
    history = [dict(entry(1), raw_output=SENTINEL_RAW, raw=SENTINEL_RAW),
               entry(2, output="shown text")]
    client = FakeClient(reply("no_contradiction"))
    run(client, history)
    sent = sent_text(client)
    assert SENTINEL_RAW not in sent
    assert "shown text" in sent
    for item in client.calls[0][0]["delivered_history_newest_first"]:
        assert set(item) == set(cj.HISTORY_FIELDS)


def test_raw_output_is_not_sent_even_when_dropped_or_timed_out():
    history = [dict(entry(1), raw_output=SENTINEL_RAW), entry(2, output="z" * 2000)]
    client = FakeClient(reply("no_contradiction"))
    run(client, history, cap=600)
    assert SENTINEL_RAW not in sent_text(client)
    failing = FakeClient(error=TimeoutError(SENTINEL_RAW))
    v = run(failing, history)
    assert v.outcome == "unsure" and SENTINEL_RAW not in v.reason


def test_secrets_in_delivered_output_are_redacted_before_sending():
    history = [entry(1, output="token=ghp_" + "a" * 36)]
    client = FakeClient(reply("no_contradiction"))
    run(client, history)
    assert "ghp_" + "a" * 36 not in sent_text(client)


def test_reason_is_bounded():
    client = FakeClient(reply("contradiction", "r" * 5000))
    assert len(run(client, [entry(1)]).reason) <= cj.MAX_REASON_CHARS


def test_reviewer_prompt_has_contradiction_section_and_keeps_review_contract():
    from pathlib import Path
    text = Path("eval/artifact_upload/prompts/profile-reviewer.v1.txt").read_text()
    assert "CONTRADICTION CHECK" in text and '"schema": "contradiction-check.v1"' in text
    assert '{"outcome": "no_contradiction" | "contradiction" | "unsure", "reason": ' in text
    assert "return\nexactly one JSON object conforming to profile-review.v1" in text
    assert "Unless the packet is a contradiction check" in text
