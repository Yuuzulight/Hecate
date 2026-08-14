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
