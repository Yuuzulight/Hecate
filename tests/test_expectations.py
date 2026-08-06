"""Data quality checks: each one catches its bad case and passes its good one."""

from datetime import datetime, timedelta, timezone

import pytest
from prometheus_client import REGISTRY

from pipeline.expectations import RepositoryExpectations

NOW = datetime.now(timezone.utc)

GOOD = {
    "id": "github_1",
    "source": "github",
    "name": "tensorflow",
    "url": "https://github.com/tensorflow/tensorflow",
    "stars": 185432,
    "forks": 74521,
    "created_at": "2015-11-09T01:02:03+00:00",
    "extracted_at": NOW.isoformat(),
}


@pytest.fixture
def expectations():
    return RepositoryExpectations()


def test_a_clean_batch_reports_no_failures(expectations):
    report = expectations.validate([GOOD])
    assert report["failures"] == {}
    assert report["pass_rate"] == 1.0
    assert report["rows"] == 1


@pytest.mark.parametrize(
    "check,row",
    [
        ("id_present", dict(GOOD, id=None)),
        ("name_present", dict(GOOD, name="")),
        ("url_present", dict(GOOD, url=None)),
        ("url_is_http", dict(GOOD, url="ftp://example.com")),
        ("source_known", dict(GOOD, source="bitbucket")),
        ("stars_in_range", dict(GOOD, stars=-1)),
        ("stars_in_range", dict(GOOD, stars=99_000_000)),
        ("forks_in_range", dict(GOOD, forks=-5)),
        ("extracted_at_present", dict(GOOD, extracted_at=None)),
    ],
)
def test_each_check_catches_its_own_bad_case(expectations, check, row):
    assert expectations.validate([row])["failures"].get(check) == 1


def test_a_creation_date_in_the_future_is_caught(expectations):
    ahead = (NOW + timedelta(days=30)).isoformat()
    assert expectations.validate([dict(GOOD, created_at=ahead)])["failures"].get(
        "created_at_not_future"
    ) == 1


def test_a_missing_creation_date_is_allowed(expectations):
    # - npm reports none at all, so absent is not a failure.
    report = expectations.validate([dict(GOOD, created_at=None)])
    assert "created_at_not_future" not in report["failures"]


def test_a_stale_extraction_is_caught(expectations):
    old = (NOW - timedelta(days=5)).isoformat()
    assert expectations.validate([dict(GOOD, extracted_at=old)])["failures"].get(
        "extracted_at_recent"
    ) == 1


def test_duplicate_ids_are_caught_across_the_batch(expectations):
    report = expectations.validate([GOOD, dict(GOOD), dict(GOOD, id="github_2")])
    assert report["failures"].get("id_unique") == 1


def test_a_naive_timestamp_does_not_crash_the_check(expectations):
    report = expectations.validate([dict(GOOD, extracted_at=NOW.replace(tzinfo=None).isoformat())])
    assert "extracted_at_recent" not in report["failures"]


def test_an_unparseable_timestamp_fails_rather_than_raising(expectations):
    report = expectations.validate([dict(GOOD, extracted_at="whenever")])
    assert report["failures"].get("extracted_at_recent") == 1


def test_the_pass_rate_reflects_how_much_failed(expectations):
    report = expectations.validate([GOOD, dict(GOOD, id="github_2", stars=-1)])
    assert 0 < report["pass_rate"] < 1


def test_an_empty_batch_is_not_a_failure(expectations):
    report = expectations.validate([])
    assert report["pass_rate"] == 1.0
    assert report["failures"] == {}


def test_outcomes_are_counted_for_prometheus(expectations):
    labels = {"check": "stars_in_range", "outcome": "fail"}
    before = REGISTRY.get_sample_value("hecate_quality_checks_total", labels) or 0
    expectations.validate([dict(GOOD, stars=-1)])
    after = REGISTRY.get_sample_value("hecate_quality_checks_total", labels)
    assert after - before == 1


def test_validation_never_raises_on_a_thoroughly_broken_row(expectations):
    # - Quality reporting must not be the thing that takes a run down.
    report = expectations.validate([{}])
    assert report["rows"] == 1
    assert report["failures"]
