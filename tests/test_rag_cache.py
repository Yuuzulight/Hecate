"""Context cache: keying, and what happens when Redis is not there.

The interesting behaviour is the failure one. A cache that takes the service
down when it is unavailable is worse than no cache, so most of this is about
Redis being broken rather than Redis working.
"""

import json

import pytest
import redis

from pipeline.rag.cache import ContextCache, context_key


class FakeRedis:
    """Enough of a Redis to exercise get/setex, or to fail on demand."""

    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail
        self.expiries: dict[str, int] = {}

    def get(self, key):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.fail:
            raise redis.ConnectionError("no route to host")
        self.store[key] = value
        self.expiries[key] = ttl


@pytest.fixture
def cache():
    c = ContextCache(url="", ttl_seconds=60)
    c.client = FakeRedis()
    return c


def test_the_same_question_on_the_same_data_is_the_same_key():
    assert context_key("what is trending", "2026-08-09") == context_key(
        " What Is Trending ", "2026-08-09"
    )


def test_new_data_is_a_new_key():
    # - The point of the version. Without it a question asked before the day's
    #   collection keeps returning yesterday's context afterwards.
    assert context_key("what is trending", "2026-08-09") != context_key(
        "what is trending", "2026-08-10"
    )


def test_a_different_question_is_a_different_key():
    assert context_key("what is trending", "v") != context_key("what is stale", "v")


def test_a_stored_context_comes_back(cache):
    cache.set("k", {"coverage": {"snapshot_days": 3}})
    assert cache.get("k") == {"coverage": {"snapshot_days": 3}}


def test_a_missing_key_is_a_miss(cache):
    assert cache.get("nothing") is None


def test_the_ttl_is_applied(cache):
    cache.set("k", {"a": 1})
    assert cache.client.expiries["k"] == 60


def test_no_url_means_no_cache_and_no_error():
    # - Running without Redis is a configuration, not a fault.
    c = ContextCache(url="")
    c.set("k", {"a": 1})
    assert c.get("k") is None


def test_redis_being_unreachable_reads_as_a_miss(cache):
    cache.client = FakeRedis(fail=True)
    assert cache.get("k") is None


def test_redis_being_unreachable_does_not_break_a_write(cache):
    cache.client = FakeRedis(fail=True)
    cache.set("k", {"a": 1})  # must not raise


def test_hits_and_misses_are_logged(cache, caplog):
    with caplog.at_level("INFO"):
        cache.get("absent")
        cache.set("present", {"a": 1})
        cache.get("present")
    messages = [r.message for r in caplog.records]
    assert "cache miss" in messages
    assert "cache hit" in messages


def test_a_hit_a_miss_and_an_outage_are_counted_apart(cache):
    # - A miss and an unreachable Redis both end in a database query, but one
    #   is the cache working and the other is the cache being gone. A hit rate
    #   that folds outages into misses is a hit rate about nothing.
    from pipeline import metrics

    def value(result):
        return metrics.rag_context_cache.labels(result=result)._value.get()

    before = {r: value(r) for r in ("hit", "miss", "unavailable")}

    cache.get("absent")
    cache.set("present", {"a": 1})
    cache.get("present")
    cache.client = FakeRedis(fail=True)
    cache.get("present")

    assert value("miss") - before["miss"] == 1
    assert value("hit") - before["hit"] == 1
    assert value("unavailable") - before["unavailable"] == 1


def test_what_is_stored_is_json(cache):
    cache.set("k", {"coverage": {"history_to": "2026-08-09"}})
    # - Dates are cast to text in the SQL so a cached context and a fresh one
    #   are the same shape. If that ever regresses, this stops round-tripping.
    assert json.loads(cache.client.store["k"]) == {"coverage": {"history_to": "2026-08-09"}}
