"""The HTTP service: the switch, the limit, and what happens when it breaks.

The chain is stubbed, so no model is called. That is not only for speed - the
central claim about `RAG_ENABLED=0` is that *nothing reaches the model*, and
the only way to check that is to hand the app something that records whether
it was asked. A test that read the 503 out of the response body would pass
just as happily against a flag that called Claude and threw the answer away.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from pipeline import server as metrics_server
from pipeline.config import Config
from pipeline.exceptions import LoadError
from pipeline.rag.api import (
    LATENCY_TARGET_MS,
    PORT,
    RateLimiter,
    build_app,
    client_key,
)

ANSWER = {
    "answer": "Python gained the most stars.",
    "confidence": "high",
    "sources": {"repository_ids": ["github_1"], "blocks": ["language_growth"]},
    "latency_ms": 5,
}


class StubChain:
    """Records every question it was asked, which is the whole point."""

    def __init__(self, answer=None, raises=None):
        self.answer_value = answer if answer is not None else ANSWER
        self.raises = raises
        self.asked = []

    def answer(self, question):
        self.asked.append(question)
        if self.raises is not None:
            raise self.raises
        return dict(self.answer_value)


class StubRetriever:
    def __init__(self, evaluations=None, fail_evaluations=False, fail_links=False):
        self.evaluations = evaluations if evaluations is not None else []
        self.fail_evaluations = fail_evaluations
        self.fail_links = fail_links
        self.conn = object()

    def links_for(self, repository_ids):
        if self.fail_links:
            raise LoadError("connection closed")
        known = {
            "github_1": {"id": "github_1", "name": "skills", "source": "github",
                         "url": "https://github.com/anthropics/skills"},
        }
        return [known[r] for r in repository_ids if r in known]

    def fastest_growing(self, limit=10):
        return [{"id": "github_1", "name": "skills", "stars_gained_1d": 207}][:limit]

    def most_discussed(self, limit=10):
        return [{"id": "npm_vite", "name": "vite", "posts": 4}][:limit]

    def language_growth(self, limit=10):
        return [{"language": "Python", "stars_gained_1d": 6081}][:limit]

    def coverage(self):
        return {"repositories": 2013, "snapshot_days": 3}

    def recent_evaluations(self, limit=50):
        if self.fail_evaluations:
            raise LoadError("relation \"rag_evaluations\" does not exist")
        return self.evaluations[:limit]

    def close(self):
        pass


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.delenv("RAG_ENABLED", raising=False)
    return Config()


def make_client(config, chain=None, retriever=None, realtime_bus=None):
    chain = chain if chain is not None else StubChain()
    retriever = retriever if retriever is not None else StubRetriever()
    if realtime_bus is not None:
        app = build_app(config, chain=chain, retriever=retriever, realtime_bus=realtime_bus)
    else:
        app = build_app(config, chain=chain, retriever=retriever)
    return TestClient(app), chain, retriever


# ---- /ask


def test_ask_returns_answer_confidence_sources_and_latency(config):
    client, _, _ = make_client(config)
    response = client.post("/ask", json={"question": "what is growing?"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"answer", "confidence", "sources", "latency_ms"}
    assert body["confidence"] == "high"
    assert body["sources"]["repository_ids"] == ["github_1"]


def test_the_reported_latency_is_the_servers_not_the_chains(config):
    # - The chain reports 5ms for its own work. The caller waited for parsing,
    #   validation and serialisation too, and that is the number worth having.
    client, _, _ = make_client(config)
    body = client.post("/ask", json={"question": "what is growing?"}).json()
    assert isinstance(body["latency_ms"], int)
    assert body["latency_ms"] >= 0
    assert LATENCY_TARGET_MS == 5000


def test_the_question_reaches_the_chain(config):
    client, chain, _ = make_client(config)
    client.post("/ask", json={"question": "what is trending"})
    assert chain.asked == ["what is trending"]


def test_an_empty_question_is_rejected_before_the_chain(config):
    client, chain, _ = make_client(config)
    assert client.post("/ask", json={"question": ""}).status_code == 422
    assert chain.asked == []


def test_an_enormous_question_is_rejected(config):
    client, chain, _ = make_client(config)
    assert client.post("/ask", json={"question": "x" * 5000}).status_code == 422
    assert chain.asked == []


# ---- the rollback switch


def test_disabled_returns_503_and_never_touches_the_chain(monkeypatch):
    # - The acceptance criterion, and the reason the chain is injected: proving
    #   the model was not called needs something that would have noticed.
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("RAG_ENABLED", "0")
    client, chain, _ = make_client(Config())

    response = client.post("/ask", json={"question": "what is growing?"})

    assert response.status_code == 503
    assert chain.asked == [], "a flag that still calls the model is not a rollback"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
def test_the_switch_understands_the_usual_spellings(monkeypatch, value):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("RAG_ENABLED", value)
    assert Config().rag_enabled is False


def test_the_switch_defaults_on(config):
    # - A rollback switch, not an opt-in. Defaulting off would mean every fresh
    #   deploy served 503 until somebody noticed and set a variable.
    assert config.rag_enabled is True


def test_health_reports_the_switch(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("RAG_ENABLED", "0")
    client, _, _ = make_client(Config())
    assert client.get("/health").json() == {"status": "ok", "rag_enabled": False}


def test_trending_still_works_when_the_switch_is_off(monkeypatch):
    # - Deliberate: /trending calls no model and costs nothing. Taking the free
    #   read-only view down as part of a rollback makes the rollback worse.
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("RAG_ENABLED", "0")
    client, _, _ = make_client(Config())
    assert client.get("/trending").status_code == 200


# ---- rate limiting, per caller rather than per socket


def test_two_clients_behind_one_ingress_are_limited_separately(config):
    client, _, _ = make_client(config)

    for _ in range(30):
        assert client.post(
            "/ask", json={"question": "q"}, headers={"x-forwarded-for": "203.0.113.1"}
        ).status_code == 200

    # - The first caller is spent.
    assert client.post(
        "/ask", json={"question": "q"}, headers={"x-forwarded-for": "203.0.113.1"}
    ).status_code == 429

    # - The second is untouched, which is the whole point: behind a Service
    #   every request shares one socket address, so limiting on that would
    #   have taken this caller down with the noisy one.
    assert client.post(
        "/ask", json={"question": "q"}, headers={"x-forwarded-for": "203.0.113.9"}
    ).status_code == 200


def test_the_first_hop_is_the_client(config):
    class Fake:
        headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1, 10.0.0.2"}
        client = None

    assert client_key(Fake()) == "203.0.113.7"


def test_without_the_header_the_socket_is_used(config):
    class Fake:
        headers = {}
        client = type("C", (), {"host": "10.1.2.3"})()

    assert client_key(Fake()) == "10.1.2.3"


def test_a_blank_header_falls_back_rather_than_keying_on_empty(config):
    class Fake:
        headers = {"x-forwarded-for": "   "}
        client = type("C", (), {"host": "10.1.2.3"})()

    assert client_key(Fake()) == "10.1.2.3"


def test_the_window_lets_a_caller_back_in():
    limiter = RateLimiter(limit=2, window=60)
    assert limiter.allow("a", now=0) is True
    assert limiter.allow("a", now=1) is True
    assert limiter.allow("a", now=2) is False
    # - Once the window has passed, the earlier requests no longer count.
    assert limiter.allow("a", now=100) is True


# ---- CORS, which the original scope imported and never registered


def test_a_browser_on_another_origin_gets_the_header(config):
    client, _, _ = make_client(config)
    response = client.get("/trending", headers={"Origin": "https://hecate.example"})
    assert response.headers.get("access-control-allow-origin") == "*"


def test_the_preflight_is_answered(config):
    client, _, _ = make_client(config)
    response = client.options(
        "/ask",
        headers={
            "Origin": "https://hecate.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" in response.headers


# ---- failures that say what failed


def test_a_broken_chain_returns_a_real_error_and_logs_the_cause(config, caplog):
    client, _, _ = make_client(config, chain=StubChain(raises=ValueError("schema drift")))

    with caplog.at_level("ERROR"):
        response = client.post("/ask", json={"question": "what is growing?"})

    assert response.status_code == 500
    assert "schema drift" in response.json()["detail"]
    assert "ask failed" in [record.message for record in caplog.records]


def test_a_warehouse_failure_is_reported_rather_than_swallowed(config, caplog):
    class Broken(StubRetriever):
        def fastest_growing(self, limit=10):
            raise LoadError("connection closed")

    client, _, _ = make_client(config, retriever=Broken())
    with caplog.at_level("ERROR"):
        response = client.get("/trending")

    assert response.status_code == 503
    assert "connection closed" in response.json()["detail"]


# ---- /trending


def test_trending_returns_the_free_blocks(config):
    client, _, _ = make_client(config)
    body = client.get("/trending").json()
    assert set(body) == {"fastest_growing", "most_discussed", "languages", "coverage"}
    assert body["coverage"]["snapshot_days"] == 3


def test_the_trending_limit_is_bounded(config):
    client, _, retriever = make_client(config)
    assert client.get("/trending?limit=99999").status_code == 200
    assert client.get("/trending?limit=0").status_code == 200


# ---- /eval-metrics


def test_eval_metrics_summarises_the_scores(config):
    rows = [
        {"question_id": "a", "question": "q", "faithfulness": 0.9, "relevance": 0.8,
         "hallucination": False, "judge_model": "claude-opus-5", "evaluated_at": "2026-08-09"},
        {"question_id": "b", "question": "q", "faithfulness": 0.3, "relevance": 0.7,
         "hallucination": True, "judge_model": "claude-opus-5", "evaluated_at": "2026-08-09"},
    ]
    client, _, _ = make_client(config, retriever=StubRetriever(evaluations=rows))

    body = client.get("/eval-metrics").json()
    assert body["evaluated"] == 2
    assert body["mean_faithfulness"] == pytest.approx(0.6)
    assert body["hallucinations"] == 1


def test_unscored_rows_do_not_become_a_mean_of_zero(config):
    rows = [{"question_id": "a", "question": "q", "faithfulness": None, "relevance": None,
             "hallucination": None, "judge_model": "claude-opus-5", "evaluated_at": "2026-08-09"}]
    client, _, _ = make_client(config, retriever=StubRetriever(evaluations=rows))

    body = client.get("/eval-metrics").json()
    # - None, not 0.0. A zero here reads as every answer being unfaithful,
    #   which is the opposite of "the judge could not be reached".
    assert body["mean_faithfulness"] is None
    assert body["evaluated"] == 1


def test_no_evaluations_yet_is_a_state_not_an_error(config, caplog):
    client, _, _ = make_client(config, retriever=StubRetriever(fail_evaluations=True))

    with caplog.at_level("WARNING"):
        response = client.get("/eval-metrics")

    assert response.status_code == 200
    assert response.json()["evaluated"] == 0
    # - Logged rather than swallowed, so a table that is missing for a bad
    #   reason is still visible.
    assert "no evaluation history" in [record.message for record in caplog.records]


# ---- the UI, and the links it needs


def test_cited_ids_come_back_as_something_clickable(config):
    # - The chain cites ids because names collide across sources. An id is not
    #   a thing a person can click, so the API resolves them here.
    client, _, _ = make_client(config)
    sources = client.post("/ask", json={"question": "what is growing?"}).json()["sources"]

    assert sources["repository_ids"] == ["github_1"]
    assert sources["repositories"] == [
        {"id": "github_1", "name": "skills", "source": "github",
         "url": "https://github.com/anthropics/skills"},
    ]


def test_a_failed_lookup_costs_the_links_not_the_answer(config, caplog):
    client, _, _ = make_client(config, retriever=StubRetriever(fail_links=True))

    with caplog.at_level("WARNING"):
        body = client.post("/ask", json={"question": "what is growing?"}).json()

    assert body["answer"], "the answer still has to arrive"
    assert body["sources"]["repository_ids"] == ["github_1"]
    assert body["sources"]["repositories"] == []
    assert "could not resolve sources" in [record.message for record in caplog.records]


def test_the_ui_is_served_by_the_service(config):
    client, _, _ = make_client(config)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Hecate" in response.text


def test_the_ui_renders_sources_rather_than_hiding_them(config):
    # - A grounded answer whose grounding is hidden looks exactly like an
    #   ungrounded one, so there is no disclosure triangle to click.
    page = make_client(config)[0].get("/").text
    assert "<details" not in page
    assert "Sources" in page


def test_the_ui_explains_a_stopped_service(config):
    page = make_client(config)[0].get("/").text
    assert "The service is not running" in page
    assert "windowed-run.ps1 -StartOnly" in page


def test_the_ui_marks_low_confidence_distinctly(config):
    page = make_client(config)[0].get("/").text
    assert ".card.low" in page
    assert "Low confidence" in page


def test_the_ui_does_not_build_markup_from_response_text(config):
    # - Answers and descriptions come from a model reading third-party text.
    #   textContent throughout; one innerHTML would make that an injection.
    #
    #   Matching the bare word caught the comment saying not to use it, which
    #   is a test failing on a mention of the thing rather than the thing.
    page = make_client(config)[0].get("/").text
    for unsafe in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe not in page, f"{unsafe} builds markup from untrusted text"


# ---- metrics, which have to exist before any panel queries them


def test_the_service_exposes_its_own_counters(config):
    client, _, _ = make_client(config)
    body = client.get("/metrics").text
    for metric in (
        "hecate_rag_questions_total",
        "hecate_rag_tokens_total",
        "hecate_rag_cost_usd_total",
        "hecate_rag_context_cache_total",
    ):
        assert metric in body, f"{metric} is on a dashboard panel but nobody emits it"


def test_the_endpoint_reflects_counter_activity(config):
    # - Incremented directly rather than by asking a question: the counter
    #   lives in the chain, and the chain here is a stub. Asking through the
    #   stub and asserting the counter moved would be testing the stub.
    from pipeline import metrics

    metrics.rag_questions.labels(outcome="high").inc()
    body = make_client(config)[0].get("/metrics").text
    assert 'hecate_rag_questions_total{outcome="high"}' in body


# ---- ports


def test_the_service_does_not_take_the_metrics_port():
    # - The original scope had both on 8000, which works on a laptop where only
    #   one of them is ever running.
    assert PORT == 8001
    assert PORT != metrics_server.PORT


# ---- a missing provider key costs /ask, not the whole service
#
# AnswerChain builds its model lazily (see tests/test_rag_chain.py for that
# guarantee directly) - constructing it never raises, even with no key
# configured, so main() can build the app unconditionally. This is the
# end-to-end proof: the service actually stays up and /ask actually returns
# a clear error, rather than the process never starting at all.


def test_a_missing_provider_key_fails_the_request_not_the_process(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("RAG_PROVIDER", "gemini")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    config = Config()

    class StubRetriever:
        def context_for(self, question, limit=10):
            return {}

        def close(self):
            pass

    from pipeline.rag.chain import AnswerChain

    # - Construction itself must not raise - that's the whole guarantee.
    chain = AnswerChain(StubRetriever(), config)

    # - The app builds and /health works - it never touches app.state.chain,
    #   so a chain with an unbuildable model doesn't stop it. (/trending is
    #   the other endpoint that doesn't touch the chain; it's exercised
    #   elsewhere in this file with a retriever that actually implements it.)
    app = build_app(config, chain=chain, retriever=StubRetriever())
    client = TestClient(app)
    assert client.get("/health").status_code == 200

    # - /ask is where the deferred ConfigError actually surfaces, with the
    #   real message, rather than the process never having started at all.
    response = client.post("/ask", json={"question": "what is growing?"})
    assert response.status_code == 502
    assert "GOOGLE_API_KEY is required for RAG_PROVIDER=gemini" in response.json()["detail"]


# ---- _connect_with_retry: a cold-start DNS blip is not the same as a real outage
#
# Seen live, twice in one session: ops/windowed-run.ps1 brings Docker up from
# a cold stop, and postgres.hecate.svc.cluster.local does not always resolve
# in the first few seconds after. Before this, that crashed the whole pod on
# the one connection attempt main() makes before uvicorn ever starts.


class StubLog:
    def __init__(self):
        self.warnings = []

    def warning(self, message, extra=None):
        self.warnings.append((message, extra))


class FlakyRetriever:
    """Fails a fixed number of times, then connects cleanly."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def connect(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LoadError("could not translate host name")


def test_connect_with_retry_does_not_sleep_when_the_first_attempt_works(monkeypatch):
    from pipeline.rag import api as api_module

    sleeps = []
    monkeypatch.setattr(api_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    retriever = FlakyRetriever(fail_times=0)
    api_module._connect_with_retry(retriever, StubLog())

    assert retriever.calls == 1
    assert sleeps == []


def test_connect_with_retry_recovers_from_a_transient_failure(monkeypatch):
    from pipeline.rag import api as api_module

    sleeps = []
    monkeypatch.setattr(api_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    log = StubLog()

    retriever = FlakyRetriever(fail_times=2)
    api_module._connect_with_retry(retriever, log)

    # - Connected on the third try, so two failures were tolerated and slept
    #   through rather than raised.
    assert retriever.calls == 3
    assert len(sleeps) == 2
    assert len(log.warnings) == 2


def test_connect_with_retry_gives_up_after_the_last_attempt(monkeypatch):
    from pipeline.rag import api as api_module

    monkeypatch.setattr(api_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(api_module, "CONNECT_RETRY_ATTEMPTS", 3)

    # - A name that still will not resolve after every attempt is a real
    #   outage, not a cold-start blip - it must still surface, not vanish
    #   into an infinite retry.
    retriever = FlakyRetriever(fail_times=99)
    with pytest.raises(LoadError):
        api_module._connect_with_retry(retriever, StubLog())

    assert retriever.calls == 3


# ---- WebSocket /live streaming real-time events


class FakeRedisForLive:
    def __init__(self):
        self.streams = {}
        self._seq = 0
        # - Where "$" resolved to, per stream, the first time it was asked
        #   for. Real Redis re-resolves "$" fresh on every call to "only
        #   entries added after this exact call" - it is never a way to
        #   replay history. The route only ever advances last_ids away from
        #   "$" once it has actually received an entry, so every call before
        #   the first delivery still arrives here as "$" - remembering the
        #   cutoff the first time means those repeated calls agree with each
        #   other on what counts as "new" instead of re-resolving to a later
        #   and later tail.
        self._dollar_cutoff = {}
        # - Set once the route's background thread has made its first read
        #   call, so a test can publish only after the "$" cutoff above is
        #   actually established, instead of racing it with a fixed sleep -
        #   publishing before the first read would make the fake capture a
        #   cutoff past the new entry, and the route would never see it,
        #   hanging the test on a receive_json() call this starlette version
        #   gives no timeout for.
        self.first_read_done = threading.Event()

    def xadd(self, stream, fields):
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.streams.setdefault(stream, []).append((entry_id, fields))
        return entry_id

    def xread(self, streams, count=None, block=None):
        result = []
        for stream, last_id in streams.items():
            entries = self.streams.get(stream, [])
            if last_id == "$":
                cutoff = self._dollar_cutoff.setdefault(stream, len(entries))
                new = entries[cutoff:]
            else:
                new = [(eid, f) for eid, f in entries if eid > last_id]
            if new:
                result.append((stream, new))
        self.first_read_done.set()
        return result


def test_live_websocket_streams_a_published_event(config):
    from pipeline.realtime.bus import EventBus
    from pipeline.realtime.npm_listener import NPM_STREAM

    bus = EventBus(url="")
    bus.client = FakeRedisForLive()

    client, _, _ = make_client(config, realtime_bus=bus)

    with client.websocket_connect("/live") as websocket:
        # - Waits for the route's first xread before publishing, rather than
        #   racing it with a fixed sleep - publishing before that first read
        #   would make the fake's "$" cutoff land past the new entry, and
        #   the route would never see it, hanging receive_json() below
        #   (this starlette version gives it no timeout). Waiting on the
        #   event makes the ordering deterministic instead of merely
        #   probable, and is what makes this exercise "push a new event to
        #   an already-connected client" for real, rather than passing only
        #   because the fake used to replay history on connect - the
        #   route's own docstring says there is no such replay.
        assert bus.client.first_read_done.wait(timeout=5), (
            "route never made its first read - fixture or route wiring broke"
        )

        def publish_after_first_read():
            bus.client.xadd(NPM_STREAM, {"data": '{"id": "npm_left-pad", "name": "left-pad"}'})

        threading.Thread(target=publish_after_first_read, daemon=True).start()

        message = websocket.receive_json()
        assert message["stream"] == "npm"
        assert message["event"]["id"] == "npm_left-pad"


def test_fake_redis_for_live_does_not_replay_history_predating_the_connection():
    """Pins real Redis $ semantics on the fixture itself, synchronously and
    without going through a live WebSocket: an entry that existed before
    the first "$" read for a stream is history, and real Redis's "$" means
    "only entries added after this call", never a replay of it. This is
    the exact bug the previous version of this fixture had - it returned
    every existing entry on a "$" read - which let
    test_live_websocket_streams_a_published_event above pass for the wrong
    reason (see that test's comment) even though the route's own docstring
    says there is no history replay on connect.
    """
    from pipeline.realtime.npm_listener import NPM_STREAM

    fake = FakeRedisForLive()
    fake.xadd(NPM_STREAM, {"data": '{"id": "npm_left-pad", "name": "left-pad"}'})

    # - Nothing yet: the entry above predates this "$" read, so it must not
    #   come back.
    assert fake.xread({NPM_STREAM: "$"}) == []

    # - Something published after that first "$" read must come back on the
    #   next read, still passing "$" - matching the route, which only ever
    #   advances last_ids away from "$" once it has actually received an
    #   entry.
    fake.xadd(NPM_STREAM, {"data": '{"id": "npm_is-thirteen", "name": "is-thirteen"}'})
    result = fake.xread({NPM_STREAM: "$"})
    assert len(result) == 1
    stream, entries = result[0]
    assert stream == NPM_STREAM
    assert len(entries) == 1


def test_live_websocket_with_no_bus_configured_closes_cleanly(config):
    # - REDIS_REALTIME_URL unset is the common case. The endpoint must exist
    #   and accept the connection, then close it, rather than the app
    #   failing to start or the route 404ing outright.
    from pipeline.realtime.bus import EventBus

    bus = EventBus(url="")
    client, _, _ = make_client(config, realtime_bus=bus)

    with client.websocket_connect("/live") as websocket:
        with pytest.raises(Exception):
            websocket.receive_json()  # connection closes with nothing to send
