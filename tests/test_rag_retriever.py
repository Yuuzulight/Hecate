"""Retriever: entity resolution and context assembly.

Every query here goes through a stand-in `_rows`, so these run without a
database. Whether the SQL returns what it claims is a question a stub cannot
answer - the marts it reads are built by dbt, which the CI database does not
have, so that is checked by hand against the cluster.
"""

import pytest

from pipeline.config import Config
from pipeline.exceptions import LoadError
from pipeline.rag.retriever import MAX_NAMED_REPOSITORIES, WarehouseRetriever


@pytest.fixture
def config(monkeypatch):
    for name in ("DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PASSWORD", "secret")
    return Config()


@pytest.fixture
def retriever(config, monkeypatch):
    """A retriever whose every query returns a canned answer.

    Keyed on a fragment of the SQL so each block can be given its own rows
    without caring what order they run in.
    """
    r = WarehouseRetriever(config)
    # - Fragments have to be unique to one query. Several of these read the
    #   growth mart, so matching on the table name alone hands the profile
    #   query the fastest-growing rows and the test passes for the wrong
    #   reason - which is what happened first time.
    answers = {
        "lower(r.name) = ANY": [
            {"id": "github_1", "name": "vite", "source": "github", "stars": 82239},
            {"id": "npm_vite", "name": "vite", "source": "npm", "stars": 0},
        ],
        "FROM raw_repositories)": [{"repositories": 2013, "snapshot_days": 2}],
        "dim_sources": [{"source": "github", "with_stars": 508}],
        "GROUP BY r.language": [{"language": "Python", "stars_gained_1d": 6081}],
        "ORDER BY g.stars_gained_1d DESC": [{"name": "skills", "stars_gained_1d": 207}],
        "fct_repository_mentions m": [{"name": "certo", "posts": 1}],
        "fct_undiscovered_mentions": [{"project": "certo", "posts": 1}],
        "stg_repositories": [{"name": "atom", "days_since_update": 1312}],
        "dim_languages": [{"language_display": "Python"}, {"language_display": "Go"},
                          {"language_display": "C++"}],
        "DISTINCT lower(name)": [{"name": "tensorflow"}, {"name": "axum"},
                                 {"name": "vite"}],
    }

    def rows(sql, params=()):
        for fragment, result in answers.items():
            if fragment in sql:
                return result
        raise AssertionError(f"unstubbed query: {sql[:80]}")

    monkeypatch.setattr(r, "_rows", rows)
    return r


def test_rows_without_a_connection_is_an_error(config):
    with pytest.raises(LoadError):
        WarehouseRetriever(config)._rows("SELECT 1")


def test_context_carries_every_general_block(retriever):
    context = retriever.context_for("what is trending")
    for block in ("coverage", "sources", "language_growth", "fastest_growing",
                  "most_discussed", "undiscovered", "stale_but_popular"):
        assert block in context


def test_coverage_reports_how_much_history_there_is(retriever):
    # - The model has to tell "no growth" from "not enough days to say", and
    #   this is the only thing in the context that lets it.
    assert retriever.context_for("anything")["coverage"]["snapshot_days"] == 2


def test_a_named_language_is_flagged(retriever):
    assert retriever.context_for("what drove Python growth")["languages_asked_about"] == ["Python"]


def test_possessives_and_punctuation_still_match_a_language(retriever):
    # - "Python's" and "Python?" are how people actually write the question.
    for question in ("what drove Python's growth", "how is Python?", "Python"):
        assert "Python" in retriever.languages_named_in(question)


def test_a_language_inside_a_longer_word_does_not_match(retriever):
    # - Without a boundary check "Go" matches "going", "Google" and "ago".
    assert retriever.languages_named_in("going to Google this, it was ages ago") == []


def test_a_language_with_punctuation_in_its_name_matches(retriever):
    assert "C++" in retriever.languages_named_in("is C++ still growing")


def test_a_named_project_returns_every_source_carrying_that_name(retriever):
    # - `vite` is an npm package and a GitHub repository. Answering with one of
    #   them silently picks a side; grouping the two together is what produced
    #   "82,239 stars gained in a day" on the dashboard once already.
    profiles = retriever.context_for("how is vite doing")["repositories_asked_about"]
    assert {p["source"] for p in profiles} == {"github", "npm"}


def test_too_many_named_projects_returns_none(retriever, monkeypatch):
    monkeypatch.setattr(
        retriever, "repositories_named_in",
        lambda q: [f"project{i}" for i in range(MAX_NAMED_REPOSITORIES + 1)],
    )
    assert "repositories_asked_about" not in retriever.context_for("compare all of these")


def test_a_question_naming_nothing_gets_no_entity_blocks(retriever):
    context = retriever.context_for("what is going on")
    assert "languages_asked_about" not in context
    assert "repositories_asked_about" not in context


def test_profiles_for_nothing_does_not_query(retriever):
    assert retriever.profiles_for([]) == []


@pytest.mark.parametrize("method", ["most_discussed", "profiles_for"])
def test_mentions_are_aggregated_before_they_are_read(config, monkeypatch, method):
    """The mentions mart has a row per repository per week.

    Reading it without aggregating ranks weeks rather than projects, and
    multiplies a profile by the number of weeks it was discussed. Both
    happened. A stub cannot see the duplicate rows a real database would
    return, so this asserts the grouping is still in the SQL - narrow, but it
    catches the revert that would reintroduce it.
    """
    r = WarehouseRetriever(config)
    seen = []
    monkeypatch.setattr(r, "_rows", lambda sql, params=(): seen.append(sql) or [])
    getattr(r, method)(["vite"]) if method == "profiles_for" else getattr(r, method)()
    assert "GROUP BY" in seen[0]


def test_the_limit_reaches_the_query(config, monkeypatch):
    r = WarehouseRetriever(config)
    seen = []
    monkeypatch.setattr(r, "_rows", lambda sql, params=(): seen.append(params) or [])
    r.fastest_growing(3)
    assert seen == [(3,)]
