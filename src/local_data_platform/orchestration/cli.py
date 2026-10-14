"""The ``ldp workflow`` and ``ldp backfill`` commands.

``ldp workflow plan FILE [--window W]`` and ``ldp workflow run FILE --window W`` plan and run a workflow for one window.
``ldp backfill FILE --from START [--to END]`` runs it for every window of a range, and again to resume.

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
    print(text, file=sys.stdout, flush=True)


def _err(text: str = "") -> None:
    print(text, file=sys.stderr, flush=True)


def _whole_number(text: str) -> int:
    import argparse

    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number from 1, got {value}")
    return value


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
                    "A task with retries that fails on an error that another attempt may fix, such as a dropped "
                    "connection, runs again after a wait, with the same run id and the next attempt number. "
                    "Run the same window again after fixing the cause: the pipelines that published are skipped by "
                    "their keys, and only the tasks that failed or did not run do their work. The events of the run go "
                    "to .ldp/events.jsonl next to FILE.")
    run.add_argument("file", metavar="FILE", help="a workflow file, or a dataset config with a schedule")
    run.add_argument("--window", metavar="WINDOW", required=True, help=WINDOW_HELP)
    run.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    run.set_defaults(handler=_cmd_run)
    _add_backfill(subparsers, parents)
    return workflow


def _add_backfill(subparsers: Any, parents: Sequence[Any]) -> None:
    from local_data_platform.orchestration.backfills import DEFAULT_MAX_WINDOWS

    backfill = subparsers.add_parser(
        "backfill", parents=list(parents), help="run a workflow or config for every window of a date range",
        description="Run FILE for every window from --from to --to, oldest first, each pipeline task under the key of "
                    "its window. A window that is done, because its keys are on the table, is skipped without being "
                    "read, so a backfill that stopped halfway is resumed by running it again, and a finished one "
                    "does nothing. A window that fails, or that a quality check blocks, does not stop the others.",
        epilog="Exit status is 0 when no window failed or was blocked, 3 when a data gate is the only problem, and 1 "
               "otherwise, or when the command could not start. Example: ldp backfill daily_orders.json "
               "--from 2026-06-01 --to 2026-09-01 --dry-run")
    backfill.add_argument("file", metavar="FILE", help="a workflow file, or a dataset config with a window")
    backfill.add_argument("--from", dest="start", metavar="START", required=True,
                          help="where the range starts, such as 2026-06-01 or 2026-06-01T06:00:00. A value with no "
                               "offset is read in the time zone of the workflow, and it has to start a window")
    backfill.add_argument("--to", dest="end", metavar="END",
                          help="where the range ends, read as --from is. The window that starts at END is not part "
                               "of the range. By default the range ends with the last complete window")
    backfill.add_argument("--dry-run", action="store_true",
                          help="print every window with its key and its state, and run nothing")
    backfill.add_argument("--replace", action="store_true",
                          help="write every window again under a replace key, with a window overwrite, so that the "
                               "rows a window has are replaced by the ones the source has now. Needs a "
                               "metadata.window.column in every pipeline that writes to Iceberg")
    backfill.add_argument("--run-backwards", action="store_true", help="run the newest window first")
    backfill.add_argument("--max-windows", type=_whole_number, default=DEFAULT_MAX_WINDOWS, metavar="N",
                          help=f"refuse a range with more than N windows, to catch a typo (default "
                               f"{DEFAULT_MAX_WINDOWS})")
    backfill.add_argument("--stop-on-failure", action="store_true",
                          help="stop at the first window that failed, instead of going on with the others. A window "
                               "that a quality check blocked does not stop it")
    backfill.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    backfill.set_defaults(handler=_cmd_backfill)


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
        retried = any("retries" in task for task in result["tasks"])
        rows = []
        for task in result["tasks"]:
            row = {"order": task["order"], "id": task["id"], "kind": task["kind"],
                   "depends_on": ", ".join(task["depends_on"]) or "-",
                   "runs": task.get("config") or task.get("project") or "-",
                   "target": task.get("target") or task.get("table") or "-",
                   "key": _short(task.get("idempotency_key")) or "-"}
            if retried:  # the most attempts a task gets, which is 1 for a task that is not retried
                row["attempts"] = task.get("retries", {}).get("max_attempts", 1)
            rows.append(row)
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
    retried = any(task.attempts > 1 for task in result.tasks.values())
    for task in result.tasks.values():
        row = {"id": task.id, "kind": task.kind, "status": task.status}
        if retried:  # the column shows only when a task ran again
            row["attempts"] = task.attempts
        row.update({"rows_read": _cell(task.detail.get("rows_read")),
                    "rows_written": _cell(task.detail.get("rows_written")),
                    "snapshot": _cell(task.detail.get("snapshot_id")), "seconds": round(task.duration_s, 2)})
        rows.append(row)
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


def _counted(counts: Any) -> str:
    """``12 published, 0 skipped, 1 blocked, 0 failed``, and the windows that were not run when there are some."""
    text = ", ".join(f"{counts.get(name, 0)} {name}" for name in ("published", "skipped", "blocked", "failed"))
    return text + (f", {counts['not_run']} not run" if counts.get("not_run") else "")


def _print_dry_run(result: Any) -> None:
    from local_data_platform.cli import format_table

    rows = []
    for entry in result.windows:
        keys = list(entry.keys.values())
        key = "-" if not keys else _short(keys[0]) + (f" (+{len(keys) - 1})" if len(keys) > 1 else "")
        rows.append({"window": entry.window, "key": key, "state": entry.state})
    _out(format_table(rows))
    states = ", ".join(f"{count} {name}" for name, count in result.states().items() if count)
    _out(f"{len(result.windows)} windows of size {result.window_size}, {result.order}: {states}. Nothing was run.")


def _cmd_backfill(args: Any) -> int:
    from local_data_platform.orchestration.backfills import backfill

    seen = {"published": 0, "skipped": 0, "blocked": 0, "failed": 0}

    def show(entry: Any, number: int, total: int) -> None:
        seen[entry.outcome] += 1
        if args.json or entry.outcome == "skipped":
            return  # a window that was done is counted, and not listed, so a resumed backfill stays short
        _out(f"[{number}/{total}] {entry.window}  {entry.outcome}")
        for problem in entry.problems():
            _err(f"  {problem}")

    try:
        result = backfill(args.file, args.start, args.end, replace=args.replace, dry_run=args.dry_run,
                          run_backwards=args.run_backwards, max_windows=args.max_windows,
                          stop_on_failure=args.stop_on_failure, on_window=None if args.dry_run else show)
    except KeyboardInterrupt:
        _err(f"interrupted after {sum(seen.values())} windows ({_counted(seen)}). Run the same command again to "
             "resume")
        raise
    if args.json:
        _out(json.dumps(result.to_dict(), indent=2))
    elif args.dry_run:
        _print_dry_run(result)
    else:
        _out(f"backfill {result.workflow}: {_counted(result.counts())} in {result.duration_s:.1f}s. "
             f"Events: {result.ledger}")
        if result.exit_code:
            _err("Fix the cause and run the same command again: the windows that are done are skipped")
    return result.exit_code


__all__ = ["add_cli"]
