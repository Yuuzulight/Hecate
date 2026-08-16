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
from pipeline.realtime.bus import EventBus, HN_GROUP, HN_STREAM

UPDATES_URL = "https://hacker-news.firebaseio.com/v0/updates.json"
ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"

# - CONSUMER_GROUP kept as an alias, not just a rename: existing importers
#   keep working. HN_STREAM and HN_GROUP now live in
#   pipeline/realtime/bus.py - see that module's comment for why.
CONSUMER_GROUP = HN_GROUP

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
