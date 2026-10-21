"""The ``ldp metrics`` commands.

``ldp metrics list DIR [--json]`` lists the metrics of a folder with what they mean, ``ldp metrics validate DIR
[--json]`` checks their names and expressions against the gold tables, and ``ldp metrics query DIR METRICS
[--by DIMENSIONS] [--grain GRAIN] [--filter DIMENSION=VALUE] [--from DAY] [--to DAY] [--release RN | --live] [--sql]
[--out FILE] [--max-rows N] [--raw] [--json]`` computes some of them. ``cli.py`` wires this in with :func:`add_cli`.
Heavy imports happen inside the command.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

DEFAULT_MAX_ROWS = 100


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number of at least 1, got {value}")
    return value


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> Any:
    """Register ``ldp metrics`` on the ``ldp`` sub-command parser.

    Args:
        subparsers: The object ``argparse.ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options such as ``-v``.

    Returns:
        The ``metrics`` parser.
    """
    from local_data_platform.metrics.model import GRAINS

    parents = list(parents)
    metrics = subparsers.add_parser(
        "metrics", parents=parents, help="define metrics once, and compute them from the gold tables",
        description="A metrics file, with \"kind\": \"Metrics\", defines the numbers that a business reads once, with "
                    "what each means, who owns it and how it is shown, over the gold tables. Every report, export and "
                    "agent then computes them the same way. The commands read the metrics files of a folder of "
                    "configs.",
        epilog="Exit status is 0 on success and 1 on an error, and with validate also when a definition does not fit "
               "its table. Example: ldp metrics query ldp_demo/restaurant/config revenue --by cuisine --grain month")
    actions = metrics.add_subparsers(dest="metrics_command", metavar="COMMAND")

    def usage(args: argparse.Namespace) -> int:
        metrics.print_help()
        return 1

    metrics.set_defaults(handler=usage)

    listing = actions.add_parser(
        "list", parents=parents, help="list the metrics of a folder with what they mean",
        description="One row for each metric of the metrics files of DIR: its description, owner and format, the gold "
                    "table it comes from, and the dimensions to group it by and the grains of time it can be reported "
                    "at. Nothing is read from a table.",
        epilog="Example: ldp metrics list ldp_demo/restaurant/config")
    listing.add_argument("folder", metavar="DIR", help="a folder of configs that has a metrics file")
    listing.add_argument("--json", action="store_true", help="print the definitions as JSON")
    listing.set_defaults(handler=_cmd_list)

    validate = actions.add_parser(
        "validate", parents=parents, help="check the metrics of a folder against the gold tables",
        description="Check every name and every expression of the metrics files of DIR against the schema of the gold "
                    "table that it is about. A table has to exist, a dimension has to be one of its columns, the time "
                    "dimension has to be a date or a timestamp, and each measure has to work on the table and give a "
                    "number. Every problem is listed. No row of any table is read, and ldp gold build runs the same "
                    "check before it cuts a release.",
        epilog="Exit status is 0 when the definitions fit their tables and 1 when something is wrong. Example: "
               "ldp metrics validate ldp_demo/restaurant/config")
    validate.add_argument("folder", metavar="DIR", help="a folder of configs that has a metrics file and gold configs")
    validate.add_argument("--json", action="store_true", help="print the result as JSON")
    validate.set_defaults(handler=_cmd_validate)

    query = actions.add_parser(
        "query", parents=parents, help="compute metrics from the gold tables",
        description="Compute METRICS, one or more separated by commas, from the gold table that they are defined "
                    "over. Group them with --by and --grain, narrow them with --filter, --from and --to, and print "
                    "the result. A filter is dimension=value and is never SQL, because the value is bound as a "
                    "parameter. A ratio is the sum of one measure over the sum of another. The metrics of one query "
                    f"have to be of one table. The grains are {', '.join(GRAINS)}. By default the tables are read as "
                    "the current release holds them, so the numbers agree with every other reader of the release.",
        epilog="Example: ldp metrics query ldp_demo/restaurant/config revenue,avg_ticket --by cuisine --grain month "
               "--filter city=Pune --from 2026-01-01 --to 2026-03-31")
    query.add_argument("folder", metavar="DIR", help="a folder of configs, with a metrics file and the gold configs")
    query.add_argument("metrics", metavar="METRICS", help="the metrics to compute, such as revenue,avg_ticket")
    query.add_argument("--by", action="append", default=[], metavar="DIMENSIONS",
                       help="the dimensions to group by, separated by commas. Can be repeated")
    query.add_argument("--grain", metavar="GRAIN",
                       help=f"group by the start of the {', '.join(GRAINS)}. It has to be one that the time dimension "
                            "of the metric allows. Without it the period is added up into one row, which needs the "
                            "year grain to be allowed")
    query.add_argument("--filter", action="append", default=[], metavar="DIMENSION=VALUE", dest="filters",
                       help="keep the rows whose dimension is the value. Repeat it to add a filter. A dimension that "
                            "is given twice matches either value")
    query.add_argument("--from", metavar="DAY", dest="start", help="the first day to include, such as 2026-01-01")
    query.add_argument("--to", metavar="DAY", dest="end", help="the last day to include, such as 2026-03-31")
    when = query.add_mutually_exclusive_group()
    when.add_argument("--release", metavar="RELEASE",
                      help="read the tables as release RELEASE of ldp gold build names them, such as r2. The default "
                           "is the current release")
    when.add_argument("--live", action="store_true",
                      help="read the tables as they are now, and no release")
    query.add_argument("--sql", action="store_true",
                       help="print the SQL that would run, with the value of each parameter, and run nothing")
    query.add_argument("--out", metavar="FILE",
                       help="write the result to FILE as CSV, Parquet or JSONL by its ending .csv, .parquet or .jsonl. "
                            "The values are written as they were computed")
    query.add_argument("--max-rows", type=_positive, default=DEFAULT_MAX_ROWS, metavar="N", dest="max_rows",
                       help=f"print at most this many rows (default {DEFAULT_MAX_ROWS}). A file gets every row")
    query.add_argument("--raw", action="store_true",
                       help="print the values as they were computed, and not in the format of the metric")
    query.add_argument("--json", action="store_true", help="print the result as JSON")
    query.set_defaults(handler=_cmd_query)
    return metrics


def _plural(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _cmd_list(args: argparse.Namespace) -> int:
    from local_data_platform.metrics.load import load_metrics
    from local_data_platform.metrics.render import list_text

    definitions = load_metrics(args.folder)
    print(json.dumps(definitions.to_dict(), indent=2) if args.json else list_text(definitions))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from local_data_platform.metrics.check import validate_folder

    check = validate_folder(args.folder)
    if args.json:
        print(json.dumps(check.to_dict(), indent=2))
    elif check.valid:
        definitions = check.definitions
        print(f"{', '.join(check.files)}: {_plural(len(definitions.models), 'semantic model')} and "
              f"{_plural(len(definitions.metrics), 'metric')} fit the gold tables of {check.folder}")
    else:
        count = len(check.problems)
        print(f"ldp: {_plural(count, 'problem')} in the metrics of {check.folder}", file=sys.stderr)
        for problem in check.problems:
            print(f"  - {problem}", file=sys.stderr)
    return 0 if check.valid else 1


def _cmd_query(args: argparse.Namespace) -> int:
    from local_data_platform.metrics.render import file_format, json_rows, table_text, write_table
    from local_data_platform.metrics.runner import run

    if args.out:
        file_format(args.out)  # a name that cannot be written is found before the query runs
    result = run(args.folder, args.metrics, by=args.by, grain=args.grain, filters=args.filters, start=args.start,
                 end=args.end, release=args.release, live=args.live, execute=not args.sql)
    compiled = result.query
    if args.sql:
        print(json.dumps(compiled.to_dict(), indent=2) if args.json else compiled.explain())
        return 0
    table = result.table
    if args.out:
        kind = write_table(table, args.out)
        print(f"wrote {_plural(table.num_rows, 'row')} to {args.out} as {kind}. Read {result.source}.")
        return 0
    if args.json:
        print(json.dumps({
            **compiled.to_dict(), "source": result.source,
            "release": result.release.name if result.release is not None else None,
            "row_count": table.num_rows, "rows": json_rows(table)}, indent=2))
        return 0
    print(table_text(table, compiled.columns, max_rows=args.max_rows, raw=args.raw))
    print(f"read {result.source}")
    return 0
