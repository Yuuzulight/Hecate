"""Ensure anonymous requests cannot initialize paid cloud dependencies."""

import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

from infra.aws import runtime

sys.modules.setdefault("aws_runtime", runtime)
spec = importlib.util.spec_from_file_location("hecate_lambda_api", Path(__file__).parents[1] / "infra/aws/lambda_api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def test_anonymous_ask_rejected_before_secret_lookup():
    with patch.object(api, "configure_environment") as configure:
        result = api.handler({"rawPath": "/ask"}, None)
    assert result["statusCode"] == 401
    configure.assert_not_called()


def test_id_token_not_accepted_as_access_token():
    event = {"rawPath": "/ask", "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "owner", "token_use": "id"}}}}}
    with patch.object(api, "configure_environment") as configure:
        result = api.handler(event, None)
    assert result["statusCode"] == 401
    configure.assert_not_called()


def test_health_does_not_keep_database_awake():
    with patch.object(api, "configure_environment") as configure:
        result = api.handler({"rawPath": "/health"}, None)
    assert result["statusCode"] == 200
    configure.assert_not_called()
