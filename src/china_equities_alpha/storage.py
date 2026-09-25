"""Append-only, versioned storage.

Two layers:

* **Raw landing zone** — every pull is written untouched (as returned by the
  source, plus fetch metadata) to ``data/raw/<source>/<dataset>/<date>/<run>-<part>.parquet``.
* **Warehouse** — DuckDB tables that are *append-only*. Each row carries a
  ``_row_hash`` over its content columns (everything except ``ingested_at``).
  A new row is inserted only if the latest stored version for its key has a
  different hash, so:

  - re-running a job inserts nothing (idempotent);
  - a revised value at the source becomes a new version, old versions stay
    (history is never overwritten; query ``<table>`` filtered on
    ``ingested_at <= T`` for "what did we know at T");
  - ``<table>_latest`` is a view of the newest version per key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa

from .config import RunContext

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: dict[str, str]  # column -> DuckDB type; excludes ingested_at/_row_hash
    keys: tuple[str, ...]
    description: str
    event_date_meaning: str
    publish_date_meaning: str


def write_raw(raw_dir: Path, source: str, dataset: str, ctx: RunContext, part: str,
              df: pd.DataFrame) -> Path | None:
    if df.empty:
        return None
    out = raw_dir / source / dataset / ctx.ingested_at.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ctx.run_id}-{part}.parquet"
    df.assign(_fetched_at=ctx.ingested_at, _run_id=ctx.run_id).to_parquet(path, index=False)
    return path


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Warehouse:
    def __init__(self, db_path: Path, read_only: bool = False):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.con = duckdb.connect(str(db_path), read_only=read_only)
        if not read_only:
            self._init_meta()

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ meta
    def _init_meta(self) -> None:
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS _ingest_log (
                run_id VARCHAR, dataset VARCHAR, key VARCHAR,
                start_date DATE, end_date DATE, rows_fetched BIGINT, rows_inserted BIGINT,
                status VARCHAR, error VARCHAR, ingested_at TIMESTAMP)
        """)

    def log_ingest(self, ctx: RunContext, dataset: str, key: str, start: date | None,
                   end: date | None, fetched: int, inserted: int | None, status: str = "ok",
                   error: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO _ingest_log VALUES (?,?,?,?,?,?,?,?,?,?)",
            [ctx.run_id, dataset, key, start, end, fetched, inserted, status, error, ctx.ingested_at],
        )

    def coverage(self, dataset: str) -> dict[str, tuple[date, date]]:
        """Successfully-ingested [start, end] per key (ranges are kept contiguous by the jobs)."""
        rows = self.con.execute("""
            SELECT key, min(start_date), max(end_date) FROM _ingest_log
            WHERE dataset = ? AND status = 'ok' GROUP BY key
        """, [dataset]).fetchall()
        return {k: (s, e) for k, s, e in rows}

    # ---------------------------------------------------------------- tables
    def table_exists(self, name: str) -> bool:
        return bool(self.con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()[0])

    def append(self, spec: TableSpec, df: pd.DataFrame, ctx: RunContext) -> int:
        """Append rows whose content differs from the latest stored version. Returns rows inserted."""
        if df.empty:
            return 0
        for col in ("event_date", "publish_date"):
            if col not in df.columns:
                raise ValueError(f"{spec.name}: missing point-in-time column {col!r}")
        null_keys = df[list(spec.keys)].isna().any(axis=1)
        if null_keys.any():
            log.warning("%s: dropping %d rows with null keys", spec.name, int(null_keys.sum()))
            df = df[~null_keys]
        dupes = df.duplicated(list(spec.keys), keep="last")
        if dupes.any():
            log.warning("%s: %d duplicate keys within batch, keeping last", spec.name, int(dupes.sum()))
            df = df[~dupes]
        missing = [c for c in spec.columns if c not in df.columns]
        if missing:
            raise ValueError(f"{spec.name}: missing columns {missing}")
        content = list(spec.columns)
        df = df[content].assign(ingested_at=ctx.ingested_at).reset_index(drop=True)
        # Arrow hand-off: DuckDB mis-reads strided object columns (e.g. dates after a reversing slice).
        self.con.register("_incoming", pa.Table.from_pandas(df, preserve_index=False))
        try:
            self.ensure_table(spec)
            hash_expr = "md5(concat_ws('\x1f', " + ", ".join(
                f"coalesce(CAST({quote_ident(c)} AS VARCHAR), '<NULL>')" for c in content) + "))"
            cols = ", ".join(quote_ident(c) for c in content)
            casts = ", ".join(f"CAST({quote_ident(c)} AS {t}) AS {quote_ident(c)}" for c, t in spec.columns.items())
            keys = ", ".join(quote_ident(k) for k in spec.keys)
            before = self._count(spec.name)
            self.con.execute(f"""
                INSERT INTO {quote_ident(spec.name)} ({cols}, ingested_at, _row_hash)
                WITH typed AS (
                    SELECT {casts}, CAST(ingested_at AS TIMESTAMP) AS ingested_at FROM _incoming
                ), incoming AS (
                    SELECT {cols}, ingested_at, {hash_expr} AS _row_hash FROM typed
                ), latest AS (
                    SELECT {keys}, _row_hash FROM {quote_ident(spec.name)} SEMI JOIN typed USING ({keys})
                    QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY ingested_at DESC) = 1
                )
                SELECT i.* FROM incoming i ANTI JOIN latest l USING ({keys}, _row_hash)
            """)
            return self._count(spec.name) - before
        finally:
            self.con.unregister("_incoming")

    def ensure_table(self, spec: TableSpec) -> None:
        if self.table_exists(spec.name):
            return
        cols = ", ".join(f"{quote_ident(c)} {t}" for c, t in spec.columns.items())
        self.con.execute(f"CREATE TABLE {quote_ident(spec.name)} ({cols}, ingested_at TIMESTAMP, _row_hash VARCHAR)")
        self._create_latest_view(spec)

    def _count(self, name: str) -> int:
        return self.con.execute(f"SELECT count(*) FROM {quote_ident(name)}").fetchone()[0]

    def _create_latest_view(self, spec: TableSpec) -> None:
        keys = ", ".join(quote_ident(k) for k in spec.keys)
        self.con.execute(f"""
            CREATE OR REPLACE VIEW {quote_ident(spec.name + '_latest')} AS
            SELECT * FROM {quote_ident(spec.name)}
            QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY ingested_at DESC) = 1
        """)

    def df(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()
