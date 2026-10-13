"""``open_lake``, ``ldp query`` over several tables, and reading a table as of a snapshot or a time.

The lake is built with ``tests/support.py``: ``raw.orders`` (3 rows), ``raw.customers`` (2) and ``ref.cities``
(2), each with a config in one folder. Reads run with whatever DuckDB offers: its iceberg extension when it is
installed (a developer machine) and the in-memory fallback otherwise (CI), and the tests that matter for both
force the fallback too.
"""

import datetime as dt
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

from local_data_platform.cli import main
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.lake import open_lake, parse_moment, pick_snapshot, snapshot_as_of
from tests.support import add_table, build_lake, dataset_config, requires_iceberg_extension, without_log_warnings


@pytest.fixture
def lake(tmp_path) -> Path:
    """The config folder of ``raw.orders``, ``raw.customers`` and ``ref.cities``."""
    return build_lake(tmp_path)


@pytest.fixture(params=["auto", "memory"])
def reading(request, monkeypatch):
    """Run a test with DuckDB's iceberg extension as the machine has it, and again with it unavailable."""
    if request.param == "memory":
        monkeypatch.setattr(DuckDBEngine, "_load_iceberg_extension", lambda self: "forced off by the test")
    return request.param


@pytest.fixture
def history(tmp_path, lake):
    """``raw.orders`` with two snapshots, 3 rows and then 5. Returns ``[(snapshot_id, timestamp_ms), ...]``."""
    time.sleep(0.02)  # so that the snapshots of the table are not in the same millisecond
    table = add_table(tmp_path, "raw", "orders", 2, start=4)
    snapshots = [(item["snapshot_id"], item["timestamp_ms"]) for item in table.snapshots()]
    assert len(snapshots) == 2 and snapshots[0][1] < snapshots[1][1]
    return snapshots


def iso(timestamp_ms: int) -> str:
    """``timestamp_ms`` as UTC text with milliseconds, as ``--as-of`` reads it."""
    moment = dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=timestamp_ms)
    return moment.isoformat(sep=" ", timespec="milliseconds")


def count(engine, relation: str) -> int:
    return engine.query(f"SELECT count(*) AS n FROM {relation}").to_pylist()[0]["n"]


# --------------------------------------------------------------------------- reading a time


@pytest.mark.parametrize("text, expected", [
    ("2026-07-31 23:59:59", dt.datetime(2026, 7, 31, 23, 59, 59, tzinfo=dt.timezone.utc)),
    ("2026-07-31T23:59:59", dt.datetime(2026, 7, 31, 23, 59, 59, tzinfo=dt.timezone.utc)),
    ("2026-07-31T23:59:59Z", dt.datetime(2026, 7, 31, 23, 59, 59, tzinfo=dt.timezone.utc)),
    ("2026-07-31T23:59:59+07:00", dt.datetime(2026, 7, 31, 16, 59, 59, tzinfo=dt.timezone.utc)),
    ("2026-07-31 23:59:59-05:00", dt.datetime(2026, 8, 1, 4, 59, 59, tzinfo=dt.timezone.utc)),
    ("2026-07-31T23:59:59.250", dt.datetime(2026, 7, 31, 23, 59, 59, 250000, tzinfo=dt.timezone.utc)),
    ("2026-07-31", dt.datetime(2026, 7, 31, tzinfo=dt.timezone.utc)),
    ("  2026-07-31 12:00  ", dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc)),
])
def test_a_time_is_iso_8601_and_utc_when_it_has_no_offset(text, expected):
    assert parse_moment(text) == expected
    assert parse_moment(text).utcoffset() == dt.timedelta(0)


def test_a_datetime_is_accepted_and_converted_to_utc():
    local = dt.timezone(dt.timedelta(hours=7))

    assert parse_moment(dt.datetime(2026, 7, 31, 12, 0, tzinfo=local)) == dt.datetime(
        2026, 7, 31, 5, 0, tzinfo=dt.timezone.utc)
    assert parse_moment(dt.datetime(2026, 7, 31, 12, 0)) == dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("text", ["yesterday", "", "2026-13-45", "31/07/2026", "9999-12-31T23:59:59-12:00"])
def test_text_that_is_not_a_time_is_a_config_error_with_an_example(text):
    with pytest.raises(ConfigError, match=r"cannot read .* as a time; use ISO 8601, such as '2026-07-31 23:59:59'"):
        parse_moment(text)


# --------------------------------------------------------------------------- picking a snapshot by time

SNAPSHOTS = [(11, 1000), (22, 2000), (33, 3000)]


@pytest.mark.parametrize("limit_ms, expected", [(1000, 11), (1999, 11), (2000, 22), (2500, 22), (3000, 33),
                                                (10**13, 33)])
def test_the_newest_snapshot_committed_at_or_before_the_time_is_picked(limit_ms, expected):
    moment = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=limit_ms)

    assert snapshot_as_of(SNAPSHOTS, moment, table="ns.t") == expected


def test_a_later_snapshot_in_the_same_millisecond_wins_and_the_order_does_not_matter():
    moment = "1970-01-01 00:00:03"

    assert snapshot_as_of([(1, 1000), (2, 1000)], "1970-01-01 00:00:01", table="ns.t") == 2
    assert snapshot_as_of(list(reversed(SNAPSHOTS)), moment, table="ns.t") == 33


def test_a_time_is_compared_to_the_millisecond_and_in_utc():
    assert snapshot_as_of(SNAPSHOTS, "1970-01-01T00:00:01.0009", table="ns.t") == 11  # 0.9 ms later, still 1000
    with pytest.raises(ConfigError):
        snapshot_as_of(SNAPSHOTS, "1970-01-01T00:00:00.9999", table="ns.t")
    assert snapshot_as_of(SNAPSHOTS, "1970-01-01T07:00:02+07:00", table="ns.t") == 22


def test_a_time_before_the_first_snapshot_names_the_table_and_when_it_starts():
    with pytest.raises(ConfigError) as error:
        snapshot_as_of(SNAPSHOTS, "1970-01-01 00:00:00.500", table="raw.orders")

    assert str(error.value) == ("Iceberg table raw.orders has no snapshot committed at or before "
                                "1970-01-01 00:00:00.500 UTC; its oldest snapshot was committed "
                                "1970-01-01 00:00:01.000 UTC")


def test_a_table_with_no_snapshots_says_so():
    with pytest.raises(ConfigError, match=r"Iceberg table raw.orders has no snapshots, so there is nothing to read"):
        snapshot_as_of([], "2026-07-31", table="raw.orders")


def test_pick_snapshot_returns_what_it_was_given_or_none():
    assert pick_snapshot(SNAPSHOTS, table="ns.t") is None
    assert pick_snapshot(SNAPSHOTS, snapshot_id=22, table="ns.t") == 22
    assert pick_snapshot(SNAPSHOTS, snapshot_id=999, table="ns.t") == 999  # whether it exists is the engine's to say
    assert pick_snapshot(SNAPSHOTS, as_of="1970-01-01 00:00:02", table="ns.t") == 22


def test_pick_snapshot_refuses_a_snapshot_and_a_time_together():
    with pytest.raises(ConfigError, match="either a snapshot id or a time, not both"):
        pick_snapshot(SNAPSHOTS, snapshot_id=11, as_of="1970-01-01 00:00:02", table="ns.t")


# --------------------------------------------------------------------------- open_lake


def test_every_table_of_a_folder_is_registered_qualified_and_by_its_bare_name(lake, reading):
    with open_lake(lake) as engine:
        assert sorted(engine.aliases) == ["raw.customers", "raw.orders", "ref.cities"]
        assert count(engine, "raw.orders") == 3 and count(engine, "orders") == 3
        assert count(engine, "raw.customers") == 2 and count(engine, "customers") == 2
        assert count(engine, "ref.cities") == 2 and count(engine, "cities") == 2
        assert count(engine, '"raw.orders"') == 3  # the engine's own alias works too


def test_sql_can_join_the_tables_of_a_lake(lake, reading):
    sql = ("SELECT o.id, o.label AS order_label, c.label AS customer_label FROM raw.orders o "
           "JOIN raw.customers c ON c.id = o.id ORDER BY o.id")

    with open_lake(lake) as engine:
        rows = engine.query(sql).to_pylist()

    assert rows == [{"id": 1, "order_label": "orders-1", "customer_label": "customers-1"},
                    {"id": 2, "order_label": "orders-2", "customer_label": "customers-2"}]


def test_a_config_file_or_several_paths_work_like_a_folder(tmp_path, lake):
    with open_lake(lake / "raw.orders.json") as engine:
        assert engine.aliases == ["raw.orders"] and count(engine, "orders") == 3
    with open_lake([lake / "raw.orders.json", lake / "ref.cities.json"]) as engine:
        assert sorted(engine.aliases) == ["raw.orders", "ref.cities"]
    with open_lake([lake, lake / "raw.orders.json"]) as engine:  # a table named twice is one table
        assert sorted(engine.aliases) == ["raw.customers", "raw.orders", "ref.cities"]
    with open_lake(str(lake / "raw.orders.json")) as engine:  # text works as well as a Path
        assert engine.aliases == ["raw.orders"]


def test_a_name_that_two_tables_share_is_only_registered_qualified(tmp_path, lake):
    add_table(tmp_path, "ref", "orders", 4)

    with open_lake(lake) as engine:
        assert count(engine, "raw.orders") == 3 and count(engine, "ref.orders") == 4
        assert count(engine, "customers") == 2  # the others keep their bare names
        with pytest.raises(duckdb.CatalogException, match="orders"):  # there is no bare table named orders
            engine.query("SELECT * FROM orders")


def test_a_table_in_the_main_namespace_has_one_name(tmp_path, lake):
    add_table(tmp_path, "main", "events", 4)

    with open_lake(lake) as engine:
        assert count(engine, "main.events") == 4 and count(engine, "events") == 4


def test_a_namespace_of_several_parts_is_one_schema_and_is_quoted_in_sql(tmp_path, lake, reading):
    add_table(tmp_path, "a.b", "events", 4)

    with open_lake(lake) as engine:
        assert "a.b.events" in engine.aliases
        assert count(engine, '"a.b".events') == 4 and count(engine, '"a.b.events"') == 4
        assert count(engine, "events") == 4 and count(engine, "raw.orders") == 3  # the bare name and the others
        with pytest.raises(duckdb.Error):  # unquoted, a.b.events reads as catalog a, schema b
            engine.query("SELECT * FROM a.b.events")


def test_a_folder_may_hold_files_that_are_not_configs(lake):
    (lake / "notes.json").write_text(json.dumps({"hello": "world"}))
    (lake / "broken.json").write_text("{not json")

    with open_lake(lake) as engine:
        assert sorted(engine.aliases) == ["raw.customers", "raw.orders", "ref.cities"]


def test_a_table_that_does_not_exist_yet_is_skipped_with_a_warning(lake, caplog):
    (lake / "raw.later.json").write_text(json.dumps(dataset_config("raw", "later")))
    caplog.set_level(logging.WARNING, logger="local_data_platform.lake")

    with open_lake(lake) as engine:
        assert "raw.later" not in engine.aliases and count(engine, "raw.orders") == 3

    assert any(record.getMessage().startswith("Skipping raw.later, named in ") for record in caplog.records)


def test_a_lake_with_no_table_that_exists_is_a_table_not_found_error(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "raw.later.json").write_text(json.dumps(dataset_config("raw", "later")))

    with pytest.raises(TableNotFound, match=r"no Iceberg table to read in .*config\. raw.later: there is no catalog"):
        open_lake(folder)

    assert not (tmp_path / "warehouse").exists(), "reading a lake created a catalog"


def test_configs_without_an_iceberg_table_are_a_table_not_found_error(tmp_path):
    (tmp_path / "export.json").write_text(json.dumps({"identifier": "export", "metadata": {
        "source": {"name": "a", "format": "CSV", "path": "a.csv"},
        "target": {"name": "b", "format": "PARQUET", "path": "b.parquet"}}}))

    with pytest.raises(TableNotFound, match="no Iceberg table found in .*; a config needs a source or a target"):
        open_lake(tmp_path)


def test_a_path_that_does_not_exist_or_is_not_a_config_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="config path not found"):
        open_lake(tmp_path / "nope")
    (tmp_path / "bad.json").write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ConfigError):
        open_lake(tmp_path / "bad.json")  # named explicitly, so it must be a config
    with pytest.raises(ConfigError, match="needs a config file or a folder"):
        open_lake([])


def test_opening_a_lake_writes_nothing(tmp_path, lake):
    def tree():
        return sorted((str(path.relative_to(tmp_path)), path.stat().st_size) for path in tmp_path.rglob("*")
                      if path.is_file())

    before = tree()

    with open_lake(lake) as engine:
        engine.query("SELECT * FROM raw.orders")

    assert tree() == before


def test_a_failed_open_closes_the_engine(lake, monkeypatch):
    closed = []
    real = DuckDBEngine.close
    monkeypatch.setattr(DuckDBEngine, "close", lambda self: (closed.append(True), real(self)))

    with pytest.raises(ConfigError):
        open_lake(lake, snapshot_id=1)  # three tables, so it is refused after the engine exists

    assert closed == [True]


# --------------------------------------------------------------------------- a snapshot or a time


def test_a_snapshot_of_one_table_can_be_read(lake, history, reading):
    first, second = (snapshot_id for snapshot_id, _ in history)

    with open_lake(lake / "raw.orders.json") as engine:
        assert count(engine, "orders") == 5
    with open_lake(lake / "raw.orders.json", snapshot_id=first) as engine:
        assert count(engine, "orders") == 3 and count(engine, "raw.orders") == 3
    with open_lake(lake / "raw.orders.json", snapshot_id=second) as engine:
        assert count(engine, "orders") == 5


def test_a_time_picks_the_snapshot_that_was_the_newest_then(lake, history, reading):
    (_, first_ms), (_, second_ms) = history

    for moment, rows in ((first_ms, 3), (second_ms - 1, 3), (second_ms, 5), (second_ms + 60_000, 5)):
        with open_lake(lake / "raw.orders.json", as_of=iso(moment)) as engine:
            assert count(engine, "raw.orders") == rows, iso(moment)


def test_a_time_with_an_offset_is_read_in_that_offset(lake, history):
    (_, first_ms), (_, second_ms) = history
    local = (dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=second_ms - 1) + dt.timedelta(hours=7))

    with open_lake(lake / "raw.orders.json", as_of=local.isoformat(timespec="milliseconds") + "+07:00") as engine:
        assert count(engine, "orders") == 3


def test_a_snapshot_that_does_not_exist_lists_the_ones_that_do(lake, history):
    (first, _), (second, _) = history

    with pytest.raises(ConfigError) as error:
        open_lake(lake / "raw.orders.json", snapshot_id=12345)

    assert str(error.value) == (f"Snapshot 12345 not found in Iceberg table raw.orders; "
                                f"known snapshots: [{first}, {second}]")


def test_a_time_before_the_first_snapshot_is_an_error(lake, history):
    with pytest.raises(ConfigError, match=r"raw.orders has no snapshot committed at or before 2020-01-01 00:00:00.000 "
                                          r"UTC; its oldest snapshot was committed 20\d\d-"):
        open_lake(lake / "raw.orders.json", as_of="2020-01-01")


def test_a_snapshot_and_a_time_together_are_an_error(lake, history):
    with pytest.raises(ConfigError, match="either a snapshot id or a time"):
        open_lake(lake / "raw.orders.json", snapshot_id=history[0][0], as_of="2999-01-01")


def test_a_snapshot_or_a_time_needs_a_single_table(lake, history):
    with pytest.raises(ConfigError) as error:
        open_lake(lake, as_of="2999-01-01")

    assert str(error.value) == ("a snapshot or a time applies to one table, but 3 were found (raw.customers, "
                                "raw.orders, ref.cities); pass a single config that names one table")


def test_a_folder_of_one_table_can_be_read_at_a_snapshot(tmp_path, history):
    folder = tmp_path / "one"
    folder.mkdir()
    config = json.loads((tmp_path / "config" / "raw.orders.json").read_text())
    config["metadata"]["target"]["catalog"]["warehouse_path"] = "../warehouse"
    (folder / "raw.orders.json").write_text(json.dumps(config))

    with open_lake(folder, snapshot_id=history[0][0]) as engine:
        assert count(engine, "orders") == 3


# --------------------------------------------------------------------------- native and in memory give the same rows


@requires_iceberg_extension
def test_with_the_extension_the_tables_are_native_scans(lake):
    with open_lake(lake) as engine:
        assert all(engine.is_native(alias) for alias in engine.aliases)


def test_without_the_extension_the_tables_are_read_into_memory_and_it_warns_once(lake, monkeypatch, caplog):
    monkeypatch.setattr(DuckDBEngine, "_load_iceberg_extension", lambda self: "forced off by the test")
    caplog.set_level(logging.WARNING)

    with open_lake(lake) as engine:
        assert not any(engine.is_native(alias) for alias in engine.aliases)
        assert count(engine, "orders") == 3

    warnings = [record for record in caplog.records if "Registering Iceberg tables in memory" in record.getMessage()]
    assert len(warnings) == 1


# --------------------------------------------------------------------------- offline, and without the MCP SDK


def run_python(code: str, *args: str, home: Path | None = None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)  # DuckDB looks for extensions in ~/.duckdb
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, env=env, timeout=300,
                          check=False)


def test_a_lake_opens_without_downloading_anything(tmp_path, lake):
    home = tmp_path / "empty_home"
    home.mkdir()
    code = ("import sys, duckdb\n"
            "from local_data_platform.lake import open_lake\n"
            "with open_lake(sys.argv[1]) as engine:\n"
            "    assert engine.query('SELECT count(*) AS n FROM raw.orders').to_pylist() == [{'n': 3}]\n"
            "row = duckdb.connect().execute(\"SELECT installed FROM duckdb_extensions() "
            "WHERE extension_name = 'iceberg'\").fetchone()\n"
            "print('installed', row[0])\n")

    done = run_python(code, str(lake), home=home)

    assert done.returncode == 0, done.stderr
    assert "installed False" in done.stdout  # open_lake never ran INSTALL iceberg


def test_a_lake_opens_without_the_mcp_sdk(lake):
    code = ("import sys\n"
            "sys.modules['mcp'] = None  # makes `import mcp` fail, as it does without the extra\n"
            "from local_data_platform.lake import open_lake\n"
            "with open_lake(sys.argv[1]) as engine:\n"
            "    print(engine.query('SELECT count(*) AS n FROM orders').to_pylist())\n")

    done = run_python(code, str(lake))

    assert done.returncode == 0, done.stderr
    assert "[{'n': 3}]" in done.stdout


def test_importing_the_module_loads_neither_duckdb_nor_the_mcp_sdk():
    code = ("import sys\n"
            "import local_data_platform.lake\n"
            "loaded = [name for name in ('duckdb', 'mcp') if name in sys.modules]\n"
            "assert not loaded, loaded\n")

    done = run_python(code)

    assert done.returncode == 0, done.stderr


# --------------------------------------------------------------------------- ldp query


def ldp(capsys, *argv):
    status = main([str(item) for item in argv])
    out, err = capsys.readouterr()
    return status, out, without_log_warnings(err)


def single_value(out: str) -> int:
    """The number in the one-row, one-column table that ``ldp query`` printed."""
    lines = out.splitlines()
    assert lines[-1] == "(1 row)", out
    return int(lines[-2])


def test_query_over_a_folder_joins_its_tables(lake, capsys):
    sql = "SELECT o.id, c.label FROM raw.orders o JOIN raw.customers c ON c.id = o.id ORDER BY o.id"

    status, out, err = ldp(capsys, "query", lake, sql)

    assert status == 0 and err == ""
    assert [line.split() for line in out.splitlines()] == [["id", "label"], ["--", "-----------"],
                                                           ["1", "customers-1"], ["2", "customers-2"], ["(2", "rows)"]]


def test_query_over_several_configs_uses_their_tables(lake, capsys):
    status, out, err = ldp(capsys, "query", lake / "raw.orders.json", lake / "ref.cities.json",
                           "SELECT (SELECT count(*) FROM orders) * (SELECT count(*) FROM cities) AS n")

    assert status == 0 and err == "" and single_value(out) == 6


def test_query_over_a_folder_keeps_max_rows(lake, capsys):
    status, out, _ = ldp(capsys, "query", lake, "SELECT * FROM raw.orders ORDER BY id", "--max-rows", "2")

    assert status == 0 and "... 1 more rows" in out and out.splitlines()[-1] == "(3 rows)"


def test_options_may_come_before_the_configs_or_after_the_sql(lake, capsys):
    orders, cities, sql = lake / "raw.orders.json", lake / "ref.cities.json", "SELECT * FROM orders ORDER BY id"

    status, out, _ = ldp(capsys, "query", "--max-rows", "1", orders, cities, sql)
    assert status == 0 and "... 2 more rows" in out
    status, out, _ = ldp(capsys, "query", orders, cities, sql, "--max-rows", "1")
    assert status == 0 and "... 2 more rows" in out


def test_a_single_config_file_still_registers_the_target_under_its_name(lake, capsys):
    status, out, err = ldp(capsys, "query", lake / "raw.orders.json", "--max-rows", "2",
                           "SELECT * FROM orders ORDER BY id")

    assert status == 0 and err == "" and "... 1 more rows" in out


def test_query_of_a_folder_with_no_table_that_exists_fails_cleanly(tmp_path, capsys):
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "raw.later.json").write_text(json.dumps(dataset_config("raw", "later")))

    status, out, err = ldp(capsys, "query", folder, "SELECT 1")

    assert status == 1 and out == ""
    assert err.splitlines()[-1].startswith("ldp: error: no Iceberg table to read in ")
    assert not (tmp_path / "warehouse").exists()


def test_query_help_shows_the_new_forms(capsys):
    assert main(["query", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())

    assert "CONFIG [CONFIG ...] SQL" in out and "--snapshot SNAPSHOT_ID" in out and "--as-of TIME" in out
    assert "Put the options before the first CONFIG or after SQL" in out


@pytest.mark.parametrize("flag, value", [("--snapshot", "latest"), ("--as-of", "yesterday")])
def test_a_flag_value_that_cannot_be_read_is_a_usage_error(lake, capsys, flag, value):
    status, out, err = ldp(capsys, "query", lake / "raw.orders.json", "SELECT 1", flag, value)

    assert status == 1 and out == "" and f"argument {flag}:" in err


def test_a_snapshot_and_a_time_together_are_a_usage_error(lake, capsys):
    status, _, err = ldp(capsys, "query", lake / "raw.orders.json", "SELECT 1", "--snapshot", "1",
                         "--as-of", "2999-01-01")

    assert status == 1 and "argument --as-of: not allowed with argument --snapshot" in err


def test_query_reads_a_snapshot(lake, history, reading, capsys):
    (first, _), (second, _) = history
    config, sql = lake / "raw.orders.json", "SELECT count(*) AS n FROM orders"

    assert single_value(ldp(capsys, "query", config, sql)[1]) == 5
    assert single_value(ldp(capsys, "query", config, sql, "--snapshot", first)[1]) == 3
    assert single_value(ldp(capsys, "query", "--snapshot", second, config, sql)[1]) == 5


def test_query_reads_as_of_a_time(lake, history, reading, capsys):
    (_, first_ms), (_, second_ms) = history
    config, sql = lake / "raw.orders.json", "SELECT count(*) AS n FROM orders"

    assert single_value(ldp(capsys, "query", config, sql, "--as-of", iso(second_ms - 1))[1]) == 3
    assert single_value(ldp(capsys, "query", config, sql, "--as-of", iso(second_ms))[1]) == 5
    assert single_value(ldp(capsys, "query", config, sql, "--as-of", "2999-01-01")[1]) == 5


def test_query_as_of_logs_the_snapshot_it_chose_with_verbose(lake, history, capsys):
    (first, first_ms), (_, second_ms) = history

    status, _, err = ldp(capsys, "-v", "query", lake / "raw.orders.json", "SELECT count(*) AS n FROM orders",
                         "--as-of", iso(second_ms - 1))

    assert status == 0 and f"snapshot {first}, committed {iso(first_ms)} UTC" in err


def test_query_with_an_unknown_snapshot_lists_the_known_ones(lake, history, capsys):
    (first, _), (second, _) = history

    status, out, err = ldp(capsys, "query", lake / "raw.orders.json", "SELECT 1", "--snapshot", "12345")

    assert status == 1 and out == ""
    assert err == (f"ldp: error: Snapshot 12345 not found in Iceberg table raw.orders; "
                   f"known snapshots: [{first}, {second}]\n")


def test_query_with_a_time_before_the_first_snapshot_fails_cleanly(lake, history, capsys):
    status, out, err = ldp(capsys, "query", lake / "raw.orders.json", "SELECT 1", "--as-of", "2020-01-01")

    assert status == 1 and out == ""
    assert err.startswith("ldp: error: Iceberg table raw.orders has no snapshot committed at or before 2020-01-01 ")


def test_query_with_a_snapshot_over_several_tables_says_to_pass_a_single_config(lake, history, capsys):
    status, out, err = ldp(capsys, "query", lake, "SELECT 1", "--snapshot", history[0][0])

    assert status == 1 and out == ""
    assert err.startswith("ldp: error: a snapshot or a time applies to one table, but 3 were found")
    assert "pass a single config" in err


def test_query_at_a_snapshot_of_a_table_that_does_not_exist_leaves_nothing_behind(tmp_path, capsys):
    config = tmp_path / "config"
    config.mkdir()
    (config / "raw.later.json").write_text(json.dumps(dataset_config("raw", "later")))

    status, _, err = ldp(capsys, "query", config / "raw.later.json", "SELECT 1", "--snapshot", "1")

    assert status == 1 and err.startswith("ldp: error: Iceberg table raw.later does not exist: there is no catalog at")
    assert not (tmp_path / "warehouse").exists()


# --------------------------------------------------------------------------- the demo table's snapshots


@pytest.fixture
def demo(tmp_path, capsys):
    """The ``ldp demo`` workdir: ``rides.json`` and a table with a snapshot for each append, overwrite and upsert."""
    workdir = tmp_path / "demo"
    assert main(["demo", "--workdir", str(workdir), "--rows", "40", "--seed", "7"]) == 0
    capsys.readouterr()
    table = Iceberg("rides", {"identifier": "demo", "warehouse_path": str(workdir / "warehouse")})
    return workdir / "rides.json", table.snapshots()


def test_every_snapshot_of_the_demo_table_reads_back_its_recorded_row_count(demo, capsys):
    config, snapshots = demo
    assert len(snapshots) >= 4

    for snapshot in snapshots:
        status, out, _ = ldp(capsys, "query", config, "SELECT count(*) AS n FROM rides", "--snapshot",
                             snapshot["snapshot_id"])
        assert status == 0, out
        assert single_value(out) == int(snapshot["summary"]["total-records"]), snapshot["snapshot_id"]


def test_the_demo_table_read_as_of_the_time_of_each_snapshot(demo, capsys):
    config, snapshots = demo
    # Two commits in one millisecond would share a time, so use the first pair that does not.
    pairs = [(a, b) for a, b in zip(snapshots, snapshots[1:]) if a["timestamp_ms"] < b["timestamp_ms"]]
    assert pairs, "every snapshot of the demo has the same timestamp"
    earlier, later = pairs[0]
    sql = "SELECT count(*) AS n FROM rides"

    before = single_value(ldp(capsys, "query", config, sql, "--as-of", iso(later["timestamp_ms"] - 1))[1])
    after = single_value(ldp(capsys, "query", config, sql, "--as-of", iso(later["timestamp_ms"]))[1])

    assert before == int(earlier["summary"]["total-records"])
    assert after == int(later["summary"]["total-records"])
    assert single_value(ldp(capsys, "query", config, sql, "--as-of", "2999-01-01")[1]) == int(
        snapshots[-1]["summary"]["total-records"])
