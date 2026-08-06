"""Prometheus metrics for the pipeline.

Module-level singletons - importing this more than once reuses the same
collectors, which is what prometheus_client expects. The HTTP endpoint that
exposes them is set up separately.
"""

from prometheus_client import Counter, Gauge, Histogram

rows_processed = Counter(
    "hecate_rows_processed_total",
    "Records handled, counted at each stage of the pipeline",
    ["stage", "source"],
)

extract_duration = Histogram(
    "hecate_extract_duration_seconds",
    "Time spent fetching from a source",
    ["source"],
)

transform_duration = Histogram(
    "hecate_transform_duration_seconds",
    "Time spent normalising records to the standard schema",
    ["source"],
)

load_duration = Histogram(
    "hecate_load_duration_seconds",
    "Time spent writing a batch to PostgreSQL",
)

errors = Counter(
    "hecate_errors_total",
    "Errors, by the stage that raised them",
    ["type", "source"],
)

batch_size = Gauge(
    "hecate_batch_size",
    "Size of the batch currently being processed",
)

# - Read back off the database rather than counted as the pipeline runs. A
#   scheduled job's counters die with its pod, so anything you want to look at
#   between runs has to come from the stored data.
repositories = Gauge(
    "hecate_repositories",
    "Rows currently stored, by source",
    ["source"],
)

quality_checks = Counter(
    "hecate_quality_checks_total",
    "Data quality checks run after load, by check and outcome",
    ["check", "outcome"],
)

last_extraction_age = Gauge(
    "hecate_last_extraction_age_seconds",
    "Seconds since this source was last extracted successfully",
    ["source"],
)
