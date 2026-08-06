"""Load normalised records into PostgreSQL.

The whole point of this module is that running it twice is the same as running
it once. A run that dies halfway through can just be run again: rows already
written get refreshed in place rather than duplicated, so there's no cleanup
step and no need to work out where the last run stopped.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import execute_values

from pipeline import metrics
from pipeline.config import Config
from pipeline.exceptions import LoadError
from pipeline.logger import get_logger

COLUMNS = (
    "id", "source", "name", "url", "stars", "forks", "language",
    "created_at", "updated_at", "description", "downloads",
    "open_issues_and_prs", "archived", "is_fork", "extracted_at",
)

MENTION_COLUMNS = (
    "id", "platform", "repository_id", "title", "url", "score", "comments",
    "author", "channel", "posted_at", "extracted_at",
)

# - downloads stays nullable rather than defaulting to zero. GitHub and GitLab
#   don't report one at all, and "no such metric" is a different thing from "no
#   downloads" - collapsing them would quietly drag every average down.
#   BIGINT because the busiest packages clear a billion a month.
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS raw_repositories (
    id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    url VARCHAR,
    stars INTEGER DEFAULT 0,
    forks INTEGER DEFAULT 0,
    language VARCHAR,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    description TEXT,
    downloads BIGINT,
    -- - Named for what GitHub actually returns. open_issues_count includes
    --   pull requests, and calling it open_issues would have every reader
    --   assume a backlog figure that overstates itself on active projects.
    open_issues_and_prs INTEGER,
    -- - A formally archived project is definitively abandoned rather than
    --   merely quiet, which no other column can express.
    archived BOOLEAN,
    is_fork BOOLEAN,
    extracted_at TIMESTAMPTZ NOT NULL,
    loaded_at TIMESTAMPTZ DEFAULT NOW()
);

-- - For databases created before these columns existed.
ALTER TABLE raw_repositories ADD COLUMN IF NOT EXISTS downloads BIGINT;
ALTER TABLE raw_repositories ADD COLUMN IF NOT EXISTS open_issues_and_prs INTEGER;
ALTER TABLE raw_repositories ADD COLUMN IF NOT EXISTS archived BOOLEAN;
ALTER TABLE raw_repositories ADD COLUMN IF NOT EXISTS is_fork BOOLEAN;

-- - Posts get their own table. Every row above is an artifact with an identity;
--   a post is an event *about* one, which is a different grain. Putting them
--   together would leave stars, forks and downloads meaningless for post rows
--   and fold a different kind of thing into every cross-source aggregate.
CREATE TABLE IF NOT EXISTS social_mentions (
    id VARCHAR PRIMARY KEY,
    platform VARCHAR NOT NULL,
    repository_id VARCHAR NOT NULL REFERENCES raw_repositories(id) ON DELETE CASCADE,
    title TEXT,
    url VARCHAR,
    score INTEGER,
    comments INTEGER,
    author VARCHAR,
    channel VARCHAR,
    posted_at TIMESTAMPTZ NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL,
    loaded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_social_mentions_repository
    ON social_mentions(repository_id);
CREATE INDEX IF NOT EXISTS idx_social_mentions_posted_at
    ON social_mentions(posted_at);

CREATE INDEX IF NOT EXISTS idx_raw_repositories_source
    ON raw_repositories(source);
CREATE INDEX IF NOT EXISTS idx_raw_repositories_extracted_at
    ON raw_repositories(extracted_at);
"""

# - id, source and created_at describe what the thing is, so they never change.
#   Everything else is a fact about right now and gets refreshed, including name
#   and url, which move when a repository is renamed.
UPSERT = f"""
INSERT INTO raw_repositories ({", ".join(COLUMNS)})
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    url = EXCLUDED.url,
    stars = EXCLUDED.stars,
    forks = EXCLUDED.forks,
    language = EXCLUDED.language,
    updated_at = EXCLUDED.updated_at,
    description = EXCLUDED.description,
    downloads = EXCLUDED.downloads,
    open_issues_and_prs = EXCLUDED.open_issues_and_prs,
    archived = EXCLUDED.archived,
    is_fork = EXCLUDED.is_fork,
    extracted_at = EXCLUDED.extracted_at,
    loaded_at = NOW()
"""


# - Score and comments move as a post ages, so they refresh. What the post is,
#   who wrote it and when, and what it points at, do not.
UPSERT_MENTION = f"""
INSERT INTO social_mentions ({", ".join(MENTION_COLUMNS)})
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title,
    score = EXCLUDED.score,
    comments = EXCLUDED.comments,
    extracted_at = EXCLUDED.extracted_at,
    loaded_at = NOW()
"""


class PostgreSQLLoader:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = get_logger("loaders.postgres")
        self.conn = None

    def connect(self) -> None:
        try:
            self.conn = psycopg2.connect(
                host=self.config.db_host,
                port=self.config.db_port,
                user=self.config.db_user,
                password=self.config.db_password,
                dbname=self.config.db_name,
            )
        except psycopg2.Error as exc:
            metrics.errors.labels(type="database", source="postgres").inc()
            raise LoadError(f"could not connect to {self._target()}: {exc}") from exc
        self.log.info("connected", extra={"context": {"target": self._target()}})

    def create_tables(self) -> None:
        with self.transaction() as cur:
            cur.execute(CREATE_TABLE)
        self.log.info("schema ready")

    def load_repositories(self, rows: list[dict]) -> int:
        """Upsert a batch. Returns how many rows were sent."""
        if not rows:
            return 0

        rows = self._deduplicate(rows)
        values = [tuple(row.get(column) for column in COLUMNS) for row in rows]

        with metrics.load_duration.time():
            with self.transaction() as cur:
                execute_values(cur, UPSERT, values, page_size=len(values))

        metrics.rows_processed.labels(stage="load", source="postgres").inc(len(values))
        self.log.info("loaded", extra={"context": {"rows": len(values)}})
        return len(values)

    def load_mentions(self, mentions: list[dict]) -> int:
        """Upsert posts, dropping any whose repository we don't track.

        The foreign key is the point of this table, so a mention of something
        unknown is reported and discarded rather than inserted orphaned - an
        orphan would count towards attention for a project that isn't in the
        dataset, which is worse than not counting it at all.
        """
        if not mentions:
            return 0

        unique = {m["id"]: m for m in mentions}
        known = self._known_repository_ids({m["repository_id"] for m in unique.values()})

        usable = [m for m in unique.values() if m["repository_id"] in known]
        dropped = len(unique) - len(usable)
        if dropped:
            self.log.warning(
                "mentions dropped, repository not tracked",
                extra={"context": {"dropped": dropped, "of": len(unique)}},
            )
        if not usable:
            return 0

        values = [tuple(m.get(column) for column in MENTION_COLUMNS) for m in usable]
        with self.transaction() as cur:
            execute_values(cur, UPSERT_MENTION, values, page_size=len(values))

        metrics.rows_processed.labels(stage="load", source="mentions").inc(len(values))
        self.log.info("mentions loaded", extra={"context": {"rows": len(values)}})
        return len(values)

    def resolve_urls(self, urls: set[str]) -> dict[str, str]:
        """Map project URLs to the repository ids we store them under.

        Compared lowercased, since the same project gets linked with every
        capitalisation and only one row is going to match.
        """
        if not urls:
            return {}
        with self.transaction() as cur:
            cur.execute(
                "SELECT lower(url), id FROM raw_repositories WHERE lower(url) = ANY(%s)",
                (list(urls),),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def _known_repository_ids(self, ids: set[str]) -> set[str]:
        if not ids:
            return set()
        with self.transaction() as cur:
            cur.execute(
                "SELECT id FROM raw_repositories WHERE id = ANY(%s)", (list(ids),)
            )
            return {row[0] for row in cur.fetchall()}

    def rows_for(self, source: str) -> list[dict]:
        """Read back what is stored for one source.

        The quality checks run on this rather than on the batch that was sent,
        so a column mapped to the wrong place, a value truncated on the way in,
        or a batch that never committed shows up as bad data instead of being
        invisible.
        """
        columns = ", ".join(COLUMNS)
        with self.transaction() as cur:
            cur.execute(
                f"SELECT {columns} FROM raw_repositories WHERE source = %s", (source,)
            )
            return [dict(zip(COLUMNS, row)) for row in cur.fetchall()]

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            self.log.info("connection closed")

    def _deduplicate(self, rows: list[dict]) -> list[dict]:
        """Keep the last record per id.

        Postgres refuses an ON CONFLICT DO UPDATE that would touch the same row
        twice in one statement, and paging over a source that's being written to
        can genuinely hand back the same repository on two pages.
        """
        unique = {row["id"]: row for row in rows}
        dropped = len(rows) - len(unique)
        if dropped:
            self.log.warning("duplicate ids in batch", extra={"context": {"dropped": dropped}})
        return list(unique.values())

    @contextmanager
    def transaction(self) -> Iterator["psycopg2.extensions.cursor"]:
        """A cursor inside a transaction.

        psycopg2 connections already commit on a clean exit and roll back on an
        exception, and cursors already close themselves, so all this adds is
        turning a driver error into a LoadError and counting it.
        """
        if self.conn is None:
            raise LoadError("not connected - call connect() first")
        try:
            with self.conn, self.conn.cursor() as cur:
                yield cur
        except psycopg2.Error as exc:
            metrics.errors.labels(type="database", source="postgres").inc()
            self.log.error("transaction rolled back", extra={"context": {"error": str(exc)}})
            raise LoadError(f"database error: {exc}") from exc

    def _target(self) -> str:
        return f"{self.config.db_host}:{self.config.db_port}/{self.config.db_name}"
