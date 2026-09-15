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
        self._processed_source_ids: set[int] = set()
        self._timestamp_watermark: int | None = None

    @property
    def next_index(self) -> int:
        return self._next_index

    @property
    def cursor(self) -> BatchCursor:
        return self._source.cursor_for_position(self._next_index)

    @property
    def processed(self) -> tuple[Observation, ...]:
        return tuple(self._processed)

    @property
    def processed_source_ids(self) -> tuple[int, ...]:
        return tuple(observation.source_id for observation in self._processed)

    def _record(self, observation: Observation) -> None:
        if observation.source_id is None:
            raise ValueError("source records must have a stable source_id")
        self._window.add(observation)
        self._processed.append(observation)
        self._processed_source_ids.add(observation.source_id)
        self._timestamp_watermark = (
            observation.timestamp
            if self._timestamp_watermark is None
            else max(self._timestamp_watermark, observation.timestamp)
        )

    def _is_replay_duplicate(self, observation: Observation) -> bool:
        """Return whether this source identity has already been accepted."""
        if observation.source_id is None:
            raise ValueError("replayed records must have a stable source_id")
        return observation.source_id in self._processed_source_ids
    def process_next(self) -> bool:
        """Process the next source record, returning false at end of input."""

        if self._next_index >= len(self._source):
            return False
        observation = self._source[self._next_index]
        self._record(observation)
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

    def process_replay_page(self, page: Iterable[Observation]) -> tuple[Observation, ...]:
        """Accept a replay page, suppressing only identities already processed."""

        accepted: list[Observation] = []
        for observation in page:
            if self._is_replay_duplicate(observation):
                continue
            self._record(observation)
            accepted.append(observation)
        return tuple(accepted)

    def process_replay_remaining(self, *, page_size: int | None = None) -> None:
        """Replay from the current cursor with the source's overlap boundary."""

        for page in self._source.iter_replay_pages(self.cursor, page_size=page_size):
            self.process_replay_page(page)
        self._next_index = len(self._source)

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
        self._processed_source_ids = {
            observation.source_id
            for observation in self._processed
            if observation.source_id is not None
        }
        if len(self._processed_source_ids) != len(self._processed):
            raise CheckpointError("checkpoint records do not have stable source identities")
        self._timestamp_watermark = (
            max(observation.timestamp for observation in self._processed)
            if self._processed
            else None
        )
        self._next_index = next_index

    def report(self) -> WindowStats:
        """Return the summary for the current window."""

        if not self._processed:
            raise ValueError("cannot report an empty stream")
        return summarize(self._window.snapshot(), timestamp=self._processed[-1].timestamp)
