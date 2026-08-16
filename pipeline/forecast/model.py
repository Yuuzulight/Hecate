"""TimesFM 2.5 zero-shot wrapper: one repository's daily star series in, a
quantile forecast out.

Pinned to the exact checkpoint revision a throwaway spike validated
(docs/specs/2026-08-15-timesfm-forecasting-design.md) - not "latest". An
unpinned rebuild of the forecast image could otherwise silently swap in a
different checkpoint with different behaviour, and forecast quality would
just quietly change with nothing to say why.
"""

import numpy as np

MODEL_ID = "google/timesfm-2.5-200m-pytorch"
MODEL_REVISION = "1d952420fba87f3c6dee4f240de0f1a0fbc790e3"

# - The largest horizon either gate in gating.py ever asks for. TimesFM's
#   own max_context (16,384 points) is irrelevant here - Hecate's real
#   series are two orders of magnitude shorter than that ceiling.
MAX_HORIZON_DAYS = 30

# - Confirmed against the real installed model - see Task 3 Step 2 of the
#   implementation plan this shipped from. The row is 10 values wide, not
#   the 11 the plan guessed. The real basis for these indices is the
#   installed timesfm==2.0.2 package's own documented output shape
#   (timesfm-2.0.2.dist-info/METADATA): "quantile_forecast.shape # (2, 12,
#   10): mean, then 10th to 90th quantiles." - column 0 is the mean,
#   columns 1-9 are the ascending 10th..90th quantiles, so index 1 = p10,
#   index 5 = the middle (50th) quantile, index -1 = the last (90th)
#   quantile. (The installed library's point forecast happens to equal
#   this row's index 5 too, but that's not independent evidence - reading
#   timesfm_2p5_torch.py shows the point forecast is literally
#   `full_forecast[..., 5]`, the same quantile array returned a second
#   time under a different name, so it would match whatever column the
#   library picked regardless of what that column represents.)
_P10_INDEX = 1
_P50_INDEX = 5
_P90_INDEX = -1


def load_model() -> "timesfm.TimesFM_2p5_200M_torch":
    import timesfm

    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    config = timesfm.ForecastConfig(
        max_context=1024,
        max_horizon=MAX_HORIZON_DAYS,
        use_continuous_quantile_head=True,
    )
    model.compile(config)
    return model


def forecast(model, series: list[float], horizon_days: int) -> dict:
    """One repository's quantile forecast, horizon_days ahead.

    Returns {"p10": int, "p50": int, "p90": int} - rounded and clamped at
    zero, since a star count can't go negative and a fractional star isn't
    a value that belongs on a dashboard.
    """
    _, quantiles = model.forecast(
        horizon=horizon_days, inputs=[np.array(series, dtype=np.float64)]
    )
    # - Row -1: the value at the end of the requested horizon, which is
    #   what "horizon_days forward" means here - not the first step ahead.
    last_step = quantiles[0][-1]
    return {
        "p10": max(0, round(float(last_step[_P10_INDEX]))),
        "p50": max(0, round(float(last_step[_P50_INDEX]))),
        "p90": max(0, round(float(last_step[_P90_INDEX]))),
    }
