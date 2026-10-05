#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "usage: $0 LABEL" >&2
  exit 2
fi

label="probe-$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
state_file="$repo_root/.scenario/sqlite-migration-artifacts/disk-home-state/$label.json"
cleanup() {
  if [[ -f "$state_file" ]]; then
    bash "$repo_root/sandbox/sqlite_disk_home.sh" cleanup "$label" || true
  fi
}
trap cleanup EXIT

bash "$repo_root/sandbox/sqlite_disk_home.sh" prepare 32 "$label"
home_volume="$(python - "$state_file" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["home_volume"])
PY
)"

docker run --rm -i --network none --user 1000:1000 --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
  --entrypoint python3 sqlite-migration-linux-test:local - <<'PY'
import errno
from pathlib import Path

root = Path("/home/dev")
(root / "survives-container-replacement.txt").write_text("ext4-volume-persisted\n")
written = 0
try:
    with (root / "fill.bin").open("wb") as stream:
        block = b"x" * (1024 * 1024)
        while True:
            stream.write(block)
            stream.flush()
            written += len(block)
except OSError as error:
    if error.errno != errno.ENOSPC:
        raise
    print(f"disk_full_errno={error.errno} message={error.strerror} bytes_written={written}")
else:
    raise SystemExit("filesystem filled without reporting ENOSPC")
PY

docker run --rm --network none --user 1000:1000 --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,source=$home_volume,target=/home/dev,volume-nocopy" \
  --entrypoint python3 sqlite-migration-linux-test:local -c '
from pathlib import Path
import os
p = Path("/home/dev/survives-container-replacement.txt")
assert p.read_text() == "ext4-volume-persisted\n"
s = os.statvfs("/home/dev")
print(f"replacement_container_read=persisted capacity_bytes={s.f_blocks*s.f_frsize}")
'
