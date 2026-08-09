"""Embedding repositories, and similarity as one extra block.

The corpus does not need this. The median description is 55 characters and the
whole thing is 44k tokens; the structured queries in retriever.py are what
actually answer the questions people ask of it. This exists to build the
pattern properly and to make vector search demonstrable rather than claimed.
That is a real reason, and it is written down rather than dressed up as need.

Which is why it is built to be absent. Similarity is one more block on top of
the structured ones, search swallows every failure, and deleting every stored
vector changes what an answer cites rather than whether there is one.
"""

import hashlib
import json
import math
import sys

import redis
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline.config import Config
from pipeline.exceptions import EmbeddingError, HecateError
from pipeline.logger import get_logger
from pipeline.rag.cache import SOCKET_TIMEOUT_SECONDS

# - The small model, with shortened vectors. Full length is 1536 floats per
#   repository, which as JSON is roughly 30KB - about 60MB across the corpus,
#   against a Redis capped at 128MB that is also holding the context cache.
#   256 dimensions costs a little separation on a corpus this size and fits in
#   a tenth of the space.
MODEL = "text-embedding-3-small"
DIMENSIONS = 256

# - Well inside the 2048-input limit, and small enough that a failed request
#   costs one batch rather than the run.
BATCH = 200

# - For the log line only. This is wrong the day OpenAI reprices; it is here to
#   make the order of magnitude visible, not to bill anyone.
COST_PER_MILLION_TOKENS = 0.02

# - Two hashes rather than a key per repository. Deciding what needs embedding
#   reads only the digests - 2013 fields of 16 bytes rather than 2013 vectors -
#   and dropping every embedding is two DELs.
#
#   It also behaves better under the eviction policy. Redis evicts whole keys,
#   so pressure drops all the vectors or all the digests at once and the next
#   run rebuilds; per-repository keys would let a digest outlive its vector,
#   leaving that row looking current and never searchable again.
HASH_KEY = "hecate:embedding:hash"
VECTOR_KEY = "hecate:embedding:vec"

ENDPOINT = "https://api.openai.com/v1/embeddings"

# - Longer than the extractors' 10s. A batch of 200 inputs is a slower call
#   than fetching a page of repositories.
TIMEOUT = 30

RETRY_STATUSES = (429, 500, 502, 503, 504)


def embedding_text(row: dict) -> str:
    """Name, description and language, which is all there is to go on.

    Stars and dates are deliberately left out. They are what the structured
    blocks are for, and folding a number into the text only teaches the vector
    that popular projects resemble each other.
    """
    parts = (row.get("name"), row.get("description"), row.get("language"))
    return " ".join(part.strip() for part in parts if part and part.strip())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _normalise(vector: list[float]) -> list[float]:
    """Unit length, so similarity is a dot product and nothing else.

    The v3 models return normalised vectors at full length, but not when
    `dimensions` shortens them - the tail that was dropped took some of the
    magnitude with it. Normalising here means the search does not have to care
    which of those two cases produced a stored vector.
    """
    magnitude = math.sqrt(sum(value * value for value in vector))
    if not magnitude:
        return vector
    return [value / magnitude for value in vector]


class EmbeddingStore:
    """Embeddings in Redis, keyed by repository id."""

    def __init__(self, config: Config, client=None) -> None:
        self.log = get_logger("rag.embeddings")
        self.api_key = config.openai_api_key
        self.client = client
        if self.client is None and config.redis_url:
            self.client = redis.from_url(
                config.redis_url,
                socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
                decode_responses=True,
            )

        self.session = requests.Session()
        retry = Retry(
            total=config.retry_attempts,
            backoff_factor=2,
            status_forcelist=RETRY_STATUSES,
            # - The extractors only ever GET, so their adapter does not allow
            #   this. Retrying an embedding POST is safe: it has no effect on
            #   the far side beyond the tokens it bills.
            allowed_methods=("POST",),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    @property
    def available(self) -> bool:
        return self.client is not None and bool(self.api_key)

    def _embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """One call. Returns unit vectors in the order given, and tokens used."""
        try:
            response = self.session.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": MODEL, "input": texts, "dimensions": DIMENSIONS},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise EmbeddingError(f"embeddings request failed: {exc}") from exc

        if not response.ok:
            # - Truncated: a 401 body is short, but a proxy in the way can
            #   return a page of HTML and that does not belong in a log line.
            raise EmbeddingError(
                f"embeddings returned {response.status_code}: {response.text[:200]}"
            )

        body = response.json()

        # - Placed by index rather than trusting the order back. The API
        #   documents that it matches the input, but if it ever did not, every
        #   vector would attach to the wrong repository and similarity would
        #   still look like it was working.
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in body.get("data", []):
            index = item.get("index")
            if index is None or not 0 <= index < len(texts):
                raise EmbeddingError(f"embeddings returned index {index} for {len(texts)} inputs")
            vectors[index] = _normalise(item["embedding"])

        if any(vector is None for vector in vectors):
            raise EmbeddingError("embeddings returned fewer vectors than inputs")

        return vectors, body.get("usage", {}).get("total_tokens", 0)

    def refresh(self, rows: list[dict]) -> dict:
        """Embed what has changed and nothing else.

        The first run does everything and the second does nothing, because the
        digest of the embedded text is stored beside the vector. Re-embedding
        the whole set nightly was the mistake behind the original cost
        estimate: it would have been the entire corpus every day to capture the
        handful of descriptions that actually changed.
        """
        if self.client is None:
            raise EmbeddingError("REDIS_URL is not set, so there is nowhere to store embeddings")
        if not self.api_key:
            raise EmbeddingError("OPENAI_API_KEY is not set")

        stored = self.client.hgetall(HASH_KEY)

        pending = []
        no_text = 0
        for row in rows:
            text = embedding_text(row)
            if not text:
                # - Counted separately from unchanged. A row with no name,
                #   description or language was never embedded and never will
                #   be; folding it into "unchanged" would report a corpus as
                #   fully covered when part of it is permanently absent.
                no_text += 1
                continue
            digest = _digest(text)
            if stored.get(str(row["id"])) == digest:
                continue
            pending.append((str(row["id"]), digest, text))

        embedded = 0
        tokens = 0
        for start in range(0, len(pending), BATCH):
            batch = pending[start : start + BATCH]
            vectors, used = self._embed([text for _, _, text in batch])
            tokens += used

            pipe = self.client.pipeline()
            for (repository_id, digest, _), vector in zip(batch, vectors):
                # - Vector before digest. A crash between the two leaves a
                #   vector with no digest, which the next run simply re-embeds.
                #   The other order leaves a digest with no vector: that row
                #   then looks current forever and is never searchable again.
                pipe.hset(VECTOR_KEY, repository_id, json.dumps(vector))
                pipe.hset(HASH_KEY, repository_id, digest)
            pipe.execute()
            embedded += len(batch)

        stats = {
            "embedded": embedded,
            "unchanged": len(rows) - len(pending) - no_text,
            "no_text": no_text,
            "tokens": tokens,
            "approx_cost_usd": round(tokens / 1_000_000 * COST_PER_MILLION_TOKENS, 4),
        }
        self.log.info("embeddings refreshed", extra={"context": stats})
        return stats

    def search(self, question: str, limit: int) -> list[tuple[str, float]]:
        """Repository ids most similar to the question, best first.

        Ids stay strings: `raw_repositories.id` is a VARCHAR carrying its
        source, so `github_1` and `npm_vite` are ordinary values here and
        anything that tried to make them numbers would fail on every real row.

        Never raises. This block is an addition to the structured ones, so a
        missing Redis, a missing key, an embeddings API having a bad day, or a
        corrupt stored value should cost the question its similarity block and
        nothing else.
        """
        if not self.available:
            return []

        # ponytail: every vector is read and scored per question - about 7MB
        # and tens of milliseconds at 2013 repositories, which is nothing
        # against the API call that precedes it. Past roughly 50k this wants a
        # real vector index rather than a bigger loop.
        try:
            stored = self.client.hgetall(VECTOR_KEY)
            if not stored:
                return []
            query = self._embed([question])[0][0]

            scored = []
            for repository_id, raw in stored.items():
                candidate = json.loads(raw)
                if len(candidate) != len(query):
                    # - A stored vector from a previous DIMENSIONS or model.
                    #   Skip it rather than scoring across mismatched spaces;
                    #   the next refresh replaces it.
                    continue
                scored.append(
                    (repository_id, sum(a * b for a, b in zip(query, candidate)))
                )
        except (redis.RedisError, HecateError, ValueError, TypeError, KeyError) as exc:
            self.log.warning("similarity unavailable", extra={"context": {"error": str(exc)}})
            return []

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]


def main() -> int:
    # - Imported here rather than at the top: the retriever imports this module
    #   for the similarity block, and at module level that is a cycle.
    from pipeline.rag.retriever import WarehouseRetriever

    log = get_logger("rag.embeddings")
    config = Config()
    retriever = WarehouseRetriever(config)

    try:
        retriever.connect()
        rows = retriever.repositories_for_embedding()
    except HecateError as exc:
        log.error("could not read repositories", extra={"context": {"error": str(exc)}})
        return 1
    finally:
        retriever.close()

    try:
        EmbeddingStore(config).refresh(rows)
    except HecateError as exc:
        # - Non-zero, deliberately. The windowed run treats this job as
        #   optional and carries on, but a job that exits 0 having embedded
        #   nothing is indistinguishable from one that worked.
        log.error("embedding failed", extra={"context": {"error": str(exc)}})
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
