"""Lobsters: mapping its feed onto the shared mention shape."""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.extractors.lobsters import LobstersExtractor
from tests.test_extractors import FakeResponse

STORY = {
    "short_id": "abc123",
    "short_id_url": "https://lobste.rs/s/abc123",
    "title": "axum 0.9 released",
    "url": "https://github.com/tokio-rs/axum",
    "score": 42,
    "comment_count": 7,
    "submitter_user": {"username": "someone"},
    "tags": ["rust", "web"],
    "created_at": "2026-08-06T09:00:00.000-06:00",
}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("BATCH_SIZE", "25")
    return Config()


def stub(extractor, response):
    extractor.session.get = lambda url, **kwargs: response


def test_a_story_becomes_a_mention(config):
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse([STORY]))

    (mention,) = extractor.fetch()
    assert mention["id"] == "lobsters_abc123"
    assert mention["platform"] == "lobsters"
    assert mention["target_url"] == "https://github.com/tokio-rs/axum"
    assert mention["url"] == "https://lobste.rs/s/abc123"
    assert mention["score"] == 42
    assert mention["comments"] == 7
    assert mention["author"] == "someone"
    assert mention["repository_id"] is None


def test_tags_become_the_channel(config):
    # - The closest thing Lobsters has to a subreddit.
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse([STORY]))
    assert extractor.fetch()[0]["channel"] == "rust,web"


def test_a_story_with_no_tags_has_no_channel(config):
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse([dict(STORY, tags=[])]))
    assert extractor.fetch()[0]["channel"] is None


def test_a_submitter_given_as_a_plain_string_still_works(config):
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse([dict(STORY, submitter_user="someone")]))
    assert extractor.fetch()[0]["author"] == "someone"


def test_a_link_in_the_description_counts(config):
    extractor = LobstersExtractor(config)
    text_post = dict(
        STORY,
        url="https://lobste.rs/s/abc123",
        description="see https://github.com/tokio-rs/axum for the API",
    )
    stub(extractor, FakeResponse([text_post]))
    assert extractor.fetch()[0]["target_url"] == "https://github.com/tokio-rs/axum"


def test_a_story_linking_nothing_recognisable_is_skipped(config):
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse([dict(STORY, url="https://example.com/post")]))
    assert extractor.fetch() == []


def test_batch_size_caps_the_feed(config, monkeypatch):
    monkeypatch.setenv("BATCH_SIZE", "2")
    extractor = LobstersExtractor(Config())
    stub(extractor, FakeResponse([dict(STORY, short_id=f"s{i}") for i in range(10)]))
    assert len(extractor.fetch()) == 2


def test_a_failed_feed_is_an_extract_error(config):
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse(status_code=503))
    with pytest.raises(ExtractError, match="lobsters: feed"):
        extractor.fetch()


def test_an_unexpected_shape_is_reported_clearly(config):
    # - Same guard as GitLab: a dict would otherwise be iterated as field names.
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse({"error": "nope"}))
    with pytest.raises(ExtractError, match="expected a list of stories"):
        extractor.fetch()


def test_an_empty_feed_is_not_a_failure(config):
    extractor = LobstersExtractor(config)
    stub(extractor, FakeResponse([]))
    assert extractor.fetch() == []
