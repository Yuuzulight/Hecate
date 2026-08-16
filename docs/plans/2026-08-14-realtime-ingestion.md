# Phase 3: Real-Time Ingestion Implementation Plan

**Goal:** Capture npm publishes and Hacker News posts within seconds of happening, via two small always-on listener processes writing into a Redis event bus, drained into the existing warehouse once a day and pushed live to anyone connected to a new WebSocket endpoint.

**Architecture:** Two standalone Python processes (`pipeline/realtime/npm_listener.py`, `pipeline/realtime/hn_listener.py`) run continuously, independent of the windowed K8s cluster, publishing matched events to Redis Streams on a second, always-on Redis instance. The daily batch (`pipeline/main.py`) gains a drain step that reads those streams through the existing `RepositoryTransformer`/`PostgreSQLLoader` path - no new schema, no new tables. The existing RAG API (`pipeline/rag/api.py`) gains a `WS /live` endpoint that reads the same streams directly, independent of Postgres.

**Tech Stack:** `requests` (already a dependency, used for both the CouchDB continuous feed and Firebase polling), `redis` (already a dependency, `redis==8.1.0`), FastAPI's native WebSocket support (no new dependency), Memurai (Windows-native Redis-compatible server, for the always-on instance), NSSM (already used successfully for a similar always-on-Windows-service need in another project, per the user).

**Spec:** `docs/specs/2026-08-14-realtime-ingestion-design.md`

## Global Constraints

- Real-time ingestion covers only npm and Hacker News. GitHub, GitLab, and PyPI stay on the existing daily batch - they structurally cannot push (see the spec's source-by-source table) and this plan does not pretend otherwise.
- The always-on Redis instance is **separate** from the Redis the RAG service uses for context caching (`pipeline/rag/cache.py`). Caching is fine to lose; a buffered event is not. They must never share a URL or a database index.
- `windowed-run.ps1` stops Docker Desktop **entirely**, not just the K8s cluster - confirmed by reading the script. Anything that needs to survive that (the bus Redis, the two listeners) must run **outside Docker**, as native Windows processes/services.
- Every new Redis-touching function must degrade to a safe no-op (or empty result) when the bus is unreachable, and log a warning rather than raise - matching the existing pattern in `pipeline/rag/cache.py`'s `ContextCache`. A dead event bus must never take down the daily batch or the RAG API.
- No new tables. Real-time-captured rows go through the exact same `RepositoryTransformer`/`PostgreSQLLoader` path as batch-captured rows and land in `raw_repositories`/`social_mentions` indistinguishably.
- Memurai's free Developer edition has a 10-day maximum uptime before it requires a restart. Task 9 accounts for this with a weekly scheduled restart rather than treating it as a blocker - the bus is drained daily anyway, so a restart loses at most a few hours of unconsumed buffer, not history.

---

## Task 1: Config gains `REDIS_REALTIME_URL`

**Files:**
- Modify: `pipeline/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.redis_realtime_url: str` (optional, default `""`, same pattern as `Config.redis_url`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`, after the existing `REDIS_URL`-adjacent defaults test (find it by searching the file for `redis_url` - add immediately after whatever test covers it, in the same style):

```python
def test_redis_realtime_url_defaults_to_blank(env):
    assert Config().redis_realtime_url == ""


def test_redis_realtime_url_is_read_when_set(env):
    env.setenv("REDIS_REALTIME_URL", "redis://localhost:6380/0")
    assert Config().redis_realtime_url == "redis://localhost:6380/0"
```

Also add `"REDIS_REALTIME_URL"` to the `ALL_VARS` list near the top of the file, alongside the other optional settings, so it's cleared between tests the same way the rest are.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v -k redis_realtime`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'redis_realtime_url'`.

- [ ] **Step 3: Implement in `pipeline/config.py`**

Add immediately after the existing `self.redis_url = _optional("REDIS_URL")` line:

```python
        # - The always-on event bus real-time listeners publish into, and the
        #   daily batch drains from. Deliberately separate from redis_url
        #   above: that one is disposable context cache, this one is a buffer
        #   of real, not-yet-durable events. They must never point at the
        #   same instance. Optional for the same reason as redis_url - a
        #   deployment with no real-time ingestion configured is a working
        #   configuration, not a broken one; the drain step below no-ops
        #   cleanly when it's unset.
        self.redis_realtime_url = _optional("REDIS_REALTIME_URL")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: All PASS, including every pre-existing test.

- [ ] **Step 5: Commit**

```bash
git add pipeline/config.py tests/test_config.py
git commit -m "Add REDIS_REALTIME_URL to Config, for the real-time event bus"
```

---

## Task 2: `pipeline/realtime/bus.py` - the event bus

**Files:**
- Create: `pipeline/realtime/__init__.py`
- Create: `pipeline/realtime/bus.py`
- Create: `tests/test_realtime_bus.py`

**Interfaces:**
- Consumes: `Config.redis_realtime_url` (Task 1).
- Produces:
  - `EventBus(url: str)`
  - `EventBus.ensure_group(stream: str, group: str) -> None`
  - `EventBus.publish(stream: str, event: dict) -> None`
  - `EventBus.read_pending_then_new(stream: str, group: str, consumer: str, count: int = 500) -> list[tuple[str, dict]]`
  - `EventBus.ack(stream: str, group: str, entry_id: str) -> None`
  - `EventBus.replace_tracked_npm(package_ids: set[str]) -> None`
  - `EventBus.is_tracked_npm(package_id: str) -> bool`
  - `TRACKED_NPM_KEY: str` module constant

- [ ] **Step 1: Create the package and write the failing tests**

Create `pipeline/realtime/__init__.py` (empty file).

Create `tests/test_realtime_bus.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_realtime_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.realtime.bus'`.

- [ ] **Step 3: Write the implementation**

Create `pipeline/realtime/bus.py`:

```python
"""The always-on event bus: real-time listeners publish into it, the daily
batch drains from it, and the RAG API's WS /live endpoint reads it directly.

Deliberately separate from pipeline/rag/cache.py's Redis, both in code and
in the actual instance it points at (see Config.redis_realtime_url). That
one is a disposable cache - losing it costs a slower answer. This one is a
buffer of real, not-yet-durable events - losing it costs history that
cannot be recaptured, since the sources that fed it were watched live, not
re-queryable after the fact the way a batch source is.

Every method degrades to a safe no-op or empty result when the bus is
unreachable, and logs rather than raises - same principle as ContextCache.
A dead bus must not take down the daily batch, a listener, or the RAG API.
"""

import json

import redis

from pipeline.logger import get_logger

SOCKET_TIMEOUT_SECONDS = 2

# - The set the npm listener filters the full registry firehose against.
#   Refreshed once a day by the batch (Task 7), which is the only thing with
#   live Postgres access to know what is actually tracked - the listener
#   runs the rest of the day off this snapshot. A module constant, not a
#   string repeated in two files, so the writer and the reader are provably
#   using the same key.
TRACKED_NPM_KEY = "hecate:tracked:npm"


class EventBus:
    def __init__(self, url: str) -> None:
        self.log = get_logger("realtime.bus")
        self.client = None
        if url:
            self.client = redis.from_url(
                url,
                socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
                decode_responses=True,
            )

    def ensure_group(self, stream: str, group: str) -> None:
        """Create the consumer group if it doesn't exist. Safe to call every
        time a listener or the drain step starts - BUSYGROUP means it's
        already there, which is the expected case after the first run."""
        if self.client is None:
            return
        try:
            self.client.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        except redis.RedisError as exc:
            self.log.warning(
                "could not ensure consumer group",
                extra={"context": {"stream": stream, "group": group, "error": str(exc)}},
            )

    def publish(self, stream: str, event: dict) -> None:
        if self.client is None:
            return
        try:
            self.client.xadd(stream, {"data": json.dumps(event)})
        except redis.RedisError as exc:
            self.log.warning(
                "publish failed",
                extra={"context": {"stream": stream, "error": str(exc)}},
            )

    def read_pending_then_new(
        self, stream: str, group: str, consumer: str, count: int = 500
    ) -> list[tuple[str, dict]]:
        """This consumer's own unacknowledged entries from a previous run
        that crashed before acking them, then whatever is new. Pending first,
        so a partially-processed batch is retried before anything new is
        picked up - otherwise a crash mid-batch loses whatever hadn't been
        acked yet, silently."""
        if self.client is None:
            return []
        try:
            pending = self.client.xreadgroup(group, consumer, {stream: "0"}, count=count)
            new = self.client.xreadgroup(group, consumer, {stream: ">"}, count=count)
        except redis.RedisError as exc:
            self.log.warning(
                "read failed",
                extra={"context": {"stream": stream, "error": str(exc)}},
            )
            return []

        entries = []
        for _, items in list(pending) + list(new):
            for entry_id, fields in items:
                entries.append((entry_id, json.loads(fields["data"])))
        return entries

    def ack(self, stream: str, group: str, entry_id: str) -> None:
        if self.client is None:
            return
        try:
            self.client.xack(stream, group, entry_id)
        except redis.RedisError as exc:
            self.log.warning(
                "ack failed",
                extra={"context": {"stream": stream, "entry_id": entry_id, "error": str(exc)}},
            )

    def replace_tracked_npm(self, package_ids: set[str]) -> None:
        """Overwrite the tracked-npm-packages set wholesale. Called once a
        day by the batch - a full replace rather than an incremental update,
        so a package dropped from tracking stops matching the same day."""
        if self.client is None:
            return
        try:
            pipe = self.client.pipeline()
            pipe.delete(TRACKED_NPM_KEY)
            if package_ids:
                pipe.sadd(TRACKED_NPM_KEY, *package_ids)
            pipe.execute()
        except redis.RedisError as exc:
            self.log.warning(
                "could not refresh tracked npm packages",
                extra={"context": {"error": str(exc)}},
            )

    def is_tracked_npm(self, package_id: str) -> bool:
        if self.client is None:
            return False
        try:
            return bool(self.client.sismember(TRACKED_NPM_KEY, package_id))
        except redis.RedisError as exc:
            self.log.warning(
                "tracked-npm check failed",
                extra={"context": {"package": package_id, "error": str(exc)}},
            )
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_realtime_bus.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/realtime/__init__.py pipeline/realtime/bus.py tests/test_realtime_bus.py
git commit -m "Add the always-on event bus (Redis Streams, consumer groups)"
```

---

## Task 3: Extract the npm registry-document-to-row mapping, shared

**Why this task exists:** the npm listener (Task 4) receives full npm registry documents from the CouchDB feed - the same shape `NpmExtractor.fetch_by_url` already maps to a row, just arriving a different way. Without extracting it, the listener would grow a second, silently-drifting copy of that mapping.

**Files:**
- Modify: `pipeline/extractors/npm.py`
- Test: `tests/test_extractors_npm.py` (find the actual existing test file name by checking `tests/` for the npm extractor's tests - it may be named differently; use whatever file currently tests `NpmExtractor.fetch_by_url`)

**Interfaces:**
- Produces: `pipeline.extractors.npm.registry_doc_to_row(raw: dict, source: str = "npm") -> dict` - module-level function, the exact mapping `fetch_by_url` already does, extracted so a second caller (Task 4) can use it too.

- [ ] **Step 1: Find the existing test for `fetch_by_url` and read it**

Run: `grep -rn "fetch_by_url" tests/` to find which test file covers `NpmExtractor.fetch_by_url`. Read that test to see the exact registry-document shape it already constructs as a fixture - Task 4's tests reuse that same shape.

- [ ] **Step 2: Write the failing test**

Add to that same test file:

```python
def test_registry_doc_to_row_maps_the_same_way_fetch_by_url_does():
    from pipeline.extractors.npm import registry_doc_to_row

    raw = {
        "name": "left-pad",
        "description": "String left pad",
        "dist-tags": {"latest": "1.3.0"},
        "time": {"created": "2014-12-01T00:00:00.000Z", "modified": "2020-03-01T00:00:00.000Z"},
    }
    row = registry_doc_to_row(raw)
    assert row == {
        "id": "npm_left-pad",
        "source": "npm",
        "name": "left-pad",
        "url": "https://www.npmjs.com/package/left-pad",
        "stars": 0,
        "forks": 0,
        "language": None,
        "created_at": "2014-12-01T00:00:00.000Z",
        "updated_at": "2020-03-01T00:00:00.000Z",
        "description": "String left pad",
        "downloads": None,
        "extracted_at": row["extracted_at"],  # timestamp, asserted separately below
    }
    from pipeline.transformer import parse_timestamp
    assert parse_timestamp(row["extracted_at"]) is not None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_extractors_npm.py -v -k registry_doc_to_row`
Expected: FAIL with `ImportError: cannot import name 'registry_doc_to_row'`.

- [ ] **Step 4: Extract the function**

In `pipeline/extractors/npm.py`, add a new module-level function, and rewrite `fetch_by_url` to call it instead of duplicating the mapping:

```python
def registry_doc_to_row(raw: dict, source: str = "npm") -> dict:
    """Map one full npm registry document to a raw_repositories row.

    Shared between fetch_by_url (fetched by URL during discovery) and the
    real-time listener (received from the CouchDB _changes feed) - both
    hand this function the exact same document shape, npm's own per-package
    registry document, just by two different paths.
    """
    latest = (raw.get("dist-tags") or {}).get("latest")
    versions = raw.get("time") or {}
    name = raw.get("name")

    return {
        "id": f"{source}_{name}",
        "source": source,
        "name": name,
        "url": f"https://www.npmjs.com/package/{name}",
        "stars": 0,
        "forks": 0,
        "language": None,
        "created_at": versions.get("created"),
        "updated_at": versions.get("modified") or versions.get(latest),
        "description": raw.get("description"),
        # - Downloads live on a different endpoint. Left empty rather than
        #   guessed; the next scheduled search run fills it in.
        "downloads": None,
        "extracted_at": Extractor.now(),
    }
```

Add `from pipeline.extractors.base import Extractor` to the imports at the top of `npm.py` if `Extractor` isn't already imported by name there (check first - `TIMEOUT, Extractor` is already imported per the existing `from pipeline.extractors.base import TIMEOUT, Extractor` line, so this is likely already available).

Replace the body of `fetch_by_url` from the `return {` line onward with:

```python
        return registry_doc_to_row(raw)
```

(Leave everything above that line - the HTTP fetch, the 404 handling - unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_extractors_npm.py -v`
Expected: All PASS, including every pre-existing test for `fetch_by_url` (the extraction must not change its observable behavior at all).

- [ ] **Step 6: Run the full test suite once**

Run: `pytest -q`
Expected: All PASS. This refactor touches a function `discover()` in `pipeline/main.py` depends on indirectly - confirm nothing else broke.

- [ ] **Step 7: Commit**

```bash
git add pipeline/extractors/npm.py tests/test_extractors_npm.py
git commit -m "Extract registry_doc_to_row from fetch_by_url, for the real-time listener to share"
```

---

## Task 4: `pipeline/realtime/npm_listener.py`

**Files:**
- Create: `pipeline/realtime/npm_listener.py`
- Create: `tests/test_realtime_npm_listener.py`

**Interfaces:**
- Consumes: `pipeline.realtime.bus.EventBus` (Task 2), `pipeline.extractors.npm.registry_doc_to_row` (Task 3).
- Produces:
  - `NPM_STREAM: str` module constant (`"hecate:events:npm"`)
  - `parse_change_line(line: str) -> dict | None` - one line of the CouchDB feed to a change document, or None if it's a heartbeat/unparseable
  - `handle_change(bus: EventBus, change: dict) -> bool` - True if the change was published (tracked and mapped), False if skipped
  - `run(config: Config) -> None` - the listener's main loop (not unit-tested directly; Step 6 below is a manual live check instead)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_realtime_npm_listener.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_realtime_npm_listener.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.realtime.npm_listener'`.

- [ ] **Step 3: Write the implementation**

Create `pipeline/realtime/npm_listener.py`:

```python
"""Listens to npm's own real-time replication feed and publishes matches.

npm has no per-package webhook, but the whole registry is a CouchDB database,
and CouchDB's continuous _changes feed is a genuine, global, no-opt-in-needed
firehose of every publish to every package - confirmed against npm/registry's
own REPLICATE-API docs. Most of that firehose is irrelevant to Hecate (it
tracks a few thousand packages out of the whole registry), so every change is
checked against the tracked-npm-packages set (pipeline.realtime.bus.
EventBus.is_tracked_npm) before anything gets published - the set the daily
batch refreshes once a day, since only the batch has live Postgres access to
know what is actually tracked (see pipeline/main.py's refresh step).

Deliberately does not resume from the last-seen seq across restarts. A
restart misses whatever was published to a tracked package during the
listener's downtime - a real, known gap, not a silently assumed one. Worth
fixing once this has run long enough to know how often it matters; not
required for this phase (see the spec's "Deferred" section - this specific
gap is a natural first extension of it, not something this spec already
covers).
"""

import json
import time

import requests

from pipeline.config import Config
from pipeline.extractors.npm import registry_doc_to_row
from pipeline.logger import get_logger
from pipeline.realtime.bus import EventBus

FEED_URL = "https://replicate.npmjs.com/registry/_changes"
NPM_STREAM = "hecate:events:npm"
CONSUMER_GROUP = "npm-drain"

# - No read timeout: this is a deliberately long-lived streaming connection,
#   not a normal request. The connect timeout still applies, so a genuinely
#   unreachable host fails fast rather than hanging forever.
CONNECT_TIMEOUT = 10


def parse_change_line(line: str) -> dict | None:
    """One line of the CouchDB continuous feed, or None if there's nothing
    to process - a heartbeat, a malformed line, or a deletion with no doc."""
    text = line.strip() if isinstance(line, str) else ""
    if not text:
        return None
    try:
        change = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(change, dict) or not change.get("doc"):
        return None
    return change


def handle_change(bus: EventBus, change: dict) -> bool:
    """Publish one change if its package is tracked. Returns whether it was."""
    package_id = change.get("id")
    if not package_id or not bus.is_tracked_npm(package_id):
        return False
    row = registry_doc_to_row(change["doc"])
    bus.publish(NPM_STREAM, row)
    return True


def run(config: Config) -> None:
    """Connect once, then process the feed until the connection drops - the
    caller (Task 9's service wrapper) is responsible for restarting this on
    exit, the same way any long-lived service expects its supervisor to."""
    log = get_logger("realtime.npm_listener")
    if not config.redis_realtime_url:
        raise SystemExit("REDIS_REALTIME_URL is required to run the npm listener")

    bus = EventBus(config.redis_realtime_url)
    bus.ensure_group(NPM_STREAM, CONSUMER_GROUP)

    log.info("npm listener starting", extra={"context": {"feed": FEED_URL}})
    response = requests.get(
        FEED_URL,
        params={"feed": "continuous", "include_docs": "true", "since": "now"},
        stream=True,
        timeout=(CONNECT_TIMEOUT, None),
    )
    response.raise_for_status()

    published = 0
    for raw_line in response.iter_lines(decode_unicode=True):
        change = parse_change_line(raw_line)
        if change is None:
            continue
        if handle_change(bus, change):
            published += 1
            if published % 50 == 0:
                log.info("npm events published", extra={"context": {"total": published}})


if __name__ == "__main__":
    while True:
        try:
            run(Config())
        except Exception as exc:  # noqa: BLE001 - a long-lived listener must not die silently or permanently
            get_logger("realtime.npm_listener").exception(
                "npm listener crashed, restarting in 10s", extra={"context": {"error": str(exc)}}
            )
            time.sleep(10)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_realtime_npm_listener.py -v`
Expected: All PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 6: Manual live check - do this before considering the task done**

This cannot be meaningfully unit-tested - it needs npm's real feed. Run by hand:

```python
# scratch_npm_listener.py - delete after running
import os
os.environ["DB_PASSWORD"] = "x"
os.environ["REDIS_REALTIME_URL"] = "redis://localhost:6380/0"  # a real reachable Redis for this check
from pipeline.config import Config
from pipeline.realtime.bus import EventBus
from pipeline.realtime.npm_listener import NPM_STREAM, CONSUMER_GROUP

# Track a package that publishes often enough to see a real event within a
# few minutes - left-pad and lodash are both frequently-touched enough for
# this. Seed the tracked set by hand for this check only.
bus = EventBus(os.environ["REDIS_REALTIME_URL"])
bus.replace_tracked_npm({"left-pad", "lodash", "react", "chalk", "axios"})

from pipeline.realtime.npm_listener import run
run(Config())  # Ctrl+C once you see at least one "npm events published" log line
```

Run: `python scratch_npm_listener.py`, watch the logs, confirm at least one real event is published within a reasonable wait (a few minutes - these are actively-maintained packages). Delete `scratch_npm_listener.py` when done.

- [ ] **Step 7: Commit**

```bash
git add pipeline/realtime/npm_listener.py tests/test_realtime_npm_listener.py
git commit -m "Add the npm real-time listener"
```

---

## Task 5: `pipeline/realtime/hn_listener.py`

**Files:**
- Create: `pipeline/realtime/hn_listener.py`
- Create: `tests/test_realtime_hn_listener.py`

**Interfaces:**
- Consumes: `pipeline.extractors.hackernews.canonical_repo_url` (existing), `pipeline.realtime.bus.EventBus` (Task 2).
- Produces:
  - `HN_STREAM: str` module constant (`"hecate:events:hn"`)
  - `mention_from_item(item: dict, target_url: str) -> dict`
  - `handle_item(bus: EventBus, item: dict) -> bool`
  - `run(config: Config) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_realtime_hn_listener.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_realtime_hn_listener.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.realtime.hn_listener'`.

- [ ] **Step 3: Write the implementation**

Create `pipeline/realtime/hn_listener.py`:

```python
"""Listens to Hacker News's real-time updates feed and publishes stories
that link somewhere resolvable.

Firebase's own docs describe this API as built for live listening, and
/v0/updates.json is designed to be checked frequently - unlike PyPI, nothing
here asks callers not to. Polling every UPDATES_POLL_SECONDS rather than
holding a true SSE connection open: HN's total story volume is a few hundred
a day, so a short poll interval is indistinguishable from a push in practice,
and it means this listener needs nothing beyond `requests` - already a
dependency, already used by every extractor - rather than adding an SSE
client library for a marginal latency improvement nothing here needs.

Resolution is by link only, matching HackerNewsExtractor exactly: a story
that names a project in prose rather than linking it is left alone.
canonical_repo_url is the same function the batch extractor uses, reused
as-is rather than reimplemented, since it is a pure function of a URL string
and does not care whether that string came from Algolia or Firebase.
"""

import time

import requests

from pipeline.config import Config
from pipeline.extractors.hackernews import canonical_repo_url
from pipeline.logger import get_logger
from pipeline.realtime.bus import EventBus

UPDATES_URL = "https://hacker-news.firebaseio.com/v0/updates.json"
ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
HN_STREAM = "hecate:events:hn"
CONSUMER_GROUP = "hn-drain"

UPDATES_POLL_SECONDS = 15
REQUEST_TIMEOUT = 10


def mention_from_item(item: dict, target_url: str) -> dict:
    """One Firebase story item, already confirmed to resolve to target_url,
    mapped to the exact shape pipeline.loader.MENTION_COLUMNS expects."""
    from datetime import datetime, timezone

    posted_at = datetime.fromtimestamp(item["time"], tz=timezone.utc).isoformat()
    item_id = item["id"]

    return {
        "id": f"hackernews_{item_id}",
        "platform": "hackernews",
        "repository_id": None,
        "target_url": target_url,
        "title": item.get("title"),
        "url": f"https://news.ycombinator.com/item?id={item_id}",
        "score": item.get("score"),
        "comments": item.get("descendants"),
        "author": item.get("by"),
        "channel": None,
        "posted_at": posted_at,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def handle_item(bus: EventBus, item: dict) -> bool:
    """Publish one item if it's a story linking somewhere resolvable.
    Returns whether it was."""
    if item.get("type") != "story":
        return False
    target = canonical_repo_url(item.get("url") or "")
    if not target:
        return False
    mention = mention_from_item(item, target)
    bus.publish(HN_STREAM, mention)
    return True


def _fetch_item(session: requests.Session, item_id: int) -> dict | None:
    response = session.get(ITEM_URL.format(item_id=item_id), timeout=REQUEST_TIMEOUT)
    if not response.ok:
        return None
    return response.json()


def run(config: Config) -> None:
    """Poll for changed items forever - the caller (Task 9's service
    wrapper) restarts this on exit, same as the npm listener."""
    log = get_logger("realtime.hn_listener")
    if not config.redis_realtime_url:
        raise SystemExit("REDIS_REALTIME_URL is required to run the HN listener")

    bus = EventBus(config.redis_realtime_url)
    bus.ensure_group(HN_STREAM, CONSUMER_GROUP)
    session = requests.Session()

    log.info("HN listener starting", extra={"context": {"poll_seconds": UPDATES_POLL_SECONDS}})
    seen: set[int] = set()
    published = 0

    while True:
        response = session.get(UPDATES_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        changed_ids = response.json().get("items") or []

        for item_id in changed_ids:
            if item_id in seen:
                continue
            seen.add(item_id)
            item = _fetch_item(session, item_id)
            if item and handle_item(bus, item):
                published += 1
                if published % 20 == 0:
                    log.info("HN events published", extra={"context": {"total": published}})

        # - Bounded so a long-running process doesn't grow this set forever.
        #   Firebase item ids are monotonically increasing, so dropping the
        #   oldest half when it gets large never re-admits a real duplicate
        #   within any realistic session length.
        if len(seen) > 20_000:
            seen = set(sorted(seen)[-10_000:])

        time.sleep(UPDATES_POLL_SECONDS)


if __name__ == "__main__":
    while True:
        try:
            run(Config())
        except Exception as exc:  # noqa: BLE001 - a long-lived listener must not die silently or permanently
            get_logger("realtime.hn_listener").exception(
                "HN listener crashed, restarting in 10s", extra={"context": {"error": str(exc)}}
            )
            time.sleep(10)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_realtime_hn_listener.py -v`
Expected: All PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 6: Manual live check - do this before considering the task done**

```python
# scratch_hn_listener.py - delete after running
import os
os.environ["DB_PASSWORD"] = "x"
os.environ["REDIS_REALTIME_URL"] = "redis://localhost:6380/0"
from pipeline.config import Config
from pipeline.realtime.hn_listener import run
run(Config())  # Ctrl+C once you see at least one "HN events published" log line
```

Run: `python scratch_hn_listener.py`, watch the logs. HN gets enough GitHub-linking stories that a real event should publish within several minutes to an hour depending on the time of day - this is a real network-dependent wait, not a fixed number. Delete `scratch_hn_listener.py` when done.

- [ ] **Step 7: Commit**

```bash
git add pipeline/realtime/hn_listener.py tests/test_realtime_hn_listener.py
git commit -m "Add the Hacker News real-time listener"
```

---

## Task 6: `pipeline/realtime/drain.py` - the stream-to-Postgres consumer

**Files:**
- Create: `pipeline/realtime/drain.py`
- Create: `tests/test_realtime_drain.py`
- Modify: `pipeline/matching.py`, `pipeline/main.py`

**Interfaces:**
- Consumes: `pipeline.realtime.bus.EventBus`, `pipeline.realtime.npm_listener.NPM_STREAM/CONSUMER_GROUP`, `pipeline.realtime.hn_listener.HN_STREAM/CONSUMER_GROUP`, `pipeline.transformer.RepositoryTransformer`, `pipeline.loader.PostgreSQLLoader`, `pipeline.matching.resolve` (moved here in Step 1 below).
- Produces: `drain(bus: EventBus, transformer: RepositoryTransformer, loader: PostgreSQLLoader) -> int` - rows written, combining both streams.

- [ ] **Step 1: Move `resolve()` out of `pipeline/main.py`, to avoid a circular import**

`drain.py` needs the same URL-resolution logic `run()` already uses for HN mentions - `resolve(loader, mentions)`, currently defined in `pipeline/main.py`. Importing it from there directly would be circular: Task 7 makes `pipeline/main.py` import `drain` from `pipeline/realtime/drain.py`, so `drain.py` importing back from `pipeline/main.py` would have the two modules importing each other. `pipeline/matching.py` (which already holds `resolve_by_name`, the other half of mention resolution) is the natural shared home - both `pipeline/main.py` and `pipeline/realtime/drain.py` can import from it with neither importing the other.

This is a pure move, not a rewrite - the function's body does not change, only which file it lives in.

Run `grep -n "def resolve\b" pipeline/main.py` to find its exact current location, then:

1. Cut the `resolve(loader: PostgreSQLLoader, mentions: list[dict]) -> list[dict]` function (and its docstring) out of `pipeline/main.py` entirely.
2. Paste it into `pipeline/matching.py`, at the end of the file. Its `loader: PostgreSQLLoader` type hint needs `from pipeline.loader import PostgreSQLLoader` added to `matching.py`'s imports - safe to add: `pipeline/loader.py` imports only `pipeline.metrics`, `pipeline.config`, `pipeline.exceptions`, and `pipeline.logger`, none of which import `pipeline.matching` or `pipeline.main`, so this does not introduce a circular import of its own.
3. In `pipeline/main.py`, change the existing `from pipeline.matching import resolve_by_name` line to `from pipeline.matching import resolve, resolve_by_name`.
4. Leave every call site inside `run()` (`resolve(loader, extractor.extract())` and `resolve(loader, loader.unresolved_mentions())`) untouched - they call `resolve` by name either way, and the import above is what makes that name resolve to the moved function.

Run: `pytest tests/test_integration.py -v`
Expected: All PASS, unchanged - this step is a pure relocation with no behavior change, so nothing here should start failing or start passing that wasn't already.

Commit this step on its own, before writing anything new:

```bash
git add pipeline/matching.py pipeline/main.py
git commit -m "Move resolve() from main.py to matching.py, ahead of drain.py needing it without a circular import"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_realtime_drain.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_realtime_drain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.realtime.drain'`.

- [ ] **Step 4: Write the implementation**

Create `pipeline/realtime/drain.py`:

```python
"""Drains the real-time event streams into Postgres, through the exact same
transformer/loader path a batch source already goes through.

Called once a day, from pipeline/main.py's run() (Task 7) - after the batch
sources so there is something in raw_repositories for HN mentions to
resolve against, and before the daily snapshot so anything captured
overnight counts in today's history.

Every drained entry is acked only after it has been successfully handed to
the loader - a crash between reading and acking leaves the entry pending,
and the next drain call picks it up again via EventBus.read_pending_then_new
rather than losing it. Acking too early (before the write actually lands)
would be the same silent-data-loss shape this project has a running list of
avoiding elsewhere.
"""

from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger
from pipeline.matching import resolve
from pipeline.realtime.bus import EventBus
from pipeline.realtime.hn_listener import CONSUMER_GROUP as HN_GROUP
from pipeline.realtime.hn_listener import HN_STREAM
from pipeline.realtime.npm_listener import CONSUMER_GROUP as NPM_GROUP
from pipeline.realtime.npm_listener import NPM_STREAM
from pipeline.transformer import RepositoryTransformer

# - Consumer name fixed rather than derived from a hostname or PID: the
#   daily batch is the only thing that ever drains, one at a time, so there
#   is exactly one consumer identity that matters, and a fixed name means a
#   crashed run's pending entries are always claimed by whichever run drains
#   next, rather than orphaned under a consumer name nothing will ever use
#   again.
DRAIN_CONSUMER = "daily-batch"


def _drain_npm(bus: EventBus, transformer: RepositoryTransformer, loader: PostgreSQLLoader) -> int:
    entries = bus.read_pending_then_new(NPM_STREAM, NPM_GROUP, DRAIN_CONSUMER)
    if not entries:
        return 0

    rows = transformer.transform_all([event for _, event in entries], "npm")
    written = loader.load_repositories(rows)
    for entry_id, _ in entries:
        bus.ack(NPM_STREAM, NPM_GROUP, entry_id)
    return written


def _drain_hn(bus: EventBus, loader: PostgreSQLLoader) -> int:
    entries = bus.read_pending_then_new(HN_STREAM, HN_GROUP, DRAIN_CONSUMER)
    if not entries:
        return 0

    mentions = resolve(loader, [event for _, event in entries])
    written = loader.load_mentions(mentions)
    for entry_id, _ in entries:
        bus.ack(HN_STREAM, HN_GROUP, entry_id)
    return written


def drain(bus: EventBus, transformer: RepositoryTransformer, loader: PostgreSQLLoader) -> int:
    """Drain both real-time streams. Never raises on a source-specific
    failure - matches pipeline/main.py's own "one source, one try block"
    discipline, since real-time ingestion is one more source, not a
    different kind of thing."""
    log = get_logger("realtime.drain")
    written = 0

    try:
        written += _drain_npm(bus, transformer, loader)
    except Exception as exc:
        log.exception("npm drain failed", extra={"context": {"error": str(exc)}})

    try:
        written += _drain_hn(bus, loader)
    except Exception as exc:
        log.exception("HN drain failed", extra={"context": {"error": str(exc)}})

    log.info("drain finished", extra={"context": {"written": written}})
    return written
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_realtime_drain.py -v`
Expected: All PASS.

- [ ] **Step 6: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add pipeline/realtime/drain.py tests/test_realtime_drain.py
git commit -m "Add the real-time-to-Postgres drain step"
```

---

## Task 7: Wire the drain and the tracked-npm refresh into `pipeline/main.py`

**Files:**
- Modify: `pipeline/main.py`
- Test: `tests/test_integration.py` - the file that already exercises `run()` end to end with the sources and the loader stood in for (`fake_extractor`, the `loader` fixture wrapping a `MagicMock`, and the `use(monkeypatch, *extractors)` helper that replaces `EXTRACTORS`/clears `MENTION_EXTRACTORS`, all defined at the top of that file).

**Interfaces:**
- Consumes: `pipeline.realtime.bus.EventBus`, `pipeline.realtime.drain.drain` (Task 6).
- Modifies: `run(config)` gains two new steps; behavior for every existing step is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_integration.py`, using the file's own `config`/`loader` fixtures and `use()` helper exactly as every existing test in it already does:

```python
def test_run_drains_the_realtime_streams(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))
    loader.rows_for.return_value = []

    drain_calls = []
    monkeypatch.setattr(main_module, "EventBus", lambda url: "fake-bus")
    monkeypatch.setattr(
        main_module, "drain",
        lambda bus, transformer, loader: drain_calls.append(bus) or 0,
    )

    run(config)
    assert drain_calls == ["fake-bus"]


def test_run_refreshes_the_tracked_npm_set_after_npm_collection(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("npm", [dict(RAW, id="npm_1", source="npm")]))
    # - Stands in for what loader.rows_for("npm") would return after a real
    #   run loaded these - the same method the quality checks already call,
    #   reused rather than tracked separately.
    loader.rows_for.return_value = [
        dict(RAW, id="npm_1", source="npm"),
        dict(RAW, id="npm_2", source="npm"),
    ]

    refresh_calls = []

    class FakeBus:
        def replace_tracked_npm(self, ids):
            refresh_calls.append(ids)

    monkeypatch.setattr(main_module, "EventBus", lambda url: FakeBus())
    monkeypatch.setattr(main_module, "drain", lambda bus, transformer, loader: 0)

    run(config)
    assert refresh_calls == [{"npm_1", "npm_2"}]


def test_a_dead_realtime_bus_does_not_fail_the_run(config, loader, monkeypatch):
    # - A refresh and a drain that both blow up must cost neither github's
    #   data nor a reported failure - same "one source, one try block"
    #   discipline every other step in run() already gets.
    use(monkeypatch, fake_extractor("github", [RAW]))
    loader.rows_for.return_value = []

    class BrokenBus:
        def replace_tracked_npm(self, ids):
            raise RuntimeError("redis is down")

    def broken_drain(bus, transformer, loader):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(main_module, "EventBus", lambda url: BrokenBus())
    monkeypatch.setattr(main_module, "drain", broken_drain)

    loaded, failed = run(config)
    assert loaded == 1
    assert failed == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_integration.py -v -k "drain or tracked_npm or realtime_bus"`
Expected: FAIL - `run()` doesn't call `drain`, doesn't reference `main_module.EventBus`, and doesn't refresh the tracked set yet, so the `monkeypatch.setattr(main_module, "EventBus", ...)` calls themselves fail with `AttributeError: module 'pipeline.main' has no attribute 'EventBus'`.

- [ ] **Step 3: Implement in `pipeline/main.py`**

Add to the imports at the top:

```python
from pipeline.realtime.bus import EventBus
from pipeline.realtime.drain import drain
```

In `run()`, after the `EXTRACTORS` loop and before the `MENTION_EXTRACTORS` loop, add the tracked-npm refresh (it needs whatever npm rows this run just loaded, which `loader.rows_for("npm")` already provides - the same method the quality checks already call):

```python
        # - Refreshes what the always-on npm listener filters against. Here
        #   specifically: after npm's own collection loop, so a package
        #   found today is filterable today, and before the mention/drain
        #   steps below, which don't depend on this ordering but keep every
        #   real-time-related step grouped together for a reader.
        bus = EventBus(config.redis_realtime_url)
        try:
            npm_ids = {row["id"] for row in loader.rows_for("npm")}
            bus.replace_tracked_npm(npm_ids)
        except Exception as exc:
            log.exception("could not refresh tracked npm packages", extra={"context": {"error": str(exc)}})
```

Then, after the discovery block and before the final `loader.snapshot(...)` call, add the drain step:

```python
        # - After discovery (so newly-discovered repositories exist for
        #   real-time HN mentions to resolve against) and before the
        #   snapshot (so anything captured overnight counts in today's
        #   history) - the same ordering reasoning the existing steps above
        #   already follow.
        try:
            drain(bus, transformer, loader)
        except Exception as exc:
            log.exception("realtime drain failed", extra={"context": {"error": str(exc)}})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_integration.py -v`
Expected: All PASS, including every pre-existing test - `run()`'s existing behavior must be unchanged when real-time ingestion isn't configured (`REDIS_REALTIME_URL` unset means `EventBus(url="")`, every method a safe no-op, per Task 2's constraints).

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/main.py tests/test_integration.py
git commit -m "Wire the real-time drain and tracked-npm refresh into the daily run"
```

---

## Task 8: `WS /live` on the existing RAG API

**Files:**
- Modify: `pipeline/rag/api.py`
- Test: `tests/test_rag_api.py`

**Interfaces:**
- Consumes: `pipeline.realtime.bus.EventBus`, `pipeline.realtime.npm_listener.NPM_STREAM`, `pipeline.realtime.hn_listener.HN_STREAM`.
- Produces: `WS /live` - a WebSocket route on the existing FastAPI app.

- [ ] **Step 1: Write the failing test**

FastAPI's `TestClient` supports WebSocket testing directly - this test uses a fake bus the same way existing tests in this file stub the chain/retriever, per `build_app`'s existing pattern of taking dependencies in rather than importing them at module scope.

Add to `tests/test_rag_api.py`:

```python
def test_live_websocket_streams_a_published_event():
    from pipeline.realtime.bus import EventBus
    from pipeline.realtime.npm_listener import NPM_STREAM, CONSUMER_GROUP

    class FakeRedisForLive:
        def __init__(self):
            self.streams = {}
            self._seq = 0

        def xadd(self, stream, fields):
            self._seq += 1
            entry_id = f"{self._seq}-0"
            self.streams.setdefault(stream, []).append((entry_id, fields))
            return entry_id

        def xread(self, streams, count=None, block=None):
            (stream, last_id), = streams.items()
            entries = self.streams.get(stream, [])
            new = [(eid, f) for eid, f in entries if eid > last_id] if last_id != "$" else []
            return [(stream, new)] if new else []

    bus = EventBus(url="")
    bus.client = FakeRedisForLive()
    bus.client.xadd(NPM_STREAM, {"data": '{"id": "npm_left-pad", "name": "left-pad"}'})

    config = Config()
    app = build_app(config, chain=StubChain(), retriever=StubRetriever(), realtime_bus=bus)
    client = TestClient(app)

    with client.websocket_connect("/live") as websocket:
        message = websocket.receive_json()
        assert message["stream"] == "npm"
        assert message["event"]["id"] == "npm_left-pad"


def test_live_websocket_with_no_bus_configured_closes_cleanly():
    # - REDIS_REALTIME_URL unset is the common case. The endpoint must exist
    #   and accept the connection, then close it, rather than the app
    #   failing to start or the route 404ing outright.
    from pipeline.realtime.bus import EventBus

    config = Config()
    app = build_app(config, chain=StubChain(), retriever=StubRetriever(), realtime_bus=EventBus(url=""))
    client = TestClient(app)

    with client.websocket_connect("/live") as websocket:
        with pytest.raises(Exception):
            websocket.receive_json()  # connection closes with nothing to send
```

Check `build_app`'s current signature and the `StubChain`/`StubRetriever` fixtures already used elsewhere in this file - the calls above assume `build_app` gains a new required-or-defaulted `realtime_bus` parameter; adjust the exact stub construction to match whatever helper functions this file already provides for building a `TestClient` against a stubbed app.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rag_api.py -v -k live`
Expected: FAIL - `build_app` doesn't accept `realtime_bus` yet, and there's no `/live` route.

- [ ] **Step 3: Implement in `pipeline/rag/api.py`**

Add to the imports:

```python
import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from pipeline.realtime.bus import EventBus
from pipeline.realtime.hn_listener import HN_STREAM
from pipeline.realtime.npm_listener import NPM_STREAM
```

Change `build_app`'s signature to accept the bus, defaulting to an unconfigured one so every existing caller (and every existing test) that doesn't pass it keeps working unchanged:

```python
def build_app(config: Config, *, chain, retriever, realtime_bus: EventBus | None = None) -> FastAPI:
```

Inside `build_app`, near where `app.state.chain`/`app.state.retriever` are set:

```python
    app.state.realtime_bus = realtime_bus if realtime_bus is not None else EventBus(config.redis_realtime_url)
```

Add the route, alongside the other route definitions (e.g. near `/trending`, since both are read-only and cost nothing):

```python
    @app.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        """Pushes real-time npm/HN events as they land on the bus.

        Reads the always-on Redis Streams directly - deliberately not
        through Postgres, so this works whether or not the daily batch has
        drained anything yet. A connection here sees only what happens
        while it's open; there is no history replay on connect, since
        XREAD's blocking-read mode (rather than a consumer group) is what
        lets many simultaneous viewers share one read position each without
        stepping on each other's acknowledgment state the way the drain
        step's consumer group does.
        """
        await websocket.accept()
        bus = app.state.realtime_bus
        if bus.client is None:
            await websocket.close()
            return

        last_ids = {NPM_STREAM: "$", HN_STREAM: "$"}
        try:
            while True:
                try:
                    result = bus.client.xread(last_ids, count=50, block=5000)
                except Exception:
                    await asyncio.sleep(1)
                    continue
                for stream, entries in result:
                    for entry_id, fields in entries:
                        last_ids[stream] = entry_id
                        await websocket.send_json({
                            "stream": "npm" if stream == NPM_STREAM else "hn",
                            "event": json.loads(fields["data"]),
                        })
        except WebSocketDisconnect:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rag_api.py -v`
Expected: All PASS, including every pre-existing test - `build_app`'s new parameter must not break any existing call site or test that doesn't pass it.

- [ ] **Step 5: Update `main()`'s call to `build_app`**

In `pipeline/rag/api.py`'s `main()`, the existing `build_app(config, chain=..., retriever=...)` call needs no change - the new parameter defaults to building a real `EventBus` from `config.redis_realtime_url` automatically, matching how every other optional dependency in this module already defaults.

- [ ] **Step 6: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 7: Manual live check - do this before considering the task done**

With a real, reachable `REDIS_REALTIME_URL` and at least one listener running (Tasks 4/5's manual checks, or a hand-published test event), start the API (`python -m pipeline.rag.api`) and connect a WebSocket client (e.g. `websocat ws://localhost:8001/live`, or a short Python script using the `websockets` library) - confirm a real event appears on the connection, not just that the socket accepts.

- [ ] **Step 8: Commit**

```bash
git add pipeline/rag/api.py tests/test_rag_api.py
git commit -m "Add WS /live, streaming real-time npm/HN events to connected clients"
```

---

## Task 9: Always-on hosting - Memurai and the two listeners as Windows services

**Files:**
- Create: `ops/realtime/install-realtime-services.ps1`
- Create: `ops/realtime/README.md`
- Modify: `README.md` (root) - one new section

**Interfaces:** None - deployment/ops scripting, verified against the real machine, not unit-tested.

- [ ] **Step 1: Install Memurai**

Download and install Memurai Developer (free edition) from memurai.com, per its own install guide. Confirm it's running as a Windows service and reachable:

```powershell
Get-Service Memurai
redis-cli -p 6379 ping   # or Memurai's own CLI if redis-cli isn't installed separately
```

Expected: `Running`, and `PONG`.

Note the port Memurai listens on (default 6379 - if the K8s-deployed Redis or anything else on this machine already uses 6379, configure Memurai to a different port, e.g. 6380, via its config file, and use that port consistently in every step below).

- [ ] **Step 2: Set `REDIS_REALTIME_URL`**

Add to `.env` (not `.env.example` - this is a real, machine-specific value):

```
REDIS_REALTIME_URL=redis://localhost:6380/0
```

(Substitute whatever port Step 1 landed on.)

- [ ] **Step 3: Write `ops/realtime/install-realtime-services.ps1`**

Following the exact pattern `ops/install-scheduled-task.ps1` already established for the daily collection task - registered once, safe to re-run, prints what it did:

```powershell
# Registers the two real-time listeners as Windows services via NSSM, so
# they start on boot and restart automatically if either one crashes.
#
# Run it once. Re-running is safe; it replaces the existing services.
#
#   powershell -ExecutionPolicy Bypass -File ops/realtime/install-realtime-services.ps1
#
# To remove them:
#
#   nssm remove HecateNpmListener confirm
#   nssm remove HecateHnListener confirm

[CmdletBinding()]
param(
    [string]$PythonExe = (Get-Command python).Source,
    [string]$RepoRoot = (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    throw "nssm not found on PATH. Install it first (e.g. 'winget install nssm' or download from nssm.cc)."
}

function Install-ListenerService {
    param([string]$Name, [string]$Module)

    nssm install $Name $PythonExe "-m" $Module
    nssm set $Name AppDirectory $RepoRoot
    # - Restart on any exit, including a clean one - these processes are
    #   meant to run forever; the only reason either exits is a crash inside
    #   its own retry loop giving up, which should not happen given both
    #   modules already retry internally, but the service layer is the
    #   backstop if it ever does.
    nssm set $Name AppExit Default Restart
    nssm set $Name AppRestartDelay 5000
    nssm set $Name Start SERVICE_AUTO_START
    nssm start $Name

    "installed  : $Name"
    "module     : $Module"
    "state      : $(nssm status $Name)"
    ""
}

Install-ListenerService -Name 'HecateNpmListener' -Module 'pipeline.realtime.npm_listener'
Install-ListenerService -Name 'HecateHnListener' -Module 'pipeline.realtime.hn_listener'

"Both services installed. Check status any time with:"
"  nssm status HecateNpmListener"
"  nssm status HecateHnListener"
```

- [ ] **Step 4: Run it and confirm both services are actually running**

```powershell
powershell -ExecutionPolicy Bypass -File ops/realtime/install-realtime-services.ps1
Start-Sleep -Seconds 5
nssm status HecateNpmListener
nssm status HecateHnListener
```

Expected: both print `SERVICE_RUNNING`.

- [ ] **Step 5: Confirm they're actually producing events, not just running**

```powershell
redis-cli -p 6380 XLEN hecate:events:npm
redis-cli -p 6380 XLEN hecate:events:hn
```

Run this once immediately (likely both `0`, nothing has happened yet) and again after 30-60 minutes - at least one of the two should have grown, confirming the services are genuinely capturing real events, not just idling without error. If both are still `0` after an hour, check `nssm status` for a crash loop and read the service's stdout log (NSSM writes to a log file if `AppStdout`/`AppStderr` are configured - add those to Step 3's install function if not already logging, so this is diagnosable).

- [ ] **Step 6: Measure the real resource footprint - the spec estimated this, this confirms it**

The spec's cost analysis estimated under 300MB combined for the two listeners plus Memurai, reasoned from how lightweight this kind of code typically runs - not measured. Confirm it against the real, running services over a real 24-hour period:

```powershell
Get-Process | Where-Object { $_.ProcessName -like "*python*" -or $_.ProcessName -like "*memurai*" } | Select-Object ProcessName, Id, @{N='RAM(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}}, CPU
```

Run this once shortly after Step 4's install, and again a full day later. Record both readings. If the real figure is meaningfully above the spec's under-300MB estimate, that is worth knowing before calling this phase's cost claim settled - update the spec's cost section with the real number rather than leaving the estimate standing unchallenged.

- [ ] **Step 7: Confirm a crashed listener doesn't corrupt the stream or block the other one**

```powershell
nssm stop HecateNpmListener
# - Simulates a hard crash rather than a graceful stop, so NSSM's AppExit
#   Restart policy (Task 9 Step 3) is what brings it back, the same as a
#   real unhandled exception would trigger.
nssm status HecateNpmListener
Start-Sleep -Seconds 10
nssm status HecateNpmListener   # expected: SERVICE_RUNNING again, NSSM restarted it
redis-cli -p 6380 XLEN hecate:events:hn   # expected: still growing independently - the HN listener's own service was never touched
```

Expected: the npm service is back to `SERVICE_RUNNING` on its own within the restart delay configured in Task 9 Step 3 (5 seconds), and the HN listener's stream continued growing throughout, undisturbed by the npm listener's outage - confirming the "one listener down costs that listener, not the other, and not the buffered stream" guarantee the spec's integration checklist calls for.

- [ ] **Step 8: Handle Memurai's 10-day uptime limit**

The free Developer edition requires a restart after 10 days of uptime. Since the bus is drained daily anyway (Task 7), a restart loses at most a partial day's unconsumed buffer, not history - not worth building anything elaborate around. Add a weekly restart via Windows Task Scheduler:

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-Command "Restart-Service Memurai"'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '02:00'
Register-ScheduledTask -TaskName 'Restart Memurai Weekly' -Action $action -Trigger $trigger -Description 'Memurai Developer requires a restart within 10 days of uptime.'
```

- [ ] **Step 9: Write `ops/realtime/README.md`**

```markdown
# Real-time ingestion services

Two always-on Windows services, separate from the daily windowed collection
task (`ops/windowed-run.ps1`) and from Docker Desktop entirely - confirmed
that `windowed-run.ps1` stops Docker Desktop wholesale, not just the K8s
cluster, so anything meant to survive that has to live outside Docker.

| service | what it does |
|---|---|
| `HecateNpmListener` | Runs `pipeline.realtime.npm_listener`, tailing npm's live replication feed |
| `HecateHnListener` | Runs `pipeline.realtime.hn_listener`, polling Hacker News's live updates feed |

Both publish into Memurai (a Windows-native Redis-compatible server - plain
Redis has no first-party Windows build), a second, separate Redis instance
from the one the RAG service uses for context caching. Losing the cache
costs a slower answer; losing this one costs events that cannot be
recaptured, since they were only ever seen live.

Install once with `install-realtime-services.ps1`. Check status any time
with `nssm status HecateNpmListener` / `nssm status HecateHnListener`.

The daily collection run drains both streams into Postgres through the
normal pipeline - see `pipeline/realtime/drain.py`. If both services have
been running but the streams stay empty, check `REDIS_REALTIME_URL` in
`.env` points at the right port before assuming the listeners are broken.
```

- [ ] **Step 10: Add a section to root `README.md`**

Add a new `## Real-time ingestion` section, after the existing `## Asking it questions` section, matching that section's tone and level of detail:

```markdown
## Real-time ingestion

npm publishes and Hacker News posts about tracked (or discoverable) projects
show up within seconds, not the next day's batch — the only two of Hecate's
six sources that genuinely support this without requiring a relationship
with the source Hecate doesn't have (see `docs/specs/2026-08-14-realtime-
ingestion-design.md` for why GitHub, GitLab, and PyPI stay batch).

Two always-on Windows services (`ops/realtime/`) listen continuously and
publish into a small, separate, always-on Redis instance (Memurai, since
plain Redis has no first-party Windows build) — independent of the daily
windowed cluster, which stays off most of the day exactly as before. The
daily batch drains what was captured into the same `raw_repositories`/
`social_mentions` tables everything else lands in — no separate schema.

```bash
powershell -ExecutionPolicy Bypass -File ops/realtime/install-realtime-services.ps1
```

Then connect to `ws://localhost:8001/live` (with the RAG API running) to see
events as they're captured.
```

- [ ] **Step 11: Commit**

```bash
git add ops/realtime/ README.md
git commit -m "Add always-on Windows services for the real-time listeners"
```

---

## Task 10: Full suite verification

**Files:** None - verification only.

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -q`
Expected: All PASS, including every test from Tasks 1-8 and every pre-existing test untouched by this plan.

- [ ] **Step 2: Run the integration-tests-are-not-skipped check**

Run: `HECATE_INTEGRATION=1 pytest -q` (needs a real Postgres - `docker compose up -d postgres` per the README's local setup)
Expected: tests run and pass.

- [ ] **Step 3: Confirm every manual live check from Tasks 4, 5, and 8 was actually run**

Not a pytest step - a checklist, because CI cannot verify any of these and "tests pass" doesn't cover them:

- [ ] Task 4 Step 6 (real npm feed, a known package's publish actually arrives)
- [ ] Task 5 Step 6 (real HN feed, a real post actually arrives)
- [ ] Task 8 Step 7 (a connected WebSocket client actually receives a real event)
- [ ] Task 9 Step 5 (both services confirmed producing real events over a real hour, not just running without error)
- [ ] Task 9 Step 6 (real RAM/CPU measured over 24h against the spec's under-300MB estimate, not left as an assumption)
- [ ] Task 9 Step 7 (a killed listener recovers on its own and the other listener's stream keeps growing undisturbed)

If any of these weren't actually done, this plan is not finished no matter what pytest says.

- [ ] **Step 4: Confirm the existing windowed daily batch is unaffected**

Run the daily batch by hand once (`powershell -ExecutionPolicy Bypass -File ops/windowed-run.ps1 -StartOnly`, then trigger a run the same way the README's "trigger one rather than waiting" section already documents) and confirm:
- `ops/logs/run-log.jsonl` gets a new entry with `ok: true`, same as before this plan existed
- The snapshot still lands for today's date
- Nothing about the existing four scheduled jobs' behavior or timing changed

- [ ] **Step 5: Push and confirm CI is green**

```bash
git push
```

Check the GitHub Actions run for the `tests` workflow. Expected green, understanding per Step 3 above that CI green here means "the code is wired correctly," not "the real feeds and the real always-on services work" - that's what Step 3's checklist covers.
