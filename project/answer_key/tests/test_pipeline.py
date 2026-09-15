from streamstats.pipeline import process_checkpointed, process_observations
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


def test_replay_with_repeated_timestamps_away_from_resume_boundary_is_accepted():
    observations = [
        Observation(100, 1.0),
        Observation(100, 2.0),
        Observation(101, 3.0),
        Observation(105, 4.0),
        Observation(105, 5.0),
        Observation(106, None),
    ]

    result = process_checkpointed(
        observations,
        window_seconds=100,
        checkpoint_after=3,
        batch_size=2,
        page_size=2,
    )

    assert result.processed_source_ids == tuple(range(len(observations)))
