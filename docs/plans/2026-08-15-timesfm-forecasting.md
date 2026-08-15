# Repository Growth Forecasting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add TimesFM 2.5 zero-shot forecasting as a new, optional daily pipeline stage that predicts 7-day-forward star counts (with a 30-day path gated behind its own, not-yet-validated threshold) for the top 50 repositories by recent star velocity.

**Architecture:** A new `pipeline/forecast/` package, invoked as its own K8s CronJob (`hecate-forecast`) between `hecate-dbt` and `hecate-backup`. Reads `raw_repositories`/`repository_snapshots` directly (no dbt-mart dependency), runs TimesFM 2.5 zero-shot per repository with quantile output, gates on real spike-derived confidence thresholds, and writes to a new `repository_forecasts` table. A new Grafana panel and three layers of output-quality verification (row-count sanity, degeneracy check, ongoing backtest) round it out.

**Tech Stack:** TimesFM 2.5 (`timesfm[torch]`, CPU-only), the existing `PostgreSQLLoader`/`psycopg2` pattern, Prometheus via the existing `pipeline/metrics.py` + `pipeline/server.py` gauge-refresh pattern, a new Docker build target mirroring `dbt`/`rag`.

**Spec:** `docs/specs/2026-08-15-timesfm-forecasting-design.md`

## Global Constraints

- The 7-day confidence gate is `days_observed >= 14` - this is the real, spike-validated threshold (58.7% error improvement over naive, 80.7% quantile calibration), not the originally-guessed 2x-horizon heuristic.
- The 30-day confidence gate is `days_observed >= 60` - this one was **never** spike-tested (the spike only backtested 7-day-ahead forecasts) and stays on the untested heuristic until it gets a backtest of its own. Never claim it as validated.
- Below its gate, a horizon produces **no real forecast** - write a row with `suppressed_reason` set and every `predicted_stars_p*` column `NULL`, never a fabricated number.
- Repository selection: top 50 by `stars_gained_1d`, computed directly from `repository_snapshots` (not the `fct_repository_growth` dbt mart) - this job must not depend on `hecate-dbt` having succeeded first.
- `hecate-forecast` is **optional** in the daily sequence, matching `hecate-embed`'s precedent - its failure is logged and does not fail the day's run.
- The TimesFM checkpoint is pinned to the exact revision the spike validated: `google/timesfm-2.5-200m-pytorch` at revision `1d952420fba87f3c6dee4f240de0f1a0fbc790e3` - baked into the Docker image at build time, never downloaded at CronJob runtime.
- `baseline_stars` is stored on every forecast row (not a separately-computed "gained" column), so "gained" stays anchored to the star count the forecast was actually made against, however long after the fact it's queried.
- `timesfm[torch]` and its dependencies live only in a new `requirements-forecast.txt` / `forecast` Docker target - the base 51MB pipeline image and the `rag` image must be completely unaffected.

---

### Task 1: `repository_forecasts` table and its loader methods

**Files:**
- Modify: `pipeline/exceptions.py`
- Modify: `pipeline/loader.py`
- Test: `tests/test_loaders_integration.py`

**Interfaces:**
- Produces: `ForecastError(HecateError)`; `PostgreSQLLoader.top_forecast_targets(n: int) -> list[dict]` (each with `id`, `name`, `stars`, `stars_gained_1d`); `PostgreSQLLoader.snapshot_series(repository_id: str) -> list[tuple[date, int | None]]` (ordered by `captured_on`, `(date, stars)` pairs); `PostgreSQLLoader.write_forecasts(rows: list[dict]) -> int`; `PostgreSQLLoader.forecast_rows_for(forecast_date) -> list[dict]`.

- [ ] **Step 1: Add `ForecastError`**

In `pipeline/exceptions.py`, add after `EmbeddingError`:

```python
class ForecastError(HecateError):
    """A forecast could not be produced or stored."""
```

- [ ] **Step 2: Write the failing integration tests**

Add to `tests/test_loaders_integration.py`, after the existing snapshot tests (the file already has `pytestmark = pytest.mark.integration`, the `loader` fixture, and the `query`/`snapshots` helpers - reuse them, don't redefine):

```python
def test_top_forecast_targets_ranks_by_one_day_gain(loader):
    loader.load_repositories([ROW, dict(ROW, id="github_2", name="smaller", stars=100)])
    loader.snapshot(with_mentions=False)
    loader.load_repositories([dict(ROW, stars=185532), dict(ROW, id="github_2", name="smaller", stars=150)])
    loader.snapshot(with_mentions=False)

    targets = loader.top_forecast_targets(n=10)
    assert [t["id"] for t in targets] == ["github_2", "github_1"]
    assert targets[0]["stars_gained_1d"] == 50
    assert targets[1]["stars_gained_1d"] == 100


def test_top_forecast_targets_handles_a_repository_with_one_day_of_history(loader):
    # - Only one snapshot exists yet, so there's no previous day to diff
    #   against. Must not crash, and must not be excluded - a single-day
    #   repository is a real candidate the confidence gate will suppress
    #   later, not something this ranking step should silently drop.
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)

    targets = loader.top_forecast_targets(n=10)
    assert len(targets) == 1
    assert targets[0]["stars_gained_1d"] is None


def test_top_forecast_targets_respects_the_limit(loader):
    for i in range(5):
        loader.load_repositories([dict(ROW, id=f"github_{i}", name=f"repo{i}", stars=100 + i)])
    loader.snapshot(with_mentions=False)

    assert len(loader.top_forecast_targets(n=3)) == 3


def test_snapshot_series_returns_the_full_daily_history_in_order(loader):
    loader.load_repositories([ROW])
    loader.snapshot(with_mentions=False)
    loader.load_repositories([dict(ROW, stars=185500)])
    loader.snapshot(with_mentions=False)

    series = loader.snapshot_series("github_1")
    assert [stars for _, stars in series] == [185432, 185500]
    assert series[0][0] < series[1][0]


def test_snapshot_series_for_an_unknown_repository_is_empty(loader):
    assert loader.snapshot_series("github_does_not_exist") == []


FORECAST_ROW = {
    "repository_id": "github_1",
    "forecast_date": "2026-08-21",
    "horizon_days": 7,
    "days_observed": 14,
    "baseline_stars": 185432,
    "predicted_stars_p10": 185600,
    "predicted_stars_p50": 185700,
    "predicted_stars_p90": 185900,
    "suppressed_reason": None,
    "model_version": "timesfm-2.5-200m-pytorch@1d952420fba87f3c6dee4f240de0f1a0fbc790e3",
    "generated_at": "2026-08-21T03:35:00+00:00",
}


def test_writing_forecasts_twice_for_the_same_day_replaces_rather_than_appends(loader):
    loader.load_repositories([ROW])
    loader.write_forecasts([FORECAST_ROW])
    loader.write_forecasts([dict(FORECAST_ROW, predicted_stars_p50=999999)])

    rows = loader.forecast_rows_for("2026-08-21")
    assert len(rows) == 1
    assert rows[0]["predicted_stars_p50"] == 999999


def test_a_suppressed_forecast_is_stored_with_null_predictions_not_omitted(loader):
    loader.load_repositories([ROW])
    suppressed = dict(
        FORECAST_ROW,
        days_observed=5,
        predicted_stars_p10=None,
        predicted_stars_p50=None,
        predicted_stars_p90=None,
        suppressed_reason="insufficient_history",
    )
    loader.write_forecasts([suppressed])

    rows = loader.forecast_rows_for("2026-08-21")
    assert len(rows) == 1
    assert rows[0]["suppressed_reason"] == "insufficient_history"
    assert rows[0]["predicted_stars_p50"] is None


def test_forecast_rows_for_a_different_date_are_unaffected(loader):
    loader.load_repositories([ROW])
    loader.write_forecasts([FORECAST_ROW])
    assert loader.forecast_rows_for("2026-08-22") == []
```

- [ ] **Step 2b: Run tests to verify they fail**

Run: `HECATE_INTEGRATION=1 pytest tests/test_loaders_integration.py -v -k forecast`
Expected: FAIL with `AttributeError: 'PostgreSQLLoader' object has no attribute 'top_forecast_targets'` (needs a real Postgres - `docker compose up -d postgres` first if one isn't already running).

- [ ] **Step 3: Add the table, the upsert, and the four methods**

In `pipeline/loader.py`, add to the `CREATE_TABLE` string, right after the `repository_snapshots` block and its indexes (after line 137's closing of the `idx_repository_snapshots_captured_on` index, before the `idx_raw_repositories_source` index):

```sql
-- - One row per repository per forecast horizon per day. Suppressed rows
--   are written, not omitted - "not enough history yet" is a queryable
--   fact, the same "NULL, not absent" discipline repository_snapshots and
--   fct_repository_growth already follow.
CREATE TABLE IF NOT EXISTS repository_forecasts (
    repository_id VARCHAR NOT NULL REFERENCES raw_repositories(id) ON DELETE CASCADE,
    forecast_date DATE NOT NULL,
    horizon_days INTEGER NOT NULL,
    days_observed INTEGER NOT NULL,
    -- - Stars as of forecast_date, not "current stars" - so a later query
    --   computing predicted_stars_p50 - baseline_stars always anchors to
    --   what the forecast was actually made against, not whatever today's
    --   count happens to be.
    baseline_stars INTEGER NOT NULL,
    predicted_stars_p10 INTEGER,
    predicted_stars_p50 INTEGER,
    predicted_stars_p90 INTEGER,
    suppressed_reason VARCHAR,
    model_version VARCHAR NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (repository_id, forecast_date, horizon_days)
);

CREATE INDEX IF NOT EXISTS idx_repository_forecasts_forecast_date
    ON repository_forecasts(forecast_date);
```

Add after the `MENTION_COLUMNS` tuple near the top of the file:

```python
FORECAST_COLUMNS = (
    "repository_id", "forecast_date", "horizon_days", "days_observed",
    "baseline_stars", "predicted_stars_p10", "predicted_stars_p50",
    "predicted_stars_p90", "suppressed_reason", "model_version", "generated_at",
)
```

Add after `UPSERT_MENTION`:

```python
UPSERT_FORECAST = f"""
INSERT INTO repository_forecasts ({", ".join(FORECAST_COLUMNS)})
VALUES %s
ON CONFLICT (repository_id, forecast_date, horizon_days) DO UPDATE SET
    days_observed = EXCLUDED.days_observed,
    baseline_stars = EXCLUDED.baseline_stars,
    predicted_stars_p10 = EXCLUDED.predicted_stars_p10,
    predicted_stars_p50 = EXCLUDED.predicted_stars_p50,
    predicted_stars_p90 = EXCLUDED.predicted_stars_p90,
    suppressed_reason = EXCLUDED.suppressed_reason,
    model_version = EXCLUDED.model_version,
    generated_at = EXCLUDED.generated_at
"""
```

Add these four methods to `PostgreSQLLoader`, after `rows_for` and before `close`:

```python
    def top_forecast_targets(self, n: int) -> list[dict]:
        """Repositories ranked by yesterday-to-today star gain, best first.

        Computed directly from repository_snapshots rather than the
        fct_repository_growth dbt mart, so this job's own repository
        selection does not depend on hecate-dbt having succeeded first -
        the same reasoning that already has this file reading
        raw_repositories directly elsewhere.

        A repository with only one snapshot has no previous day to diff
        against and comes back with stars_gained_1d = NULL rather than
        being excluded - it's a real candidate the confidence gate (not
        this ranking) will decide whether to suppress.
        """
        with self.transaction() as cur:
            cur.execute(
                """
                WITH ranked AS (
                    SELECT repository_id, stars,
                           stars - LAG(stars) OVER (
                               PARTITION BY repository_id ORDER BY captured_on
                           ) AS gained_1d,
                           ROW_NUMBER() OVER (
                               PARTITION BY repository_id ORDER BY captured_on DESC
                           ) AS rn
                    FROM repository_snapshots
                )
                SELECT r.id, r.name, ranked.stars, ranked.gained_1d
                FROM ranked
                JOIN raw_repositories r ON r.id = ranked.repository_id
                WHERE ranked.rn = 1
                ORDER BY ranked.gained_1d DESC NULLS LAST
                LIMIT %s
                """,
                (n,),
            )
            return [
                {"id": row[0], "name": row[1], "stars": row[2], "stars_gained_1d": row[3]}
                for row in cur.fetchall()
            ]

    def snapshot_series(self, repository_id: str) -> list[tuple]:
        """One repository's full daily star history, oldest first.

        The context TimesFM forecasts from. Empty for a repository with no
        snapshots yet rather than an error - the caller's confidence gate
        suppresses a series this short on its own.
        """
        with self.transaction() as cur:
            cur.execute(
                "SELECT captured_on, stars FROM repository_snapshots "
                "WHERE repository_id = %s ORDER BY captured_on",
                (repository_id,),
            )
            return cur.fetchall()

    def write_forecasts(self, rows: list[dict]) -> int:
        """Upsert a batch of forecast rows. Returns how many were sent."""
        if not rows:
            return 0
        values = [tuple(row.get(column) for column in FORECAST_COLUMNS) for row in rows]
        with self.transaction() as cur:
            execute_values(cur, UPSERT_FORECAST, values, page_size=len(values))
        self.log.info("forecasts written", extra={"context": {"rows": len(values)}})
        return len(values)

    def forecast_rows_for(self, forecast_date) -> list[dict]:
        """Read back what's stored for one date - the row-count sanity
        check runs on this rather than trusting the job's own exit code."""
        columns = ", ".join(FORECAST_COLUMNS)
        with self.transaction() as cur:
            cur.execute(
                f"SELECT {columns} FROM repository_forecasts WHERE forecast_date = %s",
                (forecast_date,),
            )
            return [dict(zip(FORECAST_COLUMNS, row)) for row in cur.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HECATE_INTEGRATION=1 pytest tests/test_loaders_integration.py -v -k forecast`
Expected: All PASS.

- [ ] **Step 5: Run the full test suite**

Run: `HECATE_INTEGRATION=1 pytest -q`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/exceptions.py pipeline/loader.py tests/test_loaders_integration.py
git commit -m "Add the repository_forecasts table and its loader methods"
```

---

### Task 2: Confidence gating

**Files:**
- Create: `pipeline/forecast/__init__.py`
- Create: `pipeline/forecast/gating.py`
- Test: `tests/test_forecast_gating.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `GATE_THRESHOLDS: dict[int, int]`; `suppressed_reason(days_observed: int, horizon_days: int) -> str | None`.

- [ ] **Step 1: Create the package**

Create `pipeline/forecast/__init__.py` (empty file).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_forecast_gating.py`:

```python
"""Confidence gating: the real, spike-derived thresholds, not the guessed
2x-horizon heuristic they replaced. See
docs/specs/2026-08-15-timesfm-forecasting-design.md for where the numbers
came from.
"""

from pipeline.forecast.gating import GATE_THRESHOLDS, suppressed_reason


def test_the_seven_day_gate_is_the_spike_validated_fourteen_days():
    assert GATE_THRESHOLDS[7] == 14


def test_the_thirty_day_gate_is_the_untested_sixty_day_heuristic():
    # - Never spike-tested - the spike only backtested 7-day-ahead
    #   forecasts. Pinned here so nobody quietly "fixes" this to look
    #   validated when it isn't.
    assert GATE_THRESHOLDS[30] == 60


def test_a_seven_day_forecast_is_suppressed_below_the_gate():
    assert suppressed_reason(13, 7) == "insufficient_history"


def test_a_seven_day_forecast_is_not_suppressed_at_the_gate():
    assert suppressed_reason(14, 7) is None


def test_a_thirty_day_forecast_is_suppressed_below_its_gate():
    assert suppressed_reason(59, 30) == "insufficient_history"


def test_a_thirty_day_forecast_is_not_suppressed_at_its_gate():
    assert suppressed_reason(60, 30) is None


def test_an_unrecognised_horizon_is_suppressed_with_its_own_reason():
    assert suppressed_reason(1000, 14) == "unknown horizon 14"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_forecast_gating.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.forecast.gating'`.

- [ ] **Step 4: Write the implementation**

Create `pipeline/forecast/gating.py`:

```python
"""Confidence gating for forecasts: how much history is enough to trust one.

These thresholds are not a generic rule of thumb - they're the real
breakeven points a throwaway spike found by backtesting TimesFM 2.5
zero-shot against real npm download-count series (a proxy for sparse,
count-like data - see the design doc for why) at a range of context
lengths, requiring it to beat a naive "repeat the last value" baseline by
at least 20% lower error on 7-day-ahead forecasts.

The 7-day threshold (14 days observed) is what the spike actually
validated: TimesFM technically cleared the 20% bar even at 8 days
(20.3% improvement), but 14 is where it jumps to a real, qualitative
win (58.7% improvement, quantile band calibrated at 80.7% against an
80% target) rather than a thin margin right at the line.

The 30-day threshold (60 days observed) was never spike-tested - the
spike only backtested 7-day-ahead forecasts - and stays on the
original, untested 2x-horizon heuristic until it gets a backtest of
its own. Do not treat it as validated just because it lives next to a
number that is.
"""

GATE_THRESHOLDS = {7: 14, 30: 60}


def suppressed_reason(days_observed: int, horizon_days: int) -> str | None:
    """None if a real forecast should be produced for this horizon;
    otherwise the reason it's being suppressed instead."""
    threshold = GATE_THRESHOLDS.get(horizon_days)
    if threshold is None:
        return f"unknown horizon {horizon_days}"
    if days_observed < threshold:
        return "insufficient_history"
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_forecast_gating.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/forecast/__init__.py pipeline/forecast/gating.py tests/test_forecast_gating.py
git commit -m "Add spike-derived confidence gating for forecasts"
```

---

### Task 3: TimesFM model wrapper

**Files:**
- Create: `requirements-forecast.txt`
- Create: `pipeline/forecast/model.py`
- Test: `tests/test_forecast_model.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `MODEL_ID: str`; `MODEL_REVISION: str`; `MAX_HORIZON_DAYS: int`; `load_model() -> TimesFM_2p5_200M_torch`; `forecast(model, series: list[float], horizon_days: int) -> dict` (with `p10`/`p50`/`p90` integer keys).

- [ ] **Step 1: Create `requirements-forecast.txt`**

```
# Forecasting only. Kept out of requirements.txt for the same reason dbt and
# the RAG stack live in their own files: the base pipeline image is 51.3MB
# and runs a job measured in minutes, and torch plus a 200M-parameter
# checkpoint have no business in it.
#
# CPU-only torch, pinned via the CPU wheel index explicitly - this project
# has no GPU anywhere it runs, and the default PyPI wheel pulls CUDA
# dependencies sized for a GPU that will never exist here.
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.13.0+cpu
timesfm[torch]==2.0.2
numpy==2.5.2
```

- [ ] **Step 2: Install it and confirm the exact quantile column mapping**

This step exists because the quantile output's column-to-percentile mapping
needs confirming against the real, installed library before writing code
that indexes into it - guessing wrong here would silently produce a
mislabelled p10/p90 that still looks like a number.

Run (from the repo root, in a virtualenv with `requirements-forecast.txt`
installed):

```bash
python -c "
import numpy as np
import timesfm

model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
    'google/timesfm-2.5-200m-pytorch',
    revision='1d952420fba87f3c6dee4f240de0f1a0fbc790e3',
)
config = timesfm.ForecastConfig(max_context=1024, max_horizon=7, use_continuous_quantile_head=True)
model.compile(config)

# A monotonically increasing series - forecasts should keep increasing, so
# the quantile columns should be trivially orderable low to high.
series = [float(x) for x in range(1, 30)]
point, quantiles = model.forecast(horizon=7, inputs=[np.array(series)])
print('point forecast:', point[0])
print('quantile row (last horizon step):', quantiles[0][-1])
print('is it sorted ascending?', list(quantiles[0][-1]) == sorted(quantiles[0][-1]))
"
```

Expected: an 11-value row that's monotonically ascending except possibly the
first value (the mean, which is a point estimate, not a quantile, and can
sit anywhere within the spread). Confirm which index is the mean and which
are p10 through p90 before continuing - the spike this plan is based on used
index 1 for p10 and index -1 for p90 and got well-calibrated results (~80%
coverage against an 80% target), so that mapping is the expected finding,
but confirm it against this real output rather than trusting the spike's
one-off usage. If the real output disagrees with the indices below, use
what this step actually found instead.

- [ ] **Step 3: Write the failing test**

Create `tests/test_forecast_model.py`:

```python
"""TimesFM wrapper: real model, real inference - there's no meaningful way
to fake a forecasting model's output and still test that the wrapper wires
it correctly. Skipped, visibly, when timesfm[torch] isn't installed - not
silently, the same way tests/test_loaders_integration.py fails loudly
rather than skipping quietly when it's pointed at the wrong thing.
"""

import pytest

pytest.importorskip("timesfm", reason="install requirements-forecast.txt to run this")

from pipeline.forecast.model import forecast, load_model


@pytest.fixture(scope="module")
def model():
    return load_model()


def test_forecast_returns_ordered_quantiles(model):
    series = [float(x) for x in range(1, 30)]  # monotonically increasing
    result = forecast(model, series, horizon_days=7)

    assert result["p10"] <= result["p50"] <= result["p90"]
    assert all(isinstance(v, int) for v in result.values())


def test_forecast_never_predicts_negative_stars(model):
    # - A sharply declining series, to make sure clamping at zero actually
    #   engages rather than only ever being exercised by non-negative input.
    series = [float(x) for x in range(200, 1, -20)]
    result = forecast(model, series, horizon_days=7)

    assert result["p10"] >= 0
    assert result["p50"] >= 0
    assert result["p90"] >= 0
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_forecast_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.forecast.model'` (not a skip - `timesfm` is installed per Step 2).

- [ ] **Step 5: Write the implementation**

Create `pipeline/forecast/model.py`, using the column indices Step 2 actually
confirmed (the code below assumes index 1 = p10, index 5 = p50, index -1 =
p90, per that step's expected finding - adjust the three index constants if
Step 2 found something different):

```python
"""TimesFM 2.5 zero-shot wrapper: one repository's daily star series in, a
quantile forecast out.

Pinned to the exact checkpoint revision a throwaway spike validated
(docs/specs/2026-08-15-timesfm-forecasting-design.md) - not "latest". An
unpinned rebuild of the forecast image could otherwise silently swap in a
different checkpoint with different behaviour, and forecast quality would
just quietly change with nothing to say why.
"""

import numpy as np
import timesfm

MODEL_ID = "google/timesfm-2.5-200m-pytorch"
MODEL_REVISION = "1d952420fba87f3c6dee4f240de0f1a0fbc790e3"

# - The largest horizon either gate in gating.py ever asks for. TimesFM's
#   own max_context (16,384 points) is irrelevant here - Hecate's real
#   series are two orders of magnitude shorter than that ceiling.
MAX_HORIZON_DAYS = 30

# - Confirmed against the real installed model - see Task 3 Step 2 of the
#   implementation plan this shipped from. Column 0 is the mean, not a
#   quantile; these three are the ones this module actually reports.
_P10_INDEX = 1
_P50_INDEX = 5
_P90_INDEX = -1


def load_model() -> "timesfm.TimesFM_2p5_200M_torch":
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    config = timesfm.ForecastConfig(
        max_context=1024,
        max_horizon=MAX_HORIZON_DAYS,
        use_continuous_quantile_head=True,
    )
    model.compile(config)
    return model


def forecast(model, series: list[float], horizon_days: int) -> dict:
    """One repository's quantile forecast, horizon_days ahead.

    Returns {"p10": int, "p50": int, "p90": int} - rounded and clamped at
    zero, since a star count can't go negative and a fractional star isn't
    a value that belongs on a dashboard.
    """
    _, quantiles = model.forecast(
        horizon=horizon_days, inputs=[np.array(series, dtype=np.float64)]
    )
    # - Row -1: the value at the end of the requested horizon, which is
    #   what "horizon_days forward" means here - not the first step ahead.
    last_step = quantiles[0][-1]
    return {
        "p10": max(0, round(float(last_step[_P10_INDEX]))),
        "p50": max(0, round(float(last_step[_P50_INDEX]))),
        "p90": max(0, round(float(last_step[_P90_INDEX]))),
    }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_forecast_model.py -v`
Expected: All PASS. (This loads the real 200M-parameter model on CPU - the
spike measured this as a real but non-trivial cost, not instant; give it a
minute or two, don't assume a hang.)

- [ ] **Step 7: Commit**

```bash
git add requirements-forecast.txt pipeline/forecast/model.py tests/test_forecast_model.py
git commit -m "Add the TimesFM model wrapper, pinned to the spike-validated checkpoint revision"
```

---

### Task 4: Forecast job orchestration

**Files:**
- Create: `pipeline/forecast/run.py`
- Test: `tests/test_forecast_run.py`

**Interfaces:**
- Consumes: `PostgreSQLLoader.top_forecast_targets`, `.snapshot_series`, `.write_forecasts`, `.forecast_rows_for` (Task 1); `pipeline.forecast.gating.suppressed_reason` (Task 2); `pipeline.forecast.model.load_model`, `.forecast`, `.MODEL_ID`, `.MODEL_REVISION` (Task 3).
- Produces: `TOP_N = 50`; `HORIZONS = (7, 30)`; `build_forecast_row(repository_id, forecast_date, horizon_days, series, model) -> dict`; `run(config: Config) -> dict` (a stats dict: `{"repositories": int, "forecasts_written": int, "suppressed": int, "degenerate": int}`); `main() -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forecast_run.py`. This tests `build_forecast_row` directly
(pure logic, given a series and a fake model) rather than the full `run()`,
which needs a real database and a real model - those are covered by the
manual verification checklist in Task 10, matching how this project has
always treated "does the real thing work end to end" as a live check, not
a unit test with everything faked.

```python
"""Forecast job orchestration: building one row is pure enough to test
directly. The full run() needs a real database and a real model - see
Task 8's manual verification checklist for that.
"""

from datetime import date

from pipeline.forecast.run import build_forecast_row


class FakeModel:
    """Stands in for pipeline.forecast.model - a model that always predicts
    a fixed, known jump, so the test can check build_forecast_row wires the
    real forecast() call's output into the row correctly without needing a
    real 200M-parameter model loaded for a pure-logic test."""

    def __init__(self, prediction):
        self.prediction = prediction
        self.calls = []

    def forecast_call(self, series, horizon_days):
        self.calls.append((series, horizon_days))
        return self.prediction


def test_a_well_observed_series_gets_a_real_forecast(monkeypatch):
    import pipeline.forecast.run as run_module

    model = FakeModel({"p10": 190, "p50": 200, "p90": 210})
    monkeypatch.setattr(run_module, "forecast", lambda m, series, horizon_days: m.forecast_call(series, horizon_days))

    series = [(date(2026, 8, 1 + i), 180 + i) for i in range(14)]
    row = build_forecast_row("github_1", date(2026, 8, 21), 7, series, model)

    assert row["repository_id"] == "github_1"
    assert row["horizon_days"] == 7
    assert row["days_observed"] == 14
    assert row["baseline_stars"] == 193  # the series's last stars value
    assert row["predicted_stars_p10"] == 190
    assert row["predicted_stars_p50"] == 200
    assert row["predicted_stars_p90"] == 210
    assert row["suppressed_reason"] is None
    assert model.calls == [([180 + i for i in range(14)], 7)]


def test_a_thinly_observed_series_is_suppressed_without_calling_the_model(monkeypatch):
    import pipeline.forecast.run as run_module

    model = FakeModel({"p10": 1, "p50": 1, "p90": 1})
    monkeypatch.setattr(run_module, "forecast", lambda m, series, horizon_days: m.forecast_call(series, horizon_days))

    series = [(date(2026, 8, 1 + i), 180 + i) for i in range(5)]
    row = build_forecast_row("github_1", date(2026, 8, 21), 7, series, model)

    assert row["suppressed_reason"] == "insufficient_history"
    assert row["predicted_stars_p10"] is None
    assert row["predicted_stars_p50"] is None
    assert row["predicted_stars_p90"] is None
    assert row["days_observed"] == 5
    assert model.calls == []  # no wasted inference on a series that's gated out


def test_a_gap_day_is_forward_filled_before_forecasting(monkeypatch):
    import pipeline.forecast.run as run_module

    model = FakeModel({"p10": 1, "p50": 2, "p90": 3})
    captured = {}

    def fake_forecast(m, series, horizon_days):
        captured["series"] = series
        return m.forecast_call(series, horizon_days)

    monkeypatch.setattr(run_module, "forecast", fake_forecast)

    # - A None in the middle - a day repository_snapshots has no row for
    #   this repository (stars can genuinely be None if a source outage
    #   left it null; the more common real gap is a missing captured_on
    #   day entirely, but a None value exercises the same fill path).
    series = [
        (date(2026, 8, 1), 100), (date(2026, 8, 2), None), (date(2026, 8, 3), 110)
    ] + [(date(2026, 8, 4 + i), 110 + i) for i in range(11)]
    build_forecast_row("github_1", date(2026, 8, 21), 7, series, model)

    assert captured["series"][1] == 100  # forward-filled from the day before, not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_forecast_run.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.forecast.run'`.

- [ ] **Step 3: Write the implementation**

Create `pipeline/forecast/run.py`:

```python
"""The forecast job: top repositories in, a gated quantile forecast for
each supported horizon out.

Optional in ops/windowed-run.ps1's sequence, the same way hecate-embed is -
a failure here is logged and does not fail the day, since a forecast is an
addition on top of the daily collection rather than part of it.
"""

import sys
from datetime import date, datetime, timezone

from pipeline.config import Config
from pipeline.exceptions import ForecastError, HecateError
from pipeline.forecast.gating import suppressed_reason
from pipeline.forecast.model import MODEL_ID, MODEL_REVISION, forecast, load_model
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger

TOP_N = 50
HORIZONS = (7, 30)

MODEL_VERSION = f"{MODEL_ID.rsplit('/', 1)[-1]}@{MODEL_REVISION}"

# - Flagged (not failed) when this fraction or more of today's real,
#   non-suppressed forecasts predict no change from baseline at all - the
#   sign of a model that's echoing its input rather than forecasting,
#   which a crash-based check would never catch (see the design doc's
#   verification section).
DEGENERACY_FRACTION_THRESHOLD = 0.5


def _forward_fill(series: list[tuple]) -> list[float]:
    """The stars column only, with any None carried forward from the prior
    day - TimesFM needs a clean numeric series, and this is a documented
    simplification, not a silent one (see the design doc)."""
    filled = []
    last = None
    for _, stars in series:
        value = stars if stars is not None else last
        filled.append(value)
        last = value
    # - Leading Nones (a repository whose very first snapshot was null)
    #   have nothing earlier to fill from - drop them rather than
    #   fabricate a value with no basis at all.
    return [v for v in filled if v is not None]


def build_forecast_row(repository_id: str, forecast_date: date, horizon_days: int, series: list[tuple], model) -> dict:
    """One repository, one horizon: a real forecast if the gate clears,
    a suppressed row explaining why if it doesn't."""
    filled = _forward_fill(series)
    days_observed = len(filled)
    baseline_stars = filled[-1] if filled else 0
    reason = suppressed_reason(days_observed, horizon_days)

    row = {
        "repository_id": repository_id,
        "forecast_date": forecast_date,
        "horizon_days": horizon_days,
        "days_observed": days_observed,
        "baseline_stars": baseline_stars,
        "predicted_stars_p10": None,
        "predicted_stars_p50": None,
        "predicted_stars_p90": None,
        "suppressed_reason": reason,
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now(timezone.utc),
    }
    if reason is not None:
        return row

    prediction = forecast(model, filled, horizon_days)
    row["predicted_stars_p10"] = prediction["p10"]
    row["predicted_stars_p50"] = prediction["p50"]
    row["predicted_stars_p90"] = prediction["p90"]
    return row


def run(config: Config) -> dict:
    log = get_logger("forecast.run")
    loader = PostgreSQLLoader(config)
    loader.connect()

    try:
        targets = loader.top_forecast_targets(n=TOP_N)
        log.info("forecast targets selected", extra={"context": {"count": len(targets)}})

        model = load_model()
        today = date.today()

        rows = []
        for target in targets:
            series = loader.snapshot_series(target["id"])
            for horizon_days in HORIZONS:
                rows.append(build_forecast_row(target["id"], today, horizon_days, series, model))

        written = loader.write_forecasts(rows)

        # - Row-count sanity: read back what actually landed rather than
        #   trusting write_forecasts's own return value - the same
        #   discipline ops/windowed-run.ps1 already applies to the day as
        #   a whole, applied here to this one job.
        stored = loader.forecast_rows_for(today)
        if len(stored) != len(rows):
            raise ForecastError(
                f"expected {len(rows)} forecast rows for {today}, found {len(stored)}"
            )

        suppressed = sum(1 for r in rows if r["suppressed_reason"] is not None)
        real = [r for r in rows if r["suppressed_reason"] is None]
        degenerate = sum(1 for r in real if r["predicted_stars_p50"] == r["baseline_stars"])
        if real and degenerate / len(real) >= DEGENERACY_FRACTION_THRESHOLD:
            log.warning(
                "unusually many forecasts predict no change from baseline",
                extra={"context": {"degenerate": degenerate, "of": len(real)}},
            )

        stats = {
            "repositories": len(targets),
            "forecasts_written": written,
            "suppressed": suppressed,
            "degenerate": degenerate,
        }
        log.info("forecast run complete", extra={"context": stats})
        return stats
    finally:
        loader.close()


def main() -> int:
    log = get_logger("forecast.run")
    try:
        run(Config())
    except HecateError as exc:
        # - Non-zero, deliberately - ops/windowed-run.ps1 treats this job
        #   as optional and carries on, but a job that exits 0 having
        #   written nothing is indistinguishable from one that worked.
        log.error("forecast run failed", extra={"context": {"error": str(exc)}})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_forecast_run.py -v`
Expected: All PASS.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/forecast/run.py tests/test_forecast_run.py
git commit -m "Add the forecast job's orchestration, with row-count and degeneracy checks"
```

---

### Task 5: Prometheus metrics

**Files:**
- Modify: `pipeline/metrics.py`
- Modify: `pipeline/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `repository_forecasts` table (Task 1).
- Produces: `metrics.forecast_rows` (Gauge, labels `horizon_days`, `suppressed`); `metrics.forecast_degenerate_fraction` (Gauge); a new query in `pipeline/server.py`'s refresh cycle.

- [ ] **Step 1: Add the gauges**

In `pipeline/metrics.py`, add after `rag_context_cache`:

```python
# - Phase 4. Read back from repository_forecasts on each scrape, the same
#   reason repositories/last_extraction_age already are: hecate-forecast is
#   a one-shot CronJob pod, and its in-process counters vanish before
#   Prometheus can scrape them.

forecast_rows = Gauge(
    "hecate_forecast_rows",
    "Today's forecast rows, by horizon and whether they were suppressed",
    ["horizon_days", "suppressed"],
)

forecast_degenerate_fraction = Gauge(
    "hecate_forecast_degenerate_fraction",
    "Fraction of today's real (non-suppressed) forecasts predicting no change from baseline",
)
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_server.py`:

```python
"""The metrics server's gauge refresh: read back from Postgres, not
accumulated in-process - a scheduled job's own counters vanish with its pod
before Prometheus can scrape them, which is the whole reason this module
exists.
"""

import pytest

from pipeline.config import Config
from pipeline.loader import PostgreSQLLoader
from pipeline.server import refresh_forecast_gauges

pytestmark = pytest.mark.integration

from tests.test_loaders_integration import ROW, TEST_SCHEMA, wanted


@pytest.fixture
def loader():
    if not wanted():
        pytest.skip("set HECATE_INTEGRATION=1 to run against a real database")
    loader = PostgreSQLLoader(Config())
    loader.connect()
    with loader.conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
    loader.conn.commit()
    loader.create_tables()
    with loader.conn.cursor() as cur:
        cur.execute("TRUNCATE raw_repositories CASCADE")
    loader.conn.commit()
    yield loader
    loader.close()


def test_refresh_forecast_gauges_counts_real_and_suppressed_separately(loader):
    from datetime import date, datetime, timezone

    loader.load_repositories([ROW])
    today = date.today()
    loader.write_forecasts([
        {
            "repository_id": "github_1", "forecast_date": today, "horizon_days": 7,
            "days_observed": 14, "baseline_stars": 100,
            "predicted_stars_p10": 101, "predicted_stars_p50": 102, "predicted_stars_p90": 103,
            "suppressed_reason": None, "model_version": "test", "generated_at": datetime.now(timezone.utc),
        },
        {
            "repository_id": "github_1", "forecast_date": today, "horizon_days": 30,
            "days_observed": 14, "baseline_stars": 100,
            "predicted_stars_p10": None, "predicted_stars_p50": None, "predicted_stars_p90": None,
            "suppressed_reason": "insufficient_history", "model_version": "test", "generated_at": datetime.now(timezone.utc),
        },
    ])

    counts = refresh_forecast_gauges(loader)
    assert counts == {(7, False): 1, (30, True): 1}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `HECATE_INTEGRATION=1 pytest tests/test_server.py -v`
Expected: FAIL with `ImportError: cannot import name 'refresh_forecast_gauges'`.

- [ ] **Step 4: Add `refresh_forecast_gauges` and wire it into the serve loop**

In `pipeline/server.py`, add after the existing `refresh` function:

```python
FORECAST_STATS_QUERY = """
SELECT horizon_days, suppressed_reason IS NOT NULL, count(*)
FROM repository_forecasts
WHERE forecast_date = current_date
GROUP BY horizon_days, suppressed_reason IS NOT NULL
"""

FORECAST_DEGENERACY_QUERY = """
SELECT count(*) FILTER (WHERE predicted_stars_p50 = baseline_stars), count(*)
FROM repository_forecasts
WHERE forecast_date = current_date AND suppressed_reason IS NULL
"""


def refresh_forecast_gauges(loader: PostgreSQLLoader) -> dict:
    """Pull today's forecast row counts and degeneracy fraction into the
    gauges. Returns {(horizon_days, suppressed): count} for testing."""
    with loader.transaction() as cur:
        cur.execute(FORECAST_STATS_QUERY)
        rows = cur.fetchall()

    counts = {}
    for horizon_days, suppressed, count in rows:
        metrics.forecast_rows.labels(horizon_days=str(horizon_days), suppressed=str(suppressed)).set(count)
        counts[(horizon_days, suppressed)] = count

    with loader.transaction() as cur:
        cur.execute(FORECAST_DEGENERACY_QUERY)
        degenerate, total = cur.fetchone()
    metrics.forecast_degenerate_fraction.set(degenerate / total if total else 0)

    return counts
```

In `serve()`'s try block, right after the existing `counts = refresh(loader)` line, add:

```python
                refresh_forecast_gauges(loader)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `HECATE_INTEGRATION=1 pytest tests/test_server.py -v`
Expected: All PASS.

- [ ] **Step 6: Run the full test suite**

Run: `HECATE_INTEGRATION=1 pytest -q`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add pipeline/metrics.py pipeline/server.py tests/test_server.py
git commit -m "Add Prometheus gauges for today's forecast rows and degeneracy fraction"
```

---

### Task 6: Docker, K8s CronJob, and windowed-run.ps1 wiring

**Files:**
- Create: `k8s/11-forecast-cronjob.yaml`
- Modify: `Dockerfile`
- Modify: `ops/windowed-run.ps1`

**Interfaces:** None - deployment/ops, verified against the real machine per Task 10, not unit-tested.

- [ ] **Step 1: Add the `forecast` build target to the Dockerfile**

In `Dockerfile`, add a new stage after the `rag` stage's `CMD` line (line 75) and before the final untargeted pipeline stage (`FROM python:3.11-slim` at line 78):

```dockerfile
# - Also before the pipeline stage, same reasoning as dbt and rag: an
#   untargeted build has to keep producing the pipeline image.
#
# A fourth image rather than a fatter one, and the biggest one here by far -
# torch plus a 200M-parameter checkpoint dwarf everything else in this
# file. The checkpoint is prefetched below at build time, not downloaded at
# CronJob runtime, so a 3am pod never makes a surprise network call to
# HuggingFace - it either runs from what's baked in, or the build itself
# fails somewhere visible.
FROM python:3.11-slim AS forecast

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/home/hecate/.local/bin:$PATH \
    HF_HOME=/home/hecate/.cache/huggingface

RUN useradd --create-home --uid 1000 hecate

USER hecate
WORKDIR /app

COPY --chown=hecate:hecate requirements.txt requirements-forecast.txt ./
RUN pip install --user --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-forecast.txt

# - Pinned to the exact revision the spike validated - see
#   docs/specs/2026-08-15-timesfm-forecasting-design.md. A different
#   revision here would silently invalidate that spike's result.
RUN python -c "\
import timesfm; \
timesfm.TimesFM_2p5_200M_torch.from_pretrained( \
    'google/timesfm-2.5-200m-pytorch', \
    revision='1d952420fba87f3c6dee4f240de0f1a0fbc790e3', \
)"

COPY --chown=hecate:hecate pipeline/ ./pipeline/

CMD ["python", "-m", "pipeline.forecast.run"]
```

- [ ] **Step 2: Create the CronJob manifest**

Create `k8s/11-forecast-cronjob.yaml`, mirroring `k8s/09-embed-cronjob.yaml`'s
structure:

```yaml
# Forecasts star growth for the top 50 repositories by recent velocity, after
# collection and dbt on the same reasoning as the other jobs: it reads
# raw_repositories and repository_snapshots directly, not a dbt mart, so it
# doesn't strictly need hecate-dbt to have run first - but the slot keeps
# every real-time/derived-data job grouped together for a reader.
#
# Optional, like hecate-embed: a failure here is logged and does not fail
# the day. This is new, ML-dependency-heavy, and forecasts are inherently
# best-effort while Hecate's own history is still short - see
# docs/specs/2026-08-15-timesfm-forecasting-design.md.

apiVersion: batch/v1
kind: CronJob
metadata:
  name: hecate-forecast
  namespace: hecate
spec:
  schedule: "45 3 * * *"

  # - Suspended like the others; the windowed run creates the Job. See
  #   03-cronjob.yaml for why the schedule above is documentation rather
  #   than something that fires.
  suspend: true

  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 3600

  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        metadata:
          labels:
            app: hecate-pipeline
        spec:
          restartPolicy: OnFailure
          containers:
            - name: forecast
              # - Built from --target forecast. Not yet published alongside
              #   hecate/hecate-dbt/hecate-rag - build and load it locally
              #   (see the ops/realtime/README.md pattern for docker build
              #   --target ... this project already uses) before this
              #   CronJob can actually run; document the real publish step
              #   once this has been verified against the real machine.
              image: ghcr.io/yuuzulight/hecate-forecast:2.0.0
              env:
                - name: DB_HOST
                  value: postgres.hecate.svc.cluster.local
                - name: DB_NAME
                  value: hecate
                - name: DB_USER
                  valueFrom:
                    secretKeyRef:
                      name: db-secret
                      key: username
                - name: DB_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: db-secret
                      key: password
              resources:
                requests:
                  cpu: 500m
                  memory: 1Gi
                limits:
                  cpu: "2"
                  memory: 2Gi
```

- [ ] **Step 3: Wire it into the daily sequence**

In `ops/windowed-run.ps1`, change the `$sequence` line (currently line 324):

```powershell
    $sequence = @('hecate-daily', 'hecate-dbt', 'hecate-backup', 'hecate-embed')
```

to:

```powershell
    $sequence = @('hecate-daily', 'hecate-dbt', 'hecate-forecast', 'hecate-backup', 'hecate-embed')
```

And change the `$optional` line (currently line 331):

```powershell
    $optional = @('hecate-embed')
```

to:

```powershell
    $optional = @('hecate-forecast', 'hecate-embed')
```

- [ ] **Step 4: Commit**

```bash
git add Dockerfile k8s/11-forecast-cronjob.yaml ops/windowed-run.ps1
git commit -m "Add the hecate-forecast job: Docker target, CronJob, and windowed-run sequencing"
```

---

### Task 7: Dashboard panel and schema docs

**Files:**
- Modify: `k8s/monitoring/dashboards/hecate.json`
- Modify: `docs/DATA_SCHEMA.md`

**Interfaces:** None - visibility and documentation, no code.

- [ ] **Step 1: Add the panel**

In `k8s/monitoring/dashboards/hecate.json`, add a new panel object to the
`panels` array (find the closing of the "Fastest growing" panel object -
the one whose `rawSql` starts `SELECT r.name, r.source, g.stars, g.stars_gained_1d`
- and insert this as the next element in the array, adjusting `gridPos.y`
to sit below it):

```json
{
  "type": "table",
  "title": "7-day forecast",
  "description": "TimesFM zero-shot, gated on real history: a repository with fewer than 14 observed days shows as insufficient_history rather than a fabricated number. p10/p90 is the model's own uncertainty band, not a fixed confidence interval - see docs/specs/2026-08-15-timesfm-forecasting-design.md.",
  "gridPos": {
    "h": 9,
    "w": 12,
    "x": 0,
    "y": 38
  },
  "datasource": {
    "type": "grafana-postgresql-datasource",
    "uid": "postgres"
  },
  "targets": [
    {
      "format": "table",
      "rawQuery": true,
      "rawSql": "SELECT r.name, f.baseline_stars, f.predicted_stars_p50, (f.predicted_stars_p50 - f.baseline_stars) AS predicted_gain, f.predicted_stars_p10, f.predicted_stars_p90, f.suppressed_reason FROM repository_forecasts f JOIN raw_repositories r ON r.id = f.repository_id WHERE f.horizon_days = 7 AND f.forecast_date = (SELECT max(forecast_date) FROM repository_forecasts) ORDER BY predicted_gain DESC NULLS LAST LIMIT 15"
    }
  ],
  "fieldConfig": {
    "defaults": {
      "custom": {
        "minWidth": 50
      }
    },
    "overrides": [
      {
        "matcher": {
          "id": "byName",
          "options": "name"
        },
        "properties": [
          {
            "id": "custom.width",
            "value": 165
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Confirm the JSON still parses**

Run: `python -c "import json; json.load(open('k8s/monitoring/dashboards/hecate.json'))" `
Expected: no output, exit code 0 (a syntax error - e.g. a missing comma from
the insertion - would raise `json.decoder.JSONDecodeError` here).

- [ ] **Step 3: Add the schema doc section**

In `docs/DATA_SCHEMA.md`, add after the `repository_snapshots` section (after
the line "The daily figure is the difference between two snapshots. That's
the point of the table." and before the `## raw_repositories` header):

```markdown
## repository_forecasts

One row per repository per forecast horizon per day - TimesFM 2.5 zero-shot,
gated on real observed history rather than run unconditionally. See
`docs/specs/2026-08-15-timesfm-forecasting-design.md` for where the gating
thresholds came from.

| column | notes |
|---|---|
| `horizon_days` | 7 or 30. Only 7 is spike-validated; 30 is gated on an untested heuristic (see the spec) |
| `days_observed` | how much history was actually available for this repository, this day |
| `baseline_stars` | stars as of `forecast_date` - what "predicted gain" is always computed against, not whatever today's count happens to be |
| `predicted_stars_p10/p50/p90` | **null when suppressed.** The model's own uncertainty band, not a fixed confidence interval |
| `suppressed_reason` | null if a real forecast was produced; `insufficient_history` below the gate |
| `model_version` | the exact pinned checkpoint - see the spec for why this is pinned rather than "latest" |

**Suppressed rows are written, not omitted** - the same "null is not zero"
discipline every other table in this file follows. A repository showing
`insufficient_history` on the dashboard is a fact about how much history
exists, not a gap in the data.
```

- [ ] **Step 4: Commit**

```bash
git add k8s/monitoring/dashboards/hecate.json docs/DATA_SCHEMA.md
git commit -m "Add the 7-day forecast dashboard panel and document repository_forecasts"
```

---

### Task 9: Ongoing backtest against Hecate's own data

**Files:**
- Create: `pipeline/forecast/backtest.py`
- Create: `tools/forecast_backtest.py`
- Test: `tests/test_forecast_backtest.py`

**Interfaces:**
- Consumes: `PostgreSQLLoader.snapshot_series` (Task 1); `pipeline.forecast.gating.GATE_THRESHOLDS` (Task 2); `pipeline.forecast.model.load_model`, `.forecast` (Task 3).
- Produces: `mape(actual: list[float], predicted: list[float]) -> float`; `naive_forecast(context: list[float], horizon_days: int) -> list[float]`; `rolling_folds(series: list[float], context_len: int, horizon_days: int, max_folds: int = 15) -> list[tuple]`; `backtest_repository(model, series: list[float], horizon_days: int) -> dict | None`.

This is the design's third verification layer, distinct from the daily job's
row-count and degeneracy checks (Task 4): a launch-time spike (already run,
its results are in the design spec) proves the model works *today*; nothing
in the daily job's own checks would catch a slow quality regression over the
following months. This exists to be run periodically (monthly is the design
spec's suggestion) against Hecate's *own* real data once it clears the
gate - not the npm proxy data the original spike used. It's a standalone
tool, not part of the daily CronJob sequence - `tools/` already holds
`tools/measure_name_matching.py` for exactly this kind of manually-run,
not-on-the-critical-path operational script. Scheduling it (a monthly
Windows Scheduled Task, mirroring Phase 3's weekly Memurai-restart pattern)
is a natural follow-up once it's confirmed to be worth automating - not
built here, since a script that has never been run manually first isn't
one you'd trust to run unattended.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_forecast_backtest.py`. `mape`, `naive_forecast`, and
`rolling_folds` are pure functions - test them directly. `backtest_repository`
is tested with a fake model, the same reasoning `tests/test_forecast_run.py`
already used for `build_forecast_row` - a real backtest run belongs in the
manual verification checklist (Task 10), not a unit test.

```python
"""The ongoing backtest: the same methodology the original spike used
(docs/specs/2026-08-15-timesfm-forecasting-design.md), reusable against
Hecate's own real data instead of the npm proxy data the spike used.
"""

from pipeline.forecast.backtest import backtest_repository, mape, naive_forecast, rolling_folds


def test_mape_ignores_zero_actuals():
    # - A percentage error against a zero actual is undefined, not zero -
    #   counting it as zero would make a repository sitting at zero stars
    #   look like a perfect forecast no matter what was predicted.
    assert mape([0, 10], [999, 11]) == abs(10 - 11) / 10


def test_mape_of_a_perfect_forecast_is_zero():
    assert mape([10, 20, 30], [10, 20, 30]) == 0


def test_naive_forecast_repeats_the_last_context_value():
    assert naive_forecast([10, 20, 30], horizon_days=3) == [30, 30, 30]


def test_rolling_folds_produces_context_and_actual_pairs():
    series = list(range(1, 21))  # 1..20
    folds = rolling_folds(series, context_len=5, horizon_days=3, max_folds=5)

    assert len(folds) > 0
    for context, actual in folds:
        assert len(context) == 5
        assert len(actual) == 3


def test_rolling_folds_is_empty_when_the_series_is_too_short():
    assert rolling_folds([1, 2, 3], context_len=5, horizon_days=3) == []


class FakeModel:
    def __init__(self, prediction):
        self.prediction = prediction


def test_backtest_repository_compares_timesfm_against_naive(monkeypatch):
    import pipeline.forecast.backtest as backtest_module

    # - A model that always predicts flat continuation - deliberately worse
    #   than naive for this series so the comparison direction is checkable
    #   rather than assuming any particular model quality.
    monkeypatch.setattr(
        backtest_module, "forecast",
        lambda model, series, horizon_days: {"p10": series[-1], "p50": series[-1], "p90": series[-1]},
    )

    series = [float(x) for x in range(1, 31)]  # steadily increasing - naive undershoots less than a flat guess would over a longer horizon
    result = backtest_repository(FakeModel(None), series, horizon_days=3)

    assert result is not None
    assert "timesfm_mape" in result
    assert "naive_mape" in result
    assert "improvement" in result
    assert result["n_folds"] > 0


def test_backtest_repository_is_none_when_there_is_not_enough_history():
    result = backtest_repository(FakeModel(None), [1.0, 2.0, 3.0], horizon_days=7)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_forecast_backtest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.forecast.backtest'`.

- [ ] **Step 3: Write `pipeline/forecast/backtest.py`**

```python
"""The backtest methodology a throwaway spike used to find the real
confidence-gating thresholds (docs/specs/2026-08-15-timesfm-forecasting-design.md),
reusable here against Hecate's own real snapshot history instead of the
npm proxy data the spike used - this is what actually catches a quality
regression the daily job's row-count and degeneracy checks (pipeline/forecast/run.py)
cannot, since those check that a forecast was produced, not that it's good.
"""

import statistics

from pipeline.forecast.model import forecast


def naive_forecast(context: list[float], horizon_days: int) -> list[float]:
    """Repeat the last known value - the baseline TimesFM has to beat."""
    return [context[-1]] * horizon_days


def mape(actual: list[float], predicted: list[float]) -> float:
    """Mean absolute percentage error. Zero actuals are skipped - a
    percentage error against zero is undefined, not zero."""
    errors = [abs(a - p) / a for a, p in zip(actual, predicted) if a != 0]
    return statistics.mean(errors) if errors else float("nan")


def rolling_folds(series: list[float], context_len: int, horizon_days: int, max_folds: int = 15) -> list[tuple]:
    """(context_window, actual_future) pairs, stepped across the series.
    Empty if the series isn't even long enough for one fold."""
    n = len(series)
    if n < context_len + horizon_days:
        return []
    folds = []
    step = max(1, (n - context_len - horizon_days) // max_folds)
    start = 0
    while start + context_len + horizon_days <= n:
        folds.append((series[start : start + context_len], series[start + context_len : start + context_len + horizon_days]))
        start += step
    return folds


def backtest_repository(model, series: list[float], horizon_days: int) -> dict | None:
    """One repository's TimesFM-vs-naive comparison, folded across its own
    history. None if there's not enough history for even one fold."""
    folds = rolling_folds(series, context_len=max(14, horizon_days * 2), horizon_days=horizon_days)
    if not folds:
        return None

    timesfm_errors = []
    naive_errors = []
    for context, actual in folds:
        naive_errors.append(mape(actual, naive_forecast(context, horizon_days)))
        prediction = forecast(model, context, horizon_days)
        timesfm_errors.append(mape(actual, [prediction["p50"]] * horizon_days))

    timesfm_mape = statistics.mean(e for e in timesfm_errors if e == e)
    naive_mape = statistics.mean(e for e in naive_errors if e == e)
    improvement = (naive_mape - timesfm_mape) / naive_mape if naive_mape else float("nan")

    return {
        "timesfm_mape": timesfm_mape,
        "naive_mape": naive_mape,
        "improvement": improvement,
        "n_folds": len(folds),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_forecast_backtest.py -v`
Expected: All PASS.

- [ ] **Step 5: Write the CLI tool**

Create `tools/forecast_backtest.py`:

```python
"""Ongoing verification that TimesFM still beats naive on Hecate's own real
data - not just the npm proxy data the original spike used. Run this
manually, periodically (monthly is a reasonable cadence) once repositories
have enough history to backtest meaningfully.

    python -m tools.forecast_backtest

Not part of the daily CronJob sequence - see pipeline/forecast/backtest.py's
module docstring for why.
"""

import sys

from pipeline.config import Config
from pipeline.forecast.backtest import backtest_repository
from pipeline.forecast.gating import GATE_THRESHOLDS
from pipeline.forecast.model import load_model
from pipeline.loader import PostgreSQLLoader
from pipeline.logger import get_logger

HORIZON_DAYS = 7
BEAT_NAIVE_MARGIN = 0.20  # the same bar the original spike was held to


def main() -> int:
    log = get_logger("forecast.backtest")
    loader = PostgreSQLLoader(Config())
    loader.connect()

    try:
        targets = loader.top_forecast_targets(n=50)
        model = load_model()

        results = []
        for target in targets:
            series_rows = loader.snapshot_series(target["id"])
            series = [stars for _, stars in series_rows if stars is not None]
            if len(series) < GATE_THRESHOLDS[HORIZON_DAYS]:
                continue  # hasn't cleared its own gate yet - nothing to backtest
            result = backtest_repository(model, series, HORIZON_DAYS)
            if result is not None:
                results.append(result)

        if not results:
            log.info(
                "no repository has enough history to backtest yet",
                extra={"context": {"gate_days": GATE_THRESHOLDS[HORIZON_DAYS]}},
            )
            return 0

        avg_improvement = sum(r["improvement"] for r in results) / len(results)
        log.info(
            "backtest complete",
            extra={"context": {"repositories": len(results), "avg_improvement": round(avg_improvement, 4)}},
        )

        if avg_improvement < BEAT_NAIVE_MARGIN:
            log.warning(
                "forecast quality has dropped below the original spike's bar",
                extra={"context": {"avg_improvement": round(avg_improvement, 4), "required": BEAT_NAIVE_MARGIN}},
            )
    finally:
        loader.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run the full test suite**

Run: `pytest -q`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add pipeline/forecast/backtest.py tools/forecast_backtest.py tests/test_forecast_backtest.py
git commit -m "Add the ongoing backtest tool, verifying forecast quality against Hecate's own real data"
```

---

### Task 10: Full suite verification and the live-verification checklist

**Files:** None - verification only.

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -q`
Expected: All PASS, including every test from Tasks 1-9.

- [ ] **Step 2: Run the integration-tests-are-not-skipped check**

Run: `HECATE_INTEGRATION=1 pytest -q` (needs a real Postgres - `docker compose up -d postgres`)
Expected: tests run and pass, including the new `test_forecast_model.py` (needs
`requirements-forecast.txt` installed in this environment - not silently
skipped; if `timesfm` genuinely isn't installed here, `pytest.importorskip`
will report it as a visible skip in the summary, and that skip must be
accounted for explicitly rather than assumed benign, the same discipline
`ARCHITECTURE.md` already documents for `HECATE_INTEGRATION`).

- [ ] **Step 3: Build the `forecast` image and confirm its real size**

Run: `docker build --target forecast -t hecate-forecast:local .`
Expected: builds successfully. Record the real image size (`docker images
hecate-forecast:local`) against this plan's ~1.5-3GB estimate - if it's
meaningfully different, update the design spec's Docker section with the
real number rather than leaving the estimate standing unchallenged, the
same discipline Phase 3's Memurai/listener footprint was held to.

- [ ] **Step 4: Confirm every manual live check from this plan was actually run**

Not a pytest step - a checklist, because CI cannot verify any of these and
"tests pass" doesn't cover them:

- [ ] Task 3's quantile-column-mapping verification (Step 2) was actually
      run against the real installed model, and `model.py`'s `_P10_INDEX`/
      `_P50_INDEX`/`_P90_INDEX` constants match what it found - not assumed
      from the spike's prior, unverified usage.
- [ ] The `hecate-forecast` job was run for real (`kubectl create job
      hecate-forecast-now --from=cronjob/hecate-forecast -n hecate`, or via
      a full `ops/windowed-run.ps1` run once the image is built and loaded)
      and its logs show a clean `forecast run complete` with a non-zero
      `forecasts_written`, not just that the job exited 0.
- [ ] `repository_forecasts` was queried directly after that run and shows
      real rows for today, including at least one repository still showing
      `insufficient_history` if Hecate's own history hasn't yet crossed 14
      days everywhere - both outcomes should be visible, not just the
      success case.
- [ ] The Grafana panel was loaded in a real browser against the real
      dashboard and shows real data (or an honest empty/suppressed state,
      not a broken query) - the JSON-parses check in Task 7 confirms the
      panel is syntactically valid, not that Grafana renders it correctly.
- [ ] The real CPU/RAM/duration of one full `hecate-forecast` run was
      measured (`kubectl top pod` during the run, or the job's own
      duration in `kubectl get jobs`) against this plan's estimated
      `500m/1Gi requests -> 2 CPU/2Gi limits` - update the CronJob manifest
      and the design spec if the real numbers disagree meaningfully.
- [ ] Once Hecate's own history crosses the 14-day gate for the 7-day
      horizon (around 2026-08-21, per the spec), confirm at least one real,
      non-suppressed forecast actually lands - the gating logic is unit
      tested, but a unit test cannot confirm the gate's threshold and
      Hecate's real snapshot cadence actually agree with each other.
- [ ] `tools/forecast_backtest.py` (Task 9) was actually run once by hand
      against real data, not just unit tested with a fake model - confirm
      it produces a real `avg_improvement` figure and doesn't error against
      whatever repositories have cleared the gate so far, even if that set
      is still small or empty this early. A monthly cadence for running it
      going forward (a Windows Scheduled Task, mirroring Phase 3's weekly
      Memurai-restart pattern) is a real follow-up, not built by this plan.

If any of these weren't actually done, this plan is not finished no matter
what pytest says - the same standard `docs/specs/2026-08-14-realtime-ingestion-design.md`'s
integration checklist already set for the previous phase.

- [ ] **Step 5: Push and confirm CI is green**

```bash
git push
```

Check CI. Expected green, understanding per Step 4 above that CI green here
means "the code is wired correctly," not "the real forecast job, the real
image, and the real dashboard panel work" - that's what Step 4's checklist
covers, and it needs the real machine, not CI.
