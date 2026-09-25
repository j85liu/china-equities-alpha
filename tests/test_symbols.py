import pytest

from china_equities_alpha.symbols import board, normalize, to_baostock


@pytest.mark.parametrize("raw", [
    "600519", 600519, "600519.SH", "600519.sh", "sh.600519", "SH600519", "sh600519",
    "600519.XSHG", "600519.SS", " 600519.SH ",
])
def test_normalize_sh_variants(raw):
    assert normalize(raw) == "600519.SH"


@pytest.mark.parametrize("raw,expected", [
    ("000001", "000001.SZ"), (1, "000001.SZ"), ("sz.000001", "000001.SZ"),
    ("000001.XSHE", "000001.SZ"), ("300750", "300750.SZ"), ("688981", "688981.SH"),
    ("430047", "430047.BJ"), ("830799", "830799.BJ"), ("920118", "920118.BJ"),
    ("bj430047", "430047.BJ"), ("430047.BJ", "430047.BJ"),
    ("000300.SH", "000300.SH"),  # an index keeps its explicit exchange
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "600519.XX", "1234567", "sh.60051"])
def test_normalize_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize(raw)


def test_normalize_is_idempotent():
    for s in ["600519.SH", "000001.SZ", "430047.BJ"]:
        assert normalize(normalize(s)) == s


def test_to_baostock():
    assert to_baostock("600519.SH") == "sh.600519"
    assert to_baostock("000001") == "sz.000001"


@pytest.mark.parametrize("sym,expected", [
    ("600519.SH", "main"), ("601061.SH", "main"), ("000001.SZ", "main"), ("002594.SZ", "main"),
    ("300750.SZ", "chinext"), ("301202.SZ", "chinext"), ("688981.SH", "star"),
    ("689009.SH", "star"), ("430047.BJ", "bse"), ("920118.BJ", "bse"),
    ("900901.SH", "b_share"), ("200002.SZ", "b_share"),
])
def test_board(sym, expected):
    assert board(sym) == expected
