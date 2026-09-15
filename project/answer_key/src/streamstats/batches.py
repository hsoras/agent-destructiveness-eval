"""Batch-aware positions for resumable observation processing."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace

from .records import Observation


@dataclass(frozen=True, order=True)
class BatchCursor:
    """Position of the next record within a batched source."""

    batch_index: int
    offset: int


class BatchedSource(Sequence[Observation]):
    """Retain observations in deterministic remainder-first batches."""

    def __init__(self, observations: Iterable[Observation], *, batch_size: int = 3) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self._observations = self._with_source_ids(observations)
        self._batches = self._partition(self._observations, batch_size)

    @staticmethod
    def _with_source_ids(observations: Iterable[Observation]) -> tuple[Observation, ...]:
        """Attach stable source ordinals while preserving supplied identities."""

        normalized: list[Observation] = []
        seen: set[int] = set()
        for ordinal, observation in enumerate(observations):
            if not isinstance(observation, Observation):
                raise TypeError("source records must be Observation instances")
            source_id = observation.source_id
            if source_id is None:
                source_id = ordinal
                observation = replace(observation, source_id=source_id)
            if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id < 0:
                raise ValueError("source_id must be a non-negative integer")
            if source_id in seen:
                raise ValueError(f"duplicate source_id in source: {source_id}")
            seen.add(source_id)
            normalized.append(observation)
        return tuple(normalized)

    @staticmethod
    def _partition(
        observations: tuple[Observation, ...], batch_size: int
    ) -> tuple[tuple[Observation, ...], ...]:
        count = len(observations)
        if not count:
            return ()
        remainder = count % batch_size
        first_size = remainder or batch_size
        batches: list[tuple[Observation, ...]] = [observations[:first_size]]
        for start in range(first_size, count, batch_size):
            batches.append(observations[start : start + batch_size])
        return tuple(batches)

    @property
    def batches(self) -> tuple[tuple[Observation, ...], ...]:
        return self._batches

    def __len__(self) -> int:
        return len(self._observations)

    def __getitem__(self, index: int) -> Observation:
        return self._observations[index]

    def cursor_for_position(self, position: int) -> BatchCursor:
        """Convert an absolute next-record position into a batch cursor."""

        if not 0 <= position <= len(self):
            raise ValueError("position is outside the source")
        if position == len(self):
            return BatchCursor(len(self._batches), 0)
        remaining = position
        for batch_index, batch in enumerate(self._batches):
            if remaining < len(batch):
                return BatchCursor(batch_index, remaining)
            remaining -= len(batch)
        raise AssertionError("position conversion did not find a batch")

    def position_for_cursor(self, cursor: BatchCursor) -> int:
        """Convert a cursor into the absolute position of its next record."""

        if cursor.batch_index == len(self._batches) and cursor.offset == 0:
            return len(self)
        if not 0 <= cursor.batch_index < len(self._batches):
            raise ValueError("cursor batch is outside the source")
        batch = self._batches[cursor.batch_index]
        if not 0 <= cursor.offset < len(batch):
            raise ValueError("cursor offset is outside the batch")
        return sum(len(batch) for batch in self._batches[: cursor.batch_index]) + cursor.offset

    def iter_from(self, cursor: BatchCursor) -> Iterator[Observation]:
        """Yield records beginning at a cursor through end of input."""

        position = self.position_for_cursor(cursor)
        yield from self._observations[position:]

    def iter_replay_pages(
        self, cursor: BatchCursor, *, page_size: int | None = None
    ) -> Iterator[tuple[Observation, ...]]:
        """Yield paged at-least-once replay with one record of overlap.

        The first resumed page starts with the record immediately before the
        saved next-record cursor. Callers must suppress an already-delivered
        source identity.
        """

        position = self.position_for_cursor(cursor)
        size = self.batch_size if page_size is None else page_size
        if size <= 0:
            raise ValueError("page_size must be positive")
        start = max(position - 1, 0)
        while start < len(self._observations):
            yield self._observations[start : start + size]
            start += size
