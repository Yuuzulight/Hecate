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
        "hecate_repositories",
        "hecate_last_extraction_age_seconds",
        "hecate_quality_checks",
    }
    registered = {name for name in REGISTRY._names_to_collectors if name.startswith("hecate_")}
    assert expected <= registered


def test_counters_record_against_their_labels():
    # - Collectors are module-level singletons shared with every other test, so
    #   this has to measure the change, not the total.
    labels = {"stage": "extract", "source": "github"}
    before = REGISTRY.get_sample_value("hecate_rows_processed_total", labels) or 0
    metrics.rows_processed.labels(**labels).inc(5)
    after = REGISTRY.get_sample_value("hecate_rows_processed_total", labels)
    assert after - before == 5
