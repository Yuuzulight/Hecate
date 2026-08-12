"""The chain over HTTP.

Three endpoints, one of which costs money. That asymmetry shapes the whole
module: `/ask` calls a model, so it is the one behind the rollback switch and
the rate limit, and the two that only read are left alone.

Port 8001. The metrics exporter has had 8000 since Phase 1, and two processes
binding the same port is the kind of thing that works on a laptop where only
one of them is running.

    python -m pipeline.rag.api
"""

import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from pipeline.config import Config
from pipeline.exceptions import HecateError
from pipeline.logger import get_logger

PORT = 8001

# - Server-side, request to response, and reported in the body. One number:
#   an internal figure that excludes the queueing and serialisation the caller
#   actually waited through is a number about us rather than about them.
LATENCY_TARGET_MS = 5000

# ponytail: per-process, in-memory, fixed window. With one replica that is the
# limit; with three it is three times the limit, and a restart forgets
# everybody. Both are fine for a service that exists to answer a handful of
# questions a day. Move to Redis - which is already here - if it ever runs
# behind more than one pod in anger.
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

MAX_QUESTION_LENGTH = 500

# - The UI ships with the service rather than beside it. One file, no build
#   step, and serving it from here means same-origin requests and nothing extra
#   to start.
UI_PATH = Path(__file__).parent / "static" / "index.html"


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)


class RateLimiter:
    """Requests per window, per caller."""

    def __init__(self, limit: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW_SECONDS):
        self.limit = limit
        self.window = window
        self.seen: dict[str, deque] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        recent = self.seen.setdefault(key, deque())
        while recent and now - recent[0] > self.window:
            recent.popleft()
        if len(recent) >= self.limit:
            return False
        recent.append(now)

        # - Forget callers who have gone quiet. The key comes from a header
        #   anyone can set, so without this a caller sending a fresh made-up
        #   address each time grows this dictionary until the pod dies.
        for gone in [k for k, seen in self.seen.items() if not seen]:
            del self.seen[gone]
        return True


def client_key(request: Request) -> str:
    """Who is asking, as far as anything here can tell.

    Behind a Kubernetes Service every request arrives from the ingress, so the
    socket address is the same for everyone and limiting on it limits the
    whole world as a single caller. The forwarded header is the only thing
    that distinguishes callers, and it is also trivially spoofable - which is
    the trade being made: this is a politeness limit against accidental
    hammering, not an access control. Anything stronger belongs at the
    ingress, which can actually see the connection.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        # - First entry is the original client; the rest are proxies it passed
        #   through on the way here.
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def build_app(config: Config, *, chain, retriever) -> FastAPI:
    """The application, with its dependencies handed in rather than imported.

    Tests pass stubs; `main()` passes the real thing. The alternative - module
    level globals wired up on import - makes "does this call the model when
    RAG_ENABLED is off" a question you cannot ask without a key.

    Both are required rather than defaulting to None, which would build an app
    that starts cleanly and fails on the first request.
    """
    log = get_logger("rag.api")
    limiter = RateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # - Connecting is the caller's job; closing is not, because uvicorn is
        #   what knows when the process is going away.
        yield
        if app.state.retriever is not None and hasattr(app.state.retriever, "close"):
            app.state.retriever.close()

    app = FastAPI(title="Hecate", version="2.0.0", lifespan=lifespan)
    app.state.config = config
    app.state.chain = chain
    app.state.retriever = retriever

    # - Registered, not merely imported. The original scope imported the
    #   middleware and never added it, which is invisible until a browser on
    #   another origin tries and gets a CORS error with nothing in the logs.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def require_enabled() -> None:
        if not config.rag_enabled:
            # - Before anything reaches the chain. A rollback switch that lets
            #   the request through and discards the answer has still paid for
            #   the tokens, which is not a rollback.
            raise HTTPException(status_code=503, detail="RAG is disabled (RAG_ENABLED=0)")

    def require_quota(request: Request) -> None:
        key = client_key(request)
        if not limiter.allow(key):
            log.warning("rate limited", extra={"context": {"client": key}})
            raise HTTPException(status_code=429, detail="too many requests, try again shortly")

    @app.get("/", response_class=HTMLResponse)
    def ui() -> str:
        try:
            return UI_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            # - Says which file, rather than 404ing as though there were no UI
            #   by design.
            log.error("ui missing", extra={"context": {"path": str(UI_PATH), "error": str(exc)}})
            raise HTTPException(status_code=500, detail=f"UI not found at {UI_PATH}") from exc

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        # - Here rather than on a second port. The exporter on 8000 reads the
        #   warehouse and knows nothing about questions; these counters live in
        #   this process and die with it, so they have to be served from it.
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    def health() -> dict:
        # - Reports the switch rather than hiding it. A service answering 503s
        #   because somebody rolled it back looks identical from outside to
        #   one that is simply broken.
        return {"status": "ok", "rag_enabled": config.rag_enabled}

    @app.post("/ask")
    def ask(body: Question, request: Request) -> dict:
        require_enabled()
        require_quota(request)

        started = time.perf_counter()
        try:
            answer = app.state.chain.answer(body.question)
        except HecateError as exc:
            log.error("ask failed", extra={"context": {"error": str(exc)}})
            raise HTTPException(status_code=502, detail=f"could not answer: {exc}") from exc
        except Exception as exc:
            # - Broad, but never silent. A swallowed exception here reaches the
            #   caller as a confident empty answer, which is the worst possible
            #   failure for something whose whole job is being trustworthy.
            log.exception("ask failed", extra={"context": {"error": str(exc)}})
            raise HTTPException(
                status_code=500, detail=f"{type(exc).__name__}: {exc}"
            ) from exc

        # - Cited ids turned into something clickable. Done here rather than in
        #   the chain because it is presentation: the model cites ids, and what
        #   a person needs is a link. A lookup failure costs the links, not the
        #   answer - the ids are still in the response either way.
        sources = dict(answer["sources"])
        try:
            sources["repositories"] = app.state.retriever.links_for(
                sources.get("repository_ids", [])
            )
        except HecateError as exc:
            log.warning("could not resolve sources", extra={"context": {"error": str(exc)}})
            sources["repositories"] = []

        # - After the lookup above, not before it. That is a database round
        #   trip the caller waits through, and a latency figure that stops
        #   measuring partway is the same kind of number this project keeps
        #   throwing out: technically computed, quietly about something else.
        #
        #   The server-side number also replaces the chain's, which times only
        #   its own work.
        latency_ms = int((time.perf_counter() - started) * 1000)
        if latency_ms > LATENCY_TARGET_MS:
            log.warning(
                "over latency target",
                extra={"context": {"latency_ms": latency_ms, "target_ms": LATENCY_TARGET_MS}},
            )

        return {**answer, "sources": sources, "latency_ms": latency_ms}

    @app.get("/trending")
    def trending(limit: int = 10) -> dict:
        # - Deliberately not behind RAG_ENABLED. It calls no model and costs
        #   nothing; the switch exists to stop spending, and a rollback that
        #   also takes down the free read-only view is a worse rollback.
        limit = max(1, min(limit, 50))
        try:
            return {
                "fastest_growing": app.state.retriever.fastest_growing(limit),
                "most_discussed": app.state.retriever.most_discussed(limit),
                "languages": app.state.retriever.language_growth(limit),
                "coverage": app.state.retriever.coverage(),
            }
        except HecateError as exc:
            log.error("trending failed", extra={"context": {"error": str(exc)}})
            raise HTTPException(status_code=503, detail=f"warehouse unavailable: {exc}") from exc

    @app.get("/eval-metrics")
    def eval_metrics(limit: int = 50) -> dict:
        """The most recent evaluation scores, as recorded by #46."""
        limit = max(1, min(limit, 500))
        try:
            rows = app.state.retriever.recent_evaluations(limit)
        except HecateError as exc:
            # - An absent table is an honest state: nothing has been evaluated
            #   yet. Reported as such rather than as a 500, and logged rather
            #   than swallowed.
            log.warning("no evaluation history", extra={"context": {"error": str(exc)}})
            return {
                "evaluated": 0,
                "mean_faithfulness": None,
                "hallucinations": 0,
                "scores": [],
                "note": "no evaluations recorded yet",
            }

        scored = [r["faithfulness"] for r in rows if r["faithfulness"] is not None]
        return {
            "evaluated": len(rows),
            # - None rather than 0 when nothing scored. A zero here would read
            #   as every answer being unfaithful.
            "mean_faithfulness": (sum(scored) / len(scored)) if scored else None,
            "hallucinations": sum(1 for r in rows if r["hallucination"]),
            "scores": rows,
        }

    return app


def main() -> int:
    import uvicorn

    from pipeline.rag.chain import AnswerChain
    from pipeline.rag.retriever import WarehouseRetriever

    log = get_logger("rag.api")
    config = Config()

    retriever = WarehouseRetriever(config)
    retriever.connect()

    app = build_app(config, chain=AnswerChain(retriever, config), retriever=retriever)
    log.info(
        "rag api listening",
        extra={"context": {"port": PORT, "rag_enabled": config.rag_enabled}},
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
