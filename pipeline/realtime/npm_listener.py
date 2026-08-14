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
from pipeline.realtime.bus import EventBus, NPM_GROUP, NPM_STREAM

FEED_URL = "https://replicate.npmjs.com/registry/_changes"

# - CONSUMER_GROUP kept as an alias, not just a rename: existing importers
#   (this module's own __main__, and anything reaching for the group name
#   under its old local name) keep working. NPM_STREAM and NPM_GROUP now
#   live in pipeline/realtime/bus.py - see that module's comment for why.
CONSUMER_GROUP = NPM_GROUP

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
