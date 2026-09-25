"""Daily price-limit (涨跌幅限制) rules by board, ST status, date and listing age.

Limits are applied to the exchange's previous close (which is already
ex-rights adjusted) and the limit price is rounded half-up to 0.01 CNY, so a
limit-up day on a low-priced stock can show a return a bit above 10%. The
check therefore compares *prices*, not percentages.

Rule timeline encoded here:
- Main board: ±10%, ST ±5%. Listings on/after 2023-04-10 (full registration
  reform) have no limit for their first 5 trading days; older listings had a
  +44%/-36% first-day band, treated here as "no limit" on day 1.
  From 2025-07-07 main-board risk-warning (ST) stocks move to ±10%.
- ChiNext: ±10% / ST ±5% until 2020-08-23; ±20% (ST included) from
  2020-08-24, with no limit for the first 5 days of listings from that date.
- STAR: ±20% (ST included), no limit for the first 5 days.
- BSE: ±30%, no limit on the first day.
Not modelled (they show up as flags): first day after relisting, first day
of the delisting-consolidation period.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .symbols import B_SHARE, BSE, CHINEXT, MAIN, STAR

CHINEXT_REFORM = date(2020, 8, 24)
MAIN_REGISTRATION = date(2023, 4, 10)
MAIN_ST_TO_10PCT = date(2025, 7, 7)


def price_limit(board: str, is_st: bool, trade_date: date, list_date: date | None,
                listing_day: int) -> float | None:
    """Limit as a fraction (0.1 = ±10%), or None when no limit applies.

    ``listing_day`` is 1 on the first trading day after IPO.
    """
    if board == BSE:
        return None if listing_day <= 1 else 0.30
    if board == STAR:
        return None if listing_day <= 5 else 0.20
    if board == CHINEXT:
        if trade_date >= CHINEXT_REFORM:
            if list_date is not None and list_date >= CHINEXT_REFORM and listing_day <= 5:
                return None
            return None if listing_day <= 1 else 0.20
        if listing_day <= 1:
            return None
        return 0.05 if is_st else 0.10
    if board in (MAIN, B_SHARE):
        if list_date is not None and list_date >= MAIN_REGISTRATION and listing_day <= 5:
            return None
        if listing_day <= 1:
            return None
        if is_st and trade_date < MAIN_ST_TO_10PCT:
            return 0.05
        return 0.10
    raise ValueError(f"unknown board {board!r}")


def _round_price(x: np.ndarray) -> np.ndarray:
    # Half-up rounding to the 0.01 tick; epsilon absorbs float noise (1.155 -> 1.16).
    return np.floor(x * 100 + 0.5 + 1e-9) / 100


def flag_limit_breaches(bars: pd.DataFrame, return_hits: bool = False):
    """Return the rows of ``bars`` whose close is outside the daily limit band.

    Required columns: symbol, board, event_date, list_date, listing_day,
    is_st, preclose, close. Suspended rows (no trade) should be removed first.
    Adds columns ``limit``, ``limit_up``, ``limit_down``, ``ret``. With ``return_hits``,
    also returns counts of closes exactly at limit-up / limit-down (proof the check is live).
    """
    df = bars.dropna(subset=["preclose", "close"])
    df = df[df["preclose"] > 0]
    limits = [
        price_limit(b, bool(st), d, ld if pd.notna(ld) else None, int(n))
        for b, st, d, ld, n in zip(df["board"], df["is_st"], df["event_date"],
                                   df["list_date"], df["listing_day"])
    ]
    df = df.assign(limit=pd.array(limits, dtype="Float64").astype("float64"))
    df = df[df["limit"].notna()]
    up = _round_price(df["preclose"].to_numpy() * (1 + df["limit"].to_numpy()))
    down = _round_price(df["preclose"].to_numpy() * (1 - df["limit"].to_numpy()))
    df = df.assign(limit_up=up, limit_down=down, ret=df["close"] / df["preclose"] - 1)
    tol = 1e-6
    breaches = df[(df["close"] > df["limit_up"] + tol) | (df["close"] < df["limit_down"] - tol)]
    if not return_hits:
        return breaches
    hits = {"up": int(((df["close"] - df["limit_up"]).abs() <= tol).sum()),
            "down": int(((df["close"] - df["limit_down"]).abs() <= tol).sum())}
    return breaches, hits
