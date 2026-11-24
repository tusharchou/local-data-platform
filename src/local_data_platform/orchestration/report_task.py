"""The ``report`` task kind: build a report for the window of a workflow run, and send it.

::

    {"id": "daily", "depends_on": ["gold"], "report": {"spec": "reports/daily_sales.json", "send": true}}

The task runs a report file for the window of the workflow run, which is the window that ``ldp report send REPORT
--window W`` takes, and with ``send`` true it sends the report to the destinations of its ``metadata.delivery``, as that
command does. Without ``send``, or with ``send`` false, it only builds the report, which runs its queries and its data
health and applies its freshness gate and writes and sends nothing, as a ``maintain`` task is a dry run unless
``apply`` is true. A workflow with one report task and a ``schedule`` is a report on its own schedule, and a report
task that depends on the ``gold`` task goes out when the gold tables of the night are built.

A report is delivered once for each window and destination, so running the window again sends nothing, and a send that
did not reach a destination is run again by the next tick, which sends to that destination only. A report whose
``metadata.freshness`` holds it back, or that waits for a pipeline of ``metadata.after`` that has not published the
window, is held back, and so is the task. The window has no delivery, so ``ldp schedule tick`` runs it again.

A report task ends with one of these statuses, and the runner emits its ``run.started`` and ``run.finished`` events,
with the destinations and what became of each in the detail. The events of the send itself, ``report.delivered`` and
the others, go to the sinks of the report, with the workflow, its run and the task, because that is where the report
looks for the deliveries it made.

* ``succeeded``: the report was sent to every destination that had not got it, or, without ``send``, it was built.
* ``skipped_duplicate``: every destination had the report for the window already, so nothing was sent.
* ``blocked_quality``: the report was held back by its freshness gate or by ``metadata.after``. Nothing was sent. The
  data has to change, so a scheduler must not retry within the tick, and the workflow exits with 3.
* ``failed``: a query did not run, a block cannot be shown, or a destination did not get the report. The others got it.

A replace, as ``ldp backfill --replace`` runs, does not send the report again, because a delivery is made once for each
window. ``ldp report send --force`` sends it again.

This module needs only the standard library when it is imported. The report is read when a task is planned or run.
"""

from collections.abc import Mapping
from typing import Any

from local_data_platform.exceptions import ConfigError
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

REPORT_OPTIONS = ("spec", "send")
"""The keys of a report task: the report file, and whether to send it."""


# ---------------------------------------------------------------------- validation


def validate(value: Any) -> list[tuple[str, str]]:
    """The problems of the value of a ``report`` task, as ``(path, message)``."""
    if not isinstance(value, Mapping):
        return [("", "must be an object with a 'spec', the path of a report file")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(map(str, value)) - set(REPORT_OPTIONS))
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(REPORT_OPTIONS)}"))
    spec = value.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        problems.append((".spec", "is required: the path of a report file"))
    if "send" in value and not isinstance(value["send"], bool):
        problems.append((".send", f"must be true or false, got {value['send']!r}"))
    return problems


def json_schema() -> dict[str, Any]:
    """The JSON Schema of the value of a ``report`` task."""
    return {
        "type": "object", "required": ["spec"], "additionalProperties": False,
        "description": "Build a report for the window of the workflow run, and send it to the destinations of its "
                       "metadata.delivery when `send` is true, as `ldp report send` does. A report is delivered once "
                       "for each window and destination.",
        "properties": {
            "spec": {"type": "string", "minLength": 1,
                     "description": "The report file, with kind Report, relative to the workflow file."},
            "send": {"type": "boolean", "default": False,
                     "description": "Send the report. Without it the task builds the report, which runs its queries "
                                    "and applies its freshness gate, and sends nothing."},
        },
    }


# ---------------------------------------------------------------------- plan


def plan(task: Task, ctx: PlanContext) -> TaskPlan:
    """Check the report of a ``report`` task and say what a run of the window would do."""
    from local_data_platform.mcp_server.discovery import is_allowed
    from local_data_platform.report.deliver import ordered
    from local_data_platform.report.health import config_index
    from local_data_platform.report.spec import load_report

    options = task.value
    send = options.get("send", False)
    outcome = TaskPlan(details={"spec": options["spec"], "send": send})
    try:
        report = load_report(ctx.resolve(options["spec"]))
    except ConfigError as exc:
        outcome.errors.append(str(exc))
        return outcome
    destinations = ordered(report.delivery)
    outcome.details.update(report=report.identifier, timezone=report.timezone,
                           target=", ".join(f"{item.name} ({item.type})" for item in destinations) if send
                           else "a build, nothing is sent")
    if report.after:
        outcome.details["after"] = list(report.after)
    if send:
        outcome.details["destinations"] = [{"destination": item.name, "type": item.type} for item in destinations]
        if not destinations:
            outcome.errors.append(f"the report {report.identifier} has no metadata.delivery, so there is nowhere to "
                                  "send it. Add a destination, or set send to false")
        elif not report.observability:
            outcome.notes.append(f"the report {report.identifier} has no sink in metadata.observability, so it cannot "
                                 "remember a delivery, and a run of the same window sends it again")
        if ctx.replace:
            outcome.notes.append("a replace does not send the report again, because a delivery is made once for each "
                                 "window. ldp report send --force sends it again")
    else:
        outcome.notes.append("send is not true, so the task builds the report for the window and sends nothing")
    index = config_index(report)
    missing = [name for name in report.after if name not in index]
    if missing:
        outcome.errors.append(f"metadata.after of the report {report.identifier} names {', '.join(missing)}, which no "
                              "config in metadata.tables.config has")
    zone = str(ctx.workflow.timezone)
    if zone != report.timezone:
        outcome.notes.append(f"the report counts its days in {report.timezone} and the windows of this workflow are "
                             f"counted in {zone}, so its queries see the window as the wall-clock times of "
                             f"{report.timezone}. Set metadata.window.timezone of the workflow to {report.timezone} "
                             "for a window that is a day of the report")
    allow = list(report.tables.allow) if report.tables.allow is not None else None
    for file, _config in index.values():  # what the report may read is what its configs write, as its allow list says
        for identity, name in config_tables(file)[1].items():
            if is_allowed(name, allow):
                outcome.reads[identity] = name
    return outcome


# ---------------------------------------------------------------------- run


def _not_delivered(report: Any, sent: Any) -> str:
    count = len(sent.failed)
    lines = [f"the report {report.identifier} did not reach {count} of {len(sent.outcomes)} destinations, and the "
             "others got it:"]
    lines += [f"  - {item.destination} ({item.type}): {item.reason}" for item in sent.failed]
    return "\n".join(lines)


def run(task: Task, ctx: RunContext) -> TaskOutcome:
    """Build the report for the window of the run, and send it when the task says so."""
    from local_data_platform import events
    from local_data_platform.report.build import build_report, report_sink
    from local_data_platform.report.deliver import send_report
    from local_data_platform.report.health import ReportHeld
    from local_data_platform.report.spec import load_report
    from local_data_platform.spec import format_instant

    options = task.value
    send = options.get("send", False)
    report = load_report(ctx.resolve(options["spec"]))
    sink = events.TaggedSink(report_sink(report), ctx.tags(task))
    detail: dict[str, Any] = {"report": report.identifier, "send": send,
                              "window": [format_instant(ctx.bounds[0]), format_instant(ctx.bounds[1])]}
    try:
        if not send:
            built = build_report(report, ctx.bounds, out=None, fmt="json", sink=sink)
            detail.update(queries=len(built.run.queries), blocks=len(report.blocks),
                          stale=[item.table for item in built.health.stale])
            return TaskOutcome("succeeded", detail)
        sent = send_report(report, ctx.bounds, sink=sink)
    except ReportHeld as held:
        detail["held"] = held.summary()
        raise TaskBlocked(str(held), detail, report=None) from held
    detail.update(send_run_id=sent.run_id, delivered=len(sent.delivered), skipped=len(sent.skipped),
                  failed=len(sent.failed),
                  destinations=[{"destination": item.destination, "type": item.type, "status": item.status,
                                 "attempts": item.attempts} for item in sent.outcomes])
    if sent.failed:
        raise TaskFailed(_not_delivered(report, sent), detail)
    return TaskOutcome("succeeded" if sent.delivered else "skipped_duplicate", detail)


register_task_kind(
    "report", validate=validate, json_schema=json_schema, plan=plan, run=run,
    description="Build a report for the window of the workflow run, and send it when send is true, as "
                "ldp report send does.")


__all__ = ["REPORT_OPTIONS", "json_schema", "plan", "run", "validate"]
