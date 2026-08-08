---
name: hecate-growth-check
description: One-week check on Hecate's growth and momentum data, plus a dashboard review, once the 7-day snapshot windows have filled
---

Check whether Hecate's growth and momentum data has become meaningful now that a week of snapshot history should exist, and review the dashboard while you are there.

## Context you need (this run starts fresh)

Hecate is a repository-intelligence pipeline at `F:\GitHub Projects\Hecate`, running on Docker Desktop's Kubernetes in namespace `hecate`. It collects from GitHub, npm, PyPI and GitLab plus Hacker News and Lobsters, and snapshots every project daily so change can be measured.

On 2026-08-07 it had only ONE day of snapshots, so every 7- and 30-day growth window was correctly null and the momentum ranking scored on attention alone. The point of this check is whether that has resolved on its own.

The four CronJobs are written for 02:00, 03:00, 04:00 and Sundays 05:00 **UTC** — no `timeZone` is set, so these are not the machine's local clock. On a UTC+8 machine that is 10:00, 11:00, 12:00 and 13:00 local. `captured_on` is the UTC date too, so `current_date` in SQL lines up with it and no conversion is needed.

## Docker is not running

The machine does not keep Docker up. A scheduled task runs `ops/windowed-run.ps1` once a day, which starts Docker, runs the jobs, and shuts it down again; the CronJobs are suspended. This check needs the database, so start it yourself and put it back afterwards:

```
powershell -ExecutionPolicy Bypass -File "F:\GitHub Projects\Hecate\ops\windowed-run.ps1" -KeepDockerRunning
```

That also runs the day's collection, so use it rather than starting Docker by hand — it means this check does not cost the day's run. When you are finished, shut it down again:

```
docker desktop stop
```

If it will not start, the usual cause is stale socket stubs under `%LOCALAPPDATA%\Docker`; the script clears those before every start, so a failure here is something else and worth reporting rather than fighting.

## What to do

Everything runs in-cluster. Query the database directly:

```
kubectl exec -n hecate postgres-0 -- psql -U dataflow -d hecate -c "<SQL>"
```

1. **Did the scheduled jobs actually run every day?**
   `SELECT captured_on, count(*) FROM repository_snapshots GROUP BY captured_on ORDER BY captured_on;`
   Missing days mean the machine was off all day or the scheduled task did not fire. A gap cannot be backfilled — snapshots describe a moment that has passed. Report gaps plainly rather than glossing them.

2. **Have the growth windows filled?**
   `SELECT days_observed, count(*), count(stars_gained_7d) AS has_7d FROM analytics_marts.fct_repository_growth GROUP BY days_observed ORDER BY days_observed;`

3. **What is actually growing?**
   `SELECT r.name, r.source, g.stars, g.stars_gained_7d, g.stars_growth_pct_7d FROM analytics_marts.fct_repository_growth g JOIN raw_repositories r ON r.id = g.repository_id WHERE g.stars_gained_7d IS NOT NULL ORDER BY g.stars_gained_7d DESC LIMIT 10;`
   Also run it ordered by `stars_growth_pct_7d DESC` — absolute and rate should give different answers, and if they don't, say so.

4. **Is momentum a real three-signal ranking yet?**
   `SELECT r.name, r.source, m.signals_measured, m.growth_component, m.usage_component, m.attention_component, m.momentum FROM analytics_marts.fct_momentum m JOIN raw_repositories r ON r.id = m.repository_id WHERE m.momentum IS NOT NULL ORDER BY m.momentum DESC LIMIT 10;`
   The key question is whether `signals_measured` is now above 1 for a decent number of rows. On 2026-08-08 it was 1 for every row and `growth_component` was null across all 2,011. If it is still 1 everywhere, the weights have never actually been exercised.

5. **Sanity-check the weights against real spread.** The momentum components were order-of-magnitude guesses made with no data. Now there is some, look at whether one component dominates the total, whether the normalisation caps (`least(..., 100)`) are clipping most rows, and whether the ranking's top entries look defensible. Say clearly if the weights look wrong — that was always expected to need revisiting.

6. **Discovery and health:** how many repositories have `origin = 'discovered'`, and did any Prometheus or Grafana alerts fire during the week.

## The dashboard

Grafana allows anonymous read access, so no login is needed:

```
kubectl port-forward svc/grafana 3000:3000 -n hecate
```

Then `http://localhost:3000/d/hecate-overview`. Fifteen panels; the default range is 30 days.

Two panels were added on 2026-08-08 and had one day of data each, which is not enough to judge them. Look at them now that a week has passed:

- **Stars gained per day, by language** — should be eight lines with a week of points rather than a single column. Does any language separate from the pack, or is it noise? Counting repositories instead would have been flat, which is why it plots the daily star delta.
- **Fastest growing** — the `+7d` and `%7d` columns were empty on 2026-08-08. They should have filled.

Also worth a look:

- Do the tables still fit their columns, or has longer data started clipping again? Check the rightmost column of each, which is where it shows first.
- **Popular but going stale** excludes archived projects deliberately. Confirm nothing archived has crept back in.
- **Top languages** counts only GitHub and GitLab. PyPI is excluded because every package there is Python by definition, which had put Python at 619 against TypeScript's 80.

Take a screenshot if the browser pane is available. Panels lazy-mount, so if one looks empty, wait a few seconds and look again before calling it broken — that has caused two false alarms already.

## Reporting

Give a short written summary, not raw dumps. Lead with whether the data is now usable, then what it shows, then anything that looks wrong. Be specific about numbers.

If the growth windows are still empty because days were missed, say that first — it is the most important finding and means the month-long observation is not accumulating as intended.

Do not change any code or push anything unless something is clearly broken and the fix is obvious. This is an observation run.
