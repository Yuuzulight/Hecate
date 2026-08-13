---
name: hecate-dashboard-check
description: Look at the Hecate dashboard the morning after the first fully automatic scheduled run
---

Check the Hecate dashboard after the scheduled run has done a day's work on its own for the first time.

## Context (this run starts fresh)

Hecate is a repository-intelligence pipeline at `F:\GitHub Projects\Hecate`, on Docker Desktop's Kubernetes in namespace `hecate`.

**Docker is not running most of the time, and that is intentional.** A Windows scheduled task, "Hecate daily run", fires at 03:00 local and runs `ops/windowed-run.ps1`: it starts Docker, runs collection, dbt and the backup back to back, checks a snapshot landed, and shuts Docker down again — about two and a half minutes. The Kubernetes CronJobs are suspended, because a fixed UTC time is missed more often than met on a machine that gets shut down.

Everything before today was triggered by hand while it was being built. **Today is the first run nobody asked for**, which is the thing actually being tested.

## First, without starting anything

Read the run log — it needs no Docker:

```
F:\GitHub Projects\Hecate\ops\logs\run-log.jsonl
```

One JSON line per run: `started_at`, `ok`, `snapshot_date`, `snapshot_rows`, `repositories`, `discovered`, a `jobs` array, and `error`. There is also `ops/logs/last-run.txt`, the console trace of the most recent run, which shows timings and how far it got.

Confirm today's entry exists and that `ok` is true. The machine is UTC+8, so a 03:00 local run is 19:00 UTC the **previous** day — `snapshot_date` on a run that fired at its scheduled hour is yesterday's UTC date, not today's. A catch-up run after the machine wakes lands on today's instead.

If there is no entry for today, that is the finding — stop and report it. Check `last-run.txt` first, and the task's own state:

```
Get-ScheduledTaskInfo -TaskName 'Hecate daily run'
```

`LastTaskResult` of 0 is success. Anything else, say so plainly rather than digging.

Entries from 8 August include several manual test runs, and two scheduled attempts that day are missing from the file — one was killed mid-run during debugging, the other wrote to a path that was later changed. Both are explained and fixed. From 9 August onward there should be exactly one entry per day, so a gap after that is real.

## Then the dashboard

Bring the cluster up without collecting a second time:

```
powershell -ExecutionPolicy Bypass -File "F:\GitHub Projects\Hecate\ops\windowed-run.ps1" -StartOnly
```

That starts Docker, waits for the database to actually answer, and leaves it running. It writes nothing to the run log. Then:

```
kubectl port-forward svc/grafana 3000:3000 -n hecate
```

and open `http://localhost:3000/d/hecate-overview`. Anonymous read access is on, so no login. Fifteen panels, default range 30 days.

**When you are finished, put it back:**

```
docker desktop stop
```

Leaving Docker running is the one way this check can cause harm — the whole arrangement exists so the machine is not carrying it all day.

## What to look at

- **Stars gained per day, by language.** Three days of points now (7, 8, 9 August) rather than one. Is anything separating from the pack, or is it still noise? Two points was a line, not a trend; three is barely better, so say so if it is too early.
- **Fastest growing.** `+1d` should be populated. `+7d` and `%7d` stay empty until seven days of snapshots — that is correct, not broken.
- **Momentum.** `signals` was 1 for every row, meaning attention alone with no growth data. Check whether it has moved above 1.
- **Being discussed but not tracked**, and **Found by being discussed** — discovery was at 8 projects. Has it found more?
- Do the tables still fit their columns? The rightmost column is where clipping shows first.

Take a screenshot if the browser pane is available. Panels lazy-mount: if one looks empty, wait a few seconds and look again before calling it broken — that produced two false alarms already.

## Reporting

Lead with whether the automatic run worked, since that is the point of today. Then what the dashboard shows, then anything that looks wrong. Be specific about numbers, and keep it short if everything is fine.

Do not change code or push anything. This is an observation run — the one action expected of you is shutting Docker down again afterwards.