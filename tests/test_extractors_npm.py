"""npm extractor: schema mapping, ranking across keywords, and failures."""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.extractors.npm import KEYWORDS, NpmExtractor
from tests.test_extractors import FakeResponse


def package(name, weekly=1000, **overrides):
    payload = {
        "package": {
            "name": name,
            "description": f"the {name} package",
            "date": "2026-07-09T08:49:47.215Z",
            "links": {"npm": f"https://www.npmjs.com/package/{name}"},
        },
        "downloads": {"weekly": weekly, "monthly": weekly * 4},
    }
    payload["package"].update(overrides)
    return payload


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("BATCH_SIZE", "3")
    return Config()


def stub(extractor, per_call):
    """Answer every keyword search with the same list of objects."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"objects": per_call})

    extractor.session.get = fake_get
    return calls


def test_maps_a_package_to_the_standard_schema(config):
    extractor = NpmExtractor(config)
    stub(extractor, [package("tslib", weekly=422616954)])

    (row,) = extractor.fetch()
    assert row["id"] == "npm_tslib"
    assert row["source"] == "npm"
    assert row["name"] == "tslib"
    assert row["url"] == "https://www.npmjs.com/package/tslib"
    assert row["downloads"] == 422616954
    assert row["updated_at"] == "2026-07-09T08:49:47.215Z"


def test_stars_and_forks_are_zero_for_a_package(config):
    extractor = NpmExtractor(config)
    stub(extractor, [package("tslib")])
    (row,) = extractor.fetch()
    assert row["stars"] == 0
    assert row["forks"] == 0


def test_language_is_not_guessed(config):
    # - Search results don't say, and defaulting to JavaScript would be made up.
    extractor = NpmExtractor(config)
    stub(extractor, [package("tslib")])
    assert extractor.fetch()[0]["language"] is None


def test_created_at_is_left_empty(config):
    # - npm's `date` is the last publish, not the first.
    extractor = NpmExtractor(config)
    stub(extractor, [package("tslib")])
    assert extractor.fetch()[0]["created_at"] is None


def test_scoped_package_names_survive(config):
    extractor = NpmExtractor(config)
    stub(extractor, [package("@babel/parser")])
    (row,) = extractor.fetch()
    assert row["id"] == "npm_@babel/parser"
    assert row["name"] == "@babel/parser"


def test_a_package_without_links_falls_back_to_the_package_page(config):
    extractor = NpmExtractor(config)
    stub(extractor, [package("lonely", links=None)])
    assert extractor.fetch()[0]["url"] == "https://www.npmjs.com/package/lonely"


def test_a_package_without_download_figures_is_not_dropped(config):
    extractor = NpmExtractor(config)
    raw = package("quiet")
    del raw["downloads"]
    stub(extractor, [raw])
    assert extractor.fetch()[0]["downloads"] is None


def test_the_same_package_across_keywords_appears_once(config):
    extractor = NpmExtractor(config)
    stub(extractor, [package("tslib")])
    rows = extractor.fetch()
    assert len(rows) == 1


def test_every_keyword_is_searched(config):
    extractor = NpmExtractor(config)
    calls = stub(extractor, [package("tslib")])
    extractor.fetch()
    assert len(calls) == len(KEYWORDS)
    searched = {call[1]["params"]["text"] for call in calls}
    assert searched == {f"keywords:{word}" for word in KEYWORDS}


def test_results_are_ranked_by_downloads_across_keywords(config):
    extractor = NpmExtractor(config)
    stub(extractor, [package("small", 10), package("huge", 9000), package("mid", 500)])

    rows = extractor.fetch()
    assert [row["name"] for row in rows] == ["huge", "mid", "small"]


def test_the_batch_size_caps_the_result(config, monkeypatch):
    monkeypatch.setenv("BATCH_SIZE", "2")
    extractor = NpmExtractor(Config())
    stub(extractor, [package(f"pkg{i}", weekly=i) for i in range(10)])

    rows = extractor.fetch()
    assert len(rows) == 2
    assert rows[0]["name"] == "pkg9"


def test_packages_without_downloads_sort_last_rather_than_crashing(config):
    extractor = NpmExtractor(config)
    quiet = package("quiet")
    del quiet["downloads"]
    stub(extractor, [quiet, package("loud", 500)])

    rows = extractor.fetch()
    assert rows[0]["name"] == "loud"


def test_a_failed_search_is_an_extract_error(config):
    extractor = NpmExtractor(config)
    extractor.session.get = lambda url, **kwargs: FakeResponse(status_code=503)
    with pytest.raises(ExtractError, match="npm: search"):
        extractor.fetch()


def test_the_configured_registry_is_used(config, monkeypatch):
    monkeypatch.setenv("NPM_REGISTRY", "https://registry.example.com")
    extractor = NpmExtractor(Config())
    calls = stub(extractor, [package("tslib")])
    extractor.fetch()
    assert calls[0][0].startswith("https://registry.example.com/-/v1/search")
