"""Gold tables as one SQL file over other tables, and ``ldp gold build``.

A gold config reads the Iceberg tables of its ``inputs`` through a ``DUCKDB`` source, and ``ldp gold build`` builds the
gold configs of a folder in the order that their inputs say, and checks the ones that something else builds. Everything
here is offline. Where DuckDB's iceberg extension is not installed the tables are registered in memory, which gives
the same rows, so the tests need no extension.
"""

import json
import re
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.etl import run_config
from local_data_platform.events import (
    MemorySink, OpenLineageSink, iceberg_block_ref, read_iceberg_events, read_openlineage)
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.gold.build import (
    BLOCKED, BUILT, CHECKED, FAILED, NOT_RUN, build_gold, check_built_elsewhere, find_gold, order_gold)
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline
from local_data_platform.spec import canonical_spec, json_schema, plan, spec_hash, validate_spec
from local_data_platform.store.source.duckdb import DuckDBTable

pytest.importorskip("duckdb")

ORDERS = "order_id,restaurant,amount\n1,north,10.5\n2,north,20.0\n3,south,7.25\n"
REVENUE_SQL = ("SELECT restaurant, count(*) AS orders, sum(amount) AS revenue FROM silver_orders "
               "GROUP BY restaurant ORDER BY restaurant")
REVENUE_COLUMNS = {"restaurant": "The restaurant.", "orders": "How many orders.", "revenue": "What they cost."}
SILVER_CATALOG = {"identifier": "lake", "warehouse_path": "warehouse"}
GOLD_CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}


@pytest.fixture(params=["auto", "memory"])
def reading(request, monkeypatch):
    """Run a test with DuckDB's iceberg extension as the machine has it, and again with it unavailable."""
    if request.param == "memory":
        monkeypatch.setattr(DuckDBEngine, "_load_iceberg_extension", lambda self: "forced off by the test")
    return request.param


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def silver(root: Path, name: str = "silver_orders", csv: str = ORDERS, *, build: bool = True) -> Path:
    """A config that loads ``orders.csv`` into ``lake.<name>``. With ``build`` the table is loaded too."""
    write(root / "orders.csv", csv)
    path = write(root / f"{name}.json", {"identifier": name, "who": "ops", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": name, "format": "ICEBERG", "write_mode": "overwrite", "catalog": SILVER_CATALOG}}})
    if build:
        run_config(path)
    return path


def gold(root: Path, name: str = "gold_revenue", sql: str = REVENUE_SQL, inputs=("silver_orders.json",), *,
         columns=None, quality=None, observability=None, inline: bool = False, docs=None, owner: str | None = None,
         source: dict | None = None) -> Path:
    """A gold config of ``root``: ``gold.<name>`` from one query over ``inputs``, with a complete contract."""
    columns = REVENUE_COLUMNS if columns is None else columns
    block = {"name": name, "format": "DUCKDB", "inputs": list(inputs)}
    if inline:
        block["sql"] = sql
    else:
        write(root / "models" / f"{name}.sql", sql + "\n")
        block["query_file"] = f"models/{name}.sql"
    metadata = {"source": {**block, **(source or {})},
                "target": {"name": name, "format": "ICEBERG", "write_mode": "overwrite", "catalog": GOLD_CATALOG},
                "docs": {"layer": "gold", "description": f"The {name} table.", "grain": "one row per restaurant",
                         "columns": dict(columns), **(docs or {})}}
    if quality is not None:
        metadata["quality"] = quality
    if observability is not None:
        metadata["observability"] = observability
    return write(root / f"{name}.json", {"identifier": name, **({"who": owner or "ops"}), "metadata": metadata})


def table(root: Path, name: str, namespace: str = "gold") -> Iceberg:
    return Iceberg(name, {"identifier": namespace, "warehouse_path": str(root / "warehouse")})


def rows(root: Path, name: str, namespace: str = "gold") -> list[dict]:
    return table(root, name, namespace).get().to_pylist()


# --------------------------------------------------------------------------- the source


@pytest.mark.usefixtures("reading")
def test_a_gold_table_is_one_sql_file_over_the_tables_of_its_inputs(tmp_path):
    silver(tmp_path)
    path = gold(tmp_path)

    result = run_config(path)

    assert result.status == "published" and result.rows_written == 2
    assert rows(tmp_path, "gold_revenue") == [{"restaurant": "north", "orders": 2, "revenue": 30.5},
                                              {"restaurant": "south", "orders": 1, "revenue": 7.25}]


def test_the_keys_of_an_inputs_source_are_known_and_not_reported_as_ignored(tmp_path, caplog):
    silver(tmp_path)
    path = gold(tmp_path)

    with caplog.at_level("WARNING"):
        run_config(path)

    assert "are not used and are ignored" not in caplog.text


def test_the_query_can_be_in_the_config_as_well_as_in_a_file(tmp_path):
    silver(tmp_path)

    run_config(gold(tmp_path, inline=True))

    assert [row["restaurant"] for row in rows(tmp_path, "gold_revenue")] == ["north", "south"]


def test_an_input_is_registered_under_the_name_of_its_table_and_not_of_its_file(tmp_path):
    silver(tmp_path, "silver_orders")
    (tmp_path / "silver_orders.json").rename(tmp_path / "the_orders_pipeline.json")

    run_config(gold(tmp_path, inputs=["the_orders_pipeline.json"]))

    assert len(rows(tmp_path, "gold_revenue")) == 2


@pytest.mark.usefixtures("reading")
def test_a_query_that_reads_a_table_that_is_not_an_input_fails_with_the_unknown_table_error(tmp_path):
    silver(tmp_path)
    path = gold(tmp_path, sql=REVENUE_SQL.replace("silver_orders", "some_other_table"))

    with pytest.raises(ConfigError) as error:
        run_config(path)

    message = str(error.value)
    assert "the query failed over the inputs (silver_orders)" in message
    assert "Table with name some_other_table does not exist" in message


def test_an_input_that_has_no_table_yet_says_which_pipeline_to_run(tmp_path):
    silver(tmp_path, build=False)
    path = gold(tmp_path)

    with pytest.raises(TableNotFound, match=r"reads a table that silver_orders\.json writes.*Run that pipeline first"):
        run_config(path)


def test_two_inputs_that_write_tables_with_the_same_name_are_refused(tmp_path):
    silver(tmp_path)
    other = json.loads((tmp_path / "silver_orders.json").read_text())
    other["identifier"] = "elsewhere"
    other["metadata"]["target"]["catalog"] = {"identifier": "archive", "warehouse_path": "warehouse"}
    write(tmp_path / "elsewhere.json", other)

    with pytest.raises(ConfigError, match=r"both write a table called 'silver_orders'"):
        DuckDBTable("g", sql="select 1", inputs=["silver_orders.json", "elsewhere.json"], base_dir=tmp_path)


def test_an_input_that_writes_a_file_is_refused(tmp_path):
    write(tmp_path / "orders.csv", ORDERS)
    write(tmp_path / "export.json", {"identifier": "export", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": "out", "format": "CSV", "path": "out.csv"}}})

    with pytest.raises(ConfigError, match="writes to a CSV target, and a query reads Iceberg tables"):
        DuckDBTable("g", sql="select 1", inputs=["export.json"], base_dir=tmp_path)


def test_an_input_config_that_is_missing_or_not_valid_fails_when_the_source_is_built(tmp_path):
    write(tmp_path / "broken.json", {"identifier": "broken", "metadata": {"source": {}}})

    with pytest.raises(ConfigError, match="config file not found"):
        DuckDBTable("g", sql="select 1", inputs=["nope.json"], base_dir=tmp_path)
    with pytest.raises(ConfigError, match="metadata"):
        DuckDBTable("g", sql="select 1", inputs=["broken.json"], base_dir=tmp_path)


@pytest.mark.parametrize("keywords, message", [
    ({"path": "x.duckdb", "inputs": ["a.json"], "sql": "select 1"}, "both a 'path' and 'inputs'"),
    ({"inputs": ["a.json"], "relation": "t"}, "'inputs' and a 'relation'"),
    ({"inputs": [], "sql": "select 1"}, "'inputs' must be a list of config paths"),
    ({"inputs": "a.json", "sql": "select 1"}, "'inputs' must be a list of config paths"),
    ({"inputs": [""], "sql": "select 1"}, "'inputs' must be a list of config paths"),
    ({"sql": "select 1"}, "needs a path, or inputs to read instead"),
])
def test_the_source_needs_a_path_or_inputs_and_not_both_and_a_relation_has_no_inputs(tmp_path, keywords, message):
    with pytest.raises(ConfigError, match=message):
        DuckDBTable("g", base_dir=tmp_path, **keywords)


def test_a_query_file_that_is_missing_or_empty_is_a_config_error(tmp_path):
    silver(tmp_path)
    source = DuckDBTable("g", query_file="models/nope.sql", inputs=["silver_orders.json"], base_dir=tmp_path)
    with pytest.raises(ConfigError, match="cannot read the query file"):
        source.get()
    write(tmp_path / "models" / "empty.sql", "  \n")
    with pytest.raises(ConfigError, match="the query file .* is empty"):
        DuckDBTable("g", query_file="models/empty.sql", inputs=["silver_orders.json"], base_dir=tmp_path).get()


def test_only_one_read_only_query_is_run(tmp_path):
    silver(tmp_path)
    for text in ("DROP TABLE silver_orders", "SELECT 1; SELECT 2"):
        with pytest.raises(ConfigError, match="DuckDB source 'g'"):
            DuckDBTable("g", sql=text, inputs=["silver_orders.json"], base_dir=tmp_path).get()


def test_a_query_file_can_read_a_duckdb_file_too(tmp_path):
    import duckdb

    database = tmp_path / "build.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE t AS SELECT 2 AS n")
    write(tmp_path / "q.sql", "SELECT n * 21 AS answer FROM t")

    result = DuckDBTable("q", database, query_file="q.sql", base_dir=tmp_path).get()

    assert result.to_pylist() == [{"answer": 42}]


@pytest.mark.usefixtures("reading")
def test_the_session_time_zone_of_a_query_over_inputs_is_utc(tmp_path):
    silver(tmp_path)

    result = DuckDBTable("g", sql="SELECT current_setting('TimeZone') AS zone", inputs=["silver_orders.json"],
                         base_dir=tmp_path).get()

    assert result.to_pylist() == [{"zone": "UTC"}]


@pytest.mark.usefixtures("reading")
def test_the_source_reads_the_snapshot_that_is_current_when_it_is_read(tmp_path):
    silver(tmp_path)
    source = DuckDBTable("g", sql="SELECT count(*) AS n FROM silver_orders", inputs=["silver_orders.json"],
                         base_dir=tmp_path)
    assert source.get().to_pylist() == [{"n": 3}]

    silver(tmp_path, csv=ORDERS + "4,south,1.0\n")

    assert source.get().to_pylist() == [{"n": 4}]


def test_the_lineage_names_the_input_tables_and_opens_nothing(tmp_path):
    silver(tmp_path, build=False)
    source = DuckDBTable("g", sql="select 1", inputs=["silver_orders.json"], base_dir=tmp_path)

    lineage = source.lineage()

    assert lineage == [{"namespace": (tmp_path / "warehouse").resolve().as_uri(), "name": "lake.silver_orders",
                        "format": "ICEBERG", "table_identifier": "lake.silver_orders"}]
    assert not (tmp_path / "warehouse").exists(), "naming a table creates no catalog and no folder"
    assert DuckDBTable("f", tmp_path / "x.duckdb", relation="t").lineage() == []


def test_ldp_pipelines_still_lists_one_duckdb_route_and_no_route_from_iceberg_to_iceberg(capsys):
    assert main(["pipelines"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert sum(1 for line in lines if re.match(r"DUCKDB\s+ICEBERG\s", line)) == 1
    assert not [line for line in lines if re.match(r"ICEBERG\s+ICEBERG\s", line)]


# --------------------------------------------------------------------------- the spec


def spec_of(**source) -> dict:
    block = {"name": "g", "format": "DUCKDB", "query_file": "q.sql", "inputs": ["a.json"], **source}
    return {"identifier": "g", "metadata": {"source": {k: v for k, v in block.items() if v is not None},
                                            "target": {"name": "g", "format": "ICEBERG", "catalog": GOLD_CATALOG}}}


def test_validate_spec_accepts_a_query_file_or_sql_over_inputs_and_needs_no_path():
    assert validate_spec(spec_of()) == []
    assert validate_spec(spec_of(query_file=None, sql="select 1")) == []
    assert validate_spec(spec_of(inputs=None, path="x.duckdb")) == []


@pytest.mark.parametrize("source, fragment", [
    ({"sql": "select 1"}, "needs exactly one of 'relation'"),
    ({"sql": "select 1"}, "got more than one"),
    ({"query_file": None}, "got none"),
    ({"path": "x.duckdb"}, "has both 'path' and 'inputs'"),
    ({"query_file": None, "relation": "t"}, "cannot go with a 'relation'"),
    ({"inputs": []}, "must be a non-empty list of dataset config paths"),
    ({"inputs": ["a.json", ""]}, "must be a non-empty list of dataset config paths"),
    ({"inputs": "a.json"}, "must be a non-empty list of dataset config paths"),
    ({"inputs": ["a.json", "b.json", "a.json"]}, "lists 'a.json' a second time"),
    ({"query_file": ""}, "must be a non-empty string"),
])
def test_validate_spec_names_what_is_wrong_with_an_inputs_source(source, fragment):
    errors = [str(error) for error in validate_spec(spec_of(**source))]

    assert any(fragment in error for error in errors), errors
    assert all(error.startswith("metadata.source") for error in errors)


def test_a_relation_still_needs_a_path():
    errors = [str(error) for error in validate_spec(spec_of(query_file=None, relation="t", inputs=None))]

    assert errors == ["metadata.source.path: is required"]


def test_the_json_schema_accepts_an_inputs_source_and_rejects_the_same_mistakes():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json_schema())

    validator.validate(spec_of())
    validator.validate(spec_of(query_file=None, sql="select 1"))
    validator.validate(spec_of(inputs=None, path="x.duckdb"))
    for bad in (spec_of(path="x.duckdb"), spec_of(sql="select 1"), spec_of(query_file=None),
                spec_of(query_file=None, relation="t"), spec_of(inputs=[]), spec_of(inputs=["a.json", "a.json"]),
                spec_of(inputs=None), spec_of(query_file=None, relation="t", inputs=None, sql="select 1")):
        assert list(validator.iter_errors(bad)), bad


def test_the_hash_has_the_paths_of_the_inputs_and_the_query_file_as_written_in_the_folder(tmp_path):
    inside = spec_of(inputs=[str(tmp_path / "models" / "a.json")], query_file=str(tmp_path / "q.sql"))
    relative = spec_of(inputs=["models/a.json"], query_file="q.sql")

    assert spec_hash(inside, base_dir=tmp_path) == spec_hash(relative, base_dir=tmp_path)
    assert canonical_spec(relative)["metadata"]["source"]["inputs"] == ["models/a.json"]
    assert spec_hash(spec_of(inputs=["b.json"])) != spec_hash(relative), "what a query reads still counts"


def test_ldp_plan_checks_that_the_input_configs_and_the_query_file_are_there(tmp_path):
    silver(tmp_path)
    good = gold(tmp_path)
    assert plan(good)["valid"] is True and plan(good)["route"] == "DUCKDB -> ICEBERG"
    assert plan(good)["pipeline"] == "DuckDBToIceberg" and plan(good)["source_path"] is None

    broken = write(tmp_path / "broken.json", spec_of(inputs=["silver_orders.json", "gone.json"],
                                                     query_file="models/gone.sql"))
    result = plan(broken)

    assert result["valid"] is False
    assert sorted(re.sub(r"the (config )?file .* does not exist", r"the \1file does not exist", error)
                  for error in result["errors"]) == [
        "metadata.source.inputs[1]: the config file does not exist",
        "metadata.source.query_file: the file does not exist"]


# --------------------------------------------------------------------------- lineage in the run events


def test_run_started_lists_the_tables_that_a_query_over_inputs_read(tmp_path):
    silver(tmp_path)
    sink = MemorySink()

    create_pipeline(Config.from_json(gold(tmp_path))).run(sink=sink)

    started = sink.events[0].payload
    assert [ref["name"] for ref in started["inputs"]] == ["lake.silver_orders"]
    assert started["source"] == {"namespace": "duckdb", "name": "memory#gold_revenue", "format": "DUCKDB"}


def test_a_run_that_does_not_know_its_inputs_has_no_inputs_key(tmp_path):
    sink = MemorySink()

    create_pipeline(Config.from_json(silver(tmp_path, build=False))).run(sink=sink)

    assert "inputs" not in sink.events[0].payload


def test_openlineage_lists_every_input_as_an_input_dataset(tmp_path):
    silver(tmp_path)
    silver(tmp_path, "silver_extra")
    sink = OpenLineageSink(tmp_path / "lineage.jsonl", namespace="test")
    path = gold(tmp_path, sql="SELECT o.restaurant, count(*) AS orders, 0.0 AS revenue "
                              "FROM silver_orders o JOIN silver_extra e USING (order_id) GROUP BY o.restaurant",
                inputs=["silver_orders.json", "silver_extra.json"])

    create_pipeline(Config.from_json(path)).run(sink=sink)

    start, complete = read_openlineage(tmp_path / "lineage.jsonl")
    assert start["eventType"] == "START" and complete["eventType"] == "COMPLETE"
    for event in (start, complete):
        assert [(item["namespace"], item["name"]) for item in event["inputs"]] == [
            ((tmp_path / "warehouse").resolve().as_uri(), "lake.silver_orders"),
            ((tmp_path / "warehouse").resolve().as_uri(), "lake.silver_extra")]
        assert [item["name"] for item in event["outputs"]] == ["gold.gold_revenue"]


def test_openlineage_names_the_one_source_of_a_run_that_lists_no_inputs(tmp_path):
    sink = OpenLineageSink(tmp_path / "lineage.jsonl")

    create_pipeline(Config.from_json(silver(tmp_path, build=False))).run(sink=sink)

    [start, _] = read_openlineage(tmp_path / "lineage.jsonl")
    assert [item["name"] for item in start["inputs"]] == [(tmp_path / "orders.csv").resolve().as_posix()]


def test_a_config_can_say_the_tables_its_source_was_made_from_and_the_run_lists_them(tmp_path):
    path = silver(tmp_path, build=False)
    document = json.loads(path.read_text())
    document["metadata"]["lineage"] = [
        {"name": "orders", "format": "ICEBERG", "catalog": {"identifier": "raw", "warehouse_path": "elsewhere"}}]
    write(path, document)
    sink = MemorySink()

    create_pipeline(Config.from_json(path)).run(sink=sink)

    assert sink.events[0].payload["inputs"] == [{
        "namespace": (tmp_path / "elsewhere").resolve().as_uri(), "name": "raw.orders", "format": "ICEBERG",
        "table_identifier": "raw.orders"}]
    assert spec_hash(document) == spec_hash(json.loads(json.dumps({**document, "metadata": {
        k: v for k, v in document["metadata"].items() if k != "lineage"}}))), "the hash leaves lineage out"


@pytest.mark.parametrize("lineage, fragment", [
    ("raw.orders", "metadata.lineage: must be a list of Iceberg table blocks"),
    ([{"name": "t", "format": "CSV", "path": "t.csv"}], "metadata.lineage[0].format: must be ICEBERG"),
    ([{"name": "t", "format": "ICEBERG"}], "metadata.lineage[0].catalog: is required for an Iceberg table"),
])
def test_validate_spec_names_what_is_wrong_with_a_lineage(tmp_path, lineage, fragment):
    document = json.loads(silver(tmp_path, build=False).read_text())
    document["metadata"]["lineage"] = lineage

    assert any(fragment in str(error) for error in validate_spec(document))


def test_a_lineage_that_cannot_be_worked_out_does_not_stop_the_run(tmp_path):
    class Odd:
        format = "CSV"
        path = None
        name = "odd"

        def get(self):
            return pa.table({"a": [1]})

        def lineage(self):
            raise RuntimeError("no lineage today")

    document = Config.from_dict({"identifier": "odd", "metadata": {
        "source": {"name": "odd", "format": "CSV", "path": "x.csv"},
        "target": {"name": "out", "format": "CSV", "path": "out.csv"}}}, base_dir=tmp_path)
    from local_data_platform.format.csv import CSV
    from local_data_platform.pipeline import Pipeline

    sink = MemorySink()
    result = Pipeline(document, source=Odd(), target=CSV("out", tmp_path / "out.csv"), sink=sink).run()

    assert result.status == "published"
    assert sink.events[0].type == "run.started" and "inputs" not in sink.events[0].payload


def test_a_block_is_named_like_an_open_table_without_opening_anything(tmp_path):
    local = iceberg_block_ref({"name": "orders", "catalog": {"identifier": "raw", "warehouse_path": "wh"}}, tmp_path)
    sql = iceberg_block_ref({"name": "orders", "catalog": {
        "type": "sql", "uri": "sqlite:///c.db", "warehouse": "s3://bucket/wh", "namespace": "raw"}}, tmp_path)
    rest = iceberg_block_ref({"name": "orders", "catalog": {
        "type": "rest", "uri": "http://catalog:8181", "warehouse": "prod", "namespace": "raw"}})
    nothing = iceberg_block_ref({"name": "orders", "catalog": {"type": "glue", "namespace": "raw", "name": "g"}})

    assert local == {"namespace": (tmp_path / "wh").resolve().as_uri(), "name": "raw.orders", "format": "ICEBERG",
                     "table_identifier": "raw.orders"}
    assert (sql["namespace"], sql["name"]) == ("s3://bucket/wh", "raw.orders")
    assert (rest["namespace"], rest["name"]) == ("prod", "raw.orders")
    assert nothing["namespace"] == "iceberg://g"
    assert not (tmp_path / "wh").exists()
    with pytest.raises(ConfigError, match="needs a 'name' and a 'catalog' object"):
        iceberg_block_ref({"name": "orders"})


def test_a_run_is_named_like_the_table_that_it_actually_opens(tmp_path):
    silver(tmp_path)
    opened = iceberg_from_config(Config.from_json(tmp_path / "silver_orders.json"), "target")
    from local_data_platform.events import dataset_ref

    named = iceberg_block_ref(json.loads((tmp_path / "silver_orders.json").read_text())["metadata"]["target"],
                              tmp_path)

    assert named == dataset_ref(opened)


# --------------------------------------------------------------------------- finding and ordering


def test_the_gold_configs_of_a_folder_are_the_ones_whose_docs_say_gold(tmp_path):
    silver(tmp_path)
    gold(tmp_path, "b_gold")
    gold(tmp_path, "a_gold")
    quiet = json.loads(gold(tmp_path, "c_silver").read_text())
    quiet["metadata"]["docs"]["layer"] = "silver"
    write(tmp_path / "c_silver.json", quiet)
    write(tmp_path / "daily.json", {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily",
                                    "metadata": {"schedule": "5 2 * * *"},
                                    "tasks": [{"id": "a", "pipeline": "silver_orders.json"}]})
    write(tmp_path / "notes.json", ["not", "a", "config"])
    write(tmp_path / "other.json", {"anything": True})

    found = find_gold(tmp_path)

    assert [item.path.name for item in found] == ["a_gold.json", "b_gold.json"]
    assert all(item.built_here and item.inputs == (tmp_path / "silver_orders.json",) for item in found)
    assert found[0].table == "gold.a_gold"


def test_a_config_that_is_not_valid_is_an_error_that_names_its_file_and_does_not_vanish_from_the_build(tmp_path):
    document = json.loads(gold(tmp_path).read_text())
    del document["metadata"]["docs"]["grain"]
    write(tmp_path / "gold_revenue.json", document)

    with pytest.raises(ConfigError, match=r"gold_revenue\.json: metadata\.docs\.grain: is required for a gold table"):
        find_gold(tmp_path)


def test_a_gold_config_has_to_write_an_iceberg_table(tmp_path):
    document = json.loads(gold(tmp_path).read_text())
    document["metadata"]["target"] = {"name": "out", "format": "CSV", "path": "out.csv"}
    write(tmp_path / "gold_revenue.json", document)

    with pytest.raises(ConfigError, match=r"gold_revenue\.json is a gold table, so its target has to be an Iceberg"):
        find_gold(tmp_path)


def test_a_folder_that_is_not_a_folder_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="is not a folder of dataset configs"):
        find_gold(tmp_path / "nope")


def chain(root: Path) -> None:
    """``c`` reads ``b``, ``b`` reads ``a``, and ``a`` reads silver. ``z`` and ``d`` read silver only."""
    silver(root)
    gold(root, "a", "SELECT restaurant, orders, revenue FROM (SELECT restaurant, count(*) AS orders, sum(amount) AS "
                    "revenue FROM silver_orders GROUP BY restaurant)")
    gold(root, "b", "SELECT restaurant, orders, revenue FROM a", inputs=["a.json"])
    gold(root, "c", "SELECT restaurant, orders, revenue FROM b", inputs=["b.json"])
    gold(root, "d")
    gold(root, "z")


def test_a_gold_table_is_built_after_the_gold_tables_that_it_reads_and_otherwise_by_file_name(tmp_path):
    chain(tmp_path)

    ordered = order_gold(find_gold(tmp_path))

    assert [item.path.stem for item in ordered] == ["a", "b", "c", "d", "z"]
    gold(tmp_path, "a2", inputs=["c.json"], sql="SELECT restaurant, orders, revenue FROM c")
    assert [item.path.stem for item in order_gold(find_gold(tmp_path))] == ["a", "b", "c", "a2", "d", "z"]


def test_an_input_that_is_not_in_the_folder_adds_no_order(tmp_path):
    (tmp_path / "other").mkdir()
    silver(tmp_path / "other")
    gold(tmp_path, "g", inputs=["other/silver_orders.json"])

    assert [item.path.name for item in order_gold(find_gold(tmp_path))] == ["g.json"]


def test_a_cycle_is_refused_and_the_message_names_it(tmp_path):
    silver(tmp_path)
    gold(tmp_path, "a", inputs=["silver_orders.json", "c.json"])
    gold(tmp_path, "b", inputs=["a.json"])
    gold(tmp_path, "c", inputs=["b.json"])
    gold(tmp_path, "d")

    with pytest.raises(ConfigError, match=r"read each other in a cycle, a\.json reads c\.json, which reads b\.json, "
                                          r"which reads a\.json"):
        order_gold(find_gold(tmp_path))


def test_a_gold_table_that_reads_its_own_table_is_refused(tmp_path):
    silver(tmp_path)
    gold(tmp_path, "g", inputs=["silver_orders.json", "g.json"])

    with pytest.raises(ConfigError, match=r"g\.json reads its own table"):
        order_gold(find_gold(tmp_path))


def test_a_build_refuses_a_cycle_before_it_builds_anything(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path, "a", inputs=["b.json"])
    gold(tmp_path, "b", inputs=["a.json"])
    gold(tmp_path, "c")

    assert main(["gold", "build", str(tmp_path)]) == 1

    assert "read each other in a cycle" in capsys.readouterr().err
    assert not table(tmp_path, "c").exists()


# --------------------------------------------------------------------------- the build


@pytest.mark.usefixtures("reading")
def test_a_build_runs_the_gold_configs_in_order_and_prints_a_line_for_each_table(tmp_path, capsys):
    chain(tmp_path)

    assert main(["gold", "build", str(tmp_path)]) == 0

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 6
    for line, name in zip(out, ["a", "b", "c", "d", "z"]):
        assert re.fullmatch(rf"{name}\s+built\s+2 rows\s+snapshot \d+\s+2 of 2 checks passed|"
                            rf"{name}\s+built\s+2 rows\s+snapshot \d+\s+no checks", line), line
    assert out[-1].startswith(f"gold build {tmp_path}: 5 built, 0 checked, 0 blocked, 0 failed, 0 not run in ")
    assert [row["restaurant"] for row in rows(tmp_path, "c")] == ["north", "south"]


@pytest.mark.usefixtures("reading")
def test_a_second_build_gives_the_same_tables_and_a_gold_table_reads_what_the_one_before_it_just_wrote(tmp_path):
    chain(tmp_path)
    build_gold(tmp_path)
    silver(tmp_path, csv=ORDERS + "4,south,1.0\n")

    result = build_gold(tmp_path)

    assert result.exit_code == 0 and result.counts() == {"built": 5, "checked": 0, "blocked": 0, "failed": 0,
                                                         "not run": 0}
    assert {row["restaurant"]: row["orders"] for row in rows(tmp_path, "c")} == {"north": 2, "south": 2}


def test_a_build_returns_each_pipeline_result(tmp_path):
    silver(tmp_path)
    gold(tmp_path)

    [built] = build_gold(tmp_path).tables

    assert built.status == BUILT and built.rows == 2 and built.result.published_snapshot_id == built.snapshot_id
    assert built.to_dict()["table"] == "gold.gold_revenue" and built.gate == "no checks"


def test_the_runs_of_a_build_are_recorded_in_the_ldp_namespace_through_the_iceberg_sink(tmp_path, capsys):
    silver(tmp_path)
    path = gold(tmp_path, quality={"checks": [{"check": "row_count", "min": 1}]})

    assert main(["gold", "build", str(tmp_path)]) == 0
    capsys.readouterr()

    events = read_iceberg_events({"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")},
                                 pipeline="gold_revenue")
    assert [event.type for event in events] == ["run.started", "run.extracted", "quality.evaluated", "run.published",
                                                "run.finished"]
    assert main(["runs", str(path)]) == 0
    assert "published" in capsys.readouterr().out


def test_freshness_and_gate_status_of_a_built_gold_table_show_in_describe_table(tmp_path):
    from local_data_platform.mcp_server.tools import LakeTools

    silver(tmp_path)
    gold(tmp_path, quality={"checks": [{"check": "row_count", "min": 1}]})
    build_gold(tmp_path)

    with LakeTools.from_sources(configs=[tmp_path]) as tools:
        described = tools.describe_table("gold.gold_revenue")

    assert described["latest_run"]["status"] == "published"
    assert described["freshness"]["last_run_status"] == "published"
    assert described["quality"]["passed"] is True
    assert described["properties"]["ldp.layer"] == "gold"


def test_a_config_that_names_its_own_sinks_keeps_them_and_gets_no_iceberg_sink(tmp_path):
    silver(tmp_path)
    log = tmp_path / "events.jsonl"
    gold(tmp_path, observability={"sinks": [{"type": "jsonl", "path": str(log)}]})

    build_gold(tmp_path)

    assert [json.loads(line)["type"] for line in log.read_text().splitlines()][0] == "run.started"
    assert read_iceberg_events({"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")}) == []


def test_a_gate_that_blocks_a_table_stops_the_build_and_the_tables_after_it_are_not_run(tmp_path, capsys):
    chain(tmp_path)
    gold(tmp_path, "b", "SELECT restaurant, orders, revenue FROM a", inputs=["a.json"],
         quality={"checks": [{"check": "row_count", "min": 100}]})

    assert main(["gold", "build", str(tmp_path)]) == 3

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert [line.split()[:2] for line in lines[:5]] == [["a", "built"], ["b", "blocked"], ["c", "not"], ["d", "not"],
                                                        ["z", "not"]]
    assert "1 of 1 checks failed (row_count)" in lines[1]
    assert lines[-1].endswith("1 built, 0 checked, 1 blocked, 0 failed, 3 not run in " + lines[-1].split(" in ")[1])
    assert "ldp: b blocked" in captured.err and "row_count" in captured.err
    assert table(tmp_path, "a").exists() and not table(tmp_path, "b").exists() and not table(tmp_path, "c").exists()


def test_a_table_that_fails_for_another_reason_exits_with_1_and_stops_the_build(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path, "a_first")
    gold(tmp_path, "b_broken", sql="SELECT restaurant FROM silver_orders WHERE nope = 1")
    gold(tmp_path, "c_last")

    assert main(["gold", "build", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert [line.split()[:2] for line in captured.out.splitlines()[:3]] == [["a_first", "built"],
                                                                            ["b_broken", "failed"], ["c_last", "not"]]
    assert "ldp: b_broken failed" in captured.err and "ConfigError" in captured.err


def test_an_input_that_was_never_built_fails_the_table_that_reads_it(tmp_path, capsys):
    silver(tmp_path, build=False)
    gold(tmp_path)

    assert main(["gold", "build", str(tmp_path)]) == 1

    assert "Run that pipeline first" in capsys.readouterr().err


def test_a_broken_contract_is_a_gate_so_the_build_exits_with_3(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path, columns={"restaurant": "The restaurant.", "orders": "How many orders."})

    assert main(["gold", "build", str(tmp_path)]) == 3

    captured = capsys.readouterr()
    assert re.match(r"gold_revenue\s+blocked", captured.out)
    assert "1 column written without a description" in captured.err and "revenue" in captured.err
    assert not table(tmp_path, "gold_revenue").exists()


def test_a_folder_with_no_gold_config_is_an_error(tmp_path, capsys):
    silver(tmp_path)

    assert main(["gold", "build", str(tmp_path)]) == 1

    assert "has no gold config" in capsys.readouterr().err


def test_the_result_can_be_printed_as_json(tmp_path, capsys):
    chain(tmp_path)

    assert main(["gold", "build", str(tmp_path), "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == 0 and result["counts"]["built"] == 5
    assert [item["file"] for item in result["tables"]] == ["a.json", "b.json", "c.json", "d.json", "z.json"]
    assert all(item["status"] == "built" and item["rows"] == 2 for item in result["tables"])


# --------------------------------------------------------------------------- a table that is built elsewhere


def external(root: Path, name: str = "elsewhere", *, quality=None, rows_csv: str = ORDERS) -> Path:
    """A gold config with no ``inputs``, over a table that a plain pipeline wrote, as dbt or Spark would."""
    write(root / "ext.csv", rows_csv)
    write(root / f"{name}_load.json", {"identifier": f"{name}_load", "metadata": {
        "source": {"name": "ext", "format": "CSV", "path": "ext.csv"},
        "target": {"name": name, "format": "ICEBERG", "write_mode": "overwrite", "catalog": GOLD_CATALOG}}})
    run_config(root / f"{name}_load.json")
    metadata = {"source": {"name": name, "format": "ICEBERG", "catalog": GOLD_CATALOG},
                "target": {"name": name, "format": "ICEBERG", "catalog": GOLD_CATALOG},
                "docs": {"layer": "gold", "description": "Built elsewhere.", "grain": "one row per order",
                         "columns": {"order_id": "The order.", "restaurant": "Where.", "amount": "How much."}}}
    if quality is not None:
        metadata["quality"] = quality
    return write(root / f"{name}.json", {"identifier": name, "who": "ops", "metadata": metadata})


def test_a_gold_config_with_no_inputs_is_checked_and_not_built(tmp_path, capsys):
    path = external(tmp_path, quality={"checks": [{"check": "row_count", "min": 3},
                                                  {"check": "not_null", "columns": ["order_id"]}]})
    snapshots = len(table(tmp_path, "elsewhere").snapshots())

    assert main(["gold", "build", str(tmp_path)]) == 0

    out = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"elsewhere\s+checked\s+3 rows\s+snapshot \d+\s+2 of 2 checks passed", out[0]), out[0]
    assert "0 built, 1 checked" in out[1]
    assert len(table(tmp_path, "elsewhere").snapshots()) == snapshots, "nothing was written"
    assert path.exists()


def test_a_check_that_fails_on_the_current_snapshot_blocks_the_build_and_writes_nothing(tmp_path, capsys):
    external(tmp_path, quality={"checks": [{"check": "row_count", "min": 10}]})
    snapshots = len(table(tmp_path, "elsewhere").snapshots())

    assert main(["gold", "build", str(tmp_path)]) == 3

    captured = capsys.readouterr()
    assert "elsewhere  checked" not in captured.out and "blocked" in captured.out
    assert "blocked by failed quality checks on the current snapshot, and nothing was written" in captured.err
    assert "row_count" in captured.err
    assert len(table(tmp_path, "elsewhere").snapshots()) == snapshots


def test_a_failing_check_only_warns_when_the_config_says_warn(tmp_path):
    external(tmp_path, quality={"on_failure": "warn", "checks": [{"check": "row_count", "min": 10}]})

    [outcome] = build_gold(tmp_path).tables

    assert outcome.status == CHECKED and outcome.gate == "1 of 1 checks failed (row_count)"


def test_a_table_with_no_checks_is_only_looked_for(tmp_path):
    external(tmp_path)

    [outcome] = build_gold(tmp_path).tables

    assert outcome.status == CHECKED and outcome.gate == "no checks" and outcome.rows == 3
    assert outcome.snapshot_id == table(tmp_path, "elsewhere").table().current_snapshot().snapshot_id


def test_a_gold_table_that_is_built_elsewhere_and_is_not_there_fails_the_build(tmp_path, capsys):
    path = external(tmp_path)
    document = json.loads(path.read_text())
    document["metadata"]["target"]["name"] = document["metadata"]["source"]["name"] = "missing"
    write(tmp_path / "missing.json", document)
    (tmp_path / "elsewhere.json").unlink()

    assert main(["gold", "build", str(tmp_path)]) == 1

    assert "gold.missing is built elsewhere, and it does not exist yet" in capsys.readouterr().err


def test_a_table_that_is_built_elsewhere_can_be_checked_on_its_own(tmp_path):
    path = external(tmp_path, quality={"checks": [{"check": "row_count", "min": 1}]})
    [gold_config] = find_gold(tmp_path)

    assert gold_config.path == path and not gold_config.built_here
    assert check_built_elsewhere(gold_config).status == CHECKED


def test_built_and_checked_tables_are_in_one_build_and_the_checked_one_is_not_rebuilt(tmp_path):
    silver(tmp_path)
    gold(tmp_path, "a_mine")
    external(tmp_path, "b_theirs")

    result = build_gold(tmp_path)

    assert [(item.gold.path.stem, item.status) for item in result.tables] == [("a_mine", BUILT),
                                                                              ("b_theirs", CHECKED)]
    assert result.exit_code == 0 and result.stopped_by is None


def test_the_statuses_are_the_words_the_build_prints():
    assert (BUILT, CHECKED, BLOCKED, FAILED, NOT_RUN) == ("built", "checked", "blocked", "failed", "not run")
