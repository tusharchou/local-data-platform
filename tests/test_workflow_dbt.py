"""The ``dbt`` task kind of workflow files, tested without dbt.

A stand-in for dbt (``tests.support.FakeDbt``), a script that records how it was called and writes the artifacts and
relations a dbt run would, plays the part of the real one. So these tests run on every install and check what the task
passes to dbt, what it makes of the result and what it publishes. ``test_workflow_dbt_run.py`` runs the same task with
the real dbt.

Every project here has an orders pipeline that loads CSV files into ``shop.orders``, and a dbt project whose one model,
``daily_revenue``, is marked ``ldp_publish`` and published to ``gold.daily_revenue``.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform.cli import main
from local_data_platform.dbt_layer import build
from local_data_platform.dbt_layer.project import DbtProject
from local_data_platform.events import read_jsonl
from local_data_platform.exceptions import ConfigError, DataQualityError, EngineNotFound, LDPError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.orchestration import (
    plan_workflow,
    run_workflow,
    task_kinds,
    validate_workflow,
    workflow_schema,
)
from local_data_platform.orchestration.dbt_task import last_build
from local_data_platform.orchestration.tasks import RunContext, TaskBlocked, TaskFailed, get_task_kind
from tests.support import FakeDbt, dbt_artifacts, dbt_model, write_dbt_project

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the stand-in dbt is a script with a shebang line")

DAY1, DAY2 = "2026-08-01/2026-08-02", "2026-08-02/2026-08-03"
ORDERS = "id,at,amount\n1,2026-08-01T10:00:00,10.0\n2,2026-08-01T11:00:00,20.0\n3,2026-08-02T09:00:00,5.0\n"
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}
GOLD_META = {"ldp_publish": True, "ldp_namespace": "gold"}
CREATE = ["create or replace table main.daily_revenue as select cast('2026-08-01' as date) as day, 2 as orders, "
          "1234.5 as revenue"]
WINDOW1 = ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"]


@pytest.fixture
def fake(tmp_path) -> FakeDbt:
    return FakeDbt(tmp_path / "fake")


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _orders_config(root: Path, identifier: str = "orders") -> Path:
    return _write(root / "config" / "orders.json", {"identifier": identifier, "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG},
        "window": {"size": "day", "column": "at"}}})


def _will(fake: FakeDbt, root: Path, *, models=None, tests=(), statuses=None, exit=0, sql=CREATE,
          artifacts=True) -> None:
    """Make the stand-in dbt build ``daily_revenue`` (or ``models``) and write the artifacts of a run."""
    manifest, results = dbt_artifacts(*(models or [dbt_model("daily_revenue", meta=GOLD_META)]), statuses=statuses)
    for check in tests:
        results["results"].append({"unique_id": f"test.shop.{check['name']}.abc123",
                                   "status": check.get("status", "pass"), "message": check.get("message", "")})
    results["elapsed_time"] = 0.25
    plan = {"database": str(root / "dbt" / "target" / "ldp_build.duckdb"), "sql": sql, "exit": exit}
    if artifacts:
        plan["artifacts"] = {"manifest.json": manifest, "run_results.json": results}
    fake.will(**plan)


def _dbt_task(fake: FakeDbt, **options) -> dict:
    return {"project": "dbt", "dbt_bin": str(fake.path), **options}


def _project(root: Path, fake: FakeDbt, *, dbt: dict | None = None, tasks: list | None = None,
             will: dict | None = None) -> Path:
    """Write the landing file, the orders config, the dbt project and the workflow ``daily.json``; return it."""
    _write(root / "landing" / "orders.csv", ORDERS)
    _orders_config(root)
    project = write_dbt_project(root / "dbt", {"daily_revenue": "select 1 as day"})
    _write(project / "ldp_dbt.json", {"sources": {"configs": ["../config"]},
                                      "publish": {"catalog": {"identifier": "gold", "warehouse_path": "../warehouse"},
                                                  "config_dir": "target/ldp"}})
    _will(fake, root, **(will or {}))
    spec = {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders",
            "metadata": {"schedule": "@daily", "window": {"size": "day"}},
            "tasks": tasks if tasks is not None else [
                {"id": "orders", "pipeline": "config/orders.json"},
                {"id": "gold", "depends_on": ["orders"], "dbt": _dbt_task(fake, **(dbt or {}))}]}
    return _write(root / "daily.json", spec)


def _calls(fake: FakeDbt) -> list[list[str]]:
    """What the stand-in dbt was called with, from the command on."""
    return [call["argv"][next(i for i, item in enumerate(call["argv"]) if item in ("build", "test")):]
            for call in fake.calls]


def _statuses(result) -> dict[str, str]:
    return {task_id: item.status for task_id, item in result.tasks.items()}


def _gold(root: Path) -> Iceberg:
    return Iceberg("daily_revenue", {"identifier": "gold", "warehouse_path": str(root / "warehouse")})


def _ledger(root: Path) -> list:
    return read_jsonl(root / ".ldp" / "events.jsonl")


def _task_finish(root: Path, task_id: str = "gold") -> list:
    """The ``run.finished`` events of a dbt task itself, which the runner emits, not of the models it published."""
    return [event for event in _ledger(root)
            if event.type == "run.finished" and event.payload.get("task_kind") == "dbt"
            and event.payload.get("task_id") == task_id]


def _vars(call: list[str]) -> dict:
    return json.loads(call[call.index("--vars") + 1])


# ====================================================================== validation and the schema


@pytest.mark.parametrize("value", [
    {"project": "dbt"},
    {"project": "dbt", "command": "test", "select": "gold+ tag:daily", "exclude": ["staging"],
     "vars": {"min_orders": 5, "tags": ["a", "b"], "nested": {"on": True, "ratio": 0.5, "none": None}},
     "skip_unchanged": False, "dbt_bin": "/opt/dbt/bin/dbt"},
    {"project": "dbt", "select": ["a", "b+"]},
])
def test_a_valid_dbt_task(value):
    assert get_task_kind("dbt").validate(value) == []
    spec = {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "w", "tasks": [{"id": "gold", "dbt": value}]}
    assert validate_workflow(spec) == []


@pytest.mark.parametrize("value, path, message", [
    ("dbt", "", "must be an object with a 'project'"),
    ({}, ".project", "is required"),
    ({"project": ""}, ".project", "is required"),
    ({"project": 5}, ".project", "is required"),
    ({"project": "dbt", "run": "x"}, "", "has unknown keys ['run']"),
    ({"project": "dbt", "command": "run"}, ".command", "must be one of ['build', 'test'], got 'run'"),
    ({"project": "dbt", "select": ""}, ".select", "must be a non-empty dbt selector, got ''"),
    ({"project": "dbt", "select": []}, ".select", "must be a dbt selector such as"),
    ({"project": "dbt", "select": 5}, ".select", "must be a dbt selector such as"),
    ({"project": "dbt", "select": ["a", " "]}, ".select[1]", "must be a non-empty dbt selector"),
    ({"project": "dbt", "select": ["a", 7]}, ".select[1]", "must be a non-empty dbt selector, got 7"),
    ({"project": "dbt", "select": "--full-refresh"}, ".select", "must be dbt selectors, not options"),
    ({"project": "dbt", "exclude": ["a", "-x"]}, ".exclude[1]", "must be dbt selectors, not options"),
    ({"project": "dbt", "vars": "x=1"}, ".vars", "must be an object of dbt variables"),
    ({"project": "dbt", "vars": {"window_start": "2026-01-01"}}, ".vars.window_start", "is set by the run"),
    ({"project": "dbt", "vars": {"window_end": "x"}}, ".vars.window_end", "is set by the run"),
    ({"project": "dbt", "vars": {"x": float("nan")}}, "", "vars.x must be a finite number"),
    ({"project": "dbt", "vars": {"x": {1, 2}}}, "", "vars.x must be JSON, got set"),
    ({"project": "dbt", "vars": {"x": [1, object()]}}, "", "vars.x[1] must be JSON, got object"),
    ({"project": "dbt", "skip_unchanged": "no"}, ".skip_unchanged", "must be true or false"),
    ({"project": "dbt", "dbt_bin": ""}, ".dbt_bin", "must be the path of a dbt"),
])
def test_a_dbt_task_is_checked(value, path, message):
    problems = get_task_kind("dbt").validate(value)

    assert len(problems) == 1 and problems[0][0] == path and message in problems[0][1], problems


def test_the_dbt_kind_is_registered_and_changes_data():
    assert "dbt" in task_kinds()
    kind = get_task_kind("dbt")
    assert kind.emits_events is False and kind.changes_data is True
    assert get_task_kind("maintain").changes_data is False and get_task_kind("pipeline").changes_data is True


def test_the_schema_describes_the_dbt_task():
    schema = get_task_kind("dbt").json_schema()

    assert schema["required"] == ["project"] and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"project", "command", "select", "exclude", "vars", "skip_unchanged",
                                         "dbt_bin"}
    assert workflow_schema()["$defs"]["task"]["properties"]["dbt"] == schema
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(workflow_schema())
    validator = jsonschema.Draft202012Validator(workflow_schema())
    good = {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "w",
            "tasks": [{"id": "gold", "dbt": {"project": "dbt", "select": ["a"], "vars": {"x": 1}}}]}
    assert not list(validator.iter_errors(good))
    good["tasks"][0]["dbt"]["command"] = "run"
    assert list(validator.iter_errors(good))


# ====================================================================== the plan


def test_the_plan_shows_the_project_the_variables_and_the_fingerprint(tmp_path, fake):
    workflow = _project(tmp_path, fake, dbt={"select": "daily_revenue+", "exclude": "staging", "command": "build"})

    plan = plan_workflow(workflow, window=DAY1)

    assert plan["valid"], plan["errors"]
    task = next(item for item in plan["tasks"] if item["id"] == "gold")
    assert task["kind"] == "dbt" and task["project"] == "dbt" and task["command"] == "build"
    assert task["target"] == "dbt build" and task["select"] == "daily_revenue+" and task["exclude"] == "staging"
    assert task["window_vars"] == {"window_start": WINDOW1[0], "window_end": WINDOW1[1]}
    assert len(task["fingerprint"]) == 16 and int(task["fingerprint"], 16) >= 0
    assert plan["warnings"] == [], "a plan that is in order has nothing to warn about"


def test_the_plan_shows_the_fingerprint_that_the_run_compares(tmp_path, fake):
    workflow = _project(tmp_path, fake, dbt={"select": "daily_revenue"})
    planned = next(task for task in plan_workflow(workflow, window=DAY1)["tasks"] if task["id"] == "gold")

    result = run_workflow(workflow, window=DAY1)

    assert result.tasks["gold"].detail["fingerprint"] == planned["fingerprint"], \
        "what a run writes into the project folder is not part of the fingerprint"
    again = next(task for task in plan_workflow(workflow, window=DAY1)["tasks"] if task["id"] == "gold")
    assert again["fingerprint"] == planned["fingerprint"]


def test_the_plan_without_a_window_has_no_variables(tmp_path, fake):
    plan = plan_workflow(_project(tmp_path, fake))

    task = next(item for item in plan["tasks"] if item["id"] == "gold")
    assert plan["valid"] and "window_vars" not in task and "fingerprint" in task


def test_the_plan_of_a_test_task_says_it_runs_dbt_test(tmp_path, fake):
    plan = plan_workflow(_project(tmp_path, fake, dbt={"command": "test"}), window=DAY1)

    task = next(item for item in plan["tasks"] if item["id"] == "gold")
    assert task["target"] == "dbt test" and task["command"] == "test" and plan["warnings"] == []


@pytest.mark.parametrize("change, expected", [
    ("no project folder", "dbt project folder not found"),
    ("no ldp_dbt.json", "has no ldp_dbt.json"),
    ("broken ldp_dbt.json", "has 1 problem"),
    ("no dbt_project.yml", "has no dbt_project.yml, so it is not a dbt project"),
    ("no dbt", "dbt was not found at"),
])
def test_the_plan_rejects_a_project_that_cannot_run_and_so_does_a_run(tmp_path, fake, change, expected):
    workflow = _project(tmp_path, fake)
    project = tmp_path / "dbt"
    if change == "no project folder":
        shutil.rmtree(project)
    elif change == "no ldp_dbt.json":
        (project / "ldp_dbt.json").unlink()
    elif change == "broken ldp_dbt.json":
        (project / "ldp_dbt.json").write_text(json.dumps({"sources": {"configs": []}}))
    elif change == "no dbt_project.yml":
        (project / "dbt_project.yml").unlink()
    else:
        spec = json.loads(workflow.read_text())
        spec["tasks"][1]["dbt"]["dbt_bin"] = str(tmp_path / "missing" / "dbt")
        workflow.write_text(json.dumps(spec))

    plan = plan_workflow(workflow, window=DAY1)

    assert not plan["valid"]
    assert any(expected in error for error in plan["errors"]), plan["errors"]
    with pytest.raises(ConfigError, match="is not valid, so no task ran"):
        run_workflow(workflow, window=DAY1)
    assert fake.calls == [] and not (tmp_path / "warehouse").exists(), "nothing ran, not even the orders load"


def test_a_missing_dbt_says_how_to_install_it(tmp_path, fake, monkeypatch):
    workflow = _project(tmp_path, fake, tasks=[{"id": "gold", "dbt": {"project": "dbt"}}])

    def not_found(dbt_bin=None):
        raise EngineNotFound('dbt was not found. Install it with: pip install "local-data-platform[dbt]"')

    monkeypatch.setattr("local_data_platform.dbt_layer.project.find_dbt", not_found)

    plan = plan_workflow(workflow, window=DAY1)

    assert not plan["valid"]
    assert any("pip install" in error and "local-data-platform[dbt]" in error for error in plan["errors"]), plan


def test_a_profile_that_points_at_another_build_file_is_a_plan_error(tmp_path, fake):
    pytest.importorskip("yaml")
    workflow = _project(tmp_path, fake)
    profile = tmp_path / "dbt" / "profiles.yml"
    profile.write_text(profile.read_text().replace("target/ldp_build.duckdb", "other.duckdb"))

    plan = plan_workflow(workflow, window=DAY1)

    assert not plan["valid"] and any("ldp_dbt.json says dbt builds in" in error for error in plan["errors"])


def test_the_plan_warns_when_no_depends_on_connects_a_reader_to_its_writer(tmp_path, fake):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"}, {"id": "gold", "dbt": _dbt_task(fake)}]
    workflow = _project(tmp_path, fake, tasks=tasks)

    plan = plan_workflow(workflow, window=DAY1)

    assert any("task 'gold' reads the table shop.orders, which task 'orders' writes, and no depends_on connects them"
               in warning for warning in plan["warnings"]), plan["warnings"]
    ordered = json.loads(workflow.read_text())
    ordered["tasks"][1]["depends_on"] = ["orders"]
    workflow.write_text(json.dumps(ordered))
    assert not [w for w in plan_workflow(workflow, window=DAY1)["warnings"] if "no depends_on" in w]


def test_the_plan_table_names_the_project(tmp_path, fake, capfd):
    workflow = _project(tmp_path, fake)

    assert main(["workflow", "plan", str(workflow), "--window", DAY1]) == 0

    out = capfd.readouterr().out
    row = next(line for line in out.splitlines() if line.split()[1:3] == ["gold", "dbt"])
    assert row.split()[3:6] == ["orders", "dbt", "dbt"] and "dbt build" in row


# ====================================================================== the fingerprint


def _folder(root: Path, *, config_dir: str | None = None) -> DbtProject:
    project = write_dbt_project(root / "dbt", {"a": "select 1", "b": "select 2"})
    publish = {"catalog": {"identifier": "g", "warehouse_path": "../w"}}
    if config_dir:
        publish["config_dir"] = config_dir
    _write(project / "ldp_dbt.json", {"sources": {"configs": []}, "publish": publish})
    return DbtProject.load(project)


def test_the_fingerprint_is_stable_and_sees_what_a_build_depends_on(tmp_path):
    project = _folder(tmp_path)
    base = build.project_fingerprint(project)

    assert base == build.project_fingerprint(project) and len(base) == 16
    models = project.directory / "models"
    (models / "a.sql").write_text("select 11")
    assert build.project_fingerprint(project) != base
    (models / "a.sql").write_text("select 1")
    assert build.project_fingerprint(project) == base
    (models / "c.sql").write_text("select 3")
    assert build.project_fingerprint(project) != base
    (models / "c.sql").unlink()
    (project.directory / "profiles.yml").write_text("changed: true\n")
    assert build.project_fingerprint(project) != base


def test_the_fingerprint_follows_the_options_of_the_run(tmp_path):
    project = _folder(tmp_path)

    base = build.project_fingerprint(project, extra={"command": "build", "select": ["a"], "vars": {"x": 1}})

    assert base == build.project_fingerprint(project, extra={"vars": {"x": 1}, "select": ["a"], "command": "build"})
    for other in ({"command": "test", "select": ["a"], "vars": {"x": 1}},
                  {"command": "build", "select": ["b"], "vars": {"x": 1}},
                  {"command": "build", "select": ["a"], "vars": {"x": 2}}):
        assert build.project_fingerprint(project, extra=other) != base


def test_the_fingerprint_leaves_out_what_dbt_and_ldp_write(tmp_path):
    project = _folder(tmp_path, config_dir="generated")
    base = build.project_fingerprint(project)
    for name, content in [("target/manifest.json", "{}"), ("target/ldp/a.json", "{}"), ("generated/a.json", "{}"),
                          ("logs/dbt.log", "x"), ("dbt_packages/p/x.sql", "x"), (".user.yml", "id: 1"),
                          ("models/.DS_Store", "x"), (".git/HEAD", "ref"), ("models/__pycache__/x.pyc", "x"),
                          ("target/ldp_build.duckdb", "binary"), ("target/ldp_build.duckdb.wal", "log"),
                          ("models/_ldp_sources.yml", "version: 2")]:
        _write(project.directory / name, content)

    assert build.project_fingerprint(project) == base


def test_the_fingerprint_follows_the_names_of_the_files_and_not_only_what_they_hold(tmp_path):
    project = _folder(tmp_path)
    base = build.project_fingerprint(project)
    models = project.directory / "models"

    (models / "a.sql").write_text("select 2")
    (models / "b.sql").write_text("select 1")
    assert build.project_fingerprint(project) != base, "the same files with their contents swapped"
    (models / "a.sql").write_text("select 1")
    (models / "b.sql").write_text("select 2")
    assert build.project_fingerprint(project) == base
    (models / "b.sql").rename(models / "c.sql")  # the files keep their order, so only their names tell
    assert build.project_fingerprint(project) != base, "a renamed model is another model"


def test_the_fingerprint_does_not_depend_on_where_the_project_is(tmp_path):
    one, two = _folder(tmp_path / "one"), _folder(tmp_path / "somewhere" / "else")

    assert build.project_fingerprint(one) == build.project_fingerprint(two)


def test_the_fingerprint_leaves_out_the_target_folder_it_is_told_about(tmp_path):
    project = _folder(tmp_path)
    base = build.project_fingerprint(project, target_path="build_out")
    _write(project.directory / "build_out" / "manifest.json", "{}")

    assert build.project_fingerprint(project, target_path="build_out") == base
    assert build.project_fingerprint(project) != base, "without being told, build_out is part of the project"


def test_the_fingerprint_notices_a_file_that_cannot_be_read(tmp_path):
    project = _folder(tmp_path)
    base = build.project_fingerprint(project)
    os.symlink(tmp_path / "nowhere", project.directory / "models" / "broken.sql")

    assert build.project_fingerprint(project) != base


# ====================================================================== the ledger


def _line(status="succeeded", *, workflow="w", task="gold", window=("a", "b"), run="r1", kind="dbt",
          fingerprint="f", type_="run.finished") -> str:
    payload = {"workflow": workflow, "task_id": task, "window": list(window), "workflow_run_id": run,
               "task_kind": kind, "status": status, "fingerprint": fingerprint}
    return json.dumps({"type": type_, "payload": payload})


def _ledger_file(tmp_path, *lines: str) -> Path:
    path = tmp_path / ".ldp" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def test_last_build_is_the_last_matching_build_of_the_window(tmp_path):
    ledger = _ledger_file(tmp_path, _line("failed", run="r1"), _line("succeeded", run="r2", fingerprint="two"),
                          _line("blocked_quality", run="r3", fingerprint="three"))

    found = last_build(ledger, "w", "gold", ["a", "b"], "now")

    assert found["workflow_run_id"] == "r3" and found["status"] == "blocked_quality"
    earlier = last_build(ledger, "w", "gold", ["a", "b"], "r3")
    assert earlier["workflow_run_id"] == "r2", "the run that is asking is not an earlier one"


@pytest.mark.parametrize("line", [
    _line(workflow="other"), _line(task="other"), _line(window=("a", "c")), _line(kind="maintain"), _line(kind=None),
    _line(type_="run.started"), _line("skipped_duplicate"), _line("upstream_failed"),
    _line(task="other", fingerprint="gold"),  # mentions the task, so only the fields can tell it is not its build
])
def test_last_build_ignores_what_is_not_a_build_of_this_task_and_window(tmp_path, line):
    ledger = _ledger_file(tmp_path, line)

    assert last_build(ledger, "w", "gold", ["a", "b"], "now") is None


def test_a_skip_does_not_hide_the_build_before_it(tmp_path):
    ledger = _ledger_file(tmp_path, _line("failed", run="r1"), _line("skipped_duplicate", run="r2"),
                          _line("upstream_failed", run="r3"))

    assert last_build(ledger, "w", "gold", ["a", "b"], "now")["workflow_run_id"] == "r1"


def test_last_build_reads_a_ledger_with_damage_or_none(tmp_path):
    assert last_build(tmp_path / "missing.jsonl", "w", "gold", ["a", "b"], "now") is None
    ledger = _ledger_file(tmp_path, "", "not json gold run.finished", '{"payload": 5} gold run.finished',
                          '{"type": "run.finished"} gold', _line(run="good"), _line(run="cut")[:60])

    assert last_build(ledger, "w", "gold", ["a", "b"], "now")["workflow_run_id"] == "good"


# ====================================================================== what a run does


def test_the_task_builds_for_the_window_and_publishes_the_marked_model(tmp_path, fake, capfd):
    workflow = _project(tmp_path, fake, dbt={"select": "daily_revenue+", "exclude": "staging",
                                             "vars": {"min_orders": 2}})

    result = run_workflow(workflow, window=DAY1)

    assert _statuses(result) == {"orders": "published", "gold": "succeeded"} and result.exit_code == 0
    [call] = _calls(fake)
    assert call[:5] == ["build", "--select", "daily_revenue+", "--exclude", "staging"]
    assert _vars(call) == {"min_orders": 2, "window_start": WINDOW1[0], "window_end": WINDOW1[1]}
    assert call[call.index("--project-dir") + 1] == str(tmp_path / "dbt")
    assert fake.calls[0]["sources_yaml_existed"] is True, "the sources are registered before dbt runs"
    detail = result.tasks["gold"].detail
    assert detail["command"] == "build" and detail["window"] == WINDOW1 and len(detail["fingerprint"]) == 16
    assert detail["nodes"] == {"success": 1} and detail["dbt_s"] == 0.25
    [published] = detail["published"]
    assert published["table"] == "gold.daily_revenue" and published["mode"] == "overwrite"
    assert (published["rows_before"], published["rows_after"]) == (0, 1)
    assert published["snapshot_id"] == _gold(tmp_path).table().current_snapshot().snapshot_id
    assert _gold(tmp_path).get().to_pylist()[0]["orders"] == 2
    out, err = capfd.readouterr()
    assert "fake dbt ran" in err and "fake dbt" not in out, "dbt's output must stay off standard output"


def test_the_window_variables_are_utc_instants_whatever_the_window_was_written_in(tmp_path, fake):
    workflow = _project(tmp_path, fake)

    run_workflow(workflow, window="2026-08-01T00:00:00+02:00/2026-08-02T00:00:00+02:00")

    [call] = _calls(fake)
    assert (_vars(call)["window_start"], _vars(call)["window_end"]) == ("2026-07-31T22:00:00Z",
                                                                        "2026-08-01T22:00:00Z")


def test_a_select_with_several_selectors_is_passed_as_separate_arguments(tmp_path, fake):
    workflow = _project(tmp_path, fake, dbt={"select": ["a b", "tag:daily+"], "exclude": ["c"]})

    run_workflow(workflow, window=DAY1)

    [call] = _calls(fake)
    assert call[:7] == ["build", "--select", "a", "b", "tag:daily+", "--exclude", "c"]


def test_a_task_without_a_selection_passes_only_the_variables(tmp_path, fake):
    run_workflow(_project(tmp_path, fake), window=DAY1)

    [call] = _calls(fake)
    assert call[:2] == ["build", "--vars"] and "--select" not in call and "--exclude" not in call


def test_the_events_of_the_task_say_what_it_ran_and_the_models_it_published_belong_to_it(tmp_path, fake):
    result = run_workflow(_project(tmp_path, fake), window=DAY1)

    [finished] = _task_finish(tmp_path)
    payload = finished.payload
    assert payload["status"] == "succeeded" and payload["workflow"] == "daily_orders"
    assert payload["workflow_run_id"] == result.run_id and payload["task_id"] == "gold"
    assert payload["command"] == "build" and payload["window"] == WINDOW1
    assert payload["published"][0]["table"] == "gold.daily_revenue" and "fingerprint" in payload
    model_runs = [event for event in _ledger(tmp_path)
                  if event.type == "run.finished" and event.payload.get("pipeline") == "daily_revenue"]
    assert len(model_runs) == 1 and model_runs[0].payload["status"] == "published"
    assert model_runs[0].payload["task_id"] == "gold" and model_runs[0].payload["workflow_run_id"] == result.run_id
    assert "task_kind" not in model_runs[0].payload
    started = [event for event in _ledger(tmp_path)
               if event.type == "run.started" and event.payload.get("task_id") == "gold"]
    assert len(started) == 2, "the run of the task, and the run that published its model"
    assert "1234.5" not in json.dumps([event.to_dict() for event in _ledger(tmp_path)]), "events never carry rows"


def test_running_the_same_window_again_skips_a_build_that_was_done(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    first = run_workflow(workflow, window=DAY1)
    rows = _gold(tmp_path).get().to_pylist()
    snapshot = _gold(tmp_path).table().current_snapshot().snapshot_id

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"orders": "skipped_duplicate", "gold": "skipped_duplicate"} and again.exit_code == 0
    assert len(fake.calls) == 1, "dbt did not run again"
    assert again.tasks["gold"].detail["built_by"] == first.run_id and "published" not in again.tasks["gold"].detail
    assert _gold(tmp_path).get().to_pylist() == rows
    assert _gold(tmp_path).table().current_snapshot().snapshot_id == snapshot, "nothing was written again"
    assert [e.payload["status"] for e in _task_finish(tmp_path)] == ["succeeded", "skipped_duplicate"]


def test_another_window_builds_again_and_an_earlier_window_stays_done(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)

    second = run_workflow(workflow, window=DAY2)

    assert _statuses(second) == {"orders": "published", "gold": "succeeded"}
    assert [_vars(call)["window_start"] for call in _calls(fake)] == ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"]
    third = run_workflow(workflow, window=DAY1)
    assert _statuses(third) == {"orders": "skipped_duplicate", "gold": "skipped_duplicate"}


def test_a_changed_model_builds_the_window_again(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)
    (tmp_path / "dbt" / "models" / "daily_revenue.sql").write_text("select 2 as day")

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"orders": "skipped_duplicate", "gold": "succeeded"} and len(fake.calls) == 2
    assert _statuses(run_workflow(workflow, window=DAY1))["gold"] == "skipped_duplicate"


def test_a_changed_selection_or_variable_builds_the_window_again(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)
    spec = json.loads(workflow.read_text())
    spec["tasks"][1]["dbt"]["vars"] = {"x": 1}
    workflow.write_text(json.dumps(spec))

    assert _statuses(run_workflow(workflow, window=DAY1))["gold"] == "succeeded"
    spec["tasks"][1]["dbt"]["select"] = "daily_revenue"
    workflow.write_text(json.dumps(spec))
    assert _statuses(run_workflow(workflow, window=DAY1))["gold"] == "succeeded"
    assert _statuses(run_workflow(workflow, window=DAY1))["gold"] == "skipped_duplicate"


def test_a_task_that_follows_an_upstream_task_that_wrote_data_builds_again(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)
    _orders_config(tmp_path, identifier="orders_again")  # a new identifier is a new key, so the load runs again

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"orders": "published", "gold": "succeeded"} and len(fake.calls) == 2


def test_a_task_that_follows_only_tasks_that_wrote_nothing_is_still_skipped(tmp_path, fake):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "gold", "depends_on": ["orders"], "dbt": _dbt_task(fake)},
             {"id": "tidy", "depends_on": ["gold"], "maintain": {"config": "config/orders.json"}},
             {"id": "check", "depends_on": ["tidy"], "dbt": _dbt_task(fake, command="test")}]
    workflow = _project(tmp_path, fake, tasks=tasks)
    first = run_workflow(workflow, window=DAY1)
    assert _statuses(first) == {"orders": "published", "gold": "succeeded", "tidy": "succeeded", "check": "succeeded"}

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"orders": "skipped_duplicate", "gold": "skipped_duplicate", "tidy": "succeeded",
                                "check": "skipped_duplicate"}, "a maintain task changes no data"


def test_a_task_that_follows_a_task_that_failed_builds_again_because_it_may_have_changed_data(tmp_path, fake):
    tasks = [{"id": "broken", "pipeline": "config/broken.json"}, {"id": "gold", "dbt": _dbt_task(fake)}]
    workflow = _project(tmp_path, fake, tasks=tasks)
    _write(tmp_path / "config" / "broken.json", {"identifier": "broken", "metadata": {
        "source": {"name": "x", "format": "CSV", "path": "../landing/missing.csv"},
        "target": {"name": "x", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG}}})
    first = run_workflow(workflow, window=DAY1)
    assert _statuses(first) == {"broken": "failed", "gold": "succeeded"}

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"broken": "failed", "gold": "succeeded"} and len(fake.calls) == 2


def test_skip_unchanged_false_always_builds(tmp_path, fake):
    workflow = _project(tmp_path, fake, dbt={"skip_unchanged": False})
    run_workflow(workflow, window=DAY1)

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"orders": "skipped_duplicate", "gold": "succeeded"} and len(fake.calls) == 2


def test_a_window_whose_last_build_failed_builds_again(tmp_path, fake):
    workflow = _project(tmp_path, fake, will={"exit": 1, "statuses": {"daily_revenue": "error"}})
    assert run_workflow(workflow, window=DAY1).tasks["gold"].status == "failed"
    _will(fake, tmp_path)

    again = run_workflow(workflow, window=DAY1)

    assert _statuses(again) == {"orders": "skipped_duplicate", "gold": "succeeded"}
    assert _statuses(run_workflow(workflow, window=DAY1))["gold"] == "skipped_duplicate"


def test_a_deleted_ledger_means_the_build_runs_again(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)
    (tmp_path / ".ldp" / "events.jsonl").unlink()

    assert _statuses(run_workflow(workflow, window=DAY1))["gold"] == "succeeded"


# ---------------------------------------------------------------------- failures


def test_failing_data_tests_are_a_data_gate(tmp_path, fake):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "gold", "depends_on": ["orders"], "dbt": _dbt_task(fake)},
             {"id": "tidy", "depends_on": ["gold"], "maintain": {"config": "config/orders.json"}}]
    check = {"name": "not_null_daily_revenue_revenue", "status": "fail",
             "message": "Got 2 results, configured to fail if != 0"}
    workflow = _project(tmp_path, fake, tasks=tasks, will={"exit": 1, "tests": [check]})

    result = run_workflow(workflow, window=DAY1)

    assert _statuses(result) == {"orders": "published", "gold": "blocked_quality", "tidy": "upstream_failed"}
    assert result.status == "blocked_quality" and result.exit_code == 3
    gold = result.tasks["gold"]
    assert "dbt build failed with exit status 1, so nothing was published" in gold.error
    assert "test.shop.not_null_daily_revenue_revenue.abc123: fail, Got 2 results" in gold.error
    assert gold.detail["exit_status"] == 1 and gold.detail["nodes"] == {"success": 1, "fail": 1}
    assert gold.detail["failed_nodes"] == [{"node": "test.shop.not_null_daily_revenue_revenue.abc123",
                                            "status": "fail"}] and gold.detail["failed_total"] == 1
    assert gold.detail["command"] == "build" and "fingerprint" in gold.detail and gold.detail["window"] == WINDOW1
    assert "published" not in gold.detail and not _gold(tmp_path).exists(), "nothing was published"
    [finished] = _task_finish(tmp_path)
    assert finished.payload["status"] == "blocked_quality" and finished.payload["failed_total"] == 1
    assert not [e for e in _ledger(tmp_path) if e.type == "run.failed" and e.payload.get("task_id") == "gold"], \
        "a data gate is not a failure"


def test_a_model_that_raised_an_error_is_a_failure(tmp_path, fake):
    workflow = _project(tmp_path, fake, will={
        "exit": 1, "statuses": {"daily_revenue": "error"}, "tests": [{"name": "unique", "status": "skipped"}]})

    result = run_workflow(workflow, window=DAY1)

    assert _statuses(result) == {"orders": "published", "gold": "failed"} and result.exit_code == 1
    gold = result.tasks["gold"]
    assert gold.detail["failed_nodes"] == [{"node": "model.shop.daily_revenue", "status": "error"}]
    assert gold.detail["nodes"] == {"error": 1, "skipped": 1}
    failed = [e for e in _ledger(tmp_path) if e.type == "run.failed" and e.payload.get("task_id") == "gold"]
    assert len(failed) == 1 and failed[0].payload["error_type"] == "TaskFailed"


def test_a_test_whose_own_sql_failed_is_a_failure_even_beside_a_failed_test(tmp_path, fake):
    workflow = _project(tmp_path, fake, will={
        "exit": 1, "tests": [{"name": "a", "status": "fail"}, {"name": "b", "status": "error"}]})

    assert run_workflow(workflow, window=DAY1).tasks["gold"].status == "failed"


def test_a_unit_test_that_fails_is_a_failure_because_the_logic_of_a_model_is_wrong_and_not_the_data(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    manifest, results = dbt_artifacts(dbt_model("daily_revenue", meta=GOLD_META))
    results["results"].append({"unique_id": "unit_test.shop.daily_revenue.adds_up", "status": "fail", "message": ""})
    fake.will(database=str(tmp_path / "dbt" / "target" / "ldp_build.duckdb"), sql=CREATE, exit=1,
              artifacts={"manifest.json": manifest, "run_results.json": results})

    result = run_workflow(workflow, window=DAY1)

    assert _statuses(result) == {"orders": "published", "gold": "failed"} and result.exit_code == 1
    assert result.tasks["gold"].detail["failed_nodes"] == [
        {"node": "unit_test.shop.daily_revenue.adds_up", "status": "fail"}]


def test_a_dbt_that_wrote_no_artifacts_is_a_failure_with_no_nodes(tmp_path, fake):
    workflow = _project(tmp_path, fake, will={"exit": 2, "artifacts": False})

    result = run_workflow(workflow, window=DAY1)

    gold = result.tasks["gold"]
    assert gold.status == "failed" and result.exit_code == 1
    assert gold.error.splitlines()[0].endswith("dbt build failed with exit status 2, so nothing was published")
    assert gold.detail["exit_status"] == 2 and gold.detail["nodes"] == {} and gold.detail["failed_nodes"] == []


def test_artifacts_from_an_earlier_run_are_not_taken_for_this_one(tmp_path, fake):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)
    for name in ("manifest.json", "run_results.json"):
        os.utime(tmp_path / "dbt" / "target" / name, (1_000_000, 1_000_000))
    _will(fake, tmp_path, exit=2, artifacts=False)
    (tmp_path / "dbt" / "models" / "daily_revenue.sql").write_text("select 3 as day")

    gold = run_workflow(workflow, window=DAY1).tasks["gold"]

    assert gold.status == "failed" and gold.detail["nodes"] == {} and gold.detail["failed_nodes"] == []


def test_a_quality_check_that_blocks_the_write_of_a_model_is_a_data_gate(tmp_path, fake):
    meta = {**GOLD_META, "ldp_quality": [{"check": "row_count", "min": 5}]}
    workflow = _project(tmp_path, fake, will={"models": [dbt_model("daily_revenue", meta=meta)]})

    result = run_workflow(workflow, window=DAY1)

    gold = result.tasks["gold"]
    assert gold.status == "blocked_quality" and result.exit_code == 3
    assert gold.detail["failed_model"] == "daily_revenue" and gold.detail["published"] == []
    assert gold.detail["not_published"] == [] and "publishing daily_revenue failed" in gold.error


def test_a_write_that_failed_for_another_reason_is_a_failure_and_names_what_was_published(tmp_path, fake):
    models = [dbt_model("daily_revenue", meta=GOLD_META), dbt_model("missing_model", meta=GOLD_META)]
    workflow = _project(tmp_path, fake, will={"models": models})  # the stand-in builds only daily_revenue

    gold = run_workflow(workflow, window=DAY1).tasks["gold"]

    assert gold.status == "failed" and gold.detail["failed_model"] == "missing_model"
    assert gold.detail["published"] == ["gold.daily_revenue"] and gold.detail["not_published"] == []
    assert "published: gold.daily_revenue" in gold.error and _gold(tmp_path).exists()


def test_a_test_command_runs_dbt_test_and_publishes_nothing(tmp_path, fake):
    workflow = _project(tmp_path, fake, dbt={"command": "test", "select": "tag:nightly"},
                        will={"tests": [{"name": "unique_daily_revenue_day"}]})

    result = run_workflow(workflow, window=DAY1)

    assert _statuses(result) == {"orders": "published", "gold": "succeeded"}
    [call] = _calls(fake)
    assert call[:3] == ["test", "--select", "tag:nightly"] and _vars(call)["window_start"] == WINDOW1[0]
    detail = result.tasks["gold"].detail
    assert detail["command"] == "test" and detail["nodes"] == {"success": 1, "pass": 1} and "published" not in detail
    assert not _gold(tmp_path).exists()


def test_failing_tests_of_a_test_command_are_a_data_gate(tmp_path, fake):
    workflow = _project(tmp_path, fake, dbt={"command": "test"},
                        will={"exit": 1, "tests": [{"name": "t", "status": "fail"}]})

    gold = run_workflow(workflow, window=DAY1).tasks["gold"]

    assert gold.status == "blocked_quality" and gold.error.splitlines()[0].endswith("dbt test exited with status 1")
    assert gold.detail["failed_nodes"] == [{"node": "test.shop.t.abc123", "status": "fail"}]


def test_the_failed_nodes_of_an_event_are_capped(tmp_path, fake):
    many = [{"name": f"t{number}", "status": "fail"} for number in range(25)]
    workflow = _project(tmp_path, fake, will={"exit": 1, "tests": many})

    gold = run_workflow(workflow, window=DAY1).tasks["gold"]

    assert gold.status == "blocked_quality" and len(gold.detail["failed_nodes"]) == 10
    assert gold.detail["failed_total"] == 25 and "... and 15 more" in gold.error


# ---------------------------------------------------------------------- the command line


def test_the_run_command_keeps_standard_output_for_its_result(tmp_path, fake, capfd):
    workflow = _project(tmp_path, fake)

    status = main(["workflow", "run", str(workflow), "--window", DAY1, "--json"])

    out, err = capfd.readouterr()
    assert status == 0
    printed = json.loads(out)  # all of standard output is the result, so dbt's lines went elsewhere
    assert [task["status"] for task in printed["tasks"]] == ["published", "succeeded"]
    assert "fake dbt ran" in err


def test_the_json_result_is_clean_when_ldp_runs_as_a_child_process(tmp_path, fake):
    workflow = _project(tmp_path, fake)

    done = subprocess.run([sys.executable, "-m", "local_data_platform.cli", "workflow", "run", str(workflow),
                           "--window", DAY1, "--json"], capture_output=True, text=True, timeout=300, check=False)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["status"] == "succeeded" and "fake dbt ran" in done.stderr


def test_a_data_gate_of_dbt_exits_with_3(tmp_path, fake, capfd):
    workflow = _project(tmp_path, fake, will={"exit": 1, "tests": [{"name": "t", "status": "fail"}]})

    status = main(["workflow", "run", str(workflow), "--window", DAY1])

    out, _ = capfd.readouterr()
    assert status == 3 and any(line.split()[:3] == ["gold", "dbt", "blocked_quality"] for line in out.splitlines())


def test_the_result_table_lists_the_dbt_task_and_the_table_it_published(tmp_path, fake, capfd):
    workflow = _project(tmp_path, fake)

    assert main(["workflow", "run", str(workflow), "--window", DAY1]) == 0

    out = capfd.readouterr().out
    assert any(line.split()[:3] == ["gold", "dbt", "succeeded"] for line in out.splitlines()), out
    assert "NULL" not in out, "a dbt task has no rows read or written, so its cells are dashes"
    snapshot = _gold(tmp_path).table().current_snapshot().snapshot_id
    assert f"gold published gold.daily_revenue: overwrite, 0 -> 1 rows, snapshot {snapshot}" in out.splitlines()
    assert main(["workflow", "run", str(workflow), "--window", DAY1]) == 0
    assert "published gold." not in capfd.readouterr().out, "a skipped task published nothing"


def test_the_runs_command_lists_the_models_the_task_published(tmp_path, fake, capfd):
    workflow = _project(tmp_path, fake)
    run_workflow(workflow, window=DAY1)

    assert main(["runs", str(tmp_path / ".ldp" / "events.jsonl")]) == 0

    out = capfd.readouterr().out
    assert "daily_revenue" in out and "daily_orders" in out and "gold" in out


# ---------------------------------------------------------------------- what the runner gives a kind


def test_the_context_says_whether_an_earlier_task_wrote_data():
    def context(*results):
        return RunContext(workflow=None, bounds=None, run_id="r", sink=None,
                          finished={str(n): SimpleNamespace(status=s, kind=k) for n, (s, k) in enumerate(results)})

    assert context().wrote_data() is False
    assert context(("skipped_duplicate", "pipeline"), ("upstream_failed", "dbt"),
                   ("succeeded", "maintain")).wrote_data() is False
    assert context(("skipped_duplicate", "pipeline"), ("published", "pipeline")).wrote_data() is True
    assert context(("succeeded", "dbt")).wrote_data() is True
    assert context(("succeeded", "maintain"), ("succeeded", "dbt")).wrote_data() is True
    # a task that stopped half way may have written part of its data
    assert context(("failed", "dbt")).wrote_data() is True
    assert context(("blocked_quality", "pipeline")).wrote_data() is True


def test_a_task_error_carries_its_facts():
    failed, blocked = TaskFailed("no", {"a": 1}), TaskBlocked("held", {"b": 2})

    assert failed.detail == {"a": 1} and blocked.detail == {"b": 2} and blocked.report is None
    assert TaskFailed("x").detail == {} and TaskBlocked("y").detail == {}
    assert isinstance(failed, LDPError) and isinstance(blocked, DataQualityError)


def test_checking_a_dbt_task_needs_neither_dbt_nor_pyiceberg_nor_duckdb():
    probe = ("import sys\nfrom local_data_platform.spec import validate_spec\n"
             "spec = {'kind': 'Workflow', 'identifier': 'w', 'tasks': [{'id': 'a', 'dbt': {'project': 'p'}}]}\n"
             "print(validate_spec(spec))\n"
             "print(sorted(m for m in ('dbt', 'duckdb', 'pyiceberg', 'yaml') if m in sys.modules))\n")

    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:2] == ["[]", "[]"]


@pytest.mark.parametrize("first", [
    "local_data_platform.orchestration.dbt_task", "local_data_platform.orchestration.tasks",
    "local_data_platform.orchestration.runner", "local_data_platform.orchestration.workflow",
    "local_data_platform.orchestration", "local_data_platform.spec", "local_data_platform.dbt_layer.build",
])
def test_the_dbt_kind_is_registered_whichever_module_of_orchestration_is_imported_first(first):
    """The dbt kind imports from the registry and the registry imports the dbt kind, so the order must not matter."""
    probe = (f"import {first}\nfrom local_data_platform.orchestration import tasks\n"
             "import local_data_platform.orchestration.dbt_task\nprint(tasks.task_kinds())\n")

    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['pipeline', 'maintain', 'dbt']"
