"""GitLab extractor: field mapping, paging, auth, and rate limits."""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.extractors.gitlab import GitLabExtractor
from tests.test_extractors import FakeResponse

PROJECT = {
    "id": 13083,
    "name": "GitLab FOSS",
    "path": "gitlab-foss",
    "path_with_namespace": "gitlab-org/gitlab-foss",
    "web_url": "https://gitlab.com/gitlab-org/gitlab-foss",
    "star_count": 7167,
    "forks_count": 8260,
    "created_at": "2013-09-26T06:02:36.000Z",
    "last_activity_at": "2026-08-06T09:47:27.070Z",
    "description": "GitLab FOSS is a read-only mirror of GitLab.",
}


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("BATCH_SIZE", "2")
    return Config()


def stub(extractor, responses):
    queue = list(responses)
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return queue.pop(0) if queue else FakeResponse([])

    extractor.session.get = fake_get
    return calls


def test_maps_a_project_to_the_standard_schema(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse([PROJECT])])

    (row,) = extractor.fetch()
    assert row["id"] == "gitlab_13083"
    assert row["source"] == "gitlab"
    assert row["url"] == "https://gitlab.com/gitlab-org/gitlab-foss"
    assert row["stars"] == 7167
    assert row["forks"] == 8260
    assert row["created_at"] == "2013-09-26T06:02:36.000Z"
    assert row["updated_at"] == "2026-08-06T09:47:27.070Z"


def test_the_slug_is_used_as_the_name_not_the_display_title(config):
    # - GitLab's `name` is "GitLab FOSS"; `path` is the slug, which is what
    #   GitHub calls name.
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse([PROJECT])])
    assert extractor.fetch()[0]["name"] == "gitlab-foss"


def test_a_project_without_a_path_falls_back_to_its_name(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse([dict(PROJECT, path=None)])])
    assert extractor.fetch()[0]["name"] == "GitLab FOSS"


def test_language_is_left_empty(config):
    # - The listing doesn't carry it; fetching it costs a request per project.
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse([PROJECT])])
    assert extractor.fetch()[0]["language"] is None


def test_downloads_are_empty_for_a_repository_host(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse([PROJECT])])
    assert extractor.fetch()[0]["downloads"] is None


def test_projects_are_requested_most_starred_first(config):
    extractor = GitLabExtractor(config)
    calls = stub(extractor, [FakeResponse([PROJECT])])
    extractor.fetch()
    assert calls[0][1]["params"]["order_by"] == "star_count"
    assert calls[0][1]["params"]["sort"] == "desc"


def test_paging_stops_at_the_batch_size(config, monkeypatch):
    monkeypatch.setenv("BATCH_SIZE", "150")
    extractor = GitLabExtractor(Config())
    page_one = FakeResponse([dict(PROJECT, id=i) for i in range(100)])
    page_two = FakeResponse([dict(PROJECT, id=i) for i in range(100, 200)])
    calls = stub(extractor, [page_one, page_two])

    rows = extractor.fetch()
    assert len(rows) == 150
    assert calls[1][1]["params"]["per_page"] == 50


def test_an_empty_page_ends_the_run(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse([])])
    assert extractor.fetch() == []


def test_the_token_is_sent_when_configured(config, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-example")
    extractor = GitLabExtractor(Config())
    calls = stub(extractor, [FakeResponse([PROJECT])])
    extractor.fetch()
    assert calls[0][1]["headers"]["PRIVATE-TOKEN"] == "glpat-example"


def test_it_works_without_a_token(config):
    extractor = GitLabExtractor(config)
    calls = stub(extractor, [FakeResponse([PROJECT])])
    extractor.fetch()
    assert calls[0][1]["headers"] == {}


def test_rate_limiting_says_when_it_lifts(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse(status_code=429, headers={"RateLimit-Reset": "1786009860"})])
    with pytest.raises(ExtractError, match="rate limited, resets at 1786009860"):
        extractor.fetch()


def test_a_rejected_token_says_so(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse(status_code=401)])
    with pytest.raises(ExtractError, match="token rejected"):
        extractor.fetch()


def test_an_error_object_instead_of_a_list_is_reported_clearly(config):
    # - A 200 carrying a dict would otherwise be iterated as field names and
    #   fail with an AttributeError that says nothing about what went wrong.
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse({"message": "403 Forbidden"})])
    with pytest.raises(ExtractError, match="expected a list of projects"):
        extractor.fetch()


def test_other_failures_surface_as_extract_errors(config):
    extractor = GitLabExtractor(config)
    stub(extractor, [FakeResponse(status_code=503)])
    with pytest.raises(ExtractError, match="returned 503"):
        extractor.fetch()
