"""Pipeline entry point.

Runs each source in turn: extract, normalise, load. A source that fails is
logged and counted and the run carries on with the rest, because one API having
a bad afternoon is a poor reason to throw away the other three. The run only
reports failure if every source failed.
"""

import sys

from pipeline.config import Config
from pipeline.exceptions import HecateError
from pipeline.extractors import GitHubExtractor, NpmExtractor
from pipeline.loaders import PostgreSQLLoader
from pipeline.logger import get_logger
from pipeline.transformers import RepositoryTransformer

# - Sources get added here as their extractors land.
EXTRACTORS = (GitHubExtractor, NpmExtractor)


def run(config: Config) -> tuple[int, list[str]]:
    """Run every source. Returns rows loaded and the sources that failed."""
    log = get_logger("main")
    transformer = RepositoryTransformer()
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
