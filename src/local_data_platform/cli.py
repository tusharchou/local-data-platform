"""The ``ldp`` command line.

Commands::

    ldp --version
    ldp run CONFIG [--mode {append,overwrite,upsert}]
    ldp pipelines
    ldp snapshots CONFIG
    ldp query CONFIG SQL [--max-rows N]
    ldp demo [--workdir DIR] [--rows N] [--seed N]

    ldp catalog test SPEC                  (catalog.provider)
    ldp schema | ldp plan CONFIG           (spec)
    ldp runs CONFIG                        (events)
    ldp commits CONFIG                     (format.iceberg.commit)
    ldp datasets pin|list|export ...       (datasets)
    ldp maintain CONFIG                    (maintenance)
    ldp spark CONFIG                       (engine.spark)
    ldp mcp --config DIR_OR_FILE           (mcp_server)

The commands in the second group are added by the ``add_cli`` function of the module named
next to them (``docs/design/v0_1_1_platform.md`` C9).

Every command exits with 0 on success and 1 on error. Errors are printed as one
friendly line on stderr; add ``-v`` for debug logging and the full traceback.

Heavy imports (pyiceberg, DuckDB) happen inside the commands. ``main`` imports the
second group's modules only when the command line may need them, so ``ldp --version``
and the first group's commands stay fast.
"""

import argparse
import datetime as dt
import importlib
import logging
import math
import sys
import traceback
from collections.abc import Sequence
from typing import Any

from local_data_platform import __version__
from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.logger import PACKAGE_LOGGER, configure_cli_logging

PROG = "ldp"
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


def _cmd_run(args: argparse.Namespace) -> int:
    from local_data_platform.etl import run_config

    result = run_config(args.config, mode=args.mode)
    _print(str(result))
    write = result.write_result
    if write is not None:
        _print(f"  target {write.table_identifier}: {write.mode}, {write.rows_before} -> {write.rows_after} rows, "
               f"snapshot {write.snapshot_id}")
    if len(result.quality):
        for line in result.quality.summary().splitlines():
            _print(f"  {line}")
    return 0


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
    from local_data_platform.engine.duckdb import DuckDBEngine
    from local_data_platform.etl import load_config

    config = load_config(args.config)
    table, alias = _iceberg_table(config, must_exist=True)
    with DuckDBEngine() as duck:
        duck.register_iceberg(table, alias)
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
        epilog="Exit status is 0 on success and 1 on error.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = commands.add_parser("run", parents=[common], help="run the pipeline a config describes",
                              description="Build the pipeline for CONFIG with the registry and run it.")
    run.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    run.add_argument("--mode", choices=WRITE_MODES, help="override the target's write_mode for this run")
    run.set_defaults(handler=_cmd_run)

    pipelines = commands.add_parser("pipelines", parents=[common], help="list the registered pipeline routes")
    pipelines.set_defaults(handler=_cmd_pipelines)

    snapshots = commands.add_parser("snapshots", parents=[common], help="list the snapshots of a config's table",
                                    description="List the snapshots of the config's Iceberg target (or source).")
    snapshots.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    snapshots.set_defaults(handler=_cmd_snapshots)

    query = commands.add_parser(
        "query", parents=[common], help="run SQL over a config's Iceberg table with DuckDB",
        description="Register the config's Iceberg target (or source) under its name and run SQL over it. "
                    'Needs the duckdb extra: pip install "local-data-platform[duckdb]".')
    query.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    query.add_argument("sql", metavar="SQL", help="the SQL to run; the table is named after target.name")
    query.add_argument("--max-rows", type=_positive_int, default=DEFAULT_MAX_ROWS,
                       help=f"print at most this many rows (default {DEFAULT_MAX_ROWS})")
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``ldp`` command line.

    Args:
        argv: The arguments, without the program name. Defaults to ``sys.argv[1:]``.

    Returns:
        The exit status: 0 on success, 1 on error (including usage errors).
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
    except Exception as error:  # noqa: BLE001 - the CLI turns every error into one line and exit 1
        if verbose:
            traceback.print_exc()
        first, *rest = _describe_error(error).splitlines() or [""]
        hint = "" if verbose or isinstance(error, LDPError) else " (run with -v for the traceback)"
        _error("\n".join([f"{PROG}: error: {first}{hint}", *rest]))
        return 1
    finally:
        # Leave logging as we found it, so main() can be called more than once in a process.
        for handler in package_logger.handlers:
            if handler not in saved_handlers:
                package_logger.removeHandler(handler)
        package_logger.setLevel(saved_level)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
