"""The HN listener: mapping a raw Firebase item to a mention, and deciding
whether it's worth publishing at all.

Firebase's item shape is not the same as the Algolia search-hit shape
HackerNewsExtractor already maps (unix epoch time vs. an ISO string,
`descendants` vs. `num_comments`, `by` vs. `author`) - so this is a new
mapping, not a reuse of the batch extractor's, even though both eventually
produce the same social_mentions shape. canonical_repo_url is reused as-is,
since it is a pure function of a URL and does not care where the URL came
from.
"""

import json

import pytest

from pipeline.realtime.bus import EventBus
from pipeline.realtime.hn_listener import HN_STREAM, handle_item, mention_from_item


def test_a_story_linking_to_a_resolvable_repo_maps_to_a_mention():
    item = {
        "id": 40000000,
        "type": "story",
        "title": "Show HN: a thing",
        "url": "https://github.com/owner/repo",
        "by": "someuser",
        "score": 42,
        "descendants": 7,
        "time": 1700000000,
    }
    mention = mention_from_item(item, "https://github.com/owner/repo")
    assert mention["id"] == "hackernews_40000000"
    assert mention["platform"] == "hackernews"
    assert mention["repository_id"] is None
    assert mention["target_url"] == "https://github.com/owner/repo"
    assert mention["title"] == "Show HN: a thing"
    assert mention["url"] == "https://news.ycombinator.com/item?id=40000000"
    assert mention["score"] == 42
    assert mention["comments"] == 7
    assert mention["author"] == "someuser"
    assert mention["channel"] is None
    # - Firebase's time is unix epoch seconds; social_mentions wants an
    #   ISO-8601 string parse_timestamp can read.
    from pipeline.transformer import parse_timestamp
    assert parse_timestamp(mention["posted_at"]) is not None


def test_a_story_with_no_url_produces_no_mention_via_handle_item():
    item = {"id": 1, "type": "story", "title": "Ask HN: something", "by": "u", "score": 1, "time": 1700000000}
    bus = EventBus(url="")
    bus.client = FakeRedisForPublishing()
    assert handle_item(bus, item) is False
    assert bus.client.published == []


def test_a_story_linking_somewhere_unresolvable_produces_no_mention():
    item = {
        "id": 2, "type": "story", "title": "x", "url": "https://example.com/blog/post",
        "by": "u", "score": 1, "descendants": 0, "time": 1700000000,
    }
    bus = EventBus(url="")
    bus.client = FakeRedisForPublishing()
    assert handle_item(bus, item) is False


def test_a_comment_is_never_published_even_with_a_resolvable_url_field():
    # - Firebase items include comments, jobs, polls - only stories are
    #   worth publishing here, matching HackerNewsExtractor's own scope
    #   (see its module docstring: it produces mentions from stories).
    item = {"id": 3, "type": "comment", "text": "check out github.com/owner/repo", "time": 1700000000}
    bus = EventBus(url="")
    bus.client = FakeRedisForPublishing()
    assert handle_item(bus, item) is False


def test_a_resolvable_story_is_published():
    item = {
        "id": 4, "type": "story", "title": "t", "url": "https://github.com/owner/repo",
        "by": "u", "score": 10, "descendants": 2, "time": 1700000000,
    }
    bus = EventBus(url="")
    bus.client = FakeRedisForPublishing()
    assert handle_item(bus, item) is True
    assert len(bus.client.published) == 1
    stream, fields = bus.client.published[0]
    assert stream == HN_STREAM
    published = json.loads(fields["data"])
    assert published["target_url"] == "https://github.com/owner/repo"


class FakeRedisForPublishing:
    def __init__(self):
        self.published = []

    def xadd(self, stream, fields):
        self.published.append((stream, fields))
        return "1-0"
