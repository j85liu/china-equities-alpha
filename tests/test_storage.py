from datetime import date, datetime

import pandas as pd
import pytest

from china_equities_alpha.config import RunContext
from china_equities_alpha.storage import TableSpec, Warehouse

SPEC = TableSpec(
    "bars", {"symbol": "VARCHAR", "event_date": "DATE", "close": "DOUBLE", "publish_date": "DATE"},
    keys=("symbol", "event_date"), description="", event_date_meaning="", publish_date_meaning="",
)


def ctx(day: int) -> RunContext:
    return RunContext(run_id=f"r{day}", ingested_at=datetime(2025, 1, day))


def frame(*rows):
    return pd.DataFrame(rows, columns=["symbol", "event_date", "close"]).assign(
        publish_date=lambda d: d["event_date"])


@pytest.fixture
def wh(tmp_path):
    with Warehouse(tmp_path / "t.duckdb") as w:
        yield w


def test_append_is_idempotent(wh):
    df = frame(("600519.SH", date(2024, 1, 2), 1685.01), ("600519.SH", date(2024, 1, 3), 1694.0))
    assert wh.append(SPEC, df, ctx(1)) == 2
    assert wh.append(SPEC, df, ctx(2)) == 0
    assert wh.append(SPEC, df.iloc[::-1], ctx(3)) == 0  # order doesn't matter
    assert len(wh.df("SELECT * FROM bars")) == 2


def test_revision_appends_new_version_and_keeps_history(wh):
    wh.append(SPEC, frame(("600519.SH", date(2024, 1, 2), 1685.01)), ctx(1))
    assert wh.append(SPEC, frame(("600519.SH", date(2024, 1, 2), 1690.00)), ctx(2)) == 1
    hist = wh.df("SELECT close, ingested_at FROM bars ORDER BY ingested_at")
    assert hist["close"].tolist() == [1685.01, 1690.00]
    latest = wh.df("SELECT close FROM bars_latest")
    assert latest["close"].tolist() == [1690.00]
    # as-of query: what did we know on day 1?
    asof = wh.df("""SELECT close FROM bars WHERE ingested_at <= '2025-01-01'
                    QUALIFY row_number() OVER (PARTITION BY symbol, event_date
                                               ORDER BY ingested_at DESC) = 1""")
    assert asof["close"].tolist() == [1685.01]


def test_revert_to_old_value_is_a_new_version(wh):
    for day, px in [(1, 1.0), (2, 2.0), (3, 1.0)]:
        wh.append(SPEC, frame(("000001.SZ", date(2024, 1, 2), px)), ctx(day))
    assert wh.df("SELECT close FROM bars_latest")["close"].tolist() == [1.0]
    assert len(wh.df("SELECT * FROM bars")) == 3


def test_duplicates_within_batch_keep_last(wh):
    df = frame(("000001.SZ", date(2024, 1, 2), 1.0), ("000001.SZ", date(2024, 1, 2), 2.0))
    assert wh.append(SPEC, df, ctx(1)) == 1
    assert wh.df("SELECT close FROM bars")["close"].tolist() == [2.0]


def test_null_values_are_hashed_consistently(wh):
    df = frame(("000001.SZ", date(2024, 1, 2), None))
    assert wh.append(SPEC, df, ctx(1)) == 1
    assert wh.append(SPEC, df, ctx(2)) == 0


def test_null_keys_are_dropped(wh):
    df = frame(("000001.SZ", None, 1.0), ("000001.SZ", date(2024, 1, 2), 1.0))
    assert wh.append(SPEC, df, ctx(1)) == 1


def test_point_in_time_columns_required(wh):
    with pytest.raises(ValueError, match="publish_date"):
        wh.append(SPEC, frame(("000001.SZ", date(2024, 1, 2), 1.0)).drop(columns="publish_date"), ctx(1))


def test_coverage_from_ingest_log(wh):
    wh.log_ingest(ctx(1), "daily_bars", "600519.SH", date(2023, 1, 1), date(2023, 12, 31), 10, 10)
    wh.log_ingest(ctx(2), "daily_bars", "600519.SH", date(2024, 1, 1), date(2024, 6, 30), 5, 5)
    wh.log_ingest(ctx(2), "daily_bars", "000001.SZ", date(2024, 1, 1), date(2024, 6, 30), 0, 0,
                  status="error", error="boom")
    assert wh.coverage("daily_bars") == {"600519.SH": (date(2023, 1, 1), date(2024, 6, 30))}
