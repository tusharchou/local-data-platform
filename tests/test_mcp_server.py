"""The read-only MCP server (``local_data_platform.mcp_server``, contract C6).

Covers the statement guard, the locked-down DuckDB sandbox (every setting checked for its
effect, not just its value), table discovery and the allowlist, the six tools, the audit
log, the MCP protocol through the SDK's in-process client, the ``ldp mcp`` CLI hook, and
the scripted stdio client in ``examples/agent_client.py``.

Everything runs offline under ``tmp_path``. The native ``iceberg_scan`` path needs the
DuckDB ``iceberg`` extension already installed; tests that need it skip otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

duckdb = pytest.importorskip("duckdb")

from local_data_platform.exceptions import ConfigError  # noqa: E402
from local_data_platform.format.iceberg import Iceberg  # noqa: E402
from local_data_platform.mcp_server import (  # noqa: E402
    LakeTools,
    QueryRejected,
    add_cli,
    check_sql,
    discover_tables,
)
from local_data_platform.mcp_server.audit import AuditLog, iceberg_audit_sink  # noqa: E402
from local_data_platform.mcp_server.cli import main as mcp_main  # noqa: E402
from local_data_platform.mcp_server.cli import run_command  # noqa: E402
from local_data_platform.mcp_server.discovery import is_allowed, parse_allowlist, redact  # noqa: E402
from local_data_platform.mcp_server.guard import first_keyword  # noqa: E402
from local_data_platform.mcp_server.sandbox import local_path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = ["describe_table", "get_dataset", "list_tables", "query", "sample_rows", "table_history"]
SECRET_VALUE = "hunter2-do-not-leak"


# ---------------------------------------------------------------------- fixtures


@dataclass
class Lake:
    root: Path
    warehouse: Path
    configs: Path
    rides: Iceberg
    secret: Iceberg
    outside_csv: Path
    outside_parquet: Path

    @property
    def rides_location(self) -> str:
        return local_path(self.rides.table().location())

    @property
    def secret_location(self) -> str:
        return local_path(self.secret.table().location())


def _config(name: str, catalog: dict) -> dict:
    return {"identifier": f"{name}_config", "metadata": {
        "source": {"name": name, "format": "CSV", "path": f"data/{name}.csv"},
        "target": {"name": name, "format": "ICEBERG", "catalog": catalog}}}


@pytest.fixture
def lake(tmp_path, make_table) -> Lake:
    warehouse = tmp_path / "warehouse"
    catalog = {"identifier": "demo", "warehouse_path": str(warehouse)}
    rides = Iceberg("rides", catalog, partition_by=[{"column": "pickup_ts", "transform": "day"}])
    rides.put(make_table())
    secret = Iceberg("secret", catalog)
    secret.put(pa.table({"id": [1, 2], "password": [SECRET_VALUE, SECRET_VALUE]}))
    configs = tmp_path / "configs"
    configs.mkdir()
    relative = {"identifier": "demo", "warehouse_path": "../warehouse"}
    (configs / "rides.json").write_text(json.dumps(_config("rides", relative)))
    (configs / "secret.json").write_text(json.dumps(_config("secret", relative)))
    (configs / "sample_query.json").write_text(json.dumps({"query": "SELECT 1"}))  # not a dataset config
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_csv = outside / "private.csv"
    outside_csv.write_text(f"id,password\n1,{SECRET_VALUE}\n")
    outside_parquet = outside / "private.parquet"
    pq.write_table(pa.table({"password": [SECRET_VALUE]}), outside_parquet)
    return Lake(tmp_path, warehouse, configs, rides, secret, outside_csv, outside_parquet)


@pytest.fixture
def tools(lake, tmp_path):
    lake_tools = LakeTools.from_sources([lake.configs], allow="demo.rides", audit_path=tmp_path / "audit.jsonl",
                                        max_rows=50, timeout_s=10)
    yield lake_tools
    lake_tools.close()


def _native_or_skip(lake_tools: LakeTools) -> None:
    if not lake_tools.sandbox.native_available:
        pytest.skip("the DuckDB iceberg extension is not installed (run once with install_extensions=True)")


def _ok(lake_tools: LakeTools, tool: str, **arguments) -> dict:
    outcome = lake_tools.call(tool, arguments)
    assert outcome.ok, outcome.data
    return outcome.data


def _refused(lake_tools: LakeTools, tool: str, **arguments):
    outcome = lake_tools.call(tool, arguments)
    assert not outcome.ok, f"{tool}({arguments}) should have been refused, got {outcome.data}"
    return outcome


# ---------------------------------------------------------------------- statement guard


ALLOWED_SQL = [
    "SELECT 1",
    "select 1;",
    "sElEcT 1",
    "  -- a comment first\n/* and another */ SELECT 1",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "((SELECT 1))",
    "WITH a AS (SELECT 1 AS x) SELECT * FROM a",
    "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT * FROM t",
    "SELECT ';' AS semicolon, 'COPY x TO y' AS text",
    "SELECT 1 /* ; COPY (SELECT 1) TO 'x.csv' */",
    "SELECT 1 AS \"; DROP TABLE x\"",
    "SELECT $$;$$ AS dollar_quoted",
]

REJECTED_SQL = {
    "copy": "COPY (SELECT 1) TO 'x.csv'",
    "copy_from": "COPY t FROM '/etc/passwd'",
    "attach": "ATTACH '/tmp/x.duckdb' AS x",
    "detach": "DETACH x",
    "install": "INSTALL httpfs",
    "force_install": "FORCE INSTALL httpfs",
    "load": "LOAD httpfs",
    "set": "SET enable_external_access = true",
    "set_lock": "SET lock_configuration = false",
    "reset": "RESET lock_configuration",
    "pragma": "PRAGMA version",
    "pragma_enable": "PRAGMA enable_external_access",
    "call": "CALL pragma_version()",
    "checkpoint": "CHECKPOINT",
    "vacuum": "VACUUM",
    "use": "USE memory",
    "explain": "EXPLAIN SELECT 1",
    "describe": "DESCRIBE SELECT 1",
    "show": "SHOW TABLES",
    "summarize": "SUMMARIZE SELECT 1",
    "from_first": "FROM range(3)",
    "values": "VALUES (1)",
    "table": "TABLE demo.rides",
    "pivot": "PIVOT (SELECT 1 AS a) ON a",
    "create": "CREATE TABLE t AS SELECT 1",
    "create_secret": "CREATE SECRET s (TYPE S3, KEY_ID 'a', SECRET 'b')",
    "create_macro": "CREATE MACRO m() AS TABLE SELECT 1",
    "insert": "INSERT INTO t VALUES (1)",
    "update": "UPDATE t SET a = 1",
    "delete": "DELETE FROM t",
    "drop": "DROP VIEW demo.rides",
    "alter": "ALTER VIEW demo.rides RENAME TO x",
    "export": "EXPORT DATABASE '/tmp/x'",
    "import": "IMPORT DATABASE '/etc'",
    "begin": "BEGIN TRANSACTION",
    "prepare": "PREPARE p AS SELECT 1",
    "execute": "EXECUTE p",
    "set_variable": "SET VARIABLE x = 1",
    "multi": "SELECT 1; SELECT 2",
    "multi_write": "SELECT 1; COPY (SELECT 1) TO 'x.csv'",
    "multi_after_comment": "SELECT 1; -- harmless?\nCOPY (SELECT 1) TO 'x.csv'",
    "multi_semicolon_string": "SELECT ';'; COPY (SELECT 1) TO 'x.csv'",
    "comment_hides_keyword": "/* SELECT */ COPY (SELECT 1) TO 'x.csv'",
    "line_comment_hides_keyword": "-- SELECT\nCOPY (SELECT 1) TO 'x.csv'",
    "paren_copy": "(COPY (SELECT 1) TO 'x.csv')",
    "cte_insert": "WITH x AS (SELECT 1 AS a) INSERT INTO t SELECT * FROM x",
    "cte_copy": "WITH x AS (SELECT 1) COPY x TO 'x.csv'",
    "cte_delete_inside": "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "subquery_copy": "SELECT * FROM (COPY (SELECT 1) TO 'x.csv')",
    "select_into": "SELECT 1 INTO t",
    "quoted_select": "\"SELECT\" 1",
    "empty": "",
    "blank": "   \n\t",
    "comment_only": "-- nothing here",
    "garbage": "SELEC 1",
}


@pytest.mark.parametrize("sql", ALLOWED_SQL)
def test_guard_allows_one_select_or_with(sql):
    statement = check_sql(sql)
    assert statement.type == duckdb.StatementType.SELECT


@pytest.mark.parametrize("name", sorted(REJECTED_SQL))
def test_guard_rejects_everything_else(name):
    with pytest.raises(QueryRejected):
        check_sql(REJECTED_SQL[name])


def test_guard_rejects_non_strings_and_huge_sql():
    with pytest.raises(QueryRejected, match="string"):
        check_sql(None)
    with pytest.raises(QueryRejected, match="limit"):
        check_sql("SELECT 1 " + " " * 100_001)


def test_first_keyword_skips_comments_and_parentheses():
    assert first_keyword("/* x */ -- y\n ( ( select 1))") == "select"
    assert first_keyword("  WITH a AS (SELECT 1) SELECT 1") == "with"
    assert first_keyword("-- only a comment") == ""
    assert first_keyword("pragma version") == "pragma"


def test_import_database_is_refused_before_duckdb_parses_it(tmp_path):
    # DuckDB's parser reads <dir>/schema.sql while parsing IMPORT DATABASE, so the guard must stop it first.
    target = tmp_path / "dump"
    target.mkdir()
    (target / "schema.sql").write_text("CREATE TABLE leaked AS SELECT 42;")
    with pytest.raises(QueryRejected, match="IMPORT"):
        check_sql(f"IMPORT DATABASE '{target}'")


# ---------------------------------------------------------------------- sandbox


def test_sandbox_settings_read_back_locked(tools, lake):
    _native_or_skip(tools)
    settings = tools.sandbox.settings()
    assert settings["enable_external_access"] == "false"
    assert settings["lock_configuration"] == "true"
    assert settings["autoinstall_known_extensions"] == "false"
    assert settings["autoload_known_extensions"] == "false"
    assert settings["allow_persistent_secrets"] == "false"
    assert settings["allowed_configs"] == "[]"
    assert settings["temp_directory"] == ""
    # Only the allowlisted table's folder: not the warehouse root (SQLite catalog) or other tables.
    assert tools.sandbox.allowed_directories == [lake.rides_location + "/"]


@pytest.mark.parametrize("statement", [
    "SET enable_external_access = true",
    "SET lock_configuration = false",
    "RESET lock_configuration",
    "SET allowed_directories = ['/']",
    "SET threads = 1",
])
def test_lock_configuration_blocks_changes_even_past_the_guard(tools, statement):
    # Bypass the guard on purpose: DuckDB itself must refuse to loosen the sandbox.
    with pytest.raises(duckdb.Error, match="locked"):
        tools.sandbox._con.execute(statement)


@pytest.mark.parametrize("statement", ["INSTALL httpfs", "LOAD httpfs", "ATTACH '{outside}/x.duckdb' AS x",
                                       "CREATE SECRET s (TYPE S3, KEY_ID 'a', SECRET 'b')"])
def test_external_access_blocks_extensions_and_attach_past_the_guard(tools, lake, statement):
    with pytest.raises(duckdb.Error):
        tools.sandbox._con.execute(statement.format(outside=lake.outside_csv.parent))
    assert not (lake.outside_csv.parent / "x.duckdb").exists()


def _denied_sql(lake: Lake) -> dict[str, str]:
    rides, secret = lake.rides_location, lake.secret_location
    return {
        "read_csv /etc/passwd": "SELECT * FROM read_csv('/etc/passwd')",
        "read_csv_auto /etc/passwd": "SELECT * FROM read_csv_auto('/etc/passwd', header = false)",
        "read_text /etc/hosts": "SELECT * FROM read_text('/etc/hosts')",
        "read_blob file uri": "SELECT * FROM read_blob('file:///etc/passwd')",
        "read_parquet outside": f"SELECT * FROM read_parquet('{lake.outside_parquet}')",
        "read_csv outside": f"SELECT * FROM read_csv('{lake.outside_csv}')",
        "glob root": "SELECT * FROM glob('/*')",
        "glob home": "SELECT * FROM glob('~/*')",
        "catalog database": f"SELECT * FROM read_blob('{lake.warehouse}/demo_catalog.db')",
        "other table files": f"SELECT * FROM read_parquet('{secret}/data/**/*.parquet')",
        "other table iceberg_scan": f"SELECT * FROM iceberg_scan('{lake.secret.table().metadata_location}')",
        "dot-dot traversal": f"SELECT * FROM read_parquet('{rides}/../secret/data/**/*.parquet')",
        "dot-dot to outside": f"SELECT * FROM read_csv('{rides}/../../../outside/private.csv')",
        "glob traversal": f"SELECT * FROM glob('{rides}/../*')",
        "sibling prefix": f"SELECT * FROM read_csv('{rides}_extra/leak.csv')",
        "list with outside": f"SELECT * FROM read_csv(['{lake.outside_csv}'])",
    }


def test_file_functions_outside_the_allowed_tables_are_denied(tools, lake):
    _native_or_skip(tools)
    extra = Path(lake.rides_location + "_extra")
    extra.mkdir()
    (extra / "leak.csv").write_text(f"password\n{SECRET_VALUE}\n")
    for label, sql in _denied_sql(lake).items():
        outcome = _refused(tools, "query", sql=sql)
        assert outcome.status == "denied", (label, outcome.data)
        assert outcome.data["error"]["type"] == "AccessDenied", label
        assert SECRET_VALUE not in json.dumps(outcome.data), label


def test_file_functions_inside_an_allowed_table_work(tools, lake):
    _native_or_skip(tools)
    data = _ok(tools, "query", sql=f"SELECT count(*) AS n FROM read_parquet('{lake.rides_location}/data/**/*.parquet')")
    assert data["rows"] == [[6]]


def test_writes_fail_safely_and_leave_no_files(tools, lake):
    _native_or_skip(tools)
    rides = lake.rides_location
    attempts = [
        f"COPY (SELECT 1) TO '{rides}/stolen.csv'",
        f"EXPORT DATABASE '{rides}/export'",
        f"SELECT * FROM query('COPY (SELECT 1) TO ''{rides}/stolen.csv''')",
        f"SELECT * FROM json_execute_serialized_sql(json_serialize_sql('COPY (SELECT 1) TO ''{rides}/stolen.csv'''))",
        f"WITH x AS (SELECT 1 AS a) COPY x TO '{rides}/stolen.csv'",
        "CREATE VIEW demo.rides AS SELECT 1 AS hijacked",
    ]
    statuses = [_refused(tools, "query", sql=sql).status for sql in attempts]
    # The guard rejects the statements; query() and json_execute_serialized_sql() pass it as SELECTs, and DuckDB
    # then refuses to run anything but a SELECT inside them.
    assert statuses == ["rejected", "rejected", "error", "error", "rejected", "rejected"]
    assert not Path(rides, "stolen.csv").exists()
    assert not Path(rides, "export").exists()
    assert _ok(tools, "query", sql="SELECT count(*) FROM demo.rides")["rows"] == [[6]]


def test_python_variables_are_not_reachable_from_sql(tools):
    leaked_variable = pa.table({"password": [SECRET_VALUE]})  # noqa: F841 - what a replacement scan would find
    outcome = _refused(tools, "query", sql="SELECT * FROM leaked_variable")
    assert SECRET_VALUE not in json.dumps(outcome.data)


def test_allowed_select_returns_exact_rows(tools):
    data = _ok(tools, "query", sql="SELECT city, count(*) AS n, round(sum(fare), 2) AS fares FROM demo.rides "
                                   "GROUP BY city ORDER BY city")
    assert [column["name"] for column in data["columns"]] == ["city", "n", "fares"]
    assert data["rows"] == [["BKK", 2, 27.5], ["LDN", 2, 30.5], ["NYC", 2, 33.5]]
    assert data["truncated"] is False
    # The bare table name works too, and so do CTEs and time functions.
    assert _ok(tools, "query", sql="WITH r AS (SELECT * FROM rides) SELECT max(ride_id) FROM r")["rows"] == [[6]]


def test_row_cap_and_max_rows(tools):
    data = _ok(tools, "query", sql="SELECT * FROM range(1000)", max_rows=7)
    assert data["row_count"] == 7 and data["truncated"] is True and data["max_rows"] == 7
    data = _ok(tools, "query", sql="SELECT * FROM range(1000)", max_rows=10_000)
    assert data["row_count"] == 50 and data["max_rows"] == 50  # clamped to the server cap
    data = _ok(tools, "query", sql="SELECT * FROM range(3)")
    assert data["row_count"] == 3 and data["truncated"] is False


def test_query_timeout_interrupts(lake, tmp_path):
    with LakeTools.from_sources([lake.configs], allow="demo.rides", audit_path=tmp_path / "a.jsonl",
                                timeout_s=0.3) as lake_tools:
        outcome = _refused(lake_tools, "query",
                           sql="SELECT sum(a.range * b.range) FROM range(100000) a, range(1000000) b")
        assert outcome.status == "timeout"
        assert "timeout" in outcome.error
        # The connection is still usable after the interrupt.
        assert _ok(lake_tools, "query", sql="SELECT 42 AS x")["rows"] == [[42]]


def test_arrow_fallback_serves_the_same_rows(lake, tmp_path):
    with LakeTools.from_sources([lake.configs], allow="demo.rides", audit_path=tmp_path / "a.jsonl",
                                native=False) as lake_tools:
        assert lake_tools.sandbox.native_available is False
        assert lake_tools.sandbox.access("demo.rides") == "arrow"
        assert lake_tools.sandbox.allowed_directories == []
        assert _ok(lake_tools, "query", sql="SELECT count(*) FROM demo.rides")["rows"] == [[6]]
        assert _refused(lake_tools, "query", sql=f"SELECT * FROM read_csv('{lake.outside_csv}')").status == "denied"


def test_native_iceberg_scan_is_preferred(tools):
    _native_or_skip(tools)
    assert tools.sandbox.access("demo.rides") == "native"
    views = _ok(tools, "query", sql="SELECT sql FROM duckdb_views() WHERE view_name = 'rides' "
                                    "AND schema_name = 'demo'")
    assert "iceberg_scan" in views["rows"][0][0]


def test_views_refresh_after_a_new_commit(tools, lake, make_table):
    assert _ok(tools, "query", sql="SELECT count(*) FROM demo.rides")["rows"] == [[6]]
    lake.rides.put(make_table(n=3, start_id=100))
    assert _ok(tools, "query", sql="SELECT count(*) FROM demo.rides")["rows"] == [[9]]
    assert _ok(tools, "sample_rows", table="demo.rides", n=50)["row_count"] == 9


# ---------------------------------------------------------------------- discovery and allowlist


def test_allowlist_hides_other_tables(tools, lake):
    listed = _ok(tools, "list_tables")
    assert [item["table"] for item in listed["tables"]] == ["demo.rides"]
    hidden = _refused(tools, "describe_table", table="demo.secret")
    missing = _refused(tools, "describe_table", table="demo.does_not_exist")
    assert hidden.data["error"]["type"] == missing.data["error"]["type"] == "TableNotAllowed"
    assert hidden.error.replace("demo.secret", "X") == missing.error.replace("demo.does_not_exist", "X")
    assert _refused(tools, "sample_rows", table="demo.secret").status == "rejected"
    outcome = _refused(tools, "query", sql="SELECT * FROM demo.secret")
    assert SECRET_VALUE not in json.dumps(outcome.data)


def test_every_table_is_served_without_an_allowlist(lake, tmp_path):
    with LakeTools.from_sources([lake.configs], audit_path=tmp_path / "a.jsonl") as lake_tools:
        assert sorted(lake_tools.tables) == ["demo.rides", "demo.secret"]


def test_allowlist_patterns():
    assert parse_allowlist(None) is None
    assert parse_allowlist("demo.rides, demo.* ,") == ["demo.rides", "demo.*"]
    assert parse_allowlist(["a.b,c.d", "e"]) == ["a.b", "c.d", "e"]
    with pytest.raises(ConfigError):
        parse_allowlist(" , ")
    assert is_allowed("demo.rides", None)
    assert not is_allowed("_ldp.runs", None)
    assert is_allowed("demo.rides", ["demo.*"])
    assert is_allowed("demo.rides", ["rides"])
    assert not is_allowed("demo.rides", ["other.*"])
    assert not is_allowed("_ldp.runs", ["*"])
    assert not is_allowed("_ldp.runs", ["runs"])
    assert is_allowed("_ldp.runs", ["_ldp.*"])


def test_catalog_spec_discovers_every_table_but_not_ldp(lake, tmp_path):
    spec = tmp_path / "catalog.json"
    spec.write_text(json.dumps({"catalog": {"type": "local", "identifier": "demo", "warehouse_path": "warehouse"}}))
    lake.rides.catalog.create_namespace_if_not_exists("_ldp")
    lake.rides.catalog.create_table("_ldp.audit", schema=pa.schema([("tool", pa.string())]))
    found = discover_tables(catalogs=[spec])
    assert sorted(table.identifier for table in found.tables) == ["demo.rides", "demo.secret"]
    assert found.warehouses() == [lake.warehouse.resolve()]
    found.close()
    found = discover_tables(catalogs=[spec], allow="_ldp.*")
    assert [table.identifier for table in found.tables] == ["_ldp.audit"]
    found.close()


def test_discovery_never_creates_a_catalog(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    later = _config("later", {"identifier": "nope", "warehouse_path": "../empty"})
    (configs / "later.json").write_text(json.dumps(later))
    found = discover_tables([configs])
    assert found.tables == []
    assert found.unavailable[0]["table"] == "nope.later"
    assert "run the pipeline first" in found.unavailable[0]["reason"]
    assert not (tmp_path / "empty").exists()


def test_discovery_never_creates_a_sqlite_sql_catalog(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    sql = {"type": "sql", "uri": "sqlite:///../lake/catalog.db", "warehouse": "../lake/wh", "namespace": "nope"}
    (configs / "later.json").write_text(json.dumps(_config("later", sql)))
    found = discover_tables([configs])
    assert found.tables == []
    assert found.unavailable[0]["table"] == "nope.later"
    assert "there is no catalog at" in found.unavailable[0]["reason"]
    assert not (tmp_path / "lake").exists()


def test_discovery_reports_a_missing_table_and_skips_non_configs(lake):
    ghost = _config("ghost", {"identifier": "demo", "warehouse_path": "../warehouse"})
    (lake.configs / "ghost.json").write_text(json.dumps(ghost))
    found = discover_tables([lake.configs])
    assert sorted(table.identifier for table in found.tables) == ["demo.rides", "demo.secret"]
    assert found.unavailable == [{"table": "demo.ghost", "source": str(lake.configs / "ghost.json"),
                                  "reason": "the table does not exist yet; run the pipeline first"}]
    found.close()
    with pytest.raises(ConfigError):
        discover_tables([lake.configs / "sample_query.json"])  # an explicitly named file must be a config
    with pytest.raises(ConfigError):
        discover_tables([lake.root / "missing"])


def test_catalog_handles_redact_secrets():
    spec = {"type": "rest", "uri": "http://x", "token": "abc", "properties": {"s3.secret-access-key": "k", "a": 1}}
    assert redact(spec) == {"type": "rest", "uri": "http://x", "token": "***",
                            "properties": {"s3.secret-access-key": "***", "a": 1}}


# ---------------------------------------------------------------------- tools


def test_list_tables(tools, lake):
    listed = _ok(tools, "list_tables")
    (table,) = listed["tables"]
    assert table["table"] == "demo.rides"
    assert table["sql_name"] == '"demo"."rides"'
    assert table["aliases"] == ["rides"]
    assert table["row_count"] == 6
    assert table["source"] == str(lake.configs / "rides.json")
    assert listed["limits"] == {"max_rows": 50, "timeout_s": 10.0}
    assert listed["unavailable"] == []


def test_describe_table(tools, lake):
    described = _ok(tools, "describe_table", table="demo.rides")
    assert [field["name"] for field in described["schema"]["fields"]] == ["ride_id", "city", "fare", "pickup_ts"]
    assert described["partition_spec"]["fields"] == [
        {"source_column": "pickup_ts", "transform": "day", "name": "pickup_ts_day"}]
    assert described["row_count"] == 6
    assert described["snapshot_count"] == len(lake.rides.snapshots())
    assert described["freshness"]["current_snapshot_id"] == lake.rides.table().current_snapshot().snapshot_id
    assert described["freshness"]["age_seconds"] >= 0
    assert described["latest_run"] is None and described["quality"] is None
    assert described["ldp"] == {"runs_table": False, "quality_table": False}
    # A bare name resolves when it is unique.
    assert _ok(tools, "describe_table", table="rides")["table"] == "demo.rides"


def test_describe_table_falls_back_to_the_snapshot_quality_property(tools, lake, make_table):
    lake.rides.table().append(pa.table({"ride_id": [7], "city": ["NYC"], "fare": [1.0],
                                        "pickup_ts": pa.array([dt.datetime(2024, 1, 3)], pa.timestamp("us"))}),
                              snapshot_properties={"ldp.quality": "warn"})
    quality = _ok(tools, "describe_table", table="demo.rides")["quality"]
    assert quality["source"] == "snapshot" and quality["status"] == "warn"


def test_describe_table_reads_latest_run_and_quality_from_ldp(tools, lake):
    events = pytest.importorskip("local_data_platform.events")
    if not hasattr(events, "IcebergSink") or not hasattr(events, "RunEvent"):
        pytest.skip("local_data_platform.events has no IcebergSink/RunEvent")
    sink = events.IcebergSink({"identifier": "demo", "warehouse_path": str(lake.warehouse)})
    table_uuid = str(lake.rides.table().metadata.table_uuid)
    start = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)
    counter = iter(range(100))

    def emit(run_id: str, kind: str, payload: dict, uuid: str | None = table_uuid) -> None:
        seq = next(counter)
        sink.emit(events.RunEvent(event_id=f"e{seq:03d}", type=kind, schema_version=1,
                                  ts=start + dt.timedelta(seconds=seq), run_id=run_id, attempt=1, seq=seq,
                                  table_uuid=uuid, payload={"pipeline": "rides_config", "table": "demo.rides",
                                                            **payload}))

    results = [{"name": "not_null(ride_id)", "column": "ride_id", "passed": True, "failing_rows": 0, "details": "ok"},
               {"name": "range(fare)", "column": "fare", "passed": False, "failing_rows": 2, "details": "2 rows"}]
    emit("run-old", "run.started", {}, uuid=None)
    emit("run-old", "quality.evaluated", {"passed": True, "checks_run": 1, "checks_failed": 0,
                                          "results": results[:1], "on_failure": "fail"})
    emit("run-old", "run.published", {"snapshot_id": 1})
    emit("run-old", "run.finished", {"status": "published"})
    emit("run-new", "run.started", {}, uuid=None)
    emit("run-new", "quality.evaluated", {"passed": False, "checks_run": 2, "checks_failed": 1, "results": results,
                                          "on_failure": "warn"})
    emit("run-new", "run.published", {"snapshot_id": 2, "idempotency_key": "k-new"})
    emit("run-new", "run.finished", {"status": "published"})
    emit("run-other", "run.started", {"table": "demo.secret"}, uuid="another-uuid")
    sink.flush()

    described = _ok(tools, "describe_table", table="demo.rides")
    run = described["latest_run"]
    assert run["run_id"] == "run-new"
    assert run["status"] == "published"
    assert run["events"] == ["run.started", "quality.evaluated", "run.published", "run.finished"]
    assert run["started_at"].startswith("2026-09-01T12:00:04")
    assert run["idempotency_key"] == "k-new"
    quality = described["quality"]
    assert quality["source"] == "_ldp.quality_results"
    assert quality["passed"] is False and quality["checks_run"] == 2 and quality["checks_failed"] == 1
    assert [check["check"] for check in quality["checks"]] == ["not_null(ride_id)", "range(fare)"]
    assert described["freshness"]["last_run_status"] == "published"
    assert described["ldp"] == {"runs_table": True, "quality_table": True}
    # _ldp tables themselves stay hidden without an explicit allowlist entry.
    assert _refused(tools, "describe_table", table="_ldp.runs").status == "rejected"


def test_sample_rows(tools):
    sample = _ok(tools, "sample_rows", table="demo.rides", n=2)
    assert sample["table"] == "demo.rides" and sample["row_count"] == 2
    assert [column["name"] for column in sample["columns"]] == ["ride_id", "city", "fare", "pickup_ts"]
    assert _ok(tools, "sample_rows", table="demo.rides")["row_count"] == 6
    assert _ok(tools, "sample_rows", table="demo.rides", n=10_000)["max_rows"] == 50


def test_table_history(tools, lake, make_table):
    lake.rides.put(make_table(n=2, start_id=50))
    history = _ok(tools, "table_history", table="demo.rides")
    entries = history["history"]
    assert history["snapshot_count"] == len(entries) == len(lake.rides.snapshots())
    assert entries[0]["is_current"] and entries[0]["snapshot_id"] == history["current_snapshot_id"]
    assert entries[0]["total_records"] == 8 and entries[0]["added_records"] == 2
    assert all(entry["on_main"] for entry in entries)
    assert [entry["committed_at"] for entry in entries] == sorted((e["committed_at"] for e in entries), reverse=True)
    assert history["refs"]["main"]["snapshot_id"] == history["current_snapshot_id"]
    limited = _ok(tools, "table_history", table="demo.rides", limit=1)
    assert len(limited["history"]) == 1 and limited["truncated"] is True


def test_get_dataset_is_a_clear_not_available_result_without_the_module(tools, monkeypatch):
    monkeypatch.setitem(sys.modules, "local_data_platform.datasets", None)  # import now raises ImportError
    data = _ok(tools, "get_dataset", name="train")
    assert data["available"] is False
    assert "local_data_platform.datasets" in data["reason"]


def test_get_dataset_returns_the_pinned_version(tools, lake, make_table):
    datasets = pytest.importorskip("local_data_platform.datasets")
    if not hasattr(datasets, "pin"):
        pytest.skip("local_data_platform.datasets has no pin()")
    first = datasets.pin(lake.rides, "cheap_rides", row_filter="fare < 15")
    lake.rides.put(make_table(n=4, start_id=200))
    second = datasets.pin(lake.rides, "cheap_rides", row_filter="fare < 15")
    datasets.pin(lake.secret, "secret_set")

    data = _ok(tools, "get_dataset", name="cheap_rides")
    assert data["available"] is True and data["version_count"] == 2
    assert data["version"]["snapshot_id"] == second.snapshot_id
    assert data["version"]["table_identifier"] == "demo.rides"
    assert _ok(tools, "get_dataset", name="cheap_rides@v1")["version"]["snapshot_id"] == first.snapshot_id
    if tools.sandbox.access("demo.rides") == "native":
        pinned = _ok(tools, "query", sql=f"SELECT count(*) FROM ({data['sql']})")
        assert pinned["rows"] == [[second.row_count]]
        old = _ok(tools, "get_dataset", name="cheap_rides@v1")
        assert _ok(tools, "query", sql=f"SELECT count(*) FROM ({old['sql']})")["rows"] == [[first.row_count]]
    # A dataset over a table the server does not expose is indistinguishable from a missing one.
    assert _refused(tools, "get_dataset", name="secret_set").data["error"]["type"] == "DatasetNotFound"
    assert _refused(tools, "get_dataset", name="nothing_here").data["error"]["type"] == "DatasetNotFound"
    assert _refused(tools, "get_dataset", name="cheap_rides@vX").status == "rejected"


@pytest.mark.parametrize("tool, arguments, message", [
    ("nope", {}, "unknown tool"),
    ("query", {}, "needs 'sql'"),
    ("query", {"sql": 1}, "string"),
    ("query", {"sql": "SELECT 1", "max_rows": "5"}, "whole number"),
    ("query", {"sql": "SELECT 1", "max_rows": True}, "whole number"),
    ("query", {"sql": "SELECT 1", "max_rows": 0}, "at least 1"),
    ("query", {"sql": "SELECT 1", "extra": 1}, "does not take"),
    ("describe_table", {"table": ""}, "non-empty"),
    ("sample_rows", {"table": "demo.rides", "n": -1}, "at least 1"),
    ("list_tables", {"verbose": True}, "does not take"),
])
def test_invalid_arguments_are_rejected(tools, tool, arguments, message):
    outcome = _refused(tools, tool, **arguments)
    assert outcome.status == "rejected"
    assert message in outcome.error


def test_float_integers_are_accepted(tools):
    assert _ok(tools, "query", sql="SELECT * FROM range(10)", max_rows=3.0)["row_count"] == 3


# ---------------------------------------------------------------------- audit


def test_every_call_is_audited_without_result_data(tools, tmp_path):
    calls = [("list_tables", {}), ("query", {"sql": "SELECT city FROM demo.rides", "max_rows": 4}),
             ("query", {"sql": "COPY (SELECT 1) TO 'x.csv'"}), ("describe_table", {"table": "demo.secret"}),
             ("query", {"sql": "SELECT * FROM read_csv('/etc/passwd')"}), ("sample_rows", {"table": "rides", "n": 2}),
             ("nope", {"x": 1})]
    for tool, arguments in calls:
        tools.call(tool, arguments)
    tools.audit.close()
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert [record["tool"] for record in records] == [tool for tool, _ in calls]
    assert [record["status"] for record in records] == ["ok", "ok", "rejected", "rejected", "denied", "ok",
                                                        "rejected"]
    query = records[1]
    assert query["sql"] == "SELECT city FROM demo.rides"
    assert query["rows"] == 4 and query["truncated"] is True and query["arguments"] == {"max_rows": 4}
    assert query["duration_ms"] >= 0 and query["error"] is None
    assert records[3]["table"] == "demo.secret" and "not available" in records[3]["error"]
    assert records[5]["rows"] == 2
    text = "\n".join(lines)
    for value in ("NYC", "BKK", "LDN", SECRET_VALUE):  # values from the results never reach the log
        assert value not in text


def test_audit_log_uses_an_iceberg_sink_that_can_audit(tmp_path, monkeypatch):
    received = []

    class FakeIcebergSink:
        def __init__(self, catalog_spec, base_dir):
            self.spec = catalog_spec

        def emit(self, event):  # the run-event API is not used for audit records
            raise AssertionError("emit() must not be used for audit records")

        def emit_audit(self, record):
            received.append(record)

        def flush(self):
            received.append("flushed")

    fake_events = types.ModuleType("local_data_platform.events")
    fake_events.IcebergSink = FakeIcebergSink
    monkeypatch.setitem(sys.modules, "local_data_platform.events", fake_events)
    sink = iceberg_audit_sink({"identifier": "demo", "warehouse_path": str(tmp_path)})
    assert isinstance(sink, FakeIcebergSink)
    log = AuditLog(tmp_path / "audit.jsonl", sink=sink)
    assert log.iceberg_enabled
    from local_data_platform.mcp_server.audit import AuditRecord

    log.record(AuditRecord(tool="query", status="ok", duration_ms=1.0, sql="SELECT 1", rows=1))
    log.close()
    assert received[0]["tool"] == "query" and received[0]["sql"] == "SELECT 1"
    assert received[-1] == "flushed"
    assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 1


def test_iceberg_audit_is_skipped_when_the_sink_cannot_audit(tmp_path, monkeypatch):
    class RunsOnlySink:
        def __init__(self, *args):
            raise AssertionError("must not be constructed")

    fake_events = types.ModuleType("local_data_platform.events")
    fake_events.IcebergSink = RunsOnlySink
    monkeypatch.setitem(sys.modules, "local_data_platform.events", fake_events)
    assert iceberg_audit_sink({"identifier": "demo", "warehouse_path": str(tmp_path)}) is None
    monkeypatch.setitem(sys.modules, "local_data_platform.events", None)
    assert iceberg_audit_sink({"identifier": "demo", "warehouse_path": str(tmp_path)}) is None


def test_default_audit_path_is_in_the_warehouse(lake):
    with LakeTools.from_sources([lake.configs], allow="demo.rides") as lake_tools:
        assert lake_tools.audit.path == lake.warehouse.resolve() / ".ldp" / "audit" / "mcp_audit.jsonl"
        lake_tools.call("list_tables", {})
    assert (lake.warehouse / ".ldp" / "audit" / "mcp_audit.jsonl").read_text().count("\n") == 1


def test_a_session_also_audits_to_the_ldp_audit_iceberg_table(lake):
    with LakeTools.from_sources([lake.configs], allow="demo.rides") as lake_tools:
        assert lake_tools.audit.iceberg_enabled
        lake_tools.call("list_tables", {})
        assert lake_tools.call("query", {"sql": "DROP TABLE rides"}).ok is False
    audit = lake.rides.catalog.load_table("_ldp.audit")
    rows = sorted(audit.scan().to_arrow().to_pylist(), key=lambda row: row["tool"])
    assert [(row["tool"], row["status"]) for row in rows] == [("list_tables", "ok"), ("query", "rejected")]
    assert rows[1]["sql"] == "DROP TABLE rides"
    with LakeTools.from_sources([lake.configs]) as lake_tools:
        assert "_ldp.audit" not in lake_tools.tables, "system tables are not served by default"


def test_iceberg_audit_can_be_turned_off(lake):
    with LakeTools.from_sources([lake.configs], allow="demo.rides", iceberg_audit=False) as lake_tools:
        assert not lake_tools.audit.iceberg_enabled
        lake_tools.call("list_tables", {})
    assert not lake.rides.catalog.table_exists("_ldp.audit")


# ---------------------------------------------------------------------- MCP protocol (in-process SDK client)


def _mcp():
    mcp = pytest.importorskip("mcp")
    from local_data_platform.mcp_server import build_server

    return mcp, build_server


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_mcp_lists_the_six_read_only_tools(tools, mode):
    mcp, build_server = _mcp()
    import anyio

    async def scenario():
        async with mcp.Client(build_server(tools), mode=mode) as client:
            listed = await client.list_tools()
            return client.server_info, client.instructions, listed.tools

    server_info, instructions, listed = anyio.run(scenario)
    assert sorted(tool.name for tool in listed) == TOOL_NAMES
    assert all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint for tool in listed)
    by_name = {tool.name: tool for tool in listed}
    assert by_name["query"].input_schema["required"] == ["sql"]
    assert by_name["query"].input_schema["properties"]["max_rows"]["maximum"] == 50
    assert server_info.name == "local-data-platform"
    assert "SELECT" in instructions


def test_mcp_calls_every_tool_and_refuses_bad_sql(tools):
    mcp, build_server = _mcp()
    import anyio

    bad = ["COPY (SELECT 1) TO 'x.csv'", "ATTACH 'x.duckdb'", "INSTALL httpfs", "LOAD httpfs", "SET threads = 1",
           "PRAGMA version", "SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM glob('/*')", "SELECT 1; SELECT 2",
           "/* SELECT */ COPY (SELECT 1) TO 'x.csv'", "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x"]

    async def scenario():
        results = {}
        async with mcp.Client(build_server(tools), mode="legacy") as client:
            results["list_tables"] = await client.call_tool("list_tables", {})
            results["describe_table"] = await client.call_tool("describe_table", {"table": "demo.rides"})
            results["query"] = await client.call_tool("query", {"sql": "SELECT count(*) AS n FROM demo.rides"})
            results["sample_rows"] = await client.call_tool("sample_rows", {"table": "demo.rides", "n": 1})
            results["table_history"] = await client.call_tool("table_history", {"table": "demo.rides"})
            results["get_dataset"] = await client.call_tool("get_dataset", {"name": "none_yet"})
            refused = [await client.call_tool("query", {"sql": sql}) for sql in bad]
        return results, refused

    results, refused = anyio.run(scenario)
    for name in ("list_tables", "describe_table", "query", "sample_rows", "table_history"):
        assert results[name].is_error is False, (name, results[name].content)
        assert json.loads(results[name].content[0].text) == results[name].structured_content
    assert results["query"].structured_content["rows"] == [[6]]
    assert results["sample_rows"].structured_content["row_count"] == 1
    dataset = results["get_dataset"]
    assert dataset.is_error or dataset.structured_content["available"] is False
    for sql, result in zip(bad, refused):
        assert result.is_error is True, sql
        assert result.structured_content["error"]["type"] in ("QueryRejected", "AccessDenied"), sql


def test_building_the_server_does_not_configure_logging(tools):
    _, build_server = _mcp()
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    build_server(tools)
    assert (list(root.handlers), root.level) == before


# ---------------------------------------------------------------------- CLI


def test_add_cli_registers_ldp_mcp():
    parser = argparse.ArgumentParser(prog="ldp")
    returned = add_cli(parser.add_subparsers(dest="command"))
    assert isinstance(returned, argparse.ArgumentParser)
    args = parser.parse_args(["mcp", "--config", "a", "--config", "b", "--catalog", "c.json", "--allow", "demo.*",
                              "--max-rows", "7", "--timeout", "2.5", "--no-native"])
    assert args.handler is run_command
    assert args.config == ["a", "b"] and args.catalog == ["c.json"] and args.allow == ["demo.*"]
    assert args.max_rows == 7 and args.timeout == 2.5 and args.no_native is True
    assert not hasattr(args, "verbose")  # SUPPRESS keeps a top-level -v intact
    with pytest.raises(SystemExit):
        parser.parse_args(["mcp", "--max-rows", "0"])


def test_no_iceberg_audit_reaches_lake_tools(monkeypatch):
    pytest.importorskip("mcp")
    seen = {}

    class Stop(Exception):
        pass

    def fake_from_sources(configs, catalogs, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(LakeTools, "from_sources", staticmethod(fake_from_sources))
    parser = argparse.ArgumentParser(prog="ldp")
    add_cli(parser.add_subparsers(dest="command"))
    for argv, expected in ((["mcp", "--config", "x.json"], True),
                           (["mcp", "--config", "x.json", "--no-iceberg-audit"], False)):
        with pytest.raises(Stop):
            run_command(parser.parse_args(argv))
        assert seen["iceberg_audit"] is expected


def test_cli_errors_are_one_line(tmp_path, capsys, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.chdir(tmp_path)
    assert mcp_main([]) == 1
    assert "needs at least one --config or --catalog" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert mcp_main(["--config", str(empty)]) == 1
    assert "no tables to serve" in capsys.readouterr().err
    assert mcp_main(["--config", str(tmp_path / "missing.json")]) == 1
    assert "not found" in capsys.readouterr().err
    assert not (tmp_path / ".ldp").exists()  # a server that answered nothing leaves no audit file


def test_audit_file_is_created_on_the_first_record(tmp_path):
    from local_data_platform.mcp_server.audit import AuditRecord

    log = AuditLog(tmp_path / "logs" / "audit.jsonl")
    assert not (tmp_path / "logs").exists()
    log.record(AuditRecord(tool="list_tables", status="ok", duration_ms=0.1))
    log.close()
    log.record(AuditRecord(tool="late", status="ok", duration_ms=0.1))
    assert [json.loads(line)["tool"] for line in (tmp_path / "logs" / "audit.jsonl").read_text().splitlines()] == [
        "list_tables"]
    assert [record.tool for record in log.records] == ["list_tables", "late"]


def test_importing_the_package_loads_neither_sdk_nor_duckdb():
    code = ("import sys, local_data_platform.mcp_server; "
            "print(sorted(m for m in ('mcp', 'mcp_types', 'duckdb', 'anyio') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------------- stdio, end to end


def test_example_agent_client_over_stdio(tmp_path):
    pytest.importorskip("mcp")
    command = f"{sys.executable} -m local_data_platform.mcp_server"
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / "agent_client.py"), "--workdir", str(tmp_path / "lake"),
         "--server-cmd", command], capture_output=True, text=True, timeout=300, cwd=tmp_path)
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-3000:]
    out = completed.stdout
    assert "Every tool answered, and every forbidden request was refused." in out
    for step in ("1. list_tables", "2. describe_table", "3. sample_rows", "4. table_history", "5. query",
                 "7. get_dataset", "8. things an agent must not be able to do"):
        assert step in out
    records = (tmp_path / "lake" / "mcp_audit.jsonl").read_text().splitlines()
    # Steps 1-8 make 13 calls. When DuckDB's iceberg extension is installed the tables are served
    # natively, get_dataset returns SQL, and the client re-reads the pinned snapshot with one more query.
    native = "access=native" in out
    assert native == ("re-read through the pinned snapshot" in out)
    assert len(records) == (14 if native else 13)
