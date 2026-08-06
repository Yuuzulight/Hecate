"""Idempotency, checked against a real PostgreSQL.

Whether ON CONFLICT actually does what it's supposed to is not something a mock
can tell you, so these run against a live database. Bring one up with
`docker compose up -d postgres` and set HECATE_INTEGRATION=1.

That opt-in is deliberate. Skipping whenever the connection fails looks tidy
but means a suite pointed at the wrong database, or the right database with the
wrong password, reports green having tested nothing at all. With the flag set,
a database that won't accept us is a failure, not a skip.
"""

import os

import psycopg2
import pytest

from pipeline.config import Config
from pipeline.loaders import PostgreSQLLoader

pytestmark = pytest.mark.integration

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


#  - "0" and "false" are strings, and every non-empty string is truthy, so a
#    plain truth test would have HECATE_INTEGRATION=0 turning these on.
OFF = ("", "0", "false", "no", "off")


def wanted() -> bool:
    return os.environ.get("HECATE_INTEGRATION", "").strip().lower() not in OFF


@pytest.fixture
def loader():
    if not wanted():
        pytest.skip("set HECATE_INTEGRATION=1 to run against a real database")

    loader = PostgreSQLLoader(Config())
    loader.connect()
    loader.create_tables()
    with loader.conn.cursor() as cur:
        cur.execute("TRUNCATE raw_repositories")
    loader.conn.commit()

    yield loader
    loader.close()


def query(loader, sql):
    with loader.conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def count(loader):
    return query(loader, "SELECT count(*) FROM raw_repositories")[0][0]


def test_loading_the_same_batch_twice_leaves_one_row(loader):
    loader.load_repositories([ROW])
    loader.load_repositories([ROW])
    assert count(loader) == 1


def test_a_second_load_refreshes_the_changed_values(loader):
    loader.load_repositories([ROW])
    loader.load_repositories([dict(ROW, stars=200000, forks=80000)])

    (stars, forks) = query(loader, "SELECT stars, forks FROM raw_repositories")[0]
    assert stars == 200000
    assert forks == 80000


def test_a_rename_is_picked_up(loader):
    loader.load_repositories([ROW])
    loader.load_repositories([dict(ROW, name="tensorflow2", url="https://github.com/x/y")])

    (name, url) = query(loader, "SELECT name, url FROM raw_repositories")[0]
    assert name == "tensorflow2"
    assert url == "https://github.com/x/y"


def test_created_at_is_not_rewritten(loader):
    loader.load_repositories([ROW])
    original = query(loader, "SELECT created_at FROM raw_repositories")[0][0]
    loader.load_repositories([dict(ROW, created_at="2020-01-01T00:00:00+00:00")])
    assert query(loader, "SELECT created_at FROM raw_repositories")[0][0] == original


def test_loaded_at_moves_on_every_write(loader):
    loader.load_repositories([ROW])
    first = query(loader, "SELECT loaded_at FROM raw_repositories")[0][0]
    loader.load_repositories([ROW])
    assert query(loader, "SELECT loaded_at FROM raw_repositories")[0][0] >= first


def test_a_batch_containing_the_same_id_twice_does_not_error(loader):
    # - Without deduplication Postgres raises "cannot affect row a second time".
    loader.load_repositories([ROW, dict(ROW, stars=999)])
    assert count(loader) == 1
    assert query(loader, "SELECT stars FROM raw_repositories")[0][0] == 999


def test_a_mixed_batch_inserts_and_updates_in_one_go(loader):
    loader.load_repositories([ROW])
    loader.load_repositories([dict(ROW, stars=1), dict(ROW, id="github_2", stars=2)])
    assert count(loader) == 2


def test_create_tables_on_an_existing_database_is_a_no_op(loader):
    loader.load_repositories([ROW])
    loader.create_tables()
    assert count(loader) == 1


def test_a_bad_row_rolls_the_whole_batch_back(loader):
    from pipeline.exceptions import LoadError

    loader.load_repositories([ROW])
    # - stars is an INTEGER column; this batch cannot land.
    with pytest.raises(LoadError):
        loader.load_repositories([
            dict(ROW, id="github_2", stars=3),
            dict(ROW, id="github_3", stars="not a number"),
        ])
    # - Neither of the two new rows should be there.
    assert count(loader) == 1


def test_downloads_survive_a_round_trip(loader):
    # - BIGINT, because weekly figures for the busiest packages run into the
    #   hundreds of millions and monthly clears a billion.
    loader.load_repositories([dict(ROW, id="npm_tslib", source="npm", downloads=1699733117)])
    assert query(loader, "SELECT downloads FROM raw_repositories")[0][0] == 1699733117


def test_a_source_without_downloads_stores_null_not_zero(loader):
    loader.load_repositories([ROW])
    assert query(loader, "SELECT downloads FROM raw_repositories")[0][0] is None


def test_the_indexes_exist(loader):
    names = {row[0] for row in query(
        loader, "SELECT indexname FROM pg_indexes WHERE tablename = 'raw_repositories'"
    )}
    assert "idx_raw_repositories_source" in names
    assert "idx_raw_repositories_extracted_at" in names
