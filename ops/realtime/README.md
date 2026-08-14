# Real-time ingestion services

Two always-on Windows services, separate from the daily windowed collection
task (`ops/windowed-run.ps1`) and from Docker Desktop entirely - confirmed
that `windowed-run.ps1` stops Docker Desktop wholesale, not just the K8s
cluster, so anything meant to survive that has to live outside Docker.

| service | what it does |
|---|---|
| `HecateNpmListener` | Runs `pipeline.realtime.npm_listener`, tailing npm's live replication feed |
| `HecateHnListener` | Runs `pipeline.realtime.hn_listener`, polling Hacker News's live updates feed |

Both publish into Memurai (a Windows-native Redis-compatible server - plain
Redis has no first-party Windows build), a second, separate Redis instance
from the one the RAG service uses for context caching. Losing the cache
costs a slower answer; losing this one costs events that cannot be
recaptured, since they were only ever seen live.

Install once with `install-realtime-services.ps1`. Check status any time
with `nssm status HecateNpmListener` / `nssm status HecateHnListener`.

The daily collection run drains both streams into Postgres through the
normal pipeline - see `pipeline/realtime/drain.py`. If both services have
been running but the streams stay empty, check `REDIS_REALTIME_URL` in
`.env` points at the right port before assuming the listeners are broken.
