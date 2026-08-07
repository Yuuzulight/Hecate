---
name: hecate-growth-check
description: One-week check on Hecate's growth and momentum data once the 7-day snapshot windows have filled
---

Check whether Hecate's growth and momentum data has become meaningful now that a week of snapshot history should exist.

## Context you need (this run starts fresh)

Hecate is a repository-intelligence pipeline at `F:\GitHub Projects\Hecate`, running on Docker Desktop's Kubernetes in namespace `hecate`. It collects from GitHub, npm, PyPI and GitLab plus Hacker News and Lobsters, and snapshots every project daily so change can be measured.

On 2026-08-07 it had only ONE day of snapshots, so every 7- and 30-day growth window was correctly null and the momentum ranking scored on attention alone. The point of this check is whether that has resolved on its own.

## What to do

Everything runs in-cluster. Query the database directly:

```
kubectl exec -n hecate postgres-0 -- psql -U dataflow -d hecate -c "<SQL>"
```

1. **Did the scheduled jobs actually run every day?**
   `SELECT captured_on, count(*) FROM repository_snapshots GROUP BY captured_on ORDER BY captured_on;`
   Missing days mean the machine was asleep or Docker was closed at 02:00. A gap cannot be backfilled — snapshots describe a moment that has passed. Report gaps plainly rather than glossing them.

2. **Have the growth windows filled?**
   `SELECT days_observed, count(*), count(stars_gained_7d) AS has_7d FROM analytics_marts.fct_repository_growth GROUP BY days_observed ORDER BY days_observed;`

3. **What is actually growing?**
   `SELECT r.name, r.source, g.stars, g.stars_gained_7d, g.stars_growth_pct_7d FROM analytics_marts.fct_repository_growth g JOIN raw_repositories r ON r.id = g.repository_id WHERE g.stars_gained_7d IS NOT NULL ORDER BY g.stars_gained_7d DESC LIMIT 10;`
   Also run it ordered by `stars_growth_pct_7d DESC` — absolute and rate should give different answers, and if they don't, say so.

4. **Is momentum a real three-signal ranking yet?**
   `SELECT r.name, r.source, m.signals_measured, m.growth_component, m.usage_component, m.attention_component, m.momentum FROM analytics_marts.fct_momentum m JOIN raw_repositories r ON r.id = m.repository_id WHERE m.momentum IS NOT NULL ORDER BY m.momentum DESC LIMIT 10;`
   The key question is whether `signals_measured` is now above 1 for a decent number of rows. If it is still 1 everywhere, the weights have never actually been exercised.

5. **Sanity-check the weights against real spread.** The momentum components were order-of-magnitude guesses made with no data. Now there is some, look at whether one component dominates the total, whether the normalisation caps (`least(..., 100)`) are clipping most rows, and whether the ranking's top entries look defensible. Say clearly if the weights look wrong — that was always expected to need revisiting.

6. **Discovery and health:** how many repositories have `origin = 'discovered'`, and did any Prometheus or Grafana alerts fire during the week.

## Reporting

Give a short written summary, not raw dumps. Lead with whether the data is now usable, then what it shows, then anything that looks wrong. Be specific about numbers.

If the growth windows are still empty because days were missed, say that first — it is the most important finding and means the month-long observation is not accumulating as intended.

Do not change any code or push anything unless something is clearly broken and the fix is obvious. This is an observation run.