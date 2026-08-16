"""Ongoing verification that TimesFM still beats naive on Hecate's own real
data - not just the npm proxy data the original spike used. Run this
manually, periodically (monthly is a reasonable cadence) once repositories
have enough history to backtest meaningfully.

    python -m tools.forecast_backtest

Not part of the daily CronJob sequence - see pipeline/forecast/backtest.py's
module docstring for why.
"""

import sys

from pipeline.config import Config
from pipeline.forecast.backtest import backtest_repository
from pipeline.forecast.gating import GATE_THRESHOLDS
from pipeline.forecast.model import load_model
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger

HORIZON_DAYS = 7
BEAT_NAIVE_MARGIN = 0.20  # the same bar the original spike was held to


def main() -> int:
    log = get_logger("forecast.backtest")
    loader = PostgreSQLLoader(Config())
    loader.connect()

    try:
        targets = loader.top_forecast_targets(n=50)
        model = load_model()

        results = []
        for target in targets:
            series_rows = loader.snapshot_series(target["id"])
            series = [stars for _, stars in series_rows if stars is not None]
            if len(series) < GATE_THRESHOLDS[HORIZON_DAYS]:
                continue  # hasn't cleared its own gate yet - nothing to backtest
            result = backtest_repository(model, series, HORIZON_DAYS)
            if result is not None:
                results.append(result)

        if not results:
            log.info(
                "no repository has enough history to backtest yet",
                extra={"context": {"gate_days": GATE_THRESHOLDS[HORIZON_DAYS]}},
            )
            return 0

        avg_improvement = sum(r["improvement"] for r in results) / len(results)
        log.info(
            "backtest complete",
            extra={"context": {"repositories": len(results), "avg_improvement": round(avg_improvement, 4)}},
        )

        if avg_improvement < BEAT_NAIVE_MARGIN:
            log.warning(
                "forecast quality has dropped below the original spike's bar",
                extra={"context": {"avg_improvement": round(avg_improvement, 4), "required": BEAT_NAIVE_MARGIN}},
            )
    finally:
        loader.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
