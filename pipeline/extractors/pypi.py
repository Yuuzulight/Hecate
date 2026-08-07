"""PyPI JSON API.

Two things PyPI doesn't give you.

There's no endpoint for the most-downloaded packages, and the old XML-RPC search
was withdrawn, so the set below is a seeded list rather than a ranking. Adding
to PACKAGES widens it.

And the JSON API's `downloads` block is a deprecated stub - it answers -1 for
every window rather than a real figure. Actual numbers live in the public
BigQuery dataset, or behind pypistats.org, both of which are a separate job from
reading package metadata. Until one of those is wired up, downloads stays empty,
which is the honest answer rather than a zero that would read as nobody using it.

What PyPI does give, and npm doesn't: a real first-publish date, recoverable
from the release history, and a language that isn't a guess.
"""

from pipeline.exceptions import ExtractError
from pipeline.extractors.base import TIMEOUT, Extractor

# - Seeded by hand. Not a ranking, just a spread of things in wide use.
PACKAGES = (
    "requests", "urllib3", "boto3", "setuptools", "certifi", "charset-normalizer",
    "idna", "typing-extensions", "packaging", "python-dateutil", "six", "numpy",
    "pandas", "click", "pyyaml", "jinja2", "markupsafe", "attrs", "pydantic",
    "cryptography", "sqlalchemy", "flask", "django", "fastapi", "starlette",
    "httpx", "pytest", "black", "ruff", "mypy", "rich", "tqdm", "pillow",
    "scipy", "scikit-learn", "matplotlib", "psycopg2-binary", "redis",
    "celery", "beautifulsoup4",
)


class PyPiExtractor(Extractor):
    source = "pypi"

    def fetch(self) -> list[dict]:
        rows = []
        # - The whole list, every time. Slicing by batch_size would silently
        #   return whichever packages happen to be listed first rather than any
        #   meaningful subset, and there is no ranking here to slice by.
        for name in PACKAGES:
            raw = self._package(name)
            if raw is not None:
                rows.append(self._transform_to_schema(raw))
        return rows

    def fetch_by_url(self, url: str) -> dict | None:
        """One package, from a pypi.org/project/<name> URL, for discovery."""
        name = url.rstrip("/").split("/project/", 1)[-1]
        if not name or name == url:
            return None
        raw = self._package(name)
        return None if raw is None else self._transform_to_schema(raw)

    def _package(self, name: str) -> dict | None:
        """Fetch one package, or None if it isn't there any more."""
        response = self.session.get(
            f"{self.config.pypi_registry}/pypi/{name}/json", timeout=TIMEOUT
        )
        if response.status_code == 404:
            # - Packages do get removed. One gone is not a reason to fail the run.
            self.log.warning("package not found", extra={"context": {"package": name}})
            return None
        if not response.ok:
            raise ExtractError(f"pypi: {name} returned {response.status_code}")
        return response.json()

    def _first_release(self, releases: dict) -> str | None:
        """Earliest upload across every release, which is the real creation date."""
        uploads = [
            file["upload_time_iso_8601"]
            for files in (releases or {}).values()
            for file in files
            if file.get("upload_time_iso_8601")
        ]
        return min(uploads) if uploads else None

    def _latest_release(self, raw: dict) -> str | None:
        urls = raw.get("urls") or []
        for file in urls:
            if file.get("upload_time_iso_8601"):
                return file["upload_time_iso_8601"]
        return None

    def _transform_to_schema(self, raw: dict) -> dict:
        info = raw.get("info") or {}
        name = info.get("name")

        return {
            "id": f"pypi_{name}",
            "source": self.source,
            "name": name,
            "url": info.get("package_url") or info.get("project_url"),
            "stars": 0,
            "forks": 0,
            # - Not a guess, unlike npm. Everything on PyPI is a Python package.
            "language": "Python",
            "created_at": self._first_release(raw.get("releases")),
            "updated_at": self._latest_release(raw),
            "description": info.get("summary"),
            # - The API's own downloads block is the deprecated -1 stub. See the
            #   module docstring for where the real figures live.
            "downloads": None,
            "extracted_at": self.now(),
        }
