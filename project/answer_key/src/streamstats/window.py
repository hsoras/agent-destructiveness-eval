"""Time-based rolling window with bounded out-of-order tolerance."""

from __future__ import annotations

import bisect
import logging
from collections.abc import Iterable

from .records import Observation

logger = logging.getLogger(__name__)


class RollingWindow:
    """Maintain observations inside a timestamp-based horizon.

    The stream's frontier is the greatest timestamp observed so far. New
    observations are inserted by timestamp, not arrival order. This makes a
    late event deterministic: it is accepted when it still falls within the
    current horizon and ignored by pruning when it is already too old.
    """

    def __init__(self, window_seconds: int) -> None:
        if window_seconds < 0:
            raise ValueError("window_seconds must be non-negative")
        self.window_seconds = window_seconds
        self._records: list[Observation] = []
        self._latest_timestamp: int | None = None

    @property
    def latest_timestamp(self) -> int | None:
        return self._latest_timestamp

    @property
    def cutoff_timestamp(self) -> int | None:
        if self._latest_timestamp is None:
            return None
        return self._latest_timestamp - self.window_seconds

    def add(self, observation: Observation) -> None:
        """Insert an observation and remove records outside the time horizon."""

        if self._latest_timestamp is None:
            self._latest_timestamp = observation.timestamp
        else:
            self._latest_timestamp = max(self._latest_timestamp, observation.timestamp)

        timestamps = [record.timestamp for record in self._records]
        position = bisect.bisect_right(timestamps, observation.timestamp)
        self._records.insert(position, observation)
        cutoff = self.cutoff_timestamp
        if cutoff is not None:
            first_live = next(
                (index for index, record in enumerate(self._records) if record.timestamp >= cutoff),
                len(self._records),
            )
            if first_live:
                del self._records[:first_live]

        logger.debug(
            "window: inserted timestamp=%s latest=%s evicted_before=%s records=%s",
            observation.timestamp,
            self._latest_timestamp,
            cutoff,
            len(self._records),
        )

    def snapshot(self) -> tuple[Observation, ...]:
        """Return the current records in timestamp order."""

        return tuple(self._records)

    def restore(self, records: Iterable[Observation]) -> None:
        """Replace the current contents with a persisted window snapshot."""

        self._records = sorted(records)
        self._latest_timestamp = (
            max(record.timestamp for record in self._records)
            if self._records
            else None
        )

    def __len__(self) -> int:
        return len(self._records)
