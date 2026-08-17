"""The forecast job: top repositories in, a gated quantile forecast for
each supported horizon out.

Optional in ops/windowed-run.ps1's sequence, the same way hecate-embed is -
a failure here is logged and does not fail the day, since a forecast is an
addition on top of the daily collection rather than part of it.
"""

import sys
from datetime import date, datetime, timedelta, timezone

from pipeline.config import Config
from pipeline.exceptions import ForecastError, HecateError
from pipeline.forecast.gating import suppressed_reason
from pipeline.forecast.model import MODEL_ID, MODEL_REVISION, forecast, load_model
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger

TOP_N = 50
HORIZONS = (7, 30)

MODEL_VERSION = f"{MODEL_ID.rsplit('/', 1)[-1]}@{MODEL_REVISION}"

# - Flagged (not failed) when this fraction or more of today's real,
#   non-suppressed forecasts predict no change from baseline at all - the
#   sign of a model that's echoing its input rather than forecasting,
#   which a crash-based check would never catch (see the design doc's
#   verification section).
DEGENERACY_FRACTION_THRESHOLD = 0.5


def forward_fill(series: list[tuple]) -> list[float]:
    """One value per calendar day between the first and last real snapshot,
    carrying the last known value forward across any gap - whether the gap
    is a missing row (a collection miss) or an explicit NULL stars value (a
    partial collection). Leading gaps before the first known value are
    dropped, not filled - there's nothing to carry forward yet."""
    known = [(captured_on, stars) for captured_on, stars in series if stars is not None]
    if not known:
        return []
    by_date = dict(known)
    start = known[0][0]
    end = known[-1][0]

    filled = []
    current = start
    last = None
    while current <= end:
        if current in by_date:
            last = by_date[current]
        filled.append(float(last))
        current += timedelta(days=1)
    return filled


def build_forecast_row(repository_id: str, forecast_date: date, horizon_days: int, series: list[tuple], model) -> dict:
    """One repository, one horizon: a real forecast if the gate clears,
    a suppressed row explaining why if it doesn't."""
    filled = forward_fill(series)
    days_observed = len(filled)
    baseline_stars = filled[-1] if filled else 0
    reason = suppressed_reason(days_observed, horizon_days)

    row = {
        "repository_id": repository_id,
        "forecast_date": forecast_date,
        "horizon_days": horizon_days,
        "days_observed": days_observed,
        "baseline_stars": baseline_stars,
        "predicted_stars_p10": None,
        "predicted_stars_p50": None,
        "predicted_stars_p90": None,
        "suppressed_reason": reason,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now(timezone.utc),
    }
    if reason is not None:
        return row

    prediction = forecast(model, filled, horizon_days)
    row["predicted_stars_p10"] = prediction["p10"]
    row["predicted_stars_p50"] = prediction["p50"]
    row["predicted_stars_p90"] = prediction["p90"]
    return row


def run(config: Config) -> dict:
    log = get_logger("forecast.run")
    loader = PostgreSQLLoader(config)
    loader.connect()

    try:
        targets = loader.top_forecast_targets(n=TOP_N)
        log.info("forecast targets selected", extra={"context": {"count": len(targets)}})

        model = load_model()
        today = date.today()

        rows = []
        for target in targets:
            series = loader.snapshot_series(target["id"])
            for horizon_days in HORIZONS:
                rows.append(build_forecast_row(target["id"], today, horizon_days, series, model))

        written = loader.write_forecasts(rows)

        # - Row-count sanity: read back what actually landed rather than
        #   trusting write_forecasts's own return value - the same
        #   discipline ops/windowed-run.ps1 already applies to the day as
        #   a whole, applied here to this one job.
        #
        # - Scoped to the repositories this run wrote, not to the whole
        #   date. Counting the date meant any leftover row failed the job
        #   on arithmetic that had nothing to do with the write: on
        #   2026-08-16 an aborted run's 12 rows made a correct 100-row run
        #   read back 112 and fail, every time, until they were deleted by
        #   hand. It also meant the job could not survive its own retry,
        #   since restartPolicy OnFailure makes that a second run on the
        #   same date.
        stored = loader.forecast_rows_for(today, repository_ids={r["repository_id"] for r in rows})
        if len(stored) != len(rows):
            raise ForecastError(
                f"expected {len(rows)} forecast rows for {today}, found {len(stored)}"
            )

        suppressed = sum(1 for r in rows if r["suppressed_reason"] is not None)
        real = [r for r in rows if r["suppressed_reason"] is None]
        degenerate = sum(1 for r in real if r["predicted_stars_p50"] == r["baseline_stars"])
        if real and degenerate / len(real) >= DEGENERACY_FRACTION_THRESHOLD:
            log.warning(
                "unusually many forecasts predict no change from baseline",
                extra={"context": {"degenerate": degenerate, "of": len(real)}},
            )

        stats = {
            "repositories": len(targets),
            "forecasts_written": written,
            "suppressed": suppressed,
            "degenerate": degenerate,
        }
        log.info("forecast run complete", extra={"context": stats})
        return stats
    finally:
        loader.close()


def main() -> int:
    log = get_logger("forecast.run")
    try:
        run(Config())
    except HecateError as exc:
        # - Non-zero, deliberately - ops/windowed-run.ps1 treats this job
        #   as optional and carries on, but a job that exits 0 having
        #   written nothing is indistinguishable from one that worked.
        log.error("forecast run failed", extra={"context": {"error": str(exc)}})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
