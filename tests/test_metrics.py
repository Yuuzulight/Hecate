"""Importing the metrics module registers every collector exactly once.

Two collectors sharing a name raises at import time, so this is the check that
catches a copy-pasted metric name before it reaches a running pod.
"""

from prometheus_client import REGISTRY

from pipeline import metrics


def test_every_metric_is_registered():
    expected = {
        "hecate_rows_processed",
        "hecate_extract_duration_seconds",
        "hecate_transform_duration_seconds",
        "hecate_load_duration_seconds",
        "hecate_errors",
        "hecate_batch_size",
    }
    registered = {name for name in REGISTRY._names_to_collectors if name.startswith("hecate_")}
    assert expected <= registered


def test_counters_record_against_their_labels():
    metrics.rows_processed.labels(stage="extract", source="github").inc(5)
    value = REGISTRY.get_sample_value(
        "hecate_rows_processed_total", {"stage": "extract", "source": "github"}
    )
    assert value == 5
