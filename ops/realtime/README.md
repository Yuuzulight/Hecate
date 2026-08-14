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
Each service's stdout/stderr - including a `REDIS_REALTIME_URL is
required...` startup misconfiguration and any `log.warning` either
listener or the bus emits - is captured to `ops/logs/HecateNpmListener.log`
/ `ops/logs/HecateHnListener.log`, rotated at 10MB, so a listener that
looks "running" in `nssm status` but is actually stuck can still be
diagnosed.

The daily collection run drains both streams into Postgres through the
normal pipeline - see `pipeline/realtime/drain.py`. If both services have
been running but the streams stay empty, check `REDIS_REALTIME_URL` in
`.env` points at the right port before assuming the listeners are broken.

## Unresolved: Memurai only listens on loopback

`k8s/03-cronjob.yaml` and `k8s/10-rag-api.yaml` both point
`REDIS_REALTIME_URL` at `redis://host.docker.internal:6380/0` - Docker
Desktop's standard address for a pod to reach a service running on the
Windows host, which is where Memurai actually runs. That address will not
work yet: Memurai's `memurai.conf` binds to `127.0.0.1` by default, the
same as stock Redis, so it only accepts connections from this machine
itself. A pod reaching for it through `host.docker.internal` arrives as a
different address as far as the loopback-only bind is concerned, and gets
refused.

Fixing it means widening Memurai's `bind` directive to also accept the
Docker Desktop network, not just `127.0.0.1`. That has not been done
automatically here on purpose - it is a real security-relevant tradeoff
(this machine's event bus becoming reachable from beyond itself, not just
from processes running on it), and a tradeoff like that deserves a
deliberate choice made on the actual machine, not a default silently
changed by a config file. Until it is made, `drain()` and `/live` will
both build a working `EventBus` pointed at the right address and still
fail every connection to Memurai from inside the cluster - the listeners
will keep publishing into Memurai just fine in the meantime, since they
run directly on the host, not from a pod.

This is a required manual step, on the real machine, before the drain
step or `/live` can actually reach Memurai from inside K8s.
