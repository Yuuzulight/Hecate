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
    NpmExtractor,
    PyPiExtractor,
)
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger
from pipeline.transformer import RepositoryTransformer

EXTRACTORS = (GitHubExtractor, NpmExtractor, PyPiExtractor, GitLabExtractor)

# - Kept apart from the above on purpose. These produce mentions, which are
#   events about a repository rather than repositories, so they skip the
#   transformer entirely and run after everything else - there has to be
#   something stored before a link has anything to resolve against.
MENTION_EXTRACTORS = (HackerNewsExtractor,)


def resolve(loader: PostgreSQLLoader, mentions: list[dict]) -> list[dict]:
    """Attach a repository id to each mention, dropping the ones that miss.

    A story linking a project nobody here tracks is not an error - most of
    Hacker News is about something else - so it is counted and discarded rather
    than raised.
    """
    log = get_logger("main")
    found = loader.resolve_urls({m["target_url"] for m in mentions})

    resolved = []
    for mention in mentions:
        repository_id = found.get(mention["target_url"])
        if repository_id:
            resolved.append({**mention, "repository_id": repository_id})

    log.info(
        "mentions resolved",
        extra={"context": {"matched": len(resolved), "of": len(mentions)}},
    )
    return resolved


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
        for extractor_class in MENTION_EXTRACTORS:
            extractor = extractor_class(config)
            try:
                mentions = resolve(loader, extractor.extract())
                loader.load_mentions(mentions)
            except Exception as exc:
                failed.append(extractor.source)
                log.exception(
                    "source failed",
                    extra={"context": {"source": extractor.source, "error": str(exc)}},
                )
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
