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
    def latest(self) -> WindowStats:
        return self.report

    def as_dict(self) -> dict[str, object]:
        return {
            "processed_timestamps": [observation.timestamp for observation in self.processed],
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
    checkpoint_store: CheckpointStore | None = None,
) -> PipelineResult:
    """Continue a batch, verify its latest checkpoint, then resume an earlier one."""

    source = tuple(observations)
    processor = StreamProcessor(
        source,
        window_seconds=window_seconds,
        batch_size=batch_size,
    )
    if not source:
        raise ValueError("cannot process an empty stream")
    if len(source) < 2:
        processor.process_remaining()
        return _result(processor)

    store = checkpoint_store or CheckpointStore()
    boundary = min(max(checkpoint_after, 1), len(source) - 1)
    processor.process_until(boundary)
    h1 = store.save("progress", processor.capture_checkpoint())

    processor.process_remaining()
    h2 = store.save("progress", processor.capture_checkpoint())
    latest_saved = store.load(h2)
    current = processor.capture_checkpoint()
    if latest_saved != current:
        raise CheckpointError("latest checkpoint does not match current processor state")

    earlier_saved = store.load(h1)
    processor.restore(earlier_saved)
    processor.process_remaining()
    return _result(processor)


def process_csv(
    source: str | Path,
    *,
    window_seconds: int = 60,
    checkpoint_after: int = 3,
    batch_size: int = 3,
) -> PipelineResult:
    """Parse and process a CSV source through the checkpointed workflow."""

    return process_checkpointed(
        parse_csv(source),
        window_seconds=window_seconds,
        checkpoint_after=checkpoint_after,
        batch_size=batch_size,
    )
