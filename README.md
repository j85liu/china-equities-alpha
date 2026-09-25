见微知著 (jiàn wēi zhī zhù) — "See the small signs, understand the big picture."

# china-equities-alpha

Alternative-data alpha research on China A-shares, built only on free data.
This repo is the ingestion layer: it pulls market and alternative data into a
local, point-in-time DuckDB warehouse.

## Setup

```bash
brew install uv            # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                    # creates .venv with Python 3.11+ and all dependencies
uv run pytest              # normalization, dedup, incremental and price-limit tests
```

Data goes to `./data` (git-ignored). Set `ALPHA_DATA_DIR` to put it somewhere else.

## Usage

```bash
# Phase 1 on the 10-stock test universe
uv run alpha ingest phase1 --test --start 2023-01-01 --end 2024-12-31

# Individual jobs
uv run alpha ingest calendar
uv run alpha ingest universe
uv run alpha ingest prices --start 2015-01-01 --workers 4     # bars + adjustment factors
uv run alpha ingest prices -s 600519 -s sz.000001              # any symbol spelling works
uv run alpha ingest index --start 2015-01-01                   # CSI 300 / CSI 500

uv run alpha report          # rebuild views and print the data-quality report
```

Every command is incremental: it fetches only date ranges it hasn't already
covered, and re-running it is a no-op. `--refetch` re-pulls covered ranges to
pick up source revisions. Rows that haven't changed are still not inserted.
`--end` defaults to yesterday.

## Architecture

```
            baostock (TCP)                 AKShare (HTTP)  [phase 2]
                 │                               │
     ┌───────────▼───────────────────────────────▼──────────┐
     │ sources/   rate limit · retry w/ backoff · timeouts  │
     └───────────┬──────────────────────────────────────────┘
                 │ raw DataFrames
     ┌───────────▼──────────────────────────────────────────┐
     │ jobs/      plain functions (settings, ctx, params)   │
     │            → JobResult; per-symbol failure isolation │
     │            symbols.normalize → 600519.SH everywhere  │
     └─────┬──────────────────────────────┬─────────────────┘
           │ untouched pull               │ typed rows + event_date/publish_date
  ┌────────▼─────────┐        ┌───────────▼──────────────────────────────┐
  │ data/raw/…parquet│        │ data/warehouse.duckdb                    │
  │ landing zone     │        │  <table>          append-only, versioned │
  └──────────────────┘        │  <table>_latest   newest version per key │
                              │  _ingest_log      coverage ledger        │
                              │  views: daily_bars_adj, st_periods,      │
                              │         suspensions, index_membership    │
                              └───────────┬──────────────────────────────┘
                                          │
                              quality.py → data/reports/quality_*.json
```

### Point-in-time and history

- Every row has `event_date` (what the row refers to), `publish_date` (when it
  became public, or NULL if the source doesn't say), and `ingested_at` (run time, UTC).
- Tables are append-only. Each row stores a hash of its content. A row is
  inserted only when its content differs from the latest stored version for
  that key. So re-runs insert nothing, and a revision at the source becomes a
  new version while the old one is kept.
- `<table>_latest` shows current values. For "what did we know at time T", filter the
  base table on `ingested_at <= T` and take the newest row per key.
- `_ingest_log` records every fetched range per dataset and key. Incremental
  jobs fill only the gaps at either end, so coverage never has holes.

### Tables (phase 1)

| table | key | event_date | publish_date |
|---|---|---|---|
| `trading_calendar` | event_date | calendar date | — |
| `securities` | symbol | list date | — (name changes show up as new versions) |
| `daily_bars` | symbol, event_date | trade date | trade date |
| `adjust_factors` | symbol, event_date | ex-date | — |
| `index_constituents` | index_code, event_date, symbol | baostock snapshot date | — |

`daily_bars` holds **unadjusted** prices. Suspended days are kept, with
`is_trading = false` and NULL volume. `is_st` is the daily ST/*ST flag.
Derived views:

- `daily_bars_adj`: `close_hfq = close × back_adj_factor` (back-adjusted, 后复权)
  is point-in-time safe. `close_qfq` (forward-adjusted, 前复权) divides by the *latest*
  factor, so it changes after every new corporate action. Don't backtest on it.
- `st_periods`, `suspensions`: derived from the daily flags.
- `index_membership`: `[first_seen, first_absent)` intervals built from the snapshots.

### Data-quality report

Written after every CLI run. It covers:
- rows, versions, event-date range, duplicate keys, exact duplicate rows and null counts for each table
- for bars: gaps against the trading calendar, bars on non-trading days, OHLC consistency, and `pct_chg` against close/preclose
- **price-limit breaches**. The limit price is `preclose × (1 ± limit)` rounded half-up to the 0.01 tick, so a 0.52 → 0.49 day on an ST stock (−5.77%) correctly counts as limit-down, not a breach. Limits used:
  - main board ±10%, ST ±5% (±10% from 2025-07-07)
  - ChiNext ±20% from 2020-08-24
  - STAR ±20%
  - BSE ±30%
  - no limit in a stock's first trading days: 5 days on the registration-based boards, day 1 otherwise

  See [limits.py](src/china_equities_alpha/limits.py).

## Orchestration

Jobs are plain functions with explicit inputs and outputs, so an Airflow task is a thin wrapper:

```python
from china_equities_alpha.config import Settings, RunContext
from china_equities_alpha.jobs.market import ingest_daily_bars

def task(ds, **_):
    return ingest_daily_bars(Settings(), RunContext(), start=..., end=date.fromisoformat(ds)).__dict__
```

DuckDB allows only one writer at a time, so run tasks that write to the same
warehouse one after another (e.g. with a pool of size 1).

## Known limitations

- **No Beijing Stock Exchange in baostock.** BSE listings need another source. The
  symbol and price-limit code already handle BSE.
- **baostock is slow** (1–5 s per request from outside mainland China). `--workers N` runs N
  sessions in parallel. Please keep N modest; it's a free public service.
- **Index history resolution** is baostock's snapshot cadence (about weekly). Snapshot dates
  are not the official effective dates, and CSI announcements (about 2 weeks earlier) aren't captured.
- **Universe** comes from a current snapshot of listings. Name history starts on
  the first ingest date. ST history comes from the daily `is_st` flag.
