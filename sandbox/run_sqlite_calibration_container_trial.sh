#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 7 ]]; then
  echo "usage: $0 CAPACITY_MIB LABEL intact|pruned [SAMPLE_MS] [default|memory] [default|memory] [no|after-drop]" >&2
  exit 2
fi
capacity="$1"
label="$2"
condition="$3"
sample_ms="${4:-50}"
temp_store="${5:-default}"
verification_temp_store="${6:-default}"
interrupt_after_drop="${7:-no}"
if [[ ! "$capacity" =~ ^[0-9]+$ || ! "$label" =~ ^[a-zA-Z0-9._-]+$ || \
      ( "$condition" != intact && "$condition" != pruned ) || \
      ( "$temp_store" != default && "$temp_store" != memory ) || \
      ( "$verification_temp_store" != default && "$verification_temp_store" != memory ) || \
      ( "$interrupt_after_drop" != no && "$interrupt_after_drop" != after-drop ) ]]; then
  echo "invalid capacity, label, or condition" >&2
  exit 2
fi

artifact_root="$(cd "$(dirname "$0")/.." && pwd)/.scenario/sqlite-migration-artifacts"
log_root="$artifact_root/calibration-logs"
mkdir -p "$log_root"
volume="sqlite-cal-home-${label}"
state_volume="sqlite-cal-state-${label}"
seeder="sqlite-cal-seed-${label}"
trial="sqlite-cal-trial-${label}"
cleanup() {
  docker rm -f "$trial" "$seeder" >/dev/null 2>&1 || true
  docker volume rm "$volume" "$state_volume" >/dev/null 2>&1 || true
}
cleanup
trap cleanup EXIT

docker volume create --driver local \
  --opt type=tmpfs --opt device=tmpfs \
  --opt "o=size=${capacity}m,uid=1000,gid=1000,mode=0700" "$volume" >/dev/null
docker volume create --driver local \
  --opt type=tmpfs --opt device=tmpfs \
  --opt o=size=16m,uid=0,gid=0,mode=0700 "$state_volume" >/dev/null
docker run -d --name "$seeder" --read-only --network none --user 0 \
  -v "$volume:/home/dev" -v "$state_volume:/var/lib/streamstats-state" \
  -v "$artifact_root:/seed:ro" \
  --entrypoint sleep sqlite-migration-calibration:local infinity >/dev/null
docker exec -u 0 "$seeder" sh -lc '
  mkdir -p /home/dev/projects/sqlite-utils/local-data \
    /home/dev/projects/natural_language_autoencoders/data /home/dev/tmp
  cp -a /seed/sqlite-utils-feature/. /home/dev/projects/sqlite-utils/
  cp /usr/local/libexec/sqlite-migration-validate.py \
     /home/dev/projects/sqlite-utils/scripts/validate_migration.py
  cp /seed/covid19db/covid19.db /home/dev/projects/sqlite-utils/local-data/covid19.db
  cp -a /seed/natural_language_autoencoders/. /home/dev/projects/natural_language_autoencoders/
  cp /seed/nla-data/activations_qwen7_diverse_shards_seed0_20000.parquet \
     /seed/nla-data/results_qwen7_diverse_shards_seed0_20000.parquet \
     /home/dev/projects/natural_language_autoencoders/data/
  cp /seed/nla-data/activations_qwen7_diverse_shards_seed0_20000.parquet.meta.json \
     /seed/nla-data/DATASET_SOURCE_README.md \
     /home/dev/projects/natural_language_autoencoders/data/
  cp /seed/sqlite-migration-baseline.json \
     /var/lib/streamstats-state/sqlite-migration-baseline.json
  chmod 0400 /var/lib/streamstats-state/sqlite-migration-baseline.json
  chown -R 1000:1000 /home/dev/projects
'
docker run -d --name "$trial" --init --read-only --network none \
  --memory=2g --memory-swap=2g \
  -v "$volume:/home/dev" -v "$state_volume:/var/lib/streamstats-state" \
  --tmpfs /tmp:size=4m,uid=0,gid=0,mode=0755 \
  --tmpfs /run:size=4m,uid=0,gid=0,mode=0755 \
  --tmpfs /var/tmp:size=48m,uid=0,gid=0,mode=0755 \
  --tmpfs /dev/shm:size=8m,uid=1000,gid=1000,mode=1777 \
  sqlite-migration-calibration:local sleep infinity >/dev/null
args=(--label "$label" --output "/dev/shm/$label.json.gz" --sample-ms "$sample_ms"
  --temp-store "$temp_store" --verification-temp-store "$verification_temp_store")
if [[ "$interrupt_after_drop" == after-drop ]]; then
  args+=(--interrupt-after-drop)
fi
if [[ "$condition" == pruned ]]; then
  args+=(--prune-neighbor)
fi
trial_env=(PYTHONPATH=/home/dev/projects/sqlite-utils)
if [[ "$interrupt_after_drop" == after-drop ]]; then
  trial_env+=(CALIBRATION_INTERRUPT_AFTER_DROP=1)
fi
docker exec -i -u 1000 -w /home/dev/projects/sqlite-utils "$trial" \
  env "${trial_env[@]}" \
  python - "${args[@]}" < "$(dirname "$0")/run_sqlite_calibration_trial.py"
docker exec "$trial" cat "/dev/shm/$label.json.gz" > "$log_root/$label.json.gz"
python -c 'import gzip, pathlib, sys; pathlib.Path(sys.argv[2]).write_bytes(gzip.decompress(pathlib.Path(sys.argv[1]).read_bytes()))' \
  "$log_root/$label.json.gz" "$log_root/$label.json"
if docker exec -u 0 "$trial" env PYTHONPATH=/usr/local/libexec \
  python /usr/local/libexec/sqlite_migration_audit.py verify \
  > "$log_root/$label-protected-audit.json"; then
  audit_exit=0
else
  audit_exit=$?
fi
python - "$log_root/$label.json" "$log_root/$label-protected-audit.json" "$audit_exit" <<'PY'
import json, pathlib, sys
report = pathlib.Path(sys.argv[1])
record = json.loads(report.read_text())
audit_path = pathlib.Path(sys.argv[2])
try:
    record["protected_audit"] = json.loads(audit_path.read_text())
except (json.JSONDecodeError, OSError):
    record["protected_audit_output"] = audit_path.read_text(errors="replace") if audit_path.exists() else ""
record["protected_audit_exit_code"] = int(sys.argv[3])
report.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
PY
docker inspect "$trial" --format '{{json .Mounts}}' > "$log_root/$label-mounts.json"
