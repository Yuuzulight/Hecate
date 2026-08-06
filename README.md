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

## Status

Early. The build is tracked in [issues](https://github.com/Yuuzulight/Hecate/issues) and lands in roughly this order: the Python pipeline first, then the remaining data sources, then dbt, then the Kubernetes and monitoring layers. Setup instructions and the architecture write-up go in once there's enough here to be worth documenting.

## Stack

Python 3.11, PostgreSQL 15, dbt, Docker, Kubernetes, Prometheus, Grafana.

## Licence

MIT — see [LICENSE](LICENSE).
