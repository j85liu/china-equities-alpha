"""Daily price-limit (涨跌幅限制) rules by board, ST status, date and listing age.

Every rule is date-aware and cites its source. "VERIFIED" means the date/value
was read from an exchange (sse.com.cn / szse.cn) or government page; anything
else is marked UNVERIFIED with what we could and couldn't confirm.

Two kinds of band:

* **Daily limit** — ``limit price = preclose × (1 ± pct)``, rounded half-up to
  0.01 CNY (SZSE Trading Rules §3.3.14). ``preclose`` is the exchange's
  ex-rights reference price, so a limit-up day on a low-priced stock can show
  a return a little above the nominal percentage.
* **IPO first-day price band** (pre-registration listings) — valid order
  prices on day 1 must lie within [64%, 144%] of the issue price (baostock's
  ``preclose`` on the listing day is the issue price). The band edge is rounded
  half-up to the tick like a daily limit. Empirical check on the 2015–2026 backfill:
  of 679 day-1 closes above the floored 1.44 × P tick, 99.3% sit exactly on the
  half-up tick (e.g. issue 7.47 → close 10.76, 1.44 × 7.47 = 10.7568).

Not modelled (they show up as flags): first day of relisting (重新上市首日),
first day of the delisting-consolidation period (退市整理期首日) — both
exempt from limits (SZSE Trading Rules 2026 §3.3.15) — and B shares.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .symbols import B_SHARE, BSE, CHINEXT, MAIN, STAR

# --- Rule effective dates -------------------------------------------------

# IPO first-day band of 144% / 64% of the issue price during continuous trading.
# VERIFIED (SSE): 上证发〔2014〕37号《关于新股上市初期交易监管有关事项的通知》, issued and
#   effective 2014-06-13 ("本通知自发布之日起施行").
#   https://www.sse.com.cn/lawandrules/sselawsrules2025/repeal/rules/c/c_20140613_10785185.shtml
# UNVERIFIED (SZSE): 深证会〔2014〕54号, also dated 2014-06-13 and applying to all SZSE
#   IPOs (main/SME/ChiNext), per secondary reports (cnstock.com 2014-06); the notice text
#   itself was not retrieved from szse.cn. Before 2014-06-13 first days had intraday-halt
#   rules but no fixed band; modelled here as "no limit".
IPO_BAND_START = date(2014, 6, 13)
IPO_BAND_UP, IPO_BAND_DOWN = 0.44, 0.36

# STAR Market: ±20% for all STAR stocks (ST included); no limit for the first 5 trading
# days after IPO. First listings traded 2019-07-22.
# VERIFIED: 《上海证券交易所科创板股票交易特别规定》 (2019), SSE rules archive:
#   https://www.sse.com.cn/lawandrules/sselawsrules/repeal/rules/c/10118601/files/f6fc4a1d4c1f469183a013c4dc36a535.pdf
#   First-batch listing date 2019-07-22 per SSE/press coverage (cnr.cn 2019-07-29).
STAR_LAUNCH = date(2019, 7, 22)

# ChiNext reform: ±10% (ST ±5%) → ±20% for all ChiNext stocks (ST included) and no limit
# for the first 5 days of new listings, effective 2020-08-24.
# VERIFIED: 深证上〔2020〕515号《深圳证券交易所创业板交易特别规定》, in force 2020-08-24.
#   http://www.szse.cn/disclosure/notice/general/t20200612_578381.html
#   https://www.szse.cn/aboutus/trends/conference/t20200821_580925.html
# Exception (not modelled): three ChiNext stocks already in delisting consolidation on
# 2020-08-24 (300216, 300156, 300090) stayed at ±10% until delisting.
CHINEXT_REFORM = date(2020, 8, 24)

# Main-board registration reform: no limit for the first 5 trading days of IPOs.
# VERIFIED: SSE says the revised trading rules apply "自按照《首次公开发行股票注册管理办法》
#   发行的首只主板股票上市首日起施行" — that first listing day was 2023-04-10 (gov.cn 2023-04-11).
#   https://www.sse.com.cn/listing/announcement/notification/c/c_20230216_10764651.shtml
#   https://www.gov.cn/yaowen/2023-04/11/content_5750765.htm
# UNVERIFIED: ~16 main-board IPOs approved under the old approval regime (核准制) listed
#   during the transition and kept the old first-day regime (北京商报 2023-02-19). We can't
#   tell approval vs registration listings apart from baostock, so any such listing on/after
#   2023-04-10 is treated as "no limit for 5 days" (looser: may miss, never false-flag).
MAIN_REGISTRATION = date(2023, 4, 10)

# Main-board risk-warning (ST/*ST) stocks: ±5% → ±10%, effective 2026-07-06
# (rules published 2026-04-24).
# VERIFIED (SSE): 《上海证券交易所交易规则（2026年修订）》 上证发〔2026〕41号, "于2026年7月6日起正式实施",
#   "将主板风险警示股票价格涨跌幅限制比例由5%调整为10%".
#   https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml
# VERIFIED (SZSE): 《深圳证券交易所交易规则（2026年修订）》 §3.3.13 (main board 10%, no separate
#   ST band), §10.9 "本规则自2026年7月6日起施行".
#   https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf
MAIN_ST_TO_10PCT = date(2026, 7, 6)

# BSE: ±30%, no limit on the listing day. UNVERIFIED — BSE is out of scope for now
# (no BSE data in baostock); kept so the symbol/limit code is complete.

REGISTRATION_IPO_FREE_DAYS = 5


@dataclass(frozen=True)
class Limit:
    up: float    # fraction above the reference price (0.10 = +10%)
    down: float  # fraction below the reference price
    band: bool = False  # True: IPO first-day order-price band rather than a daily limit


def price_limit(board: str, is_st: bool, trade_date: date, list_date: date | None,
                listing_day: int) -> Limit | None:
    """The limit in force, or None when the day is unlimited.

    ``listing_day`` is 1 on the first trading day after IPO.
    """
    if board == BSE:
        return None if listing_day <= 1 else Limit(0.30, 0.30)

    if board == STAR:
        return None if listing_day <= REGISTRATION_IPO_FREE_DAYS else Limit(0.20, 0.20)

    if board == CHINEXT:
        if trade_date >= CHINEXT_REFORM:
            if list_date is not None and list_date >= CHINEXT_REFORM:
                return None if listing_day <= REGISTRATION_IPO_FREE_DAYS else Limit(0.20, 0.20)
            return Limit(0.20, 0.20)
        return _pre_registration(is_st, trade_date, listing_day)

    if board in (MAIN, B_SHARE):
        if list_date is not None and list_date >= MAIN_REGISTRATION:
            if listing_day <= REGISTRATION_IPO_FREE_DAYS:
                return None
        elif listing_day <= 1:
            return _first_day_band(trade_date)
        pct = 0.05 if is_st and trade_date < MAIN_ST_TO_10PCT else 0.10
        return Limit(pct, pct)

    raise ValueError(f"unknown board {board!r}")


def _first_day_band(trade_date: date) -> Limit | None:
    return Limit(IPO_BAND_UP, IPO_BAND_DOWN, band=True) if trade_date >= IPO_BAND_START else None


def _pre_registration(is_st: bool, trade_date: date, listing_day: int) -> Limit | None:
    if listing_day <= 1:
        return _first_day_band(trade_date)
    pct = 0.05 if is_st else 0.10
    return Limit(pct, pct)


def _round_half_up(x: np.ndarray) -> np.ndarray:
    # Half-up rounding to the 0.01 tick; epsilon absorbs float noise (1.155 -> 1.16).
    return np.floor(x * 100 + 0.5 + 1e-9) / 100


def limit_prices(preclose: np.ndarray, up: np.ndarray, down: np.ndarray
                 ) -> tuple[np.ndarray, np.ndarray]:
    return _round_half_up(preclose * (1 + up)), _round_half_up(preclose * (1 - down))


def flag_limit_breaches(bars: pd.DataFrame, return_hits: bool = False):
    """Return the rows of ``bars`` whose close is outside the price-limit band.

    Required columns: symbol, board, event_date, list_date, listing_day,
    is_st, preclose, close. Suspended rows (no trade) should be removed first.
    Adds columns ``limit_up_pct``, ``limit_down_pct``, ``limit_up``, ``limit_down``,
    ``ret``. With ``return_hits``, also returns counts of closes exactly at
    limit-up / limit-down (proof the check is live).
    """
    df = bars.dropna(subset=["preclose", "close"])
    df = df[df["preclose"] > 0]
    limits = [
        price_limit(b, bool(st), d, ld if pd.notna(ld) else None, int(n))
        for b, st, d, ld, n in zip(df["board"], df["is_st"], df["event_date"],
                                   df["list_date"], df["listing_day"])
    ]
    has = np.array([lim is not None for lim in limits], dtype=bool)
    df = df[has]
    limits = [lim for lim in limits if lim is not None]
    up = np.array([lim.up for lim in limits], dtype=float)
    down = np.array([lim.down for lim in limits], dtype=float)
    hi, lo = limit_prices(df["preclose"].to_numpy(dtype=float), up, down)
    df = df.assign(limit_up_pct=up, limit_down_pct=down, limit_up=hi, limit_down=lo,
                   ret=df["close"] / df["preclose"] - 1)
    tol = 1e-6
    breaches = df[(df["close"] > df["limit_up"] + tol) | (df["close"] < df["limit_down"] - tol)]
    if not return_hits:
        return breaches
    hits = {"up": int(((df["close"] - df["limit_up"]).abs() <= tol).sum()),
            "down": int(((df["close"] - df["limit_down"]).abs() <= tol).sum())}
    return breaches, hits
