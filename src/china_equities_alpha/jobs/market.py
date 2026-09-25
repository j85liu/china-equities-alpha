"""Phase 1 — core market data from baostock.

Each job is a plain function: ``(settings, ctx, <params>) -> JobResult``.
Jobs open their own warehouse connection so they can be wrapped one-per-task
in an orchestrator (DuckDB allows a single writer, so run them serially).
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd

from ..config import RunContext, Settings
from ..resilience import JobResult
from ..sources.baostock_src import Baostock
from ..storage import TableSpec, Warehouse, write_raw
from ..symbols import board, normalize, to_baostock

log = logging.getLogger(__name__)
SOURCE = "baostock"
EARLIEST = date(1990, 12, 19)

CALENDAR = TableSpec(
    "trading_calendar",
    {"event_date": "DATE", "is_trading_day": "BOOLEAN", "publish_date": "DATE"},
    keys=("event_date",),
    description="SSE/SZSE trading calendar",
    event_date_meaning="calendar date",
    publish_date_meaning="not available",
)
SECURITIES = TableSpec(
    "securities",
    {"symbol": "VARCHAR", "name": "VARCHAR", "exchange": "VARCHAR", "board": "VARCHAR",
     "list_date": "DATE", "delist_date": "DATE", "is_listed": "BOOLEAN",
     "event_date": "DATE", "publish_date": "DATE"},
    keys=("symbol",),
    description="A-share universe incl. delisted; name changes appear as new versions",
    event_date_meaning="list date",
    publish_date_meaning="not available",
)
DAILY_BARS = TableSpec(
    "daily_bars",
    {"symbol": "VARCHAR", "event_date": "DATE", "open": "DOUBLE", "high": "DOUBLE",
     "low": "DOUBLE", "close": "DOUBLE", "preclose": "DOUBLE", "volume": "BIGINT",
     "amount": "DOUBLE", "turnover_pct": "DOUBLE", "is_trading": "BOOLEAN",
     "pct_chg": "DOUBLE", "is_st": "BOOLEAN", "publish_date": "DATE"},
    keys=("symbol", "event_date"),
    description="Unadjusted daily bars; suspended days present with is_trading = false",
    event_date_meaning="trade date",
    publish_date_meaning="trade date (public at the close)",
)
ADJ_FACTORS = TableSpec(
    "adjust_factors",
    {"symbol": "VARCHAR", "event_date": "DATE", "back_adj_factor": "DOUBLE", "publish_date": "DATE"},
    keys=("symbol", "event_date"),
    description="Cumulative backward adjustment factor, effective from the ex-date inclusive",
    event_date_meaning="ex-date (dividOperateDate)",
    publish_date_meaning="not available (announcement precedes ex-date)",
)
INDEX_CONSTITUENTS = TableSpec(
    "index_constituents",
    {"index_code": "VARCHAR", "event_date": "DATE", "symbol": "VARCHAR", "name": "VARCHAR",
     "publish_date": "DATE"},
    keys=("index_code", "event_date", "symbol"),
    description="Constituent snapshots of CSI 300 / CSI 500 (baostock snapshots, ~weekly)",
    event_date_meaning="baostock snapshot date (updateDate), not the official effective date",
    publish_date_meaning="not available (CSI announces changes ~2 weeks before effect)",
)
INDEXES = {"000300.SH": "hs300", "000905.SH": "zz500"}

TABLES = [CALENDAR, SECURITIES, DAILY_BARS, ADJ_FACTORS, INDEX_CONSTITUENTS]


# ---------------------------------------------------------------- helpers
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _to_date(col: pd.Series) -> pd.Series:
    return pd.to_datetime(col.replace("", None), errors="coerce").dt.date


def _num(col: pd.Series) -> pd.Series:
    return pd.to_numeric(col.replace("", None), errors="coerce")


def missing_range(requested: tuple[date, date], covered: tuple[date, date] | None
                  ) -> list[tuple[date, date]]:
    """Sub-ranges of ``requested`` not yet covered, kept contiguous with ``covered``.

    A request that lies entirely after the covered range is extended back to
    the covered end so coverage never has holes (same for before).
    """
    start, end = requested
    if start > end:
        return []
    if covered is None:
        return [(start, end)]
    c_start, c_end = covered
    out = []
    if start < c_start:
        out.append((start, c_start - timedelta(days=1)))
    if end > c_end:
        out.append((c_end + timedelta(days=1), end))
    return out


def default_end() -> date:
    """Yesterday: today's bar is not final until the evening update."""
    return date.today() - timedelta(days=1)


def _load_universe(wh: Warehouse, symbols: list[str] | None) -> pd.DataFrame:
    if not wh.table_exists("securities"):
        raise RuntimeError("securities table is empty; run `alpha ingest universe` first")
    uni = wh.df("SELECT symbol, list_date, delist_date FROM securities_latest")
    if symbols:
        wanted = {normalize(s) for s in symbols}
        unknown = wanted - set(uni.symbol)
        if unknown:
            log.warning("symbols not in universe, skipped: %s", sorted(unknown))
        uni = uni[uni.symbol.isin(wanted)]
    for col in ("list_date", "delist_date"):
        uni[col] = [d.date() if pd.notna(d) else None for d in pd.to_datetime(uni[col])]
    return uni.sort_values("symbol").reset_index(drop=True)


def _clip(start: date, end: date, list_date: date | None, delist_date: date | None
          ) -> tuple[date, date]:
    s = max(start, list_date) if list_date else start
    e = min(end, delist_date) if delist_date else end
    return s, e


# -------------------------------------------------------------------- jobs
def ingest_calendar(settings: Settings, ctx: RunContext, start: date, end: date) -> JobResult:
    res = JobResult("calendar")
    with Warehouse(settings.db_path) as wh, Baostock() as bsc:
        for s, e in missing_range((start, end), wh.coverage("calendar").get("ALL")):
            raw = bsc.trade_dates(s.isoformat(), e.isoformat())
            write_raw(settings.raw_dir, SOURCE, "trade_dates", ctx, f"{s}_{e}", raw)
            df = pd.DataFrame({
                "event_date": _to_date(raw["calendar_date"]),
                "is_trading_day": raw["is_trading_day"] == "1",
                "publish_date": None,
            })
            n = wh.append(CALENDAR, df, ctx)
            wh.log_ingest(ctx, "calendar", "ALL", s, e, len(df), n)
            res.rows_fetched += len(df)
            res.rows_inserted += n
    return res


def ingest_universe(settings: Settings, ctx: RunContext) -> JobResult:
    """Snapshot of all A-shares (type 1) incl. delisted. Always re-pulled; dedupe keeps it cheap."""
    res = JobResult("universe")
    with Warehouse(settings.db_path) as wh, Baostock() as bsc:
        raw = bsc.stock_basic()
        write_raw(settings.raw_dir, SOURCE, "stock_basic", ctx, "all", raw)
        raw = raw[raw["type"] == "1"]
        symbols = raw["code"].map(normalize)
        list_date = _to_date(raw["ipoDate"])
        df = pd.DataFrame({
            "symbol": symbols,
            "name": raw["code_name"],
            "exchange": symbols.str[-2:],
            "board": symbols.map(board),
            "list_date": list_date,
            "delist_date": _to_date(raw["outDate"]),
            "is_listed": raw["status"] == "1",
            "event_date": list_date,
            "publish_date": None,
        })
        n = wh.append(SECURITIES, df, ctx)
        wh.log_ingest(ctx, "universe", "ALL", None, None, len(df), n)
        res.rows_fetched, res.rows_inserted = len(df), n
        if not (df["exchange"] == "BJ").any():
            res.notes.append("baostock returns no Beijing Stock Exchange (BSE) listings")
    return res


def _bars_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    event_date = _to_date(raw["date"])
    return pd.DataFrame({
        "symbol": symbol,
        "event_date": event_date,
        "open": _num(raw["open"]), "high": _num(raw["high"]), "low": _num(raw["low"]),
        "close": _num(raw["close"]), "preclose": _num(raw["preclose"]),
        "volume": _num(raw["volume"]).astype("Int64"),
        "amount": _num(raw["amount"]),
        "turnover_pct": _num(raw["turn"]),
        "is_trading": raw["tradestatus"] == "1",
        "pct_chg": _num(raw["pctChg"]),
        "is_st": raw["isST"] == "1",
        "publish_date": event_date,
    })


def ingest_daily_bars(settings: Settings, ctx: RunContext, start: date, end: date,
                      symbols: list[str] | None = None, workers: int = 1, refetch: bool = False,
                      flush_every: int = 100) -> JobResult:
    """Unadjusted daily bars for each symbol's missing date range (per-symbol failure isolation)."""
    return _per_symbol_job(settings, ctx, "daily_bars", DAILY_BARS, _bars_frame, start, end,
                           symbols, workers, refetch, flush_every)


def _factors_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    event_date = _to_date(raw["dividOperateDate"])
    return pd.DataFrame({
        "symbol": symbol,
        "event_date": event_date,
        "back_adj_factor": _num(raw["backAdjustFactor"]),
        "publish_date": None,
    })


def ingest_adjust_factors(settings: Settings, ctx: RunContext, end: date,
                          symbols: list[str] | None = None, workers: int = 1,
                          refetch: bool = False, flush_every: int = 200) -> JobResult:
    """Adjustment factors from listing onward (earlier events are needed to adjust any window)."""
    return _per_symbol_job(settings, ctx, "adjust_factors", ADJ_FACTORS, _factors_frame, EARLIEST,
                           end, symbols, workers, refetch, flush_every)


# One baostock session per worker process (the client keeps a module-global socket).
_worker_session: Baostock | None = None


def _worker_init() -> None:
    global _worker_session
    logging.basicConfig(level=logging.WARNING)
    _worker_session = Baostock()
    _worker_session.login()


def _worker_fetch(dataset: str, sym: str, s: date, e: date) -> pd.DataFrame:
    return getattr(_worker_session, dataset)(to_baostock(sym), s.isoformat(), e.isoformat())


def _per_symbol_job(settings: Settings, ctx: RunContext, dataset: str, spec: TableSpec,
                    transform, start: date, end: date, symbols: list[str] | None, workers: int,
                    refetch: bool, flush_every: int) -> JobResult:
    res = JobResult(dataset)
    with Warehouse(settings.db_path) as wh:
        uni = _load_universe(wh, symbols)
        cov = {} if refetch else wh.coverage(dataset)
        todo = []
        for row in uni.itertuples():
            s, e = _clip(start, end, row.list_date, row.delist_date)
            for rng in missing_range((s, e), cov.get(row.symbol)):
                todo.append((row.symbol, *rng))
        log.info("%s: %d symbol-ranges to fetch with %d worker(s)", dataset, len(todo), workers)
        if not todo:
            return res

        buf_raw, buf, done = [], [], []
        part = 0

        def flush() -> None:
            nonlocal part
            if buf_raw:
                write_raw(settings.raw_dir, SOURCE, dataset, ctx, f"part{part:05d}",
                          pd.concat(buf_raw, ignore_index=True))
            if buf:
                res.rows_inserted += wh.append(spec, pd.concat(buf, ignore_index=True), ctx)
            for sym, s, e, rows in done:  # coverage is logged only after its data is committed
                wh.log_ingest(ctx, dataset, sym, s, e, rows, None)
            buf_raw.clear(); buf.clear(); done.clear()
            part += 1

        def handle(i: int, sym: str, s: date, e: date, raw: pd.DataFrame | None,
                   exc: BaseException | None) -> None:
            if exc is not None:  # isolate per symbol: record and move on
                log.warning("%s %s failed: %s", dataset, sym, exc)
                res.fail(sym, exc)
                wh.log_ingest(ctx, dataset, sym, s, e, 0, 0, "error", str(exc)[:500])
                return
            if len(raw):
                buf_raw.append(raw)
                buf.append(transform(raw, sym))
            res.rows_fetched += len(raw)
            done.append((sym, s, e, len(raw)))
            if len(done) >= flush_every:
                flush()
                log.info("%s: %d/%d", dataset, i, len(todo))

        if workers <= 1:
            with Baostock() as bsc:
                for i, (sym, s, e) in enumerate(todo, 1):
                    raw, err = None, None
                    try:
                        raw = getattr(bsc, dataset)(to_baostock(sym), s.isoformat(), e.isoformat())
                    except Exception as exc:  # noqa: BLE001
                        err = exc
                    handle(i, sym, s, e, raw, err)
        else:
            with ProcessPoolExecutor(workers, initializer=_worker_init) as pool:
                futures = {pool.submit(_worker_fetch, dataset, *t): t for t in todo}
                for i, fut in enumerate(as_completed(futures), 1):
                    sym, s, e = futures[fut]
                    raw, err = None, None
                    try:
                        raw = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        err = exc
                    handle(i, sym, s, e, raw, err)
        flush()
    return res


def _weekly_query_dates(wh: Warehouse, start: date, end: date) -> list[date]:
    """Last trading day of each ISO week in [start, end]."""
    cal = wh.df("""
        SELECT max(event_date) AS d FROM trading_calendar_latest
        WHERE is_trading_day AND event_date BETWEEN ? AND ?
        GROUP BY yearweek(event_date) ORDER BY d
    """, [start, end])
    return list(cal["d"].dt.date) if len(cal) else []


def ingest_index_constituents(settings: Settings, ctx: RunContext, start: date, end: date
                              ) -> JobResult:
    """CSI 300 / CSI 500 membership, sampled weekly (baostock snapshot cadence)."""
    res = JobResult("index_constituents")
    with Warehouse(settings.db_path) as wh, Baostock() as bsc:
        if not wh.table_exists("trading_calendar"):
            raise RuntimeError("trading_calendar is empty; run `alpha ingest calendar` first")
        cov = wh.coverage("index_constituents")
        for index_code, name in INDEXES.items():
            for s, e in missing_range((start, end), cov.get(index_code)):
                frames = []
                try:
                    for d in _weekly_query_dates(wh, s, e):
                        raw = bsc.index_constituents(name, d.isoformat())
                        if len(raw):
                            frames.append(raw)
                except Exception as exc:  # noqa: BLE001
                    res.fail(index_code, exc)
                    wh.log_ingest(ctx, "index_constituents", index_code, s, e, 0, 0, "error",
                                  str(exc)[:500])
                    continue
                raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
                    columns=["updateDate", "code", "code_name"])
                write_raw(settings.raw_dir, SOURCE, f"index_{name}", ctx, f"{s}_{e}", raw)
                df = pd.DataFrame({
                    "index_code": index_code,
                    "event_date": _to_date(raw["updateDate"]),
                    "symbol": raw["code"].map(normalize),
                    "name": raw["code_name"],
                    "publish_date": None,
                })
                n = wh.append(INDEX_CONSTITUENTS, df, ctx)
                wh.log_ingest(ctx, "index_constituents", index_code, s, e, len(df), n)
                res.rows_fetched += len(df)
                res.rows_inserted += n
    return res
