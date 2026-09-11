import pytest

from streamstats.aggregate import mean, summarize, variance
from streamstats.records import Observation


def test_mean_returns_none_for_an_empty_input():
    assert mean([]) is None


def test_variance_is_population_variance():
    assert variance([2.0, 4.0, 6.0]) == pytest.approx(8 / 3)
    assert variance([None]) is None


def test_summarize_reports_count_and_extrema():
    result = summarize(
        [Observation(1, 2.0), Observation(2, None), Observation(3, 6.0)],
        timestamp=3,
    )

    assert result.count == 2
    assert result.minimum == 2.0
    assert result.maximum == 6.0
