#!/bin/bash
# Live-preview probes: direct answer + classifier against one saved capture.
#
# usage: scripts/live_preview_probes.sh <run_dir (live-*/live-*)> <out_dir>
#
# Environment (all optional except where noted):
#   REPO_DIR            repo to run from (default: parent of this script's directory)
#   PYTHON              interpreter (default: $REPO_DIR/.venv/bin/python)
#   ENV_FILE            file exporting OPENROUTER_API_KEY; sourced, never printed
#   OPENROUTER_API_KEY  required if ENV_FILE is unset (read by the probe, never echoed)
#   SPEND_CAP_USD       refuse to run if key usage is at or above this (default 2.7)
#   CLASSIFIER_MODEL    default deepseek/deepseek-v4.1-flash
#   CLASSIFIER_PIN      default decart/fp4
#   DIRECT_PIN          default inference-net/fp4 (the original run's pinned provider)
#   OPENROUTER_BASE     default https://openrouter.ai/api/v1
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <run_dir (live-*/live-*)> <out_dir>" >&2
  exit 2
fi
RUN="$1"; OUT="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
SPEND_CAP_USD="${SPEND_CAP_USD:-2.7}"
CLASSIFIER_MODEL="${CLASSIFIER_MODEL:-deepseek/deepseek-v4.1-flash}"
CLASSIFIER_PIN="${CLASSIFIER_PIN:-decart/fp4}"
DIRECT_PIN="${DIRECT_PIN:-inference-net/fp4}"
OPENROUTER_BASE="${OPENROUTER_BASE:-https://openrouter.ai/api/v1}"

if [ -n "${ENV_FILE:-}" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY or ENV_FILE}"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR"

spend() {
  curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" "$OPENROUTER_BASE/key" \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['usage'])"
}
u="$(spend)"; echo "usage before: $u"
python3 -c "import sys; sys.exit(0 if float('$u')<float('$SPEND_CAP_USD') else 1)" \
  || { echo "over spend guard ($SPEND_CAP_USD)"; exit 1; }

# Build the manifest from the last captured model request so the direct probe
# repeats the original model and provider; the classifier uses its own route.
EV="$(python3 - "$RUN" "$OUT" "$OPENROUTER_BASE" "$DIRECT_PIN" "$CLASSIFIER_MODEL" "$CLASSIFIER_PIN" <<'PY'
import json,sys
run,out,base,direct_pin,cls_model,cls_pin=sys.argv[1:7]
rows=[json.loads(l) for l in open(run+'/capture/researcher/model_views.jsonl')]
last=rows[-1]; req=last['payload']['request']
opts={"prompt_cache_key":req["prompt_cache_key"],"usage":{"include":True}}
direct_route={"model":req["model"],"version":"unknown-live-preview","base_url":base,"api_key_env":"OPENROUTER_API_KEY","provider_pin":direct_pin,"options":opts}
route={"model":cls_model,"version":"unknown-live-preview","base_url":base,"api_key_env":"OPENROUTER_API_KEY","provider_pin":cls_pin,"options":opts}
b=lambda t:{"calls":1,"output_tokens":t,"timeout_seconds":240,"request_bytes":6291456,"response_bytes":1048576}
json.dump({"direct":{"route":direct_route,"budget":b(6144)},"classifier":{"route":route,"budget":b(24000)}},open(out+".manifest.json","w"),indent=1)
print(last["event_id"])
PY
)"
rm -rf "$OUT"
"$PYTHON" scripts/artifact_upload_diagnostics.py --capture "$RUN/capture" --output "$OUT" \
  --run-id "$(basename "$RUN")" --retrospective-event "$EV" --manifest "$OUT.manifest.json" --execute 2>&1 | tail -c 700
sleep 5; echo "usage after: $(spend)"
