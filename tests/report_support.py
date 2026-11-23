"""Helpers for the tests of report views and pages: a report, and a run of it, made by hand.

A run made by hand has the rows of each query as a pyarrow table and the version of each table that it says it read,
so a test of a page needs no lake and no DuckDB, and every number on the page is one that the test wrote down.
"""

import datetime as dt
from typing import Any

import pyarrow as pa

from local_data_platform.monitoring.status import Run, StatusRow
from local_data_platform.report.engine import QueryRun, ReportRun, TableRead
from local_data_platform.report.health import Health, TableHealth, judge
from local_data_platform.report.spec import Freshness, build, validate_report
from local_data_platform.report.view import resolve

UTC = dt.timezone.utc
BUILT_AT = dt.datetime(2026, 8, 2, 6, 30, 15, tzinfo=UTC)
"""The moment that the pages of the tests say they were built."""
WINDOW = (dt.datetime(2026, 7, 30, 18, 30, tzinfo=UTC), dt.datetime(2026, 7, 31, 18, 30, tzinfo=UTC))
"""The 31st of July in India, as the two UTC instants that the engine gives."""
WINDOW_UTC = (dt.datetime(2026, 7, 31, tzinfo=UTC), dt.datetime(2026, 8, 1, tzinfo=UTC))
"""The 31st of July in UTC."""
SALES = TableRead("gold.gold_sales", "r3", 8420615207115790441, "release r3, cut 2026-08-02 06:00:00 UTC",
                  "gold_sales")
CUSTOMERS = TableRead("gold.gold_customers", None, 3187645520913349012,
                      "the tables as they are now, because release r3 does not hold gold.gold_customers")


def make_report(blocks: list[dict[str, Any]], queries: list[str], **metadata: Any):
    """A report with these blocks and queries, after the checks of the file have found nothing wrong with it."""
    what = metadata.pop("what", "Orders and revenue of one day")
    spec: dict[str, Any] = {
        "apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily_sales", "who": "owner@example.com",
        "metadata": {"title": "Daily sales", "tables": {"config": ["../config"], "allow": ["gold.*"]},
                     "queries": {name: {"sql": "SELECT 1"} for name in queries or ["unused"]}, "blocks": blocks}}
    if what is not None:  # what=None is a report that does not say what it is for
        spec["what"] = what
    spec["metadata"].update(metadata)
    errors = validate_report(spec)
    assert not errors, [str(error) for error in errors]
    return build(spec)


def make_run(blocks: list[dict[str, Any]], tables: dict[str, pa.Table], *, reads: tuple[TableRead, ...] = (SALES,),
             window: tuple[dt.datetime, dt.datetime] | None = WINDOW, truncated: tuple[str, ...] = (),
             formats: dict[str, dict[str, str]] | None = None, row_cap: int = 5000, release: str | None = None,
             live: bool = False, health: Health | None = None, **metadata: Any) -> ReportRun:
    """A run of a report whose queries gave ``tables``, with its views resolved.

    Args:
        blocks: The blocks of the report.
        tables: The rows of each result by name. A query has its own name, and the metric block at position 2 has
            ``blocks[2]``.
        reads: The tables that the queries say they read.
        window: The window of the run.
        truncated: The names of the results that the row cap cut.
        formats: For a metric block, the format of each of its metric columns.
        row_cap: The row cap that the run says it had.
        release: The release that was asked for.
        live: Whether the tables were read as they are now.
        health: The data health of the tables, which a page shows as its panel, or ``None`` for a run that has none.
        **metadata: More keys of ``metadata`` of the report, such as ``timezone``.
    """
    names = [name for name in tables if not name.startswith("blocks[")]
    report = make_report(blocks, names, **metadata)
    run = ReportRun(report=report, window=window, release=release, live=live, row_cap=row_cap, health=health)
    for name, rows in tables.items():
        run.queries[name] = QueryRun(
            name=name, kind="query" if name in names else "metric", rows=rows.num_rows,
            columns=tuple(rows.schema.names), truncated=name in truncated, table=rows,
            formats=dict((formats or {}).get(name, {})))
    run.reads = {read.table: read for read in reads}
    run.views, problems = resolve(run)
    run.problems.extend(problems)
    return run


def hours_before(moment: dt.datetime, hours: float) -> dt.datetime:
    return moment - dt.timedelta(hours=hours)


def make_row(*, as_of: dt.datetime = BUILT_AT, run: Run | None = None, success_hours: float | None = None,
             commit_hours: float | None = None, freshness: str | None = "ok", problem: str | None = None,
             pipeline: str = "gold_sales", table: str = "gold.gold_sales") -> StatusRow:
    """A row of ``ldp status`` made by hand, measured at ``as_of``, with the ages in hours."""
    def seconds(hours: float | None) -> float | None:
        return None if hours is None else hours * 3600

    return StatusRow(pipeline=pipeline, table=table, owner="ops", file=f"{pipeline}.json", as_of=as_of,
                     health="error" if problem else "ok", last_run=run, last_success_age_seconds=seconds(success_hours),
                     last_commit_age_seconds=seconds(commit_hours), rows=1234, freshness=freshness, problem=problem)


def make_run_of(status: str = "published", *, started: dt.datetime = BUILT_AT - dt.timedelta(hours=3),
                rows: int | None = 1234, checks: str | None = "5/5", failing: tuple[str, ...] = (),
                duration: float | None = 2.5) -> Run:
    """A run of a pipeline made by hand."""
    return Run(run_id="gold-sales-run-7", attempt=1, status=status, started_at=started, rows_read=rows,
               rows_written=rows, checks=checks, duration_s=duration, snapshot_id=7, failing_checks=failing)


def golden_health() -> Health:
    """The health that the golden files show.

    The sales table was blocked by a check an hour ago, so it is stale for a gate that says ``warn``, and the customers
    table is written by no config of the report.
    """
    rule = Freshness(max_age_hours=26.0, on_stale="warn")
    blocked = make_run_of("blocked_quality", started=hours_before(BUILT_AT, 1), rows=0, checks="4/5",
                          failing=("not_null(city)",))
    sales = judge("gold.gold_sales", "gold_sales", make_row(run=blocked, success_hours=3, commit_hours=3), rule)
    return Health(as_of=BUILT_AT, rule=rule, tables=(sales, TableHealth(table="gold.gold_customers")))


def decimals(*values: str) -> pa.Array:
    """A column of money, ``decimal(18, 2)``, from numbers written as text."""
    from decimal import Decimal

    return pa.array([None if value is None else Decimal(value) for value in values], pa.decimal128(18, 2))


def golden_run() -> ReportRun:
    """The run that the golden files are made from, which has one block of every kind.

    It is the 31st of July in India. A report of three key figures, a bar chart, a line chart of two series, a table
    that is cut at three rows and a text that has a placeholder and characters that need escaping.
    """
    blocks = [
        {"type": "kpi", "title": "Revenue", "query": "totals", "value": "revenue", "compare": "previous",
         "format": "currency:USD"},
        {"type": "kpi", "title": "Orders", "query": "totals", "value": "orders", "compare": "previous_orders",
         "format": "integer"},
        {"type": "kpi", "title": "Share of the week", "query": "totals", "value": "share", "format": "percent"},
        {"type": "bar", "title": "Revenue by city", "query": "by_city", "x": "city", "y": "revenue",
         "format": "currency:USD"},
        {"type": "line", "title": "Revenue by day", "query": "by_day", "x": "day", "y": "revenue", "series": "city",
         "format": "currency:USD"},
        {"type": "table", "title": "Top cuisines", "query": "top", "columns": ["cuisine", "orders", "share"],
         "formats": {"orders": "integer", "share": "percent:1"}, "max_rows": 3},
        {"type": "text", "title": "Note",
         "text": "Orders & revenue of <Pune> and Delhi. There were {totals.orders} orders in all.\n\n"
                 "Numbers are from the gold tables."},
    ]
    days = [dt.date(2026, 7, 30), dt.date(2026, 7, 31)]
    tables = {
        "totals": pa.table({"revenue": decimals("1234.50"), "previous": decimals("1100.00"), "orders": [87],
                            "previous_orders": [90], "share": [0.1875]}),
        "by_city": pa.table({"city": ["Pune", "Delhi", "Mumbai"], "revenue": decimals("520.25", "410.00", "304.25")}),
        "by_day": pa.table({"day": [days[1], days[0], days[0], days[1]], "city": ["Pune", "Pune", "Delhi", "Delhi"],
                            "revenue": decimals("520.25", "480.00", "350.50", "410.00")}),
        "top": pa.table({"cuisine": ["Thai", "Italian", "Indian", "Mexican", "Greek"], "orders": [31, 24, 17, 9, 6],
                         "share": [0.3563, 0.2759, 0.1954, 0.1034, 0.069]}),
    }
    return make_run(blocks, tables, reads=(SALES, CUSTOMERS), timezone="Asia/Kolkata", health=golden_health(),
                    what="Orders and revenue of one day, from the gold tables.", title="Daily sales")
