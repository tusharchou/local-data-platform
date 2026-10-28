"""The monitors and their history: the freshness monitor, ``_ldp.monitor_results`` and ``ldp monitor``."""

import datetime as dt
import json
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.catalog.provider import create_catalog
from local_data_platform.cli import main
from local_data_platform.events import MONITOR_TABLE, IcebergSink, RunEvent, load_run_events, run_events
from local_data_platform.exceptions import ConfigError
from local_data_platform.monitoring import STATUSES, MonitorResult, evaluate
from local_data_platform.monitoring.history import JSONL_PATH, read_jsonl, read_results, record
from local_data_platform.monitoring.results import worst
from local_data_platform.spec import plan

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
FRESHNESS = {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"}}
SCHEMA = pa.schema([("id", pa.int64()), ("ordered_at", pa.timestamp("us")),
                    ("ordered_tz", pa.timestamp("us", tz="UTC")), ("day", pa.date32()), ("amount", pa.float64())])
NEWEST = dt.datetime(2026, 10, 27, 12, 30, 15)
HOUR_MS = 3600 * 1000


def _orders(newest: dt.datetime = NEWEST, day: dt.date = dt.date(2026, 10, 27)) -> pa.Table:
    older = newest - dt.timedelta(hours=2)
    return pa.Table.from_pylist([
        {"id": 1, "ordered_at": older, "ordered_tz": older.replace(tzinfo=UTC), "day": day - dt.timedelta(days=1),
         "amount": 10.5},
        {"id": 2, "ordered_at": newest, "ordered_tz": newest.replace(tzinfo=UTC), "day": day, "amount": 20.0},
    ], schema=SCHEMA)


def _catalog(tmp_path: Path, namespace: str = "shop"):
    return create_catalog({"identifier": namespace, "warehouse_path": str(tmp_path / "warehouse")})


def _table(tmp_path: Path, rows: pa.Table | None, name: str = "orders", namespace: str = "shop", **properties):
    """A table in the lake of ``tmp_path``, with ``rows`` in one commit when there are any."""
    catalog = _catalog(tmp_path, namespace)
    catalog.create_namespace_if_not_exists(namespace)
    table = catalog.create_table(f"{namespace}.{name}", schema=SCHEMA, properties=properties)
    if rows is not None:
        table.append(rows)
    return catalog.load_table(f"{namespace}.{name}")


def _committed(table) -> dt.datetime:
    return dt.datetime(1970, 1, 1, tzinfo=UTC) + dt.timedelta(milliseconds=table.current_snapshot().timestamp_ms)


def _config_data(identifier: str, sla, sinks=None, table: str | None = None, target: dict | None = None,
                 namespace: str = "shop") -> dict:
    metadata = {"source": {"name": identifier, "format": "CSV", "path": f"../{identifier}.csv"},
                "target": target or {"name": table or identifier, "format": "ICEBERG", "write_mode": "overwrite",
                                     "catalog": {"identifier": namespace, "warehouse_path": "../warehouse"}}}
    if sla is not None:
        metadata["sla"] = sla
    if sinks:
        metadata["observability"] = {"sinks": sinks}
    return {"identifier": identifier, "who": "ops", "metadata": metadata}


def _write(tmp_path: Path, identifier: str = "orders", sla="default", **options) -> Path:
    sla = {"freshness": FRESHNESS} if sla == "default" else sla
    folder = tmp_path / "config"
    folder.mkdir(exist_ok=True)
    path = folder / f"{identifier}.json"
    path.write_text(json.dumps(_config_data(identifier, sla, **options)))
    return path


def _config(tmp_path: Path, identifier: str = "orders", sla="default", **options) -> Config:
    return Config.from_json(_write(tmp_path, identifier, sla, **options))


# ---------------------------------------------------------------------- a result


def test_a_result_round_trips_through_a_dict_and_a_row():
    result = MonitorResult(monitor="freshness", kind="freshness", status="warn", evaluated_at=NOW, pipeline="orders",
                           table_identifier="shop.orders", table_uuid="u-1", observed="8.0 hour",
                           expected="error after 26 hour", details="late", metrics={"age_seconds": 28800.0})
    data = result.to_dict()
    assert data["evaluated_at"] == "2026-10-28T12:00:00Z" and data["schema_version"] == 1
    assert json.loads(json.dumps(data)) == data
    assert MonitorResult.from_dict(data) == result
    row = {**data, "ts": NOW, "metrics": json.dumps(data["metrics"])}
    del row["evaluated_at"]
    assert MonitorResult.from_dict(row) == result


def test_a_result_has_a_known_status_and_a_time_zone():
    with pytest.raises(ValueError, match="status must be one of"):
        MonitorResult(monitor="m", kind="k", status="fine", evaluated_at=NOW)
    with pytest.raises(ValueError, match="needs a time zone"):
        MonitorResult(monitor="m", kind="k", status="ok", evaluated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="evaluated_at"):
        MonitorResult.from_dict({"monitor": "m", "kind": "k", "status": "ok"})
    assert STATUSES == ("ok", "warn", "error", "unknown")


def test_the_worst_status_is_the_most_severe():
    def make(status):
        return MonitorResult(monitor="m", kind="k", status=status, evaluated_at=NOW)

    assert worst([]) is None
    assert worst([make("ok"), make("unknown")]) == "unknown"
    assert worst([make("warn"), make("unknown"), make("ok")]) == "warn"
    assert worst([make("ok"), make("error"), make("warn")]) == "error"


# ---------------------------------------------------------------------- freshness of the last commit


@pytest.mark.parametrize("after_ms, status", [
    (0, "ok"), (HOUR_MS, "ok"), (6 * HOUR_MS, "ok"), (6 * HOUR_MS + 1, "warn"), (26 * HOUR_MS, "warn"),
    (26 * HOUR_MS + 1, "error"), (100 * HOUR_MS, "error"),
])
def test_a_table_is_late_when_it_is_older_than_a_threshold(tmp_path, after_ms, status):
    table = _table(tmp_path, _orders())
    [result] = evaluate(_config(tmp_path), _committed(table) + dt.timedelta(milliseconds=after_ms))
    assert (result.monitor, result.kind, result.status) == ("freshness", "freshness", status)
    assert result.expected == "warn after 6 hour and error after 26 hour"
    assert result.metrics["age_seconds"] == after_ms / 1000 and result.metrics["source"] == "snapshot"
    assert result.metrics["warn_after_seconds"] == 6 * 3600 and result.metrics["error_after_seconds"] == 26 * 3600


def test_a_result_says_what_it_saw_and_which_table(tmp_path):
    table = _table(tmp_path, _orders())
    committed = _committed(table)
    [result] = evaluate(_config(tmp_path), committed + dt.timedelta(hours=30))
    assert result.status == "error" and result.observed == "30.0 hour"
    assert result.details == f"the last commit was 30.0 hour ago, at {committed.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    assert (result.pipeline, result.table_identifier) == ("orders", "shop.orders")
    assert result.table_uuid == str(table.metadata.table_uuid)
    assert result.evaluated_at == committed + dt.timedelta(hours=30)
    assert result.metrics["snapshot_id"] == table.current_snapshot().snapshot_id
    assert result.metrics["reference_time"] == committed.isoformat().replace("+00:00", "Z")


def test_the_age_is_in_the_period_of_the_threshold_and_never_below_zero(tmp_path):
    table = _table(tmp_path, _orders())
    committed = _committed(table)
    day = _config(tmp_path, sla={"freshness": {"warn_after": {"count": 1, "period": "day"}}})
    [result] = evaluate(day, committed + dt.timedelta(hours=36))
    assert (result.status, result.observed, result.expected) == ("warn", "1.5 day", "warn after 1 day")
    [early] = evaluate(day, committed - dt.timedelta(hours=5))
    assert (early.status, early.observed, early.metrics["age_seconds"]) == ("ok", "0.0 day", 0.0)
    minutes = _config(tmp_path, sla={"freshness": {"error_after": {"count": 90, "period": "minute"}}})
    [late] = evaluate(minutes, committed + dt.timedelta(hours=3))
    assert (late.status, late.observed) == ("error", "180.0 minute")
    both = _config(tmp_path, sla={"freshness": {"warn_after": {"count": 30, "period": "minute"},
                                                "error_after": {"count": 2, "period": "hour"}}})
    [mixed] = evaluate(both, committed + dt.timedelta(hours=3))
    assert (mixed.status, mixed.observed) == ("error", "3.0 hour"), "the age is in the period of the error threshold"


def test_the_last_commit_is_read_from_metadata_and_no_row_is_scanned(tmp_path, monkeypatch):
    from pyiceberg.table import Table

    table = _table(tmp_path, _orders())

    def refuse(*args, **kwargs):
        raise AssertionError("a monitor must not scan a table")

    monkeypatch.setattr(Table, "scan", refuse)
    config = _config(tmp_path, sla={"freshness": {**FRESHNESS, "column": "ordered_at"}})
    assert evaluate(_config(tmp_path), _committed(table))[0].status == "ok"
    assert evaluate(config, NOW)[0].metrics["source"] == "column"


# ---------------------------------------------------------------------- freshness of a column


COLUMN = {"error_after": {"count": 26, "period": "hour"}, "column": "ordered_at"}


@pytest.mark.parametrize("now, status", [
    (dt.datetime(2026, 10, 28, 12, 30, 15, tzinfo=UTC), "ok"),
    (dt.datetime(2026, 10, 28, 14, 30, 15, tzinfo=UTC), "ok"),
    (dt.datetime(2026, 10, 28, 14, 30, 16, tzinfo=UTC), "error"),
])
@pytest.mark.parametrize("column", ["ordered_at", "ordered_tz"])
def test_a_column_says_how_old_the_data_is_and_not_when_it_was_loaded(tmp_path, column, now, status):
    _table(tmp_path, _orders())
    config = _config(tmp_path, sla={"freshness": {**COLUMN, "column": column}})
    [result] = evaluate(config, now)
    assert result.status == status
    assert result.metrics["source"] == "column" and result.metrics["column"] == column
    assert result.metrics["reference_time"] == "2026-10-27T12:30:15Z"
    assert result.details.startswith(f"the newest {column} is 2026-10-27T12:30:15Z, ")


def test_the_newest_value_is_the_newest_of_all_the_data_files(tmp_path):
    table = _table(tmp_path, _orders(dt.datetime(2026, 10, 27, 12, 30, 15)))
    table.append(_orders(dt.datetime(2026, 10, 26, 8, 0, 0), day=dt.date(2026, 10, 26)))
    [result] = evaluate(_config(tmp_path, sla={"freshness": COLUMN}), NOW)
    assert result.metrics["reference_time"] == "2026-10-27T12:30:15Z"


def test_a_date_counts_as_the_end_of_its_day(tmp_path):
    _table(tmp_path, _orders())
    config = _config(tmp_path, sla={"freshness": {"warn_after": {"count": 6, "period": "hour"}, "column": "day"}})
    [result] = evaluate(config, dt.datetime(2026, 10, 28, 8, 0, 0, tzinfo=UTC))
    assert (result.status, result.observed) == ("warn", "8.0 hour")
    assert result.details == ("the newest day is 2026-10-27, which ends at 2026-10-28T00:00:00Z, 8.0 hour ago")
    [fresh] = evaluate(config, dt.datetime(2026, 10, 28, 5, 59, 59, tzinfo=UTC))
    assert fresh.status == "ok"


def test_a_column_that_cannot_say_how_fresh_a_table_is_an_error(tmp_path):
    _table(tmp_path, _orders())
    [wrong] = evaluate(_config(tmp_path, sla={"freshness": {**COLUMN, "column": "amount"}}), NOW)
    assert wrong.status == "error"
    assert wrong.details == "the column amount is double, and only timestamp and date columns are accepted"
    [missing] = evaluate(_config(tmp_path, sla={"freshness": {**COLUMN, "column": "nope"}}), NOW)
    assert (missing.status, missing.details) == ("error", "the table has no column nope")


def test_a_table_whose_files_carry_no_bounds_is_unknown_and_one_with_no_files_is_an_error(tmp_path):
    _table(tmp_path, _orders(), name="nobounds", **{"write.metadata.metrics.default": "none"})
    config = _config(tmp_path, "nobounds", sla={"freshness": COLUMN})
    [unknown] = evaluate(config, NOW)
    assert unknown.status == "unknown"
    assert unknown.details.startswith("the data files carry no upper bound for ordered_at")
    emptied = _table(tmp_path, _orders(), name="emptied")
    emptied.overwrite(pa.Table.from_pylist([], schema=SCHEMA))
    [empty] = evaluate(_config(tmp_path, "emptied", sla={"freshness": COLUMN}), NOW)
    assert empty.status == "error" and empty.details == "the table has no data files, so there is no newest ordered_at"
    deleted = _table(tmp_path, _orders(), name="deleted")
    deleted.delete("id > 0")
    [none] = evaluate(_config(tmp_path, "deleted", sla={"freshness": COLUMN}), NOW)
    assert none.status == "error" and none.details.startswith("the table has no data files")


# ---------------------------------------------------------------------- a table that is not there


def test_a_table_that_was_never_written_is_an_error_and_nothing_is_created(tmp_path):
    config = _config(tmp_path)
    [result] = evaluate(config, NOW)
    assert result.status == "error" and result.details == "table has never been written"
    assert result.observed == "never written" and result.table_uuid is None
    assert result.metrics == {"reason": "the catalog does not exist yet"}
    assert (result.pipeline, result.table_identifier) == ("orders", "shop.orders")
    assert not (tmp_path / "warehouse").exists()


def test_a_catalog_without_the_namespace_or_the_table_is_left_as_it_is(tmp_path):
    catalog = _catalog(tmp_path)
    [no_namespace] = evaluate(_config(tmp_path), NOW)
    assert (no_namespace.status, no_namespace.details) == ("error", "table has never been written")
    assert no_namespace.metrics == {"reason": "the table does not exist in its catalog"}
    assert list(catalog.list_namespaces()) == []

    _table(tmp_path, _orders(), name="customers")
    [no_table] = evaluate(_config(tmp_path), NOW)
    assert no_table.status == "error" and no_table.details == "table has never been written"
    assert sorted(catalog.list_tables("shop")) == [("shop", "customers")]


def test_a_catalog_that_says_there_is_no_namespace_is_a_table_that_was_never_written(tmp_path, monkeypatch):
    from pyiceberg.exceptions import NoSuchNamespaceError

    _catalog(tmp_path)  # the SQLite file is there, so the monitor goes on to ask the catalog

    class Catalog:
        def load_table(self, identifier):
            raise NoSuchNamespaceError(f"no namespace for {identifier}")

    monkeypatch.setattr("local_data_platform.catalog.provider.create_catalog", lambda spec, base_dir=None: Catalog())
    [result] = evaluate(_config(tmp_path), NOW)
    assert (result.status, result.details) == ("error", "table has never been written")
    assert result.metrics == {"reason": "the table does not exist in its catalog"}


def test_a_table_with_no_commit_is_a_table_that_was_never_written(tmp_path):
    _table(tmp_path, None)
    [result] = evaluate(_config(tmp_path), NOW)
    assert (result.status, result.details) == ("error", "table has never been written")
    assert result.metrics == {"reason": "the table has no snapshot"} and result.table_uuid is not None


def test_a_target_that_is_a_file_has_no_age(tmp_path):
    target = {"name": "out", "format": "CSV", "path": "../out.csv"}
    [result] = evaluate(_config(tmp_path, target=target), NOW)
    assert (result.status, result.details) == ("unknown", "the target is a CSV file, not an Iceberg table")
    assert result.table_uuid is None and result.observed is None


# ---------------------------------------------------------------------- evaluate


def test_a_config_with_no_sla_has_no_monitor(tmp_path):
    _table(tmp_path, _orders())
    assert evaluate(_config(tmp_path, sla=None), NOW) == []


def test_the_moment_is_a_datetime_or_iso_text_and_a_bad_one_is_an_error(tmp_path):
    _table(tmp_path, _orders())
    config = _config(tmp_path)
    assert evaluate(config, "2026-10-28T12:00:00Z")[0].evaluated_at == NOW
    assert evaluate(config, "2026-10-28 12:00:00")[0].evaluated_at == NOW
    assert evaluate(config, "2026-10-28T19:00:00+07:00")[0].evaluated_at == NOW
    assert evaluate(config)[0].evaluated_at > NOW
    with pytest.raises(ConfigError, match="cannot read 'soon' as a time"):
        evaluate(config, "soon")


# ---------------------------------------------------------------------- the Iceberg sink


def test_the_iceberg_sink_appends_monitor_results_to_a_day_partitioned_table(tmp_path, catalog_config):
    sink = IcebergSink(catalog_config)
    result = MonitorResult(monitor="freshness", kind="freshness", status="error", evaluated_at=NOW, pipeline="orders",
                           table_identifier="shop.orders", observed="30.0 hour", expected="error after 26 hour",
                           details="late, token=abc123 was used", metrics={"age_seconds": 108000.0})
    sink.emit_monitor(result.to_dict())
    assert not (tmp_path / "warehouse").exists(), "nothing is written before flush"
    sink.flush()
    sink.flush()  # an empty flush commits nothing
    table = sink.catalog.load_table(f"_ldp.{MONITOR_TABLE}")
    assert len(table.snapshots()) == 1 and sink.monitor_identifier == "_ldp.monitor_results"
    [field] = table.spec().fields
    assert (field.name, str(field.transform)) == ("ts_day", "day")
    [row] = table.scan().to_arrow().to_pylist()
    assert (row["pipeline"], row["monitor"], row["kind"], row["status"]) == (
        "orders", "freshness", "freshness", "error")
    assert row["ts"] == NOW and row["table_identifier"] == "shop.orders" and row["table_uuid"] is None
    assert json.loads(row["metrics"]) == {"age_seconds": 108000.0} and row["schema_version"] == 1
    assert "abc123" not in row["details"] and "token=***" in row["details"]
    assert MonitorResult.from_dict(row).metrics == {"age_seconds": 108000.0}


def test_the_iceberg_sink_flushes_monitor_results_when_a_batch_is_full(tmp_path, catalog_config):
    sink = IcebergSink(catalog_config, batch_size=2)
    result = MonitorResult(monitor="freshness", kind="freshness", status="ok", evaluated_at=NOW).to_dict()
    sink.emit_monitor(result)
    assert not (tmp_path / "warehouse").exists()
    sink.emit_monitor(result)
    table = sink.catalog.load_table(f"_ldp.{MONITOR_TABLE}")
    assert len(table.snapshots()) == 1 and table.scan().to_arrow().num_rows == 2


# ---------------------------------------------------------------------- the history


def test_results_go_to_a_file_next_to_a_config_with_no_iceberg_sink(tmp_path):
    table = _table(tmp_path, _orders())
    config = _config(tmp_path)
    first = evaluate(config, _committed(table) + dt.timedelta(hours=1))
    second = evaluate(config, _committed(table) + dt.timedelta(hours=30))
    path = config.resolve(JSONL_PATH)
    assert record([(config, first)]) == [str(path)]
    assert record([(config, second)]) == [str(path)]
    assert path == tmp_path / "config" / ".ldp" / "monitor.jsonl"
    assert len(path.read_text().splitlines()) == 2
    assert read_results(config) == first + second
    assert [result.status for result in read_results(config, monitor="freshness")] == ["ok", "error"]
    assert read_results(config, monitor="volume") == []


def test_a_file_of_the_history_skips_what_is_not_a_result(tmp_path, caplog):
    path = tmp_path / "monitor.jsonl"
    good = MonitorResult(monitor="freshness", kind="freshness", status="ok", evaluated_at=NOW).to_dict()
    path.write_text(json.dumps(good) + "\n\n{not json\n" + json.dumps({"monitor": "x"}) + "\n")
    assert [result.status for result in read_jsonl(path)] == ["ok"]
    assert caplog.text.count("not a monitor result") == 2
    assert read_jsonl(tmp_path / "missing.jsonl") == []


def test_the_results_of_one_invocation_are_one_commit_for_each_catalog(tmp_path, capsys):
    tables = [_table(tmp_path, _orders(), name=name) for name in ("orders", "customers")]
    for name in ("orders", "customers"):
        _write(tmp_path, name, sinks=["iceberg"])
    now = max(_committed(table) for table in tables) + dt.timedelta(hours=1)
    assert main(["monitor", str(tmp_path / "config"), "--now", now.isoformat()]) == 0
    catalog = _catalog(tmp_path)
    history = catalog.load_table(f"_ldp.{MONITOR_TABLE}")
    assert len(history.snapshots()) == 1
    assert sorted(row["pipeline"] for row in history.scan().to_arrow().to_pylist()) == ["customers", "orders"]
    assert "Results recorded in _ldp.monitor_results" in capsys.readouterr().out
    assert main(["monitor", str(tmp_path / "config"), "--now", now.isoformat()]) == 0
    assert len(catalog.load_table(f"_ldp.{MONITOR_TABLE}").snapshots()) == 2
    assert not (tmp_path / "config" / ".ldp").exists(), "an Iceberg sink leaves no file"
    config = Config.from_json(tmp_path / "config" / "orders.json")
    assert [result.pipeline for result in read_results(config)] == ["orders", "orders"]


def test_a_config_whose_catalog_does_not_exist_yet_records_into_the_file(tmp_path):
    config = _config(tmp_path, sinks=["iceberg"])
    results = evaluate(config, NOW)
    assert record([(config, results)]) == [str(config.resolve(JSONL_PATH))]
    assert not (tmp_path / "warehouse").exists(), "recording never creates a catalog"
    assert read_results(config) == results


def test_the_results_of_two_catalogs_are_two_destinations(tmp_path):
    first, second = _table(tmp_path, _orders()), _table(tmp_path, _orders(), name="orders", namespace="other")
    configs = [_config(tmp_path, sinks=["iceberg"]),
               _config(tmp_path, "orders_other", sinks=["iceberg"], table="orders", namespace="other")]
    entries = [(config, evaluate(config, max(_committed(first), _committed(second)))) for config in configs]
    assert record(entries) == ["_ldp.monitor_results", "_ldp.monitor_results"]
    for namespace in ("shop", "other"):
        assert len(_catalog(tmp_path, namespace).load_table(f"_ldp.{MONITOR_TABLE}").scan().to_arrow()) == 1


def test_the_history_of_a_config_is_its_file_and_its_table_in_the_order_of_time(tmp_path):
    config = _config(tmp_path, sinks=["iceberg"])
    first = evaluate(config, NOW)  # there is no catalog yet, so these go to the file
    assert record([(config, first)]) == [str(config.resolve(JSONL_PATH))]
    _table(tmp_path, _orders())
    later = NOW + dt.timedelta(days=1)
    second = evaluate(config, later)
    assert record([(config, second)]) == ["_ldp.monitor_results"]
    assert read_results(config) == first + second
    plain = _config(tmp_path, "plain", table="orders")  # no sink, so it shares the file of the folder
    other = _config(tmp_path, "customers", sinks=["iceberg"], table="orders")  # and this one the table
    record([(plain, evaluate(plain, later)), (other, evaluate(other, later))])
    assert [result.pipeline for result in read_jsonl(config.resolve(JSONL_PATH))] == ["orders", "plain"]
    assert read_results(config) == first + second, "only the results of the pipeline itself"
    assert [result.pipeline for result in read_results(other)] == ["customers"]
    assert [result.pipeline for result in read_results(plain)] == ["plain"]


def test_recording_nothing_writes_nothing(tmp_path):
    config = _config(tmp_path, sla=None)
    assert record([(config, [])]) == []
    assert not (tmp_path / "config" / ".ldp").exists()


# ---------------------------------------------------------------------- ldp monitor


def _ldp(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ldp_monitor_prints_one_line_for_each_monitor_and_exits_with_2_for_an_error(tmp_path, capsys):
    table = _table(tmp_path, _orders())
    path = _write(tmp_path)
    late = (_committed(table) + dt.timedelta(hours=30)).isoformat()
    code, out, err = _ldp(["monitor", str(path), "--now", late], capsys)
    assert (code, err) == (2, "")
    lines = out.splitlines()
    assert lines[0].startswith("Monitors at 20") and lines[1].split() == [
        "pipeline", "table", "monitor", "status", "observed", "expected", "details"]
    [row] = [line for line in lines if line.startswith("orders ")]
    assert row.split()[:5] == ["orders", "shop.orders", "freshness", "error", "30.0"]
    assert "1 error" in out and f"Results recorded in {path.parent / '.ldp' / 'monitor.jsonl'}" in out


@pytest.mark.parametrize("hours, code, status", [(1, 0, "ok"), (8, 0, "warn"), (30, 2, "error")])
def test_the_exit_status_is_0_for_ok_and_warn_and_2_for_an_error(tmp_path, capsys, hours, code, status):
    table = _table(tmp_path, _orders())
    now = (_committed(table) + dt.timedelta(hours=hours)).isoformat()
    exit_status, out, _ = _ldp(["monitor", str(_write(tmp_path)), "--now", now, "--json"], capsys)
    assert exit_status == code and json.loads(out)[0]["status"] == status


def test_ldp_monitor_json_is_a_list_of_results_and_nothing_else(tmp_path, capsys):
    table = _table(tmp_path, _orders())
    _write(tmp_path)
    _write(tmp_path, "plain", sla=None)
    code, out, err = _ldp(["monitor", str(tmp_path / "config"), "--json", "--now",
                           (_committed(table) + dt.timedelta(hours=1)).isoformat()], capsys)
    assert (code, err) == (0, "")
    [result] = json.loads(out)
    assert result["monitor"] == "freshness" and result["status"] == "ok" and result["pipeline"] == "orders"
    assert MonitorResult.from_dict(result).table_identifier == "shop.orders"


def test_ldp_monitor_of_a_table_that_was_never_written_exits_with_2_and_creates_no_catalog(tmp_path, capsys):
    path = _write(tmp_path)
    code, out, _ = _ldp(["monitor", str(path), "--now", NOW.isoformat()], capsys)
    assert code == 2 and "never written" in out and "table has never been written" in out
    assert not (tmp_path / "warehouse").exists()


def test_ldp_monitor_reads_a_folder_by_the_kind_rule_and_reports_what_it_cannot_read(tmp_path, capsys):
    table = _table(tmp_path, _orders())
    folder = tmp_path / "config"
    _write(tmp_path, "orders")
    _write(tmp_path, "plain", sla=None)
    _write(tmp_path, "broken", sla={"freshness": {"error_after": {"count": 0, "period": "hour"}}})
    workflow = {"kind": "Workflow", "identifier": "daily", "metadata": {}, "tasks": []}
    (folder / "daily.json").write_text(json.dumps(workflow))
    (folder / "notes.json").write_text(json.dumps({"hello": "world"}))
    (folder / "broken_json.json").write_text("{not json")
    now = (_committed(table) + dt.timedelta(hours=1)).isoformat()
    code, out, err = _ldp(["monitor", str(folder), "--now", now], capsys)
    assert code == 1, "a config that cannot be read is a failure of the tool"
    assert [line.split()[0] for line in out.splitlines() if line.startswith("orders ")] == ["orders"]
    assert "plain.json has no metadata.sla, so there is nothing to check" in out
    assert "ldp: broken.json: metadata.sla.freshness.error_after.count: must be a whole number from 1, got 0" in err
    assert "ldp: broken_json.json: config file " in err and "is not valid JSON" in err
    assert "daily.json" not in err and "notes.json" not in err
    assert "Results recorded in" in out, "the monitors that ran are recorded"


def test_every_result_of_one_invocation_is_measured_at_the_same_moment(tmp_path, capsys):
    for name in ("orders", "customers"):
        _table(tmp_path, _orders(), name=name)
        _write(tmp_path, name)
    code, out, _ = _ldp(["monitor", str(tmp_path / "config"), "--json"], capsys)
    results = json.loads(out)
    assert code == 0 and len(results) == 2
    assert len({result["evaluated_at"] for result in results}) == 1


def test_ldp_monitor_of_a_config_with_no_sla_says_so_and_exits_with_0(tmp_path, capsys):
    path = _write(tmp_path, sla=None)
    code, out, err = _ldp(["monitor", str(path)], capsys)
    assert (code, err) == (0, "")
    assert out == "orders.json has no metadata.sla, so there is nothing to check\n"
    assert not (tmp_path / "config" / ".ldp").exists()


def test_ldp_monitor_names_the_configs_of_a_folder_that_have_no_sla_in_one_line(tmp_path, capsys):
    for name in ("customers", "menu", "orders"):
        _write(tmp_path, name, sla=None)
    code, out, err = _ldp(["monitor", str(tmp_path / "config")], capsys)
    assert (code, err) == (0, "")
    assert out == ("3 configs have no metadata.sla, so there is nothing to check in "
                   "customers.json, menu.json, orders.json\n")


def test_ldp_monitor_stops_at_a_path_that_is_not_there_and_at_a_folder_with_no_configs(tmp_path, capsys):
    code, _, err = _ldp(["monitor", str(tmp_path / "nope.json")], capsys)
    assert code == 1 and "config path not found" in err
    (tmp_path / "empty").mkdir()
    code, _, err = _ldp(["monitor", str(tmp_path / "empty")], capsys)
    assert code == 1 and "no dataset configs in" in err


def test_ldp_monitor_refuses_a_moment_that_is_not_a_time(tmp_path, capsys):
    code, _, err = _ldp(["monitor", str(_write(tmp_path)), "--now", "yesterday"], capsys)
    assert code == 1 and "cannot read 'yesterday' as a time" in err


def test_ldp_monitor_changes_neither_the_table_nor_the_runs_that_ldp_runs_shows(tmp_path, capsys):
    (tmp_path / "orders.csv").write_text("order_id,amount,ordered_at\n1,10.5,2026-08-01\n2,20.0,2026-08-02\n")
    path = _write(tmp_path, sinks=["iceberg", "jsonl"])
    assert main(["run", str(path)]) == 0
    catalog = _catalog(tmp_path)
    table = catalog.load_table("shop.orders")
    snapshots = [item.snapshot_id for item in table.snapshots()]
    runs_commits = len(catalog.load_table("_ldp.runs").snapshots())
    events = (path.parent / ".ldp" / "events.jsonl").read_bytes()
    capsys.readouterr()
    before = _ldp(["runs", str(path)], capsys)
    now = (_committed(table) + dt.timedelta(hours=1)).isoformat()
    code, out, _ = _ldp(["monitor", str(path), "--now", now], capsys)
    assert code == 0 and "Results recorded in _ldp.monitor_results" in out
    assert _ldp(["runs", str(path)], capsys) == before
    assert [item.snapshot_id for item in _catalog(tmp_path).load_table("shop.orders").snapshots()] == snapshots
    assert len(_catalog(tmp_path).load_table("_ldp.runs").snapshots()) == runs_commits
    assert (path.parent / ".ldp" / "events.jsonl").read_bytes() == events
    assert not (path.parent / ".ldp" / "monitor.jsonl").exists()
    assert len(_catalog(tmp_path).load_table(f"_ldp.{MONITOR_TABLE}").snapshots()) == 1


def test_ldp_monitor_is_a_command_and_its_help_says_what_the_exit_statuses_mean(capsys):
    from local_data_platform.cli import MODULE_COMMANDS

    assert ("local_data_platform.monitoring.cli", ("monitor",)) in MODULE_COMMANDS
    assert main(["monitor", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Exit status is 0 when every monitor is ok or warn, 2 when at least one is error, and 1 when ldp itself" in \
        help_text
    assert main(["--help"]) == 0
    top = " ".join(capsys.readouterr().out.split())
    assert "monitor" in top and "2 when ldp monitor finds an SLA breach" in top


# ---------------------------------------------------------------------- volume

VOLUME = {"min_rows": 50, "max_rows": 200, "max_change_pct": 50}


def _append_target(mode: str = "append", **extra) -> dict:
    return {"name": "orders", "format": "ICEBERG", "write_mode": mode, **extra,
            "catalog": {"identifier": "shop", "warehouse_path": "../warehouse"}}


def _event(number: int, kind: str, **payload) -> RunEvent:
    ts = NOW + dt.timedelta(minutes=number)
    return RunEvent(event_id=f"event-{number:03d}-{kind}", type=kind, schema_version=1, ts=ts,
                    run_id=f"run-{number:03d}", attempt=1, seq=0 if kind != "run.finished" else 1,
                    payload={"pipeline": "orders", "table": "shop.orders", **payload})


def _history(tmp_path: Path, counts, *, statuses=None, start: int = 1) -> None:
    """Write the events of runs that wrote ``counts`` rows, oldest first, into the file of the jsonl sink.

    The runs are numbered from ``start``, and a status for a number says how that run ended.
    """
    lines = []
    for number, rows in enumerate(counts, start=start):
        status = (statuses or {}).get(number, "published")
        lines.append(_event(number, f"run.{status}", rows_written=rows, snapshot_id=1000 + number))
        lines.append(_event(number, "run.finished", status=status, rows_written=rows))
    folder = tmp_path / "config" / ".ldp"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(event.to_json() + "\n" for event in lines)


def _volume(tmp_path: Path, counts, sla=None, **options) -> dict[str, MonitorResult]:
    """The results of the volume monitor over a history that wrote ``counts`` rows, by the name of the monitor."""
    path = _write(tmp_path, sla={"volume": sla or VOLUME}, sinks=["jsonl"], **options)
    _history(tmp_path, counts)
    return {result.monitor: result for result in evaluate(Config.from_json(path), NOW)}


def test_a_normal_run_is_inside_the_limits_and_near_the_median_of_the_runs_before_it(tmp_path):
    results = _volume(tmp_path, [100, 102, 98, 101, 99, 100, 103])
    assert sorted(results) == ["volume.change", "volume.rows"]
    rows, change = results["volume.rows"], results["volume.change"]
    assert (rows.status, rows.kind, rows.observed, rows.expected) == ("ok", "volume", "103 rows",
                                                                      "min 50 rows and max 200 rows")
    assert rows.details == "the latest run wrote 103 rows, inside the limits"
    assert (change.status, change.observed) == ("ok", "+3.0 percent")
    assert change.expected == "change over 50 percent of the last 7 runs"
    assert change.details == ("the latest run wrote 103 rows, 3.0 percent more than the median of 6 earlier runs, "
                              "100 rows")
    assert change.metrics == {"observed": 103, "baseline_median": 100, "change_pct": 3.0, "earlier_runs": 6,
                              "lookback_runs": 7, "max_change_pct": 50, "source": "events", "run_id": "run-007",
                              "snapshot_id": 1007}
    assert rows.metrics == {"min_rows": 50, "max_rows": 200, "observed": 103, "source": "events",
                            "run_id": "run-007", "snapshot_id": 1007}
    assert (rows.pipeline, change.pipeline, rows.evaluated_at) == ("orders", "orders", NOW)


@pytest.mark.parametrize("latest, status, observed", [
    (150, "ok", "+50.0 percent"), (151, "error", "+51.0 percent"), (50, "ok", "-50.0 percent"),
    (49, "error", "-51.0 percent"), (10, "error", "-90.0 percent"), (300, "error", "+200.0 percent"),
    (100, "ok", "+0.0 percent"),
])
def test_a_run_is_off_when_it_is_further_from_the_median_than_the_limit(tmp_path, latest, status, observed):
    change = _volume(tmp_path, [100] * 6 + [latest], {"max_change_pct": 50})["volume.change"]
    assert (change.status, change.observed) == (status, observed)
    assert change.metrics["baseline_median"] == 100 and change.metrics["change_pct"] == float(observed.split()[0])


def test_a_drop_of_90_percent_is_an_error_that_says_how_far_it_fell(tmp_path):
    results = _volume(tmp_path, [100, 102, 98, 101, 99, 100, 10])
    assert results["volume.change"].status == "error"
    assert results["volume.change"].details == (
        "the latest run wrote 10 rows, 90.0 percent fewer than the median of 6 earlier runs, 100 rows")
    assert results["volume.rows"].details == ("the latest run wrote 10 rows, fewer than the 50 rows that min_rows "
                                              "asks for")
    assert worst(results.values()) == "error"


def test_a_spike_is_an_error_too(tmp_path):
    results = _volume(tmp_path, [100, 102, 98, 101, 99, 100, 300])
    assert results["volume.rows"].status == "error"
    assert results["volume.rows"].details == ("the latest run wrote 300 rows, more than the 200 rows that max_rows "
                                              "allows")
    assert (results["volume.change"].status, results["volume.change"].observed) == ("error", "+200.0 percent")


@pytest.mark.parametrize("latest, status", [(50, "ok"), (49, "error"), (200, "ok"), (201, "error")])
def test_the_limits_on_the_rows_hold_at_the_edge(tmp_path, latest, status):
    assert _volume(tmp_path, [100, latest], {"min_rows": 50, "max_rows": 200})["volume.rows"].status == status


def test_a_section_with_one_rule_makes_one_result(tmp_path):
    only_min = _volume(tmp_path, [3], {"min_rows": 5})
    assert list(only_min) == ["volume.rows"] and only_min["volume.rows"].expected == "min 5 rows"
    assert only_min["volume.rows"].details == ("the latest run wrote 3 rows, fewer than the 5 rows that min_rows "
                                               "asks for")
    assert only_min["volume.rows"].metrics == {"min_rows": 5, "observed": 3, "source": "events", "run_id": "run-001",
                                               "snapshot_id": 1001}


def test_with_fewer_than_3_earlier_runs_the_change_is_unknown_and_never_an_alert(tmp_path):
    for counts, earlier in (([100], 0), ([100, 100], 1), ([100, 100, 100], 2)):
        change = _volume(tmp_path, counts, {"max_change_pct": 1})["volume.change"]
        assert (change.status, change.details, change.observed) == ("unknown", "not enough history", None)
        assert change.metrics["earlier_runs"] == earlier and change.metrics["needed_runs"] == 3
        (tmp_path / "config" / ".ldp" / "events.jsonl").unlink()
    enough = _volume(tmp_path, [100, 100, 100, 100], {"max_change_pct": 1})["volume.change"]
    assert (enough.status, enough.metrics["earlier_runs"]) == ("ok", 3)
    assert _volume(tmp_path, [100, 100, 100, 100, 5], {"max_change_pct": 1})["volume.change"].status == "error"


def test_the_median_is_taken_over_the_lookback_and_over_the_earlier_runs_only(tmp_path):
    counts = [500, 500, 500, 100, 100, 100, 100]
    short = _volume(tmp_path, counts, {"max_change_pct": 10, "lookback_runs": 4})["volume.change"]
    assert (short.status, short.metrics["baseline_median"], short.metrics["earlier_runs"]) == ("ok", 100, 3)
    (tmp_path / "config" / ".ldp" / "events.jsonl").unlink()
    long = _volume(tmp_path, counts, {"max_change_pct": 10, "lookback_runs": 7})["volume.change"]
    assert (long.status, long.metrics["baseline_median"], long.observed) == ("error", 300, "-66.7 percent")


def test_runs_that_wrote_nothing_have_no_change_in_percent(tmp_path):
    unknown = _volume(tmp_path, [0, 0, 0, 0, 5], {"max_change_pct": 50})["volume.change"]
    assert unknown.status == "unknown"
    assert unknown.details == "the earlier runs wrote no rows, so a change in percent has no meaning"
    (tmp_path / "config" / ".ldp" / "events.jsonl").unlink()
    same = _volume(tmp_path, [0, 0, 0, 0, 0], {"max_change_pct": 50})["volume.change"]
    assert (same.status, same.observed) == ("ok", "+0.0 percent")
    assert same.details == "the latest run wrote 0 rows, the same as the median of 4 earlier runs, 0 rows"


def test_only_published_runs_count_and_a_run_counts_once_whatever_its_attempts_did(tmp_path):
    path = _write(tmp_path, sla={"volume": {"max_change_pct": 50}}, sinks=["jsonl"])
    ended = {5: "blocked_quality", 6: "failed", 7: "skipped_duplicate"}
    _history(tmp_path, [100, 100, 100, 100, 0, 0, 0], statuses=ended)
    # a run that failed on its first attempt and published on its second one
    first, second = _event(8, "run.failed", message="x"), _event(8, "run.published", rows_written=100, snapshot_id=1008)
    folder = tmp_path / "config" / ".ldp"
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(first.to_json() + "\n")
        retried = RunEvent(event_id="retry", type="run.published", schema_version=1, ts=second.ts, run_id=second.run_id,
                           attempt=2, seq=0, payload=second.payload)
        handle.write(retried.to_json() + "\n")
    # a run that published on its first attempt, and found its key published on the second one
    published, duplicate = _event(9, "run.published", rows_written=100, snapshot_id=1009), _event(
        9, "run.skipped_duplicate", rows_written=0)
    again = RunEvent(event_id="again", type="run.skipped_duplicate", schema_version=1, ts=duplicate.ts,
                     run_id=duplicate.run_id, attempt=2, seq=0, payload=duplicate.payload)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(published.to_json() + "\n" + again.to_json() + "\n")
    [change] = evaluate(Config.from_json(path), NOW)
    assert change.metrics["earlier_runs"] == 5 and change.metrics["run_id"] == "run-009"
    assert (change.status, change.metrics["observed"]) == ("ok", 100)


def test_one_odd_run_among_the_earlier_ones_does_not_move_the_median(tmp_path):
    change = _volume(tmp_path, [1000, 100, 100, 100, 100, 100, 100], {"max_change_pct": 10})["volume.change"]
    assert (change.status, change.metrics["baseline_median"], change.observed) == ("ok", 100, "+0.0 percent")


def test_a_published_run_with_no_count_is_not_a_run_with_a_count(tmp_path):
    path = _write(tmp_path, sla={"volume": {"max_change_pct": 50}}, sinks=["jsonl"])
    _history(tmp_path, [100] * 5)
    events = [_event(6, "run.published", snapshot_id=1006), _event(6, "run.finished", status="published")]
    with open(tmp_path / "config" / ".ldp" / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(event.to_json() + "\n" for event in events)
    [change] = evaluate(Config.from_json(path), NOW)
    assert (change.status, change.metrics["run_id"], change.metrics["earlier_runs"]) == ("ok", "run-005", 4)


def test_a_table_with_no_run_and_no_history_has_nothing_to_measure(tmp_path):
    path = _write(tmp_path, sla={"volume": VOLUME})
    rows, change = evaluate(Config.from_json(path), NOW)
    assert (rows.status, rows.details) == ("unknown", "no published run yet, so there is no row count to check")
    assert (change.status, change.details) == ("unknown", "not enough history")
    assert rows.metrics == {"min_rows": 50, "max_rows": 200, "source": "none"}
    assert not (tmp_path / "warehouse").exists()


def test_a_target_that_is_a_file_has_the_counts_of_its_history(tmp_path):
    target = {"name": "out", "format": "CSV", "path": "../out.csv"}
    results = _volume(tmp_path, [100, 100, 100, 100, 10], {"min_rows": 50, "max_change_pct": 50}, target=target)
    assert (results["volume.rows"].status, results["volume.change"].status) == ("error", "error")
    assert results["volume.rows"].table_uuid is None


def _load_rows(tmp_path: Path, rows: int, *, key: str | None = None) -> int:
    (tmp_path / "orders.csv").write_text("order_id,amount\n" + "".join(f"{i},{i}.5\n" for i in range(1, rows + 1)))
    argv = ["run", str(tmp_path / "config" / "orders.json")]
    return main(argv + (["--idempotency-key", key] if key else []))


@pytest.mark.parametrize("sinks, source", [(["jsonl"], "events"), (["iceberg"], "events"), (None, "snapshots")])
def test_real_runs_give_the_same_counts_from_their_history_and_from_the_snapshots(tmp_path, sinks, source):
    path = _write(tmp_path, sla={"volume": {**VOLUME, "lookback_runs": 7}}, sinks=sinks, target=_append_target())
    for rows in (100, 102, 98, 101, 99, 100, 10):
        assert _load_rows(tmp_path, rows) == 0
    rows, change = evaluate(Config.from_json(path), NOW)
    assert (rows.status, rows.observed, rows.metrics["source"]) == ("error", "10 rows", source)
    assert (change.status, change.observed) == ("error", "-90.0 percent")
    assert (change.metrics["baseline_median"], change.metrics["earlier_runs"]) == (100, 6)
    table = _catalog(tmp_path).load_table("shop.orders")
    assert rows.metrics["snapshot_id"] == table.current_snapshot().snapshot_id
    word = "run wrote" if source == "events" else "commit added"
    assert rows.details == f"the latest {word} 10 rows, fewer than the 50 rows that min_rows asks for"


def test_an_overwrite_counts_the_rows_of_the_new_batch(tmp_path):
    for sinks in (["jsonl"], None):
        folder = tmp_path / ("with" if sinks else "without")
        folder.mkdir()
        path = _write(folder, sla={"volume": {"min_rows": 1}}, sinks=sinks, target=_append_target("overwrite"))
        for rows in (5, 7):
            assert _load_rows(folder, rows) == 0
        [result] = evaluate(Config.from_json(path), NOW)
        assert (result.status, result.metrics["observed"]) == ("ok", 7)


def test_a_commit_that_rewrites_files_is_not_a_batch_and_the_lookback_limits_the_commits(tmp_path):
    path = _write(tmp_path, sla={"volume": {"min_rows": 1}}, target=_append_target())
    for rows in (100, 100):
        assert _load_rows(tmp_path, rows) == 0
    table = _catalog(tmp_path).load_table("shop.orders")
    table.delete("order_id = 1")  # it rewrites both files, and the rewrite adds 198 rows that are not new
    summary = _catalog(tmp_path).load_table("shop.orders").current_snapshot().summary
    assert (summary.operation.value, summary.additional_properties["added-records"]) == ("overwrite", "198")
    [result] = evaluate(Config.from_json(path), NOW)
    assert (result.status, result.metrics["observed"], result.metrics["source"]) == ("ok", 100, "snapshots")

    other = tmp_path / "other"
    other.mkdir()
    path = _write(other, sla={"volume": {"max_change_pct": 10, "lookback_runs": 4}}, target=_append_target())
    for rows in (500, 500, 500, 100, 100, 100, 100):
        assert _load_rows(other, rows) == 0
    [change] = evaluate(Config.from_json(path), NOW)
    assert (change.status, change.metrics["baseline_median"], change.metrics["earlier_runs"]) == ("ok", 100, 3)


def test_an_upsert_counts_the_rows_that_it_changed_and_not_the_rows_of_the_table(tmp_path):
    target = _append_target("upsert", join_cols=["order_id"])
    path = _write(tmp_path, sla={"volume": {"min_rows": 1}}, sinks=["jsonl"], target=target)
    assert _load_rows(tmp_path, 5) == 0
    [first] = evaluate(Config.from_json(path), NOW)
    assert (first.status, first.metrics["observed"]) == ("ok", 5)
    assert _load_rows(tmp_path, 5) == 0  # the same batch again changes nothing
    [again] = evaluate(Config.from_json(path), NOW)
    assert (again.status, again.metrics["observed"]) == ("error", 0)
    assert again.details == "the latest run wrote 0 rows, fewer than the 1 row that min_rows asks for"
    assert _catalog(tmp_path).load_table("shop.orders").scan().to_arrow().num_rows == 5


def test_a_run_that_found_its_key_published_or_was_blocked_is_not_the_latest_run(tmp_path):
    checks = [{"check": "row_count", "min": 3}]
    path = _write(tmp_path, sla={"volume": {"min_rows": 1}}, sinks=["jsonl"], target=_append_target())
    assert _load_rows(tmp_path, 8, key="k1") == 0
    assert _load_rows(tmp_path, 8, key="k1") == 0  # the key is published already, so nothing is written
    data = json.loads(path.read_text())
    data["metadata"]["quality"] = checks
    path.write_text(json.dumps(data))
    assert _load_rows(tmp_path, 2) == 3  # a quality check holds the run back
    [result] = evaluate(Config.from_json(path), NOW)
    assert (result.status, result.metrics["observed"]) == ("ok", 8)
    events, where = run_events(Config.from_json(path))
    assert where.endswith("events.jsonl") and (events, where) == load_run_events(path)


def test_ldp_monitor_prints_a_line_for_each_volume_rule_and_exits_with_2_for_a_drop(tmp_path, capsys):
    path = _write(tmp_path, sla={"volume": VOLUME}, sinks=["jsonl"])
    _history(tmp_path, [100, 102, 98, 101, 99, 100, 10])
    code, out, err = _ldp(["monitor", str(path), "--now", NOW.isoformat()], capsys)
    assert (code, err) == (2, "")
    names = [line.split()[2] for line in out.splitlines() if line.startswith("orders ")]
    assert names == ["volume.rows", "volume.change"]
    assert "2 error" in out
    _history(tmp_path, [100] * 7, start=8)  # seven normal runs after it
    code, out, _ = _ldp(["monitor", str(path), "--now", NOW.isoformat(), "--json"], capsys)
    assert code == 0 and [result["status"] for result in json.loads(out)] == ["ok", "ok"]


def test_the_history_of_a_volume_monitor_is_read_back_by_name(tmp_path):
    path = _write(tmp_path, sla={"volume": VOLUME}, sinks=["jsonl"])
    _history(tmp_path, [100, 100, 100, 100, 100])
    config = Config.from_json(path)
    record([(config, evaluate(config, NOW))])
    assert [result.monitor for result in read_results(config)] == ["volume.change", "volume.rows"]
    assert [result.status for result in read_results(config, monitor="volume.change")] == ["ok"]


def test_ldp_plan_warns_when_a_volume_sla_has_no_history_to_read(tmp_path):
    def notes(**options):
        return plan(_write(tmp_path, sla={"volume": VOLUME, **options.pop("sla", {})}, **options))["notes"]

    unwatched = notes()
    assert unwatched == [
        "the SLA has a volume section, but the config has no iceberg or jsonl sink, so ldp monitor can only read the "
        "rows of earlier runs from the snapshots of the table, and expiring snapshots removes them"]
    assert notes(sinks=["jsonl"]) == [] and notes(sinks=["iceberg"]) == []
    assert notes(sinks=["null"]) == unwatched
    assert plan(_write(tmp_path, sla={"freshness": FRESHNESS}))["notes"] == []


def test_ldp_plan_warns_when_the_lookback_leaves_too_few_earlier_runs(tmp_path):
    sla = {"volume": {"max_change_pct": 20, "lookback_runs": 3}}
    [note] = plan(_write(tmp_path, sla=sla, sinks=["jsonl"]))["notes"]
    assert note == "volume.lookback_runs is 3, which leaves fewer than 3 earlier runs, so the change rule never reports"
    ok = {"volume": {"max_change_pct": 20, "lookback_runs": 4}}
    assert plan(_write(tmp_path, sla=ok, sinks=["jsonl"]))["notes"] == []
    assert plan(_write(tmp_path, sla={"volume": {"min_rows": 1, "lookback_runs": 2}}, sinks=["jsonl"]))["notes"] == []
