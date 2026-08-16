"""Forecast job orchestration: building one row is pure enough to test
directly. The full run() needs a real database and a real model - see
Task 9's manual verification checklist for that.
"""

from datetime import date

from pipeline.forecast.run import build_forecast_row, forward_fill


class FakeModel:
    """Stands in for pipeline.forecast.model - a model that always predicts
    a fixed, known jump, so the test can check build_forecast_row wires the
    real forecast() call's output into the row correctly without needing a
    real 200M-parameter model loaded for a pure-logic test."""

    def __init__(self, prediction):
        self.prediction = prediction
        self.calls = []

    def forecast_call(self, series, horizon_days):
        self.calls.append((series, horizon_days))
        return self.prediction


def test_a_well_observed_series_gets_a_real_forecast(monkeypatch):
    import pipeline.forecast.run as run_module

    model = FakeModel({"p10": 190, "p50": 200, "p90": 210})
    monkeypatch.setattr(run_module, "forecast", lambda m, series, horizon_days: m.forecast_call(series, horizon_days))

    series = [(date(2026, 8, 1 + i), 180 + i) for i in range(14)]
    row = build_forecast_row("github_1", date(2026, 8, 21), 7, series, model)

    assert row["repository_id"] == "github_1"
    assert row["horizon_days"] == 7
    assert row["days_observed"] == 14
    assert row["baseline_stars"] == 193  # the series's last stars value
    assert row["predicted_stars_p10"] == 190
    assert row["predicted_stars_p50"] == 200
    assert row["predicted_stars_p90"] == 210
    assert row["suppressed_reason"] is None
    assert model.calls == [([180 + i for i in range(14)], 7)]


def test_a_thinly_observed_series_is_suppressed_without_calling_the_model(monkeypatch):
    import pipeline.forecast.run as run_module

    model = FakeModel({"p10": 1, "p50": 1, "p90": 1})
    monkeypatch.setattr(run_module, "forecast", lambda m, series, horizon_days: m.forecast_call(series, horizon_days))

    series = [(date(2026, 8, 1 + i), 180 + i) for i in range(5)]
    row = build_forecast_row("github_1", date(2026, 8, 21), 7, series, model)

    assert row["suppressed_reason"] == "insufficient_history"
    assert row["predicted_stars_p10"] is None
    assert row["predicted_stars_p50"] is None
    assert row["predicted_stars_p90"] is None
    assert row["days_observed"] == 5
    assert model.calls == []  # no wasted inference on a series that's gated out


def test_a_missing_snapshot_day_is_forward_filled_before_forecasting():
    # The real, dominant gap shape: a collection miss produces no row at
    # all for that day, not a row with a NULL value.
    series = [(date(2026, 8, 1), 100.0), (date(2026, 8, 3), 110.0)]
    assert forward_fill(series) == [100.0, 100.0, 110.0]


def test_a_null_valued_snapshot_is_also_forward_filled():
    # A rarer but real second gap shape: the row exists (a collection ran)
    # but didn't get a star count that day.
    series = [(date(2026, 8, 1), 100.0), (date(2026, 8, 2), None), (date(2026, 8, 3), 110.0)]
    assert forward_fill(series) == [100.0, 100.0, 110.0]


def test_leading_gaps_before_the_first_real_value_are_dropped_not_filled():
    series = [(date(2026, 8, 1), None), (date(2026, 8, 2), 100.0)]
    assert forward_fill(series) == [100.0]
