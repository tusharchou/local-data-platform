"""Workflow files: the pipelines and models of a flow, in the order they run, in one JSON file.

A workflow file is a spec of the kind ``Workflow``::

    {
      "apiVersion": "ldp/v1",
      "kind": "Workflow",
      "identifier": "daily_orders",
      "metadata": {"schedule": "5 2 * * *", "window": {"size": "day", "timezone": "Europe/Paris"}},
      "tasks": [
        {"id": "orders", "pipeline": "config/orders.json"},
        {"id": "customers", "pipeline": "config/customers.json"},
        {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]},
        {"id": "tidy", "maintain": {"config": "config/orders.json", "expire": 7}, "depends_on": ["export"]}
      ]
    }

``metadata`` may have ``schedule`` (a cron expression, or ``@hourly``, ``@daily``, ``@weekly`` or ``@monthly``),
``window`` (``"none"``, or the ``size`` of a window and the ``timezone`` its days are counted in), ``catchup`` (whether
``ldp schedule tick`` runs every window it missed, which needs a ``start``), ``start`` (the first window), ``retries``
(``max_attempts``, ``delay_s`` and ``backoff``, which :mod:`local_data_platform.orchestration.retries` describes) and
``observability`` (sinks for the events of the whole workflow run). Each task has a unique ``id``, an optional
``depends_on`` that lists the ids it waits for, an optional ``retries`` of its own, and exactly one task kind, which is
the key that holds what it does. The kinds come from the registry of
:mod:`local_data_platform.orchestration.tasks`. Paths are relative to the workflow file.

A dataset config that has a schedule, in ``metadata.schedule`` or in a ``when`` such as ``daily``, is a workflow of one
task: :func:`load_workflow` and ``ldp workflow`` take it wherever they take a workflow file.

This module registers nothing by itself. :mod:`local_data_platform.spec` hands the ``Workflow`` kind to
:func:`validate_workflow`, :func:`workflow_schema` and :func:`describe_workflow`, so ``validate_spec``, ``ldp plan`` and
``ldp schema --kind Workflow`` check a workflow file in full, and it needs only the standard library to do so.
"""

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.orchestration import retries, windows
from local_data_platform.orchestration import tasks as task_registry
from local_data_platform.orchestration.tasks import TASK_KEYS, PlanContext, Task, TaskPlan
from local_data_platform.paths import resolve_path
from local_data_platform.spec import (
    API_VERSION,
    WORKFLOW_KEYS,
    _check_secrets,
    _check_string,
    _Problems,
    kind_of,
    window_bounds,
)

METADATA_KEYS = ("schedule", "window", "catchup", "start", "retries", "observability")
"""The keys of the ``metadata`` of a workflow."""

RETRY_KEYS = retries.RETRY_KEYS
"""The keys of a ``retries`` object."""

LEDGER_PATH = Path(".ldp") / "events.jsonl"
"""Where the events of every run of a workflow go, next to the workflow file."""


# ---------------------------------------------------------------------- the workflow


@dataclass(frozen=True)
class Workflow:
    """A workflow, read from a file.

    Attributes:
        identifier: The workflow's id.
        spec: The workflow as a spec dict. For a dataset config that runs as a workflow, the spec of its one-task
            workflow.
        tasks: The tasks, in the order of the file.
        base_dir: The folder that the paths of its tasks are relative to: the folder of the file.
        path: The file, or ``None`` for a workflow built in memory.
        from_config: True when the file is a dataset config with a schedule.
    """

    identifier: str
    spec: Mapping[str, Any]
    tasks: tuple[Task, ...]
    base_dir: Path
    path: Path | None = None
    from_config: bool = False

    @property
    def metadata(self) -> Mapping[str, Any]:
        """The ``metadata`` of the workflow."""
        value = self.spec.get("metadata")
        return value if isinstance(value, Mapping) else {}

    @property
    def schedule(self) -> windows.Schedule | None:
        """The schedule: ``metadata.schedule``, or the one a ``when`` such as ``daily`` means, or ``None``."""
        return windows.effective_schedule(self.metadata, self.spec.get("when"))

    @property
    def timezone(self) -> Any:
        """The time zone the days of the workflow's windows are counted in, UTC by default."""
        return windows.timezone_of(self.metadata)

    @property
    def ledger_path(self) -> Path:
        """The file that every event of a run of this workflow is written to, next to the workflow file."""
        return self.base_dir / LEDGER_PATH

    def resolve(self, path: str | Path) -> Path:
        """The path of a file the workflow names, against the folder of the workflow file."""
        return resolve_path(path, self.base_dir)

    def task(self, task_id: str) -> Task:
        """The task with this id.

        Raises:
            KeyError: If there is none.
        """
        for item in self.tasks:
            if item.id == task_id:
                return item
        raise KeyError(task_id)

    def graph(self) -> dict[str, tuple[str, ...]]:
        """The tasks as ``{id: ids it depends on}``."""
        return {item.id: item.depends_on for item in self.tasks}

    def order(self) -> list[Task]:
        """The tasks in the order they run: every task after the tasks it depends on.

        Tasks that nothing orders run in the order of the file, so a plan and a run always agree.
        """
        by_id = {item.id: item for item in self.tasks}
        sorter = TopologicalSorter(self.graph())
        sorter.prepare()
        ordered: list[Task] = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready(), key=lambda task_id: by_id[task_id].index)
            ordered.extend(by_id[task_id] for task_id in ready)
            sorter.done(*ready)
        return ordered

    def ancestors(self, task_id: str) -> set[str]:
        """The ids of every task that ``task_id`` depends on, directly or through others."""
        graph, found, pending = self.graph(), set(), list(self.graph()[task_id])
        while pending:
            current = pending.pop()
            if current not in found:
                found.add(current)
                pending.extend(graph[current])
        return found

    def retries_of(self, task: Task) -> retries.RetryPolicy:
        """How a task is retried: the ``retries`` of the workflow, with each key that the task sets replaced.

        A workflow and a task that set nothing give one attempt, which is no retry.
        """
        return retries.RetryPolicy.from_settings(self.metadata.get("retries"), task.retries)


# ---------------------------------------------------------------------- validation


def _retries_problems(value: Any, where: str, problems: _Problems) -> None:
    for path, message in retries.retries_problems(value):
        problems.add(f"{where}{path}", message)


def _start_problem(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"must be an ISO-8601 date or time, got {value!r}"
    try:
        dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return f"must be an ISO-8601 date or time, got {value!r}"
    return None


def _check_metadata(problems: _Problems, metadata: Mapping[str, Any]) -> None:
    unknown = sorted(set(map(str, metadata)) - set(METADATA_KEYS))
    if unknown:
        problems.add("metadata", f"has unknown keys {unknown}; allowed: {list(METADATA_KEYS)}")
    if metadata.get("schedule") is not None:
        try:
            windows.parse_schedule(metadata["schedule"])
        except ConfigError as exc:
            problems.add("metadata.schedule", str(exc))
    for path, message in windows.window_problems(metadata.get("window")):
        problems.add(f"metadata.window{path}", message)
    declared = metadata.get("window")
    if isinstance(declared, Mapping) and "column" in declared:
        problems.add("metadata.window.column", "does not belong in a workflow, because each dataset config names the "
                                               "window column that selects its own rows")
    if "catchup" in metadata and not isinstance(metadata["catchup"], bool):
        problems.add("metadata.catchup", f"must be true or false, got {metadata['catchup']!r}")
    if "start" in metadata and (message := _start_problem(metadata["start"])):
        problems.add("metadata.start", message)
    if metadata.get("catchup") is True and "start" not in metadata:
        problems.add("metadata.catchup", "is true, and a catch-up needs metadata.start, the first window it runs")
    if "retries" in metadata:
        _retries_problems(metadata["retries"], "metadata.retries", problems)
    if "observability" in metadata:
        from local_data_platform.events import sink_specs

        try:
            sink_specs({"observability": metadata["observability"]})
        except ConfigError as exc:
            problems.add("", str(exc))


def _check_task(problems: _Problems, position: int, task: Mapping[str, Any], ids: list[str]) -> None:
    here = f"tasks[{position}]"
    kinds = task_registry.task_kinds()
    unknown = sorted(set(map(str, task)) - set(TASK_KEYS) - set(kinds))
    if unknown:
        problems.add(here, f"has unknown keys {unknown}; a task has an id, depends_on, retries and one task kind of "
                           f"{kinds}")
    present = [name for name in kinds if name in task]
    if len(present) != 1:
        problems.add(here, f"needs exactly one task kind of {kinds}, and has "
                           + (f"{len(present)}: {present}" if present else "none"))
    for name in present[:1]:
        for suffix, message in task_registry.get_task_kind(name).validate(task[name]):
            problems.add(f"{here}.{name}{suffix}", message)
    depends = task.get("depends_on", [])
    if not isinstance(depends, list):
        problems.add(f"{here}.depends_on", f"must be a list of task ids, got {depends!r}")
        return
    for place, dependency in enumerate(depends):
        label = f"{here}.depends_on[{place}]"
        if not isinstance(dependency, str):
            problems.add(label, f"must be a task id, got {dependency!r}")
        elif dependency == task.get("id"):
            problems.add(label, f"{dependency!r} is the task itself")
        elif dependency not in ids:
            problems.add(label, f"unknown task id {dependency!r}; the ids are {ids}")
        elif dependency in depends[:place]:
            problems.add(label, f"{dependency!r} is listed twice")
    if "retries" in task:
        _retries_problems(task["retries"], f"{here}.retries", problems)


def validate_workflow(spec: Mapping[str, Any]) -> list[ConfigError]:
    """The problems of a workflow spec, without reading any file.

    It checks the keys, the identifier, the ``metadata``, the ids of the tasks, that each task has exactly one
    registered kind and that the value of the kind is valid, that ``depends_on`` names known tasks, and that the tasks
    do not depend on each other in a cycle. What the paths of the tasks point at is checked by :func:`plan_workflow`.

    Args:
        spec: The workflow as a mapping.

    Returns:
        One :class:`~local_data_platform.exceptions.ConfigError` per problem, each message starting with the JSON
        path of the offending value. Empty when valid.
    """
    problems = _Problems()
    unknown = sorted(set(map(str, spec)) - set(WORKFLOW_KEYS))
    if unknown:
        problems.add("", f"unknown top-level keys {unknown}; allowed: {list(WORKFLOW_KEYS)}")
    if "apiVersion" in spec and spec["apiVersion"] != API_VERSION:
        problems.add("apiVersion", f"must be {API_VERSION!r}, got {spec['apiVersion']!r}")
    _check_string(problems, spec, "identifier", "", required=True)
    for key in ("who", "what", "where", "when", "how"):
        if key in spec and not isinstance(spec[key], str):
            problems.add(key, f"must be a string, got {type(spec[key]).__name__}")
    if "metadata" in spec:
        if isinstance(spec["metadata"], Mapping):
            _check_metadata(problems, spec["metadata"])
        else:
            problems.add("metadata", f"must be an object, got {type(spec['metadata']).__name__}")
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        problems.add("tasks", "is required and must be a list of at least one task")
    else:
        _check_tasks(problems, tasks)
    _check_secrets(problems, spec, "")
    return problems.errors


def _check_tasks(problems: _Problems, tasks: list[Any]) -> None:
    seen: list[str] = []
    for position, task in enumerate(tasks):
        here = f"tasks[{position}]"
        if not isinstance(task, Mapping):
            problems.add(here, "must be an object with an 'id'")
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            problems.add(f"{here}.id", "is required and must be a non-empty string")
        elif task_id in seen:
            problems.add(f"{here}.id", f"{task_id!r} is the id of an earlier task")
        else:
            seen.append(task_id)
    for position, task in enumerate(tasks):
        if isinstance(task, Mapping):
            _check_task(problems, position, task, seen)
    if problems.errors:
        return
    graph = {task["id"]: tuple(task.get("depends_on", [])) for task in tasks}
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        problems.add("tasks", f"depend on each other in a cycle: {' -> '.join(exc.args[1])}")


def workflow_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a workflow file, built from the registry of task kinds."""
    from local_data_platform.spec import json_schema as pipeline_schema

    defs = pipeline_schema()["$defs"]
    string = {"type": "string"}
    non_empty = {"type": "string", "minLength": 1}
    kinds = task_registry.task_kinds()
    task_properties: dict[str, Any] = {
        "id": {**non_empty, "description": "The task's id, unique in the workflow."},
        "depends_on": {"type": "array", "items": non_empty, "uniqueItems": True,
                       "description": "The ids of the tasks that must succeed before this one runs."},
        "retries": {"$ref": "#/$defs/retries"},
    }
    for name in kinds:
        task_properties[name] = task_registry.get_task_kind(name).json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local-data-platform.readthedocs.io/schemas/ldp-v1-workflow.json",
        "title": f"local-data-platform workflow ({API_VERSION})",
        "type": "object",
        "required": ["kind", "identifier", "tasks"],
        "additionalProperties": False,
        "properties": {
            "apiVersion": {"const": API_VERSION, "default": API_VERSION},
            "kind": {"const": "Workflow", "description": "A workflow file says so, because a file without a kind "
                                                         "is a Pipeline."},
            "identifier": {**non_empty, "description": "The workflow's id."},
            "who": {**string, "description": "Who owns the workflow."},
            "what": {**string, "description": "What the workflow does."},
            "where": {**string, "description": "Where its data comes from."},
            "when": {**string, "description": "How often it runs, e.g. 'daily'. Without metadata.schedule, hourly, "
                                              "daily, weekly or monthly means a schedule."},
            "how": {**string, "description": "How it runs, e.g. 'batch'."},
            "metadata": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schedule": {**non_empty, "examples": ["@daily", "5 2 * * *"],
                                 "description": "A 5-field cron expression, or @hourly, @daily, @weekly or @monthly."},
                    "window": {
                        "description": "'none', or the size of the windows of the schedule and the time zone their "
                                       "days are counted in (UTC by default).",
                        "oneOf": [{"const": "none"},
                                  {"type": "object", "required": ["size"], "additionalProperties": False,
                                   "properties": {"size": {"enum": list(windows.WINDOW_SIZES)},
                                                  "timezone": {**non_empty, "examples": ["UTC", "Europe/Paris"]}}}],
                    },
                    "catchup": {"type": "boolean", "default": False,
                                "description": "Whether the scheduler runs every window it missed since start, and not "
                                               "only the latest. It needs start."},
                    "start": {**non_empty, "description": "The first window, as an ISO-8601 date or time."},
                    "retries": {"$ref": "#/$defs/retries"},
                    "observability": {"$ref": "#/$defs/observability"},
                },
            },
            "tasks": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/task"},
                      "description": "The tasks, each with a unique id and exactly one task kind."},
        },
        "$defs": {
            "retries": retries.retries_schema(),
            "task": {"type": "object", "required": ["id"], "additionalProperties": False,
                     "properties": task_properties, "oneOf": [{"required": [name]} for name in kinds]},
            "observability": defs["observability"],
            "catalog": defs["catalog"],
        },
    }


def describe_workflow(spec: Mapping[str, Any]) -> dict[str, Any]:
    """What ``ldp plan`` reports about a valid workflow file: its task ids."""
    return {"tasks": [task["id"] for task in spec["tasks"]]}


# ---------------------------------------------------------------------- reading a workflow


def _task_of(position: int, data: Mapping[str, Any]) -> Task:
    kind = next(name for name in task_registry.task_kinds() if name in data)
    return Task(id=data["id"], kind=kind, value=data[kind], depends_on=tuple(data.get("depends_on", [])),
                retries=data.get("retries"), index=position)


def workflow_from_dict(spec: Mapping[str, Any], *, base_dir: str | Path, path: str | Path | None = None) -> Workflow:
    """Build a :class:`Workflow` from a workflow spec.

    Args:
        spec: The workflow as a mapping.
        base_dir: The folder its task paths are relative to.
        path: The file it came from, if any.

    Raises:
        ConfigError: If the spec is not valid, with every problem in the message.
    """
    problems = validate_workflow(spec)
    if problems:
        raise ConfigError(_describe_problems(path, problems))
    return Workflow(identifier=spec["identifier"], spec=spec,
                    tasks=tuple(_task_of(position, task) for position, task in enumerate(spec["tasks"])),
                    base_dir=Path(base_dir), path=Path(path) if path is not None else None)


def _describe_problems(path: Any, problems: list[ConfigError]) -> str:
    label = Path(path).name if path is not None else "the workflow"
    lines = [f"{label} has {len(problems)} problem{'s' if len(problems) != 1 else ''}:"]
    return "\n".join(lines + [f"  - {problem}" for problem in problems])


def _workflow_of_config(data: Mapping[str, Any], path: Path, require_schedule: bool = True) -> Workflow:
    """A dataset config with a schedule, or a window if ``require_schedule`` is false, as a workflow of one task."""
    from local_data_platform.config import Config

    config = Config.from_dict(dict(data), base_dir=path.parent)
    schedule = config.schedule
    if schedule is None and (require_schedule or config.window is None):
        if require_schedule:
            raise ConfigError(f"{path.name} is a dataset config with no schedule, so it is not a workflow. Add "
                              "metadata.schedule to run it as one, or name it as a task in a workflow file")
        raise ConfigError(f"{path.name} is a dataset config with no metadata.window and no schedule, so it has no "
                          "windows. Set metadata.window.size to day, hour or 15m")
    metadata: dict[str, Any] = {"schedule": schedule.expression} if schedule is not None else {}
    if config.window is not None:
        metadata["window"] = {"size": config.window.size, "timezone": config.window.timezone}
    if config.metadata.get("retries") is not None:
        metadata["retries"] = config.metadata["retries"]
    spec = {"apiVersion": API_VERSION, "kind": "Workflow", "identifier": config.identifier, "metadata": metadata,
            "tasks": [{"id": config.identifier, "pipeline": path.name}]}
    return Workflow(identifier=config.identifier, spec=spec,
                    tasks=(Task(id=config.identifier, kind="pipeline", value=path.name),),
                    base_dir=path.parent, path=path, from_config=True)


def read_workflow(path: str | Path, *, require_schedule: bool = True) -> tuple[Workflow | None, list[str]]:
    """Read a workflow file, or a dataset config with a schedule, without raising.

    Args:
        path: The file.
        require_schedule: Whether a dataset config needs a schedule to be a workflow. ``ldp backfill`` passes false,
            because it needs the windows of a config, and a config with a ``metadata.window`` has them without a
            schedule.

    Returns:
        ``(workflow, problems)``. ``workflow`` is ``None`` when the file cannot be used, and ``problems`` says why.
    """
    resolved = resolve_path(path)
    if not resolved.is_file():
        return None, [f"workflow file not found: {resolved}"]
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, [f"{resolved} is not valid JSON: {exc}"]
    if not isinstance(data, Mapping):
        return None, [f"{resolved.name} must be a JSON object, got {type(data).__name__}"]
    try:
        kind = kind_of(data)
    except ConfigError as exc:
        return None, [f"kind: {exc}"]
    try:
        if kind == "Workflow":
            return workflow_from_dict(data, base_dir=resolved.parent, path=resolved), []
        if kind == "Pipeline":
            return _workflow_of_config(data, resolved, require_schedule), []
    except ConfigError as exc:
        return None, str(exc).splitlines()
    return None, [f"{resolved.name} is a {kind}, not a workflow"]


def load_workflow(path: str | Path, *, require_schedule: bool = True) -> Workflow:
    """Read a workflow file, or a dataset config with a schedule, which is a workflow of one task.

    Args:
        path: The file.
        require_schedule: Whether a dataset config needs a schedule to be a workflow, see :func:`read_workflow`.

    Raises:
        ConfigError: If the file is missing or not valid, or is a config with no schedule.
    """
    workflow, problems = read_workflow(path, require_schedule=require_schedule)
    if workflow is None:
        raise ConfigError("\n".join(problems))
    return workflow


# ---------------------------------------------------------------------- plan


def _connection_warnings(workflow: Workflow, plans: Mapping[str, TaskPlan]) -> list[str]:
    """Warn for each table that a task reads and another writes with no ``depends_on`` between them."""
    found = []
    for reader in workflow.order():
        for identity, name in plans[reader.id].reads.items():
            for writer in workflow.tasks:
                if writer.id == reader.id or identity not in plans[writer.id].writes:
                    continue
                if writer.id in workflow.ancestors(reader.id) or reader.id in workflow.ancestors(writer.id):
                    continue
                found.append(f"task {reader.id!r} reads the table {name}, which task {writer.id!r} writes, and no "
                             "depends_on connects them, so they may run in either order")
    return found


def plan_workflow(source: "str | Path | Workflow", *, window: Any = None, replace: bool = False) -> dict[str, Any]:
    """Check a workflow and every config it names, and say what a run would do, without running anything.

    Each task is planned by its kind. A pipeline task is planned with :func:`local_data_platform.spec.plan`, so its
    idempotency key for the window is the one ``ldp plan CONFIG --window`` prints. Nothing is written or created.

    Args:
        source: A workflow file, a dataset config with a schedule, or a :class:`Workflow`.
        window: A window, ``START/END``, a pair of datetimes or ``yesterday``, ``last-week`` or ``last-month``. A
            shortcut is counted in the time zone of the workflow. Without one, the plan has no keys.
        replace: Plan the run that replaces the window, as ``ldp backfill --replace`` does. A pipeline task that writes
            to Iceberg shows the replace key of its window, and is an error when its config has no
            ``metadata.window.column``.

    Returns:
        ``{"workflow", "file", "kind", "valid", "errors", "warnings", "schedule", "schedule_from", "timezone",
        "window", "tasks"}``. ``tasks`` lists the tasks in the order they run, each with its ``order``, ``id``,
        ``kind``, ``depends_on``, ``valid``, ``errors`` and the details its kind reports, such as ``config``,
        ``target`` and ``idempotency_key``, and its ``retries`` when it is tried again after a failure. ``errors``
        lists every problem, and ``warnings`` lists what is worth knowing, such as a table that a task reads and
        another writes with nothing that orders them.
    """
    result: dict[str, Any] = {"workflow": None, "file": None, "kind": "Workflow", "valid": False, "errors": [],
                              "warnings": [], "schedule": None, "schedule_from": None, "timezone": None,
                              "window": None, "tasks": []}
    if isinstance(source, Workflow):
        workflow, problems = source, []
    else:
        workflow, problems = read_workflow(source)
        result["file"] = str(resolve_path(source))
    if workflow is None:
        result["errors"] = problems
        return result
    result.update(workflow=workflow.identifier, file=str(workflow.path) if workflow.path else result["file"])
    schedule = workflow.schedule
    if schedule is not None:
        result["schedule"], result["schedule_from"] = schedule.expression, schedule.source
    result["timezone"] = getattr(workflow.timezone, "key", "UTC")
    errors: list[str] = []
    bounds = None
    try:
        bounds = window_bounds(workflow.spec, window)
    except ConfigError as exc:
        errors.append(str(exc))
    if bounds is not None:
        from local_data_platform.spec import format_instant

        result["window"] = f"{format_instant(bounds[0])}/{format_instant(bounds[1])}"
    context = PlanContext(workflow, bounds, replace)
    plans: dict[str, TaskPlan] = {}
    for number, task in enumerate(workflow.order(), start=1):
        try:
            plan = task_registry.get_task_kind(task.kind).plan(task, context)
        except Exception as exc:  # noqa: BLE001 - a kind that cannot plan its task makes the task invalid
            plan = TaskPlan(errors=[f"could not be planned: {type(exc).__name__}: {exc}"])
        plans[task.id] = plan
        entry = {"order": number, "id": task.id, "kind": task.kind, "depends_on": list(task.depends_on),
                 "valid": not plan.errors, "errors": list(plan.errors), **plan.details}
        if (policy := workflow.retries_of(task)).enabled:
            entry["retries"] = policy.to_dict()
        result["tasks"].append(entry)
        errors.extend(f"task {task.id!r}: {message}" for message in plan.errors)
        result["warnings"].extend(f"task {task.id!r}: {message}" for message in plan.notes)
    result["warnings"].extend(_connection_warnings(workflow, plans))
    result["errors"] = errors
    result["valid"] = not errors
    return result


__all__ = [
    "LEDGER_PATH",
    "METADATA_KEYS",
    "RETRY_KEYS",
    "Workflow",
    "describe_workflow",
    "load_workflow",
    "plan_workflow",
    "read_workflow",
    "validate_workflow",
    "workflow_from_dict",
    "workflow_schema",
]
