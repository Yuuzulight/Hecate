# Data schema

Three layers. The pipeline writes `raw_repositories`; dbt builds a staging view over it and a small star schema on top of that.

```
public.raw_repositories          written by the pipeline
  └── analytics_staging.stg_repositories     view, adds derived columns
        └── analytics_marts.fct_repositories  incremental table
            ├── dim_languages
            └── dim_sources
```

## raw_repositories

One row per repository or package, keyed on `id`. The loader upserts, so re-running refreshes rather than appends.

| column | type | notes |
|---|---|---|
| `id` | varchar | primary key, source-prefixed: `github_123456`, `npm_tslib` |
| `source` | varchar | github, npm, pypi or gitlab |
| `name` | varchar | repository slug or package name |
| `url` | varchar | canonical page |
| `stars` | integer | 0 for the package registries |
| `forks` | integer | 0 for the package registries |
| `language` | varchar | null where the source doesn't report one |
| `created_at` | timestamptz | first published, null for npm |
| `updated_at` | timestamptz | last activity — see the note below, it isn't the same thing on every source |
| `description` | text | |
| `downloads` | bigint | weekly, npm only. Null elsewhere |
| `extracted_at` | timestamptz | when the pipeline read it |
| `loaded_at` | timestamptz | when it was written |

`id`, `source` and `created_at` are never rewritten by an upsert. Everything else refreshes, including `name` and `url`, which change when a project is renamed.

### Null is not zero

The most important thing about this table. Where a source doesn't report a field, it's null, not zero:

| | stars | downloads | language | created_at |
|---|---|---|---|---|
| github | yes | null | usually | yes |
| gitlab | yes | null | null | yes |
| npm | 0 | yes | null | null |
| pypi | 0 | null | yes | yes |

`stars` is the exception, stored as 0 for packages rather than null, because the column is non-null by schema. Use `popularity_tier` from the staging layer instead, which is null for sources that don't have stars. Any query averaging `stars` across sources without filtering will be wrong.

## stg_repositories

Everything above, plus:

| column | notes |
|---|---|
| `popularity_tier` | exceptional / high / moderate / emerging, by star count. **Null for npm and pypi** |
| `language_normalized` | lowercased and trimmed, null when blank. Group on this |
| `creation_year` | year of first publication |
| `days_since_update` | days since last activity |

`days_since_update` is the maintenance signal. A large value next to a high star count is roughly the shape of an abandoned project.

**It doesn't mean quite the same thing on every source, though**, and that matters if you compare across them:

| source | what it measures |
|---|---|
| GitHub | last commit (`pushed_at`) |
| npm | last publish |
| PyPI | last release upload |
| GitLab | **last activity of any kind** — an issue comment counts |

The first three are all "someone shipped something". GitLab's is looser: a project nobody has committed to in two years still looks fresh if people are filing issues. GitLab's project listing doesn't carry a push timestamp, and getting one costs an extra request per project, so this is a known gap rather than an oversight — treat GitLab staleness as a weaker signal than the other three.

GitHub deserves a note of its own. It also exposes `updated_at`, which sounds like the right field and isn't: it moves whenever the repository *record* changes, and that includes someone starring it. For a popular project it reads as today, permanently. Using it made every one of the 500 most-starred repositories look actively maintained, which is why this uses `pushed_at`.

## fct_repositories

One row per project, incremental on `extracted_at`, merged on `repository_key`.

| column | notes |
|---|---|
| `repository_key` | surrogate key over id and source |
| `source_key` | joins `dim_sources` |
| `language_key` | joins `dim_languages`, null where no language |
| `stars_per_fork` | stars over forks, two places. Null when there are no forks |

Plus the measures and descriptive columns carried through from staging.

## Dimensions

**dim_languages** — one row per language, with `repository_count`, `avg_stars`, `median_stars`, `max_stars`. Covers GitHub and PyPI, the two sources that report a language.

The star figures are computed only over sources that have stars, and `starred_count` says how many rows that was. Without that, a language with more packages than repositories gets a median of zero and an average diluted towards it — Python read as a median of 0 across 14 rows until the aggregate was restricted to the 4 that actually carry stars.

**dim_sources** — one row per source. Alongside the aggregates it carries `with_stars`, `with_downloads`, `with_language` and `with_created_at`, which say how many rows from that source actually populate each field. Check this before any cross-source comparison.

There's no date dimension. There was one, and nothing ever joined to it — the fact table carries no date key and no panel used it, so it was 2,400 rows rebuilt every run to answer questions nobody asked. If a genuine time series shows up, a spine can come back with it.

## Queries

Which languages carry the most weight:

```sql
SELECT language_display, repository_count, median_stars, max_stars
FROM analytics_marts.dim_languages
ORDER BY repository_count DESC
LIMIT 10;
```

Popular but possibly abandoned — high stars, no activity in a year:

```sql
SELECT name, stars, days_since_update
FROM analytics_marts.fct_repositories
WHERE stars > 10000 AND days_since_update > 365
ORDER BY stars DESC;
```

That one usually comes back empty on a small sample, since the default collection is the most-starred projects and those tend to be actively maintained. Raise `BATCH_SIZE` and it starts finding things.

Heavily used but rarely forked, which tends to mean a dependency rather than something people build on:

```sql
SELECT name, stars, forks, stars_per_fork
FROM analytics_marts.fct_repositories
WHERE forks > 100
ORDER BY stars_per_fork DESC
LIMIT 10;
```

Most installed packages, which is a different question from most starred:

```sql
SELECT name, downloads
FROM analytics_marts.fct_repositories
WHERE downloads IS NOT NULL
ORDER BY downloads DESC
LIMIT 10;
```

What each source actually gives you, before comparing anything across them:

```sql
SELECT source, repository_count, with_stars, with_downloads,
       with_language, avg_days_since_update
FROM analytics_marts.dim_sources
ORDER BY source;
```

How old the collection is, per source:

```sql
SELECT s.source,
       max(f.extracted_at) AS last_run,
       now() - max(f.extracted_at) AS age
FROM analytics_marts.fct_repositories f
JOIN analytics_marts.dim_sources s ON f.source_key = s.source_key
GROUP BY s.source
ORDER BY s.source;
```

The fact table carries `source_key` rather than `source`, so anything grouping by source joins `dim_sources`.
