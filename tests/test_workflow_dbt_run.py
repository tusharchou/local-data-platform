"""The ``dbt`` task of a workflow with the real dbt, on the window-aware project in ``tests/fixtures/dbt_gold``.

Every test is marked ``dbt``, so it skips without the extra and the ``dbt`` CI job runs it. Offline is checked for
each one: dbt runs as a subprocess of ``ldp`` with an HTTP proxy that is a local socket, and the test fails if anything
connects to it.

The workflow loads the orders of each day into ``shop.orders`` and builds ``daily_revenue`` from the orders of that
day. The model is incremental on ``day`` and reads the window from the variables ``window_start`` and ``window_end``, so
each day adds a row to the table that is published to ``gold.daily_revenue``, and a day with an order that has no
amount breaks the ``not_null`` test of ``revenue``.
"""

import json
import shutil
from pathlib import Path

import pytest

from local_data_platform.cli import main
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.orchestration import plan_workflow
from tests.support import without_log_warnings

pytestmark = pytest.mark.dbt

FIXTURE = Path(__file__).parent / "fixtures" / "dbt_gold"
DAY1, DAY2, DAY3 = "2026-08-01/2026-08-02", "2026-08-02/2026-08-03", "2026-08-03/2026-08-04"
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}
ORDERS = ("id,at,amount\n"
          "1,2026-08-01T10:00:00,10.0\n2,2026-08-01T11:00:00,20.0\n"
          "3,2026-08-02T09:00:00,5.0\n4,2026-08-02T10:00:00,7.5\n"
          "5,2026-08-03T09:00:00,\n6,2026-08-03T10:00:00,\n")  # the orders of day 3 have no amount


@pytest.fixture(autouse=True)
def offline(network_trap, monkeypatch):
    """Point every child process at a proxy that records connections, and fail the test if there is one."""
    env, connections = network_trap
    for name, value in env.items():
        if "proxy" in name.lower():
            monkeypatch.setenv(name, value)
    yield
    assert connections == [], f"something opened a network connection: {connections}"


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


@pytest.fixture
def workflow(tmp_path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "dbt")
    _write(tmp_path / "landing" / "orders.csv", ORDERS)
    _write(tmp_path / "config" / "orders.json", {"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG},
        "window": {"size": "day", "column": "at"}, "quality": [{"check": "row_count", "min": 2}]}})
    return _write(tmp_path / "daily.json", {
        "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders",
        "metadata": {"schedule": "@daily", "window": {"size": "day"}},
        "tasks": [{"id": "orders", "pipeline": "config/orders.json"},
                  {"id": "gold", "depends_on": ["orders"], "dbt": {"project": "dbt"}},
                  {"id": "check", "depends_on": ["gold"], "dbt": {"project": "dbt", "command": "test",
                                                                  "select": "stg_orders"}}]})


def _run(capfd, workflow: Path, window: str) -> tuple[int, dict, str]:
    """``ldp workflow run --json``: the exit status, its standard output read as JSON, and its standard error."""
    status = main(["workflow", "run", str(workflow), "--window", window, "--json"])
    out, err = capfd.readouterr()
    return status, json.loads(out), without_log_warnings(err)


def _statuses(printed: dict) -> dict[str, str]:
    return {task["id"]: task["status"] for task in printed["tasks"]}


def _gold(tmp_path: Path) -> Iceberg:
    return Iceberg("daily_revenue", {"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")})


def _rows(tmp_path: Path) -> list[tuple]:
    return sorted((str(row["day"]), row["orders"], row["revenue"]) for row in _gold(tmp_path).get().to_pylist())


def test_each_window_builds_its_day_and_a_second_run_of_a_day_does_nothing(tmp_path, workflow, capfd):
    status, day1, err = _run(capfd, workflow, DAY1)

    assert status == 0 and _statuses(day1) == {"orders": "published", "gold": "succeeded", "check": "succeeded"}
    assert "Running with dbt" in err and "Completed successfully" in err, "dbt's own output is on standard error"
    assert _rows(tmp_path) == [("2026-08-01", 2, 30.0)], "the model read only the orders of the window"
    gold = next(task for task in day1["tasks"] if task["id"] == "gold")["detail"]
    assert gold["window"] == ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"]
    assert gold["nodes"] == {"success": 2, "pass": 4}
    assert [(item["table"], item["mode"], item["rows_after"]) for item in gold["published"]] == [
        ("gold.daily_revenue", "upsert", 1)]
    snapshot = _gold(tmp_path).table().current_snapshot().snapshot_id

    status, again, _ = _run(capfd, workflow, DAY1)

    assert status == 0 and set(_statuses(again).values()) == {"skipped_duplicate"}
    assert _gold(tmp_path).table().current_snapshot().snapshot_id == snapshot
    status, day2, _ = _run(capfd, workflow, DAY2)
    assert status == 0 and _statuses(day2) == {"orders": "published", "gold": "succeeded", "check": "succeeded"}
    assert _rows(tmp_path) == [("2026-08-01", 2, 30.0), ("2026-08-02", 2, 12.5)], "each day adds a row"


def test_a_failing_data_test_blocks_the_day_until_the_model_is_fixed(tmp_path, workflow, capfd):
    _run(capfd, workflow, DAY1)

    status, day3, _ = _run(capfd, workflow, DAY3)

    assert status == 3 and _statuses(day3) == {"orders": "published", "gold": "blocked_quality",
                                               "check": "upstream_failed"}
    gold = next(task for task in day3["tasks"] if task["id"] == "gold")
    assert [(node["status"], node["node"].split(".")[2]) for node in gold["detail"]["failed_nodes"]] == [
        ("fail", "not_null_daily_revenue_revenue")]
    assert "dbt build failed with exit status 1, so nothing was published" in gold["error"]
    assert _rows(tmp_path) == [("2026-08-01", 2, 30.0)], "the failed day was not published"
    model = tmp_path / "dbt" / "models" / "gold" / "daily_revenue.sql"
    model.write_text(model.read_text().replace("sum(amount) as revenue", "coalesce(sum(amount), 0) as revenue"))

    status, fixed, _ = _run(capfd, workflow, DAY3)

    assert status == 0 and _statuses(fixed) == {"orders": "skipped_duplicate", "gold": "succeeded",
                                                "check": "succeeded"}
    assert _rows(tmp_path) == [("2026-08-01", 2, 30.0), ("2026-08-03", 2, 0.0)]


def test_the_events_show_the_task_and_the_model_it_published_under_one_workflow_run(tmp_path, workflow, capfd):
    _, printed, _ = _run(capfd, workflow, DAY1)

    from local_data_platform.events import read_jsonl

    events = read_jsonl(tmp_path / ".ldp" / "events.jsonl")
    finished = {(event.payload.get("task_id"), event.payload.get("pipeline")): event for event in events
                if event.type == "run.finished" and event.payload.get("task_id")}
    assert finished[("gold", "gold")].payload["status"] == "succeeded"
    assert finished[("gold", "daily_revenue")].payload["status"] == "published"
    assert {event.payload["workflow_run_id"] for event in finished.values()} == {printed["run_id"]}
    assert main(["runs", str(tmp_path / ".ldp" / "events.jsonl")]) == 0
    assert "daily_revenue" in capfd.readouterr().out


def test_the_plan_checks_the_real_project_and_the_variables_it_will_get(tmp_path, workflow):
    plan = plan_workflow(workflow, window=DAY2)

    assert plan["valid"], plan["errors"]
    gold = next(task for task in plan["tasks"] if task["id"] == "gold")
    assert gold["window_vars"] == {"window_start": "2026-08-02T00:00:00Z", "window_end": "2026-08-03T00:00:00Z"}
    assert plan["warnings"] == [], "the pipeline that loads the source table is in the depends_on of the task"
