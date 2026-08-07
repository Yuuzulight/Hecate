"""Pipeline entry point.

Runs each source in turn: extract, normalise, load. A source that fails is
logged and counted and the run carries on with the rest, because one API having
a bad afternoon is a poor reason to throw away the other three. The run only
reports failure if every source failed.
"""

import sys

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
from pipeline.matching import resolve_by_name
from pipeline.transformer import RepositoryTransformer

EXTRACTORS = (GitHubExtractor, NpmExtractor, PyPiExtractor, GitLabExtractor)

# - Kept apart from the above on purpose. These produce mentions, which are
#   events about a repository rather than repositories, so they skip the
#   transformer entirely and run after everything else - there has to be
#   something stored before a link has anything to resolve against.
MENTION_EXTRACTORS = (HackerNewsExtractor, LobstersExtractor)


def resolve(loader: PostgreSQLLoader, mentions: list[dict]) -> list[dict]:
    """Attach a repository id to each mention where one is known.

    Mentions that match nothing are kept with a null id rather than dropped.
    Most of Hacker News is about something we don't track, and that is the part
    worth reading rather than the part to discard.
    """
    log = get_logger("main")
    found = loader.resolve_urls({m["target_url"] for m in mentions})

    # - Annotates rather than filters. An unmatched mention is kept with a null
    #   repository_id, because a project being talked about before it is
    #   tracked is the signal, not noise to be thrown away.
    annotated = [
        {**mention, "repository_id": found.get(mention["target_url"])}
        for mention in mentions
    ]

    # - A link is certain. Anything below that came from prose.
    for mention in annotated:
        if mention["repository_id"]:
            mention["match_confidence"] = 1.0

    matched = sum(1 for m in annotated if m["repository_id"])
    log.info(
        "mentions resolved",
        extra={"context": {"matched": matched, "of": len(annotated)}},
    )
    return annotated


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

    github = GitHubExtractor(config)
    found = []
    for url in candidates:
        try:
            raw = github.fetch_by_url(url)
        except HecateError as exc:
            log.warning("discovery lookup failed", extra={"context": {"url": url, "error": str(exc)}})
            continue
        if raw:
            found.append({**raw, "origin": "discovered"})

    rows = transformer.transform_all(found, "github")
    loaded = loader.load_repositories(rows)
    log.info(
        "discovered repositories",
        extra={"context": {"looked_up": len(candidates), "added": loaded}},
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
