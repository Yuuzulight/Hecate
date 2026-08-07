# Setup

## Prerequisites

- Docker, for the local database and building the image
- Python 3.11 or newer
- A Kubernetes cluster, only if you want the scheduled deployment. Docker Desktop's built-in one is enough

## Local

```bash
docker compose up -d postgres
cp .env.example .env
pip install -r requirements.txt
python -m pipeline.main
```

The defaults in `.env` match what compose starts. The one worth checking is `DB_PORT`, covered in troubleshooting below.

Add a `GITHUB_TOKEN` if you plan to run this more than a couple of times an hour. Without one you're on the unauthenticated limit of 60 requests an hour, which a few runs will use up.

For the dbt models:

```bash
pip install -e ".[dbt]"
cd dbt && dbt run --profiles-dir . && dbt test --profiles-dir .
```

## Kubernetes

Full sequence is in the README. The parts that catch people out:

**The image has to be on the node.** Docker Desktop's Kubernetes runs its own containerd, separate from the Docker daemon, so `docker build` alone doesn't make an image visible to the cluster:

```bash
docker save hecate:v1 | docker exec -i desktop-control-plane ctr -n k8s.io images import -
```

**metrics-server isn't included.** The autoscaler needs it, and Docker Desktop doesn't ship it:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

The patch is needed because Docker Desktop's kubelet serving certificates aren't signed by the cluster CA.

**The dbt models don't build themselves in the cluster.** The CronJob loads raw data; nothing runs dbt. Port-forward and run it against the cluster database:

```bash
kubectl port-forward svc/postgres 5434:5432 -n hecate
DB_PORT=5434 DB_PASSWORD='...' dbt run --profiles-dir dbt
```

## Backups

A CronJob dumps the database nightly at 04:00, after the dbt rebuild, keeping the last seven on their own volume.

Raw repository rows can always be re-fetched from the sources. **The snapshot history cannot** — it describes days that have passed, and nothing can go back and observe them again. That is what the backup is really protecting.

List what exists:

```bash
kubectl exec -n hecate deploy/hecate-metrics -- ls -lt /backups 2>/dev/null || kubectl get pvc postgres-backups -n hecate
```

To restore, run a pod with the backup volume attached and pipe a dump into `psql`. This is the sequence that has actually been tested, not an approximation of it:

```bash
gunzip -c /backups/hecate-<stamp>.sql.gz | psql -d <target-database>
```

Restore into a scratch database first and compare row counts against the live one before doing anything to production data. A restore verified this way matched exactly — 1,543 repositories, 89 mentions, 1,543 snapshots.

The dump job fails rather than keeping a file under 1KB, because a truncated dump that looks like a backup is worse than an obvious failure.

## Troubleshooting

Everything here was hit while building this, not imagined.

### Connection refused, or a password that should work being rejected

```
FATAL: password authentication failed for user "dataflow"
```

Something else is already on port 5432. What makes this confusing is the error: you'd expect a refused connection, but you get an authentication failure, because your client reached a *different* PostgreSQL that has never heard of your user.

Check what's listening:

```bash
docker compose stop postgres
# still something on 5432? then it isn't yours
```

Set `DB_PORT=5433` in `.env`. Compose publishes on `${DB_PORT:-5432}`, so both the pipeline and the container follow it.

### `ErrImageNeverPull` on the cluster

The image isn't on the node. See above. Worth knowing that a *stale* image produces no error at all — the tag doesn't change between rebuilds, so an old copy keeps running and the only symptom is the pipeline collecting from fewer sources than it should. If a run reports `sources_run` lower than you expect, re-import before looking anywhere else.

### HPA shows `<unknown>/70%` and never scales

metrics-server is missing or hasn't started. `kubectl top nodes` returning `Metrics API not available` confirms it. Without the `--kubelet-insecure-tls` patch it installs but never becomes ready on Docker Desktop.

### PVC stuck in `Pending`

Expected, if nothing has mounted it yet. The default StorageClass is `WaitForFirstConsumer`, so the volume isn't provisioned until a pod that uses it gets scheduled. Only a problem if the pod is also stuck.

### Running the tests emptied my database

Fixed, but worth knowing what it was: the integration tests truncate between cases, and they used to do that in the default schema. They now work in `hecate_test`. If you write more of them, keep them there.

### Integration tests skipping

By design unless `HECATE_INTEGRATION=1` is set. The flag reads `0`, `false`, `no` and `off` as off, so `HECATE_INTEGRATION=0` disables them rather than enabling them.

If they skip when you expected them to run, the flag isn't reaching pytest. If they *error* instead, that's the intended behaviour: with the flag set, a database that won't accept the connection is a failure rather than a quiet skip.

### dbt can't find a profile

It looks in `~/.dbt` by default. Either pass `--profiles-dir .` from inside `dbt/`, or set `DBT_PROFILES_DIR=dbt` once.

### `kubectl apply -f k8s/monitoring/` fails on the dashboard

Shouldn't happen now — the dashboard JSON lives in `k8s/monitoring/dashboards/` precisely because `apply -f` on a directory tries to treat every file in it as a manifest. If you add other non-manifest files there, put them in a subdirectory too.
