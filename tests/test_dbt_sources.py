"""Registering LDP's Iceberg tables as dbt sources in a DuckDB build file.

Copy mode and everything around it runs in every environment. View mode needs DuckDB's iceberg
extension (see ``requires_iceberg_extension``), which CI does not install, so those tests run on a
developer machine. The tests marked ``dbt`` run a real dbt over the registered sources.
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType

from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from local_data_platform.dbt_layer import RegisteredSource, register_sources, sources_yaml
from local_data_platform.dbt_layer.sources import GENERATED_HEADER
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.exceptions import ConfigError, EngineNotFound
from tests.support import (
    add_table,
    build_lake,
    dataset_config,
    relation_rows,
    relations,
    requires_iceberg_extension,
    run_dbt,
    run_results,
    write_dbt_project,
)


@pytest.fixture
def lake(tmp_path) -> Path:
    """The config folder of a lake with ``raw.orders``, ``raw.customers`` and ``ref.cities``."""
    return build_lake(tmp_path)


@pytest.fixture
def database(tmp_path) -> Path:
    return tmp_path / "project" / "target" / "ldp_build.duckdb"


@pytest.fixture
def no_iceberg_extension(monkeypatch):
    """Make DuckDB's iceberg extension look uninstalled, as it is in CI."""
    monkeypatch.setattr(DuckDBEngine, "iceberg_extension", property(lambda self: False))


# --------------------------------------------------------------------------- copy mode


def test_copy_mode_makes_a_table_per_iceberg_table_in_a_schema_per_namespace(lake, database):
    result = register_sources(database, configs=[lake], mode="copy")

    assert result.mode == "copy"
    assert sorted(source.identifier for source in result.sources) == ["raw.customers", "raw.orders", "ref.cities"]
    assert relations(database) == {"raw.orders": "BASE TABLE", "raw.customers": "BASE TABLE",
                                   "ref.cities": "BASE TABLE", "_ldp.sources": "BASE TABLE"}
    assert [row[0] for row in relation_rows(database, "raw.orders")] == [1, 2, 3]
    assert relation_rows(database, "ref.cities")[0] == (1, "cities-1", 1.5)
    orders = next(source for source in result.sources if source.identifier == "raw.orders")
    assert orders.kind == "table" and orders.rows == 3
    assert orders.columns == (("id", "BIGINT"), ("label", "VARCHAR"), ("amount", "DOUBLE"))


def test_the_state_table_records_what_was_registered(lake, database):
    register_sources(database, configs=[lake], mode="copy")

    with duckdb.connect(str(database), read_only=True) as connection:
        state = connection.execute("SELECT schema_name, table_name, kind, snapshot_id IS NOT NULL "
                                   "FROM _ldp.sources ORDER BY 1, 2").fetchall()
    assert state == [("raw", "customers", "table", True), ("raw", "orders", "table", True),
                     ("ref", "cities", "table", True)]


def test_an_empty_table_is_registered_as_an_empty_relation(tmp_path, database):
    # A table that was created and never written has no snapshot, so there is nothing to scan.
    with LocalIcebergCatalog("raw", path=str(tmp_path / "warehouse")) as catalog:
        catalog.create_namespace_if_not_exists("raw")
        schema = Schema(NestedField(1, "id", LongType(), required=False),
                        NestedField(2, "label", StringType(), required=False))
        catalog.create_table("raw.empty", schema=schema)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "empty.json").write_text(json.dumps(dataset_config("raw", "empty")))

    result = register_sources(database, configs=[tmp_path / "config"], mode="auto")

    [source] = result.sources
    assert source.kind == "table" and source.snapshot_id is None and source.rows == 0
    assert source.columns == (("id", "BIGINT"), ("label", "VARCHAR"))


def test_the_relation_is_pinned_to_the_snapshot_that_was_current_at_registration(tmp_path, lake, database):
    result = register_sources(database, configs=[lake], mode="copy")
    pinned = next(source.snapshot_id for source in result.sources if source.identifier == "raw.orders")

    later = add_table(tmp_path, "raw", "orders", 2, start=10)  # a later commit to the same table

    assert later.table().current_snapshot().snapshot_id != pinned
    assert [row[0] for row in relation_rows(database, "raw.orders")] == [1, 2, 3]
    again = register_sources(database, configs=[lake], mode="copy")  # registering again moves the pin
    assert [row[0] for row in relation_rows(database, "raw.orders")] == [1, 2, 3, 10, 11]
    assert next(s.snapshot_id for s in again.sources if s.identifier == "raw.orders") != pinned


def test_the_build_file_is_closed_when_the_function_returns(lake, database):
    register_sources(database, configs=[lake], mode="copy")

    # DuckDB lets one process hold a file for writing, so another process can open it only if we let go.
    code = "import duckdb, sys; duckdb.connect(sys.argv[1]).execute('select 1')"
    opened = subprocess.run([sys.executable, "-c", code, str(database)], capture_output=True, text=True, timeout=60)
    assert opened.returncode == 0, opened.stderr


# --------------------------------------------------------------------------- what is left out and what is removed


def test_a_relation_of_a_table_that_no_longer_exists_is_dropped(lake, database):
    register_sources(database, configs=[lake], mode="copy")
    with duckdb.connect(str(database)) as connection:  # dbt builds a model in the same file
        connection.execute("CREATE SCHEMA marts")
        connection.execute("CREATE TABLE marts.revenue AS SELECT 1 AS x")
    (lake / "ref.cities.json").unlink()

    result = register_sources(database, configs=[lake], mode="copy")

    assert result.dropped == ["ref.cities"]
    found = relations(database)
    assert "ref.cities" not in found and "marts.revenue" in found and "raw.orders" in found
    with duckdb.connect(str(database), read_only=True) as connection:
        schemas = {row[0] for row in connection.execute(
            "SELECT schema_name FROM information_schema.schemata").fetchall()}
    assert "ref" not in schemas  # the schema was left empty, so it went too
    assert {"raw", "marts", "_ldp"} <= schemas


def test_a_schema_that_still_has_relations_is_kept(lake, database):
    register_sources(database, configs=[lake], mode="copy")
    (lake / "raw.customers.json").unlink()

    result = register_sources(database, configs=[lake], mode="copy")

    assert result.dropped == ["raw.customers"]
    assert relations(database)["raw.orders"] == "BASE TABLE"


def test_a_table_that_a_config_publishes_from_the_build_file_is_not_a_source(tmp_path, lake, database):
    add_table(tmp_path, "gold", "revenue", 2)  # the published table, with its own dataset config
    published = dataset_config("gold", "revenue")
    published["metadata"]["source"] = {"name": "revenue", "format": "DUCKDB",
                                       "path": "../project/target/ldp_build.duckdb", "relation": "main.revenue"}
    (lake / "gold.revenue.json").write_text(json.dumps(published))

    result = register_sources(database, configs=[lake], mode="copy")

    assert "gold.revenue" not in [source.identifier for source in result.sources]
    assert [skip["table"] for skip in result.skipped] == ["gold.revenue"]
    assert "gold.revenue" not in relations(database)


def test_a_config_that_publishes_from_another_file_is_still_a_source(tmp_path, lake, database):
    add_table(tmp_path, "gold", "revenue", 2)
    other = dataset_config("gold", "revenue")
    other["metadata"]["source"] = {"name": "revenue", "format": "DUCKDB", "path": "somewhere/else.duckdb",
                                   "relation": "main.revenue"}
    (lake / "gold.revenue.json").write_text(json.dumps(other))

    result = register_sources(database, configs=[lake], mode="copy")

    assert "gold.revenue" in [source.identifier for source in result.sources] and result.skipped == []


def test_the_system_namespace_is_not_a_source(tmp_path, lake, database):
    add_table(tmp_path, "_ldp", "runs", 2)

    result = register_sources(database, configs=[lake], mode="copy")

    assert not any(source.schema == "_ldp" for source in result.sources)
    assert "_ldp.runs" not in relations(database)  # only the state table is in the _ldp schema


def test_a_table_that_is_not_there_yet_is_reported_and_not_registered(tmp_path, lake, database):
    unwritten = dataset_config("raw", "later")
    (lake / "raw.later.json").write_text(json.dumps(unwritten))

    result = register_sources(database, configs=[lake], mode="copy")

    assert "raw.later" not in [source.identifier for source in result.sources]
    assert [item["table"] for item in result.unavailable] == ["raw.later"]


def test_a_name_that_dbt_made_is_never_overwritten(lake, database):
    database.parent.mkdir(parents=True)
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE SCHEMA raw")
        connection.execute("CREATE TABLE raw.orders AS SELECT 99 AS made_by_dbt")

    with pytest.raises(ConfigError, match=r"raw\.orders.*dbt made"):
        register_sources(database, configs=[lake], mode="copy")

    assert relation_rows(database, "raw.orders") == [(99,)]  # the transaction rolled back, nothing was touched
    assert "raw.customers" not in relations(database)


# --------------------------------------------------------------------------- modes


def test_auto_mode_falls_back_to_copies_and_says_how_to_install_the_extension(
        lake, database, no_iceberg_extension, caplog):
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        result = register_sources(database, configs=[lake], mode="auto")

    assert result.mode == "copy" and all(source.kind == "table" for source in result.sources)
    assert "INSTALL iceberg" in caplog.text and "copies" in caplog.text


def test_view_mode_without_the_extension_says_how_to_install_it_and_registers_nothing(
        lake, database, no_iceberg_extension):
    with pytest.raises(EngineNotFound, match='INSTALL iceberg'):
        register_sources(database, configs=[lake], mode="view")

    assert "raw.orders" not in relations(database)


@pytest.mark.parametrize("mode", ["views", "", None])
def test_an_unknown_mode_is_a_config_error(lake, database, mode):
    with pytest.raises(ConfigError, match="mode must be one of"):
        register_sources(database, configs=[lake], mode=mode)


def test_a_config_path_that_does_not_exist_is_a_config_error(tmp_path, database):
    with pytest.raises(ConfigError, match="config path not found"):
        register_sources(database, configs=[tmp_path / "nope"], mode="copy")


@requires_iceberg_extension
def test_view_mode_registers_persistent_views_pinned_to_a_snapshot(tmp_path, lake, database):
    result = register_sources(database, configs=[lake], mode="view")

    assert result.mode == "view" and all(source.kind == "view" and source.rows is None for source in result.sources)
    assert relations(database)["raw.orders"] == "VIEW"
    add_table(tmp_path, "raw", "orders", 2, start=10)  # a later commit is not seen by the pinned view
    # A new process, which has not loaded the extension, reads the view: DuckDB loads it on demand.
    code = ("import duckdb, sys; connection = duckdb.connect(sys.argv[1], read_only=True); "
            "print(connection.execute('select count(*) from raw.orders').fetchone()[0])")
    counted = subprocess.run([sys.executable, "-c", code, str(database)], capture_output=True, text=True, timeout=120)
    assert counted.stdout.strip() == "3", counted.stderr


@requires_iceberg_extension
def test_a_view_can_become_a_copy_and_back_on_later_calls(lake, database):
    register_sources(database, configs=[lake], mode="view")
    register_sources(database, configs=[lake], mode="copy")
    assert relations(database)["raw.orders"] == "BASE TABLE"
    register_sources(database, configs=[lake], mode="view")
    assert relations(database)["raw.orders"] == "VIEW"


# --------------------------------------------------------------------------- the dbt sources file


def test_the_sources_file_has_one_source_per_namespace(lake, database, tmp_path):
    target = tmp_path / "project" / "models" / "_ldp_sources.yml"

    result = register_sources(database, configs=[lake], mode="copy", sources_file=target)

    text = target.read_text()
    assert result.sources_file == target.resolve()
    assert text.startswith(GENERATED_HEADER)
    lines = text.splitlines()
    assert sum(line.startswith("  - name:") for line in lines) == 2  # raw and ref
    assert sum(line.startswith("      - name:") for line in lines) == 3  # their three tables
    assert '    schema: "raw"' in text and '      - name: "orders"' in text
    assert '          - name: "amount"\n            data_type: "DOUBLE"' in text
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(text)
    raw = next(source for source in document["sources"] if source["name"] == "raw")
    assert sorted(table["name"] for table in raw["tables"]) == ["customers", "orders"]


def test_the_sources_file_is_rewritten_only_when_its_content_changes(lake, database, tmp_path):
    target = tmp_path / "project" / "models" / "_ldp_sources.yml"
    register_sources(database, configs=[lake], mode="copy", sources_file=target)
    first = target.stat().st_mtime_ns
    time.sleep(0.05)

    register_sources(database, configs=[lake], mode="copy", sources_file=target)
    assert target.stat().st_mtime_ns == first  # same tables and columns, so dbt's partial parse still holds

    (lake / "ref.cities.json").unlink()
    register_sources(database, configs=[lake], mode="copy", sources_file=target)
    assert target.stat().st_mtime_ns != first and "cities" not in target.read_text()


def test_no_sources_make_a_valid_empty_file():
    text = sources_yaml([])

    assert text.startswith(GENERATED_HEADER) and text.endswith("sources: []\n")


def test_awkward_names_stay_valid_yaml():
    yaml = pytest.importorskip("yaml")
    source = RegisteredSource('we"ird schema', "tab: le #1", "table", 7,
                              (("col with space", "VARCHAR"), ('quote"d', "STRUCT(a INTEGER, b VARCHAR)"),
                               ("naïve", "DECIMAL(10,2)")))

    document = yaml.safe_load(sources_yaml([source]))

    [declared] = document["sources"]
    assert declared["name"] == declared["schema"] == 'we"ird schema'
    [table] = declared["tables"]
    assert table["name"] == "tab: le #1"
    assert [(c["name"], c["data_type"]) for c in table["columns"]] == [
        ("col with space", "VARCHAR"), ('quote"d', "STRUCT(a INTEGER, b VARCHAR)"), ("naïve", "DECIMAL(10,2)")]


# --------------------------------------------------------------------------- dbt reads the sources


@pytest.mark.dbt
def test_dbt_builds_models_on_the_registered_sources_with_no_network(tmp_path, lake, network_trap):
    env, connections = network_trap
    project = write_dbt_project(tmp_path / "project", {
        "stg_orders": "select id as order_id, amount from {{ source('raw', 'orders') }}",
        "orders_with_city": "select o.order_id, c.label as city from {{ ref('stg_orders') }} o "
                            "join {{ source('ref', 'cities') }} c on c.id = o.order_id",
    }, schema_yml="version: 2\nmodels:\n  - name: stg_orders\n    columns:\n      - name: order_id\n"
                  "        data_tests: [unique, not_null]\n")
    register_sources(project / "target" / "ldp_build.duckdb", configs=[lake], mode="copy",
                     sources_file=project / "models" / "_ldp_sources.yml")

    result = run_dbt(env, project, "build")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stdout, result.stdout
    assert run_results(project) == {"stg_orders": "success", "orders_with_city": "success",
                                    "unique_stg_orders_order_id": "pass", "not_null_stg_orders_order_id": "pass"}
    assert relation_rows(project / "target" / "ldp_build.duckdb", "main.orders_with_city") == [
        (1, "cities-1"), (2, "cities-2")]
    assert connections == [], f"dbt opened a network connection: {connections}"


@pytest.mark.dbt
@requires_iceberg_extension
def test_dbt_reads_persistent_views_over_iceberg_scan(tmp_path, lake, network_trap):
    # The views were made by this process. dbt is another process, so DuckDB loads the extension there on demand.
    env, connections = network_trap
    project = write_dbt_project(tmp_path / "project", {"stg_orders": "select * from {{ source('raw', 'orders') }}"})
    register_sources(project / "target" / "ldp_build.duckdb", configs=[lake], mode="view",
                     sources_file=project / "models" / "_ldp_sources.yml")

    result = run_dbt(env, project, "build")

    assert result.returncode == 0, result.stdout + result.stderr
    assert [row[0] for row in relation_rows(project / "target" / "ldp_build.duckdb", "main.stg_orders")] == [1, 2, 3]
    assert connections == []
