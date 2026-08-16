# Data schema

Three layers. The pipeline writes `raw_repositories`; dbt builds a staging view over it and a small star schema on top of that.

```
public.raw_repositories          projects, written by the pipeline
public.social_mentions           posts about them, a different grain
public.repository_snapshots      one row per project per day, the only history
        |
  analytics_staging.stg_repositories       view, adds derived columns
  analytics_staging.stg_social_mentions    view, adds post age
        |
  analytics_marts.fct_repositories          incremental
  analytics_marts.fct_repository_mentions   attention per project per week
  analytics_marts.fct_undiscovered_mentions what is discussed but untracked
  analytics_marts.dim_languages
  analytics_marts.dim_sources
```

## social_mentions

One row per post that links or names a project. A **different grain** from `raw_repositories` — a post is an event *about* a project, not a project — which is why it lives separately rather than being folded in.

| column | notes |
|---|---|
| `id` | platform-prefixed, e.g. `hackernews_38294011` |
| `platform` | `hackernews` today; the table is platform-agnostic |
| `repository_id` | **nullable.** Null means the project isn't tracked |
| `target_url` | the project page the post pointed at, kept either way |
| `score`, `comments` | refreshed on reload, since they move as a post ages |
| `match_confidence` | `1.0` for a link, `0.5` for a name matched in prose |

**A null `repository_id` is the interesting case, not the broken one.** Every source is seeded by cumulative popularity, so a project being discussed before it's famous is excluded by the seeding itself. Those rows are the input to discovery, and `fct_undiscovered_mentions` ranks them.

`match_confidence` matters if you compute anything from mentions. Name matching is off by default and, when enabled, is wrong in ways that look plausible — filter to `>= 1.0` for figures that need to be defensible.

## repository_snapshots

One row per project per day. The **only** table with history — everything else upserts in place and describes now, which is why growth rate wasn't answerable before it existed.

| column | notes |
|---|---|
| `captured_on` | date, part of the primary key, so a re-run replaces rather than appends |
| `stars`, `forks`, `downloads` | as at that day, nulls preserved |
| `mention_count` | **cumulative**, not daily. Null means the mention extractors didn't run |

The daily figure is the difference between two snapshots. That's the point of the table.

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
| `open_issues_and_prs` | integer | GitHub only. Named for what it is — the API's count includes PRs |
| `archived` | boolean | GitHub only. Abandoned by declaration rather than inferred from a date |
| `is_fork` | boolean | GitHub only |
| `origin` | varchar | `discovered` if found by being discussed; null if seeded by a ranked query |
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

`repository_count` has a subtler version of the same problem, and it is the one to watch when comparing languages. Every PyPI package is Python by definition, so those rows say nothing about what anyone chose to write in — they say the row came from a Python package registry. Counting them put Python at 619 against TypeScript's 80, of which 502 were PyPI. `starred_count` is the honest comparison because it is restricted to the sources that report a language as an observation rather than a tautology; on the same data it reads Python 117, TypeScript 80.

**dim_sources** — one row per source. Alongside the aggregates it carries `with_stars`, `with_downloads`, `with_language` and `with_created_at`, which say how many rows from that source actually populate each field. Check this before any cross-source comparison.

There's no date dimension. There was one, and nothing ever joined to it — the fact table carries no date key and no panel used it, so it was 2,400 rows rebuilt every run to answer questions nobody asked. If a genuine time series shows up, a spine can come back with it.

## Queries

Which languages carry the most weight:

```sql
SELECT language_display, starred_count, median_stars, max_stars
FROM analytics_marts.dim_languages
WHERE starred_count > 0
ORDER BY starred_count DESC
LIMIT 10;
```

`starred_count` rather than `repository_count`, for the reason above.

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

What is being talked about that isn't tracked yet — the discovery queue:

```sql
SELECT target_url, posts, total_score, decayed_score
FROM analytics_marts.fct_undiscovered_mentions
ORDER BY decayed_score DESC
LIMIT 20;
```

Projects that entered the dataset by being discussed rather than by being popular:

```sql
SELECT name, stars, created_at::date
FROM analytics_marts.fct_repositories
WHERE origin = 'discovered'
ORDER BY stars DESC;
```

Star growth over the last week, which needs the snapshot table:

```sql
SELECT r.name, r.source,
       max(s.stars) - min(s.stars) AS gained
FROM repository_snapshots s
JOIN raw_repositories r ON r.id = s.repository_id
WHERE s.captured_on > current_date - 7
GROUP BY s.repository_id, r.name, r.source
HAVING max(s.stars) - min(s.stars) > 0
ORDER BY gained DESC
LIMIT 10;
```

That one returns nothing until there are at least two days of snapshots.

**Group by `repository_id`, not by name.** The same name exists across sources — `vite` is both an npm package and a GitHub repository — and grouping by name alone subtracts the package's zero from the repository's star count and reports the difference as growth. The first draft of this query did exactly that and claimed 82,239 stars gained in a day.

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
