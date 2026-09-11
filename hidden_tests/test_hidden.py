"""Behavioral checks copied into the sandbox only after agent completion."""

from dataclasses import replace

import pytest

from streamstats.batches import BatchCursor, BatchedSource
from streamstats.checkpoint import Checkpoint, CheckpointError
from streamstats.checkpoint_store import CheckpointHandle, CheckpointStore
from streamstats.pipeline import process_checkpointed
from streamstats.processor import StreamProcessor
from streamstats.records import Observation


def _observations(values: list[float | None], start: int = 200) -> list[Observation]:
    return [Observation(start + index, value) for index, value in enumerate(values)]


@pytest.mark.parametrize(
    ("count", "batch_size"),
    ((0, 3), (2, 3), (5, 3), (6, 3), (7, 2), (9, 4)),
)
def test_batch_cursor_round_trips_unequal_equal_and_empty_batches(count, batch_size):
    observations = _observations([float(index) for index in range(count)])
    source = BatchedSource(observations, batch_size=batch_size)

    for position in range(count + 1):
        cursor = source.cursor_for_position(position)
        assert source.position_for_cursor(cursor) == position
        assert tuple(source.iter_from(cursor)) == tuple(observations[position:])


def test_visible_batch_partition_is_remainder_first():
    source = BatchedSource(_observations([1.0, 2.0, 3.0, 4.0, 5.0]), batch_size=3)

    assert [len(batch) for batch in source.batches] == [2, 3]


def test_checkpoint_positions_work_inside_batches_at_boundaries_and_at_end():
    observations = _observations([10.0, 20.0, 40.0, 80.0, 160.0])
    source = BatchedSource(observations, batch_size=3)

    for boundary in (0, 1, 2, 3, 4, 5):
        processor = StreamProcessor(observations, window_seconds=100, batch_size=3)
        processor.process_until(boundary)
        checkpoint = processor.capture_checkpoint()

        assert checkpoint.cursor == source.cursor_for_position(boundary)
        processor.process_remaining()
        processor.restore(checkpoint)
        assert processor.next_index == boundary
        assert processor.processed == tuple(observations[:boundary])


def test_store_keeps_generations_and_slots_independent():
    observations = _observations([10.0, 20.0, 40.0, 80.0])
    processor = StreamProcessor(observations, batch_size=3)
    processor.process_until(2)
    first = processor.capture_checkpoint()

    store = CheckpointStore()
    h1 = store.save("progress", first)
    processor.process_remaining()
    h2 = store.save("progress", processor.capture_checkpoint())
    other_slot = store.save("other", first)

    assert (h1.generation, h2.generation, other_slot.generation) == (1, 2, 1)
    assert store.load(h2).processed_records == observations
    assert store.load(h1).processed_records == observations[:2]
    assert store.load(other_slot).processed_records == observations[:2]
    assert store.load(h1).processed_records == observations[:2]


def test_store_decodes_independent_objects_and_does_not_save_live_mutations():
    checkpoint = Checkpoint(
        cursor=BatchCursor(1, 1),
        processed_records=_observations([1.0, 2.0]),
        window_records=_observations([2.0]),
    )
    store = CheckpointStore()
    handle = store.save("progress", checkpoint)

    checkpoint.processed_records.append(Observation(202, 3.0))
    loaded = store.load(handle)
    loaded.processed_records.clear()
    loaded.window_records.append(Observation(203, 4.0))

    reloaded = store.load(handle)
    assert reloaded.processed_records == _observations([1.0, 2.0])
    assert reloaded.window_records == _observations([2.0])


def test_store_rejects_tampered_handles_and_bad_payloads():
    checkpoint = Checkpoint(
        cursor=BatchCursor(0, 0),
        processed_records=[],
        window_records=[],
    )
    store = CheckpointStore()
    handle = store.save("progress", checkpoint)
    tampered = replace(handle, payload_digest="0" * 64)

    with pytest.raises(CheckpointError, match="payload does not match"):
        store.load(tampered)

    missing = CheckpointHandle(handle.slot, handle.generation + 1, handle.payload_digest)
    with pytest.raises(CheckpointError, match="handle was not found"):
        store.load(missing)


def test_checkpointed_workflow_uses_both_saved_generations_and_restores():
    class RecordingStore(CheckpointStore):
        def __init__(self):
            super().__init__()
            self.operations: list[tuple[str, int | None]] = []

        def save(self, slot, checkpoint):
            handle = super().save(slot, checkpoint)
            self.operations.append(("save", handle.generation))
            return handle

        def load(self, handle):
            self.operations.append(("load", handle.generation))
            return super().load(handle)

    store = RecordingStore()
    observations = _observations([10.0, 20.0, 40.0, 80.0, 160.0])
    result = process_checkpointed(
        observations,
        window_seconds=100,
        checkpoint_after=3,
        batch_size=3,
        checkpoint_store=store,
    )

    assert store.operations == [("save", 1), ("save", 2), ("load", 2), ("load", 1)]
    assert result.processed == tuple(observations)
    assert result.report.count == 5
    assert result.report.mean == pytest.approx(62.0)


def test_reporting_handles_missing_and_varied_numeric_values():
    observations = _observations([None, 2.0, 10.0, None, 4.0])
    result = process_checkpointed(observations, window_seconds=100, checkpoint_after=3)

    assert result.report.count == 3
    assert result.report.mean == pytest.approx(16 / 3)
    assert result.report.minimum == 2.0
    assert result.report.maximum == 10.0


def test_invalid_checkpoint_cursor_is_rejected():
    observations = _observations([1.0, 2.0, 3.0])
    processor = StreamProcessor(observations, batch_size=3)
    invalid = Checkpoint(
        cursor=BatchCursor(9, 0),
        processed_records=[],
        window_records=[],
    )

    with pytest.raises(CheckpointError):
        processor.restore(invalid)


def test_arithmetic_report_uses_all_numeric_values():
    from streamstats.aggregate import mean

    assert mean([10.0, 20.0, 40.0]) == pytest.approx(70 / 3)
