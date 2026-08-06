"""Shared behaviour for every extractor.

Subclasses implement fetch() and _transform_to_schema(). Everything else -
the session, retries, timeouts, timing, and metrics - lives here so the four
sources don't each grow their own version of it.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline import metrics
from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.logger import get_logger

TIMEOUT = 10

# - Retried statuses. 429 and the 5xx range are worth another go; a 404 or a
#   bad token never is.
RETRY_STATUSES = (429, 500, 502, 503, 504)


class Extractor(ABC):
    source: str

    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = get_logger(f"extractors.{self.source}")
        self.session = requests.Session()

        retry = Retry(
            total=config.retry_attempts,
            backoff_factor=1,
            status_forcelist=RETRY_STATUSES,
            allowed_methods=("GET",),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def extract(self) -> list[dict]:
        """Run fetch(), timing it and recording what came back."""
        self.log.info("extract started", extra={"context": {"source": self.source}})
        try:
            with metrics.extract_duration.labels(source=self.source).time():
                rows = self.fetch()
        except ExtractError:
            metrics.errors.labels(type="extract", source=self.source).inc()
            raise
        except requests.RequestException as exc:
            metrics.errors.labels(type="extract", source=self.source).inc()
            raise ExtractError(f"{self.source}: request failed: {exc}") from exc

        metrics.rows_processed.labels(stage="extract", source=self.source).inc(len(rows))
        self.log.info(
            "extract finished",
            extra={"context": {"source": self.source, "rows": len(rows)}},
        )
        return rows

    def paginate(self, page_params, read_page) -> list[dict]:
        """Walk pages until batch_size is reached or a page comes back empty.

        `page_params(page, remaining)` builds the query for one page and
        `read_page(response)` pulls the records out of it - which is all that
        actually differs between the two hosts that paginate this way.
        """
        wanted = self.config.batch_size
        rows: list[dict] = []
        page = 1

        while len(rows) < wanted:
            response = self.session.get(
                **page_params(page, wanted - len(rows)), timeout=TIMEOUT
            )
            self._check(response)

            records = read_page(response)
            if not records:
                break

            rows.extend(self._transform_to_schema(record) for record in records)
            page += 1

        return rows[:wanted]

    def _check(self, response) -> None:
        """Turn a bad response into an ExtractError. Overridden where a source
        distinguishes rate limiting from ordinary failure."""
        if not response.ok:
            raise ExtractError(f"{self.source}: returned {response.status_code}")

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Return records from this source, already in the standard schema."""
