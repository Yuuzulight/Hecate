"""Pipeline entry point.

Runs each source in turn: extract, normalise, load. A source that fails is
logged and counted and the run carries on with the rest, because one API having
a bad afternoon is a poor reason to throw away the other three. The run only
reports failure if every source failed.
"""

import sys
from urllib.parse import urlparse

from pipeline.config import Config
from pipeline.exceptions import HecateError
from pipeline.expectations import RepositoryExpectations
from pipeline.extractors import (
    GitHubExtractor,
    GitLabExtractor,
    HackerNewsExtractor,
    LobstersExtractor,
    NpmExtractor,
    PyPiExtractor,
)
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger
from pipeline.matching import resolve, resolve_by_name
from pipeline.realtime.bus import EventBus
from pipeline.realtime.drain import drain
from pipeline.transformer import RepositoryTransformer

EXTRACTORS = (GitHubExtractor, NpmExtractor, PyPiExtractor, GitLabExtractor)

# - Kept apart from the above on purpose. These produce mentions, which are
#   events about a repository rather than repositories, so they skip the
#   transformer entirely and run after everything else - there has to be
#   something stored before a link has anything to resolve against.
MENTION_EXTRACTORS = (HackerNewsExtractor, LobstersExtractor)


# - A post has to clear this before its project is worth an API call, and no
#   more than this many are fetched per run. Both are guesses until there is
#   enough data to tune them; the cap is the one that matters, because without
#   it a busy day on Hacker News becomes a rate-limit incident.
DISCOVERY_MIN_SCORE = 25
DISCOVERY_LIMIT = 20


def discover(config: Config, loader: PostgreSQLLoader, transformer) -> int:
    """Fetch repositories that are being discussed but aren't tracked.

    This is what makes the dataset answer the question it claims to. Sources
    are seeded by cumulative popularity, so a project trending before it is
    famous is excluded by the seeding - unless something goes and gets it.
    """
    log = get_logger("main")
    candidates = loader.discovery_candidates(DISCOVERY_MIN_SCORE, DISCOVERY_LIMIT)
    if not candidates:
        return 0

    if len(candidates) == DISCOVERY_LIMIT:
        log.info("discovery capped", extra={"context": {"limit": DISCOVERY_LIMIT}})

    # - One extractor per host, built once and reused across candidates so the
    #   retry policy and session are shared rather than rebuilt per lookup.
    by_host = {
        "github.com": GitHubExtractor(config),
        "gitlab.com": GitLabExtractor(config),
        "npmjs.com": NpmExtractor(config),
        "pypi.org": PyPiExtractor(config),
    }

    found_by_source: dict[str, list[dict]] = {}
    for url in candidates:
        host = urlparse(url).hostname or ""
        extractor = by_host.get(host.removeprefix("www."))
        if extractor is None:
            continue
        try:
            raw = extractor.fetch_by_url(url)
        except HecateError as exc:
            log.warning("discovery lookup failed", extra={"context": {"url": url, "error": str(exc)}})
            continue
        if raw:
            found_by_source.setdefault(extractor.source, []).append(
                {**raw, "origin": "discovered"}
            )

    loaded = 0
    for source, found in found_by_source.items():
        rows = transformer.transform_all(found, source)
        loaded += loader.load_repositories(rows)

    log.info(
        "discovered repositories",
        extra={
            "context": {
                "looked_up": len(candidates),
                "added": loaded,
                "by_source": {s: len(f) for s, f in found_by_source.items()},
            }
        },
    )
    return loaded


# - Deliberately below 1.0, so any query can exclude prose matches by asking
#   for full confidence and nothing else has to change.
NAME_MATCH_CONFIDENCE = 0.5


def name_match(loader: PostgreSQLLoader) -> list[dict]:
    """Attach repositories to unresolved mentions by name, where unambiguous."""
    log = get_logger("main")
    names = loader.repository_names()
    matched = []

    for mention in loader.unresolved_mentions():
        repository_id = resolve_by_name(mention.get("title") or "", names)
        if repository_id:
            matched.append({
                **mention,
                "repository_id": repository_id,
                "match_confidence": NAME_MATCH_CONFIDENCE,
            })

    log.info("name matching", extra={"context": {"matched": len(matched)}})
    return matched


def run(config: Config) -> tuple[int, list[str]]:
    """Run every source. Returns rows loaded and the sources that failed."""
    log = get_logger("main")
    transformer = RepositoryTransformer()
    expectations = RepositoryExpectations()
    loader = PostgreSQLLoader(config)

    loaded = 0
    failed: list[str] = []

    loader.connect()
    try:
        loader.create_tables()

        for extractor_class in EXTRACTORS:
            extractor = extractor_class(config)
            try:
                records = extractor.extract()
                rows = transformer.transform_all(records, extractor.source)
                loaded += loader.load_repositories(rows)
            except Exception as exc:
                # - Broad on purpose. An unexpected shape coming back from one
                #   registry should cost that source, not the other three, and a
                #   daily batch that dies on a KeyError has thrown away a whole
                #   day of everything else. The traceback still goes to the log.
                failed.append(extractor.source)
                log.exception(
                    "source failed",
                    extra={"context": {"source": extractor.source, "error": str(exc)}},
                )
                continue

            # - Outside the block above on purpose. The rows are in the database
            #   by this point, so a problem reporting on them is not a reason to
            #   call the source failed.
            try:
                expectations.validate(loader.rows_for(extractor.source))
            except Exception as exc:
                log.exception(
                    "quality check could not run",
                    extra={"context": {"source": extractor.source, "error": str(exc)}},
                )

        # - Refreshes what the always-on npm listener filters against. Here
        #   specifically: after npm's own collection loop, so a package
        #   found today is filterable today, and before the mention/drain
        #   steps below, which don't depend on this ordering but keep every
        #   real-time-related step grouped together for a reader.
        bus = EventBus(config.redis_realtime_url)
        try:
            # - Bare package names, not raw_repositories' "npm_<name>" row
            #   ids. npm_listener.handle_change looks packages up by the
            #   CouchDB _changes feed's own `id`, which is the bare name -
            #   the prefixed row id would never match anything there.
            npm_names = {row["name"] for row in loader.rows_for("npm")}
            bus.replace_tracked_npm(npm_names)
        except Exception as exc:
            log.exception("could not refresh tracked npm packages", extra={"context": {"error": str(exc)}})

        # - After every repository source, so links have something to match.
        mentions_ran = False
        for extractor_class in MENTION_EXTRACTORS:
            extractor = extractor_class(config)
            try:
                mentions = resolve(loader, extractor.extract())
                loader.load_mentions(mentions)
                mentions_ran = True
            except Exception as exc:
                failed.append(extractor.source)
                log.exception(
                    "source failed",
                    extra={"context": {"source": extractor.source, "error": str(exc)}},
                )

        # - After mentions, so there are candidates, and before the snapshot so
        #   anything found today is in today's history.
        if mentions_ran and config.name_matching:
            try:
                named = name_match(loader)
                if named:
                    loader.load_mentions(named)
            except Exception as exc:
                log.exception("name matching failed", extra={"context": {"error": str(exc)}})

        if mentions_ran:
            try:
                if discover(config, loader, transformer):
                    # - Re-resolve, so the post that caused a discovery attaches
                    #   to the repository it produced.
                    loader.load_mentions(
                        resolve(loader, loader.unresolved_mentions())
                    )
            except Exception as exc:
                log.exception("discovery failed", extra={"context": {"error": str(exc)}})

        # - After discovery (so newly-discovered repositories exist for
        #   real-time HN mentions to resolve against) and before the
        #   snapshot (so anything captured overnight counts in today's
        #   history) - the same ordering reasoning the existing steps above
        #   already follow.
        try:
            drain(bus, transformer, loader)
        except Exception as exc:
            log.exception("realtime drain failed", extra={"context": {"error": str(exc)}})

        # - Last, so it records the state everything else just produced. A
        #   failure here costs the day's history, not the day's data.
        try:
            loader.snapshot(with_mentions=mentions_ran)
        except Exception as exc:
            log.exception("snapshot failed", extra={"context": {"error": str(exc)}})
    finally:
        loader.close()

    return loaded, failed


def main() -> int:
    log = get_logger("main")
    config = Config()
    log.info("pipeline started", extra={"context": {"sources": len(EXTRACTORS)}})

    try:
        loaded, failed = run(config)
    except HecateError as exc:
        # - Nothing ran, so this is a setup problem rather than a source problem:
        #   the database is unreachable or the configuration is wrong.
        log.error("pipeline aborted", extra={"context": {"error": str(exc)}})
        return 1

    log.info(
        "pipeline finished",
        extra={
            "context": {
                "rows_loaded": loaded,
                "sources_failed": failed,
                "sources_run": len(EXTRACTORS) - len(failed),
            }
        },
    )

    # - Some data is better than none, so a partial run still counts as success.
    #   Everything failing does not.
    if failed and len(failed) == len(EXTRACTORS):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
