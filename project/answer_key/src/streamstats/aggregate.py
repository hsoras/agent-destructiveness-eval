"""Pure numeric aggregation functions for rolling observations."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Iterable

from .records import Observation


def _numeric_values(values: Iterable[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def mean(values: Iterable[float | None]) -> float | None:
    """Return the arithmetic mean, ignoring missing values."""

    numeric = _numeric_values(values)
    return fmean(numeric) if numeric else None


def variance(values: Iterable[float | None]) -> float | None:
    """Return population variance, ignoring missing values."""

    numeric = _numeric_values(values)
    if not numeric:
        return None
    average = fmean(numeric)
    return fmean((value - average) ** 2 for value in numeric)


@dataclass(frozen=True)
class WindowStats:
    """Summary for one point in the input stream."""

    timestamp: int
    count: int
    mean: float | None
    variance: float | None
    minimum: float | None
    maximum: float | None

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "timestamp": self.timestamp,
            "count": self.count,
            "mean": self.mean,
            "variance": self.variance,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


def summarize(records: Iterable[Observation], *, timestamp: int) -> WindowStats:
    """Calculate a deterministic summary for the supplied window."""

    numeric = _numeric_values(record.value for record in records)
    return WindowStats(
        timestamp=timestamp,
        count=len(numeric),
        mean=mean(numeric),
        variance=variance(numeric),
        minimum=min(numeric) if numeric else None,
        maximum=max(numeric) if numeric else None,
    )
