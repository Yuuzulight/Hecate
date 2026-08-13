"""Evaluation: the schema, the labelling, and who is doing the judging.

The judge is stubbed, so nothing here scores anything for real - whether Claude
actually gives a low faithfulness score to an invented answer is a property of
Claude and needs a live run. What these check is everything around it: that a
low score is labelled a hallucination and a merely unhelpful answer is not,
that a metric which failed is recorded as unknown rather than zero, and that
the columns the INSERT names are columns the table has.

That last one is not hypothetical. The original scope had an INSERT naming
columns the CREATE TABLE did not declare, which reviews fine and fails on the
first real write.
"""

import inspect
import os

import pytest

from pipeline.config import Config
from pipeline.exceptions import ConfigError
from pipeline.rag import evaluation
from pipeline.rag.evaluation import (
    CREATE_TABLE,
    EVAL_COLUMNS,
    HALLUCINATION_THRESHOLD,
    RELEVANCE_RUBRIC,
    Evaluator,
    build_judge,
    build_metrics,
    context_passages,
)
from pipeline.rag.questions import QUESTIONS

CONTEXT = {
    "coverage": {"repositories": 2013, "snapshot_days": 3},
    "fastest_growing": [{"id": "github_1", "name": "skills", "stars_gained_1d": 207}],
}

ANSWER = {"answer": "skills gained 207 stars.", "latency_ms": 812, "answer_model": "claude-opus-5"}


class StubMetric:
    """Returns a fixed score, or raises to stand in for the judge being down."""

    def __init__(self, value=None, fail=False):
        self.value = value
        self.fail = fail
        self.calls = []

    def score(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("judge unreachable")
        return type("Result", (), {"value": self.value})()


def evaluator_with(faithfulness=0.9, relevance=0.8, fail=None):
    metrics = {
        "faithfulness": StubMetric(faithfulness, fail=(fail == "faithfulness")),
        "relevance": StubMetric(relevance, fail=(fail == "relevance")),
    }
    config = Config()
    return Evaluator(config, metrics=metrics)


@pytest.fixture(autouse=True)
def _config_env(monkeypatch):
    """A password for Config(), without clobbering a real one.

    Setting it unconditionally is what broke this file in CI: the unit tests
    here never connect, so any string does, but the integration tests further
    down do connect - and a hardcoded password meant they authenticated as
    nobody. It passed locally only because the database on this machine
    accepts any password, which is a green that proves rather less than it
    appears to.
    """
    if not os.environ.get("DB_PASSWORD"):
        monkeypatch.setenv("DB_PASSWORD", "secret")


# ---- the schema and the INSERT agreeing


def test_every_inserted_column_exists_in_the_table():
    for column in EVAL_COLUMNS:
        assert column in CREATE_TABLE, f"{column} is inserted but not declared"


def test_the_insert_uses_the_column_tuple_in_order(monkeypatch):
    captured = {}

    def fake_execute_values(cur, sql, values):
        captured["sql"] = sql
        captured["values"] = values

    monkeypatch.setattr(evaluation, "execute_values", fake_execute_values)

    ev = evaluator_with()
    ev.conn = FakeConnection()
    row = ev.evaluate("what grew", ANSWER, CONTEXT, question_id="growth-fastest")
    ev.record([row])

    assert ", ".join(EVAL_COLUMNS) in captured["sql"]
    # - One value per column, in the same order, so a column added to the tuple
    #   without a value silently becomes None rather than shifting every field
    #   one place to the left.
    assert len(captured["values"][0]) == len(EVAL_COLUMNS)
    assert captured["values"][0][EVAL_COLUMNS.index("question")] == "what grew"


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def test_recording_nothing_writes_nothing():
    ev = evaluator_with()
    ev.conn = FakeConnection()
    assert ev.record([]) == 0


# ---- hallucination means faithfulness, and only faithfulness


def test_an_unfaithful_answer_is_labelled_a_hallucination():
    ev = evaluator_with(faithfulness=0.1, relevance=0.9)
    row = ev.evaluate("what grew", ANSWER, CONTEXT)
    assert row["faithfulness"] == pytest.approx(0.1)
    assert row["hallucination"] is True


def test_an_unhelpful_but_faithful_answer_is_not_a_hallucination():
    # - The distinction the whole module exists for. A dull, hedging, useless
    #   answer that invents nothing is a quality problem, not a safety one, and
    #   collapsing them into one number loses the only signal worth acting on.
    ev = evaluator_with(faithfulness=0.95, relevance=0.1)
    row = ev.evaluate("what grew", ANSWER, CONTEXT)
    assert row["relevance"] == pytest.approx(0.1)
    assert row["hallucination"] is False


def test_the_threshold_is_the_boundary():
    below = evaluator_with(faithfulness=HALLUCINATION_THRESHOLD - 0.01)
    at = evaluator_with(faithfulness=HALLUCINATION_THRESHOLD)
    assert below.evaluate("q", ANSWER, CONTEXT)["hallucination"] is True
    assert at.evaluate("q", ANSWER, CONTEXT)["hallucination"] is False


# ---- a metric that could not run is not a score of zero


def test_a_failed_metric_is_unknown_rather_than_zero():
    ev = evaluator_with(fail="faithfulness")
    row = ev.evaluate("what grew", ANSWER, CONTEXT)
    assert row["faithfulness"] is None
    # - Not False. "We could not check" is not "we checked and it was fine".
    assert row["hallucination"] is None


def test_one_failed_metric_does_not_cost_the_other():
    ev = evaluator_with(relevance=0.8, fail="faithfulness")
    row = ev.evaluate("what grew", ANSWER, CONTEXT)
    assert row["relevance"] == pytest.approx(0.8)


def test_a_failed_metric_is_logged(caplog):
    ev = evaluator_with(fail="relevance")
    with caplog.at_level("WARNING"):
        ev.evaluate("what grew", ANSWER, CONTEXT)
    assert "metric failed" in [record.message for record in caplog.records]


# ---- what the judge is given


def test_the_context_is_passed_as_one_passage_per_block():
    passages = context_passages(CONTEXT)
    assert len(passages) == 2
    assert passages[0].startswith("coverage:")
    assert passages[1].startswith("fastest_growing:")


def test_both_metrics_see_the_question_the_answer_and_the_context():
    ev = evaluator_with()
    ev.evaluate("what grew", ANSWER, CONTEXT)
    for metric in ev.metrics.values():
        call = metric.calls[0]
        assert call["user_input"] == "what grew"
        assert call["response"] == ANSWER["answer"]
        assert len(call["retrieved_contexts"]) == 2


class SlowStubMetric:
    """Sleeps before returning, to prove the two metrics run concurrently
    rather than one after the other - the two real LLM round trips this
    stands in for are exactly why it matters."""

    SLEEP_SECONDS = 0.2

    def __init__(self, value):
        self.value = value

    def score(self, **kwargs):
        import time

        time.sleep(self.SLEEP_SECONDS)
        return type("Result", (), {"value": self.value})()


def test_the_two_metrics_run_concurrently_not_sequentially():
    import time

    metrics = {
        "faithfulness": SlowStubMetric(0.9),
        "relevance": SlowStubMetric(0.8),
    }
    ev = Evaluator(Config(), metrics=metrics)

    started = time.perf_counter()
    scores = ev.score("what grew", ANSWER["answer"], CONTEXT)
    elapsed = time.perf_counter() - started

    assert scores == {"faithfulness": 0.9, "relevance": 0.8}
    # - Run sequentially this would take >= 2 * SLEEP_SECONDS. A generous
    #   margin over one sleep rather than a tight one, so this isn't flaky
    #   under a loaded CI runner - the two-sleeps-worth sequential case
    #   would still fail it by a wide margin.
    assert elapsed < SlowStubMetric.SLEEP_SECONDS * 1.5


def test_the_row_carries_both_models():
    ev = evaluator_with()
    row = ev.evaluate("what grew", ANSWER, CONTEXT)
    from pipeline.rag import providers

    assert row["judge_model"] == providers.spec_for(ev.config).model
    assert row["answer_model"] == "claude-opus-5"


# ---- the judge, wired to whichever provider is configured
#
# The real proof is the absence of an OpenAI request on a live run. These are
# the offline half: closing RAGAS's one side door back to OpenAI (an
# embeddings-taking relevance metric), which matters regardless of which
# provider judges.


@pytest.mark.parametrize(
    "provider, env_name, key, model, client_module_prefix",
    [
        ("anthropic", "ANTHROPIC_API_KEY", "sk-ant-test", "claude-opus-5", "anthropic"),
        ("gemini", "GOOGLE_API_KEY", "test-key", "gemini-3.5-flash", "google"),
        ("openai", "OPENAI_API_KEY", "sk-test", "gpt-5.1", "openai"),
    ],
)
def test_the_judge_is_wired_to_the_configured_provider(
    monkeypatch, provider, env_name, key, model, client_module_prefix
):
    monkeypatch.setenv("RAG_PROVIDER", provider)
    monkeypatch.setenv(env_name, key)
    judge = build_judge(Config())
    # - The brief's original guess here was ragas.llms.LangchainLLMWrapper
    #   wrapping providers.build_chat_model's LangChain model, asserted via a
    #   presumed `.langchain_llm` attribute. A live smoke test proved that
    #   shape wrong: ragas 0.4.3's collections metrics (what build_metrics
    #   actually uses) reject a LangchainLLMWrapper outright with "Collections
    #   metrics only support modern InstructorLLM" - see task-4-report.md.
    #   build_judge was adjusted to build an InstructorLLM, but not via
    #   ragas.llms.llm_factory - that was tried first and found broken for
    #   Gemini specifically (a sync/async mismatch; see evaluation.py's module
    #   docstring). It uses evaluation.py's own _instructor_client helper over
    #   a raw provider SDK client instead, which is what these assertions
    #   check: the raw client underneath is the configured provider's own SDK,
    #   not some other provider's leaking in.
    assert judge.provider == provider
    assert judge.model == model
    assert type(judge.client.client).__module__.startswith(client_module_prefix)


def test_the_judge_defaults_to_gemini_like_everything_else(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("RAG_PROVIDER", raising=False)
    judge = build_judge(Config())
    assert judge.provider == "gemini"


def test_no_key_is_a_clear_error_rather_than_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("RAG_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_judge(Config())


def test_neither_metric_takes_an_embeddings_model(monkeypatch):
    # - RAGAS' usual relevance metric embeds the answer to compare it, and
    #   Anthropic sells no embeddings API - so choosing it would have pulled
    #   OpenAI back in through the side door regardless of which provider the
    #   judge is set to. Both metrics here are LLM-only, and this is what
    #   keeps them that way, independent of provider.
    monkeypatch.setenv("RAG_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    for metric in build_metrics(build_judge(Config())).values():
        parameters = inspect.signature(type(metric).__init__).parameters
        assert "embeddings" not in parameters, f"{type(metric).__name__} would need embeddings"


def test_the_relevance_rubric_rewards_a_correct_refusal():
    # - Otherwise the rubric trains away the behaviour the chain is built for:
    #   saying the data cannot answer, rather than answering anyway.
    assert "cannot support an answer" in RELEVANCE_RUBRIC["score5_description"]


# ---- the fixed question set


def test_question_ids_are_unique():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids)), "scores are compared across runs by id"


def test_every_question_declares_the_fields_a_run_needs():
    for question in QUESTIONS:
        assert set(question) == {"id", "category", "question", "expects_refusal"}
        assert question["question"].strip()


def test_the_set_contains_questions_the_data_cannot_answer():
    # - A set the data answers cleanly would score well and prove nothing. The
    #   failures worth catching are the ones where the honest answer is that
    #   there is no answer.
    assert sum(1 for q in QUESTIONS if q["expects_refusal"]) >= 4


# ---- the run itself


def test_a_run_where_every_metric_failed_does_not_exit_clean(monkeypatch, caplog):
    # - The failure this project keeps finding, in its newest costume: twelve
    #   rows written, no scores in any of them, and an exit code saying the
    #   evaluation succeeded.
    from pipeline.rag import chain as chain_module
    from pipeline.rag import retriever as retriever_module

    class StubRetriever:
        def __init__(self, config):
            pass

        def connect(self):
            pass

        def close(self):
            pass

    class StubChain:
        def __init__(self, retriever, config):
            pass

        def answer_and_context(self, question):
            return {"answer": "something", "latency_ms": 1, "answer_model": "stub"}, CONTEXT

    class StubEvaluator(Evaluator):
        def __init__(self, config):
            super().__init__(config, metrics={
                "faithfulness": StubMetric(fail=True),
                "relevance": StubMetric(fail=True),
            })

        def connect(self):
            pass

        def close(self):
            pass

        def create_table(self):
            pass

        def record(self, rows):
            return len(rows)

    monkeypatch.setattr(retriever_module, "WarehouseRetriever", StubRetriever)
    monkeypatch.setattr(chain_module, "AnswerChain", StubChain)
    monkeypatch.setattr(evaluation, "Evaluator", StubEvaluator)

    with caplog.at_level("INFO"):
        assert evaluation.main() == 1

    messages = [record.message for record in caplog.records]
    # - It has to fail for the right reason. main() also returns 1 from its
    #   catch-all, so the exit code alone would pass this test with the run
    #   having crashed on line one instead.
    assert "evaluation run finished" in messages
    assert "no metric produced a score" in messages
    assert "evaluation run failed" not in messages


# ---- against a real database
#
# Whether the INSERT and the schema agree is not something a fake cursor can
# tell you: it accepts any SQL. This writes a row and reads it back.

OFF = ("", "0", "false", "no", "off")
TEST_SCHEMA = "hecate_test"


def wanted() -> bool:
    return os.environ.get("HECATE_INTEGRATION", "").strip().lower() not in OFF


@pytest.fixture
def live_evaluator():
    if not wanted():
        pytest.skip("set HECATE_INTEGRATION=1 to run against a real database")

    ev = evaluator_with()
    ev.connect()
    with ev.conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
    ev.conn.commit()

    ev.create_table()
    with ev.conn.cursor() as cur:
        cur.execute("TRUNCATE rag_evaluations")
    ev.conn.commit()

    yield ev
    ev.close()


@pytest.mark.integration
def test_a_scored_answer_writes_one_row_the_table_accepts(live_evaluator):
    row = live_evaluator.evaluate("what grew", ANSWER, CONTEXT, question_id="growth-fastest")
    assert live_evaluator.record([row]) == 1

    with live_evaluator.conn.cursor() as cur:
        cur.execute(
            "SELECT question_id, question, faithfulness, relevance, hallucination,"
            " judge_model, answer_model, latency_ms FROM rag_evaluations"
        )
        stored = cur.fetchall()

    assert len(stored) == 1
    assert stored[0][0] == "growth-fastest"
    assert stored[0][1] == "what grew"
    assert float(stored[0][2]) == pytest.approx(0.9)
    assert stored[0][4] is False
    assert stored[0][7] == 812


@pytest.mark.integration
def test_an_unscored_answer_stores_nulls_rather_than_zeros(live_evaluator):
    # - The column has to accept NULL. A NOT NULL here would turn an outage
    #   into a write failure, or worse, into a zero.
    live_evaluator._metrics = {"faithfulness": StubMetric(fail=True), "relevance": StubMetric(fail=True)}
    row = live_evaluator.evaluate("what grew", ANSWER, CONTEXT)
    live_evaluator.record([row])

    with live_evaluator.conn.cursor() as cur:
        cur.execute("SELECT faithfulness, relevance, hallucination FROM rag_evaluations")
        stored = cur.fetchone()

    assert stored == (None, None, None)


@pytest.mark.integration
def test_creating_the_table_twice_is_fine(live_evaluator):
    live_evaluator.create_table()
