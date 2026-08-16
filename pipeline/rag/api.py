"""The chain over HTTP.

Three endpoints, one of which costs money. That asymmetry shapes the whole
module: `/ask` calls a model, so it is the one behind the rollback switch and
the rate limit, and the two that only read are left alone.

Port 8001. The metrics exporter has had 8000 since Phase 1, and two processes
binding the same port is the kind of thing that works on a laptop where only
one of them is running.

    python -m pipeline.rag.api
"""

import asyncio
import json
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from pipeline.config import Config
from pipeline.exceptions import HecateError, LoadError
from pipeline.logger import get_logger
from pipeline.realtime.bus import EventBus, HN_STREAM, NPM_STREAM

PORT = 8001

# - Retries on the one connection this module makes before uvicorn starts.
#   ops/windowed-run.ps1 brings Docker up from a cold stop every day, and the
#   cluster's own DNS is not always ready in the first few seconds after -
#   "could not translate host name postgres.hecate.svc.cluster.local" seen
#   live, repeatedly, moments after a fresh start. server.py's refresh loop
#   already treats that as routine and reconnects; this call had no such
#   protection, so the same transient blip crashed the whole pod before it
#   ever got a chance to serve /health, and Kubernetes' own restart-backoff
#   (which grows: 10s, then 20s, then 40s...) took far longer to recover than
#   the DNS record itself needed.
#
#   6 attempts / 30s total was the first cut and still exhausted once on a
#   real cold start (measured live: DNS did not resolve until somewhere
#   between 25s and 40s after the container started) - one crash-and-restart
#   instead of zero. 12 / 60s comes from that measurement, not a guess.
CONNECT_RETRY_ATTEMPTS = 12
CONNECT_RETRY_DELAY_SECONDS = 5

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


def build_app(config: Config, *, chain, retriever, realtime_bus: EventBus | None = None) -> FastAPI:
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
    app.state.realtime_bus = realtime_bus if realtime_bus is not None else EventBus(config.redis_realtime_url)

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

    @app.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        """Pushes real-time npm/HN events as they land on the bus.

        Reads the always-on Redis Streams directly - deliberately not
        through Postgres, so this works whether or not the daily batch has
        drained anything yet. A connection here sees only what happens
        while it's open; there is no history replay on connect, since
        XREAD's blocking-read mode (rather than a consumer group) is what
        lets many simultaneous viewers share one read position each without
        stepping on each other's acknowledgment state the way the drain
        step's consumer group does.
        """
        await websocket.accept()
        bus = app.state.realtime_bus
        if bus.client is None:
            await websocket.close()
            return

        last_ids = {NPM_STREAM: "$", HN_STREAM: "$"}
        try:
            while True:
                try:
                    # - Run the blocking redis call in a thread pool to avoid
                    #   blocking the async event loop.
                    #
                    #   block=1000, under EventBus's own SOCKET_TIMEOUT_SECONDS
                    #   (2s): redis-py does not widen the socket timeout for a
                    #   blocking command, so a block value at or above it makes
                    #   every single cycle raise redis.exceptions.TimeoutError
                    #   instead of returning an empty result (confirmed live
                    #   against a real Memurai instance) - previously swallowed
                    #   by a bare except with nothing logged, so the endpoint
                    #   looked healthy while it was actually timeout-looping
                    #   and reconnecting every few seconds.
                    result = await asyncio.to_thread(
                        bus.client.xread, last_ids, count=50, block=1000
                    )
                except redis.RedisError as exc:
                    log.warning(
                        "live read failed",
                        extra={"context": {"error": str(exc)}},
                    )
                    await asyncio.sleep(1)
                    continue
                for stream, entries in result:
                    for entry_id, fields in entries:
                        last_ids[stream] = entry_id
                        await websocket.send_json({
                            "stream": "npm" if stream == NPM_STREAM else "hn",
                            "event": json.loads(fields["data"]),
                        })
        except WebSocketDisconnect:
            pass

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


def _connect_with_retry(retriever, log) -> None:
    """retriever.connect(), tolerating the first few seconds after a cold start.

    Bounded, not a loop like server.py's - this runs once, at startup, and a
    name that still will not resolve after a half-minute is a real outage
    worth failing loudly on, not something to keep retrying silently forever.
    """
    for attempt in range(1, CONNECT_RETRY_ATTEMPTS + 1):
        try:
            retriever.connect()
            return
        except LoadError as exc:
            if attempt == CONNECT_RETRY_ATTEMPTS:
                raise
            log.warning(
                "database not reachable yet, retrying",
                extra={"context": {"attempt": attempt, "error": str(exc)}},
            )
            time.sleep(CONNECT_RETRY_DELAY_SECONDS)


def main() -> int:
    import uvicorn

    from pipeline.rag.chain import AnswerChain
    from pipeline.rag.retriever import WarehouseRetriever

    log = get_logger("rag.api")
    config = Config()

    retriever = WarehouseRetriever(config)
    _connect_with_retry(retriever, log)

    # - AnswerChain builds its model lazily (on first .answer() call, not
    #   here), so a missing provider key doesn't stop the process from
    #   starting - /health and /trending never touch app.state.chain at all,
    #   and /ask's existing `except HecateError` below catches the deferred
    #   ConfigError on first use, same as any other chain failure.
    app = build_app(config, chain=AnswerChain(retriever, config), retriever=retriever)
    log.info(
        "rag api listening",
        extra={"context": {"port": PORT, "rag_enabled": config.rag_enabled}},
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
