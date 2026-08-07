"""PyPI JSON API.

Two things PyPI doesn't give you.

There's no endpoint for the most-downloaded packages and the old XML-RPC search
was withdrawn, so the ranking comes from a published dataset built on the
official BigQuery download statistics. That also supplies the download figures
the JSON API will not.

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
# - PyPI publishes no ranking and its old search was withdrawn, so the sample
#   has to come from somewhere else. This dataset is generated from the official
#   BigQuery download statistics and carries the figures PyPI's own JSON API
#   refuses to give, which fixes the empty downloads column at the same time.
#
#   It is a third party endpoint, so the hand-written list below stays as a
#   fallback. Losing the ranking should narrow this source, not kill it.
TOP_PACKAGES_URL = (
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"
)

# - The dataset counts a 30 day window; npm reports weekly. Storing both raw in
#   one column would make any cross-source comparison silently wrong, so this
#   converts to the weekly figure the column is documented to hold. Average
#   weeks per month, not four, or every PyPI package would read ~8% high.
WEEKS_PER_MONTH = 4.345

FALLBACK_PACKAGES = (
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

    def _ranked_packages(self) -> list[tuple[str, int | None]]:
        """Package names most-downloaded first, with their monthly figure.

        Falls back to the hand-written list if the dataset is unreachable. A
        third party endpoint having a bad day should narrow this source, not
        take it out - and the fallback has no download figures, which is why
        they come back as None rather than zero.
        """
        try:
            response = self.session.get(TOP_PACKAGES_URL, timeout=TIMEOUT)
            if response.ok:
                rows = response.json().get("rows", [])
                if rows:
                    return [(r["project"], r.get("download_count")) for r in rows]
            self.log.warning(
                "top packages unavailable, using the fallback list",
                extra={"context": {"status": response.status_code}},
            )
        except Exception as exc:
            self.log.warning(
                "top packages unreachable, using the fallback list",
                extra={"context": {"error": str(exc)}},
            )
        return [(name, None) for name in FALLBACK_PACKAGES]

    def fetch(self) -> list[dict]:
        rows = []
        # - Ranked, so slicing by batch_size takes the most downloaded rather
        #   than whichever happened to be listed first.
        for name, monthly in self._ranked_packages()[: self.config.batch_size]:
            raw = self._package(name)
            if raw is not None:
                rows.append({
                    **self._transform_to_schema(raw),
                    "downloads": self._weekly(monthly),
                })
        return rows

    @staticmethod
    def _weekly(monthly: int | None) -> int | None:
        """Monthly downloads as a weekly figure, or None if we have neither."""
        return None if monthly is None else int(monthly / WEEKS_PER_MONTH)

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
