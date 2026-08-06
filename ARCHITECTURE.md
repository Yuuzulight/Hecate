# Architecture

Notes on how Hecate is put together and why it ended up this way. Written as the decisions came up rather than as a plan that existed from the start, because that's closer to what happened.

## The shape of it

```
GitHub API    npm registry    PyPI JSON API    GitLab API
     |             |               |               |
     +-------------+-------+-------+---------------+
                           |
                     extractors          per-source, one shared base
                           |
                      transformer        one schema, whatever the origin
                           |
                   PostgreSQL loader     upsert, safe to re-run
                           |
                    raw_repositories
                      /          \
                 dbt models    metrics exporter
                     |               |
              staging view      /metrics :8000
                     |               |
             fact + dimensions   Prometheus
                     \               /
                        Grafana
```

Two things run on the cluster. A CronJob does the collecting once a day; a small Deployment serves metrics continuously. They're separate because they have nothing in common except the database.

## Storage first

Everything else was easier to reason about once there was somewhere to put the data, so PostgreSQL came first.

The important decision was making loads idempotent. The alternatives were appending every run and deduplicating later, or truncating and reloading. Appending means the table grows without bound and every query has to filter to the latest version of each row. Truncating means a run that dies halfway leaves you with less data than you started with.

`INSERT ... ON CONFLICT (id) DO UPDATE` avoids both. Run the pipeline twice and you get the same table as running it once. A run that crashes can just be run again, with no cleanup step and no working out where it stopped.

That decision shaped a lot downstream. Because there's exactly one row per id, the dbt staging model needs no deduplication. Because re-running is free, the CronJob can retry on failure without anyone thinking about it.

One thing that isn't obvious until it bites: Postgres refuses an upsert that would touch the same row twice in a single statement. Paging through a source that's being written to genuinely does hand back the same repository on two pages, so batches get deduplicated before they're sent.

Columns split into two groups. `id`, `source` and `created_at` describe what a thing is and are never rewritten. Everything else is a fact about right now and gets refreshed, including `name` and `url`, which move when a project is renamed.

## Four sources that agree on very little

GitHub came first and set the schema. Adding the others is where it got interesting, because the four sources have less in common than you'd hope.

GitHub and GitLab have stars and forks. npm and PyPI have downloads and no concept of a star. npm won't tell you what language a package is written in; GitLab will, but only from a separate endpoint per project, which is one extra HTTP request per row for a single field. PyPI knows when a package was first published, npm only knows when it was last.

The rule that fell out of this: **where a source doesn't report something, store null, not zero.** "This source doesn't measure that" and "the measured value is zero" are different claims, and collapsing them means every average computed later is quietly wrong. A package with 0 stars would drag down any stars-per-language figure it appeared in, and it wouldn't be visible as an error.

This applies in a few places:

- `downloads` is null for GitHub and GitLab, populated for npm
- `popularity_tier` is null for npm and PyPI rather than the lowest band, so packages don't sort below every repository in any ranking built on it
- `language` is null rather than guessed. Defaulting npm to JavaScript would have been convenient and made up.

`downloads` didn't exist in the schema until npm arrived and had nowhere to put its most useful number. It's `BIGINT` because the busiest packages clear a billion downloads a month, close enough to `INTEGER`'s ceiling to matter.

### Where the mapping happens

There are two plausible homes for turning a source's API shape into the standard schema, and it's worth being clear which does what.

Each extractor maps its own API. Only the GitHub extractor should know the field is called `stargazers_count` while GitLab calls it `star_count`. Putting that knowledge anywhere else means an API change needs editing in two files.

The transformer handles what has to be identical regardless of origin: required fields present, counts coerced to integers, timestamps converted to UTC rather than relabelled, URLs checked for an http scheme. A `javascript:` URL reaching a dashboard is worth stopping early, so non-http URLs are rejected outright.

### Neither registry ranks its packages

GitHub and GitLab will hand over their most-starred, in order. Neither npm nor PyPI has an equivalent, and npm's search rejects a wildcard query outright.

So those two are seeded. npm searches across a set of broad ecosystem keywords and ranks what comes back by weekly downloads; PyPI works from a hand-picked package list. This makes both samples shaped by their seed rather than a true top-N, which is a real limitation. A popular npm package tagged with none of the chosen keywords won't appear. Widening the lists widens the net, and both are module-level constants so it's a one-line change.

## Failures cost one source, not the run

Four external APIs means something is usually having a bad day. Each source runs in its own try block: a failure is logged and counted, and the run continues with the rest. The run only reports failure when every source failed.

The catch is deliberately broad rather than just the pipeline's own exception types. A malformed response raising `KeyError` deep in one extractor shouldn't cost a whole day of the other three. The traceback still reaches the log.

Retries come from urllib3's `Retry` on the session rather than a hand-rolled loop, with `raise_on_status` off so an exhausted retry surfaces as a normal bad response and gets handled with everything else. Rate limits are treated separately from ordinary failures. GitHub answers an exhausted limit with a 403 and a remaining count of zero, which is worth distinguishing from a permissions problem, since retrying just burns the reset window.

## Analytics

dbt sits on top of the raw table. Staging is a view, marts are tables.

Staging is a thin pass over `raw_repositories` adding the derived columns, so materialising it would only mean a second copy going stale between pipeline runs. The marts are what dashboards query, so those are worth building.

The fact table is incremental on `extracted_at`, merged on a surrogate key. Every row the pipeline touches gets a fresh timestamp, so a normal run brings them all back through and merges in place. A project that drops out of the top slice keeps its old timestamp and stays in history rather than disappearing.

`dim_sources` carries `with_stars`, `with_downloads` and `with_language` counts alongside the usual aggregates. It's a coverage report as much as a dimension. Given how uneven the sources are, knowing which of them actually populates a field is the first thing worth checking before comparing anything across them, and that felt better placed in the schema than in a README nobody reads at the right moment.

## Observability

The obvious approach is to count things as the pipeline runs and let Prometheus scrape it. That doesn't work for a scheduled job: the counters live in a pod that runs for a few seconds once a day and then exits. By the time anything scrapes, there's nothing there.

So the exporter reads its gauges back off the database instead. Row counts and extraction age per source survive between runs, which is what makes them worth scraping at all. Extraction age is the useful one: climbing steadily on every source means the scheduled job has stopped running, which is otherwise a silent failure.

The exporter deliberately runs no extractions. Two things writing the same rows on different schedules would only fight over the same keys.

Grafana has two datasources, and the split matters. Prometheus answers how the pipeline is behaving. Postgres answers what it collected. "Repositories by language" belongs to the dbt marts, and turning analytical questions into Prometheus metrics would mean building a worse database on top of a good one.

## Running on Kubernetes

The CronJob does the actual work, daily, with `concurrencyPolicy: Forbid` so a slow run can't have the next one land on top of it. Both would be upserting the same keys.

There's also a Deployment with an autoscaler on it. Being straight about this: it serves metrics, and its real workload is one query a minute, so CPU-based autoscaling on it is a demonstration rather than a response to demand. It's built and it works, but don't read the replica count as a signal about how much data is being collected.

PostgreSQL runs as a StatefulSet with a PersistentVolumeClaim, so the data outlives the pod.

Secrets are created imperatively rather than shipped as a manifest. A placeholder secret in the repo is one `kubectl apply -f k8s/` away from a real deployment running on a password someone meant to change.

## Testing

Most of the suite runs against stubs and touches no network.

Idempotency is the exception. Whether `ON CONFLICT` actually does what it's supposed to isn't a question a mock can answer, so those tests run against a real PostgreSQL. They're opt-in through `HECATE_INTEGRATION`, and that's deliberate: skipping whenever the connection fails looks tidy but means a suite pointed at the wrong database reports green having tested nothing. With the flag set, a database that won't accept the connection is a failure.

The same idea shows up in CI, which has an explicit step asserting those tests weren't skipped. A skip isn't a failure, so without it the build passes either way.

They also run in their own schema. They truncate between cases, and pointed at the default schema they quietly emptied whatever the pipeline had just collected.

## Known limits

- npm and PyPI samples are shaped by their seed lists rather than being true rankings
- npm and GitLab contribute no language data, so `dim_languages` covers GitHub and PyPI only
- PyPI download figures aren't collected. Its JSON API returns a deprecated stub answering -1; the real numbers live in a public BigQuery dataset, which is a separate piece of work
- Staleness isn't quite the same measurement on every source. GitHub uses the last commit, npm the last publish, PyPI the last release — but GitLab's project listing only offers last-activity-of-any-kind, so an issue comment keeps an abandoned project looking fresh. Getting a real push date costs a request per project, the same trade as language
- Everything describes now. There's no history, so growth rate and trend aren't answerable without a snapshot table
- The NetworkPolicy needs a CNI that enforces it, which Docker Desktop's built-in cluster doesn't. Applying it there documents intent rather than restricting anything

## If this continued

Roughly in order of what would add most:

1. PyPI downloads from BigQuery, which would give three of the four sources a real usage signal
2. Snapshots over time. Everything currently describes now; growth rate and trend need history, which means a periodic snapshot table rather than upserting in place
3. Language and a real push date for GitLab, if the extra request per project turns out to be affordable — it would fix two limitations at once
4. Alerting on extraction age, since a job that silently stops running is the most likely real failure
5. A Pushgateway, if per-run timing is ever worth having. The extract, transform and load histograms are recorded inside a pod that exits in seconds, so nothing ever scrapes them
6. YouTube. Tutorial volume is a genuine adoption signal and the API is free, but tying a video to a repository is much harder than tying a link post to one — the title and description rarely carry a URL. Worth attempting only once name-based matching has a measured false-positive rate to borrow from, otherwise it is guesswork wearing a number

## Attention, as distinct from adoption

Stars are cumulative and lagging. `atom` still carries sixty thousand of them three years after being formally sunset, and downloads measure entrenchment rather than interest. Neither answers what is happening right now, which is a question this project claims to answer and currently cannot.

Social mentions are the missing leading indicator: a library with eight hundred stars and an active thread this week is a different object from one with eight hundred stars and silence.

Posts don't belong in `raw_repositories` though. Every row there is an artifact with an identity; a post is an *event about* one, which is a different grain. They get their own table with a foreign key back, and the repository picks up aggregates rather than raw posts.

Hacker News is the source of those today. Reddit was scoped and dropped: it now gates API access behind a registration step separate from creating an app, and refuses plenty of ordinary accounts. Not worth the fight for a second source of the same signal, particularly when the Hacker News audience overlaps this domain more closely anyway. The table is platform-agnostic and has a test proving two platforms aggregate together, so another source is an extractor and nothing else.

Twitter/X was ruled out earlier on cost. Read access has been paid-only since 2023, and the tier permitting useful search volume is expensive enough to be its own decision.

The harder half is knowing what a post refers to. Matching on a link is reliable and covers most technical posts. Matching on a name is not — `requests` is a PyPI package, a GitHub repository, and an ordinary English word, and a fuzzy match would attach attention to the wrong project quietly and plausibly. So link matching first, and name matching only once the link-resolved set can serve as ground truth to measure the error rate against.

This also forces the snapshot table. A mention count without a time dimension cannot distinguish one viral post from sustained interest, so the history that growth rate and trend score always needed stops being optional.
