# Phase 4: repository growth forecasting (scope)

## Why this scope, not a bigger one

The original idea was general TimesFM-powered forecasting across Hecate's tracked
repositories. Before building any of it, the two questions that actually decide
whether this is worth doing were checked against reality rather than assumed:
does Hecate have enough history to forecast anything meaningful, and does TimesFM
actually forecast this kind of data well at all.

**History depth, checked directly:** `repository_snapshots` - the only table with
real daily history - started 2026-08-07. As of this spec, that's 8-9 days. 7-day
growth windows in `fct_repository_growth` are just starting to populate; 30-day
windows are still `NULL` for every repository (enforced by
`dbt/tests/growth_windows_are_null_not_zero.sql`). Forecasting "30 days forward"
today would mean extrapolating from almost nothing and calling it a forecast.

**Model suitability, checked with a spike, not assumed from the paper:** star
counts are sparse, integer, bursty data - nothing like the smooth continuous
series (retail sales, web traffic) TimesFM is mostly benchmarked on, and neither
TimesFM's README nor its model card say anything about performance on this shape
of data. Rather than build the full pipeline first and find out, a throwaway
spike (not part of this codebase, not committed) backtested TimesFM 2.5 zero-shot
against real npm download-count series (a genuine, long, public, sparse/noisy
count series - GitHub's own star-history endpoint was checked and found to now
be restricted to admins/collaborators as of last month, so it isn't a usable
data source for this) at a range of context lengths, against a pre-committed bar:
beat a naive "repeat the last value" baseline by at least 20% lower MAPE, on
7-day-ahead forecasts.

Result: TimesFM clears that bar even at 8 days of context (20.3% improvement),
and clears it decisively from 14 days on (58.7% improvement, with the
quantile-uncertainty band correctly covering the real outcome 80.7% of the time -
right on target). The jump from 8 to 14 days is a real, qualitative one, not
noise, so **14 days observed - not 8, and not the untested 2x-horizon heuristic
originally guessed at - is the real, evidence-based gate for a 7-day forecast.**
Hecate's own history reaches 14 days around 2026-08-21, six days from this spec
being written.

The 30-day horizon was **not** part of the spike (the spike only tested 7-day-
ahead forecasts) and stays gated behind the original, untested 2x-horizon
heuristic (60 days observed) until it gets its own real backtest - this spec
does not claim evidence it doesn't have.

## Architecture

```
raw_repositories ---> rank by stars_gained_1d, take top 50
                              |
                              v
                  repository_snapshots (daily series, per repo)
                              |
                              v
                    hecate-forecast (NEW, K8s Job)
                    - forward-fills any gap day
                    - TimesFM 2.5 zero-shot, quantile output
                    - gates on days_observed per horizon
                              |
                              v
                    repository_forecasts (NEW table)
                              |
                              v
                    Grafana panel (hecate-overview, NEW)
```

Sequence: `hecate-daily -> hecate-dbt -> hecate-forecast (NEW) -> hecate-backup ->
hecate-embed`. After `hecate-dbt` so the day's marts are fresh (even though
forecast reads `raw_repositories`/`repository_snapshots` directly, not a mart);
before `hecate-backup` so today's forecast is captured in that day's dump.
`hecate-forecast` is **optional**, matching `hecate-embed`'s precedent - a
failure here logs and moves on, it does not fail the day's run.

## Repository selection

Top 50, ranked by `stars_gained_1d` (not raw star count, and not the momentum
score). Raw star count was the first instinct and was rejected on reflection: the
highest-star repositories in Hecate's tracked set are large, mature projects
whose day-to-day velocity is close to flat noise almost by construction - they've
already saturated most of their addressable audience. `stars_gained_1d` selects
repositories that are actually moving, and unlike 7/30-day windows it's
populated from the very first day of snapshot history, so the selection itself
isn't blocked on the same history-depth problem the forecasts are.

No dbt model for this - a plain SQL query inside the forecast job itself, so
the job's repo selection doesn't couple to `hecate-dbt`'s own success (this job
already reads `raw_repositories` directly for the same reason).

## Confidence gating and output shape

TimesFM 2.5 supports native quantile/probabilistic output
(`use_continuous_quantile_head=True`, returning mean plus the 10th-90th
percentile deciles), not just a single point estimate. This spec uses it instead
of a bare point forecast: a repository with thin history naturally produces a
wide p10-p90 band - the model expressing its own uncertainty - which is more
honest than a single confident-looking number.

A hard floor still sits under that: below `days_observed` threshold for the
horizon (14 for 7-day, 60 for 30-day, per the split above), no forecast is
produced at all - a model given a handful of days shouldn't be trusted no matter
how wide it hedges. Above the floor, the interval width itself is the
confidence signal, not a second binary switch.

Gap handling: if a repository is missing a snapshot day (a collection hiccup),
the series is forward-filled (last known value carried forward) before being
handed to the model. This is a documented simplification, not a silent one -
worth knowing about if forecast quality for a specific repository looks off.

## Schema

```sql
CREATE TABLE repository_forecasts (
    repository_id       VARCHAR NOT NULL REFERENCES raw_repositories(id) ON DELETE CASCADE,
    forecast_date        DATE NOT NULL,
    horizon_days          INTEGER NOT NULL,     -- 7 or 30
    days_observed         INTEGER NOT NULL,     -- context length actually available
    baseline_stars        INTEGER NOT NULL,     -- stars as of forecast_date - what "gained" is always anchored to
    predicted_stars_p10   INTEGER,              -- NULL if suppressed
    predicted_stars_p50   INTEGER,              -- NULL if suppressed
    predicted_stars_p90   INTEGER,              -- NULL if suppressed
    suppressed_reason      VARCHAR,              -- NULL if a real forecast was produced; else e.g. 'insufficient_history'
    model_version           VARCHAR NOT NULL,     -- 'timesfm-2.5-200m-pytorch@1d952420fba87f3c6dee4f240de0f1a0fbc790e3'
    generated_at             TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (repository_id, forecast_date, horizon_days)
);
```

Rows are written even when suppressed, mirroring `fct_repository_growth`'s "NULL,
not absent" discipline - "we checked and there wasn't enough history yet" is a
queryable fact, not silence. `baseline_stars` is stored once per row rather than
a separately-computed `predicted_stars_gained` column, so "gained" (computed as
`predicted_stars_p50 - baseline_stars` at query time) always stays anchored to
the star count the forecast was actually made against, regardless of when the
row is later read - a forecast made on day N against day N's star count doesn't
silently get reinterpreted against day N+30's star count just because someone
queries it later.

## Verification: proving the forecast is actually working, not just that the job exited 0

This project has a specific, repeated history of failures that looked green -
vacuous dbt tests, a CronJob collecting one source of four, and once, a growth
query that grouped by repository *name* instead of *id* and reported npm's
near-zero-star `vite` and GitHub's ~80k-star `vite` as the same repository,
claiming +82,239 stars gained in a day. A forecasting job is at least as easy to
make look healthy while producing confident nonsense as anything else in this
project - the output is a plausible-looking number either way. Three layers,
each catching a different failure class:

1. **Row-count sanity**, mirroring `windowed-run.ps1`'s existing pattern of
   querying Postgres directly for ground truth rather than trusting job exit
   codes: after the job claims success, independently confirm today's row count
   in `repository_forecasts` matches the expected shape (50 repos x 2 horizons,
   accounting for suppression) - not "did the code throw."
2. **Degeneracy check**: flag (log + a Prometheus counter, does not fail the
   job) if an unusual fraction of today's non-suppressed forecasts are
   identical to `baseline_stars` - the sign of a model that's just echoing its
   input rather than forecasting, which is exactly the "technically a real
   forecast, functionally useless" failure mode a crash-based check would never
   catch.
3. **Ongoing backtest, not just this spec's one-time spike**: once a
   repository's own `days_observed` clears the relevant horizon's gate,
   periodically (monthly) re-run the same beat-naive-by-20% check against
   Hecate's *own* real data, not the npm proxy used here. A launch-time spike
   proves the model works today; it says nothing about a regression introduced
   by some later change, or a slow drift in forecast quality over months. This
   is what actually catches that.

## Docker and deployment

New `forecast` build target in the existing multi-stage `Dockerfile`, mirroring
the `dbt`/`rag` targets exactly: builds on `builder`, installs a new
`requirements-forecast.txt` (`timesfm[torch]`, CPU-only torch wheel pinned
explicitly via `--index-url https://download.pytorch.org/whl/cpu` so a GPU-sized
CUDA wheel never gets pulled on a machine with no GPU to use it), copies
`pipeline/`, and **prefetches the TimesFM checkpoint at build time**, pinned to
the exact revision the spike validated -
`google/timesfm-2.5-200m-pytorch@1d952420fba87f3c6dee4f240de0f1a0fbc790e3` - not
"latest." Without the pin, rebuilding this image next month could silently swap
in a different checkpoint with different behavior, invalidating this spec's
spike without anyone noticing - the same reproducibility discipline every other
dependency in this project already gets (`requirements.txt` pins exact versions
throughout; `dbt-core==1.12.0`, not a floating version).

This keeps the base 51MB pipeline image and the `rag` image completely
unaffected - `forecast` is a new, separate, meaningfully larger image (realistic
estimate 1.5-3GB with torch plus the checkpoint; **not yet measured against the
real build**, flagged here rather than asserted as settled).

New `k8s/11-forecast-cronjob.yaml`: `suspend: true`, schedule `30 3 * * *`
(between `hecate-dbt`'s `0 3 * * *` and `hecate-backup`'s `0 4 * * *`, the same
slot pattern `hecate-embed` uses relative to `hecate-dbt`). Resource
requests/limits start at `500m/1Gi requests -> 2 CPU/2Gi limits` - wider than
`hecate-dbt`'s, since loading a 200M-parameter model needs real headroom - and,
like the image size above, **flagged as needing real measurement**, not treated
as settled, the same way Phase 3's Memurai/listener footprint was measured
against the real running services rather than left as the spec's original
estimate.

## Dashboard

One new panel on `hecate-overview` (uid unchanged), a table querying
`repository_forecasts` joined to `raw_repositories`, filtered to
`horizon_days = 7` and the latest `forecast_date`: repository name, current
stars, `predicted_stars_p50`, `predicted_stars_p50 - baseline_stars` as
predicted gain, `predicted_stars_p10`/`predicted_stars_p90` as the uncertainty
band, and `suppressed_reason` where applicable - so a repository showing
"insufficient_history" is visible on the dashboard, not silently missing from
it. Ordered by predicted gain descending, surfacing the biggest predicted
movers first. No 30-day panel yet - every 30-day row will be suppressed for
months, so a panel for it right now would be an empty table; add it once that
horizon has its own real backtest and repositories start clearing its gate.

## Cost

No new recurring cloud spend - runs on the same laptop, inside the same daily
Docker window, as every other job. The real cost is local: a new ~1.5-3GB Docker
image (not yet measured) and roughly a minute or two of added CPU-only inference
time in the daily window (not yet measured against the real job), for 50
repositories' worth of short series, once a day.

## Success criteria

- A 7-day forecast is produced (not suppressed) for any tracked repository with
  14+ days of observed history, using TimesFM's quantile output.
- Every forecast row - suppressed or not - is queryable and explains itself
  (`suppressed_reason` when applicable).
- The three-layer verification (row-count sanity, degeneracy check, ongoing
  backtest) is running, not just designed - this is the check that can actually
  catch a silent regression, and this project's own history says an untested
  check is not a real check.
- The Grafana panel shows real, non-fabricated data once the first 14-day-old
  repositories clear the gate (~2026-08-21).
- The `forecast` image build and job's real CPU/RAM/duration footprint is
  measured against the real running job, not left as this spec's estimate.

## Deliberately out of scope

- **The 30-day horizon's own backtest.** It stays gated behind the original,
  untested 2x-horizon heuristic (60 days) until it gets a spike of its own -
  this spec's spike only validated 7-day-ahead forecasts.
- **Backfilling more history from an external source.** GitHub's star-history
  endpoint is now access-restricted; no other real backfill source was
  identified during this scoping. Revisit only if a real source turns up.
- **GPU acceleration.** Docker Desktop's K8s here has no GPU passthrough; CPU-
  only inference on 50 short series was checked (during the spike run itself)
  to complete in a reasonable time, so this isn't a blocker, just a known
  ceiling if the tracked-repository count grows much larger later.
- **Momentum-based or curated-list repository selection**, as an alternative to
  `stars_gained_1d`. Discussed and set aside for the minimal version, not
  because it's a bad idea - `stars_gained_1d`'s big advantage is that it's
  already populated from day one, avoiding coupling this feature's own rollout
  to yet another history-depth wait.
