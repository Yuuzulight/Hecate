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

## The question-answering service

A separate exporter, on port 8001 rather than 8000, because these counters live in the API process and die with it — there's no scheduled-job problem to work around here, the service just runs while a question is being asked.

| metric | type | labels |
|---|---|---|
| `hecate_rag_questions_total` | counter | `outcome` (the answer's confidence) |
| `hecate_rag_tokens_total` | counter | `kind` (`input` / `output`) |
| `hecate_rag_cost_usd_total` | counter | — |
| `hecate_rag_context_cache_total` | counter | `result` (`hit` / `miss`) |

Tokens and cost cover more than the answer. When the evaluation harness runs, each question is two more real, billed calls on top of the one that answers it — the judge scoring faithfulness and relevance separately — and those feed the same two counters. The judge's client isn't the LangChain model the chain uses, so the token-reading code differs, but the destination doesn't: an answer's spend and its scoring's spend are one number, not two hidden from each other.

`hecate_rag_tokens_total` carries a `kind` label, so summing across it matters:

```promql
sum(increase(hecate_rag_tokens_total[24h])) > 1000000
```

Without the `sum()`, that expression evaluates as two independent series — input and output — each compared to the threshold on its own, and a day with 900k of each (1.8M total, well past "something is looping") never trips either series alone. This is `RagTokensHigh`, the free-tier alert: on the default provider (`RAG_PROVIDER=gemini`, priced at $0.00) `hecate_rag_cost_usd_total` never increments no matter how much is asked, so the dollar-based alert can't fire regardless of usage, and this is what actually reflects activity while running on Gemini.

## Reading the dashboard

The Grafana dashboard draws from two datasources, and it's worth knowing which panel comes from where.

Prometheus feeds the counts, extraction ages, error rate, latency and pod count. Postgres feeds repositories by language, most starred, and field coverage — those are questions about the collected data, and they belong to the dbt marts rather than to a metrics store.

**Hours since last extraction** turns yellow past 26 hours and red past 50. The job runs daily, so a little over a day means one run missed and two days means something is properly wrong.

**Field coverage by source** is the panel to check before trusting any cross-source comparison. It shows how many rows from each source actually carry stars, downloads, a language and a creation date. The gaps are large and deliberate: npm reports no stars, GitHub and GitLab report no downloads. Comparing an average across sources without looking at this first gives you a number that means nothing.

**Errors per second** staying flat at zero while extraction age climbs is the interesting failure. It means nothing is failing because nothing is running.

Five panels belong to the question-answering service specifically — spend and tokens per day, questions asked, answer quality over time, and hallucination rate. The last two read from `rag_evaluations`, a Postgres table the evaluation harness writes to, not from anything Prometheus scrapes. Answer quality and hallucination rate stay empty until that harness has actually run against real traffic; a service that's up and answering questions doesn't populate them on its own.
