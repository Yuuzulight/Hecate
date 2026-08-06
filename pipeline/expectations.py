"""Data quality checks, run after load.

Different job from the transformer. That one decides whether a record is worth
keeping and drops it if not, one record at a time. This looks at what actually
landed and reports on it - how much of the batch is trustworthy, which is a
question about the batch rather than about any single row.

Nothing here stops a run. A failed expectation means the data is worth a second
look, not that the pipeline should throw away a day's collection, so failures
are counted and logged and the run carries on.

The original plan named Great Expectations for this. These are range and format
assertions over eight columns - a few dozen lines against a dependency with its
own config tree and version pinning. If the checks ever outgrow that, swapping
the library in is a contained change.
"""

from datetime import datetime, timedelta, timezone

from pipeline import metrics
from pipeline.logger import get_logger
from pipeline.transformer import parse_timestamp

# - A ceiling for catching nonsense, not a prediction. The most starred
#   repository is already past half a million, so anything close to real
#   figures would start failing on good data instead of bad.
MAX_COUNT = 100_000_000
SOURCES = ("github", "npm", "pypi", "gitlab")
# - Anything older suggests the scheduled run has stopped happening.
STALE_AFTER = timedelta(hours=48)


def _in_range(value) -> bool:
    # - bool is a subclass of int, so True would otherwise pass as a count of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 0 <= value <= MAX_COUNT


def _not_future(value) -> bool:
    parsed = parse_timestamp(value)
    if parsed is None:
        # - Absent is allowed; npm reports no creation date at all.
        return value in (None, "")
    return parsed <= datetime.now(timezone.utc)


def _recent(value) -> bool:
    parsed = parse_timestamp(value)
    if parsed is None:
        return False
    return datetime.now(timezone.utc) - parsed <= STALE_AFTER


# - Each check is a name and a predicate over one row. Order is the order they
#   get reported in.
ROW_CHECKS = (
    ("id_present", lambda row: bool(row.get("id"))),
    ("name_present", lambda row: bool(row.get("name"))),
    ("url_present", lambda row: bool(row.get("url"))),
    ("url_is_http", lambda row: str(row.get("url", "")).startswith(("http://", "https://"))),
    ("source_known", lambda row: row.get("source") in SOURCES),
    ("stars_in_range", lambda row: _in_range(row.get("stars"))),
    ("forks_in_range", lambda row: _in_range(row.get("forks"))),
    ("created_at_not_future", lambda row: _not_future(row.get("created_at"))),
    ("extracted_at_present", lambda row: bool(row.get("extracted_at"))),
    ("extracted_at_recent", lambda row: _recent(row.get("extracted_at"))),
)


class RepositoryExpectations:
    def __init__(self) -> None:
        self.log = get_logger("expectations")

    def validate(self, rows: list[dict]) -> dict:
        """Check a batch. Returns counts per check plus a pass rate."""
        failures = {name: 0 for name, _ in ROW_CHECKS}
        examples: dict[str, str] = {}

        for row in rows:
            for name, passes in ROW_CHECKS:
                if not passes(row):
                    failures[name] += 1
                    examples.setdefault(name, str(row.get("id")))

        # - Uniqueness is about the batch, not a row, so it sits outside the loop.
        ids = [row.get("id") for row in rows]
        duplicates = len(ids) - len(set(ids))
        failures["id_unique"] = duplicates

        total = len(rows)
        for name, failed in failures.items():
            passed = total - failed
            if passed:
                metrics.quality_checks.labels(check=name, outcome="pass").inc(passed)
            if failed:
                metrics.quality_checks.labels(check=name, outcome="fail").inc(failed)
                self.log.warning(
                    "quality check failed",
                    extra={
                        "context": {
                            "check": name,
                            "rows": failed,
                            "of": total,
                            "example_id": examples.get(name),
                        }
                    },
                )

        checked = total * len(failures)
        failed_total = sum(failures.values())
        report = {
            "rows": total,
            "failures": {name: count for name, count in failures.items() if count},
            "pass_rate": round(1 - failed_total / checked, 4) if checked else 1.0,
        }
        self.log.info("quality checked", extra={"context": report})
        return report
