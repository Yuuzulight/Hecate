"""Transformer: normalisation, validation, and what happens to bad records."""

from datetime import datetime, timezone

import pytest

from pipeline.exceptions import TransformError
from pipeline.transformers import RepositoryTransformer

VALID = {
    "id": "github_1",
    "source": "github",
    "name": "tensorflow",
    "url": "https://github.com/tensorflow/tensorflow",
    "stars": 185432,
    "forks": 74521,
    "language": "C++",
    "created_at": "2015-11-09T01:02:03Z",
    "updated_at": "2024-08-06T04:05:06Z",
    "description": "An open source machine learning framework",
    "extracted_at": "2024-08-06T12:00:00+00:00",
}


@pytest.fixture
def transformer():
    return RepositoryTransformer()


def test_a_good_record_passes_through(transformer):
    row = transformer.transform(VALID)
    assert row["id"] == "github_1"
    assert row["stars"] == 185432
    assert row["language"] == "C++"


def test_transforming_twice_gives_the_same_result(transformer):
    once = transformer.transform(VALID)
    assert transformer.transform(once) == once


def test_timestamps_are_normalised_to_utc(transformer):
    row = transformer.transform(dict(VALID, created_at="2015-11-09T01:02:03Z"))
    assert row["created_at"] == "2015-11-09T01:02:03+00:00"


def test_offset_timestamps_are_converted_not_just_relabelled(transformer):
    row = transformer.transform(dict(VALID, created_at="2015-11-09T11:02:03+10:00"))
    assert row["created_at"] == "2015-11-09T01:02:03+00:00"


def test_naive_timestamps_are_treated_as_utc(transformer):
    row = transformer.transform(dict(VALID, created_at="2015-11-09T01:02:03"))
    assert row["created_at"] == "2015-11-09T01:02:03+00:00"


def test_datetime_objects_are_accepted(transformer):
    when = datetime(2015, 11, 9, 1, 2, 3, tzinfo=timezone.utc)
    assert transformer.transform(dict(VALID, created_at=when))["created_at"] == \
        "2015-11-09T01:02:03+00:00"


def test_an_unparseable_timestamp_becomes_none(transformer):
    assert transformer.transform(dict(VALID, created_at="whenever"))["created_at"] is None


def test_optional_fields_may_be_missing(transformer):
    sparse = {k: v for k, v in VALID.items()
              if k not in ("language", "description", "created_at", "updated_at")}
    row = transformer.transform(sparse)
    assert row["language"] is None
    assert row["description"] is None
    assert row["created_at"] is None


def test_downloads_stay_empty_when_the_source_has_no_such_metric(transformer):
    # - Distinct from zero downloads, which is a claim about the package.
    assert transformer.transform(VALID)["downloads"] is None


def test_downloads_are_kept_when_present(transformer):
    assert transformer.transform(dict(VALID, downloads=422616954))["downloads"] == 422616954


def test_nonsense_downloads_become_zero_not_none(transformer):
    assert transformer.transform(dict(VALID, downloads="lots"))["downloads"] == 0


def test_counts_default_to_zero(transformer):
    row = transformer.transform(dict(VALID, stars=None, forks="nonsense"))
    assert row["stars"] == 0
    assert row["forks"] == 0


def test_numeric_strings_are_coerced(transformer):
    assert transformer.transform(dict(VALID, stars="42"))["stars"] == 42


def test_negative_counts_are_clamped(transformer):
    assert transformer.transform(dict(VALID, stars=-5))["stars"] == 0


def test_whitespace_is_stripped(transformer):
    assert transformer.transform(dict(VALID, name="  tensorflow  "))["name"] == "tensorflow"


def test_a_blank_name_counts_as_missing(transformer):
    with pytest.raises(TransformError, match="missing required name"):
        transformer.transform(dict(VALID, name="   "))


@pytest.mark.parametrize("field", ["id", "name", "url", "extracted_at"])
def test_required_fields_are_enforced(transformer, field):
    with pytest.raises(TransformError, match=f"missing required.*{field}"):
        transformer.transform({k: v for k, v in VALID.items() if k != field})


def test_an_unknown_source_is_rejected(transformer):
    with pytest.raises(TransformError, match="unknown source"):
        transformer.transform(dict(VALID, source="bitbucket"))


def test_a_missing_source_is_rejected(transformer):
    with pytest.raises(TransformError, match="unknown source"):
        transformer.transform({k: v for k, v in VALID.items() if k != "source"})


@pytest.mark.parametrize("url", ["not-a-url", "ftp://example.com", "javascript:alert(1)"])
def test_non_http_urls_are_rejected(transformer, url):
    with pytest.raises(TransformError, match="is not a URL"):
        transformer.transform(dict(VALID, url=url))


@pytest.mark.parametrize("source", ["github", "npm", "pypi", "gitlab"])
def test_every_source_is_accepted(transformer, source):
    assert transformer.transform(dict(VALID, source=source))["source"] == source


def test_transform_all_keeps_the_good_and_drops_the_bad(transformer):
    records = [VALID, dict(VALID, id="github_2", url="nope"), dict(VALID, id="github_3")]
    rows = transformer.transform_all(records, source="github")
    assert [row["id"] for row in rows] == ["github_1", "github_3"]


def test_transform_all_counts_what_it_dropped(transformer):
    from prometheus_client import REGISTRY

    labels = {"type": "transform", "source": "github"}
    before = REGISTRY.get_sample_value("hecate_errors_total", labels) or 0
    transformer.transform_all([dict(VALID, url="nope")], source="github")
    after = REGISTRY.get_sample_value("hecate_errors_total", labels)
    assert after - before == 1


def test_transform_all_on_an_empty_batch(transformer):
    assert transformer.transform_all([], source="github") == []
