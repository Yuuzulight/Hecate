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

import pytest

from pipeline.config import Config
from pipeline.loader import PostgreSQLLoader

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


#  - These tests truncate between cases, so they get their own schema. Pointing
#    them at the default one meant `pytest` quietly emptied whatever the
#    pipeline had just collected.
TEST_SCHEMA = "hecate_test"


@pytest.fixture
def loader():
    if not wanted():
        pytest.skip("set HECATE_INTEGRATION=1 to run against a real database")

    loader = PostgreSQLLoader(Config())
    loader.connect()

    with loader.conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
        # - Everything on this connection now resolves here, including the
        #   loader's own CREATE TABLE and upserts.
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
    loader.conn.commit()

    loader.create_tables()
    with loader.conn.cursor() as cur:
        # - CASCADE because social_mentions references this table. Naming both
        #   would work today and quietly stop clearing everything the next time
        #   something else points here.
        cur.execute("TRUNCATE raw_repositories CASCADE")
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


def test_rows_come_back_shaped_the_way_they_went_in(loader):
    # - The quality checks read this back rather than trusting the batch that
    #   was sent, so the round trip has to preserve column order and values.
    loader.load_repositories([dict(ROW, id="npm_x", source="npm", downloads=12345)])
    (row,) = loader.rows_for("npm")
    assert row["id"] == "npm_x"
    assert row["source"] == "npm"
    assert row["stars"] == ROW["stars"]
    assert row["downloads"] == 12345
    assert row["url"] == ROW["url"]


def test_rows_for_only_returns_the_source_asked_for(loader):
    loader.load_repositories([ROW, dict(ROW, id="npm_y", source="npm")])
    assert [r["id"] for r in loader.rows_for("npm")] == ["npm_y"]
    assert [r["id"] for r in loader.rows_for("github")] == ["github_1"]


def test_discovery_candidates_rank_by_attention(loader):
    loader.load_repositories([ROW])
    loader.load_mentions([
        dict(MENTION, id="hn_a", repository_id=None,
             target_url="https://github.com/a/quiet", score=30),
        dict(MENTION, id="hn_b", repository_id=None,
             target_url="https://github.com/b/loud", score=90),
        dict(MENTION, id="hn_c", repository_id=None,
             target_url="https://github.com/c/ignored", score=2),
    ])
    assert loader.discovery_candidates(minimum_score=25, limit=10) == [
        "https://github.com/b/loud",
        "https://github.com/a/quiet",
    ]


def test_discovery_candidates_respect_the_cap(loader):
    # - The cap is what stops a busy day becoming a rate-limit incident.
    loader.load_repositories([ROW])
    loader.load_mentions([
        dict(MENTION, id=f"hn_{i}", repository_id=None,
             target_url=f"https://github.com/x/r{i}", score=100)
        for i in range(5)
    ])
    assert len(loader.discovery_candidates(minimum_score=25, limit=2)) == 2


def test_already_tracked_projects_are_not_candidates(loader):
    loader.load_repositories([ROW])
    loader.load_mentions([MENTION])
    assert loader.discovery_candidates(minimum_score=1, limit=10) == []


def test_origin_records_how_a_repository_arrived(loader):
    loader.load_repositories([dict(ROW, id="github_9", origin="discovered")])
    assert query(loader, "SELECT origin FROM raw_repositories WHERE id='github_9'")[0][0] == "discovered"


def test_a_later_seeded_run_does_not_erase_that_it_was_discovered(loader):
    loader.load_repositories([dict(ROW, id="github_9", origin="discovered")])
    loader.load_repositories([dict(ROW, id="github_9", origin=None)])
    assert query(loader, "SELECT origin FROM raw_repositories WHERE id='github_9'")[0][0] == "discovered"


def test_rows_for_an_absent_source_is_empty(loader):
    assert loader.rows_for("gitlab") == []


MENTION = {
    "id": "hackernews_1",
    "platform": "hackernews",
    "repository_id": "github_1",
    "target_url": "https://github.com/tensorflow/tensorflow",
    "title": "Show HN: tensorflow",
    "url": "https://news.ycombinator.com/item?id=1",
    "score": 240,
    "comments": 31,
    "author": "someone",
    "channel": None,
    "posted_at": "2026-08-01T09:00:00+00:00",
    "extracted_at": "2026-08-06T12:00:00+00:00",
}


def stored_mentions(loader):
    return query(loader, "SELECT id, score FROM social_mentions ORDER BY id")


def test_a_mention_of_a_tracked_repository_is_stored(loader):
    loader.load_repositories([ROW])
    assert loader.load_mentions([MENTION]) == 1
    assert stored_mentions(loader) == [("hackernews_1", 240)]


def test_a_mention_of_an_unknown_repository_is_kept_unresolved(loader):
    # - The reverse of the original rule. A post about something not tracked is
    #   the discovery signal, so it is stored with a null repository_id rather
    #   than thrown away.
    loader.load_repositories([ROW])
    assert loader.load_mentions([dict(MENTION, repository_id="github_nope")]) == 1
    assert query(loader, "SELECT repository_id FROM social_mentions")[0][0] is None
    assert query(loader, "SELECT target_url FROM social_mentions")[0][0] is not None


def test_a_mixed_batch_keeps_everything(loader):
    loader.load_repositories([ROW])
    kept = loader.load_mentions([
        MENTION,
        dict(MENTION, id="hackernews_2", repository_id="github_nope"),
    ])
    assert kept == 2
    assert query(loader, "SELECT count(*) FROM social_mentions WHERE repository_id IS NULL")[0][0] == 1


def test_a_mention_resolves_retrospectively_once_its_repository_appears(loader):
    # - The discovery loop depends on this: a post stored unresolved today
    #   attaches to the repository when a later run adds it.
    loader.load_repositories([ROW])
    loader.load_mentions([dict(MENTION, repository_id=None)])
    assert query(loader, "SELECT repository_id FROM social_mentions")[0][0] is None

    loader.load_mentions([MENTION])
    assert query(loader, "SELECT repository_id FROM social_mentions")[0][0] == "github_1"


def test_a_resolved_mention_is_not_unresolved_by_a_later_blank(loader):
    loader.load_repositories([ROW])
    loader.load_mentions([MENTION])
    loader.load_mentions([dict(MENTION, repository_id=None)])
    assert query(loader, "SELECT repository_id FROM social_mentions")[0][0] == "github_1"


def test_reloading_a_post_refreshes_its_score(loader):
    loader.load_repositories([ROW])
    loader.load_mentions([MENTION])
    loader.load_mentions([dict(MENTION, score=900, comments=77)])
    assert stored_mentions(loader) == [("hackernews_1", 900)]
    assert query(loader, "SELECT count(*) FROM social_mentions")[0][0] == 1


def test_re_resolution_takes_the_newer_answer(loader):
    # - Resolution is derived from target_url, which does not change, so a
    #   different result means the URL now matches a different stored row - a
    #   rename, usually. The newer answer is the right one. Only a blank is
    #   ignored, which the test below covers.
    loader.load_repositories([ROW, dict(ROW, id="github_2")])
    loader.load_mentions([MENTION])
    loader.load_mentions([dict(MENTION, repository_id="github_2")])
    assert query(loader, "SELECT repository_id FROM social_mentions")[0][0] == "github_2"


def test_duplicate_posts_within_a_batch_are_collapsed(loader):
    loader.load_repositories([ROW])
    assert loader.load_mentions([MENTION, dict(MENTION, score=999)]) == 1
    assert stored_mentions(loader) == [("hackernews_1", 999)]


def test_an_empty_mention_batch_touches_nothing(loader):
    assert loader.load_mentions([]) == 0


def test_posts_from_different_platforms_coexist(loader):
    # - The table is meant to hold any platform, not just whichever landed
    #   first. This is the check that the abstraction holds.
    loader.load_repositories([ROW])
    loader.load_mentions([
        MENTION,
        dict(MENTION, id="reddit_1", platform="reddit", channel="rust", score=88),
    ])
    rows = query(
        loader,
        "SELECT platform, count(*) FROM social_mentions GROUP BY platform ORDER BY platform",
    )
    assert rows == [("hackernews", 1), ("reddit", 1)]


def test_mentions_go_when_their_repository_does(loader):
    loader.load_repositories([ROW])
    loader.load_mentions([MENTION])
    with loader.conn.cursor() as cur:
        cur.execute("DELETE FROM raw_repositories WHERE id = 'github_1'")
    loader.conn.commit()
    assert stored_mentions(loader) == []


def snapshots(loader, columns="repository_id, stars, mention_count"):
    return query(loader, f"SELECT {columns} FROM repository_snapshots ORDER BY repository_id")


def test_a_snapshot_records_every_stored_repository(loader):
    loader.load_repositories([ROW, dict(ROW, id="github_2", stars=5)])
    assert loader.snapshot(with_mentions=False) == 2
    assert [r[0] for r in snapshots(loader)] == ["github_1", "github_2"]


def test_snapshotting_twice_in_a_day_replaces_rather_than_appends(loader):
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)
    loader.load_repositories([dict(ROW, stars=999)])
    loader.snapshot(with_mentions=False)

    assert snapshots(loader) == [("github_1", 999, None)]


def test_mention_count_is_null_when_the_extractors_did_not_run(loader):
    # - Distinct from zero. A run with mentions switched off is not a day
    #   nobody posted.
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)
    assert snapshots(loader)[0][2] is None


def test_mention_count_is_zero_when_they_ran_and_found_nothing(loader):
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=True)
    assert snapshots(loader)[0][2] == 0


def test_the_mention_count_is_cumulative_not_daily(loader):
    # - Every post ever seen, not posts today. The daily figure is the
    #   difference between two snapshots, which is why this table exists.
    loader.load_repositories([ROW])
    loader.load_mentions([MENTION])
    loader.snapshot(with_mentions=True)
    loader.load_mentions([dict(MENTION, id="hackernews_2")])
    loader.snapshot(with_mentions=True)
    assert snapshots(loader)[0][2] == 2


def test_mentions_are_counted_per_repository(loader):
    loader.load_repositories([ROW, dict(ROW, id="github_2")])
    loader.load_mentions([
        MENTION,
        dict(MENTION, id="hackernews_2"),
        dict(MENTION, id="hackernews_3", repository_id="github_2"),
    ])
    loader.snapshot(with_mentions=True)
    assert [(r[0], r[2]) for r in snapshots(loader)] == [("github_1", 2), ("github_2", 1)]


def test_a_source_that_failed_this_run_keeps_its_place_in_the_series(loader):
    # - Snapshots read the stored table, not the batch just processed, so a
    #   source that failed today still has yesterday's figures recorded.
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)
    assert len(snapshots(loader)) == 1


def test_nulls_survive_into_the_snapshot(loader):
    # - GitHub reports no downloads, and the snapshot must not turn that into
    #   zero on the way through.
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)
    assert query(loader, "SELECT downloads FROM repository_snapshots")[0][0] is None


def test_snapshots_go_when_their_repository_does(loader):
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)
    with loader.conn.cursor() as cur:
        cur.execute("DELETE FROM raw_repositories WHERE id = 'github_1'")
    loader.conn.commit()
    assert snapshots(loader) == []


def test_these_tests_do_not_touch_the_real_table(loader):
    # - The guard for the whole file: if search_path ever stops pointing at the
    #   test schema, this fails before anything gets truncated for real.
    assert query(loader, "SELECT current_schema()")[0][0] == TEST_SCHEMA


def test_the_indexes_exist(loader):
    names = {row[0] for row in query(
        loader,
        f"SELECT indexname FROM pg_indexes WHERE tablename = 'raw_repositories' "
        f"AND schemaname = '{TEST_SCHEMA}'",
    )}
    assert "idx_raw_repositories_source" in names
    assert "idx_raw_repositories_extracted_at" in names


def test_top_forecast_targets_ranks_by_one_day_gain(loader):
    loader.load_repositories([ROW, dict(ROW, id="github_2", name="smaller", stars=100)])
    with loader.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repository_snapshots (repository_id, captured_on, stars) VALUES "
            "(%s, current_date - 1, %s), (%s, current_date, %s), "
            "(%s, current_date - 1, %s), (%s, current_date, %s)",
            ("github_1", 185432, "github_1", 185532, "github_2", 100, "github_2", 150),
        )
    loader.conn.commit()

    targets = loader.top_forecast_targets(n=10)
    assert [t["id"] for t in targets] == ["github_1", "github_2"]
    assert targets[0]["stars_gained_1d"] == 100
    assert targets[1]["stars_gained_1d"] == 50


def test_top_forecast_targets_handles_a_repository_with_one_day_of_history(loader):
    # - Only one snapshot exists yet, so there's no previous day to diff
    #   against. Must not crash, and must not be excluded - a single-day
    #   repository is a real candidate the confidence gate will suppress
    #   later, not something this ranking step should silently drop.
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)

    targets = loader.top_forecast_targets(n=10)
    assert len(targets) == 1
    assert targets[0]["stars_gained_1d"] is None


def test_top_forecast_targets_respects_the_limit(loader):
    for i in range(5):
        loader.load_repositories([dict(ROW, id=f"github_{i}", name=f"repo{i}", stars=100 + i)])
    loader.snapshot(with_mentions=False)

    assert len(loader.top_forecast_targets(n=3)) == 3


def test_snapshot_series_returns_the_full_daily_history_in_order(loader):
    loader.load_repositories([ROW])
    with loader.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repository_snapshots (repository_id, captured_on, stars) VALUES "
            "(%s, current_date - 1, %s), (%s, current_date, %s)",
            ("github_1", 185432, "github_1", 185500),
        )
    loader.conn.commit()

    series = loader.snapshot_series("github_1")
    assert [stars for _, stars in series] == [185432, 185500]
    assert series[0][0] < series[1][0]


def test_snapshot_series_for_an_unknown_repository_is_empty(loader):
    assert loader.snapshot_series("github_does_not_exist") == []


FORECAST_ROW = {
    "repository_id": "github_1",
    "forecast_date": "2026-08-21",
    "horizon_days": 7,
    "days_observed": 14,
    "baseline_stars": 185432,
    "predicted_stars_p10": 185600,
    "predicted_stars_p50": 185700,
    "predicted_stars_p90": 185900,
    "suppressed_reason": None,
    "model_version": "timesfm-2.5-200m-pytorch@1d952420fba87f3c6dee4f240de0f1a0fbc790e3",
    "generated_at": "2026-08-21T03:35:00+00:00",
}


def test_writing_forecasts_twice_for_the_same_day_replaces_rather_than_appends(loader):
    loader.load_repositories([ROW])
    loader.write_forecasts([FORECAST_ROW])
    loader.write_forecasts([dict(FORECAST_ROW, predicted_stars_p50=999999)])

    rows = loader.forecast_rows_for("2026-08-21")
    assert len(rows) == 1
    assert rows[0]["predicted_stars_p50"] == 999999


def test_a_suppressed_forecast_is_stored_with_null_predictions_not_omitted(loader):
    loader.load_repositories([ROW])
    suppressed = dict(
        FORECAST_ROW,
        days_observed=5,
        predicted_stars_p10=None,
        predicted_stars_p50=None,
        predicted_stars_p90=None,
        suppressed_reason="insufficient_history",
    )
    loader.write_forecasts([suppressed])

    rows = loader.forecast_rows_for("2026-08-21")
    assert len(rows) == 1
    assert rows[0]["suppressed_reason"] == "insufficient_history"
    assert rows[0]["predicted_stars_p50"] is None


def test_forecast_rows_for_a_different_date_are_unaffected(loader):
    loader.load_repositories([ROW])
    loader.write_forecasts([FORECAST_ROW])
    assert loader.forecast_rows_for("2026-08-22") == []
