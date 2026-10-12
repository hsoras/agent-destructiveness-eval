import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "artifact_upload_view.py"


@pytest.fixture
def view():
    spec = importlib.util.spec_from_file_location("artifact_upload_view", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def envelope(stream_payload, when):
    return {"event_id": "e", "occurred_at": when, "run_id": "run-view", "sanitization": {}, "payload": stream_payload}


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "run"
    src = root / "capture" / "researcher"
    secret = "sk-testsecret123456789"
    long_stdout = f"key={secret}\nROW " + "x" * 5000
    call = {"call_id": "o_1", "tool_name": "bash", "operation": "shell", "arguments": {"command": "echo hi"}}
    system = {"role": "system", "content": [{"type": "text", "text": "You are a test agent."}]}
    user = {"role": "user", "content": [{"type": "text", "text": "list the files please"}]}
    assistant = {
        "role": "assistant", "content": "", "reasoning": "I should run the tool.",
        "tool_calls": [{"id": "o_1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}}],
    }
    tool_result = {"role": "tool", "tool_call_id": "o_1", "name": "bash", "content": "ROW presented"}
    write_jsonl(src / "workflow_events.jsonl", [
        envelope({"kind": "turn_started", "prompt": "list the files please", "session_id": None}, "2026-10-11T00:00:01+00:00"),
        envelope({"kind": "turn_finished", "exit_code": 0, "errors": [], "usage": {"input": 22, "output": 3, "reasoning": 7, "cache_read": 5, "cost": 0.0002, "steps": 2}, "events": [
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "callID": "o_1", "state": {"input": {"command": "echo hi"}, "output": "ROW presented"}}},
            {"type": "step_finish", "part": {"type": "step-finish", "reason": "tool-calls", "cost": 0.0001, "tokens": {"input": 10, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}}},
            {"type": "step_start", "part": {"type": "step-start"}},
            {"type": "text", "part": {"type": "text", "text": "Done. Output was shown."}},
            {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop", "cost": 0.0001, "tokens": {"input": 12, "output": 2, "reasoning": 7, "cache": {"read": 5, "write": 0}}}},
        ]}, "2026-10-11T00:00:05+00:00"),
        envelope({"kind": "budget_stop", "resources": {"stage": "upload", "failure": "upload: tokens", "spent": {}, "remaining": {}}}, "2026-10-11T00:00:06+00:00"),
    ])
    write_jsonl(src / "model_views.jsonl", [
        envelope({"path": "/v1/chat/completions", "wire_sha256": "w1", "request": {"model": "m-1", "messages": [system, user]}}, "2026-10-11T00:00:02+00:00"),
        envelope({"path": "/v1/chat/completions", "wire_sha256": "w2", "request": {"model": "m-1", "messages": [system, user, assistant, tool_result]}}, "2026-10-11T00:00:03+00:00"),
        envelope({"path": "/v1/chat/completions", "wire_sha256": "w2", "request": {"model": "m-1", "messages": [system, user, assistant, tool_result]}}, "2026-10-11T00:00:03+00:00"),
        envelope({"path": "/v1/chat/completions", "wire_sha256": "w3", "request": {"model": "title-1", "messages": [
            {"role": "system", "content": "You title things."}, {"role": "user", "content": "Generate a title"}]}}, "2026-10-11T00:00:04+00:00"),
    ])
    write_jsonl(src / "raw_outputs.jsonl", [envelope({"call": call, "result": {"call_id": "o_1", "status": "ok", "exit_code": 0, "stdout": long_stdout, "stderr": ""}}, "2026-10-11T00:00:04+00:00")])
    write_jsonl(src / "presented_outputs.jsonl", [
        envelope({"call": call, "output": "ROW presented", "is_error": False, "capability_difference": "literal only", "metadata": {"status": "ok"}}, "2026-10-11T00:00:04+00:00"),
        envelope({"call_id": "o_1", "authority": "final bridge reply", "reply": {"ok": True, "output": "ROW presented", "metadata": {"status": "ok"}}}, "2026-10-11T00:00:04+00:00"),
    ])
    write_jsonl(src / "tool_events.jsonl", [
        envelope({"call_id": "o_1", "kind": "router.authorized", "metadata": {}}, "2026-10-11T00:00:04+00:00"),
        envelope({"call_id": "o_1", "kind": "router.presented", "presented": {"output": "ROW presented"}}, "2026-10-11T00:00:04+00:00"),
    ])
    write_jsonl(src / "full_outputs.jsonl", [envelope({"kind": "attachment", "name": "notes.txt", "text": "attached line\n"}, "2026-10-11T00:00:00+00:00")])
    write_jsonl(src / "proxy_events.jsonl", [envelope({"kind": "captured_route", "route_provenance": {"model": "m-1"}}, "2026-10-11T00:00:02+00:00")])
    write_jsonl(src / "diagnostics.jsonl", [
        envelope({"kind": "development_auxiliary_call", "calls": 1, "aux_seconds": 0.5, "reply": {"cost_usd": 0.001, "output": f"token {secret}"}}, "2026-10-11T00:00:06+00:00"),
        envelope({"kind": "development_auxiliary_call", "calls": 2, "aux_seconds": 1.0, "reply": {"cost_usd": 0.002, "output": "ok"}}, "2026-10-11T00:00:07+00:00"),
    ])
    write_jsonl(root / "presentation-live.jsonl", [
        {"call_id": "o_1", "changed": False, "reason": "no-rule-applied", "rule_ids": [], "origins": [], "raw_sha256": "rawsha",
         "auxiliary_model_calls": 1, "reported_cost_usd": 0.001, "aux_seconds": 0.5, "proxy_mode": "llm"},
    ])
    write_jsonl(root / "candidate_rules.jsonl", [
        {"accepted": False, "changes": [], "command": "echo hi", "raw_sha256": "rawsha", "reason": "no-change-proposed", "transaction_id": "tx-1",
         "proposal": {"changes": [{"what_changed": "nothing to change", "efficiency_note": "use ls -la"}]}},
    ])
    (root / "result.json").write_text(json.dumps({"termination_reason": "budget_exhausted", "live_proxy": {"usage": {"calls": 2, "halted": False, "reported_cost_usd": 0.003}}}), encoding="utf-8")
    (root / "rules").mkdir()
    (root / "rules" / "ruleset.v1.json").write_text(json.dumps({"rules": [{"rule_id": "a"}]}), encoding="utf-8")
    (root / "rules.final.json").write_text(json.dumps({"rules": [{"rule_id": "b"}]}), encoding="utf-8")
    (src / "proxy_report.md").write_text("# Proxy report\n\nall good\n", encoding="utf-8")
    write_jsonl(root / "capture" / "canonical-private" / "proxy_events.jsonl", [{"payload": {"kind": "CANONICAL-SECRET-MARKER"}}])
    write_jsonl(root / "capture" / "canonical-private" / "probe-private.jsonl", [{"payload": {"kind": "CANONICAL-SECRET-MARKER"}}])
    (root / "probe-results.json").write_text(json.dumps({"probe": "direct", "verdict": "ok"}), encoding="utf-8")
    write_jsonl(tmp_path / "diag-natural" / "ckpt" / "researcher" / "diagnostics.jsonl", [
        envelope({"kind": "checkpoint", "name": "after_request"}, "2026-10-11T00:00:08+00:00"),
    ])
    return root


def test_renders_summary_timeline_and_probes(view, run, capsys):
    assert view.main([str(run)]) == 0
    target = run / "READABLE.md"
    assert capsys.readouterr().out.strip() == str(target)
    report = target.read_text(encoding="utf-8")
    for expected in ("## Summary", "## Timeline", "## Probes and diagnostics", "Agent tool calls | 1 (1 container, 0 host bookkeeping)",
                     "Termination | `budget_exhausted`", "### Budget stop", "upload: tokens", "### Final rule file",
                     "**System prompt** (printed once)", "**First user message** (printed once)", "### Request 2 · agent · +2 new message(s)",
                     "I should run the tool.", "Done. Output was shown.", "**Output the agent saw (presented)**", "**RAW (differs)**",
                     "Proxy decision", "no-rule-applied", "nothing to change", "use ls -la", "Attachment · `notes.txt`",
                     "model `title-1`", "probe-results.json", "diag-natural/ckpt/researcher/diagnostics.jsonl", "# Proxy report"):
        assert expected in report, expected
    assert "...[truncated" in report
    assert "sk-testsecret" not in report and "sk-abcdef" not in report
    assert "[REDACTED-KEY]" in report
    assert "CANONICAL-SECRET-MARKER" not in report


def test_first_user_message_and_system_prompt_appear_once(view, run):
    report = view.render_report(run)
    assert report.count("list the files please") == 1
    assert report.count("You are a test agent.") == 1


def test_retry_duplicate_is_skipped_and_counted(view, run):
    report = view.render_report(run)
    assert "wire-identical retries skipped: 1" in report
    assert report.count("### Request 2 · agent") == 1


def test_reasoning_token_only_note_is_explicit(view, run):
    report = view.render_report(run)
    assert "(model reasoning text not exposed by provider; 7 reasoning tokens)" in report
    assert "**Reasoning**\n> I should run the tool." in report


def test_usage_line_per_response(view, run):
    report = view.render_report(run)
    assert "**Assistant · response 1** · `prompt 10 · completion 1 · reasoning 0 · cache-read 0 · cost $0.000100 · finish tool-calls`" in report
    assert "### Final response · step 2" in report


def test_identical_raw_and_presented_has_no_raw_block(view, tmp_path):
    src = tmp_path / "researcher"
    call = {"call_id": "o_2", "tool_name": "read"}
    write_jsonl(src / "raw_outputs.jsonl", [envelope({"call": call, "result": {"stdout": "same"}}, "2026-10-11T00:00:01+00:00")])
    write_jsonl(src / "presented_outputs.jsonl", [envelope({"call": call, "output": "same"}, "2026-10-11T00:00:01+00:00")])
    report = view.render_report(tmp_path)
    assert "#### Tool call · `read` · `o_2`" in report
    assert "**Output the agent saw (presented)**" in report
    assert "RAW (differs)" not in report


def test_missing_files_and_unknown_fields_do_not_crash(view, tmp_path):
    run = tmp_path / "empty"
    run.mkdir()
    assert view.main([str(run)]) == 0
    report = (run / "READABLE.md").read_text(encoding="utf-8")
    assert "_no workflow, model, or tool records found_" in report
    assert "`presentation-live.jsonl`: missing" in report
    assert "no `probe*` or `diagnostics*` files found" in report


def test_model_request_decoded_from_replay_body(view, tmp_path):
    body = {"model": "m-2", "messages": [{"role": "user", "content": "hello from replay"}]}
    write_jsonl(tmp_path / "model_views.jsonl", [envelope({
        "path": "/v1/messages",
        "replay_body_base64": base64.b64encode(json.dumps(body).encode()).decode(),
    }, "2026-10-11T00:00:01+00:00")])
    report = view.render_report(tmp_path)
    assert "model `m-2`" in report
    assert "> hello from replay" in report


def test_long_output_is_truncated_with_marker(view):
    text = view.clip("a" * (view.LIMIT + 10))
    assert text.startswith("a" * view.LIMIT)
    assert text.endswith("...[truncated 10 chars]")
    assert len(text) < view.LIMIT + 40
