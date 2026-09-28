from streamstats.batches import BatchCursor, BatchedSource
from streamstats.checkpoint import Checkpoint
from streamstats.records import Observation


def test_batch_positions_round_trip_at_boundaries():
    source = BatchedSource([Observation(10 + i, float(i)) for i in range(7)], batch_size=3)

    for position in range(len(source) + 1):
        cursor = source.cursor_for_position(position)
        assert source.position_for_cursor(cursor) == position


def test_checkpoint_snapshot_keeps_cursor_and_records():
    cursor = BatchCursor(1, 0)
    observation = Observation(15, 2.5, source_id=4)
    checkpoint = Checkpoint(cursor, [observation], [observation])

    assert checkpoint.cursor == cursor
    assert checkpoint.processed_records == [observation]
    assert checkpoint.window_records == [observation]
