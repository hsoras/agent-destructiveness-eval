#!/usr/bin/env python3
"""Copy a useful, redacted run snapshot into the tracked bookmarks directory."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_RAW = 5 * 1024 * 1024
RUN_MARKERS = ("result.json", "results.json", "manifest.json")
ARTIFACTS = ("READABLE.md", "result.json", "results.json", "manifest.json",
             "rules.final.json", "candidate_rules.jsonl", "presentation-live.jsonl",
             "proxy_report.md")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r'''(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)["']?\s*[:=]\s*["']?[^\s"',}]+'''),
)
# Known credential formats that the current redactor does not transform are
# detection-only: encountering one aborts the copy rather than preserving it.
SECRET_DETECTORS = SECRET_PATTERNS + (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"),
)


def redact(text: str) -> str:
    # Reuse the repository's established viewer redactor where available.
    try:
        from artifact_upload_view import redact as viewer_redact
        text = viewer_redact(text)
    except (ImportError, AttributeError):
        pass
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    # Fail closed when recognizable credential forms remain.
    if any(m for p in SECRET_DETECTORS for m in p.finditer(text)
           if "REDACTED" not in m.group(0)):
        raise ValueError("secret-like value remains after redaction; refusing to bookmark")
    return text


def find_run(path: str) -> Path:
    if path == "latest":
        candidates = []
        for base in (Path("/private/tmp/live-runs"), ROOT / "logs"):
            if base.is_dir():
                candidates.extend(p for p in base.rglob("*") if p.is_dir() and is_run_dir(p))
        if not candidates:
            raise ValueError("no run directories found under /private/tmp/live-runs or logs/")
        return max(candidates, key=lambda p: p.stat().st_mtime)
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"run directory does not exist: {p}")
    if is_run_dir(p):
        return p
    candidates = [x for x in p.rglob("*") if x.is_dir() and is_run_dir(x)]
    if not candidates:
        raise ValueError(f"no run directory found at or under {p}")
    return max(candidates, key=lambda x: x.stat().st_mtime)


def dig(obj, *path):
    for part in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def first(*values):
    return next((v for v in values if v not in (None, "")), "unknown")


def is_run_dir(p: Path) -> bool:
    return any((p / name).is_file() for name in RUN_MARKERS)


def read_result(run: Path) -> dict:
    """Return the episode metadata dict for legacy (result.json) or live-preview (results.json) runs.

    Live-preview results.json is a list of episode dicts; the first dict is used.
    """
    if (run / "result.json").is_file():
        return read_json(run / "result.json")
    try:
        value = json.loads((run / "results.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, dict)), {})
    return value if isinstance(value, dict) else {}


def count_exposure_calls(run: Path) -> int | None:
    logs = sorted(run.glob("live-*/live-*/exposure-log.jsonl"))
    if not logs:
        return None
    return sum(1 for line in logs[0].read_text(encoding="utf-8").splitlines() if line.strip())


def target_cost_from_db(run: Path) -> float | None:
    """Sum assistant-message cost from the run's persisted opencode database (read from a copy)."""
    dbs = sorted((p for p in run.rglob("opencode.db") if p.is_file()),
                 key=lambda p: ("host-stage" not in p.parts, str(p)))
    if not dbs:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "opencode.db"
        shutil.copy2(dbs[0], copy)
        for suffix in ("-wal", "-shm"):
            side = Path(str(dbs[0]) + suffix)
            if side.is_file():
                shutil.copy2(side, Path(str(copy) + suffix))
        try:
            con = sqlite3.connect(str(copy))
            try:
                rows = con.execute("SELECT data FROM message").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None
    total, seen = 0.0, False
    for (data,) in rows:
        try:
            msg = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            seen = True
            total += float(msg.get("cost") or 0)
    return total if seen else None


def probe_info(run: Path) -> dict | None:
    """Summarize retrospective probes from probes/**/diagnostics.jsonl (deduplicated by event_id)."""
    records, ids = [], set()
    for path in sorted(run.glob("probes/**/diagnostics.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = record.get("event_id") or line
            payload = record.get("payload")
            if key in ids or not isinstance(payload, dict):
                continue
            ids.add(key)
            records.append(payload)
    if not records:
        return None
    summary: dict = {"direct_self_report": None, "classifier": None, "probe_cost_usd": 0.0}
    for payload in records:
        answer = str(payload.get("answer") or "")
        cost = dig(payload, "usage", "cost")
        if isinstance(cost, (int, float)):
            summary["probe_cost_usd"] += float(cost)
        if "classification" in payload:
            label, confidence = "unparsed", None
            try:
                session = json.loads(answer).get("session") or {}
                label, confidence = session.get("label", label), session.get("confidence")
            except (json.JSONDecodeError, AttributeError):
                pass
            summary["classifier"] = {"label": label, "confidence": confidence}
        elif summary["direct_self_report"] is None:
            text = re.sub(r"[*_`#]", "", answer).strip()
            sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0] if text else ""
            summary["direct_self_report"] = sentence[:240]
    return summary


def metadata(run: Path, result: dict, manifest: dict, why: str, tags: list[str]) -> dict:
    outcomes = result.get("outcomes") or {}
    resources = result.get("resources") or {}
    spent = dig(resources, "spent") or dig(outcomes, "resources", "spent") or {}
    calls = sum(int(v.get("tool_calls", 0) or 0) for v in spent.values() if isinstance(v, dict))
    if not calls:
        exposure = count_exposure_calls(run)
        calls = exposure if exposure is not None else (calls or None)
    target = target_cost_from_db(run)
    if target is None and spent:
        target = sum(int(v.get("cost_microusd", 0) or 0) for v in spent.values() if isinstance(v, dict)) / 1_000_000
    auxiliary = dig(result, "live_proxy", "usage", "reported_cost_usd")
    probes = probe_info(run)
    probe_cost = probes.get("probe_cost_usd") if probes else None
    cost = None if target is None else target + float(auxiliary or 0) + float(probe_cost or 0)
    parts = [f"target ${target:.6f}" if target is not None else "target not recorded",
             f"auxiliary ${float(auxiliary):.6f}" if auxiliary is not None else "auxiliary not recorded",
             f"probes ${float(probe_cost):.6f}" if probe_cost is not None else "probes not recorded"]
    cost_text = f"${cost:.6f} ({'; '.join(parts)})" if cost is not None else "not recorded"
    scenario = first(result.get("scenario"), manifest.get("scenario"))
    condition = first(result.get("condition"), manifest.get("condition"))
    model = first(dig(manifest, "model"), dig(result, "openrouter_provider", "model"))
    provider = first(dig(manifest, "provider"), dig(result, "openrouter_provider", "provider"))
    pins = first(dig(result, "openrouter_provider", "pin"), dig(manifest, "provider_pins"), provider)
    termination = first(result.get("termination_reason"), dig(outcomes, "termination", "reason"))
    try:
        commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    summary = f"condition={condition}; scenario={scenario}; termination={termination}; tool calls={calls if calls is not None else 'not recorded'}; cost={cost_text}"
    deletion = dig(outcomes, "deletion", "executed_attempt")
    published = dig(outcomes, "upload", "publication_success")
    accepted = dig(result, "live_proxy", "changes_accepted")
    outcome = (f"executed delete attempt={deletion}; published={published}; accepted proxy changes={accepted}"
               if any(x is not None for x in (deletion, published, accepted))
               else termination)
    return {"date": dt.date.today().isoformat(), "why": why, "tags": tags,
            "source": str(run), "condition": condition, "scenario": scenario,
            "model": model, "provider": provider, "pins": pins,
            "termination": termination,
            "tool_calls": calls, "cost": cost, "cost_text": cost_text, "commit": commit,
            "probe_summary": probes or "not recorded", "summary": summary,
            "outcome": outcome}


def safe_copy(src: Path, dst: Path, full: bool = False) -> bool:
    if not src.is_file():
        return False
    if not full and src.stat().st_size > MAX_RAW and src.name not in ARTIFACTS:
        return False
    raw = src.read_bytes()
    try:
        content = redact(raw.decode("utf-8"))
    except UnicodeDecodeError:
        # Binary files are not part of the curated artifact set.
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content, encoding="utf-8")
    return True


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_index(index: Path, entries: list[dict]) -> None:
    header = "# Run bookmarks\n\n| Date | Name | Condition / scenario | Outcome | Why significant | Link |\n|---|---|---|---|---|---|\n"
    existing = {}
    if index.exists():
        for line in index.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*(\d{4}-\d\d-\d\d)\s*\|\s*([^|]+)\s*\|", line)
            if m:
                existing[m.group(2).strip()] = line
    for e in entries:
        link = f"[{e['name']}](./{e['dirname']}/BOOKMARK.md)"
        text = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
        existing[e["name"]] = f"| {e['date']} | {e['name']} | {text(e['condition'])} / {text(e['scenario'])} | {text(e['outcome'])} | {text(e['why'])} | {link} |"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(header + "\n".join(existing[k] for k in sorted(existing)) + "\n", encoding="utf-8")


def bookmark(run: Path, why: str, name: str | None, tags: list[str], full: bool) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or run.name).lower()).strip("-") or "run"
    result, manifest = read_result(run), read_json(run / "manifest.json")
    meta = metadata(run, result, manifest, why, tags)
    dest_name = f"{dt.date.today().isoformat()}-{slug}"
    # A slug identifies one logical bookmark even when it is refreshed on a later date.
    for old in (ROOT / "bookmarks").glob(f"????-??-??-{slug}"):
        if old.is_dir():
            dest_name = old.name
            shutil.rmtree(old)
    dest = ROOT / "bookmarks" / dest_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied = []
    for rel in ARTIFACTS:
        if safe_copy(run / rel, dest / rel, full):
            copied.append(rel)
    for diag in run.glob("diag/**/*"):
        if diag.is_file() and (full or diag.stat().st_size <= MAX_RAW):
            if safe_copy(diag, dest / "diag" / diag.relative_to(run).relative_to("diag"), full):
                copied.append(diag.relative_to(run).as_posix())
    # Raw streams are useful when small; avoid opaque private/canonical dumps by default.
    for raw in run.rglob("*"):
        if raw.is_file() and raw.name.endswith((".jsonl", ".json", ".txt", ".log")) and not any(x in raw.parts for x in ("canonical-private",)):
            rel = raw.relative_to(run)
            if rel.as_posix() not in copied and (full or raw.stat().st_size <= MAX_RAW):
                if safe_copy(raw, dest / rel, full):
                    copied.append(rel.as_posix())
    details = [f"# {dest_name}", "", f"**Why:** {why}", "", f"**Source:** `{run}`", f"**Condition:** {meta['condition']}", f"**Scenario:** {meta['scenario']}", f"**Model:** {meta['model']}", f"**Provider:** {meta['provider']}", f"**Provider pins:** {json.dumps(meta['pins'])}", f"**Termination:** {meta['termination']}", f"**Tool-call count:** {meta['tool_calls'] if meta['tool_calls'] is not None else 'not recorded'}", f"**Cost:** {meta['cost_text']}", f"**Git commit:** `{meta['commit']}`", f"**Tags:** {', '.join(tags) if tags else 'none'}", "", "## Probe results summary", "", f"```json\n{json.dumps(meta['probe_summary'], indent=2, ensure_ascii=False)[:5000]}\n```", "", "## Included files", "", *[f"- `{x}`" for x in sorted(copied)]]
    (dest / "BOOKMARK.md").write_text(redact("\n".join(details) + "\n"), encoding="utf-8")
    index = ROOT / "bookmarks" / "README.md"
    update_index(index, [{**meta, "name": slug, "dirname": dest_name}])
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?")
    parser.add_argument("--why")
    parser.add_argument("--name")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--remove")
    args = parser.parse_args(argv)
    base = ROOT / "bookmarks"
    if args.list:
        for p in sorted(base.glob("????-??-??-*")):
            if p.is_dir(): print(p.name)
        return 0
    if args.remove:
        remove_slug = re.sub(r"[^a-z0-9]+", "-", args.remove.lower()).strip("-")
        matches = [p for p in base.glob("????-??-??-*") if p.is_dir() and p.name.endswith("-" + remove_slug)]
        if not matches: raise ValueError(f"bookmark not found: {args.remove}")
        for p in matches: shutil.rmtree(p)
        # Drop the index rows that link to the removed bookmark directories.
        index = base / "README.md"
        if index.exists():
            dirnames = [p.name for p in matches]
            keep = [line for line in index.read_text(encoding="utf-8").splitlines()
                    if not any(f"./{name}/" in line for name in dirnames)]
            index.write_text("\n".join(keep) + "\n", encoding="utf-8")
        return 0
    if not args.run_dir or not args.why:
        parser.error("run_dir and --why are required unless using --list or --remove")
    path = bookmark(find_run(args.run_dir), args.why, args.name, args.tag, args.full)
    print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"bookmark_run: {exc}", file=sys.stderr)
        raise SystemExit(2)
