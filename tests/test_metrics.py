"""Metrics: numbers defined once in a ``metrics.json`` and computed the same way by every reader.

A metrics file has semantic models over gold tables and the metrics made of their measures. These tests cover the
definitions and their checks, the compiler, which turns a request into SQL with bound parameters, the answers that
``ldp metrics query`` and ``metrics.query`` give from the current release or another, ``ldp gold build`` checking the
metrics before it cuts a release, the two MCP tools, and the catalog showing the metrics of each table. Everything is
offline. The numbers of the table are small enough to work out by hand, and each metric is compared with SQL that was
written by hand over the same rows.
"""

import copy
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from local_data_platform import metrics
from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.gold import catalog as catalogs
from local_data_platform.gold.build import build_gold, find_configs, find_gold
from local_data_platform.gold.release import ReleaseError
from local_data_platform.mcp_server.tools import LakeTools
from local_data_platform.mcp_server.discovery import config_files, discover_tables
from local_data_platform.metrics import check, compiler, load, model, render, runner
from local_data_platform.metrics import sql as sqlparts
from local_data_platform.spec import RESERVED_KINDS, kind_of_file, kind_schema, validate_spec

CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
SCHEMA = pa.schema([
    ("order_date", pa.date32()), ("city", pa.string()), ("cuisine", pa.string()), ("customer_id", pa.string()),
    ("orders", pa.int64()), ("revenue", pa.decimal128(18, 2)), ("rating", pa.float64())])
D = Decimal
ROWS = [
    (dt.date(2026, 1, 3), "Pune", "Thai", "c1", 10, D("100.50"), 4.5),
    (dt.date(2026, 1, 4), "Pune", "Thai", "c2", 5, D("75.00"), 3.5),
    (dt.date(2026, 1, 20), "Delhi", "Thai", "c1", 5, D("50.25"), None),
    (dt.date(2026, 2, 2), "Pune", "Italian", "c3", 0, D("0.00"), 4.0),
    (dt.date(2026, 2, 14), "Pune", "Italian", "c3", 4, D("80.00"), 5.0),
    (dt.date(2026, 3, 31), "Pune", "Italian", "c4", 2, D("30.00"), 2.0),
    (dt.date(2026, 4, 1), "Pune", "Thai", "c1", 1, D("9.00"), 4.0),
    (dt.date(2026, 12, 31), "Delhi", "Italian", "c5", 3, D("45.00"), 3.0),
    (dt.date(2027, 1, 1), "Delhi", "Thai", "c1", 6, D("72.00"), 4.0),
]
"""Nine rows of one gold table. January of 2026 has Pune twice, so a ratio of sums and an average of ratios differ."""

MODEL = {
    "name": "daily_sales", "table": "gold.gold_sales", "description": "Sales by day.",
    "time_dimension": {"column": "order_date", "grains": ["day", "week", "month", "quarter", "year"]},
    "dimensions": ["city", {"name": "kitchen", "column": "cuisine", "description": "The kind of food."}],
    "measures": [
        {"name": "revenue_sum", "agg": "sum", "expr": "revenue"},
        {"name": "order_sum", "agg": "sum", "expr": "orders"},
        {"name": "customer_count", "agg": "count_distinct", "expr": "customer_id"},
        {"name": "row_count", "agg": "count"},
        {"name": "rating_count", "agg": "count", "expr": "rating"},
        {"name": "rating_avg", "agg": "avg", "expr": "rating"},
        {"name": "biggest", "agg": "max", "expr": "revenue"},
        {"name": "smallest", "agg": "min", "expr": "orders"},
        {"name": "cents_sum", "agg": "sum", "expr": "CAST(revenue * 100 AS BIGINT)"},
    ],
}
METRICS = {
    "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "sales_metrics", "who": "analytics@example.com",
    "semantic_models": [MODEL],
    "metrics": [
        {"name": "revenue", "type": "simple", "measure": "revenue_sum", "format": "currency",
         "description": "Money taken."},
        {"name": "orders", "type": "simple", "measure": "order_sum", "description": "Orders taken."},
        {"name": "avg_ticket", "type": "ratio", "numerator": "revenue_sum", "denominator": "order_sum",
         "format": "currency", "description": "Revenue divided by orders.", "owner": "finance@example.com"},
        {"name": "customers", "type": "simple", "measure": "customer_count", "description": "Different customers."},
        {"name": "batches", "type": "simple", "measure": "row_count", "description": "Rows."},
        {"name": "rated", "type": "simple", "measure": "rating_count", "description": "Rows with a rating."},
        {"name": "avg_rating", "type": "simple", "measure": "rating_avg", "description": "Mean rating."},
        {"name": "biggest_batch", "type": "simple", "measure": "biggest", "description": "Biggest revenue."},
        {"name": "smallest_batch", "type": "simple", "measure": "smallest", "description": "Fewest orders."},
        {"name": "cents", "type": "simple", "measure": "cents_sum", "description": "Revenue in cents."},
        {"name": "rating_share", "type": "ratio", "numerator": "rating_count", "denominator": "row_count",
         "format": "percent", "description": "The share of rows that have a rating."},
    ],
}


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def table_of(rows=ROWS, schema=SCHEMA) -> pa.Table:
    return pa.table({name: [row[position] for row in rows] for position, name in enumerate(schema.names)},
                    schema=schema)


def sales(root: Path, rows=ROWS, schema=SCHEMA, name: str = "gold_sales") -> Iceberg:
    """The gold table ``gold.<name>`` of ``root``, with its config, which says it is built elsewhere."""
    table = Iceberg(name, {"identifier": "gold", "warehouse_path": str(root / "warehouse")})
    table.put(table_of(rows, schema))
    write(root / f"{name}.json", {"identifier": name, "who": "ops", "metadata": {
        "source": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": "Sales by day and city.", "grain": "one row per order batch",
                 "columns": {column: f"The {column}." for column in schema.names}}}})
    return table


def metrics_file(root: Path, spec=None, name: str = "metrics.json") -> Path:
    return write(root / name, copy.deepcopy(METRICS if spec is None else spec))


def changed(**changes) -> dict:
    """The metrics file with some top-level keys replaced. A value of ``None`` removes the key."""
    spec = copy.deepcopy(METRICS)
    for key, value in changes.items():
        if value is None:
            spec.pop(key, None)
        else:
            spec[key] = value
    return spec


def with_model(**changes) -> dict:
    """The metrics file whose one semantic model has some keys replaced. ``None`` removes the key."""
    spec = copy.deepcopy(METRICS)
    for key, value in changes.items():
        if value is None:
            spec["semantic_models"][0].pop(key, None)
        else:
            spec["semantic_models"][0][key] = value
    return spec


def with_metric(which: str, **changes) -> dict:
    """The metrics file whose metric called ``which`` has some keys replaced. ``None`` removes the key."""
    spec = copy.deepcopy(METRICS)
    metric = next(item for item in spec["metrics"] if item["name"] == which)
    for key, value in changes.items():
        if value is None:
            metric.pop(key, None)
        else:
            metric[key] = value
    return spec


@pytest.fixture
def lake(tmp_path):
    """A folder of configs with the gold table of ``ROWS``, its config and a valid ``metrics.json``."""
    sales(tmp_path)
    metrics_file(tmp_path)
    return tmp_path


def messages(spec) -> list[str]:
    return [str(error) for error in validate_spec(spec)]


def hand(sql_text: str, *params, rows=ROWS, schema=SCHEMA) -> list[tuple]:
    """The rows of a query that was written by hand over the nine rows, as the table ``t``."""
    connection = duckdb.connect()
    connection.register("t", table_of(rows, schema))
    try:
        return connection.execute(sql_text, list(params)).fetchall()
    finally:
        connection.close()


def answer(table: pa.Table) -> list[tuple]:
    return [tuple(row.values()) for row in table.to_pylist()]


def close(left: list[tuple], right: list[tuple]) -> bool:
    """Whether two lists of rows are the same, with numbers that agree to 9 places."""
    if len(left) != len(right):
        return False
    for one, two in zip(left, right):
        if len(one) != len(two):
            return False
        for a, b in zip(one, two):
            if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)) and not isinstance(
                    a, bool):
                if abs(float(a) - float(b)) > 1e-9:
                    return False
            elif a != b:
                return False
    return True


# --------------------------------------------------------------------------- the file


def test_the_metrics_file_of_these_tests_is_valid_and_has_the_kind_metrics():
    assert messages(METRICS) == []
    assert model.KIND == "Metrics" and METRICS["kind"] == "Metrics"
    assert (model.GRAINS, model.AGGREGATIONS, model.METRIC_TYPES, model.FORMATS) == (
        ("day", "week", "month", "quarter", "year"), ("sum", "count", "count_distinct", "avg", "min", "max"),
        ("simple", "ratio"), ("number", "percent", "currency"))


def test_ldp_plan_validates_the_metrics_file_of_a_folder_as_metrics(lake, capsys):
    main(["plan", str(lake), "--json"])

    plans = json.loads(capsys.readouterr().out)
    [item] = [entry for entry in plans if entry["kind"] == "Metrics"]
    assert (item["identifier"], item["valid"], item["errors"]) == ("sales_metrics", True, [])
    assert item["semantic_models"] == ["daily_sales"] and item["metrics"][:3] == ["revenue", "orders", "avg_ticket"]
    assert item["route"] is None and item["spec_hash"] is None and item["target"] is None


def test_ldp_plan_of_a_folder_that_has_only_a_metrics_file_exits_with_0(tmp_path, capsys):
    metrics_file(tmp_path)

    assert main(["plan", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "metrics.json" in out and "Metrics" in out and "sales_metrics" in out


def test_ldp_plan_prints_the_names_of_the_models_and_metrics_of_one_file(lake, capsys):
    assert main(["plan", str(lake / "metrics.json")]) == 0

    lines = capsys.readouterr().out.splitlines()
    fields = {line.split()[0]: line.split(None, 1)[1].strip() for line in lines}
    assert fields["kind"] == "Metrics" and fields["identifier"] == "sales_metrics"
    assert fields["semantic_models"] == "daily_sales"
    assert fields["metrics"].startswith("revenue, orders, avg_ticket, customers")


def test_ldp_plan_reports_what_is_wrong_with_a_metrics_file_and_exits_with_1(lake, capsys):
    metrics_file(lake, with_model(measures=[{"name": "bad", "agg": "total", "expr": "revenue"}]))

    assert main(["plan", str(lake / "metrics.json")]) == 1

    err = capsys.readouterr().err
    assert "semantic_models[0].measures[0].agg: is required and must be one of" in err


def test_ldp_schema_prints_the_schema_of_a_metrics_file(capsys):
    assert main(["schema", "--kind", "Metrics"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == kind_schema("Metrics") == model.metrics_schema()
    assert printed["properties"]["kind"] == {"const": "Metrics", "description": printed["properties"]["kind"][
        "description"]}
    assert printed["required"] == ["kind", "identifier", "semantic_models", "metrics"]
    assert set(printed["$defs"]) == {"semantic_model", "dimension", "measure", "metric"}


def test_the_metrics_kind_is_no_longer_reserved_and_a_wrong_case_says_the_right_spelling():
    assert "Metrics" not in RESERVED_KINDS and "Report" in RESERVED_KINDS
    wrong = messages({**METRICS, "kind": "metrics"})[0]
    assert wrong.startswith("kind: unknown kind 'metrics'; did you mean 'Metrics'?")


def test_a_folder_with_a_metrics_file_is_skipped_by_the_readers_of_dataset_configs(lake):
    assert kind_of_file(lake / "metrics.json") == "Metrics"
    assert [item.name for item in config_files(lake)] == ["gold_sales.json"]
    assert [file.name for file, _ in find_configs(lake)] == ["gold_sales.json"]
    assert [item.path.name for item in find_gold(lake)] == ["gold_sales.json"]
    found = discover_tables([lake])
    try:
        assert [item.identifier for item in found.tables] == ["gold.gold_sales"] and found.unavailable == []
    finally:
        found.close()


# --------------------------------------------------------------------------- checking a file without a table

GRAIN_LIST = "['day', 'week', 'month', 'quarter', 'year']"
MEASURE_NAMES = ("['biggest', 'cents_sum', 'customer_count', 'order_sum', 'rating_avg', 'rating_count', "
                 "'revenue_sum', 'row_count', 'smallest']")
TOP_KEYS = ("['apiVersion', 'kind', 'identifier', 'who', 'what', 'where', 'when', 'how', 'semantic_models', "
            "'metrics']")


def two_models(**changes) -> dict:
    """The metrics file with a second semantic model whose one measure is ``other_sum``."""
    spec = copy.deepcopy(METRICS)
    spec["semantic_models"].append({"name": "other", "table": "gold.other",
                                    "measures": [{"name": "other_sum", "agg": "count", **changes}]})
    return spec


BROKEN_FILES = [
    ("an unknown top-level key", changed(extra=1), [f"has unknown keys ['extra']; allowed: {TOP_KEYS}"]),
    ("another api version", changed(apiVersion="ldp/v2"), ["apiVersion: must be 'ldp/v1', got 'ldp/v2'"]),
    ("no identifier", changed(identifier=None), ["identifier: is required"]),
    ("a who that is not text", changed(who=5), ["who: must be a string, got int"]),
    ("no semantic models", changed(semantic_models=None),
     ["semantic_models: is required and must be a list of at least one semantic model"]),
    ("an empty list of models", changed(semantic_models=[]),
     ["semantic_models: is required and must be a list of at least one semantic model"]),
    ("a model that is not an object", changed(semantic_models=["x"]),
     ["semantic_models[0]: must be an object with a name, a table and measures, got str"]),
    ("an unknown key of a model", with_model(color="red"),
     ["semantic_models[0]: has unknown keys ['color']; allowed: ['name', 'description', 'table', 'time_dimension', "
      "'dimensions', 'measures']"]),
    ("a model with no name", with_model(name=None),
     ["semantic_models[0].name: is required and must be a non-empty string"]),
    ("a model name with capitals and a space", with_model(name="Daily Sales"),
     ["semantic_models[0].name: must be lower case letters, digits and underscores, starting with a letter, and at "
      "most 63 characters, got 'Daily Sales'"]),
    ("a model with no table", with_model(table=None), ["semantic_models[0].table: is required"]),
    ("a description of blanks", with_model(description=" "),
     ["semantic_models[0].description: must be a non-empty string, got ' '"]),
    ("a time dimension that is text", with_model(time_dimension="order_date"),
     ["semantic_models[0].time_dimension: must be an object with a column and its grains, got str"]),
    ("a time dimension with no column", with_model(time_dimension={"grains": ["day"]}),
     ["semantic_models[0].time_dimension.column: is required and must be the name of a date or timestamp column"]),
    ("a grain that does not exist and a grain twice",
     with_model(time_dimension={"column": "order_date", "grains": ["day", "fortnight", "day"]}),
     [f"semantic_models[0].time_dimension.grains[1]: must be one of {GRAIN_LIST}, got 'fortnight'",
      "semantic_models[0].time_dimension.grains[2]: 'day' is listed twice"]),
    ("no grains", with_model(time_dimension={"column": "order_date"}),
     [f"semantic_models[0].time_dimension.grains: is required and must be a list of at least one of {GRAIN_LIST}"]),
    ("an empty list of grains", with_model(time_dimension={"column": "order_date", "grains": []}),
     [f"semantic_models[0].time_dimension.grains: is required and must be a list of at least one of {GRAIN_LIST}"]),
    ("an unknown key of a time dimension",
     with_model(time_dimension={"column": "order_date", "grains": ["day"], "tz": "UTC"}),
     ["semantic_models[0].time_dimension: has unknown keys ['tz']; allowed: ['column', 'grains']"]),
    ("dimensions that are text", with_model(dimensions="city"),
     ["semantic_models[0].dimensions: must be a list of dimensions, got str"]),
    ("a dimension that is a number", with_model(dimensions=[5]),
     ["semantic_models[0].dimensions[0]: must be a column name, or an object with a name, got int"]),
    ("a dimension twice", with_model(dimensions=["city", {"name": "city"}]),
     ["semantic_models[0].dimensions[1].name: 'city' is the name of an earlier dimension"]),
    ("a dimension called like a grain", with_model(dimensions=["month"]),
     ["semantic_models[0].dimensions[0]: 'month' is the name of a grain, and the column for --grain month would "
      "clash with it. Give the dimension another name"]),
    ("a dimension that is the time column", with_model(dimensions=["order_date"]),
     ["semantic_models[0].dimensions[0]: is the column of the time dimension, 'order_date'. Group by it with "
      "--grain"]),
    ("an unknown key of a dimension", with_model(dimensions=[{"name": "city", "label": "x"}]),
     ["semantic_models[0].dimensions[0]: has unknown keys ['label']; allowed: ['name', 'column', 'description']"]),
    ("no measures", with_model(measures=None, ) | {"metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures: is required and must be a list of at least one measure"]),
    ("a measure that is not an object", with_model(measures=["sum"]) | {"metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[0]: must be an object with a name, an agg and an expr, got str"]),
    ("an agg that does not exist", with_model(measures=[{"name": "revenue_sum", "agg": "median", "expr": "x"}]) | {
        "metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[0].agg: is required and must be one of ['sum', 'count', 'count_distinct', 'avg', "
      "'min', 'max'], got 'median'"]),
    ("no agg", with_model(measures=[{"name": "revenue_sum", "expr": "x"}]) | {"metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[0].agg: is required and must be one of ['sum', 'count', 'count_distinct', 'avg', "
      "'min', 'max']"]),
    ("a sum with no expression", with_model(measures=[{"name": "revenue_sum", "agg": "sum"}]) | {
        "metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[0].expr: is required for 'sum'. Only a count of rows has no expression"]),
    ("an expression of blanks", with_model(measures=[{"name": "revenue_sum", "agg": "sum", "expr": " "}]) | {
        "metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[0].expr: must be a SQL expression of the columns of the table, got ' '"]),
    ("an expression that is too long",
     with_model(measures=[{"name": "revenue_sum", "agg": "sum", "expr": "x" * 2001}]) | {
         "metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[0].expr: is 2001 characters long, and the limit is 2000"]),
    ("a measure twice in a model", with_model(measures=[{"name": "revenue_sum", "agg": "count"},
                                                        {"name": "revenue_sum", "agg": "count"}]) | {
        "metrics": [METRICS["metrics"][0]]},
     ["semantic_models[0].measures[1].name: 'revenue_sum' is the name of an earlier measure"]),
    ("no metrics", changed(metrics=None), ["metrics: is required and must be a list of at least one metric"]),
    ("an empty list of metrics", changed(metrics=[]),
     ["metrics: is required and must be a list of at least one metric"]),
    ("a metric that is not an object", changed(metrics=["x"]),
     ["metrics[0]: must be an object with a name, a type and a description, got str"]),
    ("no type", with_metric("revenue", type=None),
     ["metrics[0].type: is required and must be one of ['simple', 'ratio']"]),
    ("a type that does not exist", with_metric("revenue", type="derived"),
     ["metrics[0].type: is required and must be one of ['simple', 'ratio'], got 'derived'"]),
    ("no description", with_metric("revenue", description=None),
     ["metrics[0].description: is required, and says what the metric means"]),
    ("no owner and no who", changed(who=None) | {"metrics": [{**METRICS["metrics"][0]}]},
     ["metrics[0].owner: is required, unless the file has a who that owns every metric"]),
    ("an owner of nothing", with_metric("revenue", owner=""),
     ["metrics[0].owner: must be a non-empty string, got ''"]),
    ("a format that does not exist", with_metric("revenue", format="euro"),
     ["metrics[0].format: must be one of ['number', 'percent', 'currency'], got 'euro'"]),
    ("a simple metric with no measure", with_metric("revenue", measure=None),
     ["metrics[0].measure: is required and must be the name of a measure"]),
    ("a simple metric with a numerator", with_metric("revenue", numerator="revenue_sum"),
     ["metrics[0].numerator: is for a ratio. A simple metric has one measure"]),
    ("a simple metric of a measure that does not exist", with_metric("revenue", measure="rev"),
     [f"metrics[0].measure: unknown measure 'rev'; the measures are {MEASURE_NAMES}"]),
    ("a ratio with no denominator", with_metric("avg_ticket", denominator=None),
     ["metrics[2].denominator: is required and must be the name of a measure"]),
    ("a ratio with a measure", with_metric("avg_ticket", measure="revenue_sum"),
     ["metrics[2].measure: is for a simple metric. A ratio has a numerator and a denominator"]),
    ("a ratio of measures that do not exist", with_metric("avg_ticket", numerator="nope", denominator="nada"),
     [f"metrics[2].numerator: unknown measure 'nope'; the measures are {MEASURE_NAMES}",
      f"metrics[2].denominator: unknown measure 'nada'; the measures are {MEASURE_NAMES}"]),
    ("a metric called like a grain", with_metric("revenue", name="month"),
     ["metrics[0].name: 'month' is the name of a grain, and the column for --grain month would clash with it. Give "
      "the metric another name"]),
    ("a metric called like a dimension", with_metric("revenue", name="city"),
     ["metrics[0].name: is also a dimension of the semantic model 'daily_sales', and a result would have two "
      "columns of that name"]),
    ("a metric twice", with_metric("orders", name="revenue"),
     ["metrics[1].name: 'revenue' is the name of an earlier metric"]),
    ("a metric name with a capital", with_metric("revenue", name="Revenue"),
     ["metrics[0].name: must be lower case letters, digits and underscores, starting with a letter, and at most 63 "
      "characters, got 'Revenue'"]),
    ("an unknown key of a metric", with_metric("revenue", label="x"),
     ["metrics[0]: has unknown keys ['label']; allowed: ['name', 'type', 'description', 'owner', 'format', "
      "'measure', 'numerator', 'denominator']"]),
    ("an inline secret", with_metric("revenue", description="-----BEGIN PRIVATE KEY-----"),
     ["metrics[0].description: looks like an inline secret; keep secrets out of specs and reference an environment "
      "variable instead (e.g. 'token_env': 'MY_TOKEN')"]),
]


@pytest.mark.parametrize("spec, expected", [item[1:] for item in BROKEN_FILES], ids=[item[0] for item in BROKEN_FILES])
def test_a_metrics_file_is_checked_without_a_table_and_every_problem_names_its_path(spec, expected):
    assert messages(spec) == expected


def test_a_measure_name_is_unique_in_the_file_and_the_message_names_the_other_model():
    spec = two_models()
    spec["semantic_models"][1]["measures"][0]["name"] = "revenue_sum"

    assert messages(spec) == [
        "semantic_models[1].measures[0].name: 'revenue_sum' is already a measure of the semantic model "
        "'daily_sales'. A measure name is unique in the file, because a metric refers to it by name"]


def test_a_ratio_of_two_measures_in_two_models_is_refused_because_it_would_need_a_join():
    spec = two_models()
    spec["metrics"][2]["denominator"] = "other_sum"

    assert messages(spec) == ["metrics[2]: is made of measures of 2 semantic models, ['daily_sales', 'other']. A "
                              "metric reads one table, so its measures have to be of one semantic model"]


def test_a_model_name_is_unique_and_a_model_without_a_name_does_not_break_the_check_of_the_metrics():
    spec = two_models()
    spec["semantic_models"][1]["name"] = "daily_sales"
    assert messages(spec) == ["semantic_models[1].name: 'daily_sales' is the name of an earlier semantic model"]

    nameless = with_model(name=None)
    assert messages(nameless) == ["semantic_models[0].name: is required and must be a non-empty string"]


def test_the_owner_of_a_metric_is_its_own_or_the_who_of_the_file():
    models, found = model.build(METRICS, "metrics.json")
    owners = {item.name: item.owner for item in found}

    assert owners["revenue"] == "analytics@example.com" and owners["avg_ticket"] == "finance@example.com"
    assert models[0].name == "daily_sales" and {item.file for item in found} == {"metrics.json"}


def test_a_metric_that_leaves_the_format_out_is_a_number_and_a_dimension_is_its_own_column_by_default():
    models, found = model.build(METRICS, "metrics.json")

    assert {item.name: item.format for item in found}["orders"] == "number"
    assert {item.name: item.format for item in found}["rating_share"] == "percent"
    dimensions = {item.name: (item.column, item.description) for item in models[0].dimensions}
    assert dimensions == {"city": ("city", None), "kitchen": ("cuisine", "The kind of food.")}
    assert [item.agg for item in models[0].measures] == ["sum", "sum", "count_distinct", "count", "count", "avg",
                                                         "max", "min", "sum"]
    assert models[0].measure("row_count").expr is None and models[0].measure("nope") is None
    assert models[0].dimension("nope") is None and models[0].grains == tuple(model.GRAINS)


def test_a_model_with_no_time_dimension_has_no_grains():
    models, _ = model.build(with_model(time_dimension=None), "m.json")

    assert models[0].time_dimension is None and models[0].grains == ()


# --------------------------------------------------------------------------- the JSON Schema


def test_the_json_schema_is_valid_and_accepts_the_file_of_these_tests():
    jsonschema = pytest.importorskip("jsonschema")
    schema = kind_schema("Metrics")

    jsonschema.Draft202012Validator.check_schema(schema)
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(METRICS))


EXAMPLE_METRICS = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*/config*/metrics.json"))


def test_the_metrics_files_of_the_examples_are_valid_without_a_table():
    assert [path.parent.parent.name for path in EXAMPLE_METRICS] == ["restaurant", "robot_episodes"]
    for path in EXAMPLE_METRICS:
        assert messages(json.loads(path.read_text())) == [], path


def test_the_json_schema_accepts_the_metrics_files_of_the_examples():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(kind_schema("Metrics"))

    assert EXAMPLE_METRICS
    for path in EXAMPLE_METRICS:
        assert not list(validator.iter_errors(json.loads(path.read_text()))), path


@pytest.mark.parametrize("label", [
    "an unknown top-level key", "an unknown key of a model", "a model with no table", "a time dimension with no column",
    "a grain that does not exist and a grain twice", "no grains", "an empty list of grains",
    "an unknown key of a time dimension", "a dimension that is a number", "an unknown key of a dimension",
    "an agg that does not exist", "a sum with no expression", "an expression that is too long", "no metrics",
    "an empty list of metrics", "no type", "a type that does not exist", "no description",
    "a format that does not exist",
    "a simple metric with no measure", "a simple metric with a numerator", "a ratio with no denominator",
    "a ratio with a measure", "a metric name with a capital", "an unknown key of a metric",
    "a model name with capitals and a space", "no semantic models", "an empty list of models",
])
def test_the_json_schema_refuses_what_the_checks_without_a_table_refuse_where_a_schema_can_say_so(label):
    jsonschema = pytest.importorskip("jsonschema")
    spec = next(item[1] for item in BROKEN_FILES if item[0] == label)

    assert list(jsonschema.Draft202012Validator(kind_schema("Metrics")).iter_errors(spec)), label


def test_the_json_schema_names_the_grains_aggregations_formats_and_types_of_the_checks():
    defs = kind_schema("Metrics")["$defs"]

    assert defs["semantic_model"]["properties"]["time_dimension"]["properties"]["grains"]["items"] == {
        "enum": list(model.GRAINS)}
    assert defs["measure"]["properties"]["agg"] == {"enum": list(model.AGGREGATIONS)}
    assert defs["metric"]["properties"]["format"]["enum"] == list(model.FORMATS)
    assert defs["metric"]["properties"]["type"] == {"enum": list(model.METRIC_TYPES)}
    assert defs["metric"]["properties"]["name"]["pattern"] == f"^{model.NAME_PATTERN}$"


# --------------------------------------------------------------------------- the files of a folder


def test_the_metrics_files_of_a_folder_are_the_json_files_whose_kind_is_metrics_in_the_order_of_their_names(tmp_path):
    metrics_file(tmp_path, name="b_metrics.json")
    metrics_file(tmp_path, name="a_metrics.json")
    write(tmp_path / "pipeline.json", {"identifier": "p", "metadata": {}})
    write(tmp_path / "broken.json", "{not json")
    write(tmp_path / "workflow.json", {"kind": "Workflow", "identifier": "w", "tasks": []})
    write(tmp_path / "notes.txt", "metrics")

    assert [item.name for item in load.metrics_files(tmp_path)] == ["a_metrics.json", "b_metrics.json"]
    assert [item.name for item in load.metrics_files(tmp_path / "b_metrics.json")] == ["b_metrics.json"]
    assert load.metrics_files(tmp_path / "pipeline.json") == []
    with pytest.raises(ConfigError, match="path not found"):
        load.metrics_files(tmp_path / "missing")


def test_a_folder_without_a_metrics_file_says_so_and_names_a_file_whose_kind_is_in_the_wrong_case(tmp_path):
    with pytest.raises(ConfigError,
                       match=r"there is no metrics file in .*\. A metrics file has \"kind\": \"Metrics\"\.$"):
        load.load_metrics(tmp_path)
    write(tmp_path / "metrics.json", {**METRICS, "kind": "metrics"})

    with pytest.raises(ConfigError) as error:
        load.load_metrics(tmp_path)

    assert str(error.value).endswith("metrics.json says kind 'metrics', and the kind is written 'Metrics'.")
    assert load.read_definitions(tmp_path)[0] is None
    with pytest.raises(ConfigError, match="is not a folder of configs"):
        load.read_definitions(tmp_path / "metrics.json")


def test_a_metrics_file_that_is_not_valid_fails_the_load_with_every_problem_and_its_file_name(tmp_path):
    spec = with_model(measures=[{"name": "revenue_sum", "agg": "median", "expr": "revenue"}]) | {
        "metrics": [{**METRICS["metrics"][0], "format": "euro"}]}
    metrics_file(tmp_path, spec)

    with pytest.raises(ConfigError) as error:
        load.load_metrics(tmp_path)

    lines = str(error.value).splitlines()
    assert lines[0] == f"the metrics of {tmp_path.resolve()} have 2 problems"
    assert lines[1].startswith("  - metrics.json: semantic_models[0].measures[0].agg: is required and must be one of")
    assert lines[2] == ("  - metrics.json: metrics[0].format: must be one of ['number', 'percent', 'currency'], "
                        "got 'euro'")
    definitions, problems = load.read_definitions(tmp_path)
    assert definitions is None and [item.removeprefix("metrics.json: ") for item in problems] == [
        item.removeprefix("  - metrics.json: ") for item in lines[1:]]


def test_two_files_in_a_folder_are_one_set_of_definitions_and_a_name_two_files_define_is_refused(tmp_path):
    metrics_file(tmp_path, name="a.json")
    other = {"apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "reviews", "who": "ops",
             "semantic_models": [{"name": "reviews", "table": "gold.gold_reviews",
                                  "measures": [{"name": "review_count", "agg": "count"}]}],
             "metrics": [{"name": "reviews", "type": "simple", "measure": "review_count",
                          "description": "How many reviews."}]}
    write(tmp_path / "b.json", other)

    definitions = load.load_metrics(tmp_path)

    assert [item.name for item in definitions.files] == ["a.json", "b.json"]
    assert [item.name for item in definitions.models] == ["daily_sales", "reviews"]
    assert definitions.metric("reviews").file == "b.json" and definitions.metric("revenue").file == "a.json"
    write(tmp_path / "b.json", {**other, "metrics": [{**other["metrics"][0], "name": "revenue"}]})
    found, problems = load.read_definitions(tmp_path)
    assert found is None and problems == [
        "b.json: the metric 'revenue' is also defined in a.json, and a name has to mean one thing across the folder"]
    write(tmp_path / "b.json", {**other, "semantic_models": [{**other["semantic_models"][0], "name": "daily_sales",
                                                              "measures": [{"name": "revenue_sum", "agg": "count"}]}],
                                "metrics": [{**other["metrics"][0], "measure": "revenue_sum"}]})
    found, problems = load.read_definitions(tmp_path)
    assert problems == [
        "b.json: the semantic model 'daily_sales' is also defined in a.json, and a name has to mean one thing "
        "across the folder",
        "b.json: the measure 'revenue_sum' is also defined in a.json, and a name has to mean one thing across the "
        "folder"]


def test_definitions_find_a_metric_and_say_the_valid_names_and_the_closest_one_when_it_is_not_there(lake):
    definitions = load.load_metrics(lake)

    assert definitions.metric("revenue").description == "Money taken."
    assert definitions.model("daily_sales").table == "gold.gold_sales"
    assert definitions.model_of(definitions.metric("revenue")).name == "daily_sales"
    assert [item.name for item in definitions.metrics_of(definitions.model("daily_sales"))][:2] == ["revenue", "orders"]
    names = [item["name"] for item in METRICS["metrics"]]
    with pytest.raises(ConfigError) as error:
        definitions.metric("revnue")
    assert str(error.value) == f"unknown metric 'revnue'. Did you mean 'revenue'? The metrics are {names}"
    with pytest.raises(ConfigError) as error:
        definitions.metric("xyz")
    assert str(error.value) == f"unknown metric 'xyz'. The metrics are {names}"
    with pytest.raises(ConfigError, match="unknown semantic model 'sales'. Did you mean 'daily_sales'"):
        definitions.model("sales")


def test_definitions_describe_a_metric_for_a_person_or_an_agent(lake):
    definitions = load.load_metrics(lake)

    assert definitions.describe(definitions.metric("avg_ticket")) == {
        "name": "avg_ticket", "description": "Revenue divided by orders.", "owner": "finance@example.com",
        "type": "ratio", "format": "currency", "table": "gold.gold_sales", "semantic_model": "daily_sales",
        "dimensions": ["city", "kitchen"], "time_column": "order_date", "grains": list(model.GRAINS),
        "file": "metrics.json", "numerator": "revenue_sum", "denominator": "order_sum"}
    simple = definitions.describe(definitions.metric("revenue"))
    assert simple["measure"] == "revenue_sum" and "numerator" not in simple
    assert simple["owner"] == "analytics@example.com"
    everything = definitions.to_dict()
    assert everything["files"] == ["metrics.json"] and everything["folder"] == str(lake.resolve())
    [semantic] = everything["semantic_models"]
    assert semantic["time_dimension"] == {"column": "order_date", "grains": list(model.GRAINS)}
    assert semantic["dimensions"][1] == {"name": "kitchen", "column": "cuisine", "description": "The kind of food."}
    assert semantic["measures"][3] == {"name": "row_count", "agg": "count", "expr": None, "description": None}
    assert [item["name"] for item in everything["metrics"]] == [item["name"] for item in METRICS["metrics"]]
    json.dumps(everything)


@pytest.mark.parametrize("reference, expected", [
    ("gold.gold_sales", "gold.gold_sales"), ("gold_sales", "gold.gold_sales"), (" gold_sales ", "gold.gold_sales"),
    ("silver.orders", "silver.orders"), ("orders", "silver.orders")])
def test_a_table_is_named_in_full_or_by_its_bare_name_when_only_one_table_has_it(reference, expected):
    assert load.resolve_table(reference, ["gold.gold_sales", "silver.orders"]) == expected


def test_a_table_that_is_not_a_gold_table_or_whose_bare_name_is_ambiguous_is_refused():
    with pytest.raises(ConfigError, match=r"'sales' is not a gold table of this folder. The gold tables are "
                                          r"\['gold.gold_sales'\]"):
        load.resolve_table("sales", ["gold.gold_sales"])
    with pytest.raises(ConfigError, match=r"'daily' is the name of more than one gold table, \['a.daily', 'b.daily'\]. "
                                          r"Write it as <namespace>.<table>"):
        load.resolve_table("daily", ["a.daily", "b.daily"])


def test_the_metrics_files_of_several_paths_are_one_set_and_none_is_none(lake, tmp_path_factory):
    empty = tmp_path_factory.mktemp("empty")

    assert load.load_all([empty]) is None and load.load_all([]) is None
    found = load.load_all([empty, lake, lake / "metrics.json"])
    assert [item.name for item in found.files] == ["metrics.json"] and len(found.metrics) == len(METRICS["metrics"])
    write(lake / "metrics.json", with_metric("revenue", format="euro"))
    with pytest.raises(ConfigError, match=r"have 1 problem\n  - metrics.json: metrics\[0\].format"):
        load.load_all([lake])


# --------------------------------------------------------------------------- the definitions against the tables

COLUMNS = "['order_date', 'city', 'cuisine', 'customer_id', 'orders', 'revenue', 'rating']"
HERE = "metrics.json: semantic model 'daily_sales'"


def fit(tmp_path, spec, schemas=None) -> list[str]:
    """The problems of a metrics file for the schema of the gold table, which are the columns of ``SCHEMA``."""
    metrics_file(tmp_path, spec)
    return check.check_definitions(load.load_metrics(tmp_path), {"gold.gold_sales": SCHEMA} if schemas is None
                                   else schemas)


def one_measure(name: str, agg: str, expr: str | None, **model_changes) -> dict:
    """A file whose one semantic model has one measure and whose one metric is that measure."""
    spec = with_model(measures=[{"name": name, "agg": agg, **({"expr": expr} if expr is not None else {})}],
                      **model_changes)
    spec["metrics"] = [{"name": "the_metric", "type": "simple", "measure": name, "description": "A number."}]
    return spec


def test_definitions_that_fit_the_table_have_no_problem(tmp_path):
    assert fit(tmp_path, METRICS) == []


def test_a_column_is_found_whatever_its_case_and_a_timestamp_is_a_time_dimension_as_well_as_a_date(tmp_path):
    spec = with_model(dimensions=[{"name": "city", "column": "CITY"}],
                      measures=[{"name": "revenue_sum", "agg": "sum", "expr": "REVENUE"}])
    spec["metrics"] = [METRICS["metrics"][0]]
    stamped = pa.schema([("ts", pa.timestamp("us")), ("CITY", pa.string()), ("revenue", pa.decimal128(18, 2))])
    spec["semantic_models"][0]["time_dimension"] = {"column": "TS", "grains": ["day"]}

    assert fit(tmp_path, spec, {"gold.gold_sales": stamped}) == []


@pytest.mark.parametrize("label, spec, expected", [
    ("a table that is not a gold table", with_model(table="gold.nope"),
     [f"{HERE}, table: 'gold.nope' is not a gold table of this folder. The gold tables are ['gold.gold_sales']"]),
    ("a dimension that is not a column", with_model(dimensions=["cuisin", "city"]),
     [f"{HERE}, dimension 'cuisin': the column 'cuisin' is not in gold.gold_sales. Did you mean 'cuisine'? The "
      f"columns are {COLUMNS}"]),
    ("a dimension of another name than its column", with_model(dimensions=[{"name": "kitchen", "column": "cusine"}]),
     [f"{HERE}, dimension 'kitchen': the column 'cusine' is not in gold.gold_sales. Did you mean 'cuisine'? The "
      f"columns are {COLUMNS}"]),
    ("a time column that is not a column",
     with_model(time_dimension={"column": "odate", "grains": ["day"]}),
     [f"{HERE}, time dimension: the column 'odate' is not in gold.gold_sales. Did you mean 'order_date'? The "
      f"columns are {COLUMNS}"]),
    ("a time column that is text", with_model(time_dimension={"column": "customer_id", "grains": ["day"]}),
     [f"{HERE}, time dimension: the column 'customer_id' of gold.gold_sales is a VARCHAR, and a time dimension has "
      "to be a DATE or a TIMESTAMP. Make it a DATE or a TIMESTAMP in the gold table."]),
    ("a measure that reads a column that is not there", one_measure("net", "sum", "net_revenu"),
     [f"{HERE}, measure 'net': the expression 'net_revenu' reads the column 'net_revenu', which is not in "
      f"gold.gold_sales. Did you mean 'revenue'? The columns are {COLUMNS}"]),
    ("a measure that reads two columns that are not there", one_measure("net", "sum", "alpha + beta"),
     [f"{HERE}, measure 'net': the expression 'alpha + beta' reads the column 'alpha', which is not in "
      f"gold.gold_sales. The columns are {COLUMNS}",
      f"{HERE}, measure 'net': the expression 'alpha + beta' reads the column 'beta', which is not in "
      f"gold.gold_sales. The columns are {COLUMNS}"]),
    ("an expression that is not SQL", one_measure("net", "sum", "revenue -"),
     [f"{HERE}, measure 'net': the expression 'revenue -' is not a SQL expression: syntax error at or near \")\""]),
    ("an expression with a subquery", one_measure("net", "sum", "(SELECT max(revenue) FROM gold_sales)"),
     [f"{HERE}, measure 'net': the expression '(SELECT max(revenue) FROM gold_sales)' has a subquery, and a measure "
      "is an expression of the columns of one row"]),
    ("an expression with an aggregate of its own", one_measure("net", "sum", "sum(revenue)"),
     [f"{HERE}, measure 'net': the expression 'sum(revenue)' calls the aggregate sum(), and the measure already "
      "aggregates with its agg. An aggregate in an aggregate cannot work"]),
    ("an expression with a window function", one_measure("net", "sum", "sum(revenue) OVER ()"),
     [f"{HERE}, measure 'net': the expression 'sum(revenue) OVER ()' has a window function, and a measure is an "
      "expression of the columns of one row"]),
    ("an expression that is a star", one_measure("net", "count", "*"),
     [f"{HERE}, measure 'net': the expression '*' has a star, and a measure is an expression of the columns of one "
      "row"]),
    ("an expression that names a table", one_measure("net", "sum", "t.revenue"),
     [f"{HERE}, measure 'net': the expression 't.revenue' names the column 't.revenue' with a table in front. "
      "Write the column alone, because the query chooses the table"]),
    ("an expression that closes the parenthesis and adds a clause",
     one_measure("net", "sum", "revenue) FROM gold_sales WHERE (1=1) --"),
     [f"{HERE}, measure 'net': the expression 'revenue) FROM gold_sales WHERE (1=1) --' is more than one "
      "expression, and a measure is one SQL expression"]),
    ("an expression that closes the parenthesis and breaks the rest",
     one_measure("net", "sum", "revenue) FROM gold_sales WHERE (1=1"),
     [f"{HERE}, measure 'net': the expression 'revenue) FROM gold_sales WHERE (1=1' is not a SQL expression: syntax "
      "error at or near \"AS\""]),
    ("an expression that makes a union", one_measure("net", "sum", "1) UNION SELECT (2"),
     [f"{HERE}, measure 'net': the expression '1) UNION SELECT (2' is more than one expression, and a measure is "
      "one SQL expression"]),
    ("an expression that is two expressions", one_measure("net", "sum", "revenue), (orders"),
     [f"{HERE}, measure 'net': the expression 'revenue), (orders' is more than one expression, and a measure is one "
      "SQL expression"]),
    ("a sum of text", one_measure("net", "sum", "city"),
     [f"{HERE}, measure 'net': sum of 'city' does not work on gold.gold_sales. Binder Error: No function matches the "
      "given name and argument types 'sum(VARCHAR)'. You might need to add explicit type casts."]),
    ("a metric that is a date", one_measure("latest", "max", "order_date"),
     ["metrics.json: metric 'the_metric': the measure 'latest' is a DATE, and a metric is a number. Aggregate a "
      "number, or count rows"]),
    ("everything that is wrong at once", with_model(
        dimensions=["cuisin"], time_dimension={"column": "odate", "grains": ["day"]},
        measures=[{"name": "revenue_sum", "agg": "sum", "expr": "net"},
                  {"name": "x_sum", "agg": "sum", "expr": "city"}]) | {
        "metrics": [METRICS["metrics"][0]]},
     [f"{HERE}, time dimension: the column 'odate' is not in gold.gold_sales. Did you mean 'order_date'? The "
      f"columns are {COLUMNS}",
      f"{HERE}, dimension 'cuisin': the column 'cuisin' is not in gold.gold_sales. Did you mean 'cuisine'? The "
      f"columns are {COLUMNS}",
      f"{HERE}, measure 'revenue_sum': the expression 'net' reads the column 'net', which is not in "
      f"gold.gold_sales. The columns are {COLUMNS}",
      f"{HERE}, measure 'x_sum': sum of 'city' does not work on gold.gold_sales. Binder Error: No function matches "
      "the given name and argument types 'sum(VARCHAR)'. You might need to add explicit type casts."]),
], ids=lambda value: value if isinstance(value, str) and len(value) < 60 else "")
def test_definitions_that_do_not_fit_the_table_are_all_listed_with_the_model_and_the_measure(
        tmp_path, label, spec, expected):
    assert fit(tmp_path, spec) == expected, label


def test_a_time_column_with_a_time_zone_is_refused_and_the_message_says_how_to_fix_it(tmp_path):
    zoned = pa.schema([("ts", pa.timestamp("us", tz="UTC")), ("revenue", pa.decimal128(18, 2))])
    spec = one_measure("net", "sum", "revenue", time_dimension={"column": "ts", "grains": ["day"]},
                       dimensions=None)

    assert fit(tmp_path, spec, {"gold.gold_sales": zoned}) == [
        f"{HERE}, time dimension: the column 'ts' of gold.gold_sales is a TIMESTAMP WITH TIME ZONE, and a time "
        "dimension has to be a DATE or a TIMESTAMP. Cast it to a TIMESTAMP in the gold table, so that the time zone "
        "of a day is decided once."]


def test_a_table_that_does_not_exist_yet_cannot_be_checked_and_says_to_build_it(tmp_path):
    assert fit(tmp_path, METRICS, {"gold.gold_sales": None}) == [
        f"{HERE}, table: gold.gold_sales does not exist yet, so its columns cannot be checked. Build it first, for "
        "instance with ldp gold build"]


def test_a_ratio_of_a_measure_that_is_not_a_number_is_refused(tmp_path):
    spec = with_model(measures=[{"name": "first_day", "agg": "min", "expr": "order_date"},
                                {"name": "row_count", "agg": "count"}])
    spec["metrics"] = [{"name": "odd", "type": "ratio", "numerator": "first_day", "denominator": "row_count",
                        "description": "A date over a count."}]

    assert fit(tmp_path, spec) == ["metrics.json: metric 'odd': the measure 'first_day' is a DATE, and a metric is a "
                                   "number. Aggregate a number, or count rows"]


def test_a_count_of_rows_and_a_count_distinct_are_numbers_whatever_they_count(tmp_path):
    spec = with_model(measures=[{"name": "kinds", "agg": "count_distinct", "expr": "cuisine"},
                                {"name": "row_count", "agg": "count"},
                                {"name": "firsts", "agg": "count", "expr": "order_date"}])
    spec["metrics"] = [{"name": "kinds", "type": "simple", "measure": "kinds", "description": "Kinds of food."},
                       {"name": "rows", "type": "simple", "measure": "row_count", "description": "Rows."},
                       {"name": "dated", "type": "simple", "measure": "firsts", "description": "Dated rows."}]

    assert fit(tmp_path, spec) == []


def test_the_schemas_of_the_gold_tables_of_a_folder_are_read_and_a_table_that_is_not_built_has_none(tmp_path):
    sales(tmp_path)
    write(tmp_path / "gold_later.json", {"identifier": "gold_later", "who": "ops", "metadata": {
        "source": {"name": "gold_later", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "gold_later", "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": "Not built yet.", "grain": "one row", "columns": {"a": "A."}}}})

    schemas = check.table_schemas(tmp_path)

    assert sorted(schemas) == ["gold.gold_later", "gold.gold_sales"]
    assert schemas["gold.gold_later"] is None
    assert [field.name for field in schemas["gold.gold_sales"]] == SCHEMA.names
    assert schemas["gold.gold_sales"].field("revenue").type == pa.decimal128(18, 2)


def test_a_folder_is_checked_as_the_cli_does_and_a_file_that_is_not_valid_stops_the_check_of_the_tables(lake):
    result = check.validate_folder(lake)

    assert result.valid and result.problems == [] and result.files == ("metrics.json",)
    assert result.definitions is not None and len(result.definitions.metrics) == len(METRICS["metrics"])
    assert result.to_dict() == {"folder": str(lake.resolve()), "files": ["metrics.json"], "valid": True,
                                "semantic_models": 1, "metrics": len(METRICS["metrics"]), "problems": []}
    metrics_file(lake, with_metric("revenue", format="euro"))
    broken = check.validate_folder(lake)
    assert not broken.valid and broken.definitions is None
    assert broken.problems == ["metrics.json: metrics[0].format: must be one of ['number', 'percent', 'currency'], "
                               "got 'euro'"]
    assert broken.to_dict()["semantic_models"] == 0 and broken.to_dict()["valid"] is False


def test_a_folder_with_no_metrics_file_is_not_valid_and_has_no_metrics(tmp_path):
    sales(tmp_path)

    result = check.validate_folder(tmp_path)

    assert not result.valid and result.files == () and not check.has_metrics(tmp_path)
    assert result.problems[0].startswith("there is no metrics file in ")
    metrics_file(tmp_path)
    assert check.has_metrics(tmp_path)


def test_checking_a_folder_with_no_duckdb_says_how_to_install_it(lake, monkeypatch):
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("no duckdb here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(Exception) as error:
        check.validate_folder(lake)

    assert type(error.value).__name__ == "EngineNotFound"
    assert str(error.value) == (
        'Metrics need the duckdb package. Install it with: pip install "local-data-platform[duckdb]"')


# --------------------------------------------------------------------------- compiling a query

TYPES = {"order_date": "DATE", "city": "VARCHAR", "cuisine": "VARCHAR", "customer_id": "VARCHAR", "orders": "BIGINT",
         "revenue": "DECIMAL(18,2)", "rating": "DOUBLE"}


@pytest.fixture
def definitions(lake):
    return load.load_metrics(lake)


def compile_sales(definitions, metrics, **options):
    return compiler.compile_query(definitions, metrics, table="gold.gold_sales", types=TYPES, **options)


def test_a_metric_with_nothing_else_is_one_aggregate_over_the_table(definitions):
    compiled = compile_sales(definitions, "revenue")

    assert compiled.sql == 'SELECT\n  sum(revenue) AS "revenue"\nFROM "gold"."gold_sales"'
    assert compiled.parameters == () and compiled.grain is None and compiled.table == "gold.gold_sales"
    assert [item.to_dict() for item in compiled.columns] == [
        {"name": "revenue", "kind": "metric", "format": "currency", "description": "Money taken."}]
    assert compiled.explain() == compiled.sql


def test_a_ratio_is_the_sum_of_one_measure_over_the_sum_of_another_and_a_zero_denominator_is_no_value(definitions):
    compiled = compile_sales(definitions, "avg_ticket")

    assert compiled.sql == 'SELECT\n  sum(revenue) / NULLIF(sum(orders), 0) AS "avg_ticket"\nFROM "gold"."gold_sales"'


def test_a_query_with_everything_groups_by_the_grain_and_the_dimensions_and_binds_every_value(definitions):
    compiled = compile_sales(definitions, "revenue,avg_ticket", by="kitchen,city", grain="month",
                             filters=["city=Pune", "kitchen=Thai"], start="2026-01-01", end="2026-03-31")

    assert compiled.sql == "\n".join([
        "SELECT",
        '  CAST(date_trunc(\'month\', "order_date") AS DATE) AS "month",',
        '  "cuisine" AS "kitchen",',
        '  "city",',
        '  sum(revenue) AS "revenue",',
        '  sum(revenue) / NULLIF(sum(orders), 0) AS "avg_ticket"',
        'FROM "gold"."gold_sales"',
        'WHERE "city" = $1 AND "cuisine" = $2 AND "order_date" >= $3 AND "order_date" <= $4',
        "GROUP BY 1, 2, 3",
        "ORDER BY 1, 2, 3"])
    assert compiled.parameters == ("Pune", "Thai", dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    assert [(item.name, item.kind, item.format) for item in compiled.columns] == [
        ("month", "time", None), ("kitchen", "dimension", None), ("city", "dimension", None),
        ("revenue", "metric", "currency"), ("avg_ticket", "metric", "currency")]
    assert compiled.explain().splitlines()[-4:] == [
        '-- $1 = "Pune"', '-- $2 = "Thai"', "-- $3 = DATE '2026-01-01'", "-- $4 = DATE '2026-03-31'"]
    assert compiled.to_dict()["parameters"] == ["Pune", "Thai", "2026-01-01", "2026-03-31"]
    assert compiled.to_dict()["metrics"] == ["revenue", "avg_ticket"] and compiled.to_dict()["grain"] == "month"


@pytest.mark.parametrize("metric, aggregate", [
    ("revenue", "sum(revenue)"), ("customers", "count(DISTINCT customer_id)"), ("batches", "count(*)"),
    ("rated", "count(rating)"), ("avg_rating", "avg(rating)"), ("biggest_batch", "max(revenue)"),
    ("smallest_batch", "min(orders)"), ("cents", "sum(CAST(revenue * 100 AS BIGINT))")])
def test_each_aggregation_is_written_as_its_own_sql(definitions, metric, aggregate):
    assert compile_sales(definitions, metric).sql.splitlines()[1] == f'  {aggregate} AS "{metric}"'


def test_the_same_dimension_asked_for_twice_is_grouped_once_and_filters_of_one_dimension_are_alternatives(definitions):
    compiled = compile_sales(definitions, ["revenue", "revenue"], by=["city", "city,kitchen"], grain="day",
                             filters={"city": ["Pune", "Delhi"], "kitchen": "Thai"})

    assert compiled.sql.splitlines()[-3:] == ['WHERE "city" IN ($1, $2) AND "cuisine" = $3', "GROUP BY 1, 2, 3",
                                              "ORDER BY 1, 2, 3"]
    assert compiled.parameters == ("Pune", "Delhi", "Thai") and [item.name for item in compiled.metrics] == ["revenue"]
    assert [item.name for item in compiled.columns] == ["day", "city", "kitchen", "revenue"]


def test_a_filter_value_may_hold_an_equals_sign_a_comma_and_quotes_and_stays_a_value(definitions):
    compiled = compile_sales(definitions, "revenue", filters=["city=a=b, c'd\"e"])

    assert compiled.parameters == ("a=b, c'd\"e",) and "a=b" not in compiled.sql
    assert compiled.sql.splitlines()[-1] == 'WHERE "city" = $1'


@pytest.mark.parametrize("grain, truncated", [("day", "day"), ("week", "week"), ("month", "month"),
                                              ("quarter", "quarter"), ("year", "year")])
def test_each_grain_is_the_start_of_the_period_as_a_date(definitions, grain, truncated):
    compiled = compile_sales(definitions, "revenue", grain=grain)

    assert compiled.sql.splitlines()[1] == f'  CAST(date_trunc(\'{truncated}\', "order_date") AS DATE) AS "{grain}",'


def test_the_end_day_includes_the_whole_day_for_a_timestamp_column_and_the_day_itself_for_a_date(definitions):
    stamped = {**TYPES, "order_date": "TIMESTAMP"}

    compiled = compiler.compile_query(definitions, "revenue", table="gold.gold_sales", types=stamped,
                                      start="2026-01-01", end="2026-03-31")

    assert compiled.sql.splitlines()[-1] == 'WHERE "order_date" >= $1 AND "order_date" < $2'
    assert compiled.parameters == (dt.datetime(2026, 1, 1), dt.datetime(2026, 4, 1))
    only_start = compile_sales(definitions, "revenue", start="2026-02-01")
    assert only_start.sql.splitlines()[-1] == 'WHERE "order_date" >= $1'
    assert only_start.parameters == (dt.date(2026, 2, 1),)
    only_end = compile_sales(definitions, "revenue", end=dt.date(2026, 2, 1))
    assert only_end.sql.splitlines()[-1] == 'WHERE "order_date" <= $1' and only_end.parameters == (dt.date(2026, 2, 1),)


def test_a_time_column_that_is_not_a_date_or_a_timestamp_is_refused_when_a_query_needs_it(definitions):
    zoned = {**TYPES, "order_date": "TIMESTAMP WITH TIME ZONE"}

    with pytest.raises(ConfigError, match="the column 'order_date' of the time dimension of daily_sales is a TIMESTAMP "
                                          "WITH TIME ZONE, and a time dimension has to be a DATE or a TIMESTAMP"):
        compiler.compile_query(definitions, "revenue", table="gold.gold_sales", types=zoned, grain="day")
    with pytest.raises(ConfigError, match="is a VARCHAR"):
        compiler.compile_query(definitions, "revenue", table="gold.gold_sales", start="2026-01-01",
                               types={**TYPES, "order_date": "VARCHAR"})
    with pytest.raises(ConfigError, match=r"the column 'city' of the dimension 'city' is not in gold.gold_sales"):
        compiler.compile_query(definitions, "revenue", table="gold.gold_sales", types={"orders": "BIGINT"}, by="city")


def test_an_unknown_metric_or_dimension_is_refused_with_the_valid_names(definitions):
    names = [item["name"] for item in METRICS["metrics"]]
    with pytest.raises(ConfigError) as error:
        compile_sales(definitions, "revenue,avg_tikcet")
    assert str(error.value) == f"unknown metric 'avg_tikcet'. Did you mean 'avg_ticket'? The metrics are {names}"
    with pytest.raises(ConfigError) as error:
        compile_sales(definitions, "revenue", by="cuisine")
    assert str(error.value) == ("unknown dimension 'cuisine'. The dimensions are ['city', 'kitchen']. They are the "
                                "dimensions of daily_sales")
    with pytest.raises(ConfigError) as error:
        compile_sales(definitions, "revenue", by="citty")
    assert str(error.value).startswith("unknown dimension 'citty'. Did you mean 'city'? The dimensions are")
    with pytest.raises(ConfigError) as error:
        compile_sales(definitions, "revenue", filters=["cuisine=Thai"])
    assert str(error.value) == ("unknown dimension 'cuisine'. The dimensions are ['city', 'kitchen']. They are the "
                                "dimensions of daily_sales, and a filter works on a dimension")


def test_at_least_one_metric_is_asked_for_and_a_name_is_never_empty(definitions):
    with pytest.raises(ConfigError, match=r"ask for at least one metric. The metrics are \['revenue', 'orders'"):
        compile_sales(definitions, [])
    with pytest.raises(ConfigError, match=r"the metrics has an empty name in 'revenue,,orders'"):
        compile_sales(definitions, "revenue,,orders")
    with pytest.raises(ConfigError, match=r"--by has an empty name in ''"):
        compile_sales(definitions, "revenue", by="")
    with pytest.raises(ConfigError, match=r"the metrics has to be names, got 5"):
        compile_sales(definitions, [5])
    assert compiler.split_names(None, "x") == [] and compiler.split_names(" a , b,a ", "x") == ["a", "b"]


def test_metrics_of_two_semantic_models_are_refused_with_the_table_of_each_because_there_is_no_join(tmp_path):
    spec = two_models()
    spec["metrics"].append({"name": "others", "type": "simple", "measure": "other_sum", "description": "Others."})
    metrics_file(tmp_path, spec)
    found = load.load_metrics(tmp_path)

    with pytest.raises(ConfigError) as error:
        compiler.compile_query(found, "revenue,orders,others", table="gold.gold_sales", types=TYPES)

    assert str(error.value) == ("the metrics are on more than one semantic model, 'revenue' and 'orders' on "
                                "daily_sales "
                                "(gold.gold_sales); 'others' on other (gold.other). A query reads one table, so ask "
                                "for them in separate queries")


@pytest.mark.parametrize("options, message", [
    ({"grain": "fortnight"}, "unknown grain 'fortnight'. The grains are ['day', 'week', 'month', 'quarter', 'year']"),
    ({"start": "2026-4-1"}, "--from has to be a date such as 2026-01-31, got '2026-4-1'"),
    ({"end": "tomorrow"}, "--to has to be a date such as 2026-01-31, got 'tomorrow'"),
    ({"start": "2026-02-30"}, "--from has to be a date such as 2026-01-31, got '2026-02-30'"),
    ({"start": dt.datetime(2026, 1, 1, 5)}, "--from has to be a date such as 2026-01-31, got "
                                            "datetime.datetime(2026, 1, 1, 5, 0)"),
    ({"start": "2026-04-01", "end": "2026-03-31"}, "--from 2026-04-01 is after --to 2026-03-31"),
    ({"filters": ["city"]}, "a filter is written dimension=value, got 'city'"),
    ({"filters": ["=Pune"]}, "a filter is written dimension=value, got '=Pune'"),
])
def test_a_grain_a_day_or_a_filter_that_is_not_one_is_refused_and_says_what_it_should_be(definitions, options, message):
    with pytest.raises(ConfigError) as error:
        compile_sales(definitions, "revenue", **options)

    assert str(error.value) == message


def defined(tmp_path, spec) -> load.Definitions:
    metrics_file(tmp_path, spec)
    return load.load_metrics(tmp_path)


def test_a_time_dimension_that_allows_only_month_gives_the_metric_only_by_month(tmp_path):
    found = defined(tmp_path, with_model(time_dimension={"column": "order_date", "grains": ["month"]}))

    assert compile_sales(found, "revenue", grain="month").sql.splitlines()[1].endswith('AS "month",')
    with pytest.raises(ConfigError) as error:
        compile_sales(found, "revenue,orders", grain="quarter")
    assert str(error.value) == ("'revenue' and 'orders' cannot be reported by quarter. The time dimension of "
                                "daily_sales allows the grains ['month']")
    with pytest.raises(ConfigError) as error:
        compile_sales(found, "revenue", by="city")
    assert str(error.value) == (
        "'revenue' cannot be added up over the whole period, because the time dimension of daily_sales does not "
        "allow the year grain, and adding up the years is the same step. Ask for one of the grains ['month'] with "
        "--grain")
    with pytest.raises(ConfigError, match="cannot be added up over the whole period"):
        compile_sales(found, "revenue", start="2026-01-01", end="2026-12-31")


def test_the_whole_period_is_one_row_when_year_is_allowed_and_a_coarser_grain_than_allowed_is_refused(tmp_path):
    found = defined(tmp_path, with_model(time_dimension={"column": "order_date", "grains": ["day", "year"]}))

    assert compile_sales(found, "revenue", by="city").sql.splitlines()[-1] == 'ORDER BY 1'
    with pytest.raises(ConfigError, match=r"cannot be reported by month. The time dimension of daily_sales allows the "
                                          r"grains \['day', 'year'\]"):
        compile_sales(found, "revenue", grain="month")
    partial = defined(tmp_path, with_model(time_dimension={"column": "order_date", "grains": ["day", "week", "month"]}))
    with pytest.raises(ConfigError, match=r"Ask for one of the grains \['day', 'week', 'month'\] with --grain"):
        compile_sales(partial, "revenue")


def test_a_table_with_no_time_dimension_has_no_grain_and_no_range_of_days(tmp_path):
    found = defined(tmp_path, with_model(time_dimension=None))

    assert compile_sales(found, "revenue", by="city").sql.splitlines()[-1] == "ORDER BY 1"
    for options in ({"grain": "month"}, {"start": "2026-01-01"}, {"end": "2026-01-01"}):
        with pytest.raises(ConfigError) as error:
            compile_sales(found, "revenue,orders", **options)
        assert str(error.value) == ("daily_sales has no time dimension, so a grain or a range of days does not apply "
                                    "to 'revenue' and 'orders'")


# --------------------------------------------------------------------------- the values of a filter


@pytest.mark.parametrize("text, kind, value", [
    ("Pune", "VARCHAR", "Pune"), (" Pune ", "VARCHAR", " Pune "), ("", "VARCHAR", ""), ("7", "TINYINT", 7),
    ("-12", "BIGINT", -12), ("+3", "INTEGER", 3), (" 4 ", "HUGEINT", 4), ("12.50", "DECIMAL(18,2)", D("12.50")),
    ("-.5", "DECIMAL(3,1)", D("-.5")), ("2.5e3", "DOUBLE", 2500.0), ("1", "FLOAT", 1.0),
    ("true", "BOOLEAN", True), ("FALSE", "BOOLEAN", False), ("2026-01-31", "DATE", dt.date(2026, 1, 31)),
    ("2026-01-31", "TIMESTAMP", dt.datetime(2026, 1, 31)),
    ("2026-01-31 18:30", "TIMESTAMP", dt.datetime(2026, 1, 31, 18, 30)),
    ("2026-01-31T18:30:15.250", "TIMESTAMP_MS", dt.datetime(2026, 1, 31, 18, 30, 15, 250000))])
def test_a_filter_value_is_turned_into_a_value_of_the_type_of_its_column(text, kind, value):
    found = compiler.coerce(text, kind, "d")

    assert found == value and type(found) is type(value)


@pytest.mark.parametrize("text, kind, message", [
    ("four", "INTEGER", "the dimension 'd' is a INTEGER, so a filter value has to be a whole number, got 'four'"),
    ("1_0", "BIGINT", "the dimension 'd' is a BIGINT, so a filter value has to be a whole number, got '1_0'"),
    ("4.5", "INTEGER", "the dimension 'd' is a INTEGER, so a filter value has to be a whole number, got '4.5'"),
    ("a", "DECIMAL(5,1)", "the dimension 'd' is a DECIMAL(5,1), so a filter value has to be a number, got 'a'"),
    ("nan", "DOUBLE", "the dimension 'd' is a DOUBLE, so a filter value has to be a number, got 'nan'"),
    ("yes", "BOOLEAN", "the dimension 'd' is a BOOLEAN, so a filter value has to be true or false, got 'yes'"),
    ("31/01/2026", "DATE", "the filter value of 'd' has to be a date such as 2026-01-31, got '31/01/2026'"),
    ("2026-13-01", "DATE", "the filter value of 'd' has to be a date such as 2026-01-31, got '2026-13-01'"),
    ("noon", "TIMESTAMP", "the dimension 'd' is a TIMESTAMP, so a filter value has to be a date or a time such as "
                          "2026-01-31 or 2026-01-31 18:30, got 'noon'"),
    ("2026-01-31 25:00", "TIMESTAMP", "the dimension 'd' is a TIMESTAMP, so a filter value has to be a date or a time "
                                      "such as 2026-01-31 or 2026-01-31 18:30, got '2026-01-31 25:00'"),
    ("x", "INTEGER[]", "the dimension 'd' is a INTEGER[], and a filter works on text, numbers, booleans, dates and "
                       "timestamps"),
    ("x", "STRUCT(a INTEGER)", "the dimension 'd' is a STRUCT(a INTEGER), and a filter works on text, numbers, "
                               "booleans, dates and timestamps"),
    ("x", "UUID", "the dimension 'd' is a UUID, and a filter works on text, numbers, booleans, dates and timestamps")])
def test_a_filter_value_that_does_not_fit_its_column_says_what_the_column_is_and_what_is_wanted(text, kind, message):
    with pytest.raises(ConfigError) as error:
        compiler.coerce(text, kind, "d")

    assert str(error.value) == message


@pytest.mark.parametrize("items, expected", [
    (None, []), ([], []), (["city=Pune"], [("city", ["Pune"])]),
    (["city=Pune", "kitchen=Thai", "city=Delhi"], [("city", ["Pune", "Delhi"]), ("kitchen", ["Thai"])]),
    ("city=Pune", [("city", ["Pune"])]), ({"city": "Pune"}, [("city", ["Pune"])]),
    ({"city": ["Pune", "Delhi"], "n": 5, "ok": True}, [("city", ["Pune", "Delhi"]), ("n", ["5"]), ("ok", ["True"])]),
    ({"day": dt.date(2026, 1, 2)}, [("day", ["2026-01-02"])]), ([" city =x"], [("city", ["x"])]),
    (["city="], [("city", [""])])])
def test_filters_are_read_from_dimension_equals_value_items_or_from_a_mapping(items, expected):
    assert compiler.parse_filters(items) == expected


# --------------------------------------------------------------------------- the answers

MONTH = "CAST(date_trunc('{grain}', order_date) AS DATE)"
CASES = [
    ("revenue", {"metrics": "revenue"}, "SELECT sum(revenue) FROM t"),
    ("orders", {"metrics": "orders"}, "SELECT sum(orders) FROM t"),
    ("avg_ticket", {"metrics": "avg_ticket"}, "SELECT sum(revenue) / sum(orders) FROM t"),
    ("customers", {"metrics": "customers"}, "SELECT count(DISTINCT customer_id) FROM t"),
    ("batches", {"metrics": "batches"}, "SELECT count(*) FROM t"),
    ("rated", {"metrics": "rated"}, "SELECT count(rating) FROM t"),
    ("avg_rating", {"metrics": "avg_rating"}, "SELECT avg(rating) FROM t"),
    ("biggest_batch", {"metrics": "biggest_batch"}, "SELECT max(revenue) FROM t"),
    ("smallest_batch", {"metrics": "smallest_batch"}, "SELECT min(orders) FROM t"),
    ("cents", {"metrics": "cents"}, "SELECT sum(CAST(revenue * 100 AS BIGINT)) FROM t"),
    ("rating_share", {"metrics": "rating_share"}, "SELECT count(rating) / count(*) FROM t"),
    ("every metric at once", {"metrics": [item["name"] for item in METRICS["metrics"]]},
     "SELECT sum(revenue), sum(orders), sum(revenue) / sum(orders), count(DISTINCT customer_id), count(*), "
     "count(rating), avg(rating), max(revenue), min(orders), sum(CAST(revenue * 100 AS BIGINT)), "
     "count(rating) / count(*) FROM t"),
    ("by day", {"metrics": "revenue", "grain": "day"},
     f"SELECT {MONTH.format(grain='day')}, sum(revenue) FROM t GROUP BY 1 ORDER BY 1"),
    ("by week", {"metrics": "revenue,orders", "grain": "week"},
     f"SELECT {MONTH.format(grain='week')}, sum(revenue), sum(orders) FROM t GROUP BY 1 ORDER BY 1"),
    ("by month", {"metrics": "revenue,avg_ticket", "grain": "month"},
     f"SELECT {MONTH.format(grain='month')}, sum(revenue), sum(revenue) / NULLIF(sum(orders), 0) FROM t "
     "GROUP BY 1 ORDER BY 1"),
    ("by quarter", {"metrics": "orders", "grain": "quarter"},
     f"SELECT {MONTH.format(grain='quarter')}, sum(orders) FROM t GROUP BY 1 ORDER BY 1"),
    ("by year", {"metrics": "customers,batches", "grain": "year"},
     f"SELECT {MONTH.format(grain='year')}, count(DISTINCT customer_id), count(*) FROM t GROUP BY 1 ORDER BY 1"),
    ("by a dimension", {"metrics": "revenue", "by": "city"},
     "SELECT city, sum(revenue) FROM t GROUP BY 1 ORDER BY 1"),
    ("by a dimension of another name", {"metrics": "orders", "by": "kitchen"},
     "SELECT cuisine, sum(orders) FROM t GROUP BY 1 ORDER BY 1"),
    ("by month and two dimensions", {"metrics": "revenue,avg_ticket", "by": "kitchen,city", "grain": "month"},
     f"SELECT {MONTH.format(grain='month')}, cuisine, city, sum(revenue), sum(revenue) / NULLIF(sum(orders), 0) "
     "FROM t GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"),
    ("one filter", {"metrics": "revenue,orders", "filters": ["city=Pune"]},
     "SELECT sum(revenue), sum(orders) FROM t WHERE city = ?", "Pune"),
    ("a filter with two values", {"metrics": "revenue", "by": "city", "filters": ["city=Pune", "city=Delhi"]},
     "SELECT city, sum(revenue) FROM t WHERE city IN (?, ?) GROUP BY 1 ORDER BY 1", "Pune", "Delhi"),
    ("two filters", {"metrics": "avg_ticket", "filters": ["city=Pune", "kitchen=Italian"]},
     "SELECT sum(revenue) / sum(orders) FROM t WHERE city = ? AND cuisine = ?", "Pune", "Italian"),
    ("a range of days", {"metrics": "revenue", "start": "2026-01-04", "end": "2026-03-31"},
     "SELECT sum(revenue) FROM t WHERE order_date >= ? AND order_date <= ?", dt.date(2026, 1, 4), dt.date(2026, 3, 31)),
    ("a range with one end", {"metrics": "orders", "grain": "year", "start": "2026-12-31"},
     f"SELECT {MONTH.format(grain='year')}, sum(orders) FROM t WHERE order_date >= ? GROUP BY 1 ORDER BY 1",
     dt.date(2026, 12, 31)),
    ("everything together", {"metrics": "revenue,avg_ticket,customers", "by": "city", "grain": "quarter",
                             "filters": ["kitchen=Thai"], "start": "2026-01-01", "end": "2026-12-31"},
     f"SELECT {MONTH.format(grain='quarter')}, city, sum(revenue), sum(revenue) / NULLIF(sum(orders), 0), "
     "count(DISTINCT customer_id) FROM t WHERE cuisine = ? AND order_date >= ? AND order_date <= ? "
     "GROUP BY 1, 2 ORDER BY 1, 2", "Thai", dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
]


@pytest.mark.parametrize("label, options, hand_sql, params", [(item[0], item[1], item[2], item[3:]) for item in CASES],
                         ids=[item[0] for item in CASES])
def test_every_metric_gives_the_rows_of_the_sql_that_was_written_by_hand(lake, label, options, hand_sql, params):
    found = metrics.query(lake, **options)

    assert close(answer(found), hand(hand_sql, *params)), label


def test_the_answer_is_a_pyarrow_table_with_the_grain_the_dimensions_and_the_metrics_in_that_order(lake):
    found = metrics.query(lake, "revenue,avg_ticket,customers", by=["kitchen", "city"], grain="month")

    assert isinstance(found, pa.Table) and found.column_names == ["month", "kitchen", "city", "revenue", "avg_ticket",
                                                                  "customers"]
    assert pa.types.is_date(found.schema.field("month").type) and pa.types.is_string(found.schema.field("city").type)
    assert pa.types.is_decimal(found.schema.field("revenue").type) and pa.types.is_floating(
        found.schema.field("avg_ticket").type) and pa.types.is_integer(found.schema.field("customers").type)
    assert found.num_rows == 7 and found.column("month")[0].as_py() == dt.date(2026, 1, 1)


def test_a_ratio_is_the_sum_over_the_sum_and_not_the_average_of_the_ratios_of_the_rows(lake):
    pune = answer(metrics.query(lake, "avg_ticket", filters=["city=Pune"], grain="month", start="2026-01-01",
                                end="2026-01-31"))
    january = answer(metrics.query(lake, "avg_ticket", grain="month", end="2026-01-31"))
    total = answer(metrics.query(lake, "avg_ticket"))

    # January in Pune has the rows 10 orders for 100.50 and 5 orders for 75.00. The ratio of the sums is 175.50 over 15,
    # which is 11.70. The average of the ratios of the rows, 10.05 and 15.00, would be 12.525.
    assert pune == [(dt.date(2026, 1, 1), pytest.approx(11.7))]
    assert january == [(dt.date(2026, 1, 1), pytest.approx(225.75 / 20))] and 225.75 / 20 == 11.2875
    # Everything is 461.75 over 36 orders. The average of the ratios of the eight rows that have orders is 13.2625.
    assert total == [(pytest.approx(461.75 / 36),)] and 461.75 / 36 != 13.2625
    assert total[0][0] == pytest.approx(12.826388888888889)


def test_a_ratio_with_a_denominator_of_zero_has_no_value_and_not_infinity(lake):
    found = answer(metrics.query(lake, "avg_ticket,orders,revenue", grain="day", start="2026-02-02",
                                 end="2026-02-02"))

    assert found == [(dt.date(2026, 2, 2), None, 0, D("0.00"))]


def test_a_sum_over_no_row_is_no_value_and_a_count_over_no_row_is_zero(lake):
    nothing = answer(metrics.query(lake, "revenue,batches,customers,avg_ticket", filters=["city=Nowhere"]))

    assert nothing == [(None, 0, 0, None)]
    assert answer(metrics.query(lake, "revenue", by="city", filters=["city=Nowhere"])) == []


def test_the_rows_come_back_in_the_order_of_the_grain_and_then_the_dimensions(lake):
    found = metrics.query(lake, "orders", by=["city"], grain="month")

    assert [(row["month"], row["city"]) for row in found.to_pylist()] == sorted(
        (row["month"], row["city"]) for row in found.to_pylist())


# --------------------------------------------------------------------------- values are never SQL

HOSTILE = ["x' OR '1'='1", "'; DROP TABLE gold_sales; --", 'x") OR 1=1 --', "Pune' --", "%", "Pu_ne", "Pune\n--",
           "?", "\\", "$2", "$1 OR 1=1"]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_filter_value_that_holds_sql_is_a_value_and_cannot_change_the_query(lake, value):
    compiled = metrics.compile_for(lake, "revenue", by="city", filters=[f"city={value}"])
    found = metrics.query(lake, "revenue", by="city", filters=[f"city={value}"])

    assert compiled.parameters == (value,) and value not in compiled.sql
    assert compiled.sql.splitlines()[-3:] == ['WHERE "city" = $1', "GROUP BY 1", "ORDER BY 1"]
    assert found.num_rows == 0
    assert Iceberg("gold_sales", {"identifier": "gold", "warehouse_path": str(lake / "warehouse")}).get().num_rows == 9


def test_a_filter_value_of_a_real_value_with_sql_around_it_matches_nothing_and_a_wildcard_is_not_one(lake):
    assert metrics.query(lake, "orders", filters=["city=Pune"]).column("orders")[0].as_py() == 22
    assert metrics.query(lake, "orders", filters=["city=pune"]).column("orders")[0].as_py() is None
    assert metrics.query(lake, "orders", filters=["city=Pun%"]).column("orders")[0].as_py() is None
    assert metrics.query(lake, "orders", filters=["city=Pune' OR city='Delhi"]).column("orders")[0].as_py() is None
    assert metrics.query(lake, "orders", filters=["city=Pune", "city=Pune' OR 1=1 --"]).column(
        "orders")[0].as_py() == 22


@pytest.mark.parametrize("options, message", [
    ({"metrics": "revenue; DROP TABLE gold_sales"}, "unknown metric 'revenue; DROP TABLE gold_sales'."),
    ({"metrics": "revenue", "by": "city; DROP TABLE gold_sales"}, "unknown dimension 'city; DROP TABLE gold_sales'."),
    ({"metrics": "revenue", "grain": "month'); DROP TABLE gold_sales; --"}, "unknown grain "),
    ({"metrics": "revenue", "start": "2026-01-01' OR '1'='1"}, "--from has to be a date such as 2026-01-31"),
    ({"metrics": "revenue", "filters": ["city; DROP TABLE x=1"]}, "unknown dimension 'city; DROP TABLE x'."),
])
def test_a_name_a_grain_or_a_day_that_holds_sql_is_refused_before_any_sql_is_made(lake, options, message):
    with pytest.raises(ConfigError) as error:
        metrics.compile_for(lake, **options)

    assert str(error.value).startswith(message)


def test_a_measure_expression_that_is_not_only_an_expression_never_gets_into_the_sql(tmp_path):
    spec = one_measure("net", "sum", "revenue) FROM gold_sales WHERE (1=1) --")
    found = defined(tmp_path, spec)

    with pytest.raises(ConfigError, match="is more than one expression, and a measure is one SQL expression"):
        compile_sales(found, "the_metric")


# --------------------------------------------------------------------------- the release a query reads


def gold_table(root: Path, name: str = "gold_sales") -> Iceberg:
    return Iceberg(name, {"identifier": "gold", "warehouse_path": str(root / "warehouse")})


def total_orders(root: Path, **options) -> int | None:
    return metrics.query(root, "orders", **options).column("orders")[0].as_py()


def test_without_a_release_a_query_reads_the_tables_as_they_are_and_says_so(lake):
    result = runner.run(lake, "orders")

    assert result.release is None and result.table.column("orders")[0].as_py() == 36
    assert result.source == "the tables as they are now, because no release has been cut yet"
    assert runner.run(lake, "orders", live=True).source == "the tables as they are now"


def test_by_default_a_query_reads_the_current_release_and_a_release_or_live_names_another_version(lake):
    build_gold(lake, release=True)
    by_city = {"by": "city", "grain": "quarter"}
    first = answer(metrics.query(lake, "revenue,avg_ticket", **by_city))
    gold_table(lake).put(table_of(ROWS[:2]))

    assert total_orders(lake) == 36 and total_orders(lake, release="r1") == 36 and total_orders(lake, live=True) == 51
    assert build_gold(lake, release=True).release.release.name == "r2"
    assert total_orders(lake) == 51 and total_orders(lake, release="r2") == 51
    assert total_orders(lake, release="r1") == 36
    assert answer(metrics.query(lake, "revenue,avg_ticket", release="r1", **by_city)) == first
    assert answer(metrics.query(lake, "revenue,avg_ticket", **by_city)) != first


def test_the_result_says_which_release_was_read_and_when_it_was_cut(lake):
    release = build_gold(lake, release=True).release.release
    gold_table(lake).put(table_of(ROWS[:1]))

    default, older, live = (runner.run(lake, "orders"), runner.run(lake, "orders", release="r1"),
                            runner.run(lake, "orders", live=True))

    cut = release.created_at.strftime("%Y-%m-%d %H:%M:%S")
    assert default.release == older.release == release and live.release is None
    assert default.source == older.source == f"release r1, cut {cut} UTC"
    assert live.source == "the tables as they are now"
    assert runner.describe_source(release, True, "gold.x") == f"release r1, cut {cut} UTC"
    assert runner.describe_source(release, False, "gold.x") == (
        "the tables as they are now, because release r1 does not hold gold.x")
    assert runner.describe_source(None, False, "gold.x") == (
        "the tables as they are now, because no release has been cut yet")
    assert runner.describe_source(release, True, "gold.x", live=True) == "the tables as they are now"


def test_a_release_that_does_not_exist_and_a_release_with_live_are_refused(lake):
    build_gold(lake, release=True)

    with pytest.raises(ReleaseError, match="r9"):
        metrics.query(lake, "orders", release="r9")
    with pytest.raises(ConfigError, match="pass either a release or --live, not both"):
        metrics.query(lake, "orders", release="r1", live=True)


def test_a_table_that_the_current_release_does_not_hold_is_read_as_it_is_and_an_explicit_release_is_refused(lake):
    build_gold(lake, release=True)
    sales(lake, name="gold_extra")
    extra = copy.deepcopy(METRICS)
    extra["semantic_models"][0]["table"] = "gold_extra"
    metrics_file(lake, extra)

    default = runner.run(lake, "orders")

    assert default.release is None and default.table.column("orders")[0].as_py() == 36
    assert default.source == "the tables as they are now, because release r1 does not hold gold.gold_extra"
    with pytest.raises(ConfigError) as error:
        metrics.query(lake, "orders", release="r1")
    assert str(error.value) == ("release r1 does not hold gold.gold_extra, so it cannot be read as of that release. "
                                "The release holds ['gold.gold_sales']")


def test_a_definition_that_reads_a_column_an_older_release_does_not_have_fails_with_a_hint(lake):
    from pyiceberg.types import DoubleType

    build_gold(lake, release=True)
    with gold_table(lake).table().update_schema() as update:
        update.add_column("tip", DoubleType())
    wider = pa.schema([*SCHEMA, ("tip", pa.float64())])
    gold_table(lake).put(table_of([(*row, 1.5) for row in ROWS[:2]], wider))
    spec = copy.deepcopy(METRICS)
    spec["semantic_models"][0]["measures"].append({"name": "tip_sum", "agg": "sum", "expr": "tip"})
    spec["metrics"].append({"name": "tips", "type": "simple", "measure": "tip_sum", "description": "Tips."})
    metrics_file(lake, spec)
    build_gold(lake, release=True)

    assert answer(metrics.query(lake, "tips")) == [(3.0,)] and answer(metrics.query(lake, "tips", release="r2")) == [
        (3.0,)]
    with pytest.raises(ConfigError) as error:
        metrics.query(lake, "tips", release="r1")
    assert str(error.value) == (
        "the query for 'tips' could not run on gold.gold_sales. Binder Error: Referenced column \"tip\" not found in "
        "FROM clause. The definitions describe gold.gold_sales as it is now, and r1 may have the columns of an "
        "earlier schema. Try a newer release, or --live")


def test_a_query_that_fails_in_duckdb_is_one_config_error_with_the_first_line_and_no_release_hint_when_live(
        lake, monkeypatch):
    from local_data_platform.engine.duckdb import DuckDBEngine

    real = DuckDBEngine.query

    def fail(self, sql, params=None):
        if sql.startswith("DESCRIBE"):
            return real(self, sql, params)
        raise duckdb.BinderException("Binder Error: no such column!\nLINE 1: SELECT ...")

    monkeypatch.setattr(DuckDBEngine, "query", fail)

    with pytest.raises(ConfigError) as error:
        metrics.query(lake, "orders,revenue", live=True)

    assert str(error.value) == ("the query for 'orders' and 'revenue' could not run on gold.gold_sales. Binder Error: "
                                "no such column.")


def test_a_query_only_needs_the_table_that_it_reads_to_exist(lake):
    write(lake / "gold_later.json", {"identifier": "gold_later", "who": "ops", "metadata": {
        "source": {"name": "gold_later", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "gold_later", "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": "Not built yet.", "grain": "one row", "columns": {"a": "A."}}}})

    assert total_orders(lake) == 36


def test_compiling_a_query_reads_no_data_and_gives_the_sql_of_the_query_that_would_run(lake):
    result = runner.run(lake, "revenue", by="city", filters=["city=Pune"], execute=False)

    assert result.table is None and result.query.parameters == ("Pune",)
    assert result.query.sql == metrics.compile_for(lake, "revenue", by="city", filters=["city=Pune"]).sql
    assert result.query.sql.splitlines()[0] == "SELECT"


def test_the_names_of_the_package_are_loaded_when_they_are_first_used():
    assert metrics.query is runner.query and metrics.validate_folder is check.validate_folder
    assert metrics.GRAINS == model.GRAINS and metrics.load_metrics is load.load_metrics
    assert "query" in dir(metrics) and sorted(metrics.__all__) == metrics.__all__
    with pytest.raises(AttributeError, match="has no attribute 'nothing'"):
        metrics.nothing


# --------------------------------------------------------------------------- ldp metrics


def cli(capsys, *argv) -> tuple[int, str, str]:
    """Run ``ldp``, and leave out the one warning of a machine whose DuckDB has no iceberg extension."""
    code = main([str(item) for item in argv])
    out, err = capsys.readouterr()
    kept = [line for line in err.splitlines(keepends=True) if "Registering Iceberg tables in memory" not in line]
    return code, out, "".join(kept)


def cells(line: str) -> list[str]:
    return [item for item in __import__("re").split(r"\s{2,}", line.strip()) if item]


def test_ldp_metrics_list_prints_each_metric_with_its_meaning_owner_format_table_dimensions_and_grains(lake, capsys):
    code, out, err = cli(capsys, "metrics", "list", lake)

    lines = out.splitlines()
    assert (code, err) == (0, "") and lines[-1] == f"({len(METRICS['metrics'])} rows)"
    assert cells(lines[0]) == ["metric", "description", "owner", "format", "table", "dimensions", "grains"]
    assert cells(lines[2]) == ["revenue", "Money taken.", "analytics@example.com", "currency", "gold.gold_sales",
                               "city, kitchen", "day, week, month, quarter, year"]
    assert cells(lines[4])[2] == "finance@example.com" and cells(lines[12])[3] == "percent"
    assert out.rstrip("\n") == render.list_text(load.load_metrics(lake))


def test_ldp_metrics_list_clips_a_long_description_and_json_has_the_definitions(lake, capsys):
    long = "A very long description that goes on and on about what the number means to the people who read it."
    metrics_file(lake, with_metric("revenue", description=long))

    code, out, _ = cli(capsys, "metrics", "list", lake)
    assert code == 0 and "A very long description that goes on and on a..." in out and long not in out
    code, out, _ = cli(capsys, "metrics", "list", lake, "--json")
    printed = json.loads(out)
    assert code == 0 and printed == load.load_metrics(lake).to_dict()
    assert printed["metrics"][0]["description"] == long and printed["semantic_models"][0]["table"] == "gold.gold_sales"


def test_ldp_metrics_list_of_a_folder_without_a_metrics_file_is_an_error_that_says_what_a_file_has(tmp_path, capsys):
    code, out, err = cli(capsys, "metrics", "list", tmp_path)

    assert code == 1 and out == ""
    assert err.startswith("ldp: error: there is no metrics file in ") and 'A metrics file has "kind": "Metrics".' in err


def test_ldp_metrics_without_a_command_prints_its_help_and_exits_with_1(capsys):
    code, out, err = cli(capsys, "metrics")

    assert code == 1 and err == "" and "list" in out and "validate" in out and "query" in out
    assert 'kind": "Metrics"' in out and "ldp_demo/restaurant/config revenue --by cuisine --grain month" in out


def test_ldp_metrics_validate_says_that_the_definitions_fit_the_tables(lake, capsys):
    code, out, err = cli(capsys, "metrics", "validate", lake)

    assert (code, err) == (0, "")
    assert out == (f"metrics.json: 1 semantic model and {len(METRICS['metrics'])} metrics fit the gold tables of "
                   f"{lake.resolve()}\n")
    code, out, _ = cli(capsys, "metrics", "validate", lake, "--json")
    assert code == 0 and json.loads(out) == {"folder": str(lake.resolve()), "files": ["metrics.json"], "valid": True,
                                             "semantic_models": 1, "metrics": len(METRICS["metrics"]), "problems": []}


def test_ldp_metrics_validate_lists_every_problem_and_exits_with_1(lake, capsys):
    spec = with_model(dimensions=["cuisin"], time_dimension={"column": "odate", "grains": ["day"]},
                      measures=[{"name": "revenue_sum", "agg": "sum", "expr": "net"}]) | {
        "metrics": [METRICS["metrics"][0]]}
    metrics_file(lake, spec)

    code, out, err = cli(capsys, "metrics", "validate", lake)

    lines = err.splitlines()
    assert code == 1 and out == "" and lines[0] == f"ldp: 3 problems in the metrics of {lake.resolve()}"
    assert [line[:60] for line in lines[1:]] == [
        f"  - {HERE}, time dimension: the column 'odate' is not in g"[:60],
        f"  - {HERE}, dimension 'cuisin': the column 'cuisin' is not"[:60],
        f"  - {HERE}, measure 'revenue_sum': the expression 'net' re"[:60]]
    code, out, err = cli(capsys, "metrics", "validate", lake, "--json")
    printed = json.loads(out)
    assert code == 1 and err == "" and printed["valid"] is False and len(printed["problems"]) == 3


def test_ldp_metrics_validate_of_a_file_that_is_not_valid_stops_before_the_tables_and_a_missing_table_is_a_problem(
        tmp_path, capsys):
    sales(tmp_path)
    metrics_file(tmp_path, with_metric("revenue", format="euro"))

    code, _, err = cli(capsys, "metrics", "validate", tmp_path)
    assert code == 1 and err.splitlines()[0].startswith("ldp: 1 problem in the metrics of ")
    assert "  - metrics.json: metrics[0].format: must be one of" in err
    metrics_file(tmp_path)
    shutil_rmtree(tmp_path / "warehouse")
    code, _, err = cli(capsys, "metrics", "validate", tmp_path)
    assert code == 1 and "gold.gold_sales does not exist yet, so its columns cannot be checked" in err


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


def test_ldp_metrics_validate_of_a_folder_without_a_metrics_file_exits_with_1(tmp_path, capsys):
    code, out, err = cli(capsys, "metrics", "validate", tmp_path)

    assert code == 1 and out == "" and "there is no metrics file in" in err


def test_ldp_metrics_query_prints_the_result_in_the_format_of_each_metric_and_says_which_tables_it_read(lake, capsys):
    code, out, err = cli(capsys, "metrics", "query", lake, "revenue,avg_ticket,orders", "--by", "city", "--grain",
                         "quarter")

    lines = out.splitlines()
    assert (code, err) == (0, "")
    assert lines[-1] == "read the tables as they are now, because no release has been cut yet"
    assert lines[:3] == ["quarter     city   revenue  avg_ticket  orders",
                         "----------  -----  -------  ----------  ------",
                         "2026-01-01  Delhi    50.25       10.05       5"]
    assert lines[3] == "2026-01-01  Pune    285.50       13.60      21" and lines[-2] == "(5 rows)"


def test_a_percent_shows_as_a_percentage_a_number_has_the_same_decimals_in_a_column_and_raw_shows_the_values(
        lake, capsys):
    code, out, _ = cli(capsys, "metrics", "query", lake, "rating_share,avg_rating,customers", "--by", "kitchen")
    assert code == 0
    assert out.splitlines()[:4] == ["kitchen  rating_share  avg_rating  customers",
                                    "-------  ------------  ----------  ---------",
                                    "Italian        100.0%        3.50          3",
                                    "Thai            80.0%        4.00          2"]
    code, out, _ = cli(capsys, "metrics", "query", lake, "rating_share,avg_rating", "--by", "kitchen", "--raw")
    assert code == 0 and [line.split() for line in out.splitlines()[2:4]] == [["Italian", "1", "3.5"],
                                                                              ["Thai", "0.8", "4"]]


def test_a_query_that_finds_no_row_prints_the_columns_and_zero_rows(lake, capsys):
    code, out, _ = cli(capsys, "metrics", "query", lake, "revenue", "--by", "city", "--filter", "city=Nowhere")

    assert code == 0 and out.splitlines()[:3] == ["city  revenue", "----  -------", "(0 rows)"]


def test_max_rows_cuts_what_is_printed_and_says_how_many_more_there_are(lake, capsys):
    code, out, _ = cli(capsys, "metrics", "query", lake, "revenue", "--grain", "month", "--max-rows", "2")

    assert code == 0 and "... 4 more rows" in out and "(6 rows)" in out and out.count("2026-") == 2


def test_sql_prints_the_compiled_sql_with_the_value_of_each_parameter_and_runs_nothing(lake, capsys, monkeypatch):
    from local_data_platform.engine.duckdb import DuckDBEngine

    real, run_sql = DuckDBEngine.query, []

    def spy(self, sql, params=None):
        run_sql.append(sql)
        return real(self, sql, params)

    monkeypatch.setattr(DuckDBEngine, "query", spy)

    code, out, err = cli(capsys, "metrics", "query", lake, "revenue,avg_ticket", "--by", "kitchen", "--grain", "month",
                         "--filter", "city=Pune", "--from", "2026-01-01", "--to", "2026-03-31", "--sql")

    assert (code, err) == (0, "") and out.splitlines()[0] == "SELECT" and out.splitlines()[-3:] == [
        '-- $1 = "Pune"', "-- $2 = DATE '2026-01-01'", "-- $3 = DATE '2026-03-31'"]
    compiled = metrics.compile_for(lake, "revenue,avg_ticket", by="kitchen", grain="month", filters=["city=Pune"],
                                   start="2026-01-01", end="2026-03-31")
    assert out.rstrip("\n") == compiled.explain()
    assert all(item.startswith("DESCRIBE") for item in run_sql) and run_sql
    code, out, _ = cli(capsys, "metrics", "query", lake, "revenue", "--sql", "--json")
    assert code == 0 and json.loads(out)["sql"].startswith("SELECT") and json.loads(out)["parameters"] == []


def test_json_prints_the_rows_as_numbers_and_dates_with_the_sql_and_the_release(lake, capsys):
    release = build_gold(lake, release=True).release.release

    code, out, _ = cli(capsys, "metrics", "query", lake, "revenue,orders,avg_ticket", "--grain", "year", "--json")

    printed = json.loads(out)
    assert code == 0 and printed["release"] == "r1" and printed["source"].startswith("release r1, cut ")
    assert printed["rows"][0] == {"year": "2026-01-01", "revenue": 389.75, "orders": 30, "avg_ticket": 389.75 / 30}
    assert printed["row_count"] == 2 and printed["table"] == "gold.gold_sales" and printed["grain"] == "year"
    assert printed["columns"][3] == {"name": "avg_ticket", "kind": "metric", "format": "currency",
                                     "description": "Revenue divided by orders."}
    assert release.name == "r1"


def test_out_writes_the_values_as_they_were_computed_to_csv_parquet_or_jsonl(lake, capsys, tmp_path):
    options = ["revenue,avg_ticket,orders", "--by", "city", "--grain", "quarter"]
    code, out, _ = cli(capsys, "metrics", "query", lake, *options, "--out", tmp_path / "out" / "m.csv")
    assert code == 0 and out == (f"wrote 5 rows to {tmp_path / 'out' / 'm.csv'} as CSV. Read the tables as they are "
                                 "now, because no release has been cut yet.\n")
    lines = (tmp_path / "out" / "m.csv").read_text().splitlines()
    assert lines[0] == '"quarter","city","revenue","avg_ticket","orders"' and lines[1].startswith(
        '2026-01-01,"Delhi",50.25,10.05')
    cli(capsys, "metrics", "query", lake, *options, "--out", tmp_path / "m.parquet")
    import pyarrow.parquet as parquet

    stored = parquet.read_table(tmp_path / "m.parquet")
    assert stored.column_names == ["quarter", "city", "revenue", "avg_ticket", "orders"] and stored.num_rows == 5
    assert stored.column("revenue")[0].as_py() == D("50.25")
    cli(capsys, "metrics", "query", lake, *options, "--out", tmp_path / "m.JSONL")
    rows = [json.loads(line) for line in (tmp_path / "m.JSONL").read_text().splitlines()]
    assert rows[0] == {"quarter": "2026-01-01", "city": "Delhi", "revenue": 50.25, "avg_ticket": 10.05, "orders": 5}
    assert len(rows) == 5 and not list(tmp_path.glob(".*.tmp")) and not list((tmp_path / "out").glob(".*.tmp"))


def test_out_writes_every_row_whatever_max_rows_says_and_does_not_print_the_table(lake, capsys, tmp_path):
    code, out, _ = cli(capsys, "metrics", "query", lake, "revenue", "--grain", "month", "--max-rows", "1", "--out",
                       tmp_path / "m.jsonl")

    assert code == 0 and "month" not in out and out.startswith(f"wrote 6 rows to {tmp_path / 'm.jsonl'} as JSONL.")
    assert len((tmp_path / "m.jsonl").read_text().splitlines()) == 6


def test_a_file_that_cannot_be_written_is_refused_before_the_query_runs(lake, capsys, tmp_path, monkeypatch):
    from local_data_platform.engine.duckdb import DuckDBEngine

    monkeypatch.setattr(DuckDBEngine, "query", lambda *args, **kwargs: pytest.fail("the query ran"))

    code, out, err = cli(capsys, "metrics", "query", lake, "revenue", "--out", tmp_path / "m.xlsx")

    assert code == 1 and out == "" and err == (
        "ldp: error: cannot write 'm.xlsx'. The file has to end in one of ['.csv', '.jsonl', '.ndjson', '.parquet']\n")


def test_an_unknown_metric_dimension_grain_or_release_is_an_error_that_lists_what_is_valid(lake, capsys):
    code, out, err = cli(capsys, "metrics", "query", lake, "revenue,nope")
    assert code == 1 and out == "" and err.startswith("ldp: error: unknown metric 'nope'. The metrics are ['revenue', ")
    code, _, err = cli(capsys, "metrics", "query", lake, "revenue", "--by", "cuisine")
    assert code == 1 and err == ("ldp: error: unknown dimension 'cuisine'. The dimensions are ['city', 'kitchen']. "
                                 "They are the dimensions of daily_sales\n")
    code, _, err = cli(capsys, "metrics", "query", lake, "revenue", "--grain", "decade")
    assert code == 1 and err == ("ldp: error: unknown grain 'decade'. The grains are ['day', 'week', 'month', "
                                 "'quarter', 'year']\n")
    code, _, err = cli(capsys, "metrics", "query", lake, "revenue", "--release", "r4")
    assert code == 1 and err.startswith("ldp: error: there is no release r4")
    code, _, err = cli(capsys, "metrics", "query", lake, "revenue", "--release", "r1", "--live")
    assert code == 1 and "argument --live: not allowed with argument --release" in err
    code, _, err = cli(capsys, "metrics", "query", lake, "revenue", "--max-rows", "0")
    assert code == 1 and "expected a number of at least 1, got 0" in err


def test_a_query_of_two_tables_is_refused_by_the_command_with_the_message_of_the_compiler(tmp_path, capsys):
    sales(tmp_path)
    sales(tmp_path, name="gold_other")
    spec = two_models()
    spec["semantic_models"][1]["table"] = "gold_other"
    spec["metrics"].append({"name": "others", "type": "simple", "measure": "other_sum", "description": "Others."})
    metrics_file(tmp_path, spec)

    code, out, err = cli(capsys, "metrics", "query", tmp_path, "revenue,others")

    assert code == 1 and out == "" and err.startswith(
        "ldp: error: the metrics are on more than one semantic model, 'revenue' on daily_sales (gold.gold_sales); "
        "'others' on other (gold_other). A query reads one table, so ask for them in separate queries")


def test_ldp_metrics_query_reads_the_release_by_default_and_release_and_live_choose_another(lake, capsys):
    build_gold(lake, release=True)
    gold_table(lake).put(table_of(ROWS[:1]))
    build_gold(lake, release=True)
    gold_table(lake).put(table_of(ROWS[:1]))

    def read(*extra) -> tuple[str, str]:
        code, out, _ = cli(capsys, "metrics", "query", lake, "orders", *extra)
        assert code == 0
        return out.splitlines()[2].strip(), out.splitlines()[-1]

    assert read()[0] == "46" and read("--release", "r1")[0] == "36" and read("--live")[0] == "56"
    assert read("--release", "r2")[0] == "46"
    assert read()[1].startswith("read release r2, cut 20") and read("--live")[1] == "read the tables as they are now"


# --------------------------------------------------------------------------- showing a result


@pytest.mark.parametrize("value, fmt, shown", [
    (None, "currency", None), (1234.5, "currency", "1,234.50"), (D("1234567.891"), "currency", "1,234,567.89"),
    (0.5, "percent", "50.0%"), (1, "percent", "100.0%"), (D("0.1234"), "percent", "12.3%"), (-0.05, "percent", "-5.0%"),
    (1234567, "number", "1,234,567"), (4.0, "number", "4"), (3.5, "number", "3.50"), (D("17"), "number", "17"),
    (0.0004, "number", "0.00"), (12, None, "12"), (12.25, None, "12.25"), (float("inf"), "number", "inf"),
    (float("nan"), "currency", "nan"), ("text", "number", "text"), (True, "number", True),
    (dt.date(2026, 1, 1), "number", dt.date(2026, 1, 1))])
def test_a_value_is_shown_by_the_format_of_its_metric(value, fmt, shown):
    assert render.display(value, fmt) == shown


def test_a_column_that_has_a_decimal_shows_two_decimals_for_every_number_and_a_whole_column_none():
    assert [render.display(value, "number", whole=False) for value in (4.0, 3.5)] == ["4.00", "3.50"]
    assert [render.display(value, "number", whole=True) for value in (4.0, 3.0)] == ["4", "3"]
    assert render.display(2.5, "currency", whole=True) == "2.50"
    assert render.display(0.25, "percent", whole=True) == "25.0%"


def test_decimals_become_numbers_for_a_reader_that_wants_json_and_whole_decimals_become_integers():
    table = pa.table({"a": pa.array([D("1.50"), None], pa.decimal128(10, 2)),
                      "b": pa.array([D("7"), D("8")], pa.decimal128(38, 0)),
                      "c": pa.array(["x", "y"]), "d": pa.array([1.5, None])})

    plain = render.plain_numbers(table)

    assert plain.schema.types == [pa.float64(), pa.int64(), pa.string(), pa.float64()]
    assert plain.to_pylist() == [{"a": 1.5, "b": 7, "c": "x", "d": 1.5}, {"a": None, "b": 8, "c": "y", "d": None}]
    huge = pa.table({"n": pa.array([D(10) ** 30], pa.decimal128(38, 0))})
    assert render.plain_numbers(huge).schema.types == [pa.float64()]
    dated = pa.table({"day": [dt.date(2026, 1, 2)], "n": pa.array([D("2.5")], pa.decimal128(5, 1))})
    assert render.json_rows(dated) == [{"day": "2026-01-02", "n": 2.5}]


@pytest.mark.parametrize("name, kind", [("a.csv", "CSV"), ("A.CSV", "CSV"), ("a.parquet", "Parquet"),
                                        ("a.jsonl", "JSONL"), ("a.ndjson", "JSONL")])
def test_the_ending_of_a_file_says_how_the_result_is_written(name, kind):
    assert render.file_format(name) == kind


def test_a_file_name_with_another_ending_is_refused():
    with pytest.raises(ConfigError, match="cannot write 'a.txt'"):
        render.file_format("a.txt")
    with pytest.raises(ConfigError, match="cannot write 'noending'"):
        render.write_table(pa.table({"a": [1]}), "noending")


def test_a_result_is_written_whole_or_not_at_all(tmp_path, monkeypatch):
    target = tmp_path / "result.csv"
    target.write_text("old\n")

    def crash(table, path):
        Path(path).write_text("half")
        raise OSError("the disk went away")

    import pyarrow.csv

    monkeypatch.setattr(pyarrow.csv, "write_csv", crash)

    with pytest.raises(OSError):
        render.write_table(pa.table({"a": [1]}), target)

    assert target.read_text() == "old\n" and not list(tmp_path.glob(".*.tmp"))


# --------------------------------------------------------------------------- ldp gold build checks the metrics

REVENUE_METRICS = {
    "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "revenue_metrics", "who": "ops",
    "semantic_models": [{
        "name": "revenue_by_restaurant", "table": "gold_revenue", "dimensions": ["restaurant"],
        "measures": [{"name": "revenue_sum", "agg": "sum", "expr": "revenue"},
                     {"name": "order_sum", "agg": "sum", "expr": "orders"}]}],
    "metrics": [{"name": "revenue", "type": "simple", "measure": "revenue_sum", "description": "What was paid."},
                {"name": "orders", "type": "simple", "measure": "order_sum", "description": "How many orders."},
                {"name": "avg_ticket", "type": "ratio", "numerator": "revenue_sum", "denominator": "order_sum",
                 "description": "What one order costs."}]}


def restaurant_folder(root: Path, spec=None) -> Path:
    """A folder with silver orders, the gold table ``gold.gold_revenue`` built from them, and a metrics file."""
    from tests.test_gold_build import gold as gold_config
    from tests.test_gold_build import silver

    silver(root)
    gold_config(root)
    write(root / "metrics.json", REVENUE_METRICS if spec is None else spec)
    return root


def broken_revenue(**measure) -> dict:
    spec = copy.deepcopy(REVENUE_METRICS)
    spec["semantic_models"][0]["measures"][0].update(measure)
    return spec


def test_a_build_checks_the_metrics_against_the_tables_and_says_so_before_it_cuts_the_release(tmp_path, capsys):
    restaurant_folder(tmp_path)

    code, out, err = cli(capsys, "gold", "build", tmp_path)

    lines = out.splitlines()
    assert (code, err) == (0, "") and lines[-3].startswith("gold build ")
    assert lines[-2] == "metrics: 1 semantic model and 3 metrics fit the gold tables"
    assert lines[-1] == "release r1 cut with 1 table and 2 rows in gold"
    assert metrics.query(tmp_path, "revenue,orders", by="restaurant").num_rows == 2


def test_a_build_of_a_folder_without_metrics_has_no_metrics_line_and_no_metrics_in_its_result(tmp_path, capsys):
    restaurant_folder(tmp_path)
    (tmp_path / "metrics.json").unlink()

    code, out, _ = cli(capsys, "gold", "build", tmp_path, "--json")

    printed = json.loads(out)
    assert code == 0 and printed["metrics"] is None and printed["release"]["cut"] is True
    assert build_gold(tmp_path).metrics is None


def test_a_metrics_file_that_is_not_valid_stops_the_build_before_anything_is_built(tmp_path, capsys):
    spec = broken_revenue(agg="median")
    restaurant_folder(tmp_path, spec)

    code, out, err = cli(capsys, "gold", "build", tmp_path)

    assert code == 1 and out == ""
    assert err.splitlines()[0].startswith(f"ldp: error: the metrics of {tmp_path.resolve()} have 1 problem")
    assert "metrics.json: semantic_models[0].measures[0].agg: is required and must be one of" in err
    assert not gold_table(tmp_path, "gold_revenue").exists()
    assert not list((tmp_path / "warehouse" / ".ldp").glob("releases/**/*.json"))


def test_metrics_that_do_not_fit_the_tables_cut_no_release_and_the_tables_are_still_built(tmp_path, capsys):
    restaurant_folder(tmp_path, broken_revenue(expr="revenu"))

    code, out, err = cli(capsys, "gold", "build", tmp_path)

    assert code == 1
    assert out.splitlines()[-1] == "no release cut. There is no release yet."
    assert "gold_revenue  built" in out and "metrics: " not in out
    assert err.splitlines()[0] == f"ldp: 1 problem in the metrics of {tmp_path.resolve()}"
    assert err.splitlines()[1].startswith("  - metrics.json: semantic model 'revenue_by_restaurant', measure "
                                          "'revenue_sum': the expression 'revenu' reads the column 'revenu', which is "
                                          "not in gold.gold_revenue. Did you mean 'revenue'?")
    assert err.splitlines()[2:] == ["ldp: the release was not cut", "  the metrics do not fit the gold tables"]
    assert len(gold_table(tmp_path, "gold_revenue").get().to_pylist()) == 2
    assert not list((tmp_path / "warehouse" / ".ldp").glob("releases/*/*.json"))
    write(tmp_path / "metrics.json", REVENUE_METRICS)
    assert main(["gold", "build", str(tmp_path)]) == 0 and current_release_name(tmp_path) == "r1"


def current_release_name(root: Path) -> str | None:
    from local_data_platform.gold.release import current_release

    found = current_release(root)
    return found.name if found is not None else None


def test_when_the_metrics_stop_a_release_the_readers_still_see_the_last_one(tmp_path, capsys):
    restaurant_folder(tmp_path)
    assert main(["gold", "build", str(tmp_path)]) == 0
    capsys.readouterr()
    write(tmp_path / "metrics.json", broken_revenue(expr="revenu"))

    code, out, err = cli(capsys, "gold", "build", tmp_path)

    assert code == 1 and out.splitlines()[-1] == "no release cut. Readers still see r1."
    assert current_release_name(tmp_path) == "r1" and "the metrics do not fit the gold tables" in err


def test_a_build_without_a_release_still_checks_the_metrics(tmp_path, capsys):
    restaurant_folder(tmp_path, broken_revenue(expr="revenu"))

    code, out, err = cli(capsys, "gold", "build", tmp_path, "--no-release")

    assert code == 1 and "release r" not in out and "no release" not in out and err.splitlines()[0] == (
        f"ldp: 1 problem in the metrics of {tmp_path.resolve()}")
    assert "ldp: the release was not cut" not in err
    write(tmp_path / "metrics.json", REVENUE_METRICS)
    code, out, _ = cli(capsys, "gold", "build", tmp_path, "--no-release")
    assert code == 0 and out.splitlines()[-1] == "metrics: 1 semantic model and 3 metrics fit the gold tables"


def test_the_json_of_a_build_has_the_check_of_the_metrics_and_the_exit_code_is_1_when_they_do_not_fit(tmp_path, capsys):
    restaurant_folder(tmp_path, broken_revenue(expr="revenu"))

    code, out, err = cli(capsys, "gold", "build", tmp_path, "--json")

    printed = json.loads(out)
    assert code == 1 and err == "" and printed["exit_code"] == 1
    assert printed["metrics"]["valid"] is False and printed["metrics"]["files"] == ["metrics.json"]
    assert len(printed["metrics"]["problems"]) == 1 and printed["release"]["error"] == (
        "the metrics do not fit the gold tables")


def test_a_build_that_a_gate_stops_does_not_check_the_metrics_and_exits_with_3(tmp_path, capsys):
    from tests.test_gold_build import external

    external(tmp_path, quality={"checks": [{"check": "row_count", "min": 10}]})
    write(tmp_path / "metrics.json", {**REVENUE_METRICS, "semantic_models": [
        {**REVENUE_METRICS["semantic_models"][0], "table": "elsewhere", "dimensions": ["nothing"]}]})

    result = build_gold(tmp_path, release=True)
    code, _, err = cli(capsys, "gold", "build", tmp_path)

    assert result.metrics is None and result.exit_code == 3 and code == 3
    assert "problem in the metrics" not in err


def test_the_exit_code_of_a_build_is_1_when_only_the_metrics_do_not_fit(tmp_path):
    restaurant_folder(tmp_path, broken_revenue(expr="revenu"))

    result = build_gold(tmp_path)

    assert result.stopped_by is None and result.release is None and result.exit_code == 1
    assert [item.status for item in result.tables] == ["built"] and result.metrics.problems
    write(tmp_path / "metrics.json", REVENUE_METRICS)
    assert build_gold(tmp_path).exit_code == 0


def test_a_folder_with_metrics_needs_duckdb_to_be_built_and_says_so_before_anything_is_built(tmp_path, monkeypatch):
    import builtins

    restaurant_folder(tmp_path)
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("no duckdb here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(Exception) as error:
        build_gold(tmp_path)

    assert type(error.value).__name__ == "EngineNotFound" and "pip install" in str(error.value)
    assert not gold_table(tmp_path, "gold_revenue").exists()


def test_a_gold_task_of_a_workflow_fails_when_the_metrics_do_not_fit_and_cuts_a_release_when_they_do(tmp_path):
    from local_data_platform.orchestration import run_workflow
    from tests.test_gold_release import DAY_1, flow

    config, file = flow(tmp_path)
    write(config / "metrics.json", broken_revenue(expr="revenu"))

    failed = run_workflow(file, window=DAY_1)

    task = failed.tasks["gold"]
    assert failed.exit_code == 1 and task.status == "failed" and task.detail["release_cut"] is False
    assert len(task.detail["metrics_problems"]) == 1 and "revenu" in task.detail["metrics_problems"][0]
    assert "the gold tables were built, and no release was cut: the metrics do not fit the gold tables" in task.error
    write(config / "metrics.json", REVENUE_METRICS)
    done = run_workflow(file, window=DAY_1)
    assert done.exit_code == 0 and done.tasks["gold"].detail["release"] == "r1"
    assert done.tasks["gold"].detail["metrics_problems"] == []
    assert answer(metrics.query(config, "orders")) == [(3,)]


# --------------------------------------------------------------------------- the MCP tools


@pytest.fixture(params=[None, False], ids=["native", "arrow"])
def tools(request, lake, tmp_path_factory):
    """The tools over the lake, with the tables served natively when DuckDB can, and from Arrow."""
    audit = tmp_path_factory.mktemp("audit") / "audit.jsonl"
    served = LakeTools.from_sources([lake], audit_path=audit, iceberg_audit=False, native=request.param, max_rows=50,
                                    timeout_s=10)
    yield served
    served.close()


def ask(served: LakeTools, tool: str, **arguments):
    return served.call(tool, arguments)


def good(served: LakeTools, tool: str, **arguments) -> dict:
    outcome = served.call(tool, arguments)
    assert outcome.ok, outcome.data
    return outcome.data


def refused(served: LakeTools, tool: str, **arguments):
    outcome = served.call(tool, arguments)
    assert not outcome.ok and outcome.status == "rejected", outcome.data
    return outcome.data["error"]


def test_the_eight_tools_have_schemas_that_carry_the_limits_of_the_server(tools):
    specs = {spec.name: spec for spec in tools.tool_specs()}

    assert sorted(specs) == sorted(["list_tables", "describe_table", "query", "sample_rows", "table_history",
                                    "get_dataset", "list_metrics", "query_metric"])
    assert specs["list_metrics"].input_schema == {"type": "object", "properties": {}, "additionalProperties": False}
    schema = specs["query_metric"].input_schema
    assert schema["required"] == ["metrics"] and schema["additionalProperties"] is False
    assert sorted(schema["properties"]) == ["by", "end", "filters", "grain", "max_rows", "metrics", "start"]
    assert schema["properties"]["metrics"] == {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                               "description": schema["properties"]["metrics"]["description"]}
    assert schema["properties"]["grain"]["enum"] == list(model.GRAINS)
    assert schema["properties"]["max_rows"]["maximum"] == 50 and schema["properties"]["max_rows"]["minimum"] == 1
    description = specs["query_metric"].description
    assert "At most 50 rows are returned and the query is stopped after 10 seconds" in description
    assert "never be SQL" in description and "current release" in description
    assert "list_metrics" in tools.instructions() and "query_metric" in tools.instructions()
    assert "query reads the tables as they are now" in tools.instructions()


def test_list_metrics_says_what_each_metric_means_and_what_it_can_be_asked_for(tools):
    data = good(tools, "list_metrics")

    assert data["files"] == ["metrics.json"] and data["problems"] == [] and data["note"] is None
    assert [item["name"] for item in data["metrics"]] == [item["name"] for item in METRICS["metrics"]]
    first = data["metrics"][0]
    assert first == {"name": "revenue", "description": "Money taken.", "owner": "analytics@example.com",
                     "type": "simple", "format": "currency", "table": "gold.gold_sales",
                     "semantic_model": "daily_sales", "dimensions": ["city", "kitchen"], "time_column": "order_date",
                     "grains": list(model.GRAINS), "file": "metrics.json", "measure": "revenue_sum"}
    json.dumps(data)


def test_list_metrics_of_a_server_without_metrics_says_so_and_a_file_with_problems_reports_them(lake, tools):
    (lake / "metrics.json").unlink()

    nothing = good(tools, "list_metrics")

    assert nothing["metrics"] == [] and nothing["files"] == [] and nothing["problems"] == []
    assert nothing["note"].startswith("this server has no metrics. A metrics file has")
    write(lake / "metrics.json", with_metric("revenue", format="euro"))
    broken = good(tools, "list_metrics")
    assert broken["metrics"] == [] and broken["problems"] == [
        "metrics.json: metrics[0].format: must be one of ['number', 'percent', 'currency'], got 'euro'"]
    assert broken["note"] is None
    error = refused(tools, "query_metric", metrics=["revenue"])
    assert error["type"] == "InvalidArguments" and "metrics[0].format" in error["message"]


def test_the_metrics_are_read_again_at_each_call_so_an_edited_file_needs_no_restart(lake, tools):
    assert "extra" not in [item["name"] for item in good(tools, "list_metrics")["metrics"]]
    spec = copy.deepcopy(METRICS)
    spec["metrics"].append({"name": "extra", "type": "simple", "measure": "order_sum", "description": "More."})
    write(lake / "metrics.json", spec)

    assert good(tools, "list_metrics")["metrics"][-1]["name"] == "extra"
    assert good(tools, "query_metric", metrics=["extra"])["rows"] == [[36]]


def test_query_metric_computes_the_metrics_the_way_the_command_does(tools, lake):
    data = good(tools, "query_metric", metrics=["revenue", "avg_ticket"], by=["kitchen"], grain="quarter",
                filters=["city=Pune"], start="2026-01-01", end="2026-12-31")

    expected = metrics.query(lake, "revenue,avg_ticket", by="kitchen", grain="quarter", filters=["city=Pune"],
                             start="2026-01-01", end="2026-12-31")
    assert data["rows"] == [[row["quarter"].isoformat(), row["kitchen"], float(row["revenue"]), row["avg_ticket"]]
                            for row in expected.to_pylist()]
    assert [(item["name"], item["kind"], item["format"]) for item in data["columns"]] == [
        ("quarter", "time", None), ("kitchen", "dimension", None), ("revenue", "metric", "currency"),
        ("avg_ticket", "metric", "currency")]
    assert [item["type"] for item in data["columns"]] == ["date32[day]", "string", "double", "double"]
    assert (data["metrics"], data["table"], data["semantic_model"], data["grain"]) == (
        ["revenue", "avg_ticket"], "gold.gold_sales", "daily_sales", "quarter")
    assert data["parameters"] == ["Pune", "2026-01-01", "2026-12-31"] and "$1" in data["sql"]
    assert (data["row_count"], data["truncated"], data["max_rows"], data["stale_tables"]) == (3, False, 50, [])
    assert data["release"] is None and data["source"] == (
        "the tables as they are now, because no release has been cut yet")


def test_query_metric_gives_numbers_as_numbers_and_decimals_are_not_text(tools):
    data = good(tools, "query_metric", metrics=["revenue", "orders", "customers", "avg_rating"])

    assert data["rows"] == [[461.75, 36, 5, pytest.approx(3.75)]]
    assert [item["type"] for item in data["columns"]] == ["double", "int64", "int64", "double"]


def test_a_list_given_as_one_comma_separated_text_is_accepted_for_metrics_and_dimensions(tools):
    data = good(tools, "query_metric", metrics="revenue,orders", by="city,kitchen", grain="year")

    assert [item["name"] for item in data["columns"]] == ["year", "city", "kitchen", "revenue", "orders"]
    assert data["metrics"] == ["revenue", "orders"]


def test_a_filter_may_hold_a_comma_and_the_server_does_not_split_it(tools):
    data = good(tools, "query_metric", metrics=["revenue"], by=["city"], filters=["city=Pune, India"])

    assert data["parameters"] == ["Pune, India"] and data["rows"] == []


def test_query_metric_has_the_row_cap_and_the_timeout_of_the_server(lake, tmp_path, monkeypatch):
    with LakeTools.from_sources([lake], iceberg_audit=False, audit_path=tmp_path / "a.jsonl", max_rows=3,
                                timeout_s=7) as small:
        capped = good(small, "query_metric", metrics=["orders"], grain="day")
        two = good(small, "query_metric", metrics=["orders"], grain="day", max_rows=2)
        far = good(small, "query_metric", metrics=["orders"], grain="day", max_rows=500)
        seen = []
        real = small.sandbox.query

        def spy(sql, **kwargs):
            seen.append(kwargs)
            return real(sql, **kwargs)

        monkeypatch.setattr(small.sandbox, "query", spy)
        good(small, "query_metric", metrics=["orders"])

    assert (capped["row_count"], capped["truncated"], capped["max_rows"]) == (3, True, 3)
    assert (two["row_count"], two["truncated"], two["max_rows"]) == (2, True, 2)
    assert (far["row_count"], far["max_rows"]) == (3, 3)
    assert seen == [{"max_rows": 3, "timeout_s": 7.0, "params": ()}]


def test_a_query_that_runs_too_long_is_stopped_with_the_status_timeout(tools, monkeypatch):
    from local_data_platform.mcp_server.errors import QueryTimeout

    def stop(sql, **kwargs):
        raise QueryTimeout("the query ran longer than the 10s timeout and was stopped")

    monkeypatch.setattr(tools.sandbox, "query", stop)

    outcome = ask(tools, "query_metric", metrics=["revenue"])

    assert not outcome.ok and outcome.status == "timeout" and outcome.data["error"]["type"] == "QueryTimeout"


def test_every_call_of_the_metric_tools_is_audited_with_its_arguments_and_the_compiled_sql(tools):
    good(tools, "list_metrics")
    good(tools, "query_metric", metrics=["revenue"], by=["city"], grain="month", filters=["city=Pune"],
         start="2026-01-01", max_rows=2)
    refused(tools, "query_metric", metrics=["nope"])

    listed, computed, rejected = tools.audit.records[-3:]

    assert (listed.tool, listed.status, listed.sql, listed.table, listed.arguments) == (
        "list_metrics", "ok", None, None, {})
    assert (computed.tool, computed.status, computed.table, computed.rows, computed.truncated) == (
        "query_metric", "ok", "gold.gold_sales", 2, True)
    assert computed.sql.startswith("SELECT\n") and "$1" in computed.sql and "Pune" not in computed.sql
    assert computed.arguments == {"metrics": ["revenue"], "by": ["city"], "grain": "month",
                                  "filters": ["city=Pune"], "start": "2026-01-01", "max_rows": 2}
    assert (rejected.tool, rejected.status, rejected.sql, rejected.rows) == ("query_metric", "rejected", None, None)
    assert rejected.arguments == {"metrics": ["nope"]} and "unknown metric 'nope'" in rejected.error


def test_a_filter_that_is_sql_is_bound_as_a_value_and_the_table_is_not_changed(tools, lake):
    for value in ["x' OR '1'='1", "'; DROP TABLE gold_sales; --", 'x") OR 1=1 --', "Pune' --"]:
        data = good(tools, "query_metric", metrics=["revenue"], by=["city"], filters=[f"city={value}"])

        assert data["rows"] == [] and data["parameters"] == [value] and value not in data["sql"]
        assert tools.audit.records[-1].sql == data["sql"] and value not in tools.audit.records[-1].sql
    assert good(tools, "query_metric", metrics=["batches"])["rows"] == [[9]]
    assert gold_table(lake).get().num_rows == 9


@pytest.mark.parametrize("arguments, message", [
    ({"metrics": ["revenue", "nope"]}, "unknown metric 'nope'. The metrics are ['revenue', "),
    ({"metrics": ["revenue"], "by": ["cuisine"]}, "unknown dimension 'cuisine'. The dimensions are ['city', "
                                                  "'kitchen']. They are the dimensions of daily_sales"),
    ({"metrics": ["revenue"], "grain": "decade"}, "unknown grain 'decade'. The grains are ['day', 'week', 'month', "
                                                  "'quarter', 'year']"),
    ({"metrics": ["revenue"], "start": "yesterday"}, "--from has to be a date such as 2026-01-31, got 'yesterday'"),
    ({"metrics": ["revenue"], "filters": ["city"]}, "a filter is written dimension=value, got 'city'"),
    ({"metrics": ["revenue"], "filters": ["cuisine=Thai"]}, "unknown dimension 'cuisine'."),
    ({"metrics": ["revenue"], "start": "2026-05-01", "end": "2026-04-01"},
     "--from 2026-05-01 is after --to 2026-04-01"),
])
def test_a_request_that_names_something_that_is_not_there_is_rejected_with_the_valid_names(tools, arguments, message):
    error = refused(tools, "query_metric", **arguments)

    assert error["type"] == "InvalidArguments" and error["message"].startswith(message)


@pytest.mark.parametrize("arguments, message", [
    ({}, "query_metric needs 'metrics'"),
    ({"metrics": []}, "'metrics' must name at least one value"),
    ({"metrics": 5}, "'metrics' must be a list of non-empty strings"),
    ({"metrics": ["revenue", ""]}, "'metrics' must be a list of non-empty strings"),
    ({"metrics": ["revenue"], "by": [3]}, "'by' must be a list of non-empty strings"),
    ({"metrics": ["revenue"], "filters": {"city": "Pune"}}, "'filters' must be a list of non-empty strings"),
    ({"metrics": ["revenue"], "grain": 3}, "'grain' must be a non-empty string"),
    ({"metrics": ["revenue"], "max_rows": 0}, "'max_rows' must be at least 1"),
    ({"metrics": ["revenue"], "metric": "x"}, "query_metric does not take ['metric']; it takes ['by', 'end', "
                                              "'filters', 'grain', 'max_rows', 'metrics', 'start']"),
])
def test_arguments_of_the_wrong_shape_are_rejected_before_anything_is_compiled(tools, arguments, message):
    assert refused(tools, "query_metric", **arguments)["message"] == message


def test_metrics_of_two_tables_are_rejected_with_the_table_of_each(lake, tools):
    sales(lake, name="gold_other")
    spec = two_models()
    spec["semantic_models"][1]["table"] = "gold.gold_other"
    spec["metrics"].append({"name": "others", "type": "simple", "measure": "other_sum", "description": "Others."})
    write(lake / "metrics.json", spec)
    with LakeTools.from_sources([lake], iceberg_audit=False) as both:
        error = refused(both, "query_metric", metrics=["revenue", "others"])

    assert error["message"] == ("the metrics are on more than one semantic model, 'revenue' on daily_sales "
                                "(gold.gold_sales); 'others' on other (gold.gold_other). A query reads one table, so "
                                "ask for them in separate queries")


def test_a_metric_whose_table_the_server_does_not_serve_is_not_listed_and_is_unknown(lake):
    sales(lake, name="gold_other")
    with LakeTools.from_sources([lake], allow="gold.gold_other", iceberg_audit=False) as hidden:
        listed = good(hidden, "list_metrics")
        error = refused(hidden, "query_metric", metrics=["revenue"])

    assert listed["metrics"] == [] and listed["files"] == ["metrics.json"] and listed["problems"] == []
    assert error["type"] == "InvalidArguments" and error["message"] == "this server has no metrics to compute"
    with LakeTools.from_sources([lake], layer="gold", iceberg_audit=False) as served:
        assert len(good(served, "list_metrics")["metrics"]) == len(METRICS["metrics"])


# --------------------------------------------------------------------------- the MCP tools read the release


def test_query_metric_reads_the_current_release_and_a_new_release_is_read_at_the_next_call(lake, tools):
    build_gold(lake, release=True)
    gold_table(lake).put(table_of(ROWS[:2]))

    first = good(tools, "query_metric", metrics=["orders"])

    assert first["rows"] == [[36]] and first["release"] == "r1" and first["source"].startswith("release r1, cut 20")
    assert "__ldp_pinned" in first["sql"] and "gold.gold_sales@" in first["sql"]
    assert good(tools, "query", sql="SELECT sum(orders) AS n FROM gold.gold_sales")["rows"] == [["51"]]  # live, as text
    build_gold(lake, release=True)
    second = good(tools, "query_metric", metrics=["orders"])
    assert second["rows"] == [[51]] and second["release"] == "r2"
    by_year = good(tools, "query_metric", metrics=["orders"], grain="year")
    assert by_year["rows"] == [["2026-01-01", 45], ["2027-01-01", 6]]


def test_the_snapshot_of_a_release_is_read_after_the_table_has_moved_on(lake, tools):
    build_gold(lake, release=True)
    before = good(tools, "query_metric", metrics=["revenue", "avg_ticket"], by=["city"], grain="quarter")
    for _ in range(3):
        gold_table(lake).put(table_of(ROWS[:3]))

    after = good(tools, "query_metric", metrics=["revenue", "avg_ticket"], by=["city"], grain="quarter")

    assert after["rows"] == before["rows"] and after["release"] == "r1"
    assert good(tools, "sample_rows", table="gold.gold_sales", n=50)["row_count"] == 18


def test_a_table_that_the_current_release_does_not_hold_is_read_as_it_is_and_the_result_says_so(lake, tools):
    build_gold(lake, release=True)
    sales(lake, name="gold_extra")
    extra = copy.deepcopy(METRICS)
    extra["semantic_models"][0]["table"] = "gold_extra"
    write(lake / "metrics.json", extra)
    with LakeTools.from_sources([lake], iceberg_audit=False) as fresh:
        data = good(fresh, "query_metric", metrics=["orders"])

    assert data["rows"] == [[36]] and data["release"] is None
    assert data["source"] == "the tables as they are now, because release r1 does not hold gold.gold_extra"


# --------------------------------------------------------------------------- the sandbox


def test_the_sandbox_binds_the_values_that_are_given_and_the_guard_still_refuses_what_is_not_a_query(lake):
    from local_data_platform.mcp_server.errors import QueryRejected
    from local_data_platform.mcp_server.sandbox import DuckDBSandbox

    found = discover_tables([lake])
    try:
        with DuckDBSandbox(found.tables) as sandbox:
            plain = sandbox.query("SELECT $1 AS a, $2 AS b", max_rows=5, timeout_s=5, params=["x' OR '1'='1", 4])
            assert plain.table.to_pylist() == [{"a": "x' OR '1'='1", "b": 4}]
            counted = sandbox.query('SELECT count(*) AS n FROM "gold"."gold_sales" WHERE city = $1', max_rows=5,
                                    timeout_s=5, params=("Pune' OR 1=1 --",))
            assert counted.table.to_pylist() == [{"n": 0}]
            with pytest.raises(QueryRejected):
                sandbox.query("COPY (SELECT $1) TO 'x.csv'", max_rows=5, timeout_s=5, params=[1])
            assert sandbox.query("SELECT 1 AS a", max_rows=5, timeout_s=5, params=[]).table.to_pylist() == [{"a": 1}]
    finally:
        found.close()


@pytest.mark.parametrize("native", [None, False], ids=["native", "arrow"])
def test_a_view_of_a_table_at_one_snapshot_keeps_that_snapshot_when_the_table_moves_on(lake, native):
    from local_data_platform.mcp_server.errors import QueryFailed
    from local_data_platform.mcp_server.sandbox import PINNED_SCHEMA, DuckDBSandbox

    table = gold_table(lake)
    first = table.table().current_snapshot().snapshot_id
    table.put(table_of(ROWS[:2]))
    found = discover_tables([lake])
    try:
        with DuckDBSandbox(found.tables, native=native) as sandbox:
            name = sandbox.pin("gold.gold_sales", first)
            assert name.startswith(f'"{PINNED_SCHEMA}"."gold.gold_sales@')
            assert sandbox.pin("gold.gold_sales", first) == name
            old = sandbox.query(f"SELECT count(*) AS n FROM {name}", max_rows=5, timeout_s=5)
            live = sandbox.query('SELECT count(*) AS n FROM "gold"."gold_sales"', max_rows=5, timeout_s=5)
            assert (old.table.to_pylist(), live.table.to_pylist()) == ([{"n": 9}], [{"n": 11}])
            assert sandbox.columns(name)["order_date"] == "DATE" and sandbox.columns(name)["revenue"] == "DECIMAL(18,2)"
            assert sandbox.columns('"gold"."gold_sales"')["city"] == "VARCHAR"
            with pytest.raises(QueryFailed, match=r"gold.gold_sales has no snapshot 12345; its snapshots are \["):
                sandbox.pin("gold.gold_sales", 12345)
            with pytest.raises(KeyError):
                sandbox.columns("pg_catalog.pg_tables")
            with pytest.raises(KeyError):
                sandbox.pin("gold.nothing", first)
    finally:
        found.close()


# --------------------------------------------------------------------------- the catalog shows the metrics of a table


def test_the_catalog_lists_the_metrics_of_each_gold_table_in_the_order_of_the_file(lake):
    entry = catalogs.collect(lake, only="gold.gold_sales").entries[0]

    assert [item["name"] for item in entry.metrics] == [item["name"] for item in METRICS["metrics"]]
    assert dict(entry.metrics[2]) == {
        "name": "avg_ticket", "description": "Revenue divided by orders.", "owner": "finance@example.com",
        "type": "ratio", "format": "currency", "dimensions": ["city", "kitchen"], "grains": list(model.GRAINS)}
    assert entry.to_dict()["metrics"][0]["name"] == "revenue"
    assert "metric avg_ticket Revenue divided by orders." in entry.document().splitlines()
    assert catalogs.collect(lake).entries[0].metrics == entry.metrics


def test_a_table_that_no_metric_is_about_has_none_and_a_folder_without_a_metrics_file_has_none(lake, tmp_path):
    sales(lake, name="gold_other")

    entries = {item.table: item for item in catalogs.collect(lake).entries}
    assert entries["gold.gold_other"].metrics == () and len(entries["gold.gold_sales"].metrics) == 11
    assert "metric " not in entries["gold.gold_other"].document()
    (lake / "metrics.json").unlink()
    assert all(item.metrics == () for item in catalogs.collect(lake).entries)


def test_a_metric_of_a_table_that_is_not_a_gold_table_of_the_folder_is_not_shown(lake):
    sales(lake, name="gold_other")
    spec = with_model(table="gold.nowhere")
    write(lake / "metrics.json", spec)

    assert all(item.metrics == () for item in catalogs.collect(lake).entries)
    bare = with_model(table="gold_other")
    write(lake / "metrics.json", bare)
    entries = {item.table: item for item in catalogs.collect(lake).entries}
    assert len(entries["gold.gold_other"].metrics) == 11 and entries["gold.gold_sales"].metrics == ()


def test_a_metrics_file_that_cannot_be_read_leaves_the_catalog_without_metrics_and_is_logged(lake, caplog):
    write(lake / "metrics.json", with_metric("revenue", format="euro"))

    with caplog.at_level("WARNING"):
        entries = catalogs.collect(lake).entries

    assert all(item.metrics == () for item in entries)
    assert "Could not read the metrics of" in caplog.text and "metrics[0].format" in caplog.text


def test_ldp_gold_show_lists_the_metrics_of_the_table_with_their_owner_and_format(lake, capsys):
    code, out, _ = cli(capsys, "gold", "show", lake, "gold.gold_sales", "--no-samples")

    assert code == 0
    start = out.index("Metrics\n")
    block = out[start:].split("\n\n")[0].splitlines()
    assert block[0] == "Metrics" and cells(block[1]) == ["metric", "description", "owner", "format"]
    assert cells(block[3]) == ["revenue", "Money taken.", "analytics@example.com", "currency"]
    assert cells(block[5]) == ["avg_ticket", "Revenue divided by orders.", "finance@example.com", "currency"]
    code, out, _ = cli(capsys, "gold", "show", lake, "gold.gold_sales", "--json", "--no-samples")
    assert code == 0
    assert [item["name"] for item in json.loads(out)["metrics"]][:3] == ["revenue", "orders", "avg_ticket"]


def test_the_page_and_the_markdown_of_the_catalog_list_the_metrics_and_the_search_finds_them(lake, tmp_path):
    page, markdown = tmp_path / "catalog.html", tmp_path / "catalog.md"
    assert main(["gold", "docs", str(lake), "--out", str(page), "--no-samples"]) == 0
    assert main(["gold", "docs", str(lake), "--out", str(markdown), "--format", "md", "--no-samples"]) == 0

    html_text, md_text = page.read_text(), markdown.read_text()
    assert "Search tables, owners, columns and metrics" in html_text
    assert ">Metrics</summary>" in html_text.replace("\n", "")
    for text, name in ((html_text, "avg_ticket"), (md_text, "avg\\_ticket")):
        assert name in text and "finance@example.com" in text and "Revenue divided by orders." in text
    assert "### Metrics" in md_text and "| Metric | Description | Owner | Format |" in md_text
    assert 'data-search="gold.gold_sales' in html_text and "metric avg_ticket revenue divided by orders." in html_text


# --------------------------------------------------------------------------- what importing costs


def test_the_definitions_are_checked_without_importing_pyarrow_duckdb_or_pyiceberg():
    import subprocess
    import sys

    code = ("import sys, local_data_platform.metrics, local_data_platform.spec as spec; "
            "spec.validate_spec({'kind': 'Metrics'}); spec.kind_schema('Metrics'); "
            "print(sorted(m for m in ('pyarrow', 'duckdb', 'pyiceberg', 'mcp', 'local_data_platform.metrics.runner', "
            "'local_data_platform.metrics.sql', 'local_data_platform.metrics.check') if m in sys.modules))")

    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "[]"


# --------------------------------------------------------------------------- the pieces of SQL


@pytest.mark.parametrize("name, quoted", [("city", '"city"'), ("net revenue", '"net revenue"'), ('a"b', '"a""b"'),
                                          ("", '""'), ("select", '"select"')])
def test_a_name_is_quoted_as_a_duckdb_identifier(name, quoted):
    assert sqlparts.quote(name) == quoted


@pytest.mark.parametrize("agg, expr, written", [
    ("sum", "revenue", "sum(revenue)"), ("avg", "a + b", "avg(a + b)"), ("min", "x", "min(x)"), ("max", "x", "max(x)"),
    ("count", None, "count(*)"), ("count", "rating", "count(rating)"),
    ("count_distinct", "customer_id", "count(DISTINCT customer_id)")])
def test_an_aggregate_is_written_over_its_expression(agg, expr, written):
    assert sqlparts.aggregate(agg, expr) == written


def test_a_ratio_divides_a_sum_by_a_sum_that_is_no_value_when_it_is_zero():
    assert sqlparts.ratio("sum(a)", "sum(b)") == "sum(a) / NULLIF(sum(b), 0)"


@pytest.mark.parametrize("grain", list(model.GRAINS))
def test_a_time_bucket_is_the_start_of_the_period_as_a_date(grain):
    assert sqlparts.time_bucket("order date", grain) == f"CAST(date_trunc('{grain}', \"order date\") AS DATE)"


@pytest.mark.parametrize("grain", ["fortnight", "month'); DROP TABLE t; --", "", "MONTH"])
def test_a_time_bucket_of_anything_but_a_grain_is_refused_because_it_goes_into_the_sql_as_text(grain):
    with pytest.raises(ValueError, match="is not a grain; the grains are"):
        sqlparts.time_bucket("order_date", grain)


@pytest.mark.parametrize("kind, number", [
    ("TINYINT", True), ("BIGINT", True), ("HUGEINT", True), ("UBIGINT", True), ("DECIMAL(18,2)", True),
    ("decimal(38, 0)", True), ("DOUBLE", True), ("FLOAT", True), ("REAL", True), ("VARCHAR", False), ("DATE", False),
    ("BOOLEAN", False), ("TIMESTAMP", False), ("INTEGER[]", False), ("", False)])
def test_a_duckdb_type_is_a_number_when_it_is_an_integer_a_decimal_or_a_float(kind, number):
    assert sqlparts.numeric(kind) is number


@pytest.mark.parametrize("items, text", [([], ""), (["a"], "'a'"), (["a", "b"], "'a' and 'b'"),
                                         (["a", "b", "c"], "'a', 'b' and 'c'")])
def test_names_are_listed_for_a_message(items, text):
    assert sqlparts.names(items) == text


@pytest.fixture
def parser():
    connection = duckdb.connect(":memory:", config={"enable_external_access": False})
    yield connection
    connection.close()


def test_an_expression_is_inspected_for_the_columns_it_reads_in_order_and_once(parser):
    found = sqlparts.inspect_expression(parser, "CASE WHEN revenue > 10 THEN revenue ELSE orders END + coalesce(a, 0)")

    assert found.columns == ("revenue", "orders", "a") and found.problems == ()
    assert sqlparts.inspect_expression(parser, "1 + 2").columns == () and sqlparts.inspect_expression(
        parser, "upper(city) = 'PUNE'").columns == ("city",)
    assert sqlparts.inspect_expression(parser, '"net revenue" * 2').columns == ("net revenue",)


@pytest.mark.parametrize("expr, problem", [
    ("(SELECT 1)", "has a subquery, and a measure is an expression of the columns of one row"),
    ("sum(x) OVER ()", "has a window function, and a measure is an expression of the columns of one row"),
    ("*", "has a star, and a measure is an expression of the columns of one row"),
    ("x + ?", "has a parameter, and a measure is an expression of the columns of one row"),
    ("x + $1", "has a parameter, and a measure is an expression of the columns of one row"),
    ("#1", "has a positional reference, and a measure is an expression of the columns of one row"),
    ("t.x", "names the column 't.x' with a table in front. Write the column alone, because the query chooses the "
            "table"),
    ("main.t.x", "names the column 'main.t.x' with a table in front. Write the column alone, because the query "
                 "chooses the table"),
    ("max(x)", "calls the aggregate max(), and the measure already aggregates with its agg. An aggregate in an "
               "aggregate cannot work"),
    ("string_agg(x, ',')", "calls the aggregate string_agg(), and the measure already aggregates with its agg. An "
                           "aggregate in an aggregate cannot work"),
    ("x AS y", 'is not a SQL expression: syntax error at or near "AS"'),
    ("x;", 'is not a SQL expression: syntax error at or near ";"'),
    ("x, y", "is more than one expression, and a measure is one SQL expression"),
    ("x) FROM t --", "is more than one expression, and a measure is one SQL expression"),
    ("x) UNION SELECT (y", "is more than one expression, and a measure is one SQL expression"),
    ("x ORDER BY y", 'is not a SQL expression: syntax error at or near "ORDER"')])
def test_an_expression_that_is_more_than_one_value_of_the_row_is_found_out(parser, expr, problem):
    found = sqlparts.inspect_expression(parser, expr)

    assert found.problems and found.problems[0] == problem, found


def test_a_scalar_function_that_shares_a_name_with_no_aggregate_is_not_taken_for_one(parser):
    for expr in ("round(revenue, 2)", "least(a, b)", "list_max([a, b])", "date_trunc('month', d)",
                 "CAST(x AS INTEGER)"):
        assert sqlparts.inspect_expression(parser, expr).problems == (), expr


def test_the_one_expression_check_has_a_connection_of_its_own_and_says_what_is_wrong():
    sqlparts.only_expression("revenue * 1.2")
    with pytest.raises(ConfigError) as error:
        sqlparts.only_expression("(SELECT max(x) FROM t)")

    assert str(error.value) == ("the expression '(SELECT max(x) FROM t)' has a subquery, and a measure is an "
                                "expression of the columns of one row")


def test_the_metric_calls_are_also_audited_to_the_ldp_audit_table_with_their_lists(lake):
    from local_data_platform.pipeline.builders import iceberg_from_config

    with LakeTools.from_sources([lake]) as served:
        assert served.audit.iceberg_enabled
        served.call("list_metrics", {})
        served.call("query_metric", {"metrics": ["revenue", "orders"], "by": ["city"], "filters": ["city=Pune"]})
        served.call("query_metric", {"metrics": ["nope"]})
    config = load_config_of(lake)
    rows = iceberg_from_config(config, "target").table().catalog.load_table("_ldp.audit").scan().to_arrow().to_pylist()

    by_tool = {(row["tool"], row["status"]): row for row in rows}
    assert sorted(by_tool) == [("list_metrics", "ok"), ("query_metric", "ok"), ("query_metric", "rejected")]
    computed = by_tool[("query_metric", "ok")]
    assert json.loads(computed["arguments"]) == {"metrics": ["revenue", "orders"], "by": ["city"],
                                                 "filters": ["city=Pune"]}
    assert computed["table_identifier"] == "gold.gold_sales" and computed["sql"].startswith("SELECT\n")
    assert computed["rows"] == 1 and "Pune" not in computed["sql"]


def load_config_of(root: Path):
    from local_data_platform import Config

    return Config.from_json(root / "gold_sales.json")


def test_a_schema_that_duckdb_cannot_read_is_a_problem_of_the_table_and_not_a_crash(tmp_path):
    class Unreadable:
        names = ["a"]

        def empty_table(self):
            raise duckdb.InvalidInputException("Invalid Input Error: cannot register the arrow table\nsecond line")

    assert fit(tmp_path, METRICS, {"gold.gold_sales": Unreadable()}) == [
        f"{HERE}, table: DuckDB cannot read the schema of gold.gold_sales. Invalid Input Error: cannot register the "
        "arrow table"]


@pytest.mark.parametrize("native", [None, False], ids=["native", "arrow"])
def test_a_table_has_one_pinned_view_at_a_time_so_a_server_that_sees_many_releases_keeps_one(lake, native):
    from local_data_platform.mcp_server.errors import QueryFailed
    from local_data_platform.mcp_server.sandbox import DuckDBSandbox

    table = gold_table(lake)
    first = table.table().current_snapshot().snapshot_id
    table.put(table_of(ROWS[:2]))
    second = table.table().current_snapshot().snapshot_id
    found = discover_tables([lake])
    try:
        with DuckDBSandbox(found.tables, native=native) as sandbox:
            old = sandbox.pin("gold.gold_sales", first)
            new = sandbox.pin("gold.gold_sales", second)

            assert old != new
            assert sandbox.query(f"SELECT count(*) AS n FROM {new}", max_rows=5, timeout_s=5).table.to_pylist() == [
                {"n": 11}]
            with pytest.raises(QueryFailed):
                sandbox.query(f"SELECT count(*) AS n FROM {old}", max_rows=5, timeout_s=5)
            with pytest.raises(KeyError):
                sandbox.columns(old)
            assert sandbox.pin("gold.gold_sales", first) == old and sandbox.columns(old)
            with pytest.raises(KeyError):
                sandbox.columns(new)
            assert len(sandbox._pinned) == 1
    finally:
        found.close()


def test_list_metrics_names_the_table_in_full_so_that_an_agent_can_describe_it(lake, tools):
    spec = with_model(table="gold_sales")
    write(lake / "metrics.json", spec)

    listed = good(tools, "list_metrics")["metrics"]

    assert {item["table"] for item in listed} == {"gold.gold_sales"}
    assert good(tools, "describe_table", table=listed[0]["table"])["table"] == "gold.gold_sales"
    assert good(tools, "query_metric", metrics=["orders"])["table"] == "gold.gold_sales"
    assert load.load_metrics(lake).model("daily_sales").table == "gold_sales"


# --------------------------------------------------------------------------- the edges of the checks

NAME_LIMIT = "a" * 63


def test_a_name_may_have_63_characters_and_not_64():
    assert messages(with_metric("revenue", name=NAME_LIMIT)) == []
    assert messages(with_metric("revenue", name=NAME_LIMIT + "a")) == [
        "metrics[0].name: must be lower case letters, digits and underscores, starting with a letter, and at most 63 "
        f"characters, got '{NAME_LIMIT}a'"]
    assert messages(with_model(name=NAME_LIMIT)) == []


def test_an_expression_may_have_2000_characters_and_not_2001():
    long = "revenue" + " + 0" * 498 + " "
    spec = with_model(measures=[{"name": "revenue_sum", "agg": "sum", "expr": long}]) | {
        "metrics": [METRICS["metrics"][0]]}

    assert len(long) == 2000 and messages(spec) == []


def test_a_name_that_is_empty_is_not_a_name():
    assert messages(with_model(name="")) == ["semantic_models[0].name: is required and must be a non-empty string"]
    assert messages(with_metric("revenue", name="")) == ["metrics[0].name: is required and must be a non-empty string"]


def test_a_semantic_model_and_a_measure_may_be_called_like_a_grain_and_a_dimension_and_a_metric_may_not():
    renamed = with_model(name="year")
    renamed["semantic_models"][0]["measures"].append({"name": "month", "agg": "count"})

    assert messages(renamed) == []
    named = messages(with_model(dimensions=["year"]))[0]
    assert named.startswith("semantic_models[0].dimensions[0]: 'year' is the name of a grain")


def test_the_column_of_the_time_dimension_is_found_by_any_dimension_that_reads_it_whatever_the_case_and_the_blanks():
    clash = ("semantic_models[0].dimensions[0]: is the column of the time dimension, 'order_date'. Group by it with "
             "--grain")

    assert messages(with_model(dimensions=[{"name": "when", "column": "ORDER_DATE"}])) == [clash]
    assert messages(with_model(dimensions=[{"name": "when", "column": "order_date"}])) == [clash]
    spaced = with_model(time_dimension={"column": "  order_date ", "grains": ["day"]},
                        dimensions=[{"name": "when", "column": "order_date"}])
    assert messages(spaced) == [clash]
    assert messages(with_model(dimensions=[{"name": "when", "column": "created"}])) == []


def test_the_second_use_of_a_measure_name_in_a_model_that_took_it_from_another_is_an_earlier_measure_of_its_own():
    spec = two_models()
    spec["semantic_models"][1]["measures"] = [{"name": "revenue_sum", "agg": "count"},
                                              {"name": "revenue_sum", "agg": "count"}]

    assert messages(spec) == [
        "semantic_models[1].measures[0].name: 'revenue_sum' is already a measure of the semantic model 'daily_sales'. "
        "A measure name is unique in the file, because a metric refers to it by name",
        "semantic_models[1].measures[1].name: 'revenue_sum' is the name of an earlier measure"]


def test_a_measure_with_an_agg_that_does_not_exist_is_not_also_missing_its_expression():
    spec = with_model(measures=[{"name": "revenue_sum", "agg": "median"}]) | {"metrics": [METRICS["metrics"][0]]}

    assert messages(spec) == [
        "semantic_models[0].measures[0].agg: is required and must be one of ['sum', 'count', 'count_distinct', 'avg', "
        "'min', 'max'], got 'median'"]


def test_a_measure_whose_name_is_not_a_name_does_not_make_its_metrics_look_unknown():
    spec = with_model(measures=[{"name": "Revenue Sum", "agg": "sum", "expr": "revenue"}]) | {
        "metrics": [METRICS["metrics"][0]]}

    assert messages(spec) == [
        "semantic_models[0].measures[0].name: must be lower case letters, digits and underscores, starting with a "
        "letter, and at most 63 characters, got 'Revenue Sum'"]


def test_what_is_built_from_a_file_has_its_text_without_the_blanks_around_it():
    spec = with_model(table=" gold.gold_sales ",
                      dimensions=[{"name": "kitchen", "column": " cuisine ", "description": " Food. "}])
    spec["metrics"][0]["description"] = "  Money taken.  "
    models, found = model.build(spec, "m.json")

    assert models[0].table == "gold.gold_sales"
    assert models[0].dimensions == (model.Dimension("kitchen", "cuisine", "Food."),)
    assert found[0].description == "Money taken."
    assert found[2].measures == ("revenue_sum", "order_sum") and found[0].measures == ("revenue_sum",)


def test_the_definitions_of_files_in_two_folders_say_the_folder_of_the_first(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    metrics_file(first)
    other = {**METRICS, "identifier": "other", "semantic_models": [{**MODEL, "name": "more", "measures": [
        {"name": "more_sum", "agg": "sum", "expr": "orders"}]}], "metrics": [
        {"name": "more_orders", "type": "simple", "measure": "more_sum", "description": "More."}]}
    write(second / "metrics.json", other)

    found = load.load_all([first, second])

    assert found.folder == first.resolve() and [item.parent.name for item in found.files] == ["first", "second"]
    assert found.metric("more_orders").model == "more"


def test_a_metric_of_a_table_with_no_time_dimension_has_no_time_column_in_its_description(tmp_path):
    found = defined(tmp_path, with_model(time_dimension=None))

    assert found.describe(found.metric("revenue"))["time_column"] is None
    assert found.describe(found.metric("revenue"))["grains"] == []


def test_a_check_that_found_nothing_to_check_is_not_valid(tmp_path):
    assert check.FolderCheck(tmp_path).valid is False and check.FolderCheck(tmp_path).to_dict()["valid"] is False


def test_a_function_that_aggregates_and_does_not_is_taken_for_one_only_when_it_only_aggregates():
    class Rows:
        def execute(self, text):
            return self

        def fetchall(self):
            return [("round", "scalar"), ("round", "aggregate"), ("sum", "aggregate"), ("list", "aggregate"),
                    ("upper", "scalar")]

    sqlparts._AGGREGATES = None
    try:
        assert sqlparts._aggregate_names(Rows()) == frozenset({"sum", "list"})
    finally:
        sqlparts._AGGREGATES = None


@pytest.mark.parametrize("expr", ["x) WHERE (1=1) --", "x) LIMIT 1 --", "x) ORDER BY 1 --", "x) GROUP BY 1 --",
                                  "x) HAVING (1=1) --", "x) QUALIFY (1=1) --", "x) FROM t --", "x) OFFSET 1 --",
                                  "x) --"])
def test_text_that_closes_the_parenthesis_and_adds_one_clause_is_found_out_whatever_the_clause_is(parser, expr):
    found = sqlparts.inspect_expression(parser, expr)

    assert found.problems == ("is more than one expression, and a measure is one SQL expression",), expr


def test_an_aggregate_is_known_by_the_name_that_the_parser_gives_it_in_any_case():
    node = {"class": "FUNCTION", "function_name": "SUM", "children": []}
    columns: list[str] = []
    problems: list[str] = []

    sqlparts._walk(node, columns, problems, frozenset({"sum"}))

    assert problems == ["calls the aggregate SUM(), and the measure already aggregates with its agg. An aggregate "
                        "in an aggregate cannot work"]


def test_a_timestamp_parameter_is_shown_as_a_timestamp_and_a_date_as_a_date(definitions):
    stamped = {**TYPES, "order_date": "TIMESTAMP"}
    compiled = compiler.compile_query(definitions, "revenue", table="gold.gold_sales", types=stamped,
                                      start="2026-02-03")

    assert compiled.explain().splitlines()[-1] == "-- $1 = TIMESTAMP '2026-02-03 00:00:00'"
    assert compile_sales(definitions, "revenue", start="2026-02-03").explain().splitlines()[-1] == (
        "-- $1 = DATE '2026-02-03'")
    assert compiled.to_dict()["parameters"] == ["2026-02-03T00:00:00"]


def test_a_table_whose_columns_are_written_in_capitals_is_read_by_the_names_of_the_file(tmp_path):
    shouting = pa.schema([("ORDER_DATE", pa.date32()), ("City", pa.string()), ("cuisine", pa.string()),
                          ("customer_id", pa.string()), ("Orders", pa.int64()), ("Revenue", pa.decimal128(18, 2)),
                          ("rating", pa.float64())])
    sales(tmp_path, schema=shouting)
    metrics_file(tmp_path)

    assert check.validate_folder(tmp_path).problems == []
    found = metrics.query(tmp_path, "orders,revenue", by="city", grain="year", filters=["city=Pune"],
                          start="2026-01-01", end="2026-12-31")
    assert answer(found) == [(dt.date(2026, 1, 1), "Pune", 22, D("294.50"))]


def test_a_number_that_is_shown_whole_is_rounded_and_one_that_is_not_has_two_decimals_and_groups_thousands():
    assert render.display(3.7, "number", whole=True) == "4" and render.display(3.5, "number", whole=True) == "4"
    assert render.display(1234.567, "number") == "1,234.57" and render.display(-1234.5, "number") == "-1,234.50"
    assert render.display(float("inf"), "percent") == "inf" and render.display(float("-inf"), "currency") == "-inf"


def test_a_description_of_the_width_of_its_column_is_shown_whole_and_a_longer_one_is_cut_with_dots(lake, capsys):
    exact = "x" * render.DESCRIPTION_WIDTH
    names = [f"dimension_{number}" for number in range(6)]
    spec = with_metric("revenue", description=exact)
    spec["metrics"][1]["description"] = exact + "y"
    spec["semantic_models"][0]["dimensions"] = names
    write(lake / "metrics.json", spec)

    rows = [cells(line) for line in cli(capsys, "metrics", "list", lake)[1].splitlines()[2:4]]

    assert rows[0][1] == exact and rows[1][1] == "x" * (render.DESCRIPTION_WIDTH - 3) + "..."
    assert rows[0][5] == ", ".join(names)[:render.LIST_WIDTH - 3] + "..." and len(rows[0][5]) == render.LIST_WIDTH


def test_a_folder_with_no_metrics_file_is_not_a_warning_for_the_catalog(lake, caplog):
    (lake / "metrics.json").unlink()

    with caplog.at_level("WARNING"):
        catalogs.collect(lake)

    assert "Could not read the metrics" not in caplog.text


def test_a_tool_that_does_not_exist_is_refused_with_the_names_of_the_eight_that_do(tools):
    outcome = ask(tools, "list_everything")

    assert not outcome.ok and outcome.status == "rejected"
    assert outcome.data["error"]["message"] == (
        "unknown tool 'list_everything'; the tools are list_tables, describe_table, query, sample_rows, "
        "table_history, get_dataset, list_metrics, query_metric")


def test_an_optional_list_may_be_empty_and_a_required_one_may_not(tools):
    data = good(tools, "query_metric", metrics=["orders"], by=[], filters=[])

    assert data["rows"] == [[36]] and [item["name"] for item in data["columns"]] == ["orders"]
    assert refused(tools, "query_metric", metrics=[])["message"] == "'metrics' must name at least one value"


def test_a_server_over_a_table_with_capital_column_names_reads_them_by_the_names_of_the_file(tmp_path):
    shouting = pa.schema([("ORDER_DATE", pa.date32()), ("City", pa.string()), ("cuisine", pa.string()),
                          ("customer_id", pa.string()), ("Orders", pa.int64()), ("Revenue", pa.decimal128(18, 2)),
                          ("rating", pa.float64())])
    sales(tmp_path, schema=shouting)
    metrics_file(tmp_path)
    build_gold(tmp_path, release=True)

    with LakeTools.from_sources([tmp_path], iceberg_audit=False) as served:
        data = good(served, "query_metric", metrics=["orders"], by=["city"], grain="year", filters=["city=Pune"])

    assert data["rows"] == [["2026-01-01", "Pune", 22]] and data["release"] == "r1"


@pytest.mark.parametrize("native", [None, False], ids=["native", "arrow"])
def test_a_pinned_view_that_has_gone_is_made_again_and_the_arrow_data_of_a_dropped_one_is_freed(lake, native):
    import duckdb as duckdb_module

    from local_data_platform.mcp_server.sandbox import DuckDBSandbox

    table = gold_table(lake)
    first = table.table().current_snapshot().snapshot_id
    table.put(table_of(ROWS[:2]))
    second = table.table().current_snapshot().snapshot_id
    found = discover_tables([lake])
    try:
        with DuckDBSandbox(found.tables, native=native) as sandbox:
            name = sandbox.pin("gold.gold_sales", first)
            sandbox._con.execute(f"DROP VIEW {name}")

            assert sandbox.pin("gold.gold_sales", first) == name
            assert sandbox.query(f"SELECT count(*) AS n FROM {name}", max_rows=5, timeout_s=5).table.to_pylist() == [
                {"n": 9}]
            arrow = sandbox._pinned[name].arrow_name
            assert (arrow is None) == sandbox.native_available  # native=None reads natively only with the extension
            sandbox.pin("gold.gold_sales", second)
            if arrow is not None:
                with pytest.raises(duckdb_module.Error):
                    sandbox._con.execute(f'SELECT * FROM "{arrow}"').fetchall()
    finally:
        found.close()


def test_ldp_mcp_says_how_many_metrics_it_serves_and_warns_about_a_wrong_metrics_file(lake, capsys, monkeypatch):
    pytest.importorskip("mcp")
    from local_data_platform.mcp_server import server
    from local_data_platform.mcp_server.cli import main as mcp_main

    monkeypatch.setattr(server, "serve_stdio", lambda served: None)

    assert mcp_main(["--config", str(lake), "--no-iceberg-audit"]) == 0
    err = capsys.readouterr().err
    assert f"ldp mcp: serving 1 tables, {len(METRICS['metrics'])} metrics over stdio (gold.gold_sales)" in err
    assert "ldp mcp: warning" not in err
    write(lake / "metrics.json", with_metric("revenue", format="euro"))
    assert mcp_main(["--config", str(lake), "--no-iceberg-audit"]) == 0
    err = capsys.readouterr().err
    assert "ldp mcp: serving 1 tables over stdio (gold.gold_sales)" in err
    assert "ldp mcp: warning: the metrics are not served. metrics.json: metrics[0].format: must be one of" in err
    (lake / "metrics.json").unlink()
    assert mcp_main(["--config", str(lake), "--no-iceberg-audit"]) == 0
    assert "metrics" not in capsys.readouterr().err.replace("the metrics are not served", "")


# --------------------------------------------------------------------------- the churn metrics of the example

CHURN_FILE = Path(__file__).resolve().parents[1] / "examples" / "restaurant" / "config" / "metrics.json"
CHURN_SCHEMA = pa.schema([("month", pa.date32()), ("customers_at_start", pa.int64()), ("churned", pa.int64()),
                          ("churn_rate", pa.float64()), ("active_customers", pa.int64())])
ISSUE_47 = [
    (dt.date(2023, 2, 1), 3, 3, 1.0, 1),
    (dt.date(2023, 3, 1), 1, 0, 0.0, 2),
    (dt.date(2023, 4, 1), 2, 1, 0.5, 1),
    (dt.date(2023, 5, 1), 1, 1, 1.0, 0),
    (dt.date(2023, 6, 1), 0, 0, None, 1),
]
"""``churn_by_month`` as the model of the restaurant example gives it for the orders of issue #47, which
``test_restaurant_warehouse.py`` builds with dbt. January, the first month, has no row."""


@pytest.fixture
def churn_lake(tmp_path):
    """A folder with the table of issue #47 and the ``metrics.json`` that the restaurant example ships."""
    sales(tmp_path, ISSUE_47, CHURN_SCHEMA, name="churn_by_month")
    metrics_file(tmp_path, json.loads(CHURN_FILE.read_text()))
    return tmp_path


def test_the_churn_metrics_that_the_restaurant_example_ships_fit_the_columns_of_its_model(churn_lake, capsys):
    code, out, err = cli(capsys, "metrics", "validate", churn_lake)

    assert (code, err) == (0, "")
    assert out == f"metrics.json: 1 semantic model and 2 metrics fit the gold tables of {churn_lake.resolve()}\n"


def test_the_example_of_issue_47_runs_through_ldp_metrics_query_and_february_is_100_percent(churn_lake, capsys):
    code, out, err = cli(capsys, "metrics", "query", churn_lake, "churn_rate", "--grain", "month")

    lines = out.splitlines()
    assert (code, err) == (0, "")
    assert lines[:2] == ["month       churn_rate", "----------  ----------"]
    assert [cells(line) for line in lines[2:7]] == [
        ["2023-02-01", "100.0%"], ["2023-03-01", "0.0%"], ["2023-04-01", "50.0%"], ["2023-05-01", "100.0%"],
        ["2023-06-01", "NULL"]]  # nobody ordered in May, so June has no rate
    assert lines[7] == "(5 rows)"
    assert not any(line.startswith("2023-01") for line in lines), "January has no row in the model, so no value"


def test_the_churn_metrics_give_the_rows_of_the_sql_written_by_hand_and_a_month_with_no_customers_has_no_rate(
        churn_lake):
    table = metrics.query(churn_lake, "churn_rate,active_customers", grain="month")

    by_hand = hand("SELECT month, sum(churned) / NULLIF(sum(customers_at_start), 0), sum(active_customers) FROM t "
                   "GROUP BY month ORDER BY month", rows=ISSUE_47, schema=CHURN_SCHEMA)
    assert close(answer(table), by_hand)
    assert [row[1] for row in answer(table)] == [1.0, 0.0, 0.5, 1.0, None]
    assert [row[2] for row in answer(table)] == [1, 2, 1, 0, 1]


@pytest.mark.parametrize("options", [{"grain": "day"}, {"grain": "week"}, {"grain": "quarter"}, {"grain": "year"}, {}],
                         ids=["day", "week", "quarter", "year", "whole period"])
def test_the_churn_metrics_allow_only_the_month_grain_so_that_no_one_adds_up_rates_by_mistake(churn_lake, options):
    with pytest.raises(ConfigError, match=r"the grains \['month'\]"):
        metrics.query(churn_lake, "churn_rate,active_customers", **options)
