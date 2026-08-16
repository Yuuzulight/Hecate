"""Metrics server: gauge refresh and staying up when the database wobbles."""

from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

from pipeline import server
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

    # - cycles bounds the loop, so a broken database is survived rather than
    #   retried until the test times out.
    server.serve(config, cycles=1)

    after = REGISTRY.get_sample_value("hecate_errors_total", labels)
    assert after - before == 1
    stub.close.assert_called_once()


def test_the_connection_is_closed_on_the_way_out(config, monkeypatch):
    stub = MagicMock()
    stub.transaction.return_value.__enter__.return_value.fetchall.return_value = []
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    server.serve(config, cycles=1)
    stub.connect.assert_called_once()
    stub.close.assert_called_once()


def test_it_serves_on_the_port_prometheus_expects(config, monkeypatch):
    seen = {}
    stub = MagicMock()
    stub.transaction.return_value.__enter__.return_value.fetchall.return_value = []
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: seen.update(port=port))

    server.serve(config, cycles=1)
    assert seen["port"] == 8000


def test_an_unreachable_database_is_retried_rather_than_fatal(config, monkeypatch):
    # - The database not being up yet is normal on a cluster, so the exporter
    #   keeps serving and keeps trying instead of exiting. It used to connect
    #   once outside the loop, which meant a restart left it failing against a
    #   dead handle forever while still looking healthy.
    stub = MagicMock()
    stub.connect.side_effect = LoadError("connection refused")
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    labels = {"type": "database", "source": "postgres"}
    before = REGISTRY.get_sample_value("hecate_errors_total", labels) or 0
    server.serve(config, cycles=1)
    after = REGISTRY.get_sample_value("hecate_errors_total", labels)
    assert after - before == 1


def test_a_source_that_disappears_stops_being_reported(loader):
    # - A gauge holds its last value until something clears it, so without this
    #   a source whose rows were deleted would keep reporting the count it had
    #   when it vanished.
    rows_on(loader, [("github", 100, 30.0), ("gone", 7, 10.0)])
    server.refresh(loader)
    assert REGISTRY.get_sample_value("hecate_repositories", {"source": "gone"}) == 7

    rows_on(loader, [("github", 100, 30.0)])
    server.refresh(loader)
    assert REGISTRY.get_sample_value("hecate_repositories", {"source": "gone"}) is None
    assert REGISTRY.get_sample_value(
        "hecate_last_extraction_age_seconds", {"source": "gone"}
    ) is None
    # - The ones still present are untouched.
    assert REGISTRY.get_sample_value("hecate_repositories", {"source": "github"}) == 100


def test_the_exporter_does_not_create_schema(config, monkeypatch):
    # - It only reads. The pipeline owns the schema, and an exporter issuing
    #   DDL on every reconnect is privilege it has no use for.
    stub = MagicMock()
    stub.transaction.return_value.__enter__.return_value.fetchall.return_value = []
    monkeypatch.setattr(server, "PostgreSQLLoader", lambda config: stub)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    server.serve(config, cycles=1)
    stub.create_tables.assert_not_called()


def test_main_returns_zero_when_interrupted(config, monkeypatch):
    monkeypatch.setattr(server, "start_http_server", lambda port: None)
    monkeypatch.setattr(server, "serve", lambda cfg: (_ for _ in ()).throw(KeyboardInterrupt))
    assert server.main() == 0


def test_main_reports_failure_on_a_configuration_problem(monkeypatch):
    from pipeline.exceptions import ConfigError

    monkeypatch.setattr(server, "start_http_server", lambda port: None)
    monkeypatch.setattr(server, "Config", lambda: (_ for _ in ()).throw(ConfigError("no DB_PASSWORD")))
    assert server.main() == 1


def test_a_dropped_connection_is_replaced_on_the_next_cycle(config, monkeypatch):
    built = []

    def build(config):
        stub = MagicMock()
        stub.transaction.return_value.__enter__.return_value.fetchall.return_value = []
        built.append(stub)
        # - First one fails on use, forcing the loop to throw it away.
        if len(built) == 1:
            stub.transaction.side_effect = LoadError("server closed the connection")
        return stub

    monkeypatch.setattr(server, "PostgreSQLLoader", build)
    monkeypatch.setattr(server, "start_http_server", lambda port: None)

    server.serve(config, refresh_seconds=0, cycles=2)

    assert len(built) == 2, "a failed connection should be rebuilt, not reused"
    built[0].close.assert_called_once()


# - Integration tests for forecast gauges. These read back from a real
#   PostgreSQL to verify the gauge refresh correctly pulls today's forecast
#   data.

import pytest

from pipeline.config import Config
from pipeline.loader import PostgreSQLLoader
from pipeline.server import refresh_forecast_gauges

pytestmark = pytest.mark.integration

from tests.test_loaders_integration import ROW, TEST_SCHEMA, wanted


@pytest.fixture
def integration_loader():
    if not wanted():
        pytest.skip("set HECATE_INTEGRATION=1 to run against a real database")
    loader = PostgreSQLLoader(Config())
    loader.connect()
    with loader.conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
    loader.conn.commit()
    loader.create_tables()
    with loader.conn.cursor() as cur:
        cur.execute("TRUNCATE raw_repositories CASCADE")
    loader.conn.commit()
    yield loader
    loader.close()


def test_refresh_forecast_gauges_counts_real_and_suppressed_separately(integration_loader):
    from datetime import date, datetime, timezone

    integration_loader.load_repositories([ROW])
    today = date.today()
    integration_loader.write_forecasts([
        {
            "repository_id": "github_1", "forecast_date": today, "horizon_days": 7,
            "days_observed": 14, "baseline_stars": 100,
            "predicted_stars_p10": 101, "predicted_stars_p50": 102, "predicted_stars_p90": 103,
            "suppressed_reason": None, "model_version": "test", "generated_at": datetime.now(timezone.utc),
        },
        {
            "repository_id": "github_1", "forecast_date": today, "horizon_days": 30,
            "days_observed": 14, "baseline_stars": 100,
            "predicted_stars_p10": None, "predicted_stars_p50": None, "predicted_stars_p90": None,
            "suppressed_reason": "insufficient_history", "model_version": "test", "generated_at": datetime.now(timezone.utc),
        },
    ])

    counts = refresh_forecast_gauges(integration_loader)
    assert counts == {(7, False): 1, (30, True): 1}
