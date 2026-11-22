"""The ``ldp report`` commands.

``ldp report check REPORT_OR_DIR [--window WINDOW] [--param NAME=VALUE] [--release RN | --live] [--row-cap N] [--timeout
SECONDS] [--json]`` reads a report file, or every report file of a folder, finds its tables, runs every query and every
metric block, and says what each did. ``cli.py`` wires this in with :func:`add_cli`. Heavy imports happen inside the
command.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number of at least 1, got {value}")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {value:g}")
    return value


def _parameter(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
    return name.strip(), value


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> Any:
    """Register ``ldp report`` on the ``ldp`` sub-command parser.

    Args:
        subparsers: The object ``argparse.ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options such as ``-v``.

    Returns:
        The ``report`` parser.
    """
    parents = list(parents)
    report = subparsers.add_parser(
        "report", parents=parents, help="define reports in files, and check them against the gold tables",
        description="A report file, with \"kind\": \"Report\", names the tables a report reads, the queries it runs "
                    "and the blocks it shows. The commands read report files, and run their queries on the gold tables "
                    "as the current release holds them.",
        epilog="Exit status is 0 on success and 1 on an error. Example: ldp report check reports/daily_sales.json "
               "--window yesterday")
    actions = report.add_subparsers(dest="report_command", metavar="COMMAND")

    def usage(args: argparse.Namespace) -> int:
        report.print_help()
        return EXIT_ERROR

    report.set_defaults(handler=usage)

    check = actions.add_parser(
        "check", parents=parents, help="check a report and run every query of it",
        description="Check REPORT, a report file or a folder of them. The file is validated, the tables are found as "
                    "ldp mcp finds them, and every query and every metric block runs once in a locked-down DuckDB, "
                    "with the row cap and the timeout of the report. Each reads the tables as the current gold release "
                    "holds them, so the numbers agree with ldp metrics query. For each one it prints the rows, the "
                    "columns and how long it took. Every problem is listed with the JSON path of what is wrong. "
                    "Nothing is written.",
        epilog="Exit status is 0 when every report is valid and every query ran, and 1 when something is wrong. "
               "Example: ldp report check ldp_demo/restaurant/reports --window 2026-07-31/2026-08-01")
    check.add_argument("report", metavar="REPORT", help="a report file, or a folder that has report files")
    check.add_argument("--window", metavar="WINDOW",
                       help="the window of the run, such as 2026-07-31/2026-08-01, which is read as UTC like the "
                            "window of ldp run, or yesterday, last-week or last-month, counted in the time zone of the "
                            "report. The queries see it as $window_start and $window_end, as wall-clock times of that "
                            "zone. A query that uses them needs it")
    check.add_argument("--param", action="append", default=[], type=_parameter, metavar="NAME=VALUE", dest="params",
                       help="the value of a parameter that the report declares. Repeat it for more than one")
    when = check.add_mutually_exclusive_group()
    when.add_argument("--release", metavar="RELEASE",
                      help="read the tables as release RELEASE of ldp gold build names them, such as r2. The default "
                           "is the current release. A table that the release does not hold is read as it is now")
    when.add_argument("--live", action="store_true", help="read the tables as they are now, and no release")
    check.add_argument("--row-cap", type=_positive_int, metavar="N", dest="row_cap",
                       help="return at most this many rows from a query, instead of the row_cap of the report")
    check.add_argument("--timeout", type=_positive_float, metavar="SECONDS", dest="timeout",
                       help="stop a query after this many seconds, instead of the timeout_s of the report")
    check.add_argument("--json", action="store_true", help="print the result as JSON")
    check.set_defaults(handler=_cmd_check)
    return report


def _plural(number: int, noun: str, many: str | None = None) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {many or noun + 's'}"


def _params(items: Sequence[tuple[str, str]]) -> dict[str, str]:
    from local_data_platform.exceptions import ConfigError

    found: dict[str, str] = {}
    for name, value in items:
        if name in found:
            raise ConfigError(f"--param {name} is given twice")
        found[name] = value
    return found


def _describe(check: Any) -> str:
    """What a check did, as the lines that go to stdout."""
    from local_data_platform.cli import format_table
    from local_data_platform.spec import format_instant

    report, run = check.report, check.run
    if report is None or run is None:
        return ""
    ran = [item for item in run.queries.values() if item.ok]
    total = _plural(len(run.queries), "query", "queries")
    head = (f"ok, {total} ran in {run.duration_ms:.0f} ms" if check.ok
            else f"{_plural(len(check.problems), 'problem')}, {len(ran)} of {total} ran")
    lines = [f"{report.identifier} ({check.file.name}): {head}"]
    window = (f"{format_instant(run.window[0])}/{format_instant(run.window[1])}" if run.window is not None
              else "none")
    lines.append(f"  window {window}, time zone {report.timezone}")
    if run.queries:
        rows = [{"name": item.name, "kind": item.kind, "ok": "yes" if item.ok else "NO", "rows": item.rows if item.ok
                 else None, "columns": ", ".join(item.columns), "tables": ", ".join(item.tables), "ms": round(
                     item.duration_ms, 1) if item.ok else None} for item in run.queries.values()]
        lines += ["", format_table(rows)]
    if run.reads:
        reads = [{"table": item.table, "version read": item.source, "snapshot": item.snapshot_id}
                 for item in run.reads.values()]
        lines += ["", format_table(reads)]
    capped = [item.name for item in run.queries.values() if item.truncated]
    if capped:
        lines += ["", f"note: {', '.join(capped)} had more rows than the row cap, which cut them"]
    return "\n".join(lines)


def _cmd_check(args: argparse.Namespace) -> int:
    from local_data_platform.exceptions import ConfigError
    from local_data_platform.report.engine import check_report
    from local_data_platform.report.spec import report_files

    files = report_files(args.report)
    if not files:
        raise ConfigError(f"there is no report in {args.report}. A report file has \"kind\": \"Report\"")
    params = _params(args.params)
    checks = [check_report(file, args.window, params=params or None, release=args.release, live=args.live,
                           row_cap=args.row_cap, timeout_s=args.timeout) for file in files]
    if args.json:
        results = [{"file": str(item.file), "report": item.report.identifier if item.report else None, "ok": item.ok,
                    "problems": item.problems, **({k: v for k, v in item.run.to_dict().items()
                                                   if k not in ("report", "ok", "problems")} if item.run else {})}
                   for item in checks]
        single = len(results) == 1 and not _is_folder(args.report)
        print(json.dumps(results[0] if single else results, indent=2))
        return EXIT_OK if all(item.ok for item in checks) else EXIT_ERROR
    for number, item in enumerate(checks):
        text = _describe(item)
        if text:
            print(("\n" if number else "") + text)
        if item.problems:
            count = len(item.problems)
            print(f"{item.file.name}: {_plural(count, 'problem')}:", file=sys.stderr)
            for problem in item.problems:
                print(f"  - {problem}", file=sys.stderr)
    return EXIT_OK if all(item.ok for item in checks) else EXIT_ERROR


def _is_folder(path: str) -> bool:
    from local_data_platform.paths import resolve_path

    return resolve_path(path).is_dir()


__all__ = ["add_cli"]
