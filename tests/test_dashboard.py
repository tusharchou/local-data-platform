"""``ldp dashboard``: the settings, the pages as plain Python, the command, and the notebook run headless.

The pages are plain Python in ``dashboard.service``, so most of this file needs neither marimo nor Altair and runs in
every job. The tests that run the notebook itself with marimo's ``App.run()``, and the one that starts the real
server, need the ``notebook`` extra and skip with that reason where it is not installed. They all read the demo
warehouse that ``ldp demo`` makes, and a report over it.
"""

import hashlib
import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from local_data_platform import cli as ldp_cli  # noqa: E402
from local_data_platform.dashboard import cli as dashboard_cli  # noqa: E402
from local_data_platform.dashboard import service  # noqa: E402
from local_data_platform.demo import run_demo  # noqa: E402
from local_data_platform.exceptions import ConfigError  # noqa: E402
from local_data_platform.mcp_server.tools import ToolOutcome  # noqa: E402

APP = Path(dashboard_cli.NOTEBOOK)
HAS_NOTEBOOK = not dashboard_cli.missing_packages()
requires_notebook = pytest.mark.skipif(
    not HAS_NOTEBOOK, reason='marimo and Altair are not installed: pip install "local-data-platform[notebook]"')
INSTALL_HINT = 'pip install "local-data-platform[notebook]"'

REPORT = {
    "apiVersion": "ldp/v1", "kind": "Report", "identifier": "rides_by_city", "who": "analyst",
    "what": "Rides and fares by city",
    "metadata": {
        "title": "Rides by city", "tables": {"config": ["../rides.json"], "allow": ["demo.*"]},
        "queries": {"by_city": {"sql": "SELECT city, count(*) AS rides, round(avg(fare), 2) AS avg_fare FROM rides "
                                       "GROUP BY 1 ORDER BY 2 DESC, 1"}},
        "blocks": [{"type": "bar", "title": "Rides by city", "query": "by_city", "x": "city", "y": "rides"},
                   {"type": "table", "query": "by_city"}]}}
CITIES = {"BKK", "LDN", "NYC", "SFO"}


# --------------------------------------------------------------------------- what the tests read


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """The demo warehouse of 40 rides, which ends with 50 rows and 4 snapshots, and three report files over it."""
    workdir = tmp_path_factory.mktemp("dashboard") / "ldp_demo"
    numbers = run_demo(workdir, rows=40, out=io.StringIO())
    reports = workdir / "reports"
    reports.mkdir()
    (reports / "rides_by_city.json").write_text(json.dumps(REPORT))
    with_events = json.loads(json.dumps(REPORT))
    with_events["identifier"] = "rides_with_events"
    with_events["metadata"]["observability"] = {"sinks": [{"type": "jsonl", "path": "events.jsonl"}]}
    (reports / "rides_with_events.json").write_text(json.dumps(with_events))
    broken = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "broken",
              "metadata": {"tables": {"config": ["../rides.json"]}, "queries": {}, "blocks": [{"type": "nope"}]}}
    (reports / "broken.json").write_text(json.dumps(broken))
    return types.SimpleNamespace(workdir=workdir, config=Path(numbers["config_path"]), reports=reports,
                                 rows=numbers["current_rows"], snapshots=numbers["snapshot_count"])


@pytest.fixture
def settings(demo):
    return service.Settings(configs=(str(demo.config),), reports=str(demo.reports))


@pytest.fixture
def dash(settings, tmp_path):
    with service.Dashboard.open(settings, audit_path=tmp_path / "audit.jsonl") as opened:
        yield opened


def tools_of(dash: service.Dashboard) -> list[str]:
    return [record.tool for record in dash.tools.audit.records]


# --------------------------------------------------------------------------- the settings


def test_the_settings_go_through_the_environment_as_they_were():
    settings = service.Settings(configs=("/a/b", "/c"), catalogs=("/spec.json",), allow=("demo.*", "x.y"), reports="/r")

    assert service.Settings.from_json(settings.to_json()) == settings
    assert service.Settings.from_environment({service.SETTINGS_ENV: settings.to_json()}) == settings
    assert service.Settings.from_json("{}") == service.Settings()


@pytest.mark.parametrize("text, problem", [
    ("not json", "is not JSON"), ("[]", "must be a JSON object"), ('{"configs": "a"}', "'configs', which must be a "
                                                                                       "list"),
    ('{"allow": [1]}', "'allow', which must be a list"), ('{"reports": 3}', "'reports', which must be text")])
def test_settings_that_are_not_what_the_command_writes_are_refused(text, problem):
    with pytest.raises(ConfigError, match=problem):
        service.Settings.from_json(text)


def test_a_notebook_that_was_not_started_by_the_command_says_how_to_start_it():
    with pytest.raises(ConfigError, match=f"ldp dashboard --config PATH, which sets {service.SETTINGS_ENV}"):
        service.Settings.from_environment({})


# --------------------------------------------------------------------------- rows, grids and charts


def test_a_name_that_is_there_twice_gets_a_number_and_a_row_can_be_a_dict():
    assert service.unique_names(["a", "b", "a", "a", "b"]) == ["a", "b", "a_2", "a_3", "b_2"]
    grid = service.grid_from({"columns": [{"name": "x", "type": "int64"}, {"name": "x", "type": "string"}],
                              "rows": [[1, "one"], [2, "two"]], "truncated": True, "max_rows": 2, "duration_ms": 1.5})
    assert grid.columns == ("x", "x_2") and grid.types == ("int64", "string")
    assert grid.rows == [{"x": 1, "x_2": "one"}, {"x": 2, "x_2": "two"}]
    assert (grid.truncated, grid.max_rows, grid.duration_ms) == (True, 2, 1.5)


def test_a_whole_number_that_a_browser_would_round_is_shown_as_text():
    assert service.shown(2**53) == 2**53 and service.shown(-(2**53)) == -(2**53)
    assert service.shown(2**53 + 1) == str(2**53 + 1) and service.shown(-(2**60)) == str(-(2**60))
    assert service.shown(True) is True and service.shown(1.5) == 1.5 and service.shown(None) is None
    grid = service.grid_from({"columns": [{"name": "id", "type": "int64"}], "rows": [[8746247374355893574]]})
    assert grid.rows == [{"id": "8746247374355893574"}]


@pytest.mark.parametrize("type_name, kind", [
    ("int64", "quantitative"), ("uint64", "quantitative"), ("double", "quantitative"), ("float", "quantitative"),
    ("decimal128(18, 2)", "quantitative"), ("string", "nominal"), ("large_string", "nominal"), ("bool", "nominal"),
    ("date32[day]", "temporal"), ("timestamp[us]", "temporal"), ("timestamp[us, tz=UTC]", "temporal"),
    ("time64[us]", "temporal")])
def test_a_chart_reads_a_column_by_its_arrow_type(type_name, kind):
    assert service.column_kind(type_name) == kind


def _grid(columns, rows):
    types = {"city": "string", "day": "date32[day]", "rides": "int64", "fare": "double", "revenue": "decimal128(18, 2)"}
    return service.Grid(columns=tuple(columns), types=tuple(types[name] for name in columns), rows=rows)


def test_a_chart_starts_with_the_first_column_that_is_not_a_number_and_the_first_number():
    by_city = service.chart_axes(_grid(["city", "rides", "fare"], []))
    assert (by_city.kind, by_city.x_default, by_city.y_default) == ("bar", "city", "rides")
    assert by_city.x == ("city", "rides", "fare") and by_city.y == ("rides", "fare")

    by_day = service.chart_axes(_grid(["rides", "day", "fare"], []))
    assert (by_day.kind, by_day.x_default, by_day.y_default) == ("line", "day", "rides")

    numbers = service.chart_axes(_grid(["rides", "fare"], []))
    assert (numbers.kind, numbers.x_default, numbers.y_default) == ("bar", "rides", "fare")

    nothing = service.chart_axes(_grid(["city"], []))
    assert nothing.y == () and nothing.y_default is None and nothing.x_default == "city"
    assert service.chart_axes(_grid([], [])).x_default is None


def test_the_rows_of_a_chart_have_a_number_in_y_and_leave_out_the_rest():
    grid = _grid(["city", "rides", "revenue"], [
        {"city": "a", "rides": 3, "revenue": "10.50"}, {"city": "b", "rides": None, "revenue": "x"},
        {"city": "c", "rides": 5, "revenue": "NaN"}, {"city": "d", "rides": True, "revenue": "-3"},
        {"city": "e", "rides": 2, "revenue": "Infinity"}])

    assert service.chart_rows(grid, "city", "rides") == [{"x": "a", "y": 3.0}, {"x": "c", "y": 5.0},
                                                         {"x": "e", "y": 2.0}]
    assert service.chart_rows(grid, "city", "revenue") == [{"x": "a", "y": 10.5}, {"x": "d", "y": -3.0}]
    with pytest.raises(ValueError, match="no column 'nope'"):
        service.chart_rows(grid, "city", "nope")


def test_numbers_under_bars_are_ordinal_and_under_lines_they_are_numbers():
    grid = _grid(["rides", "day", "city"], [])
    assert service.chart_x_type(grid, "rides", "bar") == "ordinal"
    assert service.chart_x_type(grid, "rides", "line") == "quantitative"
    assert service.chart_x_type(grid, "day", "bar") == "temporal" and service.chart_x_type(grid, "city",
                                                                                           "line") == "nominal"


def test_the_helpers_of_a_page_say_what_it_needs():
    settings = service.Settings(configs=("/a", "/b"), catalogs=("/c",))
    assert service.sources_text(settings, 3) == "3 tables from 2 configs and 1 catalog"
    assert service.sources_text(service.Settings(configs=("/a",)), 1) == "1 table from 1 config"
    assert service.sources_text(service.Settings(), 0) == "0 tables from no source"
    assert service.facts_rows([("Rows", "5")]) == [{"what": "Rows", "value": "5"}]
    assert service.overview_rows([{"table": "a.b", "row_count": 4, "access": "native"}]) == [{
        "table": "a.b", "layer": "-", "owner": "-", "rows": 4, "last updated": "-", "meaning": "-",
        "read as": "native"}]
    assert service.default_output(service.ReportItem("/x/y.json", "daily sales/1", "t")) == "daily_sales_1.html"
    assert service.default_output(None) == "report.html"
    assert service.WINDOWS == ("yesterday", "last-week", "last-month")


def test_a_report_result_is_ok_when_it_has_a_page_and_no_problem():
    assert service.ReportResult("r", None, html="<html>").ok
    assert not service.ReportResult("r", None).ok
    assert not service.ReportResult("r", None, html="<html>", problems=["a problem"]).ok


def test_a_report_in_the_list_is_named_by_its_title_and_its_file_and_a_broken_one_says_so():
    assert service.ReportItem("/r/a.json", "a", "A report").label == "A report (a.json)"
    assert service.ReportItem("/r/a.json", "a", "a").label == "a"
    assert service.ReportItem("/r/a.json", None, None, ["kind: nope"]).label == "a.json - has problems"
    assert service.ReportItem("/r/a.json", "a", "a", ["x"]).label == "a - has problems"


# --------------------------------------------------------------------------- the Tables page


def test_the_tables_page_lists_the_table_with_what_it_says_about_itself_and_keeps_the_list(dash, demo):
    tables = dash.tables()

    assert [item["table"] for item in tables] == ["demo.rides"]
    assert tables[0]["row_count"] == demo.rows and tables[0]["access"] in ("native", "arrow")
    assert dash.tables() is tables, "the list opens every table, so it is read once"
    assert dash.tables(refresh=True) is not tables
    assert tools_of(dash) == ["dashboard.list_tables", "dashboard.list_tables"]


def test_a_table_is_shown_with_its_schema_its_first_rows_and_its_snapshots_newest_first(dash, demo):
    view = dash.table_view("demo.rides")

    assert view.problems == [] and view.table == "demo.rides"
    facts = dict(view.facts)
    assert facts["Rows"] == str(demo.rows) and facts["Name in SQL"] == '"demo"."rides"'
    assert facts["Snapshots"] == str(demo.snapshots) and facts["Location"].endswith("demo/rides")
    assert "Layer" not in facts and "Owner" not in facts, "a table that says nothing about itself has no such line"
    assert [item["name"] for item in view.schema] == ["ride_id", "pickup_ts", "city", "distance_km", "fare"]
    assert {item["name"]: item["type"] for item in view.schema}["fare"] == "double"
    assert view.sample is not None and len(view.sample.rows) == service.SAMPLE_ROWS
    assert set(view.sample.columns) == {"ride_id", "pickup_ts", "city", "distance_km", "fare"}
    assert len(view.history) == demo.snapshots
    assert [item["committed_at"] for item in view.history] == sorted((item["committed_at"] for item in view.history),
                                                                     reverse=True)
    assert [item["current"] for item in view.history] == ["yes"] + [""] * (demo.snapshots - 1)
    assert all(isinstance(item["snapshot_id"], str) and item["snapshot_id"].isdigit() for item in view.history)
    assert view.history[0]["total_records"] == demo.rows
    assert tools_of(dash) == ["dashboard.list_tables", "dashboard.describe_table", "dashboard.sample_rows",
                              "dashboard.table_history"]


def test_a_table_that_is_not_served_is_a_page_with_problems_and_not_an_error(dash):
    view = dash.table_view("demo.secret")

    assert view.facts == [] and view.schema == [] and view.sample is None and view.history == []
    assert [line.split(":")[0] for line in view.problems] == ["describe_table", "sample_rows", "table_history"]
    assert all("not available" in line for line in view.problems)


class FakeTools:
    """Tools that answer from canned data, for what the demo has not got, such as the contract of a table."""

    max_rows, timeout_s, tool_prefix = 500, 30.0, "dashboard."

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def call(self, tool, arguments=None):
        self.calls.append((tool, dict(arguments or {})))
        data = self.answers[tool]
        return data if isinstance(data, ToolOutcome) else ToolOutcome(ok=True, data=data)


def test_the_page_of_a_table_that_has_a_contract_says_what_it_means_and_what_each_column_holds():
    listed = {"table": "gold.rides", "layer": "gold", "owner": "ops", "description": "Rides", "grain": "one ride",
              "row_count": 0, "last_updated": "2026-11-27T09:00:00+00:00", "sql_name": '"gold"."rides"',
              "access": "native"}
    detail = {"snapshot_count": 2, "location": "file:///lake/gold/rides",
              "contract": {"columns": {"fare": "what a rider paid"}},
              "schema": {"fields": [{"id": 1, "name": "fare", "type": "double", "required": False, "doc": None},
                                    {"id": 2, "name": "city", "type": "string", "required": True, "doc": "the city"}]}}
    tools = FakeTools(list_tables={"tables": [listed]}, describe_table=detail,
                      sample_rows={"columns": [{"name": "fare", "type": "double"}], "rows": [[1.5]], "max_rows": 10},
                      table_history={"history": [{"snapshot_id": 8746247374355893574, "is_current": True}]})

    view = service.Dashboard(service.Settings(), tools).table_view("gold.rides")

    assert view.problems == [] and [label for label, _ in view.facts] == [
        "Layer", "Owner", "Meaning", "One row is", "Rows", "Last updated", "Name in SQL", "Read as", "Snapshots",
        "Location"]
    assert dict(view.facts)["Rows"] == "0", "no rows is a fact, and a missing line is not"
    docs = [(item["name"], item["doc"]) for item in view.schema]
    assert docs == [("fare", "what a rider paid"), ("city", "the city")]
    assert view.sample.rows == [{"fare": 1.5}]
    assert view.history == [{"committed_at": "-", "snapshot_id": "8746247374355893574", "operation": "-",
                             "added_records": None, "deleted_records": None, "total_records": None, "current": "yes"}]
    assert [(tool, arguments.get("n"), arguments.get("limit")) for tool, arguments in tools.calls] == [
        ("list_tables", None, None), ("describe_table", None, None), ("sample_rows", service.SAMPLE_ROWS, None),
        ("table_history", None, service.HISTORY_ROWS)]


def test_a_dashboard_with_no_table_to_show_is_an_error(settings, tmp_path):
    with pytest.raises(ConfigError, match="no tables to show"):
        service.Dashboard.open(service.Settings(configs=settings.configs, allow=("nothing.*",)),
                               audit_path=tmp_path / "a.jsonl")


# --------------------------------------------------------------------------- the SQL page


def test_the_sql_page_starts_with_the_first_rows_of_the_first_table(dash):
    assert dash.default_sql() == 'SELECT * FROM "demo"."rides" LIMIT 10'
    assert dash.run_sql(dash.default_sql()).ok


def test_a_query_runs_through_the_tools_and_is_audited_with_its_text(dash, demo):
    sql = "SELECT city, count(*) AS rides FROM demo.rides GROUP BY city ORDER BY city"

    run = dash.run_sql("  " + sql + "\n")

    assert run.ok and run.sql == sql and run.message is None
    assert run.grid.columns == ("city", "rides") and run.grid.types == ("string", "int64")
    assert {row["city"] for row in run.grid.rows} == CITIES and sum(row["rides"] for row in run.grid.rows) == demo.rows
    assert run.grid.truncated is False and run.grid.max_rows == dash.limits["max_rows"]
    record, = dash.tools.audit.records
    assert (record.tool, record.status, record.sql, record.rows) == ("dashboard.query", "ok", sql, 4)
    assert dash.tools.audit.iceberg_enabled is False, "the dashboard audits to the JSONL file and not to the lake"


def test_a_query_gives_as_many_rows_as_the_cap_of_the_tools_and_not_the_default_of_the_tool(dash):
    run = dash.run_sql("SELECT * FROM range(300)")

    assert run.ok and len(run.grid.rows) == 300 and run.grid.truncated is False


def test_the_guard_the_cap_and_the_timeout_of_the_tools_apply_to_the_sql_page(settings, tmp_path, demo):
    with service.Dashboard.open(settings, audit_path=tmp_path / "audit.jsonl", max_rows=7, timeout_s=0.5) as capped:
        assert capped.limits == {"max_rows": 7, "timeout_s": 0.5}
        cut = capped.run_sql("SELECT * FROM demo.rides")
        assert cut.ok and len(cut.grid.rows) == 7 and cut.grid.truncated and cut.grid.max_rows == 7
        write = capped.run_sql('DELETE FROM "demo"."rides"')
        assert (write.status, write.grid) == ("rejected", None) and "read-only" in write.message
        outside = capped.run_sql("SELECT * FROM read_csv('/etc/hosts')")
        assert not outside.ok and outside.status in ("rejected", "denied")
        slow = capped.run_sql("SELECT count(*) FROM range(100000000000) a, range(1000) b")
        assert (slow.status, slow.grid) == ("timeout", None) and "0.5" in slow.message
        again = capped.run_sql("SELECT count(*) AS n FROM demo.rides")
        assert again.ok and again.grid.rows == [{"n": demo.rows}], "the table is as it was"
        assert {record.tool for record in capped.tools.audit.records} == {"dashboard.query"}
        statuses = [record.status for record in capped.tools.audit.records]
        assert statuses == ["ok", "rejected", outside.status, "timeout", "ok"]


def test_a_query_that_is_empty_or_wrong_is_a_result_that_says_why(dash):
    empty = dash.run_sql("   ")
    assert (empty.status, empty.ok) == ("error", False) and "no query" in empty.message
    assert dash.run_sql(None).status == "error"
    wrong = dash.run_sql("SELECT nope FROM demo.rides")
    assert not wrong.ok and wrong.grid is None and wrong.message
    assert tools_of(dash) == ["dashboard.query"], "an empty query is not sent"


# --------------------------------------------------------------------------- the Reports page


def test_the_reports_page_lists_the_files_and_what_is_wrong_with_a_broken_one(dash, demo):
    items = dash.reports()

    assert [Path(item.path).name for item in items] == ["broken.json", "rides_by_city.json", "rides_with_events.json"]
    broken, ok, with_events = items
    assert broken.identifier is None and broken.problems and broken.label == "broken.json - has problems"
    assert (ok.identifier, ok.title, ok.problems, ok.label) == ("rides_by_city", "Rides by city",
                                                                [], "Rides by city (rides_by_city.json)")
    assert with_events.identifier == "rides_with_events"


def test_there_is_no_report_to_list_without_a_folder_and_a_folder_that_is_not_there_is_an_error(settings, tmp_path):
    with service.Dashboard.open(service.Settings(configs=settings.configs), audit_path=tmp_path / "a.jsonl") as without:
        assert without.reports() == []
    gone = service.Settings(configs=settings.configs, reports=str(tmp_path / "gone"))
    with service.Dashboard.open(gone, audit_path=tmp_path / "b.jsonl") as missing:
        with pytest.raises(ConfigError, match="report path not found"):
            missing.reports()


def test_a_preview_is_the_page_of_ldp_report_build_and_writes_nothing_and_records_no_event(dash, demo):
    path = str(demo.reports / "rides_with_events.json")
    events_file = demo.reports / "events.jsonl"
    before = sorted(item.name for item in demo.reports.iterdir())
    events_before = events_file.read_text() if events_file.exists() else None

    result = dash.preview_report(path, "")

    assert result.ok and result.problems == [] and result.held is False and result.output is None
    assert "<html" in result.html[:300].lower() and "Rides by city" in result.html and "SFO" in result.html
    assert (result.bytes == len(result.html.encode())
            and result.sha256 == hashlib.sha256(result.html.encode()).hexdigest())
    labels = [label for label, _ in result.facts]
    assert labels[0] == "Report" and "Read demo.rides" in labels and labels[-1] == "Built"
    assert sorted(item.name for item in demo.reports.iterdir()) == before, "no page and no event file"
    assert (events_file.read_text() if events_file.exists() else None) == events_before, "no event"
    record, = dash.tools.audit.records
    assert (record.tool, record.status, record.arguments) == (
        "dashboard.report", "ok", {"report": "rides_with_events", "window": None, "written": False})


def test_writing_a_page_is_ldp_report_build_with_an_out_and_records_the_build_as_run_events(dash, demo, tmp_path):
    out = tmp_path / "pages" / "rides.html"
    path = demo.reports / "rides_with_events.json"

    result = dash.write_report(str(path), "last-week", out)

    assert result.ok and result.output == str(out) and result.window == "last-week"
    data = out.read_bytes()
    assert result.bytes == len(data) and result.sha256 == hashlib.sha256(data).hexdigest()
    assert data.decode() == result.html and b"Rides by city" in data
    events = [json.loads(line) for line in (demo.reports / "events.jsonl").read_text().splitlines()]
    rendered = [event for event in events if event["type"] == "report.rendered"]
    assert (rendered and rendered[-1]["payload"]["output"] == str(out)
            and rendered[-1]["payload"]["sha256"] == result.sha256)
    record, = dash.tools.audit.records
    assert record.arguments == {"report": "rides_with_events", "window": "last-week", "written": True}


def test_a_window_is_read_as_ldp_reads_it_and_a_bad_one_is_a_problem_and_not_an_error(dash, demo):
    path = str(demo.reports / "rides_by_city.json")

    named = dash.preview_report(path, " 2024-01-01/2024-01-04 ")
    bad = dash.preview_report(path, "the day before")

    assert named.ok and named.window == "2024-01-01/2024-01-04"
    assert dict(named.facts)["Window"] == "2024-01-01T00:00:00+00:00 to 2024-01-04T00:00:00+00:00"
    assert not bad.ok and bad.html is None and bad.held is False and "window" in " ".join(bad.problems)
    assert [(record.status, record.arguments["window"]) for record in dash.tools.audit.records] == [
        ("ok", "2024-01-01/2024-01-04"), ("error", "the day before")]


def test_a_report_that_cannot_be_read_is_a_problem_in_every_form_of_the_call(dash, demo, tmp_path):
    for path in (demo.reports / "broken.json", tmp_path / "missing.json"):
        shown = dash.preview_report(str(path), "")
        assert not shown.ok and shown.html is None and shown.problems
    out = tmp_path / "never.html"
    written = dash.write_report(str(demo.reports / "broken.json"), "", out)
    assert not written.ok and written.problems and not out.exists()
    assert [record.status for record in dash.tools.audit.records] == ["error"] * 3


def test_a_report_that_is_held_back_by_a_stale_table_says_so_and_writes_nothing(settings, tmp_path, demo):
    held = json.loads(json.dumps(REPORT))
    held["identifier"] = "rides_held"
    held["metadata"]["freshness"] = {"max_age_hours": 0.0001, "on_stale": "block"}
    path = demo.reports / "rides_held.json"
    path.write_text(json.dumps(held))
    time.sleep(0.5)
    try:
        with service.Dashboard.open(settings, audit_path=tmp_path / "audit.jsonl") as served:
            out = tmp_path / "held.html"
            result = served.write_report(str(path), "", out)
            assert result.held and not result.ok and result.html is None and not out.exists()
            assert any("stale" in line and "metadata.freshness" in line for line in result.problems)
    finally:
        path.unlink()


# --------------------------------------------------------------------------- the Health page


def test_the_health_page_is_the_rows_of_ldp_status_with_the_worst_first(dash):
    page = dash.health_rows()

    assert page.problems == [] and page.counts == {"no sla": 1}
    row, = page.rows
    assert (row["health"], row["pipeline"], row["table"], row["rows"]) == ("no sla", "demo_rides", "demo.rides", 50)
    assert list(row)[:2] == ["health", "pipeline"] and row["problem"] == ""
    assert tools_of(dash) == [], "the health of a pipeline is read from its events, and it is no query"


def test_the_pipelines_of_every_config_are_in_one_list_with_the_worst_first_and_counted(monkeypatch):
    def row(pipeline, health, problem=None):
        return types.SimpleNamespace(pipeline=pipeline, health=health, problem=problem,
                                     cells=lambda: {"pipeline": pipeline, "rows": 2**60})

    found = {"/a": [row("b", "ok"), row("z", "warn")], "/b": [row("a", "error", "it broke"), row("c", "ok")]}
    monkeypatch.setattr(service.status, "rows", lambda path: found[path])

    page = service.Dashboard(service.Settings(configs=("/a", "/b")), types.SimpleNamespace()).health_rows()

    assert [item["pipeline"] for item in page.rows] == ["a", "z", "b", "c"]
    assert [item["health"] for item in page.rows] == ["error", "warn", "ok", "ok"]
    assert [item["problem"] for item in page.rows] == ["it broke", "", "", ""]
    assert page.counts == {"error": 1, "warn": 1, "ok": 2} and list(page.counts) == ["error", "warn", "ok"]
    assert page.rows[0]["rows"] == str(2**60), "a number that a browser would round is text"


def test_a_config_that_cannot_be_read_is_a_line_on_the_health_page(settings, tmp_path, demo):
    both = service.Settings(configs=(*settings.configs, str(tmp_path / "gone.json")))

    page = service.Dashboard(both, types.SimpleNamespace()).health_rows()

    assert [row["pipeline"] for row in page.rows] == ["demo_rides"]
    assert len(page.problems) == 1 and "gone.json" in page.problems[0]
    none = service.Dashboard(service.Settings(catalogs=("/spec.json",)), types.SimpleNamespace()).health_rows()
    assert none.rows == [] and "start the dashboard with --config" in none.problems[0]


# --------------------------------------------------------------------------- the command


def _args(*argv):
    parser = ldp_cli.build_parser()
    return parser.parse_args(["dashboard", *argv])


def test_the_command_is_registered_and_its_help_says_where_it_listens(capsys):
    assert ldp_cli.main(["dashboard", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())  # argparse wraps the help
    for word in ("127.0.0.1", "--config", "--catalog", "--allow", "--reports", "--port", "--no-browser", "--edit",
                 INSTALL_HINT):
        assert word in text
    assert "--host" not in text, "no option opens the server to the network"
    assert ldp_cli.main(["--help"]) == 0 and "dashboard" in capsys.readouterr().out


def test_marimo_is_told_to_listen_on_127_0_0_1_and_nowhere_else_and_not_to_ask_or_check_for_updates():
    command = dashboard_cli.marimo_command(APP)

    assert command[:4] == [sys.executable, "-m", "marimo", "run"] and command[4] == str(APP)
    assert command.count("--host") == 1 and command[command.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in command and "--headless" not in command and "--port" not in command
    assert "--no-sandbox" in command and command[command.index("--config") + 1] == "server.skip_update_check=true"
    assert dashboard_cli.HOST == "127.0.0.1"


def test_the_options_of_the_command_become_the_options_of_marimo():
    assert dashboard_cli.marimo_command(APP, headless=True, port=2718)[-3:] == ["--headless", "--port", "2718"]
    editing = dashboard_cli.marimo_command("/x/ldp_dashboard.py", edit=True)
    assert editing[3:5] == ["edit", "/x/ldp_dashboard.py"] and editing[editing.index("--host") + 1] == "127.0.0.1"


def test_without_the_extra_the_command_prints_how_to_install_it_and_exits_with_1(demo, monkeypatch, capsys):
    monkeypatch.setattr(dashboard_cli, "missing_packages", lambda: ["marimo", "altair"])
    monkeypatch.setattr(dashboard_cli, "launch", lambda *args: pytest.fail("nothing is started without the extra"))

    assert ldp_cli.main(["dashboard", "--config", str(demo.config)]) == 1

    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.startswith("ldp: error: ldp dashboard needs marimo and altair")
    assert INSTALL_HINT in err


def test_the_check_for_the_extra_names_what_is_missing_whatever_is_installed(monkeypatch):
    present = object()

    def without(*missing):
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *args: None if name in missing else present)
        return dashboard_cli.missing_packages()

    assert without("altair") == ["altair"] and without("marimo") == ["marimo"]
    assert without("marimo", "altair") == ["marimo", "altair"] and without() == []


@pytest.fixture
def started(monkeypatch):
    """Run the command without marimo: what it would start is kept, and the exit status of marimo is chosen."""
    monkeypatch.setattr(dashboard_cli, "missing_packages", lambda: [])
    run = types.SimpleNamespace(calls=[], status=0)

    def launch(command, environment):
        run.calls.append((list(command), dict(environment)))
        return run.status

    monkeypatch.setattr(dashboard_cli, "launch", launch)
    return run


def test_the_command_starts_marimo_on_the_notebook_of_the_package_with_the_settings_in_the_environment(
        demo, started, capsys, monkeypatch):
    monkeypatch.delenv(service.SETTINGS_ENV, raising=False)

    status = ldp_cli.main(["dashboard", "--config", str(demo.config), "--reports", str(demo.reports), "--allow",
                           "demo.*", "--allow", " ", "--port", "4321", "--no-browser"])

    assert status == 0
    (command, environment), = started.calls
    assert command == dashboard_cli.marimo_command(APP, headless=True, port=4321)
    assert service.Settings.from_json(environment[service.SETTINGS_ENV]) == service.Settings(
        configs=(str(demo.config.resolve()),), allow=("demo.*",), reports=str(demo.reports.resolve()))
    assert os.environ.get(service.SETTINGS_ENV) is None, "the settings are for the child and not for this process"
    err = capsys.readouterr().err
    assert "1 table from 1 config" in err and "127.0.0.1" in err and str(demo.reports.resolve()) in err


def test_a_relative_path_is_made_absolute_so_that_the_notebook_finds_it_from_any_folder(demo, started, monkeypatch):
    monkeypatch.chdir(demo.workdir.parent)

    assert ldp_cli.main(["dashboard", "--config", "ldp_demo/../ldp_demo/rides.json", "--reports",
                         "./ldp_demo/reports"]) == 0

    (_, environment), = started.calls
    saved = service.Settings.from_json(environment[service.SETTINGS_ENV])
    assert saved.configs == (str(demo.config.resolve()),) and saved.reports == str(demo.reports.resolve())
    assert all(Path(path).is_absolute() and ".." not in Path(path).parts for path in (*saved.configs, saved.reports))


def test_marimo_that_fails_to_start_is_an_error_with_its_status(demo, started, capsys):
    started.status = 3

    assert ldp_cli.main(["dashboard", "--config", str(demo.config)]) == 1

    assert "marimo stopped with exit status 3" in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    ([], "needs at least one --config or --catalog"),
    (["--config", "{gone}"], "config not found"),
    (["--config", "{config}", "--reports", "{gone}"], "reports folder not found"),
    (["--catalog", "{gone}"], "catalog spec not found"),
    (["--config", "{config}", "--allow", "nothing.*"], "no tables to show"),
])
def test_a_mistake_in_the_options_is_an_error_in_the_terminal_and_marimo_is_not_started(
        demo, started, capsys, tmp_path, argv, message):
    argv = [item.format(config=demo.config, gone=tmp_path / "gone") for item in argv]

    assert ldp_cli.main(["dashboard", *argv]) == 1

    err = capsys.readouterr().err
    assert message in err and err.startswith("ldp: error: ")
    assert started.calls == []


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_a_port_has_to_be_a_port(demo, started, port):
    assert ldp_cli.main(["dashboard", "--config", str(demo.config), "--port", port]) == 1
    assert started.calls == []


def test_edit_opens_a_copy_of_the_notebook_that_is_made_once_and_never_overwritten(demo, started, tmp_path,
                                                                                   monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert ldp_cli.main(["dashboard", "--config", str(demo.config), "--edit"]) == 0

    copy = tmp_path / dashboard_cli.EDIT_COPY
    (command, _), = started.calls
    assert command[3:5] == ["edit", str(copy)] and copy.read_text() == APP.read_text()
    copy.write_text("# my notebook\n")
    assert ldp_cli.main(["dashboard", "--config", str(demo.config), "--edit"]) == 0
    assert copy.read_text() == "# my notebook\n" and started.calls[-1][0][3:5] == ["edit", str(copy)]
    assert ldp_cli.main(["dashboard", "--config", str(demo.config)]) == 0
    assert started.calls[-1][0][3:5] == ["run", str(APP)], "without --edit the notebook of the package is served"


def _default_sigint() -> None:
    """What a person has: a job that a script starts in the background has SIGINT ignored, and so has what it starts."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def test_launch_gives_back_the_exit_status_of_what_it_ran():
    code = [sys.executable, "-c", "import os, sys; sys.exit(int(os.environ['STATUS']))"]

    assert dashboard_cli.launch(code, {**os.environ, "STATUS": "0"}) == 0
    assert dashboard_cli.launch(code, {**os.environ, "STATUS": "3"}) == 3


@pytest.mark.skipif(sys.platform == "win32", reason="Ctrl-C is a signal to the process group")
def test_ctrl_c_stops_marimo_and_the_command_says_so_and_exits_with_0(tmp_path):
    child = ("import signal, sys, time; signal.signal(signal.SIGINT, lambda *args: sys.exit(0)); "
             "print('marimo is up', flush=True); time.sleep(60)")
    code = ("import os, sys; from local_data_platform.dashboard import cli; "
            f"sys.exit(cli.launch([sys.executable, '-c', {child!r}], dict(os.environ)))")
    log = tmp_path / "launch.log"
    with log.open("w") as stream:
        process = subprocess.Popen([sys.executable, "-c", code], stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=True, preexec_fn=_default_sigint)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and "marimo is up" not in log.read_text():
            time.sleep(0.1)
        assert "marimo is up" in log.read_text()
        os.killpg(process.pid, signal.SIGINT)  # the terminal sends Ctrl-C to the command and to marimo, as this does
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    assert process.returncode == 0 and "ldp dashboard: stopped" in log.read_text()


def test_the_command_needs_the_standard_library_only_when_it_is_imported():
    code = ("import sys; import local_data_platform.dashboard.cli; "
            "print(sorted(name for name in ('marimo', 'altair', 'duckdb') if name in sys.modules))")

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stderr


# --------------------------------------------------------------------------- the notebook, run headless


def load_app():
    spec = importlib.util.spec_from_file_location("ldp_dashboard_app", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


@pytest.fixture
def notebook(settings, monkeypatch, tmp_path):
    """Run the notebook with marimo's ``App.run()``, with the settings in the environment, and close what it opened.

    It runs in a folder of its own, because the page writes a file relative to the folder that it was started in.
    """
    monkeypatch.setenv(service.SETTINGS_ENV, settings.to_json())
    monkeypatch.chdir(tmp_path)
    opened = []

    def run(**overrides):
        outputs, defs = load_app().run(defs=overrides or None)
        if defs.get("dash") is not None:
            opened.append(defs["dash"])
        texts = [item.text for item in outputs if getattr(item, "text", None)]
        return texts[-1] if texts else "", defs

    yield run
    for item in opened:
        item.close()


@requires_notebook
def test_the_notebook_runs_headless_and_its_page_has_a_tab_for_each_page(notebook, demo):
    page, defs = notebook()

    for text in ("Local Data Platform", "1 table from 1 config", "dashboard.", "Tables", "SQL", "Reports", "Health"):
        assert text in page
    assert page.count("<marimo-tabs") == 1 and defs["settings"].configs == (str(demo.config),)
    assert defs["tables"] == defs["dash"].tables()


@requires_notebook
def test_the_tables_page_shows_the_list_the_schema_the_rows_and_the_snapshots(notebook, demo):
    page, defs = notebook()

    for text in ("demo.rides", "pickup_ts", "distance_km", "First 10 rows", "Snapshots, the newest first", "overwrite",
                 "append", defs["tables"][0]["access"]):
        assert text in page
    assert defs["selected_table"] == "demo.rides" and defs["table_view"].problems == []
    assert len(defs["table_view"].history) == demo.snapshots
    other, defs = notebook(selected_table="demo.secret")
    assert "not available" in other and defs["table_view"].facts == []


@requires_notebook
def test_the_sql_page_waits_for_a_query_and_then_shows_the_table_and_a_chart_of_the_result(notebook, demo):
    waiting, defs = notebook()
    assert "Run the query" in waiting and "The result stays until the next query is run" in waiting
    assert defs["sql_to_run"] is None and defs["sql_run"] is None and defs["chart"] is None

    page, defs = notebook(sql_to_run="SELECT city, count(*) AS rides FROM demo.rides GROUP BY city ORDER BY city")

    assert defs["sql_run"].ok and len(defs["sql_run"].grid.rows) == 4
    assert (defs["chart_kind"].value, defs["chart_x"].value, defs["chart_y"].value) == ("bar", "city", "rides")
    assert "4 rows in" in page and "Horizontal" in page and "Vertical" in page
    assert "vega-embed" in page and all(city in page for city in CITIES)
    assert defs["chart"].to_dict()["mark"]["type"] == "bar"
    assert [item["y"] for item in defs["chart"].to_dict()["data"]["values"]] == [
        row["rides"] for row in defs["sql_run"].grid.rows]


@requires_notebook
def test_the_query_that_the_form_sends_is_the_query_that_runs(notebook, demo):
    import marimo

    sql = "SELECT count(*) AS n FROM demo.rides"

    page, defs = notebook(sql_form=marimo.ui.text(value=sql))

    assert defs["sql_to_run"] == sql and defs["sql_run"].ok and defs["sql_run"].grid.rows == [{"n": demo.rows}]
    assert "1 rows in" in page


@requires_notebook
def test_the_tables_page_starts_with_the_first_table_of_the_list(notebook):
    page, defs = notebook(tables=[{"table": "demo.rides", "row_count": 1}, {"table": "demo.zzz", "row_count": 2}])

    assert defs["selected_table"] == "demo.rides" and defs["table_view"].table == "demo.rides"
    assert "demo.zzz" in page


@requires_notebook
@pytest.mark.parametrize("window, typed, wanted", [
    ("no window", "", ""), ("last-week", "", "last-week"),
    ("yesterday", "  2024-01-01/2024-01-04 ", "2024-01-01/2024-01-04")])
def test_the_report_and_the_window_of_the_controls_are_what_the_report_is_built_for(notebook, demo, window, typed,
                                                                                    wanted):
    import marimo

    path = str(demo.reports / "rides_by_city.json")
    controls = {"report_items": [service.ReportItem(path, "rides_by_city", "Rides by city")],
                "report_picker": marimo.ui.dropdown(options={"Rides by city": path}, value="Rides by city"),
                "reports_problem": None,
                "window_picker": marimo.ui.dropdown(options=["no window", *service.WINDOWS], value=window),
                "window_text": marimo.ui.text(value=typed)}

    page, defs = notebook(**controls)

    assert (defs["report_choice"], defs["window_choice"]) == (path, wanted)
    assert defs["report_result"].ok and defs["report_result"].window == (wanted or None) and "<iframe" in page


@requires_notebook
def test_the_sql_page_has_no_chart_for_a_result_with_no_number_and_says_so(notebook):
    page, defs = notebook(sql_to_run="SELECT city FROM demo.rides GROUP BY city")

    assert defs["sql_run"].ok and defs["chart"] is None and "The result has no number to chart" in page
    assert "vega-embed" not in page


@requires_notebook
def test_a_statement_that_the_guard_refuses_is_a_message_on_the_page_and_the_table_is_untouched(notebook, demo):
    page, defs = notebook(sql_to_run='DELETE FROM "demo"."rides"')

    assert not defs["sql_run"].ok and defs["sql_run"].status == "rejected"
    assert "The query did not run" in page and "rejected" in page and "read-only" in page
    again, defs = notebook(sql_to_run="SELECT count(*) AS n FROM demo.rides")
    assert defs["sql_run"].grid.rows == [{"n": demo.rows}]


@requires_notebook
def test_the_reports_page_asks_for_a_report_and_then_shows_the_page_of_the_build(notebook, demo):
    asking, defs = notebook()
    assert "Pick a report" in asking and defs["report_result"] is None
    assert (defs["report_choice"], defs["window_choice"]) == (None, "yesterday")
    assert (defs["write_requested"], defs["write_path"]) == (False, "report.html")
    assert [item.label for item in defs["report_items"]][1] == "Rides by city (rides_by_city.json)"

    path = str(demo.reports / "rides_by_city.json")
    page, defs = notebook(report_choice=path, window_choice="")

    assert defs["report_result"].ok and "<iframe" in page and "Rides by city" in page and "Read demo.rides" in page
    assert defs["write_result"] is None and "Write the HTML file" in page


@requires_notebook
def test_the_reports_page_writes_the_html_file_with_the_code_of_ldp_report_build(notebook, demo, tmp_path):
    path = str(demo.reports / "rides_by_city.json")
    out = tmp_path / "written.html"

    page, defs = notebook(report_choice=path, window_choice="yesterday", write_requested=True, write_path=str(out))

    result = defs["write_result"]
    assert result.ok and result.output == str(out) and out.read_text() == result.html
    assert f"Wrote {result.bytes} bytes to" in page and result.sha256 in page
    not_written, defs = notebook(report_choice=path, window_choice="", write_requested=False, write_path=str(out))
    assert defs["write_result"] is None


@requires_notebook
def test_a_report_with_a_problem_is_a_message_on_the_page_and_not_an_error(notebook, demo, tmp_path):
    page, defs = notebook(report_choice=str(demo.reports / "broken.json"), window_choice="")

    assert not defs["report_result"].ok and "The report was not built" in page and "<iframe" not in page
    bad, defs = notebook(report_choice=str(demo.reports / "rides_by_city.json"), window_choice="the day before")
    assert "The report was not built" in bad and not defs["report_result"].ok


@requires_notebook
def test_the_reports_page_says_what_to_do_without_a_folder_and_when_the_folder_is_not_there(settings, notebook,
                                                                                            monkeypatch, tmp_path):
    monkeypatch.setenv(service.SETTINGS_ENV, service.Settings(configs=settings.configs).to_json())
    page, _ = notebook()
    assert "There is no report to show" in page and "--reports DIR" in page

    monkeypatch.setenv(service.SETTINGS_ENV,
                       service.Settings(configs=settings.configs, reports=str(tmp_path / "gone")).to_json())
    page, defs = notebook()
    assert "The reports could not be listed" in page and "report path not found" in page and defs["report_items"] == []


@requires_notebook
def test_the_health_page_shows_the_pipelines_of_the_configs(notebook):
    page, defs = notebook()

    assert "1 pipeline, 1 no sla" in page and "demo_rides" in page and "The rows of" in page
    assert defs["health"].rows[0]["pipeline"] == "demo_rides" and defs["health"].counts == {"no sla": 1}


@requires_notebook
def test_a_notebook_that_cannot_start_says_why_on_the_page_and_shows_no_page(demo, monkeypatch):
    monkeypatch.delenv(service.SETTINGS_ENV, raising=False)

    outputs, defs = load_app().run()

    text = "\n".join(item.text for item in outputs if getattr(item, "text", None))
    assert "The dashboard could not start" in text and service.SETTINGS_ENV in text
    assert defs.get("dash") is None and "tables_page" not in defs


@requires_notebook
def test_every_query_of_the_notebook_goes_through_the_tools_and_is_audited_as_the_dashboard(notebook, demo):
    notebook(sql_to_run="SELECT count(*) AS n FROM demo.rides")
    notebook(sql_to_run='DELETE FROM "demo"."rides"')
    notebook(report_choice=str(demo.reports / "rides_by_city.json"), window_choice="")

    log = demo.workdir / "warehouse" / ".ldp" / "audit" / "mcp_audit.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    tools = {record["tool"] for record in records}
    assert tools >= {"dashboard.list_tables", "dashboard.describe_table", "dashboard.sample_rows",
                     "dashboard.table_history", "dashboard.query", "dashboard.report"}
    assert all(tool.startswith("dashboard.") for tool in tools), tools
    statuses = [record["status"] for record in records if record["tool"] == "dashboard.query"]
    assert "ok" in statuses and "rejected" in statuses


# --------------------------------------------------------------------------- the real server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@requires_notebook
@pytest.mark.skipif(sys.platform == "win32", reason="the server is stopped with a signal to its process group")
def test_ldp_dashboard_starts_the_server_on_127_0_0_1_and_stops_with_ctrl_c(demo, tmp_path):
    port = _free_port()
    log = tmp_path / "server.log"
    with log.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "local_data_platform.cli", "dashboard", "--config", str(demo.config), "--reports",
             str(demo.reports), "--no-browser", "--port", str(port)], stdout=stream, stderr=subprocess.STDOUT,
            cwd=tmp_path, start_new_session=True, preexec_fn=_default_sigint)
    try:
        answers = {}
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and process.poll() is None and "/" not in answers:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    answers["/"] = (response.status, response.read().decode())
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        assert "/" in answers, log.read_text()
        status, body = answers["/"]
        assert status == 200 and "Local Data Platform" in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            assert response.status == 200
    finally:
        try:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=30)
        except ProcessLookupError:
            process.wait()
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    text = log.read_text()
    assert "ldp dashboard: 1 table from 1 config" in text and "ldp dashboard: stopped" in text
    assert process.returncode == 0
