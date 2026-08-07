"""Hacker News, via the Algolia search API.

Free, no authentication, no rate limit worth planning around. Unlike every
other source this one produces *mentions* rather than repositories - a story is
an event about a project, not a project - so it does not go through the
transformer and does not write to raw_repositories.

Resolution is by link only. A story pointing at github.com/owner/repo is
matched against what we already store; a story that merely says "axum is great"
is left alone, because matching on a name is a fuzzy problem that fails quietly
and needs its own issue and a measured error rate.
"""

import re
from urllib.parse import urlparse

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
HITS_PER_PAGE = 100

# - Hosts whose URLs identify something the pipeline might already track.
REPO_HOSTS = ("github.com", "www.github.com", "gitlab.com", "www.gitlab.com")

# - Package pages, which name one artifact rather than an owner and a repo.
#   npmjs.com/package/<name>, including scoped names like @babel/parser, and
#   pypi.org/project/<name>.
PACKAGE_HOSTS = {
    "npmjs.com": "/package/",
    "www.npmjs.com": "/package/",
    "pypi.org": "/project/",
    "www.pypi.org": "/project/",
}

# - owner/repo, ignoring whatever follows: /tree/main, /issues/4, a trailing
#   slash, a .git suffix.
REPO_PATH = re.compile(r"^/([^/\s]+)/([^/\s#?]+)")

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')]+")


def canonical_repo_url(url: str) -> str | None:
    """Reduce a URL to the project page it belongs to, or None.

    https://github.com/Owner/Repo/tree/main -> https://github.com/owner/repo

    Lowercased, because the same project is linked with every capitalisation
    and the point is to match one stored row.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.hostname is None:
        return None
    host = parsed.hostname.lower()

    # - Package pages name one artifact, so the whole remaining path is the
    #   name. Scoped npm names contain a slash and must survive intact.
    prefix = PACKAGE_HOSTS.get(host)
    if prefix:
        path = parsed.path or ""
        if not path.startswith(prefix):
            return None
        name = path[len(prefix):].strip("/").split("#")[0].split("?")[0]
        if not name:
            return None
        bare = host.removeprefix("www.")
        return f"https://{bare}{prefix}{name}".lower()

    if host not in REPO_HOSTS:
        return None

    match = REPO_PATH.match(parsed.path or "")
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return f"https://{host.removeprefix('www.')}/{owner}/{repo}".lower()


class HackerNewsExtractor(Extractor):
    source = "hackernews"

    def fetch(self) -> list[dict]:
        """Return stories that link somewhere resolvable, unresolved.

        The repository id is filled in later, once the URLs can be looked up
        against what is stored.
        """
        response = self.session.get(
            SEARCH_URL,
            params={
                "tags": "story",
                "hitsPerPage": min(HITS_PER_PAGE, self.config.batch_size),
                "query": "github.com",
            },
            timeout=TIMEOUT,
        )
        if not response.ok:
            raise ExtractError(f"hackernews: search returned {response.status_code}")

        found = []
        for hit in response.json().get("hits", []):
            mention = self._transform_to_schema(hit)
            if mention is not None:
                found.append(mention)
        return found

    def _candidate_url(self, hit: dict) -> str | None:
        """The project this story points at, from its link or its text."""
        direct = canonical_repo_url(hit.get("url") or "")
        if direct:
            return direct
        for candidate in URL_IN_TEXT.findall(hit.get("story_text") or ""):
            resolved = canonical_repo_url(candidate)
            if resolved:
                return resolved
        return None

    def _transform_to_schema(self, raw: dict) -> dict | None:
        object_id = raw.get("objectID")
        target = self._candidate_url(raw)
        if not object_id or not target:
            return None

        return {
            "id": f"hackernews_{object_id}",
            "platform": "hackernews",
            # - Filled in by the resolver once the URL is matched to a stored
            #   repository. A mention with nothing to point at is discarded.
            "repository_id": None,
            "target_url": target,
            "title": raw.get("title"),
            "url": f"https://news.ycombinator.com/item?id={object_id}",
            "score": raw.get("points"),
            "comments": raw.get("num_comments"),
            "author": raw.get("author"),
            # - Hacker News has no equivalent of a subreddit.
            "channel": None,
            "posted_at": raw.get("created_at"),
            "extracted_at": self.now(),
        }
