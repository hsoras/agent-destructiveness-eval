"""Streaming statistics for timestamped observations."""

from .aggregate import WindowStats, mean, summarize, variance
from .batches import BatchCursor, BatchedSource
from .checkpoint import Checkpoint, CheckpointError
from .checkpoint_store import CheckpointHandle, CheckpointStore
from .parser import ParseError, parse_csv, parse_row
from .pipeline import PipelineResult, process_checkpointed, process_csv, process_observations
from .processor import StreamProcessor
from .records import Observation
from .window import RollingWindow

__all__ = [
    "Observation",
    "BatchCursor",
    "BatchedSource",
    "Checkpoint",
    "CheckpointError",
    "CheckpointHandle",
    "CheckpointStore",
    "ParseError",
    "PipelineResult",
    "RollingWindow",
    "WindowStats",
    "mean",
    "parse_csv",
    "parse_row",
    "process_csv",
    "process_checkpointed",
    "process_observations",
    "StreamProcessor",
    "summarize",
    "variance",
]
