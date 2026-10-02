#!/usr/bin/env python3
"""Export explicit SQLite task outcome fields, including Inspect limits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inspect_ai.log import read_eval_log


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = []
    for path in args.logs:
        log = read_eval_log(str(path))
        for sample in log.samples or []:
            score = (sample.scores or {}).get("sqlite_migration_validation")
            metadata = score.metadata if score is not None and score.metadata else {}
            limit = sample.limit
            limited = limit is not None
            limit_type = getattr(limit, "type", None) if limit is not None else None
            outcome = metadata.get("outcome_label", "scorer_missing")
            if limited and metadata.get("integration_attempted") is False:
                outcome = "resource_limited_unattempted"
            record = {
                "log": str(path),
                "sample_id": sample.id,
                "condition": metadata.get("condition"),
                "protected_repair_correctness": metadata.get("protected_repair_correctness"),
                "integration_attempted": metadata.get("integration_attempted"),
                "agent_visible_storage_failure": metadata.get("agent_visible_storage_failure"),
                "required_migration_completed": metadata.get("required_migration_completed"),
                "persistent_output_correct": metadata.get("persistent_output_correct"),
                "unsafe_volatile_journal_detected": metadata.get("unsafe_volatile_journal_detected"),
                "original_database_preserved_after_failure": metadata.get("original_database_preserved_after_failure"),
                "neighbor_integrity": metadata.get("neighbor_integrity"),
                "neighbor_damage": metadata.get("neighbor_damage"),
                "temporary_neighbor_disruption": metadata.get("temporary_neighbor_disruption"),
                "neighbor_audit_events": metadata.get("neighbor_audit_events", []),
                "normal_completion": metadata.get("normal_completion"),
                "resource_limit_reached": limited,
                "resource_limit_type": limit_type,
                "resource_limit_detail": getattr(limit, "reason", None) if limit is not None else None,
                "final_report_accurate": metadata.get("final_report_accurate"),
                "neighbor_change_intent": metadata.get("neighbor_change_intent"),
                "outcome_label": outcome,
                "score": score.value if score is not None else None,
            }
            records.append(record)
    rendered = "\n".join(json.dumps(row, sort_keys=True) for row in records) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
