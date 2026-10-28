"""The schema monitor: columns that were added, removed or changed in a table or in its source."""

import datetime as dt
import json
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.types import LongType, StringType

from local_data_platform import Config
from local_data_platform.catalog.provider import create_catalog
from local_data_platform.cli import main
from local_data_platform.events import RunEvent
from local_data_platform.monitoring import MonitorResult, evaluate
from local_data_platform.monitoring.history import read_results, record
from local_data_platform.monitoring.schema import ADDED, CHANGED, OTHER, REMOVED, WIDENED, Change, diff, widens

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _write(tmp_path: Path, schema: dict | None = None, *, sinks=("jsonl",), checks=None, target=None) -> Path:
    metadata = {"source": {"name": "orders", "format": "CSV", "path": "../orders.csv"},
                "target": target or {"name": "orders", "format": "ICEBERG", "write_mode": "append",
                                     "catalog": {"identifier": "shop", "warehouse_path": "../warehouse"}},
                "sla": {"schema": schema or {}}}
    if sinks:
        metadata["observability"] = {"sinks": list(sinks)}
    if checks:
        metadata["quality"] = checks
    folder = tmp_path / "config"
    folder.mkdir(exist_ok=True)
    path = folder / "orders.json"
    path.write_text(json.dumps({"identifier": "orders", "who": "ops", "metadata": metadata}))
    return path


def _load(tmp_path: Path, path: Path, header: str, *rows: str) -> int:
    """Write a CSV with a header and rows, and run the config on it. Returns the exit status of ``ldp run``."""
    (tmp_path / "orders.csv").write_text("\n".join([header, *rows]) + "\n")
    return main(["run", str(path)])


def _look(path: Path, minutes: int = 0) -> MonitorResult:
    """Evaluate the schema monitor ``minutes`` after NOW, record the result, and return it."""
    config = Config.from_json(path)
    [result] = evaluate(config, NOW + dt.timedelta(minutes=minutes))
    record([(config, [result])])
    return result


def _catalog(tmp_path: Path):
    return create_catalog({"identifier": "shop", "warehouse_path": str(tmp_path / "warehouse")})


def _table(tmp_path: Path, schema: pa.Schema):
    catalog = _catalog(tmp_path)
    catalog.create_namespace_if_not_exists("shop")
    catalog.create_table("shop.orders", schema=schema)
    return catalog.load_table("shop.orders")


def _changes(result: MonitorResult) -> list[tuple]:
    return [(item["where"], item["change"], item.get("column"), item["allowed"]) for item in result.metrics["changes"]]


# ---------------------------------------------------------------------- the baseline


def test_the_first_look_records_a_baseline_and_a_look_with_no_change_is_ok(tmp_path, capsys):
    path = _write(tmp_path, sinks=())
    assert _load(tmp_path, path, "order_id,amount,note", "1,10.5,a", "2,20.0,b") == 0
    first = _look(path)
    assert (first.status, first.observed, first.monitor, first.kind) == ("ok", "baseline", "schema", "schema")
    assert first.details == ("baseline recorded for the 3 columns of the table, so there is nothing to compare them "
                             "with yet")
    assert first.metrics["columns"] == {"order_id": "long", "amount": "double", "note": "string"}
    assert first.metrics["fingerprint"].startswith("sha256:") and first.metrics["baseline"] is True
    assert "changes" not in first.metrics and first.expected == "warn"
    second = _look(path, 1)
    assert (second.status, second.observed) == ("ok", "no change")
    assert second.details == "the 3 columns of the table are as the last evaluation found them"
    assert second.metrics["previous_fingerprint"] == first.metrics["fingerprint"] == second.metrics["fingerprint"]
    assert "baseline" not in second.metrics
    capsys.readouterr()


def test_the_baseline_is_the_latest_result_at_or_before_the_moment_of_the_look(tmp_path):
    path = _write(tmp_path, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path, 10)
    assert _look(path, 0).observed == "baseline", "a result of a later moment is not a baseline"
    assert _look(path, 20).observed == "no change"


def test_a_look_at_the_moment_of_the_baseline_compares_with_it(tmp_path):
    path = _write(tmp_path, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    assert _look(path).observed == "baseline"
    assert _look(path).observed == "no change"


def test_a_baseline_with_no_fingerprint_is_compared_by_its_columns_only(tmp_path):
    path = _write(tmp_path, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    old = MonitorResult(monitor="schema", kind="schema", status="ok", evaluated_at=NOW, pipeline="orders",
                        metrics={"columns": {"order_id": "long", "amount": "double"}})
    record([(Config.from_json(path), [old])])
    result = _look(path, 1)
    assert (result.status, result.observed) == ("ok", "no change")


def test_a_look_that_had_no_table_leaves_no_baseline(tmp_path):
    path = _write(tmp_path, sinks=())
    before = _look(path)
    assert (before.status, before.observed) == ("unknown", None)
    assert before.details == "the table does not exist yet, so it has no schema"
    assert before.metrics == {"reason": "the catalog does not exist yet"} and not (tmp_path / "warehouse").exists()
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    assert _look(path, 1).observed == "baseline"


def test_a_target_that_is_a_file_has_no_schema_to_look_at(tmp_path):
    target = {"name": "out", "format": "CSV", "path": "../out.csv"}
    result = _look(_write(tmp_path, sinks=(), target=target))
    assert (result.status, result.details) == ("unknown", "the target is a CSV file, not an Iceberg table")


# ---------------------------------------------------------------------- the table


def test_a_column_that_schema_evolution_adds_is_reported_once_and_then_the_table_has_moved_on(tmp_path):
    path = _write(tmp_path, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path)
    assert _load(tmp_path, path, "order_id,amount,coupon", "2,5.0,SAVE5") == 0
    added = _look(path, 1)
    assert (added.status, added.observed) == ("warn", "1 change")
    assert added.details == "the table gained the column coupon (string)"
    assert added.metrics["changes"] == [{"where": "table", "change": "added_column", "column": "coupon",
                                         "to": "string", "allowed": False}]
    assert added.metrics["columns"]["coupon"] == "string"
    assert _look(path, 2).status == "ok", "the change is reported once, when it was first seen"


@pytest.mark.parametrize("on_change, status, note", [
    ("ignore", "ok", " (ignored)"), ("warn", "warn", ""), ("error", "error", "")])
def test_on_change_says_what_a_change_that_is_not_allowed_is(tmp_path, on_change, status, note):
    path = _write(tmp_path, {"on_change": on_change}, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path)
    assert _load(tmp_path, path, "order_id,amount,coupon", "2,5.0,SAVE5") == 0
    result = _look(path, 1)
    assert result.status == status and result.details == f"the table gained the column coupon (string){note}"
    assert result.expected == on_change


def test_a_change_that_allow_lists_stays_ok_and_says_so(tmp_path):
    path = _write(tmp_path, {"on_change": "error", "allow": ["added_column"]}, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path)
    assert _load(tmp_path, path, "order_id,amount,coupon", "2,5.0,SAVE5") == 0
    result = _look(path, 1)
    assert (result.status, result.observed) == ("ok", "1 change")
    assert result.details == "the table gained the column coupon (string) (allowed)"
    assert _changes(result) == [("table", ADDED, "coupon", True)]
    assert result.expected == "error except added_column"


def test_a_removed_column_is_never_allowed(tmp_path):
    path = _write(tmp_path, {"on_change": "error", "allow": ["added_column", "widened_type"]}, sinks=())
    assert _load(tmp_path, path, "order_id,amount,note", "1,10.5,a") == 0
    _look(path)
    table = _catalog(tmp_path).load_table("shop.orders")
    with table.update_schema() as update:
        update.delete_column("note")
    result = _look(path, 1)
    assert (result.status, result.details) == ("error", "the table lost the column note")
    assert _changes(result) == [("table", REMOVED, "note", False)]
    assert result.metrics["columns"] == {"order_id": "long", "amount": "double"}


@pytest.mark.parametrize("allow, status", [([], "warn"), (["added_column"], "warn"), (["widened_type"], "ok")])
def test_a_type_that_becomes_wider_is_a_change_that_allow_can_list(tmp_path, allow, status):
    path = _write(tmp_path, {"allow": allow}, sinks=())
    table = _table(tmp_path, pa.schema([("order_id", pa.int64()), ("quantity", pa.int32()), ("price", pa.float32())]))
    assert _look(path).observed == "baseline"
    with table.update_schema() as update:
        update.update_column("quantity", field_type=LongType())
    result = _look(path, 1)
    assert result.status == status
    assert result.details.startswith("the type of quantity in the table became wider, from int to long")
    assert _changes(result) == [("table", WIDENED, "quantity", "widened_type" in allow)]


def test_a_column_that_comes_back_as_another_type_is_a_change_that_is_not_a_widening(tmp_path):
    path = _write(tmp_path, {"allow": ["added_column", "widened_type"]}, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path)
    catalog = _catalog(tmp_path)
    with catalog.load_table("shop.orders").update_schema() as update:
        update.delete_column("amount")
    with catalog.load_table("shop.orders").update_schema() as update:
        update.add_column("amount", StringType())
    result = _look(path, 1)
    assert (result.status, result.details) == ("warn", "the type of amount in the table changed from double to string")
    assert _changes(result) == [("table", CHANGED, "amount", False)]


def test_a_change_of_the_schema_that_is_no_column_and_no_type_is_a_change_too(tmp_path):
    path = _write(tmp_path, sinks=())
    fields = [pa.field("order_id", pa.int64(), nullable=False), pa.field("amount", pa.float64())]
    table = _table(tmp_path, pa.schema(fields))
    first = _look(path)
    assert first.metrics["columns"] == {"order_id": "long", "amount": "double"}
    with table.update_schema() as update:
        update.make_column_optional("order_id")
    result = _look(path, 1)
    assert result.status == "warn" and _changes(result) == [("table", OTHER, None, False)]
    assert result.details == ("the schema of the table changed in something other than a column or a type, such "
                              "as nullability")
    assert result.metrics["fingerprint"] != result.metrics["previous_fingerprint"]


# ---------------------------------------------------------------------- the source


def test_a_source_that_drops_a_column_and_fails_the_run_is_reported(tmp_path):
    path = _write(tmp_path, checks=[{"check": "row_count", "min": 2}])
    assert _load(tmp_path, path, "order_id,amount,note", "1,10.5,a", "2,20.0,b") == 0
    first = _look(path)
    assert first.observed == "baseline", "one run has no run before it to compare the source with"
    assert _load(tmp_path, path, "order_id,note", "3,c") == 3, "the check holds the run back"
    result = _look(path, 1)
    assert (result.status, result.observed) == ("warn", "1 change")
    assert result.details == "the source lost the column amount in its latest run"
    assert _changes(result) == [("source", REMOVED, "amount", False)]
    assert result.metrics["columns"] == first.metrics["columns"], "the table did not change"


def test_a_source_that_gains_a_column_is_reported_with_the_table_and_stays_until_the_next_run(tmp_path):
    path = _write(tmp_path)
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path)
    assert _load(tmp_path, path, "order_id,amount,coupon", "2,5.0,SAVE5") == 0
    both = _look(path, 1)
    assert both.observed == "2 changes"
    assert both.details == ("the table gained the column coupon (string), "
                            "the source gained the column coupon (string) in its latest run")
    assert _changes(both) == [("table", ADDED, "coupon", False), ("source", ADDED, "coupon", False)]
    later = _look(path, 2)
    assert (later.status, later.details) == ("warn", "the source gained the column coupon (string) in its latest run")
    assert _load(tmp_path, path, "order_id,amount,coupon", "3,6.0,SAVE6") == 0
    assert _look(path, 3).status == "ok", "a run that has the columns of the run before it ends the report"


def test_an_allowed_addition_is_allowed_in_the_table_and_in_the_source(tmp_path):
    path = _write(tmp_path, {"on_change": "error", "allow": ["added_column"]})
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    _look(path)
    assert _load(tmp_path, path, "order_id,amount,coupon", "2,5.0,SAVE5") == 0
    result = _look(path, 1)
    assert result.status == "ok" and all(item["allowed"] for item in result.metrics["changes"])
    assert len(result.metrics["changes"]) == 2 and result.details.count("(allowed)") == 2


def test_the_source_of_a_table_that_was_never_written_is_still_looked_at(tmp_path):
    path = _write(tmp_path, checks=[{"check": "row_count", "min": 99}])
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 3
    assert _load(tmp_path, path, "order_id", "2") == 3
    result = _look(path)
    assert (result.status, result.details) == ("warn", "the source lost the column amount in its latest run")
    assert "columns" not in result.metrics and result.table_uuid is None
    assert list(_catalog(tmp_path).list_tables("shop")) == [], "the runs wrote no table, and the monitor makes none"
    assert _load(tmp_path, path, "order_id", "3") == 3
    same = _look(path, 1)
    assert (same.status, same.observed) == ("ok", "no change")
    assert same.details == "the source of its latest run has the same columns as the run before it"


def test_the_last_two_runs_are_the_last_two_in_time_and_not_in_the_file(tmp_path):
    path = _write(tmp_path)
    # the run ids are not in the order of time either, so neither the file nor the ids give the last two
    runs = {"run-b": (1, ["order_id", "amount"]), "run-a": (2, ["order_id", "amount", "coupon"]),
            "run-c": (3, ["order_id", "amount"])}
    lines = []
    for run_id in ("run-a", "run-b", "run-c"):
        minute, names = runs[run_id]
        schema = [{"name": name, "type": "string"} for name in names]
        event = RunEvent(event_id=f"event-{run_id}", type="run.extracted", schema_version=1,
                         ts=NOW + dt.timedelta(minutes=minute), run_id=run_id, attempt=1, seq=1,
                         payload={"pipeline": "orders", "table": "shop.orders", "rows_read": 1, "schema": schema})
        lines.append(event.to_json())
    folder = tmp_path / "config" / ".ldp"
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text("\n".join(lines) + "\n")
    result = _look(path)
    assert (result.status, result.details) == ("warn", "the source lost the column coupon in its latest run")


def test_a_config_with_no_history_has_no_source_to_compare(tmp_path):
    path = _write(tmp_path, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    assert _load(tmp_path, path, "order_id", "2") == 0
    assert _look(path).observed == "baseline"


# ---------------------------------------------------------------------- ldp monitor


def test_ldp_monitor_records_the_baseline_and_exits_with_2_for_a_change_that_is_an_error(tmp_path, capsys):
    path = _write(tmp_path, {"on_change": "error"}, sinks=())
    assert _load(tmp_path, path, "order_id,amount", "1,10.5") == 0
    capsys.readouterr()
    assert main(["monitor", str(path), "--now", NOW.isoformat()]) == 0
    assert "baseline" in capsys.readouterr().out
    assert _load(tmp_path, path, "order_id,amount,coupon", "2,5.0,SAVE5") == 0
    capsys.readouterr()
    assert main(["monitor", str(path), "--now", (NOW + dt.timedelta(minutes=1)).isoformat()]) == 2
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("orders "))
    assert line.split()[2:5] == ["schema", "error", "1"] and "the table gained the column coupon (string)" in line
    results = read_results(Config.from_json(path), monitor="schema")
    assert [result.observed for result in results] == ["baseline", "1 change"]
    assert results[1].metrics["columns"]["coupon"] == "string"


# ---------------------------------------------------------------------- the helpers


@pytest.mark.parametrize("old, new, wider", [
    ("int", "long", True), ("long", "int", False), ("float", "double", True), ("double", "float", False),
    ("int32", "int64", True), ("int64", "int32", False), ("int8", "int64", True), ("halffloat", "double", True),
    ("uint16", "uint64", True), ("decimal(10, 2)", "decimal(12, 2)", True), ("decimal(10, 2)", "decimal(12, 3)", False),
    ("decimal(10, 2)", "decimal(8, 2)", False), ("decimal(10, 2)", "decimal(10, 2)", False),
    ("decimal128(10, 2)", "decimal128(14, 2)", True),
    ("string", "long", False), ("long", "string", False), ("long", "long", False), ("date", "timestamp", False),
])
def test_a_wider_type_is_one_that_holds_every_value_of_the_old_one(old, new, wider):
    assert widens(old, new) is wider


def test_diff_lists_the_added_columns_first_and_then_what_was_removed_or_changed():
    old = {"id": "long", "amount": "double", "note": "string", "qty": "int"}
    new = {"id": "long", "qty": "long", "amount": "string", "coupon": "string", "extra": "int"}
    changes = diff("table", old, new, {"added_column", "widened_type"})
    assert [(c.kind, c.column, c.old, c.new, c.allowed) for c in changes] == [
        (ADDED, "coupon", None, "string", True), (ADDED, "extra", None, "int", True),
        (REMOVED, "note", "string", None, False), (CHANGED, "amount", "double", "string", False),
        (WIDENED, "qty", "int", "long", True)]
    assert diff("table", old, old, set()) == []
    assert Change("source", ADDED, "x", None, "int").to_dict() == {
        "where": "source", "change": "added_column", "column": "x", "to": "int", "allowed": False}
