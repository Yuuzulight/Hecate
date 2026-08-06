"""Hacker News: URL canonicalisation and mention mapping."""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.extractors.hackernews import HackerNewsExtractor, canonical_repo_url
from tests.test_extractors import FakeResponse

STORY = {
    "objectID": "38294011",
    "title": "Show HN: axum, a web framework",
    "url": "https://github.com/tokio-rs/axum",
    "points": 240,
    "num_comments": 31,
    "author": "someone",
    "created_at": "2026-08-01T09:00:00.000Z",
}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("BATCH_SIZE", "50")
    return Config()


def stub(extractor, response):
    extractor.session.get = lambda url, **kwargs: response


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/tokio-rs/axum", "https://github.com/tokio-rs/axum"),
        ("https://github.com/Tokio-RS/Axum", "https://github.com/tokio-rs/axum"),
        ("https://github.com/tokio-rs/axum/", "https://github.com/tokio-rs/axum"),
        ("https://github.com/tokio-rs/axum/tree/main", "https://github.com/tokio-rs/axum"),
        ("https://github.com/tokio-rs/axum/issues/42", "https://github.com/tokio-rs/axum"),
        ("https://github.com/tokio-rs/axum.git", "https://github.com/tokio-rs/axum"),
        ("https://www.github.com/tokio-rs/axum", "https://github.com/tokio-rs/axum"),
        ("https://gitlab.com/group/project", "https://gitlab.com/group/project"),
    ],
)
def test_urls_reduce_to_the_project_they_belong_to(url, expected):
    assert canonical_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "https://example.com/tokio-rs/axum",
        "https://github.com",
        "https://github.com/tokio-rs",
        "not a url at all",
        "https://news.ycombinator.com/item?id=1",
    ],
)
def test_anything_that_is_not_a_project_page_resolves_to_nothing(url):
    assert canonical_repo_url(url) is None


def test_a_story_becomes_a_mention(config):
    extractor = HackerNewsExtractor(config)
    stub(extractor, FakeResponse({"hits": [STORY]}))

    (mention,) = extractor.fetch()
    assert mention["id"] == "hackernews_38294011"
    assert mention["platform"] == "hackernews"
    assert mention["target_url"] == "https://github.com/tokio-rs/axum"
    assert mention["url"] == "https://news.ycombinator.com/item?id=38294011"
    assert mention["score"] == 240
    assert mention["comments"] == 31
    assert mention["posted_at"] == "2026-08-01T09:00:00.000Z"
    # - Filled in later, once the URL is matched against what is stored.
    assert mention["repository_id"] is None


def test_hacker_news_has_no_channel(config):
    extractor = HackerNewsExtractor(config)
    stub(extractor, FakeResponse({"hits": [STORY]}))
    assert extractor.fetch()[0]["channel"] is None


def test_a_link_in_the_body_counts_when_the_story_links_elsewhere(config):
    extractor = HackerNewsExtractor(config)
    discussion = dict(
        STORY,
        url=None,
        story_text="worth a look: https://github.com/tokio-rs/axum for the API",
    )
    stub(extractor, FakeResponse({"hits": [discussion]}))
    assert extractor.fetch()[0]["target_url"] == "https://github.com/tokio-rs/axum"


def test_a_story_linking_nothing_recognisable_is_skipped(config):
    extractor = HackerNewsExtractor(config)
    stub(extractor, FakeResponse({"hits": [dict(STORY, url="https://example.com/blog")]}))
    assert extractor.fetch() == []


def test_a_story_with_no_id_is_skipped(config):
    extractor = HackerNewsExtractor(config)
    stub(extractor, FakeResponse({"hits": [{k: v for k, v in STORY.items() if k != "objectID"}]}))
    assert extractor.fetch() == []


def test_only_stories_are_requested(config):
    extractor = HackerNewsExtractor(config)
    seen = {}
    extractor.session.get = lambda url, **kwargs: seen.update(kwargs) or FakeResponse({"hits": []})
    extractor.fetch()
    assert seen["params"]["tags"] == "story"


def test_no_credentials_are_sent(config):
    # - The API needs none, so unlike Reddit this source can never be skipped
    #   for missing auth.
    extractor = HackerNewsExtractor(config)
    seen = {}
    extractor.session.get = lambda url, **kwargs: seen.update(kwargs) or FakeResponse({"hits": []})
    extractor.fetch()
    assert "headers" not in seen or "Authorization" not in (seen.get("headers") or {})


def test_a_failed_search_is_an_extract_error(config):
    extractor = HackerNewsExtractor(config)
    stub(extractor, FakeResponse(status_code=503))
    with pytest.raises(ExtractError, match="hackernews: search"):
        extractor.fetch()


def test_an_empty_result_is_not_a_failure(config):
    extractor = HackerNewsExtractor(config)
    stub(extractor, FakeResponse({"hits": []}))
    assert extractor.fetch() == []
