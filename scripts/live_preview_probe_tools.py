"""Helpers for scripts/live_preview_probes.sh: per-time-point manifests and a summary.

  manifest <run_dir> <point> <manifest_out> <base> <direct_pin> <cls_model> <cls_pin>
      point is a checkpoint name or `end` (the last captured model request). Prints the
      selected source label. An unavailable view yields an empty manifest (no routes, no
      spend); the diagnostics CLI then records the reason.
  summary <out> [<out>.checkpoint-NAME ...]  print per time point direct answer + labels
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.artifact_upload.diagnostics import checkpoint_view

POINTS = ("after_request_before_attempt", "after_quota_before_action")


def _budget(tokens: int) -> dict:
    return {"calls": 1, "output_tokens": tokens, "timeout_seconds": 240,
            "request_bytes": 6291456, "response_bytes": 1048576}


def build_manifest(capture: Path, point: str, base: str, direct_pin: str, cls_model: str, cls_pin: str):
    """Return (manifest, selection) where selection is (flag, value) for the diagnostics CLI."""
    if point == "end":
        rows = [json.loads(line) for line in (capture / "researcher/model_views.jsonl").read_text().splitlines()]
        last = rows[-1]
        request = last["payload"]["request"]
        selection = ("--retrospective-event", last["event_id"])
    else:
        selection = ("--checkpoint", point)
        view = checkpoint_view(capture, point)
        if view.raw is None:
            return {}, selection, view.reason
        request = json.loads(view.raw)
    opts = {"prompt_cache_key": request["prompt_cache_key"], "usage": {"include": True}}
    route = lambda model, pin: {"model": model, "version": "unknown-live-preview", "base_url": base,
                                "api_key_env": "OPENROUTER_API_KEY", "provider_pin": pin, "options": opts}
    manifest = {"direct": {"route": route(request["model"], direct_pin), "budget": _budget(6144)},
                "classifier": {"route": route(cls_model, cls_pin), "budget": _budget(24000)}}
    return manifest, selection, None


def summarize(out: Path) -> dict:
    path = out / "researcher/diagnostics.jsonl"
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            record = json.loads(line)["payload"]
            result[record["measurement"]] = record
    return result


def format_summary(label: str, records: dict) -> str:
    lines = [f"== {label}"]
    direct, cls = records.get("direct"), records.get("classifier")
    if direct is None:
        lines.append("  direct: no record")
    elif direct["status"] == "completed":
        lines.append("  direct: " + " ".join(direct["answer"].split())[:200])
    else:
        lines.append(f"  direct: {direct['status']} ({direct.get('reason') or direct.get('error_type')})")
    if cls is None:
        lines.append("  classifier: no record")
    elif cls["status"] == "completed":
        labels = {k: v["label"] for k, v in cls["classification"].items() if isinstance(v, dict) and "label" in v}
        lines.append("  classifier: " + json.dumps(labels, sort_keys=True))
    else:
        lines.append(f"  classifier: {cls['status']} ({cls.get('reason') or cls.get('error_type')})")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv[0] == "manifest":
        run, point, out, base, direct_pin, cls_model, cls_pin = argv[1:8]
        manifest, (flag, value), reason = build_manifest(Path(run) / "capture", point, base, direct_pin,
                                                         cls_model, cls_pin)
        Path(out).write_text(json.dumps(manifest, indent=1))
        print(flag, value)
        if reason:
            print(f"unavailable: {reason}", file=sys.stderr)
        return 0
    if argv[0] == "summary":
        for out in argv[1:]:
            name = Path(out).name
            label = "retrospective-end" if ".checkpoint-" not in name else name.split(".checkpoint-", 1)[1]
            print(format_summary(label, summarize(Path(out))))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
