"""The DUCKDB source format: a table or a query in a DuckDB file, published to Iceberg.

This is the one way to publish SQL-built data to Iceberg. Everything runs offline on files under
``tmp_path``.
"""

import json
import re
import sys
from pathlib import Path

import duckdb
import pytest

from local_data_platform import Config, SupportedFormat
from local_data_platform.cli import main
from local_data_platform.events import MemorySink, dataset_ref
from local_data_platform.exceptions import ConfigError, DataQualityError, EngineNotFound, TableNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.builders import duckdb_from_config
from local_data_platform.pipeline.registry import create_pipeline, get_pipeline_class
from local_data_platform.spec import FORMATS, json_schema, plan, validate_spec
from local_data_platform.store.source.duckdb import DuckDBTable, relation_sql

RELATION = "gold.churn_by_month"


@pytest.fixture
def build_file(tmp_path) -> Path:
    """A DuckDB file shaped like a dbt build: a ``gold`` table with three months of churn."""
    path = tmp_path / "target" / "ldp_build.duckdb"
    path.parent.mkdir()
    with duckdb.connect(str(path)) as connection:
        connection.execute("create schema gold")
        connection.execute("""
            create table gold.churn_by_month
                (month integer, customers_at_start integer, churned integer, churn_rate double)
        """)
        connection.execute("insert into gold.churn_by_month values (1, 4, 1, 0.25), (2, 5, 0, 0.0), (3, 4, 2, 0.5)")
        connection.execute("create view gold.recent as select * from gold.churn_by_month where month >= 2")
    return path


def spec(path: str = "target/ldp_build.duckdb", target_extra: dict | None = None, **source) -> dict:
    """A dataset spec whose source is the DuckDB file and whose target is an Iceberg table."""
    source = {"relation": RELATION, **source} if not ({"relation", "sql"} & set(source)) else source
    return {
        "identifier": "churn",
        "metadata": {
            "source": {"name": "churn_by_month", "format": "DUCKDB", "path": path, **source},
            "target": {"name": "churn_by_month", "format": "ICEBERG",
                       "catalog": {"identifier": "gold", "warehouse_path": "warehouse"}, **(target_extra or {})},
        },
    }


def config(tmp_path: Path, **kwargs) -> Config:
    return Config.from_dict(spec(**kwargs), base_dir=tmp_path)


def table(tmp_path: Path) -> Iceberg:
    return Iceberg("churn_by_month", {"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")})


# --------------------------------------------------------------------------- running it


def test_ldp_run_publishes_a_relation_to_iceberg(tmp_path, build_file, capsys):
    path = tmp_path / "churn.json"
    path.write_text(json.dumps(spec()))

    assert main(["run", str(path)]) == 0

    assert "churn: read 3 rows, wrote 3 rows" in capsys.readouterr().out
    assert sorted(table(tmp_path).get().column("month").to_pylist()) == [1, 2, 3]


def test_ldp_pipelines_lists_the_route(capsys):
    assert main(["pipelines"]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if "DuckDBToIceberg" in line)
    assert line.split()[:2] == ["DUCKDB", "ICEBERG"]
    assert get_pipeline_class("DUCKDB", "ICEBERG").__name__ == "DuckDBToIceberg"


def test_a_view_is_a_relation_too(tmp_path, build_file):
    result = create_pipeline(config(tmp_path, relation="gold.recent")).run()

    assert result.rows_read == 2


def test_a_sql_source_publishes_the_result_of_a_query(tmp_path, build_file):
    sql = "select month, churned from gold.churn_by_month where churned > 0 order by month"
    result = create_pipeline(config(tmp_path, sql=sql)).run()

    assert result.rows_read == 2
    rows = table(tmp_path).get()
    assert rows.column_names == ["month", "churned"] and sorted(rows.column("month").to_pylist()) == [1, 3]


def test_overwrite_makes_a_rerun_idempotent_and_upsert_updates_rows(tmp_path, build_file):
    overwrite = config(tmp_path, target_extra={"write_mode": "overwrite"})
    create_pipeline(overwrite).run()
    create_pipeline(overwrite).run()
    assert table(tmp_path).row_count() == 3

    with duckdb.connect(str(build_file)) as connection:
        connection.execute("update gold.churn_by_month set churn_rate = 0.75 where month = 3")
        connection.execute("insert into gold.churn_by_month values (4, 6, 1, 0.1667)")
    upsert = config(tmp_path, target_extra={"write_mode": "upsert", "join_cols": ["month"]})
    result = create_pipeline(upsert).run()

    rows = table(tmp_path).get()
    assert dict(zip(rows.column("month").to_pylist(), rows.column("churn_rate").to_pylist())) == {
        1: 0.25, 2: 0.0, 3: 0.75, 4: 0.1667}
    assert result.rows_written == 2  # the changed row and the new one; unchanged rows are not rewritten


def test_a_failing_quality_check_blocks_the_write_and_emits_blocked_quality(tmp_path, build_file):
    blocked = spec()
    blocked["metadata"]["quality"] = [{"check": "range", "column": "churn_rate", "min": 0.0, "max": 0.4}]
    sink = MemorySink()

    with pytest.raises(DataQualityError):
        create_pipeline(Config.from_dict(blocked, base_dir=tmp_path), sink=sink).run()

    assert "run.blocked_quality" in [event.type for event in sink.events]
    assert not table(tmp_path).exists()  # nothing was written


def test_run_events_name_the_source_by_file_and_relation(tmp_path, build_file):
    sink = MemorySink()
    create_pipeline(config(tmp_path), sink=sink).run()

    started = next(event for event in sink.events if event.type == "run.started")
    assert started.payload["source"] == {"namespace": "duckdb", "format": "DUCKDB",
                                         "name": f"{build_file.resolve().as_posix()}#{RELATION}"}


def test_a_sql_source_is_named_by_file_and_source_name(tmp_path, build_file):
    source = duckdb_from_config(config(tmp_path, sql="select 1 as x"), "source")

    assert dataset_ref(source) == {"namespace": "duckdb", "format": "DUCKDB",
                                   "name": f"{build_file.resolve().as_posix()}#churn_by_month"}


# --------------------------------------------------------------------------- the file


def test_the_file_is_open_read_only_and_closed_before_the_iceberg_write(tmp_path, build_file, monkeypatch):
    seen = {}
    real_put = Iceberg.put

    def put_while_checking_the_file(self, *args, **kwargs):
        # A read-write connection fails while any other connection to the file is open.
        with duckdb.connect(str(build_file)) as connection:
            seen["writable"] = connection.execute("select count(*) from gold.churn_by_month").fetchone()[0]
        return real_put(self, *args, **kwargs)

    monkeypatch.setattr(Iceberg, "put", put_while_checking_the_file)

    create_pipeline(config(tmp_path)).run()

    assert seen == {"writable": 3}


def test_the_source_never_writes_the_file(tmp_path, build_file):
    before = build_file.read_bytes()

    DuckDBTable("churn", build_file, relation=RELATION).get()

    assert build_file.read_bytes() == before


@pytest.mark.parametrize("sql, message", [
    ("select 1; select 2", "exactly one statement"),
    ("create table gold.x as select 1", "starts with 'CREATE'"),
    ("insert into gold.churn_by_month values (9, 1, 1, 1.0)", "starts with 'INSERT'"),
    ("copy (select 1) to 'out.csv'", "starts with 'COPY'"),
    ("   ", "empty"),
])
def test_a_sql_source_must_be_one_read_only_query(build_file, sql, message):
    source = DuckDBTable("churn", build_file, sql=sql if sql.strip() else "x")
    source.sql = sql  # the constructor rejects blank text, so set it to reach the guard

    with pytest.raises(ConfigError, match=message):
        source.get()


def test_a_query_that_does_not_run_is_a_config_error(build_file):
    with pytest.raises(ConfigError, match="the query failed"):
        DuckDBTable("churn", build_file, sql="select no_such_column from gold.churn_by_month").get()


# --------------------------------------------------------------------------- bad input


def test_a_missing_relation_is_a_table_not_found_that_names_the_file(build_file):
    with pytest.raises(TableNotFound, match=r"gold\.nope does not exist in .*ldp_build\.duckdb"):
        DuckDBTable("churn", build_file, relation="gold.nope").get()


def test_a_missing_file_is_a_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="DuckDB file not found"):
        DuckDBTable("churn", tmp_path / "nope.duckdb", relation="t").get()


@pytest.mark.parametrize("given", [{"relation": "t", "sql": "select 1"}, {}], ids=["both", "neither"])
def test_the_source_takes_exactly_one_of_relation_and_sql(tmp_path, given):
    with pytest.raises(ConfigError, match="exactly one of 'relation' and 'sql'"):
        DuckDBTable("churn", tmp_path / "x.duckdb", **given)


@pytest.mark.parametrize("relation", ["a.b.c.d", "a..b", ".t", "t."])
def test_a_malformed_relation_fails_when_the_source_is_built(tmp_path, relation):
    with pytest.raises(ConfigError, match="table, schema.table or catalog.schema.table"):
        DuckDBTable("churn", tmp_path / "x.duckdb", relation=relation)


def test_every_part_of_a_relation_is_quoted():
    assert relation_sql("gold.churn_by_month") == 'SELECT * FROM "gold"."churn_by_month"'
    assert relation_sql('we"ird') == 'SELECT * FROM "we""ird"'
    assert relation_sql("db.s.t") == 'SELECT * FROM "db"."s"."t"'


def test_without_the_duckdb_package_the_error_has_the_install_hint(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)  # makes "import duckdb" raise ImportError

    with pytest.raises(EngineNotFound, match=r'pip install "local-data-platform\[duckdb\]"'):
        DuckDBTable("churn", tmp_path / "x.duckdb", relation="t").get()


# --------------------------------------------------------------------------- the spec


def test_the_spec_schema_and_format_enum_accept_duckdb():
    assert "DUCKDB" in FORMATS and SupportedFormat.DUCKDB.value == "DUCKDB"
    schema = json_schema()
    assert {"DUCKDB", "duckdb"} <= set(schema["$defs"]["format"]["enum"])
    assert {"relation", "sql"} <= set(schema["$defs"]["block"]["properties"])
    assert validate_spec(spec()) == []
    assert validate_spec(spec(relation=None, sql="select 1")) == []


@pytest.mark.parametrize("source, message", [
    ({"relation": "t", "sql": "select 1"}, "exactly one of 'relation'.*got both"),
    ({"relation": None}, "exactly one of 'relation'.*got neither"),
    ({"relation": "  "}, "relation.*non-empty string"),
])
def test_validate_spec_requires_exactly_one_of_relation_and_sql(source, message):
    data = spec()
    data["metadata"]["source"].pop("relation")
    data["metadata"]["source"].update({key: value for key, value in source.items() if value is not None})

    errors = validate_spec(data)

    assert len(errors) == 1 and errors[0].args[0].startswith("metadata.source")
    assert re.search(message, str(errors[0]))


def test_duckdb_is_a_source_format_only():
    data = spec()
    data["metadata"]["target"] = {"name": "t", "format": "DUCKDB", "path": "x.duckdb", "relation": "t"}

    assert any("source format only" in str(error) for error in validate_spec(data))


def test_plan_reports_a_missing_file_and_names_the_route(tmp_path, build_file):
    ok = plan(spec(), base_dir=tmp_path)
    assert ok["valid"] is True and ok["pipeline"] == "DuckDBToIceberg" and ok["route"] == "DUCKDB -> ICEBERG"

    missing = plan(spec(path="target/nope.duckdb"), base_dir=tmp_path)
    assert missing["valid"] is False
    assert any("nope.duckdb does not exist" in error for error in missing["errors"])


def test_ldp_plan_exits_one_for_a_source_with_both_relation_and_sql(tmp_path, build_file, capsys):
    data = spec()
    data["metadata"]["source"]["sql"] = "select 1"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))

    assert main(["plan", str(path)]) == 1
    assert "exactly one of 'relation'" in capsys.readouterr().err
