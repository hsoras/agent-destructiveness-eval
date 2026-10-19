"""Three-time-point live-preview probes: manifests, layout, unavailable checkpoints. No network."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

from eval.artifact_upload.diagnostics import Diagnostics, Route, checkpoint_view, retrospective_view
from tests.artifact_upload.test_diagnostics import MODEL, labels, linked_capture, response

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("probe_tools", ROOT / "scripts/live_preview_probe_tools.py")
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)

ARGS = ("https://openrouter.ai/api/v1", "direct/fp4", "cls-model", "cls/fp4")


def run_dir(tmp_path, rec):
    run = tmp_path / "run"
    run.mkdir()
    (run / "capture").symlink_to(rec.root)
    return run


def test_manifest_per_time_point(tmp_path):
    rec, event, _, raw, _ = linked_capture(tmp_path)
    run = run_dir(tmp_path, rec)
    for point, flag, value in (("after_request_before_attempt", "--checkpoint", "after_request_before_attempt"),
                               ("after_quota_before_action", "--checkpoint", "after_quota_before_action"),
                               ("end", "--retrospective-event", event)):
        out = tmp_path / f"{point}.manifest.json"
        assert tools.main(["manifest", str(run), point, str(out), *ARGS]) == 0
        manifest = json.loads(out.read_text())
        assert manifest["direct"]["route"]["model"] == MODEL
        assert manifest["direct"]["route"]["provider_pin"] == "direct/fp4"
        assert manifest["classifier"]["route"]["model"] == "cls-model"
        assert manifest["classifier"]["route"]["provider_pin"] == "cls/fp4"
        assert manifest["direct"]["route"]["options"]["prompt_cache_key"] == "k"
        assert manifest["direct"]["budget"]["calls"] == 1 and manifest["classifier"]["budget"]["calls"] == 1
        _, selection, reason = tools.build_manifest(rec.root, point, *ARGS)
        assert selection == (flag, value) and reason is None


def test_missing_checkpoint_gives_empty_manifest_and_records_unavailable(tmp_path):
    rec, *_ = linked_capture(tmp_path)
    for name in ("termination", "never_recorded"):
        manifest, selection, reason = tools.build_manifest(rec.root, name, *ARGS)
        assert manifest == {} and selection == ("--checkpoint", name) and reason
    # CLI dry run (no --execute): records unavailable for both measurements, exits 2, no network.
    out = tmp_path / "probe.checkpoint-termination"
    (tmp_path / "m.json").write_text("{}")
    proc = subprocess.run(["python3", str(ROOT / "scripts/artifact_upload_diagnostics.py"), "--capture", str(rec.root),
                           "--output", str(out), "--run-id", "x", "--checkpoint", "termination",
                           "--manifest", str(tmp_path / "m.json"), "--execute"],
                          capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert proc.returncode == 2
    records = tools.summarize(out)
    assert {k: v["status"] for k, v in records.items()} == {"direct": "unavailable", "classifier": "unavailable"}
    text = tools.format_summary("termination", records)
    assert "no exact captured model view" in text


def test_probe_sets_run_with_fake_client_and_summarize(tmp_path):
    body = {"model": MODEL, "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "Upload"}],
            "tools": [{"type": "function"}], "tool_choice": "auto", "stream": True, "prompt_cache_key": "k",
            "usage": {"include": True}}
    rec, event, _, raw, _ = linked_capture(tmp_path, body=body)
    calls = []

    def request(route, body, budget):
        calls.append(route.model)
        return response(json.dumps(labels()) if route.model == "cls-model" else "x" * 300, model=route.model)

    dirs = []
    for point in ("after_request_before_attempt", "after_quota_before_action", "end"):
        manifest, (flag, value), _ = tools.build_manifest(rec.root, point, "http://127.0.0.1:9/v1", "d", "cls-model", "c")
        out = tmp_path / ("probe" if point == "end" else f"probe.checkpoint-{point}")
        dirs.append(out)
        direct = Route(MODEL, "v", "http://127.0.0.1:9/v1", json.dumps(manifest["direct"]["route"]["options"]))
        classifier = Route("cls-model", "v", "http://127.0.0.1:9/v1")
        diag = Diagnostics(capture=rec.root, output=out, run_id="r", direct_route=direct,
                           classifier_route=classifier, request=request)
        view = (checkpoint_view(rec.root, value) if flag == "--checkpoint"
                else retrospective_view(rec.root, value))
        for kind in ("direct", "classifier"):
            got = diag.measure(view, kind)
            assert got["status"] == "completed", (point, kind, got.get("reason"), got.get("error_type"))
    assert len(calls) == 6
    summary = tools.format_summary("end", tools.summarize(dirs[2]))
    assert "direct: " + "x" * 200 in summary and "x" * 201 not in summary
    assert '"session": "uncertain"' in summary
    assert tools.main(["summary", *map(str, dirs)]) == 0


def test_provenance_base_url_root_matches_api_root():
    # Real captures record the upstream root (.../api); probe routes use the API root (.../api/v1).
    from eval.artifact_upload.diagnostics import _base_root
    assert _base_root("https://openrouter.ai/api/v1") == _base_root("https://openrouter.ai/api/") == "https://openrouter.ai/api"
    assert _base_root("http://127.0.0.1:9/v1") != _base_root("http://127.0.0.1:10/v1")
