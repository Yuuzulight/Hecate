"""Confidence gating: the real, spike-derived thresholds, not the guessed
2x-horizon heuristic they replaced. See
docs/specs/2026-08-15-timesfm-forecasting-design.md for where the numbers
came from.
"""

from pipeline.forecast.gating import GATE_THRESHOLDS, suppressed_reason


def test_the_seven_day_gate_is_the_spike_validated_fourteen_days():
    assert GATE_THRESHOLDS[7] == 14


def test_the_thirty_day_gate_is_the_untested_sixty_day_heuristic():
    # - Never spike-tested - the spike only backtested 7-day-ahead
    #   forecasts. Pinned here so nobody quietly "fixes" this to look
    #   validated when it isn't.
    assert GATE_THRESHOLDS[30] == 60


def test_a_seven_day_forecast_is_suppressed_below_the_gate():
    assert suppressed_reason(13, 7) == "insufficient_history"


def test_a_seven_day_forecast_is_not_suppressed_at_the_gate():
    assert suppressed_reason(14, 7) is None


def test_a_thirty_day_forecast_is_suppressed_below_its_gate():
    assert suppressed_reason(59, 30) == "insufficient_history"


def test_a_thirty_day_forecast_is_not_suppressed_at_its_gate():
    assert suppressed_reason(60, 30) is None


def test_an_unrecognised_horizon_is_suppressed_with_its_own_reason():
    assert suppressed_reason(1000, 14) == "unknown horizon 14"
