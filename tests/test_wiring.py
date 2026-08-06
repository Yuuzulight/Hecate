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
from pipeline.main import EXTRACTORS


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
    missing = implemented() - set(EXTRACTORS)
    assert not missing, (
        "written but never registered in main.EXTRACTORS: "
        + ", ".join(sorted(c.__name__ for c in missing))
    )


def test_nothing_is_registered_twice():
    assert len(EXTRACTORS) == len(set(EXTRACTORS))


def test_every_registered_extractor_declares_a_source():
    for extractor in EXTRACTORS:
        assert getattr(extractor, "source", None), f"{extractor.__name__} has no source"


def test_sources_are_the_ones_the_transformer_accepts():
    from pipeline.transformer import SOURCES

    assert {e.source for e in EXTRACTORS} <= set(SOURCES)


def test_source_names_are_unique():
    names = [e.source for e in EXTRACTORS]
    assert len(names) == len(set(names))
