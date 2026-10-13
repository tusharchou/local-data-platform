"""The ``ldp`` command line.

Commands::

    ldp --version
    ldp run CONFIG [--mode {append,overwrite,upsert}] [--window START/END|yesterday|last-week|last-month]
                   [--idempotency-key KEY|auto] [--run-id ID [--attempt N]] [--json]
    ldp pipelines
    ldp snapshots CONFIG
    ldp query CONFIG [CONFIG ...] SQL [--max-rows N] [--snapshot ID | --as-of TIME]
    ldp demo [--workdir DIR] [--rows N] [--seed N]

    ldp catalog test SPEC                  (catalog.provider)
    ldp schema [--kind KIND]               (spec)
    ldp plan CONFIG [--window WINDOW]      (spec)
    ldp runs CONFIG                        (events)
    ldp commits CONFIG                     (format.iceberg.commit)
    ldp datasets pin|list|export ...       (datasets)
    ldp maintain CONFIG                    (maintenance)
    ldp spark CONFIG                       (engine.spark)
    ldp mcp --config DIR_OR_FILE           (mcp_server)
    ldp dbt init|build|test|docs|exec ...  (dbt_layer.cli)

The commands in the second group are added by the ``add_cli`` function of the module named
next to them (``docs/design/v0_1_1_platform.md`` C9).

Every command follows one exit-code table::

    0  success, including a run that published and a run that skipped a duplicate
    1  an error: a bad config, a usage error, a failed write
    2  reserved for an SLA breach that ``ldp monitor`` reports (0.1.5)
    3  a data gate held the run back, such as a quality check that blocked a write

A scheduler retries a run that exits with 1, because the cause may pass, and never retries one that exits with 3,
because the data has to change first.

Errors are printed as one friendly line on stderr; add ``-v`` for debug logging and the full traceback.

Heavy imports (pyiceberg, DuckDB) happen inside the commands. ``main`` imports the
second group's modules only when the command line may need them, so ``ldp --version``
and the first group's commands stay fast.
"""

import argparse
import datetime as dt
import importlib
import json
import logging
import math
import os
import sys
import traceback
from collections.abc import Sequence
from typing import Any

from local_data_platform import __version__
from local_data_platform.exceptions import ConfigError, DataQualityError, LDPError
from local_data_platform.logger import PACKAGE_LOGGER, configure_cli_logging

PROG = "ldp"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_SLA = 2
"""Reserved for an SLA breach that ``ldp monitor`` reports in 0.1.5. No command returns it yet."""
EXIT_GATE = 3
"""A data gate, such as a quality check, held the run back. A scheduler must not retry it."""
DEFAULT_WORKDIR = "ldp_demo"
DEFAULT_MAX_ROWS = 100
WRITE_MODES = ("append", "overwrite", "upsert")

CORE_COMMANDS = ("run", "pipelines", "snapshots", "query", "demo")
"""The commands ``cli.py`` defines itself."""

MODULE_COMMANDS = (
    ("local_data_platform.catalog.provider", ("catalog",)),
    ("local_data_platform.spec", ("schema", "plan")),
    ("local_data_platform.events", ("runs",)),
    ("local_data_platform.format.iceberg.commit", ("commits",)),
    ("local_data_platform.datasets", ("datasets",)),
    ("local_data_platform.maintenance.cli", ("maintain",)),
    ("local_data_platform.engine.spark", ("spark",)),
    ("local_data_platform.mcp_server.cli", ("mcp",)),
    ("local_data_platform.dbt_layer.cli", ("dbt",)),
)
"""``(module, commands)``: each module's ``add_cli(subparsers, parents=...)`` registers its commands."""


# ---------------------------------------------------------------------- output


def _cell(value: Any, float_digits: int | None = None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) >= 1e15:
            return repr(value)
        if float_digits is not None:
            return f"{value:.{float_digits}f}"
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def format_table(data: Any, max_rows: int | None = None, float_digits: int | None = None) -> str:
    """Render rows as an aligned plain-text table.

    Args:
        data: A ``pyarrow.Table``, or a list of dicts that share the same keys.
        max_rows: Show at most this many rows, then a ``... N more rows`` line.
        float_digits: Show floats with exactly this many decimals. By default
            floats show up to 6 decimals, without trailing zeros.

    Returns:
        The table as text, with a header and a separator line. Numbers are
        right-aligned, nulls show as ``NULL``.
    """
    if hasattr(data, "to_pylist") and hasattr(data, "column_names"):
        columns = list(data.column_names)
        total = data.num_rows
        rows = data.slice(0, max_rows).to_pylist() if max_rows is not None else data.to_pylist()
    else:
        rows = list(data)
        columns = list(rows[0]) if rows else []
        total = len(rows)
        rows = rows[:max_rows] if max_rows is not None else rows
    if not columns:
        return "(no columns)"
    cells = [[_cell(row.get(column), float_digits) for column in columns] for row in rows]
    numeric = [bool(rows) and all(isinstance(row.get(column), (int, float)) and not isinstance(row.get(column), bool)
                                  for row in rows if row.get(column) is not None)
               for column in columns]
    widths = [max([len(column)] + [len(line[i]) for line in cells]) for i, column in enumerate(columns)]

    def render(values: Sequence[str]) -> str:
        parts = [value.rjust(width) if is_number else value.ljust(width)
                 for value, width, is_number in zip(values, widths, numeric)]
        return "  ".join(parts).rstrip()

    lines = [render(columns), "  ".join("-" * width for width in widths)]
    lines += [render(line) for line in cells]
    if total > len(rows):
        lines.append(f"... {total - len(rows)} more rows")
    lines.append(f"({total} row{'s' if total != 1 else ''})")
    return "\n".join(lines)


def _print(text: str = "") -> None:
    print(text, file=sys.stdout)


def _error(text: str) -> None:
    print(text, file=sys.stderr)


# ---------------------------------------------------------------------- commands


def _iceberg_table(config, must_exist: bool = False):
    """Return ``(Iceberg, alias)`` for the config's Iceberg table: the target, else the source.

    With ``must_exist``, a config whose catalog file doesn't exist raises ``TableNotFound``
    before anything is created, so a read on a mistyped config leaves no empty catalog behind.
    """
    from local_data_platform.pipeline.builders import iceberg_from_config

    for section in ("target", "source"):
        block = config.metadata[section]
        if str(block.get("format", "")).strip().upper() == "ICEBERG":
            return iceberg_from_config(config, section, must_exist=must_exist), block["name"]
    raise ConfigError(f"config {config.identifier} has no Iceberg table: its source is {config.source['format']!r} "
                      f"and its target is {config.target['format']!r}")


def _staged_options(args: argparse.Namespace) -> tuple[str | None, str | None, str | None, int]:
    """The ``(window, key, run_id, attempt)`` of a run, after the checks that only the command line can make."""
    window, key, run_id = args.window, args.idempotency_key, args.run_id
    if args.attempt is not None and run_id is None:
        raise ConfigError("--attempt needs --run-id: a retry reuses the run id of the attempt before it")
    if run_id is not None and window is None and key is None:
        raise ConfigError("--run-id needs --window or --idempotency-key, because only a staged run has attempts")
    if key == "auto":
        if window is None:
            raise ConfigError("--idempotency-key auto needs --window, because the key is derived from the window")
        key = None  # the key that ldp plan prints for the config and the window
    return window, key, run_id, args.attempt or 1


def _cmd_run(args: argparse.Namespace) -> int:
    from local_data_platform.etl import load_config, run_config
    from local_data_platform.spec import window_bounds

    window, key, run_id, attempt = _staged_options(args)
    config = load_config(args.config)
    bounds = window_bounds(config, window)  # a shortcut such as yesterday, worked out once, in the config's time zone
    result = run_config(config, mode=args.mode, window=bounds, idempotency_key=key, run_id=run_id, attempt=attempt)
    if args.json:
        _print(json.dumps(result.to_dict(), indent=2))
        return EXIT_OK
    _print(str(result))
    write = result.write_result
    if write is not None:
        _print(f"  target {write.table_identifier}: {write.mode}, {write.rows_before} -> {write.rows_after} rows, "
               f"snapshot {write.snapshot_id}")
    if len(result.quality):
        for line in result.quality.summary().splitlines():
            _print(f"  {line}")
    if result.idempotency_key is not None:
        _print_staged(result, bounds, attempt)
    return EXIT_OK


def _print_staged(result: Any, bounds: Any, attempt: int) -> None:
    """What a staged run did, in the words a scheduler or a person reads: window, key, run, status, snapshot."""
    from local_data_platform.spec import format_instant

    lines = []
    if bounds is not None:
        lines.append(("window", f"{format_instant(bounds[0])}/{format_instant(bounds[1])}"))
    lines += [("idempotency_key", result.idempotency_key), ("run_id", result.run_id), ("attempt", str(attempt)),
              ("status", result.status), ("snapshot", str(result.published_snapshot_id))]
    width = max(len(name) for name, _ in lines)
    for name, value in lines:
        _print(f"  {name.ljust(width)}  {value}")


def _cmd_pipelines(args: argparse.Namespace) -> int:
    from local_data_platform.pipeline.registry import registered_pipelines

    rows = [
        {"source": route.source_format, "target": route.target_format, "engine": route.engine or "-",
         "pipeline": cls.__name__, "module": cls.__module__}
        for route, cls in registered_pipelines().items()
    ]
    _print(format_table(rows))
    return 0


def _cmd_snapshots(args: argparse.Namespace) -> int:
    from local_data_platform.etl import load_config

    config = load_config(args.config)
    table, _ = _iceberg_table(config, must_exist=True)
    snapshots = table.snapshots()
    if not snapshots:
        _print(f"Iceberg table {table.identifier} has no snapshots")
        return 0
    rows = []
    for snapshot in snapshots:
        committed = dt.datetime.fromtimestamp(snapshot["timestamp_ms"] / 1000, tz=dt.timezone.utc)
        summary = snapshot["summary"]
        rows.append({
            "snapshot_id": snapshot["snapshot_id"],
            "parent_id": snapshot["parent_id"],
            "committed_at_utc": committed.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
            "operation": snapshot["operation"],
            "total_records": _int_or_none(summary.get("total-records")),
            "added_records": _int_or_none(summary.get("added-records")),
            "deleted_records": _int_or_none(summary.get("deleted-records")),
        })
    _print(f"Snapshots of Iceberg table {table.identifier}, oldest first:")
    _print(format_table(rows))
    return 0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cmd_query(args: argparse.Namespace) -> int:
    from local_data_platform import lake
    from local_data_platform.engine.duckdb import DuckDBEngine
    from local_data_platform.etl import load_config

    if len(args.config) == 1 and not os.path.isdir(args.config[0]):
        # One config file: its Iceberg table, named after target.name.
        config = load_config(args.config[0])
        table, alias = _iceberg_table(config, must_exist=True)
        snapshot = None
        if args.snapshot is not None or args.as_of is not None:
            snapshot = lake.pick_snapshot([(item["snapshot_id"], item["timestamp_ms"]) for item in table.snapshots()],
                                          snapshot_id=args.snapshot, as_of=args.as_of, table=table.identifier)
        with DuckDBEngine() as duck:
            try:
                duck.register_iceberg(table, alias, snapshot_id=snapshot)
            except ValueError as error:  # an unknown snapshot id; the message lists the ones that exist
                raise ConfigError(str(error)) from error
            result = duck.query(args.sql)
    else:
        with lake.open_lake(args.config, snapshot_id=args.snapshot, as_of=args.as_of) as duck:
            result = duck.query(args.sql)
    _print(format_table(result, max_rows=args.max_rows))
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from local_data_platform.demo import run_demo

    run_demo(args.workdir, rows=args.rows, seed=args.seed)
    return 0


# ---------------------------------------------------------------------- parser


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number of at least 1, got {value}")
    return value


def _moment(text: str) -> dt.datetime:
    from local_data_platform.lake import parse_moment

    try:
        return parse_moment(text)
    except ConfigError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def build_parser(*, module_commands: bool = True) -> argparse.ArgumentParser:
    """Build the ``ldp`` argument parser.

    Args:
        module_commands: Also register the commands of :data:`MODULE_COMMANDS`, importing their
            modules (and through some of them pyiceberg). ``main`` turns this off when the command
            line names a core command or only asks for ``--version``.
    """
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS keeps a subcommand from resetting a -v given before it.
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="debug logging, and a full traceback on errors")

    parser = argparse.ArgumentParser(
        prog=PROG, parents=[common],
        description="local-data-platform: a local Iceberg lakehouse driven by JSON dataset configs.",
        epilog="Exit status is 0 on success, 1 on error and 3 when a data gate held a run back (2 is reserved).",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = commands.add_parser(
        "run", parents=[common], help="run the pipeline a config describes",
        description="Build the pipeline for CONFIG with the registry and run it. With --window or --idempotency-key "
                    "the run publishes through the staged protocol, so running it again for the same window finds "
                    "what it published and writes nothing, and a scheduler can retry it safely.",
        epilog="Exit status is 0 on success, including a skipped duplicate, 1 on an error and 3 when a data gate "
               "such as a quality check held the run back. Retry 1, and do not retry 3.")
    run.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    run.add_argument("--mode", choices=WRITE_MODES, help="override the target's write_mode for this run")
    run.add_argument("--window", metavar="WINDOW",
                     help="the logical window of the run: an ISO 8601 interval such as 2026-08-01/2026-08-02, or "
                          "yesterday, last-week or last-month, with the days counted in the time zone of the "
                          "config. The run reads the window's source path and rows, when the config says how")
    run.add_argument("--idempotency-key", metavar="KEY",
                     help="use KEY as the key of the run, or 'auto' for the key that 'ldp plan' prints for the "
                          "config and the window. A key alone makes the run staged too")
    run.add_argument("--run-id", metavar="ID",
                     help="the id of the run. A retry reuses the id of the attempt before it, with a higher --attempt")
    run.add_argument("--attempt", type=_positive_int, metavar="N",
                     help="the attempt number of the run, from 1. It needs --run-id, and it fences every older "
                          "attempt of the run")
    run.add_argument("--json", action="store_true",
                     help="print the result as JSON on stdout, for scripts and operators")
    run.set_defaults(handler=_cmd_run)

    pipelines = commands.add_parser("pipelines", parents=[common], help="list the registered pipeline routes")
    pipelines.set_defaults(handler=_cmd_pipelines)

    snapshots = commands.add_parser("snapshots", parents=[common], help="list the snapshots of a config's table",
                                    description="List the snapshots of the config's Iceberg target (or source).")
    snapshots.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    snapshots.set_defaults(handler=_cmd_snapshots)

    query = commands.add_parser(
        "query", parents=[common], help="run SQL over Iceberg tables with DuckDB",
        description="Register the Iceberg tables of one or more configs and run SQL over them. With one config "
                    "file, the table is named after target.name. With a folder of configs, or several configs, "
                    "every table is registered as namespace.table, and by its bare name when that is unique, so "
                    "SQL can join them. --snapshot and --as-of read an older version of one table. "
                    'Needs the duckdb extra: pip install "local-data-platform[duckdb]".',
        epilog="Put the options before the first CONFIG or after SQL.")
    query.add_argument("config", metavar="CONFIG", nargs="+",
                       help="a JSON dataset config, or a folder of them; give several to query their tables together")
    query.add_argument("sql", metavar="SQL", help="the SQL to run")
    query.add_argument("--max-rows", type=_positive_int, default=DEFAULT_MAX_ROWS,
                       help=f"print at most this many rows (default {DEFAULT_MAX_ROWS})")
    when = query.add_mutually_exclusive_group()
    when.add_argument("--snapshot", type=int, metavar="SNAPSHOT_ID",
                      help="read this snapshot instead of the current one (ldp snapshots lists them)")
    when.add_argument("--as-of", type=_moment, metavar="TIME",
                      help="read the newest snapshot committed at or before TIME, such as '2026-07-31 23:59:59' "
                           "(UTC unless it has an offset; a date alone is midnight)")
    query.set_defaults(handler=_cmd_query)

    demo = commands.add_parser("demo", parents=[common], help="run the offline end-to-end demo",
                               description="Generate synthetic taxi rides and walk through every feature.")
    demo.add_argument("--workdir", default=DEFAULT_WORKDIR,
                      help=f"folder for the demo's data, catalog and warehouse (default ./{DEFAULT_WORKDIR})")
    demo.add_argument("--rows", type=_positive_int, default=1000, help="number of synthetic rides (default 1000)")
    demo.add_argument("--seed", type=int, default=42, help="random seed for the synthetic data (default 42)")
    demo.set_defaults(handler=_cmd_demo)

    if module_commands:
        for module, _ in MODULE_COMMANDS:
            importlib.import_module(module).add_cli(commands, parents=[common])
    return parser


def _needs_module_commands(argv: Sequence[str]) -> bool:
    """Whether parsing ``argv`` may need :data:`MODULE_COMMANDS`.

    Only ``-v`` (no value) can come before the command, so the first token that isn't an option is
    the command. A core command or a bare ``--version`` doesn't need the module commands; anything
    else (another command, a typo, ``--help`` or no command) does, so the usage and the "invalid
    choice" message list every command.
    """
    for token in argv:
        if token == "--":
            break
        if not token.startswith("-"):
            return token not in CORE_COMMANDS
    return not ("--version" in argv and not {"-h", "--help"} & set(argv))


def _describe_error(error: BaseException) -> str:
    if isinstance(error, LDPError):
        return str(error)
    return f"{type(error).__name__}: {error}"


def _fail(error: BaseException, verbose: bool, status: int) -> int:
    """Print an error as one friendly line, and the traceback with ``-v``, and return its exit status."""
    if verbose:
        traceback.print_exc()
    first, *rest = _describe_error(error).splitlines() or [""]
    hint = "" if verbose or isinstance(error, LDPError) else " (run with -v for the traceback)"
    _error("\n".join([f"{PROG}: error: {first}{hint}", *rest]))
    return status


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``ldp`` command line.

    Args:
        argv: The arguments, without the program name. Defaults to ``sys.argv[1:]``.

    Returns:
        The exit status: 0 on success, 1 on error (including usage errors), and 3 when a data gate held
        the run back. See the exit-code table at the top of this module.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser(module_commands=_needs_module_commands(argv))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        # --help and --version exit with 0; usage errors (already printed) with 2.
        return 0 if exit_.code in (0, None) else 1
    if getattr(args, "handler", None) is None:
        parser.print_help(sys.stderr)
        return 1

    verbose = bool(getattr(args, "verbose", False))
    package_logger = logging.getLogger(PACKAGE_LOGGER)
    saved_level, saved_handlers = package_logger.level, list(package_logger.handlers)
    configure_cli_logging(logging.DEBUG if verbose else logging.WARNING)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        _error(f"{PROG}: interrupted")
        return 130
    except DataQualityError as error:  # a data gate held the run back: the data has to change, so do not retry
        return _fail(error, verbose, EXIT_GATE)
    except Exception as error:  # noqa: BLE001 - the CLI turns every error into one line and exit 1
        return _fail(error, verbose, EXIT_ERROR)
    finally:
        # Leave logging as we found it, so main() can be called more than once in a process.
        for handler in package_logger.handlers:
            if handler not in saved_handlers:
                package_logger.removeHandler(handler)
        package_logger.setLevel(saved_level)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
