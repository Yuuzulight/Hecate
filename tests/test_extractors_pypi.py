"""PyPI extractor: metadata mapping, release dates, missing packages."""

import pytest
import requests

from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.extractors.pypi import PyPiExtractor
from tests.test_extractors import FakeResponse

PACKAGE = {
    "info": {
        "name": "requests",
        "summary": "Python HTTP for Humans.",
        "package_url": "https://pypi.org/project/requests/",
        "project_url": "https://pypi.org/project/requests/",
        # - The deprecated stub the real API returns.
        "downloads": {"last_day": -1, "last_month": -1, "last_week": -1},
    },
    "releases": {
        "2.0.0": [{"upload_time_iso_8601": "2013-09-24T10:00:00.000000Z"}],
        "2.32.0": [{"upload_time_iso_8601": "2024-05-20T10:00:00.000000Z"}],
    },
    "urls": [{"upload_time_iso_8601": "2026-05-14T19:25:26.443000Z"}],
}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("BATCH_SIZE", "1")
    return Config()


RANKED = [{"project": "requests", "download_count": 434500}]


def stub(extractor, responses, ranked=None):
    """Answer the rankings request, then the package lookups.

    The rankings call is not recorded, so `calls` stays a list of package URLs
    and assertions about the first lookup mean what they say.
    """
    queue = list(responses)
    calls = []

    def fake_get(url, **kwargs):
        if "top-pypi-packages" in url:
            return FakeResponse({"rows": ranked if ranked is not None else RANKED})
        calls.append(url)
        return queue.pop(0) if queue else FakeResponse(PACKAGE)

    extractor.session.get = fake_get
    return calls


def test_maps_metadata_to_the_standard_schema(config):
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)])

    row = extractor.fetch()[0]
    assert row["id"] == "pypi_requests"
    assert row["source"] == "pypi"
    assert row["name"] == "requests"
    assert row["url"] == "https://pypi.org/project/requests/"
    assert row["description"] == "Python HTTP for Humans."


def test_language_is_python(config):
    # - Not a guess: PyPI only hosts Python distributions.
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)])
    assert extractor.fetch()[0]["language"] == "Python"


def test_created_at_is_the_earliest_release_not_the_latest(config):
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)])
    assert extractor.fetch()[0]["created_at"] == "2013-09-24T10:00:00.000000Z"


def test_updated_at_is_the_current_release_upload(config):
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)])
    assert extractor.fetch()[0]["updated_at"] == "2026-05-14T19:25:26.443000Z"


def test_the_deprecated_downloads_stub_is_never_used(config):
    # - The package payload carries {last_month: -1}, which is PyPI's
    #   deprecated stub. When the ranking has no figure either, the answer is
    #   nothing - never -1, which would read as a real count.
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)],
         ranked=[{"project": "requests", "download_count": None}])
    assert extractor.fetch()[0]["downloads"] is None


def test_a_package_with_no_releases_has_no_created_at(config):
    extractor = PyPiExtractor(config)
    bare = dict(PACKAGE, releases={}, urls=[])
    stub(extractor, [FakeResponse(bare)])

    row = extractor.fetch()[0]
    assert row["created_at"] is None
    assert row["updated_at"] is None


def test_a_release_with_no_upload_time_is_skipped(config):
    extractor = PyPiExtractor(config)
    odd = dict(PACKAGE, releases={"0.1": [{}], "0.2": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}]})
    stub(extractor, [FakeResponse(odd)])
    assert extractor.fetch()[0]["created_at"] == "2020-01-01T00:00:00Z"


def test_the_ranking_supplies_download_figures(config):
    # - PyPI's own API answers -1 for every window, so these come from the
    #   dataset instead, converted from its 30-day count to the weekly figure
    #   the column holds.
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)])
    assert extractor.fetch()[0]["downloads"] == int(434500 / 4.345)


def test_packages_are_taken_most_downloaded_first(config, monkeypatch):
    monkeypatch.setenv("BATCH_SIZE", "2")
    extractor = PyPiExtractor(Config())
    ranked = [
        {"project": "big", "download_count": 900},
        {"project": "medium", "download_count": 500},
        {"project": "small", "download_count": 10},
    ]
    seen = []

    def fake_get(url, **kwargs):
        if "top-pypi-packages" in url:
            return FakeResponse({"rows": ranked})
        seen.append(url)
        return FakeResponse(PACKAGE)

    extractor.session.get = fake_get
    extractor.fetch()
    assert len(seen) == 2
    assert "big" in seen[0] and "medium" in seen[1]


def test_an_unreachable_ranking_falls_back_rather_than_failing(config, monkeypatch):
    # - A third party endpoint having a bad day should narrow this source, not
    #   take it out.
    from pipeline.extractors.pypi import FALLBACK_PACKAGES

    monkeypatch.setenv("BATCH_SIZE", "3")
    extractor = PyPiExtractor(Config())

    def fake_get(url, **kwargs):
        if "top-pypi-packages" in url:
            raise requests.ConnectionError("no route")
        return FakeResponse(PACKAGE)

    extractor.session.get = fake_get
    rows = extractor.fetch()
    assert len(rows) == 3
    # - The fallback carries no download figures, so they stay empty.
    assert rows[0]["downloads"] is None
    assert FALLBACK_PACKAGES[0] == "requests"


def test_a_removed_package_is_skipped_not_fatal(config):
    extractor = PyPiExtractor(config)
    # - The one package in the ranking 404s.
    stub(extractor, [FakeResponse(status_code=404)])
    assert extractor.fetch() == []


def test_a_server_error_is_an_extract_error(config):
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(status_code=500)])
    with pytest.raises(ExtractError, match="returned 500"):
        extractor.fetch()




def test_the_configured_registry_is_used(config, monkeypatch):
    monkeypatch.setenv("PYPI_REGISTRY", "https://pypi.example.com")
    extractor = PyPiExtractor(Config())
    calls = stub(extractor, [FakeResponse(PACKAGE)])
    extractor.fetch()
    assert calls[0].startswith("https://pypi.example.com/pypi/")
