"""Domain records used throughout the stream processing pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Observation:
    """One timestamped measurement.

    ``value=None`` represents a missing measurement. Missing observations are
    kept in the time window so that their timestamps still advance the stream,
    but they are not included in numeric aggregates.
    """

    timestamp: int
    value: float | None

    @property
    def is_missing(self) -> bool:
        return self.value is None

    def as_dict(self) -> dict[str, int | float | None]:
        return {"timestamp": self.timestamp, "value": self.value}
