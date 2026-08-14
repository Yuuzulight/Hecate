"""Draining the real-time streams into Postgres, through the exact same
transformer/loader path batch rows already go through.

Uses stub transformer/loader (not a real database - that is covered by the
project's existing HECATE_INTEGRATION-gated tests, and this module adds no
new SQL of its own to test that way) to prove the wiring: every npm entry
becomes a transform_all + load_repositories call, every HN entry becomes a
resolve + load_mentions call, and every successfully-loaded entry gets
acked so it is not drained twice.
"""

import pytest

from pipeline.realtime.bus import EventBus
from pipeline.realtime.drain import drain
from pipeline.realtime.hn_listener import HN_STREAM, CONSUMER_GROUP as HN_GROUP
from pipeline.realtime.npm_listener import NPM_STREAM, CONSUMER_GROUP as NPM_GROUP


class StubTransformer:
    def __init__(self):
        self.calls = []

    def transform_all(self, records, source):
        self.calls.append((records, source))
        return records  # pass through unchanged - not testing normalization here


class StubLoader:
    def __init__(self):
        self.repositories_loaded = []
        self.mentions_loaded = []
        self.resolve_urls_calls = []

    def load_repositories(self, rows):
        self.repositories_loaded.extend(rows)
        return len(rows)

    def resolve_urls(self, urls):
        self.resolve_urls_calls.append(urls)
        return {}  # nothing resolves in these tests - resolve() still runs, just finds nothing

    def load_mentions(self, mentions):
        self.mentions_loaded.extend(mentions)
        return len(mentions)


@pytest.fixture
def bus():
    b = EventBus(url="")
    b.client = FakeRedisWithStreams()
    return b


class FakeRedisWithStreams:
    def __init__(self):
        self.groups = set()
        self.streams = {}
        self.acked = set()
        self._seq = 0

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        self.groups.add((stream, group))
        self.streams.setdefault(stream, [])

    def xreadgroup(self, group, consumer, streams, count=None):
        (stream, marker), = streams.items()
        entries = self.streams.get(stream, [])
        if marker == ">":
            result = [(eid, f) for eid, f in entries if eid not in self.acked]
        else:
            result = []  # no pending entries in these tests
        return [(stream, result)] if result else []

    def xack(self, stream, group, entry_id):
        self.acked.add(entry_id)

    def _seed(self, stream, event):
        import json
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.streams.setdefault(stream, []).append((entry_id, {"data": json.dumps(event)}))
        return entry_id


def test_drain_with_nothing_buffered_writes_nothing(bus):
    transformer = StubTransformer()
    loader = StubLoader()
    written = drain(bus, transformer, loader)
    assert written == 0
    assert loader.repositories_loaded == []
    assert loader.mentions_loaded == []


def test_a_buffered_npm_row_is_loaded_as_a_repository(bus):
    bus.ensure_group(NPM_STREAM, NPM_GROUP)
    npm_row = {"id": "npm_left-pad", "source": "npm", "name": "left-pad", "url": "https://www.npmjs.com/package/left-pad", "extracted_at": "2026-08-14T00:00:00+00:00"}
    bus.client._seed(NPM_STREAM, npm_row)

    transformer = StubTransformer()
    loader = StubLoader()
    written = drain(bus, transformer, loader)

    assert written == 1
    assert loader.repositories_loaded == [npm_row]
    assert transformer.calls == [([npm_row], "npm")]


def test_a_buffered_hn_mention_is_loaded_through_resolve(bus):
    bus.ensure_group(HN_STREAM, HN_GROUP)
    mention = {
        "id": "hackernews_1", "platform": "hackernews", "repository_id": None,
        "target_url": "https://github.com/owner/repo", "title": "t",
        "url": "https://news.ycombinator.com/item?id=1", "score": 1, "comments": 0,
        "author": "u", "channel": None, "posted_at": "2026-08-14T00:00:00+00:00",
        "extracted_at": "2026-08-14T00:00:00+00:00",
    }
    bus.client._seed(HN_STREAM, mention)

    transformer = StubTransformer()
    loader = StubLoader()
    written = drain(bus, transformer, loader)

    assert written == 1
    assert len(loader.mentions_loaded) == 1
    assert loader.mentions_loaded[0]["id"] == "hackernews_1"
    # - resolve() was actually called, not bypassed - confirmed by the loader
    #   stub recording the call.
    assert loader.resolve_urls_calls == [{"https://github.com/owner/repo"}]


def test_drained_entries_are_acked_so_they_are_not_processed_twice(bus):
    bus.ensure_group(NPM_STREAM, NPM_GROUP)
    npm_row = {"id": "npm_x", "source": "npm", "name": "x", "url": "u", "extracted_at": "2026-08-14T00:00:00+00:00"}
    bus.client._seed(NPM_STREAM, npm_row)

    drain(bus, StubTransformer(), StubLoader())
    # - A second drain call must see nothing new, because the first call's
    #   entry was acked.
    written_again = drain(bus, StubTransformer(), StubLoader())
    assert written_again == 0


def test_both_streams_are_drained_in_one_call(bus):
    bus.ensure_group(NPM_STREAM, NPM_GROUP)
    bus.ensure_group(HN_STREAM, HN_GROUP)
    bus.client._seed(NPM_STREAM, {"id": "npm_x", "source": "npm", "name": "x", "url": "u", "extracted_at": "2026-08-14T00:00:00+00:00"})
    bus.client._seed(HN_STREAM, {
        "id": "hackernews_2", "platform": "hackernews", "repository_id": None,
        "target_url": "https://github.com/o/r", "title": "t", "url": "https://news.ycombinator.com/item?id=2",
        "score": 1, "comments": 0, "author": "u", "channel": None,
        "posted_at": "2026-08-14T00:00:00+00:00", "extracted_at": "2026-08-14T00:00:00+00:00",
    })

    written = drain(bus, StubTransformer(), StubLoader())
    assert written == 2
