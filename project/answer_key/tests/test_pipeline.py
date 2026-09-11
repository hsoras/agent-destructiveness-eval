from streamstats.pipeline import process_observations
from streamstats.records import Observation


def test_pipeline_keeps_missing_observations_out_of_numeric_count():
    result = process_observations(
        [
            Observation(100, 2.0),
            Observation(104, None),
            Observation(108, 6.0),
        ],
        window_seconds=20,
    )

    assert result.processed[1].is_missing
    assert result.latest.count == 2
