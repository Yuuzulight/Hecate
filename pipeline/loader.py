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
    "open_issues_and_prs", "archived", "is_fork", "origin", "extracted_at",
)

MENTION_COLUMNS = (
    "id", "platform", "repository_id", "target_url", "title", "url", "score",
    "comments", "author", "channel", "match_confidence", "posted_at",
    "extracted_at",
)

FORECAST_COLUMNS = (
    "repository_id", "forecast_date", "horizon_days", "days_observed",
    "baseline_stars", "predicted_stars_p10", "predicted_stars_p50",
    "predicted_stars_p90", "suppressed_reason", "model_version", "generated_at",
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

-- - How a row got here. Null means seeded by one of the ranked source queries;
--   'discovered' means it was linked from a post and fetched afterwards. Worth
--   being able to separate: a discovered set is selected by having been talked
--   about, which is a survivorship bias on any conclusion drawn from it.
ALTER TABLE raw_repositories ADD COLUMN IF NOT EXISTS origin VARCHAR;

-- - Posts get their own table. Every row above is an artifact with an identity;
--   a post is an event *about* one, which is a different grain. Putting them
--   together would leave stars, forks and downloads meaningless for post rows
--   and fold a different kind of thing into every cross-source aggregate.
CREATE TABLE IF NOT EXISTS social_mentions (
    id VARCHAR PRIMARY KEY,
    platform VARCHAR NOT NULL,
    -- - Nullable. A post about a project we do not track is the interesting
    --   case, not the discardable one: every source is seeded by cumulative
    --   popularity, so anything trending now is exactly what the seeding
    --   filtered out. The marts drop unresolved rows where a repository is
    --   required, so scoring is unaffected.
    repository_id VARCHAR REFERENCES raw_repositories(id) ON DELETE CASCADE,
    -- - What the post pointed at, kept whether or not it resolved, so it can
    --   resolve later once the repository exists.
    target_url VARCHAR,
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

-- - For databases created while mentions had to resolve.
ALTER TABLE social_mentions ALTER COLUMN repository_id DROP NOT NULL;
ALTER TABLE social_mentions ADD COLUMN IF NOT EXISTS target_url VARCHAR;

-- - How much to trust the link between post and repository. 1.0 is a URL,
--   which is certain. Anything lower came from matching a name in prose and
--   should be filterable out of any figure that matters.
ALTER TABLE social_mentions ADD COLUMN IF NOT EXISTS match_confidence NUMERIC;

CREATE INDEX IF NOT EXISTS idx_social_mentions_target_url
    ON social_mentions(target_url) WHERE repository_id IS NULL;

-- - Everything above describes now, and upserting in place means yesterday is
--   gone. This is the only table that remembers, which is what growth rate and
--   any decayed mention score need to exist at all.
--
--   Keyed on the day rather than the moment: a re-run replaces that day's row
--   instead of appending, so the same idempotency guarantee holds here.
CREATE TABLE IF NOT EXISTS repository_snapshots (
    repository_id VARCHAR NOT NULL REFERENCES raw_repositories(id) ON DELETE CASCADE,
    captured_on DATE NOT NULL,
    stars INTEGER,
    forks INTEGER,
    downloads BIGINT,
    -- - Null means the mention extractors did not run. Zero means they ran and
    --   found nothing. Averaging those together would be the same mistake as
    --   treating an absent download figure as no downloads.
    mention_count INTEGER,
    PRIMARY KEY (repository_id, captured_on)
);

CREATE INDEX IF NOT EXISTS idx_repository_snapshots_captured_on
    ON repository_snapshots(captured_on);

-- - One row per repository per forecast horizon per day. Suppressed rows
--   are written, not omitted - "not enough history yet" is a queryable
--   fact, the same "NULL, not absent" discipline repository_snapshots and
--   fct_repository_growth already follow.
CREATE TABLE IF NOT EXISTS repository_forecasts (
    repository_id VARCHAR NOT NULL REFERENCES raw_repositories(id) ON DELETE CASCADE,
    forecast_date DATE NOT NULL,
    horizon_days INTEGER NOT NULL,
    days_observed INTEGER NOT NULL,
    -- - Stars as of forecast_date, not "current stars" - so a later query
    --   computing predicted_stars_p50 - baseline_stars always anchors to
    --   what the forecast was actually made against, not whatever today's
    --   count happens to be.
    baseline_stars INTEGER NOT NULL,
    predicted_stars_p10 INTEGER,
    predicted_stars_p50 INTEGER,
    predicted_stars_p90 INTEGER,
    suppressed_reason VARCHAR,
    model_version VARCHAR NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (repository_id, forecast_date, horizon_days)
);

CREATE INDEX IF NOT EXISTS idx_repository_forecasts_forecast_date
    ON repository_forecasts(forecast_date);

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
    -- - How it first arrived, so a later seeded run does not erase the fact
    --   that a project was found by being talked about.
    origin = coalesce(raw_repositories.origin, EXCLUDED.origin),
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
    -- - So a mention resolves retrospectively once its repository is added.
    repository_id = coalesce(EXCLUDED.repository_id, social_mentions.repository_id),
    score = EXCLUDED.score,
    comments = EXCLUDED.comments,
    extracted_at = EXCLUDED.extracted_at,
    loaded_at = NOW()
"""

UPSERT_FORECAST = f"""
INSERT INTO repository_forecasts ({", ".join(FORECAST_COLUMNS)})
VALUES %s
ON CONFLICT (repository_id, forecast_date, horizon_days) DO UPDATE SET
    days_observed = EXCLUDED.days_observed,
    baseline_stars = EXCLUDED.baseline_stars,
    predicted_stars_p10 = EXCLUDED.predicted_stars_p10,
    predicted_stars_p50 = EXCLUDED.predicted_stars_p50,
    predicted_stars_p90 = EXCLUDED.predicted_stars_p90,
    suppressed_reason = EXCLUDED.suppressed_reason,
    model_version = EXCLUDED.model_version,
    generated_at = EXCLUDED.generated_at
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
        """Upsert posts, resolved or not.

        A mention pointing at a project we don't track is kept with a null
        repository_id. It's the more interesting half: every source is seeded by
        cumulative popularity, so a project being discussed before it has the
        stars to be seeded is precisely what that seeding excludes.

        A repository_id that doesn't exist is cleared rather than rejected, so
        a stale id can't fail the batch - the URL is kept either way and can
        resolve later.
        """
        if not mentions:
            return 0

        unique = {m["id"]: m for m in mentions}
        claimed = {m["repository_id"] for m in unique.values() if m.get("repository_id")}
        known = self._known_repository_ids(claimed)

        rows = []
        for mention in unique.values():
            repository_id = mention.get("repository_id")
            rows.append(
                mention if repository_id in known else {**mention, "repository_id": None}
            )

        resolved = sum(1 for r in rows if r["repository_id"])
        self.log.info(
            "mentions stored",
            extra={"context": {"resolved": resolved, "unresolved": len(rows) - resolved}},
        )

        values = [tuple(m.get(column) for column in MENTION_COLUMNS) for m in rows]
        with self.transaction() as cur:
            execute_values(cur, UPSERT_MENTION, values, page_size=len(values))

        metrics.rows_processed.labels(stage="load", source="mentions").inc(len(values))
        self.log.info("mentions loaded", extra={"context": {"rows": len(values)}})
        return len(values)

    def snapshot(self, with_mentions: bool) -> int:
        """Record today's figures for every stored repository.

        Derived from what is already in the database rather than from the batch
        just processed, so a source that failed this run keeps yesterday's row
        rather than vanishing from the series.

        `with_mentions` says whether the mention extractors ran. When they did
        not, the count is left null rather than written as zero - otherwise a
        run with mentions switched off would look like a day nobody posted.

        mention_count is cumulative, every post ever seen for that repository,
        not posts on that day. The daily figure is the difference between two
        snapshots, which is the whole reason this table exists.
        """
        # - Not interpolated user input: a fixed choice between two literals.
        mention_expression = (
            "coalesce(m.mentions, 0)" if with_mentions else "NULL::int"
        )
        with self.transaction() as cur:
            cur.execute(f"""
                INSERT INTO repository_snapshots
                    (repository_id, captured_on, stars, forks, downloads, mention_count)
                SELECT r.id, current_date, r.stars, r.forks, r.downloads,
                       {mention_expression}
                FROM raw_repositories r
                LEFT JOIN (
                    SELECT repository_id, count(*) AS mentions
                    FROM social_mentions GROUP BY repository_id
                ) m ON m.repository_id = r.id
                ON CONFLICT (repository_id, captured_on) DO UPDATE SET
                    stars = EXCLUDED.stars,
                    forks = EXCLUDED.forks,
                    downloads = EXCLUDED.downloads,
                    mention_count = EXCLUDED.mention_count
            """)
            written = cur.rowcount

        self.log.info("snapshot written", extra={"context": {"rows": written}})
        return written

    def repository_names(self) -> dict[str, str]:
        """Lowercased project name to repository id, for name matching.

        Names shared by more than one repository are dropped entirely: if
        `parser` exists under two owners, no post naming it can be attributed
        to either, and picking one would be a coin toss dressed as data.
        """
        with self.transaction() as cur:
            cur.execute("""
                SELECT lower(name), min(id), count(*)
                FROM raw_repositories
                WHERE name IS NOT NULL
                GROUP BY lower(name)
                HAVING count(*) = 1
            """)
            return {row[0]: row[1] for row in cur.fetchall()}

    def unresolved_mentions(self) -> list[dict]:
        """Stored mentions still waiting on a repository, for a second pass."""
        with self.transaction() as cur:
            cur.execute(
                f"SELECT {', '.join(MENTION_COLUMNS)} FROM social_mentions "
                "WHERE repository_id IS NULL AND target_url IS NOT NULL"
            )
            return [dict(zip(MENTION_COLUMNS, row)) for row in cur.fetchall()]

    def discovery_candidates(self, minimum_score: int, limit: int) -> list[str]:
        """Project URLs being discussed that we don't track, best first.

        Bounded by `limit` because this drives one API call each, and an
        unbounded pass over every URL Hacker News mentions is a rate-limit
        incident rather than a feature.
        """
        with self.transaction() as cur:
            cur.execute(
                """
                SELECT target_url, sum(score) AS attention
                FROM social_mentions
                WHERE repository_id IS NULL AND target_url IS NOT NULL
                GROUP BY target_url
                HAVING sum(score) >= %s
                ORDER BY attention DESC
                LIMIT %s
                """,
                (minimum_score, limit),
            )
            return [row[0] for row in cur.fetchall()]

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

    def top_forecast_targets(self, n: int) -> list[dict]:
        """Repositories ranked by yesterday-to-today star gain, best first.

        Computed directly from repository_snapshots rather than the
        fct_repository_growth dbt mart, so this job's own repository
        selection does not depend on hecate-dbt having succeeded first -
        the same reasoning that already has this file reading
        raw_repositories directly elsewhere.

        A repository with only one snapshot has no previous day to diff
        against and comes back with stars_gained_1d = NULL rather than
        being excluded - it's a real candidate the confidence gate (not
        this ranking) will decide whether to suppress.
        """
        with self.transaction() as cur:
            cur.execute(
                """
                WITH ranked AS (
                    SELECT repository_id, stars,
                           stars - LAG(stars) OVER (
                               PARTITION BY repository_id ORDER BY captured_on
                           ) AS gained_1d,
                           ROW_NUMBER() OVER (
                               PARTITION BY repository_id ORDER BY captured_on DESC
                           ) AS rn
                    FROM repository_snapshots
                )
                SELECT r.id, r.name, ranked.stars, ranked.gained_1d
                FROM ranked
                JOIN raw_repositories r ON r.id = ranked.repository_id
                WHERE ranked.rn = 1
                ORDER BY ranked.gained_1d ASC NULLS LAST
                LIMIT %s
                """,
                (n,),
            )
            return [
                {"id": row[0], "name": row[1], "stars": row[2], "stars_gained_1d": row[3]}
                for row in cur.fetchall()
            ]

    def snapshot_series(self, repository_id: str) -> list[tuple]:
        """One repository's full daily star history, oldest first.

        The context TimesFM forecasts from. Empty for a repository with no
        snapshots yet rather than an error - the caller's confidence gate
        suppresses a series this short on its own.
        """
        with self.transaction() as cur:
            cur.execute(
                "SELECT captured_on, stars FROM repository_snapshots "
                "WHERE repository_id = %s ORDER BY captured_on",
                (repository_id,),
            )
            return cur.fetchall()

    def write_forecasts(self, rows: list[dict]) -> int:
        """Upsert a batch of forecast rows. Returns how many were sent."""
        if not rows:
            return 0
        values = [tuple(row.get(column) for column in FORECAST_COLUMNS) for row in rows]
        with self.transaction() as cur:
            execute_values(cur, UPSERT_FORECAST, values, page_size=len(values))
        self.log.info("forecasts written", extra={"context": {"rows": len(values)}})
        return len(values)

    def forecast_rows_for(self, forecast_date) -> list[dict]:
        """Read back what's stored for one date - the row-count sanity
        check runs on this rather than trusting the job's own exit code."""
        columns = ", ".join(FORECAST_COLUMNS)
        with self.transaction() as cur:
            cur.execute(
                f"SELECT {columns} FROM repository_forecasts WHERE forecast_date = %s",
                (forecast_date,),
            )
            return [dict(zip(FORECAST_COLUMNS, row)) for row in cur.fetchall()]

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
