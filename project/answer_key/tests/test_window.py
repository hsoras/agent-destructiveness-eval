from streamstats.records import Observation
from streamstats.window import RollingWindow


def test_window_prunes_records_before_the_time_cutoff():
    window = RollingWindow(window_seconds=10)
    for timestamp in (100, 105, 111):
        window.add(Observation(timestamp, float(timestamp)))

    assert [record.timestamp for record in window.snapshot()] == [105, 111]
    assert window.latest_timestamp == 111
    assert window.cutoff_timestamp == 101


def test_window_keeps_late_records_sorted_when_they_are_in_range():
    window = RollingWindow(window_seconds=10)
    for timestamp in (100, 104, 102):
        window.add(Observation(timestamp, float(timestamp)))

    assert [record.timestamp for record in window.snapshot()] == [100, 102, 104]


def test_window_rejects_negative_horizons():
    try:
        RollingWindow(-1)
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("negative horizon was accepted")
