"""Post-run data-quality report: counts, coverage, nulls, duplicates, sanity and price-limit checks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .limits import flag_limit_breaches
from .storage import TableSpec, Warehouse, quote_ident


@dataclass
class TableReport:
    table: str
    rows: int = 0
    versions: int = 0  # stored rows incl. superseded versions
    min_event_date: str | None = None
    max_event_date: str | None = None
    duplicate_keys: int = 0
    exact_duplicate_rows: int = 0
    nulls: dict[str, int] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)


def _table_report(wh: Warehouse, spec: TableSpec) -> TableReport:
    r = TableReport(spec.name)
    if not wh.table_exists(spec.name):
        r.checks["status"] = "table missing"
        return r
    t, latest = quote_ident(spec.name), quote_ident(spec.name + "_latest")
    keys = ", ".join(quote_ident(k) for k in spec.keys)
    r.rows, r.min_event_date, r.max_event_date = wh.con.execute(
        f"SELECT count(*), CAST(min(event_date) AS VARCHAR), CAST(max(event_date) AS VARCHAR) FROM {latest}"
    ).fetchone()
    r.versions = wh.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    r.duplicate_keys = wh.con.execute(
        f"SELECT count(*) FROM (SELECT {keys} FROM {latest} GROUP BY {keys} HAVING count(*) > 1)"
    ).fetchone()[0]
    r.exact_duplicate_rows = wh.con.execute(
        f"SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM {t} GROUP BY {keys}, _row_hash)"
    ).fetchone()[0]
    null_sql = ", ".join(f"count(*) - count({quote_ident(c)})" for c in spec.columns)
    counts = wh.con.execute(f"SELECT {null_sql} FROM {latest}").fetchone()
    r.nulls = {c: int(n) for c, n in zip(spec.columns, counts) if n}
    return r


def _bars_checks(wh: Warehouse, r: TableReport, max_examples: int) -> None:
    con = wh.con
    # Coverage: bars present vs trading days expected over each symbol's ingested range.
    gaps = wh.df("""
        WITH cov AS (
            SELECT key AS symbol, min(start_date) s, max(end_date) e FROM _ingest_log
            WHERE dataset = 'daily_bars' AND status = 'ok' GROUP BY key
        ), expected AS (
            SELECT cov.symbol, count(*) n_expected FROM cov
            JOIN trading_calendar_latest c ON c.is_trading_day AND c.event_date BETWEEN cov.s AND cov.e
            GROUP BY cov.symbol
        ), actual AS (SELECT symbol, count(*) n_actual FROM daily_bars_latest GROUP BY symbol)
        SELECT e.symbol, n_expected, coalesce(n_actual, 0) n_actual,
               n_expected - coalesce(n_actual, 0) AS missing
        FROM expected e LEFT JOIN actual a USING (symbol)
        WHERE n_expected <> coalesce(n_actual, 0) ORDER BY missing DESC
    """)
    r.checks["symbols"] = con.execute("SELECT count(DISTINCT symbol) FROM daily_bars_latest").fetchone()[0]
    r.checks["symbols_with_missing_days"] = len(gaps)
    r.checks["missing_days_total"] = int(gaps["missing"].clip(lower=0).sum()) if len(gaps) else 0
    r.checks["missing_days_examples"] = gaps.head(max_examples).to_dict("records")
    r.checks["bars_on_non_trading_days"] = con.execute("""
        SELECT count(*) FROM daily_bars_latest b JOIN trading_calendar_latest c USING (event_date)
        WHERE NOT c.is_trading_day
    """).fetchone()[0]
    r.checks["suspended_days"], r.checks["st_days"] = con.execute(
        "SELECT count(*) FILTER (NOT is_trading), count(*) FILTER (is_st) FROM daily_bars_latest"
    ).fetchone()
    r.checks["ohlc_inconsistent"] = con.execute("""
        SELECT count(*) FROM daily_bars_latest WHERE is_trading AND (
            high < greatest(open, close, low) OR low > least(open, close, high)
            OR least(open, high, low, close) <= 0)
    """).fetchone()[0]
    r.checks["trading_with_zero_volume"] = con.execute(
        "SELECT count(*) FROM daily_bars_latest WHERE is_trading AND coalesce(volume, 0) = 0"
    ).fetchone()[0]
    r.checks["pct_chg_mismatch"] = con.execute("""
        SELECT count(*) FROM daily_bars_latest
        WHERE is_trading AND preclose > 0 AND abs(pct_chg - 100 * (close / preclose - 1)) > 0.01
    """).fetchone()[0]

    # Price limits: listing_day = trading days since first trading day on/after list_date.
    bars = wh.df("""
        WITH cal AS (
            SELECT event_date, row_number() OVER (ORDER BY event_date) AS n
            FROM trading_calendar_latest WHERE is_trading_day
        ), first_day AS (
            SELECT s.symbol, min(c.n) AS n0
            FROM securities_latest s JOIN cal c ON c.event_date >= s.list_date GROUP BY s.symbol
        )
        SELECT b.symbol, s.name, s.board, s.list_date, b.event_date, b.is_st, b.preclose, b.close,
               c.n - f.n0 + 1 AS listing_day
        FROM daily_bars_latest b
        JOIN securities_latest s ON s.symbol = b.symbol
        JOIN cal c ON c.event_date = b.event_date
        JOIN first_day f ON f.symbol = b.symbol
        WHERE b.is_trading
    """)
    if bars.empty:
        return
    for col in ("event_date", "list_date"):
        bars[col] = pd.to_datetime(bars[col]).dt.date
    breaches, hits = flag_limit_breaches(bars, return_hits=True)
    r.checks["limit_up_closes"] = int(hits["up"])
    r.checks["limit_down_closes"] = int(hits["down"])
    r.checks["price_limit_checked_rows"] = len(bars)
    r.checks["price_limit_breaches"] = len(breaches)
    ex = breaches.assign(ret=breaches["ret"].round(4))[
        ["symbol", "name", "board", "event_date", "is_st", "listing_day", "preclose", "close",
         "limit", "ret"]].head(max_examples)
    r.checks["price_limit_breach_examples"] = ex.to_dict("records")


def build_report(wh: Warehouse, specs: list[TableSpec], max_examples: int = 15) -> dict[str, Any]:
    reports = {}
    for spec in specs:
        r = _table_report(wh, spec)
        if spec.name == "daily_bars" and r.rows:
            _bars_checks(wh, r, max_examples)
        elif spec.name == "securities" and r.rows:
            r.checks["by_board"] = dict(wh.con.execute(
                "SELECT board, count(*) FROM securities_latest GROUP BY 1 ORDER BY 1").fetchall())
            r.checks["delisted"] = wh.con.execute(
                "SELECT count(*) FROM securities_latest WHERE NOT is_listed").fetchone()[0]
        elif spec.name == "trading_calendar" and r.rows:
            r.checks["trading_days_by_year"] = dict(wh.con.execute("""
                SELECT year(event_date), count(*) FILTER (is_trading_day)
                FROM trading_calendar_latest GROUP BY 1 ORDER BY 1""").fetchall())
        elif spec.name == "index_constituents" and r.rows:
            r.checks["snapshots"] = dict(wh.con.execute("""
                SELECT index_code, count(DISTINCT event_date) FROM index_constituents_latest
                GROUP BY 1 ORDER BY 1""").fetchall())
            r.checks["snapshots_with_wrong_size"] = wh.con.execute("""
                SELECT count(*) FROM (
                    SELECT index_code, event_date, count(*) n FROM index_constituents_latest
                    GROUP BY 1, 2)
                WHERE n <> CASE index_code WHEN '000300.SH' THEN 300 WHEN '000905.SH' THEN 500 END
            """).fetchone()[0]
        elif spec.name == "adjust_factors" and r.rows:
            r.checks["symbols"] = wh.con.execute(
                "SELECT count(DISTINCT symbol) FROM adjust_factors_latest").fetchone()[0]
            r.checks["non_positive_factors"] = wh.con.execute(
                "SELECT count(*) FROM adjust_factors_latest WHERE back_adj_factor <= 0").fetchone()[0]
        reports[spec.name] = asdict(r)
    return reports


def _jsonable(o: Any) -> Any:
    if isinstance(o, (date, datetime, pd.Timestamp)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def save_report(report: dict[str, Any], out_dir: Path, run_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"quality_{datetime.now():%Y%m%d_%H%M%S}_{run_id}.json"
    path.write_text(json.dumps(report, default=_jsonable, ensure_ascii=False, indent=2))
    return path


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["| table | rows | versions | event_date range | dup keys | exact dups | nulls |",
             "|---|---:|---:|---|---:|---:|---|"]
    for name, r in report.items():
        if name.startswith("_"):
            continue
        nulls = ", ".join(f"{k}={v}" for k, v in r["nulls"].items()) or "-"
        rng = f"{r['min_event_date']} → {r['max_event_date']}" if r["rows"] else "-"
        lines.append(f"| {name} | {r['rows']:,} | {r['versions']:,} | {rng} | "
                     f"{r['duplicate_keys']} | {r['exact_duplicate_rows']} | {nulls} |")
    for name, r in report.items():
        if name.startswith("_") or not r["checks"]:
            continue
        lines += ["", f"**{name}**", ""]
        for k, v in r["checks"].items():
            if isinstance(v, list):
                lines.append(f"- {k}: {len(v)} shown")
                if v:
                    df = pd.DataFrame(v)
                    lines.append("")
                    lines.append(df.to_markdown(index=False) if _has_tabulate() else df.to_string(index=False))
                    lines.append("")
            else:
                lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _has_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False
