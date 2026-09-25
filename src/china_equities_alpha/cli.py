from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Callable, Optional

import typer

from .config import TEST_UNIVERSE, RunContext, Settings
from .jobs import market
from .quality import build_report, render_markdown, save_report
from .resilience import JobResult, run_isolated
from .storage import Warehouse
from .views import refresh_views

app = typer.Typer(help="china-equities-alpha: A-share data ingestion", no_args_is_help=True)
ingest = typer.Typer(help="Ingest datasets (incremental and idempotent)", no_args_is_help=True)
app.add_typer(ingest, name="ingest")

Start = Annotated[str, typer.Option("--start", help="YYYY-MM-DD")]
End = Annotated[Optional[str], typer.Option("--end", help="YYYY-MM-DD (default: yesterday)")]
Symbols = Annotated[Optional[list[str]], typer.Option("--symbol", "-s", help="Repeatable; any format")]
Test = Annotated[bool, typer.Option("--test", help="Use the 10-stock test universe")]
Workers = Annotated[int, typer.Option("--workers", "-w", help="Parallel baostock sessions")]
Refetch = Annotated[bool, typer.Option("--refetch", help="Re-pull covered ranges (dedupe keeps it idempotent)")]


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _end(end: str | None) -> date:
    return date.fromisoformat(end) if end else market.default_end()


def _symbols(symbols: list[str] | None, test: bool) -> list[str] | None:
    return TEST_UNIVERSE if test else symbols


def _run(jobs: list[tuple[str, Callable[[], JobResult]]], settings: Settings,
         ctx: RunContext, report: bool = True) -> None:
    results = [run_isolated(name, fn) for name, fn in jobs]
    typer.echo(f"\nrun {ctx.run_id} @ {ctx.ingested_at}")
    for r in results:
        typer.echo(f"  {r.job:<20} {r.status:<8} fetched={r.rows_fetched:>9,} "
                   f"inserted={r.rows_inserted:>9,} failures={len(r.failures)} {r.seconds}s")
        for f in r.failures[:5]:
            typer.echo(f"      ! {f['key']}: {f['error'][:160]}")
        for n in r.notes:
            typer.echo(f"      note: {n}")
    if report:
        _report(settings, ctx)
    if any(r.status == "failed" for r in results):
        raise typer.Exit(1)


def _report(settings: Settings, ctx: RunContext) -> None:
    with Warehouse(settings.db_path) as wh:
        refresh_views(wh)
        rep = build_report(wh, market.TABLES)
    path = save_report(rep, settings.reports_dir, ctx.run_id)
    typer.echo("\n" + render_markdown(rep))
    typer.echo(f"\nreport saved to {path}")


@ingest.command("calendar")
def calendar(start: Start = "1990-12-19", end: End = None) -> None:
    s, c = Settings(), RunContext()
    _run([("calendar", lambda: market.ingest_calendar(s, c, date.fromisoformat(start), _end(end)))], s, c)


@ingest.command("universe")
def universe() -> None:
    s, c = Settings(), RunContext()
    _run([("universe", lambda: market.ingest_universe(s, c))], s, c)


@ingest.command("prices")
def prices(start: Start = "2015-01-01", end: End = None, symbol: Symbols = None,
           test: Test = False, workers: Workers = 1, refetch: Refetch = False) -> None:
    """Unadjusted daily bars + adjustment factors (ST flags and suspensions come with the bars)."""
    s, c = Settings(), RunContext()
    syms, e = _symbols(symbol, test), _end(end)
    _run([
        ("daily_bars", lambda: market.ingest_daily_bars(
            s, c, date.fromisoformat(start), e, syms, workers, refetch)),
        ("adjust_factors", lambda: market.ingest_adjust_factors(s, c, e, syms, workers, refetch)),
    ], s, c)


@ingest.command("index")
def index(start: Start = "2015-01-01", end: End = None) -> None:
    """CSI 300 / CSI 500 constituent history."""
    s, c = Settings(), RunContext()
    _run([("index_constituents", lambda: market.ingest_index_constituents(
        s, c, date.fromisoformat(start), _end(end)))], s, c)


@ingest.command("phase1")
def phase1(start: Start = "2015-01-01", end: End = None, symbol: Symbols = None,
           test: Test = False, workers: Workers = 1, refetch: Refetch = False) -> None:
    """Calendar, universe, bars, adjustment factors and index constituents in one run."""
    s, c = Settings(), RunContext()
    st, e, syms = date.fromisoformat(start), _end(end), _symbols(symbol, test)
    _run([
        ("calendar", lambda: market.ingest_calendar(s, c, market.EARLIEST, e)),
        ("universe", lambda: market.ingest_universe(s, c)),
        ("daily_bars", lambda: market.ingest_daily_bars(s, c, st, e, syms, workers, refetch)),
        ("adjust_factors", lambda: market.ingest_adjust_factors(s, c, e, syms, workers, refetch)),
        ("index_constituents", lambda: market.ingest_index_constituents(s, c, st, e)),
    ], s, c)


@app.command()
def report() -> None:
    """Rebuild views and print the data-quality report."""
    _report(Settings(), RunContext())


if __name__ == "__main__":
    app()
