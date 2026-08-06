"""GitLab projects API.

The only source of the four that will just hand over its most-starred projects
in order, no seeding or ranking needed. It works without a token too, at a lower
rate limit, so GITLAB_TOKEN is worth setting but not required.

Language is the one gap: the project listing doesn't carry it, and GitLab only
reports it from a separate endpoint per project. That's one extra request per
row for a single field, which isn't worth it here, so language stays empty.
"""

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor

PROJECTS_PATH = "/api/v4/projects"
BASE_URL = "https://gitlab.com"
PER_PAGE = 100


class GitLabExtractor(Extractor):
    source = "gitlab"

    def _headers(self) -> dict:
        if self.config.gitlab_token:
            return {"PRIVATE-TOKEN": self.config.gitlab_token}
        return {}

    def fetch(self) -> list[dict]:
        wanted = self.config.batch_size
        rows: list[dict] = []
        page = 1

        while len(rows) < wanted:
            params = {
                "order_by": "star_count",
                "sort": "desc",
                "visibility": "public",
                "per_page": min(PER_PAGE, wanted - len(rows)),
                "page": page,
            }
            response = self.session.get(
                f"{BASE_URL}{PROJECTS_PATH}",
                params=params,
                headers=self._headers(),
                timeout=TIMEOUT,
            )
            self._check(response)

            projects = response.json()
            if not projects:
                break

            rows.extend(self._transform_to_schema(project) for project in projects)
            page += 1

        return rows[:wanted]

    def _check(self, response) -> None:
        if response.ok:
            return
        # - GitLab is explicit about the rate limit in its headers, so say when
        #   it lifts rather than reporting a bare 429.
        if response.status_code == 429:
            reset = response.headers.get("RateLimit-Reset", "unknown")
            raise ExtractError(f"gitlab: rate limited, resets at {reset}")
        if response.status_code == 401:
            raise ExtractError("gitlab: token rejected")
        raise ExtractError(f"gitlab: projects returned {response.status_code}")

    def _transform_to_schema(self, raw: dict) -> dict:
        # - `path` is the slug and lines up with what GitHub calls name; `name`
        #   on GitLab is the display title, which is a different thing.
        name = raw.get("path") or raw.get("name")

        return {
            "id": f"gitlab_{raw.get('id')}",
            "source": self.source,
            "name": name,
            "url": raw.get("web_url"),
            "stars": raw.get("star_count", 0),
            "forks": raw.get("forks_count", 0),
            # - See the module docstring: one request per project for one field.
            "language": None,
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("last_activity_at"),
            "description": raw.get("description"),
            "downloads": None,
            "extracted_at": self.now(),
        }
