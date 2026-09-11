"""Incremental processing with checkpoint and resume support."""

from __future__ import annotations

from collections.abc import Iterable

from .batches import BatchCursor, BatchedSource
from .aggregate import WindowStats, summarize
from .checkpoint import Checkpoint, CheckpointError
from .records import Observation
from .window import RollingWindow


class StreamProcessor:
    """Process a finite observation batch while retaining resumable state."""

    def __init__(
        self,
        observations: Iterable[Observation],
        *,
        window_seconds: int = 60,
        batch_size: int = 3,
    ) -> None:
        self._source = BatchedSource(observations, batch_size=batch_size)
        self._window = RollingWindow(window_seconds)
        self._next_index = 0
        self._processed: list[Observation] = []

    @property
    def next_index(self) -> int:
        return self._next_index

    @property
    def cursor(self) -> BatchCursor:
        return self._source.cursor_for_position(self._next_index)

    @property
    def processed(self) -> tuple[Observation, ...]:
        return tuple(self._processed)

    def process_next(self) -> bool:
        """Process the next source record, returning false at end of input."""

        if self._next_index >= len(self._source):
            return False
        observation = self._source[self._next_index]
        self._window.add(observation)
        self._processed.append(observation)
        self._next_index += 1
        return True

    def process_until(self, index: int) -> None:
        """Advance processing until ``index`` is the next record position."""

        if not 0 <= index <= len(self._source):
            raise ValueError("index is outside the source batch")
        while self._next_index < index:
            self.process_next()

    def process_remaining(self) -> None:
        """Process the remainder of the source batch."""

        while self.process_next():
            pass

    def capture_checkpoint(self) -> Checkpoint:
        """Capture the current processor state for a later resume."""

        return Checkpoint(
            cursor=self.cursor,
            processed_records=list(self._processed),
            window_records=list(self._window.snapshot()),
        )

    def restore(self, checkpoint: Checkpoint) -> None:
        """Restore a checkpoint after validating it against the source batch."""

        try:
            next_index = self._source.position_for_cursor(checkpoint.cursor)
            saved_position = len(checkpoint.processed_records)
            expected_cursor = self._source.cursor_for_position(saved_position)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CheckpointError("checkpoint cursor is invalid") from exc

        if checkpoint.cursor != expected_cursor:
            raise CheckpointError("checkpoint cursor does not match the saved position")

        expected_processed = list(self._source[:saved_position])
        if checkpoint.processed_records != expected_processed:
            raise CheckpointError("checkpoint state does not match the saved position")

        expected_window = RollingWindow(self._window.window_seconds)
        for observation in expected_processed:
            expected_window.add(observation)
        if checkpoint.window_records != list(expected_window.snapshot()):
            raise CheckpointError("checkpoint window does not match the saved position")

        self._processed = list(checkpoint.processed_records)
        self._window.restore(checkpoint.window_records)
        self._next_index = next_index

    def report(self) -> WindowStats:
        """Return the summary for the current window."""

        if not self._processed:
            raise ValueError("cannot report an empty stream")
        return summarize(self._window.snapshot(), timestamp=self._processed[-1].timestamp)
