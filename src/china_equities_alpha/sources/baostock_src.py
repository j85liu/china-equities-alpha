"""Thin, resilient wrapper over baostock.

baostock is a single-session, non-thread-safe TCP client that prints to
stdout; this wrapper silences it, rate-limits calls, and re-logs in before
each retry (dropped sessions are its most common failure).
"""

from __future__ import annotations

import contextlib
import io
import logging
import socket

import baostock as bs
import pandas as pd

from ..resilience import RateLimiter, with_retries

log = logging.getLogger(__name__)

KLINE_FIELDS = ("date,code,open,high,low,close,preclose,volume,amount,"
                "adjustflag,turn,tradestatus,pctChg,isST")


class BaostockError(RuntimeError):
    pass


class Baostock:
    def __init__(self, min_interval: float = 0.05, attempts: int = 4, timeout: float = 30.0):
        self._timeout = timeout
        self._limiter = RateLimiter(min_interval)
        self._attempts = attempts
        self._logged_in = False

    def __enter__(self) -> "Baostock":
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        self.logout()

    def login(self) -> None:
        # baostock opens plain sockets with no timeout; a stalled server hangs forever otherwise.
        socket.setdefaulttimeout(self._timeout)
        with contextlib.redirect_stdout(io.StringIO()):
            r = bs.login()
        if r.error_code != "0":
            raise BaostockError(f"login failed: {r.error_code} {r.error_msg}")
        self._logged_in = True

    def logout(self) -> None:
        if self._logged_in:
            with contextlib.redirect_stdout(io.StringIO()):
                bs.logout()
            self._logged_in = False

    def _relogin(self) -> None:
        try:
            self.logout()
        except Exception:  # noqa: BLE001
            pass
        self.login()

    def _query(self, fn_name: str, **kwargs) -> pd.DataFrame:
        def once() -> pd.DataFrame:
            self._limiter.wait()
            with contextlib.redirect_stdout(io.StringIO()):
                rs = getattr(bs, fn_name)(**kwargs)
                if rs.error_code != "0":
                    raise BaostockError(f"{fn_name}{kwargs}: {rs.error_code} {rs.error_msg}")
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise BaostockError(f"{fn_name}{kwargs}: {rs.error_code} {rs.error_msg}")
            return pd.DataFrame(rows, columns=rs.fields)

        return with_retries(once, attempts=self._attempts, on_retry=self._relogin)()

    # --- endpoints -------------------------------------------------------
    def trade_dates(self, start: str, end: str) -> pd.DataFrame:
        return self._query("query_trade_dates", start_date=start, end_date=end)

    def stock_basic(self) -> pd.DataFrame:
        return self._query("query_stock_basic")

    def daily_bars(self, bs_code: str, start: str, end: str) -> pd.DataFrame:
        # adjustflag="3": unadjusted prices; adjustments are applied downstream.
        return self._query("query_history_k_data_plus", code=bs_code, fields=KLINE_FIELDS,
                           start_date=start, end_date=end, frequency="d", adjustflag="3")

    def adjust_factors(self, bs_code: str, start: str, end: str) -> pd.DataFrame:
        return self._query("query_adjust_factor", code=bs_code, start_date=start, end_date=end)

    def index_constituents(self, index: str, date: str) -> pd.DataFrame:
        fn = {"hs300": "query_hs300_stocks", "zz500": "query_zz500_stocks"}[index]
        return self._query(fn, date=date)
