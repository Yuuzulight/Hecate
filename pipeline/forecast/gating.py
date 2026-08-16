"""Confidence gating for forecasts: how much history is enough to trust one.

These thresholds are not a generic rule of thumb - they're the real
breakeven points a throwaway spike found by backtesting TimesFM 2.5
zero-shot against real npm download-count series (a proxy for sparse,
count-like data - see the design doc for why) at a range of context
lengths, requiring it to beat a naive "repeat the last value" baseline by
at least 20% lower error on 7-day-ahead forecasts.

The 7-day threshold (14 days observed) is what the spike actually
validated: TimesFM technically cleared the 20% bar even at 8 days
(20.3% improvement), but 14 is where it jumps to a real, qualitative
win (58.7% improvement, quantile band calibrated at 80.7% against an
80% target) rather than a thin margin right at the line.

The 30-day threshold (60 days observed) was never spike-tested - the
spike only backtested 7-day-ahead forecasts - and stays on the
original, untested 2x-horizon heuristic until it gets a backtest of
its own. Do not treat it as validated just because it lives next to a
number that is.
"""

GATE_THRESHOLDS = {7: 14, 30: 60}


def suppressed_reason(days_observed: int, horizon_days: int) -> str | None:
    """None if a real forecast should be produced for this horizon;
    otherwise the reason it's being suppressed instead."""
    threshold = GATE_THRESHOLDS.get(horizon_days)
    if threshold is None:
        return f"unknown horizon {horizon_days}"
    if days_observed < threshold:
        return "insufficient_history"
    return None
