"""TimesFM wrapper: real model, real inference - there's no meaningful way
to fake a forecasting model's output and still test that the wrapper wires
it correctly. Skipped, visibly, when timesfm[torch] isn't installed - not
silently, the same way tests/test_loaders_integration.py fails loudly
rather than skipping quietly when it's pointed at the wrong thing.
"""

import pytest

pytest.importorskip("timesfm", reason="install requirements-forecast.txt to run this")

from pipeline.forecast.model import forecast, load_model


@pytest.fixture(scope="module")
def model():
    return load_model()


def test_forecast_returns_ordered_quantiles(model):
    series = [float(x) for x in range(1, 30)]  # monotonically increasing
    result = forecast(model, series, horizon_days=7)

    assert result["p10"] <= result["p50"] <= result["p90"]
    assert all(isinstance(v, int) for v in result.values())


def test_forecast_never_predicts_negative_stars(model):
    # - A sharply declining series, to make sure clamping at zero actually
    #   engages rather than only ever being exercised by non-negative input.
    series = [float(x) for x in range(200, 1, -20)]
    result = forecast(model, series, horizon_days=7)

    assert result["p10"] >= 0
    assert result["p50"] >= 0
    assert result["p90"] >= 0
