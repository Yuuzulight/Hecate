"""GitHub search API.

Pulls repositories above a star threshold, most-starred first. The search API
caps out at 1000 results however you page it, which is fine here - the point is
the top of the distribution, not a complete census.
"""

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor

SEARCH_URL = "https://api.github.com/search/repositories"
PER_PAGE = 100
MAX_RESULTS = 1000
MIN_STARS = 1000


class GitHubExtractor(Extractor):
    source = "github"

    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"
        return headers

    def fetch(self) -> list[dict]:
        wanted = min(self.config.batch_size, MAX_RESULTS)
        rows: list[dict] = []
        page = 1

        while len(rows) < wanted:
            params = {
                "q": f"stars:>{MIN_STARS}",
                "sort": "stars",
                "order": "desc",
                "per_page": min(PER_PAGE, wanted - len(rows)),
                "page": page,
            }
            response = self.session.get(
                SEARCH_URL, params=params, headers=self._headers(), timeout=TIMEOUT
            )
            self._check(response)

            items = response.json().get("items", [])
            if not items:
                break

            rows.extend(self._transform_to_schema(item) for item in items)
            page += 1

        return rows[:wanted]

    def _check(self, response) -> None:
        """Turn a bad response into an ExtractError, saying so when it's the rate limit."""
        if response.ok:
            return
        # - GitHub answers an exhausted rate limit with 403 or 429 plus a
        #   remaining count of zero. Retrying immediately just burns the reset
        #   window, so give up and say when it lifts.
        if response.status_code in (403, 429) and response.headers.get(
            "X-RateLimit-Remaining"
        ) == "0":
            reset = response.headers.get("X-RateLimit-Reset", "unknown")
            raise ExtractError(f"github: rate limit exhausted, resets at {reset}")
        raise ExtractError(f"github: search returned {response.status_code}")

    def _transform_to_schema(self, raw: dict) -> dict:
        return {
            "id": f"github_{raw['id']}",
            "source": self.source,
            "name": raw.get("name"),
            "url": raw.get("html_url"),
            "stars": raw.get("stargazers_count", 0),
            "forks": raw.get("forks_count", 0),
            "language": raw.get("language"),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "description": raw.get("description"),
            "extracted_at": self.now(),
        }
