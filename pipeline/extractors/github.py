"""GitHub search API.

Pulls repositories above a star threshold, most-starred first. The search API
caps out at 1000 results however you page it, which is fine here - the point is
the top of the distribution, not a complete census.
"""

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import Extractor

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
        return self.paginate(
            lambda page, remaining: {
                "url": SEARCH_URL,
                "headers": self._headers(),
                "params": {
                    "q": f"stars:>{MIN_STARS}",
                    "sort": "stars",
                    "order": "desc",
                    # - The search API caps at 1000 results however you page it.
                    "per_page": min(PER_PAGE, remaining, MAX_RESULTS),
                    "page": page,
                },
            },
            lambda response: response.json().get("items", []),
        )

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
            # - pushed_at, not updated_at. GitHub bumps updated_at whenever the
            #   repository record changes at all, and that includes someone
            #   starring it - so for anything popular it is always today, and
            #   days_since_update measures attention rather than maintenance.
            #   pushed_at is the last actual commit, which is the question.
            "updated_at": raw.get("pushed_at") or raw.get("updated_at"),
            "description": raw.get("description"),
            "extracted_at": self.now(),
        }
