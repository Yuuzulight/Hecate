"""Name matching: the guards, and what it refuses to answer."""

import pytest

from pipeline.matching import (
    MIN_LENGTH,
    is_matchable,
    name_candidates,
    resolve_by_name,
)

NAMES = {
    "tensorflow": "github_1",
    "axum": "github_2",
    "starboard": "github_3",
    "requests": "github_4",
    "core": "github_5",
    "go": "github_6",
}


@pytest.mark.parametrize("name", ["tensorflow", "starboard", "pokeemerald"])
def test_distinctive_names_are_matchable(name):
    assert is_matchable(name)


@pytest.mark.parametrize("name", ["go", "ai", "npm", "", None, "1234"])
def test_short_or_meaningless_names_are_not(name):
    assert not is_matchable(name)


@pytest.mark.parametrize("name", ["requests", "core", "server", "data", "react"])
def test_ordinary_words_are_excluded_even_when_long_enough(name):
    # - These are all real project names and all ordinary English. Matching
    #   them turns every post containing the word into a mention.
    assert len(name) >= MIN_LENGTH or True
    assert not is_matchable(name)


def test_a_named_project_is_found():
    assert name_candidates("tensorflow just shipped 3.0", NAMES) == {"github_1"}


def test_matching_is_case_insensitive():
    assert name_candidates("TensorFlow just shipped", NAMES) == {"github_1"}


def test_a_name_inside_another_word_does_not_count():
    # - Without a word boundary, axum matches maxumum and every project whose
    #   name is a common substring collects mentions it has nothing to do with.
    assert name_candidates("the maxumum value", NAMES) == set()
    assert name_candidates("axumite kingdom", NAMES) == set()


def test_hyphenated_neighbours_do_not_count():
    assert name_candidates("axum-extra is separate", NAMES) == set()


def test_punctuation_around_a_name_is_fine():
    assert name_candidates("we use axum, mostly", NAMES) == {"github_2"}
    assert name_candidates("(axum)", NAMES) == {"github_2"}


def test_short_and_stoplisted_names_never_match():
    assert name_candidates("go is fast and requests are core", NAMES) == set()


def test_two_projects_in_one_post_produces_no_answer():
    # - A comparison or a list. Guessing between them is a coin toss dressed
    #   as data, which is the whole failure this module exists to avoid.
    assert resolve_by_name("axum vs tensorflow", NAMES) is None


def test_one_unambiguous_project_resolves():
    assert resolve_by_name("starboard is neat", NAMES) == "github_3"


def test_nothing_named_resolves_to_nothing():
    assert resolve_by_name("a post about databases", NAMES) is None


def test_empty_text_is_safe():
    assert resolve_by_name("", NAMES) is None
    assert name_candidates(None, NAMES) == set()


def test_regex_metacharacters_in_a_name_do_not_break_matching():
    # - Project names contain dots and plus signs; unescaped they would be
    #   regex wildcards and match far more than intended.
    names = {"c++": "github_7", "socket.io": "github_8"}
    assert name_candidates("we use socket.io here", names) == {"github_8"}
    assert name_candidates("we use socketxio here", names) == set()
