"""PyPI extractor: metadata mapping, release dates, missing packages."""

import pytest

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


def stub(extractor, responses):
    queue = list(responses)
    calls = []

    def fake_get(url, **kwargs):
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


def test_the_deprecated_downloads_stub_is_not_stored(config):
    # - The API answers -1 for every window. Storing that would be worse than
    #   storing nothing.
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(PACKAGE)])
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


def test_a_removed_package_is_skipped_not_fatal(config):
    from pipeline.extractors.pypi import PACKAGES

    extractor = PyPiExtractor(config)
    # - First package 404s, the rest answer normally.
    stub(extractor, [FakeResponse(status_code=404)])

    rows = extractor.fetch()
    assert len(rows) == len(PACKAGES) - 1
    assert rows[0]["name"] == "requests"


def test_a_server_error_is_an_extract_error(config):
    extractor = PyPiExtractor(config)
    stub(extractor, [FakeResponse(status_code=500)])
    with pytest.raises(ExtractError, match="returned 500"):
        extractor.fetch()


def test_the_whole_seed_list_is_fetched_regardless_of_batch_size(config, monkeypatch):
    # - Slicing by batch_size returned whichever packages happened to be listed
    #   first, which is not a meaningful subset of anything. There is no ranking
    #   here to slice by, so the list is the list.
    from pipeline.extractors.pypi import PACKAGES

    monkeypatch.setenv("BATCH_SIZE", "3")
    extractor = PyPiExtractor(Config())
    calls = stub(extractor, [])
    extractor.fetch()
    assert len(calls) == len(PACKAGES)


def test_the_configured_registry_is_used(config, monkeypatch):
    monkeypatch.setenv("PYPI_REGISTRY", "https://pypi.example.com")
    extractor = PyPiExtractor(Config())
    calls = stub(extractor, [FakeResponse(PACKAGE)])
    extractor.fetch()
    assert calls[0].startswith("https://pypi.example.com/pypi/")
