"""Running a config from Apache Airflow 3: what a task instance maps to, and the operator that uses the mapping.

Airflow gives a task its ``context``, which holds the data interval of the DAG run and a task instance that knows its
DAG, its run, its task and its try. ``local_data_platform.integrations.airflow`` turns them into what the exactly-once
protocol needs, a window, a run id and an attempt, in plain Python. So these tests need no Airflow and run in every job.
The ``context`` is a dict with the same keys, and the operator is built over a stand-in for ``BaseOperator`` in a fake
``airflow`` package. The runs are real: they load files into Iceberg tables under ``tmp_path``.
"""

import datetime as dt
import json
import logging
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError, DataQualityError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.integrations import airflow as ldp_airflow
from local_data_platform.integrations.airflow import (
    NoDataInterval,
    attempt_of,
    key_for,
    run,
    run_id_of,
    run_options,
    window_of,
)
from local_data_platform.spec import plan
from tests.support import load_example_module

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "restaurant"
UTC = dt.timezone.utc
KOLKATA = dt.timezone(dt.timedelta(hours=5, minutes=30))
NEW_YORK = dt.timezone(dt.timedelta(hours=-4))
CATALOG = {"identifier": "shop", "warehouse_path": "wh"}
DROP = "drops/orders_{window_start:%Y-%m-%d}.csv"


def at(*args: int) -> dt.datetime:
    """A UTC instant."""
    return dt.datetime(*args, tzinfo=UTC)


def interval(day: int) -> tuple[dt.datetime, dt.datetime]:
    """The data interval of one day of August 2026, as a daily schedule gives it."""
    return at(2026, 8, day), at(2026, 8, day + 1)


def task_instance(*, dag_id: str = "orders_daily", run_id: str = "scheduled__2026-08-02T00:00:00+00:00",
                  task_id: str = "orders", try_number: int = 1, map_index: int = -1) -> SimpleNamespace:
    """What the operator reads of Airflow's ``RuntimeTaskInstance``."""
    return SimpleNamespace(dag_id=dag_id, run_id=run_id, task_id=task_id, try_number=try_number, map_index=map_index)


def context(start, end, **task) -> dict:
    """The ``context`` of a task: the keys of Airflow's that the mapping reads."""
    ti = task_instance(**task)
    return {"ti": ti, "task_instance": ti, "run_id": ti.run_id, "data_interval_start": start, "data_interval_end": end}


# ====================================================================== a folder of drops, a config and a table


def write_drop(folder: Path, day: int, rows: int = 2, *, late: tuple = ()) -> None:
    """The drop file of one day of August 2026: ``rows`` orders from 10:00 on, then any ``late`` ``(id, at)`` rows."""
    (folder / "drops").mkdir(exist_ok=True)
    lines = [f"{day * 100 + n},2026-08-{day:02d}T{10 + n:02d}:00:00" for n in range(rows)]
    lines += [f"{row[0]},{row[1]}" for row in late]
    (folder / "drops" / f"orders_2026-08-{day:02d}.csv").write_text("id,at\n" + "\n".join(lines) + "\n")


def write_config(folder: Path, *, min_rows: int | None = None, target: str = "orders") -> Path:
    """A config that loads the drop of the window into ``target``, by the column ``at``, with an optional row gate."""
    metadata = {
        "source": {"name": "orders", "format": "CSV", "path": DROP},
        "target": {"name": target, "format": "ICEBERG", "catalog": CATALOG, "write_mode": "append"},
        "window": {"size": "day", "column": "at"},
    }
    if min_rows:
        metadata["quality"] = {"on_failure": "fail", "checks": [{"check": "row_count", "min": min_rows}]}
    path = folder / "orders.json"
    path.write_text(json.dumps({"identifier": "orders", "metadata": metadata}))
    return path


def table(folder: Path) -> Iceberg:
    return Iceberg("orders", CATALOG, base_dir=folder)


def ids(folder: Path) -> list[int]:
    """The ``id`` of every row of the table."""
    return sorted(row["id"] for row in table(folder).table().scan().to_arrow().to_pylist())


def summary(folder: Path):
    """The properties of the snapshot that the last publish added."""
    return table(folder).table().current_snapshot().summary


# ====================================================================== a fake Airflow


class FakeDAG:
    """What the example needs of ``DAG``: it records the operators that are made inside its ``with`` block."""

    stack: list = []

    def __init__(self, dag_id, **kwargs):
        self.dag_id, self.kwargs, self.tasks = dag_id, kwargs, []

    def __enter__(self):
        FakeDAG.stack.append(self)
        return self

    def __exit__(self, *exc):
        FakeDAG.stack.pop()
        return False


class FakeBaseOperator:
    """What the operator needs of Airflow's ``BaseOperator``: keyword arguments and a logger."""

    def __init__(self, *, task_id, retries=0, **kwargs):
        self.task_id, self.retries, self.extra = task_id, retries, kwargs
        self.log = logging.getLogger("fake_airflow.operator")
        if FakeDAG.stack:
            FakeDAG.stack[-1].tasks.append(self)


class FakeTimetable:
    """``CronDataIntervalTimetable``, which only remembers what it was given."""

    def __init__(self, expression, *, timezone):
        self.expression, self.timezone = expression, timezone


def install_fake_airflow(monkeypatch, *, new_exceptions: bool = True, old_exceptions: bool = True) -> SimpleNamespace:
    """Put a fake ``airflow`` package in ``sys.modules`` for one test.

    ``new_exceptions`` says whether ``airflow.sdk.exceptions`` has ``AirflowFailException``, as Airflow 3.1 and later,
    and ``old_exceptions`` whether ``airflow.exceptions`` has it, as every Airflow 3 does. The two exception classes
    differ, so a test can tell which one was raised.
    """
    base = type("BaseOperator", (FakeBaseOperator,), {})
    fail_new, fail_old = (type("AirflowFailException", (Exception,), {}) for _ in range(2))
    modules: dict = {}

    def module(name: str, **attributes) -> types.ModuleType:
        made = modules[name] = types.ModuleType(name)
        made.__dict__.update(attributes)
        return made

    root = module("airflow")
    root.sdk = module("airflow.sdk", BaseOperator=base, DAG=FakeDAG)
    root.timetables = module("airflow.timetables")
    root.timetables.interval = module("airflow.timetables.interval", CronDataIntervalTimetable=FakeTimetable)
    if new_exceptions:
        root.sdk.exceptions = module("airflow.sdk.exceptions", AirflowFailException=fail_new)
    if old_exceptions:
        root.exceptions = module("airflow.exceptions", AirflowFailException=fail_old)
    for name in ("airflow", "airflow.sdk", "airflow.sdk.exceptions", "airflow.exceptions", "airflow.timetables",
                 "airflow.timetables.interval"):
        if name in modules:
            monkeypatch.setitem(sys.modules, name, modules[name])
        else:
            monkeypatch.delitem(sys.modules, name, raising=False)  # a real Airflow must not show through
    return SimpleNamespace(base=base, fail_new=fail_new, fail_old=fail_old)


@pytest.fixture
def fake(monkeypatch):
    return install_fake_airflow(monkeypatch)


# ====================================================================== the window


def test_the_window_is_the_data_interval_of_the_dag_run():
    start, end = interval(2)

    assert window_of(context(start, end)) == (start, end)


def test_a_context_can_be_any_object_that_has_the_names():
    ti = task_instance(try_number=2)
    as_object = SimpleNamespace(ti=ti, data_interval_start=at(2026, 8, 2), data_interval_end=at(2026, 8, 3),
                                run_id=ti.run_id)

    assert window_of(as_object) == interval(2)
    assert run_id_of(as_object) == run_id_of(context(*interval(2)))
    assert attempt_of(as_object) == 2


@pytest.mark.parametrize("start, end", [(None, None), (None, at(2026, 8, 3)), (at(2026, 8, 2), None)])
def test_a_dag_run_without_a_data_interval_has_no_window(start, end):
    with pytest.raises(NoDataInterval, match=r"scheduled__2026-08-02.* has no data interval"):
        window_of(context(start, end))


def test_an_empty_data_interval_has_no_window_and_the_error_says_how_to_get_one():
    with pytest.raises(NoDataInterval) as caught:
        window_of(context(at(2026, 8, 2), at(2026, 8, 2)))

    message = str(caught.value)
    assert "is empty" in message and "scheduled__2026-08-02" in message
    assert 'CronDataIntervalTimetable("@daily", timezone="UTC")' in message and "create_cron_data_intervals" in message


def test_no_window_is_a_config_error_so_that_nothing_retries_it():
    assert issubclass(NoDataInterval, ConfigError)


# ====================================================================== the run id and the attempt


def test_the_run_id_is_made_of_the_dag_the_run_and_the_task():
    assert run_id_of(context(*interval(2))) == "airflow:orders_daily:scheduled__2026-08-02T00:00:00+00:00:orders"


def test_every_try_of_a_task_instance_has_one_run_id_and_every_other_task_instance_has_another():
    ids_of_the_tries = {run_id_of(context(*interval(2), try_number=number)) for number in range(1, 6)}

    assert len(ids_of_the_tries) == 1
    for other in ({"dag_id": "other_dag"}, {"run_id": "manual__2026-08-02T10:00:00+00:00"}, {"task_id": "lines"},
                  {"map_index": 0}):
        assert run_id_of(context(*interval(2), **other)) not in ids_of_the_tries, other


def test_a_mapped_task_instance_adds_its_map_index_and_one_that_is_not_mapped_does_not():
    plain = run_id_of(context(*interval(2)))

    assert run_id_of(context(*interval(2), map_index=0)) == f"{plain}:0"
    assert run_id_of(context(*interval(2), map_index=7)) == f"{plain}:7"
    assert run_id_of(context(*interval(2), map_index=-1)) == plain
    assert run_id_of(context(*interval(2), map_index=None)) == plain


def test_a_context_with_only_task_instance_works_and_a_context_without_one_is_refused():
    ti = task_instance()
    only_task_instance = {"task_instance": ti, "data_interval_start": at(2026, 8, 2),
                          "data_interval_end": at(2026, 8, 3)}

    assert run_id_of(only_task_instance) == run_id_of(context(*interval(2)))
    for mapping in (run_id_of, attempt_of):
        with pytest.raises(ConfigError, match="no task instance"):
            mapping({"data_interval_start": at(2026, 8, 2), "data_interval_end": at(2026, 8, 3)})


def test_the_attempt_is_the_try_number():
    assert [attempt_of(context(*interval(2), try_number=number)) for number in (1, 2, 7)] == [1, 2, 7]


@pytest.mark.parametrize("bad", [0, -1, None, "2", 1.0, True])
def test_a_try_number_that_is_not_a_whole_number_from_1_is_refused(bad):
    with pytest.raises(ConfigError, match="try_number"):
        attempt_of(context(*interval(2), try_number=bad))


def test_the_options_are_the_arguments_of_run_config():
    ctx = context(*interval(2), try_number=2)

    assert run_options(ctx) == {"mode": None, "window": interval(2), "attempt": 2,
                                "run_id": "airflow:orders_daily:scheduled__2026-08-02T00:00:00+00:00:orders"}
    assert run_options(ctx, "overwrite")["mode"] == "overwrite"


def test_run_hands_the_config_and_the_options_to_run_config(monkeypatch):
    calls = []
    monkeypatch.setattr("local_data_platform.etl.run_config",
                        lambda config, **options: calls.append((config, options)) or "the result")
    ctx = context(*interval(2), try_number=3)

    assert run("orders.json", ctx, "append") == "the result"
    assert calls == [("orders.json", run_options(ctx, "append"))]


# ====================================================================== the key


INTERVALS = {
    "a day in UTC": interval(2),
    "a day in Kolkata": (dt.datetime(2026, 8, 2, tzinfo=KOLKATA), dt.datetime(2026, 8, 3, tzinfo=KOLKATA)),
    "a day in New York": (dt.datetime(2026, 8, 2, tzinfo=NEW_YORK), dt.datetime(2026, 8, 3, tzinfo=NEW_YORK)),
    "a month end": (at(2026, 8, 31), at(2026, 9, 1)),
}


def as_window(start: dt.datetime, end: dt.datetime) -> str:
    """The interval as ``--window`` takes it, with the datetimes written as Airflow writes them."""
    return f"{start.isoformat()}/{end.isoformat()}"


@pytest.mark.parametrize("name", INTERVALS)
def test_the_key_of_a_data_interval_is_the_key_that_ldp_plan_prints_for_it(tmp_path, capsys, name):
    config = write_config(tmp_path)
    start, end = INTERVALS[name]

    assert main(["plan", str(config), "--window", as_window(start, end), "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)["idempotency_key"]
    assert key_for(config, context(start, end)) == printed
    assert plan(config, window=as_window(start, end))["idempotency_key"] == printed


def test_the_same_instants_shown_in_another_time_zone_have_the_same_key(tmp_path):
    config = write_config(tmp_path)
    in_utc = context(at(2026, 8, 2), at(2026, 8, 3))
    in_kolkata = context(dt.datetime(2026, 8, 2, 5, 30, tzinfo=KOLKATA), dt.datetime(2026, 8, 3, 5, 30, tzinfo=KOLKATA))
    in_new_york = context(dt.datetime(2026, 8, 1, 20, tzinfo=NEW_YORK), dt.datetime(2026, 8, 2, 20, tzinfo=NEW_YORK))

    assert key_for(config, in_utc) == key_for(config, in_kolkata) == key_for(config, in_new_york)


def test_the_key_follows_the_interval_and_the_config_and_nothing_else(tmp_path):
    config = write_config(tmp_path)
    (tmp_path / "other").mkdir()
    other_config = write_config(tmp_path / "other", target="lines")
    second = key_for(config, context(*interval(2)))

    assert key_for(config, context(*interval(3))) != second, "another interval"
    assert key_for(other_config, context(*interval(2))) != second, "another config"
    for same in ({"dag_id": "other_dag"}, {"run_id": "manual__x"}, {"task_id": "lines"}, {"try_number": 4},
                 {"map_index": 2}):
        assert key_for(config, context(*interval(2), **same)) == second, same


# ====================================================================== real runs through the mapping


def test_a_run_loads_the_rows_of_its_data_interval_and_publishes_under_the_key_of_the_interval(tmp_path):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2, rows=3, late=((99, "2026-08-01T23:30:00"),))
    ctx = context(*interval(2))

    result = run(config, ctx)

    assert (result.status, result.rows_read, result.rows_written) == ("published", 4, 3)
    assert ids(tmp_path) == [200, 201, 202], "the late row of the day before is not in this window"
    assert result.idempotency_key == key_for(config, ctx)
    assert summary(tmp_path)["ldp.idempotency-key"] == key_for(config, ctx)
    assert summary(tmp_path)["ldp.run-id"] == run_id_of(ctx) and summary(tmp_path)["ldp.attempt"] == "1"


def test_a_second_dag_run_over_the_same_interval_writes_nothing(tmp_path):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)

    first = run(config, context(*interval(2), run_id="scheduled__2026-08-02T00:00:00+00:00"))
    snapshots = len(table(tmp_path).table().snapshots())
    backfill = run(config, context(*interval(2), run_id="backfill__2026-08-03T00:00:00+00:00"))

    assert (first.status, backfill.status) == ("published", "skipped_duplicate")
    assert backfill.published_snapshot_id == first.published_snapshot_id and ids(tmp_path) == [200, 201]
    assert table(tmp_path).table().current_snapshot().snapshot_id == first.published_snapshot_id, "nothing was added"
    assert len(table(tmp_path).table().snapshots()) == snapshots


def test_every_interval_is_loaded_once_and_a_second_pass_over_all_of_them_loads_nothing(tmp_path):
    config = write_config(tmp_path)
    for day in (1, 2, 3):
        write_drop(tmp_path, day)

    first = [run(config, context(*interval(day), run_id=f"scheduled__{day}")).status for day in (1, 2, 3)]
    again = [run(config, context(*interval(day), run_id=f"backfill__{day}")).status for day in (3, 2, 1)]

    assert first == ["published"] * 3 and again == ["skipped_duplicate"] * 3
    assert ids(tmp_path) == [100, 101, 200, 201, 300, 301]


def test_a_retry_is_the_next_attempt_of_the_same_run(tmp_path):
    config = write_config(tmp_path)
    first_try = context(*interval(2), try_number=1)

    with pytest.raises(FileNotFoundError, match="orders_2026-08-02.csv"):  # the drop has not landed
        run(config, first_try)
    assert not table(tmp_path).exists()
    write_drop(tmp_path, 2)
    result = run(config, context(*interval(2), try_number=2))

    assert result.status == "published" and ids(tmp_path) == [200, 201]
    assert summary(tmp_path)["ldp.run-id"] == run_id_of(first_try)
    assert summary(tmp_path)["ldp.attempt"] == "2"


def test_a_try_after_one_that_had_already_published_finds_the_key_and_writes_nothing(tmp_path):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)
    first = run(config, context(*interval(2), try_number=1))  # Airflow lost this try after its commit

    retry = run(config, context(*interval(2), try_number=2))

    assert (first.status, retry.status) == ("published", "skipped_duplicate")
    assert retry.published_snapshot_id == first.published_snapshot_id
    assert summary(tmp_path)["ldp.attempt"] == "1" and ids(tmp_path) == [200, 201]


def test_a_data_gate_raises_and_publishes_nothing(tmp_path):
    config = write_config(tmp_path, min_rows=5)
    write_drop(tmp_path, 2, rows=2)

    with pytest.raises(DataQualityError, match="row_count"):
        run(config, context(*interval(2)))

    assert not table(tmp_path).exists()


def test_the_result_is_plain_json_so_it_can_be_an_xcom(tmp_path):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)

    value = run(config, context(*interval(2))).to_dict()

    assert json.loads(json.dumps(value)) == value
    assert value["status"] == "published" and value["rows_written"] == 2


def test_the_command_line_recipe_and_the_operator_publish_under_one_key_and_one_run_id(tmp_path, capsys):
    """What the BashOperator recipe of docs/airflow.md renders, the interval as Airflow writes a datetime and the same
    run id as the operator, and a try of the operator after it, which finds what the command published."""
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)
    ctx = context(*interval(2), try_number=1)
    start, end = window_of(ctx)

    assert main(["run", str(config), "--window", as_window(start, end), "--run-id", run_id_of(ctx),
                 "--attempt", str(attempt_of(ctx))]) == 0
    capsys.readouterr()
    after = run(config, context(*interval(2), try_number=2))

    assert after.status == "skipped_duplicate" and after.idempotency_key == key_for(config, ctx)
    assert summary(tmp_path)["ldp.run-id"] == run_id_of(ctx) and ids(tmp_path) == [200, 201]


# ====================================================================== the operator


def test_the_operator_is_built_over_the_base_operator_that_airflow_gives(fake, tmp_path):
    operator = ldp_airflow.LdpRunOperator(task_id="orders", config=tmp_path / "orders.json", retries=2)

    assert isinstance(operator, fake.base) and (operator.task_id, operator.retries) == ("orders", 2)
    assert operator.config == str(tmp_path / "orders.json") and operator.mode is None
    cls = type(operator)
    assert (cls.__module__, cls.__qualname__) == ("local_data_platform.integrations.airflow", "LdpRunOperator")
    assert cls.template_fields == ("config", "mode")
    assert ldp_airflow.LdpRunOperator is cls, "the same class every time, so that DAG files see one operator"


def test_the_config_of_an_operator_is_given_by_name(fake):
    with pytest.raises(TypeError):
        ldp_airflow.LdpRunOperator(task_id="orders")
    with pytest.raises(TypeError):
        ldp_airflow.LdpRunOperator("orders.json", task_id="orders")


def test_execute_publishes_and_returns_the_result_as_a_dict(fake, tmp_path, caplog):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)
    ctx = context(*interval(2), try_number=2)
    caplog.set_level(logging.INFO, logger="fake_airflow.operator")

    value = ldp_airflow.LdpRunOperator(task_id="orders", config=config).execute(ctx)

    assert value["status"] == "published" and value["rows_written"] == 2
    assert value["idempotency_key"] == key_for(config, ctx) and value["run_id"] == run_id_of(ctx)
    assert json.loads(json.dumps(value)) == value
    assert f"as run {run_id_of(ctx)}, attempt 2" in caplog.text
    assert "2026-08-02T00:00:00+00:00/2026-08-03T00:00:00+00:00" in caplog.text
    assert f"published, key {value['idempotency_key']}, snapshot {value['published_snapshot_id']}" in caplog.text


def test_a_mode_of_the_operator_overrides_the_mode_of_the_target(fake, tmp_path):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)

    value = ldp_airflow.LdpRunOperator(task_id="orders", config=config, mode="overwrite").execute(
        context(*interval(2)))

    assert value["write_result"]["mode"] == "overwrite"


def test_a_window_that_was_published_already_is_a_success_that_writes_nothing(fake, tmp_path):
    config = write_config(tmp_path)
    write_drop(tmp_path, 2)
    operator = ldp_airflow.LdpRunOperator(task_id="orders", config=config)
    first = operator.execute(context(*interval(2), run_id="scheduled__a"))

    second = operator.execute(context(*interval(2), run_id="backfill__b"))

    assert (first["status"], second["status"]) == ("published", "skipped_duplicate")
    assert second["published_snapshot_id"] == first["published_snapshot_id"] and ids(tmp_path) == [200, 201]


def test_a_data_gate_fails_the_task_without_a_retry(fake, tmp_path):
    config = write_config(tmp_path, min_rows=5)
    write_drop(tmp_path, 2, rows=2)

    with pytest.raises(fake.fail_new) as caught:
        ldp_airflow.LdpRunOperator(task_id="orders", config=config).execute(context(*interval(2)))

    assert isinstance(caught.value.__cause__, DataQualityError)
    assert "not retried" in str(caught.value) and "row_count" in str(caught.value) and str(config) in str(caught.value)
    assert not table(tmp_path).exists()


def test_a_config_that_cannot_run_fails_the_task_without_a_retry(fake, tmp_path):
    missing = tmp_path / "no_such_config.json"

    with pytest.raises(fake.fail_new) as caught:
        ldp_airflow.LdpRunOperator(task_id="orders", config=missing).execute(context(*interval(2)))

    assert isinstance(caught.value.__cause__, ConfigError)
    assert "not retried" in str(caught.value) and "config file not found" in str(caught.value)


@pytest.mark.parametrize("start, end", [(at(2026, 8, 2), at(2026, 8, 2)), (None, None)])
def test_a_dag_run_without_a_window_fails_the_task_without_a_retry_and_says_why(fake, tmp_path, start, end):
    config = write_config(tmp_path)

    with pytest.raises(fake.fail_new, match="not retried") as caught:
        ldp_airflow.LdpRunOperator(task_id="orders", config=config).execute(context(start, end))

    assert isinstance(caught.value.__cause__, NoDataInterval)
    assert ("CronDataIntervalTimetable" in str(caught.value)) == (start is not None)
    assert not table(tmp_path).exists()


def test_any_other_error_is_raised_as_it_is_so_that_airflow_retries_it(fake, tmp_path):
    config = write_config(tmp_path)
    operator = ldp_airflow.LdpRunOperator(task_id="orders", config=config)

    with pytest.raises(FileNotFoundError) as caught:  # the drop has not landed yet
        operator.execute(context(*interval(2), try_number=1))
    assert not isinstance(caught.value, (fake.fail_new, fake.fail_old))
    write_drop(tmp_path, 2)
    value = operator.execute(context(*interval(2), try_number=2))

    assert value["status"] == "published" and ids(tmp_path) == [200, 201]
    assert summary(tmp_path)["ldp.run-id"] == value["run_id"] and summary(tmp_path)["ldp.attempt"] == "2"


@pytest.mark.parametrize("new, old, raised", [(True, True, "new"), (True, False, "new"), (False, True, "old")])
def test_the_fail_exception_comes_from_where_this_airflow_keeps_it(monkeypatch, tmp_path, new, old, raised):
    fake = install_fake_airflow(monkeypatch, new_exceptions=new, old_exceptions=old)
    config = write_config(tmp_path, min_rows=5)
    write_drop(tmp_path, 2)

    with pytest.raises(Exception) as caught:
        ldp_airflow.LdpRunOperator(task_id="orders", config=config).execute(context(*interval(2)))

    assert type(caught.value) is (fake.fail_new if raised == "new" else fake.fail_old)


def test_each_airflow_gets_its_own_operator_class(monkeypatch):
    first = install_fake_airflow(monkeypatch)
    first_class = ldp_airflow.LdpRunOperator
    second = install_fake_airflow(monkeypatch)
    second_class = ldp_airflow.LdpRunOperator

    assert first_class is not second_class
    assert issubclass(first_class, first.base) and issubclass(second_class, second.base)
    assert not issubclass(second_class, first.base)


# ====================================================================== Airflow is imported when it is needed


def test_importing_the_module_imports_no_airflow():
    code = ("import sys\nimport local_data_platform.integrations.airflow as ldp_airflow\n"
            "print([name for name in sys.modules if name == 'airflow' or name.startswith('airflow.')])")

    output = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout

    assert output.strip() == "[]"


def test_the_operator_needs_airflow_and_the_error_says_how_to_get_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "airflow", None)  # an import of it fails, as if it were not installed
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)

    with pytest.raises(ImportError, match=r"needs Apache Airflow 3.*constraints file"):
        ldp_airflow.LdpRunOperator
    with pytest.raises(ImportError, match="Apache Airflow 3"):
        from local_data_platform.integrations.airflow import LdpRunOperator  # noqa: F401
    assert callable(run) and window_of(context(*interval(2))) == interval(2), "the mapping needs no Airflow"


def test_the_module_lists_its_names_and_refuses_others():
    assert "LdpRunOperator" in dir(ldp_airflow) and "LdpRunOperator" in ldp_airflow.__all__
    assert set(ldp_airflow.__all__) <= set(dir(ldp_airflow))
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        ldp_airflow.Nope


# ====================================================================== the example DAG


@pytest.fixture
def example(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("LDP_AIRFLOW_DEMO", str(tmp_path / "demo"))
    return load_example_module("restaurant_airflow_dag", EXAMPLE / "airflow_dag.py")


def test_the_example_dag_runs_the_orders_config_for_each_daily_data_interval(example, tmp_path):
    dag = example.dag
    (task,) = dag.tasks

    assert dag.dag_id == "restaurant_orders"
    assert isinstance(dag.kwargs["schedule"], FakeTimetable), "a cron string would give an empty data interval"
    assert (dag.kwargs["schedule"].expression, dag.kwargs["schedule"].timezone) == ("@daily", "UTC")
    assert dag.kwargs["catchup"] is True and dag.kwargs["max_active_runs"] == 1
    assert dag.kwargs["start_date"] == dt.datetime(2026, 8, 1, tzinfo=UTC)
    assert dag.kwargs["end_date"] == dt.datetime(2026, 8, 7, tzinfo=UTC), "the start of the last interval"
    assert dag.kwargs["default_args"]["retries"] == 2
    assert (type(task).__qualname__, task.task_id) == ("LdpRunOperator", "orders")
    assert task.config == str(tmp_path / "demo" / "config" / "orders.json")


def test_prepare_writes_a_drop_for_each_day_and_a_valid_windowed_orders_config(example, tmp_path, capsys):
    work = tmp_path / "work"

    assert example.main(["--prepare", str(work)]) == 0

    landing = sorted(path.name for path in (work / "landing").glob("orders_*.csv"))
    assert landing == [f"orders_2026-08-0{day}.csv" for day in range(1, 8)]
    config = work / "config" / "orders.json"
    printed = plan(config, window="2026-08-02/2026-08-03")
    assert printed["valid"], printed["errors"]
    assert (printed["window_size"], printed["window_column"]) == ("day", "order_ts")
    assert printed["source_path"] == "../landing/orders_2026-08-02.csv"
    assert str(work) in capsys.readouterr().out


def test_prepare_derives_the_config_from_the_orders_config_of_the_example(example, tmp_path):
    config = json.loads(example.prepare(tmp_path / "work").read_text())
    original = json.loads((EXAMPLE / "config" / "orders.json").read_text())

    assert config["metadata"]["quality"] == original["metadata"]["quality"], "the same checks"
    assert config["metadata"]["target"] == original["metadata"]["target"]
    assert config["metadata"]["source"]["format"] == "CSV" and "{window_start" in config["metadata"]["source"]["path"]
    assert config["metadata"]["window"] == {"size": "day", "column": "order_ts"}


def test_the_example_dag_loads_the_seven_drops_once_each_and_a_second_pass_loads_nothing(example, tmp_path):
    example.prepare(tmp_path / "demo")
    (task,) = example.dag.tasks

    first = [task.execute(context(*interval(day), run_id=f"scheduled__2026-08-0{day}")) for day in range(1, 8)]
    again = [task.execute(context(*interval(day), run_id=f"backfill__2026-08-0{day}")) for day in range(7, 0, -1)]

    assert [value["status"] for value in first] == ["published"] * 7
    assert sum(value["rows_written"] for value in first) == 30
    assert [value["status"] for value in again] == ["skipped_duplicate"] * 7
    assert sum(value["rows_written"] for value in again) == 0
    orders = Iceberg("orders", {"identifier": "raw", "warehouse_path": "../warehouse"},
                     base_dir=tmp_path / "demo" / "config")
    assert orders.row_count() == 30


# ====================================================================== the guide cannot drift from the code


def guide_block(containing: str) -> str:
    """The first Python block of docs/airflow.md that has ``containing`` in it."""
    guide = (REPO_ROOT / "docs" / "airflow.md").read_text()
    return next(block for block in re.findall(r"```python\n(.*?)```", guide, flags=re.DOTALL) if containing in block)


def test_the_example_is_listed_in_the_readme_of_the_example():
    readme = (EXAMPLE / "README.md").read_text()

    assert "`airflow_dag.py`" in readme and "(../../docs/airflow.md)" in readme


def test_the_dag_that_the_guide_shows_is_in_the_example_dag_line_for_line():
    shown = [line.strip() for line in guide_block("restaurant_orders").splitlines() if line.strip()]
    example = [line.strip() for line in (EXAMPLE / "airflow_dag.py").read_text().splitlines() if line.strip()]

    assert any(example[at:at + len(shown)] == shown for at in range(len(example))), shown


def test_the_bash_recipe_of_the_guide_renders_the_window_the_run_id_and_the_attempt_of_the_operator():
    recipe = guide_block("BashOperator(")
    ctx = context(*interval(2), try_number=3)
    ti = ctx["ti"]

    run_id = re.search(r"--run-id '([^']+)'", recipe).group(1)
    for name, value in (("ti.dag_id", ti.dag_id), ("run_id", ti.run_id), ("ti.task_id", ti.task_id)):
        run_id = run_id.replace("{{ " + name + " }}", value)
    assert run_id == run_id_of(ctx)
    assert "--window '{{ data_interval_start }}/{{ data_interval_end }}'" in recipe
    assert "--attempt {{ ti.try_number }}" in recipe and "{{ ti.try_number }}" not in run_id
    assert 'CronDataIntervalTimetable("@daily", timezone="UTC")' in recipe, "a cron string would give empty intervals"


def test_the_taskflow_snippet_of_the_guide_uses_names_that_the_module_has():
    snippet = guide_block("get_current_context")

    imported = re.search(r"from local_data_platform\.integrations\.airflow import (\w+)", snippet).group(1)
    assert imported in ldp_airflow.__all__ and f"{imported}(" in snippet
    helpers = ("window_of", "run_id_of", "attempt_of", "run_options", "key_for")
    assert all(name in ldp_airflow.__all__ for name in helpers)
