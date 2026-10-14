"""HTTP API adapter. Paid requests require an API Gateway-verified JWT."""

import json
import os
from pathlib import Path
from datetime import datetime, timezone

from aws_runtime import configure_environment


def response(status, value, content_type="application/json"):
    return {
        "statusCode": status,
        "headers": {"content-type": content_type, "cache-control": "no-store", "x-content-type-options": "nosniff", "x-frame-options": "DENY"},
        "body": value if isinstance(value, str) else json.dumps(value),
    }


def handler(event, context):
    path = event.get("rawPath", "/")
    if path == "/":
        return response(200, Path("/app/pipeline/rag/static/index.html").read_text(), "text/html; charset=utf-8")
    if path == "/auth-config":
        return response(200, {"domain": os.environ["COGNITO_DOMAIN"], "clientId": os.environ["COGNITO_CLIENT_ID"]})
    if path == "/health":
        return response(200, {"status": "ok", "rag_enabled": True})
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub") or claims.get("token_use") != "access":
        return response(401, {"detail": "Sign in to continue"})
    configure_environment()
    from pipeline.config import Config
    from pipeline.rag.retriever import WarehouseRetriever
    from pipeline.rag.chain import AnswerChain
    from pipeline.rag.api import build_app
    from mangum import Mangum

    config = Config()
    retriever = WarehouseRetriever(config)
    try:
        if path == "/ask":
            # Atomic shared quota survives cold starts and concurrent invocations.
            key = "hecate:ask-quota:" + datetime.now(timezone.utc).date().isoformat()
            script = "local n=tonumber(redis.call('GET',KEYS[1]) or '0'); if n>=tonumber(ARGV[1]) then return 0 end; redis.call('INCR',KEYS[1]); redis.call('EXPIRE',KEYS[1],172800); return 1"
            if not retriever.cache.client.eval(script, 1, key, int(os.environ.get("DAILY_ASK_LIMIT", "50"))):
                return response(429, {"detail": "Daily question limit reached"})
        retriever.connect()
        app = build_app(config, chain=AnswerChain(retriever, config), retriever=retriever)
        if path == "/forecasts":
            return response(200, retriever.forecasts())
        return Mangum(app, lifespan="off")(event, context)
    finally:
        retriever.close()
        for client in (retriever.cache.client, retriever.embeddings.client):
            if client is not None:
                client.close()
