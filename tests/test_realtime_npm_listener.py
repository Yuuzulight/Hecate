"""The npm listener: parsing the CouchDB feed, and filtering it down to
packages Hecate actually tracks before anything gets published.

The feed itself is not unit-testable - it is a live network connection to
npm's real replication service - so this tests the two pure decisions that
sit around it: can a line be parsed, and does a parsed change belong on the
bus. The live connection itself is Step 6, a manual check against the real
feed, the same way this project has always verified a live integration
before calling it done.
"""

import json

import pytest

from pipeline.extractors.npm import registry_doc_to_row
from pipeline.realtime.bus import EventBus
from pipeline.realtime.npm_listener import NPM_STREAM, handle_change, parse_change_line


def test_a_normal_change_line_parses():
    line = json.dumps({
        "seq": 12345,
        "id": "left-pad",
        "changes": [{"rev": "1-abc"}],
        "doc": {"name": "left-pad", "description": "String left pad"},
    })
    change = parse_change_line(line)
    assert change["id"] == "left-pad"
    assert change["doc"]["name"] == "left-pad"


def test_a_blank_heartbeat_line_is_not_a_change():
    # - CouchDB's continuous feed sends empty lines as heartbeats to keep the
    #   connection alive. Not an error, just nothing to process.
    assert parse_change_line("") is None
    assert parse_change_line("\n") is None


def test_an_unparseable_line_is_not_a_change_rather_than_a_crash():
    assert parse_change_line("not json at all") is None


def test_a_change_with_no_doc_is_not_a_change():
    # - A deletion shows up in the feed with no doc body. Nothing to map, so
    #   it is skipped rather than raising on a missing field downstream.
    line = json.dumps({"seq": 1, "id": "left-pad", "changes": [{"rev": "2-def"}], "deleted": True})
    assert parse_change_line(line) is None


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


def test_a_tracked_packages_change_is_published(bus):
    bus.client.tracked.add("left-pad")
    change = {
        "id": "left-pad",
        "doc": {"name": "left-pad", "description": "String left pad", "dist-tags": {}, "time": {}},
    }
    assert handle_change(bus, change) is True
    assert len(bus.client.published) == 1
    stream, fields = bus.client.published[0]
    assert stream == NPM_STREAM
    published = json.loads(fields["data"])
    assert published["id"] == "npm_left-pad"
    assert published["name"] == "left-pad"


def test_an_untracked_packages_change_is_skipped(bus):
    # - bus.client.tracked is empty - nothing is tracked yet.
    change = {
        "id": "some-random-package-nobody-tracks",
        "doc": {"name": "some-random-package-nobody-tracks", "dist-tags": {}, "time": {}},
    }
    assert handle_change(bus, change) is False
    assert bus.client.published == []


def test_a_dead_bus_does_not_crash_handle_change():
    b = EventBus(url="")
    b.client = None
    change = {"id": "left-pad", "doc": {"name": "left-pad", "dist-tags": {}, "time": {}}}
    # - is_tracked_npm reads as False when the bus is down, so this is
    #   already covered by "untracked is skipped" behaviorally, but this
    #   confirms it explicitly rather than by inference.
    assert handle_change(b, change) is False


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
    doc = {"name": "left-pad", "description": "String left pad", "dist-tags": {}, "time": {}}
    row = registry_doc_to_row(doc)

    # What pipeline/main.py's refresh step puts into the tracked set for
    # this package (see tests/test_integration.py's
    # test_run_refreshes_the_tracked_npm_set_after_npm_collection).
    bus.client.tracked.add(row["name"])

    # A real CouchDB _changes feed entry for this same package.
    change = {"id": "left-pad", "doc": doc}

    assert handle_change(bus, change) is True
