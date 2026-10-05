#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 prepare IMAGE_MIB LABEL [home|runtime] | cleanup LABEL [home|runtime]" >&2
  exit 2
}

[[ $# -ge 2 && $# -le 4 ]] || usage
action="$1"
if [[ "$action" == prepare ]]; then
  [[ $# -eq 3 || $# -eq 4 ]] || usage
  image_mib="$2"
  label="$3"
  scope="${4:-home}"
  [[ "$image_mib" =~ ^[0-9]+$ && "$image_mib" -ge 32 ]] || usage
else
  [[ "$action" == cleanup && ( $# -eq 2 || $# -eq 3 ) ]] || usage
  label="$2"
  scope="${3:-home}"
fi
[[ "$scope" == home || "$scope" == runtime ]] || usage
[[ "$label" =~ ^[a-zA-Z0-9._-]+$ ]] || usage

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$scope" = runtime ]; then
  state_root="$repo_root/.scenario/sqlite-migration-artifacts/disk-runtime-state"
  volume="sqlite-runtime-opencode-$label"
  backing_volume="sqlite-runtime-backing-$label"
  preparer="sqlite-runtime-prep-$label"
  mount_dir="/mnt/opencode-runtime"
  probe_mount="/home/dev/.local/share/opencode"
  fs_label="opencode-runtime"
else
  state_root="$repo_root/.scenario/sqlite-migration-artifacts/disk-home-state"
  volume="sqlite-block-home-$label"
  backing_volume="sqlite-block-backing-$label"
  preparer="sqlite-block-prep-$label"
  mount_dir="/mnt/sqlite-home"
  probe_mount="/home/dev"
  fs_label="sqlite-home"
fi
state_file="$state_root/$label.json"
mkdir -p "$state_root"

if [[ "$action" == cleanup ]]; then
  [[ -f "$state_file" ]] || { echo "missing state: $state_file" >&2; exit 1; }
  values=()
  while IFS= read -r item; do values+=("$item"); done < <(python - "$state_file" <<'PY'
import json, sys
s=json.load(open(sys.argv[1]))
print(s.get('runtime_volume', s.get('home_volume')))
print(s.get('loop_device', ''))
print(s['preparer_container'])
print(s['backing_volume'])
PY
)
  home_volume="${values[0]}"
  loop_device="${values[1]}"
  preparer="${values[2]}"
  backing_volume="${values[3]}"
  if [[ -z "$loop_device" ]]; then
    loop_device="$(docker exec "$preparer" cat /backing/loop-device)"
  fi
  if docker volume inspect "$home_volume" >/dev/null 2>&1; then
    docker volume rm "$home_volume"
  fi
  docker exec "$preparer" sh -lc \
    "umount '$mount_dir' 2>/dev/null || true; losetup -d '$loop_device' 2>/dev/null || true"
  for _ in $(seq 1 50); do
    attached="$(docker exec "$preparer" losetup -j "/backing/$label.ext4" 2>/dev/null || true)"
    [[ -n "$attached" ]] || break
    sleep 0.1
  done
  if [[ -n "$attached" ]]; then
    echo "loop device remains attached: $loop_device ($attached)" >&2
    exit 1
  fi
  docker rm -f "$preparer" >/dev/null
  docker volume rm "$backing_volume" >/dev/null
  rm "$state_file"
  echo "removed bounded volume, detached $loop_device, and removed backing volume"
  exit 0
fi

home_volume="$volume"
loop_device=""
cleanup_partial() {
  docker volume rm -f "$home_volume" >/dev/null 2>&1 || true
  if [[ -z "$loop_device" ]] && docker exec "$preparer" test -f /backing/loop-device >/dev/null 2>&1; then
    loop_device="$(docker exec "$preparer" cat /backing/loop-device)"
  fi
  if [[ -n "$loop_device" ]]; then
    docker exec "$preparer" sh -lc \
      "umount '$mount_dir' 2>/dev/null || true; losetup -d '$loop_device' 2>/dev/null || true" >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do
      attached="$(docker exec "$preparer" losetup -j "/backing/$label.ext4" 2>/dev/null || true)"
      [[ -n "$attached" ]] || break
      sleep 0.1
    done
  fi
  docker rm -f "$preparer" >/dev/null 2>&1 || true
  docker volume rm -f "$backing_volume" >/dev/null 2>&1 || true
}
trap cleanup_partial EXIT

docker volume create "$backing_volume" >/dev/null
docker run -d --name "$preparer" --privileged --user 0:0 --network none \
  --mount "type=volume,source=$backing_volume,target=/backing,volume-nocopy" \
  --entrypoint sleep sqlite-migration-calibration:local infinity >/dev/null
docker exec "$preparer" sh -lc "set -eu
fallocate -l ${image_mib}M /backing/$label.ext4
mkfs.ext4 -q -F -m 0 -L $fs_label /backing/$label.ext4
device=\$(losetup --find --show /backing/$label.ext4)
printf '%s\\n' \"\$device\" > /backing/loop-device
mkdir -p $mount_dir
mount \"\$device\" $mount_dir
chown 1000:1000 $mount_dir
chmod 0755 $mount_dir
if [ "$scope" = runtime ]; then
  mkdir -p $mount_dir/state/opencode $mount_dir/tmp
  chown 1000:1000 $mount_dir/state
  chown 1000:1000 $mount_dir/state/opencode
  chown 1000:1000 $mount_dir/tmp
fi
df -B1 $mount_dir
umount $mount_dir
"
loop_device="$(docker exec "$preparer" cat /backing/loop-device)"
docker volume create --driver local \
  --opt type=ext4 --opt "device=$loop_device" "$home_volume" >/dev/null

# The runtime filesystem is nested below /home/dev, so its mountpoint must be
# present in the task filesystem before Docker applies the nested volume mount.
if [[ "$scope" == home ]]; then
  docker run --rm --network none --user 0:0 \
    --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
    --entrypoint sh sqlite-migration-linux-test:local -lc \
    'mkdir -p /home/dev/projects /home/dev/.local/share/opencode /home/dev/.local && rm -rf /home/dev/.local/state && ln -s share/opencode/state /home/dev/.local/state && chown 1000:1000 /home/dev/projects /home/dev/.local/share/opencode'
fi

capacity_json="$(docker run --rm --network none --user 1000:1000 \
  --mount "type=volume,source=$home_volume,target=$probe_mount,volume-nocopy" \
  --entrypoint python3 sqlite-migration-linux-test:local -c 'import json, os, sys; s=os.statvfs(sys.argv[1]); print(json.dumps({"capacity_bytes":s.f_blocks*s.f_frsize,"free_bytes":s.f_bavail*s.f_frsize,"filesystem":"ext4"}))' "$probe_mount")"
capacity_bytes="$(python -c 'import json,sys; print(json.load(sys.stdin)["capacity_bytes"])' <<<"$capacity_json")"
free_bytes="$(python -c 'import json,sys; print(json.load(sys.stdin)["free_bytes"])' <<<"$capacity_json")"
python - "$state_file" "$home_volume" "$loop_device" "$preparer" "$backing_volume" "$image_mib" "$capacity_bytes" "$free_bytes" "$scope" <<'PY'
import json, pathlib, sys
p, volume, device, prep, backing, image_mib, capacity, free, scope = sys.argv[1:]
pathlib.Path(p).write_text(json.dumps({
    ("runtime_volume" if scope == "runtime" else "home_volume"): volume,
    "loop_device": device, "preparer_container": prep, "backing_volume": backing,
    "image_mib": int(image_mib), "capacity_bytes": int(capacity),
    "initial_free_bytes": int(free), "filesystem": "ext4", "scope": scope,
}, indent=2) + "\n")
PY
trap - EXIT
if [[ "$scope" == runtime ]]; then
  printf 'SQLITE_MIGRATION_RUNTIME_VOLUME=%s\n' "$home_volume"
  printf 'SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES=%s\n' "$capacity_bytes"
  printf 'SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES=%s\n' "$free_bytes"
else
  printf 'SQLITE_MIGRATION_HOME_VOLUME=%s\n' "$home_volume"
  printf 'SQLITE_MIGRATION_CAPACITY_BYTES=%s\n' "$capacity_bytes"
fi
printf 'LOOP_DEVICE=%s\nBACKING_VOLUME=%s\nIMAGE_MIB=%s\nINITIAL_FREE_BYTES=%s\n' \
  "$loop_device" "$backing_volume" "$image_mib" "$free_bytes"
