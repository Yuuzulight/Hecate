# Metrics

The exporter serves Prometheus metrics on port 8000. Locally:

```bash
python -m pipeline.server
curl localhost:8000/metrics
```

On the cluster it runs as the `hecate-metrics` deployment, discovered by Prometheus through the `prometheus.io/scrape` annotation on its pods.

## What's exported

### Read back from the database

These two are queried from stored data on a timer rather than counted during a run. That's the point of them: a scheduled job's counters live in a pod that runs for seconds once a day, so by the time anything scrapes there's nothing left to see. Reading from the table gives you something true between runs.

| metric | type | labels |
|---|---|---|
| `hecate_repositories` | gauge | `source` |
| `hecate_last_extraction_age_seconds` | gauge | `source` |

`hecate_last_extraction_age_seconds` is the one to watch. Rising steadily across every source means the scheduled job has stopped running, which otherwise fails silently — nothing errors, the data just quietly ages.

### Counted while the pipeline runs

Only populated inside a job's own lifetime, so useful in logs and in a job's final scrape, less so on a dashboard.

| metric | type | labels |
|---|---|---|
| `hecate_rows_processed_total` | counter | `stage`, `source` |
| `hecate_errors_total` | counter | `type`, `source` |
| `hecate_quality_checks_total` | counter | `check`, `outcome` |
| `hecate_extract_duration_seconds` | histogram | `source` |
| `hecate_transform_duration_seconds` | histogram | `source` |
| `hecate_load_duration_seconds` | histogram | — |

`stage` is one of extract, transform, load. `type` on the error counter is extract, transform, load or database.

## Queries

Total collected. `max by (source)` first because every replica reports the same figure, so summing raw would multiply by the replica count:

```promql
sum(max by (source) (hecate_repositories))
```

Hours since each source was last read:

```promql
max by (source) (hecate_last_extraction_age_seconds) / 3600
```

Anything that hasn't been read in over a day, which is the alert worth having:

```promql
max by (source) (hecate_last_extraction_age_seconds) > 86400
```

Error rate by kind:

```promql
sum by (type) (rate(hecate_errors_total[5m]))
```

Load latency percentiles:

```promql
histogram_quantile(0.95, sum(rate(hecate_load_duration_seconds_bucket[5m])) by (le))
```

Proportion of quality checks passing:

```promql
sum(rate(hecate_quality_checks_total{outcome="pass"}[1h]))
  / sum(rate(hecate_quality_checks_total[1h]))
```

Which check is failing:

```promql
sum by (check) (rate(hecate_quality_checks_total{outcome="fail"}[1h])) > 0
```

## Reading the dashboard

The Grafana dashboard draws from two datasources, and it's worth knowing which panel comes from where.

Prometheus feeds the counts, extraction ages, error rate, latency and pod count. Postgres feeds repositories by language, most starred, and field coverage — those are questions about the collected data, and they belong to the dbt marts rather than to a metrics store.

**Hours since last extraction** turns yellow past 26 hours and red past 50. The job runs daily, so a little over a day means one run missed and two days means something is properly wrong.

**Field coverage by source** is the panel to check before trusting any cross-source comparison. It shows how many rows from each source actually carry stars, downloads, a language and a creation date. The gaps are large and deliberate: npm reports no stars, GitHub and GitLab report no downloads. Comparing an average across sources without looking at this first gives you a number that means nothing.

**Errors per second** staying flat at zero while extraction age climbs is the interesting failure. It means nothing is failing because nothing is running.
