from datetime import date, timedelta

import pandas as pd
import pytest

from china_equities_alpha.limits import (
    CHINEXT_REFORM,
    IPO_BAND_START,
    MAIN_REGISTRATION,
    MAIN_ST_TO_10PCT,
    STAR_LAUNCH,
    Limit,
    flag_limit_breaches,
    price_limit,
)

ONE = timedelta(days=1)
OLD = date(2010, 1, 4)          # an old listing
MID = date(2024, 6, 3)          # an ordinary recent trading date
TEN, FIVE, TWENTY = Limit(0.10, 0.10), Limit(0.05, 0.05), Limit(0.20, 0.20)
BAND = Limit(0.44, 0.36, band=True)


def lim(board, is_st=False, day=MID, list_date=OLD, n=100):
    return price_limit(board, is_st, day, list_date, n)


# --- main board: ST 5% -> 10% on 2026-07-06 --------------------------------------------
def test_main_st_boundary():
    assert MAIN_ST_TO_10PCT == date(2026, 7, 6)
    assert lim("main", True, MAIN_ST_TO_10PCT - ONE) == FIVE
    assert lim("main", True, MAIN_ST_TO_10PCT) == TEN
    assert lim("main", True, date(2025, 7, 7)) == FIVE  # the old, wrong date must not apply


def test_main_non_st_is_always_10pct():
    for d in (date(2015, 1, 5), MAIN_ST_TO_10PCT - ONE, MAIN_ST_TO_10PCT):
        assert lim("main", False, d) == TEN


# --- IPO first-day band (+44% / -36%) from 2014-06-13 -----------------------------------
@pytest.mark.parametrize("board", ["main", "chinext"])
def test_first_day_band_boundary(board):
    before = IPO_BAND_START - ONE
    assert lim(board, day=before, list_date=before, n=1) is None
    assert lim(board, day=IPO_BAND_START, list_date=IPO_BAND_START, n=1) == BAND
    assert lim(board, day=IPO_BAND_START + ONE, list_date=IPO_BAND_START, n=2) == TEN


# --- main-board registration: first 5 days unlimited for listings from 2023-04-10 -------
def test_main_registration_boundary():
    pre, post = MAIN_REGISTRATION - ONE, MAIN_REGISTRATION
    assert lim("main", day=pre, list_date=pre, n=1) == BAND
    assert lim("main", day=pre + 2 * ONE, list_date=pre, n=2) == TEN
    for n in range(1, 6):
        assert lim("main", day=post, list_date=post, n=n) is None
    assert lim("main", day=post + 7 * ONE, list_date=post, n=6) == TEN
    assert lim("main", True, day=post + 7 * ONE, list_date=post, n=6) == FIVE


# --- ChiNext: 10% -> 20% on 2020-08-24; IPO 5-day window for listings from then --------
def test_chinext_reform_boundary():
    pre, post = CHINEXT_REFORM - ONE, CHINEXT_REFORM
    assert CHINEXT_REFORM == date(2020, 8, 24)
    assert lim("chinext", day=pre) == TEN
    assert lim("chinext", True, day=pre) == FIVE
    assert lim("chinext", day=post) == TWENTY
    assert lim("chinext", True, day=post) == TWENTY  # ST included post-reform


def test_chinext_ipo_window_boundary():
    pre, post = CHINEXT_REFORM - ONE, CHINEXT_REFORM
    assert lim("chinext", day=pre, list_date=pre, n=1) == BAND
    for n in range(1, 6):
        assert lim("chinext", day=post, list_date=post, n=n) is None
    assert lim("chinext", day=post + 7 * ONE, list_date=post, n=6) == TWENTY
    # listed before the reform, still in its first week when the reform lands
    assert lim("chinext", day=post, list_date=pre - 3 * ONE, n=3) == TWENTY


# --- STAR: 20% from launch; IPO 5-day window ---------------------------------------------
def test_star_launch_rules():
    for n in range(1, 6):
        assert lim("star", day=STAR_LAUNCH, list_date=STAR_LAUNCH, n=n) is None
    assert lim("star", day=STAR_LAUNCH + 7 * ONE, list_date=STAR_LAUNCH, n=6) == TWENTY
    assert lim("star", True) == TWENTY


def test_bse():
    assert lim("bse", n=1) is None
    assert lim("bse", n=2) == Limit(0.30, 0.30)


def test_unknown_board():
    with pytest.raises(ValueError):
        lim("nasdaq")


# --- the breach check itself --------------------------------------------------------------
def bars(rows, day=MID, list_date=OLD):
    cols = ["symbol", "board", "is_st", "preclose", "close", "listing_day"]
    return pd.DataFrame(rows, columns=cols).assign(event_date=day, list_date=list_date)


def test_tick_rounding_is_not_a_breach():
    # Real *ST day (600290.SH 2023-12-15): 0.52 -> 0.49 is -5.77% but exactly limit-down.
    # Main board 0.96 -> 1.06 is +10.42% but exactly limit-up (0.96 * 1.1 = 1.056 -> 1.06).
    df = bars([("600290.SH", "main", True, 0.52, 0.49, 100),
               ("000656.SZ", "main", False, 0.96, 1.06, 100)], day=date(2023, 12, 15))
    breaches, hits = flag_limit_breaches(df, return_hits=True)
    assert breaches.empty
    assert hits == {"up": 1, "down": 1}


def test_half_up_rounding_of_limit_price():
    # 1.05 * 1.1 = 1.155 -> 1.16 (half-up), so 1.16 is allowed and 1.17 is not.
    assert flag_limit_breaches(bars([("A", "main", False, 1.05, 1.16, 100)])).empty
    assert len(flag_limit_breaches(bars([("A", "main", False, 1.05, 1.17, 100)]))) == 1


def test_st_breach_depends_on_date():
    row = [("A", "main", True, 10.0, 10.8, 100)]  # +8%
    assert len(flag_limit_breaches(bars(row, day=MAIN_ST_TO_10PCT - ONE))) == 1
    assert flag_limit_breaches(bars(row, day=MAIN_ST_TO_10PCT)).empty


def test_first_day_band_rounds_half_up():
    # Real day-1 closes from the backfill: 1.44 * 7.47 = 10.7568 -> 10.76 (002909.SZ 2017-10-26),
    # 1.44 * 13.36 = 19.2384 -> 19.24 (300531.SZ 2016-08-09). One tick above is a breach.
    day = date(2017, 10, 26)
    ok = bars([("A", "main", False, 7.47, 10.76, 1), ("B", "chinext", False, 13.36, 19.24, 1),
               ("C", "main", False, 6.58, 4.21, 1)],  # 0.64 * 6.58 = 4.2112 -> 4.21
              day=day, list_date=day)
    bad = bars([("A", "main", False, 7.47, 10.77, 1), ("C", "main", False, 6.58, 4.20, 1)],
               day=day, list_date=day)
    breaches, hits = flag_limit_breaches(ok, return_hits=True)
    assert breaches.empty and hits == {"up": 2, "down": 1}
    assert set(flag_limit_breaches(bad)["symbol"]) == {"A", "C"}


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


def test_all_unlimited_rows():
    df = bars([("A", "star", False, 10.0, 30.0, 1)])
    breaches, hits = flag_limit_breaches(df, return_hits=True)
    assert breaches.empty and hits == {"up": 0, "down": 0}


def test_classify_breaches():
    from china_equities_alpha.quality import classify_breaches

    df = pd.DataFrame({
        "event_date": [date(2024, 3, 1), date(2021, 8, 10), date(2015, 1, 26), date(2020, 1, 2)],
        "delist_date": [date(2024, 3, 26), None, None, None],
        "prev_trading": [True, False, None, True],
        "listing_day": [3000, 5000, 1, 900],
    })
    assert classify_breaches(df).tolist() == [
        "delisting_period_first_day?", "first_trade_after_suspension (relisting?)",
        "non_ipo_listing_day1?", "unexplained"]
