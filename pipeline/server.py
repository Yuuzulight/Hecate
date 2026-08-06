"""Metrics endpoint.

The scheduled job's counters vanish when its pod exits, so anything you want to
look at between runs has to be read back off the database. This serves
/metrics on :8000 and refreshes those gauges on a timer.

It deliberately does not run extractions - that is the CronJob's job, and two
things writing the same rows on different schedules would only fight over the
same keys.

    python -m pipeline.server
"""

import sys
import time

from prometheus_client import start_http_server

from pipeline import metrics
from pipeline.config import Config
from pipeline.exceptions import HecateError, LoadError
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger

PORT = 8000
REFRESH_SECONDS = 60

STATS_QUERY = """
SELECT source,
       count(*),
       extract(epoch from now() - max(extracted_at))
FROM raw_repositories
GROUP BY source
"""


GAUGES = (metrics.repositories, metrics.last_extraction_age)

# - Sources reported last time round. A gauge keeps whatever it was last set to
#   until something clears it, so a source whose rows go away would otherwise
#   carry on reporting its final count forever - a metric outliving the data it
#   describes, which is worse than no metric.
_reported: set[str] = set()


def refresh(loader: PostgreSQLLoader) -> dict:
    """Pull current row counts and extraction ages into the gauges."""
    with loader.transaction() as cur:
        cur.execute(STATS_QUERY)
        rows = cur.fetchall()

    seen = {}
    for source, count, age in rows:
        metrics.repositories.labels(source=source).set(count)
        metrics.last_extraction_age.labels(source=source).set(age or 0)
        seen[source] = count

    for gone in _reported - seen.keys():
        for gauge in GAUGES:
            gauge.remove(gone)
    _reported.clear()
    _reported.update(seen)

    return seen


def serve(
    config: Config,
    refresh_seconds: int = REFRESH_SECONDS,
    cycles: int | None = None,
) -> None:
    """Serve /metrics, refreshing the database-backed gauges on a timer.

    Runs until stopped. `cycles` bounds the loop instead, so tests can watch it
    recover across iterations without running until something kills them.
    """
    log = get_logger("server")
    start_http_server(PORT)
    log.info("metrics endpoint listening", extra={"context": {"port": PORT}})

    loader = None
    done = 0
    try:
        while True:
            try:
                # - Reconnect if we have no connection, or lost the one we had.
                #   Without this, a database restart left the loop failing
                #   against a dead handle forever while the endpoint carried on
                #   serving - so the pod stayed healthy, the gauges froze, and
                #   the extraction-age metric that exists to catch a stalled
                #   pipeline froze along with everything else.
                if loader is None:
                    loader = PostgreSQLLoader(config)
                    loader.connect()

                # - No create_tables here. This only reads; the pipeline owns
                #   the schema. Against an empty database the query fails, gets
                #   counted, and is retried, which is the honest outcome.
                counts = refresh(loader)
                log.info("gauges refreshed", extra={"context": {"sources": counts}})
            except LoadError as exc:
                metrics.errors.labels(type="database", source="postgres").inc()
                log.error("refresh failed", extra={"context": {"error": str(exc)}})
                if loader is not None:
                    loader.close()
                    loader = None

            done += 1
            if cycles is not None and done >= cycles:
                return
            time.sleep(refresh_seconds)
    finally:
        if loader is not None:
            loader.close()


def main() -> int:
    log = get_logger("server")
    try:
        serve(Config())
    except HecateError as exc:
        log.error("server aborted", extra={"context": {"error": str(exc)}})
        return 1
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
