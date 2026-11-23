"""Report specs: what a report file says, and the checks that need no table.

A report file has ``"kind": "Report"``. It names the tables a report reads, its queries and its blocks. These tests
cover the kind in the registry, every check of the shape of the file, the SQL files with their ``-- name:`` lines, the
files of a folder, ``ldp plan`` and ``ldp schema`` for a report, and that the readers of dataset configs leave a report
alone. They read no table, so they need no DuckDB.
"""

import copy
import json
from pathlib import Path

import pytest

from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError
from local_data_platform.mcp_server.discovery import config_files
from local_data_platform.report import spec as report_spec
from local_data_platform.report.spec import (
    BLOCK_TYPES,
    BUILTIN_PARAMETERS,
    KIND,
    Block,
    build,
    describe_report,
    load_report,
    named_queries,
    read_report,
    report_files,
    report_schema,
    text_references,
    validate_report,
)
from local_data_platform.spec import RESERVED_KINDS, kind_of, kind_of_file, kind_schema, plan, validate_spec

REPORT = {
    "apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily_sales", "who": "owner@example.com",
    "what": "Orders and revenue of one day", "where": "Slack", "when": "every morning", "how": "from the gold tables",
    "metadata": {
        "title": "Daily sales",
        "tables": {"config": ["../config"], "allow": ["gold.*"]},
        "queries": {
            "by_city": {"sql": "SELECT city, sum(revenue) AS revenue FROM gold_sales GROUP BY 1",
                        "description": "Revenue by city."},
            "top": {"sql_file": "queries.sql"},
        },
        "parameters": {"top_n": {"type": "integer", "default": 5, "description": "How many to show."}},
        "blocks": [
            {"type": "kpi", "title": "Revenue", "metric": "revenue", "grain": "day", "format": "currency:USD"},
            {"type": "kpi", "query": "by_city", "value": "revenue", "compare": "revenue"},
            {"type": "bar", "title": "By city", "query": "by_city", "x": "city", "y": "revenue", "series": "city"},
            {"type": "bar", "metric": "revenue", "by": ["city"], "filters": ["cuisine=Thai"]},
            {"type": "line", "metric": "revenue", "grain": "month", "by": "city"},
            {"type": "table", "query": "top", "columns": ["cuisine", "n"], "formats": {"n": "integer"}, "max_rows": 10},
            {"type": "text", "text": "Revenue was {by_city.revenue}. Use {{braces}} for a brace."},
        ],
        "timezone": "Asia/Kolkata", "row_cap": 5000, "timeout_s": 12.5,
        "freshness": {"max_age_hours": 26, "on_stale": "block"},
        "delivery": {"team": {"type": "slack", "webhook_env": "SLACK_WEBHOOK_URL", "retries": 2},
                     "hook": {"type": "webhook", "url_env": "REPORT_URL", "headers_env": "REPORT_HEADERS"},
                     "archive": {"type": "file", "path": "out/daily.html", "public_url": "https://example.com/daily"}},
        "after": ["orders", "menu_items"],
        "observability": {"sinks": ["jsonl"]},
    },
}
SQL_FILE = """-- Research queries.

-- name: top
-- the best sellers
SELECT cuisine, count(*) AS n
FROM gold_sales
GROUP BY 1
ORDER BY 2 DESC
LIMIT $top_n;
"""


def changed(**top) -> dict:
    """The report with some top-level keys replaced. A value of ``None`` removes the key."""
    spec = copy.deepcopy(REPORT)
    for key, value in top.items():
        if value is None:
            spec.pop(key, None)
        else:
            spec[key] = value
    return spec


def with_metadata(**changes) -> dict:
    """The report with some keys of ``metadata`` replaced. A value of ``None`` removes the key."""
    spec = copy.deepcopy(REPORT)
    for key, value in changes.items():
        if value is None:
            spec["metadata"].pop(key, None)
        else:
            spec["metadata"][key] = value
    return spec


def with_block(block: dict, *, replace: int | None = None) -> dict:
    """The report with one more block, or with block ``replace`` replaced."""
    spec = copy.deepcopy(REPORT)
    if replace is None:
        spec["metadata"]["blocks"].append(block)
    else:
        spec["metadata"]["blocks"][replace] = block
    return spec


def messages(spec) -> list[str]:
    return [str(error) for error in validate_spec(spec)]


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


@pytest.fixture
def folder(tmp_path):
    """A folder with a valid report and its SQL file."""
    write(tmp_path / "queries.sql", SQL_FILE)
    write(tmp_path / "daily.json", REPORT)
    return tmp_path


# --------------------------------------------------------------------------- the kind


def test_the_report_of_these_tests_is_valid_and_has_the_kind_report():
    assert messages(REPORT) == [] and validate_report(REPORT) == []
    assert KIND == "Report" and kind_of(REPORT) == "Report"
    assert BLOCK_TYPES == ("kpi", "bar", "line", "table", "text") and BUILTIN_PARAMETERS == ("window_start",
                                                                                             "window_end")


def test_the_report_kind_is_registered_and_no_longer_reserved():
    assert "Report" not in RESERVED_KINDS and "Profiles" in RESERVED_KINDS
    assert messages(changed(kind="report"))[0].startswith("kind: unknown kind 'report'; did you mean 'Report'?")
    assert "Report" in messages(changed(kind="Banana"))[0]


def test_a_report_is_told_from_the_other_files_of_a_folder(folder):
    assert kind_of_file(folder / "daily.json") == "Report"
    assert kind_of_file(write(folder / "pipeline.json", {"identifier": "p"})) == "Pipeline"
    assert [item.name for item in report_files(folder)] == ["daily.json"]
    assert report_files(folder / "daily.json") == [folder / "daily.json"]


def test_a_file_of_another_kind_in_a_folder_is_not_a_report_and_a_missing_path_is_an_error(tmp_path):
    write(tmp_path / "workflow.json", {"kind": "Workflow", "identifier": "w", "tasks": []})
    write(tmp_path / "broken.json", "{not json")

    assert report_files(tmp_path) == []
    with pytest.raises(ConfigError, match="report path not found"):
        report_files(tmp_path / "nope")


def test_the_readers_of_dataset_configs_leave_a_report_alone(folder):
    write(folder / "orders.json", {"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "ICEBERG", "catalog": {"identifier": "gold", "warehouse_path": "w"}},
        "target": {"name": "orders", "format": "ICEBERG", "catalog": {"identifier": "gold", "warehouse_path": "w"}}}})

    assert [item.name for item in config_files(folder)] == ["orders.json"]


# --------------------------------------------------------------------------- ldp plan and ldp schema


def test_ldp_plan_validates_a_report_file_as_a_report_and_names_what_is_in_it(folder, capsys):
    assert main(["plan", str(folder / "daily.json")]) == 0

    fields = {line.split()[0]: line.split(None, 1)[1].strip() for line in capsys.readouterr().out.splitlines()}
    assert fields["kind"] == "Report" and fields["identifier"] == "daily_sales"
    assert fields["queries"] == "by_city, top"
    assert fields["blocks"].startswith("kpi revenue, kpi by_city, bar by_city, bar revenue, line revenue, table top, "
                                       "text")
    assert fields["parameters"] == "window_start, window_end, top_n" and fields["timezone"] == "Asia/Kolkata"
    assert "spec_hash" not in fields and "route" not in fields


def test_ldp_plan_of_a_folder_lists_the_report_with_the_other_kinds_and_json_has_what_it_describes(folder, capsys):
    assert main(["plan", str(folder), "--json"]) == 0

    [item] = [entry for entry in json.loads(capsys.readouterr().out) if entry["kind"] == "Report"]
    assert (item["identifier"], item["valid"], item["errors"]) == ("daily_sales", True, [])
    assert item["queries"] == ["by_city", "top"] and item["timezone"] == "Asia/Kolkata"
    assert item["route"] is None and item["spec_hash"] is None and item["target"] is None


def test_ldp_plan_reports_every_problem_of_a_report_by_its_path_and_exits_with_1(folder, capsys):
    write(folder / "daily.json", with_block({"type": "bar", "query": "ghost", "x": "city"}))

    assert main(["plan", str(folder / "daily.json")]) == 1

    err = capsys.readouterr().err
    assert "metadata.blocks[7].query: unknown query 'ghost'; the queries are ['by_city', 'top']" in err
    assert "metadata.blocks[7].y: is required and must be the name of a column of the query" in err


def test_ldp_schema_prints_the_schema_of_a_report(capsys):
    assert main(["schema", "--kind", "Report"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == kind_schema("Report") == report_schema()
    assert printed["required"] == ["kind", "identifier", "metadata"]
    assert printed["properties"]["kind"]["const"] == "Report"
    assert printed["properties"]["metadata"]["required"] == ["tables", "queries", "blocks"]
    assert set(printed["$defs"]) == {"query", "parameter", "destination", "block"}


def test_the_report_schema_is_valid_and_agrees_with_the_checks_on_a_good_and_some_bad_reports():
    jsonschema = pytest.importorskip("jsonschema")
    schema = report_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)

    assert not list(validator.iter_errors(REPORT))
    bad = [
        changed(kind="Pipeline"), changed(unknown=1), with_metadata(tables={"allow": ["gold.*"]}),
        with_metadata(queries={"q": {"sql": "SELECT 1", "sql_file": "q.sql"}}),
        with_metadata(queries={"Bad-Name": {"sql": "SELECT 1"}}), with_metadata(blocks=[]),
        with_metadata(parameters={"window_start": {"type": "string"}}),
        with_metadata(parameters={"p": {"type": "decimal"}}), with_metadata(timeout_s=0), with_metadata(row_cap=0),
        with_metadata(freshness={"on_stale": "ignore"}),
        with_metadata(delivery={"x": {"type": "slack"}}), with_metadata(delivery={"x": {"type": "email"}}),
        with_metadata(after=["a", "a"]),
        with_block({"type": "kpi", "query": "by_city", "metric": "revenue", "value": "revenue"}),
        with_block({"type": "kpi", "query": "by_city"}), with_block({"type": "line", "metric": "revenue"}),
        with_block({"type": "bar", "metric": "revenue"}), with_block({"type": "table", "query": "top", "max_rows": 0}),
        with_block({"type": "text"}), with_block({"type": "pie", "query": "top"}),
        with_block({"type": "kpi", "metric": "revenue", "format": "euro"}),
        with_block({"type": "bar", "query": "by_city", "x": "city", "y": "revenue", "max_points": 0}),
        with_block({"type": "line", "query": "by_city", "x": "city", "y": "revenue", "max_points": 10001}),
        with_block({"type": "line", "query": "by_city", "x": "city", "y": "revenue", "max_points": 2.5}),
        with_block({"type": "kpi", "query": "by_city", "value": "revenue", "max_points": 5}),
    ]
    for spec in bad:
        assert list(validator.iter_errors(spec)), spec
        assert messages(spec), spec  # and the checks of ldp say what is wrong with it too


# --------------------------------------------------------------------------- the top level


@pytest.mark.parametrize("spec, expected", [
    (changed(unknown=1), "has unknown keys ['unknown']; allowed: ['apiVersion', 'kind', 'identifier'"),
    (changed(apiVersion="ldp/v2"), "apiVersion: must be 'ldp/v1', got 'ldp/v2'"),
    (changed(identifier=None), "identifier: is required"),
    (changed(identifier="  "), "identifier: must be a non-empty string, got '  '"),
    (changed(who=7), "who: must be a string, got int"),
    (changed(what=["a"]), "what: must be a string, got list"),
    (changed(metadata=None), "metadata: is required and must be an object with tables, queries and blocks"),
    (changed(metadata=[1]), "metadata: is required and must be an object with tables, queries and blocks"),
    (with_metadata(extra=1), "metadata: has unknown keys ['extra']; allowed: ['title', 'tables', 'queries'"),
    (with_metadata(title=""), "metadata.title: must be a non-empty string, got ''"),
])
def test_the_top_level_of_a_report_is_checked(spec, expected):
    assert any(expected in message for message in messages(spec)), messages(spec)


def test_a_spec_that_is_not_an_object_is_one_problem():
    assert messages([1, 2]) == ["spec must be a JSON object: expected a spec dict or a local_data_platform.Config, got "
                                "list"]


# --------------------------------------------------------------------------- tables, queries and parameters


@pytest.mark.parametrize("tables, expected", [
    ("../config", "metadata.tables: is required and must be an object with config, catalog and allow"),
    (None, "metadata.tables: is required and must be an object with config, catalog and allow"),
    ({}, "metadata.tables: needs config, which lists dataset configs or folders of them, or catalog"),
    ({"allow": ["gold.*"]}, "metadata.tables: needs config"),
    ({"config": []}, "metadata.tables.config: must be a list of at least one path, got []"),
    ({"config": "../config"}, "metadata.tables.config: must be a list of at least one path, got '../config'"),
    ({"config": ["a", 5]}, "metadata.tables.config[1]: must be a path, got 5"),
    ({"catalog": [""]}, "metadata.tables.catalog[0]: must be a path, got ''"),
    ({"config": ["a"], "allow": []}, "metadata.tables.allow: must be a list of at least one table pattern"),
    ({"config": ["a"], "allow": ["a,b"]}, "metadata.tables.allow[0]: must be one table pattern, without a comma"),
    ({"config": ["a"], "layer": "gold"}, "metadata.tables: has unknown keys ['layer']; allowed: ['config', 'catalog', "
                                         "'allow']"),
])
def test_the_tables_of_a_report_are_what_ldp_mcp_takes(tables, expected):
    spec = with_metadata(tables=tables)
    assert any(expected in message for message in messages(spec)), messages(spec)


def test_a_report_may_read_the_tables_of_a_catalog_spec_alone_and_without_an_allowlist():
    assert messages(with_metadata(tables={"catalog": ["catalog.json"]})) == []
    assert build(with_metadata(tables={"catalog": ["catalog.json"]}), base_dir=Path(".")).tables.allow is None


@pytest.mark.parametrize("queries, expected", [
    ([], "metadata.queries: is required and must be an object that maps a name to a query"),
    ({}, "metadata.queries: is required and must be an object that maps a name to a query"),
    ({"Revenue": {"sql": "SELECT 1"}}, "metadata.queries.Revenue: the name must be lower case letters"),
    ({"a" * 64: {"sql": "SELECT 1"}}, "the name must be lower case letters, digits and underscores"),
    ({"q": "SELECT 1"}, "metadata.queries.q: must be an object with sql or sql_file, got str"),
    ({"q": {}}, "metadata.queries.q: needs exactly one of sql, which is the SQL itself, and sql_file"),
    ({"q": {"sql": "SELECT 1", "sql_file": "q.sql"}}, "needs exactly one of sql"),
    ({"q": {"sql": ""}}, "metadata.queries.q.sql: must be SQL text, got ''"),
    ({"q": {"sql": 5}}, "metadata.queries.q.sql: must be SQL text, got 5"),
    ({"q": {"sql": "SELECT " + "1" * 100_000}}, "metadata.queries.q.sql: is 100007 characters long, and the limit is "
                                                "100000"),
    ({"q": {"sql_file": ""}}, "metadata.queries.q.sql_file: must be the path of a file of SQL, got ''"),
    ({"q": {"sql": "SELECT 1", "extra": 1}}, "metadata.queries.q: has unknown keys ['extra']; allowed: ['sql', "
                                             "'sql_file', 'description']"),
    ({"q": {"sql": "SELECT 1", "description": " "}}, "metadata.queries.q.description: must be a non-empty string"),
])
def test_the_queries_of_a_report_are_inline_sql_or_a_file(queries, expected):
    spec = with_metadata(queries=queries, blocks=[{"type": "text", "text": "Hello"}])
    assert any(expected in message for message in messages(spec)), messages(spec)


def test_a_query_may_be_as_long_as_the_limit_and_not_longer():
    longest = "SELECT " + "1" * (100_000 - len("SELECT "))
    blocks = [{"type": "text", "text": "Hello"}]

    assert len(longest) == 100_000
    assert messages(with_metadata(queries={"q": {"sql": longest}}, blocks=blocks)) == []
    assert messages(with_metadata(queries={"q": {"sql": longest + "1"}}, blocks=blocks)) == [
        "metadata.queries.q.sql: is 100001 characters long, and the limit is 100000"]


@pytest.mark.parametrize("parameters, expected", [
    ([], "metadata.parameters: must be an object that maps a name to a parameter, got list"),
    ({"Top": {"type": "integer"}}, "metadata.parameters.Top: the name must be lower case letters"),
    ({"window_start": {"type": "string"}},
     "metadata.parameters.window_start: is a name that the window uses. ['window_start', 'window_end'] are set from "
     "--window"),
    ({"p": "integer"}, "metadata.parameters.p: must be an object with a type, got str"),
    ({"p": {}}, "metadata.parameters.p.type: is required and must be one of ['string', 'integer', 'number', 'boolean', "
                "'date']"),
    ({"p": {"type": "decimal"}}, "metadata.parameters.p.type: is required and must be one of ['string', 'integer', "
                                 "'number', 'boolean', 'date'], got 'decimal'"),
    ({"p": {"type": "string", "default": 5}}, "metadata.parameters.p.default: must be a string, got 5"),
    ({"p": {"type": "integer", "default": 5.5}}, "metadata.parameters.p.default: must be a whole number, got 5.5"),
    ({"p": {"type": "integer", "default": True}}, "metadata.parameters.p.default: must be a whole number, got True"),
    ({"p": {"type": "number", "default": "5"}}, "metadata.parameters.p.default: must be a number, got '5'"),
    ({"p": {"type": "number", "default": True}}, "metadata.parameters.p.default: must be a number, got True"),
    ({"p": {"type": "number", "default": float("nan")}}, "metadata.parameters.p.default: must be a number, got nan"),
    ({"p": {"type": "number", "default": float("inf")}}, "metadata.parameters.p.default: must be a number, got inf"),
    ({"p": {"type": "boolean", "default": "yes"}}, "metadata.parameters.p.default: must be true or false, got 'yes'"),
    ({"p": {"type": "date", "default": "2026-13-45"}}, "metadata.parameters.p.default: must be a day written "
                                                       "YYYY-MM-DD"),
    ({"p": {"type": "date", "default": "yesterday"}}, "metadata.parameters.p.default: must be a day written "
                                                      "YYYY-MM-DD"),
    ({"p": {"type": "string", "extra": 1}}, "metadata.parameters.p: has unknown keys ['extra']"),
    ({"p": {"type": "string", "description": ""}}, "metadata.parameters.p.description: must be a non-empty string"),
])
def test_the_parameters_of_a_report_are_declared_with_a_type_and_the_window_names_are_taken(parameters, expected):
    assert any(expected in message for message in messages(with_metadata(parameters=parameters))), messages(
        with_metadata(parameters=parameters))


def test_a_parameter_may_have_no_default_and_a_default_of_each_type():
    spec = with_metadata(parameters={
        "a": {"type": "string"}, "b": {"type": "string", "default": ""}, "c": {"type": "integer", "default": -3},
        "d": {"type": "number", "default": 2.5}, "e": {"type": "number", "default": 2},
        "f": {"type": "boolean", "default": False}, "g": {"type": "date", "default": "2026-02-28"}})

    assert messages(spec) == []
    report = build(spec, base_dir=Path("."))
    assert [(item.name, item.required) for item in report.parameters] == [
        ("a", True), ("b", False), ("c", False), ("d", False), ("e", False), ("f", False), ("g", False)]


# --------------------------------------------------------------------------- blocks


@pytest.mark.parametrize("blocks, expected", [
    ({}, "metadata.blocks: is required and must be a list of at least one block"),
    ([], "metadata.blocks: is required and must be a list of at least one block"),
    (["kpi"], "metadata.blocks[0]: must be an object with a type, got str"),
    ([{}], "metadata.blocks[0].type: is required and must be one of ['kpi', 'bar', 'line', 'table', 'text']"),
    ([{"type": "pie"}], "metadata.blocks[0].type: is required and must be one of ['kpi', 'bar', 'line', 'table', "
                        "'text'], got 'pie'"),
])
def test_the_blocks_of_a_report_are_a_list_of_objects_that_have_a_type(blocks, expected):
    assert any(expected in message for message in messages(with_metadata(blocks=blocks))), messages(
        with_metadata(blocks=blocks))


@pytest.mark.parametrize("block, expected", [
    ({"type": "kpi"}, "needs exactly one of query, which shows a query, and metric"),
    ({"type": "kpi", "query": "by_city", "metric": "revenue"}, "needs exactly one of query"),
    ({"type": "kpi", "query": "by_city"}, "metadata.blocks[7].value: is required and must be the name of a column"),
    ({"type": "kpi", "query": "by_city", "value": "revenue", "compare": "bad name"},
     "metadata.blocks[7].compare: must be the name of a column of the query, got 'bad name'"),
    ({"type": "kpi", "query": "by_city", "value": "revenue", "grain": "day"},
     "metadata.blocks[7].grain: is for a metric. A block that shows a query takes its numbers from the query"),
    ({"type": "kpi", "query": "by_city", "value": "revenue", "filters": ["a=b"]}, "metadata.blocks[7].filters: is for "
                                                                                  "a metric"),
    ({"type": "kpi", "query": "ghost", "value": "revenue"}, "metadata.blocks[7].query: unknown query 'ghost'"),
    ({"type": "kpi", "metric": "revenue", "value": "revenue"},
     "metadata.blocks[7].value: is for a block that shows a query. A metric is shown by its grain and its dimensions"),
    ({"type": "kpi", "metric": "revenue", "compare": "x"}, "metadata.blocks[7].compare: is for a block that shows a "
                                                           "query"),
    ({"type": "kpi", "metric": "revenue", "by": "city"}, "metadata.blocks[7].by: does not apply to a kpi, which shows "
                                                         "one number"),
    ({"type": "kpi", "metric": "revenue,orders"}, "metadata.blocks[7].metric: must be the name of one metric"),
    ({"type": "kpi", "metric": ""}, "metadata.blocks[7].metric: must be the name of one metric"),
    ({"type": "kpi", "metric": "revenue", "grain": "hour"},
     "metadata.blocks[7].grain: must be one of ['day', 'week', 'month', 'quarter', 'year'], got 'hour'"),
    ({"type": "kpi", "metric": "revenue", "filters": []}, "metadata.blocks[7].filters: must be a list of "
                                                          "dimension=value filters"),
    ({"type": "kpi", "metric": "revenue", "filters": ["city"]}, "metadata.blocks[7].filters[0]: must be written "
                                                                "dimension=value"),
    ({"type": "kpi", "metric": "revenue", "filters": ["=x"]}, "metadata.blocks[7].filters[0]: must be written "
                                                              "dimension=value"),
    ({"type": "kpi", "metric": "revenue", "x": "a"}, "has unknown keys ['x']; allowed for a kpi"),
    ({"type": "bar", "query": "by_city", "x": "city"}, "metadata.blocks[7].y: is required"),
    ({"type": "bar", "query": "by_city", "y": "revenue"}, "metadata.blocks[7].x: is required"),
    ({"type": "bar", "query": "by_city", "x": "city", "y": "revenue", "series": "1bad"}, "metadata.blocks[7].series: "
                                                                                         "must be the name of a "
                                                                                         "column"),
    ({"type": "bar", "metric": "revenue"}, "is a bar of a metric, which needs by, the dimension of the bars, or grain"),
    ({"type": "bar", "metric": "revenue", "by": []}, "metadata.blocks[7].by: must be the name of a dimension, or a "
                                                     "list of them"),
    ({"type": "bar", "metric": "revenue", "by": ["city", ""]}, "metadata.blocks[7].by[1]: must be the name of a "
                                                               "dimension"),
    ({"type": "bar", "metric": "revenue", "by": "a,b"}, "metadata.blocks[7].by: must be the name of a dimension"),
    ({"type": "bar", "metric": "revenue", "grain": "week", "x": "a"}, "metadata.blocks[7].x: is for a block that shows "
                                                                      "a query"),
    ({"type": "line", "metric": "revenue", "by": "city"}, "is a line of a metric, which needs grain, the periods along "
                                                          "the line"),
    ({"type": "line", "query": "by_city", "x": "city", "y": "revenue", "format": "euro"},
     "metadata.blocks[7].format: must be integer, number, number:N or percent:N with N from 0 to 6"),
    ({"type": "table"}, "metadata.blocks[7].query: is required and must be the name of a query"),
    ({"type": "table", "query": "top", "metric": "revenue"}, "metadata.blocks[7].metric: is for a kpi, a bar or a "
                                                             "line"),
    ({"type": "table", "query": "top", "columns": []}, "metadata.blocks[7].columns: must be a list of at least one "
                                                       "column"),
    ({"type": "table", "query": "top", "columns": ["a", "a"]}, "metadata.blocks[7].columns: lists a column twice"),
    ({"type": "table", "query": "top", "columns": ["a b"]}, "metadata.blocks[7].columns[0]: must be the name of a "
                                                            "column"),
    ({"type": "table", "query": "top", "formats": ["n"]}, "metadata.blocks[7].formats: must be an object that maps a "
                                                          "column to a format"),
    ({"type": "table", "query": "top", "formats": {"n": "money"}}, "metadata.blocks[7].formats.n: must be integer, "
                                                                   "number"),
    ({"type": "table", "query": "top", "formats": {"n": "currency:usd"}}, "metadata.blocks[7].formats.n: must be "
                                                                          "integer, number"),
    ({"type": "table", "query": "top", "max_rows": 0}, "metadata.blocks[7].max_rows: must be a whole number from 1 to "
                                                       "10000, got 0"),
    ({"type": "table", "query": "top", "max_rows": 10001}, "metadata.blocks[7].max_rows: must be a whole number from 1 "
                                                           "to 10000"),
    ({"type": "table", "query": "top", "max_rows": True}, "metadata.blocks[7].max_rows: must be a whole number from 1 "
                                                          "to 10000"),
    ({"type": "table", "query": "top", "value": "x"}, "has unknown keys ['value']; allowed for a table"),
    ({"type": "bar", "query": "by_city", "x": "city", "y": "revenue", "max_points": 0},
     "metadata.blocks[7].max_points: must be a whole number from 1 to 10000, got 0"),
    ({"type": "line", "query": "by_city", "x": "city", "y": "revenue", "max_points": 10001},
     "metadata.blocks[7].max_points: must be a whole number from 1 to 10000, got 10001"),
    ({"type": "line", "query": "by_city", "x": "city", "y": "revenue", "max_points": 2.5},
     "metadata.blocks[7].max_points: must be a whole number from 1 to 10000, got 2.5"),
    ({"type": "bar", "query": "by_city", "x": "city", "y": "revenue", "max_points": True},
     "metadata.blocks[7].max_points: must be a whole number from 1 to 10000, got True"),
    ({"type": "bar", "metric": "revenue", "by": "city", "max_points": "5"},
     "metadata.blocks[7].max_points: must be a whole number from 1 to 10000, got '5'"),
    ({"type": "kpi", "query": "by_city", "value": "revenue", "max_points": 5},
     "has unknown keys ['max_points']; allowed for a kpi"),
    ({"type": "table", "query": "top", "max_points": 5}, "has unknown keys ['max_points']; allowed for a table"),
    ({"type": "bar", "metric": "revenue", "by": ["city", "cuisine", "day"]},
     "metadata.blocks[7].by: names 3 dimensions, and a bar shows at most 2, one along the axis and one that splits "
     "the bars into series"),
    ({"type": "bar", "metric": "revenue", "grain": "day", "by": ["city", "cuisine"]},
     "metadata.blocks[7].by: names 2 dimensions, and a bar that shows periods shows at most 1, which splits it into "
     "series"),
    ({"type": "line", "metric": "revenue", "grain": "day", "by": ["city", "cuisine"]},
     "metadata.blocks[7].by: names 2 dimensions, and a line that shows periods shows at most 1, which splits it "
     "into series"),
    ({"type": "text"}, "metadata.blocks[7].text: is required and must be the words to show"),
    ({"type": "text", "text": "  "}, "metadata.blocks[7].text: is required and must be the words to show"),
    ({"type": "text", "text": "a { b"}, "metadata.blocks[7].text: has a lone '{'. Write '{{' for a brace"),
    ({"type": "text", "text": "a } b"}, "metadata.blocks[7].text: has a lone '}'. Write '}}' for a brace"),
    ({"type": "text", "text": "Revenue {revenue}"}, "has the placeholder {revenue}, which is not {query.column}"),
    ({"type": "text", "text": "Revenue {By_City.revenue}"}, "has the placeholder {By_City.revenue}, which is not "
                                                            "{query.column}"),
    ({"type": "text", "text": "Revenue {ghost.revenue}"}, "has the placeholder {ghost.revenue}, and 'ghost' is not a "
                                                          "query. The queries are ['by_city', 'top']"),
    ({"type": "text", "text": "Hello", "query": "top"}, "has unknown keys ['query']; allowed for a text"),
    ({"type": "kpi", "metric": "revenue", "title": ""}, "metadata.blocks[7].title: must be a non-empty string"),
])
def test_each_type_of_block_has_what_it_needs_and_nothing_else(block, expected):
    spec = with_block(block)
    assert any(expected in message for message in messages(spec)), messages(spec)


@pytest.mark.parametrize("block", [
    {"type": "bar", "query": "by_city", "x": "city", "y": "revenue", "max_points": 1},
    {"type": "line", "query": "by_city", "x": "city", "y": "revenue", "max_points": 10000},
    {"type": "bar", "metric": "revenue", "by": ["city", "cuisine"]},
    {"type": "bar", "metric": "revenue", "by": "city"},
    {"type": "bar", "metric": "revenue", "grain": "day", "by": "city"},
    {"type": "bar", "metric": "revenue", "grain": "day"},
    {"type": "line", "metric": "revenue", "grain": "week", "by": ["city"], "max_points": 52},
])
def test_a_chart_may_limit_its_points_and_name_the_dimensions_that_it_can_show(block):
    assert messages(with_block(block)) == []


def test_a_chart_keeps_its_max_points_and_the_default_is_500():
    spec = with_block({"type": "line", "query": "by_city", "x": "city", "y": "revenue", "max_points": 7})
    report = build(spec)

    assert report.blocks[-1].max_points == 7 and report.blocks[2].max_points == report_spec.DEFAULT_MAX_POINTS == 500
    assert report_spec.MAX_POINTS == 10000 and report_spec.MAX_SERIES == 8


def test_the_schema_gives_a_bar_and_a_line_their_max_points_and_no_other_block_has_it():
    options = report_schema()["$defs"]["block"]["oneOf"]
    kinds = {item["properties"]["type"]["const"]: item["properties"] for item in options}

    for kind in ("bar", "line"):
        assert kinds[kind]["max_points"] == {
            "type": "integer", "minimum": 1, "maximum": 10000, "default": 500,
            "description": "How many rows of the query the chart draws. It says how many it left out."}
    assert "max_points" not in kinds["kpi"] and "max_points" not in kinds["table"] and "max_points" not in kinds["text"]
    assert kinds["table"]["max_rows"]["maximum"] == 10000


def test_placeholders_are_filled_by_a_lookup_and_a_double_brace_is_one_brace():
    def lookup(query, column):
        return f"<{query}.{column}>"

    assert report_spec.fill_placeholders("Revenue was {by_city.revenue}.", lookup) == "Revenue was <by_city.revenue>."
    assert report_spec.fill_placeholders("{{braces}} and {top.n}", lookup) == "{braces} and <top.n>"
    assert report_spec.fill_placeholders("{top.n}{top.n}", lookup) == "<top.n><top.n>"
    assert report_spec.fill_placeholders("no placeholder", lookup) == "no placeholder"
    assert report_spec.fill_placeholders("", lookup) == ""


@pytest.mark.parametrize("number_format", ["integer", "number", "number:0", "number:6", "percent", "percent:2",
                                           "currency:USD", "currency:INR"])
def test_a_number_format_is_one_of_a_few_and_a_currency_has_a_code(number_format):
    assert messages(with_block({"type": "kpi", "metric": "revenue", "format": number_format})) == []


@pytest.mark.parametrize("number_format", ["", "Integer", "number:7", "number:-1", "percent:x", "currency",
                                           "currency:US",
                                           "currency:usd", "currency:USDX", "money", 5, None])
def test_any_other_number_format_is_a_problem(number_format):
    problems = messages(with_block({"type": "kpi", "metric": "revenue", "format": number_format}))
    assert len(problems) == 1 and "metadata.blocks[7].format: must be integer, number, number:N" in problems[0]


def test_the_placeholders_of_a_text_are_the_pairs_of_a_query_and_a_column_and_two_braces_write_one():
    found, problems = text_references("Revenue was {by_city.revenue}, orders {top.n}. {{not.a.placeholder}} and }}")

    assert found == [("by_city", "revenue"), ("top", "n")] and problems == []
    assert text_references("no placeholders") == ([], [])
    found, problems = text_references("{a.b} { {x} } {a.b.c}")
    assert found == [("a", "b")]
    assert problems == ["has a lone '{'. Write '{{' for a brace, or {query.column} for a value",
                        "has the placeholder {x}, which is not {query.column}",
                        "has a lone '}'. Write '}}' for a brace, or {query.column} for a value",
                        "has the placeholder {a.b.c}, which is not {query.column}"]


def test_every_block_of_every_type_is_a_dataclass_with_the_key_of_its_rows():
    report = build(REPORT, base_dir=Path("."))

    assert [block.type for block in report.blocks] == ["kpi", "kpi", "bar", "bar", "line", "table", "text"]
    assert [block.result for block in report.blocks] == [
        "blocks[0]", "by_city", "by_city", "blocks[3]", "blocks[4]", "top", "blocks[6]"]
    assert [block.index for block in report.metric_blocks] == [0, 3, 4]
    first = report.blocks[0]
    assert (first.metric, first.grain, first.format, first.title, first.by, first.filters) == (
        "revenue", "day", "currency:USD", "Revenue", (), ())
    assert report.blocks[3].by == ("city",) and report.blocks[3].filters == ("cuisine=Thai",)
    assert report.blocks[4].by == ("city",)
    assert isinstance(first, Block) and report.blocks[5].max_rows == 10 and report.blocks[2].max_rows == 100
    assert report.blocks[5].columns == ("cuisine", "n") and dict(report.blocks[5].formats) == {"n": "integer"}


# --------------------------------------------------------------------------- the rest of metadata


@pytest.mark.parametrize("changes, expected", [
    ({"timezone": "Mars/Olympus"}, "metadata.timezone: unknown time zone 'Mars/Olympus'"),
    ({"timezone": 5}, "metadata.timezone: must be an IANA name such as 'Asia/Kolkata' or 'UTC', got 5"),
    ({"timezone": ""}, "metadata.timezone: must be an IANA name"),
    ({"row_cap": 0}, "metadata.row_cap: must be a whole number of rows from 1 to 1000000, got 0"),
    ({"row_cap": 1_000_001}, "metadata.row_cap: must be a whole number of rows from 1 to 1000000"),
    ({"row_cap": 2.5}, "metadata.row_cap: must be a whole number of rows"),
    ({"row_cap": True}, "metadata.row_cap: must be a whole number of rows"),
    ({"timeout_s": 0}, "metadata.timeout_s: must be a number of seconds above 0 and at most 3600"),
    ({"timeout_s": 3601}, "metadata.timeout_s: must be a number of seconds above 0 and at most 3600"),
    ({"timeout_s": "5"}, "metadata.timeout_s: must be a number of seconds"),
    ({"freshness": []}, "metadata.freshness: must be an object with max_age_hours and on_stale, got list"),
    ({"freshness": {"max_age_hours": 0}}, "metadata.freshness.max_age_hours: must be a number of hours above 0, got 0"),
    ({"freshness": {"max_age_hours": "26"}}, "metadata.freshness.max_age_hours: must be a number of hours above 0"),
    ({"freshness": {"max_age_hours": float("inf")}},
     "metadata.freshness.max_age_hours: must be a number of hours above 0"),
    ({"freshness": {"max_age_hours": float("nan")}},
     "metadata.freshness.max_age_hours: must be a number of hours above 0"),
    ({"freshness": {"max_age_hours": True}}, "metadata.freshness.max_age_hours: must be a number of hours above 0"),
    ({"freshness": {"on_stale": "ignore"}}, "metadata.freshness.on_stale: must be one of ['warn', 'block'], got "
                                            "'ignore'"),
    ({"freshness": {"hours": 5}}, "metadata.freshness: has unknown keys ['hours']; allowed: ['max_age_hours', "
                                  "'on_stale']"),
    ({"after": []}, "metadata.after: must be a list of the identifiers of at least one pipeline"),
    ({"after": ["a", 5]}, "metadata.after[1]: must be the identifier of a pipeline, got 5"),
    ({"after": ["a", "a"]}, "metadata.after[1]: 'a' is listed twice"),
    ({"observability": {"sinks": ["carrier_pigeon"]}}, "metadata.observability.sinks[0] has unknown sink type "
                                                       "'carrier_pigeon'"),
    ({"observability": []}, "metadata.observability must be an object"),
])
def test_the_time_zone_the_limits_the_freshness_rule_and_what_it_waits_for_are_checked(changes, expected):
    found = messages(with_metadata(**changes))
    assert any(expected in message for message in found), found


@pytest.mark.parametrize("delivery, expected", [
    ([], "metadata.delivery: must be an object that maps the name of a destination to the destination"),
    ({}, "metadata.delivery: must be an object that maps"),
    ({"Team": {"type": "slack", "webhook_env": "X"}}, "metadata.delivery.Team: the name must be lower case letters"),
    ({"t": "slack"}, "metadata.delivery.t: must be an object with a type, got str"),
    ({"t": {}}, "metadata.delivery.t.type: is required and must be one of ['slack', 'webhook', 'file']"),
    ({"t": {"type": "email"}}, "metadata.delivery.t.type: is required and must be one of ['slack', 'webhook', 'file'], "
                               "got 'email'"),
    ({"t": {"type": "slack"}}, "metadata.delivery.t.webhook_env: is required and names the environment variable that "
                               "holds the address, never the address"),
    ({"t": {"type": "webhook"}}, "metadata.delivery.t.url_env: is required and names the environment variable"),
    ({"t": {"type": "file"}}, "metadata.delivery.t.path: is required"),
    ({"t": {"type": "slack", "webhook_env": "not a name"}}, "metadata.delivery.t.webhook_env: must be the name of an "
                                                            "environment variable such as SLACK_WEBHOOK_URL"),
    ({"t": {"type": "webhook", "url_env": "https://example.com/hook"}}, "metadata.delivery.t.url_env: must be the name "
                                                                        "of an environment variable"),
    ({"t": {"type": "webhook", "url_env": "U", "headers_env": "a=b"}}, "metadata.delivery.t.headers_env: must be the "
                                                                       "name of an environment variable"),
    ({"t": {"type": "slack", "webhook_env": "X", "url_env": "U"}}, "metadata.delivery.t: has unknown keys ['url_env']; "
                                                                   "allowed for a slack destination: ['type', "
                                                                   "'webhook_env', 'retries']"),
    ({"t": {"type": "file", "path": "a.html", "retries": 2}}, "metadata.delivery.t: has unknown keys ['retries']"),
    ({"t": {"type": "slack", "webhook_env": "X", "retries": 11}}, "metadata.delivery.t.retries: must be a whole number "
                                                                  "from 0 to 10"),
    ({"t": {"type": "slack", "webhook_env": "X", "retries": -1}}, "metadata.delivery.t.retries: must be a whole number "
                                                                  "from 0 to 10"),
    ({"t": {"type": "file", "path": ""}}, "metadata.delivery.t.path: must be a non-empty string"),
    ({"t": {"type": "file", "path": "a.html", "public_url": " "}}, "metadata.delivery.t.public_url: must be a "
                                                                   "non-empty string"),
    ({"t": {"type": "file", "path": "a.html", "public_url": "example.com/a"}},
     "metadata.delivery.t.public_url: must be an address that starts with http:// or https:// and has no spaces"),
    ({"t": {"type": "file", "path": "a.html", "public_url": "ftp://example.com/a"}},
     "metadata.delivery.t.public_url: must be an address that starts with http:// or https:// and has no spaces"),
    ({"t": {"type": "file", "path": "a.html", "public_url": "https://example.com/a b"}},
     "metadata.delivery.t.public_url: must be an address that starts with http:// or https:// and has no spaces"),
    ({"t": {"type": "file", "path": "a.html", "public_url": "javascript:alert(1)"}},
     "metadata.delivery.t.public_url: must be an address that starts with http:// or https://"),
])
def test_where_a_report_is_sent_is_a_name_with_a_type_and_the_environment_variable_that_holds_the_address(delivery,
                                                                                                          expected):
    assert any(expected in message for message in messages(with_metadata(delivery=delivery))), messages(
        with_metadata(delivery=delivery))


def test_a_secret_in_a_report_is_refused_by_the_check_that_every_spec_has():
    url = "https://hooks.slack.com/services/T000/B000/XXXXXXXX"

    problems = messages(with_metadata(delivery={"team": {"type": "slack", "webhook_env": url}}))

    assert any("metadata.delivery.team.webhook_env: looks like an inline secret" in item for item in problems), problems
    assert any("must be the name of an environment variable" in item for item in problems)
    assert url not in "\n".join(problems)  # a problem never repeats the secret that it found
    assert any("looks like an inline secret" in item for item in messages(
        with_metadata(queries={"q": {"sql": "SELECT '-----BEGIN PRIVATE KEY-----'"}}, blocks=[{"type": "text",
                                                                                               "text": "x"}])))


def test_a_query_or_a_parameter_called_after_a_secret_is_not_taken_for_one():
    spec = with_metadata(queries={"token_usage": {"sql": "SELECT 1"}},
                         parameters={"password_length": {"type": "integer", "default": 8}},
                         blocks=[{"type": "text", "text": "Hello"}])

    assert messages(spec) == []


def test_a_report_may_be_the_smallest_that_has_the_three_required_parts():
    spec = {"kind": "Report", "identifier": "r", "metadata": {
        "tables": {"config": ["config"]}, "queries": {"q": {"sql": "SELECT 1 AS n"}},
        "blocks": [{"type": "table", "query": "q"}]}}

    assert messages(spec) == []
    report = build(spec, base_dir=Path("/x"))
    assert (report.title, report.timezone, report.row_cap, report.timeout_s) == ("r", "UTC", 10_000, 30.0)
    assert (report.freshness, report.delivery, report.after, report.observability, report.parameters) == (
        None, (), (), None, ())
    assert report.who is None and report.tables.config == ("config",) and report.tables.allow is None


def test_describe_report_names_the_queries_the_blocks_the_parameters_and_the_time_zone():
    assert describe_report(REPORT) == {
        "queries": ["by_city", "top"],
        "blocks": ["kpi revenue", "kpi by_city", "bar by_city", "bar revenue", "line revenue", "table top", "text"],
        "parameters": ["window_start", "window_end", "top_n"], "timezone": "Asia/Kolkata"}


# --------------------------------------------------------------------------- the SQL files


def test_a_file_of_sql_holds_the_queries_that_a_name_line_starts():
    found = named_queries(SQL_FILE)

    assert list(found) == ["top"]
    assert found["top"].startswith("-- the best sellers\nSELECT cuisine, count(*) AS n") and found["top"].endswith(
        "LIMIT $top_n")
    assert named_queries("-- name: a\nSELECT 1;\n\n-- name: b\n-- note\nSELECT 2 ;  \n") == {
        "a": "SELECT 1", "b": "-- note\nSELECT 2"}
    assert named_queries("  --  name:   spaced  \nSELECT 1") == {"spaced": "SELECT 1"}
    assert named_queries("SELECT 1 -- name: not_a_line") == {}
    assert named_queries("") == {} and named_queries("-- name: empty") == {"empty": ""}
    assert named_queries("-- name: Upper_Case9\nSELECT 1") == {"Upper_Case9": "SELECT 1"}


def test_two_queries_with_one_name_in_a_file_are_refused():
    with pytest.raises(ConfigError, match="the query 'a' is named twice"):
        named_queries("-- name: a\nSELECT 1\n-- name: b\nSELECT 2\n-- name: a\nSELECT 3")
    with pytest.raises(ConfigError, match="the query 'a' is named twice"):
        named_queries("-- name: a\nSELECT 1\n-- name: a\nSELECT 2")


def test_a_report_is_read_with_the_sql_of_its_files(folder):
    report, problems = read_report(folder / "daily.json")

    assert problems == [] and report is not None
    assert [(item.name, item.sql_file) for item in report.queries] == [("by_city", None), ("top", "queries.sql")]
    assert (report.queries[0].sql.startswith("SELECT city, sum(revenue)")
            and report.queries[0].description == "Revenue by city.")
    assert report.queries[1].sql.endswith("LIMIT $top_n") and report.file == folder / "daily.json"
    assert report.base_dir == folder and load_report(folder / "daily.json") == report


def test_the_sql_file_is_found_next_to_the_report_wherever_the_command_runs(folder, monkeypatch, tmp_path_factory):
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))

    report, problems = read_report(folder / "daily.json")

    assert problems == [] and report.queries[1].sql is not None


def test_a_sql_file_in_another_folder_is_read_by_its_relative_path(tmp_path):
    spec = with_metadata(queries={"top": {"sql_file": "../sql/queries.sql"}}, blocks=[{"type": "table",
                                                                                       "query": "top"}])
    write(tmp_path / "sql" / "queries.sql", SQL_FILE)
    write(tmp_path / "reports" / "daily.json", spec)

    report, problems = read_report(tmp_path / "reports" / "daily.json")

    assert problems == [] and report.queries[0].sql.endswith("LIMIT $top_n")


@pytest.mark.parametrize("content, expected", [
    (None, "metadata.queries.top.sql_file: the file {file} cannot be read: No such file or directory"),
    ("-- name: other\nSELECT 1", "metadata.queries.top.sql_file: the file {file} has no '-- name: top' line. It has "
                                 "['other']"),
    ("SELECT 1", "metadata.queries.top.sql_file: the file {file} has no '-- name: top' line. It has no named query"),
    ("-- name: top\n;", "metadata.queries.top.sql_file: the query 'top' of {file} is empty"),
    ("-- name: top\nSELECT 1\n-- name: top\nSELECT 2", "metadata.queries.top.sql_file: the file {file} is not usable. "
                                                       "the query 'top' is named twice"),
    ("-- name: top\nSELECT " + "1" * 100_000, "metadata.queries.top.sql_file: the query 'top' of {file} is 100007 "
                                              "characters long, and the limit is 100000"),
])
def test_a_report_with_a_sql_file_that_cannot_be_used_says_why_with_the_path_of_the_query(folder, content, expected):
    sql = folder / "queries.sql"
    if content is None:
        sql.unlink()
    else:
        sql.write_text(content)

    report, problems = read_report(folder / "daily.json")

    assert report is None and problems == [expected.format(file=sql)]


def test_a_report_that_has_a_problem_in_its_shape_does_not_read_its_files_and_lists_the_shape_problems(folder):
    (folder / "queries.sql").unlink()
    write(folder / "daily.json", with_metadata(row_cap=0, timeout_s=0))

    report, problems = read_report(folder / "daily.json")

    assert report is None and [item.split(":")[0] for item in problems] == ["metadata.row_cap", "metadata.timeout_s"]


@pytest.mark.parametrize("content, expected", [
    ("{not json", "daily.json is not valid JSON: "),
    ("[1, 2]", "daily.json must be a JSON object, got list"),
    ({"kind": "report"}, "kind: unknown kind 'report'; did you mean 'Report'?"),
    ({"kind": "Pipeline", "identifier": "p"}, 'kind: daily.json is a Pipeline, and a report has "kind": "Report"'),
    ({"identifier": "p"}, 'kind: daily.json is a Pipeline, and a report has "kind": "Report"'),
    ({"kind": "Workflow", "identifier": "w"}, 'kind: daily.json is a Workflow, and a report has "kind": "Report"'),
])
def test_read_report_says_why_a_file_is_not_a_report(tmp_path, content, expected):
    report, problems = read_report(write(tmp_path / "daily.json", content))

    assert report is None and len(problems) == 1 and problems[0].startswith(expected)


def test_a_report_file_that_is_not_there_is_a_problem_and_load_report_raises_with_every_problem(folder):
    report, problems = read_report(folder / "nope.json")
    assert report is None and problems == [f"report file not found: {folder / 'nope.json'}"]
    with pytest.raises(ConfigError, match="nope.json: report file not found"):
        load_report(folder / "nope.json")

    write(folder / "daily.json", with_metadata(row_cap=0, timeout_s=0))
    with pytest.raises(ConfigError) as caught:
        load_report(folder / "daily.json")
    assert str(caught.value).splitlines()[0] == "daily.json has 2 problems:"
    assert "  - metadata.row_cap: must be a whole number of rows" in str(caught.value)


def test_a_report_that_has_every_optional_part_is_read_into_one_object(folder):
    report = load_report(folder / "daily.json")

    assert (report.identifier, report.title, report.who, report.what) == ("daily_sales", "Daily sales",
                                                                          "owner@example.com",
                                                                          "Orders and revenue of one day")
    assert (report.where, report.when, report.how) == ("Slack", "every morning", "from the gold tables")
    assert report.tables.config == ("../config",) and report.tables.allow == ("gold.*",)
    assert (report.timezone, report.row_cap, report.timeout_s) == ("Asia/Kolkata", 5000, 12.5)
    assert (report.freshness.max_age_hours, report.freshness.on_stale) == (26.0, "block")
    assert report.after == ("orders", "menu_items") and report.observability == {"sinks": ["jsonl"]}
    assert [(item.name, item.type) for item in report.delivery] == [("team", "slack"), ("hook", "webhook"), ("archive",
                                                                                                             "file")]
    assert dict(report.delivery[0].options) == {"webhook_env": "SLACK_WEBHOOK_URL", "retries": 2}
    parameter = report.parameter("top_n")
    assert (parameter.type, parameter.default, parameter.required, parameter.description) == (
        "integer", 5, False, "How many to show.")
    assert report.query("top").sql_file == "queries.sql" and report.query("ghost") is None
    assert report.parameter("ghost") is None


def test_plan_of_a_report_is_the_same_whichever_way_it_is_asked_for(folder):
    from_path = plan(folder / "daily.json")
    from_dict = plan(REPORT)

    assert from_path["valid"] and from_dict["valid"] and from_path["kind"] == "Report"
    assert from_path["queries"] == from_dict["queries"] == ["by_city", "top"]


def test_the_modules_of_the_report_package_load_by_name():
    import local_data_platform.report as package

    assert package.KIND == "Report" and package.validate_report is validate_report
    assert package.load_report is report_spec.load_report and "run_queries" in dir(package)
    with pytest.raises(AttributeError):
        package.not_there  # noqa: B018


@pytest.mark.parametrize("address", ["https://example.com/daily", "http://intranet.example/reports/daily.html",
                                     "https://example.com/r?a=1&b=2#top", "http://127.0.0.1:8000/x"])
def test_the_public_address_of_a_file_is_an_http_or_https_address_with_no_space(address):
    delivery = {"t": {"type": "file", "path": "a.html", "public_url": address}}

    assert messages(with_metadata(delivery=delivery)) == []
