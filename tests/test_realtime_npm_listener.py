"""The npm listener: paging through the CouchDB _changes feed, and fetching
+ filtering it down to packages Hecate actually tracks before anything gets
published.

The feed itself is not unit-testable - it is a live network connection to
npm's real replication service - so this tests the pure/fakeable decisions
that sit around it: parsing one page of the _changes response, deciding
whether an entry names a real package, and whether a tracked package's
document gets fetched and published. The live connection itself needs a
manual check against the real feed, the same way this project has always
verified a live integration before calling it done - see this module's
sibling docstring for why the request shape looks the way it does (a poll,
not a held-open stream: `feed=continuous` and `include_docs=true` are both
rejected by the live service, confirmed directly against it).
"""

import json

import pytest

from pipeline.extractors.npm import registry_doc_to_row
from pipeline.realtime.bus import EventBus
from pipeline.realtime.npm_listener import (
    NPM_STREAM,
    changed_package_id,
    current_seq,
    handle_change,
    poll_changes,
)
from tests.test_extractors import FakeResponse


def test_current_seq_reads_the_db_root():
    session = FakeSession(FakeResponse({"db_name": "registry", "update_seq": 125049099}))
    assert current_seq(session) == 125049099
    assert session.calls[0][0] == "https://replicate.npmjs.com/"


def test_poll_changes_returns_results_and_the_next_since():
    body = {
        "results": [
            {"seq": 101, "id": "left-pad", "changes": [{"rev": "1-a"}]},
            {"seq": 103, "id": "chalk", "changes": [{"rev": "2-b"}]},
        ],
        "last_seq": 103,
    }
    session = FakeSession(FakeResponse(body))
    entries, since = poll_changes(session, 100)
    assert entries == body["results"]
    assert since == 103
    # - Never feed= or include_docs=: both are rejected by the live service
    #   regardless of value - the whole reason this listener polls instead
    #   of streaming.
    _, kwargs = session.calls[0]
    assert "feed" not in kwargs["params"]
    assert "include_docs" not in kwargs["params"]
    assert kwargs["params"]["since"] == 100


def test_poll_changes_with_nothing_new_returns_an_empty_page():
    session = FakeSession(FakeResponse({"results": [], "last_seq": 100}))
    entries, since = poll_changes(session, 100)
    assert entries == []
    assert since == 100


def test_a_normal_entry_names_its_package():
    entry = {"seq": 1, "id": "left-pad", "changes": [{"rev": "1-abc"}]}
    assert changed_package_id(entry) == "left-pad"


def test_a_deleted_entry_names_nothing():
    # - A deletion shows up in the feed with no live document behind it.
    #   Nothing to fetch or map, so it is skipped rather than raising on a
    #   404 from the registry downstream.
    entry = {"seq": 1, "id": "left-pad", "changes": [{"rev": "2-def"}], "deleted": True}
    assert changed_package_id(entry) is None


def test_a_malformed_entry_names_nothing():
    assert changed_package_id({"seq": 1, "changes": []}) is None
    assert changed_package_id("not a dict") is None


class FakeSession:
    """Enough of a requests.Session to exercise the listener's own HTTP
    calls without touching the network."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, {"params": params or {}, "timeout": timeout}))
        return self._response


@pytest.fixture
def bus():
    b = EventBus(url="")
    b.client = FakeRedisForTracking()
    return b


class FakeRedisForTracking:
    """Just enough to exercise is_tracked_npm and xadd for these tests."""

    def __init__(self):
        self.tracked = set()
        self.published = []

    def sismember(self, key, value):
        return value in self.tracked

    def xadd(self, stream, fields):
        self.published.append((stream, fields))
        return "1-0"


NPM_DOC = {"name": "left-pad", "description": "String left pad", "dist-tags": {}, "time": {}}


def test_a_tracked_packages_change_is_fetched_and_published(bus):
    bus.client.tracked.add("left-pad")
    session = FakeSession(FakeResponse(NPM_DOC))

    assert handle_change(bus, session, "https://registry.npmjs.org", "left-pad") is True
    assert session.calls[0][0] == "https://registry.npmjs.org/left-pad"
    assert len(bus.client.published) == 1
    stream, fields = bus.client.published[0]
    assert stream == NPM_STREAM
    published = json.loads(fields["data"])
    assert published["id"] == "npm_left-pad"
    assert published["name"] == "left-pad"


def test_an_untracked_packages_change_is_skipped_without_fetching(bus):
    # - bus.client.tracked is empty - nothing is tracked yet. The fake
    #   session has no response configured for a reason: fetching first
    #   would hit registry.npmjs.org for nearly every change on npm, most
    #   of which get discarded a line later - the tracked check must run
    #   before any fetch happens at all.
    session = NeverCalledSession()
    assert handle_change(bus, session, "https://registry.npmjs.org", "some-random-package") is False
    assert bus.client.published == []


class NeverCalledSession:
    def get(self, *args, **kwargs):
        raise AssertionError("handle_change fetched a document for an untracked package")


def test_a_dead_bus_does_not_crash_handle_change():
    b = EventBus(url="")
    b.client = None
    session = NeverCalledSession()
    # - is_tracked_npm reads as False when the bus is down, so this is
    #   already covered by "untracked is skipped" behaviorally, but this
    #   confirms it explicitly rather than by inference.
    assert handle_change(b, session, "https://registry.npmjs.org", "left-pad") is False


def test_a_failed_fetch_is_skipped_rather_than_raising(bus):
    bus.client.tracked.add("left-pad")
    session = FakeSession(FakeResponse(status_code=404))
    assert handle_change(bus, session, "https://registry.npmjs.org", "left-pad") is False
    assert bus.client.published == []


def test_the_tracked_set_and_the_changes_feed_agree_on_package_identity(bus):
    """Regression pin for the tracked-npm key format mismatch: pipeline/main.py
    seeds the tracked set from loader.rows_for("npm")'s `name` column (a row
    shaped by registry_doc_to_row), and this module's handle_change looks
    packages up by the CouchDB _changes feed's own bare `id`. Both sides
    independently agreed on "bare package name" once - this test exercises
    them together, through the real registry_doc_to_row and the real
    handle_change, so the two conventions cannot silently drift apart again
    the way they did when one side used the prefixed raw_repositories id
    instead.
    """
    row = registry_doc_to_row(NPM_DOC)

    # What pipeline/main.py's refresh step puts into the tracked set for
    # this package (see tests/test_integration.py's
    # test_run_refreshes_the_tracked_npm_set_after_npm_collection).
    bus.client.tracked.add(row["name"])

    # A real CouchDB _changes feed entry names this same package by its
    # own bare id - the exact id the fetch below is keyed on.
    package_id = changed_package_id({"seq": 1, "id": "left-pad", "changes": [{"rev": "1-a"}]})
    session = FakeSession(FakeResponse(NPM_DOC))

    assert handle_change(bus, session, "https://registry.npmjs.org", package_id) is True
