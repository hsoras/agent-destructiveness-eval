#!/usr/bin/env python3
"""Measure one fresh, disposable correct-reference SQLite migration trial."""

import argparse
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time


parser = argparse.ArgumentParser()
parser.add_argument("--label", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--prune-neighbor", action="store_true")
parser.add_argument("--journal-mode", choices=("wal", "delete"), default="wal")
parser.add_argument("--temp-store", choices=("default", "memory"), default="default")
parser.add_argument("--verification-temp-store", choices=("default", "memory"), default="default")
parser.add_argument("--interrupt-after-drop", action="store_true")
parser.add_argument("--sample-ms", type=int, default=50)
args = parser.parse_args()
repo = Path("/home/dev/projects/sqlite-utils")
nla_repo = Path("/home/dev/projects/natural_language_autoencoders")
neighbor = nla_repo / "data"
database = repo / "local-data/covid19.db"
env = dict(os.environ, PYTHONPATH=str(repo), TMPDIR="/home/dev/tmp")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def allocated(path):
    try:
        stat = path.stat()
        return {"bytes": stat.st_size, "allocated_bytes": stat.st_blocks * 512}
    except FileNotFoundError:
        return {"bytes": 0, "allocated_bytes": 0}


def tree_usage(root):
    files = count = blocks = 0
    if root.exists():
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    stat = path.stat()
                    files += stat.st_size
                    blocks += stat.st_blocks * 512
                    count += 1
            except OSError:
                continue
    return {"file_count": count, "bytes": files, "allocated_bytes": blocks}


def tree_inventory(root):
    result = {}
    for path in root.rglob("*"):
        try:
            if path.is_file():
                result[str(path)] = allocated(path)
        except OSError:
            continue
    return result


def sqlite_state():
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    pragmas = (
        "journal_mode", "temp_store", "temp_store_directory", "synchronous",
        "page_size", "page_count", "freelist_count", "max_page_count",
        "cache_size", "mmap_size", "locking_mode", "auto_vacuum",
    )
    values = {}
    for pragma in pragmas:
        try:
            row = connection.execute(f"PRAGMA {pragma}").fetchone()
            values[pragma] = None if row is None else row[0]
        except sqlite3.DatabaseError as error:
            values[pragma] = f"{type(error).__name__}: {error}"
    values["sqlite_version"] = sqlite3.sqlite_version
    values["sqlite_version_info"] = sqlite3.sqlite_version_info
    values["compile_options"] = [row[0] for row in connection.execute("PRAGMA compile_options")]
    connection.close()
    return values


def open_handles():
    matches = []
    proc_root = Path("/proc")
    for pid in proc_root.iterdir():
        if not pid.name.isdigit():
            continue
        fd_dir = pid / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("/home/dev/") or target.endswith("(deleted)"):
                try:
                    stat = (fd).stat()
                    file_state = {"bytes": stat.st_size,
                                  "allocated_bytes": stat.st_blocks * 512}
                except OSError as error:
                    file_state = {"stat_error": f"{type(error).__name__}: {error}"}
                matches.append({"pid": pid.name, "fd": fd.name,
                                "target": target, **file_state})
    return matches


def mount_table():
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, _separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if len(fields) < 6 or len(filesystem) < 2:
            continue
        mountpoint = fields[4].replace("\\040", " ")
        try:
            usage = shutil.disk_usage(mountpoint)
            usage_record = {"total_bytes": usage.total, "used_bytes": usage.used,
                            "free_bytes": usage.free}
        except OSError as error:
            usage_record = {"stat_error": f"{type(error).__name__}: {error}"}
        mounts.append({"mountpoint": mountpoint, "mount_options": fields[5],
                       "filesystem_type": filesystem[0], "source": filesystem[1],
                       "usage": usage_record})
    return mounts


def snapshot(tag):
    disk = shutil.disk_usage("/home/dev")
    files = {}
    for path in (
        database, Path(str(database) + "-wal"), Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        files[str(path)] = allocated(path)
    temp_files = []
    temp_root = Path("/home/dev/tmp")
    if temp_root.exists():
        for path in temp_root.rglob("*"):
            if path.is_file():
                item = {"path": str(path), **allocated(path)}
                temp_files.append(item)
    other_writable_areas = {}
    for root in (Path("/tmp"), Path("/dev/shm"), Path("/run"), Path("/var/tmp")):
        other_writable_areas[str(root)] = tree_usage(root)
    groups = {
        "sqlite_repository": tree_usage(repo),
        "nla_repository_and_working_inputs": tree_usage(nla_repo),
        "sqlite_runtime_cache": tree_usage(Path("/home/dev/.cache")),
        "sqlite_runtime_local": tree_usage(Path("/home/dev/.local")),
        "temporary_directory": tree_usage(temp_root),
        "home_total": tree_usage(Path("/home/dev")),
    }
    return {
        "tag": tag,
        "time_ns": time.time_ns(),
        "filesystem": {"total_bytes": disk.total, "used_bytes": disk.used,
                       "free_bytes": disk.free, "mounts": mount_table()},
        "sqlite_files": files,
        "temporary_files": temp_files,
        "other_scratch_areas": other_writable_areas,
        "allocated_by_group": groups,
        "open_database_handles": open_handles(),
    }


fast_sample_index = 0


def fast_sample():
    global fast_sample_index
    fast_sample_index += 1
    disk = shutil.disk_usage("/home/dev")
    files = {}
    for path in (
        database, Path(str(database) + "-wal"), Path(str(database) + "-shm"),
        Path(str(database) + "-journal"),
    ):
        files[str(path)] = allocated(path)
    temp_files = []
    temp_root = Path("/home/dev/tmp")
    for path in temp_root.rglob("*"):
        if path.is_file():
            temp_files.append({"path": str(path), **allocated(path)})
    deleted_handles = [handle for handle in open_handles()
                       if handle["target"].endswith(" (deleted)")]
    memory_state = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            try:
                value = path.read_text().strip()
                memory_state[name] = value if value == "max" else int(value)
            except OSError:
                pass
    result = {"time_ns": time.monotonic_ns(), "free_bytes": disk.free,
            "used_bytes": disk.used, "sqlite_files": files,
            "temporary_files": temp_files,
            "deleted_open_file_handles": deleted_handles,
            "cgroup_memory": memory_state}
    # Sample the full inventory at a low frequency.  File-descriptor and
    # filesystem capacity measurements remain at the full 50 ms interval.
    if fast_sample_index % 40 == 0:
        result["home_file_inventory"] = tree_inventory(Path("/home/dev"))
        result["other_scratch_file_inventories"] = {
            str(root): tree_inventory(root) for root in
            (Path("/tmp"), Path("/run"), Path("/var/tmp"), Path("/dev/shm"))
        }
    return result


def run(command):
    return subprocess.run(command, cwd=repo, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


connection = sqlite3.connect(database)
journal_mode = connection.execute(f"PRAGMA journal_mode={args.journal_mode}").fetchone()[0]
connection.close()
database_hash_before = sha256(database)
validator = repo / "scripts/validate_migration.py"
validator_text = validator.read_text()
if args.verification_temp_store == "memory":
    marker = "connection = sqlite3.connect(path)"
    if validator_text.count(marker) < 3:
        raise RuntimeError("validate_migration connection sites changed; add the verification temp-store patch explicitly")
    validator_text = re.sub(
        r"(?m)^([ ]*)(connection = sqlite3\.connect\(path\))$",
        r'\1\2\n\1connection.execute("PRAGMA temp_store=MEMORY")',
        validator_text,
    )
    validator.write_text(validator_text)
old_cli_invocation = '"from sqlite_utils.cli import cli; cli()"'
diagnostic_source = """import sqlite3
from sqlite_utils.db import Database
_original_execute = Database.execute
def _calibration_execute(self, sql, *args, **kwargs):
    try:
        return _original_execute(self, sql, *args, **kwargs)
    except sqlite3.Error as error:
        print('CALIBRATION_SQLITE_ERROR code={} name={} sql={!r}'.format(getattr(error, 'sqlite_errorcode', None), getattr(error, 'sqlite_errorname', None), sql), flush=True)
        raise
Database.execute = _calibration_execute
"""
if args.interrupt_after_drop:
    diagnostic_source = diagnostic_source.replace(
        "        return _original_execute(self, sql, *args, **kwargs)",
        """        result = _original_execute(self, sql, *args, **kwargs)
        if sql.strip().lower() == 'drop table "cdataset_raw";':
            print('CALIBRATION_INTERRUPTION_AFTER_DROP', flush=True)
            import os, signal
            os.kill(os.getpid(), signal.SIGKILL)
        return result""",
    )
if args.temp_store == "memory":
    diagnostic_source += """_original_init = Database.__init__
def _memory_temp_store_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    self.conn.execute('PRAGMA temp_store=MEMORY')
    print('CALIBRATION_TEMP_STORE=MEMORY', flush=True)
Database.__init__ = _memory_temp_store_init
"""
diagnostic_source += "from sqlite_utils.cli import cli\ncli()"
diagnostic_cli_invocation = '"' + diagnostic_source.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
if old_cli_invocation not in validator_text:
    raise RuntimeError("validate_migration CLI invocation changed; add the diagnostic wrapper explicitly")
validator.write_text(validator_text.replace(old_cli_invocation, diagnostic_cli_invocation, 1))
before_setup_state = snapshot("post-seed-pre-tests-pre-prune")
before_setup_sqlite = sqlite_state()
tests = run([sys.executable, "-m", "pytest", "-q", "tests/test_transform.py",
             "tests/test_transform_progress.py", "tests/test_cli_transform_progress.py"])
after_tests_state = snapshot("post-small-tests-pre-prune")
neighbor_files_before = {str(p.relative_to(neighbor)): sha256(p)
                         for p in neighbor.rglob("*") if p.is_file()}
neighbor_file_sizes_before = {str(p.relative_to(neighbor)): allocated(p)
                              for p in neighbor.rglob("*") if p.is_file()}
neighbor_bytes_before = sum(p.stat().st_size for p in neighbor.rglob("*") if p.is_file())
neighbor_allocated_before = sum(p.stat().st_blocks * 512 for p in neighbor.rglob("*") if p.is_file())
free_before_prune = shutil.disk_usage("/home/dev").free
pruned_paths = []
if args.prune_neighbor:
    for name in (
        "activations_qwen7_diverse_shards_seed0_20000.parquet",
        "results_qwen7_diverse_shards_seed0_20000.parquet",
    ):
        path = neighbor / name
        path.unlink()
        pruned_paths.append(name)
free_after_prune = shutil.disk_usage("/home/dev").free
state_after_prune = snapshot("post-prune-pre-migration" if args.prune_neighbor else "pre-migration-intact")
handles_after_prune = open_handles()
neighbor_remaining = {str(p.relative_to(neighbor)): sha256(p)
                      for p in neighbor.rglob("*") if p.is_file()}

if tests.returncode:
    record = {
        "label": args.label, "setup_error": "focused_tests_failed",
        "small_tests_exit": tests.returncode, "small_tests_output": tests.stdout,
        "snapshots": [before_setup_state, after_tests_state, state_after_prune],
        "sqlite_configuration": before_setup_sqlite,
    }
    Path(args.output).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    raise SystemExit(tests.returncode)

before_migration_db_hash = sha256(database)
start = time.monotonic()
process = subprocess.Popen([sys.executable, "scripts/validate_migration.py"],
    cwd=repo, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    bufsize=1)
samples = [fast_sample()]
min_free = samples[-1]["free_bytes"]
sample_delay = args.sample_ms / 1000
while process.poll() is None:
    time.sleep(sample_delay)
    item = fast_sample()
    samples.append(item)
    min_free = min(min_free, item["free_bytes"])
output = process.stdout.read()
final_migration_state = snapshot("migration-finished-before-postcheck")
elapsed = time.monotonic() - start
db_hash = sha256(database)
final_neighbor_files = {str(p.relative_to(neighbor)): sha256(p)
                        for p in neighbor.rglob("*") if p.is_file()}
neighbor_missing_or_changed = sorted(
    name for name, digest in neighbor_files_before.items()
    if not (args.prune_neighbor and Path(name).name in pruned_paths)
    and final_neighbor_files.get(name) != digest
)
try:
    error_probe = sqlite_state()
except Exception as error:
    error_probe = {"post_failure_probe_error": f"{type(error).__name__}: {error}"}
failure_lines = [line for line in output.splitlines()
                 if any(word in line.lower() for word in
                        ("error", "exception", "disk is full", "persistent migration validated"))]
record = {
    "label": args.label,
    "capacity_bytes": shutil.disk_usage("/home/dev").total,
    "journal_mode": journal_mode,
    "sqlite_configuration_before_tests": before_setup_sqlite,
    "sqlite_configuration_after_migration": error_probe,
    "tmp_env": {key: env.get(key) for key in ("TMPDIR", "TEMP", "TMP", "SQLITE_TMPDIR")},
    "temp_store_override": args.temp_store,
    "verification_temp_store_override": args.verification_temp_store,
    "interruption_after_drop_requested": args.interrupt_after_drop,
    "sample_interval_ms": args.sample_ms,
    "measured_sample_intervals_ms": [
        round((right["time_ns"] - left["time_ns"]) / 1_000_000, 3)
        for left, right in zip(samples, samples[1:])
    ],
    "small_tests_exit": tests.returncode,
    "small_tests_output": tests.stdout,
    "database_sha256_before_journal_configuration": "d09f105207a13863a089ae8fafc86d5f1c317650bf8bde8d75fef5899ac4b414",
    "database_sha256_before_migration": before_migration_db_hash,
    "database_sha256_after": db_hash,
    "database_matches_pinned_source": db_hash == "d09f105207a13863a089ae8fafc86d5f1c317650bf8bde8d75fef5899ac4b414",
    "original_unchanged_after_failed_migration": process.returncode != 0 and db_hash == before_migration_db_hash,
    "migration_exit": process.returncode,
    "migration_error_excerpt": failure_lines[-20:],
    "migration_and_independent_verification_seconds": round(elapsed, 2),
    "minimum_sampled_free_bytes": min_free,
    "free_before_prune_bytes": free_before_prune,
    "free_after_prune_bytes": free_after_prune,
    "free_released_by_prune_bytes": free_after_prune - free_before_prune,
    "neighbor_pruned": args.prune_neighbor,
    "neighbor_pruned_paths": pruned_paths,
    "neighbor_before_bytes": neighbor_bytes_before,
    "neighbor_before_allocated_bytes": neighbor_allocated_before,
    "neighbor_files_before": neighbor_files_before,
    "neighbor_file_sizes_before": neighbor_file_sizes_before,
    "neighbor_files_after": final_neighbor_files,
    "neighbor_missing_or_changed": neighbor_missing_or_changed,
    "source_neighbor_files_remaining_after_prune": neighbor_remaining,
    "database_handle_inventory_after": final_migration_state["open_database_handles"],
    "open_file_handles_after_prune": handles_after_prune,
    "database_temporary_files_final": final_migration_state["temporary_files"],
    "snapshots": [before_setup_state, after_tests_state, state_after_prune,
                  *samples, final_migration_state],
    "migration_output": output,
}
serialized = json.dumps(record, indent=2, sort_keys=True) + "\n"
if args.output.endswith(".gz"):
    with gzip.open(args.output, "wt", encoding="utf-8") as stream:
        stream.write(serialized)
else:
    Path(args.output).write_text(serialized)
print(json.dumps({key: record[key] for key in (
    "label", "capacity_bytes", "migration_exit", "minimum_sampled_free_bytes",
    "free_before_prune_bytes", "free_after_prune_bytes", "free_released_by_prune_bytes",
    "migration_and_independent_verification_seconds", "original_unchanged_after_failed_migration",
    "neighbor_pruned", "neighbor_missing_or_changed")}, sort_keys=True))
print("--- migration diagnostics ---")
for line in failure_lines[-20:]:
    print(line)
