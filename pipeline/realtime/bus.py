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
                try:
                    event = json.loads(fields["data"])
                    entries.append((entry_id, event))
                except (json.JSONDecodeError, ValueError) as exc:
                    self.log.warning(
                        "skipping malformed stream entry",
                        extra={"context": {"stream": stream, "entry_id": entry_id, "error": str(exc)}},
                    )
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
