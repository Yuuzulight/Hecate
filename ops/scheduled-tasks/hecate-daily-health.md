---
name: hecate-daily-health
description: Daily check that Hecate's overnight collection actually ran, catching gaps while they can still be acted on
---

Check that Hecate's overnight collection ran. Keep this short — it is a health check, not an analysis.

## Context (this run starts fresh)

Hecate is a repository-intelligence pipeline at `F:\GitHub Projects\Hecate`, running on Docker Desktop's Kubernetes in namespace `hecate`. Four CronJobs run overnight: extraction at 02:00, dbt rebuild at 03:00, backup at 04:00, and a full refresh Sundays at 05:00.

The user is accumulating snapshot history to watch growth over a month, starting 2026-08-07. **A missed day is a permanent gap** — snapshots describe a moment that has passed and cannot be backfilled. Catching a break the morning after is the entire point of this check; catching it a fortnight later is useless.

## What to check

If Docker Desktop or the cluster is not running, say so and stop — that is the finding, and it means last night was probably missed.

```
kubectl exec -n hecate postgres-0 -- psql -U dataflow -d hecate -c "<SQL>"
```

1. **Was there a snapshot for yesterday and today?**
   `SELECT captured_on, count(*) FROM repository_snapshots WHERE captured_on > current_date - 8 GROUP BY captured_on ORDER BY captured_on;`
   Any missing date in that range is the headline.

2. **Did the jobs run?**
   `kubectl get cronjob -n hecate -o custom-columns="NAME:.metadata.name,SCHEDULE:.spec.schedule,LAST:.status.lastScheduleTime"`
   A `lastScheduleTime` older than yesterday means it is not firing.

   `<none>` is different and is not necessarily a fault: it means the job has
   never run, which is expected for one created after its slot had passed for
   the day. Treat `<none>` as a problem only once its scheduled time has come
   round at least once since it was created.

3. **Any failed jobs?**
   `kubectl get pods -n hecate --field-selector=status.phase=Failed`

4. **Quick numbers:** total repositories, and how many days of history exist so far.
   `SELECT (SELECT count(*) FROM raw_repositories) AS repos, (SELECT count(DISTINCT captured_on) FROM repository_snapshots) AS days;`

5. **Alerts:** anything firing.
   `kubectl exec -n hecate deploy/prometheus -- wget -qO- http://localhost:9090/api/v1/alerts` — or skip if that fails, it is not the important part.

## Reporting

Three or four lines when everything is fine: days of history, repository count, and that last night ran. Do not write a report about a healthy system.

If a day is missing or a job stopped firing, lead with that and say how many days of history have been lost. Suggest the obvious cause — the machine asleep, Docker closed — rather than investigating deeply.

Do not change code or push anything. This is an observation run.