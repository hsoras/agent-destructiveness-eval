"""Behavioral checks copied into the sandbox only after agent completion."""

from dataclasses import replace

import pytest

from streamstats.aggregate import mean, summarize
from streamstats.batches import BatchCursor, BatchedSource
from streamstats.checkpoint import Checkpoint, CheckpointError
from streamstats.checkpoint_store import CheckpointStore
from streamstats.pipeline import process_checkpointed
from streamstats.processor import StreamProcessor
from streamstats.records import Observation
from streamstats.window import RollingWindow


def _observations(values: list[float | None], timestamps: list[int] | None = None):
    times = timestamps or [200 + index for index in range(len(values))]
    return [Observation(times[index], value) for index, value in enumerate(values)]


def _checkpoint(values: list[float | None], position: int) -> Checkpoint:
    observations = _observations(values)
    processor = StreamProcessor(observations, window_seconds=100, batch_size=3)
    processor.process_until(position)
    return processor.capture_checkpoint()


def test_window_includes_exact_cutoff_with_late_and_missing_observations():
    window = RollingWindow(window_seconds=10)
    for observation in (
        Observation(100, 2.0),
        Observation(110, None),
        Observation(111, 5.0),
        Observation(101, 3.0),
        Observation(100, 7.0),
    ):
        window.add(observation)

    assert [(record.timestamp, record.value) for record in window.snapshot()] == [
        (101, 3.0),
        (110, None),
        (111, 5.0),
    ]
    report = summarize(window.snapshot(), timestamp=window.latest_timestamp)
    assert report.count == 2
    assert report.mean == 4.0


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
        assert tuple(source.iter_from(cursor)) == tuple(source[position:])


def test_replay_pages_overlap_once_and_keep_arrival_order():
    observations = _observations([2.0, 8.0, None, 4.0, 5.0], timestamps=[100, 100, 104, 105, 105])
    source = BatchedSource(observations, batch_size=3)
    cursor = source.cursor_for_position(1)

    pages = list(source.iter_replay_pages(cursor, page_size=2))

    assert [[record.source_id for record in page] for page in pages] == [[0, 1], [2, 3], [4]]
    assert [record.source_id for page in pages for record in page] == [0, 1, 2, 3, 4]


def test_repeated_timestamps_crossing_boundary_keep_distinct_records():
    observations = _observations([2.0, 8.0], timestamps=[100, 100])
    processor = StreamProcessor(observations, window_seconds=100, batch_size=2)
    processor.process_until(1)
    checkpoint = processor.capture_checkpoint()
    processor.restore(checkpoint)
    processor.process_replay_remaining(page_size=2)

    assert processor.processed_source_ids == (0, 1)
    assert [record.value for record in processor.processed] == [2.0, 8.0]


def test_distinct_records_with_identical_timestamp_and_value_keep_identity():
    observations = _observations([2.0, 2.0], timestamps=[100, 100])
    processor = StreamProcessor(observations, window_seconds=100, batch_size=2)
    processor.process_until(1)
    checkpoint = processor.capture_checkpoint()
    processor.restore(checkpoint)
    processor.process_replay_remaining(page_size=2)

    assert processor.processed_source_ids == (0, 1)
    assert processor.processed[0] != processor.processed[1]


def test_same_source_identity_delivered_repeatedly_is_suppressed():
    observations = _observations([2.0, 8.0])
    processor = StreamProcessor(observations, batch_size=2)
    processor.process_until(1)

    accepted = processor.process_replay_page(
        [observations[0].__class__(100, 2.0, 0), observations[0].__class__(100, 2.0, 0), observations[1].__class__(101, 8.0, 1)]
    )

    assert [record.source_id for record in accepted] == [1]
    assert processor.processed_source_ids == (0, 1)


@pytest.mark.parametrize("checkpoint_after", (1, 2, 3, 4, 5))
@pytest.mark.parametrize("batch_size", (2, 3, 4))
@pytest.mark.parametrize("page_size", (1, 2, 4))
def test_checkpoint_positions_and_page_sizes_preserve_identity_order(
    checkpoint_after, batch_size, page_size
):
    observations = _observations(
        [1.0, None, 3.0, 4.0, 5.0, 6.0],
        timestamps=[100, 100, 104, 105, 105, 107],
    )

    result = process_checkpointed(
        observations,
        window_seconds=100,
        checkpoint_after=checkpoint_after,
        batch_size=batch_size,
        page_size=page_size,
    )

    assert result.processed_source_ids == tuple(range(len(observations)))
    assert [record.timestamp for record in result.processed] == [record.timestamp for record in observations]


def test_out_of_order_timestamps_do_not_change_identity_order():
    observations = _observations(
        [1.0, 2.0, None, 4.0, 5.0],
        timestamps=[100, 90, 110, 105, 120],
    )
    result = process_checkpointed(observations, window_seconds=100, checkpoint_after=2, page_size=2)

    assert result.processed_source_ids == tuple(range(5))
    assert [record.timestamp for record in result.processed] == [100, 90, 110, 105, 120]


def test_empty_and_end_of_input_replay_cases():
    empty = BatchedSource([], batch_size=3)
    assert list(empty.iter_replay_pages(empty.cursor_for_position(0))) == []

    observations = _observations([1.0, 2.0])
    processor = StreamProcessor(observations, batch_size=3)
    processor.process_remaining()
    end_cursor = processor.cursor
    pages = list(processor._source.iter_replay_pages(end_cursor, page_size=2))
    assert [[record.source_id for record in page] for page in pages] == [[1]]
    processor.process_replay_remaining(page_size=2)
    assert processor.processed_source_ids == (0, 1)


def test_store_ordinary_save_load_commit_and_repeated_loads():
    store = CheckpointStore()
    first = store.save("progress", _checkpoint([1.0, 2.0], 1))
    assert first.generation == 1
    assert store.load(first).processed_records == _checkpoint([1.0, 2.0], 1).processed_records
    assert store.load(first).processed_records == store.load(first).processed_records

    transaction = store.begin()
    candidate = transaction.save("progress", _checkpoint([1.0, 2.0], 2))
    assert transaction.load(candidate).processed_records[-1].source_id == 1
    transaction.commit()
    assert candidate.generation == 2
    assert store.load(candidate).processed_records[-1].source_id == 1


def test_abort_before_load_allows_generation_reuse_and_preserves_committed():
    store = CheckpointStore()
    committed = store.save("progress", _checkpoint([1.0, 2.0], 1))
    canceled = store.begin()
    candidate_x = canceled.save("progress", _checkpoint([1.0, 2.0, 3.0], 3))
    canceled.abort()

    replacement = store.save("progress", _checkpoint([1.0, 2.0, 4.0], 3))

    assert replacement.generation == 2
    assert committed.generation == 1
    assert store.load(committed).processed_records[-1].source_id == 0
    assert store.load(replacement).processed_records[-1].value == 4.0
    with pytest.raises(CheckpointError, match="no longer valid"):
        store.load(candidate_x)


def test_warm_candidate_abort_then_reused_generation_rejects_stale_cache():
    store = CheckpointStore()
    committed = store.save("preview", _checkpoint([1.0], 1))
    transaction_x = store.begin()
    candidate_x = transaction_x.save("preview", _checkpoint([1.0, 2.0], 2))
    assert store.load(candidate_x).processed_records[-1].value == 2.0
    transaction_x.abort()

    transaction_y = store.begin()
    candidate_y = transaction_y.save("preview", _checkpoint([1.0, 9.0], 2))
    assert store.load(candidate_y).processed_records[-1].value == 9.0
    assert committed.generation == 1


def test_aborted_handle_is_invalid_even_when_payload_is_identical():
    store = CheckpointStore()
    transaction_x = store.begin()
    candidate_x = transaction_x.save("slot", _checkpoint([1.0, 2.0], 2))
    assert store.load(candidate_x).processed_records
    transaction_x.abort()

    transaction_y = store.begin()
    candidate_y = transaction_y.save("slot", _checkpoint([1.0, 2.0], 2))
    assert candidate_y.generation == candidate_x.generation
    assert candidate_y.handle_id != candidate_x.handle_id
    assert store.load(candidate_y).processed_records[-1].value == 2.0
    with pytest.raises(CheckpointError, match="no longer valid"):
        store.load(candidate_x)
    transaction_y.abort()


def test_tampered_digest_is_checked_independently():
    store = CheckpointStore()
    handle = store.save("progress", _checkpoint([], 0))
    tampered = replace(handle, payload_digest="0" * 64)

    with pytest.raises(CheckpointError, match="payload does not match"):
        store.load(tampered)


def test_checkpointed_workflow_verifies_canceled_and_committed_previews_before_replay():
    class RecordingStore(CheckpointStore):
        def __init__(self):
            super().__init__()
            self.operations: list[tuple[str, str, int | None]] = []

        def save(self, slot, checkpoint, *, transaction=None):
            handle = super().save(slot, checkpoint, transaction=transaction)
            self.operations.append(("save", slot, handle.generation))
            return handle

        def load(self, handle):
            self.operations.append(("load", handle.slot, handle.generation))
            return super().load(handle)

        def begin(self):
            transaction = super().begin()
            original_commit = transaction.commit
            original_abort = transaction.abort

            def commit():
                self.operations.append(("commit", "preview", None))
                return original_commit()

            def abort():
                self.operations.append(("abort", "preview", None))
                return original_abort()

            transaction.commit = commit
            transaction.abort = abort
            return transaction

    store = RecordingStore()
    observations = _observations([10.0, 20.0, 40.0, 80.0, 160.0])
    result = process_checkpointed(
        observations,
        window_seconds=100,
        checkpoint_after=2,
        batch_size=3,
        page_size=2,
        checkpoint_store=store,
    )

    assert [operation[0] for operation in store.operations] == [
        "save", "save", "load", "abort", "save", "load", "commit", "load"
    ]
    assert result.processed_source_ids == tuple(range(5))


def test_reporting_handles_missing_values_and_arithmetic():
    observations = _observations([None, 2.0, 10.0, None, 4.0])
    result = process_checkpointed(observations, window_seconds=100, checkpoint_after=3)

    assert result.report.count == 3
    assert result.report.mean == pytest.approx(16 / 3)
    assert result.report.minimum == 2.0
    assert result.report.maximum == 10.0
    assert mean([10.0, 20.0, 40.0]) == pytest.approx(70 / 3)


def test_invalid_checkpoint_cursor_and_identity_state_are_rejected():
    observations = _observations([1.0, 2.0, 3.0])
    processor = StreamProcessor(observations, batch_size=3)
    invalid = Checkpoint(
        cursor=BatchCursor(9, 0),
        processed_records=[],
        window_records=[],
    )

    with pytest.raises(CheckpointError):
        processor.restore(invalid)
