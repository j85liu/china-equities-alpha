"""Canonical symbol format: ``<6-digit code>.<EXCHANGE>``, e.g. ``600519.SH``.

Every source speaks its own dialect (``sh.600519`` for baostock, ``600519``
or ``SH600519`` for AKShare / Eastmoney, ``600519.XSHG`` for JoinQuant...).
All ingestion goes through :func:`normalize` so the warehouse only ever holds
the canonical form.
"""

from __future__ import annotations

import re

EXCHANGES = ("SH", "SZ", "BJ")
_EXCHANGE_ALIASES = {
    "SH": "SH", "SS": "SH", "SSE": "SH", "XSHG": "SH",
    "SZ": "SZ", "SZSE": "SZ", "XSHE": "SZ",
    "BJ": "BJ", "BSE": "BJ", "BJSE": "BJ",
}

MAIN = "main"
CHINEXT = "chinext"
STAR = "star"
BSE = "bse"
B_SHARE = "b_share"

_PREFIX_RE = re.compile(r"^(?P<ex>[A-Za-z]{2,4})[.\-_]?(?P<code>\d{6})$")
_SUFFIX_RE = re.compile(r"^(?P<code>\d{6})[.\-_](?P<ex>[A-Za-z]{2,4})$")
_BARE_RE = re.compile(r"^\d{1,6}$")


def infer_exchange(code: str) -> str:
    """Infer the exchange of a bare 6-digit *stock* code.

    Only valid for stocks: index codes collide across exchanges (``000300`` is
    both CSI 300 on SH and a stock on SZ), so pass indices with a suffix.
    """
    if code.startswith(("920", "4", "8")):
        return "BJ"
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    raise ValueError(f"cannot infer exchange for code {code!r}")


def normalize(symbol: str | int) -> str:
    """Convert any common symbol spelling to ``600519.SH`` form."""
    s = str(symbol).strip()
    if _BARE_RE.match(s):
        code = s.zfill(6)
        return f"{code}.{infer_exchange(code)}"
    m = _SUFFIX_RE.match(s) or _PREFIX_RE.match(s)
    if not m:
        raise ValueError(f"unrecognised symbol {symbol!r}")
    ex = _EXCHANGE_ALIASES.get(m["ex"].upper())
    if ex is None:
        raise ValueError(f"unknown exchange in symbol {symbol!r}")
    return f"{m['code']}.{ex}"


def to_baostock(symbol: str) -> str:
    code, ex = normalize(symbol).split(".")
    return f"{ex.lower()}.{code}"


def code_of(symbol: str) -> str:
    return normalize(symbol).split(".")[0]


def board(symbol: str) -> str:
    """Listing board of a canonical stock symbol."""
    code, ex = normalize(symbol).split(".")
    if ex == "BJ":
        return BSE
    if ex == "SH":
        if code.startswith(("688", "689")):
            return STAR
        if code.startswith("900"):
            return B_SHARE
        return MAIN
    if code.startswith(("300", "301", "302")):
        return CHINEXT
    if code.startswith("200"):
        return B_SHARE
    return MAIN  # 000/001/002/003 (002 = former SME board, merged into main in 2021)
