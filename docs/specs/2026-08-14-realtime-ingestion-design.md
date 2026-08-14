# Phase 3: real-time ingestion (scope)

## Why this scope, not the original sketch

The original idea was Kafka + Flink + a WebSocket API, aimed at sub-second latency across
all six sources. That doesn't survive contact with what the sources actually support, or
what the data actually needs.

**Push capability, checked source by source, not assumed:**

| source | genuine push | why |
|---|---|---|
| GitHub (stars) | No, not at this scale | Webhooks exist, but per-repo and owner-configured. Hecate tracks ~2,000 repos it doesn't own - it cannot install a webhook on someone else's repository. |
| GitLab | No, same reason | Same per-project, owner-configured pattern as GitHub. |
| npm | **Yes** | `https://replicate.npmjs.com/registry/_changes?feed=continuous` - CouchDB continuous replication, global, no per-package opt-in. |
| PyPI | No | RSS/JSON only, pull-based. PyPI's own docs ask consumers not to poll frequently. |
| Hacker News | **Yes** | `hacker-news.firebaseio.com/v0/updates` - Firebase-backed, built for live listening (SSE-capable). |
| Lobsters | No | JSON pages only, pull-based, no push mechanism found. |

Real push exists for two of six sources. For the other four, "real-time" would mean either
something structurally impossible (GitHub/GitLab star webhooks without owning the repos)
or polling faster against a source that has explicitly asked not to be.

Even where push exists, sub-second doesn't obviously matter here. Hecate's own metric is
growth *rate* - `stars_gained_7d`, momentum - a multi-day derivative by construction. A
star count updating sub-second doesn't make a trend visible sooner; the trend is still
only meaningful once days have accumulated. What *is* genuinely useful at low latency:
a new npm publish or a new HN post about a tracked project landing within seconds instead
of the next daily batch. That is what this scope builds. Star/download growth stays on
the existing batch model, honestly.

**Always-on cost, quantified rather than assumed:** Kafka + Flink self-hosted, minimum
viable (not toy): 4-8GB RAM, 2-3 CPU cores, running continuously - reversing Phase 1's
foundational decision (CronJobs suspended, cluster off most of the day, specifically to
avoid exactly this). Confluent Cloud's managed tier is near-free at hobby volume but adds
a third-party managed dependency this project has never had, and production tiers start
around $385/month. A cloud VM sized to actually run Kafka+Flink together runs
€8-15/month (Hetzner) to ~$48/month (DigitalOcean) - the first new recurring cost in this
project's history; everything currently deployed is free on local Docker Desktop.

This scope avoids all of that. It ingests only what genuinely pushes (npm, HN), reuses
Redis - already in the cluster for RAG context caching - as the event bus instead of
standing up Kafka, and reuses the existing transformer/loader pipeline instead of a
parallel Flink job. The always-on footprint is two small listener processes and one small
Redis instance: well under 1GB combined, and it runs on the laptop at $0 marginal cost,
since the machine now stays on overnight for the 3am collection job anyway.

## Architecture

```
npm CouchDB _changes ---\
                          >-- listeners (always-on) --> Redis Streams (always-on, small)
HN Firebase /v0/updates -/                                    |
                                                                |
                                          +---------------------+----------------------+
                                          |                                            |
                              drain step (in the daily window)              WS /live (on-demand,
                              -> transformer.py -> loader.py                 existing RAG API)
                              -> raw_repositories / social_mentions          -> pushes stream events
                              (same schema, same upsert, no new tables)         to a connected client
```

Two tiers, deliberately different lifecycles:

**Always-on (new, tiny):** the two listeners and a small standalone Redis instance. This
is *not* the Redis already running inside the windowed K8s cluster - that one is only up
2.5 minutes a day, same as everything else in Phase 1+2. This is a second, separate,
always-on instance, sized for a rolling event buffer rather than context caching.

**On-demand (existing lifecycle, extended):** the daily window gains one new step - drain
the Redis Streams into Postgres through the *existing* `transformer.py`/`loader.py`,
same as any other source. The RAG API (`pipeline/rag/api.py`, already built, already
on-demand) gains one new endpoint, `WS /live`, which reads directly from the always-on
Redis Streams rather than Postgres - so a connected client sees events the moment a
listener captures them, without requiring Postgres, dbt, or the dashboard to also be
always-on. "Real-time" here means the *capture* is continuous; the rest of the stack keeps
its existing on-demand shape.

This is also why the design holds together without touching the always-off dbt/warehouse
side of the system at all: durable storage and the dashboard don't need to know real-time
ingestion exists until the daily drain step runs.

## What's new vs. what's reused

**New:**
- `pipeline/realtime/npm_listener.py` - tails the npm CouchDB feed, filters to tracked
  packages, writes to a Redis Stream.
- `pipeline/realtime/hn_listener.py` - tails HN's Firebase updates feed, filters to items
  matching the existing discovery-relevance check (link resolves to a tracked or
  discoverable project), writes to a Redis Stream.
- `pipeline/realtime/drain.py` - reads the Redis Streams, calls the existing transformer
  and loader per event. One new small module, zero new normalization logic.
- `WS /live` on the existing FastAPI RAG service.
- A small always-on Redis instance, separate from the K8s-deployed one.
- A Windows background task keeping the two listeners and Redis alive continuously,
  independent of `windowed-run.ps1`'s existing "Hecate daily run" task.

**Reused directly, not reimplemented:**
- `pipeline/transformer.py`, `pipeline/loader.py` - the drain step is a caller, not a
  parallel schema.
- `raw_repositories` / `social_mentions` - no new tables. A real-time-captured row and a
  batch-captured row are indistinguishable once they land.
- `pipeline/config.py` - extended with connection settings for the always-on Redis, same
  pattern as every other setting.
- The existing "one source, one try block, one failure costs that source, not the run"
  discipline - applies per-listener; the npm listener dying doesn't take the HN listener
  down with it.
- `pipeline/rag/api.py` - the WebSocket is a new route on the app that already exists and
  already deploys the same way.

## Integration checklist

Not a calendar - a list of things that have to be verified against the real services,
not assumed from documentation, before this is called done. (This project has a specific
history of "committed but not applied" and "green CI, unexercised in reality" - the same
discipline that caught real bugs in Phase 2 applies here.)

- [ ] npm listener verified against the real feed: a known recent publish for a tracked
      package actually arrives, not just "the connection opens without error"
- [ ] HN listener verified against the real feed: a real, current post appears within
      seconds of it actually being posted
- [ ] Redis Streams survive a listener restart with no events lost - kill a listener
      mid-stream, confirm the consumer group's pending-entries list picks up where it left
      off rather than starting fresh or losing the gap
- [ ] The drain step is idempotent - the same stream entry processed twice does not create
      a duplicate row (this is the existing `ON CONFLICT` upsert; confirm it holds for
      real-time-sourced rows the same way it does for batch ones, not just assumed to)
- [ ] `WS /live` verified end-to-end: a real npm publish or HN post appears on a connected
      client within seconds, not just that a test message can be pushed through the socket
- [ ] Measured, not estimated: actual RAM/CPU of the two listeners + Redis over a real 24h
      period, checked against the <1GB estimate above
- [ ] The existing windowed daily batch is confirmed unaffected - snapshot still lands,
      `run-log.jsonl` still shows `ok: true`, with the new drain step added to the sequence
- [ ] A listener process crashing (not gracefully stopped) is confirmed to not corrupt the
      Redis Stream or block the other listener

## Cost

Always-on component: two small Python processes plus one small Redis instance. Estimated
under 300MB RAM combined, low CPU (both listeners are I/O-bound and mostly idle waiting on
their feeds). Runs on the laptop, which already stays on overnight for the 3am collection
task - **$0 marginal cost.**

No cloud spend required for this version. That's a deliberate difference from the original
sketch, which would have required €8-48/month minimum just to host Kafka+Flink together.

Not in scope now, but the honest upgrade path if it's ever needed: move the always-on
slice to a small dedicated VM (~$5/month, Hetzner or Lightsail class) if independence from
the laptop's own uptime becomes a requirement later. Nothing about this design blocks that
move - the listeners and Redis instance are already a self-contained unit that doesn't
depend on anything else being local.

## Success criteria

- An npm publish to a tracked package appears in the live feed within seconds of the real
  publish, not the next day's batch.
- A new HN post linking to a tracked or discoverable project appears in the live feed
  within seconds of the real post.
- The daily batch/warehouse/dbt/dashboard pipeline is unaffected in cadence or reliability
  - this is additive, not a replacement for anything Phase 1 or Phase 2 built.
- A connected WebSocket client receives events pushed, with no polling on the client side.
- Zero new recurring cloud cost for this version.
- GitHub/GitLab star growth and PyPI downloads remain explicitly documented as batch, not
  real-time - no overclaiming what four of six sources structurally cannot do.

## Deliberately out of scope

- **Kafka, Flink, or any general-purpose streaming platform.** The two sources that
  genuinely push don't need one, and the four that don't push wouldn't be served by one
  either.
- **Real-time GitHub/GitLab stars or PyPI downloads.** Not achievable without owning the
  tracked repos (stars) or violating the source's own guidance (PyPI). Revisit only if
  either source's public capabilities change.
- **An always-on Postgres, dashboard, or RAG service.** The WebSocket reads the always-on
  Redis Streams directly; nothing downstream of the daily drain needs to run continuously.
- **Moving the always-on slice off the laptop.** Documented as a cost-free upgrade path
  above, not built now - there's no requirement driving it yet.
