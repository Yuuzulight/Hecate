"""Scoring answers, and keeping the scores.

Every failure this project has had looked green at the time: vacuous dbt
tests, a CronJob collecting one source of four, an exporter frozen behind a
passing liveness probe, a CI watcher reporting success having parsed nothing.
A question-answering service is the easiest thing yet to make look healthy
while producing confident nonsense, because the output reads the same either
way. This is the check that can actually fail.

Two metrics, and the distinction between them is the point:

  faithfulness - is the answer supported by the context it was given?
                 Low means invented. That is a hallucination.
  relevance    - is the answer useful for the question asked?
                 Low means unhelpful. That is not a hallucination, and
                 reporting the two as one number loses the only distinction
                 worth having.

The judge is Claude. RAGAS defaults to OpenAI and will quietly use it for
anything it can, which would mean a second key and a second bill; both metrics
below were chosen partly because they need only an LLM. The obvious relevance
metric, AnswerRelevancy, needs an embeddings model as well - and Anthropic
does not sell one, so it would have pulled OpenAI back in through the side
door no matter what the judge was set to.
"""

import json
import sys
from datetime import datetime, timezone

import anthropic
import psycopg2
from psycopg2.extras import execute_values
from ragas.llms import llm_factory
from ragas.metrics.collections import Faithfulness, RubricsScoreWithoutReference

from pipeline.config import Config
from pipeline.exceptions import ConfigError, LoadError
from pipeline.logger import get_logger

JUDGE_MODEL = "claude-opus-5"

# - The judge writes a short structured verdict, not prose. This is a ceiling
#   against a runaway response, not a target.
JUDGE_MAX_TOKENS = 2048

# ponytail: a flat threshold, and a guess until there are enough scored runs to
# put a number on it. It is deliberately generous - the cost of missing a
# hallucination is higher than the cost of looking twice at an answer that was
# fine. Revisit once the eval table has a few hundred rows to look at.
HALLUCINATION_THRESHOLD = 0.5

# - Judged by the same model, against this and nothing else. Written so that a
#   correct refusal scores well: "the data cannot answer this" is a useful
#   answer, and a rubric that punished it would train the thing this project
#   is trying to avoid.
RELEVANCE_RUBRIC = {
    "score1_description": (
        "Does not address the question at all, or answers a different question."
    ),
    "score2_description": (
        "Touches the question but is mostly padding, hedging, or restatement."
    ),
    "score3_description": (
        "Addresses the question but leaves out something the context could have "
        "supported, or buries the answer in qualifications."
    ),
    "score4_description": (
        "Answers the question directly and is easy to act on, with minor "
        "vagueness or a missing detail."
    ),
    "score5_description": (
        "Answers exactly what was asked, at the right level of detail, with no "
        "padding. Clearly stating that the data cannot support an answer, and "
        "why, counts as a full score here."
    ),
}

# - One tuple, used to build both the INSERT and the assertion that the schema
#   contains it. The original scope had an INSERT naming columns the CREATE
#   TABLE did not have, which is the sort of thing that works in review and
#   fails on the first real write.
EVAL_COLUMNS = (
    "question_id",
    "question",
    "answer",
    "faithfulness",
    "relevance",
    "hallucination",
    "judge_model",
    "answer_model",
    "latency_ms",
    "evaluated_at",
)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS rag_evaluations (
    id            BIGSERIAL PRIMARY KEY,
    -- - Nullable: an ad-hoc question has no id in the fixed set, and those are
    --   still worth recording. Comparing runs filters on it being present.
    question_id   VARCHAR,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    -- - Nullable, and that matters. A metric that failed to run is not a score
    --   of zero; defaulting it would drag every average down and read as a
    --   quality collapse rather than an outage.
    faithfulness  NUMERIC(4, 3),
    relevance     NUMERIC(4, 3),
    hallucination BOOLEAN,
    judge_model   VARCHAR NOT NULL,
    answer_model  VARCHAR NOT NULL,
    latency_ms    INTEGER,
    evaluated_at  TIMESTAMPTZ NOT NULL
);

-- - Comparing a question across runs is the whole reason the set is fixed.
CREATE INDEX IF NOT EXISTS idx_rag_evaluations_question
    ON rag_evaluations (question_id, evaluated_at DESC);
"""


def build_judge(config: Config):
    """The judge, on Claude, with no OpenAI anywhere in the path."""
    if not config.anthropic_api_key:
        raise ConfigError("ANTHROPIC_API_KEY is required to evaluate answers")
    return llm_factory(
        JUDGE_MODEL,
        provider="anthropic",
        client=anthropic.Anthropic(api_key=config.anthropic_api_key),
        max_tokens=JUDGE_MAX_TOKENS,
    )


def build_metrics(judge) -> dict:
    """Faithfulness and relevance, both LLM-only.

    Neither takes an embeddings model. That is the constraint that keeps the
    judge honest: RAGAS' usual relevance metric embeds the answer to compare
    it, and there is no Anthropic embeddings API for it to use.
    """
    return {
        "faithfulness": Faithfulness(llm=judge),
        "relevance": RubricsScoreWithoutReference(llm=judge, rubrics=RELEVANCE_RUBRIC),
    }


def context_passages(context: dict) -> list[str]:
    """The retrieved context as one passage per block.

    Per block rather than one blob, so the judge can say which part of the
    context supports a claim - and so a claim supported by nothing is visible
    as such rather than lost in a wall of JSON.
    """
    return [f"{name}: {json.dumps(value, default=str)}" for name, value in sorted(context.items())]


class Evaluator:
    """Scores an answer and writes the score down."""

    def __init__(self, config: Config, metrics: dict | None = None) -> None:
        self.config = config
        self.log = get_logger("rag.evaluation")
        self.conn = None
        self._metrics = metrics

    @property
    def metrics(self) -> dict:
        # - Built on first use rather than in __init__, so an Evaluator can be
        #   constructed to read scores back without needing a key to do it.
        if self._metrics is None:
            self._metrics = build_metrics(build_judge(self.config))
        return self._metrics

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

    def create_table(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(CREATE_TABLE)
        self.conn.commit()

    def score(self, question: str, answer: str, context: dict) -> dict:
        """Both metrics, with a failed one recorded as unknown rather than zero.

        A metric that could not run is an outage, not a bad answer. Writing a
        zero would be indistinguishable from a hallucination in every average
        drawn from this table afterwards.
        """
        passages = context_passages(context)
        scores: dict[str, float | None] = {}

        for name, metric in self.metrics.items():
            try:
                # - Both take the same three arguments, which is not an
                #   accident: metrics needing anything more than the question,
                #   the answer and the context were ruled out for dragging an
                #   embeddings model in with them.
                result = metric.score(
                    user_input=question, response=answer, retrieved_contexts=passages
                )
                scores[name] = float(result.value)
            except Exception as exc:
                # - Broad on purpose, same reasoning as the extractors: one
                #   metric failing should not cost the other one's score, and
                #   the judge is a network call to somebody else's service.
                self.log.warning(
                    "metric failed",
                    extra={"context": {"metric": name, "error": str(exc)}},
                )
                scores[name] = None

        return scores

    def evaluate(self, question: str, answer: dict, context: dict, question_id=None) -> dict:
        """Score one answered question and return the row that would be stored."""
        scores = self.score(question, answer["answer"], context)
        faithfulness = scores.get("faithfulness")

        # - Faithfulness alone. An unhelpful answer is not an invented one, and
        #   folding relevance in here would label the honest "the data does not
        #   cover this" as a hallucination for being unsatisfying.
        hallucination = None if faithfulness is None else faithfulness < HALLUCINATION_THRESHOLD

        row = {
            "question_id": question_id,
            "question": question,
            "answer": answer["answer"],
            "faithfulness": faithfulness,
            "relevance": scores.get("relevance"),
            "hallucination": hallucination,
            "judge_model": JUDGE_MODEL,
            "answer_model": answer.get("answer_model", "unknown"),
            "latency_ms": answer.get("latency_ms"),
            "evaluated_at": datetime.now(timezone.utc),
        }

        self.log.info(
            "evaluated",
            extra={"context": {
                "question_id": question_id,
                "faithfulness": faithfulness,
                "relevance": row["relevance"],
                "hallucination": hallucination,
            }},
        )
        return row

    def record(self, rows: list[dict]) -> int:
        """Write scored rows. Column order comes from EVAL_COLUMNS, once."""
        if not rows:
            return 0
        if self.conn is None:
            raise LoadError("evaluator is not connected")

        columns = ", ".join(EVAL_COLUMNS)
        values = [tuple(row.get(column) for column in EVAL_COLUMNS) for row in rows]

        with self.conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO rag_evaluations ({columns}) VALUES %s",
                values,
            )
        self.conn.commit()
        return len(rows)


def main() -> int:
    """Answer the fixed question set, score it, and write the scores down."""
    from pipeline.rag.chain import MODEL as ANSWER_MODEL
    from pipeline.rag.chain import AnswerChain
    from pipeline.rag.questions import QUESTIONS
    from pipeline.rag.retriever import WarehouseRetriever

    log = get_logger("rag.evaluation")
    config = Config()
    retriever = WarehouseRetriever(config)
    evaluator = Evaluator(config)

    try:
        retriever.connect()
        evaluator.connect()
        evaluator.create_table()

        chain = AnswerChain(retriever)
        rows = []
        for question in QUESTIONS:
            # - The context the answer actually used, not a second retrieval of
            #   it. Asking again would score the answer against evidence it
            #   never saw if a snapshot landed mid-run.
            answer, context = chain.answer_and_context(question["question"])
            answer["answer_model"] = ANSWER_MODEL
            rows.append(
                evaluator.evaluate(
                    question["question"],
                    answer,
                    context,
                    question_id=question["id"],
                )
            )

        written = evaluator.record(rows)
    except Exception as exc:
        log.exception("evaluation run failed", extra={"context": {"error": str(exc)}})
        return 1
    finally:
        retriever.close()
        evaluator.close()

    scored = [r["faithfulness"] for r in rows if r["faithfulness"] is not None]
    log.info(
        "evaluation run finished",
        extra={"context": {
            "questions": len(rows),
            "written": written,
            "scored": len(scored),
            "hallucinations": sum(1 for r in rows if r["hallucination"]),
        }},
    )

    # - A run where every metric failed wrote rows full of nulls. That is an
    #   outage wearing the shape of a successful evaluation, and it should not
    #   exit clean.
    if not scored:
        log.error("no metric produced a score")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
