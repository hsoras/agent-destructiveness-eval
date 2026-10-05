"""Regression coverage for the SQLite image's pinned Inspect service patch."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("inspect_sandbox_tools")

from inspect_sandbox_tools._remote_tools._exec_remote._controller import Controller
from inspect_sandbox_tools.lifecycle import start_service


def test_terminal_poll_response_can_be_retried_after_response_loss(tmp_path, monkeypatch):
    """A duplicate terminal poll replays terminal state and exit status."""
    event_log = tmp_path / "service.jsonl"
    monkeypatch.setenv("INSPECT_SANDBOX_TOOLS_EVENT_LOG", str(event_log))
    service_id = start_service()

    async def scenario():
        controller = Controller()
        pid = await controller.submit("exit 23")
        first = None
        for _ in range(100):
            result = await controller.poll(pid, ack_seq=0)
            if result.state == "completed":
                first = result
                break
            await asyncio.sleep(0.02)
        assert first is not None
        assert first.exit_code == 23

        # Simulate a lost response: the host did not advance ack_seq.
        replay = await controller.poll(pid, ack_seq=0)
        assert replay.state == "completed"
        assert replay.exit_code == 23

        # Once acknowledged, another replay carries only terminal state.
        acknowledged = await controller.poll(pid, ack_seq=first.seq)
        assert acknowledged.state == "completed"
        assert acknowledged.exit_code == 23

    asyncio.run(scenario())
    records = [json.loads(line) for line in event_log.read_text().splitlines()]
    assert records[0]["event"] == "service_start"
    assert records[0]["startup_id"] == service_id
    assert any(row["event"] == "job_registered" for row in records)
    assert any(row["event"] == "job_process_exit_observed" and row["exit_code"] == 23 for row in records)
    assert any(
        row["event"] == "job_registry_transition"
        and row["to_registry"] == "terminal_retry_cache"
        for row in records
    )
    assert any(row["event"] == "job_terminal_poll_replayed" for row in records)


def test_killed_job_records_exit_status_and_removal_reason(tmp_path, monkeypatch):
    event_log = tmp_path / "service.jsonl"
    monkeypatch.setenv("INSPECT_SANDBOX_TOOLS_EVENT_LOG", str(event_log))
    start_service()

    async def scenario():
        controller = Controller()
        pid = await controller.submit("sleep 30")
        result = await controller.kill(pid, ack_seq=0)
        assert result.seq >= 0

    asyncio.run(scenario())
    records = [json.loads(line) for line in event_log.read_text().splitlines()]
    killed = next(row for row in records if row["event"] == "job_process_kill_observed")
    assert killed["exit_code"] is not None
    assert killed["state"] == "killed"
    assert any(
        row["event"] == "job_removed" and row["removal_reason"] == "explicit_kill"
        for row in records
    )
