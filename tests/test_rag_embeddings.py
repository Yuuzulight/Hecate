"""Embeddings: what gets sent, what gets skipped, and what happens without them.

Every OpenAI call here goes through a stand-in, so these run without a key and
without spending anything. That means the shape of a real response, the real
token counts, and whether 256 dimensions actually separate these descriptions
are all unverified - they need one live run against the API.

What a stub *can* answer is the part that costs money if it is wrong: that the
second run embeds nothing, that a changed row embeds exactly itself, and that
a vector never gets attached to the wrong repository.
"""

import json

import pytest
import redis

from pipeline.config import Config
from pipeline.exceptions import EmbeddingError
from pipeline.rag.cache import ContextCache
from pipeline.rag.embeddings import (
    COST_PER_MILLION_TOKENS,
    HASH_KEY,
    VECTOR_KEY,
    EmbeddingStore,
    _normalise,
    embedding_text,
)
from pipeline.rag.retriever import WarehouseRetriever

TOKENS_PER_INPUT = 7


def vector_for(text: str) -> list[float]:
    """A deterministic stand-in for an embedding.

    Four buckets rather than 256: the code never checks how long a vector is
    on the way in, only that a stored one matches the query's length, so the
    width is free and a short one is readable in a failure message.
    """
    vector = [0.0] * 4
    for character in text.lower():
        vector[ord(character) % 4] += 1.0
    return vector


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeOpenAI:
    """Records what it was asked to embed, and answers deterministically."""

    def __init__(self, status: int = 200, reverse: bool = False, drop: int = 0) -> None:
        self.status = status
        self.reverse = reverse
        self.drop = drop
        self.batches: list[list[str]] = []

    def post(self, url, headers=None, json=None, timeout=None):
        texts = json["input"]
        self.batches.append(list(texts))
        data = [
            {"index": index, "embedding": vector_for(text)}
            for index, text in enumerate(texts)
        ]
        if self.drop:
            data = data[: -self.drop]
        if self.reverse:
            data.reverse()
        return FakeResponse(
            {"data": data, "usage": {"total_tokens": TOKENS_PER_INPUT * len(texts)}},
            self.status,
        )

    @property
    def embedded(self) -> list[str]:
        return [text for batch in self.batches for text in batch]


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail = fail

    def _guard(self):
        if self.fail:
            raise redis.ConnectionError("no route to host")

    def hgetall(self, key):
        self._guard()
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self._guard()
        self.hashes.setdefault(key, {})[field] = value

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    """Queues writes, applies them on execute - like the real one."""

    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.queued: list[tuple] = []

    def hset(self, key, field, value):
        self.queued.append((key, field, value))
        return self

    def execute(self):
        for key, field, value in self.queued:
            self.client.hset(key, field, value)
        self.queued = []


# - Ids are VARCHAR and carry their source, exactly as raw_repositories stores
#   them. Integers here would have passed while the real thing raised on every
#   row, which is how the first version of this file got it wrong.
ROWS = [
    {"id": "github_1", "name": "vite", "description": "Next generation frontend tooling", "language": "TypeScript"},
    {"id": "github_2", "name": "axum", "description": "Ergonomic and modular web framework", "language": "Rust"},
    {"id": "npm_certo", "name": "certo", "description": None, "language": None},
]


@pytest.fixture
def config(monkeypatch):
    for name in ("DB_HOST", "DB_PORT", "DB_USER", "DB_NAME", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return Config()


@pytest.fixture
def store(config):
    s = EmbeddingStore(config, client=FakeRedis())
    s.session = FakeOpenAI()
    return s


# ---- what actually gets embedded


def test_the_text_is_name_description_and_language():
    assert embedding_text(ROWS[0]) == "vite Next generation frontend tooling TypeScript"


def test_missing_fields_do_not_leave_gaps():
    # - npm and GitLab contribute no language, and plenty of rows have no
    #   description. Joining blindly would embed the padding.
    assert embedding_text({"name": "certo", "description": None, "language": None}) == "certo"


def test_a_row_with_nothing_to_say_is_not_embedded(store):
    stats = store.refresh([{"id": "github_9", "name": None, "description": None, "language": None}])
    assert store.session.embedded == []
    # - Counted apart from unchanged. Rolled together, a corpus with rows that
    #   can never be embedded reports as fully covered.
    assert stats == {**stats, "no_text": 1, "unchanged": 0, "embedded": 0}


def test_normalising_gives_unit_length():
    assert sum(v * v for v in _normalise([3.0, 4.0])) == pytest.approx(1.0)


def test_normalising_a_zero_vector_does_not_divide_by_zero():
    assert _normalise([0.0, 0.0]) == [0.0, 0.0]


# ---- the part that costs money if it is wrong


def test_the_first_run_embeds_everything(store):
    stats = store.refresh(ROWS)
    assert stats["embedded"] == 3
    assert stats["unchanged"] == 0
    assert len(store.session.embedded) == 3


def test_the_second_run_embeds_nothing(store):
    store.refresh(ROWS)
    store.session = FakeOpenAI()

    stats = store.refresh(ROWS)

    assert stats["embedded"] == 0
    assert stats["unchanged"] == 3
    assert store.session.batches == [], "an unchanged corpus must not reach the API"


def test_adding_a_repository_embeds_exactly_that_one(store):
    store.refresh(ROWS)
    store.session = FakeOpenAI()

    added = {"id": "github_4", "name": "certo-cli", "description": "Command line", "language": "Go"}
    stats = store.refresh([*ROWS, added])

    assert stats["embedded"] == 1
    assert store.session.embedded == [embedding_text(added)]


def test_a_changed_description_embeds_exactly_that_one(store):
    store.refresh(ROWS)
    store.session = FakeOpenAI()

    edited = {**ROWS[1], "description": "Now says something else entirely"}
    stats = store.refresh([ROWS[0], edited, ROWS[2]])

    assert stats["embedded"] == 1
    assert store.session.embedded == [embedding_text(edited)]


def test_tokens_and_approximate_cost_are_reported(store):
    stats = store.refresh(ROWS)
    assert stats["tokens"] == TOKENS_PER_INPUT * 3
    assert stats["approx_cost_usd"] == pytest.approx(
        stats["tokens"] / 1_000_000 * COST_PER_MILLION_TOKENS, abs=1e-6
    )


def test_the_run_is_logged(store, caplog):
    with caplog.at_level("INFO"):
        store.refresh(ROWS)
    assert "embeddings refreshed" in [record.message for record in caplog.records]


# ---- attaching a vector to the wrong repository


def test_vectors_are_placed_by_index_not_by_arrival_order(store):
    # - The API documents that order is preserved. If that ever stopped being
    #   true, every vector would land on the wrong repository and similarity
    #   would go on looking like it was working.
    store.session = FakeOpenAI(reverse=True)
    store.refresh(ROWS)

    stored = json.loads(store.client.hashes[VECTOR_KEY]["github_1"])
    assert stored == pytest.approx(_normalise(vector_for(embedding_text(ROWS[0]))))


def test_a_short_response_is_an_error_rather_than_a_gap(store):
    store.session = FakeOpenAI(drop=1)
    with pytest.raises(EmbeddingError):
        store.refresh(ROWS)


def test_a_failed_request_is_an_error(store):
    store.session = FakeOpenAI(status=429)
    with pytest.raises(EmbeddingError):
        store.refresh(ROWS)


def test_the_digest_is_written_with_the_vector(store):
    store.refresh(ROWS)
    assert set(store.client.hashes[HASH_KEY]) == set(store.client.hashes[VECTOR_KEY])


# ---- refusing to run rather than doing nothing quietly


def test_no_api_key_is_an_error(config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = EmbeddingStore(Config(), client=FakeRedis())
    with pytest.raises(EmbeddingError):
        s.refresh(ROWS)


def test_no_redis_is_an_error(store):
    store.client = None
    with pytest.raises(EmbeddingError):
        store.refresh(ROWS)


# ---- search, which must never be the reason a question fails


def test_search_returns_the_closest_first(store):
    store.refresh(ROWS)
    results = store.search(embedding_text(ROWS[1]), limit=3)
    assert results[0][0] == "github_2"
    assert results[0][1] == pytest.approx(1.0)


def test_search_respects_the_limit(store):
    store.refresh(ROWS)
    assert len(store.search("anything", limit=2)) == 2


def test_search_without_embeddings_is_empty_not_an_error(store):
    assert store.search("what is trending", limit=5) == []


def test_search_without_a_key_is_empty(config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = EmbeddingStore(Config(), client=FakeRedis())
    assert s.search("what is trending", limit=5) == []


def test_search_survives_redis_being_gone(store):
    store.refresh(ROWS)
    store.client.fail = True
    assert store.search("what is trending", limit=5) == []


def test_search_survives_the_api_being_gone(store):
    store.refresh(ROWS)
    store.session = FakeOpenAI(status=500)
    assert store.search("what is trending", limit=5) == []


def test_vectors_of_the_wrong_width_are_skipped(store):
    # - What a change to DIMENSIONS or the model leaves behind. Scoring across
    #   two different spaces would return confident nonsense; the next refresh
    #   replaces them.
    store.refresh(ROWS)
    store.client.hset(VECTOR_KEY, "github_2", json.dumps([1.0, 0.0]))

    found = {repository_id for repository_id, _ in store.search("vite", limit=5)}
    assert "github_2" not in found
    assert "github_1" in found


# ---- the acceptance criterion: no embeddings, still an answer


def _retriever(config, embeddings):
    r = WarehouseRetriever(config, cache=ContextCache(url=""), embeddings=embeddings)
    answers = {
        "AS version": [{"version": "2026-08-09"}],
        "FROM raw_repositories)": [{"repositories": 2013}],
        "dim_sources": [{"source": "github"}],
        "GROUP BY r.language": [{"language": "Python"}],
        "ORDER BY g.stars_gained_1d DESC": [{"name": "skills"}],
        "fct_repository_mentions m": [{"name": "certo"}],
        "fct_undiscovered_mentions": [{"project": "certo"}],
        "stg_repositories": [{"name": "atom"}],
        "dim_languages": [{"language_display": "Python"}],
        "DISTINCT lower(name)": [{"name": "vite"}],
        "lower(r.name) = ANY": [{"id": "github_1", "name": "vite", "source": "github"}],
        "WHERE id = ANY": [
            {"id": "github_2", "name": "axum", "source": "github", "language": "Rust",
             "stars": 21000, "description": "Ergonomic and modular web framework"},
        ],
    }

    def rows(sql, params=()):
        for fragment, result in answers.items():
            if fragment in sql:
                return result
        raise AssertionError(f"unstubbed query: {sql[:80]}")

    r._rows = rows
    return r


def test_deleting_every_embedding_leaves_the_chain_answering(config):
    empty = EmbeddingStore(config, client=FakeRedis())
    empty.session = FakeOpenAI()

    context = _retriever(config, empty).context_for("what is trending")

    assert "similar_by_description" not in context
    assert context["language_growth"], "the structured blocks still have to be there"


def test_similarity_arrives_as_its_own_labelled_block(config, store):
    store.refresh(ROWS)

    context = _retriever(config, store).context_for(embedding_text(ROWS[1]))

    similar = context["similar_by_description"]
    assert [row["name"] for row in similar] == ["axum"]
    assert similar[0]["similarity"] == pytest.approx(1.0)


# ---- the job itself, which is what Kubernetes actually runs


class StubRetriever:
    def __init__(self, config):
        pass

    def connect(self):
        pass

    def close(self):
        pass

    def repositories_for_embedding(self):
        return ROWS


def test_the_job_reports_failure_rather_than_exiting_clean(config, monkeypatch):
    # - The failure this project keeps finding: a run that does nothing and
    #   says nothing went wrong. With neither a Redis to write to nor a key to
    #   call with, the job has to exit non-zero even though the day's
    #   collection was fine - the windowed run is what decides that does not
    #   make the day red.
    import pipeline.rag.embeddings as embeddings_module
    import pipeline.rag.retriever as retriever_module

    monkeypatch.setattr(retriever_module, "WarehouseRetriever", StubRetriever)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert embeddings_module.main() == 1


def test_the_job_reports_success_when_it_embedded(config, monkeypatch):
    import pipeline.rag.embeddings as embeddings_module
    import pipeline.rag.retriever as retriever_module

    monkeypatch.setattr(retriever_module, "WarehouseRetriever", StubRetriever)

    def build(_config):
        s = EmbeddingStore(_config, client=FakeRedis())
        s.session = FakeOpenAI()
        return s

    monkeypatch.setattr(embeddings_module, "EmbeddingStore", build)

    assert embeddings_module.main() == 0


def test_a_repository_deleted_since_embedding_is_dropped(config, store):
    # - Redis outlives a row that dbt or a cleanup removed. Carrying the name
    #   from the vector store would report a project that is no longer there.
    store.refresh(ROWS)
    context = _retriever(config, store).context_for(embedding_text(ROWS[0]))
    assert [row["id"] for row in context["similar_by_description"]] == ["github_2"]
