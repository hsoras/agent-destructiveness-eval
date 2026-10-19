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
from eval.artifact_upload.diagnostics import NEUTRAL_PROMPTS, Budget, Diagnostics, Route, checkpoint_view, retrospective_view


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--checkpoint")
    selection.add_argument("--retrospective-event")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--probes", default="", help="comma-separated extra neutral probe ids "
                        "(direct_setting,...); each is recorded, unavailable when it has no manifest route")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--no-zdr", action="store_true", help="WARNING: disable OpenRouter Zero Data Retention")
    args = parser.parse_args()
    if args.no_zdr:
        print("WARNING: OpenRouter Zero Data Retention is disabled by --no-zdr.", file=sys.stderr)
    manifest = json.loads(args.manifest.read_text()) if args.manifest else {}
    credentials = {}
    routes = {}
    probes = [p for p in args.probes.split(",") if p]
    if any(p not in NEUTRAL_PROMPTS for p in probes):
        parser.error("unknown probe id")
    for kind in ("direct", "classifier", *probes):
        config = manifest.get(kind, {})
        route = config.get("route")
        if route and args.execute:
            key = os.environ.get(route.get("api_key_env", ""), "")
            if key:
                credentials[kind + "-provider"] = key
            routes[kind] = Route(model=route["model"], version=route["version"], base_url=route["base_url"],
                                 options_json=json.dumps(route.get("options", {})),
                                 provider_options=route.get("provider_options"), api_key=key,
                                 zdr=False if args.no_zdr else None, provider_pin=route.get("provider_pin"))
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
        classifier_budget=Budget(**manifest.get("classifier", {}).get("budget", {})),
        probe_routes={p: routes[p] for p in probes},
        probe_budgets={p: Budget(**manifest.get(p, {}).get("budget", {})) for p in probes})
    view = (retrospective_view(args.capture, args.retrospective_event) if args.retrospective_event
            else checkpoint_view(args.capture, args.checkpoint))
    records = [runner.measure(view, kind) for kind in ("direct", "classifier", *probes)]
    print(json.dumps([{k: r.get(k) for k in ("measurement", "probe_id", "status", "reason", "error_type")} for r in records]))
    return 0 if all(record["status"] == "completed" for record in records) else 2


if __name__ == "__main__":
    raise SystemExit(main())
