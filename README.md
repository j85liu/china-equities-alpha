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
- **price-limit breaches**. The daily limit price is `preclose × (1 ± limit)` rounded half-up to the 0.01 tick, so a 0.52 → 0.49 day on an ST stock (−5.77%) correctly counts as limit-down, not a breach. Every rule is date-aware and cites its exchange source in the code:

  | board | rule | in force |
  |---|---|---|
  | main | ±10%; ST ±5% | ST moves to ±10% on **2026-07-06** (SSE 上证发〔2026〕41号; SZSE Trading Rules 2026 §3.3.13) |
  | main | IPO day 1: order band 144% / 64% of issue price (+44% / −36%) | 2014-06-13 (SSE 上证发〔2014〕37号) until registration reform |
  | main | IPO first 5 days: no limit | listings from 2023-04-10 (first registration-era listing) |
  | ChiNext | ±10%, ST ±5%, IPO day-1 band as main board | until 2020-08-23 |
  | ChiNext | ±20% (ST included); IPO first 5 days no limit | 2020-08-24 (SZSE 深证上〔2020〕515号) |
  | STAR | ±20% (ST included); IPO first 5 days no limit | since launch, 2019-07-22 |

  The IPO band edge is rounded half-up to the tick, like a daily limit. The 2015–2026 data confirms it: day-1 closes sit exactly on half-up(1.44 × issue price), e.g. 7.47 → 10.76.

  Breaches are grouped by likely cause, and the headline figure is `price_limit_unexplained`:
  - first day of the delisting period (≤ 45 days before the delist date)
  - first trade after a suspension (relistings, reverse mergers)
  - day 1 of a non-IPO listing (merger absorption, B-share to A-share conversion)
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

- **No Beijing Stock Exchange (BSE) data.** baostock has no BSE listings. For now BSE is out of
  scope: the warehouse covers SSE and SZSE only. The symbol and price-limit code
  recognise `.BJ`, but the BSE ±30% rule is unverified.
- **Index constituents are weekly snapshots, not an official change log.** baostock only
  exposes the constituent list as of its latest snapshot (`updateDate`, roughly weekly, sometimes
  months apart; e.g. the 2022-08-01 list was still current in January 2023). We sample the last
  trading day of each week, so:
  - membership changes are known only to within about one week;
  - `event_date` is baostock's snapshot date, not CSI's effective date;
  - CSI announces changes about 2 weeks before they take effect, and those announcement dates
    aren't captured (`publish_date` is NULL).
  Use `index_membership` for approximate universes; don't use it for event studies on index inclusion.
- **Price-limit rules not modelled.** These days show up as breaches; the report groups the common ones by cause:
  - first day of relisting;
  - first day of the delisting-consolidation period;
  - the three ChiNext stocks that stayed at ±10% during delisting after the 2020 reform;
  - main-board IPOs approved under the old approval regime that listed after 2023-04-10.
    These are treated as registration listings, which may hide a breach but never adds a false one.
  - Pre-2014-06-13 first days are treated as unlimited.
  - The SZSE 2014 first-day notice and the approval-regime carve-out come from secondary sources.
- **baostock is slow** (1–5 s per request from outside mainland China). `--workers N` runs N
  sessions in parallel with jittered spacing. Please keep N modest; it's a free public service.
- **Delisting dates**: baostock's `outDate` is sometimes the last trading day and sometimes the
  day after. Treat `delist_date` as accurate to within one day.
- **Universe** comes from a current snapshot of listings. Name history starts on
  the first ingest date. ST history comes from the daily `is_st` flag.
