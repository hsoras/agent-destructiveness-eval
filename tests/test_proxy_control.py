import json
import os
import stat
import threading

import pytest

from eval.artifact_upload.proxy_control import DESCRIPTOR, replenish_active, start_control


def test_replenishment_queues_and_applies_only_at_explicit_boundary(tmp_path):
    applied = []
    boundary = threading.Event()
    control = start_control(tmp_path, lambda increments: (applied.append(increments), {"usage": 3, "totals": increments})[1])
    try:
        result = replenish_active(tmp_path, {"cost_usd": 1.25, "pairs": 2})
        assert result == {"status": "queued", "queued": True}
        assert applied == []
        assert control.apply_pending() == {"usage": 3, "totals": {"cost_usd": 1.25, "pairs": 2}}
        assert applied == [{"cost_usd": 1.25, "pairs": 2}]
        assert control.apply_pending() is None
    finally:
        control.close()
    assert not (tmp_path / DESCRIPTOR).exists()


def test_replenishment_accumulates_and_callback_failure_requeues(tmp_path):
    seen = []
    control = start_control(tmp_path, lambda increments: seen.append(increments))
    try:
        replenish_active(tmp_path, {"pairs": 1})
        replenish_active(tmp_path, {"pairs": 2, "cost_usd": 0.5})
        assert control.apply_pending() is None
        assert seen == [{"pairs": 3, "cost_usd": 0.5}]
    finally:
        control.close()

    attempts = []
    def callback(value):
        attempts.append(value)
        if len(attempts) == 1:
            raise RuntimeError("boundary busy")
        return value
    control = start_control(tmp_path, callback)
    try:
        replenish_active(tmp_path, {"pairs": 1})
        with pytest.raises(RuntimeError, match="boundary busy"):
            control.apply_pending()
        assert control.apply_pending() == {"pairs": 1}
    finally:
        control.close()


@pytest.mark.parametrize("allowance", [
    {}, {"pairs": 0}, {"pairs": 1.5}, {"cost_usd": float("inf")},
    {"seconds": 20}, {"output_tokens": 100}, {"max_call_cost_usd": 1},
])
def test_rejects_empty_nonpositive_malformed_or_per_call_allowances(tmp_path, allowance):
    control = start_control(tmp_path, lambda _: None)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            replenish_active(tmp_path, allowance)
    finally:
        control.close()


def test_descriptor_is_private_and_second_owner_is_refused(tmp_path):
    control = start_control(tmp_path, lambda _: None)
    try:
        descriptor = json.loads((tmp_path / DESCRIPTOR).read_text())
        assert descriptor["owner_pid"] == os.getpid()
        assert stat.S_IMODE((tmp_path / DESCRIPTOR).stat().st_mode) == 0o600
        with pytest.raises(RuntimeError, match="already exists"):
            start_control(tmp_path, lambda _: None)
    finally:
        control.close()


def test_missing_or_stale_descriptor_fails_without_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="no active proxy control owner"):
        replenish_active(tmp_path, {"pairs": 1})
    (tmp_path / DESCRIPTOR).write_text(json.dumps({"schema": "wrong"}))
    with pytest.raises(RuntimeError, match="invalid or stale"):
        replenish_active(tmp_path, {"pairs": 1})
