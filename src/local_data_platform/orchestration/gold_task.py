"""The ``gold`` task kind: build the gold tables of a folder for the window of a workflow run, and cut a release.

::

    {"id": "gold", "depends_on": ["dbt"], "gold": {"dir": "config"}}

The task does what ``ldp gold build`` does. It finds the gold configs of the folder, builds them in the order their
``inputs`` say, checks the ones that something else builds, and, when every table passed its gate, cuts a release. A
scheduled workflow can run ingest, ``ldp dbt build`` and the gold build as one run, and the release is the version of
the gold layer that the run produced.

Each gold config runs as a ``pipeline`` task runs a config, for the window of the run. A table of an Iceberg target is
written under the idempotency key of the window, so running the same window again finds the tables already published
and writes nothing, and a run that replaces the window, as ``ldp backfill --replace`` does, writes them again under the
replace key. A release is cut only when the tables are at snapshots that the current release does not already name, so
a window that is run again does not cut a second release. The events of each table carry the workflow, its run and the
task, as the events of a pipeline task do, and so does the ``release.cut`` event of each table in the release.

A gold task ends with one of these statuses, and the runner emits its ``run.started`` and ``run.finished`` events:

* ``succeeded``: every table was built or checked, and a release was cut or was already current.
* ``skipped_duplicate``: every table that the task builds found its window already published, and no new release was
  needed, so the task wrote nothing. A task that only checks tables, and finds them where the current release has them,
  is skipped too.
* ``blocked_quality``: a quality check or a contract held a table back. Nothing after it was built, no release was cut,
  and the last release stays current. The data has to change, so a scheduler must not retry, and the workflow exits
  with 3.
* ``failed``: a table failed in any other way, or the release could not be cut, which it is when the metrics of
  the folder do not fit the gold tables.

This module needs only the standard library when it is imported. The gold layer is found when a task is planned or run.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger
from local_data_platform.orchestration.tasks import (
    PlanContext,
    RunContext,
    Task,
    TaskBlocked,
    TaskFailed,
    TaskOutcome,
    TaskPlan,
    config_tables,
    plan_pipeline,
    register_task_kind,
    run_pipeline_config,
)

logger = get_logger(__name__)

GOLD_OPTIONS = ("dir",)
"""The keys of a gold task: the folder of configs."""


# ---------------------------------------------------------------------- validation


def validate(value: Any) -> list[tuple[str, str]]:
    """The problems of the value of a ``gold`` task, as ``(path, message)``."""
    if not isinstance(value, Mapping):
        return [("", "must be an object with a 'dir', the path of a folder of dataset configs")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(map(str, value)) - set(GOLD_OPTIONS))
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(GOLD_OPTIONS)}"))
    folder = value.get("dir")
    if not isinstance(folder, str) or not folder.strip():
        problems.append((".dir", "is required: the path of a folder of dataset configs"))
    return problems


def json_schema() -> dict[str, Any]:
    """The JSON Schema of the value of a ``gold`` task."""
    return {
        "type": "object", "required": ["dir"], "additionalProperties": False,
        "description": "Build the gold tables of a folder of dataset configs for the window of the workflow run, as "
                       "`ldp gold build` does, and cut a release when every table passed its gate.",
        "properties": {
            "dir": {"type": "string", "minLength": 1,
                    "description": "The folder of dataset configs, relative to the workflow file. Every config whose "
                                   "metadata.docs.layer is gold is built, or checked when nothing in its source says "
                                   "how to build it. The gold tables have to be in one namespace of one warehouse."},
        },
    }


# ---------------------------------------------------------------------- plan


def plan(task: Task, ctx: PlanContext) -> TaskPlan:
    """Check the gold configs of a ``gold`` task and say what a run would build."""
    from local_data_platform.gold import release as releases
    from local_data_platform.gold.build import find_gold, order_gold

    options = task.value
    outcome = TaskPlan(details={"dir": options["dir"], "target": "gold build and release"})
    try:
        found = order_gold(find_gold(ctx.resolve(options["dir"])))
    except ConfigError as exc:
        outcome.errors.append(str(exc))
        return outcome
    if not found:
        outcome.notes.append(f"{options['dir']} has no gold config yet, so the task has nothing to build until a task "
                             "before it writes one, such as a dbt project that publishes a model to a gold namespace")
        return outcome
    try:
        scope = releases.release_scope(found)
        outcome.details["target"] = f"gold build, release in {scope.namespace}"
    except releases.ReleaseError as exc:
        outcome.errors.append(str(exc))
    tables = []
    for gold in found:
        shown = f"{options['dir'].rstrip('/')}/{gold.path.name}"
        entry: dict[str, Any] = {"config": shown, "table": gold.table, "built": gold.built_here}
        if gold.built_here:
            planned = plan_pipeline(Task(task.id, "pipeline", shown), ctx)
            outcome.errors.extend(message if message.startswith(shown) else f"{shown}: {message}"
                                  for message in planned.errors)
            outcome.notes.extend(message if message.startswith(shown) else f"{shown}: {message}"
                                 for message in planned.notes)
            if planned.details.get("idempotency_key"):
                entry["idempotency_key"] = planned.details["idempotency_key"]
            for item in gold.inputs:  # what a built table reads is what its inputs write
                outcome.reads.update(config_tables(item)[1])
            outcome.writes.update(config_tables(gold.path)[1])
        else:
            outcome.notes.append(f"{shown} is built elsewhere, so it is only checked")
            outcome.reads.update(config_tables(gold.path)[1])
        tables.append(entry)
    outcome.details["tables"] = tables
    return outcome


# ---------------------------------------------------------------------- run


def _table_run_id(ctx: RunContext, gold: Any) -> str:
    """The run id of one gold config, the same for every attempt of the task, so a retry fences the attempt before."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ldp-gold:{ctx.task_run_id}:{gold.config.identifier}"))


def run(task: Task, ctx: RunContext) -> TaskOutcome:
    """Build the gold tables for the window of the run, and cut a release."""
    from local_data_platform.events import sink_from_config
    from local_data_platform.gold import release as releases
    from local_data_platform.gold.build import BLOCKED, BUILT, build_gold, with_events
    from local_data_platform.spec import format_instant

    folder = ctx.resolve(task.value["dir"])

    def sink_of(gold: Any) -> Any:
        return ctx.task_sink(task, sink_from_config(with_events(gold.config)))

    def run_one(gold: Any) -> Any:
        return run_pipeline_config(with_events(gold.config), ctx, sink_of(gold), run_id=_table_run_id(ctx, gold),
                                   attempt=ctx.attempt)

    context = releases.ReleaseContext(window=ctx.bounds, workflow={
        "identifier": ctx.workflow.identifier, "run_id": ctx.run_id, "task": task.id})
    result = build_gold(folder, run=run_one, release=context, sink_for=sink_of)

    outcome = result.release
    detail: dict[str, Any] = {
        "dir": task.value["dir"], "window": [format_instant(ctx.bounds[0]), format_instant(ctx.bounds[1])],
        "tables": [{"table": item.gold.table, "status": item.status, "rows": item.rows,
                    "snapshot_id": item.snapshot_id, "run_status": getattr(item.result, "status", None)}
                   for item in result.tables],
        "release": outcome.release.name if outcome is not None and outcome.release is not None else None,
        "release_cut": bool(outcome is not None and outcome.cut),
        "metrics_problems": list(result.metrics.problems) if result.metrics is not None else []}
    stopped = result.stopped_by
    if stopped is not None:
        first = (stopped.error or "").splitlines()[0] if stopped.error else ""
        message = f"{stopped.gold.config.identifier} {stopped.status}, so no release was cut"
        raise (TaskBlocked if stopped.status == BLOCKED else TaskFailed)(f"{message}: {first}" if first else message,
                                                                         detail)
    if outcome is not None and outcome.error:
        raise TaskFailed(f"the gold tables were built, and no release was cut: {outcome.error}", detail)
    published = any(item.status == BUILT and getattr(item.result, "status", None) != "skipped_duplicate"
                    for item in result.tables)
    if not (published or (outcome is not None and outcome.cut)):
        return TaskOutcome("skipped_duplicate", detail)
    return TaskOutcome("succeeded", detail)


register_task_kind(
    "gold", validate=validate, json_schema=json_schema, plan=plan, run=run,
    description="Build the gold tables of a folder for the window of the workflow run and cut a release, as "
                "ldp gold build does.")


__all__ = ["GOLD_OPTIONS", "json_schema", "plan", "run", "validate"]
