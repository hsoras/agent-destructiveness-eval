"""Per-request settlement: reservations are released as responses complete (run 06 regression)."""
import json
import pytest
from eval.artifact_upload.workflow import Allowance, BudgetExhausted, Ledger, Policy, parse_request_usage

POLICY = Policy(upload=Allowance(3600, 100, 900_000, 5_000_000), output_tokens=8192, microusd_per_token=0.2)
BODY = json.dumps({'model': 'm', 'max_tokens': 8192, 'messages': [{'role': 'user', 'content': 'x' * 100_000}]}).encode()


def sse(prompt, cached, completion, cost=0.001):
    usage = {'prompt_tokens': prompt, 'completion_tokens': completion,
             'prompt_tokens_details': {'cached_tokens': cached}, 'cost': cost}
    return ('data: ' + json.dumps({'choices': []}) + '\n\ndata: ' + json.dumps({'usage': usage}) + '\n\ndata: [DONE]\n').encode()


def upload_ledger():
    ledger = Ledger(POLICY)
    ledger.enter('upload')
    return ledger


def test_run06_many_cached_turns_do_not_accumulate_reservations():
    ledger, reported = upload_ledger(), 0
    for _ in range(35):  # each reservation is ~33k; 35 unsettled would exceed 900k
        reservation = ledger.provider(BODY)
        assert ledger.settle(reservation, sse(prompt=14_000, cached=11_000, completion=500))
        reported += 3_000 + 500
        assert ledger.spent['upload']['tokens'] == reported
    assert ledger.failure is None
    inflight = ledger.provider(BODY)
    assert ledger.spent['upload']['tokens'] == reported + inflight.tokens  # reported + only the in-flight reservation


def test_unsettled_reservations_still_trip_the_ceiling():
    ledger = upload_ledger()
    with pytest.raises(BudgetExhausted):
        for _ in range(35):
            ledger.provider(BODY)
    assert ledger.failure == 'upload: tokens'


@pytest.mark.parametrize('usage', [b'', b'not json', b'data: {"choices": []}\n', sse(5, 0, 1)[:0],
                                   json.dumps({'usage': {'prompt_tokens': -1, 'completion_tokens': 2}}).encode(),
                                   json.dumps({'usage': {'prompt_tokens': 'x', 'completion_tokens': 2}}).encode()])
def test_missing_or_malformed_usage_keeps_reservation_fail_closed(usage):
    ledger = upload_ledger()
    reservation = ledger.provider(BODY)
    before = dict(ledger.spent['upload'])
    assert parse_request_usage(usage) is None
    assert not ledger.settle(reservation, usage)
    assert ledger.spent['upload'] == before


def test_settle_is_idempotent_and_json_body_supported():
    ledger = upload_ledger()
    reservation = ledger.provider(BODY)
    body = json.dumps({'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}).encode()
    assert ledger.settle(reservation, body)
    assert not ledger.settle(reservation, body)
    assert ledger.spent['upload']['tokens'] == 120
