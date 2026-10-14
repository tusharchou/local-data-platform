"""Task kinds: what a task of a workflow can be, and how each kind is checked, planned and run.

A task has an ``id``, an optional ``depends_on``, and exactly one *kind*, which is the key that holds what the task
does::

    {"id": "orders", "pipeline": "config/orders.json"}
    {"id": "tidy", "depends_on": ["orders"], "maintain": {"config": "config/orders.json", "expire": 7, "apply": true}}

This milestone ships three kinds, ``pipeline`` (the path of a dataset config, relative to the workflow file),
``maintain`` (the path of a dataset config and the options of ``ldp maintain``) and ``dbt`` (a dbt project, run for the
window, which :mod:`local_data_platform.orchestration.dbt_task` describes). Each is a :class:`TaskKind` in a small
registry, and :func:`register_task_kind` adds more. A later milestone adds a ``gold`` task and a ``report`` task the
same way, and neither the runner nor the workflow file format changes.

A kind says how its value is checked (``validate`` and ``json_schema``), what ``ldp workflow plan`` shows and checks
about it (``plan``), and what it does when a workflow runs (``run``). ``run`` returns a :class:`TaskOutcome` and raises
when the task fails: :class:`TaskFailed` for an error, and :class:`TaskBlocked` when a data gate held the task back, and
both carry small JSON facts about the run that the events and the result of the task keep. A kind whose ``run`` is not
a pipeline run leaves its events to the runner, which emits ``run.started`` and ``run.finished`` for it. A pipeline run
emits its own events (``emits_events=True``).

This module needs only the standard library when it is imported, so a workflow can be validated without pyiceberg.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import DataQualityError, LDPError

TASK_KEYS = ("id", "depends_on", "retries")
"""The keys a task may have besides its one kind."""

TASK_OK = ("published", "skipped_duplicate", "succeeded")
"""The statuses of a task that did what it should."""

_KIND_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class TaskFailed(LDPError):
    """Raised by the ``run`` of a task kind when the task failed.

    Any other exception fails the task too. This one also carries ``detail``, small JSON facts about the failed run,
    which go into the ``run.finished`` event and the result of the task, so a failure says what was run and what went
    wrong, not only that it did.

    Args:
        message: What went wrong.
        detail: The facts, such as the command, its exit status and the models that failed. Never rows.
    """

    def __init__(self, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})


class TaskBlocked(DataQualityError):
    """Raised by the ``run`` of a task kind when a data gate held the task back.

    The data has to change, so the task ends ``blocked_quality``, the workflow exits with 3 and a scheduler must not
    retry it. ``detail`` is as for :class:`TaskFailed`.

    Args:
        message: What the gate found.
        detail: The facts about the run. Never rows.
    """

    def __init__(self, message: str, detail: Mapping[str, Any] | None = None, report: Any = None):
        super().__init__(message, report)
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class Task:
    """One task of a workflow.

    Attributes:
        id: The task's id, unique in the workflow.
        kind: The kind of the task, the key of the registry that its ``value`` sits under.
        value: What the kind's key holds: the path of a config for ``pipeline``, an options object for ``maintain``
            and ``dbt``.
        depends_on: The ids of the tasks that must succeed before this one runs.
        retries: The task's own ``retries`` setting, or ``None`` to use the workflow's.
        index: The position of the task in the file, which orders tasks that nothing else orders.
    """

    id: str
    kind: str
    value: Any
    depends_on: tuple[str, ...] = ()
    retries: Mapping[str, Any] | None = None
    index: int = 0


@dataclass
class TaskPlan:
    """What ``ldp workflow plan`` found out about one task.

    Attributes:
        errors: Why the task cannot run, one message each. Empty when it can.
        notes: Things worth knowing that do not stop it.
        details: Facts to show, such as the config, the target and the idempotency key.
        reads: The Iceberg tables the task reads, as ``{identity: "namespace.name"}``.
        writes: The Iceberg tables the task writes, in the same form.
    """

    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    reads: dict[str, str] = field(default_factory=dict)
    writes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskOutcome:
    """What a task did, when it did not fail.

    Attributes:
        status: One of :data:`TASK_OK`.
        detail: Small JSON facts about the run, such as row counts and a snapshot id. Never rows.
    """

    status: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanContext:
    """What a kind's ``plan`` is given besides the task.

    Attributes:
        workflow: The :class:`~local_data_platform.orchestration.workflow.Workflow`.
        bounds: The window as UTC ``(start, end)``, or ``None`` when the plan has none.
    """

    workflow: Any
    bounds: tuple[Any, Any] | None = None

    def resolve(self, path: str | Path) -> Path:
        """The path of a file the workflow names, against the folder of the workflow file."""
        return self.workflow.resolve(path)


@dataclass(frozen=True)
class RunContext:
    """What a kind's ``run`` is given besides the task.

    Attributes:
        workflow: The :class:`~local_data_platform.orchestration.workflow.Workflow`.
        bounds: The window of the run as UTC ``(start, end)``.
        run_id: The id of the workflow run.
        sink: Where the events of the workflow run go: the ledger next to the workflow file, and the sinks of the
            workflow's ``metadata.observability``.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` of the writes of the run.
        search_since_ms: The start of the key search of the publish loop of the run, in epoch milliseconds.
        finished: The result of every task that has finished in this run, by task id, in the order they finished. It is
            read-only, and it holds what ran before the task, so a task can tell whether anything upstream wrote data.
    """

    workflow: Any
    bounds: tuple[Any, Any]
    run_id: str
    sink: Any
    policy: Any = None
    search_since_ms: int | None = None
    finished: Mapping[str, Any] = field(default_factory=dict)

    def resolve(self, path: str | Path) -> Path:
        """The path of a file the workflow names, against the folder of the workflow file."""
        return self.workflow.resolve(path)

    def wrote_data(self) -> bool:
        """Whether a task that finished earlier in this run may have written data.

        A task that was skipped, or did not run, wrote nothing. A task that succeeded wrote data unless its kind says
        it changes none, as ``maintain`` does. A task that failed, or that a data gate blocked, may have written part of
        its data before it stopped, so it counts: skipping a task as already done is only safe when nothing it reads
        can have changed.
        """
        for result in self.finished.values():
            if result.status in ("skipped_duplicate", "upstream_failed"):
                continue
            if result.status == "succeeded" and not _KINDS[result.kind].changes_data:
                continue
            return True
        return False

    def tags(self, task: Task) -> dict[str, str]:
        """The fields that every event of a task's run carries: its workflow, the workflow run and the task."""
        return {"workflow": self.workflow.identifier, "workflow_run_id": self.run_id, "task_id": task.id}

    def task_sink(self, task: Task, *own: Any) -> Any:
        """A sink for the events of a task's run: its own sinks and the workflow's, with the tags of the task."""
        from local_data_platform.events import MultiSink, TaggedSink

        return TaggedSink(MultiSink([*own, self.sink]), self.tags(task))


@dataclass(frozen=True)
class TaskKind:
    """What the registry holds for one kind of task.

    Attributes:
        name: The kind, which is the key that holds the task's value.
        validate: Takes the value and returns its problems as ``(path, message)``, where ``path`` is ``""`` for the
            value itself or a suffix such as ``".expire"``.
        json_schema: Returns the JSON Schema of the value.
        plan: Reads what the task refers to and returns a :class:`TaskPlan`.
        run: Does the task and returns a :class:`TaskOutcome`, or raises.
        emits_events: Whether ``run`` emits the events of the task's run itself, as a pipeline does. When it does not,
            the runner emits ``run.started`` and ``run.finished`` for the task.
        description: One line for ``ldp workflow plan`` and the docs.
        changes_data: Whether a task of this kind that succeeds has changed data that a later task may read. Most do. A
            kind that only looks after tables, such as ``maintain``, says no, so it does not make a later task run
            again that would otherwise be skipped as already done.
    """

    name: str
    validate: Callable[[Any], list[tuple[str, str]]]
    json_schema: Callable[[], dict[str, Any]]
    plan: Callable[[Task, PlanContext], TaskPlan]
    run: Callable[[Task, RunContext], TaskOutcome]
    emits_events: bool = False
    description: str = ""
    changes_data: bool = True


_KINDS: dict[str, TaskKind] = {}


def register_task_kind(name: str, *, validate: Callable[[Any], list[tuple[str, str]]],
                       json_schema: Callable[[], dict[str, Any]], plan: Callable[[Task, PlanContext], TaskPlan],
                       run: Callable[[Task, RunContext], TaskOutcome], emits_events: bool = False,
                       description: str = "", changes_data: bool = True, replace: bool = False) -> TaskKind:
    """Add a kind of task to the registry, so a workflow can use it.

    Args:
        name: The kind, which a task writes as the key of its value. It is lower case letters, digits and ``_``, and
            starts with a letter. ``id``, ``depends_on`` and ``retries`` are the keys of a task and cannot be kinds.
        validate: Checks the value of a task of this kind and returns its problems as ``(path, message)``.
        json_schema: Returns the JSON Schema of the value.
        plan: Returns what ``ldp workflow plan`` should show and check about a task of this kind.
        run: Does the task. It returns a :class:`TaskOutcome` and raises when the task fails.
        emits_events: Whether ``run`` emits the run events of the task itself. A kind that does not has them emitted by
            the runner.
        description: One line that says what the kind is.
        changes_data: Whether a task of this kind that succeeds has changed data that a later task may read. See
            :class:`TaskKind`.
        replace: Replace a kind that is already registered.

    Returns:
        The registered kind.

    Raises:
        ValueError: If the name is not a valid kind name or is a key of a task, or the kind is registered and
            ``replace`` is false.
    """
    if not isinstance(name, str) or not _KIND_NAME.match(name) or name in TASK_KEYS:
        raise ValueError(f"a task kind is lower case letters, digits and _, starting with a letter, and not one of "
                         f"{list(TASK_KEYS)}, got {name!r}")
    if name in _KINDS and not replace:
        raise ValueError(f"the task kind {name!r} is already registered; pass replace=True to replace it")
    _KINDS[name] = TaskKind(name, validate, json_schema, plan, run, emits_events, description, changes_data)
    return _KINDS[name]


def unregister_task_kind(name: str) -> TaskKind:
    """Remove a kind from the registry, for tests and plugins that replace built-ins.

    Raises:
        KeyError: If no such kind is registered.
    """
    return _KINDS.pop(name)


def task_kinds() -> list[str]:
    """The registered kinds, in the order they were registered."""
    return list(_KINDS)


def get_task_kind(name: str) -> TaskKind:
    """The registered kind called ``name``.

    Raises:
        KeyError: If no such kind is registered.
    """
    return _KINDS[name]


# ---------------------------------------------------------------------- Iceberg tables of a config


def config_tables(path: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    """The Iceberg tables a dataset config reads and writes, as ``({identity: name}, {identity: name})``.

    The identity says which catalog the table is in, so two catalogs with a table of the same name are two tables. A
    config that cannot be read, or that has no Iceberg source or target, gives an empty dict for that side.
    """
    from local_data_platform.paths import resolve_path

    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = data.get("metadata")
    except (OSError, ValueError, AttributeError):
        return {}, {}
    found: dict[str, dict[str, str]] = {"source": {}, "target": {}}
    for section in found:
        block = metadata.get(section) if isinstance(metadata, Mapping) else None
        if not isinstance(block, Mapping) or str(block.get("format", "")).strip().upper() != "ICEBERG":
            continue
        catalog = block.get("catalog") if isinstance(block.get("catalog"), Mapping) else {}
        namespace, name = catalog.get("namespace") or catalog.get("identifier"), block.get("name")
        if not namespace or not name:
            continue
        where = catalog.get("warehouse_path")
        if where:
            where = str(resolve_path(where, path.parent))
        else:
            where = str(catalog.get("uri") or catalog.get("warehouse") or "")
        found[section][f"{where}::{namespace}.{name}"] = f"{namespace}.{name}"
    return found["source"], found["target"]


# ---------------------------------------------------------------------- the pipeline kind


def _validate_pipeline(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, str) or not value.strip():
        return [("", f"must be the path of a dataset config, got {value!r}")]
    return []


def _pipeline_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1,
            "description": "The path of a dataset config, relative to the workflow file. The task runs it for the "
                           "window of the workflow run, so its idempotency key is the one `ldp plan CONFIG --window` "
                           "prints."}


def _plan_pipeline(task: Task, ctx: PlanContext) -> TaskPlan:
    from local_data_platform import spec

    path = ctx.resolve(task.value)
    outcome = TaskPlan(details={"config": task.value})
    result = spec.plan(path, window=ctx.bounds)
    outcome.errors.extend(result["errors"])
    if result["valid"] and result["kind"] != spec.DEFAULT_KIND:
        outcome.errors.append(f"{task.value} is a {result['kind']}, not a dataset config")
    outcome.notes.extend(result["notes"])
    for name in ("identifier", "route", "target", "write_mode"):
        if result.get(name) is not None:
            outcome.details[name] = result[name]
    if result["catalog_type"] is not None:  # an Iceberg target: the run publishes under the key of the window
        outcome.details["publish"] = "staged"
        if result.get("idempotency_key") is not None:
            outcome.details["idempotency_key"] = result["idempotency_key"]
    elif result["valid"]:
        outcome.details["publish"] = "direct"
        outcome.notes.append("its target is a file, which every run replaces whole, so the task has no idempotency "
                             "key and a second run of the window writes the file again")
    outcome.reads, outcome.writes = config_tables(path)
    _warn_if_it_appends_the_same_rows_in_every_window(outcome, path, ctx)
    return outcome


def _warn_if_it_appends_the_same_rows_in_every_window(outcome: TaskPlan, path: Path, ctx: PlanContext) -> None:
    """Note a task of a scheduled workflow that appends and selects no rows of a window.

    A workflow that has a schedule runs every task for every window. A pipeline that appends to an Iceberg table and has
    neither a placeholder in its source path nor a window column loads the whole source again in each of them.
    """
    from local_data_platform.orchestration import windows

    if ctx.workflow.schedule is None or outcome.errors:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scheduled = {**data["metadata"], "schedule": ctx.workflow.schedule.expression}
        lacking = windows.lacks_window_selection(scheduled, None)
    except (OSError, ValueError, KeyError, TypeError):
        return
    if lacking:
        outcome.notes.append("it appends to its table, and nothing selects the rows of a window, so every window "
                             "of the workflow's schedule loads the same rows again; put {window_start:FORMAT} in "
                             "metadata.source.path or set metadata.window.column")


def _run_pipeline(task: Task, ctx: RunContext) -> TaskOutcome:
    from local_data_platform.etl import load_config, run_config
    from local_data_platform.events import sink_from_config
    from local_data_platform.pipeline.registry import create_pipeline

    config = load_config(ctx.resolve(task.value))
    sink = ctx.task_sink(task, sink_from_config(config))
    if str(config.target.get("format", "")).strip().upper() == "ICEBERG":
        result = run_config(config, window=ctx.bounds, sink=sink, policy=ctx.policy,
                            search_since_ms=ctx.search_since_ms)
    else:
        # A file target is replaced whole by every run, so it needs no key and cannot take one. The window still picks
        # the source path and the rows that the run reads.
        result = create_pipeline(config, window=ctx.bounds).run(sink=sink)
    detail = {"run_id": result.run_id, "idempotency_key": result.idempotency_key,
              "snapshot_id": result.published_snapshot_id, "rows_read": result.rows_read,
              "rows_written": result.rows_written}
    if result.rows_kept is not None:
        detail["rows_kept"] = result.rows_kept
    return TaskOutcome(result.status, detail)


# ---------------------------------------------------------------------- the maintain kind

MAINTAIN_OPTIONS = ("config", "expire", "orphans", "apply", "retain_last", "orphan_age_hours",
                    "keep_metadata_versions")
"""The keys of a maintain task: the config, and the options of ``ldp maintain`` written with ``_`` for ``-``."""


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_maintain(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, Mapping):
        return [("", "must be an object with a 'config', the path of a dataset config with an Iceberg table")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(map(str, value)) - set(MAINTAIN_OPTIONS))
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(MAINTAIN_OPTIONS)}"))
    config = value.get("config")
    if not isinstance(config, str) or not config.strip():
        problems.append((".config", "is required: the path of a dataset config with an Iceberg table"))
    for key in ("expire", "orphan_age_hours"):
        if key in value and not (_number(value[key]) and value[key] >= 0):
            problems.append((f".{key}", f"must be a number from 0, got {value[key]!r}"))
    if "retain_last" in value and not (_whole(value["retain_last"]) and value["retain_last"] >= 1):
        problems.append((".retain_last", f"must be a whole number from 1, got {value['retain_last']!r}"))
    if "keep_metadata_versions" in value and not (_whole(value["keep_metadata_versions"])
                                                  and value["keep_metadata_versions"] >= 0):
        problems.append((".keep_metadata_versions",
                         f"must be a whole number from 0, got {value['keep_metadata_versions']!r}"))
    for key in ("orphans", "apply"):
        if key in value and not isinstance(value[key], bool):
            problems.append((f".{key}", f"must be true or false, got {value[key]!r}"))
    return problems


def _maintain_schema() -> dict[str, Any]:
    return {
        "type": "object", "required": ["config"], "additionalProperties": False,
        "description": "Maintain the Iceberg table of a dataset config, as `ldp maintain` does. It is a dry run unless "
                       "`apply` is true.",
        "properties": {
            "config": {"type": "string", "minLength": 1,
                       "description": "The path of a dataset config with an Iceberg table, relative to the workflow "
                                      "file."},
            "expire": {"type": "number", "minimum": 0, "description": "Expire snapshots older than this many days."},
            "orphans": {"type": "boolean",
                        "description": "Find, and with `apply` delete, files that no metadata references."},
            "apply": {"type": "boolean", "default": False},
            "retain_last": {"type": "integer", "minimum": 1},
            "orphan_age_hours": {"type": "number", "minimum": 0},
            "keep_metadata_versions": {"type": "integer", "minimum": 0},
        },
    }


def _plan_maintain(task: Task, ctx: PlanContext) -> TaskPlan:
    from local_data_platform import spec

    options = task.value
    path = ctx.resolve(options["config"])
    outcome = TaskPlan(details={"config": options["config"], "dry_run": not options.get("apply", False)})
    result = spec.plan(path)
    outcome.errors.extend(result["errors"])
    if result["valid"] and result["kind"] != spec.DEFAULT_KIND:
        outcome.errors.append(f"{options['config']} is a {result['kind']}, not a dataset config")
    reads, writes = config_tables(path)
    table = writes or reads
    if result["valid"] and not table:
        outcome.errors.append(f"{options['config']} has no Iceberg table to maintain")
    else:
        outcome.details["table"] = next(iter(table.values()), None)
    return outcome


def _run_maintain(task: Task, ctx: RunContext) -> TaskOutcome:
    from local_data_platform.maintenance.cli import maintenance_failed, run_maintenance

    options = task.value
    result = run_maintenance(
        str(ctx.resolve(options["config"])), expire=options.get("expire"), orphans=options.get("orphans", False),
        apply=options.get("apply", False), retain_last=options.get("retain_last"),
        orphan_age_hours=options.get("orphan_age_hours"),
        keep_metadata_versions=options.get("keep_metadata_versions"))
    detail: dict[str, Any] = {"table": result["table"], "dry_run": result["dry_run"]}
    if "expire" in result:
        detail["snapshots_to_expire" if result["dry_run"] else "snapshots_expired"] = len(
            result["expire"]["expired_snapshot_ids"])
    if "orphans" in result:
        orphans = result["orphans"]
        detail["orphan_files" if result["dry_run"] else "orphan_files_deleted"] = len(
            orphans["orphans"] if result["dry_run"] else orphans["deleted"])
    if maintenance_failed(result):
        raise RuntimeError(f"{len(result['orphans']['failed'])} orphan files of {result['table']} could not be deleted")
    return TaskOutcome("succeeded", detail)


register_task_kind(
    "pipeline", validate=_validate_pipeline, json_schema=_pipeline_schema, plan=_plan_pipeline, run=_run_pipeline,
    emits_events=True, description="Run a dataset config for the window of the workflow run.")
register_task_kind(
    "maintain", validate=_validate_maintain, json_schema=_maintain_schema, plan=_plan_maintain, run=_run_maintain,
    changes_data=False,
    description="Expire snapshots and find orphan files of a config's Iceberg table, as ldp maintain does.")

# The dbt kind lives in its own module and registers itself when that is imported, which has to come last: the module
# imports from this one. Importing it first works too, because this import finds it half loaded and goes on.
from local_data_platform.orchestration import dbt_task  # noqa: E402,F401


__all__ = [
    "MAINTAIN_OPTIONS",
    "PlanContext",
    "RunContext",
    "TASK_KEYS",
    "TASK_OK",
    "Task",
    "TaskBlocked",
    "TaskFailed",
    "TaskKind",
    "TaskOutcome",
    "TaskPlan",
    "config_tables",
    "get_task_kind",
    "register_task_kind",
    "task_kinds",
    "unregister_task_kind",
]
