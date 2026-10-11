"""Run isolated diagnostics against a saved capture, never start a behavioral run.

Default invocation records missing routes without network calls. Configure routes
explicitly via a frozen JSON manifest and --execute to enable auxiliary calls.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.artifact_upload.diagnostics import Budget, Diagnostics, Route, checkpoint_view, retrospective_view


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--checkpoint")
    selection.add_argument("--retrospective-event")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text()) if args.manifest else {}
    credentials = {}
    routes = {}
    for kind in ("direct", "classifier"):
        config = manifest.get(kind, {})
        route = config.get("route")
        if route and args.execute:
            key = os.environ.get(route.get("api_key_env", ""), "")
            if key:
                credentials[kind + "-provider"] = key
            routes[kind] = Route(model=route["model"], version=route["version"], base_url=route["base_url"],
                                 options_json=json.dumps(route.get("options", {})), api_key=key)
        else:
            routes[kind] = None
    # Declare capture credentials by environment variable name, never put secrets in the manifest.
    for credential_id, env_name in manifest.get("capture_credential_env", {}).items():
        if not os.environ.get(env_name):
            parser.error("declared capture credential environment variable unavailable")
        credentials[credential_id] = os.environ[env_name]
    runner = Diagnostics(capture=args.capture, output=args.output, run_id=args.run_id,
        direct_route=routes["direct"], classifier_route=routes["classifier"], credentials=credentials,
        direct_budget=Budget(**manifest.get("direct", {}).get("budget", {})),
        classifier_budget=Budget(**manifest.get("classifier", {}).get("budget", {})))
    view = (retrospective_view(args.capture, args.retrospective_event) if args.retrospective_event
            else checkpoint_view(args.capture, args.checkpoint))
    records = [runner.measure(view, kind) for kind in ("direct", "classifier")]
    print(json.dumps([{k: r.get(k) for k in ("measurement", "status", "reason", "error_type")} for r in records]))
    return 0 if all(record["status"] == "completed" for record in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
