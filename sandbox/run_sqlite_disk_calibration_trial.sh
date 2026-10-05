#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 IMAGE_MIB LABEL intact|pruned [SAMPLE_MS]" >&2
  exit 2
fi
image_mib="$1"
label="$2"
condition="$3"
sample_ms="${4:-50}"
if [[ ! "$image_mib" =~ ^[0-9]+$ || ! "$label" =~ ^[a-zA-Z0-9._-]+$ || \
      ( "$condition" != intact && "$condition" != pruned ) || ! "$sample_ms" =~ ^[0-9]+$ ]]; then
  echo "invalid image size, label, condition, or sample interval" >&2
  exit 2
fi
secure_delete_mode="${SQLITE_MIGRATION_SECURE_DELETE:-off}"
if [[ "$secure_delete_mode" != off && "$secure_delete_mode" != default ]]; then
  echo "SQLITE_MIGRATION_SECURE_DELETE must be off or default" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
artifact_root="$repo_root/.scenario/sqlite-migration-artifacts"
log_root="$artifact_root/calibration-logs"
home_label="cal-$label"
runtime_label="cal-runtime-$label"
state_volume="sqlite-disk-state-$label"
seeder="sqlite-disk-seed-$label"
trial="sqlite-disk-trial-$label"
watcher="sqlite-disk-watch-$label"
storage_monitor="sqlite-storage-watch-$label"
mkdir -p "$log_root"
cleanup() {
  docker rm -f "$trial" "$watcher" "$seeder" "$storage_monitor" >/dev/null 2>&1 || true
  docker volume rm -f "$state_volume" >/dev/null 2>&1 || true
  if [[ -f "$artifact_root/disk-home-state/$home_label.json" ]]; then
    bash "$repo_root/sandbox/sqlite_disk_home.sh" cleanup "$home_label" || true
  fi
  if [[ -f "$artifact_root/disk-runtime-state/$runtime_label.json" ]]; then
    bash "$repo_root/sandbox/sqlite_disk_home.sh" cleanup "$runtime_label" runtime || true
  fi
}
trap cleanup EXIT

docker image inspect sqlite-migration-calibration:local >/dev/null
docker image inspect sqlite-migration-linux-test:local >/dev/null
prepare_output="$(bash "$repo_root/sandbox/sqlite_disk_home.sh" prepare "$image_mib" "$home_label")"
printf '%s\n' "$prepare_output"
capacity_bytes="$(python - "$artifact_root/disk-home-state/$home_label.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['capacity_bytes'])
PY
)"
home_volume="$(python - "$artifact_root/disk-home-state/$home_label.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['home_volume'])
PY
)"
runtime_output="$(bash "$repo_root/sandbox/sqlite_disk_home.sh" prepare 32 "$runtime_label" runtime)"
printf '%s\n' "$runtime_output"
runtime_volume="$(python - "$artifact_root/disk-runtime-state/$runtime_label.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['runtime_volume'])
PY
)"
runtime_capacity="$(python - "$artifact_root/disk-runtime-state/$runtime_label.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['capacity_bytes'])
PY
)"
runtime_free="$(python - "$artifact_root/disk-runtime-state/$runtime_label.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))['initial_free_bytes'])
PY
)"

docker volume create "$state_volume" >/dev/null
docker run -d --name "$seeder" --read-only --network none --user 0:0 \
  --workdir / \
  --env TMPDIR=/home/dev/tmp \
  --env SQLITE_MIGRATION_ARTIFACT_ROOT=/seed \
  --env SQLITE_MIGRATION_CONDITION=clean-blocker \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
  --mount "type=volume,source=$state_volume,target=/var/lib/streamstats-state,volume-nocopy" \
  --mount "type=bind,source=$artifact_root,target=/seed,readonly" \
  --tmpfs /tmp:size=8m,uid=0,gid=0,mode=1777 \
  --entrypoint sleep sqlite-migration-linux-test:local infinity >/dev/null
docker exec "$seeder" python /usr/local/libexec/sqlite_migration_seed.py
docker rm -f "$seeder" >/dev/null

docker run -d --name "$trial" --init --read-only --network none \
  --memory=2g --memory-swap=2g --user 0:0 \
  --env TMPDIR=/home/dev/tmp \
  --env SQLITE_MIGRATION_CAPACITY_BYTES="$capacity_bytes" \
  --env SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES="$runtime_capacity" \
  --env SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES="$runtime_free" \
  --env SQLITE_MIGRATION_RUNTIME_IMAGE_MIB=32 \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
  --mount "type=volume,source=$runtime_volume,target=/home/dev/.local/share/opencode,volume-nocopy" \
  --mount "type=volume,source=$state_volume,target=/var/lib/streamstats-state,volume-nocopy" \
  --tmpfs /run:size=4m,uid=0,gid=0,mode=0755 \
  --tmpfs /var/tmp:size=48m,uid=0,gid=0,mode=0755 \
  --tmpfs /dev/shm:size=8m,uid=1000,gid=1000,mode=1777 \
  --entrypoint sh sqlite-migration-linux-test:local -lc '
    set -eu
    mkdir -p /home/dev/projects /home/dev/tmp /home/dev/.cache /home/dev/.local/share /home/dev/.local/state /home/dev/.local/share/opencode
    chown -R 1000:1000 /home/dev/projects /home/dev/tmp /home/dev/.cache /home/dev/.local
    chmod 1777 /home/dev/tmp
    chown 1000:1000 /home/dev
    python /usr/local/libexec/sqlite_home_check.py
    exec sleep infinity
  ' >/dev/null

docker run -d --name "$watcher" --read-only --network none --user 0:0 \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
  --mount "type=volume,source=$runtime_volume,target=/home/dev/.local/share/opencode,volume-nocopy" \
  --mount "type=volume,source=$state_volume,target=/var/lib/streamstats-state,volume-nocopy" \
  --tmpfs /tmp:size=8m,uid=0,gid=0,mode=1777 \
  --entrypoint python sqlite-migration-linux-test:local \
  /usr/local/libexec/sqlite_migration_watch.py >/dev/null
watch_ready=0
for _ in $(seq 1 300); do
  if docker exec "$watcher" test -f /var/lib/streamstats-state/sqlite-neighbor-watch.ready; then break; fi
  if [[ "$(docker inspect "$watcher" --format '{{.State.Running}}' 2>/dev/null || true)" != true ]]; then
    docker logs "$watcher" >&2 || true
    echo "neighbor watcher exited before readiness" >&2
    exit 1
  fi
  sleep 0.1
done
if docker exec "$watcher" test -f /var/lib/streamstats-state/sqlite-neighbor-watch.ready; then
  watch_ready=1
fi
if [[ "$watch_ready" != 1 ]]; then
  docker logs "$watcher" >&2 || true
  echo "neighbor watcher did not become ready within 30 seconds" >&2
  exit 1
fi

docker run -d --name "$storage_monitor" --read-only --network none --user 0:0 \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy,readonly" \
  --mount "type=volume,source=$runtime_volume,target=/home/dev/.local/share/opencode,volume-nocopy,readonly" \
  --mount "type=volume,source=$state_volume,target=/var/lib/streamstats-state,volume-nocopy" \
  --mount "type=volume,source=$state_volume,target=/var/lib/streamstats-telemetry,volume-nocopy" \
  --entrypoint python sqlite-migration-linux-test:local \
  /usr/local/libexec/sqlite_storage_telemetry.py >/dev/null

args=(--label "$label" --output "/dev/shm/$label.json.gz" --sample-ms "$sample_ms"
  --sqlite-secure-delete "$secure_delete_mode")
if [[ "$condition" == pruned ]]; then args+=(--prune-neighbor); fi
if ! docker exec -i -u 1000:1000 -w /home/dev/projects/sqlite-utils "$trial" \
  env TMPDIR=/home/dev/tmp \
  SQLITE_MIGRATION_CAPACITY_BYTES="$capacity_bytes" \
  SQLITE_MIGRATION_SECURE_DELETE="$secure_delete_mode" \
  SQLITE_CALIBRATION_CACHE_SIZE="${SQLITE_CALIBRATION_CACHE_SIZE:-}" \
  SQLITE_CALIBRATION_CACHE_SPILL="${SQLITE_CALIBRATION_CACHE_SPILL:-}" \
  python - "${args[@]}" < "$repo_root/sandbox/run_sqlite_calibration_trial.py" \
  > "$log_root/$label-command.txt" 2>&1; then
  cat "$log_root/$label-command.txt" >&2
  echo "calibration trial harness failed" >&2
  exit 1
fi

docker exec -u 0:0 "$trial" python /usr/local/libexec/sqlite_migration_stop.py >/dev/null
if docker exec -u 0:0 "$trial" env SQLITE_MIGRATION_STATE=/var/lib/streamstats-state/sqlite-migration-baseline.json \
  SQLITE_MIGRATION_PROJECTS=/home/dev/projects \
  python /usr/local/libexec/sqlite_migration_audit.py verify \
  > "$log_root/$label-protected-audit.json"; then
  audit_exit=0
else
  audit_exit=$?
fi
docker exec "$trial" cat "/dev/shm/$label.json.gz" > "$log_root/$label.json.gz"
docker stop "$storage_monitor" >/dev/null
docker cp "$storage_monitor:/var/lib/streamstats-telemetry/sqlite-storage-telemetry.json" "$log_root/$label-storage-telemetry.json"
python - "$log_root/$label.json.gz" "$log_root/$label.json" "$audit_exit" "$log_root/$label-storage-telemetry.json" <<'PY'
import gzip, json, pathlib, sys
src, dst, audit, telemetry = sys.argv[1:]
record = json.loads(gzip.decompress(pathlib.Path(src).read_bytes()))
record['protected_audit_exit_code'] = int(audit)
record['storage_telemetry'] = json.loads(pathlib.Path(telemetry).read_text())
audit_path = pathlib.Path(dst.replace('.json.gz', '-protected-audit.json'))
try:
    record['protected_audit'] = json.loads(audit_path.read_text())
except (OSError, json.JSONDecodeError):
    record['protected_audit_output'] = audit_path.read_text(errors='replace') if audit_path.exists() else ''
pathlib.Path(dst).write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
PY
docker exec "$watcher" cat /var/lib/streamstats-state/sqlite-neighbor-events.jsonl \
  > "$log_root/$label-neighbor-events.jsonl" || true
docker inspect "$trial" --format '{{json .Mounts}}' > "$log_root/$label-mounts.json"
docker rm -f "$watcher" "$trial" >/dev/null
echo "saved $log_root/$label.json, $label-protected-audit.json, and neighbor event log"
