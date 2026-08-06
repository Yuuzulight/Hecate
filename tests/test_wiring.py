"""The pipeline runs whatever is in main.EXTRACTORS, and nothing else.

Every other test replaces that tuple with stand-ins, which means an extractor
could be written, tested and merged without ever being wired in - and the only
symptom would be the pipeline quietly collecting from fewer sources than it
has. This is the test that notices.
"""

import importlib
import inspect
import pkgutil

import pipeline.extractors
from pipeline.extractors.base import Extractor
from pipeline.main import EXTRACTORS, MENTION_EXTRACTORS

REGISTERED = EXTRACTORS + MENTION_EXTRACTORS


def implemented():
    """Every concrete Extractor subclass under pipeline/extractors."""
    found = set()
    for module in pkgutil.iter_modules(pipeline.extractors.__path__):
        loaded = importlib.import_module(f"pipeline.extractors.{module.name}")
        for _, obj in inspect.getmembers(loaded, inspect.isclass):
            if issubclass(obj, Extractor) and obj is not Extractor:
                found.add(obj)
    return found


def test_every_extractor_that_exists_is_wired_in():
    missing = implemented() - set(REGISTERED)
    assert not missing, (
        "written but never registered in main.EXTRACTORS or MENTION_EXTRACTORS: "
        + ", ".join(sorted(c.__name__ for c in missing))
    )


def test_nothing_is_registered_twice():
    assert len(REGISTERED) == len(set(REGISTERED))


def test_every_registered_extractor_declares_a_source():
    for extractor in REGISTERED:
        assert getattr(extractor, "source", None), f"{extractor.__name__} has no source"


def test_repository_sources_are_the_ones_the_transformer_accepts():
    # - Only the repository extractors go through the transformer. Mention
    #   extractors bypass it entirely, so their source names are not required
    #   to be in that list and would fail this if they were included.
    from pipeline.transformer import SOURCES

    assert {e.source for e in EXTRACTORS} <= set(SOURCES)


def test_mention_extractors_are_not_repository_extractors():
    assert not set(MENTION_EXTRACTORS) & set(EXTRACTORS)


def test_the_default_registries_are_not_left_patched():
    # - A guard against the reverse of the bug that made CI hang: tests clear
    #   MENTION_EXTRACTORS to stay off the network, and a leaked patch would
    #   mean the real pipeline silently stopped collecting mentions.
    assert MENTION_EXTRACTORS, "MENTION_EXTRACTORS is empty outside a test patch"
    assert EXTRACTORS, "EXTRACTORS is empty outside a test patch"


def test_source_names_are_unique():
    names = [e.source for e in REGISTERED]
    assert len(names) == len(set(names))
