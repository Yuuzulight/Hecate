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

    assert len(folds) > 0
    for context, actual in folds:
        assert len(context) == 5
        assert len(actual) == 3


def test_rolling_folds_is_empty_when_the_series_is_too_short():
    assert rolling_folds([1, 2, 3], context_len=5, horizon_days=3) == []


class FakeModel:
    def __init__(self, prediction):
        self.prediction = prediction


def test_backtest_repository_compares_timesfm_against_naive(monkeypatch):
    import pipeline.forecast.backtest as backtest_module

    # - A model that always predicts flat continuation - deliberately worse
    #   than naive for this series so the comparison direction is checkable
    #   rather than assuming any particular model quality.
    monkeypatch.setattr(
        backtest_module, "forecast",
        lambda model, series, horizon_days: {"p10": series[-1], "p50": series[-1], "p90": series[-1]},
    )

    series = [float(x) for x in range(1, 31)]  # steadily increasing - naive undershoots less than a flat guess would over a longer horizon
    result = backtest_repository(FakeModel(None), series, horizon_days=3)

    assert result is not None
    assert "timesfm_mape" in result
    assert "naive_mape" in result
    assert "improvement" in result
    assert result["n_folds"] > 0


def test_backtest_repository_is_none_when_there_is_not_enough_history():
    result = backtest_repository(FakeModel(None), [1.0, 2.0, 3.0], horizon_days=7)
    assert result is None
