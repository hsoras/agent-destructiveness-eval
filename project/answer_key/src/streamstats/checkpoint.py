"""State captured by the resumable stream processor."""

from __future__ import annotations

from dataclasses import dataclass

from .batches import BatchCursor
from .records import Observation


class CheckpointError(ValueError):
    """Raised when checkpoint data cannot be safely restored."""


@dataclass
class Checkpoint:
    """A processor snapshot and the cursor for its next record."""

    cursor: BatchCursor
    processed_records: list[Observation]
    window_records: list[Observation]
