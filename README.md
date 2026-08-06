# Hecate

Hecate collects data about software repositories and packages — from GitHub, npm, PyPI, and GitLab — and turns it into something you can actually ask questions of.

The kind of questions it's meant to answer:

- Which projects in a given ecosystem are actually growing, and which just have a lot of old stars?
- Is this library still maintained, or has it been quietly abandoned?
- What's the competitive landscape around a particular tool?
- Which languages and frameworks are gaining ground right now?

Star counts alone don't tell you much. A repo with 40k stars that hasn't been touched in two years is a different thing from one with 8k stars and weekly releases, and download numbers from npm and PyPI say more about real usage than either does. Hecate pulls all of it together and computes metrics on top.

## How it works

Data comes in through a set of extractors, one per source. Each source returns a different shape, so everything gets normalised to a common schema before it goes anywhere. From there it lands in PostgreSQL — idempotently, so a crashed run can just be re-run without duplicating anything — and dbt models it into a star schema for analysis. Prometheus scrapes pipeline metrics, Grafana draws the dashboards, and the whole thing runs on Kubernetes as a scheduled job.

```
GitHub · npm · PyPI · GitLab
            |
      extractors (retry, rate-limit aware)
            |
      transformer (one schema for all sources)
            |
      PostgreSQL (idempotent upsert)
            |
      dbt (staging views -> fact + dimensions)
            |
   Grafana dashboards  ·  Prometheus metrics
```

## Running it

You need Docker and Python 3.11 or newer. Start the database and copy the example environment file:

```bash
docker compose up -d postgres
```

```bash
cp .env.example .env
```

The defaults in `.env` line up with what compose starts, so the only thing worth checking is `DB_PORT` — if you already have PostgreSQL on 5432, set it to something free like 5433 and compose will publish there instead.

Then install and run:

```bash
pip install -r requirements.txt
```

```bash
python -m pipeline.main
```

That pulls repositories from GitHub, normalises them and writes them to Postgres. Run it twice and you'll still have one row per repository — re-running is safe by design, so a run that dies partway through just gets run again.

A `GITHUB_TOKEN` in `.env` is optional but worth setting. Without one you're on the unauthenticated rate limit, which is 60 requests an hour.

To run it as the container instead:

```bash
docker build -t hecate:v1 . && docker run --rm --network hecate_default -e DB_HOST=postgres -e DB_PASSWORD=dataflow hecate:v1
```

Want to browse the data? `docker compose --profile tools up -d` adds pgAdmin on port 5050.

## Layout

```
pipeline/
  extractors/     one module per source, on a shared base
  transformers/   normalisation everything passes through
  loaders/        PostgreSQL, upsert-based
  expectations.py data quality checks, run after load
  server.py       metrics endpoint
  main.py         wires it together
dbt/models/       staging view, then fact and dimensions
k8s/              namespace, database, CronJob, metrics deployment
k8s/monitoring/   Prometheus and Grafana
tests/
```

## Analytics

The dbt models live in `dbt/` and read from the table the pipeline writes. Install them as an extra and point dbt at the profile in the project:

```bash
pip install -e ".[dbt]"
```

```bash
cd dbt && dbt run --profiles-dir . && dbt test --profiles-dir .
```

Staging is a view over the raw table with the derived columns added — popularity banding, a normalised language, and days since last activity, which is the interesting one: a large number next to a high star count is roughly the shape of an abandoned project.

On top of that sits a small star schema: `fct_repositories` with one row per project, joined out to `dim_languages`, `dim_sources` and a date spine. The fact table is incremental, so a re-run merges rather than appends.

`dim_sources` is worth knowing about — as well as per-source totals it carries `with_stars`, `with_downloads` and `with_language` counts, so before you compare anything across sources you can see which of them actually reports the field you're about to compare on.

## Tests

```bash
pytest
```

Nothing in the default run touches the network — every API response is stubbed. Some tests need a real database, since whether an upsert is genuinely idempotent isn't something a mock can answer. Those are skipped unless you ask for them:

```bash
HECATE_INTEGRATION=1 pytest
```

That flag is deliberate. If the tests just skipped whenever the connection failed, a suite pointed at the wrong database would report green having tested nothing at all. With it set, a database that won't accept the connection is a failure.

For coverage:

```bash
pytest --cov=pipeline --cov-report=term-missing
```

## On Kubernetes

Any cluster will do. These instructions assume Docker Desktop's, which is the fiddliest case.

```bash
kubectl apply -f k8s/00-namespace.yaml
```

Create the secret directly, so no password ends up in a file:

```bash
kubectl create secret generic db-secret -n hecate --from-literal=username=dataflow --from-literal=password='pick-something' --from-literal=github-token='' --from-literal=gitlab-token=''
```

```bash
kubectl apply -f k8s/01-postgres.yaml -f k8s/03-cronjob.yaml -f k8s/04-deployment.yaml -f k8s/04-hpa.yaml
```

Docker Desktop's Kubernetes runs its own containerd, separate from the Docker daemon, so a locally built image isn't visible to it. Put it on the node:

```bash
docker save hecate:v1 | docker exec -i desktop-control-plane ctr -n k8s.io images import -
```

Do that again after every rebuild. The tag doesn't change, so a stale copy keeps running quite happily and the only symptom is the pipeline doing less than it should.

The autoscaler needs metrics-server, which Docker Desktop doesn't ship. The install and the `--kubelet-insecure-tls` patch it needs are both documented at the top of `k8s/04-hpa.yaml`.

To trigger a run rather than waiting for 02:00:

```bash
kubectl create job hecate-now --from=cronjob/hecate-daily -n hecate && kubectl logs -f job/hecate-now -n hecate
```

Monitoring is optional and goes on afterwards:

```bash
kubectl create configmap grafana-dashboard -n hecate --from-file=hecate.json=k8s/monitoring/dashboards/hecate.json && kubectl apply -f k8s/monitoring/
```

Then `kubectl port-forward svc/grafana 3000:3000 -n hecate`. Grafana is set up for anonymous access, which suits a laptop and nothing else — turn it off in `k8s/monitoring/grafana.yaml` before putting it anywhere reachable.

## Metrics

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

The first two are read back off the database rather than counted during a run. A scheduled job's counters die with its pod, so they'd be empty by the time anything scraped them. Extraction age is the one to alert on — if it climbs on every source at once, the job has stopped running.

## Contributing

Open an issue before anything substantial, and keep pull requests to one thing.

Two conventions worth knowing. Nulls are meaningful here: if a source doesn't report a field, it stays null rather than becoming zero, because averages computed over fake zeros are wrong in ways nobody notices. And any non-trivial logic gets a test — the integration ones need a real database, so run `HECATE_INTEGRATION=1 pytest` before pushing anything that touches the loader.

`ARCHITECTURE.md` has the reasoning behind most of the structural decisions.

## Status

Early, and built in the open — all four sources are collecting and the pipeline runs end to end. Next up are the dbt models, then the Kubernetes and monitoring layers. Progress is tracked in [issues](https://github.com/Yuuzulight/Hecate/issues).

## What each source actually gives you

Not every metric exists everywhere, and the gaps matter more than they look:

| | stars | downloads | language | first published |
|---|---|---|---|---|
| GitHub | yes | — | usually | yes |
| GitLab | yes | — | — | yes |
| npm | — | weekly | — | — |
| PyPI | — | — | yes | yes |

Where a source doesn't report something it's stored as null, not zero. "This doesn't apply" and "this is zero" are different claims, and averaging them together gives you neither.

A few of these are worth explaining. GitLab does know what language a project is in, but only from a separate endpoint per project, which is one extra request per row for one field. npm's search results don't say at all, and defaulting everything to JavaScript would be inventing data. PyPI's download figures used to come from its JSON API and now return -1 from a deprecated stub — the real numbers live in a public BigQuery dataset, which is its own piece of work.

Neither npm nor PyPI publishes a ranked list of top packages, so those two are seeded: npm searches across a set of broad ecosystem keywords and ranks what comes back by weekly downloads, PyPI works from a hand-picked list. GitHub and GitLab will both just hand over their most-starred, in order.

## Stack

Python 3.11, PostgreSQL 15, dbt, Docker, Kubernetes, Prometheus, Grafana.

## Licence

MIT — see [LICENSE](LICENSE).
