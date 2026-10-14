"""Schedules: ``ldp schedule tick`` runs what is due, ``list`` shows what is coming, ``install`` prints the recipes.

The projects here are daily drop files in ``tmp_path``, one ``orders_YYYY-MM-DD.csv`` for each day, loaded by a config
whose source path has the ``{window_start:%Y-%m-%d}`` placeholder and whose ``window.column`` selects the rows of the
window, so a tick that loads day 2 can be told from one that loads day 3 by the rows in the table. Every test names the
time of its tick with ``now``, so nothing waits for a clock and nothing depends on the day it runs.

``@daily`` fires at midnight UTC and the fire of day N owns day N - 1, so a tick at 02:05 on day 3 loads day 2.
"""

import datetime as dt
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.filelock import file_lock
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import commit_log
from local_data_platform import spec
from local_data_platform.orchestration import recipes
from local_data_platform.orchestration import schedule as schedule_module
from local_data_platform.orchestration import windows
from local_data_platform.orchestration.ledger import read_windows
from local_data_platform.orchestration.schedule import (
    LOCK_PATH,
    discover,
    due_windows,
    fill_commands,
    fire_at_or_before,
    fires_after,
    latest_window,
    list_scheduled,
    next_fire,
    parse_now,
    tick,
    window_of_fire,
    windows_since,
)
from local_data_platform.orchestration.workflow import load_workflow
from tests.support import FakeDbt, dbt_artifacts, dbt_model, write_dbt_project

UTC = dt.timezone.utc
START = dt.date(2026, 6, 1)
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}
NO_ROWS = {"published": 0, "skipped": 0, "blocked": 0, "failed": 0}


# ====================================================================== helpers


def _day(n: int) -> dt.date:
    """Day number ``n``, from 1: 2026-06-01 is day 1."""
    return START + dt.timedelta(days=n - 1)


def _span(n: int) -> str:
    """The window of day ``n``, as ``START/END`` in UTC."""
    return f"{_day(n).isoformat()}T00:00:00Z/{_day(n + 1).isoformat()}T00:00:00Z"


def _at(n: int, clock: str = "02:05") -> str:
    """A time on day ``n``, as ISO text in UTC."""
    return f"{_day(n).isoformat()}T{clock}:00Z"


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _drop(root: Path, n: int, rows: int = 2) -> Path:
    """The drop file of day ``n``: ``rows`` rows at 10:00, 11:00, ..."""
    day = _day(n).isoformat()
    lines = ["id,at"] + [f"{n * 100 + i},{day}T{10 + i:02d}:00:00" for i in range(rows)]
    return _write(root / "landing" / f"orders_{day}.csv", "\n".join(lines) + "\n")


def _config(root: Path, *, name: str = "orders.json", identifier: str = "orders", schedule: str | None = None,
            size: str | None = "day", timezone: str | None = None, min_rows: int | None = 2, table: str = "orders",
            path: str = "../landing/orders_{window_start:%Y-%m-%d}.csv", extra: dict | None = None) -> Path:
    """A dataset config in ``config/``, with a schedule of its own only when ``schedule`` is given."""
    metadata: dict = {
        "source": {"name": "orders", "format": "CSV", "path": path},
        "target": {"name": table, "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG}}
    if size:  # a window column needs a size, so a config with no size has no window
        metadata["window"] = {"size": size, "column": "at", **({"timezone": timezone} if timezone else {})}
    if schedule:
        metadata["schedule"] = schedule
    if min_rows:
        metadata["quality"] = [{"check": "row_count", "min": min_rows}]
    metadata.update(extra or {})
    return _write(root / "config" / name, {"identifier": identifier, "metadata": metadata})


def _workflow(root: Path, *, name: str = "daily.json", identifier: str = "daily_orders", tasks: list | None = None,
              schedule: str | None = "@daily", size: str | None = "day", timezone: str | None = None,
              start: str | None = None, catchup: bool | None = None, extra: dict | None = None) -> Path:
    """A workflow file in ``root`` that loads the orders config, scheduled ``@daily`` by default."""
    metadata: dict = {}
    if schedule:
        metadata["schedule"] = schedule
    if size:
        metadata["window"] = {"size": size, **({"timezone": timezone} if timezone else {})}
    if start:
        metadata["start"] = start
    if catchup is not None:
        metadata["catchup"] = catchup
    metadata.update(extra or {})
    tasks = tasks if tasks is not None else [{"id": "orders", "pipeline": "config/orders.json"}]
    return _write(root / name, {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": identifier,
                                "metadata": metadata, "tasks": tasks})


def _project(root: Path, days: int = 5, **workflow) -> Path:
    """The drop files of days 1 to ``days``, the orders config, and a daily workflow that loads it."""
    for n in range(1, days + 1):
        _drop(root, n)
    _config(root, timezone=workflow.get("timezone"))  # the config counts its days in the zone of the workflow
    return _workflow(root, **workflow)


def _orders(root: Path) -> Iceberg:
    return Iceberg("orders", {"identifier": "shop", "warehouse_path": str(root / "warehouse")})


def _loaded_days(root: Path) -> list[int]:
    """The days that the orders table holds rows of, as day numbers. A table that does not exist holds none."""
    try:
        values = _orders(root).get().column("at").to_pylist()
    except TableNotFound:
        return []
    return sorted({(value.date() - START).days + 1 for value in values})


def _rows_per_day(root: Path) -> dict[int, int]:
    values = _orders(root).get().column("at").to_pylist()
    return dict(sorted(Counter((value.date() - START).days + 1 for value in values).items()))


def _ledger(root: Path) -> Path:
    return root / ".ldp" / "events.jsonl"


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in _ledger(root).read_text().splitlines() if line.strip()]


def _ran(result) -> list[tuple[str, str]]:
    """``(window, outcome)`` of each window that a tick looked at, done or not, over every workflow."""
    return [(entry.window, entry.outcome) for item in result.workflows if item.result for entry in item.result.windows]


def _did(result) -> list[tuple[str, str]]:
    """``(window, outcome)`` of each window that a tick ran, and not the ones it found done or left for later."""
    return [(window, outcome) for window, outcome in _ran(result) if outcome not in ("skipped", "not_run")]


# ====================================================================== a tick and the windows it runs


def test_a_tick_runs_the_window_that_the_latest_fire_owns_and_the_next_tick_finds_it_done(tmp_path):
    _project(tmp_path, days=3)

    first = tick(tmp_path, now=_at(3))

    assert _ran(first) == [(_span(2), "published")]
    assert first.counts() == {**NO_ROWS, "published": 1} and first.exit_code == 0 and first.status == "succeeded"
    assert _rows_per_day(tmp_path) == {2: 2}
    second = tick(tmp_path, now=_at(3, "02:10"))
    assert _ran(second) == [(_span(2), "skipped")] and second.workflows[0].due == 0, "done is not due"
    assert second.counts() == {**NO_ROWS, "skipped": 1} and second.exit_code == 0
    assert _rows_per_day(tmp_path) == {2: 2}


def test_a_tick_before_the_first_fire_has_nothing_due(tmp_path):
    _project(tmp_path, days=3, start=_day(3).isoformat())

    early = tick(tmp_path, now=_at(2))  # the fire of day 2 owns day 1, which is before the start
    later = tick(tmp_path, now=_at(4))

    assert early.workflows[0].due == 0 and early.workflows[0].result is None and early.exit_code == 0
    assert _ran(later) == [(_span(3), "published")] and later.workflows[0].due == 1


def test_a_daily_schedule_with_a_missed_night_runs_the_latest_window_and_list_says_how_to_fill_the_gap(tmp_path):
    _project(tmp_path, days=5)
    tick(tmp_path, now=_at(3))  # day 2
    # the laptop sleeps through the fire of day 4, which owns day 3, and wakes up on the morning of day 5

    woken = tick(tmp_path, now=_at(5, "09:00"))

    assert _ran(woken) == [(_span(4), "published")]
    assert _loaded_days(tmp_path) == [2, 4]
    [item] = list_scheduled(tmp_path, now=_at(5, "09:00")).items
    assert item.last_window == _span(4) and item.last_status == "succeeded" and item.due == []
    assert _texts(item.missed) == [_span(3)]
    assert item.commands == [f"ldp backfill {tmp_path / 'daily.json'} --from {_day(3)} --to {_day(4)}"]


def test_a_missed_week_without_catchup_runs_only_the_latest_window(tmp_path):
    _project(tmp_path, days=10)
    tick(tmp_path, now=_at(2))  # day 1
    # a week goes by

    result = tick(tmp_path, now=_at(10))

    assert _ran(result) == [(_span(9), "published")]
    assert _loaded_days(tmp_path) == [1, 9]
    [item] = list_scheduled(tmp_path, now=_at(10)).items
    assert len(item.missed) == 7 and item.commands == [
        f"ldp backfill {tmp_path / 'daily.json'} --from {_day(2)} --to {_day(9)}"]


def test_a_missed_week_with_catchup_runs_every_missed_window_oldest_first(tmp_path):
    _project(tmp_path, days=10, start=_day(1).isoformat(), catchup=True)
    first = tick(tmp_path, now=_at(2))
    assert _ran(first) == [(_span(1), "published")]

    result = tick(tmp_path, now=_at(10))

    assert _ran(result)[0] == (_span(1), "skipped"), "a window that is done is counted and not run"
    assert _did(result) == [(_span(n), "published") for n in range(2, 10)]
    assert _loaded_days(tmp_path) == list(range(1, 10))
    assert tick(tmp_path, now=_at(10, "02:10")).counts() == {**NO_ROWS, "skipped": 9}
    [item] = list_scheduled(tmp_path, now=_at(10)).items
    assert item.missed == [] and item.due == [] and item.commands == []


def test_catchup_runs_at_most_max_windows_a_tick_and_each_tick_goes_on_where_the_last_stopped(tmp_path):
    _project(tmp_path, days=10, start=_day(1).isoformat(), catchup=True)

    one = tick(tmp_path, now=_at(10), max_windows=3)
    two = tick(tmp_path, now=_at(10), max_windows=3)
    three = tick(tmp_path, now=_at(10), max_windows=3)
    four = tick(tmp_path, now=_at(10), max_windows=3)

    assert _did(one) == [(_span(n), "published") for n in (1, 2, 3)]
    assert one.counts() == {**NO_ROWS, "published": 3, "not_run": 6}, "the windows that wait for the next tick"
    assert _did(two) == [(_span(n), "published") for n in (4, 5, 6)] and _ran(two)[:3] == [
        (_span(n), "skipped") for n in (1, 2, 3)], "the windows that are done do not count against the cap"
    assert _did(three) == [(_span(n), "published") for n in (7, 8, 9)] and three.exit_code == 0
    assert _loaded_days(tmp_path) == list(range(1, 10))
    assert _did(four) == [] and four.counts() == {**NO_ROWS, "skipped": 9}


def test_a_window_before_the_start_is_never_run(tmp_path):
    _project(tmp_path, days=6, start=_day(3).isoformat(), catchup=True)

    result = tick(tmp_path, now=_at(6))

    assert _did(result) == [(_span(n), "published") for n in range(3, 6)]
    assert _loaded_days(tmp_path) == [3, 4, 5]


# ====================================================================== overlapping ticks


def test_a_tick_that_starts_while_another_runs_does_nothing_and_exits_with_zero(tmp_path, capsys):
    _project(tmp_path, days=3)

    with file_lock(tmp_path / LOCK_PATH):
        result = tick(tmp_path, now=_at(3))
        status = main(["schedule", "tick", str(tmp_path), "--now", _at(3)])

    assert result.overlapped and result.status == "overlapped" and result.exit_code == 0 and result.workflows == []
    out, err = capsys.readouterr()
    assert status == 0 and f"another tick holds {tmp_path / LOCK_PATH}, so this one did nothing" in out and err == ""
    assert not (tmp_path / "warehouse").exists() and not _ledger(tmp_path).exists(), "nothing ran"
    assert tick(tmp_path, now=_at(3)).counts()["published"] == 1, "the lock is free again"


HOLDER = """
import sys
from local_data_platform.filelock import file_lock

with file_lock(sys.argv[1]):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_a_tick_in_another_process_keeps_this_one_out_until_that_process_ends(tmp_path):
    _project(tmp_path, days=3)
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path / LOCK_PATH)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert tick(tmp_path, now=_at(3)).overlapped
        child.kill()
        child.wait(30)
        assert not tick(tmp_path, now=_at(3)).overlapped, "a process that dies leaves no lock behind"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(30)
        child.stdin.close()
        child.stdout.close()


def test_the_lock_of_a_tick_is_made_in_the_folder_and_given_up_when_the_tick_raises(tmp_path, monkeypatch):
    _project(tmp_path, days=3)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(schedule_module, "run_windows", interrupted)
    with pytest.raises(KeyboardInterrupt):
        tick(tmp_path, now=_at(3))

    assert (tmp_path / LOCK_PATH).is_file()
    monkeypatch.undo()
    assert not tick(tmp_path, now=_at(3)).overlapped


# ====================================================================== a failed window is retried by the next tick


def _task_runs(root: Path, window: str) -> list[dict]:
    """The ``run.started`` events of the pipeline task of a window, from the ledger."""
    started = {e["run_id"]: e for e in _events(root) if e["type"] == "workflow.started"
               and e["payload"]["window"] == window.split("/")}
    return [e for e in _events(root) if e["type"] == "run.started" and e["payload"].get("workflow_run_id") in started]


def test_a_window_that_failed_is_retried_by_the_next_tick_as_the_same_run_with_the_next_attempt(tmp_path):
    _project(tmp_path, days=3)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").rename(tmp_path / "late.csv")  # the file has not landed yet

    first = tick(tmp_path, now=_at(3))

    assert _ran(first) == [(_span(2), "failed")] and first.exit_code == 1 and first.status == "failed"
    assert first.workflows[0].result.windows[0].attempt == 1
    (tmp_path / "late.csv").rename(tmp_path / "landing" / f"orders_{_day(2)}.csv")  # and now it has

    second = tick(tmp_path, now=_at(3, "02:10"))

    assert _ran(second) == [(_span(2), "published")] and second.exit_code == 0
    window = second.workflows[0].result.windows[0]
    assert window.attempt == 2 and window.result.tasks["orders"].attempts == 1, "the attempts of this run"
    starts = [e for e in _events(tmp_path) if e["type"] == "workflow.started"]
    assert [e["attempt"] for e in starts] == [1, 2] and len({e["run_id"] for e in starts}) == 1, "one run, two attempts"
    tasks = _task_runs(tmp_path, _span(2))
    assert [e["attempt"] for e in tasks] == [1, 2] and len({e["run_id"] for e in tasks}) == 1
    [published] = commit_log(_orders(tmp_path).table())
    assert published["run_id"].replace("-", "") == tasks[0]["run_id"].replace("-", "") and published["attempt"] == "2"
    assert _rows_per_day(tmp_path) == {2: 2}
    assert tick(tmp_path, now=_at(3, "02:15")).counts() == {**NO_ROWS, "skipped": 1}


def test_a_window_that_a_data_gate_held_back_is_run_again_by_the_next_tick_and_exits_with_three_each_time(tmp_path):
    _project(tmp_path, days=3)
    _drop(tmp_path, 2, rows=1)  # one row, and the check wants two

    first = tick(tmp_path, now=_at(3))
    second = tick(tmp_path, now=_at(3, "02:10"))

    assert (first.exit_code, first.status) == (3, "blocked_quality") and second.exit_code == 3
    assert [e.attempt for e in (first.workflows[0].result.windows + second.workflows[0].result.windows)] == [1, 2]
    assert _loaded_days(tmp_path) == []
    _drop(tmp_path, 2)

    third = tick(tmp_path, now=_at(3, "02:15"))

    assert _ran(third) == [(_span(2), "published")] and third.exit_code == 0
    assert third.workflows[0].result.windows[0].attempt == 3
    assert _rows_per_day(tmp_path) == {2: 2}


def test_a_run_that_never_finished_is_carried_on_by_the_next_tick(tmp_path):
    _project(tmp_path, days=3)
    workflow = load_workflow(tmp_path / "daily.json")
    start, end = _span(2).split("/")
    _write(_ledger(tmp_path), "\n".join(json.dumps(event) for event in [
        {"type": "workflow.started", "run_id": "crashed-run", "attempt": 1, "payload": {
            "workflow": workflow.identifier, "window": [start, end]}},
        {"type": "run.started", "run_id": "crashed-task", "attempt": 3, "payload": {
            "workflow": workflow.identifier, "workflow_run_id": "crashed-run", "task_id": "orders"}}]) + "\n")

    result = tick(tmp_path, now=_at(3))

    assert _ran(result) == [(_span(2), "published")] and result.workflows[0].result.windows[0].attempt == 2
    [published] = commit_log(_orders(tmp_path).table())
    assert published["attempt"] == "4", "one more than the 3 that the crashed task had reached"
    assert {e["run_id"] for e in _events(tmp_path) if e["type"] == "workflow.started"} == {"crashed-run"}
    assert read_windows(_ledger(tmp_path), workflow.identifier)[(start, end)].attempt == 2


def test_a_window_whose_last_run_succeeded_starts_a_run_of_its_own_when_its_keys_are_gone(tmp_path):
    _project(tmp_path, days=3)
    tick(tmp_path, now=_at(3))
    first_run = next(e["run_id"] for e in _events(tmp_path) if e["type"] == "workflow.started")
    shutil.rmtree(tmp_path / "warehouse")  # the table is gone, and the ledger says the window succeeded

    result = tick(tmp_path, now=_at(3, "02:10"))

    assert _ran(result) == [(_span(2), "published")] and result.workflows[0].result.windows[0].attempt == 1
    runs = {e["run_id"] for e in _events(tmp_path) if e["type"] == "workflow.started"}
    assert len(runs) == 2 and first_run in runs


def _dbt_and_export_workflow(root: Path, fake: FakeDbt) -> Path:
    """Orders, then a dbt build that a stand-in answers, then an export to a CSV file, in that order."""
    project = write_dbt_project(root / "dbt", {"m": "select 1 as x"})
    _write(project / "ldp_dbt.json", {"sources": {"configs": ["../config"]},
                                      "publish": {"catalog": {"identifier": "gold", "warehouse_path": "../warehouse"},
                                                  "config_dir": "target/ldp"}})
    manifest, results = dbt_artifacts(dbt_model("m"))
    fake.will(database=str(root / "dbt" / "target" / "ldp_build.duckdb"),
              sql=["create or replace table main.m as select 1 as x"],
              artifacts={"manifest.json": manifest, "run_results.json": results})
    _write(root / "config" / "export.json", {"identifier": "export_orders", "metadata": {
        "source": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "orders_export", "format": "CSV", "path": "../out/orders.csv"}}})
    return _workflow(root, tasks=[
        {"id": "orders", "pipeline": "config/orders.json"},
        {"id": "build", "dbt": {"project": "dbt", "dbt_bin": str(fake.path)}, "depends_on": ["orders"]},
        {"id": "export", "pipeline": "config/export.json", "depends_on": ["build"]}])


def test_a_dbt_task_that_built_the_window_on_one_tick_is_skipped_when_the_next_tick_carries_the_run_on(tmp_path):
    _project(tmp_path, days=3)
    fake = FakeDbt(tmp_path / "fake")
    _dbt_and_export_workflow(tmp_path, fake)
    (tmp_path / "out").write_text("a file where the folder of the export should be")

    first = tick(tmp_path, now=_at(3))

    statuses = {task.id: task.status for task in first.workflows[0].result.windows[0].result.tasks.values()}
    assert statuses == {"orders": "published", "build": "succeeded", "export": "failed"} and len(fake.calls) == 1
    (tmp_path / "out").unlink()

    second = tick(tmp_path, now=_at(3, "02:10"))

    window = second.workflows[0].result.windows[0]
    statuses = {task.id: task.status for task in window.result.tasks.values()}
    assert statuses == {"orders": "skipped_duplicate", "build": "skipped_duplicate", "export": "published"}
    assert window.attempt == 2 and window.outcome == "published" and len(fake.calls) == 1, "the build is not repeated"
    assert window.result.tasks["build"].detail["built_by"] == window.result.run_id, "it was built by this very run"
    assert tick(tmp_path, now=_at(3, "02:15")).counts() == {**NO_ROWS, "skipped": 1} and len(fake.calls) == 1


# ====================================================================== other workflows, other problems


def test_a_workflow_that_cannot_be_ticked_does_not_stop_the_others_and_the_tick_exits_with_one(tmp_path):
    _project(tmp_path, days=3)
    _config(tmp_path, name="broken.json", identifier="broken", path="../landing/{window_start:%Y}.csv", table="broken")
    broken = json.loads((tmp_path / "config" / "broken.json").read_text())
    broken["metadata"]["source"]["format"] = "NO_SUCH_FORMAT"
    _write(tmp_path / "config" / "broken.json", broken)
    _workflow(tmp_path, name="a_broken.json", identifier="a_broken",
              tasks=[{"id": "broken", "pipeline": "config/broken.json"}])

    result = tick(tmp_path, now=_at(3))

    assert [item.workflow for item in result.workflows] == ["a_broken", "daily_orders"], "in the order of the paths"
    assert result.workflows[0].error is not None and "a_broken is not valid" in result.workflows[0].error
    assert result.workflows[1].error is None and _loaded_days(tmp_path) == [2]
    assert result.exit_code == 1 and result.status == "failed"


def test_an_unexpected_error_in_one_workflow_is_recorded_and_the_others_still_run(tmp_path, monkeypatch):
    _project(tmp_path, days=3)
    _workflow(tmp_path, name="a_first.json", identifier="a_first")
    real = schedule_module.run_windows

    def explode_for_the_first(workflow, *args, **kwargs):
        if workflow.identifier == "a_first":
            raise RuntimeError("a bug")
        return real(workflow, *args, **kwargs)

    monkeypatch.setattr(schedule_module, "run_windows", explode_for_the_first)

    result = tick(tmp_path, now=_at(3))

    assert result.workflows[0].error == "RuntimeError: a bug" and result.workflows[0].exit_code == 1
    assert _ran(result) == [(_span(2), "published")] and result.exit_code == 1


# ====================================================================== fires, and the window that each owns


def _utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _texts(spans) -> list[str]:
    """Windows, ``(start, end)`` pairs, as ``START/END`` text in UTC."""
    return [f"{a.strftime('%Y-%m-%dT%H:%M:%SZ')}/{b.strftime('%Y-%m-%dT%H:%M:%SZ')}" for a, b in spans]


def _fires(cron: str, tz: str, after: str, until: str) -> list[str]:
    """The fires of a cron expression after one time and up to another, as UTC text."""
    schedule, zone = windows.parse_schedule(cron), windows.get_timezone(tz)
    found = []
    for fire in fires_after(schedule, zone, _utc(after)):
        if fire > _utc(until):
            break
        found.append(fire.strftime("%Y-%m-%dT%H:%MZ"))
    return found


@pytest.mark.parametrize("moment, expected", [
    ("2026-08-05T02:05:00Z", "2026-08-05T02:05:00Z"),
    ("2026-08-05T02:05:30Z", "2026-08-05T02:05:00Z"),
    ("2026-08-05T02:04:59Z", "2026-08-04T02:05:00Z"),
    ("2026-08-05T09:00:00Z", "2026-08-05T02:05:00Z"),
    ("2026-08-05T02:05:00.500000Z", "2026-08-05T02:05:00Z"),
])
def test_the_latest_fire_is_the_last_one_at_or_before_the_moment_and_a_second_does_not_change_it(moment, expected):
    schedule = windows.parse_schedule("5 2 * * *")

    found = fire_at_or_before(schedule, windows.get_timezone(None), _utc(moment))

    assert found == _utc(expected) and found.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize("moment, expected", [
    ("2026-08-05T02:05:00Z", "2026-08-06T02:05:00Z"),
    ("2026-08-05T02:04:59Z", "2026-08-05T02:05:00Z"),
    ("2026-08-05T02:05:00.500000Z", "2026-08-06T02:05:00Z"),
])
def test_the_fires_after_a_moment_start_with_the_next_one_and_never_with_a_fire_at_the_moment(moment, expected):
    schedule = windows.parse_schedule("5 2 * * *")

    first = next(fires_after(schedule, windows.get_timezone(None), _utc(moment)))

    assert first == _utc(expected)


@pytest.mark.parametrize("cron, tz, fire, size, expected", [
    ("5 2 * * *", None, "2026-08-05T02:05Z", "day", "2026-08-04T00:00Z/2026-08-05T00:00Z"),
    ("@daily", None, "2026-08-05T00:00Z", "day", "2026-08-04T00:00Z/2026-08-05T00:00Z"),
    ("5 2 * * *", "Europe/Paris", "2026-08-05T00:05Z", "day", "2026-08-03T22:00Z/2026-08-04T22:00Z"),
    ("5 * * * *", None, "2026-08-05T10:05Z", "hour", "2026-08-05T09:00Z/2026-08-05T10:00Z"),
    ("*/15 * * * *", None, "2026-08-05T10:15Z", "15m", "2026-08-05T10:00Z/2026-08-05T10:15Z"),
    ("*/15 * * * *", None, "2026-08-05T10:00Z", "15m", "2026-08-05T09:45Z/2026-08-05T10:00Z"),
    ("5 2 * * *", None, "2026-08-05T02:05Z", None, "2026-08-04T02:05Z/2026-08-05T02:05Z"),
    ("0 3 * * 1", None, "2026-08-10T03:00Z", None, "2026-08-03T03:00Z/2026-08-10T03:00Z"),
    ("@hourly", None, "2026-08-05T10:00Z", None, "2026-08-05T09:00Z/2026-08-05T10:00Z"),
])
def test_the_window_of_a_fire_is_the_last_complete_window_of_the_size_or_the_time_since_the_fire_before(
        cron, tz, fire, size, expected):
    span = window_of_fire(_utc(fire), windows.parse_schedule(cron), size, windows.get_timezone(tz))

    assert span == tuple(_utc(part) for part in expected.split("/"))


NY = "America/New_York"


def test_a_fixed_time_that_the_spring_forward_gap_skips_fires_right_after_the_gap_and_the_days_are_23_and_24_hours(
        tmp_path):
    _workflow(tmp_path, schedule="30 2 * * *", timezone=NY)
    workflow = load_workflow(tmp_path / "daily.json")

    assert _fires("30 2 * * *", NY, "2026-03-06T12:00Z", "2026-03-09T12:00Z") == [
        "2026-03-07T07:30Z", "2026-03-08T07:00Z", "2026-03-09T06:30Z"], "03:00 EDT, as 02:30 does not exist on 8 March"
    before_the_gap_ends = latest_window(workflow, _utc("2026-03-08T06:59:00Z"))
    after = latest_window(workflow, _utc("2026-03-08T07:00:00Z"))
    next_day = latest_window(workflow, _utc("2026-03-09T06:30:00Z"))
    assert before_the_gap_ends == (_utc("2026-03-06T05:00:00Z"), _utc("2026-03-07T05:00:00Z"))
    assert after == (_utc("2026-03-07T05:00:00Z"), _utc("2026-03-08T05:00:00Z"))
    assert next_day == (_utc("2026-03-08T05:00:00Z"), _utc("2026-03-09T04:00:00Z"))
    assert next_day[1] - next_day[0] == dt.timedelta(hours=23)


def test_a_fixed_time_that_happens_twice_on_the_fall_back_day_fires_once_and_the_day_is_25_hours(tmp_path):
    _workflow(tmp_path, schedule="30 1 * * *", timezone=NY)
    workflow = load_workflow(tmp_path / "daily.json")

    assert _fires("30 1 * * *", NY, "2026-10-30T12:00Z", "2026-11-02T12:00Z") == [
        "2026-10-31T05:30Z", "2026-11-01T05:30Z", "2026-11-02T06:30Z"], "01:30 EDT only, not 01:30 EST an hour later"
    on_the_day = latest_window(workflow, _utc("2026-11-01T06:30:00Z"))
    next_day = latest_window(workflow, _utc("2026-11-02T06:30:00Z"))
    assert on_the_day == (_utc("2026-10-31T04:00:00Z"), _utc("2026-11-01T04:00:00Z"))
    assert next_day == (_utc("2026-11-01T04:00:00Z"), _utc("2026-11-02T05:00:00Z"))
    assert next_day[1] - next_day[0] == dt.timedelta(hours=25)


def test_hourly_windows_across_the_clock_changes_are_one_real_hour_each_and_none_is_lost_or_run_twice(tmp_path):
    _workflow(tmp_path, schedule="0 * * * *", size="hour", timezone=NY)
    workflow = load_workflow(tmp_path / "daily.json")

    spring = windows_since(workflow, _utc("2026-03-08T04:00:00Z"), _utc("2026-03-08T09:00:00Z"), 100)
    fall = windows_since(workflow, _utc("2026-11-01T03:00:00Z"), _utc("2026-11-01T08:00:00Z"), 100)

    for found, first, count in ((spring, "2026-03-08T04:00:00Z", 5), (fall, "2026-11-01T03:00:00Z", 5)):
        assert len(found) == count
        assert found[0][0] == _utc(first) and all(end - start == dt.timedelta(hours=1) for start, end in found)
        assert all(a[1] == b[0] for a, b in zip(found, found[1:])), "laid end to end"
    assert _fires("0 * * * *", NY, "2026-11-01T03:30Z", "2026-11-01T07:30Z") == [
        "2026-11-01T04:00Z", "2026-11-01T05:00Z", "2026-11-01T06:00Z", "2026-11-01T07:00Z"], "01:00 EDT and 01:00 EST"


def test_a_tick_loads_the_23_hour_day_of_the_spring_forward_and_the_25_hour_day_of_the_fall_back(tmp_path):
    _config(tmp_path, path="../landing/{window_start:%Y-%m-%d}.csv", timezone=NY, min_rows=None)
    _workflow(tmp_path, schedule="30 3 * * *", timezone=NY)
    for day, rows in (("2026-03-07", ("00:30", "23:30")), ("2026-03-08", ("00:30", "03:30", "23:30")),
                      ("2026-11-01", ("00:30", "01:30", "23:30"))):
        _write(tmp_path / "landing" / f"{day}.csv", "id,at\n" + "\n".join(
            f"{n},{day}T{clock}:00" for n, clock in enumerate(rows)) + "\n")

    spring = tick(tmp_path, now="2026-03-09T08:00:00Z")  # 04:00 EDT on 9 March: the fire of 03:30 owns 8 March
    fall = tick(tmp_path, now="2026-11-02T09:00:00Z")  # 04:00 EST on 2 November: the fire owns 1 November

    assert _ran(spring) == [("2026-03-08T05:00:00Z/2026-03-09T04:00:00Z", "published")]
    assert _ran(fall) == [("2026-11-01T04:00:00Z/2026-11-02T05:00:00Z", "published")]
    values = _orders(tmp_path).get().column("at").to_pylist()
    assert sorted(value.strftime("%Y-%m-%d %H:%M") for value in values) == [
        "2026-03-08 00:30", "2026-03-08 03:30", "2026-03-08 23:30",
        "2026-11-01 00:30", "2026-11-01 01:30", "2026-11-01 23:30"]


def test_the_windows_since_a_time_are_laid_end_to_end_each_once_and_none_starts_before_it(tmp_path):
    _workflow(tmp_path)
    workflow = load_workflow(tmp_path / "daily.json")

    found = windows_since(workflow, _utc("2026-06-01T00:00:00Z"), _utc("2026-06-05T09:00:00Z"), 100)

    assert [(a.date().isoformat(), b.date().isoformat()) for a, b in found] == [
        ("2026-06-01", "2026-06-02"), ("2026-06-02", "2026-06-03"), ("2026-06-03", "2026-06-04"),
        ("2026-06-04", "2026-06-05")], "the fire of 5 June owns 4 June"
    assert windows_since(workflow, _utc("2026-06-01T06:00:00Z"), _utc("2026-06-05T09:00:00Z"), 100)[0][0] == _utc(
        "2026-06-02T00:00:00Z"), "a window that starts before the time is left out"
    assert len(windows_since(workflow, _utc("2026-06-01T00:00:00Z"), _utc("2026-06-05T09:00:00Z"), 2)) == 2
    assert windows_since(workflow, None, _utc("2026-06-05T09:00:00Z"), 100) == []
    assert windows_since(workflow, _utc("2026-06-06T00:00:00Z"), _utc("2026-06-05T09:00:00Z"), 100) == []


def test_several_fires_that_own_the_same_window_give_it_once(tmp_path):
    _workflow(tmp_path, schedule="0 */6 * * *")
    workflow = load_workflow(tmp_path / "daily.json")

    found = windows_since(workflow, _utc("2026-06-01T00:00:00Z"), _utc("2026-06-03T23:00:00Z"), 100)

    assert [a.date().isoformat() for a, _ in found] == ["2026-06-01", "2026-06-02"]


def test_a_window_that_is_the_time_between_two_fires_has_no_gap_and_no_overlap(tmp_path):
    _workflow(tmp_path, schedule="5 2 * * 1-5", size=None)  # weekdays at 02:05, with no size
    workflow = load_workflow(tmp_path / "daily.json")

    found = windows_since(workflow, _utc("2026-06-01T02:05:00Z"), _utc("2026-06-09T00:00:00Z"), 100)

    assert [(a.strftime("%a %H:%M"), b.strftime("%a %H:%M")) for a, b in found] == [
        ("Mon 02:05", "Tue 02:05"), ("Tue 02:05", "Wed 02:05"), ("Wed 02:05", "Thu 02:05"),
        ("Thu 02:05", "Fri 02:05"), ("Fri 02:05", "Mon 02:05")], "Friday's window runs over the weekend"
    friday = latest_window(workflow, _utc("2026-06-06T12:00:00Z"))
    assert friday == (_utc("2026-06-04T02:05:00Z"), _utc("2026-06-05T02:05:00Z"))


def test_the_due_windows_are_the_latest_one_or_every_one_since_the_start_with_catchup(tmp_path):
    _workflow(tmp_path, name="plain.json")
    _workflow(tmp_path, name="catching.json", start="2026-06-01", catchup=True)
    plain, catching = (load_workflow(tmp_path / name) for name in ("plain.json", "catching.json"))
    now = _utc("2026-06-04T09:00:00Z")

    assert due_windows(plain, now) == [(_utc("2026-06-03T00:00:00Z"), _utc("2026-06-04T00:00:00Z"))]
    assert [a.day for a, _ in due_windows(catching, now)] == [1, 2, 3]
    assert due_windows(plain, _utc("2026-06-04T09:00:00Z")) == due_windows(plain, _utc("2026-06-04T23:59:00Z"))


def test_a_catchup_over_more_windows_than_the_cap_is_refused_with_a_way_out(tmp_path, monkeypatch):
    _workflow(tmp_path, start="2026-06-01", catchup=True)
    workflow = load_workflow(tmp_path / "daily.json")
    monkeypatch.setattr(schedule_module, "MAX_ENUMERATED", 3)

    with pytest.raises(ConfigError, match="more than 3 windows since 2026-06-01T00:00:00Z. Set metadata.start to a"):
        due_windows(workflow, _utc("2026-06-20T00:00:00Z"))
    _project(tmp_path, days=3, start="2026-06-01", catchup=True)
    result = tick(tmp_path, now="2026-06-20T00:00:00Z")
    assert result.exit_code == 1 and "more than 3 windows" in result.workflows[0].error


def test_the_start_is_read_in_the_time_zone_of_the_workflow(tmp_path):
    _workflow(tmp_path, timezone="Europe/Paris", start="2026-06-02", catchup=True)
    workflow = load_workflow(tmp_path / "daily.json")

    found = due_windows(workflow, _utc("2026-06-04T09:00:00Z"))

    assert found == [(_utc("2026-06-01T22:00:00Z"), _utc("2026-06-02T22:00:00Z")),
                     (_utc("2026-06-02T22:00:00Z"), _utc("2026-06-03T22:00:00Z"))], "the Paris days of 2 and 3 June"


def test_a_catchup_over_exactly_the_cap_is_allowed(tmp_path, monkeypatch):
    _workflow(tmp_path, start="2026-06-01", catchup=True)
    workflow = load_workflow(tmp_path / "daily.json")
    monkeypatch.setattr(schedule_module, "MAX_ENUMERATED", 3)

    found = due_windows(workflow, _utc("2026-06-04T09:00:00Z"))

    assert [a.day for a, _ in found] == [1, 2, 3]
    with pytest.raises(ConfigError, match="more than 3 windows"):
        due_windows(workflow, _utc("2026-06-05T09:00:00Z"))


def test_the_windows_between_fires_look_up_the_fire_before_the_first_one_once_and_not_for_every_window(
        tmp_path, monkeypatch):
    _workflow(tmp_path, schedule="5 2 * * *", size=None)
    workflow = load_workflow(tmp_path / "daily.json")
    real, calls = schedule_module.fire_at_or_before, []

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(schedule_module, "fire_at_or_before", counting)

    found = windows_since(workflow, _utc("2026-06-01T02:05:00Z"), _utc("2026-06-09T09:00:00Z"), 100)

    assert len(found) == 8 and len(calls) == 1


def test_the_next_fire_skips_the_fires_whose_window_is_before_the_start(tmp_path):
    _workflow(tmp_path, name="plain.json")
    _workflow(tmp_path, name="late.json", start="2026-06-10")
    plain, late = (load_workflow(tmp_path / name) for name in ("plain.json", "late.json"))
    now = _utc("2026-06-04T09:00:00Z")

    assert next_fire(plain, now) == _utc("2026-06-05T00:00:00Z")
    assert next_fire(late, now) == _utc("2026-06-11T00:00:00Z"), "the fire of 11 June owns 10 June, the first window"


@pytest.mark.parametrize("value, expected", [
    ("2026-08-05T02:05:00Z", "2026-08-05T02:05:00+00:00"),
    ("2026-08-05T02:05:00z", "2026-08-05T02:05:00+00:00"),
    ("2026-08-05T09:05:00+07:00", "2026-08-05T02:05:00+00:00"),
    ("2026-08-05T02:05:00", "2026-08-05T02:05:00+00:00"),
    ("2026-08-05", "2026-08-05T00:00:00+00:00"),
    (dt.datetime(2026, 8, 5, 2, 5), "2026-08-05T02:05:00+00:00"),
    (dt.datetime(2026, 8, 5, 9, 5, tzinfo=dt.timezone(dt.timedelta(hours=7))), "2026-08-05T02:05:00+00:00"),
])
def test_the_time_of_a_tick_is_read_as_utc_unless_it_has_an_offset(value, expected):
    assert parse_now(value).isoformat() == expected


def test_a_tick_without_a_time_uses_the_current_time_and_a_bad_time_is_refused():
    assert abs((parse_now(None) - windows.now_utc()).total_seconds()) < 5
    with pytest.raises(ConfigError, match="'yesterday' is not an ISO-8601 date or datetime"):
        parse_now("yesterday")
    with pytest.raises(ConfigError, match="now must be a datetime or ISO-8601 text, got 5"):
        parse_now(5)


@pytest.mark.parametrize("end, size, expected", [
    ("2026-08-05T00:00:00Z", "day", "2026-08-04T00:00:00Z"),
    ("2026-08-05T10:00:00Z", "hour", "2026-08-05T09:00:00Z"),
    ("2026-08-05T10:15:00Z", "15m", "2026-08-05T10:00:00Z"),
])
def test_the_window_ending_at_a_boundary_is_one_size_long(end, size, expected):
    assert windows.window_ending_at(_utc(end), size) == (_utc(expected), _utc(end))


def test_the_day_ending_at_a_local_midnight_is_23_or_25_hours_long_across_a_clock_change():
    spring = windows.window_ending_at(_utc("2026-03-09T04:00:00Z"), "day", NY)
    fall = windows.window_ending_at(_utc("2026-11-02T05:00:00Z"), "day", NY)

    assert spring == (_utc("2026-03-08T05:00:00Z"), _utc("2026-03-09T04:00:00Z"))
    assert fall == (_utc("2026-11-01T04:00:00Z"), _utc("2026-11-02T05:00:00Z"))


def test_a_catchup_without_a_start_is_not_a_valid_workflow(tmp_path):
    _workflow(tmp_path, catchup=True)

    with pytest.raises(ConfigError, match=r"metadata.catchup: is true, and a catch-up needs metadata.start"):
        load_workflow(tmp_path / "daily.json")
    _workflow(tmp_path, catchup=False)
    assert load_workflow(tmp_path / "daily.json").metadata["catchup"] is False


# ====================================================================== finding what is scheduled


def test_discovery_finds_scheduled_workflows_and_configs_told_apart_by_kind_and_sorted_by_path(tmp_path):
    _project(tmp_path, days=1)
    _config(tmp_path, name="hourly.json", identifier="hourly", schedule="@hourly", size="hour", table="hourly_orders")
    _workflow(tmp_path, name="b/weekly.json", identifier="weekly", schedule="@weekly",
              tasks=[{"id": "orders", "pipeline": "../config/orders.json"}])

    found = discover(tmp_path)

    assert [(item.path.relative_to(tmp_path).as_posix(), item.kind, item.workflow.identifier)
            for item in found.scheduled] == [("b/weekly.json", "Workflow", "weekly"),
                                             ("config/hourly.json", "Pipeline", "hourly"),
                                             ("daily.json", "Workflow", "daily_orders")]
    assert found.problems == []


def test_a_config_whose_when_means_a_schedule_is_scheduled(tmp_path):
    _config(tmp_path, extra={"quality": [{"check": "row_count", "min": 1}]})
    spec = json.loads((tmp_path / "config" / "orders.json").read_text())
    spec["when"] = "daily"
    _write(tmp_path / "config" / "orders.json", spec)

    [item] = discover(tmp_path).scheduled

    assert item.kind == "Pipeline" and item.workflow.schedule.expression == "@daily"


def test_discovery_leaves_out_what_has_no_schedule_what_is_no_spec_and_the_folders_it_does_not_enter(
        tmp_path, monkeypatch):
    _project(tmp_path, days=1)  # config/orders.json has no schedule, and daily.json has one
    _workflow(tmp_path, name="manual.json", identifier="manual", schedule=None)
    _write(tmp_path / "package.json", {"name": "a web app", "version": "1.0.0", "scripts": {"build": "x"}})
    _write(tmp_path / "list.json", ["not", "an", "object"])
    _write(tmp_path / "broken.json", "{ not json")
    _write(tmp_path / "other.json", {"kind": "Dashboard", "identifier": "x", "metadata": {"schedule": "@daily"}})
    _write(tmp_path / "v1.metadata.json", json.loads((tmp_path / "daily.json").read_text()))
    for hidden in (".ldp", ".venv/lib", "node_modules/x", "venv", "env", "site-packages"):
        _write(tmp_path / hidden / "daily.json", json.loads((tmp_path / "daily.json").read_text()))
    _write(tmp_path / "huge.json", json.loads((tmp_path / "daily.json").read_text()))
    monkeypatch.setattr(schedule_module, "MAX_SPEC_BYTES", 100)

    found = discover(tmp_path)

    assert found.scheduled == [] and found.problems == [], "the only scheduled file is larger than the limit"
    monkeypatch.setattr(schedule_module, "MAX_SPEC_BYTES", 1_000_000)
    assert [item.path.relative_to(tmp_path).as_posix() for item in discover(tmp_path).scheduled] == [
        "daily.json", "huge.json"], "and the same file is found when it is not"


def test_a_file_of_a_kind_that_is_not_a_workflow_or_a_config_is_left_out_even_with_a_schedule(tmp_path, monkeypatch):
    monkeypatch.setitem(spec._KINDS, "Dashboard", spec.SpecKind("Dashboard", lambda data: [], dict, dict))
    _write(tmp_path / "dashboard.json", {"kind": "Dashboard", "identifier": "d", "metadata": {"schedule": "@daily"}})
    _write(tmp_path / "an_app.json", {"name": "an app", "metadata": {"schedule": "@daily", "retention": 7}})

    found = discover(tmp_path)

    assert found.scheduled == [] and found.problems == [], "neither is a workflow, and neither is a config"
    _project(tmp_path, days=1)
    assert [item.workflow.identifier for item in discover(tmp_path).scheduled] == ["daily_orders"]


def test_a_file_with_a_schedule_that_cannot_be_used_makes_a_tick_exit_with_one_though_nothing_else_failed(
        tmp_path, capsys):
    _project(tmp_path, days=3)
    _workflow(tmp_path, name="bad.json", identifier="bad", tasks=[{"id": "x"}])
    tick(tmp_path, now=_at(3))

    again = tick(tmp_path, now=_at(3, "02:10"))

    assert again.counts() == {**NO_ROWS, "skipped": 1} and all(item.exit_code == 0 for item in again.workflows)
    assert again.exit_code == 1 and again.status == "failed"


@pytest.mark.parametrize("value", [0, -1, True, "3", 2.5, None])
def test_the_cap_of_a_tick_is_a_whole_number_from_one(tmp_path, value):
    with pytest.raises(ConfigError, match="max_windows must be a whole number from 1"):
        tick(tmp_path, max_windows=value)


def test_a_file_that_cannot_be_used_is_a_problem_and_the_others_are_still_found_even_when_it_is_the_only_one(
        tmp_path):
    _workflow(tmp_path, name="bad.json", identifier="bad", tasks=[{"id": "x"}])

    found = discover(tmp_path)

    assert found.scheduled == [] and [path.name for path, _ in found.problems] == ["bad.json"]


def test_a_file_that_has_a_schedule_and_cannot_be_used_is_a_problem_and_the_others_are_still_found(tmp_path):
    _project(tmp_path, days=1)
    _workflow(tmp_path, name="a_bad_task.json", identifier="bad", tasks=[{"id": "x", "no_such_kind": 1}])
    _config(tmp_path, name="bad_schedule.json", identifier="bad_schedule", schedule="every night")

    found = discover(tmp_path)

    assert [item.workflow.identifier for item in found.scheduled] == ["daily_orders"]
    assert [(path.name, lines[0][:44]) for path, lines in found.problems] == [
        ("a_bad_task.json", "a_bad_task.json has 2 problems:"),
        ("bad_schedule.json", "metadata.schedule: schedule 'every night' is")]


def test_discovery_of_something_that_is_not_a_folder_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="is not a folder"):
        discover(tmp_path / "missing")
    _write(tmp_path / "file.json", {})
    with pytest.raises(ConfigError, match="file.json is not a folder"):
        tick(tmp_path / "file.json")
    with pytest.raises(ConfigError, match="is not a folder"):
        list_scheduled(tmp_path / "missing")


def test_a_scheduled_config_is_ticked_as_a_workflow_of_one_task_with_its_events_next_to_it(tmp_path):
    _drop(tmp_path, 2)
    _config(tmp_path, schedule="@daily")

    result = tick(tmp_path, now=_at(3))

    assert [(item.kind, item.workflow) for item in result.workflows] == [("Pipeline", "orders")]
    assert _ran(result) == [(_span(2), "published")] and _loaded_days(tmp_path) == [2]
    assert (tmp_path / "config" / ".ldp" / "events.jsonl").is_file() and not _ledger(tmp_path).exists()
    assert tick(tmp_path, now=_at(3, "02:10")).counts()["skipped"] == 1


def test_a_tick_of_a_folder_with_nothing_scheduled_does_nothing_and_exits_with_zero(tmp_path, capsys):
    _config(tmp_path)  # no schedule

    result = tick(tmp_path, now=_at(3))
    status = main(["schedule", "tick", str(tmp_path), "--now", _at(3)])

    assert result.workflows == [] and result.exit_code == 0 and result.counts() == NO_ROWS
    out, err = capsys.readouterr()
    assert status == 0 and err == "" and out.splitlines()[0].endswith(
        "0 workflows, 0 windows due, 0 published, 0 skipped, 0 blocked, 0 failed in 0.0s")
    files = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file())
    assert files == [".ldp/locks/schedule.lock", "config/orders.json"], "a tick that has nothing to run makes its lock"


# ====================================================================== listing


def test_a_listing_names_the_schedule_the_zone_the_next_fire_in_utc_and_local_time_and_the_last_window(tmp_path):
    _project(tmp_path, days=3, schedule="5 2 * * *", timezone="Europe/Paris")
    tick(tmp_path, now=_at(3))  # 02:05 UTC is 04:05 in Paris, so the fire of 3 June at 02:05 Paris time has passed

    [item] = list_scheduled(tmp_path, now=_at(3)).items

    assert (item.workflow, item.kind, item.cron, item.timezone) == ("daily_orders", "Workflow", "5 2 * * *",
                                                                    "Europe/Paris")
    assert item.window_size == "day" and item.catchup is False and item.start is None
    assert item.next_fire == _utc("2026-06-04T00:05:00Z"), "02:05 in Paris on 4 June"
    assert item.last_window == "2026-06-01T22:00:00Z/2026-06-02T22:00:00Z" and item.last_status == "succeeded"
    shown = item.to_dict(dt.timezone(dt.timedelta(hours=7)))
    assert shown["next_fire"] == "2026-06-04T00:05:00Z" and shown["next_fire_local"] == "2026-06-04T07:05:00+07:00"
    assert shown["schedule"] == "5 2 * * *" and shown["timezone"] == "Europe/Paris"


def test_a_listing_with_nothing_run_yet_says_so_and_the_first_window_is_due(tmp_path):
    _project(tmp_path, days=3)

    [item] = list_scheduled(tmp_path, now=_at(3)).items

    assert item.last_window is None and item.last_status is None
    assert _texts(item.due) == [_span(2)] and item.missed == [] and item.commands == []


@pytest.mark.parametrize("make, status", [
    ("succeeded", "succeeded"), ("failed", "failed"), ("blocked", "blocked"), ("unfinished", "unfinished"),
    ("incomplete", "incomplete")])
def test_a_listing_says_how_the_last_run_of_the_last_window_ended(tmp_path, make, status):
    two_tasks = [{"id": "orders", "pipeline": "config/orders.json"},
                 {"id": "tidy", "maintain": {"config": "config/orders.json"}, "depends_on": ["orders"]}]
    _project(tmp_path, days=3, tasks=two_tasks if make == "incomplete" else None)
    window = _span(2).split("/")
    if make == "failed":
        (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()
    if make == "blocked":
        _drop(tmp_path, 2, rows=1)
    if make in ("unfinished", "incomplete"):
        events = [{"type": "workflow.started", "run_id": "r", "attempt": 1,
                   "payload": {"workflow": "daily_orders", "window": window}}]
        if make == "incomplete":
            events.append({"type": "workflow.finished", "run_id": "r", "attempt": 1,
                           "payload": {"workflow": "daily_orders", "tasks": {"orders": "published"}}})
        _write(_ledger(tmp_path), "\n".join(json.dumps(event) for event in events) + "\n")
    else:
        tick(tmp_path, now=_at(3))

    [item] = list_scheduled(tmp_path, now=_at(3, "02:10")).items

    assert item.last_window == _span(2) and item.last_status == status


def test_the_command_that_a_listing_prints_for_missed_windows_fills_them(tmp_path, capsys):
    _project(tmp_path, days=8)
    tick(tmp_path, now=_at(2))  # day 1
    tick(tmp_path, now=_at(8))  # day 7; days 2 to 6 were missed
    [item] = list_scheduled(tmp_path, now=_at(8)).items
    assert _texts(item.missed) == [_span(n) for n in range(2, 7)] and item.due == []
    [command] = item.commands
    assert command == f"ldp backfill {tmp_path / 'daily.json'} --from {_day(2)} --to {_day(7)}"
    capsys.readouterr()

    status = main(shlex.split(command)[1:])

    assert status == 0 and _loaded_days(tmp_path) == list(range(1, 8))
    [after] = list_scheduled(tmp_path, now=_at(8)).items
    assert after.missed == [] and after.commands == []


def test_a_listing_of_windows_with_no_size_prints_one_workflow_run_for_each_missed_window(tmp_path, capsys):
    for n in range(1, 6):
        _drop(tmp_path, n)
    _config(tmp_path, size=None, path="../landing/orders_{window_end:%Y-%m-%d}.csv")  # the file of the fire
    _workflow(tmp_path, schedule="5 2 * * *", size=None, start="2026-06-01T02:05:00Z")
    tick(tmp_path, now="2026-06-03T09:00:00Z")  # the window from 2 June 02:05 to 3 June 02:05
    now = "2026-06-05T09:00:00Z"

    [item] = list_scheduled(tmp_path, now=now).items

    assert item.error is None and item.window_size is None
    assert item.last_window == "2026-06-02T02:05:00Z/2026-06-03T02:05:00Z" and _loaded_days(tmp_path) == [3]
    assert _texts(item.due) == ["2026-06-04T02:05:00Z/2026-06-05T02:05:00Z"]
    assert _texts(item.missed) == ["2026-06-01T02:05:00Z/2026-06-02T02:05:00Z",
                                   "2026-06-03T02:05:00Z/2026-06-04T02:05:00Z"]
    assert item.commands == [
        f"ldp workflow run {tmp_path / 'daily.json'} --window 2026-06-01T02:05:00Z/2026-06-02T02:05:00Z",
        f"ldp workflow run {tmp_path / 'daily.json'} --window 2026-06-03T02:05:00Z/2026-06-04T02:05:00Z"]
    capsys.readouterr()
    assert main(shlex.split(item.commands[0])[1:]) == 0
    assert _loaded_days(tmp_path) == [2, 3], "the file of the fire on 2 June"


def test_with_catchup_every_window_that_is_not_done_is_due_and_none_is_missed(tmp_path):
    _project(tmp_path, days=8, start=_day(1).isoformat(), catchup=True)
    tick(tmp_path, now=_at(3), max_windows=1)  # days 1 and 2 are due, and day 1 runs

    [item] = list_scheduled(tmp_path, now=_at(5)).items

    assert item.catchup is True and item.start == _utc("2026-06-01T00:00:00Z")
    assert _texts(item.due) == [_span(n) for n in (2, 3, 4)] and item.missed == [] and item.commands == []


def test_a_listing_names_at_most_max_windows_missed_windows_the_oldest_first_and_says_there_are_more(tmp_path):
    _project(tmp_path, days=12)
    tick(tmp_path, now=_at(2))  # day 1

    [item] = list_scheduled(tmp_path, now=_at(12), max_windows=3).items

    assert _texts(item.missed) == [_span(n) for n in (2, 3, 4)] and item.more_missed is True
    assert item.commands == [f"ldp backfill {tmp_path / 'daily.json'} --from {_day(2)} --to {_day(5)}"]
    assert list_scheduled(tmp_path, now=_at(12), max_windows=20).items[0].more_missed is False
    exact = list_scheduled(tmp_path, now=_at(12), max_windows=9).items[0]
    assert len(exact.missed) == 9 and exact.more_missed is False, "as many as the limit is not more"
    assert list_scheduled(tmp_path, now=_at(12), max_windows=8).items[0].more_missed is True
    with pytest.raises(ConfigError, match="max_windows must be a whole number from 1"):
        list_scheduled(tmp_path, max_windows=0)


def test_a_listing_writes_nothing_and_reads_while_a_tick_holds_the_lock(tmp_path):
    _project(tmp_path, days=3)
    tick(tmp_path, now=_at(3))
    before = sorted((path.relative_to(tmp_path).as_posix(), path.stat().st_mtime_ns)
                    for path in tmp_path.rglob("*") if path.is_file())

    with file_lock(tmp_path / LOCK_PATH):
        listing = list_scheduled(tmp_path, now=_at(4))

    after = sorted((path.relative_to(tmp_path).as_posix(), path.stat().st_mtime_ns)
                   for path in tmp_path.rglob("*") if path.is_file())
    assert [item.workflow for item in listing.items] == ["daily_orders"] and after == before


def test_a_workflow_that_cannot_be_read_is_listed_with_its_error_and_the_others_are_still_listed(
        tmp_path, monkeypatch):
    _project(tmp_path, days=3)
    _workflow(tmp_path, name="a_first.json", identifier="a_first")
    real = schedule_module._status_of

    def explode_for_the_first(item, now, limit):
        if item.workflow.identifier == "a_first":
            raise RuntimeError("a bug")
        return real(item, now, limit)

    monkeypatch.setattr(schedule_module, "_status_of", explode_for_the_first)

    listing = list_scheduled(tmp_path, now=_at(3))

    assert [(item.workflow, item.error) for item in listing.items] == [
        ("a_first", "RuntimeError: a bug"), ("daily_orders", None)]
    assert listing.to_dict()["scheduled"][0]["error"] == "RuntimeError: a bug"


def test_a_listing_reports_a_file_with_a_schedule_that_cannot_be_used(tmp_path):
    _project(tmp_path, days=1)
    _workflow(tmp_path, name="bad.json", identifier="bad", tasks=[{"id": "x"}])

    listing = list_scheduled(tmp_path, now=_at(2))

    assert [item.workflow for item in listing.items] == ["daily_orders"]
    assert [path.name for path, _ in listing.problems] == ["bad.json"]
    shown = listing.to_dict()
    assert shown["problems"][0]["file"].endswith("bad.json") and shown["problems"][0]["lines"]
    json.dumps(shown)


@pytest.mark.parametrize("path, spans, expected", [
    ("daily.json", [], []),
    ("daily.json", [("2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z"), ("2026-06-05T00:00:00Z", "2026-06-06T00:00:00Z")],
     ["ldp backfill daily.json --from 2026-06-02 --to 2026-06-06"]),
    ("my flows/daily.json", [("2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z")],
     ["ldp backfill 'my flows/daily.json' --from 2026-06-02 --to 2026-06-03"]),
])
def test_one_backfill_command_fills_the_missed_windows_of_a_size_from_the_first_to_the_last(
        tmp_path, path, spans, expected):
    _workflow(tmp_path)
    workflow = load_workflow(tmp_path / "daily.json")

    found = fill_commands(Path(path), workflow, [(_utc(a), _utc(b)) for a, b in spans])

    assert found == expected


def test_a_backfill_command_gives_a_local_date_for_a_local_midnight_and_the_instant_for_any_other_bound(tmp_path):
    _workflow(tmp_path, name="paris.json", timezone="Europe/Paris")
    _workflow(tmp_path, name="hourly.json", schedule="@hourly", size="hour")
    paris, hourly = (load_workflow(tmp_path / name) for name in ("paris.json", "hourly.json"))

    days = fill_commands(Path("paris.json"), paris, [(_utc("2026-06-01T22:00:00Z"), _utc("2026-06-02T22:00:00Z"))])
    hours = fill_commands(Path("hourly.json"), hourly, [(_utc("2026-06-02T10:00:00Z"), _utc("2026-06-02T11:00:00Z"))])

    assert days == ["ldp backfill paris.json --from 2026-06-02 --to 2026-06-03"]
    assert hours == ["ldp backfill hourly.json --from 2026-06-02T10:00:00Z --to 2026-06-02T11:00:00Z"]


# ====================================================================== the command line


def _ldp(capsys, *argv) -> tuple[int, str, str]:
    status = main(["schedule", *[str(item) for item in argv]])
    out, err = capsys.readouterr()
    return status, out, err


@pytest.fixture
def local_zone(monkeypatch):
    """Set the time zone of the machine, which is what 'local time' means, and set it back."""
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


def test_ldp_schedule_tick_prints_a_line_for_each_window_that_ran_and_a_summary(tmp_path, capsys):
    _project(tmp_path, days=3)

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3))

    lines = out.splitlines()
    assert status == 0 and err == "" and len(lines) == 2
    assert lines[0] == f"daily_orders  {_span(2)}  published"
    assert lines[1].startswith("tick 2026-06-03T02:05:00Z: 1 workflow, 1 window due, 1 published, 0 skipped, "
                               "0 blocked, 0 failed in ") and lines[1].endswith("s")


def test_ldp_schedule_tick_takes_the_cap_of_windows_to_run_from_the_command_line(tmp_path, capsys):
    _project(tmp_path, days=10, start=_day(1).isoformat(), catchup=True)

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(10), "--max-windows", "2")

    lines = out.splitlines()
    assert status == 0 and lines[:2] == [f"daily_orders  {_span(1)}  published", f"daily_orders  {_span(2)}  published"]
    assert "9 windows due, 2 published, 0 skipped, 0 blocked, 0 failed, 7 not run" in lines[2]
    assert _loaded_days(tmp_path) == [1, 2]


def test_ldp_schedule_tick_run_again_lists_nothing_and_counts_the_window_it_found_done(tmp_path, capsys):
    _project(tmp_path, days=3)
    _ldp(capsys, "tick", tmp_path, "--now", _at(3))

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3, "02:10"))

    assert status == 0 and err == "" and len(out.splitlines()) == 1
    assert out.startswith("tick 2026-06-03T02:10:00Z: 1 workflow, 0 windows due, 0 published, 1 skipped, 0 blocked, "
                          "0 failed in ")


def test_ldp_schedule_tick_names_what_went_wrong_in_a_failed_window_and_says_the_next_tick_tries_again(
        tmp_path, capsys):
    _project(tmp_path, days=3)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3))
    again, out_again, err_again = _ldp(capsys, "tick", tmp_path, "--now", _at(3, "02:10"))

    assert status == 1 and f"daily_orders  {_span(2)}  failed\n" in out and "1 failed" in out
    assert "  orders: FileNotFoundError: CSV file not found" in err
    assert err.splitlines()[-1] == "The next tick runs the windows that failed or were blocked again"
    assert again == 1 and f"daily_orders  {_span(2)}  failed  (attempt 2)\n" in out_again


def test_ldp_schedule_tick_exits_with_three_when_only_a_data_gate_held_a_window_back(tmp_path, capsys):
    _project(tmp_path, days=3)
    _drop(tmp_path, 2, rows=1)

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3))

    assert status == 3 and f"daily_orders  {_span(2)}  blocked\n" in out and "0 published" in out and "1 blocked" in out
    assert ("  orders: DataQualityError: 1 of 1 data quality checks failed:\n"
            "    - row_count: 1 row, expected >= 2\n") in err


def test_ldp_schedule_tick_json_prints_the_result_and_nothing_else(tmp_path, capsys):
    _project(tmp_path, days=3)

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3), "--json")

    printed = json.loads(out)
    assert status == 0 and err == "" and printed["status"] == "succeeded" and printed["exit_code"] == 0
    assert printed["counts"] == {**NO_ROWS, "published": 1} and printed["now"] == "2026-06-03T02:05:00Z"
    [workflow] = printed["workflows"]
    assert workflow["workflow"] == "daily_orders" and workflow["kind"] == "Workflow" and workflow["due"] == 1
    assert workflow["windows"][0]["window"] == _span(2) and workflow["windows"][0]["outcome"] == "published"
    assert workflow["windows"][0]["attempt"] == 1


def test_ldp_schedule_tick_prints_a_workflow_that_could_not_be_ticked_and_a_file_that_cannot_be_used(
        tmp_path, capsys):
    _project(tmp_path, days=3)
    _workflow(tmp_path, name="a_missing_config.json", identifier="a_missing",
              tasks=[{"id": "x", "pipeline": "config/nothing.json"}])
    _workflow(tmp_path, name="b_invalid.json", identifier="b_invalid", tasks=[{"id": "x"}])

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3))

    assert status == 1 and f"daily_orders  {_span(2)}  published" in out
    assert "a_missing  could not be ticked\n  workflow a_missing is not valid, so no window ran:" in err
    assert f"{tmp_path / 'b_invalid.json'}  has a schedule and cannot be used\n  b_invalid.json has 1 problem" in err
    assert err.splitlines()[-1] == "The next tick runs the windows that failed or were blocked again"


def test_ldp_schedule_tick_json_has_a_workflow_with_nothing_due_one_that_could_not_be_ticked_and_a_refused_file(
        tmp_path, capsys):
    _project(tmp_path, days=3, start=_day(3).isoformat())
    _workflow(tmp_path, name="a_missing.json", identifier="a_missing",
              tasks=[{"id": "x", "pipeline": "config/nothing.json"}])
    _workflow(tmp_path, name="b_bad.json", identifier="b_bad", tasks=[{"id": "x"}])

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(2), "--json")

    printed = json.loads(out)
    assert (status, printed["status"], printed["exit_code"], printed["overlapped"]) == (1, "failed", 1, False)
    missing, daily = printed["workflows"]
    assert missing["workflow"] == "a_missing" and missing["exit_code"] == 1 and missing["windows"] == []
    assert "is not valid, so no window ran" in missing["error"]
    assert (daily["workflow"], daily["due"], daily["windows"], daily["error"]) == ("daily_orders", 0, [], None)
    [problem] = printed["problems"]
    assert problem["file"].endswith("b_bad.json") and problem["lines"][0].startswith("b_bad.json has")
    assert printed["counts"] == NO_ROWS and printed["duration_s"] >= 0


def test_ldp_schedule_tick_json_of_a_tick_that_found_another_tick_running_says_so(tmp_path, capsys):
    _project(tmp_path, days=3)

    with file_lock(tmp_path / LOCK_PATH):
        status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3), "--json")

    printed = json.loads(out)
    assert status == 0 and printed["overlapped"] is True and printed["status"] == "overlapped"
    assert printed["workflows"] == [] and printed["exit_code"] == 0


@pytest.mark.parametrize("argv, message", [
    (["tick", "{dir}", "--now", "yesterday"], "ldp: error: now 'yesterday' is not an ISO-8601 date or datetime"),
    (["tick", "{dir}/nothing"], "is not a folder"),
    (["list", "{dir}/nothing"], "is not a folder"),
])
def test_ldp_schedule_reports_a_bad_time_and_a_missing_folder_with_exit_status_one(tmp_path, capsys, argv, message):
    status, out, err = _ldp(capsys, *[part.replace("{dir}", str(tmp_path)) for part in argv])

    assert status == 1 and out == "" and message in err


@pytest.mark.parametrize("argv", [["tick"], ["tick", "{dir}", "--max-windows", "0"],
                                  ["list", "{dir}", "--max-windows", "x"], ["install", "{dir}"],
                                  ["install", "{dir}", "--print", "systemd"], ["nothing"]])
def test_ldp_schedule_reports_a_wrong_command_line_with_exit_status_one(tmp_path, capsys, argv):
    status, out, err = _ldp(capsys, *[part.replace("{dir}", str(tmp_path)) for part in argv])

    assert status == 1 and "usage: ldp schedule" in err and not (tmp_path / ".ldp").exists()


def test_ldp_schedule_alone_prints_its_usage_and_exits_with_one(capsys):
    status, out, err = _ldp(capsys)

    assert status == 1 and "list" in out and "tick" in out and "install" in out
    assert "Exit status is 0 on success, 1 on an error, and 3 when the only problem of a tick is a data gate" in (
        " ".join(out.split()))


def test_ldp_schedule_tick_says_how_far_it_got_when_it_is_interrupted_and_gives_up_the_lock(
        tmp_path, capsys, monkeypatch):
    _project(tmp_path, days=3)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(schedule_module, "run_windows", interrupted)

    status, out, err = _ldp(capsys, "tick", tmp_path, "--now", _at(3))

    assert status == 130 and "ldp: interrupted" in err
    monkeypatch.undo()
    assert _ldp(capsys, "tick", tmp_path, "--now", _at(3))[0] == 0


def test_ldp_schedule_list_prints_a_block_for_each_workflow(tmp_path, capsys, local_zone):
    local_zone("Asia/Bangkok")
    _project(tmp_path, days=3, schedule="5 2 * * *", timezone="Europe/Paris")
    tick(tmp_path, now=_at(3))

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(3))

    assert status == 0 and err == ""
    assert out.splitlines() == [
        f"1 workflow or config with a schedule under {tmp_path} at 2026-06-03T02:05:00Z",
        "",
        f"daily_orders  Workflow  {tmp_path / 'daily.json'}",
        "  schedule     5 2 * * *, in Europe/Paris, windows of one day",
        "  next fire    2026-06-04T00:05:00Z, 2026-06-04T07:05:00+07:00 local",
        "  last window  2026-06-01T22:00:00Z/2026-06-02T22:00:00Z succeeded",
        "  due          nothing",
        "  missed       none"]


def test_ldp_schedule_list_prints_what_is_due_and_what_was_missed_with_the_command_to_fill_it(tmp_path, capsys):
    _project(tmp_path, days=8)
    _ldp(capsys, "tick", tmp_path, "--now", _at(2))

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(8))

    lines = out.splitlines()
    assert status == 0 and err == "" and lines[2] == f"daily_orders  Workflow  {tmp_path / 'daily.json'}"
    assert lines[3] == "  schedule     @daily, in UTC, windows of one day"
    assert lines[5] == f"  last window  {_span(1)} succeeded"
    assert lines[6] == f"  due          1 window, {_span(7)}"
    assert lines[7:] == ["  missed       5 windows. Fill them with",
                         f"    ldp backfill {tmp_path / 'daily.json'} --from {_day(2)} --to {_day(7)}"]


def test_ldp_schedule_list_says_catchup_and_when_a_window_is_the_time_between_two_fires(tmp_path, capsys):
    _project(tmp_path, days=8, start=_day(1).isoformat(), catchup=True)
    _workflow(tmp_path, name="between.json", identifier="between", schedule="5 2 * * *", size=None)

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(5))

    assert status == 0
    assert f"  catch-up     on, since {_day(1)}T00:00:00Z" in out.splitlines()
    assert "  schedule     5 2 * * *, in UTC, a window is the time between two fires" in out.splitlines()
    assert f"  due          4 windows, {_day(1)}T00:00:00Z/{_day(5)}T00:00:00Z" in out.splitlines()


def test_ldp_schedule_list_names_the_length_of_the_windows_in_words(tmp_path, capsys):
    _config(tmp_path)
    _workflow(tmp_path, name="hourly.json", identifier="hourly", schedule="@hourly", size="hour")
    _workflow(tmp_path, name="quarter.json", identifier="quarter", schedule="*/15 * * * *", size="15m")
    _workflow(tmp_path, name="daily.json", identifier="daily")

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(5))

    lines = out.splitlines()
    assert status == 0 and lines.count("  schedule     @hourly, in UTC, windows of one hour") == 1
    assert "  schedule     */15 * * * *, in UTC, windows of 15 minutes" in lines
    assert "  schedule     @daily, in UTC, windows of one day" in lines


def test_ldp_schedule_list_says_when_nothing_ran_yet_and_when_it_names_only_some_of_the_missed_windows(
        tmp_path, capsys):
    _project(tmp_path, days=12)

    fresh = _ldp(capsys, "list", tmp_path, "--now", _at(12))[1].splitlines()
    _ldp(capsys, "tick", tmp_path, "--now", _at(2))
    cut = _ldp(capsys, "list", tmp_path, "--now", _at(12), "--max-windows", "3")[1].splitlines()

    assert "  last window  none yet" in fresh and "  missed       none" in fresh
    assert "  missed       3 windows, and more after them. Fill them with" in cut
    assert cut[-1] == f"    ldp backfill {tmp_path / 'daily.json'} --from {_day(2)} --to {_day(5)}"
    one = _ldp(capsys, "list", tmp_path, "--now", _at(12), "--max-windows", "1")[1].splitlines()
    assert "  missed       1 window, and more after them. Fill it with" in one


def test_ldp_schedule_list_json_prints_the_listing_and_nothing_else(tmp_path, capsys):
    _project(tmp_path, days=3)

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(3), "--json")

    printed = json.loads(out)
    assert status == 0 and err == "" and printed["now"] == "2026-06-03T02:05:00Z"
    [item] = printed["scheduled"]
    assert item["workflow"] == "daily_orders" and item["schedule"] == "@daily" and item["due"] == [_span(2)]
    assert item["next_fire"] == "2026-06-04T00:00:00Z" and item["missed"] == [] and item["error"] is None
    assert item["catchup"] is False and item["start"] is None and item["commands"] == []


def test_ldp_schedule_list_json_has_the_start_the_missed_windows_and_the_commands_that_fill_them(tmp_path, capsys):
    _project(tmp_path, days=8)
    _ldp(capsys, "tick", tmp_path, "--now", _at(2))
    _workflow(tmp_path, name="catching.json", identifier="catching", start=_day(1).isoformat(), catchup=True)

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(8), "--json")

    daily, catching = json.loads(out)["scheduled"][1], json.loads(out)["scheduled"][0]
    assert catching["workflow"] == "catching" and catching["catchup"] is True
    assert catching["start"] == f"{_day(1)}T00:00:00Z" and catching["commands"] == []
    assert daily["workflow"] == "daily_orders" and daily["missed"] == [_span(n) for n in range(2, 7)]
    assert daily["more_missed"] is False and daily["commands"] == [
        f"ldp backfill {tmp_path / 'daily.json'} --from {_day(2)} --to {_day(7)}"]


def test_ldp_schedule_list_prints_a_file_that_cannot_be_used_and_exits_with_one(tmp_path, capsys):
    _project(tmp_path, days=1)
    _workflow(tmp_path, name="bad.json", identifier="bad", tasks=[{"id": "x"}])

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(2))

    assert status == 1 and f"{tmp_path / 'bad.json'}  has a schedule and cannot be used" in out


def test_ldp_schedule_list_prints_the_error_of_a_workflow_that_cannot_be_read(tmp_path, capsys, monkeypatch):
    _project(tmp_path, days=3)

    def broken(item, now, limit):
        raise RuntimeError("a bug")

    monkeypatch.setattr(schedule_module, "_status_of", broken)

    status, out, err = _ldp(capsys, "list", tmp_path, "--now", _at(3))

    assert status == 0 and "  error        RuntimeError: a bug" in out.splitlines()


# ====================================================================== the recipes

EXE = "/opt/venvs/ldp/bin/ldp"
POSIX = pytest.mark.skipif(os.name == "nt", reason="a crontab line is for a shell")


def test_the_cron_recipe_is_a_comment_and_a_line_that_ticks_every_five_minutes_by_absolute_paths(tmp_path):
    folder = tmp_path.resolve()

    text = recipes.cron_recipe(tmp_path, EXE)

    comment, line = text.splitlines()
    assert text.endswith("\n") and comment == f"# ldp schedule tick for {folder}, every 5 minutes"
    *when, command = line.split(" ", 5)
    assert when == ["*/5", "*", "*", "*", "*"] and windows.parse_schedule(" ".join(when)).cron == "*/5 * * * *"
    assert command == (f"mkdir -p {folder}/.ldp && {EXE} schedule tick {folder} >> {folder}/.ldp/schedule.log 2>&1")


@POSIX
def test_the_command_of_the_cron_recipe_makes_the_log_folder_first_and_runs_the_tick_with_the_folder(tmp_path):
    folder = tmp_path / "my project"
    folder.mkdir()
    fake = tmp_path / "bin" / "ldp"
    _write(fake, '#!/bin/sh\necho "called with $@"\n')
    fake.chmod(0o755)
    command = recipes.cron_recipe(folder, fake).splitlines()[1].split(" ", 5)[5]

    subprocess.run(["sh", "-c", command], check=True, cwd=tmp_path)

    assert (folder / ".ldp" / "schedule.log").read_text() == f"called with schedule tick {folder.resolve()}\n"


@POSIX
def test_the_cron_recipe_quotes_a_path_with_spaces_and_escapes_the_percent_sign_that_cron_reads_as_a_newline(tmp_path):
    folder = tmp_path / "50% off (copy)"
    folder.mkdir()

    command = recipes.cron_recipe(folder, "/opt/my tools/ldp").splitlines()[1].split(" ", 5)[5]

    assert "\\%" in command and "%" not in command.replace("\\%", "")
    as_the_shell_sees_it = command.replace("\\%", "%")  # cron turns \% back into %
    subprocess.run(["sh", "-n", "-c", as_the_shell_sees_it], check=True)
    words = shlex.split(as_the_shell_sees_it)
    assert words[:3] == ["mkdir", "-p", f"{folder.resolve()}/.ldp"]
    assert words[4:8] == ["/opt/my tools/ldp", "schedule", "tick", str(folder.resolve())]


def test_the_launchd_recipe_is_a_plist_that_ticks_every_five_minutes_and_when_it_loads(tmp_path):
    folder, home = tmp_path.resolve(), tmp_path / "home"

    text = recipes.launchd_recipe(tmp_path, EXE, home=home)

    label = recipes.label_of(tmp_path)
    log = str(home / "Library" / "Logs" / f"{label}.log")
    assert plistlib.loads(text.encode("utf-8")) == {
        "Label": label, "ProgramArguments": [EXE, "schedule", "tick", str(folder)], "StartInterval": 300,
        "RunAtLoad": True, "WorkingDirectory": str(folder), "StandardOutPath": log, "StandardErrorPath": log}
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist')
    assert text.index("<!--") < text.index("<plist") and text.endswith("</plist>\n")
    assert f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist" in text
    assert f"launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/{label}.plist" in text


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is a macOS tool")
def test_macos_accepts_the_launchd_recipe_as_a_property_list(tmp_path):
    path = tmp_path / "recipe.plist"
    path.write_text(recipes.launchd_recipe(tmp_path, EXE, home=tmp_path), encoding="utf-8")

    done = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)

    assert done.returncode == 0 and done.stdout.strip().endswith("OK")


def test_the_comment_of_the_launchd_recipe_never_holds_two_hyphens_in_a_row_which_xml_forbids(tmp_path):
    folder = tmp_path / "a--b"
    folder.mkdir()

    text = recipes.launchd_recipe(folder, EXE, home=tmp_path)

    comment = text[text.index("<!--") + 4:text.index("-->")]
    assert "--" not in comment and plistlib.loads(text.encode("utf-8"))["Label"].startswith("local-data-platform.")


def test_the_launchd_label_names_the_folder_and_tells_two_folders_with_one_name_apart(tmp_path):
    one, other = tmp_path / "one" / "shop", tmp_path / "two" / "shop"
    one.mkdir(parents=True)
    other.mkdir(parents=True)
    odd = tmp_path / "My Shop (2026)!"
    odd.mkdir()

    assert recipes.label_of(one) == recipes.label_of(one)
    assert recipes.label_of(one) != recipes.label_of(other)
    assert re.fullmatch(r"local-data-platform\.schedule\.shop\.[0-9a-f]{8}", recipes.label_of(one))
    assert re.fullmatch(r"local-data-platform\.schedule\.my-shop-2026\.[0-9a-f]{8}", recipes.label_of(odd))


def test_the_executable_is_the_one_given_then_the_one_that_runs_then_the_first_on_the_path_then_the_one_beside_python(
        tmp_path, monkeypatch):
    given, running, on_path, beside = (tmp_path / name / "ldp" for name in ("given", "running", "on_path", "beside"))
    for path in (given, running, on_path, beside):
        _write(path, "#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(running), "schedule", "install"])
    monkeypatch.setattr(recipes.shutil, "which", lambda name: str(on_path))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "beside" / "python"))

    assert recipes.find_executable("given/ldp") == given.resolve(), "an explicit path, made absolute"
    assert recipes.find_executable() == running
    monkeypatch.setattr(sys, "argv", ["pytest"])
    assert recipes.find_executable() == on_path
    monkeypatch.setattr(recipes.shutil, "which", lambda name: None)
    assert recipes.find_executable() == beside
    beside.unlink()
    with pytest.raises(ConfigError, match=r"cannot find the ldp executable .* Pass its path with --ldp PATH"):
        recipes.find_executable()


def test_an_executable_that_is_not_a_file_is_refused_and_so_is_an_unknown_recipe(tmp_path):
    with pytest.raises(ConfigError, match="nothing is not a file, so it cannot be the ldp executable"):
        recipes.find_executable(tmp_path / "nothing")
    with pytest.raises(ConfigError, match="unknown recipe 'systemd'; the recipes are cron, launchd"):
        recipes.recipe("systemd", tmp_path, EXE)


def test_ldp_schedule_install_prints_the_recipe_and_nothing_else_and_writes_nothing(tmp_path, capsys, monkeypatch):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    fake = tmp_path / "bin" / "ldp"
    _write(fake, "#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    cron = _ldp(capsys, "install", "--print", "cron", project, "--ldp", fake)
    launchd = _ldp(capsys, "install", "--print", "launchd", project, "--ldp", fake)

    assert cron == (0, recipes.cron_recipe(project, fake), "")
    assert launchd[0] == 0 and launchd[2] == ""
    arguments = plistlib.loads(launchd[1].encode("utf-8"))["ProgramArguments"]
    assert arguments == [str(fake), "schedule", "tick", str(project.resolve())]
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


def test_ldp_schedule_install_with_an_executable_that_is_not_there_exits_with_one(tmp_path, capsys):
    status, out, err = _ldp(capsys, "install", "--print", "cron", tmp_path, "--ldp", tmp_path / "nothing")

    assert status == 1 and out == "" and "is not a file, so it cannot be the ldp executable" in err


# ====================================================================== the package


def test_importing_the_scheduler_imports_no_heavy_library_so_the_command_line_stays_quick():
    code = ("import sys; import local_data_platform.orchestration.schedule, local_data_platform.orchestration.recipes; "
            "heavy = [name for name in sys.modules if name.split('.')[0] in ('pyiceberg', 'pyarrow', 'duckdb', "
            "'cronsim')]; print(heavy)")

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert done.stdout.strip() == "[]"


def test_the_package_exports_the_scheduler_and_the_ledger_names_and_the_modules_keep_their_names():
    from local_data_platform import orchestration
    from local_data_platform.orchestration import ledger

    assert orchestration.tick is schedule_module.tick and orchestration.list_scheduled is schedule_module.list_scheduled
    assert orchestration.TickResult is schedule_module.TickResult and orchestration.Continuation is ledger.Continuation
    assert {"tick", "list_scheduled", "TickResult", "Continuation"} <= set(orchestration.__all__)
    assert orchestration.schedule is schedule_module and orchestration.recipes is recipes
