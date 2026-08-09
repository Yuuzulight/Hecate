"""Caching assembled context, and surviving the cache being gone.

The failure this is written around is not a cache miss. It is Redis being
unreachable while a question is waiting: without timeouts a dead cache turns
every request into a hang, which is worse than having no cache at all. So
every call is bounded, every Redis error is swallowed to a miss, and the
retriever carries on against PostgreSQL.
"""

import hashlib
import json

import redis

from pipeline.logger import get_logger

# - Short enough that a key scheme mistake expires on its own, long enough to
#   be worth having. The version in the key is what actually keeps answers
#   fresh; this is the backstop for whatever the version misses.
DEFAULT_TTL_SECONDS = 900

# - A dead Redis has to fail fast. The default is no timeout at all, which
#   means a question waits on a TCP connect that will never complete.
#
# ponytail: every call pays this timeout while Redis is down - measured at
# 2.4s against 0.14s healthy, which is inside the 5s target. If that stops
# being true, remember the failure for a minute rather than retrying per call.
SOCKET_TIMEOUT_SECONDS = 2


def context_key(question: str, data_version: str) -> str:
    """A key covering the question and the data it would be answered from.

    Without the version, a question asked before the day's collection returns
    yesterday's context afterwards - a stale answer wearing a plausible face,
    which is worse than a slow one.
    """
    digest = hashlib.sha256(f"{data_version}\n{question.strip().lower()}".encode()).hexdigest()
    return f"hecate:context:{digest[:32]}"


class ContextCache:
    """Redis if it answers, nothing if it does not."""

    def __init__(self, url: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self.log = get_logger("rag.cache")
        self.client = None
        if url:
            self.client = redis.from_url(
                url,
                socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
                decode_responses=True,
            )

    def get(self, key: str) -> dict | None:
        if self.client is None:
            return None
        try:
            raw = self.client.get(key)
        except redis.RedisError as exc:
            self.log.warning("cache unavailable", extra={"context": {"error": str(exc)}})
            return None
        if raw is None:
            self.log.info("cache miss", extra={"context": {"key": key}})
            return None
        self.log.info("cache hit", extra={"context": {"key": key}})
        return json.loads(raw)

    def set(self, key: str, value: dict) -> None:
        if self.client is None:
            return
        try:
            self.client.setex(key, self.ttl, json.dumps(value))
        except redis.RedisError as exc:
            # - A cache that cannot be written to is still a working service.
            self.log.warning("cache write failed", extra={"context": {"error": str(exc)}})
