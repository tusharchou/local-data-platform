"""Running a report: the queries, the release they read, the window, the time zone and ``ldp report check``.

The tables are a gold table of five orders and one of two customers, with a release. The numbers are small enough to
work out by hand, and each answer is written down in the test. Everything is offline. The sandbox of ``ldp mcp`` runs
the queries, so the tests also cover what it learned for reports, which is a session time zone, named parameters, a
table that reads one snapshot under its own name, and the tables that a query reads.
"""

import datetime as dt
import io
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from local_data_platform import metrics
from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.gold.build import build_gold
from local_data_platform.gold.release import current_release
from local_data_platform.mcp_server import sandbox as sandbox_module
from local_data_platform.mcp_server.discovery import discover_tables
from local_data_platform.mcp_server.errors import QueryFailed, QueryRejected
from local_data_platform.mcp_server.sandbox import DuckDBSandbox
from local_data_platform.report import engine
from local_data_platform.report.engine import check_report, coerce_parameter, parameter_values, run_queries
from local_data_platform.report.spec import Parameter, load_report, read_report

CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
D = Decimal
UTC = dt.timezone.utc
SCHEMA = pa.schema([
    ("order_date", pa.date32()), ("ordered_at", pa.timestamp("us")), ("paid_at", pa.timestamp("us", tz="UTC")),
    ("city", pa.string()), ("cuisine", pa.string()), ("orders", pa.int64()), ("revenue", pa.decimal128(18, 2))])


def order(day, ordered_at, paid_at, city, cuisine, orders, revenue):
    return (day, ordered_at, paid_at, city, cuisine, orders, D(revenue))


ROWS = [
    order(dt.date(2026, 7, 30), dt.datetime(2026, 7, 30, 23, 30), dt.datetime(2026, 7, 30, 18, 0, tzinfo=UTC),
          "Pune", "Thai", 2, "20.00"),
    order(dt.date(2026, 7, 31), dt.datetime(2026, 7, 31, 3, 0), dt.datetime(2026, 7, 30, 21, 30, tzinfo=UTC),
          "Pune", "Thai", 3, "30.00"),
    order(dt.date(2026, 7, 31), dt.datetime(2026, 7, 31, 22, 0), dt.datetime(2026, 7, 31, 16, 30, tzinfo=UTC),
          "Delhi", "Italian", 1, "15.50"),
    order(dt.date(2026, 8, 1), dt.datetime(2026, 8, 1, 3, 0), dt.datetime(2026, 7, 31, 21, 30, tzinfo=UTC),
          "Pune", "Italian", 4, "40.00"),
    order(dt.date(2026, 8, 2), dt.datetime(2026, 8, 2, 12, 0), dt.datetime(2026, 8, 2, 6, 30, tzinfo=UTC),
          "Delhi", "Thai", 5, "50.00"),
]
"""Five orders. ``ordered_at`` is the wall-clock time in India, and ``paid_at`` is the same moment in UTC, which is five
and a half hours earlier. The 31st of July, in India, is the second and the third row, by any of the three columns."""
METRICS = {
    "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "sales_metrics", "who": "analytics@example.com",
    "semantic_models": [{
        "name": "daily_sales", "table": "gold.gold_sales",
        "time_dimension": {"column": "order_date", "grains": ["day", "week", "month", "quarter", "year"]},
        "dimensions": ["city", "cuisine"],
        "measures": [{"name": "revenue_sum", "agg": "sum", "expr": "revenue"},
                     {"name": "order_sum", "agg": "sum", "expr": "orders"}]}],
    "metrics": [
        {"name": "revenue", "type": "simple", "measure": "revenue_sum", "format": "currency", "description": "Money."},
        {"name": "orders", "type": "simple", "measure": "order_sum", "description": "Orders."},
    ],
}
WINDOW = "2026-07-31/2026-08-01"
BY_DATE = "order_date >= $window_start AND order_date < $window_end"
QUERIES = {
    "totals": {"sql": f"SELECT sum(orders) AS orders, sum(revenue) AS revenue FROM gold_sales WHERE {BY_DATE}"},
    "by_city": {"sql": f"SELECT city, sum(orders) AS orders FROM gold.gold_sales WHERE {BY_DATE} GROUP BY 1 ORDER BY "
                       f"1"},
    "everything": {"sql": "SELECT count(*) AS n, sum(orders) AS orders FROM gold_sales"},
}
BLOCKS = [
    {"type": "kpi", "query": "totals", "value": "revenue"},
    {"type": "bar", "query": "by_city", "x": "city", "y": "orders"},
    {"type": "kpi", "metric": "revenue", "grain": "day"},
    {"type": "line", "metric": "orders", "grain": "day", "by": "city"},
]
MOMENTS = {"sql": "SELECT current_setting('TimeZone') AS zone, CAST(TIMESTAMPTZ '2026-07-31 20:00:00+00' AS VARCHAR) "
                  "AS shown, CAST(date_trunc('day', TIMESTAMPTZ '2026-07-31 20:00:00+00') AS DATE) AS day"}


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def table_of(rows=ROWS) -> pa.Table:
    return pa.table({name: [row[position] for row in rows] for position, name in enumerate(SCHEMA.names)},
                    schema=SCHEMA)


def gold_config(name: str) -> dict:
    return {"identifier": name, "who": "ops", "metadata": {
        "source": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": f"The table {name}.", "grain": "one row per order"}}}


def gold_table(config: Path, name: str = "gold_sales") -> Iceberg:
    return Iceberg(name, {"identifier": "gold", "warehouse_path": str(config / "warehouse")})


def spec_of(**metadata) -> dict:
    """A report over the lake. A value of ``None`` removes a key of ``metadata``."""
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily", "metadata": {
        "tables": {"config": ["../config"], "allow": ["gold.*"]}, "queries": dict(QUERIES), "blocks": list(BLOCKS)}}
    for key, value in metadata.items():
        if value is None:
            spec["metadata"].pop(key, None)
        else:
            spec["metadata"][key] = value
    return spec


def write_good(lake: Path, name: str = "daily.json", **metadata) -> Path:
    return write(lake / "reports" / name, spec_of(**metadata))


def report_of(lake: Path, **metadata):
    return load_report(write_good(lake, **metadata))


def one(query: str, **metadata) -> dict:
    """The keys of a report that has one query called ``q``, which a table block shows."""
    return {"queries": {"q": {"sql": query}}, "blocks": [{"type": "table", "query": "q"}], **metadata}


@pytest.fixture
def lake(tmp_path):
    """``config/`` with the gold table of ``ROWS``, its config and a metrics file, and an empty ``reports/``."""
    config = tmp_path / "config"
    gold_table(config).put(table_of())
    write(config / "gold_sales.json", gold_config("gold_sales"))
    write(config / "metrics.json", METRICS)
    (tmp_path / "reports").mkdir()
    return tmp_path


def add_customers(lake: Path) -> None:
    gold_table(lake / "config", "gold_customers").put(pa.table({"id": ["c1", "c2"], "city": ["Pune", "Delhi"]}))
    write(lake / "config" / "gold_customers.json", gold_config("gold_customers"))


def cut(lake: Path) -> str:
    """Build the gold folder, which cuts a release, and return its name."""
    return build_gold(lake / "config", release=True).release.release.name


def rows(run, name: str) -> list[dict]:
    return run.tables[name].to_pylist()


def by_column(lake: Path, column: str, window, timezone: str | None, now=None) -> list[str]:
    """The ``orders`` of the rows that ``column`` puts in the window, in the order of ``ordered_at``, from a query."""
    where = f"{column} >= $window_start AND {column} < $window_end"
    query = f"SELECT cast(orders AS VARCHAR) AS orders FROM gold_sales WHERE {where} ORDER BY ordered_at"
    run = run_queries(report_of(lake, **one(query, **({"timezone": timezone} if timezone else {}))), window, now=now)
    assert run.ok, run.problems
    return [row["orders"] for row in rows(run, "q")]


# --------------------------------------------------------------------------- running a report


def test_a_report_runs_every_query_and_every_metric_block_and_says_what_each_did(lake):
    cut(lake)

    run = run_queries(report_of(lake), WINDOW)

    assert run.ok and run.problems == []
    assert list(run.queries) == ["totals", "by_city", "everything", "blocks[2]", "blocks[3]"]
    totals = run.queries["totals"]
    assert (totals.kind, totals.ok, totals.rows, totals.columns, totals.tables) == (
        "query", True, 1, ("orders", "revenue"), ("gold.gold_sales",))
    assert totals.parameters == ("window_end", "window_start") and totals.duration_ms >= 0 and not totals.truncated
    assert "FROM gold_sales" in totals.sql and totals.problem is None
    assert rows(run, "totals") == [{"orders": 4, "revenue": D("45.50")}]
    assert rows(run, "by_city") == [{"city": "Delhi", "orders": 1}, {"city": "Pune", "orders": 3}]
    assert rows(run, "everything") == [{"n": 5, "orders": 15}]
    metric = run.queries["blocks[2]"]
    assert (metric.kind, metric.rows, metric.columns, metric.tables) == ("metric", 1, ("day", "revenue"),
                                                                         ("gold.gold_sales",))
    assert rows(run, "blocks[2]") == [{"day": dt.date(2026, 7, 31), "revenue": D("45.50")}]
    assert metric.parameters == () and metric.sql.startswith("SELECT")
    assert rows(run, "blocks[3]") == [{"day": dt.date(2026, 7, 31), "city": "Delhi", "orders": 1},
                                      {"day": dt.date(2026, 7, 31), "city": "Pune", "orders": 3}]


def test_a_metric_block_computes_what_ldp_metrics_query_computes_for_the_same_days(lake):
    cut(lake)

    daily = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=[{"type": "line", "metric": "revenue",
                                                                        "grain": "day"}])),
                        None)
    window = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=[{"type": "kpi", "metric": "orders"}])),
                         "2026-07-31/2026-08-03")

    assert rows(daily, "blocks[0]") == metrics.query(lake / "config", "revenue", grain="day").to_pylist()
    assert rows(window, "blocks[0]") == metrics.query(lake / "config", "orders", start="2026-07-31",
                                                      end="2026-08-02").to_pylist() == [{"orders": 13}]


def test_a_metric_block_reads_the_days_that_the_window_touches_in_the_time_zone_of_the_report(lake):
    blocks = [{"type": "line", "metric": "orders", "grain": "day"}]

    india = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=blocks, timezone="Asia/Kolkata")), WINDOW)
    utc = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=blocks)), WINDOW)

    # 2026-07-31 00:00 UTC to 2026-08-01 00:00 UTC is 05:30 on the 31st to 05:30 on the 1st in India, so two days
    # are touched
    assert rows(india, "blocks[0]") == [{"day": dt.date(2026, 7, 31), "orders": 4},
                                        {"day": dt.date(2026, 8, 1), "orders": 4}]
    assert rows(utc, "blocks[0]") == [{"day": dt.date(2026, 7, 31), "orders": 4}]


def test_without_a_window_a_metric_block_reads_every_day(lake):
    run = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=[{"type": "kpi", "metric": "orders"}])), None)

    assert run.ok and rows(run, "blocks[0]") == [{"orders": 15}]


def test_every_query_runs_even_when_one_of_them_fails_and_each_problem_has_the_path_of_its_query(lake):
    queries = {"first": {"sql": "SELECT 1 AS n"}, "bad": {"sql": "SELECT nope FROM gold_sales"},
               "last": {"sql": "SELECT 3 AS n"}, "worse": {"sql": "SELECT * FROM missing_table"}}

    run = run_queries(report_of(lake, queries=queries, blocks=[{"type": "table", "query": "first"}]), None)

    assert not run.ok and list(run.queries) == ["first", "bad", "last", "worse"]
    assert [(name, item.ok) for name, item in run.queries.items()] == [
        ("first", True), ("bad", False), ("last", True), ("worse", False)]
    assert rows(run, "last") == [{"n": 3}] and "bad" not in run.tables
    assert run.problems[0].startswith("metadata.queries.bad.sql: Binder Error: Referenced column \"nope\" not found")
    assert run.problems[1].startswith("metadata.queries.worse.sql: Catalog Error: Table with name missing_table does "
                                      "not exist")
    assert run.problems[1].endswith("The tables of this report are ['gold.gold_sales']")
    assert "does not exist. The tables of this report are" in run.problems[1]  # not "exist!." as DuckDB writes it
    assert (run.queries["bad"].problem == run.problems[0] and run.queries["bad"].rows == 0
            and run.queries["bad"].table is None)


def test_a_query_that_is_not_one_select_is_refused_by_the_guard_and_names_what_it_found(lake):
    queries = {"copy": {"sql": "COPY (SELECT 1) TO 'out.csv'"}, "two": {"sql": "SELECT 1; SELECT 2"},
               "drop": {"sql": "DROP TABLE gold_sales"}, "file": {"sql": "SELECT * FROM read_csv('/etc/passwd')"},
               "pragma": {"sql": "PRAGMA database_list"}}

    run = run_queries(report_of(lake, queries=queries, blocks=[{"type": "table", "query": "copy"}]), None)

    assert [item.ok for item in run.queries.values()] == [False] * 5
    problems = {name: item.problem for name, item in run.queries.items()}
    assert problems["copy"] == ("metadata.queries.copy.sql: only read-only SELECT or WITH queries are allowed; this "
                                "statement starts with 'COPY'")
    assert "exactly one statement is allowed; found 2" in problems["two"]
    assert "starts with 'DROP'" in problems["drop"] and "starts with 'PRAGMA'" in problems["pragma"]
    assert "blocked by the sandbox" in problems["file"]
    assert not (lake / "out.csv").exists() and not (lake / "reports" / "out.csv").exists()
    assert rows(run_queries(report_of(lake), WINDOW), "everything") == [{"n": 5, "orders": 15}]  # nothing was dropped


def test_a_query_cannot_change_the_time_zone_or_open_another_database(lake):
    queries = {"zone": {"sql": "SET TimeZone = 'UTC'"}, "attach": {"sql": "ATTACH 'x.db'"}, "ok": {"sql": "SELECT 1 AS "
                                                                                                          "n"}}

    run = run_queries(report_of(lake, queries=queries, timezone="Asia/Kolkata", blocks=[{"type": "table",
                                                                                         "query": "ok"}]), None)

    assert [item.ok for item in run.queries.values()] == [False, False, True]
    assert ("starts with 'SET'" in run.queries["zone"].problem
            and "starts with 'ATTACH'" in run.queries["attach"].problem)


def test_a_query_that_runs_too_long_is_stopped_by_the_timeout_and_the_others_still_run(lake):
    queries = {"slow": {"sql": "SELECT sum(hash(i)) AS n FROM range(10000000000) t(i)"}, "quick": {"sql": "SELECT 1 AS "
                                                                                                          "n"}}
    report = report_of(lake, queries=queries, blocks=[{"type": "table", "query": "quick"}])

    run = run_queries(report, None, timeout_s=0.2)

    assert run.queries["slow"].problem == ("metadata.queries.slow.sql: the query ran longer than the 0.2s timeout and "
                                           "was stopped")
    assert run.queries["quick"].ok and rows(run, "quick") == [{"n": 1}]
    assert report_of(lake, queries=queries, timeout_s=0.25, blocks=[{"type": "table",
                                                                     "query": "quick"}]).timeout_s == 0.25


def test_the_row_cap_cuts_a_long_result_and_says_so_and_the_report_has_a_cap_of_its_own(lake):
    many = "SELECT i FROM range(50) t(i) ORDER BY i"
    report = report_of(lake, **one(many, row_cap=7))

    run = run_queries(report, None)

    assert (run.queries["q"].rows, run.queries["q"].truncated) == (7, True)
    assert [row["i"] for row in rows(run, "q")] == list(range(7))
    assert (run_queries(report, None, row_cap=60).queries["q"].rows, run_queries(report, None, row_cap=60).queries[
        "q"].truncated) == (50, False)
    assert run_queries(report_of(lake, **one(many)), None).queries["q"].rows == 50


# --------------------------------------------------------------------------- the release a report reads


def test_a_query_reads_the_table_as_the_current_release_holds_it_and_not_the_rows_added_after(lake):
    assert cut(lake) == "r1"
    gold_table(lake / "config").put(table_of(ROWS[:2]))
    queries = {"n": {"sql": "SELECT count(*) AS n FROM gold_sales"}, "m": {"sql": "SELECT count(*) AS n FROM "
                                                                                  "gold.gold_sales"}}
    report = report_of(lake, queries=queries, blocks=[{"type": "table", "query": "n"}])

    run = run_queries(report, None)

    assert rows(run, "n") == [{"n": 5}] and rows(run, "m") == [{"n": 5}]
    assert rows(run_queries(report, None, live=True), "n") == [{"n": 7}]
    assert cut(lake) == "r2"
    assert rows(run_queries(report, None), "n") == [{"n": 7}]
    assert rows(run_queries(report, None, release="r1"), "n") == [{"n": 5}]
    assert rows(run_queries(report, None, release="r2"), "n") == [{"n": 7}]


def test_the_run_says_which_version_of_each_table_it_read_and_the_snapshot(lake):
    cut(lake)
    release = current_release(lake / "config")
    gold_table(lake / "config").put(table_of(ROWS[:1]))

    run = run_queries(report_of(lake), WINDOW)
    live = run_queries(report_of(lake), WINDOW, live=True)

    [read] = run.reads.values()
    assert (read.table, read.release, read.snapshot_id) == ("gold.gold_sales", "r1",
                                                            release.snapshots()["gold.gold_sales"])
    assert read.source == f"release r1, cut {release.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    assert read.to_dict()["snapshot_id"] == read.snapshot_id
    [now] = live.reads.values()
    assert (now.release, now.source) == (None, "the tables as they are now")
    assert now.snapshot_id == gold_table(lake / "config").table().current_snapshot().snapshot_id != read.snapshot_id


def test_without_a_release_a_report_reads_the_tables_as_they_are_and_says_why(lake):
    run = run_queries(report_of(lake), WINDOW)

    [read] = run.reads.values()
    assert run.ok and read.release is None
    assert read.source == "the tables as they are now, because no release has been cut yet"
    assert read.snapshot_id == gold_table(lake / "config").table().current_snapshot().snapshot_id


def test_a_release_that_does_not_exist_is_a_problem_and_a_release_with_live_is_refused(lake):
    cut(lake)

    run = run_queries(report_of(lake), WINDOW, release="r9")

    assert not run.ok and len(run.problems) == 1
    assert run.problems[0].startswith("release: there is no release r9 of gold in ") and run.problems[0].endswith("It "
                                                                                                                  "has "
                                                                                                                  "r1")
    with pytest.raises(ConfigError, match="pass either a release or live, not both"):
        run_queries(report_of(lake), WINDOW, release="r1", live=True)
    assert run_queries(report_of(lake), WINDOW, release="r1").ok


def test_a_table_that_the_release_does_not_hold_is_read_as_it_is_now_and_the_run_says_why(lake):
    cut(lake)
    add_customers(lake)
    both = "SELECT (SELECT count(*) FROM gold_sales) AS sales, (SELECT count(*) FROM gold_customers) AS people"

    run = run_queries(report_of(lake, **one(both)), None, release="r1")

    assert run.ok and rows(run, "q") == [{"sales": 5, "people": 2}]
    assert run.reads["gold.gold_sales"].release == "r1" and run.reads["gold.gold_customers"].release is None
    assert run.reads["gold.gold_customers"].source == ("the tables as they are now, because release r1 does not hold "
                                                       "gold.gold_customers")


def test_a_namespace_that_has_no_release_is_read_as_it_is_even_when_a_release_is_named(lake):
    cut(lake)
    events = gold_config("silver_events")
    for section in ("source", "target"):
        events["metadata"][section]["catalog"] = {"identifier": "silver", "warehouse_path": "warehouse"}
    events["metadata"]["docs"]["layer"] = "silver"
    Iceberg("silver_events", {"identifier": "silver", "warehouse_path": str(lake / "config" / "warehouse")}).put(
        pa.table({"id": [1, 2, 3]}))
    write(lake / "config" / "silver_events.json", events)
    query = ("SELECT (SELECT count(*) FROM gold.gold_sales) AS sales, "
             "(SELECT count(*) FROM silver.silver_events) AS events")

    run = run_queries(report_of(lake, **one(query, tables={"config": ["../config"]})), None, release="r1")

    assert run.ok and rows(run, "q") == [{"sales": 5, "events": 3}]
    assert run.reads["gold.gold_sales"].release == "r1" and run.reads["silver.silver_events"].release is None
    assert run.reads["silver.silver_events"].source == "the tables as they are now, because no release has been cut yet"


def test_a_query_that_duckdb_cannot_name_the_tables_of_counts_as_reading_every_table(lake, monkeypatch):
    add_customers(lake)
    monkeypatch.setattr(DuckDBSandbox, "tables_in", lambda self, sql: None)

    run = run_queries(report_of(lake, **one("SELECT 1 AS n")), None)

    assert run.ok and run.queries["q"].tables == ("gold.gold_customers", "gold.gold_sales")
    assert sorted(run.reads) == ["gold.gold_customers", "gold.gold_sales"]


def test_the_run_names_only_the_tables_that_a_query_reads(lake):
    add_customers(lake)
    cut(lake)
    sales = {"sql": "SELECT count(*) AS n FROM gold.gold_sales"}
    joined = {"sql": "WITH c AS (SELECT * FROM gold_customers) SELECT count(*) AS n FROM c JOIN gold_sales s ON s.city "
                     "= c.city"}

    only_sales = run_queries(report_of(lake, queries={"sales": sales}, blocks=[{"type": "table", "query": "sales"}]),
                             None)
    both = run_queries(report_of(lake, queries={"sales": sales, "joined": joined}, blocks=[{"type": "table",
                                                                                            "query": "sales"}]),
                       None)

    assert list(only_sales.reads) == ["gold.gold_sales"] and only_sales.queries["sales"].tables == ("gold.gold_sales",)
    assert both.ok and both.queries["joined"].tables == ("gold.gold_customers", "gold.gold_sales")
    assert sorted(both.reads) == ["gold.gold_customers", "gold.gold_sales"]
    assert rows(both, "joined") == [{"n": 3 + 2}]  # Pune has three orders and one customer, Delhi two and one


def test_a_report_reads_only_the_tables_that_it_may_and_the_others_do_not_exist_for_it(lake):
    gold_table(lake / "config", "silver_extra").put(pa.table({"id": [1]}))
    write(lake / "config" / "silver_extra.json", gold_config("silver_extra"))
    extra = "SELECT count(*) AS n FROM silver_extra"

    narrow = run_queries(report_of(lake, **one(extra, tables={"config": ["../config"], "allow": ["gold.gold_sales"]})),
                         None)
    wide = run_queries(report_of(lake, **one(extra, tables={"config": ["../config"]})), None)

    assert not narrow.ok and "Table with name silver_extra does not exist" in narrow.problems[0]
    assert "The tables of this report are ['gold.gold_sales']" in narrow.problems[0]
    assert wide.ok and rows(wide, "q") == [{"n": 1}]


def test_a_table_that_is_found_and_cannot_be_read_yet_is_listed_with_the_reason_in_the_problem_of_a_query(lake):
    later = gold_config("gold_later")
    for section in ("source", "target"):
        later["metadata"][section]["catalog"] = {"identifier": "gold",
                                                 "warehouse_path": "other"}  # a warehouse nobody built
    write(lake / "config" / "gold_later.json", later)
    queries = {"later": {"sql": "SELECT count(*) AS n FROM gold_later"}, "ok": {"sql": "SELECT 1 AS n"}}

    run = run_queries(report_of(lake, queries=queries, blocks=[{"type": "table", "query": "ok"}]), None)

    assert [item["table"] for item in run.unavailable] == ["gold.gold_later"]
    assert run.queries["ok"].ok and not run.queries["later"].ok
    assert ("These tables were found and cannot be read: gold.gold_later (there is no catalog at "
            in run.queries["later"].problem)


def test_a_report_with_no_table_to_read_says_why_and_runs_nothing(tmp_path):
    (tmp_path / "config").mkdir()
    write(tmp_path / "reports" / "daily.json", spec_of())

    run = run_queries(load_report(tmp_path / "reports" / "daily.json"), None)

    assert run.problems == ["metadata.tables: there is no table to read (none found)"] and run.queries == {}


def test_a_folder_of_tables_that_does_not_exist_is_one_problem_of_the_tables(lake):
    run = run_queries(report_of(lake, tables={"config": ["../nowhere"]}), None)

    assert len(run.problems) == 1 and run.problems[0].startswith("metadata.tables: config path not found: ")


def test_a_report_may_name_a_catalog_spec_and_read_the_release_of_the_tables_of_the_catalog(lake):
    cut(lake)
    gold_table(lake / "config").put(table_of(ROWS[:2]))
    write(lake / "catalog.json", {"catalog": {"identifier": "gold",
                                              "warehouse_path": str(lake / "config" / "warehouse")}})

    run = run_queries(report_of(lake, **one("SELECT count(*) AS n FROM gold.gold_sales",
                                            tables={"catalog": ["../catalog.json"]})),
                      None)

    assert run.ok and rows(run, "q") == [{"n": 5}] and run.reads["gold.gold_sales"].release == "r1"


# --------------------------------------------------------------------------- the window and the time zone


def test_the_session_time_zone_is_the_time_zone_of_the_report_and_utc_when_it_names_none(lake):
    india = run_queries(report_of(lake, **one(MOMENTS["sql"], timezone="Asia/Kolkata")), None)
    utc = run_queries(report_of(lake, **one(MOMENTS["sql"])), None)
    paris = run_queries(report_of(lake, **one(MOMENTS["sql"], timezone="Europe/Paris")), None)

    assert rows(india, "q") == [{"zone": "Asia/Kolkata", "shown": "2026-08-01 01:30:00+05:30", "day": dt.date(2026, 8,
                                                                                                              1)}]
    assert rows(utc, "q") == [{"zone": "UTC", "shown": "2026-07-31 20:00:00+00", "day": dt.date(2026, 7, 31)}]
    assert rows(paris, "q")[0]["shown"] == "2026-07-31 22:00:00+02"


def test_the_time_zone_of_the_machine_does_not_move_a_day(lake):
    path = write_good(lake, "moments.json", **one(MOMENTS["sql"], timezone="Asia/Kolkata"))
    script = ("import json, sys\nfrom local_data_platform.report.engine import check_report\ncheck = "
              "check_report(sys.argv[1], None)\nprint(json.dumps({'ok': check.ok, 'problems': check.problems, 'rows': "
              "check.run.tables['q'].to_pylist()}, default=str))\n")

    answers = {}
    for machine in ("UTC", "Pacific/Auckland", "America/Los_Angeles"):
        done = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "TZ": machine})
        assert done.returncode == 0, done.stderr
        answers[machine] = json.loads(done.stdout)

    assert answers["UTC"]["ok"] is True
    assert answers["UTC"] == answers["Pacific/Auckland"] == answers["America/Los_Angeles"]
    assert answers["UTC"]["rows"] == [{"zone": "Asia/Kolkata", "shown": "2026-08-01 01:30:00+05:30",
                                       "day": "2026-08-01"}]


def test_the_window_is_two_wall_clock_times_of_the_report_time_zone_and_any_column_cuts_the_same_day(lake):
    now = dt.datetime(2026, 8, 1, 10, 0, tzinfo=UTC)

    # yesterday in India is the 31st of July there. By any of the three columns that is the second and the third order
    for column in ("order_date", "ordered_at", "paid_at"):
        assert by_column(lake, column, "yesterday", "Asia/Kolkata", now) == ["3", "1"], column
    # yesterday in UTC is the 31st of July there. The columns disagree, as they should, because ordered_at is the
    # wall clock of India and paid_at is a moment, so that the 31st in UTC holds the third and the fourth payment
    assert by_column(lake, "order_date", "yesterday", None, now) == ["3", "1"]
    assert by_column(lake, "ordered_at", "yesterday", None, now) == ["3", "1"]
    assert by_column(lake, "paid_at", "yesterday", None, now) == ["1", "4"]


def test_a_window_written_with_dates_is_read_as_utc_and_a_query_sees_it_in_the_time_zone_of_the_report(lake):
    bounds = "SELECT CAST($window_start AS VARCHAR) AS start, CAST($window_end AS VARCHAR) AS stop"

    india = run_queries(report_of(lake, **one(bounds, timezone="Asia/Kolkata")), WINDOW)
    utc = run_queries(report_of(lake, **one(bounds)), WINDOW)
    local = run_queries(report_of(lake, **one(bounds, timezone="Asia/Kolkata")),
                        "2026-07-31T00:00:00+05:30/2026-08-01T00:00:00+05:30")

    assert rows(india, "q") == [{"start": "2026-07-31 05:30:00", "stop": "2026-08-01 05:30:00"}]
    assert rows(utc, "q") == [{"start": "2026-07-31 00:00:00", "stop": "2026-08-01 00:00:00"}]
    assert rows(local, "q") == [{"start": "2026-07-31 00:00:00",
                                 "stop": "2026-08-01 00:00:00"}]  # an offset names a local day
    assert india.window == (dt.datetime(2026, 7, 31, tzinfo=UTC), dt.datetime(2026, 8, 1, tzinfo=UTC))
    assert india.parameters["window_start"] == dt.datetime(2026, 7, 31, 5, 30)  # wall clock, with no time zone


def test_a_window_that_is_malformed_is_an_error_and_a_query_that_needs_a_window_says_so(lake):
    report = report_of(lake)

    for bad in ("2026-08-01/2026-07-31", "nonsense", "2026-08-01"):
        with pytest.raises(ConfigError, match="window"):
            run_queries(report, bad)
    run = run_queries(report, None)
    needs = "uses $window_start and $window_end, and no window was given. Pass --window START/END, or yesterday"
    assert run.problems == [f"metadata.queries.totals.sql: {needs}", f"metadata.queries.by_city.sql: {needs}"]
    assert run.queries["everything"].ok


def test_a_window_may_be_given_as_a_pair_or_a_dict(lake):
    pair = run_queries(report_of(lake), (dt.date(2026, 7, 31), dt.date(2026, 8, 1)))
    named = run_queries(report_of(lake), {"start": "2026-07-31", "end": "2026-08-01"})

    assert rows(pair, "totals") == rows(named, "totals") == [{"orders": 4, "revenue": D("45.50")}]


# --------------------------------------------------------------------------- parameters


PARAMETERS_QUERY = "SELECT $limit_n AS limit_n, $rate AS rate, $on AS on_, $since AS since, $label AS label"


def parameters_report(lake: Path, **declared):
    return report_of(lake, **one(PARAMETERS_QUERY, parameters={
        "limit_n": {"type": "integer", "default": 3}, "rate": {"type": "number", "default": 0.5},
        "on": {"type": "boolean", "default": True}, "since": {"type": "date", "default": "2026-01-02"},
        "label": {"type": "string", "default": "a b"}, **declared}))


def test_a_query_uses_the_parameters_the_report_declares_with_their_defaults(lake):
    run = run_queries(parameters_report(lake), None)

    assert run.ok
    assert rows(run, "q") == [{"limit_n": 3, "rate": 0.5, "on_": True, "since": dt.date(2026, 1, 2), "label": "a b"}]
    assert run.queries["q"].parameters == ("label", "limit_n", "on", "rate", "since")
    assert run.parameters == {"limit_n": 3, "rate": 0.5, "on": True, "since": dt.date(2026, 1, 2), "label": "a b"}


def test_a_parameter_is_given_as_text_of_the_command_line_or_as_its_type_and_is_coerced(lake):
    text = run_queries(parameters_report(lake), None, params={"limit_n": "-7", "rate": "2.5e1", "on": "FALSE",
                                                              "since": "2026-03-04", "label": " x "})
    typed = run_queries(parameters_report(lake), None, params={"limit_n": 8, "rate": 2, "on": False,
                                                               "since": dt.date(2026, 3, 4), "label": "y"})

    assert rows(text, "q") == [{"limit_n": -7, "rate": 25.0, "on_": False, "since": dt.date(2026, 3, 4), "label": " "
                                                                                                                  "x "}]
    assert rows(typed, "q") == [{"limit_n": 8, "rate": 2.0, "on_": False, "since": dt.date(2026, 3, 4), "label": "y"}]


def test_a_value_is_bound_as_data_and_never_becomes_part_of_the_sql(lake):
    hostile = "x'; DROP TABLE gold_sales; --"
    query = "SELECT $label AS label, (SELECT count(*) FROM gold_sales) AS n"

    run = run_queries(report_of(lake, **one(query, parameters={"label": {"type": "string", "default": "none"}})), None,
                      params={"label": hostile})

    assert run.ok and rows(run, "q") == [{"label": hostile, "n": 5}]
    assert hostile not in run.queries["q"].sql


@pytest.mark.parametrize("kind, value, expected", [
    ("integer", "abc", "the parameter 'p' has to be a whole number, got 'abc'"),
    ("integer", "1.5", "the parameter 'p' has to be a whole number, got '1.5'"),
    ("integer", True, "the parameter 'p' has to be a whole number, got True"),
    ("integer", 1.5, "the parameter 'p' has to be a whole number, got 1.5"),
    ("number", "x", "the parameter 'p' has to be a number, got 'x'"),
    ("number", True, "the parameter 'p' has to be a number, got True"),
    ("boolean", "yes", "the parameter 'p' has to be true or false, got 'yes'"),
    ("boolean", 1, "the parameter 'p' has to be true or false, got 1"),
    ("date", "2026-02-30", "the parameter 'p' has to be a day written YYYY-MM-DD, got '2026-02-30'"),
    ("date", "yesterday", "the parameter 'p' has to be a day written YYYY-MM-DD, got 'yesterday'"),
    ("date", dt.datetime(2026, 1, 1), "the parameter 'p' has to be a day written YYYY-MM-DD, got "
                                      "datetime.datetime(2026, 1, 1, 0, 0)"),
    ("string", 5, "the parameter 'p' has to be text, got 5"),
])
def test_a_value_that_is_not_of_the_type_of_its_parameter_is_refused(kind, value, expected):
    with pytest.raises(ConfigError) as caught:
        coerce_parameter(Parameter("p", kind), value)

    assert str(caught.value) == expected


def test_every_problem_with_the_parameters_is_listed_at_once_before_anything_runs(lake):
    report = parameters_report(lake, needed={"type": "string"})

    with pytest.raises(ConfigError) as caught:
        run_queries(report, None, params={"limit_n": "x", "ghost": "1", "window_start": "2026-01-01"})

    assert str(caught.value).splitlines() == [
        "4 problems with the parameters:",
        "  - the report has no parameter 'ghost'. It declares ['limit_n', 'rate', 'on', 'since', 'label', 'needed']",
        "  - window_start is set from the window, so it cannot be given as a parameter",
        "  - the parameter 'limit_n' has to be a whole number, got 'x'",
        "  - the parameter 'needed' has no default, so give it with --param needed=VALUE"]
    with pytest.raises(ConfigError) as single:
        run_queries(report, None)
    assert str(single.value) == "the parameter 'needed' has no default, so give it with --param needed=VALUE"


def test_a_query_that_uses_a_parameter_the_report_does_not_declare_or_a_positional_one_says_so(lake):
    queries = {"undeclared": {"sql": "SELECT $nope AS x"}, "positional": {"sql": "SELECT ? AS x"},
               "numbered": {"sql": "SELECT $1 AS x"}, "ok": {"sql": "SELECT $limit_n AS x"}}

    run = run_queries(report_of(lake, queries=queries, blocks=[{"type": "table", "query": "ok"}],
                                parameters={"limit_n": {"type": "integer", "default": 1}}), None)

    assert run.queries["undeclared"].problem == ("metadata.queries.undeclared.sql: uses $nope, which the report does "
                                                 "not declare. It can use ['window_start', 'window_end', 'limit_n']")
    for name in ("positional", "numbered"):
        assert run.queries[name].problem == (f"metadata.queries.{name}.sql: uses a positional parameter, $1 or ?, and "
                                             f"a report "
                                             "takes named ones such as $window_start. A value is never pasted into the "
                                             "SQL")
    assert run.queries["ok"].ok


def test_parameter_values_gives_the_window_as_wall_clock_times_and_each_declared_value(lake):
    report = report_of(lake, parameters={"top_n": {"type": "integer", "default": 4}})
    bounds = (dt.datetime(2026, 7, 31, 18, 30, tzinfo=UTC), dt.datetime(2026, 8, 1, 18, 30, tzinfo=UTC))
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))

    assert parameter_values(report, None, bounds, zone) == {
        "window_start": dt.datetime(2026, 8, 1, 0, 0), "window_end": dt.datetime(2026, 8, 2, 0, 0), "top_n": 4}
    assert parameter_values(report, {"top_n": "9"}, None, zone) == {"top_n": 9}
    assert engine.wall_clock(bounds[0], zone) == dt.datetime(2026, 8, 1, 0, 0)


# --------------------------------------------------------------------------- metric blocks that do not fit


def test_a_metric_block_that_the_metrics_do_not_know_or_cannot_group_that_way_is_a_problem_of_the_block(lake):
    blocks = [{"type": "kpi", "metric": "nonesuch"}, {"type": "bar", "metric": "revenue", "by": "kitchen"},
              {"type": "line", "metric": "revenue", "grain": "day", "filters": ["city=Pune", "ghost=1"]},
              {"type": "kpi", "metric": "revenue"}]

    run = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=blocks)), WINDOW)

    assert run.queries["blocks[0]"].problem.startswith("metadata.blocks[0].metric: unknown metric 'nonesuch'. The "
                                                       "metrics are ['revenue', 'orders']")
    assert run.queries["blocks[1]"].problem.startswith("metadata.blocks[1].metric: unknown dimension 'kitchen'")
    assert run.queries["blocks[2]"].problem.startswith("metadata.blocks[2].metric: unknown dimension 'ghost'")
    assert run.queries["blocks[3]"].ok and len(run.problems) == 3


def test_a_grain_that_the_metric_does_not_allow_or_a_year_that_it_does_not_have_is_a_problem(lake):
    write(lake / "config" / "metrics.json", {**METRICS, "semantic_models": [{
        **METRICS["semantic_models"][0], "time_dimension": {"column": "order_date", "grains": ["day"]}}]})

    blocks = [{"type": "line", "metric": "revenue", "grain": "month"}, {"type": "kpi", "metric": "revenue"}]

    run = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=blocks)), WINDOW)

    assert run.queries["blocks[0]"].problem.startswith("metadata.blocks[0].metric: ")
    assert "cannot be reported by month" in run.queries["blocks[0]"].problem
    assert "cannot be added up over the whole period" in run.queries["blocks[1]"].problem


def test_a_metric_block_without_a_metrics_file_says_so_and_a_metrics_file_with_a_problem_is_said_once_in_full(lake):
    (lake / "config" / "metrics.json").unlink()
    blocks = [{"type": "kpi", "metric": "revenue"}, {"type": "kpi", "metric": "orders"}]

    none = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=blocks)), WINDOW)
    write(lake / "config" / "metrics.json", {**METRICS, "metrics": [{"name": "x", "type": "simple", "measure": "nope",
                                                                    "description": "d"}]})
    broken = run_queries(report_of(lake, **one("SELECT 1 AS n", blocks=blocks)), WINDOW)

    assert none.queries["blocks[0]"].problem == ("metadata.blocks[0].metric: the config folders of metadata.tables "
                                                 "have no metrics file. A metrics file has \"kind\": \"Metrics\"")
    assert "unknown measure 'nope'" in broken.queries["blocks[0]"].problem
    assert broken.queries["blocks[1]"].problem == ("metadata.blocks[1].metric: the metrics files could not be read, "
                                                   "which the first metric block says")
    assert none.queries["q"].ok and broken.queries["q"].ok


def test_a_report_with_no_metric_block_does_not_need_a_metrics_file(lake):
    (lake / "config" / "metrics.json").unlink()

    run = run_queries(report_of(lake, blocks=[{"type": "kpi", "query": "totals", "value": "revenue"}]), WINDOW)

    assert run.ok and list(run.queries) == ["totals", "by_city", "everything"]


# --------------------------------------------------------------------------- check_report and the command


def test_check_report_reads_a_file_and_runs_it_and_lists_the_problems_of_either(lake):
    write(lake / "reports" / "queries.sql", "-- name: lines\nSELECT count(*) AS n FROM gold_sales;\n")
    good = write_good(lake, "good.json", queries={"lines": {"sql_file": "queries.sql"}}, blocks=[{"type": "table",
                                                                                                  "query": "lines"}])
    unreadable = write_good(lake, "unreadable.json", row_cap=0)
    failing = write_good(lake, "failing.json", **one("SELECT nope"))

    first, second, third = check_report(good), check_report(unreadable), check_report(failing)

    assert first.ok and first.problems == [] and first.report.identifier == "daily"
    assert first.run.tables["lines"].to_pylist() == [{"n": 5}]
    assert (not second.ok and second.report is None and second.run is None
            and second.problems[0].startswith("metadata.row_cap:"))
    assert not third.ok and third.run is not None and third.problems == third.run.problems and len(third.problems) == 1
    assert check_report(lake / "reports" / "nope.json").problems == [f"report file not found: "
                                                                     f"{lake / 'reports' / 'nope.json'}"]
    bad_window = check_report(good, "nonsense")
    assert not bad_window.ok and bad_window.run is None and "window" in bad_window.problems[0]


def test_the_run_is_a_plain_dict_for_json_and_holds_no_rows(lake):
    cut(lake)

    result = run_queries(report_of(lake), WINDOW).to_dict()

    assert (result["report"] == "daily" and result["ok"] is True and result["timezone"] == "UTC"
            and result["live"] is False)
    assert result["window"] == ["2026-07-31T00:00:00Z", "2026-08-01T00:00:00Z"] and result["release"] is None
    assert [item["name"] for item in result["queries"]] == ["totals", "by_city", "everything", "blocks[2]", "blocks[3]"]
    assert result["queries"][0]["rows"] == 1 and "table" not in result["queries"][0]
    assert result["tables"][0]["release"] == "r1" and result["problems"] == [] and result["unavailable"] == []
    assert result["parameters"] == {"window_start": "2026-07-31T00:00:00", "window_end": "2026-08-01T00:00:00"}
    json.dumps(result)


def test_ldp_report_check_runs_a_report_and_prints_what_each_query_did_and_the_version_it_read(lake, capsys):
    cut(lake)
    path = write_good(lake)

    assert main(["report", "check", str(path), "--window", WINDOW]) == 0

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("daily (daily.json): ok, 5 queries ran in ")
    assert lines[1] == "  window 2026-07-31T00:00:00Z/2026-08-01T00:00:00Z, time zone UTC"
    assert any(line.split()[:4] == ["totals", "query", "yes", "1"] for line in lines)
    assert any(line.split()[:4] == ["blocks[2]", "metric", "yes", "1"] for line in lines)
    assert "gold.gold_sales" in out and "release r1, cut " in out


def test_ldp_report_check_of_a_folder_checks_every_report_and_leaves_the_other_files_alone(lake, capsys):
    cut(lake)
    write_good(lake, "a.json")
    write_good(lake, "b.json", **one("SELECT 1 AS n"))
    write(lake / "reports" / "pipeline.json", {"identifier": "p"})
    write(lake / "reports" / "notes.txt", "not json")

    assert main(["report", "check", str(lake / "reports"), "--window", WINDOW]) == 0

    out = capsys.readouterr().out
    assert out.count("ok,") == 2 and "a.json" in out and "b.json" in out and "pipeline.json" not in out


def test_ldp_report_check_lists_every_problem_by_its_path_and_exits_with_1(lake, capsys):
    queries = {"good": {"sql": "SELECT 1 AS n"}, "bad": {"sql": "SELECT nope"}, "worse": {"sql": "COPY x TO 'y'"}}
    path = write_good(lake, queries=queries, blocks=[{"type": "table", "query": "good"}])

    assert main(["report", "check", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith("daily (daily.json): 2 problems, 1 of 3 queries ran")
    assert "daily.json: 2 problems:" in captured.err
    assert "  - metadata.queries.bad.sql: Binder Error: " in captured.err
    assert "  - metadata.queries.worse.sql: only read-only SELECT or WITH queries are allowed" in captured.err
    assert any(line.split()[:3] == ["bad", "query", "NO"] for line in captured.out.splitlines())


def test_ldp_report_check_of_a_file_that_is_not_valid_exits_with_1_and_prints_only_the_problems(lake, capsys):
    path = write_good(lake, row_cap=0, timeout_s=0)

    assert main(["report", "check", str(path)]) == 1

    captured = capsys.readouterr()
    assert captured.out == "" and "daily.json: 2 problems:" in captured.err
    assert "  - metadata.row_cap: must be a whole number of rows from 1 to 1000000, got 0" in captured.err


def test_ldp_report_check_with_json_prints_a_result_that_a_script_can_read(lake, capsys):
    cut(lake)
    path = write_good(lake)

    assert main(["report", "check", str(path), "--window", WINDOW, "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True and printed["report"] == "daily" and printed["file"] == str(path)
    assert printed["window"] == ["2026-07-31T00:00:00Z", "2026-08-01T00:00:00Z"]
    assert [item["name"] for item in printed["queries"]][:2] == ["totals", "by_city"]
    assert printed["tables"][0]["table"] == "gold.gold_sales" and printed["problems"] == []
    assert main(["report", "check", str(lake / "reports"), "--window", WINDOW, "--json"]) == 0
    assert [item["report"] for item in json.loads(capsys.readouterr().out)] == ["daily"]


def test_ldp_report_check_with_json_says_what_is_wrong_and_exits_with_1(lake, capsys):
    path = write_good(lake, **one("SELECT nope"))

    assert main(["report", "check", str(path), "--json"]) == 1

    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["problems"][0].startswith("metadata.queries.q.sql: Binder Error")
    assert printed["queries"][0]["ok"] is False and printed["queries"][0]["problem"] == printed["problems"][0]


def test_ldp_report_check_takes_a_release_or_live_and_parameters_and_limits(lake, capsys):
    cut(lake)
    gold_table(lake / "config").put(table_of(ROWS[:1]))
    path = write_good(lake, **one("SELECT count(*) AS n, $top AS top FROM gold_sales",
                                  parameters={"top": {"type": "integer", "default": 1}}, row_cap=3))

    assert main(["report", "check", str(path), "--release", "r1", "--param", "top=4", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["release"] == "r1" and first["live"] is False and first["parameters"] == {"top": 4}
    assert first["tables"][0]["release"] == "r1"

    assert main(["report", "check", str(path), "--live", "--json", "--row-cap", "1", "--timeout", "5"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["live"] is True and second["tables"][0]["release"] is None and second["parameters"] == {"top": 1}

    assert main(["report", "check", str(path), "--release", "r1", "--live"]) == 1
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize("name, days", [("yesterday", 1), ("last-week", 7)])
def test_ldp_report_check_counts_a_window_by_name_in_the_time_zone_of_the_report(lake, capsys, name, days):
    path = write_good(lake, **one("SELECT 1 AS n", timezone="Asia/Kolkata"))

    assert main(["report", "check", str(path), "--window", name, "--json"]) == 0

    window = json.loads(capsys.readouterr().out)["window"]
    start, end = (dt.datetime.fromisoformat(item.replace("Z", "+00:00")) for item in window)
    assert end - start == dt.timedelta(days=days)
    assert (start.hour, start.minute, end.hour, end.minute) == (18, 30, 18, 30)  # midnight in India is 18:30 in UTC


def test_ldp_report_check_notes_a_query_that_the_row_cap_cut(lake, capsys):
    path = write_good(lake, **one("SELECT i FROM range(30) t(i)"))

    assert main(["report", "check", str(path), "--row-cap", "5"]) == 0

    out = capsys.readouterr().out
    assert "note: q had more rows than the row cap, which cut them" in out
    assert any(line.split()[:4] == ["q", "query", "yes", "5"] for line in out.splitlines())


@pytest.mark.parametrize("argv, message", [
    (["report", "check", "{reports}", "--param", "top=1", "--param", "top=2"], "ldp: error: --param top is given "
                                                                               "twice"),
    (["report", "check", "{missing}"], "ldp: error: report path not found: "),
    (["report", "check", "{empty}"], 'ldp: error: there is no report in '),
    (["report", "check", "{good}", "--window", "nonsense"], "window 'nonsense' must be an ISO-8601 interval"),
    (["report", "check", "{good}", "--param", "ghost=1", "--window", WINDOW], "the report has no parameter 'ghost'. It "
                                                                              "declares none"),
    (["report", "check", "{good}", "--param", "novalue"], "expected NAME=VALUE, got 'novalue'"),
    (["report", "check", "{good}", "--row-cap", "0"], "expected a number of at least 1, got 0"),
    (["report", "check", "{good}", "--timeout", "0"], "expected a positive number of seconds, got 0"),
])
def test_ldp_report_check_errors_are_said_in_a_line_and_exit_with_1(lake, capsys, argv, message):
    good = write_good(lake)
    (lake / "empty").mkdir()
    argv = [item.format(reports=lake / "reports", missing=lake / "nope", empty=lake / "empty",
                        good=good) for item in argv]

    assert main(argv) == 1

    assert message in capsys.readouterr().err


def test_ldp_report_without_a_command_prints_its_help_and_exits_with_1(capsys):
    assert main(["report"]) == 1

    captured = capsys.readouterr()
    assert "check" in captured.out + captured.err and "ldp report" in captured.out + captured.err


def test_the_help_of_ldp_report_check_says_what_it_does_and_how_it_ends(capsys):
    assert main(["report", "check", "--help"]) == 0

    out = capsys.readouterr().out
    assert "--window" in out and "--param NAME=VALUE" in out and "--release RELEASE" in out and "--live" in out
    assert "Exit status is 0 when every report is valid and every query ran" in out


def test_ldp_report_check_without_duckdb_says_how_to_get_it_and_reading_a_report_needs_none(lake, monkeypatch, capsys):
    path = write_good(lake)

    def missing():
        raise EngineNotFound('The SQL sandbox needs the duckdb package. Install it with: pip install '
                             '"local-data-platform[duckdb]"')

    monkeypatch.setattr(sandbox_module, "import_duckdb", missing)

    assert main(["report", "check", str(path)]) == 1
    assert 'pip install "local-data-platform[duckdb]"' in capsys.readouterr().err
    assert read_report(path)[1] == []  # reading and validating the file needs no DuckDB


# --------------------------------------------------------------------------- the warehouse of ldp demo


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """The warehouse that ``ldp demo`` writes, with 200 rides, and the Arrow table of its ``rides``."""
    from local_data_platform.demo import run_demo

    workdir = tmp_path_factory.mktemp("demo") / "ldp_demo"
    run_demo(workdir, rows=200, out=io.StringIO())
    rides = Iceberg("rides", {"identifier": "demo", "warehouse_path": "warehouse"}, base_dir=workdir).get()
    return workdir, rides


def demo_report(workdir: Path, **metadata) -> Path:
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "demo_rides", "metadata": {
        "tables": {"config": ["../rides.json"]}, "queries": {}, "blocks": [{"type": "table", "query": "q"}],
        **metadata}}
    return write(workdir / "reports" / "rides.json", spec)


def test_a_good_report_over_the_ldp_demo_warehouse_gives_the_numbers_that_duckdb_gives_over_its_rides(demo):
    workdir, rides = demo
    by_city = "SELECT city, count(*) AS rides, round(sum(fare), 2) AS fare FROM rides GROUP BY 1 ORDER BY 1"
    first_day = "SELECT count(*) AS n FROM rides WHERE pickup_ts >= $window_start AND pickup_ts < $window_end"
    path = demo_report(workdir, queries={"q": {"sql": by_city}, "day": {"sql": first_day}}, timezone="Asia/Kolkata")
    first = rides["pickup_ts"].to_pylist()
    start = min(first).replace(hour=0, minute=0, second=0, microsecond=0)

    check = check_report(path, f"{start.isoformat()}+05:30/{(start + dt.timedelta(days=1)).isoformat()}+05:30")

    connection = duckdb.connect()
    connection.register("rides", rides)
    expected = connection.execute(by_city).fetchall()
    day = connection.execute(first_day.replace("$window_start", f"TIMESTAMP '{start}'").replace(
        "$window_end", f"TIMESTAMP '{start + dt.timedelta(days=1)}'")).fetchall()
    assert check.ok, check.problems
    assert [tuple(row.values()) for row in check.run.tables["q"].to_pylist()] == expected
    assert [tuple(row.values()) for row in check.run.tables["day"].to_pylist()] == day
    [read] = check.run.reads.values()
    assert read.table == "demo.rides" and read.release is None  # a table of no gold release is read as it is
    assert read.source == "the tables as they are now, because no release has been cut yet"


def test_the_ldp_demo_warehouse_shows_a_rejected_copy_a_missing_table_a_bad_parameter_and_an_unknown_metric(demo):
    workdir, _ = demo
    queries = {"q": {"sql": "SELECT count(*) AS n FROM rides"}, "copy": {"sql": "COPY rides TO 'rides.csv'"},
               "missing": {"sql": "SELECT * FROM rides_of_tomorrow"}, "limited": {"sql": "SELECT $cap AS cap"}}
    blocks = [{"type": "table", "query": "q"}, {"type": "kpi", "metric": "revenue"}]
    path = demo_report(workdir, queries=queries, blocks=blocks, parameters={"cap": {"type": "integer", "default": 1}})

    check = check_report(path, None, params={"cap": "many"})
    assert not check.ok and check.run is None and check.problems == ["the parameter 'cap' has to be a whole number, "
                                                                     "got 'many'"]
    check = check_report(path, None)

    assert not check.ok and [item.ok for item in check.run.queries.values()] == [True, False, False, True, False]
    problems = check.problems
    assert problems[0] == ("metadata.queries.copy.sql: only read-only SELECT or WITH queries are allowed; this "
                           "statement starts with 'COPY'")
    assert problems[1].startswith("metadata.queries.missing.sql: Catalog Error: Table with name rides_of_tomorrow does "
                                  "not exist")
    assert problems[2] == ("metadata.blocks[1].metric: the config folders of metadata.tables have no metrics file. A "
                           "metrics file has \"kind\": \"Metrics\"")
    assert check.run.tables["q"].to_pylist() == [{"n": 200 + 10}] and not (workdir / "rides.csv").exists()


def test_a_report_over_the_ldp_demo_warehouse_runs_in_the_time_zone_of_the_report(demo):
    workdir, _ = demo
    query = "SELECT current_setting('TimeZone') AS zone, CAST(TIMESTAMPTZ '2026-07-31 20:00:00+00' AS VARCHAR) AS shown"

    india = check_report(demo_report(workdir, queries={"q": {"sql": query}}, timezone="Asia/Kolkata"), None)
    utc = check_report(demo_report(workdir, queries={"q": {"sql": query}}), None)

    assert india.run.tables["q"].to_pylist() == [{"zone": "Asia/Kolkata", "shown": "2026-08-01 01:30:00+05:30"}]
    assert utc.run.tables["q"].to_pylist() == [{"zone": "UTC", "shown": "2026-07-31 20:00:00+00"}]


def test_a_folder_that_holds_a_report_next_to_its_configs_is_read_by_the_others_as_before(lake):
    from local_data_platform import lake as lakes

    write_good(lake, "daily.json")
    write(lake / "config" / "daily.json", spec_of())  # a report in the folder of the configs

    discovery = discover_tables([lake / "config"])
    try:
        assert [item.identifier for item in discovery.tables] == ["gold.gold_sales"] and discovery.unavailable == []
    finally:
        discovery.close()
    with lakes.open_lake([lake / "config"]) as duck:
        assert duck.query("SELECT count(*) AS n FROM gold_sales").to_pylist() == [{"n": 5}]


# --------------------------------------------------------------------------- what the sandbox learned for reports


@pytest.fixture
def found(lake):
    discovery = discover_tables([lake / "config"])
    yield discovery
    discovery.close()


def scalar(sandbox, sql: str, **params):
    return sandbox.query(sql, max_rows=10, timeout_s=10, params=params or None).table.to_pylist()


def test_the_sandbox_has_the_time_zone_it_was_given_and_a_query_cannot_change_it(found):
    with DuckDBSandbox(found.tables, timezone="Asia/Kolkata") as sandbox:
        assert scalar(sandbox, "SELECT current_setting('TimeZone') AS zone") == [{"zone": "Asia/Kolkata"}]
        with pytest.raises(QueryRejected):
            scalar(sandbox, "SET TimeZone = 'UTC'")
        assert scalar(sandbox, "SELECT current_setting('TimeZone') AS zone") == [{"zone": "Asia/Kolkata"}]
    with DuckDBSandbox(found.tables) as sandbox:
        assert scalar(sandbox, "SELECT current_setting('TimeZone') AS zone")[0]["zone"]  # the machine's own


def test_a_time_zone_that_duckdb_does_not_know_is_a_value_error(found):
    with pytest.raises(ValueError, match="unknown time zone 'Mars/Olympus'"):
        DuckDBSandbox(found.tables, timezone="Mars/Olympus")


def test_named_parameters_are_bound_and_the_names_have_to_match_the_query(found):
    with DuckDBSandbox(found.tables) as sandbox:
        assert scalar(sandbox, "SELECT $a + $b AS total, $a AS a", a=1, b=2) == [{"total": 3, "a": 1}]
        positional = sandbox.query("SELECT $1 + $2 AS total", max_rows=5, timeout_s=5, params=[1, 2])
        assert positional.table.to_pylist() == [{"total": 3}]
        with pytest.raises(QueryFailed, match="Parameter argument/count mismatch"):
            scalar(sandbox, "SELECT $a AS a", a=1, b=2)
        with pytest.raises(QueryFailed, match="Values were not provided for the following (prepared statement )?param"):
            scalar(sandbox, "SELECT $a + $b AS total", a=1)


def test_the_sandbox_lists_the_parameters_that_a_query_uses_after_the_guard_has_passed_it(found):
    with DuckDBSandbox(found.tables) as sandbox:
        assert sandbox.parameters("SELECT $b, $a, $b") == ("a", "b")
        assert sandbox.parameters("SELECT 1") == ()
        assert sandbox.parameters("SELECT ?, ?") == ("1", "2")
        assert sandbox.parameters("SELECT $2, $1") == ("1", "2")
        assert sandbox.parameters("SELECT '$not_a_parameter' AS x -- $nor_this") == ()
        with pytest.raises(QueryRejected):
            sandbox.parameters("DELETE FROM gold_sales WHERE $a")


def test_the_sandbox_says_which_tables_a_query_reads_and_a_cte_is_not_one(lake):
    add_customers(lake)
    discovery = discover_tables([lake / "config"])
    try:
        with DuckDBSandbox(discovery.tables) as sandbox:
            assert sandbox.identifiers == ["gold.gold_customers", "gold.gold_sales"]
            assert sandbox.tables_in("SELECT 1") == []
            assert sandbox.tables_in("SELECT * FROM gold.gold_sales") == ["gold.gold_sales"]
            assert sandbox.tables_in("SELECT * FROM gold_sales") == ["gold.gold_sales"]
            assert sandbox.tables_in('SELECT * FROM "gold"."gold_sales" s JOIN gold_customers c USING (city)') == [
                "gold.gold_customers", "gold.gold_sales"]
            assert sandbox.tables_in("WITH recent AS (SELECT * FROM gold_customers) SELECT * FROM recent") == [
                "gold.gold_customers"]
            assert sandbox.tables_in("SELECT (SELECT max(id) FROM gold_customers) AS m FROM range(2)") == [
                "gold.gold_customers"]
            assert sandbox.tables_in("SELECT * FROM gold_sales WHERE city IN (SELECT city FROM gold_customers)") == [
                "gold.gold_customers", "gold.gold_sales"]
            assert sandbox.tables_in("SELECT * FROM other_schema.gold_sales") == []
            # a CTE that has the name of a table counts as a read of the table, which is the safe way to be wrong
            shadowed = sandbox.tables_in("WITH gold_sales AS (SELECT 1 AS x) SELECT * FROM gold_sales")
            assert shadowed == ["gold.gold_sales"]
            with pytest.raises(QueryRejected):
                sandbox.tables_in("DROP TABLE gold_sales")
    finally:
        discovery.close()


def test_the_sandbox_says_none_when_duckdb_cannot_serialize_a_query(found):
    class Answer:
        def __init__(self, tree):
            self.tree = tree

        def fetchall(self):
            return [[json.dumps(self.tree)]]

    with DuckDBSandbox(found.tables) as sandbox:
        real = sandbox._con

        def serializing(outcome):
            class Fake:
                def __getattr__(self, name):
                    return getattr(real, name)

                def execute(self, sql, *args):
                    if "json_serialize_sql" not in str(sql):
                        return real.execute(sql, *args)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return Answer(outcome)

            return Fake()

        try:
            refusals = (sandbox._duckdb.Error("cannot serialize"), {"error": True, "error_message": "cannot serialize"})
            for outcome in refusals:
                sandbox._con = serializing(outcome)
                assert sandbox.tables_in("SELECT * FROM gold_sales") is None
            sandbox._con = serializing({"error": False, "statements": []})
            assert sandbox.tables_in("SELECT * FROM gold_sales") == []
        finally:
            sandbox._con = real


@pytest.mark.parametrize("native", [None, False], ids=["native", "arrow"])
def test_a_table_reads_one_snapshot_under_its_own_name_and_its_bare_name_and_keeps_it_through_a_refresh(lake, native):
    table = gold_table(lake / "config")
    first = table.table().current_snapshot().snapshot_id
    table.put(table_of(ROWS[:2]))
    second = table.table().current_snapshot().snapshot_id
    discovery = discover_tables([lake / "config"])
    try:
        with DuckDBSandbox(discovery.tables, native=native) as sandbox:
            assert sandbox.snapshot_of("gold.gold_sales") == second and sandbox.snapshot_of("gold.nope") is None
            assert scalar(sandbox, "SELECT count(*) AS n FROM gold.gold_sales") == [{"n": 7}]

            sandbox.read_as_of("gold.gold_sales", first)

            assert sandbox.snapshot_of("gold.gold_sales") == first
            assert scalar(sandbox, "SELECT count(*) AS n FROM gold.gold_sales") == [{"n": 5}]
            assert scalar(sandbox, "SELECT count(*) AS n FROM gold_sales") == [{"n": 5}]
            table.put(table_of(ROWS[:1]))
            assert sandbox.refresh() == [] and scalar(sandbox, "SELECT count(*) AS n FROM gold_sales") == [{"n": 5}]
            assert sandbox.columns(sandbox.sql_name("gold.gold_sales"))["revenue"].startswith("DECIMAL")
            with pytest.raises(KeyError):
                sandbox.read_as_of("gold.nope", first)
            with pytest.raises(QueryFailed, match="has no snapshot 12345"):
                sandbox.read_as_of("gold.gold_sales", 12345)
            assert scalar(sandbox, "SELECT count(*) AS n FROM gold_sales") == [{"n": 5}]
    finally:
        discovery.close()


def test_a_table_that_was_not_put_at_a_snapshot_follows_a_refresh_as_before(lake):
    table = gold_table(lake / "config")
    discovery = discover_tables([lake / "config"])
    try:
        with DuckDBSandbox(discovery.tables) as sandbox:
            before = sandbox.snapshot_of("gold.gold_sales")
            table.put(table_of(ROWS[:2]))
            assert sandbox.refresh() == []
            assert scalar(sandbox, "SELECT count(*) AS n FROM gold_sales") == [{"n": 7}]
            assert sandbox.snapshot_of("gold.gold_sales") != before
    finally:
        discovery.close()
