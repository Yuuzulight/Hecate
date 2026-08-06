"""Loader: SQL wiring, batching, transactions, and error handling.

These run against a stand-in connection. Whether the SQL is actually idempotent
is a question a mock cannot answer, so that's checked against a real database in
test_loaders_integration.py.
"""

from unittest.mock import MagicMock

import psycopg2
import pytest

from pipeline.config import Config
from pipeline.exceptions import LoadError
from pipeline.loaders import PostgreSQLLoader

ROW = {
    "id": "github_1",
    "source": "github",
    "name": "tensorflow",
    "url": "https://github.com/tensorflow/tensorflow",
    "stars": 185432,
    "forks": 74521,
    "language": "C++",
    "created_at": "2015-11-09T01:02:03+00:00",
    "updated_at": "2024-08-06T04:05:06+00:00",
    "description": "An open source machine learning framework",
    "extracted_at": "2024-08-06T12:00:00+00:00",
}


@pytest.fixture
def config(monkeypatch):
    # - Pin the whole environment, or an ambient DB_PORT from a local database
    #   leaks in and these assertions become machine-dependent.
    for name in ("DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PASSWORD", "secret")
    return Config()


@pytest.fixture
def upserts(monkeypatch):
    """Capture what would have gone to execute_values.

    The real one reaches into the connection for its encoding, which a stand-in
    connection can't provide, and the interesting part is the SQL and the values
    anyway.
    """
    captured = []
    monkeypatch.setattr(
        "pipeline.loaders.postgres.execute_values",
        lambda cur, sql, values, **kwargs: captured.append((sql, values)),
    )
    return captured


@pytest.fixture
def loader(config, upserts):
    loader = PostgreSQLLoader(config)
    loader.conn = MagicMock()
    return loader


def cursor_of(loader):
    """The cursor the loader actually uses, via `with conn.cursor() as cur`."""
    return loader.conn.cursor.return_value.__enter__.return_value


def executed(loader):
    """Every SQL string handed to the cursor directly."""
    return [call.args[0] for call in cursor_of(loader).execute.call_args_list]


def test_connect_passes_the_configured_target(config, monkeypatch):
    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    PostgreSQLLoader(config).connect()
    assert seen["host"] == "localhost"
    assert seen["port"] == 5432
    assert seen["dbname"] == "hecate"


def test_a_refused_connection_becomes_a_load_error(config, monkeypatch):
    def refuse(**kwargs):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    with pytest.raises(LoadError, match="could not connect"):
        PostgreSQLLoader(config).connect()


def test_the_password_stays_out_of_the_error(config, monkeypatch):
    def refuse(**kwargs):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    with pytest.raises(LoadError) as caught:
        PostgreSQLLoader(config).connect()
    assert "secret" not in str(caught.value)


def test_create_tables_is_safe_to_repeat(loader):
    loader.create_tables()
    (sql,) = executed(loader)
    assert "CREATE TABLE IF NOT EXISTS raw_repositories" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql


# - Commit and rollback are psycopg2's own connection-context behaviour, so
#   asserting them against a stand-in connection would only be testing the
#   stand-in. test_loaders_integration.py checks them against a real database.


def test_load_upserts_rather_than_inserting_blindly(loader, upserts):
    loader.load_repositories([ROW])
    ((sql, _),) = upserts
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "stars = EXCLUDED.stars" in sql


def test_load_does_not_overwrite_immutable_columns(loader, upserts):
    loader.load_repositories([ROW])
    ((sql, _),) = upserts
    update = sql.split("DO UPDATE")[1]
    assert "created_at = EXCLUDED" not in update
    assert "source = EXCLUDED" not in update


def test_load_returns_the_row_count(loader):
    assert loader.load_repositories([ROW, dict(ROW, id="github_2")]) == 2


def test_an_empty_batch_touches_nothing(loader):
    assert loader.load_repositories([]) == 0
    loader.conn.cursor.assert_not_called()


def test_duplicate_ids_within_a_batch_are_collapsed(loader):
    # - Postgres rejects an upsert that would touch the same row twice in one
    #   statement, so the batch has to be unique before it goes anywhere.
    rows = [ROW, dict(ROW, stars=999), dict(ROW, id="github_2")]
    assert loader.load_repositories(rows) == 2


def test_the_last_duplicate_wins(loader, upserts):
    from pipeline.loaders.postgres import COLUMNS

    loader.load_repositories([ROW, dict(ROW, stars=999)])
    ((_, values),) = upserts
    assert values[0][COLUMNS.index("stars")] == 999


def test_values_are_sent_in_column_order(loader, upserts):
    from pipeline.loaders.postgres import COLUMNS

    loader.load_repositories([ROW])
    ((_, values),) = upserts
    assert values[0] == tuple(ROW[column] for column in COLUMNS)


def test_a_driver_error_becomes_a_load_error(loader):
    cursor_of(loader).execute.side_effect = psycopg2.DataError("bad value")
    with pytest.raises(LoadError, match="database error"):
        loader.create_tables()


def test_a_database_failure_is_counted(loader):
    from prometheus_client import REGISTRY

    labels = {"type": "database", "source": "postgres"}
    before = REGISTRY.get_sample_value("hecate_errors_total", labels) or 0
    cursor_of(loader).execute.side_effect = psycopg2.DataError("nope")
    with pytest.raises(LoadError):
        loader.create_tables()
    after = REGISTRY.get_sample_value("hecate_errors_total", labels)
    assert after - before == 1


def test_using_the_loader_before_connecting_says_so(config):
    with pytest.raises(LoadError, match="not connected"):
        PostgreSQLLoader(config).create_tables()


def test_close_is_safe_to_call_twice(loader):
    connection = loader.conn
    loader.close()
    loader.close()
    connection.close.assert_called_once()


def test_close_before_connecting_does_nothing(config):
    PostgreSQLLoader(config).close()
