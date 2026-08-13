---
name: hecate-daily-health
description: Daily check that Hecate's windowed run actually happened, read from its log rather than the cluster
---

Check that Hecate's daily run happened. Keep this short — it is a health check, not an analysis.

## Context (this run starts fresh)

Hecate is a repository-intelligence pipeline at `F:\GitHub Projects\Hecate`. It runs on Docker Desktop's Kubernetes in namespace `hecate`, but **Docker is not running most of the time and that is intentional**. A Windows scheduled task runs `ops/windowed-run.ps1` once a day, which starts Docker, runs collection, dbt and the backup back to back, checks a snapshot landed, then shuts Docker down again. The Kubernetes CronJobs are suspended because a fixed UTC time would be missed more often than met.

So **do not expect the cluster to be up, and do not start Docker**. A stopped Docker is the normal state, not a finding.

The user is accumulating snapshot history to watch growth over a month, starting 2026-08-07. **A missed day is a permanent gap** — snapshots describe a moment that has passed and cannot be backfilled. Catching a break the day after is the entire point of this check.

## What to read

Every run appends one JSON line to:

```
F:\GitHub Projects\Hecate\ops\logs\run-log.jsonl
```

It lives on the repo drive rather than under `%LOCALAPPDATA%` on purpose. AppData is redirected for packaged applications, so the same path can be two different files depending on what opens it — the scheduled task wrote entries there that nothing else could see.

Read the last few lines. Each has `started_at`, `ok`, `snapshot_date`, `snapshot_rows`, `repositories`, `discovered`, a `jobs` array with one entry per job, and `error` when something went wrong.

1. **Did today's run happen, and did it work?**
   The newest entry's `started_at` should be today. `ok` is true only when every job completed *and* a snapshot exists for the current UTC date — a run can have all jobs succeed and still not be `ok`, which is the case worth catching.

2. **Any gaps?** Walk the `snapshot_date` values across recent entries. A date missing from the sequence is the headline. `snapshot_date` is a UTC date and the machine is UTC+8, so the hour a run started decides which date it lands on. The task is scheduled for 03:00 local, which is 19:00 UTC the **previous** day — a run at its scheduled hour writes yesterday's UTC date. A catch-up run after the machine wakes is usually past 08:00 local and writes the current one. Either way the sequence should stay one-per-UTC-date; the failure mode to watch for is an overnight-on day followed by an overnight-off day, which skips a UTC date outright.

3. **Did any job fail?** Check the `jobs` array. `detail` says `complete`, `failed (n attempts)`, or `still running after Ns`.

4. **Is it growing?** `repositories` and `discovered` across the last few entries. Flat `discovered` for a week is worth mentioning; it is what the discovery-idle alert would catch if the cluster were up to fire it.

If the log file does not exist, or its newest entry is more than about 36 hours old, the scheduled task itself is not running. Say that plainly — it means nothing is collecting and every day from here is being lost.

## Reporting

Three or four lines when everything is fine: days of history, repository count, and that the last run was clean. Do not write a report about a healthy system.

If a day is missing or the task has stopped firing, lead with that and say how many days have been lost. Suggest the obvious cause — machine off all day, scheduled task disabled, Docker failing to start — rather than investigating deeply.

Do not start Docker, change code, or push anything. This is an observation run against a log file.
