"""Tests of ``ldp status``: the rows, the quality count, the open alerts, the text, the JSON and the page."""

import datetime as dt
import json
import re
import shlex
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.catalog import provider
from local_data_platform.catalog.provider import create_catalog
from local_data_platform.cli import MODULE_COMMANDS, main
from local_data_platform.events import (
    IcebergSink,
    RunEvent,
    append_line,
    read_iceberg_events,
    run_events,
    run_source,
)
from local_data_platform.exceptions import ConfigError
from local_data_platform.monitoring.configs import is_a_config, load_configs
from local_data_platform.monitoring.evaluation import evaluate
from local_data_platform.monitoring.history import JSONL_PATH, DIRECT, read_iceberg_results, read_results, record
from local_data_platform.monitoring.ledger import Ledger
from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.monitoring.status import (
    COLUMNS,
    HEALTH,
    NO_SLA,
    Run,
    StatusRow,
    _table_size,
    age_text,
    rows,
)
from local_data_platform.monitoring.status_page import _slugs, render

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
SCHEMA = pa.schema([("id", pa.int64()), ("ordered_at", pa.timestamp("us")), ("amount", pa.float64())])
LATE = {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"},
        "column": "ordered_at"}
FILE_CHANNEL = {"type": "file", "path": "alerts.jsonl"}


def _hours_ago(hours: float) -> dt.datetime:
    return NOW - dt.timedelta(hours=hours)


def _catalog(tmp_path: Path):
    return create_catalog({"identifier": "shop", "warehouse_path": str(tmp_path / "warehouse")})


def _table(tmp_path: Path, name: str = "orders", newest_hours_ago: float | None = 2, rows_in_table: int = 2):
    """A table in the lake of ``tmp_path`` whose newest ``ordered_at`` is that many hours before ``NOW``."""
    catalog = _catalog(tmp_path)
    catalog.create_namespace_if_not_exists("shop")
    table = catalog.create_table(f"shop.{name}", schema=SCHEMA)
    if newest_hours_ago is not None:
        newest = (_hours_ago(newest_hours_ago)).replace(tzinfo=None)
        table.append(pa.Table.from_pylist(
            [{"id": number, "ordered_at": newest - dt.timedelta(minutes=number), "amount": 10.0 + number}
             for number in range(rows_in_table)], schema=SCHEMA))
    return catalog.load_table(f"shop.{name}")


def _write(tmp_path: Path, name: str = "orders", *, sla="default", sinks=("jsonl",), alerts=None,
           who: str = "ops team", target: dict | None = None) -> Path:
    folder = tmp_path / "config"
    folder.mkdir(exist_ok=True)
    metadata = {"source": {"name": name, "format": "CSV", "path": f"../{name}.csv"},
                "target": target or {"name": name, "format": "ICEBERG", "write_mode": "overwrite",
                                     "catalog": {"identifier": "shop", "warehouse_path": "../warehouse"}}}
    if sla is not None:
        metadata["sla"] = {"freshness": LATE} if sla == "default" else sla
    observability = {}
    if sinks:
        observability["sinks"] = list(sinks)
    if alerts:
        observability["alerts"] = alerts
    if observability:
        metadata["observability"] = observability
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"identifier": name, "who": who, "metadata": metadata}))
    return path


def _event(name: str, number: int, kind: str, hours_ago: float, *, seq: int = 0, **payload) -> RunEvent:
    return RunEvent(event_id=f"{name}-{number:03d}-{kind}-{seq}", type=kind, schema_version=1,
                    ts=_hours_ago(hours_ago), run_id=f"{name}-run-{number:03d}", attempt=1, seq=seq,
                    payload={"pipeline": name, "table": f"shop.{name}", **payload})


def _run(name: str, number: int, hours_ago: float, status: str = "published", rows_written: int = 100,
         results: list | None = None, rows_read: int | None = None) -> list[RunEvent]:
    """The events of one run that ended with ``status``, with the verdicts of the checks that it ran."""
    events = [_event(name, number, "run.started", hours_ago, mode="overwrite", publish=True),
              _event(name, number, "run.extracted", hours_ago - 0.001, seq=1,
                     rows_read=rows_written if rows_read is None else rows_read)]
    if results is not None:
        failed = sum(1 for result in results if not result["passed"])
        events.append(_event(name, number, "quality.evaluated", hours_ago - 0.002, seq=2, checks_run=len(results),
                             checks_failed=failed, passed=failed == 0, results=results))
    if status == "failed":
        events.append(_event(name, number, "run.failed", hours_ago - 0.003, seq=3, stage="extract",
                             error_type="OSError", message="boom"))
    else:
        events.append(_event(name, number, f"run.{status}", hours_ago - 0.003, seq=3, rows_written=rows_written,
                             snapshot_id=1000 + number))
    events.append(_event(name, number, "run.finished", hours_ago - 0.004, seq=9, status=status,
                         rows_written=rows_written, duration_s=1.5))
    return events


def _with(event: RunEvent, **payload) -> RunEvent:
    """The same event with more in its payload."""
    return RunEvent(event_id=event.event_id, type=event.type, schema_version=event.schema_version, ts=event.ts,
                    run_id=event.run_id, attempt=event.attempt, seq=event.seq, payload={**event.payload, **payload})


def _check(name: str, passed: bool, failing: int = 0) -> dict:
    return {"name": f"{name}(amount)", "column": "amount", "passed": passed, "failing_rows": failing,
            "details": "d", "metrics": {}}


def _history(tmp_path: Path, *runs: list[RunEvent]) -> None:
    """Append the events of the runs to the file of the jsonl sink next to the configs."""
    folder = tmp_path / "config" / ".ldp"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(event.to_json() + "\n" for run in runs for event in run)


def _by_pipeline(found: list[StatusRow]) -> dict[str, StatusRow]:
    return {row.pipeline: row for row in found}


def _ldp(argv: list[str], capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------- the age and the cells


@pytest.mark.parametrize("seconds, text", [
    (None, "-"), (0, "0.0 minute"), (30, "0.5 minute"), (90, "1.5 minute"), (3599.9, "60.0 minute"),
    (3600, "1.0 hour"), (5400, "1.5 hour"), (108360, "30.1 hour"), (172799, "48.0 hour"), (172800, "2.0 day"),
    (291600, "3.4 day")])
def test_an_age_is_in_minutes_under_an_hour_in_hours_under_two_days_and_in_days_after_that(seconds, text):
    assert age_text(seconds) == text


def test_a_run_has_an_age_that_is_never_below_zero_and_a_dict_that_json_can_carry():
    run = Run(run_id="r1", attempt=2, status="published", started_at=_hours_ago(3), rows_read=5, rows_written=4,
              checks="3/4", duration_s=1.25, snapshot_id=7)
    assert run.age(NOW) == 3 * 3600 and run.age(_hours_ago(5)) == 0
    assert run.to_dict(NOW) == {
        "run_id": "r1", "attempt": 2, "status": "published", "started_at": "2026-10-28T09:00:00Z",
        "age_seconds": 10800.0, "rows_read": 5, "rows_written": 4, "checks": "3/4", "duration_s": 1.25,
        "snapshot_id": 7, "failing_checks": []}
    assert json.loads(json.dumps(run.to_dict(NOW)))["status"] == "published"


def test_a_run_ends_when_its_length_says_and_has_no_end_before_it_has_a_length():
    done = Run(run_id="r1", attempt=1, status="published", started_at=_hours_ago(3), duration_s=1.5)
    open_ = Run(run_id="r2", attempt=1, status="running", started_at=_hours_ago(3))

    assert done.finished_at == _hours_ago(3) + dt.timedelta(seconds=1.5) and open_.finished_at is None


def test_a_run_that_took_no_time_ends_when_it_started():
    run = Run(run_id="r1", attempt=1, status="published", started_at=_hours_ago(3), duration_s=0.0)

    assert run.finished_at == _hours_ago(3)


def test_a_run_made_from_a_summary_keeps_the_names_of_its_failing_checks_and_a_summary_without_them_has_none():
    summary = {"run_id": "r1", "attempt": 1, "status": "blocked_quality", "started_at": _hours_ago(1),
               "rows_read": 5, "rows_written": 0, "checks": "1/2", "duration_s": 0.5, "snapshot_id": None}

    named = Run.of({**summary, "failing_checks": ["unique(id)", "range(amount)"]})
    bare = Run.of(summary)

    assert named.failing_checks == ("unique(id)", "range(amount)") and bare.failing_checks == ()
    assert named.to_dict(NOW)["failing_checks"] == ["unique(id)", "range(amount)"]
    assert Run.of({**summary, "failing_checks": None}).failing_checks == ()


def test_the_health_values_are_the_order_of_the_rows_and_the_columns_are_the_ones_of_the_table():
    assert HEALTH == ("error", "warn", "unknown", "ok", NO_SLA)
    assert COLUMNS == ("pipeline", "table", "owner", "last_run", "last_success_age", "rows", "freshness", "volume",
                       "schema", "metrics", "quality_7d", "open_alerts")


# ---------------------------------------------------------------------- the rows


def test_a_row_has_the_last_run_the_last_success_the_size_of_the_table_and_the_sections_of_the_sla(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=2, rows_in_table=3)
    path = _write(tmp_path, sla={"freshness": LATE, "volume": {"min_rows": 50}})
    three = [_check("not_null", True), _check("unique", True), _check("range", False, 1)]
    _history(tmp_path, _run("orders", 1, 30, rows_written=120),
             _run("orders", 2, 5, rows_written=100, rows_read=130, results=three))
    [row] = rows(path, NOW)
    assert (row.pipeline, row.table, row.owner, row.as_of, row.health) == (
        "orders", "shop.orders", "ops team", NOW, "ok")
    assert row.file == str(path)
    assert row.last_run == Run(run_id="orders-run-002", attempt=1, status="published", started_at=_hours_ago(5),
                               rows_read=130, rows_written=100, checks="2/3", duration_s=1.5, snapshot_id=1002,
                               failing_checks=("range(amount)",))
    assert (row.last_run.age(NOW), row.last_success_age_seconds) == (5 * 3600, 5 * 3600)
    assert row.rows == 3 and row.last_commit_age_seconds is not None and row.last_commit_age_seconds >= 0
    assert (row.freshness, row.volume, row.schema, row.metrics) == ("ok", "ok", None, None)
    assert [result.monitor for result in row.results] == ["freshness", "volume.rows"]
    assert row.sla == "freshness warn after 6 hour and error after 26 hour on column ordered_at, volume min 50 rows"
    assert [run.run_id for run in row.recent_runs] == ["orders-run-002", "orders-run-001"]
    assert row.problem is None and row.history_since == _hours_ago(30)


def test_the_rows_come_worst_first_and_then_by_pipeline(tmp_path):
    for name, hours in (("ok_one", 2), ("late_one", 30), ("warn_one", 10), ("late_two", 40), ("ok_two", 1)):
        _table(tmp_path, name, newest_hours_ago=hours)
        _write(tmp_path, name)
    _table(tmp_path, "plain", newest_hours_ago=1)
    _write(tmp_path, "plain", sla=None)
    found = rows(tmp_path / "config", NOW)
    assert [(row.pipeline, row.health) for row in found] == [
        ("late_one", "error"), ("late_two", "error"), ("warn_one", "warn"), ("ok_one", "ok"), ("ok_two", "ok"),
        ("plain", NO_SLA)]


def test_a_run_that_failed_makes_a_pipeline_an_error_and_one_that_a_gate_held_back_a_warning(tmp_path):
    for name in ("broke", "held", "fine", "cascade"):
        _table(tmp_path, name, newest_hours_ago=1)
        _write(tmp_path, name)
    _history(tmp_path, _run("broke", 1, 3, status="failed"), _run("held", 1, 3, status="blocked_quality"),
             _run("fine", 1, 3), _run("cascade", 1, 3, status="upstream_failed"))
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert [(name, found[name].health, found[name].freshness, found[name].last_run.status)
            for name in ("broke", "held", "fine", "cascade")] == [
        ("broke", "error", "ok", "failed"), ("held", "warn", "ok", "blocked_quality"),
        ("fine", "ok", "ok", "published"), ("cascade", "error", "ok", "upstream_failed")]
    assert found["broke"].last_success_age_seconds is None and found["held"].last_success_age_seconds is None


def test_a_pipeline_with_no_sla_whose_last_run_failed_is_an_error_and_one_whose_run_went_well_says_no_sla(tmp_path):
    for name in ("broke", "fine"):
        _table(tmp_path, name, newest_hours_ago=1)
        _write(tmp_path, name, sla=None)
    _history(tmp_path, _run("broke", 1, 3, status="failed"), _run("fine", 1, 3))
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert (found["broke"].health, found["fine"].health) == ("error", NO_SLA)


@pytest.mark.parametrize("status, counts", [
    ("published", True), ("skipped_duplicate", True), ("succeeded", True), ("blocked_quality", False),
    ("failed", False), ("upstream_failed", False)])
def test_the_last_success_is_the_last_run_that_went_well_and_a_skipped_duplicate_counts(tmp_path, status, counts):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    _history(tmp_path, _run("orders", 1, 20), _run("orders", 2, 4, status=status))
    [row] = rows(path, NOW)
    assert row.last_run.status == status
    assert row.last_success_age_seconds == (4 if counts else 20) * 3600


def test_a_run_that_nothing_ended_is_running_and_a_workflow_and_a_release_are_not_runs_of_the_pipeline(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    started = [_event("orders", 3, "run.started", 1)]
    workflow = [_event("daily", 4, "workflow.started", 0.5, tasks=["orders"]),
                _event("daily", 4, "workflow.finished", 0.4, seq=1, status="succeeded", duration_s=2)]
    workflow = [_with(event, pipeline="orders") for event in workflow]
    release = [_event("orders", 5, "release.cut", 0.2, release="r1", snapshot_id=9)]
    _history(tmp_path, _run("orders", 1, 20), _run("orders", 2, 10), started, workflow, release)
    [row] = rows(path, NOW)
    assert [(run.run_id, run.status) for run in row.recent_runs] == [
        ("orders-run-003", "running"), ("orders-run-002", "published"), ("orders-run-001", "published")]
    assert row.last_success_age_seconds == 10 * 3600


def test_a_pipeline_keeps_the_last_20_runs_newest_first_and_a_retried_task_is_its_last_attempt(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    runs = [_run("orders", number, 100 - number) for number in range(1, 26)]
    first = _run("orders", 26, 3, status="failed")
    retry = [RunEvent(event_id=event.event_id + "-retry", type=event.type, schema_version=1, ts=event.ts,
                      run_id=event.run_id, attempt=2, seq=event.seq, payload=event.payload)
             for event in _run("orders", 26, 2.9)]
    _history(tmp_path, *runs, first, retry)
    [row] = rows(path, NOW)
    assert len(row.recent_runs) == 20
    assert [run.run_id for run in row.recent_runs][:3] == ["orders-run-026", "orders-run-025", "orders-run-024"]
    assert (row.recent_runs[0].attempt, row.recent_runs[0].status) == (2, "published")


def test_a_config_with_no_sinks_and_no_sla_shows_the_commit_age_and_the_rows_from_the_table_metadata(tmp_path):
    table = _table(tmp_path, "orders", newest_hours_ago=48, rows_in_table=5)
    committed = dt.datetime.fromtimestamp(table.current_snapshot().timestamp_ms / 1000, tz=UTC)
    path = _write(tmp_path, sla=None, sinks=())
    moment = committed + dt.timedelta(hours=3)
    [row] = rows(path, moment)
    assert (row.health, row.freshness, row.volume, row.schema, row.metrics) == (NO_SLA,) * 5
    assert row.rows == 5 and row.last_commit_age_seconds == pytest.approx(3 * 3600, abs=0.01)
    assert row.last_run is None and row.last_success_age_seconds is None and row.recent_runs == ()
    assert row.history_since is None and row.sla is None and row.results == ()
    assert row.cells()["last_success_age"] == "3.0 hour (commit)" and row.cells()["rows"] == 5
    assert row.cells()["freshness"] == NO_SLA and row.cells()["last_run"] == "-"


def test_an_sla_without_a_section_says_a_dash_for_it_and_a_config_without_an_sla_says_no_sla_for_all(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sla={"freshness": LATE})
    [row] = rows(path, NOW)
    assert (row.freshness, row.volume, row.schema, row.metrics) == ("ok", None, None, None)
    assert [row.cells()[column] for column in ("freshness", "volume", "schema", "metrics")] == ["ok", "-", "-", "-"]
    _write(tmp_path, "plain", sla=None)
    _table(tmp_path, "plain", newest_hours_ago=1)
    plain = _by_pipeline(rows(tmp_path / "config", NOW))["plain"]
    assert [plain.cells()[column] for column in ("freshness", "volume", "schema", "metrics")] == [NO_SLA] * 4


def test_a_section_says_the_worst_of_its_monitors(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sla={"volume": {"min_rows": 50, "max_change_pct": 30}})
    _history(tmp_path, *[_run("orders", number, 10 - number, rows_written=100) for number in range(1, 5)],
             _run("orders", 5, 0.5, rows_written=40))
    [row] = rows(path, NOW)
    assert {result.monitor: result.status for result in row.results} == {
        "volume.rows": "error", "volume.change": "error"}
    assert row.volume == "error" and row.health == "error"
    path = _write(tmp_path, sla={"volume": {"min_rows": 10, "max_change_pct": 30}})
    [row] = rows(path, NOW)
    assert {result.monitor: result.status for result in row.results} == {"volume.rows": "ok", "volume.change": "error"}
    assert row.volume == "error"


def test_the_metrics_section_says_what_its_monitors_say_and_the_other_sections_say_nothing_when_they_are_not_there(
        tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sla={"freshness": LATE, "metrics": [{"metric": "revenue", "grain": "day", "min": 0}]})
    [row] = rows(path, NOW)
    assert [result.monitor for result in row.results] == ["freshness", "metric.revenue.value"]
    assert (row.freshness, row.volume, row.schema, row.metrics) == ("ok", None, None, "error")
    assert row.health == "error" and row.cells()["metrics"] == "error"
    only = rows(_write(tmp_path, sla={"metrics": [{"metric": "revenue", "grain": "day", "min": 0}]}), NOW)[0]
    assert (only.freshness, only.metrics) == (None, "error")


def test_a_target_that_is_a_file_has_no_commit_and_no_rows(tmp_path):
    path = _write(tmp_path, sla=None, sinks=(), target={"name": "orders_csv", "format": "CSV", "path": "../out.csv"})
    [row] = rows(path, NOW)
    assert (row.table, row.rows, row.last_commit_age_seconds, row.health) == ("../out.csv", None, None, NO_SLA)
    assert row.cells()["rows"] == "-" and row.cells()["last_success_age"] == "-"


def test_a_table_that_was_never_written_is_an_error_and_nothing_is_created(tmp_path):
    path = _write(tmp_path)
    [row] = rows(path, NOW)
    assert (row.health, row.freshness, row.rows, row.last_commit_age_seconds) == ("error", "error", None, None)
    assert not (tmp_path / "warehouse").exists()


def test_a_table_with_no_data_has_no_rows_to_count_but_has_a_commit(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=None)
    path = _write(tmp_path, sla=None, sinks=())
    [row] = rows(path, NOW)
    assert row.rows is None and row.last_commit_age_seconds is None


# ---------------------------------------------------------------------- the quality of the last 7 days


def test_the_failed_checks_of_the_last_7_days_are_counted_and_older_ones_are_not(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    bad = [_check("not_null", False, 2), _check("unique", False, 1), _check("range", True)]
    _history(tmp_path, _run("orders", 1, 24 * 8, results=bad), _run("orders", 2, 24 * 6.9, results=bad),
             _run("orders", 3, 24 * 2, results=[_check("not_null", False, 1), _check("unique", True)]),
             _run("orders", 4, 1, results=[_check("not_null", True)]))
    [row] = rows(path, NOW)
    assert row.quality_7d == 3
    assert rows(path, NOW + dt.timedelta(days=3))[0].quality_7d == 1


def test_the_window_includes_its_start_and_stops_at_the_moment_asked_for(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    _history(tmp_path, _run("orders", 1, 24 * 7, results=[_check("not_null", False, 1)]),
             _run("orders", 2, 0.5, results=[_check("not_null", False, 1), _check("unique", False, 1)]))
    assert rows(path, NOW)[0].quality_7d == 3
    assert rows(path, NOW - dt.timedelta(hours=1))[0].quality_7d == 1


def test_a_pipeline_whose_checks_all_passed_has_0_and_one_that_never_ran_a_check_has_none(tmp_path):
    for name in ("clean", "unchecked", "silent"):
        _table(tmp_path, name, newest_hours_ago=1)
        _write(tmp_path, name)
    _history(tmp_path, _run("clean", 1, 3, results=[_check("not_null", True)]), _run("unchecked", 1, 3),
             _run("silent", 1, 3, results=[]))
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert (found["clean"].quality_7d, found["unchecked"].quality_7d, found["silent"].quality_7d) == (0, None, None)
    assert found["clean"].cells()["quality_7d"] == 0 and found["unchecked"].cells()["quality_7d"] == "-"


def test_checks_that_ran_only_before_the_window_make_it_0_and_not_none(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    _history(tmp_path, _run("orders", 1, 24 * 20, results=[_check("not_null", False, 1)]))
    assert rows(path, NOW)[0].quality_7d == 0


@pytest.mark.parametrize("sinks", [("jsonl",), ("iceberg",)])
def test_a_check_that_ran_after_the_moment_asked_for_is_not_evidence_that_checks_ran(tmp_path, sinks):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sinks=sinks)
    config = Config.from_json(path)
    run = _run("orders", 1, 1, results=[_check("not_null", False, 1)])
    if sinks == ("jsonl",):
        _history(tmp_path, run)
    else:
        sink = IcebergSink(config.metadata["target"]["catalog"], config.base_dir)
        for event in run:
            sink.emit(event)
        sink.flush()
    assert rows(path, NOW - dt.timedelta(hours=5))[0].quality_7d is None
    assert rows(path, NOW)[0].quality_7d == 1


def test_a_pipeline_that_never_ran_a_check_has_none_even_when_the_catalog_holds_the_checks_of_others(tmp_path):
    for name in ("checked", "unchecked"):
        _table(tmp_path, name, newest_hours_ago=1)
    configs = [Config.from_json(_write(tmp_path, name, sinks=("iceberg",))) for name in ("checked", "unchecked")]
    sink = IcebergSink(configs[0].metadata["target"]["catalog"], configs[0].base_dir)
    for event in _run("checked", 1, 3, results=[_check("not_null", False, 1)]) + _run("unchecked", 1, 3):
        sink.emit(event)
    sink.flush()
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert (found["checked"].quality_7d, found["unchecked"].quality_7d) == (1, None)


def test_the_quality_table_is_the_truth_for_the_failed_checks_when_the_events_that_carried_them_were_trimmed(
        tmp_path):
    from pyiceberg.expressions import EqualTo

    configs = []
    for name in ("first", "second"):
        _table(tmp_path, name, newest_hours_ago=1)
        configs.append(Config.from_json(_write(tmp_path, name, sinks=("iceberg",))))
    spec, base = configs[0].metadata["target"]["catalog"], configs[0].base_dir
    sink = IcebergSink(spec, base)
    for event in (_run("first", 1, 3, results=[_check("not_null", False, 1)])
                  + _run("second", 1, 3, results=[_check("not_null", False, 1), _check("unique", False, 2)])):
        sink.emit(event)
    sink.flush()
    assert _by_pipeline(rows(tmp_path / "config", NOW))["second"].quality_7d == 2, "the events say so too"
    create_catalog(spec, base_dir=base).load_table("_ldp.runs").delete(EqualTo("type", "quality.evaluated"))
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert (found["first"].quality_7d, found["second"].quality_7d) == (1, 2)


def test_a_pipeline_that_keeps_its_events_in_a_file_is_counted_from_the_file_whatever_its_catalog_holds(tmp_path):
    for name in ("filed", "catalogued"):
        _table(tmp_path, name, newest_hours_ago=1)
    filed = Config.from_json(_write(tmp_path, "filed", sinks=("jsonl",)))
    catalogued = Config.from_json(_write(tmp_path, "catalogued", sinks=("iceberg",)))
    sink = IcebergSink(catalogued.metadata["target"]["catalog"], catalogued.base_dir)
    for event in _run("catalogued", 1, 3, results=[_check("not_null", True)]):
        sink.emit(event)
    sink.flush()
    _history(tmp_path, _run("filed", 1, 4, results=[_check("not_null", False, 1), _check("unique", False, 2)]))
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert (found["filed"].quality_7d, found["catalogued"].quality_7d) == (2, 0)
    assert filed.identifier == "filed"


def test_the_quality_results_table_is_where_the_failed_checks_are_counted_when_the_catalog_has_it(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sinks=("iceberg",))
    config = Config.from_json(path)
    sink = IcebergSink(config.metadata["target"]["catalog"], config.base_dir)
    for run in (_run("orders", 1, 24 * 9, results=[_check("not_null", False, 1)]),
                _run("orders", 2, 4, results=[_check("not_null", False, 1), _check("unique", False, 3)])):
        for event in run:
            sink.emit(event)
    sink.flush()
    [row] = rows(path, NOW)
    assert row.quality_7d == 2
    assert [run.run_id for run in row.recent_runs] == ["orders-run-002", "orders-run-001"]
    assert row.history_since == _hours_ago(24 * 9)


def test_a_catalog_with_runs_and_no_quality_table_counts_the_failed_checks_in_the_events(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sinks=("iceberg",))
    config = Config.from_json(path)
    sink = IcebergSink(config.metadata["target"]["catalog"], config.base_dir)
    for event in _run("orders", 1, 4):
        sink.emit(event)
    sink.flush()
    assert rows(path, NOW)[0].quality_7d is None
    for event in _run("orders", 2, 3, results=[_check("not_null", False, 1)]):
        sink.emit(event)
    sink.flush()
    assert rows(path, NOW)[0].quality_7d == 1


# ---------------------------------------------------------------------- open alerts


def test_a_config_with_no_channel_has_no_count_of_open_alerts_and_one_with_a_channel_has_a_number(tmp_path):
    _table(tmp_path, "quiet", newest_hours_ago=30)
    _write(tmp_path, "quiet")
    _table(tmp_path, "loud", newest_hours_ago=30)
    _write(tmp_path, "loud", alerts=[FILE_CHANNEL])
    _table(tmp_path, "fine", newest_hours_ago=1)
    _write(tmp_path, "fine", alerts=[FILE_CHANNEL])
    _table(tmp_path, "plain", newest_hours_ago=1)
    _write(tmp_path, "plain", sla=None, alerts=[FILE_CHANNEL])
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert [(name, found[name].open_alerts) for name in ("quiet", "loud", "fine", "plain")] == [
        ("quiet", None), ("loud", 1), ("fine", 0), ("plain", 0)]
    assert found["quiet"].cells()["open_alerts"] == "-" and found["loud"].cells()["open_alerts"] == 1


def test_a_warning_is_an_open_alert_only_for_a_channel_that_hears_it(tmp_path):
    _table(tmp_path, "deaf", newest_hours_ago=10)
    _write(tmp_path, "deaf", alerts=[FILE_CHANNEL])
    _table(tmp_path, "hears", newest_hours_ago=10)
    _write(tmp_path, "hears", alerts=[{**FILE_CHANNEL, "on": ["monitor.warn", "monitor.resolved"]}])
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert (found["deaf"].freshness, found["deaf"].open_alerts) == ("warn", 0)
    assert (found["hears"].freshness, found["hears"].open_alerts) == ("warn", 1)


def test_an_error_is_not_an_open_alert_for_a_channel_that_only_hears_warnings(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=30)
    path = _write(tmp_path, alerts=[{**FILE_CHANNEL, "on": ["monitor.warn"]}])
    assert rows(path, NOW)[0].open_alerts == 0


def test_a_monitor_that_says_unknown_keeps_the_alert_that_was_open_until_it_says_something_else(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=None)
    path = _write(tmp_path, sla={"volume": {"min_rows": 50}}, alerts=[FILE_CHANNEL])
    config = Config.from_json(path)
    assert [result.status for result in evaluate(config, NOW)] == ["unknown"]
    assert rows(path, NOW)[0].open_alerts == 0
    earlier = MonitorResult(monitor="volume.rows", kind="volume", status="error", evaluated_at=_hours_ago(2),
                            pipeline="orders", table_identifier="shop.orders")
    record([(config, [earlier])])
    assert rows(path, NOW)[0].open_alerts == 1
    assert rows(path, _hours_ago(3))[0].open_alerts == 0


def test_the_history_since_is_the_oldest_run_event_or_monitor_result_up_to_the_moment_asked_for(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    config = Config.from_json(path)
    _history(tmp_path, _run("orders", 1, 50), _run("orders", 2, 5))
    record([(config, [MonitorResult(monitor="freshness", kind="freshness", status="ok", evaluated_at=_hours_ago(80),
                                    pipeline="orders")])])
    assert rows(path, NOW)[0].history_since == _hours_ago(80)
    assert rows(path, _hours_ago(60))[0].history_since == _hours_ago(80)
    assert rows(path, _hours_ago(90))[0].history_since is None


def test_what_came_after_the_moment_asked_for_is_not_the_last_run(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    _history(tmp_path, _run("orders", 1, 20), _run("orders", 2, 4, status="failed"))
    assert rows(path, NOW)[0].last_run.status == "failed"
    before = rows(path, _hours_ago(10))[0]
    assert (before.last_run.run_id, before.last_run.status) == ("orders-run-001", "published")
    assert before.last_run.age(_hours_ago(10)) == 10 * 3600
    assert rows(path, _hours_ago(30))[0].last_run is None


def test_a_run_that_began_at_the_moment_asked_for_is_the_last_run_and_a_result_made_then_is_history(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    _history(tmp_path, _run("orders", 1, 0))
    [row] = rows(path, NOW)
    assert (row.last_run.run_id, row.last_run.status, row.last_run.age(NOW)) == ("orders-run-001", "running", 0)
    config = Config.from_json(_write(tmp_path, "refunds"))
    _table(tmp_path, "refunds", newest_hours_ago=1)
    record([(config, [MonitorResult(monitor="freshness", kind="freshness", status="ok", evaluated_at=NOW,
                                    pipeline="refunds")])])
    assert _by_pipeline(rows(tmp_path / "config", NOW))["refunds"].history_since == NOW


def test_a_table_size_is_read_from_the_summary_of_its_snapshot_and_a_summary_that_is_no_number_is_no_size():
    class Summary:
        def __init__(self, properties):
            self.additional_properties = properties

    class Snapshot:
        def __init__(self, summary, timestamp_ms=1_000_000):
            self.summary, self.timestamp_ms = summary, timestamp_ms

    class Table:
        def __init__(self, snapshot):
            self._snapshot = snapshot

        def current_snapshot(self):
            return self._snapshot

    moment = dt.datetime.fromtimestamp(1_000_000 / 1000 + 90, tz=UTC)
    assert _table_size(Table(Snapshot(Summary({"total-records": "12"}))), moment) == (90.0, 12)
    assert _table_size(Table(Snapshot(Summary({"total-records": "n/a"}))), moment) == (90.0, None)
    assert _table_size(Table(Snapshot(Summary({}))), moment) == (90.0, None)
    assert _table_size(Table(Snapshot(None)), moment) == (90.0, None)
    assert _table_size(Table(None), moment) == (None, None) and _table_size(None, moment) == (None, None)
    assert _table_size(Table(Snapshot(Summary({"total-records": "3"}))), dt.datetime.fromtimestamp(0, tz=UTC)) == (
        0.0, 3)


# ---------------------------------------------------------------------- a config that cannot be read


def test_a_file_that_is_not_a_config_is_a_row_with_the_reason_and_it_is_an_error_at_the_top(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    _write(tmp_path)
    (tmp_path / "config" / "typo.json").write_text("{not json")
    found = rows(tmp_path / "config", NOW)
    assert [(row.pipeline, row.health) for row in found] == [("typo.json", "error"), ("orders", "ok")]
    assert found[0].table is None and "is not valid JSON" in found[0].problem and found[0].results == ()
    assert found[0].cells()["table"] == "-" and found[0].cells()["last_run"] == "-"


def test_rows_of_the_same_health_are_in_the_order_of_their_names_whether_or_not_the_file_could_be_read(tmp_path):
    _table(tmp_path, "late", newest_hours_ago=40)
    _write(tmp_path, "late")
    (tmp_path / "config" / "aardvark.json").write_text("{nope")
    found = rows(tmp_path / "config", NOW)
    assert [(row.pipeline, row.health) for row in found] == [("aardvark.json", "error"), ("late", "error")]


def test_a_config_that_fails_to_evaluate_is_a_row_with_the_reason_and_the_others_are_still_there(tmp_path, monkeypatch):
    from local_data_platform.monitoring import evaluation

    for name in ("orders", "refunds"):
        _table(tmp_path, name, newest_hours_ago=1)
        _write(tmp_path, name)
    original = evaluation.evaluate

    def flaky(config, now=None, history=None, target=None):
        if config.identifier == "orders":
            raise RuntimeError("the catalog went away")
        return original(config, now, history, target)

    monkeypatch.setattr(evaluation, "evaluate", flaky)
    found = _by_pipeline(rows(tmp_path / "config", NOW))
    assert found["orders"].problem == "RuntimeError: the catalog went away" and found["orders"].health == "error"
    assert (found["orders"].table, found["orders"].owner) == (None, "ops team")
    assert found["refunds"].problem is None and found["refunds"].health == "ok"


def test_a_path_that_is_not_there_and_a_folder_with_no_config_and_a_moment_that_is_no_time_are_errors(tmp_path):
    with pytest.raises(ConfigError, match="config path not found"):
        rows(tmp_path / "nope", NOW)
    (tmp_path / "empty").mkdir()
    with pytest.raises(ConfigError, match="no dataset configs"):
        rows(tmp_path / "empty", NOW)
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    with pytest.raises(ConfigError):
        rows(path, "yesterday-ish")


def test_the_moment_may_be_text_or_a_datetime_and_by_default_it_is_now(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    assert rows(path, "2026-10-28T12:00:00Z")[0].as_of == NOW == rows(path, NOW)[0].as_of
    before = dt.datetime.now(UTC)
    assert before <= rows(path)[0].as_of <= dt.datetime.now(UTC)


# ---------------------------------------------------------------------- the row as cells and as JSON


def test_the_cells_of_a_row_are_the_text_of_its_columns(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1, rows_in_table=4)
    path = _write(tmp_path, sla={"freshness": LATE, "volume": {"min_rows": 10}}, alerts=[FILE_CHANNEL])
    _history(tmp_path, _run("orders", 1, 30),
             _run("orders", 2, 5, status="blocked_quality", results=[_check("not_null", False, 1)]))
    [row] = rows(path, NOW)
    assert list(row.cells()) == list(COLUMNS)
    assert row.cells() == {
        "pipeline": "orders", "table": "shop.orders", "owner": "ops team", "last_run": "blocked_quality 5.0 hour",
        "last_success_age": "30.0 hour", "rows": 4, "freshness": "ok", "volume": "ok", "schema": "-",
        "metrics": "-", "quality_7d": 1, "open_alerts": 0}


def test_a_zero_is_a_number_in_a_cell_and_only_a_missing_value_is_a_dash():
    zeros = _plain_row(rows=0, quality_7d=0, open_alerts=0).cells()
    assert (zeros["rows"], zeros["quality_7d"], zeros["open_alerts"]) == (0, 0, 0)
    missing = _plain_row(rows=None, quality_7d=None, open_alerts=None).cells()
    assert (missing["rows"], missing["quality_7d"], missing["open_alerts"]) == ("-", "-", "-")


def test_a_row_is_json_with_ages_in_seconds_and_times_in_utc(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, alerts=[FILE_CHANNEL])
    _history(tmp_path, _run("orders", 1, 5))
    [row] = rows(path, NOW)
    data = json.loads(json.dumps(row.to_dict()))
    assert data["as_of"] == "2026-10-28T12:00:00Z" and data["history_since"] == "2026-10-28T07:00:00Z"
    assert data["last_run"]["age_seconds"] == 5 * 3600 and data["health"] == "ok"
    assert data["last_success_age_seconds"] == pytest.approx(5 * 3600, abs=0.01)
    assert [result["monitor"] for result in data["results"]] == ["freshness"]
    assert data["recent_runs"][0]["run_id"] == "orders-run-001" and data["problem"] is None
    assert set(data) == {"pipeline", "table", "owner", "file", "as_of", "health", "last_run",
                         "last_success_age_seconds", "last_commit_age_seconds", "rows", "freshness", "volume", "schema",
                         "metrics", "quality_7d", "open_alerts", "history_since", "sla", "results", "recent_runs",
                         "problem"}


# ---------------------------------------------------------------------- the ledger


class _Counting:
    """A catalog that counts the tables that are loaded from it."""

    loads: Counter = Counter()

    def __init__(self, catalog):
        self._catalog = catalog

    def load_table(self, identifier):
        _Counting.loads[identifier if isinstance(identifier, str) else ".".join(identifier)] += 1
        return self._catalog.load_table(identifier)

    def __getattr__(self, name):
        return getattr(self._catalog, name)


@pytest.fixture
def counting(monkeypatch):
    """Every table that any code loads from a catalog is counted, by its identifier."""
    real = provider.create_catalog
    _Counting.loads = Counter()
    monkeypatch.setattr(provider, "create_catalog", lambda *args, **kwargs: _Counting(real(*args, **kwargs)))
    return _Counting.loads


def _shared_lake(tmp_path: Path, count: int) -> list[Config]:
    """``count`` pipelines that share one catalog, each with a table, runs and quality results in ``_ldp``."""
    sla = {"freshness": LATE, "volume": {"min_rows": 1}}
    configs = []
    for number in range(count):
        name = f"pipe_{number:02d}"
        _table(tmp_path, name, newest_hours_ago=1 + number)
        configs.append(Config.from_json(_write(tmp_path, name, sinks=("iceberg",), sla=sla)))
    sink = IcebergSink(configs[0].metadata["target"]["catalog"], configs[0].base_dir)
    for config in configs:
        for event in _run(config.identifier, 1, 20, results=[_check("not_null", True)]):
            sink.emit(event)
        for event in _run(config.identifier, 2, 3, results=[_check("not_null", False, 2), _check("unique", True)]):
            sink.emit(event)
    sink.flush()
    record([(config, evaluate(config, _hours_ago(30))) for config in configs])
    return configs


def test_each_ldp_table_is_read_once_for_twenty_configs_that_share_a_catalog(tmp_path, counting):
    configs = _shared_lake(tmp_path, 20)
    counting.clear()
    found = rows(tmp_path / "config", NOW)
    assert len(found) == 20
    assert (counting["_ldp.runs"], counting["_ldp.quality_results"], counting["_ldp.monitor_results"]) == (1, 1, 1)
    assert all(counting[f"shop.pipe_{number:02d}"] == 1 for number in range(20))
    assert all(row.last_run.status == "published" and row.quality_7d == 1 and len(row.recent_runs) == 2
               for row in found)
    counting.clear()
    for config in configs:
        run_events(config)
        read_results(config)
    assert (counting["_ldp.runs"], counting["_ldp.monitor_results"]) == (20, 20), "one read for each config"


def test_two_catalogs_are_two_reads_of_each_table_and_a_folder_of_them_still_has_every_row(tmp_path, counting):
    for number, namespace in enumerate(("shop", "wholesale", "shop", "wholesale")):
        name = f"pipe_{number}"
        catalog = create_catalog({"identifier": namespace, "warehouse_path": str(tmp_path / f"lake_{namespace}")})
        catalog.create_namespace_if_not_exists(namespace)
        catalog.create_table(f"{namespace}.{name}", schema=SCHEMA)
        target = {"name": name, "format": "ICEBERG", "write_mode": "overwrite",
                  "catalog": {"identifier": namespace, "warehouse_path": f"../lake_{namespace}"}}
        config = Config.from_json(_write(tmp_path, name, sinks=("iceberg",), target=target, sla=None))
        sink = IcebergSink(target["catalog"], config.base_dir)
        for event in _run(name, 1, 3):
            sink.emit(event)
        sink.flush()
    counting.clear()
    found = rows(tmp_path / "config", NOW)
    assert sorted(row.pipeline for row in found) == ["pipe_0", "pipe_1", "pipe_2", "pipe_3"]
    assert counting["_ldp.runs"] == 2 and all(row.last_run is not None for row in found)


def _lake_of_every_kind(tmp_path: Path) -> list[Config]:
    """A jsonl sink alone, an iceberg sink, no sink with a catalog that has ``_ldp``, and both sinks."""
    configs = []
    for name, sinks in (("only_file", ("jsonl",)), ("in_catalog", ("iceberg",)), ("no_sink", ()),
                        ("both", ("jsonl", "iceberg")), ("file_with_empty_catalog", ("jsonl",)),
                        ("iceberg_empty", ("iceberg", "jsonl")), ("catalog_and_file", ("jsonl",))):
        _table(tmp_path, name, newest_hours_ago=1)
        configs.append(Config.from_json(_write(tmp_path, name, sinks=sinks)))
    sink = IcebergSink(configs[1].metadata["target"]["catalog"], configs[1].base_dir)
    for name in ("in_catalog", "both", "catalog_and_file"):
        for event in _run(name, 1, 6, results=[_check("not_null", False, 1)]) + _run(name, 2, 2):
            sink.emit(event)
    sink.flush()
    nameless = RunEvent(event_id="nameless", type="run.started", schema_version=1, ts=_hours_ago(9), run_id="nameless",
                        attempt=1, seq=0, payload={})
    _history(tmp_path, _run("only_file", 1, 7), _run("file_with_empty_catalog", 1, 8), _run("both", 9, 1),
             _run("no_sink", 1, 6), _run("iceberg_empty", 1, 5), _run("catalog_and_file", 3, 4), [nameless])
    record([(config, evaluate(config, _hours_ago(9))) for config in configs[:2]])
    return configs


def test_the_ledger_answers_as_the_readers_of_one_config_do_for_every_place_that_events_can_be(tmp_path):
    configs = _lake_of_every_kind(tmp_path)
    ledger = Ledger(configs)
    for config in configs:
        assert ledger.run_events(config) == run_events(config)[0], config.identifier
        assert ledger.monitor_results(config) == read_results(config), config.identifier
        assert ledger.monitor_results(config, "freshness") == read_results(config, monitor="freshness")
        assert ledger.monitor_results(config, "nothing") == []
    assert ledger.monitor_results(configs[0], "freshness") != [] and ledger.monitor_results(configs[0]) != []
    assert {event.run_id for event in ledger.run_events(configs[6])} >= {"catalog_and_file-run-001"}
    assert "catalog_and_file-run-003" not in {event.run_id for event in ledger.run_events(configs[6])}
    assert {event.run_id for event in ledger.run_events(configs[4])} == {"file_with_empty_catalog-run-001", "nameless"}
    assert len(ledger.run_events(configs[4])) == 5, "the file, because the catalog has nothing of this pipeline"


def test_two_lakes_with_the_same_catalog_spec_are_two_catalogs(tmp_path):
    configs = []
    for name in ("alpha", "beta"):
        lake = tmp_path / name
        lake.mkdir()
        _table(lake, name, newest_hours_ago=1)
        config = Config.from_json(_write(lake, name, sinks=("iceberg",)))
        sink = IcebergSink(config.metadata["target"]["catalog"], config.base_dir)
        for event in _run(name, 1, 3):
            sink.emit(event)
        sink.flush()
        configs.append(config)
    assert configs[0].metadata["target"]["catalog"] == configs[1].metadata["target"]["catalog"]
    ledger = Ledger(configs)
    assert {event.run_id for event in ledger.run_events(configs[0])} == {"alpha-run-001"}
    assert {event.run_id for event in ledger.run_events(configs[1])} == {"beta-run-001"}


def test_the_results_of_the_file_and_of_the_table_are_one_history_in_the_order_of_time(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    config = Config.from_json(_write(tmp_path, sinks=("iceberg",)))

    def result(hours: float, status: str) -> MonitorResult:
        return MonitorResult(monitor="freshness", kind="freshness", status=status, evaluated_at=_hours_ago(hours),
                             pipeline="orders")

    file = config.resolve(JSONL_PATH)
    file.parent.mkdir(parents=True, exist_ok=True)
    for hours in (30, 10):
        append_line(file, json.dumps(result(hours, "ok").to_dict()))
    record([(config, [result(20, "warn")])])
    found = Ledger([config]).monitor_results(config)
    assert [(item.evaluated_at, item.status) for item in found] == [
        (_hours_ago(30), "ok"), (_hours_ago(20), "warn"), (_hours_ago(10), "ok")]
    assert found == read_results(config)


def test_a_pipeline_whose_events_are_in_a_file_does_not_see_those_of_another_pipeline_that_share_it(tmp_path):
    configs = _lake_of_every_kind(tmp_path)
    ledger = Ledger(configs)
    assert {event.payload.get("pipeline") for event in ledger.run_events(configs[0])} == {"only_file", None}
    assert ledger.run_events(configs[2]) == [], "a catalog that has the table and no jsonl sink is all there is"
    assert ledger.run_events(configs[5]) == [], "an iceberg sink says that the events are in the catalog"
    assert {event.run_id for event in ledger.run_events(configs[3])} >= {"both-run-001", "both-run-002"}


def test_the_ledger_reads_again_for_a_config_that_it_was_not_built_with(tmp_path, counting):
    configs = _shared_lake(tmp_path, 3)
    counting.clear()
    ledger = Ledger(configs[:2])
    assert len(ledger.run_events(configs[0])) == 10 and counting["_ldp.runs"] == 1
    assert len(ledger.run_events(configs[2])) == 10 and counting["_ldp.runs"] == 2
    assert len(ledger.run_events(configs[1])) == 10 and counting["_ldp.runs"] == 2


def test_a_config_with_sinks_that_are_not_valid_does_not_keep_the_ledger_from_serving_the_others(tmp_path):
    configs = _shared_lake(tmp_path, 2)
    bad = Config.from_dict({"identifier": "bad", "who": "x", "metadata": {
        **configs[0].metadata, "observability": {"sinks": ["nope"]}}}, base_dir=configs[0].base_dir)
    ledger = Ledger([bad, *configs])
    assert len(ledger.run_events(configs[0])) == 10
    with pytest.raises(ConfigError):
        ledger.run_events(bad)


def test_the_ledger_creates_nothing_and_a_catalog_that_is_not_there_has_no_history(tmp_path):
    path = _write(tmp_path, sinks=("iceberg",))
    config = Config.from_json(path)
    ledger = Ledger([config], since=NOW)
    assert ledger.run_events(config) == [] and ledger.monitor_results(config) == []
    assert ledger.failed_checks(config, NOW) is None
    assert sorted(item.name for item in tmp_path.rglob("*")) == ["config", "orders.json"]


def _failed_check_at(moment: dt.datetime, number: int) -> RunEvent:
    return RunEvent(event_id=f"q-{number}", type="quality.evaluated", schema_version=1, ts=moment,
                    run_id=f"orders-run-{number:03d}", attempt=1, seq=2,
                    payload={"pipeline": "orders", "table": "shop.orders", "checks_run": 1, "checks_failed": 1,
                             "passed": False, "results": [_check("not_null", False, 1)]})


@pytest.mark.parametrize("sinks", [("jsonl",), ("iceberg",)])
def test_a_check_that_ran_at_the_start_of_the_window_or_at_its_end_counts_and_one_outside_does_not(tmp_path, sinks):
    _table(tmp_path, "orders", newest_hours_ago=1)
    config = Config.from_json(_write(tmp_path, sinks=sinks))
    since, until = _hours_ago(48), _hours_ago(2)
    events = [_failed_check_at(moment, number) for number, moment in enumerate(
        (since - dt.timedelta(seconds=1), since, until, until + dt.timedelta(seconds=1)))]
    if sinks == ("jsonl",):
        _history(tmp_path, events)
    else:
        sink = IcebergSink(config.metadata["target"]["catalog"], config.base_dir)
        for event in events:
            sink.emit(event)
        sink.flush()
    assert Ledger([config], since=since).failed_checks(config, until) == 2
    assert Ledger([config]).failed_checks(config, until) == 3


def test_the_window_of_the_ledger_starts_where_it_was_told_to_and_ends_at_the_moment_asked_for(tmp_path):
    configs = _shared_lake(tmp_path, 1)
    config = configs[0]
    assert Ledger(configs, since=_hours_ago(24)).failed_checks(config, NOW) == 1
    assert Ledger(configs, since=_hours_ago(3)).failed_checks(config, NOW) == 1
    assert Ledger(configs, since=_hours_ago(2.9)).failed_checks(config, NOW) == 0
    assert Ledger(configs).failed_checks(config, NOW) == 1
    assert Ledger(configs, since=_hours_ago(24)).failed_checks(config, _hours_ago(5)) == 0
    assert Ledger(configs, since=_hours_ago(24)).failed_checks(config, _hours_ago(2.9)) == 1


# ---------------------------------------------------------------------- where the history is kept


def test_the_events_of_a_config_are_where_its_sinks_say_and_where_its_catalog_has_them(tmp_path):
    _table(tmp_path, "orders", newest_hours_ago=1)
    jsonl = run_source(Config.from_json(_write(tmp_path, "orders", sinks=("jsonl",))))
    assert (jsonl.catalog, jsonl.namespace, jsonl.iceberg, jsonl.jsonl_listed) == (
        {"identifier": "shop", "warehouse_path": "../warehouse"}, "_ldp", False, True)
    assert jsonl.jsonl == tmp_path / "config" / ".ldp" / "events.jsonl"
    iceberg = run_source(Config.from_json(_write(tmp_path, "orders", sinks=("iceberg",))))
    assert (iceberg.iceberg, iceberg.jsonl_listed, iceberg.namespace) == (True, False, "_ldp")
    custom = Config.from_json(_write(tmp_path, "orders", sinks=(), sla=None))
    custom.metadata["observability"] = {"sinks": [{"type": "iceberg", "namespace": "_audit"},
                                                  {"type": "jsonl", "path": "elsewhere/events.jsonl"}]}
    source = run_source(custom)
    assert (source.namespace, source.iceberg, source.jsonl_listed) == ("_audit", True, True)
    assert source.jsonl == tmp_path / "config" / "elsewhere" / "events.jsonl"
    nothing = run_source(Config.from_json(_write(tmp_path, "csv", sinks=(), sla=None, target={
        "name": "out", "format": "CSV", "path": "../out.csv"})))
    assert (nothing.catalog, nothing.iceberg, nothing.jsonl_listed) == (None, False, False)
    assert not nothing.has_catalog(tmp_path)


def test_a_local_catalog_that_does_not_exist_yet_is_not_one_to_ask_and_one_that_exists_is(tmp_path):
    config = Config.from_json(_write(tmp_path, "orders"))
    source = run_source(config)
    assert source.catalog is not None and not source.has_catalog(config.base_dir)
    _table(tmp_path, "orders", newest_hours_ago=1)
    assert source.has_catalog(config.base_dir)


def test_events_are_read_for_the_pipelines_that_are_asked_for_and_for_none_without_a_read(tmp_path, counting):
    configs = _shared_lake(tmp_path, 3)
    catalog = configs[0].metadata["target"]["catalog"]
    base = configs[0].base_dir
    counting.clear()
    both = read_iceberg_events({**catalog, "warehouse_path": str(tmp_path / "warehouse")},
                               pipelines=["pipe_00", "pipe_02", "pipe_unknown"])
    assert {event.payload["pipeline"] for event in both} == {"pipe_00", "pipe_02"} and len(both) == 20
    assert counting["_ldp.runs"] == 1
    assert read_iceberg_events(create_catalog(catalog, base_dir=base), pipelines=[]) == []
    assert counting["_ldp.runs"] == 1, "no pipeline, no read of the table"
    one = read_iceberg_events(create_catalog(catalog, base_dir=base), pipeline="pipe_01",
                              pipelines=["pipe_00", "pipe_01"])
    assert {event.payload["pipeline"] for event in one} == {"pipe_01"}


def test_results_are_read_for_the_pipelines_that_are_asked_for_and_none_when_there_is_no_table(tmp_path):
    configs = _shared_lake(tmp_path, 3)
    catalog, base = configs[0].metadata["target"]["catalog"], configs[0].base_dir
    found = read_iceberg_results(catalog, base, "_ldp", ["pipe_00", "pipe_02", "nobody"])
    assert {result.pipeline for result in found} == {"pipe_00", "pipe_02"}
    assert len(found) == 2 * len(evaluate(configs[0], NOW))
    assert read_iceberg_results(catalog, base, "_ldp", []) == []
    assert read_iceberg_results(catalog, base, "_elsewhere", ["pipe_00"]) == []


def test_no_pipeline_is_no_read_of_a_table_and_a_catalog_with_no_namespace_has_no_history(
        tmp_path, counting, monkeypatch):
    configs = _shared_lake(tmp_path, 1)
    catalog, base = configs[0].metadata["target"]["catalog"], configs[0].base_dir

    class Refusing:
        def load_table(self, identifier):
            raise AssertionError(f"{identifier} was read")

    assert read_iceberg_events(Refusing(), pipelines=[]) == []
    counting.clear()
    assert read_iceberg_results(catalog, base, "_ldp", []) == []
    assert counting["_ldp.monitor_results"] == 0

    from pyiceberg.exceptions import NoSuchNamespaceError

    class NoNamespace:
        def load_table(self, identifier):
            raise NoSuchNamespaceError(identifier)

    monkeypatch.setattr(provider, "create_catalog", lambda *args, **kwargs: NoNamespace())
    assert read_iceberg_results(catalog, base, "_ldp", ["pipe_00"]) == []


def test_the_default_history_reads_one_config_at_a_time_as_the_functions_do(tmp_path):
    configs = _lake_of_every_kind(tmp_path)
    for config in configs:
        assert DIRECT.run_events(config) == run_events(config)[0]
        assert DIRECT.monitor_results(config) == read_results(config)
        assert DIRECT.monitor_results(config, "freshness") == read_results(config, monitor="freshness")
    assert DIRECT.monitor_results(configs[0], "nothing") == []


def test_the_configs_of_a_folder_are_in_the_order_of_their_names_whatever_the_file_system_says(tmp_path, monkeypatch):
    for name in ("b", "a", "c"):
        _write(tmp_path, name)
    real = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter(sorted(real(self, pattern), reverse=True)))
    assert [file.name for file, _ in load_configs(tmp_path / "config")[0]] == ["a.json", "b.json", "c.json"]


def test_the_configs_of_a_path_are_a_file_or_the_dataset_configs_of_a_folder_and_the_files_that_were_not(tmp_path):
    first, second = _write(tmp_path, "first"), _write(tmp_path, "second")
    (tmp_path / "config" / "workflow.json").write_text(json.dumps({"apiVersion": "ldp/v1", "kind": "Workflow"}))
    (tmp_path / "config" / "notes.json").write_text(json.dumps({"hello": "world"}))
    (tmp_path / "config" / "typo.json").write_text("{nope")
    (tmp_path / "config" / "bad.json").write_text(json.dumps({"identifier": "bad", "metadata": {}}))
    (tmp_path / "config" / "daily.json").write_text(json.dumps(
        {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily", "metadata": {}}))
    (tmp_path / "config" / "only_id.json").write_text(json.dumps({"identifier": "orders"}))
    (tmp_path / "config" / "only_metadata.json").write_text(json.dumps({"metadata": {}}))
    configs, failures = load_configs(tmp_path / "config")
    assert [file.name for file, _ in configs] == ["first.json", "second.json"]
    assert [config.identifier for _, config in configs] == ["first", "second"]
    assert [file.name for file, _ in failures] == ["bad.json", "typo.json"]
    assert "is not valid JSON" in dict((file.name, message) for file, message in failures)["typo.json"]
    only, none = load_configs(first)
    assert [file.name for file, _ in only] == ["first.json"] and none == []
    assert load_configs(str(second))[0][0][0] == second.resolve()
    assert is_a_config(first) and not is_a_config(tmp_path / "config" / "workflow.json")
    assert not is_a_config(tmp_path / "config" / "daily.json"), "a file of another kind is left alone"
    assert not is_a_config(tmp_path / "config" / "only_id.json")
    assert not is_a_config(tmp_path / "config" / "only_metadata.json")
    assert not is_a_config(tmp_path / "config" / "notes.json") and is_a_config(tmp_path / "config" / "typo.json")
    assert is_a_config(tmp_path / "config" / "bad.json")


class _Spy:
    """A history that gives nothing and writes down what the monitors ask it for."""

    def __init__(self):
        self.calls = []

    def run_events(self, config):
        self.calls.append(("run_events", config.identifier))
        return []

    def monitor_results(self, config, monitor=None):
        self.calls.append(("monitor_results", monitor))
        return []


@pytest.mark.parametrize("sla, calls", [
    ({"volume": {"min_rows": 1}}, [("run_events", "orders")]),
    ({"schema": {}}, [("monitor_results", "schema"), ("run_events", "orders")]),
    ({"metrics": [{"metric": "revenue", "grain": "day", "max_change_pct": 50}]}, [("monitor_results", None)]),
    ({"metrics": [{"metric": "revenue", "grain": "day", "min": 0}]}, []),
    ({"freshness": LATE}, [])])
def test_the_monitors_read_the_history_that_evaluate_gives_them_and_only_when_they_need_it(tmp_path, sla, calls):
    _table(tmp_path, "orders", newest_hours_ago=1)
    config = Config.from_json(_write(tmp_path, sla=sla))
    spy = _Spy()
    evaluate(config, NOW, history=spy)
    assert spy.calls == calls


def test_evaluate_uses_the_table_that_it_is_given_and_loads_none_of_its_own(tmp_path, counting):
    _table(tmp_path, "orders", newest_hours_ago=1)
    config = Config.from_json(_write(tmp_path))
    from local_data_platform.monitoring.target import load_target
    target = load_target(config)
    counting.clear()
    assert [result.status for result in evaluate(config, NOW, target=target)] == ["ok"]
    assert counting["shop.orders"] == 0


# ---------------------------------------------------------------------- ldp status


@pytest.fixture
def hooks(lineage_server, monkeypatch):
    url, handler = lineage_server
    handler.status = 200
    monkeypatch.setenv("ALERT_URL", url + "/hook")
    return handler


def _morning(tmp_path: Path) -> Path:
    """Five configs in a folder, and a sixth file that is not one. At ``NOW`` each one tells another story."""
    _table(tmp_path, "orders", newest_hours_ago=23.5)
    _write(tmp_path, "orders", sla={"freshness": LATE, "volume": {"min_rows": 50}}, alerts=[
        {"type": "file", "path": "alerts.jsonl", "on": ["monitor.warn", "monitor.error"]}], who="ops team")
    _table(tmp_path, "refunds", newest_hours_ago=1)
    _write(tmp_path, "refunds", who="finance")
    _table(tmp_path, "returns", newest_hours_ago=40)
    _write(tmp_path, "returns", alerts=[FILE_CHANNEL], who="ops team")
    _table(tmp_path, "legacy", newest_hours_ago=72, rows_in_table=7)
    _write(tmp_path, "legacy", sla=None, who="")
    _table(tmp_path, "payments", newest_hours_ago=1)
    _write(tmp_path, "payments", sla={"freshness": LATE, "volume": {"min_rows": 50, "max_change_pct": 30}},
           who="finance")
    two_failed = [_check("not_null", False, 2), _check("unique", False, 1)]
    steady = [_run("payments", number, 12 - number, rows_written=100) for number in range(1, 5)]
    _history(tmp_path, _run("orders", 1, 30, rows_written=100),
             _run("orders", 2, 3, rows_written=120, results=two_failed), _run("refunds", 1, 5),
             _run("refunds", 2, 1, status="failed"), _run("legacy", 1, 50), *steady,
             _run("payments", 5, 0.5, rows_written=40))
    (tmp_path / "config" / "typo.json").write_text("{not json")
    return tmp_path / "config"


GOLDEN = """\
Status at 2026-10-28T12:00:00Z
pipeline   table          owner     last_run               last_success_age     rows  freshness  volume  schema  metrics  quality_7d  open_alerts
---------  -------------  --------  ---------------------  -------------------  ----  ---------  ------  ------  -------  ----------  -----------
payments   shop.payments  finance   published 30.0 minute  30.0 minute             2  ok         error   -       -                 -            -
refunds    shop.refunds   finance   failed 1.0 hour        5.0 hour                2  ok         -       -       -                 -            -
returns    shop.returns   ops team  -                      0.0 minute (commit)     2  error      -       -       -                 -            1
typo.json  -              -         -                      -                       -  -          -       -       -                 -            -
orders     shop.orders    ops team  published 3.0 hour     3.0 hour                2  warn       ok      -       -                 2            1
legacy     shop.legacy    -         published 2.1 day      2.1 day                 7  no sla     no sla  no sla  no sla            -            -
(6 rows)
4 error, 1 warn, 1 no sla
History goes back to 2026-10-26T10:00:00Z, the oldest event of any pipeline here. 1 pipeline has none.
"""  # noqa: E501 - it is what the command prints


def test_the_screen_is_one_row_for_each_config_the_worst_first_and_it_says_how_far_back_the_history_goes(
        tmp_path, capsys):
    folder = _morning(tmp_path)
    code, out, err = _ldp(["status", str(folder), "--now", "2026-10-28T12:00:00Z"], capsys)
    assert out == GOLDEN
    assert code == 1 and err.startswith("ldp: typo.json: ") and "is not valid JSON" in err
    assert [line.split()[0] for line in out.splitlines()[3:9]] == [
        "payments", "refunds", "returns", "typo.json", "orders", "legacy"]


def test_the_exit_status_is_0_when_every_config_could_be_read_whatever_the_monitors_say(tmp_path, capsys):
    folder = _morning(tmp_path)
    (folder / "typo.json").unlink()
    code, out, err = _ldp(["status", str(folder), "--now", "2026-10-28T12:00:00Z"], capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[-2] == "3 error, 1 warn, 1 no sla"
    code, _, _ = _ldp(["monitor", str(folder), "--now", "2026-10-28T12:00:00Z"], capsys)
    assert code == 2, "ldp monitor reports the same monitors with 2"


def test_a_single_config_is_a_screen_of_one_row_and_its_history_line_has_no_pipeline_without_history(tmp_path, capsys):
    folder = _morning(tmp_path)
    code, out, err = _ldp(["status", str(folder / "orders.json"), "--now", "2026-10-28T12:00:00Z"], capsys)
    assert (code, err) == (0, "")
    assert [line.split()[0] for line in out.splitlines() if line.startswith("orders")] == ["orders"]
    assert "(1 row)" in out and out.splitlines()[-2:] == [
        "1 warn", "History goes back to 2026-10-27T06:00:00Z, the oldest event of any pipeline here."]


def test_a_folder_with_no_history_says_so_and_a_missing_path_and_a_bad_moment_are_errors(tmp_path, capsys):
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path, sinks=())
    code, out, _ = _ldp(["status", str(path), "--now", "2026-10-28T12:00:00Z"], capsys)
    assert code == 0 and out.splitlines()[-1] == "No run or monitor history was found."
    code, _, err = _ldp(["status", str(tmp_path / "nope")], capsys)
    assert code == 1 and "config path not found" in err
    (tmp_path / "empty").mkdir()
    code, _, err = _ldp(["status", str(tmp_path / "empty")], capsys)
    assert code == 1 and "no dataset configs in" in err
    code, _, err = _ldp(["status", str(path), "--now", "yesterday"], capsys)
    assert code == 1 and "cannot read 'yesterday' as a time" in err


def test_json_is_a_list_of_the_rows_and_nothing_else(tmp_path, capsys):
    folder = _morning(tmp_path)
    code, out, err = _ldp(["status", str(folder), "--now", "2026-10-28T12:00:00Z", "--json"], capsys)
    data = json.loads(out)
    assert code == 1 and err.startswith("ldp: typo.json: ")
    assert [row["pipeline"] for row in data] == ["payments", "refunds", "returns", "typo.json", "orders", "legacy"]
    orders = data[4]
    assert (orders["health"], orders["freshness"], orders["quality_7d"], orders["open_alerts"], orders["rows"]) == (
        "warn", "warn", 2, 1, 2)
    assert orders["last_run"]["age_seconds"] == 3 * 3600 and orders["as_of"] == "2026-10-28T12:00:00Z"
    assert [run["run_id"] for run in orders["recent_runs"]] == ["orders-run-002", "orders-run-001"]
    assert data[3]["problem"] and data[3]["table"] is None and data[5]["freshness"] == NO_SLA
    assert data[5]["owner"] is None and data[0]["owner"] == "finance" and data[3]["owner"] is None
    assert "Status at" not in out and "rows)" not in out


# ---------------------------------------------------------------------- the page


def test_the_page_is_one_file_with_no_script_no_request_and_only_anchors_inside_itself(tmp_path, capsys):
    folder = _morning(tmp_path)
    target = tmp_path / "out" / "deeper" / "status.html"
    code, out, err = _ldp(["status", str(folder), "--now", "2026-10-28T12:00:00Z", "--html", str(target)], capsys)
    page = target.read_text(encoding="utf-8")
    assert code == 1 and out.startswith(GOLDEN) and out.endswith("It needs no network.\n")
    assert re.search(r"wrote .*status\.html, 6 pipelines, \d+\.\d KB\. Open it as a page\. It needs no network\.", out)
    assert page.startswith("<!doctype html>") and "<script" not in page and "<link" not in page
    assert "default-src &#x27;none&#x27;" in page
    assert re.findall(r'\bsrc="', page) == []
    links = re.findall(r'\bhref="([^"]*)"', page)
    assert links and all(link.startswith("#") for link in links)
    assert not re.search(r"(?:src|href)=\"(?:https?:)?//", page) and "url(" not in page.replace("--font", "")
    assert sorted(path.name for path in target.parent.iterdir()) == ["status.html"], "no temporary file is left"


def test_the_page_has_the_summary_and_for_each_pipeline_its_monitors_and_its_last_runs(tmp_path, capsys):
    folder = _morning(tmp_path)
    target = tmp_path / "status.html"
    _ldp(["status", str(folder), "--now", "2026-10-28T12:00:00Z", "--html", str(target)], capsys)
    page = target.read_text(encoding="utf-8")
    assert "<title>Pipeline status</title>" in page and "6 pipelines at 2026-10-28T12:00:00Z" in page
    assert "4 error" in page and "1 warn" in page and "1 no sla" in page
    assert "History goes back to 2026-10-26T10:00:00Z." in page
    for slug in ("p-orders", "p-payments", "p-typo-json"):
        assert f'href="#{slug}"' in page and f'id="{slug}"' in page
    section = page[page.index('id="p-orders"'):page.index('id="p-legacy"')]
    assert "freshness warn after 6 hour" in section and "volume.rows" in section
    assert "orders-run-002" in section and "orders-run-001" in section
    assert "This config has no metadata.sla" in page and "No run is in the history of this pipeline." in page
    assert "is not valid JSON" in page


def test_the_page_shows_20_runs_for_a_pipeline_and_the_text_of_a_config_is_escaped(tmp_path, capsys):
    _table(tmp_path, "orders", newest_hours_ago=1)
    _write(tmp_path, "orders", who="<img src=x onerror=alert(1)> & co")
    _history(tmp_path, *[_run("orders", number, 100 - number) for number in range(1, 31)])
    target = tmp_path / "status.html"
    _ldp(["status", str(tmp_path / "config"), "--now", "2026-10-28T12:00:00Z", "--html", str(target)], capsys)
    page = target.read_text(encoding="utf-8")
    assert len(set(re.findall(r"orders-run-\d{3}", page))) == 20
    assert "orders-run-030" in page and "orders-run-011" in page and "orders-run-010" not in page
    assert "<img" not in page and "&lt;img src=x onerror=alert(1)&gt; &amp; co" in page


def test_the_page_is_written_to_a_temporary_file_and_moved_into_place_so_that_nobody_opens_half_of_it(
        tmp_path, capsys, monkeypatch):
    import os

    real, calls = os.replace, []

    def spy(source, target):
        calls.append((Path(source).name, Path(target).name, Path(source).read_text(encoding="utf-8")[:15]))
        real(source, target)

    monkeypatch.setattr(os, "replace", spy)
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    page = tmp_path / "out" / "status.html"
    _ldp(["status", str(path), "--now", "2026-10-28T12:00:00Z", "--html", str(page)], capsys)
    assert calls == [(".status.html.tmp", "status.html", "<!doctype html>")]


def test_the_page_with_json_keeps_the_json_clean_and_says_where_it_wrote_on_stderr(tmp_path, capsys):
    folder = _morning(tmp_path)
    target = tmp_path / "status.html"
    code, out, err = _ldp(["status", str(folder), "--now", "2026-10-28T12:00:00Z", "--json", "--html", str(target)],
                          capsys)
    assert len(json.loads(out)) == 6 and "It needs no network" not in out
    assert f"wrote {target}, 6 pipelines" in err and target.is_file()


def test_the_page_of_no_pipeline_is_a_page_and_one_pipeline_is_not_plural(tmp_path):
    assert "No pipeline" in render([]) and "No run or monitor history was found." in render([])
    _table(tmp_path, "orders", newest_hours_ago=1)
    path = _write(tmp_path)
    assert "1 pipeline at 2026-10-28T12:00:00Z" in render(rows(path, NOW))


def test_two_pipelines_that_make_the_same_anchor_get_different_ones(tmp_path):
    for name in ("a_b", "a-b"):
        _table(tmp_path, name, newest_hours_ago=1)
        _write(tmp_path, name)
    page = render(rows(tmp_path / "config", NOW))
    assert 'id="p-a-b"' in page and 'id="p-a-b-2"' in page and 'href="#p-a-b-2"' in page


def _plain_row(**changes) -> StatusRow:
    values = {"pipeline": "orders", "table": "shop.orders", "owner": "ops", "file": "/x/orders.json", "as_of": NOW,
              "health": "ok", "freshness": "ok", "sla": "freshness warn after 6 hour"}
    return StatusRow(**{**values, **changes})


@pytest.mark.parametrize("status, kind", [
    ("ok", "ok"), ("published", "ok"), ("skipped_duplicate", "ok"), ("succeeded", "ok"), ("warn", "warn"),
    ("blocked_quality", "warn"), ("error", "bad"), ("failed", "bad"), ("upstream_failed", "bad"), ("unknown", None),
    ("running", None)])
def test_a_status_is_a_chip_of_the_colour_that_says_how_bad_it_is(status, kind):
    run = Run(run_id="r1", attempt=1, status=status, started_at=_hours_ago(1))
    result = MonitorResult(monitor="freshness", kind="freshness", status=status if status in (
        "ok", "warn", "error", "unknown") else "ok", evaluated_at=NOW)
    page = render([_plain_row(last_run=run, recent_runs=(run,), results=(result,), freshness=result.status)])
    expected = f'<span class="chip {kind}">{status}</span>' if kind else f'<span class="chip">{status}</span>'
    assert page.count(expected) >= 2, f"{expected} for the last run in the table and in the list of runs"


def test_the_counts_of_the_page_are_chips_of_the_colour_of_their_health():
    page = render([_plain_row(pipeline="a", health="error"), _plain_row(pipeline="b", health="warn"),
                   _plain_row(pipeline="c", health="ok"), _plain_row(pipeline="d", health="unknown"),
                   _plain_row(pipeline="e", health=NO_SLA, freshness=NO_SLA, sla=None)])
    for expected in ('<span class="chip bad">1 error</span>', '<span class="chip warn">1 warn</span>',
                     '<span class="chip ok">1 ok</span>', '<span class="chip">1 unknown</span>',
                     '<span class="chip">1 no sla</span>'):
        assert expected in page
    assert page.index("1 error") < page.index("1 warn") < page.index("1 unknown") < page.index("1 ok") < \
        page.index("1 no sla")


def test_the_counts_of_the_page_leave_out_a_health_that_no_pipeline_has():
    page = render([_plain_row(pipeline="a"), _plain_row(pipeline="b")])
    assert '<span class="chip ok">2 ok</span>' in page
    for health in ("error", "warn", "unknown", "no sla"):
        assert f"0 {health}" not in page


def test_the_runs_of_a_pipeline_are_in_a_table_that_has_its_numbers_on_the_right_and_a_heading_that_says_20():
    run = Run(run_id="r1", attempt=2, status="published", started_at=_hours_ago(1), rows_read=5, rows_written=4,
              checks="3/4", duration_s=1.5, snapshot_id=77)
    page = render([_plain_row(last_run=run, recent_runs=(run,), file="/secret/place/orders.json")])
    runs = page[page.index("<h3>Last 20 runs</h3>"):]
    for number in ("2", "5", "4", "1.5"):
        assert f'<td class="num">{number}</td>' in runs, number
    assert "<td>3/4</td>" in runs and "<td>r1</td>" in runs and "<td>2026-10-28T11:00:00Z</td>" in runs
    assert "orders.json" in page and "/secret/place" not in page


def test_the_cells_of_the_sla_that_say_nothing_are_not_chips_and_numbers_are_on_the_right():
    page = render([_plain_row(freshness=None, volume=NO_SLA, rows=7, quality_7d=0, open_alerts=3)])
    summary = page[page.index("<table>"):page.index("</table>")]
    assert summary.count("<td>-</td>") == 5, "last_run, last_success_age, freshness, schema and metrics"
    assert "<td>no sla</td>" in summary
    assert '<td class="num">7</td>' in summary and '<td class="num">0</td>' in summary
    assert '<td class="num">3</td>' in summary and '<th scope="col" class="num">rows</th>' in summary


def test_an_unreadable_config_shows_its_problem_and_no_monitors_and_a_config_with_no_sla_says_so():
    bad = _plain_row(pipeline="typo.json", table=None, owner=None, health="error", freshness=None, sla=None,
                     problem="is not valid JSON")
    plain = _plain_row(pipeline="plain", health=NO_SLA, freshness=NO_SLA, sla=None)
    page = render([bad, plain])
    unreadable = page[page.index('id="p-typo-json"'):page.index('id="p-plain"')]
    assert "is not valid JSON" in unreadable and "Monitors" not in unreadable and "runs" not in unreadable
    assert "This config has no metadata.sla, so no monitor looks at it." in page[page.index('id="p-plain"'):]
    assert page.count("This config has no metadata.sla") == 1


def test_an_anchor_is_made_of_letters_digits_and_dashes_and_two_names_never_make_the_same_one():
    assert _slugs(["***", "a b", "a-b", "A B", "orders"]) == ["p-pipeline", "p-a-b", "p-a-b-2", "p-a-b-3", "p-orders"]
    assert _slugs([]) == []


def test_the_page_has_a_footer_that_says_how_far_back_the_history_goes_and_a_line_for_none():
    page = render([_plain_row(history_since=_hours_ago(30)), _plain_row(pipeline="b", history_since=_hours_ago(50))])
    assert "History goes back to 2026-10-26T10:00:00Z." in page
    assert "No run or monitor history was found." in render([_plain_row()])


# ---------------------------------------------------------------------- it only reads


def _files(root: Path) -> list[tuple[str, int, int]]:
    return sorted((str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns)
                  for path in root.rglob("*") if path.is_file())


def test_ldp_status_changes_no_file_of_the_lake_and_creates_none(tmp_path, capsys):
    folder = _morning(tmp_path)
    before = _files(tmp_path)
    for argv in (["status", str(folder)], ["status", str(folder), "--json"],
                 ["status", str(folder / "orders.json"), "--now", "2026-10-28T12:00:00Z"]):
        _ldp(argv, capsys)
    assert _files(tmp_path) == before


def test_ldp_status_sends_no_alert_and_does_not_use_up_the_change_of_state_that_ldp_monitor_alerts_for(
        tmp_path, capsys, hooks, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _table(tmp_path, "orders", newest_hours_ago=40)
    _write(tmp_path, "orders", alerts=[{"type": "webhook", "url_env": "ALERT_URL"}])
    for _ in range(2):
        code, _, _ = _ldp(["status", "config", "--now", "2026-10-28T12:00:00Z"], capsys)
        assert code == 0
    assert hooks.received == [] and not (tmp_path / "config" / ".ldp" / "monitor.jsonl").exists()
    code, out, _ = _ldp(["monitor", "config", "--now", "2026-10-28T12:00:00Z"], capsys)
    assert code == 2 and out.splitlines()[-1] == "Alerts sent to webhook ALERT_URL (1)"
    assert [body["event"] for _, _, body in hooks.received] == ["monitor.error"]
    code, out, _ = _ldp(["status", "config", "--now", "2026-10-28T12:00:00Z"], capsys)
    assert code == 0 and len(hooks.received) == 1
    assert [line.split()[-1] for line in out.splitlines() if line.startswith("orders")] == ["1"]


def test_the_command_that_an_alert_tells_a_person_to_run_is_this_command_and_it_works(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _table(tmp_path, "orders", newest_hours_ago=40)
    _write(tmp_path, "orders", alerts=[FILE_CHANNEL])
    assert main(["monitor", "config/orders.json", "--now", "2026-10-28T12:00:00Z"]) == 2
    [line] = (tmp_path / "config" / "alerts.jsonl").read_text().splitlines()
    command = json.loads(line)["command"]
    assert command == "ldp status config/orders.json"
    code, out, _ = _ldp(shlex.split(command)[1:] + ["--now", "2026-10-28T12:00:00Z"], capsys)
    assert code == 0 and "orders" in out and "error" in out


# ---------------------------------------------------------------------- the command


def test_ldp_status_is_a_command_and_its_help_says_what_the_exit_status_means(capsys):
    assert ("local_data_platform.monitoring.cli", ("monitor", "status")) in MODULE_COMMANDS
    assert main(["status", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Exit status is 0 unless ldp itself fails, such as a config that cannot be read." in help_text
    assert "records no result and sends no alert" in help_text and "--html FILE" in help_text
    assert main(["--help"]) == 0
    assert "status" in capsys.readouterr().out
