"""Assertions shared by the integration tests and deliberately kept independent."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isclose
from typing import Any


def _record_summary(record: Mapping[str, Any]) -> str:
    return (
        f"id={record.get('source_id')}, timestamp={record.get('timestamp')}, "
        f"value={record.get('value')}"
    )


def assert_processed_identity_order(
    observed: Iterable[Mapping[str, Any]], expected: Iterable[Mapping[str, Any]]
) -> None:
    """Verify exact source identity, arrival order, and record contents."""

    observed_records = list(observed)
    expected_records = list(expected)
    observed_ids = [record.get("source_id") for record in observed_records]
    expected_ids = [record.get("source_id") for record in expected_records]
    if observed_records == expected_records:
        return

    first_difference = next(
        (
            index
            for index in range(min(len(observed_records), len(expected_records)))
            if observed_records[index] != expected_records[index]
        ),
        min(len(observed_records), len(expected_records)),
    )
    start = max(first_difference - 2, 0)
    stop = first_difference + 3
    expected_context = [_record_summary(record) for record in expected_records[start:stop]]
    observed_context = [_record_summary(record) for record in observed_records[start:stop]]
    raise AssertionError(
        "Processed source identity/order mismatch: "
        f"expected source identities={expected_ids}; "
        f"observed source identities={observed_ids}; "
        f"first difference at index {first_difference}; "
        f"expected surrounding records={expected_context}; "
        f"observed surrounding records={observed_context}"
    )


def assert_report_arithmetic(report: Any, expected_values: Iterable[float | None]) -> None:
    """Check final arithmetic after checkpoint and coverage checks have passed."""

    numeric_values = [value for value in expected_values if value is not None]
    expected_count = len(numeric_values)
    expected_sum = sum(numeric_values)
    expected_mean = expected_sum / expected_count if expected_count else None
    actual_count = report.count
    actual_mean = report.mean
    count_matches = actual_count == expected_count
    mean_matches = (
        actual_mean is None
        if expected_mean is None
        else actual_mean is not None and isclose(actual_mean, expected_mean, rel_tol=1e-12, abs_tol=1e-12)
    )
    if count_matches and mean_matches:
        return
    raise AssertionError(
        "Final report arithmetic mismatch (correct record coverage already verified): "
        f"input numeric values={numeric_values}; "
        f"expected count={expected_count}, sum={expected_sum}, mean={expected_mean}; "
        f"actual count={actual_count}, mean={actual_mean}"
    )
