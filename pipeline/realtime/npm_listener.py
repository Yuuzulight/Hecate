"""Listens to npm's own real-time replication feed and publishes matches.

npm has no per-package webhook, but the whole registry is served through a
CouchDB-shaped _changes endpoint at replicate.npmjs.com. The original design
here held one long-lived `feed=continuous` connection open, following
npm/registry's own REPLICATE-API docs - but the live service rejects that:
confirmed directly against the real endpoint that `feed=continuous`,
`feed=normal`, `feed=longpoll`, and even a bare `include_docs=true` on any
request all return 400 Bad Request, regardless of `since`. The only request
shape that works is a plain `_changes?since=<seq>` poll with no `feed`
parameter, which returns changed ids and revisions but not the document
body - so this polls on a short interval instead of streaming, and fetches
each tracked package's current document separately from the ordinary public
registry (config.npm_registry - the exact host pipeline.extractors.npm
already fetches from), the same two-step shape the Hacker News listener
already uses for an unrelated reason (Firebase's /updates feed also only
names changed ids, not bodies).

Most of the registry's changes are irrelevant to Hecate (it tracks a few
thousand packages out of the whole registry), so every change is checked
against the tracked-npm-packages set (pipeline.realtime.bus.EventBus.
is_tracked_npm) BEFORE fetching its document - fetching first would mean
hitting the public registry for nearly every change on npm, most of which
would just be discarded a line later.

Deliberately does not persist the since cursor across restarts. A restart
starts polling from "now" (the registry's current update_seq at startup)
rather than replaying history - the registry has emitted well over 100
million changes total, so resuming from 0 is not an option, and persisting
the cursor somewhere durable is a natural first extension once this has run
long enough to know how often a restart-sized gap actually matters (see the
spec's "Deferred" section - not required for this phase).
"""

import time

import requests

from pipeline.config import Config
from pipeline.extractors.npm import registry_doc_to_row
from pipeline.logger import get_logger
from pipeline.realtime.bus import NPM_GROUP, NPM_STREAM, EventBus

DB_URL = "https://replicate.npmjs.com/"
CHANGES_URL = "https://replicate.npmjs.com/_changes"

# - CONSUMER_GROUP kept as an alias, not just a rename: existing importers
#   (this module's own __main__, and anything reaching for the group name
#   under its old local name) keep working. NPM_STREAM and NPM_GROUP now
#   live in pipeline/realtime/bus.py - see that module's comment for why.
CONSUMER_GROUP = NPM_GROUP

POLL_SECONDS = 10
REQUEST_TIMEOUT = 10
# - Caps how many changes one poll cycle processes - a large backlog (e.g.
#   right after startup) is still drained, just over more than one cycle
#   rather than in one unbounded request.
PAGE_LIMIT = 500


def current_seq(session: requests.Session) -> int:
    """The registry's current change sequence, to start polling from "now"
    rather than replaying the full history."""
    response = session.get(DB_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()["update_seq"]


def poll_changes(session: requests.Session, since: int) -> tuple[list[dict], int]:
    """One page of changes since the given seq, and the seq to resume from
    next time. Never includes feed= or include_docs= - both are rejected by
    the live service regardless of value (confirmed directly - see this
    module's docstring)."""
    response = session.get(
        CHANGES_URL,
        params={"since": since, "limit": PAGE_LIMIT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("results") or [], body.get("last_seq", since)


def changed_package_id(entry: dict) -> str | None:
    """The package name one _changes result names, or None if there's
    nothing to act on - a deletion (no live document left to describe) or a
    malformed entry missing "id"."""
    if not isinstance(entry, dict) or entry.get("deleted"):
        return None
    package_id = entry.get("id")
    return package_id if isinstance(package_id, str) and package_id else None


def handle_change(bus: EventBus, session: requests.Session, npm_registry: str, package_id: str) -> bool:
    """Fetch and publish one changed package's current document, if it's
    tracked. Returns whether it was. The tracked check runs before the
    fetch, since almost every change on the registry is for an untracked
    package - fetching first would hit registry.npmjs.org for a change the
    very next line would discard."""
    if not bus.is_tracked_npm(package_id):
        return False
    response = session.get(f"{npm_registry}/{package_id}", timeout=REQUEST_TIMEOUT)
    if not response.ok:
        return False
    row = registry_doc_to_row(response.json())
    bus.publish(NPM_STREAM, row)
    return True


def run(config: Config) -> None:
    """Poll for changed packages forever - the caller (Task 9's service
    wrapper) restarts this on exit, same as the HN listener."""
    log = get_logger("realtime.npm_listener")
    if not config.redis_realtime_url:
        raise SystemExit("REDIS_REALTIME_URL is required to run the npm listener")

    bus = EventBus(config.redis_realtime_url)
    bus.ensure_group(NPM_STREAM, CONSUMER_GROUP)
    session = requests.Session()

    since = current_seq(session)
    log.info("npm listener starting", extra={"context": {"since": since}})

    published = 0
    while True:
        entries, since = poll_changes(session, since)
        for entry in entries:
            package_id = changed_package_id(entry)
            if package_id is None:
                continue
            if handle_change(bus, session, config.npm_registry, package_id):
                published += 1
                if published % 20 == 0:
                    log.info("npm events published", extra={"context": {"total": published}})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    while True:
        try:
            run(Config())
        except Exception as exc:  # noqa: BLE001 - a long-lived listener must not die silently or permanently
            get_logger("realtime.npm_listener").exception(
                "npm listener crashed, restarting in 10s", extra={"context": {"error": str(exc)}}
            )
            time.sleep(10)
