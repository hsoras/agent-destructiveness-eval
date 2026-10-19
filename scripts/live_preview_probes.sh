#!/bin/bash
# Live-preview probes: direct answer + classifier against one saved capture, at three
# time points: checkpoint after_request_before_attempt, checkpoint
# after_quota_before_action, and the end (last captured model request).
# Layout: end probe at <out> (unchanged); checkpoint probes at
# <out>.checkpoint-<name>/; each with its own <dir>.manifest.json; summary printed and
# written to <out>.summary.txt. A missing/invalid checkpoint is recorded as
# unavailable (no spend) and the script continues.
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
TOOLS="$REPO_DIR/scripts/live_preview_probe_tools.py"
POINTS="after_request_before_attempt after_quota_before_action end"
DIRS=()

guard() {
  u="$(spend)"; echo "usage before: $u"
  python3 -c "import sys; sys.exit(0 if float('$u')<float('$SPEND_CAP_USD') else 1)" \
    || { echo "over spend guard ($SPEND_CAP_USD)"; return 1; }
}

for point in $POINTS; do
  if [ "$point" = end ]; then DIR="$OUT"; else DIR="$OUT.checkpoint-$point"; fi
  DIRS+=("$DIR")
  echo "--- $point -> $DIR"
  guard || { echo "stopping: spend guard"; break; }
  # Manifest repeats the original model/provider for the direct probe; the classifier
  # uses its own route. An unavailable checkpoint gets an empty manifest (zero dispatch).
  SEL="$(python3 "$TOOLS" manifest "$RUN" "$point" "$DIR.manifest.json" "$OPENROUTER_BASE" \
          "$DIRECT_PIN" "$CLASSIFIER_MODEL" "$CLASSIFIER_PIN")" \
    || { echo "manifest failed for $point; skipping"; continue; }
  rm -rf "$DIR"
  # Non-zero exit just means a measurement was unavailable/limited/error; keep going.
  # shellcheck disable=SC2086
  "$PYTHON" scripts/artifact_upload_diagnostics.py --capture "$RUN/capture" --output "$DIR" \
    --run-id "$(basename "$RUN")" $SEL --manifest "$DIR.manifest.json" --execute 2>&1 | tail -c 700 || true
  sleep 5; echo "usage after: $(spend)"
done
python3 "$TOOLS" summary "${DIRS[@]}" | tee "$OUT.summary.txt"
