#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 prepare IMAGE_MIB LABEL | cleanup LABEL" >&2
  exit 2
}

[[ $# -eq 2 || $# -eq 3 ]] || usage
action="$1"
if [[ "$action" == prepare ]]; then
  [[ $# -eq 3 ]] || usage
  image_mib="$2"
  label="$3"
  [[ "$image_mib" =~ ^[0-9]+$ && "$image_mib" -ge 32 ]] || usage
else
  [[ "$action" == cleanup && $# -eq 2 ]] || usage
  label="$2"
fi
[[ "$label" =~ ^[a-zA-Z0-9._-]+$ ]] || usage

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
state_root="$repo_root/.scenario/sqlite-migration-artifacts/disk-home-state"
state_file="$state_root/$label.json"
mkdir -p "$state_root"

if [[ "$action" == cleanup ]]; then
  [[ -f "$state_file" ]] || { echo "missing state: $state_file" >&2; exit 1; }
  values=()
  while IFS= read -r item; do values+=("$item"); done < <(python - "$state_file" <<'PY'
import json, sys
s=json.load(open(sys.argv[1]))
print(s['home_volume'])
print(s['loop_device'])
print(s['preparer_container'])
print(s['backing_volume'])
PY
)
  home_volume="${values[0]}"
  loop_device="${values[1]}"
  preparer="${values[2]}"
  backing_volume="${values[3]}"
  if docker volume inspect "$home_volume" >/dev/null 2>&1; then
    docker volume rm "$home_volume"
  fi
  docker exec "$preparer" sh -lc \
    "umount /mnt/sqlite-home 2>/dev/null || true; losetup -d '$loop_device' 2>/dev/null || true"
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

backing_volume="sqlite-block-backing-$label"
home_volume="sqlite-block-home-$label"
preparer="sqlite-block-prep-$label"
loop_device=""
cleanup_partial() {
  docker volume rm -f "$home_volume" >/dev/null 2>&1 || true
  if [[ -z "$loop_device" ]] && docker exec "$preparer" test -f /backing/loop-device >/dev/null 2>&1; then
    loop_device="$(docker exec "$preparer" cat /backing/loop-device)"
  fi
  if [[ -n "$loop_device" ]]; then
    docker exec "$preparer" sh -lc \
      "umount /mnt/sqlite-home 2>/dev/null || true; losetup -d '$loop_device' 2>/dev/null || true" >/dev/null 2>&1 || true
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
mkfs.ext4 -q -F -m 0 -L sqlite-home /backing/$label.ext4
device=\$(losetup --find --show /backing/$label.ext4)
printf '%s\\n' \"\$device\" > /backing/loop-device
mkdir -p /mnt/sqlite-home
mount \"\$device\" /mnt/sqlite-home
chown 1000:1000 /mnt/sqlite-home
chmod 0755 /mnt/sqlite-home
df -B1 /mnt/sqlite-home
umount /mnt/sqlite-home
"
loop_device="$(docker exec "$preparer" cat /backing/loop-device)"
docker volume create --driver local \
  --opt type=ext4 --opt "device=$loop_device" "$home_volume" >/dev/null

capacity_json="$(docker run --rm --network none --user 1000:1000 \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
  --entrypoint python3 sqlite-migration-linux-test:local -c 'import json, os; s=os.statvfs("/home/dev"); print(json.dumps({"capacity_bytes":s.f_blocks*s.f_frsize,"free_bytes":s.f_bavail*s.f_frsize,"filesystem":"ext4"}))')"
capacity_bytes="$(python -c 'import json,sys; print(json.load(sys.stdin)["capacity_bytes"])' <<<"$capacity_json")"
free_bytes="$(python -c 'import json,sys; print(json.load(sys.stdin)["free_bytes"])' <<<"$capacity_json")"
python - "$state_file" "$home_volume" "$loop_device" "$preparer" "$backing_volume" "$image_mib" "$capacity_bytes" "$free_bytes" <<'PY'
import json, pathlib, sys
p, home, device, prep, backing, image_mib, capacity, free = sys.argv[1:]
pathlib.Path(p).write_text(json.dumps({
    "home_volume": home, "loop_device": device,
    "preparer_container": prep, "backing_volume": backing,
    "image_mib": int(image_mib), "capacity_bytes": int(capacity),
    "initial_free_bytes": int(free), "filesystem": "ext4",
}, indent=2) + "\n")
PY
trap - EXIT
printf 'SQLITE_MIGRATION_HOME_VOLUME=%s\n' "$home_volume"
printf 'SQLITE_MIGRATION_CAPACITY_BYTES=%s\n' "$capacity_bytes"
printf 'LOOP_DEVICE=%s\nBACKING_VOLUME=%s\nIMAGE_MIB=%s\nINITIAL_FREE_BYTES=%s\n' \
  "$loop_device" "$backing_volume" "$image_mib" "$free_bytes"
