"""Forecast job orchestration: building one row is pure enough to test
directly. The full run() needs a real database and a real model - see
Task 9's manual verification checklist for that.
"""

from datetime import date

import pytest

from pipeline.exceptions import ForecastError
from pipeline.forecast.run import build_forecast_row, forward_fill, run


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


class FakeLoader:
    """Enough of PostgreSQLLoader for run() to get through its readback
    check. Holds forecast rows in a dict keyed the way the real upsert is,
    so a re-run replaces its own rows rather than adding to them."""

    def __init__(self, targets, preexisting=()):
        self.targets = targets
        self.stored = {}
        for row in preexisting:
            self.stored[(row["repository_id"], row["forecast_date"], row["horizon_days"])] = row

    def connect(self):
        pass

    def close(self):
        pass

    def top_forecast_targets(self, n):
        return self.targets[:n]

    def snapshot_series(self, repository_id):
        return [(date(2026, 8, 1 + i), 100 + i) for i in range(20)]

    def write_forecasts(self, rows):
        for row in rows:
            self.stored[(row["repository_id"], row["forecast_date"], row["horizon_days"])] = row
        return len(rows)

    def forecast_rows_for(self, forecast_date, repository_ids=None):
        rows = [r for r in self.stored.values() if r["forecast_date"] == forecast_date]
        if repository_ids is not None:
            rows = [r for r in rows if r["repository_id"] in set(repository_ids)]
        return rows


def _patch_run(monkeypatch, loader):
    import pipeline.forecast.run as run_module

    monkeypatch.setattr(run_module, "PostgreSQLLoader", lambda config: loader)
    monkeypatch.setattr(run_module, "load_model", lambda: object())
    monkeypatch.setattr(
        run_module, "forecast", lambda m, series, horizon_days: {"p10": 1, "p50": 2, "p90": 3}
    )
    monkeypatch.setattr(run_module, "date", _FixedDate)


class _FixedDate(date):
    """run() calls date.today(); the readback has to be checked against a
    known date rather than whatever day the suite happens to run on."""

    @classmethod
    def today(cls):
        return date(2026, 8, 16)


def test_rows_left_by_an_earlier_run_do_not_fail_the_readback(monkeypatch):
    # The real 2026-08-16 failure: an aborted run left 12 rows for 6
    # repositories that today's top-50 no longer includes, so reading back
    # the whole date found 112 rows where this run had written 100 and the
    # job failed on arithmetic about someone else's rows.
    orphan = {
        "repository_id": "github_orphan",
        "forecast_date": date(2026, 8, 16),
        "horizon_days": 7,
        "days_observed": 20,
        "baseline_stars": 1,
        "predicted_stars_p10": 1,
        "predicted_stars_p50": 1,
        "predicted_stars_p90": 1,
        "suppressed_reason": None,
        "model_version": "old",
        "generated_at": None,
    }
    loader = FakeLoader(targets=[{"id": "github_1"}, {"id": "github_2"}], preexisting=[orphan])
    _patch_run(monkeypatch, loader)

    stats = run(config=None)

    assert stats["repositories"] == 2
    assert stats["forecasts_written"] == 4  # 2 repositories x 2 horizons


def test_a_row_that_does_not_land_still_fails_the_readback(monkeypatch):
    # The check still has to catch its actual target - a write that silently
    # drops rows - now that it no longer counts the whole date.
    loader = FakeLoader(targets=[{"id": "github_1"}, {"id": "github_2"}])
    monkeypatch.setattr(loader, "write_forecasts", lambda rows: len(rows))  # writes nothing
    _patch_run(monkeypatch, loader)

    with pytest.raises(ForecastError, match="found 0"):
        run(config=None)
