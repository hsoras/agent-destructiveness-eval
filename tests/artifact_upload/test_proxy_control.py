"""proxy_control: strict additive allowances, flat == proposer alias, explicit reviewer credit, single owner."""
import pytest

from eval.artifact_upload import proxy_control as pc


def test_allowance_normalization_and_rejection():
    assert pc._allowances({"cost_usd": 1, "pairs": 2}) == {"proposer": {"cost_usd": 1.0, "pairs": 2}}
    assert pc._allowances({"reviewer": {"pairs": 3}}) == {"reviewer": {"pairs": 3}}
    for bad in ({}, {"pairs": 0}, {"pairs": -1}, {"pairs": True}, {"cost_usd": float("nan")}, {"cost_usd": -0.1},
                {"tokens": 1}, {"proposer": {}}, {"reviewer": {"cost_usd": 0}}, {"judge": {"pairs": 1}}, "x", None):
        with pytest.raises(ValueError):
            pc._allowances(bad)


def test_queue_accumulates_and_legacy_callback_gets_flat_proposer_batch(tmp_path):
    seen = []
    with pc.start_control(tmp_path, seen.append) as control:
        control.queue({"pairs": 2})
        control.queue({"pairs": 3, "cost_usd": 0.5})
        control.apply_pending()
        control.queue({"reviewer": {"pairs": 1}})
        control.queue({"cost_usd": 1})
        control.apply_pending()
        assert control.apply_pending() is None
    assert seen == [{"pairs": 5, "cost_usd": 0.5}, {"proposer": {"cost_usd": 1.0}, "reviewer": {"pairs": 1}}]


def test_failed_callback_requeues_and_replenish_active_round_trip(tmp_path):
    calls = []

    def callback(batch):
        calls.append(batch)
        if len(calls) == 1:
            raise RuntimeError("busy")
    with pc.start_control(tmp_path, callback) as control:
        assert pc.replenish_active(tmp_path, {"reviewer": {"pairs": 2}, "proposer": {"cost_usd": 2}})["status"] == "queued"
        with pytest.raises(RuntimeError):
            control.apply_pending()
        control.apply_pending()
        with pytest.raises(RuntimeError):  # a second owner is refused
            pc.start_control(tmp_path, callback)
    assert calls[0] == calls[1] == {"proposer": {"cost_usd": 2.0}, "reviewer": {"pairs": 2}}
    with pytest.raises(RuntimeError):
        pc.replenish_active(tmp_path, {"pairs": 1})  # descriptor removed on close
