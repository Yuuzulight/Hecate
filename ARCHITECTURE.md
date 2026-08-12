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
- PyPI download figures come from the same third-party dataset that supplies its ranking, since the JSON API's own `downloads` block is a deprecated stub answering -1. A package that falls back to the hand-written seed list has no figure at all
- Staleness isn't quite the same measurement on every source. GitHub uses the last commit, npm the last publish, PyPI the last release — but GitLab's project listing only offers last-activity-of-any-kind, so an issue comment keeps an abandoned project looking fresh. Getting a real push date costs a request per project, the same trade as language
- History is short. Snapshots started on 7 August, so seven- and thirty-day growth are null rather than zero until that much has accumulated — a distinction the dashboard and the answering prompt both have to make explicitly, because a null read as a zero says a project stopped growing
- The NetworkPolicy needs a CNI that enforces it, which Docker Desktop's built-in cluster doesn't. Applying it there documents intent rather than restricting anything

## If this continued

This list was written at v1.0.0 and had gone stale in both directions — three of its items shipped, and one arrived by a route it did not anticipate. What is actually left:

1. Language and a real push date for GitLab, if the extra request per project turns out to be affordable — it would fix two limitations at once
2. A Pushgateway, if per-run timing is ever worth having. The extract, transform and load histograms are recorded inside a pod that exits in seconds, so nothing ever scrapes them
3. YouTube. Tutorial volume is a genuine adoption signal and the API is free, but tying a video to a repository is much harder than tying a link post to one — the title and description rarely carry a URL. Worth attempting only once name-based matching has a measured false-positive rate to borrow from, otherwise it is guesswork wearing a number

Done since, and struck from the list rather than left to look pending: snapshots over time, which is what made growth rate and trend answerable at all; alerting on extraction age, which is the `ExtractionStalled` rule; and PyPI downloads — though not from BigQuery directly, as this list assumed. The ranking dataset that solves the "PyPI publishes no ranking" problem turns out to be generated from those same statistics and carries the figures with it, so one request fixed both.

## Attention, as distinct from adoption

Stars are cumulative and lagging. `atom` still carries sixty thousand of them three years after being formally sunset, and downloads measure entrenchment rather than interest. Neither answers what is happening right now, which is a question this project claims to answer and currently cannot.

Social mentions are the missing leading indicator: a library with eight hundred stars and an active thread this week is a different object from one with eight hundred stars and silence.

Posts don't belong in `raw_repositories` though. Every row there is an artifact with an identity; a post is an *event about* one, which is a different grain. They get their own table with a foreign key back, and the repository picks up aggregates rather than raw posts.

Hacker News is the source of those today. Reddit was scoped and dropped: it now gates API access behind a registration step separate from creating an app, and refuses plenty of ordinary accounts. Not worth the fight for a second source of the same signal, particularly when the Hacker News audience overlaps this domain more closely anyway. The table is platform-agnostic and has a test proving two platforms aggregate together, so another source is an extractor and nothing else.

Twitter/X was ruled out earlier on cost. Read access has been paid-only since 2023, and the tier permitting useful search volume is expensive enough to be its own decision.

The harder half is knowing what a post refers to. Matching on a link is reliable and covers most technical posts. Matching on a name is not — `requests` is a PyPI package, a GitHub repository, and an ordinary English word, and a fuzzy match would attach attention to the wrong project quietly and plausibly. So link matching first, and name matching only once the link-resolved set can serve as ground truth to measure the error rate against.

This also forces the snapshot table. A mention count without a time dimension cannot distinguish one viral post from sustained interest, so the history that growth rate and trend score always needed stops being optional.

## Answering questions

Phase 2 puts a question-answering service over the warehouse: retrieve context, ask whichever LLM `RAG_PROVIDER` names, return an answer with the rows it was built from. Most of the decisions in it are about what the model is *not* allowed to do.

Retrieval is structured SQL against the marts, not a similarity search. The questions this dataset gets asked — what is growing, what is being discussed, what is popular and going quiet — are aggregates, and no nearest-neighbour lookup surfaces an aggregate. Every block is bounded and already summarised, so a context that grows with the dataset cannot quietly start truncating.

The prompt carries the same discipline as the dashboard. It is told what the data cannot say: that a null seven-day figure means the history is too short rather than that growth stopped, that npm and PyPI report no stars and GitHub no downloads so a measure must not be compared across a source that does not collect it, and that similarity between descriptions is a reason to look rather than evidence about either project. It is also told the context is data and never instructions, because repository descriptions are written by strangers. Cited repository ids are checked against the retrieved context before the answer is returned — an id the model produced from memory reads exactly like one it read, and points at nothing.

The original intention was to run at temperature 0.2, to stop the model filling gaps with things that sound right. That parameter no longer exists on current models — sending it is rejected outright — so the reasoning survives instead in the prompt, in a confidence the model has to commit to, and in that citation check.

### Why there is a vector store

Because it was worth learning to build one, not because the data needed it. That is the whole reason and it is better written down than implied.

Retrieval here is structured SQL against the marts. The questions this dataset gets asked — what is growing, what is being discussed, what is popular and going stale — are aggregates, and no nearest-neighbour lookup surfaces an aggregate. The corpus is 44k tokens with a median description of 55 characters; there is not enough text for similarity to be doing much work, and picking the right rows beats finding similar ones at this size.

So the embeddings are built to be an addition rather than a foundation. Similarity is one more block alongside the structured ones, labelled as similarity everywhere it appears, and capped smaller than the rest so five weak rows cannot outweigh seven strong blocks. Search swallows every error it can hit. Dropping every stored vector changes what an answer cites, not whether there is one — which is the property that makes it safe to have built something the data did not ask for.

The cost discipline is real even if the scale is not. Only rows whose text has changed are re-embedded, so the first run does the corpus and every run after does the handful of descriptions that moved; vectors are shortened to 256 dimensions because the full 1536 would be 60MB of JSON against a Redis capped at 128MB. Re-embedding everything nightly was the original plan, and it was the mistake behind the original cost estimate.

### On demand, and what the uptime target means

The cluster is off most of the day. That was decided a day before the service was built, when the CronJobs were suspended in favour of a script that starts Docker, runs the day's work in about two and a half minutes, and shuts it down again — so the API is brought up the same way rather than standing permanently. Asking a question is start, port-forward, ask, stop.

Which makes the uptime figure worth stating carefully: **99.5% applies to while it is running**, not to wall-clock. Measured against the clock the number would be far lower and would be describing a switched-off machine rather than a service.

The under-five-second target is the query, not the start. Starting is dominated by Docker Desktop: measured cold, 36 seconds from launching it to a database answering a query — 20 of those before the daemon reported itself up, and the rest waiting for the API server and Postgres. The service itself adds almost nothing on top.

A permanent cluster was considered and rejected: it reverses a one-day-old decision for something asked a handful of times a week. Hosting it somewhere else is the right answer only if the goal becomes a URL other people can open, which is a different objective and a paid one.

The service is deliberately not part of the daily window. That window collects, rebuilds the models and takes a backup; it has no use for an API, and starting one more thing lengthens a window whose whole point is being short.

### Why the judge, and the chain, are provider-selectable

Both used to be hardcoded to Claude. Both were blocked on the same thing at once: Anthropic account credits, separate from and unfunded by the Claude subscription. Gemini's free tier removes that specific blocker, so `RAG_PROVIDER` (`gemini` default / `anthropic` / `openai`) picks the provider for both the answering chain and the judge, built through one shared module, `pipeline/rag/providers.py`. That removes the Anthropic-credits blocker, not every possible one: a Google Cloud project still has its own billing state, and live verification of this feature hit exactly that — deployment and provider-selection wiring confirmed correct on `RAG_PROVIDER=gemini`, but the answer itself blocked on that Google Cloud project's prepay credits being exhausted (`429 RESOURCE_EXHAUSTED`), a billing fact about that account rather than a defect here. `RAG_PROVIDER=anthropic` is what returned a complete grounded answer in that same pass, now that this project's Anthropic account has credits again.

RAGAS defaults to OpenAI and will use it for anything it can. Left alone that means a second key, a second bill, and quality scores that cost more than the answers they grade - so the judge is always built from the same provider the chain answers with, never OpenAI regardless of what `RAG_PROVIDER` says.

Pointing it there is the easy half. The harder half is that RAGAS reaches OpenAI through a second door: its usual relevance metric works by embedding the answer to compare it, and neither Anthropic nor Gemini sells an embeddings API the same way - so choosing that metric would have pulled the OpenAI client back in regardless of which provider is configured. Both metrics used here need only a language model, and a test asserts that by inspecting their signatures rather than by reading the setting.

The two scores stay apart, because they mean different things. A low faithfulness score is an answer that invented something; a low relevance score is one that was merely unhelpful. Only the first is a hallucination, and reporting them as a single quality number loses the only distinction worth acting on. A metric that could not run at all is stored as null rather than zero - an unreachable judge is an outage, and a zero would be indistinguishable from a confident lie in every average taken afterwards.

### The switch

`RAG_ENABLED` is a rollback. Set to `0`, `/ask` returns 503 **before the chain is touched**, so nothing reaches the model and nothing is billed. A flag that let the request through and discarded the answer would still have paid for the tokens, which is not a rollback.

It defaults on, unlike `NAME_MATCHING`, because it is a switch you reach for during an incident rather than one you opt into — a service that had to be enabled after every deploy would spend its first hour serving 503s while somebody worked out why. It is an environment variable, so turning it off is `kubectl set env` rather than a rebuild and a retag.

The free endpoints stay up when it is off. `/trending` and `/eval-metrics` call no model and cost nothing, and a rollback that also removes the read-only view of the warehouse is a worse rollback. The health probe is `/health` and never `/ask`: a probe that asked a question would spend money on every check, and with the switch off it would take the 503 and restart the pod in a loop, turning a deliberate rollback into an outage.
