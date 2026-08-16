"""The backtest methodology a throwaway spike used to find the real
confidence-gating thresholds (docs/specs/2026-08-15-timesfm-forecasting-design.md),
reusable here against Hecate's own real snapshot history instead of the
npm proxy data the spike used - this is what actually catches a quality
regression the daily job's row-count and degeneracy checks (pipeline/forecast/run.py)
cannot, since those check that a forecast was produced, not that it's good.
"""

import statistics

from pipeline.forecast.model import forecast


def naive_forecast(context: list[float], horizon_days: int) -> list[float]:
    """Repeat the last known value - the baseline TimesFM has to beat."""
    return [context[-1]] * horizon_days


def mape(actual: list[float], predicted: list[float]) -> float:
    """Mean absolute percentage error. Zero actuals are skipped - a
    percentage error against zero is undefined, not zero."""
    errors = [abs(a - p) / a for a, p in zip(actual, predicted) if a != 0]
    return statistics.mean(errors) if errors else float("nan")


def rolling_folds(series: list[float], context_len: int, horizon_days: int, max_folds: int = 15) -> list[tuple]:
    """(context_window, actual_future) pairs, stepped across the series.
    Empty if the series isn't even long enough for one fold."""
    n = len(series)
    if n < context_len + horizon_days:
        return []
    folds = []
    step = max(1, (n - context_len - horizon_days) // max_folds)
    start = 0
    while start + context_len + horizon_days <= n:
        folds.append((series[start : start + context_len], series[start + context_len : start + context_len + horizon_days]))
        start += step
    return folds


def backtest_repository(model, series: list[float], horizon_days: int) -> dict | None:
    """One repository's TimesFM-vs-naive comparison, folded across its own
    history. None if there's not enough history for even one fold."""
    folds = rolling_folds(series, context_len=max(14, horizon_days * 2), horizon_days=horizon_days)
    if not folds:
        return None

    timesfm_errors = []
    naive_errors = []
    for context, actual in folds:
        naive_errors.append(mape(actual, naive_forecast(context, horizon_days)))
        prediction = forecast(model, context, horizon_days)
        timesfm_errors.append(mape(actual, [prediction["p50"]] * horizon_days))

    timesfm_mape = statistics.mean(e for e in timesfm_errors if e == e)
    naive_mape = statistics.mean(e for e in naive_errors if e == e)
    improvement = (naive_mape - timesfm_mape) / naive_mape if naive_mape else float("nan")

    return {
        "timesfm_mape": timesfm_mape,
        "naive_mape": naive_mape,
        "improvement": improvement,
        "n_folds": len(folds),
    }
