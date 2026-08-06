"""Metrics server: gauge refresh and staying up when the database wobbles."""

from unittest.mock import MagicMock

import psycopg2
import pytest
from prometheus_client import REGISTRY

from pipeline import metrics, server
from pipeline.config import Config
from pipeline.exceptions import LoadError


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    return Config()


@pytest.fixture
def loader(config):
    loader = MagicMock()
    cursor = MagicMock()
    loader.transaction.return_value.__enter__.return_value = cursor
    return loader


def rows_on(loader, rows):
    loader.transaction.return_value.__enter__.return_value.fetchall.return_value = rows


def test_gauges_follow_the_stored_row_counts(loader):
    rows_on(loader, [("github", 100, 30.0), ("npm", 40, 90.0)])

    assert server.refresh(loader) == {"github": 100, "npm": 40}
    assert REGISTRY.get_sample_value("hecate_repositories", {"source": "github"}) == 100
    assert REGISTRY.get_sample_value("hecate_repositories", {"source": "npm"}) == 40


def test_extraction_age_is_recorded(loader):
    rows_on(loader, [("github", 10, 3600.0)])
    server.refresh(loader)
    value = REGISTRY.get_sample_value(
        "hecate_last_extraction_age_seconds", {"source": "github"}
    )
    assert value == 3600.0


def test_a_null_age_becomes_zero_rather_than_failing(loader):
    rows_on(loader, [("gitlab", 5, None)])
    server.refresh(loader)
    assert REGISTRY.get_sample_value(
        "hecate_last_extraction_age_seconds", {"source": "gitlab"}
    ) == 0


def test_an_empty_table_refreshes_to_nothing(loader):
    rows_on(loader, [])
    assert server.refresh(loader) == {}


def test_a_database_failure_does_not_bring_the_endpoint_down(config, monkeypatch):
    stub = MagicMock()
    stub.transaction.side_effect = LoadError("connection lost")
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    labels = {"type": "database", "source": "postgres"}
    before = REGISTRY.get_sample_value("hecate_errors_total", labels) or 0

    # - forever=False runs a single cycle, so a broken database is survived
    #   rather than retried until the test times out.
    server.serve(config, forever=False)

    after = REGISTRY.get_sample_value("hecate_errors_total", labels)
    assert after - before == 1
    stub.close.assert_called_once()


def test_the_connection_is_closed_on_the_way_out(config, monkeypatch):
    stub = MagicMock()
    stub.transaction.return_value.__enter__.return_value.fetchall.return_value = []
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    server.serve(config, forever=False)
    stub.connect.assert_called_once()
    stub.close.assert_called_once()


def test_it_serves_on_the_port_prometheus_expects(config, monkeypatch):
    seen = {}
    stub = MagicMock()
    stub.transaction.return_value.__enter__.return_value.fetchall.return_value = []
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: seen.update(port=port))

    server.serve(config, forever=False)
    assert seen["port"] == 8000


def test_an_unreachable_database_exits_non_zero(config, monkeypatch):
    stub = MagicMock()
    stub.connect.side_effect = LoadError("connection refused")
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    assert server.main() == 1
