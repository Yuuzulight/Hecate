"""npm registry.

npm has no endpoint for "the most downloaded packages" - search needs a real
text query, and a wildcard is rejected outright. So this seeds the search with a
spread of broad ecosystem keywords, boosted hard towards popularity, then unions
the results and keeps whatever has the most weekly downloads.

That makes the sample keyword-shaped rather than a true global top-N, which is
worth being honest about: a popular package tagged with none of these keywords
won't appear. Widening KEYWORDS widens the net.

Downloads are the useful signal here. Stars and forks are GitHub's units and
don't exist for a package, so they stay at zero.
"""

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor

SEARCH_PATH = "/-/v1/search"
PER_QUERY = 250

KEYWORDS = (
    "javascript", "typescript", "react", "node", "cli",
    "framework", "testing", "build", "server", "database",
)


def registry_doc_to_row(raw: dict, source: str = "npm") -> dict:
    """Map one full npm registry document to a raw_repositories row.

    Shared between fetch_by_url (fetched by URL during discovery) and the
    real-time listener (received from the CouchDB _changes feed) - both
    hand this function the exact same document shape, npm's own per-package
    registry document, just by two different paths.
    """
    latest = (raw.get("dist-tags") or {}).get("latest")
    versions = raw.get("time") or {}
    name = raw.get("name")

    return {
        "id": f"{source}_{name}",
        "source": source,
        "name": name,
        "url": f"https://www.npmjs.com/package/{name}",
        "stars": 0,
        "forks": 0,
        "language": None,
        "created_at": versions.get("created"),
        "updated_at": versions.get("modified") or versions.get(latest),
        "description": raw.get("description"),
        # - Downloads live on a different endpoint. Left empty rather than
        #   guessed; the next scheduled search run fills it in.
        "downloads": None,
        "extracted_at": Extractor.now(),
    }


class NpmExtractor(Extractor):
    source = "npm"

    def fetch(self) -> list[dict]:
        wanted = self.config.batch_size
        found: dict[str, dict] = {}

        for keyword in KEYWORDS:
            for raw in self._search(keyword):
                row = self._transform_to_schema(raw)
                if row["id"] not in found:
                    found[row["id"]] = row

        # - Rank across everything the keywords turned up, so the batch is the
        #   most-installed packages overall rather than the first keyword's.
        ranked = sorted(found.values(), key=lambda row: row["downloads"] or 0, reverse=True)
        return ranked[:wanted]

    def fetch_by_url(self, url: str) -> dict | None:
        """One package, from an npmjs.com/package/<name> URL.

        Used by discovery. The registry's per-package document has a different
        shape from a search hit, so this maps it rather than reusing
        _transform_to_schema - the fields genuinely differ.
        """
        name = url.rstrip("/").split("/package/", 1)[-1]
        if not name or name == url:
            return None

        response = self.session.get(
            f"{self.config.npm_registry}/{name}", timeout=TIMEOUT
        )
        if response.status_code == 404:
            self.log.warning("package not found", extra={"context": {"package": name}})
            return None
        if not response.ok:
            raise ExtractError(f"npm: {name} returned {response.status_code}")

        raw = response.json()
        return registry_doc_to_row(raw)

    def _search(self, keyword: str) -> list[dict]:
        params = {
            "text": f"keywords:{keyword}",
            "size": min(PER_QUERY, self.config.batch_size),
            "popularity": 1.0,
        }
        response = self.session.get(
            f"{self.config.npm_registry}{SEARCH_PATH}", params=params, timeout=TIMEOUT
        )
        if not response.ok:
            raise ExtractError(f"npm: search for {keyword!r} returned {response.status_code}")
        return response.json().get("objects", [])

    def _transform_to_schema(self, raw: dict) -> dict:
        package = raw.get("package", {})
        links = package.get("links") or {}
        downloads = raw.get("downloads") or {}
        name = package.get("name")

        return {
            "id": f"npm_{name}",
            "source": self.source,
            "name": name,
            # - Fall back to the canonical package page; a few packages carry no
            #   homepage or repository link at all.
            "url": links.get("npm") or (f"https://www.npmjs.com/package/{name}" if name else None),
            "stars": 0,
            "forks": 0,
            # - Search results don't say what a package is written in, and
            #   guessing "JavaScript" would be inventing data.
            "language": None,
            # - `date` is the last publish, not the first. npm search doesn't
            #   expose a creation date, so created_at stays empty.
            "created_at": None,
            "updated_at": package.get("date"),
            "description": package.get("description"),
            "downloads": downloads.get("weekly"),
            "extracted_at": self.now(),
        }
