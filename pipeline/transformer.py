"""Normalise extracted records into the shape the loader expects.

Each extractor already maps its own API onto the standard field names - it has
to, since only the extractor knows what its source calls things. What's left is
the part that has to be identical across sources no matter where a record came
from: required fields present, counts as integers, timestamps in one format,
URLs that actually look like URLs.

Keeping that here rather than in each extractor means a change to the schema is
one edit, and a new source gets the same treatment for free.
"""

from datetime import datetime, timezone

from pipeline import metrics
from pipeline.exceptions import TransformError
from pipeline.logger import get_logger

SOURCES = ("github", "npm", "pypi", "gitlab")

REQUIRED = ("id", "source", "name", "url", "extracted_at")


def _timestamp(value) -> str | None:
    """Return an ISO-8601 UTC string, or None if there's nothing usable."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    # - Naive timestamps come back from a few of the registries. Treating them
    #   as UTC is a guess, but a consistent one, and better than mixing aware
    #   and naive values in the same column.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _count(value) -> int:
    """Star and fork counts, coerced. Anything unusable counts as zero."""
    try:
        # - Via float, because int("1234.0") raises and a source reporting a
        #   whole number with a decimal point should not silently read as zero.
        count = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(count, 0)


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class RepositoryTransformer:
    def __init__(self) -> None:
        self.log = get_logger("transformers.repository")

    def transform(self, record: dict) -> dict:
        """Normalise one record. Raises TransformError if it can't be salvaged."""
        source = _text(record.get("source"))
        if source not in SOURCES:
            raise TransformError(f"unknown source {record.get('source')!r}")

        row = {
            "id": _text(record.get("id")),
            "source": source,
            "name": _text(record.get("name")),
            "url": _text(record.get("url")),
            "stars": _count(record.get("stars")),
            "forks": _count(record.get("forks")),
            "language": _text(record.get("language")),
            "created_at": _timestamp(record.get("created_at")),
            "updated_at": _timestamp(record.get("updated_at")),
            "description": _text(record.get("description")),
            # - Left as None when the source has no such metric, rather than
            #   coerced to zero. GitHub reporting no downloads is not the same
            #   claim as a package nobody installs.
            "downloads": None if record.get("downloads") is None else _count(record.get("downloads")),
            "extracted_at": _timestamp(record.get("extracted_at")),
        }

        missing = [field for field in REQUIRED if not row[field]]
        if missing:
            raise TransformError(
                f"{record.get('id')}: missing required {', '.join(missing)}"
            )

        if not row["url"].startswith(("http://", "https://")):
            raise TransformError(f"{row['id']}: {row['url']!r} is not a URL")

        return row

    def transform_all(self, records: list[dict], source: str) -> list[dict]:
        """Normalise a batch, dropping records that can't be and saying which."""
        rows = []
        with metrics.transform_duration.labels(source=source).time():
            for record in records:
                try:
                    rows.append(self.transform(record))
                except TransformError as exc:
                    metrics.errors.labels(type="transform", source=source).inc()
                    self.log.warning(
                        "record dropped",
                        extra={"context": {"source": source, "reason": str(exc)}},
                    )

        metrics.rows_processed.labels(stage="transform", source=source).inc(len(rows))
        self.log.info(
            "transform finished",
            extra={
                "context": {
                    "source": source,
                    "kept": len(rows),
                    "dropped": len(records) - len(rows),
                }
            },
        )
        return rows
