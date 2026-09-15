"""Batch processing workflows for streamstats."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .aggregate import WindowStats
from .checkpoint import CheckpointError
from .checkpoint_store import CheckpointStore
from .parser import parse_csv
from .processor import StreamProcessor
from .records import Observation


@dataclass(frozen=True)
class PipelineResult:
    """The records and final report produced by one processing run."""

    processed: tuple[Observation, ...]
    report: WindowStats

    @property
    def processed_source_ids(self) -> tuple[int | None, ...]:
        return tuple(observation.source_id for observation in self.processed)

    @property
    def latest(self) -> WindowStats:
        return self.report

    def as_dict(self) -> dict[str, object]:
        return {
            "processed_timestamps": [observation.timestamp for observation in self.processed],
            "processed_source_ids": [observation.source_id for observation in self.processed],
            "processed_records": [observation.as_dict() for observation in self.processed],
            "report": self.report.as_dict(),
        }


def _result(processor: StreamProcessor) -> PipelineResult:
    return PipelineResult(processed=processor.processed, report=processor.report())


def process_observations(
    observations: Iterable[Observation], *, window_seconds: int = 60, batch_size: int = 3
) -> PipelineResult:
    """Process observations in arrival order and return the final report."""

    processor = StreamProcessor(
        observations,
        window_seconds=window_seconds,
        batch_size=batch_size,
    )
    processor.process_remaining()
    return _result(processor)


def process_checkpointed(
    observations: Iterable[Observation],
    *,
    window_seconds: int = 60,
    checkpoint_after: int = 3,
    batch_size: int = 3,
    page_size: int | None = None,
    checkpoint_store: CheckpointStore | None = None,
) -> PipelineResult:
    """Run a checkpoint preview, cancel it, then replay an earlier checkpoint."""

    source = tuple(observations)
    processor = StreamProcessor(
        source,
        window_seconds=window_seconds,
        batch_size=batch_size,
    )
    effective_page_size = batch_size if page_size is None else page_size
    if effective_page_size <= 0:
        raise ValueError("page_size must be positive")
    if not source:
        raise ValueError("cannot process an empty stream")
    if len(source) < 2:
        processor.process_remaining()
        return _result(processor)

    store = checkpoint_store or CheckpointStore()
    boundary = min(max(checkpoint_after, 1), len(source) - 1)
    processor.process_until(boundary)
    early_checkpoint = processor.capture_checkpoint()
    early_handle = store.save("progress", early_checkpoint)

    first_preview_position = min(len(source), boundary + effective_page_size)
    processor.process_until(first_preview_position)
    canceled_preview = store.begin()
    candidate_x = canceled_preview.save("preview", processor.capture_checkpoint())
    if canceled_preview.load(candidate_x) != processor.capture_checkpoint():
        raise CheckpointError("candidate checkpoint does not match its processor state")
    canceled_preview.abort()

    second_preview_position = min(len(source), first_preview_position + 1)
    if second_preview_position == first_preview_position:
        second_preview_position = boundary
        processor.restore(early_checkpoint)
    processor.process_until(second_preview_position)
    committed_preview = store.begin()
    candidate_y = committed_preview.save("preview", processor.capture_checkpoint())
    if committed_preview.load(candidate_y) != processor.capture_checkpoint():
        raise CheckpointError("candidate checkpoint does not match its processor state")
    committed_preview.commit()

    earlier_saved = store.load(early_handle)
    processor.restore(earlier_saved)
    processor.process_replay_remaining(page_size=effective_page_size)
    return _result(processor)


def process_csv(
    source: str | Path,
    *,
    window_seconds: int = 60,
    checkpoint_after: int = 3,
    batch_size: int = 3,
    page_size: int | None = None,
) -> PipelineResult:
    """Parse and process a CSV source through the checkpointed workflow."""

    return process_checkpointed(
        parse_csv(source),
        window_seconds=window_seconds,
        checkpoint_after=checkpoint_after,
        batch_size=batch_size,
        page_size=page_size,
    )
