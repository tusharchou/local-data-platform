"""The ``dbt`` task kind: run a dbt project for the window of a workflow run.

::

    {"id": "gold", "depends_on": ["orders"], "dbt": {"project": "dbt", "select": "+daily_revenue"}}

The task does what ``ldp dbt build`` does, for the window of the run. It registers the Iceberg tables of the project as
sources, runs ``dbt build`` (or ``dbt test`` with ``"command": "test"``), and writes the models marked ``ldp_publish``
back to Iceberg. dbt gets the window as the variables ``window_start`` and ``window_end``, which are UTC instants such
as ``2026-08-01T00:00:00Z``, so a model can read only the rows of the window::

    where order_ts >= '{{ var("window_start") }}'::timestamp and order_ts < '{{ var("window_end") }}'::timestamp

A dbt task ends with one of these statuses, and the runner emits its ``run.started`` and ``run.finished`` events:

* ``succeeded``: dbt exited with 0 and every model marked ``ldp_publish`` was written.
* ``skipped_duplicate``: the same window of the same project was built before, and no task that ran earlier in this
  workflow run may have written data, so there is nothing to do. See below.
* ``blocked_quality``: only data tests failed, or the quality check of a model blocked its write. The data has to
  change, so a scheduler must not retry, and the workflow exits with 3.
* ``failed``: dbt or a write failed in any other way, such as a model that raised an error or a project that is
  missing. Nothing is published when dbt fails, and the models written before a failed write stay published.

**Running the same window again.** A pipeline task skips a window it already published, by its idempotency key. A dbt
task has no key, because a model is a function of its inputs and its publish is an overwrite or an upsert, so the
runner decides from the ledger of the workflow instead. The task is skipped when the last build of this task for this
window succeeded, the project (its files, the command, the selection and the variables) has the same fingerprint as
then, and no task that ran earlier in this run may have written data, which a task that failed may have done before
it stopped. Anything else runs the build again: a changed model, a window that failed last time, or an upstream task
that published new rows. A task that sets ``"skip_unchanged": false`` always runs.

This module needs only the standard library when it is imported. dbt is found when a task is planned or run.
"""

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, EngineNotFound
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
    register_task_kind,
)

logger = get_logger(__name__)

DBT_OPTIONS = ("project", "command", "select", "exclude", "vars", "skip_unchanged", "dbt_bin")
"""The keys of a dbt task: the project folder and the options of ``ldp dbt`` that make sense in a workflow."""

COMMANDS = ("build", "test")
"""The dbt commands a task can run. Only ``build`` publishes."""

WINDOW_VARS = ("window_start", "window_end")
"""The dbt variables that every run sets from its window, so a task's own ``vars`` cannot use them."""

MAX_FAILED_NODES = 10
"""How many failed nodes the events of a failed run list."""

STDERR = 2
"""The descriptor that dbt's output goes to, so ``ldp workflow run --json`` keeps standard output for its result."""

_LEDGER_STATUSES = ("succeeded", "failed", "blocked_quality")
"""The statuses of a dbt task that say what the last build of a window did. A skip and an ``upstream_failed`` do not."""


# ---------------------------------------------------------------------- validation


def _selectors(value: Any) -> list[str]:
    """The dbt selectors a ``select`` or ``exclude`` value means: a string is split at spaces, as dbt splits it."""
    items = [value] if isinstance(value, str) else list(value)
    return [token for item in items for token in item.split()]


def _selector_problems(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, str):
        items, where = [value], lambda number: ""
    elif isinstance(value, list) and value:
        items, where = value, lambda number: f"[{number}]"
    else:
        return [("", f"must be a dbt selector such as 'gold+', or a non-empty list of them, got {value!r}")]
    problems = []
    for number, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            problems.append((where(number), f"must be a non-empty dbt selector, got {item!r}"))
        elif any(token.startswith("-") for token in item.split()):
            problems.append((where(number), f"must be dbt selectors, not options, got {item!r}"))
    return problems


def _json_problem(value: Any, path: str) -> str | None:
    """Why ``value`` cannot be a dbt variable, as a message that names the offending part, or ``None``."""
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{path} must be a finite number, got {value!r}"
    if isinstance(value, list):
        return next((message for number, item in enumerate(value)
                     if (message := _json_problem(item, f"{path}[{number}]"))), None)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{path} has a key that is not a string: {key!r}"
            if message := _json_problem(item, f"{path}.{key}"):
                return message
        return None
    return f"{path} must be JSON, got {type(value).__name__}"


def validate(value: Any) -> list[tuple[str, str]]:
    """The problems of the value of a ``dbt`` task, as ``(path, message)``."""
    if not isinstance(value, Mapping):
        return [("", "must be an object with a 'project', the path of a dbt project folder with an ldp_dbt.json")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(map(str, value)) - set(DBT_OPTIONS))
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(DBT_OPTIONS)}"))
    project = value.get("project")
    if not isinstance(project, str) or not project.strip():
        problems.append((".project", "is required: the path of a dbt project folder with an ldp_dbt.json"))
    if "command" in value and value["command"] not in COMMANDS:
        problems.append((".command", f"must be one of {list(COMMANDS)}, got {value['command']!r}"))
    for key in ("select", "exclude"):
        if key in value:
            problems += [(f".{key}{suffix}", message) for suffix, message in _selector_problems(value[key])]
    if "vars" in value:
        variables = value["vars"]
        if not isinstance(variables, Mapping):
            problems.append((".vars", f"must be an object of dbt variables, got {variables!r}"))
        else:
            for name in WINDOW_VARS:
                if name in variables:
                    problems.append((f".vars.{name}", "is set by the run from its window, so a task cannot set it"))
            if message := _json_problem(variables, "vars"):
                problems.append(("", message))
    if "skip_unchanged" in value and not isinstance(value["skip_unchanged"], bool):
        problems.append((".skip_unchanged", f"must be true or false, got {value['skip_unchanged']!r}"))
    if "dbt_bin" in value and not (isinstance(value["dbt_bin"], str) and value["dbt_bin"].strip()):
        problems.append((".dbt_bin", f"must be the path of a dbt, or a name to look up on PATH, got "
                                     f"{value['dbt_bin']!r}"))
    return problems


def json_schema() -> dict[str, Any]:
    """The JSON Schema of the value of a ``dbt`` task."""
    selector = {"oneOf": [{"type": "string", "minLength": 1},
                          {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}]}
    return {
        "type": "object", "required": ["project"], "additionalProperties": False,
        "description": "Run a dbt project for the window of the workflow run, as `ldp dbt build` does: register the "
                       "Iceberg tables as sources, run dbt, and write the models marked ldp_publish to Iceberg. dbt "
                       "gets the window as the variables window_start and window_end.",
        "properties": {
            "project": {"type": "string", "minLength": 1,
                        "description": "The dbt project folder, with its ldp_dbt.json, relative to the workflow file."},
            "command": {"enum": list(COMMANDS), "default": "build",
                        "description": "`build` runs the models and tests and publishes the marked models. `test` "
                                       "runs the data tests and publishes nothing."},
            "select": {**selector, "description": "The dbt selection, as for `dbt build --select`."},
            "exclude": {**selector, "description": "What to leave out of the selection."},
            "vars": {"type": "object",
                     "description": "dbt variables, passed with --vars. window_start and window_end are set by the "
                                    "run and cannot be set here."},
            "skip_unchanged": {"type": "boolean", "default": True,
                               "description": "Skip the task when the last build of this window succeeded with the "
                                              "same project and no task that ran earlier may have written data. Set "
                                              "it to false for a project that reads files outside its folder."},
            "dbt_bin": {"type": "string", "minLength": 1,
                        "description": "The dbt to run, as `ldp dbt --dbt-bin` takes it: a path, or a name to look up "
                                       "on PATH. By default the one next to this Python, else the one on PATH."},
        },
    }


# ---------------------------------------------------------------------- what a run passes to dbt


def _identity(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options that decide what a build makes, in the form that goes into its fingerprint."""
    return {"command": options.get("command", "build"), "select": _selectors(options.get("select", [])),
            "exclude": _selectors(options.get("exclude", [])), "vars": dict(options.get("vars", {}))}


def _window(bounds: Any) -> list[str]:
    from local_data_platform.spec import format_instant

    return [format_instant(bounds[0]), format_instant(bounds[1])]


def _arguments(identity: Mapping[str, Any], window: list[str]) -> list[str]:
    """What follows the command on the dbt command line: the selection, and the variables with the window."""
    extra: list[str] = []
    if identity["select"]:
        extra += ["--select", *identity["select"]]
    if identity["exclude"]:
        extra += ["--exclude", *identity["exclude"]]
    variables = {**identity["vars"], "window_start": window[0], "window_end": window[1]}
    return [*extra, "--vars", json.dumps(variables, sort_keys=True, separators=(",", ":"))]


def _fingerprint(project: Any, identity: Mapping[str, Any], extra: list[str]) -> str:
    from local_data_platform.dbt_layer.build import project_fingerprint
    from local_data_platform.dbt_layer.project import find_target_path

    return project_fingerprint(project, target_path=find_target_path(project, extra), extra=identity)


# ---------------------------------------------------------------------- plan


def _source_tables(project: Any) -> dict[str, str]:
    """The Iceberg tables that the source configs of a project name, which dbt reads as sources."""
    from local_data_platform import spec

    found: dict[str, str] = {}
    for entry in project.source_configs:
        if entry.is_dir():
            files = sorted(item for item in entry.glob("*.json")
                           if item.is_file() and spec.kind_of_file(item) in (spec.DEFAULT_KIND, None))
        else:
            files = [entry] if entry.is_file() else []
        for file in files:
            sources, targets = config_tables(file)
            found.update(sources)
            found.update(targets)
    return found


def plan(task: Task, ctx: PlanContext) -> TaskPlan:
    """Check the project of a ``dbt`` task and say what a run would pass to dbt."""
    from local_data_platform.dbt_layer.project import DbtProject, check_profile, find_dbt

    options = task.value
    identity = _identity(options)
    outcome = TaskPlan(details={"project": options["project"], "command": identity["command"],
                                "target": f"dbt {identity['command']}"})
    try:
        project = DbtProject.load(ctx.resolve(options["project"]))
    except ConfigError as exc:
        outcome.errors.append(str(exc))
        return outcome
    if not (project.directory / "dbt_project.yml").is_file():
        outcome.errors.append(f"{project.directory} has no dbt_project.yml, so it is not a dbt project")
    try:
        find_dbt(options.get("dbt_bin"))
    except EngineNotFound as exc:
        outcome.errors.append(str(exc))
    try:
        check_profile(project)
    except ConfigError as exc:
        outcome.errors.append(str(exc))
    if identity["select"]:
        outcome.details["select"] = " ".join(identity["select"])
    if identity["exclude"]:
        outcome.details["exclude"] = " ".join(identity["exclude"])
    window = _window(ctx.bounds) if ctx.bounds is not None else None
    if window is not None:
        outcome.details["window_vars"] = {"window_start": window[0], "window_end": window[1]}
    try:
        outcome.details["fingerprint"] = _fingerprint(project, identity, _arguments(identity, window or ["", ""]))
    except OSError as exc:
        outcome.notes.append(f"the project cannot be hashed for the skip rule: {exc}")
    outcome.reads = _source_tables(project)
    return outcome


# ---------------------------------------------------------------------- the ledger


def last_build(ledger: Path, workflow: str, task_id: str, window: list[str], current_run: str,
               attempt: int = 1) -> Mapping[str, Any] | None:
    """The payload of the last ``run.finished`` of a dbt task for a window, from another run, or ``None``.

    A build of the current run does not count, unless it was an earlier attempt of the task: a run that a later tick
    carries on goes on with the attempts after the last one, so ``attempt`` is the one that is asking and a build of
    this run with a lower attempt is an earlier build. The default, 1, leaves out the whole run.

    Only a build that ended with ``succeeded``, ``failed`` or ``blocked_quality`` counts, so a skip and an
    ``upstream_failed`` do not hide what the last real build did. A line that is not valid JSON, such as one cut short
    by a crash, is ignored, and so is a ledger that does not exist.
    """
    try:
        handle = open(ledger, encoding="utf-8")
    except OSError:
        return None
    last = None
    with handle:
        for line in handle:
            if task_id not in line or "run.finished" not in line:
                continue  # a cheap test that spares most lines the JSON parser
            try:
                event = json.loads(line)
                payload = event["payload"]
            except (ValueError, KeyError, TypeError):
                continue
            if (event.get("type") == "run.finished" and isinstance(payload, Mapping)
                    and payload.get("workflow") == workflow and payload.get("task_id") == task_id
                    and payload.get("task_kind") == "dbt"
                    and (payload.get("workflow_run_id") != current_run or _attempt_of(event) < attempt)
                    and payload.get("window") == window and payload.get("status") in _LEDGER_STATUSES):
                last = payload
    return last


def _attempt_of(event: Mapping[str, Any]) -> int:
    attempt = event.get("attempt")
    return attempt if isinstance(attempt, int) and attempt >= 1 else 1


def _built_before(ctx: RunContext, task: Task, window: list[str], fingerprint: str) -> str | None:
    """The id of the workflow run whose build this run would repeat, or ``None`` when the task has to run."""
    earlier = last_build(ctx.workflow.ledger_path, ctx.workflow.identifier, task.id, window, ctx.run_id, ctx.attempt)
    if earlier is None or earlier.get("status") != "succeeded" or earlier.get("fingerprint") != fingerprint:
        return None
    if ctx.wrote_data():
        logger.info("Workflow %s: dbt task %s built this window before, but a task that ran earlier may have "
                    "written data, so it runs", ctx.workflow.identifier, task.id)
        return None
    return str(earlier.get("workflow_run_id"))


# ---------------------------------------------------------------------- run


def run(task: Task, ctx: RunContext) -> TaskOutcome:
    """Build or test the project for the window of the run, and publish what a build made."""
    from local_data_platform.dbt_layer import build

    options = task.value
    identity = _identity(options)
    command = identity["command"]
    prepared = build.prepare(ctx.resolve(options["project"]), dbt_bin=options.get("dbt_bin"))
    project = prepared.project
    window = _window(ctx.bounds)
    extra = _arguments(identity, window)
    fingerprint = _fingerprint(project, identity, extra)
    base: dict[str, Any] = {"command": command, "window": window, "fingerprint": fingerprint}

    if options.get("skip_unchanged", True) and not ctx.replace:  # a replace builds the window again, whatever it built
        earlier = _built_before(ctx, task, window, fingerprint)
        if earlier is not None:
            logger.info("Workflow %s: dbt task %s already built this window in run %s, so it did not run",
                        ctx.workflow.identifier, task.id, earlier)
            return TaskOutcome("skipped_duplicate", {**base, "built_by": earlier})

    registration = build.register(project)
    for item in registration.unavailable:
        logger.warning("%s is not a source yet: %s", item["table"], item["reason"])
    logger.info("Workflow %s: dbt task %s %s", ctx.workflow.identifier, task.id,
                build.registration_summary(registration))
    try:
        if command == "build":
            result = build.run_build(project, prepared.binary, extra, stdout=STDERR,
                                     wrap_sink=lambda own: ctx.task_sink(task, own))
        else:
            result = build.run_test(project, prepared.binary, extra, stdout=STDERR)
    except build.DbtFailed as error:
        detail = {**base, "exit_status": error.status, "nodes": error.nodes,
                  "failed_nodes": [{"node": node["node"], "status": node["status"]}
                                   for node in error.failed[:MAX_FAILED_NODES]],
                  "failed_total": len(error.failed)}
        raise (TaskBlocked if error.data_gate else TaskFailed)(str(error), detail) from error
    except build.PublishFailed as error:
        model, _ = error.outcome.failed
        detail = {**base, "published": [item.table for item in error.outcome.published], "failed_model": model,
                  "not_published": list(error.outcome.pending or [])}
        raise (TaskBlocked if error.data_gate else TaskFailed)(str(error), detail) from error

    detail = {**base, "nodes": dict(result.nodes)}
    if result.elapsed_s is not None:
        detail["dbt_s"] = result.elapsed_s
    if command == "build":
        detail["published"] = [{"table": item.table, "mode": item.mode, "rows_before": item.rows_before,
                                "rows_after": item.rows_after, "snapshot_id": item.snapshot_id}
                               for item in result.published]
    return TaskOutcome("succeeded", detail)


register_task_kind(
    "dbt", validate=validate, json_schema=json_schema, plan=plan, run=run,
    description="Run a dbt project for the window of the workflow run and publish the models marked ldp_publish, as "
                "ldp dbt build does.")


__all__ = ["COMMANDS", "DBT_OPTIONS", "WINDOW_VARS", "json_schema", "last_build", "plan", "run", "validate"]
