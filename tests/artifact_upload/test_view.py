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
    write_jsonl(src / "workflow_events.jsonl", [
        envelope({"kind": "turn_started", "prompt": "list the files please", "session_id": None}, "2026-10-11T00:00:01+00:00"),
        envelope({"kind": "turn_finished", "exit_code": 0, "usage": {"input": 3}, "errors": [], "events": [
            {"type": "text", "part": {"type": "reasoning", "text": "I should run the tool."}},
            {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "callID": "o_1", "state": {"input": {"command": "echo hi"}}}},
            {"type": "text", "part": {"type": "text", "text": "Done. Output was shown."}},
        ]}, "2026-10-11T00:00:05+00:00"),
    ])
    write_jsonl(src / "model_views.jsonl", [envelope({
        "path": "/v1/chat/completions",
        "request": {"model": "m-1", "messages": [{"role": "user", "content": [{"type": "text", "text": "list the files please"}]}]},
    }, "2026-10-11T00:00:02+00:00")])
    write_jsonl(src / "raw_outputs.jsonl", [envelope({"call": call, "result": {"call_id": "o_1", "status": "ok", "exit_code": 0, "stdout": long_stdout, "stderr": ""}}, "2026-10-11T00:00:04+00:00")])
    write_jsonl(src / "presented_outputs.jsonl", [envelope({"call": call, "output": "ROW presented", "is_error": False, "capability_difference": "literal only", "metadata": {"status": "ok"}}, "2026-10-11T00:00:04+00:00")])
    write_jsonl(src / "tool_events.jsonl", [envelope({"call_id": "o_1", "kind": "router.presented", "presented": {"output": "ROW presented"}}, "2026-10-11T00:00:04+00:00")])
    write_jsonl(src / "full_outputs.jsonl", [envelope({"kind": "attachment", "name": "notes.txt", "text": "attached line\n"}, "2026-10-11T00:00:00+00:00")])
    write_jsonl(src / "proxy_events.jsonl", [envelope({"kind": "captured_route", "route_provenance": {"model": "m-1"}}, "2026-10-11T00:00:02+00:00")])
    write_jsonl(src / "diagnostics.jsonl", [envelope({"kind": "auxiliary_measurement", "protocol_valid": True}, "2026-10-11T00:00:06+00:00")])
    write_jsonl(src / "presentation-live.jsonl", [{"occurred_at": "2026-10-11T00:00:03+00:00", "payload": {"kind": "live_rule_hit"}}])
    write_jsonl(src / "candidate_rules.jsonl", [{"rule": "r1", "unexpected_new_field": [1, 2]}])
    (src / "proxy_report.md").write_text("# Proxy report\n\nall good\n", encoding="utf-8")
    (root / "probe-results.json").write_text(json.dumps({"probe": "direct", "verdict": "ok"}), encoding="utf-8")
    return root


def test_renders_sections_and_writes_report(view, run, capsys):
    assert view.main([str(run)]) == 0
    target = run / "READABLE.md"
    assert capsys.readouterr().out.strip() == str(target)
    report = target.read_text(encoding="utf-8")
    for expected in ("## Timeline", "#### User prompt", "list the files please", "### Model request", "#### Thinking",
                     "I should run the tool.", "#### Assistant output", "Done. Output was shown.", "#### Tool call · `bash` · `o_1`",
                     "**RAW output**", "**PRESENTED output**", "**Diff note:**", "Attachment input · `notes.txt`",
                     "Proxy event · `captured_route`", "## Live proxy", "live_rule_hit", "candidate_rules.jsonl (1 rows)",
                     "# Proxy report", "## Diagnostics and probes", "auxiliary_measurement", "probe-results.json"):
        assert expected in report, expected
    assert "...[truncated" in report
    assert "sk-testsecret" not in report
    assert "[REDACTED-KEY]" in report


def test_identical_raw_and_presented_has_no_diff_note(view, tmp_path):
    src = tmp_path / "researcher"
    call = {"call_id": "o_2", "tool_name": "read"}
    write_jsonl(src / "raw_outputs.jsonl", [envelope({"call": call, "result": {"stdout": "same"}}, "2026-10-11T00:00:01+00:00")])
    write_jsonl(src / "presented_outputs.jsonl", [envelope({"call": call, "output": "same"}, "2026-10-11T00:00:01+00:00")])
    report = view.render_report(tmp_path)
    assert "#### Tool record without turn part · `o_2`" in report
    assert "**PRESENTED output**" in report
    assert "Diff note" not in report


def test_missing_files_and_unknown_fields_do_not_crash(view, tmp_path):
    run = tmp_path / "empty"
    run.mkdir()
    assert view.main([str(run)]) == 0
    report = (run / "READABLE.md").read_text(encoding="utf-8")
    assert "_no workflow, model, or tool records found_" in report
    assert "`presentation-live.jsonl`: missing" in report
    assert "no `*probe*` files present" in report


def test_model_request_decoded_from_replay_body(view, tmp_path):
    body = {"model": "m-2", "messages": [{"role": "user", "content": "hello from replay"}]}
    write_jsonl(tmp_path / "model_views.jsonl", [envelope({
        "path": "/v1/messages",
        "replay_body_base64": base64.b64encode(json.dumps(body).encode()).decode(),
    }, "2026-10-11T00:00:01+00:00")])
    report = view.render_report(tmp_path)
    assert "Model: `m-2`" in report
    assert "> hello from replay" in report


def test_long_output_is_truncated_with_marker(view):
    text = view.clip("a" * (view.LIMIT + 10))
    assert text.startswith("a" * view.LIMIT)
    assert text.endswith("...[truncated 10 chars]")
    assert len(text) < view.LIMIT + 40
