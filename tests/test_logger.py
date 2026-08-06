"""Log output has to be one parseable JSON object per line."""

import json
import logging
import sys

from pipeline.logger import JsonFormatter, get_logger


def _format(message="extracted", context=None, exc_info=None, name="hecate.check"):
    record = logging.LogRecord(name, logging.INFO, __file__, 1, message, None, exc_info)
    if context is not None:
        record.context = context
    return json.loads(JsonFormatter().format(record))


def test_output_is_json_with_the_expected_fields():
    entry = _format()
    assert entry["level"] == "INFO"
    assert entry["message"] == "extracted"
    assert entry["logger"] == "hecate.check"
    assert entry["timestamp"].endswith("+00:00")


def test_extra_context_is_merged_into_the_object():
    entry = _format(context={"source": "github", "rows": 100})
    assert entry["source"] == "github"
    assert entry["rows"] == 100


def test_context_that_is_not_a_dict_is_ignored():
    assert "context" not in _format(context="github")


def test_unserialisable_context_does_not_blow_up():
    assert "object at" in _format(context={"when": object()})["when"]


def test_exception_text_is_captured():
    try:
        raise ValueError("nope")
    except ValueError:
        entry = _format(message="failed", exc_info=sys.exc_info())
    assert entry["message"] == "failed"
    assert "ValueError: nope" in entry["exception"]


def test_logger_is_attached_to_the_hecate_tree():
    log = get_logger("check")
    assert log.name == "hecate.check"
    root = logging.getLogger("hecate")
    # - One handler, and propagate off, or every line would be emitted twice.
    assert len(root.handlers) == 1
    assert root.propagate is False


def test_names_outside_the_hecate_tree_are_still_reparented():
    assert get_logger("hecatecombing").name == "hecate.hecatecombing"


def test_the_hecate_logger_itself_is_not_renamed():
    assert get_logger("hecate").name == "hecate"
    assert get_logger("hecate.extractors").name == "hecate.extractors"
