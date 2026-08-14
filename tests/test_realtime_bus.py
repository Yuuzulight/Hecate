"""The always-on event bus: publish, consumer-group read, ack, and the
tracked-npm-packages set the npm listener filters against.

A dead bus must never raise into a caller - the daily batch and the live
listeners both have to keep working (in whatever degraded way makes sense)
when Redis is unreachable, same principle as pipeline/rag/cache.py.
"""

import json

import pytest
import redis

from pipeline.realtime.bus import TRACKED_NPM_KEY, EventBus


class FakeRedis:
    """Enough of a Redis Streams client to exercise the bus, or fail on demand."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        self.groups: dict[tuple[str, str], set[str]] = {}  # (stream, group) -> acked ids
        self.pending: dict[tuple[str, str], list[str]] = {}  # (stream, group) -> unacked ids
        self.sets: dict[str, set[str]] = {}
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._seq}-0"

    def xgroup_create(self, stream, group, id="0", mkstream=False):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        if (stream, group) in self.groups:
            raise redis.ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups[(stream, group)] = set()
        self.pending[(stream, group)] = []
        self.streams.setdefault(stream, [])

    def xadd(self, stream, fields):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        entry_id = self._next_id()
        self.streams.setdefault(stream, []).append((entry_id, fields))
        return entry_id

    def xreadgroup(self, group, consumer, streams, count=None):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        (stream, marker), = streams.items()
        acked = self.groups.get((stream, group), set())
        all_entries = self.streams.get(stream, [])
        if marker == ">":
            result = [
                (entry_id, fields) for entry_id, fields in all_entries
                if entry_id not in acked and entry_id not in self.pending[(stream, group)]
            ]
            self.pending[(stream, group)].extend(entry_id for entry_id, _ in result)
        else:
            pending_ids = self.pending.get((stream, group), [])
            result = [(eid, f) for eid, f in all_entries if eid in pending_ids]
        if count is not None:
            result = result[:count]
        return [(stream, result)] if result else []

    def xack(self, stream, group, entry_id):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        self.groups[(stream, group)].add(entry_id)
        if entry_id in self.pending.get((stream, group), []):
            self.pending[(stream, group)].remove(entry_id)

    def delete(self, key):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        self.sets.pop(key, None)

    def sadd(self, key, *values):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        self.sets.setdefault(key, set()).update(values)

    def sismember(self, key, value):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        return value in self.sets.get(key, set())

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    """Enough of a Redis pipeline to exercise replace_tracked_npm."""

    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.calls = []

    def delete(self, key):
        self.calls.append(("delete", key))
        return self

    def sadd(self, key, *values):
        self.calls.append(("sadd", key, values))
        return self

    def execute(self):
        if self.client.fail:
            raise redis.ConnectionError("no route to host")
        for call in self.calls:
            if call[0] == "delete":
                self.client.delete(call[1])
            elif call[0] == "sadd":
                self.client.sadd(call[1], *call[2])


@pytest.fixture
def bus():
    b = EventBus(url="")
    b.client = FakeRedis()
    return b


def test_no_url_means_no_bus_and_no_error():
    b = EventBus(url="")
    b.publish("s", {"a": 1})  # must not raise
    assert b.read_pending_then_new("s", "g", "c") == []


def test_a_published_event_comes_back_through_read(bus):
    bus.ensure_group("s", "g")
    bus.publish("s", {"kind": "npm-publish", "package": "left-pad"})
    entries = bus.read_pending_then_new("s", "g", "c1")
    assert len(entries) == 1
    entry_id, event = entries[0]
    assert event == {"kind": "npm-publish", "package": "left-pad"}


def test_an_acked_entry_is_not_read_again(bus):
    bus.ensure_group("s", "g")
    bus.publish("s", {"a": 1})
    [(entry_id, _)] = bus.read_pending_then_new("s", "g", "c1")
    bus.ack("s", "g", entry_id)
    assert bus.read_pending_then_new("s", "g", "c1") == []


def test_an_unacked_entry_is_retried_as_pending(bus):
    # - The crash-recovery case: a consumer reads an entry, dies before
    #   acking it, and a new run of the same consumer name must see it again
    #   rather than lose it.
    bus.ensure_group("s", "g")
    bus.publish("s", {"a": 1})
    first_read = bus.read_pending_then_new("s", "g", "c1")
    assert len(first_read) == 1
    second_read = bus.read_pending_then_new("s", "g", "c1")
    assert len(second_read) == 1
    assert second_read[0][0] == first_read[0][0]


def test_ensure_group_is_safe_to_call_twice(bus):
    bus.ensure_group("s", "g")
    bus.ensure_group("s", "g")  # must not raise on BUSYGROUP


def test_a_dead_bus_reads_as_empty_not_an_error(bus):
    bus.client = FakeRedis(fail=True)
    assert bus.read_pending_then_new("s", "g", "c1") == []


def test_a_dead_bus_does_not_break_publish(bus):
    bus.client = FakeRedis(fail=True)
    bus.publish("s", {"a": 1})  # must not raise


def test_a_dead_bus_does_not_break_ack(bus):
    bus.client = FakeRedis(fail=True)
    bus.ack("s", "g", "1-0")  # must not raise


def test_tracked_npm_starts_empty(bus):
    assert bus.is_tracked_npm("left-pad") is False


def test_tracked_npm_after_replace(bus):
    bus.replace_tracked_npm({"left-pad", "react"})
    assert bus.is_tracked_npm("left-pad") is True
    assert bus.is_tracked_npm("react") is True
    assert bus.is_tracked_npm("something-else") is False


def test_replace_tracked_npm_clears_what_was_there_before(bus):
    bus.replace_tracked_npm({"left-pad"})
    bus.replace_tracked_npm({"react"})
    assert bus.is_tracked_npm("left-pad") is False
    assert bus.is_tracked_npm("react") is True


def test_replace_tracked_npm_with_an_empty_set_clears_everything(bus):
    bus.replace_tracked_npm({"left-pad"})
    bus.replace_tracked_npm(set())
    assert bus.is_tracked_npm("left-pad") is False


def test_a_dead_bus_reads_tracked_npm_as_false_not_an_error(bus):
    bus.client = FakeRedis(fail=True)
    assert bus.is_tracked_npm("left-pad") is False


def test_the_tracked_npm_key_is_a_module_constant():
    # - So the daily batch (Task 7) and the listener (Task 4) are provably
    #   reading and writing the same key, not two strings that happen to
    #   currently match.
    assert TRACKED_NPM_KEY == "hecate:tracked:npm"
