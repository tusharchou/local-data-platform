"""The ``ldp schedule`` commands: ``list``, ``tick`` and ``install``.

``ldp schedule tick DIR`` runs every window that is due and not done, for every workflow and every dataset config under
DIR that has a schedule, and exits. cron or launchd calls it every 5 minutes. It prints a line for each window that ran
and a summary, and exits with 0 when nothing failed or was blocked, with 3 when a data gate is the only problem, and
with 1 otherwise. A tick that starts while another runs on the same folder does nothing and exits with 0.

``ldp schedule list DIR`` shows, for each scheduled workflow and config, the cron expression, the time zone, the next
fire in UTC and in local time, the last window and how it ended, and the windows that were missed with the
``ldp backfill`` command that fills them. ``ldp schedule install --print cron|launchd DIR`` prints the recipe that calls
the tick every 5 minutes, and writes nothing.

``cli.py`` wires this in with :func:`add_cli`. Heavy imports happen inside the commands.
"""

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any

from local_data_platform.orchestration.cli import _counted, _err, _out, _whole_number

SIZE_TEXT = {"day": "one day", "hour": "one hour", "15m": "15 minutes"}

NOW_HELP = ("the time to act at, such as 2026-08-05T02:05:00Z. UTC unless it has an offset. The current time by "
            "default, and a test or a demo passes it instead of waiting")


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> Any:
    """Register ``ldp schedule list|tick|install`` on the ``ldp`` sub-command parser.

    Args:
        subparsers: The object ``argparse.ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options such as ``-v``.

    Returns:
        The ``schedule`` parser.
    """
    from local_data_platform.orchestration.backfills import DEFAULT_MAX_WINDOWS
    from local_data_platform.orchestration.recipes import INTERVAL_MINUTES, RECIPES

    parents = list(parents)
    schedule = subparsers.add_parser(
        "schedule", parents=parents, help="run what is due on a schedule, with no services",
        description="A scheduler for a laptop. 'tick' runs every window that is due and not done, for every workflow "
                    f"and dataset config under DIR that has a schedule, and exits. cron or launchd calls it every "
                    f"{INTERVAL_MINUTES} minutes, so a machine that was asleep catches up at the first tick after it "
                    "wakes. A window that failed is carried on by the next tick.",
        epilog="Exit status is 0 on success, 1 on an error, and 3 when the only problem of a tick is a data gate that "
               "held a window back.")
    actions = schedule.add_subparsers(dest="schedule_command", metavar="COMMAND")

    def usage(args: Any) -> int:
        schedule.print_help()
        return 1

    schedule.set_defaults(handler=usage)

    def common(command: Any) -> None:
        command.add_argument("directory", metavar="DIR", help="the folder to search for workflows and configs")
        command.add_argument("--now", metavar="TIME", help=NOW_HELP)
        command.add_argument("--max-windows", type=_whole_number, default=DEFAULT_MAX_WINDOWS, metavar="N",
                             help="the most windows a workflow runs or lists at a time "
                                  f"(default {DEFAULT_MAX_WINDOWS})")
        command.add_argument("--json", action="store_true", help="print the result as JSON on stdout")

    listing = actions.add_parser(
        "list", parents=parents, help="show every scheduled workflow, its next fire and what it missed",
        description="List every workflow file and dataset config under DIR that has a schedule, told apart by kind. "
                    "For each it shows the cron expression, the time zone, the next fire in UTC and in local time, the "
                    "last window and how it ended, the windows that are due, and the windows that were missed with "
                    "the exact 'ldp backfill' command that fills them. It writes nothing.")
    common(listing)
    listing.set_defaults(handler=_cmd_list)

    ticking = actions.add_parser(
        "tick", parents=parents, help="run every window that is due and not done, then exit",
        description="Find every workflow file and dataset config under DIR that has a schedule, and run each window "
                    "that is due at the time of the tick and not done, oldest first. Without catchup that is the "
                    "latest window, and with metadata.catchup it is every window since metadata.start. A window that "
                    "failed, was blocked or never finished on an earlier tick is carried on: it keeps its run id and "
                    "its attempt goes up by one. A tick that starts while another runs on DIR does nothing and exits "
                    "with 0. The events of each workflow go to .ldp/events.jsonl next to its file.")
    common(ticking)
    ticking.set_defaults(handler=_cmd_tick)

    installing = actions.add_parser(
        "install", parents=parents, help="print a cron or launchd recipe that ticks every 5 minutes",
        description=f"Print the crontab line or the launchd plist that calls 'ldp schedule tick DIR' every "
                    f"{INTERVAL_MINUTES} minutes, with the absolute path of the ldp executable. It never writes to a "
                    "system location, so you decide where the text goes. To add the line to your crontab: "
                    "'(crontab -l; ldp schedule install --print cron DIR) | crontab -'.")
    installing.add_argument("directory", metavar="DIR", help="the folder to tick")
    installing.add_argument("--print", dest="recipe", choices=RECIPES, required=True, metavar="|".join(RECIPES),
                            help="the recipe to print: a crontab line for Linux, or a launchd plist for macOS")
    installing.add_argument("--ldp", metavar="PATH",
                            help="the ldp executable the recipe calls (default: the one that is running, or the first "
                                 "on the PATH)")
    installing.set_defaults(handler=_cmd_install)
    return schedule


# ---------------------------------------------------------------------- list


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _span(pair: Any) -> str:
    from local_data_platform.spec import format_instant

    return f"{format_instant(pair[0])}/{format_instant(pair[1])}"


def _print_listing(listing: Any, local_tz: dt.tzinfo | None = None) -> None:
    from local_data_platform.spec import format_instant

    found = _plural(len(listing.items), "workflow or config").replace("workflow or configs", "workflows or configs")
    _out(f"{found} with a schedule under {listing.directory} at {format_instant(listing.now)}")
    for item in listing.items:
        _out()
        _out(f"{item.workflow}  {item.kind}  {item.path}")
        if item.error is not None:
            _out(f"  error        {item.error}")
            continue
        size = ("a window is the time between two fires" if item.window_size is None
                else f"windows of {SIZE_TEXT[item.window_size]}")
        _out(f"  schedule     {item.cron}, in {item.timezone}, {size}")
        if item.catchup:
            _out(f"  catch-up     on, since {format_instant(item.start)}")
        if item.next_fire is None:
            _out("  next fire    none")
        else:
            local = item.next_fire.astimezone(local_tz).isoformat(timespec="seconds")
            _out(f"  next fire    {format_instant(item.next_fire)}, {local} local")
        _out(f"  last window  {item.last_window} {item.last_status}" if item.last_window else "  last window  none yet")
        if not item.due:
            _out("  due          nothing")
        elif len(item.due) == 1:
            _out(f"  due          1 window, {_span(item.due[0])}")
        else:
            _out(f"  due          {len(item.due)} windows, {_span((item.due[0][0], item.due[-1][1]))}")
        if not item.missed:
            _out("  missed       none")
            continue
        more = ", and more after them" if item.more_missed else ""
        fill = "it" if len(item.missed) == 1 else "them"
        _out(f"  missed       {_plural(len(item.missed), 'window')}{more}. Fill {fill} with")
        for command in item.commands:
            _out(f"    {command}")
    for path, lines in listing.problems:
        _out()
        _out(f"{path}  has a schedule and cannot be used")
        for line in lines:
            _out(f"  {line}")


def _cmd_list(args: Any) -> int:
    from local_data_platform.orchestration.schedule import list_scheduled

    listing = list_scheduled(args.directory, now=args.now, max_windows=args.max_windows)
    if args.json:
        _out(json.dumps(listing.to_dict(), indent=2))
    else:
        _print_listing(listing)
    return 1 if listing.problems else 0


# ---------------------------------------------------------------------- tick


def _cmd_tick(args: Any) -> int:
    from local_data_platform.orchestration.schedule import LOCK_PATH, tick
    from local_data_platform.spec import format_instant

    def show(item: Any, entry: Any, number: int, total: int) -> None:
        if args.json or entry.outcome == "skipped":
            return  # a window that was done is counted, and not listed, so an idle tick stays short
        carried = f"  (attempt {entry.attempt})" if (entry.attempt or 1) > 1 else ""
        _out(f"{item.workflow.identifier}  {entry.window}  {entry.outcome}{carried}")
        for problem in entry.problems():
            _err(f"  {problem}")

    result = tick(args.directory, now=args.now, max_windows=args.max_windows, on_window=show)
    if args.json:
        _out(json.dumps(result.to_dict(), indent=2))
        return result.exit_code
    if result.overlapped:
        _out(f"another tick holds {result.directory / LOCK_PATH}, so this one did nothing")
        return 0
    for item in result.workflows:
        if item.error is not None:
            _err(f"{item.workflow}  could not be ticked")
            for line in item.error.splitlines()[:8]:
                _err(f"  {line}")
    for path, lines in result.problems:
        _err(f"{path}  has a schedule and cannot be used")
        for line in lines:
            _err(f"  {line}")
    due = sum(item.due for item in result.workflows)
    _out(f"tick {format_instant(result.now)}: {_plural(len(result.workflows), 'workflow')}, "
         f"{_plural(due, 'window')} due, {_counted(result.counts())} in {result.duration_s:.1f}s")
    if result.exit_code:
        _err("The next tick runs the windows that failed or were blocked again")
    return result.exit_code


# ---------------------------------------------------------------------- install


def _cmd_install(args: Any) -> int:
    from local_data_platform.orchestration.recipes import recipe

    _out(recipe(args.recipe, args.directory, args.ldp).rstrip("\n"))
    return 0


__all__ = ["add_cli"]
