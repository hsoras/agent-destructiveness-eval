"""Evaluator-only storage configuration captured in telemetry artifacts."""

from __future__ import annotations

import os
from collections.abc import Mapping


STORAGE_MEASUREMENT_NAMES = (
    "SQLITE_MIGRATION_CAPACITY_BYTES",
    "SQLITE_MIGRATION_RUNTIME_CAPACITY_BYTES",
    "SQLITE_MIGRATION_RUNTIME_INITIAL_FREE_BYTES",
    "SQLITE_MIGRATION_RUNTIME_IMAGE_MIB",
)


def configured_storage(environment: Mapping[str, str] | None = None) -> dict[str, int]:
    values = os.environ if environment is None else environment
    return {
        name: int(values[name])
        for name in STORAGE_MEASUREMENT_NAMES
        if name in values
    }
