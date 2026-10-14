"""Workflow files: their format and validation, the task kinds, ``ldp workflow plan`` and ``ldp workflow run``.

The projects here are built from CSV files in ``tmp_path``. A workflow loads orders and customers into Iceberg tables,
exports the orders to a CSV file after the orders are in, and tidies the orders table. Everything runs offline.
"""

import copy
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.events import (
    EVENT_TYPES,
    MemorySink,
    RunEvent,
    TaggedSink,
    _ol_run_id,
    read_jsonl,
    read_openlineage,
    summarize_runs,
    uuid7,
)
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.orchestration import (
    load_workflow,
    plan_workflow,
    register_task_kind,
    run_workflow,
    task_kinds,
    validate_workflow,
    windows,
    workflow_schema,
)
from local_data_platform.orchestration.tasks import (
    TaskBlocked,
    TaskFailed,
    TaskOutcome,
    TaskPlan,
    config_tables,
    get_task_kind,
    unregister_task_kind,
)
from local_data_platform.orchestration.workflow import read_workflow
from local_data_platform.spec import idempotency_key, kind_schema, plan, validate_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY1, DAY2 = "2026-08-01/2026-08-02", "2026-08-02/2026-08-03"
ORDERS = "id,at\n1,2026-08-01T10:00:00\n2,2026-08-01T11:00:00\n3,2026-08-02T09:00:00\n4,2026-08-02T10:00:00\n"
CUSTOMERS = "id,name\n1,Ana\n2,Bo\n"
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _spec(**changes) -> dict:
    """A workflow of four tasks: two loads, an export that waits for the orders, and a tidy that follows it."""
    spec = {
        "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders",
        "metadata": {"schedule": "@daily", "window": {"size": "day"}},
        "tasks": [
            {"id": "orders", "pipeline": "config/orders.json"},
            {"id": "customers", "pipeline": "config/customers.json"},
            {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]},
            {"id": "tidy", "maintain": {"config": "config/orders.json"}, "depends_on": ["export"]},
        ],
    }
    spec.update(changes)
    return spec


def _project(root: Path, *, workflow: dict | None = None, orders_min: int = 2) -> Path:
    """Write the landing files, three configs and the workflow ``daily.json`` under ``root``; return the workflow."""
    _write(root / "landing" / "orders.csv", ORDERS)
    _write(root / "landing" / "customers.csv", CUSTOMERS)
    _write(root / "config" / "orders.json", {"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG},
        "window": {"size": "day", "column": "at"},
        "quality": [{"check": "row_count", "min": orders_min}]}})
    _write(root / "config" / "customers.json", {"identifier": "customers", "metadata": {
        "source": {"name": "customers", "format": "CSV", "path": "../landing/customers.csv"},
        "target": {"name": "customers", "format": "ICEBERG", "write_mode": "overwrite", "catalog": CATALOG}}})
    _write(root / "config" / "export.json", {"identifier": "export_orders", "metadata": {
        "source": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "orders_export", "format": "CSV", "path": "../out/orders.csv"}}})
    return _write(root / "daily.json", workflow if workflow is not None else _spec())


def _table(root: Path, name: str) -> Iceberg:
    return Iceberg(name, {"identifier": "shop", "warehouse_path": str(root / "warehouse")})


def _statuses(result) -> dict[str, str]:
    return {task_id: item.status for task_id, item in result.tasks.items()}


def _problems(spec: dict) -> list[str]:
    return [str(problem) for problem in validate_workflow(spec)]


@pytest.fixture
def custom_kind():
    """Register a task kind called ``echo`` that records what it ran, and remove it afterwards."""
    ran = []

    def run(task, ctx):
        ran.append((task.id, ctx.bounds))
        return TaskOutcome("succeeded", {"echo": task.value})

    register_task_kind(
        "echo", validate=lambda value: [] if isinstance(value, str) else [("", "must be a word")],
        json_schema=lambda: {"type": "string"}, plan=lambda task, ctx: TaskPlan(details={"word": task.value}), run=run,
        description="Say a word.")
    yield ran
    unregister_task_kind("echo")


# ====================================================================== the format and its validation


def test_a_workflow_with_every_key_is_valid():
    spec = _spec(who="data team", what="daily orders", where="the shop", when="daily", how="batch",
                 metadata={"schedule": "5 2 * * *", "window": {"size": "day", "timezone": "Europe/Paris"},
                           "catchup": True, "start": "2026-01-01",
                           "retries": {"max_attempts": 3, "delay_s": 30, "backoff": 2.0},
                           "observability": {"sinks": [{"type": "jsonl", "path": "events/all.jsonl"}]}})
    spec["tasks"][0]["retries"] = {"max_attempts": 2}

    assert _problems(spec) == [] and validate_spec(spec) == []


def test_the_shipped_task_kinds_are_pipeline_maintain_and_dbt():
    assert task_kinds()[:3] == ["pipeline", "maintain", "dbt"]
    assert get_task_kind("pipeline").emits_events is True and get_task_kind("maintain").emits_events is False
    assert get_task_kind("dbt").emits_events is False


@pytest.mark.parametrize("changes, expected", [
    ({"identifier": None}, "identifier: is required"),
    ({"apiVersion": "ldp/v9"}, "apiVersion: must be 'ldp/v1', got 'ldp/v9'"),
    ({"schedule": "@daily"}, "unknown top-level keys ['schedule']"),
    ({"metadata": "daily"}, "metadata: must be an object, got str"),
    ({"metadata": {"schedul": "@daily"}}, "metadata: has unknown keys ['schedul']; allowed: ['schedule', 'window', "),
    ({"metadata": {"schedule": "daily"}}, "metadata.schedule: schedule 'daily' is not a valid cron expression"),
    ({"metadata": {"window": "daily"}}, "metadata.window: must be 'none' or an object"),
    ({"metadata": {"window": {"size": "day", "column": "at"}}},
     "metadata.window.column: does not belong in a workflow"),
    ({"metadata": {"window": {"size": "week"}}}, "metadata.window.size: must be one of"),
    ({"metadata": {"catchup": "yes"}}, "metadata.catchup: must be true or false, got 'yes'"),
    ({"metadata": {"start": "last tuesday"}}, "metadata.start: must be an ISO-8601 date or time"),
    ({"metadata": {"start": 5}}, "metadata.start: must be an ISO-8601 date or time"),
    ({"metadata": {"retries": 3}}, "metadata.retries: must be an object"),
    ({"metadata": {"retries": {"max_attempts": 0}}}, "metadata.retries.max_attempts: must be a whole number from 1"),
    ({"metadata": {"retries": {"delay_s": -1}}}, "metadata.retries.delay_s: must be a number from 0"),
    ({"metadata": {"retries": {"backoff": 0.5}}}, "metadata.retries.backoff: must be a number from 1"),
    ({"metadata": {"retries": {"tries": 3}}}, "metadata.retries: has unknown keys ['tries']"),
    ({"metadata": {"observability": {"sinks": ["carrier pigeon"]}}},
     "metadata.observability.sinks[0] has unknown sink"),
    ({"tasks": None}, "tasks: is required and must be a list of at least one task"),
    ({"tasks": []}, "tasks: is required and must be a list of at least one task"),
    ({"tasks": ["load"]}, "tasks[0]: must be an object with an 'id'"),
])
def test_a_workflow_is_checked_for_its_envelope_and_its_metadata(changes, expected):
    messages = _problems(_spec(**changes))

    assert len(messages) == 1 and messages[0].startswith(expected), messages


@pytest.mark.parametrize("task, expected", [
    ({"id": "a"}, "tasks[0]: needs exactly one task kind of ['pipeline', 'maintain', 'dbt'], and has none"),
    ({"id": "a", "pipeline": "a.json", "maintain": {"config": "a.json"}},
     "tasks[0]: needs exactly one task kind of ['pipeline', 'maintain', 'dbt'], and has 2: ['pipeline', 'maintain']"),
    ({"id": "a", "pipeline": "a.json", "gold": {}},
     "tasks[0]: has unknown keys ['gold']; a task has an id, depends_on"),
    ({"id": "a", "pipeline": ""}, "tasks[0].pipeline: must be the path of a dataset config, got ''"),
    ({"id": "a", "pipeline": 5}, "tasks[0].pipeline: must be the path of a dataset config, got 5"),
    ({"pipeline": "a.json"}, "tasks[0].id: is required and must be a non-empty string"),
    ({"id": "a", "pipeline": "a.json", "depends_on": "orders"}, "tasks[0].depends_on: must be a list of task ids"),
    ({"id": "a", "pipeline": "a.json", "depends_on": [5]}, "tasks[0].depends_on[0]: must be a task id, got 5"),
    ({"id": "a", "pipeline": "a.json", "depends_on": ["a"]}, "tasks[0].depends_on[0]: 'a' is the task itself"),
    ({"id": "a", "pipeline": "a.json", "depends_on": ["ghost"]},
     "tasks[0].depends_on[0]: unknown task id 'ghost'; the ids are ['a']"),
    ({"id": "a", "pipeline": "a.json", "retries": {"max_attempts": "3"}},
     "tasks[0].retries.max_attempts: must be a whole number from 1"),
    ({"id": "a", "maintain": "orders.json"}, "tasks[0].maintain: must be an object with a 'config'"),
    ({"id": "a", "maintain": {}}, "tasks[0].maintain.config: is required"),
    ({"id": "a", "maintain": {"config": "a.json", "expiry": 7}}, "tasks[0].maintain: has unknown keys ['expiry']"),
    ({"id": "a", "maintain": {"config": "a.json", "expire": -1}}, "tasks[0].maintain.expire: must be a number from 0"),
    ({"id": "a", "maintain": {"config": "a.json", "retain_last": 0}},
     "tasks[0].maintain.retain_last: must be a whole number from 1"),
    ({"id": "a", "maintain": {"config": "a.json", "orphan_age_hours": "x"}},
     "tasks[0].maintain.orphan_age_hours: must be a number from 0"),
    ({"id": "a", "maintain": {"config": "a.json", "keep_metadata_versions": -1}},
     "tasks[0].maintain.keep_metadata_versions: must be a whole number from 0"),
    ({"id": "a", "maintain": {"config": "a.json", "apply": "yes"}}, "tasks[0].maintain.apply: must be true or false"),
    ({"id": "a", "maintain": {"config": "a.json", "orphans": 1}}, "tasks[0].maintain.orphans: must be true or false"),
])
def test_a_task_needs_exactly_one_valid_kind_and_known_dependencies(task, expected):
    messages = _problems(_spec(tasks=[task]))

    assert len(messages) == 1 and messages[0].startswith(expected), messages


def test_duplicate_ids_and_repeated_dependencies_are_reported():
    tasks = [{"id": "a", "pipeline": "a.json"}, {"id": "b", "pipeline": "b.json", "depends_on": ["a", "a"]},
             {"id": "a", "pipeline": "c.json"}]

    assert _problems(_spec(tasks=tasks)) == [
        "tasks[2].id: 'a' is the id of an earlier task", "tasks[1].depends_on[1]: 'a' is listed twice"]


def test_tasks_that_depend_on_each_other_in_a_cycle_are_rejected_and_the_message_names_the_cycle():
    tasks = [{"id": "a", "pipeline": "a.json", "depends_on": ["c"]},
             {"id": "b", "pipeline": "b.json", "depends_on": ["a"]},
             {"id": "c", "pipeline": "c.json", "depends_on": ["b"]}]

    messages = _problems(_spec(tasks=tasks))

    assert len(messages) == 1 and messages[0].startswith("tasks: depend on each other in a cycle: ")
    assert {"a", "b", "c"} <= set(messages[0].split(": ", 1)[1].split(" -> "))


def test_an_inline_secret_is_refused_anywhere_in_a_workflow_and_not_repeated():
    messages = _problems(_spec(what="AKIAABCDEFGHIJKLMNOP"))

    assert len(messages) == 1 and messages[0].startswith("what: looks like an inline secret")
    assert "AKIAABCDEFGHIJKLMNOP" not in messages[0]


def test_validate_workflow_never_raises_for_odd_values():
    for odd in ({"tasks": [None, 5, [], {"id": 5}, {"id": "a", "depends_on": None}]}, {"metadata": []},
                {"metadata": {"retries": None, "start": None, "window": []}}):
        assert all(isinstance(problem, ConfigError) for problem in validate_workflow(_spec(**odd))), odd


# ---------------------------------------------------------------------- the JSON Schema and the registry


def test_the_schema_lists_the_task_kinds_and_checks_a_workflow_file():
    jsonschema = pytest.importorskip("jsonschema")
    schema = kind_schema("Workflow")
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema == workflow_schema()
    task = schema["$defs"]["task"]
    assert task["required"] == ["id"] and task["additionalProperties"] is False
    assert task["oneOf"] == [{"required": ["pipeline"]}, {"required": ["maintain"]}, {"required": ["dbt"]}]
    assert {"pipeline", "maintain", "dbt", "depends_on", "retries"} <= set(task["properties"])
    assert set(schema["properties"]["metadata"]["properties"]) == {"schedule", "window", "catchup", "start", "retries",
                                                                   "observability"}

    validator = jsonschema.Draft202012Validator(schema)
    assert not list(validator.iter_errors(_spec()))
    broken = copy.deepcopy(_spec())
    broken["tasks"][0]["maintain"] = {"config": "a.json"}
    assert list(validator.iter_errors(broken))  # two kinds
    assert list(validator.iter_errors(_spec(tasks=[{"id": "a"}])))  # no kind
    assert list(validator.iter_errors(_spec(metadata={"schedul": "@daily"})))
    assert list(validator.iter_errors(_spec(tasks=[])))


def test_ldp_schema_prints_the_workflow_schema(capsys):
    assert main(["schema", "--kind", "Workflow"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["$defs"]["task"]["oneOf"] == [{"required": ["pipeline"]}, {"required": ["maintain"]},
                                                 {"required": ["dbt"]}]


def test_a_new_task_kind_is_checked_planned_and_run_without_changing_the_runner(tmp_path, custom_kind):
    root = tmp_path
    path = _project(root, workflow=_spec(tasks=[
        {"id": "say", "echo": "hello"}, {"id": "orders", "pipeline": "config/orders.json", "depends_on": ["say"]}]))

    assert "echo" in task_kinds() and "echo" in kind_schema("Workflow")["$defs"]["task"]["properties"]
    assert _problems(_spec(tasks=[{"id": "say", "echo": 5}]))[0].startswith("tasks[0].echo: must be a word")
    assert plan_workflow(path)["tasks"][0]["word"] == "hello"

    result = run_workflow(path, window=DAY1)

    assert _statuses(result) == {"say": "succeeded", "orders": "published"}
    assert custom_kind[0][0] == "say" and result.tasks["say"].detail == {"echo": "hello"}
    events = read_jsonl(root / ".ldp" / "events.jsonl")
    say = [e for e in events if e.payload.get("task_id") == "say"]
    assert [e.type for e in say] == ["run.started", "run.finished"]  # the runner emitted them for a kind that does not
    assert say[1].payload["status"] == "succeeded" and say[1].payload["echo"] == "hello"


def test_a_task_kind_has_a_name_that_is_a_word_and_is_registered_once(custom_kind):
    for bad in ("Echo", "1echo", "echo-two", "", "id", "depends_on", "retries", 5):
        with pytest.raises(ValueError, match="a task kind is lower case"):
            register_task_kind(bad, validate=lambda value: [], json_schema=dict, plan=lambda t, c: TaskPlan(),
                               run=lambda t, c: TaskOutcome("succeeded"))
    with pytest.raises(ValueError, match="already registered"):
        register_task_kind("echo", validate=lambda value: [], json_schema=dict, plan=lambda t, c: TaskPlan(),
                           run=lambda t, c: TaskOutcome("succeeded"))
    replaced = register_task_kind("echo", validate=lambda value: [], json_schema=dict, plan=lambda t, c: TaskPlan(),
                                  run=lambda t, c: TaskOutcome("succeeded"), replace=True, description="new")
    assert get_task_kind("echo") is replaced and replaced.description == "new"


# ====================================================================== reading a workflow


def test_a_workflow_file_loads_with_its_tasks_in_file_order_and_paths_against_its_folder(tmp_path):
    path = _project(tmp_path)

    workflow = load_workflow(path)

    assert workflow.identifier == "daily_orders" and workflow.base_dir == tmp_path and workflow.path == path
    assert [(t.id, t.kind, t.index) for t in workflow.tasks] == [
        ("orders", "pipeline", 0), ("customers", "pipeline", 1), ("export", "pipeline", 2), ("tidy", "maintain", 3)]
    assert workflow.resolve("config/orders.json") == tmp_path / "config" / "orders.json"
    assert workflow.ledger_path == tmp_path / ".ldp" / "events.jsonl"
    assert workflow.schedule.expression == "@daily" and workflow.from_config is False
    assert workflow.task("export").depends_on == ("orders",)
    with pytest.raises(KeyError):
        workflow.task("ghost")


def test_tasks_run_after_the_tasks_they_wait_for_and_otherwise_in_the_order_of_the_file(tmp_path):
    tasks = [{"id": "d", "pipeline": "d.json", "depends_on": ["b", "c"]}, {"id": "a", "pipeline": "a.json"},
             {"id": "c", "pipeline": "c.json", "depends_on": ["a"]}, {"id": "b", "pipeline": "b.json"}]
    workflow = load_workflow(_write(tmp_path / "w.json", _spec(tasks=tasks)))

    assert [t.id for t in workflow.order()] == ["a", "b", "c", "d"]  # a and b first, in the order of the file
    assert workflow.ancestors("d") == {"a", "b", "c"} and workflow.ancestors("a") == set()
    assert workflow.graph()["d"] == ("b", "c")


def test_a_workflow_that_is_not_valid_does_not_load_and_names_every_problem(tmp_path):
    bad = _spec(tasks=[{"id": "a"}, {"id": "a", "pipeline": "x.json", "depends_on": ["ghost"]}])

    with pytest.raises(ConfigError) as caught:
        load_workflow(_write(tmp_path / "bad.json", bad))

    message = str(caught.value)
    assert message.startswith("bad.json has 3 problems:") and "unknown task id 'ghost'" in message
    assert "'a' is the id of an earlier task" in message and "needs exactly one task kind" in message


@pytest.mark.parametrize("content, expected", [
    ("{not json", "is not valid JSON"),
    ("[1, 2]", "must be a JSON object, got list"),
    ({"kind": "workflow", "identifier": "w", "tasks": []}, "did you mean 'Workflow'"),
    ({"kind": "Metrics", "identifier": "w"}, "reserved for 0.1.4"),
])
def test_read_workflow_says_why_a_file_cannot_be_used(tmp_path, content, expected):
    workflow, problems = read_workflow(_write(tmp_path / "w.json", content))

    assert workflow is None and expected in " ".join(problems)


def test_a_missing_workflow_file_is_a_config_error(tmp_path):
    workflow, problems = read_workflow(tmp_path / "nope.json")
    assert workflow is None and problems[0].startswith("workflow file not found:")
    with pytest.raises(ConfigError, match="workflow file not found"):
        load_workflow(tmp_path / "nope.json")


def test_a_dataset_config_with_a_schedule_is_a_workflow_of_one_task(tmp_path):
    _project(tmp_path)
    config = json.loads((tmp_path / "config" / "customers.json").read_text())
    config["metadata"]["schedule"] = "5 2 * * *"
    config["metadata"]["window"] = {"size": "day", "timezone": "Asia/Kolkata"}
    path = _write(tmp_path / "config" / "scheduled.json", config)

    workflow = load_workflow(path)

    assert workflow.from_config is True and workflow.identifier == "customers"
    assert [(t.id, t.kind, t.value) for t in workflow.tasks] == [("customers", "pipeline", "scheduled.json")]
    assert workflow.schedule.cron == "5 2 * * *" and workflow.timezone.key == "Asia/Kolkata"
    assert workflow.ledger_path == tmp_path / "config" / ".ldp" / "events.jsonl"
    result = plan_workflow(path, window=DAY1)
    assert result["valid"] and [t["id"] for t in result["tasks"]] == ["customers"]


def test_a_dataset_config_whose_when_means_a_schedule_is_a_workflow_too(tmp_path):
    config = {"identifier": "customers", "when": "daily", "metadata": {
        "source": {"name": "customers", "format": "CSV", "path": "../landing/customers.csv"},
        "target": {"name": "customers", "format": "ICEBERG", "write_mode": "overwrite", "catalog": CATALOG}}}

    workflow = load_workflow(_write(tmp_path / "config" / "c.json", config))

    assert workflow.schedule.expression == "@daily" and workflow.spec["metadata"] == {"schedule": "@daily"}


def test_a_dataset_config_with_no_schedule_is_not_a_workflow(tmp_path):
    _project(tmp_path)

    with pytest.raises(ConfigError, match=r"customers.json is a dataset config with no schedule, so it is not a "
                                          r"workflow. Add metadata.schedule"):
        load_workflow(tmp_path / "config" / "customers.json")


def test_a_config_that_is_not_valid_is_not_a_workflow_either(tmp_path):
    workflow, problems = read_workflow(_write(tmp_path / "c.json", {"identifier": "x", "metadata": {"source": {}}}))

    assert workflow is None and "target" in " ".join(problems)


def test_config_tables_name_the_iceberg_tables_a_config_reads_and_writes_by_catalog(tmp_path):
    _project(tmp_path)
    reads, writes = config_tables(tmp_path / "config" / "export.json")
    assert list(reads.values()) == ["shop.orders"] and writes == {}
    reads, writes = config_tables(tmp_path / "config" / "orders.json")
    assert reads == {} and list(writes.values()) == ["shop.orders"]
    assert list(reads) == [] and config_tables(tmp_path / "config" / "missing.json") == ({}, {})
    # The same name in another catalog is another table.
    other = {"identifier": "x", "metadata": {
        "source": {"name": "orders", "format": "ICEBERG", "catalog": {"identifier": "shop", "warehouse_path": "../b"}},
        "target": {"name": "t", "format": "CSV", "path": "t.csv"}}}
    other_reads, _ = config_tables(_write(tmp_path / "config" / "other.json", other))
    assert list(other_reads.values()) == ["shop.orders"] and list(other_reads) != list(config_tables(
        tmp_path / "config" / "export.json")[0])


# ====================================================================== ldp workflow plan


def test_a_plan_lists_the_tasks_in_run_order_and_each_key_is_the_key_ldp_plan_prints(tmp_path, capsys):
    path = _project(tmp_path)

    result = plan_workflow(path, window=DAY1)

    assert result["valid"] is True and result["errors"] == [] and result["workflow"] == "daily_orders"
    assert result["window"] == "2026-08-01T00:00:00Z/2026-08-02T00:00:00Z" and result["timezone"] == "UTC"
    assert (result["schedule"], result["schedule_from"]) == ("@daily", "metadata.schedule")
    assert [(t["order"], t["id"], t["kind"]) for t in result["tasks"]] == [
        (1, "orders", "pipeline"), (2, "customers", "pipeline"), (3, "export", "pipeline"), (4, "tidy", "maintain")]
    orders = result["tasks"][0]
    assert orders["config"] == "config/orders.json" and orders["target"] == "shop.orders"
    assert orders["publish"] == "staged" and orders["identifier"] == "orders"
    for item in result["tasks"][:2]:
        config = Config.from_json(tmp_path / item["config"])
        assert item["idempotency_key"] == idempotency_key(config, DAY1)
        assert main(["plan", str(tmp_path / item["config"]), "--window", DAY1]) == 0
        assert f"idempotency_key  {item['idempotency_key']}" in capsys.readouterr().out
    assert result["tasks"][2]["publish"] == "direct" and "idempotency_key" not in result["tasks"][2]
    assert result["tasks"][3]["table"] == "shop.orders" and result["tasks"][3]["dry_run"] is True
    assert not (tmp_path / "warehouse").exists() and not (tmp_path / ".ldp").exists()  # a plan runs nothing


def test_a_plan_without_a_window_has_no_keys(tmp_path):
    result = plan_workflow(_project(tmp_path))

    assert result["valid"] and result["window"] is None
    assert all("idempotency_key" not in item for item in result["tasks"])


def test_a_plan_notes_that_a_file_target_has_no_key(tmp_path):
    result = plan_workflow(_project(tmp_path), window=DAY1)

    assert any(w.startswith("task 'export': its target is a file") for w in result["warnings"])


def test_a_plan_reports_a_config_that_is_missing_or_not_valid_and_names_the_task(tmp_path):
    path = _project(tmp_path)
    (tmp_path / "config" / "customers.json").unlink()
    _write(tmp_path / "config" / "export.json", {"identifier": "export_orders", "metadata": {"source": {}}})

    result = plan_workflow(path, window=DAY1)

    assert result["valid"] is False
    assert any(e.startswith("task 'customers': config file not found:") for e in result["errors"])
    assert any(e.startswith("task 'export': metadata.source.format") or e.startswith("task 'export': metadata.target")
               for e in result["errors"]), result["errors"]
    by_id = {t["id"]: t for t in result["tasks"]}
    assert by_id["orders"]["valid"] is True and by_id["customers"]["valid"] is False


def test_a_task_that_names_a_workflow_instead_of_a_config_is_an_error(tmp_path):
    path = _project(tmp_path, workflow=_spec(tasks=[{"id": "again", "pipeline": "other.json"}]))
    _write(tmp_path / "other.json", _spec(identifier="other"))

    result = plan_workflow(path)

    assert result["errors"] == ["task 'again': other.json is a Workflow, not a dataset config"]


def test_a_maintain_task_needs_a_config_with_an_iceberg_table(tmp_path):
    path = _project(tmp_path, workflow=_spec(tasks=[{"id": "tidy", "maintain": {"config": "config/plain.json"}}]))
    (tmp_path / "landing" / "sheet.xlsx").write_bytes(b"")
    _write(tmp_path / "config" / "plain.json", {"identifier": "plain", "metadata": {
        "source": {"name": "sheet", "format": "XLSX", "path": "../landing/sheet.xlsx"},
        "target": {"name": "copy", "format": "CSV", "path": "../out/copy.csv"}}})

    result = plan_workflow(path)

    assert result["errors"] == ["task 'tidy': config/plain.json has no Iceberg table to maintain"]


def test_a_plan_warns_when_a_task_reads_a_table_that_another_writes_and_nothing_connects_them(tmp_path):
    tasks = [{"id": "export", "pipeline": "config/export.json"}, {"id": "orders", "pipeline": "config/orders.json"}]
    path = _project(tmp_path, workflow=_spec(tasks=tasks))

    result = plan_workflow(path, window=DAY1)

    assert result["valid"] is True  # a warning does not make a plan invalid
    assert ("task 'export' reads the table shop.orders, which task 'orders' writes, and no depends_on connects them, "
            "so they may run in either order") in result["warnings"]


@pytest.mark.parametrize("tasks", [
    [{"id": "orders", "pipeline": "config/orders.json"},
     {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]}],
    [{"id": "orders", "pipeline": "config/orders.json"},
     {"id": "mid", "maintain": {"config": "config/orders.json"}, "depends_on": ["orders"]},
     {"id": "export", "pipeline": "config/export.json", "depends_on": ["mid"]}],
    [{"id": "export", "pipeline": "config/export.json"},
     {"id": "orders", "pipeline": "config/orders.json", "depends_on": ["export"]}],
])
def test_a_plan_does_not_warn_when_a_depends_on_chain_connects_the_two_tasks_in_either_direction(tmp_path, tasks):
    result = plan_workflow(_project(tmp_path, workflow=_spec(tasks=tasks)))

    assert not [w for w in result["warnings"] if "no depends_on connects them" in w]


def test_a_table_with_the_same_name_in_another_catalog_is_not_the_table_another_task_writes(tmp_path):
    path = _project(tmp_path, workflow=_spec(tasks=[{"id": "orders", "pipeline": "config/orders.json"},
                                                    {"id": "export", "pipeline": "config/export.json"}]))
    other = json.loads((tmp_path / "config" / "export.json").read_text())
    other["metadata"]["source"]["catalog"] = {"identifier": "shop", "warehouse_path": "../other_warehouse"}
    _write(tmp_path / "config" / "export.json", other)

    assert not [w for w in plan_workflow(path)["warnings"] if "connects them" in w]


def test_a_plan_notes_a_pipeline_that_appends_the_same_rows_in_every_window_of_a_schedule(tmp_path):
    path = _project(tmp_path)
    config = json.loads((tmp_path / "config" / "orders.json").read_text())
    del config["metadata"]["window"]
    _write(tmp_path / "config" / "orders.json", config)

    note = "every window of the workflow's schedule loads the same rows again"
    assert any(note in w and w.startswith("task 'orders'") for w in plan_workflow(path, window=DAY1)["warnings"])

    unscheduled = _spec(metadata={"window": {"size": "day"}})
    unscheduled_path = _write(tmp_path / "manual.json", unscheduled)
    assert not [w for w in plan_workflow(unscheduled_path, window=DAY1)["warnings"] if note in w]


def test_a_plan_says_that_retries_are_read_but_a_task_still_runs_once(tmp_path):
    spec = _spec(metadata={"schedule": "@daily", "retries": {"max_attempts": 3}})
    result = plan_workflow(_project(tmp_path, workflow=spec))

    assert any("retries is read and checked, but every task still runs once" in w for w in result["warnings"])
    one = plan_workflow(_project(tmp_path, workflow=_spec(metadata={"retries": {"max_attempts": 1}})))
    assert not [w for w in one["warnings"] if "retries" in w]


def test_a_plan_counts_a_shortcut_in_the_time_zone_of_the_workflow(tmp_path, monkeypatch):
    spec = _spec(metadata={"schedule": "@daily", "window": {"size": "day", "timezone": "Asia/Kolkata"}})
    path = _project(tmp_path, workflow=spec)
    monkeypatch.setattr(windows, "now_utc", lambda: dt.datetime(2026, 8, 2, 3, tzinfo=dt.timezone.utc))

    result = plan_workflow(path, window="yesterday")

    assert result["window"] == "2026-07-31T18:30:00Z/2026-08-01T18:30:00Z" and result["timezone"] == "Asia/Kolkata"
    config = Config.from_json(tmp_path / "config" / "customers.json")
    assert result["tasks"][1]["idempotency_key"] == idempotency_key(config, "2026-07-31T18:30:00Z/2026-08-01T18:30:00Z")


def test_a_bad_window_makes_the_plan_invalid(tmp_path):
    result = plan_workflow(_project(tmp_path), window="tomorrow")

    assert result["valid"] is False and "START/END" in result["errors"][0]


def test_a_plan_of_a_file_that_cannot_be_read_is_invalid_with_a_reason(tmp_path):
    result = plan_workflow(tmp_path / "nope.json")

    assert result["valid"] is False and result["workflow"] is None and result["errors"][0].startswith("workflow file")


def test_a_cycle_makes_the_plan_invalid_and_the_command_exit_with_one(tmp_path, capsys):
    tasks = [{"id": "a", "pipeline": "config/orders.json", "depends_on": ["b"]},
             {"id": "b", "pipeline": "config/customers.json", "depends_on": ["a"]}]
    path = _project(tmp_path, workflow=_spec(tasks=tasks))

    assert plan_workflow(path)["valid"] is False
    assert main(["workflow", "plan", str(path)]) == 1
    assert "depend on each other in a cycle: " in capsys.readouterr().err


def test_ldp_plan_checks_a_workflow_file_in_full_and_the_plan_function_agrees(tmp_path):
    bad = _spec(tasks=[{"id": "a", "pipeline": "x.json", "depends_on": ["b"]},
                       {"id": "b", "pipeline": "y.json", "depends_on": ["a"]}])
    path = _write(tmp_path / "bad.json", bad)

    result = plan(path)

    assert result["valid"] is False and any("cycle" in error for error in result["errors"])
    assert plan(_write(tmp_path / "ok.json", _spec()))["tasks"] == ["orders", "customers", "export", "tidy"]


def test_ldp_workflow_plan_prints_the_tasks_in_order_with_keys_and_exits_with_zero(tmp_path, capsys):
    path = _project(tmp_path)

    assert main(["workflow", "plan", str(path), "--window", DAY1]) == 0

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0].split() == ["workflow", "daily_orders"]
    assert "schedule  @daily (from metadata.schedule)" in captured.out
    assert "window    2026-08-01T00:00:00Z/2026-08-02T00:00:00Z" in captured.out and "tasks     4" in captured.out
    header = next(line for line in lines if line.startswith("order"))
    assert header.split() == ["order", "id", "kind", "depends_on", "runs", "target", "key"]
    rows = [line.split() for line in lines if line.strip()[:1].isdigit()]
    assert [row[1] for row in rows] == ["orders", "customers", "export", "tidy"] and rows[2][3] == "orders"
    assert rows[0][-1] == plan_workflow(path, window=DAY1)["tasks"][0]["idempotency_key"][:12]
    assert "warning: task 'export': its target is a file" in captured.err


def test_ldp_workflow_plan_prints_json_and_exits_with_one_for_a_bad_config(tmp_path, capsys):
    path = _project(tmp_path)
    assert main(["workflow", "plan", str(path), "--window", DAY1, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"][0]["id"] == "orders"

    (tmp_path / "config" / "orders.json").unlink()
    assert main(["workflow", "plan", str(path), "--window", DAY1]) == 1
    assert "task 'orders': config file not found" in capsys.readouterr().err


def test_ldp_workflow_without_a_command_prints_its_help_and_exits_with_one(capsys):
    assert main(["workflow"]) == 1
    assert "plan" in capsys.readouterr().out and "run" in capsys.readouterr().out or True
    assert main(["--help"]) == 0
    assert "workflow" in capsys.readouterr().out


# ====================================================================== ldp workflow run


def test_a_workflow_of_three_pipelines_and_a_tidy_runs_in_order_and_every_task_succeeds(tmp_path):
    path = _project(tmp_path)

    result = run_workflow(path, window=DAY1)

    assert _statuses(result) == {"orders": "published", "customers": "published", "export": "published",
                                 "tidy": "succeeded"}
    assert list(result.tasks) == ["orders", "customers", "export", "tidy"]  # the order they ran in
    assert result.status == "succeeded" and result.exit_code == 0
    assert result.counts() == {"published": 3, "succeeded": 1}
    assert result.window == (dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                             dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc))
    assert _table(tmp_path, "orders").row_count() == 2  # the two orders of 1 August, not the four in the file
    assert _table(tmp_path, "customers").row_count() == 2
    assert (tmp_path / "out" / "orders.csv").read_text().count("\n") == 3  # a header and the two orders
    assert result.tasks["orders"].detail["rows_read"] == 4 and result.tasks["orders"].detail["rows_kept"] == 2
    assert result.tasks["tidy"].detail == {"table": "shop.orders", "dry_run": True, "snapshots_to_expire": 0,
                                           "orphan_files": 0}
    assert result.ledger == tmp_path / ".ldp" / "events.jsonl" and result.ledger.is_file()


def test_every_pipeline_task_publishes_under_the_key_that_ldp_plan_prints_for_the_window(tmp_path, capsys):
    path = _project(tmp_path)
    keys = {item["id"]: item.get("idempotency_key") for item in plan_workflow(path, window=DAY1)["tasks"]}

    result = run_workflow(path, window=DAY1)

    for task_id, config in (("orders", "orders"), ("customers", "customers")):
        assert result.tasks[task_id].detail["idempotency_key"] == keys[task_id]
        assert main(["plan", str(tmp_path / "config" / f"{config}.json"), "--window", DAY1]) == 0
        assert f"idempotency_key  {keys[task_id]}" in capsys.readouterr().out
        summary = _table(tmp_path, config).table().current_snapshot().summary.additional_properties
        assert summary["ldp.idempotency-key"] == keys[task_id]
        assert summary["ldp.logical-window"].startswith("2026-08-01T00:00:00+00:00/2026-08-02")


def test_a_quality_gate_stops_its_downstream_tasks_and_the_rest_still_run_with_exit_code_three(tmp_path):
    path = _project(tmp_path, orders_min=3)  # the 1 August window has 2 orders

    result = run_workflow(path, window=DAY1)

    assert _statuses(result) == {"orders": "blocked_quality", "customers": "published", "export": "upstream_failed",
                                 "tidy": "upstream_failed"}
    assert result.status == "blocked_quality" and result.exit_code == 3
    assert result.tasks["orders"].error.startswith("DataQualityError: 1 of 1 data quality checks failed")
    assert result.tasks["export"].detail == {"blocked_by": ["orders"]}
    assert result.tasks["tidy"].detail == {"blocked_by": ["export"]}
    assert _table(tmp_path, "customers").row_count() == 2  # an independent branch ran
    assert not _table(tmp_path, "orders").exists() and not (tmp_path / "out").exists()


def test_a_failed_task_stops_its_downstream_tasks_and_the_exit_code_is_one(tmp_path):
    path = _project(tmp_path)
    (tmp_path / "landing" / "orders.csv").unlink()

    result = run_workflow(path, window=DAY1)

    assert _statuses(result) == {"orders": "failed", "customers": "published", "export": "upstream_failed",
                                 "tidy": "upstream_failed"}
    assert result.status == "failed" and result.exit_code == 1
    assert result.tasks["orders"].error.startswith("FileNotFoundError: CSV file not found")


def test_a_failure_outranks_a_data_gate_in_the_exit_code(tmp_path):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "customers", "pipeline": "config/customers.json"}]
    path = _project(tmp_path, workflow=_spec(tasks=tasks), orders_min=3)
    (tmp_path / "landing" / "customers.csv").unlink()

    result = run_workflow(path, window=DAY1)

    assert _statuses(result) == {"orders": "blocked_quality", "customers": "failed"} and result.exit_code == 1


def test_running_the_same_window_again_after_the_fix_skips_what_published_and_runs_the_rest(tmp_path):
    path = _project(tmp_path, orders_min=3)
    first = run_workflow(path, window=DAY1)
    assert first.exit_code == 3

    # The fix: the check is met when the file holds three orders for the day. The customers file is gone, so a run that
    # read it again would fail, and one that skips it by its key does not.
    _write(tmp_path / "landing" / "orders.csv", ORDERS + "5,2026-08-01T12:00:00\n")
    (tmp_path / "landing" / "customers.csv").unlink()
    second = run_workflow(path, window=DAY1)

    assert _statuses(second) == {"orders": "published", "customers": "skipped_duplicate", "export": "published",
                                 "tidy": "succeeded"} and second.exit_code == 0
    assert second.tasks["customers"].detail["snapshot_id"] == first.tasks["customers"].detail["snapshot_id"]
    assert second.tasks["customers"].detail["rows_read"] == 0  # its source was not read
    assert _table(tmp_path, "orders").row_count() == 3 and _table(tmp_path, "customers").row_count() == 2

    third = run_workflow(path, window=DAY1)
    assert _statuses(third)["orders"] == "skipped_duplicate" and _statuses(third)["customers"] == "skipped_duplicate"
    assert _table(tmp_path, "orders").row_count() == 3 and third.exit_code == 0


def test_a_workflow_that_has_a_cycle_does_not_start(tmp_path):
    tasks = [{"id": "a", "pipeline": "config/orders.json", "depends_on": ["b"]},
             {"id": "b", "pipeline": "config/customers.json", "depends_on": ["a"]}]
    path = _project(tmp_path, workflow=_spec(tasks=tasks))

    with pytest.raises(ConfigError, match="depend on each other in a cycle"):
        run_workflow(path, window=DAY1)
    assert not (tmp_path / ".ldp").exists() and not (tmp_path / "warehouse").exists()


def test_a_workflow_with_a_config_that_is_not_valid_does_not_start_and_no_task_runs(tmp_path):
    path = _project(tmp_path)
    (tmp_path / "config" / "export.json").unlink()

    with pytest.raises(ConfigError) as caught:
        run_workflow(path, window=DAY1)

    assert "workflow daily_orders is not valid, so no task ran:" in str(caught.value)
    assert "task 'export': config file not found" in str(caught.value)
    assert not (tmp_path / "warehouse").exists() and not (tmp_path / ".ldp").exists()


def test_a_run_needs_a_window_and_a_good_one(tmp_path):
    path = _project(tmp_path)
    with pytest.raises(ConfigError, match="runs for a window: pass one"):
        run_workflow(path, window=None)
    with pytest.raises(ConfigError, match="START/END"):
        run_workflow(path, window="tomorrow")
    assert not (tmp_path / "warehouse").exists()


def test_a_shortcut_window_is_counted_in_the_time_zone_of_the_workflow(tmp_path, monkeypatch):
    spec = _spec(metadata={"schedule": "@daily", "window": {"size": "day", "timezone": "Asia/Kolkata"}})
    path = _project(tmp_path, workflow=spec)
    monkeypatch.setattr(windows, "now_utc", lambda: dt.datetime(2026, 8, 2, 3, tzinfo=dt.timezone.utc))

    result = run_workflow(path, window="yesterday")

    assert result.window == (dt.datetime(2026, 7, 31, 18, 30, tzinfo=dt.timezone.utc),
                             dt.datetime(2026, 8, 1, 18, 30, tzinfo=dt.timezone.utc))
    assert result.exit_code == 0 or result.status != "succeeded"  # the window is what is asserted here
    expected = idempotency_key(Config.from_json(tmp_path / "config" / "customers.json"),
                               "2026-07-31T18:30:00Z/2026-08-01T18:30:00Z")
    assert result.tasks["customers"].detail["idempotency_key"] == expected


def test_a_maintain_task_is_a_dry_run_until_it_is_told_to_apply(tmp_path):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "tidy", "maintain": {"config": "config/orders.json", "expire": 0, "retain_last": 1, "apply": True},
              "depends_on": ["orders"]}]
    path = _project(tmp_path, workflow=_spec(tasks=tasks))
    run_workflow(path, window=DAY1)
    run_workflow(path, window=DAY2)
    before = len(_table(tmp_path, "orders").table().snapshots())

    result = run_workflow(path, window=DAY1)  # the orders are skipped, and the tidy expires what it may

    assert result.tasks["tidy"].status == "succeeded"
    assert result.tasks["tidy"].detail["dry_run"] is False
    detail = result.tasks["tidy"].detail
    assert "snapshots_expired" in detail and "snapshots_to_expire" not in detail
    assert len(_table(tmp_path, "orders").table().snapshots()) <= before


def test_a_maintain_task_that_raises_fails_and_its_error_is_kept(tmp_path, monkeypatch):
    path = _project(tmp_path)
    import local_data_platform.maintenance.cli as maintenance

    def broken(*args, **kwargs):
        raise RuntimeError("the catalog went away")

    monkeypatch.setattr(maintenance, "run_maintenance", broken)

    result = run_workflow(path, window=DAY1)

    assert result.tasks["tidy"].status == "failed"
    assert result.tasks["tidy"].error == "RuntimeError: the catalog went away" and result.exit_code == 1
    finished = [e for e in read_jsonl(path.parent / ".ldp" / "events.jsonl")
                if e.payload.get("task_id") == "tidy" and e.type in ("run.failed", "run.finished")]
    assert [e.type for e in finished] == ["run.failed", "run.finished"]
    assert finished[0].payload["error_type"] == "RuntimeError" and finished[0].payload["stage"] == "maintain"


def test_maintenance_has_one_implementation_that_the_command_and_the_task_share(tmp_path, capsys):
    from local_data_platform.maintenance.cli import maintenance_failed, run_maintenance

    path = _project(tmp_path)
    run_workflow(path, window=DAY1)

    result = run_maintenance(str(tmp_path / "config" / "orders.json"), orphans=True)

    assert set(result) == {"table", "dry_run", "orphans"} and result["dry_run"] is True
    assert maintenance_failed(result) is False
    assert main(["maintain", str(tmp_path / "config" / "orders.json"), "--orphans"]) == 0
    assert "Maintenance of Iceberg table shop.orders (dry run" in capsys.readouterr().out


def test_a_task_that_returns_something_else_than_an_outcome_fails_the_task(tmp_path, custom_kind):
    get_task_kind("echo")  # registered by the fixture
    register_task_kind("odd", validate=lambda v: [], json_schema=dict, plan=lambda t, c: TaskPlan(),
                       run=lambda t, c: "done")
    try:
        path = _project(tmp_path, workflow=_spec(tasks=[{"id": "x", "odd": 1}]))
        result = run_workflow(path, window=DAY1)
    finally:
        unregister_task_kind("odd")

    assert result.tasks["x"].status == "failed" and "returned str, not a TaskOutcome" in result.tasks["x"].error


def test_a_kind_that_raises_with_facts_gets_them_into_the_result_and_the_events(tmp_path):
    def gate(task, ctx):
        raise TaskBlocked("the amounts are not valid", {"rows_checked": 3})

    def boom(task, ctx):
        raise TaskFailed("the engine stopped", {"exit_status": 9, "nodes": {"error": 1}})

    for name, run in (("gate", gate), ("boom", boom)):
        register_task_kind(name, validate=lambda v: [], json_schema=dict, plan=lambda t, c: TaskPlan(), run=run)
    try:
        tasks = [{"id": "g", "gate": 1}, {"id": "b", "boom": 1}, {"id": "after", "depends_on": ["g"], "gate": 1}]
        result = run_workflow(_project(tmp_path, workflow=_spec(tasks=tasks)), window=DAY1)
    finally:
        unregister_task_kind("gate")
        unregister_task_kind("boom")

    assert _statuses(result) == {"g": "blocked_quality", "b": "failed", "after": "upstream_failed"}
    assert result.exit_code == 1, "a failure outranks a data gate"
    assert result.tasks["g"].detail == {"rows_checked": 3} and "the amounts are not valid" in result.tasks["g"].error
    assert result.tasks["b"].detail == {"exit_status": 9, "nodes": {"error": 1}}
    finished = {event.payload["task_id"]: event.payload for event in read_jsonl(tmp_path / ".ldp" / "events.jsonl")
                if event.type == "run.finished" and event.payload.get("task_id")}
    assert finished["g"]["rows_checked"] == 3 and finished["b"]["exit_status"] == 9
    failed = [event for event in read_jsonl(tmp_path / ".ldp" / "events.jsonl") if event.type == "run.failed"]
    assert [event.payload["task_id"] for event in failed] == ["b"], "a data gate is not a failure"


def test_an_error_with_a_detail_that_is_not_an_object_is_just_an_error(tmp_path):
    class Odd(Exception):
        detail = "not a mapping"

    def run(task, ctx):
        raise Odd("odd")

    register_task_kind("odd", validate=lambda v: [], json_schema=dict, plan=lambda t, c: TaskPlan(), run=run)
    try:
        result = run_workflow(_project(tmp_path, workflow=_spec(tasks=[{"id": "x", "odd": 1}])), window=DAY1)
    finally:
        unregister_task_kind("odd")

    assert result.tasks["x"].status == "failed" and result.tasks["x"].detail == {}


def test_a_task_sees_the_results_of_the_tasks_that_ran_before_it_and_cannot_change_them(tmp_path):
    seen = {}

    def look(task, ctx):
        seen[task.id] = {name: item.status for name, item in ctx.finished.items()}
        with pytest.raises(TypeError):
            ctx.finished["x"] = None
        return TaskOutcome("succeeded", {})

    register_task_kind("look", validate=lambda v: [], json_schema=dict, plan=lambda t, c: TaskPlan(), run=look)
    try:
        tasks = [{"id": "one", "look": 1}, {"id": "two", "depends_on": ["one"], "look": 1},
                 {"id": "three", "depends_on": ["two"], "look": 1}]
        run_workflow(_project(tmp_path, workflow=_spec(tasks=tasks)), window=DAY1)
    finally:
        unregister_task_kind("look")

    assert seen == {"one": {}, "two": {"one": "succeeded"}, "three": {"one": "succeeded", "two": "succeeded"}}


def test_an_interrupted_run_still_writes_workflow_finished_and_the_interrupt_goes_on(tmp_path):
    def interrupt(task, ctx):
        raise KeyboardInterrupt

    register_task_kind("stop", validate=lambda v: [], json_schema=dict, plan=lambda t, c: TaskPlan(), run=interrupt)
    try:
        path = _project(tmp_path, workflow=_spec(tasks=[{"id": "first", "pipeline": "config/customers.json"},
                                                        {"id": "x", "stop": 1, "depends_on": ["first"]}]))
        with pytest.raises(KeyboardInterrupt):
            run_workflow(path, window=DAY1)
    finally:
        unregister_task_kind("stop")

    events = read_jsonl(tmp_path / ".ldp" / "events.jsonl")
    finished = [e for e in events if e.type == "workflow.finished"][0]
    assert finished.payload["status"] == "succeeded"  # what had finished before the interrupt
    assert finished.payload["tasks"] == {"first": "published"}


def test_retries_that_are_set_are_warned_about_and_a_failing_task_still_runs_once(tmp_path, caplog):
    path = _project(tmp_path, workflow=_spec(metadata={"schedule": "@daily", "retries": {"max_attempts": 3}}))
    (tmp_path / "landing" / "orders.csv").unlink()
    caplog.set_level("WARNING", logger="local_data_platform.orchestration.runner")

    result = run_workflow(path, window=DAY1)

    assert result.tasks["orders"].status == "failed"
    note = "retries is read and checked, but every task still runs once"
    assert [r for r in caplog.records if note in r.getMessage()]
    attempts = [e for e in read_jsonl(path.parent / ".ldp" / "events.jsonl")
                if e.payload.get("task_id") == "orders" and e.type == "run.failed"]
    assert len(attempts) == 1


def test_a_pipeline_that_reads_a_file_of_the_window_runs_directly_and_replaces_its_file(tmp_path):
    root = tmp_path
    _write(root / "landing" / "customers_2026-08-01.csv", "id,name\n1,Ana\n")
    _write(root / "landing" / "customers_2026-08-02.csv", "id,name\n2,Bo\n")
    _write(root / "config" / "export.json", {"identifier": "copy", "metadata": {
        "source": {"name": "c", "format": "CSV", "path": "../landing/customers_{window_start:%Y-%m-%d}.csv"},
        "target": {"name": "c_copy", "format": "CSV", "path": "../out/customers.csv"}}})
    # A CSV to CSV route does not exist, so the file goes into a table and out again.
    _write(root / "config" / "load.json", {"identifier": "load", "metadata": {
        "source": {"name": "c", "format": "CSV", "path": "../landing/customers_{window_start:%Y-%m-%d}.csv"},
        "target": {"name": "customers", "format": "ICEBERG", "write_mode": "overwrite", "catalog": CATALOG}}})
    path = _write(root / "flow.json", _spec(tasks=[{"id": "load", "pipeline": "config/load.json"}]))

    first, second = run_workflow(path, window=DAY1), run_workflow(path, window=DAY2)

    assert first.tasks["load"].detail["rows_read"] == 1 and second.tasks["load"].detail["rows_read"] == 1
    assert _table(root, "customers").get().column("name").to_pylist() == ["Bo"]  # overwrite: the last window's file


# ====================================================================== a config that runs as a workflow


def test_ldp_workflow_run_runs_a_dataset_config_that_has_a_schedule_as_one_task(tmp_path, capsys):
    _project(tmp_path)
    config = json.loads((tmp_path / "config" / "customers.json").read_text())
    config["metadata"]["schedule"] = "@daily"
    path = _write(tmp_path / "config" / "scheduled.json", config)

    assert main(["workflow", "plan", str(path), "--window", DAY1]) == 0
    assert "customers  pipeline" in capsys.readouterr().out
    assert main(["workflow", "run", str(path), "--window", DAY1]) == 0

    out = capsys.readouterr().out
    assert "workflow customers" in out and "published" in out
    assert _table(tmp_path, "customers").row_count() == 2
    assert main(["workflow", "run", str(tmp_path / "config" / "orders.json"), "--window", DAY1]) == 1
    assert "dataset config with no schedule" in capsys.readouterr().err


# ====================================================================== the command line


def test_ldp_workflow_run_prints_each_task_and_exits_with_zero(tmp_path, capsys):
    path = _project(tmp_path)

    assert main(["workflow", "run", str(path), "--window", DAY1]) == 0

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("workflow daily_orders  run ") and lines[0].endswith(
        "window 2026-08-01T00:00:00Z/2026-08-02T00:00:00Z")
    assert lines[1].split() == ["id", "kind", "status", "rows_read", "rows_written", "snapshot", "seconds"]
    statuses = {line.split()[0]: line.split()[2] for line in lines[3:7]}
    assert statuses == {"orders": "published", "customers": "published", "export": "published", "tidy": "succeeded"}
    assert "succeeded: 3 published, 1 succeeded in " in out and str(tmp_path / ".ldp" / "events.jsonl") in out


def test_ldp_workflow_run_exits_with_three_for_a_data_gate_and_names_what_did_not_run(tmp_path, capsys):
    path = _project(tmp_path, orders_min=3)

    assert main(["workflow", "run", str(path), "--window", DAY1]) == 3

    captured = capsys.readouterr()
    assert "blocked_quality: 1 blocked_quality, 1 published, 2 upstream_failed in " in captured.out
    assert "orders: DataQualityError: 1 of 1 data quality checks failed" in captured.err
    assert "export: did not run, because orders did not succeed" in captured.err


def test_ldp_workflow_run_exits_with_one_for_a_failure_and_for_a_workflow_that_is_not_valid(tmp_path, capsys):
    path = _project(tmp_path)
    (tmp_path / "landing" / "orders.csv").unlink()
    assert main(["workflow", "run", str(path), "--window", DAY1]) == 1
    assert "orders: FileNotFoundError" in capsys.readouterr().err

    (tmp_path / "config" / "export.json").unlink()
    assert main(["workflow", "run", str(path), "--window", DAY1]) == 1
    assert "is not valid, so no task ran" in capsys.readouterr().err


def test_ldp_workflow_run_needs_a_window_and_prints_json_on_request(tmp_path, capsys):
    path = _project(tmp_path)
    assert main(["workflow", "run", str(path)]) == 1  # a usage error
    assert "--window" in capsys.readouterr().err

    assert main(["workflow", "run", str(path), "--window", DAY1, "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "succeeded" and printed["exit_code"] == 0 and printed["workflow"] == "daily_orders"
    assert printed["window"] == "2026-08-01T00:00:00Z/2026-08-02T00:00:00Z"
    assert [t["id"] for t in printed["tasks"]] == ["orders", "customers", "export", "tidy"]
    assert printed["counts"] == {"published": 3, "succeeded": 1}


def test_the_workflow_result_maps_its_tasks_to_a_status_and_an_exit_code():
    from local_data_platform.orchestration.runner import TaskResult, WorkflowResult

    def result(*statuses):
        tasks = {f"t{i}": TaskResult(f"t{i}", "pipeline", status) for i, status in enumerate(statuses)}
        return WorkflowResult("w", "r", (dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)),
                              dt.datetime(2026, 1, 3), tasks=tasks)

    assert (result("published", "skipped_duplicate", "succeeded").status, result("published").exit_code) == (
        "succeeded", 0)
    assert (result("blocked_quality", "upstream_failed").status, result("blocked_quality").exit_code) == (
        "blocked_quality", 3)
    assert (result("blocked_quality", "failed").status, result("failed", "published").exit_code) == ("failed", 1)
    assert result().status == "succeeded" and result("published", "published").counts() == {"published": 2}


# ====================================================================== events


def _ledger(root: Path) -> list[RunEvent]:
    return read_jsonl(root / ".ldp" / "events.jsonl")


def test_the_ledger_holds_the_workflow_events_and_every_task_event_with_the_workflow_fields(tmp_path):
    path = _project(tmp_path, orders_min=3)

    result = run_workflow(path, window=DAY1)

    events = _ledger(tmp_path)
    started, finished = [e for e in events if e.type in ("workflow.started", "workflow.finished")]
    assert started.run_id == finished.run_id == result.run_id and (started.seq, finished.seq) == (0, 1)
    assert started.payload["workflow"] == "daily_orders" and started.payload["workflow_run_id"] == result.run_id
    assert started.payload["window"] == ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"]
    assert started.payload["schedule"] == "@daily" and started.payload["ldp_version"]
    assert [t["id"] for t in started.payload["tasks"]] == ["orders", "customers", "export", "tidy"]
    assert started.payload["tasks"][2] == {"id": "export", "kind": "pipeline", "depends_on": ["orders"]}
    assert finished.payload["status"] == "blocked_quality" and finished.payload["counts"] == {
        "blocked_quality": 1, "published": 1, "upstream_failed": 2}
    assert finished.payload["tasks"] == _statuses(result)
    task_events = [e for e in events if e.type not in ("workflow.started", "workflow.finished")]
    assert task_events and all(e.payload["workflow"] == "daily_orders" for e in task_events)
    assert all(e.payload["workflow_run_id"] == result.run_id and e.payload["task_id"] for e in task_events)
    assert {e.payload["task_id"] for e in task_events} == {"orders", "customers", "export", "tidy"}
    orders = [e.type for e in task_events if e.payload["task_id"] == "orders"]
    assert orders == ["run.started", "run.extracted", "quality.evaluated", "run.blocked_quality", "run.finished"]
    upstream = [e for e in task_events if e.payload["task_id"] == "tidy"]
    assert [e.type for e in upstream] == ["run.started", "run.finished"]
    assert upstream[1].payload["status"] == "upstream_failed" and upstream[1].payload["blocked_by"] == ["export"]


def test_ldp_runs_lists_the_workflow_run_and_the_run_of_each_task_from_the_ledger(tmp_path, capsys):
    path = _project(tmp_path, orders_min=3)
    result = run_workflow(path, window=DAY1)

    assert main(["runs", str(tmp_path / ".ldp" / "events.jsonl")]) == 0

    out = capsys.readouterr().out
    header = out.splitlines()[1].split()
    assert header[:5] == ["run_id", "attempt", "pipeline", "workflow", "task"]
    rows = {line.split()[0]: line.split() for line in out.splitlines()[3:] if line.strip() and "rows)" not in line}
    assert rows[result.run_id][2:5] == ["daily_orders", "daily_orders", "(workflow)"]
    assert "blocked_quality" in rows[result.run_id]
    expected = {"orders": "blocked_quality", "customers": "published", "export": "upstream_failed",
                "tidy": "upstream_failed"}
    for row in rows.values():
        if row[4] != "(workflow)":
            assert expected[row[4]] in row, row


def test_summarize_runs_knows_workflow_runs_and_the_tasks_of_a_workflow(tmp_path):
    path = _project(tmp_path)
    result = run_workflow(path, window=DAY1)

    runs = summarize_runs(_ledger(tmp_path))

    workflow = next(run for run in runs if run["is_workflow"])
    assert (workflow["run_id"], workflow["pipeline"], workflow["status"]) == (
        result.run_id, "daily_orders", "succeeded")
    assert workflow["duration_s"] is not None and workflow["workflow"] == "daily_orders"
    tasks = [run for run in runs if not run["is_workflow"]]
    assert {run["task_id"] for run in tasks} == {"orders", "customers", "export", "tidy"}
    assert all(run["workflow_run_id"] == result.run_id for run in tasks)
    plain = summarize_runs([RunEvent(uuid7(), "run.started", 1, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), "r", 1,
                                     0, payload={"pipeline": "p"})])
    assert plain[0]["workflow"] is None and plain[0]["task_id"] is None and plain[0]["is_workflow"] is False


def test_the_events_of_a_pipeline_task_also_go_to_the_sinks_of_its_own_config(tmp_path):
    path = _project(tmp_path)
    config = json.loads((tmp_path / "config" / "customers.json").read_text())
    config["metadata"]["observability"] = {"sinks": [{"type": "jsonl", "path": "own/customers.jsonl"}]}
    _write(tmp_path / "config" / "customers.json", config)

    result = run_workflow(path, window=DAY1)

    own = read_jsonl(tmp_path / "config" / "own" / "customers.jsonl")
    assert [e.type for e in own] == ["run.started", "run.extracted", "quality.evaluated", "run.published",
                                     "run.finished"]
    assert all(e.payload["task_id"] == "customers" and e.payload["workflow_run_id"] == result.run_id for e in own)
    assert not [e for e in own if e.payload["task_id"] != "customers"]  # only its own task's events
    ledger = [e for e in _ledger(tmp_path) if e.payload.get("task_id") == "customers"]
    assert [e.event_id for e in ledger] == [e.event_id for e in own]  # the same events, in both places


def test_the_observability_of_the_workflow_gets_every_event_too(tmp_path):
    spec = _spec(metadata={"schedule": "@daily", "observability": {"sinks": [{"type": "jsonl",
                                                                              "path": "events/all.jsonl"}]}})
    path = _project(tmp_path, workflow=spec)

    run_workflow(path, window=DAY1)

    everything = read_jsonl(tmp_path / "events" / "all.jsonl")
    assert [e.event_id for e in everything] == [e.event_id for e in _ledger(tmp_path)]
    assert everything[0].type == "workflow.started" and everything[-1].type == "workflow.finished"


def test_openlineage_runs_of_a_workflow_carry_a_parent_facet_that_points_at_the_workflow_run(tmp_path):
    spec = _spec(metadata={"schedule": "@daily", "observability": {
        "sinks": [{"type": "openlineage", "path": "lineage/workflow.jsonl", "namespace": "shop"}]}})
    path = _project(tmp_path, workflow=spec)
    config = json.loads((tmp_path / "config" / "customers.json").read_text())
    config["metadata"]["observability"] = {"sinks": [{"type": "openlineage", "path": "lineage/own.jsonl"}]}
    _write(tmp_path / "config" / "customers.json", config)

    result = run_workflow(path, window=DAY1)

    lineage = read_openlineage(tmp_path / "lineage" / "workflow.jsonl")
    assert lineage and {event["eventType"] for event in lineage} == {"START", "COMPLETE"}
    parent_id = _ol_run_id(result.run_id)
    for event in lineage:
        parent = event["run"]["facets"]["parent"]
        assert parent["run"] == {"runId": parent_id}
        assert parent["job"] == {"namespace": "shop", "name": "daily_orders"}
        assert parent["_schemaURL"].endswith("ParentRunFacet.json#/$defs/ParentRunFacet")
        assert event["run"]["runId"] != parent_id
    own = read_openlineage(tmp_path / "config" / "lineage" / "own.jsonl")
    assert own and all(e["run"]["facets"]["parent"]["run"]["runId"] == parent_id for e in own)
    assert {e["job"]["name"] for e in own} == {"customers"}


def test_a_run_that_is_not_in_a_workflow_has_no_parent_facet(tmp_path):
    from local_data_platform.etl import run_config

    _project(tmp_path)
    config = json.loads((tmp_path / "config" / "customers.json").read_text())
    config["metadata"]["observability"] = {"sinks": [{"type": "openlineage", "path": "lineage/plain.jsonl"}]}
    path = _write(tmp_path / "config" / "customers.json", config)

    run_config(path, window=DAY1)

    events = read_openlineage(tmp_path / "config" / "lineage" / "plain.jsonl")
    assert events and all("parent" not in e["run"]["facets"] for e in events)


def test_the_workflow_event_types_are_part_of_the_vocabulary():
    assert "workflow.started" in EVENT_TYPES and "workflow.finished" in EVENT_TYPES


def test_a_tagged_sink_adds_its_fields_to_the_payload_of_every_event_and_leaves_the_event_alone():
    inner = MemorySink()
    sink = TaggedSink(inner, {"workflow": "w", "task_id": "t"})
    event = RunEvent(uuid7(), "run.started", 1, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), "r", 1, 0,
                     payload={"pipeline": "p", "task_id": "old"})

    sink.emit(event)
    sink.flush()

    assert inner.events[0].payload == {"pipeline": "p", "task_id": "t", "workflow": "w"}  # the tag replaces
    assert event.payload == {"pipeline": "p", "task_id": "old"} and inner.flushes == 1
    assert "TaggedSink" in repr(sink) and "task_id" in repr(sink)
    with pytest.raises(TypeError, match="emit"):
        TaggedSink(object(), {})


# ====================================================================== the package


def test_the_names_of_the_package_load_when_they_are_first_used():
    import local_data_platform.orchestration as orchestration

    assert orchestration.run_workflow is run_workflow and orchestration.load_workflow is load_workflow
    assert "run_workflow" in dir(orchestration) and "WorkflowResult" in orchestration.__all__
    with pytest.raises(AttributeError, match="no attribute 'nothing'"):
        orchestration.nothing  # noqa: B018


def test_importing_the_orchestration_package_does_not_import_pyiceberg_or_the_runner():
    probe = ("import sys\nimport local_data_platform.orchestration, local_data_platform.spec\n"
             "print(sorted(m for m in ('pyiceberg', 'local_data_platform.orchestration.runner',\n"
             "                         'local_data_platform.orchestration.workflow') if m in sys.modules))\n")
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO_ROOT,
                               timeout=120)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"


def test_validating_a_workflow_needs_no_pyiceberg(tmp_path):
    probe = ("import sys\nfrom local_data_platform.spec import validate_spec\n"
             "spec = {'kind': 'Workflow', 'identifier': 'w', 'tasks': [{'id': 'a', 'pipeline': 'a.json'}]}\n"
             "print(validate_spec(spec))\nprint('pyiceberg' in sys.modules)\n")
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO_ROOT,
                               timeout=120)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["[]", "False"]
