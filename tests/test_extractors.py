"""GitHub extractor: schema mapping, paging, and the failure paths.

Nothing here touches the network. Every response is a stand-in built below.
"""

import pytest
import requests

from pipeline.config import Config
from pipeline.exceptions import ExtractError
from pipeline.extractors.github import GitHubExtractor

REPO = {
    "id": 123456,
    "name": "tensorflow",
    "html_url": "https://github.com/tensorflow/tensorflow",
    "stargazers_count": 185432,
    "forks_count": 74521,
    "language": "C++",
    "created_at": "2015-11-09T01:02:03Z",
    "updated_at": "2024-08-06T04:05:06Z",
    "pushed_at": "2024-07-01T09:00:00Z",
    "description": "An open source machine learning framework",
}


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        # - Not `payload or {}`. An empty list is falsy, so that turned every
        #   "no more results" response into a dict, which is a different shape
        #   from anything the real API returns.
        self._payload = {} if payload is None else payload
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("BATCH_SIZE", "2")
    return Config()


def stub_session(extractor, responses):
    """Answer each successive GET with the next response, recording the calls."""
    calls = []
    queue = list(responses)

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return queue.pop(0) if queue else FakeResponse({"items": []})

    extractor.session.get = fake_get
    return calls


def test_fetch_maps_the_api_shape_to_the_standard_schema(config):
    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse({"items": [REPO]})])

    (row,) = extractor.fetch()
    assert row["id"] == "github_123456"
    assert row["source"] == "github"
    assert row["name"] == "tensorflow"
    assert row["url"] == "https://github.com/tensorflow/tensorflow"
    assert row["stars"] == 185432
    assert row["forks"] == 74521
    assert row["language"] == "C++"
    assert row["created_at"] == "2015-11-09T01:02:03Z"
    # - pushed_at, because updated_at moves when someone stars the repo.
    assert row["updated_at"] == "2024-07-01T09:00:00Z"
    assert row["description"].startswith("An open source")
    assert row["extracted_at"].endswith("+00:00")


def test_updated_at_falls_back_when_there_is_no_push(config):
    extractor = GitHubExtractor(config)
    without_push = {k: v for k, v in REPO.items() if k != "pushed_at"}
    stub_session(extractor, [FakeResponse({"items": [without_push]})])
    assert extractor.fetch()[0]["updated_at"] == "2024-08-06T04:05:06Z"


def test_missing_optional_fields_do_not_raise(config):
    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse({"items": [{"id": 1}]})])

    (row,) = extractor.fetch()
    assert row["id"] == "github_1"
    assert row["stars"] == 0
    assert row["forks"] == 0
    assert row["language"] is None


def test_fetch_stops_once_batch_size_is_reached(config, monkeypatch):
    monkeypatch.setenv("BATCH_SIZE", "150")
    extractor = GitHubExtractor(Config())
    page_one = FakeResponse({"items": [dict(REPO, id=i) for i in range(100)]})
    page_two = FakeResponse({"items": [dict(REPO, id=i) for i in range(100, 200)]})
    calls = stub_session(extractor, [page_one, page_two])

    rows = extractor.fetch()
    assert len(rows) == 150
    assert len(calls) == 2
    assert calls[0][1]["params"]["page"] == 1
    assert calls[1][1]["params"]["per_page"] == 50


def test_an_empty_page_ends_the_run(config):
    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse({"items": []})])
    assert extractor.fetch() == []


def test_exhausted_rate_limit_is_reported_as_such(config):
    extractor = GitHubExtractor(config)
    stub_session(
        extractor,
        [FakeResponse(status_code=403, headers={"X-RateLimit-Remaining": "0",
                                                "X-RateLimit-Reset": "1754438400"})],
    )
    with pytest.raises(ExtractError, match="rate limit"):
        extractor.fetch()


def test_a_403_that_is_not_the_rate_limit_is_a_plain_error(config):
    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse(status_code=403,
                                          headers={"X-RateLimit-Remaining": "42"})])
    with pytest.raises(ExtractError, match="returned 403"):
        extractor.fetch()


def test_server_errors_surface_as_extract_errors(config):
    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse(status_code=500)])
    with pytest.raises(ExtractError, match="returned 500"):
        extractor.fetch()


def test_the_token_is_sent_when_one_is_configured(config, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    extractor = GitHubExtractor(Config())
    calls = stub_session(extractor, [FakeResponse({"items": [REPO]})])

    extractor.fetch()
    assert calls[0][1]["headers"]["Authorization"] == "Bearer ghp_example"


def test_no_auth_header_without_a_token(config):
    extractor = GitHubExtractor(config)
    calls = stub_session(extractor, [FakeResponse({"items": [REPO]})])

    extractor.fetch()
    assert "Authorization" not in calls[0][1]["headers"]


def test_requests_are_bounded_by_a_timeout(config):
    extractor = GitHubExtractor(config)
    calls = stub_session(extractor, [FakeResponse({"items": [REPO]})])

    extractor.fetch()
    assert calls[0][1]["timeout"] == 10


def test_extract_wraps_transport_failures(config):
    extractor = GitHubExtractor(config)

    def blow_up(url, **kwargs):
        raise requests.ConnectionError("no route to host")

    extractor.session.get = blow_up
    with pytest.raises(ExtractError, match="request failed"):
        extractor.extract()


def test_extract_counts_an_extraction_failure(config):
    from prometheus_client import REGISTRY

    labels = {"type": "extract", "source": "github"}
    before = REGISTRY.get_sample_value("hecate_errors_total", labels) or 0

    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse(status_code=500)])
    with pytest.raises(ExtractError):
        extractor.extract()

    after = REGISTRY.get_sample_value("hecate_errors_total", labels)
    assert after - before == 1


def test_extract_counts_the_rows_it_returned(config):
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value(
        "hecate_rows_processed_total", {"stage": "extract", "source": "github"}
    ) or 0

    extractor = GitHubExtractor(config)
    stub_session(extractor, [FakeResponse({"items": [REPO, dict(REPO, id=2)]})])
    extractor.extract()

    after = REGISTRY.get_sample_value(
        "hecate_rows_processed_total", {"stage": "extract", "source": "github"}
    )
    assert after - before == 2


def test_retries_are_configured_on_the_session(config):
    extractor = GitHubExtractor(config)
    retry = extractor.session.get_adapter("https://api.github.com").max_retries
    assert retry.total == config.retry_attempts
    assert 429 in retry.status_forcelist
    assert retry.backoff_factor > 0
