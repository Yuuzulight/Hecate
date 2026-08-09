"""Assembling the facts a question needs, before any model sees it.

Structured SQL against the dbt marts rather than a similarity search. That is
not a shortcut - it is what the questions actually want. "What drove Python's
growth" is an aggregate over the growth models; no nearest-neighbour lookup
surfaces it, and the whole corpus is 44k tokens, small enough that the useful
move is picking the right rows rather than finding similar ones.

Every block is bounded. A context that grows with the dataset eventually stops
fitting in a prompt, and that failure arrives quietly, as truncation.
"""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from pipeline.config import Config
from pipeline.exceptions import LoadError
from pipeline.logger import get_logger
from pipeline.matching import name_candidates
from pipeline.rag.cache import ContextCache, context_key
from pipeline.rag.embeddings import EmbeddingStore

# - Enough rows to reason over, few enough to stay well inside a prompt. The
#   growth tables have a long tail of single-star movements below this, which
#   costs tokens and adds nothing.
DEFAULT_LIMIT = 10

# - Past this many named projects it is not a question about projects, and
#   pulling a profile for each stops being context and starts being a dump.
MAX_NAMED_REPOSITORIES = 5

# - Smaller than the rest. Similarity over 55-character descriptions is a weak
#   signal, and ten weak rows next to seven strong blocks invites the model to
#   treat them as evidence.
SIMILAR_LIMIT = 5


def _plain(value):
    """Postgres types the prompt and the cache can both handle."""
    if isinstance(value, Decimal):
        # - Scores and counts, not money. Nothing here needs exact decimals,
        #   and a float reads better in a prompt than "15.00".
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class WarehouseRetriever:
    """Reads the marts and returns bounded, already-summarised context."""

    def __init__(
        self,
        config: Config,
        cache: ContextCache | None = None,
        embeddings: EmbeddingStore | None = None,
    ) -> None:
        self.config = config
        self.log = get_logger("rag.retriever")
        self.conn = None
        self.cache = cache if cache is not None else ContextCache(config.redis_url)
        self.embeddings = embeddings if embeddings is not None else EmbeddingStore(config)
        self._languages: list[str] | None = None
        self._names: dict[str, str] | None = None

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
            raise LoadError(f"could not connect to {self.config.db_name}: {exc}") from exc

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        """Query, and hand back something that survives a JSON round trip.

        Postgres returns Decimal for numeric columns and date objects for
        dates, neither of which json.dumps will take. Converting here rather
        than casting in each query means a cached context and a freshly built
        one are the same shape - otherwise the two differ by type for the same
        question, which surfaces much later and somewhere else.
        """
        if self.conn is None:
            raise LoadError("retriever is not connected")
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [{k: _plain(v) for k, v in row.items()} for row in cur.fetchall()]

    # ---- the shape of the data, which the model needs before the data itself

    def coverage(self) -> dict:
        """How much there is, and how much history - both change the answer.

        The history count matters more than it looks. Seven- and thirty-day
        growth are null until that many days of snapshots exist, and a model
        told only "stars_gained_7d: null" will happily explain it as a project
        that stopped growing. It has to be able to tell no change from not
        enough history to say.
        """
        rows = self._rows(
            """
            SELECT
              (SELECT count(*) FROM raw_repositories)                         AS repositories,
              (SELECT count(*) FROM raw_repositories
                 WHERE origin = 'discovered')                                 AS discovered,
              (SELECT count(*) FROM social_mentions)                          AS mentions,
              (SELECT count(DISTINCT captured_on) FROM repository_snapshots)  AS snapshot_days,
              (SELECT min(captured_on) FROM repository_snapshots)             AS history_from,
              (SELECT max(captured_on) FROM repository_snapshots)             AS history_to
            """
        )
        return rows[0] if rows else {}

    def data_version(self) -> str:
        """What the answer would be built from, as a cache key component."""
        rows = self._rows("SELECT max(captured_on)::text AS version FROM repository_snapshots")
        return (rows[0]["version"] if rows else None) or "empty"

    def sources(self) -> list[dict]:
        """Per-source coverage, so the model cannot compare across a gap.

        npm reports no stars and GitHub no downloads. Without this block a
        question like "which source has the most stars" gets an answer that is
        arithmetically right and meaningless.
        """
        return self._rows(
            """
            SELECT source, repository_count AS repositories,
                   with_stars, with_downloads, with_language
            FROM analytics_marts.dim_sources
            ORDER BY source
            """
        )

    # ---- the blocks that answer questions

    def language_growth(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Stars gained per language per day.

        GitHub and GitLab only. Every PyPI row is Python by definition, and
        counting those measures which registry a project came from rather than
        what anyone chose to write in - the same correction the dashboard's
        language panel needed.
        """
        return self._rows(
            """
            SELECT r.language,
                   count(*)               AS repositories,
                   sum(g.stars_gained_1d) AS stars_gained_1d,
                   sum(g.stars_gained_7d) AS stars_gained_7d,
                   sum(r.stars)           AS stars_total
            FROM analytics_marts.fct_repository_growth g
            JOIN raw_repositories r ON r.id = g.repository_id
            WHERE r.language IS NOT NULL
              AND r.source IN ('github', 'gitlab')
            GROUP BY r.language
            ORDER BY sum(g.stars_gained_1d) DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )

    def fastest_growing(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Biggest one-day star movements.

        The id comes along for citation. A name is what a person reads, but
        names collide across sources - `vite` is three different rows - so an
        answer that cites one cannot be checked against anything in particular.
        """
        return self._rows(
            """
            SELECT r.id, r.name, r.source, r.language, g.stars,
                   g.stars_gained_1d, g.stars_gained_7d,
                   g.stars_growth_pct_7d, g.days_observed
            FROM analytics_marts.fct_repository_growth g
            JOIN raw_repositories r ON r.id = g.repository_id
            WHERE g.stars_gained_1d IS NOT NULL
            ORDER BY g.stars_gained_1d DESC
            LIMIT %s
            """,
            (limit,),
        )

    def most_discussed(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Attention per project, summed across weeks.

        The mart is one row per repository per week. Reading it directly ranks
        weeks rather than projects - a project discussed steadily for a month
        loses to one that had a single loud week, and the same project appears
        several times in one top ten.
        """
        return self._rows(
            """
            SELECT r.id, r.name, r.source,
                   sum(m.posts)         AS posts,
                   sum(m.decayed_score) AS decayed_score,
                   max(m.week_starting) AS latest_week
            FROM analytics_marts.fct_repository_mentions m
            JOIN raw_repositories r ON r.id = m.repository_id
            GROUP BY r.id, r.name, r.source
            ORDER BY sum(m.decayed_score) DESC
            LIMIT %s
            """,
            (limit,),
        )

    def undiscovered(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Projects being talked about that the dataset does not contain yet."""
        return self._rows(
            """
            SELECT owner, project, host, posts, decayed_score
            FROM (
              SELECT CASE WHEN split_part(target_url, '/', 3)
                            IN ('github.com', 'gitlab.com')
                          THEN split_part(target_url, '/', 4) END AS owner,
                     CASE WHEN split_part(target_url, '/', 3)
                            IN ('github.com', 'gitlab.com')
                          THEN split_part(target_url, '/', 5)
                          WHEN split_part(target_url, '/', 3)
                            IN ('npmjs.com', 'pypi.org')
                          THEN nullif(regexp_replace(
                                 target_url,
                                 '^https://[^/]+/(package|project)/', ''), '')
                     END AS project,
                     split_part(target_url, '/', 3) AS host,
                     posts, decayed_score
              FROM analytics_marts.fct_undiscovered_mentions
            ) d
            ORDER BY decayed_score DESC
            LIMIT %s
            """,
            (limit,),
        )

    def stale_but_popular(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Popular, untouched for six months, and not archived.

        Archived is excluded on purpose: maintainers announcing they have
        stopped is a different claim from nobody having touched it.
        """
        return self._rows(
            """
            SELECT id, name, language, stars, days_since_update
            FROM analytics_staging.stg_repositories
            WHERE stars > 5000 AND days_since_update > 180 AND NOT archived
            ORDER BY days_since_update DESC
            LIMIT %s
            """,
            (limit,),
        )

    # ---- resolving what the question is about

    def known_languages(self) -> list[str]:
        if self._languages is None:
            rows = self._rows(
                """
                SELECT language_display
                FROM analytics_marts.dim_languages
                WHERE starred_count > 0
                ORDER BY starred_count DESC
                """
            )
            self._languages = [r["language_display"] for r in rows if r["language_display"]]
        return self._languages

    def known_names(self) -> dict[str, str]:
        """Lowercased project name to itself, for the matcher.

        Deliberately not name to id. Names collide across sources - `vite` is
        both an npm package and a GitHub repository - so a name cannot pick one
        row. Profiles are fetched by name and return every match, which is the
        honest answer to a question that named something ambiguous.
        """
        if self._names is None:
            rows = self._rows("SELECT DISTINCT lower(name) AS name FROM raw_repositories")
            self._names = {r["name"]: r["name"] for r in rows if r["name"]}
        return self._names

    def languages_named_in(self, question: str) -> list[str]:
        """Languages the question mentions, as whole words.

        A controlled vocabulary, unlike project names - "Rust" is on the stop
        list for repository matching because it is an ordinary word in prose,
        but as a language it is exactly what it looks like.

        Word boundaries rather than substring: "go" must not match "going", and
        the possessive in "Python's growth" still has to match Python.
        """
        lowered = question.lower()
        return [
            language
            for language in self.known_languages()
            if re.search(rf"(?<![\w+#-]){re.escape(language.lower())}(?![\w+#-])", lowered)
        ]

    def repositories_named_in(self, question: str) -> list[str]:
        """Project names the question mentions.

        Reuses the mention matcher rather than a second implementation: its
        stop list and word-boundary rules were built against real posts and
        measured against the URL-resolved set. A question is the same problem
        as a post title.
        """
        return sorted(name_candidates(question, self.known_names()))

    def profiles_for(self, names: list[str]) -> list[dict]:
        """Every repository carrying one of these names, across sources.

        Mentions are aggregated before the join. That mart has a row per week,
        so joining it directly returns one copy of the project per week it was
        discussed - a profile that silently multiplies.
        """
        if not names:
            return []
        return self._rows(
            """
            SELECT r.id, r.name, r.source, r.language, r.stars, r.forks,
                   r.downloads, r.description, r.created_at, r.updated_at,
                   r.origin,
                   g.stars_gained_1d, g.stars_gained_7d, g.days_observed,
                   m.posts, m.decayed_score
            FROM raw_repositories r
            LEFT JOIN analytics_marts.fct_repository_growth g
                   ON g.repository_id = r.id
            LEFT JOIN (
                SELECT repository_id,
                       sum(posts)         AS posts,
                       sum(decayed_score) AS decayed_score
                FROM analytics_marts.fct_repository_mentions
                GROUP BY repository_id
            ) m ON m.repository_id = r.id
            WHERE lower(r.name) = ANY(%s)
            ORDER BY r.stars DESC NULLS LAST
            """,
            (names,),
        )

    # ---- similarity, which is an addition rather than a source of fact

    def repositories_for_embedding(self) -> list[dict]:
        """Every repository, as the text the embedding job works from.

        Unbounded, unlike everything above it - this one feeds a batch job
        rather than a prompt, and skipping rows here would leave them
        permanently unsearchable rather than merely absent from one answer.
        """
        return self._rows(
            """
            SELECT id, name, description, language
            FROM raw_repositories
            ORDER BY id
            """
        )

    def similar_repositories(self, question: str, limit: int = SIMILAR_LIMIT) -> list[dict]:
        """Projects whose description reads like the question.

        Labelled as similarity everywhere it surfaces, because that is what it
        is: two projects described in the same words, which is a reason to look
        rather than a fact about either of them.
        """
        scored = self.embeddings.search(question, limit)
        if not scored:
            return []

        rows = self._rows(
            """
            SELECT id, name, source, language, stars, description
            FROM raw_repositories
            WHERE id = ANY(%s)
            """,
            ([repository_id for repository_id, _ in scored],),
        )
        by_id = {row["id"]: row for row in rows}

        # - Rebuilt in score order, and a row that has since been deleted from
        #   the database is dropped rather than carried as a stale name from
        #   Redis.
        return [
            {**by_id[repository_id], "similarity": round(score, 3)}
            for repository_id, score in scored
            if repository_id in by_id
        ]

    # ---- reading the evaluation history

    def recent_evaluations(self, limit: int = 50) -> list[dict]:
        """The most recent scores written by the evaluation run.

        Here rather than on the Evaluator that writes them, and that is not
        tidiness. Importing that module pulls RAGAS in with it - forty-odd
        packages including OpenAI's client - and the service that answers
        questions has no business carrying the thing that grades them. This is
        a read of one table; the retriever is already the service's way of
        reading tables.
        """
        try:
            return self._rows(
                """
                SELECT question_id, question, faithfulness, relevance,
                       hallucination, judge_model, evaluated_at
                FROM rag_evaluations
                ORDER BY evaluated_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        except psycopg2.Error as exc:
            # - The table not existing yet is an ordinary state, but it still
            #   aborts the transaction. Without the rollback every later query
            #   on this connection fails too, and the service looks broken
            #   because nobody has run an evaluation.
            if self.conn is not None:
                self.conn.rollback()
            raise LoadError(f"could not read evaluations: {exc}") from exc

    # ---- what the chain actually calls

    def context_for(self, question: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """Everything relevant to one question, bounded and already aggregated.

        The general blocks go in every time. They are small, and a question
        rarely wants only one of them - "what is trending" needs growth and
        attention together, and the difference between them is often the
        interesting part of the answer.
        """
        key = context_key(question, self.data_version())
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        context: dict[str, Any] = {
            "coverage": self.coverage(),
            "sources": self.sources(),
            "language_growth": self.language_growth(limit),
            "fastest_growing": self.fastest_growing(limit),
            "most_discussed": self.most_discussed(limit),
            "undiscovered": self.undiscovered(limit),
            "stale_but_popular": self.stale_but_popular(limit),
        }

        languages = self.languages_named_in(question)
        if languages:
            context["languages_asked_about"] = languages

        names = self.repositories_named_in(question)
        if names and len(names) <= MAX_NAMED_REPOSITORIES:
            context["repositories_asked_about"] = self.profiles_for(names)

        # - Absent rather than empty when there are no embeddings. A key with
        #   an empty list reads to a model as "we looked and there is nothing
        #   like it", which is a different claim from not having looked.
        similar = self.similar_repositories(question)
        if similar:
            context["similar_by_description"] = similar

        self.log.info(
            "context built",
            extra={"context": {
                "blocks": len(context),
                "rows": sum(len(v) for v in context.values() if isinstance(v, list)),
                "languages": languages,
                "names": names[:MAX_NAMED_REPOSITORIES],
            }},
        )
        self.cache.set(key, context)
        return context
