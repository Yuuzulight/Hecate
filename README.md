# Hecate

Hecate collects data about software repositories and packages, watches what people are saying about them, and turns the two together into something you can ask questions of.

The kind of questions it's meant to answer:

- Which projects are actually growing, and which just have a lot of old stars?
- Is this library still maintained, or has it been quietly abandoned?
- What's being talked about right now that I haven't heard of?
- Which languages and frameworks are gaining ground?

Star counts alone don't tell you much. A repo with 60k stars that hasn't taken a commit in three years is a different thing from one with 8k stars and weekly releases, and download numbers say more about real use than either. Discussion says something different again, and earlier: it moves before the stars do.

## How it works

Four sources supply projects: GitHub, npm, PyPI and GitLab. Two more supply conversation about them: Hacker News and Lobsters. Everything is normalised to one schema, then loaded into PostgreSQL idempotently, so a run that dies partway through can just be run again.

The part worth knowing is what happens to a post about a project Hecate doesn't track. Every source is seeded by cumulative popularity — GitHub's most-starred, npm's most-installed — so anything trending *before* it's famous is excluded by the seeding itself. Those posts aren't discarded. They're kept, ranked, and the projects behind them get fetched and added. What people are discussing decides what gets tracked, rather than the other way round.

Every project is also snapshotted daily, which is the only reason growth can be measured at all: everything else describes now, and upserts in place.

```
GitHub · npm · PyPI · GitLab          Hacker News · Lobsters
        |                                      |
   extractors (retry, rate-limit aware)   link resolution
        |                                      |
   transformer (one schema)              social_mentions
        |                                  /        \
   raw_repositories  <--- discovery <-----          |
        |                                            |
   repository_snapshots (daily, the only history)    |
        |                                            |
        +--------------> dbt <---------------------- +
                          |
        staging views -> facts, dimensions, growth, momentum
                          |
       Grafana dashboards  ·  Prometheus metrics  ·  alerts
```

## Running it

You need Docker and Python 3.11 or newer.

```bash
docker compose up -d postgres
```

```bash
cp .env.example .env
```

The defaults line up with what compose starts. The one worth checking is `DB_PORT` — if something already holds 5432, set it to 5433 and compose publishes there instead. A `GITHUB_TOKEN` is optional but worth setting; without one you're on the unauthenticated limit of 60 requests an hour.

```bash
pip install -r requirements.txt
```

```bash
python -m pipeline.main
```

Run it twice and you'll still have one row per project. Re-running is safe by design.

Or from the published image:

```bash
docker run --rm --network hecate_default -e DB_HOST=postgres -e DB_PASSWORD=dataflow ghcr.io/yuuzulight/hecate:latest
```

`docker compose --profile tools up -d` adds pgAdmin on 5050 if you want to browse the tables.

## Layout

```
pipeline/
  extractors/     one module per source, on a shared base
  transformer.py  normalisation everything passes through
  loader.py       PostgreSQL, upsert-based
  matching.py     name matching, off by default
  expectations.py data quality checks
  server.py       metrics endpoint
  main.py         wires it together
dbt/models/       staging views, then facts and dimensions
k8s/              namespace, database, the four scheduled jobs
k8s/monitoring/   Prometheus, Alertmanager, Grafana
ops/              scheduled task prompts
tools/            one-off measurement scripts
tests/
```

## Analytics

The dbt models read from the tables the pipeline writes.

```bash
pip install -e ".[dbt]"
```

```bash
cd dbt && dbt build --profiles-dir .
```

`dbt build` rather than run-then-test, so a model whose test fails isn't left in place looking authoritative.

Staging is a view over the raw table with the derived columns added: popularity banding, a normalised language, days since last activity, and a maintenance status folding in whether a project is formally archived.

On top sits a star schema — `fct_repositories` joined to `dim_languages` and `dim_sources` — plus four models that answer the harder questions:

| model | question |
|---|---|
| `fct_repository_growth` | what's gaining stars, downloads, attention |
| `fct_momentum` | what's accelerating across all three at once |
| `fct_repository_mentions` | attention per project per week, recent posts weighted higher |
| `fct_undiscovered_mentions` | what's being discussed that isn't tracked yet |

`dim_sources` is worth knowing about. Alongside per-source totals it carries `with_stars`, `with_downloads` and `with_language` counts, so before comparing anything across sources you can see which of them actually reports the field you're about to compare on.

## What each source actually gives you

Not every metric exists everywhere, and the gaps matter more than they look:

| | stars | downloads | language | first published |
|---|---|---|---|---|
| GitHub | yes | — | usually | yes |
| GitLab | yes | — | — | yes |
| npm | — | weekly | — | on discovery |
| PyPI | — | weekly | yes | yes |

Where a source doesn't report something it's stored as null, not zero. "This doesn't apply" and "this is zero" are different claims, and averaging them together gives you neither. That rule runs through the whole schema and is the single thing most likely to trip you up if you ignore it.

A few of the gaps have reasons. GitLab knows what language a project is in, but only from a separate endpoint per project, which is one request per row for one field. npm's search results don't say, and defaulting everything to JavaScript would be inventing data. GitHub and GitLab hand over their most-starred in order; npm has no such ranking, so it's seeded from broad ecosystem keywords and sorted by weekly downloads.

Staleness isn't quite the same measurement everywhere either. GitHub uses the last commit, npm the last publish, PyPI the last release — but GitLab's listing only offers last-activity-of-any-kind, so an issue comment keeps an abandoned project looking fresh. Treat GitLab staleness as the weaker signal.

## Tests

```bash
pytest
```

Nothing in the default run touches the network. Some tests need a real database, since whether an upsert is genuinely idempotent isn't a question a mock can answer:

```bash
HECATE_INTEGRATION=1 pytest
```

That flag is deliberate. If the tests simply skipped whenever the connection failed, a suite pointed at the wrong database would report green having tested nothing. With it set, a database that won't accept the connection is a failure.

## On Kubernetes

Any cluster will do. These instructions assume Docker Desktop's, which is the fiddliest.

```bash
kubectl apply -f k8s/00-namespace.yaml
```

Create the secret directly, so no password ends up in a file:

```bash
kubectl create secret generic db-secret -n hecate --from-literal=username=dataflow --from-literal=password='pick-something' --from-literal=github-token='' --from-literal=gitlab-token='' --from-literal=grafana-password='pick-another'
```

```bash
kubectl apply -f k8s/ && kubectl apply -f k8s/monitoring/
```

The images come from ghcr.io, so nothing needs building or side-loading. The dashboard is the one thing that isn't a manifest:

```bash
kubectl create configmap grafana-dashboard -n hecate --from-file=hecate.json=k8s/monitoring/dashboards/hecate.json
```

The autoscaler needs metrics-server, which Docker Desktop doesn't ship — the install and the `--kubelet-insecure-tls` patch it needs are documented at the top of `k8s/04-hpa.yaml`.

Four jobs do the day's work:

| | | |
|---|---|---|
| 02:00 | `hecate-daily` | collect, discover, snapshot |
| 03:00 | `hecate-dbt` | rebuild the models |
| 04:00 | `hecate-backup` | dump the database, keep seven |
| Sun 05:00 | `hecate-dbt-full` | full refresh, clears incremental drift |

Those times are UTC. No `timeZone` is set, so they don't follow the machine's clock — worth knowing before you conclude a run was missed. `captured_on` on the snapshots is the UTC date as well, which is what you want: both move together, so a day is a day regardless of where the machine is.

To trigger one rather than waiting:

```bash
kubectl create job hecate-now --from=cronjob/hecate-daily -n hecate && kubectl logs -f job/hecate-now -n hecate
```

Then `kubectl port-forward svc/grafana 3000:3000 -n hecate` for the dashboard.

### If the cluster isn't up all day

The four CronJobs ship **suspended**, because a fixed UTC time is no use on a machine that gets shut down — the schedule gets missed more often than met, and a missed snapshot is a permanent hole in the history.

`ops/windowed-run.ps1` covers that case. It starts Docker Desktop, waits for the cluster, creates a Job from each CronJob template in order, checks a snapshot actually landed for today, and shuts Docker down again — a bit over two minutes on a weekday, nearer four on the Sunday that adds a full model refresh. If Docker was already up when it started, it leaves it up.

```bash
powershell -ExecutionPolicy Bypass -File ops/windowed-run.ps1
```

Point Task Scheduler at that once a day with *run as soon as possible after a missed start*, and the machine only has to be on at some point, not at a particular time. Every run appends a JSON line to `ops/logs/run-log.jsonl` with the job results and row counts, so you can see what happened without starting anything. It lives next to the script rather than under `%LOCALAPPDATA%` on purpose: that path is redirected for packaged applications, so a log written there by the scheduled task and a log read there by anything else can be two different files that both report the path they were given.

If the machine does stay on, unsuspend them and ignore all of the above:

```bash
kubectl patch cronjob hecate-daily -n hecate -p '{"spec":{"suspend":false}}'
```

## Monitoring

The exporter serves Prometheus metrics on port 8000.

| metric | what it tells you |
|---|---|
| `hecate_repositories{source}` | rows currently stored, per source |
| `hecate_last_extraction_age_seconds{source}` | time since that source was last read |
| `hecate_rows_processed_total{stage,source}` | records handled at each stage |
| `hecate_errors_total{type,source}` | failures, by where they happened |
| `hecate_quality_checks_total{check,outcome}` | data quality results |
| `hecate_extract_duration_seconds{source}` | how long each source takes |
| `hecate_load_duration_seconds` | database write time |

The first two are read back off the database rather than counted during a run. A scheduled job's counters die with its pod, so they'd be empty by the time anything scraped them.

Alerting splits along the same line. Prometheus rules cover liveness — job stalled, exporter unreachable, errors climbing. Grafana rules cover the data itself, because quality and coverage live in Postgres and Prometheus can't see them. A pipeline that runs perfectly while producing rubbish is a real failure mode, and it needs watching separately from one that stops.

Extraction age is the alert that matters most. If it climbs across every source at once, the job has stopped running, and nothing else will tell you.

The question-answering service keeps its own counters on port 8001, because they live in that process and die with it — `hecate_rag_questions_total{outcome}`, `hecate_rag_tokens_total{kind}`, `hecate_rag_cost_usd_total`, and `hecate_rag_context_cache_total{result}`. Cost is the one alert here about money rather than data: every other component fails by producing something wrong, that one fails by producing a bill.

## Asking it questions

Everything above collects and models the data. This part answers questions about it in English.

```bash
python -m pipeline.rag.api      # serves on http://localhost:8001
```

Then open `http://localhost:8001` and type a question. You'll get an answer, how confident the model was, the repositories it drew on as links you can follow, and how long it took.

**It needs an API key for whichever provider is configured.** `RAG_PROVIDER` defaults to `gemini` — put `GOOGLE_API_KEY=...` in your `.env`, free via Google AI Studio. Set `RAG_PROVIDER=anthropic` or `RAG_PROVIDER=openai` and the matching key instead if you'd rather use one of those. Without a key for whichever provider is configured, the service still starts and `/trending` still works, but asking a question returns an error saying what's missing. Gemini's free tier costs nothing at this project's scale; Anthropic and OpenAI are billed per question — a few cents a day at any sane rate of asking, and there's an alert if it isn't.

Three endpoints:

| endpoint | what it does |
|---|---|
| `POST /ask` | `{"question": "..."}` → answer, confidence, sources, latency |
| `GET /trending` | what's growing and what's being discussed, straight from the warehouse — no model, no cost |
| `GET /eval-metrics` | quality scores from the most recent evaluation runs |

The answer is built from SQL against the analytics models, not a similarity search — the questions people ask are aggregates, and the whole corpus is small enough that picking the right rows beats finding similar ones. The model is given those rows and told to answer from them and nothing else; every repository it cites is checked against what it was actually shown before you see it. If the data can't answer, it's supposed to say so, and there's an evaluation harness that scores whether it does.

`RAG_ENABLED=0` turns answering off without touching anything else — useful if a bill starts moving. `/trending` keeps working.

## Contributing

Open an issue before anything substantial, and keep pull requests to one thing.

Two conventions. Nulls are meaningful: if a source doesn't report a field it stays null rather than becoming zero, because averages over fake zeros are wrong in ways nobody notices. And any non-trivial logic gets a test — the integration ones need a real database, so run `HECATE_INTEGRATION=1 pytest` before pushing anything touching the loader.

`ARCHITECTURE.md` has the reasoning behind the structural decisions, including the ones that were reversed later and why.

## Status

Running. All six sources collect, the models build, and the whole thing runs unattended on a schedule with alerting and nightly backups. The question-answering service on top of it is built and deployed, provider-selectable between Gemini, Anthropic, and OpenAI.

Momentum needs about a week of snapshot history before it means much — with fewer days than that, the growth windows are correctly null and the ranking leans on attention alone. That resolves itself rather than needing a change.

Known limits are listed at the end of `ARCHITECTURE.md`. The honest short version: name-based mention matching is off by default because its error rate hasn't been measured on enough data to trust, PyPI's ranking depends on a third-party dataset, and GitLab contributes no language data.

## Stack

Python 3.11, PostgreSQL 15, dbt, Docker, Kubernetes, Prometheus, Alertmanager, Grafana, Redis, FastAPI, LangChain, Gemini/Claude/GPT (provider-selectable).

## Licence

MIT — see [LICENSE](LICENSE).
