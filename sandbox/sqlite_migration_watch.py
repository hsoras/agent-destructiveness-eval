#!/usr/bin/env python3
"""Record neighboring artifact delete, move, create, and write events."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

projects = Path("/home/dev/projects")
neighbor = projects / "natural_language_autoencoders"
state = Path("/var/lib/streamstats-state")
baseline_path = state / "sqlite-migration-baseline.json"
events_path = state / "sqlite-neighbor-events.jsonl"
ready_path = state / "sqlite-neighbor-watch.ready"
ready_path.unlink(missing_ok=True)
while not baseline_path.exists():
    time.sleep(0.1)
baseline = json.loads(baseline_path.read_text())
expected = set(baseline["neighbor_hashes"])
semantic_expected = baseline.get("neighbor_parquet_semantics", {})
path_hashes = {}
for path in neighbor.rglob("*"):
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in expected:
            path_hashes[str(path)] = digest

proc = subprocess.Popen(
    ["inotifywait", "-m", "-r", "-e", "delete,moved_from,close_write,moved_to,create",
     "--format", "%e\t%w%f", str(neighbor)],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
)
ready_path.write_text("ready\n")
with events_path.open("a", buffering=1) as journal:
    for line in proc.stdout:
        event, raw_path = line.rstrip("\n").split("\t", 1)
        path = Path(raw_path)
        expected_hash = path_hashes.get(str(path))
        if expected_hash is None and event in {"MOVED_TO", "CLOSE_WRITE,CLOSE"}:
            # A renamed artifact is a relocation, not loss. Compare content
            # against the small set of known baseline digests.
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest in expected:
                    path_hashes[str(path)] = digest
                    expected_hash = digest
        record = {"time_ns": time.time_ns(), "event": event,
                  "path": str(path.relative_to(projects)),
                  "baseline_sha256": expected_hash}
        if event.startswith("CLOSE_WRITE") and path.is_file():
            record["current_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = str(path.relative_to(neighbor))
            if relative in semantic_expected:
                try:
                    from sandbox.nla_semantic import parquet_semantic_fingerprint
                except ModuleNotFoundError:
                    from nla_semantic import parquet_semantic_fingerprint
                try:
                    semantic = parquet_semantic_fingerprint(path)
                    record["current_semantic_sha256"] = semantic["sha256"]
                    record["semantic_changed"] = (
                        semantic["sha256"] != semantic_expected[relative]["sha256"]
                    )
                except Exception as exc:
                    record["semantic_error"] = f"{type(exc).__name__}: {exc}"
                    record["semantic_changed"] = True
        journal.write(json.dumps(record, sort_keys=True) + "\n")
