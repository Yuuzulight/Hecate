"""GitHub search API.

Pulls repositories above a star threshold, most-starred first. The search API
caps out at 1000 results however you page it, which is fine here - the point is
the top of the distribution, not a complete census.
"""

from urllib.parse import urlparse

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor

API_URL = "https://api.github.com"
SEARCH_URL = f"{API_URL}/search/repositories"
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

    def fetch_by_url(self, project_url: str) -> dict | None:
        """Fetch one repository by its page URL, or None if it isn't there.

        Same field names as the search response, so the existing mapping is
        reused rather than a second one written that would drift from it.
        """
        path = urlparse(project_url).path.strip("/")
        if path.count("/") != 1:
            return None

        response = self.session.get(
            f"{API_URL}/repos/{path}", headers=self._headers(), timeout=TIMEOUT
        )
        if response.status_code == 404:
            # - Renamed, deleted or private. Not an error worth failing a run.
            self.log.warning("repository not found", extra={"context": {"url": project_url}})
            return None
        self._check(response)
        return self._transform_to_schema(response.json())

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
            # - open_issues_count includes pull requests, which is why the
            #   column says so. Treating it as a backlog overstates it, badly
            #   on projects with a lot of PR traffic.
            "open_issues_and_prs": raw.get("open_issues_count"),
            "archived": raw.get("archived"),
            "is_fork": raw.get("fork"),
            # - Deliberately not watchers_count. In the search API that is a
            #   duplicate of stargazers_count kept for backwards compatibility,
            #   so mapping it would store the star count twice under two names.
            #   The real figure is subscribers_count, which only appears on the
            #   individual repository endpoint - one request per row, the trade
            #   already refused for GitLab language.
            "extracted_at": self.now(),
        }
