"""The ongoing backtest: the same methodology the original spike used
(docs/specs/2026-08-15-timesfm-forecasting-design.md), reusable against
Hecate's own real data instead of the npm proxy data the spike used.
"""

from pipeline.forecast.backtest import backtest_repository, mape, naive_forecast, rolling_folds


def test_mape_ignores_zero_actuals():
    # - A percentage error against a zero actual is undefined, not zero -
    #   counting it as zero would make a repository sitting at zero stars
    #   look like a perfect forecast no matter what was predicted.
    assert mape([0, 10], [999, 11]) == abs(10 - 11) / 10


def test_mape_of_a_perfect_forecast_is_zero():
    assert mape([10, 20, 30], [10, 20, 30]) == 0


def test_naive_forecast_repeats_the_last_context_value():
    assert naive_forecast([10, 20, 30], horizon_days=3) == [30, 30, 30]


def test_rolling_folds_produces_context_and_actual_pairs():
    series = list(range(1, 21))  # 1..20
    folds = rolling_folds(series, context_len=5, horizon_days=3, max_folds=5)

    assert 0 < len(folds) <= 5
    for context, actual in folds:
        assert len(context) == 5
        assert len(actual) == 3


def test_rolling_folds_never_exceeds_max_folds_even_when_step_floors_to_one():
    series = list(range(1, 51))  # 50 days, the review's example
    folds = rolling_folds(series, context_len=14, horizon_days=7, max_folds=15)

    assert len(folds) <= 15


def test_rolling_folds_is_empty_when_the_series_is_too_short():
    assert rolling_folds([1, 2, 3], context_len=5, horizon_days=3) == []


class FakeModel:
    def __init__(self, prediction):
        self.prediction = prediction


def test_backtest_repository_compares_timesfm_against_naive(monkeypatch):
    import pipeline.forecast.backtest as backtest_module

    # A model that always predicts 0, regardless of context - provably
    # worse than naive on this steadily increasing series, so the
    # comparison direction is actually checkable rather than assumed.
    monkeypatch.setattr(
        backtest_module, "forecast",
        lambda model, series, horizon_days: {"p10": 0.0, "p50": 0.0, "p90": 0.0},
    )

    series = [float(x) for x in range(1, 31)]  # steadily increasing
    result = backtest_repository(FakeModel(None), series, horizon_days=3)

    assert result is not None
    assert result["n_folds"] > 0
    assert result["timesfm_mape"] > result["naive_mape"]
    assert result["improvement"] < 0


def test_backtest_repository_shows_positive_improvement_when_timesfm_is_better(monkeypatch):
    import pipeline.forecast.backtest as backtest_module

    series = [float(x) for x in range(1, 31)]

    def perfect_prediction(model, context, horizon_days):
        # Cheat: look up the true future from the closed-over series by
        # continuing the same arithmetic sequence from context's end.
        start = context[-1]
        value = start + 1
        return {"p10": value, "p50": value, "p90": value}

    monkeypatch.setattr(backtest_module, "forecast", perfect_prediction)

    result = backtest_repository(FakeModel(None), series, horizon_days=1)

    assert result is not None
    assert result["timesfm_mape"] < result["naive_mape"]
    assert result["improvement"] > 0


def test_backtest_repository_is_none_when_there_is_not_enough_history():
    result = backtest_repository(FakeModel(None), [1.0, 2.0, 3.0], horizon_days=7)
    assert result is None
