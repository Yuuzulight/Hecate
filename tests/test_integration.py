"""End to end through main(), with the sources and the database stood in for.

The point here is the wiring and the failure handling: does a record that comes
out of an extractor reach the loader intact, and does one broken source take
the whole run down.
"""

from unittest.mock import MagicMock

import pytest

from pipeline import main as main_module
from pipeline.config import Config
from pipeline.exceptions import ExtractError, LoadError
from pipeline.main import main, run

RAW = {
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


def fake_extractor(source, records=None, error=None):
    """A stand-in extractor class, since main() constructs its own."""

    class Fake:
        def __init__(self, config):
            self.source = source

        def extract(self):
            if error:
                raise error
            return list(records or [])

    return Fake


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    return Config()


@pytest.fixture
def loader(monkeypatch):
    """Swap the real loader out and hand back the stand-in main() will use."""
    stub = MagicMock()
    stub.load_repositories.side_effect = lambda rows: len(rows)
    monkeypatch.setattr(main_module, "PostgreSQLLoader", lambda config: stub)
    return stub


def use(monkeypatch, *extractors):
    monkeypatch.setattr(main_module, "EXTRACTORS", tuple(extractors))
    # - Cleared too, or run() builds the real mention extractors and calls a
    #   live API. Nothing in this suite is allowed near the network, and the
    #   only symptom was the run getting slower until CI timed out.
    monkeypatch.setattr(main_module, "MENTION_EXTRACTORS", ())


def test_a_record_survives_the_whole_trip(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))

    loaded, failed = run(config)
    assert loaded == 1
    assert failed == []

    (rows,) = loader.load_repositories.call_args.args
    assert rows[0]["id"] == "github_1"
    # - Normalised on the way through, not passed along as it arrived.
    assert rows[0]["created_at"] == "2015-11-09T01:02:03+00:00"


def test_the_table_is_created_before_anything_loads(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))
    run(config)
    loader.connect.assert_called_once()
    loader.create_tables.assert_called_once()


def test_a_failing_source_does_not_stop_the_others(config, loader, monkeypatch):
    use(
        monkeypatch,
        fake_extractor("github", error=ExtractError("rate limit exhausted")),
        fake_extractor("npm", [dict(RAW, id="npm_1", source="npm")]),
    )

    loaded, failed = run(config)
    assert loaded == 1
    assert failed == ["github"]


def test_unusable_records_are_dropped_not_loaded(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW, dict(RAW, id="github_2", url="nope")]))

    loaded, _ = run(config)
    assert loaded == 1


def test_the_connection_is_closed_even_when_a_source_fails(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", error=ExtractError("boom")))
    run(config)
    loader.close.assert_called_once()


def test_the_connection_is_closed_when_loading_blows_up(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))
    loader.load_repositories.side_effect = LoadError("disk full")

    run(config)
    loader.close.assert_called_once()


def test_a_partial_run_still_reports_success(config, loader, monkeypatch):
    use(
        monkeypatch,
        fake_extractor("github", error=ExtractError("boom")),
        fake_extractor("npm", [dict(RAW, id="npm_1", source="npm")]),
    )
    assert main() == 0


def test_every_source_failing_reports_failure(config, loader, monkeypatch):
    use(
        monkeypatch,
        fake_extractor("github", error=ExtractError("boom")),
        fake_extractor("npm", error=ExtractError("boom")),
    )
    assert main() == 1


def test_a_clean_run_reports_success(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))
    assert main() == 0


def test_an_unreachable_database_aborts_the_run(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))
    loader.connect.side_effect = LoadError("connection refused")

    assert main() == 1
    # - Nothing should have been attempted against a database we never reached.
    loader.load_repositories.assert_not_called()


def test_an_unexpected_error_costs_one_source_not_the_run(config, loader, monkeypatch):
    # - A malformed response raising KeyError deep in an extractor should not
    #   throw away every other source's data for the day.
    use(
        monkeypatch,
        fake_extractor("github", error=KeyError("stargazers_count")),
        fake_extractor("npm", [dict(RAW, id="npm_1", source="npm")]),
    )

    loaded, failed = run(config)
    assert loaded == 1
    assert failed == ["github"]


def test_quality_checks_run_against_what_is_stored_not_what_was_sent(config, loader, monkeypatch):
    # - Reading back is the whole point: a loader that dropped or mangled a row
    #   would otherwise be invisible, because the batch in memory still looks
    #   perfect.
    use(monkeypatch, fake_extractor("github", [RAW]))
    loader.rows_for.return_value = [dict(RAW, id="github_stored")]

    seen = {}
    monkeypatch.setattr(
        main_module.RepositoryExpectations, "validate",
        lambda self, rows: seen.update(rows=rows) or {},
    )
    run(config)

    loader.rows_for.assert_called_once_with("github")
    assert seen["rows"][0]["id"] == "github_stored"


def test_a_broken_quality_check_does_not_fail_a_source_whose_rows_landed(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", [RAW]))
    loader.rows_for.side_effect = RuntimeError("cannot read back")

    loaded, failed = run(config)
    # - The rows are in the database. Failing to report on them is not the same
    #   as failing to collect them.
    assert loaded == 1
    assert failed == []


def test_an_empty_source_is_not_a_failure(config, loader, monkeypatch):
    use(monkeypatch, fake_extractor("github", []))
    loaded, failed = run(config)
    assert loaded == 0
    assert failed == []


def test_main_can_be_run_as_a_module():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import pipeline.main; print(pipeline.main.main.__name__)"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "main" in result.stdout
