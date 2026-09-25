"""Derived views, computed from stored raw data (never materialised copies)."""

from __future__ import annotations

from .storage import Warehouse

VIEWS = {
    # Back-adjusted (后复权) prices are point-in-time safe: raw * cumulative factor as of the
    # bar date. Forward-adjusted (前复权) prices divide by the *latest* factor, so they change
    # whenever a new corporate action happens; use them for charts, not for backtests.
    "daily_bars_adj": ["daily_bars", "adjust_factors"],
    "st_periods": ["daily_bars"],
    "suspensions": ["daily_bars"],
    "index_membership": ["index_constituents"],
}

_SQL = {
    "daily_bars_adj": """
        CREATE OR REPLACE VIEW daily_bars_adj AS
        WITH f AS (SELECT symbol, event_date, back_adj_factor FROM adjust_factors_latest),
        joined AS (
            SELECT b.*, coalesce(f.back_adj_factor, 1.0) AS back_adj_factor
            FROM daily_bars_latest b
            ASOF LEFT JOIN f ON b.symbol = f.symbol AND b.event_date >= f.event_date
        ),
        latest_f AS (
            SELECT symbol, arg_max(back_adj_factor, event_date) AS latest_factor
            FROM f GROUP BY symbol
        )
        SELECT j.symbol, j.event_date, j.is_trading, j.is_st, j.volume, j.amount,
               j.open, j.high, j.low, j.close, j.preclose, j.back_adj_factor,
               j.open * j.back_adj_factor AS open_hfq,
               j.high * j.back_adj_factor AS high_hfq,
               j.low * j.back_adj_factor AS low_hfq,
               j.close * j.back_adj_factor AS close_hfq,
               j.close * j.back_adj_factor / coalesce(l.latest_factor, 1.0) AS close_qfq,
               j.close / nullif(j.preclose, 0) - 1 AS ret
        FROM joined j LEFT JOIN latest_f l USING (symbol)
    """,
    "st_periods": """
        CREATE OR REPLACE VIEW st_periods AS
        WITH b AS (
            SELECT symbol, event_date, is_st,
                   row_number() OVER (PARTITION BY symbol ORDER BY event_date)
                 - row_number() OVER (PARTITION BY symbol, is_st ORDER BY event_date) AS grp
            FROM daily_bars_latest
        )
        SELECT symbol, min(event_date) AS start_date, max(event_date) AS end_date,
               count(*) AS n_days
        FROM b WHERE is_st GROUP BY symbol, grp
    """,
    "suspensions": """
        CREATE OR REPLACE VIEW suspensions AS
        SELECT symbol, event_date, publish_date, ingested_at
        FROM daily_bars_latest WHERE NOT is_trading
    """,
    "index_membership": """
        CREATE OR REPLACE VIEW index_membership AS
        WITH snaps AS (
            SELECT index_code, event_date,
                   row_number() OVER (PARTITION BY index_code ORDER BY event_date) AS snap_no,
                   lead(event_date) OVER (PARTITION BY index_code ORDER BY event_date) AS next_date
            FROM (SELECT DISTINCT index_code, event_date FROM index_constituents_latest)
        ),
        m AS (
            SELECT c.index_code, c.symbol, c.event_date, s.snap_no, s.next_date,
                   s.snap_no - row_number() OVER (
                       PARTITION BY c.index_code, c.symbol ORDER BY c.event_date) AS grp
            FROM index_constituents_latest c JOIN snaps s USING (index_code, event_date)
        )
        SELECT index_code, symbol, min(event_date) AS first_seen,
               arg_max(next_date, event_date) AS first_absent
        FROM m GROUP BY index_code, symbol, grp
    """,
}


def refresh_views(wh: Warehouse) -> list[str]:
    created = []
    for name, deps in VIEWS.items():
        if all(wh.table_exists(d) for d in deps):
            wh.con.execute(_SQL[name])
            created.append(name)
    return created
