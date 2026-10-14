"""The ``ldp workflow`` command: ``ldp workflow plan FILE [--window W]`` and ``ldp workflow run FILE --window W``.

``plan`` checks a workflow file, or a dataset config with a schedule, and every config it names, and prints the tasks
in the order they run with the idempotency key of each pipeline task for the window. It runs nothing and exits with 1
when anything is invalid. ``run`` runs the tasks one at a time and exits with 0 when every task succeeded, with 3 when
the only problem is a data gate that held a task back, and with 1 when anything else went wrong.

``cli.py`` wires this in with :func:`add_cli`. Heavy imports happen inside the commands.
"""

import json
import sys
from collections.abc import Sequence
from typing import Any

WINDOW_HELP = ("the window to plan or run: an ISO 8601 interval such as 2026-08-01/2026-08-02, or yesterday, "
               "last-week or last-month, counted in the time zone of the workflow")


def _out(text: str = "") -> None:
    print(text, file=sys.stdout)


def _err(text: str = "") -> None:
    print(text, file=sys.stderr)


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> Any:
    """Register ``ldp workflow plan|run`` on the ``ldp`` sub-command parser.

    Args:
        subparsers: The object ``argparse.ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options such as ``-v``.

    Returns:
        The ``workflow`` parser.
    """
    parents = list(parents)
    workflow = subparsers.add_parser(
        "workflow", parents=parents, help="plan and run a workflow file",
        description="A workflow file lists the pipelines, dbt models and maintenance that run for each window, and in "
                    "what order. A dataset config that has a schedule is a workflow of one task.",
        epilog="Exit status is 0 on success, 1 on an error or an invalid workflow, and 3 when the only problem of a "
               "run is a data gate that held a task back.")
    actions = workflow.add_subparsers(dest="workflow_command", metavar="COMMAND")

    def usage(args: Any) -> int:
        workflow.print_help()
        return 1

    workflow.set_defaults(handler=usage)

    plan = actions.add_parser(
        "plan", parents=parents, help="check a workflow and show the order its tasks run in",
        description="Check FILE and every config it names, without running anything, and print the tasks in the order "
                    "they run with the idempotency key of each pipeline task for the window. It rejects a cycle, an "
                    "unknown id and a duplicate id, and warns when a task reads a table that another writes and no "
                    "depends_on connects them. It exits with 1 when anything is invalid.")
    plan.add_argument("file", metavar="FILE", help="a workflow file, or a dataset config with a schedule")
    plan.add_argument("--window", metavar="WINDOW", help=WINDOW_HELP)
    plan.add_argument("--json", action="store_true", help="print the plan as JSON")
    plan.set_defaults(handler=_cmd_plan)

    run = actions.add_parser(
        "run", parents=parents, help="run the tasks of a workflow for a window",
        description="Run the tasks of FILE one at a time, in dependency order, each pipeline task for the window, so "
                    "its idempotency key is the one 'ldp plan CONFIG --window' prints. A task that fails, or that a "
                    "quality check blocks, makes the tasks that depend on it upstream_failed, and the rest still run. "
                    "Run the same window again after fixing the cause: the pipelines that published are skipped by "
                    "their keys, and only the tasks that failed or did not run do their work. The events of the run go "
                    "to .ldp/events.jsonl next to FILE.")
    run.add_argument("file", metavar="FILE", help="a workflow file, or a dataset config with a schedule")
    run.add_argument("--window", metavar="WINDOW", required=True, help=WINDOW_HELP)
    run.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    run.set_defaults(handler=_cmd_run)
    return workflow


def _cell(value: Any) -> Any:
    """A table cell: a dash for what the task has none of, such as the rows of a dbt task, where a table says NULL."""
    return "-" if value is None else value


def _short(value: Any, width: int = 12) -> Any:
    return value[:width] if isinstance(value, str) and len(value) > width else value


def _print_plan(result: dict[str, Any]) -> None:
    from local_data_platform.cli import format_table

    if result["workflow"] is not None:
        lines = [("workflow", result["workflow"]), ("file", result["file"])]
        if result["schedule"]:
            lines.append(("schedule", f"{result['schedule']} (from {result['schedule_from']})"))
        lines.append(("timezone", result["timezone"]))
        if result["window"]:
            lines.append(("window", result["window"]))
        lines.append(("tasks", str(len(result["tasks"]))))
        width = max(len(name) for name, _ in lines)
        for name, value in lines:
            _out(f"{name.ljust(width)}  {value}")
        _out()
        rows = [{"order": task["order"], "id": task["id"], "kind": task["kind"],
                 "depends_on": ", ".join(task["depends_on"]) or "-",
                 "runs": task.get("config") or task.get("project") or "-",
                 "target": task.get("target") or task.get("table") or "-",
                 "key": _short(task.get("idempotency_key")) or "-"} for task in result["tasks"]]
        _out(format_table(rows))
    if result["errors"]:
        _err(f"{len(result['errors'])} problem{'s' if len(result['errors']) != 1 else ''}:")
        for message in result["errors"]:
            _err(f"  - {message}")
    for message in result["warnings"]:
        _err(f"warning: {message}")


def _cmd_plan(args: Any) -> int:
    from local_data_platform.orchestration.workflow import plan_workflow

    result = plan_workflow(args.file, window=args.window)
    if args.json:
        _out(json.dumps(result, indent=2))
    else:
        _print_plan(result)
    return 0 if result["valid"] else 1


def _print_result(result: Any) -> None:
    from local_data_platform.cli import format_table
    from local_data_platform.spec import format_instant

    _out(f"workflow {result.workflow}  run {result.run_id}  "
         f"window {format_instant(result.window[0])}/{format_instant(result.window[1])}")
    rows = []
    for task in result.tasks.values():
        rows.append({"id": task.id, "kind": task.kind, "status": task.status,
                     "rows_read": _cell(task.detail.get("rows_read")),
                     "rows_written": _cell(task.detail.get("rows_written")),
                     "snapshot": _cell(task.detail.get("snapshot_id")), "seconds": round(task.duration_s, 2)})
    _out(format_table(rows))
    for task in result.tasks.values():
        for item in task.detail.get("published", []):
            if isinstance(item, dict):  # the tables a dbt task wrote, as `ldp dbt build` prints them
                _out(f"{task.id} published {item['table']}: {item['mode']}, {item['rows_before']} -> "
                     f"{item['rows_after']} rows, snapshot {item['snapshot_id']}")
    counted = ", ".join(f"{count} {status}" for status, count in result.counts().items())
    _out(f"{result.status}: {counted} in {result.duration_s:.1f}s. Events: {result.ledger}")
    for task in result.tasks.values():
        if task.error:
            _err(f"  {task.id}: {task.error.splitlines()[0]}")
        elif task.status == "upstream_failed":
            _err(f"  {task.id}: did not run, because {', '.join(task.detail['blocked_by'])} did not succeed")


def _cmd_run(args: Any) -> int:
    from local_data_platform.orchestration.runner import run_workflow

    result = run_workflow(args.file, window=args.window)
    if args.json:
        _out(json.dumps(result.to_dict(), indent=2))
    else:
        _print_result(result)
    return result.exit_code


__all__ = ["add_cli"]
