#!/usr/bin/env python3
"""Verify the Run07 evidence package against the original scenario checkout.

Read-only: nothing is written. Prints hashes, paths, and ledger arithmetic only.

Usage:
  python3 -I verify.py /path/to/agent-destructiveness-eval [--tree]

  --tree  also hash every file under the run-07 directory and print one digest
          over the sorted listing (about 100 MB is read). SQLite *-shm index files are
          skipped because they are rewritten whenever a DB is opened.
"""

import hashlib
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_REL = ".scenario/live-proxy-paid/budget-ledger.json"
RUN07_REL = ".scenario/live-proxy-paid/run-07"
QUANT = Decimal("0.0000000001")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_sums(root):
    ok = bad = missing = 0
    with open(os.path.join(HERE, "SHA256SUMS"), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            digest, rel = line.split("  ", 1)
            path = os.path.join(root, rel)
            if not os.path.isfile(path):
                missing += 1
                print("MISSING  ", rel)
            elif sha256(path) == digest:
                ok += 1
            else:
                bad += 1
                print("MISMATCH ", rel)
    print(f"SHA256SUMS: ok={ok} mismatch={bad} missing={missing}")
    return bad == 0 and missing == 0


def check_ledger(root):
    with open(os.path.join(root, LEDGER_REL), encoding="utf-8") as f:
        ledger = json.load(f)
    seen, bad, missing = set(), [], []
    for attempt in ledger["paid_attempts"]:
        entries = list(attempt.get("bounded_unknown_billing", {}).get("evidence", []))
        for event in attempt.get("hold_history", []):
            entries += event.get("evidence", [])
        for entry in entries:
            if entry["path"] in seen:
                continue
            seen.add(entry["path"])
            path = os.path.join(root, entry["path"])
            if not os.path.isfile(path):
                missing.append(entry["path"])
            elif sha256(path) != entry["sha256"]:
                bad.append(entry["path"])
    print(f"ledger evidence entries: {len(seen)} mismatch={len(bad)} missing={len(missing)}")
    for p in bad + missing:
        print("  ", p)

    print()
    print(f"{'run':7} {'status':20} {'reserved':>9} {'tgt_known':>12} {'aux_rep':>11} "
          f"{'posts':>5} {'steps':>5} {'tgt_unk':>7} {'aux_unk':>7}")
    reserved_total = Decimal(0)
    known_total = Decimal(0)
    for a in ledger["paid_attempts"]:
        acc = a.get("accounting", {})
        tgt = Decimal(str(acc.get("target_known_cost_subtotal_usd", 0)))
        aux = Decimal(str(acc.get("auxiliary_reported_cost_usd", 0)))
        reserved = Decimal(str(a["reserved_usd"]))
        reserved_total += reserved
        known_total += tgt + aux
        print(f"{a['name']:7} {a['status']:20} {reserved:>9} {tgt:>12} {aux:>11} "
              f"{acc.get('target_admitted_posts', ''):>5} {acc.get('target_usage_steps', ''):>5} "
              f"{acc.get('target_unaccounted_posts', ''):>7} {acc.get('auxiliary_unaccounted_calls', ''):>7}")
    print()
    print("reported known total (target + auxiliary):", known_total.quantize(QUANT))
    print("ledger reported_spend_usd:               ", ledger["reported_spend_usd"])
    print("reserved total (all attempts):           ", reserved_total.quantize(QUANT))
    print("ledger unaccounted_reserved_usd:         ", ledger["unaccounted_reserved_usd"])
    authorized = Decimal(str(ledger["authorized_total_usd"]))
    print("authorized minus reserved total:         ", (authorized - reserved_total).quantize(QUANT))
    print("ledger remaining_authorized_usd:         ", ledger.get("remaining_authorized_usd"))
    return not bad and not missing


def tree_digest(root):
    base = os.path.join(root, RUN07_REL)
    lines = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith("-shm"):
                # SQLite WAL index: volatile, rewritten whenever the DB is opened.
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            lines.append(f"{sha256(full)}  {rel}\n")
    lines.sort(key=lambda line: line.split("  ", 1)[1])
    digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
    print(f"run-07 tree: files={len(lines)} manifest_sha256={digest}")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    root = os.path.abspath(argv[1])
    ok = check_sums(root)
    print()
    ok = check_ledger(root) and ok
    if "--tree" in argv[2:]:
        print()
        tree_digest(root)
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
