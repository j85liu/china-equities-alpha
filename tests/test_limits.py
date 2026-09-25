from datetime import date

import pandas as pd
import pytest

from china_equities_alpha.limits import flag_limit_breaches, price_limit

D = date(2024, 6, 3)
OLD = date(2010, 1, 4)


@pytest.mark.parametrize("board,is_st,day,list_date,n,expected", [
    ("main", False, D, OLD, 100, 0.10),
    ("main", True, D, OLD, 100, 0.05),
    ("main", True, date(2025, 7, 7), OLD, 100, 0.10),  # main-board ST widened to 10%
    ("main", False, D, OLD, 1, None),                  # first day of an old listing
    ("main", False, D, date(2023, 4, 10), 5, None),    # registration-era: 5 days unlimited
    ("main", False, D, date(2023, 4, 10), 6, 0.10),
    ("main", False, D, date(2020, 1, 2), 2, 0.10),     # pre-reform listing: day 2 limited
    ("chinext", False, D, OLD, 100, 0.20),
    ("chinext", True, D, OLD, 100, 0.20),              # ST on ChiNext is 20% post-reform
    ("chinext", False, date(2019, 6, 3), OLD, 100, 0.10),
    ("chinext", True, date(2019, 6, 3), OLD, 100, 0.05),
    ("chinext", False, D, date(2023, 7, 5), 5, None),
    ("chinext", False, D, date(2023, 7, 5), 6, 0.20),
    ("star", False, D, OLD, 100, 0.20),
    ("star", True, D, OLD, 100, 0.20),
    ("star", False, D, date(2023, 5, 5), 3, None),
    ("bse", False, D, OLD, 1, None),
    ("bse", False, D, OLD, 2, 0.30),
])
def test_price_limit(board, is_st, day, list_date, n, expected):
    assert price_limit(board, is_st, day, list_date, n) == expected


def bars(rows):
    cols = ["symbol", "board", "is_st", "preclose", "close", "listing_day"]
    return pd.DataFrame(rows, columns=cols).assign(event_date=D, list_date=OLD)


def test_tick_rounding_is_not_a_breach():
    # Real *ST day (600290.SH 2023-12-15): 0.52 -> 0.49 is -5.77% but exactly limit-down.
    # Main board 0.96 -> 1.06 is +10.42% but exactly limit-up (0.96 * 1.1 = 1.056 -> 1.06).
    df = bars([("600290.SH", "main", True, 0.52, 0.49, 100),
               ("000656.SZ", "main", False, 0.96, 1.06, 100)])
    breaches, hits = flag_limit_breaches(df, return_hits=True)
    assert breaches.empty
    assert hits == {"up": 1, "down": 1}


def test_half_up_rounding_of_limit_price():
    # 1.05 * 1.1 = 1.155 -> 1.16 (half-up), so 1.16 is allowed and 1.17 is not.
    ok = bars([("A", "main", False, 1.05, 1.16, 100)])
    bad = bars([("A", "main", False, 1.05, 1.17, 100)])
    assert flag_limit_breaches(ok).empty
    assert len(flag_limit_breaches(bad)) == 1


def test_breaches_by_board():
    df = bars([
        ("main_ok", "main", False, 10.0, 11.0, 100),
        ("main_bad", "main", False, 10.0, 11.5, 100),
        ("st_bad", "main", True, 10.0, 10.6, 100),
        ("chinext_ok", "chinext", False, 10.0, 12.0, 100),
        ("chinext_bad", "chinext", False, 10.0, 7.9, 100),
        ("star_ok", "star", False, 10.0, 8.0, 100),
        ("bse_ok", "bse", False, 10.0, 13.0, 100),
        ("bse_bad", "bse", False, 10.0, 13.1, 100),
        ("ipo_day", "star", False, 10.0, 30.0, 1),
    ])
    assert set(flag_limit_breaches(df)["symbol"]) == {"main_bad", "st_bad", "chinext_bad", "bse_bad"}


def test_rows_without_preclose_are_skipped():
    df = bars([("A", "main", False, None, 11.5, 100), ("B", "main", False, 0.0, 11.5, 100)])
    assert flag_limit_breaches(df).empty
